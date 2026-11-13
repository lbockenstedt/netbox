"""Reserve statically addressed hosts in DHCP (``NETBOX_RESERVE_STATIC_HOSTS``).

A host configured with a static IP inside a Kea-served scope holds no lease,
so Kea may hand the same address to another client. This pass finds such
hosts and marks their NetBox IP as a DHCP reservation
(``custom_fields.mac_address`` + ``dhcp_reservation: true``). The hub's next
NetBox → Kea sync then pushes the reservation to Kea.

The hub owns the policy (on/off, interval), knows which agents and which Kea
serve each tenant, and relays what this spoke cannot reach itself: the raw
``GET_HOST_ADDRESSING`` answers of the tenant's agents, the hub's sweep and
ARP observations, and a strict lease and subnet read of the tenant's Kea.
This spoke does the rest: it reads the tenant's prefixes and IPs from its own
NetBox, decides which addresses to reserve (``plan_static_reservations``)
and writes them.

Writes are reversible. Each write is a compare-and-set on the two custom
fields and is reported as ``{before, after}``. If a write fails, the writes
already made in this pass are rolled back in reverse order and the pass
stops. The hub sends ``{"rollback": changes}`` when the DHCP sync that
follows the pass fails; a rollback restores ``before`` only where NetBox
still holds ``after``, so it never undoes a later edit.

Audience: NetBox spoke developers.
"""

import ipaddress
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Sweep observations: seen answering within a day, ARP MAC under 2h old.
SEEN_WINDOW = 24 * 3600
ARP_MAX_AGE = 2 * 3600
# Writes per pass unless the hub sends ``max_writes``.
DEFAULT_MAX_WRITES = 25
# An address whose write failed is not retried for this long (in memory).
WRITE_BACKOFF = 3600
# The two custom fields this pass owns.
FIELDS = ("mac_address", "dhcp_reservation")

_MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")


def _mac(value: Any) -> Optional[str]:
    """Lower-colon MAC; ``""`` when absent, ``None`` when present but malformed."""
    text = str(value or "").strip().lower()
    # Absent is not an observation; anything present must be a real MAC.
    if not text:
        return ""
    hexd = re.sub(r"[^0-9a-f]", "", text)
    mac = ":".join(hexd[i:i + 2] for i in range(0, 12, 2)) if len(hexd) == 12 else text
    return mac if _MAC_RE.fullmatch(mac) else None


def _norm_mac(value: Any) -> str:
    """Comparable MAC: lower-colon, or ``""`` for absent and malformed values."""
    return _mac(value) or ""


def _vrf(value: Any) -> str:
    """A NetBox VRF as one comparable string: dict → id (else name),
    anything else → its name; None/"" → ``""`` (the global table)."""
    if isinstance(value, dict):
        key = value.get("id")
        return str(key) if key not in (None, "") else str(value.get("name") or "")
    return str(value or "").strip()


def _host(address: Any) -> str:
    """``"10.0.0.5/24"`` → ``"10.0.0.5"``; ``""`` for anything not an IPv4 host."""
    try:
        ip = ipaddress.ip_address(str(address or "").split("/")[0].strip())
    except ValueError:
        return ""
    return str(ip) if ip.version == 4 else ""


def _status(row: Dict[str, Any]) -> str:
    """NetBox status as its value (it comes as a string or ``{value, label}``)."""
    status = row.get("status")
    if isinstance(status, dict):
        status = status.get("value")
    return str(status or "").lower()


def _is_dhcp_scope(prefix: Dict[str, Any]) -> bool:
    """Whether a prefix becomes a Kea scope. The same rule as lm core's
    ``dns_dhcp_sync.is_dhcp_scope`` (prefix set, not a container,
    ``dhcp_enabled`` ticked); kept here so this spoke does not depend on the
    version of lm core installed beside it."""
    if not isinstance(prefix, dict) or not prefix.get("prefix"):
        return False
    if _status(prefix) == "container":
        return False
    return bool((prefix.get("custom_fields") or {}).get("dhcp_enabled"))


def _scopes(prefixes: Iterable[Dict[str, Any]]) -> List[Tuple[Any, str]]:
    """DHCP-served prefixes as ``[(network, vrf)]``, most specific first."""
    out = []
    for p in prefixes or []:
        if not _is_dhcp_scope(p):
            continue
        try:
            net = ipaddress.ip_network(str(p["prefix"]), strict=False)
        except ValueError:
            continue
        if net.version == 4:
            out.append((net, _vrf(p.get("vrf"))))
    return sorted(out, key=lambda s: -s[0].prefixlen)


def _kea_pools(kea_subnets: Iterable[Dict[str, Any]]
               ) -> List[Tuple[Any, Optional[List[Tuple[int, int]]]]]:
    """Kea v4 subnets as ``[(network, [(first, last)] or None)]``, most
    specific first. ``None`` pools means a pool could not be parsed."""
    out = []
    for sub in kea_subnets or []:
        try:
            net = ipaddress.ip_network(str(sub.get("subnet") or ""), strict=False)
        except ValueError:
            continue
        # Each pool is "a - b" or a CIDR; anything else makes the pools unknown.
        ranges: Optional[List[Tuple[int, int]]] = []
        for p in sub.get("pools") or []:
            text = str((p or {}).get("pool") if isinstance(p, dict) else p or "")
            try:
                if "/" in text:
                    pn = ipaddress.ip_network(text.strip(), strict=False)
                    ranges.append((int(pn.network_address), int(pn.broadcast_address)))
                else:
                    a, b = text.split("-")
                    ranges.append((int(ipaddress.ip_address(a.strip())),
                                   int(ipaddress.ip_address(b.strip()))))
            except ValueError:
                ranges = None
                break
        out.append((net, ranges))
    return sorted(out, key=lambda s: -s[0].prefixlen)


def _kea_reservations(kea_subnets: Optional[Iterable[Dict[str, Any]]]
                      ) -> Optional[Tuple[Dict[str, set], Dict[str, set]]]:
    """Kea host reservations as ``({ip: {mac}}, {mac: {ip}})``.

    LM's Kea keeps every host reservation inline in its subnet4 config and
    omits ``reservations`` for a subnet with none. No subnet read, or a
    ``reservations`` value that is not a list, means unknown (None). A
    reservation keyed by a client-id or DUID indexes its IP with an empty
    MAC, so the IP still counts as taken.
    """
    # No subnet read at all: unknown.
    if kea_subnets is None:
        return None
    # Index every subnet's reservations both ways.
    by_ip: Dict[str, set] = {}
    by_mac: Dict[str, set] = {}
    for sub in kea_subnets:
        res = sub.get("reservations", []) if isinstance(sub, dict) else None
        if not isinstance(res, list):
            return None
        for r in res:
            ip = _host((r or {}).get("ip-address"))
            mac = _norm_mac((r or {}).get("hw-address"))
            if ip:
                by_ip.setdefault(ip, set()).add(mac)
            if mac and ip:
                by_mac.setdefault(mac, set()).add(ip)
    return by_ip, by_mac


def candidates(tenant: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tenant's candidate addresses from the data the hub relayed.

    ``agent_addresses`` are the raw ``GET_HOST_ADDRESSING`` rows
    (``{ip, mac, mode}``); only ``static`` counts, and ``dhcp`` vetoes.
    ``sweep`` rows (``{ip, mac, seen_age, arp_age}``) count as static when the
    host answered within ``SEEN_WINDOW`` and its MAC came from an ARP table
    younger than ``ARP_MAX_AGE``.
    """
    # Agent rows keep their mode.
    out = []
    for a in tenant.get("agent_addresses") or []:
        if isinstance(a, dict):
            out.append({"ip": a.get("ip"), "mac": a.get("mac"),
                        "mode": a.get("mode") or "unknown", "source": "agent"})
    # Sweep rows count as static only while both sightings are fresh.
    for s in tenant.get("sweep") or []:
        if not isinstance(s, dict) or not s.get("mac"):
            continue
        try:
            fresh = (float(s.get("seen_age")) <= SEEN_WINDOW
                     and float(s.get("arp_age")) <= ARP_MAX_AGE)
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            out.append({"ip": s.get("ip"), "mac": s.get("mac"),
                        "mode": "static", "source": "sweep"})
    return out


def plan_static_reservations(prefixes: Iterable[Dict[str, Any]],
                             ips: Iterable[Dict[str, Any]],
                             leases: Iterable[Dict[str, Any]],
                             cands: Iterable[Dict[str, Any]],
                             kea_subnets: Optional[Iterable[Dict[str, Any]]] = None
                             ) -> Dict[str, List]:
    """Decide which candidates become reservations. Pure; no I/O.

    Args:
        prefixes: the tenant's NetBox prefixes (``get_prefixes`` rows).
        ips: the tenant's NetBox IPs (``get_ip_addresses`` rows).
        leases: Kea v4 lease dicts (``ip-address``, ``hw-address``).
        cands: ``{ip, mac, mode, source}`` from :func:`candidates`.
        kea_subnets: Kea v4 subnets (``subnet``, ``pools``,
            ``reservations``). None means unknown: nothing is reserved.

    A MAC seen at several candidate IPs is skipped, except that addresses an
    agent reports as ``unknown`` (a secondary or VIP on the same NIC) are left
    out of that count. Anything that differs from what the candidate claims
    (another MAC or reservation in NetBox or Kea, a lease for another MAC, a
    declined address) is a conflict and is never overwritten.

    Returns:
        ``{"reserve": [{ip_id, ip, mac, source, before}], "conflicts": [{ip,
        mac, reason}], "skipped": [{ip, reason}]}``. ``before`` holds the two
        custom fields as NetBox stores them now.
    """
    reserve: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    scopes = _scopes(prefixes)
    kea_nets = _kea_pools(kea_subnets)
    kea_res = _kea_reservations(kea_subnets)

    # Index NetBox IPs by host address; existing reservations by MAC.
    nb_rows: Dict[str, List[Dict[str, Any]]] = {}
    reserved_mac_at: Dict[str, str] = {}
    for row in ips or []:
        ip = _host(row.get("address"))
        if not ip:
            continue
        nb_rows.setdefault(ip, []).append(row)
        cf = row.get("custom_fields") or {}
        mac = _norm_mac(cf.get("mac_address"))
        if cf.get("dhcp_reservation") and mac:
            reserved_mac_at.setdefault(mac, ip)

    # Index Kea leases by IP and the set of MACs holding any lease.
    lease_macs_by_ip: Dict[str, set] = {}
    leased_macs = set()
    for lease in leases or []:
        ip = _host(lease.get("ip-address") or lease.get("ip"))
        mac = _norm_mac(lease.get("hw-address") or lease.get("mac"))
        if ip:
            lease_macs_by_ip.setdefault(ip, set()).add(mac)
        if mac:
            leased_macs.add(mac)

    # Merge candidates per IP: an agent "dhcp" answer vetoes; MACs must agree.
    merged: Dict[str, Dict[str, Any]] = {}
    for c in cands or []:
        ip, mac = _host(c.get("ip")), _mac(c.get("mac"))
        if not ip:
            continue
        m = merged.setdefault(ip, {"macs": set(), "modes": set(),
                                   "sources": set(), "bad_mac": False})
        if mac is None:
            m["bad_mac"] = True
        elif mac:
            m["macs"].add(mac)
        m["modes"].add(str(c.get("mode") or "unknown"))
        m["sources"].add(str(c.get("source") or ""))
    mac_ip_count: Dict[str, int] = {}
    for m in merged.values():
        if "unknown" in m["modes"]:
            continue
        for mac in m["macs"]:
            mac_ip_count[mac] = mac_ip_count.get(mac, 0) + 1

    for ip, m in sorted(merged.items()):
        # Step 1: exactly one well-formed MAC, used at no other candidate IP.
        if m["bad_mac"]:
            skipped.append({"ip": ip, "reason": "malformed mac"})
            continue
        if len(m["macs"]) > 1:
            skipped.append({"ip": ip, "reason": "conflicting macs"})
            continue
        if not m["macs"]:
            skipped.append({"ip": ip, "reason": "mac unknown"})
            continue
        mac = next(iter(m["macs"]))
        if mac_ip_count.get(mac, 0) > 1:
            skipped.append({"ip": ip, "reason": "mac seen at several addresses"})
            continue
        # Step 2: every source must say static; an agent's dhcp/unknown vetoes.
        if "dhcp" in m["modes"]:
            skipped.append({"ip": ip, "reason": "agent reports dhcp"})
            continue
        if m["modes"] != {"static"}:
            skipped.append({"ip": ip, "reason": "not known to be static"})
            continue
        # Step 3: inside some Kea-served prefix of this tenant.
        addr = ipaddress.ip_address(ip)
        if not any(addr in s[0] for s in scopes):
            skipped.append({"ip": ip, "reason": "outside any dhcp scope"})
            continue
        # Step 4: exactly one NetBox IP object, in a scope of exactly its VRF.
        rows = nb_rows.get(ip) or []
        if len(rows) != 1:
            skipped.append({"ip": ip, "reason": "no netbox ip" if not rows
                            else "several netbox ips"})
            continue
        row = rows[0]
        scope = next((s for s in scopes if addr in s[0] and s[1] == _vrf(row.get("vrf"))), None)
        if scope is None:
            skipped.append({"ip": ip, "reason": "vrf differs from scope"})
            continue
        if addr in (scope[0].network_address, scope[0].broadcast_address):
            skipped.append({"ip": ip, "reason": "network or broadcast address"})
            continue
        if _status(row) == "dhcp":
            skipped.append({"ip": ip, "reason": "netbox status is dhcp"})
            continue
        cf = row.get("custom_fields") or {}
        nb_mac = _norm_mac(cf.get("mac_address"))
        # Step 5: never overwrite a different reservation or MAC.
        if cf.get("dhcp_reservation"):
            if nb_mac == mac:
                skipped.append({"ip": ip, "reason": "already reserved"})
            else:
                conflicts.append({"ip": ip, "mac": mac, "reason": "reserved for another mac"})
            continue
        if nb_mac and nb_mac != mac:
            conflicts.append({"ip": ip, "mac": mac, "reason": "netbox mac differs"})
            continue
        if reserved_mac_at.get(mac, ip) != ip:
            conflicts.append({"ip": ip, "mac": mac, "reason": "mac reserved at another address"})
            continue
        # Step 5b: the same against Kea's own host reservations (unknown → wait).
        if kea_res is None:
            skipped.append({"ip": ip, "reason": "kea reservations unknown"})
            continue
        kea_ip_macs, kea_mac_ips = kea_res
        if not any(addr in k[0] for k in kea_nets):
            skipped.append({"ip": ip, "reason": "not in a kea subnet"})
            continue
        if ip in kea_ip_macs:
            if kea_ip_macs[ip] == {mac}:
                skipped.append({"ip": ip, "reason": "already reserved"})
            else:
                conflicts.append({"ip": ip, "mac": mac, "reason": "reserved in kea for another mac"})
            continue
        if kea_mac_ips.get(mac):
            conflicts.append({"ip": ip, "mac": mac,
                              "reason": "mac reserved in kea at another address"})
            continue
        # Step 6: any Kea lease for the IP or the MAC means not static. A
        # declined lease has no MAC; HA peers may disagree (one declined,
        # one leased), which is reported as its own conflict.
        lease_macs = lease_macs_by_ip.get(ip)
        if lease_macs:
            if lease_macs == {mac}:
                skipped.append({"ip": ip, "reason": "dynamic lease"})
            elif lease_macs == {""}:
                conflicts.append({"ip": ip, "mac": mac, "reason": "address declined in kea"})
            elif "" in lease_macs:
                conflicts.append({"ip": ip, "mac": mac,
                                  "reason": "kea leases disagree (declined and leased)"})
            else:
                conflicts.append({"ip": ip, "mac": mac, "reason": "leased to another mac"})
            continue
        if mac in leased_macs:
            skipped.append({"ip": ip, "reason": "mac holds a lease"})
            continue
        # Step 7: a sweep-only host needs positive evidence — an IP outside
        # every Kea pool (a DHCP client always holds a pool address). An
        # agent's "static" is not pool-checked: a static host inside a pool
        # is the very clash this pass prevents.
        if "agent" not in m["sources"]:
            kea = next((k for k in kea_nets if addr in k[0]), None)
            if kea is None or kea[1] is None:
                skipped.append({"ip": ip, "reason": "kea pools unknown"})
                continue
            if any(lo <= int(addr) <= hi for lo, hi in kea[1]):
                skipped.append({"ip": ip, "reason": "inside a dhcp pool"})
                continue
        reserve.append({"ip_id": row.get("id"), "ip": ip, "mac": mac,
                        "source": ",".join(sorted(x for x in m["sources"] if x)),
                        "before": {k: cf.get(k) for k in FIELDS}})
    return {"reserve": reserve, "conflicts": conflicts, "skipped": skipped}


def _fields(cf: Dict[str, Any]) -> Dict[str, Any]:
    """The two custom fields this pass owns, exactly as stored."""
    return {k: (cf or {}).get(k) for k in FIELDS}


def compare_and_set(engine, ip_id: Any, expect: Dict[str, Any],
                    new: Dict[str, Any]) -> Tuple[str, str]:
    """Set the IP's two custom fields to ``new`` only if they still equal
    ``expect`` exactly. Other custom fields are kept.

    Returns ``(outcome, detail)``; outcome is one of ``written``,
    ``already`` (NetBox already holds ``new``), ``changed`` (NetBox holds
    something else: another writer got there first), ``gone`` (the IP was
    deleted) or ``failed`` (NetBox refused; ``detail`` says why).
    """
    try:
        obj = engine.nb.ipam.ip_addresses.get(int(ip_id))
        if not obj:
            return "gone", "netbox ip deleted since it was read"
        fields = dict(obj.custom_fields or {})
        if _fields(fields) == _fields(new):
            return "already", ""
        if _fields(fields) != _fields(expect):
            return "changed", "changed in netbox since it was read"
        fields.update(_fields(new))
        obj.custom_fields = fields
        obj.save()
        return "written", ""
    except Exception as e:  # noqa: BLE001 — reported, never raised
        return "failed", str(e) or type(e).__name__


def roll_back(engine, changes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Undo ``changes`` (newest first): restore ``before`` where NetBox still
    holds ``after``.

    Returns ``{status, rolled_back, rollback_kept, rollback_failed}``:
    restored (or already back to ``before``, so a repeated rollback is
    harmless); left alone on purpose because NetBox changed since (a later
    edit is never undone) or the IP is gone, with a ``reason``; and could not
    be restored, with an ``error``. Only the last makes ``status`` ERROR.
    """
    # Undo newest first, sorting each change by what NetBox holds now.
    done, kept, failed = [], [], []
    for c in reversed(list(changes or [])):
        if not isinstance(c, dict) or c.get("ip_id") in (None, ""):
            continue
        outcome, detail = compare_and_set(engine, c["ip_id"], c.get("after") or {},
                                          c.get("before") or {})
        if outcome in ("written", "already"):
            done.append(c)
        elif outcome in ("changed", "gone"):
            kept.append({**c, "reason": detail})
        else:
            failed.append({**c, "error": detail})
    # Log and report.
    if done or kept or failed:
        logger.info("static reservations: rolled back %d, left %d, could not roll back %d",
                    len(done), len(kept), len(failed))
    return {"status": "ERROR" if failed else "SUCCESS", "rolled_back": done,
            "rollback_kept": kept, "rollback_failed": failed}


def reserve_static_hosts(engine, data: Dict[str, Any], failed_writes: Dict[str, float],
                         now: float) -> Dict[str, Any]:
    """One pass over the tenants the hub sent (blocking; run in a thread).

    ``data``: ``{tenants: [{ref, filters, agent_addresses, sweep, kea}],
    kea: {key: {leases, subnets}}, max_writes}``. ``ref`` is an opaque label
    the hub maps back to its tenant; ``kea`` names the tenant's entry in the
    shared ``kea`` map, so a Kea serving several tenants is sent once.

    ``failed_writes`` (``{"<filters>|<ip>": when}``) carries the write
    backoff between passes.

    Returns ``{status, evaluated, changes, conflicts, errors, skipped,
    deferred}``; ``status`` is ERROR whenever ``errors`` is not empty. An IP
    another writer changed (or deleted) after the read is a conflict and
    the rest of the pass goes on. When a write fails, the pass stops,
    ``changes`` is empty and the earlier writes are in ``rolled_back``,
    ``rollback_kept`` or ``rollback_failed`` (see :func:`roll_back`).
    """
    report: Dict[str, Any] = {"status": "SUCCESS", "evaluated": [], "changes": [],
                              "conflicts": [], "errors": [], "skipped": 0, "deferred": 0}
    max_writes = int(data.get("max_writes") or DEFAULT_MAX_WRITES)
    kea = data.get("kea") or {}
    for key in [k for k, at in failed_writes.items() if now - at >= WRITE_BACKOFF]:
        del failed_writes[key]

    # Plan every tenant first, so a write cap applies across the pass.
    planned = []
    for t in data.get("tenants") or []:
        ref = str(t.get("ref") or "")
        filters = {k: v for k, v in (t.get("filters") or {}).items()
                   if k in ("tenant", "tenant_group") and v}
        if not filters:
            report["errors"].append({"ref": ref, "error": "no netbox tenant scope"})
            continue
        state = kea.get(str(t.get("kea") or "")) or {}
        if not isinstance(state.get("leases"), list) or not isinstance(state.get("subnets"), list):
            report["errors"].append({"ref": ref, "error": "kea state missing"})
            continue
        pfx = engine.get_prefixes(**filters)
        ips = engine.get_ip_addresses(**filters)
        bad = next((r for r in (pfx, ips) if not isinstance(r, dict)
                    or r.get("status") != "SUCCESS"), None)
        if bad is not None:
            report["errors"].append({"ref": ref, "error": "netbox read: "
                                     + str((bad or {}).get("message") or "failed")})
            continue
        plan = plan_static_reservations(pfx.get("prefixes"), ips.get("ip_addresses"),
                                        state["leases"], candidates(t), state["subnets"])
        report["evaluated"].append(ref)
        report["skipped"] += len(plan["skipped"])
        report["conflicts"].extend({"ref": ref, **c} for c in plan["conflicts"])
        fkey = "|".join(f"{k}={filters[k]}" for k in sorted(filters))
        planned.extend((ref, f"{fkey}|{r['ip']}", r) for r in plan["reserve"])

    # Write, capped; a failure rolls back this pass's earlier writes.
    for ref, key, r in planned:
        if len(report["changes"]) >= max_writes:
            report["deferred"] += 1
            continue
        if r.get("ip_id") is None:
            report["skipped"] += 1
            continue
        if now - failed_writes.get(key, float("-inf")) < WRITE_BACKOFF:
            report["errors"].append({"ref": ref, "ip": r["ip"],
                                     "error": "earlier write failed; retry after backoff"})
            continue
        after = {"mac_address": r["mac"], "dhcp_reservation": True}
        outcome, detail = compare_and_set(engine, r["ip_id"], r["before"], after)
        if outcome in ("changed", "gone"):
            # Another writer got there first: hold this IP back, keep the rest.
            report["conflicts"].append({"ref": ref, "ip": r["ip"], "mac": r["mac"],
                                        "reason": detail})
            continue
        if outcome == "failed":
            failed_writes[key] = now
            report["errors"].append({"ref": ref, "ip": r["ip"], "error": detail})
            undo = roll_back(engine, report["changes"])
            report.update(changes=[], rolled_back=undo["rolled_back"],
                          rollback_kept=undo["rollback_kept"],
                          rollback_failed=undo["rollback_failed"],
                          message=f"write for {r['ip']} failed; earlier writes of this "
                                  "pass rolled back")
            logger.warning("static reservations: write for %s failed (%s); pass rolled back",
                           r["ip"], detail)
            break
        failed_writes.pop(key, None)
        if outcome == "already":
            report["skipped"] += 1
            continue
        report["changes"].append({"ref": ref, "ip": r["ip"], "ip_id": r["ip_id"],
                                  "mac": r["mac"], "source": r["source"],
                                  "before": r["before"], "after": after})
        logger.info("static reservations: reserved %s (%s)", r["ip"], r["source"])
    # ERROR whenever anything went wrong, so "every read failed" never reads
    # as an empty success; the rows say what.
    if report["errors"]:
        report["status"] = "ERROR"
        report.setdefault("message", f"{len(report['errors'])} error(s); see errors")
    return report

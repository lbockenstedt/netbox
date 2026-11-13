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

Device replacement: an IP NetBox reserves for an old MAC whose host now
answers with a new one (new NIC, server, iLO, switch) moves to the new MAC,
on the IP and on its interface, when every check passes and the hub's
``replace_watch`` shows the waits are over (``auto_replace``), or at once
from the "Use new MAC" button (:func:`replace_reservation_mac`). The hub
keeps the waiting state; this module keeps none.

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
# Automatic device replacement: the new MAC seen at the IP for this long
# (so on at least two passes) and the old MAC unseen anywhere for this long.
REPLACE_NEW_MIN_AGE = 1800
REPLACE_OLD_UNSEEN = 24 * 3600
# Writes a replacement may need (the IP, and its interface's MAC).
REPLACE_WRITES = 2
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


def _age(value: Any) -> Optional[float]:
    """An age in seconds from the hub, or None when missing or not a number."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def plan_static_reservations(prefixes: Iterable[Dict[str, Any]],
                             ips: Iterable[Dict[str, Any]],
                             leases: Iterable[Dict[str, Any]],
                             cands: Iterable[Dict[str, Any]],
                             kea_subnets: Optional[Iterable[Dict[str, Any]]] = None,
                             replace: Optional[Dict[str, Any]] = None
                             ) -> Dict[str, List]:
    """Decide which candidates become reservations. Pure; no I/O.

    Args:
        prefixes: the tenant's NetBox prefixes (``get_prefixes`` rows).
        ips: the tenant's NetBox IPs (``get_ip_addresses`` rows).
        leases: Kea v4 lease dicts (``ip-address``, ``hw-address``).
        cands: ``{ip, mac, mode, source}`` from :func:`candidates`.
        kea_subnets: Kea v4 subnets (``subnet``, ``pools``,
            ``reservations``). None means unknown: nothing is reserved.
        replace: device replacement policy ``{auto, watch, force}``.
            ``auto`` (bool) lets a NetBox reservation move to a new MAC once
            ``watch[ip]`` (the hub's ``{old_mac, new_mac, first_seen_age,
            old_unseen_age}``) shows the new MAC for ``REPLACE_NEW_MIN_AGE``
            and the old MAC unseen for ``REPLACE_OLD_UNSEEN``. ``force``
            (``{ip, old_mac, new_mac}``, the "Use new MAC" button) replaces
            that one IP without the waits. Every other check still applies.

    A MAC seen at several candidate IPs is skipped, except that addresses an
    agent reports as ``unknown`` (a secondary or VIP on the same NIC) are left
    out of that count. Anything that differs from what the candidate claims
    (another MAC or reservation in NetBox or Kea, a lease for another MAC, a
    declined address) is a conflict and is never overwritten. The one
    exception is a device replacement: NetBox holds old MAC A for the IP, the
    new MAC B passes every check a new reservation needs, and A holds no
    lease, is reserved nowhere else and is not seen in this pass.

    Returns:
        ``{"reserve": [{ip_id, ip, mac, source, before}], "replace": [{ip_id,
        ip, mac, old_mac, source, before}], "pending_replace": [{ip, old_mac,
        new_mac, first_seen_age}], "conflicts": [{ip, mac, reason}],
        "skipped": [{ip, reason}]}``. ``before`` holds the two custom fields
        as NetBox stores them now. A conflict where NetBox holds another MAC
        also has ``old_mac`` and ``replace`` (why it was not replaced).
    """
    reserve: List[Dict[str, Any]] = []
    replace_rows: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    scopes = _scopes(prefixes)
    kea_nets = _kea_pools(kea_subnets)
    kea_res = _kea_reservations(kea_subnets)
    replace = replace or {}
    auto = bool(replace.get("auto"))
    watch = replace.get("watch") or {}
    force = replace.get("force") or {}

    # Index NetBox IPs by host address; existing reservations by MAC.
    nb_rows: Dict[str, List[Dict[str, Any]]] = {}
    reserved_at: Dict[str, set] = {}
    for row in ips or []:
        ip = _host(row.get("address"))
        if not ip:
            continue
        nb_rows.setdefault(ip, []).append(row)
        cf = row.get("custom_fields") or {}
        mac = _norm_mac(cf.get("mac_address"))
        if cf.get("dhcp_reservation") and mac:
            reserved_at.setdefault(mac, set()).add(ip)

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
    seen_macs = set()
    for c in cands or []:
        ip, mac = _host(c.get("ip")), _mac(c.get("mac"))
        if mac:
            seen_macs.add(mac)
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

    def _new_mac_block(ip: str, addr: Any, mac: str, sources: set,
                       old: str, repeat: bool = False) -> Optional[Tuple[str, str]]:
        """Why ``mac`` may not be reserved at ``ip``, as ``(list, reason)``,
        or None. ``old`` is the MAC NetBox reserves the IP for now (a
        replacement), whose own Kea reservation does not count. On a
        ``repeat`` of the button, Kea may already reserve the IP for ``mac``
        (the sync pushed it) and every other check still runs."""
        if reserved_at.get(mac, set()) - {ip}:
            return "conflicts", "mac reserved at another address"
        # The same against Kea's own host reservations (unknown → wait).
        if kea_res is None:
            return "skipped", "kea reservations unknown"
        kea_ip_macs, kea_mac_ips = kea_res
        if not any(addr in k[0] for k in kea_nets):
            return "skipped", "not in a kea subnet"
        held = kea_ip_macs.get(ip)
        if held is not None and not (old and held == {old}) and not (repeat and held == {mac}):
            if held == {mac}:
                return "skipped", "already reserved"
            return "conflicts", "reserved in kea for another mac"
        if kea_mac_ips.get(mac, set()) - {ip}:
            return "conflicts", "mac reserved in kea at another address"
        # Any Kea lease for the IP or the MAC means not static. A declined
        # lease has no MAC; HA peers may disagree (one declined, one
        # leased), which is reported as its own conflict.
        lease_macs = lease_macs_by_ip.get(ip)
        if lease_macs:
            if lease_macs == {mac}:
                return "skipped", "dynamic lease"
            if lease_macs == {""}:
                return "conflicts", "address declined in kea"
            if "" in lease_macs:
                return "conflicts", "kea leases disagree (declined and leased)"
            return "conflicts", "leased to another mac"
        if mac in leased_macs:
            return "skipped", "mac holds a lease"
        # A sweep-only host needs positive evidence — an IP outside every
        # Kea pool (a DHCP client always holds a pool address). An agent's
        # "static" is not pool-checked: a static host inside a pool is the
        # very clash this pass prevents.
        if "agent" not in sources:
            kea = next((k for k in kea_nets if addr in k[0]), None)
            if kea is None or kea[1] is None:
                return "skipped", "kea pools unknown"
            if any(lo <= int(addr) <= hi for lo, hi in kea[1]):
                return "skipped", "inside a dhcp pool"
        return None

    def _old_mac_block(ip: str, old: str) -> Optional[str]:
        """Why old MAC ``old`` may not give up ``ip``, or None."""
        if old in leased_macs:
            return "old mac holds a lease"
        if reserved_at.get(old, set()) - {ip}:
            return "old mac reserved at another address"
        if kea_res is not None and kea_res[1].get(old, set()) - {ip}:
            return "old mac reserved in kea at another address"
        if old in seen_macs:
            return "old mac still seen"
        return None

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
        source = ",".join(sorted(x for x in m["sources"] if x))
        before = {k: cf.get(k) for k in FIELDS}
        # Step 5: NetBox holds another MAC. Never overwritten, except a
        # device replacement that passes every check (see above).
        if nb_mac and nb_mac != mac:
            reserved = bool(cf.get("dhcp_reservation"))
            block = _new_mac_block(ip, addr, mac, m["sources"], nb_mac)
            why = block[1] if block else _old_mac_block(ip, nb_mac)
            row_out = {"ip_id": row.get("id"), "ip": ip, "mac": mac, "old_mac": nb_mac,
                       "source": source, "before": before}
            if why is None and not reserved:
                why = "not a dhcp reservation"
            if force.get("ip") == ip:
                if why is None and (_norm_mac(force.get("old_mac")), _norm_mac(
                        force.get("new_mac"))) == (nb_mac, mac):
                    replace_rows.append(row_out)
                    continue
                why = why or "macs changed since the last pass; refresh"
            elif why is None and not auto:
                why = "automatic replacement is off"
            elif why is None:
                w = watch.get(ip) or {}
                same = (_norm_mac(w.get("old_mac")), _norm_mac(w.get("new_mac"))) == (nb_mac, mac)
                first = (_age(w.get("first_seen_age")) or 0.0) if same else 0.0
                unseen = _age(w.get("old_unseen_age")) if same else None
                if first >= REPLACE_NEW_MIN_AGE and unseen is not None \
                        and unseen >= REPLACE_OLD_UNSEEN:
                    replace_rows.append(row_out)
                    continue
                pending.append({"ip": ip, "old_mac": nb_mac, "new_mac": mac,
                                "first_seen_age": first})
                why = "waiting before replacing"
            conflicts.append({"ip": ip, "mac": mac, "old_mac": nb_mac,
                              "reason": "reserved for another mac" if reserved
                              else "netbox mac differs", "replace": why})
            continue
        if cf.get("dhcp_reservation"):
            f_old = _norm_mac(force.get("old_mac"))
            if nb_mac == mac and force.get("ip") == ip and f_old and f_old != mac \
                    and _norm_mac(force.get("new_mac")) == mac:
                # A repeated button request: done only if the replacement
                # would still pass every check (Kea may already hold the
                # new MAC here, via the sync).
                block = _new_mac_block(ip, addr, mac, m["sources"], f_old, repeat=True)
                why = block[1] if block else _old_mac_block(ip, f_old)
                skipped.append({"ip": ip, "reason": why or "already replaced"})
            elif nb_mac == mac:
                skipped.append({"ip": ip, "reason": "already reserved"})
            else:
                conflicts.append({"ip": ip, "mac": mac, "reason": "reserved for another mac"})
            continue
        # Steps 6-7: Kea reservations, leases and pools (see _new_mac_block).
        block = _new_mac_block(ip, addr, mac, m["sources"], "")
        if block is not None:
            (conflicts if block[0] == "conflicts" else skipped).append(
                {"ip": ip, "mac": mac, "reason": block[1]} if block[0] == "conflicts"
                else {"ip": ip, "reason": block[1]})
            continue
        reserve.append({"ip_id": row.get("id"), "ip": ip, "mac": mac,
                        "source": source, "before": before})
    return {"reserve": reserve, "replace": replace_rows, "pending_replace": pending,
            "conflicts": conflicts, "skipped": skipped}


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


# Where each kind of object holding an interface MAC lives in pynetbox.
_MAC_HOLDERS = {"dcim.interface": ("dcim", "interfaces"),
                "virtualization.vminterface": ("virtualization", "interfaces"),
                "dcim.macaddress": ("dcim", "mac_addresses")}


def _holder_endpoint(engine, kind: str):
    """The pynetbox endpoint for ``kind`` (see ``_MAC_HOLDERS``), or None."""
    app, name = _MAC_HOLDERS.get(kind) or (None, None)
    return getattr(getattr(engine.nb, app), name) if app else None


def interface_mac_holder(engine, ip_id: Any, old_mac: str) -> Optional[Dict[str, Any]]:
    """The object that holds the MAC of the interface IP ``ip_id`` is
    assigned to, when that MAC is ``old_mac``: ``{object, id}``, else None.

    NetBox 4.2+ keeps MACs as ``dcim.mac-addresses`` records and shows the
    interface's ``primary_mac_address``; the record itself is edited, since
    the interface's ``mac_address`` is read-only there (see
    ``SyncMixin._set_iface_mac``). Older NetBox has no
    ``primary_mac_address`` and keeps a writable ``mac_address`` on the
    interface. An interface with another MAC, or none, is left alone.
    Raises on a NetBox error.
    """
    ip_obj = engine.nb.ipam.ip_addresses.get(int(ip_id))
    kind = str(getattr(ip_obj, "assigned_object_type", "") or "")
    oid = getattr(ip_obj, "assigned_object_id", None)
    if kind not in ("dcim.interface", "virtualization.vminterface") or oid in (None, ""):
        return None
    iface = _holder_endpoint(engine, kind).get(int(oid))
    if not iface:
        return None
    primary = getattr(iface, "primary_mac_address", None)
    if primary:
        pid = primary.get("id") if isinstance(primary, dict) else getattr(primary, "id", None)
        pmac = (primary.get("mac_address") if isinstance(primary, dict)
                else getattr(primary, "mac_address", None))
        if pid is not None and _norm_mac(pmac) == old_mac:
            return {"object": "dcim.macaddress", "id": pid}
        return None
    if _norm_mac(getattr(iface, "mac_address", None)) == old_mac:
        return {"object": kind, "id": int(oid)}
    return None


def mac_compare_and_set(engine, holder: Dict[str, Any], expect: str,
                        new: str) -> Tuple[str, str]:
    """Set ``mac_address`` on ``holder`` (``{object, id}``) to ``new`` only
    if it still is ``expect``. Outcomes as :func:`compare_and_set`."""
    try:
        ep = _holder_endpoint(engine, str(holder.get("object") or ""))
        obj = ep.get(int(holder["id"])) if ep is not None else None
        if not obj:
            return "gone", "interface mac deleted since it was read"
        current = _norm_mac(getattr(obj, "mac_address", None))
        if current == _norm_mac(new):
            return "already", ""
        if current != _norm_mac(expect):
            return "changed", "interface mac changed in netbox since it was read"
        obj.mac_address = new
        obj.save()
        return "written", ""
    except Exception as e:  # noqa: BLE001 — reported, never raised
        return "failed", str(e) or type(e).__name__


def _undo(engine, c: Dict[str, Any]) -> Tuple[str, str]:
    """Undo one change: its interface MAC (a replacement) first, then the
    IP. The worst outcome wins: ``failed``, then ``kept``, else ``done``."""
    outcomes = []
    iface = c.get("interface")
    if isinstance(iface, dict) and "after" in iface:
        outcomes.append(mac_compare_and_set(
            engine, iface, (iface.get("after") or {}).get("mac_address"),
            (iface.get("before") or {}).get("mac_address")))
    outcomes.append(compare_and_set(engine, c["ip_id"], c.get("after") or {},
                                    c.get("before") or {}))
    for worst in (("failed",), ("changed", "gone")):
        hit = next((d for o, d in outcomes if o in worst), None)
        if hit is not None:
            if len(outcomes) == 2:
                # Say what happened to each object, not only the worst.
                hit = "; ".join(f"{name}: {d or 'restored'}"
                                for name, (_o, d) in zip(("interface", "ip"), outcomes))
            return ("failed" if worst == ("failed",) else "kept"), hit
    return "done", ""


def roll_back(engine, changes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Undo ``changes`` (newest first): restore ``before`` where NetBox still
    holds ``after``. A replacement also restores its interface MAC
    (``interface``: ``{object, id, before, after}``) the same way.

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
        outcome, detail = _undo(engine, c)
        if outcome == "done":
            done.append(c)
        elif outcome == "kept":
            kept.append({**c, "reason": detail})
        else:
            failed.append({**c, "error": detail})
    # Log and report.
    if done or kept or failed:
        logger.info("static reservations: rolled back %d, left %d, could not roll back %d",
                    len(done), len(kept), len(failed))
    return {"status": "ERROR" if failed else "SUCCESS", "rolled_back": done,
            "rollback_kept": kept, "rollback_failed": failed}


def _write_replacement(engine, r: Dict[str, Any]) -> Tuple[str, str, Optional[Dict]]:
    """Move the IP's reservation from ``old_mac`` to ``mac``, then the MAC
    of the interface the IP is assigned to when it is ``old_mac``.

    Returns ``(outcome, detail, interface)``. ``interface`` is the interface
    write ``{object, id, before, after}`` (also when that write failed, so
    the rollback covers it), or ``{object, id, left: reason}``
    when the interface already held the new MAC or changed in between, or
    None when no interface held the old MAC. An outcome of ``failed`` with
    the IP already written comes back as ``partial``, so the caller rolls
    the IP write back too.
    """
    after = {"mac_address": r["mac"], "dhcp_reservation": True}
    outcome, detail = compare_and_set(engine, r["ip_id"], r["before"], after)
    if outcome != "written":
        return outcome, detail, None
    try:
        holder = interface_mac_holder(engine, r["ip_id"], r["old_mac"])
    except Exception as e:  # noqa: BLE001 — reported, never raised
        return "partial", str(e) or type(e).__name__, None
    if holder is None:
        return "written", "", None
    iface = {**holder, "before": {"mac_address": r["old_mac"]},
             "after": {"mac_address": r["mac"]}}
    io, idetail = mac_compare_and_set(engine, holder, r["old_mac"], r["mac"])
    if io == "failed":
        # NetBox may have taken the write before failing: the rollback
        # compare-and-sets it back too (a no-op if it still holds old_mac).
        return "partial", idetail, iface
    if io == "written":
        return "written", "", iface
    # Already moved, or changed or gone in between: the IP still moves, the
    # interface is left alone and the row says why (it holds no undo).
    return "written", "", {**holder, "left": idetail or "interface already has the new mac"}


def reserve_static_hosts(engine, data: Dict[str, Any], failed_writes: Dict[str, float],
                         now: float) -> Dict[str, Any]:
    """One pass over the tenants the hub sent (blocking; run in a thread).

    ``data``: ``{tenants: [{ref, filters, agent_addresses, sweep, kea,
    replace_watch}], kea: {key: {leases, subnets}}, max_writes,
    auto_replace}``. ``ref`` is an opaque label the hub maps back to its
    tenant; ``kea`` names the tenant's entry in the shared ``kea`` map, so a
    Kea serving several tenants is sent once. ``replace_watch`` is the hub's
    view of each pending device replacement (``{ip, old_mac, new_mac,
    first_seen_age, old_unseen_age}``); ``auto_replace`` turns automatic
    replacement on (off when absent). ``force`` (``{ip, old_mac,
    new_mac}``) is set only by :func:`replace_reservation_mac`.

    ``failed_writes`` (``{"<filters>|<ip>": when}``) carries the write
    backoff between passes.

    Returns ``{status, evaluated, changes, replaced, pending_replace,
    conflicts, errors, skipped, deferred}``; ``status`` is ERROR whenever
    ``errors`` is not empty. An IP another writer changed (or deleted) after
    the read is a conflict and the rest of the pass goes on. When a write
    fails, the pass stops, ``changes`` and ``replaced`` are empty and the
    earlier writes are in ``rolled_back``, ``rollback_kept`` or
    ``rollback_failed`` (see :func:`roll_back`).
    """
    report: Dict[str, Any] = {"status": "SUCCESS", "evaluated": [], "changes": [],
                              "replaced": [], "pending_replace": [],
                              "conflicts": [], "errors": [], "skipped": 0, "deferred": 0}
    max_writes = int(data.get("max_writes") or DEFAULT_MAX_WRITES)
    kea = data.get("kea") or {}
    force = data.get("force") or None
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
        watch = {str(w.get("ip")): w for w in t.get("replace_watch") or []
                 if isinstance(w, dict)}
        plan = plan_static_reservations(
            pfx.get("prefixes"), ips.get("ip_addresses"), state["leases"], candidates(t),
            state["subnets"], {"auto": bool(data.get("auto_replace")) and not force,
                               "watch": watch, "force": force})
        report["evaluated"].append(ref)
        fkey = "|".join(f"{k}={filters[k]}" for k in sorted(filters))
        if force:
            # The button: only the one replacement; say why when there is none.
            planned.extend((ref, f"{fkey}|{r['ip']}", "replace", r) for r in plan["replace"])
            if not plan["replace"]:
                report["message"] = _why_not(plan, str(force.get("ip") or ""))
                # NetBox already reserves this IP for the one MAC the
                # sources report and the replacement's checks still pass:
                # a repeat of the button.
                report["_force_already"] = any(
                    s.get("ip") == force.get("ip") and s.get("reason") == "already replaced"
                    for s in plan["skipped"])
            continue
        report["skipped"] += len(plan["skipped"])
        report["conflicts"].extend({"ref": ref, **c} for c in plan["conflicts"])
        report["pending_replace"].extend({"ref": ref, **p} for p in plan["pending_replace"])
        rows = [("reserve", r) for r in plan["reserve"]] + [("replace", r) for r in plan["replace"]]
        rows.sort(key=lambda kr: ipaddress.ip_address(kr[1]["ip"]))
        planned.extend((ref, f"{fkey}|{r['ip']}", kind, r) for kind, r in rows)

    # Write, capped; a failure rolls back this pass's earlier writes.
    writes: List[Dict[str, Any]] = []
    used = 0
    for ref, key, kind, r in planned:
        need = REPLACE_WRITES if kind == "replace" else 1
        if used + need > max_writes:
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
        if kind == "replace":
            outcome, detail, iface = _write_replacement(engine, r)
        else:
            (outcome, detail), iface = compare_and_set(engine, r["ip_id"], r["before"], after), None
        if outcome in ("changed", "gone"):
            # Another writer got there first: hold this IP back, keep the rest.
            report["conflicts"].append({"ref": ref, "ip": r["ip"], "mac": r["mac"],
                                        "reason": detail})
            continue
        if outcome in ("failed", "partial"):
            failed_writes[key] = now
            report["errors"].append({"ref": ref, "ip": r["ip"], "error": detail})
            if outcome == "partial":
                writes.append({"ref": ref, "ip": r["ip"], "ip_id": r["ip_id"],
                               "before": r["before"], "after": after, "interface": iface})
            undo = roll_back(engine, writes)
            report.update(changes=[], replaced=[], rolled_back=undo["rolled_back"],
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
            if force and kind == "replace":
                # Another run moved the IP between the read and the write
                # (after this run's checks passed): finished like a repeat.
                report["_force_already"] = True
            continue
        used += need
        if kind == "replace":
            row = {"ref": ref, "ip": r["ip"], "ip_id": r["ip_id"], "old_mac": r["old_mac"],
                   "new_mac": r["mac"], "source": r["source"], "before": r["before"],
                   "after": after, "interface": iface}
            report["replaced"].append(row)
            logger.info("static reservations: %s moved to a new mac (%s)%s", r["ip"],
                        r["source"], " with its interface" if iface else "")
        else:
            row = {"ref": ref, "ip": r["ip"], "ip_id": r["ip_id"], "mac": r["mac"],
                   "source": r["source"], "before": r["before"], "after": after}
            report["changes"].append(row)
            logger.info("static reservations: reserved %s (%s)", r["ip"], r["source"])
        writes.append(row)
    # ERROR whenever anything went wrong, so "every read failed" never reads
    # as an empty success; the rows say what.
    if report["errors"]:
        report["status"] = "ERROR"
        report.setdefault("message", f"{len(report['errors'])} error(s); see errors")
    return report


def _why_not(plan: Dict[str, List], ip: str) -> str:
    """Why the button's replacement for ``ip`` was not planned."""
    for c in plan["conflicts"]:
        if c.get("ip") == ip:
            return f"cannot replace: {c.get('replace') or c.get('reason')}"
    for s in plan["skipped"]:
        if s.get("ip") == ip:
            return f"cannot replace: {s.get('reason')}"
    return "cannot replace: the new mac is not seen at this address now"


def _already_moved(engine, data: Dict[str, Any], ip: str, new: str) -> Any:
    """The NetBox id of ``ip`` when NetBox already reserves it for ``new``
    in the request's tenant scope (the button's move was done before), else
    None."""
    t = (data.get("tenants") or [{}])[0] or {}
    filters = {k: v for k, v in (t.get("filters") or {}).items()
               if k in ("tenant", "tenant_group") and v}
    if not filters:
        return None
    ips = engine.get_ip_addresses(**filters)
    if not isinstance(ips, dict) or ips.get("status") != "SUCCESS":
        return None
    rows = [r for r in ips.get("ip_addresses") or [] if _host(r.get("address")) == ip]
    cf = (rows[0].get("custom_fields") or {}) if len(rows) == 1 else {}
    if cf.get("dhcp_reservation") and _norm_mac(cf.get("mac_address")) == new:
        return rows[0].get("id")
    return None


def _finish_moved(engine, res: Dict[str, Any], ref: str, ip: str, ip_id: Any, old: str,
                  new: str) -> Dict[str, Any]:
    """A repeated button request: the IP already has ``new``. Finish its
    interface if an earlier run stopped between the two writes, and report
    the move as a ``replaced`` row like a fresh one, so the hub can still
    roll it back (its interface is the one that holds ``new`` now)."""
    left = None
    try:
        holder = interface_mac_holder(engine, ip_id, old)
        if holder is None:
            # Nothing to move. An interface already on the new MAC may not
            # have been changed by this feature, so it is never undone.
            holder = interface_mac_holder(engine, ip_id, new)
            if holder is not None:
                left = {**holder, "left": "interface already has the new mac"}
        else:
            # Move it; after the write the known holder is reported, not re-read.
            io, detail = mac_compare_and_set(engine, holder, old, new)
            if io in ("changed", "gone"):
                # Changed or deleted in between: left alone, as in a fresh run.
                left = {**holder, "left": detail}
            elif io == "already":
                # Another writer moved it in between: not this run's to undo.
                left = {**holder, "left": "interface already has the new mac"}
            elif io == "failed":
                # NetBox may have saved before failing: put it back (a
                # no-op when it still holds old).
                back, bdetail = mac_compare_and_set(engine, holder, new, old)
                out = dict(res, status="ERROR", message=(
                    f"{ip} already has the new mac but its interface could not be "
                    f"updated: {detail}"))
                if back not in ("written", "already"):
                    # Changed or deleted since: kept, like any rollback;
                    # refused: failed.
                    key = "rollback_failed" if back == "failed" else "rollback_kept"
                    out[key] = list(res.get(key) or []) + [{
                        "ref": ref, "ip": ip, "interface": {
                            **holder, "before": {"mac_address": old},
                            "after": {"mac_address": new}},
                        ("error" if back == "failed" else "reason"): bdetail}]
                return out
    except Exception as e:  # noqa: BLE001 — reported, never raised
        return dict(res, status="ERROR", message=(
            f"{ip} already has the new mac but its interface could not be read: "
            f"{str(e) or type(e).__name__}"))
    iface = left or ({**holder, "before": {"mac_address": old}, "after": {"mac_address": new}}
                     if holder is not None else None)
    row = {"ref": ref, "ip": ip, "ip_id": ip_id, "old_mac": old, "new_mac": new,
           "source": "button", "before": {"mac_address": old, "dhcp_reservation": True},
           "after": {"mac_address": new, "dhcp_reservation": True}, "interface": iface}
    return dict(res, status="SUCCESS", already=True, replaced=[row],
                message=f"{ip} is already reserved for the new mac")


def replace_reservation_mac(engine, data: Dict[str, Any], failed_writes: Dict[str, float],
                            now: float) -> Dict[str, Any]:
    """``NETBOX_REPLACE_RESERVATION_MAC``: the "Use new MAC" button.

    ``data`` is one tenant in the pass format (``tenants``, ``kea``) plus
    ``ip``, ``old_mac`` and ``new_mac``. Moves that IP's reservation (and
    its interface MAC) from ``old_mac`` to ``new_mac`` now, without the
    automatic waits but with every other check and the same compare-and-set
    and rollback. Writes nothing else. Returns the pass report; ``status``
    is ERROR (with a ``message``) unless ``replaced`` has the one row, or
    this run wrote and failed nothing, the sources pass every check and
    report only ``new_mac``, and NetBox already reserves the IP for it
    (``already: true``, so a repeated request is harmless; an
    interface still on ``old_mac`` is moved then). ``replaced`` then holds
    the move as a fresh run would report it, for the hub's rollback.
    """
    ip = _host(data.get("ip"))
    old, new = _mac(data.get("old_mac")), _mac(data.get("new_mac"))
    if not ip or not old or not new or old == new or len(data.get("tenants") or []) != 1:
        return {"status": "ERROR", "message": "need one tenant, an ip and two different macs",
                "replaced": [], "errors": []}
    res = reserve_static_hosts(
        engine, {"tenants": data.get("tenants"), "kea": data.get("kea"),
                 "max_writes": REPLACE_WRITES,
                 "force": {"ip": ip, "old_mac": old, "new_mac": new}},
        failed_writes, now)
    clean = res.pop("_force_already", False) and not any(
        res.get(k) for k in ("replaced", "changes", "errors", "rolled_back",
                             "rollback_kept", "rollback_failed"))
    ip_id = _already_moved(engine, data, ip, new) if clean else None
    if ip_id is not None:
        # A repeated request after a success (say its reply was lost).
        ref = str(((data.get("tenants") or [{}])[0] or {}).get("ref") or "")
        return _finish_moved(engine, res, ref, ip, ip_id, old, new)
    if not res["replaced"]:
        res["status"] = "ERROR"
        lost = next((c.get("reason") for c in res["conflicts"] if c.get("ip") == ip), None)
        res.setdefault("message", f"cannot replace: {lost}" if lost else "cannot replace")
    return res

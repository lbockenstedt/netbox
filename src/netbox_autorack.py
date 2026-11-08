"""Auto-rack: place devices in NetBox racks from their names.

Name grammar (domain suffix ignored)::

    <SITE3><NET>-<TENANTGROUP>-<RACK>-R<RU>[R]-<DEVICE>
    MIPBE-SSPLM-N31-R45-VPNC1   -> rack N31, RU 45, front
    MIPBE-SSPLM-N31-R45R-VPNC1  -> rack N31, RU 45, rear

``plan()`` is pure: it takes normalised device/rack records and returns the
ordered actions (unrack / place / move) plus the devices that could not be
placed and why. A slot held by another device is resolved before it is
reported: a holder that is moving elsewhere moves out first, an offline holder
is unracked (replaced), and only an online holder still named for the slot is a
real conflict. Chains are capped so swaps and cycles are reported, not looped.
"""

import logging
import re
import time as _time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("netbox.autorack")

_NAME_RE = re.compile(
    r"^(?P<site>[A-Za-z0-9]{3})(?P<net>[A-Za-z]{0,3})-(?P<tenant>[A-Za-z0-9]+)-"
    r"(?P<rack>[A-Za-z]{1,3}\d{1,3})-R(?P<ru>\d{1,3})(?P<rear>R?)-(?P<device>.+)$",
    re.IGNORECASE)

ONLINE_STATUSES = {"active", "staged"}
MAX_DEPTH = 6


def parse_rack_name(name: str) -> Optional[Dict[str, Any]]:
    """Rack, RU and face a device name asks for; None when it carries none."""
    short = str(name or "").strip().split(".", 1)[0]
    m = _NAME_RE.match(short)
    if not m:
        return None
    ru = int(m.group("ru"))
    if ru < 1:
        return None
    return {"site": m.group("site").upper(), "net": m.group("net").upper(),
            "tenant": m.group("tenant").upper(), "rack": m.group("rack").upper(),
            "ru": ru, "face": "rear" if m.group("rear") else "front",
            "device": m.group("device")}


_HALF_WIDTH_RE = re.compile(r"half[- ]?width|\b9004\b", re.I)


def is_half_width(model: Any, comments: Any = "") -> bool:
    """A chassis that shares a U with a second one (e.g. Aruba CX 9004): the
    device type is marked '[half-width]' in its comments (the LLM spec writes
    it) or is a known half-width model."""
    return bool(_HALF_WIDTH_RE.search(f"{model or ''} {comments or ''}"))


def _cells(rack_id, pos, face, height, full_depth) -> List[Tuple[Any, int, str]]:
    faces = ("front", "rear") if full_depth else (face,)
    return [(rack_id, u, f) for u in range(int(pos), int(pos) + max(int(height), 1)) for f in faces]


def resolve_rack(parsed: Dict[str, Any], racks: List[Dict[str, Any]],
                 site_id: Any = None, tenant_id: Any = None) -> Tuple[Optional[Dict], str]:
    """The NetBox rack a parsed name points at, or (None, reason)."""
    cands = [r for r in racks if str(r.get("name") or "").upper() == parsed["rack"]]
    if site_id is not None:
        same = [r for r in cands if r.get("site_id") == site_id]
        # A device still in a generic site is moved to its rack's site on placement.
        cands = same or cands
    if len(cands) > 1 and tenant_id is not None:
        cands = [r for r in cands if r.get("tenant_id") == tenant_id] or cands
    if not cands:
        return None, f"rack {parsed['rack']} not found in NetBox"
    if len(cands) > 1:
        return None, f"rack name {parsed['rack']} is ambiguous ({len(cands)} racks)"
    return cands[0], ""


def plan(devices: List[Dict[str, Any]], racks: List[Dict[str, Any]],
         probe: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
         max_depth: int = MAX_DEPTH) -> Dict[str, Any]:
    """Actions that bring every named device to its slot.

    device: {id, name, site_id, tenant_id, rack_id, position, face, online,
             height, full_depth}; rack: {id, name, site_id, tenant_id, u_height}.
    ``probe(device)`` may return a fresher {name, online} for a holder (the
    hub's re-poll); without it NetBox's own record is used.
    """
    racks_by_id = {r["id"]: r for r in racks}
    by_id = {d["id"]: d for d in devices}
    cur: Dict[Any, Optional[Tuple[Any, int, str]]] = {}
    occ: Dict[Tuple[Any, int, str], Any] = {}
    for d in devices:
        if d.get("rack_id") and d.get("position") is not None:
            cur[d["id"]] = (d["rack_id"], int(d["position"]), d.get("face") or "front")
            for c in _cells(*cur[d["id"]], d.get("height") or 1, d.get("full_depth", True)):
                occ[c] = d["id"]
        else:
            cur[d["id"]] = None

    want: Dict[Any, Optional[Tuple[Any, int, str]]] = {}
    refused: Dict[Any, str] = {}
    for d in devices:
        parsed = parse_rack_name(d.get("name"))
        want[d["id"]] = None
        if not parsed:
            continue
        rack, why = resolve_rack(parsed, racks, d.get("site_id"), d.get("tenant_id"))
        h = int(d.get("height") or 0)
        if rack is None:
            refused[d["id"]] = why
        elif h < 1:
            refused[d["id"]] = "device type has no rack height (0U or unknown)"
        elif parsed["ru"] + h - 1 > int(rack.get("u_height") or 0):
            refused[d["id"]] = (f"U{parsed['ru']} + {h}U does not fit rack "
                                f"{rack['name']} ({rack.get('u_height')}U)")
        else:
            want[d["id"]] = (rack["id"], parsed["ru"], parsed["face"])

    actions: List[Dict[str, Any]] = []
    report: List[Dict[str, Any]] = []
    state: Dict[Any, str] = {}  # id -> ok | failed
    probed: Dict[Any, Dict[str, Any]] = {}

    def fresh(h):
        if h["id"] not in probed:
            got = None
            if probe:
                try:
                    got = probe(h)
                except Exception as e:
                    logger.debug("autorack probe %s failed: %s", h.get("name"), e)
            probed[h["id"]] = got or {}
        return probed[h["id"]]

    def online(h):
        got = fresh(h)
        return bool(got["online"]) if "online" in got else bool(h.get("online"))

    def free(d_id):
        pos = cur.get(d_id)
        if pos:
            d = by_id[d_id]
            for c in _cells(*pos, d.get("height") or 1, d.get("full_depth", True)):
                if occ.get(c) == d_id:
                    del occ[c]
        cur[d_id] = None

    def unrack(h, reason):
        actions.append({"op": "unrack", "id": h["id"], "name": h["name"], "reason": reason,
                        "from": cur[h["id"]]})
        free(h["id"])

    def fail(d, kind, reason, holder=None):
        state[d["id"]] = "failed"
        rec = {"id": d["id"], "name": d["name"], "kind": kind, "reason": reason,
               "current": cur.get(d["id"])}
        if holder:
            rec["holder"] = holder["name"]
        report.append(rec)

    def settle(d, depth, stack) -> bool:
        did = d["id"]
        if state.get(did):
            return state[did] == "ok"
        w = want[did]
        half = bool(d.get("half_width"))
        if cur[did] == w or (half and cur[did] and w and cur[did][:2] == w[:2]):
            state[did] = "ok"
            return True
        if depth > max_depth or did in stack:
            fail(d, "cannot", "slot chain loops or is too deep (swap/cycle)")
            return False
        stack = stack | {did}
        h_ = d.get("height") or 1
        fd = d.get("full_depth", True)
        if half:
            # Two half-width chassis share a U; NetBox can only hold that as one
            # per face, so the second takes the other face of the same U.
            fd = False
            other = "rear" if w[2] == "front" else "front"
            mine = [occ.get(c) for c in _cells(*w, h_, False)]
            holder = next((by_id[x] for x in mine if x is not None and x != did), None)
            if holder is not None and holder.get("half_width"):
                alt = [occ.get(c) for c in _cells(w[0], w[1], other, h_, False)]
                if all(x is None or x == did for x in alt):
                    w = (w[0], w[1], other)
        for _ in range(max_depth + 1):
            holders = []
            for c in _cells(*w, h_, fd):
                hid = occ.get(c)
                if hid is not None and hid != did and by_id[hid] not in holders:
                    holders.append(by_id[hid])
            if not holders:
                break
            h = holders[0]
            newname = fresh(h).get("name")
            if newname and newname != h.get("name"):
                h = dict(h, name=newname)
                by_id[h["id"]] = h
                p = parse_rack_name(newname)
                r, _ = resolve_rack(p, racks, h.get("site_id"), h.get("tenant_id")) if p else (None, "")
                want[h["id"]] = (r["id"], p["ru"], p["face"]) if r else None
            hw = want.get(h["id"])
            if hw and hw != cur[h["id"]] and settle(h, depth + 1, stack):
                continue  # holder moved out; re-scan the slot
            if not online(h):
                unrack(h, f"offline; replaced by {d['name']}")
                continue
            if hw and hw == cur[h["id"]]:
                fail(d, "conflict", f"slot is held by online {h['name']}, also named for it", h)
                if state.get(h["id"]) != "failed":
                    fail(h, "conflict", f"slot is also claimed by online {d['name']}", d)
            elif hw:
                fail(d, "cannot", f"held by {h['name']}, which is moving but is blocked", h)
            else:
                fail(d, "cannot", f"slot is held by online {h['name']}, not named for it", h)
            return False
        else:
            fail(d, "cannot", "could not clear the slot")
            return False
        old = cur[did]
        free(did)
        for c in _cells(*w, h_, fd):
            occ[c] = did
        cur[did] = w
        actions.append({"op": "move" if old else "place", "id": did, "name": d["name"],
                        "from": old, "to": w})
        state[did] = "ok"
        return True

    for d in sorted(devices, key=lambda x: str(x["name"])):
        if d["id"] in refused:
            fail(d, "cannot", refused[d["id"]])
        elif want[d["id"]] is not None:
            settle(d, 0, frozenset())
    return {"actions": actions, "report": report,
            "settled": sum(1 for v in state.values() if v == "ok")}


def _val(x, key="value"):
    return str(x.get(key) if isinstance(x, dict) else x or "").lower()


class AutoRackMixin:
    """NetboxEngine side of auto-rack: read NetBox, ``plan()``, apply."""

    def autorack_sweep(self, tenant_slug: str = "", dry_run: bool = False,
                       online: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        """Reconcile every named device's rack/RU/face with its name.

        The name wins: a device whose NetBox slot differs is re-racked through
        the same fit/free/conflict checks, and when it cannot be it stays put
        and is reported (never silently unracked). ``online`` (name -> bool,
        from the hub's fleet state) overrides NetBox status for liveness.
        Returns {status, placed, moved, unracked, noop, conflicts, cannot}."""
        try:
            params: Dict[str, Any] = {}
            if tenant_slug:
                tenant = self._resolve_tenant_ci(tenant_slug)
                if tenant:
                    params["tenant_id"] = tenant.id
            rows = self._api_get_all("/api/dcim/devices/", params)
            rack_rows = self._api_get_all("/api/dcim/racks/")
            type_rows = self._api_get_all("/api/dcim/device-types/")
        except Exception as e:
            return {"status": "ERROR", "message": f"autorack read failed: {e}"}
        types = {t["id"]: t for t in type_rows}
        racks = [{"id": r["id"], "name": r.get("name"), "u_height": r.get("u_height"),
                  "site_id": (r.get("site") or {}).get("id"),
                  "tenant_id": (r.get("tenant") or {}).get("id")} for r in rack_rows]
        online = {str(k).lower(): bool(v) for k, v in (online or {}).items()}
        devices = []
        for r in rows:
            dt = types.get((r.get("device_type") or {}).get("id"), {})
            nm = str(r.get("name") or "")
            up = online.get(nm.lower().split(".", 1)[0])
            half = is_half_width(dt.get("model"), dt.get("comments"))
            devices.append({
                "id": r["id"], "name": nm,
                "tenant": (r.get("tenant") or {}).get("slug") or "",
                "site_id": (r.get("site") or {}).get("id"),
                "tenant_id": (r.get("tenant") or {}).get("id"),
                "rack_id": (r.get("rack") or {}).get("id"),
                "position": r.get("position"), "face": _val(r.get("face")) or None,
                "online": up if up is not None else _val(r.get("status")) in ONLINE_STATUSES,
                "height": int(float(dt.get("u_height") or 0)),
                "full_depth": False if half else bool(dt.get("is_full_depth", True)),
                "half_width": half, "type_id": dt.get("id"),
                "type_full": bool(dt.get("is_full_depth", True))})
        result = plan(devices, racks)
        dev_by_id = {d["id"]: d for d in devices}
        rack_site = {r["id"]: r["site_id"] for r in racks}
        counts = {"placed": 0, "moved": 0, "unracked": 0}
        failed = []
        for a in result["actions"]:
            if dry_run:
                counts[{"place": "placed", "move": "moved", "unrack": "unracked"}[a["op"]]] += 1
                continue
            try:
                obj = self.nb.dcim.devices.get(a["id"])
                if a["op"] == "unrack":
                    obj.rack = None
                    obj.position = None
                    obj.face = None
                else:
                    rack_id, pos, face = a["to"]
                    dv = dev_by_id.get(a["id"]) or {}
                    if dv.get("half_width") and dv.get("type_full") and dv.get("type_id"):
                        # NetBox only lets two chassis share a U when neither is full depth.
                        dtobj = self.nb.dcim.device_types.get(dv["type_id"])
                        dtobj.is_full_depth = False
                        dtobj.save()
                        for x in devices:
                            if x.get("type_id") == dv["type_id"]:
                                x["type_full"] = False
                    obj.site = rack_site[rack_id]
                    obj.rack = rack_id
                    obj.position = pos
                    obj.face = face
                obj.save()
                counts[{"place": "placed", "move": "moved", "unrack": "unracked"}[a["op"]]] += 1
                self._journal("dcim.device", a["id"], "autorack",
                              note=f"{a['op']}: {a.get('reason') or a.get('to')}")
            except Exception as e:
                logger.warning("autorack %s %s failed: %s", a["op"], a["name"], e)
                failed.append({"id": a["id"], "name": a["name"], "kind": "cannot",
                               "reason": f"NetBox refused: {e}"})
        report = result["report"] + failed
        rack_name = {r["id"]: r["name"] for r in racks}
        by_id = {d["id"]: d for d in devices}

        def label(slot):
            if not slot:
                return "unracked"
            return f"{rack_name.get(slot[0], slot[0])} U{slot[1]} {slot[2]}"

        for x in report:
            d = by_id.get(x["id"], {})
            p = parse_rack_name(x["name"])
            cur = x.get("current")
            x["tenant"] = d.get("tenant", "")
            x["wanted"] = (f"{p['rack']} U{p['ru']} {p['face']}" if p else "")
            x["current"] = label(cur)
            x["mismatch"] = bool(cur and p and x["current"] != x["wanted"])
        out = {"status": "SUCCESS", "dry_run": dry_run, "ts": int(_time.time()),
               "racked": sum(1 for d in devices if d["rack_id"]),
               "named": sum(1 for d in devices if parse_rack_name(d["name"])),
               **counts,
               "noop": result["settled"] - counts["placed"] - counts["moved"],
               "conflicts": [x for x in report if x["kind"] == "conflict"],
               "cannot": [x for x in report if x["kind"] != "conflict"],
               "actions": result["actions"] if dry_run else []}
        logger.info("autorack: placed=%d moved=%d unracked=%d conflicts=%d cannot=%d",
                    counts["placed"], counts["moved"], counts["unracked"],
                    len(out["conflicts"]), len(out["cannot"]))
        self._autorack_last = out
        return out

"""Device de-duplication (merge) sweep for NetboxEngine.

Before the sync sinks were unified onto one reconciliation ladder
(serial → mac → nw_device_id → ip → hostname), the same physical box discovered
by different modules (console→serial, switch-poll→nw_device_id, ClearPass→mac)
could land as SEPARATE NetBox devices. Unifying the ladder stops NEW duplicates,
but pre-existing ones remain. This sweep finds and (optionally) merges them.

Two devices are the SAME hardware when they share a strong hardware identity —
a serial or a MAC. Grouping is transitive (union-find): A≡B by serial and B≡C by
MAC ⇒ {A,B,C} is one box. Each group elects a SURVIVOR (the most complete
record) and the rest are merged into it: the survivor backfills any identity /
custom-field GAP from a duplicate (never clobbering data it already has), each
duplicate's IP addresses are re-pointed to the survivor (best-effort), and the
duplicate is deleted.

Detection is the default (``apply=False``) — a dry run reports the groups it
WOULD merge so an operator can review before anything is changed.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NetboxEngine")


class DedupeMixin:
    """Find + merge duplicate NetBox devices that share a serial or MAC."""

    @staticmethod
    def _dsu_find(parent: Dict[Any, Any], x: Any) -> Any:
        """Union-find root with path compression."""
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def _dsu_union(self, parent: Dict[Any, Any], a: Any, b: Any) -> None:
        ra, rb = self._dsu_find(parent, a), self._dsu_find(parent, b)
        if ra != rb:
            parent[rb] = ra

    @staticmethod
    def _completeness_score(row: dict) -> int:
        """Rank a device row for survivor election: the record carrying the most
        hardware truth wins. Serial (globally unique) is weighted highest, then
        MAC, then a real (non-generic) device_type, a primary IP and a name."""
        cf = row.get("custom_fields") or {}
        score = 0
        if str(row.get("serial") or "").strip():
            score += 8
        if str(cf.get("mac_address") or "").strip():
            score += 4
        if str(cf.get("nw_device_id") or "").strip():
            score += 2
        dt = row.get("device_type") or {}
        dt_slug = str((dt.get("slug") if isinstance(dt, dict) else "") or "").lower()
        if dt_slug and dt_slug != "discovered":
            score += 2
        if isinstance(row.get("primary_ip4"), dict):
            score += 1
        if str(row.get("name") or "").strip():
            score += 1
        return score

    def dedupe_devices(self, tenant_slug: str = "",
                       apply: bool = False) -> Dict[str, Any]:
        """Find (and, when ``apply``, merge) duplicate devices sharing a serial
        or MAC.

        Groups devices transitively by shared serial / MAC (union-find), elects
        the most-complete SURVIVOR per group, and — only when ``apply=True`` —
        merges the rest into it (gap-only identity/custom-field backfill,
        best-effort IP re-point, then delete). ``apply=False`` (default) is a
        DRY RUN: it reports the groups it would merge and changes nothing.

        ``tenant_slug`` scopes the scan to one tenant (recommended); empty scans
        all devices. Returns ``{status, scanned, groups, merged, deleted,
        errors, applied, message}`` where ``groups`` is a list of
        ``{key, survivor_id, survivor_name, duplicate_ids, merged}``. Never
        raises — a per-group failure is counted in ``errors`` so one bad merge
        can't abort the sweep.
        """
        scanned = 0
        merged = 0
        deleted = 0
        errors = 0
        first_err: Optional[str] = None
        groups_out: List[Dict[str, Any]] = []
        try:
            list_params: Dict[str, Any] = {"limit": 500}
            if tenant_slug:
                list_params["tenant"] = tenant_slug
            try:
                rows = self._api_get_all("/api/dcim/devices/", list_params)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "scanned": 0, "groups": [], "merged": 0, "deleted": 0,
                        "errors": 0, "applied": bool(apply)}
            by_id: Dict[Any, dict] = {}
            serial_map: Dict[str, List[Any]] = {}
            mac_map: Dict[str, List[Any]] = {}
            parent: Dict[Any, Any] = {}
            for row in (rows or []):
                did = row.get("id")
                if did is None:
                    continue
                scanned += 1
                by_id[did] = row
                parent[did] = did
                serial = str(row.get("serial") or "").strip().lower()
                if serial:
                    serial_map.setdefault(serial, []).append(did)
                cf = row.get("custom_fields") or {}
                mac = self._norm_mac(cf.get("mac_address") or "")
                if mac:
                    mac_map.setdefault(mac, []).append(did)

            # Union devices that share a serial, then a MAC (transitive).
            for ids in list(serial_map.values()) + list(mac_map.values()):
                for other in ids[1:]:
                    self._dsu_union(parent, ids[0], other)

            # Collect components of size > 1.
            comps: Dict[Any, List[Any]] = {}
            for did in by_id:
                comps.setdefault(self._dsu_find(parent, did), []).append(did)

            for comp_ids in comps.values():
                if len(comp_ids) < 2:
                    continue
                members = [by_id[i] for i in comp_ids]
                # Survivor = most complete; tie-break lowest (oldest) id.
                survivor = max(
                    members,
                    key=lambda r: (self._completeness_score(r), -int(r["id"])))
                dupes = [m for m in members if m["id"] != survivor["id"]]
                # Group key: the shared serial if any, else the shared MAC.
                key = ""
                scf = survivor.get("custom_fields") or {}
                if str(survivor.get("serial") or "").strip():
                    key = f"serial:{str(survivor.get('serial')).strip().lower()}"
                elif str(scf.get("mac_address") or "").strip():
                    key = f"mac:{self._norm_mac(scf.get('mac_address'))}"
                else:
                    for d in dupes:
                        s = str(d.get("serial") or "").strip()
                        if s:
                            key = f"serial:{s.lower()}"
                            break
                        dm = self._norm_mac((d.get("custom_fields") or {}).get("mac_address") or "")
                        if dm:
                            key = f"mac:{dm}"
                            break
                did_merged = 0
                if apply:
                    for dupe in dupes:
                        try:
                            if self._merge_device(survivor, dupe):
                                did_merged += 1
                                deleted += 1
                        except Exception as e:
                            errors += 1
                            if first_err is None:
                                first_err = f"merge {dupe.get('id')}→{survivor.get('id')}: {e}"
                            logger.debug("dedupe: merge %s into %s failed: %s",
                                         dupe.get("id"), survivor.get("id"), e)
                    merged += did_merged
                groups_out.append({
                    "key": key or "unknown",
                    "survivor_id": survivor["id"],
                    "survivor_name": survivor.get("name") or "",
                    "duplicate_ids": [d["id"] for d in dupes],
                    "merged": did_merged,
                })

            status = "SUCCESS" if errors == 0 else "PARTIAL"
            msg = (f"{len(groups_out)} duplicate group(s); "
                   f"{'merged ' + str(merged) + ' device(s)' if apply else 'dry run (no changes)'}")
            if first_err:
                msg += f"; first error: {first_err}"
            return {"status": status, "scanned": scanned, "groups": groups_out,
                    "merged": merged, "deleted": deleted, "errors": errors,
                    "applied": bool(apply), "message": msg}
        except Exception as e:
            logger.warning("dedupe_devices failed: %s", e)
            return {"status": "ERROR", "message": str(e), "scanned": scanned,
                    "groups": groups_out, "merged": merged, "deleted": deleted,
                    "errors": errors + 1, "applied": bool(apply)}

    def _merge_device(self, survivor: dict, dupe: dict) -> bool:
        """Merge ``dupe`` into ``survivor``: gap-only backfill of the survivor's
        identity + custom fields from the duplicate (NEVER clobbering data the
        survivor already has — network hardware is source of truth and the
        survivor is the most complete record), re-point the duplicate's IP
        addresses to the survivor (best-effort), then delete the duplicate.
        Returns True when the duplicate was deleted."""
        sobj = self.nb.dcim.devices.get(survivor["id"])
        dobj = self.nb.dcim.devices.get(dupe["id"])
        if not dobj:
            return False
        if sobj:
            changed = False
            # Native serial (fill-gap).
            if not str(getattr(sobj, "serial", "") or "").strip():
                dserial = str(getattr(dobj, "serial", "") or "").strip() \
                    or str(dupe.get("serial") or "").strip()
                if dserial:
                    sobj.serial = dserial
                    changed = True
            # Custom-field gaps (mac/nw id/switch topology/discovered_from).
            scf = dict(getattr(sobj, "custom_fields", {}) or {})
            dcf = dict(getattr(dobj, "custom_fields", {}) or {})
            for k in ("mac_address", "nw_device_id", "switch_ip", "switch_port",
                      "switch_name", "discovered_from", "last_seen"):
                if not str(scf.get(k) or "").strip() and str(dcf.get(k) or "").strip():
                    scf[k] = dcf[k]
                    changed = True
            if changed:
                sobj.custom_fields = scf
                sobj.save()
            # Re-point the duplicate's IPs onto the survivor (best-effort). An
            # IP is assigned to an interface, so land them on a 'merged'
            # interface of the survivor; if the API path differs, deleting the
            # duplicate below still frees the IPs (assigned_object goes null).
            try:
                self._repoint_ips(sobj, dobj)
            except Exception as e:
                logger.debug("dedupe: repoint IPs %s→%s skipped: %s",
                             dupe.get("id"), survivor.get("id"), e)
        self._journal("dcim.device", dupe["id"], "dedupe-merge",
                      note=f"merged duplicate into device {survivor['id']}")
        dobj.delete()
        return True

    def _repoint_ips(self, sobj, dobj) -> None:
        """Re-assign every IP address of the duplicate device to a 'merged'
        interface on the survivor so the address→device link survives the
        delete. Best-effort; caller guards exceptions."""
        ips = list(self.nb.ipam.ip_addresses.filter(device_id=dobj.id))
        if not ips:
            return
        iface = None
        try:
            existing = list(self.nb.dcim.interfaces.filter(
                device=sobj.id, name="merged"))
            iface = existing[0] if existing else self.nb.dcim.interfaces.create(
                device=sobj.id, name="merged", type="other")
        except Exception as e:
            logger.debug("dedupe: survivor merged-iface create skipped: %s", e)
            return
        if not iface:
            return
        for ip in ips:
            try:
                ip.assigned_object_type = "dcim.interface"
                ip.assigned_object_id = iface.id
                ip.save()
            except Exception as e:
                logger.debug("dedupe: repoint ip %s skipped: %s",
                             getattr(ip, "address", "?"), e)

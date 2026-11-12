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
# Standard-library logging; shares the "NetboxEngine" logger with the engine.
import logging
# re validates MAC address format.
import re
# Type hints.
from typing import Any, Dict, List, Optional

# Same logger name as the engine so all NetBox messages appear together.
logger = logging.getLogger("NetboxEngine")

# Values that are not a unique hardware identity: placeholder serials and
# shared/virtual MACs (VRRP, HSRP, multicast, all-zero). Two devices sharing one
# of these are NOT the same box.
_JUNK_SERIALS = {"0", "na", "n/a", "none", "null", "unknown", "default string",
                 "to be filled by o.e.m.", "system serial number",
                 "not specified", "0123456789", "123456789", "1234567890"}
# MAC prefixes of virtual router addresses (VRRP v4/v6, Cisco HSRP v1/v2) that
# many boxes share, so they never identify a single device.
_SHARED_MAC_PREFIXES = ("00:00:5e:00:01", "00:00:5e:00:02", "00:00:0c:07:ac",
                        "00:00:0c:9f:f")
# A "duplicate group" bigger than this is a shared identity, not one box.
_MAX_GROUP = 5


class DedupeMixin:
    """Find + merge duplicate NetBox devices that share a serial or MAC."""

    @staticmethod
    def _dsu_find(parent: Dict[Any, Any], x: Any) -> Any:
        """Union-find root with path compression."""
        # Walk up to the root of x's set.
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path compression: point every node on the way directly at the root.
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def _dsu_union(self, parent: Dict[Any, Any], a: Any, b: Any) -> None:
        """Merge the sets containing ``a`` and ``b`` (union-find union)."""
        # Find both roots; if different, hang b's set under a's root.
        ra, rb = self._dsu_find(parent, a), self._dsu_find(parent, b)
        if ra != rb:
            parent[rb] = ra

    @staticmethod
    def _real_serial(serial) -> str:
        """Normalised serial, or "" when it is a placeholder that identifies nothing."""
        # Lower-case for comparison.
        s = str(serial or "").strip().lower()
        # Too short, a known placeholder, or one repeated character (e.g. "0000"): not real.
        if len(s) < 4 or s in _JUNK_SERIALS or len(set(s)) == 1:
            return ""
        return s

    def _real_mac(self, mac) -> str:
        """Normalised MAC, or "" when it is malformed or shared (multicast/virtual/zero)."""
        # Normalise to lower-case colon form.
        m = self._norm_mac(mac or "")
        # Must be six colon-separated hex pairs.
        if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", m):
            return ""
        # Multicast bit set, all zeros, or a virtual-router prefix: not a unique identity.
        if int(m[:2], 16) & 1 or m in ("00:00:00:00:00:00",) \
                or m.startswith(_SHARED_MAC_PREFIXES):
            return ""
        return m

    @staticmethod
    def _slug(ref) -> str:
        """Lower-case slug of a nested NetBox reference dict, or ""."""
        return str((ref or {}).get("slug") or "").lower() if isinstance(ref, dict) else ""

    @staticmethod
    def _is_legacy_row(row: dict) -> bool:
        """An old LM-created record no sync maintains any more: no last_seen,
        no serial/MAC, not racked, in the generic 'discovered' site. Records a
        human placed (rack, real site) never qualify."""
        # Legacy = nothing that a sync or a human would have set, and in the fallback site.
        cf = row.get("custom_fields") or {}
        return (not str(cf.get("last_seen") or "").strip()
                and not str(row.get("serial") or "").strip()
                and not str(cf.get("mac_address") or "").strip()
                and not row.get("rack")
                and DedupeMixin._slug(row.get("site")) == "discovered")

    @staticmethod
    def _completeness_score(row: dict) -> int:
        """Rank a device row for survivor election: the record carrying the most
        hardware truth wins. Serial (globally unique) is weighted highest, then
        MAC, then a real (non-generic) device_type, a primary IP and a name."""
        # Custom fields may be null on the row.
        cf = row.get("custom_fields") or {}
        score = 0
        # Add weights for each piece of hardware truth the row carries.
        if row.get("rack"):
            score += 16  # placed by a human: never delete it in favour of a copy
        if str(cf.get("last_seen") or "").strip():
            score += 1
        if str(row.get("serial") or "").strip():
            score += 8
        if str(cf.get("mac_address") or "").strip():
            score += 4
        if str(cf.get("nw_device_id") or "").strip():
            score += 2
        # A specific device type (not the generic "discovered" one) counts too.
        dt = row.get("device_type") or {}
        dt_slug = str((dt.get("slug") if isinstance(dt, dict) else "") or "").lower()
        if dt_slug and dt_slug != "discovered":
            score += 2
        # A primary IP and a name each add a little.
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
        # Counters and the result list returned to the caller.
        scanned = 0
        merged = 0
        deleted = 0
        errors = 0
        first_err: Optional[str] = None
        groups_out: List[Dict[str, Any]] = []
        # Outer try: any unexpected error is reported as an ERROR result, never raised.
        try:
            # Fetch every device (optionally only one tenant's), 500 per page.
            list_params: Dict[str, Any] = {"limit": 500}
            if tenant_slug:
                list_params["tenant"] = tenant_slug
            try:
                rows = self._api_get_all("/api/dcim/devices/", list_params)
            except Exception as e:
                # Cannot list devices: nothing to do, report the error.
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "scanned": 0, "groups": [], "merged": 0, "deleted": 0,
                        "errors": 0, "applied": bool(apply)}
            # Indexes: rows by id, ids by identity key, ids by name, and the union-find parents.
            by_id: Dict[Any, dict] = {}
            keyed: Dict[str, List[Any]] = {}
            by_name: Dict[str, List[Any]] = {}
            parent: Dict[Any, Any] = {}
            # Index every device row by its identity keys.
            for row in (rows or []):
                did = row.get("id")
                # Rows without an id cannot be merged.
                if did is None:
                    continue
                # Count it and start it as its own set.
                scanned += 1
                by_id[did] = row
                parent[did] = did
                # A real serial is a strong identity key.
                serial = self._real_serial(row.get("serial"))
                if serial:
                    keyed.setdefault("serial:" + serial, []).append(did)
                # So is a real (unique) MAC from the custom field.
                cf = row.get("custom_fields") or {}
                mac = self._real_mac(cf.get("mac_address"))
                if mac:
                    keyed.setdefault("mac:" + mac, []).append(did)
                # Name (lower-case, no trailing dot) and primary IPv4 without the mask.
                name = str(row.get("name") or "").strip().lower().rstrip(".")
                pip = row.get("primary_ip4")
                ip = str((pip or {}).get("address") or "").split("/")[0] if isinstance(pip, dict) else ""
                # Index by name for the legacy-record pass below.
                if name:
                    by_name.setdefault(name, []).append(did)
                    if ip:
                        # Same name AND same IP within one tenant is one device.
                        keyed.setdefault(f"nameip:{self._slug(row.get('tenant'))}|{name}|{ip}",
                                         []).append(did)

            # Union devices that share a serial, MAC or name+IP (transitive).
            for ids in keyed.values():
                for other in ids[1:]:
                    self._dsu_union(parent, ids[0], other)
            # A legacy record (see _is_legacy_row) folds into the one live
            # record with the same name, whatever its tenant or name case.
            for ids in by_name.values():
                live = [i for i in ids if not self._is_legacy_row(by_id[i])]
                if len(live) == 1 and len(ids) > 1:
                    for i in ids:
                        self._dsu_union(parent, live[0], i)

            # Collect components of size > 1.
            # Group every device id under its set's root.
            comps: Dict[Any, List[Any]] = {}
            for did in by_id:
                comps.setdefault(self._dsu_find(parent, did), []).append(did)

            # Look at each connected group.
            for comp_ids in comps.values():
                # Singletons are not duplicates.
                if len(comp_ids) < 2:
                    continue
                # Too large to be one physical box: a shared identity, so skip and warn.
                if len(comp_ids) > _MAX_GROUP:
                    logger.warning("dedupe: skip %d devices sharing one identity (ids %s)",
                                   len(comp_ids), sorted(comp_ids)[:10])
                    continue
                # Row dicts of the group's members.
                members = [by_id[i] for i in comp_ids]
                # Survivor = most complete; tie-break lowest (oldest) id.
                survivor = max(
                    members,
                    key=lambda r: (self._completeness_score(r), -int(r["id"])))
                # Every other member is a duplicate to merge.
                dupes = [m for m in members if m["id"] != survivor["id"]]
                # Group key: the shared serial if any, else the shared MAC.
                # Used only for reporting in the result.
                key = ""
                scf = survivor.get("custom_fields") or {}
                if str(survivor.get("serial") or "").strip():
                    key = f"serial:{str(survivor.get('serial')).strip().lower()}"
                elif str(scf.get("mac_address") or "").strip():
                    key = f"mac:{self._norm_mac(scf.get('mac_address'))}"
                else:
                    # Survivor has neither: name-based group; prefer a duplicate's
                    # serial/MAC as the key.
                    key = "name:" + str(survivor.get("name") or "").lower()
                    for d in dupes:
                        s = str(d.get("serial") or "").strip()
                        if s:
                            key = f"serial:{s.lower()}"
                            break
                        dm = self._norm_mac((d.get("custom_fields") or {}).get("mac_address") or "")
                        if dm:
                            key = f"mac:{dm}"
                            break
                # Merge count for this group (stays 0 in a dry run).
                did_merged = 0
                # Only change NetBox when apply=True.
                if apply:
                    for dupe in dupes:
                        try:
                            if self._merge_device(survivor, dupe):
                                did_merged += 1
                                deleted += 1
                        # Count the failure, remember the first message, keep going.
                        except Exception as e:
                            errors += 1
                            if first_err is None:
                                first_err = f"merge {dupe.get('id')}→{survivor.get('id')}: {e}"
                            logger.debug("dedupe: merge %s into %s failed: %s",
                                         dupe.get("id"), survivor.get("id"), e)
                    # Add this group's merges to the total.
                    merged += did_merged
                # Report the group (dry run or applied).
                groups_out.append({
                    "key": key or "unknown",
                    "survivor_id": survivor["id"],
                    "survivor_name": survivor.get("name") or "",
                    "duplicate_ids": [d["id"] for d in dupes],
                    "merged": did_merged,
                })

            # PARTIAL when some merges failed.
            status = "SUCCESS" if errors == 0 else "PARTIAL"
            # Human-readable summary line.
            msg = (f"{len(groups_out)} duplicate group(s); "
                   f"{'merged ' + str(merged) + ' device(s)' if apply else 'dry run (no changes)'}")
            if first_err:
                msg += f"; first error: {first_err}"
            # Final result.
            return {"status": status, "scanned": scanned, "groups": groups_out,
                    "merged": merged, "deleted": deleted, "errors": errors,
                    "applied": bool(apply), "message": msg}
        except Exception as e:
            # Unexpected failure: return what was gathered so far plus an error.
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
        # Load both live records (the rows above may be from the bulk listing).
        sobj = self.nb.dcim.devices.get(survivor["id"])
        dobj = self.nb.dcim.devices.get(dupe["id"])
        # Duplicate already gone: nothing to merge.
        if not dobj:
            return False
        # Survivor exists: backfill its gaps from the duplicate.
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
                # Copy only when the survivor's value is empty and the duplicate has one.
                if not str(scf.get(k) or "").strip() and str(dcf.get(k) or "").strip():
                    scf[k] = dcf[k]
                    changed = True
            # Save once if anything was filled in.
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
                # IP re-point is best-effort; the delete still frees the IPs.
                logger.debug("dedupe: repoint IPs %s→%s skipped: %s",
                             dupe.get("id"), survivor.get("id"), e)
        # Record the merge in the duplicate's journal (audit trail), then delete it.
        self._journal("dcim.device", dupe["id"], "dedupe-merge",
                      note=f"merged duplicate into device {survivor['id']}")
        dobj.delete()
        self._forget_cached_device(dupe["id"])
        self._recheck_device_cache()  # the survivor may have changed
        return True

    def _repoint_ips(self, sobj, dobj) -> None:
        """Re-assign every IP address of the duplicate device to a 'merged'
        interface on the survivor so the address→device link survives the
        delete. Best-effort; caller guards exceptions."""
        # IPs assigned to any interface of the duplicate device.
        ips = list(self.nb.ipam.ip_addresses.filter(device_id=dobj.id))
        # None: nothing to move.
        if not ips:
            return
        iface = None
        # Find or create a "merged" interface on the survivor to hold the IPs.
        try:
            existing = list(self.nb.dcim.interfaces.filter(
                device=sobj.id, name="merged"))
            iface = existing[0] if existing else self.nb.dcim.interfaces.create(
                device=sobj.id, name="merged", type="other")
        except Exception as e:
            # Cannot create the interface: leave the IPs (the delete will unassign them).
            logger.debug("dedupe: survivor merged-iface create skipped: %s", e)
            return
        # No interface returned: nothing to attach to.
        if not iface:
            return
        # Move each IP; a failure on one does not stop the others.
        for ip in ips:
            try:
                ip.assigned_object_type = "dcim.interface"
                ip.assigned_object_id = iface.id
                ip.save()
            except Exception as e:
                # This IP could not be moved; it is freed when the duplicate is deleted.
                logger.debug("dedupe: repoint ip %s skipped: %s",
                             getattr(ip, "address", "?"), e)

"""Staleness sweep (offline/decommission/delete) for NetboxEngine."""
# Standard-library logging; shares the "NetboxEngine" logger with the engine.
import logging
# datetime/timezone compute ages from the UTC timestamps stored in NetBox.
from datetime import datetime, timezone
# Type hints.
from typing import Any, Dict, Optional

# Same logger name as the engine so all NetBox messages appear together.
logger = logging.getLogger("NetboxEngine")


class StalenessMixin:
    """Staleness sweep (offline/decommission/delete) for NetboxEngine."""

    # ── staleness sweep (cluster-wide age-out of sync-owned objects) ──────────

    # Objects with NO ``last_seen`` custom field are NEVER swept — that protects
    # hand-managed inventory (a human-created device/VM the syncs never touched
    # has no last_seen, so the sweep can't age it out). Only objects the syncs
    # stamped (every detection writes last_seen) are eligible.

    # Global safety floor: if the owned set is at least this large AND NOTHING
    # in it was seen within ``stale_days``, treat the discovery feed as stalled
    # (not the whole fleet as genuinely dead) and refuse to decommission/delete
    # anything that run. A tiny fleet (< floor) is exempt so a couple of truly
    # dead objects can still be cleaned. Override via LM_NETBOX_STALENESS_FLOOR.
    # Local os alias used only to read the env override, then removed from the class.
    import os as _os
    _STALENESS_MIN_FLEET = int(
        _os.environ.get("LM_NETBOX_STALENESS_FLOOR", "3") or 3)
    del _os
    @staticmethod
    def _parse_iso_cf(ts: str) -> Optional[datetime]:
        """Parse a ``last_seen``/``decommissioned_at`` CF timestamp (ISO, Z or
        offset) into an aware UTC datetime. None on unparseable/empty."""
        # Normalise to a stripped string; empty means "no timestamp".
        s = str(ts or "").strip()
        if not s:
            return None
        try:
            # Normalize a trailing Z to +00:00 for fromisoformat.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            # Parse; treat a naive timestamp as UTC, and return it converted to UTC.
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            # Unparsable value: behave as if there were no timestamp.
            return None

    def staleness_sweep(self, stale_days: int = 7,
                        delete_days: int = 30) -> Dict[str, Any]:
        """Cluster-wide age-out of sync-owned NetBox objects.

        For every device / VM / unassigned IP that carries a ``last_seen``
        custom field (i.e. a sync touched it — hand-managed objects have none
        and are never swept):
          • not seen for ``stale_days`` (default 7) and not already offline →
            set ``status = "offline"`` + ``decommissioned_at = now`` + journal
            entry ``staleness-sweep: decommissioned: not seen since <last_seen>``.
          • offline but seen again after ``decommissioned_at`` → revived
            (``status = "active"``, ``decommissioned_at`` cleared).
          • offline with ``decommissioned_at`` older than ``delete_days``
            (default 30) → DELETE the object. Deleting a device/VM frees its
            assigned IPs automatically (assigned_object goes null); an
            unassigned stale IP record is deleted so the address becomes free.

        Returns ``{status, scanned, decommissioned, deleted, ip_freed,
        errors, message, per_tenant}``. Never raises — a sweep failure is
        per-object and recorded in ``errors`` so one bad row can't abort the run.
        """
        # Counters for the result, the first error text, and per-tenant tallies.
        scanned = 0; decommissioned = 0; deleted = 0; revived = 0; ip_freed = 0; errors = 0
        first_err: Optional[str] = None
        per_tenant: Dict[str, Dict[str, int]] = {}

        def _bucket(slug: str) -> Dict[str, int]:
            """Return (creating if needed) the per-tenant counter dict for ``slug``."""
            # Objects without a tenant are counted under the shared "unassigned" key.
            key = str(slug or "").strip() or self._VM_SYNC_UNASSIGNED_KEY
            b = per_tenant.get(key)
            if b is None:
                b = {"decommissioned": 0, "deleted": 0, "errors": 0}
                per_tenant[key] = b
            return b

        def _tenant_slug(row: dict) -> str:
            """Tenant slug of a NetBox list row, or "" when it has no tenant."""
            t = row.get("tenant")
            if isinstance(t, dict):
                return str(t.get("slug") or "")
            return ""

        def _age_days(ts: str) -> Optional[float]:
            """Days since timestamp ``ts``, or None when it cannot be parsed."""
            dt = self._parse_iso_cf(ts)
            if dt is None:
                return None
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0

        def _seen_since(last_seen: str, decomm: str) -> bool:
            """Seen again after it was decommissioned (e.g. it answers pings again)."""
            # Both timestamps must parse, and last_seen must be the later one.
            ls_dt, dc_dt = self._parse_iso_cf(last_seen), self._parse_iso_cf(decomm)
            return bool(ls_dt and dc_dt and ls_dt > dc_dt)

        def _revive(obj) -> None:
            """Bring an offline object back: status active and decommission clock cleared."""
            obj.status = "active"
            # Clear the decommission timestamp and save both changes together.
            m = dict(obj.custom_fields or {})
            m["decommissioned_at"] = None
            obj.custom_fields = m
            obj.save()

        # One "now" stamp used for every decommission in this run.
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Any unexpected error below becomes an ERROR result (this method never raises).
        try:
            # Make sure last_seen/decommissioned_at fields exist before reading/writing them.
            self._ensure_custom_fields()
            # Thresholds in days as floats for comparison with fractional ages.
            cutoff_stale = float(stale_days)
            cutoff_delete = float(delete_days)

            # Fetch every owned object list UP FRONT so we can evaluate a global
            # safety floor before taking any destructive action.
            # Devices are mandatory: without them the sweep cannot run.
            try:
                dev_rows = self._api_get_all("/api/dcim/devices/", {"limit": 500})
            except Exception as e:
                # Cannot list devices: stop and report.
                return {"status": "ERROR", "message": f"failed to list devices: {e}",
                        "scanned": 0, "decommissioned": 0, "deleted": 0,
                        "ip_freed": 0, "errors": 0, "per_tenant": per_tenant}
            # VMs and IPs are optional: a failure just skips that object type.
            try:
                vm_rows = self._api_get_all("/api/virtualization/virtual-machines/",
                                            {"limit": 500})
            except Exception as e:
                # Log and continue without VMs.
                logger.warning("staleness_sweep: list VMs failed: %s", e)
                vm_rows = []
            try:
                ip_rows = self._api_get_all("/api/ipam/ip-addresses/", {"limit": 500})
            except Exception as e:
                # Log and continue without IPs.
                logger.warning("staleness_sweep: list IPs failed: %s", e)
                ip_rows = []

            # ── GLOBAL SAFETY FLOOR ──
            # A stalled discovery feed makes the ENTIRE owned fleet age past
            # stale_days at once — which would otherwise decommission then delete
            # everything we own. Require that at least ONE owned object was seen
            # recently (within stale_days): if nothing is fresh and the fleet is
            # non-trivial, the feed is down, not the fleet. Count sync-owned
            # objects (carry last_seen; VMs also require proxmox_unique_id) and
            # how many are fresh.
            # Totals updated by _tally below.
            owned_total = 0
            fresh_count = 0

            def _tally(rows, require_uid):
                """Count sync-owned rows and how many of them were seen recently."""
                nonlocal owned_total, fresh_count
                # Only rows with last_seen (and, for VMs, proxmox_unique_id) are owned.
                for r in rows:
                    rcf = r.get("custom_fields") or {}
                    if require_uid and not str(
                            rcf.get("proxmox_unique_id") or "").strip():
                        continue
                    rls = str(rcf.get("last_seen") or "").strip()
                    if not rls:
                        continue
                    # Owned: count it, and count it as fresh if seen within stale_days.
                    owned_total += 1
                    a = _age_days(rls)
                    if a is not None and a < cutoff_stale:
                        fresh_count += 1

            # Tally each object type (VMs must carry proxmox_unique_id to be ours).
            _tally(dev_rows, False)
            _tally(vm_rows, True)
            _tally(ip_rows, False)
            # Feed looks stalled (big enough fleet, nothing fresh): refuse to touch anything.
            if owned_total >= self._STALENESS_MIN_FLEET and fresh_count == 0:
                msg = (f"ABORTED: none of {owned_total} owned object(s) seen "
                       f"within {stale_days}d — discovery feed appears stalled; "
                       f"refusing to decommission/delete the entire owned fleet")
                logger.error("staleness_sweep: %s", msg)
                return {"status": "ABORTED", "scanned": owned_total,
                        "decommissioned": 0, "deleted": 0, "ip_freed": 0,
                        "errors": 0, "message": msg, "per_tenant": per_tenant}

            # ── devices (cluster-wide, no tenant scope) ──
            for row in dev_rows:
                # Rows without last_seen were never stamped by a sync.
                cf = row.get("custom_fields") or {}
                ls = str(cf.get("last_seen") or "").strip()
                if not ls:
                    continue  # never swept (hand-managed)
                # Count the row and work out its tenant and age; unparsable age is skipped.
                scanned += 1
                tslug = _tenant_slug(row)
                age = _age_days(ls)
                if age is None:
                    continue
                # Status may be a {"value": ...} dict or a plain string.
                st = row.get("status")
                status_val = str((st.get("value") if isinstance(st, dict)
                                  else st) or "")
                # When it was decommissioned (empty if never).
                decomm = str(cf.get("decommissioned_at") or "").strip()
                # Decide from the bulk-list row FIRST — the list already carries
                # status + custom_fields, so rows that need no action skip the
                # per-row ``.get(id)`` round-trip entirely (was an N+1 fetch on
                # every device, even ones far from any threshold). Only fetch
                # the live object when we're actually about to mutate it.
                action = None  # "revive" | "delete" | "decommission" | None
                if status_val == "offline" and decomm:
                    # Already offline: revive if seen again, or delete once offline long enough.
                    dage = _age_days(decomm)
                    if _seen_since(ls, decomm):
                        action = "revive"
                    elif dage is not None and dage >= cutoff_delete:
                        action = "delete"
                # Online but unseen for stale_days: decommission it.
                if action is None and age >= cutoff_stale and status_val != "offline":
                    action = "decommission"
                # Nothing to do for this row.
                if action is None:
                    continue
                # Per-object try so one failure does not stop the sweep.
                try:
                    # Fetch the live record we are about to change; skip if it vanished.
                    obj = self.nb.dcim.devices.get(row["id"])
                    if not obj:
                        continue
                    # Delete: removing the device also frees its IPs. Journal records why.
                    if action == "delete":
                        obj.delete()
                        deleted += 1
                        self._global_devices_cache = None  # next listing must drop it
                        _bucket(tslug)["deleted"] += 1
                        self._journal("dcim.device", row["id"], "staleness-sweep",
                                       note=f"deleted: offline since {decomm}")
                        continue
                    # Revive: bring it back to active.
                    if action == "revive":
                        _revive(obj)
                        revived += 1
                        self._journal("dcim.device", row["id"], "staleness-sweep",
                                       note=f"revived: seen {ls} after decommission {decomm}")
                        continue
                    # 7-day decommission: unseen past stale_days + not yet offline.
                    try:
                        obj.status = "offline"
                        m = dict(obj.custom_fields or {})
                        m["decommissioned_at"] = now_iso
                        obj.custom_fields = m
                        obj.save()
                        decommissioned += 1
                        _bucket(tslug)["decommissioned"] += 1
                        self._journal("dcim.device", row["id"], "staleness-sweep",
                                       note=f"decommissioned: not seen since {ls}")
                    except Exception as e:
                        # Decommission write failed: count it.
                        errors += 1
                        _bucket(tslug)["errors"] += 1
                        if first_err is None:
                            first_err = f"decomm device {row['id']}: {e}"
                except Exception as e:
                    # Any other per-device failure: count it and keep going.
                    errors += 1
                    _bucket(tslug)["errors"] += 1
                    if first_err is None:
                        first_err = f"device {row['id']}: {e}"
                    logger.debug("staleness_sweep: device %s failed: %s", row["id"], e)

            # ── VMs (cluster-wide; only those we own via proxmox_unique_id) ──
            # (vm_rows fetched up front for the global floor above.)
            for row in vm_rows:
                # Only VMs the Proxmox sync owns (they carry proxmox_unique_id) are swept.
                cf = row.get("custom_fields") or {}
                if not str(cf.get("proxmox_unique_id") or "").strip():
                    continue  # not sync-owned → never swept
                # Not stamped by a sync: skip.
                ls = str(cf.get("last_seen") or "").strip()
                if not ls:
                    continue
                # Count the row and work out its tenant and age; unparsable age is skipped.
                scanned += 1
                tslug = _tenant_slug(row)
                age = _age_days(ls)
                if age is None:
                    continue
                # Status may be a {"value": ...} dict or a plain string.
                status = row.get("status")
                status_val = str((status.get("value") if isinstance(status, dict)
                                  else status) or "")
                # When it was decommissioned (empty if never).
                decomm = str(cf.get("decommissioned_at") or "").strip()
                # Decide from the bulk-list row FIRST (see devices loop): skip
                # the per-row .get(id) round-trip for VMs that need no action.
                action = None
                # Same decision rules as for devices.
                if status_val == "offline" and decomm:
                    dage = _age_days(decomm)
                    if _seen_since(ls, decomm):
                        action = "revive"
                    elif dage is not None and dage >= cutoff_delete:
                        action = "delete"
                # Online but unseen for stale_days: decommission it.
                if action is None and age >= cutoff_stale and status_val != "offline":
                    action = "decommission"
                # Nothing to do for this VM.
                if action is None:
                    continue
                # Per-VM try so one failure does not stop the sweep.
                try:
                    # Fetch the live record; skip if it vanished.
                    obj = self.nb.virtualization.virtual_machines.get(row["id"])
                    if not obj:
                        continue
                    # Delete the VM (frees its IPs) and journal why.
                    if action == "delete":
                        obj.delete()
                        deleted += 1
                        _bucket(tslug)["deleted"] += 1
                        self._journal("virtualization.virtualmachine", row["id"],
                                       "staleness-sweep",
                                       note=f"deleted: offline since {decomm}")
                        continue
                    # Revive the VM.
                    if action == "revive":
                        _revive(obj)
                        revived += 1
                        self._journal("virtualization.virtualmachine", row["id"],
                                       "staleness-sweep",
                                       note=f"revived: seen {ls} after decommission {decomm}")
                        continue
                    # Decommission: mark offline and start the delete clock.
                    try:
                        obj.status = "offline"
                        m = dict(obj.custom_fields or {})
                        m["decommissioned_at"] = now_iso
                        obj.custom_fields = m
                        obj.save()
                        decommissioned += 1
                        _bucket(tslug)["decommissioned"] += 1
                        self._journal("virtualization.virtualmachine", row["id"],
                                       "staleness-sweep",
                                       note=f"decommissioned: not seen since {ls}")
                    except Exception as e:
                        # Decommission write failed: count it.
                        errors += 1
                        _bucket(tslug)["errors"] += 1
                        if first_err is None:
                            first_err = f"decomm VM {row['id']}: {e}"
                except Exception as e:
                    # Any other per-VM failure: count it and keep going.
                    errors += 1
                    _bucket(tslug)["errors"] += 1
                    if first_err is None:
                        first_err = f"VM {row['id']}: {e}"
                    logger.debug("staleness_sweep: VM %s failed: %s", row["id"], e)

            # ── unassigned stale IPs (free the address) ──
            # IPs still assigned to a kept device are freed by that device's
            # delete above; here we only delete IPs that are already unassigned
            # (assigned_object_id null) + carry our last_seen + aged past
            # delete_days, so an orphaned IP record releases its address.
            # (ip_rows fetched up front for the global floor above.)
            for row in ip_rows:
                # Only IPs stamped by a sync are considered.
                cf = row.get("custom_fields") or {}
                ls = str(cf.get("last_seen") or "").strip()
                if not ls:
                    continue
                # assigned? (a_terminations / assigned_object_id). Skip assigned —
                # the owning device/VM sweep (or NetBox's cascade on delete)
                # handles those.
                assigned = row.get("assigned_object_id")
                if row.get("assigned_object_type") and assigned is not None:
                    continue
                # Count it; only delete once it has been unseen for delete_days.
                scanned += 1
                age = _age_days(ls)
                if age is None or age < cutoff_delete:
                    continue
                # Delete the record so the address becomes free again; journal why.
                try:
                    obj = self.nb.ipam.ip_addresses.get(row["id"])
                    if obj:
                        obj.delete()
                        ip_freed += 1
                        self._journal("ipam.ipaddress", row["id"], "staleness-sweep",
                                       note=f"freed: unassigned, last seen {ls}")
                except Exception as e:
                    # Count the failure and keep going.
                    errors += 1
                    if first_err is None:
                        first_err = f"IP {row['id']}: {e}"
                    logger.debug("staleness_sweep: IP %s failed: %s", row["id"], e)

            # Summary line with all counters.
            msg = (f"swept {scanned} object(s): {decommissioned} decommissioned, "
                   f"{revived} revived, {deleted} deleted, {ip_freed} IP(s) freed, {errors} errors")
            # Errors: include the first one and log at WARNING.
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("staleness_sweep: %s", msg)
            else:
                # Clean run: log at INFO.
                logger.info("staleness_sweep: %s", msg)
            # Final result for the Hub.
            return {"status": "SUCCESS", "scanned": scanned,
                    "decommissioned": decommissioned, "revived": revived, "deleted": deleted,
                    "ip_freed": ip_freed, "errors": errors, "message": msg,
                    "per_tenant": per_tenant}
        except Exception as e:
            # Unexpected failure: return the counters gathered so far with an error.
            logger.error("staleness_sweep failed: %s", e)
            return {"status": "ERROR", "message": str(e), "scanned": scanned,
                    "decommissioned": decommissioned, "deleted": deleted,
                    "ip_freed": ip_freed, "errors": errors, "per_tenant": per_tenant}

"""Staleness sweep (offline/decommission/delete) for NetboxEngine."""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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
    import os as _os
    _STALENESS_MIN_FLEET = int(
        _os.environ.get("LM_NETBOX_STALENESS_FLOOR", "3") or 3)
    del _os
    @staticmethod
    def _parse_iso_cf(ts: str) -> Optional[datetime]:
        """Parse a ``last_seen``/``decommissioned_at`` CF timestamp (ISO, Z or
        offset) into an aware UTC datetime. None on unparseable/empty."""
        s = str(ts or "").strip()
        if not s:
            return None
        try:
            # Normalize a trailing Z to +00:00 for fromisoformat.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
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
          • offline with ``decommissioned_at`` older than ``delete_days``
            (default 30) → DELETE the object. Deleting a device/VM frees its
            assigned IPs automatically (assigned_object goes null); an
            unassigned stale IP record is deleted so the address becomes free.

        Returns ``{status, scanned, decommissioned, deleted, ip_freed,
        errors, message, per_tenant}``. Never raises — a sweep failure is
        per-object and recorded in ``errors`` so one bad row can't abort the run.
        """
        scanned = 0; decommissioned = 0; deleted = 0; ip_freed = 0; errors = 0
        first_err: Optional[str] = None
        per_tenant: Dict[str, Dict[str, int]] = {}

        def _bucket(slug: str) -> Dict[str, int]:
            key = str(slug or "").strip() or self._VM_SYNC_UNASSIGNED_KEY
            b = per_tenant.get(key)
            if b is None:
                b = {"decommissioned": 0, "deleted": 0, "errors": 0}
                per_tenant[key] = b
            return b

        def _tenant_slug(row: dict) -> str:
            t = row.get("tenant")
            if isinstance(t, dict):
                return str(t.get("slug") or "")
            return ""

        def _age_days(ts: str) -> Optional[float]:
            dt = self._parse_iso_cf(ts)
            if dt is None:
                return None
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            self._ensure_custom_fields()
            cutoff_stale = float(stale_days)
            cutoff_delete = float(delete_days)

            # Fetch every owned object list UP FRONT so we can evaluate a global
            # safety floor before taking any destructive action.
            try:
                dev_rows = self._api_get_all("/api/dcim/devices/", {"limit": 500})
            except Exception as e:
                return {"status": "ERROR", "message": f"failed to list devices: {e}",
                        "scanned": 0, "decommissioned": 0, "deleted": 0,
                        "ip_freed": 0, "errors": 0, "per_tenant": per_tenant}
            try:
                vm_rows = self._api_get_all("/api/virtualization/virtual-machines/",
                                            {"limit": 500})
            except Exception as e:
                logger.warning("staleness_sweep: list VMs failed: %s", e)
                vm_rows = []
            try:
                ip_rows = self._api_get_all("/api/ipam/ip-addresses/", {"limit": 500})
            except Exception as e:
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
            owned_total = 0
            fresh_count = 0

            def _tally(rows, require_uid):
                nonlocal owned_total, fresh_count
                for r in rows:
                    rcf = r.get("custom_fields") or {}
                    if require_uid and not str(
                            rcf.get("proxmox_unique_id") or "").strip():
                        continue
                    rls = str(rcf.get("last_seen") or "").strip()
                    if not rls:
                        continue
                    owned_total += 1
                    a = _age_days(rls)
                    if a is not None and a < cutoff_stale:
                        fresh_count += 1

            _tally(dev_rows, False)
            _tally(vm_rows, True)
            _tally(ip_rows, False)
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
                cf = row.get("custom_fields") or {}
                ls = str(cf.get("last_seen") or "").strip()
                if not ls:
                    continue  # never swept (hand-managed)
                scanned += 1
                tslug = _tenant_slug(row)
                age = _age_days(ls)
                if age is None:
                    continue
                st = row.get("status")
                status_val = str((st.get("value") if isinstance(st, dict)
                                  else st) or "")
                decomm = str(cf.get("decommissioned_at") or "").strip()
                # Decide from the bulk-list row FIRST — the list already carries
                # status + custom_fields, so rows that need no action skip the
                # per-row ``.get(id)`` round-trip entirely (was an N+1 fetch on
                # every device, even ones far from any threshold). Only fetch
                # the live object when we're actually about to mutate it.
                action = None  # "delete" | "decommission" | None
                if status_val == "offline" and decomm:
                    dage = _age_days(decomm)
                    if dage is not None and dage >= cutoff_delete:
                        action = "delete"
                if action is None and age >= cutoff_stale and status_val != "offline":
                    action = "decommission"
                if action is None:
                    continue
                try:
                    obj = self.nb.dcim.devices.get(row["id"])
                    if not obj:
                        continue
                    if action == "delete":
                        obj.delete()
                        deleted += 1
                        _bucket(tslug)["deleted"] += 1
                        self._journal("dcim.device", row["id"], "staleness-sweep",
                                       note=f"deleted: offline since {decomm}")
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
                        errors += 1
                        _bucket(tslug)["errors"] += 1
                        if first_err is None:
                            first_err = f"decomm device {row['id']}: {e}"
                except Exception as e:
                    errors += 1
                    _bucket(tslug)["errors"] += 1
                    if first_err is None:
                        first_err = f"device {row['id']}: {e}"
                    logger.debug("staleness_sweep: device %s failed: %s", row["id"], e)

            # ── VMs (cluster-wide; only those we own via proxmox_unique_id) ──
            # (vm_rows fetched up front for the global floor above.)
            for row in vm_rows:
                cf = row.get("custom_fields") or {}
                if not str(cf.get("proxmox_unique_id") or "").strip():
                    continue  # not sync-owned → never swept
                ls = str(cf.get("last_seen") or "").strip()
                if not ls:
                    continue
                scanned += 1
                tslug = _tenant_slug(row)
                age = _age_days(ls)
                if age is None:
                    continue
                status = row.get("status")
                status_val = str((status.get("value") if isinstance(status, dict)
                                  else status) or "")
                decomm = str(cf.get("decommissioned_at") or "").strip()
                # Decide from the bulk-list row FIRST (see devices loop): skip
                # the per-row .get(id) round-trip for VMs that need no action.
                action = None
                if status_val == "offline" and decomm:
                    dage = _age_days(decomm)
                    if dage is not None and dage >= cutoff_delete:
                        action = "delete"
                if action is None and age >= cutoff_stale and status_val != "offline":
                    action = "decommission"
                if action is None:
                    continue
                try:
                    obj = self.nb.virtualization.virtual_machines.get(row["id"])
                    if not obj:
                        continue
                    if action == "delete":
                        obj.delete()
                        deleted += 1
                        _bucket(tslug)["deleted"] += 1
                        self._journal("virtualization.virtualmachine", row["id"],
                                       "staleness-sweep",
                                       note=f"deleted: offline since {decomm}")
                        continue
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
                        errors += 1
                        _bucket(tslug)["errors"] += 1
                        if first_err is None:
                            first_err = f"decomm VM {row['id']}: {e}"
                except Exception as e:
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
                scanned += 1
                age = _age_days(ls)
                if age is None or age < cutoff_delete:
                    continue
                try:
                    obj = self.nb.ipam.ip_addresses.get(row["id"])
                    if obj:
                        obj.delete()
                        ip_freed += 1
                        self._journal("ipam.ipaddress", row["id"], "staleness-sweep",
                                       note=f"freed: unassigned, last seen {ls}")
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"IP {row['id']}: {e}"
                    logger.debug("staleness_sweep: IP %s failed: %s", row["id"], e)

            msg = (f"swept {scanned} object(s): {decommissioned} decommissioned, "
                   f"{deleted} deleted, {ip_freed} IP(s) freed, {errors} errors")
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("staleness_sweep: %s", msg)
            else:
                logger.info("staleness_sweep: %s", msg)
            return {"status": "SUCCESS", "scanned": scanned,
                    "decommissioned": decommissioned, "deleted": deleted,
                    "ip_freed": ip_freed, "errors": errors, "message": msg,
                    "per_tenant": per_tenant}
        except Exception as e:
            logger.error("staleness_sweep failed: %s", e)
            return {"status": "ERROR", "message": str(e), "scanned": scanned,
                    "decommissioned": decommissioned, "deleted": deleted,
                    "ip_freed": ip_freed, "errors": errors, "per_tenant": per_tenant}

"""Tests for NetboxEngine.staleness_sweep (NETBOX_STALENESS_SWEEP handler).

Cluster-wide age-out of sync-owned objects: a device/VM not seen for ``stale_days``
→ status=offline + decommissioned_at + journal; offline + decommissioned_at older
than ``delete_days`` → deleted (IPs free automatically); an unassigned stale IP →
freed. Objects with NO ``last_seen`` custom field are never swept (protects
hand-managed inventory).

Self-contained harness: fakes engine.nb + _api_get_all (the cluster-wide lists) so
only the sweep decision logic runs.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Obj:
    """Minimal pynetbox-record stand-in: settable custom_fields/status + save/delete."""
    def __init__(self, id=1, custom_fields=None, status="active"):
        self.id = id
        self.custom_fields = dict(custom_fields or {})
        self.status = status
        self.primary_ip4 = None
        self.save = MagicMock()
        self.delete = MagicMock()


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _engine(device_rows=None, vm_rows=None, ip_rows=None):
    """Engine whose nb is a MagicMock; _api_get_all returns the cluster-wide lists
    in order (devices, then VMs, then IPs). _ensure_custom_fields no-ops."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    lists = [device_rows or [], vm_rows or [], ip_rows or []]
    eng._api_get_all = MagicMock(side_effect=lists)
    eng._ensure_custom_fields = MagicMock()
    return eng


def _row(rid, last_seen, status="active", decomm="", tenant_slug="lrb",
         discovered_from="opnsense", assigned=None):
    """A raw API device/VM row dict carrying custom_fields + status."""
    cf = {"last_seen": last_seen}
    if decomm:
        cf["decommissioned_at"] = decomm
    if discovered_from is not None:
        cf["discovered_from"] = discovered_from
    row = {"id": rid, "custom_fields": cf, "tenant": {"slug": tenant_slug}}
    # NetBox returns status as {value: "active"} for the list endpoint.
    row["status"] = {"value": status} if status else {}
    return row


# ── 7-day decommission ────────────────────────────────────────────────────────

def test_staleness_sweep_decommissions_device_unseen_past_stale_days():
    # Device last seen 10 days ago, status active → offline + decommissioned_at +
    # journal. Not yet 30d past decommission (just set) → NOT deleted.
    eng = _engine(device_rows=[_row(11, last_seen=_iso(10))])
    dev = _Obj(id=11, custom_fields={"last_seen": _iso(10)}, status="active")
    eng.nb.dcim.devices.get.return_value = dev

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["status"] == "SUCCESS", res
    assert res["decommissioned"] == 1
    assert res["deleted"] == 0
    assert dev.status == "offline"
    assert dev.custom_fields.get("decommissioned_at") not in ("", None)
    dev.save.assert_called()
    # Journal entry written (best-effort; create on journal_entries endpoint).
    eng.nb.extras.journal_entries.create.assert_called()


def test_staleness_sweep_skips_device_with_no_last_seen():
    # Hand-managed device (no last_seen CF) is NEVER swept — protects inventory.
    row = {"id": 12, "custom_fields": {}, "tenant": {"slug": "lrb"}, "status": {"value": "active"}}
    eng = _engine(device_rows=[row])

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["status"] == "SUCCESS", res
    assert res["decommissioned"] == 0
    assert res["deleted"] == 0
    assert res["scanned"] == 0     # never counted as eligible
    eng.nb.dcim.devices.get.assert_not_called()


def test_staleness_sweep_leaves_recently_seen_device_alone():
    # Device seen 2 days ago (< 7d stale) → no change.
    eng = _engine(device_rows=[_row(13, last_seen=_iso(2))])
    dev = _Obj(id=13, custom_fields={"last_seen": _iso(2)}, status="active")
    eng.nb.dcim.devices.get.return_value = dev

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["decommissioned"] == 0
    assert res["deleted"] == 0
    assert dev.status == "active"
    dev.save.assert_not_called()


# ── 30-day delete ─────────────────────────────────────────────────────────────

def test_staleness_sweep_deletes_device_offline_past_delete_days():
    # Already offline, decommissioned_at 40 days ago (> 30d) → deleted + journal.
    eng = _engine(device_rows=[
        _row(21, last_seen=_iso(40), status="offline", decomm=_iso(40))])
    dev = _Obj(id=21, custom_fields={"last_seen": _iso(40),
                                      "decommissioned_at": _iso(40)},
               status="offline")
    eng.nb.dcim.devices.get.return_value = dev

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["deleted"] == 1
    dev.delete.assert_called_once()


def test_staleness_sweep_keeps_offline_device_within_delete_window():
    # Offline + decommissioned_at 15 days ago (< 30d) → kept (not yet deletable).
    eng = _engine(device_rows=[
        _row(22, last_seen=_iso(15), status="offline", decomm=_iso(15))])
    dev = _Obj(id=22, custom_fields={"last_seen": _iso(15),
                                      "decommissioned_at": _iso(15)},
               status="offline")
    eng.nb.dcim.devices.get.return_value = dev

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["deleted"] == 0
    dev.delete.assert_not_called()


# ── VMs ───────────────────────────────────────────────────────────────────────

def test_staleness_sweep_decommissions_sync_owned_vm():
    # VM with proxmox_unique_id + last_seen 10d ago → offline + decommissioned_at.
    row = _row(31, last_seen=_iso(10), discovered_from=None, tenant_slug="lrb")
    row["custom_fields"]["proxmox_unique_id"] = "pxmx:300"
    eng = _engine(device_rows=[], vm_rows=[row])
    vm = _Obj(id=31, custom_fields={"proxmox_unique_id": "pxmx:300",
                                      "last_seen": _iso(10)}, status="active")
    eng.nb.virtualization.virtual_machines.get.return_value = vm

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["decommissioned"] == 1
    assert vm.status == "offline"


def test_staleness_sweep_skips_non_sync_owned_vm():
    # A hand-created VM (no proxmox_unique_id) → never swept even if last_seen set.
    row = _row(32, last_seen=_iso(40), discovered_from=None)
    # no proxmox_unique_id
    eng = _engine(device_rows=[], vm_rows=[row])

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["decommissioned"] == 0
    assert res["deleted"] == 0
    eng.nb.virtualization.virtual_machines.get.assert_not_called()


# ── IP freeing ────────────────────────────────────────────────────────────────

def test_staleness_sweep_frees_unassigned_stale_ip():
    # Unassigned IP (assigned_object_id None) with last_seen 40d ago → freed.
    ip_row = {"id": 41, "custom_fields": {"last_seen": _iso(40)},
              "assigned_object_type": None, "assigned_object_id": None}
    eng = _engine(device_rows=[], vm_rows=[], ip_rows=[ip_row])
    ipobj = _Obj(id=41, custom_fields={"last_seen": _iso(40)})
    eng.nb.ipam.ip_addresses.get.return_value = ipobj

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["ip_freed"] == 1
    ipobj.delete.assert_called_once()


def test_staleness_sweep_keeps_assigned_ip():
    # IP still assigned to a device → NOT freed here (the owning device's sweep /
    # NetBox cascade on device delete handles it).
    ip_row = {"id": 42, "custom_fields": {"last_seen": _iso(40)},
              "assigned_object_type": "dcim.interface", "assigned_object_id": 900}
    eng = _engine(device_rows=[], vm_rows=[], ip_rows=[ip_row])

    res = eng.staleness_sweep(stale_days=7, delete_days=30)

    assert res["ip_freed"] == 0
    eng.nb.ipam.ip_addresses.get.assert_not_called()
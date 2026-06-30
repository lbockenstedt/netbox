"""Tests for NetboxEngine.sync_vms custom-field resilience.

The deployed external NetBox didn't have the proxmox_* custom fields attached to
``virtualization.virtualmachine``, so a VM create carrying inline ``custom_fields``
400'd ("Custom field 'proxmox_node' does not exist for this object type") —
blocking ALL VM syncs (0/N). The fix: create/update WITHOUT custom_fields, then
PATCH the proxmox_* linkage best-effort so a provisioning gap never blocks the
sync. These tests pin that contract.

Self-contained: inserts src/ on sys.path and fakes engine.nb + the helpers
sync_vms calls (cluster/tenant resolution, primary-ip assignment, custom-field
self-heal) so we exercise only the create/update + custom-field logic.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Obj:
    """Minimal pynetbox-record stand-in: settable custom_fields + save/delete."""
    def __init__(self, id=1, custom_fields=None):
        self.id = id
        self.custom_fields = dict(custom_fields or {})
        self.primary_ip4 = None
        self.save = MagicMock()
        self.delete = MagicMock()


def _engine():
    """An engine whose nb + sync_vms helpers are faked so only the
    create/update + custom-field logic runs."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng._api_get_all = MagicMock(return_value=[])          # no existing VMs
    eng._ensure_vm_cluster = MagicMock(return_value=999)   # cluster id
    eng._vm_status_map = MagicMock(return_value="active")
    eng._assign_vm_primary_ip4 = MagicMock()               # no-op (ips best-effort)
    eng._ensure_custom_fields = MagicMock()                # skip self-heal here
    return eng


_VM = {"unique_id": "pxmx:100", "vmid": 100, "name": "vm100",
       "node": "pve1", "type": "qemu", "status": "running",
       "vcpus": 2, "disk_gb": 10, "mem_mb": 2048, "tenant_slug": "lrb"}


def test_sync_vms_create_omits_custom_fields_then_patches_best_effort():
    eng = _engine()
    vm_obj = _Obj(id=42, custom_fields={})
    eng.nb.virtualization.virtual_machines.create.return_value = vm_obj

    res = eng.sync_vms(vms=[_VM], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0
    ck = eng.nb.virtualization.virtual_machines.create.call_args.kwargs
    # Never inline custom_fields on a VM create (the 0/N 400 trap).
    assert "custom_fields" not in ck
    assert ck["name"] == "vm100"
    # proxmox_* linkage is PATCHed on after the create succeeds.
    assert vm_obj.custom_fields.get("proxmox_unique_id") == "pxmx:100"
    assert vm_obj.custom_fields.get("proxmox_node") == "pve1"
    assert vm_obj.custom_fields.get("proxmox_vmid") == "100"
    vm_obj.save.assert_called()   # the best-effort cf PATCH


def test_sync_vms_create_succeeds_even_when_custom_fields_unprovisioned():
    # The post-create custom_fields PATCH raises (fields not attached on the
    # deployed NetBox). The VM must still be created/synced — only the linkage
    # is skipped, and the failure is NOT counted as a sync error.
    eng = _engine()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.save.side_effect = [Exception(
        "Custom field 'proxmox_node' does not exist for this object type")]
    eng.nb.virtualization.virtual_machines.create.return_value = vm_obj

    res = eng.sync_vms(vms=[_VM], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0              # best-effort cf failure, not a sync error
    eng.nb.virtualization.virtual_machines.create.assert_called_once()


def test_sync_vms_update_core_save_lands_even_if_custom_fields_fail():
    # Update path: core fields (name/cluster/status/tenant) save FIRST, then the
    # custom_fields PATCH best-effort. A cf 400 must NOT undo the core update.
    eng = _engine()
    existing_row = {"id": 7, "custom_fields": {"proxmox_unique_id": "pxmx:100"}}
    eng._api_get_all = MagicMock(return_value=[existing_row])
    existing_vm = _Obj(id=7, custom_fields={"proxmox_unique_id": "pxmx:100"})
    # 1st save = core fields (ok); 2nd save = custom_fields PATCH (raises).
    existing_vm.save.side_effect = [
        None, Exception("does not exist for this object type")]
    eng.nb.virtualization.virtual_machines.get.return_value = existing_vm

    res = eng.sync_vms(
        vms=[{**_VM, "name": "vm-renamed"}], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0
    # Core rename landed before the cf PATCH failed.
    assert existing_vm.name == "vm-renamed"
    assert existing_vm.save.call_count == 2   # core save + (failed) cf PATCH
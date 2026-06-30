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


# ── VM IP/MAC gathering: _assign_vm_primary_ip4 builds vminterfaces + IPs ─────

def _engine_real_assign():
    """Engine whose nb is a MagicMock but _assign_vm_primary_ip4 is NOT mocked —
    so the vminterface/IP-building logic actually runs. Helpers it calls
    (_mask_for_ip, _norm_mac, _reuse_or_create_ip, _journal, _stamp_last_seen)
    run against the faked nb."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.ipam.ip_addresses.get.return_value = None   # no pre-existing global IP
    eng._api_get = MagicMock(return_value={"results": []})  # no containing prefix → /32
    eng._ensure_custom_fields = MagicMock()
    return eng


class _Iface:
    def __init__(self, id=1, name="eth0", mac_address=None):
        self.id = id
        self.name = name
        self.mac_address = mac_address
        self.save = MagicMock()


def test_assign_vm_primary_ip4_builds_vminterfaces_with_macs_and_all_ips():
    # Two interfaces from the pxmx agent (eth0 + eth1), each with a MAC + IPs.
    # The helper creates a vminterface per name carrying the native MAC, one
    # ipam.ip_address per IP (reused/created), tags each IP with the iface MAC,
    # and sets primary_ip4 to the first IP.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    eng.nb.virtualization.vminterfaces.filter.return_value = []  # none exist
    eng.nb.virtualization.vminterfaces.create.side_effect = [_Iface(100, "eth0"),
                                                              _Iface(101, "eth1")]
    ip_objs = [_Obj(id=1001), _Obj(id=1002), _Obj(id=1003)]
    eng.nb.ipam.ip_addresses.create.side_effect = ip_objs

    vm = {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5", "10.0.0.6"]},
        {"name": "eth1", "mac": "aa:bb:cc:dd:ee:02", "ips": ["10.0.0.7"]},
    ]}
    eng._assign_vm_primary_ip4(vm_obj, vm, tenant=None)

    # Two vminterfaces created, each with its MAC.
    vmi_calls = eng.nb.virtualization.vminterfaces.create.call_args_list
    assert len(vmi_calls) == 2
    assert vmi_calls[0].kwargs["name"] == "eth0"
    assert vmi_calls[0].kwargs["mac_address"] == "aa:bb:cc:dd:ee:01"
    assert vmi_calls[1].kwargs["name"] == "eth1"
    assert vmi_calls[1].kwargs["mac_address"] == "aa:bb:cc:dd:ee:02"
    # Three IPs created (2 + 1), assigned to vminterfaces, MAC-tagged.
    ip_calls = eng.nb.ipam.ip_addresses.create.call_args_list
    assert len(ip_calls) == 3
    assert ip_calls[0].kwargs["assigned_object_type"] == "virtualization.vminterface"
    assert ip_calls[0].kwargs["assigned_object_id"] == 100
    assert ip_calls[0].kwargs["address"] == "10.0.0.5/32"
    # primary_ip4 = first IP id.
    assert vm_obj.primary_ip4 == 1001


def test_assign_vm_primary_ip4_reuses_existing_vminterface_by_name():
    # The VM already has an eth0 vminterface → reuse it (don't create a second),
    # refresh its MAC if MAC-less, and assign the IP to it.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    existing = _Iface(100, "eth0", mac_address=None)   # exists but MAC-less
    eng.nb.virtualization.vminterfaces.filter.return_value = [existing]
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=1001)

    eng._assign_vm_primary_ip4(vm_obj, {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]},
        tenant=None)

    eng.nb.virtualization.vminterfaces.create.assert_not_called()  # reused
    assert existing.mac_address == "aa:bb:cc:dd:ee:01"             # MAC refreshed
    existing.save.assert_called()
    ipk = eng.nb.ipam.ip_addresses.create.call_args.kwargs
    assert ipk["assigned_object_id"] == 100     # assigned to the reused vminterface
    assert vm_obj.primary_ip4 == 1001


def test_assign_vm_primary_ip4_backcompat_flat_ips():
    # Older pxmx agent that sent only a flat ``ips`` list (no interfaces) → one
    # eth0 vminterface holding those IPs. Keeps the legacy path working.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    eng.nb.virtualization.vminterfaces.filter.return_value = []
    eng.nb.virtualization.vminterfaces.create.return_value = _Iface(100, "eth0")
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=1001)

    eng._assign_vm_primary_ip4(vm_obj, {"ips": ["10.0.0.5"]}, tenant=None)

    vmi = eng.nb.virtualization.vminterfaces.create.call_args.kwargs
    assert vmi["name"] == "eth0"
    assert "mac_address" not in vmi          # no MAC known → MAC-less eth0
    assert eng.nb.ipam.ip_addresses.create.call_args.kwargs["address"] == "10.0.0.5/32"


# ── source-of-truth gating ───────────────────────────────────────────────────

def test_sync_vms_netbox_sot_only_adds_missing_no_overwrite():
    # source_of_truth="netbox" → NetBox owns VMs. An EXISTING VM is NOT
    # overwritten: name/cluster/status/vcpus/memory/tenant/proxmox_* stay as-is.
    # Only last_seen is refreshed (a staleness signal, not a truth field).
    eng = _engine()
    existing_row = {"id": 7, "custom_fields": {"proxmox_unique_id": "pxmx:100"}}
    eng._api_get_all = MagicMock(return_value=[existing_row])
    existing_vm = _Obj(id=7, custom_fields={"proxmox_unique_id": "pxmx:100",
                                             "proxmox_node": "pve1"})
    existing_vm.name = "original-name"
    eng.nb.virtualization.virtual_machines.get.return_value = existing_vm

    res = eng.sync_vms(
        vms=[{**_VM, "name": "vm-renamed", "node": "pve2"}],
        tenant_slug="lrb", replace=False, source_of_truth="netbox")

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    # No truth-field overwrites: name stays, proxmox_node untouched.
    assert existing_vm.name == "original-name"
    assert existing_vm.custom_fields.get("proxmox_node") == "pve1"
    # last_seen refreshed (the only write under netbox-SoT) + counted as pushed.
    assert existing_vm.custom_fields.get("last_seen") not in (None, "")
    existing_vm.save.assert_called()


def test_sync_vms_external_sot_overwrites_existing_vm():
    # source_of_truth="external" (default, Proxmox owns VMs) → overwrite as
    # before: name/status/vcpus + proxmox_* refreshed on the existing VM.
    eng = _engine()
    existing_row = {"id": 7, "custom_fields": {"proxmox_unique_id": "pxmx:100"}}
    eng._api_get_all = MagicMock(return_value=[existing_row])
    existing_vm = _Obj(id=7, custom_fields={"proxmox_unique_id": "pxmx:100"})
    eng.nb.virtualization.virtual_machines.get.return_value = existing_vm

    res = eng.sync_vms(
        vms=[{**_VM, "name": "vm-renamed"}],
        tenant_slug="lrb", replace=False, source_of_truth="external")

    assert res["status"] == "SUCCESS", res
    assert existing_vm.name == "vm-renamed"          # overwritten
    assert existing_vm.custom_fields.get("proxmox_node") == "pve1"  # linkage refreshed
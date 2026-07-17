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
    eng._assign_vm_primary_ip4 = MagicMock(return_value=(0, None))  # no-op (ips best-effort); returns (failures, first_err)
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
    # Update path (perf FIX C: ONE combined save for core + custom fields).
    # When the combined save 400s (cf unprovisioned on the deployed NetBox),
    # the core update is retried WITHOUT the cf patch — a cf 400 must NOT lose
    # the core-field update, same contract as the old two-save split.
    eng = _engine()
    existing_row = {"id": 7, "custom_fields": {"proxmox_unique_id": "pxmx:100"}}
    eng._api_get_all = MagicMock(return_value=[existing_row])
    existing_vm = _Obj(id=7, custom_fields={"proxmox_unique_id": "pxmx:100"})
    # 1st save = combined core+cf (raises: cf unprovisioned);
    # 2nd save = core-only retry (ok).
    existing_vm.save.side_effect = [
        Exception("does not exist for this object type"), None]
    eng.nb.virtualization.virtual_machines.get.return_value = existing_vm

    res = eng.sync_vms(
        vms=[{**_VM, "name": "vm-renamed"}], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0                 # cf failure is best-effort, not an error
    # Core rename landed via the core-only retry.
    assert existing_vm.name == "vm-renamed"
    assert existing_vm.save.call_count == 2   # combined (failed) + core-only retry
    # The unprovisioned cf patch was reverted so the retry couldn't re-send it.
    assert "proxmox_node" not in existing_vm.custom_fields


def test_sync_vms_update_single_combined_save_on_healthy_netbox():
    # perf FIX C: on a healthy NetBox the update path is ONE save carrying core
    # fields + custom fields (was two PATCHes per VM).
    eng = _engine()
    existing_row = {"id": 7, "custom_fields": {"proxmox_unique_id": "pxmx:100"}}
    eng._api_get_all = MagicMock(return_value=[existing_row])
    existing_vm = _Obj(id=7, custom_fields={"proxmox_unique_id": "pxmx:100"})
    eng.nb.virtualization.virtual_machines.get.return_value = existing_vm

    res = eng.sync_vms(
        vms=[{**_VM, "name": "vm-renamed"}], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1 and res["errors"] == 0
    assert existing_vm.name == "vm-renamed"
    assert existing_vm.custom_fields.get("proxmox_node") == "pve1"
    assert existing_vm.save.call_count == 1   # ONE combined PATCH


# ── Adopt-by-name self-heal: the "name must be unique per cluster" 400 ────────
#
# A NetBox VM occupies (name, cluster) but has no/mismatched proxmox_unique_id
# (a stale/manual record, or one whose uid changed after a node rename). The
# uid index misses it, so without a name+cluster fallback the create branch
# 400s. The fix resolves existing by uid first, then by (name, cluster) —
# adopting the record and stamping the incoming proxmox_unique_id.

def test_sync_vms_adopts_existing_vm_by_name_when_uid_missing():
    # Existing NetBox VM with the same name+cluster but NO proxmox_unique_id.
    existing_row = {"id": 7, "name": "vm100",
                    "cluster": {"id": 999, "name": "c1"}, "custom_fields": {}}
    eng = _engine()
    eng._api_get_all = MagicMock(return_value=[existing_row])
    existing_vm = _Obj(id=7, custom_fields={})
    eng.nb.virtualization.virtual_machines.get.return_value = existing_vm

    res = eng.sync_vms(vms=[_VM], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0
    # Adopted, NOT recreated — the 400 trap is avoided.
    eng.nb.virtualization.virtual_machines.create.assert_not_called()
    # The incoming proxmox_unique_id is stamped so future syncs match by uid.
    assert existing_vm.custom_fields.get("proxmox_unique_id") == "pxmx:100"
    assert existing_vm.name == "vm100"
    existing_vm.save.assert_called()   # core update + cf PATCH


def test_sync_vms_uid_match_takes_precedence_over_name_cluster():
    # Two rows: one matching by uid (id=7), one matching by name+cluster (id=8,
    # different uid). The uid match must win so the right record is updated.
    uid_row = {"id": 7, "name": "vm100",
               "cluster": {"id": 999, "name": "c1"},
               "custom_fields": {"proxmox_unique_id": "pxmx:100"}}
    name_row = {"id": 8, "name": "vm100",
                "cluster": {"id": 999, "name": "c1"},
                "custom_fields": {"proxmox_unique_id": "pxmx:other"}}
    eng = _engine()
    eng._api_get_all = MagicMock(return_value=[uid_row, name_row])
    uid_vm = _Obj(id=7, custom_fields={"proxmox_unique_id": "pxmx:100"})
    eng.nb.virtualization.virtual_machines.get.return_value = uid_vm

    res = eng.sync_vms(vms=[_VM], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    # The uid-matched row (id=7) was the one fetched/updated, not id=8.
    eng.nb.virtualization.virtual_machines.get.assert_called_once_with(7)
    eng.nb.virtualization.virtual_machines.create.assert_not_called()


def test_sync_vms_writes_proxmox_labels_from_tags():
    # The pxmx agent emits per-VM tags as a list; sync_vms joins them with ';'
    # (Proxmox's native separator) into the proxmox_labels custom field so the
    # VM's labels round-trip with the Proxmox GUI. Empty/blank tags are dropped.
    eng = _engine()
    vm_obj = _Obj(id=42, custom_fields={})
    eng.nb.virtualization.virtual_machines.create.return_value = vm_obj

    res = eng.sync_vms(
        vms=[{**_VM, "tags": ["prod", "web", "  ", ""]}],
        tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert vm_obj.custom_fields.get("proxmox_labels") == "prod;web"


def test_sync_vms_proxmox_labels_empty_when_no_tags():
    eng = _engine()
    vm_obj = _Obj(id=42, custom_fields={})
    eng.nb.virtualization.virtual_machines.create.return_value = vm_obj

    res = eng.sync_vms(vms=[_VM], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert vm_obj.custom_fields.get("proxmox_labels") == ""


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
    eng.nb.virtualization.interfaces.filter.return_value = []  # none exist
    eng.nb.virtualization.interfaces.create.side_effect = [_Iface(100, "eth0"),
                                                              _Iface(101, "eth1")]
    ip_objs = [_Obj(id=1001), _Obj(id=1002), _Obj(id=1003)]
    eng.nb.ipam.ip_addresses.create.side_effect = ip_objs

    vm = {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5", "10.0.0.6"]},
        {"name": "eth1", "mac": "aa:bb:cc:dd:ee:02", "ips": ["10.0.0.7"]},
    ]}
    eng._assign_vm_primary_ip4(vm_obj, vm, tenant=None)

    # Two vminterfaces created, each with its MAC.
    vmi_calls = eng.nb.virtualization.interfaces.create.call_args_list
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
    # primary_ip4 PATCHed via a targeted virtual_machines.update({id, primary_ip4})
    # — NOT a full obj.save() that would re-send custom_fields (which 400s on a
    # NetBox where the CFs aren't yet attached to virtualization.virtualmachine).
    eng.nb.virtualization.virtual_machines.update.assert_called_once_with(
        [{"id": 42, "primary_ip4": 1001}])
    vm_obj.save.assert_not_called()


def test_assign_vm_primary_ip4_reuses_existing_vminterface_by_name():
    # The VM already has an eth0 vminterface → reuse it (don't create a second),
    # refresh its MAC if MAC-less, and assign the IP to it.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    existing = _Iface(100, "eth0", mac_address=None)   # exists but MAC-less
    eng.nb.virtualization.interfaces.filter.return_value = [existing]
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=1001)

    eng._assign_vm_primary_ip4(vm_obj, {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]},
        tenant=None)

    eng.nb.virtualization.interfaces.create.assert_not_called()  # reused
    assert existing.mac_address == "aa:bb:cc:dd:ee:01"             # MAC refreshed
    existing.save.assert_called()
    ipk = eng.nb.ipam.ip_addresses.create.call_args.kwargs
    assert ipk["assigned_object_id"] == 100     # assigned to the reused vminterface
    eng.nb.virtualization.virtual_machines.update.assert_called_once_with(
        [{"id": 42, "primary_ip4": 1001}])


def test_assign_vm_primary_ip4_backcompat_flat_ips():
    # Older pxmx agent that sent only a flat ``ips`` list (no interfaces) → one
    # eth0 vminterface holding those IPs. Keeps the legacy path working.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    eng.nb.virtualization.interfaces.filter.return_value = []
    eng.nb.virtualization.interfaces.create.return_value = _Iface(100, "eth0")
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=1001)

    eng._assign_vm_primary_ip4(vm_obj, {"ips": ["10.0.0.5"]}, tenant=None)

    vmi = eng.nb.virtualization.interfaces.create.call_args.kwargs
    assert vmi["name"] == "eth0"
    assert "mac_address" not in vmi          # no MAC known → MAC-less eth0
    assert eng.nb.ipam.ip_addresses.create.call_args.kwargs["address"] == "10.0.0.5/32"


def test_assign_vm_primary_ip4_no_interfaces_no_ips_returns_zero_none():
    # A VM the agent reported with NO interfaces AND no flat ips (e.g. the leaked
    # pxmx-cs-svr-02 record, or a powered-off VM QGA couldn't introspect) must
    # return (0, None) — honoring the (build_failures, first_build_err) contract.
    # A bare ``return`` (None) made the caller's
    # ``ip_fail, ip_err = _assign_vm_primary_ip4(...)`` raise
    # "cannot unpack non-iterable NoneType", surfacing in sync_vms as
    # "upsert <uid>: cannot unpack non-iterable NoneType object" (17 errors/run).
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "phantom"
    failures, first_err = eng._assign_vm_primary_ip4(
        vm_obj, {"interfaces": [], "ips": []}, tenant=None)
    assert failures == 0
    assert first_err is None
    eng.nb.virtualization.virtual_machines.update.assert_not_called()


def test_assign_vm_primary_ip4_targeted_update_does_not_resend_custom_fields():
    # primary_ip4 is PATCHed via virtual_machines.update(id, {primary_ip4}) — a
    # targeted update that sends ONLY primary_ip4, never the custom_fields loaded
    # on the object. sync_vms sets the proxmox_*/last_seen CFs best-effort just
    # before this call and swallows the 400 when a CF isn't yet attached to
    # virtualization.virtualmachine, but the unattached CFs stay on the Python
    # object — a full obj.save() here would re-send them and 400 ("Custom field
    # 'last_seen' does not exist for this object type"), leaving the VM IP-less.
    # The targeted update can't 400 on custom_fields and can't wipe attached CFs.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={"proxmox_vmid": "100", "last_seen": "x"})
    vm_obj.name = "vm-with-cfs"
    eng.nb.virtualization.interfaces.filter.return_value = []
    eng.nb.virtualization.interfaces.create.return_value = _Iface(100, "eth0")
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=1001)

    eng._assign_vm_primary_ip4(vm_obj, {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]},
        tenant=None)

    # Only primary_ip4 is sent — no custom_fields key in the update payload,
    # and never a full obj.save() that would re-send the CFs.
    upd = eng.nb.virtualization.virtual_machines.update.call_args
    assert upd.args == ([{"id": 42, "primary_ip4": 1001}],)
    vm_obj.save.assert_not_called()


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


def test_sync_vms_netbox_sot_still_builds_vminterfaces_and_ips():
    # Regression: source_of_truth="netbox" used to ``continue`` BEFORE
    # ``_assign_vm_primary_ip4`` ran, so an existing VM in only-add-missing mode
    # NEVER got its vminterfaces/IPs built (IP-less with 0 errors). The IP data
    # is gathered, not a truth field, so only-add-missing must still ADD it.
    # Truth fields stay untouched; only last_seen + the IP build run.
    eng = _engine()
    existing_row = {"id": 7, "custom_fields": {"proxmox_unique_id": "pxmx:100"}}
    eng._api_get_all = MagicMock(return_value=[existing_row])
    existing_vm = _Obj(id=7, custom_fields={"proxmox_unique_id": "pxmx:100"})
    existing_vm.name = "original-name"
    eng.nb.virtualization.virtual_machines.get.return_value = existing_vm

    res = eng.sync_vms(
        vms=[{**_VM, "name": "vm-renamed", "interfaces": [
            {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]}],
        tenant_slug="lrb", replace=False, source_of_truth="netbox")

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0
    # Truth fields NOT overwritten (only-add-missing).
    assert existing_vm.name == "original-name"
    # ...but the IP build DID run (the fix) — _assign_vm_primary_ip4 was called.
    eng._assign_vm_primary_ip4.assert_called_once()
    called_vm = eng._assign_vm_primary_ip4.call_args.args[1]
    assert called_vm["interfaces"][0]["ips"] == ["10.0.0.5"]


def test_assign_vm_primary_ip4_surfaces_build_failures(caplog):
    # A real-world pynetbox failure (the IP already exists in IPAM on a
    # discovered dcim.device, so reuse/reassign raises) used to be swallowed at
    # DEBUG with nothing reported — the VM ended up IP-less with 0 errors.
    # Now the failure is counted + the first error returned + logged WARNING.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    eng.nb.virtualization.interfaces.filter.return_value = []
    eng.nb.virtualization.interfaces.create.return_value = _Iface(100, "eth0")
    eng.nb.ipam.ip_addresses.get.return_value = None        # no exact-prefix match
    eng.nb.ipam.ip_addresses.create.side_effect = Exception(
        "Duplicate IP address found in global table")       # create 400s
    eng.nb.ipam.ip_addresses.filter.return_value = []      # bare-IP fallback empty

    import logging
    caplog.set_level(logging.WARNING, logger="NetboxEngine")
    failures, first_err = eng._assign_vm_primary_ip4(vm_obj, {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]},
        tenant=None)

    assert failures == 1                       # the IP build failed → counted
    assert first_err is not None
    assert "10.0.0.5" in first_err
    assert vm_obj.primary_ip4 is None          # no IP landed → no primary_ip4
    # The first failure is a WARNING ([sync-error]) so it reaches the spoke log
    # + GET_ERROR_LOGS — the silent DEBUG swallow is gone.
    assert any("[sync-error]" in r.message and "10.0.0.5" in r.message
               for r in caplog.records)


def test_assign_vm_primary_ip4_success_returns_zero_failures():
    # Happy path: all vminterfaces + IPs built → returns (0, None).
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    eng.nb.virtualization.interfaces.filter.return_value = []
    eng.nb.virtualization.interfaces.create.return_value = _Iface(100, "eth0")
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=1001)

    failures, first_err = eng._assign_vm_primary_ip4(vm_obj, {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]},
        tenant=None)

    assert failures == 0
    assert first_err is None
    eng.nb.virtualization.virtual_machines.update.assert_called_once_with(
        [{"id": 42, "primary_ip4": 1001}])


# ── reuse/reassign: IP pre-exists in IPAM (e.g. on a discovered dcim.device) ──
#
# The most common real-world cause of "the IP record exists but isn't connected
# to the VM": sync_devices created the IP first on a dcim.interface (and set the
# device's primary_ip4 to it). sync_vms then reuses the global IP and must
# REASSIGN it to the VM's vminterface. The old _reassign_ip used ipobj.save()
# whose diff-detection can omit assigned_object_type (a ContentType fetched as a
# nested dict) from the PATCH → the save failed → swallowed at DEBUG → the VM's
# primary_ip4 ended up pointing at an IP still on the device. The fix uses an
# explicit ipam.ip_addresses.update(id, {...}) PATCH.

def test_assign_vm_primary_ip4_reassigns_existing_ip_to_vminterface_via_update():
    # The IP already exists globally (on a device) → reuse path must reassign it
    # to the VM's vminterface via ipam.ip_addresses.update, NOT create a dup.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    eng.nb.virtualization.interfaces.filter.return_value = []
    eng.nb.virtualization.interfaces.create.return_value = _Iface(100, "eth0")
    existing_ip = _Obj(id=901, custom_fields={})
    eng.nb.ipam.ip_addresses.get.return_value = existing_ip   # global duplicate

    failures, first_err = eng._assign_vm_primary_ip4(vm_obj, {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]},
        tenant=None)

    assert failures == 0
    assert first_err is None
    # No duplicate create — the existing record was reused + reassigned.
    eng.nb.ipam.ip_addresses.create.assert_not_called()
    # The reassign is an explicit PATCH with the vminterface content-type + the
    # new iface id (the reliable write that replaced ipobj.save()).
    eng.nb.ipam.ip_addresses.update.assert_called_once()
    upd_body = eng.nb.ipam.ip_addresses.update.call_args.args[0][0]
    assert upd_body["id"] == 901
    assert upd_body["assigned_object_type"] == "virtualization.vminterface"
    assert upd_body["assigned_object_id"] == 100
    # VM primary_ip4 now points at the (reassigned) existing IP — PATCHed via a
    # targeted virtual_machines.update that sends only {primary_ip4}.
    eng.nb.virtualization.virtual_machines.update.assert_called_once_with(
        [{"id": 42, "primary_ip4": 901}])


def test_assign_vm_primary_ip4_reassign_failure_is_surfaced(caplog):
    # The reuse path used to swallow a reassign failure at DEBUG (the VM went
    # IP-less with 0 errors). Now _reassign_ip logs WARNING [sync-error] + returns
    # False, _reuse_or_create_ip raises, and _assign_vm_primary_ip4 counts it.
    import logging
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    eng.nb.virtualization.interfaces.filter.return_value = []
    eng.nb.virtualization.interfaces.create.return_value = _Iface(100, "eth0")
    existing_ip = _Obj(id=901, custom_fields={})
    eng.nb.ipam.ip_addresses.get.return_value = existing_ip
    eng.nb.ipam.ip_addresses.update.side_effect = Exception("assigned_object_type is required")

    caplog.set_level(logging.WARNING, logger="NetboxEngine")
    failures, first_err = eng._assign_vm_primary_ip4(vm_obj, {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]},
        tenant=None)

    assert failures == 1
    assert first_err is not None and "10.0.0.5" in first_err
    assert vm_obj.primary_ip4 is None          # no IP landed → no primary_ip4
    eng.nb.ipam.ip_addresses.create.assert_not_called()
    # The reassign failure is a WARNING [sync-error] (reaches GET_ERROR_LOGS).
    assert any("[sync-error]" in r.message and "10.0.0.5" in r.message
               for r in caplog.records)


def test_assign_vm_primary_ip4_clears_stale_device_primary_ip():
    # IP is moved OFF a dcim.interface that was a device's primary_ip4 → the
    # device's primary_ip4 is cleared so it doesn't keep pointing at an IP now
    # on a VM. old_aot arrives as the nested-dict form pynetbox fetches.
    eng = _engine_real_assign()
    vm_obj = _Obj(id=42, custom_fields={})
    vm_obj.name = "vm100"
    eng.nb.virtualization.interfaces.filter.return_value = []
    eng.nb.virtualization.interfaces.create.return_value = _Iface(100, "eth0")
    existing_ip = _Obj(id=901, custom_fields={})
    # pynetbox fetches assigned_object_type as a nested dict {app_label, model}.
    existing_ip.assigned_object_type = {"app_label": "dcim", "model": "interface"}
    existing_ip.assigned_object_id = 500
    eng.nb.ipam.ip_addresses.get.return_value = existing_ip
    # The old dcim.interface belongs to device 7, whose primary_ip4 == this IP.
    eng.nb.dcim.interfaces.get.return_value = MagicMock(id=500, device={"id": 7})
    stale_dev = _Obj(id=7, custom_fields={})
    stale_dev.primary_ip4 = 901
    eng.nb.dcim.devices.get.return_value = stale_dev

    failures, _ = eng._assign_vm_primary_ip4(vm_obj, {"interfaces": [
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]},
        tenant=None)

    assert failures == 0
    # Reassigned to the vminterface...
    upd_body = eng.nb.ipam.ip_addresses.update.call_args.args[0][0]
    assert upd_body["assigned_object_type"] == "virtualization.vminterface"
    # ...and the stale device primary_ip4 cleared.
    assert stale_dev.primary_ip4 is None
    stale_dev.save.assert_called()


# ── Case-insensitive tenant resolution ────────────────────────────────────────
#
# A VM's Proxmox label (or a configured tenant_slug) can arrive in mixed case,
# and the label may be the tenant's display NAME rather than its slug. sync_vms
# builds a lower(slug) ∪ lower(name) → canonical-slug lookup once per batch and
# resolves the incoming tenant_slug through it, so "LRB" / "Lrb" / "LRB Labs"
# all land on tenant slug "lrb".

def _ci_engine(tenant_rows):
    """Engine whose _api_get_all returns tenant_rows for the tenancy/tenants
    path and [] (no existing VMs) otherwise; tenants.get returns a real-ish
    tenant object only for the canonical slug "lrb"."""
    eng = _engine()

    def fake_get_all(path, params=None, **kw):
        if "tenancy/tenants" in path:
            return tenant_rows
        return []
    eng._api_get_all = MagicMock(side_effect=fake_get_all)

    tenant_obj = MagicMock()
    tenant_obj.id = 11
    # tenants.get(slug="lrb") → the tenant; any other slug → None (not found).

    def tenants_get(slug=None, **kw):
        return tenant_obj if slug == "lrb" else None
    eng.nb.tenancy.tenants.get = MagicMock(side_effect=tenants_get)
    eng.nb.virtualization.virtual_machines.create.return_value = _Obj(id=42)
    return eng


def test_sync_vms_resolves_tenant_slug_case_insensitively():
    eng = _ci_engine([{"id": 11, "slug": "lrb", "name": "LRB Labs"}])
    # Mixed-case slug "LRB" must resolve to canonical "lrb".
    res = eng.sync_vms(vms=[{**_VM, "tenant_slug": "LRB"}],
                       tenant_slug="", replace=False)
    assert res["status"] == "SUCCESS", res
    eng.nb.tenancy.tenants.get.assert_called_with(slug="lrb")
    ck = eng.nb.virtualization.virtual_machines.create.call_args.kwargs
    assert ck["tenant"] == 11


def test_sync_vms_resolves_tenant_by_display_name_case_insensitive():
    eng = _ci_engine([{"id": 11, "slug": "lrb", "name": "LRB Labs"}])
    # The VM label is the tenant's display NAME in mixed case — must still
    # resolve to slug "lrb" via the lower(name) index.
    res = eng.sync_vms(vms=[{**_VM, "tenant_slug": "lrb labs"}],
                       tenant_slug="", replace=False)
    assert res["status"] == "SUCCESS", res
    eng.nb.tenancy.tenants.get.assert_called_with(slug="lrb")
    assert eng.nb.virtualization.virtual_machines.create.call_args.kwargs["tenant"] == 11


def test_sync_vms_slug_match_wins_over_name_collision():
    # Two tenants: slug "alpha" and a tenant whose NAME is "Alpha" (slug "a2").
    # An incoming "alpha" must resolve to slug "alpha" (the slug), not "a2".
    eng = _ci_engine([{"id": 11, "slug": "lrb", "name": "LRB Labs"}])

    def tenants_get(slug=None, **kw):
        return MagicMock(id=11) if slug == "lrb" else (
            MagicMock(id=22) if slug == "a2" else None)
    eng.nb.tenancy.tenants.get = MagicMock(side_effect=tenants_get)
    eng._api_get_all = MagicMock(side_effect=lambda path, params=None, **kw: [
        {"id": 11, "slug": "lrb", "name": "LRB Labs"},
        {"id": 22, "slug": "a2", "name": "lrb"},  # name collides with slug "lrb"
    ] if "tenancy/tenants" in path else [])

    res = eng.sync_vms(vms=[{**_VM, "tenant_slug": "LRB"}],
                       tenant_slug="", replace=False)
    assert res["status"] == "SUCCESS", res
    # slug.lower() "lrb" was inserted before name.lower() "lrb" → canonical "lrb"
    eng.nb.tenancy.tenants.get.assert_called_with(slug="lrb")

# ── perf FIX C: hydrate matched VMs from the listing (no per-VM re-GET) ───────

def test_sync_vms_update_hydrates_from_listed_row_no_reget():
    # The listing already returned the full serialized row — the update path
    # hydrates a pynetbox Record from it instead of re-GETting each VM by id.
    # (Rows without a 'url' key — like the minimal ones elsewhere in this file —
    # fall back to .get(), which is why those tests still see the mock.)
    eng = _engine()
    # pynetbox Record hydration reads api.base_url (a string on a real api);
    # give the MagicMock one so hydration succeeds like it does in production.
    eng.nb.base_url = "http://nb/api"
    existing_row = {"id": 7,
                    "url": "http://nb/api/virtualization/virtual-machines/7/",
                    "name": "vm100",
                    "custom_fields": {"proxmox_unique_id": "pxmx:100"}}
    eng._api_get_all = MagicMock(return_value=[existing_row])

    res = eng.sync_vms(vms=[_VM], tenant_slug="lrb", replace=False)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    eng.nb.virtualization.virtual_machines.get.assert_not_called()
    eng.nb.virtualization.virtual_machines.create.assert_not_called()

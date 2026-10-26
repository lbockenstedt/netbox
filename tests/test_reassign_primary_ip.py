"""_reassign_ip moving an IP that is still its old parent's primary IP.

NetBox rejects that move ("Cannot reassign IP address while it is designated
as the primary IP for the parent object"), so moving an IP onto a VM interface
must release the old device/VM's primary first, and put it back if the move
still fails.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402

VMIF = "virtualization.vminterface"


def _ip(aot="dcim.interface", iface_id=11):
    return SimpleNamespace(id=500, assigned_object_type=aot,
                           assigned_object_id=iface_id, tenant=None,
                           dns_name="", custom_fields={})


def _parent(name="old"):
    # primary_ip4 as pynetbox returns it: a nested Record, not an int.
    p = SimpleNamespace(id=70, name=name, primary_ip4=SimpleNamespace(id=500),
                        primary_ip6=None)
    p.save = MagicMock()
    return p


def _engine(log):
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.ipam.ip_addresses.update = MagicMock(
        side_effect=lambda rows: log.append(("update", rows[0]["assigned_object_id"])))
    return eng


def test_releases_device_primary_before_moving_ip_to_vm():
    log = []
    eng = _engine(log)
    dev = _parent("MIPBE-SVCS01")
    dev.save.side_effect = lambda: log.append(("save", dev.primary_ip4))
    eng.nb.dcim.interfaces.get.return_value = SimpleNamespace(device=SimpleNamespace(id=70))
    eng.nb.dcim.devices.get.return_value = dev

    assert eng._reassign_ip(_ip(), 1322, None, "", "", "vm-sync", "172.21.1.11/24", VMIF)
    assert log == [("save", None), ("update", 1322)]


def test_releases_stale_vm_primary_when_ip_moves_between_vms():
    eng = _engine([])
    vm = _parent("old-vm")
    eng.nb.virtualization.interfaces.get.return_value = SimpleNamespace(
        virtual_machine={"id": 70})
    eng.nb.virtualization.virtual_machines.get.return_value = vm

    assert eng._reassign_ip(_ip(VMIF, 900), 1322, None, "", "", "vm-sync", "a", VMIF)
    assert vm.primary_ip4 is None
    vm.save.assert_called_once()


def test_failed_move_restores_released_primary():
    eng = _engine([])
    eng.nb.ipam.ip_addresses.update = MagicMock(side_effect=RuntimeError("400"))
    dev = _parent()
    eng.nb.dcim.interfaces.get.return_value = SimpleNamespace(device=SimpleNamespace(id=70))
    eng.nb.dcim.devices.get.return_value = dev

    assert not eng._reassign_ip(_ip(), 1322, None, "", "", "vm-sync", "a", VMIF)
    assert dev.primary_ip4 == 500
    assert dev.save.call_count == 2


def test_device_sink_never_takes_an_ip_off_a_vm():
    log = []
    eng = _engine(log)
    assert not eng._reassign_ip(_ip(VMIF, 900), 12, None, "", "", "Kea", "a")
    assert log == []
    eng.nb.virtualization.virtual_machines.get.assert_not_called()


def test_device_sink_moving_between_devices_does_not_release_primaries():
    log = []
    eng = _engine(log)
    assert eng._reassign_ip(_ip(), 12, None, "", "", "sync_devices", "a")
    assert log == [("update", 12)]
    eng.nb.dcim.devices.get.assert_not_called()


def test_ip_that_is_not_the_old_parents_primary_is_left_alone():
    eng = _engine([])
    dev = _parent()
    dev.primary_ip4 = SimpleNamespace(id=999)
    eng.nb.dcim.interfaces.get.return_value = SimpleNamespace(device=SimpleNamespace(id=70))
    eng.nb.dcim.devices.get.return_value = dev

    assert eng._reassign_ip(_ip(), 1322, None, "", "", "vm-sync", "a", VMIF)
    dev.save.assert_not_called()

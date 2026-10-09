"""Idempotent write-back: an nw-discovered LLDP adjacency becomes a real
NetBox ``dcim.cable``, so the next scan's topology has a persistent record
in NetBox rather than only living in nw's in-memory LLDP cache.

Conservative by design (mirrors ``netbox_sync._cable_nic_to_port``): never
invents a device, only interfaces on devices that already exist; never
overwrites a human's own cable record on the same port.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


def _device(name, dev_id):
    dev = MagicMock()
    dev.id = dev_id
    dev.name = name
    return dev


def _iface(iface_id, connected_to=None):
    """``connected_to`` is (device_name, port_name) or None (unconnected)."""
    iface = MagicMock()
    iface.id = iface_id
    if connected_to is None:
        iface.connected_endpoint = None
    else:
        remote_dev = MagicMock()
        remote_dev.name = connected_to[0]
        endpoint = MagicMock()
        endpoint.device = remote_dev
        endpoint.name = connected_to[1]
        iface.connected_endpoint = endpoint
    return iface


def _engine(devices, interfaces, created_interfaces=None, fresh_interfaces=None):
    """``devices``: {name: MagicMock device}
    ``interfaces``: {(device_id, port_name): MagicMock iface or None}
    ``fresh_interfaces``: {iface_id: MagicMock iface} — what a bare
    ``interfaces.get(id)`` (the post-create connected-endpoint re-fetch)
    returns, keyed by the SAME iface object's id.
    """
    eng = NetboxEngine.__new__(NetboxEngine)
    eng.nb = MagicMock()
    eng.cable_calls = []
    eng.created_ifaces = []

    def devices_get(name=None, **kw):
        return devices.get(name)

    def interfaces_get(*args, **kwargs):
        if args:  # interfaces.get(id) — bare id lookup
            return (fresh_interfaces or {}).get(args[0])
        return interfaces.get((kwargs.get("device_id"), kwargs.get("name")))

    def interfaces_create(device=None, name=None, type=None):
        iface = _iface(iface_id=f"new-{device}-{name}")
        eng.created_ifaces.append((device, name))
        if created_interfaces is not None:
            created_interfaces.append(iface)
        return iface

    def cables_create(**kwargs):
        eng.cable_calls.append(kwargs)
        return MagicMock()

    eng.nb.dcim.devices.get.side_effect = devices_get
    eng.nb.dcim.interfaces.get.side_effect = interfaces_get
    eng.nb.dcim.interfaces.create.side_effect = interfaces_create
    eng.nb.dcim.cables.create.side_effect = cables_create
    return eng


def test_new_cable_is_created_between_existing_devices_and_ports():
    sw1 = _device("SW1", 1)
    sw2 = _device("SW2", 2)
    iface_a = _iface(101, connected_to=None)
    iface_b = _iface(102, connected_to=None)
    eng = _engine(
        devices={"SW1": sw1, "SW2": sw2},
        interfaces={(1, "1/1/1"): iface_a, (2, "1/1/48"): iface_b},
        fresh_interfaces={101: iface_a},
    )
    res = eng.sync_cable("SW1", "1/1/1", "SW2", "1/1/48")
    assert res["status"] == "SUCCESS"
    assert len(eng.cable_calls) == 1
    call = eng.cable_calls[0]
    assert call["a_terminations"] == [{"object_type": "dcim.interface", "object_id": 101}]
    assert call["b_terminations"] == [{"object_type": "dcim.interface", "object_id": 102}]
    assert call["status"] == "connected"


def test_missing_interface_is_created_not_errored():
    sw1 = _device("SW1", 1)
    sw2 = _device("SW2", 2)
    eng = _engine(
        devices={"SW1": sw1, "SW2": sw2},
        interfaces={},  # neither port exists yet
        fresh_interfaces={},
    )
    res = eng.sync_cable("SW1", "1/1/1", "SW2", "1/1/48")
    assert res["status"] == "SUCCESS"
    assert (1, "1/1/1") in eng.created_ifaces
    assert (2, "1/1/48") in eng.created_ifaces


def test_already_cabled_to_expected_far_end_is_unchanged():
    sw1 = _device("SW1", 1)
    sw2 = _device("SW2", 2)
    iface_a = _iface(101, connected_to=("SW2", "1/1/48"))
    eng = _engine(
        devices={"SW1": sw1, "SW2": sw2},
        interfaces={(1, "1/1/1"): iface_a, (2, "1/1/48"): _iface(102)},
        fresh_interfaces={101: iface_a},
    )
    res = eng.sync_cable("SW1", "1/1/1", "SW2", "1/1/48")
    assert res["status"] == "UNCHANGED"
    assert eng.cable_calls == []


def test_already_cabled_to_a_different_device_is_not_overwritten():
    sw1 = _device("SW1", 1)
    sw2 = _device("SW2", 2)
    iface_a = _iface(101, connected_to=("SW9", "9/9/9"))
    eng = _engine(
        devices={"SW1": sw1, "SW2": sw2},
        interfaces={(1, "1/1/1"): iface_a, (2, "1/1/48"): _iface(102)},
        fresh_interfaces={101: iface_a},
    )
    res = eng.sync_cable("SW1", "1/1/1", "SW2", "1/1/48")
    assert res["status"] == "SKIPPED"
    assert "SW9" in res["message"]
    assert eng.cable_calls == []


def test_unknown_device_is_skipped_not_invented():
    eng = _engine(devices={"SW1": _device("SW1", 1)}, interfaces={})
    res = eng.sync_cable("SW1", "1/1/1", "GHOST-SWITCH", "1/1/48")
    assert res["status"] == "SKIPPED"
    assert "GHOST-SWITCH" in res["message"]
    assert eng.cable_calls == []


def test_missing_port_arguments_is_an_error():
    eng = _engine(devices={}, interfaces={})
    res = eng.sync_cable("SW1", "", "SW2", "1/1/48")
    assert res["status"] == "ERROR"

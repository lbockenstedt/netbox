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
    # NetBox >= 3.3 shape: ``cable`` + ``link_peers`` (``connected_endpoint``
    # singular no longer exists, so it must not be what the check relies on).
    iface.connected_endpoint = None
    if connected_to is None:
        iface.cable = None
        iface.link_peers = []
    else:
        remote_dev = MagicMock()
        remote_dev.name = connected_to[0]
        endpoint = MagicMock()
        endpoint.device = remote_dev
        endpoint.name = connected_to[1]
        iface.cable = MagicMock(id=17)
        iface.link_peers = [endpoint]
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


def test_far_end_already_cabled_is_not_recreated():
    # The LLDP link arrives from the other switch's view: our A port looks
    # free but the B port already carries the cable back to A. Creating it
    # again 400s ("Duplicate termination found"), so it must read UNCHANGED.
    sw1 = _device("SW1", 1)
    sw2 = _device("SW2", 2)
    iface_a = _iface(101, connected_to=None)
    iface_b = _iface(102, connected_to=("SW1", "1/1/1"))
    eng = _engine(
        devices={"SW1": sw1, "SW2": sw2},
        interfaces={(1, "1/1/1"): iface_a, (2, "1/1/48"): iface_b},
        fresh_interfaces={101: iface_a, 102: iface_b},
    )
    res = eng.sync_cable("SW1", "1/1/1", "SW2", "1/1/48")
    assert res["status"] == "UNCHANGED"
    assert eng.cable_calls == []


def test_cable_without_peer_detail_still_counts_as_cabled():
    # A cabled interface whose peers weren't expanded must not be re-cabled.
    iface = MagicMock()
    iface.cable = MagicMock(id=5)
    iface.link_peers = []
    iface.connected_endpoints = None
    iface.connected_endpoint = None
    assert NetboxEngine._iface_cable_peer(iface) == ("(unknown)", "")
    iface.cable = None
    assert NetboxEngine._iface_cable_peer(iface) is None


def test_batch_cache_miss_checks_live_port_before_creating():
    """A port added after the batch listing is reused, not duplicate-created."""
    sw1, sw2 = _device("SW1", 1), _device("SW2", 2)
    iface_a = _iface(101, connected_to=("SW2", "1/1/48"))
    iface_b = _iface(102)
    eng = _engine(devices={}, interfaces={(1, "1/1/1"): iface_a})
    index = {1: {}, 2: {"1/1/48": iface_b}}

    for _ in range(2):
        res = eng._cable_devices(sw1, "1/1/1", sw2, "1/1/48", port_index=index)
        assert res["status"] == "UNCHANGED", res

    assert index[1]["1/1/1"] is iface_a
    eng.nb.dcim.interfaces.get.assert_called_once_with(device_id=1, name="1/1/1")
    eng.nb.dcim.interfaces.filter.assert_not_called()
    eng.nb.dcim.interfaces.create.assert_not_called()
    eng.nb.dcim.cables.create.assert_not_called()


def test_created_port_is_cached_when_far_end_cable_prevents_connection():
    """Skipping a cable must not leave the newly created port out of the index."""
    sw1, sw2 = _device("SW1", 1), _device("SW2", 2)
    eng = _engine(devices={}, interfaces={})
    index = {1: {}, 2: {"1/1/48": _iface(102, connected_to=("SW9", "9/9/9"))}}

    for _ in range(2):
        res = eng._cable_devices(sw1, "1/1/1", sw2, "1/1/48", port_index=index)
        assert res["status"] == "SKIPPED", res

    eng.nb.dcim.interfaces.create.assert_called_once_with(
        device=1, name="1/1/1", type="other")
    eng.nb.dcim.interfaces.get.assert_called_once_with(device_id=1, name="1/1/1")
    eng.nb.dcim.interfaces.filter.assert_not_called()
    eng.nb.dcim.cables.create.assert_not_called()


def test_interface_create_race_reuses_winner_and_preserves_its_cable():
    """A concurrent creator's port is re-read, including its cable ownership."""
    sw1, sw2 = _device("SW1", 1), _device("SW2", 2)
    interfaces = {(2, "1/1/48"): _iface(102)}
    eng = _engine(devices={"SW1": sw1, "SW2": sw2}, interfaces=interfaces)

    def concurrent_create(**kwargs):
        """Simulate another sync creating and cabling the port first."""
        interfaces[(kwargs["device"], kwargs["name"])] = _iface(
            101, connected_to=("SW9", "9/9/9"))
        raise RuntimeError("{'__all__': ['Interface with this Device and Name already exists.']}")

    eng.nb.dcim.interfaces.create.side_effect = concurrent_create
    for _ in range(2):
        res = eng.sync_cable("SW1", "1/1/1", "SW2", "1/1/48")
        assert res["status"] == "SKIPPED", res
        assert "SW9" in res["message"]

    eng.nb.dcim.interfaces.create.assert_called_once()
    eng.nb.dcim.cables.create.assert_not_called()


def test_interface_create_failures_without_a_reusable_port_remain_errors():
    """Neither unrelated failures nor an unresolved uniqueness error are hidden."""
    for message in (
        "permission denied",
        "Interface with this Device and Name already exists.",
    ):
        eng = _engine(
            devices={"SW1": _device("SW1", 1), "SW2": _device("SW2", 2)},
            interfaces={},
        )
        eng.nb.dcim.interfaces.create.side_effect = RuntimeError(message)
        res = eng.sync_cable("SW1", "1/1/1", "SW2", "1/1/48")
        assert res["status"] == "ERROR"
        assert message in res["message"]
        eng.nb.dcim.interfaces.create.assert_called_once()
        eng.nb.dcim.cables.create.assert_not_called()


def test_batch_lists_each_devices_ports_once_and_never_rereads():
    # Live load: an LLDP push of ~800 links did a name lookup per port plus a
    # re-GET of each interface. A batch lists each device's ports once.
    sw1, sw2 = _device("SW1", 1), _device("SW2", 2)
    ports = {1: [], 2: []}
    for p in range(1, 4):
        a = _iface(100 + p, connected_to=("SW2", f"1/1/{p}"))
        a.name = f"1/1/{p}"
        b = _iface(200 + p, connected_to=("SW1", f"1/1/{p}"))
        b.name = f"1/1/{p}"
        ports[1].append(a)
        ports[2].append(b)
    eng = _engine(devices={}, interfaces={})
    eng.nb.dcim.interfaces.filter.side_effect = lambda device_id: list(ports[device_id])
    index = {}
    for p in range(1, 4):
        res = eng._cable_devices(sw1, f"1/1/{p}", sw2, f"1/1/{p}", port_index=index)
        assert res["status"] == "UNCHANGED"
    assert eng.nb.dcim.interfaces.filter.call_count == 2
    eng.nb.dcim.interfaces.get.assert_not_called()


def test_batch_relists_ports_after_creating_a_cable():
    # A cable just created must be seen by the next link on the same ports,
    # or the batch would try to create it twice.
    sw1, sw2 = _device("SW1", 1), _device("SW2", 2)
    state = {"cabled": False}

    def listing(device_id):
        peer = ("SW2", "1/1/48") if device_id == 1 else ("SW1", "1/1/1")
        iface = _iface(100 + device_id, connected_to=peer if state["cabled"] else None)
        iface.name = "1/1/1" if device_id == 1 else "1/1/48"
        return [iface]

    eng = _engine(devices={}, interfaces={})
    eng.nb.dcim.interfaces.filter.side_effect = listing
    eng.nb.dcim.cables.create.side_effect = lambda **kw: state.update(cabled=True)
    index = {}
    assert eng._cable_devices(sw1, "1/1/1", sw2, "1/1/48", port_index=index)["status"] == "SUCCESS"
    assert eng._cable_devices(sw2, "1/1/48", sw1, "1/1/1", port_index=index)["status"] == "UNCHANGED"

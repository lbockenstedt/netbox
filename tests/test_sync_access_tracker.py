"""Tests for NetboxEngine.sync_access_tracker (NETBOX_SYNC_ACCESS_TRACKER handler).

Realtime NAC→IPAM reverse sync: ClearPass Access Tracker sessions → NetBox DCIM,
only-add-missing, MAC-first. Self-contained harness mirroring test_sync_devices
(constructs the engine without a live NetBox; _ensure_custom_fields no-ops because
the fake cf_api.all() isn't iterable and the ensure swallows that).
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Iface:
    """Minimal interface stand-in (needs .id; optionally a connected_endpoint)."""
    def __init__(self, id=100, connected_endpoint=None):
        self.id = id
        self.connected_endpoint = connected_endpoint


class _Obj:
    """Minimal pynetbox-record stand-in: settable custom_fields/primary_ip4,
    save/delete mocks."""
    def __init__(self, id=1, custom_fields=None, name=None):
        self.id = id
        self.name = name
        self.custom_fields = dict(custom_fields or {})
        self.primary_ip4 = None
        self.save = MagicMock()
        self.delete = MagicMock()


def _engine_with(existing_rows, tenant_obj=None):
    """Engine whose nb is a MagicMock; _api_get_all returns existing device rows;
    _api_get (prefix lookup) returns empty (→ /32 masks)."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.tenancy.tenants.get.return_value = tenant_obj
    eng._api_get_all = MagicMock(return_value=existing_rows)
    eng._api_get = MagicMock(return_value={"results": []})
    return eng


def test_sync_access_tracker_skips_existing_mac_no_create_no_delete():
    # MAC already in NetBox (on a device we tagged) → only-add-missing: skip the
    # create, best-effort refresh last_seen/switch_ip/switch_port, no delete.
    row = {"id": 77, "name": "device-aabbccddeeff",
           "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {"mac_address": "aa:bb:cc:dd:ee:ff",
                             "discovered_from": "cppm-access-tracker"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    refresh_dev = _Obj(id=77, custom_fields={"mac_address": "aa:bb:cc:dd:ee:ff",
                                             "discovered_from": "cppm-access-tracker"})
    eng.nb.dcim.devices.get.return_value = refresh_dev

    res = eng.sync_access_tracker(
        sessions=[{"mac": "AA:BB:CC:DD:EE:FF", "ip": "10.0.0.5",
                   "nas_ip": "10.0.0.254", "nas_port": "Ethernet1/0/12",
                   "nas_name": "sw-core", "username": "alice",
                   "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="lrb", defaults={})

    assert res["status"] == "SUCCESS"
    assert res["pushed"] == 0
    assert res["skipped"] == 1
    assert res["deleted"] == 0
    eng.nb.dcim.devices.create.assert_not_called()  # no duplicate
    # Best-effort topology/last_seen refresh on the owned device.
    assert refresh_dev.custom_fields.get("last_seen") == "2026-06-30T10:00:00"
    assert refresh_dev.custom_fields.get("switch_ip") == "10.0.0.254"
    assert refresh_dev.custom_fields.get("switch_port") == "Ethernet1/0/12"
    refresh_dev.save.assert_called()


def test_sync_access_tracker_skips_existing_mac_does_not_touch_other_source():
    # MAC exists on a device we DON'T own (discovered_from=opnsense, not ours) →
    # skip the create AND do not refresh it (never touch another source's record).
    row = {"id": 88, "name": "ks205",
           "primary_ip4": {"id": 902, "address": "10.0.0.5/24"},
           "custom_fields": {"mac_address": "aa:bb:cc:dd:ee:ff",
                             "discovered_from": "opnsense"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    other_dev = _Obj(id=88, custom_fields={"mac_address": "aa:bb:cc:dd:ee:ff",
                                           "discovered_from": "opnsense"})
    eng.nb.dcim.devices.get.return_value = other_dev

    res = eng.sync_access_tracker(
        sessions=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.5",
                   "nas_ip": "10.0.0.254", "nas_port": "Ethernet1",
                   "username": "alice", "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="lrb", defaults={})

    assert res["status"] == "SUCCESS"
    assert res["pushed"] == 0
    assert res["skipped"] == 1
    eng.nb.dcim.devices.create.assert_not_called()
    # Not our device → no refresh (no last_seen write, no save).
    assert "last_seen" not in other_dev.custom_fields
    other_dev.save.assert_not_called()


def test_sync_access_tracker_creates_missing_endpoint_with_tag_and_nic_ip():
    # MAC not in NetBox → create device tagged cppm-access-tracker + MAC/switch
    # custom fields + NIC interface (native MAC) + framed IP + primary_ip4.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = dev
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_access_tracker(
        sessions=[{"mac": "AA-BB-CC-DD-EE-FF", "ip": "10.0.0.5",
                   "nas_ip": "", "nas_port": "", "nas_name": "",
                   "username": "alice", "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="lrb", defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    # Device created with tenant + active + name from username.
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck["status"] == "active"
    assert ck["tenant"] == 1
    assert ck["name"] == "alice"
    # Ownership tag + topology custom fields on the device.
    assert dev.custom_fields.get("discovered_from") == "cppm-access-tracker"
    assert dev.custom_fields.get("mac_address") == "aa:bb:cc:dd:ee:ff"
    assert dev.custom_fields.get("last_seen") == "2026-06-30T10:00:00"
    dev.save.assert_called()
    # NIC interface created with the native MAC.
    ik = eng.nb.dcim.interfaces.create.call_args.kwargs
    assert ik["device"] == 42
    assert ik["name"] == "eth0"
    assert ik["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert ik["type"] == "other"
    # Framed IP on the NIC + dns_name=username + primary_ip4 set.
    ipk = eng.nb.ipam.ip_addresses.create.call_args.kwargs
    assert ipk["address"] == "10.0.0.5/32"  # no containing prefix → /32
    assert ipk["assigned_object_type"] == "dcim.interface"
    assert ipk["assigned_object_id"] == 100
    assert ipk["dns_name"] == "alice"
    assert dev.primary_ip4 == 555


def test_sync_access_tracker_builds_switch_topology_and_cable():
    # Missing MAC + a NAS IP/port → endpoint device + switch device (by NAS IP)
    # + port interface on the switch + one cable NIC↔port.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    eng.nb.dcim.devices.create.side_effect = [_Obj(id=42), _Obj(id=50)]   # endpoint, switch
    eng.nb.dcim.interfaces.create.side_effect = [_Iface(100), _Iface(200), _Iface(300)]
    eng.nb.ipam.ip_addresses.create.side_effect = [_Obj(id=555), _Obj(id=556)]
    eng.nb.dcim.interfaces.get.return_value = None   # port not found → create; nic not cabled → cable
    eng.nb.dcim.cables.create.return_value = _Obj(id=7)

    res = eng.sync_access_tracker(
        sessions=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.5",
                   "nas_ip": "10.0.0.254", "nas_port": "Ethernet1/0/12",
                   "nas_name": "sw-core", "username": "alice",
                   "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="lrb", defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    # Two devices created: the endpoint + the switch (named from nas_name).
    created = [c.kwargs["name"] for c in eng.nb.dcim.devices.create.call_args_list]
    assert "alice" in created
    assert "sw-core" in created
    sw_call = next(c for c in eng.nb.dcim.devices.create.call_args_list
                   if c.kwargs["name"] == "sw-core")
    assert sw_call.kwargs["role"] is not None
    # One cable, endpoint NIC (100) ↔ switch port (300).
    eng.nb.dcim.cables.create.assert_called_once()
    ck = eng.nb.dcim.cables.create.call_args.kwargs
    a = ck["a_terminations"][0]
    b = ck["b_terminations"][0]
    assert a["object_type"] == "dcim.interface" and a["object_id"] == 100
    assert b["object_type"] == "dcim.interface" and b["object_id"] == 300
    assert ck["status"] == "connected"


def test_sync_access_tracker_cable_not_recreated_when_already_connected():
    # The NIC already has a connected_endpoint → don't create a second cable.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    eng.nb.dcim.devices.create.side_effect = [_Obj(id=42), _Obj(id=50)]
    eng.nb.dcim.interfaces.create.side_effect = [_Iface(100), _Iface(200)]  # NIC, switch mgmt
    eng.nb.ipam.ip_addresses.create.side_effect = [_Obj(id=555), _Obj(id=556)]
    # interfaces.get returns a connected iface for BOTH the port-find (port
    # exists → reuse) and the nic connected_endpoint check (already cabled).
    eng.nb.dcim.interfaces.get.return_value = _Iface(id=300,
                                                     connected_endpoint={"id": 999})

    res = eng.sync_access_tracker(
        sessions=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.5",
                   "nas_ip": "10.0.0.254", "nas_port": "Ethernet1/0/12",
                   "nas_name": "sw-core", "username": "alice",
                   "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="lrb", defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    eng.nb.dcim.cables.create.assert_not_called()  # idempotent — no second cable


def test_sync_access_tracker_cable_failure_falls_back_to_custom_fields():
    # cables.create raises (pynetbox/NetBox version mismatch on terminations) →
    # WARNING + custom-field fallback. The device + IP + MAC + switch_ip/
    # switch_port custom fields are already written, so the sync still succeeds.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    eng.nb.dcim.devices.create.side_effect = [dev, _Obj(id=50)]
    eng.nb.dcim.interfaces.create.side_effect = [_Iface(100), _Iface(200), _Iface(300)]
    eng.nb.ipam.ip_addresses.create.side_effect = [_Obj(id=555), _Obj(id=556)]
    eng.nb.dcim.interfaces.get.return_value = None
    eng.nb.dcim.cables.create.side_effect = Exception("unsupported termination fields")

    res = eng.sync_access_tracker(
        sessions=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.5",
                   "nas_ip": "10.0.0.254", "nas_port": "Ethernet1/0/12",
                   "nas_name": "sw-core", "username": "alice",
                   "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="lrb", defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0          # cable failure is best-effort, not a sync error
    # Custom-field topology record still written on the endpoint device.
    assert dev.custom_fields.get("switch_ip") == "10.0.0.254"
    assert dev.custom_fields.get("switch_port") == "Ethernet1/0/12"


def test_sync_access_tracker_intra_batch_duplicate_usernames_get_unique_names():
    # Two sessions, same username, distinct MACs → first keeps the username,
    # the rest get a -<mac[-4:]> suffix (no (name, site, tenant) 400).
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    eng.nb.dcim.devices.create.side_effect = [_Obj(id=42), _Obj(id=43)]
    eng.nb.dcim.interfaces.create.side_effect = [_Iface(100), _Iface(101)]
    eng.nb.ipam.ip_addresses.create.side_effect = [_Obj(id=555), _Obj(id=556)]

    res = eng.sync_access_tracker(
        sessions=[
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "10.0.0.5", "username": "alice",
             "nas_ip": "", "start_time": "2026-06-30T10:00:00"},
            {"mac": "aa:bb:cc:dd:ee:02", "ip": "10.0.0.6", "username": "alice",
             "nas_ip": "", "start_time": "2026-06-30T10:00:30"},
        ],
        tenant_slug="lrb", defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 2
    assert res["errors"] == 0
    names = [c.kwargs["name"] for c in eng.nb.dcim.devices.create.call_args_list]
    assert len(names) == len(set(names))   # all unique → no constraint 400
    assert names[0] == "alice"
    assert names[1].startswith("alice-") and names[1].endswith("ee02")


def test_sync_access_tracker_refuses_unknown_tenant():
    eng = _engine_with(existing_rows=[], tenant_obj=None)
    res = eng.sync_access_tracker(
        sessions=[{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.5", "username": "a"}],
        tenant_slug="ghost", defaults={})
    assert res["status"] == "ERROR"
    assert "ghost" in res["message"]
    eng.nb.dcim.devices.create.assert_not_called()


def test_sync_access_tracker_drops_macless_session():
    # A session with no MAC can't be matched/created → skipped, not an error.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    res = eng.sync_access_tracker(
        sessions=[{"mac": "", "ip": "10.0.0.5", "username": "a"}],
        tenant_slug="lrb", defaults={})
    assert res["status"] == "SUCCESS"
    assert res["pushed"] == 0
    assert res["skipped"] == 1
    eng.nb.dcim.devices.create.assert_not_called()
"""Tests for NetboxEngine.sync_nw_device — the POLL NOW inventory sink.

sync_nw_device upserts ONE polled network device (switch/gateway) as a
dcim.device with dcim.interfaces + per-interface IPs. These tests pin: a new
device is created + interfaces + IP + primary_ip4 from the mgmt address;
missing create-defaults ERROR (named); an existing device matched by
nw_device_id is updated in place (no recreate); replace-delete only ever
removes nw-managed interfaces absent from the incoming set (never manual ones).

Self-contained: fakes engine.nb + the helpers sync_nw_device calls, mirroring
test_sync_vms.py's stand-in style.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Obj:
    """Minimal pynetbox-record stand-in: settable fields + save/delete."""
    _next = 1

    def __init__(self, id=None, name="", custom_fields=None, **kw):
        self.id = id if id is not None else _Obj._next
        _Obj._next += 1
        self.name = name
        self.custom_fields = dict(custom_fields or {})
        self.primary_ip4 = None
        self.status = kw.get("status")
        self.mac_address = kw.get("mac_address", "")
        self.speed = kw.get("speed", 0)
        self.save = MagicMock()
        self.delete = MagicMock()

    def __repr__(self):
        return f"_Obj(id={self.id}, name={self.name!r})"


def _engine(existing_devices=None, existing_ifaces=None, tenant_rows=None):
    """Faked engine: _api_get_all returns existing_devices for dcim/devices,
    tenant_rows for tenancy/tenants, else []; _api_get returns no prefixes
    (→ /32 mask). nb sub-APIs are MagicMocks with targeted returns."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng._ensure_custom_fields = MagicMock()
    eng._journal = MagicMock()

    def fake_get_all(path, params=None, **kw):
        if "dcim/devices" in path:
            return list(existing_devices or [])
        if "tenancy/tenants" in path:
            return list(tenant_rows or [])
        return []
    eng._api_get_all = MagicMock(side_effect=fake_get_all)
    eng._api_get = MagicMock(return_value={"results": []})  # no containing prefix

    # device type/role/site lookups → tiny stand-ins with .id
    eng.nb.dcim.device_types.get = MagicMock(return_value=MagicMock(id=301))
    eng.nb.dcim.device_roles.get = MagicMock(return_value=MagicMock(id=401))
    eng.nb.dcim.sites.get = MagicMock(return_value=MagicMock(id=501))
    # devices.get → the existing device stand-in (tests set per-call)
    eng.nb.dcim.devices.get = MagicMock(return_value=None)
    # interfaces.filter → the existing interface stand-ins for the device
    eng.nb.dcim.interfaces.filter = MagicMock(return_value=list(existing_ifaces or []))
    # IP reuse: no existing → create path
    eng.nb.ipam.ip_addresses.get = MagicMock(return_value=None)
    eng.nb.ipam.ip_addresses.create = MagicMock(return_value=_Obj(id=9000))
    return eng


_DEFAULTS = {"device_type": "switch", "role": "switch", "site": "site1"}
_DEV = {"id": "nw-1", "name": "sw1", "address": "10.0.0.1",
        "object_type": "aos_switch"}


def test_sync_nw_device_creates_device_interfaces_and_primary_ip():
    eng = _engine()  # no existing devices, no existing ifaces
    dev_obj = _Obj(id=100, name="sw1")
    eng.nb.dcim.devices.create = MagicMock(return_value=dev_obj)
    iface_objs = [_Obj(name="vlan1"), _Obj(name="trunk1")]
    eng.nb.dcim.interfaces.create = MagicMock(side_effect=iface_objs)

    ifaces = [
        {"name": "vlan1", "ip": "10.0.0.1", "mac": "aa:bb:cc:dd:ee:01",
         "status": "up", "speed": 1000000000},
        {"name": "trunk1", "status": "down"},
    ]
    res = eng.sync_nw_device(device=_DEV, interfaces=ifaces,
                             tenant_slug="", defaults=_DEFAULTS)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 2
    assert res["errors"] == 0
    assert res["device_id"] == 100
    eng.nb.dcim.devices.create.assert_called_once()
    assert eng.nb.dcim.interfaces.create.call_count == 2
    # primary_ip4 set from the management-address interface IP.
    assert dev_obj.primary_ip4 == 9000


def test_sync_nw_device_missing_defaults_errors_named():
    eng = _engine()
    res = eng.sync_nw_device(device=_DEV, interfaces=[{"name": "vlan1"}],
                             tenant_slug="", defaults={})
    assert res["status"] == "ERROR"
    assert "device_type" in res["message"]
    assert "role" in res["message"]
    assert "site" in res["message"]
    eng.nb.dcim.devices.create.assert_not_called()


def test_sync_nw_device_updates_existing_in_place_no_recreate():
    # Existing device matched by nw_device_id cf; one existing nw-managed iface
    # named vlan1 (in incoming), one stale nw-managed iface oldport (NOT in
    # incoming), one manual iface manual (NOT nw-managed, NOT in incoming).
    existing_row = {"id": 7, "name": "sw1",
                    "custom_fields": {"nw_device_id": "nw-1",
                                      "discovered_from": "Network Devices"}}
    eng = _engine(existing_devices=[existing_row])
    dev_obj = _Obj(id=7, name="sw1", custom_fields={"nw_device_id": "nw-1"})
    eng.nb.dcim.devices.get = MagicMock(return_value=dev_obj)
    vlan1 = _Obj(name="vlan1", custom_fields={"nw_managed": "true"},
                 status="planned", mac_address="")
    oldport = _Obj(name="oldport", custom_fields={"nw_managed": "true"})
    manual = _Obj(name="manual", custom_fields={})  # NOT nw-managed
    eng.nb.dcim.interfaces.filter = MagicMock(return_value=[vlan1, oldport, manual])

    res = eng.sync_nw_device(device=_DEV,
                             interfaces=[{"name": "vlan1", "ip": "10.0.0.1",
                                          "mac": "aa:bb:cc:dd:ee:01",
                                          "status": "up"}],
                             tenant_slug="", defaults=_DEFAULTS)

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1            # vlan1 upserted (matched, updated)
    assert res["deleted"] == 1           # oldport (nw-managed, stale) deleted
    eng.nb.dcim.devices.create.assert_not_called()       # existing → no recreate
    eng.nb.dcim.interfaces.create.assert_not_called()    # vlan1 matched by name
    # vlan1 was updated (status → active, mac stamped) + saved.
    assert vlan1.status == "active"
    assert vlan1.mac_address == "aa:bb:cc:dd:ee:01"
    vlan1.save.assert_called()
    # oldport (nw-managed, stale) deleted; manual (not nw-managed) left alone.
    oldport.delete.assert_called_once()
    manual.delete.assert_not_called()


def test_sync_nw_device_primary_ip_not_overwritten_when_already_set():
    # Existing device already has a primary_ip4 — the mgmt-IP interface must NOT
    # clobber it (only sets when primary_ip4 is unset).
    existing_row = {"id": 7, "name": "sw1",
                    "custom_fields": {"nw_device_id": "nw-1"}}
    eng = _engine(existing_devices=[existing_row])
    dev_obj = _Obj(id=7, name="sw1", custom_fields={"nw_device_id": "nw-1"})
    dev_obj.primary_ip4 = 5555  # already set
    eng.nb.dcim.devices.get = MagicMock(return_value=dev_obj)
    eng.nb.dcim.interfaces.filter = MagicMock(return_value=[])
    eng.nb.dcim.interfaces.create = MagicMock(return_value=_Obj(name="vlan1"))

    res = eng.sync_nw_device(device=_DEV,
                             interfaces=[{"name": "vlan1", "ip": "10.0.0.1",
                                          "status": "up"}],
                             tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    assert dev_obj.primary_ip4 == 5555  # unchanged
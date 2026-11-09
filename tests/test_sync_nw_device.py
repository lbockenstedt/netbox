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
    eng.nb.dcim.interfaces.get = MagicMock(return_value=None)
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


def test_sync_nw_device_adopts_interface_the_listing_missed():
    # NetBox already has vlan1 on the device but the listing came back empty:
    # update it in place instead of a create that 400s as a duplicate.
    existing_row = {"id": 7, "name": "sw1",
                    "custom_fields": {"nw_device_id": "nw-1"}}
    eng = _engine(existing_devices=[existing_row])
    eng.nb.dcim.devices.get = MagicMock(return_value=_Obj(id=7, name="sw1"))
    vlan1 = _Obj(name="vlan1", status="planned")
    eng.nb.dcim.interfaces.get = MagicMock(return_value=vlan1)

    res = eng.sync_nw_device(device=_DEV,
                             interfaces=[{"name": "vlan1", "status": "up"}],
                             tenant_slug="", defaults=_DEFAULTS)

    assert res["errors"] == 0, res
    eng.nb.dcim.interfaces.create.assert_not_called()
    eng.nb.dcim.interfaces.get.assert_called_with(device_id=7, name="vlan1")
    assert vlan1.status == "active"


def test_sync_nw_device_missing_defaults_errors_named():
    eng = _engine()
    unidentified = dict(_DEV, object_type="")
    res = eng.sync_nw_device(device=unidentified, interfaces=[{"name": "vlan1"}],
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
    # Index by device *id* — NetBox's `device` filter matches the name, so
    # filter(device=<id>) returns nothing and every iface re-creates (400).
    eng.nb.dcim.interfaces.filter.assert_called_with(device_id=7)
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

def test_sync_nw_device_matches_existing_by_serial_no_duplicate():
    # A device already in NetBox by SERIAL (created by another feeder, e.g.
    # console) but with a DIFFERENT/absent nw_device_id must be updated in
    # place — the canonical ladder tries serial before nw_device_id/name, so
    # the switch is NOT duplicated under an nw_device_id key.
    existing_row = {"id": 55, "name": "console-sw1",
                    "serial": "SN-ABC-123",
                    "custom_fields": {"discovered_from": "Console"}}
    eng = _engine(existing_devices=[existing_row])
    dev_obj = _Obj(id=55, name="console-sw1", custom_fields={})
    dev_obj.serial = "SN-ABC-123"
    eng.nb.dcim.devices.get = MagicMock(return_value=dev_obj)
    eng.nb.dcim.interfaces.filter = MagicMock(return_value=[])

    dev = {"id": "nw-9", "name": "sw1", "address": "10.0.0.1",
           "serial": "SN-ABC-123", "object_type": "aos_switch"}
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="",
                             defaults=_DEFAULTS)

    assert res["status"] == "SUCCESS", res
    eng.nb.dcim.devices.create.assert_not_called()   # matched by serial → no dup
    assert res["device_id"] == 55


def test_sync_nw_device_writes_serial_on_create():
    # A polled device carrying a serial stamps the native dcim.device.serial on
    # create so a later feeder can reconcile by serial.
    eng = _engine()
    dev_obj = _Obj(id=101, name="sw2")
    eng.nb.dcim.devices.create = MagicMock(return_value=dev_obj)
    eng.nb.dcim.interfaces.create = MagicMock(return_value=_Obj(name="vlan1"))

    dev = {"id": "nw-2", "name": "sw2", "address": "10.0.0.2",
           "serial": "SN-XYZ-9", "object_type": "aos_switch"}
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="",
                             defaults=_DEFAULTS)

    assert res["status"] == "SUCCESS", res
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck.get("serial") == "SN-XYZ-9"


def test_sync_nw_device_matches_by_base_mac_when_no_serial():
    # Ladder SERIAL -> MAC: no serial on the poll, but the switch's base MAC
    # (device.mac from the nw info command) matches an ARP-created device ->
    # updated in place, not duplicated under the nw_device_id key.
    other = {"id": 66, "name": "device-aabbccddeeff", "serial": "",
             "custom_fields": {"mac_address": "aa:bb:cc:dd:ee:ff"}}
    eng = _engine(existing_devices=[other])
    eng.nb.dcim.devices.get = MagicMock(return_value=_Obj(
        id=66, name="device-aabbccddeeff",
        custom_fields={"mac_address": "aa:bb:cc:dd:ee:ff"}))
    dev = dict(_DEV, mac="AA-BB-CC-DD-EE-FF")
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="",
                             defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    eng.nb.dcim.devices.create.assert_not_called()
    assert res["device_id"] == 66


def test_sync_nw_device_serial_beats_mac():
    # Both identities present but on DIFFERENT existing rows: serial wins.
    by_mac = {"id": 70, "name": "a", "serial": "",
              "custom_fields": {"mac_address": "aa:bb:cc:dd:ee:ff"}}
    by_serial = {"id": 71, "name": "b", "serial": "SN-1", "custom_fields": {}}
    eng = _engine(existing_devices=[by_mac, by_serial])
    eng.nb.dcim.devices.get = MagicMock(side_effect=lambda i: _Obj(id=i, name="x"))
    dev = dict(_DEV, mac="aa:bb:cc:dd:ee:ff", serial="SN-1")
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="",
                             defaults=_DEFAULTS)
    assert res["device_id"] == 71, res


def test_sync_nw_device_stores_base_mac_on_create():
    eng = _engine(existing_devices=[])
    created = _Obj(id=88, name="sw1")
    eng.nb.dcim.devices.create = MagicMock(return_value=created)
    dev = dict(_DEV, mac="AA:BB:CC:DD:EE:01")
    eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert created.custom_fields.get("mac_address") == "aa:bb:cc:dd:ee:01"


def test_sync_nw_device_renames_auto_named_match_to_polled_hostname():
    # ARP minted DEVICE-<mac>; the poll now knows the real hostname -> adopt it.
    other = {"id": 66, "name": "DEVICE-001A1E042F00", "serial": "",
             "custom_fields": {"mac_address": "00:1a:1e:04:2f:00"}}
    eng = _engine(existing_devices=[other])
    rec = _Obj(id=66, name="DEVICE-001A1E042F00",
               custom_fields={"mac_address": "00:1a:1e:04:2f:00"})
    eng.nb.dcim.devices.get = MagicMock(return_value=rec)
    dev = dict(_DEV, name="mipbe-ssplm-n31-vpnc1", mac="00:1a:1e:04:2f:00")
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="",
                             defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    assert rec.name == "MIPBE-SSPLM-N31-VPNC1"
    eng.nb.dcim.devices.create.assert_not_called()


def test_sync_nw_device_follows_hostname_changed_on_the_box():
    # Matched by serial; the box was renamed on the device -> NetBox follows.
    row = {"id": 70, "name": "MIPBE-SSPLM-N31-VPNC1", "serial": "SN-1",
           "custom_fields": {"discovered_from": "Network Devices"}}
    eng = _engine(existing_devices=[row])
    rec = _Obj(id=70, name="MIPBE-SSPLM-N31-VPNC1", custom_fields={})
    rec.serial = "SN-1"
    eng.nb.dcim.devices.get = MagicMock(return_value=rec)
    dev = dict(_DEV, name="mipbe-ssplm-n31-r45-vpnc1", serial="SN-1")
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    assert rec.name == "MIPBE-SSPLM-N31-R45-VPNC1"


def test_sync_nw_device_name_match_only_never_renames_human_name():
    # Matched by name only (no serial/MAC/nw id in common) -> name untouched.
    row = {"id": 71, "name": "core-sw", "serial": "", "custom_fields": {}}
    eng = _engine(existing_devices=[row])
    rec = _Obj(id=71, name="core-sw", custom_fields={})
    eng.nb.dcim.devices.get = MagicMock(return_value=rec)
    dev = dict(_DEV, name="core-sw", serial="", mac="")
    eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert rec.name == "core-sw"


def test_sync_nw_device_keeps_auto_name_when_poll_has_only_ip_name():
    other = {"id": 66, "name": "DEVICE-001A1E042F00", "serial": "",
             "custom_fields": {"mac_address": "00:1a:1e:04:2f:00"}}
    eng = _engine(existing_devices=[other])
    rec = _Obj(id=66, name="DEVICE-001A1E042F00",
               custom_fields={"mac_address": "00:1a:1e:04:2f:00"})
    eng.nb.dcim.devices.get = MagicMock(return_value=rec)
    dev = dict(_DEV, name="172.21.2.3", mac="00:1a:1e:04:2f:00")
    eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="",
                       defaults=_DEFAULTS)
    assert rec.name == "DEVICE-001A1E042F00"


def test_sync_nw_device_filters_tenant_devices_by_tenant_id():
    # NetBox's ``tenant`` filter takes a slug; a numeric id there is a 400.
    eng = _engine()
    eng._resolve_tenant_ci = MagicMock(return_value=MagicMock(id=7, slug="lrb"))
    eng.nb.dcim.devices.create = MagicMock(return_value=_Obj(id=100, name="sw1"))
    eng.sync_nw_device(device=_DEV, interfaces=[], tenant_slug="lrb",
                       defaults=_DEFAULTS)
    params = [c.args[1] for c in eng._api_get_all.call_args_list
              if "dcim/devices" in c.args[0]]
    assert params and params[0].get("tenant_id") == 7
    assert "tenant" not in params[0]


def test_sync_nw_device_create_takes_type_from_model_and_role_from_object_type():
    eng = _engine()
    eng.nb.dcim.devices.create = MagicMock(return_value=_Obj(id=7, name="SW1"))
    dev = dict(_DEV, model="JL635A", object_type="cx_switch")
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="",
                             defaults={"site": "site1"})
    assert res["status"] == "SUCCESS", res
    eng.nb.dcim.device_types.get.assert_any_call(slug="aruba-jl635a")
    eng.nb.dcim.device_roles.get.assert_any_call(slug="switch")
    kwargs = eng.nb.dcim.devices.create.call_args.kwargs
    assert kwargs["device_type"] == 301 and kwargs["role"] == 401


def _generic_existing(eng, type_slug="discovered", role_slug="discovered"):
    existing_row = {"id": 7, "name": "sw1",
                    "custom_fields": {"nw_device_id": "nw-1"}}
    dev_obj = _Obj(id=7, name="sw1", custom_fields={"nw_device_id": "nw-1"})
    dev_obj.device_type = MagicMock(slug=type_slug)
    dev_obj.role = MagicMock(slug=role_slug)
    eng._api_get_all.side_effect = (
        lambda path, params=None, **kw: [existing_row] if "dcim/devices" in path else [])
    eng.nb.dcim.devices.get = MagicMock(return_value=dev_obj)
    return dev_obj


def test_sync_nw_device_upgrades_generic_existing_device_type_and_role():
    eng = _engine()
    dev_obj = _generic_existing(eng)
    dev = dict(_DEV, model="JL635A", object_type="cx_switch")
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults={})
    assert res["status"] == "SUCCESS", res
    assert dev_obj.device_type == 301
    assert dev_obj.role == 401
    dev_obj.save.assert_called()


def test_sync_nw_device_keeps_a_type_and_role_someone_set():
    eng = _engine()
    dev_obj = _generic_existing(eng, type_slug="8325-48y8c", role_slug="core")
    dev = dict(_DEV, model="JL635A", object_type="cx_switch")
    eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults={})
    assert dev_obj.device_type.slug == "8325-48y8c"
    assert dev_obj.role.slug == "core"


_SPEC = {"manufacturer": "Aruba", "model": "8325-48Y8C", "role": "switch", "u_height": 1,
         "ports": [{"prefix": "1/1/", "start": 1, "count": 2, "type": "25gbase-x-sfp28"}],
         "console": None, "power": []}


def test_sync_nw_device_spec_builds_type_with_ports_and_replaces_bare_sku_type():
    eng = _engine()
    eng.nb.dcim.device_types.get = MagicMock(
        side_effect=lambda slug=None, **kw: MagicMock(id=302 if slug == "aruba-8325-48y8c" else 301))
    dev_obj = _generic_existing(eng, type_slug="aruba-jl635a", role_slug="switch")
    dev = dict(_DEV, model="JL635A", object_type="cx_switch", spec=_SPEC)
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults={})
    assert res["status"] == "SUCCESS", res
    assert dev_obj.device_type == 302
    names = [c.kwargs["name"] for c in eng.nb.dcim.interface_templates.create.call_args_list]
    assert names == ["1/1/1", "1/1/2"]


def test_sync_nw_device_spec_never_replaces_a_type_someone_set():
    eng = _engine()
    dev_obj = _generic_existing(eng, type_slug="my-core-switch", role_slug="core")
    dev = dict(_DEV, model="JL635A", object_type="cx_switch", spec=_SPEC)
    eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults={})
    assert dev_obj.device_type.slug == "my-core-switch"


def test_sync_nw_device_empty_poll_never_wipes_interfaces():
    eng = _engine()
    _generic_existing(eng, type_slug="x", role_slug="y")
    port = _Obj(name="1/1/1", custom_fields={"nw_managed": "true"})
    eng.nb.dcim.interfaces.filter = MagicMock(return_value=[port])
    res = eng.sync_nw_device(device=_DEV, interfaces=[], tenant_slug="", defaults={})
    assert res["deleted"] == 0
    port.delete.assert_not_called()


def _with_site_prefixes(eng, prefixes):
    """Add site-scoped prefix rows to the faked /ipam/prefixes listing."""
    inner = eng._api_get_all.side_effect

    def fake(path, params=None, **kw):
        if "ipam/prefixes" in path:
            return prefixes
        return inner(path, params, **kw)
    eng._api_get_all = MagicMock(side_effect=fake)


_MIP = {"prefix": "10.0.0.0/16", "scope_type": "dcim.site",
        "scope": {"id": 77, "slug": "mip", "name": "MIP"}}
_MIP24 = {"prefix": "10.0.0.0/24", "scope_type": "dcim.site",
          "scope": {"id": 78, "slug": "mip-lab", "name": "MIP Lab"}}


def test_sync_nw_device_creates_in_site_of_most_specific_prefix():
    eng = _engine()
    _with_site_prefixes(eng, [_MIP, _MIP24, {"prefix": "10.0.0.0/8"}])
    eng.nb.dcim.devices.create = MagicMock(return_value=_Obj(id=100, name="sw1"))
    res = eng.sync_nw_device(device=_DEV, interfaces=[], tenant_slug="",
                             defaults={"device_type": "switch", "role": "switch"})
    assert res["status"] == "SUCCESS", res
    assert eng.nb.dcim.devices.create.call_args.kwargs["site"] == 78


def test_sync_nw_device_moves_device_out_of_fallback_site_only():
    row = {"id": 7, "name": "sw1", "custom_fields": {"nw_device_id": "nw-1"}}
    eng = _engine(existing_devices=[row])
    _with_site_prefixes(eng, [_MIP])
    dev_obj = _Obj(id=7, name="sw1", custom_fields={"nw_device_id": "nw-1"})
    dev_obj.site = MagicMock(id=1, slug="discovered")
    eng.nb.dcim.devices.get = MagicMock(return_value=dev_obj)
    res = eng.sync_nw_device(device=_DEV, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert dev_obj.site == 77
    assert res["site"] == {"slug": "mip", "name": "MIP"}
    placed = _Obj(id=7, name="sw1", custom_fields={"nw_device_id": "nw-1"})
    placed.site = MagicMock(id=5, slug="olathe")
    eng.nb.dcim.devices.get = MagicMock(return_value=placed)
    eng.sync_nw_device(device=_DEV, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert placed.site.slug == "olathe"


# ── hardware-serial stitching: controller (iLO) vs host server ──────────────
_ILO = {"id": "nw-ilo", "name": "mipbe-n31-ilo-tnas", "address": "10.0.0.15",
        "serial": "2M292101K3", "object_type": "ipmi"}


def test_ipmi_never_matches_or_renames_the_server_sharing_its_serial():
    server = {"id": 60, "name": "MIPBE-N31-TNAS", "serial": "2M292101K3",
              "role": {"slug": "storage"}, "custom_fields": {}}
    eng = _engine(existing_devices=[server])
    created = _Obj(id=61, name="MIPBE-N31-ILO-TNAS")
    eng.nb.dcim.devices.create = MagicMock(return_value=created)
    srv_rec = _Obj(id=60, name="MIPBE-N31-TNAS")
    eng.nb.dcim.devices.get = MagicMock(return_value=srv_rec)
    res = eng.sync_nw_device(device=_ILO, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS" and res["device_id"] == 61, res
    eng.nb.dcim.devices.create.assert_called_once()
    assert srv_rec.name == "MIPBE-N31-TNAS"


def test_server_feed_never_matches_the_controller_by_serial():
    ilo = {"id": 61, "name": "MIPBE-N31-ILO-TNAS", "serial": "2M292101K3",
           "role": {"slug": "bmc"}, "custom_fields": {}}
    eng = _engine(existing_devices=[ilo])
    idx = eng._index_existing_devices([ilo])
    assert "2m292101k3" not in idx["by_serial"] and "2m292101k3" in idx["by_serial_mgmt"]


def test_adopt_real_name_refuses_across_kinds():
    eng = _engine()
    rec = _Obj(id=1, name="device-aabbccddeeff")
    rec.role = MagicMock(slug="storage")
    assert eng._adopt_real_name(rec, "MIPBE-N31-ILO-TNAS", {}, force=True, expect_mgmt=True) is False
    assert rec.name == "device-aabbccddeeff"


def test_ipmi_links_host_and_racks_it_without_touching_oob_ip():
    ilo_row = {"id": 61, "name": "MIPBE-N31-ILO-TNAS", "role": {"slug": "bmc"},
               "custom_fields": {"nw_device_id": "nw-ilo"}}
    host_row = {"id": 60, "name": "MIPBE-N31-TNAS", "serial": "2M292101K3",
                "role": {"slug": "storage"}, "custom_fields": {}}
    eng = _engine(existing_devices=[ilo_row, host_row])
    ilo = _Obj(id=61, name="MIPBE-N31-ILO-TNAS")
    ilo.rack = None
    ilo.role = MagicMock(slug="bmc")
    host = _Obj(id=60, name="MIPBE-N31-TNAS")
    host.rack = MagicMock(id=3)
    host.site = MagicMock(id=501)
    host.position = 20
    host.face = MagicMock(value="front")
    host.oob_ip = None
    eng.nb.dcim.devices.get = MagicMock(side_effect=lambda i: {61: ilo, 60: host}[i])
    ifaces = [{"name": "mgmt", "ip": "10.0.0.15", "status": "up"}]
    eng.nb.dcim.interfaces.create = MagicMock(return_value=_Obj(id=700))
    dev = dict(_ILO, linked_host={"kind": "truenas", "name": "MIPBE-N31-TNAS",
                                  "serial": "2M292101K3"})
    res = eng.sync_nw_device(device=dev, interfaces=ifaces, tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    assert ilo.custom_fields["linked_device"] == "MIPBE-N31-TNAS"
    assert (ilo.rack, ilo.position) == (3, 20)
    assert host.custom_fields["linked_device"] == "MIPBE-N31-ILO-TNAS"
    assert host.oob_ip is None  # NetBox refuses another device's IP
    assert host.name == "MIPBE-N31-TNAS"


def test_ipmi_link_fills_the_host_serial_when_the_host_has_none():
    ilo_row = {"id": 61, "name": "MIPBE-N31-ILO-PX1", "role": {"slug": "bmc"},
               "custom_fields": {"nw_device_id": "nw-ilo"}}
    host_row = {"id": 60, "name": "MIPBE-SSPLM-PXMX01", "serial": "",
                "role": {"slug": "hypervisor"}, "custom_fields": {}}
    eng = _engine(existing_devices=[ilo_row, host_row])
    ilo = _Obj(id=61, name="MIPBE-N31-ILO-PX1")
    ilo.rack = None
    ilo.role = MagicMock(slug="bmc")
    host = _Obj(id=60, name="MIPBE-SSPLM-PXMX01")
    host.rack = None
    host.serial = ""
    host.oob_ip = None
    eng.nb.dcim.devices.get = MagicMock(side_effect=lambda i: {61: ilo, 60: host}[i])
    dev = dict(_ILO, linked_host={"kind": "agent", "name": "mipbe-ssplm-pxmx01",
                                  "serial": "2M284302GR"})
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    assert host.serial == "2M284302GR"
    assert host.custom_fields["linked_device"] == ilo.name


def test_ipmi_link_finds_a_host_outside_the_controller_tenant():
    ilo_row = {"id": 61, "name": "MIPBE-N31-ILO-PX1", "role": {"slug": "bmc"},
               "custom_fields": {"nw_device_id": "nw-ilo"}}
    host_row = {"id": 60, "name": "MIPBE-SSPLM-PXMX01", "serial": "",
                "role": {"slug": "hypervisor"}, "custom_fields": {}}
    eng = _engine(existing_devices=[ilo_row])
    eng._api_get = MagicMock(side_effect=lambda path, params=None: {"results": [host_row] if path == "/api/dcim/devices/" else []})
    ilo = _Obj(id=61, name="MIPBE-N31-ILO-PX1")
    ilo.rack = None
    ilo.role = MagicMock(slug="bmc")
    host = _Obj(id=60, name="MIPBE-SSPLM-PXMX01")
    host.rack = None
    host.serial = ""
    host.oob_ip = None
    eng.nb.dcim.devices.get = MagicMock(side_effect=lambda i: {61: ilo, 60: host}[i])
    dev = dict(_ILO, linked_host={"kind": "agent", "name": "mipbe-ssplm-pxmx01",
                                  "serial": "2M284302GR"})
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    assert host.serial == "2M284302GR"
    assert host.custom_fields["linked_device"] == ilo.name


def _linked_host_case(host_type_slug):
    ilo_row = {"id": 61, "name": "MIPBE-N31-ILO-PX1", "role": {"slug": "bmc"},
               "custom_fields": {"nw_device_id": "nw-ilo"}}
    host_row = {"id": 60, "name": "MIPBE-SSPLM-PXMX01", "serial": "",
                "role": {"slug": "hypervisor"}, "custom_fields": {}}
    eng = _engine(existing_devices=[ilo_row, host_row])
    ilo = _Obj(id=61, name="MIPBE-N31-ILO-PX1")
    ilo.rack = None
    ilo.role = MagicMock(slug="bmc")
    host = _Obj(id=60, name="MIPBE-SSPLM-PXMX01")
    host.rack = None
    host.serial = ""
    host.oob_ip = None
    host.device_type = MagicMock(slug=host_type_slug)
    eng.nb.dcim.devices.get = MagicMock(side_effect=lambda i: {61: ilo, 60: host}[i])
    eng._identified_device_type = MagicMock(
        return_value=(MagicMock(id=77, slug="hpe-proliant-dl380-gen10"), {"discovered"}))
    dev = dict(_ILO, host_model="ProLiant DL380 Gen10",
               linked_host={"kind": "agent", "name": "mipbe-ssplm-pxmx01", "serial": "2M284302GR"})
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    return host


def test_ipmi_link_gives_a_generic_host_the_server_model_type():
    assert _linked_host_case("discovered").device_type == 77


def test_ipmi_link_keeps_a_host_type_someone_set():
    assert _linked_host_case("dell-r740").device_type.slug == "dell-r740"


def test_ipmi_link_without_host_device_only_marks_the_controller():
    ilo_row = {"id": 61, "name": "MIPBE-N31-ILO-TNAS", "role": {"slug": "bmc"},
               "custom_fields": {"nw_device_id": "nw-ilo"}}
    eng = _engine(existing_devices=[ilo_row])
    ilo = _Obj(id=61, name="MIPBE-N31-ILO-TNAS")
    ilo.role = MagicMock(slug="bmc")
    eng.nb.dcim.devices.get = MagicMock(return_value=ilo)
    dev = dict(_ILO, linked_host={"kind": "truenas", "name": "MIPBE-N31-TNAS", "serial": "2M292101K3"})
    res = eng.sync_nw_device(device=dev, interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    assert ilo.custom_fields["linked_device"] == "MIPBE-N31-TNAS"
    assert ilo.serial == "2M292101K3"
    eng.nb.dcim.devices.create.assert_not_called()


def test_sync_nw_device_merges_sibling_mac_stub_into_the_polled_device():
    real = {"id": 7, "name": "SW-OLD", "serial": "SN1",
            "custom_fields": {"nw_device_id": "nw-1", "mac_address": "ec:50:aa:f4:5b:01"}}
    stub = {"id": 8, "name": "SW-STUB", "serial": "",
            "custom_fields": {"discovered_from": "opnsense", "mac_address": "ec:50:aa:f4:5b:00"}}
    other = {"id": 9, "name": "UNRELATED", "serial": "",
             "custom_fields": {"discovered_from": "opnsense", "mac_address": "ec:50:aa:11:22:00"}}
    eng = _engine(existing_devices=[real, stub, other])
    real_obj, stub_obj = _Obj(id=7, name="SW-OLD"), _Obj(id=8, name="SW-STUB")
    stub_obj.primary_ip4 = 6828
    eng.nb.dcim.devices.get = MagicMock(side_effect=lambda x=None, **k: {7: real_obj, 8: stub_obj}.get(x))
    stub_ip = _Obj(id=6828)
    eng.nb.ipam.ip_addresses.filter = MagicMock(return_value=[stub_ip])
    eng.nb.dcim.interfaces.create = MagicMock(return_value=_Obj(id=555, name="lm-merged"))
    res = eng.sync_nw_device(
        device={"id": "nw-1", "name": "SW-OLD", "serial": "SN1", "mac": "ec:50:aa:f4:5b:01",
                "address": "10.0.0.1", "object_type": "aos_switch"},
        interfaces=[], tenant_slug="", defaults=_DEFAULTS)
    assert res["status"] == "SUCCESS", res
    stub_obj.delete.assert_called_once()
    assert stub_ip.assigned_object_id == 555
    assert stub_obj.primary_ip4 is None


def test_merge_stub_device_refuses_stub_with_different_serial():
    eng = _engine()
    eng._api_get_all = MagicMock(return_value=[{"id": 7, "custom_fields": {"nw_device_id": "nw-1"}}])
    stub = _Obj(id=8, name="STUB")
    stub.serial = "OTHER"
    eng.nb.dcim.devices.get = MagicMock(return_value=stub)
    eng._merge_device_into = MagicMock()
    assert eng.merge_stub_device(8, "nw-1", "SN1")["status"] == "ERROR"
    eng._merge_device_into.assert_not_called()
    stub.serial = ""
    eng._merge_device_into.return_value = True
    assert eng.merge_stub_device(8, "nw-1", "SN1") == {"status": "SUCCESS", "merged": True}

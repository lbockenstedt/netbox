"""Tests for NetboxEngine.sync_devices (NETBOX_SYNC_DEVICES handler).

Self-contained: inserts src/ on sys.path and constructs the engine without a
live NetBox (pynetbox.api is lazy; we overwrite engine.nb + the _api_get*
helpers with fakes). Uses lightweight mock objects because sync_devices does
``dict(obj.custom_fields or {})`` which needs real dicts, not MagicMocks.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Iface:
    """Minimal interface stand-in (just needs .id)."""
    def __init__(self, id=100):
        self.id = id


class _Obj:
    """A minimal stand-in for a pynetbox record: settable custom_fields, save,
    delete, and an arbitrary .id / .interfaces / .primary_ip4."""

    def __init__(self, id=1, custom_fields=None):
        self.id = id
        self.custom_fields = dict(custom_fields or {})
        self.primary_ip4 = None
        self.interfaces = MagicMock()
        self.interfaces.create.return_value = _Iface(id=100)
        self.save = MagicMock()
        self.delete = MagicMock()


def _engine_with(existing_rows, tenant_obj=None):
    """Build an engine whose nb is a MagicMock; _api_get_all returns the given
    existing device rows; _api_get (prefix lookup) returns empty (→ /32)."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.tenancy.tenants.get.return_value = tenant_obj  # _Obj(id=1) or None
    eng._api_get_all = MagicMock(return_value=existing_rows)
    eng._api_get = MagicMock(return_value={"results": []})  # no containing prefix → /32
    return eng


def test_sync_devices_refuses_unknown_tenant():
    eng = _engine_with(existing_rows=[], tenant_obj=None)
    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "AA-BB-CC-DD-EE-FF", "hostname": "ws"}],
        tenant_slug="ghost", replace=True, defaults={})
    assert res["status"] == "ERROR"
    assert "ghost" in res["message"]
    # Nothing created when the tenant is unknown (no unattributed records).
    eng.nb.dcim.devices.create.assert_not_called()


def test_sync_devices_creates_new_owned_device():
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = dev
    iface = _Iface(id=100)
    eng.nb.dcim.interfaces.create.return_value = iface
    ip = _Obj(id=555)
    eng.nb.ipam.ip_addresses.create.return_value = ip

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "AA-BB-CC-DD-EE-FF", "hostname": "ws-05"}],
        tenant_slug="lrb", replace=True, defaults={})

    assert res["status"] == "SUCCESS"
    assert res["pushed"] == 1
    # Device created with tenant + role/device_type/site defaults + active.
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck["status"] == "active"
    assert ck["tenant"] == 1
    assert ck["role"] is not None and ck["device_type"] is not None
    assert ck["name"] == "ws-05"
    # Ownership tag applied to the device.
    assert dev.custom_fields.get("discovered_from") == "opnsense"
    dev.save.assert_called()
    # mgmt interface created via the top-level dcim.interfaces endpoint
    # (device=<id>) — NOT devobj.interfaces.create (nested accessor unsupported
    # on some pynetbox versions). IP created against it + mac_address set.
    eng.nb.dcim.interfaces.create.assert_called_once()
    assert eng.nb.dcim.interfaces.create.call_args.kwargs["device"] == 42
    ik = eng.nb.ipam.ip_addresses.create.call_args.kwargs
    assert ik["address"] == "10.0.0.5/32"  # no containing prefix → /32
    assert ik["assigned_object_type"] == "dcim.interface"
    assert ik["assigned_object_id"] == 100
    assert ik["dns_name"] == "ws-05"
    assert ip.custom_fields.get("mac_address") == "aa:bb:cc:dd:ee:ff"  # normalized
    # primary_ip4 set from the created IP.
    assert dev.primary_ip4 == 555


def test_sync_devices_updates_existing_by_ip_no_duplicate():
    # Existing device (ours, tagged) with primary IP 10.0.0.5 → PUT-refresh, no new device.
    row = {"id": 77, "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {"discovered_from": "opnsense"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    ip = _Obj(id=901, custom_fields={})
    eng.nb.ipam.ip_addresses.get.return_value = ip
    dev = _Obj(id=77, custom_fields={"discovered_from": "opnsense"})
    eng.nb.dcim.devices.get.return_value = dev

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "AA:BB:CC:DD:EE:FF", "hostname": "ws-renamed"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS"
    assert res["pushed"] == 1
    eng.nb.dcim.devices.create.assert_not_called()  # no duplicate
    # MAC refreshed on the existing IP (normalized) + dns_name set.
    assert ip.custom_fields.get("mac_address") == "aa:bb:cc:dd:ee:ff"
    assert ip.dns_name == "ws-renamed"
    ip.save.assert_called()
    # Owned → renamed.
    assert dev.name == "ws-renamed"
    dev.save.assert_called()


def test_sync_devices_replace_deletes_owned_absent_tenant_scoped():
    # Two owned devices; incoming only has one → the other is deleted.
    rows = [
        {"id": 11, "primary_ip4": {"id": 111, "address": "10.0.0.5/24"},
         "custom_fields": {"discovered_from": "opnsense"}},
        {"id": 22, "primary_ip4": {"id": 222, "address": "10.0.0.6/24"},
         "custom_fields": {"discovered_from": "opnsense"}},
    ]
    eng = _engine_with(existing_rows=rows, tenant_obj=_Obj(id=1))
    # The absent device's IP 10.0.0.6 → device 22 fetched + deleted.
    dev22 = _Obj(id=22)
    eng.nb.dcim.devices.get.return_value = dev22
    eng.nb.ipam.ip_addresses.get.return_value = _Obj(id=111, custom_fields={})

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws-05"}],
        tenant_slug="lrb", replace=True, defaults={})

    assert res["status"] == "SUCCESS"
    assert res["deleted"] == 1
    dev22.delete.assert_called()


def test_sync_devices_no_delete_when_unscoped():
    # Global sync (no tenant slug) must NOT delete even with replace=True and
    # owned devices present — mirror sync_vms's global safety contract.
    rows = [{"id": 11, "primary_ip4": {"id": 111, "address": "10.0.0.5/24"},
             "custom_fields": {"discovered_from": "opnsense"}}]
    eng = _engine_with(existing_rows=rows, tenant_obj=None)
    eng.nb.dcim.devices.create.return_value = _Obj(id=42)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.6", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws-06"}],
        tenant_slug="", replace=True, defaults={})

    assert res["status"] == "SUCCESS"
    assert res["deleted"] == 0
    eng.nb.dcim.devices.get.assert_not_called()  # no delete path entered


def test_sync_devices_missing_custom_field_is_graceful():
    # mac_address / discovered_from custom fields absent in NetBox → the
    # post-create save raises (NetBox rejects unknown custom fields). The sync
    # must swallow it and still create the device + IP.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    # First save (the discovered_from tag) raises; the second (primary_ip4) must
    # succeed so the device + IP are still created.
    dev.save.side_effect = [Exception("custom field discovered_from not found"), None]
    eng.nb.dcim.devices.create.return_value = dev
    ip = _Obj(id=555)
    eng.nb.ipam.ip_addresses.create.return_value = ip

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS"
    assert res["pushed"] == 1  # device still counts as pushed despite the tag failure


# ── name + tenant unique-constraint fix (DHCP IP-move collision) ────────────

def test_sync_devices_owned_name_collision_deletes_stale_then_creates():
    # A device we own (discovered_from=opnsense) named device-<mac> at an OLD
    # IP; the same MAC now reports a NEW IP. The new IP is absent from
    # existing_by_ip → create branch → the same device-<mac> name collides on
    # (name, tenant). We must delete the stale owned record and re-create with
    # the new IP instead of 400-ing on the unique constraint. replace=False so
    # replace-delete doesn't pre-empt the collision path.
    row = {"id": 77, "name": "device-aabbccddeeff",
           "primary_ip4": {"id": 901, "address": "10.0.0.9/24"},
           "custom_fields": {"discovered_from": "opnsense"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    stale = _Obj(id=77, custom_fields={"discovered_from": "opnsense"})
    new_dev = _Obj(id=42)
    eng.nb.dcim.devices.get.return_value = stale      # the by-name stale fetch
    eng.nb.dcim.devices.create.return_value = new_dev
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": ""}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0
    # The stale owned device was deleted, then a fresh one created with the
    # same name (now freed) + the new IP — no 400.
    stale.delete.assert_called_once()
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck["name"] == "device-aabbccddeeff"
    assert ck["tenant"] == 1
    # New IP created against the new device.
    ik = eng.nb.ipam.ip_addresses.create.call_args.kwargs
    assert ik["address"] == "10.0.0.5/32"


def test_sync_devices_unowned_name_collision_uniquifies_does_not_clobber():
    # A HUMAN device owns the name device-<mac> (no discovered_from tag). We
    # must NOT delete it; instead create our device under a uniquified name.
    row = {"id": 88, "name": "device-aabbccddeeff",
           "primary_ip4": {"id": 902, "address": "10.0.0.9/24"},
           "custom_fields": {}}   # unowned
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    new_dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = new_dev
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": ""}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0
    # The human device was NOT touched (no devices.get for a delete).
    eng.nb.dcim.devices.get.assert_not_called()
    # We created under a uniquified name (mac suffix), not the colliding one.
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck["name"] != "device-aabbccddeeff"
    assert ck["name"].startswith("device-aabbccddeeff-")
    assert ck["name"].endswith("eeff")  # last 4 of normalized mac


def test_sync_devices_no_ip_owned_device_indexed_by_name():
    # An owned device with NO primary_ip4 is skipped from existing_by_ip but
    # MUST still be indexed by name — otherwise a re-create with the same
    # device-<mac> name 400s on the unique constraint.
    row = {"id": 99, "name": "device-aabbccddeeff",
           "primary_ip4": None, "custom_fields": {"discovered_from": "opnsense"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    stale = _Obj(id=99, custom_fields={"discovered_from": "opnsense"})
    eng.nb.dcim.devices.get.return_value = stale
    eng.nb.dcim.devices.create.return_value = _Obj(id=42)
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": ""}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["errors"] == 0
    stale.delete.assert_called_once()  # the no-IP stale owned device was freed


# ── intra-batch duplicate hostnames (the real 182-error root cause) ─────────

def test_sync_devices_intra_batch_duplicate_hostnames_get_unique_names():
    # Many discovery records share a hostname (ks205, sonoszp…) across distinct
    # MACs. existing_by_name is a pre-batch snapshot and can't see names created
    # earlier THIS batch, so a 2nd create with the same name 400s on
    # (name, site, tenant). used_names dedups intra-batch: the first keeps the
    # hostname, the rest get a -<mac[-4:]> suffix — no 400s.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    eng.nb.dcim.devices.create.side_effect = [_Obj(id=i) for i in (42, 43, 44)]
    eng.nb.ipam.ip_addresses.create.side_effect = [_Obj(id=i) for i in (555, 556, 557)]

    res = eng.sync_devices(
        devices=[
            {"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:01", "hostname": "ks205"},
            {"ip": "10.0.0.6", "mac": "aa:bb:cc:dd:ee:02", "hostname": "ks205"},
            {"ip": "10.0.0.7", "mac": "aa:bb:cc:dd:ee:03", "hostname": "ks205"},
        ],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 3
    assert res["errors"] == 0
    names = [c.kwargs["name"] for c in eng.nb.dcim.devices.create.call_args_list]
    assert len(names) == len(set(names))   # all unique → no constraint 400
    assert names[0] == "ks205"
    assert names[1].startswith("ks205-") and names[1].endswith("ee02")
    assert names[2].startswith("ks205-") and names[2].endswith("ee03")


def test_sync_devices_duplicate_hostname_does_not_clobber_refreshed_device():
    # An owned "ks205" at 10.0.0.5 IS in the batch (refreshed by IP-match) and a
    # SECOND record shares hostname "ks205" at a different IP + MAC. The
    # duplicate must uniquify — it must NOT delete (reclaim) the device we just
    # refreshed for a different IP. refreshed_ids + used_names guard this.
    row = {"id": 77, "name": "ks205",
           "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {"discovered_from": "opnsense"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    refreshed_dev = _Obj(id=77, custom_fields={"discovered_from": "opnsense"})
    eng.nb.dcim.devices.get.return_value = refreshed_dev   # IP-match rename fetch
    eng.nb.ipam.ip_addresses.get.return_value = _Obj(id=901, custom_fields={})
    eng.nb.dcim.devices.create.return_value = _Obj(id=42)
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[
            {"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:01", "hostname": "ks205"},  # refresh
            {"ip": "10.0.0.9", "mac": "aa:bb:cc:dd:ee:09", "hostname": "ks205"},  # dup → uniquify
        ],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 2
    assert res["errors"] == 0
    refreshed_dev.delete.assert_not_called()   # never clobber the refreshed device
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck["name"] != "ks205"
    assert ck["name"].startswith("ks205-") and ck["name"].endswith("ee09")


# ── _ensure_custom_fields (spoke-side self-heal) ────────────────────────────

def test_ensure_custom_fields_creates_missing_skips_present():
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    # proxmox_node already exists AND is attached → skip (no create, no save);
    # the other 7 must be created.
    present = SimpleNamespace(name="proxmox_node",
                              content_types=["virtualization.virtualmachine"],
                              save=MagicMock())
    eng.nb.extras.custom_fields.all.return_value = [present]
    eng.nb.extras.custom_fields.create.return_value = SimpleNamespace(
        name="x", content_types=[], save=MagicMock())
    eng._ensure_custom_fields()
    created = {c.kwargs["name"] for c in eng.nb.extras.custom_fields.create.call_args_list}
    assert "proxmox_node" not in created
    assert {"proxmox_unique_id", "proxmox_vmid", "proxmox_type",
            "discovered_from", "mac_address", "vmid_start", "vmid_end"} <= created
    # Each create carries the NetBox REST shape (incl. content_types).
    sample = eng.nb.extras.custom_fields.create.call_args_list[0].kwargs
    assert sample["type"] in ("text", "integer")
    assert isinstance(sample["content_types"], list)
    # Already-attached present field was not re-saved.
    present.save.assert_not_called()


def test_ensure_custom_fields_attaches_existing_unattached_field():
    # A field that exists globally but is NOT attached to its content type →
    # NetBox writes fail with "does not exist for this object type". The ensure
    # must attach it (save) instead of skipping. NetBox returns ONE object per
    # field name; mac_address is listed twice in _REQUIRED_CUSTOM_FIELDS (once
    # per content type — ipam.ipaddress + dcim.device) so the shared field gets
    # each content type attached.
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    by_required = {}
    for name, ftype, label, ct in NetboxEngine._REQUIRED_CUSTOM_FIELDS:
        if name not in by_required:
            attached = [] if name == "proxmox_node" else [ct]
            by_required[name] = SimpleNamespace(name=name,
                                                content_types=list(attached),
                                                save=MagicMock())
    cfs = list(by_required.values())
    eng.nb.extras.custom_fields.all.return_value = cfs
    eng._ensure_custom_fields()
    eng.nb.extras.custom_fields.create.assert_not_called()  # all exist
    prox = by_required["proxmox_node"]
    prox.save.assert_called_once()
    assert "virtualization.virtualmachine" in prox.content_types
    # mac_address shared across two content types — the second (dcim.device) is
    # attached via one save; the first was already attached (no extra save).
    mac = by_required["mac_address"]
    assert "ipam.ipaddress" in mac.content_types
    assert "dcim.device" in mac.content_types
    assert mac.save.call_count == 1
    # Other already-attached fields were not re-saved.
    others = [c for c in cfs if c.name not in ("proxmox_node", "mac_address")]
    assert all(c.save.call_count == 0 for c in others)


def test_ensure_custom_fields_swallows_api_error():
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.extras.custom_fields.all.side_effect = Exception("403 forbidden")
    # Must not raise — a restricted token must never break the spoke.
    eng._ensure_custom_fields()
    eng.nb.extras.custom_fields.create.assert_not_called()
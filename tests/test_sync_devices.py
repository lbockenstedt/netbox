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
    # Default: no pre-existing global IP → _reuse_or_create_ip creates. Tests
    # that need the reuse/refresh path override ip_addresses.get.return_value.
    eng.nb.ipam.ip_addresses.get.return_value = None
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
    # primary_ip4 is set on a FRESH fetch (devices.get), not on the created
    # dev, so the save re-sends only NetBox's actually-provisioned custom_fields
    # — re-sending the best-effort cf stamp 400s when the deployed NetBox hasn't
    # attached those fields yet. See test_sync_devices_missing_custom_field_is_graceful.
    fresh = _Obj(id=42)
    eng.nb.dcim.devices.get.return_value = fresh
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
    # MAC stamped on the DEVICE (not just the IP) so a recurrence MAC-matches
    # this device instead of duplicate-creating — the linchpin of the registry
    # dedup fix.
    assert dev.custom_fields.get("mac_address") == "aa:bb:cc:dd:ee:ff"
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
    # primary_ip4 set from the created IP — on the FRESH fetch, not on dev
    # (re-sending dev's best-effort cf stamp would 400 when fields are unprovisioned).
    assert fresh.primary_ip4 == 555
    assert fresh.save.called


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
    eng.nb.dcim.devices.delete.assert_not_called()  # no delete path entered


def test_sync_devices_missing_custom_field_is_graceful():
    # mac_address / discovered_from custom fields absent in NetBox → the
    # post-create save raises (NetBox rejects unknown custom fields). The sync
    # must swallow it and still create the device + IP.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    # The discovered_from/last_seen/mac_address cf stamp save raises (NetBox
    # rejects unprovisioned custom fields). It is swallowed, so the device is
    # still created. primary_ip4 is set on a FRESH fetch whose save carries only
    # NetBox's actually-provisioned cfs, so it succeeds — the device is not left
    # IP-less (which used to make the next sync re-create + 400 every cycle).
    dev.save.side_effect = Exception("custom field discovered_from not found")
    eng.nb.dcim.devices.create.return_value = dev
    fresh = _Obj(id=42)
    eng.nb.dcim.devices.get.return_value = fresh
    ip = _Obj(id=555)
    eng.nb.ipam.ip_addresses.create.return_value = ip

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS"
    assert res["pushed"] == 1  # device still counts as pushed despite the tag failure
    # primary_ip4 landed on the fresh fetch even though the cf-stamp save raised.
    assert fresh.primary_ip4 == 555
    assert fresh.save.called


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
    # The human device was NOT touched (no delete). NB: the create branch now
    # calls devices.get for the fresh primary_ip4 fetch, so get IS called here.
    eng.nb.dcim.devices.delete.assert_not_called()
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
    # last_seen spans three content types (dcim.device already attached above;
    # virtualization.virtualmachine + ipam.ipaddress each attach via a save).
    ls = by_required["last_seen"]
    assert "dcim.device" in ls.content_types
    assert "virtualization.virtualmachine" in ls.content_types
    assert "ipam.ipaddress" in ls.content_types
    assert ls.save.call_count == 2
    # decommissioned_at spans two content types (dcim.device attached;
    # virtualization.virtualmachine attaches via one save).
    dc = by_required["decommissioned_at"]
    assert "dcim.device" in dc.content_types
    assert "virtualization.virtualmachine" in dc.content_types
    assert dc.save.call_count == 1
    # Other (single-content-type) already-attached fields were not re-saved.
    multi = ("proxmox_node", "mac_address", "last_seen", "decommissioned_at")
    others = [c for c in cfs if c.name not in multi]
    assert all(c.save.call_count == 0 for c in others)


def test_ensure_custom_fields_swallows_api_error():
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.extras.custom_fields.all.side_effect = Exception("403 forbidden")
    # Must not raise — a restricted token must never break the spoke.
    eng._ensure_custom_fields()
    eng.nb.extras.custom_fields.create.assert_not_called()


# ── global duplicate-IP reuse (the 183-error fix) ───────────────────────────

def test_sync_devices_reuses_existing_global_ip_instead_of_duplicate_create():
    # The IP already exists globally in NetBox (the IPAM provisioned it — NetBox
    # is the IPAM source of truth). The create branch must REUSE the existing
    # ipam.ip_address and reassign it to the new device's mgmt interface + tag
    # MAC, instead of ipam.ip_addresses.create — which 400s "Duplicate IP
    # address found in global table" and was failing ~every record.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = dev
    fresh = _Obj(id=42)
    eng.nb.dcim.devices.get.return_value = fresh
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    existing_ip = _Obj(id=901, custom_fields={})
    eng.nb.ipam.ip_addresses.get.return_value = existing_ip   # global duplicate

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws-05"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    # No duplicate create — the existing record was reused + reassigned.
    eng.nb.ipam.ip_addresses.create.assert_not_called()
    assert existing_ip.assigned_object_id == 100
    assert existing_ip.assigned_object_type == "dcim.interface"
    assert existing_ip.custom_fields.get("mac_address") == "aa:bb:cc:dd:ee:ff"
    existing_ip.save.assert_called()
    assert fresh.primary_ip4 == 901   # device points at the reused IP (fresh fetch)


# ── change-log journal stamps ───────────────────────────────────────────────

def test_sync_devices_journals_created_device_and_ip_with_module_and_timestamp():
    # Every entry the sync ADDS to NetBox gets a journal entry (NetBox's
    # per-object change log) recording which module created it and when.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    eng.nb.dcim.devices.create.return_value = _Obj(id=42)
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws"}],
        tenant_slug="lrb", replace=False, defaults={})

    calls = eng.nb.extras.journal_entries.create.call_args_list
    assert len(calls) == 2   # one on the device, one on the IP
    ctypes = {c.kwargs["assigned_object_type"] for c in calls}
    assert ctypes == {"dcim.device", "ipam.ipaddress"}
    for c in calls:
        assert c.kwargs["kind"] == "info"
        # The journal names the REAL source (default firewall → "opnsense"),
        # not a hardcoded "firewall-discovery" string — provenance in the
        # NetBox change log so the user can tell which sync created each device.
        assert "opnsense" in c.kwargs["comment"]                 # source tag
        assert " at " in c.kwargs["comment"]                     # timestamp


# ── source-tag generalization (nw sync → "Network Devices" ownership tag) ────

def test_sync_devices_default_source_tags_opnsense():
    # No source passed → legacy "opnsense" tag (unchanged firewall behavior).
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = dev
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert dev.custom_fields.get("discovered_from") == "opnsense"


def test_sync_devices_firewall_synonyms_normalize_to_opnsense():
    # "fw" / "firewall" / "OPNsense" (any case) all collapse to the legacy
    # "opnsense" tag so existing firewall deployments stay byte-identical.
    for src in ("fw", "firewall", "OPNsense", "Firewall"):
        eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
        dev = _Obj(id=42)
        eng.nb.dcim.devices.create.return_value = dev
        eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
        eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)
        eng.sync_devices(
            devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws"}],
            tenant_slug="lrb", replace=False, defaults={}, source=src)
        assert dev.custom_fields.get("discovered_from") == "opnsense", src


def test_sync_devices_nw_source_tags_network_devices():
    # The nw sync passes source="Network Devices" → created devices are tagged
    # verbatim "Network Devices" (the nw ownership tag), NOT "opnsense".
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = dev
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws"}],
        tenant_slug="lrb", replace=False, defaults={}, source="Network Devices")

    assert dev.custom_fields.get("discovered_from") == "Network Devices"


def test_sync_devices_nw_replace_delete_skips_opnsense_owned():
    # CRITICAL cross-source invariant: a nw sync (source="Network Devices") with
    # replace=True must replace-delete ONLY nw-owned records — it must NEVER
    # touch an opnsense-tagged device in the same tenant, even though the
    # opnsense device's IP is absent from the nw incoming set. owned_ips is
    # scoped to the nw tag, so the firewall record stays put.
    rows = [
        {"id": 11, "primary_ip4": {"id": 111, "address": "10.0.0.5/24"},
         "custom_fields": {"discovered_from": "opnsense"}},        # firewall's
        {"id": 22, "primary_ip4": {"id": 222, "address": "10.0.0.6/24"},
         "custom_fields": {"discovered_from": "Network Devices"}},  # nw's
    ]
    eng = _engine_with(existing_rows=rows, tenant_obj=_Obj(id=1))
    dev22 = _Obj(id=22)
    eng.nb.dcim.devices.get.return_value = dev22   # the nw-owned absent fetch
    eng.nb.ipam.ip_addresses.get.return_value = _Obj(id=222, custom_fields={})

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:01", "hostname": "ws-05"}],
        tenant_slug="lrb", replace=True, defaults={}, source="Network Devices")

    assert res["status"] == "SUCCESS", res
    assert res["deleted"] == 1           # the nw-owned device 22 (10.0.0.6) gone
    dev22.delete.assert_called_once()
    # The firewall-owned device 11 (10.0.0.5, opnsense) is present in the
    # incoming set so it's refreshed, NOT deleted. devices.get is fetched for:
    # the replace-delete of 22, then the refresh of 11 — which now stamps the
    # device's mac_address custom field (so future MAC-matching works) AND
    # last_seens it so the staleness sweep sees it as recently active. The only
    # DELETE is on dev22; device 11 is only ever fetched, never deleted.
    assert eng.nb.dcim.devices.get.call_count == 3
    # No second delete call — device 11 was not reaped.
    assert dev22.delete.call_count == 1


# ── multi-property dedup: IP OR MAC OR bare-hostname → same machine ─────────
#
# A device added to NetBox with no MAC and no IP used to spawn a SECOND copy on
# every firewall sync: the only property left to compare was the hostname, but
# the sync only used the hostname for collision-avoidance (uniquify → create a
# suffixed duplicate) instead of recognizing it as the same machine. The fix
# resolves an existing device by IP first, then MAC, then hostname (ONLY when
# the existing device is bare — no IP AND no mac_address cf, so two distinct
# machines sharing a hostname still get separate rows) and updates it in place.

def test_sync_devices_mac_match_no_duplicate_on_ip_move():
    # Existing OWNED device at 10.0.0.5 carrying mac_address=aa:bb:cc:dd:ee:ff.
    # The same machine later reports in from a DHCP'd 10.0.0.9 (same MAC). The
    # IP index misses (different IP), but the MAC index matches → the existing
    # device is adopted, given the new primary IP, and NOT duplicated.
    row = {"id": 77, "name": "printer-x",
           "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {"discovered_from": "opnsense",
                             "mac_address": "aa:bb:cc:dd:ee:ff"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    existing = _Obj(id=77, custom_fields={"discovered_from": "opnsense",
                                          "mac_address": "aa:bb:cc:dd:ee:ff"})
    eng.nb.dcim.devices.get.return_value = existing
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.get.return_value = None  # new IP → create
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.9", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "printer-x"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0
    # No new device created — the existing one was adopted by MAC.
    eng.nb.dcim.devices.create.assert_not_called()
    # The existing device was repointed at the new primary IP 10.0.0.9.
    assert existing.primary_ip4 == 555


def test_sync_devices_bare_hostname_match_adopts_in_place_assigns_ip():
    # The reported bug: an existing device with NO IP and NO MAC (only a name).
    # A discovery record for the same hostname arrives with an IP + MAC. The
    # sync used to uniquify-create a second device; now it adopts the bare
    # device by hostname, assigns it the primary IP + MAC, no duplicate.
    row = {"id": 77, "name": "printer-x", "primary_ip4": None,
           "custom_fields": {}}   # bare: no IP, no mac_address
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    existing = _Obj(id=77, custom_fields={})
    eng.nb.dcim.devices.get.return_value = existing
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.get.return_value = None
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.9", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "printer-x"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["errors"] == 0
    eng.nb.dcim.devices.create.assert_not_called()   # adopted, not duplicated
    # The bare device now has a primary IP + a stamped MAC.
    assert existing.primary_ip4 == 555
    assert existing.custom_fields.get("mac_address") == "aa:bb:cc:dd:ee:ff"


def test_sync_devices_hostname_match_against_non_bare_device_uniquifies():
    # An UNOWNED (human) device at 10.0.0.5 (HAS an IP — not bare) is named
    # "ks205". A DIFFERENT machine (different MAC) reports in from 10.0.0.9 with
    # the SAME hostname "ks205". This is the shared-hostname case (ks205 across
    # distinct MACs): the existing device is NOT bare, so hostname does NOT
    # adopt it → the new machine gets its own uniquified device, and the human
    # device is left untouched (no merge of distinct machines).
    row = {"id": 77, "name": "ks205",
           "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {}}   # unowned + has IP → not bare, don't adopt
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    new_dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = new_dev
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.get.return_value = None
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.9", "mac": "aa:bb:cc:dd:ee:01", "hostname": "ks205"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    # A NEW device was created (distinct machine) — under a uniquified name, not
    # clobbering device 77's "ks205".
    eng.nb.dcim.devices.create.assert_called_once()
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck["name"] != "ks205"
    assert ck["name"].startswith("ks205-")
    # The human device 77 was NOT deleted (no reclaim of an unowned name).
    eng.nb.dcim.devices.delete.assert_not_called()


def test_sync_devices_mac_only_record_matches_existing_by_mac_no_duplicate():
    # A MAC-only record (no IP) for a device that already exists at 10.0.0.5
    # with mac_address=aa:bb:cc:dd:ee:ff. The MAC index matches → the existing
    # device's IP gets its MAC refreshed; no duplicate is created.
    row = {"id": 77, "name": "ws-05",
           "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {"discovered_from": "opnsense",
                             "mac_address": "aa:bb:cc:dd:ee:ff"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    ipobj = _Obj(id=901, custom_fields={})
    eng.nb.ipam.ip_addresses.get.return_value = ipobj
    existing = _Obj(id=77, custom_fields={"discovered_from": "opnsense",
                                          "mac_address": "aa:bb:cc:dd:ee:ff"})
    eng.nb.dcim.devices.get.return_value = existing

    res = eng.sync_devices(
        devices=[{"ip": "", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws-05"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    eng.nb.dcim.devices.create.assert_not_called()   # matched by MAC, no new device
    # The existing IP's mac_address was refreshed.
    assert ipobj.custom_fields.get("mac_address") == "aa:bb:cc:dd:ee:ff"


# ── unified-registry pass: no-MAC/no-IP dedup, duplicate rule, provenance ─────

def test_sync_devices_hostname_only_record_not_skipped_added_by_name():
    # A no-MAC/no-IP record carrying just a hostname is ADDED (not skipped),
    # keyed by host:<name> so a later cross-batch record for the same hostname
    # adopts it (the bare-hostname adoption is covered by
    # test_sync_devices_bare_hostname_match_adopts_in_place_assigns_ip). Here we
    # assert the no-skip: a name-only row produces a bare owned device, counted
    # as pushed with skipped == 0.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    bare = _Obj(id=42, custom_fields={})
    eng.nb.dcim.devices.create.return_value = bare

    res = eng.sync_devices(
        devices=[{"ip": "", "mac": "", "hostname": "printer-x"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    assert res["skipped"] == 0
    eng.nb.dcim.devices.create.assert_called_once()
    assert eng.nb.dcim.devices.create.call_args.kwargs["name"] == "printer-x"
    # Bare owned device: ownership tag set, no mac_address (none supplied).
    assert bare.custom_fields.get("discovered_from") == "opnsense"
    assert not bare.custom_fields.get("mac_address")


def test_sync_devices_truly_empty_record_is_skipped():
    # No ip, no mac, no usable hostname → nothing to key on → skipped (the only
    # remaining skip case now that hostname-only records are kept).
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    res = eng.sync_devices(
        devices=[{"ip": "", "mac": "", "hostname": ""}],
        tenant_slug="lrb", replace=False, defaults={})
    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 0
    assert res["skipped"] == 1
    eng.nb.dcim.devices.create.assert_not_called()


def test_sync_devices_duplicate_allowed_when_hostname_same_and_mac_ip_both_differ():
    # The ONE case the registry allows a duplicate: an OWNED device "ks205" at
    # 10.0.0.5 carrying mac ee01, and a DIFFERENT machine reporting the SAME
    # hostname from 10.0.0.9 with a different mac ee09. Both mac & ip differ on
    # both sides → provably different → NOT adopted → the new machine gets its
    # own uniquified device, and the existing owned device is left in place
    # (NOT deleted — it's a different machine, not a stale orphan).
    row = {"id": 77, "name": "ks205",
           "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {"discovered_from": "opnsense",
                             "mac_address": "aa:bb:cc:dd:ee:01"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    new_dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = new_dev
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.get.return_value = None
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.9", "mac": "aa:bb:cc:dd:ee:09", "hostname": "ks205"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    # A NEW device was created (the duplicate the rule allows) under a
    # uniquified name — not "ks205" (which the existing machine keeps).
    eng.nb.dcim.devices.create.assert_called_once()
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck["name"] != "ks205"
    assert ck["name"].startswith("ks205-")
    # The existing owned device 77 was NOT deleted (different machine, not a
    # reclaimable orphan) — no delete. NB: the create branch's fresh primary_ip4
    # fetch now calls devices.get, so get IS called here.
    eng.nb.dcim.devices.delete.assert_not_called()


def test_sync_devices_same_mac_merges_no_duplicate_on_dhcp_move():
    # Owned "ks205" at 10.0.0.5 with mac ee01; same machine DHCP-moves to
    # 10.0.0.9 (same MAC). MAC-match adopts the existing device, repoints it at
    # the new IP, no duplicate — the "same MAC → merge" half of the rule.
    row = {"id": 77, "name": "ks205",
           "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {"discovered_from": "opnsense",
                             "mac_address": "aa:bb:cc:dd:ee:01"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    existing = _Obj(id=77, custom_fields={"discovered_from": "opnsense",
                                          "mac_address": "aa:bb:cc:dd:ee:01"})
    eng.nb.dcim.devices.get.return_value = existing
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.get.return_value = None
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.9", "mac": "aa:bb:cc:dd:ee:01", "hostname": "ks205"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 1
    eng.nb.dcim.devices.create.assert_not_called()   # adopted by MAC, no duplicate
    assert existing.primary_ip4 == 555                # repointed at the new IP


def test_sync_devices_source_switch_cfs_stamped_on_created_device():
    # A MAC-sighting record (e.g. from a switch MAC table) carries the source
    # switch name/ip/port. The created device records them so NetBox answers
    # "where is this MAC?".
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    dev = _Obj(id=42)
    eng.nb.dcim.devices.create.return_value = dev
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.get.return_value = None
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws-05",
                  "source_switch_name": "core-sw1",
                  "source_switch_ip": "10.255.0.2",
                  "source_switch_port": "GigabitEthernet1/0/24"}],
        tenant_slug="lrb", replace=False, defaults={})

    cf = dev.custom_fields
    assert cf.get("switch_name") == "core-sw1"
    assert cf.get("switch_ip") == "10.255.0.2"
    assert cf.get("switch_port") == "GigabitEthernet1/0/24"
    assert cf.get("mac_address") == "aa:bb:cc:dd:ee:ff"


def test_sync_devices_source_switch_cfs_stamped_on_refreshed_device():
    # An existing device matched by IP gets the source-switch cfs updated when
    # the feed attaches them (a later sighting on a different port/switch).
    row = {"id": 77, "name": "ws-05",
           "primary_ip4": {"id": 901, "address": "10.0.0.5/24"},
           "custom_fields": {"discovered_from": "opnsense",
                             "switch_port": "Gi1/0/1"}}
    eng = _engine_with(existing_rows=[row], tenant_obj=_Obj(id=1))
    eng.nb.ipam.ip_addresses.get.return_value = _Obj(id=901, custom_fields={})
    existing = _Obj(id=77, custom_fields={"discovered_from": "opnsense",
                                          "switch_port": "Gi1/0/1"})
    eng.nb.dcim.devices.get.return_value = existing

    res = eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws-05",
                  "source_switch_name": "core-sw2",
                  "source_switch_ip": "10.255.0.3",
                  "source_switch_port": "Gi1/0/24"}],
        tenant_slug="lrb", replace=False, defaults={})

    assert res["status"] == "SUCCESS", res
    cf = existing.custom_fields
    assert cf.get("switch_name") == "core-sw2"
    assert cf.get("switch_ip") == "10.255.0.3"
    assert cf.get("switch_port") == "Gi1/0/24"   # updated from Gi1/0/1
    assert cf.get("mac_address") == "aa:bb:cc:dd:ee:ff"


def test_sync_devices_journal_uses_real_source_tag():
    # The NetBox change-log journal entry names the REAL sync source, not a
    # hardcoded "firewall-discovery" string — so the user can tell which sync
    # created each device. source="Network Devices" → journal says so.
    eng = _engine_with(existing_rows=[], tenant_obj=_Obj(id=1))
    eng.nb.dcim.devices.create.return_value = _Obj(id=42)
    eng.nb.dcim.interfaces.create.return_value = _Iface(id=100)
    eng.nb.ipam.ip_addresses.create.return_value = _Obj(id=555)

    eng.sync_devices(
        devices=[{"ip": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "ws"}],
        tenant_slug="lrb", replace=False, defaults={}, source="Network Devices")

    calls = eng.nb.extras.journal_entries.create.call_args_list
    assert len(calls) == 2
    for c in calls:
        assert "Network Devices" in c.kwargs["comment"]
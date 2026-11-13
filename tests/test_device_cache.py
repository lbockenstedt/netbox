"""The spoke's per-tenant device cache (netbox_device_cache) and the engine
code built on it: the slim index plus per-tenant files, the response hook
that follows our own writes, one rebuild for a missing / corrupt / old cache,
and the daily reconcile of scanner-owned devices."""
import json
import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import netbox_device_cache  # noqa: E402
from netbox_device_cache import DeviceCache, write_json  # noqa: E402
from netbox_engine import NetboxEngine  # noqa: E402
from fake_netbox import FakeNetBox, make_engine  # noqa: E402

NW = {"device_type": "discovered", "role": "discovered", "site": "lab"}


def _world():
    """Tenants t1/t2 and the refs a device create needs."""
    f = FakeNetBox(page_size=500)
    f.add("tenants", {"id": 1, "slug": "t1", "name": "T1"})
    f.add("tenants", {"id": 2, "slug": "t2", "name": "T2"})
    f.add("sites", {"id": 21, "slug": "lab", "name": "Lab"})
    f.add("device-roles", {"id": 31, "slug": "discovered", "name": "Discovered"})
    f.add("device-types", {"id": 41, "slug": "discovered", "model": "Discovered Device"})
    return f


def _device(f, dev_id, name, tenant_id=1, serial="", **cf):
    f.add("devices", {"id": dev_id, "name": name, "serial": serial, "tenant_id": tenant_id,
                      "site_id": 21, "role_id": 31, "device_type_id": 41,
                      "status": "active", "custom_fields": dict(cf), "primary_ip4_id": None})


def _listings(f):
    """Unfiltered device listings (full walks) in the fake's request log."""
    return [q for m, p, q, _ in f.log if m == "GET" and p == "/api/dcim/devices/"
            and not any(k in q for k in ("serial=", "name__ie=", "cf_mac_address=",
                                         "tenant_id=", "id="))]


def _mock_engine(rows):
    """Engine with no NetBox session; ``_api_get_all`` returns ``rows``."""
    eng = NetboxEngine.__new__(NetboxEngine)
    eng._api_get_all = MagicMock(return_value=rows)
    return eng


ROWS = [{"id": 1, "name": "A", "tenant": {"id": 1, "slug": "t1"}, "url": "u1",
         "custom_fields": {"mac_address": "aa"}},
        {"id": 2, "name": "B", "tenant": {"id": 2, "slug": "t2"}, "url": "u2"},
        {"id": 3, "name": "C", "tenant": None, "url": "u3"}]


# ── layout, rebuilds and atomic writes ──────────────────────────────────────

def test_cache_keeps_a_slim_index_and_one_file_per_tenant():
    eng = _mock_engine(ROWS)
    slim = {r["id"]: r for r in eng._all_devices_cached()}
    assert "url" not in slim[1] and slim[1]["custom_fields"] == {"mac_address": "aa"}
    files = sorted(os.listdir(netbox_device_cache.CACHE_DIR))
    assert files == ["_meta.json", "_none.json", "t1.json", "t2.json"]
    assert [r["name"] for r in eng._tenant_devices_cached("t2")] == ["B"]
    assert [r["name"] for r in eng._tenant_devices_cached("")] == ["C"]


def test_cache_is_built_once_and_reused_after_a_restart():
    eng = _mock_engine(ROWS)
    for _ in range(5):
        eng._all_devices_cached()
    assert eng._api_get_all.call_count == 1
    restarted = _mock_engine(ROWS)
    assert len(restarted._all_devices_cached()) == 3
    restarted._api_get_all.assert_not_called()


@pytest.mark.parametrize("damage", ["corrupt tenant file", "missing tenant file",
                                    "missing meta", "old version", "missing directory"])
def test_damaged_cache_is_rebuilt_once(damage):
    _mock_engine(ROWS)._all_devices_cached()
    d = netbox_device_cache.CACHE_DIR
    if damage == "corrupt tenant file":
        with open(os.path.join(d, "t1.json"), "w") as fh:
            fh.write("{not json")
    elif damage == "missing tenant file":
        os.remove(os.path.join(d, "t1.json"))
    elif damage == "missing meta":
        os.remove(os.path.join(d, "_meta.json"))
    elif damage == "old version":
        write_json(os.path.join(d, "_meta.json"), {"version": 1, "netbox": ""})
    else:
        for name in os.listdir(d):
            os.remove(os.path.join(d, name))
        os.rmdir(d)
    eng = _mock_engine(ROWS)
    for _ in range(3):
        assert len(eng._all_devices_cached()) == 3
    assert eng._api_get_all.call_count == 1


def test_legacy_single_file_cache_is_replaced_by_one_rebuild():
    # The previous release kept one device_cache.json next to the directory.
    legacy = os.path.join(os.path.dirname(netbox_device_cache.CACHE_DIR), "device_cache.json")
    with open(legacy, "w") as fh:
        json.dump({"rows": ROWS}, fh)
    eng = _mock_engine(ROWS)
    eng._all_devices_cached()
    eng._all_devices_cached()
    assert eng._api_get_all.call_count == 1
    assert not os.path.exists(legacy)


def test_unwritable_cache_directory_keeps_the_cache_in_memory(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(netbox_device_cache, "CACHE_DIR", str(blocker / "devices"))
    eng = _mock_engine(ROWS)
    for _ in range(3):
        assert len(eng._all_devices_cached()) == 3
    assert [r["name"] for r in eng._tenant_devices_cached("t2")] == ["B"]
    assert eng._api_get_all.call_count == 1


def test_tenant_file_corrupted_while_running_rebuilds_once():
    eng = _mock_engine(ROWS)
    eng._all_devices_cached()
    with open(os.path.join(netbox_device_cache.CACHE_DIR, "t2.json"), "w") as fh:
        fh.write("garbage")
    assert [r["name"] for r in eng._tenant_devices_cached("t2")] == ["B"]
    assert eng._api_get_all.call_count == 2


def test_tenant_file_deleted_while_running_rebuilds_once():
    eng = _mock_engine(ROWS)
    eng._all_devices_cached()
    os.remove(os.path.join(netbox_device_cache.CACHE_DIR, "t2.json"))
    assert [r["name"] for r in eng._tenant_devices_cached("t2")] == ["B"]
    assert eng._api_get_all.call_count == 2


def test_a_write_made_while_netbox_is_paged_is_kept():
    # The listing was read before the device was created; the hook saw the create.
    eng = _mock_engine([])
    cache = eng._device_cache()
    new = dict(ROWS[0], last_updated="2026-01-02T00:00:00Z")

    def walk(*_a, **_k):
        resp = MagicMock(status_code=201, ok=True)
        resp.request.method, resp.request.url = "POST", "http://nb/api/dcim/devices/"
        resp.json.return_value = new
        cache.on_response(resp)
        return [ROWS[1]]
    eng._api_get_all.side_effect = walk
    assert sorted(r["id"] for r in eng._all_devices_cached()) == [1, 2]


def test_upsert_keeps_the_order_of_a_tenants_rows():
    eng = _mock_engine([dict(ROWS[0], id=i, name=f"D{i}") for i in (1, 2, 3)])
    eng._all_devices_cached()
    cache = eng._device_cache()
    cache.upsert([dict(ROWS[0], id=1, name="D1-NEW"), dict(ROWS[0], id=4, name="D4")], force=True)
    assert [r["name"] for r in cache.tenant_rows("t1")] == ["D1-NEW", "D2", "D3", "D4"]


def test_a_tenant_listing_takes_a_moved_device_out_of_its_old_file():
    eng = _mock_engine(ROWS)
    eng._all_devices_cached()
    cache = eng._device_cache()
    # Device 1 moved from t1 to t2; t2 is listed before t1 is.
    cache.replace_tenant("t2", [ROWS[1], dict(ROWS[0], tenant={"id": 2, "slug": "t2"})])
    assert cache.tenant_rows("t1") == []
    assert sorted(r["id"] for r in cache.tenant_rows("t2")) == [1, 2]
    assert sorted(r["id"] for r in cache.rows()) == [1, 2, 3]


@pytest.mark.parametrize("write", ["upsert", "remove"])
def test_a_damaged_file_during_a_write_rebuilds_instead_of_truncating(write):
    eng = _mock_engine(ROWS)
    eng._all_devices_cached()
    cache = eng._device_cache()
    cache.tenant_rows("t1")  # the parsed file is kept in memory
    with open(os.path.join(netbox_device_cache.CACHE_DIR, "t1.json"), "w") as fh:
        fh.write("garbage")
    if write == "upsert":
        cache.upsert([dict(ROWS[0], name="A2")], force=True)
    else:
        cache.remove([1])
    # The cache was cleared rather than left with a one-row t1 file.
    assert not os.path.exists(os.path.join(netbox_device_cache.CACHE_DIR, "t1.json"))
    assert sorted(r["id"] for r in eng._all_devices_cached()) == [1, 2, 3]
    assert eng._api_get_all.call_count == 2


def test_writes_are_atomic(tmp_path, monkeypatch):
    path = str(tmp_path / "t1.json")
    write_json(path, {"rows": [1]})

    def broken_dump(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(netbox_device_cache.json, "dump", broken_dump)
    with pytest.raises(OSError):
        write_json(path, {"rows": [2]})
    monkeypatch.undo()
    with open(path) as fh:
        assert json.load(fh) == {"rows": [1]}
    assert os.listdir(tmp_path) == ["t1.json"]


def test_reconcile_turns_are_spread_over_the_day():
    cache = DeviceCache(str(netbox_device_cache.CACHE_DIR))
    cache.rebuild(ROWS, now=0)
    seen = []
    while True:
        t = cache.due_tenant(now=86400)
        if t is None:
            break
        seen.append(t["slug"])
        cache.mark_reconciled(t["slug"], now=86400)
    assert sorted(seen) == ["", "t1", "t2"]
    assert cache.due_tenant(now=86400 * 2 - 1) is None


# ── the response hook follows our own writes ────────────────────────────────

def test_cache_follows_creates_updates_moves_and_deletes():
    f = _world()
    _device(f, 100, "OLD")
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    f.log.clear()
    rec = eng.nb.dcim.devices.create(name="NEW", device_type=41, role=31, site=21, tenant=1)
    assert eng._device_cache().full_row(rec.id)["name"] == "NEW"
    rec.name = "RENAMED"
    rec.save()
    assert {r["id"]: r["name"] for r in eng._tenant_devices_cached("t1")}[rec.id] == "RENAMED"
    eng.nb.dcim.devices.update([{"id": rec.id, "tenant": 2}])
    assert [r["id"] for r in eng._tenant_devices_cached("t2")] == [rec.id]
    assert [r["id"] for r in eng._tenant_devices_cached("t1")] == [100]
    rec.delete()
    assert sorted(r["id"] for r in eng._all_devices_cached()) == [100]
    assert _listings(f) == []


# ── matching from the cache ─────────────────────────────────────────────────

def test_cross_tenant_match_loads_the_full_row_from_that_tenants_file():
    # A MAC known only in tenant t2 matches a t1 record; the device is built
    # from t2's cached file, not fetched or duplicated.
    f = _world()
    mac = "aa:bb:cc:00:00:01"
    _device(f, 100, "WS-01", tenant_id=2, discovered_from="opnsense", mac_address=mac)
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    f.log.clear()
    matched = []
    match = eng._match_existing_device
    eng._match_existing_device = lambda run, d: matched.append(match(run, d)) or matched[-1]
    res = eng.sync_devices([{"ip": "", "mac": mac, "hostname": "ws-01"}],
                           tenant_slug="t1", defaults={"site": "lab"})
    assert res["status"] == "SUCCESS", res
    assert matched[0]["url"] and matched[0]["site"]["slug"] == "lab"
    assert [d["id"] for d in f.db["devices"].values()] == [100]
    assert not [x for x in f.log if x[0] == "GET" and x[1] == "/api/dcim/devices/100/"]
    assert _listings(f) == []


def test_console_sync_finds_a_device_added_in_netbox_since_the_cache_was_built():
    f = _world()
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    f.log.clear()
    _device(f, 100, "BOX", serial="SER1")  # added in the NetBox UI
    res = eng.sync_devices([{"ip": "", "mac": "", "hostname": "box", "serial": "SER1"}],
                           tenant_slug="t1", source="Console", defaults={"site": "lab"})
    assert res["status"] == "SUCCESS", res
    assert [d["id"] for d in f.db["devices"].values()] == [100]
    assert _listings(f) == []


def test_console_sync_finds_a_device_added_in_netbox_by_its_ip():
    f = _world()
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    _hand_made_device_with_ip(f, "10.0.0.9/24")  # under another name
    res = eng.sync_devices([{"ip": "10.0.0.9", "mac": "", "hostname": "box", "serial": ""}],
                           tenant_slug="t1", source="Console", defaults={"site": "lab"})
    assert res["status"] == "SUCCESS", res
    assert sorted(f.db["devices"]) == [100]


def test_feed_sync_asks_netbox_before_creating_a_device():
    # An opnsense batch is not looked up up front; the record the cache does
    # not know is looked up before the create and matched instead.
    f = _world()
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    _device(f, 100, "BOX", serial="SER1")  # added in the NetBox UI
    _device(f, 101, "KNOWN", serial="SER2")
    eng._lookup_devices(serials=["SER2"])
    f.log.clear()
    res = eng.sync_devices([{"ip": "", "mac": "", "hostname": "box", "serial": "SER1"},
                            {"ip": "", "mac": "", "hostname": "known", "serial": "SER2"}],
                           tenant_slug="t1", source="opnsense", defaults={"site": "lab"})
    assert res["status"] == "SUCCESS", res
    assert sorted(f.db["devices"]) == [100, 101]
    lookups = [q for m, p, q, _ in f.log if m == "GET" and p == "/api/dcim/devices/"]
    assert lookups and all("SER2" not in q and "known" not in q for q in lookups), lookups
    assert _listings(f) == []


def test_nw_push_finds_a_device_added_in_netbox_by_its_ip():
    f = _world()
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    _hand_made_device_with_ip(f, "10.0.0.9/24")  # under another name
    f.log.clear()
    res = eng.sync_nw_device(device={"id": "nw-9", "name": "sw9", "address": "10.0.0.9"},
                             interfaces=[], tenant_slug="t1", defaults=NW)
    assert res["status"] == "SUCCESS", res
    assert sorted(f.db["devices"]) == [100]
    assert _listings(f) == []


def _hand_made_device_with_ip(f, address):
    """Device 100 added in the NetBox UI, holding ``address`` on an interface."""
    _device(f, 100, "HAND-MADE")
    f.add("interfaces", {"id": 7, "device_id": 100, "name": "mgmt", "type": "virtual"})
    ip = f.add("ip-addresses", {"address": address, "assigned_object_type": "dcim.interface",
                                "assigned_object_id": 7, "custom_fields": {}})
    f.db["devices"][100]["primary_ip4_id"] = ip["id"]


def test_access_tracker_finds_a_switch_added_in_netbox_by_its_ip():
    f = _world()
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    _hand_made_device_with_ip(f, "10.0.0.254/24")
    eng.sync_access_tracker(
        sessions=[{"mac": "AA:BB:CC:DD:EE:FF", "ip": "10.0.0.5", "nas_ip": "10.0.0.254",
                   "nas_port": "Ethernet1/0/12", "nas_name": "sw-core",
                   "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="t1", defaults={"site": "lab"})
    assert not [d for d in f.db["devices"].values() if "sw-core" in str(d.get("name")).lower()]


def test_replace_sync_does_not_delete_a_device_released_in_netbox():
    # The cached row says opnsense owns it; NetBox says someone took it over.
    f = _world()
    eng = make_engine(NetboxEngine, f)
    feed = dict(tenant_slug="t1", source="opnsense", defaults={"site": "lab"}, replace=True)
    eng.sync_devices([{"ip": "10.0.0.5", "mac": "aa:bb:cc:00:00:05", "hostname": "pc5"}], **feed)
    (dev_id,) = f.db["devices"]
    # An older device without last_seen (one with it is left to the sweep).
    cache = eng._device_cache()
    row = cache.full_row(dev_id)
    del row["custom_fields"]["last_seen"], f.db["devices"][dev_id]["custom_fields"]["last_seen"]
    cache.upsert([row], force=True)
    f.db["devices"][dev_id]["custom_fields"]["discovered_from"] = "manual"
    res = eng.sync_devices([{"ip": "10.0.0.6", "mac": "aa:bb:cc:00:00:06", "hostname": "pc6"}], **feed)
    assert res["deleted"] == 0, res
    assert dev_id in f.db["devices"]


def test_nw_device_deleted_in_netbox_is_recreated_on_its_next_push():
    # Used to return "existing nw device vanished" until the cache caught up.
    f = _world()
    eng = make_engine(NetboxEngine, f)
    push = dict(device={"id": "nw-1", "name": "sw1", "address": "10.0.0.1"},
                interfaces=[], tenant_slug="t1", defaults=NW)
    first = eng.sync_nw_device(**push)["device_id"]
    del f.db["devices"][first]
    res = eng.sync_nw_device(**push)
    assert res["status"] == "SUCCESS" and res["errors"] == 0, res
    assert res["device_id"] != first and res["device_id"] in f.db["devices"]


# ── daily reconcile ─────────────────────────────────────────────────────────

def _ago(seconds):
    """An ISO timestamp ``seconds`` ago."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def _reconcile_world():
    f = _world()
    _device(f, 100, "SW1", serial="S1", discovered_from="Network Devices",
            nw_device_id="nw-1", mac_address="aa:bb:cc:00:00:01", last_seen=_ago(3600))
    _device(f, 101, "HUMAN")
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    return f, eng


def _records():
    with open(os.path.join(netbox_device_cache.CACHE_DIR, "_reconcile.jsonl")) as fh:
        return [json.loads(line) for line in fh]


def test_reconcile_reverts_scanner_fields_and_leaves_human_devices_alone():
    f, eng = _reconcile_world()
    # Edits made in the NetBox UI.
    f.db["devices"][100]["name"] = "SOMEONE-RENAMED-IT"
    f.db["devices"][100]["custom_fields"]["nw_device_id"] = "wrong"
    f.db["devices"][101]["name"] = "HUMAN-RENAMED"
    f.log.clear()
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 1, res
    assert f.db["devices"][100]["name"] == "SW1"
    assert f.db["devices"][100]["custom_fields"]["nw_device_id"] == "nw-1"
    assert f.db["devices"][101]["name"] == "HUMAN-RENAMED"
    writes = [x for x in f.log if x[0] != "GET"]
    assert len(writes) == 1 and [d["id"] for d in writes[0][3]] == [100]
    (rec,) = _records()
    assert rec["action"] == "revert" and rec["tenant"] == "t1"
    assert rec["before"] == {"name": "SOMEONE-RENAMED-IT",
                             "custom_fields": {"nw_device_id": "wrong"}}
    assert rec["after"] == {"name": "SW1", "custom_fields": {"nw_device_id": "nw-1"}}
    # The cache now holds NetBox's copy of the human device.
    assert eng._device_cache().full_row(101)["name"] == "HUMAN-RENAMED"


def test_reconcile_recreates_a_scanner_device_deleted_outside_lm():
    f, eng = _reconcile_world()
    del f.db["devices"][100]
    del f.db["devices"][101]
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 1, res
    (new,) = f.db["devices"].values()
    assert new["name"] == "SW1" and new["serial"] == "S1"
    assert new["custom_fields"]["nw_device_id"] == "nw-1"
    assert new["custom_fields"]["discovered_from"] == "Network Devices"
    # last_seen comes back too, so the staleness sweep can still retire it.
    assert new["custom_fields"]["last_seen"] == f.db["devices"][new["id"]]["custom_fields"]["last_seen"]
    assert new["custom_fields"]["last_seen"]
    assert _records()[0]["action"] == "recreate"
    # The human device is not recreated; it just leaves the cache.
    assert sorted(r["id"] for r in eng._all_devices_cached()) == [new["id"]]


def test_reconcile_leaves_deleted_a_scanner_device_not_seen_for_a_week():
    f, eng = _reconcile_world()
    f.db["devices"][100]["custom_fields"]["last_seen"] = _ago(8 * 86400)
    eng._drop_device_cache()
    eng._all_devices_cached()
    del f.db["devices"][100]
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 0, res
    assert 100 not in {r["id"] for r in eng._all_devices_cached()}
    assert [d["name"] for d in f.db["devices"].values()] == ["HUMAN"]


def test_retagging_a_device_in_netbox_releases_it_from_the_scanner():
    f, eng = _reconcile_world()
    f.db["devices"][100]["custom_fields"]["discovered_from"] = "manual"
    f.db["devices"][100]["name"] = "MINE-NOW"
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 0, res
    assert f.db["devices"][100]["name"] == "MINE-NOW"


def test_a_failed_revert_is_reported_and_tried_again_next_time():
    f, eng = _reconcile_world()
    f.db["devices"][100]["name"] = "DRIFTED"
    f.fail.add(("PATCH", "devices", 100))
    res = eng.reconcile_device_cache("t1")
    assert res["status"] == "ERROR" and res["errors"] == 1, res
    assert f.db["devices"][100]["name"] == "DRIFTED"
    # The cache kept the scanner's value, so the next run reverts it.
    f.fail.clear()
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 1 and f.db["devices"][100]["name"] == "SW1", res


def test_a_failed_recreate_keeps_the_device_for_the_next_reconcile():
    f, eng = _reconcile_world()
    del f.db["devices"][100]
    f.fail.add(("POST", "devices", "create"))
    res = eng.reconcile_device_cache("t1")
    assert res["status"] == "ERROR", res
    # Retried within the hour, not a day later.
    assert eng._device_cache()._meta["reconcile"]["t1"] - time.time() <= 3600
    f.fail.clear()
    assert 100 in {r["id"] for r in eng._device_cache().rows()}
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 1, res
    assert [d["name"] for d in f.db["devices"].values()].count("SW1") == 1


def test_reconcile_leaves_deleted_a_scanner_device_without_last_seen():
    # LLDP-created devices carry no last_seen; their feed recreates them.
    f, eng = _reconcile_world()
    del f.db["devices"][100]["custom_fields"]["last_seen"]
    eng._drop_device_cache()
    eng._all_devices_cached()
    del f.db["devices"][100]
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 0, res
    assert [d["name"] for d in f.db["devices"].values()] == ["HUMAN"]


def test_reconcile_keeps_a_write_the_spoke_made_during_the_listing():
    f, eng = _reconcile_world()
    listing = eng._api_get_all

    def list_after_a_poll(path, params):
        # A scanner poll renames the device while the tenant is being listed.
        eng.nb.dcim.devices.update([{"id": 100, "name": "SW1-POLLED"}])
        return listing(path, params)
    eng._api_get_all = list_after_a_poll
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 0, res
    assert f.db["devices"][100]["name"] == "SW1-POLLED"


def test_reconcile_does_not_recreate_a_device_re_added_under_another_id():
    f, eng = _reconcile_world()
    del f.db["devices"][100]
    _device(f, 200, "SW1-READDED", serial="S1")
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 0, res
    assert sorted(f.db["devices"]) == [101, 200]


def test_reconcile_keeps_a_write_the_spoke_made_after_the_listing():
    f = _world()
    # SW0 (checked first) was deleted in NetBox and is not recreated.
    _device(f, 98, "SW0", discovered_from="Network Devices", last_seen=_ago(8 * 86400))
    _device(f, 100, "SW1", discovered_from="Network Devices", last_seen=_ago(3600))
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    del f.db["devices"][98]
    f.db["devices"][100]["name"] = "DRIFTED"  # edited in the NetBox UI
    seen_recently = eng._seen_recently

    def poll_meanwhile(row):
        # A scanner poll renames SW1 while the reconcile is checking SW0.
        eng.nb.dcim.devices.update([{"id": 100, "name": "SW1-POLLED"}])
        return seen_recently(row)
    eng._seen_recently = poll_meanwhile
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 0, res
    assert f.db["devices"][100]["name"] == "SW1-POLLED"


def test_reading_a_drifted_scanner_device_does_not_adopt_the_drift():
    f, eng = _reconcile_world()
    f.db["devices"][100].update(name="DRIFTED", last_updated=_ago(0))
    eng.nb.dcim.devices.get(100)  # e.g. a poll hydrating the device
    res = eng.reconcile_device_cache("t1")
    assert res["changed"] == 1 and f.db["devices"][100]["name"] == "SW1", res


def test_reconcile_reports_records_it_could_not_write():
    f, eng = _reconcile_world()
    os.mkdir(os.path.join(netbox_device_cache.CACHE_DIR, "_reconcile.jsonl"))
    f.db["devices"][100]["name"] = "DRIFTED"
    res = eng.reconcile_device_cache("t1")
    assert res["status"] == "ERROR" and res["changed"] == 1 and res["errors"] == 0, res


def test_a_failed_tenant_listing_is_retried_within_the_hour():
    f, eng = _reconcile_world()
    f.fail.add(("GET", "devices", "list"))
    with pytest.raises(Exception):
        eng.reconcile_device_cache("t1")
    assert eng._device_cache()._meta["reconcile"]["t1"] - time.time() <= 3600


def test_reconcile_runs_only_for_a_due_tenant():
    f, eng = _reconcile_world()
    assert eng.reconcile_device_cache()["status"] == "SKIPPED"
    cache = eng._device_cache()
    cache._meta["built"] = 0  # built long ago: every tenant is due
    res = eng.reconcile_device_cache()
    assert res["tenant"] == "t1", res
    assert eng.reconcile_device_cache()["tenant"] is None


def test_concurrent_first_reads_page_netbox_once():
    import threading
    import time

    def slow_listing(*_a, **_k):
        time.sleep(0.05)
        return ROWS
    eng = _mock_engine(ROWS)
    eng._api_get_all.side_effect = slow_listing
    threads = [threading.Thread(target=eng._all_devices_cached) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert eng._api_get_all.call_count == 1


def test_global_identities_come_from_the_cache_not_a_listing_per_call():
    eng = _mock_engine(ROWS)
    first, second = {}, {}
    eng._add_global_identities(first, {})
    eng._add_global_identities(second, {})
    assert first == second == {"aa": first["aa"]} and first["aa"]["id"] == 1
    assert eng._api_get_all.call_count == 1


# ── deletes made by LM leave the cache (no recreate by the reconcile) ───────

def _run(eng):
    from netbox_sync import _DeviceSyncRun
    return _DeviceSyncRun(tenant_slug="t1", source_tag="Network Devices",
                          source_of_truth="external", defaults={})


DELETES = {
    "record delete": lambda f, eng: eng.delete_device(100),
    "bulk delete": lambda f, eng: eng.nb.dcim.devices.delete([100]),
    "replace delete": lambda f, eng: eng._delete_device(_run(eng), {"id": 100}, lambda live: True),
    "stub merge": lambda f, eng: eng._merge_device_into(
        eng.nb.dcim.devices.get(101), 100, "SW1", "test"),
    "dedupe": lambda f, eng: eng.dedupe_devices("t1", apply=True),
    "staleness": lambda f, eng: eng.staleness_sweep(stale_days=1, delete_days=1),
}


@pytest.mark.parametrize("how", sorted(DELETES))
def test_a_device_lm_deletes_leaves_the_cache_and_is_not_recreated(how):
    f, eng = _reconcile_world()
    if how == "dedupe":
        # A bare copy of SW1 by MAC: dedupe keeps the more complete device.
        _device(f, 102, "COPY", mac_address="aa:bb:cc:00:00:01")
    if how == "staleness":
        # Retired two days ago, seen three days ago: due for deletion.
        f.db["devices"][100]["status"] = "offline"
        f.db["devices"][100]["custom_fields"].update(
            last_seen=_ago(3 * 86400), decommissioned_at=_ago(2 * 86400))
    eng._drop_device_cache()
    eng._all_devices_cached()
    before = set(f.db["devices"])
    DELETES[how](f, eng)
    gone = before - set(f.db["devices"])
    assert gone, how
    assert not gone & {r["id"] for r in eng._all_devices_cached()}
    eng.reconcile_device_cache("t1")
    assert set(f.db["devices"]) == before - gone


# ── global identities never let a sync write another tenant's device ────────

def _lldp_engine(f):
    eng = make_engine(NetboxEngine, f)
    eng._ensure_device_role = MagicMock(return_value=MagicMock(id=31))
    eng._ensure_device_type = MagicMock(return_value=MagicMock(id=41))
    eng._resolve_site = MagicMock(return_value=MagicMock(id=21))
    eng.cabled = []

    def cable(a, a_port, b, b_port, status="connected", port_index=None):
        eng.cabled.append((a.id, b.id))
        return {"status": "SUCCESS"}
    eng._cable_devices = cable
    return eng


MAC = "aa:bb:cc:00:00:09"
LINK = [{"a": {"name": "sw1"}, "a_port": "1", "b": {"name": "nbr.example.com", "macs": [MAC]},
         "b_port": "eth0"}]


def test_lldp_picks_the_tenants_device_when_two_tenants_hold_the_mac():
    f = _world()
    _device(f, 50, "SW1")
    eng = _lldp_engine(f)
    eng._all_devices_cached()
    # Added in NetBox since the cache was built; t2's copy sorts first.
    _device(f, 100, "AAA", tenant_id=2, serial="S9", mac_address=MAC)
    _device(f, 101, "ZZZ", tenant_id=1, serial="S9", mac_address=MAC)
    res = eng.sync_lldp_links(LINK, tenant_slug="t1")
    assert res["cabled"] == 1 and res["created"] == 0, res
    assert eng.cabled == [(50, 101)]
    assert f.db["devices"][100]["name"] == "AAA"


@pytest.mark.parametrize("cached", [True, False])
def test_lldp_leaves_another_tenants_device_alone(cached):
    f = _world()
    _device(f, 50, "SW1")
    _device(f, 100, f"DEVICE-{MAC}", tenant_id=2, serial="S9", mac_address=MAC)
    if not cached:
        del f.db["devices"][100]
    eng = _lldp_engine(f)
    eng._all_devices_cached()
    if not cached:  # added in NetBox since the cache was built
        _device(f, 100, f"DEVICE-{MAC}", tenant_id=2, serial="S9", mac_address=MAC)
    f.log.clear()
    res = eng.sync_lldp_links(LINK, tenant_slug="t1")
    assert res["cabled"] == 0 and res["created"] == 0 and res["skipped"] == 1, res
    assert eng.cabled == [] and sorted(f.db["devices"]) == [50, 100]
    assert not [x for x in f.log if x[0] != "GET"]
    assert f.db["devices"][100]["name"] == f"DEVICE-{MAC}"


def test_access_tracker_does_not_use_another_tenants_switch_on_the_same_ip():
    f = _world()
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    _hand_made_device_with_ip(f, "10.0.0.254/24")
    f.db["devices"][100]["tenant_id"] = 2
    eng.sync_access_tracker(
        sessions=[{"mac": "AA:BB:CC:DD:EE:FF", "ip": "10.0.0.5", "nas_ip": "10.0.0.254",
                   "nas_port": "Ethernet1/0/12", "nas_name": "sw-core",
                   "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="t1", defaults={"site": "lab"})
    (switch,) = [d for d in f.db["devices"].values() if "sw-core" in str(d.get("name")).lower()]
    assert switch["tenant_id"] == 1
    assert not [i for i in f.db["interfaces"].values() if i["device_id"] == 100 and i["id"] != 7]


def test_lldp_prefers_the_tenants_cached_device_over_an_untenanted_one():
    f = _world()
    _device(f, 50, "SW1")
    _device(f, 60, "AAA", tenant_id=None, mac_address=MAC)  # listed first
    _device(f, 61, "ZZZ", mac_address=MAC)
    eng = _lldp_engine(f)
    res = eng.sync_lldp_links(LINK, tenant_slug="t1")
    assert res["cabled"] == 1, res
    assert eng.cabled == [(50, 61)]


def test_lldp_looks_up_an_unknown_end_by_its_serial():
    f = _world()
    _device(f, 50, "SW1")
    eng = _lldp_engine(f)
    eng._all_devices_cached()
    _device(f, 100, "BOX", serial="S9")  # added in NetBox since the cache was built
    res = eng.sync_lldp_links([{"a": {"name": "sw1"}, "a_port": "1",
                                "b": {"name": "nbr.example.com", "serial": "S9"},
                                "b_port": "eth0"}], tenant_slug="t1")
    assert res["cabled"] == 1 and res["created"] == 0, res
    assert eng.cabled == [(50, 100)]


def test_lldp_refuses_a_tenant_netbox_does_not_have():
    f = _world()
    _device(f, 50, "SW1", tenant_id=2)
    _device(f, 100, f"DEVICE-{MAC}", tenant_id=2, mac_address=MAC)
    eng = _lldp_engine(f)
    eng._all_devices_cached()
    f.log.clear()
    res = eng.sync_lldp_links(LINK, tenant_slug="gone")
    assert res["status"] == "ERROR" and "gone" in res["message"], res
    assert eng.cabled == [] and not [x for x in f.log if x[0] != "GET"]


def test_lldp_hardware_match_beats_the_tenants_name_match():
    # The MAC is another tenant's box; the tenant's device only shares the name.
    f = _world()
    _device(f, 50, "SW1")
    eng = _lldp_engine(f)
    eng._all_devices_cached()
    _device(f, 100, "BOX", tenant_id=2, mac_address=MAC)
    _device(f, 101, "nbr.example.com")
    res = eng.sync_lldp_links(LINK, tenant_slug="t1")
    assert res["cabled"] == 0 and res["created"] == 0, res
    assert eng.cabled == []


def test_lldp_refusal_is_not_reused_for_another_end_with_the_same_name():
    f = _world()
    _device(f, 50, "SW1")
    _device(f, 100, "BOX", tenant_id=2, mac_address=MAC)
    _device(f, 101, "OURS", mac_address="aa:bb:cc:00:00:0a")
    eng = _lldp_engine(f)
    res = eng.sync_lldp_links(LINK + [
        {"a": {"name": "sw1"}, "a_port": "2",
         "b": {"name": "nbr.example.com", "macs": ["aa:bb:cc:00:00:0a"]}, "b_port": "eth0"}],
        tenant_slug="t1")
    assert eng.cabled == [(50, 101)], res


def test_access_tracker_names_its_switch_freely_beside_another_tenants_device():
    f = _world()
    eng = make_engine(NetboxEngine, f)
    eng._all_devices_cached()
    _hand_made_device_with_ip(f, "10.0.0.254/24")
    f.db["devices"][100].update(tenant_id=2, name="sw-core")
    eng.sync_access_tracker(
        sessions=[{"mac": "AA:BB:CC:DD:EE:FF", "ip": "10.0.0.5", "nas_ip": "10.0.0.254",
                   "nas_port": "Ethernet1/0/12", "nas_name": "sw-core",
                   "start_time": "2026-06-30T10:00:00"}],
        tenant_slug="t1", defaults={"site": "lab"})
    assert "sw-core" in [str(d["name"]).lower() for d in f.db["devices"].values()
                         if d["tenant_id"] == 1]

"""The spoke's per-tenant device cache (netbox_device_cache) and the engine
code built on it: the slim index plus per-tenant files, the response hook
that follows our own writes, one rebuild for a missing / corrupt / old cache,
and the daily reconcile of scanner-owned devices."""
import json
import os
import sys
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
                                         "tenant_id="))]


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


@pytest.mark.parametrize("damage", ["corrupt tenant file", "missing meta", "old version",
                                    "missing directory"])
def test_damaged_cache_is_rebuilt_once(damage):
    _mock_engine(ROWS)._all_devices_cached()
    d = netbox_device_cache.CACHE_DIR
    if damage == "corrupt tenant file":
        with open(os.path.join(d, "t1.json"), "w") as fh:
            fh.write("{not json")
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

def _reconcile_world():
    f = _world()
    _device(f, 100, "SW1", serial="S1", discovered_from="Network Devices",
            nw_device_id="nw-1", mac_address="aa:bb:cc:00:00:01")
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
    assert _records()[0]["action"] == "recreate"
    # The human device is not recreated; it just leaves the cache.
    assert sorted(r["id"] for r in eng._all_devices_cached()) == [new["id"]]


def test_reconcile_runs_only_for_a_due_tenant():
    f, eng = _reconcile_world()
    assert eng.reconcile_device_cache() == {"status": "SUCCESS", "tenant": None}
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

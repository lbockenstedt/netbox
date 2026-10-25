"""sync_devices against the REAL pynetbox client and an in-memory NetBox.

``tests/test_sync_devices.py`` pins each matching/ownership rule with
MagicMocks. These tests cover what mocks can't see — the request traffic and
pynetbox's Record semantics that the B3 performance work relies on:

* an unchanged device costs ZERO per-device requests (one live Record per
  device, built from the listing; IPs batch-prefetched);
* custom fields are PATCHed delta-only, so a Record read at batch start can't
  revert another sync's newer value;
* a device matched by two records in one batch sees the first one's writes;
* a FAILED write (e.g. rename 400) doesn't poison later writes to the device;
* the create path still re-reads the new device before setting primary_ip4;
* the single global listing still feeds the tenant + global indexes, and
  falls back to the tenant listing when the global one fails;
* a device deleted by someone else after the listing is a silent no-op.

See fake_netbox.py for the modelled NetBox semantics.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from netbox_engine import NetboxEngine  # noqa: E402
from fake_netbox import FakeNetBox, make_engine  # noqa: E402

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
TENANT = {"id": 1, "slug": "t1", "name": "T1"}


def _world(n_owned=0, page_size=50):
    """A NetBox with tenant t1, site/role/type 'discovered'-ready, and
    ``n_owned`` firewall-owned devices that are fully up to date (name =
    hostname, MAC on device + IP, dns_name set, fresh last_seen)."""
    f = FakeNetBox(page_size=page_size)
    f.add("tenants", TENANT)
    f.add("tenants", {"id": 2, "slug": "t2", "name": "T2"})
    f.add("sites", {"id": 21, "slug": "lab", "name": "Lab"})
    f.add("device-roles", {"id": 31, "slug": "discovered", "name": "Discovered"})
    f.add("device-types", {"id": 41, "slug": "discovered", "model": "Discovered Device"})
    feed = []
    for i in range(n_owned):
        mac = f"aa:bb:cc:00:01:{i:02x}"
        ip = f"10.1.0.{i + 10}"
        host = f"ws-{i:02d}"
        _add_device(f, 100 + i * 3, host.upper(), mac, ip, host,
                    {"discovered_from": "opnsense", "mac_address": mac, "last_seen": NOW})
        feed.append({"ip": ip, "mac": mac, "hostname": host})
    return f, feed


def _add_device(f, dev_id, name, mac, ip, dns, dev_cf, tenant_id=1, ip_cf=None):
    f.add("devices", {"id": dev_id, "name": name, "serial": "", "tenant_id": tenant_id,
                      "site_id": 21, "role_id": 31, "device_type_id": 41,
                      "status": "active", "custom_fields": dict(dev_cf),
                      "primary_ip4_id": None})
    if ip:
        f.add("interfaces", {"id": dev_id + 1, "device_id": dev_id, "name": "mgmt",
                             "type": "other"})
        f.add("ip-addresses", {"id": dev_id + 2, "address": f"{ip}/32", "vrf_id": None,
                               "tenant_id": tenant_id, "dns_name": dns,
                               "assigned_object_type": "dcim.interface",
                               "assigned_object_id": dev_id + 1,
                               "custom_fields": dict(ip_cf if ip_cf is not None else
                                                     {"mac_address": mac, "last_seen": NOW})})
        f.db["devices"][dev_id]["primary_ip4_id"] = dev_id + 2


def _sync(f, feed, **kw):
    eng = make_engine(NetboxEngine, f)
    kw.setdefault("tenant_slug", "t1")
    kw.setdefault("defaults", {"site": "lab"})
    f.log.clear()
    return eng, eng.sync_devices(feed, **kw)


def _writes(f):
    return [x for x in f.log if x[0] != "GET"]


def _gets(f):
    return [x for x in f.log if x[0] == "GET"]


# ── request budget ────────────────────────────────────────────────────────

def test_unchanged_devices_cost_no_per_device_requests():
    # Steady state (the ~2,200 syncs/day case): nothing changed → zero writes,
    # and the GET count does NOT grow with the number of devices (it used to
    # be 1 IP GET + 2 device GETs per device: 88 s for 107 devices).
    counts = {}
    for n in (3, 30):
        f, feed = _world(n_owned=n, page_size=500)
        _, res = _sync(f, feed)
        assert res["status"] == "SUCCESS", res
        assert res["pushed"] == n
        assert _writes(f) == [], _writes(f)
        counts[n] = len(_gets(f))
    assert counts[3] == counts[30], counts
    assert counts[30] <= 8, counts  # tenant, listing, IP prefetch, refs…


def test_device_listing_is_paged_once_for_a_tenant_sync():
    f, feed = _world(n_owned=4, page_size=2)
    _sync(f, feed)
    device_list_gets = [x for x in _gets(f) if x[1] == "/api/dcim/devices/"]
    # 4 devices / page_size 2 = 2 pages — one walk, not tenant + global (4).
    assert len(device_list_gets) == 2, device_list_gets
    assert all("tenant=" not in x[2] for x in device_list_gets)


def test_ip_prefetch_is_chunked_by_id():
    f, feed = _world(n_owned=3, page_size=500)
    _sync(f, feed)
    ip_lists = [x for x in _gets(f) if x[1] == "/api/ipam/ip-addresses/" and "id=" in x[2]]
    assert len(ip_lists) == 1
    assert sorted(ip_lists[0][2].split("&")) == sorted(
        ["id=102", "id=105", "id=108", "limit=500"])


def test_role_and_type_lookups_are_cached_across_syncs():
    f, feed = _world(n_owned=1)
    eng = make_engine(NetboxEngine, f)
    eng.sync_devices(feed, tenant_slug="t1", defaults={"site": "lab"})
    f.log.clear()
    eng.sync_devices(feed, tenant_slug="t1", defaults={"site": "lab"})
    paths = [x[1] for x in _gets(f)]
    assert "/api/dcim/device-roles/" not in paths
    assert "/api/dcim/device-types/" not in paths


# ── delta-only custom fields / live Records ───────────────────────────────

def test_custom_field_patch_sends_only_the_changed_key():
    f, feed = _world(n_owned=1)
    feed[0]["source_switch_port"] = "1/1/7"
    _sync(f, feed)
    dev_patches = [x for x in _writes(f) if x[1] == "/api/dcim/devices/100/"]
    assert dev_patches == [("PATCH", "/api/dcim/devices/100/", "",
                            {"custom_fields": {"switch_port": "1/1/7"}})]
    assert f.db["devices"][100]["custom_fields"]["discovered_from"] == "opnsense"


def test_value_written_by_another_sync_mid_batch_is_not_reverted():
    # The Record is built from the listing at batch start. If another sync
    # (cppm, nw) changes a custom field afterwards, our later PATCH of a
    # DIFFERENT key must not write the listing's old value back.
    f, feed = _world(n_owned=1)
    feed[0]["source_switch_port"] = "1/1/7"
    eng = make_engine(NetboxEngine, f)
    orig = eng._prefetch_primary_ips

    def _concurrent_writer(run, incoming):
        f.db["devices"][100]["custom_fields"]["switch_name"] = "set-by-other-sync"
        return orig(run, incoming)

    eng._prefetch_primary_ips = _concurrent_writer
    eng.sync_devices(feed, tenant_slug="t1", defaults={"site": "lab"})
    cf = f.db["devices"][100]["custom_fields"]
    assert cf["switch_name"] == "set-by-other-sync"
    assert cf["switch_port"] == "1/1/7"


def test_device_matched_twice_in_one_batch_sees_first_records_writes():
    # Two feed rows for one machine (same MAC, a second address): both match
    # device 100 by MAC; the second must build on the first's writes.
    f, feed = _world(n_owned=1)
    feed[0]["source_switch_port"] = "1/1/1"
    feed.append({"ip": "", "mac": feed[0]["mac"], "hostname": "",
                 "source_switch_name": "sw9", "source_switch_port": "1/1/2"})
    _, res = _sync(f, feed)
    assert res["pushed"] == 2, res
    cf = f.db["devices"][100]["custom_fields"]
    assert (cf["switch_name"], cf["switch_port"]) == ("sw9", "1/1/2")
    assert cf["mac_address"] == feed[0]["mac"]
    dev_gets = [x for x in _gets(f) if x[1] == "/api/dcim/devices/100/"]
    assert dev_gets == []  # one live Record from the listing, never re-fetched


def test_failed_rename_does_not_poison_later_writes_to_the_device():
    # Device 100 (ours, "OLD") is renamed to "WS-TAKEN", which a human's device
    # in the same site+tenant already holds → NetBox 400. A failed save keeps
    # the unsent name on the Record; the device's NEXT write (a second feed
    # row stamping switch fields) must not re-send it and fail as well.
    f, _ = _world()
    mac = "aa:bb:cc:00:09:01"
    _add_device(f, 100, "OLD", mac, "10.1.0.9", "old",
                {"discovered_from": "opnsense", "mac_address": mac, "last_seen": NOW})
    f.add("devices", {"id": 200, "name": "WS-TAKEN", "serial": "", "tenant_id": 1,
                      "site_id": 21, "role_id": 31, "device_type_id": 41,
                      "status": "active", "custom_fields": {}, "primary_ip4_id": None})
    feed = [{"ip": "10.1.0.9", "mac": mac, "hostname": "ws-taken"},
            {"ip": "", "mac": mac, "hostname": "", "source_switch_name": "sw1",
             "source_switch_port": "7"}]
    _sync(f, feed)
    assert f.db["devices"][100]["name"] == "OLD"  # the rename really failed …
    cf = f.db["devices"][100]["custom_fields"]
    assert (cf.get("switch_name"), cf.get("switch_port")) == ("sw1", "7")  # … the stamp didn't


def test_create_path_rereads_the_new_device_before_setting_primary_ip():
    # History: re-sending the best-effort custom fields stamped on the create
    # response 400'd on NetBoxes missing those fields and left every new
    # device without primary_ip4 (→ re-created every sync). Keep the fresh GET.
    f, _ = _world()
    _, res = _sync(f, [{"ip": "10.9.9.9", "mac": "aa:bb:cc:09:09:09", "hostname": "new-box"}])
    assert res["pushed"] == 1, res
    new_id = next(d["id"] for d in f.db["devices"].values() if d["name"] == "NEW-BOX")
    log = [(m, p) for m, p, _q, _b in f.log]
    i_get = log.index(("GET", f"/api/dcim/devices/{new_id}/"))
    patch = f.log[i_get + 1]
    assert patch[:2] == ("PATCH", f"/api/dcim/devices/{new_id}/")
    assert set(patch[3]) == {"primary_ip4"}
    assert f.db["devices"][new_id]["primary_ip4_id"] is not None


# ── listing / identities / deletes ────────────────────────────────────────

def test_other_tenants_mac_is_adopted_and_tenant_rows_win():
    # 2d95909: MAC is global — a t1 record whose MAC only t2 has recorded
    # updates t2's device instead of minting a duplicate.
    f, _ = _world()
    mac = "aa:bb:cc:00:02:02"
    _add_device(f, 300, "DEVICE-AABBCC000202", mac, "", "",
                {"discovered_from": "Network Devices", "mac_address": mac}, tenant_id=2)
    _, res = _sync(f, [{"ip": "", "mac": mac, "hostname": ""}])
    assert res["pushed"] == 1, res
    assert len(f.db["devices"]) == 1


def test_global_listing_failure_falls_back_to_the_tenant_listing():
    f, feed = _world(n_owned=2)
    f.fail.add(("GET", "devices", "list-global"))
    _, res = _sync(f, feed)
    assert res["status"] == "SUCCESS", res
    assert res["pushed"] == 2 and len(f.db["devices"]) == 2  # matched, not duplicated
    assert any(x[1] == "/api/dcim/devices/" and "tenant=t1" in x[2] for x in _gets(f))


def test_replace_delete_of_a_device_already_gone_is_a_silent_noop():
    f, feed = _world(n_owned=2)
    eng = make_engine(NetboxEngine, f)
    orig = eng._index_sync_rows

    def _someone_deletes_it(run, rows):
        orig(run, rows)
        del f.db["devices"][103]  # gone after our listing

    eng._index_sync_rows = _someone_deletes_it
    res = eng.sync_devices(feed[:1], tenant_slug="t1", replace=True,
                           defaults={"site": "lab"})
    assert res["status"] == "SUCCESS", res
    assert (res["deleted"], res["errors"]) == (0, 0)


def test_replace_delete_removes_vanished_owned_device_and_its_ip():
    f, feed = _world(n_owned=2)
    _, res = _sync(f, feed[:1], replace=True)
    assert res["deleted"] == 1, res
    assert 103 not in f.db["devices"] and 105 not in f.db["ip-addresses"]
    assert 100 in f.db["devices"]


def test_netbox_source_of_truth_fills_only_empty_values_delta_only():
    # c245c7e: NetBox authoritative → fill EMPTY mac only; the PATCH carries
    # just that key (never the rest of the listing's custom fields).
    f, _ = _world()
    _add_device(f, 100, "KEEP-NAME", "", "10.1.0.5", "", {"discovered_from": "human"},
                ip_cf={"mac_address": "11:11:11:11:11:11", "last_seen": NOW})
    _sync(f, [{"ip": "10.1.0.5", "mac": "aa:bb:cc:05:05:05", "hostname": "x"}],
          source_of_truth="netbox")
    dev = f.db["devices"][100]
    assert dev["name"] == "KEEP-NAME"
    assert dev["custom_fields"]["mac_address"] == "aa:bb:cc:05:05:05"
    assert f.db["ip-addresses"][102]["custom_fields"]["mac_address"] == "11:11:11:11:11:11"
    assert f.db["ip-addresses"][102]["dns_name"] == "x"  # empty → filled
    dev_patches = [x[3] for x in _writes(f) if x[1] == "/api/dcim/devices/100/"]
    assert {"custom_fields": {"mac_address": "aa:bb:cc:05:05:05"}} in dev_patches

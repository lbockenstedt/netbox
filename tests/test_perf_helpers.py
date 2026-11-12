"""Unit tests for the perf-fix helpers (pure logic, no NetBox API needed).

FIX A — last_seen quantization: ``_last_seen_stale`` decides whether the
stored ``last_seen`` custom field needs a rewrite (missing/unparsable/≥24h off)
and ``_stamp_last_seen`` skips the save entirely while the stamp is fresh, so
a no-op sync produces zero PATCHes. The staleness sweep's thresholds are days
(7d/30d), so day resolution loses nothing.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── _last_seen_stale: the rewrite decision ────────────────────────────────────

def test_last_seen_stale_missing_or_empty_is_stale():
    stale = NetboxEngine._last_seen_stale
    assert stale(None, now=NOW) is True
    assert stale("", now=NOW) is True
    assert stale("   ", now=NOW) is True


def test_last_seen_stale_unparsable_is_stale():
    stale = NetboxEngine._last_seen_stale
    assert stale("not-a-timestamp", now=NOW) is True
    assert stale("2026-13-45T99:00:00Z", now=NOW) is True
    assert stale(12345, now=NOW) is True   # non-string garbage


def test_last_seen_fresh_within_the_hour_is_not_stale():
    stale = NetboxEngine._last_seen_stale
    assert stale(_iso(NOW - timedelta(minutes=5)), now=NOW) is False
    assert stale(_iso(NOW - timedelta(hours=23)), now=NOW) is False
    assert stale(_iso(NOW), now=NOW) is False


def test_last_seen_older_than_an_hour_is_stale():
    stale = NetboxEngine._last_seen_stale
    assert stale(_iso(NOW - timedelta(hours=24)), now=NOW) is True    # boundary
    assert stale(_iso(NOW - timedelta(hours=30)), now=NOW) is True
    assert stale(_iso(NOW - timedelta(days=8)), now=NOW) is True


def test_last_seen_far_future_is_stale_clock_skew_cant_freeze_it():
    # A clock-skewed FUTURE stamp must not freeze the signal forever.
    stale = NetboxEngine._last_seen_stale
    assert stale(_iso(NOW + timedelta(hours=30)), now=NOW) is True
    assert stale(_iso(NOW + timedelta(minutes=30)), now=NOW) is False


def test_last_seen_accepts_offset_and_naive_formats():
    stale = NetboxEngine._last_seen_stale
    # Explicit offset form (what a non-LM writer might store).
    assert stale("2026-07-17T11:30:00+00:00", now=NOW) is False
    # Naive timestamp → treated as UTC.
    assert stale("2026-07-17T11:30:00", now=NOW) is False
    assert stale("2026-07-16T09:00:00", now=NOW) is True


# ── _stamp_last_seen: skip the save while fresh ───────────────────────────────

def _engine():
    return NetboxEngine("http://localhost", "tok")


class _Obj:
    def __init__(self, custom_fields=None):
        self.custom_fields = dict(custom_fields or {})
        self.save = MagicMock()


def test_stamp_last_seen_skips_save_when_fresh():
    eng = _engine()
    fresh = _iso(datetime.now(timezone.utc) - timedelta(minutes=10))
    obj = _Obj({"last_seen": fresh})
    eng._stamp_last_seen(obj)
    obj.save.assert_not_called()
    assert obj.custom_fields["last_seen"] == fresh   # untouched


def test_stamp_last_seen_writes_when_missing():
    eng = _engine()
    obj = _Obj({})
    eng._stamp_last_seen(obj)
    obj.save.assert_called_once()
    assert obj.custom_fields.get("last_seen")


def test_stamp_last_seen_writes_when_old_or_unparsable():
    eng = _engine()
    old = _Obj({"last_seen": _iso(datetime.now(timezone.utc) - timedelta(days=2))})
    eng._stamp_last_seen(old)
    old.save.assert_called_once()

    junk = _Obj({"last_seen": "garbage"})
    eng._stamp_last_seen(junk)
    junk.save.assert_called_once()
    assert junk.custom_fields["last_seen"] != "garbage"


def test_stamp_last_seen_explicit_when_still_quantized():
    # An explicit ``when`` (e.g. the access-tracker session start_time) is still
    # subject to quantization: a fresh stored stamp skips the write.
    eng = _engine()
    fresh = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    obj = _Obj({"last_seen": fresh})
    eng._stamp_last_seen(obj, when=_iso(datetime.now(timezone.utc)))
    obj.save.assert_not_called()


def test_stamp_last_seen_never_raises():
    eng = _engine()
    obj = _Obj({})
    obj.save.side_effect = Exception("field unprovisioned")
    eng._stamp_last_seen(obj)   # swallowed at DEBUG — must not raise


# ── FIX C: local longest-prefix mask resolution ───────────────────────────────

import ipaddress  # noqa: E402


def _nets(*cidrs):
    return [ipaddress.ip_network(c) for c in cidrs]


def test_mask_from_prefixes_longest_match_wins():
    nets = _nets("10.0.0.0/8", "10.1.0.0/16", "10.1.2.0/24")
    assert NetboxEngine._mask_from_prefixes("10.1.2.5", nets) == "24"
    assert NetboxEngine._mask_from_prefixes("10.1.9.5", nets) == "16"
    assert NetboxEngine._mask_from_prefixes("10.9.9.9", nets) == "8"


def test_mask_from_prefixes_uncovered_or_invalid_returns_none():
    nets = _nets("10.0.0.0/8")
    assert NetboxEngine._mask_from_prefixes("192.168.1.1", nets) is None
    assert NetboxEngine._mask_from_prefixes("not-an-ip", nets) is None
    assert NetboxEngine._mask_from_prefixes("10.0.0.1", []) is None
    assert NetboxEngine._mask_from_prefixes("10.0.0.1", None) is None


def test_mask_from_prefixes_family_mismatch_never_matches():
    nets = _nets("fd00::/8")
    assert NetboxEngine._mask_from_prefixes("10.0.0.1", nets) is None
    assert NetboxEngine._mask_from_prefixes("fd00::1", nets) == "8"


def test_mask_from_prefixes_strips_inline_mask():
    nets = _nets("10.1.2.0/24")
    assert NetboxEngine._mask_from_prefixes("10.1.2.5/32", nets) == "24"


def test_mask_for_ip_uses_prefetch_no_api_call():
    eng = _engine()
    eng._api_get = MagicMock()
    eng._prefix_prefetch = _nets("10.1.2.0/24")
    assert eng._mask_for_ip("10.1.2.5") == "24"
    eng._api_get.assert_not_called()          # resolved locally


def test_mask_for_ip_uncovered_falls_back_to_api():
    eng = _engine()
    eng._api_get = MagicMock(return_value={"results": [{"prefix": "192.168.0.0/16"}]})
    eng._prefix_prefetch = _nets("10.1.2.0/24")
    assert eng._mask_for_ip("192.168.1.1") == "16"   # per-IP API fallback kept
    eng._api_get.assert_called_once()


def test_mask_for_ip_without_prefetch_unchanged():
    eng = _engine()
    eng._api_get = MagicMock(return_value={"results": []})
    assert eng._mask_for_ip("10.1.2.5") == "32"      # no containing prefix → /32
    eng._api_get.assert_called_once()


def test_begin_end_prefix_prefetch_lifecycle():
    eng = _engine()
    eng._api_get_all = MagicMock(return_value=[
        {"prefix": "10.0.0.0/8"}, {"prefix": "bogus"}, {}])
    eng._begin_prefix_prefetch()
    assert [str(n) for n in eng._prefix_prefetch] == ["10.0.0.0/8"]
    eng._end_prefix_prefetch()
    assert eng._prefix_prefetch is None


def test_begin_prefix_prefetch_failure_leaves_fallback():
    eng = _engine()
    eng._api_get_all = MagicMock(side_effect=Exception("boom"))
    eng._begin_prefix_prefetch()
    assert eng._prefix_prefetch is None       # → per-IP API path everywhere


# ── _add_global_identities: the global listing is reused within the TTL ──────

def test_global_identity_listing_is_cached_between_calls(monkeypatch):
    import netbox_sync

    eng = NetboxEngine.__new__(NetboxEngine)
    eng._api_get_all = MagicMock(return_value=[
        {"id": 1, "custom_fields": {"mac_address": "aa:bb:cc:dd:ee:01"}, "serial": "S1"}])

    first, second = {}, {}
    eng._add_global_identities(first, {})
    eng._add_global_identities(second, {})
    assert eng._api_get_all.call_count == 1
    assert first == second and first

    # Once the cache is older than the TTL the listing is fetched again.
    monkeypatch.setattr(netbox_sync, "_GLOBAL_IDENTITY_TTL_S", 0)
    eng._add_global_identities({}, {})
    assert eng._api_get_all.call_count == 2


def test_concurrent_device_listing_misses_fetch_once():
    # Several commands missing the cache at the same moment used to each page
    # every device; now one fetches and the rest reuse its listing.
    import threading
    import time as _time

    eng = NetboxEngine.__new__(NetboxEngine)

    def slow_listing(*_a, **_k):
        _time.sleep(0.05)
        return [{"id": 1}]
    eng._api_get_all = MagicMock(side_effect=slow_listing)
    threads = [threading.Thread(target=eng._all_devices_cached) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert eng._api_get_all.call_count == 1


def test_nw_store_submits_dedupes_and_tracks_due(tmp_path):
    from netbox_nw_store import NwInventoryStore
    path = str(tmp_path / "nw.json")
    store = NwInventoryStore(path)
    rec = {"device": {"id": "d1", "name": "sw1"},
           "interfaces": [{"name": "1/1", "mac": "aa", "rx_bytes": 5}]}
    assert store.submit(rec) is True
    assert [k for k, _ in store.due()] == ["d1"]
    store.mark_applied("d1", store.entries["d1"]["sig"], {"site": "s"})
    assert store.due() == []
    # Counters are not part of the signature; same inventory is not a change.
    rec2 = {**rec, "interfaces": [{"name": "1/1", "mac": "aa", "rx_bytes": 9}]}
    assert store.submit(rec2) is False
    # A real change makes it due again and keeps the last result for the ack.
    rec3 = {**rec, "interfaces": [{"name": "1/1", "mac": "bb"}]}
    assert store.submit(rec3) is True
    assert [k for k, _ in store.due()] == ["d1"]
    assert store.last_result("d1") == {"site": "s"}
    # Survives a restart.
    assert NwInventoryStore(path).entries["d1"]["sig"] == store.entries["d1"]["sig"]


def _nightly_store(tmp_path, **kw):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from netbox_nw_store import NwInventoryStore
    clock = [datetime(2026, 11, 9, 20, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()]
    store = NwInventoryStore(str(tmp_path / "nw.json"), clock=lambda: clock[0], **kw)
    for k in ("a", "b"):
        store.submit({"device": {"id": k, "name": k}, "interfaces": []})
        store.mark_applied(k, store.entries[k]["sig"], {})
        store.entries[k]["applied_at"] = clock[0]
    return store, clock


def test_nw_store_due_lists_changed_before_nightly_heal(tmp_path):
    store, clock = _nightly_store(tmp_path)
    store.submit({"device": {"id": "b", "name": "b-renamed"}, "interfaces": []})
    clock[0] += 8 * 3600  # past 03:00 Pacific next day
    assert [k for k, _ in store.due()] == ["b", "a"]


def test_nw_store_heals_unchanged_devices_only_after_3am_pacific(tmp_path):
    store, clock = _nightly_store(tmp_path, stale_batch=1)
    clock[0] += 5 * 3600  # 01:00 Pacific: not yet
    assert store.due() == []
    clock[0] += 3 * 3600  # 04:00 Pacific
    assert len(store.due()) == 1  # trickled
    for k in ("a", "b"):
        store.entries[k]["applied_at"] = clock[0]
    assert store.due() == []  # healed tonight; next heal is tomorrow's 3 AM


def test_nw_store_restart_does_not_trigger_an_immediate_heal(tmp_path):
    store, clock = _nightly_store(tmp_path)
    for k in ("a", "b"):
        store.entries[k]["applied_at"] = clock[0] - 86400 * 3
    assert store.due() == []


def _chg(name, pre, post, t="dcim.device", action="update", oid=None):
    """Build one object-change row for the churn tests."""
    return {"changed_object_id": oid, "changed_object_type": t, "action": {"value": action}, "object_repr": name,
            "prechange_data": {"custom_fields": pre}, "postchange_data": {"custom_fields": post}}


def test_churn_same_name_different_ids_not_flap():
    from netbox_churn import analyze_changes
    rows = [_chg("d", {"f": "B"}, {"f": "A"}, oid=2),
            _chg("d", {"f": "A"}, {"f": "B"}, oid=1)]
    assert analyze_changes(rows, 1800)["flapping"]["objects"] == 0


def test_churn_null_vs_none_string_and_malformed():
    from netbox_churn import analyze_changes
    rows = [_chg("d", {"f": "None"}, {"f": None}, oid=1),
            _chg("d", {"f": None}, {"f": "None"}, oid=1),
            {"changed_object_type": "dcim.device", "action": "update",
             "prechange_data": "x", "postchange_data": []}]
    assert analyze_changes(rows, 1800)["flapping"]["objects"] == 1


def test_churn_report_capped_uses_next_link():
    from netbox_churn import ChurnMixin

    class E(ChurnMixin):
        def _api_get_all(self, path, params=None, max_pages=200, meta=None):
            meta["more"] = True
            return []
    assert E().churn_report()["capped"] is True


def test_churn_report_detects_flapping_and_rate():
    from netbox_churn import analyze_changes
    # newest first, as NetBox returns: A->B, then B->A (flap); other device changes once.
    rows = [_chg("d1", {"switch_ip": "B"}, {"switch_ip": "A"}),
            _chg("d1", {"switch_ip": "A"}, {"switch_ip": "B"}),
            _chg("d2", {"switch_ip": "A"}, {"switch_ip": "B"}),
            _chg("DEVICE-x", {}, {}, action="create")]
    r = analyze_changes(rows, 1800)
    assert r["writes_per_hour"] == 8
    assert r["flapping"]["objects"] == 1 and r["flapping"]["sample"][0]["name"] == "d1"
    assert r["top_fields"][0] == ["switch_ip", 3] and r["stub_creates"] == 1


def test_device_listing_refresh_fetches_only_changed_devices(monkeypatch):
    # The shared listing was re-paged in full every minute (~15 NetBox pages).
    # After the TTL only devices updated since the newest held row are read,
    # and merged by id; a full re-read still happens every _GLOBAL_FULL_RELIST_S.
    import netbox_sync
    eng = NetboxEngine.__new__(NetboxEngine)
    full = [{"id": 1, "name": "a", "last_updated": "2026-11-12T01:00:00Z"},
            {"id": 2, "name": "b", "last_updated": "2026-11-12T01:04:00Z"}]
    changed = [{"id": 2, "name": "b2", "last_updated": "2026-11-12T01:10:00Z"},
               {"id": 3, "name": "c", "last_updated": "2026-11-12T01:11:00Z"}]
    calls = []

    def api_get_all(path, params):
        calls.append(dict(params))
        return changed if "last_updated__gte" in params else full
    eng._api_get_all = api_get_all

    assert eng._all_devices_cached() == full
    monkeypatch.setattr(netbox_sync, "_GLOBAL_IDENTITY_TTL_S", 0)
    rows = eng._all_devices_cached()
    assert calls[1] == {"limit": 500, "last_updated__gte": "2026-11-12T01:04:00Z"}
    assert sorted((r["id"], r["name"]) for r in rows) == [(1, "a"), (2, "b2"), (3, "c")]

    monkeypatch.setattr(netbox_sync, "_GLOBAL_FULL_RELIST_S", 0)
    eng._all_devices_cached()
    assert calls[2] == {"limit": 500}
    # An invalidation (create/delete) forces a full read too.
    monkeypatch.setattr(netbox_sync, "_GLOBAL_FULL_RELIST_S", 900)
    eng._drop_device_cache()
    eng._all_devices_cached()
    assert calls[3] == {"limit": 500}


def test_device_listing_is_kept_on_disk_not_in_memory():
    # The listing used to stay in the spoke's memory, and every restart paged
    # every device again. It now lives in a JSON file: once callers drop it
    # nothing holds it, it is re-read from disk within the TTL, and a new
    # engine (restart) only asks NetBox for devices changed since the file.
    import gc
    row = {"id": 1, "last_updated": "2026-11-12T01:00:00Z"}
    eng = NetboxEngine.__new__(NetboxEngine)
    eng._api_get_all = MagicMock(return_value=[row])
    rows = eng._all_devices_cached()
    del rows
    gc.collect()
    assert eng._global_devices_held[0]() is None
    assert eng._all_devices_cached() == [row]
    assert eng._api_get_all.call_count == 1

    restarted = NetboxEngine.__new__(NetboxEngine)
    restarted._api_get_all = MagicMock(return_value=[row])
    assert restarted._all_devices_cached() == [row]
    restarted._api_get_all.assert_called_once_with(
        "/api/dcim/devices/", {"limit": 500, "last_updated__gte": "2026-11-12T01:00:00Z"})


def test_device_create_rechecks_changes_instead_of_reading_everything():
    # A create used to throw the listing away, so the next lookup paged all
    # ~7,000 devices. The new device is the newest, so a change fetch finds it.
    row = {"id": 1, "last_updated": "2026-11-12T01:00:00Z"}
    new = {"id": 2, "last_updated": "2026-11-12T01:05:00Z"}
    eng = NetboxEngine.__new__(NetboxEngine)
    eng._api_get_all = MagicMock(return_value=[row])
    eng._all_devices_cached()
    eng._recheck_device_cache()
    eng._api_get_all.return_value = [row, new]
    assert eng._all_devices_cached() == [row, new]
    assert eng._api_get_all.call_args.args[1] == {"limit": 500, "last_updated__gte": "2026-11-12T01:00:00Z"}

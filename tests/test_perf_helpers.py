"""Unit tests for the perf-fix helpers (pure logic, no NetBox API needed).

FIX A — last_seen quantization: ``_last_seen_stale`` decides whether the
stored ``last_seen`` custom field needs a rewrite (missing/unparsable/≥1h off)
and ``_stamp_last_seen`` skips the save entirely while the stamp is fresh, so
a no-op sync produces zero PATCHes. The staleness sweep's thresholds are days
(7d/30d), so hour resolution loses nothing.
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
    assert stale(_iso(NOW - timedelta(minutes=59)), now=NOW) is False
    assert stale(_iso(NOW), now=NOW) is False


def test_last_seen_older_than_an_hour_is_stale():
    stale = NetboxEngine._last_seen_stale
    assert stale(_iso(NOW - timedelta(hours=1)), now=NOW) is True     # boundary
    assert stale(_iso(NOW - timedelta(hours=2)), now=NOW) is True
    assert stale(_iso(NOW - timedelta(days=8)), now=NOW) is True


def test_last_seen_far_future_is_stale_clock_skew_cant_freeze_it():
    # A clock-skewed FUTURE stamp must not freeze the signal forever.
    stale = NetboxEngine._last_seen_stale
    assert stale(_iso(NOW + timedelta(hours=2)), now=NOW) is True
    assert stale(_iso(NOW + timedelta(minutes=30)), now=NOW) is False


def test_last_seen_accepts_offset_and_naive_formats():
    stale = NetboxEngine._last_seen_stale
    # Explicit offset form (what a non-LM writer might store).
    assert stale("2026-07-17T11:30:00+00:00", now=NOW) is False
    # Naive timestamp → treated as UTC.
    assert stale("2026-07-17T11:30:00", now=NOW) is False
    assert stale("2026-07-17T09:00:00", now=NOW) is True


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

"""Tests for the KEA diff-push (perf FIX B).

The old loop re-POSTed subnet4-add for EVERY DHCP scope every 300s tick — Kea
rejects an existing subnet, so that was a guaranteed rejected POST per scope
per tick. The fix keeps a prefix→gateway map of scopes last successfully
pushed, diffs each tick, and only sends deltas; any connection error or
unexpected rejection clears the map so the next tick full-syncs (covers Kea
restarts). Scope REMOVAL (prefix deleted from NetBox) is now handled too — it
wasn't at all before.

Pure logic + async orchestration with mocked Kea calls — no live Kea needed.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_spoke import NetboxSpoke  # noqa: E402


def _scope(prefix, gateway=None):
    s = {"prefix": prefix}
    if gateway:
        s["gateway"] = gateway
    return s


# ── _kea_scope_diff: the pure diff ────────────────────────────────────────────

def test_diff_first_tick_pushes_everything():
    desired = {"10.0.0.0/24": _scope("10.0.0.0/24"),
               "10.1.0.0/24": _scope("10.1.0.0/24", "10.1.0.1")}
    to_add, to_remove = NetboxSpoke._kea_scope_diff(desired, {})
    assert sorted(to_add) == ["10.0.0.0/24", "10.1.0.0/24"]
    assert to_remove == []


def test_diff_steady_state_is_empty():
    desired = {"10.0.0.0/24": _scope("10.0.0.0/24", "10.0.0.1")}
    synced = {"10.0.0.0/24": "10.0.0.1"}
    to_add, to_remove = NetboxSpoke._kea_scope_diff(desired, synced)
    assert to_add == [] and to_remove == []   # zero Kea POSTs on a no-op tick


def test_diff_default_gateway_matches_tracked_default():
    # A scope with no gateway is pushed with the default; the tracked value is
    # that default, so the next tick must NOT see a phantom delta.
    desired = {"10.0.0.0/24": _scope("10.0.0.0/24")}
    synced = {"10.0.0.0/24": NetboxSpoke._KEA_DEFAULT_GATEWAY}
    assert NetboxSpoke._kea_scope_diff(desired, synced) == ([], [])


def test_diff_removed_prefix_is_deleted():
    desired = {"10.0.0.0/24": _scope("10.0.0.0/24", "10.0.0.1")}
    synced = {"10.0.0.0/24": "10.0.0.1", "10.9.0.0/24": "10.9.0.1"}
    to_add, to_remove = NetboxSpoke._kea_scope_diff(desired, synced)
    assert to_add == []
    assert to_remove == ["10.9.0.0/24"]


def test_diff_gateway_change_is_remove_then_add():
    desired = {"10.0.0.0/24": _scope("10.0.0.0/24", "10.0.0.254")}
    synced = {"10.0.0.0/24": "10.0.0.1"}
    to_add, to_remove = NetboxSpoke._kea_scope_diff(desired, synced)
    assert to_add == ["10.0.0.0/24"]
    assert to_remove == ["10.0.0.0/24"]


# ── _kea_apply_scopes: orchestration with mocked Kea calls ───────────────────

def _spoke(synced=None):
    """A NetboxSpoke shell (no __init__ — no engine/cert/env side effects) with
    just the state _kea_apply_scopes needs."""
    sp = object.__new__(NetboxSpoke)
    sp._kea_synced = dict(synced or {})
    sp._sync_scope_to_kea = AsyncMock(return_value=True)
    sp._remove_scope_from_kea = AsyncMock(return_value=True)
    return sp


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_apply_steady_state_sends_nothing():
    sp = _spoke({"10.0.0.0/24": "10.0.0.1"})
    _run(sp._kea_apply_scopes([_scope("10.0.0.0/24", "10.0.0.1")]))
    sp._sync_scope_to_kea.assert_not_awaited()
    sp._remove_scope_from_kea.assert_not_awaited()
    assert sp._kea_synced == {"10.0.0.0/24": "10.0.0.1"}


def test_apply_adds_only_new_scopes_and_tracks_them():
    sp = _spoke({"10.0.0.0/24": "10.0.0.1"})
    _run(sp._kea_apply_scopes([_scope("10.0.0.0/24", "10.0.0.1"),
                               _scope("10.2.0.0/24", "10.2.0.1")]))
    assert sp._sync_scope_to_kea.await_count == 1
    assert sp._sync_scope_to_kea.await_args.args[0]["prefix"] == "10.2.0.0/24"
    assert sp._kea_synced == {"10.0.0.0/24": "10.0.0.1", "10.2.0.0/24": "10.2.0.1"}


def test_apply_removes_scope_deleted_in_netbox():
    sp = _spoke({"10.0.0.0/24": "10.0.0.1", "10.9.0.0/24": "10.9.0.1"})
    _run(sp._kea_apply_scopes([_scope("10.0.0.0/24", "10.0.0.1")]))
    sp._remove_scope_from_kea.assert_awaited_once_with("10.9.0.0/24")
    assert "10.9.0.0/24" not in sp._kea_synced


def test_apply_connection_error_clears_state_for_full_resync():
    sp = _spoke({"10.0.0.0/24": "10.0.0.1"})
    sp._sync_scope_to_kea = AsyncMock(return_value=False)   # conn error/reject
    _run(sp._kea_apply_scopes([_scope("10.0.0.0/24", "10.0.0.1"),
                               _scope("10.2.0.0/24", "10.2.0.1")]))
    # Pushed state forgotten → next tick re-pushes everything.
    assert sp._kea_synced == {}


def test_apply_locally_invalid_scope_neither_tracked_nor_resyncs():
    sp = _spoke({"10.0.0.0/24": "10.0.0.1"})
    sp._sync_scope_to_kea = AsyncMock(return_value=None)    # unparsable prefix
    _run(sp._kea_apply_scopes([_scope("10.0.0.0/24", "10.0.0.1"),
                               _scope("not-a-prefix")]))
    assert "not-a-prefix" not in sp._kea_synced
    assert sp._kea_synced == {"10.0.0.0/24": "10.0.0.1"}    # no full-resync wipe


def test_apply_failed_removal_clears_state():
    sp = _spoke({"10.9.0.0/24": "10.9.0.1"})
    sp._remove_scope_from_kea = AsyncMock(return_value=False)
    _run(sp._kea_apply_scopes([]))
    assert sp._kea_synced == {}    # forgotten → full-resync next tick

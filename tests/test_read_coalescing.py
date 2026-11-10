"""Identical concurrent commands share one run; reads are briefly reused; writes clear them."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import netbox_spoke as spoke_mod  # noqa: E402


def _spoke(calls):
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)

    async def inner(cmd, data):
        calls.append(cmd)
        n = len(calls)
        await asyncio.sleep(0.05 if "GET" in cmd else 0.01)
        return {"status": "SUCCESS", "n": n}
    sp._handle_command = inner
    return sp


def test_identical_prefix_reads_share_one_fetch_and_writes_clear_it():
    calls = []
    sp = _spoke(calls)

    async def run():
        a, b, c = await asyncio.gather(
            sp.handle_command("NETBOX_GET_PREFIXES", {"tenant": "x"}),
            sp.handle_command("NETBOX_GET_PREFIXES", {"tenant": "x"}),
            sp.handle_command("NETBOX_GET_PREFIXES", {"tenant": "y"}))
        assert a is b and a["n"] != c["n"]
        await sp.handle_command("NETBOX_GET_PREFIXES", {"tenant": "x"})  # recent: shared
        assert calls.count("NETBOX_GET_PREFIXES") == 2
        await sp.handle_command("NETBOX_CREATE_PREFIX", {})
        await sp.handle_command("NETBOX_GET_PREFIXES", {"tenant": "x"})  # after a write: fresh
        assert calls.count("NETBOX_GET_PREFIXES") == 3
    asyncio.run(run())


def test_failed_read_is_not_reused():
    calls = []
    sp = _spoke(calls)

    async def bad(cmd, data):
        calls.append(cmd)
        return {"status": "ERROR"}
    sp._handle_command = bad

    async def run():
        await sp.handle_command("NETBOX_GET_IPS", {})
        await sp.handle_command("NETBOX_GET_IPS", {})
    asyncio.run(run())
    assert len(calls) == 2


def test_identical_syncs_join_while_running_but_never_reuse_a_finished_one():
    calls = []
    sp = _spoke(calls)
    body = {"tenant_slug": "dxp", "devices": [{"ip": "10.0.0.1"}]}

    async def run():
        a, b = await asyncio.gather(sp.handle_command("NETBOX_SYNC_DEVICES", body),
                                    sp.handle_command("NETBOX_SYNC_DEVICES", body))
        assert a is b
        await sp.handle_command("NETBOX_SYNC_DEVICES", body)
    asyncio.run(run())
    assert calls == ["NETBOX_SYNC_DEVICES"] * 2


def test_a_write_does_not_split_a_read_already_running_but_clears_it_after():
    calls = []
    sp = _spoke(calls)

    async def run():
        first = asyncio.ensure_future(sp.handle_command("NETBOX_GET_PREFIXES", {}))
        await asyncio.sleep(0)
        await sp.handle_command("NETBOX_CREATE_PREFIX", {})
        second = await sp.handle_command("NETBOX_GET_PREFIXES", {})
        assert second is await first
        await sp.handle_command("NETBOX_CREATE_PREFIX", {})
        await sp.handle_command("NETBOX_GET_PREFIXES", {})
    asyncio.run(run())
    assert calls.count("NETBOX_GET_PREFIXES") == 2


def test_list_reads_are_cached_and_background_syncs_do_not_flush_them():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp._list_ttl = 300.0
    calls = []

    def fetch():
        calls.append(1)
        return {"status": "SUCCESS", "n": len(calls)}

    async def run():
        a = await sp._run_list("K", fetch)
        assert await sp._run_list("K", fetch) is a and len(calls) == 1
        # a background sync must not drop it; a user write (or config change) does
        assert "NETBOX_SYNC_NW_DEVICE" in sp._BACKGROUND_MUTATIONS
        sp._list_invalidate()
        assert (await sp._run_list("K", fetch))["n"] == 2
    asyncio.run(run())


def test_list_errors_are_not_cached():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    replies = [{"status": "ERROR"}, {"status": "SUCCESS"}]

    async def run():
        assert (await sp._run_list("K", lambda: replies.pop(0)))["status"] == "ERROR"
        assert (await sp._run_list("K", lambda: replies.pop(0)))["status"] == "SUCCESS"
    asyncio.run(run())

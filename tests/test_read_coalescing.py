"""Identical concurrent bulk reads share one NetBox fetch; writes clear it."""
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
        await asyncio.sleep(0.05)
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

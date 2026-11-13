"""The admin overview's counts come from NetBox's ``count``, not full lists."""
import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import netbox_spoke as spoke_mod  # noqa: E402
from netbox_engine import NetboxEngine  # noqa: E402


def test_counts_are_one_limit_1_read_per_object_type():
    eng = NetboxEngine("http://localhost", "tok")
    counts = {"/api/dcim/devices/": 7, "/api/ipam/prefixes/": 3, "/api/ipam/ip-addresses/": 1200}
    eng._api_get = MagicMock(side_effect=lambda path, params: {"count": counts[path], "results": [{}]})
    eng._api_get_all = MagicMock(side_effect=AssertionError("no full list"))
    assert eng.count_tenant_objects("acme") == {
        "status": "SUCCESS", "devices": 7, "prefixes": 3, "ip_addresses": 1200}
    for call in eng._api_get.call_args_list:
        assert call.args[1] == {"limit": 1, "brief": 1, "tenant": "acme"}


def test_a_failed_count_is_an_error_not_zero():
    eng = NetboxEngine("http://localhost", "tok")
    eng._api_get = MagicMock(side_effect=RuntimeError("502 Bad Gateway"))
    assert eng.count_tenant_objects("acme")["status"] == "ERROR"


def test_spoke_answers_netbox_count_with_the_tenant_counts():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.engine = MagicMock()
    sp.engine.count_tenant_objects = MagicMock(return_value={"status": "SUCCESS", "devices": 2})

    async def run_sync(fn, *a, **k):
        return fn(*a, **k)
    sp._run_sync = run_sync
    out = asyncio.run(sp.handle_command("NETBOX_COUNT", {"tenant": "acme"}))
    assert out == {"status": "SUCCESS", "devices": 2}
    sp.engine.count_tenant_objects.assert_called_once_with(tenant="acme", tenant_group=None)


def test_a_reply_without_a_count_is_an_error_not_zero():
    eng = NetboxEngine("http://localhost", "tok")
    eng._api_get = MagicMock(return_value={"results": []})
    assert eng.count_tenant_objects("acme")["status"] == "ERROR"


def test_a_tenant_group_count_uses_the_group_filter():
    eng = NetboxEngine("http://localhost", "tok")
    eng._api_get = MagicMock(return_value={"count": 1})
    eng.count_tenant_objects(tenant="acme", tenant_group="emea")
    assert eng._api_get.call_args.args[1] == {"limit": 1, "brief": 1, "tenant_group": "emea"}

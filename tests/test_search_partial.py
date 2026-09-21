"""Tests for NetboxEngine.search — the partial-match inventory pass.

The legacy ``q=`` sections are neutralised (_api_get returns nothing) so every
hit here comes from the cached device / ip / vm inventory snapshot.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import netbox_dcim  # noqa: E402
from netbox_engine import NetboxEngine  # noqa: E402

DEV_PATH = "/api/dcim/devices/"
IP_PATH = "/api/ipam/ip-addresses/"
VM_PATH = "/api/virtualization/virtual-machines/"


def _device(**over):
    d = {
        "id": 1,
        "name": "sw-MIAmi-01",
        "status": {"value": "active", "label": "Active"},
        "serial": "FOC1234ABCD",
        "asset_tag": "AT-778899",
        "device_type": {"id": 5, "display": "Catalyst C9300-48P", "model": "C9300-48P",
                        "manufacturer": {"id": 2, "name": "Cisco"}},
        "role": {"id": 3, "name": "Access Switch"},
        "site": {"id": 7, "name": "Miami DC"},
        "rack": {"id": 9, "name": "R12"},
        "tenant": {"id": 4, "name": "Acme Corp", "slug": "acme"},
        "tags": [{"name": "prod", "slug": "prod"}, {"name": "edge-fw", "slug": "edge-fw"}],
        "primary_ip": {"id": 11, "address": "10.20.30.40/24"},
        "custom_fields": {"mac_address": "aa:bb:cc:dd:ee:ff"},
    }
    d.update(over)
    return d


def _ip(**over):
    r = {
        "id": 100,
        "address": "192.168.30.40/24",
        "dns_name": "printer.lab",
        "status": {"value": "active"},
        "assigned_object": {"display": "eth0"},
        "tenant": {"name": "Acme Corp"},
        "tags": [],
        "custom_fields": {},
    }
    r.update(over)
    return r


def _vm(**over):
    r = {
        "id": 200,
        "name": "web-frontend-vm",
        "status": {"value": "active", "label": "Active"},
        "cluster": {"name": "prod-cluster"},
        "site": {"name": "Miami DC"},
        "primary_ip": {"address": "172.16.0.9/16"},
        "tenant": {"name": "Acme Corp"},
        "tags": [],
    }
    r.update(over)
    return r


def _engine(devices=(), ips=(), vms=()):
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    by_path = {DEV_PATH: list(devices), IP_PATH: list(ips), VM_PATH: list(vms)}
    eng._api_get_all = MagicMock(side_effect=lambda path, params=None: list(by_path[path]))
    eng._api_get = MagicMock(return_value={"results": []})
    return eng


def _search(eng, q, **kw):
    kw.setdefault("tenant", "acme")
    return eng.search(q, **kw)


def _names(res):
    return [r["name"] for r in res["results"]]


def test_partial_hostname_case_insensitive():
    res = _search(_engine([_device()]), "MIAm")
    assert _names(res) == ["sw-MIAmi-01"]
    assert res["results"][0]["type"] == "device"


def test_partial_device_type():
    res = _search(_engine([_device()]), "c9300")
    assert res["count"] == 1
    assert res["results"][0]["device_type"] == "Catalyst C9300-48P"
    assert res["results"][0]["manufacturer"] == "Cisco"


def test_partial_site_role_rack_tag_tenant_status():
    eng = _engine([_device()])
    for q in ("miami dc", "access sw", "r12", "edge-f", "acme co", "activ", "cisco"):
        assert _search(eng, q)["count"] == 1, q


def test_serial_and_asset_tag_fragment():
    eng = _engine([_device()])
    assert _search(eng, "1234ab")["count"] == 1
    assert _search(eng, "778899")["count"] == 1


def test_partial_mac_with_separators():
    eng = _engine([_device()])
    for q in ("aabbcc", "AA-BB-CC", "cc:dd", "aabb.ccdd"):
        assert _search(eng, q)["count"] == 1, q


def test_partial_ip():
    eng = _engine([_device()], [_ip()])
    res = _search(eng, "20.30")
    assert [r["type"] for r in res["results"]] == ["device"]
    res = _search(eng, "30.40")
    assert sorted(r["type"] for r in res["results"]) == ["device", "ip"]
    assert res["results"][0]["ip"] == "10.20.30.40/24"


def test_vm_partial_name():
    res = _search(_engine(vms=[_vm()]), "FRONTend")
    assert [(r["type"], r["name"], r["ip"]) for r in res["results"]] == [
        ("vm", "web-frontend-vm", "172.16.0.9/16")]
    assert res["results"][0]["cluster"] == "prod-cluster"


def test_no_false_positives_and_short_hex_skips_mac_path():
    eng = _engine([_device(name="core-1", serial="X", asset_tag="")])
    assert _search(eng, "zzzz-nothing")["count"] == 0
    # "aab" is hex-ish but < 4 digits: only the haystack path applies. The
    # MAC aa:bb:cc:dd:ee:ff contains "aabb" as hex, so "aab" must NOT match
    # via the MAC path (and is not a substring of the haystack text).
    assert _search(eng, "aab")["count"] == 0
    assert _search(eng, "aabb")["count"] == 1


def test_missing_nested_objects_tolerated():
    dev = {"id": 9, "name": "bare", "device_type": None, "role": None, "site": None,
           "rack": None, "tenant": None, "tags": None, "primary_ip": None,
           "custom_fields": None, "status": None}
    old = {"id": 10, "name": "legacy", "device_role": {"name": "Router"}}
    res = _search(_engine([dev, old]), "e")
    assert sorted(_names(res)) == ["bare", "legacy"]
    legacy = [r for r in res["results"] if r["name"] == "legacy"][0]
    assert legacy["role"] == "Router"
    assert _search(_engine([old]), "router")["count"] == 1


def test_scoping_calls():
    eng = _engine([_device()])
    _search(eng, "miam", tenant="acme")
    params = [c.args[1] for c in eng._api_get_all.call_args_list]
    for path in (DEV_PATH, IP_PATH, VM_PATH):
        got = [c.args[1] for c in eng._api_get_all.call_args_list if c.args[0] == path]
        assert got == [{"tenant": "acme"}, {"tenant": "shared"}]
    assert len(params) == 6

    eng = _engine([_device()])
    res = eng.search("miam", tenant=None, is_admin=True)
    assert res["count"] == 1
    got = [c.args[1] for c in eng._api_get_all.call_args_list]
    assert got == [{}, {}, {}]

    eng = _engine([_device()])
    assert eng.search("miam", tenant=None, is_admin=False) == {
        "status": "SUCCESS", "results": [], "count": 0}
    eng._api_get_all.assert_not_called()
    eng._api_get.assert_not_called()


def test_dedupe_across_tenant_passes():
    eng = _engine([_device()])
    # both passes return the same device -> listed once
    assert _search(eng, "miam")["count"] == 1


def test_cache_and_ttl(monkeypatch):
    eng = _engine([_device()])
    clock = [1000.0]
    monkeypatch.setattr(netbox_dcim.time, "monotonic", lambda: clock[0])
    _search(eng, "miam")
    first = eng._api_get_all.call_count
    assert first == 6  # 3 paths x (acme, shared)
    _search(eng, "c9300")
    assert eng._api_get_all.call_count == first
    clock[0] += netbox_dcim._SEARCH_INV_TTL + 1
    _search(eng, "miam")
    assert eng._api_get_all.call_count == first * 2


def test_failed_type_not_cached_and_never_raises():
    eng = _engine([_device()])
    calls = {"n": 0}

    def flaky(path, params=None):
        if path == IP_PATH:
            calls["n"] += 1
            raise RuntimeError("boom")
        return [_device()] if path == DEV_PATH else []

    eng._api_get_all = MagicMock(side_effect=flaky)
    assert _search(eng, "miam")["count"] == 1
    _search(eng, "miam")
    assert calls["n"] == 2  # ip fetch retried, entry not cached


def test_empty_query():
    eng = _engine([_device()])
    assert eng.search("   ", tenant="acme") == {"status": "SUCCESS", "results": [], "count": 0}
    assert eng.search("", is_admin=True) == {"status": "SUCCESS", "results": [], "count": 0}
    eng._api_get_all.assert_not_called()
    eng._api_get.assert_not_called()


def test_no_private_keys_in_results():
    eng = _engine([_device()], [_ip()], [_vm()])
    res = _search(eng, "a")
    assert {r["type"] for r in res["results"]} == {"device", "ip", "vm"}
    for r in res["results"]:
        assert not [k for k in r if k.startswith("_")]


def test_match_limit_per_type():
    devs = [_device(id=i, name=f"host-{i}") for i in range(1, 121)]
    res = _search(_engine(devs), "host")
    assert res["count"] == netbox_dcim._SEARCH_MATCH_LIMIT

"""IPv6 support for find_available_prefixes()/_mask_for_hosts().

Prior to this test, find_available_prefixes() hardcoded 32-bit address math
and rejected any 'near' outside RFC1918 (10/8, 172.16/12, 192.168/16) — every
IPv6 prefix, including a routed GUA /48 from a tunnel broker (this lab's
HE.net allocation, e.g. 2001:470:4948::/48), was refused outright. This pins:
  - IPv4 behavior is unchanged (regression guard).
  - IPv6 with rfc1918=True is restricted to ULA (fc00::/7).
  - IPv6 with rfc1918=False treats 'near' itself as the search container,
    the path a routed GUA /48 must use since it is neither RFC1918 nor ULA.
  - Occupied prefixes of the *other* family never affect availability
    (mixed-family overlap comparisons must not raise).
Uses a pynetbox stand-in (no live NetBox)."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


def _engine(occupied_prefixes):
    """occupied_prefixes: list of (prefix_str, has_tenant) tuples returned by
    the mocked /api/ipam/prefixes/ fetch."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    rows = [{"prefix": p, "tenant": {"id": 1} if has_tenant else None}
            for p, has_tenant in occupied_prefixes]
    eng._api_get_all = MagicMock(return_value=rows)
    return eng


def test_ipv4_behavior_unchanged():
    eng = _engine([("172.17.5.0/24", True)])
    result = eng.find_available_prefixes(near="172.17.0.0/24", prefix_length=24, count=5)
    assert result["status"] == "SUCCESS"
    prefixes = [c["prefix"] for c in result["available"]]
    assert "172.17.5.0/24" not in prefixes
    assert "172.17.0.0/24" in prefixes


def test_ipv4_outside_rfc1918_rejected():
    eng = _engine([])
    result = eng.find_available_prefixes(near="8.8.8.0/24", prefix_length=24, count=5)
    assert result["status"] == "ERROR"
    assert "RFC1918" in result["message"]


def test_ipv6_rejected_outside_ula_by_default():
    eng = _engine([])
    result = eng.find_available_prefixes(near="2001:470:4948::/48", prefix_length=64, count=5)
    assert result["status"] == "ERROR"
    assert "ULA" in result["message"]


def test_ipv6_ula_container_works():
    eng = _engine([("fc00::100:0:0:0:0/64", True)])
    result = eng.find_available_prefixes(near="fc00::/64", prefix_length=64, count=5)
    assert result["status"] == "SUCCESS"
    assert len(result["available"]) == 5
    for cand in result["available"]:
        assert "/64" in cand["prefix"]


def test_ipv6_gua_48_with_rfc1918_false_uses_near_as_container():
    """The lab's real scenario: routed /48 from HE.net, carve /64s from it."""
    eng = _engine([("2001:470:4948:1::/64", True)])
    result = eng.find_available_prefixes(
        near="2001:470:4948::/48", prefix_length=64, count=10, rfc1918=False)
    assert result["status"] == "SUCCESS"
    prefixes = [c["prefix"] for c in result["available"]]
    assert "2001:470:4948:1::/64" not in prefixes
    assert "2001:470:4948::/64" in prefixes
    for p in prefixes:
        assert p.startswith("2001:470:4948:")


def test_ipv6_mixed_family_occupied_rows_ignored_not_crashed():
    """An IPv4 row in the occupied set must not raise on overlaps() and must
    not block an otherwise-free IPv6 candidate."""
    eng = _engine([("10.0.0.0/24", True), ("2001:470:4948::/64", True)])
    result = eng.find_available_prefixes(
        near="2001:470:4948::/48", prefix_length=64, count=5, rfc1918=False)
    assert result["status"] == "SUCCESS"
    prefixes = [c["prefix"] for c in result["available"]]
    assert "2001:470:4948::/64" not in prefixes
    assert any(p.startswith("2001:470:4948:") for p in prefixes)


def test_prefix_length_bounds_family_aware():
    eng = _engine([])
    v4_result = eng.find_available_prefixes(near="10.0.0.0/24", prefix_length=33, count=1)
    assert v4_result["status"] == "ERROR"
    assert "0..32" in v4_result["message"]

    v6_result = eng.find_available_prefixes(
        near="fc00::/48", prefix_length=129, count=1)
    assert v6_result["status"] == "ERROR"
    assert "0..128" in v6_result["message"]


def test_mask_for_hosts_ipv6():
    eng = _engine([])
    assert eng._mask_for_hosts(1, family=6) == 64  # floored to /64 minimum
    assert eng._mask_for_hosts(1 << 90, family=6) == 48  # capped at /48


def test_mask_for_hosts_ipv4_unchanged():
    eng = _engine([])
    assert eng._mask_for_hosts(2) == 30
    assert eng._mask_for_hosts(2000) == 22

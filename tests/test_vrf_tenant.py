"""Tenants own a NetBox VRF (overlapping IP space): created prefixes/IPs must
land in it and lookups must be scoped to it."""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


def _engine(vrf_rows):
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng._api_get = MagicMock(return_value={"results": vrf_rows})
    return eng


def test_vrf_lookup_by_tenant():
    eng = _engine([{"id": 7, "name": "acme"}])
    assert eng._vrf_id_for_tenant(SimpleNamespace(id=3)) == 7
    assert eng._api_get.call_args[0][1]["tenant_id"] == 3


def test_no_vrf_is_none():
    assert _engine([])._vrf_id_for_tenant(SimpleNamespace(id=3)) is None
    assert _engine([])._vrf_id_for_tenant(None) is None


def test_reuse_or_create_ip_creates_in_tenant_vrf():
    eng = _engine([{"id": 7, "name": "acme"}])
    eng.nb.ipam.ip_addresses.get.return_value = None
    eng._tag_ip_mac = MagicMock()
    eng._reuse_or_create_ip("10.0.0.5/24", {"address": "10.0.0.5/24"}, "10.0.0.5", 1,
                            tenant=SimpleNamespace(id=3))
    assert eng.nb.ipam.ip_addresses.get.call_args[1] == {"address": "10.0.0.5/24", "vrf_id": 7}
    assert eng.nb.ipam.ip_addresses.create.call_args[1]["vrf"] == 7


def test_mask_uses_only_tenant_vrf_prefixes():
    import ipaddress
    eng = _engine([{"id": 7, "name": "acme"}])
    eng._prefix_prefetch = [ipaddress.ip_network("10.0.0.0/16"), ipaddress.ip_network("10.0.0.0/24")]
    eng._prefix_prefetch_vrf = {7: [ipaddress.ip_network("10.0.0.0/16")],
                                8: [ipaddress.ip_network("10.0.0.0/24")]}
    assert eng._mask_for_ip("10.0.0.5", SimpleNamespace(id=3)) == "16"
    assert eng._mask_for_ip("10.0.0.5") == "24"

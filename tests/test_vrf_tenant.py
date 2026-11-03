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


def test_reuse_ip_for_tenant_without_vrf_stays_in_global_table():
    # Another tenant's VRF may hold the same address; a VRF-less tenant must
    # not find (and retag) it.
    eng = _engine([])
    eng.nb.ipam.ip_addresses.get.return_value = None
    eng._tag_ip_mac = MagicMock()
    eng._reuse_or_create_ip("10.0.0.5/24", {"address": "10.0.0.5/24"}, "10.0.0.5", 1,
                            tenant=SimpleNamespace(id=3))
    assert eng.nb.ipam.ip_addresses.get.call_args[1] == {"address": "10.0.0.5/24",
                                                         "vrf_id": "null"}
    assert "vrf" not in eng.nb.ipam.ip_addresses.create.call_args[1]


def test_mask_uses_only_tenant_vrf_prefixes():
    import ipaddress
    eng = _engine([{"id": 7, "name": "acme"}])
    eng._prefix_prefetch = [ipaddress.ip_network("10.0.0.0/16"), ipaddress.ip_network("10.0.0.0/24")]
    eng._prefix_prefetch_vrf = {7: [ipaddress.ip_network("10.0.0.0/16")],
                                8: [ipaddress.ip_network("10.0.0.0/24")]}
    assert eng._mask_for_ip("10.0.0.5", SimpleNamespace(id=3)) == "16"
    assert eng._mask_for_ip("10.0.0.5") == "24"


def test_global_ips_of_a_vrf_tenant_move_into_its_vrf():
    # IPs created before the tenant got a VRF stay global until a sync moves
    # them; one the VRF already holds is left (moving it would duplicate it).
    eng = _engine([{"id": 7, "name": "acme"}])
    eng._api_get_all = MagicMock(side_effect=[
        [{"id": 1, "address": "10.0.0.5/24"}, {"id": 2, "address": "10.0.0.6/24"}],
        [{"id": 9, "address": "10.0.0.6/24"}],
    ])
    out = eng._move_tenant_ips_to_vrf(SimpleNamespace(id=3))
    assert eng._api_get_all.call_args_list[0][0][1]["vrf_id"] == "null"
    eng.nb.ipam.ip_addresses.update.assert_called_once_with([{"id": 1, "vrf": 7}])
    assert out == {"moved": 1, "conflicts": ["10.0.0.6/24"]}


def test_tenant_without_vrf_moves_nothing():
    eng = _engine([])
    eng._api_get_all = MagicMock()
    assert eng._move_tenant_ips_to_vrf(SimpleNamespace(id=3))["moved"] == 0
    eng._api_get_all.assert_not_called()

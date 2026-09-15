"""``allocate_ip`` must attribute the new address to a tenant.

NetBox does not inherit tenancy from the containing prefix when an IP address
is created — the tenant field simply stays empty unless the creating payload
sets it. ``allocate_ip`` never set it, so every address the Lab Manager minted
landed tenant-less while every address created by hand in the NetBox UI carried
its prefix's tenant.

Observed live: of 123 ``ipam_ipaddress`` rows, 122 carried a tenant (RA 67,
LRB 55) and exactly one did not. The fleet rule is that new resources carry
tenant context, and a tenant-scoped IPAM view filters on it — so an address
minted without one is invisible to the very operator who just asked for it.

The path that made this matter is the DHCP reservation write-back: the hub
creates the missing NetBox IP object behind the scenes (``NETBOX_ALLOCATE_IP``)
and has no tenant to pass, because ``NETBOX_GET_PREFIXES`` does not forward the
prefix's tenant. Resolving it here — where the containing prefix object is
already in hand — fixes that caller and every other one at the same time,
without a wire-contract change.

Uses a pynetbox stand-in (no live NetBox), mirroring test_ip_mac_custom_fields.py.
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402
from netbox_spoke import NetboxSpoke  # noqa: E402


class _Rec:
    """pynetbox Record stand-in: arbitrary attrs + an auto id."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        if not getattr(self, "id", None):
            self.id = id(self) % 100000


def _engine(prefix_tenant=None, tenant_lookup=None):
    """Engine whose prefix 172.17.1.0/24 optionally carries ``prefix_tenant``.

    ``tenant_lookup`` stands in for ``nb.tenancy.tenants.get(slug=...)``.
    """
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()

    prefix_obj = _Rec(prefix="172.17.1.0/24", tenant=prefix_tenant)
    prefix_obj.available_ips = MagicMock()
    prefix_obj.available_ips.create = MagicMock(
        return_value=_Rec(address="172.17.1.50/24", id=901))
    eng.nb.ipam.prefixes.get = MagicMock(return_value=prefix_obj)
    eng.nb.ipam.ip_addresses.create = MagicMock(
        return_value=_Rec(address="172.17.1.199/24", id=900))
    eng.nb.tenancy.tenants.get = MagicMock(
        side_effect=lambda **kw: tenant_lookup)
    return eng


def _created_payload(eng):
    """The payload handed to ``ip_addresses.create``."""
    assert eng.nb.ipam.ip_addresses.create.called, "no IP was created"
    return eng.nb.ipam.ip_addresses.create.call_args[0][0]


# ------------------------------------------------------------- tenant inherit

def test_exact_address_inherits_prefix_tenant():
    """The reservation write-back path: exact address, no tenant supplied."""
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"))
    res = eng.allocate_ip(prefix="172.17.1.0/24", address="172.17.1.199")
    assert res["status"] == "SUCCESS", res
    assert _created_payload(eng).get("tenant") == 7


def test_next_available_inherits_prefix_tenant():
    """The auto-allocate branch must attribute the address too."""
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"))
    res = eng.allocate_ip(prefix="172.17.1.0/24")
    assert res["status"] == "SUCCESS", res
    payload = eng.nb.ipam.prefixes.get.return_value.available_ips.create.call_args[0][0]
    assert payload.get("tenant") == 7


def test_result_reports_the_tenant_applied():
    """Callers need to see which tenant the address landed in."""
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"))
    res = eng.allocate_ip(prefix="172.17.1.0/24", address="172.17.1.199")
    assert res.get("tenant") == 7


# ------------------------------------------------------------ explicit tenant

def test_explicit_tenant_slug_wins_over_prefix_tenant():
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"),
                  tenant_lookup=_Rec(id=42, name="LRB"))
    res = eng.allocate_ip(prefix="172.17.1.0/24", address="172.17.1.199",
                          tenant_slug="lrb")
    assert res["status"] == "SUCCESS", res
    assert _created_payload(eng).get("tenant") == 42


def test_unresolvable_tenant_slug_is_an_error_not_a_fallback():
    """Silently using the prefix's tenant would be the exact mismatch we guard."""
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"), tenant_lookup=None)
    res = eng.allocate_ip(prefix="172.17.1.0/24", address="172.17.1.199",
                          tenant_slug="nope")
    assert res["status"] == "ERROR", res
    assert "nope" in res["message"]
    assert not eng.nb.ipam.ip_addresses.create.called, \
        "address must not be created when the named tenant cannot be resolved"


# ------------------------------------------------------------------ non-regression

def test_prefix_without_tenant_is_not_an_error():
    """Nothing to inherit is a normal case, not a failure."""
    eng = _engine(prefix_tenant=None)
    res = eng.allocate_ip(prefix="172.17.1.0/24", address="172.17.1.199")
    assert res["status"] == "SUCCESS", res
    assert "tenant" not in _created_payload(eng)


def test_missing_prefix_still_errors():
    eng = _engine()
    eng.nb.ipam.prefixes.get = MagicMock(return_value=None)
    res = eng.allocate_ip(prefix="10.9.9.0/24", address="10.9.9.5")
    assert res["status"] == "ERROR" and "not found" in res["message"]


def test_address_outside_prefix_still_rejected():
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"))
    res = eng.allocate_ip(prefix="172.17.1.0/24", address="172.17.2.5")
    assert res["status"] == "ERROR" and "not within" in res["message"]


def test_description_and_dns_name_still_forwarded():
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"))
    eng.allocate_ip(prefix="172.17.1.0/24", address="172.17.1.199",
                    dns_name="wks.example.", description="DHCP reservation")
    payload = _created_payload(eng)
    assert payload["dns_name"] == "wks.example."
    assert payload["description"] == "DHCP reservation"


# ------------------------------------------------------------------ spoke wire

def _dispatch(eng, data):
    spoke = NetboxSpoke.__new__(NetboxSpoke)
    spoke.engine = eng
    # Set by the real __init__; ALLOCATE_IP is a mutating command and clears it.
    spoke._picklist_cache = {}

    async def _run_sync(fn, *a, **kw):
        return fn(*a, **kw)

    spoke._run_sync = _run_sync
    return asyncio.get_event_loop().run_until_complete(
        spoke.handle_command("NETBOX_ALLOCATE_IP", data))


def test_spoke_allocate_ip_inherits_tenant_end_to_end():
    """The hub sends no tenant at all — inheritance must still happen."""
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"))
    res = _dispatch(eng, {"prefix": "172.17.1.0/24", "address": "172.17.1.199",
                          "description": "DHCP reservation (created by Lab Manager)"})
    assert res["status"] == "SUCCESS", res
    assert _created_payload(eng).get("tenant") == 7


def test_spoke_forwards_explicit_tenant_slug():
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"),
                  tenant_lookup=_Rec(id=42, name="LRB"))
    res = _dispatch(eng, {"prefix": "172.17.1.0/24", "address": "172.17.1.199",
                          "tenant_slug": "lrb"})
    assert res["status"] == "SUCCESS", res
    assert _created_payload(eng).get("tenant") == 42


def test_spoke_accepts_tenant_alias():
    """``tenant`` is accepted as an alias so an older hub payload still works."""
    eng = _engine(prefix_tenant=_Rec(id=7, name="DEFAULT"),
                  tenant_lookup=_Rec(id=42, name="LRB"))
    res = _dispatch(eng, {"prefix": "172.17.1.0/24", "address": "172.17.1.199",
                          "tenant": "lrb"})
    assert res["status"] == "SUCCESS", res
    assert _created_payload(eng).get("tenant") == 42

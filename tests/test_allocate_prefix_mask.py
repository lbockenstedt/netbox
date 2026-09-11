"""Pins the reported bug: creating a subnet from a pool (Allocate Subnet
modal — "Parent Prefix" + "Prefix Length" dropdown + optional exact "Subnet"
field) with parent 172.17.0.0/17 and a /24 selection must create a /24, not
a /32.

Root cause: when the optional "Subnet" field is filled with a bare address
(no explicit "/mask", e.g. "172.17.0.0" instead of "172.17.0.0/24"),
``ipaddress.ip_network(x, strict=False)`` silently defaults the missing mask
to /32, discarding the separately-selected prefix_length dropdown entirely.
Uses a pynetbox stand-in (no live NetBox)."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Rec:
    """pynetbox Record stand-in: arbitrary attrs + an auto id."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        if not getattr(self, "id", None):
            self.id = id(self) % 100000


def _engine(parent_prefix="172.17.0.0/17", tenants=()):
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()

    parent = _Rec(prefix=parent_prefix, id=1)

    eng.nb.tenancy.tenants = MagicMock()
    eng.nb.tenancy.tenants.get = MagicMock(
        side_effect=lambda slug=None, **kw: next((t for t in tenants if t.slug == slug), None))

    eng.nb.dcim.sites = MagicMock()
    eng.nb.dcim.sites.get = MagicMock(return_value=None)

    eng._created = []

    def _create_direct(payload):
        eng._created.append(dict(payload))
        return _Rec(prefix=payload["prefix"], id=9001)

    def _create_available(payload):
        eng._created.append(dict(payload))
        # Emulate NetBox's own available-prefixes allocator: it derives the
        # actual child CIDR from prefix_length within the parent.
        length = payload["prefix_length"]
        return _Rec(prefix=f"172.17.0.0/{length}", id=9002)

    parent.available_prefixes = MagicMock()
    parent.available_prefixes.create = MagicMock(side_effect=_create_available)

    eng.nb.ipam.prefixes = MagicMock()
    eng.nb.ipam.prefixes.get = MagicMock(return_value=parent)
    eng.nb.ipam.prefixes.create = MagicMock(side_effect=_create_direct)

    return eng


def test_pool_auto_allocate_uses_prefix_length_directly():
    """No exact subnet typed: NetBox auto-allocates from prefix_length."""
    eng = _engine()
    r = eng.allocate_prefix("172.17.0.0/17", prefix_length=24)
    assert r["status"] == "SUCCESS"
    assert r["prefix"] == "172.17.0.0/24"
    assert eng._created[0]["prefix_length"] == 24


def test_exact_subnet_with_mask_creates_requested_cidr():
    """Exact subnet typed WITH a mask: created verbatim, no /32 corruption."""
    eng = _engine()
    r = eng.allocate_prefix("172.17.0.0/17", prefix_length=24,
                            requested_prefix="172.17.0.0/24")
    assert r["status"] == "SUCCESS"
    assert r["prefix"] == "172.17.0.0/24"
    assert eng._created[0]["prefix"] == "172.17.0.0/24"


def test_exact_subnet_bare_address_applies_dropdown_mask_not_32():
    """Regression for the reported bug: parent 172.17.0.0/17, /24 selected in
    the dropdown, but the optional exact-subnet field has a bare address with
    no "/mask" — must still create a /24, not silently fall back to /32."""
    eng = _engine()
    r = eng.allocate_prefix("172.17.0.0/17", prefix_length=24,
                            requested_prefix="172.17.0.0")
    assert r["status"] == "SUCCESS"
    assert r["prefix"] == "172.17.0.0/24"
    assert eng._created[0]["prefix"] == "172.17.0.0/24"
    assert not eng._created[0]["prefix"].endswith("/32")


def test_bare_address_outside_parent_still_rejected():
    """The mask-completion must not bypass the subnet_of(parent) guard."""
    eng = _engine()
    r = eng.allocate_prefix("172.17.0.0/17", prefix_length=24,
                            requested_prefix="10.0.0.0")
    assert r["status"] == "ERROR"
    assert eng._created == []

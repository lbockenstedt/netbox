"""Tests for NetboxEngine.add_rack / update_rack tenant attribution — the
WebUI "Add Rack" path (create from a tenant view must stamp that tenant; edit
must preserve an existing tenant when no tenant is sent).

Pins the contract mirrored from allocate_prefix / claim_device: a supplied
tenant_slug is resolved to a tenant id; an unresolvable slug is REFUSED (no
silent unattributed create); a None slug on update leaves the tenant untouched.
Uses a pynetbox stand-in (no live NetBox)."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Rec:
    """pynetbox Record stand-in: arbitrary attrs + a save mock + an auto id."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.save = MagicMock()
        if not getattr(self, "id", None):
            self.id = id(self) % 100000


def _engine(tenants=(), sites=()):
    """Engine wired with fake nb endpoints for sites/racks/tenants."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()

    eng.nb.dcim.sites = MagicMock()
    eng.nb.dcim.sites.get = MagicMock(
        side_effect=lambda slug=None, **kw: next((s for s in sites if s.slug == slug), None))

    eng.nb.tenancy.tenants = MagicMock()
    eng.nb.tenancy.tenants.get = MagicMock(
        side_effect=lambda slug=None, **kw: next((t for t in tenants if t.slug == slug), None))

    eng._created = []

    def _create(**kw):
        eng._created.append(kw)
        return _Rec(**{**kw, "id": 9000 + len(eng._created)})

    eng.nb.dcim.racks = MagicMock()
    eng.nb.dcim.racks.create = MagicMock(side_effect=_create)
    return eng


# ─── add_rack ───────────────────────────────────────────────────────────────

def test_add_rack_with_resolvable_tenant_sets_tenant_id():
    eng = _engine(tenants=[_Rec(id=7, slug="acme")],
                  sites=[_Rec(id=3, slug="site-a")])
    r = eng.add_rack("R1", "site-a", u_height=42, tenant_slug="acme")
    assert r["status"] == "SUCCESS"
    payload = eng._created[0]
    assert payload["tenant"] == 7
    assert payload["site"] == 3 and payload["name"] == "R1"


def test_add_rack_without_tenant_creates_global():
    eng = _engine(sites=[_Rec(id=3, slug="site-a")])
    r = eng.add_rack("R2", "site-a", tenant_slug=None)
    assert r["status"] == "SUCCESS"
    assert "tenant" not in eng._created[0]


def test_add_rack_unresolvable_tenant_refused_not_silent():
    eng = _engine(tenants=[_Rec(id=7, slug="acme")],
                  sites=[_Rec(id=3, slug="site-a")])
    r = eng.add_rack("R3", "site-a", tenant_slug="ghost")
    assert r["status"] == "ERROR"
    assert "ghost" in r["message"]
    assert eng._created == []  # nothing created


def test_add_rack_missing_site_errors_before_tenant_lookup():
    eng = _engine(tenants=[_Rec(id=7, slug="acme")])
    r = eng.add_rack("R4", "nope", tenant_slug="acme")
    assert r["status"] == "ERROR"
    assert eng._created == []


# ─── update_rack ────────────────────────────────────────────────────────────

def test_update_rack_without_tenant_preserves_existing():
    eng = _engine()
    rack = _Rec(id=55, name="old", u_height=42, facility_id=None, tenant=7)
    eng.nb.dcim.racks.get = MagicMock(return_value=rack)
    r = eng.update_rack(55, name="new", tenant_slug=None)
    assert r["status"] == "SUCCESS"
    rack.save.assert_called_once()
    # tenant attribute must NOT have been reassigned to None
    assert rack.tenant == 7


def test_update_rack_with_tenant_reattributes():
    eng = _engine(tenants=[_Rec(id=9, slug="beta")])
    rack = _Rec(id=55, name="old", u_height=42, tenant=7)
    eng.nb.dcim.racks.get = MagicMock(return_value=rack)
    r = eng.update_rack(55, name="new", tenant_slug="beta")
    assert r["status"] == "SUCCESS"
    assert rack.tenant == 9


def test_update_rack_unresolvable_tenant_refused():
    eng = _engine(tenants=[_Rec(id=9, slug="beta")])
    rack = _Rec(id=55, name="old", tenant=7)
    eng.nb.dcim.racks.get = MagicMock(return_value=rack)
    r = eng.update_rack(55, tenant_slug="ghost")
    assert r["status"] == "ERROR"
    assert "ghost" in r["message"]
    rack.save.assert_not_called()


def test_update_rack_empty_string_tenant_clears_to_none():
    # An explicit empty string means "move to global" (distinct from None=skip).
    eng = _engine()
    rack = _Rec(id=55, name="old", tenant=7)
    eng.nb.dcim.racks.get = MagicMock(return_value=rack)
    r = eng.update_rack(55, tenant_slug="")
    assert r["status"] == "SUCCESS"
    assert rack.tenant is None
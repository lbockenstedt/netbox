"""Pins the reported bug: the "Enable DHCP scope" checkbox on a NetBox
prefix (Allocate Subnet / Edit Subnet modals) silently never took effect.

Three compounding bugs, all in this repo:

1. ``get_prefixes()`` never returned ``custom_fields`` at all — so the hub's
   DHCP sync (which reads ``custom_fields.dhcp_enabled``/``gateway``/
   ``dns_servers``) always saw an empty dict, and the WebUI's edit modal
   could never show the checkbox checked even if it HAD been saved.
2. ``allocate_prefix()`` (create) never accepted or wrote ``custom_fields``
   at all — the checkbox on the "Allocate Subnet" modal was a no-op.
3. ``update_prefix()`` (edit) never accepted or wrote ``custom_fields``
   either — checking the box on Edit and saving silently discarded it.

A fourth, narrower variant of the same bug: ``claim_prefix()`` — the "Add
Prefix" one-click finder/assign flow — never accepted ``custom_fields``
either, on EITHER its create-new or reassign-existing-unassigned branch. That
flow has no edit step of its own, so a prefix "created" through it could
never become a Kea DHCP scope until someone separately opened Edit Subnet and
re-saved it — exactly the "works on edit, not on initial creation" report.

Uses a pynetbox stand-in (no live NetBox), mirroring
test_allocate_prefix_mask.py.
"""
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


def _engine():
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    return eng


def test_get_prefixes_returns_custom_fields():
    eng = _engine()
    eng._api_get_all = MagicMock(return_value=[
        {"id": 1, "prefix": "10.0.0.0/24", "status": {"value": "active"},
         "description": "Lab A", "custom_fields": {"dhcp_enabled": True,
                                                     "gateway": "10.0.0.1"}},
    ])
    result = eng.get_prefixes()
    assert result["status"] == "SUCCESS"
    assert result["prefixes"][0]["custom_fields"] == {
        "dhcp_enabled": True, "gateway": "10.0.0.1"}


def test_get_prefixes_defaults_custom_fields_to_empty_dict():
    eng = _engine()
    eng._api_get_all = MagicMock(return_value=[
        {"id": 1, "prefix": "10.0.0.0/24", "status": {"value": "active"}},
    ])
    result = eng.get_prefixes()
    assert result["prefixes"][0]["custom_fields"] == {}


def test_allocate_prefix_writes_custom_fields_on_create():
    eng = _engine()
    parent = _Rec(prefix="172.17.0.0/17", id=1)
    eng.nb.ipam.prefixes.get = MagicMock(return_value=parent)
    created = {}

    def _create_available(payload):
        created.update(payload)
        return _Rec(prefix="172.17.0.0/24", id=9002)

    parent.available_prefixes = MagicMock()
    parent.available_prefixes.create = MagicMock(side_effect=_create_available)

    r = eng.allocate_prefix("172.17.0.0/17", prefix_length=24,
                            custom_fields={"dhcp_enabled": True})
    assert r["status"] == "SUCCESS"
    assert created["custom_fields"] == {"dhcp_enabled": True}


def test_allocate_prefix_omits_custom_fields_when_not_given():
    eng = _engine()
    parent = _Rec(prefix="172.17.0.0/17", id=1)
    eng.nb.ipam.prefixes.get = MagicMock(return_value=parent)
    created = {}

    def _create_available(payload):
        created.update(payload)
        return _Rec(prefix="172.17.0.0/24", id=9002)

    parent.available_prefixes = MagicMock()
    parent.available_prefixes.create = MagicMock(side_effect=_create_available)

    eng.allocate_prefix("172.17.0.0/17", prefix_length=24)
    assert "custom_fields" not in created


def test_update_prefix_merges_custom_fields_onto_existing():
    eng = _engine()
    pfx = _Rec(id=5, prefix="10.0.0.0/24",
              custom_fields={"dhcp_enabled": False, "gateway": "10.0.0.1"})
    eng.nb.ipam.prefixes.get = MagicMock(return_value=pfx)
    pfx.save = MagicMock()

    r = eng.update_prefix(5, custom_fields={"dhcp_enabled": True})
    assert r["status"] == "SUCCESS"
    # dhcp_enabled flips, gateway (not part of this update) survives untouched.
    assert pfx.custom_fields == {"dhcp_enabled": True, "gateway": "10.0.0.1"}
    pfx.save.assert_called_once()


def test_update_prefix_sets_custom_fields_when_none_existed():
    eng = _engine()
    pfx = _Rec(id=5, prefix="10.0.0.0/24", custom_fields=None)
    eng.nb.ipam.prefixes.get = MagicMock(return_value=pfx)
    pfx.save = MagicMock()

    r = eng.update_prefix(5, custom_fields={"dhcp_enabled": True})
    assert r["status"] == "SUCCESS"
    assert pfx.custom_fields == {"dhcp_enabled": True}


def test_update_prefix_no_op_when_custom_fields_not_given():
    eng = _engine()
    pfx = _Rec(id=5, prefix="10.0.0.0/24", custom_fields={"dhcp_enabled": True})
    eng.nb.ipam.prefixes.get = MagicMock(return_value=pfx)
    pfx.save = MagicMock()

    eng.update_prefix(5, description="renamed")
    assert pfx.custom_fields == {"dhcp_enabled": True}


def test_claim_prefix_writes_custom_fields_on_create():
    eng = _engine()
    eng.nb.ipam.prefixes.get = MagicMock(return_value=None)  # not yet in NetBox
    created = {}

    def _create(payload):
        created.update(payload)
        return _Rec(prefix="10.0.0.0/24", id=9003)

    eng.nb.ipam.prefixes.create = MagicMock(side_effect=_create)

    r = eng.claim_prefix("10.0.0.0/24", custom_fields={"dhcp_enabled": True, "gateway": "10.0.0.1"})
    assert r["status"] == "SUCCESS"
    assert created["custom_fields"] == {"dhcp_enabled": True, "gateway": "10.0.0.1"}


def test_claim_prefix_merges_custom_fields_onto_existing_unassigned():
    eng = _engine()
    pfx = _Rec(id=6, prefix="10.0.0.0/24", tenant=None,
              custom_fields={"gateway": "10.0.0.1"})
    eng.nb.ipam.prefixes.get = MagicMock(return_value=pfx)
    pfx.save = MagicMock()

    r = eng.claim_prefix("10.0.0.0/24", custom_fields={"dhcp_enabled": True})
    assert r["status"] == "SUCCESS"
    assert pfx.custom_fields == {"gateway": "10.0.0.1", "dhcp_enabled": True}
    pfx.save.assert_called_once()


def test_claim_prefix_omits_custom_fields_when_not_given():
    eng = _engine()
    eng.nb.ipam.prefixes.get = MagicMock(return_value=None)
    created = {}

    def _create(payload):
        created.update(payload)
        return _Rec(prefix="10.0.0.0/24", id=9004)

    eng.nb.ipam.prefixes.create = MagicMock(side_effect=_create)

    eng.claim_prefix("10.0.0.0/24")
    assert "custom_fields" not in created


def test_prefix_site_reads_and_writes_netbox_scope():
    eng = _engine()
    eng._api_get_all = MagicMock(return_value=[
        {"id": 1, "prefix": "10.0.0.0/24", "status": {"value": "active"},
         "scope_type": "dcim.site", "scope": {"id": 9, "name": "MIP", "slug": "mip"}}])
    p = eng.get_prefixes()["prefixes"][0]
    assert (p["site"], p["site_slug"]) == ("MIP", "mip")
    pfx = _Rec(id=5, prefix="10.0.0.0/24", custom_fields={})
    pfx.save = MagicMock()
    eng.nb.ipam.prefixes.get = MagicMock(return_value=pfx)
    eng.nb.dcim.sites.get = MagicMock(return_value=_Rec(id=9))
    assert eng.update_prefix(5, site_slug="mip")["status"] == "SUCCESS"
    assert (pfx.scope_type, pfx.scope_id) == ("dcim.site", 9)
    eng.update_prefix(5, site_slug="")
    assert (pfx.scope_type, pfx.scope_id) == (None, None)


def test_prefix_site_change_cascades_to_child_prefixes():
    eng = _engine()
    pfx = _Rec(id=5, prefix="10.0.0.0/16", vrf=_Rec(id=3), custom_fields={})
    pfx.save = MagicMock()
    eng.nb.ipam.prefixes.get = MagicMock(return_value=pfx)
    eng.nb.dcim.sites.get = MagicMock(return_value=_Rec(id=9))
    eng._api_get_all = MagicMock(return_value=[{"id": 6}, {"id": 7}])

    res = eng.update_prefix(5, site_slug="mip")

    assert res["children_updated"] == 2
    eng._api_get_all.assert_called_once_with(
        "/api/ipam/prefixes/", {"within": "10.0.0.0/16", "vrf_id": 3, "limit": 500})
    eng.nb.ipam.prefixes.update.assert_called_once_with([
        {"id": 6, "scope_type": "dcim.site", "scope_id": 9},
        {"id": 7, "scope_type": "dcim.site", "scope_id": 9}])


def test_prefix_site_cascade_failure_rolls_back_parent():
    eng = _engine()
    pfx = _Rec(id=5, prefix="10.0.0.0/16", scope_type="dcim.site", scope_id=1,
               custom_fields={})
    pfx.save = MagicMock()
    eng.nb.ipam.prefixes.get = MagicMock(return_value=pfx)
    eng.nb.dcim.sites.get = MagicMock(return_value=_Rec(id=9))
    eng._api_get_all = MagicMock(return_value=[{"id": 6}])
    eng.nb.ipam.prefixes.update = MagicMock(side_effect=Exception("boom"))

    assert eng.update_prefix(5, site_slug="mip")["status"] == "ERROR"
    assert (pfx.scope_type, pfx.scope_id) == ("dcim.site", 1)
    assert pfx.save.call_count == 2

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

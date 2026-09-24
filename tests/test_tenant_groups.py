"""NetBox tenant-group read paths.

Covers the transitive (upward) membership rollup in ``get_tenant_groups``, the
``group_slug``/``group_name`` tagging added to ``get_tenants``, and the
``tenant_group`` filter superseding ``tenant`` on the four list endpoints.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


def _engine(api_rows):
    """NetboxEngine with pynetbox stubbed and ``_api_get_all`` served from a
    {path: rows} map, recording the params each call received."""
    eng = NetboxEngine.__new__(NetboxEngine)
    eng.nb = MagicMock()
    eng.calls = []

    def _get_all(path, params=None, max_pages=200):
        eng.calls.append((path, dict(params or {})))
        return api_rows.get(path, [])

    eng._api_get_all = _get_all
    return eng


# ─── get_tenant_groups: transitive membership ────────────────────────────────

# A(root) → B → C, one tenant hanging off each level, plus an ungrouped tenant.
_NESTED = {
    "/api/tenancy/tenant-groups/": [
        {"id": 1, "name": "A", "slug": "a", "parent": None, "description": ""},
        {"id": 2, "name": "B", "slug": "b",
         "parent": {"id": 1, "name": "A", "slug": "a"}, "description": None},
        {"id": 3, "name": "C", "slug": "c",
         "parent": {"id": 2, "name": "B", "slug": "b"}, "description": ""},
    ],
    "/api/tenancy/tenants/": [
        {"id": 10, "name": "T_A", "slug": "t_a",
         "group": {"id": 1, "name": "A", "slug": "a"}},
        {"id": 11, "name": "T_B", "slug": "t_b",
         "group": {"id": 2, "name": "B", "slug": "b"}},
        {"id": 12, "name": "T_C", "slug": "t_c",
         "group": {"id": 3, "name": "C", "slug": "c"}},
        {"id": 13, "name": "LOOSE", "slug": "loose", "group": None},
    ],
}


def test_membership_rolls_upward_not_downward():
    """The ROOT sees every descendant tenant; a LEAF sees only its own."""
    res = _engine(_NESTED).get_tenant_groups()
    assert res["status"] == "SUCCESS"
    by_slug = {g["slug"]: g for g in res["groups"]}

    assert by_slug["a"]["tenant_slugs"] == ["t_a", "t_b", "t_c"]
    assert by_slug["b"]["tenant_slugs"] == ["t_b", "t_c"]
    assert by_slug["c"]["tenant_slugs"] == ["t_c"]
    # A child must NOT inherit its parent's tenants.
    assert "t_a" not in by_slug["c"]["tenant_slugs"]


def test_group_row_shape():
    res = _engine(_NESTED).get_tenant_groups()
    by_slug = {g["slug"]: g for g in res["groups"]}

    assert by_slug["a"]["parent_slug"] == ""       # root → empty string, not None
    assert by_slug["b"]["parent_slug"] == "a"
    assert by_slug["b"]["description"] == ""       # None coerced
    assert by_slug["a"]["tenant_count"] == 3
    assert by_slug["c"]["tenant_count"] == 1
    assert [g["name"] for g in res["groups"]] == ["A", "B", "C"]  # sorted by name


def test_ungrouped_tenant_is_never_a_member():
    res = _engine(_NESTED).get_tenant_groups()
    for g in res["groups"]:
        assert "loose" not in g["tenant_slugs"]


def test_parent_cycle_does_not_hang():
    """NetBox can't express a parent loop, but a corrupt fixture can."""
    rows = {
        "/api/tenancy/tenant-groups/": [
            {"id": 1, "name": "X", "slug": "x",
             "parent": {"id": 2, "slug": "y"}, "description": ""},
            {"id": 2, "name": "Y", "slug": "y",
             "parent": {"id": 1, "slug": "x"}, "description": ""},
        ],
        "/api/tenancy/tenants/": [
            {"id": 9, "name": "T", "slug": "t", "group": {"id": 1, "slug": "x"}},
        ],
    }
    res = _engine(rows).get_tenant_groups()
    assert res["status"] == "SUCCESS"
    assert {g["slug"] for g in res["groups"]} == {"x", "y"}


def test_api_error_is_reported_not_raised():
    eng = NetboxEngine.__new__(NetboxEngine)
    eng.nb = MagicMock()

    def _boom(path, params=None, max_pages=200):
        raise Exception("503 Service Unavailable")

    eng._api_get_all = _boom
    res = eng.get_tenant_groups()
    assert res["status"] == "ERROR"
    assert "503" in res["message"]


# ─── get_tenants: group tagging ──────────────────────────────────────────────

def test_get_tenants_tags_group():
    res = _engine(_NESTED).get_tenants()
    assert res["status"] == "SUCCESS"
    by_slug = {t["slug"]: t for t in res["tenants"]}

    assert by_slug["t_b"]["group_slug"] == "b"
    assert by_slug["t_b"]["group_name"] == "B"
    # "group": null must not blow up and must yield empty strings, not None.
    assert by_slug["loose"]["group_slug"] == ""
    assert by_slug["loose"]["group_name"] == ""


# ─── list filters: tenant_group supersedes tenant ────────────────────────────

_LIST_CASES = [
    ("get_devices", "/api/dcim/devices/"),
    ("get_racks", "/api/dcim/racks/"),
    ("get_prefixes", "/api/ipam/prefixes/"),
    ("get_ip_addresses", "/api/ipam/ip-addresses/"),
]


@pytest.mark.parametrize("method,path", _LIST_CASES)
def test_tenant_group_supersedes_tenant(method, path):
    eng = _engine({})
    getattr(eng, method)(tenant="ra", tenant_group="solution-tme")
    params = dict(eng.calls)[path]
    assert params["tenant_group"] == "solution-tme"
    assert "tenant" not in params


@pytest.mark.parametrize("method,path", _LIST_CASES)
def test_plain_tenant_filter_unchanged(method, path):
    eng = _engine({})
    getattr(eng, method)(tenant="ra")
    params = dict(eng.calls)[path]
    assert params["tenant"] == "ra"
    assert "tenant_group" not in params


@pytest.mark.parametrize("method,path", _LIST_CASES)
def test_unscoped_sends_neither_filter(method, path):
    eng = _engine({})
    getattr(eng, method)()
    params = dict(eng.calls)[path]
    assert "tenant" not in params and "tenant_group" not in params

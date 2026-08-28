"""Tests for ``DcimMixin.get_rack_elevation`` — the render model behind the
WebUI rack-elevation "View" button (#105). Covers:

* front + rear unit lists, top→bottom ordering (highest unit first);
* multi-U device occupies consecutive units (same device id at U42/41/40) —
  the WebUI merges these via rowspan;
* null/empty device slot (an RU with nothing installed) stays in the list with
  ``device: None`` so the grid keeps its empty cell;
* 0U / side devices (position null or 0, never in the elevation unit list)
  collected separately in ``zero_u``;
* a positioned device returned by the devices list is NOT duplicated into
  zero_u (it is already accounted for in ``positioned_ids``);
* device summary carries role ``color``, u_height from device_type, tenant,
  primary_ip, face stamped from the unit;
* rack-not-found → ERROR; rack meta exposes name/u_height/site/tenant.

Self-contained: inserts src/ on sys.path; fake ``nb`` + ``_api_get`` +
``_api_get_all`` — no live NetBox.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


# ─── helpers ──────────────────────────────────────────────────────────────────


class _Ref:
    """Nested object stand-in with a .name/.slug (pynetbox-Record-like)."""
    def __init__(self, name, slug=""):
        self.name = name
        self.slug = slug


class _Rack:
    def __init__(self, id, name, u_height, site, tenant):
        self.id = id
        self.name = name
        self.u_height = u_height
        self.site = site
        self.tenant = tenant


def _dev(id, name, model, role, color, status="active", u_height=1,
         position=None, primary_ip=None, tenant=None, face=None):
    """REST-shaped nested device dict (what the elevation + devices APIs return)."""
    return {
        "id": id,
        "name": name,
        "display": name,
        "position": position,
        "face": face,
        "device_type": {"display": model, "model": model, "u_height": u_height},
        "role": {"id": 1, "name": role, "color": color},
        "status": {"value": status, "label": status.title()},
        "primary_ip": ({"address": primary_ip} if primary_ip else None),
        "tenant": ({"name": tenant} if tenant else None),
    }


def _unit(unit, device, face):
    """One elevation endpoint entry: {id/name, face, device|null}."""
    return {"id": unit, "name": f"U{unit}", "face": face, "device": device}


def _engine(front_units, rear_units, all_devs, rack):
    eng = NetboxEngine("http://localhost", "tok")

    # BOTH the elevation and the device list go through _api_get_all: the
    # elevation endpoint is paginated and honours the deployment's page-size
    # cap, so it has to follow ``next`` links or a tall rack silently truncates.
    # The fake therefore has to dispatch on path — when it returned the device
    # list for every call, the elevation parsed devices as rack units.
    def _api_get_all(path, params=None):
        if path.endswith("/elevation/"):
            face = (params or {}).get("face")
            return list(front_units if face == "front" else rear_units)
        if "/devices/" in path:
            return list(all_devs)
        return []

    eng._api_get = MagicMock(return_value={"results": []})
    eng._api_get_all = MagicMock(side_effect=_api_get_all)
    eng.nb = MagicMock()
    eng.nb.dcim.racks.get = MagicMock(return_value=rack)
    return eng


# ─── tests ────────────────────────────────────────────────────────────────────


def test_front_rear_units_top_to_bottom():
    """Front face lists U42→U40 (multi-U A), U39 empty, U38 (B). Rear: U42 (C)."""
    A = _dev(11, "sw-hh1-01", "Aruba 3810M-24G", "Switch", "3aa84f", u_height=3,
             position=42, primary_ip="10.0.0.11/24", tenant="acme", face="front")
    B = _dev(12, "srv-hh1-01", "DL380", "Server", "9e9e9e", position=38,
             tenant="acme")
    C = _dev(13, "pdu-rear", "PDU", "Power", "ffb300", position=42, face="rear")
    front = [_unit(42, A, "front"), _unit(41, A, "front"), _unit(40, A, "front"),
             _unit(39, None, "front"), _unit(38, B, "front")]
    rear = [_unit(42, C, "rear")]
    rack = _Rack(7, "HH1", 42, _Ref("Site A", "site-a"), _Ref("acme"))
    eng = _engine(front, rear, [A, B, C], rack)

    res = eng.get_rack_elevation(7)
    assert res["status"] == "SUCCESS"
    # site_slug is carried alongside the display name because add_device_to_rack
    # takes a site SLUG — the WebUI's "Add device" from this view needs it to
    # prefill.
    assert res["rack"] == {"id": 7, "name": "HH1", "u_height": 42,
                           "site": "Site A", "site_slug": "site-a",
                           "tenant": "acme"}

    f = res["faces"]["front"]
    assert [u["unit"] for u in f] == [42, 41, 40, 39, 38]
    # multi-U: same device id occupies three consecutive units
    assert [u["device"]["id"] for u in f[:3]] == [11, 11, 11]
    # empty slot kept as a cell with no device
    assert f[3]["unit"] == 39 and f[3]["device"] is None
    assert f[4]["device"]["id"] == 12

    r = res["faces"]["rear"]
    assert [u["unit"] for u in r] == [42]
    assert r[0]["device"]["id"] == 13


def test_device_summary_fields():
    A = _dev(11, "sw-hh1-01", "Aruba 3810M-24G", "Switch", "3aa84f", u_height=3,
             position=42, primary_ip="10.0.0.11/24", tenant="acme", face="front")
    front = [_unit(42, A, "front")]
    rack = _Rack(7, "HH1", 42, _Ref("Site A"), _Ref("acme"))
    eng = _engine(front, [], [A], rack)

    dev = eng.get_rack_elevation(7)["faces"]["front"][0]["device"]
    assert dev["name"] == "sw-hh1-01"
    assert dev["model"] == "Aruba 3810M-24G"
    assert dev["u_height"] == 3
    assert dev["role"] == "Switch"
    assert dev["role_color"] == "3aa84f"
    assert dev["status"] == "active"
    assert dev["status_label"] == "Active"
    assert dev["primary_ip"] == "10.0.0.11/24"
    assert dev["tenant"] == "acme"
    # face stamped from the unit (defensive even if device already had it)
    assert dev["face"] == "front"


def test_zero_u_devices_collected_and_no_duplicates():
    """A 0U PDU (position None) goes into zero_u; a positioned device in the
    devices list is NOT duplicated (already in positioned_ids)."""
    A = _dev(11, "sw-hh1-01", "3810M", "Switch", "3aa84f", u_height=1, position=42)
    PDU = _dev(20, "pdu-hh1-01", "PDU", "Power", "ffb300", position=None)
    front = [_unit(42, A, "front")]
    # all_devs includes the positioned A again (devices API lists everything) +
    # the 0U PDU. A must be excluded from zero_u; PDU included.
    rack = _Rack(7, "HH1", 42, _Ref("Site A"), _Ref("acme"))
    eng = _engine(front, [], [A, PDU], rack)

    res = eng.get_rack_elevation(7)
    ids = [d["id"] for d in res["zero_u"]]
    assert 20 in ids          # PDU → zero_u
    assert 11 not in ids      # positioned A → not duplicated into zero_u
    assert res["zero_u"][0]["name"] == "pdu-hh1-01"


def test_rack_not_found():
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.dcim.racks.get = MagicMock(return_value=None)
    eng._api_get = MagicMock(return_value=[])
    eng._api_get_all = MagicMock(return_value=[])
    res = eng.get_rack_elevation(999)
    assert res["status"] == "ERROR"
    assert "not found" in res["message"].lower()


def test_unit_id_falls_back_to_name():
    """If the elevation entry lacks ``id``, the unit number is parsed from the
    ``name`` (e.g. 'U42')."""
    A = _dev(11, "sw", "3810M", "Switch", "3aa84f", position=1)
    # entry with no id, name 'U5'
    front = [{"id": None, "name": "U5", "face": "front", "device": A}]
    rack = _Rack(7, "HH1", 5, _Ref("Site A"), None)
    eng = _engine(front, [], [A], rack)
    u = eng.get_rack_elevation(7)["faces"]["front"][0]
    assert u["unit"] == 5

def test_empty_half_u_rows_dropped_but_occupied_ones_kept():
    """NetBox can emit half-U (0.5) granularity slots. This deployment never
    mounts at half-U, so an EMPTY half-U row is filler that renders as a blank
    '.5' line — drop it. An OCCUPIED half-U slot is real and must survive, or a
    device would silently vanish from the elevation."""
    A = _dev(11, "sw", "3810M", "Switch", "3aa84f", position=42)
    front = [
        _unit(42, A, "front"),
        {"id": 41.5, "name": "U41.5", "face": "front", "device": None},
        _unit(41, None, "front"),
        {"id": 40.5, "name": "U40.5", "face": "front", "device": A},
    ]
    rack = _Rack(7, "HH1", 42, _Ref("Site A", "site-a"), None)
    eng = _engine(front, [], [A], rack)

    units = eng.get_rack_elevation(7)["faces"]["front"]
    assert [u["unit"] for u in units] == [42, 41, 40.5]
    # Whole units are normalized to int, not left as floats, so the WebUI
    # renders "U42" rather than "U42.0".
    assert all(isinstance(u["unit"], int) for u in units[:2])

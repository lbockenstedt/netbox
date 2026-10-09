"""NetBox cable connections → flattened device/port link rows.

Feeds the ``nw`` module's topology map (see ``lm/core/src/nw_topology.py``),
which draws links but has never modelled NetBox cables, leaving gear that
exists only in NetBox (never logged into by the nw fleet) permanently
"unlinked" even when its physical cabling IS recorded. The interesting
failures are all about which terminations a cable actually resolves to:
NetBox models each end as a LIST of terminations (multi-point breakout
cables), and most rows in a real inventory terminate on something that is
not a device interface at all (power ports, rear ports, circuit
terminations) and must be skipped, not raised as an error.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


def _engine(api_rows):
    eng = NetboxEngine.__new__(NetboxEngine)
    eng.nb = MagicMock()
    eng.calls = []

    def _get_all(path, params=None, max_pages=200):
        eng.calls.append((path, dict(params or {})))
        return api_rows.get(path, [])

    eng._api_get_all = _get_all
    return eng


def _iface_termination(device_name, port_name):
    return {"object_type": "dcim.interface",
            "object": {"device": {"name": device_name}, "name": port_name}}


def test_interface_to_interface_cable_is_flattened():
    rows = {
        "/api/dcim/cables/": [{
            "id": 1,
            "a_terminations": [_iface_termination("SW1", "1/1/1")],
            "b_terminations": [_iface_termination("SW2", "1/1/48")],
            "status": {"value": "connected"},
            "label": "patch-1",
        }],
    }
    res = _engine(rows).get_cables()
    assert res["status"] == "SUCCESS"
    assert res["cables"] == [{
        "id": 1, "a_device": "SW1", "a_port": "1/1/1",
        "b_device": "SW2", "b_port": "1/1/48",
        "status": "connected", "label": "patch-1",
    }]


def test_non_interface_termination_is_skipped_not_raised():
    """A cable with one end on a power port (a PDU → device power cable, not a
    data link) carries nothing the topology map can draw and must be dropped
    silently rather than blowing up the whole fetch."""
    rows = {
        "/api/dcim/cables/": [
            {
                "id": 1,
                "a_terminations": [{"object_type": "dcim.powerport",
                                    "object": {"device": {"name": "PDU1"}, "name": "1"}}],
                "b_terminations": [_iface_termination("SW1", "psu")],
                "status": {"value": "connected"}, "label": "",
            },
            {
                "id": 2,
                "a_terminations": [_iface_termination("SW1", "1/1/2")],
                "b_terminations": [_iface_termination("SW3", "1/1/1")],
                "status": {"value": "connected"}, "label": "",
            },
        ],
    }
    res = _engine(rows).get_cables()
    assert res["status"] == "SUCCESS"
    assert len(res["cables"]) == 1
    assert res["cables"][0]["a_device"] == "SW1"
    assert res["cables"][0]["b_device"] == "SW3"


def test_empty_terminations_yield_no_cable():
    rows = {"/api/dcim/cables/": [{"id": 1, "a_terminations": [], "b_terminations": [],
                                   "status": {}, "label": ""}]}
    res = _engine(rows).get_cables()
    assert res["status"] == "SUCCESS"
    assert res["cables"] == []


def test_site_filter_is_forwarded():
    eng = _engine({})
    eng.get_cables(site="olks")
    params = dict(eng.calls)["/api/dcim/cables/"]
    assert params["site"] == "olks"


def test_api_error_is_reported_not_raised():
    eng = NetboxEngine.__new__(NetboxEngine)
    eng.nb = MagicMock()

    def _boom(path, params=None, max_pages=200):
        raise Exception("503 Service Unavailable")

    eng._api_get_all = _boom
    res = eng.get_cables()
    assert res["status"] == "ERROR"
    assert "503" in res["message"]

"""Auto-rack: name grammar, slot resolution (move-out, offline holder, conflict, cycles) and apply."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_autorack import parse_rack_name, plan  # noqa: E402
from netbox_engine import NetboxEngine  # noqa: E402

RACKS = [{"id": 1, "name": "N31", "u_height": 48, "site_id": 10, "tenant_id": None}]


def dev(i, name, rack=None, pos=None, online=True, h=1, face="front"):
    return {"id": i, "name": name, "site_id": 10, "tenant_id": None,
            "rack_id": rack, "position": pos, "face": face if rack else None,
            "online": online, "height": h, "full_depth": True}


def test_parse_name():
    p = parse_rack_name("MIPBE-SSPLM-N31-R45-VPNC1.mip.example.com")
    assert (p["site"], p["net"], p["tenant"], p["rack"], p["ru"], p["face"], p["device"]) == \
        ("MIP", "BE", "SSPLM", "N31", 45, "front", "VPNC1")
    assert parse_rack_name("mipbe-ssplm-n31-r45r-vpnc1")["face"] == "rear"
    assert parse_rack_name("MIPBE-SSPLM-N31-VPNC1") is None
    assert parse_rack_name("MIPBE-SSPLM-N31-R0-VPNC1") is None
    p = parse_rack_name("mipbe-ssplm-pxmx01-n31-r18-ilo.mip.example.com")
    assert (p["rack"], p["ru"], p["face"], p["device"]) == ("N31", 18, "front", "pxmx01-ilo")
    assert parse_rack_name("MIPBE-SSPLM-PXMX01-N31-R18R")["face"] == "rear"
    assert parse_rack_name("mipbe-ssplm-pxmx01-ilo") is None


def test_places_free_slot_and_noop_when_in_place():
    out = plan([dev(1, "MIPBE-SSPLM-N31-R45-A", h=2), dev(2, "MIPBE-SSPLM-N31-R10-B", 1, 10)], RACKS)
    assert [(a["op"], a["id"], a["to"]) for a in out["actions"]] == [("place", 1, (1, 45, "front"))]
    assert out["report"] == []


def test_reports_unknown_rack_and_overflow():
    out = plan([dev(1, "MIPBE-SSPLM-Z99-R5-A"), dev(2, "MIPBE-SSPLM-N31-R48-B", h=2)], RACKS)
    reasons = {r["id"]: r["reason"] for r in out["report"]}
    assert "not found" in reasons[1] and "does not fit" in reasons[2]
    assert out["actions"] == []


def test_offline_holder_is_unracked():
    out = plan([dev(1, "MIPBE-SSPLM-N31-R20-NEW"),
                dev(2, "old-box", 1, 20, online=False)], RACKS)
    assert [a["op"] for a in out["actions"]] == ["unrack", "place"]
    assert out["report"] == []


def test_holder_that_moved_goes_first():
    # A's slot holder B now names a different slot: B moves out, then A moves in.
    out = plan([dev(1, "MIPBE-SSPLM-N31-R20-A"),
                dev(2, "MIPBE-SSPLM-N31-R30-B", 1, 20)], RACKS)
    assert [(a["op"], a["id"]) for a in out["actions"]] == [("move", 2), ("place", 1)]
    assert out["report"] == []


def test_probe_renamed_holder_is_replaced_first():
    # NetBox still says the holder is "old"; the re-poll shows it was renamed.
    holder = dev(2, "MIPBE-SSPLM-N31-R20-OLD", 1, 20)
    out = plan([dev(1, "MIPBE-SSPLM-N31-R20-A"), holder], RACKS,
               probe=lambda d: {"name": "MIPBE-SSPLM-N31-R30-OLD"} if d["id"] == 2 else None)
    assert [(a["op"], a["id"]) for a in out["actions"]] == [("move", 2), ("place", 1)]


def test_online_holder_named_for_slot_is_conflict():
    out = plan([dev(1, "MIPBE-SSPLM-N31-R20-A"),
                dev(2, "MIPBE-SSPLM-N31-R20-B", 1, 20)], RACKS)
    assert out["actions"] == []
    assert {r["id"] for r in out["report"] if r["kind"] == "conflict"} == {1, 2}


def test_unnamed_online_holder_blocks_and_is_kept():
    out = plan([dev(1, "MIPBE-SSPLM-N31-R20-A"), dev(2, "manual-box", 1, 20)], RACKS)
    assert out["actions"] == []
    assert out["report"][0]["kind"] == "cannot" and out["report"][0]["holder"] == "manual-box"


def test_swap_cycle_terminates_with_report():
    out = plan([dev(1, "MIPBE-SSPLM-N31-R20-A", 1, 30),
                dev(2, "MIPBE-SSPLM-N31-R30-B", 1, 20)], RACKS)
    assert out["actions"] == []
    assert out["report"] and all(r["kind"] == "cannot" for r in out["report"])


def _engine(rows, types, racks):
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng._journal = MagicMock()
    eng._api_get_all = MagicMock(side_effect=lambda path, params=None: {
        "/api/dcim/devices/": rows, "/api/dcim/racks/": racks,
        "/api/dcim/device-types/": types}[path])
    return eng


def test_sweep_applies_placement_and_reports_dry_run():
    rows = [{"id": 7, "name": "MIPBE-SSPLM-N31-R45R-VPNC1", "site": {"id": 3},
             "device_type": {"id": 5}, "status": {"value": "active"}}]
    types = [{"id": 5, "u_height": 2, "is_full_depth": False}]
    racks = [{"id": 1, "name": "N31", "u_height": 48, "site": {"id": 10}}]
    eng = _engine(rows, types, racks)
    obj = MagicMock()
    eng.nb.dcim.devices.get.return_value = obj
    dry = eng.autorack_sweep(dry_run=True)
    assert dry["placed"] == 1 and obj.save.call_count == 0
    res = eng.autorack_sweep()
    assert res["placed"] == 1 and res["cannot"] == []
    assert (obj.site, obj.rack, obj.position, obj.face) == (10, 1, 45, "rear")
    obj.save.assert_called_once()


def test_report_entries_carry_tenant_and_slots():
    rows = [{"id": 1, "name": "MIPBE-SSPLM-N31-R20-A", "site": {"id": 10}, "tenant": {"id": 4, "slug": "ssplm"},
             "device_type": {"id": 5}, "status": {"value": "active"}},
            {"id": 2, "name": "MIPBE-SSPLM-N31-R20-B", "site": {"id": 10}, "tenant": {"id": 4, "slug": "ssplm"},
             "device_type": {"id": 5}, "status": {"value": "active"}, "rack": {"id": 1}, "position": 20,
             "face": {"value": "front"}}]
    eng = _engine(rows, [{"id": 5, "u_height": 1}], [{"id": 1, "name": "N31", "u_height": 48, "site": {"id": 10}}])
    res = eng.autorack_sweep(dry_run=True)
    c = {x["id"]: x for x in res["conflicts"]}
    assert c[1]["tenant"] == "ssplm" and c[1]["wanted"] == "N31 U20 front" and c[1]["current"] == "unracked"
    assert c[2]["current"] == "N31 U20 front" and not c[2]["mismatch"]
    assert res["named"] == 2 and res["racked"] == 1


def test_two_half_width_devices_share_one_u_on_opposite_faces():
    from netbox_autorack import plan
    racks = [{"id": 1, "name": "N31", "site_id": 1, "tenant_id": None, "u_height": 48}]
    base = {"site_id": 1, "tenant_id": None, "rack_id": None, "position": None, "face": None,
            "online": True, "height": 1, "full_depth": False, "half_width": True}
    devs = [dict(base, id=1, name="MIPBE-SSPLM-N31-R10-A"), dict(base, id=2, name="MIPBE-SSPLM-N31-R10-B")]
    res = plan(devs, racks)
    assert res["report"] == []
    faces = sorted(a["to"][2] for a in res["actions"])
    assert faces == ["front", "rear"]
    again = [dict(d, rack_id=1, position=10, face=a["to"][2]) for d, a in zip(devs, res["actions"])]
    assert plan(again, racks)["actions"] == []


def test_two_normal_devices_in_one_u_still_conflict():
    from netbox_autorack import plan
    racks = [{"id": 1, "name": "N31", "site_id": 1, "tenant_id": None, "u_height": 48}]
    base = {"site_id": 1, "tenant_id": None, "rack_id": None, "position": None, "face": None,
            "online": True, "height": 1, "full_depth": False}
    devs = [dict(base, id=1, name="MIPBE-SSPLM-N31-R10-A"), dict(base, id=2, name="MIPBE-SSPLM-N31-R10-B")]
    assert plan(devs, racks)["report"]


def _host(i, name, ctl, **kw):
    return dict(dev(i, name, **kw), controller=ctl)


def test_server_takes_slot_from_controller_name_when_its_own_has_none():
    out = plan([_host(1, "tnas", "MIPBE-SSPLM-N31-R44-ILO-TNAS", h=2)], RACKS)
    assert [(a["op"], a["to"]) for a in out["actions"]] == [("place", (1, 44, "front"))]


def test_server_own_name_wins_when_controller_agrees_or_has_none():
    out = plan([_host(1, "MIPBE-SSPLM-N31-R30-A", "MIPBE-SSPLM-N31-R30-ILO-A"),
                _host(2, "MIPBE-SSPLM-N31-R31-B", "ilo-b")], RACKS)
    assert sorted(a["to"][1] for a in out["actions"]) == [30, 31] and out["report"] == []


def test_server_and_controller_naming_different_slots_cannot_rack():
    out = plan([_host(1, "MIPBE-SSPLM-N31-R30-A", "MIPBE-SSPLM-N31-R44-ILO-A")], RACKS)
    assert out["actions"] == []
    r = out["report"][0]
    assert r["kind"] == "cannot" and "U30" in r["reason"] and "U44" in r["reason"]


def test_sweep_server_racks_from_ilo_and_ilo_gives_up_the_u():
    rows = [{"id": 1, "name": "mipbe-ssplm-n31-tnas", "site": {"id": 10}, "device_type": {"id": 5},
             "status": {"value": "active"}, "role": {"slug": "server"},
             "custom_fields": {"linked_device": "MIPBE-SSPLM-N31-R44-ILO-TNAS"}},
            {"id": 2, "name": "MIPBE-SSPLM-N31-R44-ILO-TNAS", "site": {"id": 10}, "device_type": {"id": 5},
             "status": {"value": "active"}, "role": {"slug": "bmc"}, "rack": {"id": 1}, "position": 44,
             "face": {"value": "front"},
             "custom_fields": {"linked_device": "mipbe-ssplm-n31-tnas.mip.example.com"}}]
    eng = _engine(rows, [{"id": 5, "u_height": 2}], [{"id": 1, "name": "N31", "u_height": 48, "site": {"id": 10}}])
    objs = {1: MagicMock(), 2: MagicMock()}
    eng.nb.dcim.devices.get.side_effect = lambda i: objs[i]
    res = eng.autorack_sweep()
    assert res["cannot"] == [] and res["conflicts"] == [] and res["named"] == 1
    assert (objs[1].rack, objs[1].position) == (1, 44)
    assert objs[2].position is None and objs[2].rack is not None  # stays in the rack

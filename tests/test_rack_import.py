"""Tests for the Excel rack-layout importer:

* ``netbox_xlsx.detect_rack_sheets`` — detects both sheet shapes
  (one-rack-per-sheet + multi-rack summary) from an in-memory openpyxl workbook
  (no sample-file dependency) and guesses a column→field map.
* ``netbox_xlsx.parse_one_rack_sheet`` — applies a user column_map to produce
  per-device field dicts (skips nameless+serial-less rows).
* ``DcimMixin._resolve_device_type_slug`` — maps messy Excel model strings
  onto catalog slugs (stem + port-hint disambiguation; ambiguous flag).
* ``DcimMixin.import_rack_layout`` — idempotent rack+device+mgmt-iface+IP
  create/update; per-device error isolation; dry-run creates nothing.

Self-contained: inserts src/ on sys.path; builds the engine with a fake nb
(combinining the _Rec slug-side-effect harness from test_rack_tenant with the
_Obj/_Iface devices.create/interfaces.create/ip_addresses.create wiring from
test_sync_devices). No live NetBox.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import openpyxl  # noqa: E402

from netbox_engine import NetboxEngine  # noqa: E402
import netbox_xlsx as xx  # noqa: E402


# ─── shared fakes ─────────────────────────────────────────────────────────────


class _Rec:
    """pynetbox Record stand-in: arbitrary attrs + a save mock + an auto id."""
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.save = MagicMock()
        if not getattr(self, "id", None):
            self.id = id(self) % 100000


class _Iface:
    def __init__(self, id=100):
        self.id = id


def _catalog_slugs():
    import json
    return {t["slug"]: t for t in json.load(open(os.path.join(
        os.path.dirname(__name__) or ".", "src", "seed_catalog.json")))["device_types"]}


def _engine(tenants=(), sites=(), roles=(), device_types=()):
    """Engine wired with fake nb endpoints. ``device_types`` is a set of slugs
    that resolve to a live record (defaults to the full seed catalog)."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng._api_get = MagicMock(return_value={"results": []})  # no containing prefix → /32

    eng.nb.dcim.sites.get = MagicMock(
        side_effect=lambda slug=None, **kw: next((s for s in sites if s.slug == slug), None))
    eng.nb.tenancy.tenants.get = MagicMock(
        side_effect=lambda slug=None, **kw: next((t for t in tenants if t.slug == slug), None))
    eng.nb.dcim.device_roles.get = MagicMock(
        side_effect=lambda slug=None, name=None, **kw:
            next((r for r in roles if r.slug == (slug or "").lower()), None)
            or next((r for r in roles if r.name == name), None) if name else None)

    dt_slugs = device_types or set(_catalog_slugs().keys())

    def _dt_get(slug=None, model=None, **kw):
        if slug is not None and slug in dt_slugs:
            return _Rec(id=abs(hash(slug)) % 100000, slug=slug)
        if model is not None:
            cat = _catalog_slugs()
            for s, t in cat.items():
                if t["model"].lower() == (model or "").lower():
                    return _Rec(id=abs(hash(s)) % 100000, slug=s)
            return None
        return None
    eng.nb.dcim.device_types.get = MagicMock(side_effect=_dt_get)

    # racks: get by (name, site_id) → None by default (tests override)
    eng.nb.dcim.racks.get = MagicMock(return_value=None)
    eng.nb.dcim.racks.create = MagicMock(
        side_effect=lambda **kw: _Rec(id=9000 + len(eng.nb.dcim.racks.create.mock_calls), **kw))

    # devices: get by serial or (rack,name) → None by default
    eng.nb.dcim.devices.get = MagicMock(return_value=None)
    eng._created_devices = []

    def _dev_create(**kw):
        eng._created_devices.append(kw)
        return _Rec(**{**kw, "id": 8000 + len(eng._created_devices)})
    eng.nb.dcim.devices.create = MagicMock(side_effect=_dev_create)

    # interfaces: filter → [] (no existing mgmt); create → _Iface
    eng.nb.dcim.interfaces.filter = MagicMock(return_value=[])
    eng.nb.dcim.interfaces.create = MagicMock(side_effect=lambda **kw: _Iface(id=7000))
    eng.nb.ipam.ip_addresses.create = MagicMock(
        side_effect=lambda **kw: _Rec(id=6000, **kw))
    return eng


# ─── workbook builder ─────────────────────────────────────────────────────────


def _one_rack_wb():
    """A tiny HH1-shaped workbook: one-rack-per-sheet + a summary sheet."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HH1"
    header = ["RU", "F/R", "Status", "Function", "Type of device",
              "Hostname", "Part Number", "Serial Number", "MAC Address", "MGMT IP"]
    ws.append(header)
    # devices (RU ascending; Excel conventionally lists U1 at the bottom, but
    # detection only reads RU values, order-independent)
    ws.append([42, "F", "Active", "Switch", "3810M-24", "sw-hh1-01", "JL071A",
               "CN12345", "AA:BB:CC:DD:EE:01", "10.0.0.11"])
    ws.append([41, "F", "Active", "Switch", "6300M 24SR5 CL6", "sw-hh1-02", "JL072B",
               "CN67890", "AA:BB:CC:DD:EE:02", "10.0.0.12"])
    ws.append([40, "R", "Active", "Server", "DL360-G10", "srv-hh1-01", "P123",
               "CN99999", "", ""])
    # PDU at 0U with a type but NO hostname/serial → skipped by parse (surfaced)
    ws.append([0, "F", "Active", "PDU", "IPDU", "", "", "", "", ""])
    ws.append(["N/A", "", "", "", "4-Post 51U Rack", "", "", "", "", ""])  # metadata

    # summary sheet — mirrors the real MIP Rack Layout shape: a 'RACK <name>'
    # cell shares its row with the RU header; the next row carries Front/Rear
    # headers at ru_col+1 / ru_col+2; data rows put front/rear text there.
    sm = wb.create_sheet("MIP Rack Layout")
    sm.append(["TOC"])
    sm.append(["", "RU", "RACK AZ03 (LAB CORE)", "", ""])
    sm.append(["", "", "Front", "Rear"])
    sm.append(["", 48, "Cable Manager", "USB Hub (Console)"])
    sm.append(["", 47, "sw-az03-01", ""])
    sm.append(["", 46, "", "psu-az03-rear"])
    return wb


# ─── detection tests ──────────────────────────────────────────────────────────


def test_detect_finds_one_rack_sheet_and_guesses_column_map():
    sheets = xx.detect_rack_sheets(_one_rack_wb())
    one = next(s for s in sheets if s["shape"] == "one-rack")
    assert one["rack_name"] == "HH1"
    assert one["u_height"] == 51  # max RU 42 < metadata 51U → metadata wins
    cm = one["column_map"]
    assert cm["RU"] == "position"
    assert cm["F/R"] == "face"
    assert cm["Type of device"] == "device_type"
    assert cm["Hostname"] == "name"
    assert cm["Serial Number"] == "serial"
    assert cm["MGMT IP"] == "mgmt_ip"
    assert one["device_count"] >= 3  # the three named devices (PDU has no name)


def test_detect_finds_summary_block():
    sheets = xx.detect_rack_sheets(_one_rack_wb())
    summaries = [s for s in sheets if s["shape"] == "summary"]
    assert summaries, "expected a summary block from MIP Rack Layout"
    az = next(s for s in summaries if s["rack_name"] == "AZ03 (LAB CORE)")
    # front+rear rows → 4 device slots (48 front+rear, 47 front, 46 rear)
    assert az["device_count"] == 4
    assert az["u_height"] == 48


def test_parse_one_rack_applies_column_map_and_skips_nameless():
    wb = _one_rack_wb()
    ws = wb["HH1"]
    cm = {"RU": "position", "F/R": "face", "Type of device": "device_type",
          "Hostname": "name", "Serial Number": "serial", "MGMT IP": "mgmt_ip",
          "Part Number": "asset_tag", "MAC Address": "mac"}
    parsed = xx.parse_one_rack_sheet(ws, cm)
    names = {d.get("name") for d in parsed["devices"]}
    assert "sw-hh1-01" in names and "sw-hh1-02" in names
    # the named server (serial present) is kept; the PDU (no name, no serial) skipped
    assert "pdu-hh1-01" not in names
    assert parsed["skipped"] >= 1  # PDU had a device_type but no name/serial
    sw1 = next(d for d in parsed["devices"] if d["name"] == "sw-hh1-01")
    assert sw1["position"] == 42
    assert sw1["face"] == "F"
    assert sw1["mgmt_ip"] == "10.0.0.11"
    assert sw1["serial"] == "CN12345"


# ─── resolver tests ───────────────────────────────────────────────────────────


def test_resolve_exact_and_port_hint():
    eng = _engine()
    r = eng._resolve_device_type_slug("3810M-24")
    assert r["resolved"] and r["slug"] == "3810m-24g"
    assert r["match"] == "catalog-stem" and not r["ambiguous"]


def test_resolve_port_hint_disambiguates_24_vs_48():
    eng = _engine()
    assert eng._resolve_device_type_slug("6300M 24SR5 CL6")["slug"] == "6300m-24g"
    assert eng._resolve_device_type_slug("6300M-48")["slug"] == "6300m-48g"


def test_resolve_ambiguous_without_port_hint_flags():
    eng = _engine()
    r = eng._resolve_device_type_slug("3810M")
    assert r["resolved"]
    assert r["ambiguous"] is True  # 24G and 48G both match → flagged


def test_resolve_strips_cx_prefix_and_parens():
    eng = _engine()
    r = eng._resolve_device_type_slug("CX8325-32 (F2B)")
    assert r["resolved"] and r["slug"] == "8325-32c"


def test_resolve_unresolved_returns_not_resolved():
    eng = _engine()
    r = eng._resolve_device_type_slug("Cable Manager")
    assert r["resolved"] is False
    assert r["match"] == "unresolved"


# ─── import_rack_layout commit tests ─────────────────────────────────────────


def _selected(devices, **over):
    base = {"sheet": "HH1", "rack_name": "R1", "site_slug": "site-a",
            "u_height": 42, "tenant_slug": "acme",
            "default_role_slug": "switch", "default_status": "active",
            "devices": devices}
    base.update(over)
    return [base]


def test_import_creates_rack_device_with_position_face_tenant_serial_and_ip():
    eng = _engine(
        tenants=[_Rec(id=7, slug="acme")],
        sites=[_Rec(id=3, slug="site-a")],
        roles=[_Rec(id=20, slug="switch", name="Switch")])
    # racks.get: first call (get-or-create check) → None; after add_rack the
    # engine re-gets to attach devices → return the created rack.
    created_rack = _Rec(id=9001, name="R1", u_height=42, tenant=7)
    eng.nb.dcim.racks.get = MagicMock(side_effect=[None, created_rack])
    devs = [{"name": "sw-01", "device_type": "3810M-24", "serial": "CN1",
             "position": 42, "face": "F", "mgmt_ip": "10.0.0.11", "mac": "AA:BB:CC:DD:EE:01"}]
    res = eng.import_rack_layout(_selected(devs))
    assert res["status"] == "SUCCESS"
    assert res["racks_created"] == 1
    assert res["devices_created"] == 1
    assert res["ips_assigned"] == 1
    assert res["interfaces_created"] == 1
    dev_payload = eng._created_devices[0]
    assert dev_payload["rack"] == 9001
    assert dev_payload["position"] == 42
    assert dev_payload["face"] == "front"
    assert dev_payload["tenant"] == 7
    assert dev_payload["serial"] == "CN1"
    assert dev_payload["device_type"] == eng.nb.dcim.device_types.get(slug="3810m-24g").id
    eng.nb.ipam.ip_addresses.create.assert_called_once()
    ip_kw = eng.nb.ipam.ip_addresses.create.call_args.kwargs
    assert ip_kw["address"] == "10.0.0.11/32"  # no containing prefix → /32
    assert ip_kw["assigned_object_type"] == "dcim.interface"
    assert ip_kw["tenant"] == 7
    assert ip_kw["dns_name"] == "sw-01"


def test_import_idempotent_rerun_updates_not_duplicates():
    eng = _engine(
        tenants=[_Rec(id=7, slug="acme")],
        sites=[_Rec(id=3, slug="site-a")],
        roles=[_Rec(id=20, slug="switch", name="Switch")])
    # First run: rack absent, device absent → created.
    eng.import_rack_layout(_selected(
        [{"name": "sw-01", "device_type": "3810M-24", "serial": "CN1",
          "position": 42, "face": "F"}]))
    assert eng._created_devices and eng.nb.dcim.racks.create.called

    # Second run: rack exists, device exists (by serial) → updated, not created.
    eng.nb.dcim.racks.create.reset_mock()
    eng.nb.dcim.devices.create.reset_mock()
    rack_existing = _Rec(id=9001, name="R1", u_height=42, tenant=7)
    eng.nb.dcim.racks.get = MagicMock(return_value=rack_existing)
    existing_dev = _Rec(id=8001, name="sw-01", serial="CN1", position=42,
                        face="front", tenant=7, device_type=123)
    eng.nb.dcim.devices.get = MagicMock(return_value=existing_dev)

    res = eng.import_rack_layout(_selected(
        [{"name": "sw-01", "device_type": "3810M-24", "serial": "CN1",
          "position": 41, "face": "R"}]))
    assert res["racks_updated"] == 1 and res["racks_created"] == 0
    assert res["devices_updated"] == 1 and res["devices_created"] == 0
    eng.nb.dcim.racks.create.assert_not_called()
    eng.nb.dcim.devices.create.assert_not_called()
    existing_dev.save.assert_called()  # position/face changed → saved


def test_import_unresolved_device_type_is_per_device_error_rack_still_imports():
    eng = _engine(
        tenants=[_Rec(id=7, slug="acme")],
        sites=[_Rec(id=3, slug="site-a")],
        roles=[_Rec(id=20, slug="switch", name="Switch")])
    devs = [
        {"name": "sw-01", "device_type": "3810M-24", "position": 42, "face": "F"},
        {"name": "pdu-01", "device_type": "Cable Manager", "position": 0, "face": "F"},
    ]
    res = eng.import_rack_layout(_selected(devs))
    assert res["racks_created"] == 1          # rack still imported
    assert res["devices_created"] == 1        # only the resolved one
    assert res["skipped_devices"] == 1
    assert any("Cable Manager" in e["message"] for e in res["errors"])


def test_import_dry_run_creates_nothing_but_counts():
    eng = _engine(
        tenants=[_Rec(id=7, slug="acme")],
        sites=[_Rec(id=3, slug="site-a")],
        roles=[_Rec(id=20, slug="switch", name="Switch")])
    devs = [{"name": "sw-01", "device_type": "3810M-24", "position": 42, "face": "F"}]
    res = eng.import_rack_layout(_selected(devs), dry_run=True)
    assert res["dry_run"] is True
    assert res["devices_created"] == 1  # would-create count
    assert res["racks_created"] == 1
    eng.nb.dcim.racks.create.assert_not_called()
    eng.nb.dcim.devices.create.assert_not_called()
    eng.nb.ipam.ip_addresses.create.assert_not_called()


def test_import_unresolvable_tenant_skips_rack():
    eng = _engine(
        tenants=[_Rec(id=7, slug="acme")],
        sites=[_Rec(id=3, slug="site-a")],
        roles=[_Rec(id=20, slug="switch", name="Switch")])
    res = eng.import_rack_layout(_selected(
        [{"name": "sw-01", "device_type": "3810M-24", "position": 42, "face": "F"}],
        tenant_slug="ghost"))
    assert res["racks_created"] == 0
    assert any("ghost" in e["message"] for e in res["errors"])
    eng.nb.dcim.devices.create.assert_not_called()
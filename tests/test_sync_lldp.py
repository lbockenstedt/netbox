"""LLDP -> NetBox: the hub's merged topology links become cables, an unknown
neighbour is created as a discovered device, and every device name NetBox
gets is upper-case (``_nb_name``) while all matching stays case-insensitive."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


def _rec(dev_id, name, cf=None):
    r = MagicMock()
    r.id = dev_id
    r.name = name
    r.custom_fields = dict(cf or {})
    return r


def _engine(rows, recs):
    eng = NetboxEngine.__new__(NetboxEngine)
    eng.nb = MagicMock()
    eng._api_get_all = MagicMock(return_value=rows)
    eng.nb.dcim.devices.get.side_effect = lambda *a, **k: recs.get(a[0]) if a else None
    eng._journal = MagicMock()
    eng._ensure_custom_fields = MagicMock()
    eng._ensure_device_role = MagicMock(return_value=MagicMock(id=5))
    eng._ensure_device_type = MagicMock(return_value=MagicMock(id=6))
    eng._resolve_site = MagicMock(return_value=MagicMock(id=7))
    eng._resolve_tenant_ci = MagicMock(return_value=MagicMock(id=1))
    eng.cabled = []

    def cable(a, a_port, b, b_port, status="connected"):
        eng.cabled.append((a.name, a_port, b.name, b_port))
        return {"status": "SUCCESS"}

    eng._cable_devices = cable
    return eng


def test_nb_name_and_uniq_name_are_upper_case():
    assert NetboxEngine._nb_name("  mipbe-ssplm-n31-tor ") == "MIPBE-SSPLM-N31-TOR"
    assert NetboxEngine._nb_name(None) == ""
    name = NetboxEngine._uniq_device_name("ks205", "aa:bb:cc:dd:ee:0f", "",
                                          {"ks205": {}}, set())
    assert name == "KS205-EE0F"


def test_lldp_links_match_existing_by_mac_and_short_name_and_create_unknown():
    rows = [
        {"id": 10, "name": "mipbe-ssplm-n31-crsw1",
         "custom_fields": {"mac_address": "ec:50:aa:f4:5a:00"}},
        {"id": 11, "name": "mipbe-ssplm-n31-tor"},
    ]
    recs = {10: _rec(10, "MIPBE-SSPLM-N31-CRSW1", {"mac_address": "ec:50:aa:f4:5a:00"}),
            11: _rec(11, "mipbe-ssplm-n31-tor")}
    eng = _engine(rows, recs)
    created = _rec(99, "MIPBE-SSPLM-PXMX02")
    eng.nb.dcim.devices.create.return_value = created

    crsw1 = {"name": "10.0.0.1", "macs": ["EC:50:AA:F4:5A:00"]}
    res = eng.sync_lldp_links(links=[
        # TOR advertised as an FQDN, NetBox has the short lower-case name.
        {"a": crsw1, "a_port": "1/1/40",
         "b": {"name": "MIPBE-SSPLM-N31-TOR.orange-tme.com"}, "b_port": "28"},
        # The server NetBox has never seen -> created, upper short name.
        {"a": crsw1, "a_port": "1/1/2",
         "b": {"name": "mipbe-ssplm-pxmx02.orange-tme.com",
               "macs": ["b0:26:28:2d:52:90"]}, "b_port": "nic1"},
        # A link missing a port is skipped, not guessed.
        {"a": crsw1, "a_port": "", "b": {"name": "x"}, "b_port": "1"},
    ], tenant_slug="default")

    assert res["status"] == "SUCCESS", res
    assert res["cabled"] == 2 and res["created"] == 1 and res["skipped"] == 1
    ck = eng.nb.dcim.devices.create.call_args.kwargs
    assert ck["name"] == "MIPBE-SSPLM-PXMX02"
    assert ck["tenant"] == 1 and ck["role"] == 5 and ck["device_type"] == 6
    assert created.custom_fields["discovered_from"] == "LLDP"
    assert created.custom_fields["mac_address"] == "b0:26:28:2d:52:90"
    assert eng.cabled == [
        ("MIPBE-SSPLM-N31-CRSW1", "1/1/40", "mipbe-ssplm-n31-tor", "28"),
        ("MIPBE-SSPLM-N31-CRSW1", "1/1/2", "MIPBE-SSPLM-PXMX02", "nic1"),
    ]


def test_lldp_never_creates_a_device_named_by_ip_or_mac():
    eng = _engine([], {})
    res = eng.sync_lldp_links(links=[
        {"a": {"name": "172.21.0.9"}, "a_port": "1",
         "b": {"name": "84:16:0c:54:af:20"}, "b_port": "2"},
    ])
    eng.nb.dcim.devices.create.assert_not_called()
    assert res["skipped"] == 1 and res["cabled"] == 0


def test_device_by_name_ignores_case_and_domain():
    eng = NetboxEngine.__new__(NetboxEngine)
    eng.nb = MagicMock()
    target = _rec(3, "MIPBE-SSPLM-PXMX02")
    eng.nb.dcim.devices.get.return_value = None
    eng.nb.dcim.devices.filter.side_effect = lambda **kw: (
        [] if "name__ie" in kw else [target, _rec(4, "MIPBE-SSPLM-PXMX02-ILO")])
    assert eng._device_by_name("mipbe-ssplm-pxmx02.orange-tme.com") is target
    assert eng._device_by_name("") is None


def test_normalize_device_names_renames_lower_and_reports_collisions():
    rows = [{"id": 1, "name": "sw-a"}, {"id": 2, "name": "SW-B"}, {"id": 3, "name": "sw-c"}]
    a, c = _rec(1, "sw-a"), _rec(3, "sw-c")
    c.save.side_effect = Exception("Device name must be unique per site.")
    eng = _engine(rows, {1: a, 3: c})
    res = eng.normalize_device_names()
    assert a.name == "SW-A" and a.save.called
    assert res["renamed"] == 1 and res["collisions"] == 1 and res["errors"] == 0
    assert eng.nb.dcim.devices.get.call_count == 2  # SW-B untouched


def test_same_mac_under_two_names_creates_one_device():
    eng = _engine([], {})
    made = _rec(50, "SRV1")
    eng.nb.dcim.devices.create.return_value = made
    eng.nb.dcim.devices.get.side_effect = lambda *a, **k: made if a and a[0] == 50 else None
    sw = {"name": "sw", "macs": ["00:11:22:33:44:55"]}
    res = eng.sync_lldp_links(links=[
        {"a": sw, "a_port": "1", "b": {"name": "srv1", "macs": ["aa:aa:aa:aa:aa:01"]}, "b_port": "nic0"},
        {"a": sw, "a_port": "2", "b": {"name": "srv1.lab.example", "macs": ["AA-AA-AA-AA-AA-01"]}, "b_port": "nic1"},
    ])
    # "sw" is created once too; srv1 is created once and re-found by MAC.
    assert [c.kwargs["name"] for c in eng.nb.dcim.devices.create.call_args_list].count("SRV1") == 1
    assert res["errors"] == 0


def test_truncated_or_junk_lldp_names_are_never_created():
    eng = _engine([], {})
    res = eng.sync_lldp_links(links=[
        {"a": {"name": "PXMX.OLTH.LRBTE..."}, "a_port": "1", "b": {"name": "B"}, "b_port": "2"},
    ])
    eng.nb.dcim.devices.create.assert_not_called()
    assert res["skipped"] == 1

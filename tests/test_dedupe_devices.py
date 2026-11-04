"""Tests for NetboxEngine.dedupe_devices — the duplicate-merge sweep.

Pre-existing duplicates (same box landed as separate devices before the sync
ladder was unified) are grouped transitively by shared serial/MAC, the most
complete record survives, and — only on apply=True — the rest are merged
(gap-only backfill + delete). apply=False is a dry run.

Self-contained harness mirroring test_sync_devices: engine built without a live
NetBox; _api_get_all returns the device rows.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Obj:
    """Minimal pynetbox-record stand-in with save/delete mocks."""
    def __init__(self, id=1, name="", serial="", custom_fields=None):
        self.id = id
        self.name = name
        self.serial = serial
        self.custom_fields = dict(custom_fields or {})
        self.save = MagicMock()
        self.delete = MagicMock()


def _engine(rows):
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng._journal = MagicMock()
    eng._api_get_all = MagicMock(return_value=list(rows))
    # No IPs to repoint by default.
    eng.nb.ipam.ip_addresses.filter = MagicMock(return_value=[])
    return eng


def _row(id, name="", serial="", mac="", nw=None, dt_slug="discovered", ip=None):
    cf = {}
    if mac:
        cf["mac_address"] = mac
    if nw:
        cf["nw_device_id"] = nw
    row = {"id": id, "name": name, "serial": serial, "custom_fields": cf,
           "device_type": {"slug": dt_slug}}
    if ip:
        row["primary_ip4"] = {"address": ip}
    return row


def test_dedupe_dry_run_groups_by_serial_no_changes():
    rows = [_row(1, name="console-sw", serial="SN-1"),
            _row(2, name="nw-sw", serial="SN-1", nw="nw-9"),
            _row(3, name="unrelated", serial="SN-2")]
    eng = _engine(rows)
    res = eng.dedupe_devices(tenant_slug="lrb", apply=False)
    assert res["status"] == "SUCCESS"
    assert res["applied"] is False
    assert res["merged"] == 0
    assert len(res["groups"]) == 1
    g = res["groups"][0]
    assert g["key"] == "serial:sn-1"
    assert set([g["survivor_id"]] + g["duplicate_ids"]) == {1, 2}
    # Nothing deleted on a dry run.
    eng.nb.dcim.devices.get.assert_not_called()


def test_dedupe_transitive_serial_then_mac_one_group():
    # A≡B by serial, B≡C by mac → {A,B,C} is one physical box.
    rows = [_row(1, serial="SN-1"),
            _row(2, serial="SN-1", mac="aa:bb:cc:dd:ee:ff"),
            _row(3, mac="aa:bb:cc:dd:ee:ff")]
    eng = _engine(rows)
    res = eng.dedupe_devices(apply=False)
    assert len(res["groups"]) == 1
    g = res["groups"][0]
    assert set([g["survivor_id"]] + g["duplicate_ids"]) == {1, 2, 3}


def test_dedupe_apply_merges_backfills_and_deletes():
    # Survivor should be the most complete (id 2: serial+mac+real type). The
    # duplicate (id 1) carries an nw_device_id the survivor lacks → backfilled.
    survivor_row = _row(2, name="sw", serial="SN-1", mac="aa:bb:cc:dd:ee:01",
                        dt_slug="aruba-2930f")
    dupe_row = _row(1, name="sw-dup", serial="SN-1", nw="nw-77")
    eng = _engine([survivor_row, dupe_row])
    sobj = _Obj(id=2, name="sw", serial="SN-1",
                custom_fields={"mac_address": "aa:bb:cc:dd:ee:01"})
    dobj = _Obj(id=1, name="sw-dup", serial="SN-1",
                custom_fields={"nw_device_id": "nw-77"})
    eng.nb.dcim.devices.get = MagicMock(side_effect=lambda i: {2: sobj, 1: dobj}[i])

    res = eng.dedupe_devices(apply=True)
    assert res["status"] == "SUCCESS", res
    assert res["applied"] is True
    assert res["merged"] == 1
    assert res["deleted"] == 1
    # Duplicate deleted; survivor kept.
    dobj.delete.assert_called_once()
    sobj.delete.assert_not_called()
    # nw_device_id gap backfilled onto the survivor.
    assert sobj.custom_fields.get("nw_device_id") == "nw-77"


def test_dedupe_survivor_backfills_serial_from_dupe_when_missing():
    survivor_row = _row(5, name="by-mac", mac="aa:bb:cc:dd:ee:02")
    dupe_row = _row(6, name="by-serial", serial="SN-9", mac="aa:bb:cc:dd:ee:02")
    eng = _engine([survivor_row, dupe_row])
    # id 6 has serial (score 8+4) vs id 5 (4) → 6 survives.
    sobj = _Obj(id=6, name="by-serial", serial="SN-9",
                custom_fields={"mac_address": "aa:bb:cc:dd:ee:02"})
    dobj = _Obj(id=5, name="by-mac", serial="",
                custom_fields={"mac_address": "aa:bb:cc:dd:ee:02"})
    eng.nb.dcim.devices.get = MagicMock(side_effect=lambda i: {6: sobj, 5: dobj}[i])
    res = eng.dedupe_devices(apply=True)
    assert res["groups"][0]["survivor_id"] == 6
    assert res["merged"] == 1
    dobj.delete.assert_called_once()


def test_dedupe_no_duplicates_empty_groups():
    rows = [_row(1, serial="SN-1"), _row(2, serial="SN-2", mac="aa:bb:cc:dd:ee:03")]
    eng = _engine(rows)
    res = eng.dedupe_devices(apply=True)
    assert res["groups"] == []
    assert res["merged"] == 0
    assert res["scanned"] == 2


def _r(id, name, tenant="t", ip=None, last_seen="2026-11-01", site="discovered", rack=None, **kw):
    row = _row(id, name=name, ip=ip, **kw)
    row["tenant"] = {"slug": tenant}
    row["site"] = {"slug": site}
    if rack:
        row["rack"] = {"id": rack}
    if last_seen:
        row["custom_fields"]["last_seen"] = last_seen
    return row


def _ids(g):
    return set([g["survivor_id"]] + g["duplicate_ids"])


def test_dedupe_ignores_shared_macs_and_placeholder_serials():
    rows = [_row(1, mac="00:00:5e:00:01:01"), _row(2, mac="00:00:5e:00:01:01"),
            _row(3, serial="N/A"), _row(4, serial="N/A")]
    assert _engine(rows).dedupe_devices()["groups"] == []


def test_dedupe_name_and_ip_merges_only_within_a_tenant():
    rows = [_r(1, "FW", ip="10.0.0.1/24"), _r(2, "fw", ip="10.0.0.1/32"),
            _r(3, "FW", tenant="other", ip="10.0.0.1/24")]
    groups = _engine(rows).dedupe_devices()["groups"]
    assert [_ids(g) for g in groups] == [{1, 2}]


def test_dedupe_legacy_row_folds_into_live_record_across_tenants():
    rows = [_r(925, "BO-INDm-CRFW01", tenant="old", last_seen=""),
            _r(4455, "BO-INDM-CRFW01", serial="CZ2623AN0483"),
            _r(7, "HOSTNAME", last_seen=""), _r(8, "hostname", last_seen="")]
    groups = _engine(rows).dedupe_devices()["groups"]
    assert len(groups) == 1 and groups[0]["survivor_id"] == 4455
    assert groups[0]["duplicate_ids"] == [925]


def test_dedupe_racked_device_survives_and_is_never_legacy():
    rows = [_r(1, "SW1", rack=5, last_seen=""), _r(2, "SW1", mac="aa:bb:cc:dd:ee:01", serial="SN99")]
    assert _engine(rows).dedupe_devices()["groups"] == []
    rows = [_r(1, "SW1", rack=5, mac="aa:bb:cc:dd:ee:01"), _r(2, "SW1", mac="aa:bb:cc:dd:ee:01", serial="SN99")]
    assert _engine(rows).dedupe_devices()["groups"][0]["survivor_id"] == 1

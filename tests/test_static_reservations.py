"""NETBOX_RESERVE_STATIC_HOSTS: the planner, before/after writes, rollback of
a pass when a later write fails, the hub-sent rollback, and the command
handler. NetBox is a small in-memory fake (no live NetBox)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import netbox_spoke as spoke_mod  # noqa: E402
import netbox_static_reservations as sr  # noqa: E402

MAC = "aa:bb:cc:dd:ee:01"
MAC2 = "aa:bb:cc:dd:ee:02"
PREFIX = {"prefix": "10.0.0.0/24", "status": "active", "vrf": "",
          "custom_fields": {"dhcp_enabled": True}}
SUBNET = {"subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.100 - 10.0.0.200"}]}


class _Rec:
    """pynetbox IP record stand-in; ``fail`` makes save() raise."""

    def __init__(self, engine, ip_id, custom_fields):
        self.engine, self.id, self.custom_fields = engine, ip_id, custom_fields

    def save(self):
        if self.id in self.engine.fail_ids:
            raise RuntimeError("netbox said no")
        self.engine.saves.append(self.id)
        self.engine.stored[self.id] = dict(self.custom_fields)


class _Engine:
    """The engine surface the pass uses: two reads and ip_addresses.get."""

    def __init__(self, ips):
        # ips: {ip_id: (address, custom_fields)}
        self.addresses = {i: a for i, (a, _) in ips.items()}
        self.stored = {i: dict(cf) for i, (_, cf) in ips.items()}
        self.saves, self.fail_ids = [], set()
        self.nb = self
        self.ipam = self
        self.ip_addresses = self

    def get(self, ip_id):
        return _Rec(self, ip_id, dict(self.stored[ip_id])) if ip_id in self.stored else None

    def get_prefixes(self, **_filters):
        return {"status": "SUCCESS", "prefixes": [PREFIX]}

    def get_ip_addresses(self, **_filters):
        return {"status": "SUCCESS", "ip_addresses": [
            {"id": i, "address": a, "status": "active", "vrf": "",
             "custom_fields": dict(self.stored[i])} for i, a in self.addresses.items()]}


def _data(agent_addresses, leases=(), max_writes=25):
    return {"tenants": [{"ref": "0", "filters": {"tenant": "acme"},
                         "agent_addresses": list(agent_addresses), "kea": "k0"}],
            "kea": {"k0": {"leases": list(leases), "subnets": [SUBNET]}},
            "max_writes": max_writes}


def _static(ip, mac):
    return {"ip": ip, "mac": mac, "mode": "static"}


def _plan(cands, leases=(), ips=None):
    ips = ips if ips is not None else [{"id": 1, "address": "10.0.0.5/24", "custom_fields": {}}]
    return sr.plan_static_reservations([PREFIX], ips, list(leases), cands, [SUBNET])


# ── planner ──────────────────────────────────────────────────────────────────

def test_agent_static_host_is_reserved_with_its_current_fields_as_before():
    plan = _plan([{**_static("10.0.0.5", "AA-BB-CC-DD-EE-01"), "source": "agent"}])
    assert plan["reserve"] == [{"ip_id": 1, "ip": "10.0.0.5", "mac": MAC, "source": "agent",
                                "before": {"mac_address": None, "dhcp_reservation": None}}]


def test_ha_lease_states_fail_closed_with_distinct_conflicts():
    cand = [{**_static("10.0.0.5", MAC), "source": "agent"}]
    declined = _plan(cand, leases=[{"ip-address": "10.0.0.5", "hw-address": ""}])
    mixed = _plan(cand, leases=[{"ip-address": "10.0.0.5", "hw-address": ""},
                                {"ip-address": "10.0.0.5", "hw-address": MAC}])
    assert [c["reason"] for c in declined["conflicts"]] == ["address declined in kea"]
    assert [c["reason"] for c in mixed["conflicts"]] == [
        "kea leases disagree (declined and leased)"]
    assert not declined["reserve"] and not mixed["reserve"]


def test_sweep_rows_count_only_when_fresh_and_outside_the_pools():
    tenant = {"sweep": [
        {"ip": "10.0.0.5", "mac": MAC, "seen_age": 60, "arp_age": 60},
        {"ip": "10.0.0.6", "mac": MAC2, "seen_age": sr.SEEN_WINDOW + 1, "arp_age": 60},
        {"ip": "10.0.0.150", "mac": "aa:bb:cc:dd:ee:03", "seen_age": 60, "arp_age": 60},
    ]}
    ips = [{"id": n, "address": f"10.0.0.{h}/24", "custom_fields": {}}
           for n, h in ((1, 5), (2, 6), (3, 150))]
    plan = _plan(sr.candidates(tenant), ips=ips)
    assert [r["ip"] for r in plan["reserve"]] == ["10.0.0.5"]
    assert {"ip": "10.0.0.150", "reason": "inside a dhcp pool"} in plan["skipped"]


def test_agent_dhcp_answer_vetoes_the_sweep():
    cands = [{**_static("10.0.0.5", MAC), "source": "sweep"},
             {"ip": "10.0.0.5", "mac": MAC, "mode": "dhcp", "source": "agent"}]
    assert _plan(cands)["skipped"] == [{"ip": "10.0.0.5", "reason": "agent reports dhcp"}]


# ── pass: writes, before/after, rollback ─────────────────────────────────────

def test_pass_reports_before_after_and_a_second_pass_writes_nothing():
    eng = _Engine({1: ("10.0.0.5/24", {"owner": "lab"})})
    first = sr.reserve_static_hosts(eng, _data([_static("10.0.0.5", MAC)]), {}, 1000.0)
    assert first["status"] == "SUCCESS" and first["evaluated"] == ["0"]
    assert first["changes"] == [{
        "ref": "0", "ip": "10.0.0.5", "ip_id": 1, "mac": MAC, "source": "agent",
        "before": {"mac_address": None, "dhcp_reservation": None},
        "after": {"mac_address": MAC, "dhcp_reservation": True}}]
    assert eng.stored[1] == {"owner": "lab", "mac_address": MAC, "dhcp_reservation": True}
    second = sr.reserve_static_hosts(eng, _data([_static("10.0.0.5", MAC)]), {}, 2000.0)
    assert second["changes"] == [] and eng.saves == [1]


def test_failed_write_rolls_back_the_pass_and_backs_off_that_address():
    eng = _Engine({1: ("10.0.0.5/24", {}), 2: ("10.0.0.6/24", {})})
    eng.fail_ids = {2}
    failed = {}
    data = _data([_static("10.0.0.5", MAC), _static("10.0.0.6", MAC2)])
    res = sr.reserve_static_hosts(eng, data, failed, 1000.0)
    assert res["status"] == "ERROR" and res["changes"] == []
    assert [c["ip"] for c in res["rolled_back"]] == ["10.0.0.5"]
    assert res["rollback_failed"] == []
    assert eng.stored[1] == {"mac_address": None, "dhcp_reservation": None}
    assert [e["ip"] for e in res["errors"]] == ["10.0.0.6"]
    # The next pass skips the failed address and writes the other one.
    again = sr.reserve_static_hosts(eng, data, failed, 1100.0)
    assert [c["ip"] for c in again["changes"]] == ["10.0.0.5"]
    assert again["errors"][0]["error"] == "earlier write failed; retry after backoff"


def test_write_cap_defers_the_rest():
    eng = _Engine({1: ("10.0.0.5/24", {}), 2: ("10.0.0.6/24", {})})
    res = sr.reserve_static_hosts(
        eng, _data([_static("10.0.0.5", MAC), _static("10.0.0.6", MAC2)], max_writes=1),
        {}, 1000.0)
    assert len(res["changes"]) == 1 and res["deferred"] == 1


def _changes(*rows):
    """Rollback input: (ip_id, ip, mac) written over empty fields."""
    return [{"ip": ip, "ip_id": i, "before": {"mac_address": None, "dhcp_reservation": None},
             "after": {"mac_address": m, "dhcp_reservation": True}} for i, ip, m in rows]


def test_rollback_restores_before_and_leaves_later_edits_alone():
    eng = _Engine({1: ("10.0.0.5/24", {"mac_address": MAC, "dhcp_reservation": True}),
                   2: ("10.0.0.6/24", {"mac_address": "11:22:33:44:55:66",
                                       "dhcp_reservation": True})})
    changes = _changes((1, "10.0.0.5", MAC), (2, "10.0.0.6", MAC2), (3, "10.0.0.7", MAC))
    res = sr.roll_back(eng, changes)
    assert [c["ip"] for c in res["rolled_back"]] == ["10.0.0.5"]
    assert [(c["ip"], c["reason"]) for c in res["rollback_kept"]] == [
        ("10.0.0.7", "netbox ip deleted since it was read"),
        ("10.0.0.6", "changed in netbox since it was read")]
    assert res["status"] == "SUCCESS" and res["rollback_failed"] == []
    assert eng.stored[2]["mac_address"] == "11:22:33:44:55:66"
    # Repeating the rollback (a lost ack) is harmless: already restored.
    again = sr.roll_back(eng, changes[:1])
    assert again["status"] == "SUCCESS" and len(again["rolled_back"]) == 1


def test_a_rollback_netbox_refuses_is_the_only_rollback_error():
    eng = _Engine({1: ("10.0.0.5/24", {"mac_address": MAC, "dhcp_reservation": True})})
    eng.fail_ids = {1}
    res = sr.roll_back(eng, _changes((1, "10.0.0.5", MAC)))
    assert res["status"] == "ERROR" and res["rollback_failed"][0]["error"] == "netbox said no"


def _racing_engine(edit):
    """Two planned IPs; after the plan's read, ``edit(stored)`` runs, as
    another writer would."""
    eng = _Engine({1: ("10.0.0.5/24", {}), 2: ("10.0.0.6/24", {})})
    read = eng.get_ip_addresses

    def _read_then_edit(**filters):
        rows = read(**filters)
        edit(eng.stored)
        return rows
    eng.get_ip_addresses = _read_then_edit
    return eng


def test_an_ip_changed_or_deleted_after_the_read_is_a_conflict_not_a_failed_pass():
    edits = {"changed in netbox since it was read":
             lambda st: st[2].update(mac_address="not-a-mac"),  # compared exactly
             "netbox ip deleted since it was read": lambda st: st.pop(2)}
    for reason, edit in edits.items():
        eng = _racing_engine(edit)
        failed = {}
        res = sr.reserve_static_hosts(
            eng, _data([_static("10.0.0.5", MAC), _static("10.0.0.6", MAC2)]), failed, 1000.0)
        assert res["status"] == "SUCCESS" and "rolled_back" not in res
        assert [c["ip"] for c in res["changes"]] == ["10.0.0.5"]
        assert res["conflicts"] == [{"ref": "0", "ip": "10.0.0.6", "mac": MAC2, "reason": reason}]
        assert failed == {}


def test_skipped_ips_are_counted_per_reason_without_addresses():
    eng = _Engine({1: ("10.0.0.5/24", {})})
    rows = [_static("10.0.0.5", MAC), _static("10.0.1.5", MAC2),
            _static("10.0.0.9", "aa:bb:cc:dd:ee:03")]
    sr.reserve_static_hosts(eng, _data(rows[:1]), {}, 1000.0)
    res = sr.reserve_static_hosts(eng, _data(rows), {}, 2000.0)
    assert res["skipped"] == 3
    assert res["skipped_reasons"] == {"already reserved": 1, "outside any dhcp scope": 1,
                                      "no netbox ip": 1}
    # Another writer set the same values after the read: counted at the write.
    eng = _racing_engine(lambda st: st[2].update(mac_address=MAC2, dhcp_reservation=True))
    res = sr.reserve_static_hosts(
        eng, _data([_static("10.0.0.5", MAC), _static("10.0.0.6", MAC2)]), {}, 1000.0)
    assert res["skipped_reasons"] == {"already set at write": 1}


def test_missing_kea_state_or_netbox_read_failure_is_an_error_not_an_empty_view():
    eng = _Engine({1: ("10.0.0.5/24", {})})
    data = _data([_static("10.0.0.5", MAC)])
    data["kea"]["k0"]["subnets"] = None
    res = sr.reserve_static_hosts(eng, data, {}, 1000.0)
    assert res["errors"] == [{"ref": "0", "error": "kea state missing"}] and not eng.saves
    eng.get_prefixes = lambda **_: {"status": "ERROR", "message": "timeout"}
    res = sr.reserve_static_hosts(eng, _data([_static("10.0.0.5", MAC)]), {}, 1000.0)
    assert res["errors"] == [{"ref": "0", "error": "netbox read: timeout"}]
    # Nothing could be evaluated: not an empty success.
    assert res["status"] == "ERROR" and res["evaluated"] == []


# ── command handler ──────────────────────────────────────────────────────────

def _spoke(eng):
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.engine = eng
    sp._picklist_cache = {}
    sp._list_cache = {"GET_IPS|x": {"ts": 0, "value": {}}}
    return sp


def test_command_writes_then_rolls_back_and_drops_the_list_cache():
    eng = _Engine({1: ("10.0.0.5/24", {})})
    sp = _spoke(eng)
    res = asyncio.run(sp.handle_command("NETBOX_RESERVE_STATIC_HOSTS",
                                        _data([_static("10.0.0.5", MAC)])))
    assert [c["ip"] for c in res["changes"]] == ["10.0.0.5"] and sp._list_cache == {}
    sp._list_cache = {"GET_IPS|x": {"ts": 0, "value": {}}}
    undo = asyncio.run(sp.handle_command("NETBOX_RESERVE_STATIC_HOSTS",
                                         {"rollback": res["changes"]}))
    assert undo["status"] == "SUCCESS" and len(undo["rolled_back"]) == 1
    assert eng.stored[1] == {"mac_address": None, "dhcp_reservation": None}
    assert sp._list_cache == {}


def test_a_pass_whose_rollback_failed_still_drops_the_list_cache():
    eng = _Engine({1: ("10.0.0.5/24", {}), 2: ("10.0.0.6/24", {})})
    sp = _spoke(eng)
    real_save = _Rec.save

    def _save(rec):
        # The second write fails, and so does every later write to IP 1.
        if rec.id == 2 or (rec.id == 1 and 1 in eng.saves):
            raise RuntimeError("netbox said no")
        real_save(rec)
    _Rec.save = _save
    try:
        res = asyncio.run(sp.handle_command("NETBOX_RESERVE_STATIC_HOSTS", _data(
            [_static("10.0.0.5", MAC), _static("10.0.0.6", MAC2)])))
    finally:
        _Rec.save = real_save
    assert res["changes"] == [] and res["rolled_back"] == []
    assert [c["ip"] for c in res["rollback_failed"]] == ["10.0.0.5"]
    assert sp._list_cache == {}

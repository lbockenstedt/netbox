"""Device replacement in the static-reservation pass: a NetBox reservation
whose host now answers with a new MAC moves to that MAC (with its
interface's MAC) automatically after the waits, or at once from the "Use new
MAC" button (``NETBOX_REPLACE_RESERVATION_MAC``), never past a safety check.
NetBox is a small in-memory fake. Documentation addresses and locally
administered MACs only."""
import asyncio
import copy
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import netbox_spoke as spoke_mod  # noqa: E402
import netbox_static_reservations as sr  # noqa: E402

IP = "192.0.2.10"
MAC_A = "02:00:00:00:00:0a"   # the old device
MAC_B = "02:00:00:00:00:0b"   # the replacement
MAC_C = "02:00:00:00:00:0c"
PREFIX = {"prefix": "192.0.2.0/24", "status": "active", "vrf": "",
          "custom_fields": {"dhcp_enabled": True}}
SUBNET = {"subnet": "192.0.2.0/24", "pools": [{"pool": "192.0.2.100 - 192.0.2.200"}]}
RESERVED_A = {"mac_address": MAC_A, "dhcp_reservation": True}
# The hub's watch once B was first seen 30 minutes ago and A unseen for a day.
WATCH_DONE = [{"ip": IP, "old_mac": MAC_A, "new_mac": MAC_B,
               "first_seen_age": 30 * 60,
               "old_unseen_age": 24 * 3600}]


class _Obj:
    """A pynetbox record stand-in: attributes in, ``save()`` stores them."""

    def __init__(self, ep, oid, data):
        self.__dict__.update(copy.deepcopy(data))
        self._ep, self.id = ep, oid

    def save(self):
        ep = self._ep
        if (ep.name, self.id) in ep.eng.fail:
            raise RuntimeError("netbox said no")
        ep.rows[self.id] = {k: copy.deepcopy(v) for k, v in self.__dict__.items()
                            if not k.startswith("_") and k != "id"}
        ep.eng.saves.append((ep.name, self.id))


class _Endpoint:
    """One pynetbox endpoint over ``rows`` (``{id: attributes}``)."""

    def __init__(self, eng, name, rows):
        self.eng, self.name, self.rows = eng, name, rows

    def get(self, oid):
        return _Obj(self, oid, self.rows[oid]) if oid in self.rows else None


class _IfaceEndpoint(_Endpoint):
    """Interfaces whose ``primary_mac_address`` shows the MAC record's
    current value, as NetBox does."""

    def get(self, oid):
        obj = super().get(oid)
        pm = getattr(obj, "primary_mac_address", None) if obj else None
        if isinstance(pm, dict) and pm.get("id") in self.eng.mac.rows:
            obj.primary_mac_address = dict(pm, mac_address=self.eng.mac.rows[pm["id"]]["mac_address"])
        return obj


class _Engine:
    """The engine surface the pass uses: two reads, IPs, interfaces, MACs."""

    def __init__(self, ips, interfaces=None, macs=None, vm_interfaces=None):
        self.fail, self.saves = set(), []
        self.ip = _Endpoint(self, "ip", ips)
        self.mac = _Endpoint(self, "mac", macs or {})
        self.iface = _IfaceEndpoint(self, "iface", interfaces or {})
        self.vm_iface = _Endpoint(self, "vm_iface", vm_interfaces or {})
        self.nb = SimpleNamespace(
            ipam=SimpleNamespace(ip_addresses=self.ip),
            dcim=SimpleNamespace(interfaces=self.iface, mac_addresses=self.mac),
            virtualization=SimpleNamespace(interfaces=self.vm_iface))

    def get_prefixes(self, **_filters):
        return {"status": "SUCCESS", "prefixes": [PREFIX]}

    def get_ip_addresses(self, **_filters):
        return {"status": "SUCCESS", "ip_addresses": [
            {"id": i, "address": r["address"], "status": "active", "vrf": "",
             "custom_fields": dict(r["custom_fields"])} for i, r in self.ip.rows.items()]}


def _engine_v4(cf=None):
    """NetBox 4.2+: IP 1 on interface 5, whose primary MAC record 9 is A."""
    return _Engine(
        {1: {"address": f"{IP}/24", "custom_fields": dict(cf or RESERVED_A),
             "assigned_object_type": "dcim.interface", "assigned_object_id": 5}},
        interfaces={5: {"mac_address": MAC_A.upper(),
                        "primary_mac_address": {"id": 9, "mac_address": MAC_A.upper()}}},
        macs={9: {"mac_address": MAC_A.upper()}})


def _engine_legacy():
    """NetBox before 4.2: the VM interface keeps a writable mac_address."""
    return _Engine(
        {1: {"address": f"{IP}/24", "custom_fields": dict(RESERVED_A),
             "assigned_object_type": "virtualization.vminterface", "assigned_object_id": 7}},
        vm_interfaces={7: {"mac_address": MAC_A}})


def _agent(ip=IP, mac=MAC_B, mode="static"):
    return {"ip": ip, "mac": mac, "mode": mode}


def _data(cands=None, *, watch=None, leases=(), reservations=None, auto=True,
          max_writes=25, sweep=()):
    """One tenant; Kea reserves the IP for A (the sync pushed it from NetBox)."""
    tenant = {"ref": "0", "filters": {"tenant": "acme"}, "kea": "k0",
              "agent_addresses": list(cands if cands is not None else [_agent()]),
              "sweep": list(sweep)}
    if watch is not None:
        tenant["replace_watch"] = watch
    res = reservations if reservations is not None else [{"ip-address": IP, "hw-address": MAC_A}]
    return {"tenants": [tenant], "max_writes": max_writes, "auto_replace": auto,
            "kea": {"k0": {"leases": list(leases), "subnets": [{**SUBNET, "reservations": res}]}}}


def _pass(eng, data):
    return sr.reserve_static_hosts(eng, data, {}, 1000.0)


def _button(eng, data, old=MAC_A, new=MAC_B):
    body = {"tenants": data["tenants"], "kea": data["kea"], "ip": IP,
            "old_mac": old, "new_mac": new}
    return sr.replace_reservation_mac(eng, body, {}, 1000.0)


# ── automatic replacement ───────────────────────────────────────────────────

def test_first_pass_only_waits_then_the_second_moves_ip_and_interface_mac():
    eng = _engine_v4()
    first = _pass(eng, _data())
    assert first["replaced"] == [] and eng.saves == []
    assert first["pending_replace"] == [{"ref": "0", "ip": IP, "old_mac": MAC_A,
                                         "new_mac": MAC_B, "first_seen_age": 0.0}]
    assert first["conflicts"][0]["replace"] == "waiting before replacing"
    # Not yet 30 minutes since B was first seen: still waiting.
    early = _pass(eng, _data(watch=[{**WATCH_DONE[0], "first_seen_age": 1799}]))
    assert early["replaced"] == [] and early["pending_replace"][0]["first_seen_age"] == 1799

    second = _pass(eng, _data(watch=WATCH_DONE))
    assert second["status"] == "SUCCESS" and second["pending_replace"] == []
    assert second["replaced"] == [{
        "ref": "0", "ip": IP, "ip_id": 1, "old_mac": MAC_A, "new_mac": MAC_B,
        "source": "agent", "before": RESERVED_A,
        "after": {"mac_address": MAC_B, "dhcp_reservation": True},
        "interface": {"object": "dcim.macaddress", "id": 9,
                      "before": {"mac_address": MAC_A}, "after": {"mac_address": MAC_B}}}]
    assert eng.ip.rows[1]["custom_fields"] == {"mac_address": MAC_B, "dhcp_reservation": True}
    assert eng.mac.rows[9]["mac_address"] == MAC_B


def test_legacy_netbox_interface_mac_field_is_moved():
    eng = _engine_legacy()
    res = _pass(eng, _data(watch=WATCH_DONE))
    assert res["replaced"][0]["interface"]["object"] == "virtualization.vminterface"
    assert eng.vm_iface.rows[7]["mac_address"] == MAC_B


def test_an_interface_with_another_mac_is_left_alone():
    eng = _engine_v4()
    eng.mac.rows[9]["mac_address"] = MAC_C
    eng.iface.rows[5]["primary_mac_address"]["mac_address"] = MAC_C
    res = _pass(eng, _data(watch=WATCH_DONE))
    assert res["replaced"][0]["interface"] is None and eng.mac.rows[9]["mac_address"] == MAC_C


def test_old_mac_seen_recently_or_unvouched_blocks_the_replacement():
    for unseen in (24 * 3600 - 1, None):
        eng = _engine_v4()
        res = _pass(eng, _data(watch=[{**WATCH_DONE[0], "old_unseen_age": unseen}]))
        assert res["replaced"] == [] and eng.saves == [] and res["pending_replace"]
    # A seen at another address in this very pass.
    eng = _engine_v4()
    res = _pass(eng, _data([_agent(), _agent("192.0.2.11", MAC_A, "unknown")], watch=WATCH_DONE))
    assert res["replaced"] == [] and res["conflicts"][0]["replace"] == "old mac still seen"


def test_old_mac_holding_a_lease_or_reserved_elsewhere_blocks_the_replacement():
    cases = {
        "old mac holds a lease": dict(leases=[{"ip-address": "192.0.2.150", "hw-address": MAC_A}]),
        "old mac reserved in kea at another address": dict(reservations=[
            {"ip-address": IP, "hw-address": MAC_A},
            {"ip-address": "192.0.2.12", "hw-address": MAC_A}]),
    }
    for reason, kw in cases.items():
        eng = _engine_v4()
        res = _pass(eng, _data(watch=WATCH_DONE, **kw))
        assert res["replaced"] == [] and eng.saves == [], reason
        assert res["conflicts"][0]["replace"] == reason
    # A reserved in NetBox at another address.
    eng = _engine_v4()
    eng.ip.rows[2] = {"address": "192.0.2.12/24", "custom_fields": dict(RESERVED_A)}
    res = _pass(eng, _data(watch=WATCH_DONE))
    assert res["conflicts"][0]["replace"] == "old mac reserved at another address"


def test_sources_that_disagree_never_replace():
    eng = _engine_v4()
    sweep = [{"ip": IP, "mac": MAC_C, "seen_age": 60, "arp_age": 60}]
    res = _pass(eng, _data(watch=WATCH_DONE, sweep=sweep))
    assert res["replaced"] == [] and res["pending_replace"] == [] and eng.saves == []
    # An agent that says dhcp vetoes as well.
    res = _pass(eng, _data([_agent(), _agent(mode="dhcp")], watch=WATCH_DONE))
    assert res["replaced"] == [] and eng.saves == []


def test_new_mac_must_pass_the_new_reservation_checks():
    cases = {
        "mac holds a lease": dict(leases=[{"ip-address": "192.0.2.150", "hw-address": MAC_B}]),
        "reserved in kea for another mac": dict(reservations=[{"ip-address": IP,
                                                               "hw-address": MAC_C}]),
        "mac reserved in kea at another address": dict(reservations=[
            {"ip-address": IP, "hw-address": MAC_A},
            {"ip-address": "192.0.2.13", "hw-address": MAC_B}]),
    }
    for reason, kw in cases.items():
        eng = _engine_v4()
        res = _pass(eng, _data(watch=WATCH_DONE, **kw))
        assert res["replaced"] == [] and eng.saves == [], reason
        assert res["conflicts"][0]["replace"] == reason


def test_auto_replace_off_keeps_the_conflict_and_does_not_wait():
    eng = _engine_v4()
    res = _pass(eng, _data(watch=WATCH_DONE, auto=False))
    assert res["replaced"] == [] and res["pending_replace"] == [] and eng.saves == []
    assert res["conflicts"] == [{"ref": "0", "ip": IP, "mac": MAC_B, "old_mac": MAC_A,
                                 "reason": "reserved for another mac",
                                 "replace": "automatic replacement is off"}]


def test_a_replacement_takes_two_write_slots():
    eng = _engine_v4()
    res = _pass(eng, _data(watch=WATCH_DONE, max_writes=1))
    assert res["replaced"] == [] and res["deferred"] == 1 and eng.saves == []


# ── rollback ────────────────────────────────────────────────────────────────

def test_interface_write_failure_rolls_back_the_ip_write():
    eng = _engine_v4()
    eng.fail = {("mac", 9)}
    res = _pass(eng, _data(watch=WATCH_DONE))
    assert res["status"] == "ERROR" and res["replaced"] == []
    assert [c["ip"] for c in res["rolled_back"]] == [IP]
    assert eng.ip.rows[1]["custom_fields"] == RESERVED_A
    assert eng.mac.rows[9]["mac_address"] == MAC_A.upper()


def test_a_later_failure_rolls_back_the_replacement_and_its_interface():
    eng = _engine_v4()
    eng.ip.rows[2] = {"address": "192.0.2.20/24", "custom_fields": {}}
    eng.fail = {("ip", 2)}
    res = _pass(eng, _data([_agent(), _agent("192.0.2.20", MAC_C)], watch=WATCH_DONE))
    assert res["status"] == "ERROR" and res["replaced"] == []
    assert [c["ip"] for c in res["rolled_back"]] == [IP]
    assert eng.ip.rows[1]["custom_fields"] == RESERVED_A
    assert eng.mac.rows[9]["mac_address"] == MAC_A


def test_hub_rollback_restores_ip_and_interface_and_keeps_later_edits():
    eng = _engine_v4()
    done = _pass(eng, _data(watch=WATCH_DONE))["replaced"]
    undo = sr.roll_back(eng, done)
    assert undo["status"] == "SUCCESS" and len(undo["rolled_back"]) == 1
    assert eng.ip.rows[1]["custom_fields"] == RESERVED_A and eng.mac.rows[9]["mac_address"] == MAC_A
    # Someone edits the interface MAC after the write: that edit stays.
    eng = _engine_v4()
    done = _pass(eng, _data(watch=WATCH_DONE))["replaced"]
    eng.mac.rows[9]["mac_address"] = MAC_C
    undo = sr.roll_back(eng, done)
    assert [c["reason"] for c in undo["rollback_kept"]] == [
        "interface: interface mac changed in netbox since it was read; ip: restored"]
    assert eng.mac.rows[9]["mac_address"] == MAC_C
    assert eng.ip.rows[1]["custom_fields"] == RESERVED_A


def test_an_interface_changed_in_between_is_reported_and_never_undone(monkeypatch):
    eng = _engine_v4()
    real = sr.mac_compare_and_set

    def raced(engine, holder, before, after):
        eng.mac.rows[9]["mac_address"] = MAC_C   # someone edits it first
        return real(engine, holder, before, after)
    monkeypatch.setattr(sr, "mac_compare_and_set", raced)
    done = _pass(eng, _data(watch=WATCH_DONE))["replaced"]
    monkeypatch.setattr(sr, "mac_compare_and_set", real)
    iface = done[0]["interface"]
    assert iface["id"] == 9 and "after" not in iface and "changed" in iface["left"]
    assert sr.roll_back(eng, done)["status"] == "SUCCESS"
    assert eng.mac.rows[9]["mac_address"] == MAC_C and eng.ip.rows[1]["custom_fields"] == RESERVED_A


# ── the "Use new MAC" button ────────────────────────────────────────────────

def test_button_replaces_at_once_without_the_waits():
    eng = _engine_v4()
    res = _button(eng, _data(auto=False))
    assert res["status"] == "SUCCESS" and [r["ip"] for r in res["replaced"]] == [IP]
    assert eng.mac.rows[9]["mac_address"] == MAC_B


def test_button_keeps_every_other_check():
    cases = {
        "cannot replace: old mac holds a lease":
            dict(leases=[{"ip-address": "192.0.2.150", "hw-address": MAC_A}]),
        "cannot replace: mac holds a lease":
            dict(leases=[{"ip-address": "192.0.2.150", "hw-address": MAC_B}]),
        "cannot replace: reserved in kea for another mac":
            dict(reservations=[{"ip-address": IP, "hw-address": MAC_C}]),
        "cannot replace: conflicting macs":
            dict(sweep=[{"ip": IP, "mac": MAC_C, "seen_age": 60, "arp_age": 60}]),
        "cannot replace: the new mac is not seen at this address now": dict(cands=[]),
    }
    for message, kw in cases.items():
        eng = _engine_v4()
        res = _button(eng, _data(**kw))
        assert res["status"] == "ERROR" and res["message"] == message, res
        assert res["replaced"] == [] and eng.saves == []
    # A stale button (NetBox or the host changed since the last pass).
    eng = _engine_v4()
    res = _button(eng, _data(), new=MAC_C)
    assert res["message"] == "cannot replace: macs changed since the last pass; refresh"
    assert eng.saves == []


def test_button_needs_an_existing_reservation():
    eng = _engine_v4(cf={"mac_address": MAC_A, "dhcp_reservation": False})
    res = _button(eng, _data())
    assert res["status"] == "ERROR" and res["message"] == "cannot replace: not a dhcp reservation"
    assert eng.saves == []


def test_a_repeated_button_request_succeeds_without_writing():
    eng = _engine_v4()
    assert _button(eng, _data())["status"] == "SUCCESS"
    saves = list(eng.saves)
    # The host now answers with B and Kea got B from the sync; the reply was lost.
    again = _button(eng, _data(reservations=[{"ip-address": IP, "hw-address": MAC_B}]))
    assert again["status"] == "SUCCESS" and again["already"] is True and eng.saves == saves
    # Reported like the first run, so the hub can still roll the IP back. The
    # interface already on B may not be this feature's doing: never undone.
    [row] = again["replaced"]
    assert (row["ref"], row["ip_id"], row["after"]["mac_address"]) == ("0", 1, MAC_B)
    assert row["interface"] == {"object": row["interface"]["object"], "id": 9,
                                "left": "interface already has the new mac"}
    sr.roll_back(eng, again["replaced"])
    assert eng.ip.rows[1]["custom_fields"] == RESERVED_A and eng.mac.rows[9]["mac_address"] == MAC_B


def test_a_button_whose_ip_moved_under_it_still_finishes_the_interface(monkeypatch):
    real_cas = sr.compare_and_set

    def moved_first(engine, ip_id, expect, new):
        # Another run wrote the IP between this run's read and its write.
        engine.ip.rows[ip_id]["custom_fields"] = dict(new)
        return real_cas(engine, ip_id, expect, new)
    monkeypatch.setattr(sr, "compare_and_set", moved_first)
    eng = _engine_v4()
    res = _button(eng, _data())
    assert res["status"] == "SUCCESS" and res["already"] is True
    assert eng.mac.rows[9]["mac_address"] == MAC_B
    assert res["replaced"][0]["interface"]["after"] == {"mac_address": MAC_B}


def test_a_repeat_finishes_an_interface_an_earlier_run_left_on_the_old_mac():
    eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
    res = _button(eng, _data())
    assert res["status"] == "SUCCESS" and res["already"] is True
    assert res["replaced"][0]["interface"]["after"] == {"mac_address": MAC_B}
    assert eng.mac.rows[9]["mac_address"] == MAC_B and eng.saves == [("mac", 9)]


def test_a_repeat_whose_sources_now_fail_a_check_writes_nothing():
    sweep = [{"ip": IP, "mac": MAC_C, "seen_age": 60, "arp_age": 60}]
    for kw in (dict(sweep=sweep), dict(cands=[_agent(mode="dhcp")]), dict(cands=[])):
        eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
        res = _button(eng, _data(**kw))
        assert res["status"] == "ERROR" and not res.get("already"), kw
        assert eng.saves == [] and eng.mac.rows[9]["mac_address"] == MAC_A.upper()


def test_a_repeat_keeps_the_old_and_new_mac_checks():
    # NetBox already reserves the IP for B, but the replacement would no
    # longer pass: the interface stays on A and nothing is reported done.
    cases = {
        "old mac still seen": dict(cands=[_agent(), _agent(ip="192.0.2.11", mac=MAC_A,
                                                            mode="unknown")]),
        "old mac holds a lease": dict(leases=[{"ip-address": "192.0.2.150", "hw-address": MAC_A}]),
        "mac holds a lease": dict(leases=[{"ip-address": "192.0.2.151", "hw-address": MAC_B}]),
        "reserved in kea for another mac": dict(reservations=[{"ip-address": IP,
                                                               "hw-address": MAC_C}]),
        "mac reserved in kea at another address": dict(reservations=[
            {"ip-address": IP, "hw-address": MAC_B},
            {"ip-address": "192.0.2.12", "hw-address": MAC_B}]),
    }
    # The same when Kea already reserves the IP for B (the sync pushed it).
    kea_b = [{"ip-address": IP, "hw-address": MAC_B}]
    cases.update({
        "kea b, mac holds a lease": dict(reservations=kea_b, leases=[
            {"ip-address": "192.0.2.151", "hw-address": MAC_B}]),
        "kea b, leased to another mac": dict(reservations=kea_b, leases=[
            {"ip-address": IP, "hw-address": MAC_C}]),
    })
    for reason, kw in cases.items():
        eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
        res = _button(eng, _data(**kw))
        assert res["status"] == "ERROR" and not res.get("already"), reason
        assert res["message"] == "cannot replace: " + reason.replace("kea b, ", "")
        assert eng.saves == [] and eng.mac.rows[9]["mac_address"] == MAC_A.upper(), reason
    # Kea already holding B there (the sync pushed it) is a done repeat.
    eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
    res = _button(eng, _data(reservations=[{"ip-address": IP, "hw-address": MAC_B}]))
    assert res["status"] == "SUCCESS" and res["already"] is True


def test_a_repeat_reports_the_interface_it_moved_without_reading_it_again(monkeypatch):
    real_holder = sr.interface_mac_holder

    def new_mac_read_fails(engine, ip_id, mac):
        if mac == MAC_B:
            raise RuntimeError("netbox unavailable")
        return real_holder(engine, ip_id, mac)
    monkeypatch.setattr(sr, "interface_mac_holder", new_mac_read_fails)
    eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
    res = _button(eng, _data())
    assert res["status"] == "SUCCESS" and eng.mac.rows[9]["mac_address"] == MAC_B
    assert res["replaced"][0]["interface"]["before"] == {"mac_address": MAC_A}


def test_a_repeat_never_claims_an_interface_another_writer_moved(monkeypatch):
    monkeypatch.setattr(sr, "mac_compare_and_set", lambda *a: ("already", ""))
    eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
    res = _button(eng, _data())
    iface = res["replaced"][0]["interface"]
    assert iface["left"] == "interface already has the new mac" and "after" not in iface


def test_a_repeat_keeps_an_interface_edited_after_its_failed_write(monkeypatch):
    outcomes = iter([("failed", "timed out"), ("changed", "interface mac changed")])
    monkeypatch.setattr(sr, "mac_compare_and_set", lambda *a: next(outcomes))
    eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
    res = _button(eng, _data())
    assert res["status"] == "ERROR" and not res.get("rollback_failed")
    assert res["rollback_kept"][0]["reason"] == "interface mac changed"


def test_a_repeat_reports_an_interface_that_changed_in_between(monkeypatch):
    monkeypatch.setattr(sr, "mac_compare_and_set",
                        lambda *a: ("changed", "interface mac changed in netbox since it was read"))
    eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
    res = _button(eng, _data())
    [row] = res["replaced"]
    assert row["interface"]["left"] == "interface mac changed in netbox since it was read"
    assert "before" not in row["interface"]


def test_a_repeat_whose_interface_write_failed_after_saving_puts_it_back(monkeypatch):
    real_mac = sr.mac_compare_and_set

    def saved_then_failed(engine, holder, before, after):
        if after == MAC_B:
            eng.mac.rows[9]["mac_address"] = MAC_B
            return "failed", "timed out after saving"
        return real_mac(engine, holder, before, after)
    monkeypatch.setattr(sr, "mac_compare_and_set", saved_then_failed)
    eng = _engine_v4(cf={"mac_address": MAC_B, "dhcp_reservation": True})
    res = _button(eng, _data())
    assert res["status"] == "ERROR" and not res.get("rollback_failed")
    assert eng.mac.rows[9]["mac_address"] == MAC_A


def test_an_interface_write_that_failed_after_saving_is_rolled_back(monkeypatch):
    eng = _engine_v4()
    real_mac = sr.mac_compare_and_set

    def saved_then_failed(engine, holder, before, after):
        if after == MAC_B:
            eng.mac.rows[9]["mac_address"] = MAC_B
            return "failed", "timed out after saving"
        return real_mac(engine, holder, before, after)
    monkeypatch.setattr(sr, "mac_compare_and_set", saved_then_failed)
    res = _pass(eng, _data(watch=WATCH_DONE))
    assert res["status"] == "ERROR" and [c["ip"] for c in res["rolled_back"]] == [IP]
    assert eng.mac.rows[9]["mac_address"] == MAC_A and eng.ip.rows[1]["custom_fields"] == RESERVED_A


def test_a_failed_write_and_rollback_is_never_reported_as_already_done(monkeypatch):
    eng = _engine_v4()
    calls = []
    real_mac = sr.mac_compare_and_set

    def interface_fails_once(*a):
        calls.append(a)
        return ("failed", "netbox said no") if len(calls) == 1 else real_mac(*a)
    monkeypatch.setattr(sr, "mac_compare_and_set", interface_fails_once)
    real = sr.compare_and_set

    def ip_rollback_fails(engine, ip_id, before, after):
        if after == RESERVED_A:
            return "failed", "netbox said no"
        return real(engine, ip_id, before, after)
    monkeypatch.setattr(sr, "compare_and_set", ip_rollback_fails)
    res = _button(eng, _data())
    assert eng.ip.rows[1]["custom_fields"]["mac_address"] == MAC_B
    assert res["status"] == "ERROR" and not res.get("already") and res["rollback_failed"]


def test_button_writes_nothing_but_its_one_ip():
    eng = _engine_v4()
    eng.ip.rows[2] = {"address": "192.0.2.20/24", "custom_fields": {}}
    res = _button(eng, _data([_agent(), _agent("192.0.2.20", MAC_C)]))
    assert [r["ip"] for r in res["replaced"]] == [IP] and res["changes"] == []
    assert eng.ip.rows[2]["custom_fields"] == {}


def test_button_command_runs_through_the_spoke_and_drops_the_list_cache():
    eng = _engine_v4()
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.engine, sp._picklist_cache = eng, {}
    sp._list_cache = {"GET_IPS|x": {"ts": 0, "value": {}}}
    data = _data()
    res = asyncio.run(sp.handle_command("NETBOX_REPLACE_RESERVATION_MAC", {
        "tenants": data["tenants"], "kea": data["kea"], "ip": IP,
        "old_mac": MAC_A, "new_mac": MAC_B}))
    assert res["status"] == "SUCCESS" and sp._list_cache == {}
    assert eng.ip.rows[1]["custom_fields"]["mac_address"] == MAC_B


def test_a_kept_rollback_still_drops_the_list_cache(monkeypatch):
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.engine, sp._picklist_cache = _engine_v4(), {}
    sp._list_cache = {"GET_IPS|x": {"ts": 0, "value": {}}}
    monkeypatch.setattr(sr, "reserve_static_hosts", lambda *a, **k: {
        "status": "ERROR", "changes": [], "replaced": [], "rolled_back": [],
        "rollback_kept": [{"ip": IP, "reason": "changed"}], "rollback_failed": []})
    asyncio.run(sp.handle_command("NETBOX_RESERVE_STATIC_HOSTS", _data()))
    assert sp._list_cache == {}

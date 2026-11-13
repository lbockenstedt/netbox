"""The static-reservation planner (``plan_static_reservations``), case by case.

These moved here from lm ``core/tests/test_static_ip_reservations.py`` with
the planner itself (lm#1887). All data is RFC 1918 example data.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_static_reservations import plan_static_reservations  # noqa: E402

MAC_A = "02:00:00:00:00:0a"
MAC_B = "02:00:00:00:00:0b"

# The Kea subnet as config-get returns it: pool .100-.200, no host reservations.
KEA = [{"subnet": "10.20.0.0/24", "pools": [{"pool": "10.20.0.100 - 10.20.0.200"}],
        "reservations": []}]


def _plan(pfx, ips, leases, cands, kea_subnets=KEA):
    """plan_static_reservations on NETBOX_GET_*-shaped replies, default Kea subnet."""
    return plan_static_reservations(pfx["prefixes"], ips["ip_addresses"], leases, cands,
                                    kea_subnets)


def _pfx():
    """One DHCP-enabled scope, one container and one non-DHCP prefix."""
    return {"prefixes": [
        {"prefix": "10.20.0.0/24", "status": "active", "vrf": "lab",
         "custom_fields": {"dhcp_enabled": True}},
        {"prefix": "10.0.0.0/8", "status": "container",
         "custom_fields": {"dhcp_enabled": True}},
        {"prefix": "10.30.0.0/24", "status": "active", "custom_fields": {}},
    ]}

def _ip(address, ip_id, **cf):
    """A NETBOX_GET_IPS row."""
    return {"id": ip_id, "address": address, "status": "active", "vrf": "lab",
            "custom_fields": cf}

def _ips(*rows):
    """Wrap rows as a NETBOX_GET_IPS reply."""
    return {"ip_addresses": list(rows)}

def _static(ip, mac=MAC_A, source="agent"):
    """A candidate reported as static."""
    return {"ip": ip, "mac": mac, "mode": "static", "source": source}

def _reasons(plan, key):
    """``{ip: reason}`` for one plan bucket."""
    return {x["ip"]: x["reason"] for x in plan[key]}

def test_static_host_without_lease_is_reserved():
    plan = _plan(_pfx(), _ips(_ip("10.20.0.50/24", 7)), [],
                                    [_static("10.20.0.50")])
    assert plan["reserve"] == [{"ip_id": 7, "ip": "10.20.0.50", "mac": MAC_A,
                                "source": "agent",
                                "before": {"mac_address": None, "dhcp_reservation": None}}]
    assert not plan["conflicts"]

def test_dynamic_lease_is_not_reserved():
    leases = [{"ip-address": "10.20.0.50", "hw-address": MAC_A, "state": 0}]
    plan = _plan(_pfx(), _ips(_ip("10.20.0.50/24", 7)), leases,
                                    [_static("10.20.0.50", source="sweep")])
    assert plan["reserve"] == []
    assert _reasons(plan, "skipped") == {"10.20.0.50": "dynamic lease"}

def test_mac_holding_a_lease_elsewhere_is_not_reserved():
    leases = [{"ip-address": "10.20.0.60", "hw-address": MAC_A}]
    plan = _plan(_pfx(), _ips(_ip("10.20.0.50/24", 7)), leases,
                                    [_static("10.20.0.50")])
    assert plan["reserve"] == []
    assert _reasons(plan, "skipped") == {"10.20.0.50": "mac holds a lease"}

def test_agent_dhcp_mode_vetoes_sweep():
    cands = [_static("10.20.0.50", source="sweep"),
             {"ip": "10.20.0.50", "mac": MAC_A, "mode": "dhcp", "source": "agent"}]
    plan = _plan(_pfx(), _ips(_ip("10.20.0.50/24", 7)), [], cands)
    assert plan["reserve"] == []
    assert _reasons(plan, "skipped") == {"10.20.0.50": "agent reports dhcp"}

def test_existing_reservation_for_other_mac_is_a_conflict_not_overwritten():
    row = _ip("10.20.0.50/24", 7, mac_address=MAC_B, dhcp_reservation=True)
    plan = _plan(_pfx(), _ips(row), [], [_static("10.20.0.50")])
    assert plan["reserve"] == []
    assert _reasons(plan, "conflicts") == {"10.20.0.50": "reserved for another mac"}

def test_lease_for_other_mac_and_stale_netbox_mac_are_conflicts():
    rows = _ips(_ip("10.20.0.50/24", 7), _ip("10.20.0.51/24", 8, mac_address=MAC_B))
    leases = [{"ip-address": "10.20.0.50", "hw-address": MAC_B}]
    cands = [_static("10.20.0.50"), _static("10.20.0.51", mac="02:00:00:00:00:0c")]
    plan = _plan(_pfx(), rows, leases, cands)
    assert plan["reserve"] == []
    assert _reasons(plan, "conflicts") == {"10.20.0.50": "leased to another mac",
                                           "10.20.0.51": "netbox mac differs"}

def test_already_reserved_same_mac_is_idempotent():
    row = _ip("10.20.0.50/24", 7, mac_address=MAC_A.upper(), dhcp_reservation=True)
    plan = _plan(_pfx(), _ips(row), [], [_static("10.20.0.50")])
    assert plan["reserve"] == [] and not plan["conflicts"]
    assert _reasons(plan, "skipped") == {"10.20.0.50": "already reserved"}

def test_out_of_scope_unknown_mac_and_missing_ip_are_skipped():
    rows = _ips(_ip("10.30.0.5/24", 1), _ip("10.20.0.51/24", 2))
    cands = [_static("10.30.0.5", mac="02:00:00:00:00:01"),   # prefix not DHCP-enabled
             _static("10.40.0.5", mac="02:00:00:00:00:02"),   # only under the container
             _static("10.20.0.51", mac="incomplete"),         # MAC unknown
             _static("10.20.0.52", mac="02:00:00:00:00:03")]  # no NetBox IP object
    plan = _plan(_pfx(), rows, [], cands)
    assert plan["reserve"] == []
    assert _reasons(plan, "skipped") == {
        "10.30.0.5": "outside any dhcp scope", "10.40.0.5": "outside any dhcp scope",
        "10.20.0.51": "malformed mac", "10.20.0.52": "no netbox ip"}

def test_mac_seen_at_two_addresses_is_skipped():
    rows = _ips(_ip("10.20.0.50/24", 7), _ip("10.20.0.51/24", 8))
    plan = _plan(_pfx(), rows, [],
                                    [_static("10.20.0.50"), _static("10.20.0.51")])
    assert plan["reserve"] == []
    assert set(_reasons(plan, "skipped").values()) == {"mac seen at several addresses"}

def test_a_secondary_address_does_not_block_the_hosts_static_primary():
    """An agent reports a NIC's secondary/VIP as unknown with the same MAC."""
    rows = _ips(_ip("10.20.0.50/24", 7), _ip("10.20.0.99/24", 8))
    cands = [_static("10.20.0.50"),
             {"ip": "10.20.0.99", "mac": MAC_A, "mode": "unknown", "source": "agent"}]
    plan = _plan(_pfx(), rows, [], cands)
    assert [r["ip"] for r in plan["reserve"]] == ["10.20.0.50"]
    assert _reasons(plan, "skipped") == {"10.20.0.99": "not known to be static"}

def _vrf_pfx(*prefixes):
    """DHCP-enabled prefixes given as (cidr, vrf)."""
    return {"prefixes": [{"prefix": p, "status": "active", "vrf": v,
                          "custom_fields": {"dhcp_enabled": True}} for p, v in prefixes]}

def _vrf_ip(vrf):
    """10.20.0.50 as a NetBox IP row in ``vrf``."""
    return _ips({"id": 7, "address": "10.20.0.50/24", "status": "active",
                 "vrf": vrf, "custom_fields": {}})

def test_vrf_must_match_exactly():
    """The scope is the DHCP prefix in exactly the IP's VRF; overlapping space
    in another VRF, or one side global, never counts."""
    # Overlap: a more specific prefix in another VRF must not hide the IP's own scope.
    pfx = _vrf_pfx(("10.20.0.0/24", "red"), ("10.20.0.0/25", "blue"))
    plan = _plan(pfx, _vrf_ip("red"), [], [_static("10.20.0.50")])
    assert [r["ip"] for r in plan["reserve"]] == ["10.20.0.50"]
    # Nested VRF objects normalise (dict → id) on both sides.
    pfx = _vrf_pfx(("10.20.0.0/24", {"id": 3, "name": "red"}))
    plan = _plan(pfx, _vrf_ip({"id": 3, "name": "red"}), [],
                                    [_static("10.20.0.50")])
    assert [r["ip"] for r in plan["reserve"]] == ["10.20.0.50"]
    # One-sided empty: VRF prefix with a global IP, global prefix with a VRF IP.
    for pfx_vrf, ip_vrf in (("red", None), ("", "red")):
        plan = _plan(_vrf_pfx(("10.20.0.0/24", pfx_vrf)),
                                        _vrf_ip(ip_vrf), [], [_static("10.20.0.50")])
        assert plan["reserve"] == []
        assert _reasons(plan, "skipped") == {"10.20.0.50": "vrf differs from scope"}

def test_malformed_or_conflicting_mac_vetoes_the_candidate():
    """A bad MAC from one source is never dropped in favour of the other."""
    rows = _ips(_ip("10.20.0.50/24", 7), _ip("10.20.0.51/24", 8))
    cands = [_static("10.20.0.50", mac="gg:gg:gg:gg:gg:gg", source="sweep"),
             _static("10.20.0.50"),
             _static("10.20.0.51", mac=MAC_B), _static("10.20.0.51", mac="02:00:00:00:00:0c",
                                                       source="sweep")]
    plan = _plan(_pfx(), rows, [], cands)
    assert plan["reserve"] == []
    assert _reasons(plan, "skipped") == {"10.20.0.50": "malformed mac",
                                         "10.20.0.51": "conflicting macs"}

def test_sweep_only_host_needs_an_ip_outside_every_kea_pool():
    """A sweep-seen host inside a pool may be an offline DHCP client; outside
    the pools it cannot be one. Agent-reported static needs no pool check."""
    subnets = [{"subnet": "10.20.0.0/24", "pools": [{"pool": "10.20.0.10 - 10.20.0.99"}],
                "reservations": []}]
    rows = _ips(_ip("10.20.0.50/24", 7), _ip("10.20.0.150/24", 8), _ip("10.20.0.60/24", 9))
    cands = [_static("10.20.0.50", source="sweep"),
             _static("10.20.0.150", mac=MAC_B, source="sweep"),
             _static("10.20.0.60", mac="02:00:00:00:00:0c")]
    plan = _plan(_pfx(), rows, [], cands, subnets)
    assert sorted(r["ip"] for r in plan["reserve"]) == ["10.20.0.150", "10.20.0.60"]
    assert _reasons(plan, "skipped") == {"10.20.0.50": "inside a dhcp pool"}
    # Pools unknown: sweep-only candidates are skipped.
    bad_pool = [dict(subnets[0], pools=[{"pool": "not a range"}])]
    plan = _plan(_pfx(), rows, [], cands[1:2], bad_pool)
    assert _reasons(plan, "skipped") == {"10.20.0.150": "kea pools unknown"}

def test_a_structured_netbox_dhcp_status_is_not_reserved():
    row = dict(_ip("10.20.0.50/24", 7), status={"value": "dhcp", "label": "DHCP"})
    plan = _plan(_pfx(), _ips(row), [], [_static("10.20.0.50")])
    assert plan["reserve"] == []
    assert _reasons(plan, "skipped") == {"10.20.0.50": "netbox status is dhcp"}

def _kea_with(*reservations, **sub):
    """The default Kea subnet holding ``reservations`` (Kea's own shape)."""
    return [dict(KEA[0], reservations=[{"ip-address": ip, "hw-address": mac}
                                       for ip, mac in reservations], **sub)]

def test_kea_host_reservations_veto_and_report_conflicts():
    """A reservation held directly in Kea counts like a NetBox one: the IP
    reserved for another MAC, or the MAC reserved at another IP, is a
    conflict; the same MAC at the same IP is already done."""
    rows = _ips(_ip("10.20.0.50/24", 7))
    cases = {
        ("10.20.0.50", MAC_B): ("conflicts", "reserved in kea for another mac"),
        ("10.20.0.60", MAC_A.upper()): ("conflicts", "mac reserved in kea at another address"),
        ("10.20.0.50", MAC_A.upper()): ("skipped", "already reserved"),
    }
    for res, (bucket, reason) in cases.items():
        plan = _plan(_pfx(), rows, [], [_static("10.20.0.50")], _kea_with(res))
        assert plan["reserve"] == []
        assert _reasons(plan, bucket) == {"10.20.0.50": reason}
    # A reservation by client-id (no MAC) still takes the IP.
    kea = [dict(KEA[0], reservations=[{"ip-address": "10.20.0.50", "client-id": "01:02"}])]
    plan = _plan(_pfx(), rows, [], [_static("10.20.0.50")], kea)
    assert _reasons(plan, "conflicts") == {"10.20.0.50": "reserved in kea for another mac"}

def test_unknown_kea_reservations_reserve_nothing():
    """Fail closed: no subnet read, a reservations value that is not a list,
    or an IP outside every Kea subnet → nothing is written."""
    rows = _ips(_ip("10.20.0.50/24", 7))
    for kea, reason in ((None, "kea reservations unknown"),
                        ([dict(KEA[0], reservations="?")], "kea reservations unknown"),
                        ([dict(KEA[0], subnet="10.99.0.0/24")], "not in a kea subnet")):
        plan = _plan(_pfx(), rows, [], [_static("10.20.0.50")], kea)
        assert plan["reserve"] == []
        assert _reasons(plan, "skipped") == {"10.20.0.50": reason}

def test_a_subnet_without_a_reservations_key_has_none():
    """KeaManager.build omits ``reservations`` for a subnet with none, so a
    missing key is an empty list, not unknown: the host is reserved, and a
    reservation in another subnet still counts."""
    rows = _ips(_ip("10.20.0.50/24", 7))
    bare = {k: v for k, v in KEA[0].items() if k != "reservations"}
    plan = _plan(_pfx(), rows, [], [_static("10.20.0.50")], [bare])
    assert [r["ip"] for r in plan["reserve"]] == ["10.20.0.50"]
    other = {"subnet": "10.30.0.0/24", "pools": [],
             "reservations": [{"ip-address": "10.30.0.5", "hw-address": MAC_A}]}
    plan = _plan(_pfx(), rows, [], [_static("10.20.0.50")], [bare, other])
    assert _reasons(plan, "conflicts") == {"10.20.0.50": "mac reserved in kea at another address"}

def test_a_declined_lease_without_a_mac_is_its_own_conflict():
    """Kea keeps a declined address as a lease with an empty hw-address: still
    no write, but not reported as another client's lease."""
    rows = _ips(_ip("10.20.0.50/24", 7))
    declined = [{"ip-address": "10.20.0.50", "hw-address": "", "state": 1}]
    plan = _plan(_pfx(), rows, declined, [_static("10.20.0.50")])
    assert plan["reserve"] == []
    assert _reasons(plan, "conflicts") == {"10.20.0.50": "address declined in kea"}

"""Unit tests for NetBox Issue #319: nw-poll reuse-IP PATCH storm.

Tests address classification (rejecting loopback, link-local, multicast,
network/broadcast, while keeping private/ULA eligible), _ref_id reference
resolution across attribute/dict/int/null shapes, primary-IP conflict
detection (zero PATCHes on conflict, logs once at INFO, zero second-cycle
writes), external sync caller safety on None, and interface journal entry
suppression.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_changelog import (  # noqa: E402
    ChangelogMixin,
    _is_unassignable_or_non_host_ip,
    _ref_id,
)
from netbox_engine import NetboxEngine  # noqa: E402


# ── 1. Address Classification ────────────────────────────────────────────────

def test_unassignable_addresses_rejected():
    unassignable = [
        "127.0.0.1",
        "127.0.0.1/32",
        "127.5.6.7/8",
        "::1",
        "::1/128",
        "0.0.0.0",
        "0.0.0.0/32",
        "0.1.2.3/8",
        "::",
        "::/128",
        "169.254.1.1",
        "169.254.1.1/16",
        "fe80::1",
        "fe80::1/10",
        "fe80::abcd/64",
        "224.0.0.1",
        "224.0.0.1/32",
        "ff02::1",
        "ff02::1/128",
        "192.168.1.0/24",    # network address
        "192.168.1.255/24",  # directed broadcast
        "10.0.0.0/8",        # network address
        "172.16.0.0/12",     # network address
        "not-an-ip",
        "",
    ]
    for addr in unassignable:
        assert _is_unassignable_or_non_host_ip(addr) is True, f"Expected {addr} to be unassignable"


def test_eligible_host_addresses_accepted():
    eligible = [
        "10.0.0.1",
        "10.0.0.1/24",
        "172.16.1.5/24",
        "172.31.42.172/24",
        "192.168.1.1",
        "192.168.1.1/24",
        "192.168.1.50/24",
        "192.168.1.1/31",    # RFC 3021 /31 point-to-point host
        "192.168.1.0/31",    # RFC 3021 /31 point-to-point host
        "fd00::1",
        "fd00::abcd/64",
        "fc00::1/7",
    ]
    for addr in eligible:
        assert _is_unassignable_or_non_host_ip(addr) is False, f"Expected {addr} to be eligible"


# ── 2. Reference ID Resolution (_ref_id) ────────────────────────────────────

def test_ref_id_resolution():
    assert _ref_id(None) is None
    assert _ref_id({}) is None
    assert _ref_id(0) is None
    assert _ref_id(-5) is None
    assert _ref_id("not-a-number") is None
    assert _ref_id(500) == 500
    assert _ref_id("500") == 500
    assert _ref_id({"id": 500}) == 500
    assert _ref_id({"id": "500"}) == 500
    assert _ref_id({"name": "sw1"}) is None
    assert _ref_id(SimpleNamespace(id=500)) == 500
    assert _ref_id(SimpleNamespace(id="500")) == 500
    assert _ref_id(SimpleNamespace(name="sw1")) is None


# ── 3. Interface Journal Suppression ─────────────────────────────────────────

def test_journal_suppresses_interfaces():
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()

    # Device and IPAddress should write journal entries
    eng._journal("dcim.device", 100, "sync", note="device test")
    assert eng.nb.extras.journal_entries.create.call_count == 1

    eng._journal("ipam.ipaddress", 200, "sync", note="ip test")
    assert eng.nb.extras.journal_entries.create.call_count == 2

    # Interface types must NOT write journal entries (NetBox rejects them with 400)
    eng._journal("virtualization.vminterface", 300, "sync", note="vminterface test")
    eng._journal("dcim.interface", 400, "sync", note="interface test")
    assert eng.nb.extras.journal_entries.create.call_count == 2  # still 2!


# ── 4. Primary IP Conflict & Counting Fake ──────────────────────────────────

class _CountingNetbox:
    """Mock NetBox client that counts API operations."""
    def __init__(self):
        self.patch_calls = []
        self.get_calls = []
        self.ipam = SimpleNamespace(
            ip_addresses=SimpleNamespace(
                get=self._ip_get,
                update=self._ip_update,
                create=MagicMock(return_value=SimpleNamespace(id=888)),
                filter=MagicMock(return_value=[]),
            )
        )
        self.dcim = SimpleNamespace(
            devices=SimpleNamespace(get=self._dev_get, create=MagicMock()),
            interfaces=SimpleNamespace(get=self._iface_get, create=MagicMock(), filter=MagicMock(return_value=[])),
            device_types=SimpleNamespace(get=MagicMock(return_value=SimpleNamespace(id=1))),
            device_roles=SimpleNamespace(get=MagicMock(return_value=SimpleNamespace(id=2))),
            sites=SimpleNamespace(get=MagicMock(return_value=SimpleNamespace(id=3))),
        )
        self.extras = SimpleNamespace(
            journal_entries=SimpleNamespace(create=MagicMock())
        )

        # Existing IP 500 is assigned to interface 11 on Device 70 (primary_ip4=500)
        self.ip_record = SimpleNamespace(
            id=500,
            address="172.31.42.172/24",
            assigned_object_type="dcim.interface",
            assigned_object_id=11,
            assigned_object={"id": 11, "device": {"id": 70, "name": "DeviceA"}},
            tenant=None,
            dns_name="",
            custom_fields={},
        )
        self.device_70 = SimpleNamespace(
            id=70,
            name="DeviceA",
            primary_ip4=500,
            primary_ip6=None,
            save=MagicMock(),
        )

    def _ip_get(self, address=None, **kw):
        self.get_calls.append(("ipam.ip_addresses.get", address))
        if address in ("172.31.42.172/24", "172.31.42.172"):
            return self.ip_record
        return None

    def _ip_update(self, rows):
        self.patch_calls.append(("ipam.ip_addresses.update", rows))
        return rows

    def _dev_get(self, *args, **kw):
        dev_id = args[0] if args else kw.get("id")
        self.get_calls.append(("dcim.devices.get", dev_id))
        if dev_id == 70:
            return self.device_70
        return None

    def _iface_get(self, *args, **kw):
        iface_id = args[0] if args else kw.get("id")
        self.get_calls.append(("dcim.interfaces.get", iface_id))
        if iface_id == 11:
            return SimpleNamespace(id=11, device={"id": 70, "name": "DeviceA"})
        return None


def test_primary_conflict_never_patches_and_second_cycle_is_zero_reads_zero_writes(caplog):
    import logging
    fake_nb = _CountingNetbox()
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = fake_nb
    eng._ensure_custom_fields = MagicMock()
    eng._journal = MagicMock()
    eng._stamp_last_seen = MagicMock()

    # Target: DeviceB (id=80), interface 22
    dev_b = SimpleNamespace(id=80, name="DeviceB", primary_ip4=None, save=MagicMock())
    target_iface_id = 22

    # Cycle 1: Attempt to reassign primary IP 172.31.42.172/24 to DeviceB interface 22
    with caplog.at_level(logging.INFO):
        res1 = eng._reuse_or_create_ip(
            "172.31.42.172/24",
            {"address": "172.31.42.172/24", "assigned_object_type": "dcim.interface", "assigned_object_id": target_iface_id},
            "172.31.42.172",
            target_iface_id,
            source="nw-poll",
            iface_type="dcim.interface",
        )

    assert res1 is None, "Expected primary conflict to return None on reuse"
    assert len(fake_nb.patch_calls) == 0, "Expected zero PATCH calls on primary conflict"
    eng._stamp_last_seen.assert_not_called()

    # Verify INFO log was emitted once
    conflict_logs = [r for r in caplog.records if "designated as primary IP" in r.message]
    assert len(conflict_logs) == 1
    assert "DeviceA" in conflict_logs[0].message

    # Cycle 2: Repeat poll cycle
    gets_before_c2 = len(fake_nb.get_calls)
    with caplog.at_level(logging.INFO):
        res2 = eng._reuse_or_create_ip(
            "172.31.42.172/24",
            {"address": "172.31.42.172/24", "assigned_object_type": "dcim.interface", "assigned_object_id": target_iface_id},
            "172.31.42.172",
            target_iface_id,
            source="nw-poll",
            iface_type="dcim.interface",
        )

    assert res2 is None
    assert len(fake_nb.patch_calls) == 0, "Still zero PATCH calls on cycle 2"
    # Secondary device lookups must be avoided via memo (rule 4.8)
    dev_gets_c2 = [c for c in fake_nb.get_calls[gets_before_c2:] if c[0] == "dcim.devices.get"]
    assert len(dev_gets_c2) == 0, "Expected zero device GET lookups on cycle 2 due to memoization"

    # Verify log fired only once (not repeated on cycle 2)
    conflict_logs_c2 = [r for r in caplog.records if "designated as primary IP" in r.message]
    assert len(conflict_logs_c2) == 1


def test_loopback_address_skipped_without_netbox_lookup():
    fake_nb = _CountingNetbox()
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = fake_nb
    eng._ensure_custom_fields = MagicMock()

    res = eng._reuse_or_create_ip(
        "127.0.0.1/32",
        {"address": "127.0.0.1/32", "assigned_object_type": "dcim.interface", "assigned_object_id": 99},
        "127.0.0.1",
        99,
        source="nw-poll",
        iface_type="dcim.interface",
    )
    assert res is None
    assert len(fake_nb.get_calls) == 0
    assert len(fake_nb.patch_calls) == 0


def test_sync_nw_device_with_loopback_and_conflict():
    fake_nb = _CountingNetbox()
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = fake_nb
    eng._ensure_custom_fields = MagicMock()
    eng._journal = MagicMock()
    eng._stamp_last_seen = MagicMock()
    eng._api_get_all = MagicMock(return_value=[])
    eng._api_get = MagicMock(return_value={"results": []})
    eng._mask_for_ip = MagicMock(return_value="24")

    dev_obj = SimpleNamespace(id=80, name="sw2", primary_ip4=None, save=MagicMock())
    fake_nb.dcim.devices.create = MagicMock(return_value=dev_obj)

    iface_vlan1 = SimpleNamespace(id=21, name="vlan1", save=MagicMock())
    iface_lo0 = SimpleNamespace(id=22, name="lo0", save=MagicMock())
    iface_conflict = SimpleNamespace(id=23, name="vlan2", save=MagicMock())
    fake_nb.dcim.interfaces.create = MagicMock(side_effect=[iface_vlan1, iface_lo0, iface_conflict])

    interfaces = [
        {"name": "vlan1", "ip": "10.0.0.2", "mac": "00:11:22:33:44:55", "status": "up"},
        {"name": "lo0", "ip": "127.0.0.1", "status": "up"},
        {"name": "vlan2", "ip": "172.31.42.172", "status": "up"},
    ]
    defaults = {"device_type": "switch", "role": "switch", "site": "site1"}
    device = {"id": "nw-sw2", "name": "sw2", "address": "10.0.0.2", "object_type": "aos_switch"}

    res = eng.sync_nw_device(device=device, interfaces=interfaces, tenant_slug="", defaults=defaults)

    assert res["status"] == "SUCCESS"
    assert res["errors"] == 0
    assert res["pushed"] == 3
    # vlan1 mgmt IP was set as primary_ip4
    assert dev_obj.primary_ip4 == 888  # created IP
    # loopback and conflict did NOT patch fake_nb
    assert len(fake_nb.patch_calls) == 0

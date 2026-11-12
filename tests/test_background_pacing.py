"""Background feeds can't overrun NetBox, whoever sends them and however many."""
import asyncio
import os
import sys
import threading
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import netbox_engine  # noqa: E402
import netbox_spoke as spoke_mod  # noqa: E402

REQUEST_S = 0.05


def _slow_netbox(monkeypatch):
    """Every NetBox request takes REQUEST_S; returns the overlap counters."""
    seen = {"now": 0, "max": 0}
    lock = threading.Lock()

    def send(self, request, **kw):
        with lock:
            seen["now"] += 1
            seen["max"] = max(seen["max"], seen["now"])
        time.sleep(REQUEST_S)
        with lock:
            seen["now"] -= 1
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(netbox_engine, "_background_next", 0.0)
    return seen


def test_background_feeds_together_use_about_half_of_netbox(monkeypatch):
    seen = _slow_netbox(monkeypatch)
    adapter = netbox_engine._DefaultTimeoutHTTPAdapter(30)

    def feed():
        for _ in range(3):
            adapter.send(None)
    threads = [threading.Thread(target=netbox_engine.run_as_background, args=(feed,))
               for _ in range(3)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    took = time.monotonic() - start
    # 9 requests one at a time, each followed by an equal rest.
    assert seen["max"] == 1
    assert took >= (9 * 2 - 1) * REQUEST_S * 0.9


def test_user_requests_are_not_paced(monkeypatch):
    _slow_netbox(monkeypatch)
    adapter = netbox_engine._DefaultTimeoutHTTPAdapter(30)
    netbox_engine.run_as_background(adapter.send, None)  # a feed just used NetBox
    start = time.monotonic()
    adapter.send(None)
    assert time.monotonic() - start < REQUEST_S * 1.8


def test_only_background_commands_run_paced():
    """A discovery sync is paced; a user's picklist read is not."""
    def paced(*a, **kw):
        return {"status": "SUCCESS", "paced": getattr(netbox_engine._background, "on", False)}

    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp._picklist_cache = {}
    sp.engine = type("Engine", (), {"sync_devices": staticmethod(paced),
                                    "get_tenants": staticmethod(paced)})()

    async def run():
        sync = await sp._handle_command("NETBOX_SYNC_DEVICES", {"devices": []})
        read = await sp._handle_command("NETBOX_GET_TENANTS", {})
        assert (sync["paced"], read["paced"]) == (True, False)
    asyncio.run(run())


def test_store_and_ack_nw_device_push_is_not_queued_as_background():
    # SYNC_NW_DEVICE only stores the device and acks; in the capped background
    # lane it waited behind slow syncs until the hub gave up and it was dropped.
    lane = spoke_mod.NetboxSpoke.BACKGROUND_COMMANDS
    assert "NETBOX_SYNC_NW_DEVICE" not in lane
    assert "NETBOX_SYNC_DEVICES" in lane
    # Its immediate (write-now) path is still paced against NetBox.
    assert "NETBOX_SYNC_NW_DEVICE" in spoke_mod.NetboxSpoke._BACKGROUND_MUTATIONS


def test_two_reads_run_side_by_side_but_never_more_than_netbox_workers(monkeypatch):
    # A paged IP/prefix walk held the only slot, so every other read queued
    # behind it and timed out at the hub while NetBox sat idle.
    seen = {"now": 0, "max": 0}
    lock = threading.Lock()

    class Ok:
        ok = True

        def json(self):
            return {"results": []}

    def get(url, params=None, timeout=None):
        with lock:
            seen["now"] += 1
            seen["max"] = max(seen["max"], seen["now"])
        time.sleep(REQUEST_S)
        with lock:
            seen["now"] -= 1
        return Ok()

    eng = netbox_engine.NetboxEngine.__new__(netbox_engine.NetboxEngine)
    eng.url = "https://netbox.example"
    eng.nb = type("Nb", (), {"http_session": type("S", (), {"get": staticmethod(get)})()})()
    threads = [threading.Thread(target=eng._api_get, args=("/api/ipam/ip-addresses/",))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["max"] == 2

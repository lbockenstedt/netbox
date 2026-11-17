"""Standalone IPAM ties the shared TLS watcher to its listener lifecycle."""

import asyncio
from unittest.mock import AsyncMock, Mock

import control_plane


def test_standalone_watcher_stops_with_control_plane(monkeypatch):
    """A listener watcher starts once and is awaited on exit."""
    cp = control_plane.NetboxControlPlane.__new__(control_plane.NetboxControlPlane)
    cp.spoke_id, cp.config, cp.hub_url = "ipam", {}, "wss://hub"
    cp._agent_listener_enabled = lambda: True
    cp._start_agent_server_task = Mock()
    cp._agent_server_task = None
    cp._start_ipam_tls_watch = Mock()
    cp._stop_ipam_tls_watch = AsyncMock()
    cp.register_module = Mock()
    module = Mock()
    module.start_kea_sync = AsyncMock()
    module.start_nw_flush = AsyncMock()
    module.stop_kea_sync = AsyncMock()
    module.stop_nw_flush = AsyncMock()
    monkeypatch.setattr(control_plane, "NetboxSpoke", lambda *a, **k: module)
    monkeypatch.setattr(control_plane.AgentHostingControlPlane, "run", AsyncMock())
    asyncio.run(cp.run())
    cp._start_ipam_tls_watch.assert_called_once()
    cp._stop_ipam_tls_watch.assert_awaited_once()

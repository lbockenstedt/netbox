"""Unit tests for security audit hardening in NetboxEngine and NetboxSpoke:
1. Cross-origin and downgrade pagination link rejection in NetboxEngine._api_get_all.
2. Cross-origin upload download_url rejection in NetboxSpoke._fetch_upload_bytes.
3. Cert Fernet key derivation using control plane / env secrets without predictable fallbacks.
"""
import os
import sys
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
try:
    from core.src.base_spoke import BaseSpoke  # noqa: F401
except ImportError:
    pass

from netbox_engine import NetboxEngine
import netbox_spoke as spoke_mod


def test_api_get_all_rejects_cross_origin_next_link():
    eng = NetboxEngine("https://netbox.internal:8443", "test-token")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "results": [{"id": 1}],
        "next": "https://attacker.evil.com/api/ipam/prefixes/?offset=500"
    }
    eng.nb.http_session.get = MagicMock(return_value=mock_resp)

    with pytest.raises(ValueError, match="Cross-origin pagination link rejected"):
        eng._api_get_all("/api/ipam/prefixes/")


def test_api_get_all_rejects_https_downgrade_next_link():
    eng = NetboxEngine("https://netbox.internal:8443", "test-token")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "results": [{"id": 1}],
        "next": "http://netbox.internal:8443/api/ipam/prefixes/?offset=500"
    }
    eng.nb.http_session.get = MagicMock(return_value=mock_resp)

    with pytest.raises(ValueError, match="HTTPS to HTTP downgrade in pagination link rejected"):
        eng._api_get_all("/api/ipam/prefixes/")


def test_api_get_all_allows_same_origin_pagination():
    eng = NetboxEngine("https://netbox.internal:8443", "test-token")
    resp1 = MagicMock()
    resp1.ok = True
    resp1.json.return_value = {
        "results": [{"id": 1}],
        "next": "https://netbox.internal:8443/api/ipam/prefixes/?offset=500"
    }
    resp2 = MagicMock()
    resp2.ok = True
    resp2.json.return_value = {
        "results": [{"id": 2}],
        "next": None
    }
    eng.nb.http_session.get = MagicMock(side_effect=[resp1, resp2])

    res = eng._api_get_all("/api/ipam/prefixes/")
    assert len(res) == 2
    assert res[0]["id"] == 1
    assert res[1]["id"] == 2


def test_fetch_upload_bytes_rejects_cross_origin_url():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.control_plane = MagicMock()
    sp.control_plane.hub_url = "https://hub.local:443"

    with pytest.raises(ValueError, match="Cross-origin upload download_url rejected"):
        sp._fetch_upload_bytes("https://malicious.external.com/file.xlsx", "token123")


def test_fetch_upload_bytes_rejects_https_downgrade():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.control_plane = MagicMock()
    sp.control_plane.hub_url = "https://hub.local:443"

    with pytest.raises(ValueError, match="HTTPS to HTTP downgrade"):
        sp._fetch_upload_bytes("http://hub.local:443/file.xlsx", "token123")


def test_fetch_upload_bytes_rejects_cross_port():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.control_plane = MagicMock()
    sp.control_plane.hub_url = "https://hub.local:443"

    with pytest.raises(ValueError, match="Cross-port upload download_url rejected"):
        sp._fetch_upload_bytes("https://hub.local:8443/file.xlsx", "token123")


def test_fetch_upload_bytes_rejects_invalid_scheme():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.control_plane = MagicMock()
    sp.control_plane.hub_url = "https://hub.local:443"

    with pytest.raises(ValueError, match="Invalid upload URL scheme"):
        sp._fetch_upload_bytes("//attacker.com/file.xlsx", "token123")


def test_fetch_upload_bytes_rejects_empty_host():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.control_plane = MagicMock()
    sp.control_plane.hub_url = "https://hub.local:443"

    with pytest.raises(ValueError, match="Invalid upload URL host"):
        sp._fetch_upload_bytes("https:///file.xlsx", "token123")


def test_fetch_upload_bytes_rejects_missing_hub_url():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.control_plane = MagicMock()
    sp.control_plane.hub_url = ""

    with patch.dict(os.environ, {"LM_HUB_URL": ""}):
        with pytest.raises(ValueError, match="Cannot verify origin for download_url without configured hub_url"):
            sp._fetch_upload_bytes("https://hub.local:443/file.xlsx", "token123")


def test_cert_fernet_derivation_deterministic_with_secret():
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.spoke_id = "netbox-spoke-1"
    sp.control_plane = MagicMock()
    sp.control_plane.secret = "my-secret-vault-key"

    f1 = sp._cert_fernet()
    f2 = sp._cert_fernet()
    test_data = b"secret-private-key-data"
    encrypted = f1.encrypt(test_data)
    assert f2.decrypt(encrypted) == test_data

    # Different secret must produce different key and fail to decrypt
    sp.control_plane.secret = "different-secret"
    f3 = sp._cert_fernet()
    import cryptography.fernet
    with pytest.raises(cryptography.fernet.InvalidToken):
        f3.decrypt(encrypted)

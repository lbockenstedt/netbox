import ipaddress
import re
import pynetbox
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from custom_fields_spec import CUSTOM_FIELDS_SPEC

# Limit concurrent HTTP requests to gunicorn to avoid OOM-killing workers
# when multiple IPAM queries arrive simultaneously.
_netbox_http_sem = threading.Semaphore(1)

logger = logging.getLogger("NetboxEngine")

def get_version():
    try:
        with open("VERSION", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "unknown"

version = get_version()

def _clean_token(token: str) -> str:
    """Strip any prefix a user may have copy-pasted alongside the raw token."""
    if not token:
        return token
    for prefix in ("Token ", "Bearer ", "token ", "bearer "):
        if token.startswith(prefix):
            return token[len(prefix):]
    return token


from netbox_dcim import DcimMixin
from netbox_ipam import IpamMixin
from netbox_vmsync import VmSyncMixin
from netbox_changelog import ChangelogMixin
from netbox_sync import SyncMixin
from netbox_staleness import StalenessMixin
from netbox_tenancy import TenancyMixin


class NetboxEngine(DcimMixin, IpamMixin, VmSyncMixin, ChangelogMixin, SyncMixin, StalenessMixin, TenancyMixin):
    """
    NetBox API client. Covers DCIM (devices/racks) and IPAM (prefixes/IPs).
    """
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = _clean_token(token)
        self.nb = pynetbox.api(url, token=self.token)
        self._apply_auth()
        logger.info(f"Initialized NetboxEngine v{version} → {url}")

    def reconnect(self, url: str, token: str):
        self.url = url
        self.token = _clean_token(token)
        self.nb = pynetbox.api(url, token=self.token)
        self._apply_auth()

    def _apply_auth(self) -> None:
        """Pin the Authorization header onto the shared http_session.

        pynetbox >=7 applies the token per-request via its internal Request
        wrapper and does NOT set it on `http_session.headers`. _api_get() calls
        `http_session.get()` directly (bypassing that wrapper), so without this
        the GET goes out unauthenticated and NetBox returns 403
        "Authentication credentials were not provided." Setting it on the
        session ourselves makes the direct GETs authenticate; it is a no-op for
        the ORM methods, which inject their own header per-request."""
        self.nb.http_session.headers.update({"Authorization": f"Token {self.token}"})

    def _api_get(self, path: str, params: dict = None) -> dict:
        """Single-page GET — uses the existing pynetbox session (auth already set).
        Never follows pagination links, so this is always exactly ONE HTTP request.
        Serialised via a module-level semaphore to prevent concurrent requests from
        exhausting gunicorn worker memory."""
        url = self.url.rstrip("/") + path
        with _netbox_http_sem:
            resp = self.nb.http_session.get(url, params=params or {})
        if not resp.ok:
            raise Exception(f"{resp.status_code} {resp.reason} from {path}")
        return resp.json()

    def _api_get_all(self, path: str, params: dict = None,
                     max_pages: int = 200) -> list:
        """Paginated GET that follows NetBox ``next`` links until exhausted.

        Used by ``find_available_prefixes`` which needs the *complete* set of
        prefixes within a block (the existing ``_api_get`` caps at one page of
        ``limit`` and silently truncates). ``max_pages`` is a runaway guard.
        The first request carries ``params``; subsequent requests hit the
        absolute ``next`` URL NetBox returns (which already encodes the
        offset), so params are not re-sent."""
        params = dict(params or {})
        params.setdefault("limit", 500)
        base_url = self.url.rstrip("/") + path
        results: list = []
        next_url: Optional[str] = None
        for _ in range(max_pages):
            with _netbox_http_sem:
                if next_url:
                    resp = self.nb.http_session.get(next_url)
                else:
                    resp = self.nb.http_session.get(base_url, params=params)
            if not resp.ok:
                raise Exception(f"{resp.status_code} {resp.reason} from {path}")
            data = resp.json()
            results.extend(data.get("results", []))
            next_url = data.get("next")
            if not next_url:
                break
        return results

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
    # perf-scan D1b: TTL for the reference-data (site/role) resolver cache.
    # Sites and device-roles are created out-of-band in NetBox and change
    # rarely; a short TTL keeps a fleet-wide POLL NOW from re-fetching the
    # same slug once per device while still picking up an added site within
    # a few minutes. Override via LM_NETBOX_REF_TTL for testing.
    import os as _os
    _ref_cache_ttl = float(_os.environ.get("LM_NETBOX_REF_TTL", "300") or 300)

    def __init__(self, url: str, token: str, verify_ssl: bool = True):
        self.url = url
        self.token = _clean_token(token)
        self.verify_ssl = bool(verify_ssl)
        self.nb = pynetbox.api(url, token=self.token)
        self._apply_auth()
        self._apply_ssl()
        # perf-scan D1b: shared slug→object cache for reference data (sites,
        # device-roles). Keyed "kind:slug"; each entry {"ts", "value"}. Shared
        # across every mixin because they all share `self`.
        self._ref_cache: Dict[str, Any] = {}
        logger.info(f"Initialized NetboxEngine v{version} → {url}")

    def reconnect(self, url: str, token: str, verify_ssl: bool = None):
        self.url = url
        self.token = _clean_token(token)
        if verify_ssl is not None:
            self.verify_ssl = bool(verify_ssl)
        self.nb = pynetbox.api(url, token=self.token)
        self._apply_auth()
        self._apply_ssl()
        # A new API target invalidates any slug→object mappings from the old one.
        self._ref_cache = {}

    def _cached_ref(self, kind: str, slug: str, fetch):
        """TTL-cached slug→object resolver for rarely-changing reference data.

        ``kind`` namespaces the key ("site", "device_role"); ``fetch`` is a
        zero-arg callable that performs the actual ``self.nb`` lookup on a miss.
        A ``None`` result (slug not found) is cached too, so a fleet of devices
        pointing at a bogus slug doesn't re-hit the API once per device. TTL-only
        expiry — there is no site/role mutation command to invalidate against.
        Best-effort: on fetch error, returns None without caching (so a
        transient API blip retries next call)."""
        import time as _time
        key = f"{kind}:{(slug or '').strip().lower()}"
        entry = getattr(self, "_ref_cache", None)
        if entry is None:  # defensive: pre-__init__ / reconnect race
            self._ref_cache = entry = {}
        hit = entry.get(key)
        if hit is not None and (_time.time() - hit["ts"]) < self._ref_cache_ttl:
            return hit["value"]
        try:
            value = fetch()
        except Exception as e:  # noqa: BLE001 — don't poison cache on transient error
            logger.warning("ref-cache %s lookup failed: %s", key, e)
            return None
        entry[key] = {"ts": _time.time(), "value": value}
        return value

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

    def _apply_ssl(self) -> None:
        """Toggle TLS certificate verification on the shared http_session.

        Both the pynetbox ORM methods and _api_get()'s direct
        ``http_session.get()`` calls go through ``self.nb.http_session``, so
        setting ``verify`` there covers every request. Verify defaults ON; a
        deployment pointing at a NetBox server with a self-signed cert (e.g. the
        Azure NetBox behind a public IP) can turn it OFF via the module's
        ``netbox_verify_ssl`` setting / ``NETBOX_VERIFY_SSL=0``. When OFF we
        silence urllib3's per-request InsecureRequestWarning (otherwise the sync
        loops would flood the log) and warn ONCE that the cert is unverified —
        no silent downgrade."""
        self.nb.http_session.verify = self.verify_ssl
        if not self.verify_ssl:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
            logger.warning(
                "NetBox TLS verification is OFF (netbox_verify_ssl=0) — the "
                "NetBox server cert at %s is NOT authenticated; an on-path MITM "
                "can read/forge the API traffic. Use only with a trusted "
                "self-signed NetBox; set netbox_verify_ssl=1 once a CA-signed "
                "cert is deployed.", self.url)

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

import os as _os
import pynetbox
import logging
import threading
from typing import Any, Dict, Optional

import requests

# Limit concurrent HTTP requests to gunicorn to avoid OOM-killing workers
# when multiple IPAM queries arrive simultaneously.
_netbox_http_sem = threading.Semaphore(1)

logger = logging.getLogger("NetboxEngine")

# Default per-request timeout (seconds) for EVERY upstream NetBox call.
# requests' own default is NO timeout → a hung NetBox worker / stalled
# reverse-proxy / black-holed route blocks the calling thread FOREVER. These
# calls run in to_thread workers, so a handful of stuck requests wedge the
# spoke: the hub WS stays "online" but every command (NETBOX_GET_*,
# NETBOX_STALENESS_SWEEP, …) times out. A finite read timeout bounds the hang
# to this many seconds and reclaims the thread. Override via the env var.
_NETBOX_API_TIMEOUT = float(_os.environ.get("LM_NETBOX_API_TIMEOUT", "30") or 30)


class _DefaultTimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    """Mount on ``nb.http_session`` so every request — pynetbox ORM calls
    (``.all()``/``.filter()``/``.save()``) AND our direct ``http_session.get()``
    — gets a default connect+read timeout. A caller that already passes a
    (non-None) timeout keeps it."""

    def __init__(self, timeout: float, *a, **kw):
        self._timeout = timeout
        super().__init__(*a, **kw)

    def send(self, request, **kw):
        if kw.get("timeout") is None:
            kw["timeout"] = self._timeout
        return super().send(request, **kw)

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
        self._apply_timeout()
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
        self._apply_timeout()
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

    def _apply_timeout(self) -> None:
        """Mount a default-timeout adapter so a hung NetBox can't block a
        worker thread forever. Covers pynetbox ORM calls + our direct
        ``http_session.get()`` (both route through ``Session.send`` → the
        adapter). See ``_DefaultTimeoutHTTPAdapter``."""
        adapter = _DefaultTimeoutHTTPAdapter(_NETBOX_API_TIMEOUT)
        self.nb.http_session.mount("https://", adapter)
        self.nb.http_session.mount("http://", adapter)

    def _api_get(self, path: str, params: dict = None) -> dict:
        """Single-page GET — uses the existing pynetbox session (auth already set).
        Never follows pagination links, so this is always exactly ONE HTTP request.
        Serialised via a module-level semaphore to prevent concurrent requests from
        exhausting gunicorn worker memory."""
        url = self.url.rstrip("/") + path
        with _netbox_http_sem:
            resp = self.nb.http_session.get(url, params=params or {},
                                            timeout=_NETBOX_API_TIMEOUT)
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
                    resp = self.nb.http_session.get(next_url, timeout=_NETBOX_API_TIMEOUT)
                else:
                    resp = self.nb.http_session.get(base_url, params=params,
                                                    timeout=_NETBOX_API_TIMEOUT)
            if not resp.ok:
                raise Exception(f"{resp.status_code} {resp.reason} from {path}")
            data = resp.json()
            results.extend(data.get("results", []))
            next_url = data.get("next")
            if not next_url:
                break
        return results

"""NetBox API client used by the NetBox spoke.

NetboxEngine wraps a pynetbox connection (auth header, TLS verification and a
default request timeout) and combines the feature mixins (DCIM, IPAM, VM sync,
changelog, sync, staleness, dedupe, tenancy) into one object. It also provides
the low-level paginated/unpaginated GET helpers the mixins share.
"""
# Stdlib os under a private alias: reads env overrides and the VERSION file path.
import os as _os
# pynetbox is the NetBox REST client library.
import pynetbox
# Standard-library logging for status and error messages.
import logging
# threading provides the semaphore that serialises direct HTTP requests.
import threading
# Pacing of background requests (see _background_turn).
import contextlib
import time
# Type hints.
from typing import Any, Dict, Optional

# requests supplies the HTTPAdapter subclassed below for default timeouts.
import requests

# Limit concurrent HTTP requests to gunicorn to avoid OOM-killing workers
# when multiple IPAM queries arrive simultaneously. Whole-fleet listings get
# their own slot so a quick read (prefixes, one device) never waits behind a
# many-page device walk: at most two requests run at once.
_netbox_http_sem = threading.Semaphore(1)
_netbox_bulk_sem = threading.Semaphore(1)

# Background feeds (discovery syncs, sweeps; see NetboxSpoke._BACKGROUND_MUTATIONS)
# must leave NetBox room for people and other modules, whoever sends them, on
# whatever schedule, however big. Background requests take turns through one
# gate, and each turn waits as long as the previous background request took.
# So all background work together uses at most about half of NetBox, and slows
# down by itself when NetBox is slow. The wait happens before the HTTP slot is
# taken, so user requests never queue behind it.
_background = threading.local()
_background_gate = threading.Lock()
_background_next = 0.0
_BACKGROUND_MAX_WAIT_S = 30.0


def run_as_background(fn, *args, **kwargs):
    """Run ``fn`` in this thread with its NetBox requests paced (see above)."""
    _background.on = True
    try:
        return fn(*args, **kwargs)
    finally:
        _background.on = False


@contextlib.contextmanager
def _background_turn():
    """One paced NetBox request for a background thread; no-op otherwise."""
    global _background_next
    if not getattr(_background, "on", False) or getattr(_background, "in_turn", False):
        yield
        return
    with _background_gate:
        time.sleep(max(0.0, _background_next - time.monotonic()))
        _background.in_turn = True
        start = time.monotonic()
        try:
            yield
        finally:
            _background.in_turn = False
            took = time.monotonic() - start
            _background_next = time.monotonic() + min(took, _BACKGROUND_MAX_WAIT_S)


_BULK_PATHS = frozenset({"/api/dcim/devices/", "/api/dcim/interfaces/",
                         "/api/virtualization/virtual-machines/",
                         "/api/virtualization/interfaces/", "/api/dcim/cables/"})

# Named logger for all engine messages.
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
        """Remember the default timeout and build a normal HTTPAdapter."""
        self._timeout = timeout
        super().__init__(*a, **kw)

    def send(self, request, **kw):
        # Only fill in a timeout when the caller did not choose one.
        if kw.get("timeout") is None:
            kw["timeout"] = self._timeout
        # Hand the request to the normal adapter (paced for background work).
        with _background_turn():
            return super().send(request, **kw)

def get_version():
    """Return this spoke's version string from the VERSION file, or "unknown"."""
    # VERSION lives one directory above src/.
    try:
        version_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "VERSION")
        with open(version_path, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        # Running from a tree without a VERSION file.
        return "unknown"

# Read once at import; shown in the startup log line.
version = get_version()

def _clean_token(token: str) -> str:
    """Strip any prefix a user may have copy-pasted alongside the raw token."""
    # Empty/None token: nothing to clean.
    if not token:
        return token
    # Remove a leading "Token "/"Bearer " that came from a copied header.
    for prefix in ("Token ", "Bearer ", "token ", "bearer "):
        if token.startswith(prefix):
            return token[len(prefix):]
    # Token was already bare.
    return token


# Feature mixins; each adds one area of NetBox operations to NetboxEngine.
# Imported here (after the helpers) because they may import names from this module.
from netbox_dcim import DcimMixin
from netbox_ipam import IpamMixin
from netbox_vmsync import VmSyncMixin
from netbox_changelog import ChangelogMixin
from netbox_sync import SyncMixin
from netbox_staleness import StalenessMixin
from netbox_dedupe import DedupeMixin
from netbox_churn import ChurnMixin
from netbox_tenancy import TenancyMixin
from netbox_autorack import AutoRackMixin


class NetboxEngine(DcimMixin, IpamMixin, VmSyncMixin, ChangelogMixin, SyncMixin, StalenessMixin, DedupeMixin, TenancyMixin, AutoRackMixin, ChurnMixin):
    """
    NetBox API client. Covers DCIM (devices/racks) and IPAM (prefixes/IPs).
    """
    # perf-scan D1b: TTL for the reference-data (site/role) resolver cache.
    # Sites and device-roles are created out-of-band in NetBox and change
    # rarely; a short TTL keeps a fleet-wide POLL NOW from re-fetching the
    # same slug once per device while still picking up an added site within
    # a few minutes. Override via LM_NETBOX_REF_TTL for testing.
    # Local os alias in the class body, used for the env lookups just below.
    import os as _os
    _ref_cache_ttl = float(_os.environ.get("LM_NETBOX_REF_TTL", "300") or 300)
    # Negative-cache TTL: a "not found" (None) is held only a few seconds so a
    # newly-created site/role resolves on the next sync tick instead of being
    # pinned as missing for the full positive TTL.
    _ref_cache_neg_ttl = float(_os.environ.get("LM_NETBOX_REF_NEG_TTL", "5") or 5)

    def __init__(self, url: str, token: str, verify_ssl: bool = True):
        """Connect to NetBox at ``url`` with ``token``, optionally verifying TLS."""
        # Remember the connection settings (token stripped of any copied prefix).
        self.url = url
        self.token = _clean_token(token)
        self.verify_ssl = bool(verify_ssl)
        # Build the pynetbox client, then configure its shared HTTP session.
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
        """Point the engine at a (possibly new) NetBox URL/token and rebuild the session."""
        # Update connection settings; verify_ssl is only changed when explicitly given.
        self.url = url
        self.token = _clean_token(token)
        if verify_ssl is not None:
            self.verify_ssl = bool(verify_ssl)
        # Rebuild the pynetbox client and reapply auth, TLS and timeout settings.
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
        A successful (non-None) lookup is cached for the full ``_ref_cache_ttl``.
        A ``None`` result (slug not found) is cached only VERY briefly
        (``_ref_cache_neg_ttl``, a few seconds): that still absorbs a burst of a
        fleet of devices pointing at a bogus slug within one sync, but does NOT
        pin the miss for 5 min — so once the missing site/role is created in
        NetBox, the very next sync tick re-resolves it instead of failing for up
        to the full TTL. TTL-only expiry (there is no site/role mutation command
        to invalidate against). Best-effort: on fetch error, returns None
        without caching (so a transient API blip retries next call)."""
        # time is imported lazily; only used for cache timestamps.
        import time as _time
        # Normalise the slug so "Lab" and "lab " share one cache entry.
        key = f"{kind}:{(slug or '').strip().lower()}"
        entry = getattr(self, "_ref_cache", None)
        if entry is None:  # defensive: pre-__init__ / reconnect race
            self._ref_cache = entry = {}
        # Look for a cached entry.
        hit = entry.get(key)
        if hit is not None:
            # Negative (None) entries expire fast so a freshly-created object is
            # picked up next tick; positive entries live the full TTL.
            ttl = (self._ref_cache_neg_ttl if hit["value"] is None
                   else self._ref_cache_ttl)
            # Entry still fresh: return it without an API call.
            if (_time.time() - hit["ts"]) < ttl:
                return hit["value"]
        # Miss or expired: perform the real lookup.
        try:
            value = fetch()
        except Exception as e:  # noqa: BLE001 — don't poison cache on transient error
            # Transient API error: log it and do not cache the failure.
            logger.warning("ref-cache %s lookup failed: %s", key, e)
            return None
        # Store the result (including None) with the current time.
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
        # requests honours session.verify for every call made through this session.
        self.nb.http_session.verify = self.verify_ssl
        if not self.verify_ssl:
            # Hide urllib3's per-request warning; ignore failure if urllib3 is unavailable.
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
            # One clear warning that the server certificate is not being checked.
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
        # One adapter instance for both schemes so every request gets the default timeout.
        adapter = _DefaultTimeoutHTTPAdapter(_NETBOX_API_TIMEOUT)
        self.nb.http_session.mount("https://", adapter)
        self.nb.http_session.mount("http://", adapter)

    def _api_get(self, path: str, params: dict = None) -> dict:
        """Single-page GET — uses the existing pynetbox session (auth already set).
        Never follows pagination links, so this is always exactly ONE HTTP request.
        Serialised via a module-level semaphore to prevent concurrent requests from
        exhausting gunicorn worker memory."""
        # Build the absolute URL from the base and the API path.
        url = self.url.rstrip("/") + path
        # One request at a time against NetBox (see _netbox_http_sem).
        with _background_turn(), _netbox_http_sem:
            resp = self.nb.http_session.get(url, params=params or {},
                                            timeout=_NETBOX_API_TIMEOUT)
        # Turn HTTP errors into an exception with status and path for the caller's log.
        if not resp.ok:
            raise Exception(f"{resp.status_code} {resp.reason} from {path}")
        # Parsed JSON body.
        return resp.json()

    def _api_get_all(self, path: str, params: dict = None,
                     max_pages: int = 200, meta: dict = None) -> list:
        """Paginated GET that follows NetBox ``next`` links until exhausted.

        Used by ``find_available_prefixes`` which needs the *complete* set of
        prefixes within a block (the existing ``_api_get`` caps at one page of
        ``limit`` and silently truncates). ``max_pages`` is a runaway guard.
        The first request carries ``params``; subsequent requests hit the
        absolute ``next`` URL NetBox returns (which already encodes the
        offset), so params are not re-sent."""
        # Copy params so the caller's dict is not modified; default to 500 rows per page.
        params = dict(params or {})
        params.setdefault("limit", 500)
        # Absolute first-page URL, accumulated rows and the next-page link.
        base_url = self.url.rstrip("/") + path
        results: list = []
        next_url: Optional[str] = None
        # Fetch at most max_pages pages to avoid an endless loop on a bad "next" link.
        sem = _netbox_bulk_sem if path in _BULK_PATHS else _netbox_http_sem
        for _ in range(max_pages):
            with _background_turn(), sem:
                # Later pages: follow NetBox's absolute "next" URL (already includes offset).
                if next_url:
                    resp = self.nb.http_session.get(next_url, timeout=_NETBOX_API_TIMEOUT)
                # First page: base URL plus the caller's filters.
                else:
                    resp = self.nb.http_session.get(base_url, params=params,
                                                    timeout=_NETBOX_API_TIMEOUT)
            # Any HTTP error aborts the whole fetch.
            if not resp.ok:
                raise Exception(f"{resp.status_code} {resp.reason} from {path}")
            # Collect this page's rows and find the next page link.
            data = resp.json()
            results.extend(data.get("results", []))
            next_url = data.get("next")
            # No next link: all pages read.
            if not next_url:
                break
        # Report whether a next page was still pending (truncated read).
        if meta is not None:
            meta["more"] = bool(next_url)
        # Every row from every page.
        return results

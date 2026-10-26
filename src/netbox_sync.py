"""Device / NW-device / access-tracker sync + discovery helpers for NetboxEngine."""
import ipaddress
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import pynetbox

logger = logging.getLogger("NetboxEngine")


# Auto-generated names the create branch mints when a record has no hostname
# (device-<mac12>, device-<ip>, device-<serial>/unknown, optionally uniquified).
# A device still carrying one was never named by a human, so any source that
# later learns its real hostname (DHCP/DNS) may rename it.
_PLACEHOLDER_NAME_RE = re.compile(
    r"^device-(?:[0-9a-f]{12}|\d{1,3}(?:\.\d{1,3}){3}|unknown)(?:-[0-9a-z]+)*$", re.I)


def _is_auto_name(name) -> bool:
    """True for a name no human chose: empty, the device-<mac>/<ip>
    placeholder, a bare IPv4 address, or a bare 12-hex-digit MAC."""
    s = str(name or "").strip()
    if not s or _PLACEHOLDER_NAME_RE.match(s):
        return True
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", s):
        return True
    return bool(re.fullmatch(r"[0-9a-fA-F]{12}", re.sub(r"[:.\-]", "", s)))


# NetBox's own ipaddress.dns_name validator; anything else 400s the save.
_DNS_NAME_RE = re.compile(r"^([0-9A-Za-z_-]+|\*)(\.[0-9A-Za-z_-]+)*\.?$")


# Device custom fields that carry a MAC sighting ("last seen on switch X port
# Y"). The incoming record uses the same names (``_incoming_device_record``).
_SWITCH_CF_KEYS = ("switch_name", "switch_ip", "switch_port")


def _normalize_source_tag(source: Any) -> str:
    """The ``discovered_from`` ownership tag for a sync ``source``: the
    firewall synonyms collapse to the legacy ``"opnsense"`` tag (existing
    firewall deployments stay byte-identical); anything else (nw's
    ``"Network Devices"``) is used verbatim."""
    tag = str(source or "opnsense").strip()
    return "opnsense" if tag.lower() in ("opnsense", "fw", "firewall") else tag


def _primary_addr(row: dict) -> str:
    """Bare primary IPv4 (no mask) of a listed device row, or ''."""
    pip = row.get("primary_ip4")
    if not isinstance(pip, dict):
        return ""
    return (pip.get("address") or "").split("/")[0].strip()


@dataclass
class _DeviceSyncRun:
    """Everything ONE ``sync_devices`` call shares between its steps.

    The indexes are a snapshot of NetBox taken once at the start of the batch
    (one paginated listing) — they do NOT see devices created later in the
    batch. The ``used_names`` / ``created_by_name`` / ``refreshed_ids`` sets
    cover that gap; see the field comments.
    """
    tenant_slug: str
    source_tag: str                      # discovered_from value we own
    source_of_truth: str                 # "external" | "netbox"
    defaults: Dict[str, Any]
    tenant: Any = None
    # result counters
    pushed: int = 0
    errors: int = 0
    skipped: int = 0
    deleted: int = 0
    first_err: Optional[str] = None      # first failure text, shown in the result
    # pre-batch indexes of existing device rows
    by_ip: Dict[str, dict] = field(default_factory=dict)      # primary IPv4 → row (last wins)
    by_name: Dict[str, dict] = field(default_factory=dict)    # name.lower() → row
    by_mac: Dict[str, dict] = field(default_factory=dict)     # cf.mac_address → row
    by_serial: Dict[str, dict] = field(default_factory=dict)  # serial.lower() → row
    owned_ips: set = field(default_factory=set)  # primary IPs of devices we own
    # Names created/refreshed THIS batch: a 2nd create with the same name would
    # 400 on (name, site, tenant), and by_name can't see these.
    used_names: set = field(default_factory=set)
    # hostname.lower() → id of a device CREATED this batch, so an
    # operator-named second address can join it.
    created_by_name: Dict[str, Any] = field(default_factory=dict)
    # Device ids already updated this batch: the hostname tier never re-adopts
    # them and the create branch never reclaims (deletes) them by name.
    refreshed_ids: set = field(default_factory=set)
    # batch-wide create refs (``_resolve_create_defaults``)
    role: Any = None
    dtype: Any = None
    site: Any = None
    cf_whitelist: set = field(default_factory=set)
    # set after the first "site required" create failure (ccb5034)
    site_required_abort: bool = False
    # ── live pynetbox Records for this batch (perf, B3) ──
    # Rows waiting to be turned into Records on first use (id → listed row).
    # Built from the ONE device listing / the IP prefetch, so an unchanged
    # device costs no GET at all. See ``SyncMixin._device_record``.
    device_rows: Dict[Any, dict] = field(default_factory=dict)
    ip_rows: Dict[Any, dict] = field(default_factory=dict)
    # id → the ONE live Record every write to that object goes through this
    # batch (``save()`` re-reads the PATCH response into it, so a device
    # matched by two records sees the first record's writes).
    device_objs: Dict[Any, Any] = field(default_factory=dict)
    ip_objs: Dict[Any, Any] = field(default_factory=dict)
    deleted_ids: set = field(default_factory=set)  # devices deleted this batch

    def forget_device(self, dev_id: Any) -> None:
        """A write to this device FAILED (or may have): drop its Record so the
        next use re-reads NetBox. A failed ``save()`` keeps the unsent change
        on the Record — e.g. a rename that 400s on the (name, site, tenant)
        constraint — and every later save would re-send it and fail too. The
        per-write GET this cache replaced naturally discarded it."""
        self.device_objs.pop(dev_id, None)

    def forget_ip(self, ip_id: Any) -> None:
        """An IP was written OUTSIDE its cached Record (``_reuse_or_create_ip``
        reassigns/tags by its own lookup, a device delete cascades to its
        IPs): drop what we hold so the next use re-reads it (GET), exactly
        like the per-write GET this cache replaced."""
        self.ip_rows.pop(ip_id, None)
        self.ip_objs.pop(ip_id, None)

    def owns(self, cf: Optional[dict]) -> bool:
        """True iff a device's custom fields carry OUR ``discovered_from`` tag
        (case-insensitive)."""
        return str((cf or {}).get("discovered_from") or "").lower() == self.source_tag.lower()

    def note_error(self, text: str) -> None:
        """Count one error; keep the first one's text for the result message."""
        self.errors += 1
        if self.first_err is None:
            self.first_err = text


class _IncomingDevice:
    """Read-only view of one normalized incoming record (see
    ``SyncMixin._incoming_device_record``) plus the facts every step asks."""

    __slots__ = ("key", "rec", "mac", "hostname", "serial", "real_ip")

    def __init__(self, key: str, rec: Dict[str, Any]):
        self.key = key                     # incoming-set key: IP or mac:/host:/serial:
        self.rec = rec
        self.mac = rec["mac"]
        self.hostname = rec["hostname"]
        self.serial = rec.get("serial") or ""
        # A synthetic key carries no address to assign.
        self.real_ip = "" if key.startswith(("mac:", "host:", "serial:")) else key

    @property
    def keyed_by_ip(self) -> bool:
        return bool(self.real_ip)

    @property
    def has_hostname(self) -> bool:
        """A usable hostname ('unknown' is what DHCP reports for none)."""
        return bool(self.hostname) and self.hostname.lower() != "unknown"

    @property
    def operator_named(self) -> bool:
        """The hostname is an operator assertion (DNS A record / DHCP
        reservation): one name = one device."""
        return self.rec.get("hostname_source") in ("dns", "reservation")

    @property
    def default_name(self) -> str:
        """Name for a new device: the hostname, else an auto placeholder
        ``device-<mac12>`` / ``device-<serial>`` / ``device-<ip>``."""
        if self.has_hostname:
            return self.hostname
        if self.mac:
            return f"device-{self.mac.replace(':', '')}"
        if self.serial:
            return f"device-{self.serial}"
        return f"device-{self.real_ip or 'unknown'}"


class SyncMixin:
    """Device / NW-device / access-tracker sync + discovery helpers for NetboxEngine."""

    # ---- firewall→NetBox device discovery sync helpers ---------------------

    @staticmethod
    def _nb_name(name) -> str:
        """Canonical NetBox device name: upper-case. Every sink writes device
        names through this so a box reported as ``mipbe-ssplm-n31-tor`` by DNS
        and ``MIPBE-SSPLM-N31-TOR`` by LLDP is one consistently named record;
        matching stays case-insensitive everywhere."""
        return str(name or "").strip().upper()

    @staticmethod
    def _norm_mac(mac: str) -> str:
        """Normalize a MAC to lowercase colon form (aa:bb:cc:dd:ee:ff).

        The OPNsense spoke returns the raw MAC; normalize here so the value
        written to the IP's ``mac_address`` custom field matches what the
        NetBox→CPPM endpoint sync reads (it keys on the colon form).
        """
        m = (mac or "").strip().lower()
        hexonly = re.sub(r"[^0-9a-f]", "", m)
        if len(hexonly) == 12:
            return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))
        return m

    @staticmethod
    def _row_has_ip_or_mac(row: dict) -> bool:
        """Whether a raw NetBox device row carries a primary IP OR a mac_address
        custom field — i.e. it is identifiable by something other than its name.

        Used by ``sync_devices`` to decide a hostname match: a device that is
        "bare" (neither IP nor MAC) sharing the incoming hostname is the same
        machine → adopt/update it in place; a device that already has an IP or
        MAC sharing a hostname is a DIFFERENT machine (the feed regularly sends
        one hostname across distinct MACs — ks205, sonoszp, iphone) → leave it
        and uniquify the new record instead of merging distinct devices.
        """
        pip = row.get("primary_ip4") or {}
        has_ip = isinstance(pip, dict) and bool(
            (pip.get("address") or "").split("/")[0].strip())
        cf = row.get("custom_fields") or {}
        has_mac = bool(NetboxEngine._norm_mac(cf.get("mac_address", "")))
        return has_ip or has_mac

    # ── perf FIX C: per-sync-run prefix prefetch for _mask_for_ip ────────────
    # _mask_for_ip used to GET /api/ipam/prefixes/?contains=<ip> once per IP
    # per record — the dominant N+1 in the VM sync. A sync run prefetches ALL
    # prefixes once and resolves masks locally (longest-prefix match); the
    # per-IP API lookup remains as the fallback for IPs no local prefix covers
    # (e.g. a prefix added mid-run) and for callers outside a sync run.

    def _begin_prefix_prefetch(self) -> None:
        """Fetch every NetBox prefix once for the current sync run so
        ``_mask_for_ip`` resolves locally. Best-effort: on failure the cache
        stays None and every lookup falls back to the per-IP API path."""
        try:
            nets = []
            by_vrf: Dict[Any, list] = {}
            for p in self._api_get_all("/api/ipam/prefixes/", {"limit": 500}):
                pfx = (p or {}).get("prefix")
                if not pfx:
                    continue
                try:
                    net = ipaddress.ip_network(pfx, strict=False)
                except ValueError:
                    continue
                nets.append(net)
                vrf = p.get("vrf")
                by_vrf.setdefault(vrf.get("id") if isinstance(vrf, dict) else None,
                                  []).append(net)
            self._prefix_prefetch = nets
            self._prefix_prefetch_vrf = by_vrf
        except Exception as e:
            logger.debug("prefix prefetch failed (per-IP fallback stays): %s", e)
            self._prefix_prefetch = None
            self._prefix_prefetch_vrf = None

    def _end_prefix_prefetch(self) -> None:
        """Drop the per-run prefix cache (call in the sync's ``finally``)."""
        self._prefix_prefetch = None
        self._prefix_prefetch_vrf = None

    @staticmethod
    def _mask_from_prefixes(ip_str: str, nets) -> Optional[str]:
        """Longest-prefix match of ``ip_str`` against a list of
        ``ipaddress`` networks. Returns the prefixlen as a string, or None
        when no prefix of the same address family covers the IP (or the IP is
        unparsable) — the caller then falls back to the per-IP API lookup."""
        try:
            addr = ipaddress.ip_address(str(ip_str).split("/")[0].strip())
        except ValueError:
            return None
        best = None
        for net in (nets or []):
            if net.version == addr.version and addr in net:
                if best is None or net.prefixlen > best:
                    best = net.prefixlen
        return str(best) if best is not None else None

    def _mask_for_ip(self, ip_str: str, tenant: Any = None) -> str:
        """Derive the mask from the most specific containing prefix; /32 if none.

        Mirrors the inline lookup in ``claim_device`` (engine.py ~296-304),
        extracted so the device sync reuses it. When a sync run has prefetched
        the prefix set (``_begin_prefix_prefetch``) the match is local — no
        API call; only an uncovered IP hits the per-IP fallback below.

        With overlapping tenant address space, a ``tenant`` whose VRF is known
        restricts the match to that VRF's prefixes (plus global-table ones) so
        another tenant's overlapping prefix can't supply the mask.
        """
        vrf_id = self._vrf_id_for_tenant(tenant) if tenant else None
        nets = getattr(self, "_prefix_prefetch", None)
        by_vrf = getattr(self, "_prefix_prefetch_vrf", None)
        if nets is not None and vrf_id is not None and by_vrf is not None:
            nets = by_vrf.get(vrf_id, []) + by_vrf.get(None, [])
        if nets is not None:
            local = self._mask_from_prefixes(ip_str, nets)
            if local is not None:
                return local
            # uncovered by the prefetched set → per-IP API fallback (catches a
            # prefix added mid-run; otherwise resolves to /32 as before).
        try:
            q: Dict[str, Any] = {"contains": ip_str, "limit": 500}
            if vrf_id is not None:
                q["present_in_vrf_id"] = vrf_id
            pdata = self._api_get("/api/ipam/prefixes/", q)
            prefs = [ipaddress.ip_network(p["prefix"], strict=False)
                     for p in pdata.get("results", []) if p.get("prefix")]
            if prefs:
                prefs.sort(key=lambda n: n.prefixlen, reverse=True)  # longest first
                return str(prefs[0].prefixlen)
        except Exception as e:
            logger.debug("containing-prefix lookup for %s failed, using /32: %s", ip_str, e)
        return "32"

    # Custom fields the sink OWNS/manages on dcim.device — a per-record
    # custom_fields map must never override these (ownership, staleness clock,
    # MAC/topology, nw linkage), or it would e.g. break _owns() by rewriting
    # discovered_from. Per-record CFs may only set free-form discovery
    # attributes (product/version/health/pool_count …).
    _SINK_MANAGED_DEVICE_CFS = frozenset({
        "discovered_from", "last_seen", "decommissioned_at", "mac_address",
        "switch_name", "switch_ip", "switch_port", "nw_device_id", "nw_managed",
    })

    @staticmethod
    def _device_cf_whitelist():
        """Return the set of custom-field names PROVISIONED on ``dcim.device``
        (from the shared ``CUSTOM_FIELDS_SPEC``). sync_devices only writes a
        per-record custom field whose name is in this set — writing a field
        that doesn't exist on the object type 400s the ENTIRE device upsert
        ("Custom field 'X' does not exist for this object type."), so an
        unknown per-record key (a feeder typo, or a field not yet added to the
        spec) is silently dropped rather than breaking the whole sync. To add a
        new pass-through field, add its (name, type, label, 'dcim.device') row
        to ``custom_fields_spec.CUSTOM_FIELDS_SPEC`` — it then provisions AND
        becomes writable here automatically (one source of truth, no drift)."""
        try:
            from custom_fields_spec import CUSTOM_FIELDS_SPEC
            return {name for (name, _t, _l, ct) in CUSTOM_FIELDS_SPEC
                    if ct == "dcim.device"}
        except Exception:
            return set()

    def _ensure_device_role(self, slug: str = "discovered"):
        """Return the device role (auto-creating 'discovered' if missing). Best-effort.

        TTL-cached per engine (``_cached_ref``): every sync_devices call — and
        every record carrying its own role — used to re-GET the same role.
        A failure returns None, cached only for the short negative TTL."""
        slug = (slug or "discovered").strip().lower() or "discovered"
        return self._cached_ref("ensure_device_role", slug,
                                lambda: self._get_or_create_device_role(slug))

    def _get_or_create_device_role(self, slug: str):
        try:
            r = self.nb.dcim.device_roles.get(slug=slug)
            if r:
                return r
            return self.nb.dcim.device_roles.create(
                name=slug.capitalize(), slug=slug, color="9e9e9e")
        except Exception as e:
            # WARNING (not debug): a None role cascades into per-device create
            # failures (device_type/role unresolved), so surface the reason.
            logger.warning("ensure_device_role '%s' failed (creates will error): %s", slug, e)
            return None

    def _ensure_device_type(self, slug: str = "discovered"):
        """Return the device type (auto-creating 'Discovered Device' under an
        'Unknown' manufacturer if missing). Best-effort; TTL-cached like
        ``_ensure_device_role``."""
        slug = (slug or "discovered").strip().lower() or "discovered"
        return self._cached_ref("ensure_device_type", slug,
                                lambda: self._get_or_create_device_type(slug))

    def _get_or_create_device_type(self, slug: str):
        try:
            dt = self.nb.dcim.device_types.get(slug=slug)
            if dt:
                return dt
            mfr = None
            try:
                mfr = self.nb.dcim.manufacturers.get(slug="unknown")
            except Exception:
                mfr = None
            if not mfr:
                mfr = self.nb.dcim.manufacturers.create(name="Unknown", slug="unknown")
            return self.nb.dcim.device_types.create(
                model="Discovered Device", slug=slug, manufacturer=mfr.id)
        except Exception as e:
            # WARNING (not debug): a None device_type cascades into per-device
            # create 400s (device_type is required), so surface the reason.
            logger.warning("ensure_device_type '%s' failed (creates will error): %s", slug, e)
            return None

    @staticmethod
    def _slugify(text: str) -> str:
        """Lowercase, hyphenate a human string into a NetBox slug. Empty →
        'discovered' so a slug is never blank."""
        import re as _re
        s = _re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
        return s or "discovered"

    def _ensure_manufacturer(self, name: str = ""):
        """Get-or-create a ``dcim.manufacturer`` by name ('Unknown' when blank),
        TTL-cached. Returns the object or None (best-effort)."""
        name = str(name or "").strip() or "Unknown"
        slug = self._slugify(name)

        def _fetch():
            m = self.nb.dcim.manufacturers.get(slug=slug)
            if m:
                return m
            return self.nb.dcim.manufacturers.create(name=name, slug=slug)
        try:
            return self._cached_ref("manufacturer", slug, _fetch)
        except Exception as e:
            logger.warning("ensure_manufacturer '%s' failed: %s", name, e)
            return None

    def _device_type_for_model(self, model: str = "", manufacturer: str = ""):
        """Get-or-create a ``dcim.device_type`` for a DISCOVERED model string so
        a device lands under its REAL type (e.g. 'Aruba 2930F', 'TrueNAS Mini
        X+') instead of the generic 'discovered' default. Keyed by a slug
        derived from manufacturer+model (so the same model under two vendors
        doesn't collide); ``model`` is the human string; the manufacturer (e.g.
        the console vendor) is ensured + linked ('Unknown' when absent).
        TTL-cached per run. Returns the device_type object, or None so the
        caller falls back to the configured default dtype. Network hardware is
        the source of truth — a feeder that learns the model (console identify,
        nw poll, ClearPass profile) upgrades the device off the generic type."""
        model = str(model or "").strip()
        if not model:
            return None
        mfr_name = str(manufacturer or "").strip()
        slug = self._slugify(f"{mfr_name}-{model}" if mfr_name else model)

        def _fetch():
            dt = self.nb.dcim.device_types.get(slug=slug)
            if dt:
                return dt
            mfr = self._ensure_manufacturer(mfr_name)
            return self.nb.dcim.device_types.create(
                model=model, slug=slug,
                manufacturer=(mfr.id if mfr else None))
        try:
            return self._cached_ref("device_type_model", slug, _fetch)
        except Exception as e:
            logger.warning("device_type_for_model '%s' failed: %s", model, e)
            return None

    def _resolve_site(self, slug: str = "", tenant=None):
        """Resolve a site for device creation. Configured slug first; else the
        first existing site; else AUTO-CREATE a fallback site. NetBox REQUIRES
        every dcim.device to carry a site, so on the create path this must not
        return None — it mirrors _ensure_device_role / _ensure_device_type, which
        auto-provision the other two required refs. Returns None only if the
        auto-create itself fails (e.g. a permissions issue — logged)."""
        slug = (slug or "").strip().lower()
        if slug:
            # perf-scan D1b: TTL-cached so a fleet poll doesn't re-fetch the
            # same slug once per device.
            s = self._cached_ref("site", slug,
                                  lambda: self.nb.dcim.sites.get(slug=slug))
            if s:
                return s
        try:
            sites = list(self.nb.dcim.sites.filter(limit=1))
            if sites:
                return sites[0]
        except Exception as e:
            # warn so a permissions issue is visible rather than silent.
            logger.warning("resolve_site first-site fallback failed: %s", e)
        # Nothing configured and no site exists — auto-create one so device
        # creation (which REQUIRES a site) succeeds instead of 400ing every
        # record (the failure behind "fw-discovery: 183 errors, site required").
        # Use the configured slug if given, else a default 'discovered' (mirrors
        # _ensure_device_role/_type). Created once; the first-site fallback above
        # adopts it on subsequent syncs.
        create_slug = slug or "discovered"
        try:
            return self.nb.dcim.sites.create(
                name=create_slug.replace("-", " ").replace("_", " ").title(),
                slug=create_slug, status="active")
        except Exception as e:
            logger.warning("resolve_site auto-create '%s' failed (device creates "
                           "will 400 on the required site): %s", create_slug, e)
        return None

    def _add_global_identities(self, by_mac: Dict[str, dict],
                               by_serial: Optional[Dict[str, dict]] = None,
                               rows: Optional[list] = None) -> None:
        """Fill MAC / serial indexes from EVERY tenant (setdefault: rows already
        indexed from the caller's tenant-scoped listing win).

        MAC and serial identify a physical device globally. A sync scoped to
        tenant B could not see tenant A's record of the same MAC and minted a
        copy every time (the RA/LRB ``device-<mac>`` pairs). ``rows`` is an
        already-fetched global device listing (sync_devices pages it once and
        reuses it); without it the listing is fetched here. Best-effort: a
        listing failure leaves the scoped indexes as they were."""
        if rows is None:
            try:
                rows = self._api_get_all("/api/dcim/devices/", {"limit": 500})
            except Exception as e:
                logger.debug("global MAC/serial index: %s", e)
                return
        for row in rows or []:
            cf = row.get("custom_fields") or {}
            rmac = self._norm_mac(cf.get("mac_address", ""))
            if rmac:
                by_mac.setdefault(rmac, row)
            rserial = str(row.get("serial") or "").strip()
            if rserial and by_serial is not None:
                by_serial.setdefault(rserial.lower(), row)

    def _index_existing_devices(self, rows) -> Dict[str, Dict[str, dict]]:
        """Index existing NetBox device rows for the canonical reconciliation
        ladder used by ALL device-creating sinks. Returns five dicts keyed by
        the identity a feeder might carry: ``by_serial`` (native serial, lower),
        ``by_mac`` (custom_fields.mac_address, normalized), ``by_nw``
        (custom_fields.nw_device_id), ``by_ip`` (primary_ip4) and ``by_name``
        (name, lower). First row wins per key. Every sink builds the SAME index
        from its ``/api/dcim/devices/`` list so a physical box discovered by
        different modules — console (serial), switch-poll (nw_device_id),
        ClearPass (mac), firewall (ip) — resolves to ONE device instead of a
        duplicate per feeder."""
        by_serial: Dict[str, dict] = {}
        by_mac: Dict[str, dict] = {}
        by_nw: Dict[str, dict] = {}
        by_ip: Dict[str, dict] = {}
        by_name: Dict[str, dict] = {}
        for row in (rows or []):
            rname = str(row.get("name") or "").strip().lower()
            if rname:
                by_name.setdefault(rname, row)
            rserial = str(row.get("serial") or "").strip()
            if rserial:
                by_serial.setdefault(rserial.lower(), row)
            cf = row.get("custom_fields") or {}
            rmac = self._norm_mac(cf.get("mac_address", ""))
            if rmac:
                by_mac.setdefault(rmac, row)
            rnw = str(cf.get("nw_device_id") or "").strip()
            if rnw:
                by_nw.setdefault(rnw, row)
            pip = row.get("primary_ip4")
            addr = ((pip.get("address") or "").split("/")[0].strip()
                    if isinstance(pip, dict) else "")
            if addr:
                by_ip.setdefault(addr, row)
        return {"by_serial": by_serial, "by_mac": by_mac, "by_nw": by_nw,
                "by_ip": by_ip, "by_name": by_name}

    def _adopt_real_name(self, devobj, hostname: str, by_name: dict,
                         source: str = "sync") -> bool:
        """Rename a device still carrying an auto name (see ``_is_auto_name``)
        to a real hostname learned by another feeder (LLDP system name, polled
        hostname). Never renames a human-chosen name and never takes a name
        another device already holds. Returns True iff renamed."""
        cur = str(getattr(devobj, "name", "") or "").strip()
        host = str(hostname or "").strip()
        if not host or host.lower() == "unknown" or _is_auto_name(host) \
                or not _is_auto_name(cur):
            return False
        new = self._nb_name(host.split(".", 1)[0])
        if not new or new == cur:
            return False
        holder = (by_name or {}).get(new.lower())
        if holder and holder.get("id") != getattr(devobj, "id", None):
            logger.info("netbox: keep %s - name %s already held by device %s",
                        cur or "(unnamed)", new, holder.get("id"))
            return False
        try:
            devobj.name = new
            devobj.save()
        except Exception as e:
            logger.warning("netbox: rename %s -> %s failed: %s", cur, new, e)
            return False
        if by_name is not None:
            row = by_name.pop(cur.lower(), None) or {}
            by_name[new.lower()] = {**row, "id": getattr(devobj, "id", None), "name": new}
        self._journal("dcim.device", getattr(devobj, "id", None), source,
                      note=f"renamed auto name {cur or '(unnamed)'} -> {new}")
        logger.info("netbox: renamed auto-named device %s -> %s", cur or "(unnamed)", new)
        return True

    @staticmethod
    def _resolve_existing_device(serial: str = "", mac: str = "",
                                 nw_device_id: str = "", ip: str = "",
                                 hostname: str = "", *,
                                 by_serial: Optional[dict] = None,
                                 by_mac: Optional[dict] = None,
                                 by_nw: Optional[dict] = None,
                                 by_ip: Optional[dict] = None,
                                 by_name: Optional[dict] = None) -> Optional[dict]:
        """Canonical device reconciliation ladder shared by every device sink:
        **SERIAL → MAC → nw_device_id → primary-IP → hostname**. Network
        hardware is the source of truth, so the strongest hardware identity is
        tried first: serial (globally unique, survives IP/hostname churn), then
        MAC, then the fleet ``nw_device_id``, then primary IP, then an exact
        hostname. The first tier whose key is present on the incoming record AND
        indexed among existing devices wins — so the same box arriving from
        different feeders reconciles to one record. A caller passes only the
        keys/indexes it has (e.g. an ARP feeder has no serial); missing tiers
        are skipped. ``hostname == 'unknown'`` never matches. Returns the
        matched existing-device row dict or ``None``."""
        by_serial = by_serial or {}; by_mac = by_mac or {}
        by_nw = by_nw or {}; by_ip = by_ip or {}; by_name = by_name or {}
        if serial:
            r = by_serial.get(str(serial).strip().lower())
            if r:
                return r
        if mac:
            r = by_mac.get(mac)
            if r:
                return r
        if nw_device_id:
            r = by_nw.get(str(nw_device_id).strip())
            if r:
                return r
        if ip:
            r = by_ip.get(str(ip).strip())
            if r:
                return r
        if hostname and hostname.strip().lower() != "unknown":
            r = by_name.get(hostname.strip().lower())
            if r:
                return r
        return None

    def _fill_device_gaps(self, run: "_DeviceSyncRun", row: dict, hostname: str,
                          mac: str) -> None:
        """Fill-gap update for an existing device when NetBox is the source of
        truth: never overwrite a populated value, only fill EMPTY ones.
        Reads/writes go through the batch's live Records (``_ip_record`` /
        ``_device_record``); custom fields are PATCHed delta-only."""
        good_host = bool(hostname) and hostname.lower() != "unknown"
        ip_id = None
        pip = row.get("primary_ip4")
        if isinstance(pip, dict):
            ip_id = pip.get("id")
        if ip_id and (mac or good_host):
            try:
                ipobj = self._ip_record(run, ip_id)
                changed = False
                if ipobj and good_host and _DNS_NAME_RE.match(hostname) and not str(getattr(ipobj, "dns_name", "") or "").strip():
                    ipobj.dns_name = hostname
                    changed = True
                if ipobj and mac and not (ipobj.custom_fields or {}).get("mac_address"):
                    self._save_cf_delta(ipobj, {"mac_address": mac})  # also sends dns_name
                elif changed:
                    ipobj.save()
            except Exception as e:
                run.forget_ip(ip_id)
                logger.debug("sync_devices: fill-gap IP %s: %s", ip_id, e)
        cf = row.get("custom_fields") or {}
        cur_name = str(row.get("name") or "").strip()
        need_mac = bool(mac) and not cf.get("mac_address")
        need_name = good_host and bool(_PLACEHOLDER_NAME_RE.match(cur_name))
        if need_mac or need_name:
            try:
                devobj = self._device_record(run, row["id"])
                if devobj is None:
                    raise LookupError("device vanished")
                if need_name:
                    new_name = self._uniq_device_name(hostname, mac, "", run.by_name, run.used_names)
                    devobj.name = new_name
                    run.used_names.add(new_name.lower())
                if need_mac:
                    self._save_cf_delta(devobj, {"mac_address": mac})  # also sends the name
                else:
                    devobj.save()
                if need_name:
                    logger.info("sync_devices: renamed placeholder %s -> %s", cur_name, devobj.name)
            except Exception as e:
                run.forget_device(row["id"])
                logger.debug("sync_devices: fill-gap device %s: %s", row.get("id"), e)

    def _adopt_secondary_ip(self, device_id, real_ip: str, mac: str, hostname: str, tenant, source_tag: str,
                            touched_ips: Optional[set] = None) -> bool:
        """An operator-assigned name (DNS A record / DHCP reservation) seen on a
        second MAC+IP is the same device's other port (a stack member's OOBM,
        in-band vs out-of-band). Attach the address to a secondary interface
        instead of minting a suffixed copy; never touch the device's primary IP,
        mac_address or name. The id of the IP written is added to
        ``touched_ips`` when given (sync_devices drops its cached copy)."""
        if not real_ip:
            return False
        iname = "mgmt-" + mac.replace(":", "")[-4:].upper() if mac else "mgmt-" + real_ip
        try:
            existing = list(self.nb.dcim.interfaces.filter(device_id=device_id, name=iname))
            iface = existing[0] if existing else self.nb.dcim.interfaces.create(device=device_id, name=iname, type="other")
            mask = self._mask_for_ip(real_ip, tenant)
            ip_kwargs = {"address": f"{real_ip}/{mask}", "assigned_object_type": "dcim.interface", "assigned_object_id": iface.id}
            if tenant: ip_kwargs["tenant"] = tenant.id
            if hostname and hostname.lower() != "unknown": ip_kwargs["dns_name"] = hostname
            ipobj = self._reuse_or_create_ip(f"{real_ip}/{mask}", ip_kwargs, real_ip, iface.id, tenant=tenant, hostname=hostname, mac=mac, source=source_tag)
            if touched_ips is not None:
                touched_ips.add(ipobj.id)
            self._journal("ipam.ipaddress", ipobj.id, source_tag, note=f"IP {real_ip}/{mask} → secondary interface {iname} of device {device_id}")
            self._stamp_last_seen(ipobj)
            return True
        except Exception as e:
            logger.debug("sync_devices: secondary IP %s on device %s failed: %s", real_ip, device_id, e)
            return False

    # ══════════════════════════════════════════════════════════════════════
    # sync_devices — discovery feed → NetBox DCIM
    #
    # Layout (read top-down; each step is one method below):
    #
    #   sync_devices                      entry point + result envelope
    #   ├─ _normalize_incoming_devices    raw feed rows → {key: record}
    #   ├─ _list_devices_for_sync         ONE paginated device listing
    #   ├─ _index_sync_rows               existing devices → lookup indexes
    #   ├─ _add_global_identities         + other tenants' MAC/serial (2d95909)
    #   ├─ _delete_vanished_owned_devices replace=True housekeeping
    #   ├─ _prefetch_primary_ips          batch-read the IPs we may touch
    #   ├─ _resolve_create_defaults       role / device type / site / cf list
    #   └─ per record: _sync_one_device
    #        ├─ _match_existing_device    SERIAL → MAC → IP → hostname
    #        ├─ _join_operator_named_device   second port of a named device
    #        ├─ _refresh_matched_device   update an existing device
    #        └─ _create_discovered_device create a device we own
    #
    # Behaviour is pinned by tests/test_sync_devices.py (unit, per rule) and
    # tests/test_sync_devices_fake_netbox.py (real pynetbox against an
    # in-memory NetBox). The 2026-10 readability refactor that produced this
    # layout was proven request-for-request identical to the previous single
    # 700-line function by a differential fuzz (20k random batches incl.
    # injected API failures) — keep that bar for any change here.
    #
    # Performance (B3): reads are batched, writes are not. Every device / IP
    # this batch writes goes through ONE live pynetbox Record per id
    # (``_device_record`` / ``_ip_record``), built from the listing rows — an
    # unchanged device costs zero requests instead of 3 GETs. Custom fields
    # are PATCHed delta-only (``_save_cf_delta``) so a Record read at batch
    # start can't revert another sync's newer value. Proven against the
    # previous version by the same fuzz: identical return value and final
    # NetBox state for 20k batches.
    # ══════════════════════════════════════════════════════════════════════

    def sync_devices(self, devices: list, tenant_slug: str = "",
                     replace: bool = False,
                     defaults: Optional[Dict[str, Any]] = None,
                     source: str = "opnsense",
                     source_of_truth: str = "external") -> Dict[str, Any]:
        """Push a tenant's discovery-source device set into NetBox DCIM.

        Source = a discovery feed relayed by the hub (OPNsense DHCP leases +
        ARP for the firewall sync; switch/gateway ARP tables for the nw sync).
        Each incoming record ``{ip, mac, hostname, serial}`` is matched to an
        existing device by serial (strongest), then MAC, then primary IPv4,
        then bare hostname; missing devices are created (mirroring
        ``claim_device``: tenant-owned device + ``mgmt`` interface + IP with
        ``custom_fields.mac_address`` + ``primary_ip4``). Writing the MAC onto
        the IP record feeds the NetBox→CPPM endpoint sync (which keys on
        ``mac_address``) — so static-IP devices the ARP table sees start flowing
        to ClearPass too.

        ``source`` is the ownership tag stamped onto created devices'
        ``custom_fields.discovered_from`` AND the scope key for replace-delete:
        ``"opnsense"`` / ``"fw"`` / ``"firewall"`` all normalize to the legacy
        ``"opnsense"`` tag (unchanged firewall behavior); any other value (e.g.
        the nw sync's ``"Network Devices"``) is used verbatim, so nw-created
        records are tagged ``Network Devices`` and replace-delete only ever
        touches nw-owned records — never the firewall's ``opnsense``-tagged
        ones, even within the same tenant.

        Ownership / replace-delete: devices this sync CREATES are tagged
        ``custom_fields.discovered_from = <source>`` (best-effort; mirrors
        ``proxmox_unique_id`` on VMs). When ``replace`` AND a tenant slug are
        provided, tagged devices of that tenant whose primary IP is absent from
        the incoming set are deleted. Pre-existing devices matched by IP are
        refreshed (MAC/dns_name on the IP) but NOT tagged and NOT deleted — we
        don't own them. Replace-delete is skipped when unscoped (global) so a
        global sync can't delete another tenant's records. If the
        ``discovered_from`` custom field isn't configured in NetBox the tag
        write is silently skipped and replace-delete becomes a safe no-op.

        ``source_of_truth="netbox"`` makes NetBox authoritative for devices
        that already exist: only EMPTY values are filled (c245c7e) — see
        ``_refresh_matched_device``.

        Returns ``{status, pushed, errors, skipped, deleted, devices_total, message}``.
        """
        run = _DeviceSyncRun(tenant_slug=tenant_slug,
                             source_tag=_normalize_source_tag(source),
                             source_of_truth=source_of_truth,
                             defaults=defaults or {})
        try:
            # Self-heal custom fields (discovered_from on dcim.device,
            # mac_address on ipam.ipaddress) so the ownership tag + MAC writes
            # below land. Cached per-process; no-op once provisioned.
            self._ensure_custom_fields()
            if tenant_slug:
                run.tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if not run.tenant:
                    # Refuse rather than create unattributed records.
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — "
                                       f"firewall-discovered devices not attributed. "
                                       f"Check the tenant's NetBox slug mapping.",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "devices_total": len(devices or [])}

            incoming, run.skipped = self._normalize_incoming_devices(devices)

            try:
                rows, all_rows = self._list_devices_for_sync(run)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": run.skipped, "deleted": 0,
                        "devices_total": len(incoming)}
            self._index_sync_rows(run, rows)
            run.device_rows = {r["id"]: r for r in (all_rows or rows) if r.get("id") is not None}

            # MAC and serial are GLOBAL identities: a device is one device no
            # matter which tenant first recorded it. A tenant-scoped listing
            # can't see another tenant's record of the same MAC, so every sync
            # from a second tenant minted a copy (73 RA/LRB device-<mac> pairs,
            # 2d95909). Index the rest of NetBox by MAC/serial too; tenant-local
            # rows still win, and replace-delete stays tenant-scoped (owned_ips).
            if all_rows is not None:
                self._add_global_identities(run.by_mac, run.by_serial, rows=all_rows)

            if replace and tenant_slug:
                self._delete_vanished_owned_devices(run, incoming)

            self._prefetch_primary_ips(run, incoming)
            self._resolve_create_defaults(run)

            for key, rec in incoming.items():
                try:
                    self._sync_one_device(run, _IncomingDevice(key, rec))
                except Exception as e:
                    self._note_upsert_failure(run, key, e)

            return self._sync_devices_result(run, len(incoming))
        except Exception as e:
            logger.error("sync_devices failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": run.pushed,
                    "errors": run.errors, "skipped": run.skipped, "deleted": run.deleted,
                    "devices_total": len(devices or [])}

    # ── step 1: normalize the feed ───────────────────────────────────────────

    def _normalize_incoming_devices(self, devices) -> Tuple[Dict[str, Dict[str, Any]], int]:
        """Turn raw feed rows into ``({key: record}, skipped)``.

        ``key`` is the record's IPv4 (mask stripped) when it has one, else a
        synthetic key — ``mac:<mac>``, ``host:<hostname>`` or
        ``serial:<serial>`` — so IP-less records still flow through. A later
        row with the same key replaces an earlier one (dict semantics).
        Non-dict rows are ignored; rows with nothing identifiable (no IP, MAC,
        usable hostname or serial) are counted as ``skipped``."""
        incoming: Dict[str, Dict[str, Any]] = {}
        skipped = 0
        for dev in (devices or []):
            if not isinstance(dev, dict):
                continue
            key = self._incoming_device_key(dev)
            if key is None:
                skipped += 1  # nothing identifiable — don't add a phantom row
                continue
            incoming[key] = self._incoming_device_record(dev)
        return incoming, skipped

    def _incoming_device_key(self, dev: dict) -> Optional[str]:
        """The incoming-set key for one feed row (see ``_normalize_incoming_devices``)."""
        ip = str(dev.get("ip") or "").strip().split("/")[0].strip()
        if ip:
            return ip
        mac = self._norm_mac(dev.get("mac", ""))
        if mac:
            # MAC-only: created like any other record, but replace-delete
            # (which keys on IP) won't track it.
            return f"mac:{mac}"
        hostname = str(dev.get("hostname") or "").strip()
        if hostname and hostname.lower() != "unknown":
            # Hostname-only (no IP, no MAC): the hostname tier can adopt a
            # same-name device instead of duplicate-creating. The firewall path
            # drops no-IP records upstream, so this is mostly the nw/console case.
            return f"host:{hostname.lower()}"
        serial = str(dev.get("serial") or "").strip()
        if serial:
            # Serial-only (console-identified, nothing else): the serial tier
            # adopts an existing device instead of creating a phantom.
            return f"serial:{serial.lower()}"
        return None

    def _incoming_device_record(self, dev: dict) -> Dict[str, Any]:
        """The normalized record for one feed row. Optional fields are present
        only when the feed supplied a non-empty value:

        * ``serial`` — strongest, globally-unique match key (tried first).
        * ``hostname_source`` — ``dns``/``reservation`` are operator assertions
          (one name = one device); ``lease`` is device-chosen.
        * ``switch_name``/``switch_ip``/``switch_port`` — MAC-sighting
          enrichment ("last seen on switch X port Y"), from ``source_switch_*``.
        * ``model``/``manufacturer`` — discovered hardware, used to place a NEW
          device under its real device_type instead of 'discovered'.
        * ``role`` — per-record device role, applied on CREATE only.
        * ``custom_fields`` — free-form per-record fields (TrueNAS product/
          version/…), stringified; only provisioned names are written later
          (``_device_cf_whitelist``)."""
        rec: Dict[str, Any] = {"mac": self._norm_mac(dev.get("mac", "")),
                               "hostname": str(dev.get("hostname") or "").strip()}
        serial = str(dev.get("serial") or "").strip()
        if serial:
            rec["serial"] = serial
        h_src = str(dev.get("hostname_source") or "").strip().lower()
        if h_src:
            rec["hostname_source"] = h_src
        for rec_key, feed_key in (("switch_name", "source_switch_name"),
                                  ("switch_ip", "source_switch_ip"),
                                  ("switch_port", "source_switch_port")):
            val = str(dev.get(feed_key) or "").strip()
            if val:
                rec[rec_key] = val
        model = str(dev.get("model") or "").strip()
        if model:
            rec["model"] = model
        manufacturer = str(dev.get("manufacturer") or dev.get("vendor") or "").strip()
        if manufacturer:
            rec["manufacturer"] = manufacturer
        role = str(dev.get("role") or "").strip()
        if role:
            rec["role"] = role
        rec_cfs = dev.get("custom_fields")
        if isinstance(rec_cfs, dict) and rec_cfs:
            rec["custom_fields"] = {str(k): ("" if v is None else str(v))
                                    for k, v in rec_cfs.items() if k}
        return rec

    # ── step 2: index what NetBox already has ────────────────────────────────

    def _index_sync_rows(self, run: "_DeviceSyncRun", rows: list) -> None:
        """Build the pre-batch lookup indexes from the device listing.

        ``by_name``/``by_mac``/``by_serial``: first row wins. ``by_name``
        includes devices WITHOUT a primary IP — their name can still collide on
        the (name, site, tenant) unique constraint. ``by_ip``: LAST row wins
        (historical behaviour, kept). ``owned_ips``: primary IPs of devices
        tagged with this sync's ``discovered_from`` — the only candidates
        replace-delete may remove."""
        for row in rows:
            rname = str(row.get("name") or "").strip().lower()
            if rname:
                run.by_name.setdefault(rname, row)
            cf = row.get("custom_fields") or {}
            # MAC index: catches DHCP IP-moves + MAC-only records.
            rmac = self._norm_mac(cf.get("mac_address", ""))
            if rmac:
                run.by_mac.setdefault(rmac, row)
            # Serial index: survives IP/hostname churn.
            rserial = str(row.get("serial") or "").strip()
            if rserial:
                run.by_serial.setdefault(rserial.lower(), row)
            addr = _primary_addr(row)
            if not addr:
                continue
            run.by_ip[addr] = row
            if run.owns(cf):
                run.owned_ips.add(addr)

    def _list_devices_for_sync(self, run: "_DeviceSyncRun") -> Tuple[list, Optional[list]]:
        """Return ``(rows, all_rows)``: the devices this sync indexes, and the
        global listing for ``_add_global_identities`` (None when not needed or
        unavailable). Raises when no listing can be read.

        A tenant-scoped sync used to page NetBox twice — the tenant's devices,
        then EVERY device for the global MAC/serial index (2d95909). Now it
        pages everything once and keeps the tenant's rows in listing order
        (same order NetBox's ``?tenant=`` filter returns, so first-row-wins
        indexing is unchanged). If the global listing fails it falls back to
        the old tenant-scoped listing without global identities — exactly what
        the old code ended up with when its global listing failed."""
        if not run.tenant_slug:
            return self._api_get_all("/api/dcim/devices/", {"limit": 500}), None
        try:
            all_rows = self._api_get_all("/api/dcim/devices/", {"limit": 500})
        except Exception as e:
            logger.debug("global MAC/serial index: %s", e)
            return self._api_get_all("/api/dcim/devices/",
                                     {"limit": 500, "tenant": run.tenant_slug}), None
        rows = [r for r in all_rows
                if isinstance(r.get("tenant"), dict)
                and r["tenant"].get("slug") == run.tenant_slug]
        return rows, all_rows

    def _prefetch_primary_ips(self, run: "_DeviceSyncRun", incoming: dict) -> None:
        """Batch-read the primary IPs of every device a record COULD match
        (its serial / MAC / IP / name index hits), ~100 ids per request, into
        ``run.ip_rows``. Replaces one ``ip_addresses.get`` per matched device
        (``_refresh_primary_ip`` / ``_fill_device_gaps``). A superset is fine:
        unused rows are never written. Runs AFTER replace-delete so deleted
        devices' IPs (cascade-deleted by NetBox) aren't prefetched.
        Best-effort: a failed chunk just leaves those ids to the per-id GET."""
        ids: list = []
        for key, rec in incoming.items():
            d = _IncomingDevice(key, rec)
            for row in (run.by_serial.get(d.serial.lower()) if d.serial else None,
                        run.by_mac.get(d.mac) if d.mac else None,
                        run.by_ip.get(d.key) if d.keyed_by_ip else None,
                        run.by_name.get(d.hostname.lower()) if d.hostname else None):
                pip = (row or {}).get("primary_ip4")
                if (isinstance(pip, dict) and pip.get("id") is not None
                        and row["id"] not in run.deleted_ids):
                    ids.append(pip["id"])
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                for row in self._api_get_all("/api/ipam/ip-addresses/",
                                             {"id": chunk, "limit": 500}):
                    run.ip_rows[row["id"]] = row
            except Exception as e:
                logger.debug("sync_devices: IP prefetch of %d ids failed: %s", len(chunk), e)

    def _device_record(self, run: "_DeviceSyncRun", dev_id: Any):
        """The batch's live pynetbox Record for device ``dev_id``; None when it
        was deleted this batch or NetBox no longer has it.

        First use builds it from the listing row (``_hydrate_record``, no
        GET); a device that wasn't listed is fetched once. Later uses return
        the SAME Record — its ``save()`` folds the PATCH response back in, so
        it always reflects this batch's writes. Stands in for the
        ``devices.get(id)`` that used to precede every write."""
        if dev_id in run.deleted_ids:
            return None
        obj = run.device_objs.get(dev_id)
        if obj is None:
            row = run.device_rows.pop(dev_id, None)
            obj = (self._hydrate_record(row, self.nb.dcim.devices) if row is not None
                   else self.nb.dcim.devices.get(dev_id))
            if obj is not None:
                run.device_objs[dev_id] = obj
        return obj

    def _ip_record(self, run: "_DeviceSyncRun", ip_id: Any):
        """Same as ``_device_record`` for an IP address: built from the
        prefetched row, else fetched once; None when NetBox has no such IP."""
        obj = run.ip_objs.get(ip_id)
        if obj is None:
            row = run.ip_rows.pop(ip_id, None)
            obj = (self._hydrate_record(row, self.nb.ipam.ip_addresses) if row is not None
                   else self.nb.ipam.ip_addresses.get(ip_id))
            if obj is not None:
                run.ip_objs[ip_id] = obj
        return obj

    def _delete_device(self, run: "_DeviceSyncRun", row: dict) -> bool:
        """Delete a listed device; True iff this call deleted it. Raises on a
        failed delete.

        A device that is already gone (deleted earlier this batch, or by
        someone else since the listing → 404) is a silent no-op, matching the
        old ``get() → None → skip``. NetBox cascades the delete to the
        device's interfaces and their IPs, so its primary IP is forgotten."""
        obj = self._device_record(run, row["id"])
        if obj is None:
            return False
        try:
            obj.delete()
            deleted_now = True
        except pynetbox.RequestError as e:
            if getattr(getattr(e, "req", None), "status_code", None) != 404:
                raise
            deleted_now = False  # someone else already deleted it
        run.deleted_ids.add(row["id"])
        run.device_objs.pop(row["id"], None)
        pip = row.get("primary_ip4")
        if isinstance(pip, dict):
            run.forget_ip(pip.get("id"))
        return deleted_now

    # ── step 3: replace-delete ───────────────────────────────────────────────

    def _delete_vanished_owned_devices(self, run: "_DeviceSyncRun", incoming: dict) -> None:
        """``replace=True`` (tenant-scoped only — the caller checks): delete
        devices THIS source created (``owned_ips``) whose primary IP is absent
        from the incoming set. Human / other-source devices are never touched.
        A failed delete is counted as an error, not raised."""
        for ip_str in list(run.owned_ips - set(incoming.keys())):
            row = run.by_ip.get(ip_str)
            if not row:
                continue
            try:
                if self._delete_device(run, row):
                    run.deleted += 1
            except Exception as e:
                run.note_error(f"delete {ip_str}: {e}")
                logger.debug("sync_devices: delete stale %s failed: %s", ip_str, e)

    # ── step 4: what a CREATE needs ──────────────────────────────────────────

    def _resolve_create_defaults(self, run: "_DeviceSyncRun") -> None:
        """Resolve the batch-wide refs a device CREATE needs (each
        auto-provisions when missing — 406ffd0) and the custom-field
        whitelist. Order matters only for request order: role, device type,
        site."""
        run.role = self._ensure_device_role(run.defaults.get("role") or "discovered")
        # Names of custom fields provisioned on dcim.device — the only ones a
        # per-record custom_fields map may write (an unknown key would 400 the
        # whole device upsert — 268f7ed).
        run.cf_whitelist = self._device_cf_whitelist()
        run.dtype = self._ensure_device_type(run.defaults.get("device_type") or "discovered")
        run.site = self._resolve_site(run.defaults.get("site") or "", run.tenant)

    # ── step 5: one record ───────────────────────────────────────────────────

    def _sync_one_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice") -> None:
        """Reconcile one incoming record: update the device it matches, else
        attach it to an operator-named device as a second port, else create a
        device we own. Exceptions propagate to ``sync_devices``, which counts
        them (``_note_upsert_failure``)."""
        row = self._match_existing_device(run, d)
        if row is None and self._join_operator_named_device(run, d):
            run.pushed += 1
            return
        if row:
            self._refresh_matched_device(run, d, row)
        else:
            self._create_discovered_device(run, d)

    def _match_existing_device(self, run: "_DeviceSyncRun",
                               d: "_IncomingDevice") -> Optional[dict]:
        """Find the existing device this record IS (f5a57b9 / 228c875).

        A record is the same machine if its SERIAL, MAC, IP or hostname
        matches — matching any one updates that device in place instead of
        creating a duplicate (the bug where a device added with no MAC/IP
        spawned a copy on every sync). Tiers, strongest first:

        1. serial — globally unique, survives IP/hostname churn;
        2. MAC — the other hardware identity (catches DHCP IP-moves and
           MAC-only records);
        3. primary IP — only for records keyed by a real IP;
        4. hostname — see ``_adoptable_by_name``.
        """
        row = None
        if d.serial:
            row = run.by_serial.get(d.serial.lower())
        if row is None and d.mac:
            row = run.by_mac.get(d.mac)
        if row is None and d.keyed_by_ip:
            row = run.by_ip.get(d.key)
        if row is None and d.has_hostname:
            row = self._adoptable_by_name(run, d)
        return row

    def _adoptable_by_name(self, run: "_DeviceSyncRun",
                           d: "_IncomingDevice") -> Optional[dict]:
        """Hostname tier: adopt the same-name device unless the record is
        PROVABLY a different machine (MAC and IP present on both sides and
        both differ — the one case the registry allows a duplicate). Only a
        bare placeholder (no IP, no MAC) or a device THIS source owns is
        adopted — a human's device is never clobbered — and never one already
        refreshed this batch (``refreshed_ids``)."""
        cand = run.by_name.get(d.hostname.lower())
        if cand is None or cand["id"] in run.refreshed_ids:
            return None
        ecf = cand.get("custom_fields") or {}
        emac = self._norm_mac(ecf.get("mac_address", ""))
        eaddr = _primary_addr(cand)
        cand_bare = not emac and not eaddr
        mac_differs = bool(d.mac) and bool(emac) and emac != d.mac
        ip_differs = bool(d.real_ip) and bool(eaddr) and eaddr != d.real_ip
        if (cand_bare or run.owns(ecf)) and not (mac_differs and ip_differs):
            return cand
        return None

    def _join_operator_named_device(self, run: "_DeviceSyncRun",
                                    d: "_IncomingDevice") -> bool:
        """An operator-assigned name (DNS A record / DHCP reservation) on a
        new MAC+IP is the SAME device's other port (stack OOBM / in-band —
        bc7575a). Attach the address to that device — one created earlier this
        batch, else an existing device this source owns — instead of minting
        a suffixed copy. True iff attached."""
        if not (d.operator_named and d.real_ip and d.hostname):
            return False
        owner = run.created_by_name.get(d.hostname.lower())
        if owner is None:
            cand = run.by_name.get(d.hostname.lower())
            if cand is not None and run.owns(cand.get("custom_fields") or {}):
                owner = cand["id"]
        if owner is None:
            return False
        touched: set = set()
        try:
            return self._adopt_secondary_ip(owner, d.real_ip, d.mac, d.hostname,
                                            run.tenant, run.source_tag, touched_ips=touched)
        finally:
            for ip_id in touched:
                run.forget_ip(ip_id)

    # ── step 5a: update an existing device ───────────────────────────────────

    def _refresh_matched_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                                row: dict) -> None:
        """Update a matched device in place, then count it as pushed.

        Its id joins ``refreshed_ids`` and its name ``used_names`` so the
        create branch never reclaims/clobbers it when a duplicate hostname
        shows up later in the batch.

        * ``source_of_truth == "netbox"`` (c245c7e): NetBox owns the data —
          only fill EMPTY dns_name / mac / placeholder name, then refresh
          ``last_seen`` (a staleness signal, not a truth field).
        * ``"external"`` (the feed is the truth): refresh/assign the primary
          IP, stamp device custom fields, rename when allowed, mark seen.
        """
        run.refreshed_ids.add(row["id"])
        rname = str(row.get("name") or "").strip().lower()
        if rname:
            run.used_names.add(rname)
        if run.source_of_truth == "netbox":
            self._fill_device_gaps(run, row, d.hostname, d.mac)
            self._touch_last_seen_if_stale(run, row, d.key, "last_seen refresh")
        else:
            self._sync_matched_primary_ip(run, d, row)
            self._stamp_matched_device_fields(run, d, row)
            self._rename_or_touch_matched_device(run, d, row)
        run.pushed += 1

    def _touch_last_seen_if_stale(self, run: "_DeviceSyncRun", row: dict, key: str,
                                  what: str) -> None:
        """Refresh the device's ``last_seen`` — but only when the LISTED row's
        stamp is ≥1h old (8095627 / perf FIX A): a fresh stamp skips the
        per-device GET + PATCH entirely. Best-effort."""
        try:
            if self._last_seen_stale((row.get("custom_fields") or {}).get("last_seen")):
                devobj = self._device_record(run, row["id"])
                if devobj:
                    self._stamp_last_seen(devobj)
        except Exception as e:
            run.forget_device(row["id"])
            logger.debug("sync_devices: %s %s: %s", what, key, e)

    def _sync_matched_primary_ip(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                                 row: dict) -> None:
        """Refresh the existing primary IP when it already equals the incoming
        IP (or the record has no IP to assign); otherwise ASSIGN the incoming
        IP as primary_ip4 (bare-device adoption, or a DHCP IP-move caught by
        MAC match)."""
        pip = row.get("primary_ip4") or {}
        ip_id = pip.get("id") if isinstance(pip, dict) else None
        match_addr = _primary_addr(row)
        if ip_id and (not d.real_ip or match_addr == d.real_ip):
            self._refresh_primary_ip(run, d, ip_id)
        elif d.real_ip:
            self._assign_primary_ip(run, d, row, match_addr)

    def _refresh_primary_ip(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                            ip_id: Any) -> None:
        """Write the record's MAC (``custom_fields.mac_address`` — what the
        NetBox→CPPM endpoint sync keys on) and hostname (``dns_name``) onto
        the device's existing primary IP, then mark it seen. Only changed
        values are sent; an unchanged IP costs no request. Best-effort."""
        try:
            ipobj = self._ip_record(run, ip_id)
            if ipobj:
                if d.has_hostname:
                    ipobj.dns_name = d.hostname
                if d.mac and (ipobj.custom_fields or {}).get("mac_address") != d.mac:
                    self._save_cf_delta(ipobj, {"mac_address": d.mac})
                else:
                    ipobj.save()  # no-op unless dns_name changed
                self._stamp_last_seen(ipobj)
        except Exception as e:
            run.forget_ip(ip_id)
            logger.debug("sync_devices: refresh IP %s failed: %s", d.key, e)

    def _assign_primary_ip(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                           row: dict, match_addr: str) -> None:
        """Give a matched device the incoming IP as its primary_ip4 (new
        ``mgmt`` interface + reused-or-created IP). Best-effort."""
        try:
            ipobj, mask = self._attach_mgmt_ip(run, d, row["id"])
            devobj = self._device_record(run, row["id"])
            if devobj:
                devobj.primary_ip4 = ipobj.id
                devobj.save()
            self._journal("ipam.ipaddress", ipobj.id, run.source_tag,
                          note=f"IP {d.real_ip}/{mask} → adopted")
            self._stamp_last_seen(ipobj)
            # The device moved to a new address — it is NOT stale. Stop
            # replace-delete from reaping it for its OLD primary IP.
            if match_addr and match_addr != d.real_ip and match_addr in run.owned_ips:
                run.owned_ips.discard(match_addr)
        except Exception as e:
            run.forget_device(row["id"])
            logger.debug("sync_devices: assign IP %s failed: %s", d.real_ip, e)

    def _attach_mgmt_ip(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                        device_id: Any) -> Tuple[Any, str]:
        """Create a ``mgmt`` interface on ``device_id`` and put the record's IP
        on it; returns ``(ipobj, mask)``. Raises on failure.

        Uses the top-level ``dcim.interfaces`` endpoint (``device=<id>``), not
        ``devobj.interfaces.create`` — the nested accessor isn't supported on
        every pynetbox version. The IP goes through ``_reuse_or_create_ip``:
        NetBox enforces global IP uniqueness, so an address the IPAM already
        holds is REUSED and reassigned instead of 400ing "Duplicate IP address
        found in global table" (which used to fail ~every record)."""
        iface = self.nb.dcim.interfaces.create(device=device_id, name="mgmt", type="other")
        mask = self._mask_for_ip(d.real_ip, run.tenant)
        ip_kwargs: Dict[str, Any] = {
            "address": f"{d.real_ip}/{mask}",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface.id,
        }
        if run.tenant:
            ip_kwargs["tenant"] = run.tenant.id
        if d.has_hostname:
            ip_kwargs["dns_name"] = d.hostname
        ipobj = self._reuse_or_create_ip(
            f"{d.real_ip}/{mask}", ip_kwargs, d.real_ip, iface.id,
            tenant=run.tenant, hostname=d.hostname, mac=d.mac, source=run.source_tag)
        # A REUSED address was reassigned/tagged by its own lookup — drop any
        # cached copy (it may be another listed device's primary IP).
        run.forget_ip(ipobj.id)
        return ipobj, mask

    def _stamp_matched_device_fields(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                                     row: dict) -> None:
        """Stamp the matched device's custom fields; one save when anything
        changed. Best-effort.

        * ``mac_address`` — so a future MAC-match finds it after a DHCP IP
          move, and the endpoint sync can match it by MAC.
        * ``switch_name/ip/port`` — "where is this MAC" lives on the device.
        * per-record custom fields — only on a device THIS source owns (never a
          human's), only whitelisted names, never a sink-managed field
          (268f7ed).
        * ``serial`` — fill-gap only; an existing serial stays authoritative.
        """
        we_own = run.owns(row.get("custom_fields") or {})
        try:
            devobj = self._device_record(run, row["id"])
            if devobj:
                current = devobj.custom_fields or {}
                wanted: Dict[str, Any] = {}
                if d.mac:
                    wanted["mac_address"] = d.mac
                for cf_key in _SWITCH_CF_KEYS:
                    if d.rec.get(cf_key):
                        wanted[cf_key] = d.rec[cf_key]
                if we_own:
                    for ck, cv in (d.rec.get("custom_fields") or {}).items():
                        if ck in run.cf_whitelist and ck not in self._SINK_MANAGED_DEVICE_CFS:
                            wanted[ck] = cv
                delta = {k: v for k, v in wanted.items() if current.get(k) != v}
                ser_changed = False
                if d.serial and not str(getattr(devobj, "serial", "") or "").strip():
                    devobj.serial = d.serial
                    ser_changed = True
                if delta:
                    self._save_cf_delta(devobj, delta)  # also sends the serial
                elif ser_changed:
                    devobj.save()
        except Exception as e:
            run.forget_device(row["id"])
            logger.debug("sync_devices: device cf stamp %s: %s", d.key, e)

    def _rename_or_touch_matched_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                                        row: dict) -> None:
        """Rename the matched device to the record's hostname and mark it seen
        — but only when we OWN it, or it still has an auto placeholder name
        another feed minted (nw's ``device-<mac>``); a human's name is never
        clobbered (c245c7e). An owned device takes the hostname as-is
        (upper-cased, f15f672); a placeholder takes a uniquified name.
        Otherwise just refresh ``last_seen`` when stale."""
        we_own = run.owns(row.get("custom_fields") or {})
        placeholder = bool(_PLACEHOLDER_NAME_RE.match(str(row.get("name") or "").strip()))
        if not ((we_own or placeholder) and d.has_hostname):
            self._touch_last_seen_if_stale(run, row, d.key, "last_seen")
            return
        try:
            devobj = self._device_record(run, row["id"])
            if devobj:
                devobj.name = (self._nb_name(d.hostname) if we_own else
                               self._uniq_device_name(d.hostname, d.mac, d.real_ip,
                                                      run.by_name, run.used_names))
                devobj.save()
                run.used_names.add(str(devobj.name).strip().lower())
                self._stamp_last_seen(devobj)
        except Exception as e:
            run.forget_device(row["id"])
            logger.debug("sync_devices: rename %s failed: %s", d.key, e)

    # ── step 5b: create a device we own ──────────────────────────────────────

    def _create_discovered_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice") -> None:
        """Create a device for a record nothing matched, tagged as ours.

        Raises on a failed create (the caller counts it). Steps:
        1. pick a free name (``_claim_create_name``);
        2. create the device (role / device_type / site / tenant / serial);
        3. stamp ownership + ``last_seen`` + MAC + switch + per-record cfs;
        4. with a real IP: ``mgmt`` interface + IP + ``primary_ip4``.
        """
        name = self._claim_create_name(run, d, d.default_name)
        # A prior create already 400'd on a missing site — the same 400 hits
        # every record, so stop attempting creates (count once each, one
        # warning) instead of spamming an identical error (ccb5034).
        if run.site_required_abort:
            run.errors += 1
            return
        name = self._nb_name(name)
        devobj = self.nb.dcim.devices.create(**self._device_create_kwargs(run, d, name))
        if d.hostname:
            run.created_by_name.setdefault(d.hostname.lower(), devobj.id)
        self._stamp_created_device(run, d, devobj)
        self._journal("dcim.device", devobj.id, run.source_tag, note=f"device {name}")
        if d.real_ip:
            ipobj, mask = self._attach_mgmt_ip(run, d, devobj.id)
            # Set primary_ip4 on a FRESH fetch so the save does not re-send the
            # best-effort custom_fields merged onto ``devobj`` above. When the
            # deployed NetBox hasn't attached those custom fields to
            # dcim.device yet (a provisioning gap — _ensure_custom_fields
            # self-heals it, but there's a first-sync window + a restricted
            # token can't create them), re-sending them 400s ("Custom field 'X'
            # does not exist for this object type") and used to kill the whole
            # upsert: the device was created but primary_ip4 was never set, so
            # the next sync couldn't match it → re-create + 400 every cycle
            # (the persistent 100+ errors/tenant in the hub log). A fresh get
            # carries only NetBox's actually-provisioned cfs. Best-effort: the
            # device still counts as pushed.
            try:
                fresh = self.nb.dcim.devices.get(devobj.id)
                if fresh:
                    fresh.primary_ip4 = ipobj.id
                    fresh.save()
            except Exception as e:
                logger.debug("sync_devices: set primary_ip4 %s on %s failed: %s",
                             d.real_ip, name, e)
            self._journal("ipam.ipaddress", ipobj.id, run.source_tag,
                          note=f"IP {d.real_ip}/{mask} → {name}")
            self._stamp_last_seen(ipobj)
        run.pushed += 1

    def _claim_create_name(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                           name: str) -> str:
        """Return a name the new device can take without tripping NetBox's
        (name, site, tenant) unique constraint, and reserve it in
        ``used_names``.

        Two hazards: (1) a PRE-existing device already holds the name
        (``by_name`` snapshot); (2) an earlier record THIS batch took it — many
        discovery records share a hostname (ks205, sonoszp, iphone…) across
        distinct MACs, and the pre-batch snapshot can't see those.

        For (1): when the holder is OUR orphan of the same machine
        (``_is_reclaimable``) and the name isn't already used this batch, the
        holder is deleted and the name reclaimed; otherwise the new name is
        uniquified and the holder left alone. A device refreshed this batch
        (``refreshed_ids``) is never reclaimed — that is a different IP's
        real device, not a stale orphan."""
        byname = run.by_name.get(name.lower())
        if byname and byname["id"] not in run.refreshed_ids:
            if (self._is_reclaimable(run, d, byname, name)
                    and name.lower() not in run.used_names):
                try:
                    if self._delete_device(run, byname):
                        run.deleted += 1
                except Exception as e:
                    run.note_error(f"delete-stale {name}: {e}")
                    logger.debug("sync_devices: delete stale by-name %s failed: %s", name, e)
            else:
                orig = name
                name = self._uniq_device_name(name, d.mac, d.real_ip,
                                              run.by_name, run.used_names)
                logger.debug("sync_devices: name %s taken; creating as %s", orig, name)
        elif name.lower() in run.used_names:
            orig = name
            name = self._uniq_device_name(name, d.mac, d.real_ip,
                                          run.by_name, run.used_names)
            logger.debug("sync_devices: hostname %s already used this batch; "
                         "creating as %s", orig, name)
        run.used_names.add(name.lower())
        return name

    def _is_reclaimable(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                        holder: dict, name: str) -> bool:
        """True when the device holding ``name`` is OUR stale copy of the same
        machine: we own it (an unowned/human device never qualifies) AND its
        MAC cf equals the record's MAC, OR its primary IP equals the record's
        IP, OR the name is the MAC-derived ``device-<mac>`` form (a no-hostname
        DHCP IP-move of one of our orphans)."""
        hcf = holder.get("custom_fields") or {}
        if not run.owns(hcf):
            return False
        hmac = self._norm_mac(hcf.get("mac_address", ""))
        haddr = _primary_addr(holder)
        return ((bool(d.mac) and bool(hmac) and hmac == d.mac)
                or (bool(d.real_ip) and bool(haddr) and haddr == d.real_ip)
                or (bool(d.mac) and name.lower() == f"device-{d.mac.replace(':', '')}"))

    def _device_create_kwargs(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                              name: str) -> Dict[str, Any]:
        """``dcim.devices.create`` kwargs for a new device. A per-record role
        (e.g. TrueNAS→"storage") beats the batch default — role is set on
        CREATE only, never on an existing device (268f7ed). A device_type
        resolved from the DISCOVERED model beats the generic default."""
        kwargs: Dict[str, Any] = {"name": name, "status": "active"}
        if d.serial:
            kwargs["serial"] = d.serial
        rec_role = d.rec.get("role")
        create_role = self._ensure_device_role(rec_role) if rec_role else run.role
        if create_role:
            kwargs["role"] = create_role.id
        rec_dtype = self._device_type_for_model(
            d.rec.get("model", ""), d.rec.get("manufacturer", "")) or run.dtype
        if rec_dtype:
            kwargs["device_type"] = rec_dtype.id
        if run.site:
            kwargs["site"] = run.site.id
        if run.tenant:
            kwargs["tenant"] = run.tenant.id
        return kwargs

    def _stamp_created_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                              devobj: Any) -> None:
        """Stamp a NEW device: ownership tag (``discovered_from``), ``last_seen``
        (starts the staleness clock), ``mac_address`` + switch cfs (so a
        recurrence MAC-matches this device instead of duplicating — the
        linchpin of the registry dedup) and whitelisted per-record cfs.
        Best-effort: a missing custom field just skips the stamp."""
        try:
            merged = dict(devobj.custom_fields or {})
            merged["discovered_from"] = run.source_tag
            merged["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if d.mac:
                merged["mac_address"] = d.mac
            for cf_key in _SWITCH_CF_KEYS:
                val = d.rec.get(cf_key)
                if val:
                    merged[cf_key] = val
            for ck, cv in (d.rec.get("custom_fields") or {}).items():
                if ck in run.cf_whitelist and ck not in self._SINK_MANAGED_DEVICE_CFS:
                    merged[ck] = cv
            devobj.custom_fields = merged
            devobj.save()
        except Exception as e:
            logger.debug("sync_devices: discovered_from tag skipped: %s", e)

    # ── step 6: errors + result ──────────────────────────────────────────────

    def _note_upsert_failure(self, run: "_DeviceSyncRun", key: str, e: Exception) -> None:
        """Count a failed record. If it failed because NetBox requires a site
        (none resolvable), the same 400 will hit every remaining create — set
        ``site_required_abort`` so later creates are skipped, and log ONE
        actionable warning instead of 185 identical errors (ccb5034)."""
        run.note_error(f"upsert {key}: {e}")
        es = str(e).lower()
        if not run.site_required_abort and "site" in es and "required" in es:
            run.site_required_abort = True
            logger.warning(
                "[sync-error] sync_devices tenant=%s: NetBox requires a "
                "site for device creation but none resolved (defaults.site="
                "%r) — set a valid 'site' slug for this sync or create a "
                "site in NetBox; skipping remaining creates.",
                run.tenant_slug or "—", run.defaults.get("site"))
        logger.debug("sync_devices: upsert %s failed: %s", key, e)

    @staticmethod
    def _sync_devices_result(run: "_DeviceSyncRun", devices_total: int) -> Dict[str, Any]:
        """The SUCCESS envelope. The first per-record failure is appended to
        the message (the hub status UI shows it) and logged at WARNING, so the
        cause is visible without DEBUG logs."""
        msg = (f"{run.pushed} device(s) upserted, {run.deleted} deleted, "
               f"{run.skipped} skipped, {run.errors} errors")
        if run.errors and run.first_err:
            msg += f" — first error: {run.first_err}"
            logger.warning("sync_devices tenant=%s: %s", run.tenant_slug or "<global>", msg)
        else:
            logger.info("sync_devices tenant=%s: %s", run.tenant_slug or "<global>", msg)
        return {"status": "SUCCESS", "pushed": run.pushed, "errors": run.errors,
                "skipped": run.skipped, "deleted": run.deleted,
                "devices_total": devices_total, "message": msg}

    def sync_nw_device(self, device: dict, interfaces: list,
                       tenant_slug: str = "",
                       defaults: Optional[Dict[str, Any]] = None,
                       source: str = "Network Devices") -> Dict[str, Any]:
        """Upsert ONE polled network device (switch/gateway) as a NetBox
        ``dcim.device`` with its ``dcim.interfaces`` + per-interface IPs.

        Called by the hub's POLL NOW path (``NETBOX_SYNC_NW_DEVICE``) with the
        live SNMP/CLI/REST poll of a single fleet device. This is the
        network-device **inventory** path — distinct from the scheduled
        ARP-neighbor→endpoint ``sync_devices`` flow: here the polled device
        ITSELF becomes the NetBox device, and its learned interfaces become
        ``dcim.interfaces`` (name/MAC/status/speed) with an ``ipam.ip_address``
        per interface IP.

        Match the existing device by ``serial`` (native, hardware truth) first,
        then ``custom_fields.nw_device_id`` (the fleet id), management IP, and
        finally name within the tenant — the shared canonical ladder so a switch
        already in NetBox from another feeder (console serial, ARP IP) is updated
        in place instead of duplicated. Create if missing, tagged
        ``discovered_from=<source>`` + ``nw_device_id=<id>``; set ``primary_ip4``
        from the device management address. Each incoming interface is upserted
        by name (native ``mac_address``, ``status``, ``speed``) with a
        reused/created IP attached. ``replace=True`` deletes interfaces we
        created (``nw_managed`` marker) whose name is absent from the incoming
        set — so NetBox tracks the live switch without touching manually-created
        interfaces.

        ``defaults`` (``device_type``/``role``/``site`` slugs) are required to
        CREATE a device; an existing match needs only an interface refresh. A
        missing default on the create path returns an ERROR naming it (never
        silently fail).

        Returns ``{status, pushed, errors, skipped, deleted, interfaces_total,
        device_id, message}``.
        """
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None
        defaults = defaults or {}
        source_tag = str(source or "Network Devices").strip() or "Network Devices"
        try:
            self._ensure_custom_fields()
            dev = device or {}
            nw_id = str(dev.get("id") or "").strip()
            name = str(dev.get("name") or "").strip()
            serial = str(dev.get("serial") or "").strip()
            base_mac = self._norm_mac(dev.get("mac") or dev.get("base_mac") or "")
            if not re.fullmatch(r"[0-9a-f]{12}", re.sub(r"[:.\-]", "", base_mac)):
                base_mac = ""
            mgmt_ip = str(dev.get("address") or "").strip().split("/")[0].strip()
            if not name and not nw_id:
                return {"status": "ERROR", "message": "nw device has no name/id",
                        "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                        "interfaces_total": len(interfaces or []), "device_id": None}
            if not name:
                name = f"nw-{nw_id[:8]}" if nw_id else "nw-device"

            # Case-insensitive tenant resolve (slug OR name → canonical slug),
            # mirroring sync_vms' lookup so a mixed-case/none tenant still lands.
            tenant = None
            if tenant_slug:
                tenant = self._resolve_tenant_ci(tenant_slug)

            # ── Resolve the existing device via the canonical ladder ──────────
            # SERIAL → MAC → nw_device_id → mgmt-IP → name. Serial and the
            # switch's base MAC (``device.mac`` from the nw info command) are the
            # hardware identities; routing through the shared resolver means a device
            # another feeder created (console by serial, ARP/fw by IP) is found
            # and updated in place instead of spawning an nw_device_id-keyed
            # duplicate of the same box.
            try:
                params: Dict[str, Any] = {"limit": 500}
                if tenant:
                    params["tenant_id"] = tenant.id  # ``tenant`` filters by slug
                rows = self._api_get_all("/api/dcim/devices/", params)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                        "interfaces_total": len(interfaces or []), "device_id": None}
            idx = self._index_existing_devices(rows)
            if tenant:
                self._add_global_identities(idx["by_mac"], idx["by_serial"])
            existing = self._resolve_existing_device(
                serial=serial, mac=base_mac, nw_device_id=nw_id, ip=mgmt_ip,
                hostname=name, by_serial=idx["by_serial"], by_mac=idx["by_mac"],
                by_nw=idx["by_nw"],
                by_ip=idx["by_ip"], by_name=idx["by_name"])

            # ── Create the device if missing (defaults required) ────────────────
            if existing is None:
                dt_slug = str(defaults.get("device_type") or "").strip()
                role_slug = str(defaults.get("role") or "").strip()
                site_slug = str(defaults.get("site") or "").strip()
                missing = [k for k, v in (("device_type", dt_slug),
                                         ("role", role_slug),
                                         ("site", site_slug)) if not v]
                if missing:
                    return {"status": "ERROR",
                            "message": f"cannot create nw device {name!r}: missing "
                                       f"default(s): {', '.join(missing)} "
                                       f"(set them in Setup → Network Devices → "
                                       f"IPAM Sync defaults)",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "interfaces_total": len(interfaces or []), "device_id": None}
                try:
                    dt = self.nb.dcim.device_types.get(slug=dt_slug)
                    # Prefer the device_type resolved from the polled hardware
                    # model (real type, e.g. 'Aruba 2930F') over the generic
                    # default; a resolved model type also satisfies the required
                    # device_type even when the default slug is unset/missing.
                    model_dt = self._device_type_for_model(
                        str(dev.get("model") or "").strip())
                    if model_dt:
                        dt = model_dt
                    # perf-scan D1b: role + site TTL-cached — a fleet POLL NOW
                    # otherwise re-fetches the same two slugs once per device.
                    role = self._cached_ref(
                        "device_role", role_slug,
                        lambda: self.nb.dcim.device_roles.get(slug=role_slug))
                    site = self._cached_ref(
                        "site", site_slug,
                        lambda: self.nb.dcim.sites.get(slug=site_slug))
                    # A configured-but-nonexistent slug resolves to None; guard
                    # so it's a clear, actionable error instead of an
                    # AttributeError on .id.
                    _unresolved = [k for k, v in (("device_type", dt),
                                                  ("role", role),
                                                  ("site", site)) if v is None]
                    if _unresolved:
                        return {"status": "ERROR",
                                "message": f"cannot create nw device {name!r}: "
                                           f"default(s) not found in NetBox: "
                                           f"{', '.join(_unresolved)} "
                                           f"(create them in NetBox or fix the "
                                           f"slug in Setup → Network Devices → "
                                           f"IPAM Sync defaults)",
                                "pushed": 0, "errors": 1, "skipped": 0,
                                "deleted": 0,
                                "interfaces_total": len(interfaces or []),
                                "device_id": None}
                    ck: Dict[str, Any] = {"name": self._nb_name(name), "device_type": dt.id,
                                          "role": role.id, "site": site.id}
                    if serial:
                        ck["serial"] = serial
                    if tenant:
                        ck["tenant"] = tenant.id
                    devobj = self.nb.dcim.devices.create(**ck)
                    # Tag ownership + link to the fleet id (best-effort cf PATCH).
                    try:
                        devobj.custom_fields = {
                            "discovered_from": source_tag,
                            "nw_device_id": nw_id,
                            "last_seen": datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"),
                        }
                        if base_mac:
                            devobj.custom_fields["mac_address"] = base_mac
                        devobj.save()
                    except Exception as e:
                        logger.warning("sync_nw_device: tag new device %s skipped: %s",
                                       nw_id or name, e)
                    self._journal("dcim.device", devobj.id, "nw-poll",
                                  note=f"NW device {name} ({nw_id or 'no id'})")
                except Exception as e:
                    return {"status": "ERROR",
                            "message": f"create nw device {name!r} failed: {e}",
                            "pushed": 0, "errors": 1, "skipped": 0, "deleted": 0,
                            "interfaces_total": len(interfaces or []), "device_id": None}
            else:
                devobj = self.nb.dcim.devices.get(existing["id"])
                if not devobj:
                    return {"status": "ERROR",
                            "message": f"existing nw device {existing['id']} vanished",
                            "pushed": 0, "errors": 1, "skipped": 0, "deleted": 0,
                            "interfaces_total": len(interfaces or []), "device_id": None}
                # Refresh ownership + last_seen (best-effort). perf FIX A:
                # last_seen is quantized — keep a fresh (<1h) stamp so an
                # unchanged device yields a no-diff save (pynetbox skips the
                # PATCH when nothing moved).
                try:
                    cf = dict(devobj.custom_fields or {})
                    cf["discovered_from"] = source_tag
                    if nw_id:
                        cf["nw_device_id"] = nw_id
                    if base_mac and not str(cf.get("mac_address") or "").strip():
                        cf["mac_address"] = base_mac
                    if self._last_seen_stale(cf.get("last_seen")):
                        cf["last_seen"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                    devobj.custom_fields = cf
                    # Backfill the native serial only when the matched device
                    # has none — hardware truth fills a gap, but never clobber a
                    # serial another feeder already set.
                    if serial and not str(getattr(devobj, "serial", "") or "").strip():
                        devobj.serial = serial
                    devobj.save()
                except Exception as e:
                    logger.debug("sync_nw_device: refresh device cf skipped: %s", e)
                # Matched a device another feeder auto-named (DEVICE-<mac> from
                # ARP, or a bare IP): the polled hostname is the real name. Only
                # the fleet's own name counts, never the nw-<id> fallback.
                try:
                    self._adopt_real_name(devobj, str(dev.get("name") or ""),
                                          idx["by_name"], source="nw-poll")
                except Exception as e:
                    logger.debug("sync_nw_device: adopt name skipped: %s", e)

            dev_id = devobj.id

            # ── Index existing interfaces on this device by name (lowercased) ──
            existing_ifaces: Dict[str, Any] = {}
            try:
                for ifc in self.nb.dcim.interfaces.filter(device=dev_id):
                    existing_ifaces[str(getattr(ifc, "name", "") or "").lower()] = ifc
            except Exception as e:
                logger.debug("sync_nw_device: list interfaces for %s failed: %s",
                             dev_id, e)

            incoming_names: set = set()
            for ifc in (interfaces or []):
                if not isinstance(ifc, dict):
                    continue
                ifname = str(ifc.get("name") or "").strip()
                if not ifname:
                    skipped += 1
                    continue
                incoming_names.add(ifname.lower())
                mac = self._norm_mac(ifc.get("mac", ""))
                status = "active" if str(ifc.get("status") or "").strip().lower() in (
                    "up", "active", "1") else "planned"
                speed_val = ifc.get("speed")
                try:
                    sv = int(speed_val) if speed_val not in (None, "") else 0
                except (TypeError, ValueError):
                    sv = 0
                # ifSpeed is bps; NetBox interface speed is Kbps. Convert when the
                # value is clearly bps (>=1e6); else assume already Kbps.
                speed_kbps = sv // 1000 if sv >= 1_000_000 else sv
                ip = str(ifc.get("ip") or "").strip().split("/")[0].strip()
                vlan = str(ifc.get("vlan") or "").strip()
                try:
                    match = existing_ifaces.get(ifname.lower())
                    if match is None:
                        ik: Dict[str, Any] = {"device": dev_id, "name": ifname,
                                              "type": "other", "status": status}
                        if mac:
                            ik["mac_address"] = mac
                        if speed_kbps > 0:
                            ik["speed"] = speed_kbps
                        # VLAN: best-effort tagged vlan (NetBox expects a list of
                        # vlan ids or a single untagged vlan id). We only have a
                        # vlan label/number from the poll; skip unless numeric.
                        iface_obj = self.nb.dcim.interfaces.create(**ik)
                        try:
                            iface_obj.custom_fields = {"nw_managed": "true"}
                            iface_obj.save()
                        except Exception as e:
                            logger.debug("sync_nw_device: tag new iface %s skipped: %s",
                                         ifname, e)
                    else:
                        iface_obj = match
                        changed = False
                        if getattr(iface_obj, "status", None) != status:
                            iface_obj.status = status
                            changed = True
                        if mac and str(getattr(iface_obj, "mac_address", "") or "") != mac:
                            iface_obj.mac_address = mac
                            changed = True
                        if speed_kbps > 0 and int(getattr(iface_obj, "speed", 0) or 0) != speed_kbps:
                            iface_obj.speed = speed_kbps
                            changed = True
                        if changed:
                            iface_obj.save()
                        # Mark an adopted interface as nw-managed (best-effort) so
                        # replace-delete can track it on later polls.
                        try:
                            m = dict(iface_obj.custom_fields or {})
                            if str(m.get("nw_managed") or "") != "true":
                                m["nw_managed"] = "true"
                                iface_obj.custom_fields = m
                                iface_obj.save()
                        except Exception as e:
                            logger.debug("sync_nw_device: mark adopted iface %s: %s",
                                         ifname, e)

                    # Per-interface IP (reuse global record, attach to the iface).
                    if ip:
                        mask = self._mask_for_ip(ip, tenant)
                        ip_kwargs = {
                            "address": f"{ip}/{mask}",
                            "assigned_object_type": "dcim.interface",
                            "assigned_object_id": iface_obj.id,
                        }
                        if tenant:
                            ip_kwargs["tenant"] = tenant.id
                        try:
                            ipobj = self._reuse_or_create_ip(
                                f"{ip}/{mask}", ip_kwargs, ip, iface_obj.id,
                                tenant=tenant, hostname="", mac=mac,
                                source="nw-poll", iface_type="dcim.interface")
                            # Device primary_ip4 from the management address.
                            if mgmt_ip and ip == mgmt_ip and \
                                    not getattr(devobj, "primary_ip4", None):
                                devobj.primary_ip4 = ipobj.id
                                devobj.save()
                            self._stamp_last_seen(ipobj)
                        except Exception as e:
                            errors += 1
                            if first_err is None:
                                first_err = f"iface {ifname} ip {ip}: {e}"
                            logger.debug("sync_nw_device: iface %s ip %s failed: %s",
                                         ifname, ip, e)
                    pushed += 1
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"iface {ifname}: {e}"
                    logger.debug("sync_nw_device: iface %s failed: %s", ifname, e)

            # ── Replace-delete: drop nw-managed interfaces no longer reported ──
            for ifname_l, ifc_obj in list(existing_ifaces.items()):
                    if ifname_l in incoming_names:
                        continue
                    try:
                        cf = dict(ifc_obj.custom_fields or {})
                        if str(cf.get("nw_managed") or "") != "true":
                            continue  # never delete a manually-created interface
                        ifc_obj.delete()
                        deleted += 1
                    except Exception as e:
                        errors += 1
                        if first_err is None:
                            first_err = f"delete iface {ifname_l}: {e}"
                        logger.debug("sync_nw_device: delete stale iface %s failed: %s",
                                     ifname_l, e)

            msg = (f"nw device {name} upserted: {pushed} interface(s), "
                   f"{deleted} deleted, {skipped} skipped, {errors} errors")
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("sync_nw_device: %s", msg)
            else:
                logger.info("sync_nw_device: %s", msg)
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "interfaces_total": len(interfaces or []),
                    "device_id": dev_id, "message": msg}
        except Exception as e:
            logger.error("sync_nw_device failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "interfaces_total": len(interfaces or []), "device_id": None}

    def _resolve_tenant_ci(self, slug: str) -> Any:
        """Case-insensitive tenant resolve: lower(slug) ∪ lower(name) → tenant
        object. Returns None when not found. Mirrors sync_vms' per-batch lookup
        but as a reusable helper. NetBox slugs are conventionally lowercase, but
        a configured tenant_slug may arrive mixed-case or as the display name."""
        s = str(slug or "").strip()
        if not s:
            return None
        try:
            lut: Dict[str, str] = {}
            for t in self._api_get_all("/api/tenancy/tenants/"):
                tslug = str((t or {}).get("slug") or "").strip()
                if not tslug:
                    continue
                lut.setdefault(tslug.lower(), tslug)
                nm = str((t or {}).get("name") or "").strip()
                if nm:
                    lut.setdefault(nm.lower(), tslug)
            canon = lut.get(s.lower()) or s
            return self.nb.tenancy.tenants.get(slug=canon)
        except Exception as e:
            logger.debug("resolve_tenant_ci %s failed: %s", s, e)
            try:
                return self.nb.tenancy.tenants.get(slug=s)
            except Exception:
                return None

    def sync_access_tracker(self, sessions: list, tenant_slug: str = "",
                            defaults: Optional[Dict[str, Any]] = None,
                            source_of_truth: str = "netbox") -> Dict[str, Any]:
        """Pull ClearPass Access Tracker / session data INTO NetBox (NAC→IPAM
        reverse sync; the bidirectional counterpart to ``EndpointSyncMixin``).

        Source = CPPM ``/api/session`` (relayed by the hub realtime loop). Each
        incoming session ``{mac, ip, nas_ip, nas_port, nas_name, username,
        start_time}`` is matched **MAC then framed-IP** against the tenant's
        existing devices (the device's ``custom_fields.mac_address``, else its
        ``primary_ip4``) via the shared canonical resolver — so a device another
        feeder already created (e.g. console/firewall by IP, MAC not yet
        stamped) is reconciled, not duplicated. NetBox
        stays source of truth → this is **only-add-missing**: a MAC already in
        NetBox is skipped (never duplicated, never overwritten), with a
        best-effort ``last_seen``/``switch_ip``/``switch_port`` refresh on
        devices *we* created (``discovered_from == "cppm-access-tracker"``); a
        MAC not in NetBox → a device is created.

        Created endpoint device mirrors ``sync_devices`` (tenant-owned device +
        NIC interface carrying the native MAC + framed IP + ``primary_ip4``),
        tagged ``discovered_from = "cppm-access-tracker"`` with
        ``mac_address``/``switch_ip``/``switch_port``/``last_seen`` custom
        fields. Full switch topology is built best-effort: a switch
        ``dcim.devices`` (role ``switch``) keyed by NAS IP, a port-named
        ``dcim.interfaces`` on it, and a ``dcim.cables`` connection from the
        endpoint NIC to that switch port — idempotent, with a graceful fallback
        to the custom-field record if the cable API differs on the deployed
        NetBox (the device + IP + MAC are still synced).

        ``replace`` is always False here (only-add-missing by design — never
        delete hand-managed NetBox records). Returns ``{status, pushed, errors,
        skipped, deleted, sessions_total, message}``.
        """
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None
        defaults = defaults or {}
        try:
            self._ensure_custom_fields()
            tenant = None
            if tenant_slug:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if not tenant:
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — "
                                       f"access-tracker sessions not attributed.",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "sessions_total": len(sessions or [])}

            role = self._ensure_device_role(defaults.get("role") or "discovered")
            dtype = self._ensure_device_type(defaults.get("device_type") or "discovered")
            site = self._resolve_site(defaults.get("site") or "", tenant)
            switch_role = self._ensure_device_role(defaults.get("switch_role") or "switch")
            switch_dtype = self._ensure_device_type(defaults.get("switch_device_type") or "switch")

            # MAC-first index of the tenant's existing devices (the device's
            # custom_fields.mac_address), plus a primary-IP index (to find the
            # switch by NAS IP) and a name index for uniquification. Reuses the
            # same intra-batch used_names dedup that fixed sync_devices.
            existing_by_mac: Dict[str, dict] = {}
            existing_by_ip: Dict[str, dict] = {}
            existing_by_name: Dict[str, dict] = {}
            used_names: set = set()
            list_params: Dict[str, Any] = {"limit": 500}
            if tenant_slug:
                list_params["tenant"] = tenant_slug
            try:
                rows = self._api_get_all("/api/dcim/devices/", list_params)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": skipped, "deleted": 0,
                        "sessions_total": len(sessions or [])}
            for row in rows:
                rname = str(row.get("name") or "").strip().lower()
                if rname:
                    existing_by_name.setdefault(rname, row)
                cf = row.get("custom_fields") or {}
                mac_cf = self._norm_mac(cf.get("mac_address") or "")
                if mac_cf:
                    existing_by_mac.setdefault(mac_cf, row)
                pip = row.get("primary_ip4")
                addr = ""
                if isinstance(pip, dict):
                    addr = (pip.get("address") or "").split("/")[0].strip()
                if addr:
                    existing_by_ip.setdefault(addr, row)
            if tenant_slug:
                self._add_global_identities(existing_by_mac)

            # Switch-topology caches (this batch): NAS-IP → switch device,
            # (switch.id, nas_port) → port interface.
            switch_by_ip: Dict[str, dict] = {}
            port_iface_by_key: Dict[tuple, Any] = {}

            def _ensure_switch(nas_ip: str, nas_name: str):
                """Get-or-create a switch device by its NAS IP (IP-keyed upsert,
                mirroring sync_devices' IP match). Returns the device row dict or
                None on failure."""
                if not nas_ip:
                    return None
                row = existing_by_ip.get(nas_ip) or switch_by_ip.get(nas_ip)
                if row:
                    switch_by_ip[nas_ip] = row
                    return row
                name = (nas_name or f"switch-{nas_ip}").strip() or f"switch-{nas_ip}"
                name = self._uniq_device_name(name, "", nas_ip, existing_by_name, used_names)
                used_names.add(name.lower())
                ck: Dict[str, Any] = {"name": name, "status": "active"}
                if switch_role:
                    ck["role"] = switch_role.id
                if switch_dtype:
                    ck["device_type"] = switch_dtype.id
                if site:
                    ck["site"] = site.id
                if tenant:
                    ck["tenant"] = tenant.id
                try:
                    sw = self.nb.dcim.devices.create(**ck)
                    # mgmt interface holding the NAS IP + primary_ip4 so the
                    # next batch finds this switch by IP (existing_by_ip path).
                    if sw:
                        try:
                            miface = self.nb.dcim.interfaces.create(
                                device=sw.id, name="mgmt", type="other")
                            mask = self._mask_for_ip(nas_ip, tenant)
                            ipk: Dict[str, Any] = {
                                "address": f"{nas_ip}/{mask}",
                                "assigned_object_type": "dcim.interface",
                                "assigned_object_id": miface.id,
                            }
                            if tenant:
                                ipk["tenant"] = tenant.id
                                _vrf = self._vrf_id_for_tenant(tenant)
                                if _vrf is not None:
                                    ipk["vrf"] = _vrf
                            ipo = self.nb.ipam.ip_addresses.create(**ipk)
                            sw.primary_ip4 = ipo.id
                            sw.save()
                        except Exception as e:
                            logger.debug("sync_access_tracker: switch mgmt/IP %s skipped: %s", nas_ip, e)
                        row = {"id": sw.id, "name": sw.name,
                               "primary_ip4": {"address": f"{nas_ip}/{self._mask_for_ip(nas_ip, tenant)}"}}
                        switch_by_ip[nas_ip] = row
                        existing_by_ip[nas_ip] = row
                        return row
                except Exception as e:
                    logger.debug("sync_access_tracker: create switch %s failed: %s", nas_ip, e)
                return None

            def _ensure_switch_port(switch_row: dict, nas_port: str):
                """Get-or-create the named port interface on the switch."""
                if not switch_row or not nas_port:
                    return None
                key = (switch_row["id"], nas_port)
                cached = port_iface_by_key.get(key)
                if cached:
                    return cached
                try:
                    iface = self.nb.dcim.interfaces.get(
                        device=switch_row["id"], name=nas_port)
                    if iface:
                        port_iface_by_key[key] = iface
                        return iface
                except Exception as e:
                    logger.debug("sync_access_tracker: find port %s failed: %s", nas_port, e)
                try:
                    iface = self.nb.dcim.interfaces.create(
                        device=switch_row["id"], name=nas_port, type="other")
                    port_iface_by_key[key] = iface
                    return iface
                except Exception as e:
                    logger.debug("sync_access_tracker: create port %s failed: %s", nas_port, e)
                return None

            def _cable_nic_to_port(nic, port) -> None:
                """Idempotently cable the endpoint NIC to the switch port. Skips
                if the NIC already has a connected endpoint. On any cable API
                failure (pynetbox/NetBox version mismatch on terminations) →
                WARNING + fall back to the custom-field record already written
                (switch_ip/switch_port on the device). Never raises."""
                try:
                    nic_obj = self.nb.dcim.interfaces.get(nic.id)
                    if nic_obj and self._iface_cable_peer(nic_obj) is not None:
                        return  # already cabled — don't create a second link
                except Exception as e:
                    logger.debug("sync_access_tracker: nic connected_endpoint check: %s", e)
                try:
                    self.nb.dcim.cables.create(
                        a_terminations=[{"object_type": "dcim.interface",
                                         "object_id": nic.id}],
                        b_terminations=[{"object_type": "dcim.interface",
                                         "object_id": port.id}],
                        status="connected")
                except Exception as e:
                    # Cable API differs across NetBox versions (legacy used
                    # termination_a_type/termination_a_id). The device + IP +
                    # MAC + switch_ip/switch_port custom fields are already
                    # written, so topology is recorded even without the cable.
                    logger.warning("sync_access_tracker: cable NIC→%s skipped "
                                   "(custom-field fallback): %s", nas_port, e)

            for s in (sessions or []):
                if not isinstance(s, dict):
                    continue
                try:
                    mac = self._norm_mac(s.get("mac", ""))
                    ip = str(s.get("ip") or "").strip().split("/")[0].strip()
                    nas_ip = str(s.get("nas_ip") or "").strip().split("/")[0].strip()
                    nas_port = str(s.get("nas_port") or "").strip()
                    nas_name = str(s.get("nas_name") or "").strip()
                    username = str(s.get("username") or "").strip()
                    start_time = str(s.get("start_time") or "").strip()
                    # ClearPass endpoint profiling (best-effort — present only
                    # when the CPPM feed profiles the endpoint): device model +
                    # manufacturer place it under its real device_type.
                    model = str(s.get("model") or "").strip()
                    manufacturer = str(s.get("manufacturer") or s.get("vendor") or "").strip()
                    if not mac:
                        skipped += 1
                        continue

                    row = self._resolve_existing_device(
                        mac=mac, ip=ip,
                        by_mac=existing_by_mac, by_ip=existing_by_ip)
                    if row:
                        # Already in NetBox → only-add-missing: skip the create.
                        # Reconciled by MAC OR by framed IP, so a device another
                        # feeder created (console/fw by IP) with no MAC stamped
                        # yet is NOT duplicated here. Backfill the MAC (hardware
                        # truth) when the matched device lacks one — strengthens
                        # future dedup — without clobbering an existing MAC.
                        cf = row.get("custom_fields") or {}
                        matched_mac = self._norm_mac(cf.get("mac_address") or "")
                        if mac and not matched_mac:
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    merged = dict(devobj.custom_fields or {})
                                    merged["mac_address"] = mac
                                    devobj.custom_fields = merged
                                    devobj.save()
                                    existing_by_mac.setdefault(mac, row)
                            except Exception as e:
                                logger.debug("sync_access_tracker: backfill mac %s failed: %s", mac, e)
                        # Best-effort refresh of topology/last_seen ONLY on a
                        # device we own (never touch another source's record).
                        if str((cf.get("discovered_from") or "")).lower() == "cppm-access-tracker":
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    merged = dict(devobj.custom_fields or {})
                                    if start_time:
                                        merged["last_seen"] = start_time
                                    if nas_ip:
                                        merged["switch_ip"] = nas_ip
                                    if nas_port:
                                        merged["switch_port"] = nas_port
                                    if nas_name:
                                        merged["switch_name"] = nas_name
                                    devobj.custom_fields = merged
                                    devobj.save()
                            except Exception as e:
                                logger.debug("sync_access_tracker: refresh %s failed: %s", mac, e)
                        skipped += 1
                        continue

                    # Create the missing endpoint device.
                    name = (username or f"device-{mac.replace(':', '')}")
                    name = self._uniq_device_name(name, mac, ip, existing_by_name, used_names)
                    used_names.add(name.lower())
                    create_kwargs: Dict[str, Any] = {"name": name, "status": "active"}
                    if role:
                        create_kwargs["role"] = role.id
                    # Prefer a device_type from the profiled model over default.
                    at_dtype = self._device_type_for_model(model, manufacturer) or dtype
                    if at_dtype:
                        create_kwargs["device_type"] = at_dtype.id
                    if site:
                        create_kwargs["site"] = site.id
                    if tenant:
                        create_kwargs["tenant"] = tenant.id
                    devobj = self.nb.dcim.devices.create(**create_kwargs)
                    # Ownership tag + topology custom fields (best-effort; same
                    # create-without-cf + best-effort PATCH lesson as sync_vms).
                    try:
                        merged = dict(devobj.custom_fields or {})
                        merged["discovered_from"] = "cppm-access-tracker"
                        merged["mac_address"] = mac
                        if nas_ip:
                            merged["switch_ip"] = nas_ip
                        if nas_port:
                            merged["switch_port"] = nas_port
                        if nas_name:
                            merged["switch_name"] = nas_name
                        if start_time:
                            merged["last_seen"] = start_time
                        devobj.custom_fields = merged
                        devobj.save()
                    except Exception as e:
                        logger.debug("sync_access_tracker: cf tag skipped: %s", e)
                    self._journal("dcim.device", devobj.id,
                                  "realtime-nac-access-tracker",
                                  note=f"endpoint {name} (MAC {mac})")

                    # NIC interface (native MAC) + framed IP + primary_ip4.
                    nic = None
                    if ip:
                        nic = self.nb.dcim.interfaces.create(
                            device=devobj.id, name="eth0", type="other",
                            mac_address=mac)
                        mask = self._mask_for_ip(ip, tenant)
                        ip_kwargs: Dict[str, Any] = {
                            "address": f"{ip}/{mask}",
                            "assigned_object_type": "dcim.interface",
                            "assigned_object_id": nic.id,
                        }
                        if tenant:
                            ip_kwargs["tenant"] = tenant.id
                        if username:
                            ip_kwargs["dns_name"] = username
                        # Reuse an existing global IP record (NetBox enforces
                        # global uniqueness) and reassign it to this NIC instead
                        # of creating a duplicate that 400s — same fix as
                        # sync_devices, for the same root cause.
                        ipobj = self._reuse_or_create_ip(
                            f"{ip}/{mask}", ip_kwargs, ip, nic.id,
                            tenant=tenant, hostname=username, mac=mac,
                            source="realtime-nac-access-tracker")
                        devobj.primary_ip4 = ipobj.id
                        devobj.save()
                        self._journal("ipam.ipaddress", ipobj.id,
                                      "realtime-nac-access-tracker",
                                      note=f"framed IP {ip}/{mask} → {name}")
                        self._stamp_last_seen(ipobj, when=start_time)

                    # Switch topology (best-effort, never breaks the sync).
                    if nas_ip:
                        sw = _ensure_switch(nas_ip, nas_name)
                        if sw and nas_port and nic is not None:
                            port = _ensure_switch_port(sw, nas_port)
                            if port:
                                _cable_nic_to_port(nic, port)

                    # Track the new device so a later duplicate-MAC session in
                    # the same batch skips instead of re-creating.
                    new_row = {"id": devobj.id, "name": devobj.name,
                               "custom_fields": {"mac_address": mac,
                                                 "discovered_from": "cppm-access-tracker"}}
                    existing_by_mac[mac] = new_row
                    if ip:
                        existing_by_ip.setdefault(ip, new_row)
                    pushed += 1
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"upsert {s.get('mac','?')}: {e}"
                    logger.debug("sync_access_tracker: upsert failed: %s", e)

            msg = (f"{pushed} endpoint(s) added, {skipped} already present, "
                   f"{deleted} deleted, {errors} errors")
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("sync_access_tracker tenant=%s: %s", tenant_slug or "<global>", msg)
            else:
                logger.info("sync_access_tracker tenant=%s: %s", tenant_slug or "<global>", msg)
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "sessions_total": len(sessions or []), "message": msg}
        except Exception as e:
            logger.error("sync_access_tracker failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "sessions_total": len(sessions or [])}

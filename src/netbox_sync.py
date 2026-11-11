"""Device / NW-device / access-tracker sync + discovery helpers for NetboxEngine."""
# ipaddress parses IPs/networks for prefix (mask) matching and address validation.
import ipaddress
# re matches placeholder names, MAC formats and DNS-name validity.
import re
# time.monotonic ages the global-identity device listing cache.
import threading
import time
# logging reports sync progress and best-effort failures.
import logging
# dataclass/field build the per-batch state container _DeviceSyncRun.
from dataclasses import dataclass, field
# datetime/timezone stamp "last seen" style timestamps in UTC.
from datetime import datetime, timezone
# Type hints used across the helpers.
from typing import Any, Dict, Optional, Tuple

# pynetbox provides live NetBox Records that we update and save().
import pynetbox
# SimpleNamespace builds lightweight attribute objects from plain dicts.
from types import SimpleNamespace

# Slug of the catch-all site used when a device has no better site.
from custom_fields_spec import FALLBACK_SITE_SLUG
# guess_role infers a device role from names/vendors for newly discovered devices.
from netbox_profile import guess_role

# Shared logger name so these messages land with the rest of the NetBox engine's logs.
logger = logging.getLogger("NetboxEngine")

# How long a global device listing is reused for the MAC/serial identity index.
_GLOBAL_IDENTITY_TTL_S = 60
# One fetch at a time: concurrent callers that miss the cache wait and reuse it.
_global_devices_lock = threading.Lock()


# Auto-generated names the create branch mints when a record has no hostname
# (device-<mac12>, device-<ip>, device-<serial>/unknown, optionally uniquified).
# A device still carrying one was never named by a human, so any source that
# later learns its real hostname (DHCP/DNS) may rename it.
_PLACEHOLDER_NAME_RE = re.compile(
    r"^device-(?:[0-9a-f]{12}|\d{1,3}(?:\.\d{1,3}){3}|unknown)(?:-[0-9a-z]+)*$", re.I)


def _is_auto_name(name) -> bool:
    """True for a name no human chose: empty, the device-<mac>/<ip>
    placeholder, a bare IPv4 address, or a bare 12-hex-digit MAC."""
    # Normalise to a trimmed string; empty or the device-<...> placeholder means auto-named.
    s = str(name or "").strip()
    if not s or _PLACEHOLDER_NAME_RE.match(s):
        return True
    # A bare dotted IPv4 used as a name was never chosen by a human either.
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", s):
        return True
    # Strip MAC separators and check for exactly 12 hex digits (a raw MAC used as a name).
    return bool(re.fullmatch(r"[0-9a-fA-F]{12}", re.sub(r"[:.\-]", "", s)))


# NetBox's own ipaddress.dns_name validator; anything else 400s the save.
_DNS_NAME_RE = re.compile(r"^([0-9A-Za-z_-]+|\*)(\.[0-9A-Za-z_-]+)*\.?$")


# Device custom fields that carry a MAC sighting ("last seen on switch X port
# Y"). The incoming record uses the same names (``_incoming_device_record``).
_SWITCH_CF_KEYS = ("switch_name", "switch_ip", "switch_port")
# Logical interfaces a MAC transits rather than plugs into (LAGs, VXLAN
# tunnels, VLAN SVIs); same list as lm's nw_discovery_sync.
_LOGICAL_PORT_RE = re.compile(
    r"^(lag|po\d|port-channel|trk|ae\d|bond|vxlan|nve|vlan|vl\d|irb|bvi|tunnel)",
    re.IGNORECASE)

# The placeholder type/role slug for a device nobody has identified yet.
_GENERIC_SLUG = "discovered"

# nw poll ``object_type`` -> (device role slug, manufacturer).
# Roles a management controller (iLO/iDRAC) record carries. Such a record shares
# its server's chassis serial, so it must never be matched to, or renamed from,
# the server's record (and vice versa) — they are linked, not merged.
_MGMT_ROLE_SLUGS = frozenset({"bmc", "ipmi", "management-controller"})


def _row_role_slug(row: Any) -> str:
    """Role slug of a device listing row (NetBox 4 ``role`` / 3 ``device_role``)."""
    if isinstance(row, dict):
        ref = row.get("role") or row.get("device_role") or {}
    else:
        ref = getattr(row, "role", None) or getattr(row, "device_role", None) or {}
    return _ref_slug(ref).lower()


def _is_mgmt_row(row: Any) -> bool:
    """True when a device listing row is a management controller."""
    return _row_role_slug(row) in _MGMT_ROLE_SLUGS


_NW_OBJECT_TYPE_IDENTITY = {
    "ipmi": ("bmc", ""),
    "aos_switch": ("switch", "Aruba"),
    "cx_switch": ("switch", "Aruba"),
    "ex_switch": ("switch", "Juniper"),
    "gateway": ("gateway", "Aruba"),
}


def _ref_slug(ref: Any) -> str:
    """Slug of a nested NetBox reference (listing dict or pynetbox record)."""
    # Listing rows give nested refs as dicts; read the slug key.
    if isinstance(ref, dict):
        return str(ref.get("slug") or "")
    # pynetbox Records expose slug as an attribute; a missing slug becomes ''.
    return str(getattr(ref, "slug", "") or "")


def _normalize_source_tag(source: Any) -> str:
    """The ``discovered_from`` ownership tag for a sync ``source``: the
    firewall synonyms collapse to the legacy ``"opnsense"`` tag (existing
    firewall deployments stay byte-identical); anything else (nw's
    ``"Network Devices"``) is used verbatim."""
    # Default to the firewall source when nothing is given.
    tag = str(source or "opnsense").strip()
    # Firewall synonyms map to the legacy tag so old ownership tags still match.
    return "opnsense" if tag.lower() in ("opnsense", "fw", "firewall") else tag


def _primary_addr(row: dict) -> str:
    """Bare primary IPv4 (no mask) of a listed device row, or ''."""
    # primary_ip4 is a nested dict (or None) in a listing row.
    pip = row.get("primary_ip4")
    # No primary IP assigned: nothing to return.
    if not isinstance(pip, dict):
        return ""
    # Drop the /prefix length and surrounding spaces to get the bare address.
    return (pip.get("address") or "").split("/")[0].strip()


@dataclass
class _DeviceSyncRun:
    """Everything ONE ``sync_devices`` call shares between its steps.

    The indexes are a snapshot of NetBox taken once at the start of the batch
    (one paginated listing) — they do NOT see devices created later in the
    batch. The ``used_names`` / ``created_by_name`` / ``refreshed_ids`` sets
    cover that gap; see the field comments.
    """
    # Batch identity: tenant slug, the ownership tag we write, which side wins conflicts, and
    # the default field values new devices receive.
    tenant_slug: str
    source_tag: str                      # discovered_from value we own
    source_of_truth: str                 # "external" | "netbox"
    defaults: Dict[str, Any]
    # Resolved NetBox tenant Record (looked up once per batch).
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
        # Discard the possibly-dirty cached Record; the next access fetches a fresh copy.
        self.device_objs.pop(dev_id, None)

    def forget_ip(self, ip_id: Any) -> None:
        """An IP was written OUTSIDE its cached Record (``_reuse_or_create_ip``
        reassigns/tags by its own lookup, a device delete cascades to its
        IPs): drop what we hold so the next use re-reads it (GET), exactly
        like the per-write GET this cache replaced."""
        # Drop both the listed row and the live Record so neither stale copy is reused.
        self.ip_rows.pop(ip_id, None)
        self.ip_objs.pop(ip_id, None)

    def owns(self, cf: Optional[dict]) -> bool:
        """True iff a device's custom fields carry OUR ``discovered_from`` tag
        (case-insensitive)."""
        # Compare the device's discovered_from with our tag, ignoring case and missing values.
        return str((cf or {}).get("discovered_from") or "").lower() == self.source_tag.lower()

    def note_error(self, text: str) -> None:
        """Count one error; keep the first one's text for the result message."""
        # Count the failure.
        self.errors += 1
        # Only the first error's text is kept for the summary.
        if self.first_err is None:
            self.first_err = text


class _IncomingDevice:
    """Read-only view of one normalized incoming record (see
    ``SyncMixin._incoming_device_record``) plus the facts every step asks."""

    # A fixed attribute set saves memory and catches typos when assigning attributes.
    __slots__ = ("key", "rec", "mac", "hostname", "serial", "real_ip")

    def __init__(self, key: str, rec: Dict[str, Any]):
        """Wrap one incoming record under its incoming-set key and pre-extract common fields."""
        # Keep the key and raw record, and copy out the identity fields used for matching.
        self.key = key                     # incoming-set key: IP or mac:/host:/serial:
        self.rec = rec
        self.mac = rec["mac"]
        self.hostname = rec["hostname"]
        self.serial = rec.get("serial") or ""
        # A synthetic key carries no address to assign.
        self.real_ip = "" if key.startswith(("mac:", "host:", "serial:")) else key

    @property
    def keyed_by_ip(self) -> bool:
        """True when the record is keyed by a real IP address (not a mac:/host:/serial: key)."""
        return bool(self.real_ip)

    @property
    def has_hostname(self) -> bool:
        """A usable hostname ('unknown' is what DHCP reports for none)."""
        # DHCP reports 'unknown' when a client gave no name, so that does not count.
        return bool(self.hostname) and self.hostname.lower() != "unknown"

    @property
    def operator_named(self) -> bool:
        """The hostname is an operator assertion (DNS A record / DHCP
        reservation): one name = one device."""
        # DNS records and DHCP reservations are deliberate names set by an operator.
        return self.rec.get("hostname_source") in ("dns", "reservation")

    @property
    def default_name(self) -> str:
        """Name for a new device: the hostname, else an auto placeholder
        ``device-<mac12>`` / ``device-<serial>`` / ``device-<ip>``."""
        # Prefer the real hostname when there is one.
        if self.has_hostname:
            return self.hostname
        # Next best: a placeholder built from the MAC without colons.
        if self.mac:
            return f"device-{self.mac.replace(':', '')}"
        # Then a placeholder from the serial number.
        if self.serial:
            return f"device-{self.serial}"
        # Last resort: the IP address (or 'unknown' if there is none).
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
        # Trim and upper-case; None becomes an empty name.
        return str(name or "").strip().upper()

    @staticmethod
    def _norm_mac(mac: str) -> str:
        """Normalize a MAC to lowercase colon form (aa:bb:cc:dd:ee:ff).

        The OPNsense spoke returns the raw MAC; normalize here so the value
        written to the IP's ``mac_address`` custom field matches what the
        NetBox→CPPM endpoint sync reads (it keys on the colon form).
        """
        # Trim/lower-case, then keep only hex digits to check whether it is a real MAC.
        m = (mac or "").strip().lower()
        hexonly = re.sub(r"[^0-9a-f]", "", m)
        # Exactly 12 hex digits: re-insert a colon between every byte pair.
        if len(hexonly) == 12:
            return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))
        # Not a recognisable MAC: return the cleaned value unchanged.
        return m

    def _set_iface_mac(self, iface: Any, mac: str,
                       kind: str = "dcim.interface") -> None:
        """Make ``mac`` the primary MAC of ``iface`` (``kind`` is
        ``dcim.interface`` or ``virtualization.vminterface``). Raises on failure.

        Why: NetBox 4.2+ keeps MACs as separate ``dcim.mac-addresses`` records,
        and the interface's own ``mac_address`` field is read-only. Writing it
        returns 200 but stores nothing, so every poll saw a "changed" MAC and
        re-PATCHed every interface (about 3,600 no-op PATCHes an hour on
        LM-NETBOX). Writes happen only when the MAC NetBox shows differs."""
        # Nothing to write without a MAC, or when NetBox already shows this one.
        if not mac or self._norm_mac(str(getattr(iface, "mac_address", "") or "")) == mac:
            return
        # Reuse a record already on this interface (left by an earlier write
        # whose second step failed), else create one, so retries never pile up.
        key = "interface_id" if kind == "dcim.interface" else "vminterface_id"
        rec = next(iter(self.nb.dcim.mac_addresses.filter(mac_address=mac,
                                                          **{key: iface.id})), None)
        if rec is None:
            rec = self.nb.dcim.mac_addresses.create(
                mac_address=mac, assigned_object_type=kind, assigned_object_id=iface.id)
        # Make it the interface's primary MAC.
        iface.primary_mac_address = rec.id
        iface.save()

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
        # Has an IP if primary_ip4 is a dict with a non-empty address (mask stripped).
        pip = row.get("primary_ip4") or {}
        has_ip = isinstance(pip, dict) and bool(
            (pip.get("address") or "").split("/")[0].strip())
        # Has a MAC if the mac_address custom field normalises to something non-empty.
        cf = row.get("custom_fields") or {}
        has_mac = bool(SyncMixin._norm_mac(cf.get("mac_address", "")))
        # Either identifier makes the row a distinct, identifiable machine.
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
        # Any unexpected failure below just disables the cache (see the except).
        try:
            # All networks, plus the same networks grouped by VRF id (None = global table).
            nets = []
            by_vrf: Dict[Any, list] = {}
            # Page through every prefix in NetBox once.
            for p in self._api_get_all("/api/ipam/prefixes/", {"limit": 500}):
                pfx = (p or {}).get("prefix")
                # Skip rows without a prefix value.
                if not pfx:
                    continue
                # Parse the prefix; a malformed one is skipped rather than aborting the prefetch.
                try:
                    net = ipaddress.ip_network(pfx, strict=False)
                except ValueError:
                    continue
                # Remember the network in the flat list and under its VRF id.
                nets.append(net)
                vrf = p.get("vrf")
                by_vrf.setdefault(vrf.get("id") if isinstance(vrf, dict) else None,
                                  []).append(net)
            # Publish the caches for _mask_for_ip to use.
            self._prefix_prefetch = nets
            self._prefix_prefetch_vrf = by_vrf
        # Prefetch failed: leave the caches empty so lookups use the per-IP API path.
        except Exception as e:
            logger.debug("prefix prefetch failed (per-IP fallback stays): %s", e)
            self._prefix_prefetch = None
            self._prefix_prefetch_vrf = None

    def _end_prefix_prefetch(self) -> None:
        """Drop the per-run prefix cache (call in the sync's ``finally``)."""
        # Clearing both caches makes later calls outside a sync run use the API again.
        self._prefix_prefetch = None
        self._prefix_prefetch_vrf = None

    @staticmethod
    def _mask_from_prefixes(ip_str: str, nets) -> Optional[str]:
        """Longest-prefix match of ``ip_str`` against a list of
        ``ipaddress`` networks. Returns the prefixlen as a string, or None
        when no prefix of the same address family covers the IP (or the IP is
        unparsable) — the caller then falls back to the per-IP API lookup."""
        # Parse the bare address (mask stripped); unparsable input means "no local answer".
        try:
            addr = ipaddress.ip_address(str(ip_str).split("/")[0].strip())
        except ValueError:
            return None
        # Track the longest prefix length seen so far among matching networks.
        best = None
        # Check every network of the same IP family that contains the address.
        for net in (nets or []):
            if net.version == addr.version and addr in net:
                # Keep the most specific (longest) match.
                if best is None or net.prefixlen > best:
                    best = net.prefixlen
        # Return it as a string, or None if nothing covered the address.
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
        # Find the tenant's VRF (if any) and the prefetched prefix caches (if a run is active).
        vrf_id = self._vrf_id_for_tenant(tenant) if tenant else None
        nets = getattr(self, "_prefix_prefetch", None)
        by_vrf = getattr(self, "_prefix_prefetch_vrf", None)
        # With a known VRF, only that VRF's prefixes plus global ones may supply the mask.
        if nets is not None and vrf_id is not None and by_vrf is not None:
            nets = by_vrf.get(vrf_id, []) + by_vrf.get(None, [])
        # A prefetch exists: try a local longest-prefix match first.
        if nets is not None:
            local = self._mask_from_prefixes(ip_str, nets)
            if local is not None:
                return local
            # uncovered by the prefetched set → per-IP API fallback (catches a
            # prefix added mid-run; otherwise resolves to /32 as before).
        # Per-IP API fallback; any failure there just means we use /32.
        try:
            # Ask NetBox for every prefix containing this IP, limited to the tenant's VRF when
            # known.
            q: Dict[str, Any] = {"contains": ip_str, "limit": 500}
            if vrf_id is not None:
                q["present_in_vrf_id"] = vrf_id
            # Parse each returned prefix into a network object.
            pdata = self._api_get("/api/ipam/prefixes/", q)
            prefs = [ipaddress.ip_network(p["prefix"], strict=False)
                     for p in pdata.get("results", []) if p.get("prefix")]
            # Several prefixes can contain the IP; the longest prefix is the most specific subnet.
            if prefs:
                prefs.sort(key=lambda n: n.prefixlen, reverse=True)  # longest first
                return str(prefs[0].prefixlen)
        # API or parse failure: log quietly and fall through to the host mask.
        except Exception as e:
            logger.debug("containing-prefix lookup for %s failed, using /32: %s", ip_str, e)
        # No containing prefix: treat the address as a single host.
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
        # Import lazily so a missing/broken spec module only disables pass-through fields.
        try:
            from custom_fields_spec import CUSTOM_FIELDS_SPEC
            # Keep the names of fields defined for the dcim.device object type.
            return {name for (name, _t, _l, ct) in CUSTOM_FIELDS_SPEC
                    if ct == "dcim.device"}
        # Spec unavailable: allow no per-record custom fields rather than risk 400s.
        except Exception:
            return set()

    def _ensure_device_role(self, slug: str = "discovered"):
        """Return the device role (auto-creating 'discovered' if missing). Best-effort.

        TTL-cached per engine (``_cached_ref``): every sync_devices call — and
        every record carrying its own role — used to re-GET the same role.
        A failure returns None, cached only for the short negative TTL."""
        # Normalise the slug; blank falls back to 'discovered'.
        slug = (slug or "discovered").strip().lower() or "discovered"
        # Look it up through the per-engine TTL cache, creating it on a miss.
        return self._cached_ref("ensure_device_role", slug,
                                lambda: self._get_or_create_device_role(slug))

    def _get_or_create_device_role(self, slug: str):
        """Fetch a device role by slug, creating it (grey, title-cased name) when missing."""
        # Network or permission errors are handled below by returning None.
        try:
            # Reuse the role if it already exists.
            r = self.nb.dcim.device_roles.get(slug=slug)
            if r:
                return r
            # Otherwise create it with a readable name derived from the slug.
            return self.nb.dcim.device_roles.create(
                name=slug.replace("-", " ").title(), slug=slug, color="9e9e9e")
        except Exception as e:
            # WARNING (not debug): a None role cascades into per-device create
            # failures (device_type/role unresolved), so surface the reason.
            logger.warning("ensure_device_role '%s' failed (creates will error): %s", slug, e)
            return None

    def _ensure_device_type(self, slug: str = "discovered"):
        """Return the device type (auto-creating 'Discovered Device' under an
        'Unknown' manufacturer if missing). Best-effort; TTL-cached like
        ``_ensure_device_role``."""
        # Normalise the slug; blank falls back to 'discovered'.
        slug = (slug or "discovered").strip().lower() or "discovered"
        # Look it up through the per-engine TTL cache, creating it on a miss.
        return self._cached_ref("ensure_device_type", slug,
                                lambda: self._get_or_create_device_type(slug))

    def _get_or_create_device_type(self, slug: str):
        """Fetch a device type by slug, creating a 'Discovered Device' type when missing."""
        # Errors are handled below by returning None.
        try:
            # Reuse the device type if it already exists.
            dt = self.nb.dcim.device_types.get(slug=slug)
            if dt:
                return dt
            # A new device type needs a manufacturer; try the 'unknown' one first.
            mfr = None
            try:
                mfr = self.nb.dcim.manufacturers.get(slug="unknown")
            # A lookup failure is treated like "not found" so we try to create it.
            except Exception:
                mfr = None
            # Create the 'Unknown' manufacturer when it does not exist yet.
            if not mfr:
                mfr = self.nb.dcim.manufacturers.create(name="Unknown", slug="unknown")
            # Create the generic device type under that manufacturer.
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
        # Local import keeps this static helper self-contained.
        import re as _re
        # Replace runs of non-alphanumerics with '-', then trim leading/trailing hyphens.
        s = _re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
        # Never return an empty slug.
        return s or "discovered"

    def _ensure_manufacturer(self, name: str = ""):
        """Get-or-create a ``dcim.manufacturer`` by name ('Unknown' when blank),
        TTL-cached. Returns the object or None (best-effort)."""
        # Blank names map to the 'Unknown' manufacturer; the slug is derived from the name.
        name = str(name or "").strip() or "Unknown"
        slug = self._slugify(name)

        def _fetch():
            """Get the manufacturer by slug, or create it on a miss."""
            # Reuse an existing manufacturer.
            m = self.nb.dcim.manufacturers.get(slug=slug)
            if m:
                return m
            # Create a new manufacturer with the given name.
            return self.nb.dcim.manufacturers.create(name=name, slug=slug)
        # Go through the TTL cache; failures are logged and return None (best-effort).
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
        # Without a model string there is nothing to identify; caller uses the default type.
        model = str(model or "").strip()
        if not model:
            return None
        # Include the manufacturer in the slug so identical model names from two vendors differ.
        mfr_name = str(manufacturer or "").strip()
        slug = self._slugify(f"{mfr_name}-{model}" if mfr_name else model)

        def _fetch():
            """Get the device type by slug, or create it under its manufacturer on a miss."""
            # Reuse an existing type.
            dt = self.nb.dcim.device_types.get(slug=slug)
            if dt:
                return dt
            # Ensure the manufacturer exists, then create the type linked to it (if available).
            mfr = self._ensure_manufacturer(mfr_name)
            return self.nb.dcim.device_types.create(
                model=model, slug=slug,
                manufacturer=(mfr.id if mfr else None))
        # Go through the TTL cache; failures are logged and return None.
        try:
            return self._cached_ref("device_type_model", slug, _fetch)
        except Exception as e:
            logger.warning("device_type_for_model '%s' failed: %s", model, e)
            return None

    def _device_type_from_spec(self, spec: Dict[str, Any]):
        """Get-or-create the device type (with port/console/power templates)
        described by a hub-learned model spec. None when it can't be built."""
        # Copy the spec and add a slug derived from manufacturer and model.
        t = dict(spec, slug=self._slugify(f"{spec.get('manufacturer')}-{spec.get('model')}"))

        def _fetch():
            """Build the manufacturer, device type and its component templates from the spec."""
            # Seed (get-or-create) the manufacturer and the device type.
            mfr, _ = self._seed_manufacturer(t["manufacturer"], {})
            dt, _, _ = self._seed_device_type(t, mfr)
            # Add port/console/power templates; a failure here still leaves a usable type.
            try:
                self._seed_templates(dt, t)
            except Exception as e:  # noqa: BLE001 — the type is still right
                logger.warning("device type %s templates incomplete: %s", t["slug"], e)
            return dt
        # Go through the TTL cache; a failure is logged and the caller gets None.
        try:
            return self._cached_ref("device_type_spec", t["slug"], _fetch)
        except Exception as e:  # noqa: BLE001
            logger.warning("device type from spec %s failed: %s", t["slug"], e)
            return None

    def _identified_device_type(self, model: str, manufacturer: str, spec: Any):
        """The device type a feeder identified: the model spec's type when one
        is known, else a bare type named after the reported model.
        Returns ``(device_type|None, slugs it may replace)``."""
        # Slugs this type may replace: always the generic one, plus the bare model-named one.
        raw_slugs = {_GENERIC_SLUG}
        if model:
            raw_slugs.add(self._slugify(f"{manufacturer}-{model}" if manufacturer else model))
        # A full model spec (manufacturer + model) builds the richest type.
        if isinstance(spec, dict) and spec.get("manufacturer") and spec.get("model"):
            dt = self._device_type_from_spec(spec)
            if dt is not None:
                return dt, raw_slugs
        # No spec: use a bare type named after the model; only the generic type is replaceable.
        return self._device_type_for_model(model, manufacturer), {_GENERIC_SLUG}

    @staticmethod
    def _upgrade_generic_identity(devobj: Any, dtype: Any = None, role: Any = None,
                                  replaceable_types: Any = (_GENERIC_SLUG,)) -> bool:
        """Move a device off the generic 'discovered' type/role once a feeder
        has identified it. A type or role someone set is never replaced; only
        the generic type or a bare type LM named after the reported model
        (``replaceable_types``). Returns True when ``devobj`` changed."""
        # Read the device's current type slug.
        changed = False
        current_type = _ref_slug(getattr(devobj, "device_type", None))
        # Replace the type only if it is generic/replaceable and actually different.
        if (dtype is not None and current_type in replaceable_types
                and current_type != _ref_slug(dtype)):
            devobj.device_type = dtype.id
            changed = True
        # Replace the role only if it is still the generic 'discovered' role.
        if role is not None and _ref_slug(getattr(devobj, "role", None)) == _GENERIC_SLUG:
            devobj.role = role.id
            changed = True
        return changed

    def _site_for_ip(self, ip: str, tenant=None) -> Optional[Dict[str, Any]]:
        """The site of the most specific site-scoped prefix containing ``ip``
        (``{id, slug, name}``), else None. On a tie (the same prefix in two
        tenants' VRFs) the device tenant's prefix wins. The prefix list is
        cached for a minute so a fleet poll asks NetBox once."""
        # Local import of time for the monotonic cache clock.
        import time as _time
        # Parse the bare address; an invalid one has no site.
        try:
            addr = ipaddress.ip_address(str(ip or "").split("/")[0].strip())
        except ValueError:
            return None
        # Reuse the cached site-scoped prefix list if it is less than a minute old.
        cache = self.__dict__.get("_site_prefix_cache")
        if not cache or _time.monotonic() - cache[0] > 60:
            # Refresh: list all prefixes scoped to a site; failure leaves an empty list.
            try:
                rows = self._api_get_all("/api/ipam/prefixes/", {"scope_type": "dcim.site", "limit": 1000})
            except Exception as e:  # noqa: BLE001
                logger.warning("site_for_ip: listing site-scoped prefixes failed: %s", e)
                rows = []
            # Build (network, tenant id, site ref) tuples for prefixes scoped to a site.
            nets = []
            for p in rows:
                scope = p.get("scope") if p.get("scope_type") == "dcim.site" else None
                # Skip prefixes with a missing or malformed prefix value.
                try:
                    if isinstance(scope, dict) and scope.get("id"):
                        nets.append((ipaddress.ip_network(p["prefix"], strict=False),
                                     (p.get("tenant") or {}).get("id"),
                                     {k: scope.get(k) for k in ("id", "slug", "name")}))
                except (KeyError, ValueError):
                    continue
            # Store the fresh list with its timestamp on the instance.
            cache = self.__dict__["_site_prefix_cache"] = (_time.monotonic(), nets)
        # Pick the longest matching prefix; on equal length prefer the device tenant's prefix.
        tid = getattr(tenant, "id", None)
        best = None
        # Only prefixes of the same IP family that contain the address are candidates.
        for net, ptid, site in cache[1]:
            if addr.version != net.version or addr not in net:
                continue
            # Another tenant's subnet says nothing about this tenant's device
            # (overlapping/unrelated address space, e.g. a DXP device must not
            # land in a MIP site); untenanted prefixes stay eligible.
            if tid is not None and ptid is not None and ptid != tid:
                continue
            # Rank by prefix length first, then by tenant match.
            rank = (net.prefixlen, ptid == tid and tid is not None)
            if best is None or rank > best[0]:
                best = (rank, site)
        # Return the winning site ref, or None if no prefix matched.
        return best[1] if best else None

    def _device_site_ref(self, devobj: Any) -> Dict[str, Any]:
        """``{slug, name}`` of the device's site ({} when unknown)."""
        # The site may be a Record, or a bare id right after we assigned it.
        site = getattr(devobj, "site", None)
        # For a bare id, rebuild a slug/name object from the cached site prefix list.
        if isinstance(site, int):  # just assigned by id
            site = next((SimpleNamespace(**x[2]) for x in
                         (self.__dict__.get("_site_prefix_cache") or (0, []))[1]
                         if x[2].get("id") == site), None)
        # Return slug and name when a slug is known, else an empty dict.
        slug = getattr(site, "slug", None)
        return {"slug": str(slug), "name": str(getattr(site, "name", "") or slug)} if isinstance(slug, str) and slug else {}

    def _place_in_prefix_site(self, devobj: Any, ip: str, tenant=None) -> bool:
        """Move a device still in the auto-created ``discovered`` site into the
        site of the prefix holding ``ip``. A device someone placed in a real
        site is left alone. Returns True when the site changed."""
        # Only devices still parked in the fallback 'discovered' site are moved.
        cur = getattr(devobj, "site", None)
        if str(getattr(cur, "slug", "") or "") != FALLBACK_SITE_SLUG:
            return False
        # Find the site of the prefix holding the IP; no site or same site means no change.
        site = self._site_for_ip(ip, tenant)
        if not site or site["id"] == getattr(cur, "id", None):
            return False
        # Assign the new site by id and log the move.
        devobj.site = site["id"]
        logger.info("netbox: device %s moved to site %s (prefix of %s)",
                    getattr(devobj, "name", devobj), site.get("slug"), ip)
        return True

    def _resolve_site(self, slug: str = "", tenant=None):
        """Resolve a site for device creation. Configured slug first; else the
        first existing site; else AUTO-CREATE a fallback site. NetBox REQUIRES
        every dcim.device to carry a site, so on the create path this must not
        return None — it mirrors _ensure_device_role / _ensure_device_type, which
        auto-provision the other two required refs. Returns None only if the
        auto-create itself fails (e.g. a permissions issue — logged)."""
        # Normalise the requested slug.
        slug = (slug or "").strip().lower()
        # A configured slug: look it up (cached) and use it if it exists.
        if slug:
            # perf-scan D1b: TTL-cached so a fleet poll doesn't re-fetch the
            # same slug once per device.
            s = self._cached_ref("site", slug,
                                  lambda: self.nb.dcim.sites.get(slug=slug))
            if s:
                return s
        # No configured site: a site named after the tenant, else the neutral
        # 'discovered' site. Never an arbitrary existing site — "the first one"
        # is whichever tenant's site NetBox lists first (devices of a tenant
        # with no site ended up in another tenant's site).
        try:
            tslug = str(getattr(tenant, "slug", "") or "").strip().lower()
            if tslug:
                ts = self._cached_ref("site", tslug,
                                      lambda: self.nb.dcim.sites.get(slug=tslug))
                if ts:
                    return ts
            fb = self._cached_ref("site", FALLBACK_SITE_SLUG,
                                  lambda: self.nb.dcim.sites.get(slug=FALLBACK_SITE_SLUG))
            if fb:
                return fb
        except Exception as e:
            # warn so a permissions issue is visible rather than silent.
            logger.warning("resolve_site fallback lookup failed: %s", e)
        # Nothing configured and no site exists — auto-create one so device
        # creation (which REQUIRES a site) succeeds instead of 400ing every
        # record (the failure behind "fw-discovery: 183 errors, site required").
        # Use the configured slug if given, else a default 'discovered' (mirrors
        # _ensure_device_role/_type). Created once; the first-site fallback above
        # adopts it on subsequent syncs.
        # Pick the slug for the auto-created site.
        create_slug = slug or FALLBACK_SITE_SLUG
        # Create the site with a readable name; a failure is logged and None is returned.
        try:
            return self.nb.dcim.sites.create(
                name=create_slug.replace("-", " ").replace("_", " ").title(),
                slug=create_slug, status="active")
        except Exception as e:
            logger.warning("resolve_site auto-create '%s' failed (device creates "
                           "will 400 on the required site): %s", create_slug, e)
        return None

    def _all_devices_cached(self) -> list:
        """Every device (all tenants), reused for _GLOBAL_IDENTITY_TTL_S seconds.

        Each NW device push needs this listing (~1 MB per 500 devices), and a
        fleet poll pushes devices seconds apart. Code that creates a device sets
        ``_global_devices_cache = None`` so the next lookup sees it."""
        with _global_devices_lock:
            cached = getattr(self, "_global_devices_cache", None)
            if cached and time.monotonic() - cached[0] < _GLOBAL_IDENTITY_TTL_S:
                return cached[1]
            rows = self._api_get_all("/api/dcim/devices/", {"limit": 500})
            self._global_devices_cache = (time.monotonic(), rows)
            return rows

    def _add_global_identities(self, by_mac: Dict[str, dict],
                               by_serial: Optional[Dict[str, dict]] = None,
                               rows: Optional[list] = None,
                               by_serial_mgmt: Optional[Dict[str, dict]] = None) -> None:
        """Fill MAC / serial indexes from EVERY tenant (setdefault: rows already
        indexed from the caller's tenant-scoped listing win).

        MAC and serial identify a physical device globally. A sync scoped to
        tenant B could not see tenant A's record of the same MAC and minted a
        copy every time (the RA/LRB ``device-<mac>`` pairs). ``rows`` is an
        already-fetched global device listing (sync_devices pages it once and
        reuses it); without it the listing is fetched here. A listing failure
        raises: matching without global identities creates cross-tenant
        duplicates, so the sync must fail and retry instead."""
        # Fetch every device (all tenants) unless the caller already has the listing.
        # Reuse a listing fetched in the last _GLOBAL_IDENTITY_TTL_S seconds: every
        # NW device push calls this, and each fetch is ~1 MB per 500 devices.
        if rows is None:
            rows = self._all_devices_cached()
        # Walk every device row and index it by MAC and by serial.
        for row in rows or []:
            # Normalised MAC from the custom field; existing (tenant-scoped) entries are kept.
            cf = row.get("custom_fields") or {}
            rmac = self._norm_mac(cf.get("mac_address", ""))
            if rmac:
                by_mac.setdefault(rmac, row)
            # Serial (lower-cased) when the caller wants a serial index too.
            rserial = str(row.get("serial") or "").strip()
            if rserial and by_serial is not None and not _is_mgmt_row(row):
                by_serial.setdefault(rserial.lower(), row)
            if rserial and by_serial_mgmt is not None and _is_mgmt_row(row):
                by_serial_mgmt.setdefault(rserial.lower(), row)

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
        # One empty index per identity type.
        by_serial: Dict[str, dict] = {}
        by_serial_mgmt: Dict[str, dict] = {}
        by_mac: Dict[str, dict] = {}
        by_nw: Dict[str, dict] = {}
        by_ip: Dict[str, dict] = {}
        by_name: Dict[str, dict] = {}
        # Index every listed device row under each identity it carries.
        for row in (rows or []):
            # By lower-cased name.
            rname = str(row.get("name") or "").strip().lower()
            if rname:
                by_name.setdefault(rname, row)
            # By lower-cased serial number.
            rserial = str(row.get("serial") or "").strip()
            if rserial:
                (by_serial_mgmt if _is_mgmt_row(row) else by_serial).setdefault(rserial.lower(), row)
            # By normalised MAC from the mac_address custom field.
            cf = row.get("custom_fields") or {}
            rmac = self._norm_mac(cf.get("mac_address", ""))
            if rmac:
                by_mac.setdefault(rmac, row)
            # By the network-poller device id custom field.
            rnw = str(cf.get("nw_device_id") or "").strip()
            if rnw:
                by_nw.setdefault(rnw, row)
            # By bare primary IPv4 address (mask stripped).
            pip = row.get("primary_ip4")
            addr = ((pip.get("address") or "").split("/")[0].strip()
                    if isinstance(pip, dict) else "")
            if addr:
                by_ip.setdefault(addr, row)
        # Hand back all five indexes keyed by name.
        return {"by_serial": by_serial, "by_serial_mgmt": by_serial_mgmt, "by_mac": by_mac,
                "by_nw": by_nw, "by_ip": by_ip, "by_name": by_name}

    def _adopt_real_name(self, devobj, hostname: str, by_name: dict,
                         source: str = "sync", force: bool = False,
                         expect_mgmt: Optional[bool] = None) -> bool:
        """Rename a device still carrying an auto name (see ``_is_auto_name``)
        to a real hostname learned by another feeder (LLDP system name, polled
        hostname). Never renames a human-chosen name unless ``force`` (the
        caller matched the device by hardware identity, so a changed hostname
        is a rename of the same box) and never takes a name another device
        already holds. Returns True iff renamed."""
        # A feed never renames a record of a different kind (a controller's
        # hostname is not its server's, even when they share a serial).
        if expect_mgmt is not None and _is_mgmt_row(devobj) != expect_mgmt:
            return False
        # Current device name and the candidate hostname, trimmed.
        cur = str(getattr(devobj, "name", "") or "").strip()
        host = str(hostname or "").strip()
        # Only rename when the new name is real and the current name is an auto placeholder.
        if not host or host.lower() == "unknown" or _is_auto_name(host) \
                or not (force or _is_auto_name(cur)):
            return False
        # Use the short (first DNS label) hostname in canonical upper case; skip if unchanged.
        new = self._nb_name(host.split(".", 1)[0])
        if not new or new == cur:
            return False
        # Never steal a name another device already holds.
        holder = (by_name or {}).get(new.lower())
        if holder and holder.get("id") != getattr(devobj, "id", None):
            logger.info("netbox: keep %s - name %s already held by device %s",
                        cur or "(unnamed)", new, holder.get("id"))
            return False
        # Rename and save; a failure (e.g. a name clash in NetBox) is logged and reported as False.
        try:
            devobj.name = new
            devobj.save()
        except Exception as e:
            logger.warning("netbox: rename %s -> %s failed: %s", cur, new, e)
            return False
        # Keep the caller's name index in step with the rename.
        if by_name is not None:
            row = by_name.pop(cur.lower(), None) or {}
            by_name[new.lower()] = {**row, "id": getattr(devobj, "id", None), "name": new}
        # Record the rename in the change journal and log it.
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
        # Missing indexes become empty dicts so every tier can be checked uniformly.
        by_serial = by_serial or {}; by_mac = by_mac or {}
        by_nw = by_nw or {}; by_ip = by_ip or {}; by_name = by_name or {}
        # Tier 1: serial number (case-insensitive).
        if serial:
            r = by_serial.get(str(serial).strip().lower())
            if r:
                return r
        # Tier 2: normalised MAC.
        if mac:
            r = by_mac.get(mac)
            if r:
                return r
        # Tier 3: the network-poller device id.
        if nw_device_id:
            r = by_nw.get(str(nw_device_id).strip())
            if r:
                return r
        # Tier 4: primary IPv4.
        if ip:
            r = by_ip.get(str(ip).strip())
            if r:
                return r
        # Tier 5: exact hostname, ignoring the DHCP 'unknown' placeholder.
        if hostname and hostname.strip().lower() != "unknown":
            r = by_name.get(hostname.strip().lower())
            if r:
                return r
        # No tier matched: the caller treats this as a new device.
        return None

    def _fill_device_gaps(self, run: "_DeviceSyncRun", row: dict, hostname: str,
                          mac: str) -> None:
        """Fill-gap update for an existing device when NetBox is the source of
        truth: never overwrite a populated value, only fill EMPTY ones.
        Reads/writes go through the batch's live Records (``_ip_record`` /
        ``_device_record``); custom fields are PATCHed delta-only."""
        # A hostname is usable unless empty or 'unknown'.
        good_host = bool(hostname) and hostname.lower() != "unknown"
        # Find the id of the device's primary IP, if it has one.
        ip_id = None
        pip = row.get("primary_ip4")
        if isinstance(pip, dict):
            ip_id = pip.get("id")
        # Fill the IP's empty dns_name / mac_address when we have something to fill.
        if ip_id and (mac or good_host):
            try:
                # Get the batch's live Record for that IP.
                ipobj = self._ip_record(run, ip_id)
                changed = False
                # Set dns_name only if it is empty and the hostname is valid for NetBox's validator.
                if ipobj and good_host and _DNS_NAME_RE.match(hostname) and not str(getattr(ipobj, "dns_name", "") or "").strip():
                    ipobj.dns_name = hostname
                    changed = True
                # Set the MAC only if the IP has none; this PATCH also carries any dns_name change.
                if ipobj and mac and not (ipobj.custom_fields or {}).get("mac_address"):
                    self._save_cf_delta(ipobj, {"mac_address": mac})  # also sends dns_name
                # Only dns_name changed: a plain save sends it.
                elif changed:
                    ipobj.save()
            # Write failed: drop the cached IP Record so a stale change is not re-sent.
            except Exception as e:
                run.forget_ip(ip_id)
                logger.debug("sync_devices: fill-gap IP %s: %s", ip_id, e)
        # Device-level gaps: missing MAC custom field, or a placeholder name we can replace.
        cf = row.get("custom_fields") or {}
        cur_name = str(row.get("name") or "").strip()
        need_mac = bool(mac) and not cf.get("mac_address")
        need_name = good_host and bool(_PLACEHOLDER_NAME_RE.match(cur_name))
        # Only touch the device when there is something to fill.
        if need_mac or need_name:
            try:
                # Get the live device Record; a vanished device is treated as a failure.
                devobj = self._device_record(run, row["id"])
                if devobj is None:
                    raise LookupError("device vanished")
                # Replace the placeholder name with a unique version of the hostname.
                if need_name:
                    new_name = self._uniq_device_name(hostname, mac, "", run.by_name, run.used_names)
                    devobj.name = new_name
                    run.used_names.add(new_name.lower())
                # Write the MAC via a delta PATCH (which also sends the name), else a plain save.
                if need_mac:
                    self._save_cf_delta(devobj, {"mac_address": mac})  # also sends the name
                else:
                    devobj.save()
                # Log a successful rename.
                if need_name:
                    logger.info("sync_devices: renamed placeholder %s -> %s", cur_name, devobj.name)
            # Write failed: drop the cached device Record so the bad change is not re-sent.
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
        # Without a real address there is nothing to attach.
        if not real_ip:
            return False
        # Name the secondary interface after the MAC's last 4 hex digits (or the IP when no MAC).
        iname = "mgmt-" + mac.replace(":", "")[-4:].upper() if mac else "mgmt-" + real_ip
        # Any NetBox failure below is logged and reported as False.
        try:
            # Reuse the interface if it already exists on the device, else create it.
            existing = list(self.nb.dcim.interfaces.filter(device_id=device_id, name=iname))
            iface = existing[0] if existing else self.nb.dcim.interfaces.create(device=device_id, name=iname, type="other")
            # Build the IP with the subnet mask, assigned to that interface.
            mask = self._mask_for_ip(real_ip, tenant)
            ip_kwargs = {"address": f"{real_ip}/{mask}", "assigned_object_type": "dcim.interface", "assigned_object_id": iface.id}
            # Carry tenant and dns_name onto the IP when known.
            if tenant: ip_kwargs["tenant"] = tenant.id
            if hostname and hostname.lower() != "unknown": ip_kwargs["dns_name"] = hostname
            # Reuse an existing IP object for this address or create one.
            ipobj = self._reuse_or_create_ip(f"{real_ip}/{mask}", ip_kwargs, real_ip, iface.id, tenant=tenant, hostname=hostname, mac=mac, source=source_tag)
            # Report the IP id so the caller can drop its cached copy.
            if touched_ips is not None:
                touched_ips.add(ipobj.id)
            # Journal the assignment and mark the IP as recently seen.
            self._journal("ipam.ipaddress", ipobj.id, source_tag, note=f"IP {real_ip}/{mask} → secondary interface {iname} of device {device_id}")
            self._stamp_last_seen(ipobj)
            return True
        # NetBox rejected something: log and let the caller fall back to its normal path.
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
        # Shared state for this one batch.
        run = _DeviceSyncRun(tenant_slug=tenant_slug,
                             source_tag=_normalize_source_tag(source),
                             source_of_truth=source_of_truth,
                             defaults=defaults or {})
        try:
            # Self-heal custom fields (discovered_from on dcim.device,
            # mac_address on ipam.ipaddress) so the ownership tag + MAC writes
            # below land. Cached per-process; no-op once provisioned.
            self._ensure_custom_fields()
            # Resolve the tenant; refuse to run if the slug is unknown.
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
                # Make sure the tenant's IPs live in its VRF before matching by IP.
                self._move_tenant_ips_to_vrf(run.tenant)

            # Clean and key the incoming feed; count rows that could not be used.
            incoming, run.skipped = self._normalize_incoming_devices(devices)

            # One paginated listing of existing devices; failing here aborts the batch.
            try:
                rows, all_rows = self._list_devices_for_sync(run)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": run.skipped, "deleted": 0,
                        "devices_total": len(incoming)}
            # Build the lookup indexes and the id -> row map used to make live Records lazily.
            self._index_sync_rows(run, rows)
            run.device_rows = {r["id"]: r for r in (all_rows or rows) if r.get("id") is not None}

            # MAC and serial are GLOBAL identities: a device is one device no
            # matter which tenant first recorded it. A tenant-scoped listing
            # can't see another tenant's record of the same MAC, so every sync
            # from a second tenant minted a copy (73 RA/LRB device-<mac> pairs,
            # 2d95909). Index the rest of NetBox by MAC/serial too; tenant-local
            # rows still win, and replace-delete stays tenant-scoped (owned_ips).
            # Only possible when a global listing was read (tenant-scoped syncs).
            if all_rows is not None:
                self._add_global_identities(run.by_mac, run.by_serial, rows=all_rows)
                # A device with NO tenant holding one of this tenant's IPs is
                # this tenant's: match it by IP (and adopt it) instead of
                # minting a duplicate whose IP reassign then fails.
                # Index tenant-less devices by IP so they are adopted rather than duplicated.
                for r in all_rows:
                    addr = _primary_addr(r)
                    if addr and not r.get("tenant"):
                        run.by_ip.setdefault(addr, r)

            # replace=True and tenant-scoped: remove owned devices missing from the feed.
            if replace and tenant_slug:
                self._delete_vanished_owned_devices(run, incoming)

            # Batch-read primary IPs we may touch, then resolve the refs a create needs.
            self._prefetch_primary_ips(run, incoming)
            self._resolve_create_defaults(run)

            # Process each incoming record; one record's failure is counted, not fatal to the batch.
            for key, rec in incoming.items():
                try:
                    self._sync_one_device(run, _IncomingDevice(key, rec))
                except Exception as e:
                    self._note_upsert_failure(run, key, e)

            # Build the summary result for the whole batch.
            return self._sync_devices_result(run, len(incoming))
        # Unexpected failure: log it and return what was achieved so far.
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
        # Keyed records and the count of unusable rows.
        incoming: Dict[str, Dict[str, Any]] = {}
        skipped = 0
        # Process every feed row, ignoring anything that is not a dict.
        for dev in (devices or []):
            if not isinstance(dev, dict):
                continue
            # Work out the key; rows with no identity are skipped and counted.
            key = self._incoming_device_key(dev)
            if key is None:
                skipped += 1  # nothing identifiable — don't add a phantom row
                continue
            # Later rows with the same key replace earlier ones.
            incoming[key] = self._incoming_device_record(dev)
        return incoming, skipped

    def _incoming_device_key(self, dev: dict) -> Optional[str]:
        """The incoming-set key for one feed row (see ``_normalize_incoming_devices``)."""
        # Prefer the bare IPv4 address (mask stripped) as the key.
        ip = str(dev.get("ip") or "").strip().split("/")[0].strip()
        if ip:
            return ip
        # No IP: fall back to the normalised MAC.
        mac = self._norm_mac(dev.get("mac", ""))
        if mac:
            # MAC-only: created like any other record, but replace-delete
            # (which keys on IP) won't track it.
            return f"mac:{mac}"
        # No MAC: fall back to a usable hostname.
        hostname = str(dev.get("hostname") or "").strip()
        if hostname and hostname.lower() != "unknown":
            # Hostname-only (no IP, no MAC): the hostname tier can adopt a
            # same-name device instead of duplicate-creating. The firewall path
            # drops no-IP records upstream, so this is mostly the nw/console case.
            return f"host:{hostname.lower()}"
        # Last resort: the serial number.
        serial = str(dev.get("serial") or "").strip()
        if serial:
            # Serial-only (console-identified, nothing else): the serial tier
            # adopts an existing device instead of creating a phantom.
            return f"serial:{serial.lower()}"
        # Nothing identifiable at all.
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
        * ``profile`` — what the host answered a probe with (sysDescr, HTTP
          title/server, SSH banner); with the hostname it lets
          ``netbox_profile.guess_role`` type a device no feed identified.
        * ``custom_fields`` — free-form per-record fields (TrueNAS product/
          version/…), stringified; only provisioned names are written later
          (``_device_cf_whitelist``)."""
        # Always present: the normalised MAC and the trimmed hostname (possibly empty).
        rec: Dict[str, Any] = {"mac": self._norm_mac(dev.get("mac", "")),
                               "hostname": str(dev.get("hostname") or "").strip()}
        # Serial number, when supplied.
        serial = str(dev.get("serial") or "").strip()
        if serial:
            rec["serial"] = serial
        # Where the hostname came from (dns / reservation / lease), lower-cased.
        h_src = str(dev.get("hostname_source") or "").strip().lower()
        if h_src:
            rec["hostname_source"] = h_src
        # Copy the switch-sighting fields from their source_switch_* feed names.
        for rec_key, feed_key in (("switch_name", "source_switch_name"),
                                  ("switch_ip", "source_switch_ip"),
                                  ("switch_port", "source_switch_port")):
            val = str(dev.get(feed_key) or "").strip()
            if val:
                rec[rec_key] = val
        # Discovered model, when supplied.
        model = str(dev.get("model") or "").strip()
        if model:
            rec["model"] = model
        # Manufacturer, accepting 'vendor' as a synonym.
        manufacturer = str(dev.get("manufacturer") or dev.get("vendor") or "").strip()
        if manufacturer:
            rec["manufacturer"] = manufacturer
        # Per-record role, applied only when creating.
        role = str(dev.get("role") or "").strip()
        if role:
            rec["role"] = role
        # A full hub-learned model spec, passed through as-is.
        if isinstance(dev.get("spec"), dict):
            rec["spec"] = dev["spec"]
        # A non-empty probe profile, used later by guess_role.
        if isinstance(dev.get("profile"), dict) and dev["profile"]:
            rec["profile"] = dev["profile"]
        # Per-record custom fields, stringified (None becomes ''); empty keys are dropped.
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
        # Index every listed row of this sync's scope.
        for row in rows:
            # By lower-cased name (first row wins), including devices without an IP.
            rname = str(row.get("name") or "").strip().lower()
            if rname:
                run.by_name.setdefault(rname, row)
            # Read the row's custom fields for MAC and ownership checks.
            cf = row.get("custom_fields") or {}
            # MAC index: catches DHCP IP-moves + MAC-only records.
            rmac = self._norm_mac(cf.get("mac_address", ""))
            if rmac:
                run.by_mac.setdefault(rmac, row)
            # Serial index: survives IP/hostname churn.
            rserial = str(row.get("serial") or "").strip()
            if rserial and not _is_mgmt_row(row):
                run.by_serial.setdefault(rserial.lower(), row)
            # Only rows with a primary IP go into the IP index.
            addr = _primary_addr(row)
            if not addr:
                continue
            # IP index: the last row wins.
            run.by_ip[addr] = row
            # Remember IPs of devices we own: the only ones replace-delete may remove.
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
        indexing is unchanged). There is no tenant-only fallback: matching
        without the global MAC/serial index mints cross-tenant duplicates."""
        # Page through every device once (all tenants).
        all_rows = self._api_get_all("/api/dcim/devices/", {"limit": 500})
        # Unscoped sync: the full listing is what we index; no separate global list needed.
        if not run.tenant_slug:
            return all_rows, None
        # Tenant-scoped: keep only this tenant's rows, in listing order.
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
        # Collect primary-IP ids of every existing device that any incoming record could match.
        ids: list = []
        for key, rec in incoming.items():
            # Wrap the record so its identity fields are easy to read.
            d = _IncomingDevice(key, rec)
            # Look up the record in each index (serial, MAC, IP, name); misses yield None.
            for row in (run.by_serial.get(d.serial.lower()) if d.serial else None,
                        run.by_mac.get(d.mac) if d.mac else None,
                        run.by_ip.get(d.key) if d.keyed_by_ip else None,
                        run.by_name.get(d.hostname.lower()) if d.hostname else None):
                # Keep the primary IP id of each hit that was not deleted this batch.
                pip = (row or {}).get("primary_ip4")
                if (isinstance(pip, dict) and pip.get("id") is not None
                        and row["id"] not in run.deleted_ids):
                    ids.append(pip["id"])
        # De-duplicate the ids while preserving order.
        ids = list(dict.fromkeys(ids))
        # Fetch the IP rows in chunks of 100 ids per request.
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            # A failed chunk is logged; those IPs will be fetched one by one if needed.
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
        # A device deleted this batch has no Record.
        if dev_id in run.deleted_ids:
            return None
        # Reuse the live Record if we already have one.
        obj = run.device_objs.get(dev_id)
        # First use: build it from the listing row (no request) or fetch it once.
        if obj is None:
            row = run.device_rows.pop(dev_id, None)
            obj = (self._hydrate_record(row, self.nb.dcim.devices) if row is not None
                   else self.nb.dcim.devices.get(dev_id))
            # Cache it so every later write goes through the same Record.
            if obj is not None:
                run.device_objs[dev_id] = obj
        return obj

    def _ip_record(self, run: "_DeviceSyncRun", ip_id: Any):
        """Same as ``_device_record`` for an IP address: built from the
        prefetched row, else fetched once; None when NetBox has no such IP."""
        # Reuse the live IP Record if we already have one.
        obj = run.ip_objs.get(ip_id)
        # First use: build it from the prefetched row (no request) or fetch it once.
        if obj is None:
            row = run.ip_rows.pop(ip_id, None)
            obj = (self._hydrate_record(row, self.nb.ipam.ip_addresses) if row is not None
                   else self.nb.ipam.ip_addresses.get(ip_id))
            # Cache it for later writes in this batch.
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
        # Get the live Record; an already-deleted or missing device is a no-op.
        obj = self._device_record(run, row["id"])
        if obj is None:
            return False
        # Delete it; a 404 means someone else already deleted it, anything else is raised.
        try:
            obj.delete()
            deleted_now = True
        except pynetbox.RequestError as e:
            if getattr(getattr(e, "req", None), "status_code", None) != 404:
                raise
            deleted_now = False  # someone else already deleted it
        # Mark it deleted and drop its cached Record.
        run.deleted_ids.add(row["id"])
        run.device_objs.pop(row["id"], None)
        # NetBox cascades the delete to its IPs, so forget the cached primary IP too.
        pip = row.get("primary_ip4")
        if isinstance(pip, dict):
            run.forget_ip(pip.get("id"))
        return deleted_now

    # ── step 3: replace-delete ───────────────────────────────────────────────

    def _delete_vanished_owned_devices(self, run: "_DeviceSyncRun", incoming: dict) -> None:
        """``replace=True`` (tenant-scoped only — the caller checks): delete
        devices THIS source created (``owned_ips``) whose primary IP is absent
        from the incoming set. Human / other-source devices are never touched.
        A device carrying ``last_seen`` is left to the staleness sweep
        (offline after stale_days, deleted delete_days later): one feed cycle
        missing it is not proof it is gone. A failed delete is counted as an
        error, not raised."""
        # Owned IPs not present in the incoming feed are the stale ones.
        for ip_str in list(run.owned_ips - set(incoming.keys())):
            # Find the device row for that IP; skip if it is no longer indexed.
            row = run.by_ip.get(ip_str)
            if not row or (row.get("custom_fields") or {}).get("last_seen"):
                continue
            # Delete it; a failure is recorded as an error and the loop continues.
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
        # The default role for new devices (configured, else 'discovered').
        run.role = self._ensure_device_role(run.defaults.get("role") or "discovered")
        # Names of custom fields provisioned on dcim.device — the only ones a
        # per-record custom_fields map may write (an unknown key would 400 the
        # whole device upsert — 268f7ed).
        run.cf_whitelist = self._device_cf_whitelist()
        # The default device type and site for new devices.
        run.dtype = self._ensure_device_type(run.defaults.get("device_type") or "discovered")
        run.site = self._resolve_site(run.defaults.get("site") or "", run.tenant)

    # ── step 5: one record ───────────────────────────────────────────────────

    def _sync_one_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice") -> None:
        """Reconcile one incoming record: update the device it matches, else
        attach it to an operator-named device as a second port, else create a
        device we own. Exceptions propagate to ``sync_devices``, which counts
        them (``_note_upsert_failure``)."""
        # Look for an existing device; with none, try attaching as a named device's second port.
        row = self._match_existing_device(run, d)
        if row is None and self._join_operator_named_device(run, d):
            run.pushed += 1
            return
        # Matched: update it in place; otherwise create a new device we own.
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
        # Tier 1: serial number.
        row = None
        if d.serial:
            row = run.by_serial.get(d.serial.lower())
        # Tier 2: MAC address.
        if row is None and d.mac:
            row = run.by_mac.get(d.mac)
        # Tier 3: primary IP, only for records that carry a real IP.
        if row is None and d.keyed_by_ip:
            row = run.by_ip.get(d.key)
            # An untenanted device on that IP only counts if the IP is not another tenant's.
            if row is not None and not row.get("tenant") and not self._ip_is_tenants(run, row):
                row = None
        # Tier 4: hostname, subject to the adoption rules.
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
        # Find a same-name device; one already refreshed this batch is never re-adopted.
        cand = run.by_name.get(d.hostname.lower())
        if cand is None or cand["id"] in run.refreshed_ids:
            return None
        # Read the candidate's MAC and primary IP.
        ecf = cand.get("custom_fields") or {}
        emac = self._norm_mac(ecf.get("mac_address", ""))
        eaddr = _primary_addr(cand)
        # Bare = no identity; differs = both sides have a value and they disagree.
        cand_bare = not emac and not eaddr
        mac_differs = bool(d.mac) and bool(emac) and emac != d.mac
        ip_differs = bool(d.real_ip) and bool(eaddr) and eaddr != d.real_ip
        # Adopt a bare or owned device unless both MAC and IP prove it is a different machine.
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
        # Only an operator-asserted name with a real IP and hostname qualifies.
        if not (d.operator_named and d.real_ip and d.hostname):
            return False
        # Prefer a device created earlier this batch under the same name.
        owner = run.created_by_name.get(d.hostname.lower())
        # Else an existing same-name device that this source owns.
        if owner is None:
            cand = run.by_name.get(d.hostname.lower())
            if cand is not None and run.owns(cand.get("custom_fields") or {}):
                owner = cand["id"]
        # No owning device found: let the caller match/create normally.
        if owner is None:
            return False
        # Attach the address; whatever happens, drop cached copies of any IP that was written.
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
        # Protect this device from later reclaim/rename in the same batch.
        run.refreshed_ids.add(row["id"])
        rname = str(row.get("name") or "").strip().lower()
        if rname:
            run.used_names.add(rname)
        # Steps that apply to both modes: tenant adoption, type/role upgrade, site placement.
        self._adopt_untenanted_device(run, row)
        self._identify_matched_device(run, d, row)
        self._site_matched_device(run, d, row)
        # NetBox is the source of truth: only fill empty values.
        if run.source_of_truth == "netbox":
            self._fill_device_gaps(run, row, d.hostname, d.mac)
            # An empty primary IP is a gap too (e.g. the device was deleted
            # and re-created by a feed that had no IP for it).
            if d.real_ip and not _primary_addr(row):
                self._assign_primary_ip(run, d, row, "")
            self._touch_last_seen_if_stale(run, row, d.key, "last_seen refresh")
        # The feed is the source of truth: refresh IP, custom fields and name.
        else:
            self._sync_matched_primary_ip(run, d, row)
            self._stamp_matched_device_fields(run, d, row)
            self._rename_or_touch_matched_device(run, d, row)
        # Count the device as successfully pushed.
        run.pushed += 1

    def _ip_is_tenants(self, run: "_DeviceSyncRun", row: dict) -> bool:
        """True when the untenanted ``row``'s primary IP belongs to this sync's
        tenant (or to none): overlapping VRFs mean the same address can be
        another tenant's."""
        # No tenant on this sync: any IP is acceptable.
        if not run.tenant:
            return True
        # Fetch the primary IP; a lookup failure is treated as "not ours" to be safe.
        try:
            ip = self.nb.ipam.ip_addresses.get((row.get("primary_ip4") or {}).get("id"))
        except Exception:  # noqa: BLE001
            return False
        # Ours when the IP has no tenant or this sync's tenant.
        t = getattr(ip, "tenant", None) if ip else None
        return ip is not None and (t is None or getattr(t, "id", None) == run.tenant.id)

    def _adopt_untenanted_device(self, run: "_DeviceSyncRun", row: dict) -> None:
        """Give a matched device with no tenant this sync's tenant. Best-effort."""
        # Nothing to do when the device already has a tenant or this sync has none.
        if row.get("tenant") or not run.tenant:
            return
        # Set the tenant and save; on failure drop the cached Record and log.
        try:
            devobj = self._device_record(run, row["id"])
            if devobj:
                devobj.tenant = run.tenant.id
                devobj.save()
                # Keep the listing row in step so later steps see the new tenant.
                row["tenant"] = {"id": run.tenant.id, "slug": run.tenant_slug}
        except Exception as e:  # noqa: BLE001
            run.forget_device(row["id"])
            logger.debug("sync_devices: adopt untenanted device %s: %s", row.get("id"), e)

    def _site_matched_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                             row: dict) -> None:
        """A matched device still in the fallback site joins its prefix's site."""
        # Only devices with a real IP that still sit in the fallback site are candidates.
        if not d.real_ip or _ref_slug(row.get("site")) != FALLBACK_SITE_SLUG:
            return
        # Skip when no site-scoped prefix covers the IP.
        if not self._site_for_ip(d.real_ip, run.tenant):
            return
        # Move it and save; on failure drop the cached Record and log.
        try:
            devobj = self._device_record(run, row["id"])
            if devobj and self._place_in_prefix_site(devobj, d.real_ip, run.tenant):
                devobj.save()
        except Exception as e:  # noqa: BLE001
            run.forget_device(row["id"])
            logger.debug("sync_devices: site %s failed: %s", d.key, e)

    def _identify_matched_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                                 row: dict) -> None:
        """Give a matched device the real type/role this record carries when
        the device is still generic. Types and roles are otherwise set on
        create only, so a device first seen by an ARP/DHCP feed stayed
        'Discovered' even after the console/poll learned its model.
        Best-effort."""
        # Identity the record carries: a model, and an explicit or guessed role.
        model = d.rec.get("model", "")
        rec_role = d.rec.get("role", "") or guess_role(
            d.hostname if d.has_hostname else row.get("name", ""), d.rec.get("profile"), d.mac)
        # Nothing to upgrade to.
        if not (model or rec_role):
            return
        # Slug of the bare model-named type LM may have created earlier.
        mfr = d.rec.get("manufacturer", "")
        raw_type = self._slugify(f"{mfr}-{model}" if mfr else model) if model else ""
        # Leave devices whose type and role were both set to something specific.
        if (_ref_slug(row.get("device_type")) not in (_GENERIC_SLUG, raw_type)
                and _ref_slug(row.get("role")) != _GENERIC_SLUG):
            return
        # Resolve the target type/role and apply them only where still generic; save on change.
        try:
            devobj = self._device_record(run, row["id"])
            dtype, replaceable = self._identified_device_type(model, mfr, d.rec.get("spec"))
            role = self._ensure_device_role(rec_role) if rec_role else None
            if devobj and self._upgrade_generic_identity(devobj, dtype, role, replaceable):
                devobj.save()
        # Failure: drop the cached Record and log.
        except Exception as e:
            run.forget_device(row["id"])
            logger.debug("sync_devices: identify %s failed: %s", d.key, e)

    def _touch_last_seen_if_stale(self, run: "_DeviceSyncRun", row: dict, key: str,
                                  what: str) -> None:
        """Refresh the device's ``last_seen`` — but only when the LISTED row's
        stamp is ≥1h old (8095627 / perf FIX A): a fresh stamp skips the
        per-device GET + PATCH entirely. Best-effort."""
        # Only refresh when the listed stamp is old enough; failures are logged and the cache
        # dropped.
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
        # Current primary IP id and address of the matched device.
        pip = row.get("primary_ip4") or {}
        ip_id = pip.get("id") if isinstance(pip, dict) else None
        match_addr = _primary_addr(row)
        # Same IP (or none incoming): refresh the existing IP record.
        if ip_id and (not d.real_ip or match_addr == d.real_ip):
            self._refresh_primary_ip(run, d, ip_id)
        # Different IP: assign the incoming address as the new primary.
        elif d.real_ip:
            self._assign_primary_ip(run, d, row, match_addr)

    def _refresh_primary_ip(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                            ip_id: Any) -> None:
        """Write the record's MAC (``custom_fields.mac_address`` — what the
        NetBox→CPPM endpoint sync keys on) and hostname (``dns_name``) onto
        the device's existing primary IP, then mark it seen. Only changed
        values are sent; an unchanged IP costs no request. Best-effort."""
        # Best-effort: failures drop the cached IP and are logged.
        try:
            # Get the live IP Record.
            ipobj = self._ip_record(run, ip_id)
            if ipobj:
                # Refresh dns_name from a usable hostname.
                if d.has_hostname:
                    ipobj.dns_name = d.hostname
                # A changed MAC is written via a delta PATCH (which also sends dns_name).
                if d.mac and (ipobj.custom_fields or {}).get("mac_address") != d.mac:
                    self._save_cf_delta(ipobj, {"mac_address": d.mac})
                else:
                    ipobj.save()  # no-op unless dns_name changed
                # Mark the IP as recently seen.
                self._stamp_last_seen(ipobj)
        except Exception as e:
            run.forget_ip(ip_id)
            logger.debug("sync_devices: refresh IP %s failed: %s", d.key, e)

    def _assign_primary_ip(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                           row: dict, match_addr: str) -> None:
        """Give a matched device the incoming IP as its primary_ip4 (new
        ``mgmt`` interface + reused-or-created IP). Best-effort."""
        # Best-effort: failures drop the cached device and are logged.
        try:
            # Create/reuse the mgmt interface and IP, then make it the primary IPv4.
            ipobj, mask = self._attach_mgmt_ip(run, d, row["id"])
            devobj = self._device_record(run, row["id"])
            if devobj:
                devobj.primary_ip4 = ipobj.id
                devobj.save()
            # Journal the adoption and mark the IP seen.
            self._journal("ipam.ipaddress", ipobj.id, run.source_tag,
                          note=f"IP {d.real_ip}/{mask} → adopted")
            self._stamp_last_seen(ipobj)
            # The device moved to a new address — it is NOT stale. Stop
            # replace-delete from reaping it for its OLD primary IP.
            if match_addr and match_addr != d.real_ip and match_addr in run.owned_ips:
                run.owned_ips.discard(match_addr)
        # Failure: drop the cached device Record so the unsent change is not retried.
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
        # Reuse an existing mgmt interface (its IP was deleted) — a second one
        # with the same name is rejected by NetBox.
        iface = (self.nb.dcim.interfaces.get(device_id=device_id, name="mgmt")
                 or self.nb.dcim.interfaces.create(device=device_id, name="mgmt", type="other"))
        # Mask from the containing prefix (tenant VRF aware).
        mask = self._mask_for_ip(d.real_ip, run.tenant)
        # IP payload assigned to the mgmt interface.
        ip_kwargs: Dict[str, Any] = {
            "address": f"{d.real_ip}/{mask}",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface.id,
        }
        # Add tenant and dns_name when known.
        if run.tenant:
            ip_kwargs["tenant"] = run.tenant.id
        if d.has_hostname:
            ip_kwargs["dns_name"] = d.hostname
        # Reuse the existing IPAM entry for this address if any, else create it.
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
        # Per-record custom fields are only written onto devices this source owns.
        we_own = run.owns(row.get("custom_fields") or {})
        # Best-effort: failures drop the cached device and are logged.
        try:
            # Get the live device Record and its current custom fields.
            devobj = self._device_record(run, row["id"])
            if devobj:
                current = devobj.custom_fields or {}
                # Build the custom fields we want: MAC and switch sighting, when present.
                wanted: Dict[str, Any] = {}
                if d.mac:
                    wanted["mac_address"] = d.mac
                if not self._keeps_switch_sighting(run, row, current, d):
                    for cf_key in _SWITCH_CF_KEYS:
                        if d.rec.get(cf_key):
                            wanted[cf_key] = d.rec[cf_key]
                # Owned devices also take whitelisted, non-sink-managed per-record fields.
                if we_own:
                    for ck, cv in (d.rec.get("custom_fields") or {}).items():
                        if ck in run.cf_whitelist and ck not in self._SINK_MANAGED_DEVICE_CFS:
                            wanted[ck] = cv
                # Only send fields whose value actually changes.
                delta = {k: v for k, v in wanted.items() if current.get(k) != v}
                # Fill the serial number only when the device has none.
                ser_changed = False
                if d.serial and not str(getattr(devobj, "serial", "") or "").strip():
                    devobj.serial = d.serial
                    ser_changed = True
                # Send the delta PATCH (it also carries the serial), or a plain save for a serial
                # only.
                if delta:
                    self._save_cf_delta(devobj, delta)  # also sends the serial
                elif ser_changed:
                    devobj.save()
        except Exception as e:
            run.forget_device(row["id"])
            logger.debug("sync_devices: device cf stamp %s: %s", d.key, e)

    @staticmethod
    def _keeps_switch_sighting(run: "_DeviceSyncRun", row: dict, current: dict,
                               d: "_IncomingDevice") -> bool:
        """True when this record must not move the device's switch/port.

        A MAC is seen by every switch on its path, and several tenants' nw
        agents push the same MAC to the one NetBox device. Each push writing
        its own view flipped switch_name/switch_port twice a cycle on ~5,500
        devices a day, which is most of NetBox's write load. So:

        * another tenant's device keeps the location its own tenant recorded;
        * a physical port is never replaced by a logical one (LAG, VXLAN, SVI).
        """
        row_tid = (row.get("tenant") or {}).get("id")
        if run.tenant and row_tid and row_tid != run.tenant.id:
            return True
        have = str(current.get("switch_port") or "")
        new = str(d.rec.get("switch_port") or "")
        return bool(have) and not _LOGICAL_PORT_RE.match(have) and bool(_LOGICAL_PORT_RE.match(new))

    def _rename_or_touch_matched_device(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                                        row: dict) -> None:
        """Rename the matched device to the record's hostname and mark it seen
        — but only when we OWN it, or it still has an auto placeholder name
        another feed minted (nw's ``device-<mac>``); a human's name is never
        clobbered (c245c7e). An owned device takes the hostname as-is
        (upper-cased, f15f672); a placeholder takes a uniquified name.
        Otherwise just refresh ``last_seen`` when stale."""
        # Rename is allowed only for devices we own or ones still carrying a placeholder name.
        we_own = run.owns(row.get("custom_fields") or {})
        placeholder = bool(_PLACEHOLDER_NAME_RE.match(str(row.get("name") or "").strip()))
        # Not allowed (or no hostname): just refresh last_seen if stale.
        if not ((we_own or placeholder) and d.has_hostname):
            self._touch_last_seen_if_stale(run, row, d.key, "last_seen")
            return
        # Best-effort: failures drop the cached device and are logged.
        try:
            # Owned devices take the hostname as-is; placeholders take a unique variant of it.
            devobj = self._device_record(run, row["id"])
            if devobj:
                devobj.name = (self._nb_name(d.hostname) if we_own else
                               self._uniq_device_name(d.hostname, d.mac, d.real_ip,
                                                      run.by_name, run.used_names))
                # Save, reserve the name for this batch and mark the device seen.
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
        # An IP NetBox already has on a VM belongs to the hypervisor sync: skip it.
        if d.real_ip and self._ip_on_vm(d.real_ip):
            run.skipped += 1  # the hypervisor sync owns VM addresses
            return
        # Choose a free name (possibly reclaiming one from our own stale copy).
        name = self._claim_create_name(run, d, d.default_name)
        # A prior create already 400'd on a missing site — the same 400 hits
        # every record, so stop attempting creates (count once each, one
        # warning) instead of spamming an identical error (ccb5034).
        if run.site_required_abort:
            run.errors += 1
            return
        # Canonical upper-case name, then create the device.
        name = self._nb_name(name)
        devobj = self.nb.dcim.devices.create(**self._device_create_kwargs(run, d, name))
        self._global_devices_cache = None  # next listing must see it
        # Remember the device by hostname so a second operator-named port can join it.
        if d.hostname:
            run.created_by_name.setdefault(d.hostname.lower(), devobj.id)
        # Stamp ownership/custom fields and journal the creation.
        self._stamp_created_device(run, d, devobj)
        self._journal("dcim.device", devobj.id, run.source_tag, note=f"device {name}")
        # With a real IP: create the mgmt interface + IP and make it primary.
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
        # Count the new device as pushed.
        run.pushed += 1

    def _ip_on_vm(self, ip: str) -> bool:
        """True when NetBox has ``ip`` (any VRF) on a VM interface: a sighting
        of a VM must not mint a device copy of it. Best-effort: False on error."""
        # Look up every IPAM entry for this address; a lookup failure means "not a VM".
        try:
            rows = self._api_get_all("/api/ipam/ip-addresses/", {"address": ip})
        except Exception as e:
            logger.debug("sync_devices: VM-IP lookup %s failed: %s", ip, e)
            return False
        # True if any of them is assigned to a VM interface.
        return any(r.get("assigned_object_type") == "virtualization.vminterface"
                   for r in rows or [] if isinstance(r, dict))

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
        # Is the name held by a pre-existing device that was not refreshed this batch?
        byname = run.by_name.get(name.lower())
        if byname and byname["id"] not in run.refreshed_ids:
            # Our own stale copy of this machine: delete it and take the name back.
            if (self._is_reclaimable(run, d, byname, name)
                    and name.lower() not in run.used_names):
                try:
                    if self._delete_device(run, byname):
                        run.deleted += 1
                except Exception as e:
                    run.note_error(f"delete-stale {name}: {e}")
                    logger.debug("sync_devices: delete stale by-name %s failed: %s", name, e)
            # Someone else's device: keep it and pick a unique variant of the name.
            else:
                orig = name
                name = self._uniq_device_name(name, d.mac, d.real_ip,
                                              run.by_name, run.used_names)
                logger.debug("sync_devices: name %s taken; creating as %s", orig, name)
        # Name already taken by an earlier record in this batch: pick a unique variant.
        elif name.lower() in run.used_names:
            orig = name
            name = self._uniq_device_name(name, d.mac, d.real_ip,
                                          run.by_name, run.used_names)
            logger.debug("sync_devices: hostname %s already used this batch; "
                         "creating as %s", orig, name)
        # Reserve the chosen name for the rest of the batch.
        run.used_names.add(name.lower())
        return name

    def _is_reclaimable(self, run: "_DeviceSyncRun", d: "_IncomingDevice",
                        holder: dict, name: str) -> bool:
        """True when the device holding ``name`` is OUR stale copy of the same
        machine: we own it (an unowned/human device never qualifies) AND its
        MAC cf equals the record's MAC, OR its primary IP equals the record's
        IP, OR the name is the MAC-derived ``device-<mac>`` form (a no-hostname
        DHCP IP-move of one of our orphans)."""
        # Never reclaim a device this source does not own.
        hcf = holder.get("custom_fields") or {}
        if not run.owns(hcf):
            return False
        # Same machine if MAC matches, IP matches, or the name is our device-<mac> placeholder.
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
        # Base fields: name, active status and the serial when known.
        kwargs: Dict[str, Any] = {"name": name, "status": "active"}
        if d.serial:
            kwargs["serial"] = d.serial
        # Role: per-record first, else a guess when the batch default is generic, else the default.
        rec_role = d.rec.get("role")
        if not rec_role and _ref_slug(run.role) in ("", _GENERIC_SLUG):
            rec_role = guess_role(d.hostname if d.has_hostname else "", d.rec.get("profile"), d.mac)
        create_role = self._ensure_device_role(rec_role) if rec_role else run.role
        if create_role:
            kwargs["role"] = create_role.id
        # Device type: the identified type when the record has one, else the batch default.
        rec_dtype = self._identified_device_type(
            d.rec.get("model", ""), d.rec.get("manufacturer", ""), d.rec.get("spec"))[0] or run.dtype
        if rec_dtype:
            kwargs["device_type"] = rec_dtype.id
        # Site: the prefix's site when the IP has one, else the batch default site.
        ip_site = self._site_for_ip(d.real_ip, run.tenant) if d.real_ip else None
        if ip_site:
            kwargs["site"] = ip_site["id"]
        elif run.site:
            kwargs["site"] = run.site.id
        # Attribute to the sync's tenant.
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
        # Best-effort: a missing custom field in NetBox makes the save fail; that is just logged.
        try:
            # Start from current values and add the ownership tag and a UTC last-seen timestamp.
            merged = dict(devobj.custom_fields or {})
            merged["discovered_from"] = run.source_tag
            merged["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            # Add MAC and switch sighting fields when present.
            if d.mac:
                merged["mac_address"] = d.mac
            for cf_key in _SWITCH_CF_KEYS:
                val = d.rec.get(cf_key)
                if val:
                    merged[cf_key] = val
            # Add whitelisted per-record fields that the sink does not manage itself.
            for ck, cv in (d.rec.get("custom_fields") or {}).items():
                if ck in run.cf_whitelist and ck not in self._SINK_MANAGED_DEVICE_CFS:
                    merged[ck] = cv
            # Write all custom fields in one save.
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
        # Count it and remember the first error text.
        run.note_error(f"upsert {key}: {e}")
        # A "site required" error will repeat for every create: stop creating and warn once.
        es = str(e).lower()
        if not run.site_required_abort and "site" in es and "required" in es:
            run.site_required_abort = True
            logger.warning(
                "[sync-error] sync_devices tenant=%s: NetBox requires a "
                "site for device creation but none resolved (defaults.site="
                "%r) — set a valid 'site' slug for this sync or create a "
                "site in NetBox; skipping remaining creates.",
                run.tenant_slug or "—", run.defaults.get("site"))
        # Every failure is also logged at debug level.
        logger.debug("sync_devices: upsert %s failed: %s", key, e)

    @staticmethod
    def _sync_devices_result(run: "_DeviceSyncRun", devices_total: int) -> Dict[str, Any]:
        """The SUCCESS envelope. The first per-record failure is appended to
        the message (the hub status UI shows it) and logged at WARNING, so the
        cause is visible without DEBUG logs."""
        # Summary line with the batch counters.
        msg = (f"{run.pushed} device(s) upserted, {run.deleted} deleted, "
               f"{run.skipped} skipped, {run.errors} errors")
        # With errors: append the first error and log at WARNING; else INFO.
        if run.errors and run.first_err:
            msg += f" — first error: {run.first_err}"
            logger.warning("sync_devices tenant=%s: %s", run.tenant_slug or "<global>", msg)
        else:
            logger.info("sync_devices tenant=%s: %s", run.tenant_slug or "<global>", msg)
        # Return the success envelope.
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
        by name (MAC via :meth:`_set_iface_mac`, ``speed``) with a
        reused/created IP attached. ``replace=True`` deletes interfaces we
        created (``nw_managed`` marker) whose name is absent from the incoming
        set — so NetBox tracks the live switch without touching manually-created
        interfaces.

        On CREATE the polled ``model`` gives the device_type and the
        ``object_type`` the role (switch/gateway); ``defaults`` (``device_type``/
        ``role``/``site`` slugs) fill what the poll can't — site always. A
        missing value returns an ERROR naming it (never silently fail). An
        existing match still on the generic 'discovered' type/role is upgraded
        the same way.

        Returns ``{status, pushed, errors, skipped, deleted, interfaces_total,
        device_id, message}``.
        """
        # Result counters and the first error text for the summary.
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None
        # Default inputs: no defaults dict, and the nw ownership tag.
        defaults = defaults or {}
        source_tag = str(source or "Network Devices").strip() or "Network Devices"
        # Any unexpected failure is caught at the bottom and reported as an ERROR envelope.
        try:
            # Make sure the custom fields we write exist in NetBox.
            self._ensure_custom_fields()
            # Pull the identity fields out of the polled device.
            dev = device or {}
            nw_id = str(dev.get("id") or "").strip()
            name = str(dev.get("name") or "").strip()
            serial = str(dev.get("serial") or "").strip()
            # Base MAC (either key); anything that is not 12 hex digits is discarded.
            base_mac = self._norm_mac(dev.get("mac") or dev.get("base_mac") or "")
            if not re.fullmatch(r"[0-9a-f]{12}", re.sub(r"[:.\-]", "", base_mac)):
                base_mac = ""
            # Bare management IP and the polled model.
            mgmt_ip = str(dev.get("address") or "").strip().split("/")[0].strip()
            model = str(dev.get("model") or "").strip()
            # Role and manufacturer implied by the poll's object_type (e.g. cx_switch -> Aruba
            # switch).
            ident_role, ident_mfr = _NW_OBJECT_TYPE_IDENTITY.get(
                str(dev.get("object_type") or "").strip().lower(), ("", ""))
            # Hub-learned model spec (LLM-identified once per model).
            spec = dev.get("spec") if isinstance(dev.get("spec"), dict) else None
            # Use the spec's role when object_type did not give one ('other' is not useful).
            if spec and not ident_role and spec.get("role") != "other":
                ident_role = str(spec.get("role") or "")
            # Without a name or id we cannot identify or name the device.
            if not name and not nw_id:
                return {"status": "ERROR", "message": "nw device has no name/id",
                        "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                        "interfaces_total": len(interfaces or []), "device_id": None}
            # Fall back to a name derived from the fleet id.
            if not name:
                name = f"nw-{nw_id[:8]}" if nw_id else "nw-device"

            # Case-insensitive tenant resolve (slug OR name → canonical slug),
            # mirroring sync_vms' lookup so a mixed-case/none tenant still lands.
            tenant = None
            if tenant_slug:
                tenant = self._resolve_tenant_ci(tenant_slug)
            if tenant:
                self._move_tenant_ips_to_vrf(tenant)

            # ── Resolve the existing device via the canonical ladder ──────────
            # SERIAL → MAC → nw_device_id → mgmt-IP → name. Serial and the
            # switch's base MAC (``device.mac`` from the nw info command) are the
            # hardware identities; routing through the shared resolver means a device
            # another feeder created (console by serial, ARP/fw by IP) is found
            # and updated in place instead of spawning an nw_device_id-keyed
            # duplicate of the same box.
            # List the tenant's devices (or all); failing here aborts with an ERROR.
            # The cached global listing is filtered here instead of paging the
            # tenant again on every push.
            try:
                rows = self._all_devices_cached()
                if tenant:
                    rows = [r for r in rows
                            if isinstance(r.get("tenant"), dict)
                            and r["tenant"].get("id") == tenant.id]
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                        "interfaces_total": len(interfaces or []), "device_id": None}
            # Index them, and add global MAC/serial identities when tenant-scoped.
            idx = self._index_existing_devices(rows)
            if tenant:
                self._add_global_identities(idx["by_mac"], idx["by_serial"],
                                            by_serial_mgmt=idx["by_serial_mgmt"])
            # A management controller shares its server's serial: it only ever
            # matches other controller records by serial, never the server.
            is_mgmt = ident_role in _MGMT_ROLE_SLUGS
            # Walk the canonical ladder to find an existing device for this box.
            existing = self._resolve_existing_device(
                serial=serial, mac=base_mac, nw_device_id=nw_id, ip=mgmt_ip,
                hostname=name,
                by_serial=idx["by_serial_mgmt"] if is_mgmt else idx["by_serial"],
                by_mac=idx["by_mac"], by_nw=idx["by_nw"],
                by_ip=idx["by_ip"], by_name=idx["by_name"])
            # Whatever tier matched, a controller and a server are different objects.
            if existing is not None and _is_mgmt_row(existing) != is_mgmt:
                existing = None

            # ── Create the device if missing (defaults required) ────────────────
            if existing is None:
                # The polled model and object_type identify the device; the
                # configured defaults only fill in what they can't.
                # Gather the type, role and site we would use for a new device.
                model_dt, _ = self._identified_device_type(model, ident_mfr, spec)
                dt_slug = str(defaults.get("device_type") or "").strip()
                role_slug = ident_role or str(defaults.get("role") or "").strip()
                site_slug = str(defaults.get("site") or "").strip()
                ip_site = self._site_for_ip(mgmt_ip, tenant)
                # List any required value that is still empty.
                missing = [k for k, v in (("device_type", model_dt or dt_slug),
                                         ("role", role_slug),
                                         ("site", ip_site or site_slug)) if not v]
                # Report missing defaults clearly instead of failing obscurely.
                if missing:
                    return {"status": "ERROR",
                            "message": f"cannot create nw device {name!r}: missing "
                                       f"default(s): {', '.join(missing)} "
                                       f"(set them in Setup → Network Devices → "
                                       f"IPAM Sync defaults)",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "interfaces_total": len(interfaces or []), "device_id": None}
                # Resolve the actual NetBox objects; any failure becomes an ERROR result.
                try:
                    # Device type: identified model type, else the configured default slug.
                    dt = model_dt or self.nb.dcim.device_types.get(slug=dt_slug)
                    # perf-scan D1b: role + site TTL-cached — a fleet POLL NOW
                    # otherwise re-fetches the same two slugs once per device.
                    # Role: the identified role (auto-created), else the configured slug (cached).
                    if ident_role:
                        role = self._ensure_device_role(ident_role)
                    else:
                        role = self._cached_ref(
                            "device_role", role_slug,
                            lambda: self.nb.dcim.device_roles.get(slug=role_slug))
                    # The site of the prefix holding the management IP beats the default.
                    site = (SimpleNamespace(**ip_site) if ip_site else self._cached_ref(
                        "site", site_slug,
                        lambda: self.nb.dcim.sites.get(slug=site_slug)))
                    # A configured-but-nonexistent slug resolves to None; guard
                    # so it's a clear, actionable error instead of an
                    # AttributeError on .id.
                    _unresolved = [k for k, v in (("device_type", dt),
                                                  ("role", role),
                                                  ("site", site)) if v is None]
                    # A configured slug that does not exist: report which ones.
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
                    # Build the create payload: canonical name, type, role, site, plus
                    # serial/tenant.
                    ck: Dict[str, Any] = {"name": self._nb_name(name), "device_type": dt.id,
                                          "role": role.id, "site": site.id}
                    if serial:
                        ck["serial"] = serial
                    if tenant:
                        ck["tenant"] = tenant.id
                    # Create the device.
                    devobj = self.nb.dcim.devices.create(**ck)
                    self._global_devices_cache = None  # next listing must see it
                    # Tag ownership + link to the fleet id (best-effort cf PATCH).
                    try:
                        devobj.custom_fields = {
                            "discovered_from": source_tag,
                            "nw_device_id": nw_id,
                            "last_seen": datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"),
                        }
                        # Record the base MAC too when known.
                        if base_mac:
                            devobj.custom_fields["mac_address"] = base_mac
                        devobj.save()
                    except Exception as e:
                        logger.warning("sync_nw_device: tag new device %s skipped: %s",
                                       nw_id or name, e)
                    # Journal the creation.
                    self._journal("dcim.device", devobj.id, "nw-poll",
                                  note=f"NW device {name} ({nw_id or 'no id'})")
                # Creation failed: report it as an ERROR result.
                except Exception as e:
                    return {"status": "ERROR",
                            "message": f"create nw device {name!r} failed: {e}",
                            "pushed": 0, "errors": 1, "skipped": 0, "deleted": 0,
                            "interfaces_total": len(interfaces or []), "device_id": None}
            # Matched an existing device: fetch its live Record.
            else:
                devobj = self.nb.dcim.devices.get(existing["id"])
                # It disappeared since the listing: report it.
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
                    # Copy the custom fields, then set ownership and fleet id.
                    cf = dict(devobj.custom_fields or {})
                    cf["discovered_from"] = source_tag
                    if nw_id:
                        cf["nw_device_id"] = nw_id
                    # Fill the MAC only if the device has none.
                    if base_mac and not str(cf.get("mac_address") or "").strip():
                        cf["mac_address"] = base_mac
                    # Refresh last_seen only when stale, so unchanged devices produce no PATCH.
                    if self._last_seen_stale(cf.get("last_seen")):
                        cf["last_seen"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                    # Apply the updated custom fields.
                    devobj.custom_fields = cf
                    # Backfill the native serial only when the matched device
                    # has none — hardware truth fills a gap, but never clobber a
                    # serial another feeder already set.
                    if serial and not str(getattr(devobj, "serial", "") or "").strip():
                        devobj.serial = serial
                    # Move it out of the fallback site if its management IP has a site.
                    self._place_in_prefix_site(devobj, mgmt_ip, tenant)
                    # The device may have been created generic by an ARP/DHCP
                    # feed before this poll learned what it is.
                    known_dt, replaceable = self._identified_device_type(model, ident_mfr, spec)
                    self._upgrade_generic_identity(
                        devobj, known_dt,
                        self._ensure_device_role(ident_role) if ident_role else None,
                        replaceable)
                    # Save all changes in one PATCH; a failure is only logged.
                    devobj.save()
                except Exception as e:
                    logger.debug("sync_nw_device: refresh device cf skipped: %s", e)
                # Matched a device another feeder auto-named (DEVICE-<mac> from
                # ARP, or a bare IP): the polled hostname is the real name. Only
                # the fleet's own name counts, never the nw-<id> fallback.
                # A box matched by hardware identity that now reports a new
                # hostname was renamed on the device: follow it.
                try:
                    cfs = existing.get("custom_fields") or {}
                    same_box = ((nw_id and str(cfs.get("nw_device_id") or "") == nw_id)
                                or (serial and str(existing.get("serial") or "") == serial)
                                or (base_mac and self._norm_mac(cfs.get("mac_address", "")) == base_mac))
                    self._adopt_real_name(devobj, str(dev.get("name") or ""),
                                          idx["by_name"], source="nw-poll",
                                          force=bool(same_box), expect_mgmt=is_mgmt)
                except Exception as e:
                    logger.debug("sync_nw_device: adopt name skipped: %s", e)

            # Id of the device we are working on (new or existing).
            dev_id = devobj.id

            # ── Index existing interfaces on this device by name (lowercased) ──
            # Map lower-cased interface name -> interface Record; a listing failure is logged.
            existing_ifaces: Dict[str, Any] = {}
            try:
                for ifc in self.nb.dcim.interfaces.filter(device_id=dev_id):
                    existing_ifaces[str(getattr(ifc, "name", "") or "").lower()] = ifc
            except Exception as e:
                logger.warning("sync_nw_device: list interfaces for %s failed: %s",
                               dev_id, e)

            # Names seen in the poll, used later to find interfaces that disappeared.
            incoming_names: set = set()
            # Upsert each polled interface (non-dict rows ignored).
            for ifc in (interfaces or []):
                if not isinstance(ifc, dict):
                    continue
                # An interface without a name cannot be matched; count it as skipped.
                ifname = str(ifc.get("name") or "").strip()
                if not ifname:
                    skipped += 1
                    continue
                # Record the name and normalise MAC and status (up/active/1 -> active, else
                # planned).
                incoming_names.add(ifname.lower())
                mac = self._norm_mac(ifc.get("mac", ""))
                status = "active" if str(ifc.get("status") or "").strip().lower() in (
                    "up", "active", "1") else "planned"
                # Parse the speed as an integer; garbage becomes 0 (unknown).
                speed_val = ifc.get("speed")
                try:
                    sv = int(speed_val) if speed_val not in (None, "") else 0
                except (TypeError, ValueError):
                    sv = 0
                # ifSpeed is bps; NetBox interface speed is Kbps. Convert when the
                # value is clearly bps (>=1e6); else assume already Kbps.
                speed_kbps = sv // 1000 if sv >= 1_000_000 else sv
                # Bare interface IP and VLAN label from the poll.
                ip = str(ifc.get("ip") or "").strip().split("/")[0].strip()
                vlan = str(ifc.get("vlan") or "").strip()
                # Per-interface failures are counted below without aborting the other interfaces.
                try:
                    # Find the interface by name in the cached listing.
                    match = existing_ifaces.get(ifname.lower())
                    if match is None:
                        # The device listing can miss an interface NetBox
                        # already has; creating it again only fails as a dupe.
                        match = self.nb.dcim.interfaces.get(device_id=dev_id,
                                                            name=ifname)
                    # Not in NetBox: create it with its speed (the MAC is set below).
                    if match is None:
                        ik: Dict[str, Any] = {"device": dev_id, "name": ifname,
                                              "type": "other"}
                        if speed_kbps > 0:
                            ik["speed"] = speed_kbps
                        # VLAN: best-effort tagged vlan (NetBox expects a list of
                        # vlan ids or a single untagged vlan id). We only have a
                        # vlan label/number from the poll; skip unless numeric.
                        iface_obj = self.nb.dcim.interfaces.create(**ik)
                        # Mark it as managed by the nw sync so replace-delete may remove it later.
                        try:
                            iface_obj.custom_fields = {"nw_managed": "true"}
                            iface_obj.save()
                        except Exception as e:
                            logger.debug("sync_nw_device: tag new iface %s skipped: %s",
                                         ifname, e)
                    # Exists: update the speed only when it changed.
                    else:
                        iface_obj = match
                        # NetBox interfaces have no ``status`` field: setting one made every
                        # interface look changed and PATCHed it on every poll.
                        if speed_kbps > 0 and int(getattr(iface_obj, "speed", 0) or 0) != speed_kbps:
                            iface_obj.speed = speed_kbps
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

                    # Set the interface MAC (its own NetBox record). A failure is
                    # logged; the interface and its IP still sync.
                    try:
                        self._set_iface_mac(iface_obj, mac)
                    except Exception as e:
                        logger.warning("[sync-error] nw-poll: MAC %s on %s failed: %s",
                                       mac, ifname, e)

                    # Per-interface IP (reuse global record, attach to the iface).
                    if ip:
                        # Build the IP payload: address with mask, assigned to this interface,
                        # tenant if any.
                        mask = self._mask_for_ip(ip, tenant)
                        ip_kwargs = {
                            "address": f"{ip}/{mask}",
                            "assigned_object_type": "dcim.interface",
                            "assigned_object_id": iface_obj.id,
                        }
                        if tenant:
                            ip_kwargs["tenant"] = tenant.id
                        # Reuse/create the IP; a failure here is logged without failing the
                        # interface.
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
                            # Mark the IP as recently seen.
                            self._stamp_last_seen(ipobj)
                        # IP step failed: count it, keep the first error, and continue with the
                        # interface.
                        except Exception as e:
                            errors += 1
                            if first_err is None:
                                first_err = f"iface {ifname} ip {ip}: {e}"
                            logger.debug("sync_nw_device: iface %s ip %s failed: %s",
                                         ifname, ip, e)
                    # The interface itself was synced.
                    pushed += 1
                # Interface step failed: count it and keep the first error text.
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"iface {ifname}: {e}"
                    logger.debug("sync_nw_device: iface %s failed: %s", ifname, e)

            # ── Replace-delete: drop nw-managed interfaces no longer reported ──
            # A poll that returned no interfaces at all failed to read them
            # (spoke restarting, CLI timeout); it never means the box lost
            # every port, so it must not wipe the device's interfaces.
            for ifname_l, ifc_obj in (list(existing_ifaces.items()) if incoming_names else []):
                    # Skip interfaces that are still reported.
                    if ifname_l in incoming_names:
                        continue
                    # Delete only interfaces this sync created/adopted; failures are counted.
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

            # Another feeder (firewall ARP/DHCP) may hold a stub for this same box
            # under a sibling MAC (an SVI/VLAN MAC next to the base MAC): fold it in.
            if base_mac and not is_mgmt:
                try:
                    self._merge_sibling_stubs(devobj, base_mac, serial, nw_id, idx)
                except Exception as e:
                    logger.warning("sync_nw_device: sibling merge for %s skipped: %s", name, e)

            # Stitch this controller to its server's record (hardware-serial match).
            if is_mgmt:
                try:
                    self._link_host_device(devobj, dev.get("linked_host"), idx, serial,
                                           host_model=str(dev.get("host_model") or "").strip(),
                                           spec=spec)
                except Exception as e:
                    logger.warning("sync_nw_device: link host for %s skipped: %s", name, e)

            # Summary line for this device.
            msg = (f"nw device {name} upserted: {pushed} interface(s), "
                   f"{deleted} deleted, {skipped} skipped, {errors} errors")
            # With errors: append the first error and log at WARNING; else INFO.
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("sync_nw_device: %s", msg)
            else:
                logger.info("sync_nw_device: %s", msg)
            # Success envelope, including the device's site for the caller.
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "interfaces_total": len(interfaces or []),
                    "device_id": dev_id, "message": msg,
                    "site": self._device_site_ref(devobj)}
        # Unexpected failure: log it and return what was achieved so far.
        except Exception as e:
            logger.error("sync_nw_device failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "interfaces_total": len(interfaces or []), "device_id": None}

    def _merge_device_into(self, devobj: Any, stub_id: int, stub_name: Any,
                           why: str) -> bool:
        """Move the stub's IPs onto ``devobj`` (interface ``lm-merged``), journal
        the merge, and delete the stub. False when the stub no longer exists."""
        my_id = getattr(devobj, "id", None)
        stub = self.nb.dcim.devices.get(stub_id)
        if stub is None or stub_id == my_id:
            return False
        iface = self.nb.dcim.interfaces.get(device_id=my_id, name="lm-merged")
        ips = list(self.nb.ipam.ip_addresses.filter(device_id=stub_id))
        if ips and iface is None:
            iface = self.nb.dcim.interfaces.create(
                device=my_id, name="lm-merged", type="other", status="active")
        if getattr(stub, "primary_ip4", None):
            stub.primary_ip4 = None
            stub.save()
        for ipo in ips:
            ipo.assigned_object_type = "dcim.interface"
            ipo.assigned_object_id = iface.id
            ipo.save()
        self._journal("dcim.device", my_id, "nw-poll",
                      note=f"merged duplicate device {stub_name} (id {stub_id}, {why}) "
                           f"into this device; {len(ips)} IP(s) moved")
        stub.delete()
        logger.info("netbox: merged duplicate %s (id %s) into %s",
                    stub_name, stub_id, getattr(devobj, "name", my_id))
        self._global_devices_cache = None
        return True

    def list_stub_devices(self) -> Dict[str, Any]:
        """Devices another feeder made that no poll has identified: no serial, a
        primary IP, not a polled network device, not a management controller."""
        rows = self._all_devices_cached()
        out = []
        for r in rows or []:
            cf = r.get("custom_fields") or {}
            pip = r.get("primary_ip4")
            addr = (pip.get("address") or "").split("/")[0] if isinstance(pip, dict) else ""
            if (r.get("serial") or cf.get("nw_device_id") or not addr or _is_mgmt_row(r)
                    or str(cf.get("discovered_from") or "") == "Network Devices"):
                continue
            out.append({"id": r.get("id"), "name": r.get("name"), "ip": addr})
        return {"status": "SUCCESS", "stubs": out}

    def merge_stub_device(self, stub_id: int, nw_device_id: str, serial: str) -> Dict[str, Any]:
        """Fold stub ``stub_id`` into the polled device carrying ``nw_device_id``
        (the hub verified by a live poll that the stub's IP answers with that
        device's serial). Refuses a stub that has since gained a different serial."""
        rows = self._api_get_all("/api/dcim/devices/", {"limit": 500, "cf_nw_device_id": nw_device_id})
        target = next((r for r in rows or []
                       if str((r.get("custom_fields") or {}).get("nw_device_id") or "") == nw_device_id), None)
        if target is None:
            return {"status": "ERROR", "message": f"no NetBox device for nw id {nw_device_id}"}
        stub_row = self.nb.dcim.devices.get(int(stub_id))
        if stub_row is None:
            return {"status": "SUCCESS", "merged": False, "message": "stub already gone"}
        if str(getattr(stub_row, "serial", "") or "").strip() not in ("", serial):
            return {"status": "ERROR", "message": "stub carries a different serial"}
        devobj = self.nb.dcim.devices.get(target["id"])
        ok = self._merge_device_into(devobj, int(stub_id), getattr(stub_row, "name", ""),
                                     f"live poll serial {serial}")
        return {"status": "SUCCESS", "merged": ok}

    def _merge_sibling_stubs(self, devobj: Any, base_mac: str, serial: str,
                             nw_id: str, idx: dict) -> int:
        """Fold stub devices of the same physical box into ``devobj``.

        A switch's VLAN/SVI MACs share all but the last octet with its base MAC,
        so a firewall/ARP feeder files them as a separate device (own name, no
        serial). A stub qualifies only when it is a different record whose MAC
        shares the first 5 octets, carries no serial (or the same one), is not
        itself a polled network device, and is the same kind (controller vs not).
        Its IPs move onto ``devobj`` (interface ``lm-merged``), a journal entry
        records the merge, then the stub is deleted. Returns the number merged."""
        mine = re.sub(r"[^0-9a-f]", "", base_mac.lower())
        if len(mine) != 12:
            return 0
        my_id = getattr(devobj, "id", None)
        merged = 0
        seen = set()
        for key, row in list((idx.get("by_mac") or {}).items()):
            other = re.sub(r"[^0-9a-f]", "", str(key).lower())
            rid = row.get("id")
            if (len(other) != 12 or other == mine or other[:10] != mine[:10]
                    or rid in (None, my_id) or rid in seen):
                continue
            cf = row.get("custom_fields") or {}
            rserial = str(row.get("serial") or "").strip()
            rnw = str(cf.get("nw_device_id") or "").strip()
            if (rserial and rserial != serial) or (rnw and rnw != nw_id):
                continue
            if str(cf.get("discovered_from") or "") == "Network Devices" or _is_mgmt_row(row):
                continue
            seen.add(rid)
            if self._merge_device_into(devobj, rid, row.get("name"), f"MAC {key}"):
                merged += 1
        if merged:
            self._global_devices_cache = None
        return merged

    def _host_row_for(self, linked_host: Any, idx: dict, serial: str = "") -> Optional[dict]:
        """The host server's NetBox row for a serial-linked controller: the
        non-controller device with that serial, else that name; None if absent."""
        if not isinstance(linked_host, dict):
            return None
        host_name = str(linked_host.get("name") or "").strip()
        if not host_name:
            return None
        host_serial = str(linked_host.get("serial") or serial or "").strip()
        names = list(dict.fromkeys([self._nb_name(host_name.split(".", 1)[0]), host_name]))
        row = (idx["by_serial"].get(host_serial.lower()) if host_serial else None) \
            or next((idx["by_name"][n.lower()] for n in names if n.lower() in idx["by_name"]), None)
        if row is None:
            # The index may hold one tenant only; the host can sit in another.
            for n in names:
                try:
                    hits = self._api_get("/api/dcim/devices/", {"name__ie": n, "limit": 5})
                except Exception as e:
                    logger.debug("netbox: host lookup %s failed: %s", n, e)
                    continue
                row = next((r for r in (hits or {}).get("results") or []
                            if not _is_mgmt_row(r)), None)
                if row:
                    break
        if row is None or _is_mgmt_row(row):
            return None
        return row

    def _link_host_device(self, devobj: Any, linked_host: Any, idx: dict,
                          serial: str = "", host_model: str = "", spec: Any = None) -> None:
        """Link a management controller's NetBox device to its host server's.

        The hub matched the two by hardware serial and sends the host's name.
        Both stay separate records: the controller gets the native serial and a
        ``linked_device`` pointing at the host; when the host already exists in
        NetBox it gets the reverse pointer (not ``oob_ip``: NetBox only accepts
        an IP on the host's own interfaces), and a racked host's rack (and U position when that
        slot is free) is copied to the controller. A host with no NetBox device
        yet is never invented - only the controller side is recorded."""
        host_name = str((linked_host or {}).get("name") or "").strip() if isinstance(linked_host, dict) else ""
        if not host_name:
            return
        host_serial = str((linked_host or {}).get("serial") or serial or "").strip()
        # The controller side: native serial (fill-gap only) and the pointer.
        cf = dict(devobj.custom_fields or {})
        changed = False
        if cf.get("linked_device") != host_name:
            cf["linked_device"] = host_name
            devobj.custom_fields = cf
            changed = True
        if host_serial and not str(getattr(devobj, "serial", "") or "").strip():
            devobj.serial = host_serial
            changed = True
        row = self._host_row_for(linked_host, idx, serial)
        if row is None or row.get("id") == getattr(devobj, "id", None):
            if changed:
                devobj.save()
            return
        host = self.nb.dcim.devices.get(row["id"])
        if not host:
            if changed:
                devobj.save()
            return
        # Racked host: put the controller in the same rack (and U when free).
        host_rack = getattr(host, "rack", None)
        if host_rack and not getattr(devobj, "rack", None):
            attempts = [{"rack": host_rack.id, "site": host.site.id,
                         "position": getattr(host, "position", None),
                         "face": getattr(getattr(host, "face", None), "value", None)},
                        {"rack": host_rack.id, "site": host.site.id}]
            saved = False
            for kw in attempts:
                if kw.get("position") is None:
                    kw = {k: v for k, v in kw.items() if k not in ("position", "face")}
                before = {k: getattr(devobj, k, None) for k in kw}
                try:
                    for k, v in kw.items():
                        setattr(devobj, k, v)
                    devobj.save()
                    saved = True
                    break
                except Exception as e:
                    logger.info("netbox: rack %s for %s refused (%s)", kw, devobj.name, e)
                    for k, v in before.items():
                        setattr(devobj, k, v)
            changed = changed and not saved
        if changed:
            devobj.save()
        # The host side: reverse pointer.
        hcf = dict(host.custom_fields or {})
        hchanged = False
        if hcf.get("linked_device") != devobj.name:
            hcf["linked_device"] = devobj.name
            host.custom_fields = hcf
            hchanged = True
        # The chassis serial the host's agent reported (fill-gap only).
        if host_serial and not str(getattr(host, "serial", "") or "").strip():
            host.serial = host_serial
            hchanged = True
        if hchanged:
            host.save()
            self._journal("dcim.device", host.id, "nw-poll",
                          note=f"linked to controller {devobj.name} by serial")
        # The controller reports the server's model: a generic host gets its
        # real type (and height). Saved alone - a taken U must not undo the link.
        if host_model:
            dtype, replaceable = self._identified_device_type(host_model, "", spec)
            if self._upgrade_generic_identity(host, dtype, None, replaceable):
                try:
                    host.save()
                    self._journal("dcim.device", host.id, "nw-poll",
                                  note=f"device type {host_model} from controller {devobj.name}")
                except Exception as e:
                    logger.info("netbox: type %s for %s refused (%s)", host_model, host.name, e)

    def _resolve_tenant_ci(self, slug: str) -> Any:
        """Case-insensitive tenant resolve: lower(slug) ∪ lower(name) → tenant
        object. Returns None when not found. Mirrors sync_vms' per-batch lookup
        but as a reusable helper. NetBox slugs are conventionally lowercase, but
        a configured tenant_slug may arrive mixed-case or as the display name."""
        # Blank input: nothing to resolve.
        s = str(slug or "").strip()
        if not s:
            return None
        # Build a lower-case lookup table and resolve; failures fall back to an exact slug lookup.
        try:
            lut: Dict[str, str] = {}
            # Map every tenant's slug and name (lower-cased) to its canonical slug.
            for t in self._api_get_all("/api/tenancy/tenants/"):
                tslug = str((t or {}).get("slug") or "").strip()
                if not tslug:
                    continue
                lut.setdefault(tslug.lower(), tslug)
                nm = str((t or {}).get("name") or "").strip()
                if nm:
                    lut.setdefault(nm.lower(), tslug)
            # Translate the input to the canonical slug (or use it as-is) and fetch the tenant.
            canon = lut.get(s.lower()) or s
            return self.nb.tenancy.tenants.get(slug=canon)
        # Lookup table failed: try the slug as given; a second failure means "not found".
        except Exception as e:
            logger.debug("resolve_tenant_ci %s failed: %s", s, e)
            try:
                return self.nb.tenancy.tenants.get(slug=s)
            except Exception:
                return None

    def _attach_endpoint_ip(self, devobj: Any, mac: str, ip: str, username: str,
                            tenant: Any, start_time: str = "") -> Any:
        """Put an endpoint's framed IP on its ``eth0`` NIC (created if missing)
        and make it the device's primary IP. Returns the NIC. Raises on failure.

        The IP goes through ``_reuse_or_create_ip``: NetBox enforces global IP
        uniqueness, so an existing record is reassigned instead of 400ing."""
        # Reuse the endpoint's eth0 interface, or create it.
        nic = (self.nb.dcim.interfaces.get(device_id=devobj.id, name="eth0")
               or self.nb.dcim.interfaces.create(device=devobj.id, name="eth0",
                                                 type="other"))
        # Set its MAC (own NetBox record); a failure must not block the IP.
        try:
            self._set_iface_mac(nic, self._norm_mac(mac))
        except Exception as e:
            logger.warning("[sync-error] access-tracker: MAC %s on eth0 of %s failed: %s",
                           mac, devobj.name, e)
        # Build the IP payload: address with mask, assigned to eth0.
        mask = self._mask_for_ip(ip, tenant)
        ip_kwargs: Dict[str, Any] = {
            "address": f"{ip}/{mask}",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": nic.id,
        }
        # Add tenant, and the username as the DNS name when known.
        if tenant:
            ip_kwargs["tenant"] = tenant.id
        if username:
            ip_kwargs["dns_name"] = username
        # Reuse the existing IPAM entry for this address or create it.
        ipobj = self._reuse_or_create_ip(
            f"{ip}/{mask}", ip_kwargs, ip, nic.id,
            tenant=tenant, hostname=username, mac=mac,
            source="realtime-nac-access-tracker")
        # Make it the device's primary IPv4.
        devobj.primary_ip4 = ipobj.id
        devobj.save()
        # Journal it and stamp last_seen with the session start time.
        self._journal("ipam.ipaddress", ipobj.id, "realtime-nac-access-tracker",
                      note=f"framed IP {ip}/{mask} → {devobj.name}")
        self._stamp_last_seen(ipobj, when=start_time)
        return nic

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
        # Result counters and the first error text for the summary.
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None
        # Default to an empty defaults dict.
        defaults = defaults or {}
        # Any unexpected failure is caught at the bottom and reported as an ERROR envelope.
        try:
            # Make sure the custom fields we write exist in NetBox.
            self._ensure_custom_fields()
            # Resolve the tenant; refuse to run if the slug is unknown.
            tenant = None
            if tenant_slug:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if not tenant:
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — "
                                       f"access-tracker sessions not attributed.",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "sessions_total": len(sessions or [])}
                self._move_tenant_ips_to_vrf(tenant)

            # Resolve role/type/site for endpoints and role/type for auto-created switches.
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
            # Names taken by earlier creates in this batch.
            used_names: set = set()
            # List the tenant's (or all) devices; failing here aborts with an ERROR.
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
            # Index every listed device by name, MAC custom field and primary IP.
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
            # Tenant-scoped: also index other tenants' MACs so we never duplicate a known MAC.
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
                # No NAS IP: no switch to model.
                if not nas_ip:
                    return None
                # Reuse a switch already known by that IP (listed or created this batch).
                row = existing_by_ip.get(nas_ip) or switch_by_ip.get(nas_ip)
                if row:
                    switch_by_ip[nas_ip] = row
                    return row
                # New switch: pick a unique name from the NAS name or IP and reserve it.
                name = (nas_name or f"switch-{nas_ip}").strip() or f"switch-{nas_ip}"
                name = self._uniq_device_name(name, "", nas_ip, existing_by_name, used_names)
                used_names.add(name.lower())
                # Create payload with role, type, site and tenant when known.
                ck: Dict[str, Any] = {"name": name, "status": "active"}
                if switch_role:
                    ck["role"] = switch_role.id
                if switch_dtype:
                    ck["device_type"] = switch_dtype.id
                if site:
                    ck["site"] = site.id
                if tenant:
                    ck["tenant"] = tenant.id
                # Create the switch; a failure is handled below by returning None.
                try:
                    sw = self.nb.dcim.devices.create(**ck)
                    self._global_devices_cache = None  # next listing must see it
                    # mgmt interface holding the NAS IP + primary_ip4 so the
                    # next batch finds this switch by IP (existing_by_ip path).
                    if sw:
                        # Add a mgmt interface holding the NAS IP (in the tenant's VRF) and make it
                        # primary.
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
                        # Remember the new switch in this batch's IP indexes.
                        row = {"id": sw.id, "name": sw.name,
                               "primary_ip4": {"address": f"{nas_ip}/{self._mask_for_ip(nas_ip, tenant)}"}}
                        switch_by_ip[nas_ip] = row
                        existing_by_ip[nas_ip] = row
                        return row
                # Switch create failed: log and let the endpoint sync continue without topology.
                except Exception as e:
                    logger.debug("sync_access_tracker: create switch %s failed: %s", nas_ip, e)
                return None

            def _ensure_switch_port(switch_row: dict, nas_port: str):
                """Get-or-create the named port interface on the switch."""
                # Need both a switch and a port name.
                if not switch_row or not nas_port:
                    return None
                # Reuse a port already resolved this batch.
                key = (switch_row["id"], nas_port)
                cached = port_iface_by_key.get(key)
                if cached:
                    return cached
                # Look the port up in NetBox; a lookup failure is logged and we try to create it.
                try:
                    iface = self.nb.dcim.interfaces.get(
                        device=switch_row["id"], name=nas_port)
                    if iface:
                        port_iface_by_key[key] = iface
                        return iface
                except Exception as e:
                    logger.debug("sync_access_tracker: find port %s failed: %s", nas_port, e)
                # Create the port; a failure is logged and None returned.
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
                # Skip if the NIC is already cabled; a failed check is logged and we try to cable
                # anyway.
                try:
                    nic_obj = self.nb.dcim.interfaces.get(nic.id)
                    if nic_obj and self._iface_cable_peer(nic_obj) is not None:
                        return  # already cabled — don't create a second link
                except Exception as e:
                    logger.debug("sync_access_tracker: nic connected_endpoint check: %s", e)
                # Create a connected cable between the NIC and the switch port.
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

            # Process each session (non-dict rows ignored); one failure does not stop the batch.
            for s in (sessions or []):
                if not isinstance(s, dict):
                    continue
                # Any failure for this session is counted in the except at the bottom of the loop.
                try:
                    # Extract and clean the session fields.
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
                    # MAC is the identity for NAC endpoints; sessions without one are skipped.
                    if not mac:
                        skipped += 1
                        continue

                    # Find an existing device by MAC, then by framed IP.
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
                        # Matched device has no MAC: write ours onto it and index it.
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
                        # Fill a missing primary IP with the framed IP (e.g. the
                        # device was re-created from a MAC table with no IP).
                        # Same tenant only: never put this tenant's IP on another
                        # tenant's device.
                        # The matched device's tenant id; nothing filled yet.
                        row_tid = (row.get("tenant") or {}).get("id")
                        filled = False
                        # Missing primary IP on a same-tenant device: attach the framed IP.
                        if (ip and not row.get("primary_ip4")
                                and row_tid == (tenant.id if tenant else None)):
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    self._attach_endpoint_ip(devobj, mac, ip, username,
                                                             tenant, start_time)
                                    row["primary_ip4"] = {"address": ip}
                                    existing_by_ip.setdefault(ip, row)
                                    filled = True
                            except Exception as e:
                                logger.debug("sync_access_tracker: fill IP %s failed: %s", ip, e)
                        # Best-effort refresh of topology/last_seen ONLY on a
                        # device we own (never touch another source's record).
                        # Devices this tracker created get their topology/last_seen fields
                        # refreshed.
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
                        # Filling the IP counts as a push; otherwise the device was simply already
                        # present.
                        if filled:
                            pushed += 1
                        else:
                            skipped += 1
                        continue

                    # Create the missing endpoint device.
                    # Name it after the user (or MAC placeholder), uniquified and reserved for this
                    # batch.
                    name = (username or f"device-{mac.replace(':', '')}")
                    name = self._uniq_device_name(name, mac, ip, existing_by_name, used_names)
                    used_names.add(name.lower())
                    # Create payload: name, status, role.
                    create_kwargs: Dict[str, Any] = {"name": name, "status": "active"}
                    if role:
                        create_kwargs["role"] = role.id
                    # Prefer a device_type from the profiled model over default.
                    at_dtype = self._device_type_for_model(model, manufacturer) or dtype
                    if at_dtype:
                        create_kwargs["device_type"] = at_dtype.id
                    # Add site and tenant when known, then create the device.
                    if site:
                        create_kwargs["site"] = site.id
                    if tenant:
                        create_kwargs["tenant"] = tenant.id
                    devobj = self.nb.dcim.devices.create(**create_kwargs)
                    self._global_devices_cache = None  # next listing must see it
                    # Ownership tag + topology custom fields (best-effort; same
                    # create-without-cf + best-effort PATCH lesson as sync_vms).
                    # Best-effort: a missing custom field in NetBox makes the save fail; that is
                    # just logged.
                    try:
                        # Ownership tag, MAC and switch-sighting fields.
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
                    # Journal the creation.
                    self._journal("dcim.device", devobj.id,
                                  "realtime-nac-access-tracker",
                                  note=f"endpoint {name} (MAC {mac})")

                    # NIC interface (native MAC) + framed IP + primary_ip4.
                    # Only endpoints with a framed IP get a NIC and IP.
                    nic = None
                    if ip:
                        nic = self._attach_endpoint_ip(devobj, mac, ip, username,
                                                       tenant, start_time)

                    # Switch topology (best-effort, never breaks the sync).
                    # Model the switch, its port and the cable when the session names them.
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
                    # Index the new device by MAC (and IP).
                    existing_by_mac[mac] = new_row
                    if ip:
                        existing_by_ip.setdefault(ip, new_row)
                    # Count the created endpoint.
                    pushed += 1
                # Session failed: count it, keep the first error text and continue.
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"upsert {s.get('mac','?')}: {e}"
                    logger.debug("sync_access_tracker: upsert failed: %s", e)

            # Summary line for the batch.
            msg = (f"{pushed} endpoint(s) added, {skipped} already present, "
                   f"{deleted} deleted, {errors} errors")
            # With errors: append the first error and log at WARNING; else INFO.
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("sync_access_tracker tenant=%s: %s", tenant_slug or "<global>", msg)
            else:
                logger.info("sync_access_tracker tenant=%s: %s", tenant_slug or "<global>", msg)
            # Success envelope.
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "sessions_total": len(sessions or []), "message": msg}
        # Unexpected failure: log it and return what was achieved so far.
        except Exception as e:
            logger.error("sync_access_tracker failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "sessions_total": len(sessions or [])}

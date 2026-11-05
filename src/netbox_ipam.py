"""IPAM prefix/IP methods incl. free-subnet finder + claim for NetboxEngine."""
# ipaddress for subnet math.
import ipaddress
# Standard-library logging.
import logging
# Type hints.
from typing import Any, Dict, List, Optional

# Shares the engine's logger name so all engine messages go to one place.
logger = logging.getLogger("NetboxEngine")



# NetBox 4.2 replaced Prefix.site with a generic scope (region/site group/site/location).
SITE_SCOPE_TYPE = "dcim.site"


def _site_scope(site_id: int) -> Dict[str, Any]:
    """Payload fields that scope a prefix to a site (NetBox 4.2+ scope form)."""
    return {"scope_type": SITE_SCOPE_TYPE, "scope_id": site_id}


def _prefix_site(p: Dict[str, Any]) -> Dict[str, Any]:
    """The site a prefix (API row) is scoped to, or {}."""
    # New-style scope pointing at a site.
    if p.get("scope_type") == SITE_SCOPE_TYPE and isinstance(p.get("scope"), dict):
        return p["scope"]
    # Older NetBox: the site field.
    return p["site"] if isinstance(p.get("site"), dict) else {}

class IpamMixin:
    """IPAM prefix/IP methods incl. free-subnet finder + claim for NetboxEngine."""

    # ─── IPAM – Prefixes / IPs ─────────────────────────────────────────────────

    def get_prefixes(self, site: Optional[str] = None, vrf: Optional[str] = None,
                     tenant: Optional[str] = None,
                     tenant_group: Optional[str] = None) -> Dict[str, Any]:
        """Fetch prefixes from NetBox filtered by site, VRF, tenant or tenant group.

        ``tenant_group`` supersedes ``tenant``: NetBox's own ``?tenant_group=``
        filter is tree-aware, so it unions every tenant in the group AND its
        descendant groups server-side. We deliberately do not expand a group
        into a list of tenant slugs — one unknown slug makes NetBox reject the
        entire query."""
        # Any API failure becomes an ERROR envelope.
        try:
            # Build the filter parameters.
            params: Dict[str, Any] = {}
            if site:
                params["site"] = site
            if vrf:
                params["vrf"] = vrf
            # ``tenant_group`` is NetBox's own tree-aware union filter: it
            # matches every tenant in the group AND its sub-groups, server-side.
            # It supersedes ``tenant`` when both arrive.
            if tenant_group:
                params["tenant_group"] = tenant_group
            elif tenant:
                params["tenant"] = tenant
            # Fetch every page of matching prefixes.
            rows = self._api_get_all("/api/ipam/prefixes/", params)
            prefixes = []
            # Flatten each row into the fields the hub uses.
            for p in rows:
                status = p.get("status") or {}
                prefixes.append({
                    "id": p["id"],
                    "prefix": p["prefix"],
                    "status": status.get("value", "") if isinstance(status, dict) else str(status),
                    "site": _prefix_site(p).get("name", ""),
                    "site_slug": _prefix_site(p).get("slug", ""),
                    "vrf": p["vrf"]["name"] if p.get("vrf") else "",
                    "description": p.get("description") or "",
                    "is_pool": p.get("is_pool", False),
                    # Forward custom_fields so the hub's DHCP sync can read
                    # dhcp_enabled/gateway/dns_servers, and the WebUI edit
                    # modal can pre-check "Enable DHCP scope" — this was
                    # dropped entirely before, so dhcp_enabled always read as
                    # false downstream regardless of what was actually saved
                    # in NetBox (no prefix could ever become a Kea scope, and
                    # the edit modal's checkbox could never reflect reality).
                    "custom_fields": p.get("custom_fields") or {},
                })
            # All prefixes.
            return {"status": "SUCCESS", "prefixes": prefixes}
        except Exception as e:
            # API failure.
            return {"status": "ERROR", "message": str(e)}

    def allocate_prefix(
        self,
        parent_prefix: str,
        prefix_length: int,
        description: str = "",
        site_slug: Optional[str] = None,
        status: str = "active",
        requested_prefix: Optional[str] = None,
        tenant_slug: Optional[str] = None,
        custom_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Allocate a child prefix of a parent.

        When ``requested_prefix`` is supplied, that exact subnet is created
        directly (after verifying it is a subnet of ``parent_prefix``); otherwise
        NetBox auto-allocates the next available child of ``prefix_length``.
        ``custom_fields`` (e.g. ``dhcp_enabled``/``gateway``/``dns_servers``)
        is written at creation time so the WebUI's "Enable DHCP scope"
        checkbox on the Allocate Subnet modal actually takes effect."""
        try:
            # Resolve the tenant's VRF (if any) and find the parent prefix in it.
            vrf_id = self._vrf_id_for_tenant_slug(tenant_slug) if tenant_slug else None
            parent = self._get_prefix_obj(parent_prefix, vrf_id)
            # Parent must exist.
            if not parent:
                return {"status": "ERROR", "message": f"Parent prefix '{parent_prefix}' not found"}

            # Base payload for the new prefix.
            payload: Dict[str, Any] = {"description": description, "status": status}
            if vrf_id is None:
                # No tenant VRF: the child inherits the parent's VRF. NetBox's
                # available-prefixes allocator does this itself, but a direct
                # prefixes.create (exact "Subnet" typed) would otherwise land
                # in the global table.
                vrf_id = self._obj_vrf_id(parent)
            # Put the child in the chosen VRF.
            if vrf_id is not None:
                payload["vrf"] = vrf_id
            # DHCP and other custom fields set at creation time.
            if custom_fields:
                payload["custom_fields"] = custom_fields
            # Scope the prefix to the site, if given.
            if site_slug:
                site = self.nb.dcim.sites.get(slug=site_slug)
                # Unknown site: error.
                if not site:
                    return {"status": "ERROR", "message": f"Site '{site_slug}' not found"}
                payload.update(_site_scope(site.id))
            # Attribute the prefix to the tenant.
            if tenant_slug:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if tenant:
                    payload["tenant"] = tenant.id
                else:
                    # Refuse silent unattributed allocation (mirrors claim_prefix):
                    # an unresolvable slug would produce a prefix with no tenant.
                    logger.warning(
                        f"allocate_prefix: NetBox tenant '{tenant_slug}' not found; "
                        f"refusing unattributed allocate under {parent_prefix}")
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — subnet not attributed. Check the tenant's NetBox slug mapping."}

            if requested_prefix:
                # A bare address with no explicit "/mask" (e.g. the user typed
                # "172.17.0.0" instead of "172.17.0.0/24") would make
                # ipaddress.ip_network() silently default to /32, discarding
                # the separately-selected prefix_length and creating a
                # single-host prefix instead of the requested subnet. Apply
                # the dropdown's prefix_length in that case.
                if "/" not in requested_prefix:
                    requested_prefix = f"{requested_prefix}/{prefix_length}"
                # The requested subnet must fall inside the parent.
                parent_net = ipaddress.ip_network(parent_prefix, strict=False)
                req_net = ipaddress.ip_network(requested_prefix, strict=False)
                if not req_net.subnet_of(parent_net):
                    return {"status": "ERROR", "message": f"{requested_prefix} is not within {parent_prefix}"}
                # Create exactly that subnet.
                payload["prefix"] = str(req_net)
                allocated = self.nb.ipam.prefixes.create(payload)
            elif vrf_id is not None and self._obj_vrf_id(parent) != vrf_id:
                # Parent is a global-table container: NetBox would create the
                # child in the parent's (global) VRF, so pick a free block
                # ourselves — free in the tenant's VRF — and create it there.
                cand = self._next_free_in_vrf(parent_prefix, prefix_length, vrf_id)
                if not cand:
                    return {"status": "ERROR",
                            "message": f"No free /{prefix_length} available in {parent_prefix} for this tenant's VRF"}
                payload["prefix"] = cand
                allocated = self.nb.ipam.prefixes.create(payload)
            else:
                # Normal case: NetBox picks the next free child of the parent.
                payload["prefix_length"] = prefix_length
                allocated = parent.available_prefixes.create(payload)
            # Allocated prefix and its id.
            return {"status": "SUCCESS", "prefix": str(allocated.prefix), "id": allocated.id, "description": description}
        except Exception as e:
            # Log and return the error.
            logger.error(f"allocate_prefix failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    @staticmethod
    def _obj_vrf_id(obj: Any):
        """VRF id of a pynetbox object, or None for the global table."""
        # Only a real integer id counts.
        vid = getattr(getattr(obj, "vrf", None), "id", None)
        return vid if isinstance(vid, int) else None

    def _next_free_in_vrf(self, parent_prefix: str, prefix_length: int, vrf_id: int) -> Optional[str]:
        """First ``/prefix_length`` inside ``parent_prefix`` not overlapping any
        prefix already in VRF ``vrf_id``."""
        # The requested size cannot be bigger than the parent.
        parent_net = ipaddress.ip_network(parent_prefix, strict=False)
        if prefix_length < parent_net.prefixlen:
            return None
        # Every prefix already in that VRF inside the parent.
        used = []
        for r in self._api_get_all("/api/ipam/prefixes/",
                                   {"within": str(parent_net), "vrf_id": vrf_id, "limit": 500}):
            # Skip rows with an unparseable prefix.
            try:
                used.append(ipaddress.ip_network(r["prefix"], strict=False))
            except (ValueError, TypeError, KeyError):
                continue
        # First candidate that overlaps nothing in use.
        for cand in parent_net.subnets(new_prefix=prefix_length):
            if not any(cand.overlaps(u) for u in used):
                return str(cand)
        # Parent is full.
        return None

    # ─── IPAM – Free-subnet finder + claim (tenant self-service) ────────────────
    #
    # "Available" means: a subnet of the requested size that no tenant-assigned
    # NetBox prefix overlaps. Undefined-in-NetBox and defined-but-unassigned
    # both count as free (the user's contract for this feature). Search is
    # restricted to RFC1918 space. "Closest" = smallest absolute numeric
    # distance between the candidate network address and the reference subnet.

    _RFC1918_BLOCKS = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    # Default IPv6 private-space container, mirroring RFC1918 for v4. A lab
    # using a real ISP/tunnel-broker GUA (not ULA) instead should pass
    # ``rfc1918=False`` so ``near`` itself becomes the search container (see
    # ``find_available_prefixes``) instead of being forced inside fc00::/7.
    _ULA_BLOCKS = (
        ipaddress.ip_network("fc00::/7"),
    )

    @staticmethod
    def _mask_for_hosts(hosts: int, family: int = 4) -> int:
        """Smallest prefix length whose usable host count fits ``hosts``.

        IPv4: usable = 2^(32-L) - 2 (network + broadcast reserved). Minimum
        /30 (2 usable), capped at /22 (1022 usable) — the largest subnet a
        tenant may request through the self-service finder.

        IPv6: no broadcast address is reserved, so usable = 2^(128-L). Floor
        is /64 (the smallest subnet SLAAC-capable hosts expect), capped at
        /48 (a full "site" allocation) — a host count needing more than /48
        still yields /48 rather than going larger."""
        # At least one host.
        if hosts < 1:
            hosts = 1
        # IPv6: no reserved addresses; search /64 down to /48.
        if family == 6:
            for length in range(64, 47, -1):
                if (1 << (128 - length)) >= hosts:
                    return length
            return 48
        # IPv4: subtract network and broadcast; search /30 down to /22.
        for length in range(30, 21, -1):
            if (1 << (32 - length)) - 2 >= hosts:
                return length
        # Largest allowed size.
        return 22

    def find_available_prefixes(self, near: str, prefix_length: int = 24,
                                count: int = 20, exact: Optional[str] = None,
                                rfc1918: bool = True,
                                tenant_slug: Optional[str] = None) -> Dict[str, Any]:
        """Return up to ``count`` free subnets of ``prefix_length`` closest to
        ``near``, ranked by numeric distance.

        Family (v4/v6) is inferred from ``near``. When ``rfc1918`` is true
        (the default), ``near`` must lie within RFC1918 (v4) or ULA fc00::/7
        (v6). Passing ``rfc1918=False`` skips that containment check and uses
        ``near`` itself as the search container instead — the path a lab using
        a real GUA allocation (e.g. a tunnel-broker-routed /48) should take,
        since that space is neither RFC1918 nor ULA. ``exact`` (if given) is
        checked first and returned as distance 0 when free — this is the
        "type a subnet, try it first, else nearest" path."""
        # Parse the reference subnet.
        try:
            near_net = ipaddress.ip_network(near, strict=False)
        except (ValueError, TypeError) as e:
            # Unparseable reference subnet.
            return {"status": "ERROR", "message": f"Invalid 'near' CIDR: {e}"}
        # Address family and its bit width.
        family = near_net.version
        bits = 128 if family == 6 else 32
        # Validate the requested size and count.
        if not 0 <= prefix_length <= bits:
            return {"status": "ERROR", "message": f"prefix_length must be 0..{bits}"}
        if not 1 <= count <= 200:
            return {"status": "ERROR", "message": "count must be 1..200"}

        # Find the private-space block that contains the reference subnet.
        if rfc1918:
            blocks = self._ULA_BLOCKS if family == 6 else self._RFC1918_BLOCKS
            container = next((b for b in blocks
                              if near_net.subnet_of(b) or near_net.overlaps(b)), None)
            # Outside private space: error.
            if not container:
                label = "ULA (fc00::/7)" if family == 6 else "RFC1918 (10/8, 172.16/12, 192.168/16)"
                return {"status": "ERROR", "message": f"near must be within {label}"}
        else:
            # No private-space restriction: treat ``near`` itself as the
            # bounding container (e.g. a routed GUA /48) that candidates are
            # carved from.
            container = near_net

        # Validate the optional exact subnet the user typed.
        exact_net = None
        if exact:
            try:
                exact_net = ipaddress.ip_network(exact, strict=False)
            except (ValueError, TypeError) as e:
                # Unparseable exact subnet.
                return {"status": "ERROR", "message": f"Invalid 'exact' CIDR: {e}"}
            # Must match family and lie in the same container.
            if exact_net.version != family:
                return {"status": "ERROR", "message": "exact must be the same IP family as near"}
            if not (exact_net.subnet_of(container) or exact_net.overlaps(container)):
                return {"status": "ERROR",
                        "message": "exact must be within the same search container as near"}

        # Authoritative occupied set = every tenant-assigned prefix inside the
        # container, restricted to matching family (mixed-family overlap
        # comparisons raise TypeError). Unassigned/undefined prefixes are free
        # space and do not make a candidate occupied.
        # With overlapping tenant address space, "occupied" is per-VRF: only the
        # tenant's own VRF (when it has one) counts, not other tenants' prefixes.
        # Prefixes inside the container, limited to the tenant's VRF when it has one.
        params: Dict[str, Any] = {"within_include": str(container), "limit": 500}
        vrf_id = self._vrf_id_for_tenant_slug(tenant_slug) if tenant_slug else None
        if vrf_id is not None:
            params["vrf_id"] = vrf_id
        try:
            # Fetch them all.
            rows = self._api_get_all("/api/ipam/prefixes/", params)
        except Exception as e:
            # Fetch failed.
            return {"status": "ERROR", "message": f"NetBox prefix fetch failed: {e}"}
        # Keep only tenant-assigned prefixes of the same family.
        occupied: List[ipaddress._BaseNetwork] = []
        for r in rows:
            if not r.get("tenant"):
                continue
            try:
                # Skip unparseable rows.
                onet = ipaddress.ip_network(r["prefix"], strict=False)
            except (ValueError, TypeError):
                continue
            if onet.version != family:
                continue
            occupied.append(onet)

        def is_free(cand) -> bool:
            """True when the candidate overlaps no occupied prefix."""
            return not any(cand.overlaps(o) for o in occupied)

        # Candidate grid: the step size and the anchor aligned to it.
        net_cls = ipaddress.IPv6Network if family == 6 else ipaddress.IPv4Network
        step = 1 << (bits - prefix_length)        # address span of one candidate
        base = int(near_net.network_address)
        base = (base // step) * step             # align anchor to candidate grid
        # Search starts at the exact subnet when given, else at the reference.
        start = int(exact_net.network_address) if exact_net else base
        start = (start // step) * step

        # Container bounds; the walk never leaves them.
        cfirst = int(container.network_address)
        clast = int(container.broadcast_address)
        candidates: List[Dict[str, Any]] = []
        max_offset = (clast - cfirst) // step + 1   # never walk past the block
        # Walk outward from the start, alternating above and below.
        i = 0
        while len(candidates) < count and i <= max_offset:
            signs = (0,) if i == 0 else (1, -1)
            for sign in signs:
                # Candidate address at this offset.
                cand_int = start + sign * i * step
                # Skip candidates outside the container or running past its end.
                if cand_int < cfirst or cand_int > clast:
                    continue
                if cand_int + (step - 1) > clast:
                    continue
                # Build the network object.
                try:
                    cand = net_cls((cand_int, prefix_length))
                except ValueError:
                    continue
                # Skip occupied candidates.
                if not is_free(cand):
                    continue
                # Record the free candidate with its distance from the reference.
                dist = abs(cand_int - base) // step
                candidates.append({"prefix": str(cand), "distance": dist})
                if len(candidates) >= count:
                    break
            # Next ring outward.
            i += 1

        # Candidates, nearest first.
        return {"status": "SUCCESS", "available": candidates, "count": len(candidates)}

    def claim_prefix(self, prefix: str, tenant_slug: Optional[str] = None,
                      description: str = "", site_slug: Optional[str] = None,
                      status: str = "active",
                      custom_fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Assign a specific free subnet to a tenant (the "Assign" action).

        If the prefix already exists in NetBox but has no tenant, reassign it
        (no duplicate). If it exists and is already tenant-assigned, refuse —
        it wasn't actually free. Otherwise create it with the tenant/site
        attached. Tenant/site slug→id resolution mirrors ``allocate_prefix``.

        ``custom_fields`` (e.g. ``dhcp_enabled``/``gateway``/``dns_servers``)
        is written at creation time and MERGED onto an existing unassigned
        prefix — mirrors ``allocate_prefix``/``update_prefix``. Before this,
        the "Add Prefix" finder was the one creation path that silently
        dropped the DHCP options a caller supplied: a prefix "created" through
        it could never become a Kea scope until someone separately edited it
        afterward."""
        try:
            # Normalise the prefix string.
            prefix = str(ipaddress.ip_network(prefix, strict=False))
        except (ValueError, TypeError) as e:
            # Unparseable prefix.
            return {"status": "ERROR", "message": f"Invalid prefix: {e}"}

        # Resolve the tenant id and its VRF.
        tenant_id = None
        vrf_id = None
        if tenant_slug:
            tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
            if tenant:
                tenant_id = tenant.id
                vrf_id = self._vrf_id_for_tenant(tenant)

        # Look for an existing prefix with this value.
        try:
            if vrf_id is not None:
                # Only a prefix already in the tenant's VRF (or the global
                # table, which we adopt into the VRF) can be "this" prefix.
                existing = self._get_prefix_obj(prefix, vrf_id)
            else:
                existing = self.nb.ipam.prefixes.get(prefix=prefix)
        except Exception as e:
            # Lookup failed.
            return {"status": "ERROR", "message": f"NetBox lookup failed: {e}"}
        # Resolve the site id.
        site_id = None
        if site_slug:
            site = self.nb.dcim.sites.get(slug=site_slug)
            if site:
                site_id = site.id

        # Refuse to silently create an unattributed prefix: if a tenant slug was
        # supplied but didn't resolve to a NetBox tenant, the prefix would be
        # created with no tenant, never appear in get_prefixes(tenant=<slug>),
        # and the tenant's subnet-filtered views (firewall rules, etc.) would
        # never see it. Surface the misconfiguration as an error instead.
        # Unresolvable tenant slug: refuse.
        if tenant_slug and tenant_id is None:
            logger.warning(
                f"claim_prefix: NetBox tenant '{tenant_slug}' not found; "
                f"refusing unattributed create for {prefix}")
            return {"status": "ERROR",
                    "message": f"NetBox tenant '{tenant_slug}' not found — subnet not attributed. Check the tenant's NetBox slug mapping."}

        # Any write failure becomes an ERROR envelope.
        try:
            # Existing prefix: reuse it if it has no tenant.
            if existing:
                # Already assigned: it was not free.
                if getattr(existing, "tenant", None):
                    return {"status": "ERROR",
                            "message": f"Prefix {prefix} is already assigned to a tenant"}
                # Attach tenant, VRF, description, status and site.
                if tenant_id is not None:
                    existing.tenant = tenant_id
                if vrf_id is not None:
                    existing.vrf = vrf_id
                if description:
                    existing.description = description
                if status:
                    existing.status = status
                if site_id is not None:
                    existing.scope_type, existing.scope_id = SITE_SCOPE_TYPE, site_id
                # Merge custom fields onto the existing ones.
                if custom_fields:
                    merged = dict(existing.custom_fields or {})
                    merged.update(custom_fields)
                    existing.custom_fields = merged
                # Save and return.
                existing.save()
                return {"status": "SUCCESS", "prefix": str(existing.prefix), "id": existing.id}
            # No existing prefix: create one.
            payload: Dict[str, Any] = {"prefix": prefix, "status": status,
                                       "description": description}
            if tenant_id is not None:
                payload["tenant"] = tenant_id
            if vrf_id is not None:
                payload["vrf"] = vrf_id
            if site_id is not None:
                payload.update(_site_scope(site_id))
            if custom_fields:
                payload["custom_fields"] = custom_fields
            # Create and return.
            created = self.nb.ipam.prefixes.create(payload)
            return {"status": "SUCCESS", "prefix": str(created.prefix), "id": created.id}
        except Exception as e:
            # Log and return the error.
            logger.error(f"claim_prefix failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def get_ip_addresses(self, prefix: Optional[str] = None, device: Optional[str] = None,
                         tenant: Optional[str] = None,
                         tenant_group: Optional[str] = None) -> Dict[str, Any]:
        """Fetch IP addresses from NetBox filtered by prefix, device, tenant or
        tenant group.

        ``tenant_group`` supersedes ``tenant`` — see :meth:`get_prefixes`."""
        try:
            # Paginated: follow NetBox ``next`` links so a tenant with >500 IPs
            # (routine at thousands-of-VMs scale) isn't silently truncated in
            # the IPAM→CPPM endpoint sync. _api_get_all returns the full flat
            # results list (not a dict), capped at max_pages=200 as a runaway
            # guard.
            # Build the filter parameters.
            params: Dict[str, Any] = {}
            if tenant_group:
                params["tenant_group"] = tenant_group
            elif tenant:
                params["tenant"] = tenant
            if prefix:
                params["parent"] = prefix
            if device:
                params["device"] = device
            # Fetch every page of matching IPs.
            rows = self._api_get_all("/api/ipam/ip-addresses/", params)
            ips = []
            # Flatten each row into the fields the hub uses.
            for ip in rows:
                status = ip.get("status") or {}
                ao = ip.get("assigned_object")
                # Parent device/virtual-machine of the assigned interface, so the
                # IPAM IP Addresses table can show which device an IP lives on.
                # assigned_object is a nested interface (dcim.interface carries a
                # `device` dict; virtualization.vminterface carries a
                # `virtual_machine` dict). Best-effort: empty when NetBox doesn't
                # nest the parent on this endpoint.
                device_name = ""
                if isinstance(ao, dict):
                    parent = ao.get("device") or ao.get("virtual_machine")
                    if isinstance(parent, dict):
                        device_name = parent.get("display") or parent.get("name") or ""
                    elif isinstance(parent, str) and parent:
                        device_name = parent
                # Add the flattened row.
                ips.append({
                    "id": ip["id"],
                    "address": ip["address"],
                    "status": status.get("value", "") if isinstance(status, dict) else str(status),
                    "dns_name": ip.get("dns_name") or "",
                    "description": ip.get("description") or "",
                    "vrf": (ip.get("vrf") or {}).get("name", "") if isinstance(ip.get("vrf"), dict) else "",
                    "assigned_to": ao.get("display", "") if isinstance(ao, dict) else (str(ao) if ao else ""),
                    "device": device_name,
                    # Forward custom_fields so the hub can read mac_address for
                    # IPAM→ClearPass endpoint sync (without this, mac is always
                    # empty at the hub and every record skips in CPPM).
                    "custom_fields": ip.get("custom_fields") or {},
                })
            # All IPs.
            return {"status": "SUCCESS", "ip_addresses": ips}
        except Exception as e:
            # API failure.
            return {"status": "ERROR", "message": str(e)}

    def allocate_ip(
        self,
        prefix: str,
        description: str = "",
        dns_name: str = "",
        status: str = "active",
        address: Optional[str] = None,
        tenant_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Allocate an IP address from a prefix.

        When ``address`` is supplied, that exact address is created directly
        (mask derived from ``prefix`` if not included, and verified to be inside
        the prefix); otherwise NetBox auto-allocates the next available address.

        The new address is always attributed to a tenant: ``tenant_slug`` when
        the caller names one, otherwise the containing prefix's tenant. NetBox
        does **not** inherit tenancy from the parent prefix on its own, so
        without this every address minted here landed tenant-less while every
        address created by hand carried its prefix's tenant. That breaks the
        fleet rule that new resources carry tenant context: a tenant-scoped
        IPAM view filters the address out, so the operator who just reserved it
        cannot see it. It bit the DHCP reservation write-back in particular,
        which creates the IP object behind the scenes and had no tenant to pass.

        An explicitly named tenant that cannot be resolved is an error rather
        than a silent fallback (mirrors ``allocate_prefix``) — quietly
        attributing the address to the prefix's tenant instead of the one the
        caller asked for is exactly the kind of tenant mismatch this guards
        against. An unset tenant on the prefix itself is not an error; there is
        simply nothing to inherit."""
        try:
            # Resolve the named tenant and its VRF, then find the prefix in that VRF.
            pre_tenant = None
            if tenant_slug:
                pre_tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
            vrf_id = self._vrf_id_for_tenant(pre_tenant) if pre_tenant else None
            prefix_obj = self._get_prefix_obj(prefix, vrf_id)
            # Prefix must exist.
            if not prefix_obj:
                return {"status": "ERROR", "message": f"Prefix '{prefix}' not found"}

            # Base payload for the new address.
            payload: Dict[str, Any] = {"description": description, "status": status}
            if dns_name:
                payload["dns_name"] = dns_name

            # A named tenant must resolve.
            if tenant_slug:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if not tenant:
                    logger.warning(
                        f"allocate_ip: NetBox tenant '{tenant_slug}' not found; "
                        f"refusing unattributed allocate under {prefix}")
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — address not attributed. Check the tenant's NetBox slug mapping."}
                payload["tenant"] = tenant.id
            else:
                # No tenant named: inherit the prefix's tenant and its VRF.
                inherited = getattr(prefix_obj, "tenant", None)
                inherited_id = getattr(inherited, "id", None)
                if inherited_id is not None:
                    payload["tenant"] = inherited_id
                    vrf_id = self._vrf_id_for_tenant(inherited_id)
            # The address lives in the prefix's own VRF; fall back to the
            # tenant's VRF only for a global-table prefix.
            pfx_vrf = self._obj_vrf_id(prefix_obj)
            if pfx_vrf is not None:
                vrf_id = pfx_vrf
            if vrf_id is not None:
                payload["vrf"] = vrf_id

            # Exact address requested.
            if address:
                # Derive the mask from the containing prefix when the caller
                # supplied a bare address (e.g. "10.0.0.5" within "10.0.0.0/24").
                if "/" in address:
                    full_address = address
                else:
                    # Host mask must match the address family: /32 for IPv4,
                    # /128 for IPv6 (a hardcoded "32" here silently truncated
                    # every bare IPv6 address to a /32, which is invalid).
                    default_mask = "128" if ipaddress.ip_address(address).version == 6 else "32"
                    mask = prefix.split("/")[-1] if "/" in prefix else default_mask
                    full_address = f"{address}/{mask}"
                # The address must be inside the prefix.
                parent_net = ipaddress.ip_network(prefix, strict=False)
                if ipaddress.ip_interface(full_address).ip not in parent_net:
                    return {"status": "ERROR", "message": f"Address {full_address} is not within {prefix}"}
                # Create exactly that address.
                payload["address"] = full_address
                ip_obj = self.nb.ipam.ip_addresses.create(payload)
            else:
                # Otherwise NetBox picks the next free address.
                ip_obj = prefix_obj.available_ips.create(payload)
            # New address, its id and tenant.
            return {"status": "SUCCESS", "address": ip_obj.address, "id": ip_obj.id,
                    "dns_name": dns_name, "tenant": payload.get("tenant")}
        except Exception as e:
            # Log and return the error.
            logger.error(f"allocate_ip failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def release_ip(self, ip_id: int) -> Dict[str, Any]:
        """Release (delete) an IP address record by ID."""
        try:
            # Look up the IP record.
            ip_obj = self.nb.ipam.ip_addresses.get(ip_id)
            # Unknown id: error.
            if not ip_obj:
                return {"status": "ERROR", "message": f"IP {ip_id} not found"}
            # Delete it.
            ip_obj.delete()
            return {"status": "SUCCESS"}
        except Exception as e:
            # API failure.
            return {"status": "ERROR", "message": str(e)}

    def update_ip_address(self, ip_id: int, dns_name: Optional[str] = None,
                          description: Optional[str] = None,
                          status: Optional[str] = None,
                          custom_fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Edit an IP address's mutable attributes.

        ``custom_fields`` (e.g. ``{"mac_address": "aa:bb:.."}``) is MERGED onto
        the IP's existing custom fields rather than replacing the dict outright,
        for the same reason as ``update_prefix``: a partial update must not
        blank out any other custom field NetBox tracks on this address.

        This was previously silently dropped entirely, and the consequence was
        severe rather than cosmetic. The hub mirrors every DHCP reservation's
        MAC onto its NetBox IP via this call, because ``build_dhcp_payload``
        mints a reservation only for an IP carrying ``custom_fields.mac_address``
        and ``core.dns_dhcp_sync`` rebuilds Kea's whole ``subnet4`` from NetBox
        alone. With the MAC never landing here, a reservation added in the
        WebUI stayed Kea-only and the next sync deleted it — the add returned
        SUCCESS and the row appeared in the list, so the loss looked like the
        reservation had simply vanished on its own some minutes later.
        """
        try:
            # Look up the IP record.
            ip_obj = self.nb.ipam.ip_addresses.get(ip_id)
            # Unknown id: error.
            if not ip_obj:
                return {"status": "ERROR", "message": f"IP {ip_id} not found"}
            # Apply only the fields that were passed.
            if dns_name is not None:
                ip_obj.dns_name = dns_name
            if description is not None:
                ip_obj.description = description
            if status:
                ip_obj.status = status
            # Merge custom fields onto the existing ones.
            if custom_fields:
                merged = dict(ip_obj.custom_fields or {})
                merged.update(custom_fields)
                ip_obj.custom_fields = merged
            # Save and return.
            ip_obj.save()
            return {"status": "SUCCESS", "id": ip_obj.id, "address": ip_obj.address}
        except Exception as e:
            # Log and return the error.
            logger.error(f"update_ip_address failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def update_prefix(self, prefix_id: int, description: Optional[str] = None,
                      status: Optional[str] = None,
                      site_slug: Optional[str] = None,
                      custom_fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Edit a prefix's description/status/site/custom_fields.

        ``custom_fields`` (e.g. ``{"dhcp_enabled": true}``) is MERGED onto the
        prefix's existing custom fields rather than replacing the dict
        outright — a partial update (just the DHCP checkbox) must not blank
        out any other custom field NetBox is tracking on this prefix. This
        was previously silently dropped entirely: the WebUI's "Enable DHCP
        scope" checkbox on Edit never persisted, so a prefix could never
        become a Kea DHCP scope through the edit path, and the checkbox
        never reflected NetBox's actual saved state on reopen.

        A site change cascades to every child prefix (nested inside this one,
        same VRF) in one bulk NetBox update; if that fails the parent's site
        is put back so parent and children never disagree.
        """
        try:
            # Look up the prefix.
            pfx = self.nb.ipam.prefixes.get(prefix_id)
            # Unknown id: error.
            if not pfx:
                return {"status": "ERROR", "message": f"Prefix {prefix_id} not found"}
            # Remember the current scope so a failed cascade can roll back.
            old_scope = (getattr(pfx, "scope_type", None), getattr(pfx, "scope_id", None))
            # Apply only the fields that were passed.
            if description is not None:
                pfx.description = description
            if status:
                pfx.status = status
            # Site change: set the new site, or clear it when empty.
            if site_slug is not None:
                if site_slug:
                    # Unknown site: error.
                    site = self.nb.dcim.sites.get(slug=site_slug)
                    if not site:
                        return {"status": "ERROR", "message": f"Site '{site_slug}' not found"}
                    pfx.scope_type, pfx.scope_id = SITE_SCOPE_TYPE, site.id
                else:
                    pfx.scope_type, pfx.scope_id = None, None
            # Merge custom fields onto the existing ones.
            if custom_fields:
                merged = dict(pfx.custom_fields or {})
                merged.update(custom_fields)
                pfx.custom_fields = merged
            # Save the prefix.
            pfx.save()
            result = {"status": "SUCCESS", "id": pfx.id, "prefix": str(pfx.prefix)}
            # Site changed: push it to child prefixes.
            if site_slug is not None:
                try:
                    result["children_updated"] = self._cascade_prefix_site(pfx)
                except Exception as e:
                    # Cascade failed: restore the old site and report.
                    pfx.scope_type, pfx.scope_id = old_scope
                    pfx.save()
                    raise RuntimeError(f"child prefixes not updated, site change rolled back: {e}")
            # Updated prefix (and child count).
            return result
        except Exception as e:
            # Log and return the error.
            logger.error(f"update_prefix failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def _cascade_prefix_site(self, pfx: Any) -> int:
        """Give every prefix inside ``pfx`` (same VRF) ``pfx``'s site scope."""
        # Every child prefix in the same VRF (or the global table).
        vrf_id = self._obj_vrf_id(pfx)
        children = self._api_get_all("/api/ipam/prefixes/", {
            "within": str(pfx.prefix),
            "vrf_id": vrf_id if vrf_id is not None else "null",
            "limit": 500,
        })
        # One bulk update giving each child the parent's scope.
        updates = [{"id": c["id"], "scope_type": pfx.scope_type, "scope_id": pfx.scope_id}
                   for c in children]
        if updates:
            self.nb.ipam.prefixes.update(updates)
        # Number of children updated.
        return len(updates)

    def delete_prefix(self, prefix_id: int) -> Dict[str, Any]:
        """Delete an IP prefix record by ID."""
        try:
            # Look up the prefix.
            pfx = self.nb.ipam.prefixes.get(prefix_id)
            # Unknown id: error.
            if not pfx:
                return {"status": "ERROR", "message": f"Prefix {prefix_id} not found"}
            # Delete it.
            pfx.delete()
            return {"status": "SUCCESS"}
        except Exception as e:
            # API failure.
            return {"status": "ERROR", "message": str(e)}

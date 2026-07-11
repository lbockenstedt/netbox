"""IPAM prefix/IP methods incl. free-subnet finder + claim for NetboxEngine."""
import ipaddress
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NetboxEngine")


class IpamMixin:
    """IPAM prefix/IP methods incl. free-subnet finder + claim for NetboxEngine."""

    # ─── IPAM – Prefixes / IPs ─────────────────────────────────────────────────

    def get_prefixes(self, site: Optional[str] = None, vrf: Optional[str] = None,
                     tenant: Optional[str] = None) -> Dict[str, Any]:
        try:
            params: Dict[str, Any] = {}
            if site:
                params["site"] = site
            if vrf:
                params["vrf"] = vrf
            if tenant:
                params["tenant"] = tenant
            rows = self._api_get_all("/api/ipam/prefixes/", params)
            prefixes = []
            for p in rows:
                status = p.get("status") or {}
                prefixes.append({
                    "id": p["id"],
                    "prefix": p["prefix"],
                    "status": status.get("value", "") if isinstance(status, dict) else str(status),
                    "site": p["site"]["name"] if p.get("site") else "",
                    "vrf": p["vrf"]["name"] if p.get("vrf") else "",
                    "description": p.get("description") or "",
                    "is_pool": p.get("is_pool", False),
                })
            return {"status": "SUCCESS", "prefixes": prefixes}
        except Exception as e:
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
    ) -> Dict[str, Any]:
        """Allocate a child prefix of a parent.

        When ``requested_prefix`` is supplied, that exact subnet is created
        directly (after verifying it is a subnet of ``parent_prefix``); otherwise
        NetBox auto-allocates the next available child of ``prefix_length``."""
        try:
            parent = self.nb.ipam.prefixes.get(prefix=parent_prefix)
            if not parent:
                return {"status": "ERROR", "message": f"Parent prefix '{parent_prefix}' not found"}

            payload: Dict[str, Any] = {"description": description, "status": status}
            if site_slug:
                site = self.nb.dcim.sites.get(slug=site_slug)
                if site:
                    payload["site"] = site.id
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
                parent_net = ipaddress.ip_network(parent_prefix, strict=False)
                req_net = ipaddress.ip_network(requested_prefix, strict=False)
                if not req_net.subnet_of(parent_net):
                    return {"status": "ERROR", "message": f"{requested_prefix} is not within {parent_prefix}"}
                payload["prefix"] = requested_prefix
                allocated = self.nb.ipam.prefixes.create(payload)
            else:
                payload["prefix_length"] = prefix_length
                allocated = parent.available_prefixes.create(payload)
            return {"status": "SUCCESS", "prefix": str(allocated.prefix), "id": allocated.id, "description": description}
        except Exception as e:
            logger.error(f"allocate_prefix failed: {e}")
            return {"status": "ERROR", "message": str(e)}

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

    @staticmethod
    def _mask_for_hosts(hosts: int) -> int:
        """Smallest prefix length whose usable host count fits ``hosts``.

        Usable = 2^(32-L) - 2 (network + broadcast reserved). Minimum /30
        (2 usable). Capped at /22 (1022 usable) — the largest subnet a tenant
        may request through the self-service finder. A host count that needs
        more than /22 yields /22 rather than going larger."""
        if hosts < 1:
            hosts = 1
        for length in range(30, 21, -1):
            if (1 << (32 - length)) - 2 >= hosts:
                return length
        return 22

    def find_available_prefixes(self, near: str, prefix_length: int = 24,
                                count: int = 20, exact: Optional[str] = None,
                                rfc1918: bool = True) -> Dict[str, Any]:
        """Return up to ``count`` free subnets of ``prefix_length`` closest to
        ``near``, ranked by numeric distance.

        ``near`` anchors the search and must lie within RFC1918. ``exact`` (if
        given) is checked first and returned as distance 0 when free — this is
        the "type a subnet, try it first, else nearest" path."""
        try:
            near_net = ipaddress.ip_network(near, strict=False)
        except (ValueError, TypeError) as e:
            return {"status": "ERROR", "message": f"Invalid 'near' CIDR: {e}"}
        if not 0 <= prefix_length <= 32:
            return {"status": "ERROR", "message": "prefix_length must be 0..32"}
        if not 1 <= count <= 200:
            return {"status": "ERROR", "message": "count must be 1..200"}

        container = next((b for b in self._RFC1918_BLOCKS
                          if near_net.subnet_of(b) or near_net.overlaps(b)), None)
        if not container:
            return {"status": "ERROR",
                    "message": "near must be within RFC1918 (10/8, 172.16/12, 192.168/16)"}

        exact_net = None
        if exact:
            try:
                exact_net = ipaddress.ip_network(exact, strict=False)
            except (ValueError, TypeError) as e:
                return {"status": "ERROR", "message": f"Invalid 'exact' CIDR: {e}"}
            if not (exact_net.subnet_of(container) or exact_net.overlaps(container)):
                return {"status": "ERROR",
                        "message": "exact must be within the same RFC1918 block as near"}

        # Authoritative occupied set = every tenant-assigned prefix inside the
        # containing RFC1918 block. Unassigned/undefined prefixes are free space
        # and do not make a candidate occupied.
        try:
            rows = self._api_get_all("/api/ipam/prefixes/",
                                     {"within_include": str(container), "limit": 500})
        except Exception as e:
            return {"status": "ERROR", "message": f"NetBox prefix fetch failed: {e}"}
        occupied: List[ipaddress.IPv4Network] = []
        for r in rows:
            if not r.get("tenant"):
                continue
            try:
                occupied.append(ipaddress.ip_network(r["prefix"], strict=False))
            except (ValueError, TypeError):
                continue

        def is_free(cand: ipaddress.IPv4Network) -> bool:
            return not any(cand.overlaps(o) for o in occupied)

        step = 1 << (32 - prefix_length)          # address span of one candidate
        base = int(near_net.network_address)
        base = (base // step) * step             # align anchor to candidate grid
        start = int(exact_net.network_address) if exact_net else base
        start = (start // step) * step

        cfirst = int(container.network_address)
        clast = int(container.broadcast_address)
        candidates: List[Dict[str, Any]] = []
        max_offset = (clast - cfirst) // step + 1   # never walk past the block
        i = 0
        while len(candidates) < count and i <= max_offset:
            signs = (0,) if i == 0 else (1, -1)
            for sign in signs:
                cand_int = start + sign * i * step
                if cand_int < cfirst or cand_int > clast:
                    continue
                if cand_int + (step - 1) > clast:
                    continue
                try:
                    cand = ipaddress.ip_network((cand_int, prefix_length))
                except ValueError:
                    continue
                if not is_free(cand):
                    continue
                dist = abs(cand_int - base) // step
                candidates.append({"prefix": str(cand), "distance": dist})
                if len(candidates) >= count:
                    break
            i += 1

        return {"status": "SUCCESS", "available": candidates, "count": len(candidates)}

    def claim_prefix(self, prefix: str, tenant_slug: Optional[str] = None,
                      description: str = "", site_slug: Optional[str] = None,
                      status: str = "active") -> Dict[str, Any]:
        """Assign a specific free subnet to a tenant (the "Assign" action).

        If the prefix already exists in NetBox but has no tenant, reassign it
        (no duplicate). If it exists and is already tenant-assigned, refuse —
        it wasn't actually free. Otherwise create it with the tenant/site
        attached. Tenant/site slug→id resolution mirrors ``allocate_prefix``."""
        try:
            prefix = str(ipaddress.ip_network(prefix, strict=False))
        except (ValueError, TypeError) as e:
            return {"status": "ERROR", "message": f"Invalid prefix: {e}"}

        try:
            existing = self.nb.ipam.prefixes.get(prefix=prefix)
        except Exception as e:
            return {"status": "ERROR", "message": f"NetBox lookup failed: {e}"}

        tenant_id = None
        if tenant_slug:
            tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
            if tenant:
                tenant_id = tenant.id
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
        if tenant_slug and tenant_id is None:
            logger.warning(
                f"claim_prefix: NetBox tenant '{tenant_slug}' not found; "
                f"refusing unattributed create for {prefix}")
            return {"status": "ERROR",
                    "message": f"NetBox tenant '{tenant_slug}' not found — subnet not attributed. Check the tenant's NetBox slug mapping."}

        try:
            if existing:
                if getattr(existing, "tenant", None):
                    return {"status": "ERROR",
                            "message": f"Prefix {prefix} is already assigned to a tenant"}
                if tenant_id is not None:
                    existing.tenant = tenant_id
                if description:
                    existing.description = description
                if status:
                    existing.status = status
                if site_id is not None:
                    existing.site = site_id
                existing.save()
                return {"status": "SUCCESS", "prefix": str(existing.prefix), "id": existing.id}
            payload: Dict[str, Any] = {"prefix": prefix, "status": status,
                                       "description": description}
            if tenant_id is not None:
                payload["tenant"] = tenant_id
            if site_id is not None:
                payload["site"] = site_id
            created = self.nb.ipam.prefixes.create(payload)
            return {"status": "SUCCESS", "prefix": str(created.prefix), "id": created.id}
        except Exception as e:
            logger.error(f"claim_prefix failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def get_ip_addresses(self, prefix: Optional[str] = None, device: Optional[str] = None,
                         tenant: Optional[str] = None) -> Dict[str, Any]:
        try:
            # Paginated: follow NetBox ``next`` links so a tenant with >500 IPs
            # (routine at thousands-of-VMs scale) isn't silently truncated in
            # the IPAM→CPPM endpoint sync. _api_get_all returns the full flat
            # results list (not a dict), capped at max_pages=200 as a runaway
            # guard.
            params: Dict[str, Any] = {}
            if tenant:
                params["tenant"] = tenant
            if prefix:
                params["parent"] = prefix
            if device:
                params["device"] = device
            rows = self._api_get_all("/api/ipam/ip-addresses/", params)
            ips = []
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
                ips.append({
                    "id": ip["id"],
                    "address": ip["address"],
                    "status": status.get("value", "") if isinstance(status, dict) else str(status),
                    "dns_name": ip.get("dns_name") or "",
                    "description": ip.get("description") or "",
                    "assigned_to": ao.get("display", "") if isinstance(ao, dict) else (str(ao) if ao else ""),
                    "device": device_name,
                    # Forward custom_fields so the hub can read mac_address for
                    # IPAM→ClearPass endpoint sync (without this, mac is always
                    # empty at the hub and every record skips in CPPM).
                    "custom_fields": ip.get("custom_fields") or {},
                })
            return {"status": "SUCCESS", "ip_addresses": ips}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def allocate_ip(
        self,
        prefix: str,
        description: str = "",
        dns_name: str = "",
        status: str = "active",
        address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Allocate an IP address from a prefix.

        When ``address`` is supplied, that exact address is created directly
        (mask derived from ``prefix`` if not included, and verified to be inside
        the prefix); otherwise NetBox auto-allocates the next available address."""
        try:
            prefix_obj = self.nb.ipam.prefixes.get(prefix=prefix)
            if not prefix_obj:
                return {"status": "ERROR", "message": f"Prefix '{prefix}' not found"}

            payload: Dict[str, Any] = {"description": description, "status": status}
            if dns_name:
                payload["dns_name"] = dns_name

            if address:
                # Derive the mask from the containing prefix when the caller
                # supplied a bare address (e.g. "10.0.0.5" within "10.0.0.0/24").
                if "/" in address:
                    full_address = address
                else:
                    mask = prefix.split("/")[-1] if "/" in prefix else "32"
                    full_address = f"{address}/{mask}"
                parent_net = ipaddress.ip_network(prefix, strict=False)
                if ipaddress.ip_interface(full_address).ip not in parent_net:
                    return {"status": "ERROR", "message": f"Address {full_address} is not within {prefix}"}
                payload["address"] = full_address
                ip_obj = self.nb.ipam.ip_addresses.create(payload)
            else:
                ip_obj = prefix_obj.available_ips.create(payload)
            return {"status": "SUCCESS", "address": ip_obj.address, "id": ip_obj.id, "dns_name": dns_name}
        except Exception as e:
            logger.error(f"allocate_ip failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def release_ip(self, ip_id: int) -> Dict[str, Any]:
        try:
            ip_obj = self.nb.ipam.ip_addresses.get(ip_id)
            if not ip_obj:
                return {"status": "ERROR", "message": f"IP {ip_id} not found"}
            ip_obj.delete()
            return {"status": "SUCCESS"}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def update_ip_address(self, ip_id: int, dns_name: Optional[str] = None,
                          description: Optional[str] = None,
                          status: Optional[str] = None) -> Dict[str, Any]:
        """Edit an IP address's mutable attributes (dns_name/description/status)."""
        try:
            ip_obj = self.nb.ipam.ip_addresses.get(ip_id)
            if not ip_obj:
                return {"status": "ERROR", "message": f"IP {ip_id} not found"}
            if dns_name is not None:
                ip_obj.dns_name = dns_name
            if description is not None:
                ip_obj.description = description
            if status:
                ip_obj.status = status
            ip_obj.save()
            return {"status": "SUCCESS", "id": ip_obj.id, "address": ip_obj.address}
        except Exception as e:
            logger.error(f"update_ip_address failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def update_prefix(self, prefix_id: int, description: Optional[str] = None,
                      status: Optional[str] = None,
                      site_slug: Optional[str] = None) -> Dict[str, Any]:
        """Edit a prefix's description/status/site."""
        try:
            pfx = self.nb.ipam.prefixes.get(prefix_id)
            if not pfx:
                return {"status": "ERROR", "message": f"Prefix {prefix_id} not found"}
            if description is not None:
                pfx.description = description
            if status:
                pfx.status = status
            if site_slug is not None:
                if site_slug:
                    site = self.nb.dcim.sites.get(slug=site_slug)
                    if not site:
                        return {"status": "ERROR", "message": f"Site '{site_slug}' not found"}
                    pfx.site = site.id
                else:
                    pfx.site = None
            pfx.save()
            return {"status": "SUCCESS", "id": pfx.id, "prefix": str(pfx.prefix)}
        except Exception as e:
            logger.error(f"update_prefix failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def delete_prefix(self, prefix_id: int) -> Dict[str, Any]:
        try:
            pfx = self.nb.ipam.prefixes.get(prefix_id)
            if not pfx:
                return {"status": "ERROR", "message": f"Prefix {prefix_id} not found"}
            pfx.delete()
            return {"status": "SUCCESS"}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

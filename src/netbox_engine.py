import ipaddress
import pynetbox
import logging
import threading
from typing import Any, Dict, List, Optional

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


class NetboxEngine:
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

    # ─── Health ────────────────────────────────────────────────────────────────

    def get_system_health(self) -> Dict[str, Any]:
        try:
            data = self._api_get("/api/dcim/sites/", {"limit": 1})
            return {"status": "SUCCESS", "api_reachable": True, "site_count": data.get("count", 0)}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    # ─── DCIM – Sites / Racks / Devices ───────────────────────────────────────

    def get_sites(self) -> Dict[str, Any]:
        try:
            data = self._api_get("/api/dcim/sites/", {"limit": 500})
            sites = [{"id": s["id"], "name": s["name"], "slug": s["slug"]}
                     for s in data.get("results", [])]
            return {"status": "SUCCESS", "sites": sites}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_racks(self, site: Optional[str] = None, tenant: Optional[str] = None) -> Dict[str, Any]:
        try:
            params: Dict[str, Any] = {"limit": 500}
            if site:
                params["site"] = site
            if tenant:
                params["tenant"] = tenant
            data = self._api_get("/api/dcim/racks/", params)
            racks = []
            for r in data.get("results", []):
                racks.append({
                    "id": r["id"],
                    "name": r["name"],
                    "site": r["site"]["name"] if r.get("site") else "",
                    "u_height": r.get("u_height"),
                    "facility_id": r.get("facility_id"),
                })
            return {"status": "SUCCESS", "racks": racks}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_devices(self, site: Optional[str] = None, rack: Optional[str] = None,
                    tenant: Optional[str] = None) -> Dict[str, Any]:
        try:
            params: Dict[str, Any] = {"limit": 500}
            if site:
                params["site"] = site
            if rack:
                params["rack_id"] = rack
            if tenant:
                params["tenant"] = tenant
            data = self._api_get("/api/dcim/devices/", params)
            devices = []
            for d in data.get("results", []):
                status = d.get("status") or {}
                devices.append({
                    "id": d["id"],
                    "name": d["name"],
                    "status": status.get("value", "") if isinstance(status, dict) else str(status),
                    "site": d["site"]["name"] if d.get("site") else "",
                    "rack": d["rack"]["name"] if d.get("rack") else "",
                    "position": d.get("position"),
                    "device_type": d["device_type"]["display"] if d.get("device_type") else "",
                    "role": d["role"]["name"] if d.get("role") else "",
                    "primary_ip": d["primary_ip"]["address"] if d.get("primary_ip") else "",
                })
            return {"status": "SUCCESS", "devices": devices}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def add_device_to_rack(
        self,
        name: str,
        device_type_slug: str,
        role_slug: str,
        site_slug: str,
        rack_name: str,
        rack_unit: int,
        face: str = "front",
        status: str = "active",
    ) -> Dict[str, Any]:
        """Create a device and place it in a specific rack unit."""
        try:
            site = self.nb.dcim.sites.get(slug=site_slug)
            if not site:
                return {"status": "ERROR", "message": f"Site '{site_slug}' not found"}

            rack = self.nb.dcim.racks.get(name=rack_name, site_id=site.id)
            if not rack:
                return {"status": "ERROR", "message": f"Rack '{rack_name}' not found at site '{site_slug}'"}

            device_type = self.nb.dcim.device_types.get(slug=device_type_slug)
            if not device_type:
                return {"status": "ERROR", "message": f"Device type '{device_type_slug}' not found"}

            role = self.nb.dcim.device_roles.get(slug=role_slug)
            if not role:
                return {"status": "ERROR", "message": f"Role '{role_slug}' not found"}

            device = self.nb.dcim.devices.create(
                name=name,
                device_type=device_type.id,
                role=role.id,
                site=site.id,
                rack=rack.id,
                position=rack_unit,
                face=face,
                status=status,
            )
            return {"status": "SUCCESS", "device_id": device.id, "name": name, "rack": rack_name, "unit": rack_unit}
        except Exception as e:
            logger.error(f"add_device_to_rack failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def get_device_form_options(self) -> Dict[str, Any]:
        """One round trip of the picklists needed to create a device: sites,
        device types, device roles, and tenants. Used by the LM 'Claim an
        unknown device' modal so the user chooses from real NetBox values
        rather than typing slugs blind."""
        try:
            sites = [{"id": s["id"], "name": s["name"], "slug": s["slug"]}
                     for s in self._api_get("/api/dcim/sites/", {"limit": 500}).get("results", [])]
            dt_data = self._api_get("/api/dcim/device-types/", {"limit": 500})
            device_types = []
            for d in dt_data.get("results", []):
                mfr = d.get("manufacturer") or {}
                device_types.append({
                    "id": d["id"],
                    "slug": d.get("slug", ""),
                    "model": d.get("model", ""),
                    "manufacturer": mfr.get("name", "") if isinstance(mfr, dict) else str(mfr),
                })
            dr_data = self._api_get("/api/dcim/device-roles/", {"limit": 500})
            device_roles = [{"id": r["id"], "name": r["name"], "slug": r.get("slug", "")}
                            for r in dr_data.get("results", [])]
            tenants = [{"id": t["id"], "name": t["name"], "slug": t["slug"]}
                       for t in self._api_get("/api/tenancy/tenants/", {"limit": 500}).get("results", [])]
            return {"status": "SUCCESS", "sites": sites, "device_types": device_types,
                    "device_roles": device_roles, "tenants": tenants}
        except Exception as e:
            logger.error(f"get_device_form_options failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def claim_device(self, name: str, device_type_slug: str, role_slug: str,
                     site_slug: str, tenant_slug: str, status: str = "active",
                     description: str = "", ip_address: str = "", mac: str = "",
                     dns_name: str = "") -> Dict[str, Any]:
        """Create a rack-less device for claiming a CPPM unknown endpoint into
        NetBox, owned by ``tenant``, and attach the endpoint's current IP as the
        device's primary IPv4 on an ``mgmt`` interface. The MAC is recorded in
        the description (no custom-field dependency). The hub follows this with
        an endpoint sync so the matching ClearPass endpoint gets the tenant tag
        and leaves 'Unknown Devices'. ``site`` and ``device_type`` are required
        by NetBox; the others are optional."""
        try:
            site = self.nb.dcim.sites.get(slug=site_slug) if site_slug else None
            if site_slug and not site:
                return {"status": "ERROR", "message": f"Site '{site_slug}' not found"}
            device_type = self.nb.dcim.device_types.get(slug=device_type_slug) if device_type_slug else None
            if device_type_slug and not device_type:
                return {"status": "ERROR", "message": f"Device type '{device_type_slug}' not found"}
            role = self.nb.dcim.device_roles.get(slug=role_slug) if role_slug else None
            if role_slug and not role:
                return {"status": "ERROR", "message": f"Role '{role_slug}' not found"}
            tenant = self.nb.tenancy.tenants.get(slug=tenant_slug) if tenant_slug else None
            if tenant_slug and not tenant:
                return {"status": "ERROR", "message": f"Tenant '{tenant_slug}' not found"}

            desc = (description or "").strip()
            if mac:
                desc = f"{desc}\nMAC: {mac}".strip()

            create_kwargs: Dict[str, Any] = {"name": name, "status": status, "description": desc}
            if device_type:
                create_kwargs["device_type"] = device_type.id
            if role:
                create_kwargs["role"] = role.id
            if site:
                create_kwargs["site"] = site.id
            if tenant:
                create_kwargs["tenant"] = tenant.id
            device = self.nb.dcim.devices.create(**create_kwargs)

            attached_ip = ""
            if ip_address and ip_address.strip():
                ip_str = ip_address.strip()
                if "/" in ip_str:
                    full = ip_str
                else:
                    # Derive the mask from the most specific containing prefix;
                    # fall back to /32 (a host route) if the lookup fails/empty.
                    mask = "32"
                    try:
                        pdata = self._api_get("/api/ipam/prefixes/", {"contains": ip_str, "limit": 500})
                        prefs = [ipaddress.ip_network(p["prefix"], strict=False)
                                 for p in pdata.get("results", []) if p.get("prefix")]
                        if prefs:
                            prefs.sort(key=lambda n: n.prefixlen, reverse=True)  # longest first
                            mask = str(prefs[0].prefixlen)
                    except Exception as e:
                        logger.debug(f"containing-prefix lookup for {ip_str} failed, using /32: {e}")
                    full = f"{ip_str}/{mask}"
                iface = device.interfaces.create(name="mgmt", type="other")
                ip_kwargs: Dict[str, Any] = {
                    "address": full,
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": iface.id,
                }
                if tenant:
                    ip_kwargs["tenant"] = tenant.id
                if dns_name:
                    ip_kwargs["dns_name"] = dns_name
                ip_obj = self.nb.ipam.ip_addresses.create(**ip_kwargs)
                # Best-effort: store the MAC on the IP's mac_address custom field
                # so the NetBox→CPPM endpoint sync (which keys on this field) can
                # match the existing ClearPass endpoint by MAC and merge the
                # tenant tag instead of skipping the record. Done via a post-create
                # save inside try/except so a missing/unconfigured custom field
                # never breaks the claim — the device + IP are already created.
                if mac:
                    try:
                        ip_obj.custom_fields = {"mac_address": mac}
                        ip_obj.save()
                    except Exception as e:
                        logger.debug("claim_device: mac_address custom field set on IP %s skipped: %s", full, e)
                device.primary_ip4 = ip_obj.id
                device.save()
                attached_ip = ip_obj.address

            return {"status": "SUCCESS", "device_id": device.id, "name": name,
                    "ip": attached_ip or ip_address, "tenant": tenant_slug or ""}
        except Exception as e:
            logger.error(f"claim_device failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def delete_device(self, device_id: int) -> Dict[str, Any]:
        try:
            device = self.nb.dcim.devices.get(device_id)
            if not device:
                return {"status": "ERROR", "message": f"Device {device_id} not found"}
            device.delete()
            return {"status": "SUCCESS"}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    # ─── IPAM – Prefixes / IPs ─────────────────────────────────────────────────

    def get_prefixes(self, site: Optional[str] = None, vrf: Optional[str] = None,
                     tenant: Optional[str] = None) -> Dict[str, Any]:
        try:
            params: Dict[str, Any] = {"limit": 500}
            if site:
                params["site"] = site
            if vrf:
                params["vrf"] = vrf
            if tenant:
                params["tenant"] = tenant
            data = self._api_get("/api/ipam/prefixes/", params)
            prefixes = []
            for p in data.get("results", []):
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
            params: Dict[str, Any] = {"limit": 500}
            if tenant:
                params["tenant"] = tenant
            if prefix:
                params["parent"] = prefix
            if device:
                params["device"] = device
            data = self._api_get("/api/ipam/ip-addresses/", params)
            ips = []
            for ip in data.get("results", []):
                status = ip.get("status") or {}
                ao = ip.get("assigned_object")
                ips.append({
                    "id": ip["id"],
                    "address": ip["address"],
                    "status": status.get("value", "") if isinstance(status, dict) else str(status),
                    "dns_name": ip.get("dns_name") or "",
                    "description": ip.get("description") or "",
                    "assigned_to": ao.get("display", "") if isinstance(ao, dict) else (str(ao) if ao else ""),
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

    # ─── DCIM – Racks (CRUD) ───────────────────────────────────────────────────

    def add_rack(self, name: str, site_slug: str, u_height: int = 42,
                 facility_id: Optional[str] = None) -> Dict[str, Any]:
        """Create a rack at a site."""
        try:
            site = self.nb.dcim.sites.get(slug=site_slug)
            if not site:
                return {"status": "ERROR", "message": f"Site '{site_slug}' not found"}
            payload: Dict[str, Any] = {"name": name, "site": site.id, "u_height": u_height}
            if facility_id:
                payload["facility_id"] = facility_id
            rack = self.nb.dcim.racks.create(**payload)
            return {"status": "SUCCESS", "rack_id": rack.id, "name": name}
        except Exception as e:
            logger.error(f"add_rack failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def update_rack(self, rack_id: int, name: Optional[str] = None,
                    u_height: Optional[int] = None,
                    facility_id: Optional[str] = None) -> Dict[str, Any]:
        """Edit a rack's name/u_height/facility_id."""
        try:
            rack = self.nb.dcim.racks.get(rack_id)
            if not rack:
                return {"status": "ERROR", "message": f"Rack {rack_id} not found"}
            if name is not None:
                rack.name = name
            if u_height is not None:
                rack.u_height = u_height
            if facility_id is not None:
                rack.facility_id = facility_id
            rack.save()
            return {"status": "SUCCESS", "id": rack.id, "name": rack.name}
        except Exception as e:
            logger.error(f"update_rack failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def delete_rack(self, rack_id: int) -> Dict[str, Any]:
        try:
            rack = self.nb.dcim.racks.get(rack_id)
            if not rack:
                return {"status": "ERROR", "message": f"Rack {rack_id} not found"}
            rack.delete()
            return {"status": "SUCCESS"}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def update_device(self, device_id: int, name: Optional[str] = None,
                      status: Optional[str] = None,
                      rack_name: Optional[str] = None,
                      rack_unit: Optional[int] = None) -> Dict[str, Any]:
        """Edit a device's name/status/rack placement."""
        try:
            device = self.nb.dcim.devices.get(device_id)
            if not device:
                return {"status": "ERROR", "message": f"Device {device_id} not found"}
            if name is not None:
                device.name = name
            if status:
                device.status = status
            if rack_name is not None:
                if rack_name:
                    site = device.site
                    site_id = site.id if site else None
                    rack = self.nb.dcim.racks.get(name=rack_name, site_id=site_id) if site_id \
                        else self.nb.dcim.racks.get(name=rack_name)
                    if not rack:
                        return {"status": "ERROR", "message": f"Rack '{rack_name}' not found"}
                    device.rack = rack.id
                else:
                    device.rack = None
            if rack_unit is not None:
                device.position = rack_unit
            device.save()
            return {"status": "SUCCESS", "id": device.id, "name": device.name}
        except Exception as e:
            logger.error(f"update_device failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    # ─── Legacy methods ────────────────────────────────────────────────────────

    def update_device_ip(self, device_name: str, ip_address: str) -> Dict[str, Any]:
        try:
            device = self.nb.dcim.devices.get(name=device_name)
            if not device:
                return {"status": "ERROR", "message": f"Device {device_name} not found"}
            interface = next((i for i in device.interfaces if i.name == 'eth0'), None)
            if not interface and not device.interfaces:
                return {"status": "ERROR", "message": f"No interfaces for {device_name}"}
            interface = interface or list(device.interfaces)[0]
            ip_obj = next(iter(interface.ip_addresses), None)
            if ip_obj:
                ip_obj.address = ip_address
                ip_obj.save()
            else:
                self.nb.ipam.ip_addresses.create(
                    address=ip_address,
                    assigned_object_type="dcim.interface",
                    assigned_object_id=interface.id,
                )
            return {"status": "SUCCESS", "device": device_name, "ip": ip_address}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def create_vm_entry(self, name: str, cluster: str, vcpus: int, ram: int) -> Dict[str, Any]:
        try:
            site = self.nb.dcim.sites.get(name=cluster)
            if not site:
                return {"status": "ERROR", "message": f"Site {cluster} not found"}
            role = self.nb.dcim.device_roles.get(name="Virtual Machine")
            dev_type = self.nb.dcim.device_types.get(model="Virtual Machine")
            vm = self.nb.dcim.devices.create(
                name=name,
                device_type=dev_type.id if dev_type else None,
                role=role.id if role else None,
                site=site.id,
                description=f"vCPUs: {vcpus}, RAM: {ram}GB",
            )
            return {"status": "SUCCESS", "vm_id": vm.id, "name": name}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def search(self, query: str, tenant: Optional[str] = None) -> Dict[str, Any]:
        """
        Universal search across devices, IPs, and prefixes.
        Returns a normalised list of hits tagged with source="netbox".
        """
        q = query.strip()
        results: List[Dict] = []
        try:
            # Device search
            dev_params: Dict = {"q": q, "limit": 20}
            if tenant:
                dev_params["tenant"] = tenant
            for d in self._api_get("/api/dcim/devices/", dev_params).get("results", []):
                status = d.get("status") or {}
                results.append({
                    "source":      "netbox",
                    "type":        "device",
                    "id":          d["id"],
                    "name":        d.get("name") or "",
                    "status":      status.get("value", "") if isinstance(status, dict) else str(status),
                    "primary_ip":  d["primary_ip"]["address"] if d.get("primary_ip") else "",
                    "site":        d["site"]["name"] if d.get("site") else "",
                    "rack":        d["rack"]["name"] if d.get("rack") else "",
                    "role":        d["role"]["name"] if d.get("role") else "",
                    "device_type": d["device_type"]["display"] if d.get("device_type") else "",
                    "url":         f"/dcim/devices/{d['id']}/",
                })

            # IP address search
            ip_params: Dict = {"q": q, "limit": 20}
            if tenant:
                ip_params["tenant"] = tenant
            for ip in self._api_get("/api/ipam/ip-addresses/", ip_params).get("results", []):
                status = ip.get("status") or {}
                ao = ip.get("assigned_object")
                results.append({
                    "source":      "netbox",
                    "type":        "ip",
                    "id":          ip["id"],
                    "name":        ip.get("address") or "",
                    "dns_name":    ip.get("dns_name") or "",
                    "status":      status.get("value", "") if isinstance(status, dict) else str(status),
                    "assigned_to": ao.get("display", "") if isinstance(ao, dict) else (str(ao) if ao else ""),
                    "url":         f"/ipam/ip-addresses/{ip['id']}/",
                })

            # Prefix search
            pre_params: Dict = {"q": q, "limit": 10}
            if tenant:
                pre_params["tenant"] = tenant
            for pre in self._api_get("/api/ipam/prefixes/", pre_params).get("results", []):
                status = pre.get("status") or {}
                results.append({
                    "source":  "netbox",
                    "type":    "prefix",
                    "id":      pre["id"],
                    "name":    pre.get("prefix") or "",
                    "status":  status.get("value", "") if isinstance(status, dict) else str(status),
                    "site":    pre["site"]["name"] if pre.get("site") else "",
                    "vrf":     pre["vrf"]["name"] if pre.get("vrf") else "",
                    "url":     f"/ipam/prefixes/{pre['id']}/",
                })
        except Exception as e:
            logger.error(f"NetBox search failed: {e}")
            return {"status": "ERROR", "message": str(e), "results": []}

        return {"status": "SUCCESS", "results": results, "count": len(results)}

    # ─── Tenancy ───────────────────────────────────────────────────────────────

    def get_tenants(self) -> Dict[str, Any]:
        try:
            data = self._api_get("/api/tenancy/tenants/", {"limit": 500})
            tenants = [
                {"id": t["id"], "name": t["name"], "slug": t["slug"],
                 "description": t.get("description") or ""}
                for t in data.get("results", [])
            ]
            return {"status": "SUCCESS", "tenants": tenants}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_dhcp_prefixes(self) -> Dict[str, Any]:
        try:
            data = self._api_get("/api/ipam/prefixes/", {"limit": 500})
            scopes = [
                {"prefix": p["prefix"], "gateway": p.get("custom_fields", {}).get("gateway"),
                 "mask": None, "id": p["id"]}
                for p in data.get("results", [])
            ]
            return {"status": "SUCCESS", "scopes": scopes}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

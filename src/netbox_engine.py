import ipaddress
import re
import pynetbox
import logging
import threading
from datetime import datetime, timezone
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
            rows = self._api_get_all("/api/dcim/sites/")
            sites = [{"id": s["id"], "name": s["name"], "slug": s["slug"]}
                     for s in rows]
            return {"status": "SUCCESS", "sites": sites}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_racks(self, site: Optional[str] = None, tenant: Optional[str] = None) -> Dict[str, Any]:
        try:
            params: Dict[str, Any] = {}
            if site:
                params["site"] = site
            if tenant:
                params["tenant"] = tenant
            rows = self._api_get_all("/api/dcim/racks/", params)
            racks = []
            for r in rows:
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
            params: Dict[str, Any] = {}
            if site:
                params["site"] = site
            if rack:
                params["rack_id"] = rack
            if tenant:
                params["tenant"] = tenant
            rows = self._api_get_all("/api/dcim/devices/", params)
            devices = []
            for d in rows:
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
                     for s in self._api_get_all("/api/dcim/sites/")]
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
                       for t in self._api_get_all("/api/tenancy/tenants/")]
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
                iface = self.nb.dcim.interfaces.create(
                    device=device.id, name="mgmt", type="other")
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

    # ─── Virtualization – Proxmox VM sync ──────────────────────────────────────
    # The hub's VmSyncMixin pulls a tenant's VMs from the pxmx (Proxmox) spoke
    # and relays them here via NETBOX_SYNC_VMS so NetBox's virtualization
    # records mirror the live hypervisor inventory. VMs are matched by the
    # `proxmox_unique_id` custom field (created if missing, updated if present),
    # clusters are auto-created under a 'Proxmox' cluster type, and primary_ip4
    # is set from each VM's first IP. Authoritative replace-with-delete runs only
    # when tenant-scoped (a NetBox tenant slug is provided) so a global sync
    # can't delete another tenant's VM records.
    @staticmethod
    def _vm_status_map(s: str) -> str:
        """Proxmox VM status → NetBox VM status value."""
        s = str(s or "").lower()
        if s == "running":
            return "active"
        if s in ("stopped", "paused", "suspended"):
            return "offline"
        return "active"

    # Custom fields the Lab Manager syncs write to. Provisioned idempotently
    # at spoke startup by _ensure_custom_fields() so the Proxmox/Hypervisor→IPAM
    # and Firewall→IPAM syncs don't 400 on a missing custom field (the
    # installer also provisions these via the REST API, but the deployed
    # external NetBox is reached spoke-only where the installer's Django-shell
    # step doesn't run — this is the self-healing safety net). (name, type,
    # label, content_type) — type is the NetBox REST custom-field type string.
    _REQUIRED_CUSTOM_FIELDS = [
        ("proxmox_unique_id", "text", "Proxmox unique id", "virtualization.virtualmachine"),
        ("proxmox_vmid", "text", "Proxmox VMID", "virtualization.virtualmachine"),
        ("proxmox_node", "text", "Proxmox node", "virtualization.virtualmachine"),
        ("proxmox_type", "text", "Proxmox type", "virtualization.virtualmachine"),
        ("discovered_from", "text", "Discovered from", "dcim.device"),
        ("mac_address", "text", "MAC address", "ipam.ipaddress"),
        # mac_address attached to dcim.device too — the access-tracker sync keys
        # existing-device matching by MAC (read off the device's custom field), so
        # it can skip MACs already in NetBox (only-add-missing). Re-uses the same
        # global custom field; _ensure_custom_fields attaches the extra content type.
        ("mac_address", "text", "MAC address", "dcim.device"),
        # Access-tracker (NAC→IPAM reverse sync) topology on the endpoint device:
        ("switch_ip", "text", "Switch IP", "dcim.device"),
        ("switch_port", "text", "Switch port", "dcim.device"),
        ("last_seen", "text", "Last seen", "dcim.device"),
        # last_seen also on VMs + IP addresses so the staleness sweep can age
        # every sync-owned object uniformly. Re-uses the same global field;
        # _ensure_custom_fields attaches the extra content types (a duplicate-
        # name row resolves to the existing field, never a second create).
        ("last_seen", "text", "Last seen", "virtualization.virtualmachine"),
        ("last_seen", "text", "Last seen", "ipam.ipaddress"),
        # Decommission clock: set when staleness_sweep flips an object to
        # offline (7d unseen); when it then ages past delete_days (30d) the
        # object is deleted and its IPs free automatically. Text ISO timestamp.
        ("decommissioned_at", "text", "Decommissioned at", "dcim.device"),
        ("decommissioned_at", "text", "Decommissioned at", "virtualization.virtualmachine"),
        ("vmid_start", "integer", "Proxmox VMID range start", "tenancy.tenant"),
        ("vmid_end", "integer", "Proxmox VMID range end", "tenancy.tenant"),
    ]

    def _ensure_custom_fields(self) -> None:
        """Ensure each of _REQUIRED_CUSTOM_FIELDS exists AND is attached to its
        content type on NetBox.

        A field can exist globally but be unassigned to the object type — in
        that case NetBox rejects writes with "Custom field 'X' does not exist
        for this object type." (the exact sync_vms failure seen after the
        fields were created without content_types). So this get-or-creates each
        field AND verifies/attaches its content_type. Best-effort: a permission
        / API error is logged at WARNING (so it's visible) and swallowed — a
        restricted token must never break a sync. Safe at startup and reconnect.

        Called at spoke startup + reconnect (netbox_spoke.py) AND at the top of
        ``sync_vms`` / ``sync_devices`` so a sync self-heals even if the startup
        call was skipped or failed. Cached per-process via ``_cf_ensured``: a
        clean run (every field present AND attached) sets the flag so subsequent
        syncs skip the list-all; a run with any WARNING leaves it unset so the
        next sync retries (self-healing until the provisioning gap closes).
        """
        if getattr(self, "_cf_ensured", False):
            return
        had_failure = False
        try:
            cf_api = self.nb.extras.custom_fields
            by_name = {str(f.name): f for f in cf_api.all()}
        except Exception as e:
            logger.warning("ensure_custom_fields: list failed: %s", e)
            return  # leave _cf_ensured unset → next sync retries
        for name, ftype, label, content_type in self._REQUIRED_CUSTOM_FIELDS:
            cf = by_name.get(name)
            if cf is None:
                try:
                    cf = cf_api.create(name=name, type=ftype, label=label,
                                       content_types=[content_type])
                    logger.info("ensure_custom_fields: created %s on %s", name, content_type)
                except Exception as e:
                    logger.warning("ensure_custom_fields: create %s failed: %s", name, e)
                    had_failure = True
                    continue
            # Verify the content type is attached (create may not attach it on
            # every NetBox version; a pre-existing field may be unattached).
            try:
                current = list(getattr(cf, "content_types", None) or [])
                if content_type not in current:
                    cf.content_types = current + [content_type]
                    cf.save()
                    logger.info("ensure_custom_fields: attached %s to %s",
                                name, content_type)
            except Exception as e:
                logger.warning("ensure_custom_fields: attach %s to %s failed: %s",
                               name, content_type, e)
                had_failure = True
        if not had_failure:
            self._cf_ensured = True

    @staticmethod
    def _uniq_device_name(base: str, mac: str, real_ip: str,
                          existing_by_name: Dict[str, dict],
                          used_names: set) -> str:
        """Uniquify ``base`` against pre-existing device names AND names already
        used this batch so the NetBox ``(name, site, tenant)`` unique constraint
        can't fire on a create.

        Many firewall-discovered records share a hostname (ks205, sonoszp,
        iphone…) across distinct MACs — genuinely different devices that the
        constraint forces to distinct names. Appends ``-<mac[-4:]>`` (or
        ``-<ip>`` when there's no MAC); if that still collides, a ``-<n>``
        counter guarantees uniqueness. Returns ``base`` unchanged when it
        doesn't collide with either set.
        """
        key = base.lower()
        if key not in existing_by_name and key not in used_names:
            return base
        suffix = (mac.replace(":", "")[-4:] if mac else (real_ip or "x"))
        cand = f"{base}-{suffix}"
        i = 2
        while cand.lower() in existing_by_name or cand.lower() in used_names:
            cand = f"{base}-{suffix}-{i}"
            i += 1
        return cand

    def _ensure_cluster_type(self, name: str = "Proxmox", slug: str = "proxmox"):
        """Return the 'Proxmox' cluster type (creating it if missing). Best-effort."""
        try:
            ct = self.nb.virtualization.cluster_types.get(name=name)
            if ct:
                return ct
            return self.nb.virtualization.cluster_types.create(name=name, slug=slug)
        except Exception as e:
            logger.debug("ensure_cluster_type failed: %s", e)
            return None

    def _ensure_vm_cluster(self, name: str, tenant=None) -> Optional[int]:
        """Return a NetBox cluster id for ``name``, auto-creating it under the
        'Proxmox' cluster type. None if it can't be resolved/created."""
        if not name:
            return None
        try:
            c = self.nb.virtualization.clusters.get(name=name)
            if c:
                return c.id
            ctype = self._ensure_cluster_type()
            kwargs: Dict[str, Any] = {"name": name}
            if ctype:
                kwargs["type"] = ctype.id
            if tenant:
                kwargs["tenant"] = tenant.id
            c = self.nb.virtualization.clusters.create(**kwargs)
            return c.id
        except Exception as e:
            logger.debug("ensure_vm_cluster %s failed: %s", name, e)
            return None

    # ── change-log + IP-reuse helpers (shared by the external-source syncs) ────

    def _journal(self, content_type: str, object_id: Any, module: str,
                 note: str = "") -> None:
        """Write a NetBox **journal entry** on ``object_id`` (of NetBox content
        type ``dcim.device`` / ``ipam.ipaddress`` / ``dcim.interface`` /
        ``virtualization.virtualmachine`` / ``dcim.cable``) recording which LM
        sync module created it and when. The Journal tab is NetBox's native
        per-object change log, so this is the audit trail the user asked for
        ("comments to the change log for what module added the entry and when").

        Best-effort by design: a journal failure (older NetBox without the
        journal endpoint, a content-type mismatch, a transient 4xx) must NEVER
        break a sync — it's logged at DEBUG and swallowed.
        """
        if not object_id:
            return
        try:
            when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            comment = f"Created by LM {module} sync at {when}"
            if note:
                comment += f" — {note}"
            self.nb.extras.journal_entries.create(
                assigned_object_type=content_type,
                assigned_object_id=int(object_id),
                kind="info",
                comment=comment,
            )
        except Exception as e:
            logger.debug("journal %s/%s (%s) failed: %s",
                         content_type, object_id, module, e)

    def _reuse_or_create_ip(self, addr: str, create_kwargs: Dict[str, Any],
                            bare_ip: str, iface_id: int, tenant: Any = None,
                            hostname: str = "", mac: str = "",
                            source: str = "sync",
                            iface_type: str = "dcim.interface") -> Any:
        """Return an ``ipam.ip_address`` for ``addr`` (``host/prefix``), reusing
        an existing **global** record when one already exists and reassigning it
        to ``iface_id``, else creating a new one.

        NetBox enforces global IP uniqueness, so a discovery source that tries
        to create an IP the IPAM already provisioned 400s with ``Duplicate IP
        address found in global table`` — that was failing ~every record in
        ``sync_devices`` because NetBox (the IPAM source of truth) already held
        most of the discovered addresses. Reusing the existing record and
        pointing it at the discovered device's NIC fixes that without losing the
        address, and tags MAC/tenant/dns_name best-effort.

        The create path propagates a real failure (so the caller records it);
        the reuse path never raises. A mask-mismatch duplicate on create falls
        back to a bare-IP lookup + reassign so the record isn't lost.
        """
        # 1) Proactive reuse: an exact host/prefix match already exists.
        ipobj = None
        try:
            ipobj = self.nb.ipam.ip_addresses.get(address=addr)
        except Exception as e:
            logger.debug("%s: existing-IP lookup %s failed: %s", source, addr, e)
        if ipobj:
            self._reassign_ip(ipobj, iface_id, tenant, hostname, mac, source, addr,
                             iface_type)
            return ipobj

        # 2) No existing record — create one.
        try:
            ipobj = self.nb.ipam.ip_addresses.create(**create_kwargs)
            self._tag_ip_mac(ipobj, mac, source, addr)
            return ipobj
        except Exception as create_err:
            # 3) Mask-mismatch duplicate: the existing record has a different
            # prefix length than we computed, so the exact lookup missed it but
            # the create still 400s. Fall back to a bare-IP lookup + reassign.
            matches: List[Any] = []
            try:
                matches = list(self.nb.ipam.ip_addresses.filter(address=bare_ip))
            except Exception:
                matches = []
            if not matches:
                raise create_err
            ipobj = matches[0]
            self._reassign_ip(ipobj, iface_id, tenant, hostname, mac, source, bare_ip,
                             iface_type)
            return ipobj

    def _reassign_ip(self, ipobj: Any, iface_id: int, tenant: Any,
                     hostname: str, mac: str, source: str, addr: str,
                     iface_type: str = "dcim.interface") -> None:
        """Reassign an existing ipam.ip_address to ``iface_id`` and best-effort
        tag tenant/dns_name/MAC. Never raises (reuse is best-effort)."""
        try:
            changed = False
            if getattr(ipobj, "assigned_object_id", None) != iface_id:
                ipobj.assigned_object_type = iface_type
                ipobj.assigned_object_id = iface_id
                changed = True
            if tenant and getattr(ipobj, "tenant", None) != tenant.id:
                ipobj.tenant = tenant.id
                changed = True
            if hostname and hostname.lower() != "unknown" and \
                    (getattr(ipobj, "dns_name", "") or "") != hostname:
                ipobj.dns_name = hostname
                changed = True
            if changed:
                ipobj.save()
        except Exception as e:
            logger.debug("%s: reuse-IP %s reassign failed: %s", source, addr, e)
        self._tag_ip_mac(ipobj, mac, source, addr)

    def _tag_ip_mac(self, ipobj: Any, mac: str, source: str, addr: str) -> None:
        """Best-effort write ``mac_address`` onto an ipam.ip_address custom
        field. Never raises (the IP is still synced without the MAC tag)."""
        if not mac:
            return
        try:
            m = dict(ipobj.custom_fields or {})
            if m.get("mac_address") != mac:
                m["mac_address"] = mac
                ipobj.custom_fields = m
                ipobj.save()
        except Exception as e:
            logger.debug("%s: mac_address on IP %s skipped: %s", source, addr, e)

    def _stamp_last_seen(self, obj: Any, when: str = "") -> None:
        """Write the ``last_seen`` custom field (ISO UTC) on ``obj`` so the
        staleness sweep can age it. Best-effort: a missing field / save failure
        is logged at DEBUG and swallowed — a staleness signal must never break
        the sync that produced it. ``when`` defaults to now (UTC)."""
        try:
            ts = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            m = dict(obj.custom_fields or {})
            if m.get("last_seen") != ts:
                m["last_seen"] = ts
                obj.custom_fields = m
                obj.save()
        except Exception as e:
            logger.debug("stamp_last_seen on %r failed: %s", obj, e)

    def _assign_vm_primary_ip4(self, vm_obj, vm: dict, tenant=None) -> None:
        """Build the VM's interfaces in NetBox from the per-interface records
        the pxmx agent gathers, set ``primary_ip4`` from the first IP, and
        journal-stamp each created vminterface + IP.

        ``vm["interfaces"]`` is ``[{name, mac, ips:[..]}, ...]`` (pxmx agent
        ``_vm_interfaces``). For each interface a vminterface is reused-by-name
        (or created) carrying the native ``mac_address``; each guest IP becomes
        an ``ipam.ip_address`` assigned to that vminterface via
        ``_reuse_or_create_ip`` (global-IP uniqueness respected) and tagged with
        the interface MAC. Falls back to a single ``eth0`` vminterface + the
        legacy flat ``vm["ips"]`` list when the agent sent no interface records
        (older spoke). Never raises — a missing/unassignable IP must not break
        the VM record it follows."""
        ifaces_in = list((vm or {}).get("interfaces") or [])
        # Back-compat: an older pxmx agent that sent only a flat ``ips`` list
        # (no per-interface MAC) → one eth0 vminterface holding those IPs.
        if not ifaces_in:
            flat_ips = list((vm or {}).get("ips") or [])
            if flat_ips:
                ifaces_in = [{"name": "eth0", "mac": "", "ips": flat_ips}]
        if not ifaces_in:
            return
        try:
            existing = list(self.nb.virtualization.vminterfaces.filter(
                virtual_machine_id=vm_obj.id))
        except Exception as e:
            logger.debug("assign_vm_primary_ip4: list vminterfaces %s failed: %s",
                         vm_obj.id, e)
            existing = []
        by_name = {getattr(i, "name", ""): i for i in existing}
        first_ip_id = None
        for ifc in ifaces_in:
            name = str(ifc.get("name") or "").strip() or "eth0"
            mac = self._norm_mac(str(ifc.get("mac") or ""))
            ips = [str(x).split("/")[0].strip() for x in (ifc.get("ips") or [])
                   if str(x or "").strip()]
            if not ips and not mac:
                continue
            try:
                iface = by_name.get(name)
                if iface is None:
                    kw: Dict[str, Any] = {"virtual_machine": vm_obj.id, "name": name}
                    if mac:
                        kw["mac_address"] = mac
                    iface = self.nb.virtualization.vminterfaces.create(**kw)
                    self._journal("virtualization.vminterface", iface.id,
                                   "hypervisor-vm-sync",
                                   note=f"vminterface {name} for VM "
                                        f"{getattr(vm_obj, 'name', '')}")
                else:
                    # Refresh the MAC if the interface exists but is MAC-less.
                    if mac and not getattr(iface, "mac_address", None):
                        try:
                            iface.mac_address = mac
                            iface.save()
                        except Exception as e:
                            logger.debug("assign_vm_primary_ip4: mac refresh %s: %s",
                                         name, e)
            except Exception as e:
                logger.debug("assign_vm_primary_ip4: vminterface %s failed: %s", name, e)
                continue
            for ip_str in ips:
                if not ip_str:
                    continue
                try:
                    mask = self._mask_for_ip(ip_str)
                    full = ip_str if "/" in ip_str else f"{ip_str}/{mask}"
                    ip_kwargs: Dict[str, Any] = {
                        "address": full,
                        "assigned_object_type": "virtualization.vminterface",
                        "assigned_object_id": iface.id,
                    }
                    if tenant:
                        ip_kwargs["tenant"] = tenant.id
                    ip_obj = self._reuse_or_create_ip(
                        full, ip_kwargs, ip_str, iface.id, tenant,
                        hostname=str(getattr(vm_obj, "name", "") or ""),
                        mac=mac, source="hypervisor-vm-sync",
                        iface_type="virtualization.vminterface")
                    self._journal("ipam.ipaddress", ip_obj.id,
                                   "hypervisor-vm-sync",
                                   note=f"VM {getattr(vm_obj, 'name', '')} {name}")
                    if first_ip_id is None:
                        first_ip_id = getattr(ip_obj, "id", None)
                except Exception as e:
                    logger.debug("assign_vm_primary_ip4: IP %s on %s failed: %s",
                                 ip_str, name, e)
        if first_ip_id is not None:
            try:
                vm_obj.primary_ip4 = first_ip_id
                vm_obj.save()
            except Exception as e:
                logger.debug("assign_vm_primary_ip4: set primary_ip4 failed: %s", e)

    # ---- firewall→NetBox device discovery sync helpers ---------------------

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

    def _mask_for_ip(self, ip_str: str) -> str:
        """Derive the mask from the most specific containing prefix; /32 if none.

        Mirrors the inline lookup in ``claim_device`` (engine.py ~296-304),
        extracted so the device sync reuses it.
        """
        try:
            pdata = self._api_get("/api/ipam/prefixes/", {"contains": ip_str, "limit": 500})
            prefs = [ipaddress.ip_network(p["prefix"], strict=False)
                     for p in pdata.get("results", []) if p.get("prefix")]
            if prefs:
                prefs.sort(key=lambda n: n.prefixlen, reverse=True)  # longest first
                return str(prefs[0].prefixlen)
        except Exception as e:
            logger.debug("containing-prefix lookup for %s failed, using /32: %s", ip_str, e)
        return "32"

    def _ensure_device_role(self, slug: str = "discovered"):
        """Return the device role (auto-creating 'discovered' if missing). Best-effort."""
        slug = (slug or "discovered").strip().lower() or "discovered"
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
        'Unknown' manufacturer if missing). Best-effort."""
        slug = (slug or "discovered").strip().lower() or "discovered"
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

    def _resolve_site(self, slug: str = "", tenant=None):
        """Resolve a site for device creation. Configured slug first; else the
        first site as a fallback. None if none resolve (site is optional for a
        NetBox device). Best-effort."""
        slug = (slug or "").strip().lower()
        if slug:
            try:
                s = self.nb.dcim.sites.get(slug=slug)
                if s:
                    return s
            except Exception as e:
                logger.warning("resolve_site '%s' failed: %s", slug, e)
        try:
            sites = list(self.nb.dcim.sites.filter(limit=1))
            if sites:
                return sites[0]
        except Exception as e:
            # site is optional for a device, so this stays best-effort — but
            # warn so a permissions issue is visible rather than silent.
            logger.warning("resolve_site first-site fallback failed: %s", e)
        return None

    # Per-tenant breakdown key used when a VM carries no tenant slug (untagged
    # / no NetBox tenant). Mirrors the hub's VmSyncMixin._VM_SYNC_UNASSIGNED_KEY.
    _VM_SYNC_UNASSIGNED_KEY = "__unassigned__"

    def sync_vms(self, vms: list, tenant_slug: str = "",
                 replace: bool = False,
                 source_of_truth: str = "external") -> Dict[str, Any]:
        """Push a set of Proxmox VMs into NetBox virtualization records (grab-all).

        Each incoming VM carries its own ``tenant_slug`` (None/'' → created with
        no NetBox tenant, i.e. a global/unassigned record). The batch
        ``tenant_slug`` is only a fallback for VMs that don't carry one (legacy
        callers). Each VM is matched by ``custom_fields.proxmox_unique_id`` —
        created if missing, updated if present; a VM that changed tenants just
        gets its ``tenant`` updated (never deleted-and-recreated). Clusters are
        auto-created; ``primary_ip4`` is set from the first IP in each VM's
        ``ips`` list.

        When ``replace`` is set, NetBox VMs carrying our ``proxmox_unique_id``
        custom field whose uid is NOT in the incoming full set are deleted
        (cluster-wide — the VM was destroyed in Proxmox). Manually-created
        NetBox VMs (no ``proxmox_unique_id``) are never touched, so a global
        sync can't delete records it doesn't own.

        Returns ``{status, pushed, errors, skipped, deleted, vms_total,
        message, per_tenant}`` where ``per_tenant`` maps tenant-slug (or
        ``__unassigned__``) → ``{pushed, errors, skipped, deleted, vms_total}``
        so the hub can record per-tenant last-sync status from one batch.
        """
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None   # first per-record failure text (diagnosability)
        per_tenant: Dict[str, Dict[str, int]] = {}
        UNASSIGNED = self._VM_SYNC_UNASSIGNED_KEY

        def _bucket(slug: Optional[str]) -> Dict[str, int]:
            key = str(slug or "").strip() or UNASSIGNED
            b = per_tenant.get(key)
            if b is None:
                b = {"pushed": 0, "errors": 0, "skipped": 0,
                     "deleted": 0, "vms_total": 0}
                per_tenant[key] = b
            return b

        # slug -> tenant object cache (None for unassigned). '' → None.
        tenant_cache: Dict[str, Any] = {}

        def _resolve_tenant(slug: Optional[str]):
            s = str(slug or "").strip()
            if not s:
                return None
            if s in tenant_cache:
                return tenant_cache[s]
            try:
                t = self.nb.tenancy.tenants.get(slug=s)
            except Exception as e:
                logger.debug("sync_vms: resolve tenant %s failed: %s", s, e)
                t = None
            tenant_cache[s] = t
            return t

        try:
            # Self-heal proxmox_* custom fields on virtualization.virtualmachine
            # so the linkage PATCHes below land. Cached per-process.
            self._ensure_custom_fields()
            incoming: Dict[str, Dict[str, Any]] = {}
            for vm in (vms or []):
                uid = str((vm or {}).get("unique_id") or "").strip()
                if not uid:
                    skipped += 1
                    _bucket((vm or {}).get("tenant_slug") or tenant_slug)["skipped"] += 1
                    continue
                # Backfill a missing per-VM slug from the legacy batch slug.
                if not str((vm or {}).get("tenant_slug") or "").strip() and tenant_slug:
                    vm = dict(vm or {})
                    vm["tenant_slug"] = tenant_slug
                incoming[uid] = vm or {}

            # Index ALL existing NetBox VMs that carry a proxmox_unique_id
            # (proxmox-sourced) — cluster-wide, so replace-delete can remove
            # VMs destroyed in Proxmox regardless of which tenant owns them.
            existing: Dict[str, dict] = {}  # uid -> raw row dict (carries "id")
            try:
                rows = self._api_get_all("/api/virtualization/virtual-machines/",
                                         {"limit": 500})
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox VMs: {e}",
                        "pushed": 0, "errors": 0, "skipped": skipped,
                        "deleted": 0, "vms_total": len(incoming),
                        "per_tenant": per_tenant}
            for row in rows:
                cf = row.get("custom_fields") or {}
                uid = str((cf.get("proxmox_unique_id") or "").strip())
                if uid:
                    existing[uid] = row

            # Replace-delete (cluster-wide): drop proxmox-sourced NetBox VMs
            # whose uid is no longer in the incoming full set. Attribute each
            # delete to the row's tenant for per-tenant reporting.
            if replace:
                for uid, row in list(existing.items()):
                    if uid in incoming:
                        continue
                    rslug = ""
                    rten = row.get("tenant")
                    if isinstance(rten, dict):
                        rslug = str(rten.get("slug") or "")
                    try:
                        obj = self.nb.virtualization.virtual_machines.get(row["id"])
                        if obj:
                            obj.delete()
                            deleted += 1
                            _bucket(rslug)["deleted"] += 1
                    except Exception as e:
                        errors += 1
                        _bucket(rslug)["errors"] += 1
                        if first_err is None:
                            first_err = f"delete {uid}: {e}"
                        logger.debug("sync_vms: delete stale %s failed: %s", uid, e)

            for uid, vm in incoming.items():
                vslug = vm.get("tenant_slug")
                tenant = _resolve_tenant(vslug)
                b = _bucket(vslug)
                b["vms_total"] += 1
                try:
                    cluster_id = self._ensure_vm_cluster(
                        str(vm.get("cluster") or "").strip(), tenant)
                    name = str(vm.get("name") or "").strip() or f"vm-{vm.get('vmid') or uid}"
                    status = self._vm_status_map(vm.get("status"))
                    vcpus = int(vm.get("vcpus") or 0)
                    disk_gb = round(float(vm.get("disk_gb") or 0), 1)
                    mem_mb = int(vm.get("mem_mb") or 0)
                    cf = {
                        "proxmox_unique_id": uid,
                        "proxmox_vmid": str(vm.get("vmid") or ""),
                        "proxmox_node": str(vm.get("node") or ""),
                        "proxmox_type": str(vm.get("type") or ""),
                        # last_seen clocks the staleness sweep from this detection
                        # (folded into the cf PATCH so it rides the same save — no
                        # extra write per VM). Best-effort: a missing field is
                        # swallowed by the cf-PATCH try/except below.
                        "last_seen": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"),
                    }
                    if uid in existing:
                        obj = self.nb.virtualization.virtual_machines.get(existing[uid]["id"])
                        if not obj:
                            errors += 1
                            b["errors"] += 1
                            continue
                        # source_of_truth=="netbox" → NetBox is the source of truth
                        # for VMs: only-add-missing. The VM already exists, so do
                        # NOT overwrite any field Proxmox would otherwise clobber
                        # (name/cluster/status/vcpus/disk/memory/tenant/proxmox_*).
                        # We still refresh last_seen (a staleness signal, not a
                        # truth field) so a seen VM isn't swept. "external"
                        # (Proxmox is the source of truth) overwrites as before.
                        if source_of_truth == "netbox":
                            self._stamp_last_seen(obj)
                            pushed += 1
                            b["pushed"] += 1
                            continue
                        obj.name = name
                        if cluster_id:
                            obj.cluster = cluster_id
                        obj.status = status
                        if vcpus:
                            obj.vcpus = vcpus
                        if disk_gb:
                            obj.disk = int(disk_gb)
                        if mem_mb:
                            obj.memory = mem_mb
                        # Set/clear so a VM that changed tags moves tenant
                        # (or drops to unassigned) without a delete+recreate.
                        obj.tenant = tenant.id if tenant else None
                        obj.save()  # core fields — always syncs even if cf unprovisioned
                        # proxmox_* linkage is best-effort: the deployed NetBox
                        # may not have the custom fields attached to
                        # virtualization.virtualmachine yet, and a 400 here must
                        # NOT undo the core update above. The create path sets
                        # them once the fields exist (next sync after ensure).
                        try:
                            merged = dict(obj.custom_fields or {})
                            merged.update(cf)
                            obj.custom_fields = merged
                            obj.save()
                        except Exception as e:
                            logger.warning("sync_vms: custom_fields update %s skipped "
                                           "(field unprovisioned?): %s", uid, e)
                        # last_seen is folded into ``cf`` above → rides the cf PATCH.
                    else:
                        # Create WITHOUT inline custom_fields: a create carrying
                        # custom_fields 400s ("Custom field 'proxmox_node' does
                        # not exist for this object type") when the field isn't
                        # attached to virtualization.virtualmachine on the
                        # deployed NetBox — which blocked ALL VM syncs (0/N).
                        # Sync the VM first, then PATCH the proxmox_* linkage
                        # best-effort so a provisioning gap never blocks the
                        # sync. The update path sets them once fields exist.
                        create_kwargs: Dict[str, Any] = {
                            "name": name, "status": status}
                        if cluster_id:
                            create_kwargs["cluster"] = cluster_id
                        if vcpus:
                            create_kwargs["vcpus"] = vcpus
                        if disk_gb:
                            create_kwargs["disk"] = int(disk_gb)
                        if mem_mb:
                            create_kwargs["memory"] = mem_mb
                        if tenant:
                            create_kwargs["tenant"] = tenant.id
                        obj = self.nb.virtualization.virtual_machines.create(**create_kwargs)
                        try:
                            obj.custom_fields = cf
                            obj.save()
                        except Exception as e:
                            logger.warning("sync_vms: custom_fields set on new VM %s "
                                           "skipped (field unprovisioned?): %s", uid, e)
                        self._journal("virtualization.virtualmachine", obj.id,
                                      "hypervisor-vm-sync",
                                      note=f"VM {name} ({uid})")
                        # last_seen folded into ``cf`` above → rides the cf save.
                    # vminterfaces + all IPs + primary_ip4 (best-effort) — built
                    # from the per-interface records the pxmx agent gathers.
                    self._assign_vm_primary_ip4(obj, vm, tenant)
                    pushed += 1
                    b["pushed"] += 1
                except Exception as e:
                    errors += 1
                    b["errors"] += 1
                    if first_err is None:
                        first_err = f"upsert {uid}: {e}"
                    logger.debug("sync_vms: upsert %s failed: %s", uid, e)

            msg = (f"{pushed} VM(s) upserted, {deleted} deleted, "
                   f"{skipped} skipped, {errors} errors")
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("sync_vms: %s", msg)
            else:
                logger.info("sync_vms: %s", msg)
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "vms_total": len(incoming), "message": msg,
                    "per_tenant": per_tenant}
        except Exception as e:
            logger.error("sync_vms failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "vms_total": len(vms or []), "per_tenant": per_tenant}

    def get_tenant_vmid_range(self, tenant_slug: str = "") -> Dict[str, Any]:
        """Read a NetBox tenant's Proxmox VMID allocation range + in-use VMIDs.

        Returns ``{status, vmid_start, vmid_end, used_vmids}`` where
        ``vmid_start``/``vmid_end`` come from the tenant's
        ``vmid_start``/``vmid_end`` custom fields (None when the tenant has no
        range set), and ``used_vmids`` is the sorted list of ``proxmox_vmid``
        custom-field values on that tenant's VMs (only those inside the range,
        when a range is set). Used by the LM hub's VMID auto-allocation knob
        to pick the next free VMID inside a tenant's range.

        ``status`` is ``SUCCESS`` with ``vmid_start``/``vmid_end`` = None when
        the tenant exists but has no range (→ caller falls back to Proxmox
        nextid), or ``ERROR`` when the tenant can't be resolved / the read
        fails (→ caller also falls back).
        """
        try:
            slug = str(tenant_slug or "").strip()
            if not slug:
                return {"status": "ERROR", "message": "no tenant_slug",
                        "vmid_start": None, "vmid_end": None, "used_vmids": []}
            tenant = self.nb.tenancy.tenants.get(slug=slug)
            if not tenant:
                return {"status": "ERROR",
                        "message": f"NetBox tenant '{slug}' not found",
                        "vmid_start": None, "vmid_end": None, "used_vmids": []}
            cf = tenant.custom_fields or {}
            start = cf.get("vmid_start")
            end = cf.get("vmid_end")

            def _as_int(v):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None

            start_i, end_i = _as_int(start), _as_int(end)

            used: List[int] = []
            try:
                rows = self._api_get_all("/api/virtualization/virtual-machines/",
                                         {"limit": 500, "tenant": slug})
            except Exception as e:
                logger.debug("get_tenant_vmid_range: list VMs for %s failed: %s",
                             slug, e)
                rows = []
            for row in rows:
                rc = (row.get("custom_fields") or {})
                vid = _as_int(rc.get("proxmox_vmid"))
                if vid is None:
                    continue
                if start_i is not None and end_i is not None:
                    if not (start_i <= vid <= end_i):
                        continue
                used.append(vid)
            used = sorted(set(used))
            return {"status": "SUCCESS",
                    "vmid_start": start_i, "vmid_end": end_i,
                    "used_vmids": used}
        except Exception as e:
            logger.error("get_tenant_vmid_range failed: %s", e)
            return {"status": "ERROR", "message": str(e),
                    "vmid_start": None, "vmid_end": None, "used_vmids": []}

    def sync_devices(self, devices: list, tenant_slug: str = "",
                     replace: bool = False,
                     defaults: Optional[Dict[str, Any]] = None,
                     source: str = "opnsense",
                     source_of_truth: str = "external") -> Dict[str, Any]:
        """Push a tenant's discovery-source device set into NetBox DCIM.

        Source = a discovery feed relayed by the hub (OPNsense DHCP leases +
        ARP for the firewall sync; switch/gateway ARP tables for the nw sync).
        Each incoming record ``{ip, mac, hostname}`` is matched to an existing
        device by its primary IPv4; missing devices are created (mirroring
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

        Returns ``{status, pushed, errors, skipped, deleted, devices_total, message}``.
        """
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None   # first per-record failure text (diagnosability)
        defaults = defaults or {}
        # Normalize the ownership tag: firewall synonyms collapse to the legacy
        # "opnsense" tag (so existing firewall deployments are byte-identical);
        # anything else (nw's "Network Devices") is used verbatim. Comparison +
        # replace-delete scoping are case-insensitive against this tag.
        source_tag = str(source or "opnsense").strip()
        if source_tag.lower() in ("opnsense", "fw", "firewall"):
            source_tag = "opnsense"
        source_tag_l = source_tag.lower()
        def _owns(cf: dict) -> bool:
            return str(((cf or {}).get("discovered_from") or "")).lower() == source_tag_l
        try:
            # Self-heal custom fields (discovered_from on dcim.device,
            # mac_address on ipam.ipaddress) so the ownership tag + MAC writes
            # below land. Cached per-process; no-op once provisioned.
            self._ensure_custom_fields()
            tenant = None
            if tenant_slug:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if not tenant:
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — "
                                       f"firewall-discovered devices not attributed. "
                                       f"Check the tenant's NetBox slug mapping.",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "devices_total": len(devices or [])}

            # Normalize incoming: ip (mask stripped) -> {mac, hostname}.
            incoming: Dict[str, Dict[str, str]] = {}
            for dev in (devices or []):
                if not isinstance(dev, dict):
                    continue
                ip = str(dev.get("ip") or "").strip().split("/")[0].strip()
                mac = self._norm_mac(dev.get("mac", ""))
                hostname = str(dev.get("hostname") or "").strip()
                if not ip and not mac:
                    skipped += 1
                    continue
                if not ip:
                    # MAC-only: index by mac-key so it's at least created, but
                    # replace-delete (which keys on IP) won't track it.
                    ip = f"mac:{mac}"
                incoming[ip] = {"mac": mac, "hostname": hostname}

            # Index existing tenant devices by primary IPv4 (all) + track which
            # of those we own (discovered_from tag) for replace-delete. Also
            # index by name (lowercased) for ALL rows — including devices with
            # no primary_ip4, which the IP index skips below but whose name can
            # still collide on the (name, site, tenant) unique constraint when
            # the create branch re-uses device-<mac> after a DHCP IP move.
            existing_by_ip: Dict[str, dict] = {}   # ip_str -> raw device row
            existing_by_name: Dict[str, dict] = {}  # name.lower() -> raw device row
            owned_ips: set = set()                   # primary IPs of tagged devices
            # Intra-batch dedup: many discovery records share a hostname across
            # distinct MACs (ks205, sonoszp, iphone…). existing_by_name is a
            # pre-batch snapshot and can't see names created earlier THIS batch,
            # so a 2nd create with the same name 400s on (name, site, tenant).
            # used_names tracks every name we create/refresh this batch.
            used_names: set = set()
            # device ids handled via the IP-match refresh path — the create
            # branch must never reclaim/clobber one of these by name (a
            # duplicate-hostname record would otherwise delete a device we just
            # refreshed for a different IP).
            refreshed_ids: set = set()
            list_params: Dict[str, Any] = {"limit": 500}
            if tenant_slug:
                list_params["tenant"] = tenant_slug
            try:
                rows = self._api_get_all("/api/dcim/devices/", list_params)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": skipped, "deleted": 0,
                        "devices_total": len(incoming)}
            for row in rows:
                rname = str(row.get("name") or "").strip().lower()
                if rname:
                    existing_by_name.setdefault(rname, row)  # first row wins
                pip = row.get("primary_ip4")
                addr = ""
                if isinstance(pip, dict):
                    addr = (pip.get("address") or "").split("/")[0].strip()
                if not addr:
                    continue
                existing_by_ip[addr] = row
                cf = row.get("custom_fields") or {}
                if _owns(cf):
                    owned_ips.add(addr)

            # Replace-with-delete — only when tenant-scoped, only owned devices.
            if replace and tenant_slug:
                for ip_str in list(owned_ips - set(incoming.keys())):
                    row = existing_by_ip.get(ip_str)
                    if not row:
                        continue
                    try:
                        obj = self.nb.dcim.devices.get(row["id"])
                        if obj:
                            obj.delete()
                            deleted += 1
                    except Exception as e:
                        errors += 1
                        if first_err is None:
                            first_err = f"delete {ip_str}: {e}"
                        logger.debug("sync_devices: delete stale %s failed: %s", ip_str, e)

            role = self._ensure_device_role(defaults.get("role") or "discovered")
            dtype = self._ensure_device_type(defaults.get("device_type") or "discovered")
            site = self._resolve_site(defaults.get("site") or "", tenant)

            for ip_str, rec in incoming.items():
                mac = rec["mac"]
                hostname = rec["hostname"]
                is_mac_key = ip_str.startswith("mac:")
                real_ip = "" if is_mac_key else ip_str
                try:
                    row = existing_by_ip.get(ip_str) if not is_mac_key else None
                    if row:
                        # Existing device matched by IP — refresh its IP's MAC +
                        # dns_name (the goal: populate mac_address so the endpoint
                        # sync can match). Rename only if we own it. Remember its
                        # id so the create branch never reclaims/clobbers a device
                        # we just refreshed when a duplicate hostname shows up.
                        refreshed_ids.add(row["id"])
                        # The refreshed device keeps (or is renamed to) a name
                        # that's now occupied in NetBox — track it so a later
                        # duplicate-hostname create this batch uniquifies instead
                        # of colliding on (name, site, tenant).
                        rname = str(row.get("name") or "").strip().lower()
                        if rname:
                            used_names.add(rname)
                        pip = row.get("primary_ip4") or {}
                        ip_id = pip.get("id") if isinstance(pip, dict) else None
                        cf = row.get("custom_fields") or {}
                        we_own = _owns(cf)
                        # source_of_truth=="netbox" → NetBox is the source of truth
                        # for this device: only-add-missing. The device already
                        # exists, so do NOT overwrite its IP's mac_address/dns_name
                        # or rename it — only refresh last_seen (a staleness signal,
                        # not a truth field). "external" (the discovery feed is the
                        # source of truth) overwrites as before.
                        if source_of_truth == "netbox":
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    self._stamp_last_seen(devobj)
                            except Exception as e:
                                logger.debug("sync_devices: last_seen refresh %s: %s",
                                              ip_str, e)
                            pushed += 1
                            continue
                        if ip_id:
                            try:
                                ipobj = self.nb.ipam.ip_addresses.get(ip_id)
                                if ipobj:
                                    if mac:
                                        merged = dict(ipobj.custom_fields or {})
                                        merged["mac_address"] = mac
                                        ipobj.custom_fields = merged
                                    if hostname and hostname.lower() != "unknown":
                                        ipobj.dns_name = hostname
                                    ipobj.save()
                                    self._stamp_last_seen(ipobj)
                            except Exception as e:
                                logger.debug("sync_devices: refresh IP %s failed: %s", ip_str, e)
                        if we_own and hostname and hostname.lower() != "unknown":
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    devobj.name = hostname
                                    devobj.save()
                                    used_names.add(hostname.strip().lower())
                                    self._stamp_last_seen(devobj)
                            except Exception as e:
                                logger.debug("sync_devices: rename %s failed: %s", ip_str, e)
                        else:
                            # Not renaming, but still mark the device seen.
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    self._stamp_last_seen(devobj)
                            except Exception as e:
                                logger.debug("sync_devices: last_seen %s: %s", ip_str, e)
                        pushed += 1
                    else:
                        # No existing device for this IP — create one we own.
                        name = (hostname if hostname and hostname.lower() != "unknown"
                                else (f"device-{mac.replace(':', '')}" if mac
                                      else f"device-{real_ip or 'unknown'}"))
                        # The (name, site, tenant) unique constraint. Two hazards:
                        # (1) the name matches a PRE-existing device (snapshot in
                        # existing_by_name); (2) INTRA-batch duplicates — many
                        # discovery records share a hostname (ks205, sonoszp,
                        # iphone…) across distinct MACs, and existing_by_name
                        # (a pre-batch snapshot) can't see names created earlier
                        # this batch, so a 2nd create with the same name 400s.
                        # used_names tracks every name we create/refresh this
                        # batch; _uniq_device_name uniquifies against both sets.
                        # Never reclaim a name held by a device we just refreshed
                        # via IP-match (refreshed_ids) — that's a different IP's
                        # real device, not a stale orphan.
                        byname = existing_by_name.get(name.lower())
                        if byname and byname["id"] not in refreshed_ids:
                            bcf = byname.get("custom_fields") or {}
                            b_own = _owns(bcf)
                            # Reclaim the name only for a stale OWNED device that
                            # is NOT being refreshed this batch (refreshed_ids) and
                            # whose name no earlier record this batch took
                            # (used_names) — i.e. our own orphan (DHCP IP-move of
                            # a device-<mac> record, or a stale owned hostname).
                            # refreshed_ids guarantees we never delete a live
                            # device we just refreshed for a different IP. An
                            # UNOWNED (human) collision, or a name already used
                            # this batch, → uniquify instead of clobbering.
                            if b_own and name.lower() not in used_names:
                                try:
                                    old = self.nb.dcim.devices.get(byname["id"])
                                    if old:
                                        old.delete()
                                        deleted += 1
                                except Exception as e:
                                    errors += 1
                                    if first_err is None:
                                        first_err = f"delete-stale {name}: {e}"
                                    logger.debug("sync_devices: delete stale by-name %s failed: %s", name, e)
                            else:
                                orig = name
                                name = self._uniq_device_name(name, mac, real_ip,
                                                              existing_by_name, used_names)
                                logger.debug("sync_devices: name %s taken; creating as %s", orig, name)
                        elif name.lower() in used_names:
                            # Intra-batch duplicate hostname: no pre-existing
                            # device by that name, but an earlier record this
                            # batch already created it.
                            orig = name
                            name = self._uniq_device_name(name, mac, real_ip,
                                                          existing_by_name, used_names)
                            logger.debug("sync_devices: hostname %s already used this batch; "
                                         "creating as %s", orig, name)
                        used_names.add(name.lower())
                        create_kwargs: Dict[str, Any] = {"name": name, "status": "active"}
                        if role:
                            create_kwargs["role"] = role.id
                        if dtype:
                            create_kwargs["device_type"] = dtype.id
                        if site:
                            create_kwargs["site"] = site.id
                        if tenant:
                            create_kwargs["tenant"] = tenant.id
                        devobj = self.nb.dcim.devices.create(**create_kwargs)
                        # Ownership tag + last_seen stamp (best-effort; missing
                        # custom field => no-op). last_seen clocks the staleness
                        # sweep from the moment NetBox first saw this device.
                        try:
                            merged = dict(devobj.custom_fields or {})
                            merged["discovered_from"] = source_tag
                            merged["last_seen"] = datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ")
                            devobj.custom_fields = merged
                            devobj.save()
                        except Exception as e:
                            logger.debug("sync_devices: discovered_from tag skipped: %s", e)
                        self._journal("dcim.device", devobj.id, "firewall-discovery",
                                      note=f"device {name}")
                        # mgmt interface + IP (mac on the IP) + primary_ip4.
                        if real_ip:
                            # Use the top-level dcim.interfaces endpoint with
                            # device=<id> rather than devobj.interfaces.create
                            # — the nested accessor isn't supported on every
                            # pynetbox version (AttributeError "object has no
                            # attribute 'interfaces'"), and it's what unblocked
                            # the create branch once the name-collision 400s were
                            # resolved.
                            iface = self.nb.dcim.interfaces.create(
                                device=devobj.id, name="mgmt", type="other")
                            mask = self._mask_for_ip(real_ip)
                            ip_kwargs: Dict[str, Any] = {
                                "address": f"{real_ip}/{mask}",
                                "assigned_object_type": "dcim.interface",
                                "assigned_object_id": iface.id,
                            }
                            if tenant:
                                ip_kwargs["tenant"] = tenant.id
                            if hostname and hostname.lower() != "unknown":
                                ip_kwargs["dns_name"] = hostname
                            # Reuse an existing global IP record (NetBox enforces
                            # global uniqueness — creating a duplicate 400s with
                            # "Duplicate IP address found in global table", which
                            # was failing ~every record because the IPAM already
                            # held most of these addresses) and reassign it to
                            # this mgmt interface instead of creating a new one.
                            ipobj = self._reuse_or_create_ip(
                                f"{real_ip}/{mask}", ip_kwargs, real_ip, iface.id,
                                tenant=tenant, hostname=hostname, mac=mac,
                                source="firewall-discovery")
                            devobj.primary_ip4 = ipobj.id
                            devobj.save()
                            self._journal("ipam.ipaddress", ipobj.id,
                                          "firewall-discovery",
                                          note=f"IP {real_ip}/{mask} → {name}")
                            self._stamp_last_seen(ipobj)
                        pushed += 1
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"upsert {ip_str}: {e}"
                    logger.debug("sync_devices: upsert %s failed: %s", ip_str, e)

            msg = (f"{pushed} device(s) upserted, {deleted} deleted, "
                   f"{skipped} skipped, {errors} errors")
            if errors and first_err:
                # Surface the first failure in the returned message (the hub
                # status UI shows it) + a WARNING, so the cause is visible
                # without digging through DEBUG logs.
                msg += f" — first error: {first_err}"
                logger.warning("sync_devices tenant=%s: %s", tenant_slug or "<global>", msg)
            else:
                logger.info("sync_devices tenant=%s: %s", tenant_slug or "<global>", msg)
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "devices_total": len(incoming), "message": msg}
        except Exception as e:
            logger.error("sync_devices failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "devices_total": len(devices or [])}

    def sync_access_tracker(self, sessions: list, tenant_slug: str = "",
                            defaults: Optional[Dict[str, Any]] = None,
                            source_of_truth: str = "netbox") -> Dict[str, Any]:
        """Pull ClearPass Access Tracker / session data INTO NetBox (NAC→IPAM
        reverse sync; the bidirectional counterpart to ``EndpointSyncMixin``).

        Source = CPPM ``/api/session`` (relayed by the hub realtime loop). Each
        incoming session ``{mac, ip, nas_ip, nas_port, nas_name, username,
        start_time}`` is matched **MAC-first** against the tenant's existing
        devices (keyed by the device's ``custom_fields.mac_address``). NetBox
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
                            mask = self._mask_for_ip(nas_ip)
                            ipo = self.nb.ipam.ip_addresses.create(
                                address=f"{nas_ip}/{mask}",
                                assigned_object_type="dcim.interface",
                                assigned_object_id=miface.id)
                            if tenant:
                                ipo.tenant = tenant.id
                                ipo.save()
                            sw.primary_ip4 = ipo.id
                            sw.save()
                        except Exception as e:
                            logger.debug("sync_access_tracker: switch mgmt/IP %s skipped: %s", nas_ip, e)
                        row = {"id": sw.id, "name": sw.name,
                               "primary_ip4": {"address": f"{nas_ip}/{self._mask_for_ip(nas_ip)}"}}
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
                    if nic_obj and getattr(nic_obj, "connected_endpoint", None):
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
                    if not mac:
                        skipped += 1
                        continue

                    row = existing_by_mac.get(mac)
                    if row:
                        # Already in NetBox → only-add-missing: skip the create.
                        # Best-effort refresh of topology/last_seen ONLY on a
                        # device we own (never touch another source's record).
                        cf = row.get("custom_fields") or {}
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
                    if dtype:
                        create_kwargs["device_type"] = dtype.id
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
                        mask = self._mask_for_ip(ip)
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

    # ── staleness sweep (cluster-wide age-out of sync-owned objects) ──────────

    # Objects with NO ``last_seen`` custom field are NEVER swept — that protects
    # hand-managed inventory (a human-created device/VM the syncs never touched
    # has no last_seen, so the sweep can't age it out). Only objects the syncs
    # stamped (every detection writes last_seen) are eligible.
    @staticmethod
    def _parse_iso_cf(ts: str) -> Optional[datetime]:
        """Parse a ``last_seen``/``decommissioned_at`` CF timestamp (ISO, Z or
        offset) into an aware UTC datetime. None on unparseable/empty."""
        s = str(ts or "").strip()
        if not s:
            return None
        try:
            # Normalize a trailing Z to +00:00 for fromisoformat.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def staleness_sweep(self, stale_days: int = 7,
                        delete_days: int = 30) -> Dict[str, Any]:
        """Cluster-wide age-out of sync-owned NetBox objects.

        For every device / VM / unassigned IP that carries a ``last_seen``
        custom field (i.e. a sync touched it — hand-managed objects have none
        and are never swept):
          • not seen for ``stale_days`` (default 7) and not already offline →
            set ``status = "offline"`` + ``decommissioned_at = now`` + journal
            entry ``staleness-sweep: decommissioned: not seen since <last_seen>``.
          • offline with ``decommissioned_at`` older than ``delete_days``
            (default 30) → DELETE the object. Deleting a device/VM frees its
            assigned IPs automatically (assigned_object goes null); an
            unassigned stale IP record is deleted so the address becomes free.

        Returns ``{status, scanned, decommissioned, deleted, ip_freed,
        errors, message, per_tenant}``. Never raises — a sweep failure is
        per-object and recorded in ``errors`` so one bad row can't abort the run.
        """
        scanned = 0; decommissioned = 0; deleted = 0; ip_freed = 0; errors = 0
        first_err: Optional[str] = None
        per_tenant: Dict[str, Dict[str, int]] = {}

        def _bucket(slug: str) -> Dict[str, int]:
            key = str(slug or "").strip() or self._VM_SYNC_UNASSIGNED_KEY
            b = per_tenant.get(key)
            if b is None:
                b = {"decommissioned": 0, "deleted": 0, "errors": 0}
                per_tenant[key] = b
            return b

        def _tenant_slug(row: dict) -> str:
            t = row.get("tenant")
            if isinstance(t, dict):
                return str(t.get("slug") or "")
            return ""

        def _age_days(ts: str) -> Optional[float]:
            dt = self._parse_iso_cf(ts)
            if dt is None:
                return None
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            self._ensure_custom_fields()
            cutoff_stale = float(stale_days)
            cutoff_delete = float(delete_days)

            # ── devices (cluster-wide, no tenant scope) ──
            try:
                dev_rows = self._api_get_all("/api/dcim/devices/", {"limit": 500})
            except Exception as e:
                return {"status": "ERROR", "message": f"failed to list devices: {e}",
                        "scanned": 0, "decommissioned": 0, "deleted": 0,
                        "ip_freed": 0, "errors": 0, "per_tenant": per_tenant}
            for row in dev_rows:
                cf = row.get("custom_fields") or {}
                ls = str(cf.get("last_seen") or "").strip()
                if not ls:
                    continue  # never swept (hand-managed)
                scanned += 1
                tslug = _tenant_slug(row)
                age = _age_days(ls)
                if age is None:
                    continue
                st = row.get("status")
                status_val = str((st.get("value") if isinstance(st, dict)
                                  else st) or "")
                decomm = str(cf.get("decommissioned_at") or "").strip()
                try:
                    obj = self.nb.dcim.devices.get(row["id"])
                    if not obj:
                        continue
                    # 30-day delete: already offline + decommissioned_at aged out.
                    if status_val == "offline" and decomm:
                        dage = _age_days(decomm)
                        if dage is not None and dage >= cutoff_delete:
                            obj.delete()
                            deleted += 1
                            _bucket(tslug)["deleted"] += 1
                            self._journal("dcim.device", row["id"], "staleness-sweep",
                                           note=f"deleted: offline since {decomm}")
                            continue
                    # 7-day decommission: unseen past stale_days + not yet offline.
                    if age >= cutoff_stale and status_val != "offline":
                        try:
                            obj.status = "offline"
                            m = dict(obj.custom_fields or {})
                            m["decommissioned_at"] = now_iso
                            obj.custom_fields = m
                            obj.save()
                            decommissioned += 1
                            _bucket(tslug)["decommissioned"] += 1
                            self._journal("dcim.device", row["id"], "staleness-sweep",
                                           note=f"decommissioned: not seen since {ls}")
                        except Exception as e:
                            errors += 1
                            _bucket(tslug)["errors"] += 1
                            if first_err is None:
                                first_err = f"decomm device {row['id']}: {e}"
                except Exception as e:
                    errors += 1
                    _bucket(tslug)["errors"] += 1
                    if first_err is None:
                        first_err = f"device {row['id']}: {e}"
                    logger.debug("staleness_sweep: device %s failed: %s", row["id"], e)

            # ── VMs (cluster-wide; only those we own via proxmox_unique_id) ──
            try:
                vm_rows = self._api_get_all("/api/virtualization/virtual-machines/",
                                            {"limit": 500})
            except Exception as e:
                logger.warning("staleness_sweep: list VMs failed: %s", e)
                vm_rows = []
            for row in vm_rows:
                cf = row.get("custom_fields") or {}
                if not str(cf.get("proxmox_unique_id") or "").strip():
                    continue  # not sync-owned → never swept
                ls = str(cf.get("last_seen") or "").strip()
                if not ls:
                    continue
                scanned += 1
                tslug = _tenant_slug(row)
                age = _age_days(ls)
                if age is None:
                    continue
                status = row.get("status")
                status_val = str((status.get("value") if isinstance(status, dict)
                                  else status) or "")
                decomm = str(cf.get("decommissioned_at") or "").strip()
                try:
                    obj = self.nb.virtualization.virtual_machines.get(row["id"])
                    if not obj:
                        continue
                    if status_val == "offline" and decomm:
                        dage = _age_days(decomm)
                        if dage is not None and dage >= cutoff_delete:
                            obj.delete()
                            deleted += 1
                            _bucket(tslug)["deleted"] += 1
                            self._journal("virtualization.virtualmachine", row["id"],
                                           "staleness-sweep",
                                           note=f"deleted: offline since {decomm}")
                            continue
                    if age >= cutoff_stale and status_val != "offline":
                        try:
                            obj.status = "offline"
                            m = dict(obj.custom_fields or {})
                            m["decommissioned_at"] = now_iso
                            obj.custom_fields = m
                            obj.save()
                            decommissioned += 1
                            _bucket(tslug)["decommissioned"] += 1
                            self._journal("virtualization.virtualmachine", row["id"],
                                           "staleness-sweep",
                                           note=f"decommissioned: not seen since {ls}")
                        except Exception as e:
                            errors += 1
                            _bucket(tslug)["errors"] += 1
                            if first_err is None:
                                first_err = f"decomm VM {row['id']}: {e}"
                except Exception as e:
                    errors += 1
                    _bucket(tslug)["errors"] += 1
                    if first_err is None:
                        first_err = f"VM {row['id']}: {e}"
                    logger.debug("staleness_sweep: VM %s failed: %s", row["id"], e)

            # ── unassigned stale IPs (free the address) ──
            # IPs still assigned to a kept device are freed by that device's
            # delete above; here we only delete IPs that are already unassigned
            # (assigned_object_id null) + carry our last_seen + aged past
            # delete_days, so an orphaned IP record releases its address.
            try:
                ip_rows = self._api_get_all("/api/ipam/ip-addresses/", {"limit": 500})
            except Exception as e:
                logger.warning("staleness_sweep: list IPs failed: %s", e)
                ip_rows = []
            for row in ip_rows:
                cf = row.get("custom_fields") or {}
                ls = str(cf.get("last_seen") or "").strip()
                if not ls:
                    continue
                # assigned? (a_terminations / assigned_object_id). Skip assigned —
                # the owning device/VM sweep (or NetBox's cascade on delete)
                # handles those.
                assigned = row.get("assigned_object_id")
                if row.get("assigned_object_type") and assigned is not None:
                    continue
                scanned += 1
                age = _age_days(ls)
                if age is None or age < cutoff_delete:
                    continue
                try:
                    obj = self.nb.ipam.ip_addresses.get(row["id"])
                    if obj:
                        obj.delete()
                        ip_freed += 1
                        self._journal("ipam.ipaddress", row["id"], "staleness-sweep",
                                       note=f"freed: unassigned, last seen {ls}")
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"IP {row['id']}: {e}"
                    logger.debug("staleness_sweep: IP %s failed: %s", row["id"], e)

            msg = (f"swept {scanned} object(s): {decommissioned} decommissioned, "
                   f"{deleted} deleted, {ip_freed} IP(s) freed, {errors} errors")
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("staleness_sweep: %s", msg)
            else:
                logger.info("staleness_sweep: %s", msg)
            return {"status": "SUCCESS", "scanned": scanned,
                    "decommissioned": decommissioned, "deleted": deleted,
                    "ip_freed": ip_freed, "errors": errors, "message": msg,
                    "per_tenant": per_tenant}
        except Exception as e:
            logger.error("staleness_sweep failed: %s", e)
            return {"status": "ERROR", "message": str(e), "scanned": scanned,
                    "decommissioned": decommissioned, "deleted": deleted,
                    "ip_freed": ip_freed, "errors": errors, "per_tenant": per_tenant}

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
            rows = self._api_get_all("/api/tenancy/tenants/")
            tenants = [
                {"id": t["id"], "name": t["name"], "slug": t["slug"],
                 "description": t.get("description") or ""}
                for t in rows
            ]
            return {"status": "SUCCESS", "tenants": tenants}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_dhcp_prefixes(self) -> Dict[str, Any]:
        try:
            rows = self._api_get_all("/api/ipam/prefixes/")
            scopes = [
                {"prefix": p["prefix"], "gateway": p.get("custom_fields", {}).get("gateway"),
                 "mask": None, "id": p["id"]}
                for p in rows
            ]
            return {"status": "SUCCESS", "scopes": scopes}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

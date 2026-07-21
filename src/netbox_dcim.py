"""DCIM sites/racks/devices + health + universal search methods for NetboxEngine."""
import ipaddress
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NetboxEngine")

# Bundled Aruba/HPE/Juniper device-type catalog loaded by seed_catalog(). Lives
# next to this module so the spoke ships it without an extra install step.
_SEED_CATALOG_PATH = Path(__file__).parent / "seed_catalog.json"


class DcimMixin:
    """DCIM sites/racks/devices + health + universal search methods for NetboxEngine."""

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

    # ─── DCIM – Racks (CRUD) ───────────────────────────────────────────────────

    def add_rack(self, name: str, site_slug: str, u_height: int = 42,
                 facility_id: Optional[str] = None,
                 tenant_slug: Optional[str] = None) -> Dict[str, Any]:
        """Create a rack at a site, optionally attributed to a NetBox tenant.

        ``tenant_slug`` is resolved to a tenant id (mirrors ``allocate_prefix`` /
        ``claim_device``): if a slug is supplied but no NetBox tenant matches it,
        we refuse rather than silently create an unattributed rack — the WebUI
        stamps the current tenant on create, so an unresolvable slug means the
        tenant's NetBox slug mapping is wrong (not that the user wanted global)."""
        try:
            site = self.nb.dcim.sites.get(slug=site_slug)
            if not site:
                return {"status": "ERROR", "message": f"Site '{site_slug}' not found"}
            tenant_id = None
            if tenant_slug:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if not tenant:
                    logger.warning(
                        f"add_rack: NetBox tenant '{tenant_slug}' not found; "
                        f"refusing unattributed rack at site '{site_slug}'")
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — rack not created. Check the tenant's NetBox slug mapping."}
                tenant_id = tenant.id
            payload: Dict[str, Any] = {"name": name, "site": site.id, "u_height": u_height}
            if facility_id:
                payload["facility_id"] = facility_id
            if tenant_id is not None:
                payload["tenant"] = tenant_id
            rack = self.nb.dcim.racks.create(**payload)
            return {"status": "SUCCESS", "rack_id": rack.id, "name": name}
        except Exception as e:
            logger.error(f"add_rack failed: {e}")
            return {"status": "ERROR", "message": str(e)}

    def update_rack(self, rack_id: int, name: Optional[str] = None,
                    u_height: Optional[int] = None,
                    facility_id: Optional[str] = None,
                    tenant_slug: Optional[str] = None) -> Dict[str, Any]:
        """Edit a rack's name/u_height/facility_id (and re-attribute tenant when
        ``tenant_slug`` is supplied). A ``None`` tenant_slug leaves the existing
        tenant untouched — so an edit that doesn't send a tenant preserves it."""
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
            if tenant_slug is not None:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug) if tenant_slug else None
                if tenant_slug and not tenant:
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — rack not updated."}
                rack.tenant = tenant.id if tenant else None
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

    # ─── Catalog seed (device types + templates) ──────────────────────────────

    @staticmethod
    def _slugify(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "unknown"

    def _load_seed_catalog(self) -> Dict[str, Any]:
        """Load and cache the bundled seed_catalog.json (next to this module)."""
        cat = getattr(self, "_seed_catalog_cache", None)
        if cat is None:
            with open(_SEED_CATALOG_PATH) as f:
                cat = json.load(f)
            self._seed_catalog_cache = cat
        return cat

    def _seed_manufacturer(self, name: str, cache: Dict[str, Any]):
        """Get-or-create a manufacturer by slug. Returns (record, created)."""
        slug = self._slugify(name)
        if slug in cache:
            return cache[slug], False
        try:
            mfr = self.nb.dcim.manufacturers.get(slug=slug)
        except Exception:  # noqa: BLE001
            mfr = None
        created = False
        if not mfr:
            mfr = self.nb.dcim.manufacturers.create(name=name, slug=slug)
            created = True
        cache[slug] = mfr
        return mfr, created

    def _seed_device_type(self, t: Dict[str, Any], mfr) -> tuple:
        """Get-or-create a device type; upsert scalar fields on an existing one.
        Returns (record, created, updated)."""
        slug = t["slug"]
        try:
            dt = self.nb.dcim.device_types.get(slug=slug)
        except Exception:  # noqa: BLE001
            dt = None
        u = int(t.get("u_height", 1))
        full = bool(t.get("is_full_depth", True))
        comments = t.get("comments", "") or ""
        if dt is None:
            dt = self.nb.dcim.device_types.create(
                model=t["model"], slug=slug, manufacturer=mfr.id,
                u_height=u, is_full_depth=full, comments=comments)
            return dt, True, False
        # Existing → upsert scalars (templates reconciled separately, add-missing)
        changed = False
        if u != int(getattr(dt, "u_height", 0) or 0):
            dt.u_height = u
            changed = True
        if full != bool(getattr(dt, "is_full_depth", False)):
            dt.is_full_depth = full
            changed = True
        if comments != (getattr(dt, "comments", "") or ""):
            dt.comments = comments
            changed = True
        if changed:
            try:
                dt.save()
            except Exception as e:  # noqa: BLE001
                logger.warning("seed_catalog: %s scalar save failed: %s", slug, e)
                changed = False
        return dt, False, changed

    def _seed_templates(self, dt, t: Dict[str, Any]) -> int:
        """Add-missing interface/console/power templates for a device type.
        Non-destructive: never deletes or re-types existing templates (so
        hand-added ports and already-instantiated devices are safe). Returns
        the number of templates created this run."""
        added = 0
        try:
            existing_ifaces = {r.name for r in
                               self.nb.dcim.interface_templates.filter(device_type=dt.id)}
        except Exception:  # noqa: BLE001
            existing_ifaces = set()
        try:
            existing_console = {r.name for r in
                                self.nb.dcim.console_port_templates.filter(device_type=dt.id)}
        except Exception:  # noqa: BLE001
            existing_console = set()
        try:
            existing_power = {r.name for r in
                              self.nb.dcim.power_port_templates.filter(device_type=dt.id)}
        except Exception:  # noqa: BLE001
            existing_power = set()

        def _add_iface(name, itype, mgmt_only=False):
            nonlocal added
            if name in existing_ifaces:
                return
            self.nb.dcim.interface_templates.create(
                device_type=dt.id, name=name, type=itype, mgmt_only=mgmt_only)
            existing_ifaces.add(name)
            added += 1

        for p in t.get("ports", []) or []:
            prefix = p.get("prefix", "")
            itype = p.get("type", "1000base-t")
            start = int(p.get("start", 1))
            for i in range(int(p.get("count", 0))):
                _add_iface(f"{prefix}{start + i}", itype)
        mg = t.get("mgmt")
        if mg:
            _add_iface(mg.get("name", "mgmt"), mg.get("type", "1000base-t"), mgmt_only=True)
        con = t.get("console")
        if con:
            cn = con.get("name", "Console")
            if cn not in existing_console:
                self.nb.dcim.console_port_templates.create(
                    device_type=dt.id, name=cn, type=con.get("type", "rj-45"))
                existing_console.add(cn)
                added += 1
        for pw in t.get("power", []) or []:
            pn = pw.get("name", "PSU1")
            if pn not in existing_power:
                self.nb.dcim.power_port_templates.create(
                    device_type=dt.id, name=pn, type=pw.get("type", "iec-60320-c14"))
                existing_power.add(pn)
                added += 1
        return added

    def seed_catalog(self) -> Dict[str, Any]:
        """Seed NetBox with the bundled Aruba/HPE/Juniper device-type catalog
        (manufacturers + device types + interface/console/power templates).

        Idempotent UPSERT — re-runs never error on an existing type:
        - Manufacturers: get-or-create by slug.
        - Device types: create if missing; else upsert u_height/is_full_depth/
          comments (count ``device_types_updated``).
        - Templates: ADD-MISSING only — fetch the type's existing
          interface/console/power template names once, then create each catalog
          template whose name isn't already present. Never deletes or re-types
          existing templates, so hand-added ports and instantiated devices are
          safe. ``templates_added`` counts this run's creates.

        Per-model try/except so one bad model doesn't abort the rest.
        Returns ``{status, manufacturers_created, device_types_created,
        device_types_updated, templates_added, errors}``."""
        try:
            catalog = self._load_seed_catalog()
            types = catalog.get("device_types") or []
            mfrs_created = types_created = types_updated = tpl_added = 0
            errors: List[str] = []
            mfr_cache: Dict[str, Any] = {}
            for t in types:
                slug = t.get("slug", "?")
                try:
                    mfr, mfr_created = self._seed_manufacturer(t.get("manufacturer", ""), mfr_cache)
                    if mfr is None:
                        errors.append(f"{slug}: manufacturer unresolved")
                        continue
                    if mfr_created:
                        mfrs_created += 1
                    dt, created, updated = self._seed_device_type(t, mfr)
                    if created:
                        types_created += 1
                    elif updated:
                        types_updated += 1
                    n = self._seed_templates(dt, t)
                    tpl_added += n
                    action = "created" if created else ("updated" if updated else "ok")
                    logger.info("seed_catalog: %s %s (+%d templates)", slug, action, n)
                except Exception as e:  # noqa: BLE001
                    logger.error("seed_catalog: %s failed: %s", slug, e)
                    errors.append(f"{slug}: {e}")
            return {"status": "SUCCESS",
                    "manufacturers_created": mfrs_created,
                    "device_types_created": types_created,
                    "device_types_updated": types_updated,
                    "templates_added": tpl_added,
                    "errors": errors}
        except Exception as e:  # noqa: BLE001
            logger.exception("seed_catalog failed")
            return {"status": "ERROR", "message": str(e)}

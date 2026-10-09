"""Tenancy + DHCP-prefix read methods for NetboxEngine."""
import logging
from typing import Any, Dict

logger = logging.getLogger("NetboxEngine")


class TenancyMixin:
    """Tenancy + DHCP-prefix read methods for NetboxEngine."""

    # ─── VRF resolution ────────────────────────────────────────────────────────
    #
    # Tenants have overlapping address space, so each tenant is bound to a NetBox
    # VRF (``vrf.tenant``). VRFs are managed in NetBox itself; we only look the
    # tenant's VRF up so prefixes/IPs we create land in it (and lookups of
    # existing records are scoped to it). A tenant with no VRF keeps the old
    # global-table behaviour (vrf_id None).

    def _vrf_id_for_tenant(self, tenant: Any):
        """Return the id of the VRF owned by ``tenant`` (a pynetbox record, a
        dict, or a bare id), or None when the tenant has no VRF / no tenant."""
        if tenant is None or tenant == "":
            return None
        tid = tenant if isinstance(tenant, int) else (
            tenant.get("id") if isinstance(tenant, dict) else getattr(tenant, "id", None))
        if tid is None:
            return None

        def _fetch():
            rows = self._api_get("/api/ipam/vrfs/",
                                 {"tenant_id": tid, "limit": 50}).get("results", [])
            if not rows:
                return None
            rows = sorted(rows, key=lambda r: r.get("id", 0))
            if len(rows) > 1:
                logger.warning("tenant id=%s owns %d VRFs (%s); using '%s'", tid, len(rows),
                               ", ".join(str(r.get("name")) for r in rows), rows[0].get("name"))
            return rows[0]["id"]

        return self._cached_ref("vrf_tenant", str(tid), _fetch)

    def _vrf_id_for_tenant_slug(self, tenant_slug: str):
        """Like ``_vrf_id_for_tenant`` but resolves a tenant slug first."""
        if not tenant_slug:
            return None
        try:
            t = self.nb.tenancy.tenants.get(slug=tenant_slug)
        except Exception as e:
            logger.debug("vrf lookup: tenant %s resolve failed: %s", tenant_slug, e)
            return None
        return self._vrf_id_for_tenant(t)

    def _get_prefix_obj(self, prefix: str, vrf_id=None):
        """Fetch one prefix record. With a ``vrf_id``, prefer the prefix in that
        VRF, then the global-table one; without it, behave as before. Needed
        because overlapping prefixes make a bare ``prefixes.get(prefix=...)``
        raise on multiple matches."""
        if vrf_id is not None:
            obj = self.nb.ipam.prefixes.get(prefix=prefix, vrf_id=vrf_id)
            if obj:
                return obj
            return self.nb.ipam.prefixes.get(prefix=prefix, vrf_id="null")
        return self.nb.ipam.prefixes.get(prefix=prefix)

    # ─── Tenancy ───────────────────────────────────────────────────────────────

    def get_tenants(self) -> Dict[str, Any]:
        """Retrieve all tenants configured in NetBox, each tagged with its group.

        ``group_slug``/``group_name`` are "" for an ungrouped tenant. The hub
        stores them so a tenant record knows its parent group without a second
        round trip."""
        try:
            rows = self._api_get_all("/api/tenancy/tenants/")
            tenants = []
            for t in rows:
                g = t.get("group")
                g = g if isinstance(g, dict) else {}
                tenants.append({
                    "id": t["id"], "name": t["name"], "slug": t["slug"],
                    "description": t.get("description") or "",
                    "group_slug": g.get("slug") or "",
                    "group_name": g.get("name") or "",
                })
            return {"status": "SUCCESS", "tenants": tenants}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_tenant_groups(self) -> Dict[str, Any]:
        """List NetBox tenant groups with their TRANSITIVE tenant membership.

        ``tenant_slugs`` rolls UPWARD: a group carries its own tenants plus
        every tenant of a descendant group, so selecting a parent group shows
        the union of the whole subtree. That matches NetBox's own
        ``?tenant_group=<slug>`` filter, which is tree-aware — the hub sends
        that filter rather than a slug list, and uses ``tenant_slugs`` only to
        authorize writes. The ancestor walk is cycle-guarded: NetBox cannot
        express a parent loop, but a hand-edited fixture can."""
        try:
            groups = self._api_get_all("/api/tenancy/tenant-groups/")
            tenants = self._api_get_all("/api/tenancy/tenants/")

            direct: Dict[Any, list] = {}
            for t in tenants:
                g = t.get("group")
                if isinstance(g, dict) and g.get("id") is not None:
                    direct.setdefault(g["id"], []).append(t["slug"])

            parent_of: Dict[Any, Any] = {}
            for g in groups:
                p = g.get("parent")
                parent_of[g["id"]] = p["id"] if isinstance(p, dict) else None

            # Each group donates its OWN tenants to itself and every ancestor.
            rollup: Dict[Any, set] = {}
            for g in groups:
                donated = direct.get(g["id"], [])
                seen = set()
                node = g["id"]
                while node is not None and node not in seen:
                    seen.add(node)
                    rollup.setdefault(node, set()).update(donated)
                    node = parent_of.get(node)

            out = []
            for g in groups:
                members = sorted(rollup.get(g["id"], set()))
                p = g.get("parent")
                out.append({
                    "id": g["id"], "name": g["name"], "slug": g["slug"],
                    "description": g.get("description") or "",
                    "parent_slug": p["slug"] if isinstance(p, dict) else "",
                    "tenant_slugs": members,
                    "tenant_count": len(members),
                })
            out.sort(key=lambda x: x["name"])
            return {"status": "SUCCESS", "groups": out}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}

    def get_dhcp_prefixes(self) -> Dict[str, Any]:
        """Read every NetBox prefix as a KEA DHCP scope source.

        Returns ``{status, scopes}`` where each scope is
        ``{prefix, gateway, mask, id}`` — ``gateway`` comes from the prefix's
        ``gateway`` custom field (None when unset). Called by the spoke's
        ``_kea_sync_loop`` (every 300s); each scope is POSTed to the KEA
        Control Agent as ``subnet4-add`` with a derived pool range and the
        gateway as the ``routers`` option. Paginated via ``_api_get_all`` so a
        large prefix table doesn't truncate."""
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

    # ─── Tenant migration (Migrate Data to new Tenant) ─────────────────────────

    # Every NetBox object type that carries a ``tenant`` FK. A tenant rename in
    # the NetBox UI orphans data (LM keys tenants by name, so the next sync
    # re-creates the old name empty); this migration reassigns all of one
    # tenant's objects to another, then optionally deletes the now-empty source.
    # Guarded per-endpoint so a model absent in a given NetBox version/plugin set
    # is skipped, not fatal.
    _TENANT_OWNING = (
        ("dcim", "devices"), ("dcim", "racks"), ("dcim", "sites"),
        ("virtualization", "virtual_machines"), ("virtualization", "clusters"),
        ("ipam", "ip_addresses"), ("ipam", "prefixes"), ("ipam", "ip_ranges"),
        ("ipam", "vlans"), ("ipam", "vrfs"), ("ipam", "aggregates"),
        ("circuits", "circuits"),
        ("wireless", "wireless_lans"), ("wireless", "wireless_links"),
    )

    def _resolve_tenant(self, ref):
        """Resolve a tenant reference (numeric id, slug, or name) to a pynetbox
        record, or None. Tries id → slug → name so callers can pass whatever the
        UI has."""
        if ref in (None, ""):
            return None
        try:
            return self.nb.tenancy.tenants.get(int(ref))
        except (ValueError, TypeError):
            pass
        return (self.nb.tenancy.tenants.get(slug=ref)
                or self.nb.tenancy.tenants.get(name=ref))

    def migrate_tenant(self, source, target, delete_source=True,
                       create_target=False) -> Dict[str, Any]:
        """Reassign every object owned by tenant ``source`` to tenant ``target``,
        then (optionally) delete the source tenant. ``source``/``target`` may be
        a tenant id, slug, or name. Returns a per-endpoint moved-count summary.

        Safe-guards: refuses when source == target; requires both tenants to
        exist (unless ``create_target`` mints a missing target); never deletes
        the source until every reassignment endpoint has been attempted; a
        per-endpoint failure is recorded but does NOT abort the rest, and the
        source is kept whenever any error occurred (so a partial migrate is
        reported honestly rather than half-applied with the source gone)."""
        try:
            src = self._resolve_tenant(source)
            if src is None:
                return {"status": "ERROR", "message": f"source tenant '{source}' not found"}
            tgt = self._resolve_tenant(target)
            if tgt is None:
                if not create_target:
                    return {"status": "ERROR", "message": f"target tenant '{target}' not found"}
                slug = str(target).strip().lower().replace(" ", "-")
                tgt = self.nb.tenancy.tenants.create(name=str(target), slug=slug)
            if src.id == tgt.id:
                return {"status": "ERROR", "message": "source and target are the same tenant"}

            moved: Dict[str, int] = {}
            errors: Dict[str, str] = {}
            total = 0
            for app, model in self._TENANT_OWNING:
                label = f"{app}.{model}"
                try:
                    endpoint = getattr(getattr(self.nb, app), model)
                except AttributeError:
                    continue  # model not present in this NetBox build
                try:
                    recs = list(endpoint.filter(tenant_id=src.id))
                except Exception as e:  # noqa: BLE001 - some models reject tenant_id
                    logger.debug("migrate_tenant: filter %s failed: %s", label, e)
                    continue
                cnt = 0
                for rec in recs:
                    try:
                        rec.tenant = tgt.id
                        rec.save()
                        cnt += 1
                    except Exception as e:  # noqa: BLE001 - one object must not abort
                        errors[f"{label}#{getattr(rec, 'id', '?')}"] = str(e)
                if cnt:
                    moved[label] = cnt
                    total += cnt

            deleted = False
            if delete_source and not errors:
                try:
                    src.delete()
                    deleted = True
                except Exception as e:  # noqa: BLE001
                    errors["delete_source"] = str(e)

            status = "SUCCESS" if not errors else "PARTIAL"
            return {
                "status": status,
                "source": {"id": src.id, "name": src.name},
                "target": {"id": tgt.id, "name": tgt.name},
                "moved": moved, "total": total,
                "source_deleted": deleted,
                "errors": errors,
                "message": (f"migrated {total} object(s) to '{tgt.name}'"
                            + (f", deleted '{src.name}'" if deleted else "")
                            + (f" — {len(errors)} error(s); source kept" if errors else "")),
            }
        except Exception as e:  # noqa: BLE001
            return {"status": "ERROR", "message": str(e)}

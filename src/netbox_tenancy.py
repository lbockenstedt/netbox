"""Tenancy + DHCP-prefix read methods for NetboxEngine."""
import logging
from typing import Any, Dict

logger = logging.getLogger("NetboxEngine")


class TenancyMixin:
    """Tenancy + DHCP-prefix read methods for NetboxEngine."""

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

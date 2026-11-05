"""Tenancy + DHCP-prefix read methods for NetboxEngine."""
# ipaddress decides which tenant prefix contains a given IP.
import ipaddress
# Standard-library logging; shares the "NetboxEngine" logger with the engine.
import logging
# Type hints.
from typing import Any, Dict

# Same logger name as the engine so all NetBox messages appear together.
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
        # No tenant: global table.
        if tenant is None or tenant == "":
            return None
        # Accept a bare id, a dict or a pynetbox record and pull out the tenant id.
        tid = tenant if isinstance(tenant, int) else (
            tenant.get("id") if isinstance(tenant, dict) else getattr(tenant, "id", None))
        # Could not find an id: treat as no tenant.
        if tid is None:
            return None

        def _fetch():
            """Look up the tenant's VRF in NetBox (called on a cache miss)."""
            # VRFs whose tenant is this tenant.
            rows = self._api_get("/api/ipam/vrfs/",
                                 {"tenant_id": tid, "limit": 50}).get("results", [])
            # The tenant owns no VRF.
            if not rows:
                return None
            # Several VRFs: pick the lowest id (oldest) deterministically and warn.
            rows = sorted(rows, key=lambda r: r.get("id", 0))
            if len(rows) > 1:
                logger.warning("tenant id=%s owns %d VRFs (%s); using '%s'", tid, len(rows),
                               ", ".join(str(r.get("name")) for r in rows), rows[0].get("name"))
            return rows[0]["id"]

        # Cache the answer so a fleet-wide sync does not repeat the lookup per device.
        return self._cached_ref("vrf_tenant", str(tid), _fetch)

    @staticmethod
    def _tenant_id(tenant: Any):
        """Tenant id from a bare id, dict or pynetbox record, or None."""
        if tenant is None or tenant == "":
            return None
        return tenant if isinstance(tenant, int) else (
            tenant.get("id") if isinstance(tenant, dict) else getattr(tenant, "id", None))

    def _tenant_prefix_vrfs(self, tid):
        """``[(prefix, vrf_id|None)]`` for the tenant's prefixes (TTL-cached)."""
        def _fetch():
            """Load every prefix of the tenant with its VRF id (cache miss)."""
            rows = self._api_get_all("/api/ipam/prefixes/", {"tenant_id": tid, "limit": 500})
            return [(r["prefix"], (r.get("vrf") or {}).get("id")) for r in rows or []]
        # Cached per tenant; an empty list when the fetch failed.
        return self._cached_ref("tenant_prefixes", str(tid), _fetch) or []

    def _vrf_id_for_ip(self, tenant: Any, ip: str):
        """The VRF an IP of ``tenant`` belongs in: the VRF of the tenant's most
        specific prefix containing it (None when that prefix is in the global
        table). With no containing prefix, the VRF the tenant owns. A tenant
        can have prefixes in a VRF it does not own (a shared VRF), so the
        prefix, not VRF ownership, decides."""
        # No tenant: global table.
        tid = self._tenant_id(tenant)
        if tid is None:
            return None

        # Find the most specific (longest) tenant prefix that contains the address.
        try:
            addr = ipaddress.ip_address(str(ip).split("/")[0])
            best = None
            for prefix, vrf in self._tenant_prefix_vrfs(tid):
                net = ipaddress.ip_network(prefix, strict=False)
                if addr in net and (best is None or net.prefixlen > best[0]):
                    best = (net.prefixlen, vrf)
        except Exception as e:
            # Bad IP or prefix data: fall back to the tenant's own VRF.
            logger.debug("vrf for ip %s (tenant id=%s) failed: %s", ip, tid, e)
            best = None
        # Prefix VRF when a prefix matched, else the VRF the tenant owns.
        return best[1] if best else self._vrf_id_for_tenant(tenant)

    def _move_tenant_ips_to_vrf(self, tenant: Any) -> Dict[str, Any]:
        """Move ``tenant``'s IP addresses still in the global table into the VRF
        they belong in (``_vrf_id_for_ip``). IPs created before the VRFs
        existed stayed global, and the syncs only update a matched IP in place,
        so nothing moved them. An address the VRF already holds is left alone
        and reported (moving it would make a duplicate). Best-effort; never
        raises."""
        # Running summary returned to the caller.
        result: Dict[str, Any] = {"moved": 0, "conflicts": []}
        # No tenant: nothing to move.
        tid = self._tenant_id(tenant)
        if tid is None:
            return result
        # Tenant has no VRF at all (owned or via prefixes): nothing to move.
        if self._vrf_id_for_tenant(tenant) is None and not any(
                vrf for _, vrf in self._tenant_prefix_vrfs(tid)):
            return result
        # Any failure is logged and the partial result returned.
        try:
            # The tenant's IPs that are still in the global table (vrf null).
            stale = self._api_get_all("/api/ipam/ip-addresses/",
                                      {"tenant_id": tid, "vrf_id": "null", "limit": 500})
            # Addresses already in each target VRF (loaded once per VRF), and pending moves.
            in_vrf: Dict[Any, set] = {}
            moves = []
            # Decide where each global IP belongs.
            for r in stale or []:
                bare = r["address"].split("/")[0]
                vrf_id = self._vrf_id_for_ip(tenant, bare)
                # Belongs in the global table: leave it.
                if vrf_id is None:
                    continue
                # Load the target VRF's addresses the first time we need them.
                if vrf_id not in in_vrf:
                    in_vrf[vrf_id] = {x["address"].split("/")[0] for x in self._api_get_all(
                        "/api/ipam/ip-addresses/", {"vrf_id": vrf_id, "limit": 500})}
                # Already present in that VRF: moving would create a duplicate, so report it.
                if bare in in_vrf[vrf_id]:
                    result["conflicts"].append(r["address"])
                else:
                    # Queue the move.
                    moves.append({"id": r["id"], "vrf": vrf_id})
            # Apply the moves in bulk, 100 per request.
            for i in range(0, len(moves), 100):
                self.nb.ipam.ip_addresses.update(moves[i:i + 100])
                result["moved"] += len(moves[i:i + 100])
        except Exception as e:
            # Log and return whatever was moved before the failure.
            logger.warning("[sync-error] move tenant id=%s global IPs into VRFs failed: %s", tid, e)
        # Log a summary only when something happened.
        if result["moved"] or result["conflicts"]:
            logger.info("tenant id=%s: moved %d global IPs into VRFs; %d already in the "
                        "VRF left global: %s", tid, result["moved"],
                        len(result["conflicts"]), ", ".join(result["conflicts"][:20]))
        return result

    def _vrf_id_for_tenant_slug(self, tenant_slug: str):
        """Like ``_vrf_id_for_tenant`` but resolves a tenant slug first."""
        # No slug: no VRF.
        if not tenant_slug:
            return None
        # Resolve the slug to a tenant record.
        try:
            t = self.nb.tenancy.tenants.get(slug=tenant_slug)
        except Exception as e:
            # Lookup failed: treat as no VRF.
            logger.debug("vrf lookup: tenant %s resolve failed: %s", tenant_slug, e)
            return None
        # Reuse the record-based resolver.
        return self._vrf_id_for_tenant(t)

    def _get_prefix_obj(self, prefix: str, vrf_id=None):
        """Fetch one prefix record. With a ``vrf_id``, prefer the prefix in that
        VRF, then the global-table one; without it, behave as before. Needed
        because overlapping prefixes make a bare ``prefixes.get(prefix=...)``
        raise on multiple matches."""
        # VRF given: try that VRF first, then fall back to the global table.
        if vrf_id is not None:
            obj = self.nb.ipam.prefixes.get(prefix=prefix, vrf_id=vrf_id)
            if obj:
                return obj
            return self.nb.ipam.prefixes.get(prefix=prefix, vrf_id="null")
        # No VRF: plain lookup (may raise if the prefix exists in several VRFs).
        return self.nb.ipam.prefixes.get(prefix=prefix)

    # ─── Tenancy ───────────────────────────────────────────────────────────────

    def get_tenants(self) -> Dict[str, Any]:
        """Retrieve all tenants configured in NetBox, each tagged with its group.

        ``group_slug``/``group_name`` are "" for an ungrouped tenant. The hub
        stores them so a tenant record knows its parent group without a second
        round trip."""
        # Any API failure becomes an ERROR result.
        try:
            # All tenants (all pages).
            rows = self._api_get_all("/api/tenancy/tenants/")
            tenants = []
            # Shape each tenant for the Hub, flattening its optional group reference.
            for t in rows:
                g = t.get("group")
                g = g if isinstance(g, dict) else {}
                tenants.append({
                    "id": t["id"], "name": t["name"], "slug": t["slug"],
                    "description": t.get("description") or "",
                    "group_slug": g.get("slug") or "",
                    "group_name": g.get("name") or "",
                })
            # Every tenant shaped.
            return {"status": "SUCCESS", "tenants": tenants}
        except Exception as e:
            # Report the failure to the caller.
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
        # Any API failure becomes an ERROR result.
        try:
            # All groups and all tenants (all pages).
            groups = self._api_get_all("/api/tenancy/tenant-groups/")
            tenants = self._api_get_all("/api/tenancy/tenants/")

            # Group id -> slugs of the tenants directly in that group.
            direct: Dict[Any, list] = {}
            for t in tenants:
                g = t.get("group")
                if isinstance(g, dict) and g.get("id") is not None:
                    direct.setdefault(g["id"], []).append(t["slug"])

            # Group id -> parent group id (None for a top-level group).
            parent_of: Dict[Any, Any] = {}
            for g in groups:
                p = g.get("parent")
                parent_of[g["id"]] = p["id"] if isinstance(p, dict) else None

            # Each group donates its OWN tenants to itself and every ancestor.
            rollup: Dict[Any, set] = {}
            # Walk from each group up through its ancestors, adding its tenants to each;
            # 'seen' stops the walk if the parent chain loops.
            for g in groups:
                donated = direct.get(g["id"], [])
                seen = set()
                node = g["id"]
                while node is not None and node not in seen:
                    seen.add(node)
                    rollup.setdefault(node, set()).update(donated)
                    node = parent_of.get(node)

            # Build the output row for each group.
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
            # Alphabetical by group name.
            out.sort(key=lambda x: x["name"])
            return {"status": "SUCCESS", "groups": out}
        except Exception as e:
            # Report the failure to the caller.
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
        # Any API failure becomes an ERROR result.
        try:
            # All prefixes (all pages).
            rows = self._api_get_all("/api/ipam/prefixes/")
            # One scope per prefix; mask is left for the spoke to derive from the prefix.
            scopes = [
                {"prefix": p["prefix"], "gateway": p.get("custom_fields", {}).get("gateway"),
                 "mask": None, "id": p["id"]}
                for p in rows
            ]
            # Every prefix as a scope.
            return {"status": "SUCCESS", "scopes": scopes}
        except Exception as e:
            # Report the failure to the caller.
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
        # Nothing given.
        if ref in (None, ""):
            return None
        # Numeric id first.
        try:
            return self.nb.tenancy.tenants.get(int(ref))
        except (ValueError, TypeError):
            # Not a number: try slug, then name.
            pass
        # Look up by slug, then by name.
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
        # Any failure becomes an ERROR result.
        try:
            # Resolve the source tenant; it must exist.
            src = self._resolve_tenant(source)
            if src is None:
                return {"status": "ERROR", "message": f"source tenant '{source}' not found"}
            # Resolve the target tenant; create it only when asked to.
            tgt = self._resolve_tenant(target)
            if tgt is None:
                # Target missing and creation not allowed.
                if not create_target:
                    return {"status": "ERROR", "message": f"target tenant '{target}' not found"}
                # Create the target with a slug derived from its name.
                slug = str(target).strip().lower().replace(" ", "-")
                tgt = self.nb.tenancy.tenants.create(name=str(target), slug=slug)
            # Moving a tenant onto itself is meaningless.
            if src.id == tgt.id:
                return {"status": "ERROR", "message": "source and target are the same tenant"}

            # Per-type moved counts, per-object errors and a grand total.
            moved: Dict[str, int] = {}
            errors: Dict[str, str] = {}
            total = 0
            # Walk every tenant-owning object type.
            for app, model in self._TENANT_OWNING:
                label = f"{app}.{model}"
                # Skip object types this NetBox version does not have.
                try:
                    endpoint = getattr(getattr(self.nb, app), model)
                except AttributeError:
                    continue  # model not present in this NetBox build
                # List the source tenant's objects of this type; skip types that cannot filter.
                try:
                    recs = list(endpoint.filter(tenant_id=src.id))
                except Exception as e:  # noqa: BLE001 - some models reject tenant_id
                    logger.debug("migrate_tenant: filter %s failed: %s", label, e)
                    continue
                # Reassign each object; one failure is recorded and does not stop the rest.
                cnt = 0
                for rec in recs:
                    try:
                        rec.tenant = tgt.id
                        rec.save()
                        cnt += 1
                    except Exception as e:  # noqa: BLE001 - one object must not abort
                        # Record the failed object and continue.
                        errors[f"{label}#{getattr(rec, 'id', '?')}"] = str(e)
                # Record non-zero counts for the summary.
                if cnt:
                    moved[label] = cnt
                    total += cnt

            # Delete the source only when asked and when every reassignment succeeded.
            deleted = False
            if delete_source and not errors:
                try:
                    src.delete()
                    deleted = True
                except Exception as e:  # noqa: BLE001
                    # Deletion failed: keep going and report it.
                    errors["delete_source"] = str(e)

            # PARTIAL when anything failed (the source is then kept).
            status = "SUCCESS" if not errors else "PARTIAL"
            # Summary for the UI, including a readable message.
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
            # Unexpected failure: report it.
            return {"status": "ERROR", "message": str(e)}

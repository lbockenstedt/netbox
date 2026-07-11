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

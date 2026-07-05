"""Tenancy + DHCP-prefix read methods for NetboxEngine."""
import ipaddress
import re
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

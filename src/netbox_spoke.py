import logging
import asyncio
import functools
import httpx
import os
from typing import Dict, Any
try:
    from base_spoke import BaseSpoke
except ImportError:
    from core.src.base_spoke import BaseSpoke
from netbox_engine import NetboxEngine

logger = logging.getLogger("NetboxSpoke")

class NetboxSpoke(BaseSpoke):
    """
    NetBox spoke: DCIM (rack/device) and IPAM (prefix/IP) management + KEA DHCP sync.
    """
    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        super().__init__(spoke_id, config)
        self.engine = NetboxEngine(
            url=config.get("netbox_url", os.getenv("NETBOX_URL", "http://localhost:8000")),
            token=config.get("api_token", os.getenv("NETBOX_API_TOKEN", "")),
        )
        self.kea_url = config.get("kea_ctrl_url", os.getenv("KEA_CTRL_URL", "http://localhost:8000"))
        self._sync_task = None
        # Self-heal the custom fields the syncs depend on (idempotent, best-effort;
        # a restricted token never breaks the spoke — failures are DEBUG-logged).
        self.engine._ensure_custom_fields()

    def _persist_env(self, key: str, value: str):
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        env_path = os.path.abspath(env_path)
        try:
            lines = open(env_path).readlines() if os.path.exists(env_path) else []
            found = False
            new_lines = []
            for line in lines:
                if line.startswith(f"{key}="):
                    new_lines.append(f"{key}={value}\n")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"{key}={value}\n")
            with open(env_path, "w") as f:
                f.writelines(new_lines)
            logger.info(f"Persisted {key} to .env")
        except Exception as e:
            logger.warning(f"Could not persist {key} to .env: {e}")

    async def start_kea_sync(self):
        if self._sync_task is None:
            self._sync_task = asyncio.create_task(self._kea_sync_loop())

    async def stop_kea_sync(self):
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None

    async def _kea_sync_loop(self):
        while True:
            try:
                res = await self._run_sync(self.engine.get_dhcp_prefixes)
                if res.get("status") == "SUCCESS":
                    for scope in res.get("scopes", []):
                        await self._sync_scope_to_kea(scope)
            except Exception as e:
                logger.error(f"KEA sync error: {e}", exc_info=True)
            await asyncio.sleep(300)

    async def _sync_scope_to_kea(self, scope: Dict[str, Any]):
        prefix = scope.get("prefix")
        gateway = scope.get("gateway") or "10.0.0.1"
        if not prefix:
            return
        try:
            import ipaddress
            net = ipaddress.ip_network(prefix, strict=False)
            pool_range = f"{net.network_address + 100}-{net.network_address + 200}"
        except Exception as e:
            logger.error(f"Cannot derive KEA pool range from {prefix!r}: {e}")
            return
        payload = {
            "command": "subnet4-add",
            "service": ["dhcp4"],
            "arguments": {"subnet4": [{"subnet": prefix, "pools": [{"pool": pool_range}], "option-data": [{"name": "routers", "data": gateway}]}]},
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{self.kea_url.rstrip('/')}/", json=payload)
                body = resp.json()
                result_obj = body[0] if isinstance(body, list) and body else body
                if result_obj.get("result") == 0:
                    logger.info(f"KEA synced scope {prefix}")
                else:
                    logger.warning(f"KEA rejected scope {prefix}: {result_obj.get('text')}")
        except Exception as e:
            logger.error(f"KEA HTTP error for {prefix}: {e}")

    async def _run_sync(self, fn, *args, **kwargs):
        """Run a synchronous (blocking) function in a thread pool so the event loop stays free."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = command_type.upper()
        logger.info(f"NetBox command: {normalized}")

        if normalized == "GET_VERSION":
            return {"status": "SUCCESS", "version": self.get_version()}

        if normalized == "UPDATE_CONFIG":
            url = data.get("netbox_url") or data.get("url")
            token = data.get("api_token")
            if url or token:
                self.engine.reconnect(
                    url or self.engine.url,
                    token or self.engine.token,
                )
                self.engine._ensure_custom_fields()
                if token:
                    self._persist_env("NETBOX_API_TOKEN", token)
                if url:
                    self._persist_env("NETBOX_URL", url)
            if data.get("kea_ctrl_url"):
                self.kea_url = data["kea_ctrl_url"]
            self.config.update(data)
            return {"status": "SUCCESS", "message": "NetBox config updated"}

        if normalized == "SPOKE_UPDATE":
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "pull", "--rebase", "--autostash"],
                    capture_output=True, text=True, cwd=os.getcwd()
                )
                if result.returncode == 0:
                    subprocess.Popen(["sudo", "systemctl", "restart", "lm-netbox"])
                    return {"status": "SUCCESS", "message": result.stdout.strip()}
                return {"status": "ERROR", "message": result.stderr.strip()}
            except Exception as e:
                return {"status": "ERROR", "message": str(e)}

        if normalized == "NETBOX_HEALTH":
            return await self._run_sync(self.engine.get_system_health)

        if normalized == "NETBOX_GET_SITES":
            return await self._run_sync(self.engine.get_sites)

        if normalized == "NETBOX_GET_RACKS":
            return await self._run_sync(self.engine.get_racks,
                                        site=data.get("site"), tenant=data.get("tenant"))

        if normalized == "NETBOX_ADD_RACK":
            return await self._run_sync(
                self.engine.add_rack,
                name=data.get("name", ""),
                site_slug=data.get("site", ""),
                u_height=int(data.get("u_height", 42)),
                facility_id=data.get("facility_id"),
            )

        if normalized == "NETBOX_UPDATE_RACK":
            return await self._run_sync(
                self.engine.update_rack,
                int(data.get("rack_id", 0)),
                name=data.get("name"),
                u_height=int(data["u_height"]) if data.get("u_height") not in (None, "") else None,
                facility_id=data.get("facility_id"),
            )

        if normalized == "NETBOX_DELETE_RACK":
            return await self._run_sync(self.engine.delete_rack, int(data.get("rack_id", 0)))

        if normalized == "NETBOX_GET_DEVICES":
            return await self._run_sync(self.engine.get_devices, site=data.get("site"),
                                        rack=data.get("rack"), tenant=data.get("tenant"))

        if normalized == "NETBOX_ADD_DEVICE":
            return await self._run_sync(
                self.engine.add_device_to_rack,
                name=data.get("name", ""),
                device_type_slug=data.get("device_type", ""),
                role_slug=data.get("role", ""),
                site_slug=data.get("site", ""),
                rack_name=data.get("rack", ""),
                rack_unit=int(data.get("rack_unit", 1)),
                face=data.get("face", "front"),
                status=data.get("status", "active"),
            )

        if normalized == "NETBOX_GET_DEVICE_FORM_OPTIONS":
            return await self._run_sync(self.engine.get_device_form_options)

        if normalized == "NETBOX_CLAIM_DEVICE":
            return await self._run_sync(
                self.engine.claim_device,
                name=data.get("name", ""),
                device_type_slug=data.get("device_type", ""),
                role_slug=data.get("role", ""),
                site_slug=data.get("site", ""),
                tenant_slug=data.get("tenant", ""),
                status=data.get("status", "active"),
                description=data.get("description", ""),
                ip_address=data.get("ip", ""),
                mac=data.get("mac", ""),
                dns_name=data.get("dns_name", ""),
            )

        if normalized == "NETBOX_DELETE_DEVICE":
            return await self._run_sync(self.engine.delete_device, int(data.get("device_id", 0)))

        if normalized == "NETBOX_UPDATE_DEVICE":
            return await self._run_sync(
                self.engine.update_device,
                int(data.get("device_id", 0)),
                name=data.get("name"),
                status=data.get("status"),
                rack_name=data.get("rack"),
                rack_unit=int(data["rack_unit"]) if data.get("rack_unit") not in (None, "") else None,
            )

        if normalized == "NETBOX_GET_PREFIXES":
            return await self._run_sync(self.engine.get_prefixes, site=data.get("site"),
                                        tenant=data.get("tenant"))

        if normalized == "NETBOX_ALLOCATE_PREFIX":
            return await self._run_sync(
                self.engine.allocate_prefix,
                parent_prefix=data.get("parent_prefix", ""),
                prefix_length=int(data.get("prefix_length", 24)),
                description=data.get("description", ""),
                site_slug=data.get("site"),
                status=data.get("status", "active"),
                requested_prefix=data.get("requested_prefix"),
                tenant_slug=data.get("tenant"),
            )

        if normalized == "NETBOX_FIND_AVAILABLE_PREFIXES":
            # Size may be given as a mask or as a host count (smallest mask that
            # fits). prefix_length wins when both are supplied.
            prefix_length = data.get("prefix_length")
            if prefix_length in (None, ""):
                hosts = int(data.get("hosts", 0) or 0)
                prefix_length = self.engine._mask_for_hosts(hosts) if hosts else 24
            return await self._run_sync(
                self.engine.find_available_prefixes,
                near=data.get("near", ""),
                prefix_length=int(prefix_length),
                count=int(data.get("count", 20)),
                exact=data.get("exact"),
                rfc1918=bool(data.get("rfc1918", True)),
            )

        if normalized == "NETBOX_CLAIM_PREFIX":
            return await self._run_sync(
                self.engine.claim_prefix,
                prefix=data.get("prefix", ""),
                tenant_slug=data.get("tenant"),
                description=data.get("description", ""),
                site_slug=data.get("site"),
                status=data.get("status", "active"),
            )

        if normalized == "NETBOX_UPDATE_PREFIX":
            return await self._run_sync(
                self.engine.update_prefix,
                int(data.get("prefix_id", 0)),
                description=data.get("description"),
                status=data.get("status"),
                site_slug=data.get("site"),
            )

        if normalized == "NETBOX_DELETE_PREFIX":
            return await self._run_sync(self.engine.delete_prefix, int(data.get("prefix_id", 0)))

        if normalized == "NETBOX_GET_IPS":
            return await self._run_sync(
                self.engine.get_ip_addresses,
                prefix=data.get("prefix"),
                device=data.get("device"),
                tenant=data.get("tenant"),
            )

        if normalized == "NETBOX_ALLOCATE_IP":
            return await self._run_sync(
                self.engine.allocate_ip,
                prefix=data.get("prefix", ""),
                description=data.get("description", ""),
                dns_name=data.get("dns_name", ""),
                status=data.get("status", "active"),
                address=data.get("address"),
            )

        if normalized == "NETBOX_RELEASE_IP":
            return await self._run_sync(self.engine.release_ip, int(data.get("ip_id", 0)))

        if normalized == "NETBOX_UPDATE_IP_ADDR":
            return await self._run_sync(
                self.engine.update_ip_address,
                int(data.get("ip_id", 0)),
                dns_name=data.get("dns_name"),
                description=data.get("description"),
                status=data.get("status"),
            )

        if normalized == "NETBOX_UPDATE_IP":
            return await self._run_sync(self.engine.update_device_ip,
                                        data.get("device", ""), data.get("ip", ""))

        if normalized == "NETBOX_DOC_VM":
            return await self._run_sync(
                self.engine.create_vm_entry,
                data.get("name", ""), data.get("cluster", ""),
                data.get("vcpus", 2), data.get("ram", 4096),
            )

        if normalized == "NETBOX_GET_TENANTS":
            return await self._run_sync(self.engine.get_tenants)

        if normalized == "NETBOX_SYNC_DHCP":
            await self.start_kea_sync()
            return {"status": "SUCCESS", "message": "DHCP sync triggered"}

        if normalized == "NETBOX_SYNC_VMS":
            # Hypervisor → NetBox VM sync. The hub relays a tenant's Proxmox VM
            # set (pulled from the pxmx spoke) here for an authoritative replace
            # into NetBox virtualization records. Blocking pynetbox calls run
            # off the event loop via _run_sync, like every other engine method.
            return await self._run_sync(
                self.engine.sync_vms,
                vms=data.get("vms", []),
                tenant_slug=data.get("tenant_slug", ""),
                replace=bool(data.get("replace", False)),
            )

        if normalized == "NETBOX_SYNC_DEVICES":
            # Discovery-source → NetBox device sync. The hub relays a tenant's
            # discovered devices (OPNsense DHCP leases + ARP for the firewall
            # sync, or switch/gateway ARP for the nw sync, attributed to the
            # tenant by prefix) for an authoritative replace into NetBox DCIM
            # devices + IP records. ``source`` is the ownership tag stamped on
            # created devices + the replace-delete scope key (opnsense/fw/
            # firewall → legacy "opnsense"; else verbatim, e.g. nw's
            # "Network Devices" so nw replace-delete never touches firewall
            # records). ``defaults`` carries the role/device_type/site slugs.
            return await self._run_sync(
                self.engine.sync_devices,
                devices=data.get("devices", []),
                tenant_slug=data.get("tenant_slug", ""),
                replace=bool(data.get("replace", False)),
                defaults=data.get("defaults", {}),
                source=data.get("source", "opnsense"),
            )

        if normalized == "NETBOX_SYNC_ACCESS_TRACKER":
            # Realtime NAC→IPAM reverse sync. The hub relays a tenant's recent
            # ClearPass Access Tracker / session records (CPPM_GET_RECENT_SESSIONS,
            # attributed to the tenant by IP prefix containment) for an
            # only-add-missing push into NetBox DCIM: a device per MAC not already
            # present, with a NIC interface (native MAC) + framed IP + a cable to
            # a switch device's port interface. NetBox stays source of truth →
            # replace is always False. See lm core/src/realtime_ipam_nac_sync.py.
            return await self._run_sync(
                self.engine.sync_access_tracker,
                sessions=data.get("sessions", []),
                tenant_slug=data.get("tenant_slug", ""),
                defaults=data.get("defaults", {}),
            )

        if normalized == "NETBOX_SEARCH":
            return await self._run_sync(self.engine.search,
                                        query=data.get("q", ""), tenant=data.get("tenant"))

        if normalized == "NETBOX_TENANT_VMID_RANGE":
            # LM hub VMID auto-allocation knob: read a tenant's
            # vmid_start/vmid_end custom-field range + the proxmox_vmid values
            # already in use on that tenant's VMs (inside the range), so the
            # hub can pick the next free VMID. No range set → vmid_start/end
            # None (caller falls back to Proxmox nextid).
            return await self._run_sync(self.engine.get_tenant_vmid_range,
                                        tenant_slug=data.get("tenant_slug", ""))

        logger.warning(f"Unknown NetBox command: {command_type}")
        return {"status": "ERROR", "message": f"Unknown command: {command_type}"}

    async def get_status(self) -> Dict[str, Any]:
        health = self.engine.get_system_health()
        return {
            "spoke_id": self.spoke_id,
            "module": "netbox",
            "api_health": health,
            "connection": "CONNECTED" if health.get("status") == "SUCCESS" else "DISCONNECTED",
            "kea_sync": "ACTIVE" if self._sync_task and not self._sync_task.done() else "INACTIVE",
        }

    def get_version(self) -> str:
        try:
            vp = os.path.join(os.path.dirname(__file__), "../VERSION")
            with open(vp) as f:
                return f.read().strip()
        except Exception:
            return "unknown"

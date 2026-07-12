import logging
import asyncio
import functools
import httpx
import os
import ssl
import tempfile
from typing import Dict, Any
try:
    from base_spoke import BaseSpoke
except ImportError:
    from core.src.base_spoke import BaseSpoke
from netbox_engine import NetboxEngine

logger = logging.getLogger("NetboxSpoke")

# Hub-level system commands handled by BaseControlPlane.handle_system_command
# (shared /opt/lm/core). A spoke running a STALE core (older than the build that
# added one of these handlers — e.g. CLEAR_LOGS, added cf37058) returns None from
# handle_system_command for that command, so dispatch falls through to the
# module's handle_command. Rather than WARN + ERROR (which cascades to a FAILED
# mailbox ack → "unknown message ID" noise on the hub), degrade gracefully: the
# spoke clearly can't honour it until its core updates, so ack success and let
# the next SPOKE_UPDATE pull current core. Only system commands are muted here;
# genuinely-unknown MODULE commands still WARN + ERROR (so typos surface).
_SYSTEM_COMMANDS = frozenset({
    "HUB_PING", "RUN_COMMAND", "GET_AGENTS",
    "SPOKE_SET_LOG_LEVEL", "SET_LOG_LEVEL", "CLEAR_LOGS",
    "SPOKE_GET_STATUS", "SPOKE_UPDATE",
    "SPOKE_SET_HUB_SECRET", "SPOKE_UPDATE_SESSION_KEY", "SPOKE_SET_HOSTNAME",
})

# Sudoers-allowed cert-install helper (provisioned by netbox/install.sh). The
# spoke runs as unprivileged svc_lm and NetBox has no cert API, so LE cert
# distribution invokes this root helper, which swaps /etc/lm/netbox/tls/netbox
# .{crt,key} + reloads nginx. The spoke writes fullchain+privkey to 0600 temp
# files under /tmp and passes the two paths as args; the helper validates
# (openssl + key/cert pubkey match), installs atomically, nginx -t (restores
# on failure), reloads, and prints a one-line "OK <msg>" / "ERROR: <msg>".
# See netbox/install.sh + lm/core/src/hub_cert_distribution.py for the pattern.
_NETBOX_INSTALL_CERT_HELPER = "/usr/local/bin/lm-netbox-install-cert"


def _as_bool(val, default: bool = True) -> bool:
    """Parse a config/env value into a bool. Accepts real bools, and the strings
    0/1/true/false/yes/no/on/off (case-insensitive). Anything else → default."""
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


class NetboxSpoke(BaseSpoke):
    """
    NetBox spoke: DCIM (rack/device) and IPAM (prefix/IP) management + KEA DHCP sync.
    """
    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        super().__init__(spoke_id, config)
        self.engine = NetboxEngine(
            url=config.get("netbox_url", os.getenv("NETBOX_URL", "http://localhost:8000")),
            token=config.get("api_token", os.getenv("NETBOX_API_TOKEN", "")),
            # Verify the NetBox server's TLS cert by default; turn OFF for a
            # NetBox behind a self-signed cert (e.g. the Azure NetBox on a public
            # IP) via the WebUI toggle / NETBOX_VERIFY_SSL=0.
            verify_ssl=_as_bool(config.get("netbox_verify_ssl",
                                           os.getenv("NETBOX_VERIFY_SSL", "1")), default=True),
        )
        # KEA Control Agent URL. Default port 8760 matches install_kea.sh —
        # NOT 8000 (the LM hub owns 8000 on hub-colocated boxes; KEA CA on 8000
        # fails to bind and the sync loop POSTs the hub → 405 "rejected scope").
        self.kea_url = config.get("kea_ctrl_url", os.getenv("KEA_CTRL_URL", "http://localhost:8760"))
        self._sync_task = None
        # Short-TTL cache for read-only picklist commands that populate WebUI
        # dropdowns (sites/racks/tenants/device-form-options). These change
        # rarely but the UI re-fetches them on every form open / view switch,
        # so a 60s TTL cuts a round-trip per open without staling long enough
        # to hide a just-added site. Invalidated by _picklist_invalidate() on
        # any mutation (add/update/delete/claim/allocate/release/sync) and on
        # UPDATE_CONFIG (token/url change).
        self._picklist_cache: Dict[str, Any] = {}
        self._picklist_ttl = 60.0
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

    def _picklist_invalidate(self):
        """Drop all cached picklist data — call after any mutation that could
        change the dropdown contents (add/update/delete/claim/allocate/release/
        sync, or an UPDATE_CONFIG that repoints NetBox)."""
        if self._picklist_cache:
            self._picklist_cache.clear()

    async def _run_picklist(self, key: str, fn, *args, **kwargs):
        """TTL-gated read for picklist commands. ``key`` is a stable string
        encoding the command + its filter args so filtered reads (e.g.
        NETBOX_GET_RACKS?site=X) cache separately. Returns a deep-enough copy
        of the cached result (the engine returns plain dicts/lists, so a
        shallow copy is fine — callers don't mutate)."""
        import time as _time
        entry = self._picklist_cache.get(key)
        if entry and (_time.time() - entry["ts"]) < self._picklist_ttl:
            return entry["value"]
        value = await self._run_sync(fn, *args, **kwargs)
        self._picklist_cache[key] = {"ts": _time.time(), "value": value}
        return value

    # Commands that change NetBox state and so could stale a cached picklist.
    _PICKLIST_MUTATIONS = frozenset({
        "NETBOX_ADD_RACK", "NETBOX_UPDATE_RACK", "NETBOX_DELETE_RACK",
        "NETBOX_ADD_DEVICE", "NETBOX_CLAIM_DEVICE", "NETBOX_DELETE_DEVICE",
        "NETBOX_UPDATE_DEVICE", "NETBOX_ALLOCATE_PREFIX", "NETBOX_CLAIM_PREFIX",
        "NETBOX_UPDATE_PREFIX", "NETBOX_DELETE_PREFIX", "NETBOX_ALLOCATE_IP",
        "NETBOX_RELEASE_IP", "NETBOX_UPDATE_IP", "NETBOX_UPDATE_IP_ADDR",
        "NETBOX_DOC_VM", "NETBOX_SYNC_DHCP", "NETBOX_SYNC_VMS",
        "NETBOX_SYNC_DEVICES", "NETBOX_SYNC_NW_DEVICE", "NETBOX_SYNC_ACCESS_TRACKER",
        "NETBOX_STALENESS_SWEEP", "NETBOX_PROVISION_CUSTOM_FIELDS",
    })

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = command_type.upper()
        logger.info(f"NetBox command: {normalized}")

        # A mutation could change the dropdown contents a picklist feeds — drop
        # the cache up front so the next read sees fresh data. (UPDATE_CONFIG
        # invalidates itself below.)
        if normalized in self._PICKLIST_MUTATIONS:
            self._picklist_invalidate()

        if normalized == "GET_VERSION":
            return {"status": "SUCCESS", "version": self.get_version()}

        if normalized == "UPDATE_CONFIG":
            url = data.get("netbox_url") or data.get("url")
            token = data.get("api_token")
            # TLS-verify toggle — reconnect when it (or url/token) changes so the
            # new http_session picks up verify on/off immediately.
            verify_ssl = None
            if "netbox_verify_ssl" in data:
                verify_ssl = _as_bool(data.get("netbox_verify_ssl"), default=self.engine.verify_ssl)
            if url or token or verify_ssl is not None:
                self.engine.reconnect(
                    url or self.engine.url,
                    token or self.engine.token,
                    verify_ssl=verify_ssl,
                )
                # Offload to a thread: _ensure_custom_fields does sync pynetbox
                # I/O (list + per-field GET + save) that can block for seconds
                # when NetBox is slow/down. This spoke shares the lm-svcs agent's
                # event loop with the dns/dhcp sub-spokes; a blocking call here
                # stalls the whole shared loop and surfaces as simultaneous
                # Request Timeouts from lm-svcs/lm-svcs-dhcp/lm-svcs-dns (the
                # shared-loop stall). _run_sync runs it in the executor.
                await self._run_sync(self.engine._ensure_custom_fields)
                if token:
                    self._persist_env("NETBOX_API_TOKEN", token)
                if url:
                    self._persist_env("NETBOX_URL", url)
                if verify_ssl is not None:
                    self._persist_env("NETBOX_VERIFY_SSL", "1" if verify_ssl else "0")
            if data.get("kea_ctrl_url"):
                self.kea_url = data["kea_ctrl_url"]
            self.config.update(data)
            # Repointing NetBox (or KEA) makes any cached picklist data stale.
            self._picklist_invalidate()
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
            return await self._run_picklist("GET_SITES", self.engine.get_sites)

        if normalized == "NETBOX_GET_RACKS":
            return await self._run_picklist(
                f"GET_RACKS|site={data.get('site')}|tenant={data.get('tenant')}",
                self.engine.get_racks,
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
            return await self._run_picklist("GET_DEVICE_FORM_OPTIONS", self.engine.get_device_form_options)

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
            return await self._run_picklist("GET_TENANTS", self.engine.get_tenants)

        if normalized == "NETBOX_SYNC_DHCP":
            await self.start_kea_sync()
            return {"status": "SUCCESS", "message": "DHCP sync triggered"}

        if normalized == "NETBOX_SYNC_VMS":
            # Hypervisor → NetBox VM sync. The hub relays a tenant's Proxmox VM
            # set (pulled from the pxmx spoke) here for an authoritative replace
            # into NetBox virtualization records. Blocking pynetbox calls run
            # off the event loop via _run_sync, like every other engine method.
            # ``source_of_truth``: "external" (Proxmox owns VMs → overwrite) or
            # "netbox" (NetBox owns VMs → only-add-missing). Hub default config is
            # proxmox/external; the WebUI Source-of-Truth selector sets it.
            return await self._run_sync(
                self.engine.sync_vms,
                vms=data.get("vms", []),
                tenant_slug=data.get("tenant_slug", ""),
                replace=bool(data.get("replace", False)),
                source_of_truth=data.get("source_of_truth", "external"),
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
            # ``source_of_truth``: "external" (discovery feed owns the device →
            # overwrite IP mac/dns_name + rename) or "netbox" (NetBox owns the
            # device → only-add-missing: refresh last_seen only).
            return await self._run_sync(
                self.engine.sync_devices,
                devices=data.get("devices", []),
                tenant_slug=data.get("tenant_slug", ""),
                replace=bool(data.get("replace", False)),
                defaults=data.get("defaults", {}),
                source=data.get("source", "opnsense"),
                source_of_truth=data.get("source_of_truth", "external"),
            )

        if normalized == "NETBOX_SYNC_NW_DEVICE":
            # Network Devices POLL NOW inventory sync. The hub relays a single
            # polled switch/gateway (SNMP/CLI/REST) here for an upsert into a
            # NetBox dcim.device + its dcim.interfaces + per-interface IPs — the
            # device itself becomes the NetBox record (distinct from the
            # ARP-neighbor→endpoint NETBOX_SYNC_DEVICES flow). ``defaults`` carry
            # the role/device_type/site slugs required to create the device.
            return await self._run_sync(
                self.engine.sync_nw_device,
                device=data.get("device", {}),
                interfaces=data.get("interfaces", []),
                tenant_slug=data.get("tenant_slug", ""),
                defaults=data.get("defaults", {}),
                source=data.get("source", "Network Devices"),
            )

        if normalized == "NETBOX_SYNC_ACCESS_TRACKER":
            # Realtime NAC→IPAM reverse sync. The hub relays a tenant's recent
            # ClearPass Access Tracker / session records (CPPM_GET_RECENT_SESSIONS,
            # attributed to the tenant by IP prefix containment) for an
            # only-add-missing push into NetBox DCIM: a device per MAC not already
            # present, with a NIC interface (native MAC) + framed IP + a cable to
            # a switch device's port interface. NetBox stays source of truth →
            # replace is always False. See lm core/src/realtime_ipam_nac_sync.py.
            # ``source_of_truth`` is accepted for parity (always only-add-missing
            # here; an "external" owner that would overwrite isn't exposed in v1).
            return await self._run_sync(
                self.engine.sync_access_tracker,
                sessions=data.get("sessions", []),
                tenant_slug=data.get("tenant_slug", ""),
                defaults=data.get("defaults", {}),
                source_of_truth=data.get("source_of_truth", "netbox"),
            )

        if normalized == "NETBOX_STALENESS_SWEEP":
            # Cluster-wide staleness sweep: devices/VMs/IPs not seen for
            # ``stale_days`` → offline + decommissioned_at; offline + aged past
            # ``delete_days`` → deleted (IPs free automatically). The hub runs
            # this on a schedule + on-demand (StalenessSweepMixin). See
            # lm core/src/staleness_sweep.py. Defaults mirror the hub config
            # defaults so a sweep without explicit thresholds is safe.
            return await self._run_sync(
                self.engine.staleness_sweep,
                stale_days=int(data.get("stale_days", 7)),
                delete_days=int(data.get("delete_days", 30)),
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

        if normalized == "NETBOX_PROVISION_CUSTOM_FIELDS":
            # WebUI "Apply schema changes" button (Setup/IPAM → edit/add a
            # NetBox instance). force=True re-runs the full idempotent
            # verify/attach pass over CUSTOM_FIELDS_SPEC so an existing install
            # can pick up newly-added fields without a reinstall, and re-running
            # it never errors when the fields are already present. Returns the
            # engine's report dict (status/total/present/created/attached/...).
            return await self._run_sync(self.engine._ensure_custom_fields,
                                        force=True)

        if normalized == "INSTALL_CERT":
            # LE cert distribution (hub-brokered): the hub pulled fullchain +
            # privkey from the le spoke and pushed INSTALL_CERT here. NetBox
            # has no cert API and this spoke runs as unprivileged svc_lm, so
            # we validate the pair in-process (throwaway ssl ctx — same guard
            # the hub uses in _install_cert_on_hub), write both to 0600 temp
            # files under /tmp, and hand the paths to the root sudoers helper
            # which swaps /etc/lm/netbox/tls/netbox.{crt,key} + reloads nginx.
            # ``identifier`` is ignored (one NetBox HTTPS endpoint). Never
            # leaves a temp file behind (finally). Logs to the NetboxSpoke
            # logger → the IPAM/NetBox log tab (this is NetBox's own install
            # activity, separate from the hub's le.distribution Certificates
            # tab).
            domain = data.get("domain", "") or ""
            fullchain = data.get("fullchain", "") or ""
            privkey = data.get("privkey", "") or ""
            if not fullchain or not privkey:
                logger.warning("[cert] %s → netbox: FAILED — missing cert material", domain)
                return {"status": "ERROR", "message": "missing cert material"}
            if "BEGIN CERTIFICATE" not in fullchain or "PRIVATE KEY" not in privkey:
                logger.warning("[cert] %s → netbox: FAILED — cert/key not PEM", domain)
                return {"status": "ERROR", "message": "fullchain/privkey not PEM"}

            # Validate in-process BEFORE calling the helper so a malformed
            # pair never reaches the live nginx paths. load_cert_chain into a
            # throwaway ssl context — mirrors _install_cert_on_hub.
            crt_tmp = key_tmp = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".crt.pem",
                                                 delete=False) as cf:
                    cf.write(fullchain); crt_tmp = cf.name
                with tempfile.NamedTemporaryFile("w", suffix=".key.pem",
                                                 delete=False) as kf:
                    kf.write(privkey); key_tmp = kf.name
                os.chmod(crt_tmp, 0o600); os.chmod(key_tmp, 0o600)
                try:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(crt_tmp, key_tmp)
                except Exception as e:
                    logger.warning("[cert] %s → netbox: FAILED — cert validation "
                                   "failed (helper not called): %s", domain, e)
                    return {"status": "ERROR",
                            "message": f"cert validation failed (helper not called): {e}"}

                # Hand the temp paths to the root helper. It re-validates +
                # installs + reloads + prints one line. 20s ceiling (nginx -t
                # + reload is fast; a hung sudo would otherwise hang the spoke).
                # FileNotFoundError (sudo/helper missing) or a sudo denial must
                # surface as ERROR, not propagate — the hub's distribution loop
                # expects a {"status","message"} from every target.
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "sudo", "-n", _NETBOX_INSTALL_CERT_HELPER, crt_tmp, key_tmp,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=20.0)
                except asyncio.TimeoutError:
                    try: proc.kill()
                    except (ProcessLookupError, UnboundLocalError): pass
                    logger.warning("[cert] %s → netbox: FAILED — helper timed out", domain)
                    return {"status": "ERROR", "message": "cert-install helper timed out"}
                except Exception as e:
                    logger.warning("[cert] %s → netbox: FAILED — helper invocation "
                                   "failed: %s", domain, e)
                    return {"status": "ERROR",
                            "message": f"cert-install helper invocation failed: {e}"}
                out = (out_b or b"").decode(errors="replace").strip()
                err = (err_b or b"").decode(errors="replace").strip()
                if proc.returncode == 0 and out.startswith("OK"):
                    logger.info("[cert] %s → netbox: installed — %s", domain, out[2:].strip() or out)
                    return {"status": "SUCCESS",
                            "message": out[2:].strip() or out or "installed on netbox"}
                msg = err or out or f"helper exit {proc.returncode}"
                logger.warning("[cert] %s → netbox: FAILED — %s", domain, msg)
                return {"status": "ERROR", "message": msg}
            finally:
                for p in (crt_tmp, key_tmp):
                    if p:
                        try: os.unlink(p)
                        except OSError: pass

        if command_type in _SYSTEM_COMMANDS:
            # Stale /opt/lm/core: this system command should have been
            # intercepted by handle_system_command but wasn't (core predates
            # its handler). Don't WARN+ERROR (FAILED ack cascade); the next
            # SPOKE_UPDATE pulls current core. See _SYSTEM_COMMANDS above.
            logger.info("Stale-core fallthrough for system command %s "
                        "(handle_system_command returned None); update spoke "
                        "to clear.", command_type)
            return {"status": "SUCCESS",
                    "message": f"{command_type} not applied — spoke core stale, update needed"}
        logger.warning(f"Unknown NetBox command: {command_type}")
        return {"status": "ERROR", "message": f"Unknown command: {command_type}"}

    async def get_status(self) -> Dict[str, Any]:
        # get_system_health() issues a pynetbox HTTP round-trip; run it in a
        # thread so the spoke's asyncio loop stays free to heartbeats / inbound
        # commands while NetBox (or its DB) is slow to answer.
        health = await self._run_sync(self.engine.get_system_health)
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

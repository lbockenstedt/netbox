# netbox — IPAM/DCIM

NetBox spoke. Repo: `netbox`. `module_type = "ipam"`. See [architecture-topology.md](architecture-topology.md).

## Role & module_type

The IPAM/DCIM source-of-truth spoke. Owns NetBox REST access (sites, racks, devices, prefixes, IPs, VMs, tenants) and a background NetBox→Kea DHCP scope sync. It is the **sink** for every discovery sync (firewall DHCP/ARP via opnsense, switch ARP/MAC via nw, hypervisor VMs via pxmx, NAC sessions via cppm) and where staleness sweeps age objects out. Hub-colocated by default (the hub owns :443 on the same box).

## What it does

This module connects Lab Manager to a NetBox instance and makes NetBox the shared source of truth for "what devices, VMs, IPs, and prefixes exist in this lab." In the WebUI it shows up as the **IPAM** section (sites, racks, devices, prefixes, IP addresses, tenants), and its connection is configured under **Setup → IPAM**.

You rarely have to enter data into NetBox by hand: this module is the landing zone every other discovery source writes to — the firewall's DHCP/ARP data, switch ARP/MAC tables, Proxmox VM inventories, and NAC (ClearPass) sessions all get synced in here automatically, so a device that shows up anywhere in the lab tends to show up in IPAM without anyone typing it in. It also ages out and eventually deletes records for things that stop being seen, so IPAM doesn't accumulate stale ghosts forever.

## Entrypoints

- `python3 -m src.control_plane` (`NetboxControlPlane`); spoke `NetboxSpoke(BaseSpoke)`, module name `"netbox"`.
- systemd `lm-netbox.service` (spoke); full-app mode also installs `netbox.service` (gunicorn) + `netbox-rq.service` + nginx, with a self-heal block that restarts gunicorn on 502/000.
- Installers: `install.sh` (spoke + optional full app), `install_kea.sh` (Kea CA pinned to **8760**).

> **Primarily a role now.** The netbox spoke runs mainly as the **`netbox`** role hosted by the generic agent (`agent-<hostname>`, unit `lm-agent`): the agent opens a sub-spoke `{agent}-netbox` (module_type `ipam`, parent-auto-approved) and self-installs it via `agent/src/agent_spoke.py::_install_role` (clones `lbockenstedt/netbox.git` + deps). The dedicated `lm-netbox.service` / `install.sh` `netbox-spoke-1` path is the **legacy/standalone** alternative; the NetBox *application* (gunicorn/nginx full-app) remains a separate install either way. Connection config (NetBox URL/token) comes from the hub push (WebUI), not a per-module `.env`.

## Ports / backends

- NetBox REST via `pynetbox` (`NetboxEngine`). Default `http://localhost:8000`; gunicorn `127.0.0.1:8001` behind nginx :80 in full-app mode.
- Kea Control Agent REST (`subnet4-add`) at `KEA_CTRL_URL` (default `http://localhost:8000`; `install_kea.sh` pins to **8760**).
- Serves no port itself.

## Environment variables

`NETBOX_URL` (`http://localhost:8000`), `NETBOX_API_TOKEN` (required), `KEA_CTRL_URL` (`http://localhost:8000` — override to 8760 on hub-colocated), `SPOKE_SECRET`, `HUB_SECRET`. `_persist_env()` writes `NETBOX_API_TOKEN`/`NETBOX_URL` back to `.env` on `UPDATE_CONFIG`.

## Install flags

`install.sh`: `--hub`, `--id`/`--name`, `--secret`, `--hub-secret`, `--netbox-url`, `--netbox-token`, `--db-pass`, `--superuser`, `--superpass`, `--supermail`, `--netbox-version`, `--spoke-only` (skip full app), `--infra-only` (install only the NetBox app — Postgres/Redis/gunicorn/nginx + custom-field/validator provisioning — and skip the LM spoke unit; used by the generic agent's `netbox-server` deploy role; mutually exclusive with `--spoke-only`), `--all-prereqs` (no-op), `--admin-token` (deprecated). Installs NetBox v4.2+, provisions custom fields via REST from `custom_fields_spec.py`, injects `CUSTOM_VALIDATORS` (`ProxmoxRangeValidator`, `ProxmoxVmidInRangeValidator`), registers the API token with the hub `POST /setup/netbox-config`.

## Key commands / handlers (`netbox_spoke.handle_command`, via `_run_sync` thread pool)

DCIM/IPAM: `NETBOX_HEALTH`, `NETBOX_GET_SITES`, `NETBOX_GET/ADD/UPDATE/DELETE_RACK`, `NETBOX_GET/ADD/CLAIM/DELETE/UPDATE_DEVICE`, `NETBOX_GET_DEVICE_FORM_OPTIONS`, `NETBOX_GET_PREFIXES`, `NETBOX_ALLOCATE_PREFIX`, `NETBOX_FIND_AVAILABLE_PREFIXES`, `NETBOX_CLAIM/UPDATE/DELETE_PREFIX`, `NETBOX_GET_IPS`, `NETBOX_ALLOCATE/RELEASE/UPDATE_IP(_ADDR)`, `NETBOX_DOC_VM`, `NETBOX_GET_TENANTS`, `NETBOX_SEARCH`.

> **Rack tenant attribution** — `NETBOX_ADD_RACK` / `NETBOX_UPDATE_RACK` accept a `tenant` (NetBox tenant **slug**); the WebUI Add/Edit Rack modal stamps the current tenant (`currentTenant`, omitted when viewing the global `default` tenant). `add_rack`/`update_rack` resolve the slug to a tenant id (mirroring `allocate_prefix`/`claim_device`); an unresolvable slug is REFUSED (no silent unattributed create). On update, a missing tenant (None) preserves the rack's existing tenant; an empty string clears it to global. This is why a rack created from a tenant view lands in that tenant and shows up under it.

Sync family:
- `NETBOX_SYNC_DHCP` — background KEA sync loop (`_kea_sync_loop`, 300s): `get_dhcp_prefixes` → POST `subnet4-add` per scope with derived pool range + routers option.
- `NETBOX_SYNC_VMS` — `sync_vms(vms, tenant_slug, replace, source_of_truth)`: Proxmox→NetBox VM sync; matches by `proxmox_unique_id` cf; tag-driven tenant attribution; `replace=True` deletes proxmox-sourced VMs absent from the pull; `source_of_truth` `external` (Proxmox overwrites) or `netbox` (only-add-missing).
- `NETBOX_SYNC_DEVICES` — `sync_devices(devices, tenant_slug, replace, defaults, source, source_of_truth)`: discovery-source (opnsense DHCP/ARP or nw ARP) → DCIM device + IP upsert; `source` stamps `discovered_from` and scopes replace-delete.
- `NETBOX_SYNC_NW_DEVICE` — `sync_nw_device(device, interfaces, …)`: per-switch/gateway POLL NOW upsert into `dcim.device` + `dcim.interfaces` + per-interface IPs; matches by `nw_device_id` cf; marks interfaces `nw_managed`.
- `NETBOX_SYNC_ACCESS_TRACKER` — `sync_access_tracker(...)`: ClearPass sessions → only-add-missing (replace always False): device per MAC, NIC interface, framed IP, cable to a switch port interface.
- `NETBOX_STALENESS_SWEEP` — `staleness_sweep(stale_days=7, delete_days=30)`: not seen `stale_days` → offline + `decommissioned_at`; offline + aged `delete_days` → deleted (IPs freed).
- `NETBOX_TENANT_VMID_RANGE` — tenant `vmid_start`/`vmid_end` cf + in-use `proxmox_vmid` for hub VMID auto-allocation.
- `NETBOX_PROVISION_CUSTOM_FIELDS` — `force=True` re-run of `_ensure_custom_fields` (WebUI "Apply schema changes" button).
- `NETBOX_SEED_CATALOG` — `seed_catalog()`: load the bundled Aruba/HPE/Juniper device-type catalog (`src/seed_catalog.json`) — get-or-create manufacturers + device types (upsert scalars), add-missing interface/console/power templates. Idempotent; admin-only (WebUI Setup → Module Management → "Seed catalog"). In `_PICKLIST_MUTATIONS` (drops the picklist cache).

Plus `GET_VERSION`, `UPDATE_CONFIG`, `SPOKE_UPDATE`.

## Key files

`src/netbox_engine.py` (thin composition class — `NetboxEngine(DcimMixin, IpamMixin, VmSyncMixin, ChangelogMixin, SyncMixin, StalenessMixin, TenancyMixin)`; owns the pynetbox client, `_apply_auth`, the module-level HTTP semaphore, and the single-page/paginated GET helpers `_api_get`/`_api_get_all`). The sync/DCIM/IPAM logic lives in the mixin modules it composes:

- `src/netbox_dcim.py` (`DcimMixin`) — sites, racks, devices, `update_device_ip`, form-options picklists, `seed_catalog()` (loads `src/seed_catalog.json`).
- `src/netbox_ipam.py` (`IpamMixin`) — prefixes, IPs, `find_available_prefixes`, `claim_prefix`, allocate/release.
- `src/netbox_vmsync.py` (`VmSyncMixin`) — `sync_vms`, `_ensure_custom_fields`, `_assign_vm_primary_ip4`, `get_tenant_vmid_range`, `create_vm_entry`.
- `src/netbox_sync.py` (`SyncMixin`) — `sync_devices`, `sync_nw_device`, `sync_access_tracker`.
- `src/netbox_staleness.py` (`StalenessMixin`) — `staleness_sweep`.
- `src/netbox_changelog.py` (`ChangelogMixin`) — `_journal` (object-change entries).
- `src/netbox_tenancy.py` (`TenancyMixin`) — `get_dhcp_prefixes` (the KEA sync source) + tenant helpers.

`src/custom_fields_spec.py` (pure-data `CUSTOM_FIELDS_SPEC` — single source of truth for the engine self-heal, the Django shell block, and the REST provisioning block), `src/netbox_spoke.py` (dispatch, KEA loop, `_persist_env`, picklist cache), `src/control_plane.py`, `install.sh`, `install_kea.sh`, `API_SPEC.md`.

## Notable behaviors & gotchas

- **Custom fields self-heal at startup** (`_ensure_custom_fields()` called from `NetboxSpoke.__init__`, best-effort so a restricted token never breaks the spoke). Failures are logged at **WARNING** (visible by default — a restricted/permission error surfaces in the spoke log + `GET_ERROR_LOGS`), not raised. Spec: `proxmox_unique_id`, `proxmox_vmid`, `proxmox_node`, `proxmox_type`, `proxmox_labels`, `discovered_from`, `nw_device_id`, `nw_managed`, `mac_address` (on `ipam.ipaddress` + `dcim.device`), `switch_name`/`switch_ip`/`switch_port`, `last_seen`, `decommissioned_at`, `vmid_start`/`vmid_end` (on `tenancy.tenant`). CFs-not-attached is a stale-deploy gap (rerun `install.sh` / Apply-schema), not a spec gap.
- **pynetbox auth pin** — `_apply_auth()` sets `Authorization: Token …` on `http_session.headers` because `_api_get()` calls `http_session.get()` directly (bypassing pynetbox's per-request wrapper) and would otherwise go out unauthenticated → 403.
- **HTTP semaphore** — module-level `_netbox_http_sem = threading.Semaphore(1)` serializes direct GETs to avoid OOM-killing gunicorn workers on concurrent IPAM queries. `_api_get_all` paginates via `next` links; `_api_get` is single-page and would silently truncate.
- **Hypervisor vminterface endpoint** — accessor is `nb.virtualization.interfaces` (REST endpoint `interfaces`), NOT `nb.virtualization.vminterfaces` (404s). Name collision: model string is `virtualization.vminterface`.
- **sync_vms primary_ip4** — `_assign_vm_primary_ip4` PATCHes only `primary_ip4` via `virtual_machines.update`; a full `obj.save` re-sends unattached custom_fields → 400 "does not exist for this object type". Bare-return→`(0,None)` contract.
- **sync_devices fresh-fetch** — create branch re-sent unprovisioned custom_fields on primary_ip4 save → 400 killed the upsert; fixed by fresh `devices.get` + best-effort save so a missing cf degrades to "field not set".
- **Kea port trap** — KEA CA must be 8760, not 8000 (the unified hub owns :443, but NetBox/the legacy webui-spoke can occupy :8000 on a co-located box); KEA on 8000 fails to bind there and the sync loop POSTs the hub → 405.
- **Custom validators** — `ProxmoxRangeValidator` (tenant ranges non-overlapping) + `ProxmoxVmidInRangeValidator` (VM VMID within tenant range), lenient when a range is unset. NetBox v4.2+.

## How it works

**Command path.** Whether it runs standalone (`lm-netbox.service`) or as the `netbox` role on a generic agent, this module is a WebSocket spoke that dials the hub over `/ws/spoke`. WebUI pages under IPAM never talk to NetBox directly: an action becomes a JSON command (the `NETBOX_*` names listed under "Key commands" above), sent hub → spoke, dispatched by `netbox_spoke.handle_command`, and executed against the NetBox REST API through `pynetbox`/`NetboxEngine` — most calls run in a thread pool (`_run_sync`) so a slow NetBox request never blocks the spoke's event loop.

**How the connection gets configured.** The spoke connects to `NETBOX_URL`/`NETBOX_API_TOKEN` from its environment at boot (or NetBox is provisioned locally by `install.sh`), but from then on the connection is **hub-managed**: saving NetBox URL/token in the WebUI's **Setup → IPAM** page sends an `UPDATE_CONFIG` command that calls `engine.reconnect(url, token)` and re-runs the custom-field self-heal — it is not something you hand-edit in a per-module `.env` on an ongoing basis (though the spoke does write the new value back to its local `.env` via `_persist_env` so a restart picks up the same config). The one exception is the KEA Control Agent URL (`kea_ctrl_url`), which can also be pushed the same way.

**Custom fields — why syncs sometimes need "provisioning".** Every discovery sync writes to NetBox custom fields (things like `proxmox_unique_id`, `proxmox_vmid`, `discovered_from`, `nw_device_id`, `mac_address`, `last_seen`, `decommissioned_at`, tenant `vmid_start`/`vmid_end`, etc. — the full list lives in `custom_fields_spec.py`). The spoke self-heals these at startup (best-effort — a restricted API token just logs a warning line rather than crashing the spoke), but a NetBox instance that was never provisioned, or that fell behind after a schema change, will reject a sync with a 400 error until the fields exist. The WebUI's "Apply schema changes" button (Setup → IPAM → edit the NetBox instance) calls this same provisioning logic with `force=True` and is always safe to re-run — it only adds what's missing.

**Data flow in.** Every other module hands NetBox data the same general way: the hub collects records from a discovery source (opnsense DHCP/ARP, nw switch ARP/MAC, pxmx VM inventories, cppm NAC sessions) and relays them here as one of the `NETBOX_SYNC_*` commands. Each sync family has its own matching/replace rules (see "Notable behaviors" below and each sync's description in "Key commands"), but the shared idea is: match an existing record first (by a stable identifier like a Proxmox unique ID, a MAC address, or an IP), only create a new one if nothing matches, and only delete records the same source previously created if that source's `replace=True` pass no longer sees them.

**Source of truth.** Most syncs accept a `source_of_truth` of `"external"` (the discovery feed is authoritative — it can overwrite fields and rename things) or `"netbox"` (NetBox's existing data wins — the sync only fills in what's missing, never overwrites). This is set per-sync from the hub's configuration (source-of-truth selector in the WebUI), not hard-coded in this module.

**Staleness sweep.** A background/scheduled job (`NETBOX_STALENESS_SWEEP`, hub-triggered) ages out anything a sync previously touched (identified by having a `last_seen` custom field — hand-entered inventory has none and is never swept): not seen for `stale_days` (default **7**) → marked offline + `decommissioned_at` stamped; still offline after `delete_days` (default **30**) → deleted outright, which frees any IP addresses it held. This is why a device that unplugged a week ago shows "offline" rather than vanishing immediately, and why it eventually disappears entirely after a month of continued absence.

**KEA DHCP sync.** Independently of the discovery syncs, a background loop (`_kea_sync_loop`, every **300s**) reads DHCP-eligible prefixes out of NetBox and pushes them to a KEA Control Agent as `subnet4-add` calls (with a derived pool range and the prefix's gateway as the `routers` option) — this is how a prefix you allocate in IPAM becomes an actual DHCP scope.

## How to use it

- **Point Lab Manager at a NetBox instance.** If NetBox is being installed fresh, `install.sh` provisions it and registers the token with the hub automatically. To point at an existing/external NetBox, or to change the URL/token later, go to **Setup → IPAM**, enter the **NetBox URL** and **API Token**, and save — the hub pushes it to the spoke immediately.
- **Provision or refresh the custom fields NetBox needs.** Setup → IPAM → edit the NetBox instance → **Apply schema changes**. Safe to click any time (it's idempotent); do this after connecting a pre-existing NetBox for the first time, or after upgrading Lab Manager if a sync starts complaining about a missing field.
- **Let discovery fill in devices/VMs for you.** You generally don't add devices/VMs by hand for anything that's already visible to the firewall, a switch, Proxmox, or NAC — those show up in IPAM on their own via the sync loops described above. Manual add/claim (`NETBOX_ADD_DEVICE`/`NETBOX_CLAIM_DEVICE`, etc.) is for inventory nothing else can see yet.
- **Allocate a prefix or IP.** IPAM → Prefixes/IPs → allocate; you can search for available space within a parent block or claim a specific prefix/IP directly.
- **Force a staleness sweep or check why something is offline.** The staleness sweep normally runs on the hub's own schedule; if you need to check sooner, look at a device/VM's `last_seen` value — anything older than the configured stale-days threshold will show (or shortly become) offline.
- **Seed the device-type catalog (admin).** Setup → Module Management → **Seed catalog** loads the bundled Aruba/HPE/Juniper manufacturers + device types + interface/console/power templates. Idempotent (re-runs upsert scalars + add-missing templates, never delete/re-type). To add/amend a model, edit `src/seed_catalog.json`, redeploy the spoke, and click again. Renames/port-type changes on an existing name = delete that device type in NetBox + re-seed (re-create).

## Troubleshooting / common questions

- **A sync fails with 400 / "field does not exist for this object type."** The custom fields this sync needs were never provisioned on this NetBox (a stale deploy, or an externally-managed NetBox that was connected without provisioning). Fix: Setup → IPAM → edit the instance → **Apply schema changes**, or rerun `install.sh` on the module side. This is safe and idempotent.
- **The NetBox / IPAM page shows offline or "NetBox spoke not connected."** Verify the NetBox URL and API Token under Setup → IPAM first — a wrong token or URL leaves the spoke unable to reach NetBox. If those look right, the `netbox` role/spoke itself may not be connected to the hub (check spoke/agent status in Setup).
- **KEA DHCP sync isn't working / DHCP scopes aren't appearing.** Check `KEA_CTRL_URL` (or the `kea_ctrl_url` pushed via config) — the Kea Control Agent must be on port **8760**, not 8000, whenever it's co-located with a hub/NetBox box, since the hub itself can own port 8000 there; Kea trying to bind or being reached on 8000 causes the sync loop's POST to fail (commonly surfacing as a 405).
- **The same device shows up twice, or a device's IP moved and it created a duplicate.** Device sync matching is tiered: it tries to match an existing record by **IP** first, then by **MAC address**, then by **bare hostname** only (never on a fully-qualified name, to avoid merging unrelated hosts that happen to share a short name). If a genuine duplicate appears, it usually means none of those three matched (e.g. both IP and MAC changed at once) — a follow-up sync pass with an updated identifier (MAC or IP) usually reconciles it.
- **Why did a device/VM go offline or get deleted on its own?** The staleness sweep: not seen for the configured `stale_days` (default 7) marks it offline; still offline after `delete_days` (default 30) deletes it and frees its IPs. Only records a sync previously touched (they carry a `last_seen` custom field) are eligible — anything you entered by hand and that no sync ever touched is never swept.
- **Should the discovery feed overwrite what's already in NetBox, or just fill gaps?** That's the `source_of_truth` setting per sync: `"external"` lets the discovery feed (Proxmox, firewall, switch) overwrite/rename; `"netbox"` treats NetBox's existing data as authoritative and only adds what's missing. Check the relevant sync's source-of-truth selector in the WebUI if data isn't updating the way you expect.
- **Hypervisor VM interfaces 404 / "vminterfaces not found."** This is an internal detail (the module uses the `nb.virtualization.interfaces` endpoint, not `vminterfaces`) rather than something to fix from the WebUI — if you see a raw 404 referencing `vminterfaces`, it indicates the module needs updating, not a NetBox configuration problem.

## Related pages

[architecture-topology.md](architecture-topology.md), [opnsense.md](opnsense.md), [nw.md](nw.md), [cppm.md](cppm.md), [pxmx.md](pxmx.md), [install-flags.md](install-flags.md).
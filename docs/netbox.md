---
summary: "NetBox spoke. Repo: netbox. moduletype = 'ipam'. See architecture-topology.md."
keywords: [backends, custom_fields_spec, decommissioned_at, download, kea_ctrl_url, last_seen, lm, netbox, rack, source_of_truth]
---

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

> **Primarily a role now.** The netbox spoke runs mainly as the **`netbox`** role hosted by the agent (`agent-<hostname>`, unit `lm-agent`): the agent opens a sub-spoke `{agent}-netbox` (module_type `ipam`, parent-auto-approved) and self-installs it via `agent/src/agent_spoke.py::_install_role` (clones `lbockenstedt/netbox.git` + deps). The dedicated `lm-netbox.service` / `install.sh` `netbox-spoke-1` path is the **legacy/standalone** alternative; the NetBox *application* (gunicorn/nginx full-app) remains a separate install either way. Connection config (NetBox URL/token) comes from the hub push (WebUI), not a per-module `.env`.

## Ports / backends

- NetBox REST via `pynetbox` (`NetboxEngine`). Default `http://localhost:8000`; gunicorn `127.0.0.1:8001` behind nginx :80 in full-app mode.
- Kea Control Agent REST (`subnet4-add`) at `KEA_CTRL_URL` (default `http://localhost:8000`; `install_kea.sh` pins to **8760**).
- Serves no port itself.

## Environment variables

`NETBOX_URL` (`http://localhost:8000`), `NETBOX_API_TOKEN` (required), `KEA_CTRL_URL` (`http://localhost:8000` — override to 8760 on hub-colocated), `SPOKE_SECRET`, `HUB_SECRET`. `_persist_env()` writes `NETBOX_API_TOKEN`/`NETBOX_URL` back to `.env` on `UPDATE_CONFIG`.

## Install flags

`install.sh`: `--hub`, `--id`/`--name`, `--secret`, `--hub-secret`, `--netbox-url`, `--netbox-token`, `--db-pass`, `--superuser`, `--superpass`, `--supermail`, `--netbox-version`, `--spoke-only` (skip full app — wire up the LM spoke only), `--infra-only` (inverse of `--spoke-only`: install the NetBox *application* — Postgres/Redis/gunicorn/nginx — but SKIP the LM spoke unit; used by the agent's `netbox-server` deploy role, where the separate `netbox` IPAM role sub-spoke is the module that talks to it; **mutually exclusive** with `--spoke-only`), `--all-prereqs` (no-op), `--admin-token` (deprecated). Installs NetBox v4.2+, provisions custom fields via REST from `custom_fields_spec.py`, injects `CUSTOM_VALIDATORS` (`ProxmoxRangeValidator`, `ProxmoxVmidInRangeValidator`), registers the API token with the hub `POST /setup/netbox-config`.

## Key commands / handlers (`netbox_spoke.handle_command`, via `_run_sync` thread pool)

DCIM/IPAM: `NETBOX_HEALTH`, `NETBOX_GET_SITES`, `NETBOX_GET/ADD/UPDATE/DELETE_RACK`, `NETBOX_GET/ADD/CLAIM/DELETE/UPDATE_DEVICE`, `NETBOX_GET_DEVICE_FORM_OPTIONS`, `NETBOX_GET_PREFIXES`, `NETBOX_ALLOCATE_PREFIX`, `NETBOX_FIND_AVAILABLE_PREFIXES`, `NETBOX_CLAIM/UPDATE/DELETE_PREFIX`, `NETBOX_GET_IPS`, `NETBOX_COUNT` (a tenant's device/prefix/IP counts from NetBox's own `count`, for the admin overview), `NETBOX_ALLOCATE/RELEASE/UPDATE_IP(_ADDR)`, `NETBOX_DOC_VM`, `NETBOX_GET_TENANTS`, `NETBOX_SEARCH`. Excel rack-layout import: `NETBOX_IMPORT_RACK_DETECT` (upload → detect rack sheets + guess column maps; pure parse, no writes) and `NETBOX_IMPORT_RACK_COMMIT` (re-parse selected sheets with the user's maps → create/update racks + devices + mgmt interface/IP). Both `_run_sync`; both fetch the workbook from a hub-relayed `download_url` + token (`_fetch_upload_bytes`); `COMMIT` is a `_PICKLIST_MUTATIONS` member (it mutates racks/devices).

Sync family:
- `NETBOX_SYNC_DHCP` — background KEA sync loop (`_kea_sync_loop`, 300s): `get_dhcp_prefixes` → POST `subnet4-add` per scope with derived pool range + routers option.
- `NETBOX_SYNC_VMS` — `sync_vms(vms, tenant_slug, replace, source_of_truth)`: Proxmox→NetBox VM sync; matches by `proxmox_unique_id` cf; tag-driven tenant attribution; `replace=True` deletes proxmox-sourced VMs absent from the pull; `source_of_truth` `external` (Proxmox overwrites) or `netbox` (only-add-missing).
- `NETBOX_SYNC_DEVICES` — `sync_devices(devices, tenant_slug, replace, defaults, source, source_of_truth)`: discovery-source (opnsense DHCP/ARP or nw ARP) → DCIM device + IP upsert; `source` stamps `discovered_from` and scopes replace-delete; a device stamped with `last_seen` is never replace-deleted, it ages out via the staleness sweep (offline at 7 days, deleted 30 days later).
- `NETBOX_SYNC_NW_DEVICE` — `sync_nw_device(device, interfaces, …)`: per-switch/gateway POLL NOW upsert into `dcim.device` + `dcim.interfaces` + per-interface IPs; matches by `nw_device_id` cf; marks interfaces `nw_managed`. Matching reads the spoke's device cache (see **Device cache** below), not NetBox; only a box the cache doesn't know is looked up by serial / MAC / name before it is created. A cached device NetBox no longer has (deleted in the UI) is created again on its next push.
- `NETBOX_SYNC_ACCESS_TRACKER` — `sync_access_tracker(...)`: ClearPass sessions → only-add-missing (replace always False): device per MAC, NIC interface, framed IP, cable to a switch port interface.
- `NETBOX_SYNC_LLDP` — `sync_lldp_links(links, tenant_slug)`: hub topology links → cables via `_cable_devices` (never overwrites a human's cable). Matches ends against the device cache (an unknown end is looked up by MAC / name before it is created) and lists each device's interfaces once per push, so an unchanged link costs no per-port requests. A missing cached port is checked by device ID and name before creation; a concurrent interface-create conflict is recovered by re-reading that exact port and checking its cable. Newly resolved ports stay in the batch index even when cabling is skipped or fails.
- `NETBOX_RESERVE_STATIC_HOSTS` — `netbox_static_reservations.reserve_static_hosts(engine, data, …)`: the static-hosts → DHCP reservations pass (lm `docs/dhcp.md`). The hub relays per tenant the raw agent `GET_HOST_ADDRESSING` rows, sweep/ARP observations and a strict Kea lease/subnet read; this spoke reads the tenant's prefixes/IPs, plans (`plan_static_reservations`; conflicts are reported, never overwritten) and writes `mac_address` + `dhcp_reservation` as exact compare-and-sets, reporting each as `{before, after}`. An IP changed or deleted by another writer since the read is a conflict; a write NetBox refuses rolls back the pass's earlier writes; `{"rollback": changes}` undoes a pass where NetBox still holds `after` and reports later edits it left alone as `rollback_kept`. At most `max_writes` (25) per pass; a failed address waits an hour. Runs as a background command (paced) and drops the list cache after a write.
- `NETBOX_STALENESS_SWEEP` — `staleness_sweep(stale_days=7, delete_days=30)`: not seen `stale_days` → offline + `decommissioned_at`; offline + aged `delete_days` → deleted (IPs freed).
- `NETBOX_TENANT_VMID_RANGE` — tenant `vmid_start`/`vmid_end` cf + in-use `proxmox_vmid` for hub VMID auto-allocation.
- `NETBOX_PROVISION_CUSTOM_FIELDS` — `force=True` re-run of `_ensure_custom_fields` (WebUI "Apply schema changes" button).
- `NETBOX_GET_TENANT_GROUPS` — `get_tenant_groups()`: every NetBox tenant group with its **transitive** member tenants (a group owns its own tenants plus those of all descendant groups). Feeds the hub's tenant-group pseudo-tenants; see below.

**Device cache** (`src/netbox_device_cache.py`). Paging every NetBox device to answer "does this box exist?" was about 60% of NetBox's load, so the spoke keeps its own copy and treats NetBox as a write target for the discovery feeds:
- *Layout.* A slim identity index in memory (id, tenant, name, serial, role slug, primary IPv4, last_updated and the `mac_address` / `nw_device_id` / `discovered_from` custom fields). Full rows on disk, one JSON file per tenant: `/var/lib/lm/netbox/devices/<tenant-slug>.json` (`_none.json` for no tenant; `LM_NETBOX_DEVICE_CACHE_DIR` overrides the directory), keeping id, name, serial, role, device_type, status, site, tenant, rack, primary_ip4, url, last_updated and custom_fields. Every file is written atomically (temp file, then rename).
- *Following writes.* A `requests` response hook on the engine's NetBox session applies every device create, update (including bulk PATCH), delete and single-device read the spoke makes. Nothing polls NetBox.
- *Rebuild.* NetBox is paged in full only when `_meta.json` or a tenant file is missing or unreadable, from an older format (including the single `device_cache.json` of earlier releases, removed on rebuild), or for another NetBox URL. That is one walk.
- *Who reads it.* `NETBOX_SYNC_NW_DEVICE`, `NETBOX_SYNC_DEVICES` (including `replace=True` deletes of vanished owned devices, decided from the cache), `NETBOX_SYNC_LLDP`, `NETBOX_SYNC_ACCESS_TRACKER` and `NETBOX_LIST_STUBS`. Targeted writers check NetBox with filtered lookups before creating: an unknown network device or LLDP neighbour, and console `SYNC_DEVICES` batches (`source` `Console`, up to 25 records) by `serial`, `cf_mac_address` and `name__ie`. Hub reads (`NETBOX_GET_DEVICES`, search, counts), staleness, dedupe and auto-rack still read NetBox directly.
- *Daily reconcile* (`reconcile_device_cache`). The NW flush loop reconciles at most one due tenant per tick; each tenant is due once a day at its own slug-derived time, so the work is spread out. It lists that tenant's devices once (`tenant_id`), stores NetBox's copy, and then, for **scanner-owned** devices only (`discovered_from` = `Network Devices` or `LLDP`):
  - puts back the **scanner-owned fields**, `name`, `serial` and the custom fields `nw_device_id`, `mac_address` and `discovered_from`, when they were changed in NetBox, in one bulk PATCH. A device is reverted only when both the cache and NetBox say it is scanner-owned;
  - recreates a device deleted in NetBox from its cached row (type, role, site, tenant, status, serial and those custom fields; interfaces and IPs return on its next poll). A device that only moved tenant is re-filed, not recreated.
  Every revert or recreate is logged (device id and field names) and appended with before/after values to `_reconcile.jsonl` in the cache directory (rolled to `.1` past 1 MB). Devices that are not scanner-owned (human-made, or from other feeds) are never written by the reconcile; the cache takes NetBox's copy. One of those deleted in NetBox drops out of the cache and its feed creates it again on its next sync.
- *Known gap.* NetBox clears a device's `primary_ip4` server-side when the IP is reassigned or deleted elsewhere; the cached value can lag until that tenant's next reconcile.

Auto-rack: `NETBOX_AUTORACK_SWEEP` (dry-run / apply / cached report of the rack-from-name sweep), `NETBOX_NW_DEVICE` (stores a polled device in the on-disk NW inventory; applied by the scheduled flush), `NETBOX_LIST_STUBS` / `NETBOX_MERGE_STUB` (duplicate-stub merge, see below; `NETBOX_MERGE_STUB` is a `_PICKLIST_MUTATIONS` member).

Plus `GET_VERSION`, `UPDATE_CONFIG`, `SPOKE_UPDATE`.

## Key files

`src/netbox_spoke.py` (dispatch, KEA loop, `_persist_env`), `src/netbox_engine.py` (~154-line composition class `NetboxEngine(DcimMixin, IpamMixin, VmSyncMixin, ChangelogMixin, SyncMixin, StalenessMixin, TenancyMixin)` — the work lives in the mixin modules: `netbox_dcim.py`, `netbox_ipam.py`, `netbox_vmsync.py` (also `_ensure_custom_fields`), `netbox_sync.py` (the largest — `sync_devices`/`sync_vms`/`sync_nw_device`/`sync_access_tracker`/`_journal`), `netbox_staleness.py` (`staleness_sweep`), `netbox_changelog.py`, `netbox_tenancy.py`), `src/netbox_static_reservations.py` (static hosts → DHCP reservations: planner, compare-and-set writes, rollback), `src/netbox_autorack.py` (`parse_rack_name`, pure `plan()`, `resolve_rack`, the sweep), `src/netbox_xlsx.py` (pure-parse Excel rack-layout detection — `detect_rack_sheets` for both sheet shapes + `parse_one_rack_sheet`/`parse_summary_block_by_name` commit-time re-parse; lazy openpyxl), `src/custom_fields_spec.py` (pure-data `CUSTOM_FIELDS_SPEC` — single source of truth for the engine self-heal, the Django shell block, and the REST provisioning block), `src/control_plane.py`, `install.sh`, `install_kea.sh`, `API_SPEC.md`.

## Notable behaviors & gotchas

- **Tenant groups are selectable like tenants.** A NetBox tenant *group* is synced into hub state as a pseudo-tenant keyed `group:<slug>` (`is_tenant_group`, `member_tenant_slugs`), so it appears in the header tenant picker with a **Group** badge and a tooltip listing its members. Selecting it shows the union of every member tenant's data — equivalent to selecting all of them at once — which lets a user be assigned either a single tenant *or* a whole group. Membership rolls **upward**: a parent group includes the tenants of all descendant groups.
  - Reads scope via NetBox's own `?tenant_group=` filter (tree-aware, resolved server-side) — the hub sends **either** `tenant` **or** `tenant_group`, never both. It deliberately does **not** expand a group into a list of tenant slugs: NetBox rejects the *entire* query with a 400 if any one slug is unknown.
  - `member_tenant_slugs` is used only for **write authorization** (a group-assigned user may create into any member tenant, never into the group itself) and non-NetBox scoping — never as the read filter.
  - The `group:` prefix guarantees a group can never collide with a real tenant slug, including in the per-tenant warm-cache keys.
  - A group whose members can't be resolved degrades to unscoped rather than erroring, and a spoke that doesn't yet implement `NETBOX_GET_TENANT_GROUPS` leaves existing group records untouched (a mid-upgrade spoke can't strip a user's assigned group).

- **Custom fields self-heal at startup** (`_ensure_custom_fields()` in `netbox_vmsync.py`'s `VmSyncMixin`, called from the spoke at boot/reconnect, best-effort/WARNING-logged so a restricted token never breaks the spoke — failures surface at WARNING, not DEBUG, so they're visible). Spec: `proxmox_unique_id`, `proxmox_vmid`, `proxmox_node`, `proxmox_type`, `proxmox_labels`, `discovered_from`, `nw_device_id`, `nw_managed`, `mac_address` (on `ipam.ipaddress` + `dcim.device`), `switch_name`/`switch_ip`/`switch_port`, `last_seen`, `decommissioned_at`, `vmid_start`/`vmid_end` (on `tenancy.tenant`). CFs-not-attached is a stale-deploy gap (rerun `install.sh` / Apply-schema), not a spec gap.
- **Prefix sites place devices.** NetBox → Prefixes → Edit/Allocate Subnet has a **Site** dropdown listing the sites in NetBox (stored as the prefix's native scope; NetBox ≥ 4.2). Changing a prefix's site also applies it to every child prefix nested inside it (same VRF); if the children can't be updated the parent change is rolled back. Scanned/polled devices are created in the site of the most specific site-scoped prefix containing their IP, and devices still in the fallback `discovered` site are moved there; a device already in another site is left alone. The site shows in NW → Fleet (Site column) and device detail. The `discovered` site is provisioned with the custom fields (install / self-heal / Apply schema); prefix→site assignments themselves are NetBox data — back up the NetBox DB to keep them across a reinstall.
- **pynetbox auth pin** — `_apply_auth()` sets `Authorization: Token …` on `http_session.headers` because `_api_get()` calls `http_session.get()` directly (bypassing pynetbox's per-request wrapper) and would otherwise go out unauthenticated → 403.
- **HTTP semaphore** — module-level `_netbox_http_sem = threading.Semaphore(2)` plus `_netbox_bulk_sem` (1, whole-fleet listings) cap the spoke at NetBox's 3 gunicorn workers, so a paged IP/prefix walk can't block other reads and workers aren't overloaded. `_api_get_all` paginates via `next` links; `_api_get` is single-page and would silently truncate.
- **Hypervisor vminterface endpoint** — accessor is `nb.virtualization.interfaces` (REST endpoint `interfaces`), NOT `nb.virtualization.vminterfaces` (404s). Name collision: model string is `virtualization.vminterface`.
- **sync_vms primary_ip4** — `_assign_vm_primary_ip4` PATCHes only `primary_ip4` via `virtual_machines.update`; a full `obj.save` re-sends unattached custom_fields → 400 "does not exist for this object type". Bare-return→`(0,None)` contract.
- **sync_devices fresh-fetch** — create branch re-sent unprovisioned custom_fields on primary_ip4 save → 400 killed the upsert; fixed by fresh `devices.get` + best-effort save so a missing cf degrades to "field not set".
- **Kea port trap** — KEA CA must be 8760, not 8000 (the unified hub owns :443, but NetBox/the legacy webui-spoke can occupy :8000 on a co-located box); KEA on 8000 fails to bind there and the sync loop POSTs the hub → 405.
- **Custom validators** — `ProxmoxRangeValidator` (tenant ranges non-overlapping) + `ProxmoxVmidInRangeValidator` (VM VMID within tenant range), lenient when a range is unset. NetBox v4.2+.

## How it works

**Command path.** Whether it runs standalone (`lm-netbox.service`) or as the `netbox` role on an agent, this module is a WebSocket spoke that dials the hub over `/ws/spoke`. WebUI pages under IPAM never talk to NetBox directly: an action becomes a JSON command (the `NETBOX_*` names listed under "Key commands" above), sent hub → spoke, dispatched by `netbox_spoke.handle_command`, and executed against the NetBox REST API through `pynetbox`/`NetboxEngine` — most calls run in a thread pool (`_run_sync`) so a slow NetBox request never blocks the spoke's event loop.

**How the connection gets configured.** The spoke connects to `NETBOX_URL`/`NETBOX_API_TOKEN` from its environment at boot (or NetBox is provisioned locally by `install.sh`), but from then on the connection is **hub-managed**: saving NetBox URL/token in the WebUI's **Setup → IPAM** page sends an `UPDATE_CONFIG` command that calls `engine.reconnect(url, token)` and re-runs the custom-field self-heal — it is not something you hand-edit in a per-module `.env` on an ongoing basis (though the spoke does write the new value back to its local `.env` via `_persist_env` so a restart picks up the same config). The one exception is the KEA Control Agent URL (`kea_ctrl_url`), which can also be pushed the same way.

**Custom fields — why syncs sometimes need "provisioning".** Every discovery sync writes to NetBox custom fields (things like `proxmox_unique_id`, `proxmox_vmid`, `discovered_from`, `nw_device_id`, `mac_address`, `last_seen`, `decommissioned_at`, tenant `vmid_start`/`vmid_end`, etc. — the full list lives in `custom_fields_spec.py`). The spoke self-heals these at startup (best-effort — a restricted API token just logs a WARNING line rather than crashing the spoke), but a NetBox instance that was never provisioned, or that fell behind after a schema change, will reject a sync with a 400 error until the fields exist. The WebUI's "Apply schema changes" button (Setup → IPAM → edit the NetBox instance) calls this same provisioning logic with `force=True` and is always safe to re-run — it only adds what's missing.

**Data flow in.** Every other module hands NetBox data the same general way: the hub collects records from a discovery source (opnsense DHCP/ARP, nw switch ARP/MAC, pxmx VM inventories, cppm NAC sessions) and relays them here as one of the `NETBOX_SYNC_*` commands. Each sync family has its own matching/replace rules (see "Notable behaviors" below and each sync's description in "Key commands"), but the shared idea is: match an existing record first (by a stable identifier like a Proxmox unique ID, a MAC address, or an IP), only create a new one if nothing matches, and only delete records the same source previously created if that source's `replace=True` pass no longer sees them.

**Source of truth.** Most syncs accept a `source_of_truth` of `"external"` (the discovery feed is authoritative — it can overwrite fields and rename things) or `"netbox"` (NetBox's existing data wins — the sync only fills in what's missing, never overwrites). This is set per-sync from the hub's configuration (source-of-truth selector in the WebUI), not hard-coded in this module.

**Staleness sweep.** A background/scheduled job (`NETBOX_STALENESS_SWEEP`, hub-triggered) ages out anything a sync previously touched (identified by having a `last_seen` custom field — hand-entered inventory has none and is never swept): not seen for `stale_days` (default **7**) → marked offline + `decommissioned_at` stamped; still offline after `delete_days` (default **30**) → deleted outright, which frees any IP addresses it held. This is why a device that unplugged a week ago shows "offline" rather than vanishing immediately, and why it eventually disappears entirely after a month of continued absence.

**KEA DHCP sync.** Independently of the discovery syncs, a background loop (`_kea_sync_loop`, every **300s**) reads DHCP-eligible prefixes out of NetBox and pushes them to a KEA Control Agent as `subnet4-add` calls (with a derived pool range and the prefix's gateway as the `routers` option) — this is how a prefix you allocate in IPAM becomes an actual DHCP scope.

## How to use it

- **Point Lab Manager at a NetBox instance.** If NetBox is being installed fresh, `install.sh` provisions it and registers the token with the hub automatically. To point at an existing/external NetBox, or to change the URL/token later, go to **Setup → IPAM**, enter the **NetBox URL** and **API Token**, and save — the hub pushes it to the spoke immediately.
- **Supply the API token from the Credential Vault (optional).** Instead of typing the token inline, pick a Credential Vault secret on the IPAM instance so the plaintext never lands in `global_config`. Store the token as a **Token** or **API key** secret (automation-readable) in the vault, then select it under **Setup → IPAM → edit the instance → Credential Vault**. At push time the hub overlays the resolved token onto the `api_token` field before sending the config to the spoke; the URL and TLS-verify setting stay inline (they are not secrets). This mirrors the ClearPass vault option (see `docs/cppm.md`).
- **Provision or refresh the custom fields NetBox needs.** Setup → IPAM → edit the NetBox instance → **Apply schema changes**. Safe to click any time (it's idempotent); do this after connecting a pre-existing NetBox for the first time, or after upgrading Lab Manager if a sync starts complaining about a missing field.
- **Let discovery fill in devices/VMs for you.** You generally don't add devices/VMs by hand for anything that's already visible to the firewall, a switch, Proxmox, or NAC — those show up in IPAM on their own via the sync loops described above. Manual add/claim (`NETBOX_ADD_DEVICE`/`NETBOX_CLAIM_DEVICE`, etc.) is for inventory nothing else can see yet.
- **Create a rack by hand when discovery can't.** IPAM → Racks → **Add Rack**. The **Site** field is a dropdown populated from the real NetBox sites (`GET /api/netbox/sites`) rather than a free-text slug — pick the site, set the U-height, and save. (`add_rack` takes a site **slug**, so the option values are slugs; on edit the site is immutable and shown disabled, since `update_rack` doesn't take it. If the sites fetch fails the field falls back to a free-text slug input so the form still works offline.) The **current tenant** (header picker) is stamped onto the new rack, so create it while viewing the tenant it belongs to — see the troubleshooting note below if a rack doesn't show up in the tenant view.
- **Allocate a prefix or IP.** IPAM → Prefixes/IPs → allocate; you can search for available space within a parent block or claim a specific prefix/IP directly.
- **Force a staleness sweep or check why something is offline.** The staleness sweep normally runs on the hub's own schedule; if you need to check sooner, look at a device/VM's `last_seen` value — anything older than the configured stale-days threshold will show (or shortly become) offline.
- **Turn on "Sign in with Microsoft" for the NetBox web UI.** Settings → Azure → **NetBox SSO** → pick the **NetBox host** (the generic agent on the NetBox server; the list shows connected, approved agents that report a NetBox install) → tick **Enable** → **Save & Apply**. NetBox reuses the hub's own Entra connection (Settings → Azure → SSO must be configured first): tenant + client ID, the hub's **client certificate** (no client secret — NetBox authenticates with a cert-signed client assertion), the hub's allowed group (unless overridden), the Permission Groups' Entra group → NetBox group map (unless one is entered), and Permission Groups granting Global Admin → NetBox **superuser**. Because the push carries the hub's Entra private key, it goes only to that explicitly picked agent, re-verified (connected, approved, reports NetBox) on every enable — never to an agent that merely self-advertises a capability. Picking a different host strips SSO and the key from the previous one. The hub sends it over the encrypted `NETBOX_APPLY_SSO` frame; the agent installs `lm_sso_backend.py` + the cert (`/etc/netbox-lm-sso/`, 0600, NetBox user) and writes the LM SSO block into `configuration.py` (the same sentinels `install.sh --netbox-sso-*` uses), which labels the login button **Sign in with Microsoft**, then restarts NetBox. The one Azure-side step: add `https://<netbox-host>/oauth/complete/oidc/` as a **Web redirect URI** on the hub's app registration and enter the same URI in the tile. **Test login** probes NetBox's OIDC start URL and checks it redirects to Entra with the expected tenant/client/redirect. Unticking **Enable** removes the block and the cert. Local NetBox logins keep working as break-glass. A client secret can still be pasted if the hub has no certificate.

## Seed device catalog

Populating NetBox by hand is tedious: every switch model needs a manufacturer, a device type, and a set of interface/console/power-port templates before you can instantiate devices and get their 24/48 ports + SFP+ uplinks. **Setup → Module Management → "Seed catalog"** (Global-Admin only) loads a bundled **Aruba / HPE / Juniper** device-type catalog — manufacturers, device types, and interface/console/power templates — into NetBox in one action. It runs on the spoke (`NETBOX_SEED_CATALOG` → `NetboxEngine.seed_catalog()`, thread-offloaded) reusing the spoke's existing `NETBOX_URL`/`NETBOX_API_TOKEN`, then refreshes the device picklists.

- **Scope:** device **types + templates only**. No device instances, sites, racks, prefixes, or IPs are created — you still place devices via the IPAM UI as normal. Instantiating a device from a seeded type auto-creates its interfaces. This catalog is also what the **Excel rack-layout importer** (below) resolves model→slug against — seeding it first is a prerequisite for a clean import, not just a convenience for hand-added devices.
- **Idempotent (safe to re-run):** manufacturers and device types are get-or-created by slug; an existing device type has its scalar fields (`u_height`, `is_full_depth`, `comments`) upserted and `.save()`d rather than erroring. Templates are **add-missing** — existing templates (including any you added by hand) are never deleted or re-typed. So the intended "edit the catalog, re-run" workflow works for *adding* models/ports; a rename or port-type *change* on an existing name is a delete-that-device-type + re-seed (re-create), not an automatic migration (avoids clobbering hand-added ports / breaking instantiated devices).
- **Access:** Global-Admin only — the WebUI card is hidden for non-admins (`isAdmin()`), and `POST /api/netbox/seed-catalog` returns **403** for non-admin sessions. Defense in depth.
- **Catalog location / extending it:** the catalog is `netbox/src/seed_catalog.json` (compact schema: per model, a `ports` spec expanded into named templates, plus `mgmt`/`console`/`power`). To add or amend a model, edit that JSON on the spoke repo, redeploy the spoke (WebUI Update), and click Seed catalog again. The toast reports manufacturers created, device types created/updated, templates added, and any per-model errors (one bad model doesn't abort the rest).

## Import rack layout from Excel

Recreating a lab's racks + devices by hand is infeasible, and a rigid fixed-schema parser breaks on real workbook drift (columns shuffle between sheets; some racks carry a `Switch Port` column others don't; whole-lab summary sheets use a different shape entirely). **IPAM → Racks → "Import .xlsx"** (Global-Admin only) is a **dynamic**, two-step importer:

1. **Upload + detect** — `POST /api/netbox/racks/import-xlsx` (multipart `.xlsx`). The hub saves the file to a scratch dir (`/var/lib/lm/imports/<uuid>.xlsx`), mints a one-time download token, and relays `NETBOX_IMPORT_RACK_DETECT {download_url, token}` to the spoke. The spoke HTTP-GETs the file, parses it with **openpyxl**, auto-detects rack sheets (both shapes), **guesses a column→field map**, and returns a JSON preview. The hub also returns device form-options (sites/device-types/roles/tenants) so the WebUI can render the mapping UI in one round trip. This mirrors the template-refresh URL relay (the file is NOT inlined over the WS — 16 MiB frame cap).
2. **Map + commit** — the WebUI shows one collapsible panel per detected rack (editable rack name, site override, U-height, and a column-mapping table pre-filled from the guess). Pick racks + fix mappings + optional **Dry run**, then `POST /api/netbox/racks/import-commit {upload_id, sheets, dry_run}`. The spoke re-GETs the file, re-parses the **selected** sheets with the user's maps (full device rows — detect only returned previews), and creates/updates racks + devices + mgmt interface + IP via `NetboxEngine.import_rack_layout`. The hub deletes the scratch file after.

**Two sheet shapes** (`netbox_xlsx.detect_rack_sheets`):
- **one-rack-per-sheet** (the common, high-value shape) — a header row (`RU`, `F/R`, `Type of device`, `Hostname`, `Serial Number`, `Part Number`, `MAC Address`, `MGMT IP`, `Function`, `Status`…) followed by one row per device slot. The user maps columns → NetBox fields.
- **multi-rack summary** (whole-lab elevation grid) — rack-blocks stacked vertically, each introduced by a `RACK <name>` cell with `RU`/`Front`/`Rear` columns; each non-empty Front/Rear cell at a RU becomes a front/rear-face device named by its text (no model/serial/IP). No column mapping needed; deselect if noisy. (v1 handles vertically-stacked blocks; side-by-side rack groups in one row band aren't parsed — deselect those.)

**Create behavior** (`DcimMixin.import_rack_layout`, idempotent, per-device error isolation — one bad row never aborts the whole import):
- **Racks** get-or-create by `name` within `site`; `u_height` + `tenant` upserted (reuses the tenant-aware `add_rack`/`update_rack`).
- **Devices** placed at `position` = RU (1:1 for 1U; RU 0 → 0U, position unset) with `face` F→front / R→rear; stamped with `tenant`, `serial`, `asset_tag`. Idempotent match by **serial** (global) else by **name** within the rack — re-import updates, never duplicates.
- **device_type** resolved by `_resolve_device_type_slug(model)`: exact catalog match → **stem + port-hint** match (e.g. `"6300M 24SR5 CL6"` → `6300m-24g`; `"CX8325-32 (F2B)"` → `8325-32c`; `"3810M"` with no port hint → first variant, flagged **ambiguous**) → live `device_types.get(model=…)` → live `get(slug=…)`. **Unresolved = a per-device error (device skipped); it never auto-creates a junk type.** Tie-in: **seed/extend the catalog first** (`netbox/src/seed_catalog.json`) for the models you import — the resolver is built on it.
- **mgmt interface + IP** — if `mgmt_ip` is mapped, mirrors `claim_device`: reuses/creates an `mgmt` interface, attaches the IP (mask from the most-specific containing prefix, `/32` fallback) with `dns_name` = hostname, sets `primary_ip4`, best-effort stamps `mac_address` cf.
- **Dry run** resolves everything (sites/tenants/types/racks/devices) but skips every create/save — returns would-be counts.

**Access:** Global-Admin only — the WebUI button is hidden for non-admins (`isAdmin()`), and both POST routes return **403** for non-admin sessions. Defense in depth. The spoke's token-gated `GET /api/netbox/racks/import-xlsx/{upload_id}` download is session-exempt (the spoke has no browser session) — gated by the one-time bearer token inside the route, like the template-refresh download. **Tenant:** the current tenant (header picker) is stamped on every rack + device; `default` = global. **Dep:** `openpyxl` on the spoke (`netbox/requirements.txt`); a missing dep degrades to a clear ERROR, not a spoke crash.

## Auto-rack (rack placement from the device name)

Devices are racked in NetBox automatically from their **name** plus the tenant
they were found under, so almost nothing is configured by hand in LM.

**Name grammar** (domain suffix ignored; `netbox_autorack.parse_rack_name`):

```
<SITE3><NET>-<TENANT>-<RACK>-R<RU>[R][H]-<DEVICE>
MIPBE-SSPLM-N31-R45-VPNC1        rack N31, RU 45, front
MIPBE-SSPLM-N31-R45R-VPNC1       rack N31, RU 45, rear (reverse mounted)
MIPBE-SSPLM-PXMX01-N31-R18-ILO   device-before-rack form is accepted too
MIPBE-SSPLM-N31-R43H-GW01        half width: GW01 left, GW02 right (see below)
```

`SITE3` is the 3-letter site code, `NET` 0-3 letters of network role (e.g. `BE`
backend), `TENANT` the tenant group. A trailing `R` on the RU means **rear**.

- **Height** comes from the device type in NetBox. If the type has no RU height,
  the hub asks the LLM for the model's height (`device_specs`) and records it.
  An iLO/iDRAC/BMC reports its host server model via SNMP so the host is sized
  from that.
- **Servers** take their slot from their management controller's name
  (iLO/iDRAC/BMC, linked on the server as `linked_device` once matched by
  serial). If the two names carry different slots the server is reported as
  unable to rack.
- **Conflicts** (`plan()` is pure): a holder that is moving elsewhere moves out
  first, an *offline* holder is unracked (replaced), and only an *online* holder
  still named for the slot is a real conflict. Chains are capped so swaps and
  cycles are reported, not looped. A slot held by a device's own management
  controller is released (it sits inside the server). A controller finds its
  server by its own link, or else by the server's link to it (the controller's
  link may hold the host's agent hostname rather than its NetBox name).
- **Half-width chassis** (two per U, e.g. Aruba CX 9004): a device type is
  half-width when its comments say `half-width` or the model is a known one
  (9004), **or** the name carries `H` after the RU (`R43H`). Devices naming the
  same rack and RU are ordered by the number in their name: the **lower-numbered
  device is on the left**, the next on the right. NetBox cannot hold two devices
  on one face of a U, so they are stored as non-full-depth chassis on the front
  and rear face of the same U (the type is switched to non-full-depth on first
  use); the elevation draws both side by side on the **Front** view only. The
  `R` rear flag is not combined with `H`.
- **Rename** keeps history: a device renamed on the hub (`reconcile_nw_device_name`)
  is matched in NetBox by serial / MAC / `nw_device_id` / IP and renamed **in
  place** (`_adopt_real_name`), so its changelog is preserved.
- **Schedule.** The hub pushes each polled device with `NETBOX_NW_DEVICE`; the
  spoke keeps the inventory on disk and applies changes every
  `LM_NETBOX_NW_FLUSH_S` seconds (default 900), **rack-named devices first and
  racked immediately**, then runs an autorack sweep. Device polls run every 6 h
  by default, so a rename typically shows up racked within one flush after the
  next poll (the first rename test took about 15-45 minutes end to end).
  When the agent reloads the role (every netbox update), it calls
  `stop_background_loops()`, which cancels this flush loop and the Kea sync loop
  so the old code does not keep running next to the new.
- **Report.** `GET /api/netbox/autorack` (tenant-scoped) feeds **NetBox → Rack
  Placement**: conflicts and "cannot auto-rack" entries with tenant / wanted /
  current / mismatch. It serves the cached report (the first sweep runs in the
  background); **Re-check now** forces a dry-run. Admin ops:
  `/admin/ops/netbox-autorack`, hub `/setup/netbox-autorack/run` (dry-run /
  apply / report).

### Duplicate stub devices

Firewall/ARP feeders (e.g. OPNsense) file a switch's extra MACs and IPs as a
separate, serial-less *stub* device. They are folded into the real polled
device two ways; in both the stub's IPs move to an interface `lm-merged` on the
real device, a journal entry records it and the stub is deleted (the real device
keeps its history; the stub's few changelog entries are lost).

1. **Sibling MAC** (`_merge_sibling_stubs`, during `sync_nw_device`): a stub whose
   MAC shares the first 5 octets with the polled base MAC, has no serial (or the
   same one), is not itself a polled device, is in the same tenant (or none) and is the
   same kind (not a management controller). Another tenant's record of the box
   (e.g. its DHCP lease feed) is left alone: deleting it only made that feed
   re-create it every cycle.
2. **Live poll** (`nw_merge_stubs_by_live_poll`, hub): after a successful push the
   hub lists stubs (`NETBOX_LIST_STUBS`: IP, no serial, no `nw_device_id`), takes
   those in the same /16 as the device, probes each stub IP through the device's
   own nw spoke (`NW_GET_DEVICE_INFO` with an `address` override, so the
   device's credentials are tried against that IP) and, when the returned serial
   equals the device's, sends `NETBOX_MERGE_STUB`. Limits: one probe per
   stub/device pair per 24 h and 5 probes per call. A stub that has since gained
   a different serial is never merged.

## Rack elevation view

A NetBox-style front/back rack elevation, opened from the **eye button** on each rack row (IPAM → Racks). `GET /api/netbox/racks/{rack_id}/elevation` relays `NETBOX_GET_RACK_ELEVATION` to the ipam spoke, which calls NetBox's `/api/dcim/racks/{id}/elevation/?face=front|rear` (one entry per RU, top→bottom, multi-U devices occupying consecutive units) plus the rack's device list for 0U / side devices (position null/0). The render model is `{rack:{name,u_height,site,tenant}, faces:{front,rear:[{unit,device}]}, zero_u:[…]}` — each device summary carries the role `color` so the WebUI tints the slot like NetBox; consecutive same-device units are merged into one rowspan cell; 0U/side devices render in a tray below the two grids.

**Look and feel.** Each device is a dark chassis block with a role-colour strip, per-type front-panel artwork (switch ports, drive bays, gateway/firewall ports, patch panel, PDU outlets, BMC, wireless) and its name (wrapped on multi-U devices). Half-width chassis sit side by side in one U (see Auto-rack). **Click a device** to open the same all-module detail as the global search (`/api/device-detail`: IPAM, Zabbix, DNS, DHCP, NAC, NW, Proxmox, LDAP, console; admin only).

**Performance.** The spoke reads the front face, rear face and rack device list concurrently and caches the half-width device-type lookup for 5 minutes.

**Access:** read-only, but non-admins are ownership-gated against their tenant's racks cache (`_verify_owns`, same cross-tenant guard as the rack PUT/DELETE routes) so a user can't enumerate another tenant's rack layout by guessing the sequential `rack_id`; admins bypass.

## Troubleshooting / common questions

- **A sync fails with 400 / "field does not exist for this object type."** The custom fields this sync needs were never provisioned on this NetBox (a stale deploy, or an externally-managed NetBox that was connected without provisioning). Fix: Setup → IPAM → edit the instance → **Apply schema changes**, or rerun `install.sh` on the module side. This is safe and idempotent.
- **The NetBox / IPAM page shows offline or "NetBox spoke not connected."** Verify the NetBox URL and API Token under Setup → IPAM first — a wrong token or URL leaves the spoke unable to reach NetBox. If those look right, the `netbox` role/spoke itself may not be connected to the hub (check spoke/agent status in Setup).
- **KEA DHCP sync isn't working / DHCP scopes aren't appearing.** Check `KEA_CTRL_URL` (or the `kea_ctrl_url` pushed via config) — the Kea Control Agent must be on port **8760**, not 8000, whenever it's co-located with a hub/NetBox box, since the hub itself can own port 8000 there; Kea trying to bind or being reached on 8000 causes the sync loop's POST to fail (commonly surfacing as a 405).
- **The same device shows up twice, or a device's IP moved and it created a duplicate.** Device sync matching is tiered: it tries to match an existing record by **IP** first, then by **MAC address**, then by **bare hostname** only (never on a fully-qualified name, to avoid merging unrelated hosts that happen to share a short name). If a genuine duplicate appears, it usually means none of those three matched (e.g. both IP and MAC changed at once) — a follow-up sync pass with an updated identifier (MAC or IP) usually reconciles it.
- **One switch, several management addresses (stack/VSX in-band + per-member OOBM).** When the hostname comes from an operator assertion — a DNS A record or a DHCP *reservation* — the name means one device. A second address under that name (different MAC and IP) is attached to the existing device as a secondary interface `mgmt-<last 4 MAC hex>` carrying that IP; the device's primary IP, MAC and name are left unchanged. Hostnames that only come from a DHCP *lease* are device-chosen (e.g. every Sonos says `sonoszp`), so a collision there still creates a separate device with a `-<MAC4>` suffix.
- **Why did a device/VM go offline or get deleted on its own?** The staleness sweep: not seen for the configured `stale_days` (default 7) marks it offline; still offline after `delete_days` (default 30) deletes it and frees its IPs. Only records a sync previously touched (they carry a `last_seen` custom field) are eligible — anything you entered by hand and that no sync ever touched is never swept.
- **Should the discovery feed overwrite what's already in NetBox, or just fill gaps?** That's the `source_of_truth` setting per sync: `"external"` lets the discovery feed (Proxmox, firewall, switch) overwrite/rename; `"netbox"` treats NetBox's existing data as authoritative and only adds what's missing. Check the relevant sync's source-of-truth selector in the WebUI if data isn't updating the way you expect.
- **Hypervisor VM interfaces 404 / "vminterfaces not found."** This is an internal detail (the module uses the `nb.virtualization.interfaces` endpoint, not `vminterfaces`) rather than something to fix from the WebUI — if you see a raw 404 referencing `vminterfaces`, it indicates the module needs updating, not a NetBox configuration problem.
- **A rack I created doesn't show up in the tenant view.** The Add/Edit Rack modal stamps the **current tenant** (the one selected in the header picker) onto the rack — if you were viewing the global `default` tenant when you created it, the rack was created with no tenant and won't appear under a specific tenant. Switch to the tenant first, then create the rack. The tenant is resolved by its NetBox **slug** mapping (Setup → Tenants → the tenant's `netbox_tenant_slug`); if that mapping is missing/wrong the create is refused with "NetBox tenant … not found" rather than silently dropping the attribution.

- **Scanned devices land in the `discovered` (or wrong) site.** Give the subnet a site: NetBox → Prefixes → Edit Subnet → **Site**. The next poll/scan moves devices out of `discovered` into that site; a device already placed in a different site is not moved — change it in NetBox.

## Related pages

[architecture-topology.md](architecture-topology.md), [opnsense.md](opnsense.md), [nw.md](nw.md), [cppm.md](cppm.md), [pxmx.md](pxmx.md), [install-flags.md](install-flags.md).
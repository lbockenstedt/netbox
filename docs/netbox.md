# netbox — IPAM/DCIM

NetBox spoke. Repo: `netbox`. `module_type = "ipam"`. See [architecture-topology.md](architecture-topology.md).

## Role & module_type

The IPAM/DCIM source-of-truth spoke. Owns NetBox REST access (sites, racks, devices, prefixes, IPs, VMs, tenants) and a background NetBox→Kea DHCP scope sync. It is the **sink** for every discovery sync (firewall DHCP/ARP via opnsense, switch ARP/MAC via nw, hypervisor VMs via pxmx, NAC sessions via cppm) and where staleness sweeps age objects out. Hub-colocated by default (the hub owns :443 on the same box).

## Entrypoints

- `python3 -m src.control_plane` (`NetboxControlPlane`); spoke `NetboxSpoke(BaseSpoke)`, module name `"netbox"`.
- systemd `lm-netbox.service` (spoke); full-app mode also installs `netbox.service` (gunicorn) + `netbox-rq.service` + nginx, with a self-heal block that restarts gunicorn on 502/000.
- Installers: `install.sh` (spoke + optional full app), `install_kea.sh` (Kea CA pinned to **8760**).

## Ports / backends

- NetBox REST via `pynetbox` (`NetboxEngine`). Default `http://localhost:8000`; gunicorn `127.0.0.1:8001` behind nginx :80 in full-app mode.
- Kea Control Agent REST (`subnet4-add`) at `KEA_CTRL_URL` (default `http://localhost:8000`; `install_kea.sh` pins to **8760**).
- Serves no port itself.

## Environment variables

`NETBOX_URL` (`http://localhost:8000`), `NETBOX_API_TOKEN` (required), `KEA_CTRL_URL` (`http://localhost:8000` — override to 8760 on hub-colocated), `SPOKE_SECRET`, `HUB_SECRET`. `_persist_env()` writes `NETBOX_API_TOKEN`/`NETBOX_URL` back to `.env` on `UPDATE_CONFIG`.

## Install flags

`install.sh`: `--hub`, `--id`/`--name`, `--secret`, `--hub-secret`, `--netbox-url`, `--netbox-token`, `--db-pass`, `--superuser`, `--superpass`, `--supermail`, `--netbox-version`, `--spoke-only` (skip full app), `--all-prereqs` (no-op), `--admin-token` (deprecated). Installs NetBox v4.2+, provisions custom fields via REST from `custom_fields_spec.py`, injects `CUSTOM_VALIDATORS` (`ProxmoxRangeValidator`, `ProxmoxVmidInRangeValidator`), registers the API token with the hub `POST /setup/netbox-config`.

## Key commands / handlers (`netbox_spoke.handle_command`, via `_run_sync` thread pool)

DCIM/IPAM: `NETBOX_HEALTH`, `NETBOX_GET_SITES`, `NETBOX_GET/ADD/UPDATE/DELETE_RACK`, `NETBOX_GET/ADD/CLAIM/DELETE/UPDATE_DEVICE`, `NETBOX_GET_DEVICE_FORM_OPTIONS`, `NETBOX_GET_PREFIXES`, `NETBOX_ALLOCATE_PREFIX`, `NETBOX_FIND_AVAILABLE_PREFIXES`, `NETBOX_CLAIM/UPDATE/DELETE_PREFIX`, `NETBOX_GET_IPS`, `NETBOX_ALLOCATE/RELEASE/UPDATE_IP(_ADDR)`, `NETBOX_DOC_VM`, `NETBOX_GET_TENANTS`, `NETBOX_SEARCH`.

Sync family:
- `NETBOX_SYNC_DHCP` — background KEA sync loop (`_kea_sync_loop`, 300s): `get_dhcp_prefixes` → POST `subnet4-add` per scope with derived pool range + routers option.
- `NETBOX_SYNC_VMS` — `sync_vms(vms, tenant_slug, replace, source_of_truth)`: Proxmox→NetBox VM sync; matches by `proxmox_unique_id` cf; tag-driven tenant attribution; `replace=True` deletes proxmox-sourced VMs absent from the pull; `source_of_truth` `external` (Proxmox overwrites) or `netbox` (only-add-missing).
- `NETBOX_SYNC_DEVICES` — `sync_devices(devices, tenant_slug, replace, defaults, source, source_of_truth)`: discovery-source (opnsense DHCP/ARP or nw ARP) → DCIM device + IP upsert; `source` stamps `discovered_from` and scopes replace-delete.
- `NETBOX_SYNC_NW_DEVICE` — `sync_nw_device(device, interfaces, …)`: per-switch/gateway POLL NOW upsert into `dcim.device` + `dcim.interfaces` + per-interface IPs; matches by `nw_device_id` cf; marks interfaces `nw_managed`.
- `NETBOX_SYNC_ACCESS_TRACKER` — `sync_access_tracker(...)`: ClearPass sessions → only-add-missing (replace always False): device per MAC, NIC interface, framed IP, cable to a switch port interface.
- `NETBOX_STALENESS_SWEEP` — `staleness_sweep(stale_days=7, delete_days=30)`: not seen `stale_days` → offline + `decommissioned_at`; offline + aged `delete_days` → deleted (IPs freed).
- `NETBOX_TENANT_VMID_RANGE` — tenant `vmid_start`/`vmid_end` cf + in-use `proxmox_vmid` for hub VMID auto-allocation.
- `NETBOX_PROVISION_CUSTOM_FIELDS` — `force=True` re-run of `_ensure_custom_fields` (WebUI "Apply schema changes" button).

Plus `GET_VERSION`, `UPDATE_CONFIG`, `SPOKE_UPDATE`.

## Key files

`src/netbox_spoke.py` (dispatch, KEA loop, `_persist_env`), `src/netbox_engine.py` (~3.5k lines — all DCIM/IPAM + sync_* + staleness_sweep + `_ensure_custom_fields` + `_journal`), `src/custom_fields_spec.py` (pure-data `CUSTOM_FIELDS_SPEC` — single source of truth for the engine self-heal, the Django shell block, and the REST provisioning block), `src/control_plane.py`, `install.sh`, `install_kea.sh`, `API_SPEC.md`.

## Notable behaviors & gotchas

- **Custom fields self-heal at startup** (`_ensure_custom_fields()` in `__init__`, best-effort/DEBUG-logged so a restricted token never breaks the spoke). Spec: `proxmox_unique_id`, `proxmox_vmid`, `proxmox_node`, `proxmox_type`, `proxmox_labels`, `discovered_from`, `nw_device_id`, `nw_managed`, `mac_address` (on `ipam.ipaddress` + `dcim.device`), `switch_name`/`switch_ip`/`switch_port`, `last_seen`, `decommissioned_at`, `vmid_start`/`vmid_end` (on `tenancy.tenant`). CFs-not-attached is a stale-deploy gap (rerun `install.sh` / Apply-schema), not a spec gap.
- **pynetbox auth pin** — `_apply_auth()` sets `Authorization: Token …` on `http_session.headers` because `_api_get()` calls `http_session.get()` directly (bypassing pynetbox's per-request wrapper) and would otherwise go out unauthenticated → 403.
- **HTTP semaphore** — module-level `_netbox_http_sem = threading.Semaphore(1)` serializes direct GETs to avoid OOM-killing gunicorn workers on concurrent IPAM queries. `_api_get_all` paginates via `next` links; `_api_get` is single-page and would silently truncate.
- **Hypervisor vminterface endpoint** — accessor is `nb.virtualization.interfaces` (REST endpoint `interfaces`), NOT `nb.virtualization.vminterfaces` (404s). Name collision: model string is `virtualization.vminterface`.
- **sync_vms primary_ip4** — `_assign_vm_primary_ip4` PATCHes only `primary_ip4` via `virtual_machines.update`; a full `obj.save` re-sends unattached custom_fields → 400 "does not exist for this object type". Bare-return→`(0,None)` contract.
- **sync_devices fresh-fetch** — create branch re-sent unprovisioned custom_fields on primary_ip4 save → 400 killed the upsert; fixed by fresh `devices.get` + best-effort save so a missing cf degrades to "field not set".
- **Kea port trap** — KEA CA must be 8760, not 8000 (the unified hub owns :443, but NetBox/the legacy webui-spoke can occupy :8000 on a co-located box); KEA on 8000 fails to bind there and the sync loop POSTs the hub → 405.
- **Custom validators** — `ProxmoxRangeValidator` (tenant ranges non-overlapping) + `ProxmoxVmidInRangeValidator` (VM VMID within tenant range), lenient when a range is unset. NetBox v4.2+.

## Related pages

[architecture-topology.md](architecture-topology.md), [opnsense.md](opnsense.md), [nw.md](nw.md), [cppm.md](cppm.md), [pxmx.md](pxmx.md), [install-flags.md](install-flags.md).
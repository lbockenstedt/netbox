# netbox — NetBox DCIM/IPAM Spoke (Lab Manager Module)

The **NetBox Spoke** (`module_type = "ipam"`) provides the authoritative single source of truth for all DCIM (sites, racks, physical devices) and IPAM (subnets/prefixes, IP addresses, VLANs, VRFs) assets across the Lab Manager fleet. In addition to serving interactive queries and mutations from the Lab Manager WebUI, it acts as the centralized discovery sink for network scans (`nw`), hypervisor VM inventories (`pxmx`), firewall leases (`opnsense`), and NAC sessions (`cppm`), complete with automated staleness retirement sweeps and background Kea DHCP synchronization.

---

## Architecture Overview

The module connects to the Lab Manager Hub over an outbound TLS/WebSocket tunnel (`wss://<hub>:443/ws/spoke`) using the standard push-ack-retry mailbox pattern (`BaseSpoke`). It interfaces with NetBox REST endpoints using `pynetbox` through a thread-pool executor (`_run_sync`) to ensure long-running database operations never block the event loop.

```
                  ┌─────────────────────────────────────────────────────────┐
                  │                    Lab Manager Hub                      │
                  │       (WebUI / REST API / Discovery Ingestion)          │
                  └────────────────────────────┬────────────────────────────┘
                                               │ WebSocket (Port 443)
                                               ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │                 NetboxSpoke (BaseSpoke)                 │
                  │   src/netbox_spoke.py ── Worker Dispatch & Picklist     │
                  └────────────┬─────────────────────────────┬──────────────┘
                               │                             │
                     pynetbox (REST)             Kea REST API (Port 8760)
                               ▼                             ▼
                  ┌────────────────────────┐    ┌───────────────────────────┐
                  │     NetBox Server      │    │    Kea Control Agent      │
                  │   DCIM / IPAM / Tenancy│    │   (subnet4-add / pools)   │
                  └────────────────────────┘    └───────────────────────────┘
```

### Key Components

- **`src/netbox_spoke.py` (`NetboxSpoke`)**: Core command router, background Kea sync loop (default 300s interval), environment persistence (`_persist_env`), and picklist caching.
- **`src/netbox_engine.py` (`NetboxEngine`)**: Composition class binding domain mixins with shared HTTP concurrency semaphores (`_netbox_http_sem`) and paginated API walkers.
- **`src/netbox_ipam.py` (`IpamMixin`)**: Prefix allocation, free-subnet finder grid calculations, IP address reservations, and custom field updates.
- **`src/netbox_dcim.py` (`DcimMixin`)**: Sites, racks, device CRUD, front/rear rack elevation models, bundled catalog seeding, and universal multi-attribute search.
- **`src/netbox_vmsync.py` (`VmSyncMixin`)**: Proxmox cluster VM ingestion, primary IP assignment, tag-driven tenancy mappings, and VMID range queries.
- **`src/netbox_sync.py` (`SyncMixin`)**: Multi-source device discovery upsert (switches, APs, firewalls, ClearPass access tracker) with MAC normalization and prefix-length derivation.
- **`src/netbox_tenancy.py` (`TenancyMixin`)**: Tenant queries, multi-model tenant data reassignment/migration (`migrate_tenant`), and DHCP scope harvesting.
- **`src/netbox_staleness.py` (`StalenessMixin`)**: Scheduled discovery record retirement (marking devices offline after inactivity, followed by automatic purge).
- **`src/custom_fields_spec.py`**: Pure-data specification defining custom fields required for discovery tracking, Proxmox links, switch ports, and tenancy ranges.

---

## Key Features

1. **IPAM Source of Truth**: Full lifecycle management of IPv4/IPv6 prefixes and addresses. Includes an intelligent free-subnet finder that locates contiguous available subnets within supernets.
2. **DCIM & Rack Elevations**: Visual front and rear rack layout rendering with RU-level precision, multi-U aggregation, 0U device capture, and device role color tinting.
3. **Proxmox VM Cluster Sync**: Ingests complete hypervisor inventories, mapping Proxmox tags to NetBox tenants while tracking VM status, MACs, and primary IPs.
4. **Multi-Source Discovery Sink**: Consolidates live observations from switches (`nw`), firewalls (`opnsense`), and NAC (`cppm`), eliminating manual inventory entry.
5. **Multi-Tenant Isolation & Migration**: Enforces strict tenant boundaries across all models, supports Proxmox VMID range reservations (`vmid_start`/`vmid_end`), and offers one-click tenant migration.
6. **Kea DHCP Scope Synchronization**: Harvests subnets marked with `gateway` and `dhcp_enabled` custom fields and pushes them to Kea DHCP via its Control Agent.
7. **Excel Rack Importer**: Dynamic spreadsheet parser (`netbox_xlsx.py`) that reads rack elevation workbooks and provisions racks and devices with column auto-detection.

---

## Spoke Commands Reference Table

| Spoke Command | Handler / Target | Description |
| :--- | :--- | :--- |
| `GET_VERSION` | `get_version` | Returns spoke software version and git commit hash. |
| `UPDATE_CONFIG` | `_reconnect` / `_persist_env` | Updates NetBox URL, API token, or Kea URL; re-runs schema self-heal. |
| `SPOKE_UPDATE` | Self-update | Triggers automated git pull and spoke service restart. |
| `NETBOX_HEALTH` | `get_system_health` | Checks reachability and latency against the NetBox REST API. |
| `NETBOX_GET_SITES` | `get_sites` | Lists all DCIM sites configured in NetBox. |
| `NETBOX_GET_RACKS` | `get_racks` | Retrieves racks filtered by site or tenant. |
| `NETBOX_ADD_RACK` | `add_rack` | Provisions a new rack with specified height, facility ID, and tenant. |
| `NETBOX_UPDATE_RACK` | `update_rack` | Edits rack name, height, facility ID, or tenant ownership. |
| `NETBOX_DELETE_RACK` | `delete_rack` | Removes a rack by ID from NetBox. |
| `NETBOX_GET_RACK_ELEVATION` | `get_rack_elevation` | Generates front/rear elevation unit slots and 0U summaries. |
| `NETBOX_GET_DEVICES` | `get_devices` | Retrieves devices filtered by site, rack, or tenant. |
| `NETBOX_ADD_DEVICE` | `add_device_to_rack` | Places a device into a specified rack unit and face. |
| `NETBOX_UPDATE_DEVICE` | `update_device` | Modifies device name, status, or rack position. |
| `NETBOX_DELETE_DEVICE` | `delete_device` | Deletes a device record and disassociates its interfaces. |
| `NETBOX_CLAIM_DEVICE` | `claim_device` | Creates a device, configures primary interface, and assigns IP. |
| `NETBOX_GET_DEVICE_FORM_OPTIONS` | `get_device_form_options` | Returns cached picklists for roles, device types, sites, and racks. |
| `NETBOX_DEDUPE_DEVICES` | `dedupe_devices` | Reconciles and merges duplicate device records. |
| `NETBOX_GET_PREFIXES` | `get_prefixes` | Lists IP prefixes filtered by site, VRF, or tenant. |
| `NETBOX_ALLOCATE_PREFIX` | `allocate_prefix` | Allocates the next available child subnet within a parent prefix. |
| `NETBOX_FIND_AVAILABLE_PREFIXES` | `find_available_prefixes` | Discovers available contiguous subnets near a target anchor. |
| `NETBOX_CLAIM_PREFIX` | `claim_prefix` | Directly reserves a specified subnet prefix for a tenant. |
| `NETBOX_UPDATE_PREFIX` | `update_prefix` | Modifies prefix description, tenant, or custom fields. |
| `NETBOX_DELETE_PREFIX` | `delete_prefix` | Deletes an IP prefix from NetBox. |
| `NETBOX_GET_IPS` | `get_ip_addresses` | Lists IP addresses filtered by prefix, device, or tenant. |
| `NETBOX_ALLOCATE_IP` | `allocate_ip` | Reserves the next available IP address in a prefix. |
| `NETBOX_RELEASE_IP` | `release_ip` | Deletes an IP address record. |
| `NETBOX_UPDATE_IP_ADDR` / `NETBOX_UPDATE_IP` | `update_ip_address` | Updates DNS name, description, status, or custom fields of an IP. |
| `NETBOX_DOC_VM` | `create_vm_entry` | Creates or updates a virtual machine entry with primary IP. |
| `NETBOX_GET_TENANTS` | `get_tenants` | Lists all tenants configured in NetBox. |
| `NETBOX_MIGRATE_TENANT` | `migrate_tenant` | Reassigns all DCIM/IPAM/VM objects from source to target tenant. |
| `NETBOX_TENANT_VMID_RANGE` | `get_tenant_vmid_range` | Queries tenant `vmid_start`/`vmid_end` and allocated VMIDs. |
| `NETBOX_SYNC_DHCP` | `_kea_sync_loop` / `get_dhcp_prefixes` | Pushes NetBox subnets and router options to Kea DHCP agent. |
| `NETBOX_SYNC_VMS` | `sync_vms` | Synchronizes Proxmox VM inventories into NetBox. |
| `NETBOX_SYNC_DEVICES` | `sync_devices` | Ingests switch/firewall discovery feeds into DCIM devices. |
| `NETBOX_SYNC_NW_DEVICE` | `sync_nw_device` | Upserts switches and APs with interface tables from `nw`. |
| `NETBOX_SYNC_ACCESS_TRACKER` | `sync_access_tracker` | Ingests 802.1X/MAB client sessions from ClearPass. |
| `NETBOX_STALENESS_SWEEP` | `staleness_sweep` | Flags inactive discovery assets offline and purges aged records. |
| `NETBOX_SEARCH` | `search` | Multi-attribute fast regex and substring search across inventory. |
| `NETBOX_PROVISION_CUSTOM_FIELDS` | `_ensure_custom_fields` | Forces idempotent provisioning of required custom fields. |
| `NETBOX_SEED_CATALOG` | `seed_catalog` | Seeds bundled Aruba/HPE/Juniper hardware models into NetBox. |
| `NETBOX_IMPORT_RACK_DETECT` | `detect_rack_sheets` | Analyzes uploaded Excel workbook for rack elevations. |
| `NETBOX_IMPORT_RACK_COMMIT` | `import_rack_layout` | Commits mapped Excel rack elevation data to NetBox. |
| `INSTALL_CERT` | `_persist_cert` | Stores and activates custom SSL/TLS certificates for the spoke. |

---


<!-- INSTALLERS:START -->
## Installation

Every installer in this repo, with every flag and environment variable it accepts.
Installers are idempotent — re-running one updates code and preserves credentials.

### NetBox (IPAM) + spoke — `install.sh`

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/netbox/main/install.sh \
  | sudo bash -s -- --hub lm-hub.lrbtechnologies.com
```

Installs NetBox itself — PostgreSQL, Redis, gunicorn, nginx — **and** the LM spoke in one shot. Safe to re-run: it updates code, runs migrations and restarts services without overwriting your data.

| Flag | Purpose |
| :--- | :--- |
| `--hub URL` | Hub WebSocket URL. A bare host is fine — `lm-hub.example.com` becomes `wss://lm-hub.example.com:443`, `host:port` gets a `wss://` prefix, and an explicit `ws://`/`wss://` is left alone. Omit it to auto-discover the hub (DNS `lm-hub.<suffix>`, then mDNS `_lm-hub._tcp.local.`). |
| `--id`, `--name` | Pin the spoke id. Omitted, the id derives from the hostname, so a renamed clone reconnects under its new name. |
| `--secret` | Pre-shared spoke secret. |
| `--hub-secret` | Hub PSK for auto-approval. Without it the spoke lands in *pending approval* in the WebUI. |
| `--all-prereqs` | Accepted and ignored — kept so the hub's install-module call doesn't abort. |
| `--spoke-only` | Install just the LM spoke against an existing NetBox. |
| `--infra-only` | Host-level infrastructure only — no spoke runtime. |
| `--netbox-url` | Base URL of the NetBox instance. |
| `--netbox-token` | NetBox API token. |
| `--netbox-version` | NetBox version to install. |
| `--db-pass` | PostgreSQL password. |
| `--superuser`, `--admin-user` | NetBox superuser name. |
| `--superpass`, `--admin-password` | Superuser password. |
| `--supermail` | Superuser email. |
| `--reset-admin-password PW` | Reset the superuser password and exit — no reinstall. |
| `--netbox-sso-tenant` | Entra tenant id for SSO. |
| `--netbox-sso-client-id` | SSO application (client) id. |
| `--netbox-sso-client-secret` | SSO client secret. |
| `--netbox-sso-redirect-uri` | OAuth redirect URI. |
| `--netbox-sso-group-map` | Group→role mapping. |
| `--netbox-sso-allowed-group` | Restrict sign-in to this group. |
| `--provision-cert-helper` | Install the certificate helper. |
| `--admin-token` | Deprecated (zero-touch provisioning), accepted and ignored. |

**Environment overrides:** `HUB_URL` (same normalization as `--hub`), `SPOKE_ID`., `HUB_SECRET`, `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_FQDN`, `NETBOX_IP`, `NB_SUPERPASS`, `LM_NETBOX_SSO_TENANT`, `LM_NETBOX_SSO_CLIENT_ID`, `LM_NETBOX_SSO_CLIENT_SECRET`, `LM_NETBOX_SSO_REDIRECT_URI`, `LM_NETBOX_SSO_GROUP_MAP`, `LM_NETBOX_SSO_ALLOWED_GROUP`

### Kea DHCP helper — `install_kea.sh`

```bash
sudo bash install_kea.sh
```

Provisions the Kea DHCP pieces NetBox's DHCP integration depends on. No flags.
<!-- INSTALLERS:END -->

## Seed device catalog (`src/seed_catalog.json` + `DcimMixin.seed_catalog`)

A bundled **Aruba / HPE / Juniper** device-type catalog (`src/seed_catalog.json`,
~45 models) plus `NetboxEngine.seed_catalog()` load it into NetBox in one action
via the `NETBOX_SEED_CATALOG` command — get-or-create **manufacturers** +
**device types** (upsert `u_height`/`is_full_depth`/`comments`, `.save()`),
then **add-missing** interface/console/power templates (expand each model's
`ports` spec into named templates; one `mgmt` interface with `mgmt_only=True`).

- **Idempotent** — re-runs never error on an existing slug; they upsert scalars
  and add only missing templates. Existing templates (hand-added or re-typed)
  are never deleted/clobbered. So "edit the catalog, re-run" works for
  *adding* models/ports; a rename or port-type *change* on an existing name is
  a delete-that-device-type + re-seed (re-create), not an automatic migration.
- **Admin-only** — triggered from the WebUI **Setup → Module Management →
  "Seed catalog"** card (hidden for non-admins) → `POST /api/netbox/seed-catalog`
  (403 for non-admins). Runs on the spoke, reusing its `NETBOX_URL`/token.
- **Per-model error isolation** — one bad model is collected into `errors[]`
  and doesn't abort the rest. Returns `{status, manufacturers_created,
  device_types_created, device_types_updated, templates_added, errors[]}`.
- Listed in `_PICKLIST_MUTATIONS` so the device-type/form-options picklist cache
  is dropped after seeding.

To extend: edit `src/seed_catalog.json`, redeploy the spoke (WebUI Update), and
click Seed catalog again.

## Import rack layout from Excel (`src/netbox_xlsx.py` + `DcimMixin.import_rack_layout`)

A dynamic, admin-only importer for recreating a lab's racks + devices from an
`.xlsx` workbook (the format the lab keeps its rack elevations in). Handles
**column drift** between sheets and **two sheet shapes** — one-rack-per-sheet
(header row `RU`/`F/R`/`Type of device`/`Hostname`/`Serial`/`MGMT IP`…) and
whole-lab multi-rack summary sheets (`RACK <name>` blocks with `Front`/`Rear`
text cells). Flow (two-step URL relay, mirroring template-refresh):

1. **Upload + detect** — `POST /api/netbox/racks/import-xlsx` (multipart) → the
   hub saves the file to `/var/lib/lm/imports/<uuid>.xlsx`, mints a one-time
   token, relays `NETBOX_IMPORT_RACK_DETECT {download_url, token}` → the spoke
   HTTP-GETs it, parses with **openpyxl** (`detect_rack_sheets`), auto-detects
   rack sheets, and **guesses a column→field map**. Returns the preview +
   device form-options.
2. **Map + commit** — the WebUI shows a per-rack column-mapping table (edit +
   pick racks + optional Dry run) → `POST /api/netbox/racks/import-commit` →
   the spoke re-GETs the file, re-parses the selected sheets with the user's
   maps (`parse_one_rack_sheet` / `parse_summary_block_by_name`), and runs
   `import_rack_layout(selected, dry_run)`.

`import_rack_layout` is idempotent (match by **serial** else **name-in-rack**;
re-import updates, never duplicates) with **per-device error isolation** (one
bad row never aborts the whole import). Devices are placed at `position` = RU
(1:1 for 1U; RU 0 → 0U) with `face` F→front / R→rear, stamped with the current
tenant, `serial`, `asset_tag`; an `mgmt` interface + IP (mask from the most-
specific containing prefix, `/32` fallback) is attached when `mgmt_ip` is
mapped (mirrors `claim_device`). Device types are resolved by
`_resolve_device_type_slug` against the **seed catalog** (stem + port-hint:
`"6300M 24SR5 CL6"` → `6300m-24g`, `"CX8325-32 (F2B)"` → `8325-32c`) → live
NetBox → unresolved = a **per-device error (skipped, never a junk type)**.
**Seed/extend the catalog first** for the models you import. `dry_run=True`
resolves everything but writes nothing. Admin-only (button hidden for
non-admins + both routes 403). Dep: `openpyxl` (`requirements.txt`); a missing
dep degrades to a clear ERROR, not a spoke crash.

## Rack elevation view (`DcimMixin.get_rack_elevation`)

A NetBox-style front/back rack elevation, surfaced in the WebUI via the eye
button on each rack row (IPAM → Racks). `get_rack_elevation(rack_id)` calls
NetBox's `/api/dcim/racks/{id}/elevation/?face=front|rear` — one entry per RU,
top→bottom (U=N at the top), multi-U devices occupying consecutive units — plus
the rack's device list for 0U / side devices (position null/0, never in the
unit list). It returns `{rack:{name,u_height,site,tenant},
faces:{front,rear:[{unit,device}]}, zero_u:[…]}` with per-device summaries
(`name`, `model`, `u_height`, `role`, role `color` for slot tinting, `status`,
`primary_ip`, `tenant`, `face`). The WebUI merges consecutive same-device units
into one rowspan cell. Dispatched by `NETBOX_GET_RACK_ELEVATION` (read-only,
not a picklist mutation); non-admins are ownership-gated against their tenant's
racks cache at the hub route, admins bypass.

## Proxmox VMID ranges & custom validators (`install.sh`)

`install.sh` idempotently provisions two integer custom fields on
`tenancy.tenant` and registers two custom validators, so a fresh install **and**
a re-run both end up with them. Operators set a tenant's `vmid_start` /
`vmid_end` (in the NetBox UI, on the tenant) to reserve a Proxmox VMID range
for that tenant.

- **`vmid_start` / `vmid_end`** — integer custom fields on `tenancy.tenant`.
- **`ProxmoxRangeValidator`** (`tenancy.tenant`) — `vmid_start <= vmid_end`, and
  a tenant's `[vmid_start, vmid_end]` must not overlap another tenant's range.
- **`ProxmoxVmidInRangeValidator`** (`virtualization.virtualmachine`) — a VM's
  `proxmox_vmid` custom field must fall inside its assigned tenant's range.

Both validators are **lenient when a range is unset**: a tenant with no
`vmid_start`/`vmid_end` is unconstrained, and a VM whose tenant has no range
(or which has no `proxmox_vmid`, or no tenant) is skipped. This keeps the
Lab Manager Proxmox→NetBox sync working before/without ranges — enforcement
strengthens as tenants get ranges. So deploying this never blocks the sync.

The validator module is installed at
`/opt/netbox-app/netbox/lm_custom_validators.py` (project root, on NetBox's
`sys.path`) and wired in via a guarded `CUSTOM_VALIDATORS` block appended to
`configuration.py` (only if absent). NetBox loads `CUSTOM_VALIDATORS` on boot,
so a restart of `netbox`/`netbox-rq` is performed when the block is first
added. Re-running `install.sh` is safe — every step is `get_or_create` /
grep-guarded.

Imports target NetBox **v4.2+** (`extras.validators.CustomValidator`; in v3 the
path was `extras.custom_validators`).

## Proxmox → NetBox VM sync (grab-all)

The LM hub syncs the **entire** Proxmox cluster into NetBox virtualization
records via the `NETBOX_SYNC_VMS` command (`netbox_engine.sync_vms`):

- One pull of **all** VMs/CTs from the hypervisor; each VM is matched by its
  `custom_fields.proxmox_unique_id` and upserted (all attributes every sync).
- Tenant attribution is tag-driven: a VM whose Proxmox `tags` contain a
  tenant's `proxmox_tag` is assigned to that NetBox tenant; an untagged VM
  (or one whose tag matches no tenant) is created with **no tenant** (a
  global/unassigned record).
- `replace=True` deletes NetBox VMs that carry a `proxmox_unique_id` but are no
  longer in the pull (destroyed in Proxmox) — cluster-wide, proxmox-sourced
  only. Manually-created NetBox VMs are never touched. A VM that changed tags
  simply has its `tenant` updated (never deleted-and-recreated).
- The response includes a `per_tenant` breakdown so the hub records per-tenant
  last-sync status (plus an `__unassigned__` bucket for untagged VMs).

`NETBOX_TENANT_VMID_RANGE` (`netbox_engine.get_tenant_vmid_range`) reads a
tenant's `vmid_start`/`vmid_end` + the `proxmox_vmid` values already in use on
that tenant's VMs (inside the range), used by the LM hub's optional VMID
auto-allocation knob.

## Entra ID (OIDC) SSO — installer support (`install.sh`)

`install.sh` can wire NetBox for **Azure Entra ID (OIDC) single sign-on** with
Entra-group → NetBox-group sync (Entra is the source of truth for group
membership). Two pieces ship in the installer:

- **`social-auth-core[openidconnect]`** is pip-installed into the NetBox venv.
  NetBox ships `social-auth-core` without the `[openidconnect]` extra (which
  pulls `python-jose`), so the stock `OpenIdConnectAuth` backend would crash at
  load (`ModuleNotFoundError: jose`) without this. Idempotent.
- **`lm_sso_pipeline.py`** is written to the NetBox project root
  (`/opt/netbox-app/netbox/lm_sso_pipeline.py`, on `sys.path`, alongside
  `lm_custom_validators.py`). It exports one social-auth pipeline step,
  `sync_entra_groups`, which maps Entra group object IDs → NetBox groups via the
  `NETBOX_SSO_GROUP_MAP` setting and sets the NetBox user's groups to exactly
  that set on every login — so a dropped Entra group drops the NetBox group
  next login. It also handles the Entra **>200-groups overflow** (when the
  `groups` claim is omitted) by falling back to Microsoft Graph
  `/me/transitiveMemberOf` (ported from the LM hub's
  `security.oidc.fetch_member_groups_via_graph`), and an optional
  `NETBOX_SSO_ALLOWED_GROUP` login gate.

MFA is enforced by **Entra conditional access at the IdP** — NetBox trusts the
IdP (social-auth doesn't expose the `amr` claim for a hub-side hard-check the
way the LM hub's own OIDC provider does).

The pipeline module is written on every run but is only imported once
`SOCIAL_AUTH_PIPELINE` references it — i.e. once SSO is configured via the
`--netbox-sso-*` flags (see the Entra setup section below).

### Entra setup

SSO is applied by **re-running `install.sh` with the `--netbox-sso-*` flags**
(or the `LM_NETBOX_SSO_*` env equivalents). There is no live WebUI push and no
sudoers grant — the installer writes a guarded, sentinel-delimited block into
`configuration.py` and restarts `netbox`/`netbox-rq`. Re-running with the same
flags is a no-op (the block matches → unchanged → no restart); re-running with
changed flags replaces the block in place (idempotent, never clobbers the rest
of `configuration.py`); omitting the flags leaves an existing SSO block intact
(we never silently disable a working setup — edit `configuration.py` to remove
it by hand).

**1. App registration (Entra):** create a new app registration in your Entra
tenant (separate from the LM hub's cert-auth app — NetBox uses a client
secret, not a certificate). Note the **tenant (directory) id**, the
**client (application) id**, and generate a **client secret**. Add a web
redirect URI of `https://<netbox-host>/oauth/complete/oidc/` (trailing slash;
the `oidc` segment is the social-auth backend name). Under **Token
configuration → Add groups claim**, select **Groups assigned to the
application** (or Security groups) emitted as **group object IDs** (the
`groups` claim) — this is what `sync_entra_groups` maps. Apply a
**conditional-access policy enforcing MFA** on this app (NetBox trusts Entra
to enforce MFA at the IdP).

**2. Pre-create NetBox groups + permissions:** in NetBox admin, create the
target groups named exactly as in your group map (e.g. `netbox-admins`) and
assign NetBox **permissions** to them once. With
`REMOTE_AUTH_AUTO_CREATE_GROUPS=True` (the installer default) the groups
auto-create on first login, but pre-creating lets you assign permissions ahead
of time. Entra group membership drives which NetBox groups a user lands in; a
dropped Entra group drops the NetBox group on the next login.

**3. Apply** (re-run the installer with the flags):

```bash
sudo bash install.sh \
  --hub wss://lm-hub:443 \
  --netbox-sso-tenant          <directory-id> \
  --netbox-sso-client-id       <application-id> \
  --netbox-sso-client-secret   <client-secret> \
  --netbox-sso-redirect-uri    https://<netbox-host>/oauth/complete/oidc/ \
  --netbox-sso-group-map       '{"<entra-group-obj-id>": "netbox-admins"}' \
  --netbox-sso-allowed-group   <entra-group-obj-id>   # optional login gate
```

Flags (all optional; `tenant` + `client-id` + `client-secret` together enable
SSO):

| Flag | Env | Purpose |
|------|-----|---------|
| `--netbox-sso-tenant` | `LM_NETBOX_SSO_TENANT` | Entra directory (tenant) id |
| `--netbox-sso-client-id` | `LM_NETBOX_SSO_CLIENT_ID` | Entra application (client) id |
| `--netbox-sso-client-secret` | `LM_NETBOX_SSO_CLIENT_SECRET` | Entra client secret (quoted safely into `configuration.py`) |
| `--netbox-sso-redirect-uri` | `LM_NETBOX_SSO_REDIRECT_URI` | The URI registered in Entra (recorded as a comment; social-auth derives the actual redirect) |
| `--netbox-sso-group-map` | `LM_NETBOX_SSO_GROUP_MAP` | JSON `{"<entra-group-obj-id>": "<netbox-group-name>"}` |
| `--netbox-sso-allowed-group` | `LM_NETBOX_SSO_ALLOWED_GROUP` | Entra group obj-id; when set, only its members may log in |

The resulting `configuration.py` block sets `REMOTE_AUTH_*` +
`SOCIAL_AUTH_OIDC_*` + a `SOCIAL_AUTH_PIPELINE` extended with
`lm_sso_pipeline.sync_entra_groups`, plus `NETBOX_SSO_GROUP_MAP` and
`NETBOX_SSO_ALLOWED_GROUP`. **Break-glass:** a local NetBox superuser (created
by the installer) still logs in via NetBox's local Django auth when Entra is
unreachable. **Secret rotation** = re-run the installer with a new
`--netbox-sso-client-secret`.
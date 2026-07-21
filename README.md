# netbox
Netbox Lab Manager Module

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
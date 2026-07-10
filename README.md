# netbox
Netbox Lab Manager Module

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
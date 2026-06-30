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
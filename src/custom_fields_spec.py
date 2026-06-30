"""Single source of truth for the NetBox custom fields the Lab Manager syncs use.

This module is intentionally **pure data** — no heavy imports — so it can be
consumed by every provisioning path without dragging in pynetbox/Django:

  * ``netbox_engine._ensure_custom_fields`` imports ``CUSTOM_FIELDS_SPEC`` and
    provisions the fields over the NetBox REST API (spoke self-heal at startup
    + the on-demand "Apply schema changes" button in the WebUI IPAM screen).
  * ``install.sh`` reads this file two ways:
      - the Django ``manage.py shell`` block (full-app install on the NetBox
        host) ``exec``s it to get ``CUSTOM_FIELDS_SPEC``;
      - the REST-API block (spoke-only / external NetBox) imports it from the
        spoke venv.
    Both map the REST type string ("text"/"integer") to the right constant for
    their environment, so there is exactly ONE list driving fresh installs,
    updates, and the runtime button — no drift, no partial change.

Each entry is ``(name, type, label, content_type)`` where:

  * ``type``          — the NetBox REST custom-field type string ("text" or
                        "integer"); identical to the
                        ``extras.choices.CustomFieldTypeChoices`` value, so the
                        Django path maps "text"→TYPE_TEXT, "integer"→
                        TYPE_INTEGER.
  * ``content_type``  — ``"<app_label>.<model>"`` (e.g.
                        ``"virtualization.virtualmachine"``), resolvable both
                        via ``ContentType.objects.get(app_label=..., model=...)``
                        (Django) and as the REST ``content_types`` list entry.

A field name may appear on more than one content type (e.g. ``mac_address`` on
both ``ipam.ipaddress`` and ``dcim.device``) — each (name, content_type) pair
is its own provisioning row, so the same global field gets attached to every
object type it's needed on. Provisioning is idempotent (get-or-create + verify
the content_type is attached) and safe to re-run any number of times.
"""

# (name, type, label, content_type)
CUSTOM_FIELDS_SPEC = [
    # Proxmox/Hypervisor → IPAM VM sync (virtualization.virtualmachine)
    ("proxmox_unique_id", "text", "Proxmox unique id", "virtualization.virtualmachine"),
    ("proxmox_vmid", "text", "Proxmox VMID", "virtualization.virtualmachine"),
    ("proxmox_node", "text", "Proxmox node", "virtualization.virtualmachine"),
    ("proxmox_type", "text", "Proxmox type", "virtualization.virtualmachine"),
    ("proxmox_labels", "text", "Proxmox labels", "virtualization.virtualmachine"),
    # Firewall → IPAM device discovery sync (dcim.device)
    ("discovered_from", "text", "Discovered from", "dcim.device"),
    # Network Devices (nw) POLL NOW inventory sync — links a NetBox dcim.device
    # to its nw fleet device id (match key for the polled switch/gateway upsert)
    # and marks dcim.interfaces the nw sync created (so replace-delete only ever
    # touches our interfaces, never manually-created ones).
    ("nw_device_id", "text", "NW device id", "dcim.device"),
    ("nw_managed", "text", "NW managed", "dcim.interface"),
    # MAC-keyed matching shared by the NAC↔IPAM (ClearPass access tracker) and
    # firewall discovery syncs. Attached to both ipam.ipaddress and dcim.device.
    ("mac_address", "text", "MAC address", "ipam.ipaddress"),
    ("mac_address", "text", "MAC address", "dcim.device"),
    # Access-tracker (NAC→IPAM reverse sync) endpoint topology (dcim.device)
    ("switch_ip", "text", "Switch IP", "dcim.device"),
    ("switch_port", "text", "Switch port", "dcim.device"),
    # Staleness-sweep "last seen" clock on every sync-owned object type so the
    # sweep can age them uniformly (7d → offline, 30d → delete + free IPs).
    ("last_seen", "text", "Last seen", "dcim.device"),
    ("last_seen", "text", "Last seen", "virtualization.virtualmachine"),
    ("last_seen", "text", "Last seen", "ipam.ipaddress"),
    # Decommission clock: set when staleness_sweep flips an object offline.
    ("decommissioned_at", "text", "Decommissioned at", "dcim.device"),
    ("decommissioned_at", "text", "Decommissioned at", "virtualization.virtualmachine"),
    # Proxmox VMID auto-allocation range on tenancy.tenant (integer fields).
    ("vmid_start", "integer", "Proxmox VMID range start", "tenancy.tenant"),
    ("vmid_end", "integer", "Proxmox VMID range end", "tenancy.tenant"),
]
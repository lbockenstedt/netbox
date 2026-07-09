# NetBox Spoke API Specification

The NetBox Spoke integrates Lab Manager with NetBox to maintain the authoritative Source of Truth (SoT) for IPAM and DCIM.

## Command Set

### IPAM & Device Management
- **`NETBOX_UPDATE_IP`**
  - **Purpose**: Updates the primary IP address for a specific device.
  - **Payload**: `{"device": "vm-name", "ip": "10.0.0.x"}`
  - **Response**: `{"status": "SUCCESS", "device": "vm-name", "ip": "10.0.0.x"}`
- **`NETBOX_DOC_VM`**
  - **Purpose**: Creates a virtual machine entry in NetBox to document its existence, location, and resources.
  - **Payload**: `{"name": "string", "cluster": "string", "vcpus": integer, "ram": integer}`
  - **Response**: `{"status": "SUCCESS", "vm_id": "integer", "name": "string"}`
- **`NETBOX_GET_DEVICE_FORM_OPTIONS`**
  - **Purpose**: Returns the picklists (sites, device types, device roles,
    tenants) needed to populate a "create device" form, in one round trip. Used
    by the LM "Claim an unknown device" modal so the user chooses from real
    NetBox values.
  - **Payload**: `{}`
  - **Response**: `{"status": "SUCCESS", "sites": [{"id","name","slug"}],
    "device_types": [{"id","slug","model","manufacturer"}],
    "device_roles": [{"id","name","slug"}], "tenants": [{"id","name","slug"}]}`
- **`NETBOX_CLAIM_DEVICE`**
  - **Purpose**: Creates a rack-less device owned by a tenant — the "Claim"
    action on a CPPM unknown (untagged) endpoint. Records the endpoint's MAC in
    the description and, when `ip` is supplied, attaches that IP as the device's
    primary IPv4 on an `mgmt` interface (mask derived from the most specific
    containing NetBox prefix, else `/32`). The hub follows this with an
    endpoint sync so the matching ClearPass endpoint is tagged with the tenant
    and leaves "Unknown Devices". `site` and `device_type` are required by
    NetBox; `role`, `tenant`, `status`, `description`, `ip`, `mac`, `dns_name`
    are optional.
  - **Payload**: `{"name": "router-01", "device_type": "<slug>", "role": "<slug>",
    "site": "<slug>", "tenant": "<netbox-tenant-slug>", "status": "active",
    "description": "...", "ip": "10.0.0.5", "mac": "AA:BB:CC:DD:EE:FF",
    "dns_name": ""}`
  - **Response**: `{"status": "SUCCESS", "device_id": 123, "name": "router-01",
    "ip": "10.0.0.5/24", "tenant": "<slug>"}`
  - **Errors**: unknown site/type/role/tenant slug → `{"status": "ERROR",
    "message": "<X> '<slug>' not found"}`.

### Synchronization
- **`NETBOX_SYNC_DHCP`**
  - **Purpose**: Triggers an immediate synchronization of DHCP prefixes from NetBox to the KEA DHCP server.
  - **Payload**: `{}`
  - **Response**: `{"status": "SUCCESS", "message": "DHCP synchronization triggered."}`
- **`NETBOX_SYNC_DEVICES`**
  - **Purpose**: Firewall → NetBox device discovery sync. The hub relays a tenant's firewall-discovered devices (DHCP leases + ARP table from the OPNsense spoke, attributed to the tenant by prefix containment) for an authoritative replace into NetBox DCIM devices + IP records. Each incoming `{ip, mac, hostname}` is matched to an existing device by a tiered key — **primary IPv4 first, then `custom_fields.mac_address`, then bare hostname only** (a bare-hostname match is refused when the candidate already carries an IP or MAC, so two distinct machines sharing a short name are never merged); missing devices are created (tenant-owned device + `mgmt` interface + IP with `custom_fields.mac_address` + `primary_ip4`). Writing the MAC onto the IP feeds the NetBox→CPPM endpoint sync (keys on `mac_address`) — so static-IP devices the ARP table sees flow to ClearPass. Created devices are tagged `custom_fields.discovered_from = "opnsense"`; when `replace` + a tenant slug are given, tagged devices of that tenant whose primary IP is absent from the incoming set are deleted. Pre-existing devices matched by IP are refreshed (MAC/dns_name) but not tagged/deleted.
  - **Payload**: `{"tenant_id", "tenant_slug": "<netbox-tenant-slug>", "tenant_name", "source": "OPNsense", "replace": true, "devices": [{"ip", "mac", "hostname"}, ...], "defaults": {"role": "<slug>", "device_type": "<slug>", "site": "<slug>"}}`
  - **Response**: `{"status": "SUCCESS", "pushed": N, "errors": N, "skipped": N, "deleted": N, "devices_total": N, "message": "..."}`
  - **Errors**: unknown tenant → `{"status": "ERROR", "message": "NetBox tenant '<slug>' not found ..."}`. Missing `discovered_from`/`mac_address` custom fields are tolerated (writes skipped, replace-delete becomes a safe no-op).

### Tenant Self-Service Subnet Allocation
- **`NETBOX_FIND_AVAILABLE_PREFIXES`**
  - **Purpose**: Finds the closest free subnets of a requested size to a
    reference subnet, for a tenant to pick a new subnet. "Free" = no
    tenant-assigned NetBox prefix overlaps it (undefined-in-NetBox and
    defined-but-unassigned both count as free). Search is restricted to RFC1918
    (`10/8`, `172.16/12`, `192.168/16`). "Closest" = smallest absolute numeric
    distance from the reference network. If `exact` is given and free it is
    returned first (distance 0) — the "type a subnet, try it first, else nearest"
    path.
  - **Payload**: `{"near": "10.0.5.0/24", "prefix_length": 24, "count": 20, "exact": "10.0.50.0/24", "rfc1918": true}`
    — `prefix_length` may be omitted in favor of `hosts` (number of hosts
    needed → smallest mask that fits); `count` defaults to 20; `exact` optional.
    **Size cap:** the self-service finder allows `/22` through `/30` only (a
    `/22` is the largest subnet a tenant may request). `hosts` is clamped so it
    never yields a mask smaller than `/22`; the hub rejects an explicit
    `prefix_length` outside `22..30` with HTTP 400.
  - **Response**: `{"status": "SUCCESS", "available": [{"prefix": "10.0.5.0/24", "distance": 0}, ...], "count": N}`
  - **Errors**: `near` outside RFC1918 → `{"status": "ERROR", "message": "near must be within RFC1918 ..."}`.
- **`NETBOX_CLAIM_PREFIX`**
  - **Purpose**: Assigns a specific free subnet to a tenant (the "Assign"
    action after the user picks one from the finder). If the prefix already
    exists in NetBox but has no tenant, it is reassigned (no duplicate). If it
    exists and is already tenant-assigned, the claim is refused. Otherwise the
    prefix is created with the tenant/site attached.
  - **Payload**: `{"prefix": "10.0.50.0/24", "tenant": "<netbox-tenant-slug>", "description": "...", "site": "<site-slug>", "status": "active"}`
  - **Response**: `{"status": "SUCCESS", "prefix": "10.0.50.0/24", "id": 123}`
  - **Errors**: already assigned → `{"status": "ERROR", "message": "Prefix ... is already assigned to a tenant"}`.

## Background Processes
- **KEA Sync Loop**: The spoke runs a background task every 5 minutes that polls NetBox for DHCP-enabled prefixes and pushes them to the KEA DHCP server via the `ctrl-agent` API.

## Integration Flow
1. **Command Trigger**: Hub sends a signed WebSocket message (e.g., `NETBOX_UPDATE_IP`).
2. **Execution**: `NetboxSpoke` calls `NetboxEngine`, which uses the `pynetbox` library to interact with the NetBox REST API.
3. **SOT Update**: The device or IP record is updated in NetBox.
4. **Confirmation**: A signed success/error response is returned to the Hub.

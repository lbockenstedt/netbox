# NetBox Spoke API Specification

The NetBox Spoke integrates Lab Manager with NetBox to maintain the authoritative Source of Truth (SoT) for IPAM and DCIM.

## Command Set

### IPAM & Device Management
- **`NETBOX_GET_CABLES`**
  - **Purpose**: Returns physical device-to-device cable connections, flattened
    to one device/port pair per end. Feeds the `nw` module's topology map with
    NetBox's operator-maintained cable inventory, which is the only link source
    for gear the fleet never logs into (unmanaged switches, PDUs, patch
    panels). Cables not terminating on two device interfaces are skipped.
  - **Payload**: `{"site": "<slug>"}` (optional)
  - **Response**: `{"status": "SUCCESS", "cables": [{"id", "a_device", "a_port",
    "b_device", "b_port", "status", "label"}]}`
- **`NETBOX_SYNC_CABLE`**
  - **Purpose**: Idempotently records a device-to-device link discovered
    elsewhere (nw's live LLDP topology) as a real NetBox `dcim.cable`, so an
    LLDP-confirmed adjacency persists in NetBox past nw's in-memory cache.
    Conservative: never creates a device (only an existing device's missing
    interface), and never overwrites an existing cable to a different far
    end — a human's own cable record always wins.
  - **Payload**: `{"a_device", "a_port", "b_device", "b_port", "status":
    "connected"}` (`status` optional). Device names match case-insensitively
    and ignore a domain suffix (`host.example.com` finds `HOST`).
  - **Response**: `{"status": "SUCCESS"|"UNCHANGED"|"SKIPPED"|"ERROR",
    "message": "<detail>"}`
- **`NETBOX_SYNC_LLDP`**
  - **Purpose**: Records the hub's merged LLDP topology in NetBox in one call.
    Each end is resolved by identity — SERIAL → MAC (`mac_address` cf) →
    `nw_device_id` → IP → name (case-insensitive, domain stripped). An end
    NetBox has never seen is created as a `discovered` device with
    `discovered_from=LLDP`, its MAC and its short UPPER-CASE hostname. IP-,
    MAC-, truncated (`...`) or junk names are never created. Cabling uses the
    same rules as `NETBOX_SYNC_CABLE`, so a human's cable always wins. Ends
    with legacy lower-case names are then upper-cased (see below).
  - **Payload**: `{"links": [{"a": {"name", "macs", "addresses",
    "nw_device_id", "serial"}, "a_port", "b": {...}, "b_port"}],
    "tenant_slug": "", "create_missing": true, "normalize_names": true}`
  - **Response**: `{"status": "SUCCESS"|"PARTIAL", "message", "cabled",
    "unchanged", "created", "skipped", "errors", "details": [..20], "names"}`
- **`NETBOX_NORMALIZE_DEVICE_NAMES`**
  - **Purpose**: Upper-cases every existing `dcim.device` name (idempotent).
    Every write path already stores names upper-case; this converts older
    records. A rename that would collide with an existing upper-case twin is
    skipped and reported (resolve it with "Merge duplicates").
  - **Payload**: `{"tenant_slug": ""}` (optional scope)
  - **Response**: `{"status", "message", "renamed", "collisions", "errors",
    "details"}`
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
  - **Role guessing**: a record may carry an optional `profile` (`{"sys_descr", "http_title", "http_server", "http_realm", "ssh_banner"}`). When a record has no `role` and the batch default is the generic `discovered`, the role is guessed from the profile and the hostname tokens (`netbox_profile.guess_role`: e.g. `-AP-`→`access-point`, `TOR`/`*SW`→`switch`, `PDU`, `UPS`, `ILO`/`IDRAC`→`bmc`, `PROXMOX`/`PXSVR`→`hypervisor`, `NIMBLE`/`NAS`→`storage`, an OS/web-server banner→`server`, and last a hypervisor guest MAC OUI such as Proxmox `BC:24:11` or VMware `00:50:56`→`virtual-machine`). A matched device still on `discovered` is upgraded the same way, using its NetBox name when the record has no hostname.
  - **Response**: `{"status": "SUCCESS", "pushed": N, "errors": N, "skipped": N, "deleted": N, "devices_total": N, "message": "..."}`
  - **Errors**: unknown tenant → `{"status": "ERROR", "message": "NetBox tenant '<slug>' not found ..."}`. Missing `discovered_from`/`mac_address` custom fields are tolerated (writes skipped, replace-delete becomes a safe no-op).

- **`NETBOX_RESERVE_STATIC_HOSTS`**
  - **Purpose**: Marks statically addressed hosts as DHCP reservations
    (`custom_fields.mac_address` + `dhcp_reservation: true` on their NetBox
    IP), so Kea stops handing their address to other clients. The hub
    decides when (it owns the on/off switch and the interval) and relays what
    this spoke cannot reach: each tenant's raw agent `GET_HOST_ADDRESSING`
    rows, its sweep/ARP observations, and a strict lease and subnet read of
    the tenant's Kea. This spoke reads the tenant's prefixes and IPs from
    NetBox, plans (`netbox_static_reservations.plan_static_reservations`:
    conservative, conflicts are reported and never overwritten) and writes.
  - **Payload**: `{"tenants": [{"ref": "<opaque label>", "filters":
    {"tenant"|"tenant_group": "<slug>"}, "agent_addresses": [{"ip", "mac",
    "mode"}], "sweep": [{"ip", "mac", "seen_age", "arp_age"}], "kea": "<key>",
    "replace_watch": [{"ip", "old_mac", "new_mac", "first_seen_age",
    "old_unseen_age"}]}], "kea": {"<key>": {"leases": [...], "subnets": [...]}},
    "max_writes": 25, "auto_replace": true}`. A Kea serving several tenants is
    sent once under its key. `auto_replace` absent means off.
  - **Response**: `{"status": "SUCCESS", "evaluated": ["<ref>"], "changes":
    [{"ref", "ip", "ip_id", "mac", "source", "before": {"mac_address",
    "dhcp_reservation"}, "after": {...}}], "replaced": [{"ref", "ip", "ip_id",
    "old_mac", "new_mac", "source", "before", "after", "interface": {"object",
    "id", "before": {"mac_address"}, "after": {...}} | {"object", "id",
    "left": "<reason>"} | null}],
    "pending_replace": [{"ref", "ip", "old_mac", "new_mac", "first_seen_age"}],
    "conflicts": [{"ref", "ip", "mac", "old_mac"?, "reason", "replace"?}],
    "errors": [{"ref", "ip"?, "error"}], "skipped": N,
    "skipped_reasons": {"<reason>": N}, "deferred": N}`.
    `skipped_reasons` counts the skipped IPs per reason across tenants
    (counts only, no addresses): the planner's reasons (e.g. `"outside any
    dhcp scope"`, `"no netbox ip"`, `"already reserved"`) plus `"already set
    at write"` (NetBox already held the values at the compare-and-set) and
    `"no netbox ip id"`.
    `status` is `"ERROR"` (with a `message`) whenever `errors` is not empty,
    so a pass where every read failed never looks like an empty success.
    This spoke always sends `replaced` and `pending_replace` (empty lists
    when there are none); a hub reads their absence as an older spoke.
  - **Device replacement**: an IP NetBox reserves for old MAC A whose host
    now answers with new MAC B (a new NIC, server, iLO or switch at the same
    address) is a conflict with `old_mac: A` and a `replace` reason, unless
    it can be replaced. It is replaced (reported in `replaced`) only when
    `auto_replace` is on, the IP has `dhcp_reservation: true`, every source
    reports exactly B and says static, B passes every check a new
    reservation needs (with Kea's reservation of A at this IP allowed, since
    the sync pushed it from NetBox), A holds no Kea lease, is reserved at no
    other address in NetBox or Kea and is not reported by any source in this
    pass, and the hub's `replace_watch` row for the IP names the same A and B
    with `first_seen_age` ≥ 1800 (B seen on two passes 30 minutes apart) and
    `old_unseen_age` ≥ 86400 (A unseen for a day; null means the hub cannot
    vouch, so no). Otherwise an eligible IP is listed in `pending_replace`.
    The hub keeps that waiting state (see lm `docs/dhcp.md`); this spoke
    keeps none. A replacement compare-and-sets the IP's fields A→B, then the
    MAC of the interface the IP is assigned to when it is A: the primary
    `dcim.mac-addresses` record on NetBox 4.2+ (the interface's own
    `mac_address` is read-only there), or the interface's `mac_address` on
    older NetBox (`dcim.interface` or `virtualization.vminterface`). It takes
    two of the `max_writes` slots. If that interface changed in between,
    the IP still moves and `interface` is `{"object", "id", "left": reason}`
    (no undo). If the interface write fails, the rollback also
    compare-and-sets the interface back, in case NetBox took it before
    failing. The IP is written first: if the spoke process stops between
    the two writes, the interface keeps A and is fixed by hand or by a repeated replace request for that
    IP; later passes see the IP as already reserved. DHCP is unaffected,
    since Kea reads only the IP's fields. Host
    reservations held only in Kea are never touched.
  - **Reversible**: each write is a compare-and-set on the two fields,
    compared exactly as stored (other custom fields are kept). An IP that
    another writer changed or deleted after the read is a conflict
    (`changed in netbox since it was read` / `netbox ip deleted since it was
    read`) and the pass goes on. If NetBox refuses a write, the pass stops,
    its earlier writes are rolled back newest first, and the reply is
    `status: "ERROR"` with `changes: []`, `rolled_back`, `rollback_kept` and
    `rollback_failed`; that address is not retried for an hour. Payload
    `{"rollback": [<changes>]}` undoes a pass (the hub sends it when the
    DHCP sync after the pass fails) and replies `{"status", "rolled_back",
    "rollback_kept", "rollback_failed"}`: `rolled_back` were restored to
    `before` (or already were, so repeating a rollback is harmless);
    `rollback_kept` were left alone with a `reason` because NetBox changed
    since (a later edit is never undone) or the IP is gone; only
    `rollback_failed` (NetBox refused, with an `error`) makes the status
    `ERROR`. A replacement rolls back its interface MAC first, then the IP,
    each only where NetBox still holds `after`; when either is kept or
    fails, the `reason`/`error` names both (`interface: ...; ip: ...`), and
    any kept or failed rollback still drops the IP list cache. The IP list cache is dropped
    after any write.
  - **Older spokes** answer `Unknown command: NETBOX_RESERVE_STATIC_HOSTS`;
    the hub then reports the feature as unsupported and writes nothing.

- **`NETBOX_REPLACE_RESERVATION_MAC`**
  - **Purpose**: The "Use new MAC" button (lm System → Sync): moves one
    IP's reservation, and its interface MAC, from `old_mac` to `new_mac`
    now. It skips the waits of the automatic replacement (`first_seen_age`,
    `old_unseen_age`) but keeps every other check, the compare-and-set and
    the rollback, and writes nothing else. The IP must already be a
    reservation (`dhcp_reservation: true`) for `old_mac`.
  - **Payload**: one tenant in the `NETBOX_RESERVE_STATIC_HOSTS` format plus
    the IP: `{"tenants": [{"ref", "filters", "agent_addresses", "sweep",
    "kea"}], "kea": {...}, "ip", "old_mac", "new_mac"}`.
  - **Response**: the pass report. `status: "SUCCESS"` with the one row in
    `replaced`; `status: "SUCCESS"` with `already: true` when this run
    wrote and failed nothing, the sources report only `new_mac`, NetBox
    already reserves the IP for it (a repeated request, or another run
    moved the IP between this run's checks and its write) and the
    replacement still passes every new- and old-MAC check (Kea may already
    hold `new_mac` there). An interface still on `old_mac` is moved then;
    if that write fails it is put back (or listed in `rollback_failed`).
    `replaced` then holds the move as a fresh run would report it, so the
    hub can still roll the IP back. An interface found already on
    `new_mac` is reported as `{object, id, left: "interface already has
    the new mac"}` and never undone, since this run cannot tell who set it;
    else `status: "ERROR"` with a `message` such as `cannot
    replace: old mac holds a lease` or `cannot replace: macs changed since
    the last pass; refresh`. The hub undoes it with
    `NETBOX_RESERVE_STATIC_HOSTS {"rollback": [<replaced row>]}` if the
    DHCP sync after it fails. Identical requests in flight share one run.
  - **Older spokes** answer `Unknown command: NETBOX_REPLACE_RESERVATION_MAC`.

### Tenancy
- **`NETBOX_GET_TENANTS`**
  - **Purpose**: Lists NetBox tenants for the hub's tenant sync and the tenant
    picker. Each tenant is tagged with the tenant group that owns it, so the hub
    can nest tenants under their group without a second round trip.
  - **Payload**: `{}`
  - **Response**: `{"status": "SUCCESS", "tenants": [{"id", "name", "slug",
    "description", "group_slug", "group_name"}]}` — `group_slug`/`group_name`
    are `""` for an ungrouped tenant.
- **`NETBOX_GET_TENANT_GROUPS`**
  - **Purpose**: Lists NetBox **tenant groups** so the hub can offer a group as
    a selectable pseudo-tenant. Selecting a group shows the **union** of its
    member tenants, which is how a user can be assigned to a whole group
    instead of one tenant.
  - **Membership is TRANSITIVE and rolls UPWARD**: a group's `tenant_slugs`
    contains its own tenants **plus** every tenant of a descendant group. A
    child group never inherits its parent's tenants. (Matches NetBox's own
    tree-aware `?tenant_group=` filter.)
  - **Payload**: `{}`
  - **Response**: `{"status": "SUCCESS", "groups": [{"id", "name", "slug",
    "description", "parent_slug", "tenant_slugs": ["<slug>", ...],
    "tenant_count": N}]}` — sorted by name; `parent_slug` is `""` at the root.
  - **Notes**: The hub stores each group as its own tenant keyed
    `group:<slug>`. `tenant_slugs` is used only to authorize writes (you cannot
    create an object *into* a group) — reads use the `tenant_group` filter
    below.

**Tenant-group scoping on list commands.** `NETBOX_GET_RACKS`,
`NETBOX_GET_DEVICES`, `NETBOX_GET_PREFIXES` and `NETBOX_GET_IPS` accept an
optional **`tenant_group`** slug alongside the existing `tenant` slug.
`tenant_group` **supersedes** `tenant` when both are sent, and maps to NetBox's
`?tenant_group=` query filter, which unions the group and its sub-groups
server-side. Sending neither is unscoped (all tenants), exactly as before — so
an older hub that never sends `tenant_group` is unaffected.

- **`NETBOX_COUNT`**
  - **Purpose**: A tenant's device, prefix and IP-address counts for the hub's
    admin overview, from NetBox's own `count` (one `limit=1` read per type)
    instead of fetching every row.
  - **Payload**: `{"tenant": "<slug>"?, "tenant_group": "<slug>"?}` — scoped like
    the list commands above; neither counts all tenants.
  - **Response**: `{"status": "SUCCESS", "devices": N, "prefixes": N,
    "ip_addresses": N}`, or `{"status": "ERROR", "message": ...}` when any read
    fails or has no count (never a zero). Cached and shared like the list reads.
    An older spoke answers "Unknown command"; the hub then counts the full lists.

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

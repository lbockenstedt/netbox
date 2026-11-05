"""Virtualization / Proxmox VM sync + custom-field provisioning for NetboxEngine."""
# Standard-library logging.
import logging
# UTC timestamps.
from datetime import datetime, timezone
# Type hints.
from typing import Any, Dict, List, Optional

# Shared list of custom fields and reference objects to provision.
from custom_fields_spec import CUSTOM_FIELDS_SPEC, REFERENCE_OBJECTS_SPEC

# Shares the engine's logger name so all engine messages go to one place.
logger = logging.getLogger("NetboxEngine")


class VmSyncMixin:
    """Virtualization / Proxmox VM sync + custom-field provisioning for NetboxEngine."""

    def create_vm_entry(self, name: str, cluster: str, vcpus: int, ram: int) -> Dict[str, Any]:
        """Create a placeholder device for a VM at the site named after its cluster."""
        # Any API failure becomes an ERROR envelope.
        try:
            # Look up the site.
            site = self.nb.dcim.sites.get(name=cluster)
            # Unknown site: error.
            if not site:
                return {"status": "ERROR", "message": f"Site {cluster} not found"}
            # Role and device type used for VM placeholders.
            role = self.nb.dcim.device_roles.get(name="Virtual Machine")
            dev_type = self.nb.dcim.device_types.get(model="Virtual Machine")
            # Create the device, with sizing in the description.
            vm = self.nb.dcim.devices.create(
                name=name.strip().upper(),
                device_type=dev_type.id if dev_type else None,
                role=role.id if role else None,
                site=site.id,
                description=f"vCPUs: {vcpus}, RAM: {ram}GB",
            )
            # New device id.
            return {"status": "SUCCESS", "vm_id": vm.id, "name": name}
        except Exception as e:
            # API failure.
            return {"status": "ERROR", "message": str(e)}

    # ─── Virtualization – Proxmox VM sync ──────────────────────────────────────
    # The hub's VmSyncMixin pulls a tenant's VMs from the pxmx (Proxmox) spoke
    # and relays them here via NETBOX_SYNC_VMS so NetBox's virtualization
    # records mirror the live hypervisor inventory. VMs are matched by the
    # `proxmox_unique_id` custom field (created if missing, updated if present),
    # clusters are auto-created under a 'Proxmox' cluster type, and primary_ip4
    # is set from each VM's first IP. Authoritative replace-with-delete runs only
    # when tenant-scoped (a NetBox tenant slug is provided) so a global sync
    # can't delete another tenant's VM records.
    @staticmethod
    def _vm_status_map(s: str) -> str:
        """Proxmox VM status → NetBox VM status value."""
        # Normalise and map the status.
        s = str(s or "").lower()
        if s == "running":
            return "active"
        if s in ("stopped", "paused", "suspended"):
            return "offline"
        return "active"

    # Custom fields the Lab Manager syncs write to. Provisioned idempotently
    # at spoke startup by _ensure_custom_fields() so the Proxmox/Hypervisor→IPAM
    # and Firewall→IPAM syncs don't 400 on a missing custom field (the
    # installer also provisions these, but the deployed external NetBox is
    # reached spoke-only where the installer's Django-shell step doesn't run —
    # this is the self-healing safety net). The list itself lives in
    # ``custom_fields_spec.CUSTOM_FIELDS_SPEC`` — the ONE source of truth shared
    # with install.sh (Django-shell + REST blocks) and the WebUI "Apply schema
    # changes" button, so a fresh install, an update, and the button all
    # provision exactly the same set. (name, type, label, content_type).
    _REQUIRED_CUSTOM_FIELDS = CUSTOM_FIELDS_SPEC

    # How long a clean custom-field verify is trusted before the idempotent
    # ensure re-runs. Bounds self-heal latency if a field is deleted in NetBox
    # after the flag was set (was once-per-process → never re-verified until a
    # process restart). The ensure is cheap (one list-all), so a modest window
    # is fine. Override via LM_NETBOX_CF_ENSURE_TTL (seconds).
    # Read the TTL from the environment once at class definition.
    import os as _os
    _CF_ENSURE_TTL = float(_os.environ.get("LM_NETBOX_CF_ENSURE_TTL", "900") or 900)
    del _os

    def _cf_types_list(self, cf: Any, types_key: str) -> Any:
        """Read the attached content-type list off a custom-field record,
        tolerant of the NetBox 4.x ``content_types`` → ``object_types`` REST
        rename. ``types_key`` is the best guess; if the record doesn't expose
        it, fall back to the other attr name so a record cached from a
        mixed-version box still resolves."""
        # Try the expected attribute name.
        val = getattr(cf, types_key, None)
        # Fall back to the other name.
        if val is None:
            alt = "content_types" if types_key == "object_types" else "object_types"
            val = getattr(cf, alt, None)
        # Empty list when neither exists.
        return val or []

    def _cf_create(self, cf_api: Any, name: str, ftype: str, label: str,
                   content_type: str, types_key: str) -> tuple:
        """Create a custom field, flipping ``object_types``↔``content_types``
        once if the first key is rejected. NetBox 4.x uses ``object_types``;
        3.x uses ``content_types`` and 400s an unknown ``object_types``. Returns
        ``(cf, key_used)``; re-raises the last error if both attempts fail (the
        caller logs it + records the warning). Only retries when the error
        looks like a serializer field-name rejection, so a permission/timeout
        failure isn't double-attempted and its real error is preserved."""
        # Try the given key first, then the other one.
        keys = [types_key]
        if types_key == "object_types":
            keys.append("content_types")
        elif types_key == "content_types":
            keys.append("object_types")
        # Last error seen, re-raised if both attempts fail.
        last_err: Optional[Exception] = None
        # Try each key in turn.
        for key in keys:
            try:
                # Success: the new field and the key that worked.
                return cf_api.create(name=name, type=ftype, label=label,
                                     **{key: [content_type]}), key
            except Exception as e:
                # Remember the error.
                last_err = e
                # Only retry if it looks like a field-name rejection.
                msg = str(e).lower()
                if not any(tok in msg for tok in (
                        "object_types", "content_types",
                        "valid field", "unexpected", "required")):
                    break  # not a field-name rejection → don't retry
        # Both attempts failed.
        assert last_err is not None
        raise last_err

    def _ensure_custom_fields(self, force: bool = False) -> Dict[str, Any]:
        """Ensure each custom field in CUSTOM_FIELDS_SPEC exists AND is attached
        to its content type on NetBox. Idempotent + safe to re-run any number
        of times (the contract the WebUI "Apply schema changes" button relies
        on: never errors if the changes are already there).

        A field can exist globally but be unassigned to the object type — in
        that case NetBox rejects writes with "Custom field 'X' does not exist
        for this object type." (the exact sync_vms failure seen after the
        fields were created without content_types). So this get-or-creates each
        field AND verifies/attaches its content_type. Best-effort: a permission
        / API error is logged at WARNING (so it's visible) and swallowed — a
        restricted token must never break a sync. Safe at startup and reconnect.

        Called at spoke startup + reconnect (netbox_spoke.py) AND at the top of
        ``sync_vms`` / ``sync_devices`` so a sync self-heals even if the startup
        call was skipped or failed. Cached per-process via ``_cf_ensured``: a
        clean run (every field present AND attached) sets the flag so subsequent
        syncs skip the list-all; a run with any WARNING leaves it unset so the
        next sync retries (self-healing until the provisioning gap closes).

        ``force=True`` bypasses the per-process cache and re-runs the full
        verify/attach pass — used by the NETBOX_PROVISION_CUSTOM_FIELDS command
        (the WebUI button) so a manual apply always re-checks every field.

        Returns a report dict: {status, total, present, created, attached,
        already_attached, warnings}. ``status`` is "SUCCESS" (everything
        present+attached, no warnings), "PARTIAL" (some warnings), or "ERROR"
        (the field list itself couldn't be fetched).
        """
        # Start with an all-clear report.
        report: Dict[str, Any] = {
            "status": "SUCCESS", "total": len(self._REQUIRED_CUSTOM_FIELDS),
            "present": 0, "created": 0, "attached": 0,
            "already_attached": 0, "warnings": [],
        }
        # Cache age and TTL.
        import time as _time
        _cf_ttl = getattr(self, "_CF_ENSURE_TTL", 900.0)
        _cf_ts = getattr(self, "_cf_ensured_ts", 0.0)
        # A set flag with no timestamp (ts == 0) means "trust the flag" — the
        # clean-run branch below always co-sets flag + ts, so in real runs a set
        # flag carries a real ts and the TTL bounds the skip window; ts == 0 only
        # arises from an externally-set flag.
        _cf_fresh = _cf_ts == 0.0 or (_time.time() - _cf_ts) < _cf_ttl
        # Fresh clean run: report everything present and skip the API.
        if not force and getattr(self, "_cf_ensured", False) and _cf_fresh:
            # Recent clean run (within _CF_ENSURE_TTL) — everything was present+
            # attached. Report it as such rather than re-hitting the API. The
            # TTL bound means the idempotent verify still re-runs periodically,
            # so a custom field later deleted in NetBox self-heals on the next
            # sync past the window instead of 400ing forever until restart.
            report["already_attached"] = report["total"]
            report["present"] = report["total"]
            return report
        # Set when any step fails, so the cache is not marked clean.
        had_failure = False
        # List every custom field already defined.
        try:
            cf_api = self.nb.extras.custom_fields
            by_name = {str(f.name): f for f in cf_api.all()}
        except Exception as e:
            # List failed: report ERROR.
            logger.warning("ensure_custom_fields: list failed: %s", e)
            report["status"] = "ERROR"
            report["warnings"].append(f"list failed: {e}")
            return report  # leave _cf_ensured unset → next sync retries
        # NetBox 4.x renamed the CustomField REST serializer field
        # ``content_types`` → ``object_types``. The old code sent
        # ``content_types`` on create, which 4.x ignores → it 400s with
        # ``{'object_types': ['This field is required.']}``, so NO field is ever
        # created and every sync_vms/sync_devices custom_fields write then 400s
        # "Custom field 'X' does not exist for this object type." Detect which
        # key this NetBox speaks from any pre-existing record; default to
        # ``object_types`` (4.x) and let ``_cf_create`` flip to ``content_types``
        # on a 3.x box that rejects it. Cached on the engine after the first run.
        # Work out which content-type key this NetBox uses (cached after first run).
        types_key = getattr(self, "_cf_types_key", None)
        if types_key is None:
            types_key = "object_types"
            # Detection is best-effort; default to the 4.x key.
            try:
                for f in by_name.values():
                    # hasattr() on a pynetbox record triggers full_details() (a
                    # per-id GET) when the attr isn't in the list payload — that
                    # call can fail INDEPENDENTLY of cf_api.all() above if NetBox
                    # flaps down between the list and the detail fetch
                    # (ConnectionRefused). Default to the 4.x key on any failure;
                    # _cf_create flips to content_types on a 3.x box that rejects
                    # object_types.
                    if hasattr(f, "content_types") and not hasattr(f, "object_types"):
                        types_key = "content_types"
                        break
            except Exception as e:
                # Detection failed: keep the default.
                logger.warning(
                    "ensure_custom_fields: content_types/object_types detection "
                    "failed (%s); defaulting to object_types", e)
            # Cache the detected key.
            self._cf_types_key = types_key
        # Create or verify each required field.
        for name, ftype, label, content_type in self._REQUIRED_CUSTOM_FIELDS:
            cf = by_name.get(name)
            # Missing: create it.
            if cf is None:
                try:
                    cf, types_key = self._cf_create(
                        cf_api, name, ftype, label, content_type, types_key)
                    self._cf_types_key = types_key
                    logger.info("ensure_custom_fields: created %s on %s", name, content_type)
                    report["created"] += 1
                except Exception as e:
                    # Create failed: warn and move on.
                    logger.warning("ensure_custom_fields: create %s failed: %s", name, e)
                    report["warnings"].append(f"create {name} on {content_type}: {e}")
                    had_failure = True
                    continue
            else:
                # Already present.
                report["present"] += 1
            # Verify the content type is attached (create may not attach it on
            # every NetBox version; a pre-existing field may be unattached).
            # Read/write the detected key (object_types on 4.x, content_types on
            # 3.x), falling back to the other attr so a record fetched from a
            # mixed-version cache still resolves.
            # Attach the content type if it is missing.
            try:
                current = list(self._cf_types_list(cf, types_key))
                # Not attached yet: attach and save.
                if content_type not in current:
                    setattr(cf, types_key, current + [content_type])
                    cf.save()
                    logger.info("ensure_custom_fields: attached %s to %s",
                                name, content_type)
                    report["attached"] += 1
                else:
                    # Already attached.
                    report["already_attached"] += 1
            except Exception as e:
                # Attach failed: warn.
                logger.warning("ensure_custom_fields: attach %s to %s failed: %s",
                               name, content_type, e)
                report["warnings"].append(f"attach {name} to {content_type}: {e}")
                had_failure = True
        # Reference objects the syncs rely on (e.g. the fallback site), from the
        # same spec install.sh provisions.
        # Create any missing reference objects.
        for path, slug, body in REFERENCE_OBJECTS_SPEC:
            try:
                # Find the endpoint from the API path.
                app, ep = path.strip("/").split("/")[1:3]
                endpoint = getattr(getattr(self.nb, app), ep)
                # Create it if missing.
                if endpoint.get(slug=slug) is None:
                    endpoint.create(**body)
                    logger.info("ensure_custom_fields: created %s %s", path, slug)
                    report["created"] += 1
            except Exception as e:
                # Failed: warn.
                logger.warning("ensure_custom_fields: reference %s %s failed: %s", path, slug, e)
                report["warnings"].append(f"reference {path} {slug}: {e}")
                had_failure = True
        # Any failure: mark PARTIAL and leave the cache unset so the next sync retries.
        if had_failure:
            report["status"] = "PARTIAL"
            self._cf_ensured = False  # retry next sync
            self._cf_ensured_ts = 0.0
        else:
            # Clean run: mark the cache with the current time.
            self._cf_ensured = True
            self._cf_ensured_ts = _time.time()  # bound the skip window
        # Final report.
        return report

    @staticmethod
    def _uniq_device_name(base: str, mac: str, real_ip: str,
                          existing_by_name: Dict[str, dict],
                          used_names: set) -> str:
        """Uniquify ``base`` against pre-existing device names AND names already
        used this batch so the NetBox ``(name, site, tenant)`` unique constraint
        can't fire on a create.

        Many firewall-discovered records share a hostname (ks205, sonoszp,
        iphone…) across distinct MACs — genuinely different devices that the
        constraint forces to distinct names. Appends ``-<mac[-4:]>`` (or
        ``-<ip>`` when there's no MAC); if that still collides, a ``-<n>``
        counter guarantees uniqueness. Returns ``base`` unchanged when it
        doesn't collide with either set.
        """
        # Normalise the base name.
        base = str(base or "").strip().upper()
        key = base.lower()
        # No collision: use it as-is.
        if key not in existing_by_name and key not in used_names:
            return base
        # Add a MAC (or IP) suffix.
        suffix = (mac.replace(":", "")[-4:] if mac else (real_ip or "x")).upper()
        cand = f"{base}-{suffix}"
        # Add a counter until the name is unique.
        i = 2
        while cand.lower() in existing_by_name or cand.lower() in used_names:
            cand = f"{base}-{suffix}-{i}"
            i += 1
        # Unique name.
        return cand

    def _ensure_cluster_type(self, name: str = "Proxmox", slug: str = "proxmox"):
        """Return the 'Proxmox' cluster type (creating it if missing). Best-effort."""
        # Best-effort: failures return None.
        try:
            # Reuse the existing type.
            ct = self.nb.virtualization.cluster_types.get(name=name)
            if ct:
                return ct
            # Otherwise create it.
            return self.nb.virtualization.cluster_types.create(name=name, slug=slug)
        except Exception as e:
            # Failed: log and return None.
            logger.debug("ensure_cluster_type failed: %s", e)
            return None

    def _ensure_vm_cluster(self, name: str, tenant=None) -> Optional[int]:
        """Return a NetBox cluster id for ``name``, auto-creating it under the
        'Proxmox' cluster type. None if it can't be resolved/created."""
        # No name: nothing to resolve.
        if not name:
            return None
        # Best-effort: failures return None.
        try:
            # Reuse an existing cluster.
            c = self.nb.virtualization.clusters.get(name=name)
            if c:
                return c.id
            # Otherwise create it with the Proxmox type and the tenant.
            ctype = self._ensure_cluster_type()
            kwargs: Dict[str, Any] = {"name": name}
            if ctype:
                kwargs["type"] = ctype.id
            if tenant:
                kwargs["tenant"] = tenant.id
            # Create and return its id.
            c = self.nb.virtualization.clusters.create(**kwargs)
            return c.id
        except Exception as e:
            # Failed: log and return None.
            logger.debug("ensure_vm_cluster %s failed: %s", name, e)
            return None

    # ── perf FIX C: batching helpers (kill the per-VM N+1s) ────────────────────

    def _hydrate_record(self, row: dict, endpoint):
        """Rebuild a pynetbox ``Record`` from a raw list-row dict so a matched
        object can be updated/deleted WITHOUT re-fetching it by id — the full
        row already came from the one paginated ``_api_get_all`` listing.
        Falls back to a per-id GET when the row can't hydrate (no ``url`` key —
        e.g. a minimal row in tests; real API rows always carry it)."""
        # Build a Record from the row when it has a URL.
        try:
            if isinstance(row, dict) and row.get("url"):
                from pynetbox.core.response import Record
                return Record(row, self.nb, endpoint)
        except Exception as e:
            # Hydration failed: log and fall through.
            logger.debug("hydrate from row failed, falling back to GET: %s", e)
        # Fall back to a GET by id.
        return endpoint.get(row["id"])

    def _prefetch_vm_interfaces(self):
        """One paginated fetch of ALL vminterfaces, bucketed by VM id, so
        ``_assign_vm_primary_ip4`` doesn't issue a filter GET per VM. Returns
        the bucket dict, or None on failure (→ per-VM filter fallback)."""
        # Bucket every vminterface by its VM id.
        try:
            buckets: Dict[int, list] = {}
            for row in self._api_get_all("/api/virtualization/interfaces/",
                                         {"limit": 500}):
                # VM reference may be nested or a bare id.
                vmref = (row or {}).get("virtual_machine")
                vmid = vmref.get("id") if isinstance(vmref, dict) else vmref
                if vmid:
                    buckets.setdefault(int(vmid), []).append(row)
            # All buckets.
            return buckets
        except Exception as e:
            # Fetch failed: caller falls back to per-VM lookups.
            logger.debug("sync_vms: vminterface prefetch failed "
                         "(per-VM filter fallback): %s", e)
            return None

    def _assign_vm_primary_ip4(self, vm_obj, vm: dict, tenant=None):
        """Build the VM's interfaces in NetBox from the per-interface records
        the pxmx agent gathers, set ``primary_ip4`` from the first IP, and
        journal-stamp each created vminterface + IP.

        ``vm["interfaces"]`` is ``[{name, mac, ips:[..]}, ...]`` (pxmx agent
        ``_vm_interfaces``). For each interface a vminterface is reused-by-name
        (or created) carrying the native ``mac_address``; each guest IP becomes
        an ``ipam.ip_address`` assigned to that vminterface via
        ``_reuse_or_create_ip`` (global-IP uniqueness respected) and tagged with
        the interface MAC. Falls back to a single ``eth0`` vminterface + the
        legacy flat ``vm["ips"]`` list when the agent sent no interface records
        (older spoke). Never raises — a missing/unassignable IP must not break
        the VM record it follows.

        Returns ``(build_failures, first_build_err)``: the count of vminterface
        / IP / primary_ip4 build failures and the first one's human-readable
        text (None when 0). A real-world pynetbox failure (e.g. the IP already
        exists in IPAM assigned to a discovered dcim.device, so reuse/reassign
        raises) used to be swallowed at DEBUG with nothing reported back — the
        VM ended up IP-less with 0 errors. The first failure is now logged at
        WARNING (``[sync-error]``) so it reaches the spoke log + GET_ERROR_LOGS;
        the caller folds the count + first error into the sync result."""
        # Interfaces the agent reported.
        ifaces_in = list((vm or {}).get("interfaces") or [])
        # Back-compat: an older pxmx agent that sent only a flat ``ips`` list
        # (no per-interface MAC) → one eth0 vminterface holding those IPs.
        if not ifaces_in:
            flat_ips = list((vm or {}).get("ips") or [])
            if flat_ips:
                ifaces_in = [{"name": "eth0", "mac": "", "ips": flat_ips}]
        # Nothing to build.
        if not ifaces_in:
            # Honor the (build_failures, first_build_err) contract documented
            # below — a bare ``return`` yields None, and the caller unpacks it
            # as ``ip_fail, ip_err = None`` → TypeError "cannot unpack
            # non-iterable NoneType", which surfaced as "upsert <uid>: cannot
            # unpack non-iterable NoneType" for VMs the agent reported with no
            # interfaces AND no flat ips (e.g. the leaked pxmx-cs-svr-02 record
            # or a powered-off VM QGA couldn't introspect).
            return 0, None
        try:
            # pynetbox accessor is ``virtualization.interfaces`` — the REST
            # endpoint is /api/virtualization/interfaces/ (VMInterfaceViewSet),
            # NOT /api/virtualization/vminterfaces/ (which 404s "could not be
            # found" and left every VM IP-less). The model is ``vminterface``
            # (the content-type string used in assigned_object_type below), but
            # the endpoint path is ``interfaces`` — a name collision masked by
            # the mocked tests, which is why this 404'd in production only.
            # perf FIX C: inside a sync_vms run all vminterfaces were bulk-
            # fetched once (_prefetch_vm_interfaces) — hydrate this VM's bucket
            # instead of a filter GET per VM. Outside a run (cache None) the
            # per-VM filter is unchanged.
            # Use the prefetched interfaces when available, else fetch them for this VM.
            pre = getattr(self, "_vmiface_prefetch", None)
            if pre is not None:
                existing = [self._hydrate_record(
                                r, self.nb.virtualization.interfaces)
                            for r in pre.get(int(vm_obj.id), [])]
            else:
                existing = list(self.nb.virtualization.interfaces.filter(
                    virtual_machine_id=vm_obj.id))
        except Exception as e:
            # Listing failed: treat as none.
            logger.debug("assign_vm_primary_ip4: list vminterfaces %s failed: %s",
                         vm_obj.id, e)
            existing = []
        # Existing interfaces by name, and the first IP id for primary_ip4.
        by_name = {getattr(i, "name", ""): i for i in existing}
        first_ip_id = None
        # Surface IP/interface build failures instead of swallowing them at
        # DEBUG: a real-world pynetbox failure (e.g. the VM's IP already exists
        # in IPAM assigned to a discovered dcim.device, so the reuse/reassign
        # path raises) used to leave the VM IP-less with 0 reported errors —
        # impossible to diagnose. Count failures + capture the first error text
        # so sync_vms can fold them into its result + [sync-error] marker.
        # Failure count, first error text and the VM name for messages.
        build_failures = 0
        first_build_err: Optional[str] = None
        vm_name = str(getattr(vm_obj, "name", "") or "")

        def _record_fail(msg: str, exc: BaseException) -> None:
            """Count a build failure, keep the first message and log it."""
            nonlocal build_failures, first_build_err
            # Count it and keep the first message.
            build_failures += 1
            if first_build_err is None:
                first_build_err = msg
            # First failure → WARNING (lands in the spoke log + GET_ERROR_LOGS);
            # later ones → DEBUG (avoid log spam on a batch-wide outage).
            if build_failures == 1:
                logger.warning("[sync-error] assign_vm_primary_ip4: %s: %s", msg, exc)
            else:
                logger.debug("assign_vm_primary_ip4: %s: %s", msg, exc)

        # Build each interface.
        for ifc in ifaces_in:
            # Interface name, normalised MAC and bare IPs.
            name = str(ifc.get("name") or "").strip() or "eth0"
            mac = self._norm_mac(str(ifc.get("mac") or ""))
            ips = [str(x).split("/")[0].strip() for x in (ifc.get("ips") or [])
                   if str(x or "").strip()]
            # Skip interfaces with neither IPs nor a MAC.
            if not ips and not mac:
                continue
            # Interface create/update failures are recorded and the interface skipped.
            try:
                # Reuse by name or create a new vminterface.
                iface = by_name.get(name)
                if iface is None:
                    # New interface, with its MAC if known.
                    kw: Dict[str, Any] = {"virtual_machine": vm_obj.id, "name": name}
                    if mac:
                        kw["mac_address"] = mac
                    # Create and journal it.
                    iface = self.nb.virtualization.interfaces.create(**kw)
                    self._journal("virtualization.vminterface", iface.id,
                                   "hypervisor-vm-sync",
                                   note=f"vminterface {name} for VM "
                                        f"{getattr(vm_obj, 'name', '')}")
                else:
                    # Refresh the MAC if the interface exists but is MAC-less.
                    if mac and not getattr(iface, "mac_address", None):
                        # Set the MAC; failure is only logged.
                        try:
                            iface.mac_address = mac
                            iface.save()
                        except Exception as e:
                            # MAC refresh failed.
                            logger.debug("assign_vm_primary_ip4: mac refresh %s: %s",
                                         name, e)
            except Exception as e:
                # Record the failure and skip this interface.
                _record_fail(f"vminterface {name} for VM {vm_name} failed", e)
                continue
            # Create or reuse each IP on the interface.
            for ip_str in ips:
                # Skip blanks.
                if not ip_str:
                    continue
                # IP failures are recorded and skipped.
                try:
                    # Full address with the matching prefix mask.
                    mask = self._mask_for_ip(ip_str, tenant)
                    full = ip_str if "/" in ip_str else f"{ip_str}/{mask}"
                    # Assign the IP to the interface.
                    ip_kwargs: Dict[str, Any] = {
                        "address": full,
                        "assigned_object_type": "virtualization.vminterface",
                        "assigned_object_id": iface.id,
                    }
                    # Attribute it to the tenant.
                    if tenant:
                        ip_kwargs["tenant"] = tenant.id
                    # Reuse an existing IP or create one.
                    ip_obj = self._reuse_or_create_ip(
                        full, ip_kwargs, ip_str, iface.id, tenant,
                        hostname=vm_name,
                        mac=mac, source="hypervisor-vm-sync",
                        iface_type="virtualization.vminterface")
                    # Journal the IP.
                    self._journal("ipam.ipaddress", ip_obj.id,
                                   "hypervisor-vm-sync",
                                   note=f"VM {vm_name} {name}")
                    # Remember the first IP for primary_ip4.
                    if first_ip_id is None:
                        first_ip_id = getattr(ip_obj, "id", None)
                except Exception as e:
                    # Record the failure.
                    _record_fail(f"IP {ip_str} on {name} for VM {vm_name} failed", e)
        # Set primary_ip4 from the first IP.
        if first_ip_id is not None:
            try:
                # PATCH only primary_ip4 via a targeted endpoint update — NOT a
                # full ``vm_obj.save()``. pynetbox serializes the whole record on
                # ``save()``, including whatever ``custom_fields`` are loaded on
                # the object. sync_vms sets the proxmox_*/last_seen CFs best-effort
                # just before this call and SWALLOWS the 400 when a field isn't
                # yet attached to virtualization.virtualmachine on the deployed
                # NetBox — but the unattached CFs stay on the Python object, so a
                # full ``save()`` here re-sends them and 400s the whole request,
                # leaving the VM without a primary IP ("Custom field 'last_seen'
                # does not exist for this object type"). A targeted update sends
                # ONLY primary_ip4, so the separately-applied custom_fields can
                # never break the primary-IP assignment (and can't wipe attached
                # CFs on a healthy box either).
                # pynetbox >=7's Endpoint.update() is a BULK op — a LIST of
                # dicts each with its id. A single dict raises "Objects passed
                # must be list[dict|Record]". Wrap the one dict in a list.
                self.nb.virtualization.virtual_machines.update(
                    [{"id": vm_obj.id, "primary_ip4": first_ip_id}])
            except Exception as e:
                # Record the failure.
                _record_fail(f"set primary_ip4 for VM {vm_name} failed", e)
        # Failure count and first error.
        return build_failures, first_build_err

    # Per-tenant breakdown key used when a VM carries no tenant slug (untagged
    # / no NetBox tenant). Mirrors the hub's VmSyncMixin._VM_SYNC_UNASSIGNED_KEY.
    _VM_SYNC_UNASSIGNED_KEY = "__unassigned__"

    def sync_vms(self, vms: list, tenant_slug: str = "",
                 replace: bool = False,
                 source_of_truth: str = "external") -> Dict[str, Any]:
        """Push a set of Proxmox VMs into NetBox virtualization records (grab-all).

        Each incoming VM carries its own ``tenant_slug`` (None/'' → created with
        no NetBox tenant, i.e. a global/unassigned record). The batch
        ``tenant_slug`` is only a fallback for VMs that don't carry one (legacy
        callers). Each VM is matched by ``custom_fields.proxmox_unique_id`` —
        created if missing, updated if present; a VM that changed tenants just
        gets its ``tenant`` updated (never deleted-and-recreated). Clusters are
        auto-created; ``primary_ip4`` is set from the first IP in each VM's
        ``ips`` list.

        When ``replace`` is set, NetBox VMs carrying our ``proxmox_unique_id``
        custom field whose uid is NOT in the incoming full set are deleted
        (cluster-wide — the VM was destroyed in Proxmox). Manually-created
        NetBox VMs (no ``proxmox_unique_id``) are never touched, so a global
        sync can't delete records it doesn't own.

        Returns ``{status, pushed, errors, skipped, deleted, vms_total,
        message, per_tenant}`` where ``per_tenant`` maps tenant-slug (or
        ``__unassigned__``) → ``{pushed, errors, skipped, deleted, vms_total}``
        so the hub can record per-tenant last-sync status from one batch.
        """
        # Running totals and the first per-record error.
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None   # first per-record failure text (diagnosability)
        # Per-tenant breakdown.
        per_tenant: Dict[str, Dict[str, int]] = {}
        UNASSIGNED = self._VM_SYNC_UNASSIGNED_KEY

        def _bucket(slug: Optional[str]) -> Dict[str, int]:
            """Per-tenant counters for ``slug``, created on first use."""
            # Unassigned VMs share one bucket.
            key = str(slug or "").strip() or UNASSIGNED
            b = per_tenant.get(key)
            # Create the bucket on first use.
            if b is None:
                b = {"pushed": 0, "errors": 0, "skipped": 0,
                     "deleted": 0, "vms_total": 0}
                per_tenant[key] = b
            return b

        # slug -> tenant object cache (None for unassigned). '' → None.
        tenant_cache: Dict[str, Any] = {}
        # perf FIX C: cluster name -> id cache (mirrors the tenant cache) —
        # _ensure_vm_cluster was a clusters.get() per VM for the same few names.
        # Failed resolutions (None) are cached too, matching _resolve_tenant.
        cluster_cache: Dict[str, Optional[int]] = {}

        # Case-insensitive tenant lookup table: map lower(slug) AND lower(name)
        # → the tenant's canonical NetBox slug, built once per batch. NetBox
        # slugs are conventionally lowercase, but a VM's Proxmox label (or a
        # configured tenant_slug) can arrive in mixed case, and the label may
        # be the tenant's display NAME rather than its slug. We resolve the
        # incoming value to the canonical slug first (slug match wins over a
        # name match on collision), then do the exact pynetbox fetch — so a VM
        # labeled "LRB" / "Lrb" / "LRB Labs" still attributes to tenant slug
        # "lrb". Falls back to the raw value if the index build failed.
        tenant_lut: Dict[str, str] = {}
        # Best-effort: a failure leaves the table empty.
        try:
            # Map each tenant's slug and name (lowercased) to its slug.
            for t in self._api_get_all("/api/tenancy/tenants/"):
                slug = str((t or {}).get("slug") or "").strip()
                # Skip rows without a slug.
                if not slug:
                    continue
                tenant_lut.setdefault(slug.lower(), slug)
                nm = str((t or {}).get("name") or "").strip()
                if nm:
                    tenant_lut.setdefault(nm.lower(), slug)
        except Exception as e:
            # Index build failed: fall back to raw values.
            logger.debug("sync_vms: build tenant lookup failed: %s", e)

        def _resolve_tenant(slug: Optional[str]):
            """Tenant object for a slug or name (cached), or None."""
            # Empty slug: unassigned.
            s = str(slug or "").strip()
            if not s:
                return None
            # Cached answer.
            if s in tenant_cache:
                return tenant_cache[s]
            # Map to the canonical slug.
            canon = tenant_lut.get(s.lower()) or s
            try:
                # Fetch the tenant by canonical slug.
                t = self.nb.tenancy.tenants.get(slug=canon)
            except Exception as e:
                # Transient API error — do NOT cache. Caching this None would
                # mislabel every subsequent VM with the same slug as unassigned
                # for the rest of the run. Return None (unassigned for THIS VM)
                # and retry the lookup on the next VM.
                # Lookup failed: log and return None without caching.
                logger.debug("sync_vms: resolve tenant %s failed: %s", s, e)
                return None
            # Confirmed result (found, or a genuine "no such tenant" None) — safe
            # to memoize for the run.
            # Cache the result.
            tenant_cache[s] = t
            # Move the tenant's IPs into its VRF.
            if t:
                self._move_tenant_ips_to_vrf(t)
            # Tenant (or None).
            return t

        # Prefetch caches are always cleared in the finally block.
        try:
            # Self-heal proxmox_* custom fields on virtualization.virtualmachine
            # so the linkage PATCHes below land. Cached per-process.
            self._ensure_custom_fields()
            # perf FIX C: prefetch per-run reference data ONCE — every prefix
            # (mask resolution goes local in _mask_for_ip; was a GET per IP per
            # VM) and every vminterface (per-VM bucket lookup in
            # _assign_vm_primary_ip4; was a filter GET per VM). Cleared in the
            # ``finally`` below.
            self._begin_prefix_prefetch()
            self._vmiface_prefetch = self._prefetch_vm_interfaces()
            # Index incoming VMs by unique id.
            incoming: Dict[str, Dict[str, Any]] = {}
            for vm in (vms or []):
                # VMs without a unique id are skipped.
                uid = str((vm or {}).get("unique_id") or "").strip()
                if not uid:
                    skipped += 1
                    _bucket((vm or {}).get("tenant_slug") or tenant_slug)["skipped"] += 1
                    continue
                # Backfill a missing per-VM slug from the legacy batch slug.
                if not str((vm or {}).get("tenant_slug") or "").strip() and tenant_slug:
                    vm = dict(vm or {})
                    vm["tenant_slug"] = tenant_slug
                # Keep the VM.
                incoming[uid] = vm or {}

            # Index ALL existing NetBox VMs that carry a proxmox_unique_id
            # (proxmox-sourced) — cluster-wide, so replace-delete can remove
            # VMs destroyed in Proxmox regardless of which tenant owns them.
            existing: Dict[str, dict] = {}  # uid -> raw row dict (carries "id")
            existing_by_nc: Dict[tuple, dict] = {}  # (name, cluster_id) -> row
            # List every NetBox VM.
            try:
                rows = self._api_get_all("/api/virtualization/virtual-machines/",
                                         {"limit": 500})
            except Exception as e:
                # Listing failed: return an ERROR result.
                return {"status": "ERROR",
                        "message": f"failed to list NetBox VMs: {e}",
                        "pushed": 0, "errors": 0, "skipped": skipped,
                        "deleted": 0, "vms_total": len(incoming),
                        "per_tenant": per_tenant}
            # Index existing VMs by uid and by (name, cluster).
            for row in rows:
                # By proxmox_unique_id.
                cf = row.get("custom_fields") or {}
                uid = str((cf.get("proxmox_unique_id") or "").strip())
                if uid:
                    existing[uid] = row
                # Secondary index by (name, cluster_id) — NetBox enforces name
                # uniqueness per cluster, so at most one row per key. Used as a
                # fallback when a NetBox VM occupies the name+cluster but has no
                # /mismatched proxmox_unique_id (a stale/manual record, or a VM
                # whose uid changed after a node rename): without this the uid
                # lookup misses and the create branch 400s with "Virtual machine
                # name must be unique per cluster." Adopting by name self-heals
                # — the update stamps the incoming proxmox_unique_id so future
                # syncs match by uid.
                # By (name, cluster id).
                rname = str(row.get("name") or "").strip()
                rcl = row.get("cluster")
                rcid = (rcl.get("id") if isinstance(rcl, dict) else rcl)
                if rname and rcid:
                    existing_by_nc[(rname, int(rcid))] = row

            # Replace-delete (cluster-wide): drop proxmox-sourced NetBox VMs
            # whose uid is no longer in the incoming full set. Attribute each
            # delete to the row's tenant for per-tenant reporting.
            # Replace mode: delete Proxmox-sourced VMs that are gone.
            if replace:
                for uid, row in list(existing.items()):
                    # Still present: keep.
                    if uid in incoming:
                        continue
                    # Owning tenant slug, for the per-tenant breakdown.
                    rslug = ""
                    rten = row.get("tenant")
                    if isinstance(rten, dict):
                        rslug = str(rten.get("slug") or "")
                    try:
                        # perf FIX C: hydrate from the listed row — no per-VM
                        # re-GET before the delete.
                        # Delete it and count.
                        obj = self._hydrate_record(
                            row, self.nb.virtualization.virtual_machines)
                        if obj:
                            obj.delete()
                            deleted += 1
                            _bucket(rslug)["deleted"] += 1
                    except Exception as e:
                        # Delete failed: count and keep the first error.
                        errors += 1
                        _bucket(rslug)["errors"] += 1
                        if first_err is None:
                            first_err = f"delete {uid}: {e}"
                        logger.debug("sync_vms: delete stale %s failed: %s", uid, e)

            # Create or update each incoming VM.
            for uid, vm in incoming.items():
                # Resolve the VM's tenant and its counters.
                vslug = vm.get("tenant_slug")
                tenant = _resolve_tenant(vslug)
                b = _bucket(vslug)
                b["vms_total"] += 1
                # Per-VM failures are counted, not raised.
                try:
                    # Resolve the cluster id (cached).
                    cname = str(vm.get("cluster") or "").strip()
                    if cname in cluster_cache:
                        cluster_id = cluster_cache[cname]
                    else:
                        cluster_id = self._ensure_vm_cluster(cname, tenant)
                        # _ensure_vm_cluster auto-creates on a genuine miss, so a
                        # None here means an API error (or empty name), NOT a
                        # confirmed "no cluster". Don't cache it — caching would
                        # strand every later VM in this cluster as cluster-less
                        # for the rest of the run; leave it uncached to retry.
                        if cluster_id is not None:
                            cluster_cache[cname] = cluster_id
                    # Basic fields from the VM record.
                    name = str(vm.get("name") or "").strip() or f"vm-{vm.get('vmid') or uid}"
                    status = self._vm_status_map(vm.get("status"))
                    vcpus = int(vm.get("vcpus") or 0)
                    disk_gb = round(float(vm.get("disk_gb") or 0), 1)
                    mem_mb = int(vm.get("mem_mb") or 0)
                    # Proxmox linkage custom fields plus last_seen.
                    cf = {
                        "proxmox_unique_id": uid,
                        "proxmox_vmid": str(vm.get("vmid") or ""),
                        "proxmox_node": str(vm.get("node") or ""),
                        "proxmox_type": str(vm.get("type") or ""),
                        # Proxmox tags/labels (semicolon-joined) — the pxmx agent
                        # emits tags as a list; join back to Proxmox's native
                        # ';' separation so the field round-trips with the GUI.
                        "proxmox_labels": ";".join(
                            str(t).strip() for t in (vm.get("tags") or [])
                            if str(t).strip()),
                        # last_seen clocks the staleness sweep from this detection
                        # (folded into the cf PATCH so it rides the same save — no
                        # extra write per VM). Best-effort: a missing field is
                        # swallowed by the cf-PATCH try/except below.
                        "last_seen": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"),
                    }
                    # Resolve the existing NetBox VM: by proxmox_unique_id first
                    # (the steady-state match), then by (name, cluster) as a
                    # fallback that adopts a stale/manual record occupying the
                    # name — see the existing_by_nc comment above.
                    # Find the existing NetBox VM: by uid, then by name and cluster.
                    match_row = existing.get(uid)
                    if match_row is None and cluster_id:
                        match_row = existing_by_nc.get((name, cluster_id))
                    # Existing VM: update it.
                    if match_row is not None:
                        # perf FIX C: hydrate the pynetbox Record from the
                        # listed row (the listing already returned the full
                        # serialized record) — no per-VM re-GET.
                        # Build the record from the listed row.
                        obj = self._hydrate_record(
                            match_row, self.nb.virtualization.virtual_machines)
                        # Hydration failed: count an error.
                        if not obj:
                            errors += 1
                            b["errors"] += 1
                            continue
                        # source_of_truth=="netbox" → NetBox is the source of truth
                        # for VMs: only-add-missing. The VM already exists, so do
                        # NOT overwrite any field Proxmox would otherwise clobber
                        # (name/cluster/status/vcpus/disk/memory/tenant/proxmox_*).
                        # We still refresh last_seen (a staleness signal, not a
                        # truth field) so a seen VM isn't swept. The per-interface
                        # vminterfaces/IPs are GATHERED data, not a truth field, so
                        # only-add-missing still builds them — a pre-existing VM
                        # that predates the IP-gathering feature must still get its
                        # IPs added. (Was: ``continue``d here, so a netbox-SoT VM
                        # never got ``_assign_vm_primary_ip4`` → IP-less with 0
                        # reported errors.) "external" (Proxmox is the source of
                        # truth) overwrites the truth fields as before.
                        # NetBox is the source of truth: only refresh last_seen.
                        if source_of_truth == "netbox":
                            self._stamp_last_seen(obj)
                        else:
                            # Proxmox is the source of truth: overwrite the fields.
                            obj.name = name
                            if cluster_id:
                                obj.cluster = cluster_id
                            obj.status = status
                            if vcpus:
                                obj.vcpus = vcpus
                            # Only overwrite sizing that Proxmox actually reported.
                            if disk_gb:
                                obj.disk = int(disk_gb)
                            if mem_mb:
                                obj.memory = mem_mb
                            # Set/clear so a VM that changed tags moves tenant
                            # (or drops to unassigned) without a delete+recreate.
                            obj.tenant = tenant.id if tenant else None
                            # perf FIX C: ONE save for core fields + proxmox_*
                            # custom fields (was two PATCHes per VM). perf FIX A:
                            # last_seen is quantized — keep the stored stamp
                            # while it's fresh (<1h) so an otherwise-unchanged
                            # VM yields a no-diff save (zero PATCHes).
                            prev_cf = dict(obj.custom_fields or {})
                            cf_patch = dict(cf)
                            # Drop last_seen from the patch while the stored value is still fresh.
                            if not self._last_seen_stale(prev_cf.get("last_seen")):
                                cf_patch.pop("last_seen", None)
                            obj.custom_fields = {**prev_cf, **cf_patch}
                            # One save for core fields and custom fields.
                            try:
                                obj.save()
                            except Exception as e:
                                # The deployed NetBox may not have the proxmox_*/
                                # last_seen custom fields attached to
                                # virtualization.virtualmachine — a cf 400 must
                                # NOT lose the core-field update. Retry WITHOUT
                                # the cf patch (best-effort linkage, same
                                # contract as the old two-save split).
                                logger.warning("sync_vms: combined save %s failed "
                                               "(custom fields unprovisioned?): %s "
                                               "— retrying core fields only", uid, e)
                                # Retry without the custom-field changes.
                                obj.custom_fields = prev_cf
                                obj.save()
                    else:
                        # Create WITHOUT inline custom_fields: a create carrying
                        # custom_fields 400s ("Custom field 'proxmox_node' does
                        # not exist for this object type") when the field isn't
                        # attached to virtualization.virtualmachine on the
                        # deployed NetBox — which blocked ALL VM syncs (0/N).
                        # Sync the VM first, then PATCH the proxmox_* linkage
                        # best-effort so a provisioning gap never blocks the
                        # sync. The update path sets them once fields exist.
                        # Core fields for the new VM.
                        create_kwargs: Dict[str, Any] = {
                            "name": name, "status": status}
                        if cluster_id:
                            create_kwargs["cluster"] = cluster_id
                        if vcpus:
                            create_kwargs["vcpus"] = vcpus
                        if disk_gb:
                            create_kwargs["disk"] = int(disk_gb)
                        if mem_mb:
                            create_kwargs["memory"] = mem_mb
                        if tenant:
                            create_kwargs["tenant"] = tenant.id
                        # Create the VM.
                        obj = self.nb.virtualization.virtual_machines.create(**create_kwargs)
                        # Set the linkage custom fields; failure is only logged.
                        try:
                            obj.custom_fields = cf
                            obj.save()
                        except Exception as e:
                            # Custom fields not provisioned yet.
                            logger.warning("sync_vms: custom_fields set on new VM %s "
                                           "skipped (field unprovisioned?): %s", uid, e)
                        # Journal the new VM.
                        self._journal("virtualization.virtualmachine", obj.id,
                                      "hypervisor-vm-sync",
                                      note=f"VM {name} ({uid})")
                        # last_seen folded into ``cf`` above → rides the cf save.
                    # vminterfaces + all IPs + primary_ip4 (best-effort) — built
                    # from the per-interface records the pxmx agent gathers.
                    # Returns (failures, first_err): a real-world pynetbox failure
                    # (e.g. the IP already exists in IPAM on a discovered device)
                    # is now counted + surfaced instead of silently DEBUG-logged.
                    ip_fail, ip_err = self._assign_vm_primary_ip4(obj, vm, tenant)
                    # Fold interface/IP failures into the totals.
                    if ip_fail:
                        errors += ip_fail
                        b["errors"] += ip_fail
                        if first_err is None and ip_err:
                            first_err = ip_err
                    # Count the VM as pushed.
                    pushed += 1
                    b["pushed"] += 1
                except Exception as e:
                    # Upsert failed: count and keep the first error.
                    errors += 1
                    b["errors"] += 1
                    if first_err is None:
                        first_err = f"upsert {uid}: {e}"
                    logger.debug("sync_vms: upsert %s failed: %s", uid, e)

            # Summary line, with the first error when there were any.
            msg = (f"{pushed} VM(s) upserted, {deleted} deleted, "
                   f"{skipped} skipped, {errors} errors")
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("sync_vms: %s", msg)
            else:
                logger.info("sync_vms: %s", msg)
            # Totals and per-tenant breakdown.
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "vms_total": len(incoming), "message": msg,
                    "per_tenant": per_tenant}
        except Exception as e:
            # Unexpected failure: ERROR with the counts so far.
            logger.error("sync_vms failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "vms_total": len(vms or []), "per_tenant": per_tenant}
        finally:
            # perf FIX C: the prefetched prefix/vminterface sets are valid for
            # THIS run only — drop them so later single-record calls (claim,
            # nw poll) don't resolve against stale data.
            self._end_prefix_prefetch()
            self._vmiface_prefetch = None

    def get_tenant_vmid_range(self, tenant_slug: str = "") -> Dict[str, Any]:
        """Read a NetBox tenant's Proxmox VMID allocation range + in-use VMIDs.

        Returns ``{status, vmid_start, vmid_end, used_vmids}`` where
        ``vmid_start``/``vmid_end`` come from the tenant's
        ``vmid_start``/``vmid_end`` custom fields (None when the tenant has no
        range set), and ``used_vmids`` is the sorted list of ``proxmox_vmid``
        custom-field values on that tenant's VMs (only those inside the range,
        when a range is set). Used by the LM hub's VMID auto-allocation knob
        to pick the next free VMID inside a tenant's range.

        ``status`` is ``SUCCESS`` with ``vmid_start``/``vmid_end`` = None when
        the tenant exists but has no range (→ caller falls back to Proxmox
        nextid), or ``ERROR`` when the tenant can't be resolved / the read
        fails (→ caller also falls back).
        """
        try:
            # A tenant slug is required.
            slug = str(tenant_slug or "").strip()
            if not slug:
                return {"status": "ERROR", "message": "no tenant_slug",
                        "vmid_start": None, "vmid_end": None, "used_vmids": []}
            # Look up the tenant.
            tenant = self.nb.tenancy.tenants.get(slug=slug)
            # Unknown tenant: error.
            if not tenant:
                return {"status": "ERROR",
                        "message": f"NetBox tenant '{slug}' not found",
                        "vmid_start": None, "vmid_end": None, "used_vmids": []}
            # Range from the tenant's custom fields.
            cf = tenant.custom_fields or {}
            start = cf.get("vmid_start")
            end = cf.get("vmid_end")

            def _as_int(v):
                """Integer value of ``v``, or None if it is not a number."""
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None

            # Range bounds as integers (None when unset).
            start_i, end_i = _as_int(start), _as_int(end)

            # VMIDs already used by this tenant's VMs.
            used: List[int] = []
            # Listing failure is treated as no VMs.
            try:
                rows = self._api_get_all("/api/virtualization/virtual-machines/",
                                         {"limit": 500, "tenant": slug})
            except Exception as e:
                # Listing failed.
                logger.debug("get_tenant_vmid_range: list VMs for %s failed: %s",
                             slug, e)
                rows = []
            # Collect each VM's VMID, limited to the range when one is set.
            for row in rows:
                # VMID from the custom field.
                rc = (row.get("custom_fields") or {})
                # Skip VMs without one.
                vid = _as_int(rc.get("proxmox_vmid"))
                if vid is None:
                    continue
                # Skip VMIDs outside the range.
                if start_i is not None and end_i is not None:
                    if not (start_i <= vid <= end_i):
                        continue
                # Keep it.
                used.append(vid)
            # Sorted, de-duplicated list.
            used = sorted(set(used))
            return {"status": "SUCCESS",
                    "vmid_start": start_i, "vmid_end": end_i,
                    "used_vmids": used}
        except Exception as e:
            # Unexpected failure.
            logger.error("get_tenant_vmid_range failed: %s", e)
            return {"status": "ERROR", "message": str(e),
                    "vmid_start": None, "vmid_end": None, "used_vmids": []}

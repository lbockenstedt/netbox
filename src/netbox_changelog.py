"""Change-log journaling + IP-reuse/reassign helpers for NetboxEngine."""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NetboxEngine")


def _ref_id(ref: Any) -> Any:
    """Id of a pynetbox reference: a Record, a nested dict, or a bare id.
    (A Record never equals an int, so compare ids, not objects.)"""
    if isinstance(ref, dict):
        return ref.get("id")
    return getattr(ref, "id", ref)


class ChangelogMixin:
    """Change-log journaling + IP-reuse/reassign helpers for NetboxEngine."""

    # ── change-log + IP-reuse helpers (shared by the external-source syncs) ────

    def _journal(self, content_type: str, object_id: Any, module: str,
                 note: str = "") -> None:
        """Write a NetBox **journal entry** on ``object_id`` (of NetBox content
        type ``dcim.device`` / ``ipam.ipaddress`` / ``dcim.interface`` /
        ``virtualization.virtualmachine`` / ``dcim.cable``) recording which LM
        sync module created it and when. The Journal tab is NetBox's native
        per-object change log, so this is the audit trail the user asked for
        ("comments to the change log for what module added the entry and when").

        Best-effort by design: a journal failure (older NetBox without the
        journal endpoint, a content-type mismatch, a transient 4xx) must NEVER
        break a sync — it's logged at DEBUG and swallowed.
        """
        if not object_id:
            return
        try:
            when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            comment = f"Created by LM {module} sync at {when}"
            if note:
                comment += f" — {note}"
            self.nb.extras.journal_entries.create(
                assigned_object_type=content_type,
                assigned_object_id=int(object_id),
                kind="info",
                comment=comment,
            )
        except Exception as e:
            logger.debug("journal %s/%s (%s) failed: %s",
                         content_type, object_id, module, e)

    def _reuse_or_create_ip(self, addr: str, create_kwargs: Dict[str, Any],
                            bare_ip: str, iface_id: int, tenant: Any = None,
                            hostname: str = "", mac: str = "",
                            source: str = "sync",
                            iface_type: str = "dcim.interface") -> Any:
        """Return an ``ipam.ip_address`` for ``addr`` (``host/prefix``), reusing
        an existing **global** record when one already exists and reassigning it
        to ``iface_id``, else creating a new one.

        NetBox enforces global IP uniqueness, so a discovery source that tries
        to create an IP the IPAM already provisioned 400s with ``Duplicate IP
        address found in global table`` — that was failing ~every record in
        ``sync_devices`` because NetBox (the IPAM source of truth) already held
        most of the discovered addresses. Reusing the existing record and
        pointing it at the discovered device's NIC fixes that without losing the
        address, and tags MAC/tenant/dns_name best-effort.

        The create path propagates a real failure (so the caller records it);
        the reuse path never raises. A mask-mismatch duplicate on create falls
        back to a bare-IP lookup + reassign so the record isn't lost.
        """
        # Tenants have overlapping address space, each in its own VRF: create
        # in the tenant's VRF and only reuse a record from that same VRF.
        vrf_id = self._vrf_id_for_tenant(tenant) if tenant else None
        if vrf_id is not None:
            create_kwargs = {**create_kwargs, "vrf": vrf_id}
            lookup: Dict[str, Any] = {"vrf_id": vrf_id}
        else:
            lookup = {}

        # 1) Proactive reuse: an exact host/prefix match already exists.
        ipobj = None
        try:
            ipobj = self.nb.ipam.ip_addresses.get(address=addr, **lookup)
        except Exception as e:
            logger.debug("%s: existing-IP lookup %s failed: %s", source, addr, e)
        if ipobj:
            ok = self._reassign_ip(ipobj, iface_id, tenant, hostname, mac, source, addr,
                                   iface_type)
            if not ok:
                raise RuntimeError(
                    f"reuse-IP {addr} reassign to {iface_type}:{iface_id} failed")
            return ipobj

        # 2) No existing record — create one.
        try:
            ipobj = self.nb.ipam.ip_addresses.create(**create_kwargs)
            self._tag_ip_mac(ipobj, mac, source, addr)
            return ipobj
        except Exception as create_err:
            # 3) Mask-mismatch duplicate: the existing record has a different
            # prefix length than we computed, so the exact lookup missed it but
            # the create still 400s. Fall back to a bare-IP lookup + reassign.
            matches: List[Any] = []
            try:
                matches = list(self.nb.ipam.ip_addresses.filter(address=bare_ip, **lookup))
            except Exception:
                matches = []
            if not matches:
                raise create_err
            ipobj = matches[0]
            ok = self._reassign_ip(ipobj, iface_id, tenant, hostname, mac, source, bare_ip,
                                   iface_type)
            if not ok:
                raise RuntimeError(
                    f"reuse-IP {bare_ip} reassign to {iface_type}:{iface_id} failed")
            return ipobj

    def _reassign_ip(self, ipobj: Any, iface_id: int, tenant: Any,
                     hostname: str, mac: str, source: str, addr: str,
                     iface_type: str = "dcim.interface") -> bool:
        """Reassign an existing ipam.ip_address to ``iface_id`` and best-effort
        tag tenant/dns_name/MAC. Returns True on success (reassigned or already
        correct), False if the write failed — the caller then raises so the
        sync result surfaces it instead of going IP-less silent.

        Uses an explicit ``ipam.ip_addresses.update(id, {...})`` PATCH rather
        than ``ipobj.save()`` so the ``assigned_object_type`` change is sent
        reliably: ``assigned_object_type`` is a ContentType that pynetbox
        fetches as a nested dict, and ``.save()``'s diff-detection can omit it
        from the PATCH (sending only ``assigned_object_id`` → NetBox 400s
        "assigned_object_type is required") or no-op, leaving the IP on its old
        interface. That was the root cause of "the IP record exists but isn't
        connected to the VM": the IP was created first by ``sync_devices`` on a
        ``dcim.interface`` (and set as that device's ``primary_ip4``);
        ``sync_vms`` tried to move it to the vminterface; the ``.save()``
        failed; and the failure was swallowed at DEBUG — so the VM's
        ``primary_ip4`` ended up pointing at an IP still assigned to the
        device. The local ``ipobj`` is mutated to match so any later
        ``.save()`` (MAC tag / last_seen stamp) sends the new assignment
        instead of reverting it. A failure is now logged WARNING
        ``[sync-error]`` (reaches the spoke log + GET_ERROR_LOGS) and returned
        False; the old silent DEBUG swallow is gone.

        When the IP is moved onto a VM interface, the old parent (a device or
        a stale duplicate VM) loses it as its primary IP FIRST — NetBox refuses
        to move an IP that is still its parent's primary ("Cannot reassign IP
        address while it is designated as the primary IP for the parent
        object"). If the move then fails, that primary IP is put back.
        Never raises.
        """
        updates: Dict[str, Any] = {}
        if getattr(ipobj, "assigned_object_id", None) != iface_id:
            updates["assigned_object_type"] = iface_type
            updates["assigned_object_id"] = iface_id
        if tenant and getattr(ipobj, "tenant", None) != tenant.id:
            updates["tenant"] = tenant.id
        if hostname and hostname.lower() != "unknown" and \
                (getattr(ipobj, "dns_name", "") or "") != hostname:
            updates["dns_name"] = hostname
        released = []
        if "assigned_object_id" in updates and iface_type != "dcim.interface":
            released = self._release_old_parent_primary(ipobj, source, addr)
        if updates:
            try:
                # pynetbox >=7's Endpoint.update() is a BULK op: it takes a LIST
                # of dicts/Records, each carrying its ``id``. A single dict raises
                # "Objects passed must be list[dict|Record] - was <dict>" (the
                # earlier 2-arg ``update(id, body)`` form was also wrong — "takes 2
                # positional arguments but 3 were given"). Wrap the one dict in a list.
                self.nb.ipam.ip_addresses.update([{"id": ipobj.id, **updates}])
            except Exception as e:
                logger.warning("[sync-error] %s: reuse-IP %s reassign failed: %s",
                               source, addr, e)
                self._restore_parent_primary(released, ipobj.id, source, addr)
                return False
            # Reflect the change on the local object so a later .save() (MAC
            # tag / last_seen stamp) sends the new assignment, not the old one.
            if "assigned_object_type" in updates:
                ipobj.assigned_object_type = updates["assigned_object_type"]
                ipobj.assigned_object_id = updates["assigned_object_id"]
            if "tenant" in updates:
                ipobj.tenant = updates["tenant"]
            if "dns_name" in updates:
                ipobj.dns_name = updates["dns_name"]
        self._tag_ip_mac(ipobj, mac, source, addr)
        return True

    def _release_old_parent_primary(self, ipobj: Any, source: str,
                                    addr: str) -> List[tuple]:
        """Clear the IP as primary_ip4/primary_ip6 of the object it is assigned
        to now (a device or a VM), so NetBox lets it move. Returns the
        ``(record, field)`` pairs cleared, for :meth:`_restore_parent_primary`.
        Never raises — on any error the move is simply attempted as-is."""
        released: List[tuple] = []
        try:
            aot = getattr(ipobj, "assigned_object_type", None)
            if isinstance(aot, dict):
                aot = f"{aot.get('app_label', '')}.{aot.get('model', '')}"
            iface_id = _ref_id(getattr(ipobj, "assigned_object_id", None))
            if aot == "dcim.interface":
                ifaces, parent_key, parents = (self.nb.dcim.interfaces, "device",
                                               self.nb.dcim.devices)
            elif aot == "virtualization.vminterface":
                ifaces, parent_key, parents = (self.nb.virtualization.interfaces,
                                               "virtual_machine",
                                               self.nb.virtualization.virtual_machines)
            else:
                return released
            iface = ifaces.get(iface_id) if iface_id else None
            parent_id = _ref_id(getattr(iface, parent_key, None)) if iface else None
            parent = parents.get(parent_id) if parent_id else None
            if not parent:
                return released
            for field in ("primary_ip4", "primary_ip6"):
                if _ref_id(getattr(parent, field, None)) == ipobj.id:
                    setattr(parent, field, None)
                    released.append((parent, field))
            if released:
                parent.save()
                logger.info("%s: released IP %s as primary of %s %s so it can "
                            "move to the VM", source, addr, parent_key,
                            getattr(parent, "name", parent_id))
        except Exception as e:
            logger.debug("%s: releasing old primary for IP %s skipped: %s",
                         source, addr, e)
            return []
        return released

    def _restore_parent_primary(self, released: List[tuple], ip_id: Any,
                                source: str, addr: str) -> None:
        """Undo :meth:`_release_old_parent_primary` after a failed move."""
        for parent, field in released:
            try:
                setattr(parent, field, ip_id)
                parent.save()
            except Exception as e:
                logger.warning("[sync-error] %s: could not restore %s %s on %s: %s",
                               source, field, addr, getattr(parent, "name", "?"), e)

    def _tag_ip_mac(self, ipobj: Any, mac: str, source: str, addr: str) -> None:
        """Best-effort write ``mac_address`` onto an ipam.ip_address custom
        field. Never raises (the IP is still synced without the MAC tag)."""
        if not mac:
            return
        try:
            m = dict(ipobj.custom_fields or {})
            if m.get("mac_address") != mac:
                m["mac_address"] = mac
                ipobj.custom_fields = m
                ipobj.save()
        except Exception as e:
            logger.debug("%s: mac_address on IP %s skipped: %s", source, addr, e)

    @staticmethod
    def _save_cf_delta(obj: Any, delta: Dict[str, Any]) -> None:
        """Save ``obj`` sending ONLY the custom-field keys in ``delta`` (plus
        any other attribute the caller already changed on ``obj``).

        Why: pynetbox's ``save()`` PATCHes the WHOLE ``custom_fields`` dict as
        soon as one key differs. If ``obj`` was read a while ago (sync_devices
        now reuses one Record per device for the whole batch), the whole-dict
        PATCH would write back the old value of every OTHER key, silently
        reverting a concurrent sync's change (e.g. a cppm sync that set
        ``switch_port`` meanwhile). NetBox MERGES the ``custom_fields`` of a
        PATCH, so sending just the changed keys is exact. pynetbox >= 7.5
        compares only the assigned keys (issue #748); older versions send the
        assigned subset as-is — both send exactly ``delta``.

        Afterwards ``obj.custom_fields`` is the full merged view again (the
        PATCH response normally provides it; restored by hand when the save
        raised or the pynetbox version doesn't re-parse the response), so code
        reading ``obj.custom_fields`` later never sees a partial dict.
        Raises whatever ``save()`` raises."""
        before = dict(obj.custom_fields or {})
        obj.custom_fields = dict(delta)
        try:
            obj.save()
        except Exception:
            obj.custom_fields = before  # nothing landed
            raise
        after = obj.custom_fields
        if not isinstance(after, dict) or set(before) - set(after):
            obj.custom_fields = {**before, **delta}

    # Quantization window for last_seen rewrites. The staleness sweep's
    # thresholds are DAYS (7d offline / 30d delete — netbox_staleness.py), so a
    # second-resolution ``now()`` stamp was pure churn: every sync tick produced
    # a guaranteed-diff PATCH per object even when nothing else changed. Only
    # rewrite when the stored stamp is missing/unparsable or ≥1h off.
    _LAST_SEEN_MAX_AGE_S = 3600

    @staticmethod
    def _last_seen_stale(stored: Any, now: Optional[datetime] = None,
                         max_age_s: int = 3600) -> bool:
        """Whether the stored ``last_seen`` CF value needs a rewrite.

        True when ``stored`` is missing/empty, unparsable (treat as stale), or
        more than ``max_age_s`` away from ``now`` — in EITHER direction, so a
        clock-skewed future stamp can't freeze the signal forever. False when
        the stamp is fresh (within the window), letting the caller skip the
        save entirely — a no-op sync then produces zero PATCHes."""
        s = str(stored or "").strip()
        if not s:
            return True
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True
        now = now or datetime.now(timezone.utc)
        return abs((now - dt.astimezone(timezone.utc)).total_seconds()) >= max_age_s

    def _stamp_last_seen(self, obj: Any, when: str = "") -> None:
        """Write the ``last_seen`` custom field (ISO UTC) on ``obj`` so the
        staleness sweep can age it. Quantized: when the stored stamp is already
        fresh (parsable and <1h old — see ``_last_seen_stale``) the write is
        skipped, so a steady-state sync no longer PATCHes every object every
        tick. Best-effort: a missing field / save failure is logged at DEBUG
        and swallowed — a staleness signal must never break the sync that
        produced it. ``when`` defaults to now (UTC)."""
        try:
            m = dict(obj.custom_fields or {})
            if not self._last_seen_stale(m.get("last_seen")):
                return  # fresh within the hour — skip the no-op-diff PATCH
            ts = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if m.get("last_seen") != ts:
                self._save_cf_delta(obj, {"last_seen": ts})
        except Exception as e:
            logger.debug("stamp_last_seen on %r failed: %s", obj, e)

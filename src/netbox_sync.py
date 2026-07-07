"""Device / NW-device / access-tracker sync + discovery helpers for NetboxEngine."""
import ipaddress
import re
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NetboxEngine")


class SyncMixin:
    """Device / NW-device / access-tracker sync + discovery helpers for NetboxEngine."""

    # ---- firewall→NetBox device discovery sync helpers ---------------------

    @staticmethod
    def _norm_mac(mac: str) -> str:
        """Normalize a MAC to lowercase colon form (aa:bb:cc:dd:ee:ff).

        The OPNsense spoke returns the raw MAC; normalize here so the value
        written to the IP's ``mac_address`` custom field matches what the
        NetBox→CPPM endpoint sync reads (it keys on the colon form).
        """
        m = (mac or "").strip().lower()
        hexonly = re.sub(r"[^0-9a-f]", "", m)
        if len(hexonly) == 12:
            return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))
        return m

    @staticmethod
    def _row_has_ip_or_mac(row: dict) -> bool:
        """Whether a raw NetBox device row carries a primary IP OR a mac_address
        custom field — i.e. it is identifiable by something other than its name.

        Used by ``sync_devices`` to decide a hostname match: a device that is
        "bare" (neither IP nor MAC) sharing the incoming hostname is the same
        machine → adopt/update it in place; a device that already has an IP or
        MAC sharing a hostname is a DIFFERENT machine (the feed regularly sends
        one hostname across distinct MACs — ks205, sonoszp, iphone) → leave it
        and uniquify the new record instead of merging distinct devices.
        """
        pip = row.get("primary_ip4") or {}
        has_ip = isinstance(pip, dict) and bool(
            (pip.get("address") or "").split("/")[0].strip())
        cf = row.get("custom_fields") or {}
        has_mac = bool(NetboxEngine._norm_mac(cf.get("mac_address", "")))
        return has_ip or has_mac

    def _mask_for_ip(self, ip_str: str) -> str:
        """Derive the mask from the most specific containing prefix; /32 if none.

        Mirrors the inline lookup in ``claim_device`` (engine.py ~296-304),
        extracted so the device sync reuses it.
        """
        try:
            pdata = self._api_get("/api/ipam/prefixes/", {"contains": ip_str, "limit": 500})
            prefs = [ipaddress.ip_network(p["prefix"], strict=False)
                     for p in pdata.get("results", []) if p.get("prefix")]
            if prefs:
                prefs.sort(key=lambda n: n.prefixlen, reverse=True)  # longest first
                return str(prefs[0].prefixlen)
        except Exception as e:
            logger.debug("containing-prefix lookup for %s failed, using /32: %s", ip_str, e)
        return "32"

    def _ensure_device_role(self, slug: str = "discovered"):
        """Return the device role (auto-creating 'discovered' if missing). Best-effort."""
        slug = (slug or "discovered").strip().lower() or "discovered"
        try:
            r = self.nb.dcim.device_roles.get(slug=slug)
            if r:
                return r
            return self.nb.dcim.device_roles.create(
                name=slug.capitalize(), slug=slug, color="9e9e9e")
        except Exception as e:
            # WARNING (not debug): a None role cascades into per-device create
            # failures (device_type/role unresolved), so surface the reason.
            logger.warning("ensure_device_role '%s' failed (creates will error): %s", slug, e)
            return None

    def _ensure_device_type(self, slug: str = "discovered"):
        """Return the device type (auto-creating 'Discovered Device' under an
        'Unknown' manufacturer if missing). Best-effort."""
        slug = (slug or "discovered").strip().lower() or "discovered"
        try:
            dt = self.nb.dcim.device_types.get(slug=slug)
            if dt:
                return dt
            mfr = None
            try:
                mfr = self.nb.dcim.manufacturers.get(slug="unknown")
            except Exception:
                mfr = None
            if not mfr:
                mfr = self.nb.dcim.manufacturers.create(name="Unknown", slug="unknown")
            return self.nb.dcim.device_types.create(
                model="Discovered Device", slug=slug, manufacturer=mfr.id)
        except Exception as e:
            # WARNING (not debug): a None device_type cascades into per-device
            # create 400s (device_type is required), so surface the reason.
            logger.warning("ensure_device_type '%s' failed (creates will error): %s", slug, e)
            return None

    def _resolve_site(self, slug: str = "", tenant=None):
        """Resolve a site for device creation. Configured slug first; else the
        first site as a fallback. None if none resolve (site is optional for a
        NetBox device). Best-effort."""
        slug = (slug or "").strip().lower()
        if slug:
            try:
                s = self.nb.dcim.sites.get(slug=slug)
                if s:
                    return s
            except Exception as e:
                logger.warning("resolve_site '%s' failed: %s", slug, e)
        try:
            sites = list(self.nb.dcim.sites.filter(limit=1))
            if sites:
                return sites[0]
        except Exception as e:
            # site is optional for a device, so this stays best-effort — but
            # warn so a permissions issue is visible rather than silent.
            logger.warning("resolve_site first-site fallback failed: %s", e)
        return None

    def sync_devices(self, devices: list, tenant_slug: str = "",
                     replace: bool = False,
                     defaults: Optional[Dict[str, Any]] = None,
                     source: str = "opnsense",
                     source_of_truth: str = "external") -> Dict[str, Any]:
        """Push a tenant's discovery-source device set into NetBox DCIM.

        Source = a discovery feed relayed by the hub (OPNsense DHCP leases +
        ARP for the firewall sync; switch/gateway ARP tables for the nw sync).
        Each incoming record ``{ip, mac, hostname}`` is matched to an existing
        device by its primary IPv4; missing devices are created (mirroring
        ``claim_device``: tenant-owned device + ``mgmt`` interface + IP with
        ``custom_fields.mac_address`` + ``primary_ip4``). Writing the MAC onto
        the IP record feeds the NetBox→CPPM endpoint sync (which keys on
        ``mac_address``) — so static-IP devices the ARP table sees start flowing
        to ClearPass too.

        ``source`` is the ownership tag stamped onto created devices'
        ``custom_fields.discovered_from`` AND the scope key for replace-delete:
        ``"opnsense"`` / ``"fw"`` / ``"firewall"`` all normalize to the legacy
        ``"opnsense"`` tag (unchanged firewall behavior); any other value (e.g.
        the nw sync's ``"Network Devices"``) is used verbatim, so nw-created
        records are tagged ``Network Devices`` and replace-delete only ever
        touches nw-owned records — never the firewall's ``opnsense``-tagged
        ones, even within the same tenant.

        Ownership / replace-delete: devices this sync CREATES are tagged
        ``custom_fields.discovered_from = <source>`` (best-effort; mirrors
        ``proxmox_unique_id`` on VMs). When ``replace`` AND a tenant slug are
        provided, tagged devices of that tenant whose primary IP is absent from
        the incoming set are deleted. Pre-existing devices matched by IP are
        refreshed (MAC/dns_name on the IP) but NOT tagged and NOT deleted — we
        don't own them. Replace-delete is skipped when unscoped (global) so a
        global sync can't delete another tenant's records. If the
        ``discovered_from`` custom field isn't configured in NetBox the tag
        write is silently skipped and replace-delete becomes a safe no-op.

        Returns ``{status, pushed, errors, skipped, deleted, devices_total, message}``.
        """
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None   # first per-record failure text (diagnosability)
        defaults = defaults or {}
        # Normalize the ownership tag: firewall synonyms collapse to the legacy
        # "opnsense" tag (so existing firewall deployments are byte-identical);
        # anything else (nw's "Network Devices") is used verbatim. Comparison +
        # replace-delete scoping are case-insensitive against this tag.
        source_tag = str(source or "opnsense").strip()
        if source_tag.lower() in ("opnsense", "fw", "firewall"):
            source_tag = "opnsense"
        source_tag_l = source_tag.lower()
        def _owns(cf: dict) -> bool:
            return str(((cf or {}).get("discovered_from") or "")).lower() == source_tag_l
        try:
            # Self-heal custom fields (discovered_from on dcim.device,
            # mac_address on ipam.ipaddress) so the ownership tag + MAC writes
            # below land. Cached per-process; no-op once provisioned.
            self._ensure_custom_fields()
            tenant = None
            if tenant_slug:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if not tenant:
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — "
                                       f"firewall-discovered devices not attributed. "
                                       f"Check the tenant's NetBox slug mapping.",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "devices_total": len(devices or [])}

            # Normalize incoming: ip (mask stripped) -> {mac, hostname}.
            incoming: Dict[str, Dict[str, str]] = {}
            for dev in (devices or []):
                if not isinstance(dev, dict):
                    continue
                ip = str(dev.get("ip") or "").strip().split("/")[0].strip()
                mac = self._norm_mac(dev.get("mac", ""))
                hostname = str(dev.get("hostname") or "").strip()
                # MAC-sighting enrichment: a feed (nw ARP/MAC table, ClearPass)
                # may attach the source switch identity + port to a record so the
                # device in NetBox answers "where is this MAC?" — last seen on
                # switch X, port Y, mgmt IP Z.
                s_swname = str(dev.get("source_switch_name") or "").strip()
                s_swip = str(dev.get("source_switch_ip") or "").strip()
                s_swport = str(dev.get("source_switch_port") or "").strip()
                if not ip and not mac:
                    if not hostname or hostname.lower() == "unknown":
                        # Nothing identifiable at all — don't add a phantom row.
                        skipped += 1
                        continue
                    # Hostname-only record (no IP, no MAC): key by hostname so the
                    # hostname-match tier adopts any same-name device instead of
                    # duplicate-creating. The firewall path drops no-IP records
                    # upstream, so this is defensive; the active case is a
                    # no-MAC/no-IP discovery row that still carries a name.
                    ip = f"host:{hostname.lower()}"
                elif not ip:
                    # MAC-only: index by mac-key so it's at least created, but
                    # replace-delete (which keys on IP) won't track it.
                    ip = f"mac:{mac}"
                rec: Dict[str, str] = {"mac": mac, "hostname": hostname}
                if s_swname:
                    rec["switch_name"] = s_swname
                if s_swip:
                    rec["switch_ip"] = s_swip
                if s_swport:
                    rec["switch_port"] = s_swport
                incoming[ip] = rec

            # Index existing tenant devices by primary IPv4 (all) + track which
            # of those we own (discovered_from tag) for replace-delete. Also
            # index by name (lowercased) for ALL rows — including devices with
            # no primary_ip4, which the IP index skips below but whose name can
            # still collide on the (name, site, tenant) unique constraint when
            # the create branch re-uses device-<mac> after a DHCP IP move.
            existing_by_ip: Dict[str, dict] = {}   # ip_str -> raw device row
            existing_by_name: Dict[str, dict] = {}  # name.lower() -> raw device row
            existing_by_mac: Dict[str, dict] = {}   # norm_mac(cf.mac_address) -> row
            owned_ips: set = set()                   # primary IPs of tagged devices
            # Intra-batch dedup: many discovery records share a hostname across
            # distinct MACs (ks205, sonoszp, iphone…). existing_by_name is a
            # pre-batch snapshot and can't see names created earlier THIS batch,
            # so a 2nd create with the same name 400s on (name, site, tenant).
            # used_names tracks every name we create/refresh this batch.
            used_names: set = set()
            # device ids handled via the IP-match refresh path — the create
            # branch must never reclaim/clobber one of these by name (a
            # duplicate-hostname record would otherwise delete a device we just
            # refreshed for a different IP).
            refreshed_ids: set = set()
            list_params: Dict[str, Any] = {"limit": 500}
            if tenant_slug:
                list_params["tenant"] = tenant_slug
            try:
                rows = self._api_get_all("/api/dcim/devices/", list_params)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": skipped, "deleted": 0,
                        "devices_total": len(incoming)}
            for row in rows:
                rname = str(row.get("name") or "").strip().lower()
                if rname:
                    existing_by_name.setdefault(rname, row)  # first row wins
                cf = row.get("custom_fields") or {}
                # Index by the device's mac_address custom field (normalized) so
                # a record can match an existing device by MAC — catches DHCP
                # IP-moves + MAC-only records and (with the bare-hostname adopt
                # below) stops the duplicate a no-MAC/no-IP device spawned on
                # every sync. first row wins per MAC.
                rmac = self._norm_mac(cf.get("mac_address", ""))
                if rmac:
                    existing_by_mac.setdefault(rmac, row)
                pip = row.get("primary_ip4")
                addr = ""
                if isinstance(pip, dict):
                    addr = (pip.get("address") or "").split("/")[0].strip()
                if not addr:
                    continue
                existing_by_ip[addr] = row
                if _owns(cf):
                    owned_ips.add(addr)

            # Replace-with-delete — only when tenant-scoped, only owned devices.
            if replace and tenant_slug:
                for ip_str in list(owned_ips - set(incoming.keys())):
                    row = existing_by_ip.get(ip_str)
                    if not row:
                        continue
                    try:
                        obj = self.nb.dcim.devices.get(row["id"])
                        if obj:
                            obj.delete()
                            deleted += 1
                    except Exception as e:
                        errors += 1
                        if first_err is None:
                            first_err = f"delete {ip_str}: {e}"
                        logger.debug("sync_devices: delete stale %s failed: %s", ip_str, e)

            role = self._ensure_device_role(defaults.get("role") or "discovered")
            dtype = self._ensure_device_type(defaults.get("device_type") or "discovered")
            site = self._resolve_site(defaults.get("site") or "", tenant)
            # When a create fails because NetBox requires a site (no site
            # resolvable), the SAME 400 hits every record — previously ~one error
            # per device (185 in a batch). Detect that on the first create and
            # stop attempting further creates, aggregating the rest into one clear
            # message, instead of spamming an identical 400 for every record. Set
            # from the create handler below.
            _site_required_abort = False

            for ip_str, rec in incoming.items():
                mac = rec["mac"]
                hostname = rec["hostname"]
                is_mac_key = ip_str.startswith("mac:")
                is_host_key = ip_str.startswith("host:")
                real_ip = "" if (is_mac_key or is_host_key) else ip_str
                try:
                    # Resolve the existing NetBox device for this record. A
                    # record is the SAME machine if its IP, MAC, OR hostname
                    # matches an existing device — matching any one updates that
                    # device in place instead of creating a duplicate (the bug
                    # where a device added with no MAC/IP spawned a second copy
                    # on every sync because the sync could only compare one
                    # property). IP is the strongest key; MAC catches DHCP
                    # IP-moves + MAC-only records; hostname adopts a same-name
                    # device unless the record is PROVABLY a different machine
                    # (both MAC and IP present on both sides AND both differ) —
                    # the one case the registry allows a duplicate. A bare
                    # placeholder (no IP/MAC) is always adopted; an unowned
                    # non-bare (human) device is never adopted (don't clobber
                    # human data). A device refreshed this batch
                    # (refreshed_ids) is never adopted by a later record.
                    row = existing_by_ip.get(ip_str) if not (is_mac_key or is_host_key) else None
                    if row is None and mac:
                        row = existing_by_mac.get(mac)
                    if row is None and hostname and hostname.lower() != "unknown":
                        cand = existing_by_name.get(hostname.lower())
                        if cand is not None and cand["id"] not in refreshed_ids:
                            ecf = cand.get("custom_fields") or {}
                            emac = self._norm_mac(ecf.get("mac_address", ""))
                            epip = cand.get("primary_ip4") or {}
                            eaddr = ((epip.get("address") or "").split("/")[0].strip()
                                      if isinstance(epip, dict) else "")
                            cand_bare = not emac and not eaddr
                            cand_owned = _owns(ecf)
                            mac_differs = bool(mac) and bool(emac) and emac != mac
                            ip_differs = bool(real_ip) and bool(eaddr) and eaddr != real_ip
                            provably_different = mac_differs and ip_differs
                            if (cand_bare or cand_owned) and not provably_different:
                                row = cand
                    if row:
                        # Existing device matched (by IP / MAC / bare hostname) —
                        # update it in place. Remember its id so the create
                        # branch never reclaims/clobbers a device we just updated
                        # when a duplicate hostname shows up.
                        refreshed_ids.add(row["id"])
                        rname = str(row.get("name") or "").strip().lower()
                        if rname:
                            used_names.add(rname)
                        pip = row.get("primary_ip4") or {}
                        ip_id = pip.get("id") if isinstance(pip, dict) else None
                        match_addr = ((pip.get("address") or "").split("/")[0].strip()
                                      if isinstance(pip, dict) else "")
                        cf = row.get("custom_fields") or {}
                        we_own = _owns(cf)
                        # source_of_truth=="netbox" → NetBox is the source of truth
                        # for this device: only-add-missing. The device already
                        # exists, so do NOT overwrite its IP's mac_address/dns_name,
                        # assign it a primary IP, or rename it — only refresh
                        # last_seen (a staleness signal, not a truth field).
                        # "external" (the discovery feed is the source of truth)
                        # updates as below.
                        if source_of_truth == "netbox":
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    self._stamp_last_seen(devobj)
                            except Exception as e:
                                logger.debug("sync_devices: last_seen refresh %s: %s",
                                              ip_str, e)
                            pushed += 1
                            continue
                        # Primary IP: refresh the existing IP when it already
                        # equals the incoming IP (or the record is MAC-only with
                        # no IP to assign); otherwise ASSIGN the incoming IP as
                        # primary_ip4 (bare-device adoption, or a DHCP IP-move
                        # caught by MAC match) — creating the mgmt interface + IP
                        # the create branch uses, via _reuse_or_create_ip.
                        if ip_id and (not real_ip or match_addr == real_ip):
                            try:
                                ipobj = self.nb.ipam.ip_addresses.get(ip_id)
                                if ipobj:
                                    if mac:
                                        merged = dict(ipobj.custom_fields or {})
                                        merged["mac_address"] = mac
                                        ipobj.custom_fields = merged
                                    if hostname and hostname.lower() != "unknown":
                                        ipobj.dns_name = hostname
                                    ipobj.save()
                                    self._stamp_last_seen(ipobj)
                            except Exception as e:
                                logger.debug("sync_devices: refresh IP %s failed: %s", ip_str, e)
                        elif real_ip:
                            try:
                                iface = self.nb.dcim.interfaces.create(
                                    device=row["id"], name="mgmt", type="other")
                                mask = self._mask_for_ip(real_ip)
                                ip_kwargs: Dict[str, Any] = {
                                    "address": f"{real_ip}/{mask}",
                                    "assigned_object_type": "dcim.interface",
                                    "assigned_object_id": iface.id,
                                }
                                if tenant:
                                    ip_kwargs["tenant"] = tenant.id
                                if hostname and hostname.lower() != "unknown":
                                    ip_kwargs["dns_name"] = hostname
                                ipobj = self._reuse_or_create_ip(
                                    f"{real_ip}/{mask}", ip_kwargs, real_ip, iface.id,
                                    tenant=tenant, hostname=hostname, mac=mac,
                                    source=source_tag)
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    devobj.primary_ip4 = ipobj.id
                                    devobj.save()
                                self._journal("ipam.ipaddress", ipobj.id,
                                              source_tag,
                                              note=f"IP {real_ip}/{mask} → adopted")
                                self._stamp_last_seen(ipobj)
                                # We moved/assigned the device's primary IP away
                                # from match_addr — stop replace-delete from
                                # reaping this device because its OLD primary IP
                                # isn't in the incoming set (same device, now at a
                                # new address, not a stale one).
                                if match_addr and match_addr != real_ip and match_addr in owned_ips:
                                    owned_ips.discard(match_addr)
                            except Exception as e:
                                logger.debug("sync_devices: assign IP %s failed: %s", real_ip, e)
                        # Stamp the device's mac_address custom field (best-effort)
                        # so a future MAC-match finds this device even after a DHCP
                        # IP move, and the endpoint sync can match it by MAC. Also
                        # stamp the source-switch cfs (switch_name/ip/port) when
                        # the feed attached them — the "where is this MAC" answer
                        # lives on the device itself. One save when anything moved.
                        try:
                            devobj = self.nb.dcim.devices.get(row["id"])
                            if devobj:
                                merged = dict(devobj.custom_fields or {})
                                changed = False
                                if mac and merged.get("mac_address") != mac:
                                    merged["mac_address"] = mac
                                    changed = True
                                for cf_key, rec_key in (("switch_name", "switch_name"),
                                                        ("switch_ip", "switch_ip"),
                                                        ("switch_port", "switch_port")):
                                    val = rec.get(rec_key)
                                    if val and merged.get(cf_key) != val:
                                        merged[cf_key] = val
                                        changed = True
                                if changed:
                                    devobj.custom_fields = merged
                                    devobj.save()
                        except Exception as e:
                            logger.debug("sync_devices: device cf stamp %s: %s", ip_str, e)
                        # Rename only if we own it (don't clobber a human device's
                        # name); then mark the device seen.
                        if we_own and hostname and hostname.lower() != "unknown":
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    devobj.name = hostname
                                    devobj.save()
                                    used_names.add(hostname.strip().lower())
                                    self._stamp_last_seen(devobj)
                            except Exception as e:
                                logger.debug("sync_devices: rename %s failed: %s", ip_str, e)
                        else:
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    self._stamp_last_seen(devobj)
                            except Exception as e:
                                logger.debug("sync_devices: last_seen %s: %s", ip_str, e)
                        pushed += 1
                    else:
                        # No existing device for this IP — create one we own.
                        name = (hostname if hostname and hostname.lower() != "unknown"
                                else (f"device-{mac.replace(':', '')}" if mac
                                      else f"device-{real_ip or 'unknown'}"))
                        # The (name, site, tenant) unique constraint. Two hazards:
                        # (1) the name matches a PRE-existing device (snapshot in
                        # existing_by_name); (2) INTRA-batch duplicates — many
                        # discovery records share a hostname (ks205, sonoszp,
                        # iphone…) across distinct MACs, and existing_by_name
                        # (a pre-batch snapshot) can't see names created earlier
                        # this batch, so a 2nd create with the same name 400s.
                        # used_names tracks every name we create/refresh this
                        # batch; _uniq_device_name uniquifies against both sets.
                        # Never reclaim a name held by a device we just refreshed
                        # via IP-match (refreshed_ids) — that's a different IP's
                        # real device, not a stale orphan.
                        byname = existing_by_name.get(name.lower())
                        if byname and byname["id"] not in refreshed_ids:
                            bcf = byname.get("custom_fields") or {}
                            b_own = _owns(bcf)
                            bmac = self._norm_mac(bcf.get("mac_address", ""))
                            bpip = byname.get("primary_ip4") or {}
                            baddr = ((bpip.get("address") or "").split("/")[0].strip()
                                      if isinstance(bpip, dict) else "")
                            # Reclaim the name (delete the colliding device and
                            # recreate ours) ONLY when the colliding device is the
                            # SAME machine: incoming MAC matches its mac_address
                            # cf, OR incoming IP matches its primary_ip4, OR the
                            # name is the mac-derived form device-<incomingmac>
                            # (a no-hostname DHCP IP-move of one of our orphans).
                            # Otherwise the colliding device is a DIFFERENT machine
                            # sharing the hostname (the one case the registry allows
                            # a duplicate) → uniquify our new name and leave the
                            # other device in place. refreshed_ids + used_names
                            # guarantee we never delete a device we just refreshed
                            # or created this batch; an UNOWNED (human) collision
                            # never reclaims either (b_own False → uniquify).
                            same_device = (
                                (bool(mac) and bool(bmac) and bmac == mac)
                                or (bool(real_ip) and bool(baddr) and baddr == real_ip)
                                or (bool(mac) and name.lower() == f"device-{mac.replace(':', '')}")
                            )
                            if b_own and same_device and name.lower() not in used_names:
                                try:
                                    old = self.nb.dcim.devices.get(byname["id"])
                                    if old:
                                        old.delete()
                                        deleted += 1
                                except Exception as e:
                                    errors += 1
                                    if first_err is None:
                                        first_err = f"delete-stale {name}: {e}"
                                    logger.debug("sync_devices: delete stale by-name %s failed: %s", name, e)
                            else:
                                orig = name
                                name = self._uniq_device_name(name, mac, real_ip,
                                                              existing_by_name, used_names)
                                logger.debug("sync_devices: name %s taken; creating as %s", orig, name)
                        elif name.lower() in used_names:
                            # Intra-batch duplicate hostname: no pre-existing
                            # device by that name, but an earlier record this
                            # batch already created it.
                            orig = name
                            name = self._uniq_device_name(name, mac, real_ip,
                                                          existing_by_name, used_names)
                            logger.debug("sync_devices: hostname %s already used this batch; "
                                         "creating as %s", orig, name)
                        used_names.add(name.lower())
                        # A prior create already 400'd on a missing site — the same
                        # 400 hits every record, so stop attempting creates (count
                        # once, aggregate) instead of spamming an identical error.
                        if _site_required_abort:
                            errors += 1
                            continue
                        create_kwargs: Dict[str, Any] = {"name": name, "status": "active"}
                        if role:
                            create_kwargs["role"] = role.id
                        if dtype:
                            create_kwargs["device_type"] = dtype.id
                        if site:
                            create_kwargs["site"] = site.id
                        if tenant:
                            create_kwargs["tenant"] = tenant.id
                        devobj = self.nb.dcim.devices.create(**create_kwargs)
                        # Ownership tag + last_seen stamp (best-effort; missing
                        # custom field => no-op). last_seen clocks the staleness
                        # sweep from the moment NetBox first saw this device.
                        # Also stamp mac_address + the source-switch cfs here so a
                        # recurrence MAC-matches this device (no duplicate) and a
                        # MAC sighting carries "last seen on switch X port Y" on
                        # the device itself — the linchpin of the registry dedup.
                        try:
                            merged = dict(devobj.custom_fields or {})
                            merged["discovered_from"] = source_tag
                            merged["last_seen"] = datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ")
                            if mac:
                                merged["mac_address"] = mac
                            for cf_key, rec_key in (("switch_name", "switch_name"),
                                                    ("switch_ip", "switch_ip"),
                                                    ("switch_port", "switch_port")):
                                val = rec.get(rec_key)
                                if val:
                                    merged[cf_key] = val
                            devobj.custom_fields = merged
                            devobj.save()
                        except Exception as e:
                            logger.debug("sync_devices: discovered_from tag skipped: %s", e)
                        self._journal("dcim.device", devobj.id, source_tag,
                                      note=f"device {name}")
                        # mgmt interface + IP (mac on the IP) + primary_ip4.
                        if real_ip:
                            # Use the top-level dcim.interfaces endpoint with
                            # device=<id> rather than devobj.interfaces.create
                            # — the nested accessor isn't supported on every
                            # pynetbox version (AttributeError "object has no
                            # attribute 'interfaces'"), and it's what unblocked
                            # the create branch once the name-collision 400s were
                            # resolved.
                            iface = self.nb.dcim.interfaces.create(
                                device=devobj.id, name="mgmt", type="other")
                            mask = self._mask_for_ip(real_ip)
                            ip_kwargs: Dict[str, Any] = {
                                "address": f"{real_ip}/{mask}",
                                "assigned_object_type": "dcim.interface",
                                "assigned_object_id": iface.id,
                            }
                            if tenant:
                                ip_kwargs["tenant"] = tenant.id
                            if hostname and hostname.lower() != "unknown":
                                ip_kwargs["dns_name"] = hostname
                            # Reuse an existing global IP record (NetBox enforces
                            # global uniqueness — creating a duplicate 400s with
                            # "Duplicate IP address found in global table", which
                            # was failing ~every record because the IPAM already
                            # held most of these addresses) and reassign it to
                            # this mgmt interface instead of creating a new one.
                            ipobj = self._reuse_or_create_ip(
                                f"{real_ip}/{mask}", ip_kwargs, real_ip, iface.id,
                                tenant=tenant, hostname=hostname, mac=mac,
                                source=source_tag)
                            # Set primary_ip4 on a FRESH fetch so the save does
                            # not re-send the best-effort custom_fields merged onto
                            # ``devobj`` above (the discovered_from/last_seen/
                            # mac_address stamp). When the deployed NetBox hasn't
                            # attached those custom fields to dcim.device yet (a
                            # provisioning gap — _ensure_custom_fields self-heals
                            # it, but there's a first-sync window + a restricted
                            # token can't create them), re-sending them 400s
                            # ("Custom field 'X' does not exist for this object
                            # type") and used to kill the whole upsert: the device
                            # was created but primary_ip4 was never set, so the
                            # next sync couldn't match it (no primary_ip4, no
                            # mac_address cf) → re-create + 400 every cycle (the
                            # persistent 100+ errors/tenant in the hub log). A fresh
                            # get carries only NetBox's actually-provisioned cfs,
                            # so the save sends only valid fields. Best-effort: a
                            # failure here is logged DEBUG and the device still
                            # counts as pushed (it was created above).
                            try:
                                fresh = self.nb.dcim.devices.get(devobj.id)
                                if fresh:
                                    fresh.primary_ip4 = ipobj.id
                                    fresh.save()
                            except Exception as e:
                                logger.debug("sync_devices: set primary_ip4 %s on %s failed: %s",
                                              real_ip, name, e)
                            self._journal("ipam.ipaddress", ipobj.id,
                                          source_tag,
                                          note=f"IP {real_ip}/{mask} → {name}")
                            self._stamp_last_seen(ipobj)
                        pushed += 1
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"upsert {ip_str}: {e}"
                    # If a create failed because NetBox requires a site (none
                    # resolvable), the same 400 will hit every remaining record —
                    # flip the abort flag so the create guard above skips the rest
                    # and log ONE clear, actionable message instead of 185 dupes.
                    _es = str(e).lower()
                    if not _site_required_abort and "site" in _es and "required" in _es:
                        _site_required_abort = True
                        logger.warning(
                            "[sync-error] sync_devices tenant=%s: NetBox requires a "
                            "site for device creation but none resolved (defaults.site="
                            "%r) — set a valid 'site' slug for this sync or create a "
                            "site in NetBox; skipping remaining creates.",
                            tenant_slug or "—", defaults.get("site"))
                    logger.debug("sync_devices: upsert %s failed: %s", ip_str, e)

            msg = (f"{pushed} device(s) upserted, {deleted} deleted, "
                   f"{skipped} skipped, {errors} errors")
            if errors and first_err:
                # Surface the first failure in the returned message (the hub
                # status UI shows it) + a WARNING, so the cause is visible
                # without digging through DEBUG logs.
                msg += f" — first error: {first_err}"
                logger.warning("sync_devices tenant=%s: %s", tenant_slug or "<global>", msg)
            else:
                logger.info("sync_devices tenant=%s: %s", tenant_slug or "<global>", msg)
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "devices_total": len(incoming), "message": msg}
        except Exception as e:
            logger.error("sync_devices failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "devices_total": len(devices or [])}

    def sync_nw_device(self, device: dict, interfaces: list,
                       tenant_slug: str = "",
                       defaults: Optional[Dict[str, Any]] = None,
                       source: str = "Network Devices") -> Dict[str, Any]:
        """Upsert ONE polled network device (switch/gateway) as a NetBox
        ``dcim.device`` with its ``dcim.interfaces`` + per-interface IPs.

        Called by the hub's POLL NOW path (``NETBOX_SYNC_NW_DEVICE``) with the
        live SNMP/CLI/REST poll of a single fleet device. This is the
        network-device **inventory** path — distinct from the scheduled
        ARP-neighbor→endpoint ``sync_devices`` flow: here the polled device
        ITSELF becomes the NetBox device, and its learned interfaces become
        ``dcim.interfaces`` (name/MAC/status/speed) with an ``ipam.ip_address``
        per interface IP.

        Match the existing device by ``custom_fields.nw_device_id`` (the fleet
        id) first, else by name within the tenant. Create if missing, tagged
        ``discovered_from=<source>`` + ``nw_device_id=<id>``; set ``primary_ip4``
        from the device management address. Each incoming interface is upserted
        by name (native ``mac_address``, ``status``, ``speed``) with a
        reused/created IP attached. ``replace=True`` deletes interfaces we
        created (``nw_managed`` marker) whose name is absent from the incoming
        set — so NetBox tracks the live switch without touching manually-created
        interfaces.

        ``defaults`` (``device_type``/``role``/``site`` slugs) are required to
        CREATE a device; an existing match needs only an interface refresh. A
        missing default on the create path returns an ERROR naming it (never
        silently fail).

        Returns ``{status, pushed, errors, skipped, deleted, interfaces_total,
        device_id, message}``.
        """
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None
        defaults = defaults or {}
        source_tag = str(source or "Network Devices").strip() or "Network Devices"
        try:
            self._ensure_custom_fields()
            dev = device or {}
            nw_id = str(dev.get("id") or "").strip()
            name = str(dev.get("name") or "").strip()
            mgmt_ip = str(dev.get("address") or "").strip().split("/")[0].strip()
            if not name and not nw_id:
                return {"status": "ERROR", "message": "nw device has no name/id",
                        "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                        "interfaces_total": len(interfaces or []), "device_id": None}
            if not name:
                name = f"nw-{nw_id[:8]}" if nw_id else "nw-device"

            # Case-insensitive tenant resolve (slug OR name → canonical slug),
            # mirroring sync_vms' lookup so a mixed-case/none tenant still lands.
            tenant = None
            if tenant_slug:
                tenant = self._resolve_tenant_ci(tenant_slug)

            # ── Resolve the existing device (by nw_device_id cf, else by name) ──
            existing = None
            try:
                params: Dict[str, Any] = {"limit": 500}
                if tenant:
                    params["tenant"] = tenant.id
                rows = self._api_get_all("/api/dcim/devices/", params)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                        "interfaces_total": len(interfaces or []), "device_id": None}
            for row in rows:
                cf = row.get("custom_fields") or {}
                if nw_id and str(cf.get("nw_device_id") or "").strip() == nw_id:
                    existing = row
                    break
            if existing is None and name:
                nl = name.lower()
                for row in rows:
                    if str(row.get("name") or "").strip().lower() == nl:
                        existing = row
                        break

            # ── Create the device if missing (defaults required) ────────────────
            if existing is None:
                dt_slug = str(defaults.get("device_type") or "").strip()
                role_slug = str(defaults.get("role") or "").strip()
                site_slug = str(defaults.get("site") or "").strip()
                missing = [k for k, v in (("device_type", dt_slug),
                                         ("role", role_slug),
                                         ("site", site_slug)) if not v]
                if missing:
                    return {"status": "ERROR",
                            "message": f"cannot create nw device {name!r}: missing "
                                       f"default(s): {', '.join(missing)} "
                                       f"(set them in Setup → Network Devices → "
                                       f"IPAM Sync defaults)",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "interfaces_total": len(interfaces or []), "device_id": None}
                try:
                    dt = self.nb.dcim.device_types.get(slug=dt_slug)
                    role = self.nb.dcim.device_roles.get(slug=role_slug)
                    site = self.nb.dcim.sites.get(slug=site_slug)
                    ck: Dict[str, Any] = {"name": name, "device_type": dt.id,
                                          "role": role.id, "site": site.id}
                    if tenant:
                        ck["tenant"] = tenant.id
                    devobj = self.nb.dcim.devices.create(**ck)
                    # Tag ownership + link to the fleet id (best-effort cf PATCH).
                    try:
                        devobj.custom_fields = {
                            "discovered_from": source_tag,
                            "nw_device_id": nw_id,
                            "last_seen": datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"),
                        }
                        devobj.save()
                    except Exception as e:
                        logger.warning("sync_nw_device: tag new device %s skipped: %s",
                                       nw_id or name, e)
                    self._journal("dcim.device", devobj.id, "nw-poll",
                                  note=f"NW device {name} ({nw_id or 'no id'})")
                except Exception as e:
                    return {"status": "ERROR",
                            "message": f"create nw device {name!r} failed: {e}",
                            "pushed": 0, "errors": 1, "skipped": 0, "deleted": 0,
                            "interfaces_total": len(interfaces or []), "device_id": None}
            else:
                devobj = self.nb.dcim.devices.get(existing["id"])
                if not devobj:
                    return {"status": "ERROR",
                            "message": f"existing nw device {existing['id']} vanished",
                            "pushed": 0, "errors": 1, "skipped": 0, "deleted": 0,
                            "interfaces_total": len(interfaces or []), "device_id": None}
                # Refresh ownership + last_seen (best-effort).
                try:
                    cf = dict(devobj.custom_fields or {})
                    cf["discovered_from"] = source_tag
                    if nw_id:
                        cf["nw_device_id"] = nw_id
                    cf["last_seen"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    devobj.custom_fields = cf
                    devobj.save()
                except Exception as e:
                    logger.debug("sync_nw_device: refresh device cf skipped: %s", e)

            dev_id = devobj.id

            # ── Index existing interfaces on this device by name (lowercased) ──
            existing_ifaces: Dict[str, Any] = {}
            try:
                for ifc in self.nb.dcim.interfaces.filter(device=dev_id):
                    existing_ifaces[str(getattr(ifc, "name", "") or "").lower()] = ifc
            except Exception as e:
                logger.debug("sync_nw_device: list interfaces for %s failed: %s",
                             dev_id, e)

            incoming_names: set = set()
            for ifc in (interfaces or []):
                if not isinstance(ifc, dict):
                    continue
                ifname = str(ifc.get("name") or "").strip()
                if not ifname:
                    skipped += 1
                    continue
                incoming_names.add(ifname.lower())
                mac = self._norm_mac(ifc.get("mac", ""))
                status = "active" if str(ifc.get("status") or "").strip().lower() in (
                    "up", "active", "1") else "planned"
                speed_val = ifc.get("speed")
                try:
                    sv = int(speed_val) if speed_val not in (None, "") else 0
                except (TypeError, ValueError):
                    sv = 0
                # ifSpeed is bps; NetBox interface speed is Kbps. Convert when the
                # value is clearly bps (>=1e6); else assume already Kbps.
                speed_kbps = sv // 1000 if sv >= 1_000_000 else sv
                ip = str(ifc.get("ip") or "").strip().split("/")[0].strip()
                vlan = str(ifc.get("vlan") or "").strip()
                try:
                    match = existing_ifaces.get(ifname.lower())
                    if match is None:
                        ik: Dict[str, Any] = {"device": dev_id, "name": ifname,
                                              "type": "other", "status": status}
                        if mac:
                            ik["mac_address"] = mac
                        if speed_kbps > 0:
                            ik["speed"] = speed_kbps
                        # VLAN: best-effort tagged vlan (NetBox expects a list of
                        # vlan ids or a single untagged vlan id). We only have a
                        # vlan label/number from the poll; skip unless numeric.
                        iface_obj = self.nb.dcim.interfaces.create(**ik)
                        try:
                            iface_obj.custom_fields = {"nw_managed": "true"}
                            iface_obj.save()
                        except Exception as e:
                            logger.debug("sync_nw_device: tag new iface %s skipped: %s",
                                         ifname, e)
                    else:
                        iface_obj = match
                        changed = False
                        if getattr(iface_obj, "status", None) != status:
                            iface_obj.status = status
                            changed = True
                        if mac and str(getattr(iface_obj, "mac_address", "") or "") != mac:
                            iface_obj.mac_address = mac
                            changed = True
                        if speed_kbps > 0 and int(getattr(iface_obj, "speed", 0) or 0) != speed_kbps:
                            iface_obj.speed = speed_kbps
                            changed = True
                        if changed:
                            iface_obj.save()
                        # Mark an adopted interface as nw-managed (best-effort) so
                        # replace-delete can track it on later polls.
                        try:
                            m = dict(iface_obj.custom_fields or {})
                            if str(m.get("nw_managed") or "") != "true":
                                m["nw_managed"] = "true"
                                iface_obj.custom_fields = m
                                iface_obj.save()
                        except Exception as e:
                            logger.debug("sync_nw_device: mark adopted iface %s: %s",
                                         ifname, e)

                    # Per-interface IP (reuse global record, attach to the iface).
                    if ip:
                        mask = self._mask_for_ip(ip)
                        ip_kwargs = {
                            "address": f"{ip}/{mask}",
                            "assigned_object_type": "dcim.interface",
                            "assigned_object_id": iface_obj.id,
                        }
                        if tenant:
                            ip_kwargs["tenant"] = tenant.id
                        try:
                            ipobj = self._reuse_or_create_ip(
                                f"{ip}/{mask}", ip_kwargs, ip, iface_obj.id,
                                tenant=tenant, hostname="", mac=mac,
                                source="nw-poll", iface_type="dcim.interface")
                            # Device primary_ip4 from the management address.
                            if mgmt_ip and ip == mgmt_ip and \
                                    not getattr(devobj, "primary_ip4", None):
                                devobj.primary_ip4 = ipobj.id
                                devobj.save()
                            self._stamp_last_seen(ipobj)
                        except Exception as e:
                            errors += 1
                            if first_err is None:
                                first_err = f"iface {ifname} ip {ip}: {e}"
                            logger.debug("sync_nw_device: iface %s ip %s failed: %s",
                                         ifname, ip, e)
                    pushed += 1
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"iface {ifname}: {e}"
                    logger.debug("sync_nw_device: iface %s failed: %s", ifname, e)

            # ── Replace-delete: drop nw-managed interfaces no longer reported ──
            for ifname_l, ifc_obj in list(existing_ifaces.items()):
                    if ifname_l in incoming_names:
                        continue
                    try:
                        cf = dict(ifc_obj.custom_fields or {})
                        if str(cf.get("nw_managed") or "") != "true":
                            continue  # never delete a manually-created interface
                        ifc_obj.delete()
                        deleted += 1
                    except Exception as e:
                        errors += 1
                        if first_err is None:
                            first_err = f"delete iface {ifname_l}: {e}"
                        logger.debug("sync_nw_device: delete stale iface %s failed: %s",
                                     ifname_l, e)

            msg = (f"nw device {name} upserted: {pushed} interface(s), "
                   f"{deleted} deleted, {skipped} skipped, {errors} errors")
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("sync_nw_device: %s", msg)
            else:
                logger.info("sync_nw_device: %s", msg)
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "interfaces_total": len(interfaces or []),
                    "device_id": dev_id, "message": msg}
        except Exception as e:
            logger.error("sync_nw_device failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "interfaces_total": len(interfaces or []), "device_id": None}

    def _resolve_tenant_ci(self, slug: str) -> Any:
        """Case-insensitive tenant resolve: lower(slug) ∪ lower(name) → tenant
        object. Returns None when not found. Mirrors sync_vms' per-batch lookup
        but as a reusable helper. NetBox slugs are conventionally lowercase, but
        a configured tenant_slug may arrive mixed-case or as the display name."""
        s = str(slug or "").strip()
        if not s:
            return None
        try:
            lut: Dict[str, str] = {}
            for t in self._api_get_all("/api/tenancy/tenants/"):
                tslug = str((t or {}).get("slug") or "").strip()
                if not tslug:
                    continue
                lut.setdefault(tslug.lower(), tslug)
                nm = str((t or {}).get("name") or "").strip()
                if nm:
                    lut.setdefault(nm.lower(), tslug)
            canon = lut.get(s.lower()) or s
            return self.nb.tenancy.tenants.get(slug=canon)
        except Exception as e:
            logger.debug("resolve_tenant_ci %s failed: %s", s, e)
            try:
                return self.nb.tenancy.tenants.get(slug=s)
            except Exception:
                return None

    def sync_access_tracker(self, sessions: list, tenant_slug: str = "",
                            defaults: Optional[Dict[str, Any]] = None,
                            source_of_truth: str = "netbox") -> Dict[str, Any]:
        """Pull ClearPass Access Tracker / session data INTO NetBox (NAC→IPAM
        reverse sync; the bidirectional counterpart to ``EndpointSyncMixin``).

        Source = CPPM ``/api/session`` (relayed by the hub realtime loop). Each
        incoming session ``{mac, ip, nas_ip, nas_port, nas_name, username,
        start_time}`` is matched **MAC-first** against the tenant's existing
        devices (keyed by the device's ``custom_fields.mac_address``). NetBox
        stays source of truth → this is **only-add-missing**: a MAC already in
        NetBox is skipped (never duplicated, never overwritten), with a
        best-effort ``last_seen``/``switch_ip``/``switch_port`` refresh on
        devices *we* created (``discovered_from == "cppm-access-tracker"``); a
        MAC not in NetBox → a device is created.

        Created endpoint device mirrors ``sync_devices`` (tenant-owned device +
        NIC interface carrying the native MAC + framed IP + ``primary_ip4``),
        tagged ``discovered_from = "cppm-access-tracker"`` with
        ``mac_address``/``switch_ip``/``switch_port``/``last_seen`` custom
        fields. Full switch topology is built best-effort: a switch
        ``dcim.devices`` (role ``switch``) keyed by NAS IP, a port-named
        ``dcim.interfaces`` on it, and a ``dcim.cables`` connection from the
        endpoint NIC to that switch port — idempotent, with a graceful fallback
        to the custom-field record if the cable API differs on the deployed
        NetBox (the device + IP + MAC are still synced).

        ``replace`` is always False here (only-add-missing by design — never
        delete hand-managed NetBox records). Returns ``{status, pushed, errors,
        skipped, deleted, sessions_total, message}``.
        """
        pushed = 0; errors = 0; skipped = 0; deleted = 0
        first_err: Optional[str] = None
        defaults = defaults or {}
        try:
            self._ensure_custom_fields()
            tenant = None
            if tenant_slug:
                tenant = self.nb.tenancy.tenants.get(slug=tenant_slug)
                if not tenant:
                    return {"status": "ERROR",
                            "message": f"NetBox tenant '{tenant_slug}' not found — "
                                       f"access-tracker sessions not attributed.",
                            "pushed": 0, "errors": 0, "skipped": 0, "deleted": 0,
                            "sessions_total": len(sessions or [])}

            role = self._ensure_device_role(defaults.get("role") or "discovered")
            dtype = self._ensure_device_type(defaults.get("device_type") or "discovered")
            site = self._resolve_site(defaults.get("site") or "", tenant)
            switch_role = self._ensure_device_role(defaults.get("switch_role") or "switch")
            switch_dtype = self._ensure_device_type(defaults.get("switch_device_type") or "switch")

            # MAC-first index of the tenant's existing devices (the device's
            # custom_fields.mac_address), plus a primary-IP index (to find the
            # switch by NAS IP) and a name index for uniquification. Reuses the
            # same intra-batch used_names dedup that fixed sync_devices.
            existing_by_mac: Dict[str, dict] = {}
            existing_by_ip: Dict[str, dict] = {}
            existing_by_name: Dict[str, dict] = {}
            used_names: set = set()
            list_params: Dict[str, Any] = {"limit": 500}
            if tenant_slug:
                list_params["tenant"] = tenant_slug
            try:
                rows = self._api_get_all("/api/dcim/devices/", list_params)
            except Exception as e:
                return {"status": "ERROR",
                        "message": f"failed to list NetBox devices: {e}",
                        "pushed": 0, "errors": 0, "skipped": skipped, "deleted": 0,
                        "sessions_total": len(sessions or [])}
            for row in rows:
                rname = str(row.get("name") or "").strip().lower()
                if rname:
                    existing_by_name.setdefault(rname, row)
                cf = row.get("custom_fields") or {}
                mac_cf = self._norm_mac(cf.get("mac_address") or "")
                if mac_cf:
                    existing_by_mac.setdefault(mac_cf, row)
                pip = row.get("primary_ip4")
                addr = ""
                if isinstance(pip, dict):
                    addr = (pip.get("address") or "").split("/")[0].strip()
                if addr:
                    existing_by_ip.setdefault(addr, row)

            # Switch-topology caches (this batch): NAS-IP → switch device,
            # (switch.id, nas_port) → port interface.
            switch_by_ip: Dict[str, dict] = {}
            port_iface_by_key: Dict[tuple, Any] = {}

            def _ensure_switch(nas_ip: str, nas_name: str):
                """Get-or-create a switch device by its NAS IP (IP-keyed upsert,
                mirroring sync_devices' IP match). Returns the device row dict or
                None on failure."""
                if not nas_ip:
                    return None
                row = existing_by_ip.get(nas_ip) or switch_by_ip.get(nas_ip)
                if row:
                    switch_by_ip[nas_ip] = row
                    return row
                name = (nas_name or f"switch-{nas_ip}").strip() or f"switch-{nas_ip}"
                name = self._uniq_device_name(name, "", nas_ip, existing_by_name, used_names)
                used_names.add(name.lower())
                ck: Dict[str, Any] = {"name": name, "status": "active"}
                if switch_role:
                    ck["role"] = switch_role.id
                if switch_dtype:
                    ck["device_type"] = switch_dtype.id
                if site:
                    ck["site"] = site.id
                if tenant:
                    ck["tenant"] = tenant.id
                try:
                    sw = self.nb.dcim.devices.create(**ck)
                    # mgmt interface holding the NAS IP + primary_ip4 so the
                    # next batch finds this switch by IP (existing_by_ip path).
                    if sw:
                        try:
                            miface = self.nb.dcim.interfaces.create(
                                device=sw.id, name="mgmt", type="other")
                            mask = self._mask_for_ip(nas_ip)
                            ipo = self.nb.ipam.ip_addresses.create(
                                address=f"{nas_ip}/{mask}",
                                assigned_object_type="dcim.interface",
                                assigned_object_id=miface.id)
                            if tenant:
                                ipo.tenant = tenant.id
                                ipo.save()
                            sw.primary_ip4 = ipo.id
                            sw.save()
                        except Exception as e:
                            logger.debug("sync_access_tracker: switch mgmt/IP %s skipped: %s", nas_ip, e)
                        row = {"id": sw.id, "name": sw.name,
                               "primary_ip4": {"address": f"{nas_ip}/{self._mask_for_ip(nas_ip)}"}}
                        switch_by_ip[nas_ip] = row
                        existing_by_ip[nas_ip] = row
                        return row
                except Exception as e:
                    logger.debug("sync_access_tracker: create switch %s failed: %s", nas_ip, e)
                return None

            def _ensure_switch_port(switch_row: dict, nas_port: str):
                """Get-or-create the named port interface on the switch."""
                if not switch_row or not nas_port:
                    return None
                key = (switch_row["id"], nas_port)
                cached = port_iface_by_key.get(key)
                if cached:
                    return cached
                try:
                    iface = self.nb.dcim.interfaces.get(
                        device=switch_row["id"], name=nas_port)
                    if iface:
                        port_iface_by_key[key] = iface
                        return iface
                except Exception as e:
                    logger.debug("sync_access_tracker: find port %s failed: %s", nas_port, e)
                try:
                    iface = self.nb.dcim.interfaces.create(
                        device=switch_row["id"], name=nas_port, type="other")
                    port_iface_by_key[key] = iface
                    return iface
                except Exception as e:
                    logger.debug("sync_access_tracker: create port %s failed: %s", nas_port, e)
                return None

            def _cable_nic_to_port(nic, port) -> None:
                """Idempotently cable the endpoint NIC to the switch port. Skips
                if the NIC already has a connected endpoint. On any cable API
                failure (pynetbox/NetBox version mismatch on terminations) →
                WARNING + fall back to the custom-field record already written
                (switch_ip/switch_port on the device). Never raises."""
                try:
                    nic_obj = self.nb.dcim.interfaces.get(nic.id)
                    if nic_obj and getattr(nic_obj, "connected_endpoint", None):
                        return  # already cabled — don't create a second link
                except Exception as e:
                    logger.debug("sync_access_tracker: nic connected_endpoint check: %s", e)
                try:
                    self.nb.dcim.cables.create(
                        a_terminations=[{"object_type": "dcim.interface",
                                         "object_id": nic.id}],
                        b_terminations=[{"object_type": "dcim.interface",
                                         "object_id": port.id}],
                        status="connected")
                except Exception as e:
                    # Cable API differs across NetBox versions (legacy used
                    # termination_a_type/termination_a_id). The device + IP +
                    # MAC + switch_ip/switch_port custom fields are already
                    # written, so topology is recorded even without the cable.
                    logger.warning("sync_access_tracker: cable NIC→%s skipped "
                                   "(custom-field fallback): %s", nas_port, e)

            for s in (sessions or []):
                if not isinstance(s, dict):
                    continue
                try:
                    mac = self._norm_mac(s.get("mac", ""))
                    ip = str(s.get("ip") or "").strip().split("/")[0].strip()
                    nas_ip = str(s.get("nas_ip") or "").strip().split("/")[0].strip()
                    nas_port = str(s.get("nas_port") or "").strip()
                    nas_name = str(s.get("nas_name") or "").strip()
                    username = str(s.get("username") or "").strip()
                    start_time = str(s.get("start_time") or "").strip()
                    if not mac:
                        skipped += 1
                        continue

                    row = existing_by_mac.get(mac)
                    if row:
                        # Already in NetBox → only-add-missing: skip the create.
                        # Best-effort refresh of topology/last_seen ONLY on a
                        # device we own (never touch another source's record).
                        cf = row.get("custom_fields") or {}
                        if str((cf.get("discovered_from") or "")).lower() == "cppm-access-tracker":
                            try:
                                devobj = self.nb.dcim.devices.get(row["id"])
                                if devobj:
                                    merged = dict(devobj.custom_fields or {})
                                    if start_time:
                                        merged["last_seen"] = start_time
                                    if nas_ip:
                                        merged["switch_ip"] = nas_ip
                                    if nas_port:
                                        merged["switch_port"] = nas_port
                                    if nas_name:
                                        merged["switch_name"] = nas_name
                                    devobj.custom_fields = merged
                                    devobj.save()
                            except Exception as e:
                                logger.debug("sync_access_tracker: refresh %s failed: %s", mac, e)
                        skipped += 1
                        continue

                    # Create the missing endpoint device.
                    name = (username or f"device-{mac.replace(':', '')}")
                    name = self._uniq_device_name(name, mac, ip, existing_by_name, used_names)
                    used_names.add(name.lower())
                    create_kwargs: Dict[str, Any] = {"name": name, "status": "active"}
                    if role:
                        create_kwargs["role"] = role.id
                    if dtype:
                        create_kwargs["device_type"] = dtype.id
                    if site:
                        create_kwargs["site"] = site.id
                    if tenant:
                        create_kwargs["tenant"] = tenant.id
                    devobj = self.nb.dcim.devices.create(**create_kwargs)
                    # Ownership tag + topology custom fields (best-effort; same
                    # create-without-cf + best-effort PATCH lesson as sync_vms).
                    try:
                        merged = dict(devobj.custom_fields or {})
                        merged["discovered_from"] = "cppm-access-tracker"
                        merged["mac_address"] = mac
                        if nas_ip:
                            merged["switch_ip"] = nas_ip
                        if nas_port:
                            merged["switch_port"] = nas_port
                        if nas_name:
                            merged["switch_name"] = nas_name
                        if start_time:
                            merged["last_seen"] = start_time
                        devobj.custom_fields = merged
                        devobj.save()
                    except Exception as e:
                        logger.debug("sync_access_tracker: cf tag skipped: %s", e)
                    self._journal("dcim.device", devobj.id,
                                  "realtime-nac-access-tracker",
                                  note=f"endpoint {name} (MAC {mac})")

                    # NIC interface (native MAC) + framed IP + primary_ip4.
                    nic = None
                    if ip:
                        nic = self.nb.dcim.interfaces.create(
                            device=devobj.id, name="eth0", type="other",
                            mac_address=mac)
                        mask = self._mask_for_ip(ip)
                        ip_kwargs: Dict[str, Any] = {
                            "address": f"{ip}/{mask}",
                            "assigned_object_type": "dcim.interface",
                            "assigned_object_id": nic.id,
                        }
                        if tenant:
                            ip_kwargs["tenant"] = tenant.id
                        if username:
                            ip_kwargs["dns_name"] = username
                        # Reuse an existing global IP record (NetBox enforces
                        # global uniqueness) and reassign it to this NIC instead
                        # of creating a duplicate that 400s — same fix as
                        # sync_devices, for the same root cause.
                        ipobj = self._reuse_or_create_ip(
                            f"{ip}/{mask}", ip_kwargs, ip, nic.id,
                            tenant=tenant, hostname=username, mac=mac,
                            source="realtime-nac-access-tracker")
                        devobj.primary_ip4 = ipobj.id
                        devobj.save()
                        self._journal("ipam.ipaddress", ipobj.id,
                                      "realtime-nac-access-tracker",
                                      note=f"framed IP {ip}/{mask} → {name}")
                        self._stamp_last_seen(ipobj, when=start_time)

                    # Switch topology (best-effort, never breaks the sync).
                    if nas_ip:
                        sw = _ensure_switch(nas_ip, nas_name)
                        if sw and nas_port and nic is not None:
                            port = _ensure_switch_port(sw, nas_port)
                            if port:
                                _cable_nic_to_port(nic, port)

                    # Track the new device so a later duplicate-MAC session in
                    # the same batch skips instead of re-creating.
                    new_row = {"id": devobj.id, "name": devobj.name,
                               "custom_fields": {"mac_address": mac,
                                                 "discovered_from": "cppm-access-tracker"}}
                    existing_by_mac[mac] = new_row
                    if ip:
                        existing_by_ip.setdefault(ip, new_row)
                    pushed += 1
                except Exception as e:
                    errors += 1
                    if first_err is None:
                        first_err = f"upsert {s.get('mac','?')}: {e}"
                    logger.debug("sync_access_tracker: upsert failed: %s", e)

            msg = (f"{pushed} endpoint(s) added, {skipped} already present, "
                   f"{deleted} deleted, {errors} errors")
            if errors and first_err:
                msg += f" — first error: {first_err}"
                logger.warning("sync_access_tracker tenant=%s: %s", tenant_slug or "<global>", msg)
            else:
                logger.info("sync_access_tracker tenant=%s: %s", tenant_slug or "<global>", msg)
            return {"status": "SUCCESS", "pushed": pushed, "errors": errors,
                    "skipped": skipped, "deleted": deleted,
                    "sessions_total": len(sessions or []), "message": msg}
        except Exception as e:
            logger.error("sync_access_tracker failed: %s", e)
            return {"status": "ERROR", "message": str(e), "pushed": pushed,
                    "errors": errors, "skipped": skipped, "deleted": deleted,
                    "sessions_total": len(sessions or [])}

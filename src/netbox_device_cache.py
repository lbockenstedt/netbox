"""The spoke's own copy of every NetBox device, kept on disk per tenant.

Paging every NetBox device to answer "does this box already exist?" was most
of NetBox's load. Instead the spoke keeps:

* a slim identity index in memory: per device only what matching needs
  (id, tenant, name, serial, role slug for management controllers, the
  mac_address / nw_device_id / discovered_from custom fields, the primary
  IPv4 and last_updated), shaped like a NetBox row so the matching code reads
  it the same way;
* the full rows on disk, one JSON file per tenant
  (``<CACHE_DIR>/<tenant-slug>.json``, ``_none.json`` for devices without a
  tenant), read only when a sync needs that tenant's rows.

Every device write the spoke makes goes through ``on_response`` (a requests
response hook on the engine's NetBox session), so the copy follows our own
creates, updates and deletes without asking NetBox again. Changes made
outside LM are picked up by the daily per-tenant reconcile (netbox_sync) and
by the single-device reads the sync paths already make. NetBox is paged in
full only when the cache is missing, unreadable, from an older format, or
belongs to another NetBox URL.
"""
import json
import logging
import os
import re
import threading
import time
import zlib
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("NetboxEngine")

# Where the per-tenant files live; tests point it at a temporary directory.
CACHE_DIR = os.getenv("LM_NETBOX_DEVICE_CACHE_DIR", "/var/lib/lm/netbox/devices")
# Bump when the file layout changes: an older cache is rebuilt once.
VERSION = 2
# Each tenant is reconciled once a day; its time of day comes from its slug.
RECONCILE_EVERY_S = 86400
# File name for devices without a tenant.
NO_TENANT = "_none"
# Fields kept from a NetBox device row in the per-tenant files.
FULL_FIELDS = ("id", "name", "serial", "role", "device_type", "status", "site",
               "tenant", "rack", "primary_ip4", "url", "last_updated", "custom_fields")
# Custom fields the slim index keeps (identity and ownership).
_SLIM_CF = ("mac_address", "nw_device_id", "discovered_from")
_DEVICE_PATH_RE = re.compile(r"/api/dcim/devices/(?:(\d+)/)?$")


def tenant_key(row: dict) -> str:
    """File key of a row's tenant: its slug, or NO_TENANT."""
    t = row.get("tenant")
    slug = t.get("slug") if isinstance(t, dict) else ""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(slug or "")) or NO_TENANT


def trim_row(row: dict) -> dict:
    """A NetBox device row reduced to FULL_FIELDS."""
    return {k: row.get(k) for k in FULL_FIELDS if k in row}


def slim_row(row: dict) -> dict:
    """The identity fields of a device row, in NetBox's row shape."""
    t, role, pip = row.get("tenant"), row.get("role") or row.get("device_role"), row.get("primary_ip4")
    cf = row.get("custom_fields") or {}
    return {
        "id": row.get("id"), "name": row.get("name"), "serial": row.get("serial") or "",
        "tenant": {"id": t.get("id"), "slug": t.get("slug")} if isinstance(t, dict) else None,
        "role": {"slug": role.get("slug")} if isinstance(role, dict) else None,
        "primary_ip4": ({"id": pip.get("id"), "address": pip.get("address")}
                        if isinstance(pip, dict) else None),
        "custom_fields": {k: cf[k] for k in _SLIM_CF if cf.get(k)},
        "last_updated": row.get("last_updated"),
    }


def write_json(path: str, data: Any) -> None:
    """Write ``data`` as JSON atomically: a temporary file, then a rename."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        # A failed write leaves the old file as it was, and no partial file.
        if os.path.exists(tmp):
            os.remove(tmp)


class DeviceCache:
    """Slim index in memory, full rows per tenant on disk (see module doc)."""

    def __init__(self, path: str, netbox_url: str = ""):
        """Cache in directory ``path`` for the NetBox at ``netbox_url``."""
        self.dir = path
        self.netbox_url = netbox_url
        self.lock = threading.RLock()
        # Held while the cache is rebuilt from NetBox, so only one caller pages it.
        self.build_lock = threading.Lock()
        self._index: Optional[Dict[Any, dict]] = None  # id -> slim row
        self._meta: Dict[str, Any] = {}
        # Tenant rows held in memory instead, when the directory can't be written.
        self._mem: Optional[Dict[str, List[dict]]] = None

    # ── files ──────────────────────────────────────────────────────────────

    def _path(self, key: str) -> str:
        """Path of one tenant's file."""
        return os.path.join(self.dir, key + ".json")

    def _meta_path(self) -> str:
        """Path of the file holding the version, NetBox URL and reconcile times."""
        return os.path.join(self.dir, "_meta.json")

    def _read_tenant(self, key: str) -> List[dict]:
        """One tenant's full rows; raises when the file is unreadable."""
        if self._mem is not None:
            return [dict(r) for r in self._mem.get(key, [])]
        with open(self._path(key)) as f:
            data = json.load(f)
        if data.get("version") != VERSION:
            raise ValueError("old cache format")
        return data["rows"]

    def _write_tenant(self, key: str, rows: List[dict]) -> None:
        """Replace one tenant's file (removed when it has no devices left)."""
        if self._mem is not None:
            self._mem[key] = rows
        elif rows:
            write_json(self._path(key), {"version": VERSION, "rows": rows})
        else:
            try:
                os.remove(self._path(key))
            except FileNotFoundError:
                pass

    def _save_meta(self) -> None:
        """Persist the meta file (best-effort)."""
        if self._mem is not None:
            return
        try:
            write_json(self._meta_path(), self._meta)
        except OSError as e:
            logger.warning("device cache: could not write %s: %s", self._meta_path(), e)

    # ── load / rebuild ─────────────────────────────────────────────────────

    def load(self) -> bool:
        """True when the slim index is ready, reading it from disk if needed.
        False (caller rebuilds) when the files are missing, unreadable, from
        an older format or from another NetBox."""
        with self.lock:
            if self._index is not None:
                return True
            # Read the meta file: version and NetBox URL must match.
            try:
                with open(self._meta_path()) as f:
                    meta = json.load(f)
                if meta.get("version") != VERSION or meta.get("netbox") != self.netbox_url:
                    return False
                # Every tenant file builds the slim index.
                index: Dict[Any, dict] = {}
                for name in os.listdir(self.dir):
                    if name.endswith(".json") and not name.startswith("_meta"):
                        for row in self._read_tenant(name[:-5]):
                            index[row.get("id")] = slim_row(row)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                logger.info("device cache: unreadable (%s), rebuilding from NetBox", e)
                return False
            self._meta, self._index = meta, index
            return True

    def rebuild(self, rows: Iterable[dict], now: Optional[float] = None) -> None:
        """Replace the whole cache with ``rows`` (every NetBox device)."""
        now = time.time() if now is None else now
        with self.lock:
            # Start from an empty directory, and drop the single-file cache
            # older versions kept next to it.
            self.clear()
            try:
                os.remove(os.path.join(os.path.dirname(self.dir), "device_cache.json"))
            except OSError:
                pass
            # Group by tenant and write one file each.
            by_tenant: Dict[str, List[dict]] = {}
            for row in rows:
                by_tenant.setdefault(tenant_key(row), []).append(trim_row(row))
            # Meta last, so a crash part-way leaves a cache that rebuilds again.
            self._meta = {"version": VERSION, "netbox": self.netbox_url, "built": now,
                          "reconcile": {}}
            try:
                for key, trows in by_tenant.items():
                    self._write_tenant(key, trows)
                write_json(self._meta_path(), self._meta)
            except OSError as e:
                # Unwritable directory: keep the rows in memory until restart
                # rather than paging NetBox again on every call.
                logger.warning("device cache: could not write %s (%s); keeping it in memory",
                               self.dir, e)
                self._mem = by_tenant
            self._index = {r["id"]: slim_row(r) for t in by_tenant.values() for r in t}

    def clear(self) -> None:
        """Forget everything (memory and disk); the next load fails and rebuilds."""
        with self.lock:
            self._index = None
            self._meta = {}
            self._mem = None
            try:
                names = os.listdir(self.dir)
            except OSError:
                names = []
            for name in names:
                if name.endswith((".json", ".tmp")):
                    try:
                        os.remove(os.path.join(self.dir, name))
                    except OSError:
                        pass

    # ── reads ──────────────────────────────────────────────────────────────

    def rows(self) -> List[dict]:
        """Slim rows of every cached device (call ``load`` first); copies, so
        a caller may change them."""
        with self.lock:
            return [dict(r) for r in (self._index or {}).values()]

    def tenant_rows(self, slug: str) -> List[dict]:
        """Full rows of one tenant (``""`` = no tenant). An unreadable file
        clears the cache and raises, so the caller's next load rebuilds."""
        key = tenant_key({"tenant": {"slug": slug}})
        with self.lock:
            try:
                return self._read_tenant(key)
            except FileNotFoundError:
                return []
            except (OSError, ValueError, KeyError, TypeError) as e:
                self.clear()
                raise RuntimeError(f"device cache file for {key} unreadable: {e}") from e

    def full_row(self, dev_id: Any) -> Optional[dict]:
        """One device's full row from its tenant's file, or None."""
        with self.lock:
            slim = (self._index or {}).get(dev_id)
            if slim is None:
                return None
            try:
                rows = self._read_tenant(tenant_key(slim))
            except (OSError, ValueError, KeyError, TypeError):
                return None
            return next((r for r in rows if r.get("id") == dev_id), None)

    # ── writes ─────────────────────────────────────────────────────────────

    def upsert(self, rows: Iterable[dict]) -> None:
        """Store fresh NetBox rows (a write response or a filtered read),
        moving a device between tenant files when its tenant changed."""
        with self.lock:
            if not self.load():
                return  # nothing to update; the next read rebuilds anyway
            # Which files gain which rows, and which lose a moved device.
            add: Dict[str, Dict[Any, dict]] = {}
            drop: Dict[str, set] = {}
            for row in rows:
                if not isinstance(row, dict) or row.get("id") is None:
                    continue
                key = tenant_key(row)
                old = self._index.get(row["id"])
                # Unchanged since we stored it (a read of a known device): skip.
                if (old is not None and row.get("last_updated")
                        and old.get("last_updated") == row.get("last_updated")
                        and tenant_key(old) == key):
                    continue
                add.setdefault(key, {})[row["id"]] = trim_row(row)
                if old is not None and tenant_key(old) != key:
                    drop.setdefault(tenant_key(old), set()).add(row["id"])
            # Rewrite each touched file once, then the index.
            for key in set(add) | set(drop):
                new = add.get(key, {})
                gone = drop.get(key, set())
                kept = [r for r in self._safe_read(key)
                        if r.get("id") not in new and r.get("id") not in gone]
                self._write_tenant(key, kept + list(new.values()))
            for new in add.values():
                for dev_id, row in new.items():
                    self._index[dev_id] = slim_row(row)

    def remove(self, ids: Iterable[Any]) -> None:
        """Forget deleted devices."""
        with self.lock:
            if not self.load():
                return
            by_key: Dict[str, set] = {}
            for dev_id in ids:
                slim = self._index.pop(dev_id, None)
                if slim is not None:
                    by_key.setdefault(tenant_key(slim), set()).add(dev_id)
            for key, gone in by_key.items():
                self._write_tenant(key, [r for r in self._safe_read(key) if r.get("id") not in gone])

    def replace_tenant(self, slug: str, rows: List[dict]) -> None:
        """Make one tenant's file exactly ``rows`` (a fresh tenant listing)."""
        key = tenant_key({"tenant": {"slug": slug}})
        with self.lock:
            if not self.load():
                return
            for dev_id in [i for i, s in self._index.items() if tenant_key(s) == key]:
                del self._index[dev_id]
            trimmed = [trim_row(r) for r in rows]
            self._write_tenant(key, trimmed)
            self._index.update((r["id"], slim_row(r)) for r in trimmed)

    def _safe_read(self, key: str) -> List[dict]:
        """A tenant's rows, or [] when its file is missing or unreadable."""
        try:
            return self._read_tenant(key)
        except (OSError, ValueError, KeyError, TypeError):
            return []

    # ── daily reconcile schedule ───────────────────────────────────────────

    def due_tenant(self, now: Optional[float] = None) -> Optional[dict]:
        """The cached tenant whose daily reconcile is most overdue, as
        ``{"id", "slug"}`` (slug "" = no tenant), or None. A tenant's first
        reconcile falls at a slug-derived offset after the cache was built,
        which spreads the tenants over the day."""
        now = time.time() if now is None else now
        with self.lock:
            if not self.load():
                return None
            tenants: Dict[str, dict] = {}
            for s in self._index.values():
                t = s.get("tenant") or {}
                tenants.setdefault(tenant_key(s), {"id": t.get("id"), "slug": t.get("slug") or ""})
            built = float(self._meta.get("built", now))
            sched = self._meta.setdefault("reconcile", {})
            due = sorted((sched.get(k) or built + zlib.crc32(k.encode()) % RECONCILE_EVERY_S, k)
                         for k in tenants)
            return tenants[due[0][1]] if due and due[0][0] <= now else None

    def mark_reconciled(self, slug: str, now: Optional[float] = None) -> None:
        """Schedule the tenant's next reconcile a day from ``now``."""
        now = time.time() if now is None else now
        with self.lock:
            if self.load():
                key = tenant_key({"tenant": {"slug": slug}})
                self._meta.setdefault("reconcile", {})[key] = now + RECONCILE_EVERY_S
                self._save_meta()

    # ── follow our own writes ──────────────────────────────────────────────

    def on_response(self, resp, *args, **kwargs):
        """requests response hook: apply a device write (or single-device
        read) the spoke just made to the cache. List reads are ignored; the
        callers that want them stored pass them to ``upsert``. Never raises:
        a cache problem must not fail the NetBox call."""
        try:
            req = resp.request
            url = urlparse(req.url)
            m = _DEVICE_PATH_RE.search(url.path)
            if not m or (req.method == "GET" and (not m.group(1) or url.query)):
                return resp
            dev_id = int(m.group(1)) if m.group(1) else None
            # A device that is gone: drop it.
            if resp.status_code == 404 and dev_id is not None:
                self.remove([dev_id])
            elif not resp.ok:
                pass
            # Deleted (one, or a bulk delete listing ids in the body).
            elif req.method == "DELETE":
                body = json.loads(req.body) if dev_id is None and req.body else []
                self.remove([dev_id] if dev_id is not None
                            else [b.get("id") for b in body if isinstance(b, dict)])
            # Created, updated or read: the response is the new row (or rows).
            else:
                data = resp.json()
                self.upsert(data if isinstance(data, list) else [data])
        except Exception as e:  # noqa: BLE001 - see docstring
            logger.debug("device cache: response hook skipped: %s", e)
        return resp

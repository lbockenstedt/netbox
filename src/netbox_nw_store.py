"""On-disk copy of the network-device inventory the hub pushes to this spoke.

The hub sends a polled device on every poll. Instead of writing each one to
NetBox immediately, the spoke keeps the latest record per device here, notes
which ones changed, and a scheduled loop applies only those to NetBox.
"""
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Tuple

_HEAL_TZ = ZoneInfo("America/Los_Angeles")

# Interface fields that reach NetBox; anything else in a poll (counters etc.) is ignored.
_IFACE_FIELDS = ("name", "mac", "status", "speed", "ip", "vlan")
_DEVICE_FIELDS = ("id", "name", "serial", "mac", "base_mac", "address",
                  "object_type", "model", "firmware", "spec")


def device_key(payload: Dict[str, Any]) -> str:
    """Stable key for a pushed device: its fleet id, else its name."""
    dev = payload.get("device") or {}
    return str(dev.get("id") or dev.get("name") or "").strip()


def signature(payload: Dict[str, Any]) -> str:
    """Hash of everything in the payload that would be written to NetBox."""
    dev = payload.get("device") or {}
    ifaces = sorted(
        [ifc.get(k) for k in _IFACE_FIELDS]
        for ifc in payload.get("interfaces") or [] if isinstance(ifc, dict))
    body = [{k: dev.get(k) for k in _DEVICE_FIELDS},
            payload.get("tenant_slug"), payload.get("defaults"),
            payload.get("source"), ifaces]
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class NwInventoryStore:
    """Latest pushed record per device, persisted as JSON.

    Each entry: ``{"payload", "sig", "applied_sig", "applied_at", "result"}``. A device is
    due when its signature differs from the last one applied to NetBox, or when
    it was last applied before the most recent nightly heal time (3 AM Pacific),
    which heals manual NetBox edits.
    """

    def __init__(self, path: str, heal_hour: int = 3, stale_batch: int = 500,
                 clock=time.time):
        self.path = path
        self.heal_hour = heal_hour
        self._clock = clock
        # Cap on unchanged devices re-applied per flush, so the nightly heal
        # trickles through instead of hitting NetBox all at once.
        self.stale_batch = stale_batch
        # Heals only start for a 03:00 that falls after this process started,
        # so a restart or deploy never triggers an immediate fleet-wide rewrite.
        self._first_cutoff = self._heal_cutoff()
        self.entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _heal_cutoff(self) -> float:
        """Epoch of the most recent heal time (``heal_hour``:00 Pacific)."""
        pt = datetime.fromtimestamp(self._clock(), _HEAL_TZ)
        cut = pt.replace(hour=self.heal_hour, minute=0, second=0, microsecond=0)
        if cut > pt:
            cut -= timedelta(days=1)
        return cut.timestamp()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                self.entries = json.load(f)
        except FileNotFoundError:
            self.entries = {}
        except (OSError, ValueError):
            # A corrupt file only costs a re-sync of every device.
            self.entries = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
        with os.fdopen(fd, "w") as f:
            json.dump(self.entries, f)
        os.replace(tmp, self.path)

    def submit(self, payload: Dict[str, Any]) -> bool:
        """Store the latest record for its device; True when it changed."""
        key = device_key(payload)
        sig = signature(payload)
        entry = self.entries.get(key)
        if entry and entry["sig"] == sig:
            return False
        applied = entry or {"applied_sig": None, "applied_at": 0}
        self.entries[key] = {"payload": payload, "sig": sig,
                             "applied_sig": applied["applied_sig"],
                             "applied_at": applied["applied_at"],
                             "result": applied.get("result")}
        self._save()
        return True

    def last_result(self, key: str) -> Dict[str, Any]:
        """Result of the last NetBox write for ``key`` (site, device_id), if any."""
        return (self.entries.get(key) or {}).get("result") or {}

    def due(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Records that need writing to NetBox: changed, or not yet re-applied since
        the nightly heal time. Changed records (renames, new data) come first so the
        heal of thousands of unchanged devices never delays them."""
        cutoff = self._heal_cutoff()
        healing = cutoff > self._first_cutoff
        changed = [(k, e["payload"]) for k, e in self.entries.items()
                   if e["sig"] != e["applied_sig"]]
        stale = sorted(((k, e) for k, e in self.entries.items()
                        if healing and e["sig"] == e["applied_sig"] and e["applied_at"] < cutoff),
                       key=lambda ke: ke[1]["applied_at"])[:self.stale_batch]
        return changed + [(k, e["payload"]) for k, e in stale]

    def mark_applied(self, key: str, sig: str, result: Dict[str, Any]) -> None:
        """Record that ``sig`` was written to NetBox for ``key`` with ``result``."""
        entry = self.entries.get(key)
        if entry:
            entry["applied_sig"] = sig
            entry["result"] = result
            entry["applied_at"] = time.time()
            self._save()

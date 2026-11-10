"""Write-churn report: what the feeders are changing in NetBox, so the hub
health check can spot flapping fields and runaway write rates."""

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MAX_ROWS = 3000


def analyze_changes(rows: List[dict], window_s: int, capped: bool = False) -> Dict[str, Any]:
    """Summarise object-change rows (newest first, as NetBox returns them)."""
    by_kind: Counter = Counter()
    fields: Counter = Counter()
    seqs: Dict[tuple, List[tuple]] = {}
    stub_creates = 0
    for r in reversed(rows):
        t = str(r.get("changed_object_type") or "?")
        act = str(r.get("action", {}).get("value") if isinstance(r.get("action"), dict)
                  else r.get("action") or "?")
        by_kind[f"{t} {act}"] += 1
        name = str(r.get("object_repr") or "")
        if act == "create" and t == "dcim.device" and name.upper().startswith("DEVICE-"):
            stub_creates += 1
        if act != "update" or t != "dcim.device":
            continue
        pre = ((r.get("prechange_data") or {}).get("custom_fields")) or {}
        post = ((r.get("postchange_data") or {}).get("custom_fields")) or {}
        for k in set(pre) | set(post):
            if pre.get(k) != post.get(k):
                fields[k] += 1
                seq = seqs.setdefault((name, k), [])
                if not seq:
                    seq.append(pre.get(k))
                seq.append(post.get(k))
    flapping: Dict[str, List[str]] = {}
    sample: List[Dict[str, Any]] = []
    for (name, k), seq in seqs.items():
        # A flap returns to a value the field already held in this window.
        strs = [str(v) for v in seq]
        if any(strs[i] in strs[:i] for i in range(2, len(strs))):
            flapping.setdefault(k, []).append(name)
            if len(sample) < 8:
                sample.append({"name": name, "field": k,
                               "values": [str(v) for v in seq[:6]]})
    total = len(rows)
    return {
        "ts": int(time.time()), "window_s": window_s, "rows": total, "capped": capped,
        "writes_per_hour": int(total * 3600 / max(1, window_s)),
        "by_kind": dict(by_kind.most_common(8)),
        "top_fields": [[k, n] for k, n in fields.most_common(6)],
        "stub_creates": stub_creates,
        "flapping": {"objects": len({n for v in flapping.values() for n in v}),
                     "by_field": {k: len(v) for k, v in flapping.items()},
                     "sample": sample},
    }


class ChurnMixin:
    def churn_report(self, window_s: int = 1800) -> Dict[str, Any]:
        """Analyse the last ``window_s`` seconds of NetBox changelog (bounded read)."""
        since = datetime.fromtimestamp(time.time() - window_s, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        try:
            rows = self._api_get_all("/api/core/object-changes/",
                                     {"time_after": since, "limit": 1000}, max_pages=3)
        except Exception as e:  # noqa: BLE001
            return {"status": "ERROR", "message": f"changelog read failed: {e}"}
        return {"status": "SUCCESS", **analyze_changes(rows, window_s, len(rows) >= _MAX_ROWS)}

"""Pure-parse Excel rack-layout detection for the NetBox importer.

No NetBox calls live here — this module turns an .xlsx workbook into a JSON-safe
description of the rack sheets it contains (so the WebUI can render a
column-mapping UI) and, later, turns one sheet + a user column-map into device
rows for ``DcimMixin.import_rack_layout``.

Two sheet shapes are recognized (the HPE DXP lab workbook uses both):

* **one-rack-per-sheet** — a header row (``RU``, ``F/R``, ``Type of device``,
  ``Hostname``, …) followed by one row per device slot. This is the clean,
  high-value shape; the user maps columns → NetBox fields.
* **summary** — a whole-lab elevation grid (``MIP Rack Layout``) where
  rack-blocks are stacked vertically, each introduced by a ``RACK <name>``
  cell with ``RU`` / ``Front`` / ``Rear`` columns. Front/Rear text cells become
  front/rear-face devices named by their text (no model/serial/IP).

openpyxl is imported lazily so a spoke without the dep degrades to a clear
ERROR rather than crashing on import.
"""
# Postpone evaluation of type hints (keeps annotations cheap and forward-safe).
from __future__ import annotations

# Standard-library logging; shares the "NetboxEngine" logger with the engine.
import logging
# re normalises whitespace and finds rack names / U heights in cell text.
import re
# Type hints.
from typing import Any, Dict, List, Optional

# Same logger name as the engine so all NetBox messages appear together.
logger = logging.getLogger("NetboxEngine")

# ─── column-label → importer-field synonyms ─────────────────────────────────
# Each field lists header labels (lowercased, whitespace-collapsed) that map to
# it. The first matching label wins. Order matters: more-specific fields first.
_FIELD_SYNONYMS: Dict[str, List[str]] = {
    "position":    ["ru", "rack unit", "u", "rack u"],
    "face":        ["f/r", "fr", "front/rear", "f/r "],
    "device_type": ["type of device", "device type", "model", "type"],
    "name":        ["hostname", "name", "host name", "device name"],
    "serial":      ["serial number", "serial", "serial no", "s/n"],
    "asset_tag":   ["part number", "asset tag", "part no", "p/n"],
    "mac":         ["mac address", "mac", "mac addr"],
    "mgmt_ip":     ["mgmt ip", "management ip", "mgmt ip address", "local ip",
                    "ip address", "ip"],
    "role":        ["function", "role"],
    "status":      ["status"],
    "description": ["description", "desc", "comments"],
}

# The fixed set of target fields the WebUI offers in its mapping <select>.
TARGET_FIELDS: List[str] = (
    ["name", "device_type", "serial", "asset_tag", "position", "face",
     "mgmt_ip", "mac", "role", "status", "description", "ignore"]
)

_PREVIEW_ROWS = 20  # cap on preview rows returned to the WebUI per sheet


def _norm_label(v: Any) -> str:
    """Lowercase + collapse whitespace + strip, for header matching."""
    # Collapse runs of whitespace to one space after lower-casing.
    return re.sub(r"\s+", " ", str(v or "").strip().lower())


def _cell_str(v: Any) -> str:
    """Stringify a cell value, stripping whitespace; '' for None/blank."""
    # Empty cell.
    if v is None:
        return ""
    # Convert to text and trim.
    s = str(v).strip()
    # openpyxl can yield trailing '\t' / nbsp in messy sheets; normalize.
    s = s.replace("\t", " ").replace(" ", " ")
    # Collapse any remaining whitespace runs.
    return re.sub(r"\s+", " ", s).strip()


def _guess_field(label: str) -> Optional[str]:
    """Return the importer field a header label maps to, or None."""
    # Normalise the header text; blank headers map to nothing.
    n = _norm_label(label)
    if not n:
        return None
    # Exact synonym match first.
    for field, syns in _FIELD_SYNONYMS.items():
        if n in syns:
            return field
    # substring fallback: e.g. "MGMT IP (OOB)" contains "mgmt ip"
    for field, syns in _FIELD_SYNONYMS.items():
        for s in syns:
            if s and s in n:
                return field
    return None


def load_workbook_from_bytes(data: bytes):
    """Parse .xlsx bytes into an openpyxl Workbook (lazy import)."""
    # io wraps the bytes as a file-like object for openpyxl.
    import io
    # openpyxl is optional: give a clear install hint if it is missing.
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "openpyxl is required to parse .xlsx imports; install it on the "
            "netbox spoke (`pip install openpyxl`) and restart.") from e
    # data_only=True returns cached formula results; read_only streams large sheets.
    return openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)


def _is_header_row(cells: List[str]) -> bool:
    """A one-rack-per-sheet header row contains 'RU' AND a device/hostname col."""
    # Normalised set of the row's cell labels.
    labels = {_norm_label(c) for c in cells}
    # No RU column: not a rack header.
    if "ru" not in labels:
        return False
    # Also need a device-identifying column.
    return any(k in labels for k in
               ("hostname", "type of device", "device type", "model", "name"))


def _uheight_from_metadata(rows: List[List[Any]]) -> Optional[int]:
    """Scan for a trailing rack-metadata row like '4-Post 51U Rack' → 51."""
    # The metadata row sits near the bottom, so only scan the last 12 rows.
    for cells in rows[-12:]:
        for c in cells:
            s = _cell_str(c)
            # Look for "<number>U" in a cell that also mentions "rack".
            m = re.search(r"(\d+)\s*u\b", s, re.IGNORECASE)
            if m and "rack" in s.lower():
                # Return the number of units.
                try:
                    return int(m.group(1))
                except ValueError:
                    # Not a valid integer: keep looking.
                    continue
    # No metadata row found.
    return None


def _detect_one_rack_sheet(ws) -> Optional[Dict[str, Any]]:
    """Detect a one-rack-per-sheet shape. Returns a rack_sheet dict or None."""
    # Load all rows as plain value lists.
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    # Find the first header row.
    header_idx = None
    for i, cells in enumerate(rows):
        if _is_header_row([_cell_str(c) for c in cells]):
            header_idx = i
            break
    # No header: not a one-rack sheet.
    if header_idx is None:
        return None
    # Header cell texts (cleaned).
    header = [(_cell_str(c) if j < len(rows[header_idx]) else "")
              for j, c in enumerate(rows[header_idx])]
    # Build label → column index (first occurrence wins).
    label_to_idx: Dict[str, int] = {}
    for idx, label in enumerate(header):
        lbl = _cell_str(label)
        if lbl and lbl not in label_to_idx:
            label_to_idx[lbl] = idx
    # Guess a column_map: source-label → target-field.
    column_map: Dict[str, str] = {}
    for label, idx in label_to_idx.items():
        # Only keep labels that map to a known field.
        f = _guess_field(label)
        if f:
            column_map[label] = f
    # Resolve the RU column index for U-height + device-row detection.
    # Which column holds the RU (rack unit) number.
    ru_idx = None
    for label, idx in label_to_idx.items():
        if _guess_field(label) == "position":
            ru_idx = idx
            break
    # Device rows: below header, with a numeric RU and at least one non-empty
    # device-ish cell (hostname / type / function). Nameless rows are still
    # surfaced (the importer skips rows lacking name+serial, but the user sees
    # them in the preview so they know to fill names or map a column).
    # Device rows found, and the highest RU seen.
    device_rows: List[List[Any]] = []
    max_ru = 0
    # Columns that indicate a device is present in a row.
    name_idx = next((i for lbl, i in label_to_idx.items()
                     if _guess_field(lbl) == "name"), None)
    type_idx = next((i for lbl, i in label_to_idx.items()
                     if _guess_field(lbl) == "device_type"), None)
    role_idx = next((i for lbl, i in label_to_idx.items()
                     if _guess_field(lbl) == "role"), None)
    # Walk every row below the header.
    for cells in rows[header_idx + 1:]:
        # RU value of this row (None when the column is missing).
        ru_val = cells[ru_idx] if ru_idx is not None and ru_idx < len(cells) else None
        # Integer RU, or None when blank or not a number.
        try:
            ru = int(ru_val) if ru_val is not None and str(ru_val).strip() != "" else None
        except (ValueError, TypeError):
            # Text RU (e.g. "N/A"): treat as no RU.
            ru = None
        # Track the highest occupied RU.
        if ru is not None and ru > max_ru:
            max_ru = ru
        # stop scanning at the trailing metadata row (RU == 'N/A')
        if _norm_label(ru_val) in ("n/a", "na", ""):
            # could be a blank separator or the metadata row; skip but continue
            continue
        # Does any device-identifying column have text?
        has_device = any(
            _cell_str(cells[i]) for i in (name_idx, type_idx, role_idx)
            if i is not None and i < len(cells))
        # Keep rows with an RU and some device information.
        if ru is not None and has_device:
            device_rows.append([_cell_str(c) for c in cells])
    # Rack U-height = the rack's total U count. The trailing metadata row
    # ('4-Post 51U Rack') gives the true size; the max occupied RU is a lower
    # bound. Take the larger so a sparsely-filled 51U rack still reports 51.
    meta = _uheight_from_metadata(rows) or 0
    u_height = max(max_ru, meta) or max_ru or 0
    # Descriptor the WebUI uses to show the sheet and its suggested column map.
    return {
        "shape": "one-rack",
        "sheet": ws.title,
        "rack_name": ws.title,
        "u_height": u_height,
        "columns": [h for h in header if h],
        "column_map": column_map,
        "preview_rows": device_rows[:_PREVIEW_ROWS],
        "device_count": len(device_rows),
    }


# Matches a "RACK <name>" cell that starts a rack block in a summary sheet.
_RACK_BLOCK_RE = re.compile(r"^\s*rack\s+(.+)$", re.IGNORECASE)


def _detect_summary_sheets(ws) -> List[Dict[str, Any]]:
    """Detect rack-blocks in a whole-lab summary sheet. Returns a list of
    rack_sheet dicts (one per block). Best-effort: handles vertically-stacked
    blocks (``RACK <name>`` … RU/Front/Rear … rows …). Side-by-side rack groups
    in one row band are not handled in v1 — the user can deselect these."""
    # Load all rows as plain value lists.
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    sheets: List[Dict[str, Any]] = []
    n = len(rows)
    # Look for "RACK <name>" cells anywhere in the sheet.
    for i, cells in enumerate(rows):
        for c in cells:
            m = _RACK_BLOCK_RE.match(_cell_str(c))
            # Not a rack header cell.
            if not m:
                continue
            # Rack name follows the word "RACK".
            rack_name = m.group(1).strip()
            # nearest 'RU' header at/after this row
            block = _parse_summary_block(rows, i, rack_name, ws.title)
            # Keep blocks that produced device rows.
            if block:
                sheets.append(block)
            # Only one rack block per row band (side-by-side blocks are not handled).
            break
    # All rack blocks found in this sheet.
    return sheets


def _parse_summary_block(rows: List[List[Any]], start: int,
                         rack_name: str, sheet: str) -> Optional[Dict[str, Any]]:
    """From a 'RACK <name>' row, find the RU/Front/Rear columns and read device
    rows until the next rack block or a long blank band."""
    # Number of rows in the sheet.
    n = len(rows)
    # find the RU header row within the next ~6 rows
    ru_col = front_col = rear_col = None
    # Row index of the RU header.
    header_row = None
    for j in range(start, min(start + 8, n)):
        cells = rows[j]
        # Normalised labels of this row with their column indexes.
        labels = [(_norm_label(c), idx) for idx, c in enumerate(cells)]
        # Column whose label is exactly "RU".
        ru = next((idx for lbl, idx in labels if lbl == "ru"), None)
        if ru is not None:
            ru_col = ru
            header_row = j
            # Front/Rear headers usually sit in the row after RU, same/next cols.
            # Look in this row and the next for 'front'/'rear'.
            for k in (j, j + 1):
                # Past the end of the sheet.
                if k >= n:
                    break
                # Labels of the row being checked.
                klabels = [(_norm_label(c), idx) for idx, c in enumerate(rows[k])]
                if front_col is None:
                    front_col = next((idx for lbl, idx in klabels if lbl == "front"), None)
                if rear_col is None:
                    rear_col = next((idx for lbl, idx in klabels if lbl == "rear"), None)
            break
    # No RU header near the rack title: not a usable block.
    if ru_col is None:
        return None
    # No explicit Front/Rear headers: assume the two columns right after RU.
    if front_col is None:
        front_col = ru_col + 1
    if rear_col is None:
        rear_col = ru_col + 2
    # Device rows, highest RU, and count of consecutive blank rows.
    device_rows: List[List[Any]] = []
    max_ru = 0
    blank_run = 0
    # Read rows below the RU header.
    for cells in rows[header_row + 1:]:
        # RU value of this row.
        ru_val = cells[ru_col] if ru_col < len(cells) else None
        # Integer RU, or None when blank or not a number.
        try:
            ru = int(ru_val) if ru_val is not None and str(ru_val).strip() != "" else None
        except (ValueError, TypeError):
            # Not a number.
            ru = None
        # Stop at the next rack block.
        if any(_RACK_BLOCK_RE.match(_cell_str(c)) for c in cells):
            break
        # Entirely blank row: four in a row ends the block.
        if ru is None and not any(_cell_str(c) for c in cells):
            blank_run += 1
            if blank_run >= 4:
                break
            continue
        # Non-blank row resets the blank counter; rows without an RU are skipped.
        blank_run = 0
        if ru is None:
            continue
        # Track the highest RU.
        if ru > max_ru:
            max_ru = ru
        # Front and rear text of this RU.
        front = _cell_str(cells[front_col]) if front_col < len(cells) else ""
        rear = _cell_str(cells[rear_col]) if rear_col < len(cells) else ""
        # Keep RUs that hold something.
        if front or rear:
            device_rows.append([str(ru), front, rear])
    # Block with no devices: ignore it.
    if not device_rows:
        return None
    # device_count = front+rear device slots (a RU with both front and rear
    # text yields two devices), not raw rows.
    n_devices = sum(1 for r in device_rows if r[1]) + sum(1 for r in device_rows if r[2])
    # Descriptor for the WebUI (same shape as the one-rack descriptor).
    return {
        "shape": "summary",
        "sheet": sheet,
        "rack_name": rack_name,
        "u_height": max_ru or 0,
        "columns": ["RU", "Front", "Rear"],
        "column_map": {"Front": "name", "Rear": "name"},  # informational
        "preview_rows": device_rows[:_PREVIEW_ROWS],
        "device_count": n_devices,
    }


def detect_rack_sheets(wb) -> List[Dict[str, Any]]:
    """Scan every worksheet and return detected rack-sheet descriptors.

    A sheet may yield zero, one (one-rack-per-sheet), or many (summary blocks)
    descriptors. Order preserves the workbook's sheet order."""
    # Descriptors for every detected rack.
    out: List[Dict[str, Any]] = []
    # Check each worksheet in order.
    for ws in wb.worksheets:
        # Skip unnamed sheets and the workbook's table-of-contents/calculation sheets.
        title = (ws.title or "").strip()
        if not title or title.lower() in ("toc", "calcs"):
            continue
        # Try one-rack-per-sheet first; if not, try summary blocks.
        one = _detect_one_rack_sheet(ws)
        if one:
            out.append(one)
            continue
        out.extend(_detect_summary_sheets(ws))
    return out


# ─── commit-time parse: one sheet + column_map → device field dicts ─────────

def parse_one_rack_sheet(ws, column_map: Dict[str, str]) -> Dict[str, Any]:
    """Re-read a one-rack-per-sheet worksheet and apply the user's column_map
    (source-label → target-field) to produce per-device field dicts.

    Returns ``{rack_name, u_height, devices: [{field: value, ...}]}``. Rows
    with no name AND no serial are omitted (returned as ``skipped`` count)."""
    # Load all rows and find the header again (the workbook was re-uploaded).
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    header_idx = None
    for i, cells in enumerate(rows):
        if _is_header_row([_cell_str(c) for c in cells]):
            header_idx = i
            break
    # Header vanished since detection: report an empty result with an error.
    if header_idx is None:
        return {"rack_name": ws.title, "u_height": 0, "devices": [], "skipped": 0,
                "error": "header row not found on re-parse"}
    # Header label -> column index (first occurrence wins).
    header = [_cell_str(c) for c in rows[header_idx]]
    label_to_idx: Dict[str, int] = {}
    for idx, label in enumerate(header):
        if label and label not in label_to_idx:
            label_to_idx[label] = idx
    # Invert the user map → field → column index (last label wins per field).
    field_to_idx: Dict[str, int] = {}
    # Columns mapped to "ignore" (or nothing) are skipped.
    for src_label, field in (column_map or {}).items():
        if field in (None, "", "ignore"):
            continue
        # Exact label match first.
        idx = label_to_idx.get(src_label)
        if idx is None:
            # try a normalized match (label may have been trimmed in the UI)
            idx = next((i for lbl, i in label_to_idx.items()
                        if _norm_label(lbl) == _norm_label(src_label)), None)
        # Remember the column for this field.
        if idx is not None:
            field_to_idx[field] = idx
    # RU column, output devices, skipped-slot counter and highest RU.
    ru_idx = field_to_idx.get("position")
    devices: List[Dict[str, Any]] = []
    skipped = 0
    max_ru = 0
    # Walk every row below the header.
    for cells in rows[header_idx + 1:]:
        # RU value of this row.
        ru_val = cells[ru_idx] if ru_idx is not None and ru_idx < len(cells) else None
        # Integer RU, or None when blank or not a number.
        try:
            ru = int(ru_val) if ru_val is not None and str(ru_val).strip() != "" else None
        except (ValueError, TypeError):
            # Not a number.
            ru = None
        # Trailing metadata row: skip.
        if _norm_label(ru_val) in ("n/a", "na"):
            continue
        # Track the highest RU.
        if ru is not None and ru > max_ru:
            max_ru = ru
        # Build the device's field dict from the mapped columns (non-empty cells only).
        dev: Dict[str, Any] = {}
        for field, idx in field_to_idx.items():
            if idx is not None and idx < len(cells):
                v = _cell_str(cells[idx])
                if v:
                    dev[field] = v
        # Require a RU and at least a name or serial to import a device.
        if ru is None or not (dev.get("name") or dev.get("serial")):
            # Count occupied but unnamed slots so the user can see them.
            if ru is not None and (dev.get("device_type") or dev.get("role")):
                skipped += 1  # an occupied slot we can't name — surfaced
            continue
        # Record the RU as the device position and keep it.
        dev["position"] = ru
        devices.append(dev)
    # Rack info plus the importable devices.
    return {"rack_name": ws.title, "u_height": max_ru or 0,
            "devices": devices, "skipped": skipped}


def parse_summary_block_by_name(wb, rack_name: str) -> Optional[Dict[str, Any]]:
    """Re-read a summary sheet and extract the front/rear device rows for the
    named rack block. Returns ``{rack_name, u_height, devices}`` or None."""
    # Search every sheet for the named rack block.
    for ws in wb.worksheets:
        # Load all rows as plain value lists.
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        for i, cells in enumerate(rows):
            for c in cells:
                m = _RACK_BLOCK_RE.match(_cell_str(c))
                # Found the requested rack: parse its block.
                if m and m.group(1).strip() == rack_name:
                    block = _parse_summary_block(rows, i, rack_name, ws.title)
                    # Block has no devices.
                    if not block:
                        return None
                    # Turn the block rows into device dicts.
                    return _summary_rows_to_devices(block, rack_name)
    # Rack not found in any sheet.
    return None


def _summary_rows_to_devices(block: Dict[str, Any], rack_name: str) -> Dict[str, Any]:
    """Turn a summary block's [RU, Front, Rear] rows into front+rear devices."""
    # Output device dicts.
    devices: List[Dict[str, Any]] = []
    # NOTE: reads the block's preview_rows (capped at _PREVIEW_ROWS rows).
    for row in block["preview_rows"]:
        # Pad short rows so unpacking always gets three values.
        ru_s, front, rear = (row + ["", "", ""])[:3]
        # RU must be an integer; otherwise skip the row.
        try:
            ru = int(ru_s)
        except (ValueError, TypeError):
            # Not a number.
            continue
        # Front-face device.
        if front:
            devices.append({"name": front, "position": ru, "face": "front"})
        # Rear-face device.
        if rear:
            devices.append({"name": rear, "position": ru, "face": "rear"})
    # Rack info plus the devices.
    return {"rack_name": rack_name, "u_height": block.get("u_height", 0),
            "devices": devices, "skipped": 0}
"""Tests for the single-source custom-field schema: custom_fields_spec +
NetboxEngine._ensure_custom_fields (the engine self-heal + the WebUI "Apply
schema changes" button path) + the NETBOX_PROVISION_CUSTOM_FIELDS contract.

The spec module (custom_fields_spec.CUSTOM_FIELDS_SPEC) is the ONE list shared
by install.sh (REST provisioner), the engine self-heal, and the button — so a
fresh install, an update, and a manual apply produce an identical schema. These
tests pin that contract: the engine reads the spec, _ensure_custom_fields is
idempotent + re-runnable (never errors when fields already exist), force=True
bypasses the per-process cache, and the report shape matches what the hub
route relays to the WebUI.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from custom_fields_spec import CUSTOM_FIELDS_SPEC  # noqa: E402
from netbox_engine import NetboxEngine  # noqa: E402


class _Cf:
    """pynetbox custom-field stand-in: settable content_types + save."""
    def __init__(self, name, content_types=None):
        self.name = name
        self.id = hash(name) & 0xFFFF
        self.content_types = list(content_types or [])
        self.save = MagicMock()


def _engine_with_existing(existing_by_name):
    """Engine whose nb.extras.custom_fields.all() returns the given fields."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.extras.custom_fields.all.return_value = list(existing_by_name.values())
    eng.nb.extras.custom_fields.create = MagicMock(side_effect=lambda **kw: _Cf(kw["name"]))
    return eng


def test_engine_required_custom_fields_is_the_spec():
    # The engine class attribute MUST be the spec list itself (same object) —
    # not a copy — so there is exactly one source of truth.
    assert NetboxEngine._REQUIRED_CUSTOM_FIELDS is CUSTOM_FIELDS_SPEC


def test_spec_has_expected_fields_and_no_duplicate_name_ct_pairs():
    names = {n for n, *_ in CUSTOM_FIELDS_SPEC}
    for must in ("proxmox_unique_id", "proxmox_vmid", "proxmox_node", "proxmox_type",
                 "proxmox_labels", "discovered_from", "mac_address", "switch_ip",
                 "switch_port", "last_seen", "decommissioned_at", "vmid_start",
                 "vmid_end"):
        assert must in names, f"{must} missing from spec"
    # A name may repeat across content types (mac_address, last_seen,
    # decommissioned_at) but never the same (name, content_type) twice.
    seen = set()
    for name, _ftype, _label, ct in CUSTOM_FIELDS_SPEC:
        key = (name, ct)
        assert key not in seen, f"duplicate (name, content_type): {key}"
        seen.add(key)
    # vmid_start/vmid_end are integers; everything else is text.
    types = {n: t for n, t, _l, _ct in CUSTOM_FIELDS_SPEC}
    assert types["vmid_start"] == "integer"
    assert types["vmid_end"] == "integer"
    assert types["proxmox_labels"] == "text"


def test_ensure_custom_fields_idempotent_when_all_present_and_attached():
    # Every spec field already exists AND is attached to its content type → the
    # run is a no-op that reports SUCCESS, present=total, created=0, attached=0,
    # and sets the per-process cache.
    by_name = {}
    for name, _ftype, _label, ct in CUSTOM_FIELDS_SPEC:
        if name not in by_name:
            by_name[name] = _Cf(name, content_types=[ct])
        elif ct not in by_name[name].content_types:
            by_name[name].content_types.append(ct)
    eng = _engine_with_existing(by_name)

    report = eng._ensure_custom_fields(force=True)

    assert report["status"] == "SUCCESS", report
    assert report["created"] == 0
    assert report["attached"] == 0
    assert report["already_attached"] == len(CUSTOM_FIELDS_SPEC)
    assert report["warnings"] == []
    eng.nb.extras.custom_fields.create.assert_not_called()
    assert getattr(eng, "_cf_ensured", False) is True


def test_ensure_custom_fields_creates_missing_and_attaches_unattached():
    # Half the fields don't exist; the other half exist but are unattached to
    # the content type this entry needs → create the missing, attach the
    # unattached, never error, report SUCCESS.
    by_name = {}
    for i, (name, _ftype, _label, ct) in enumerate(CUSTOM_FIELDS_SPEC):
        if i % 2 == 0:
            # exists but NOT attached to ct (empty content_types)
            by_name[name] = _Cf(name, content_types=[])
    eng = _engine_with_existing(by_name)

    report = eng._ensure_custom_fields(force=True)

    assert report["status"] == "SUCCESS", report
    assert report["created"] > 0            # the missing half
    assert report["attached"] > 0           # the unattached half repaired
    assert report["warnings"] == []
    # An existing-but-unattached field's content_types got the ct added + saved.
    repaired = next(iter(by_name.values()))
    repaired.save.assert_called()


def test_ensure_custom_fields_force_bypasses_cache():
    # A cached clean run (force=False) returns immediately with an
    # already-attached report and does NOT hit the API. force=True re-runs the
    # full verify/attach pass even with the cache set.
    eng = _engine_with_existing({})
    eng._cf_ensured = True  # pretend a prior clean run cached

    cached = eng._ensure_custom_fields(force=False)
    assert cached["already_attached"] == len(CUSTOM_FIELDS_SPEC)
    eng.nb.extras.custom_fields.all.assert_not_called()  # cache short-circuit

    forced = eng._ensure_custom_fields(force=True)
    eng.nb.extras.custom_fields.all.assert_called_once()  # cache bypassed
    # Nothing pre-existed → everything created, status SUCCESS.
    assert forced["status"] == "SUCCESS", forced
    assert forced["created"] == len(CUSTOM_FIELDS_SPEC)


def test_ensure_custom_fields_partial_on_create_failure():
    # A create failure (e.g. a restricted token) must NOT abort the whole pass:
    # the failed field is recorded as a warning, status=PARTIAL, the cache is
    # left unset so the next sync retries (self-healing).
    by_name = {}
    for name, _ftype, _label, ct in CUSTOM_FIELDS_SPEC:
        by_name.setdefault(name, _Cf(name, content_types=[ct]))
        if ct not in by_name[name].content_types:
            by_name[name].content_types.append(ct)
    eng = _engine_with_existing(by_name)
    # All present+attached, but force a warning via an attach save failure on
    # one field to exercise the PARTIAL path without re-implementing create.
    first_name = next(iter(by_name))
    by_name[first_name].content_types = []  # force an attach attempt
    by_name[first_name].save.side_effect = Exception("permission denied")

    report = eng._ensure_custom_fields(force=True)

    assert report["status"] == "PARTIAL", report
    assert report["warnings"], "expected at least one warning"
    assert getattr(eng, "_cf_ensured", False) is False  # cache unset → retries
"""Tests for NetboxEngine.seed_catalog — the WebUI "Seed catalog" button path
(Setup → Module Management → Seed catalog → NETBOX_SEED_CATALOG → engine).

Pins the contract: idempotent UPSERT (re-runs never error on an existing type),
add-missing templates (never delete/re-type existing), per-model error
isolation, manufacturer get-or-create + caching, and the summary return shape.
Uses a pynetbox stand-in (no live NetBox)."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402


class _Rec:
    """pynetbox Record stand-in: arbitrary attrs + a save mock + an auto id."""
    _ids = {}

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.save = MagicMock()
        cls = type(self)
        cls._ids[cls] = cls._ids.get(cls, 0) + 1
        if not getattr(self, "id", None):
            self.id = cls._ids[cls]
        # tag which device_type this template belongs to (for filter())
        self._dt = kw.get("device_type")


class _FakeEp:
    """pynetbox endpoint stand-in: get(slug=) / create(**kw) / filter(device_type=)."""

    def __init__(self, existing=None, create_fails_for=None):
        self.existing = list(existing or [])
        self.created = []
        self.create_fails_for = set(create_fails_for or [])

    def get(self, slug=None, **kw):
        for r in self.existing:
            if getattr(r, "slug", None) == slug:
                return r
        return None

    def filter(self, device_type=None, **kw):
        return [r for r in self.existing if getattr(r, "_dt", None) == device_type]

    def create(self, **kw):
        key = kw.get("name") or kw.get("slug") or kw.get("model")
        if key in self.create_fails_for:
            raise RuntimeError(f"boom: {key}")
        r = _Rec(**kw)
        self.created.append(kw)
        self.existing.append(r)
        return r


def _engine(catalog):
    """Engine wired with fake nb endpoints and a pre-set (cached) catalog so
    seed_catalog() doesn't touch the bundled JSON file."""
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    eng.nb.dcim.manufacturers = _FakeEp()
    eng.nb.dcim.device_types = _FakeEp()
    eng.nb.dcim.interface_templates = _FakeEp()
    eng.nb.dcim.console_port_templates = _FakeEp()
    eng.nb.dcim.power_port_templates = _FakeEp()
    eng._seed_catalog_cache = catalog
    return eng


def _catalog(*types):
    return {"manufacturers": ["Aruba", "HPE", "Juniper"], "device_types": list(types)}


def _model(slug="2930f-24g", mfr="Aruba", u_height=1, full=True,
           ports=None, mgmt=True, console=True, power=True):
    return {
        "manufacturer": mfr,
        "model": slug.upper().replace("-", " "),
        "slug": slug,
        "u_height": u_height,
        "is_full_depth": full,
        "comments": f"{slug} test",
        "ports": ports or [{"prefix": "", "count": 24, "start": 1, "type": "1000base-t"},
                           {"prefix": "", "count": 4, "start": 25, "type": "10gbase-x-sfplus"}],
        "mgmt": {"name": "mgmt", "type": "1000base-t"} if mgmt else None,
        "console": {"name": "Console", "type": "rj-45"} if console else None,
        "power": [{"name": "PSU1", "type": "iec-60320-c14"},
                  {"name": "PSU2", "type": "iec-60320-c14"}] if power else None,
    }


# ─── contract tests ────────────────────────────────────────────────────────

def test_new_device_type_creates_all_templates():
    eng = _engine(_catalog(_model()))
    r = eng.seed_catalog()
    assert r["status"] == "SUCCESS"
    assert r["manufacturers_created"] == 1
    assert r["device_types_created"] == 1
    assert r["device_types_updated"] == 0
    # 24 access + 4 uplink + 1 mgmt + 1 console + 2 power = 32
    assert r["templates_added"] == 32
    assert r["errors"] == []
    iface_names = {c["name"] for c in eng.nb.dcim.interface_templates.created}
    assert iface_names == {str(n) for n in range(1, 25)} | {"25", "26", "27", "28", "mgmt"}
    # mgmt interface must be mgmt_only=True
    mgmt = [c for c in eng.nb.dcim.interface_templates.created if c["name"] == "mgmt"]
    assert mgmt and mgmt[0]["mgmt_only"] is True
    assert {c["name"] for c in eng.nb.dcim.console_port_templates.created} == {"Console"}
    assert {c["name"] for c in eng.nb.dcim.power_port_templates.created} == {"PSU1", "PSU2"}
    # device type created with the expected scalars + manufacturer id
    dtc = eng.nb.dcim.device_types.created[0]
    assert dtc["slug"] == "2930f-24g"
    assert dtc["u_height"] == 1 and dtc["is_full_depth"] is True


def test_existing_device_type_no_dup_templates():
    eng = _engine(_catalog(_model()))
    # Pre-seed: the device type already exists, and ALL its templates exist.
    mfr = eng.nb.dcim.manufacturers.create(name="Aruba", slug="aruba")
    dt = eng.nb.dcim.device_types.create(
        model="X", slug="2930f-24g", manufacturer=mfr.id, u_height=1,
        is_full_depth=True, comments="2930f-24g test")
    # populate every template name so add-missing creates nothing
    names = [str(n) for n in range(1, 25)] + ["25", "26", "27", "28", "mgmt"]
    for nm in names:
        eng.nb.dcim.interface_templates.existing.append(_Rec(name=nm, device_type=dt.id))
    eng.nb.dcim.console_port_templates.existing.append(_Rec(name="Console", device_type=dt.id))
    for nm in ("PSU1", "PSU2"):
        eng.nb.dcim.power_port_templates.existing.append(_Rec(name=nm, device_type=dt.id))

    r = eng.seed_catalog()
    assert r["status"] == "SUCCESS"
    assert r["device_types_created"] == 0
    assert r["device_types_updated"] == 0   # scalars match → no update
    assert r["templates_added"] == 0        # all templates already present
    assert r["manufacturers_created"] == 0  # manufacturer already existed
    assert eng.nb.dcim.interface_templates.created == []


def test_existing_device_type_scalar_update_marks_updated_and_saves():
    eng = _engine(_catalog(_model(u_height=2)))
    mfr = eng.nb.dcim.manufacturers.create(name="Aruba", slug="aruba")
    dt = eng.nb.dcim.device_types.create(
        model="X", slug="2930f-24g", manufacturer=mfr.id, u_height=1,
        is_full_depth=True, comments="2930f-24g test")
    dt.save.reset_mock()
    r = eng.seed_catalog()
    assert r["device_types_updated"] == 1
    assert r["device_types_created"] == 0
    dt.save.assert_called_once()


def test_idempotent_re_run_creates_nothing():
    eng = _engine(_catalog(_model()))
    eng.seed_catalog()
    # second run on the same fakes: everything already exists
    r2 = eng.seed_catalog()
    assert r2["status"] == "SUCCESS"
    assert r2["manufacturers_created"] == 0
    assert r2["device_types_created"] == 0
    assert r2["device_types_updated"] == 0
    assert r2["templates_added"] == 0
    assert r2["errors"] == []


def test_per_model_error_isolation():
    eng = _engine(_catalog(_model(slug="good-24g"), _model(slug="bad-24g")))
    # The second model's device_types.create raises (e.g. NetBox 400).
    eng.nb.dcim.device_types.create_fails_for.add("bad-24g")
    r = eng.seed_catalog()
    assert r["status"] == "SUCCESS"
    assert r["device_types_created"] == 1          # the good one still created
    assert len(r["errors"]) == 1
    assert "bad-24g" in r["errors"][0]


def test_manufacturer_get_or_create_cached_once():
    eng = _engine(_catalog(_model(slug="a-24g", mfr="Aruba"),
                           _model(slug="b-24g", mfr="Aruba")))
    r = eng.seed_catalog()
    assert r["manufacturers_created"] == 1
    # Aruba created exactly once even though two models use it
    assert len(eng.nb.dcim.manufacturers.created) == 1
    assert eng.nb.dcim.manufacturers.created[0]["slug"] == "aruba"


def test_return_shape():
    eng = _engine(_catalog(_model()))
    r = eng.seed_catalog()
    for k in ("status", "manufacturers_created", "device_types_created",
              "device_types_updated", "templates_added", "errors"):
        assert k in r, f"missing key {k}"
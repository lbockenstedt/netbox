"""In-process fake NetBox REST API for exercising the REAL pynetbox client.

Why this exists
---------------
``sync_devices`` correctness depends on pynetbox behaviour that MagicMock
fakes cannot reproduce: ``Record.save()`` diffing against the init snapshot,
custom_fields being sent as a whole dict (and NetBox MERGING it on PATCH),
``save()`` re-parsing the PATCH response so the Record stays live, ``get()``
returning ``None`` on a 404, pagination via ``next`` links. This module mounts
a ``requests`` transport adapter on the engine's ``http_session`` so every
pynetbox call and every ``_api_get``/``_api_get_all`` call hits a small
in-memory NetBox instead of the network.

Modelled NetBox semantics (the ones sync_devices relies on):

* ``custom_fields`` on PATCH are MERGED into the stored values (NetBox
  ``CustomFieldsDataField`` partial-update behaviour) — omitted keys keep their
  value.
* device ``(name, site, tenant)`` uniqueness → 400 on create/rename collision;
  a create without ``site`` → 400 ``{"site": ["This field is required."]}``.
* IP addresses are unique per (VRF, host) → 400 "Duplicate IP address found in
  global table".
* deleting a device cascades to its interfaces, and deleting an interface
  cascades to the IPs assigned to it (NetBox ``GenericRelation``).
* ``primary_ip4`` must reference an IP assigned to one of the device's
  interfaces.
* list endpoints paginate (``page_size``) with ``next`` links; ordering is
  (name, id) for devices and id for everything else.
* ``?address=`` matches host/prefix exactly when a mask is given, else any
  mask; ``?id=`` may repeat.

Every request is appended to ``log`` as ``(METHOD, path, sorted query, body)``
so a differential test can compare two implementations call-for-call, and
``fail`` lets a test make a specific object's write fail with a 500.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from requests.adapters import BaseAdapter

BASE = "http://nb.test"

# app/collection pairs the fake understands
_COLLECTIONS = {
    ("tenancy", "tenants"), ("dcim", "sites"), ("dcim", "device-roles"),
    ("dcim", "device-types"), ("dcim", "manufacturers"), ("dcim", "devices"),
    ("dcim", "interfaces"), ("ipam", "ip-addresses"), ("ipam", "prefixes"),
    ("ipam", "vrfs"), ("extras", "journal-entries"), ("extras", "custom-fields"),
}


def _host(addr: str) -> str:
    return str(addr or "").split("/")[0].strip()


class FakeNetBox(BaseAdapter):
    """requests adapter backed by an in-memory NetBox database."""

    def __init__(self, page_size: int = 50):
        super().__init__()
        self.page_size = page_size
        self.db: Dict[str, Dict[int, dict]] = {c: {} for _a, c in _COLLECTIONS}
        self._next_id = 1000
        self.log: List[Tuple[str, str, str, Any]] = []
        # {(METHOD, collection, key)} -> respond 500. key is an object id; for
        # POST the ``slug``/``name``/``address`` being created; for list GETs
        # "list" (any) or "list-global" (an unfiltered device listing).
        self.fail: set = set()

    # ── seeding helpers ────────────────────────────────────────────────────
    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def add(self, coll: str, obj: dict) -> dict:
        obj = dict(obj)
        obj.setdefault("id", self.new_id())
        self.db[coll][obj["id"]] = obj
        return obj

    # ── transport ──────────────────────────────────────────────────────────
    def close(self):  # pragma: no cover - adapter API
        pass

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        query = parse_qs(url.query, keep_blank_values=True)
        body = None
        if request.body:
            raw = request.body.decode() if isinstance(request.body, bytes) else request.body
            body = json.loads(raw)
        self.log.append((request.method, url.path,
                         urlencode(sorted((k, v) for k, vs in query.items() for v in vs)),
                         body))
        try:
            status, payload = self._route(request.method, url.path, query, body)
        except _Http as h:
            status, payload = h.status, h.payload
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(payload).encode() if payload is not None else b""
        resp.headers["Content-Type"] = "application/json"
        resp.headers["API-Version"] = "4.1"
        resp.url = request.url
        resp.request = request
        resp.reason = "OK" if status < 400 else "ERR"
        return resp

    # ── routing ────────────────────────────────────────────────────────────
    def _route(self, method: str, path: str, q: dict, body: Any):
        parts = [p for p in path.split("/") if p]
        if parts[:1] != ["api"] or len(parts) < 3:
            return 200, {}
        app, coll = parts[1], parts[2]
        if (app, coll) not in _COLLECTIONS:
            raise _Http(404, {"detail": "Not found."})
        oid = int(parts[3]) if len(parts) > 3 else None
        if oid is not None:
            obj = self.db[coll].get(oid)
            if obj is None:
                raise _Http(404, {"detail": "Not found."})
            if method == "GET":
                self._maybe_fail("GET", coll, oid)
                return 200, self.render(coll, obj)
            if method == "PATCH":
                self._maybe_fail("PATCH", coll, oid)
                self._patch(coll, obj, body or {})
                return 200, self.render(coll, obj)
            if method == "DELETE":
                self._maybe_fail("DELETE", coll, oid)
                self._delete(coll, oid)
                return 204, None
            raise _Http(405, {"detail": "method"})
        if method == "GET":
            self._maybe_fail("GET", coll, "list")
            if coll == "devices" and "tenant" not in q:
                self._maybe_fail("GET", coll, "list-global")
            return 200, self._list(coll, q, path)
        if method == "POST":
            return 201, self._create(coll, body or {})
        if method == "PATCH" and isinstance(body, list):  # bulk update
            out = []
            for item in body:
                obj = self.db[coll].get(item.get("id"))
                if obj is None:
                    raise _Http(400, {"detail": f"id {item.get('id')} not found"})
                self._maybe_fail("PATCH", coll, obj["id"])
                self._patch(coll, obj, {k: v for k, v in item.items() if k != "id"})
                out.append(self.render(coll, obj))
            return 200, out
        raise _Http(405, {"detail": "method"})

    def _maybe_fail(self, method: str, coll: str, key: Any) -> None:
        if (method, coll, key) in self.fail:
            raise _Http(500, {"detail": f"injected failure {method} {coll} {key}"})

    # ── list / filter ──────────────────────────────────────────────────────
    def _list(self, coll: str, q: dict, path: str) -> dict:
        rows = list(self.db[coll].values())

        def one(k):
            return (q.get(k) or [None])[0]

        for key in ("slug", "name"):
            if one(key) is not None:
                rows = [r for r in rows if str(r.get(key)) == one(key)]
        if "id" in q:
            ids = {int(x) for x in q["id"]}
            rows = [r for r in rows if r["id"] in ids]
        if coll == "devices" and one("tenant") is not None:
            tid = self._slug_to_id("tenants", one("tenant"))
            rows = [r for r in rows if r.get("tenant_id") == tid and tid is not None]
        if one("tenant_id") is not None:
            rows = [r for r in rows if str(r.get("tenant_id")) == one("tenant_id")]
        if one("device_id") is not None:
            rows = [r for r in rows if str(r.get("device_id")) == one("device_id")]
        if coll == "ip-addresses":
            addr = one("address")
            if addr is not None:
                if "/" in addr:
                    rows = [r for r in rows if r["address"] == addr]
                else:
                    rows = [r for r in rows if _host(r["address"]) == addr]
            if one("vrf_id") is not None:
                rows = [r for r in rows if str(r.get("vrf_id")) == one("vrf_id")]
        if coll == "prefixes":
            rows = []  # no containing prefixes → sync falls back to /32
        if coll == "devices":
            rows.sort(key=lambda r: (str(r.get("name") or ""), r["id"]))
        else:
            rows.sort(key=lambda r: r["id"])
        limit = int(one("limit") or 0) or self.page_size
        limit = min(limit, self.page_size)
        offset = int(one("offset") or 0)
        page = rows[offset:offset + limit]
        nxt = None
        if offset + limit < len(rows):
            nq = {k: v for k, v in q.items() if k not in ("offset", "limit")}
            nq["limit"] = [str(limit)]
            nq["offset"] = [str(offset + limit)]
            nxt = BASE + path + "?" + urlencode(
                [(k, v) for k, vs in nq.items() for v in vs])
        return {"count": len(rows), "next": nxt, "previous": None,
                "results": [self.render(coll, r) for r in page]}

    def _slug_to_id(self, coll: str, slug: str) -> Optional[int]:
        for r in self.db[coll].values():
            if r.get("slug") == slug:
                return r["id"]
        return None

    # ── create ─────────────────────────────────────────────────────────────
    def _create(self, coll: str, body: dict) -> dict:
        key = body.get("slug") or body.get("name") or body.get("address") or ""
        self._maybe_fail("POST", coll, key)
        if coll == "devices":
            if not body.get("site"):
                raise _Http(400, {"site": ["This field is required."]})
            if not body.get("device_type") or not body.get("role"):
                raise _Http(400, {"device_type": ["This field is required."]})
            obj = {"name": body.get("name"), "serial": body.get("serial", ""),
                   "status": body.get("status", "active"),
                   "tenant_id": body.get("tenant"), "site_id": body.get("site"),
                   "role_id": body.get("role"),
                   "device_type_id": body.get("device_type"),
                   "primary_ip4_id": None,
                   "custom_fields": dict(body.get("custom_fields") or {})}
            self._check_device_unique(obj, None)
            return self.render(coll, self.add(coll, obj))
        if coll == "interfaces":
            if body.get("device") not in self.db["devices"]:
                raise _Http(400, {"device": ["Invalid pk"]})
            obj = {"device_id": body["device"], "name": body.get("name"),
                   "type": body.get("type", "other")}
            return self.render(coll, self.add(coll, obj))
        if coll == "ip-addresses":
            obj = {"address": body["address"], "vrf_id": body.get("vrf"),
                   "tenant_id": body.get("tenant"),
                   "dns_name": body.get("dns_name", ""),
                   "assigned_object_type": body.get("assigned_object_type"),
                   "assigned_object_id": body.get("assigned_object_id"),
                   "custom_fields": dict(body.get("custom_fields") or {})}
            self._check_ip_unique(obj, None)
            return self.render(coll, self.add(coll, obj))
        if coll == "journal-entries":
            obj = {k: body.get(k) for k in ("assigned_object_type",
                                            "assigned_object_id", "kind", "comment")}
            return self.render(coll, self.add(coll, obj))
        if coll in ("sites", "device-roles", "manufacturers", "tenants"):
            if self._slug_to_id(coll, body.get("slug")) is not None:
                raise _Http(400, {"slug": ["already exists"]})
            return self.render(coll, self.add(coll, dict(body)))
        if coll == "device-types":
            obj = {"model": body.get("model"), "slug": body.get("slug"),
                   "manufacturer_id": body.get("manufacturer")}
            return self.render(coll, self.add(coll, obj))
        raise _Http(405, {"detail": "create unsupported"})

    # ── update ─────────────────────────────────────────────────────────────
    def _patch(self, coll: str, obj: dict, body: dict) -> None:
        new = copy.deepcopy(obj)
        for k, v in body.items():
            if k == "custom_fields":
                new["custom_fields"] = {**(obj.get("custom_fields") or {}), **(v or {})}
            elif k in ("tenant", "site", "role", "device_type", "vrf", "manufacturer"):
                new[f"{k}_id"] = v.get("id") if isinstance(v, dict) else v
            elif k == "primary_ip4":
                new["primary_ip4_id"] = v.get("id") if isinstance(v, dict) else v
            elif k == "status" and isinstance(v, dict):
                new["status"] = v.get("value")
            elif k == "assigned_object":
                continue  # read-only nested view
            else:
                new[k] = v
        if coll == "devices":
            self._check_device_unique(new, obj["id"])
            pid = new.get("primary_ip4_id")
            if pid is not None and pid != obj.get("primary_ip4_id"):
                ip = self.db["ip-addresses"].get(pid)
                ok = (ip is not None and ip.get("assigned_object_type") == "dcim.interface"
                      and self.db["interfaces"].get(ip.get("assigned_object_id"), {})
                      .get("device_id") == obj["id"])
                if not ok:
                    raise _Http(400, {"primary_ip4": ["not assigned to this device"]})
        if coll == "ip-addresses":
            self._check_ip_unique(new, obj["id"])
        obj.clear()
        obj.update(new)

    def _check_device_unique(self, obj: dict, self_id: Optional[int]) -> None:
        name = obj.get("name")
        if not name:
            return
        for r in self.db["devices"].values():
            if r["id"] != self_id and r.get("name") == name \
                    and r.get("site_id") == obj.get("site_id") \
                    and r.get("tenant_id") == obj.get("tenant_id"):
                raise _Http(400, {"non_field_errors": [
                    "Device name must be unique per site and tenant."]})

    def _check_ip_unique(self, obj: dict, self_id: Optional[int]) -> None:
        for r in self.db["ip-addresses"].values():
            if r["id"] != self_id and _host(r["address"]) == _host(obj["address"]) \
                    and r.get("vrf_id") == obj.get("vrf_id"):
                raise _Http(400, {"address": [
                    f"Duplicate IP address found in global table: {r['address']}"]})

    # ── delete (with NetBox cascades) ──────────────────────────────────────
    def _delete(self, coll: str, oid: int) -> None:
        self.db[coll].pop(oid, None)
        if coll == "devices":
            for iid in [i for i, r in self.db["interfaces"].items() if r["device_id"] == oid]:
                self._delete("interfaces", iid)
        elif coll == "interfaces":
            for pid in [p for p, r in self.db["ip-addresses"].items()
                        if r.get("assigned_object_type") == "dcim.interface"
                        and r.get("assigned_object_id") == oid]:
                self._delete("ip-addresses", pid)
        elif coll == "ip-addresses":
            for d in self.db["devices"].values():
                if d.get("primary_ip4_id") == oid:
                    d["primary_ip4_id"] = None

    # ── rendering (NetBox REST shapes) ─────────────────────────────────────
    def _nested(self, coll: str, oid: Optional[int], app: str) -> Optional[dict]:
        if oid is None:
            return None
        r = self.db[coll].get(oid)
        out = {"id": oid, "url": f"{BASE}/api/{app}/{coll}/{oid}/"}
        if r is not None:
            for k in ("name", "slug", "address", "model"):
                if k in r:
                    out[k] = r[k]
            out["display"] = str(r.get("name") or r.get("address") or r.get("model") or oid)
        return out

    def render(self, coll: str, obj: dict) -> dict:
        app = next(a for a, c in _COLLECTIONS if c == coll)
        base = {"id": obj["id"], "url": f"{BASE}/api/{app}/{coll}/{obj['id']}/",
                "display": str(obj.get("name") or obj.get("address") or obj["id"])}
        if coll == "devices":
            return {**base, "name": obj.get("name"), "serial": obj.get("serial") or "",
                    "status": {"value": obj.get("status") or "active",
                               "label": (obj.get("status") or "active").title()},
                    "tenant": self._nested("tenants", obj.get("tenant_id"), "tenancy"),
                    "site": self._nested("sites", obj.get("site_id"), "dcim"),
                    "role": self._nested("device-roles", obj.get("role_id"), "dcim"),
                    "device_type": self._nested("device-types", obj.get("device_type_id"), "dcim"),
                    "primary_ip4": self._nested("ip-addresses", obj.get("primary_ip4_id"), "ipam"),
                    "custom_fields": copy.deepcopy(obj.get("custom_fields") or {})}
        if coll == "ip-addresses":
            return {**base, "address": obj["address"],
                    "vrf": self._nested("vrfs", obj.get("vrf_id"), "ipam"),
                    "tenant": self._nested("tenants", obj.get("tenant_id"), "tenancy"),
                    "dns_name": obj.get("dns_name") or "",
                    "assigned_object_type": obj.get("assigned_object_type"),
                    "assigned_object_id": obj.get("assigned_object_id"),
                    "custom_fields": copy.deepcopy(obj.get("custom_fields") or {})}
        if coll == "interfaces":
            return {**base, "name": obj.get("name"),
                    "device": self._nested("devices", obj.get("device_id"), "dcim"),
                    "type": {"value": obj.get("type"), "label": obj.get("type")}}
        if coll == "device-types":
            return {**base, "model": obj.get("model"), "slug": obj.get("slug"),
                    "manufacturer": self._nested("manufacturers", obj.get("manufacturer_id"), "dcim")}
        out = dict(base)
        out.update({k: v for k, v in obj.items() if k != "id"})
        return out

    # ── comparison helpers ─────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """Order-independent final state: devices/IPs/interfaces keyed by a
        stable identity (ids differ between runs only when creates are
        reordered, which a correct refactor never does — but keying by
        identity keeps the comparison meaningful either way)."""
        devs = {d["id"]: {k: d.get(k) for k in ("name", "serial", "tenant_id", "site_id",
                                              "role_id", "device_type_id",
                                              "primary_ip4_id", "custom_fields", "status")}
                for d in self.db["devices"].values()}
        ips = {p["id"]: {k: p.get(k) for k in ("address", "vrf_id", "tenant_id", "dns_name",
                                             "assigned_object_type", "assigned_object_id",
                                             "custom_fields")}
               for p in self.db["ip-addresses"].values()}
        ifaces = {i["id"]: dict(i) for i in self.db["interfaces"].values()}
        journal = sorted((j["assigned_object_type"], j["assigned_object_id"], j["comment"])
                         for j in self.db["journal-entries"].values())
        refs = {c: sorted((r.get("slug") or r.get("model") or "") for r in self.db[c].values())
                for c in ("sites", "device-roles", "device-types", "manufacturers")}
        return {"devices": devs, "ips": ips, "interfaces": ifaces,
                "journal": journal, "refs": refs}


class _Http(Exception):
    def __init__(self, status: int, payload: Any):
        super().__init__(f"{status} {payload}")
        self.status = status
        self.payload = payload


def attach(engine, fake: FakeNetBox) -> FakeNetBox:
    """Route ``engine``'s pynetbox + raw HTTP traffic to ``fake``."""
    engine.nb.http_session.mount(BASE + "/", fake)
    # no proxy / netrc discovery (slow on macOS, irrelevant in-process)
    engine.nb.http_session.trust_env = False
    engine._ensure_custom_fields = lambda *a, **k: {"status": "OK"}
    return fake


def make_engine(engine_cls, fake: FakeNetBox):
    eng = engine_cls(BASE, "0123456789abcdef0123456789abcdef01234567")
    attach(eng, fake)
    return eng


"""Tests for NetboxSpoke INSTALL_CERT — the NetBox (ipam) cert target.

NetBox has no cert API and the spoke runs as unprivileged svc_lm, so LE cert
distribution (hub-brokered) routes INSTALL_CERT here. The spoke is a **cert
custodian**: it validates the fullchain+privkey in-process (throwaway ssl ctx —
the same guard the hub uses in ``_install_cert_on_hub``), persists the material
encrypted so a restart or a late-connecting agent still gets it, then drives
every connected Agent to install it. The Agent does the privileged work:
WRITE_FILE both PEMs to 0600 temps, then RUN_COMMAND the root sudoers helper
``/usr/local/bin/lm-netbox-install-cert``, which swaps
``/etc/lm/netbox/tls/netbox.{crt,key}`` and reloads nginx.

The helper re-validates + runs ``nginx -t`` (restoring on failure) — that is
root-OS work and is not tested here. What IS tested is the spoke's contract:

* reject junk (missing material / non-PEM / mismatched pair) BEFORE anything
  reaches a live host;
* persist the material even when nobody is connected yet, and say so rather
  than reporting a deploy that did not happen;
* deploy to each connected agent and summarise the outcome;
* relay the agent's own failure text instead of a generic message.

Self-contained: inserts netbox/src/ + lm/core/src on sys.path (base_spoke) and
constructs the spoke via ``__new__`` (skipping ``__init__`` — it builds a real
NetboxEngine that would hit NetBox; the INSTALL_CERT handler never touches
``self.engine``). A real self-signed cert+key is generated with ``cryptography``
so the in-process ``ssl.load_cert_chain`` validation passes for the success
path (fake ``LEAF``/``KEY`` PEM bodies are rejected by the SSL library).
"""
import asyncio
import datetime as _dt
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
# lm/core/src supplies base_spoke. Derive it from THIS file's location — it used
# to be a hard-coded /Users/... absolute path, which resolved only on one
# developer's machine and could never work in CI.
sys.path.insert(0, os.path.join(_HERE, "..", "..", "lm", "core", "src"))

import netbox_spoke as spoke_mod  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _real_pair(cn: str = "netbox.test"):
    """Generate a real self-signed cert + matching privkey (PEM) so
    ssl.load_cert_chain accepts the pair during the spoke's in-process
    validation step."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    crt_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption()).decode()
    return crt_pem, key_pem


class _FakeControlPlane:
    """Stand-in for the NetboxControlPlane.

    Records every send_to_agent call so a test can assert the WRITE_FILE +
    RUN_COMMAND sequence, and returns a canned RUN_COMMAND result.
    ``connected_agents`` is what the handler reads to decide deploy-vs-cache.
    """

    def __init__(self, agents=(), rc=0, stdout="OK installed", stderr="",
                 raise_on=None):
        self.connected_agents = {a: object() for a in agents}
        self.secret = "test-secret"
        self.calls = []
        self._rc, self._stdout, self._stderr = rc, stdout, stderr
        self._raise_on = raise_on

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=None):
        self.calls.append({"cmd": cmd, "data": data, "agent_id": agent_id})
        if self._raise_on and cmd == self._raise_on:
            raise RuntimeError("agent link died")
        if cmd == "RUN_COMMAND":
            return {"result": {"rc": self._rc, "stdout": self._stdout,
                               "stderr": self._stderr}}
        return {"status": "SUCCESS"}


@pytest.fixture
def store(tmp_path):
    """Path the custodian persists to — tmp so no test writes /etc or /var."""
    return str(tmp_path / "cert_store.enc")


def _make_spoke(store, control_plane=None):
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    sp.control_plane = control_plane
    sp.spoke_id = "netbox-test"
    sp._cert_material = None
    sp._cert_store = store
    return sp


def _install(sp, crt, key, domain="netbox.test"):
    return _run(sp._handle_install_cert(
        {"domain": domain, "fullchain": crt, "privkey": key}))


def _run_commands(cp):
    return [c["data"]["command"] for c in cp.calls if c["cmd"] == "RUN_COMMAND"]


# ── rejected before anything reaches a host ──────────────────────────────────

def test_missing_material_errors_and_never_touches_an_agent(store):
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    res = _run(sp._handle_install_cert({"domain": "netbox.test"}))
    assert res["status"] == "ERROR"
    assert "missing cert material" in res["message"]
    assert cp.calls == []
    assert not os.path.exists(store)          # nothing persisted


def test_non_pem_rejected_and_never_touches_an_agent(store):
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    res = _install(sp, "not-a-cert", "not-a-key")
    assert res["status"] == "ERROR"
    assert "not PEM" in res["message"]
    assert cp.calls == []
    assert not os.path.exists(store)


def test_mismatched_pair_rejected_before_deploy(store):
    # PEM-shaped and structurally valid, but the key does not match the cert —
    # only the in-process ssl.load_cert_chain catches this. It must be caught
    # HERE: shipping it would swap in a cert nginx cannot serve.
    crt, _ = _real_pair()
    _, other_key = _real_pair("other.test")
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, other_key)
    assert res["status"] == "ERROR"
    assert "cert validation failed" in res["message"]
    assert cp.calls == []
    assert not os.path.exists(store)


# ── custody ──────────────────────────────────────────────────────────────────

def test_valid_pair_with_no_agent_is_cached_not_reported_as_deployed(store):
    # The IPAM spoke commonly has no NetBox-host agent attached yet. Caching is
    # the correct outcome, but it must NOT claim an install happened.
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=[])
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)
    assert res["status"] == "SUCCESS"
    assert "cached" in res["message"]
    assert "deploy" in res["message"]         # tells the operator what happens next
    assert _run_commands(cp) == []
    assert sp._cert_material["fullchain"] == crt
    assert os.path.exists(store)              # survives a restart


def test_persisted_material_is_encrypted_at_rest(store):
    # The private key is the sensitive half; it must not be readable in the file.
    crt, key = _real_pair()
    sp = _make_spoke(store, _FakeControlPlane(agents=[]))
    _install(sp, crt, key)
    blob = open(store, "rb").read()
    assert b"PRIVATE KEY" not in blob
    assert key.encode() not in blob


def test_cached_cert_deploys_to_a_late_connecting_agent(store):
    # The whole point of custody: an agent that shows up after the cert arrived
    # still gets it, without the hub re-issuing.
    crt, key = _real_pair()
    sp = _make_spoke(store, _FakeControlPlane(agents=[]))
    _install(sp, crt, key)

    cp = _FakeControlPlane(agents=["late-agent"])
    sp.control_plane = cp
    _run(sp.deploy_cached_cert_to_agent("late-agent"))
    assert any(c["cmd"] == "WRITE_FILE" for c in cp.calls)
    assert any("lm-netbox-install-cert" in c for c in _run_commands(cp))


def test_no_cached_cert_means_a_connecting_agent_gets_nothing(store):
    cp = _FakeControlPlane(agents=["late-agent"])
    sp = _make_spoke(store, cp)
    _run(sp.deploy_cached_cert_to_agent("late-agent"))
    assert cp.calls == []


# ── deploy ───────────────────────────────────────────────────────────────────

def test_deploy_writes_both_pems_then_runs_helper_then_cleans_temps(store):
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)

    assert res["status"] == "SUCCESS"
    assert "1/1" in res["message"]

    writes = [c for c in cp.calls if c["cmd"] == "WRITE_FILE"]
    assert len(writes) == 2
    assert [w["data"]["content"] for w in writes] == [crt, key]
    # Both PEMs land 0600 — the key must never be world-readable on the host.
    assert all(w["data"]["mode"] == 0o600 for w in writes)

    cmds = _run_commands(cp)
    assert "lm-netbox-install-cert" in cmds[0]
    assert cmds[0].startswith("sudo -n ")
    # Temps are removed even on the success path; both paths written are named.
    assert cmds[-1].startswith("rm -f ")
    for w in writes:
        assert w["data"]["path"] in cmds[-1]


def test_helper_failure_maps_to_error_and_still_cleans_temps(store):
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=["agent-1"], rc=1, stdout="",
                           stderr="ERROR: nginx -t failed")
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)
    assert res["status"] == "ERROR"
    # The agent's own text is relayed — "nginx -t failed" and "helper missing"
    # need different fixes, so a generic message would cost a debugging round.
    assert "nginx -t failed" in res["message"]
    assert _run_commands(cp)[-1].startswith("rm -f ")


def test_helper_stdout_used_when_stderr_empty(store):
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=["agent-1"], rc=2,
                           stdout="ERROR: helper not installed", stderr="")
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)
    assert res["status"] == "ERROR"
    assert "helper not installed" in res["message"]


def test_rc_zero_without_ok_prefix_is_still_a_failure(store):
    # The helper signals success by printing OK. A zero exit alone is not
    # enough — a helper that no-ops would otherwise read as installed.
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=["agent-1"], rc=0, stdout="nothing to do")
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)
    assert res["status"] == "ERROR"


def test_agent_link_failure_is_reported_not_raised(store):
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=["agent-1"], raise_on="WRITE_FILE")
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)
    assert res["status"] == "ERROR"
    assert "agent link died" in res["message"]


def test_partial_success_across_agents_reports_success_with_a_count(store):
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=["agent-1", "agent-2"])

    ok = {"agent-1"}

    async def send_to_agent(cmd, data, agent_id=None, timeout=None):
        cp.calls.append({"cmd": cmd, "data": data, "agent_id": agent_id})
        if cmd == "RUN_COMMAND":
            good = agent_id in ok
            return {"result": {"rc": 0 if good else 1,
                               "stdout": "OK installed" if good else "",
                               "stderr": "" if good else "boom"}}
        return {"status": "SUCCESS"}

    cp.send_to_agent = send_to_agent
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)
    # One host serving the new cert is a better outcome than none, so this is
    # SUCCESS — but the count has to make the partial failure visible.
    assert res["status"] == "SUCCESS"
    assert "1/2" in res["message"]


def test_no_control_plane_still_takes_custody(store):
    crt, key = _real_pair()
    sp = _make_spoke(store, control_plane=None)
    res = _install(sp, crt, key)
    # Custody does not depend on a control plane: the material is kept so it can
    # be deployed once one exists.
    assert sp._cert_material is not None
    assert res["status"] == "SUCCESS"
    assert "cached" in res["message"]

"""Tests for NetboxSpoke INSTALL_CERT — the NetBox (ipam) cert target.

NetBox has no cert API and the spoke runs as unprivileged svc_lm, so LE cert
distribution (hub-brokered) routes INSTALL_CERT here. The spoke is a **cert
custodian**: it validates the fullchain+privkey in-process (throwaway ssl ctx —
the same guard the hub uses in ``_install_cert_on_hub``), persists the material
encrypted so a restart or a late-connecting agent still gets it, then drives
the explicitly associated Agent to install it. The Agent does the privileged work:
WRITE_FILE both PEMs to 0600 temps, then RUN_COMMAND the root sudoers helper
``/usr/local/bin/lm-netbox-install-cert``, which swaps
``/etc/lm/netbox/tls/netbox.{crt,key}`` and reloads nginx.

The helper re-validates + runs ``nginx -t`` (restoring on failure) — that is
root-OS work and is not tested here. What IS tested is the spoke's contract:

* reject junk (missing material / non-PEM / mismatched pair) BEFORE anything
  reaches a live host;
* persist the material even when nobody is connected yet, and say so rather
  than reporting a deploy that did not happen;
* deliver only to the explicitly associated, authenticated agent;
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
import types
from unittest.mock import Mock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
# lm/core/src supplies base_spoke. Derive it from THIS file's location — it used
# to be a hard-coded /Users/... absolute path, which resolved only on one
# developer's machine and could never work in CI.
sys.path.insert(0, os.environ.get("LM_CORE_SRC",
    os.path.join(_HERE, "..", "..", "lm", "core", "src")))

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
            return {"status": "SUCCESS" if self._rc == 0 else "ERROR",
                    "result": {"rc": self._rc, "stdout": self._stdout,
                               "stderr": self._stderr}}
        return {"status": "SUCCESS", "result": {"ok": True}}


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
    sp.cert_agent_id = "agent-1"
    sp._cert_replay_tasks = {}
    sp._cert_lock_obj = None
    sp._cert_association_lock_obj = None
    sp._cert_update_pending = False
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
    # The IPAM spoke commonly has no NetBox-host agent attached. Caching is
    # correct, but the status must be DEFERRED: a SUCCESS made the hub stop
    # retrying while the NetBox host kept the old cert until it expired.
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=[])
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)
    assert res["status"] == "DEFERRED"
    assert "not installed" in res["message"]
    assert "DEPLOY-AGENT-CERT.md" in res["message"]  # tells the operator the fix
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
    sp.cert_agent_id = "late-agent"
    result = _run(sp.deploy_cached_cert_to_agent("late-agent"))
    assert result["status"] == "SUCCESS"
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


@pytest.mark.parametrize("stdout", ["nothing to do", "OKAY", ["OK installed"]])
def test_rc_zero_without_ok_prefix_is_still_a_failure(store, stdout):
    # The helper signals success by printing OK. A zero exit alone is not
    # enough — a helper that no-ops would otherwise read as installed.
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=["agent-1"], rc=0, stdout=stdout)
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


def test_unrelated_connected_agents_never_receive_certificate_material(store):
    """Only the explicit instance association can receive a certificate."""
    crt, key = _real_pair()
    cp = _FakeControlPlane(agents=["agent-1", "agent-2"])

    ok = {"agent-1"}

    async def send_to_agent(cmd, data, agent_id=None, timeout=None):
        cp.calls.append({"cmd": cmd, "data": data, "agent_id": agent_id})
        if cmd == "RUN_COMMAND":
            good = agent_id in ok
            return {"status": "SUCCESS" if good else "ERROR",
                    "result": {"rc": 0 if good else 1,
                               "stdout": "OK installed" if good else "",
                               "stderr": "" if good else "boom"}}
        return {"status": "SUCCESS", "result": {"ok": True}}

    cp.send_to_agent = send_to_agent
    sp = _make_spoke(store, cp)
    res = _install(sp, crt, key)
    assert res["status"] == "SUCCESS"
    assert "1/1" in res["message"]
    assert {call["agent_id"] for call in cp.calls} == {"agent-1"}


@pytest.mark.parametrize("reply", [
    None, {}, {"status": "ERROR", "message": "Unknown command WRITE_FILE"},
    {"status": "SUCCESS"}, {"status": "SUCCESS", "result": {}},
    {"status": "SUCCESS", "result": None},
    {"status": "SUCCESS", "result": {"ok": False}},
])
def test_failed_or_old_peer_write_never_runs_install_helper(store, reply):
    """The helper cannot run unless each write was explicitly acknowledged."""
    cp = _FakeControlPlane(agents=["agent-1"])
    original = cp.send_to_agent

    async def send(cmd, data, **kwargs):
        """Return the failure as an ordinary reply, not an exception."""
        if cmd == "WRITE_FILE":
            return reply
        return await original(cmd, data, **kwargs)

    cp.send_to_agent = send
    sp = _make_spoke(store, cp)
    crt, key = _real_pair()
    result = _install(sp, crt, key)
    assert result["status"] == "ERROR"
    assert "WRITE_FILE" in result["message"]
    assert not any("lm-netbox-install-cert" in cmd for cmd in _run_commands(cp))


def test_missing_association_defers_even_with_a_connected_host(store):
    """No first-connected-host fallback is allowed."""
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    sp.cert_agent_id = ""
    result = _install(sp, *_real_pair())
    assert result["status"] == "DEFERRED"
    assert cp.calls == []


def test_second_write_failure_never_runs_install_helper(store):
    """A successful certificate write cannot mask a failed private-key write."""
    cp = _FakeControlPlane(agents=["agent-1"])
    original = cp.send_to_agent
    writes = 0

    async def send(cmd, data, **kwargs):
        """Fail only the private-key write."""
        nonlocal writes
        if cmd == "WRITE_FILE":
            writes += 1
            if writes == 2:
                return {"status": "ERROR", "message": "disk full"}
        return await original(cmd, data, **kwargs)

    cp.send_to_agent = send
    result = _install(_make_spoke(store, cp), *_real_pair())
    assert result["status"] == "ERROR" and writes == 2
    assert not any("lm-netbox-install-cert" in cmd for cmd in _run_commands(cp))


def test_error_envelope_cannot_be_overridden_by_success_shaped_helper_result(store):
    """A failed peer RPC cannot claim delivery using rc/stdout from a stale result."""
    cp = _FakeControlPlane(agents=["agent-1"])
    original = cp.send_to_agent

    async def send(cmd, data, **kwargs):
        """Wrap an apparently good helper result in an error envelope."""
        reply = await original(cmd, data, **kwargs)
        if cmd == "RUN_COMMAND":
            reply["status"] = "ERROR"
        return reply

    cp.send_to_agent = send
    assert _install(_make_spoke(store, cp), *_real_pair())["status"] == "ERROR"


def test_cache_persist_failure_never_reports_success(store, monkeypatch):
    """An undurable certificate cannot be acknowledged as safely cached."""
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    monkeypatch.setattr(sp, "_persist_cert", lambda: False)
    result = _install(sp, *_real_pair())
    assert result["status"] == "ERROR"
    assert cp.calls == []


def test_replay_is_scheduled_only_for_associated_connected_agent(store):
    """Registration returns before replay RPCs wait for the listener's reader."""
    cp = _FakeControlPlane(agents=["agent-1"], rc=1, stderr="reload failed")
    sp = _make_spoke(store, cp)
    sp._cert_material = dict(zip(("fullchain", "privkey"), _real_pair()))

    async def run():
        """Assert the task yields until registration has completed."""
        sp.schedule_cached_cert_to_agent("unrelated")
        assert sp._cert_replay_tasks == {}
        sp.schedule_cached_cert_to_agent("agent-1")
        sp.schedule_cached_cert_to_agent("agent-1")
        assert cp.calls == []
        assert len(sp._cert_replay_tasks) == 1
        result = await sp._cert_replay_tasks["agent-1"]
        assert result["status"] == "ERROR"
        assert "reload failed" in result["message"]

    asyncio.run(run())


def test_no_control_plane_still_takes_custody(store):
    crt, key = _real_pair()
    sp = _make_spoke(store, control_plane=None)
    res = _install(sp, crt, key)
    # Custody does not depend on a control plane: the material is kept so it can
    # be deployed once one exists.
    assert sp._cert_material is not None
    assert res["status"] == "DEFERRED"
    assert "cached" in res["message"]


def test_association_config_is_persisted_preserved_on_restart_and_clearable(store, tmp_path, monkeypatch):
    """Only our module's saved association is loaded; unrelated config survives."""
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(spoke_mod, "__file__", str(tmp_path / "src" / "netbox_spoke.py"))
    env_file = tmp_path / ".env"
    env_file.write_text("NETBOX_URL=https://saved.example.test\n")
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    sp.config = {}
    sp.engine = types.SimpleNamespace(verify_ssl=True)
    sp._picklist_invalidate = lambda: None
    sp._list_invalidate = lambda: None
    sp.schedule_cached_cert_to_agent = Mock()

    result = _run(sp.handle_command("UPDATE_CONFIG", {"cert_agent_id": "agent-1"}))
    assert result["status"] == "SUCCESS"
    sp.schedule_cached_cert_to_agent.assert_called_once_with("agent-1")
    assert "NETBOX_URL=https://saved.example.test\n" in env_file.read_text()
    assert (env_file.stat().st_mode & 0o777) == 0o600

    monkeypatch.setattr(spoke_mod, "NetboxEngine", Mock())
    monkeypatch.setattr(spoke_mod, "NwInventoryStore", Mock())
    monkeypatch.setenv("LM_NETBOX_CERT_STORE", store)
    restarted = spoke_mod.NetboxSpoke("netbox-test", {}, control_plane=cp)
    assert restarted.cert_agent_id == "agent-1"
    assert _run(sp.handle_command("UPDATE_CONFIG", {}))["status"] == "SUCCESS"
    assert sp.cert_agent_id == "agent-1"
    assert _run(sp.handle_command("UPDATE_CONFIG", {"cert_agent_id": ""}))["status"] == "SUCCESS"
    assert spoke_mod.NetboxSpoke("netbox-test", {}, control_plane=cp).cert_agent_id == ""


@pytest.mark.parametrize("agent_id", ["new-agent", "invalid\nidentity"])
def test_failed_association_update_never_changes_active_host_or_replays(store, monkeypatch, agent_id):
    """Validation/durable-save errors leave both active identity and cache untouched."""
    sp = _make_spoke(store, _FakeControlPlane(agents=["new-agent"]))
    persist = Mock(return_value=False)
    replay = Mock()
    monkeypatch.setattr(sp, "_persist_env", persist)
    monkeypatch.setattr(sp, "schedule_cached_cert_to_agent", replay)
    result = _run(sp.handle_command("UPDATE_CONFIG", {"cert_agent_id": agent_id}))
    assert result["status"] == "ERROR"
    assert sp.cert_agent_id == "agent-1"
    replay.assert_not_called()
    assert persist.call_count == (0 if "\n" in agent_id else 1)


def test_association_atomic_write_failure_preserves_previous_configuration(store, tmp_path, monkeypatch):
    """A failed association save cannot truncate the module's other settings."""
    monkeypatch.setattr(spoke_mod, "__file__", str(tmp_path / "src" / "netbox_spoke.py"))
    env_file = tmp_path / ".env"
    before = "NETBOX_URL=https://saved.example.test\nNETBOX_CERT_AGENT_ID=old-agent\n"
    env_file.write_text(before)
    monkeypatch.setattr(spoke_mod.os, "replace", Mock(side_effect=OSError("read-only")))
    sp = _make_spoke(store)
    assert not sp._persist_env("NETBOX_CERT_AGENT_ID", "new-agent")
    assert env_file.read_text() == before


def test_reassociation_cancels_paused_replay_before_private_key(store, monkeypatch):
    """A pending A write is cancelled and A never receives the key or helper."""
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    sp.config = {}
    sp.engine = types.SimpleNamespace(verify_ssl=True)
    sp._picklist_invalidate = lambda: None
    sp._list_invalidate = lambda: None
    monkeypatch.setattr(sp, "_persist_env", lambda *_: True)
    crt, key = _real_pair()
    sp._cert_material = {"fullchain": crt, "privkey": key}

    async def run():
        """Pause the first real RPC while UPDATE_CONFIG clears its association."""
        started = asyncio.Event()
        original = cp.send_to_agent

        async def send(cmd, data, **kwargs):
            if cmd == "WRITE_FILE":
                cp.calls.append({"cmd": cmd, "data": data, "agent_id": kwargs["agent_id"]})
                started.set()
                await asyncio.Event().wait()
            return await original(cmd, data, **kwargs)

        cp.send_to_agent = send
        sp.schedule_cached_cert_to_agent("agent-1")
        await started.wait()
        result = await sp.handle_command("UPDATE_CONFIG", {"cert_agent_id": ""})
        assert result["status"] == "SUCCESS"
        assert sp.cert_agent_id == ""
        assert sp._cert_replay_tasks["agent-1"].cancelled()
        assert not any(c["data"].get("content") == key for c in cp.calls)
        assert not any("lm-netbox-install-cert" in cmd for cmd in _run_commands(cp))

    asyncio.run(run())


def test_overlapping_installs_serialize_custody_and_delivery(store):
    """Two installs never interleave writes or race over the cached temp file."""
    first = _real_pair("first.test")
    second = _real_pair("second.test")
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)

    async def run():
        """Hold the first RPC while the second install starts."""
        started, resume = asyncio.Event(), asyncio.Event()
        original = cp.send_to_agent
        paused = False

        async def send(cmd, data, **kwargs):
            nonlocal paused
            if cmd == "WRITE_FILE" and not paused:
                paused = True
                started.set()
                await resume.wait()
            return await original(cmd, data, **kwargs)

        cp.send_to_agent = send
        a = asyncio.create_task(sp._handle_install_cert(
            {"domain": "first.test", "fullchain": first[0], "privkey": first[1]}))
        await started.wait()
        b = asyncio.create_task(sp._handle_install_cert(
            {"domain": "second.test", "fullchain": second[0], "privkey": second[1]}))
        await asyncio.sleep(0)
        assert not b.done()
        resume.set()
        assert [r["status"] for r in await asyncio.gather(a, b)] == ["SUCCESS", "SUCCESS"]
        assert [c["data"]["content"] for c in cp.calls if c["cmd"] == "WRITE_FILE"] == [
            first[0], first[1], second[0], second[1]]
        restored = _make_spoke(store, cp)
        restored._load_persisted_cert()
        assert restored._cert_material["domain"] == "second.test"

    asyncio.run(run())


def test_reassociation_blocks_key_during_inflight_install(store, monkeypatch):
    """A concurrent direct install cannot continue to the old host's key."""
    cp = _FakeControlPlane(agents=["agent-1"])
    sp = _make_spoke(store, cp)
    sp.config = {}
    sp.engine = types.SimpleNamespace(verify_ssl=True)
    sp._picklist_invalidate = lambda: None
    sp._list_invalidate = lambda: None
    monkeypatch.setattr(sp, "_persist_env", lambda *_: True)
    crt, key = _real_pair()

    async def run():
        """Pause the first write and begin an association change."""
        entered, resume = asyncio.Event(), asyncio.Event()
        original = cp.send_to_agent

        async def send(cmd, data, **kwargs):
            if cmd == "WRITE_FILE":
                entered.set()
                await resume.wait()
            return await original(cmd, data, **kwargs)

        cp.send_to_agent = send
        install = asyncio.create_task(sp._handle_install_cert(
            {"domain": "netbox.test", "fullchain": crt, "privkey": key}))
        await entered.wait()
        update = asyncio.create_task(sp.handle_command("UPDATE_CONFIG", {"cert_agent_id": ""}))
        while not sp._cert_update_pending:
            await asyncio.sleep(0)
        resume.set()
        installed, updated = await asyncio.gather(install, update)
        assert installed["status"] == "ERROR" and updated["status"] == "SUCCESS"
        assert not any(c["data"].get("content") == key for c in cp.calls)
        assert not any("lm-netbox-install-cert" in cmd for cmd in _run_commands(cp))

    asyncio.run(run())


def test_unchanged_persisted_association_does_not_replace_env(store, tmp_path, monkeypatch):
    """Repeated reconnect projections do not rewrite an unchanged .env."""
    monkeypatch.setattr(spoke_mod, "__file__", str(tmp_path / "src" / "netbox_spoke.py"))
    env_file = tmp_path / ".env"
    env_file.write_text("NETBOX_CERT_AGENT_ID=agent-1\n")
    replace = Mock(side_effect=AssertionError("unexpected rewrite"))
    monkeypatch.setattr(spoke_mod.os, "replace", replace)
    assert _make_spoke(store)._persist_env("NETBOX_CERT_AGENT_ID", "agent-1")
    replace.assert_not_called()

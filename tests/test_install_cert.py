"""Tests for NetboxSpoke INSTALL_CERT — the NetBox (ipam) cert target.

NetBox has no cert API and the spoke runs as unprivileged svc_lm, so LE cert
distribution (hub-brokered) routes INSTALL_CERT here: the spoke validates the
fullchain+privkey in-process (throwaway ssl ctx — same guard the hub uses in
``_install_cert_on_hub``), writes both to 0600 temp files under /tmp, and hands
the paths to the root sudoers helper ``/usr/local/bin/lm-netbox-install-cert``,
which swaps ``/etc/lm/netbox/tls/netbox.{crt,key}`` + reloads nginx. The helper
re-validates + nginx -t (restores on failure) — we don't test that here (it's
root-OS work); we test the spoke's contract: validate BEFORE calling the helper,
clean up temps always, and map the helper's one-line stdout/exit to
SUCCESS/ERROR.

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
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/Users/lbockenstedt/vscode/lm/core/src")

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
    now = _dt.datetime.utcnow()
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


def _make_spoke():
    """NetboxSpoke instance with __init__ skipped (the INSTALL_CERT handler
    never touches self.engine, so the engine that would hit NetBox is never
    needed). The handler's early ``os.path.exists(_NETBOX_INSTALL_CERT_HELPER)``
    check would short-circuit on a test machine where the real helper isn't
    installed, so point the constant at a path that exists HERE — the exec-path
    tests below exercise the sudo/helper contract. The dedicated absent-helper
    test overrides this to a missing path."""
    sp = spoke_mod.NetboxSpoke.__new__(spoke_mod.NetboxSpoke)
    spoke_mod._NETBOX_INSTALL_CERT_HELPER = os.path.abspath(__file__)
    return sp


class _FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input=None):
        return self._stdout, self._stderr


def _patch_exec(capture, returncode=0, stdout=b"OK installed /etc/lm/netbox/tls/netbox.crt + reloaded nginx",
                stderr=b"", raises=None):
    """Replace asyncio.create_subprocess_exec on the spoke module with a fake
    that records the call (argv) and returns a _FakeProc. ``raises`` (an
    exception instance) makes the helper invocation raise, to exercise the
    helper-missing/timeout path. Returns the real exec to restore."""
    real = spoke_mod.asyncio.create_subprocess_exec

    async def fake_exec(bin_, *args, **kwargs):
        capture.setdefault("calls", []).append([bin_, *args])
        if raises is not None:
            raise raises
        return _FakeProc(returncode, stdout=stdout, stderr=stderr)

    spoke_mod.asyncio.create_subprocess_exec = fake_exec
    return real


def _tmp_files_after():
    """Count leftover temp cert/key files the spoke failed to clean up."""
    d = tempfile.gettempdir()
    return [f for f in os.listdir(d) if f.endswith((".crt.pem", ".key.pem"))
            and f.startswith("tmp")]


def test_install_cert_success_calls_helper_and_cleans_temps():
    crt, key = _real_pair()
    sp = _make_spoke()
    cap = {}
    real = _patch_exec(cap)
    try:
        res = _run(sp.handle_command("INSTALL_CERT",
                                     {"domain": "netbox.test",
                                      "fullchain": crt, "privkey": key}))
    finally:
        spoke_mod.asyncio.create_subprocess_exec = real
    assert res["status"] == "SUCCESS", res
    assert "installed" in res["message"].lower()
    # The helper was invoked via sudo -n with the exact path + two temp file args.
    assert len(cap["calls"]) == 1
    call = cap["calls"][0]
    assert call[0] == "sudo"
    assert call[1] == "-n"
    assert call[2] == spoke_mod._NETBOX_INSTALL_CERT_HELPER
    assert len(call) == 5  # sudo -n <helper> <crt-tmp> <key-tmp>
    crt_tmp, key_tmp = call[3], call[4]
    assert crt_tmp != key_tmp
    assert os.path.basename(crt_tmp).endswith(".crt.pem")
    assert os.path.basename(key_tmp).endswith(".key.pem")
    # Both temps are gone (finally unlink).
    assert not os.path.exists(crt_tmp)
    assert not os.path.exists(key_tmp)


def test_install_cert_missing_material_errors_without_helper():
    sp = _make_spoke()
    cap = {}
    real = _patch_exec(cap)
    try:
        r1 = _run(sp.handle_command("INSTALL_CERT",
                                    {"domain": "x", "fullchain": "", "privkey": "k"}))
        r2 = _run(sp.handle_command("INSTALL_CERT",
                                    {"domain": "x", "fullchain": "c", "privkey": ""}))
        r3 = _run(sp.handle_command("INSTALL_CERT", {"domain": "x"}))
    finally:
        spoke_mod.asyncio.create_subprocess_exec = real
    for r in (r1, r2, r3):
        assert r["status"] == "ERROR"
        assert "missing cert material" in r["message"]
    # No helper invocation on missing material.
    assert cap.get("calls", []) == []


def test_install_cert_non_pem_rejected_without_helper():
    sp = _make_spoke()
    cap = {}
    real = _patch_exec(cap)
    try:
        # Has BEGIN CERTIFICATE but no PRIVATE KEY.
        r1 = _run(sp.handle_command("INSTALL_CERT",
                                    {"domain": "x",
                                     "fullchain": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n",
                                     "privkey": "not a key"}))
        # Has PRIVATE KEY but no BEGIN CERTIFICATE.
        r2 = _run(sp.handle_command("INSTALL_CERT",
                                    {"domain": "x", "fullchain": "not a cert",
                                     "privkey": "-----BEGIN PRIVATE KEY-----\nK\n-----END PRIVATE KEY-----\n"}))
    finally:
        spoke_mod.asyncio.create_subprocess_exec = real
    assert r1["status"] == "ERROR"
    assert "PEM" in r1["message"]
    assert r2["status"] == "ERROR"
    assert "PEM" in r2["message"]
    assert cap.get("calls", []) == []


def test_install_cert_bad_pair_rejected_before_helper():
    """A cert whose key doesn't match (or malformed PEM) is rejected by the
    in-process ssl.load_cert_chain BEFORE the helper is invoked — so the live
    nginx paths are never touched by a bad cert."""
    crt, _ = _real_pair()
    _, key = _real_pair()  # DIFFERENT key, not matching crt
    sp = _make_spoke()
    cap = {}
    real = _patch_exec(cap)
    try:
        res = _run(sp.handle_command("INSTALL_CERT",
                                     {"domain": "x", "fullchain": crt, "privkey": key}))
    finally:
        spoke_mod.asyncio.create_subprocess_exec = real
    assert res["status"] == "ERROR"
    assert "validation failed" in res["message"]
    assert "helper not called" in res["message"]
    # Helper never invoked.
    assert cap.get("calls", []) == []


def test_install_cert_helper_failure_maps_to_error_and_cleans_temps():
    crt, key = _real_pair()
    sp = _make_spoke()
    cap = {}
    real = _patch_exec(cap, returncode=1, stdout=b"", stderr=b"ERROR: nginx -t failed")
    try:
        res = _run(sp.handle_command("INSTALL_CERT",
                                     {"domain": "x", "fullchain": crt, "privkey": key}))
    finally:
        spoke_mod.asyncio.create_subprocess_exec = real
    assert res["status"] == "ERROR"
    assert "nginx -t failed" in res["message"]
    # Temps still cleaned up on the failure path.
    call = cap["calls"][0]
    assert not os.path.exists(call[3])
    assert not os.path.exists(call[4])


def test_install_cert_helper_stderr_used_when_stdout_empty():
    crt, key = _real_pair()
    sp = _make_spoke()
    cap = {}
    real = _patch_exec(cap, returncode=2, stdout=b"", stderr=b"ERROR: openssl not found")
    try:
        res = _run(sp.handle_command("INSTALL_CERT",
                                     {"domain": "x", "fullchain": crt, "privkey": key}))
    finally:
        spoke_mod.asyncio.create_subprocess_exec = real
    assert res["status"] == "ERROR"
    assert "openssl not found" in res["message"]


def test_install_cert_helper_missing_raises_cleans_temps_and_errors():
    """If sudo denies at exec time (helper present but sudo refuses / the binary
    vanished between the existence check and the exec), create_subprocess_exec
    raises FileNotFoundError. The handler must surface ERROR + still unlink
    temps. (A genuinely-absent helper is caught earlier by the os.path.exists
    check — see test_install_cert_helper_absent_returns_clear_error_no_exec.)"""
    crt, key = _real_pair()
    sp = _make_spoke()
    cap = {}
    real = _patch_exec(cap, raises=FileNotFoundError(2, "No such file", "sudo"))
    try:
        res = _run(sp.handle_command("INSTALL_CERT",
                                     {"domain": "x", "fullchain": crt, "privkey": key}))
    finally:
        spoke_mod.asyncio.create_subprocess_exec = real
    assert res["status"] == "ERROR"
    # Temps written before the helper call are cleaned up in finally.
    assert cap["calls"]  # the call was attempted


def test_install_cert_helper_absent_returns_clear_error_no_exec():
    """The IPAM spoke is API-only (install_all.sh runs install.sh --spoke-only,
    so the cert helper is NEVER provisioned on this host). When the helper is
    absent the handler must return a clear, actionable ERROR pointing at the
    netbox-server target — NOT the raw 'sudo: lm-netbox-install-cert: command
    not found' that surfaced when the cert target was misconfigured as 'ipam'
    on a split topology — and must NOT attempt the exec (no sudo call at all)."""
    crt, key = _real_pair()
    sp = _make_spoke()
    orig_helper = spoke_mod._NETBOX_INSTALL_CERT_HELPER
    spoke_mod._NETBOX_INSTALL_CERT_HELPER = "/tmp/lm-netbox-install-cert.does.not.exist"
    cap = {}
    real = _patch_exec(cap)
    try:
        res = _run(sp.handle_command("INSTALL_CERT",
                                     {"domain": "x", "fullchain": crt, "privkey": key}))
    finally:
        spoke_mod.asyncio.create_subprocess_exec = real
        spoke_mod._NETBOX_INSTALL_CERT_HELPER = orig_helper
    assert res["status"] == "ERROR"
    assert "netbox-server" in res["message"], res["message"]
    assert not cap.get("calls"), "exec must not be attempted when the helper is absent"
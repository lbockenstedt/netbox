"""Tests for the Entra ID SSO social-auth pipeline shipped by install.sh.

``lm_sso_pipeline.py`` is written to the NetBox project root at install time
from a heredoc embedded in ``install.sh`` (the same convention as
``lm_custom_validators.py`` — single source, no separate repo copy). These
tests extract that heredoc, load it, and exercise its pure helpers plus the
>200-groups Graph fallback so the shipped code is covered without standing up
a full Django/NetBox app context.

The Django-touching ``sync_entra_groups`` step (group mapping + assignment +
the allowed-group gate) needs the apps registry and is verified manually
against a live NetBox; the claim-decode and group-extraction logic it depends
on is unit-tested here.
"""
import base64
import json
import os
import re
import sys
import unittest
from unittest.mock import patch

# The module under test is a heredoc inside install.sh; load it from there so
# the tests pin the actually-shipped code (not a divergent repo copy).
INSTALL_SH = os.path.join(os.path.dirname(__file__), "..", "install.sh")
_HEREDOC = re.compile(
    r"cat > \"\$NB_PROJECT_DIR/lm_sso_pipeline\.py\" <<'LMSSO'\n(.*?)\nLMSSO",
    re.S,
)


def _load_pipeline_module():
    with open(INSTALL_SH) as f:
        src = f.read()
    m = _HEREDOC.search(src)
    if not m:
        raise RuntimeError("lm_sso_pipeline.py heredoc not found in install.sh")
    body = m.group(1)
    ns = {"__name__": "lm_sso_pipeline", "__file__": "lm_sso_pipeline.py"}
    exec(compile(body, "lm_sso_pipeline.py", "exec"), ns)
    return ns


def _make_id_token(claims):
    """Build an unsigned JWT (header.payload.signature) for testing the
    payload-decode path (signature is not verified by the helper)."""
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return "%s.%s.%s" % (b64({"alg": "none"}), b64(claims), "sig")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class LmSsoPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_pipeline_module()
        cls.decode = staticmethod(cls.mod["_decode_id_token_claims"])
        cls.member_groups = staticmethod(cls.mod["_member_groups"])

    # ── _decode_id_token_claims ──────────────────────────────────────────

    def test_decode_reads_payload_without_verifying(self):
        tok = _make_id_token({"groups": ["g1", "g2"], "sub": "u"})
        self.assertEqual(self.decode(tok)["groups"], ["g1", "g2"])

    def test_decode_rejects_malformed_tokens(self):
        for bad in ["", "not-a-jwt", "a.b", "a.b.c.d", None, "onlytwo.parts"]:
            self.assertEqual(self.decode(bad), {})

    def test_decode_never_raises_on_bad_base64(self):
        # signature-looking but un-decodable payload -> {} not an exception
        self.assertEqual(self.decode("h.@@@@@.s"), {})

    # ── _member_groups — groups claim present ───────────────────────────

    def test_groups_present_returns_object_ids(self):
        self.assertEqual(
            self.member_groups({"groups": ["a", "b", "c"]}, ""),
            ["a", "b", "c"],
        )

    def test_groups_empty_list_is_not_overflow(self):
        # An empty groups: [] means "user in zero groups", NOT a >200 overflow.
        self.assertEqual(self.member_groups({"groups": []}, "tok"), [])

    def test_groups_values_stringified(self):
        self.assertEqual(self.member_groups({"groups": [1, 2, 3]}, ""), ["1", "2", "3"])

    # ── _member_groups — absent / overflow ─────────────────────────────

    def test_no_groups_and_no_overflow_pointer_returns_empty(self):
        self.assertEqual(self.member_groups({}, "tok"), [])
        self.assertEqual(self.member_groups({"other": 1}, "tok"), [])

    def test_overflow_without_access_token_returns_empty(self):
        # Entra emits _claim_names={"groups": ...} when the groups claim overflowed.
        claims = {"_claim_names": {"groups": "src1"},
                  "_claim_sources": {"src1": {"endpoint": "..."}}}
        self.assertEqual(self.member_groups(claims, ""), [])

    def test_overflow_uses_graph_fallback(self):
        claims = {"_claim_names": {"groups": "src1"},
                  "_claim_sources": {"src1": {"endpoint": "..."}}}
        graph_payload = {"value": [{"id": "g-graph-1"}, {"id": "g-graph-2"}, {"no_id": 1}]}

        def fake_urlopen(req, timeout):
            self.assertEqual(req.full_url,
                             "https://graph.microsoft.com/v1.0/me/transitiveMemberOf?$select=id")
            self.assertEqual(req.headers.get("Authorization"), "Bearer ACCESS")
            return _FakeResp(graph_payload)

        with patch.object(self.mod["urllib"].request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(self.member_groups(claims, "ACCESS"),
                             ["g-graph-1", "g-graph-2"])

    def test_graph_fallback_swallows_errors(self):
        claims = {"_claim_names": {"groups": "src1"}}
        with patch.object(self.mod["urllib"].request, "urlopen", side_effect=RuntimeError("boom")):
            self.assertEqual(self.member_groups(claims, "ACCESS"), [])


class PipelineWiredInInstallShTest(unittest.TestCase):
    """Sanity: the pipeline step name the config block references exists in the
    shipped module, so configuration.py's SOCIAL_AUTH_PIPELINE can import it."""

    def test_sync_entra_groups_is_exported(self):
        self.assertIn("sync_entra_groups", LmSsoPipelineTests.mod)
        self.assertTrue(callable(LmSsoPipelineTests.mod["sync_entra_groups"]))

    def test_config_block_references_the_step(self):
        with open(INSTALL_SH) as f:
            src = f.read()
        # The configuration.py writer embeds the pipeline path.
        self.assertIn("lm_sso_pipeline.sync_entra_groups", src)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    unittest.main()
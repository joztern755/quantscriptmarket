"""Cross-language check: files written by signals/emit.js (Node) verify in app.strategies.signals (Python).

Skipped when `node` is not on PATH. Uses the deterministic synthetic rows of signals/testdata.js (no market data).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:  # the cryptography native backend is broken on some dev boxes (pyo3 panic); never skip in CI
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
except BaseException as e:  # noqa: BLE001 - pyo3 PanicException derives from BaseException
    if isinstance(e, (KeyboardInterrupt, SystemExit)) or os.environ.get("CI"):
        raise
    raise unittest.SkipTest(f"cryptography unavailable: {type(e).__name__}") from None

from app.strategies import signals as S

ROOT = Path(__file__).resolve().parents[2]
SIGNALS = ROOT / "signals"
NODE = shutil.which("node")
UTC = timezone.utc


@unittest.skipUnless(NODE and (SIGNALS / "emit.js").exists(), "node or signals/emit.js not available")
class NodeEmitPythonVerify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.key = Ed25519PrivateKey.generate()
        pem = cls.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        cls.pub = base64.b64encode(cls.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        subprocess.run([NODE, str(SIGNALS / "testdata.js"), str(d / "in.json")], check=True, timeout=120)
        env = {**os.environ, "SIGNALS_ED25519_PRIVATE_KEY_PEM": pem, "SIGNALS_ED25519_PUBLIC_KEY_B64": cls.pub}
        subprocess.run([NODE, str(SIGNALS / "emit.js"), "--input", str(d / "in.json"), "--out-dir", str(d / "out"),
                        "--now", "2026-09-30T00:30:00Z", "--self-test-cuts", "5"],
                       check=True, timeout=300, env=env, capture_output=True)
        cls.body = (d / "out" / "signals.json").read_bytes()
        cls.sig = (d / "out" / "signals.sig").read_bytes()
        cls.manifest = json.loads((SIGNALS / "vendor" / "MANIFEST.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_python_verifies_node_output(self):
        b = S.verify_and_parse(self.body, self.sig, pubkey_b64=self.pub, now=datetime(2026, 9, 30, 0, 35, tzinfo=UTC),
                               expected_script_sha256={"silver": self.manifest["scripts"]["silver"]["sha256"]})
        r = b.records[0]
        self.assertEqual(r.strategy_key, "silver")
        self.assertEqual(r.coin, "xyz:SILVER")
        self.assertEqual(r.bar_close, datetime(2026, 9, 30, tzinfo=UTC))
        self.assertEqual(r.target_weight_bps, 10_000)          # synthetic seed 4 ends LONG
        self.assertEqual(r.last_action, "BUY")

    def test_canonical_bytes_agree(self):
        self.assertEqual(S.canonical_json(json.loads(self.body)), self.body)

    def test_tampered_node_output_rejected(self):
        with self.assertRaises(S.SignalSignatureInvalid):
            S.verify_and_parse(self.body.replace(b'"target_weight":1', b'"target_weight":2'), self.sig,
                               pubkey_b64=self.pub, now=datetime(2026, 9, 30, 0, 35, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()

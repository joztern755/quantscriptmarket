#!/usr/bin/env python3
"""Tests for infra/hosting/deploy_hosting.py against a fake Firebase Hosting REST server (stdlib only).

    python3 infra/hosting/test_deploy_hosting.py
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_hosting as dh  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class Fake(BaseHTTPRequestHandler):
    log: list = []
    uploads: dict = {}
    known: set = set()

    def log_message(self, *a) -> None:  # silence
        pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, obj: dict) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        assert self.headers["Authorization"] == "Bearer tok", "missing token"
        Fake.log.append(("POST", self.path))
        if self.path == "/v1beta1/sites/site-1/versions":
            Fake.config = json.loads(body)["config"]
            return self._json({"name": "sites/site-1/versions/v1"})
        if self.path == "/v1beta1/sites/site-1/versions/v1:populateFiles":
            files = json.loads(body)["files"]
            Fake.files = files
            need = sorted({h for h in files.values() if h not in Fake.known})
            return self._json({"uploadRequiredHashes": need, "uploadUrl": f"http://127.0.0.1:{self.server.server_port}/upload/sites/site-1/versions/v1/files"})
        if self.path.startswith("/upload/sites/site-1/versions/v1/files/"):
            h = self.path.rsplit("/", 1)[1]
            assert hashlib.sha256(body).hexdigest() == h, "hash mismatch"
            Fake.uploads[h] = gzip.decompress(body)
            return self._json({})
        if self.path.startswith("/v1beta1/sites/site-1/releases?versionName=sites/site-1/versions/v1"):
            Fake.release = json.loads(body)
            return self._json({"name": "sites/site-1/releases/r1"})
        self.send_response(404)
        self.end_headers()

    def do_PATCH(self) -> None:  # noqa: N802
        body = self._body()
        Fake.log.append(("PATCH", self.path))
        assert self.path == "/v1beta1/sites/site-1/versions/v1?update_mask=status"
        assert json.loads(body) == {"status": "FINALIZED"}
        return self._json({})


class DeployHostingTest(unittest.TestCase):
    def test_config_translation_matches_firebase_json(self) -> None:
        fb = json.loads((ROOT / "firebase.json").read_text())["hosting"]
        cfg = dh.hosting_config(fb)
        self.assertEqual(cfg["rewrites"], [{"path": "/index.html", "glob": "**"}])
        self.assertEqual(cfg["trailingSlashBehavior"], "REMOVE")
        doc = [h for h in cfg["headers"] if "regex" in h and "X-Frame-Options" in h["headers"]]
        self.assertEqual(len(doc), 1)
        self.assertEqual(doc[0]["headers"]["X-Frame-Options"], "DENY")
        self.assertIn("Content-Security-Policy", doc[0]["headers"])
        self.assertIn("Reporting-Endpoints", doc[0]["headers"])

    def test_ignore_globs(self) -> None:
        r = dh.glob_to_regex
        self.assertTrue(r("**/node_modules/**").match("a/node_modules/x.js"))
        self.assertTrue(r("**/node_modules/**").match("node_modules/x.js"))
        self.assertTrue(r("csp.txt").match("csp.txt"))
        self.assertFalse(r("csp.txt").match("legal/csp.txt"))
        self.assertTrue(r("**/.DS_Store").match(".DS_Store"))

    def test_deploy_flow(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            pub = Path(d)
            (pub / "index.html").write_text("<!doctype html>")
            (pub / "app").mkdir()
            (pub / "app" / "main.js").write_text("export {}")
            (pub / "csp.txt").write_text("ignored")
            (pub / "headers.json").write_text("{}")
            srv = HTTPServer(("127.0.0.1", 0), Fake)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                Fake.known = {dh.collect(pub, [])["/app/main.js"][0]}   # already on the CDN: not re-uploaded
                fb = {"public": "x", "ignore": ["csp.txt", "headers.json"], "rewrites": [{"source": "**", "destination": "/index.html"}],
                      "headers": [{"source": "**", "headers": [{"key": "X-Test", "value": "1"}]}], "trailingSlash": False}
                api = dh.Api("tok", f"http://127.0.0.1:{srv.server_port}/v1beta1")
                rel = dh.deploy(api, "site-1", pub, fb, "deploy test")
            finally:
                srv.shutdown()
                srv.server_close()
        self.assertEqual(rel, "sites/site-1/releases/r1")
        self.assertEqual(sorted(Fake.files), ["/app/main.js", "/index.html"])        # ignored files not deployed
        self.assertEqual(list(Fake.uploads.values()), [b"<!doctype html>"])          # only the missing hash uploaded
        self.assertEqual(Fake.config["headers"], [{"headers": {"X-Test": "1"}, "glob": "**"}])
        self.assertEqual([m for m, _ in Fake.log][-2:], ["PATCH", "POST"])           # finalize, then release
        self.assertEqual(Fake.release, {"message": "deploy test"})

    def test_refuses_without_index(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                dh.deploy(dh.Api("tok", "http://127.0.0.1:9/v1beta1"), "site-1", Path(d), {"ignore": []}, "x")


if __name__ == "__main__":
    unittest.main()

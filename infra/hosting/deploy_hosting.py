#!/usr/bin/env python3
"""Deploy a built site (web/dist) to Firebase Hosting through the Hosting REST API — stdlib only.

Why (docs/security/REVIEW_WEB_INFRA.md H2): the deploy job must not run npm packages (firebase-tools and its
dependency tree, lifecycle scripts included) while it holds cloud credentials. This script is the whole Hosting
deploy: it reads firebase.json (headers, rewrites, cleanUrls, trailingSlash, ignore), uploads the gzipped files and
releases the version, authenticated with the HOSTING-ONLY identity's short-lived access token
(google-github-actions/auth `token_format: access_token`, env HOSTING_ACCESS_TOKEN). That identity holds
roles/firebasehosting.admin only.

    HOSTING_ACCESS_TOKEN=... python3 infra/hosting/deploy_hosting.py --site aijalon-trade-prod \\
        --public web/dist --config firebase.json --message "deploy abc123"
    python3 infra/hosting/deploy_hosting.py ... --dry-run      # print the version config + file list, no network

API (v1beta1): sites.versions.create → versions.populateFiles → upload each required gzip by sha256 →
versions.patch(status=FINALIZED) → sites.releases.create. Test: infra/hosting/test_deploy_hosting.py (fake server).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://firebasehosting.googleapis.com/v1beta1"
BATCH = 1000


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Firebase/superstatic-style glob on paths relative to the public dir (no leading slash)."""
    g = glob.lstrip("/")
    out = ""
    i = 0
    while i < len(g):
        c = g[i]
        if g.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if g.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else ("[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("^" + out + "$")


def hosting_config(fb: dict[str, Any]) -> dict[str, Any]:
    """firebase.json "hosting" → REST ServingConfig (headers / rewrites / cleanUrls / trailingSlashBehavior)."""
    cfg: dict[str, Any] = {}
    headers = []
    for rule in fb.get("headers", []):
        h = {"headers": {x["key"]: x["value"] for x in rule["headers"]}}
        if "regex" in rule:
            h["regex"] = rule["regex"]
        else:
            h["glob"] = rule["source"]
        headers.append(h)
    if headers:
        cfg["headers"] = headers
    rewrites = []
    for rule in fb.get("rewrites", []):
        if "destination" not in rule:
            raise SystemExit(f"unsupported rewrite (only destination rewrites): {rule}")
        r = {"path": rule["destination"]}
        if "regex" in rule:
            r["regex"] = rule["regex"]
        else:
            r["glob"] = rule["source"]
        rewrites.append(r)
    if rewrites:
        cfg["rewrites"] = rewrites
    if fb.get("redirects"):
        raise SystemExit("redirects are not supported by this deployer yet")
    if "cleanUrls" in fb:
        cfg["cleanUrls"] = bool(fb["cleanUrls"])
    if fb.get("trailingSlash") is True:
        cfg["trailingSlashBehavior"] = "ADD"
    elif fb.get("trailingSlash") is False:
        cfg["trailingSlashBehavior"] = "REMOVE"
    return cfg


def collect(public: Path, ignore: list[str]) -> dict[str, tuple[str, bytes]]:
    """{"/path": (sha256 of gzip, gzip bytes)} for every file not ignored. gzip mtime=0 → deterministic hashes."""
    pats = [glob_to_regex(p) for p in ignore]
    files: dict[str, tuple[str, bytes]] = {}
    for f in sorted(public.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(public).as_posix()
        if any(p.match(rel) for p in pats):
            continue
        gz = gzip.compress(f.read_bytes(), compresslevel=9, mtime=0)
        files["/" + rel] = (hashlib.sha256(gz).hexdigest(), gz)
    return files


class Api:
    def __init__(self, token: str, base: str = API) -> None:
        self.token, self.base = token, base.rstrip("/")

    def call(self, method: str, url: str, body: Any = None, *, raw: bytes | None = None,
             ctype: str = "application/json") -> dict[str, Any]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url if url.startswith("http") else f"{self.base}/{url}", data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out = r.read()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {url}: HTTP {e.code} {e.read()[:400]!r}") from None
        return json.loads(out) if out else {}


def deploy(api: Api, site: str, public: Path, fb: dict[str, Any], message: str) -> str:
    cfg = hosting_config(fb)
    files = collect(public, fb.get("ignore", []))
    if "/index.html" not in files:
        raise SystemExit("refusing to deploy: no index.html in the public directory")
    version = api.call("POST", f"sites/{site}/versions", {"config": cfg, "labels": {"deployment-tool": "aijalon-rest"}})["name"]
    print(f"version {version}: {len(files)} files")
    by_hash = {h: gz for h, gz in files.values()}
    items = sorted(files.items())
    upload_url = ""
    required: set[str] = set()
    for i in range(0, len(items), BATCH):
        res = api.call("POST", f"{version}:populateFiles", {"files": {p: h for p, (h, _) in items[i:i + BATCH]}})
        upload_url = res.get("uploadUrl", upload_url)
        required.update(res.get("uploadRequiredHashes", []))
    for h in sorted(required):
        if h not in by_hash:
            raise SystemExit(f"server asked for an unknown hash {h}")
        api.call("POST", f"{upload_url}/{h}", raw=by_hash[h], ctype="application/octet-stream")
    print(f"uploaded {len(required)} new file(s)")
    api.call("PATCH", f"{version}?update_mask=status", {"status": "FINALIZED"})
    rel = api.call("POST", f"sites/{site}/releases?versionName={version}", {"message": message[:512]})
    print(f"released {rel.get('name', '?')}")
    return str(rel.get("name", ""))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--site", required=True)
    ap.add_argument("--public", default="web/dist")
    ap.add_argument("--config", default="firebase.json")
    ap.add_argument("--message", default="deploy")
    ap.add_argument("--api", default=os.environ.get("HOSTING_API_BASE", API))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9-]{3,63}", a.site):
        raise SystemExit("invalid site id")
    fb = json.loads(Path(a.config).read_text())["hosting"]
    public = Path(a.public)
    if a.dry_run:
        print(json.dumps(hosting_config(fb), indent=2))
        for p, (h, _) in sorted(collect(public, fb.get("ignore", [])).items()):
            print(h[:12], p)
        return 0
    token = os.environ.get("HOSTING_ACCESS_TOKEN", "")
    if not token:
        raise SystemExit("HOSTING_ACCESS_TOKEN is required")
    deploy(Api(token, a.api), a.site, public, fb, a.message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

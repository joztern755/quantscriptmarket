#!/usr/bin/env python3
"""Keep the site's Content-Security-Policy identical everywhere it is written down.

Sources of the policy:
  * web/build.mjs            GENERATES it at build time (web/dist/csp.txt; meta tag = same minus
                             frame-ancestors / upgrade-insecure-requests) from web/public/app-config.json
  * infra/csp.txt            the reviewed, committed copy (one line)
  * firebase.json            the HTTP header actually served by Firebase Hosting (must equal infra/csp.txt)
  * backend/app/security/csp.py  its docstring embeds the same string verbatim (describes the policy); NOTE when stale

    python3 infra/csp_sync.py check                 # csp.txt == firebase.json (+ dist if built, + csp.py)
    python3 infra/csp_sync.py check --require-dist  # deploy: the production build must match too
    python3 infra/csp_sync.py write                 # copy web/dist/csp.txt -> infra/csp.txt + firebase.json

A change in the build's policy (new third-party origin, Firebase SRI import-map hash, new project id) makes
`check --require-dist` fail in deploy until someone runs `make csp-sync`, reviews the diff and commits it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "infra" / "csp.txt"
FIREBASE = ROOT / "firebase.json"
DIST = ROOT / "web" / "dist" / "csp.txt"
PYMOD = ROOT / "backend" / "app" / "security" / "csp.py"


def firebase_csp(cfg: dict) -> list[str]:
    out = []
    for rule in cfg["hosting"]["headers"]:
        for h in rule["headers"]:
            if h["key"].lower() == "content-security-policy":
                out.append(h["value"])
    return out


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("check", "write"):
        print(__doc__)
        return 64
    cfg = json.loads(FIREBASE.read_text())
    if argv[0] == "write":
        if not DIST.exists():
            print("web/dist/csp.txt missing — run the production build first (see Makefile: csp-sync)")
            return 1
        new = DIST.read_text().strip()
        CANON.write_text(new + "\n")
        for rule in cfg["hosting"]["headers"]:
            for h in rule["headers"]:
                if h["key"].lower() == "content-security-policy":
                    h["value"] = new
        FIREBASE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
        print("infra/csp.txt and firebase.json updated — review `git diff` and commit")
        return 0

    canon = CANON.read_text().strip()
    rc = 0
    fb = firebase_csp(cfg)
    if not fb:
        print("FAIL firebase.json has no Content-Security-Policy header")
        rc = 1
    for v in fb:
        if v != canon:
            print("FAIL firebase.json CSP != infra/csp.txt")
            rc = 1
    if DIST.exists():
        built = DIST.read_text().strip()
        if built != canon:
            print("MISMATCH web/dist/csp.txt != infra/csp.txt")
            print(f"  built : {built}\n  commit: {canon}")
            if "--require-dist" in argv:
                rc = 1
    elif "--require-dist" in argv:
        print("FAIL web/dist/csp.txt missing (build first)")
        rc = 1
    if PYMOD.exists() and canon not in PYMOD.read_text():
        # Not fatal: the API's own CSP (JSON responses) may legitimately be stricter; flag for review.
        print("NOTE backend/app/security/csp.py does not embed infra/csp.txt verbatim — confirm intended")
    if rc == 0:
        print("CSP consistent")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

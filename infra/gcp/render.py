#!/usr/bin/env python3
"""Render a template by substituting ${VAR} from the environment. Stdlib only; used by deploy.yml.

Fails (exit 2) if any referenced variable is unset or empty, or if an image reference is not pinned by
digest — a half-rendered Cloud Run spec must never reach `gcloud run services replace`.

    python3 infra/gcp/render.py infra/gcp/run/api.service.yaml > /tmp/api.yaml
"""
from __future__ import annotations

import os
import re
import sys

VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")
IMAGE_LINE = re.compile(r"^\s*image:\s*(\S+)\s*$")


def render(text: str) -> str:
    missing = sorted({m for m in VAR.findall(text) if not os.environ.get(m)})
    if missing:
        sys.stderr.write(f"render: unset/empty variables: {', '.join(missing)}\n")
        sys.exit(2)
    out = VAR.sub(lambda m: os.environ[m.group(1)], text)
    for line in out.splitlines():
        m = IMAGE_LINE.match(line)
        if m and not re.search(r"@sha256:[0-9a-f]{64}$", m.group(1)):
            sys.stderr.write(f"render: image not pinned by digest: {m.group(1)}\n")
            sys.exit(2)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write(__doc__ or "")
        sys.exit(64)
    with open(sys.argv[1], encoding="utf-8") as fh:
        sys.stdout.write(render(fh.read()))

#!/usr/bin/env python3
"""Render a template by substituting ${VAR} from the environment. Stdlib only; used by deploy.yml.

Syntax (nothing else is interpreted):
  ${NAME}             required: exit 2 if NAME is unset or empty
  ${NAME:-default}    optional: NAME's value, else the literal default (may be empty, e.g. ${STRIPE_API_VERSION:-})
  #@if NAME=value     (a line of its own, any indentation) keeps the lines up to the matching `#@endif` only when
  #@endif             NAME == value (NAME unset/empty compares as ""); no nesting. Used for secrets that exist
                      only when a feature is on (KYC_* when KYC_PROVIDER=sumsub): a Cloud Run secretKeyRef to a
                      secret without a version would make the revision fail.

Fails (exit 2) if any required variable is unset or empty, if an `${` survives rendering, if an #@if block is
malformed, or if an image reference is not pinned by digest — a half-rendered Cloud Run spec must never reach
`gcloud run services replace`.

    python3 infra/gcp/render.py infra/gcp/run/api.service.yaml > /tmp/api.yaml
"""
from __future__ import annotations

import os
import re
import sys

VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
IMAGE_LINE = re.compile(r"^\s*image:\s*(\S+)\s*$")
IF_LINE = re.compile(r"^\s*#@if\s+([A-Z0-9_]+)=(\S*)\s*$")
ENDIF_LINE = re.compile(r"^\s*#@endif\s*$")


def _fail(msg: str) -> None:
    sys.stderr.write(f"render: {msg}\n")
    sys.exit(2)


def _conditionals(text: str) -> str:
    out: list[str] = []
    keep: bool | None = None          # None = outside a block
    for n, line in enumerate(text.splitlines(keepends=True), 1):
        m = IF_LINE.match(line)
        if m:
            if keep is not None:
                _fail(f"line {n}: nested #@if")
            keep = os.environ.get(m.group(1), "") == m.group(2)
            continue
        if ENDIF_LINE.match(line):
            if keep is None:
                _fail(f"line {n}: #@endif without #@if")
            keep = None
            continue
        if keep is None or keep:
            out.append(line)
    if keep is not None:
        _fail("unterminated #@if")
    return "".join(out)


def render(text: str) -> str:
    text = _conditionals(text)
    missing = sorted({m.group(1) for m in VAR.finditer(text)
                      if m.group(2) is None and not os.environ.get(m.group(1))})
    if missing:
        _fail(f"unset/empty variables: {', '.join(missing)}")
    out = VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), text)
    if "${" in out:
        _fail("unrendered '${' left in the output (bad placeholder syntax?)")
    for line in out.splitlines():
        m = IMAGE_LINE.match(line)
        if m and not re.search(r"@sha256:[0-9a-f]{64}$", m.group(1)):
            _fail(f"image not pinned by digest: {m.group(1)}")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write(__doc__ or "")
        sys.exit(64)
    with open(sys.argv[1], encoding="utf-8") as fh:
        sys.stdout.write(render(fh.read()))

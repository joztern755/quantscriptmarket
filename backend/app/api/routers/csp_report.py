"""POST /v1/csp-report — Content-Security-Policy / Trusted Types violation reports from the web app (SECURITY M2).

The Hosting CSP header carries ``report-uri https://api.aijalon.trade/v1/csp-report; report-to csp`` and the
``Reporting-Endpoints: csp="…/v1/csp-report"`` header (firebase.json, web/build.mjs). Browsers POST either the legacy
``application/csp-report`` body ``{"csp-report": {...}}`` or Reporting-API ``application/reports+json`` lists.

Anonymous by design (browsers send no credentials), so it is treated as hostile input:
  * per-IP limit (20/min) + a process-wide ceiling (600/min) — Cloudflare's per-IP rate limit sits in front;
  * body ≤ 16 KiB, at most 5 reports per request, JSON only;
  * only a fixed set of fields is kept, URLs are reduced to scheme://host/path (no query / fragment: tokens),
    samples (``script-sample`` / ``sample``) are DROPPED (they can contain user data), every string is truncated;
  * the result is one structured WARNING log line per report (log-based metric / alert: infra/gcp/monitoring.py).
Always answers 204 for well-formed requests; nothing is stored in the database.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from app.logging import get_logger

log = get_logger("app.api.csp_report")

__all__ = ["router", "sanitize_reports", "MAX_BODY", "MAX_REPORTS"]

MAX_BODY = 16 * 1024
MAX_REPORTS = 5
_CONTENT_TYPES = ("application/csp-report", "application/reports+json", "application/json")
_DIRECTIVE = re.compile(r"^[a-z][a-z-]{0,40}$")
_KEYWORD = re.compile(r"^(inline|eval|wasm-eval|trusted-types-policy|trusted-types-sink|data|blob|self|about)$")


def _url(v: Any) -> str:
    s = str(v or "")[:2000]
    if not s:
        return ""
    if _KEYWORD.match(s):
        return s
    try:
        p = urlsplit(s)
    except ValueError:
        return "invalid"
    if p.scheme in ("http", "https", "wss", "ws", "chrome-extension", "moz-extension", "safari-web-extension"):
        return f"{p.scheme}://{p.netloc}{p.path}"[:200]
    return (p.scheme or "other")[:20]


def _directive(v: Any) -> str:
    s = str(v or "").strip().split(" ")[0].lower()
    return s if _DIRECTIVE.match(s) else "unknown"


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10_000_000 else None


def _one(body: dict[str, Any], legacy: bool) -> dict[str, Any]:
    g = (lambda a, b: body.get(a)) if legacy else (lambda a, b: body.get(b))
    return {
        "document": _url(g("document-uri", "documentURL")),
        "directive": _directive(g("effective-directive", "effectiveDirective") or g("violated-directive", "effectiveDirective")),
        "blocked": _url(g("blocked-uri", "blockedURL")),
        "source": _url(g("source-file", "sourceFile")),
        "line": _int(g("line-number", "lineNumber")),
        "column": _int(g("column-number", "columnNumber")),
        "disposition": "report" if str(g("disposition", "disposition") or "") == "report" else "enforce",
        "status": _int(g("status-code", "statusCode")),
    }


def sanitize_reports(payload: Any) -> list[dict[str, Any]]:
    """Legacy ``{"csp-report": {...}}`` or a Reporting-API list → at most MAX_REPORTS minimal, sanitized dicts."""
    out: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("csp-report"), dict):
        out.append(_one(payload["csp-report"], legacy=True))
    elif isinstance(payload, list):
        for item in payload[:MAX_REPORTS]:
            if isinstance(item, dict) and item.get("type") == "csp-violation" and isinstance(item.get("body"), dict):
                out.append(_one(item["body"], legacy=False))
    return out[:MAX_REPORTS]


# ------------------------------------------------------------------------------ route (FastAPI) — pure code above
from fastapi import APIRouter, Depends, Request, Response  # noqa: E402

from app.api.deps import Services, get_services, ip_limit  # noqa: E402
from app.errors import RateLimited, ValidationFailed  # noqa: E402

router = APIRouter(tags=["csp"])


@router.post("/csp-report", status_code=204, response_class=Response,
             dependencies=[ip_limit("csp_report", 20, 60)])
async def csp_report(request: Request, svc: Services = Depends(get_services)) -> Response:
    if not svc.ratelimit.hit("csp_report:global", 600, 60):
        raise RateLimited("too many reports", retry_after_seconds=60)
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in _CONTENT_TYPES:
        raise ValidationFailed("unsupported report content type")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValidationFailed("report too large")
    try:
        payload = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        raise ValidationFailed("report is not JSON") from None
    for rep in sanitize_reports(payload):
        log.warning("csp_violation", extra={"fields": rep})
    return Response(status_code=204)

"""Pure-ASGI middleware (no BaseHTTPMiddleware: no body re-streaming bugs, works with raw-body webhooks).

Order (outermost first): RequestContext → CORS → BodyLimit → EdgeGuard → app.

Trusting Cloudflare's CF-IPCountry / CF-Connecting-IP (SPEC §5.7 jurisdiction gate)
------------------------------------------------------------------------------------
Those headers are just request headers: anyone who can reach the Cloud Run URL directly can forge them. They are
trusted ONLY when the request proves it came through our Cloudflare zone:
  1. Cloud Run ingress = "internal-and-cloud-load-balancing"; public traffic enters through an external HTTPS
     load balancer whose Cloud Armor policy allows only Cloudflare's published IP ranges (so *.run.app and the
     LB IP are not reachable by the public; Cloud Scheduler still reaches the service as internal traffic).
  2. A Cloudflare Transform Rule (Modify Request Header) on api.aijalon.trade sets `X-Edge-Auth: <secret>`
     (Secret Manager → EDGE_AUTH_SECRET, rotated by adding the new value to Cloudflare first). Cloudflare
     overwrites any client-supplied value.
  3. This middleware compares X-Edge-Auth in constant time. Only then are CF-IPCountry / CF-Connecting-IP used.
     In prod, /v1 requests without a valid X-Edge-Auth are refused (403 edge_required) — fail closed.
Exempt: /healthz, /v1/internal/* (Google OIDC from Cloud Scheduler, no Cloudflare hop) and /v1/webhooks/stripe
(HMAC-signed by Stripe; Stripe's servers are in the US, which is itself a restricted country).
Tor exit traffic (CF-IPCountry "T1") is treated as restricted because its jurisdiction cannot be determined.
"""
from __future__ import annotations

import hmac
import json
import time
import uuid
from typing import Any, Awaitable, Callable

from starlette.datastructures import Headers, MutableHeaders

from app.api import validation as v
from app.logging import get_logger

log = get_logger("app.api.access")

ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]

_FALLBACK_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "Cross-Origin-Resource-Policy": "same-site",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), browsing-topics=()",
    "Cache-Control": "no-store",
}


def security_headers(*, cacheable_public: bool) -> dict[str, str]:
    try:
        from app.security.csp import api_security_headers
        return api_security_headers(cacheable_public=cacheable_public)
    except ImportError:
        h = dict(_FALLBACK_HEADERS)
        if cacheable_public:
            h["Cache-Control"] = "public, max-age=60"
        return h


async def send_json_error(send: Callable[[dict], Awaitable[None]], status: int, code: str, message: str,
                          request_id: str = "", extra_headers: dict[str, str] | None = None) -> None:
    body = json.dumps({"error": {"code": code, "message": message}, "request_id": request_id or None}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    for k, val in (extra_headers or {}).items():
        headers.append((k.lower().encode(), val.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class RequestContextMiddleware:
    """Request id (validated inbound X-Request-ID or a new one), security headers on EVERY response (including
    errors produced by inner middleware), and a structured access log line (no query strings, no bodies)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        inbound = headers.get("x-request-id", "")
        rid = inbound if v.REQUEST_ID_RE.match(inbound or "") else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        path, method = scope.get("path", ""), scope.get("method", "")
        start = time.perf_counter()
        status_box = {"status": 500}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_box["status"] = message["status"]
                h = MutableHeaders(scope=message)
                cacheable = method == "GET" and path.startswith("/v1/public/") and message["status"] == 200
                for k, val in security_headers(cacheable_public=cacheable).items():
                    if k.lower() == "cache-control" and "cache-control" in h and not cacheable:
                        continue
                    h[k] = val
                h["X-Request-ID"] = rid
                if "server" in h:
                    del h["server"]
            await send(message)

        started = {"v": False}

        async def tracking_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                started["v"] = True
            await send_wrapper(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:  # noqa: BLE001 - last line of defence: generic 500, never a stack trace
            log.error("unhandled error", exc_info=exc, extra={"fields": {"request_id": rid, "path": path[:200]}})
            if started["v"]:
                raise
            await send_json_error(tracking_send, 500, "internal_error", "internal error", rid)
        finally:
            state = scope.get("state", {})
            log.info("request", extra={"fields": {
                "request_id": rid, "method": method, "path": path[:200], "status": status_box["status"],
                "ms": int((time.perf_counter() - start) * 1000), "user_id": state.get("user_id"),
                "country": state.get("edge_country")}})


class BodyLimitMiddleware:
    """Hard request-size limit. Bodies are buffered (≤ limit) then replayed, so an oversize body is refused
    with 413 before any route or JSON parser sees it (FastAPI would otherwise turn it into a 400)."""

    def __init__(self, app: ASGIApp, *, default_limit: int, upload_limit: int,
                 upload_prefixes: tuple[str, ...] = ("/v1/creator/", "/v1/webhooks/")) -> None:
        self.app = app
        self.default_limit = default_limit
        self.upload_limit = upload_limit
        self.upload_prefixes = upload_prefixes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        limit = self.upload_limit if path.startswith(self.upload_prefixes) else self.default_limit
        rid = scope.get("state", {}).get("request_id", "")
        cl = Headers(scope=scope).get("content-length")
        if cl is not None and (not cl.isdigit() or int(cl) > limit):
            await send_json_error(send, 413, "payload_too_large", f"request body exceeds {limit} bytes", rid)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > limit:
                await send_json_error(send, 413, "payload_too_large", f"request body exceeds {limit} bytes", rid)
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class EdgeGuardMiddleware:
    EXEMPT_PREFIXES = ("/v1/internal/",)
    EXEMPT_PATHS = ("/healthz", "/v1/webhooks/stripe")

    def __init__(self, app: ASGIApp, *, get_services: Callable[[], Any]) -> None:
        self.app = app
        self.get_services = get_services

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        svc = self.get_services()
        cfg, settings = svc.config, svc.settings
        headers = Headers(scope=scope)
        state = scope.setdefault("state", {})
        secret = cfg.edge_auth_secret
        presented = headers.get("x-edge-auth", "")
        trusted = bool(secret) and hmac.compare_digest(presented.encode(), secret.encode())
        country = None
        if trusted:
            c = headers.get("cf-ipcountry", "").strip().upper()
            if v.COUNTRY_RE.match(c):
                country = c
        state["edge_trusted"] = trusted
        state["edge_country"] = country
        path = scope.get("path", "")
        if path in self.EXEMPT_PATHS or path.startswith(self.EXEMPT_PREFIXES) or not path.startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        rid = state.get("request_id", "")
        if settings.is_prod and not trusted:
            await send_json_error(send, 403, "edge_required", "requests must come through aijalon.trade", rid)
            return
        if country and (country in settings.restricted_countries or country == "T1"):
            await send_json_error(send, 451, "jurisdiction_restricted",
                                  "aijalon.trade is not available in your jurisdiction", rid)
            return
        await self.app(scope, receive, send)

"""Sandbox HTTP service (Cloud Run "sandbox", SPEC §2). Stdlib only: ``http.server``.

Endpoints (JSON in, JSON out; errors → ``{"error": code, "message": ..., "details": {...}}``):

* ``GET  /healthz``                      → ``{"ok": true}`` (no auth; carries no data)
* ``POST /validate``   ``{source, known_markets?, platform_max_leverage?}`` → ValidationResult
* ``POST /run``        ``{source, bars, now_ms?}`` → ``{weights, cpu_seconds, code_hash}``
  (live: the executor calls this once per strategy version per bar close)
* ``POST /backtest``   ``{source, data: MarketData, params?}`` → backtest report. The sandbox has **no
  egress**, so the caller (api) fetches candles/funding with ``backtest.fetch_market_data`` and sends them.
* ``POST /nocode/compile`` ``{spec}`` → ``{source, meta, code_hash}``

Auth, two layers:

1. **Prod: Cloud Run IAM.** Deploy with ``--no-allow-unauthenticated --ingress internal`` and grant
   ``roles/run.invoker`` on this service ONLY to the ``api`` and ``executor`` service accounts. Callers
   send a Google-signed OIDC ID token (audience = service URL); Cloud Run's front end verifies it before
   the request reaches this process.
2. **Shared secret** header ``X-Sandbox-Secret`` (constant-time compare), from Secret Manager mounted as
   the env var ``SANDBOX_SHARED_SECRET``. Defense in depth, and the only auth for local runs.
   The service refuses to start without it.

Source code is never logged (creator IP): logs carry a 16-hex prefix of the SHA-256 code hash, timings, outcome.
Env is read only in :func:`main` (this entrypoint runs in its own container without ``app.config``).

Recommended Cloud Run flags (see sandbox/Dockerfile): ``--execution-environment gen1`` (gVisor),
``--service-account sandbox@…`` (no roles), ``--network … --subnet … --vpc-egress all-traffic`` into a
VPC with no NAT and a deny-all egress firewall, ``--concurrency 4 --cpu 2 --memory 2Gi``,
``--max-instances`` small, ``--timeout 300``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from app.errors import AppError, RateLimited, Unauthorized, ValidationFailed
from app.logging import get_logger
from app.sandbox import backtest as bt
from app.sandbox import nocode, runner, validate

__all__ = ["make_server", "SandboxHandler", "main", "MAX_BODY_BYTES", "SECRET_HEADER"]

MAX_BODY_BYTES = 16 * 1024 * 1024
SECRET_HEADER = "X-Sandbox-Secret"
log = get_logger("sandbox.service")


def _reject_constant(c: str) -> Any:
    raise ValueError(f"{c} is not valid JSON")


def _code_hash(source: Any) -> str:
    return hashlib.sha256(source.encode("utf-8", "replace")).hexdigest() if isinstance(source, str) else ""


def _need_source(body: dict[str, Any]) -> str:
    src = body.get("source")
    if not isinstance(src, str):
        raise ValidationFailed("'source' (string) is required")
    return src


def h_validate(body: dict[str, Any]) -> dict[str, Any]:
    src = _need_source(body)
    km = body.get("known_markets")
    if km is not None and (not isinstance(km, list) or not all(isinstance(x, str) for x in km)):
        raise ValidationFailed("known_markets must be a list of strings")
    kw: dict[str, Any] = {"known_markets": km}
    if "platform_max_leverage" in body:
        v = body["platform_max_leverage"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (1 <= v <= 50):
            raise ValidationFailed("platform_max_leverage must be a number 1–50")
        kw["platform_max_leverage"] = v
    return validate.validate_source(src, **kw).to_dict()


def h_run(body: dict[str, Any]) -> dict[str, Any]:
    src = _need_source(body)
    bars = body.get("bars")
    if not isinstance(bars, dict):
        raise ValidationFailed("'bars' must be an object {coin: [bar, ...]}")
    now_ms = body.get("now_ms")
    if now_ms is not None and (isinstance(now_ms, bool) or not isinstance(now_ms, int)):
        raise ValidationFailed("now_ms must be an integer")
    res = runner.run_signal(src, bars, now_ms=now_ms)
    return {"weights": res.weights, "cpu_seconds": res.cpu_seconds, "code_hash": _code_hash(src)}


def h_backtest(body: dict[str, Any]) -> dict[str, Any]:
    src = _need_source(body)
    data = bt.MarketData.from_json(body.get("data") or {})
    params = bt.BacktestParams.from_dict(body.get("params"))
    report = bt.backtest_on_data(src, data, params=params)
    report["code_hash"] = _code_hash(src)
    return report


def h_nocode(body: dict[str, Any]) -> dict[str, Any]:
    src = nocode.compile_spec(body.get("spec"))
    res = validate.validate_source(src)
    return {"source": src, "meta": res.meta.to_dict() if res.meta else None, "code_hash": res.code_hash}


ROUTES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "/validate": h_validate, "/run": h_run, "/backtest": h_backtest, "/nocode/compile": h_nocode,
}


class SandboxHandler(BaseHTTPRequestHandler):
    server_version = "aijalon-sandbox"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # per-socket-operation timeout (slowloris)

    # set by make_server
    secret: bytes = b""
    slots: threading.BoundedSemaphore

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default stderr logging
        pass

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, e: AppError) -> None:
        details = e.details if isinstance(e.details, dict) else {}
        try:
            json.dumps(details, allow_nan=False)
        except (TypeError, ValueError):
            details = {}
        self._send(e.http_status, {"error": e.code, "message": e.message, "details": details})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not_found", "message": "not found", "details": {}})

    def do_POST(self) -> None:  # noqa: N802
        t0 = time.monotonic()
        route = ROUTES.get(self.path)
        try:
            given = self.headers.get(SECRET_HEADER, "").encode()
            if not self.secret or not hmac.compare_digest(given, self.secret):
                raise Unauthorized("missing or bad sandbox secret")
            if route is None:
                self.close_connection = True
                self._send(404, {"error": "not_found", "message": "not found", "details": {}})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise ValidationFailed("Content-Length required") from None
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                raise ValidationFailed(f"body too large (max {MAX_BODY_BYTES} bytes)")
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                raise ValidationFailed("body must be valid JSON") from None
            if not isinstance(body, dict):
                raise ValidationFailed("body must be a JSON object")
            if not self.slots.acquire(timeout=5):
                raise RateLimited("sandbox busy, retry later")
            try:
                out = route(body)
            finally:
                self.slots.release()
            self._send(200, out)
            log.info("sandbox request", extra={"fields": {"path": self.path, "status": 200,
                     # 16-hex prefix: the shared log redactor (rightly) masks any 64-hex string as a key
                     "code_hash16": _code_hash(body.get("source"))[:16], "ms": int((time.monotonic() - t0) * 1000)}})
        except AppError as e:
            self._err(e)
            log.info("sandbox request", extra={"fields": {"path": self.path, "status": e.http_status, "error": e.code,
                     "kind": (e.details or {}).get("kind"), "ms": int((time.monotonic() - t0) * 1000)}})
        except Exception as e:  # never leak internals
            log.error("sandbox internal error", extra={"fields": {"path": self.path, "exc": type(e).__name__}})
            self._send(500, {"error": "internal", "message": "internal error", "details": {}})


def make_server(host: str, port: int, secret: str, *, max_concurrent: int = 4) -> ThreadingHTTPServer:
    if not secret or len(secret) < 16:
        raise RuntimeError("SANDBOX_SHARED_SECRET must be set (≥ 16 chars)")
    handler = type("BoundSandboxHandler", (SandboxHandler,), {
        "secret": secret.encode(), "slots": threading.BoundedSemaphore(max_concurrent)})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main() -> None:  # pragma: no cover - container entrypoint
    port = int(os.environ.get("PORT", "8080"))
    secret = os.environ.get("SANDBOX_SHARED_SECRET", "")
    conc = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "4"))
    try:
        srv = make_server("0.0.0.0", port, secret, max_concurrent=conc)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    log.info("sandbox listening", extra={"fields": {"port": port, "max_concurrent": conc}})
    srv.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()

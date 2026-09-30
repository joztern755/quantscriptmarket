"""FastAPI app factory for aijalon.trade.

    uvicorn --factory app.api.main:create_app            # public API service (Cloud Run "api")
    uvicorn --factory app.api.main:create_executor_app   # executor service: /healthz + /v1/internal/* incl. tick

The API service account can only ENCRYPT agent keys (KMS); /v1/internal/tick (which needs decrypt) is mounted
only on the executor service. OpenAPI/docs are disabled in prod. Errors never include stack traces.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import Services
from app.api.middleware import BodyLimitMiddleware, EdgeGuardMiddleware, RequestContextMiddleware
from app.config import get_settings
from app.errors import AppError, RateLimited
from app.logging import get_logger

log = get_logger("app.api")

_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "method_not_allowed", 406: "not_acceptable", 409: "conflict", 413: "payload_too_large",
               415: "unsupported_media_type", 429: "rate_limited"}
_MAX_DETAILS_BYTES = 4096


def _request_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None)


def _public_details(exc: AppError) -> Optional[dict[str, Any]]:
    """Client-visible details for 4xx errors only; JSON-safe and size-bounded. 5xx never carry details."""
    if exc.http_status >= 500 or not exc.details:
        return None
    try:
        raw = json.dumps(exc.details, default=str)
    except (TypeError, ValueError):
        return None
    if len(raw) > _MAX_DETAILS_BYTES:
        return None
    return json.loads(raw)


def _error(status: int, code: str, message: str, request: Request, details: Optional[dict] = None,
           headers: Optional[dict[str, str]] = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status, content={"error": body, "request_id": _request_id(request)},
                        headers=headers)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.http_status >= 500:
            log.warning("app error", extra={"fields": {"code": exc.code, "request_id": _request_id(request),
                                                       "path": request.url.path}})
        headers = None
        if isinstance(exc, RateLimited):
            retry = exc.details.get("retry_after_seconds") or exc.details.get("retry_after")
            if isinstance(retry, int) and retry > 0:
                headers = {"Retry-After": str(retry)}
        message = exc.message if exc.http_status < 500 else "service temporarily unavailable"
        return _error(exc.http_status, exc.code, message, request, _public_details(exc), headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo input values back (they may contain secrets/PII): location + message only.
        fields = [{"loc": [str(p) for p in e.get("loc", ())][:6], "msg": str(e.get("msg", ""))[:200]}
                  for e in exc.errors()[:20]]
        return _error(422, "validation_failed", "request validation failed", request, {"fields": fields})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, code.replace("_", " "), request, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", exc_info=exc, extra={"fields": {"request_id": _request_id(request),
                                                                     "path": request.url.path}})
        return _error(500, "internal_error", "internal error", request)


def _check_prod_config(svc: Services) -> None:
    """Fail closed at startup rather than serve prod traffic with a missing security control."""
    s = svc.settings
    if not s.is_prod:
        return
    cfg = svc.config
    missing = [name for name, ok in (
        ("EDGE_AUTH_SECRET (edge_auth_secret)", len(cfg.edge_auth_secret) >= 32),
        ("SCHEDULER_SA_EMAIL (scheduler_sa_email)", bool(cfg.scheduler_sa_email)),
        ("AUDIT_PEPPER_B64 (audit_pepper_b64)", len(cfg.pepper) >= 32),
        ("STRIPE_WEBHOOK_SECRET", bool(s.stripe_webhook_secret)),
        ("SANDBOX_URL (sandbox_url)", bool(cfg.sandbox_url) or not s.feature_creator_uploads),
    ) if not ok]
    if missing:
        raise RuntimeError(f"prod API config missing: {missing}")
    if not s.web_origin.startswith("https://"):
        raise RuntimeError("WEB_ORIGIN must be https in prod")


def _build(services: Optional[Services], role: str) -> FastAPI:
    if services is None:
        from app.api.adapters import build_services
        services = build_services(get_settings())
    _check_prod_config(services)
    prod = services.settings.is_prod
    app = FastAPI(
        title="aijalon.trade API",
        version="1",
        docs_url=None if prod else "/docs",
        redoc_url=None,
        openapi_url=None if prod else "/openapi.json",
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.services = services
    _install_error_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    from app.api.routers import internal
    if role == "executor":
        app.include_router(internal.router, prefix="/v1")
        app.include_router(internal.executor_router, prefix="/v1")
    else:
        from app.api.routers import (
            admin,
            agents,
            alerts,
            balance,
            consents,
            creator,
            deposits,
            me,
            positions,
            posts,
            public,
            referrals,
            reviews,
            subscriptions,
            wallets,
            webhooks,
            withdrawals,
        )
        for r in (public, me, consents, wallets, agents, subscriptions, balance, deposits, withdrawals, positions,
                  alerts, reviews, posts, referrals, creator, admin, internal, webhooks):
            app.include_router(r.router, prefix="/v1")

    cfg = services.config
    # add_middleware prepends: the LAST added is the OUTERMOST.
    app.add_middleware(EdgeGuardMiddleware, get_services=lambda: app.state.services)
    app.add_middleware(BodyLimitMiddleware, default_limit=cfg.max_body_bytes, upload_limit=cfg.max_upload_body_bytes)
    if role != "executor":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[services.settings.web_origin],
            allow_credentials=False,               # Bearer tokens, never cookies
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Ref-Code", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Idempotent-Replayed", "Retry-After"],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware)
    return app


def create_app(services: Optional[Services] = None) -> FastAPI:
    return _build(services, "api")


def create_executor_app(services: Optional[Services] = None) -> FastAPI:
    return _build(services, "executor")

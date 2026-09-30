"""Typed errors. The API layer maps these to HTTP status codes."""
from __future__ import annotations


class AppError(Exception):
    http_status = 400
    code = "bad_request"

    def __init__(self, message: str = "", **details):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details


class NotFound(AppError):
    http_status, code = 404, "not_found"


class Unauthorized(AppError):
    http_status, code = 401, "unauthorized"


class Forbidden(AppError):
    http_status, code = 403, "forbidden"


class StepUpRequired(AppError):
    """Fresh sign-in + MFA needed for a financial/security action."""
    http_status, code = 401, "step_up_required"


class ConsentRequired(AppError):
    http_status, code = 403, "consent_required"


class Conflict(AppError):
    http_status, code = 409, "conflict"


class InsufficientBalance(AppError):
    http_status, code = 402, "insufficient_balance"


class GuardRejected(AppError):
    """A pre-trade risk guard refused an order (fail closed)."""
    http_status, code = 422, "guard_rejected"


class KillSwitchActive(AppError):
    http_status, code = 503, "kill_switch_active"


class ValidationFailed(AppError):
    http_status, code = 422, "validation_failed"


class ExternalServiceError(AppError):
    http_status, code = 502, "external_service_error"


class RateLimited(AppError):
    http_status, code = 429, "rate_limited"

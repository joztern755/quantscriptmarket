"""Alerts: severity-routed notifications (in-app, email, Telegram ops) with dedupe, rate limits and auto-pause."""
from __future__ import annotations

from app.alerts.notifier import (
    Alert,
    EmailProvider,
    EmailSink,
    InAppSink,
    Notifier,
    NotifyResult,
    PermanentSinkError,
    ResendProvider,
    SendGridProvider,
    Severity,
    TelegramSink,
    coerce_alert,
    render,
    TransientSinkError,
    mask_address,
    sanitize_text,
)

__all__ = [
    "Alert",
    "EmailProvider",
    "EmailSink",
    "InAppSink",
    "Notifier",
    "NotifyResult",
    "PermanentSinkError",
    "ResendProvider",
    "SendGridProvider",
    "Severity",
    "TelegramSink",
    "coerce_alert",
    "render",
    "TransientSinkError",
    "mask_address",
    "sanitize_text",
]

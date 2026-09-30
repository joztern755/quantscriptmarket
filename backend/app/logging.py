"""Structured JSON logging with secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

# 64-hex private keys (with/without 0x), bearer tokens, Stripe secrets, long base64 blobs.
_REDACT = [
    re.compile(r"(?i)\b(0x)?[0-9a-f]{64}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"\b(sk|rk|whsec)_(live|test)?_?[A-Za-z0-9]{8,}\b"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]


def redact(text: str) -> str:
    for pat in _REDACT:
        text = pat.sub("[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload["fields"] = json.loads(redact(json.dumps(extra, default=str)))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.handlers[:] = [h]
        root.setLevel(logging.INFO)
        _configured = True
    return logging.getLogger(name)

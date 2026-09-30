"""Data jobs behind /v1/internal/* (owner: app/jobs_data; tables in migrations/0006_data.sql).

Every job is ``fn(db=<DatabasePort>, now=<aware UTC datetime>, **params) -> dict`` (JSON-safe summary), idempotent,
bounded per call and resumable through ``job_cursors``:

  candles_sync        /internal/candles-sync       app.jobs_data.candles.candles_sync
  fills_ingest        /internal/fills-ingest       app.jobs_data.fills.fills_ingest
  funding_scan        /internal/funding-scan       app.jobs_data.funding.funding_scan
  agent_expiry_scan   /internal/agent-expiry-scan  app.jobs_data.agents.agent_expiry_scan
  deposits scan       /internal/deposits-scan      app.hl.deposits.scan → app.jobs_data.deposits.deposits_scan
  signals ingest      /internal/ingest-signals     app.strategies.signals.ingest → app.jobs_data.signals.ingest

Events for users and ops go to ``events_outbox`` (delivered by app/alerts).
"""
from __future__ import annotations

from typing import Any

__all__ = ["candles_sync", "fills_ingest", "funding_scan", "agent_expiry_scan", "deposits_scan", "signals_ingest",
           "JOBS"]


def candles_sync(db: Any, now: Any, **params: Any) -> dict[str, Any]:
    from app.jobs_data.candles import candles_sync as fn

    return fn(db, now, **params)


def fills_ingest(db: Any, now: Any, **params: Any) -> dict[str, Any]:
    from app.jobs_data.fills import fills_ingest as fn

    return fn(db, now, **params)


def funding_scan(db: Any, now: Any, **params: Any) -> dict[str, Any]:
    from app.jobs_data.funding import funding_scan as fn

    return fn(db, now, **params)


def agent_expiry_scan(db: Any, now: Any, **params: Any) -> dict[str, Any]:
    from app.jobs_data.agents import agent_expiry_scan as fn

    return fn(db, now, **params)


def deposits_scan(db: Any, now: Any, **params: Any) -> dict[str, Any]:
    from app.jobs_data.deposits import deposits_scan as fn

    return fn(db, now, **params)


def signals_ingest(db: Any, now: Any, **params: Any) -> dict[str, Any]:
    from app.jobs_data.signals import ingest as fn

    return fn(db, now, **params)


#: internal route name → (module, function) — the shape of app.api.adapters.JOB_ENTRYPOINTS.
JOBS: dict[str, tuple[str, str]] = {
    "candles-sync": ("app.jobs_data", "candles_sync"),
    "fills-ingest": ("app.jobs_data", "fills_ingest"),
    "funding-scan": ("app.jobs_data", "funding_scan"),
    "agent-expiry-scan": ("app.jobs_data", "agent_expiry_scan"),
}

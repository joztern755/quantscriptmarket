"""SQL for the final clean-up round (migrations/0014_cleanup.sql). Mixed into ``app.api.store.SqlStore`` (same
conventions as store_security.py: SQLAlchemy ``text()`` bound parameters only, plain dicts back).

Admin strategy pause (SPEC §12 "strategy paused" mandatory alert; REVIEW_MONEY H5 sibling): while a strategy is
``paused`` no new subscription starts, no renewal is charged, the executor opens nothing (exits run), and every live
subscriber is told. On unpause each live subscription whose prepaid period had not ended when the pause began gets
the paused time back, then billing resumes at its pinned price.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

#: subscriptions a strategy pause concerns (told about it); the billable ones get their paused time back on unpause
PAUSE_NOTIFY_STATUSES = ("pending", "active", "past_due", "reduce_only", "paused_user", "closing")
PAUSE_CREDIT_STATUSES = ("active", "past_due", "reduce_only", "paused_user")


class CleanupStoreMixin:
    def mark_strategy_paused(self, conn: Any, strategy_id: str, now: datetime) -> list[dict]:
        """Record when the admin pause began (kept if already set) and return the live subscriptions
        [{id, user_id, status}] to notify."""
        self._exec(conn, """UPDATE strategies SET paused_at = coalesce(paused_at, CAST(:t AS timestamptz))
                             WHERE id = CAST(:id AS uuid) RETURNING id""", t=now, id=strategy_id)
        return self._all(conn, """
            SELECT s.id, s.user_id, s.status::text AS status FROM subscriptions s
             WHERE s.strategy_id = CAST(:id AS uuid) AND s.status::text = ANY(CAST(:st AS text[]))
             ORDER BY s.created_at, s.id""", id=strategy_id, st=list(PAUSE_NOTIFY_STATUSES))

    def resume_strategy_billing(self, conn: Any, strategy_id: str, now: datetime) -> tuple[Optional[datetime],
                                                                                            list[dict]]:
        """Unpause: give every live subscription whose paid period was still running at ``paused_at`` the paused
        time back (current_period_end += now − paused_at), clear paused_at. Returns (paused_at, [{id, user_id,
        current_period_end}] of every live subscription — to notify). No-op (None, …) when not paused."""
        row = self._one(conn, "SELECT paused_at FROM strategies WHERE id = CAST(:id AS uuid) FOR UPDATE",
                        id=strategy_id)
        paused_at = row.get("paused_at") if row else None
        if paused_at is not None:
            self._exec(conn, """
                UPDATE subscriptions
                   SET current_period_end = current_period_end
                                            + (CAST(:now AS timestamptz) - CAST(:p AS timestamptz))
                 WHERE strategy_id = CAST(:id AS uuid) AND status::text = ANY(CAST(:st AS text[]))
                   AND current_period_end IS NOT NULL AND current_period_end > CAST(:p2 AS timestamptz)
                   AND CAST(:now2 AS timestamptz) > CAST(:p3 AS timestamptz)
                RETURNING id""", now=now, p=paused_at, id=strategy_id, st=list(PAUSE_CREDIT_STATUSES), p2=paused_at,
                       now2=now, p3=paused_at)
            self._exec(conn, "UPDATE strategies SET paused_at = NULL WHERE id = CAST(:id AS uuid) RETURNING id",
                       id=strategy_id)
        subs = self._all(conn, """
            SELECT s.id, s.user_id, s.status::text AS status, s.current_period_end FROM subscriptions s
             WHERE s.strategy_id = CAST(:id AS uuid) AND s.status::text = ANY(CAST(:st AS text[]))
             ORDER BY s.created_at, s.id""", id=strategy_id, st=list(PAUSE_NOTIFY_STATUSES))
        return paused_at, subs

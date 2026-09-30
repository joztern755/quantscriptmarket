"""In-memory fakes of the clean-up round store methods (app/api/store_cleanup.py) — TEST-ONLY, mixed into
app.api.testing.FakeStore. Same semantics as the SQL (exercised against a real database in
tests/test_fix_cleanup_db.py)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.api.store_cleanup import PAUSE_CREDIT_STATUSES, PAUSE_NOTIFY_STATUSES


class FakeCleanupStoreMixin:
    w: Any

    def mark_strategy_paused(self, conn: Any, strategy_id: str, now: datetime) -> list[dict]:
        st = self.w.strategies[str(strategy_id)]
        if st.get("paused_at") is None:
            st["paused_at"] = now
        return [{"id": s["id"], "user_id": s["user_id"], "status": s["status"]}
                for s in self.w.subscriptions.values()
                if s["strategy_id"] == str(strategy_id) and s["status"] in PAUSE_NOTIFY_STATUSES]

    def resume_strategy_billing(self, conn: Any, strategy_id: str, now: datetime) -> tuple[Optional[datetime],
                                                                                            list[dict]]:
        st = self.w.strategies[str(strategy_id)]
        paused_at = st.get("paused_at")
        out = []
        for s in self.w.subscriptions.values():
            if s["strategy_id"] != str(strategy_id) or s["status"] not in PAUSE_NOTIFY_STATUSES:
                continue
            end = s.get("current_period_end")
            if (paused_at is not None and s["status"] in PAUSE_CREDIT_STATUSES and end is not None
                    and end > paused_at and now > paused_at):
                s["current_period_end"] = end + (now - paused_at)
            out.append({"id": s["id"], "user_id": s["user_id"], "status": s["status"],
                        "current_period_end": s.get("current_period_end")})
        st["paused_at"] = None
        return paused_at, out

    # routes the clean-up tests exercise that the base fakes did not cover
    def referral_stats(self, conn: Any, user_id: str, since: datetime) -> dict:
        refs = [u for u in self.w.users.values() if str(u.get("referred_by") or "") == str(user_id)]
        return {"total": len(refs), "active": 0, "notional": 0}

    def publish_version(self, conn: Any, version_id: str, now: datetime) -> int:
        v = self.w.versions.get(str(version_id))
        if v is None or v.get("published_at") is not None:
            return 0
        v.update(published_at=now, live_since=now)
        return 1

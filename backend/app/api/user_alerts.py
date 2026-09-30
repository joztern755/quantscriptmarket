"""User-facing alert producers used by several API routers (kinds: app/alerts/prefs.CATALOG; payloads:
app/alerts/user_templates). The `alerts` row is written in the caller's transaction; app.alerts.delivery sends it
(Telegram + email per the email policy). No FastAPI imports (unit-testable)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

__all__ = ["builder_approval_missing", "short_address"]


def short_address(addr: Optional[str]) -> str:
    s = str(addr or "")
    return s[:6] + "…" + s[-4:] if len(s) >= 12 else s


def builder_approval_missing(conn: Any, svc: Any, *, user_id: str, master: str, approved_tenths_bp: int,
                             required_tenths_bp: int, where: str, now: datetime) -> bool:
    """Mandatory ``builder_approval_missing`` alert when the on-chain maxBuilderFee is below what our orders carry
    (SPEC §12). At most one per (user, wallet, UTC day) from the API paths (alerts.dedup_key). Returns True when the
    approval is insufficient (whether or not a new row was written)."""
    if int(approved_tenths_bp) >= int(required_tenths_bp):
        return False
    svc.notifier.notify(conn, user_id=user_id, severity="critical", kind="builder_approval_missing",
                        payload={"master": short_address(master), "approved_tenths_bp": int(approved_tenths_bp),
                                 "required_tenths_bp": int(required_tenths_bp), "where": where},
                        dedup_key=f"builder_approval_missing:{user_id}:{master.lower()}:{now.date().isoformat()}")
    return True

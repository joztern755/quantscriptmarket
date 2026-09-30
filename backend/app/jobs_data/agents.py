"""``agent_expiry_scan(db, now)`` — ``/internal/agent-expiry-scan`` (SPEC §6 agents, §12 alert kinds
"agent approval expiring at 14, 7, 3, 1 days and expired*").

For each ``agent_keys`` row with status ``active`` (grouped by master address — agents belong to the master):
``extraAgents(master)`` → our agent's ``validUntil`` → ``agent_keys.valid_until`` (+ ``valid_until_checked_at``).

Events (``events_outbox``, user-facing, mandatory kinds of app/alerts/prefs.py):
  * ``agent_expiring`` (warn) when fewer than 14 / 7 / 3 / 1 days remain — the most urgent threshold crossed, once
    per (agent, validUntil, threshold); a re-approval that extends validUntil re-arms them.
  * ``agent_expired`` (critical) once validUntil has passed → ``agent_keys.status = 'expired'``.
  * ``agent_revoked`` (critical) when the agent is absent from ``extraAgents`` on ``missing_scans_to_revoke``
    consecutive scans (and its known validUntil has not simply passed) → ``status = 'revoked'``, ``revoked_at``.
    A single miss only raises an ops event (a transient empty answer must not revoke anything).

EXECUTION CONTRACT (the executor/settlement code must honour it; this job never touches subscriptions):
  an expired or revoked agent cannot place ANY order on Hyperliquid (not even reduce-only exits). The executor must
  only trade subscriptions whose user has an ``agent_keys`` row with ``status = 'active'`` AND
  ``(valid_until IS NULL OR valid_until > now())`` for the subscription's ``master_address``
  (``SubscriptionRepo.due_subscriptions`` filter; ``KeyProvider.agent_key`` must refuse other rows). Subscriptions
  stay in their billing status; the user re-approves (new agent via POST /agents) to resume.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.errors import AppError
from app.jobs_data import _db
from app.jobs_data.hl import WeightPacer, make_info_client

__all__ = ["agent_expiry_scan", "THRESHOLD_DAYS", "threshold_for"]

JOB = "agent_expiry"
THRESHOLD_DAYS = (14, 7, 3, 1)
DAY_MS = 86_400_000


def threshold_for(valid_until_ms: int, now_ms: int, thresholds: tuple[int, ...] = THRESHOLD_DAYS) -> Optional[int]:
    """The most urgent threshold (days) already crossed, or None (more than 14 days left / already expired)."""
    left = valid_until_ms - now_ms
    if left <= 0:
        return None
    crossed = [t for t in thresholds if left <= t * DAY_MS]
    return min(crossed) if crossed else None


@dataclass
class AgentReport:
    agents: int = 0
    masters: int = 0
    checked: int = 0
    updated: int = 0
    expiring_events: int = 0
    expired: int = 0
    missing: int = 0
    revoked: int = 0
    errors: list[str] = field(default_factory=list)
    remaining: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["errors"] = self.errors[:20]
        return d


def agent_expiry_scan(db: Any, now: datetime, *, info: Any = None, settings: Any = None, max_masters: int = 300,
                      max_seconds: float = 240.0, weight_per_minute: int = 600, missing_scans_to_revoke: int = 2,
                      pacer: Optional[WeightPacer] = None) -> dict[str, Any]:
    now_ms = _db.now_ms(now)
    info = make_info_client(settings, info=info)
    pacer = pacer or WeightPacer(weight_per_minute, max_seconds=max_seconds)
    report = AgentReport()
    with _db.transaction(db) as conn:
        agents = _db.rows(conn, f"""
            SELECT id::text AS id, user_id::text AS user_id, master_address, agent_address, agent_name,
                   {_db.ts_to_ms('valid_until')} AS valid_until_ms,
                   {_db.ts_to_ms('valid_until_checked_at')} AS checked_ms
              FROM agent_keys WHERE status = 'active'
             ORDER BY valid_until_checked_at NULLS FIRST, id""")
    report.agents = len(agents)
    by_master: dict[str, list[dict[str, Any]]] = {}
    for a in agents:
        by_master.setdefault(str(a["master_address"]), []).append(a)
    report.masters = len(by_master)
    for i, (master, rows) in enumerate(by_master.items()):
        if report.checked >= max_masters or not pacer.can_start(20):
            report.remaining = len(by_master) - i
            break
        pacer.spend(20)
        try:
            listed = info.extra_agents(master)
        except AppError as e:
            report.errors.append(f"{_db.short_addr(master)}:{type(e).__name__}")
            continue
        report.checked += 1
        on_chain = {str(x.get("address", "")).lower(): x for x in listed if isinstance(x, dict)}
        for a in rows:
            try:
                with _db.transaction(db) as conn:
                    _handle_agent(conn, a, on_chain.get(str(a["agent_address"]).lower()), now_ms,
                                  missing_scans_to_revoke, report)
            except Exception as e:  # noqa: BLE001 - one agent must not block the others
                report.errors.append(f"{a['id'][:8]}:{type(e).__name__}")
                _db.log.error("agent_scan_failed", exc_info=True, extra={"fields": {"agent_id": a["id"]}})
    _db.log.info("agent_expiry_scan_done", extra={"fields": report.as_dict()})
    return report.as_dict()


def _payload(a: dict[str, Any], valid_until_ms: Optional[int], now_ms: int, **extra: Any) -> dict[str, Any]:
    p = {"agent_id": a["id"], "agent": _db.short_addr(a["agent_address"]), "master": _db.short_addr(a["master_address"]),
         "valid_until_ms": valid_until_ms, "reapprove_path": "#/agents"}
    if valid_until_ms is not None:
        p["days_left"] = max(0, (valid_until_ms - now_ms) // DAY_MS)
    p.update(extra)
    return p


def _handle_agent(conn: Any, a: dict[str, Any], chain: Optional[dict[str, Any]], now_ms: int,
                  missing_to_revoke: int, report: AgentReport) -> None:
    aid, uid = a["id"], a["user_id"]
    _, state = _db.get_cursor(conn, JOB, aid)
    known = int(a["valid_until_ms"]) if a.get("valid_until_ms") is not None else None
    if chain is None:
        if known is not None and known <= now_ms:
            _expire(conn, a, known, now_ms, report)
            return
        missing = int(state.get("missing_count") or 0) + 1
        report.missing += 1
        if missing >= missing_to_revoke:
            _db.rows(conn, """UPDATE agent_keys SET status = 'revoked', revoked_at = CAST(:t AS timestamptz),
                                     valid_until_checked_at = CAST(:t2 AS timestamptz)
                               WHERE id = CAST(:id AS uuid) AND status = 'active' RETURNING id""",
                     t=_db.dt_from_ms(now_ms), t2=_db.dt_from_ms(now_ms), id=aid)
            report.revoked += 1
            _db.emit_event(conn, kind="agent_revoked", user_id=uid, severity="critical",
                           payload=_payload(a, known, now_ms, reason="agent approval no longer on-chain"),
                           dedup_key=f"agent_revoked:{aid}")
            _db.ops_alert(conn, "agent_revoked_on_chain", {"agent_id": aid, "user_id": uid, "scans": missing},
                          severity="warn", dedup_key=f"agent_revoked_on_chain:{aid}")
        else:
            _db.ops_alert(conn, "agent_missing_on_chain", {"agent_id": aid, "user_id": uid, "scans": missing},
                          severity="warn", dedup_key=f"agent_missing_on_chain:{aid}:{now_ms // DAY_MS}")
        _db.set_cursor(conn, JOB, aid, now_ms, {"missing_count": missing}, monotonic=False)
        return

    vu = chain.get("validUntil")
    valid_until = int(vu) if isinstance(vu, int) and not isinstance(vu, bool) and vu > 0 else None
    _db.rows(conn, """UPDATE agent_keys SET valid_until = CAST(:vu AS timestamptz),
                             valid_until_checked_at = CAST(:t AS timestamptz)
                       WHERE id = CAST(:id AS uuid) RETURNING id""",
             vu=_db.dt_from_ms(valid_until) if valid_until is not None else None, t=_db.dt_from_ms(now_ms), id=aid)
    report.updated += 1
    _db.set_cursor(conn, JOB, aid, now_ms, {"missing_count": 0, "valid_until_ms": valid_until}, monotonic=False)
    if valid_until is None:
        return
    if valid_until <= now_ms:
        _expire(conn, a, valid_until, now_ms, report)
        return
    t = threshold_for(valid_until, now_ms)
    if t is not None and _db.emit_event(
            conn, kind="agent_expiring", user_id=uid, severity="warn",
            payload=_payload(a, valid_until, now_ms, threshold_days=t),
            dedup_key=f"agent_expiring:{aid}:{valid_until}:{t}"):
        report.expiring_events += 1


def _expire(conn: Any, a: dict[str, Any], valid_until: int, now_ms: int, report: AgentReport) -> None:
    changed = _db.rows(conn, """UPDATE agent_keys SET status = 'expired', valid_until = CAST(:vu AS timestamptz),
                                       valid_until_checked_at = CAST(:t AS timestamptz)
                                 WHERE id = CAST(:id AS uuid) AND status = 'active' RETURNING id""",
                       vu=_db.dt_from_ms(valid_until), t=_db.dt_from_ms(now_ms), id=a["id"])
    if changed:
        report.expired += 1
    _db.emit_event(conn, kind="agent_expired", user_id=a["user_id"], severity="critical",
                   payload=_payload(a, valid_until, now_ms), dedup_key=f"agent_expired:{a['id']}:{valid_until}")

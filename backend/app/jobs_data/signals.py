"""Signal feed ingestion — ``app.strategies.signals.ingest(db, now)`` (``/internal/ingest-signals``; SPEC §7, §10).

For every listed in-house key (``Settings.in_house_listed``; launch: ``silver``): the strategy row is looked up by
slug (= key) and its LATEST ``strategy_versions`` row is the version the signals belong to (seeded as version 1 by the
execution migration). The feed is fetched and verified by ``app.strategies.signals.ingest_signals`` (Ed25519 over the
exact bytes, strict schema, staleness, continuity against the last stored signal of that version) and each record is
stored in ``signals`` (source ``terminal``, raw = the signed JSON, signature) idempotently on
UNIQUE(strategy_version_id, bar_close, coin).

Pinning: when the version's ``params`` carry ``script_sha256`` it is enforced (a different script → rejected).

On rejection nothing is stored; the error's alerts become ops events (``signals_<reason>``) and, for CRITICAL
rejections (bad signature, stale, malformed, …), new entries are paused on the affected market
(``system_flags new_entries_paused:{coin}`` — SPEC §5.5 auto-pause; lifting is maker-checker). A stored bar whose
weight differs from the feed's is a ``signals_conflict`` (critical) — never overwritten.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from app.jobs_data import _db

__all__ = ["ingest"]


def _last_accepted(conn: Any, version_id: str, key: str) -> Optional[Any]:
    from app.strategies.signals import SignalRecord

    r = _db.one(conn, f"""
        SELECT {_db.ts_to_ms('bar_close')} AS bar_ms, coin, target_weight_bps FROM signals
         WHERE strategy_version_id = CAST(:v AS uuid) AND source = 'terminal'
         ORDER BY bar_close DESC, coin LIMIT 1""", v=version_id)
    if r is None:
        return None
    bar = _db.dt_from_ms(int(r["bar_ms"]))
    return SignalRecord(strategy_key=key, bar_close=bar, coin=str(r["coin"]),
                        target_weight_bps=int(r["target_weight_bps"]), as_of=(bar - timedelta(days=1)).date())


def _emit_rejection(conn: Any, error: Any, now: datetime) -> list[str]:
    paused = []
    day = now.astimezone(timezone.utc).date().isoformat()
    for p in error.alert_payloads():
        coin = p.get("coin")
        data = dict(p.get("data") or {})
        if coin:
            data["coin"] = coin
        _db.ops_alert(conn, str(p["kind"]), data, severity=str(p["severity"]),
                      dedup_key=f"{p['key']}:{day}")
        if p.get("severity") == "critical" and coin and _db.pause_market_entries(conn, coin, str(p["kind"])):
            paused.append(coin)
    return paused


def ingest(db: Any, now: datetime, *, settings: Any = None, session: Any = None, url: Optional[str] = None,
           listed_keys: Optional[list[str]] = None, **_ignored: Any) -> dict[str, Any]:
    from app.strategies.signals import ingest_signals

    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    keys = [k.strip().lower() for k in (listed_keys if listed_keys is not None else settings.in_house_listed)
            if k.strip()]
    out: dict[str, Any] = {"ok": False, "stored": 0, "duplicates": 0, "conflicts": 0, "keys": keys}
    versions: dict[str, Mapping[str, Any]] = {}
    last: dict[str, Any] = {}
    pinned: dict[str, str] = {}
    with _db.transaction(db) as conn:
        for key in keys:
            v = _db.one(conn, """
                SELECT v.id::text AS version_id, v.strategy_id::text AS strategy_id, v.version, v.params
                  FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id
                 WHERE s.slug = :slug
                 ORDER BY v.version DESC LIMIT 1""", slug=key)
            if v is None:
                _db.ops_alert(conn, "signals_no_version", {"strategy_key": key}, severity="critical",
                              dedup_key=f"signals_no_version:{key}:{now.date().isoformat()}")
                continue
            versions[key] = v
            rec = _last_accepted(conn, v["version_id"], key)
            if rec is not None:
                last[key] = rec
            sha = _db.jload(v.get("params")).get("script_sha256")
            if isinstance(sha, str) and len(sha) == 64:
                pinned[key] = sha
    if not versions:
        out["error"] = "no strategy version for the listed keys"
        return out

    kwargs: dict[str, Any] = {"now": now, "pubkey_b64": settings.signals_pubkey_b64, "listed_keys": list(versions),
                              "max_age_hours": settings.risk.signal_max_age_hours,
                              "max_bar_age_days": settings.risk.signal_max_bar_age_days,
                              "expected_script_sha256": pinned or None, "last_accepted": last or None}
    if session is not None:
        kwargs["session"] = session
    result = ingest_signals(url or settings.signals_url, **kwargs)
    if not result.ok:
        with _db.transaction(db) as conn:
            paused = _emit_rejection(conn, result.error, now)
        out.update(error=result.error.reason, message=result.error.message[:200], paused_markets=paused)
        _db.log.warning("signals_rejected", extra={"fields": {"reason": result.error.reason}})
        return out

    batch = result.batch
    raw = _db.jdump(batch.payload())
    with _db.transaction(db) as conn:
        for rec in batch.records:
            v = versions.get(rec.strategy_key)
            if v is None:
                continue
            ins = _db.one(conn, """
                INSERT INTO signals (strategy_id, strategy_version_id, bar_close, coin, target_weight_bps, source, raw,
                                     signature, received_at)
                VALUES (CAST(:s AS uuid), CAST(:v AS uuid), CAST(:bar AS timestamptz), :coin, :w, 'terminal',
                        CAST(:raw AS jsonb), :sig, CAST(:now AS timestamptz))
                ON CONFLICT (strategy_version_id, bar_close, coin) DO NOTHING
                RETURNING id""", s=v["strategy_id"], v=v["version_id"], bar=rec.bar_close, coin=rec.coin,
                              w=rec.target_weight_bps, raw=raw, sig=batch.signature_b64, now=now)
            if ins is not None:
                out["stored"] += 1
                continue
            existing = _db.one(conn, """
                SELECT target_weight_bps FROM signals
                 WHERE strategy_version_id = CAST(:v AS uuid) AND bar_close = CAST(:bar AS timestamptz)
                   AND coin = :coin""", v=v["version_id"], bar=rec.bar_close, coin=rec.coin)
            if existing is not None and int(existing["target_weight_bps"]) != rec.target_weight_bps:
                out["conflicts"] += 1
                _db.ops_alert(conn, "signals_conflict", {
                    "strategy_key": rec.strategy_key, "coin": rec.coin, "bar_close": rec.bar_close.isoformat(),
                    "stored_bps": int(existing["target_weight_bps"]), "feed_bps": rec.target_weight_bps},
                    severity="critical", dedup_key=f"signals_conflict:{v['version_id']}:{rec.bar_close.isoformat()}")
                _db.pause_market_entries(conn, rec.coin, "signals_conflict")
            else:
                out["duplicates"] += 1
    out.update(ok=True, as_of=batch.as_of.isoformat(), generated_at=batch.generated_at.isoformat(),
               body_sha256=batch.body_sha256)
    _db.log.info("signals_ingested", extra={"fields": {k: out[k] for k in ("stored", "duplicates", "conflicts")}})
    return out

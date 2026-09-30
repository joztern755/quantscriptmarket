"""API data access: SQLAlchemy 2 Core `text()` queries with bound parameters ONLY (no string-formatted SQL).

Every method takes an open transactional `conn` (from `Services.db.begin()`), returns plain dicts, and never
selects `agent_keys.key_ciphertext` or `strategy_versions.code_ciphertext` (the `app_api` DB role has no SELECT
privilege on them — so `SELECT *` / `RETURNING *` is never used on those tables).

Keyset pagination: list methods take `limit` and an optional decoded cursor `(created_at, id)` and return
`limit + 1` rows so the caller can tell whether a next page exists (see deps.next_cursor).
Tables beyond SPEC §4: api_idempotency, admin_changes, user_login_countries (migrations/0004_api_extras.sql),
user_devices + users.mfa_factor_hash (0008_security_kyc.sql).

f-strings below ONLY splice module-level constant column lists (_USER_COLS …); every value is a bound parameter.
Each bound parameter is used with ONE type context per statement (Postgres rejects inconsistent deductions).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from contextlib import nullcontext

from app.api.store_security import SecurityStoreMixin

Cursor = Optional[tuple[datetime, str]]

LIVE_SUB_STATUSES = ("pending", "active", "past_due", "reduce_only", "paused_user", "closing")
PUBLIC_STRATEGY_STATUSES = ("listed", "paused")

_USER_COLS = ("id, created_at, firebase_uid, email, display_name, role::text AS role, plan::text AS plan, "
              "plan_period_end, country_attested, referral_code, referred_by, referral_tier::text AS referral_tier, "
              "status::text AS status, mfa_enrolled, device_fp_hash, mfa_factor_hash")
_AGENT_COLS = ("id, created_at, user_id, master_address, agent_address, agent_name, status::text AS status, "
               "approved_at, revoked_at")
_SUB_COLS = ("s.id, s.created_at, s.user_id, s.strategy_id, s.strategy_version_id, s.trading_address, "
             "s.master_address, s.allocation_micro, s.max_leverage_x100, s.status::text AS status, "
             "s.current_period_end, s.hwm_micro, s.cum_pnl_micro, s.cancel_positions::text AS cancel_positions, "
             "s.cancelled_at, s.past_due_since, s.pre_pause_status::text AS pre_pause_status, "
             "s.price_monthly_micro AS pinned_price_micro, s.profit_share_bps AS pinned_profit_share_bps, "
             "s.end_reason")
_STRAT_COLS = ("st.id, st.created_at, st.slug, st.name, st.owner_user_id, st.in_house, st.markets, st.timeframe, "
               "st.price_monthly_micro, st.profit_share_bps, st.status::text AS status, st.description")
_VERSION_COLS = ("v.id, v.created_at, v.strategy_id, v.version, v.code_hash, v.params, v.markets, v.timeframe, "
                 "v.lookback, v.max_leverage, v.published_at, v.backtest, v.live_since")


def _c(cursor: Cursor) -> dict[str, Any]:
    return {"cts": cursor[0] if cursor else None, "cid": cursor[1] if cursor else None}


def _j(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


class SqlStore(SecurityStoreMixin):
    # ------------------------------------------------------------------------------------------ helpers
    @staticmethod
    def _runner(conn: Any) -> Any:
        """A SqlRunner (has fetchall; e.g. tests' psql runner) or a SQLAlchemy Connection (prod)."""
        if hasattr(conn, "fetchall"):
            return conn
        from app.db.engine import SqlAlchemyRunner  # raises DbError(sqlstate) on database errors
        return SqlAlchemyRunner(conn)

    def _all(self, conn: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self._runner(conn).fetchall(sql, params)]

    def _one(self, conn: Any, sql: str, **params: Any) -> Optional[dict[str, Any]]:
        rows = self._all(conn, sql, **params)
        return rows[0] if rows else None

    def _exec(self, conn: Any, sql: str, **params: Any) -> int:
        """Returns the number of RETURNING rows (statements that need a count say `RETURNING id`)."""
        return len(self._all(conn, sql, **params))

    @staticmethod
    def savepoint(conn: Any) -> Any:
        begin_nested = getattr(conn, "begin_nested", None)
        if begin_nested is not None:
            return begin_nested()
        sp = getattr(conn, "savepoint", None)
        return sp() if sp is not None else nullcontext()

    # ------------------------------------------------------------------------------------------ idempotency
    def idem_claim(self, conn: Any, *, user_id: str, key: str, scope: str, fingerprint: str) -> Optional[dict]:
        row = self._one(conn, """
            INSERT INTO api_idempotency (user_id, idem_key, scope, fingerprint)
            VALUES (CAST(:uid AS uuid), :key, :scope, :fp)
            ON CONFLICT (user_id, idem_key) DO NOTHING
            RETURNING idem_key""", uid=user_id, key=key, scope=scope, fp=fingerprint)
        if row is not None:
            return None
        return self._one(conn, """
            SELECT scope, fingerprint, status_code, response FROM api_idempotency
            WHERE user_id = CAST(:uid AS uuid) AND idem_key = :key""", uid=user_id, key=key)

    def idem_complete(self, conn: Any, *, user_id: str, key: str, status_code: int, response: dict) -> None:
        self._exec(conn, """
            UPDATE api_idempotency SET status_code = :sc, response = CAST(:resp AS jsonb), completed_at = now()
            WHERE user_id = CAST(:uid AS uuid) AND idem_key = :key""",
                   sc=status_code, resp=_j(response), uid=user_id, key=key)

    # ------------------------------------------------------------------------------------------ audit
    def insert_audit(self, conn: Any, *, actor: str, action: str, target: str, payload: dict,
                     ip_hash: Optional[str]) -> None:
        """chain_seq / prev_hash / hash / created_at are set by the audit_log BEFORE INSERT trigger."""
        self._exec(conn, """INSERT INTO audit_log (actor, action, target, payload, ip_hash)
                            VALUES (:a, :ac, :t, CAST(:p AS jsonb), :ip)""",
                   a=actor, ac=action, t=target or None, p=_j(payload), ip=ip_hash)

    # ------------------------------------------------------------------------------------------ users
    def get_user_by_firebase_uid(self, conn: Any, uid: str) -> Optional[dict]:
        return self._one(conn, f"SELECT {_USER_COLS} FROM users WHERE firebase_uid = :uid", uid=uid)

    def get_user(self, conn: Any, user_id: str, *, for_update: bool = False) -> Optional[dict]:
        if for_update:
            return self._one(conn, f"SELECT {_USER_COLS} FROM users WHERE id = CAST(:id AS uuid) FOR UPDATE",
                             id=user_id)
        return self._one(conn, f"SELECT {_USER_COLS} FROM users WHERE id = CAST(:id AS uuid)", id=user_id)

    def lock_user(self, conn: Any, user_id: str) -> None:
        """Serialises money movements of one user (balance check → ledger post) within a transaction."""
        self._exec(conn, "SELECT 1 FROM users WHERE id = CAST(:id AS uuid) FOR UPDATE", id=user_id)

    def get_user_by_referral_code(self, conn: Any, code: str) -> Optional[dict]:
        return self._one(conn, f"SELECT {_USER_COLS} FROM users WHERE referral_code = :c", c=code)

    def create_user(self, conn: Any, *, firebase_uid: str, email: Optional[str], display_name: Optional[str],
                    referral_code: str, referred_by: Optional[str], mfa_enrolled: bool) -> Optional[dict]:
        """Insert; on firebase_uid race return the existing row (`_created` False); on referral-code collision
        return None so the caller retries with a new code."""
        row = self._one(conn, f"""
            INSERT INTO users (firebase_uid, email, display_name, referral_code, referred_by, mfa_enrolled)
            VALUES (:uid, :email, :name, :code, CAST(:ref AS uuid), :mfa)
            ON CONFLICT DO NOTHING
            RETURNING {_USER_COLS}""", uid=firebase_uid, email=email, name=(display_name or None) and display_name[:64],
                        code=referral_code, ref=referred_by, mfa=mfa_enrolled)
        if row is not None:
            row["_created"] = True
            return row
        existing = self.get_user_by_firebase_uid(conn, firebase_uid)
        if existing is not None:
            existing["_created"] = False
        return existing

    def update_display_name(self, conn: Any, user_id: str, name: str) -> None:
        self._exec(conn, "UPDATE users SET display_name = :n WHERE id = CAST(:id AS uuid)", n=name, id=user_id)

    def set_country_attested(self, conn: Any, user_id: str, country: str) -> None:
        self._exec(conn, "UPDATE users SET country_attested = :c WHERE id = CAST(:id AS uuid)", c=country, id=user_id)

    def set_role(self, conn: Any, user_id: str, role: str) -> None:
        self._exec(conn, "UPDATE users SET role = :r WHERE id = CAST(:id AS uuid)", r=role, id=user_id)

    def set_plan(self, conn: Any, user_id: str, plan: str, period_end: Optional[datetime]) -> None:
        self._exec(conn, "UPDATE users SET plan = :p, plan_period_end = :e WHERE id = CAST(:id AS uuid)",
                   p=plan, e=period_end, id=user_id)

    def set_user_status(self, conn: Any, user_id: str, status: str) -> int:
        return self._exec(conn, "UPDATE users SET status = :s WHERE id = CAST(:id AS uuid) RETURNING id",
                          s=status, id=user_id)

    def record_login_country(self, conn: Any, user_id: str, country: str) -> bool:
        """True when this country is new for the user AND the user had logged in from another country before."""
        inserted = self._one(conn, """
            INSERT INTO user_login_countries (user_id, country) VALUES (CAST(:u AS uuid), :c)
            ON CONFLICT (user_id, country) DO UPDATE SET last_seen = now()
            RETURNING (xmax = 0) AS inserted""", u=user_id, c=country)
        if not inserted or not inserted["inserted"]:
            return False
        prior = self._one(conn, """SELECT count(*) AS n FROM user_login_countries
                                   WHERE user_id = CAST(:u AS uuid) AND country <> :c""", u=user_id, c=country)
        return bool(prior and prior["n"] > 0)

    def record_device(self, conn: Any, user_id: str, device_hash: str, label: Optional[str]) -> bool:
        """True when this device is new for the user AND the user had signed in from another device before (0008)."""
        inserted = self._one(conn, """
            INSERT INTO user_devices (user_id, device_hash, label) VALUES (CAST(:u AS uuid), :h, :l)
            ON CONFLICT (user_id, device_hash) DO UPDATE SET last_seen = now()
            RETURNING (xmax = 0) AS inserted""", u=user_id, h=device_hash, l=(label or None) and label[:64])
        if not inserted or not inserted["inserted"]:
            return False
        prior = self._one(conn, """SELECT count(*) AS n FROM user_devices
                                   WHERE user_id = CAST(:u AS uuid) AND device_hash <> :h""", u=user_id, h=device_hash)
        return bool(prior and prior["n"] > 0)

    def set_mfa_factor_hash(self, conn: Any, user_id: str, factor_hash: str) -> None:
        self._exec(conn, "UPDATE users SET mfa_factor_hash = :h WHERE id = CAST(:u AS uuid)", h=factor_hash, u=user_id)

    def search_users(self, conn: Any, q: Optional[str], limit: int, cursor: Cursor) -> list[dict]:
        """Prefix search on e-mail (LIKE wildcards in the query are escaped: F18) or exact id."""
        like = None if q is None else q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._all(conn, f"""
            SELECT {_USER_COLS} FROM users
            WHERE (CAST(:q AS text) IS NULL OR lower(email) LIKE lower(CAST(:like AS text)) || '%' ESCAPE '\\'
                   OR id::text = CAST(:q AS text))
              AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
            ORDER BY created_at DESC, id DESC LIMIT :lim""", q=q, like=like, lim=limit + 1, **_c(cursor))

    # ------------------------------------------------------------------------------------------ consents
    def accepted_consents(self, conn: Any, user_id: str) -> dict[str, str]:
        rows = self._all(conn, """
            SELECT DISTINCT ON (doc) doc::text AS doc, doc_version FROM consents
            WHERE user_id = CAST(:u AS uuid) AND strategy_id IS NULL
            ORDER BY doc, accepted_at DESC""", u=user_id)
        return {r["doc"]: r["doc_version"] for r in rows}

    def insert_consent(self, conn: Any, *, user_id: str, doc: str, version: str, doc_text_sha256: str, context: str,
                       strategy_id: Optional[str], ip_hash: Optional[str], ua_hash: Optional[str]) -> None:
        self._exec(conn, """
            INSERT INTO consents (user_id, doc, doc_version, doc_text_sha256, context, strategy_id, ip_hash,
                                  user_agent_hash)
            VALUES (CAST(:u AS uuid), :doc, :ver, :h, :ctx, CAST(:sid AS uuid), :ip, :ua)""",
                   u=user_id, doc=doc, ver=version, h=doc_text_sha256, ctx=context, sid=strategy_id, ip=ip_hash,
                   ua=ua_hash)

    def recent_subscription_ack(self, conn: Any, *, user_id: str, strategy_id: str, version: str,
                                since: datetime) -> Optional[dict]:
        return self._one(conn, """
            SELECT doc_version, doc_text_sha256, accepted_at FROM consents
             WHERE user_id = CAST(:u AS uuid) AND doc = 'subscription_ack' AND strategy_id = CAST(:s AS uuid)
               AND doc_version = :v AND accepted_at >= :since
             ORDER BY accepted_at DESC LIMIT 1""", u=user_id, s=strategy_id, v=version, since=since)

    # ------------------------------------------------------------------------------------------ wallets
    def list_wallets(self, conn: Any, user_id: str) -> list[dict]:
        return self._all(conn, """SELECT master_address AS address, verified_at FROM wallets
                                  WHERE user_id = CAST(:u AS uuid) ORDER BY created_at""", u=user_id)

    def get_wallet(self, conn: Any, address: str) -> Optional[dict]:
        return self._one(conn, """SELECT id, user_id, master_address AS address, verified_at FROM wallets
                                  WHERE master_address = :a""", a=address)

    def verified_wallet(self, conn: Any, user_id: str, address: str) -> Optional[dict]:
        return self._one(conn, """SELECT master_address AS address, verified_at FROM wallets
                                  WHERE user_id = CAST(:u AS uuid) AND master_address = :a
                                    AND verified_at IS NOT NULL""", u=user_id, a=address)

    def upsert_verified_wallet(self, conn: Any, user_id: str, address: str, now: datetime) -> dict:
        """Bind address → user. Never re-binds a wallet that belongs to another user (returns that row). The FIRST
        verification time is kept (it is the wallet's age for the 48 h payout-address hold and the USDC credit cut-off)."""
        row = self._one(conn, """
            INSERT INTO wallets (user_id, master_address, verified_at) VALUES (CAST(:u AS uuid), :a, :t)
            ON CONFLICT (master_address) DO UPDATE SET verified_at = coalesce(wallets.verified_at, EXCLUDED.verified_at)
                WHERE wallets.user_id = EXCLUDED.user_id
            RETURNING user_id, master_address AS address, verified_at""", u=user_id, a=address, t=now)
        if row is None:
            row = self.get_wallet(conn, address)
        assert row is not None
        return row

    def user_for_verified_wallet(self, conn: Any, address: str) -> Optional[str]:
        row = self._one(conn, """SELECT user_id FROM wallets WHERE master_address = :a AND verified_at IS NOT NULL""",
                        a=address)
        return str(row["user_id"]) if row else None

    def create_wallet_nonce(self, conn: Any, *, user_id: str, nonce: str, expires_at: datetime) -> None:
        self._exec(conn, """INSERT INTO wallet_nonces (nonce, user_id, expires_at)
                            VALUES (:n, CAST(:u AS uuid), :e)""", n=nonce, u=user_id, e=expires_at)

    def consume_wallet_nonce(self, conn: Any, *, user_id: str, nonce: str, now: datetime) -> bool:
        """Single use: marks the nonce used iff it belongs to the user, is unused and unexpired."""
        row = self._one(conn, """UPDATE wallet_nonces SET used_at = :t
                                 WHERE nonce = :n AND user_id = CAST(:u AS uuid) AND used_at IS NULL AND expires_at > :t
                                 RETURNING nonce""", t=now, n=nonce, u=user_id)
        return row is not None

    def bind_referrer(self, conn: Any, *, user_id: str, referrer_id: str) -> bool:
        """First and only binding (the DB trigger also makes referred_by immutable once set)."""
        row = self._one(conn, """UPDATE users SET referred_by = CAST(:r AS uuid)
                                 WHERE id = CAST(:u AS uuid) AND referred_by IS NULL AND id <> CAST(:r AS uuid)
                                 RETURNING id""", r=referrer_id, u=user_id)
        return row is not None

    # ------------------------------------------------------------------------------------------ agents / builder
    def list_agents(self, conn: Any, user_id: str) -> list[dict]:
        return self._all(conn, f"""SELECT {_AGENT_COLS} FROM agent_keys WHERE user_id = CAST(:u AS uuid)
                                   ORDER BY created_at DESC LIMIT 50""", u=user_id)

    def get_agent(self, conn: Any, agent_id: str, user_id: str, *, for_update: bool = False) -> Optional[dict]:
        sql = f"SELECT {_AGENT_COLS} FROM agent_keys WHERE id = CAST(:id AS uuid) AND user_id = CAST(:u AS uuid)"
        if for_update:
            sql += " FOR UPDATE"
        return self._one(conn, sql, id=agent_id, u=user_id)

    def live_agents_for_master(self, conn: Any, master: str) -> list[dict]:
        return self._all(conn, f"""SELECT {_AGENT_COLS} FROM agent_keys WHERE master_address = :m
                                   AND status IN ('pending_approval', 'active') FOR UPDATE""", m=master)

    def active_agent_for_master(self, conn: Any, user_id: str, master: str) -> Optional[dict]:
        return self._one(conn, f"""SELECT {_AGENT_COLS} FROM agent_keys WHERE master_address = :m
                                   AND user_id = CAST(:u AS uuid) AND status = 'active'""", m=master, u=user_id)

    def set_agent_status(self, conn: Any, agent_id: str, status: str, now: datetime) -> None:
        self._exec(conn, """
            UPDATE agent_keys SET status = CAST(:s AS agent_key_status),
                approved_at = CASE WHEN CAST(:s AS agent_key_status) = 'active' THEN CAST(:t AS timestamptz)
                                   ELSE approved_at END,
                revoked_at = CASE WHEN CAST(:s AS agent_key_status) IN ('revoked', 'rotated') THEN CAST(:t AS timestamptz)
                                  ELSE revoked_at END
            WHERE id = CAST(:id AS uuid)""", s=status, t=now, id=agent_id)

    def insert_agent(self, conn: Any, *, user_id: str, master: str, agent_address: str, agent_name: str,
                     key_ciphertext: bytes, kms_key_version: str) -> dict:
        row = self._one(conn, f"""
            INSERT INTO agent_keys (user_id, master_address, agent_address, agent_name, key_ciphertext, kms_key_version)
            VALUES (CAST(:u AS uuid), :m, :a, :n, :ct, :kv)
            RETURNING {_AGENT_COLS}""", u=user_id, m=master, a=agent_address, n=agent_name, ct=key_ciphertext,
                        kv=kms_key_version)
        assert row is not None
        return row

    def insert_builder_approval(self, conn: Any, *, user_id: str, master: str, rate: int, now: datetime) -> dict:
        row = self._one(conn, """
            INSERT INTO builder_approvals (user_id, master_address, max_fee_rate_tenths_bp, verified_on_chain_at)
            VALUES (CAST(:u AS uuid), :m, :r, :t)
            RETURNING master_address, max_fee_rate_tenths_bp, verified_on_chain_at""",
                        u=user_id, m=master, r=rate, t=now)
        assert row is not None
        return row

    # ------------------------------------------------------------------------------------------ strategies (read)
    def list_public_strategies(self, conn: Any, *, market: Optional[str], limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, f"""
            SELECT {_STRAT_COLS} FROM strategies st
            WHERE st.status IN ('listed', 'paused')
              AND (CAST(:m AS text) IS NULL OR CAST(:m AS text) = ANY(st.markets))
              AND (CAST(:cts AS timestamptz) IS NULL OR (st.created_at, st.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
            ORDER BY st.created_at DESC, st.id DESC LIMIT :lim""", m=market, lim=limit + 1, **_c(cursor))

    def get_public_strategy(self, conn: Any, slug: str) -> Optional[dict]:
        return self._one(conn, f"""SELECT {_STRAT_COLS} FROM strategies st
                                   WHERE st.slug = :s AND st.status IN ('listed', 'paused')""", s=slug)

    def get_strategy(self, conn: Any, strategy_id: str, *, for_update: bool = False) -> Optional[dict]:
        sql = f"SELECT {_STRAT_COLS} FROM strategies st WHERE st.id = CAST(:id AS uuid)"
        if for_update:
            sql += " FOR UPDATE"
        return self._one(conn, sql, id=strategy_id)

    def current_versions(self, conn: Any, strategy_ids: list[str]) -> dict[str, dict]:
        """Latest PUBLISHED version per strategy (the one subscriptions trade and the live record counts)."""
        if not strategy_ids:
            return {}
        rows = self._all(conn, f"""
            SELECT DISTINCT ON (v.strategy_id) {_VERSION_COLS} FROM strategy_versions v
            WHERE v.strategy_id = ANY(CAST(:ids AS uuid[])) AND v.published_at IS NOT NULL
            ORDER BY v.strategy_id, v.version DESC""", ids=strategy_ids)
        return {str(r["strategy_id"]): r for r in rows}

    def list_versions(self, conn: Any, strategy_id: str) -> list[dict]:
        return self._all(conn, f"""SELECT {_VERSION_COLS} FROM strategy_versions v
                                   WHERE v.strategy_id = CAST(:id AS uuid) ORDER BY v.version DESC""", id=strategy_id)

    def get_version(self, conn: Any, version_id: str) -> Optional[dict]:
        return self._one(conn, f"SELECT {_VERSION_COLS} FROM strategy_versions v WHERE v.id = CAST(:id AS uuid)",
                         id=version_id)

    def holds(self, conn: Any, strategy_ids: list[str]) -> dict[str, bool]:
        """True = every coin's target weight at the latest bar close is 0 ("HOLDS — no active signals")."""
        if not strategy_ids:
            return {}
        rows = self._all(conn, """
            WITH latest AS (
                SELECT strategy_id, max(bar_close) AS bc FROM signals
                WHERE strategy_id = ANY(CAST(:ids AS uuid[])) GROUP BY strategy_id)
            SELECT s.strategy_id, bool_and(s.target_weight_bps = 0) AS flat
            FROM signals s JOIN latest l ON l.strategy_id = s.strategy_id AND l.bc = s.bar_close
            GROUP BY s.strategy_id""", ids=strategy_ids)
        return {str(r["strategy_id"]): bool(r["flat"]) for r in rows}

    def track_record_inputs(self, conn: Any, strategy_id: str, window_start: datetime) -> tuple[list[dict], list[dict]]:
        """(events, spans) for domain.track_record. Events are pre-aggregated per (subscription, day) using the
        LAST event time of the day so window filtering stays exact. Spans: one per subscription at its current
        allocation (allocation history is not stored yet — documented approximation)."""
        events = self._all(conn, """
            SELECT subscription_id, max(t) AS time, sum(pnl)::bigint AS pnl_micro FROM (
                SELECT f.subscription_id, f.time AS t, (f.closed_pnl_micro - f.fee_micro) AS pnl
                  FROM fills f JOIN subscriptions s ON s.id = f.subscription_id
                 WHERE s.strategy_id = CAST(:sid AS uuid) AND f.time >= :ws
                UNION ALL
                SELECT fe.subscription_id, fe.time, fe.usdc_micro
                  FROM funding_events fe JOIN subscriptions s ON s.id = fe.subscription_id
                 WHERE s.strategy_id = CAST(:sid AS uuid) AND fe.time >= :ws
            ) x GROUP BY subscription_id, date_trunc('day', t)""", sid=strategy_id, ws=window_start)
        spans = self._all(conn, """
            SELECT id AS subscription_id, user_id, allocation_micro, created_at AS start,
                   CASE WHEN status IN ('cancelled') THEN coalesce(cancelled_at, status_changed_at)
                        WHEN status = 'paused_user' THEN status_changed_at ELSE NULL END AS "end"
              FROM subscriptions
             WHERE strategy_id = CAST(:sid AS uuid)
               AND (status NOT IN ('cancelled', 'paused_user') OR coalesce(cancelled_at, status_changed_at) >= :ws)
               AND status <> 'pending'""", sid=strategy_id, ws=window_start)
        return events, spans

    def rating_summary(self, conn: Any, strategy_id: str) -> dict:
        row = self._one(conn, """SELECT count(*) AS n, (avg(rating) * 100)::int AS avg_x100 FROM reviews
                                 WHERE strategy_id = CAST(:id AS uuid)""", id=strategy_id)
        return row or {"n": 0, "avg_x100": None}

    def list_reviews(self, conn: Any, strategy_id: str, limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, """
            SELECT r.id, r.created_at, r.rating, r.body, coalesce(u.display_name, 'Subscriber') AS author
              FROM reviews r JOIN users u ON u.id = r.user_id
             WHERE r.strategy_id = CAST(:id AS uuid)
               AND (CAST(:cts AS timestamptz) IS NULL OR (r.created_at, r.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY r.created_at DESC, r.id DESC LIMIT :lim""", id=strategy_id, lim=limit + 1, **_c(cursor))

    def list_public_posts(self, conn: Any, *, strategy_slug: Optional[str], limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, """
            SELECT p.id, p.created_at, p.title, p.price_micro, p.published_at, st.slug AS strategy_slug,
                   u.display_name AS creator_display_name,
                   CASE WHEN p.price_micro = 0 THEN left(p.body, 280) ELSE NULL END AS preview
              FROM posts p JOIN users u ON u.id = p.creator_id
              LEFT JOIN strategies st ON st.id = p.strategy_id
             WHERE p.published_at IS NOT NULL AND u.status = 'active'
               AND (p.strategy_id IS NULL OR st.status IN ('listed', 'paused'))
               AND (CAST(:slug AS text) IS NULL OR st.slug = CAST(:slug AS text))
               AND (CAST(:cts AS timestamptz) IS NULL OR (p.created_at, p.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY p.created_at DESC, p.id DESC LIMIT :lim""", slug=strategy_slug, lim=limit + 1, **_c(cursor))

    def showcase(self, conn: Any, slug: str, now: datetime) -> list[dict]:
        return self._all(conn, """
            SELECT w.address, w.period_month, w.revealed_at FROM showcase_wallets w
              JOIN strategies st ON st.id = w.strategy_id
             WHERE st.slug = :s AND st.status IN ('listed', 'paused')
               AND w.revealed_at IS NOT NULL AND w.revealed_at <= :now
               AND (w.period_month + interval '1 month') <= :now
             ORDER BY w.period_month DESC LIMIT 60""", s=slug, now=now)

    # ------------------------------------------------------------------------------------------ subscriptions
    def count_live_subscriptions(self, conn: Any, user_id: str) -> int:
        row = self._one(conn, """SELECT count(*) AS n FROM subscriptions WHERE user_id = CAST(:u AS uuid)
                                 AND status::text IN ('pending', 'active', 'past_due', 'reduce_only', 'paused_user', 'closing')""",
                        u=user_id)
        return int(row["n"]) if row else 0

    def live_subscription_prices(self, conn: Any, user_id: str) -> list[int]:
        rows = self._all(conn, """
            SELECT coalesce(st.price_monthly_micro, 0) AS p FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
             WHERE s.user_id = CAST(:u AS uuid) AND s.status::text IN ('pending', 'active', 'past_due', 'reduce_only')""",
                         u=user_id)
        return [int(r["p"]) for r in rows]

    def total_live_allocation(self, conn: Any, user_id: Optional[str] = None, *,
                              exclude_subscription_id: Optional[str] = None) -> int:
        """Σ allocation of live subscriptions (one user, or the whole platform) — launch-phase caps."""
        row = self._one(conn, """
            SELECT coalesce(sum(allocation_micro), 0)::bigint AS s FROM subscriptions
             WHERE status::text IN ('pending', 'active', 'past_due', 'reduce_only', 'paused_user', 'closing')
               AND (CAST(:u AS uuid) IS NULL OR user_id = CAST(:u AS uuid))
               AND (CAST(:x AS uuid) IS NULL OR id <> CAST(:x AS uuid))""", u=user_id, x=exclude_subscription_id)
        return int(row["s"]) if row else 0

    def live_subscription_on_address(self, conn: Any, address: str) -> Optional[dict]:
        return self._one(conn, f"""SELECT {_SUB_COLS} FROM subscriptions s WHERE s.trading_address = :a
                                   AND s.status::text IN ('pending', 'active', 'past_due', 'reduce_only', 'closing')""", a=address)

    def insert_subscription(self, conn: Any, *, user_id: str, strategy_id: str, version_id: str, trading_address: str,
                            master_address: str, allocation_micro: int, max_leverage_x100: int, status: str,
                            current_period_end: datetime, price_monthly_micro: Optional[int] = None,
                            profit_share_bps: Optional[int] = None) -> dict:
        """M8: the terms the user acknowledged are PINNED on the row (the 0011 trigger pins the strategy's current
        terms when they are not passed)."""
        row = self._one(conn, f"""
            WITH s AS (
                INSERT INTO subscriptions (user_id, strategy_id, strategy_version_id, trading_address, master_address,
                                           allocation_micro, max_leverage_x100, status, current_period_end,
                                           price_monthly_micro, profit_share_bps)
                VALUES (CAST(:u AS uuid), CAST(:sid AS uuid), CAST(:vid AS uuid), :a, :m, :alloc, :lev,
                        CAST(:st AS subscription_status), :pe, CAST(:price AS bigint), CAST(:ps AS integer))
                RETURNING *)
            SELECT {_SUB_COLS}, st.slug AS strategy_slug, st.name AS strategy_name, st.markets AS strategy_markets
              FROM s JOIN strategies st ON st.id = s.strategy_id""",
                        u=user_id, sid=strategy_id, vid=version_id, a=trading_address, m=master_address,
                        alloc=allocation_micro,
                        lev=max_leverage_x100, st=status, pe=current_period_end, price=price_monthly_micro,
                        ps=profit_share_bps)
        assert row is not None
        return row

    def get_subscription(self, conn: Any, sub_id: str, user_id: str, *, for_update: bool = False) -> Optional[dict]:
        sql = f"""SELECT {_SUB_COLS}, st.slug AS strategy_slug, st.name AS strategy_name, st.markets AS strategy_markets
                  FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
                  WHERE s.id = CAST(:id AS uuid) AND s.user_id = CAST(:u AS uuid)"""
        if for_update:
            sql += " FOR UPDATE OF s"
        return self._one(conn, sql, id=sub_id, u=user_id)

    def update_subscription(self, conn: Any, sub_id: str, *, allocation_micro: Optional[int],
                            max_leverage_x100: Optional[int], status: Optional[str]) -> None:
        self._exec(conn, """
            UPDATE subscriptions SET
                allocation_micro = coalesce(:alloc, allocation_micro),
                max_leverage_x100 = coalesce(:lev, max_leverage_x100),
                status = coalesce(CAST(:st AS subscription_status), status),
                status_changed_at = CASE WHEN CAST(:st AS subscription_status) IS NOT NULL
                                          AND CAST(:st AS subscription_status) <> status
                                         THEN now() ELSE status_changed_at END
            WHERE id = CAST(:id AS uuid)""", alloc=allocation_micro, lev=max_leverage_x100, st=status, id=sub_id)

    def end_subscription(self, conn: Any, sub_id: str, *, positions: str, now: datetime) -> Optional[dict]:
        """SPEC §12: positions 'close' → status 'closing' (executor flattens, then marks cancelled);
        'leave' → 'cancelled' now. Returns the updated row, or None if it was already cancelled."""
        return self._one(conn, """
            UPDATE subscriptions
               SET cancel_positions = CAST(:p AS cancel_positions_mode),
                   status = CASE WHEN :p2 = 'close' THEN CAST('closing' AS subscription_status)
                                 ELSE CAST('cancelled' AS subscription_status) END,
                   status_changed_at = :t,
                   cancelled_at = CASE WHEN :p3 = 'close' THEN cancelled_at ELSE CAST(:t2 AS timestamptz) END
             WHERE id = CAST(:id AS uuid) AND status <> 'cancelled'
            RETURNING id, status::text AS status""", p=positions, p2=positions, p3=positions, t=now, t2=now, id=sub_id)

    def list_subscriptions(self, conn: Any, user_id: str, limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, f"""
            SELECT {_SUB_COLS}, st.slug AS strategy_slug, st.name AS strategy_name, st.markets AS strategy_markets
              FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
             WHERE s.user_id = CAST(:u AS uuid)
               AND (CAST(:cts AS timestamptz) IS NULL OR (s.created_at, s.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY s.created_at DESC, s.id DESC LIMIT :lim""", u=user_id, lim=limit + 1, **_c(cursor))

    def trading_addresses(self, conn: Any, user_id: str) -> list[str]:
        rows = self._all(conn, """
            SELECT DISTINCT trading_address AS a FROM subscriptions WHERE user_id = CAST(:u AS uuid)
               AND status::text IN ('pending', 'active', 'past_due', 'reduce_only', 'paused_user', 'closing')
            UNION SELECT master_address FROM wallets WHERE user_id = CAST(:u AS uuid) AND verified_at IS NOT NULL""",
                         u=user_id)
        return sorted({str(r["a"]) for r in rows})

    def earliest_subscription(self, conn: Any, user_id: str, strategy_id: str) -> Optional[datetime]:
        row = self._one(conn, """SELECT min(created_at) AS t FROM subscriptions WHERE user_id = CAST(:u AS uuid)
                                 AND strategy_id = CAST(:s AS uuid) AND status <> 'pending'""",
                        u=user_id, s=strategy_id)
        return row["t"] if row else None

    # ------------------------------------------------------------------------------------------ ledger reads
    def account_id(self, conn: Any, code: str) -> Optional[str]:
        row = self._one(conn, "SELECT id FROM ledger_accounts WHERE code = :c", c=code)
        return str(row["id"]) if row else None

    def account_code_by_id(self, conn: Any, account_id: str) -> Optional[str]:
        row = self._one(conn, "SELECT code FROM ledger_accounts WHERE id = CAST(:id AS uuid)", id=account_id)
        return str(row["code"]) if row else None

    def ledger_history(self, conn: Any, code: str, limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, """
            SELECT t.id, t.id AS tx_id, t.created_at, t.kind, t.memo, e.amount_micro AS raw_amount_micro
              FROM ledger_entries e
              JOIN ledger_accounts a ON a.id = e.account_id
              JOIN ledger_transactions t ON t.id = e.tx_id
             WHERE a.code = :c
               AND (CAST(:cts AS timestamptz) IS NULL OR (t.created_at, t.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY t.created_at DESC, t.id DESC LIMIT :lim""", c=code, lim=limit + 1, **_c(cursor))

    def total_credited(self, conn: Any, code: str) -> int:
        """Lifetime credits (normal-side increases) of a liability account, e.g. creator/referrer earnings."""
        row = self._one(conn, """SELECT coalesce(-sum(e.amount_micro) FILTER (WHERE e.amount_micro < 0), 0)::bigint AS s
                                 FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
                                 WHERE a.code = :c""", c=code)
        return int(row["s"]) if row else 0

    # ------------------------------------------------------------------------------------------ deposits
    def list_deposits(self, conn: Any, user_id: str, limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, """
            SELECT id, created_at, method::text AS method, external_ref, amount_micro, status::text AS status
              FROM deposits WHERE user_id = CAST(:u AS uuid)
               AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY created_at DESC, id DESC LIMIT :lim""", u=user_id, lim=limit + 1, **_c(cursor))

    def insert_pending_deposit(self, conn: Any, *, user_id: str, method: str, external_ref: str, amount_micro: int,
                               currency: str = "USD", amount_minor: Optional[int] = None) -> None:
        self._exec(conn, """
            INSERT INTO deposits (user_id, method, external_ref, amount_micro, currency, amount_minor, status)
            VALUES (CAST(:u AS uuid), CAST(:m AS deposit_method), :r, :a, :cur, :minor, 'pending')
            ON CONFLICT (external_ref) DO NOTHING""",
                   u=user_id, m=method, r=external_ref, a=amount_micro, cur=currency.upper(), minor=amount_minor)

    def mark_deposit_credited(self, conn: Any, *, user_id: str, method: str, external_ref: str, amount_micro: int,
                              tx_id: str, withdrawable: bool, fee_micro: int = 0, currency: Optional[str] = None,
                              amount_minor: Optional[int] = None, meta: Optional[dict] = None) -> dict:
        cols = ("id, created_at, method::text AS method, external_ref, amount_micro, status::text AS status, "
                "withdrawable")
        row = self._one(conn, f"""
            INSERT INTO deposits (user_id, method, external_ref, amount_micro, currency, amount_minor, fee_micro,
                                  withdrawable, status, credited_tx_id, meta)
            VALUES (CAST(:u AS uuid), CAST(:m AS deposit_method), :r, :a, coalesce(CAST(:cur AS text), 'USD'), :minor,
                    :fee, :w, 'credited', CAST(:tx AS uuid), CAST(:meta AS jsonb))
            ON CONFLICT (external_ref) DO UPDATE
               SET status = 'credited', credited_tx_id = EXCLUDED.credited_tx_id, amount_micro = EXCLUDED.amount_micro,
                   fee_micro = EXCLUDED.fee_micro, withdrawable = EXCLUDED.withdrawable,
                   amount_minor = coalesce(EXCLUDED.amount_minor, deposits.amount_minor), meta = EXCLUDED.meta
             WHERE deposits.status = 'pending' AND deposits.user_id = EXCLUDED.user_id
            RETURNING {cols}""", u=user_id, m=method, r=external_ref, a=amount_micro,
                        cur=(currency or None) and currency.upper(), minor=amount_minor, fee=fee_micro, w=withdrawable,
                        tx=tx_id, meta=_j(meta or {}))
        if row is None:
            row = self._one(conn, f"SELECT {cols} FROM deposits WHERE external_ref = :r", r=external_ref)
        assert row is not None
        return row

    def mark_deposit_reversed(self, conn: Any, external_ref: str) -> None:
        self._exec(conn, "UPDATE deposits SET status = 'reversed' WHERE external_ref = :r AND status = 'credited'",
                   r=external_ref)

    def withdrawable_usdc(self, conn: Any, user_id: str) -> int:
        """USDC-funded UNSPENT balance (F4/H4). Card-funded credits are spend-only and are spent FIRST
        (fee_funding_card_unspent, 0011), so withdrawable = spendable − card lot, and never more than the USDC ever
        credited minus what was already withdrawn."""
        row = self._one(conn, """
            SELECT (SELECT -coalesce(sum(e.amount_micro), 0) FROM ledger_entries e
                      JOIN ledger_accounts a ON a.id = e.account_id
                     WHERE a.code = CAST(:code AS text)) AS spendable,
                   fee_funding_card_unspent(CAST(:u2 AS uuid)) AS card_unspent""",
                        code=f"user:{user_id}:fee_balance", u2=user_id)
        if not row:
            return 0
        lot = max(0, int(row["spendable"] or 0) - int(row["card_unspent"] or 0))
        return max(0, min(lot, self.usdc_credited_minus_withdrawn(conn, user_id)))

    # ------------------------------------------------------------------------------------------ withdrawals / payouts
    _PAYOUT_COLS_W = ("id, created_at, 'withdrawal' AS kind, beneficiary_user_id AS beneficiary, amount_micro, "
                      "to_address, status::text AS status, maker_admin, checker_admin, tx_hash, NULL::uuid AS ledger_account_id, "
                      "send_nonce, send_issued_at")
    _PAYOUT_COLS_P = ("id, created_at, 'payout' AS kind, beneficiary_user_id AS beneficiary, amount_micro, "
                      "to_address, status::text AS status, maker_admin, checker_admin, tx_hash, ledger_account_id, "
                      "send_nonce, send_issued_at")

    def pending_withdrawals_total(self, conn: Any, user_id: str) -> int:
        row = self._one(conn, """SELECT coalesce(sum(amount_micro), 0)::bigint AS s FROM withdrawals
                                 WHERE beneficiary_user_id = CAST(:u AS uuid)
                                   AND status IN ('requested', 'approved_1', 'approved_2')""", u=user_id)
        return int(row["s"]) if row else 0

    def pending_payouts_total(self, conn: Any, user_id: str) -> int:
        row = self._one(conn, """SELECT coalesce(sum(amount_micro), 0)::bigint AS s FROM payouts
                                 WHERE beneficiary_user_id = CAST(:u AS uuid)
                                   AND status IN ('requested', 'approved_1', 'approved_2')""", u=user_id)
        return int(row["s"]) if row else 0

    def insert_withdrawal(self, conn: Any, *, user_id: str, amount_micro: int, to_address: str) -> dict:
        row = self._one(conn, f"""INSERT INTO withdrawals (beneficiary_user_id, amount_micro, to_address)
                                  VALUES (CAST(:u AS uuid), :a, :to) RETURNING {self._PAYOUT_COLS_W}""",
                        u=user_id, a=amount_micro, to=to_address)
        assert row is not None
        return row

    def insert_payout(self, conn: Any, *, user_id: str, ledger_account_id: str, amount_micro: int,
                      to_address: str) -> dict:
        row = self._one(conn, f"""INSERT INTO payouts (beneficiary_user_id, ledger_account_id, amount_micro, to_address)
                                  VALUES (CAST(:u AS uuid), CAST(:la AS uuid), :a, :to)
                                  RETURNING {self._PAYOUT_COLS_P}""",
                        u=user_id, la=ledger_account_id, a=amount_micro, to=to_address)
        assert row is not None
        return row

    def list_user_payouts(self, conn: Any, user_id: str, limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, f"""
            SELECT * FROM (
                SELECT {self._PAYOUT_COLS_W} FROM withdrawals WHERE beneficiary_user_id = CAST(:u AS uuid)
                UNION ALL
                SELECT {self._PAYOUT_COLS_P} FROM payouts WHERE beneficiary_user_id = CAST(:u AS uuid)
            ) x
            WHERE (CAST(:cts AS timestamptz) IS NULL OR (x.created_at, x.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
            ORDER BY x.created_at DESC, x.id DESC LIMIT :lim""", u=user_id, lim=limit + 1, **_c(cursor))

    def admin_list_payouts(self, conn: Any, *, kind: str, status: Optional[str], limit: int,
                           cursor: Cursor) -> list[dict]:
        if kind == "withdrawal":
            sql = f"""SELECT {self._PAYOUT_COLS_W} FROM withdrawals
                      WHERE (CAST(:st AS text) IS NULL OR status::text = CAST(:st AS text))
                        AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
                      ORDER BY created_at DESC, id DESC LIMIT :lim"""
        else:
            sql = f"""SELECT {self._PAYOUT_COLS_P} FROM payouts
                      WHERE (CAST(:st AS text) IS NULL OR status::text = CAST(:st AS text))
                        AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
                      ORDER BY created_at DESC, id DESC LIMIT :lim"""
        return self._all(conn, sql, st=status, lim=limit + 1, **_c(cursor))

    def get_payout(self, conn: Any, kind: str, payout_id: str, *, for_update: bool = True) -> Optional[dict]:
        lock = " FOR UPDATE" if for_update else ""
        if kind == "withdrawal":
            return self._one(conn, f"SELECT {self._PAYOUT_COLS_W} FROM withdrawals WHERE id = CAST(:id AS uuid)" + lock,
                             id=payout_id)
        return self._one(conn, f"SELECT {self._PAYOUT_COLS_P} FROM payouts WHERE id = CAST(:id AS uuid)" + lock,
                         id=payout_id)

    def payout_approve_1(self, conn: Any, kind: str, payout_id: str, admin_id: str, now: datetime) -> int:
        sql_w = """UPDATE withdrawals SET status = 'approved_1', maker_admin = CAST(:a AS uuid), maker_approved_at = :t
                   WHERE id = CAST(:id AS uuid) AND status = 'requested' RETURNING id"""
        sql_p = """UPDATE payouts SET status = 'approved_1', maker_admin = CAST(:a AS uuid), maker_approved_at = :t
                   WHERE id = CAST(:id AS uuid) AND status = 'requested' RETURNING id"""
        return self._exec(conn, sql_w if kind == "withdrawal" else sql_p, a=admin_id, t=now, id=payout_id)

    def payout_approve_2(self, conn: Any, kind: str, payout_id: str, admin_id: str, now: datetime) -> int:
        sql_w = """UPDATE withdrawals SET status = 'approved_2', checker_admin = CAST(:a AS uuid), checker_approved_at = :t
                   WHERE id = CAST(:id AS uuid) AND status = 'approved_1' AND maker_admin <> CAST(:a AS uuid)
                   RETURNING id"""
        sql_p = """UPDATE payouts SET status = 'approved_2', checker_admin = CAST(:a AS uuid), checker_approved_at = :t
                   WHERE id = CAST(:id AS uuid) AND status = 'approved_1' AND maker_admin <> CAST(:a AS uuid)
                   RETURNING id"""
        return self._exec(conn, sql_w if kind == "withdrawal" else sql_p, a=admin_id, t=now, id=payout_id)

    def payout_reject(self, conn: Any, kind: str, payout_id: str, admin_id: str, reason: str) -> int:
        sql_w = """UPDATE withdrawals SET status = 'rejected', rejected_by = CAST(:a AS uuid), reject_reason = :r
                   WHERE id = CAST(:id AS uuid) AND status IN ('requested', 'approved_1', 'approved_2') RETURNING id"""
        sql_p = """UPDATE payouts SET status = 'rejected', rejected_by = CAST(:a AS uuid), reject_reason = :r
                   WHERE id = CAST(:id AS uuid) AND status IN ('requested', 'approved_1', 'approved_2') RETURNING id"""
        return self._exec(conn, sql_w if kind == "withdrawal" else sql_p, a=admin_id, r=reason, id=payout_id)

    def payout_mark_sent(self, conn: Any, kind: str, payout_id: str, tx_hash: str, ledger_tx_id: str) -> int:
        sql_w = """UPDATE withdrawals SET status = 'sent', tx_hash = :h, ledger_tx_id = CAST(:tx AS uuid)
                   WHERE id = CAST(:id AS uuid) AND status = 'approved_2' RETURNING id"""
        sql_p = """UPDATE payouts SET status = 'sent', tx_hash = :h, ledger_tx_id = CAST(:tx AS uuid)
                   WHERE id = CAST(:id AS uuid) AND status = 'approved_2' RETURNING id"""
        return self._exec(conn, sql_w if kind == "withdrawal" else sql_p, h=tx_hash, tx=ledger_tx_id, id=payout_id)

    def tx_hash_used(self, conn: Any, tx_hash: str) -> bool:
        """A treasury usdSend hash already recorded for a withdrawal, a payout or a held-deposit refund (0009)."""
        row = self._one(conn, """SELECT EXISTS (SELECT 1 FROM withdrawals WHERE tx_hash = :h)
                                     OR EXISTS (SELECT 1 FROM payouts WHERE tx_hash = :h)
                                     OR (to_regclass('public.suspense_releases') IS NOT NULL
                                         AND EXISTS (SELECT 1 FROM suspense_releases WHERE refund_tx_hash = lower(:h)))
                                     AS used""", h=tx_hash)
        return bool(row and row["used"])

    # ------------------------------------------------------------------------------------------ alerts
    def insert_alert(self, conn: Any, *, user_id: Optional[str], severity: str, kind: str, payload: dict,
                     dedup_key: Optional[str] = None) -> None:
        if dedup_key:
            self._exec(conn, """INSERT INTO alerts (user_id, severity, kind, payload, dedup_key)
                                VALUES (CAST(:u AS uuid), CAST(:s AS alert_severity), :k, CAST(:p AS jsonb), :d)
                                ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING""",
                       u=user_id, s=severity, k=kind, p=_j(payload), d=dedup_key[:200])
            return
        self._exec(conn, """INSERT INTO alerts (user_id, severity, kind, payload)
                            VALUES (CAST(:u AS uuid), :s, :k, CAST(:p AS jsonb))""",
                   u=user_id, s=severity, k=kind, p=_j(payload))

    def list_user_alerts(self, conn: Any, user_id: str, limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, """
            SELECT id, created_at, severity::text AS severity, kind, payload, acked_at FROM alerts
             WHERE user_id = CAST(:u AS uuid)
               AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY created_at DESC, id DESC LIMIT :lim""", u=user_id, lim=limit + 1, **_c(cursor))

    def ack_user_alert(self, conn: Any, alert_id: str, user_id: str, now: datetime) -> int:
        return self._exec(conn, """UPDATE alerts SET acked_at = :t, acked_by = CAST(:u AS uuid)
                                   WHERE id = CAST(:id AS uuid) AND user_id = CAST(:u AS uuid) AND acked_at IS NULL
                                   RETURNING id""",
                          t=now, u=user_id, id=alert_id)

    def admin_list_alerts(self, conn: Any, *, severity: Optional[str], unacked_only: bool, ops_only: bool,
                          limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, """
            SELECT id, created_at, severity::text AS severity, kind, payload, acked_at FROM alerts
             WHERE (CAST(:sev AS text) IS NULL OR severity::text = CAST(:sev AS text))
               AND (NOT :un OR acked_at IS NULL) AND (NOT :ops OR user_id IS NULL)
               AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY created_at DESC, id DESC LIMIT :lim""",
                         sev=severity, un=unacked_only, ops=ops_only, lim=limit + 1, **_c(cursor))

    def admin_ack_alert(self, conn: Any, alert_id: str, admin_id: str, now: datetime) -> int:
        return self._exec(conn, """UPDATE alerts SET acked_at = :t, acked_by = CAST(:a AS uuid)
                                   WHERE id = CAST(:id AS uuid) AND acked_at IS NULL RETURNING id""",
                          t=now, a=admin_id, id=alert_id)

    # ------------------------------------------------------------------------------------------ reviews / posts
    def upsert_review(self, conn: Any, *, strategy_id: str, user_id: str, rating: int, body: Optional[str],
                      eligible_since: datetime) -> dict:
        row = self._one(conn, """
            INSERT INTO reviews (strategy_id, user_id, rating, body, eligible_since)
            VALUES (CAST(:s AS uuid), CAST(:u AS uuid), :r, :b, :e)
            ON CONFLICT (strategy_id, user_id) DO UPDATE SET rating = EXCLUDED.rating, body = EXCLUDED.body
            RETURNING id, created_at, rating, body""", s=strategy_id, u=user_id, r=rating, b=body, e=eligible_since)
        assert row is not None
        return row

    def get_post(self, conn: Any, post_id: str) -> Optional[dict]:
        return self._one(conn, """
            SELECT p.id, p.created_at, p.creator_id, p.strategy_id, p.title, p.body, p.price_micro, p.published_at,
                   st.slug AS strategy_slug, st.in_house AS strategy_in_house
              FROM posts p LEFT JOIN strategies st ON st.id = p.strategy_id
             WHERE p.id = CAST(:id AS uuid)""", id=post_id)

    def has_purchased(self, conn: Any, post_id: str, user_id: str) -> bool:
        row = self._one(conn, """SELECT 1 AS x FROM post_purchases WHERE post_id = CAST(:p AS uuid)
                                 AND user_id = CAST(:u AS uuid)""", p=post_id, u=user_id)
        return row is not None

    def insert_purchase(self, conn: Any, *, post_id: str, user_id: str, price_micro: int, tx_id: str) -> bool:
        row = self._one(conn, """INSERT INTO post_purchases (post_id, user_id, price_micro, ledger_tx_id)
                                 VALUES (CAST(:p AS uuid), CAST(:u AS uuid), :pr, CAST(:tx AS uuid))
                                 ON CONFLICT (post_id, user_id) DO NOTHING RETURNING id""",
                        p=post_id, u=user_id, pr=price_micro, tx=tx_id)
        return row is not None

    def insert_post(self, conn: Any, *, creator_id: str, strategy_id: Optional[str], title: str, body: str,
                    price_micro: int, now: datetime) -> dict:
        row = self._one(conn, """
            INSERT INTO posts (creator_id, strategy_id, title, body, price_micro, published_at)
            VALUES (CAST(:c AS uuid), CAST(:s AS uuid), :t, :b, :p, :now)
            RETURNING id, created_at, title, price_micro, published_at, body""",
                        c=creator_id, s=strategy_id, t=title, b=body, p=price_micro, now=now)
        assert row is not None
        return row

    # ------------------------------------------------------------------------------------------ referrals
    def referral_stats(self, conn: Any, user_id: str, since: datetime) -> dict:
        row = self._one(conn, """
            WITH ref AS (SELECT id FROM users WHERE referred_by = CAST(:u AS uuid))
            SELECT (SELECT count(*) FROM ref) AS total,
                   (SELECT count(DISTINCT s.user_id) FROM subscriptions s JOIN ref ON ref.id = s.user_id
                     WHERE s.status IN ('active', 'past_due', 'reduce_only')
                       AND user_has_paid_activity(s.user_id)) AS active,
                   (SELECT coalesce(sum(round(f.px * f.sz * 1000000)), 0)::bigint
                      FROM fills f JOIN subscriptions s ON s.id = f.subscription_id JOIN ref ON ref.id = s.user_id
                     WHERE f.time >= :since) AS notional""", u=user_id, since=since)
        return row or {"total": 0, "active": 0, "notional": 0}

    # ------------------------------------------------------------------------------------------ creator
    def insert_strategy(self, conn: Any, *, owner_user_id: str, slug: str, name: str, description: Optional[str],
                        markets: list[str], timeframe: str, price_monthly_micro: int, profit_share_bps: int) -> Optional[dict]:
        try:
            with self.savepoint(conn):
                return self._one(conn, f"""
                    WITH st AS (
                        INSERT INTO strategies (slug, name, owner_user_id, in_house, markets, timeframe,
                                                price_monthly_micro, profit_share_bps, status, description)
                        VALUES (:slug, :name, CAST(:o AS uuid), false, CAST(:m AS text[]), :tf, :p, :ps, 'draft', :d)
                        RETURNING *)
                    SELECT {_STRAT_COLS} FROM st""", slug=slug, name=name, o=owner_user_id, m=list(markets), tf=timeframe,
                                 p=price_monthly_micro, ps=profit_share_bps, d=description)
        except Exception as e:  # noqa: BLE001 - only a unique violation is expected here
            from app.db.engine import sqlstate_of
            if sqlstate_of(e) == "23505":
                return None  # slug taken
            raise

    def list_owned_strategies(self, conn: Any, owner_id: str) -> list[dict]:
        return self._all(conn, f"""SELECT {_STRAT_COLS} FROM strategies st WHERE st.owner_user_id = CAST(:o AS uuid)
                                   ORDER BY st.created_at DESC LIMIT 100""", o=owner_id)

    def update_strategy_terms(self, conn: Any, strategy_id: str, *, name: Optional[str], description: Optional[str],
                              price_monthly_micro: Optional[int], profit_share_bps: Optional[int]) -> None:
        self._exec(conn, """
            UPDATE strategies SET name = coalesce(:n, name), description = coalesce(:d, description),
                   price_monthly_micro = coalesce(:p, price_monthly_micro),
                   profit_share_bps = coalesce(:ps, profit_share_bps)
             WHERE id = CAST(:id AS uuid)""", n=name, d=description, p=price_monthly_micro, ps=profit_share_bps,
                   id=strategy_id)

    def set_strategy_status(self, conn: Any, strategy_id: str, status: str) -> None:
        self._exec(conn, "UPDATE strategies SET status = :s WHERE id = CAST(:id AS uuid)", s=status, id=strategy_id)

    def set_strategy_price(self, conn: Any, strategy_id: str, price_monthly_micro: int) -> None:
        self._exec(conn, "UPDATE strategies SET price_monthly_micro = :p WHERE id = CAST(:id AS uuid)",
                   p=price_monthly_micro, id=strategy_id)

    def next_version_number(self, conn: Any, strategy_id: str) -> int:
        row = self._one(conn, """SELECT coalesce(max(version), 0) + 1 AS n FROM strategy_versions
                                 WHERE strategy_id = CAST(:id AS uuid)""", id=strategy_id)
        return int(row["n"]) if row else 1

    def insert_version(self, conn: Any, *, strategy_id: str, version: int, code_hash: str, code_ciphertext: bytes,
                       params: dict, markets: list[str], timeframe: str, lookback: int, max_leverage: int,
                       backtest: dict) -> dict:
        row = self._one(conn, f"""
            WITH v AS (
                INSERT INTO strategy_versions (strategy_id, version, code_hash, code_ciphertext, params, markets,
                                               timeframe, lookback, max_leverage, backtest)
                VALUES (CAST(:s AS uuid), :ver, :h, :ct, CAST(:params AS jsonb), CAST(:m AS text[]), :tf, :lb, :ml,
                        CAST(:bt AS jsonb))
                RETURNING id, created_at, strategy_id, version, code_hash, params, markets, timeframe, lookback,
                          max_leverage, published_at, backtest, live_since)
            SELECT {_VERSION_COLS} FROM v""", s=strategy_id, ver=version, h=code_hash, ct=code_ciphertext,
                        params=_j(params), m=list(markets), tf=timeframe, lb=lookback, ml=max_leverage, bt=_j(backtest))
        assert row is not None
        return row

    def publish_version(self, conn: Any, version_id: str, now: datetime) -> int:
        return self._exec(conn, """UPDATE strategy_versions SET published_at = :t, live_since = :t
                                   WHERE id = CAST(:id AS uuid) AND published_at IS NULL RETURNING id""",
                          t=now, id=version_id)

    def active_subscribers_by_strategy(self, conn: Any, owner_id: str) -> list[dict]:
        return self._all(conn, """
            SELECT st.id AS strategy_id, st.slug,
                   count(DISTINCT s.user_id) FILTER (WHERE s.status IN ('active', 'past_due', 'reduce_only')) AS active_subscribers
              FROM strategies st LEFT JOIN subscriptions s ON s.strategy_id = st.id
             WHERE st.owner_user_id = CAST(:o AS uuid) GROUP BY st.id, st.slug ORDER BY st.slug""", o=owner_id)

    def list_creator_posts(self, conn: Any, creator_id: str, limit: int, cursor: Cursor) -> list[dict]:
        """The creator's own posts (any strategy or none), newest first, with sales count and gross sales."""
        return self._all(conn, """
            SELECT p.id, p.created_at, p.title, p.price_micro, p.published_at, p.body, st.slug AS strategy_slug,
                   (SELECT count(*) FROM post_purchases pp WHERE pp.post_id = p.id) AS sales,
                   (SELECT coalesce(sum(pp.price_micro), 0)::bigint FROM post_purchases pp
                     WHERE pp.post_id = p.id) AS gross_sales_micro
              FROM posts p LEFT JOIN strategies st ON st.id = p.strategy_id
             WHERE p.creator_id = CAST(:c AS uuid)
               AND (CAST(:cts AS timestamptz) IS NULL OR (p.created_at, p.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY p.created_at DESC, p.id DESC LIMIT :lim""", c=creator_id, lim=limit + 1, **_c(cursor))

    def creator_earnings_by_strategy(self, conn: Any, creator_id: str) -> list[dict]:
        """Credits to ``creator:{id}:payable`` grouped by (strategy_id, category). Categories: builder (builder-fee
        share, via fills.builder_fee_ledger_tx_id), subscription (start + renewals: key sub:{subscription}:…),
        profit_share (ps:{subscription}:{date}), posts (post:{post}:{buyer}; strategy NULL for general posts),
        other. strategy_id NULL = not attributable to one of the creator's strategies."""
        return self._all(conn, """
            WITH cr AS (
                SELECT t.id AS tx_id, t.kind, t.idempotency_key AS k, -e.amount_micro AS amt
                  FROM ledger_entries e
                  JOIN ledger_accounts a ON a.id = e.account_id
                  JOIN ledger_transactions t ON t.id = e.tx_id
                 WHERE a.code = :code AND e.amount_micro < 0
            ), src AS (
                SELECT cr.amt,
                       CASE WHEN cr.kind = 'builder_fee' THEN 'builder'
                            WHEN cr.kind IN ('subscription_start', 'subscription_renewal') THEN 'subscription'
                            WHEN cr.kind = 'profit_share' THEN 'profit_share'
                            WHEN cr.kind = 'post_purchase' THEN 'posts'
                            ELSE 'other' END AS cat,
                       CASE WHEN cr.kind = 'builder_fee' THEN
                                 (SELECT s.strategy_id FROM fills f JOIN subscriptions s ON s.id = f.subscription_id
                                   WHERE f.builder_fee_ledger_tx_id = cr.tx_id LIMIT 1)
                            WHEN cr.kind IN ('subscription_start', 'subscription_renewal', 'profit_share')
                                 AND split_part(cr.k, ':', 2) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                                 (SELECT s.strategy_id FROM subscriptions s
                                   WHERE s.id = CAST(split_part(cr.k, ':', 2) AS uuid))
                            WHEN cr.kind = 'post_purchase'
                                 AND split_part(cr.k, ':', 2) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                                 (SELECT p.strategy_id FROM posts p WHERE p.id = CAST(split_part(cr.k, ':', 2) AS uuid))
                       END AS strategy_id
                  FROM cr)
            SELECT strategy_id, cat, sum(amt)::bigint AS micro FROM src GROUP BY strategy_id, cat
             ORDER BY strategy_id NULLS LAST, cat""", code=f"creator:{creator_id}:payable")

    def get_kyc(self, conn: Any, user_id: str) -> Optional[dict]:
        return self._one(conn, """SELECT provider, provider_ref, status::text AS status FROM kyc_creators
                                  WHERE user_id = CAST(:u AS uuid)""", u=user_id)

    def set_kyc_status(self, conn: Any, user_id: str, status: str) -> int:
        return self._exec(conn, """UPDATE kyc_creators SET status = CAST(:s AS kyc_status)
                                   WHERE user_id = CAST(:u AS uuid) RETURNING id""", s=status, u=user_id)

    def upsert_kyc_pending(self, conn: Any, *, user_id: str, provider: str, provider_ref: str) -> None:
        self._exec(conn, """
            INSERT INTO kyc_creators (user_id, provider, provider_ref, status)
            VALUES (CAST(:u AS uuid), :p, :r, 'pending')
            ON CONFLICT (user_id) DO UPDATE SET provider = EXCLUDED.provider, provider_ref = EXCLUDED.provider_ref
             WHERE kyc_creators.status NOT IN ('approved', 'provider_approved')""", u=user_id, p=provider, r=provider_ref)

    # ------------------------------------------------------------------------------------------ admin
    def list_flags(self, conn: Any) -> list[dict]:
        return self._all(conn, """SELECT key, value, updated_by, updated_at, pending_value, pending_by, pending_at
                                  FROM system_flags ORDER BY key""")

    def get_flag(self, conn: Any, key: str, *, for_update: bool = False) -> Optional[dict]:
        sql = """SELECT key, value, updated_by, updated_at, pending_value, pending_by, pending_at
                 FROM system_flags WHERE key = :k"""
        if for_update:
            sql += " FOR UPDATE"
        return self._one(conn, sql, k=key)

    def set_flag(self, conn: Any, key: str, value: Any, by: str) -> None:
        """Apply now (protective direction, or a checked pending change). Clears any pending proposal."""
        self._exec(conn, """
            INSERT INTO system_flags (key, value, updated_by) VALUES (:k, CAST(:v AS jsonb), :by)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                pending_value = NULL, pending_by = NULL, pending_at = NULL""", k=key, v=_j(value), by=by)

    def propose_flag(self, conn: Any, key: str, value: Any, by: str, now: datetime) -> bool:
        """Maker step for lifting a switch (existing flag only). False if a proposal is already pending."""
        return self._exec(conn, """
            UPDATE system_flags SET pending_value = CAST(:v AS jsonb), pending_by = :by, pending_at = :t
             WHERE key = :k AND pending_value IS NULL
            RETURNING key""", k=key, v=_j(value), by=by, t=now) > 0

    def clear_flag_proposal(self, conn: Any, key: str) -> int:
        return self._exec(conn, """UPDATE system_flags SET pending_value = NULL, pending_by = NULL, pending_at = NULL
                                   WHERE key = :k AND pending_value IS NOT NULL RETURNING key""", k=key)

    def list_changes(self, conn: Any, status: Optional[str], limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, """
            SELECT id, created_at, kind, target, payload, reason, status, maker_admin, checker_admin, decided_at
              FROM admin_changes
             WHERE (CAST(:st AS text) IS NULL OR status = CAST(:st AS text))
               AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY created_at DESC, id DESC LIMIT :lim""", st=status, lim=limit + 1, **_c(cursor))

    def insert_change(self, conn: Any, *, kind: str, target: str, payload: dict, reason: str, maker: str) -> dict:
        row = self._one(conn, """
            INSERT INTO admin_changes (kind, target, payload, reason, maker_admin)
            VALUES (:k, :t, CAST(:p AS jsonb), :r, CAST(:m AS uuid))
            RETURNING id, created_at, kind, target, payload, reason, status, maker_admin, checker_admin, decided_at""",
                        k=kind, t=target, p=_j(payload), r=reason, m=maker)
        assert row is not None
        return row

    def open_change_for(self, conn: Any, kind: str, target: str) -> Optional[dict]:
        return self._one(conn, """SELECT id FROM admin_changes WHERE kind = :k AND target = :t AND status = 'pending'""",
                         k=kind, t=target)

    def get_change(self, conn: Any, change_id: str, *, for_update: bool = True) -> Optional[dict]:
        sql = """SELECT id, created_at, kind, target, payload, reason, status, maker_admin, checker_admin, decided_at
                 FROM admin_changes WHERE id = CAST(:id AS uuid)"""
        if for_update:
            sql += " FOR UPDATE"
        return self._one(conn, sql, id=change_id)

    def decide_change(self, conn: Any, change_id: str, *, status: str, checker: str, now: datetime,
                      decision_reason: str) -> Optional[dict]:
        """None when the change is no longer pending or checker == maker (four-eyes)."""
        row = self._one(conn, """
            UPDATE admin_changes SET status = :s, checker_admin = CAST(:c AS uuid), decided_at = :t,
                   decision_reason = :dr
             WHERE id = CAST(:id AS uuid) AND status = 'pending' AND maker_admin <> CAST(:c AS uuid)
            RETURNING id, created_at, kind, target, payload, reason, status, maker_admin, checker_admin, decided_at""",
                        s=status, c=checker, t=now, dr=decision_reason, id=change_id)
        return row

    def admin_list_strategies(self, conn: Any, status: Optional[str], limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, f"""
            SELECT {_STRAT_COLS}, k.status::text AS owner_kyc_status FROM strategies st
              LEFT JOIN kyc_creators k ON k.user_id = st.owner_user_id
             WHERE (CAST(:st AS text) IS NULL OR st.status::text = CAST(:st AS text))
               AND (CAST(:cts AS timestamptz) IS NULL OR (st.created_at, st.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY st.created_at DESC, st.id DESC LIMIT :lim""", st=status, lim=limit + 1, **_c(cursor))

    def pause_subscriptions_of_strategy(self, conn: Any, strategy_id: str) -> int:
        """Delist → live subscriptions go reduce_only (exits allowed, no new entries)."""
        return self._exec(conn, """UPDATE subscriptions SET status = 'reduce_only', status_changed_at = now()
                                   WHERE strategy_id = CAST(:id AS uuid) AND status IN ('pending', 'active', 'past_due')
                                   RETURNING id""",
                          id=strategy_id)

    # ------------------------------------------------------------------------------------------ held USDC (0009)
    # suspense:usdc_unattributed release, maker-checker (app/api/suspense.py; RUNBOOK §13.3). A held transfer is the
    # deposits-scan ledger transaction kind deposit_held, key usdc_hl:{hash}; usdc_held_deposits has its sender.
    _HELD_SELECT = """
        SELECT t.id AS held_tx_id, t.created_at, t.id, substr(t.idempotency_key, 9) AS tx_hash,
               (-e.amount_micro)::bigint AS amount_micro, t.memo, h.sender_address, h.reason, h.transfer_time,
               r.id AS release_id, r.status AS release_status, r.action AS release_action
          FROM ledger_transactions t
          JOIN ledger_entries e ON e.tx_id = t.id
          JOIN ledger_accounts a ON a.id = e.account_id AND a.code = 'suspense:usdc_unattributed'
          LEFT JOIN usdc_held_deposits h ON h.held_tx_id = t.id
          LEFT JOIN LATERAL (SELECT x.id, x.status, x.action FROM suspense_releases x
                              WHERE x.tx_hash = substr(t.idempotency_key, 9) AND x.status <> 'rejected'
                              ORDER BY x.created_at DESC LIMIT 1) r ON true
         WHERE t.kind = 'deposit_held' AND t.idempotency_key LIKE 'usdc\\_hl:%' AND e.amount_micro < 0"""
    _RELEASE_COLS = ("id, created_at, tx_hash, held_tx_id, amount_micro, action, user_id, sender_address, sender_source, "
                     "evidence, status, maker_admin, checker_admin, decided_at, decision_reason, release_tx_id, "
                     "refund_tx_hash, refund_ledger_tx_id, sent_by, sent_at")

    def list_held_deposits(self, conn: Any, *, open_only: bool, limit: int, cursor: Cursor) -> list[dict]:
        """Held transfers, newest first. ``open_only``: not yet released (no release, or one still proposed)."""
        return self._all(conn, self._HELD_SELECT + """
           AND (NOT CAST(:open AS boolean) OR r.id IS NULL OR r.status = 'proposed')
           AND (CAST(:cts AS timestamptz) IS NULL OR (t.created_at, t.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
         ORDER BY t.created_at DESC, t.id DESC LIMIT :lim""", open=bool(open_only), lim=limit + 1, **_c(cursor))

    def get_held_deposit(self, conn: Any, tx_hash: str) -> Optional[dict]:
        return self._one(conn, self._HELD_SELECT + " AND t.idempotency_key = :k", k="usdc_hl:" + tx_hash.lower())

    def suspense_balance(self, conn: Any) -> int:
        """Normal (credit-side) balance of suspense:usdc_unattributed = USDC held, not yet released."""
        row = self._one(conn, """SELECT coalesce(-sum(e.amount_micro), 0)::bigint AS s FROM ledger_entries e
                                   JOIN ledger_accounts a ON a.id = e.account_id
                                  WHERE a.code = 'suspense:usdc_unattributed'""")
        return int(row["s"]) if row else 0

    def insert_suspense_release(self, conn: Any, *, tx_hash: str, held_tx_id: str, amount_micro: int, action: str,
                                user_id: Optional[str], sender_address: str, sender_source: str, evidence: str,
                                maker: str) -> Optional[dict]:
        """None when a live (non-rejected) release for this transfer exists (partial unique index)."""
        return self._one(conn, f"""
            INSERT INTO suspense_releases (tx_hash, held_tx_id, amount_micro, action, user_id, sender_address,
                                           sender_source, evidence, maker_admin)
            VALUES (:h, CAST(:ht AS uuid), :a, :ac, CAST(:u AS uuid), :s, :src, :ev, CAST(:m AS uuid))
            ON CONFLICT (tx_hash) WHERE status <> 'rejected' DO NOTHING
            RETURNING {self._RELEASE_COLS}""", h=tx_hash.lower(), ht=held_tx_id, a=int(amount_micro), ac=action,
                         u=user_id, s=sender_address.lower(), src=sender_source, ev=evidence, m=maker)

    def get_suspense_release(self, conn: Any, release_id: str, *, for_update: bool = False) -> Optional[dict]:
        sql = f"SELECT {self._RELEASE_COLS} FROM suspense_releases WHERE id = CAST(:id AS uuid)"
        return self._one(conn, sql + (" FOR UPDATE" if for_update else ""), id=release_id)

    def list_suspense_releases(self, conn: Any, status: Optional[str], limit: int, cursor: Cursor) -> list[dict]:
        return self._all(conn, f"""
            SELECT {self._RELEASE_COLS} FROM suspense_releases
             WHERE (CAST(:st AS text) IS NULL OR status = CAST(:st AS text))
               AND (CAST(:cts AS timestamptz) IS NULL OR (created_at, id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid)))
             ORDER BY created_at DESC, id DESC LIMIT :lim""", st=status, lim=limit + 1, **_c(cursor))

    def approve_suspense_release(self, conn: Any, release_id: str, *, checker: str, now: datetime, reason: str,
                                 release_tx_id: str) -> Optional[dict]:
        """None unless still proposed and checker ≠ maker (four-eyes; the DB CHECK enforces it too)."""
        return self._one(conn, f"""
            UPDATE suspense_releases SET status = 'approved', checker_admin = CAST(:c AS uuid), decided_at = :t,
                   decision_reason = :r, release_tx_id = CAST(:tx AS uuid)
             WHERE id = CAST(:id AS uuid) AND status = 'proposed' AND maker_admin <> CAST(:c AS uuid)
            RETURNING {self._RELEASE_COLS}""", c=checker, t=now, r=reason, tx=release_tx_id, id=release_id)

    def reject_suspense_release(self, conn: Any, release_id: str, *, checker: str, now: datetime,
                                reason: str) -> Optional[dict]:
        return self._one(conn, f"""
            UPDATE suspense_releases SET status = 'rejected', checker_admin = CAST(:c AS uuid), decided_at = :t,
                   decision_reason = :r
             WHERE id = CAST(:id AS uuid) AND status = 'proposed' AND maker_admin <> CAST(:c AS uuid)
            RETURNING {self._RELEASE_COLS}""", c=checker, t=now, r=reason, id=release_id)

    def mark_suspense_refund_sent(self, conn: Any, release_id: str, *, refund_tx_hash: str, ledger_tx_id: str,
                                  admin: str, now: datetime) -> Optional[dict]:
        return self._one(conn, f"""
            UPDATE suspense_releases SET status = 'sent', refund_tx_hash = :h, refund_ledger_tx_id = CAST(:tx AS uuid),
                   sent_by = CAST(:a AS uuid), sent_at = :t
             WHERE id = CAST(:id AS uuid) AND status = 'approved' AND action = 'refund'
            RETURNING {self._RELEASE_COLS}""", h=refund_tx_hash.lower(), tx=ledger_tx_id, a=admin, t=now, id=release_id)


    # ------------------------------------------------------------------------------------------ trusted dexes (0012)
    # SPEC §12 / REVIEW_TRADING_KEYS F1: SQL lives in app.strategies.dexes (shared with the executor + signal ingest).
    def trusted_dexes(self, conn: Any) -> frozenset[str]:
        """Active builder dexes plus the validator dex ''. Raises on DB errors (callers fail closed)."""
        from app.strategies.dexes import load_trusted
        return load_trusted(self._runner(conn))

    def list_trusted_dexes(self, conn: Any) -> list[dict]:
        from app.strategies.dexes import list_dexes
        return list_dexes(self._runner(conn))

    def add_trusted_dex(self, conn: Any, dex: str, *, by: str, reason: str) -> Optional[dict]:
        from app.strategies.dexes import add_dex
        return add_dex(self._runner(conn), dex, by=by, reason=reason)

    def remove_trusted_dex(self, conn: Any, dex: str, *, by: str, reason: str) -> Optional[dict]:
        from app.strategies.dexes import remove_dex
        return remove_dex(self._runner(conn), dex, by=by, reason=reason)

    def strategies_on_dex(self, conn: Any, dex: str) -> list[dict]:
        """Strategies (any status but delisted) with at least one market on builder dex ``dex``."""
        return self._all(conn, """
            SELECT st.id, st.slug, st.status::text AS status, st.markets FROM strategies st
             WHERE st.status::text <> 'delisted'
               AND EXISTS (SELECT 1 FROM unnest(st.markets) AS m(coin)
                            WHERE position(':' in m.coin) > 0 AND split_part(m.coin, ':', 1) = CAST(:d AS text))
             ORDER BY st.slug""", d=dex)

    # ------------------------------------------------------------------------------------------ deposit scan requests
    def request_deposit_scan(self, conn: Any, user_id: str, *, since: datetime, now: datetime) -> None:
        """AUTH F1: POST /deposits/usdc/confirm records a wake-up hint instead of scanning Hyperliquid inline."""
        self._exec(conn, """
            INSERT INTO deposit_scan_requests (user_id, requested_at, since, served_at)
            VALUES (CAST(:u AS uuid), CAST(:t AS timestamptz), CAST(:s AS timestamptz), NULL)
            ON CONFLICT (user_id) DO UPDATE SET requested_at = EXCLUDED.requested_at,
                   since = LEAST(CASE WHEN deposit_scan_requests.served_at IS NULL THEN deposit_scan_requests.since
                                      ELSE EXCLUDED.since END, EXCLUDED.since),
                   served_at = NULL
            RETURNING user_id""", u=user_id, t=now, s=since)

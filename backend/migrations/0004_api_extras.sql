-- =====================================================================================================
-- 0004_api_extras.sql — tables the HTTP API needs beyond SPEC §4 (owner: app/api).
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
-- Explicit GRANTs per table; nobody gets DELETE (retention = partition/archive jobs run by app_migrator).
--
--   api_idempotency       Idempotency-Key ledger for money-moving POSTs: (user, key) → request fingerprint +
--                         stored response. Claimed in the SAME transaction as the business write, so a crash
--                         or error leaves no claim behind and a concurrent duplicate waits on the unique key.
--   admin_changes         maker-checker queue for permissive admin actions other than system flags
--                         (system_flags.pending_* is used for kill switches): strategy_list, strategy_price,
--                         user_unsuspend. checker ≠ maker enforced here too; decided rows are immutable.
--   user_login_countries  first/last seen country per user (CF-IPCountry, only when the edge is authenticated)
--                         → "login from new country" alert (SPEC §5.5).
--   wallet_nonces         single-use nonces (10 min) for SIWE-style wallet ownership proofs.
-- =====================================================================================================

CREATE TABLE api_idempotency (
    user_id       uuid NOT NULL REFERENCES users(id),
    idem_key      text NOT NULL CHECK (idem_key ~ '^[A-Za-z0-9_:.-]{16,128}$'),
    scope         text NOT NULL CHECK (length(scope) BETWEEN 1 AND 200),       -- "POST /withdrawals"
    fingerprint   text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),       -- sha256 of method+path+body
    status_code   integer CHECK (status_code IS NULL OR status_code BETWEEN 200 AND 299),
    response      jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,
    PRIMARY KEY (user_id, idem_key),
    CONSTRAINT api_idempotency_complete CHECK ((response IS NULL) = (completed_at IS NULL))
);
CREATE INDEX api_idempotency_created_idx ON api_idempotency (created_at);

-- A stored response is final: only the first completion may write it.
CREATE FUNCTION api_idempotency_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.completed_at IS NOT NULL THEN
        RAISE EXCEPTION 'api_idempotency row is final' USING ERRCODE = 'AJ403';
    END IF;
    IF NEW.user_id <> OLD.user_id OR NEW.idem_key <> OLD.idem_key OR NEW.scope <> OLD.scope
       OR NEW.fingerprint <> OLD.fingerprint OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'api_idempotency identity columns are immutable' USING ERRCODE = 'AJ422';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER api_idempotency_guard BEFORE UPDATE ON api_idempotency
    FOR EACH ROW EXECUTE FUNCTION api_idempotency_guard();

CREATE TABLE admin_changes (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    kind             text NOT NULL CHECK (kind IN ('strategy_list', 'strategy_price', 'user_unsuspend')),
    target           text NOT NULL CHECK (target ~ '^(strategy|user):[0-9a-f-]{36}$'),
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason           text NOT NULL CHECK (length(reason) BETWEEN 5 AND 500),
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    maker_admin      uuid NOT NULL REFERENCES users(id),
    checker_admin    uuid REFERENCES users(id),
    decided_at       timestamptz,
    decision_reason  text CHECK (decision_reason IS NULL OR length(decision_reason) BETWEEN 5 AND 500),
    CONSTRAINT admin_changes_four_eyes CHECK (checker_admin IS NULL OR checker_admin <> maker_admin),
    CONSTRAINT admin_changes_decided CHECK ((status = 'pending') = (checker_admin IS NULL AND decided_at IS NULL))
);
CREATE UNIQUE INDEX admin_changes_one_pending ON admin_changes (kind, target) WHERE status = 'pending';
CREATE INDEX admin_changes_status_idx ON admin_changes (status, created_at DESC);

-- Only the pending → approved|rejected transition is allowed, and nothing else may change.
CREATE FUNCTION admin_changes_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'admin_changes row % is already decided', OLD.id USING ERRCODE = 'AJ403';
    END IF;
    IF NEW.kind <> OLD.kind OR NEW.target <> OLD.target OR NEW.payload <> OLD.payload OR NEW.reason <> OLD.reason
       OR NEW.maker_admin <> OLD.maker_admin OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'admin_changes proposal fields are immutable' USING ERRCODE = 'AJ422';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER admin_changes_guard BEFORE UPDATE ON admin_changes
    FOR EACH ROW EXECUTE FUNCTION admin_changes_guard();

CREATE TABLE user_login_countries (
    user_id     uuid NOT NULL REFERENCES users(id),
    country     text NOT NULL CHECK (country ~ '^[A-Z0-9]{2}$'),
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, country)
);

CREATE TABLE wallet_nonces (
    nonce       text PRIMARY KEY CHECK (nonce ~ '^[A-Za-z0-9]{16,64}$'),
    user_id     uuid NOT NULL REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    CONSTRAINT wallet_nonces_expiry CHECK (expires_at > created_at AND expires_at <= created_at + interval '1 hour')
);
CREATE INDEX wallet_nonces_user_idx ON wallet_nonces (user_id, created_at DESC);

-- ---------------------------------------------------------------- grants (no DELETE for anyone)
GRANT SELECT, INSERT, UPDATE ON api_idempotency, admin_changes, user_login_countries, wallet_nonces TO app_api;
GRANT SELECT ON admin_changes TO app_executor;
GRANT ALL ON api_idempotency, admin_changes, user_login_countries, wallet_nonces TO app_migrator;

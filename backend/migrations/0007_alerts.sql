-- =====================================================================================================
-- 0007_alerts.sql — user alert contacts (Telegram + email), per-kind mute preferences and per-channel
-- delivery bookkeeping (owner: app/alerts; SPEC §12 "User alerts on Telegram + email", "Email volume policy").
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
-- Explicit GRANTs per table; nobody gets DELETE (retention = archive jobs run by app_migrator).
--
--   user_contacts             one row per user: linked Telegram chat, confirmed alert email.
--                             Telegram states: linked (chat_id, blocked_at NULL) · blocked (403 from Telegram:
--                             chat_id kept, blocked_at + reason 'blocked'; a tokenless /start from the same chat
--                             re-activates) · stopped (/stop: chat_id cleared, blocked_at + reason 'stopped').
--                             Email: the account email by default (confirmed when the identity provider
--                             verified it); a change needs a 6-digit code sent to the new address + step-up.
--   telegram_link_tokens      one-time /start tokens (sha256 only; 10 min; single use; bound to a user).
--   email_verification_codes  6-digit codes (HMAC-SHA256 with the audit pepper; 10 min; ≤ 5 attempts;
--                             at most one live code per user).
--   alert_prefs               per user × kind mute switch. Mandatory kinds can never be muted (CHECK below
--                             mirrors app/alerts/prefs.py MANDATORY_KINDS — change both together).
--   alert_deliveries          (alert, channel) → status/attempts/backoff; UNIQUE(alert_id, channel) makes
--                             delivery idempotent. Events from events_outbox (0006) are first materialised
--                             into `alerts` (dedup_key 'outbox:<id>'), so every delivery is keyed on alerts.id.
--
-- Executor contract (entries gate): alert_contacts_entries_allowed(user, now) is FALSE when the user has no
-- confirmed email or no working Telegram link, except during the 24 h grace after the link lapsed (blocked /
-- stopped). The executor must treat such users' subscriptions like reduce_only for NEW entries (exits still run).
-- =====================================================================================================

CREATE TABLE user_contacts (
    user_id                uuid PRIMARY KEY REFERENCES users(id),
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    telegram_chat_id       bigint,
    telegram_linked_at     timestamptz,
    telegram_blocked_at    timestamptz,
    telegram_block_reason  text CHECK (telegram_block_reason IN ('blocked', 'stopped', 'chat_not_found')),
    email                  text CHECK (email IS NULL OR (length(email) BETWEEN 3 AND 254
                                                         AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$')),
    email_verified_at      timestamptz,
    CONSTRAINT user_contacts_tg_link CHECK ((telegram_chat_id IS NULL) = (telegram_linked_at IS NULL)),
    CONSTRAINT user_contacts_tg_block CHECK ((telegram_blocked_at IS NULL) = (telegram_block_reason IS NULL)),
    CONSTRAINT user_contacts_tg_stopped CHECK (telegram_block_reason IS DISTINCT FROM 'stopped' OR telegram_chat_id IS NULL),
    CONSTRAINT user_contacts_email_verified CHECK (email_verified_at IS NULL OR email IS NOT NULL)
);
CREATE INDEX user_contacts_chat_idx ON user_contacts (telegram_chat_id) WHERE telegram_chat_id IS NOT NULL;

CREATE TABLE telegram_link_tokens (
    token_hash    text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),   -- sha256(token); token never stored
    user_id       uuid NOT NULL REFERENCES users(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    used_at       timestamptz,
    used_chat_id  bigint,
    CONSTRAINT telegram_link_tokens_expiry CHECK (expires_at > created_at AND expires_at <= created_at + interval '15 minutes'),
    CONSTRAINT telegram_link_tokens_used CHECK ((used_at IS NULL) = (used_chat_id IS NULL))
);
CREATE INDEX telegram_link_tokens_user_idx ON telegram_link_tokens (user_id, created_at DESC);

CREATE TABLE email_verification_codes (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES users(id),
    email          text NOT NULL CHECK (length(email) BETWEEN 3 AND 254
                                        AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'),
    code_hash      text NOT NULL CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    attempts       integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    consumed_at    timestamptz,
    superseded_at  timestamptz,
    CONSTRAINT email_codes_expiry CHECK (expires_at > created_at AND expires_at <= created_at + interval '15 minutes')
);
CREATE INDEX email_codes_user_idx ON email_verification_codes (user_id, created_at DESC);
CREATE UNIQUE INDEX email_codes_one_live ON email_verification_codes (user_id)
    WHERE consumed_at IS NULL AND superseded_at IS NULL;

-- Codes/tokens: identity columns immutable; attempts only grow; consumed/superseded/used are write-once.
CREATE FUNCTION alerts_onetime_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'email_verification_codes' THEN
        IF NEW.user_id <> OLD.user_id OR NEW.email <> OLD.email OR NEW.code_hash <> OLD.code_hash
           OR NEW.created_at <> OLD.created_at OR NEW.expires_at <> OLD.expires_at THEN
            RAISE EXCEPTION 'email code identity columns are immutable' USING ERRCODE = 'AJ422';
        END IF;
        IF NEW.attempts < OLD.attempts
           OR (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at)
           OR (OLD.superseded_at IS NOT NULL AND NEW.superseded_at IS DISTINCT FROM OLD.superseded_at) THEN
            RAISE EXCEPTION 'email code state is write-once' USING ERRCODE = 'AJ403';
        END IF;
    ELSE
        IF NEW.token_hash <> OLD.token_hash OR NEW.user_id <> OLD.user_id OR NEW.created_at <> OLD.created_at
           OR NEW.expires_at <> OLD.expires_at THEN
            RAISE EXCEPTION 'link token identity columns are immutable' USING ERRCODE = 'AJ422';
        END IF;
        IF OLD.used_at IS NOT NULL THEN
            RAISE EXCEPTION 'link token already used' USING ERRCODE = 'AJ403';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER email_codes_guard BEFORE UPDATE ON email_verification_codes
    FOR EACH ROW EXECUTE FUNCTION alerts_onetime_guard();
CREATE TRIGGER telegram_link_tokens_guard BEFORE UPDATE ON telegram_link_tokens
    FOR EACH ROW EXECUTE FUNCTION alerts_onetime_guard();

CREATE TABLE alert_prefs (
    user_id     uuid NOT NULL REFERENCES users(id),
    kind        text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]{1,63}$'),
    muted       boolean NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind),
    -- mandatory (*) kinds, SPEC §12 — keep in sync with app/alerts/prefs.py MANDATORY_KINDS
    CONSTRAINT alert_prefs_mandatory_unmutable CHECK (NOT muted OR kind NOT IN (
        'agent_expiring', 'agent_expired', 'agent_revoked', 'builder_approval_missing',
        'balance_low', 'balance_empty', 'subscription_past_due', 'subscription_reduce_only',
        'stripe_refund', 'stripe_dispute', 'deposit_refunded', 'deposit_disputed',
        'withdrawal_requested', 'withdrawal_sent', 'withdrawal_rejected',
        'new_device_login', 'login_new_country', 'new_country_login', 'mfa_changed', 'mfa_reset',
        'market_paused', 'strategy_paused', 'telegram_unreachable', 'alert_email_changed'))
);

CREATE TABLE alert_deliveries (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id         uuid NOT NULL REFERENCES alerts(id),
    channel          text NOT NULL CHECK (channel IN ('telegram', 'email', 'fanout')),
    status           text NOT NULL CHECK (status IN ('sending', 'retry', 'sent', 'failed', 'skipped')),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 100),
    next_attempt_at  timestamptz,                     -- 'sending': lease expiry; 'retry': backoff due time
    last_error       text CHECK (last_error IS NULL OR length(last_error) <= 300),
    sent_at          timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT alert_deliveries_once UNIQUE (alert_id, channel),
    CONSTRAINT alert_deliveries_due CHECK (status NOT IN ('sending', 'retry') OR next_attempt_at IS NOT NULL),
    CONSTRAINT alert_deliveries_sent CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);
CREATE INDEX alert_deliveries_due_idx ON alert_deliveries (next_attempt_at) WHERE status IN ('sending', 'retry');
-- the delivery worker scans recent USER alerts (ops alerts have user_id NULL)
CREATE INDEX alerts_user_recent_idx ON alerts (created_at) WHERE user_id IS NOT NULL;

-- A terminal delivery (sent / failed / skipped) is final.
CREATE FUNCTION alert_deliveries_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IN ('sent', 'failed', 'skipped') THEN
        RAISE EXCEPTION 'alert delivery % is final (%)', OLD.id, OLD.status USING ERRCODE = 'AJ403';
    END IF;
    IF NEW.alert_id <> OLD.alert_id OR NEW.channel <> OLD.channel OR NEW.created_at <> OLD.created_at
       OR NEW.attempts < OLD.attempts THEN
        RAISE EXCEPTION 'alert delivery identity columns are immutable' USING ERRCODE = 'AJ422';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER alert_deliveries_guard BEFORE UPDATE ON alert_deliveries
    FOR EACH ROW EXECUTE FUNCTION alert_deliveries_guard();

-- Entries gate for the executor (and the API): may this user's subscriptions OPEN new positions?
-- TRUE only with a confirmed email AND (a working Telegram link OR a link that lapsed < 24 h ago).
CREATE FUNCTION alert_contacts_entries_allowed(p_user uuid, p_now timestamptz DEFAULT now()) RETURNS boolean
LANGUAGE sql STABLE
AS $$
    SELECT coalesce((
        SELECT c.email_verified_at IS NOT NULL
               AND ((c.telegram_chat_id IS NOT NULL AND c.telegram_blocked_at IS NULL)
                    OR (c.telegram_blocked_at IS NOT NULL AND c.telegram_blocked_at > p_now - interval '24 hours'))
          FROM user_contacts c
         WHERE c.user_id = p_user), false)
$$;

-- ---------------------------------------------------------------- grants (no DELETE for anyone)
GRANT SELECT, INSERT, UPDATE ON user_contacts, telegram_link_tokens, email_verification_codes, alert_prefs TO app_api;
GRANT SELECT ON alert_deliveries TO app_api;

GRANT SELECT ON user_contacts, alert_prefs TO app_executor;
GRANT UPDATE (telegram_blocked_at, telegram_block_reason, updated_at) ON user_contacts TO app_executor;
GRANT SELECT, INSERT, UPDATE ON alert_deliveries TO app_executor;

REVOKE EXECUTE ON FUNCTION alert_contacts_entries_allowed(uuid, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION alert_contacts_entries_allowed(uuid, timestamptz) TO app_api, app_executor, app_migrator;

GRANT ALL ON user_contacts, telegram_link_tokens, email_verification_codes, alert_prefs, alert_deliveries TO app_migrator;

-- events_outbox is created by 0006 (data jobs). The delivery worker (executor role) reads it and stamps
-- delivered_at once an event is materialised into `alerts`. Grant only when the table exists.
DO $$
BEGIN
    IF to_regclass('public.events_outbox') IS NOT NULL THEN
        EXECUTE 'GRANT SELECT ON events_outbox TO app_executor';
        EXECUTE 'GRANT UPDATE (delivered_at) ON events_outbox TO app_executor';
    END IF;
END
$$;

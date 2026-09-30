-- =====================================================================================================
-- migrate:session-user
-- 0001_init.sql — aijalon.trade core schema (docs/SPEC.md §4 is the contract).
-- Applied by backend/scripts/migrate.py inside ONE transaction. Do not add BEGIN/COMMIT here.
-- Never edit this file after it has been applied anywhere: migrate.py refuses changed checksums.
--
-- LEDGER SIGN CONVENTION (read this before writing any money code)
-- -----------------------------------------------------------------
--   * ledger_entries.amount_micro: POSITIVE = DEBIT, NEGATIVE = CREDIT (integer micro-USD).
--   * Every ledger transaction has >= 2 entries and Σ amount_micro = 0 (checked at COMMIT).
--   * Raw balance of an account = Σ amount_micro (view ledger_balances.balance_micro).
--       asset / expense accounts   (debit-normal):  raw balance >= 0 normally (what we hold / spent).
--       liability / revenue accts  (credit-normal): raw balance <= 0 normally (what we OWE / earned).
--   * normal_balance_micro (also in ledger_balances) = raw for asset|expense, −raw for liability|revenue,
--     i.e. the "human" positive number. A user's spendable fee balance = −(raw balance of
--     user:{id}:fee_balance) = normal_balance_micro.
--   * NON-NEGATIVE accounts (ledger_accounts.non_negative; always true for user:{id}:fee_balance, and set by
--     app.ledger for creator:/referrer: payables): a transaction may not DECREASE such an account's
--     normal balance to below zero (SQLSTATE AJ402), checked at COMMIT. Top-ups onto a negative balance are
--     always allowed. The only kinds allowed to overdraw (money already left / is owed regardless) are
--     ledger_kind_allows_overdraft(): 'profit_share', 'stripe_refund', 'stripe_dispute'. A negative fee
--     balance = the user owes us; billing moves their subscriptions to past_due -> reduce_only.
--   Examples:
--     USDC top-up $10:      treasury:hl_usdc +10_000_000 | user:{id}:fee_balance −10_000_000
--     subscription $20:     user:{id}:fee_balance +20_000_000 | creator:{c}:payable −19_400_000
--                           | platform:revenue:subscription −600_000
--     creator payout $50:   creator:{c}:payable +50_000_000 | treasury:hl_usdc −50_000_000
--
-- CUSTOM SQLSTATEs raised here (mapped to app.errors by app.db / app.ledger):
--   AJ402 non-negative account would go negative (InsufficientBalance)
--   AJ403 append-only table mutated (UPDATE/DELETE/TRUNCATE)
--   AJ404 unknown ledger account (NotFound)
--   AJ409 idempotency key reused with different content; stale prev_hash (Conflict)
--   AJ422 invalid / unbalanced ledger transaction, immutable field changed, bad hash (ValidationFailed)
--
-- HASH CHAINS (ledger_transactions, audit_log) — same construction as app/security/audit.py:
--   hash_n = hex(sha256( prev_hash_n || "\n" || canonical_json(body_n) )), prev_hash_0 = "0"*64,
--   hashes stored as 64-char lower-case hex TEXT. canonical_json = sorted keys, no whitespace, UTF-8
--   (json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)); SQL twin: canonical_json(jsonb).
--   audit_log body   = {actor, action, target, payload, ip_hash, created_at}
--   ledger body      = {id, idempotency_key, kind, memo, created_by, created_at, entries_digest}
--   created_at text  = UTC ISO-8601 with microseconds and "+00:00" (Python isoformat(timespec="microseconds")).
--   A BEFORE INSERT trigger takes the chain's transaction-scoped advisory lock
--   (pg_advisory_xact_lock(hashtext('aijalon.audit_log' | 'aijalon.ledger'))), sets seq = last + 1 and
--   prev_hash = last hash, and computes hash. A writer MAY pre-compute prev_hash/hash (audit.py does); they
--   must then match exactly or the insert fails (AJ409 stale prev_hash / AJ422 hash mismatch). created_at
--   may be supplied but must be within 10 minutes of the DB clock.
--   UNIQUE(seq), UNIQUE(prev_hash) make forks impossible even under REPEATABLE READ (a stale writer fails
--   with 23505 and must retry). verify_chain() recomputes everything; it returns NO ROWS when intact,
--   otherwise the FIRST broken row per chain. Deleting the newest rows is only detectable against an
--   external anchor: reconcile should store/publish chain_heads daily.
-- =====================================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid() (core in PG13+, extension kept for parity)

-- ---------------------------------------------------------------- domains & enums
CREATE DOMAIN eth_address AS text CHECK (VALUE ~ '^0x[0-9a-f]{40}$');
CREATE DOMAIN sha256_hex  AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE TYPE user_role            AS ENUM ('user', 'creator', 'admin');
CREATE TYPE user_plan            AS ENUM ('free', 'pro', 'max');
CREATE TYPE user_status          AS ENUM ('active', 'suspended', 'closed');
CREATE TYPE referral_tier        AS ENUM ('starter', 'partner', 'elite');
CREATE TYPE consent_doc          AS ENUM ('terms', 'risk', 'privacy', 'jurisdiction', 'waiver', 'creator_agreement', 'subscription_ack');
CREATE TYPE consent_context      AS ENUM ('site_entry', 'subscribe', 'creator');
CREATE TYPE agent_key_status     AS ENUM ('pending_approval', 'active', 'revoked', 'rotated');
CREATE TYPE strategy_status      AS ENUM ('draft', 'review', 'listed', 'paused', 'delisted');
CREATE TYPE subscription_status  AS ENUM ('pending', 'active', 'past_due', 'reduce_only', 'paused_user', 'closing', 'cancelled');
CREATE TYPE cancel_positions_mode AS ENUM ('close', 'leave');   -- SPEC §12 cancel flow
CREATE TYPE signal_source        AS ENUM ('sandbox', 'terminal');
CREATE TYPE order_side           AS ENUM ('buy', 'sell');       -- Hyperliquid fills: 'B' -> buy, 'A' -> sell
-- = app.execution.ports ORDER_* constants (+ 'cancelled')
CREATE TYPE order_status         AS ENUM ('submitting', 'filled', 'partial', 'rejected', 'resting', 'not_submitted', 'unknown', 'cancelled');
CREATE TYPE ledger_account_kind  AS ENUM ('asset', 'liability', 'revenue', 'expense');
CREATE TYPE deposit_method       AS ENUM ('usdc_hl', 'stripe');
CREATE TYPE deposit_status       AS ENUM ('pending', 'credited', 'failed', 'reversed');
CREATE TYPE payout_status        AS ENUM ('requested', 'approved_1', 'approved_2', 'sent', 'rejected');
CREATE TYPE alert_severity       AS ENUM ('info', 'warn', 'critical');
CREATE TYPE kyc_status           AS ENUM ('pending', 'approved', 'rejected');

-- Hyperliquid perp coin: "BTC", "kPEPE", "xyz:SILVER" (builder dex prefix is lower-case).
CREATE FUNCTION is_valid_coin(c text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT c ~ '^([a-z0-9]{1,16}:)?[A-Za-z0-9]{1,32}$' $$;

CREATE FUNCTION are_valid_coins(cs text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT coalesce(bool_and(is_valid_coin(c)), true) FROM unnest(cs) AS c $$;

-- Deterministic JSON text identical to Python json.dumps(obj, sort_keys=True, separators=(",", ":"),
-- ensure_ascii=False) for str/int/bool/null/list/dict values (floats are not canonical: never hash floats).
-- Key order = code-point order (COLLATE "C" on UTF-8 == Python str ordering).
CREATE FUNCTION canonical_json(j jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
AS $$
DECLARE
    t text;
BEGIN
    IF j IS NULL THEN
        RETURN 'null';
    END IF;
    t := jsonb_typeof(j);
    IF t = 'object' THEN
        RETURN '{' || coalesce((SELECT string_agg(to_json(k)::text || ':' || canonical_json(v), ',' ORDER BY k COLLATE "C")
                                 FROM jsonb_each(j) AS e(k, v)), '') || '}';
    ELSIF t = 'array' THEN
        RETURN '[' || coalesce((SELECT string_agg(canonical_json(v), ',' ORDER BY o)
                                 FROM jsonb_array_elements(j) WITH ORDINALITY AS e(v, o)), '') || ']';
    ELSIF t = 'string' THEN
        RETURN to_json(j #>> '{}')::text;
    ELSE
        RETURN j::text;   -- number | boolean | null
    END IF;
END
$$;

CREATE FUNCTION utc_iso(ts timestamptz) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') $$;

-- Generic guard for append-only tables.
CREATE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'AJ403';
END
$$;

CREATE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

-- ---------------------------------------------------------------- users & compliance
CREATE TABLE users (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    firebase_uid          text NOT NULL UNIQUE,
    email                 text,
    display_name          text,
    role                  user_role   NOT NULL DEFAULT 'user',
    plan                  user_plan   NOT NULL DEFAULT 'free',
    plan_started_at       timestamptz,                         -- renewal anchor (settlement PlanAccount.anchor)
    plan_period_end       timestamptz,
    plan_past_due_since   timestamptz,
    country_attested      text CHECK (country_attested ~ '^[A-Z]{2}$'),
    referral_code         text UNIQUE CHECK (referral_code ~ '^[A-Za-z0-9_-]{3,32}$'),
    referred_by           uuid REFERENCES users(id),
    referral_tier         referral_tier NOT NULL DEFAULT 'starter',
    device_fp_hash        text,                                -- self-referral check (§1.2)
    status                user_status NOT NULL DEFAULT 'active',
    mfa_enrolled          boolean NOT NULL DEFAULT false,
    CONSTRAINT users_no_self_referral CHECK (referred_by IS NULL OR referred_by <> id)
);
CREATE INDEX users_referred_by_idx ON users (referred_by) WHERE referred_by IS NOT NULL;
CREATE INDEX users_email_idx ON users (lower(email));
CREATE INDEX users_plan_due_idx ON users (plan_period_end) WHERE plan <> 'free';

-- A referral binds once and is immutable afterwards (§1.2).
CREATE FUNCTION users_referral_immutable() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.referred_by IS NOT NULL AND NEW.referred_by IS DISTINCT FROM OLD.referred_by THEN
        RAISE EXCEPTION 'users.referred_by is immutable once set' USING ERRCODE = 'AJ422';
    END IF;
    IF OLD.referral_code IS NOT NULL AND NEW.referral_code IS DISTINCT FROM OLD.referral_code THEN
        RAISE EXCEPTION 'users.referral_code is immutable once set' USING ERRCODE = 'AJ422';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER users_referral_immutable BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION users_referral_immutable();
CREATE TRIGGER users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE consents (                                       -- append-only
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    user_id          uuid NOT NULL REFERENCES users(id),
    doc              consent_doc NOT NULL,
    doc_version      text NOT NULL CHECK (length(doc_version) BETWEEN 1 AND 64),
    doc_text_sha256  text NOT NULL CHECK (doc_text_sha256 ~ '^[0-9a-f]{64}$'),   -- exact text accepted (evidence)
    context          consent_context NOT NULL,
    strategy_id      uuid,                                    -- FK added after strategies
    ip_hash          text,
    user_agent_hash  text,
    accepted_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX consents_user_doc_idx ON consents (user_id, doc, accepted_at DESC);

CREATE TABLE wallets (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    user_id         uuid NOT NULL REFERENCES users(id),
    master_address  eth_address NOT NULL UNIQUE,
    verified_at     timestamptz
);
CREATE INDEX wallets_user_idx ON wallets (user_id);

CREATE TABLE agent_keys (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    user_id          uuid NOT NULL REFERENCES users(id),
    master_address   eth_address NOT NULL,
    agent_address    eth_address NOT NULL UNIQUE,
    agent_name       text NOT NULL DEFAULT 'aijalon',
    key_ciphertext   bytea NOT NULL,                          -- KMS envelope; app_api has NO SELECT on it
    kms_key_version  text NOT NULL,
    status           agent_key_status NOT NULL DEFAULT 'pending_approval',
    approved_at      timestamptz,
    revoked_at       timestamptz
);
CREATE INDEX agent_keys_user_idx ON agent_keys (user_id);
-- one live agent per trading master address (one unique agent per user/account, §5.8)
CREATE UNIQUE INDEX agent_keys_one_live_per_master ON agent_keys (master_address)
    WHERE status IN ('pending_approval', 'active');

CREATE TABLE builder_approvals (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at              timestamptz NOT NULL DEFAULT now(),
    user_id                 uuid NOT NULL REFERENCES users(id),
    master_address          eth_address NOT NULL,
    max_fee_rate_tenths_bp  integer NOT NULL CHECK (max_fee_rate_tenths_bp BETWEEN 0 AND 1000),
    verified_on_chain_at    timestamptz
);
CREATE INDEX builder_approvals_master_idx ON builder_approvals (master_address, created_at DESC);
CREATE INDEX builder_approvals_user_idx ON builder_approvals (user_id);

CREATE TABLE kyc_creators (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    user_id       uuid NOT NULL UNIQUE REFERENCES users(id),
    provider      text NOT NULL,
    provider_ref  text NOT NULL,
    status        kyc_status NOT NULL DEFAULT 'pending',
    UNIQUE (provider, provider_ref)
);

-- ---------------------------------------------------------------- strategies
CREATE TABLE strategies (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    slug                 text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name                 text NOT NULL,
    owner_user_id        uuid REFERENCES users(id),           -- NULL only for in-house strategies
    in_house             boolean NOT NULL DEFAULT false,
    markets              text[] NOT NULL DEFAULT '{}' CHECK (are_valid_coins(markets) AND cardinality(markets) <= 5),
    timeframe            text NOT NULL DEFAULT '1d' CHECK (timeframe IN ('1h', '4h', '1d')),
    price_monthly_micro  bigint CHECK (price_monthly_micro >= 0),          -- NULL = not priced yet
    profit_share_bps     integer CHECK (profit_share_bps BETWEEN 0 AND 1200), -- creator cap 12% (owner 30 Sep 2026); NULL = not set yet
    status               strategy_status NOT NULL DEFAULT 'draft',
    description          text,
    CONSTRAINT strategies_owner_or_in_house CHECK (in_house OR owner_user_id IS NOT NULL),
    -- Nothing can be listed (sold / executed) until it is priced and has 1..5 markets. In-house strategies
    -- are seeded in 'review' with NULL prices; an admin sets prices then lists (0003_seed.sql).
    CONSTRAINT strategies_listed_requires_terms CHECK (
        status <> 'listed'
        OR (price_monthly_micro IS NOT NULL AND profit_share_bps IS NOT NULL AND cardinality(markets) BETWEEN 1 AND 5)
    )
);
CREATE INDEX strategies_owner_idx ON strategies (owner_user_id);
CREATE INDEX strategies_status_idx ON strategies (status);
CREATE TRIGGER strategies_touch BEFORE UPDATE ON strategies
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

ALTER TABLE consents ADD CONSTRAINT consents_strategy_fk FOREIGN KEY (strategy_id) REFERENCES strategies(id);
CREATE INDEX consents_strategy_idx ON consents (strategy_id) WHERE strategy_id IS NOT NULL;

CREATE TABLE strategy_versions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    strategy_id      uuid NOT NULL REFERENCES strategies(id),
    version          integer NOT NULL CHECK (version >= 1),
    code_hash        text NOT NULL,                            -- sha256 of creator code, or terminal engine_hash
    code_ciphertext  bytea,                                    -- creator uploads only; app_api has NO SELECT on it
    params           jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- §10 script contract (NULL for in-house terminal strategies where the feed defines it)
    markets          text[] CHECK (markets IS NULL OR (are_valid_coins(markets) AND cardinality(markets) BETWEEN 1 AND 5)),
    timeframe        text CHECK (timeframe IS NULL OR timeframe IN ('1h', '4h', '1d')),
    lookback         integer CHECK (lookback IS NULL OR lookback BETWEEN 50 AND 1000),
    max_leverage     integer CHECK (max_leverage IS NULL OR max_leverage BETWEEN 1 AND 5),
    published_at     timestamptz,
    backtest         jsonb,
    live_since       timestamptz,                              -- a new version resets the live track record
    UNIQUE (strategy_id, version)
);

-- ---------------------------------------------------------------- subscriptions & trading
CREATE TABLE subscriptions (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    user_id                 uuid NOT NULL REFERENCES users(id),
    strategy_id             uuid NOT NULL REFERENCES strategies(id),
    strategy_version_id     uuid NOT NULL REFERENCES strategy_versions(id),
    trading_address         eth_address NOT NULL,              -- master or sub-account
    master_address          eth_address,                       -- wallet that approved our agent (NULL until connected)
    allocation_micro        bigint NOT NULL CHECK (allocation_micro > 0),
    max_leverage_x100       integer NOT NULL CHECK (max_leverage_x100 BETWEEN 1 AND 10000),
    status                  subscription_status NOT NULL DEFAULT 'pending',
    status_changed_at       timestamptz NOT NULL DEFAULT now(),
    past_due_since          timestamptz,
    current_period_end      timestamptz,
    hwm_micro               bigint NOT NULL DEFAULT 0 CHECK (hwm_micro >= 0),
    cum_pnl_micro           bigint NOT NULL DEFAULT 0,
    pnl_cursor              timestamptz,                       -- attributed PnL settled up to (exclusive)
    consecutive_rejections  integer NOT NULL DEFAULT 0 CHECK (consecutive_rejections >= 0), -- circuit breaker §5.4
    cancel_positions        cancel_positions_mode,             -- SPEC §12: chosen on cancel (NULL until then)
    cancelled_at            timestamptz,
    CONSTRAINT subscriptions_closing_means_close CHECK (status <> 'closing' OR cancel_positions IS NOT DISTINCT FROM 'close')
);
-- ONE live subscription per trading address (positions would otherwise collide on the same account).
-- 'closing' still occupies the address until its positions are flat (SPEC §12).
CREATE UNIQUE INDEX subscriptions_one_live_per_address ON subscriptions (trading_address)
    WHERE status IN ('pending', 'active', 'past_due', 'reduce_only', 'closing');
CREATE INDEX subscriptions_user_idx ON subscriptions (user_id);
CREATE INDEX subscriptions_strategy_status_idx ON subscriptions (strategy_id, status);
CREATE INDEX subscriptions_version_status_idx ON subscriptions (strategy_version_id, status);
CREATE TRIGGER subscriptions_touch BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE signals (                                          -- §10 storage format
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    strategy_id          uuid NOT NULL REFERENCES strategies(id),         -- denormalised for queries
    strategy_version_id  uuid NOT NULL REFERENCES strategy_versions(id),
    bar_close            timestamptz NOT NULL,
    coin                 text NOT NULL CHECK (is_valid_coin(coin)),
    target_weight_bps    integer NOT NULL CHECK (target_weight_bps BETWEEN -100000 AND 100000), -- weight × 10000
    source               signal_source NOT NULL,
    raw                  jsonb,
    signature            text,
    received_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT signals_terminal_signed CHECK (source <> 'terminal' OR signature IS NOT NULL),
    UNIQUE (strategy_version_id, bar_close, coin)
);
CREATE INDEX signals_strategy_bar_idx ON signals (strategy_id, bar_close DESC);
CREATE INDEX signals_version_bar_idx ON signals (strategy_version_id, bar_close DESC);

CREATE TABLE orders (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    subscription_id      uuid NOT NULL REFERENCES subscriptions(id),
    strategy_version_id  uuid REFERENCES strategy_versions(id),
    bar_close            timestamptz,
    attempt              integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    cloid                text NOT NULL UNIQUE CHECK (cloid ~ '^0x[0-9a-f]{32}$'),   -- Hyperliquid 128-bit client order id
    coin                 text NOT NULL CHECK (is_valid_coin(coin)),
    side                 order_side NOT NULL,
    sz                   numeric NOT NULL CHECK (sz > 0),    -- exact decimal from/to Hyperliquid strings
    limit_px             numeric NOT NULL CHECK (limit_px > 0),
    reduce_only          boolean NOT NULL DEFAULT false,
    status               order_status NOT NULL DEFAULT 'submitting',
    filled_sz            numeric NOT NULL DEFAULT 0 CHECK (filled_sz >= 0),
    avg_px               numeric CHECK (avg_px IS NULL OR avg_px > 0),
    oid                  bigint,                              -- Hyperliquid order id once accepted
    hl_response          jsonb,
    error                text,
    submitted_at         timestamptz,
    jitter_seconds       integer NOT NULL DEFAULT 0 CHECK (jitter_seconds >= 0),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_subscription_idx ON orders (subscription_id, created_at DESC);
CREATE INDEX orders_bar_idx ON orders (subscription_id, bar_close, coin, attempt);
CREATE INDEX orders_pending_idx ON orders (status) WHERE status IN ('submitting', 'unknown', 'resting');
CREATE TRIGGER orders_touch BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Executor bookkeeping (addition to SPEC §4, used by app.execution ports):
--   subscription_bar_runs — is_bar_done / mark_bar_done (one row per subscription per bar close)
--   subscription_targets  — record_target / expected_positions (last target per subscription+coin)
CREATE TABLE subscription_bar_runs (
    subscription_id  uuid NOT NULL REFERENCES subscriptions(id),
    bar_close        timestamptz NOT NULL,
    outcome          text NOT NULL,
    done_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subscription_id, bar_close)
);

CREATE TABLE subscription_targets (
    subscription_id        uuid NOT NULL REFERENCES subscriptions(id),
    coin                   text NOT NULL CHECK (is_valid_coin(coin)),
    bar_close              timestamptz NOT NULL,
    target_notional_micro  bigint NOT NULL,
    weight_bps             integer NOT NULL,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subscription_id, coin)
);

CREATE TABLE fills (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                 timestamptz NOT NULL DEFAULT now(),
    subscription_id            uuid REFERENCES subscriptions(id),    -- NULL = not ours / unattributed
    trading_address            eth_address NOT NULL,
    coin                       text NOT NULL CHECK (is_valid_coin(coin)),
    tid                        bigint NOT NULL,
    oid                        bigint,
    px                         numeric NOT NULL CHECK (px > 0),
    sz                         numeric NOT NULL CHECK (sz > 0),
    side                       order_side NOT NULL,
    closed_pnl_micro           bigint NOT NULL DEFAULT 0,
    fee_micro                  bigint NOT NULL DEFAULT 0,           -- may be negative (maker rebate)
    builder_fee_micro          bigint NOT NULL DEFAULT 0 CHECK (builder_fee_micro >= 0),
    cloid                      text,
    time                       timestamptz NOT NULL,
    raw                        jsonb,
    builder_fee_recognised_at  timestamptz,                          -- settlement: builder fee posted to ledger
    builder_fee_ledger_tx_id   uuid,                                 -- FK added after ledger_transactions
    -- NOTE: SPEC says "tid unique". A Hyperliquid tid identifies the TRADE, shared by both counterparties;
    -- two of our users can be on opposite sides of one trade, so uniqueness is per trading address.
    UNIQUE (trading_address, tid)
);
CREATE INDEX fills_subscription_time_idx ON fills (subscription_id, time) WHERE subscription_id IS NOT NULL;
CREATE INDEX fills_address_time_idx ON fills (trading_address, time);
CREATE INDEX fills_cloid_idx ON fills (cloid) WHERE cloid IS NOT NULL;
CREATE INDEX fills_builder_unrecognised_idx ON fills (time) WHERE builder_fee_recognised_at IS NULL;

CREATE TABLE funding_events (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    subscription_id  uuid REFERENCES subscriptions(id),
    trading_address  eth_address NOT NULL,
    coin             text NOT NULL CHECK (is_valid_coin(coin)),
    time             timestamptz NOT NULL,
    usdc_micro       bigint NOT NULL,                         -- signed: + received, − paid
    raw              jsonb,
    UNIQUE (trading_address, coin, time)
);
CREATE INDEX funding_subscription_idx ON funding_events (subscription_id, time) WHERE subscription_id IS NOT NULL;

-- ---------------------------------------------------------------- ledger (append-only, hash-chained)
CREATE TABLE ledger_accounts (                                 -- append-only
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    code           text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9_]+(:[a-z0-9_.-]+)+$'),
    kind           ledger_account_kind NOT NULL,
    owner_user_id  uuid REFERENCES users(id),
    non_negative   boolean NOT NULL DEFAULT false,
    CONSTRAINT ledger_accounts_fee_balance_shape CHECK (
        code !~ '^user:[^:]+:fee_balance$' OR (kind = 'liability' AND non_negative AND owner_user_id IS NOT NULL)
    )
);
CREATE INDEX ledger_accounts_owner_idx ON ledger_accounts (owner_user_id) WHERE owner_user_id IS NOT NULL;

CREATE TABLE ledger_transactions (                             -- append-only, hash chain
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    idempotency_key  text NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    kind             text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.]{0,63}$'),
    memo             text,
    created_by       text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    entries_digest   sha256_hex NOT NULL,
    seq              bigint NOT NULL UNIQUE,                   -- set by trigger (1, 2, 3, … no gaps)
    prev_hash        sha256_hex NOT NULL UNIQUE,               -- set/verified by trigger
    hash             sha256_hex NOT NULL UNIQUE                -- set/verified by trigger
);
CREATE INDEX ledger_transactions_created_idx ON ledger_transactions (created_at);

ALTER TABLE fills ADD CONSTRAINT fills_builder_fee_tx_fk FOREIGN KEY (builder_fee_ledger_tx_id) REFERENCES ledger_transactions(id);

CREATE TABLE ledger_entries (                                  -- append-only
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    tx_id         uuid NOT NULL REFERENCES ledger_transactions(id),
    account_id    uuid NOT NULL REFERENCES ledger_accounts(id),
    amount_micro  bigint NOT NULL CHECK (amount_micro <> 0)    -- + debit / − credit
);
CREATE INDEX ledger_entries_tx_idx ON ledger_entries (tx_id);
CREATE INDEX ledger_entries_account_idx ON ledger_entries (account_id, created_at) INCLUDE (amount_micro);

-- Kinds that may push a non-negative account below zero (debts that exist regardless of balance).
CREATE FUNCTION ledger_kind_allows_overdraft(k text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT k IN ('profit_share', 'stripe_refund', 'stripe_dispute') $$;

-- Canonical digest of a multiset of (account code, amount); order-independent.
-- Python twin (app.ledger.service.entries_digest):
--   sha256(json.dumps(sorted([code, amt] ...), separators=(",", ":")).encode()).hexdigest()
CREATE FUNCTION ledger_entries_digest(codes text[], amounts bigint[]) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT encode(sha256(convert_to(
        '[' || coalesce(string_agg('[' || to_json(x.c)::text || ',' || x.a::text || ']', ',' ORDER BY x.c COLLATE "C", x.a), '') || ']',
        'UTF8')), 'hex')
    FROM unnest(codes, amounts) AS x(c, a)
$$;

-- Digest recomputed from the entries actually stored for a transaction.
CREATE FUNCTION ledger_tx_stored_digest(p_tx_id uuid) RETURNS text
LANGUAGE sql STABLE
AS $$
    SELECT ledger_entries_digest(coalesce(array_agg(a.code), '{}'), coalesce(array_agg(e.amount_micro), '{}'))
    FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
    WHERE e.tx_id = p_tx_id
$$;

CREATE FUNCTION ledger_tx_hash(t ledger_transactions) RETURNS text
LANGUAGE sql IMMUTABLE
AS $$
    SELECT encode(sha256(convert_to(t.prev_hash || E'\n' || canonical_json(jsonb_build_object(
        'id', t.id,
        'idempotency_key', t.idempotency_key,
        'kind', t.kind,
        'memo', t.memo,
        'created_by', t.created_by,
        'created_at', utc_iso(t.created_at),
        'entries_digest', t.entries_digest
    )), 'UTF8')), 'hex')
$$;

CREATE TABLE audit_log (                                       -- append-only, hash chain
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL,                                 -- 'user:<uuid>', 'admin:<uuid>', 'system:executor', ...
    action      text NOT NULL,
    target      text,                                          -- audit.py writes '' when none
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip_hash     text,
    seq         bigint NOT NULL UNIQUE,
    prev_hash   sha256_hex NOT NULL UNIQUE,
    hash        sha256_hex NOT NULL UNIQUE
);
CREATE INDEX audit_log_created_idx ON audit_log (created_at);
CREATE INDEX audit_log_target_idx ON audit_log (target) WHERE target IS NOT NULL AND target <> '';
CREATE INDEX audit_log_actor_idx ON audit_log (actor, created_at);

-- = app.security.audit.compute_hash(prev_hash, body)
CREATE FUNCTION audit_log_hash(t audit_log) RETURNS text
LANGUAGE sql IMMUTABLE
AS $$
    SELECT encode(sha256(convert_to(t.prev_hash || E'\n' || canonical_json(jsonb_build_object(
        'actor', t.actor,
        'action', t.action,
        'target', t.target,
        'payload', t.payload,
        'ip_hash', t.ip_hash,
        'created_at', utc_iso(t.created_at)
    )), 'UTF8')), 'hex')
$$;

CREATE FUNCTION chain_genesis_hash() RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT repeat('0', 64) $$;

-- Shared BEFORE INSERT logic for both chains (TG_ARGV[0] = advisory lock name).
CREATE FUNCTION hash_chain_before_insert() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_seq      bigint;
    v_last     text;
    v_computed text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext(TG_ARGV[0]));
    EXECUTE format('SELECT seq, hash FROM %I ORDER BY seq DESC LIMIT 1', TG_TABLE_NAME) INTO v_seq, v_last;
    v_last := coalesce(v_last, chain_genesis_hash());
    IF NEW.prev_hash IS NOT NULL AND NEW.prev_hash <> v_last THEN
        RAISE EXCEPTION '%: stale prev_hash (another writer appended first); retry', TG_TABLE_NAME
            USING ERRCODE = 'AJ409';
    END IF;
    NEW.seq := coalesce(v_seq, 0) + 1;
    NEW.prev_hash := v_last;
    NEW.created_at := coalesce(NEW.created_at, now());
    IF abs(extract(epoch FROM (NEW.created_at - clock_timestamp()))) > 600 THEN
        RAISE EXCEPTION '%: created_at % is more than 10 minutes from the DB clock', TG_TABLE_NAME, NEW.created_at
            USING ERRCODE = 'AJ422';
    END IF;
    IF TG_TABLE_NAME = 'ledger_transactions' THEN
        v_computed := ledger_tx_hash(NEW::ledger_transactions);
    ELSE
        v_computed := audit_log_hash(NEW::audit_log);
    END IF;
    IF NEW.hash IS NOT NULL AND NEW.hash <> v_computed THEN
        RAISE EXCEPTION '%: supplied hash does not match the canonical row hash', TG_TABLE_NAME
            USING ERRCODE = 'AJ422';
    END IF;
    NEW.hash := v_computed;
    RETURN NEW;
END
$$;

CREATE TRIGGER ledger_transactions_hash BEFORE INSERT ON ledger_transactions
    FOR EACH ROW EXECUTE FUNCTION hash_chain_before_insert('aijalon.ledger');
CREATE TRIGGER audit_log_hash BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION hash_chain_before_insert('aijalon.audit_log');

-- Integrity of ONE ledger transaction: >= 2 entries, Σ = 0, stored entries match entries_digest (so
-- entries cannot be appended to an old transaction later), and no non-negative account is pushed below
-- zero by a decrease (unless the kind allows overdraft).
CREATE FUNCTION ledger_check_tx(p_tx_id uuid) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_count   bigint;
    v_sum     numeric;
    v_tx      ledger_transactions%ROWTYPE;
    v_bad     record;
BEGIN
    SELECT count(*), coalesce(sum(amount_micro), 0) INTO v_count, v_sum FROM ledger_entries WHERE tx_id = p_tx_id;
    IF v_count < 2 THEN
        RAISE EXCEPTION 'ledger transaction % has % entries (need >= 2)', p_tx_id, v_count USING ERRCODE = 'AJ422';
    END IF;
    IF v_sum <> 0 THEN
        RAISE EXCEPTION 'ledger transaction % is unbalanced: sum = %', p_tx_id, v_sum USING ERRCODE = 'AJ422';
    END IF;
    SELECT * INTO v_tx FROM ledger_transactions WHERE id = p_tx_id;
    IF v_tx.entries_digest IS DISTINCT FROM ledger_tx_stored_digest(p_tx_id) THEN
        RAISE EXCEPTION 'ledger transaction % entries do not match entries_digest', p_tx_id USING ERRCODE = 'AJ422';
    END IF;
    IF ledger_kind_allows_overdraft(v_tx.kind) THEN
        RETURN;
    END IF;
    SELECT a.code, d.delta, s.bal INTO v_bad
      FROM (SELECT account_id, sum(amount_micro) AS raw_delta FROM ledger_entries WHERE tx_id = p_tx_id GROUP BY account_id) t
      JOIN ledger_accounts a ON a.id = t.account_id AND a.non_negative
      CROSS JOIN LATERAL (SELECT CASE WHEN a.kind IN ('asset', 'expense') THEN 1 ELSE -1 END AS sgn) g
      CROSS JOIN LATERAL (SELECT g.sgn * t.raw_delta AS delta) d
      CROSS JOIN LATERAL (SELECT g.sgn * coalesce(sum(e.amount_micro), 0) AS bal
                            FROM ledger_entries e WHERE e.account_id = a.id) s
     WHERE d.delta < 0 AND s.bal < 0
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'insufficient balance: account % would be % after this transaction (change %)',
            v_bad.code, v_bad.bal, v_bad.delta USING ERRCODE = 'AJ402';
    END IF;
END
$$;

CREATE FUNCTION ledger_entries_deferred_check() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM ledger_check_tx(NEW.tx_id);
    RETURN NULL;
END
$$;

CREATE FUNCTION ledger_transactions_deferred_check() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM ledger_check_tx(NEW.id);
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER ledger_entries_balanced
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_deferred_check();

CREATE CONSTRAINT TRIGGER ledger_transactions_balanced
    AFTER INSERT ON ledger_transactions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_transactions_deferred_check();

-- Append-only guards (fire for every role incl. table owners; only a superuser with
-- session_replication_role = replica, or an owner running ALTER TABLE … DISABLE TRIGGER, can bypass —
-- the hash chain detects that).
CREATE TRIGGER ledger_accounts_append_only BEFORE UPDATE OR DELETE ON ledger_accounts
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_accounts_no_truncate BEFORE TRUNCATE ON ledger_accounts
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_transactions_append_only BEFORE UPDATE OR DELETE ON ledger_transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_transactions_no_truncate BEFORE TRUNCATE ON ledger_transactions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_entries_no_truncate BEFORE TRUNCATE ON ledger_entries
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER consents_append_only BEFORE UPDATE OR DELETE ON consents
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER consents_no_truncate BEFORE TRUNCATE ON consents
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Balances. balance_micro = raw Σ (+debit/−credit); normal_balance_micro = sign-adjusted (see header).
CREATE VIEW ledger_balances AS
SELECT a.id                                              AS account_id,
       a.code,
       a.kind,
       a.owner_user_id,
       coalesce(sum(e.amount_micro), 0)::bigint          AS balance_micro,
       (CASE WHEN a.kind IN ('asset', 'expense') THEN 1 ELSE -1 END
          * coalesce(sum(e.amount_micro), 0))::bigint    AS normal_balance_micro
FROM ledger_accounts a
LEFT JOIN ledger_entries e ON e.account_id = a.id
GROUP BY a.id;

-- The ONE supported way to write the ledger (app.ledger.service calls it).
--   p_entries: jsonb array of {"account": "<code>", "amount_micro": <int>}.
--   Returns (tx_id, created): created = false when the idempotency key already exists with the same
--   kind and the same entries (memo / created_by are not compared); raises AJ409 if they differ.
--   Serialised by the ledger chain advisory lock, so idempotency + balance checks are race-free under
--   READ COMMITTED (the default; use it for ledger writes).
CREATE FUNCTION ledger_post(p_idempotency_key text, p_kind text, p_memo text, p_created_by text, p_entries jsonb)
RETURNS TABLE (tx_id uuid, created boolean)
LANGUAGE plpgsql
AS $$
DECLARE
    v_codes    text[];
    v_amounts  bigint[];
    v_digest   text;
    v_existing ledger_transactions%ROWTYPE;
    v_missing  text;
    v_id       uuid;
    v_attempt  int;
BEGIN
    IF p_entries IS NULL OR jsonb_typeof(p_entries) <> 'array' THEN
        RAISE EXCEPTION 'entries must be a JSON array' USING ERRCODE = 'AJ422';
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_entries) e
               WHERE jsonb_typeof(e) <> 'object'
                  OR jsonb_typeof(e->'account') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(e->'amount_micro') IS DISTINCT FROM 'number'
                  OR (e->>'amount_micro') !~ '^-?[0-9]{1,18}$') THEN
        RAISE EXCEPTION 'each entry must be {"account": text, "amount_micro": integer}' USING ERRCODE = 'AJ422';
    END IF;
    SELECT array_agg(e->>'account' ORDER BY o), array_agg((e->>'amount_micro')::bigint ORDER BY o)
      INTO v_codes, v_amounts
      FROM jsonb_array_elements(p_entries) WITH ORDINALITY AS t(e, o);
    IF coalesce(cardinality(v_codes), 0) < 2 THEN
        RAISE EXCEPTION 'a ledger transaction needs >= 2 entries' USING ERRCODE = 'AJ422';
    END IF;
    IF 0 = ANY (v_amounts) THEN
        RAISE EXCEPTION 'zero-amount ledger entry' USING ERRCODE = 'AJ422';
    END IF;
    IF (SELECT sum(x) FROM unnest(v_amounts) x) <> 0 THEN
        RAISE EXCEPTION 'ledger transaction is unbalanced' USING ERRCODE = 'AJ422';
    END IF;
    v_digest := ledger_entries_digest(v_codes, v_amounts);

    -- Check twice: cheap path without the lock, then authoritative under the chain lock.
    FOR v_attempt IN 1..2 LOOP
        IF v_attempt = 2 THEN
            PERFORM pg_advisory_xact_lock(hashtext('aijalon.ledger'));
        END IF;
        SELECT * INTO v_existing FROM ledger_transactions WHERE idempotency_key = p_idempotency_key;
        IF FOUND THEN
            IF v_existing.kind = p_kind AND v_existing.entries_digest = v_digest THEN
                RETURN QUERY SELECT v_existing.id, false;
                RETURN;
            END IF;
            RAISE EXCEPTION 'idempotency key % already used with different content', p_idempotency_key
                USING ERRCODE = 'AJ409';
        END IF;
    END LOOP;

    SELECT string_agg(DISTINCT c, ', ') INTO v_missing
      FROM unnest(v_codes) c WHERE NOT EXISTS (SELECT 1 FROM ledger_accounts a WHERE a.code = c);
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'unknown ledger account(s): %', v_missing USING ERRCODE = 'AJ404';
    END IF;

    INSERT INTO ledger_transactions (idempotency_key, kind, memo, created_by, entries_digest)
    VALUES (p_idempotency_key, p_kind, p_memo, p_created_by, v_digest)
    RETURNING id INTO v_id;

    INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
    SELECT v_id, a.id, x.amt
      FROM unnest(v_codes, v_amounts) WITH ORDINALITY AS x(code, amt, o)
      JOIN ledger_accounts a ON a.code = x.code
     ORDER BY x.o;

    -- Same checks as the deferred trigger, run now so callers get the error at the call site.
    PERFORM ledger_check_tx(v_id);
    RETURN QUERY SELECT v_id, true;
END
$$;

-- Verify both hash chains from genesis. Returns NO ROWS when intact; otherwise the first broken row of
-- each broken chain with a reason.
CREATE FUNCTION verify_chain()
RETURNS TABLE (chain text, seq bigint, row_id uuid, reason text)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    t        ledger_transactions%ROWTYPE;
    a        audit_log%ROWTYPE;
    v_prev   text;
    v_expect bigint;
BEGIN
    v_prev := chain_genesis_hash();
    v_expect := 1;
    FOR t IN SELECT * FROM ledger_transactions lt ORDER BY lt.seq LOOP
        IF t.seq <> v_expect THEN
            RETURN QUERY SELECT 'ledger_transactions'::text, t.seq, t.id, format('sequence gap: expected %s', v_expect);
            EXIT;
        ELSIF t.prev_hash <> v_prev THEN
            RETURN QUERY SELECT 'ledger_transactions'::text, t.seq, t.id, 'prev_hash does not match previous row'::text;
            EXIT;
        ELSIF t.hash <> ledger_tx_hash(t) THEN
            RETURN QUERY SELECT 'ledger_transactions'::text, t.seq, t.id, 'row hash mismatch (row altered)'::text;
            EXIT;
        ELSIF t.entries_digest <> ledger_tx_stored_digest(t.id) THEN
            RETURN QUERY SELECT 'ledger_transactions'::text, t.seq, t.id, 'entries digest mismatch (entries altered)'::text;
            EXIT;
        END IF;
        v_prev := t.hash;
        v_expect := v_expect + 1;
    END LOOP;

    v_prev := chain_genesis_hash();
    v_expect := 1;
    FOR a IN SELECT * FROM audit_log al ORDER BY al.seq LOOP
        IF a.seq <> v_expect THEN
            RETURN QUERY SELECT 'audit_log'::text, a.seq, a.id, format('sequence gap: expected %s', v_expect);
            EXIT;
        ELSIF a.prev_hash <> v_prev THEN
            RETURN QUERY SELECT 'audit_log'::text, a.seq, a.id, 'prev_hash does not match previous row'::text;
            EXIT;
        ELSIF a.hash <> audit_log_hash(a) THEN
            RETURN QUERY SELECT 'audit_log'::text, a.seq, a.id, 'row hash mismatch (row altered)'::text;
            EXIT;
        END IF;
        v_prev := a.hash;
        v_expect := v_expect + 1;
    END LOOP;
END
$$;

-- Latest heads, for external anchoring by reconcile (store/publish daily).
CREATE VIEW chain_heads AS
SELECT 'ledger_transactions'::text AS chain, l.seq, l.hash, l.created_at
  FROM (SELECT * FROM ledger_transactions ORDER BY seq DESC LIMIT 1) l
UNION ALL
SELECT 'audit_log'::text, x.seq, x.hash, x.created_at
  FROM (SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1) x;

-- ---------------------------------------------------------------- money in / out
CREATE TABLE deposits (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    user_id         uuid NOT NULL REFERENCES users(id),
    method          deposit_method NOT NULL,
    external_ref    text NOT NULL UNIQUE,                     -- HL tx hash / Stripe PaymentIntent id
    amount_micro    bigint NOT NULL CHECK (amount_micro > 0), -- USD credited to the fee balance
    currency        text NOT NULL DEFAULT 'USD' CHECK (currency ~ '^[A-Z]{3,5}$'),   -- charged currency
    amount_minor    bigint CHECK (amount_minor IS NULL OR amount_minor > 0),         -- charged amount (cents/sen)
    fee_micro       bigint NOT NULL DEFAULT 0 CHECK (fee_micro >= 0),                -- processor fee passed through
    withdrawable    boolean NOT NULL DEFAULT false,           -- USDC credits true; card credits spend-only
    status          deposit_status NOT NULL DEFAULT 'pending',
    credited_tx_id  uuid REFERENCES ledger_transactions(id),
    meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT deposits_credited_has_tx CHECK (status <> 'credited' OR credited_tx_id IS NOT NULL)
);
CREATE INDEX deposits_user_idx ON deposits (user_id, created_at DESC);

-- Fee-balance withdrawals (beneficiary = user) and creator/referrer payouts share the maker-checker shape.
-- maker_admin = first approver (approved_1), checker_admin = second approver (approved_2), never the same.
CREATE TABLE withdrawals (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    beneficiary_user_id  uuid NOT NULL REFERENCES users(id),
    amount_micro         bigint NOT NULL CHECK (amount_micro > 0),
    to_address           eth_address NOT NULL,
    status               payout_status NOT NULL DEFAULT 'requested',
    maker_admin          uuid REFERENCES users(id),
    maker_approved_at    timestamptz,
    checker_admin        uuid REFERENCES users(id),
    checker_approved_at  timestamptz,
    rejected_by          uuid REFERENCES users(id),
    reject_reason        text,
    tx_hash              text,
    ledger_tx_id         uuid REFERENCES ledger_transactions(id),
    CONSTRAINT withdrawals_four_eyes CHECK (checker_admin IS NULL OR checker_admin <> maker_admin),
    CONSTRAINT withdrawals_maker_set CHECK (status NOT IN ('approved_1', 'approved_2', 'sent') OR maker_admin IS NOT NULL),
    CONSTRAINT withdrawals_checker_set CHECK (status NOT IN ('approved_2', 'sent') OR checker_admin IS NOT NULL),
    CONSTRAINT withdrawals_sent_hash CHECK (status <> 'sent' OR tx_hash IS NOT NULL),
    CONSTRAINT withdrawals_not_self_approved CHECK (maker_admin IS DISTINCT FROM beneficiary_user_id
                                                    AND checker_admin IS DISTINCT FROM beneficiary_user_id)
);
CREATE INDEX withdrawals_beneficiary_idx ON withdrawals (beneficiary_user_id, created_at DESC);
CREATE INDEX withdrawals_open_idx ON withdrawals (status) WHERE status IN ('requested', 'approved_1', 'approved_2');

CREATE TABLE payouts (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    beneficiary_user_id  uuid NOT NULL REFERENCES users(id),
    ledger_account_id    uuid NOT NULL REFERENCES ledger_accounts(id),   -- creator:{id}:payable / referrer:{id}:payable
    amount_micro         bigint NOT NULL CHECK (amount_micro > 0),
    to_address           eth_address NOT NULL,
    status               payout_status NOT NULL DEFAULT 'requested',
    maker_admin          uuid REFERENCES users(id),
    maker_approved_at    timestamptz,
    checker_admin        uuid REFERENCES users(id),
    checker_approved_at  timestamptz,
    rejected_by          uuid REFERENCES users(id),
    reject_reason        text,
    tx_hash              text,
    ledger_tx_id         uuid REFERENCES ledger_transactions(id),
    CONSTRAINT payouts_four_eyes CHECK (checker_admin IS NULL OR checker_admin <> maker_admin),
    CONSTRAINT payouts_maker_set CHECK (status NOT IN ('approved_1', 'approved_2', 'sent') OR maker_admin IS NOT NULL),
    CONSTRAINT payouts_checker_set CHECK (status NOT IN ('approved_2', 'sent') OR checker_admin IS NOT NULL),
    CONSTRAINT payouts_sent_hash CHECK (status <> 'sent' OR tx_hash IS NOT NULL),
    CONSTRAINT payouts_not_self_approved CHECK (maker_admin IS DISTINCT FROM beneficiary_user_id
                                                AND checker_admin IS DISTINCT FROM beneficiary_user_id)
);
CREATE INDEX payouts_beneficiary_idx ON payouts (beneficiary_user_id, created_at DESC);
CREATE INDEX payouts_open_idx ON payouts (status) WHERE status IN ('requested', 'approved_1', 'approved_2');

-- Daily profit-share settlement record (§1.1), idempotent on (subscription_id, settle_date).
-- (Addition to SPEC §4 — the ledger tx carries the money; this row carries the HWM math for audit;
--  SettlementRepo.is_settled / save_profit_share.)
CREATE TABLE profit_share_settlements (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at             timestamptz NOT NULL DEFAULT now(),
    subscription_id        uuid NOT NULL REFERENCES subscriptions(id),
    settle_date            date NOT NULL,
    cum_pnl_micro          bigint NOT NULL,
    hwm_micro              bigint NOT NULL CHECK (hwm_micro >= 0),        -- HWM after this settlement
    pnl_cursor             timestamptz NOT NULL,
    ledger_tx_id           uuid REFERENCES ledger_transactions(id),        -- NULL when nothing was charged
    UNIQUE (subscription_id, settle_date)
);

-- ---------------------------------------------------------------- content & social
CREATE TABLE posts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    creator_id       uuid NOT NULL REFERENCES users(id),
    strategy_id      uuid REFERENCES strategies(id),
    title            text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    body             text,
    body_ciphertext  bytea,
    price_micro      bigint NOT NULL DEFAULT 0 CHECK (price_micro >= 0),   -- 0 = free; min price lives in config
    published_at     timestamptz,
    CONSTRAINT posts_has_body CHECK (body IS NOT NULL OR body_ciphertext IS NOT NULL)
);
CREATE INDEX posts_creator_idx ON posts (creator_id, created_at DESC);
CREATE INDEX posts_strategy_idx ON posts (strategy_id, published_at DESC) WHERE strategy_id IS NOT NULL;

CREATE TABLE post_purchases (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    post_id       uuid NOT NULL REFERENCES posts(id),
    user_id       uuid NOT NULL REFERENCES users(id),
    price_micro   bigint NOT NULL DEFAULT 0 CHECK (price_micro >= 0),
    ledger_tx_id  uuid REFERENCES ledger_transactions(id),
    UNIQUE (post_id, user_id)
);
CREATE INDEX post_purchases_user_idx ON post_purchases (user_id);

CREATE TABLE reviews (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    strategy_id     uuid NOT NULL REFERENCES strategies(id),
    user_id         uuid NOT NULL REFERENCES users(id),
    rating          smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
    body            text CHECK (body IS NULL OR length(body) <= 5000),
    eligible_since  timestamptz NOT NULL,                     -- subscription start + 30 days (checked in API)
    UNIQUE (strategy_id, user_id)
);
CREATE INDEX reviews_user_idx ON reviews (user_id);

CREATE TABLE showcase_wallets (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    strategy_id   uuid NOT NULL REFERENCES strategies(id),
    address       eth_address NOT NULL,
    period_month  date NOT NULL CHECK (extract(day FROM period_month) = 1),
    revealed_at   timestamptz,                                -- public only after the month ends
    UNIQUE (strategy_id, address, period_month)
);

-- ---------------------------------------------------------------- ops
CREATE TABLE alerts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    user_id     uuid REFERENCES users(id),                   -- NULL = ops alert
    severity    alert_severity NOT NULL,
    kind        text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedup_key   text,                                        -- notifier dedupe (INSERT … ON CONFLICT DO NOTHING)
    acked_at    timestamptz,
    acked_by    uuid REFERENCES users(id)
);
CREATE INDEX alerts_user_idx ON alerts (user_id, created_at DESC);
CREATE INDEX alerts_unacked_idx ON alerts (severity, created_at DESC) WHERE acked_at IS NULL;
CREATE UNIQUE INDEX alerts_dedup_key_uidx ON alerts (dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE system_flags (
    key            text PRIMARY KEY CHECK (key ~ '^[a-z_]+(:[A-Za-z0-9_:.-]+)?$'),  -- kill_switch_market:xyz:SILVER
    value          jsonb NOT NULL,
    updated_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    -- maker-checker for lifting a switch: maker proposes pending_value, a different admin applies it
    pending_value  jsonb,
    pending_by     text,
    pending_at     timestamptz,
    CONSTRAINT system_flags_pending_complete CHECK ((pending_value IS NULL) = (pending_by IS NULL))
);
CREATE TRIGGER system_flags_touch BEFORE UPDATE ON system_flags
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

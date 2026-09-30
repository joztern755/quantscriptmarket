-- =====================================================================================================
-- 0006_data.sql — data jobs (owner: app/jobs_data): own candle history, fills / funding ingestion, deposit scan,
-- agent-expiry scan, and the user/ops event outbox. Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps
-- each file in one transaction). Explicit GRANTs per table; nobody gets DELETE.
--
--   job_cursors     resumable per-(job, key) cursors of the /internal data jobs (candles-sync, fills-ingest,
--                   funding-scan, deposits-scan, agent-expiry-scan). cursor_ms = ms since epoch the next run resumes
--                   from; state = small job-specific JSON (next_due_ms, missing_count, ...).
--   candles         SPEC §12 "Own candle history": CLOSED Hyperliquid candles, prices as exact NUMERIC from the API
--                   strings (never floats). A stored candle is immutable (UPDATE raises AJ403); a later differing
--                   value is a data alert, never an overwrite.
--   events_outbox   user + ops events (trade opened/closed/resized, agent expiring/expired, deposit credited/held,
--                   data alerts, signal rejections). Written in the SAME transaction as the fact they describe;
--                   delivered (Telegram / email / in-app) by the alerts module, which only sets delivered_at /
--                   attempts / last_error. user_id NULL = ops event. dedup_key makes emitters idempotent.
--
-- Columns added to existing tables (IF NOT EXISTS: harmless if another migration added the same name):
--   fills.net_pnl_micro           floor((closedPnl − fee) × 1e6) on the exact Decimal difference (SPEC §1.1; fee
--                                 already includes the builder fee). settlement pnl_since sums THIS column.
--   fills.attributed_via          'cloid' (orders table) | 'window' (crash fallback) | NULL (not attributed)
--   funding_events.attributed_micro  part of usdc_micro attributed to funding_events.subscription_id (floored toward
--                                 −∞; daily aggregates: estimated, income dropped — app.hl.fills.attribute_funding).
--                                 settlement pnl_since sums THIS column (usdc_micro is the whole account's payment).
--   funding_events.n_samples / estimated / szi / funding_rate   raw bucket facts (daily aggregate when n_samples set)
--   agent_keys.valid_until        on-chain validUntil of the approved agent (extraAgents), refreshed by the scan
--   agent_keys.valid_until_checked_at
--   agent_key_status += 'expired' the agent's approval ran out on-chain: it can no longer place ANY order (not even
--                                 exits). The executor must treat only status = 'active' agents as usable.
-- Seed: ledger account suspense:usdc_unattributed (liability) — USDC that reached the treasury but could not be
-- credited automatically (unknown sender, below minimum): debit treasury:hl_usdc / credit suspense, key usdc_hl:{hash}.
-- =====================================================================================================

-- ---------------------------------------------------------------- job cursors
CREATE TABLE job_cursors (
    job         text NOT NULL CHECK (job ~ '^[a-z][a-z0-9_]{0,63}$'),
    key         text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
    cursor_ms   bigint CHECK (cursor_ms IS NULL OR cursor_ms >= 0),
    state       jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job, key)
);
CREATE TRIGGER job_cursors_touch BEFORE UPDATE ON job_cursors
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------- candles (SPEC §12)
CREATE TABLE candles (
    coin        text NOT NULL CHECK (is_valid_coin(coin)),
    interval    text NOT NULL CHECK (interval IN ('1h', '4h', '1d')),
    open_time   timestamptz NOT NULL,
    o           numeric NOT NULL,
    h           numeric NOT NULL,
    l           numeric NOT NULL,
    c           numeric NOT NULL,
    v           numeric NOT NULL,
    trades      integer CHECK (trades IS NULL OR trades >= 0),           -- Hyperliquid "n"
    source      text NOT NULL DEFAULT 'hyperliquid' CHECK (source ~ '^[a-z][a-z0-9_]{0,31}$'),
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (coin, interval, open_time),
    CONSTRAINT candles_positive CHECK (o > 0 AND h > 0 AND l > 0 AND c > 0 AND v >= 0),
    CONSTRAINT candles_range CHECK (h >= l)
);

CREATE FUNCTION candles_immutable() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'candles are immutable once stored (%.% %): report a data alert instead', OLD.coin, OLD.interval,
        OLD.open_time USING ERRCODE = 'AJ403';
END
$$;
CREATE TRIGGER candles_immutable BEFORE UPDATE ON candles
    FOR EACH ROW EXECUTE FUNCTION candles_immutable();

-- ---------------------------------------------------------------- events outbox
CREATE TABLE events_outbox (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- delivery order
    created_at    timestamptz NOT NULL DEFAULT now(),
    user_id       uuid REFERENCES users(id),                         -- NULL = ops event
    kind          text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]{1,63}$'),        -- = app/alerts/prefs.py kinds
    severity      alert_severity NOT NULL DEFAULT 'info',
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,                 -- never full addresses, keys or tokens
    dedup_key     text UNIQUE CHECK (dedup_key IS NULL OR length(dedup_key) BETWEEN 1 AND 200),
    delivered_at  timestamptz,
    attempts      integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error    text CHECK (last_error IS NULL OR length(last_error) <= 500)
);
CREATE INDEX events_outbox_pending_idx ON events_outbox (id) WHERE delivered_at IS NULL;
CREATE INDEX events_outbox_user_idx ON events_outbox (user_id, created_at DESC) WHERE user_id IS NOT NULL;

-- The event itself is immutable; delivery bookkeeping only moves forward.
CREATE FUNCTION events_outbox_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id <> OLD.id OR NEW.created_at <> OLD.created_at OR NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.kind <> OLD.kind OR NEW.severity <> OLD.severity OR NEW.payload <> OLD.payload
       OR NEW.dedup_key IS DISTINCT FROM OLD.dedup_key THEN
        RAISE EXCEPTION 'events_outbox event fields are immutable' USING ERRCODE = 'AJ422';
    END IF;
    IF OLD.delivered_at IS NOT NULL AND NEW.delivered_at IS DISTINCT FROM OLD.delivered_at THEN
        RAISE EXCEPTION 'events_outbox row % already delivered', OLD.id USING ERRCODE = 'AJ403';
    END IF;
    IF NEW.attempts < OLD.attempts THEN
        RAISE EXCEPTION 'events_outbox attempts cannot decrease' USING ERRCODE = 'AJ422';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER events_outbox_guard BEFORE UPDATE ON events_outbox
    FOR EACH ROW EXECUTE FUNCTION events_outbox_guard();

-- ---------------------------------------------------------------- columns on existing tables
ALTER TABLE fills ADD COLUMN IF NOT EXISTS net_pnl_micro bigint;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS attributed_via text;
ALTER TABLE fills ADD CONSTRAINT fills_attributed_via_chk
    CHECK (attributed_via IS NULL OR attributed_via IN ('cloid', 'window'));

ALTER TABLE funding_events ADD COLUMN IF NOT EXISTS attributed_micro bigint;
ALTER TABLE funding_events ADD COLUMN IF NOT EXISTS n_samples integer;
ALTER TABLE funding_events ADD COLUMN IF NOT EXISTS estimated boolean NOT NULL DEFAULT false;
ALTER TABLE funding_events ADD COLUMN IF NOT EXISTS szi numeric;
ALTER TABLE funding_events ADD COLUMN IF NOT EXISTS funding_rate numeric;
ALTER TABLE funding_events ADD CONSTRAINT funding_events_attribution_chk
    CHECK (attributed_micro IS NULL OR subscription_id IS NOT NULL);
ALTER TABLE funding_events ADD CONSTRAINT funding_events_n_samples_chk
    CHECK (n_samples IS NULL OR n_samples BETWEEN 1 AND 24);

ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS valid_until timestamptz;
ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS valid_until_checked_at timestamptz;
CREATE INDEX IF NOT EXISTS agent_keys_active_idx ON agent_keys (valid_until_checked_at NULLS FIRST)
    WHERE status = 'active';

-- New enum value (not used elsewhere in this migration: PG allows ADD VALUE in a transaction but not its use).
ALTER TYPE agent_key_status ADD VALUE IF NOT EXISTS 'expired';

-- ---------------------------------------------------------------- seed
INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative) VALUES
    ('suspense:usdc_unattributed', 'liability', NULL, false)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------- grants (no DELETE for anyone)
GRANT SELECT, INSERT, UPDATE ON job_cursors TO app_executor;
GRANT SELECT, INSERT ON candles TO app_executor;
GRANT SELECT ON candles TO app_api;                                  -- backtests + listing history check
GRANT SELECT, INSERT ON events_outbox TO app_executor, app_api;
GRANT UPDATE (delivered_at, attempts, last_error) ON events_outbox TO app_executor, app_api;
GRANT UPDATE (valid_until, valid_until_checked_at) ON agent_keys TO app_executor;
GRANT SELECT (valid_until, valid_until_checked_at) ON agent_keys TO app_api;
GRANT ALL ON job_cursors, candles, events_outbox TO app_migrator;

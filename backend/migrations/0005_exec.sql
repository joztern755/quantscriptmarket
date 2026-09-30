-- =====================================================================================================
-- 0005_exec.sql — execution integration (owner: app/execution). Runs as app_migrator. No BEGIN/COMMIT
-- (migrate.py wraps each file in one transaction). Explicit GRANTs; nobody gets DELETE.
--
--   1. SILVER strategy_version 1 (SPEC §7, §12 owner decision: SILVER listed free at launch). Subscriptions pin a
--      strategy_version_id, so the listed in-house strategy needs a published version before anyone can
--      subscribe; the terminal feed's signals are stored against it. code_hash = sha256 of the vendored CREST
--      script (signals/vendor/MANIFEST.json "scripts.silver.sha256"); re-vendoring a script is a NEW version.
--      published_at / live_since = migration time (the live track record starts here).
--   2. reconciliation_reports — one row per /internal/reconcile run (jobs.reconcile); the admin console shows
--      the latest (jobs.latest_reconciliation, read through the API role).
--   3. Indexes for the executor's hot queries (closing subscriptions, unresolved orders of a subscription).
-- =====================================================================================================

-- ---------------------------------------------------------------- 1. SILVER version 1
INSERT INTO strategy_versions (strategy_id, version, code_hash, params, markets, timeframe, lookback, max_leverage,
                               published_at, live_since)
SELECT st.id, 1,
       'e60119a7222c352085cf6753be7231e05a201ba2ecb495812d4671fc44a942ed',
       jsonb_build_object(
           'source', 'terminal',
           'signal_key', 'silver',
           'script_file', 'crest_silver.js',
           'script_sha256', 'e60119a7222c352085cf6753be7231e05a201ba2ecb495812d4671fc44a942ed',
           'script_version', '3.0',
           'script_bytes', 57245,
           'source_repo', 'joztern755/terminal.aijalon',
           'source_commit', '2ef153ea1dd48e0ddd8ef5d066a40c4c1dfed29b',
           'vendored', '2026-09-30',
           'manifest', 'signals/vendor/MANIFEST.json',
           'weights', jsonb_build_array(0, 1, 2),
           'long_only', true),
       ARRAY['xyz:SILVER']::text[], '1d', NULL, 2,
       now(), now()
  FROM strategies st
 WHERE st.slug = 'silver'
ON CONFLICT (strategy_id, version) DO NOTHING;

-- ---------------------------------------------------------------- 2. reconciliation reports
CREATE TABLE reconciliation_reports (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    date_key    date NOT NULL,
    report      jsonb NOT NULL
);
CREATE INDEX reconciliation_reports_created_idx ON reconciliation_reports (created_at DESC);

-- ---------------------------------------------------------------- 3. executor indexes
CREATE INDEX subscriptions_closing_idx ON subscriptions (status_changed_at) WHERE status = 'closing';
CREATE INDEX orders_unresolved_by_sub_idx ON orders (subscription_id, bar_close, coin, attempt)
    WHERE status IN ('submitting', 'unknown', 'resting');

-- ---------------------------------------------------------------- grants (no DELETE for anyone)
GRANT SELECT, INSERT ON reconciliation_reports TO app_executor;
GRANT SELECT ON reconciliation_reports TO app_api;
GRANT ALL ON reconciliation_reports TO app_migrator;

-- =====================================================================================================
-- 0012_trading_fixes.sql — security review fixes: trusted builder dexes, in-house script pin, API HL budget,
-- deposit-scan requests (docs/security/REVIEW_TRADING_KEYS.md F1/F4, REVIEW_AUTH_API.md F1).
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
-- Explicit GRANTs per table; nobody gets DELETE.
--
--   trusted_dexes          SPEC §12 "Trusted builder dexes" (owner, 30 Sep 2026): strategies may trade validator perps
--                          (dex '') and builder-deployed (HIP-3) perps ONLY on an allowlisted dex. A dex deployer controls
--                          its own oracle / mark / 24h volume / open interest, so the thin-market guards cannot protect
--                          subscribers on an attacker-owned dex. ONE admin adds a dex (step-up, audit-logged); removal is
--                          a soft delete (removed_at) that immediately stops new entries on the dex's markets (the
--                          executor re-reads this table every tick and treats a non-active dex as entries-blocked;
--                          exits keep running). The validator dex '' can never be removed (CHECK).
--                          Enforced at: creator strategy create, version upload, listing proposal and listing approval
--                          (API), the executor pre-trade guard (fail closed: table unreadable → only validator perps may
--                          open), and in-house signal ingestion.
--   strategy_versions_inhouse_pin
--                          every IN-HOUSE strategy version must carry params.script_sha256 (64 lower-case hex): the
--                          signal ingest refuses a feed whose per-strategy script hash differs, and refuses to ingest a
--                          listed in-house strategy without a pin (F4).
--   deposit_scan_requests  POST /deposits/usdc/confirm no longer scans Hyperliquid inline (AUTH F1): it records a
--                          request (one row per user, upserted) that the deposits-scan job may use as a wake-up hint;
--                          the scheduled scan (every 5 min, single-flight, cursor) books every transfer regardless.
--   hl_rate_budget         the API now charges its own Hyperliquid /info calls to the shared budget table under its
--                          own egress key (it has its own NAT IP since this fix round) → INSERT/UPDATE for app_api.
-- =====================================================================================================

-- ---------------------------------------------------------------- trusted builder dexes
CREATE TABLE trusted_dexes (
    dex             text PRIMARY KEY CHECK (dex = '' OR dex ~ '^[a-z][a-z0-9]{0,15}$'),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    added_by        text NOT NULL CHECK (length(added_by) BETWEEN 1 AND 200),
    reason          text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    removed_at      timestamptz,
    removed_by      text CHECK (removed_by IS NULL OR length(removed_by) BETWEEN 1 AND 200),
    removal_reason  text CHECK (removal_reason IS NULL OR length(removal_reason) BETWEEN 1 AND 500),
    CONSTRAINT trusted_dexes_removal_complete
        CHECK ((removed_at IS NULL) = (removed_by IS NULL) AND (removed_at IS NULL) = (removal_reason IS NULL)),
    CONSTRAINT trusted_dexes_validator_permanent CHECK (dex <> '' OR removed_at IS NULL)
);

CREATE FUNCTION trusted_dexes_touch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trusted_dexes_touch BEFORE UPDATE ON trusted_dexes
    FOR EACH ROW EXECUTE FUNCTION trusted_dexes_touch();

-- Launch allowlist = the 10 builder dexes live on 30 Sep 2026 (SPEC §12) + the validator dex.
INSERT INTO trusted_dexes (dex, added_by, reason) VALUES
    ('',     'system:migration_0012', 'validator perps (Hyperliquid L1) — always trusted'),
    ('xyz',  'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('flx',  'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('vntl', 'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('hyna', 'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('km',   'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('abcd', 'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('cash', 'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('para', 'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('mkts', 'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)'),
    ('io',   'system:migration_0012', 'launch allowlist (owner decision 30 Sep 2026, SPEC §12)');

-- ---------------------------------------------------------------- in-house versions must pin their script hash
CREATE FUNCTION strategy_versions_inhouse_pin() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM strategies s WHERE s.id = NEW.strategy_id AND s.in_house)
       AND NOT (COALESCE(NEW.params ->> 'script_sha256', '') ~ '^[0-9a-f]{64}$') THEN
        RAISE EXCEPTION 'in-house strategy versions must pin params.script_sha256 (64 lower-case hex)'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER strategy_versions_inhouse_pin BEFORE INSERT OR UPDATE OF params, strategy_id ON strategy_versions
    FOR EACH ROW EXECUTE FUNCTION strategy_versions_inhouse_pin();

DO $$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n
      FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id
     WHERE s.in_house AND NOT (COALESCE(v.params ->> 'script_sha256', '') ~ '^[0-9a-f]{64}$');
    IF n > 0 THEN
        RAISE EXCEPTION '% existing in-house strategy version(s) have no params.script_sha256 pin; add it first', n
            USING ERRCODE = '23514';
    END IF;
END;
$$;

-- ---------------------------------------------------------------- deposit scan requests (wake-up hints)
CREATE TABLE deposit_scan_requests (
    user_id       uuid PRIMARY KEY REFERENCES users(id),
    requested_at  timestamptz NOT NULL DEFAULT now(),
    since         timestamptz NOT NULL,             -- clamped server-side: ≥ now − 48 h and ≥ wallet verification − 1 h
    served_at     timestamptz,
    CONSTRAINT deposit_scan_requests_since_sane CHECK (since <= requested_at)
);

-- ---------------------------------------------------------------- grants
GRANT SELECT, INSERT, UPDATE ON trusted_dexes TO app_api;             -- admin add / remove (soft) + creator checks
GRANT SELECT ON trusted_dexes TO app_executor;                        -- pre-trade guard + signal ingest
GRANT SELECT, INSERT, UPDATE ON deposit_scan_requests TO app_api;
GRANT SELECT, UPDATE ON deposit_scan_requests TO app_executor;        -- deposits-scan marks requests served
GRANT INSERT, UPDATE ON hl_rate_budget TO app_api;                    -- API HL calls charge the shared budget
GRANT ALL ON trusted_dexes, deposit_scan_requests TO app_migrator;

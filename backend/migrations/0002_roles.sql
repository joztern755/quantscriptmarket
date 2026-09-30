-- =====================================================================================================
-- migrate:session-user
-- 0002_roles.sql — least-privilege database roles (SPEC §2.1, §4 "DB roles").
--
-- Three NOLOGIN group roles. Real principals (Cloud SQL IAM service-account users, e.g.
-- "api@PROJECT.iam", "executor@PROJECT.iam", "deployer@PROJECT.iam") are created by infra/ and granted
-- membership:   GRANT app_api TO "api@PROJECT.iam";   (never grant these roles LOGIN)
--
--   app_migrator  owns every schema object (ownership is moved to it below) and runs migrations
--                 (migrate.py does SET ROLE app_migrator when the connecting user is a member).
--                 NOTE: a table owner can ALTER TABLE … DISABLE TRIGGER; the hash chains detect tampering.
--   app_api       public API. Column-level SELECT on agent_keys WITHOUT key_ciphertext and on
--                 strategy_versions WITHOUT code_ciphertext (use explicit column lists — `SELECT *` on those
--                 tables fails for app_api by design). No UPDATE/DELETE/TRUNCATE on ledger_*, audit_log,
--                 consents (also blocked by triggers for everyone). No DELETE anywhere.
--   app_executor  trading + all /internal/* jobs (tick, settle-daily, ingest-signals, reconcile,
--                 deposits-scan, referral-tiers). Reads key_ciphertext (decrypts via KMS in memory).
--
-- Requirements: run as a role with CREATEROLE (Cloud SQL: the built-in `postgres` user / cloudsqlsuperuser)
-- the first time. Idempotent: roles are created only if missing; grants are repeatable.
-- New tables in later migrations MUST add their own GRANTs (no blanket default privileges on purpose).
-- =====================================================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
        CREATE ROLE app_api NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_executor') THEN
        CREATE ROLE app_executor NOLOGIN;
    END IF;
    IF NOT pg_has_role(current_user, 'app_migrator', 'MEMBER') THEN
        EXECUTE format('GRANT app_migrator TO %I', current_user);
    END IF;
END
$$;

-- ---------------------------------------------------------------- ownership -> app_migrator
-- Every table / view / sequence / function / type in schema public owned by the current user, except
-- extension members (pgcrypto) — so later migrations (run as app_migrator) can ALTER them.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.oid, c.relname, c.relkind
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
           AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
           AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER %s public.%I OWNER TO app_migrator',
                       CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
                       r.relname);
    END LOOP;
    FOR r IN
        SELECT p.oid
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
           AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER ROUTINE %s OWNER TO app_migrator', r.oid::regprocedure);
    END LOOP;
    FOR r IN
        SELECT t.oid, t.typtype
          FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd')
           AND t.typowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
           AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO app_migrator', CASE r.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, r.oid::regtype);
    END LOOP;
END
$$;

-- ---------------------------------------------------------------- schema / database
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO app_api, app_executor, app_migrator', current_database());
END
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO app_api, app_executor, app_migrator;
GRANT CREATE ON SCHEMA public TO app_migrator;

-- Start from nothing for the app roles (idempotent re-runs, and nothing inherited from PUBLIC).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, app_api, app_executor;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, app_api, app_executor;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_api, app_executor;

-- Write entry points: only the app roles (helpers such as canonical_json stay PUBLIC-executable; they
-- are pure). SECURITY INVOKER everywhere: callers still need the table privileges below.
REVOKE EXECUTE ON FUNCTION ledger_post(text, text, text, text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION verify_chain() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger_post(text, text, text, text, jsonb) TO app_api, app_executor, app_migrator;
GRANT EXECUTE ON FUNCTION verify_chain() TO app_api, app_executor, app_migrator;

-- ---------------------------------------------------------------- app_api
GRANT SELECT, INSERT, UPDATE ON users TO app_api;
GRANT SELECT, INSERT ON consents TO app_api;
GRANT SELECT, INSERT, UPDATE ON wallets TO app_api;
-- agent_keys: the API encrypts + stores (INSERT incl. key_ciphertext) but can never read the ciphertext back.
GRANT SELECT (id, created_at, user_id, master_address, agent_address, agent_name, kms_key_version, status,
              approved_at, revoked_at) ON agent_keys TO app_api;
GRANT INSERT ON agent_keys TO app_api;
GRANT UPDATE (status, approved_at, revoked_at) ON agent_keys TO app_api;
GRANT SELECT, INSERT, UPDATE ON builder_approvals TO app_api;
GRANT SELECT, INSERT, UPDATE ON kyc_creators TO app_api;
GRANT SELECT, INSERT, UPDATE ON strategies TO app_api;
-- strategy_versions: creator uploads are written (encrypted) by the API; code is never read back by it.
GRANT SELECT (id, created_at, strategy_id, version, code_hash, params, markets, timeframe, lookback, max_leverage,
              published_at, backtest, live_since) ON strategy_versions TO app_api;
GRANT INSERT ON strategy_versions TO app_api;
GRANT UPDATE (params, published_at, backtest, live_since) ON strategy_versions TO app_api;
GRANT SELECT, INSERT, UPDATE ON subscriptions TO app_api;
GRANT SELECT ON signals, orders, fills, funding_events, subscription_targets TO app_api;
GRANT SELECT, INSERT ON ledger_accounts, ledger_transactions, ledger_entries TO app_api;
GRANT SELECT ON ledger_balances, chain_heads TO app_api;
GRANT SELECT, INSERT, UPDATE ON deposits TO app_api;
GRANT SELECT, INSERT, UPDATE ON withdrawals, payouts TO app_api;
GRANT SELECT ON profit_share_settlements TO app_api;
GRANT SELECT, INSERT, UPDATE ON posts TO app_api;
GRANT SELECT, INSERT ON post_purchases TO app_api;
GRANT SELECT, INSERT, UPDATE ON reviews TO app_api;
GRANT SELECT, INSERT, UPDATE ON showcase_wallets TO app_api;
GRANT SELECT, INSERT, UPDATE ON alerts TO app_api;
GRANT SELECT, INSERT ON audit_log TO app_api;
GRANT SELECT, INSERT, UPDATE ON system_flags TO app_api;

-- ---------------------------------------------------------------- app_executor
GRANT SELECT ON users TO app_executor;
GRANT UPDATE (plan, plan_started_at, plan_period_end, plan_past_due_since, referral_tier) ON users TO app_executor;
GRANT SELECT ON wallets, builder_approvals TO app_executor;
GRANT SELECT ON agent_keys TO app_executor;                                   -- incl. key_ciphertext
GRANT UPDATE (status, approved_at, revoked_at) ON agent_keys TO app_executor; -- on-chain approval revoked
GRANT SELECT ON strategies, strategy_versions TO app_executor;                -- incl. code_ciphertext (sandbox)
GRANT UPDATE (live_since) ON strategy_versions TO app_executor;
GRANT SELECT, UPDATE ON subscriptions TO app_executor;
GRANT SELECT, INSERT ON signals TO app_executor;
GRANT SELECT, INSERT, UPDATE ON orders, subscription_bar_runs, subscription_targets TO app_executor;
GRANT SELECT, INSERT, UPDATE ON fills, funding_events TO app_executor;
GRANT SELECT, INSERT ON ledger_accounts, ledger_transactions, ledger_entries TO app_executor;
GRANT SELECT ON ledger_balances, chain_heads TO app_executor;
GRANT SELECT, INSERT, UPDATE ON deposits TO app_executor;
GRANT SELECT ON withdrawals, payouts TO app_executor;
GRANT SELECT, INSERT ON profit_share_settlements TO app_executor;
GRANT SELECT ON posts TO app_executor;
GRANT SELECT, INSERT ON alerts TO app_executor;
GRANT SELECT, INSERT ON audit_log TO app_executor;
GRANT SELECT, INSERT, UPDATE ON system_flags TO app_executor;

-- ---------------------------------------------------------------- app_migrator
-- Owner of everything above; explicit grants too so a member can run seeds without SET ROLE.
GRANT ALL ON ALL TABLES IN SCHEMA public TO app_migrator;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app_migrator;

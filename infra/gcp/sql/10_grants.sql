-- Runs as `migrator` AFTER migrations (roles app_api / app_executor / app_migrator come from 0002_roles.sql;
-- migrator created them, so in PG16 it holds ADMIN OPTION and may grant them).
-- Maps the Cloud Run service accounts' IAM database users onto the application roles. Idempotent.
-- psql variables: db, api_user, executor_user
\set ON_ERROR_STOP on
SELECT set_config('grants.api', :'api_user', false) AS _a, set_config('grants.exec', :'executor_user', false) AS _b \gset
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api')
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_executor') THEN
    RAISE EXCEPTION 'roles app_api/app_executor missing: migrations (0002_roles.sql) have not run';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('grants.api'))
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('grants.exec')) THEN
    RAISE EXCEPTION 'IAM database users missing: run `db_bootstrap.sh users` first';
  END IF;
END $$;

GRANT app_api      TO :"api_user";
GRANT app_executor TO :"executor_user";

-- Per-login guard rails. Altering a role created by Cloud SQL (IAM users) may need privileges `migrator`
-- lacks on some Cloud SQL versions — then this only WARNS; the same timeouts are also passed by the app in
-- DATABASE_URL (`options=-c statement_timeout=...`, see infra/gcp/run/*.service.yaml), so they still apply.
DO $$
DECLARE
  stmts text[] := ARRAY[
    format('ALTER ROLE %I CONNECTION LIMIT 110', current_setting('grants.api')),
    format('ALTER ROLE %I SET statement_timeout = %L', current_setting('grants.api'), '15s'),
    format('ALTER ROLE %I SET lock_timeout = %L', current_setting('grants.api'), '5s'),
    format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', current_setting('grants.api'), '30s'),
    format('ALTER ROLE %I CONNECTION LIMIT 40', current_setting('grants.exec')),
    format('ALTER ROLE %I SET statement_timeout = %L', current_setting('grants.exec'), '300s'),
    format('ALTER ROLE %I SET lock_timeout = %L', current_setting('grants.exec'), '10s'),
    format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', current_setting('grants.exec'), '120s')];
  s text;
BEGIN
  FOREACH s IN ARRAY stmts LOOP
    BEGIN
      EXECUTE s;
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE WARNING 'skipped (insufficient privilege): %', s;
    END;
  END LOOP;
END $$;

-- Runs as `migrator` AFTER migrations (roles app_api / app_executor / app_migrator come from 0002).
-- Maps the Cloud Run service accounts' IAM database users onto the application roles and sets
-- per-login guard rails. Idempotent. psql variables: db, api_user, executor_user
\set ON_ERROR_STOP on
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api')
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_executor') THEN
    RAISE EXCEPTION 'roles app_api/app_executor missing: migrations (0002) have not run';
  END IF;
END $$;

GRANT app_api      TO :"api_user";
GRANT app_executor TO :"executor_user";
GRANT CONNECT ON DATABASE :"db" TO app_api, app_executor;

-- api: many short requests. executor: fewer, longer (settlement, reconciliation).
ALTER ROLE :"api_user"      CONNECTION LIMIT 110;
ALTER ROLE :"api_user"      SET statement_timeout = '15s';
ALTER ROLE :"api_user"      SET lock_timeout = '5s';
ALTER ROLE :"api_user"      SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE :"executor_user" CONNECTION LIMIT 40;
ALTER ROLE :"executor_user" SET statement_timeout = '300s';
ALTER ROLE :"executor_user" SET lock_timeout = '10s';
ALTER ROLE :"executor_user" SET idle_in_transaction_session_timeout = '120s';

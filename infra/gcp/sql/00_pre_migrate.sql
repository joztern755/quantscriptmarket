-- Runs as the built-in `migrator` user (member of cloudsqlsuperuser, CREATEROLE) BEFORE migrations.
-- Idempotent. psql variables: db
\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pgcrypto;              -- gen_random_uuid() & digest helpers
CREATE EXTENSION IF NOT EXISTS pgaudit;               -- instance flag cloudsql.enable_pgaudit=on, pgaudit.log=ddl,role
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;             -- nobody connects unless granted (0002 grants CONNECT)
REVOKE CREATE ON SCHEMA public FROM PUBLIC;           -- default since PG15; asserted anyway
ALTER DATABASE :"db" SET timezone TO 'UTC';           -- SPEC §11: UTC everywhere
ALTER DATABASE :"db" SET default_transaction_isolation TO 'read committed';

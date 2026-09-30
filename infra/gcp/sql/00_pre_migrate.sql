-- Runs as the built-in `migrator` user (member of cloudsqlsuperuser, CREATEROLE) BEFORE migrations.
-- Idempotent. psql variables: db
\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pgcrypto;              -- gen_random_uuid() & digest helpers
CREATE EXTENSION IF NOT EXISTS pgaudit;               -- instance flag cloudsql.enable_pgaudit=on, pgaudit.log=ddl,role
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;             -- nobody connects unless granted (0002 grants CONNECT)
REVOKE CREATE ON SCHEMA public FROM PUBLIC;           -- default since PG15; asserted anyway
ALTER DATABASE :"db" SET timezone TO 'UTC';           -- SPEC §11: UTC everywhere
ALTER DATABASE :"db" SET default_transaction_isolation TO 'read committed';
-- PG16: a NON-superuser with CREATEROLE (Cloud SQL's built-in users are not superusers) gets only ADMIN on the
-- roles it creates, not SET/INHERIT, so 0002_roles.sql's `ALTER ... OWNER TO app_migrator` would fail with
-- "must be able to SET ROLE app_migrator". Make roles created by this login self-granted with SET + INHERIT.
-- Takes effect for the NEXT session of this role (migrate.py connects afresh).
ALTER ROLE CURRENT_USER SET createrole_self_grant = 'set, inherit';
SET createrole_self_grant = 'set, inherit';            -- ... and for this session
-- Pre-create app_migrator (0002 creates it only IF NOT EXISTS, so this is compatible) and give it CREATE on
-- schema public up front: PG16 requires the NEW owner to have CREATE on the schema for `ALTER ... OWNER TO`,
-- which a superuser skips but Cloud SQL's migrator cannot. 0002 grants the same privileges again later.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
    CREATE ROLE app_migrator NOLOGIN;
  END IF;
  IF NOT pg_has_role(current_user, 'app_migrator', 'SET') THEN
    EXECUTE format('GRANT app_migrator TO %I WITH INHERIT TRUE, SET TRUE', current_user);
  END IF;
END $$;
GRANT USAGE, CREATE ON SCHEMA public TO app_migrator;

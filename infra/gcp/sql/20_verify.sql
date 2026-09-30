-- Asserts the SPEC §2.1 / §4 privilege model actually holds in the live database. Fails loudly.
-- psql variables: api_user, executor_user
\set ON_ERROR_STOP on
SELECT set_config('verify.api', :'api_user', false) AS _a, set_config('verify.exec', :'executor_user', false) AS _b \gset
DO $$
DECLARE
  api  text := current_setting('verify.api');
  ex   text := current_setting('verify.exec');
  t    text;
  bad  text[] := '{}';
BEGIN
  -- no login may be superuser / create roles / create databases / bypass RLS
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN (api, ex)
             AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls)) THEN
    bad := bad || 'api/executor login has elevated attributes'::text;
  END IF;
  -- api must never read agent key ciphertext (column privilege)
  IF to_regclass('public.agent_keys') IS NOT NULL
     AND has_column_privilege(api, 'public.agent_keys', 'key_ciphertext', 'SELECT') THEN
    bad := bad || 'api can SELECT agent_keys.key_ciphertext'::text;
  END IF;
  -- append-only tables: no UPDATE/DELETE/TRUNCATE for either runtime login
  FOREACH t IN ARRAY ARRAY['ledger_entries','ledger_transactions','audit_log','consents'] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      IF has_table_privilege(api, 'public.' || t, 'UPDATE') OR has_table_privilege(api, 'public.' || t, 'DELETE')
         OR has_table_privilege(api, 'public.' || t, 'TRUNCATE') THEN
        bad := bad || format('api can modify %s', t);
      END IF;
      IF has_table_privilege(ex, 'public.' || t, 'UPDATE') OR has_table_privilege(ex, 'public.' || t, 'DELETE')
         OR has_table_privilege(ex, 'public.' || t, 'TRUNCATE') THEN
        bad := bad || format('executor can modify %s', t);
      END IF;
    ELSE
      RAISE NOTICE 'table % not present yet — skipped', t;
    END IF;
  END LOOP;
  -- api must never read creator strategy code ciphertext (SECURITY §6; REVIEW_WEB_INFRA L7)
  IF to_regclass('public.strategy_versions') IS NOT NULL
     AND EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'strategy_versions' AND column_name = 'code_ciphertext')
     AND has_column_privilege(api, 'public.strategy_versions', 'code_ciphertext', 'SELECT') THEN
    bad := bad || 'api can SELECT strategy_versions.code_ciphertext'::text;
  END IF;
  -- runtime logins must not inherit superuser-like or owner roles (Cloud SQL: cloudsqlsuperuser; the schema owner
  -- app_migrator; PG16 predefined read/write-everything and server-file roles)
  FOREACH t IN ARRAY ARRAY['cloudsqlsuperuser','app_migrator','pg_read_all_data','pg_write_all_data',
                           'pg_read_server_files','pg_write_server_files','pg_execute_server_program',
                           'pg_signal_backend','pg_database_owner'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = t) THEN
      IF pg_has_role(api, t, 'MEMBER') THEN bad := bad || format('api is a member of %s', t); END IF;
      IF pg_has_role(ex, t, 'MEMBER') THEN bad := bad || format('executor is a member of %s', t); END IF;
    END IF;
  END LOOP;
  -- agent attestations (0013) are written by the executor only; the api can read, never write them
  IF to_regclass('public.agent_keys') IS NOT NULL
     AND EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'agent_keys' AND column_name = 'attestation_sig')
     AND has_column_privilege(api, 'public.agent_keys', 'attestation_sig', 'UPDATE') THEN
    bad := bad || 'api can UPDATE agent_keys.attestation_sig'::text;
  END IF;
  -- neither runtime login may create objects
  IF has_schema_privilege(api, 'public', 'CREATE') OR has_schema_privilege(ex, 'public', 'CREATE') THEN
    bad := bad || 'runtime login can CREATE in schema public'::text;
  END IF;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION 'privilege model violated: %', array_to_string(bad, '; ');
  END IF;
  RAISE NOTICE 'privilege model OK for % and %', api, ex;
END $$;

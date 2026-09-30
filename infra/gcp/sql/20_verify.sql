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
  -- neither runtime login may create objects
  IF has_schema_privilege(api, 'public', 'CREATE') OR has_schema_privilege(ex, 'public', 'CREATE') THEN
    bad := bad || 'runtime login can CREATE in schema public'::text;
  END IF;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION 'privilege model violated: %', array_to_string(bad, '; ');
  END IF;
  RAISE NOTICE 'privilege model OK for % and %', api, ex;
END $$;

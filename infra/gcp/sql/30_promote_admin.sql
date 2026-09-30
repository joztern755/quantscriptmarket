-- Promote an EXISTING user (signed in once, MFA enrolled, active) to admin. psql variables: email
-- Runs as `migrator` (break-glass). Prefer the backend admin CLI if one exists, because it also writes the
-- hash-chained audit_log entry; if you use this file, record the promotion in audit_log through the app
-- (admin console) right after, and tell the second admin. Payout approval needs TWO distinct admins.
\set ON_ERROR_STOP on
SELECT set_config('promote.email', :'email', false) AS _e \gset
DO $$
DECLARE n int;
BEGIN
  UPDATE users SET role = 'admin'
   WHERE lower(email) = lower(current_setting('promote.email')) AND status = 'active' AND mfa_enrolled;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 1 THEN
    RAISE EXCEPTION 'expected exactly 1 active, MFA-enrolled user with that e-mail; matched %', n;
  END IF;
END $$;
SELECT id, email, role FROM users WHERE role = 'admin' ORDER BY created_at;

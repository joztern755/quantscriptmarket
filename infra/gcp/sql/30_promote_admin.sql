-- Promote an EXISTING user (signed in once, MFA enrolled, active) to admin. psql variables: email
-- Runs as `migrator` (break-glass). Uses promote_admin() (migrations/0011_api_fixes.sql, SECURITY DEFINER): the
-- users_role_guard trigger refuses any other way to grant or remove the admin role (app_api cannot), and the function
-- writes the hash-chained audit_log entry itself. The e-mail must ALSO be in the API's ADMIN_EMAILS allowlist
-- (REVIEW_AUTH_API F12). Tell the second admin. Payout approval needs TWO distinct admins.
\set ON_ERROR_STOP on
SELECT promote_admin(:'email') AS promoted_user_id;
SELECT id, email, role FROM users WHERE role = 'admin' ORDER BY created_at;

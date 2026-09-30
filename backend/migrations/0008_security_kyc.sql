-- =====================================================================================================
-- 0008_security_kyc.sql — sign-in security events + single-admin creator KYC (owner decisions 30 Sep 2026).
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
--
--   user_devices          first/last seen per (user, device hash). device_hash = HMAC(pepper, "device" || key) where
--                         key = the web's X-Device-Id (random id kept in the browser) or, without it, the user agent
--                         with version numbers stripped (browser family + OS). A NEW device for a user who already has
--                         one → mandatory `new_device_login` alert (SPEC §12). The raw key is never stored.
--   users.mfa_factor_hash HMAC of the Firebase second-factor identifier (firebase.second_factor_identifier) last seen
--                         in a TOTP sign-in. A different factor → mandatory `mfa_changed` alert (SPEC §5.5 "MFA reset").
--   kyc_status += 'provider_approved'   the KYC provider (Sumsub) returned GREEN; ONE admin must confirm before the
--                         status becomes 'approved' (never auto-approved). Manual provider: one admin decides directly.
--                         Listing strategies and payouts keep their two-admin maker-checker (admin_changes / payouts).
-- =====================================================================================================

-- New enum value (not used elsewhere in this migration: PG allows ADD VALUE in a transaction but not its use).
ALTER TYPE kyc_status ADD VALUE IF NOT EXISTS 'provider_approved';

CREATE TABLE user_devices (
    user_id      uuid NOT NULL REFERENCES users(id),
    device_hash  text NOT NULL CHECK (device_hash ~ '^[0-9a-f]{32,128}$'),
    label        text CHECK (label IS NULL OR length(label) <= 64),        -- coarse, e.g. "Chrome on macOS"
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, device_hash)
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_factor_hash text
    CHECK (mfa_factor_hash IS NULL OR mfa_factor_hash ~ '^[0-9a-f]{32,128}$');

-- ---------------------------------------------------------------- grants (no DELETE for anyone)
GRANT SELECT, INSERT, UPDATE ON user_devices TO app_api;
GRANT ALL ON user_devices TO app_migrator;
-- users: app_api already has table-level SELECT/INSERT/UPDATE (0002), which covers the new column.

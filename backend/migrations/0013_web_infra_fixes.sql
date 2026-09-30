-- =====================================================================================================
-- 0013_web_infra_fixes.sql — signing trust anchors (docs/security/REVIEW_WEB_INFRA.md H1).
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
--
--   agent_keys.attestation_*   the EXECUTOR's attestation that the sealed key of this row decrypts to agent_address:
--                              a Cloud KMS EC_SIGN_P256_SHA256 signature over
--                                  aijalon-agent-v1|{user_id}|{agent_address}
--                              (job /v1/internal/attest-agents, app/execution/trust_jobs.py). The browser verifies it
--                              with the public key pinned in web/public/app-config.json before asking the wallet to
--                              sign ApproveAgent. Only app_executor may write these columns; they are set once
--                              (NULL → value) and are then immutable (trigger); an INSERT can never carry them, so
--                              the api cannot pre-fill an attestation (it could not forge the signature anyway).
--                              attestation_failed_at/_error: the sealed key did NOT open to agent_address (tampering or
--                              a bug) — the job stops retrying that row and raises a critical ops alert.
--   wallets.proof_*            the EIP-4361 message + personal_sign signature of the LATEST ownership proof of this
--                              wallet (POST /v1/wallets/verify). Admins' browsers re-verify it before approving or
--                              signing a payout/withdrawal to that wallet (web/src/pages/_shared/walletproof.ts).
-- =====================================================================================================

-- ---------------------------------------------------------------- agent attestation
ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS attestation_sig          text;
ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS attestation_key_version  text;
ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS attested_at              timestamptz;
ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS attestation_failed_at    timestamptz;
ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS attestation_error        text;

DO $$ BEGIN
  ALTER TABLE agent_keys ADD CONSTRAINT agent_keys_attestation_shape CHECK (
        (attestation_sig IS NULL AND attestation_key_version IS NULL AND attested_at IS NULL)
     OR (attestation_sig ~ '^[A-Za-z0-9+/]{40,200}={0,2}$' AND attestation_key_version IS NOT NULL
         AND length(attestation_key_version) BETWEEN 1 AND 300 AND attested_at IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE agent_keys ADD CONSTRAINT agent_keys_attestation_error_len
    CHECK (attestation_error IS NULL OR length(attestation_error) <= 200);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- the job's work queue: live agents without an attestation that have not failed
CREATE INDEX IF NOT EXISTS agent_keys_attest_todo_idx ON agent_keys (created_at)
    WHERE attestation_sig IS NULL AND attestation_failed_at IS NULL AND status IN ('pending_approval', 'active');

CREATE OR REPLACE FUNCTION agent_keys_attestation_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.attestation_sig IS NOT NULL OR NEW.attestation_key_version IS NOT NULL OR NEW.attested_at IS NOT NULL
       OR NEW.attestation_failed_at IS NOT NULL OR NEW.attestation_error IS NOT NULL THEN
      RAISE EXCEPTION 'agent_keys: attestation columns are written only by the executor attestation job'
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.attestation_sig IS NOT NULL AND (
       NEW.attestation_sig IS DISTINCT FROM OLD.attestation_sig
    OR NEW.attestation_key_version IS DISTINCT FROM OLD.attestation_key_version
    OR NEW.attested_at IS DISTINCT FROM OLD.attested_at) THEN
    RAISE EXCEPTION 'agent_keys: an attestation is immutable once written' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.agent_address IS DISTINCT FROM OLD.agent_address OR NEW.user_id IS DISTINCT FROM OLD.user_id
     OR NEW.key_ciphertext IS DISTINCT FROM OLD.key_ciphertext THEN
    RAISE EXCEPTION 'agent_keys: identity and key material are immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS agent_keys_attestation_guard ON agent_keys;
CREATE TRIGGER agent_keys_attestation_guard BEFORE INSERT OR UPDATE ON agent_keys
    FOR EACH ROW EXECUTE FUNCTION agent_keys_attestation_guard();

GRANT SELECT (attestation_sig, attestation_key_version, attested_at, attestation_failed_at) ON agent_keys TO app_api;
GRANT UPDATE (attestation_sig, attestation_key_version, attested_at, attestation_failed_at, attestation_error)
    ON agent_keys TO app_executor;

-- ---------------------------------------------------------------- wallet ownership proof (payout destinations)
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS proof_message     text;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS proof_signature   text;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS proof_recorded_at timestamptz;
DO $$ BEGIN
  ALTER TABLE wallets ADD CONSTRAINT wallets_proof_shape CHECK (
        (proof_message IS NULL AND proof_signature IS NULL AND proof_recorded_at IS NULL)
     OR (length(proof_message) BETWEEN 40 AND 2000 AND proof_signature ~ '^0x[0-9a-fA-F]{130}$'
         AND proof_recorded_at IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- app_api already holds SELECT, INSERT, UPDATE on wallets (0002); app_executor SELECT.

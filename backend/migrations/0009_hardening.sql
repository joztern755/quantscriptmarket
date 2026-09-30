-- =====================================================================================================
-- 0009_hardening.sql — shared Hyperliquid rate budget + held-USDC-deposit release (maker-checker).
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
-- Explicit GRANTs per table; nobody gets DELETE.
--
--   hl_rate_budget       per-(egress IP key, minute slot) weight counters shared by EVERY process that calls the
--                        Hyperliquid /info endpoint from that IP (app/hl/budget.py). A ring of 60 slots per key
--                        (slot = minute of the hour): a slot whose window_start is older than the current minute is
--                        reset on first use, so the table stays bounded without DELETE. spent_tick = executor tick
--                        (priority, never blocked), spent_jobs = data jobs + reconcile (capped at
--                        budget − max(spent_tick, tick reserve), see config.HlLimits).
--   usdc_held_deposits   what deposits-scan knew about a treasury transfer it booked to suspense:usdc_unattributed
--                        (kind deposit_held, key usdc_hl:{hash}): the on-chain SENDER, amount and reason. The admin
--                        refund goes back to this recorded sender (never to an address typed by an admin, except
--                        for transfers held before this table existed — then the sender is verified on-chain).
--   suspense_releases    maker-checker release of one held transfer (RUNBOOK §13.3): admin A proposes
--                        (attribute to a user whose VERIFIED wallet sent it, or refund to the sender), admin B ≠ A
--                        approves → ONE ledger transaction, key suspense_release:{hash}:
--                          attribute: suspense:usdc_unattributed +amt | user:{id}:fee_balance −amt (kind
--                                     suspense_release; plus a deposits row: usdc_hl, credited, withdrawable)
--                          refund:    suspense:usdc_unattributed +amt | refunds:usdc_pending −amt (kind
--                                     suspense_refund), then an admin signs the treasury usdSend to the sender with
--                                     the hardware wallet (like payouts) and records the tx hash, verified on-chain →
--                                     refunds:usdc_pending +amt | treasury:hl_usdc −amt (key suspense_refund:{hash}:sent).
--                        At most one non-rejected release per transfer (partial unique index); no self-approval
--                        (CHECK); decided proposal fields are immutable (trigger).
-- Seed: ledger account refunds:usdc_pending (liability) — held USDC approved for refund, not yet sent.
-- =====================================================================================================

-- ---------------------------------------------------------------- shared Hyperliquid rate budget
CREATE TABLE hl_rate_budget (
    egress_key    text NOT NULL CHECK (egress_key ~ '^[a-z0-9_.:-]{1,64}$'),
    slot          smallint NOT NULL CHECK (slot BETWEEN 0 AND 59),
    window_start  timestamptz NOT NULL,
    spent_tick    integer NOT NULL DEFAULT 0 CHECK (spent_tick >= 0),
    spent_jobs    integer NOT NULL DEFAULT 0 CHECK (spent_jobs >= 0),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (egress_key, slot)
);

-- ---------------------------------------------------------------- held USDC deposits
CREATE TABLE usdc_held_deposits (
    tx_hash         text PRIMARY KEY CHECK (tx_hash ~ '^0x[0-9a-f]{64}$'),
    created_at      timestamptz NOT NULL DEFAULT now(),
    sender_address  eth_address NOT NULL,
    amount_micro    bigint NOT NULL CHECK (amount_micro > 0),
    reason          text NOT NULL CHECK (length(reason) BETWEEN 1 AND 200),
    transfer_time   timestamptz NOT NULL,
    held_tx_id      uuid NOT NULL REFERENCES ledger_transactions(id)
);

CREATE TABLE suspense_releases (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    tx_hash              text NOT NULL CHECK (tx_hash ~ '^0x[0-9a-f]{64}$'),
    held_tx_id           uuid NOT NULL REFERENCES ledger_transactions(id),
    amount_micro         bigint NOT NULL CHECK (amount_micro > 0),
    action               text NOT NULL CHECK (action IN ('attribute', 'refund')),
    user_id              uuid REFERENCES users(id),                 -- attribute: the credited user
    sender_address       eth_address NOT NULL,                      -- on-chain sender (refund destination)
    sender_source        text NOT NULL CHECK (sender_source IN ('scan', 'onchain')),
    evidence             text NOT NULL CHECK (length(evidence) BETWEEN 5 AND 1000),
    status               text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'sent', 'rejected')),
    maker_admin          uuid NOT NULL REFERENCES users(id),
    checker_admin        uuid REFERENCES users(id),
    decided_at           timestamptz,
    decision_reason      text CHECK (decision_reason IS NULL OR length(decision_reason) BETWEEN 5 AND 500),
    release_tx_id        uuid REFERENCES ledger_transactions(id),   -- suspense → fee balance / refunds:usdc_pending
    refund_tx_hash       text UNIQUE CHECK (refund_tx_hash IS NULL OR refund_tx_hash ~ '^0x[0-9a-f]{64}$'),
    refund_ledger_tx_id  uuid REFERENCES ledger_transactions(id),   -- refunds:usdc_pending → treasury
    sent_by              uuid REFERENCES users(id),
    sent_at              timestamptz,
    CONSTRAINT suspense_releases_four_eyes CHECK (checker_admin IS NULL OR checker_admin <> maker_admin),
    CONSTRAINT suspense_releases_target CHECK ((action = 'attribute') = (user_id IS NOT NULL)),
    CONSTRAINT suspense_releases_not_self CHECK (maker_admin IS DISTINCT FROM user_id
                                                 AND checker_admin IS DISTINCT FROM user_id),
    CONSTRAINT suspense_releases_decided CHECK ((status = 'proposed') = (decided_at IS NULL)),
    CONSTRAINT suspense_releases_approved CHECK (status NOT IN ('approved', 'sent')
                                                 OR (checker_admin IS NOT NULL AND release_tx_id IS NOT NULL)),
    CONSTRAINT suspense_releases_sent CHECK ((status = 'sent') = (refund_tx_hash IS NOT NULL
                                                                   AND refund_ledger_tx_id IS NOT NULL)),
    CONSTRAINT suspense_releases_sent_is_refund CHECK (status <> 'sent' OR action = 'refund')
);
CREATE UNIQUE INDEX suspense_releases_one_live ON suspense_releases (tx_hash) WHERE status <> 'rejected';
CREATE INDEX suspense_releases_status_idx ON suspense_releases (status, created_at DESC);

-- proposed → approved | rejected; approved (refund) → sent. Proposal fields never change.
CREATE FUNCTION suspense_releases_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tx_hash <> OLD.tx_hash OR NEW.held_tx_id <> OLD.held_tx_id OR NEW.amount_micro <> OLD.amount_micro
       OR NEW.action <> OLD.action OR NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.sender_address <> OLD.sender_address OR NEW.sender_source <> OLD.sender_source
       OR NEW.evidence <> OLD.evidence OR NEW.maker_admin <> OLD.maker_admin OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'suspense_releases proposal fields are immutable' USING ERRCODE = 'AJ422';
    END IF;
    IF NOT ((OLD.status = 'proposed' AND NEW.status IN ('approved', 'rejected'))
            OR (OLD.status = 'approved' AND NEW.status = 'sent' AND OLD.action = 'refund')) THEN
        RAISE EXCEPTION 'suspense_releases % cannot go % -> %', OLD.id, OLD.status, NEW.status USING ERRCODE = 'AJ403';
    END IF;
    IF OLD.status = 'approved' AND (NEW.checker_admin IS DISTINCT FROM OLD.checker_admin
                                    OR NEW.release_tx_id IS DISTINCT FROM OLD.release_tx_id
                                    OR NEW.decided_at IS DISTINCT FROM OLD.decided_at) THEN
        RAISE EXCEPTION 'suspense_releases decision fields are immutable' USING ERRCODE = 'AJ422';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER suspense_releases_guard BEFORE UPDATE ON suspense_releases
    FOR EACH ROW EXECUTE FUNCTION suspense_releases_guard();

-- ---------------------------------------------------------------- seed
INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative) VALUES
    ('refunds:usdc_pending', 'liability', NULL, false)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------- grants (no DELETE for anyone)
GRANT SELECT, INSERT, UPDATE ON hl_rate_budget TO app_executor;
GRANT SELECT ON hl_rate_budget TO app_api;                           -- admin console: budget usage
GRANT SELECT, INSERT ON usdc_held_deposits TO app_executor;          -- deposits-scan records held transfers
GRANT SELECT ON usdc_held_deposits TO app_api;
GRANT SELECT, INSERT, UPDATE ON suspense_releases TO app_api;
GRANT SELECT ON suspense_releases TO app_executor;                   -- reconcile / reports
GRANT ALL ON hl_rate_budget, usdc_held_deposits, suspense_releases TO app_migrator;

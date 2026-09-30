-- =====================================================================================================
-- 0011_api_fixes.sql — API / billing / account security fixes (docs/security/REVIEW_AUTH_API.md F2–F18,
-- docs/security/REVIEW_MONEY.md H3 H4 H5 M1 M2 M4 M6 M8 L1 L2 L6 L7).
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
-- Explicit GRANTs per table; nobody gets DELETE.
--
--   subscriptions.pre_pause_status / pre_pause_past_due_since   (F3/H3) the billing state saved when the user pauses;
--                         unpause restores it (never a fresh `active`, never a new grace period).
--   subscriptions.price_monthly_micro / profit_share_bps          (M8) terms PINNED at subscribe (BEFORE INSERT
--                         trigger fills them from the strategy when the caller did not); immutable afterwards. Renewals
--                         and profit share use the pinned terms, so a later price change (in-house maker-checker price,
--                         or a creator re-pricing a strategy before re-listing) applies to NEW subscriptions only.
--   subscriptions.end_reason                                      (H5) 'strategy_delisted' when a delisting ended it.
--                         Delisting a strategy moves its live subscriptions to `closing` (cancel_positions = 'close':
--                         the executor flattens reduce-only, then marks them cancelled; settlement never renews or
--                         reactivates `closing`) and user-paused ones to `cancelled` (leave: the executor was not
--                         trading them); every affected user gets a mandatory `strategy_ended` alert. Existing live
--                         subscriptions of already-delisted strategies are moved the same way below.
--   users.security_hold_until                                     (F5) withdrawals/payouts refused until then (set
--                         48 h after an MFA change or a new-device / new-country sign-in).
--   users.referral_flagged_at / referral_flag_reason              (F7/M2) self-referral suspected → no referral reward
--                         from this referee until ops clears it.
--   user_ip_nets                                                  (F7) peppered hash of the sign-in network (/24 IPv4,
--                         /64 IPv6) per user — same-network heuristic for self-referral.
--   users role guard                                              (F17) only app_migrator (members) or a superuser may
--                         grant or remove `admin`; the promote script calls promote_admin() (SECURITY DEFINER).
--   admin_changes                                                 (F6/F12) status 'cancelled' (auto-cancel of pending
--                         changes when a strategy is delisted/paused/rejected — no checker), kind 'user_suspend'
--                         (suspending another ADMIN needs a second admin).
--   withdrawals/payouts send_* + unique tx_hash + payout_tx_hashes (F9/M4) typed data is issued ONCE per payout
--                         (nonce pinned; the same payload is returned again), a payout whose typed data was issued
--                         cannot be rejected for 72 h (the signed usdSend could still execute), and one on-chain
--                         transfer can settle only one payout/withdrawal (DB-unique).
--   fee_funding_card_unspent() / fee_tx_card_part() / payable_card_held()   (F4/H4) funding-source tracking computed
--                         from the ledger: card-funded credits (ledger keys stripe:…) are spent FIRST; withdrawable
--                         = USDC-funded unspent only; creator/referrer earnings paid from card-funded spending are
--                         held for the dispute window.
--   user_has_paid_activity()                                      (M2) a referee counts (tier counts and referral
--                         rewards) only after a real paid activity — not for free showcase-only usage.
-- =====================================================================================================

-- ---------------------------------------------------------------- subscriptions (F3/H3, M8, H5)
ALTER TABLE subscriptions
    ADD COLUMN pre_pause_status          subscription_status,
    ADD COLUMN pre_pause_past_due_since  timestamptz,
    ADD COLUMN price_monthly_micro       bigint CHECK (price_monthly_micro IS NULL OR price_monthly_micro >= 0),
    ADD COLUMN profit_share_bps          integer CHECK (profit_share_bps IS NULL OR profit_share_bps BETWEEN 0 AND 1200),
    ADD COLUMN end_reason                text CHECK (end_reason IS NULL OR end_reason IN ('user', 'strategy_delisted'));

ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_pre_pause_not_paused
    CHECK (pre_pause_status IS NULL OR pre_pause_status IN ('pending', 'active', 'past_due', 'reduce_only'));

-- Pin the terms of existing subscriptions (the strategy's current terms are what they were sold at: listed terms
-- cannot change, SPEC §12 / creator PATCH refuses non-draft/review strategies).
UPDATE subscriptions s
   SET price_monthly_micro = coalesce(st.price_monthly_micro, 0),
       profit_share_bps = coalesce(st.profit_share_bps, 0)
  FROM strategies st
 WHERE st.id = s.strategy_id AND (s.price_monthly_micro IS NULL OR s.profit_share_bps IS NULL);

CREATE FUNCTION subscriptions_pin_terms() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    st record;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.price_monthly_micro IS NULL OR NEW.profit_share_bps IS NULL THEN
            SELECT price_monthly_micro, profit_share_bps INTO st FROM strategies WHERE id = NEW.strategy_id;
            NEW.price_monthly_micro := coalesce(NEW.price_monthly_micro, st.price_monthly_micro, 0);
            NEW.profit_share_bps := coalesce(NEW.profit_share_bps, st.profit_share_bps, 0);
        END IF;
        RETURN NEW;
    END IF;
    IF (OLD.price_monthly_micro IS NOT NULL AND NEW.price_monthly_micro IS DISTINCT FROM OLD.price_monthly_micro)
       OR (OLD.profit_share_bps IS NOT NULL AND NEW.profit_share_bps IS DISTINCT FROM OLD.profit_share_bps) THEN
        RAISE EXCEPTION 'subscription terms are pinned at subscribe and cannot change' USING ERRCODE = 'AJ422';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER subscriptions_pin_terms BEFORE INSERT OR UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION subscriptions_pin_terms();

-- H5: live subscriptions of strategies that are ALREADY delisted (before this fix they were left reduce_only and
-- settlement re-activated and billed them).
UPDATE subscriptions s
   SET status = 'closing', cancel_positions = 'close', end_reason = 'strategy_delisted', status_changed_at = now(),
       pre_pause_status = NULL, pre_pause_past_due_since = NULL
  FROM strategies st
 WHERE st.id = s.strategy_id AND st.status = 'delisted' AND s.status IN ('active', 'past_due', 'reduce_only');
UPDATE subscriptions s
   SET status = 'cancelled', cancel_positions = 'leave', end_reason = 'strategy_delisted', status_changed_at = now(),
       cancelled_at = coalesce(s.cancelled_at, now()), pre_pause_status = NULL, pre_pause_past_due_since = NULL
  FROM strategies st
 WHERE st.id = s.strategy_id AND st.status = 'delisted' AND s.status IN ('pending', 'paused_user');

-- ---------------------------------------------------------------- users (F5, F7/M2)
ALTER TABLE users
    ADD COLUMN security_hold_until   timestamptz,
    ADD COLUMN referral_flagged_at   timestamptz,
    ADD COLUMN referral_flag_reason  text CHECK (referral_flag_reason IS NULL OR length(referral_flag_reason) <= 200);

CREATE TABLE user_ip_nets (
    user_id     uuid NOT NULL REFERENCES users(id),
    net_hash    text NOT NULL CHECK (net_hash ~ '^[0-9a-f]{32,128}$'),   -- HMAC(pepper, "ipnet" || /24 or /64 prefix)
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, net_hash)
);
CREATE INDEX user_ip_nets_net_idx ON user_ip_nets (net_hash, last_seen DESC);
CREATE INDEX user_devices_hash_idx ON user_devices (device_hash);

-- ---------------------------------------------------------------- F17: role guard (admin only via app_migrator)
CREATE FUNCTION users_role_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    privileged boolean;
BEGIN
    IF (TG_OP = 'INSERT' AND NEW.role = 'admin')
       OR (TG_OP = 'UPDATE' AND NEW.role IS DISTINCT FROM OLD.role AND (NEW.role = 'admin' OR OLD.role = 'admin')) THEN
        SELECT r.rolsuper OR pg_has_role(current_user, 'app_migrator', 'USAGE') INTO privileged
          FROM pg_roles r WHERE r.rolname = current_user;
        IF NOT coalesce(privileged, false) THEN
            RAISE EXCEPTION 'the admin role can only be granted or removed by promote_admin()' USING ERRCODE = 'AJ403';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER users_role_guard BEFORE INSERT OR UPDATE OF role ON users
    FOR EACH ROW EXECUTE FUNCTION users_role_guard();

-- Break-glass promotion (infra/gcp/sql/30_promote_admin.sql): exactly one active, MFA-enrolled user with that e-mail.
CREATE FUNCTION promote_admin(p_email text) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    n int;
    uid uuid;
BEGIN
    SELECT count(*) INTO n FROM users
     WHERE lower(email) = lower(p_email) AND status = 'active' AND mfa_enrolled;
    IF n <> 1 THEN
        RAISE EXCEPTION 'expected exactly 1 active, MFA-enrolled user with that e-mail; matched %', n
            USING ERRCODE = 'AJ404';
    END IF;
    UPDATE users SET role = 'admin'
     WHERE lower(email) = lower(p_email) AND status = 'active' AND mfa_enrolled
    RETURNING id INTO uid;
    INSERT INTO audit_log (actor, action, target, payload)
    VALUES ('system:promote_admin', 'user.promote_admin', 'user:' || uid::text, '{}'::jsonb);
    RETURN uid;
END
$$;
REVOKE ALL ON FUNCTION promote_admin(text) FROM PUBLIC;

-- ---------------------------------------------------------------- admin_changes (F6, F12)
ALTER TABLE admin_changes DROP CONSTRAINT admin_changes_kind_check;
ALTER TABLE admin_changes ADD CONSTRAINT admin_changes_kind_check
    CHECK (kind IN ('strategy_list', 'strategy_price', 'user_unsuspend', 'kyc_approve', 'user_suspend'));
ALTER TABLE admin_changes DROP CONSTRAINT admin_changes_status_check;
ALTER TABLE admin_changes ADD CONSTRAINT admin_changes_status_check
    CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled'));
ALTER TABLE admin_changes DROP CONSTRAINT admin_changes_decided;
ALTER TABLE admin_changes ADD CONSTRAINT admin_changes_decided CHECK (
       (status = 'pending' AND checker_admin IS NULL AND decided_at IS NULL)
    OR (status IN ('approved', 'rejected') AND checker_admin IS NOT NULL AND decided_at IS NOT NULL)
    OR (status = 'cancelled' AND checker_admin IS NULL AND decided_at IS NOT NULL));

-- ---------------------------------------------------------------- payouts / withdrawals (F9, M4)
ALTER TABLE withdrawals
    ADD COLUMN send_nonce      bigint CHECK (send_nonce IS NULL OR send_nonce > 0),
    ADD COLUMN send_issued_at  timestamptz,
    ADD COLUMN send_issued_by  uuid REFERENCES users(id);
ALTER TABLE payouts
    ADD COLUMN send_nonce      bigint CHECK (send_nonce IS NULL OR send_nonce > 0),
    ADD COLUMN send_issued_at  timestamptz,
    ADD COLUMN send_issued_by  uuid REFERENCES users(id);
CREATE UNIQUE INDEX withdrawals_tx_hash_uq ON withdrawals (lower(tx_hash)) WHERE tx_hash IS NOT NULL;
CREATE UNIQUE INDEX payouts_tx_hash_uq ON payouts (lower(tx_hash)) WHERE tx_hash IS NOT NULL;

-- One on-chain transfer settles ONE row across both tables (inserted in the mark-sent transaction).
CREATE TABLE payout_tx_hashes (
    tx_hash     text PRIMARY KEY CHECK (tx_hash ~ '^0x[0-9a-f]{64}$'),
    created_at  timestamptz NOT NULL DEFAULT now(),
    kind        text NOT NULL CHECK (kind IN ('withdrawal', 'payout')),
    payout_id   uuid NOT NULL,
    UNIQUE (kind, payout_id)
);

CREATE FUNCTION payout_send_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.send_nonce IS NOT NULL AND (NEW.send_nonce IS DISTINCT FROM OLD.send_nonce
                                       OR NEW.send_issued_at IS DISTINCT FROM OLD.send_issued_at) THEN
        RAISE EXCEPTION 'the usdSend nonce of this payout is already issued' USING ERRCODE = 'AJ409';
    END IF;
    IF NEW.send_nonce IS NOT NULL AND OLD.send_nonce IS NULL AND OLD.status <> 'approved_2' THEN
        RAISE EXCEPTION 'typed data can only be issued after two approvals' USING ERRCODE = 'AJ409';
    END IF;
    -- HL accepts a usdSend nonce (= time ms) only inside a short window around its own clock; after 72 h an issued,
    -- signed but never submitted payload can no longer execute, so a reject cannot double-pay.
    IF NEW.status = 'rejected' AND OLD.status <> 'rejected' AND OLD.send_issued_at IS NOT NULL
       AND OLD.send_issued_at > now() - interval '72 hours' THEN
        RAISE EXCEPTION 'typed data was issued; record the transfer or wait 72 h before rejecting'
            USING ERRCODE = 'AJ409';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER withdrawals_send_guard BEFORE UPDATE ON withdrawals
    FOR EACH ROW EXECUTE FUNCTION payout_send_guard();
CREATE TRIGGER payouts_send_guard BEFORE UPDATE ON payouts
    FOR EACH ROW EXECUTE FUNCTION payout_send_guard();

-- ---------------------------------------------------------------- funding source (F4, H4)
-- Replays one user's fee-balance history in ledger order. Card credits (idempotency key 'stripe:%' with a credit)
-- add to the card lot — minus whatever part of them paid an existing debt (negative balance), which is spent at
-- once; card reversals (refund / dispute: 'stripe:%' debits) and every SPEND reduce the lot first (floored at 0);
-- withdrawal holds never touch it (they may only draw on USDC money). The lot never exceeds the positive balance.
-- Everything else credited (USDC top-ups, attributed suspense, released holds) is USDC-funded.
CREATE FUNCTION fee_funding_card_unspent(p_user uuid, p_before_seq bigint DEFAULT NULL) RETURNS bigint
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
    r record;
    cu bigint := 0;
    bal bigint := 0;
BEGIN
    FOR r IN
        SELECT e.amount_micro AS amt, t.kind, t.idempotency_key AS k
          FROM ledger_entries e
          JOIN ledger_accounts a ON a.id = e.account_id
          JOIN ledger_transactions t ON t.id = e.tx_id
         WHERE a.code = 'user:' || p_user::text || ':fee_balance'
           AND (p_before_seq IS NULL OR t.seq < p_before_seq)
         ORDER BY t.seq, e.id
    LOOP
        IF r.amt < 0 THEN
            IF r.k LIKE 'stripe:%' THEN
                cu := cu + greatest(0, -r.amt - greatest(0, -bal));
            END IF;
            bal := bal - r.amt;
        ELSE
            bal := bal - r.amt;
            IF r.kind <> 'withdrawal_hold' THEN
                cu := greatest(0, cu - r.amt);
            END IF;
        END IF;
        cu := least(cu, greatest(bal, 0));
    END LOOP;
    RETURN cu;
END
$$;

-- Card-funded part of the fee-balance SPEND inside one ledger transaction (0 for non-spends).
CREATE FUNCTION fee_tx_card_part(p_tx uuid) RETURNS TABLE (spend_micro bigint, card_micro bigint)
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
    r record;
    cu bigint;
BEGIN
    spend_micro := 0;
    card_micro := 0;
    FOR r IN
        SELECT e.amount_micro AS amt, a.owner_user_id AS uid, t.seq, t.kind, t.idempotency_key AS k
          FROM ledger_entries e
          JOIN ledger_accounts a ON a.id = e.account_id
          JOIN ledger_transactions t ON t.id = e.tx_id
         WHERE e.tx_id = p_tx AND e.amount_micro > 0 AND a.code ~ '^user:[^:]+:fee_balance$'
    LOOP
        IF r.k LIKE 'stripe:%' OR r.kind = 'withdrawal_hold' THEN
            CONTINUE;
        END IF;
        cu := fee_funding_card_unspent(r.uid, r.seq);
        spend_micro := spend_micro + r.amt;
        card_micro := card_micro + least(r.amt, cu);
    END LOOP;
    RETURN NEXT;
END
$$;

-- Earnings on a creator/referrer payable that came from card-funded spending in the last dispute window: they stay
-- on the payable but cannot be requested for payout until the window has passed.
CREATE FUNCTION payable_card_held(p_code text, p_since timestamptz) RETURNS bigint
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
    r record;
    part record;
    held bigint := 0;
BEGIN
    FOR r IN
        SELECT t.id AS tx_id, -e.amount_micro AS credited, t.kind, t.idempotency_key AS k
          FROM ledger_entries e
          JOIN ledger_accounts a ON a.id = e.account_id
          JOIN ledger_transactions t ON t.id = e.tx_id
         WHERE a.code = p_code AND e.amount_micro < 0 AND t.created_at >= p_since
    LOOP
        -- 0010 (C1): uncollected profit share released to the payable when the user's debt was paid; key
        -- ps_release:{user}:{seq of the triggering posting}. Held in full when that posting was a CARD top-up.
        IF r.kind = 'ps_pending_release' THEN
            IF split_part(r.k, ':', 3) ~ '^[0-9]{1,18}$' AND EXISTS (
                   SELECT 1 FROM ledger_transactions c
                     JOIN ledger_entries ce ON ce.tx_id = c.id AND ce.amount_micro < 0
                     JOIN ledger_accounts ca ON ca.id = ce.account_id
                    WHERE c.seq = CAST(split_part(r.k, ':', 3) AS bigint) AND c.idempotency_key LIKE 'stripe:%'
                      AND ca.code = 'user:' || split_part(r.k, ':', 2) || ':fee_balance') THEN
                held := held + r.credited;
            END IF;
            CONTINUE;
        END IF;
        SELECT * INTO part FROM fee_tx_card_part(r.tx_id);
        IF part.spend_micro > 0 AND part.card_micro > 0 THEN
            -- ceil: never release a micro too early
            held := held + ceil(r.credited::numeric * part.card_micro / part.spend_micro)::bigint;
        END IF;
    END LOOP;
    RETURN held;
END
$$;

-- ---------------------------------------------------------------- referrals (M2)
-- A real paid activity of the user: a paid subscription start/renewal, a plan, a post, or profit share on a strategy
-- that is not a free showcase (in-house, $0 and 0 %). Free showcase-only usage never counts.
CREATE FUNCTION user_has_paid_activity(p_user uuid) RETURNS boolean
LANGUAGE sql
STABLE
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
          FROM ledger_entries e
          JOIN ledger_accounts a ON a.id = e.account_id
          JOIN ledger_transactions t ON t.id = e.tx_id
         WHERE a.code = 'user:' || p_user::text || ':fee_balance'
           AND e.amount_micro > 0
           AND (t.kind IN ('subscription_start', 'subscription_renewal', 'plan_purchase', 'plan_renewal',
                           'post_purchase')
                OR (t.kind = 'profit_share' AND EXISTS (
                        SELECT 1 FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
                         WHERE s.id = CASE WHEN split_part(t.idempotency_key, ':', 2)
                                                ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                                           THEN CAST(split_part(t.idempotency_key, ':', 2) AS uuid) END
                           AND NOT (st.in_house AND coalesce(st.price_monthly_micro, 0) = 0
                                    AND coalesce(st.profit_share_bps, 0) = 0)))))
$$;

-- ---------------------------------------------------------------- grants (no DELETE for anyone)
GRANT SELECT, INSERT, UPDATE ON user_ip_nets TO app_api;
GRANT SELECT ON user_ip_nets TO app_executor;
GRANT SELECT, INSERT ON payout_tx_hashes TO app_api;
GRANT SELECT ON payout_tx_hashes TO app_executor;
GRANT ALL ON user_ip_nets, payout_tx_hashes TO app_migrator;
GRANT EXECUTE ON FUNCTION fee_funding_card_unspent(uuid, bigint), fee_tx_card_part(uuid),
                          payable_card_held(text, timestamptz), user_has_paid_activity(uuid)
    TO app_api, app_executor;
-- users / subscriptions / withdrawals / payouts / admin_changes: table-level grants (0002/0004) cover the new columns.
-- users: app_executor may also flag self-referrals from the daily referral-tiers job.
GRANT UPDATE (referral_flagged_at, referral_flag_reason) ON users TO app_executor;

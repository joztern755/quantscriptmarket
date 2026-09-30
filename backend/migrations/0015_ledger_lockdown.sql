-- =====================================================================================================
-- 0015_ledger_lockdown.sql — ledger posting lockdown (REVIEW_MONEY M5, remaining part) + the postings M7(b)/(c) need.
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
--
-- M5  The app roles can no longer write the ledger tables at all: INSERT on ledger_transactions / ledger_entries is
--     revoked from app_api and app_executor, and so is EXECUTE on the old SECURITY INVOKER ledger_post(). Every
--     posting goes through
--         ledger_post_as(p_role, key, kind, memo, created_by, entries)      SECURITY DEFINER (owner app_migrator)
--     p_role is the app role the caller posts AS. A SECURITY DEFINER function runs with current_user = its owner, so
--     the caller is identified by the SESSION: the role set with SET ROLE (current_setting('role')), else
--     session_user (the IAM login, a member of exactly one app role). p_role must be one of app_api / app_executor /
--     app_migrator and the invoker must be a member of it (AJ403 otherwise). The posting must then match a row of the
--     FIXED table ledger_posting_rules for (p_role, kind): the idempotency key matches key_pattern, EVERY debit
--     (amount > 0) account matches debit_pattern, EVERY credit (amount < 0) account matches credit_pattern, and the
--     number of debit / credit entries is within max_debits / max_credits. No matching rule → AJ403. The rule id,
--     the role and the invoking login are recorded in ledger_tx_authorizations, which the deferred balance check now
--     requires for EVERY new transaction (a row inserted around the functions — e.g. by the owner — is refused at
--     COMMIT). Idempotency (same key + kind + entries → the existing tx, created = false; different → AJ409), the
--     hash chain, the running balances, the C1 profit-share invariant (ledger_authorize_profit_share, now a pure
--     check) and the (kind, account) overdraft allowlist are unchanged.
--     The old 5-argument ledger_post() is reserved to owner context: the owner's own session posts as app_migrator
--     (migrations / ops break-glass); an app session that reaches it through a SECURITY DEFINER function
--     (ps_pending_release, whatever migration last defined it) posts as the pseudo-role 'system', whose only rule is
--     ps_pending_release. ledger_post_core is not executable by the app roles. The Stripe reversal wrapper is kept for
--     compatibility and now goes through the rules as well.
--     Rule table (role, kind: debit accounts → credit accounts [key]); U = uuid, FEE = user:U:fee_balance:
--       app_api       subscription_start, subscription_renewal   FEE → creator:U:payable | platform:revenue:subscription
--                     plan_purchase                              FEE → platform:revenue:plans
--                     post_purchase                              FEE → creator:U:payable | platform:revenue:posts
--                     withdrawal_hold / _release / _sent         FEE → withdrawals:pending → FEE | treasury:hl_usdc
--                     payout_hold / _release / _sent             (creator|referrer):U:payable → payouts:pending → payable | treasury
--                     deposit                                    stripe:clearing → FEE [stripe:…];  treasury:hl_usdc → FEE
--                     stripe_refund / stripe_dispute             FEE → stripe:clearing [stripe:refund:… / stripe:dispute:…]
--                     stripe_dispute_reinstated                  stripe:clearing → FEE [stripe:dispute_reinstated:…]
--                     suspense_release / suspense_refund         suspense:usdc_unattributed → FEE | refunds:usdc_pending
--                     suspense_refund_sent                       refunds:usdc_pending → treasury:hl_usdc
--       app_executor  profit_share                               FEE → creator:U:payable | platform:revenue:profit_share
--                                                                      | ps_pending:U:(U|platform)   (+ C1 check)
--                     subscription_renewal / plan_renewal        FEE → creator payable | subscription / plans revenue
--                     builder_fee                                builder:hl_receivable → platform:revenue:builder
--                                                                      | creator:U:payable | referrer:U:payable (referral reward)
--                     deposit / deposit_held (deposits-scan)     treasury:hl_usdc → FEE | suspense:usdc_unattributed
--                     builder_rewards_claim (M7(b))              treasury:hl_usdc → builder:hl_receivable [builder_claim:…]
--                     stripe_payout (M7(c))                      bank:payouts (+ expense:stripe_fees) → stripe:clearing [stripe:payout:txn_…]
--                     stripe_payout_reversal (M7(c))             stripe:clearing → bank:payouts [stripe:payout_reversal:txn_…]
--       system        ps_pending_release                         ps_pending:U:(U|platform) → creator:U:payable
--                                                                      | platform:revenue:profit_share [ps_release:U:seq]
--       app_migrator  * (owner: migrations and reviewed break-glass corrections, RUNBOOK §ledger corrections; it owns
--                     the tables and could bypass anything anyway — every such posting is still recorded with its login)
--     The rules table is append-only for everyone (changing a rule is a new migration).
-- M7  Seeds bank:payouts (asset: Stripe balance paid out to the bank) and expense:stripe_fees (payout fees) for the
--     Stripe payout postings (reconcile books them from Stripe balance transactions, app.execution.treasury_books).
-- =====================================================================================================

-- ---------------------------------------------------------------- accounts for the M7 postings
INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative) VALUES
    ('bank:payouts',        'asset',   NULL, false),
    ('expense:stripe_fees', 'expense', NULL, false)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------- the rule table
CREATE TABLE ledger_posting_rules (                           -- append-only, fixed by migrations
    id              integer PRIMARY KEY,
    role            text NOT NULL CHECK (role IN ('app_api', 'app_executor', 'app_migrator', 'system')),
    kind            text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.]{0,63}$' OR (kind = '*' AND role = 'app_migrator')),
    key_pattern     text NOT NULL CHECK (key_pattern ~ '^\^.*\$$'),       -- anchored regexes only
    debit_pattern   text NOT NULL CHECK (debit_pattern ~ '^\^.*\$$'),
    credit_pattern  text NOT NULL CHECK (credit_pattern ~ '^\^.*\$$'),
    max_debits      integer CHECK (max_debits IS NULL OR max_debits >= 1),
    max_credits     integer CHECK (max_credits IS NULL OR max_credits >= 1),
    note            text NOT NULL
);
CREATE INDEX ledger_posting_rules_role_kind_idx ON ledger_posting_rules (role, kind);

-- {U} = lower-case uuid, {FEE} = a user fee balance, {PAY} = a creator payable; expanded once here.
INSERT INTO ledger_posting_rules (id, role, kind, key_pattern, debit_pattern, credit_pattern, max_debits, max_credits, note)
SELECT r.id, r.role, r.kind,
       replace(replace(replace(r.k, '{FEE}', 'user:{U}:fee_balance'), '{PAY}', 'creator:{U}:payable'), '{U}',
               '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'),
       replace(replace(replace(r.d, '{FEE}', 'user:{U}:fee_balance'), '{PAY}', 'creator:{U}:payable'), '{U}',
               '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'),
       replace(replace(replace(r.c, '{FEE}', 'user:{U}:fee_balance'), '{PAY}', 'creator:{U}:payable'), '{U}',
               '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'),
       r.md, r.mc, r.note
  FROM (VALUES
    -- ---- app_api (public API + Stripe webhook + admin maker-checker actions)
    (101, 'app_api', 'subscription_start', '^.{1,200}$', '^{FEE}$', '^({PAY}|platform:revenue:subscription)$', 1, 2,
          'first period at subscribe (ledger_ops.charge_subscription_start)'),
    (102, 'app_api', 'subscription_renewal', '^.{1,200}$', '^{FEE}$', '^({PAY}|platform:revenue:subscription)$', 1, 2,
          'renewal due while paused, charged on unpause (billing_ops.charge_subscription_renewal)'),
    (103, 'app_api', 'plan_purchase', '^.{1,200}$', '^{FEE}$', '^platform:revenue:plans$', 1, 1,
          'first month of a paid plan (ledger_ops.charge_plan)'),
    (104, 'app_api', 'post_purchase', '^.{1,200}$', '^{FEE}$', '^({PAY}|platform:revenue:posts)$', 1, 2,
          'paid post (ledger_ops.charge_post)'),
    (105, 'app_api', 'withdrawal_hold', '^.{1,200}$', '^{FEE}$', '^withdrawals:pending$', 1, 1,
          'fee-balance withdrawal requested'),
    (106, 'app_api', 'withdrawal_release', '^.{1,200}$', '^withdrawals:pending$', '^{FEE}$', 1, 1,
          'withdrawal rejected (admin)'),
    (107, 'app_api', 'withdrawal_sent', '^.{1,200}$', '^withdrawals:pending$', '^treasury:hl_usdc$', 1, 1,
          'withdrawal usdSend verified on-chain (admin)'),
    (108, 'app_api', 'payout_hold', '^.{1,200}$', '^(creator|referrer):{U}:payable$', '^payouts:pending$', 1, 1,
          'creator / referrer payout requested'),
    (109, 'app_api', 'payout_release', '^.{1,200}$', '^payouts:pending$', '^(creator|referrer):{U}:payable$', 1, 1,
          'payout rejected (admin)'),
    (110, 'app_api', 'payout_sent', '^.{1,200}$', '^payouts:pending$', '^treasury:hl_usdc$', 1, 1,
          'payout usdSend verified on-chain (admin)'),
    (111, 'app_api', 'deposit', '^stripe:[A-Za-z0-9_]{1,180}$', '^stripe:clearing$', '^{FEE}$', 1, 1,
          'Stripe top-up (webhook payment_intent.succeeded)'),
    (112, 'app_api', 'deposit', '^.{1,200}$', '^treasury:hl_usdc$', '^{FEE}$', 1, 1,
          'USDC top-up confirmed on-chain (POST /deposits/usdc/confirm)'),
    (113, 'app_api', 'stripe_refund', '^stripe:refund:[A-Za-z0-9_:.-]{1,180}$', '^{FEE}$', '^stripe:clearing$', 1, 1,
          'Stripe refund (may overdraw the fee balance)'),
    (114, 'app_api', 'stripe_dispute', '^stripe:dispute:[A-Za-z0-9_:.-]{1,180}$', '^{FEE}$', '^stripe:clearing$', 1, 1,
          'Stripe dispute (may overdraw the fee balance)'),
    (115, 'app_api', 'stripe_dispute_reinstated', '^stripe:dispute_reinstated:[A-Za-z0-9_:.-]{1,180}$',
          '^stripe:clearing$', '^{FEE}$', 1, 1, 'dispute won / warning closed: balance reinstated'),
    (116, 'app_api', 'suspense_release', '^suspense_release:.{1,180}$', '^suspense:usdc_unattributed$', '^{FEE}$', 1, 1,
          'held USDC attributed to a user (maker-checker)'),
    (117, 'app_api', 'suspense_refund', '^suspense_release:.{1,180}$', '^suspense:usdc_unattributed$',
          '^refunds:usdc_pending$', 1, 1, 'held USDC approved for refund (maker-checker)'),
    (118, 'app_api', 'suspense_refund_sent', '^suspense_refund:.{1,180}:sent$', '^refunds:usdc_pending$',
          '^treasury:hl_usdc$', 1, 1, 'held-USDC refund usdSend verified on-chain'),
    -- ---- app_executor (settlement, deposits-scan, reconcile)
    (201, 'app_executor', 'profit_share', '^.{1,200}$', '^{FEE}$',
          '^({PAY}|platform:revenue:profit_share|ps_pending:{U}:({U}|platform))$', 1, NULL,
          'daily profit share; collected part to payable/revenue, rest pending (C1 checked by ledger_authorize_profit_share)'),
    (202, 'app_executor', 'subscription_renewal', '^.{1,200}$', '^{FEE}$', '^({PAY}|platform:revenue:subscription)$', 1, 2,
          'subscription renewal (settlement)'),
    (203, 'app_executor', 'plan_renewal', '^.{1,200}$', '^{FEE}$', '^platform:revenue:plans$', 1, 1,
          'plan renewal (settlement)'),
    (204, 'app_executor', 'builder_fee', '^.{1,200}$', '^builder:hl_receivable$',
          '^(platform:revenue:builder|{PAY}|referrer:{U}:payable)$', 1, 3,
          'builder-fee recognition incl. creator share and referral reward (settlement)'),
    (205, 'app_executor', 'deposit', '^.{1,200}$', '^treasury:hl_usdc$', '^{FEE}$', 1, 1,
          'USDC top-up found by deposits-scan'),
    (206, 'app_executor', 'deposit_held', '^.{1,200}$', '^treasury:hl_usdc$', '^suspense:usdc_unattributed$', 1, 1,
          'unattributable USDC held for review (deposits-scan)'),
    (207, 'app_executor', 'builder_rewards_claim', '^builder_claim:[A-Za-z0-9_:.-]{1,180}$', '^treasury:hl_usdc$',
          '^builder:hl_receivable$', 1, 1, 'builder rewards claimed into the treasury (reconcile, M7(b))'),
    (208, 'app_executor', 'stripe_payout', '^stripe:payout:txn_[A-Za-z0-9]{1,180}$',
          '^(bank:payouts|expense:stripe_fees)$', '^stripe:clearing$', 2, 1,
          'Stripe balance paid out to the bank (reconcile, M7(c))'),
    (209, 'app_executor', 'stripe_payout_reversal', '^stripe:payout_reversal:txn_[A-Za-z0-9]{1,180}$',
          '^stripe:clearing$', '^bank:payouts$', 1, 1, 'Stripe payout failed / cancelled: back in the Stripe balance'),
    -- ---- system (owner-context SECURITY DEFINER code only)
    (301, 'system', 'ps_pending_release', '^ps_release:{U}:[0-9]{1,19}$', '^ps_pending:{U}:({U}|platform)$',
          '^({PAY}|platform:revenue:profit_share)$', NULL, NULL,
          'uncollected profit share released after a top-up (ps_pending_release)'),
    -- ---- owner
    (901, 'app_migrator', '*', '^.{1,200}$', '^.+$', '^.+$', NULL, NULL,
          'owner: migrations and reviewed break-glass corrections (the owner can bypass anything; still recorded)')
  ) AS r(id, role, kind, k, d, c, md, mc, note);

CREATE TRIGGER ledger_posting_rules_append_only BEFORE UPDATE OR DELETE ON ledger_posting_rules
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_posting_rules_no_truncate BEFORE TRUNCATE ON ledger_posting_rules
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- who is calling
-- The invoking role of this SESSION, unaffected by SECURITY DEFINER (which only changes current_user): the role set
-- with SET ROLE, else the login (session_user).
CREATE FUNCTION ledger_invoker() RETURNS text
LANGUAGE sql STABLE
AS $$
    SELECT CASE WHEN coalesce(current_setting('role', true), 'none') IN ('none', '') THEN session_user::text
                ELSE current_setting('role', true) END
$$;

-- The app role the invoker posts as when the caller does not say (app.db.repositories.ledger passes NULL → this):
-- the SET ROLE / login itself when it is an app role; app_migrator for its members (and superusers); else the ONE
-- runtime role the login belongs to. Ambiguous → AJ403 (pass p_role explicitly).
CREATE FUNCTION ledger_invoker_app_role() RETURNS text
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    v   text := ledger_invoker();
    api boolean;
    exe boolean;
BEGIN
    IF v IN ('app_api', 'app_executor', 'app_migrator') THEN
        RETURN v;
    END IF;
    IF pg_has_role(v, 'app_migrator', 'MEMBER') THEN
        RETURN 'app_migrator';
    END IF;
    api := pg_has_role(v, 'app_api', 'MEMBER');
    exe := pg_has_role(v, 'app_executor', 'MEMBER');
    IF api AND NOT exe THEN
        RETURN 'app_api';
    ELSIF exe AND NOT api THEN
        RETURN 'app_executor';
    END IF;
    RAISE EXCEPTION 'cannot determine the ledger posting role of %', v USING ERRCODE = 'AJ403';
END
$$;

-- The first rule of (p_role, p_kind) the posting satisfies (specific kinds before the owner wildcard), else NULL.
CREATE FUNCTION ledger_match_rule(p_role text, p_kind text, p_key text, p_codes text[], p_amounts bigint[])
RETURNS integer
LANGUAGE sql STABLE
AS $$
    SELECT r.id
      FROM ledger_posting_rules r
     WHERE r.role = p_role AND (r.kind = p_kind OR r.kind = '*')
       AND p_key ~ r.key_pattern
       AND NOT EXISTS (SELECT 1 FROM unnest(p_codes, p_amounts) AS x(c, a)
                        WHERE (x.a > 0 AND x.c !~ r.debit_pattern) OR (x.a < 0 AND x.c !~ r.credit_pattern))
       AND (r.max_debits IS NULL OR (SELECT count(*) FROM unnest(p_amounts) AS y(a) WHERE y.a > 0) <= r.max_debits)
       AND (r.max_credits IS NULL OR (SELECT count(*) FROM unnest(p_amounts) AS z(a) WHERE z.a < 0) <= r.max_credits)
     ORDER BY (r.kind = '*'), r.id
     LIMIT 1
$$;

-- ---------------------------------------------------------------- authorisations: one per transaction
ALTER TABLE ledger_tx_authorizations ADD COLUMN invoker text;                  -- login / SET ROLE that posted
ALTER TABLE ledger_tx_authorizations ADD COLUMN rule_id integer REFERENCES ledger_posting_rules(id);

CREATE OR REPLACE FUNCTION ledger_tx_authorizations_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_kind text;
BEGIN
    SELECT t.kind INTO v_kind FROM ledger_transactions t WHERE t.id = NEW.tx_id;
    IF v_kind IS NULL OR v_kind <> NEW.kind THEN
        RAISE EXCEPTION 'authorisation does not match a ledger transaction' USING ERRCODE = 'AJ403';
    END IF;
    -- only owner-context code (ledger_post_core inside the SECURITY DEFINER entry points) writes authorisations
    IF NOT pg_has_role(current_user, 'app_migrator', 'MEMBER') THEN
        RAISE EXCEPTION 'role % may not authorise ledger transactions', current_user USING ERRCODE = 'AJ403';
    END IF;
    IF NEW.authorized_as NOT IN ('app_api', 'app_executor', 'app_migrator', 'system') THEN
        RAISE EXCEPTION 'unknown posting role %', NEW.authorized_as USING ERRCODE = 'AJ403';
    END IF;
    RETURN NEW;
END
$$;

-- C1 check of ONE profit_share transaction (0010's ledger_authorize_profit_share without the INSERT: the
-- authorisation row is now written by ledger_post_core for every transaction).
CREATE OR REPLACE FUNCTION ledger_authorize_profit_share(p_tx_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    v_kind    text;
    v_debits  int;
    v_fee     text;
    v_debit   bigint;
    v_user    text;
    v_paid    bigint;
    v_bad     int;
    v_before  bigint;
BEGIN
    SELECT t.kind INTO v_kind FROM ledger_transactions t WHERE t.id = p_tx_id;
    IF v_kind IS DISTINCT FROM 'profit_share' THEN
        RAISE EXCEPTION 'not a profit_share transaction' USING ERRCODE = 'AJ403';
    END IF;
    SELECT count(*), min(a.code), sum(e.amount_micro)::bigint INTO v_debits, v_fee, v_debit
      FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
     WHERE e.tx_id = p_tx_id AND e.amount_micro > 0;
    IF v_debits <> 1 OR v_fee !~ '^user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:fee_balance$' THEN
        RAISE EXCEPTION 'profit_share must debit exactly one user fee balance' USING ERRCODE = 'AJ403';
    END IF;
    v_user := split_part(v_fee, ':', 2);
    SELECT count(*) FILTER (WHERE NOT (
               a.code ~ '^creator:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:payable$'
               OR a.code = 'platform:revenue:profit_share'
               OR a.code IN ('ps_pending:' || v_user || ':platform')
               OR a.code ~ ('^ps_pending:' || v_user || ':[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))),
           coalesce(sum(-e.amount_micro) FILTER (WHERE a.code !~ '^ps_pending:'), 0)::bigint
      INTO v_bad, v_paid
      FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
     WHERE e.tx_id = p_tx_id AND e.amount_micro < 0;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'profit_share may only credit creator payables, platform profit-share revenue and the user''s ps_pending accounts'
            USING ERRCODE = 'AJ403';
    END IF;
    v_before := v_debit - ledger_raw_balance(v_fee);
    IF v_paid > greatest(0, v_before) THEN
        RAISE EXCEPTION 'profit_share credits % to payables/revenue but only % was collected from %', v_paid,
            greatest(0, v_before), v_fee USING ERRCODE = 'AJ402';
    END IF;
END
$$;

-- Integrity of ONE ledger transaction (replaces 0010's version): as before, plus EVERY transaction must carry the
-- authorisation ledger_post_core writes (checked last, so the specific integrity errors keep their SQLSTATE).
CREATE OR REPLACE FUNCTION ledger_check_tx(p_tx_id uuid) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_count   bigint;
    v_sum     numeric;
    v_tx      ledger_transactions%ROWTYPE;
    v_bad     record;
BEGIN
    SELECT count(*), coalesce(sum(amount_micro), 0) INTO v_count, v_sum FROM ledger_entries WHERE tx_id = p_tx_id;
    IF v_count < 2 THEN
        RAISE EXCEPTION 'ledger transaction % has % entries (need >= 2)', p_tx_id, v_count USING ERRCODE = 'AJ422';
    END IF;
    IF v_sum <> 0 THEN
        RAISE EXCEPTION 'ledger transaction % is unbalanced: sum = %', p_tx_id, v_sum USING ERRCODE = 'AJ422';
    END IF;
    SELECT * INTO v_tx FROM ledger_transactions WHERE id = p_tx_id;
    IF v_tx.entries_digest IS DISTINCT FROM ledger_tx_stored_digest(p_tx_id) THEN
        RAISE EXCEPTION 'ledger transaction % entries do not match entries_digest', p_tx_id USING ERRCODE = 'AJ422';
    END IF;
    -- C1: pending (uncollected) profit share can only move by a release
    IF v_tx.kind <> 'ps_pending_release' AND EXISTS (
           SELECT 1 FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
            WHERE e.tx_id = p_tx_id AND a.code ~ '^ps_pending:' AND e.amount_micro > 0) THEN
        RAISE EXCEPTION 'ps_pending accounts may only be debited by ps_pending_release' USING ERRCODE = 'AJ403';
    END IF;
    -- C1: a payout hold moves a creator/referrer payable (never pending) to payouts:pending, nothing else
    IF v_tx.kind = 'payout_hold' AND EXISTS (
           SELECT 1 FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
            WHERE e.tx_id = p_tx_id
              AND NOT ((e.amount_micro > 0 AND a.code ~ '^(creator|referrer):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:payable$')
                       OR (e.amount_micro < 0 AND a.code = 'payouts:pending'))) THEN
        RAISE EXCEPTION 'payout_hold must move a creator/referrer payable to payouts:pending' USING ERRCODE = 'AJ403';
    END IF;
    -- non-negative accounts (running balance, O(1)); overdraft only for the fixed (kind, account) allowlist
    SELECT a.code, d.delta, s.bal INTO v_bad
      FROM (SELECT account_id, sum(amount_micro) AS raw_delta FROM ledger_entries WHERE tx_id = p_tx_id GROUP BY account_id) t
      JOIN ledger_accounts a ON a.id = t.account_id AND a.non_negative
      LEFT JOIN ledger_account_balances b ON b.account_id = a.id
      CROSS JOIN LATERAL (SELECT CASE WHEN a.kind IN ('asset', 'expense') THEN 1 ELSE -1 END AS sgn) g
      CROSS JOIN LATERAL (SELECT g.sgn * t.raw_delta AS delta) d
      CROSS JOIN LATERAL (SELECT g.sgn * coalesce(b.balance_micro, 0) AS bal) s
     WHERE d.delta < 0 AND s.bal < 0 AND NOT ledger_overdraft_allowed(v_tx.kind, a.code)
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'insufficient balance: account % would be % after this transaction (change %)',
            v_bad.code, v_bad.bal, v_bad.delta USING ERRCODE = 'AJ402';
    END IF;
    -- M5 (0015): posted through ledger_post_core (rule-checked), never around it
    IF NOT EXISTS (SELECT 1 FROM ledger_tx_authorizations z WHERE z.tx_id = p_tx_id) THEN
        RAISE EXCEPTION 'ledger transaction % (kind %) was not posted through ledger_post_as', p_tx_id, v_tx.kind
            USING ERRCODE = 'AJ403';
    END IF;
END
$$;

-- ---------------------------------------------------------------- the posting core (owner context only)
-- 0010's ledger_post body, with the rule check and the authorisation row. NOT executable by the app roles: they
-- reach it through ledger_post_as (SECURITY DEFINER) only.
CREATE FUNCTION ledger_post_core(p_role text, p_idempotency_key text, p_kind text, p_memo text, p_created_by text,
                                 p_entries jsonb)
RETURNS TABLE (tx_id uuid, created boolean)
LANGUAGE plpgsql
AS $$
DECLARE
    v_codes    text[];
    v_amounts  bigint[];
    v_digest   text;
    v_existing ledger_transactions%ROWTYPE;
    v_missing  text;
    v_id       uuid;
    v_attempt  int;
    v_rule     integer;
BEGIN
    IF p_role IS NULL OR p_role NOT IN ('app_api', 'app_executor', 'app_migrator', 'system') THEN
        RAISE EXCEPTION 'unknown ledger posting role %', p_role USING ERRCODE = 'AJ403';
    END IF;
    IF p_idempotency_key IS NULL OR p_kind IS NULL THEN
        RAISE EXCEPTION 'idempotency key and kind are required' USING ERRCODE = 'AJ422';
    END IF;
    IF p_entries IS NULL OR jsonb_typeof(p_entries) <> 'array' THEN
        RAISE EXCEPTION 'entries must be a JSON array' USING ERRCODE = 'AJ422';
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_entries) e
               WHERE jsonb_typeof(e) <> 'object'
                  OR jsonb_typeof(e->'account') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(e->'amount_micro') IS DISTINCT FROM 'number'
                  OR (e->>'amount_micro') !~ '^-?[0-9]{1,18}$') THEN
        RAISE EXCEPTION 'each entry must be {"account": text, "amount_micro": integer}' USING ERRCODE = 'AJ422';
    END IF;
    SELECT array_agg(e->>'account' ORDER BY o), array_agg((e->>'amount_micro')::bigint ORDER BY o)
      INTO v_codes, v_amounts
      FROM jsonb_array_elements(p_entries) WITH ORDINALITY AS t(e, o);
    IF coalesce(cardinality(v_codes), 0) < 2 THEN
        RAISE EXCEPTION 'a ledger transaction needs >= 2 entries' USING ERRCODE = 'AJ422';
    END IF;
    IF 0 = ANY (v_amounts) THEN
        RAISE EXCEPTION 'zero-amount ledger entry' USING ERRCODE = 'AJ422';
    END IF;
    IF (SELECT sum(x) FROM unnest(v_amounts) x) <> 0 THEN
        RAISE EXCEPTION 'ledger transaction is unbalanced' USING ERRCODE = 'AJ422';
    END IF;

    -- M5: the posting must match a fixed rule of the posting role
    v_rule := ledger_match_rule(p_role, p_kind, p_idempotency_key, v_codes, v_amounts);
    IF v_rule IS NULL THEN
        IF NOT EXISTS (SELECT 1 FROM ledger_posting_rules r WHERE r.role = p_role AND (r.kind = p_kind OR r.kind = '*')) THEN
            RAISE EXCEPTION 'role % may not post ledger kind %', p_role, p_kind USING ERRCODE = 'AJ403';
        END IF;
        RAISE EXCEPTION 'ledger % posting by % does not match any posting rule (key or accounts)', p_kind, p_role
            USING ERRCODE = 'AJ403';
    END IF;
    v_digest := ledger_entries_digest(v_codes, v_amounts);

    FOR v_attempt IN 1..2 LOOP
        IF v_attempt = 2 THEN
            PERFORM pg_advisory_xact_lock(hashtext('aijalon.ledger'));
        END IF;
        SELECT * INTO v_existing FROM ledger_transactions WHERE idempotency_key = p_idempotency_key;
        IF FOUND THEN
            IF v_existing.kind = p_kind AND v_existing.entries_digest = v_digest THEN
                RETURN QUERY SELECT v_existing.id, false;
                RETURN;
            END IF;
            RAISE EXCEPTION 'idempotency key % already used with different content', p_idempotency_key
                USING ERRCODE = 'AJ409';
        END IF;
    END LOOP;

    SELECT string_agg(DISTINCT c, ', ') INTO v_missing
      FROM unnest(v_codes) c WHERE NOT EXISTS (SELECT 1 FROM ledger_accounts a WHERE a.code = c);
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'unknown ledger account(s): %', v_missing USING ERRCODE = 'AJ404';
    END IF;

    INSERT INTO ledger_transactions (idempotency_key, kind, memo, created_by, entries_digest)
    VALUES (p_idempotency_key, p_kind, p_memo, p_created_by, v_digest)
    RETURNING id INTO v_id;

    INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
    SELECT v_id, a.id, x.amt
      FROM unnest(v_codes, v_amounts) WITH ORDINALITY AS x(code, amt, o)
      JOIN ledger_accounts a ON a.code = x.code
     ORDER BY x.o;

    IF p_kind = 'profit_share' THEN
        PERFORM ledger_authorize_profit_share(v_id);          -- C1 shape + collection invariant, whoever posts
    END IF;
    INSERT INTO ledger_tx_authorizations (tx_id, kind, authorized_as, invoker, rule_id)
    VALUES (v_id, p_kind, p_role, left(ledger_invoker(), 200), v_rule);

    PERFORM ledger_check_tx(v_id);
    RETURN QUERY SELECT v_id, true;
END
$$;

-- ---------------------------------------------------------------- the ONE entry point for the app roles
CREATE FUNCTION ledger_post_as(p_role text, p_idempotency_key text, p_kind text, p_memo text, p_created_by text,
                               p_entries jsonb)
RETURNS TABLE (tx_id uuid, created boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    v_invoker text := ledger_invoker();
BEGIN
    IF p_role IS NULL OR p_role NOT IN ('app_api', 'app_executor', 'app_migrator') THEN
        RAISE EXCEPTION 'unknown ledger posting role %', p_role USING ERRCODE = 'AJ403';
    END IF;
    IF NOT pg_has_role(v_invoker, p_role, 'MEMBER') THEN
        RAISE EXCEPTION '% may not post to the ledger as %', v_invoker, p_role USING ERRCODE = 'AJ403';
    END IF;
    RETURN QUERY SELECT l.tx_id, l.created
                   FROM ledger_post_core(p_role, p_idempotency_key, p_kind, p_memo, p_created_by, p_entries) l;
END
$$;

-- Old entry point (same signature; SECURITY INVOKER), reserved to owner context (current_user a member of
-- app_migrator). It posts AS
--   app_migrator  when the SESSION itself is the owner / a superuser (migrations, break-glass: rule 901), or
--   system        when an app session reached it through owner-context code, i.e. a SECURITY DEFINER function such
--                 as ps_pending_release (0010 / 0014, run by the top-up trigger and the settlement sweep): only the
--                 'system' rules apply there, so a definer function cannot post anything else for an app role.
CREATE OR REPLACE FUNCTION ledger_post(p_idempotency_key text, p_kind text, p_memo text, p_created_by text, p_entries jsonb)
RETURNS TABLE (tx_id uuid, created boolean)
LANGUAGE plpgsql
AS $$
DECLARE
    v_role text;
BEGIN
    IF NOT pg_has_role(current_user, 'app_migrator', 'MEMBER') THEN
        RAISE EXCEPTION 'ledger_post is reserved to the owner; app roles post through ledger_post_as'
            USING ERRCODE = 'AJ403';
    END IF;
    v_role := CASE WHEN pg_has_role(ledger_invoker(), 'app_migrator', 'MEMBER') THEN 'app_migrator' ELSE 'system' END;
    RETURN QUERY SELECT l.tx_id, l.created
                   FROM ledger_post_core(v_role, p_idempotency_key, p_kind, p_memo, p_created_by, p_entries) l;
END
$$;

-- Stripe refund / dispute wrapper (0010), kept for compatibility: same fixed checks, then the rules of the
-- invoker's role (only app_api has rules for these kinds; the owner's wildcard for break-glass).
CREATE OR REPLACE FUNCTION ledger_post_payment_reversal(p_idempotency_key text, p_kind text, p_memo text,
                                                        p_created_by text, p_entries jsonb)
RETURNS TABLE (tx_id uuid, created boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    v_n      int;
    v_ok     int;
BEGIN
    IF p_kind NOT IN ('stripe_refund', 'stripe_dispute') THEN
        RAISE EXCEPTION 'not a payment reversal kind: %', p_kind USING ERRCODE = 'AJ403';
    END IF;
    IF p_idempotency_key !~ ('^stripe:' || CASE p_kind WHEN 'stripe_refund' THEN 'refund' ELSE 'dispute' END || ':[A-Za-z0-9_:.-]{1,180}$') THEN
        RAISE EXCEPTION 'payment reversal key % does not match its kind', p_idempotency_key USING ERRCODE = 'AJ403';
    END IF;
    IF p_entries IS NULL OR jsonb_typeof(p_entries) <> 'array' THEN
        RAISE EXCEPTION 'entries must be a JSON array' USING ERRCODE = 'AJ422';
    END IF;
    SELECT count(*),
           count(*) FILTER (WHERE (e->>'account') ~ '^user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:fee_balance$'
                                   AND (e->>'amount_micro') ~ '^[0-9]{1,18}$' AND (e->>'amount_micro')::bigint > 0)
         + count(*) FILTER (WHERE (e->>'account') = 'stripe:clearing' AND (e->>'amount_micro') ~ '^-[0-9]{1,18}$')
      INTO v_n, v_ok
      FROM jsonb_array_elements(p_entries) e;
    IF v_n <> 2 OR v_ok <> 2 THEN
        RAISE EXCEPTION 'payment reversal must be fee_balance +amount / stripe:clearing -amount' USING ERRCODE = 'AJ403';
    END IF;
    RETURN QUERY SELECT l.tx_id, l.created
                   FROM ledger_post_core(ledger_invoker_app_role(), p_idempotency_key, p_kind, p_memo, p_created_by,
                                         p_entries) l;
END
$$;

-- ---------------------------------------------------------------- grants
REVOKE INSERT ON ledger_transactions, ledger_entries FROM app_api, app_executor;
REVOKE EXECUTE ON FUNCTION ledger_post(text, text, text, text, jsonb) FROM PUBLIC, app_api, app_executor;
REVOKE EXECUTE ON FUNCTION ledger_post_core(text, text, text, text, text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ledger_post_as(text, text, text, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger_post_as(text, text, text, text, text, jsonb) TO app_api, app_executor, app_migrator;
REVOKE EXECUTE ON FUNCTION ledger_authorize_profit_share(uuid) FROM PUBLIC, app_executor;
GRANT SELECT ON ledger_posting_rules TO app_api, app_executor;
GRANT ALL ON ledger_posting_rules TO app_migrator;

-- =====================================================================================================
-- 0010_money_fixes.sql — money-core fixes from docs/security/REVIEW_MONEY.md (C1, H1, H2, M3, M5, M7, L4, L5).
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
--
-- C1  uncollected profit share is PENDING, never payable:
--       ps_pending:{user}:{creator}   liability, non-negative, owner = creator — creator share not yet collected
--       ps_pending:{user}:platform    liability, non-negative, owner NULL    — platform share not yet collected
--     Settlement credits creator:{id}:payable / platform:revenue:profit_share only with what the user's
--     non-negative fee balance covered; the rest goes to the pending accounts (the fee balance still takes the full
--     debit, so the user's debt stays visible and blocks spending/withdrawals). ps_pending_release(user) moves pending
--     → payable / revenue pro-rata once the user's debt shrinks below the pending total: pending_after =
--     min(pending, debt). It runs automatically (statement trigger on ledger_entries) whenever a posting CREDITS a
--     user fee balance (every top-up path: Stripe, USDC confirm, deposit scanner, suspense release, reinstated
--     disputes), and daily from settlement as a sweep. Pending accounts can only be debited by kind
--     ps_pending_release (posted only through that SECURITY DEFINER function), so they can never feed a payout.
--     A payout_hold must be exactly (creator|referrer):{uuid}:payable → payouts:pending.
-- M5  overdraft is decided by the DB from a FIXED allowlist of (kind, account pattern): only
--     user:{uuid}:fee_balance may be overdrawn, and only by profit_share | stripe_refund | stripe_dispute. Those kinds
--     (and ps_pending_release) are GATED: ledger_post refuses them unless the caller role may post them
--       profit_share        → member of app_executor (settlement)            [or app_migrator]
--       stripe_refund/…     → only through ledger_post_payment_reversal()   (SECURITY DEFINER; runs as app_migrator,
--                             checks key prefix + exact 2-entry shape fee_balance → stripe:clearing)
--       ps_pending_release  → only through ps_pending_release()             (SECURITY DEFINER)
--     and records the authorisation in ledger_tx_authorizations (app_api cannot insert there). The deferred balance
--     check refuses a gated kind without that row, so a direct INSERT into ledger_transactions/ledger_entries by an
--     app role cannot use them either. Protected accounts are forced non-negative by trigger + CHECK whoever creates
--     them: user fee balances, creator/referrer payables, ps_pending:*, withdrawals:pending, payouts:pending,
--     refunds:usdc_pending, suspense:* (existing rows are flipped here). Per-user shapes must carry the right kind
--     and owner (owner taken from the code when omitted, AJ422 when it differs).
-- L5  ledger_account_balances: running balance per account, maintained by an AFTER INSERT row trigger on
--     ledger_entries (SECURITY DEFINER, so app roles need no privilege on it) under the ledger chain lock; the
--     balance check is O(1). verify_chain() also compares it with the full Σ of entries.
-- L4  ledger_accounts is hash-chained too (seq, prev_hash, hash over id, code, kind, owner_user_id, non_negative,
--     created_at); verify_chain() checks it and chain_heads publishes its head. Existing rows are chained in
--     (created_at, id) order here.
-- M7  ledger_chain_anchors: daily chain heads stored by /internal/verify-chain (and sent to ops); verify_chain_anchors()
--     reports an anchored (chain, seq) whose row is gone or whose hash changed (truncation / rewrite after anchoring).
-- H1  per-subscription position book from OUR fills only (subscription_positions: qty, average entry) and the PnL we
--     compute from it (fills.book_pnl_micro = realised against our average entry − fee). Adjustments that are not
--     our fills live in subscription_pnl_events: foreign (manual) fills on a strategy coin mark the book to market at
--     the fill price and take the closed quantity out of the book; pause and cancel-"leave" mark it to market at the
--     mark price. Settlement charges profit share on them like on fills (HWM rules unchanged).
-- M3  late data is never lost: fills.ps_settlement_date / funding_events.ps_settlement_date / subscription_pnl_events
--     .ps_settlement_date record which settlement consumed a row. Settlement claims every UNCLAIMED row up to its
--     cut-off (a fill ingested after its day was settled is booked into the next settlement). Rows already covered
--     by a subscription's pnl_cursor are marked as settled here.
-- H2  fills.oid_verified: the fill's oid equals the oid we recorded for that cloid (or the order had no oid yet and
--     the fill set it). Builder-fee revenue is recognised only for such fills (see app.execution.pg).
-- =====================================================================================================

-- ---------------------------------------------------------------- protected account shapes (M5)
CREATE FUNCTION ledger_account_forced_non_negative(p_code text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT p_code ~ '^user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:fee_balance$'
        OR p_code ~ '^(creator|referrer):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:payable$'
        OR p_code ~ '^ps_pending:'
        OR p_code IN ('withdrawals:pending', 'payouts:pending', 'refunds:usdc_pending')
        OR p_code ~ '^suspense:'
$$;

-- (kind, owner) a per-user account code implies; NULL row fields = no constraint. owner_from_code: the uuid in the
-- code that must be the owner (NULL = owner must be NULL for ps_pending platform accounts; not applicable otherwise).
CREATE FUNCTION ledger_account_expected(p_code text, OUT kind text, OUT owner uuid, OUT owner_null boolean)
LANGUAGE plpgsql IMMUTABLE
AS $$
DECLARE
    m text[];
BEGIN
    kind := NULL; owner := NULL; owner_null := false;
    m := regexp_match(p_code, '^user:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):fee_balance$');
    IF m IS NOT NULL THEN kind := 'liability'; owner := m[1]::uuid; RETURN; END IF;
    m := regexp_match(p_code, '^(?:creator|referrer):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):payable$');
    IF m IS NOT NULL THEN kind := 'liability'; owner := m[1]::uuid; RETURN; END IF;
    m := regexp_match(p_code, '^ps_pending:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$');
    IF m IS NOT NULL THEN kind := 'liability'; owner := m[1]::uuid; RETURN; END IF;
    IF p_code ~ '^ps_pending:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:platform$' THEN
        kind := 'liability'; owner_null := true; RETURN;
    END IF;
    IF p_code ~ '^ps_pending:' THEN
        RAISE EXCEPTION 'malformed ps_pending account %', p_code USING ERRCODE = 'AJ422';
    END IF;
    IF p_code IN ('withdrawals:pending', 'payouts:pending', 'refunds:usdc_pending') OR p_code ~ '^suspense:' THEN
        kind := 'liability'; owner_null := true; RETURN;
    END IF;
END
$$;

CREATE FUNCTION ledger_accounts_shape() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    e record;
BEGIN
    e := ledger_account_expected(NEW.code);
    IF e.kind IS NOT NULL AND NEW.kind::text <> e.kind THEN
        RAISE EXCEPTION 'ledger account % must be a %', NEW.code, e.kind USING ERRCODE = 'AJ422';
    END IF;
    IF e.owner IS NOT NULL THEN
        IF NEW.owner_user_id IS NULL THEN
            NEW.owner_user_id := e.owner;
        ELSIF NEW.owner_user_id <> e.owner THEN
            RAISE EXCEPTION 'ledger account % must be owned by %', NEW.code, e.owner USING ERRCODE = 'AJ422';
        END IF;
    ELSIF e.owner_null AND NEW.owner_user_id IS NOT NULL THEN
        RAISE EXCEPTION 'ledger account % has no owner', NEW.code USING ERRCODE = 'AJ422';
    END IF;
    IF ledger_account_forced_non_negative(NEW.code) THEN
        NEW.non_negative := true;              -- whoever creates it (M5: app_api could pre-create it overdraftable)
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER ledger_accounts_10_shape BEFORE INSERT ON ledger_accounts
    FOR EACH ROW EXECUTE FUNCTION ledger_accounts_shape();

-- ---------------------------------------------------------------- ledger_accounts hash chain (L4)
ALTER TABLE ledger_accounts ADD COLUMN seq bigint;
ALTER TABLE ledger_accounts ADD COLUMN prev_hash sha256_hex;
ALTER TABLE ledger_accounts ADD COLUMN hash sha256_hex;

CREATE FUNCTION ledger_account_hash(a ledger_accounts) RETURNS text
LANGUAGE sql IMMUTABLE
AS $$
    SELECT encode(sha256(convert_to(a.prev_hash || E'\n' || canonical_json(jsonb_build_object(
        'id', a.id,
        'code', a.code,
        'kind', a.kind,
        'owner_user_id', a.owner_user_id,
        'non_negative', a.non_negative,
        'created_at', utc_iso(a.created_at)
    )), 'UTF8')), 'hex')
$$;

CREATE FUNCTION ledger_accounts_chain() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_seq  bigint;
    v_last text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('aijalon.ledger'));   -- same lock as the transaction chain
    SELECT a.seq, a.hash INTO v_seq, v_last FROM ledger_accounts a WHERE a.seq IS NOT NULL ORDER BY a.seq DESC LIMIT 1;
    NEW.seq := coalesce(v_seq, 0) + 1;
    NEW.prev_hash := coalesce(v_last, chain_genesis_hash());
    NEW.created_at := coalesce(NEW.created_at, now());
    NEW.hash := ledger_account_hash(NEW);
    RETURN NEW;
END
$$;

-- Backfill: flip protected accounts to non-negative, then chain the existing rows (append-only trigger off for the
-- duration of this transaction only; the owner running migrations is the only one who can do this).
ALTER TABLE ledger_accounts DISABLE TRIGGER ledger_accounts_append_only;
UPDATE ledger_accounts SET non_negative = true
 WHERE ledger_account_forced_non_negative(code) AND NOT non_negative;
DO $$
DECLARE
    r      record;
    v_prev text := chain_genesis_hash();
    v_seq  bigint := 0;
BEGIN
    FOR r IN SELECT id FROM ledger_accounts ORDER BY created_at, id LOOP
        v_seq := v_seq + 1;
        UPDATE ledger_accounts SET seq = v_seq, prev_hash = v_prev WHERE id = r.id;
        UPDATE ledger_accounts a SET hash = ledger_account_hash(a) WHERE a.id = r.id RETURNING a.hash INTO v_prev;
    END LOOP;
END
$$;
ALTER TABLE ledger_accounts ENABLE TRIGGER ledger_accounts_append_only;

ALTER TABLE ledger_accounts ALTER COLUMN seq SET NOT NULL;
ALTER TABLE ledger_accounts ALTER COLUMN prev_hash SET NOT NULL;
ALTER TABLE ledger_accounts ALTER COLUMN hash SET NOT NULL;
ALTER TABLE ledger_accounts ADD CONSTRAINT ledger_accounts_seq_key UNIQUE (seq);
ALTER TABLE ledger_accounts ADD CONSTRAINT ledger_accounts_prev_hash_key UNIQUE (prev_hash);
ALTER TABLE ledger_accounts ADD CONSTRAINT ledger_accounts_hash_key UNIQUE (hash);
ALTER TABLE ledger_accounts ADD CONSTRAINT ledger_accounts_protected_non_negative
    CHECK (non_negative OR NOT ledger_account_forced_non_negative(code));
CREATE TRIGGER ledger_accounts_20_chain BEFORE INSERT ON ledger_accounts
    FOR EACH ROW EXECUTE FUNCTION ledger_accounts_chain();

-- ---------------------------------------------------------------- running balances (L5)
CREATE TABLE ledger_account_balances (
    account_id     uuid PRIMARY KEY REFERENCES ledger_accounts(id),
    balance_micro  bigint NOT NULL DEFAULT 0,                  -- raw Σ amount_micro (+debit / −credit)
    entries        bigint NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
INSERT INTO ledger_account_balances (account_id, balance_micro, entries)
SELECT e.account_id, sum(e.amount_micro)::bigint, count(*) FROM ledger_entries e GROUP BY e.account_id;

CREATE FUNCTION ledger_entries_apply_balance() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
BEGIN
    INSERT INTO ledger_account_balances AS b (account_id, balance_micro, entries, updated_at)
    VALUES (NEW.account_id, NEW.amount_micro, 1, now())
    ON CONFLICT (account_id) DO UPDATE
       SET balance_micro = b.balance_micro + EXCLUDED.balance_micro, entries = b.entries + 1, updated_at = now();
    RETURN NULL;
END
$$;
CREATE TRIGGER ledger_entries_10_balance AFTER INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_apply_balance();
CREATE TRIGGER ledger_account_balances_no_delete BEFORE DELETE ON ledger_account_balances
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_account_balances_no_truncate BEFORE TRUNCATE ON ledger_account_balances
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Raw balance, O(1) (0 for an account without entries).
CREATE FUNCTION ledger_raw_balance(p_code text) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT coalesce((SELECT b.balance_micro FROM ledger_accounts a JOIN ledger_account_balances b ON b.account_id = a.id
                      WHERE a.code = p_code), 0)::bigint
$$;

-- Accounts whose running balance disagrees with the full Σ of their entries (verify job).
CREATE FUNCTION ledger_balance_mismatches()
RETURNS TABLE (code text, running_micro bigint, summed_micro bigint)
LANGUAGE sql STABLE
AS $$
    SELECT a.code, coalesce(b.balance_micro, 0)::bigint, coalesce(s.total, 0)::bigint
      FROM ledger_accounts a
      LEFT JOIN ledger_account_balances b ON b.account_id = a.id
      LEFT JOIN (SELECT e.account_id, sum(e.amount_micro) AS total FROM ledger_entries e GROUP BY e.account_id) s
             ON s.account_id = a.id
     WHERE coalesce(b.balance_micro, 0) <> coalesce(s.total, 0)
     ORDER BY a.code
$$;

-- ---------------------------------------------------------------- gated kinds + overdraft allowlist (M5)
-- The ONLY (kind, account) pairs that may take a non-negative account below zero. Fixed here, not chosen by callers.
CREATE FUNCTION ledger_overdraft_allowed(p_kind text, p_code text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT p_kind IN ('profit_share', 'stripe_refund', 'stripe_dispute')
       AND p_code ~ '^user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:fee_balance$'
$$;

-- Kinds that need a recorded authorisation (ledger_tx_authorizations) to be posted at all.
CREATE FUNCTION ledger_kind_requires_auth(p_kind text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT p_kind IN ('profit_share', 'stripe_refund', 'stripe_dispute', 'ps_pending_release') $$;

-- May the CURRENT role post this gated kind? (Inside a SECURITY DEFINER function current_user is its owner,
-- app_migrator; superusers are members of every role.)
CREATE FUNCTION ledger_role_may_post(p_kind text) RETURNS boolean
LANGUAGE sql STABLE
AS $$
    SELECT CASE
        WHEN NOT ledger_kind_requires_auth(p_kind) THEN true
        WHEN p_kind = 'profit_share' THEN pg_has_role(current_user, 'app_executor', 'MEMBER')
                                          OR pg_has_role(current_user, 'app_migrator', 'MEMBER')
        ELSE pg_has_role(current_user, 'app_migrator', 'MEMBER')
    END
$$;

CREATE TABLE ledger_tx_authorizations (
    tx_id          uuid PRIMARY KEY REFERENCES ledger_transactions(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    kind           text NOT NULL,
    authorized_as  text NOT NULL
);

CREATE FUNCTION ledger_tx_authorizations_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_kind text;
BEGIN
    SELECT t.kind INTO v_kind FROM ledger_transactions t WHERE t.id = NEW.tx_id;
    IF v_kind IS NULL OR v_kind <> NEW.kind OR NOT ledger_kind_requires_auth(v_kind) THEN
        RAISE EXCEPTION 'authorisation does not match a gated ledger transaction' USING ERRCODE = 'AJ403';
    END IF;
    IF NOT ledger_role_may_post(v_kind) THEN
        RAISE EXCEPTION 'role % may not authorise ledger kind %', current_user, v_kind USING ERRCODE = 'AJ403';
    END IF;
    NEW.authorized_as := current_user;
    RETURN NEW;
END
$$;
CREATE TRIGGER ledger_tx_authorizations_guard BEFORE INSERT ON ledger_tx_authorizations
    FOR EACH ROW EXECUTE FUNCTION ledger_tx_authorizations_guard();
CREATE TRIGGER ledger_tx_authorizations_append_only BEFORE UPDATE OR DELETE ON ledger_tx_authorizations
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_tx_authorizations_no_truncate BEFORE TRUNCATE ON ledger_tx_authorizations
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Integrity of ONE ledger transaction (replaces 0001's version; same callers: ledger_post + deferred triggers).
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
    -- gated kinds need a recorded authorisation (only ledger_post / the SECURITY DEFINER wrappers write one)
    IF ledger_kind_requires_auth(v_tx.kind)
       AND NOT EXISTS (SELECT 1 FROM ledger_tx_authorizations z WHERE z.tx_id = p_tx_id) THEN
        RAISE EXCEPTION 'ledger kind % was posted without authorisation', v_tx.kind USING ERRCODE = 'AJ403';
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
END
$$;

-- The ONE supported way to write the ledger (replaces 0001's version; same signature and grants).
CREATE OR REPLACE FUNCTION ledger_post(p_idempotency_key text, p_kind text, p_memo text, p_created_by text, p_entries jsonb)
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
BEGIN
    IF NOT ledger_role_may_post(p_kind) THEN
        RAISE EXCEPTION 'role % may not post ledger kind %', current_user, p_kind USING ERRCODE = 'AJ403';
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
    IF ledger_kind_requires_auth(p_kind) THEN
        INSERT INTO ledger_tx_authorizations (tx_id, kind, authorized_as) VALUES (v_id, p_kind, current_user);
    END IF;

    INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
    SELECT v_id, a.id, x.amt
      FROM unnest(v_codes, v_amounts) WITH ORDINALITY AS x(code, amt, o)
      JOIN ledger_accounts a ON a.code = x.code
     ORDER BY x.o;

    PERFORM ledger_check_tx(v_id);
    RETURN QUERY SELECT v_id, true;
END
$$;

-- Stripe refund / dispute (webhook path, app_api): the only way to post those overdraft kinds. Fixed shape:
-- exactly user:{uuid}:fee_balance +amount and stripe:clearing −amount, key stripe:refund:… / stripe:dispute:….
CREATE FUNCTION ledger_post_payment_reversal(p_idempotency_key text, p_kind text, p_memo text, p_created_by text,
                                             p_entries jsonb)
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
    RETURN QUERY SELECT l.tx_id, l.created FROM ledger_post(p_idempotency_key, p_kind, p_memo, p_created_by, p_entries) l;
END
$$;

-- ---------------------------------------------------------------- uncollected profit share release (C1)
-- Releases pending (uncollected) profit share of ONE user to the creator payables / platform revenue, pro-rata to
-- each pending account's balance, so that what stays pending never exceeds the user's current debt:
--   released = pending − min(pending, max(0, −spendable)). Returns the released micro-USD. Idempotent per ledger state
-- (key ps_release:{user}:{newest ledger seq}); runs under the ledger chain lock.
CREATE FUNCTION ps_pending_release(p_user uuid, p_created_by text DEFAULT 'system:ps_release') RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    v_spend   bigint;
    v_debt    bigint;
    v_pending bigint;
    v_release bigint;
    v_left    bigint;
    v_part    bigint;
    v_entries jsonb := '[]'::jsonb;
    v_target  text;
    v_seq     bigint;
    r         record;
    v_alloc   jsonb := '{}'::jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('aijalon.ledger'));
    v_spend := -ledger_raw_balance('user:' || p_user::text || ':fee_balance');
    v_debt := greatest(0, -v_spend);
    SELECT coalesce(sum(-b.balance_micro), 0) INTO v_pending
      FROM ledger_accounts a JOIN ledger_account_balances b ON b.account_id = a.id
     WHERE a.code LIKE 'ps_pending:' || p_user::text || ':%' AND b.balance_micro < 0;
    v_release := v_pending - least(v_pending, v_debt);
    IF v_release <= 0 THEN
        RETURN 0;
    END IF;
    -- floor pro-rata shares, then the remainder 1 micro at a time in code order (never above an account's balance)
    v_left := v_release;
    FOR r IN SELECT a.code, -b.balance_micro AS q
               FROM ledger_accounts a JOIN ledger_account_balances b ON b.account_id = a.id
              WHERE a.code LIKE 'ps_pending:' || p_user::text || ':%' AND b.balance_micro < 0
              ORDER BY a.code COLLATE "C" LOOP
        v_part := floor(v_release::numeric * r.q / v_pending)::bigint;
        v_alloc := v_alloc || jsonb_build_object(r.code, jsonb_build_array(v_part, r.q));
        v_left := v_left - v_part;
    END LOOP;
    FOR r IN SELECT key AS code FROM jsonb_each(v_alloc) ORDER BY key COLLATE "C" LOOP
        EXIT WHEN v_left <= 0;
        IF (v_alloc->r.code->>0)::bigint < (v_alloc->r.code->>1)::bigint THEN
            v_part := least(v_left, (v_alloc->r.code->>1)::bigint - (v_alloc->r.code->>0)::bigint);
            v_alloc := jsonb_set(v_alloc, ARRAY[r.code, '0'], to_jsonb((v_alloc->r.code->>0)::bigint + v_part));
            v_left := v_left - v_part;
        END IF;
    END LOOP;
    FOR r IN SELECT key AS code, (value->>0)::bigint AS amt FROM jsonb_each(v_alloc) ORDER BY key COLLATE "C" LOOP
        CONTINUE WHEN r.amt <= 0;
        IF r.code LIKE '%:platform' THEN
            v_target := 'platform:revenue:profit_share';
        ELSE
            v_target := 'creator:' || split_part(r.code, ':', 3) || ':payable';
            INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative)
            VALUES (v_target, 'liability', split_part(r.code, ':', 3)::uuid, true)
            ON CONFLICT (code) DO NOTHING;
        END IF;
        v_entries := v_entries || jsonb_build_array(jsonb_build_object('account', r.code, 'amount_micro', r.amt),
                                                    jsonb_build_object('account', v_target, 'amount_micro', -r.amt));
    END LOOP;
    SELECT coalesce(max(seq), 0) INTO v_seq FROM ledger_transactions;
    PERFORM ledger_post('ps_release:' || p_user::text || ':' || v_seq::text, 'ps_pending_release',
                        'uncollected profit share released after top-up', p_created_by, v_entries);
    RETURN v_release;
END
$$;

-- Every posting that CREDITS a user fee balance (top-ups of any kind) releases that user's pending profit share.
-- A failure here never blocks the top-up (the daily settlement sweep retries the release).
CREATE FUNCTION ledger_entries_release_pending() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT DISTINCT a.owner_user_id AS uid
               FROM new_entries n JOIN ledger_accounts a ON a.id = n.account_id
              WHERE n.amount_micro < 0 AND a.owner_user_id IS NOT NULL
                AND a.code ~ '^user:[0-9a-f-]{36}:fee_balance$'
                AND EXISTS (SELECT 1 FROM ledger_accounts p JOIN ledger_account_balances b ON b.account_id = p.id
                             WHERE p.code LIKE 'ps_pending:' || a.owner_user_id::text || ':%' AND b.balance_micro < 0)
    LOOP
        BEGIN
            PERFORM ps_pending_release(r.uid, 'system:ps_release_on_topup');
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'ps_pending_release(%) failed: % %', r.uid, SQLSTATE, SQLERRM;
        END;
    END LOOP;
    RETURN NULL;
END
$$;
CREATE TRIGGER ledger_entries_20_release_pending AFTER INSERT ON ledger_entries
    REFERENCING NEW TABLE AS new_entries
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_entries_release_pending();

-- ---------------------------------------------------------------- verify_chain + heads + anchors (L4, L5, M7)
CREATE OR REPLACE FUNCTION verify_chain()
RETURNS TABLE (chain text, seq bigint, row_id uuid, reason text)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    t        ledger_transactions%ROWTYPE;
    a        audit_log%ROWTYPE;
    la       ledger_accounts%ROWTYPE;
    m        record;
    v_prev   text;
    v_expect bigint;
BEGIN
    v_prev := chain_genesis_hash();
    v_expect := 1;
    FOR t IN SELECT * FROM ledger_transactions lt ORDER BY lt.seq LOOP
        IF t.seq <> v_expect THEN
            RETURN QUERY SELECT 'ledger_transactions'::text, t.seq, t.id, format('sequence gap: expected %s', v_expect);
            EXIT;
        ELSIF t.prev_hash <> v_prev THEN
            RETURN QUERY SELECT 'ledger_transactions'::text, t.seq, t.id, 'prev_hash does not match previous row'::text;
            EXIT;
        ELSIF t.hash <> ledger_tx_hash(t) THEN
            RETURN QUERY SELECT 'ledger_transactions'::text, t.seq, t.id, 'row hash mismatch (row altered)'::text;
            EXIT;
        ELSIF t.entries_digest <> ledger_tx_stored_digest(t.id) THEN
            RETURN QUERY SELECT 'ledger_transactions'::text, t.seq, t.id, 'entries digest mismatch (entries altered)'::text;
            EXIT;
        END IF;
        v_prev := t.hash;
        v_expect := v_expect + 1;
    END LOOP;

    v_prev := chain_genesis_hash();
    v_expect := 1;
    FOR a IN SELECT * FROM audit_log al ORDER BY al.seq LOOP
        IF a.seq <> v_expect THEN
            RETURN QUERY SELECT 'audit_log'::text, a.seq, a.id, format('sequence gap: expected %s', v_expect);
            EXIT;
        ELSIF a.prev_hash <> v_prev THEN
            RETURN QUERY SELECT 'audit_log'::text, a.seq, a.id, 'prev_hash does not match previous row'::text;
            EXIT;
        ELSIF a.hash <> audit_log_hash(a) THEN
            RETURN QUERY SELECT 'audit_log'::text, a.seq, a.id, 'row hash mismatch (row altered)'::text;
            EXIT;
        END IF;
        v_prev := a.hash;
        v_expect := v_expect + 1;
    END LOOP;

    v_prev := chain_genesis_hash();
    v_expect := 1;
    FOR la IN SELECT * FROM ledger_accounts x ORDER BY x.seq LOOP
        IF la.seq <> v_expect THEN
            RETURN QUERY SELECT 'ledger_accounts'::text, la.seq, la.id, format('sequence gap: expected %s', v_expect);
            EXIT;
        ELSIF la.prev_hash <> v_prev THEN
            RETURN QUERY SELECT 'ledger_accounts'::text, la.seq, la.id, 'prev_hash does not match previous row'::text;
            EXIT;
        ELSIF la.hash <> ledger_account_hash(la) THEN
            RETURN QUERY SELECT 'ledger_accounts'::text, la.seq, la.id, 'row hash mismatch (account attributes altered)'::text;
            EXIT;
        END IF;
        v_prev := la.hash;
        v_expect := v_expect + 1;
    END LOOP;

    FOR m IN SELECT * FROM ledger_balance_mismatches() LOOP
        RETURN QUERY SELECT 'ledger_account_balances'::text, NULL::bigint, NULL::uuid,
            format('running balance of %s is %s, entries sum to %s', m.code, m.running_micro, m.summed_micro);
    END LOOP;
END
$$;

CREATE OR REPLACE VIEW chain_heads AS
SELECT 'ledger_transactions'::text AS chain, l.seq, l.hash, l.created_at
  FROM (SELECT * FROM ledger_transactions ORDER BY seq DESC LIMIT 1) l
UNION ALL
SELECT 'audit_log'::text, x.seq, x.hash, x.created_at
  FROM (SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1) x
UNION ALL
SELECT 'ledger_accounts'::text, y.seq, y.hash, y.created_at
  FROM (SELECT * FROM ledger_accounts ORDER BY seq DESC LIMIT 1) y;

CREATE TABLE ledger_chain_anchors (                            -- append-only
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    anchor_date  date NOT NULL,
    chain        text NOT NULL CHECK (chain IN ('ledger_transactions', 'audit_log', 'ledger_accounts')),
    seq          bigint NOT NULL CHECK (seq > 0),
    hash         sha256_hex NOT NULL,
    published    jsonb NOT NULL DEFAULT '{}'::jsonb,           -- where the anchor was sent (ops Telegram / email)
    UNIQUE (anchor_date, chain)
);
CREATE TRIGGER ledger_chain_anchors_append_only BEFORE UPDATE OR DELETE ON ledger_chain_anchors
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_chain_anchors_no_truncate BEFORE TRUNCATE ON ledger_chain_anchors
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Anchored heads that no longer match the chain (row gone = truncated after anchoring; hash differs = rewritten).
CREATE FUNCTION verify_chain_anchors()
RETURNS TABLE (chain text, seq bigint, anchor_date date, reason text)
LANGUAGE sql STABLE
AS $$
    SELECT x.chain, x.seq, x.anchor_date,
           CASE WHEN x.found IS NULL THEN 'anchored row missing (chain truncated or rewritten)'
                ELSE 'anchored hash differs (chain rewritten)' END
      FROM (SELECT an.chain, an.seq, an.anchor_date, an.hash,
                   CASE an.chain
                       WHEN 'ledger_transactions' THEN (SELECT t.hash FROM ledger_transactions t WHERE t.seq = an.seq)
                       WHEN 'audit_log' THEN (SELECT l.hash FROM audit_log l WHERE l.seq = an.seq)
                       ELSE (SELECT a.hash FROM ledger_accounts a WHERE a.seq = an.seq)
                   END::text AS found
              FROM ledger_chain_anchors an) x
     WHERE x.found IS DISTINCT FROM x.hash::text
     ORDER BY x.anchor_date, x.chain
$$;

-- ---------------------------------------------------------------- per-subscription books (H1) + claims (M3) + H2
ALTER TABLE fills ADD COLUMN book_pnl_micro bigint;            -- our realised PnL of this fill (avg entry) − fee
ALTER TABLE fills ADD COLUMN ps_settlement_date date;          -- settlement that consumed it (NULL = not yet)
ALTER TABLE fills ADD COLUMN oid_verified boolean NOT NULL DEFAULT false;
ALTER TABLE funding_events ADD COLUMN ps_settlement_date date;
CREATE INDEX fills_unsettled_idx ON fills (subscription_id, time)
    WHERE subscription_id IS NOT NULL AND ps_settlement_date IS NULL;
CREATE INDEX funding_events_unsettled_idx ON funding_events (subscription_id, time)
    WHERE subscription_id IS NOT NULL AND ps_settlement_date IS NULL;

CREATE TABLE subscription_positions (                          -- the book: OUR fills only
    subscription_id    uuid NOT NULL REFERENCES subscriptions(id),
    coin               text NOT NULL CHECK (is_valid_coin(coin)),
    qty                numeric NOT NULL DEFAULT 0,               -- signed (+ long / − short)
    avg_px             numeric NOT NULL DEFAULT 0 CHECK (avg_px >= 0),
    realized_micro     bigint NOT NULL DEFAULT 0,                -- Σ book PnL (fills − fees + adjustments), info only
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subscription_id, coin)
);

CREATE TABLE subscription_pnl_events (                         -- book adjustments that are not our fills
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    subscription_id     uuid NOT NULL REFERENCES subscriptions(id),
    coin                text NOT NULL CHECK (is_valid_coin(coin)),
    kind                text NOT NULL CHECK (kind IN ('foreign_fill', 'mtm_pause', 'mtm_leave', 'excess_reduce')),
    ref                 text NOT NULL CHECK (length(ref) BETWEEN 1 AND 200),   -- tid / status epoch (idempotency)
    time                timestamptz NOT NULL,
    px                  numeric,                                  -- fill price / mark price used
    qty_before          numeric NOT NULL,
    avg_before          numeric NOT NULL,
    qty_after           numeric NOT NULL,
    avg_after           numeric NOT NULL,
    pnl_micro           bigint NOT NULL DEFAULT 0,                -- PnL attributed to the subscription (MTM gain/loss)
    detail              jsonb NOT NULL DEFAULT '{}'::jsonb,
    ps_settlement_date  date,
    UNIQUE (subscription_id, kind, ref)
);
CREATE INDEX subscription_pnl_events_unsettled_idx ON subscription_pnl_events (subscription_id, time)
    WHERE ps_settlement_date IS NULL;

-- rows already inside a subscription's settled window were consumed by past settlements
UPDATE fills f SET ps_settlement_date = (s.pnl_cursor AT TIME ZONE 'UTC')::date
  FROM subscriptions s
 WHERE f.subscription_id = s.id AND s.pnl_cursor IS NOT NULL AND f.time <= s.pnl_cursor AND f.ps_settlement_date IS NULL;
UPDATE funding_events e SET ps_settlement_date = (s.pnl_cursor AT TIME ZONE 'UTC')::date
  FROM subscriptions s
 WHERE e.subscription_id = s.id AND s.pnl_cursor IS NOT NULL AND e.time <= s.pnl_cursor AND e.ps_settlement_date IS NULL;
-- legacy attributions: cloid-matched fills of recorded orders whose oid agrees
UPDATE fills f SET oid_verified = true
  FROM orders o
 WHERE f.attributed_via = 'cloid' AND o.cloid = f.cloid AND o.subscription_id = f.subscription_id
   AND (o.oid IS NULL OR o.oid = f.oid);

-- ---------------------------------------------------------------- grants (no DELETE for anyone)
GRANT SELECT ON ledger_account_balances, ledger_tx_authorizations, ledger_chain_anchors TO app_api, app_executor;
GRANT INSERT ON ledger_tx_authorizations TO app_executor;          -- profit_share (guard trigger checks the role)
GRANT INSERT ON ledger_chain_anchors TO app_executor;              -- /internal/verify-chain
GRANT SELECT, INSERT, UPDATE ON subscription_positions, subscription_pnl_events TO app_executor;
GRANT SELECT ON subscription_positions, subscription_pnl_events TO app_api;
GRANT ALL ON ledger_account_balances, ledger_tx_authorizations, ledger_chain_anchors, subscription_positions,
             subscription_pnl_events TO app_migrator;

REVOKE EXECUTE ON FUNCTION ledger_post_payment_reversal(text, text, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger_post_payment_reversal(text, text, text, text, jsonb) TO app_api, app_executor;
REVOKE EXECUTE ON FUNCTION ps_pending_release(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ps_pending_release(uuid, text) TO app_api, app_executor;
REVOKE EXECUTE ON FUNCTION ledger_entries_apply_balance() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ledger_entries_release_pending() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION verify_chain_anchors() TO app_api, app_executor;

-- =====================================================================================================
-- 0014_cleanup.sql — final clean-up round (admin strategy pause billing, card-hold linkage of the pending sweep).
-- Runs as app_migrator (owner). No BEGIN/COMMIT (migrate.py wraps each file in one transaction).
-- Explicit GRANTs per table; nobody gets DELETE.
--
--   strategies.paused_at        when an admin paused the strategy (POST /v1/admin/strategies/{id}/pause). While a
--                               strategy is paused: no new subscriptions and no user resume (API: status must be
--                               `listed`), no renewal is charged (settlement skips a due renewal and leaves the
--                               subscription's billing status untouched), the executor opens nothing (entries gate —
--                               exits still run), and every live subscriber gets the mandatory `strategy_paused`
--                               alert. On unpause (the maker-checker `strategy_list` approval from `paused`) every
--                               live subscription whose prepaid period had not ended when the pause started gets the
--                               paused time back (current_period_end += unpause − paused_at), and billing resumes
--                               at the subscription's PINNED price (0011) from the next settlement. Cleared on
--                               unpause. Existing paused strategies start counting from this migration.
--
--   ps_pending_release()        (REVIEW_MONEY C1 × H4 linkage, 0010/0011) the release key ps_release:{user}:{seq}
--                               must name the posting that ACTUALLY covered the user's debt, because
--                               payable_card_held() holds a release for the card dispute window (120 days) exactly
--                               when that posting is a card top-up (key stripe:…). The top-up trigger released with
--                               the triggering posting's seq, but the daily settlement sweep (PgPendingReleaser —
--                               e.g. after a failed trigger release) used the newest GLOBAL ledger seq, i.e. some
--                               unrelated posting, so a card-funded release was never held. Now: the user's
--                               fee-balance postings since their previous release are replayed; if a card credit
--                               (stripe:…) was applied while the balance was negative (it paid debt), the key carries
--                               the newest such card posting's seq (held in full, like the trigger path — the
--                               conservative choice when card and USDC both paid debt); otherwise the newest global
--                               seq as before (USDC / other funding: not held). Same signature, SECURITY DEFINER,
--                               owner and grants as 0010.
-- =====================================================================================================

-- ---------------------------------------------------------------- admin strategy pause
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS paused_at timestamptz;
UPDATE strategies SET paused_at = now() WHERE status = 'paused' AND paused_at IS NULL;

-- ---------------------------------------------------------------- pending release attributed to its funding source
CREATE OR REPLACE FUNCTION ps_pending_release(p_user uuid, p_created_by text DEFAULT 'system:ps_release') RETURNS bigint
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
    v_last    bigint;
    v_bal     bigint;
    v_card    bigint;
    v_fee     text := 'user:' || p_user::text || ':fee_balance';
    r         record;
    v_alloc   jsonb := '{}'::jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('aijalon.ledger'));
    v_spend := -ledger_raw_balance(v_fee);
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

    -- Which funding covered the debt? Replay this user's fee-balance postings since their previous release
    -- (spendable = −raw balance); a card credit applied while spendable < 0 paid debt → key its seq.
    SELECT max(t.seq) INTO v_last FROM ledger_transactions t
     WHERE t.kind = 'ps_pending_release' AND t.idempotency_key LIKE 'ps_release:' || p_user::text || ':%';
    SELECT coalesce(-sum(e.amount_micro), 0) INTO v_bal
      FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
      JOIN ledger_transactions t ON t.id = e.tx_id
     WHERE a.code = v_fee AND t.seq <= coalesce(v_last, 0);
    FOR r IN SELECT t.seq, t.idempotency_key AS k, -sum(e.amount_micro) AS amt
               FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
               JOIN ledger_transactions t ON t.id = e.tx_id
              WHERE a.code = v_fee AND t.seq > coalesce(v_last, 0)
              GROUP BY t.seq, t.idempotency_key
              ORDER BY t.seq LOOP
        IF r.amt > 0 AND v_bal < 0 AND r.k LIKE 'stripe:%' THEN
            v_card := r.seq;
        END IF;
        v_bal := v_bal + r.amt;
    END LOOP;
    IF v_card IS NOT NULL THEN
        v_seq := v_card;
    ELSE
        SELECT coalesce(max(seq), 0) INTO v_seq FROM ledger_transactions;
    END IF;
    PERFORM ledger_post('ps_release:' || p_user::text || ':' || v_seq::text, 'ps_pending_release',
                        'uncollected profit share released after top-up', p_created_by, v_entries);
    RETURN v_release;
END
$$;
REVOKE EXECUTE ON FUNCTION ps_pending_release(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ps_pending_release(uuid, text) TO app_api, app_executor;

# Money-flow security and financial-integrity review — aijalon.trade

Date: 30 Sep 2026. Code: commit `b3bee9a` plus the working tree at review time. Line numbers are from that tree; another agent was editing some files at the same time, so a few lines may have moved.
Scope: migrations 0001–0008, `app/ledger/**`, `app/money.py`, `app/domain/{fees,profit_share,referrals,billing}.py`, `app/execution/{settlement,pg}.py` (plus the parts of executor/reconcile they depend on), `app/api/{ledger_ops,store,creator_earnings}.py`, routers `balance, deposits, withdrawals, subscriptions, posts, referrals, admin, creator, webhooks` (plus `me.py` for referral binding), `app/payments/**`, `app/jobs_data/{deposits,fills,funding}.py`, `app/hl/{fills,deposits}.py`.

Method: I read the code by hand. I also ran exploits against the real SQL in a throwaway Postgres 16 database (`sec_money_1`: all 8 migrations applied, then dropped).

**Severity scale.** Critical = anyone can take real treasury money. High = direct, repeatable loss of revenue or user funds, or unsafe behaviour of the money state. Medium = conditional loss, or a missing control that a High depends on. Low/Info = hygiene.

---

## 0. What holds up (verified)

| Area | Result |
|---|---|
| Concurrent double-spend on the fee balance | **Safe.** Two sessions each posted 60 against a balance of 100. Under READ COMMITTED the second failed with `AJ402`. Under REPEATABLE READ (stale snapshot) the second failed on `ledger_transactions_seq_key` (23505). The chain advisory lock plus `UNIQUE(seq, prev_hash)` serialise every ledger write. |
| Idempotency | `ledger_post` re-checks under the chain lock. The same key with different entries or kind gives `AJ409`. The key namespaces do not collide: `sub:{id}:start` / `sub:{id}:{date}`, `plan:{uid}:start:…` / `plan:{uid}:{date}`, `ps:`, `bf:{addr}:{tid}`, `stripe:{pi}`, `stripe:refund:{ch}:{cum}`, `stripe:dispute:{dp}`, `usdc_hl:{hash}`, `withdrawal:{id}:hold/release/sent`, `payout:{id}:…`. |
| USDC credited by both the confirm route and the scanner | **No double credit.** Both use `detect_deposits` → `credit_from_detection`, which produce the same key (`usdc_hl:{hash}`), the same kind and the same entries. The loser of a race gets `created=false`. If a deposit was booked to suspense first, a later credit hits `Conflict` (kind differs) instead of crediting twice. The zero hash is refused (`hl/deposits.py:132`). |
| Stripe webhook redelivery | Idempotent. Keys are per PaymentIntent, per cumulative refund amount and per dispute. The credit is net of the actual fee from `balance_transaction`. A refund or dispute reverses the gross amount, which is intended and documented (a full chargeback leaves the balance negative by the fee). |
| Fee splits | `split_exact`, `split_builder_fee`, `subscription_split`, `post_sale_split` and `profit_share.settle` all sum exactly to their input, with the remainder going to the platform. Settlement asserts this again (`settlement.py:329,428`). |
| Ledger sign convention | Consistent in every poster I found: settlement (ps / renewal / plan / builder), `ledger_ops` (charges, holds, release, sent, apply_credit/apply_debit), the deposit scanner (credit and held). Spend = debit (+) on the liability `user:*:fee_balance`; top-up = credit (−); treasury out = credit on the asset. |
| Negative / zero / float amounts | Blocked in Python (`normalize_entries`), in SQL (`^-?[0-9]{1,18}$`, `<> 0`, Σ = 0) and by table CHECKs (`amount_micro > 0` on deposits, withdrawals and payouts). |
| Tids shared between two of our users | Handled: fills are `UNIQUE(trading_address, tid)` and the builder-fee key is `bf:{addr}:{tid}`. |
| Precision | Micro-USD is integer throughout. Hyperliquid strings go through `Decimal` and are floored. Funding pro-rata floors toward −∞. There is no float on any money path I reviewed. |
| Hash chain | Tampering with an entry amount is detected: `verify_chain` returned "entries digest mismatch" after I disabled the trigger and updated an entry. See L4 and M7 for what is not covered. |

---

## CRITICAL

### C1. Creator / referrer payables are credited from profit share the platform never collected, so colluding accounts can drain the treasury
- **Where:** `backend/app/execution/settlement.py:279-293` credits `creator:{id}:payable` for the full charge. The `profit_share` kind may overdraw the user (`0001_init.sql:482-484`, `ledger_check_tx:620-622`). `withdrawals.py:291-327` pays the payable out in USDC.
- **Problem:** Profit share is post-paid. The user's fee balance is only required to be ≥ price + $10 at subscribe time (`subscriptions.py:184-185`). After that, profit share is posted in full even when the balance goes deeply negative. The creator's share is booked immediately as a payable liability and can be withdrawn after KYC plus two admin approvals. Nothing ties the creator payout to the platform having received the money.
- **Exploit (numbers):**
  1. Attacker A is a KYC'd creator (a mule identity is enough). A lists strategy S at 12% profit share.
  2. Accounts B1 and B2 each deposit $10 USDC and subscribe with $50k allocation, B1 to S (long) and B2 to a second strategy S' of A's (short, same coin). Together the positions are market-neutral.
  3. One side gains every day. Say the coin moves 4%, so B1 realises +$2,000 at 1x.
     - Settlement charges B1 13.5% = $270. The creator payable gets $240. B1's balance goes to −$260.
     - The losing side pays nothing: losses never refund.
  4. Repeat each day: roughly $240 per 4% move goes to A's payable.
  5. A requests payouts. Admins see a valid ledger balance and approve.
  6. B1 and B2 abandon their negative balances. Their HL accounts net out to about zero minus trading fees: builder 0.1% and HL ≈0.045% per side, so ≈0.3% round trip ≈ $150 on $50k.
  7. Net extraction ≈ 12% × |daily move| × notional − fees, paid from treasury USDC that users never deposited.
- The same applies without collusion whenever a real subscriber simply never tops up (credit loss). The creator is still paid at 12%.
- **Fix (precise):**
  - Split the charge into a collected part and a receivable part:
    - `collected = min(charge, max(0, spendable))`: debit the fee balance and credit the creator payable and platform revenue as now.
    - `uncollected = charge − collected`: debit the fee balance (overdraft) and credit `creator:{id}:payable_pending` (liability, not payable out) and `platform:revenue:profit_share_pending`.
  - On each later top-up, release pending to payable pro-rata. Do this in the same transaction as the credit, under the user lock.
  - Alternatively, and simpler: pay creators only from an account swept daily for the amount actually recovered from the user.
  - Also add a DB guard. The payout source must be `creator:*:payable`, and an account `…:payable_pending` must never appear in a `payout_hold`.
  - Add an ops rule: alert when a creator's payable is funded by more than X% of users with negative balances.

---

## HIGH

### H1. Profit share can be avoided with manual trades on the same account (pause, "leave", or even while active)
- **Where:** `backend/app/hl/fills.py:182-184` ignores foreign fills (`out.foreign`), and PnL is `closedPnl − fee` per our fill (`fills.py:86-87`). `closedPnl` is computed by Hyperliquid against the **account's** average entry, not the subscription's. The executor plans against the **on-chain** position (`execution/executor.py:17,352-359`). `subscriptions.py:229-236` allows pause/unpause, and `paused_user` places no orders. SPEC §12 "leave" stops at `cancelled_at`.
- **Exploit:**
  1. The subscription holds 1 BTC bought at $60k. The price is now $70k, so +$10k is unrealised.
  2. The user sells 1 BTC by hand at $70k. That is a foreign fill: +$10k realised and never attributed.
  3. Next bar the executor sees 0 against a target of 1 BTC and buys 1 BTC at $70k, so the new average entry is $70k.
  4. The strategy later exits at $70k. Attributed PnL is ≈ −fees.
  5. Evaded: 13.5% × $10k = **$1,350** (creator $1,200, platform $150). This repeats on every winning trade.
  - Pause: pause, sell by hand, unpause. The strategy re-enters at market with the same result.
  - "Leave": cancel with leave while in profit, then close by hand. Profit share is lost for good.
  - The reverse also works: adding by hand at worse prices raises the account average entry, so our closing fills show a loss (cum_pnl ↓, HWM not reached).
  - `check_positions` in reconcile (`reconcile.py:103-127`) only raises a `position_drift` warning.
- **Fix:**
  - Keep a per-subscription **virtual position and cost basis** from our own fills (FIFO or average cost, computed from `fills` with `attributed_via='cloid'`). Compute attributed PnL from it, not from HL `closedPnl`.
  - Any foreign fill on a strategy coin in the trading address (these are already fetched as `att.foreign`) triggers one of:
    - (a) a synthetic mark-to-market close of the subscription's virtual position at the fill price, so profit share is charged on the gain; or
    - (b) moving the subscription to `paused_user` and alerting.
  - On `paused_user` and on cancel "leave", settle profit share on **mark-to-market unrealised PnL** at that moment (charge at mark; a later manual close is the user's own business).
  - Require a dedicated sub-account (already recommended in the UI copy) and refuse to resume while foreign positions exist on strategy coins.

### H2. Fill attribution can be forged: prefixed cloids, the window fallback and a non-secret HMAC key feed PnL and builder-fee revenue
- **Where:**
  - `hl/fills.py:182-196`: any fill whose cloid starts `0xa17a1000` is "ours". If the cloid is not in `orders`, a single covering subscription window attributes it (`via="window"`).
  - `execution/pg.py:674`: builder fee is recognised for any fill whose cloid matches the prefix.
  - `hl/client.py:98`: cloid = HMAC keyed with `subscription_id`, which the user knows, so the user can compute our exact past and future cloids.
- **Problem:** The user controls their own HL account and may place orders with any cloid and any approved builder.
  1. **Loss injection:** the user tags only their *losing* manual trades on strategy coins with a prefixed cloid. They are attributed via the window, so cum_pnl ↓ and profit share is avoided. Winning trades carry no cloid and stay foreign.
  2. **Phantom builder revenue:** the user places prefixed-cloid orders with `builder = attacker's own builder address` (any address holding ≥100 USDC can be a builder). HL reports `builderFee` in the fill without the builder address (**UNCERTAIN**: verify that userFills never exposes the builder). Settlement then books `builder:hl_receivable` and credits creator 50% and referrer 20–100% of pool.
     - Example: attacker controls the referrer (self-referral, see M2) and, optionally, the creator of the strategy. They wash-trade $10M notional between two of their own subscribed accounts.
     - Phantom credits are 0.05% (creator) + 0.02% (Elite referrer) = 0.07% × $20M fill notional (both sides) ≈ **$14k** of payables.
     - Cost: HL fees ≈ 0.03–0.045% per side ≈ $6–9k. The builder fee goes to the attacker's own builder, so it nets to zero for them.
     - The wash volume also lifts the referrer to Elite (≥ $25M referred notional).
     - Reconcile's `builder_mismatch` fires, but only as a daily alert after the payables are already booked.
  3. Precomputable cloids let a user reuse or pre-empt our cloids. **UNCERTAIN**: this depends on HL's cloid-uniqueness rules. It could also DoS the user's own subscription.
- **Fix:**
  - Attribute a fill to a subscription **only** when `fill.oid == orders.oid` for the cloid we recorded from the exchange response. The oid is assigned by HL and cannot be forged. Store the oid on `resting`/`filled` responses and on orderStatus recovery.
  - Keep the window fallback for **alerting only**. Never use it for PnL or builder-fee recognition; hold such fills with `subscription_id NULL` until ops confirms.
  - Recognise builder fees only for oid-matched fills, and cap the recognised amount at `notional × f/100000`.
  - Key the cloid HMAC with a server secret (KMS-derived), not with `subscription_id`.

### H3. Pause/unpause resets the billing state machine: a paid strategy can be used indefinitely for free
- **Where:**
  - `api/routers/subscriptions.py:229-236`: unpause sets `status='active'` with no balance or renewal check. It is allowed from any of `_CHANGEABLE`, which includes `past_due` and `reduce_only`.
  - `store.update_subscription` (`store.py:477-487`) keeps the old `past_due_since`, which settlement then overwrites.
  - `domain/billing.py:220-221`: `active` → `past_due` with `since=now`, so grace restarts.
  - `executor.py:348` treats `past_due` as fully tradable and never checks the 72h grace. The flip to `reduce_only` only happens at 00:30 settlement.
- **Exploit:**
  1. A strategy costs $500/month. The user pays month 1, then keeps the fee balance at $0.
  2. At renewal the sub goes `past_due` (72h of entries), then `reduce_only`.
  3. The user PATCHes `paused=true` and then `paused=false`. The sub is `active` immediately and trades until the next 00:30 settlement.
  4. That settlement sets `past_due` with a new `since`, giving 72h more (up to 96h in practice).
  5. Repeat every ~4 days with two step-up calls.
  6. The creator loses $500/month per abusing subscriber.
- **Fix:**
  - Store `pre_pause_status` and `past_due_since` when pausing.
  - On unpause, restore the previous status and timestamp. If `current_period_end <= now`, run `billing.next_status(..., amount_due=price)` and charge the renewal in the same transaction (under `lock_user`), or refuse unpause with `InsufficientBalance`.
  - In the executor, compute entries with `billing.entries_allowed(status, past_due_since, now, grace)`, not `status == 'reduce_only'`.

### H4. Card-funded balances can be laundered into USDC through creator/referrer payouts, and chargebacks do not claw them back
- **Where:**
  - `payments/stripe_pay.py:996-1007`: a dispute debits only the buyer's fee balance.
  - `ledger_ops.charge_post` and `charge_subscription_start` credit the creator payable at once.
  - `withdrawals.py:291-327`: a payout needs only KYC (creator) and nothing at all for referrers.
  - `store.withdrawable_usdc` (`store.py:593-603`) caps fee-balance withdrawals at Σ USDC deposits − withdrawals and ignores spending order.
- **Exploit:**
  1. The attacker tops up $1,000 with a stolen card.
  2. The attacker buys a $1,000 post from their own KYC'd creator account (a mule). The creator payable gets $999.
  3. The attacker requests a $999 USDC payout. Two admins approve (the ledger is valid) and it is sent.
  4. The cardholder disputes within 120 days. The `stripe_dispute` overdraft takes the buyer to −$1,000 − fee.
  5. The platform loses $999 plus the Stripe dispute fee.
  - The fee-balance withdrawal cap stops the *direct* path, but not this indirect one. Subscriptions to one's own strategy and profit share work the same way.
- **Fix:**
  - Tag each fee-balance debit with its funding source: spend card credits first, and track a per-user `card_funded_unspent` / `card_funded_spent_on_creators`.
  - Credit creator/referrer earnings that were paid from card-funded balance to `creator:{id}:payable_held` with a maturity date (charge date + 120 days, or `charge.dispute` window end). Release daily.
  - On `stripe_dispute` / `stripe_refund`, claw back from the held creator payable first (a new overdraft kind scoped to that account).
  - Add velocity rules: buyer-to-creator concentration, new card, creator payout shortly after a card-funded sale.
  - Keep the fee-balance rule, but compute withdrawable as `min(spendable, usdc_credits − withdrawals − max(0, spent − card_credits))`, so that spending never unlocks card money.

### H5. Subscriptions of a delisted strategy are reactivated and billed by the next settlement
- **Where:**
  - Delisting sets live subs to `reduce_only` (`store.py:1006-1011`).
  - Settlement treats `reduce_only` as billable (`settlement.py:77,314-316`). `billing.next_status` returns `ACTIVE` as soon as `balance >= due`: with no renewal due that means balance ≥ 0 (`billing.py:217-218`).
  - Signals stop for delisted strategies (`pg.py:454,864`), so exits never run.
- **Exploit/impact:** A strategy priced $100/month is delisted (for example because it is broken or malicious).
  1. At the next 00:30 run every subscriber with a balance ≥ 0 is flipped back to `active`, which re-enables new entries on any signal path that still runs.
  2. At each `current_period_end` the user is charged $100. $97 goes to the creator of a delisted strategy.
  3. Positions are stuck with no exit signal.
- **Fix:**
  - Add a terminal-ish status (e.g. reuse `closing` with `cancel_positions='close'`, or a new `strategy_delisted`) that settlement never bills or reactivates.
  - Delisting should move subs to `closing` (flatten) or to `paused_user` plus a mandatory user alert. `subscriptions_to_settle` should still settle profit share but skip `_renew_and_update_status` when `st.status IN ('delisted','paused')`.

---

## MEDIUM

### M1. USDC can be withdrawn while profit share is accrued but not yet settled (and the $10 reserve is withdrawable straight after subscribing)
- **Where:** `withdrawals.py:266-277` checks only `spendable >= amount` and the USDC cap. The reserve check exists only at subscribe (`subscriptions.py:184-185`).
- **Exploit:**
  1. The user has $2,000 USDC balance and an active 12% strategy.
  2. At 23:00 the day's realised profit is $20k, so $2,700 of profit share accrues at 00:30.
  3. The user requests a $2,000 withdrawal at 23:05. An admin may approve without seeing the accrual.
  4. The balance ends at −$2,700 and the platform collects $0 (and C1 still pays the creator).
- **Fix:** For users with live subscriptions:
  - Compute `accrued = Σ_subs max(0, cum_pnl + pnl_since(cursor, now) − hwm) × rate` and require `spendable − amount ≥ accrued + reserve × live_subs`.
  - Show `accrued` in the admin payout view.
  - Also block withdrawals while any subscription is `past_due`/`reduce_only`.

### M2. Self-referral controls are inert; tiers can be gamed; referral payouts need no KYC
- **Where:**
  - `api/routers/me.py:64-90`: the device check reads `users.device_fp_hash`, which is **never written** anywhere (grep: only reads). The wallet check can never match across users, because `wallets.master_address` is UNIQUE. The binding may be applied at any time within 30 days via `PATCH /me` (not first-touch at signup).
  - `pg.py:821-840`: active referred users include free SILVER subscriptions ($10 reserve, withdrawable).
  - `withdrawals.py:303-306`: KYC is enforced only for `source=="creator"`.
- **Exploit:**
  1. A user creates alt account B, binds B to their own code, and trades through B. They get 0.01–0.02% of B's notional back: a rebate of 10–20% of the builder fee.
  2. 100 puppet accounts × $10 SILVER subscriptions = Elite (100% of pool) for all real referees.
  3. With H2 the payouts become phantom cash, paid out without KYC.
- **Fix:**
  - Populate `device_fp_hash` from `user_devices` (0008) at sign-up and compare against *all* of the referrer's device hashes.
  - Also compare the HL funding source of the referee's master (the first USDC deposit sender) and IP/ASN hashes.
  - Bind only at account creation (drop the `PATCH /me` path, or limit it to 24h and before any wallet/deposit).
  - Count a referred user as "active" only with a paid subscription or ≥ $X of 30-day notional.
  - Require KYC (or at least a payout cap) for referrer payouts.
  - Stop paying suspended/closed referrers (`PgReferralLookup.referrer_share` should check `r.status='active'`).

### M3. Settlement does not check data completeness: late fills or funding permanently mis-charge
- **Where:** `settlement.py:264-273` (committed version: `:191-199`) settles `(cursor, cutoff]` from whatever is in `fills`/`funding_events`. A fill ingested later only raises `fill_after_settlement` (`jobs_data/fills.py:303-311`), and fills-ingest is bounded (`max_addresses=200`, 5 pages). The runbook's "pause settle-daily" is manual.
- **Impact:** A missed *loss* fill means the HWM is advanced on a too-high cum_pnl, so the user is overcharged. It can never be corrected, because the fill is outside every later window. A missed *profit* fill means revenue is lost.
- **Status at review end:** another agent's uncommitted change to `settlement.py` (`_coverage` / `_missing_data`, `repo.data_coverage`) now defers a subscription until both `fills-ingest` and `funding-scan` cover `min(cutoff, cancelled_at)` + 2 min, and fails closed. That addresses the gating half once `PgSettlementRepo.data_coverage` and the tests land. The late-fill half (fills ingested after their day was settled are still never counted) remains.
- **Fix:**
  - Before settling a subscription, require `job_cursors('fills', addr).cursor_ms >= cutoff_ms` with `state.complete = true`, and the same for `funding`. Otherwise skip that sub (report `profit_share_skipped_data`) and alert.
  - Optionally include late fills in the *next* settlement: select `f.created_at > last_settlement.created_at` instead of `f.time > cursor`, or add a `settled_in` column to fills.

### M4. Payout "sent" / "reject" races can pay twice or record one transfer against two payouts
- **Where:** `admin.py:277-296` allows `reject` in `approved_2`, even after typed data was issued and USDC may already have left. `admin.py:317-347` checks `tx_hash_used` in one transaction and marks sent in another. `withdrawals`/`payouts` have no UNIQUE on `tx_hash` (verified in `pg_indexes`). Typed data can be re-issued without limit.
- **Exploit:**
  1. Admin 1 sends the $5,000 usdSend. Before it is recorded, admin 2 (or a colluding admin) rejects. The hold is released to the fee balance and the user also receives the USDC: **$5,000 double pay**.
  2. Two payouts of the same amount to the same address are marked sent concurrently with one tx hash, so one beneficiary is never paid while the ledger says they were.
  3. Typed data issued twice and signed twice produces two on-chain sends for one payout.
- **Fix:**
  - Add a `sending` status, entered when typed data is issued (record `nonce=time_ms`). Allow only one outstanding nonce. Forbid `reject` from `sending` unless an on-chain lookup proves no usdSend with that nonce exists.
  - `CREATE UNIQUE INDEX … ON withdrawals(tx_hash) WHERE tx_hash IS NOT NULL` (same on payouts), plus a shared `payout_tx_hashes(tx_hash PK)` table inserted in the mark-sent transaction.
  - Match `find_usd_send` on the exact nonce.

### M5. Overdraft is authorised by a free-text `kind`, and payables' non-negativity is not enforced by the DB
- **Where:** `0001_init.sql:482-484,620-622`: overdraft is allowed for *every* non-negative account in any tx whose kind is `profit_share|stripe_refund|stripe_dispute`. `app_api` and `app_executor` can call `ledger_post` with any kind, and have direct INSERT on `ledger_accounts/transactions/entries` (`0002_roles.sql:121,146`). `ledger_accounts_fee_balance_shape` only covers `user:*:fee_balance`. `ledger/service.py:210-216` accepts an existing `creator:*:payable` with `non_negative=false`.
- **Verified in DB:**
  - As `app_api`, `ledger_post('t1b','profit_share', user2 fee_balance +5,000, creator payable −5,000)` succeeded with user2 at **−$5,000**.
  - A `creator:…:payable` inserted by `app_api` with `non_negative=false` was overdrawn to **−$4,000** by a normal `payout_hold`.
- **Impact:** Any bug or SQL-injection in API code that picks a kind or pre-creates an account defeats the balance guard. This is defence in depth for C1/H4.
- **Fix:**
  - Extend the CHECK to `code !~ '^(creator|referrer):[^:]+:payable$' OR non_negative`. Also make `withdrawals:pending`, `payouts:pending` and `suspense:*` non-negative.
  - Make overdraft per (kind, account role): e.g. `profit_share` may overdraw only `user:*:fee_balance`, and only for the `app_executor` role (`current_user`/`session_user` check or a SECURITY DEFINER wrapper). `stripe_*` may overdraw only the fee balance.
  - Revoke direct INSERT on `ledger_transactions/ledger_entries/ledger_accounts` from the app roles. Grant EXECUTE on `ledger_post` (SECURITY DEFINER, owned by app_migrator) and an `ensure_ledger_account` function instead.
  - In `ensure_account`, raise if an existing per-user account has `non_negative=false`.

### M6. Chargebacks, refunds and negative balances do not stop trading promptly
- **Where:**
  - `ledger_ops.apply_debit` (`ledger_ops.py:188-204`) and `webhooks._apply` never re-evaluate billing. The `DebitInstruction` docstring says the API layer MUST.
  - Status only moves at the 00:30 settlement.
  - `executor.py:348` lets `past_due` open positions without checking the grace window.
- **Impact:** After a $1,000 chargeback at 01:00, the user keeps full entries for about 23h, then another 72h of grace, then until the next 00:30. That is up to about 4 days of trading on a debt.
- **Fix:** After any fee-balance debit that leaves the balance < 0 (refund, dispute, profit share), set the user's billable subs to `past_due`, or directly to `reduce_only` for disputes, in the same transaction. Use `entries_allowed()` in the executor (see H3).

### M7. Treasury reconciliation and hash-chain coverage gaps
- **Where:** `execution/reconcile.py:129-148`, `0001_init.sql:785-842`.
- **Gaps:**
  - (a) `verify_chain()` is never scheduled, and `chain_heads` is never anchored externally (grep shows no caller). Truncating the newest ledger rows as table owner is undetectable.
  - (b) `builder:hl_receivable` is never relieved: no posting books the builder-reward claim into the treasury. If the builder address equals, or sweeps into, the treasury, `check_treasury` shows a growing false mismatch and real mismatches drown in it.
  - (c) `stripe:clearing` is never reconciled against Stripe balance/payouts, and there is no posting for Stripe payouts to the bank.
  - (d) There is no solvency check `Σ positive fee balances + payables + pending ≤ treasury + clearing + receivable` (negative fee balances are receivables of doubtful value, see C1).
  - (e) `suspense:usdc_unattributed` releases are ad-hoc SQL by ops (RUNBOOK), with no maker-checker or endpoint. The account is not non-negative, so a double release is not stopped.
  - (f) The builder check compares `fills` totals, not ledger `builder_fee` postings, so a posting error is not caught.
  - (g) **UNCERTAIN:** whether `treasury_usdc_micro` includes spot and builder-dex balances. Deposits into `destinationDex="spot"`/`"xyz"` are credited (`hl/deposits.py:24`).
- **Fix:**
  - Add `verify_chain()` plus a signed/published `chain_heads` snapshot to the daily reconcile. Store it in the report and alert on any row.
  - Add a `builder_rewards_claim` posting (debit treasury, credit builder receivable) keyed on the claim tx hash, and a Stripe payout posting (debit bank, credit clearing) from `payout.paid` webhooks.
  - Reconcile the clearing account against Stripe's `balance` API.
  - Add the solvency invariant.
  - Add a maker-checker `suspense_release` admin action (key `suspense_release:{hash}`) and make suspense non-negative.
  - Reconcile `Σ ledger builder_fee` against on-chain rewards.
  - Sum all treasury balances (perp + spot USDC + each dex).

### M8. Billing uses a stale snapshot and live strategy terms
- **Where:**
  - `settlement.py:181` loads all subscriptions once. `_renew_and_update_status` (`:314-346`) charges a renewal from that snapshot. Only `set_status` is guarded by the current status (`pg.py:654-661`); the ledger post is not.
  - Price and profit share are read live from `strategies` (`pg.py:594-612`); subscriptions do not snapshot terms.
- **Exploit/impact:**
  - A user who cancels at 00:30:05, while a long settlement run is in progress, is still charged the renewal a minute later. There is no refund.
  - An admin maker-checker price change on an in-house strategy (e.g. SILVER $0 → $50) is charged to existing subscribers at renewal without re-consent. The subscribe flow's `expected_price` protection is only at creation.
- **Fix:**
  - Inside `uow.atomic()`, run `SELECT … FROM subscriptions WHERE id=:id FOR UPDATE` and re-check `status IN BILLABLE`, `current_period_end` and `cancelled_at IS NULL` before posting.
  - Add `price_monthly_micro` and `profit_share_bps` columns to `subscriptions`, set at subscribe. Settlement uses those. A price change applies only after an explicit re-acknowledgement (or at the next period with notice).

---

## LOW / INFO

| # | Finding | Where | Fix |
|---|---|---|---|
| L1 | `deposits.status='reversed'` is never set (`mark_deposit_reversed` has no caller). A redelivered `payment_intent.succeeded` would flip any reversed row back to `credited` (`ON CONFLICT … WHERE status <> 'credited'`). The ledger is still correct; reporting and `withdrawable_usdc` inputs are wrong. | `store.py:566-591` | Call it from `apply_debit` for full reversals. In the upsert, use `WHERE deposits.status = 'pending'`. |
| L2 | Plan change key `plan:{uid}:start:{plan}:{date}`: free→max ($50) →free→max on the same day replays the key, so no second charge while the response claims `charged_micro=50`. Resetting `period_end` gives no real gain today. | `ledger_ops.py:125`, `me.py:92-119` | Key on a per-change uuid, or reject re-upgrading within the same paid period without a charge. |
| L3 | The HWM advances even when `floor(profit×rate)` = 0 (profit < ~8 micro/day at 13.5%). The leak is ≤ $0.00001/sub/day. | `domain/profit_share.py:246-252` | None needed; document. |
| L4 | The hash chain does not cover `ledger_accounts` (`kind` / `non_negative` / `owner_user_id`). I verified that flipping `owner_user_id` on a payable (after disabling the trigger) leaves `verify_chain()` clean. A flip of `non_negative` on a payable would pass too (for fee balances the CHECK blocked it). | `0001_init.sql:442-452,787-834` | Include `(code, kind, non_negative)` of each account in `entries_digest`, or hash-chain `ledger_accounts`. |
| L5 | `ledger_check_tx` re-sums the full history of each non-negative account up to 3× per post, under a global lock. Throughput will degrade and become a DoS surface at scale. | `0001_init.sql:600-637` | Keep a `ledger_account_balances` row updated in the same transaction (under the chain lock) and check against it. |
| L6 | Referral payouts do not check the referrer's `status` (suspended accounts keep earning). | `pg.py:731-739` | Add `AND r.status='active'`. |
| L7 | `past_due` is tradable until the next 00:30, beyond the 72h grace (up to 96h). | `executor.py:348` | See H3/M6. |

---

## Summary table

| ID | Severity | Title | Primary location |
|---|---|---|---|
| C1 | **Critical** | Creator/referrer payables credited from uncollected (negative-balance) profit share; collusive treasury drain | `execution/settlement.py:279-293`, `0001_init.sql:482-484`, `routers/withdrawals.py:291-327` |
| H1 | High | Profit share avoided via manual trades / pause / "leave" (HL `closedPnl` uses account average entry) | `hl/fills.py:86,182-184`, `execution/executor.py:352`, `routers/subscriptions.py:229-236` |
| H2 | High | Forgeable attribution: prefix-cloid + window fallback + public HMAC key → loss injection and phantom builder-fee payables | `hl/fills.py:182-196`, `execution/pg.py:674`, `hl/client.py:98` |
| H3 | High | Pause/unpause resets billing → free use of paid strategies | `routers/subscriptions.py:229-236`, `domain/billing.py:220`, `executor.py:348` |
| H4 | High | Card funds laundered to USDC via creator/referrer payouts; no dispute clawback | `payments/stripe_pay.py:996-1007`, `api/ledger_ops.py:103-142`, `routers/withdrawals.py:291-327` |
| H5 | High | Delisted strategies' subscriptions reactivated and billed by settlement | `api/store.py:1006-1011`, `execution/settlement.py:314-362` |
| M1 | Medium | USDC withdrawal ignores accrued, unsettled profit share / reserve | `routers/withdrawals.py:266-277` |
| M2 | Medium | Self-referral checks inert (device hash never written, wallet check unreachable), tier gaming, no KYC for referrer payouts | `routers/me.py:64-90`, `execution/pg.py:821-840`, `routers/withdrawals.py:303` |
| M3 | Medium (fix in progress) | Settlement not gated on fills/funding ingest completeness; late fills permanently mis-charge | `execution/settlement.py:264-273`, `jobs_data/fills.py:303-311` |
| M4 | Medium | Payout reject-after-send double pay; `tx_hash` not unique (TOCTOU); unlimited typed-data issuance | `routers/admin.py:277-347`, `0001_init.sql:865-914` |
| M5 | Medium | Overdraft authorised by caller-chosen kind for all accounts; payables' non-negativity not enforced by DB (**exploited in test DB**) | `0001_init.sql:482-484,620-631`, `0002_roles.sql:121,146`, `ledger/service.py:210-216` |
| M6 | Medium | Chargeback/refund/negative balance does not re-evaluate billing; trading continues ~4 days | `api/ledger_ops.py:188-204`, `routers/webhooks.py:149-159` |
| M7 | Medium | Reconciliation gaps: `verify_chain` unscheduled, heads unanchored, builder receivable/claims, Stripe clearing, solvency, suspense release | `execution/reconcile.py:129-148`, `0001_init.sql:785-842` |
| M8 | Medium | Renewal from stale snapshot (charged after cancel); terms not snapshotted per subscription | `execution/settlement.py:181,314-346`, `execution/pg.py:594-612` |
| L1 | Low | `deposits.reversed` never set; redelivery can flip it back | `api/store.py:566-591` |
| L2 | Low | Same-day plan re-upgrade replays the idempotency key (free, misreported charge) | `api/ledger_ops.py:125` |
| L3 | Info | HWM advances on zero-rounded charge (negligible) | `domain/profit_share.py:246-252` |
| L4 | Low | Hash chain does not cover `ledger_accounts` attributes (verified) | `0001_init.sql:442-452` |
| L5 | Low | Balance check re-sums full account history under a global lock (scaling/DoS) | `0001_init.sql:600-637` |
| L6 | Low | Suspended referrers still earn | `execution/pg.py:731-739` |
| L7 | Low | `past_due` grace enforced only at daily settlement | `execution/executor.py:348` |

**Suggested order:** C1 → H2 (oid matching) → H1 → H3/M6 (billing gate) → H4 → H5 → M5 (DB guards) → M1 → M4 → M3 → M7 → M2 → M8 → Lows. Payouts are off during the internal phase (SPEC §12), so C1/H4/M4 cannot be cashed out until the owner says "public". Fix them before that switch.

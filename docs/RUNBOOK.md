# aijalon.trade — Operations Runbook

Version: 2026-09-30 · Owner: ops lead [●] · On-call rota: [link] · Ops chat: Telegram `[ops chat]`
Related: `docs/SECURITY.md`, `docs/INCIDENT_RESPONSE.md`, `docs/GO_LIVE_CHECKLIST.md`

> **Status note.** The procedures below follow SPEC v1 and the executor/risk code as of this version. Commands marked **[VERIFY]** must be tested in staging and replaced with the exact project, instance and service names before go-live. **Every procedure that moves money or lifts a safety control is maker-checker: two different admins.** Engaging a safety control never needs a second person. **When in doubt, pause first.**

Conventions:
- **UTC everywhere.**
- `$PROJECT` = the GCP project id; region `asia-southeast1`; Cloud SQL instance `$SQL`; Cloud Run services `api`, `executor` and `sandbox`.
- Every manual action gets an audit-log entry with a reason. The admin console does this for you. Direct DB access is break-glass only (§11).

---

## 1. Daily operations checklist

The on-call person does this, ideally after 01:00 UTC.

1. **Signals** (`/internal/ingest-signals`): today's feed was received, its signature is valid, and it is not stale (≤ 36 h). Check the listed strategies' `as_of`. If it is missing or invalid, see §12.
2. **Settlement** (`/internal/settle-daily`, 00:30 UTC): the job succeeded. There is one ledger transaction per active subscription for today's `settle_date`. Look for no duplicate-key errors, and any failures. The pre-settlement data jobs succeeded too: `fills-ingest` 00:00–00:25 (incl. `fills-ingest-presettle`) and `funding-scan` 00:07. **No `fill_after_settlement` event** (§13.4).
3. **Reconciliation report** (admin → Reconciliation):
   - Σ builder-fee ledger vs Hyperliquid builder rewards;
   - treasury USDC vs `treasury:hl_usdc` ledger;
   - Stripe balance vs `stripe:clearing`;
   - positions vs expected.

   Any delta **> $1** means §6.
4. **Executor health:**
   - ticks ran every minute (no gaps > 5 min);
   - error rate;
   - guard-rejection counts by reason (`stale_data`, `liquidity_cap`, `mark_oracle_deviation`…);
   - open circuit breakers.
5. **Alerts console:** every critical alert is acknowledged; warns are reviewed; active pauses and kill switches are listed, each with an owner and a next-review time.
6. **Billing:** count of `past_due` / `reduce_only` subscriptions; failed Stripe webhooks (Stripe dashboard → Webhooks); deposits not credited > 1 h.
7. **Payout queue:** requests waiting for approval; address-change holds.
8. **Security:**
   - admin logins;
   - MFA resets;
   - new-country logins;
   - KMS decrypt volume vs baseline;
   - IAM changes (Cloud Audit Logs alert).
9. **Data jobs and alert delivery** (§13): Scheduler shows no failing job; no new `candle_mismatch` (§13.5); `deliver-alerts` has no growing `retry`/`failed` backlog; count of users with a lapsed Telegram link (§13.2); agents expiring within 3 days on subscriptions with open positions (§13.1); balance of `suspense:usdc_unattributed` and every open `topup_held` event (§13.3).
10. Record "Daily check done, [anomalies]" in the ops log.

**Weekly:** review guard-rejection trends per market (thin HIP-3 markets); check Hyperliquid announcements (delistings, parameter changes, HIP-3 deployer notices); test restore a PITR clone (monthly, §8).

## 2. Dashboards (Cloud Monitoring) [DESIGN]

| Dashboard | Key panels |
|---|---|
| Trading | Ticks per minute; tick duration; orders placed / filled / rejected; guard rejections by reason; notional per market vs the 24h-volume and OI caps; open circuit breakers; active flags |
| Markets | Mark vs oracle deviation; OI 1h change; funding for each traded market; aggregate user exposure per market |
| Money | Fee-balance total (liability); past-due count; settlement status; reconciliation deltas; treasury USDC; payouts pending |
| Security | Auth failures; step-up events; MFA resets; admin actions; KMS encrypt/decrypt counts per SA; Cloud Armor / Cloudflare blocks |
| Platform | Cloud Run errors and latency; Cloud SQL CPU, connections and replication; Scheduler job failures |

Alerting: critical goes to the Telegram ops chat plus email plus a phone page [●]. Warn goes to the Telegram ops chat.

## 3. Safety controls: what each flag does

The executor reads `system_flags` once per tick. See the `backend/app/domain/risk.py` contract.

| Flag | Effect | Who sets it | Who lifts it |
|---|---|---|---|
| `new_entries_paused` | **Reduce-only everywhere:** exits allowed, no new or increased positions | Any admin | Maker-checker |
| `new_entries_paused:{coin}` | Reduce-only on that market. **Set automatically by critical market alerts.** | Automatic / any admin | Maker-checker (admin API `POST /v1/admin/flags`, key pattern accepts `new_entries_paused:{coin}` incl. builder-dex coins such as `new_entries_paused:xyz:SILVER`) |
| `kill_switch_market:{coin}` | **Hard stop on that market: no orders at all, including exits** | Any admin | Maker-checker |
| `kill_switch_global` | **Hard stop everywhere: the tick is skipped** | Any admin | Maker-checker |
| Per-subscription circuit breaker | No orders for that subscription after 3 consecutive exchange rejections | Automatic | Maker-checker |

**Choosing a flag.**
- Suspected **manipulated market or oracle**: use `kill_switch_market:{coin}`. Exits at a manipulated price may be worse than holding. Decide on flattening deliberately.
- Suspected **agent-key or executor compromise**: use `kill_switch_global`.
- **Uncertain but not urgent** (for example, an unexplained drawdown): use `new_entries_paused`.

## 4. Engaging and lifting a kill switch

### 4.1 Engage (one admin, immediately)

1. Admin console → Flags → set the flag (for example `kill_switch_market:xyz:SILVER` = true). Give a reason. Step-up is required.
2. **Fallback if the console or API is down** [VERIFY]: pause the executor schedule so no ticks run:
   `gcloud scheduler jobs pause tick --location=asia-southeast1 --project=$PROJECT`  (or `make pause-all` to stop every job)
   Then set the flag as soon as the API is back. Record the manual action in the ops log.
3. Post in the ops chat: flag, time, reason, and incident id (open an incident if SEV2 or higher).
4. User notice: the in-app banner shows automatically for flagged markets [DESIGN]; see INCIDENT_RESPONSE §5.

### 4.2 Flattening positions during a hard stop (deliberate)

If positions must be closed while a kill switch is on:
- **Preferred:** move to reduce-only. Lift `kill_switch_market:{coin}` **and at the same time** set `new_entries_paused:{coin}` (maker-checker). The next ticks then only reduce exposure, and all guards still apply.
- If the guards themselves block the exits (for example, the oracle deviation stays > 2%), **do not** disable guards in production. Escalate to the Incident Commander. Options are: wait; tell users to close manually on Hyperliquid (they always can); or a reviewed code or config change through the 2-person rule.

### 4.3 Lift (maker-checker)

1. **Maker** (Admin A) opens a "lift" request with:
   - evidence the cause is resolved (market data normal for ≥ [2 h]; oracle deviation < 0.5%; OI stable; venue or deployer statement if relevant);
   - an exposure review;
   - the expected first-tick behaviour (how many orders, and what notional).
2. **Checker** (Admin B ≠ A) reviews and approves in the console (step-up). The API refuses if the checker equals the maker.
3. Lift **gradually**: move from kill switch to `new_entries_paused` first, observe [1–2] ticks, then lift fully.
4. Watch the next ticks live. Close the incident, or record in the ops log.

## 5. Payouts and fee-balance withdrawals (hardware wallet)

Schedule: creator and referrer payouts [monthly, on day ●]; user withdrawals on request (target ≤ [5] business days).

1. **Prepare** (system): a batch is built from payables minus holds (KYC incomplete — creators AND referrers: `POST /payouts` and both admin approvals refuse a `payout` whose beneficiary's KYC is not `approved`, reason `kyc_required`; investigation, reconciliation mismatch, address-change hold ≤ 48 h, card-funded earnings inside the 120-day dispute window). Each item shows the beneficiary, amount, `to_address` and ledger reference.
2. **Pre-checks** (maker):
   - today's reconciliation is clean;
   - treasury USDC ≥ batch total plus buffer;
   - addresses verified (signed message) and not changed within the hold window;
   - no sanctions or suspension flags;
   - amounts match the ledger.
3. **Approve 1** (maker, Admin A, step-up), then **Approve 2** (checker, Admin B ≠ A, step-up). Status moves `requested → approved_1 → approved_2`.
4. **Sign** (an officer holding the hardware wallet, from the admin console in the browser):
   - connect the hardware wallet; the console builds a `usdSend` for each item;
   - **check the destination and amount on the device screen** against the console, character by character for the first and last 6 characters of the address;
   - for a **new address**, send a small test amount first and have the beneficiary confirm it.
5. **Record:** the tx hash is saved; status `sent`; the ledger entry is posted (idempotent on payout id).
6. **Verify:** the next reconciliation shows the treasury decrease equal to the sent total.
7. **Limits** [CONFIRM values]:
   - per-transaction cap $[●];
   - daily cap $[●];
   - above the caps, a third approval, or a split across days.
8. **Never:**
   - type the seed phrase into a computer;
   - sign a payload you cannot read on the device;
   - approve your own request.

## 6. Reconciliation mismatches

| Mismatch | Likely causes | Steps |
|---|---|---|
| Builder-fee ledger ≠ Hyperliquid builder rewards | Fill attribution lag; fills without our builder code; fee-field semantics ([VERIFY] SPEC §1.1); a missed fill ingest; **the on-chain reader itself** — `builderRewards` (info `referral`) + `rewardsClaim` ledger updates are UNVERIFIED field names until the go-live check (GO_LIVE Gate B "Mainnet verification") | Re-run fills ingest for the window (idempotent on `tid`); compare by coin and day; check `builder_fee_micro` vs on-chain; compare the reader's figure with the Hyperliquid UI for the builder address. **If unexplained after 24 h, SEV3.** |
| Treasury USDC ≠ ledger | Deposit not credited or double-credited; payout sent but not recorded; external transfer | Scan `usdSend` history for the treasury; match by tx hash to `deposits` and `payouts`; **hold all payouts** until resolved. **Any unexplained outflow is SEV1** (possible key compromise). |
| Stripe balance ≠ `stripe:clearing` | Webhook missed or failed; refund or dispute not posted; processor fee mismatch (fees are passed to users; the credit must equal net); fees ABSORBED by the platform (`stripe_fee_absorbed`) and dispute fees are not in the ledger | Reconcile books every Stripe payout itself (`stripe_payout` / `stripe_payout_reversal`, keyed on the balance transaction; migration 0015) and compares clearing with Stripe available + pending — needs a Stripe key on the executor (else `stripe_clearing_status = not_configured`) and a USD settlement currency (`STRIPE_SETTLEMENT_CURRENCY`, default `usd`; any other → `unsupported_currency` + warn `stripe_clearing_unsupported_currency`, nothing booked: an FX policy is an owner decision). Stripe dashboard → resend failed events (idempotent); post refunds and disputes; check the net-of-fee calculation. `stripe_payout_unbooked` (warn): a payout reversal carrying a fee — book it by a reviewed correction. |
| Builder receivable ≠ on-chain unclaimed rewards | Rewards claimed but not into the treasury (`builder_claims_status = outside_treasury`: the builder address is not the treasury — claimed USDC must be swept and booked by a reviewed correction); fills not yet recognised by settlement (already added: `builder_unrecognised_micro`); UNVERIFIED reader field names (`HL_REWARDS_*` env, see app/execution/treasury_books.py) | Reconcile books each `rewardsClaim` of the builder=treasury account (`builder_rewards_claim`: treasury ← receivable, key `builder_claim:{hash}:{time}`) before comparing. Check the claim history of the builder address; if the field names are wrong, fix the `HL_REWARDS_*` settings, never the ledger. |
| Positions ≠ expected | User traded manually on the same account; partial fills; skipped deltas; liquidation | Check the user's fills; if it is a user action, note it on the subscription (it may affect PnL attribution). Liquidations: alert the user. |

Corrections are **new ledger transactions** (never edits), with memo and approver (maker-checker for any correction > $[10]).

## 7. Rotating agent keys and KMS keys

### 7.1 Single user (user-initiated or support-assisted)

The user chooses "Rotate agent" (step-up). The API generates a new key and encrypts it with KMS. The user signs `approveAgent` with the same name `aijalon`, which should replace the old agent [VERIFY on mainnet]. The server confirms on-chain via `extraAgents` and marks the old key `rotated`. If the old agent is still listed, ask the user to revoke it.

### 7.2 Suspected mass compromise of agent keys

1. `kill_switch_global` now. Open a SEV1.
2. Revoke the executor SA's KMS decrypt permission (so no further decrypts) [VERIFY command]:
   `gcloud kms keys remove-iam-policy-binding agent-keys --keyring=aijalon --location=asia-southeast1 --member=serviceAccount:executor@$PROJECT.iam.gserviceaccount.com --role=roles/cloudkms.cryptoKeyDecrypter`
3. **We cannot revoke agents on users' behalf** (that requires their master wallet). Notify all users (INCIDENT_RESPONSE §5, template C). Tell them to revoke the `aijalon` agent on Hyperliquid and to review their positions.
4. After root cause and fix: generate new keys per user, **with new user approvals**. Old keys are marked `revoked`. Restore the IAM binding through maker-checker.

### 7.3 KMS key-version rotation (routine)

Automatic rotation creates a new primary version. New encryptions use it. Old ciphertexts still decrypt with their recorded `kms_key_version`. Re-wrap old ciphertexts via a job [DESIGN]. **Never disable or destroy a version while any row references it.** Check first:
`SELECT kms_key_version, count(*) FROM agent_keys WHERE status='active' GROUP BY 1;`

## 8. Restoring the database from PITR

Targets: RPO ≤ 5 min, RTO ≤ 4 h (see DATA_PROTECTION §6).

1. **Stop writers:** `kill_switch_global`; pause every Scheduler job (`make pause-all`; job list: DEPLOY §14.1). Set the API to maintenance mode (read-only) [DESIGN].
2. **Choose a timestamp** just before the corruption or incident (from the audit log or Cloud Logging).
3. **Clone** to a new instance (never overwrite the original — it is evidence) [VERIFY flags]:
   `gcloud sql instances clone $SQL $SQL-restore-YYYYMMDDHHMM --point-in-time='2026-10-01T03:14:00Z' --project=$PROJECT`
4. **Verify the clone:**
   - ledger hash chain intact;
   - Σ=0 per transaction;
   - audit chain intact;
   - row counts are plausible;
   - latest `settle_date`.
5. **Re-ingest external truth** (all idempotent):
   - fills (`tid`), funding, deposits (tx hash) from Hyperliquid for the gap;
   - Stripe events (resend from the dashboard for the gap);
   - signals.
6. **Re-run reconciliation.** Differences must be explained before resuming.
7. Point the services at the restored instance (update the connection secret or config through the pipeline), with maker-checker.
8. Resume: Scheduler jobs, then lift `kill_switch_global` → `new_entries_paused` → normal (§4.3).
9. Keep the old instance for [30] days for forensics. Write a post-mortem.

## 9. Hyperliquid outage or degradation

**Symptoms:** API errors or timeouts; stale data (`stale_data` rejections spike); chain halt announced.

1. The executor fails closed automatically (data older than 60 s means no orders). Confirm on the dashboard.
2. Set `new_entries_paused` to avoid a burst of entries at recovery. Post in the ops chat. Show an in-app banner if it lasts over [30 min].
3. Monitor Hyperliquid status and announcements.
4. **On recovery:**
   - check that data is fresh;
   - reconcile positions vs expected;
   - review signals that changed during the outage. **Do not blindly catch up**: the executor targets the *current* signal, so a missed entry will be taken late at the current price. The IC decides whether that is acceptable, or whether to wait for the next bar.
   - lift via maker-checker.
5. If the outage was > [72 h], check the refund-policy pro-rata rule (Refund Policy §3.2). A Hyperliquid outage is not our fault, so no automatic credit applies. Record the decision.

## 10. Oracle or market incident on a HIP-3 market (e.g. `xyz:SILVER`)

**Triggers:** mark/oracle deviation alert; OI spike; funding spike; deployer halt; price disconnected from the reference market (for example, silver spot); community reports of manipulation.

1. **Auto-pause** will usually already have set `new_entries_paused:{coin}`. If manipulation is plausible, **escalate to `kill_switch_market:{coin}`** now (one admin).
2. Open an incident (usually SEV2; SEV1 if large user losses or liquidations are in progress).
3. **Assess:**
   - aggregate subscriber exposure on the coin;
   - distance to liquidation for the largest positions;
   - reference-market price vs oracle vs mark;
   - deployer and validator announcements.
4. **Decide on exits.** Choose one of:
   - (a) hold (kill switch stays on);
   - (b) controlled reduce-only (§4.2);
   - (c) advise users to manage positions themselves.

   Record the reasoning. **Two admins agree** on (b).
5. **Communicate** to affected subscribers (template B) and post updates at least every [2 h] while the incident is active.
6. **Forced settlement or delisting:**
   - ingest the settlement fills;
   - reconcile PnL;
   - **hold profit-share settlement** for affected subscriptions, pending review (maker-checker), so fees are not charged on distorted PnL;
   - mark the market delisted in config (the guard blocks it);
   - move affected strategies to `paused` and notify their creators.
7. **Resume** only after the conditions in §4.3 hold, and a post-mortem with a decision on whether to keep listing the market.

## 11. Break-glass access

Direct production DB or console access is only for SEV1/SEV2, when the tools are unavailable.
- Request in the ops chat with the incident id.
- A second admin approves.
- Use time-bound IAM elevation; every command is logged.
- Revoke the access afterwards.
- Review in the post-mortem.

**Ledger, audit and consents stay append-only even for break-glass.** Corrections are new rows.

## 12. Other common procedures

- **Signal feed stale or bad signature:** the strategy holds (no trades). Check the terminal GitHub Action run; confirm with the terminal owner. **Never** override signature verification. If the key has rotated, follow SECURITY §4 (dual-verify, then pin via maker-checker).
- **Stripe webhook failures:** fix the cause; resend events from the Stripe dashboard (idempotent). Credited amount = gross − Stripe fee.
- **USDC deposit not credited:** get the tx hash from the user; check that the sender is their verified master address and the destination is the treasury. The user's "I've sent it" (POST /deposits/usdc/confirm) queues a scan request that the next `deposits-scan` run serves FIRST (§13.9); check `SELECT requested_at, since, served_at FROM deposit_scan_requests WHERE user_id = …` — `served_at` NULL for > 15 min means the runs are failing or out of HL budget (§13.8). Then run the deposits scan (idempotent). If the sender was unverified (or the amount below the minimum) the transfer was booked to `suspense:usdc_unattributed` — release it per §13.3 (maker-checker).
- **User reports unexpected trades:**
  1. check that the orders carry our `cloid` prefix;
  2. if ours, find the signal and guard trail;
  3. if not ours, it was manual or another agent — tell the user;
  4. **if ours but unexplained, pause the subscription and treat it as a possible SEV2.**
- **Admin account suspected compromised:** remove the admin role (the other admin); revoke sessions (Firebase: revoke refresh tokens); rotate that admin's hardware keys; review the audit log for their actions; SEV1 if any money or flag action looks suspicious.
- **Creator script failing in the sandbox:** the circuit breaker pauses the strategy; notify the creator; the admin reviews.

## 13. Data jobs, alerts and held funds

Scheduler job list, schedules and ordering: DEPLOY §14.1 (`SCHEDULER_SPEC` in `infra/gcp/env.sh`). Every job is idempotent; re-run one with `gcloud scheduler jobs run <job> --location=asia-southeast1`. A failing job: read its executor log (`jsonPayload.job`), fix the cause, run it again — do not skip it.

### 13.1 Agent approval expiring / expired / revoked

`agent-expiry-scan` (every 6 h) reads `extraAgents` per master address and raises user alerts `agent_expiring` (14, 7, 3, 1 days left), `agent_expired` (critical) and `agent_revoked` (critical, after 2 consecutive scans without the agent). These are mandatory kinds (Telegram + e-mail, cannot be muted).
- **An expired or revoked agent cannot place any order, not even a reduce-only exit.** The executor stops trading those subscriptions; their open positions stay open and unmanaged until the user re-approves (new agent via the site) or closes them on Hyperliquid.
- Daily: list active subscriptions whose agent expires within 3 days **and** that hold a position; contact those users directly (support e-mail) in addition to the automatic reminders.
- A sudden batch of `agent_revoked` events for many users at once is more likely a reader or Hyperliquid API problem than real revocations: check `extraAgents` by hand for two affected masters before telling anyone; one scan miss only raises an ops event.
- Never re-approve on a user's behalf (it needs their master wallet).

### 13.2 Telegram unreachable

User side: a Telegram 403 / "chat not found", or the user blocking/stopping the bot, marks the link **lapsed** and queues the mandatory `telegram_unreachable` alert (e-mail). **After 24 h without a working link, new entries pause for that user's subscriptions** (exits continue). The user fixes it at **#/alerts** (re-link; unblocking the bot re-activates the link). Nothing for ops to do per user.

Platform side (many users lapse at once, or nobody receives Telegram alerts):
1. `getWebhookInfo` (DEPLOY §10.1 — read the token from Secret Manager, never print it): `last_error_message`, `pending_update_count`, URL, `allowed_updates`.
2. Bot token revoked or regenerated in BotFather → `add_secret TELEGRAM_BOT_TOKEN`, redeploy api + executor, `setWebhook` again.
3. Webhook 401s → `TELEGRAM_WEBHOOK_SECRET` differs from what was registered: run `setWebhook` with the current secret.
4. Webhook 403 at the edge → Telegram's IP ranges changed: update the allow-list in `infra/cloudflare/dns.sh` and re-run `make dns`.
5. Ops group silent → the bot was removed from the group, or the group was upgraded to a supergroup (its chat id changes to `-100…`): re-add the bot / `add_secret TELEGRAM_OPS_CHAT_ID` with the new id, redeploy. Ops alerts still reach `OPS_EMAILS` by e-mail meanwhile.
6. If Telegram is down for > 24 h platform-wide, the automatic entries pause will hit every user: decide (IC) whether to accept it; do **not** disable the gate in code.

### 13.3 Held USDC deposits: `suspense:usdc_unattributed` release (maker-checker)

`deposits-scan` books a treasury transfer it cannot credit (sender not a verified wallet, amount below the minimum, …) **once**: debit `treasury:hl_usdc` / credit `suspense:usdc_unattributed` (kind `deposit_held`, key `usdc_hl:{hash}`), records the on-chain sender in `usdc_held_deposits`, and raises the ops event `topup_held` (and the user's `topup_held` alert when the sender is known). A later wallet verification does **not** credit it automatically (the key is taken). Release it in the admin console, **Admin → Held deposits** (API: `/v1/admin/held-deposits…`, API_CONTRACT):
1. **Maker** (Admin A, step-up): identify the owner and record the evidence (ops-log reference) in the proposal:
   - **Attribute** — only to the user whose **verified wallet is the sending address** (they prove control by the normal wallet verification, a signed message; the server refuses otherwise). You cannot attribute to yourself.
   - **Refund** — back to the on-chain sender (recorded by the scan; for transfers held before that existed, enter the sender from the explorer — the server verifies it against the transfer on-chain).
2. **Checker** (Admin B ≠ A, step-up; never the beneficiary) reviews the evidence and approves (or rejects — the transfer is then open for a new proposal). Approval posts **one** ledger transaction, key `suspense_release:{hash}`: attribute → `user:{id}:fee_balance` (plus a withdrawable USDC `deposits` row and the user's `topup_credited` alert); refund → `refunds:usdc_pending`.
3. **Refund only — send:** an officer with the treasury hardware wallet clicks *Sign & send refund*: the console builds the `usdSend` to the recorded sender for the held amount; check destination and amount **on the device** (§5 step 4). The tx hash is found automatically (or pasted), verified on-chain by the server, and posted: `refunds:usdc_pending` → `treasury:hl_usdc` (key `suspense_refund:{hash}:sent`). Refunds are not blocked by `PAYOUTS_ENABLED` (it is the sender's own money); they still need two admins + the hardware wallet.
4. Never improvise SQL (the ledger is hash-chained and append-only; `suspense_releases` rows cannot be re-opened or edited — DB trigger).
5. Daily: `suspense:usdc_unattributed` balance (shown on the tab) = Σ open held transfers; `refunds:usdc_pending` = Σ approved refunds not yet sent. Any unexplained balance is a reconciliation mismatch (§6).

### 13.4 `fill_after_settlement` (warn)

Meaning: `fills-ingest` stored one of our fills whose time is at or before the subscription's `pnl_cursor` — its day was already settled. Since REVIEW_MONEY M3 (0010) nothing is lost: settlement CLAIMS every not-yet-settled fill (`fills.ps_settlement_date`), so the fill is booked into the **next** settlement (a day late for the HWM, never dropped).
1. Find the fill (`fills` by `tid`), subscription, day and `book_pnl_micro`; check why it arrived late (Scheduler history of `fills-ingest` / `fills-ingest-presettle` before 00:30, Hyperliquid outage, `fills-ingest` errors).
2. Check the next settlement consumed it (`fills.ps_settlement_date` set). No ledger correction is needed.
3. Prevent a repeat: `settle-daily` now defers any subscription whose address `fills-ingest` / `funding-scan` have not synced past the cut-off (§13.7), so this event means a fill arrived later than the job's own sync said was complete (Hyperliquid indexing lag, or a cursor written by a run that missed data). If `fills-ingest` is failing or behind (cursor older than a few minutes), **pause `settle-daily` and `settle-daily-retry` until it has caught up**, then run `settle-daily` by hand (idempotent; it settles yesterday by default). For an older missed day the executor is not reachable from outside, so set the date on the job temporarily: `gcloud scheduler jobs update http settle-daily --location=asia-southeast1 --message-body='{"settle_date":"YYYY-MM-DD"}'` → `gcloud scheduler jobs run settle-daily …` → set `--message-body='{}'` back (verify with `describe`). Settle missed days in date order.

### 13.4a Money-core events (REVIEW_MONEY C1, H1, H2, M7 — migration 0010)

- `profit_share_uncollected` (warn, settlement): a profit-share charge exceeded the user's balance. Only the collected part reached the creator payable / platform revenue; the rest is in `ps_pending:{user}:{creator|platform}` (never payable, `payout_hold` from it is refused by the DB) and is released automatically when the user tops up (and by the daily sweep). Many of these on one creator from users who never top up = the C1 collusion pattern: review the creator before approving payouts. Writing off a pending amount is a reviewed ledger transaction, never SQL.
- `foreign_trade_on_strategy_coin` (warn): a fill on a strategy coin in a subscription's trading account that is not one of our orders (manual trade, other app, forged `0xa17a1000` cloid). The subscription's book was marked to market at that price (`subscription_pnl_events`, kind `foreign_fill`) and profit share is charged on it at the next settlement. Repeated on one user = profit-share avoidance attempts (REVIEW_MONEY H1); the user got `foreign_trade_detected`.
- `fill_oid_mismatch` (critical): a fill carries the cloid of one of our recorded orders but another exchange oid — a forged order. It was NOT stored or attributed. Check the order (`orders.cloid`, `oid`), the user, and whether `CLOID_SECRET` may have leaked (rotate only with no unresolved orders).
- `fill_unattributed` (warn): a platform-prefixed fill with no recorded order. It is never attributed by time window any more (REVIEW_MONEY H2); `window_subscription_id` is only a hint. Our crash-window orders are recorded before sending, so this is normally a user's own prefixed order.
- `solvency_shortfall` (critical, reconcile): on-chain treasury USDC + builder receivable + Stripe clearing < Σ positive fee balances + payables + pending + withdrawals/payouts/refunds pending + suspense. SEV2: stop payouts, find the cause (unrecorded outflow, un-swept builder rewards, reconciliation read error).
- Ledger posting lockdown (migration 0015, REVIEW_MONEY M5): the api / executor logins cannot INSERT into the ledger; every posting goes through `ledger_post_as(role, …)` and must match a row of `ledger_posting_rules` for (role, kind) — key pattern, debit / credit accounts, entry counts — recorded in `ledger_tx_authorizations` (role, login, rule id). A refused posting is SQLSTATE AJ403 in the service logs: a code path posting a new kind or shape needs a NEW rule in a migration (the table is append-only), never a grant. Break-glass corrections are posted by the migrator login through `ledger_post` (owner rule 901, recorded with the login). The treasury balance reconcile compares now sums perp + spot USDC + every trusted builder dex (`treasury_breakdown` in the report, M7(g)).
- `ledger_chain_anchor` (warn, daily 03:40, ops email) + the ops Telegram message: today's chain heads (ledger transactions, audit log, ledger accounts). Keep them: they are the external anchor. `ledger_chain_broken` (critical): `verify_chain()` or `verify_chain_anchors()` found a problem (row altered, gap, account attributes changed, running balance ≠ Σ entries, anchored head missing/changed = truncation or rewrite after anchoring) → SEV1, freeze payouts and admin money actions, preserve the database, compare with the anchors sent to Telegram/email.

### 13.5 `candle_mismatch`

`candles-sync` re-fetched a closed candle that differs from the stored one. Stored closed candles are immutable: the new value is **not** written. Stored candles feed **creator signals in the tick** (stored first, the API only for missing bars — `app/execution/jobs.py _bars_for`), backtests and the listing-history rule, so a wrong stored candle can change a live creator signal.
1. Compare the stored row, the API's current value and the Hyperliquid UI for that coin/interval/open time.
2. A one-off venue correction: note it in the ops log; leave the stored candle (it is what backtests recorded) unless the review decides otherwise — any change is a reviewed migration, never an in-place edit.
3. If the stored value is wrong and a listed creator strategy trades that coin/interval: `new_entries_paused:{coin}` (one admin) while the review decides; check the strategy's recent signals for that bar.
4. Many mismatches at once (a whole interval or coin): suspect our code (e.g. a candle stored before it closed) or an API change — SEV3, pause `candles-sync`, fix, then resume.

### 13.6 First-sync candle backfill

The first `candles-sync` run backfills ~5,000 candles per series across every perp market at 1h/4h/1d; each call is bounded, so the backfill spreads over **~40 calls (~7 h)**. Until it finishes, backtests fall back to the API and a strategy's "history days" may read low. Listing needs ≥ 180 days on every market (SPEC §12).

### 13.7 `settlement_deferred` (warn) and the `settle-daily-retry` slots

Meaning: at the 00:30 `settle-daily` run, `fills-ingest` and/or `funding-scan` had not completely synced one or more subscriptions' trading addresses past the PnL cut-off (00:00 UTC) + 2 min (`job_cursors`: a complete run counts up to its run time, an incomplete one up to its cursor). Those subscriptions were **deferred**: no profit-share posting, no cursor move and no renewal / billing-status change for them. Everything else settled normally. One event per settle date (the payload lists up to 20 subscription ids and which job was missing); the executor log line `settlement_deferred` has every one.
1. Nothing to do if the data jobs are healthy: `settle-daily-retry` (02:30 and 06:30 UTC, same route, body `{}` = yesterday) settles them once the jobs have caught up. The retry is a no-op for everything already settled.
2. Check the missing job's Scheduler history and executor logs (`jsonPayload.job` = `fills-ingest` / `funding-scan`): errors, deadline misses, Hyperliquid 429s (shared rate budget, §13.8) or an outage (§9). Fix the cause and run the job by hand (`gcloud scheduler jobs run fills-ingest …`), then `gcloud scheduler jobs run settle-daily-retry --location=asia-southeast1`.
3. Still deferred after 06:30: no money is lost — the next day's 00:30 run settles both days in one posting (PnL is summed from the subscription's own `pnl_cursor`), but the user's profit share, renewal and past-due status are a day late. Fix the data job the same day; never settle by editing `job_cursors` by hand.
4. `missing` lists an address that is no longer tracked (a subscription cancelled > 7 days ago, still unsettled): the data jobs no longer scan it; a cancelled subscription only needs coverage up to its `cancelled_at`, so this means the data never reached that point — SEV3, investigate before touching anything.
5. `settle-daily-retry` was added after go-live on existing projects: `bootstrap.sh scheduler` creates it **paused**; resume it by name (`gcloud scheduler jobs resume settle-daily-retry --location=asia-southeast1`) once `settle-daily` itself is live.

### 13.8 Hyperliquid rate budget (`hl_rate_budget`)

Every Hyperliquid `/info` call from the executor (tick, data jobs, reconcile) charges one shared per-egress-IP counter in Postgres (`app/hl/budget.py`): `HL_BUDGET_WEIGHT_PER_MINUTE` (default 800; Hyperliquid's own limit is ~1200/min/IP, UNVERIFIED) with `HL_TICK_RESERVE_PER_MINUTE` (300) kept for the tick. The tick is always charged and never blocked; data jobs and reconcile wait for the next minute when their share is spent and stop cleanly at their deadline (cursors resume next run). Request weights are code config (`app.config.HlLimits`, UNVERIFIED — check against Hyperliquid's docs).
- Symptoms of pressure: executor log `hl_budget_wait` (jobs backing off), `hl_budget_over` (the tick alone exceeded the budget), data jobs ending with `remaining` > 0 several runs in a row, `settlement_deferred` (§13.7), Hyperliquid 429s (`hl_info_retry` status 429).
- Current usage: `SELECT slot, window_start, spent_tick, spent_jobs FROM hl_rate_budget ORDER BY window_start DESC LIMIT 10;` (read-only; never edit rows by hand).
- Too little room for jobs (candles backfill, many subscribers): raise `HL_BUDGET_WEIGHT_PER_MINUTE` carefully (stay well below Hyperliquid's limit) or lower the tick reserve; redeploy the executor. If the budget table is unreachable the calls are allowed (fail open, logged `hl_budget_unavailable`).

### 13.9 Clean-up round (migration 0014): deposit scan requests, relay budget, untrusted dexes, strategy pause, card-held releases, referrer KYC

- **Deposit scan requests.** `deposits-scan` (every 5 min) first serves the oldest pending `deposit_scan_requests` (≤ 20 users per run, ≤ 5 verified wallets each): it reads each wallet's OWN ledger from the request's `since` (clamped by the API to ≥ now − 48 h and ≥ wallet verification) and books transfers to the treasury through the normal credit/hold path (same `usdc_hl:{hash}` key — never twice). Every read is charged to the shared HL budget; with no room the request stays pending (`scan_requests_deferred` in the job report) and is served next run. `served_at` is set only when all of the request's transfers were booked and the user did not re-request meanwhile. Requested-wallet reads never move the treasury cursor.
- **`/hl/exchange-relay`** now charges the API's shared HL budget (egress key `api`, weight `HlLimits.exchange_weight` = 1) before forwarding; when the minute is spent the user gets 502 "hyperliquid rate budget exhausted" and nothing is sent. Many of these = user traffic spike or a loop — check `hl_rate_budget` rows for the `api` key (§13.8).
- **`creator_signal_untrusted_dex` (critical).** A listed creator version produced a weight for a market on a builder dex that is not on the ACTIVE `trusted_dexes` allowlist (never added, or removed after listing — or the allowlist was unreadable: `allowlist_loaded=false`). That market got NO signal row (the version's other markets did); the executor would not have opened it anyway. Action: if the dex was removed on purpose, pause/delist the strategy (its version still names the market) and tell the creator; if the allowlist was unreadable, treat as a DB incident.
- **Admin strategy pause** (`POST /admin/strategies/{id}/pause`, one admin): the strategy's live subscribers get the mandatory `strategy_paused` alert; no new subscriptions or user resumes; no renewal is charged while paused (`renewals_skipped_paused` in the settlement report; billing status untouched); the executor opens nothing (exits and reductions run). **Unpause** = `POST /admin/strategies/{id}/list` + a second admin's approval: every live subscription whose paid period was still running when the pause began gets the paused time added to its period end, and billing resumes at the subscription's pinned price (subscribers get `strategy_resumed`). `strategies.paused_at` holds the pause start; never edit it by hand. A paused strategy that will not come back → delist (§H5 flow: subscriptions close).
- **Card-held pending releases.** Uncollected profit share (`ps_pending:*`) released to a creator payable is held for the 120-day card dispute window when the debt was paid by a CARD top-up — now also when the daily settlement sweep (not the top-up trigger) did the release: the release key `ps_release:{user}:{seq}` names the card posting that paid the debt. `GET /creator/earnings` shows released pending profit share under profit share (split over the strategies that fed it).
- **Referrer KYC.** Referral payouts need the same per-user KYC as creators (`POST /referrals/kyc/session` → one admin decides at `POST /admin/users/{id}/kyc`); `GET /referrals` shows `kyc_status`. A payout requested before this rule (no KYC) is refused at approval with `kyc_required` — reject it and ask the referrer to verify.


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
2. **Settlement** (`/internal/settle-daily`, 00:30 UTC): the job succeeded. There is one ledger transaction per active subscription for today's `settle_date`. Look for no duplicate-key errors, and any failures.
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
9. Record "Daily check done, [anomalies]" in the ops log.

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
| `new_entries_paused:{coin}` | Reduce-only on that market. **Set automatically by critical market alerts.** | Automatic / any admin | Maker-checker [GAP: the admin API key pattern does not yet accept this key; see SECURITY §7] |
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
   `gcloud scheduler jobs pause executor-tick --location=asia-southeast1 --project=$PROJECT`
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

1. **Prepare** (system): a batch is built from payables minus holds (KYC incomplete, investigation, reconciliation mismatch, address-change hold ≤ 48 h). Each item shows the beneficiary, amount, `to_address` and ledger reference.
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
| Builder-fee ledger ≠ Hyperliquid builder rewards | Fill attribution lag; fills without our builder code; fee-field semantics ([VERIFY] SPEC §1.1); a missed fill ingest | Re-run fills ingest for the window (idempotent on `tid`); compare by coin and day; check `builder_fee_micro` vs on-chain. **If unexplained after 24 h, SEV3.** |
| Treasury USDC ≠ ledger | Deposit not credited or double-credited; payout sent but not recorded; external transfer | Scan `usdSend` history for the treasury; match by tx hash to `deposits` and `payouts`; **hold all payouts** until resolved. **Any unexplained outflow is SEV1** (possible key compromise). |
| Stripe balance ≠ `stripe:clearing` | Webhook missed or failed; refund or dispute not posted; processor fee mismatch (fees are passed to users; the credit must equal net) | Stripe dashboard → resend failed events (idempotent); post refunds and disputes; check the net-of-fee calculation |
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

1. **Stop writers:** `kill_switch_global`; pause Scheduler jobs (tick, settle, reconcile, deposits scan). Set the API to maintenance mode (read-only) [DESIGN].
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
- **USDC deposit not credited:** get the tx hash from the user; check that the sender is their verified master address and the destination is the treasury; run the deposits scan (idempotent). If the sender is unverified, get a verification signature and then credit manually (maker-checker).
- **User reports unexpected trades:**
  1. check that the orders carry our `cloid` prefix;
  2. if ours, find the signal and guard trail;
  3. if not ours, it was manual or another agent — tell the user;
  4. **if ours but unexplained, pause the subscription and treat it as a possible SEV2.**
- **Admin account suspected compromised:** remove the admin role (the other admin); revoke sessions (Firebase: revoke refresh tokens); rotate that admin's hardware keys; review the audit log for their actions; SEV1 if any money or flag action looks suspicious.
- **Creator script failing in the sandbox:** the circuit breaker pauses the strategy; notify the creator; the admin reviews.

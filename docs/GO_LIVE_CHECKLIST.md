# aijalon.trade — Go-Live Checklist

Version: 2026-09-30 · Owner: [●] · Every box needs **evidence** (a link, screenshot, test output or sign-off). "Done" without evidence counts as not done.
Related: `docs/SECURITY.md`, `docs/RUNBOOK.md`, `docs/INCIDENT_RESPONSE.md`, `docs/DATA_PROTECTION.md`, `legal/README.md`

> **Status note.** Nothing below is ticked yet. Two gates matter:
> - **Gate B** (internal real-USDC testing) allows real money from **allowlisted team members only**, with small caps.
> - **Gate C** (public launch) requires the legal and licensing position, a pentest, and a clean internal phase.
>
> **The owner's instruction to rely on user waivers does not remove the Gate C legal items. Waivers do not replace licences.**

---

## Phase limits (all configurable; never hard-coded)

These keys exist in `backend/app/config.py` and are set per deploy (full table: DEPLOY §5.3). The API enforces them server-side, fail-closed.

| Key (env) | Gate B internal | Gate C public (initial) | Where it is set | Enforced where |
|---|---|---|---|---|
| `LAUNCH_PHASE` | `internal` | `public` | GitHub variable | api + executor (the app refuses to start in `internal` without an allowlist) |
| `ALLOWLIST_EMAILS` | team e-mails only; everyone else sees "coming soon" after the site gate | not used | **Secret Manager** (personal data) | api: signup and subscribe |
| `MAX_ALLOCATION_PER_USER_USD` | `0` = no cap (owner, 30 Sep 2026, SPEC §12 "No caps") | `0` | GitHub variable | api (subscribe/patch) when > 0 |
| `MAX_TOTAL_PLATFORM_ALLOCATION_USD` | `0` = no cap (owner) | `0` | GitHub variable | api when > 0 |
| `MAX_USER_LEVERAGE` | `0` = no launch cap (owner); bounded by the user's setting, the strategy's `MAX_LEVERAGE` and each market's Hyperliquid max | `0` | GitHub variable | api when > 0 |
| `IN_HOUSE_LISTED` | `silver` | `silver` (others only when they pass the walk-forward) | template | existing |
| `FEATURE_CREATOR_UPLOADS` | on (listing needs admin review + creator KYC) | on (KYC required) | template | existing |
| `STRIPE_MAX_TOPUP_USD` | `10000` default [CONFIRM a lower internal value] | [CONFIRM] | GitHub variable | api |
| Stripe mode | live keys with low Radar limits, or test keys [CONFIRM] | live | secret + `STRIPE_PUBLISHABLE_KEY` variable | Stripe |
| `PAYOUTS_ENABLED` | `false` (manual only, maker-checker) | `true` | GitHub variable | api |
| `KYC_PROVIDER` | `manual` | provider [CONFIRM] | GitHub variable (+ `KYC_*` secrets for sumsub) | api |

The owner removed the allocation and leverage caps; the liquidity guards (0.5% of 24h volume and 2% of OI per order) and every other pre-trade guard still apply. Setting a `MAX_*` variable to a positive value and redeploying restores a cap without a code change — keep that as the fast brake (Gate C3 rollback plan).

**The per-market exposure cap** is already partly covered by the risk guards (0.5% of 24h volume and 2% of OI per order). Consider an **aggregate** per-market cap across all subscribers for thin HIP-3 markets such as `xyz:SILVER` [CONFIRM].

---

## Gate A — Testnet / staging complete (no real money)

- [ ] All unit tests pass. Domain and ledger invariants tested: Σ=0, floor rounding, idempotency, HWM settlement, referral tiers, the fee-balance state machine (active → past_due → reduce_only after 72 h).
- [ ] End-to-end on Hyperliquid **testnet**: connect the wallet → approveAgent → approveBuilderFee → subscribe → tick places orders with the builder code and `cloid` → fills attributed → daily settlement → fee deductions → reduce-only when the balance hits 0.
- [ ] **Verify every SPEC §6 Hyperliquid fact** against current docs and mainnet behaviour:
  - agent limits;
  - agent cannot withdraw;
  - same-name agent replacement;
  - builder-fee max and the ≥100 USDC builder requirement;
  - EIP-712 domains;
  - HIP-3 asset ids;
  - **rate limits**;
  - reduce-only close below $10;
  - **the `fee` / `builderFee` field semantics in fills** (SPEC §1.1).
- [ ] Stripe test mode: PaymentIntent → webhook (signature verified) → **credit = gross − processor fee**, with the fee shown to the user before payment. Replayed webhook is idempotent. Refund and dispute flows.
- [ ] USDC deposit scan credits exactly once per tx hash, only from the verified master address.
- [ ] Sandbox: escape test suite (dunder, import, getattr, resource bombs) is blocked; the no-code builder output passes the same validator.
- [ ] **Creator-code decrypt path defined and implemented** (SECURITY §3.4 [GAP]).
- [ ] **Admin flags API accepts `new_entries_paused:{coin}`**, aligned with the executor (SECURITY §7 [GAP]).
- [ ] Web gates: site-entry gate (jurisdiction + terms + risk + privacy + waiver, none pre-ticked); subscribe gate (subscription-ack + fees + T&C again). Consents recorded with doc, version, context, strategy_id and a hash of the rendered text.

## Gate B — Internal real-USDC testing (allowlisted team only)

### B1. Legal (minimum for internal use)
- [ ] Company incorporated; bank and Stripe accounts in the company's name; Stripe has **approved the business category** (investment, crypto-adjacent).
- [ ] Counsel engaged, and `legal/README.md` questions sent. **Internal testers are team members trading their own money.** Counsel confirms this is acceptable before any licensing opinion [COUNSEL].
- [ ] Draft legal docs published behind the gate, marked "internal test".

### B2. Key ceremony (treasury / builder wallet)
- [ ] Held in person with at least 2 officers present, and a written script.
- [ ] Hardware wallets bought new from the manufacturer; firmware verified.
- [ ] Seed generated **on the device**. Metal backups made (2 copies, separate secure locations). The seed is never photographed or typed.
- [ ] Address recorded, and signed off by both officers. `BUILDER_ADDRESS` / `TREASURY_ADDRESS` set via a maker-checker config change.
- [ ] Test `usdSend` of a small amount, signed from the admin console, verified on the device and on-chain.
- [ ] Builder account funded with ≥ 100 USDC perps account value (the Hyperliquid builder requirement) [VERIFY].
- [ ] Evaluate Hyperliquid native multi-sig for the treasury [VERIFY]; record the decision.
- [ ] Ceremony minutes stored (without secrets).

### B2b. Mainnet verification (tiny amounts, before any tester funds)
- [ ] **Hyperliquid `/exchange` from the browser (CORS):** approveAgent, approveBuilderFee and the USDC `usdSend` deposit are posted by the browser straight to `https://api.hyperliquid.xyz/exchange` (CSP `connect-src` allows it). Confirm on Safari, Chrome and a mobile wallet browser that no CORS error occurs. **Fallback if it fails:** a server relay endpoint that forwards the user-signed payload unchanged (backend change; the API still never signs user actions).
- [ ] **Builder-dex asset id:** one tiny order on `xyz:SILVER` (expected asset id 110026 = 100000 + 10000 × dex index 1 + index 26, `backend/app/hl/markets.py`) fills on that market with our builder code and `0xa17a1000…` cloid. Re-check the ids after any Hyperliquid builder-dex change.
- [ ] **Reduce-only close below $10:** open a small position, shrink it, then close a remainder worth < $10 reduce-only. If Hyperliquid refuses, record it; the executor's $10 minimum and the "close positions" cancel flow must be adjusted before public launch.
- [ ] **Reconcile readers:** the builder-fee reader (`builderRewards` from info `referral` + `rewardsClaim` ledger updates) is UNVERIFIED — compare its figure for the builder address with the Hyperliquid UI after the first builder fees accrue and after a claim. Until verified, a builder-fee reconciliation mismatch may be the reader, not the ledger.
- [ ] **Agent `validUntil`:** `agent-expiry-scan` reads the approved agent's expiry from `extraAgents`; confirm it matches the wallet's approval and that the first reminder (14 days) is scheduled correctly.

### B3. Cloud security baseline
- [ ] KMS keyring `aijalon` / key `agent-keys` with **HSM** protection, in `asia-southeast1`. Rotation schedule set. IAM: encrypt = api SA, decrypt = executor SA, **no human decrypt**.
- [ ] Cloud SQL: private IP, CMEK, PITR on, backups kept 30 days, IAM DB auth. DB role grants verified (`\dp`): api has no SELECT on key and code ciphertext, and no UPDATE/DELETE on the ledger, audit or consents.
- [ ] Cloud Run: executor and sandbox ingress internal; sandbox has no egress and no SA permissions; Scheduler uses OIDC.
- [ ] No SA JSON keys (org policy). Deployer uses WIF and has no secret access.
- [ ] Secret Manager holds all secrets. `LOCAL_DEV_KEK_B64` absent in prod (the app refuses to start otherwise).
- [ ] Cloudflare: WAF, rate limits, geo-block of the restricted list (`RESTRICTED_COUNTRIES`), HSTS.
- [ ] Admin accounts: FIDO2 keys enrolled; admin allowlist; step-up tested.
- [ ] GitHub: branch protection with the 2-person rule, CODEOWNERS, Actions pinned by SHA, dependencies pinned with hashes, gitleaks, pip-audit and CodeQL passing.

### B4. Monitoring and ops readiness
- [ ] Dashboards (RUNBOOK §2) live.
- [ ] Critical alerts page to the Telegram ops chat plus email plus phone. **A test page was received and acknowledged.**
- [ ] **Kill-switch drill:**
  - engage global and per-market switches → confirm no orders;
  - `new_entries_paused` → reduce-only only;
  - a lift attempt by the same admin is **refused**; a lift by a second admin works.
- [ ] **Auto-pause drill:** inject a mark/oracle divergence in staging, and confirm the market pauses and ops are paged.
- [ ] **PITR restore drill** (RUNBOOK §8) done, with timings recorded (RTO target 4 h).
- [ ] Reconciliation job runs daily. The mismatch alert (> $1) has been tested.
- [ ] Incident response tabletop exercise done (one market-manipulation scenario and one key-compromise scenario).
- [ ] On-call rota and contacts sheet exist. At least 2 admins are available for maker-checker.
- [ ] **Scheduler:** all 13 jobs exist (DEPLOY §14.1) and are paused until `make go-live`; `fills-ingest` / `fills-ingest-presettle` / `funding-scan` run before `settle-daily` (00:30). The on-call knows the `fill_after_settlement` procedure (RUNBOOK §13.4).
- [ ] **Agent expiry reminders:** a test agent approved with a short validity produces `agent_expiring` / `agent_expired` on Telegram + e-mail; the executor stops trading that subscription (RUNBOOK §13.1).
- [ ] **Telegram unreachable drill:** block the bot from a test account → the link shows lapsed, the `telegram_unreachable` e-mail arrives, new entries pause after 24 h, re-linking lifts it (RUNBOOK §13.2). Ops alerts reach the ops group and `OPS_EMAILS`.
- [ ] **Held USDC deposit:** a transfer from an unverified wallet lands in `suspense:usdc_unattributed` with a `topup_held` ops event and is not credited. The release procedure (RUNBOOK §13.3, maker-checker) is agreed — **[GAP] no admin-console action exists yet**; until it does, held funds stay in suspense.
- [ ] **`candle_mismatch` events** are routed to ops and the on-call knows RUNBOOK §13.5; the first candle backfill (~40 calls) has finished.

### B5. Phase limits set
- [ ] `LAUNCH_PHASE=internal`; allowlist = team emails; per-user allocation cap **$1,000**; total platform allocation cap [$●]; leverage cap [●]x; only `xyz:SILVER` listed.
- [ ] Limits verified by trying to exceed each one (the API rejects; the executor clamps).

## Gate B exit criteria (before moving to Gate C)
- [ ] ≥ [30] days of internal live trading, including ≥ [N] real signal changes on SILVER (entries and exits).
- [ ] **Zero unexplained reconciliation mismatches > $1** over the last [14] days. The builder-fee ledger matches Hyperliquid builder rewards.
- [ ] Daily settlement ran every day, with no duplicates. Profit-share amounts were hand-checked for at least 3 subscriptions against on-chain fills.
- [ ] At least one full payout cycle (maker-checker + hardware wallet) and one fee-balance refund done.
- [ ] All SEV1/SEV2 incidents (if any) have closed post-mortems.

## Gate C — Public launch

### C1. Legal and regulatory (blocking)
- [ ] **Written opinion from Malaysian counsel on licensing** (legal/README Q1–Q6), covering: the Capital Markets and Services Act 2007 (fund management, dealing in derivatives, investment advice); the Securities Commission's digital-asset framework; whether the Fee Balance is e-money under the Financial Services Act 2013; AML reporting-institution status. **The launch design must be adjusted to match the opinion** (for example: restrict Malaysia, restructure the Fee Balance, obtain a licence, or remove the profit share).
- [ ] All `legal/*.md` reviewed, finalised, all markers removed, versions bumped. **Privacy notice available in Bahasa Malaysia and English.**
- [ ] Restricted-jurisdiction list confirmed by counsel (US stays restricted unless counsel signs off). Hyperliquid's own terms checked.
- [ ] Consumer Protection (Electronic Trade Transactions) Regulations 2012 disclosures on the site (entity, registration number, contacts, prices, terms) [COUNSEL].
- [ ] Tax: SST or service-tax registration decision; e-invoicing; withholding on creator and referrer payouts.
- [ ] DPO appointed (and notified if required); processor DPAs signed; breach register created.
- [ ] Insurance considered (cyber, professional indemnity) [CONFIRM].

### C2. Security (blocking)
- [ ] **Independent penetration test** (scope: SECURITY §9) completed. **No open critical or high findings.** Retest report received.
- [ ] Every [DESIGN] item in SECURITY §3 on the money path verified. Every [GAP] closed or formally accepted by the owner, with its risk written down.
- [ ] Admin just-in-time access (no standing production console access) in place.
- [ ] Audit-log WORM export in place.
- [ ] `security.txt` published; vulnerability disclosure policy live (bug bounty later).

### C3. Product and operations
- [ ] Phase limits changed to their public values via maker-checker config change; allowlist removed.
- [ ] Support inbox and response targets set; status banner mechanism tested; comms templates (INCIDENT_RESPONSE §5) pre-approved.
- [ ] Creator programme: KYC provider live; Creator Agreement final; review checklist for scripts (including a manipulation-intent review of scripts trading thin HIP-3 markets).
- [ ] Referral programme terms final (Direct Sales and Anti-Pyramid Scheme Act check).
- [ ] Capacity: Hyperliquid rate limits verified against the expected subscriber count × markets × ticks.
- [ ] Rollback plan: `new_entries_paused` + `LAUNCH_PHASE=internal` can be restored in < 5 minutes.

### C4. Sign-off

| Role | Name | Date | Signature / link |
|---|---|---|---|
| Owner | | | |
| Engineering lead | | | |
| Security lead | | | |
| DPO | | | |
| External counsel (legal gates only) | | | |

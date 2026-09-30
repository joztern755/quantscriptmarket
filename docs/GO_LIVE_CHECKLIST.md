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

The launch-phase controls below are **proposed config keys. They do not exist in `backend/app/config.py` yet** (as of this version). The config owner must add them, and the API and executor must enforce them server-side, fail-closed. Suggested names:

| Key (env) | Gate B internal | Gate C public (initial) | Enforced where |
|---|---|---|---|
| `LAUNCH_PHASE` | `internal` | `public` | api + executor |
| `ALLOWLIST_EMAILS` (or an allowlist table) | Team emails only; everyone else sees a "coming soon" page after the site gate | empty (off) | api: signup and subscribe |
| `MAX_ALLOCATION_PER_USER_USD` | **1,000** | [CONFIRM, e.g. 10,000] | api (subscribe/patch) + executor (clamp) |
| `MAX_TOTAL_PLATFORM_ALLOCATION_USD` | **[CONFIRM, e.g. 5,000]** | [CONFIRM, e.g. 250,000] | api (reject new or increased allocations over the cap) |
| `MAX_USER_LEVERAGE` (cap under `platform_max_leverage`) | **1–2x** [CONFIRM] | ≤ 5x | api + risk guards |
| `IN_HOUSE_LISTED` | `silver` | `silver` (others only when they pass the walk-forward) | existing |
| `FEATURE_CREATOR_UPLOADS` | on for team creators only (listing needs admin review) | on (KYC required) | existing |
| `MAX_FEE_BALANCE_TOPUP_USD` per user | [CONFIRM, e.g. 200] | [CONFIRM] | api |
| `STRIPE_MODE` | live with low Radar limits, or test [CONFIRM] | live | api |
| `PAYOUTS_ENABLED` | false (manual only, maker-checker) | true | api |

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

# aijalon.trade — Strategy Marketplace on Hyperliquid (SPEC v1)

This is the single source of truth every module is built against. If code and this file disagree, fix one of them in the same commit. Values marked **[CONFIRM]** are owner decisions still open — they live in config, never hard-coded.

## 0. Product in one paragraph

Users subscribe monthly to trading strategies ("scripts"). Scripts run only on our servers; buyers never see code. A buyer connects their own Hyperliquid account by approving (a) our per-user **agent wallet** (can trade, can never withdraw or transfer) and (b) our **builder fee**. Their funds stay in their own Hyperliquid account. They choose allocation and max leverage. Every order we place carries our builder code (0.1% of notional, collected on-chain by Hyperliquid). Profit share, subscriptions and paid posts are auto-deducted from a prepaid **fee balance** (topped up with USDC on Hyperliquid, or Stripe: cards, Apple Pay, Google Pay, local methods). If the fee balance runs out, the subscription stops opening new positions (exits still run). Creators (later: third parties; at launch: in-house) get paid from the ledger.

Operator: Malaysia. Markets: Hyperliquid perps only — validator perps + builder-deployed (HIP-3) perps (e.g. `xyz:SILVER`).

## 1. Money rules (all configurable in `backend/app/config.py` → `Economics`)

All money is **integer micro-USD** (1 USD = 1_000_000). Rates are integer **basis points (bps)** unless stated. Rounding: every fee charged to a user rounds **down** (floor) to the micro; splits allocate the remainder to the platform so a split always sums exactly to its input.

| Item | Rule | Default |
|---|---|---|
| Builder fee | On every strategy order; Hyperliquid collects it to our builder address. Order field `builder: {b, f}` with `f` in tenths of a bp. | 0.10% = `f=100` (perp max) |
| Builder fee split (per fill fee actually charged) | creator 0.05%, platform 0.03%, referral pool 0.02% (of notional) → i.e. of the collected fee: creator 50%, platform 30%, referral pool 20% | 5000/3000/2000 bps of fee |
| Referral pool | referrer gets tier% of the pool; remainder → platform. No referrer → all to platform | see §1.2 |
| In-house strategy | "creator" share → platform | — |
| Profit share | creator sets 0–12% (0–1200 bps) of **net realized profit above high-water mark** | cap 1200 |
| Platform profit share | 1.5% of the same profit (150 bps), charged ON TOP: user pays creator% + 1.5% → max 13.5% total (owner 30 Sep 2026). `carved_out` mode kept in code but unused | `on_top` |
| Strategy subscription | creator sets monthly price (USD); platform keeps 3% (300 bps), creator 97% | — |
| Paid post ("subletter") | creator sets price; platform keeps $1 per sale; min price $2 | $1 / $2 |
| Platform plans | Free $0 (1 active strategy), Pro $20/mo (3 strategies, alerts by email/Telegram, paid posts), Max $50/mo (unlimited strategies, CSV/tax export, read API) | 0/20/50 |
| Fee balance | Prepaid USD balance per user. Min top-up $10. Deductions: profit share (daily settlement), subscriptions (monthly, prepaid at start/renewal), paid posts (at purchase), platform plan (monthly) | — |
| Insufficient balance | subscription → `past_due`; after grace (72h) → `reduce_only` (no new entries, exits allowed); alerts sent at 50%, 20%, 0% of estimated monthly need | 72h |
| Stripe fee | passed to the user: credit = amount received − actual Stripe fee (from the charge's balance_transaction), shown to the user before paying as "card/processor fee deducted" (owner 30 Sep 2026) | pass-through |

### 1.1 Profit share math (per subscription)
- Attributed PnL = Σ over fills placed by us for this subscription (identified by our `cloid` prefix + account + coin) of `closedPnl − fee` (fee includes builder fee as reported by Hyperliquid — verify field semantics against real fills before go-live), plus funding payments on the strategy's coins while the subscription held a position.
- `cum_pnl` accumulates. `hwm` starts at 0 at subscription start. At each daily settlement: `profit = max(0, cum_pnl − hwm)`; if profit > 0: charge `profit × rate` and set `hwm = cum_pnl`. Losses never refund; they must be recovered before new profit share is charged.
- Settlement 00:30 UTC daily; ledger entries per subscription per day; idempotent on `(subscription_id, settle_date)`.

### 1.2 Referral tiers (single level, % grows with performance)
Evaluated daily on trailing-30-day stats of the referrer's referred users:

| Tier | Condition (either) | Referrer share of pool | = of notional |
|---|---|---|---|
| Starter | default | 50% | 0.010% |
| Partner | ≥10 active referred users OR ≥$1M referred 30d notional | 75% | 0.015% |
| Elite | ≥100 active referred users OR ≥$25M referred 30d notional | 100% | 0.020% |

A referral binds at signup (`?ref=CODE`, stored first-touch, 30-day cookie), is immutable afterwards, and self-referral is blocked (same user, same wallet, same device fingerprint hash).

## 2. Architecture (Google Cloud + Firebase, Cloudflare DNS/WAF)

```
Cloudflare (DNS aijalon.trade, WAF, rate limits)
 ├─ aijalon.trade  → Firebase Hosting (static SPA from web/dist, strict CSP)
 └─ api.aijalon.trade → Cloud Run "api" (FastAPI, Python 3.12)
Cloud Run "executor"  ← Cloud Scheduler (OIDC) every minute: /internal/tick
Cloud Run "sandbox"   (creator scripts; no egress, no service-account permissions)
Cloud SQL Postgres 16 (private IP, CMEK, PITR, IAM auth)
Cloud KMS keyring "aijalon" / key "agent-keys" (HSM protection) — envelope encryption
Secret Manager (Stripe keys, Telegram token, signal pubkey…)
Firebase Auth / Identity Platform (Google + Apple sign-in, TOTP MFA enforced)
```
Region: `asia-southeast1` (Singapore) for everything. Strategies are daily-bar; latency to Hyperliquid (Tokyo) is irrelevant.

### 2.1 Service accounts (least privilege — segregation is a security control)
| SA | Can | Cannot |
|---|---|---|
| `api` | KMS **encrypt** on agent-keys; Cloud SQL client; read its secrets | decrypt agent keys; sign orders |
| `executor` | KMS **decrypt** on agent-keys; Cloud SQL client | serve public traffic (ingress internal only) |
| `sandbox` | nothing | network egress, DB, KMS |
| `deployer` (GitHub Actions via Workload Identity Federation, no JSON keys) | deploy Run/Hosting, run migrations | read secrets values, decrypt |

The **treasury / builder wallet private key is never on any server.** Builder fees accrue to the builder address on Hyperliquid. Payouts and fee-balance withdrawals are prepared by the server, approved by two different admins (maker-checker), then signed in the admin's browser with a hardware wallet (`usdSend`).

## 3. Repository layout (module ownership)

```
docs/            SPEC.md (this), ARCHITECTURE.md, SECURITY.md, RUNBOOK.md, GO_LIVE_CHECKLIST.md, DEPLOY.md
legal/           terms.md, risk-disclosure.md, privacy.md (PDPA 2010), creator-agreement.md, acceptable-use.md, jurisdiction.md  — DRAFTS for Malaysian counsel
backend/
  pyproject.toml, requirements.txt (pinned, hashes in CI), requirements-dev.txt
  app/
    config.py                 settings from env (pydantic-settings); Economics; feature flags
    money.py                  micro-USD helpers, Decimal parsing of Hyperliquid strings, floor rounding
    domain/                   PURE logic, stdlib only, 100% unit tested
      fees.py                 builder-fee split, subscription split, post split, plan pricing
      profit_share.py         HWM settlement
      referrals.py            tier evaluation, reward calc, self-referral checks
      billing.py              fee-balance state machine (active/past_due/reduce_only), renewal schedule
      risk.py                 pre-trade guards (pure): sizing, leverage, liquidity, oracle deviation, whitelist
      jitter.py               per-user deterministic-random delay/ordering (privacy + fairness)
      track_record.py         ROI/$ aggregation, version resets, k-anonymity (min 5 subscribers)
      alerts_rules.py         anomaly rules → Alert objects (pure)
    ledger/                   double-entry ledger service over the DB (append-only, hash-chained)
    db/                       SQLAlchemy Core engine/session, repositories
    security/
      kms.py                  envelope encryption: CloudKmsKeyWrapper (prod) / LocalAesKeyWrapper (dev/test only, refuses to start when ENV=prod)
      agent_keys.py           generate secp256k1 key → address; encrypt; decrypt (executor only)
      auth.py                 verify Firebase ID token; require MFA claim; step-up (auth_time ≤ 300s)
      audit.py                audit-log writer (hash chain) — every security/financial event
      ratelimit.py
    hl/
      client.py               wrapper over official hyperliquid-python-sdk (Exchange/Info); builder code on every order
      markets.py              asset index resolution incl. builder dexes (100000 + dex_idx*10000 + idx), px/sz formatting
      fills.py                fetch fills/funding/ledger updates, attribution to subscriptions
      deposits.py             detect USDC usdSend to treasury → credit fee balance (idempotent on tx hash)
      fake.py                 in-memory fake exchange for tests
    strategies/
      registry.py             strategy + version records; in-house list
      signals.py              ingest signed signals (Ed25519) from terminal; validate; staleness
    execution/
      executor.py             tick: for each due subscription compute target → guarded orders → place → record
      reconcile.py            positions vs expected; ledger vs on-chain builder fees
    payments/
      stripe_pay.py           PaymentIntent (automatic_payment_methods), webhook verify, credit fee balance
    alerts/
      notifier.py             in-app + email + Telegram; severity routing; auto kill-switch on critical
    sandbox/
      runner.py               restricted execution of creator Python (AST allowlist), used by sandbox service
      backtest.py             walk-forward backtest for uploaded scripts
    api/
      main.py                 FastAPI app, security headers, CORS (aijalon.trade only)
      routers/*.py            public, me, consents, agents, subscriptions, balance, deposits, strategies, leaderboard, reviews, posts, referrals, creator, admin, internal
  migrations/               0001_init.sql … (plain SQL, applied in order by scripts/migrate.py)
  tests/                    unittest/pytest; tests for domain run with stdlib only
signals/                    Node adapter that runs vendored CREST scripts → signed signals.json (see §7)
web/                        vanilla TypeScript SPA (no framework, no npm runtime deps), tsc → web/dist
infra/                      gcloud bootstrap scripts, IAM, KMS, SQL, Run, Scheduler; firebase.json; cloudflare DNS script
.github/workflows/          ci.yml (tests, lint, gitleaks, SDK parity), deploy.yml (main only, WIF)
```

## 4. Data model (Postgres; see backend/migrations)

Core tables (all have `id uuid pk default gen_random_uuid()`, `created_at timestamptz default now()`):
- `users` (firebase_uid unique, email, display_name, role enum user|creator|admin, plan enum free|pro|max, country_attested, referral_code unique, referred_by → users, status active|suspended|closed, mfa_enrolled bool)
- `consents` (user_id, doc enum terms|risk|privacy|jurisdiction|waiver|creator_agreement|subscription_ack, doc_version, context enum site_entry|subscribe|creator, strategy_id null, ip_hash, user_agent_hash, accepted_at) — **append-only**
- `wallets` (user_id, master_address unique lower-case, verified_at via signed message)
- `agent_keys` (user_id, master_address, agent_address, agent_name, key_ciphertext bytea, kms_key_version, status pending_approval|active|revoked|rotated, approved_at, revoked_at) — ciphertext only; never selected by api role (column privilege)
- `builder_approvals` (user_id, master_address, max_fee_rate_tenths_bp, verified_on_chain_at)
- `strategies` (slug unique, name, owner_user_id, in_house bool, markets text[], timeframe, price_monthly_micro, profit_share_bps ≤1200, status draft|review|listed|paused|delisted, description)
- `strategy_versions` (strategy_id, version int, code_hash, code_ciphertext (creator uploads), params jsonb, published_at, backtest jsonb, live_since) — a new version resets live track record
- `subscriptions` (user_id, strategy_id, strategy_version_id, trading_address (master or sub-account), allocation_micro, max_leverage_x100, status pending|active|past_due|reduce_only|paused_user|closing|cancelled, cancel_positions close|leave null, current_period_end, hwm_micro, cum_pnl_micro, created_at) — UNIQUE active per trading_address
- `signals` (strategy_id, version_id, as_of_date, target_weight_x100 (0,100,200 for CREST), raw jsonb, signature, received_at) UNIQUE(strategy_id, as_of_date)
- `orders` (subscription_id, cloid unique, coin, side, sz, limit_px, reduce_only, status, hl_response jsonb, submitted_at, jitter_seconds)
- `fills` (subscription_id null, trading_address, coin, tid unique, px, sz, side, closed_pnl_micro, fee_micro, builder_fee_micro, cloid, time)
- `funding_events` (trading_address, coin, time, usdc_micro, UNIQUE(trading_address, coin, time))
- `ledger_accounts` (code unique, kind asset|liability|revenue|expense, owner_user_id null) — e.g. `user:{id}:fee_balance` (liability), `creator:{id}:payable`, `referrer:{id}:payable`, `platform:revenue:builder`, `platform:revenue:profit_share`, `platform:revenue:subscription`, `platform:revenue:posts`, `platform:revenue:plans`, `treasury:hl_usdc` (asset), `stripe:clearing` (asset), `builder:hl_receivable` (asset)
- `ledger_transactions` (idempotency_key unique, kind, memo, created_by, prev_hash, hash) and `ledger_entries` (tx_id, account_id, amount_micro bigint (+debit/−credit), CHECK per tx Σ=0 enforced by deferred constraint trigger). Both **append-only** (UPDATE/DELETE raise). Balances via view.
- `deposits` (user_id, method usdc_hl|stripe, external_ref unique, amount_micro, status, credited_tx_id)
- `withdrawals` / `payouts` (beneficiary, amount_micro, to_address, status requested|approved_1|approved_2|sent|rejected, maker_admin, checker_admin (≠ maker), tx_hash)
- `posts` (creator_id, strategy_id null, title, body_ciphertext or body, price_micro (0=free), published_at) ; `post_purchases` (post_id, user_id, ledger_tx_id) UNIQUE(post_id,user_id)
- `reviews` (strategy_id, user_id, rating 1–5, body, eligible_since) UNIQUE(strategy_id,user_id) — only after ≥30 days subscribed
- `showcase_wallets` (strategy_id, address, period_month, revealed_at null) — address public only after its month ends
- `alerts` (user_id null, severity info|warn|critical, kind, payload jsonb, created_at, acked_at)
- `audit_log` (actor, action, target, payload jsonb, ip_hash, prev_hash, hash) — append-only, hash chain
- `system_flags` (key pk, value jsonb, updated_by) — `kill_switch_global`, `kill_switch_market:{coin}`, `new_entries_paused`, maker-checker to lift
- `kyc_creators` (user_id, provider, provider_ref, status) — documents stay at provider

DB roles: `app_api` (no SELECT on agent_keys.key_ciphertext, no UPDATE/DELETE on ledger/audit/consents), `app_executor`, `app_migrator`.

## 5. Security model (summary; full in docs/SECURITY.md)
1. **Non-custodial trading funds.** We cannot withdraw user funds: Hyperliquid agents cannot transfer. Worst case of a full agent-key leak = unwanted trades, not theft by withdrawal → mitigated by §5.4.
2. **Auth:** Firebase Auth with Google or Apple only. TOTP MFA mandatory before any account action. API rejects tokens without `firebase.sign_in_second_factor`. Step-up (fresh sign-in ≤5 min + MFA) for: connect/rotate agent, subscribe/change allocation or leverage, withdraw, payout approve, change payout address, creator publish, admin actions. Passkeys: phase 2.
3. **Key custody:** agent keys generated in `security/agent_keys.py`, encrypted by KMS (HSM) before touching the DB; API can encrypt only, executor can decrypt only. Plaintext keys live only in executor memory during a tick; never logged (log filter redacts 64-hex strings).
4. **Trade guards (pre-trade, fail closed):** strategy market whitelist; target notional ≤ allocation × weight; leverage ≤ min(user max, strategy max, market max); order notional ≤ 0.5% of market 24h notional volume and ≤ 2% of open interest (thin HIP-3 markets!); limit price within 0.5% of mid (IOC); reject if mark deviates >2% from oracle or data older than 60s; global/market kill switches; per-subscription circuit breaker after 3 consecutive rejections.
5. **Anomaly alerts (JELLY lesson — detect and act fast):** mark/oracle divergence, OI spike >50% in 1h on a traded market, funding spike, user drawdown > 20% of allocation in 24h, order rejections burst, agent approval changed/revoked on-chain, login from new country, MFA reset, withdrawal/payout requests, ledger ↔ on-chain reconciliation mismatch > $1. Critical alerts auto-pause new entries on the affected market and page ops (Telegram + email).
6. **Integrity:** double-entry ledger, append-only + hash chain; daily reconciliation (Σ builder-fee ledger vs Hyperliquid builder rewards; treasury USDC vs ledger); idempotency keys on every money movement; all admin actions maker-checker and audit-logged.
7. **Web:** strict CSP (no inline script), SRI for CDN scripts, HSTS preload, frame-ancestors none, Referrer-Policy strict-origin, CORS only aijalon.trade. Cloudflare WAF + rate limits; API per-user and per-IP rate limits.
8. **Privacy of subscriber wallets:** addresses never shown publicly or to creators; one unique agent per user; per-user random delay (0–10 min default) and randomized execution order; strategy public stats only aggregated with ≥5 subscribers. On-chain clustering cannot be fully prevented — disclosed in risk disclosure.
9. **Creator code:** stored encrypted, executed only in the sandbox (no egress, no creds, CPU/mem/time limits, AST allowlist: math/statistics only, no I/O, no dunder access), outputs a target weight only. Buyers never receive code.

## 6. Hyperliquid facts used (verify at go-live against docs)
- Agent (API) wallet: `approveAgent` signed by the master wallet (EIP-712 user-signed action). Limit: 1 unnamed + 3 named per account, +2 named per sub-account. Agents cannot withdraw/transfer. Named agent name we use: `aijalon`.
- Builder fee: user signs `approveBuilderFee` (maxFeeRate as percent string, e.g. `"0.1%"`); builder needs ≥100 USDC perps account value; max 0.1% perps; ≤10 active builder approvals per user.
- User-signed EIP-712 domain: `{name:"HyperliquidSignTransaction", version:"1", chainId:<signatureChainId>, verifyingContract:0x000…0}`; `signatureChainId` = the wallet's current chain id (hex); `hyperliquidChain` = "Mainnet".
  - ApproveAgent types: hyperliquidChain string, agentAddress address, agentName string, nonce uint64; primaryType `HyperliquidTransaction:ApproveAgent`.
  - ApproveBuilderFee types: hyperliquidChain string, maxFeeRate string, builder address, nonce uint64; primaryType `HyperliquidTransaction:ApproveBuilderFee`.
  - UsdSend types: hyperliquidChain string, destination string, amount string, time uint64; primaryType `HyperliquidTransaction:UsdSend`.
  - POST `https://api.hyperliquid.xyz/exchange` body `{action, nonce, signature:{r,s,v}}`; nonce = ms timestamp.
- Builder-deployed perp asset id = `100000 + perp_dex_index * 10000 + index_in_meta`; coin name `dex:COIN` (e.g. `xyz:SILVER`). Launch markets verified on mainnet 2026-09-30: `BTC`, `SOL`, `HYPE`, `xyz:GOLD`, `xyz:SILVER`, `xyz:CL` (WTI), `xyz:BRENTOIL`.
- Rate limits: **unverified in this environment** (docs blocked) — must be checked before scaling beyond internal testing.

## 7. In-house strategies and signals
**Owner decision (30 Sep 2026): launch with SILVER only** (the only CREST script that passes its walk-forward per the terminal's REASSESSMENT.md, 28 Sep 2026). BTC, SOL, HYPE, GOLD, OIL, RUNNERS stay unlisted (seeded as `draft`, not visible) until their scripts pass. **Creator uploads are ON at launch** (Creator Studio enabled; creator KYC required before a script can be listed or paid out; every creator script goes through sandbox validation + walk-forward backtest + admin review before `listed`).

Signal pipeline: the terminal's daily GitHub Action already fetches the full-history data. It will additionally run `signals/emit.js`, which loads each vendored `crest_<key>.js`, runs it over the same rows, reads the **last completed bar's** state (in position? leveraged?) and writes `signals.json` = `{as_of, generated_at, engine_sha256, strategies:{key:{target_weight:0|1|2, last_action, last_action_date, market:"xyz:SILVER", script_sha256, status}}}` (bar_close = as_of + 1 day 00:00 UTC; generated_at ≤ 36h old; as_of ≤ 4 days old), signed with Ed25519 (private key only in the terminal repo's GitHub secret). The marketplace fetches it, verifies the signature against the pinned public key, rejects if stale (>36h), and stores it in `signals`. Market mapping: BTC→`BTC`, SOL→`SOL`, HYPE→`HYPE`, GOLD→`xyz:GOLD`, SILVER→`xyz:SILVER`, OIL→`xyz:CL` **[CONFIRM WTI vs Brent]**, RUNNERS→its per-coin members (multi-market).

## 8. API surface (FastAPI, JSON, all under `/v1`)
Public: `GET /public/strategies`, `/public/strategies/{slug}` (stats, version history, reset banners, backtest), `/public/leaderboard?by=roi|pnl|subscribers&period=30d|90d|all`, `/public/posts?strategy=`, `/public/config` (fees, plans, restricted jurisdictions), `/public/showcase/{slug}` (revealed wallets only).
Auth (Bearer Firebase ID token with MFA): `GET/PATCH /me`, `POST /consents` (batch), `GET /consents/status`, `POST /wallets/verify` (signed message), `POST /agents` (step-up; returns agent address + typed data to sign), `POST /agents/{id}/confirm` (server checks on-chain approval via info `extraAgents`), `POST /builder-approval/confirm` (checks `maxBuilderFee`), `GET/POST /subscriptions`, `PATCH /subscriptions/{id}` (allocation/leverage/pause; step-up), `DELETE /subscriptions/{id}`, `GET /balance` + ledger history, `POST /deposits/stripe` (PaymentIntent), `POST /deposits/usdc/typed-data` + `POST /deposits/usdc/confirm`, `POST /withdrawals` (step-up), `GET /positions` (live from Hyperliquid), `GET /alerts`, `POST /reviews`, `POST /posts/{id}/purchase`, `GET /referrals` (code, tier, stats, earnings).
Creator: `POST /creator/strategies`, `POST /creator/strategies/{id}/versions` (upload code → sandbox validation + walk-forward backtest; resets live record), `POST /creator/posts`, `GET /creator/earnings`, `POST /creator/kyc/session`.
Admin (role=admin, step-up): flags/kill switches (maker-checker to lift), payouts approve (two admins), strategy review/list/delist, prices for in-house strategies, user suspend, alerts console, reconciliation report.
Internal (OIDC from Scheduler only): `POST /internal/tick`, `/internal/settle-daily`, `/internal/ingest-signals`, `/internal/reconcile`, `/internal/deposits-scan`, `/internal/referral-tiers`.
Webhook: `POST /webhooks/stripe` (signature verified, idempotent).

## 9. Web app (web/)
Vanilla TypeScript SPA, hash routing, no npm runtime deps. Brand = terminal's tokens (warm neutral light theme + dark theme; accent `#9A5B14` light / `#E8A94A` dark; brass `#2C6A70`/`#7FB3B8`; Instrument Sans + JetBrains Mono). Firebase Auth JS loaded from gstatic with pinned version.
Gates: (1) **Site entry gate** before any content: jurisdiction attestation + Terms + Risk Disclosure + Privacy + Liability waiver checkboxes (each must be ticked, versions recorded server-side after login; before login stored locally and re-recorded at login). (2) **Subscribe gate**: strategy-specific risk acknowledgement + fees summary (builder fee, subscription, profit share incl. platform share) + T&C again.
Pages: Gate, Sign in (Google/Apple) + MFA enrolment, Marketplace (filters by asset/market, status badges incl. "HOLDS — no active signals"), Strategy detail (live on-chain record since current version, ROI %, $ made for users (aggregate), version reset timeline, walk-forward backtest with the warning "Backtest after a script change can be fitted to history; not proven live yet"), Subscribe wizard (connect wallet → verify → approve agent (EIP-712 in wallet) → approve builder fee → allocation & max leverage → fee balance check → confirm with step-up), Dashboard (positions, PnL per subscription, fee balance + deposit USDC/Stripe, alerts), Leaderboard, Reviews, Posts, Referrals, Creator Studio (ON at launch: upload Python or build with the no-code builder, backtest, submit for review, posts, earnings, KYC), Admin console, Legal pages.

## 10. Strategy script contract (creator Python, no-code builder, in-house)

Every strategy version, whatever its source, reduces to **target weights per coin at each bar close**.

Creator Python file (single module, ≤ 64 KB):
```python
MARKETS = ["BTC", "xyz:SILVER"]   # 1–5 Hyperliquid perp coins (validated against live meta)
TIMEFRAME = "1d"                  # "1h" | "4h" | "1d"
LOOKBACK = 300                    # bars supplied per coin, 50–1000
MAX_LEVERAGE = 2                  # 1–5 (platform cap), per-coin |weight| ≤ MAX_LEVERAGE

def signal(bars):
    # bars: {coin: [{"t": ms_open_time, "o": float, "h": float, "l": float, "c": float, "v": float}, ...]}
    # oldest → newest; the last element is the last CLOSED bar. No future data is ever supplied.
    # return {coin: weight}; weight = fraction of the subscriber's allocation; negative = short; 0 = flat.
    return {"BTC": 1.0, "xyz:SILVER": 0.0}
```
Rules (enforced by `sandbox/runner.py` before anything runs): AST allowlist — imports only `math`, `statistics`; no `open`, `eval`, `exec`, `compile`, `__import__`, `globals`, `locals`, `getattr`/`setattr`/`delattr`, `vars`, `dir`, `type`, attribute names starting with `_`, `while True` without break is allowed but CPU time limit 2 s per call, memory 256 MB; deterministic (no `random`, no time). Output validated: keys ⊆ MARKETS, finite floats, |w| ≤ MAX_LEVERAGE, Σ|w| ≤ MAX_LEVERAGE.
No-code builder: JSON spec (`indicators`: sma/ema/rsi/atr/highest/lowest/roc; `rules`: comparisons combined with all/any → weight) compiled by `sandbox/nocode.py` into Python source that passes the same validator (so there is one execution path).
In-house (CREST): weights come from the signed terminal feed (§7), long-only, weight ∈ {0, 1, 2}.

Signal storage: `signals(strategy_version_id, bar_close timestamptz, coin, target_weight_bps int (weight × 10000), source enum sandbox|terminal, UNIQUE(strategy_version_id, bar_close, coin))`. The sandbox runs each listed version **once per bar close** (not per user); the executor fans out to subscriptions.
Order sizing per subscription: `target_notional = allocation × weight` (signed), clamped by guards (§5.4) and by `subscription.max_leverage`; `delta = target_notional − current_position_notional`; skip if |delta| < max($10, 2% of allocation) (avoids churn); orders are IOC limit within slippage cap; reduce-only when `reduce_only` status or when delta shrinks exposure.

Walk-forward backtest on every upload (`sandbox/backtest.py`): Hyperliquid candles (as much history as the API gives, ≥ 1 year required to list), fees = taker fee + builder fee 0.1%, funding approximated from historical funding API, anchored walk-forward is not possible without re-fitting, so we report **in-sample (full) + out-of-sample (last 30%)** separately and always show the warning "Backtest of a newly uploaded script can be fitted to history; not proven live yet" until 90 days of live signals exist.

## 11. Code conventions (all modules)
- Python 3.12, type hints, `from __future__ import annotations`. `backend/app/domain/*` and `backend/app/sandbox/runner.py` use **stdlib only**.
- Money: `app.money` helpers only; never float for money. Hyperliquid strings → `Decimal` → micro via `to_micro(Decimal, rounding=FLOOR)`.
- Config: `from app.config import get_settings, Economics` — never read env elsewhere.
- Time: UTC everywhere, `datetime.now(timezone.utc)`; injected clocks in domain code for tests.
- Errors: raise typed exceptions from `app.errors`; API maps to HTTP.
- Logging: `app.logging.get_logger`; structured JSON; secrets redacted.
- Tests: `backend/tests/test_<module>.py`, runnable with `python -m pytest` (prod) and the domain/sandbox ones also with `python -m unittest` (no deps).

## 12. Owner decisions (30 Sep 2026, latest)
- **Cancel flow:** the user picks one of two buttons — "Close positions and cancel" or "Leave positions open and cancel" — each with a double confirmation (second dialog restates the consequence) and step-up auth. API: `DELETE /v1/subscriptions/{id}` body `{"positions": "close"|"leave"}` (required, no default).
  - `close` → status `closing`: executor treats target weight 0 for every strategy market, reduce-only, until the on-chain position for those markets is 0 (bounded retries + alert on residual), then `cancelled`. No new entries. Profit share settles on the realized PnL of the closing fills, then stops.
  - `leave` → status `cancelled` immediately; the executor never touches the account again for this subscription; open positions are the user's responsibility (UI says so explicitly). Profit share settles realized PnL up to cancellation only.
  - Either way the subscription's prepaid period is not refunded (see legal/refund-policy.md) and the agent stays approved unless the user revokes it (UI offers "revoke agent" guidance).
- **SILVER is listed FREE as a transparent showcase of the engine:** price $0, profit share 0%, builder fee still applies to any orders. Its card and page state plainly: live signal is CASH since 1980-01-15 under the current setting (M2 filter blocking entries), so subscribers may see no trades for a long time.
- **Listing history rule (owner):** a strategy version can be listed with **≥ 180 days** of backtestable history on every one of its markets (Hyperliquid serves only the latest 5,000 candles per coin/interval ≈ 208 days of 1h, 833 days of 4h). Versions with < 365 days show a clear **"Short history (N days)"** warning on the card and page, next to the existing "not proven live" warning.
- **Own candle history (owner):** from launch we store candles ourselves so history grows over time: table `candles(coin, interval, open_time, o, h, l, c, v, source, fetched_at, PRIMARY KEY(coin, interval, open_time))` (prices as NUMERIC strings from the API, never floats), job `/internal/candles-sync` hourly for every perp market (validator + all builder dexes) at 1h, 4h and 1d, backfilling what the API still serves. Only CLOSED candles are stored; a stored closed candle is immutable (a later differing value raises a data alert, not an overwrite). Backtests read stored candles first, then the API for the rest, and record the data range used.
- **User alerts on Telegram + email (owner):** every user must link Telegram AND confirm an email for alerts before their first subscription can start (onboarding step; required on all plans, so "email/Telegram alerts" is no longer a paid-plan feature).
  - Telegram link: user taps `https://t.me/<bot>?start=<one-time token>` (token 10-min, single use, bound to the user); the bot webhook `/v1/webhooks/telegram` (secret-token header verified) stores `telegram_chat_id`. "Send test alert" button. If the bot is blocked by the user (403 from Telegram), mark unlinked, pause new entries for that user's subscriptions after 24h and alert by email.
  - Email: account email by default (Apple private-relay addresses are fine); changing it requires a 6-digit code sent to the new address + step-up.
  - Alert kinds (per user, each can be muted except the mandatory ones marked *): agent approval expiring at 14, 7, 3, 1 days and expired* (re-approve link), builder approval missing*, trade opened / closed / resized (coin, side, size, avg price, fees), realized profit / loss per closed trade and daily PnL summary, fee balance low (50/20/0%)* and past-due / reduce-only*, profit share charged, deposit credited / refunded / disputed*, withdrawal requested/sent*, new-device login*, MFA change*, strategy paused / kill switch on a market the user trades*, signal stale.
  - Delivery: Telegram + email for warn/critical and trade events; info in-app + Telegram. Messages never contain full addresses, keys or tokens.

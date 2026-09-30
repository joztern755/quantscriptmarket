# API contract — web (web/src) ↔ backend (backend/app/api)

The **backend is the source of truth** (`backend/app/api/schemas.py` + `routers/*.py`); the web mirrors it in
`web/src/pages/_shared/types.ts` and `web/src/core/api.ts` (`PublicConfig`). If code and this file disagree, fix
one of them in the same commit. Reconciled 30 Sep 2026 (the two sides had been built in parallel on guessed shapes).

## Conventions

| Topic | Rule |
|---|---|
| Base | `https://api.aijalon.trade/v1` (`app-config.json` `apiOrigin`). JSON only. |
| Auth | `Authorization: Bearer <Firebase ID token>`; token must carry a TOTP second factor (`401 mfa_required` otherwise). `/public/*` is anonymous. |
| Gates | `consented_user` = current versions of terms/risk/privacy/jurisdiction/waiver accepted (`403 consent_required`, details.missing). Step-up = sign-in ≤ 300 s (`401 step_up_required`; core `api.ts` runs `stepUp()` and retries once with the same Idempotency-Key). |
| Idempotency | `Idempotency-Key` header (16–128 chars) **required** on money POSTs (marked 🔑); core sends a fresh UUID on every POST/PATCH/DELETE, pages reuse one per user action where retries are possible. Replays return the stored response with `Idempotent-Replayed: true`. |
| Money | Requests: `<name>_micro` (strict int) **or** `<name>` (USD decimal string ≤ 6 dp) — exactly one. Responses: always `*_micro` ints. Rates: bps ints. HL sizes/prices: decimal strings. |
| Request bodies | Every request model forbids unknown fields (`422 validation_failed`, details.fields = `[{loc, msg}]`). The web sends only the fields below. |
| Lists | `Page<T>` = `{"items": [...], "next_cursor": str|null}` with `?limit=&cursor=` — except the plain arrays noted below. |
| Errors | `{"error": {"code", "message", "details"?}, "request_id"}`. Codes: `bad_request 400`, `unauthorized 401`, `mfa_required 401`, `step_up_required 401`, `insufficient_balance 402`, `forbidden 403`, `consent_required 403`, `not_found 404`, `conflict 409`, `contacts_required 409`, `payload_too_large 413`, `validation_failed 422`, `guard_rejected 422`, `rate_limited 429`, `jurisdiction_restricted 451`, `internal_error 500`, `not_implemented 501`, `external_service_error 502`, `service_unavailable 503`, `kill_switch_active 503`. Machine-readable sub-cases are in `details.reason` (listed per endpoint). |

## Public (anonymous, per-IP limit 120/min)

| Method & path | Request | Response | Errors / notes | Web caller |
|---|---|---|---|---|
| GET `/public/config` | — | `PublicConfigOut` (below) | — | core `publicConfig()` |
| GET `/public/strategies` | `?market=&limit≤50&cursor=` | `Page<StrategySummary>` | 422 bad cursor | market, home |
| GET `/public/strategies/{slug}` | — | `StrategyDetail` | 404 | strategy, subscribe |
| GET `/public/strategies/{slug}/equity` **(new)** | — | `EquitySeriesOut {slug, version|null, since|null, points[{t, pnl_micro, roi_bps|null}], hidden_reason|null}` | 404 · `hidden_reason` `not_live` / `too_few_subscribers` (then `points = []`) | strategy (live chart) |
| GET `/public/strategies/{slug}/reviews` | `?limit&cursor` | `Page<ReviewOut>` | 404 | strategy |
| GET `/public/leaderboard` | `?by=roi\|pnl\|subscribers&period=30d\|90d\|all` | `LeaderboardOut {by, period, entries:[{rank, slug, name, roi_bps, pnl_micro, subscribers}]}` | hidden/not-live strategies never ranked | leaderboard |
| GET `/public/posts` | `?strategy=<slug>&limit&cursor` | `Page<PostSummary {id, title, price_micro, strategy_slug, creator_display_name, published_at, preview}>` | preview = first 280 chars of FREE posts | posts, strategy |
| GET `/public/posts/{id}` **(new)** | — | `PostOut` (body only when free) | 404 unpublished | posts (signed out) |
| GET `/public/showcase/{slug}` | — | **array** `[{address, period_month, revealed_at}]` | only months that ended + revealed | strategy |

### `PublicConfigOut`
`builder_address, treasury_address, agent_name, hl_chain, stripe_publishable_key|null,`
`stripe_fee_estimate_bps|null, stripe_fee_estimate_fixed_micro|null` (**renamed** from the web's guessed
`stripe_fee_estimate{pct_bps,fixed_micro}`; names = `config.Settings`; null when not configured or
`economics.stripe_fee_absorbed`), `restricted_jurisdictions[]`, `legal_versions{doc key → version}`,
`economics{builder_fee_tenths_bp, builder_split_*_bps, profit_share_creator_cap_bps (1200), platform_profit_share_bps (150), platform_profit_share_mode ("on_top"), subscription_platform_bps, post_platform_fee_micro, post_min_price_micro, min_topup_micro, past_due_grace_hours, stripe_fee_absorbed}`,
`plans[{key, price_monthly_micro, max_active_strategies|null, features[]}]`, `referral_tiers[{name, min_active_users, min_notional_30d_micro, share_of_pool_bps}]`,
`features{creator_uploads, payouts}`, `platform_max_leverage`, `max_user_leverage_x100|null` (optional operator limit; `null` by default — owner removed allocation and leverage caps),
`min_allocation_micro` (100 USD), `min_listing_history_days` (180) **(new)**, `short_history_warning_days` (365) **(new)**, `launch_phase`.

### `StrategySummary` (list) / `StrategyDetail`
Summary: `id, slug, name, description` **(moved up from detail)**, `in_house, markets[], timeframe, status, price_monthly_micro|null, profit_share_bps, platform_profit_share_bps, platform_profit_share_mode, holds|null,`
**new:** `signal_state ("trades"|"holds"|"unknown"), live_days|null, not_live_proven, max_leverage|null (current version MAX_LEVERAGE), history_days|null, short_history_days|null (set when < 365 → "Short history (N days)"), free_showcase, showcase_text|null`,
`current_version|null (int), live_since|null, stats{subscribers, roi_bps, pnl_micro, since, hidden_reason}` (k-anonymity: nulls + `hidden_reason` `not_live`/`too_few_subscribers`).
Detail adds: `versions[{version, published_at, live_since, is_current}]`, `backtest` (sandbox report minus `trades`, `latest_signal`, `data_notes`: `period{sim_days,…}, history_days, equity_curve[[t_ms, equity]], metrics{in_sample, out_of_sample, full, split_t}` with fractional returns, `trade_count, warnings[]`), `backtest_warning|null`, **`risk_ack_text` (new, shown by the subscribe gate)**, `rating_avg_x100|null, rating_count`.
SILVER (SPEC §12): `free_showcase=true`, `showcase_text` = "Free showcase of the engine: $0/month and 0% profit share … CASH since 1980-01-15 … no trades for a long time." — card and page render it verbatim.
Not provided (web guessed, dropped): `creator_name`, `featured`, per-strategy `showcase` embed.

### `EquitySeriesOut` (GET `/public/strategies/{slug}/equity`)
Shape: `{slug, version (int|null), since (ISO datetime|null), points: [{t: "YYYY-MM-DD" (UTC day string), pnl_micro, roi_bps|null}], hidden_reason: null|"not_live"|"too_few_subscribers"}`.
Daily aggregate LIVE record of the **current version** (a new version resets it), from the same inputs as `stats`
(subscribers' fills `closedPnl − fee` + funding, `app.domain.track_record.daily_series`): one point per UTC day since
`since` (= the version's `live_since`), at most the latest 1000 days. `t` = the UTC day (`"YYYY-MM-DD"`); `pnl_micro` =
cumulative $ made by all subscribers since `since` through the end of that day (today: now); `roi_bps` = that PnL over
the time-weighted capital of [since, end of day] (null without capital) — the last point equals `stats` of the
`all` period. k-anonymity (SPEC §5.8): the whole series is hidden (`points: []`, `hidden_reason: "too_few_subscribers"`)
below `min_subscribers` (5) distinct users, and days before 5 users had capital deployed are omitted; a version that
never went live → `hidden_reason: "not_live"`. Cacheable 60 s like every `/public` route.

## Account (Bearer + MFA)

| Method & path | Gate | Request | Response | Errors / reasons | Web caller |
|---|---|---|---|---|---|
| GET `/me` | MFA | — | `MeOut {id, email, display_name, role, plan, status, referral_code, country_attested, mfa_enrolled, created_at, consents_complete, wallets[{address, verified_at}], kyc_status|null}` (**kyc_status new**: `pending` \| `provider_approved` (provider passed, awaiting our admin) \| `approved` \| `rejected`) | 403 suspended/not allow-listed | core state |
| PATCH `/me` | MFA | `{display_name?, referral_code_used?}` | `MeOut` | 409 already bound, 403 self-referral/window, 404 code | main.ts (first-touch ref) |
| POST `/me/plan` 🔑 | consent | `{plan: "free"\|"pro"\|"max"}` | `PlanChangeOut {plan, charged_micro, period_end|null, fee_balance_micro}` | 402 `insufficient_balance` (details `balance_micro`, `required_micro`) · 409 already on this plan · 409 too many active strategies for the plan (details `active`) | plan picker in the UI (see below) |
| GET `/consents/status` | MFA | — | `{required, accepted, missing[], complete}` | — | — |
| POST `/consents` | MFA | `{consents:[{doc, doc_version, context, strategy_id?, accepted_at?, doc_text_sha256, country?}]}` (1–10) | `ConsentStatusOut` | 409 version changed (details.current_version) · **409 reason `legal_text_mismatch`** · 422 missing hash · 451 restricted country · 503 prod without canonical hashes | gate.ts (site + subscribe), creator (creator_agreement) |

`POST /me/plan` (exists, `routers/me.py`): switches the platform plan NOW. A paid plan (Pro $20, Max $50 —
`public/config.plans`) charges the first month immediately from the fee balance (`charged_micro`; no proration, no
refund when switching) and sets `period_end` = now + 1 month; the daily settlement renews it from the fee balance at
`period_end` (unpaid → `plan_past_due` alert, downgraded to free after the 72 h grace → `plan_downgraded` alert).
`free` → `charged_micro: 0`, `period_end: null`. Downgrading is refused (409) while the user has more live
subscriptions than the target plan allows (`max_active_strategies`). Send a fresh `Idempotency-Key` per click.

Sign-in security alerts (server-side, no web call): the API raises the mandatory `new_device_login` alert on a sign-in
from a new country or a new device and `mfa_changed` when the Firebase second factor differs from the last one seen.
The web SHOULD send **`X-Device-Id`** on every API call: a random 16–128 char `[A-Za-z0-9_-]` id created once and kept
in `localStorage` (never derived from hardware; CORS allows the header). Without it the device is approximated from
the User-Agent without version numbers.

**Consent doc keys** (DB enum `consent_doc`, SPEC §4) and the file whose bytes are hashed:

| doc | legal file | context |
|---|---|---|
| `terms` | `legal/terms.md` | site_entry / subscribe |
| `risk` | `legal/risk-disclosure.md` | site_entry / subscribe |
| `privacy` | `legal/privacy.md` | site_entry |
| `jurisdiction` | `legal/jurisdiction.md` | site_entry |
| `waiver` | `legal/liability-waiver.md` | site_entry / subscribe |
| `subscription_ack` | `legal/subscription-ack.md` | subscribe (+ strategy_id) |
| `creator_agreement` | `legal/creator-agreement.md` | creator |

`config.legal_versions` / `config.legal_doc_hashes` are keyed by file stem; `deps.LEGAL_DOC_FILES` maps them to doc
keys (same table as `gate.ts LEGAL_SLUGS` and `build.mjs DOC_FILES`). **`doc_text_sha256` is required**: the web
fetches `/legal/<file>.md` as an ArrayBuffer, hashes it with `crypto.subtle.digest("SHA-256")`, and the server
refuses any hash ≠ `legal_doc_hashes[doc]` of the current version.

## Wallets, agent, builder fee

| Method & path | Gate | Request | Response | Errors / reasons | Web caller |
|---|---|---|---|---|---|
| POST `/wallets/nonce` | consent | (no body) | `{nonce, expires_at}` | 429 | core wallet.proveOwnership |
| POST `/wallets/verify` | step-up | `{address, message (EIP-4361), signature}` | `WalletOut {address, verified_at}` | 422 domain/URI/nonce/time/signature, 409 wallet linked elsewhere | wallet.proveOwnership |
| GET `/agents` | consent | — | **array** `AgentOut[] {id, master_address, agent_address, agent_name, status, approved_at, created_at}` | — | subscribe (reuse active agent) |
| POST `/agents` | step-up | `{master_address, signature_chain_id ("0x…" = wallet chain), rotate?}` | 201 `{agent: AgentOut, approve_agent: {typed_data, action, nonce}, approve_builder_fee|null, exchange_url, required_builder_fee_tenths_bp}` | 403 wallet not verified · 409 active agent exists (web then reuses it) | subscribe |
| POST `/agents/{id}/confirm` | step-up | — | `AgentOut` | 422 not on-chain yet / expiring · 409 replaced | subscribe |
| POST `/builder-approval/confirm` | consent | `{master_address}` | `{master_address, max_fee_rate_tenths_bp, required_tenths_bp, sufficient, verified_on_chain_at}` | 403 · 502 | subscribe (checks `sufficient`) |

The web never signs server typed data as received: it validates it (`hl.validateServerTypedData`) and rebuilds the
action locally with the wallet's current chain id.

## Subscriptions (SPEC §12 cancel flow)

| Method & path | Gate | Request | Response | Errors / reasons | Web caller |
|---|---|---|---|---|---|
| GET `/subscriptions` | consent | `?limit≤100&cursor` | `Page<SubscriptionOut>` | — | dashboard, subscribe |
| GET `/subscriptions/{id}` | consent | — | `SubscriptionOut` | 404 | — |
| POST `/subscriptions` 🔑 | step-up | `{strategy_id, trading_address, allocation_micro (≥ 100 USD), max_leverage_x100 (100–2000, ≤ the version's MAX_LEVERAGE; each market's own max leverage + liquidity guards apply at order time), expected_price_monthly_micro, expected_profit_share_bps}` (+ optional inline `ack`) | 201 `{subscription, charged_micro, fee_balance_micro}` | 402 `insufficient_balance` (price + reserve = min top-up when any profit share can accrue) · 409 reasons `contacts_required` (code), `builder_fee_not_approved`, `terms_changed`, `subscription_ack_required` (ack must be ≤ 30 min old), `agent_not_active`, `trading_address_in_use` · 403 reason `plan_limit` (403 allocation limit only if an operator limit is configured; none by default) · 404 not listed · 422 leverage (details.max_x100) | subscribe |
| PATCH `/subscriptions/{id}` | step-up | `{allocation_micro?, max_leverage_x100?, paused?}` | `SubscriptionOut` | 409 not changeable / `agent_not_active` · 422 | dashboard (pause/resume/edit) |
| DELETE `/subscriptions/{id}` | step-up | `{"positions": "close"\|"leave"}` (**required**) | `SubscriptionOut` (`closing` or `cancelled`) | 409 already cancelled | core `subscriptions.cancelButtons` from the dashboard |

`SubscriptionOut`: `id, strategy_id, strategy_slug, strategy_name, strategy_markets[]` **(new)**, `trading_address, allocation_micro, max_leverage_x100, status (pending|active|past_due|reduce_only|paused_user|closing|cancelled), cancel_positions|null, cancelled_at|null` **(new)**, `current_period_end, cum_pnl_micro, hwm_micro, created_at`.
Dashboard cancel = "Cancel…" → modal with the two core buttons "Close positions and cancel" / "Leave positions open and cancel" → confirm → second confirm restating the consequence → step-up → DELETE (the old "type CANCEL" dialog and body-less DELETE are gone).

## Fee balance, deposits, withdrawals, payouts

| Method & path | Gate | Request | Response | Errors | Web caller |
|---|---|---|---|---|---|
| GET `/balance` | consent | — | `{fee_balance_micro, withdrawable_micro (new), withdrawals_pending_micro, estimated_monthly_need_micro, reserve_required_micro, min_topup_micro}` | — | dashboard, subscribe |
| GET `/balance/ledger` | consent | `?limit&cursor` | `Page<{tx_id, kind, memo, amount_micro (+ = balance up), created_at}>` | — | dashboard |
| GET `/deposits` | consent | `?limit&cursor` | `Page<DepositOut>` | — | — |
| POST `/deposits/stripe` 🔑 | consent | `{amount_micro}` (≥ min top-up) | 201 `{payment_intent_id, client_secret, amount_micro (estimated credit)}` | 422 below min | dashboard (Stripe Payment Element) |
| POST `/deposits/usdc/typed-data` | consent | `{amount_micro, from_address (verified), signature_chain_id}` | `{from_address, destination (= config treasury), amount_micro, time_ms, payload{typed_data, action, nonce}, exchange_url}` | 403 wallet | dashboard (checks destination = config treasury) |
| POST `/deposits/usdc/confirm` 🔑 | consent | `{from_address?, time_ms?}` (web sends the signed usdSend time) | `{credited: DepositOut[], fee_balance_micro}` | 403 | dashboard |
| GET `/withdrawals` | consent | `?limit&cursor` | `Page<PayoutOut>` (both kinds) | — | — |
| POST `/withdrawals` 🔑 | step-up | `{amount_micro, to_address (verified wallet)}` | 201 `PayoutOut {id, kind, amount_micro, to_address, status, tx_hash, created_at}` | 402 (details.withdrawable_micro) · 403 payouts disabled (reason `payouts_disabled`) / not verified · 422 below min | dashboard |
| POST `/payouts` 🔑 | step-up | `{amount_micro, source: "creator"\|"referrer", to_address}` | 201 `PayoutOut` | 402 · 403 `kyc_required` / `payouts_disabled` | creator earnings, referrals (**new UI**) |

Stripe fee (SPEC §1, owner): passed to the user. UI estimate = `ceil(amount × stripe_fee_estimate_bps / 10000) + stripe_fee_estimate_fixed_micro`; the webhook credits amount − actual fee.

## Positions, alerts, reviews, posts, referrals

| Method & path | Gate | Request | Response | Web caller |
|---|---|---|---|---|
| GET `/positions` | consent | — | `{positions:[{trading_address, coin, size (signed), entry_px, position_value, unrealized_pnl, leverage, liquidation_px}], unavailable[]}` | dashboard |
| GET `/alerts` | consent | `?limit&cursor` | `Page<{id, severity, kind, payload{}, created_at, acked_at}>` | dashboard |
| POST `/alerts/{id}/ack` | consent | — | `{ok: true}` | dashboard |
| `/alerts/settings`, `/alerts/contacts`, `/alerts/telegram/link`, `/alerts/email/*`, `/alerts/prefs`, `/alerts/test` | consent | see `routers/alerts_settings.py` (alerts module) | | pages/alerts.ts, _shared/contacts.ts |
| POST `/reviews` | consent | `{strategy_id, rating 1–5, body?}` | 201 `ReviewOut {id, rating, body, author, created_at}` (403 < 30 days subscribed) | strategy |
| GET `/posts/{id}` | consent | — | `PostOut {id, title, price_micro, strategy_slug, published_at, body|null, purchased}` | posts (signed in) |
| POST `/posts/{id}/purchase` 🔑 | consent | — | 201 `{post_id, charged_micro, fee_balance_micro}` (402; 403 needs Pro/Max; 409 free/own) | posts |
| GET `/referrals` | consent | — | `{code, link, tier, share_of_pool_bps, active_referred_users_30d, referred_notional_30d_micro, referred_users_total, earnings_payable_micro, earnings_total_micro, next_tier|null}` | referrals |

User alert kinds in `GET /alerts` (catalog `app/alerts/prefs.py`, payloads `app/alerts/user_templates.py`; Telegram +
email delivery by the worker per the email policy) now produced by the backend: `withdrawal_requested` (POST
/withdrawals), `withdrawal_sent` / `withdrawal_rejected` (admin), `new_device_login` {country?, device?, reason}
and `mfa_changed` (sign-in), `builder_approval_missing` {master, approved_tenths_bp, required_tenths_bp, where}
(subscribe refusal, builder confirm, agent confirm, daily scan), `profit_share_charged` {amount_micro, profit_micro,
rate_bps, strategy, period_start, period_end}, `subscription_renewed`, `balance_low` / `balance_empty` (every
fee-balance posting incl. settlement and USDC scans), `kyc_status` {status}.

## Creator Studio (feature flag + creator agreement consent → role creator)

| Method & path | Gate | Request | Response | Errors | Web caller |
|---|---|---|---|---|---|
| GET `/creator/strategies` | creator | — | **array** `CreatorStrategyOut {id, slug, name, status, markets, timeframe, price_monthly_micro, profit_share_bps, description, created_at}` | — | creator |
| POST `/creator/strategies` | creator | `{slug (^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$), name, description?, markets[1–5], timeframe, price_monthly_micro, profit_share_bps (≤ cap 1200)}` | 201 `CreatorStrategyOut` | 409 slug taken · 422 cap/markets | creator (slug field **added**) |
| PATCH `/creator/strategies/{id}` | creator step-up | `{name?, description?, price_monthly_micro?, profit_share_bps?}` (draft/review only) | `CreatorStrategyOut` | 409 listed | — |
| GET `/creator/strategies/{id}/versions` | creator | — | **array** `CreatorVersionOut {id, version, code_hash, published_at, live_since, params{source,…}, backtest, created_at, warning}` | — | creator (reset warning) |
| POST `/creator/strategies/{id}/versions` | creator step-up | `{source: "python", code}` or `{source: "nocode", spec}` | 201 `CreatorVersionOut` (validation + backtest run synchronously; `backtest.history_days` stamped from the candle store / backtest span) | 422 `details.errors` (validator strings or no-code `{path, message}`) | creator — uploading moves draft → review; there is **no** `/submit` or `GET …/versions/{vid}` endpoint (web polling removed) |
| POST `/creator/posts` | creator step-up | `{title, body, strategy_id?, price_micro}` (no `excerpt`) | 201 `PostOut` | 403 `kyc_required` for paid | creator |
| GET `/creator/posts` **(new)** | creator | `?limit≤100&cursor` | `Page<CreatorPostOut {id, title, price_micro, strategy_slug|null, published_at, created_at, body, sales, gross_sales_micro}>` (own posts, newest first, full bodies) | — | creator (my posts) |
| GET `/creator/earnings` | creator | — | `{payable_micro, payouts_pending_micro, total_earned_micro, by_strategy[{strategy_id, slug, active_subscribers, earned_micro, builder_share_micro, subscription_share_micro, profit_share_micro, posts_micro}], general_posts_micro, other_micro, recent: LedgerEntryOut[]}` (**per-strategy breakdown new**) | — | creator |
| POST `/creator/kyc/session` | creator | — | `{url ("" when manual), provider, status, manual}` | 409 approved · 409 reason `awaiting_admin` (provider passed, our admin confirms) | creator (manual → "reviewed by our team" notice) |

Earnings per strategy = lifetime credits to the creator's payable, attributed through the ledger: builder-fee share
(creator 50 % of the builder fee on subscribers' fills), subscription share (97 % of first periods + renewals), the
creator's profit share (the platform's 1.5 % is charged on top and is not the creator's), paid posts linked to the
strategy (price − $1). `earned_micro` = Σ of the four. Posts without a strategy → `general_posts_micro`; anything else
(manual adjustments) → `other_micro`. Σ `by_strategy[].earned_micro` + `general_posts_micro` + `other_micro` =
`total_earned_micro`. Every owned strategy is listed (zeros included).

No-code spec = exactly `backend/app/sandbox/nocode.py` (SPEC §10): `{version: 1, markets[], timeframe, lookback, max_leverage, indicators: {id: {type, source?, period, shift?}}, rules: [{when: {all|any: [{left, op, right} | nested]}, weight}], default_weight}`; operands = indicator id, price field (`close|open|high|low|volume`) or number; ops `> < >= <= crosses_above crosses_below`; weights apply to **each** market. The web's former per-coin rule format was replaced. `web/tests/nocode_contract.mjs` validates/compiles/runs web-generated specs with the Python module.

## Admin (role admin from DB; mutations step-up + audit; maker-checker)

| Method & path | Request | Response | Notes |
|---|---|---|---|
| GET `/admin/flags` | — | **array** `FlagOut {key, value, pending_value, pending_by ("admin:<uuid>"), pending_at, updated_by, updated_at}` | |
| POST `/admin/flags` | `{key, value: bool, reason (5–500)}` | `{status: "applied"\|"pending", change?}` | engage = now; lift = pending |
| POST `/admin/flags/{key}/approve` \| `/reject` | `{reason}` | `AdminActionOut` | different admin |
| GET `/admin/changes` | `?status=pending\|approved\|rejected` | `Page<ChangeOut {id, kind, target, payload, reason, status, maker_admin, checker_admin, created_at, decided_at}>` | Approvals tab (**new UI**) |
| POST `/admin/changes/{id}/approve` \| `/reject` | `{reason}` | `AdminActionOut` | strategy_list / strategy_price / user_unsuspend (KYC no longer uses the queue; a legacy `kyc_approve` entry can only be rejected) |
| GET `/admin/payouts` | `?kind=withdrawal\|payout&status=` | `Page<AdminPayoutOut {id, kind, beneficiary, amount_micro, to_address, status, maker_admin, checker_admin, tx_hash, created_at}>` | web loads both kinds |
| POST `/admin/payouts/{kind}/{id}/approve` | — | `AdminPayoutOut` | 1st then 2nd (different) admin |
| POST `/admin/payouts/{kind}/{id}/reject` | `{reason}` | `AdminPayoutOut` | releases hold |
| POST `/admin/payouts/{kind}/{id}/typed-data` | `{signature_chain_id}` | `{payout, payload{typed_data, action, nonce}, exchange_url}` | web **refuses unless the connected wallet = config `treasury_address`**, validates destination/amount |
| POST `/admin/payouts/{kind}/{id}/sent` | `{tx_hash, time_ms}` | `AdminPayoutOut` | web finds the hash in the treasury's `userNonFundingLedgerUpdates` (or asks); server verifies on-chain |
| GET `/admin/strategies` | `?status=review\|listed\|paused\|draft\|delisted` | `Page<AdminStrategyOut {…, owner_user_id, owner_kyc_status, versions: CreatorVersionOut[] (5)}>` | |
| POST `/admin/strategies/{id}/list` | `{version_id, reason}` | pending change | approval checks price set, creator KYC, **≥ risk.min_listing_history_days (180)** (was a hard-coded 365) |
| POST `/admin/strategies/{id}/pause` \| `/delist` \| `/reject` | `{reason}` | applied | |
| POST `/admin/strategies/{id}/price` | `{price_monthly_micro, reason}` | pending (in-house only) | web's PATCH /admin/strategies/{id} did not exist |
| GET `/admin/users` | `?q (≥3)` | `Page<AdminUserOut>` | |
| POST `/admin/users/{id}/suspend` \| `/unsuspend` | `{reason}` | applied / pending | |
| POST `/admin/users/{id}/kyc` | `{decision: approved\|rejected, reason}` | `AdminActionOut {status: "applied"}` | **ONE admin** (owner 30 Sep 2026), step-up + audit. Manual provider: pending/rejected → approved. Sumsub: only `provider_approved` (provider GREEN; never auto-approved) → approved, else 409 reason `provider_not_approved`. 403 own KYC. 409 already approved/rejected. Rejection immediate. Listing and payouts keep two admins. |
| GET `/admin/alerts` | `?severity=&unacked=&ops_only=` | `Page<AlertOut>` | |
| POST `/admin/alerts/{id}/ack` | — | `{ok}` | |
| GET `/admin/reconciliation` | — | `{report: ReconcileReport.as_dict()|null, generated_at}` | report = `{positions_checked, drifts[], builder_db_micro, builder_chain_micro, builder_mismatch, treasury_ledger_micro, treasury_chain_micro, treasury_mismatch, errors[]}` |

Webhooks (`/webhooks/stripe`, `/webhooks/telegram`, `/webhooks/kyc`) and `/internal/*` (executor service, Scheduler OIDC) are server-to-server and not called by the web.

## Mismatches fixed (summary)

| Area | Web guessed | Now |
|---|---|---|
| Lists | `{strategies:[…]}`, `{rows}`, `{posts}` … | `Page.items` everywhere (plain arrays where noted) |
| Strategy stats | `roi_pct, pnl_micro, subscribers` top-level | `stats{roi_bps, pnl_micro, subscribers, hidden_reason}` |
| Strategy extras | `signal_state, live_days, max_leverage, risk_ack_text, current_version{…}, equity` | backend adds signal_state, live_days, not_live_proven, max_leverage, history/short_history_days, free_showcase/showcase_text, risk_ack_text; current_version is an int; live equity series at GET `/public/strategies/{slug}/equity` (k-anonymous) |
| Consents | no hash; docs posted without evidence | `doc_text_sha256` required and verified; file-stem ↔ doc-key mapping fixed on the server |
| Agent create | `{master_address, trading_address}` → `{id, agent_address, typed_data}` | `{master_address, signature_chain_id}` → `{agent{…}, approve_agent{typed_data}}` |
| Subscribe | extra `agent_id` (422); min $10; balance ≥ price | no agent_id; expected terms sent; min $100; balance ≥ price + reserve; reasons handled |
| Cancel | "type CANCEL" + body-less DELETE (422) | two buttons + double confirm + step-up + `{positions}` |
| Balance | `balance_micro`, ledger inside `/balance` | `fee_balance_micro`, `withdrawable_micro`; ledger at `/balance/ledger` |
| USDC deposit | no `signature_chain_id`; confirm `{deposit_id, amount_micro}` (422) | typed-data with chain id + destination check; confirm `{from_address, time_ms}` |
| Stripe fee | `stripe_fee_estimate{pct_bps}` (backend read a non-existent setting → always null) | `stripe_fee_estimate_bps / _fixed_micro` from config |
| Positions | `szi` | `size`; `unavailable[]` shown |
| Posts | `/public/posts/{id}` (missing), `excerpt`, `creator_name` | new public route; `preview`, `creator_display_name`; `/posts/{id}` when signed in; no `excerpt` on create |
| Referrals | `active_users_30d, payable_micro, tiers, history` | ReferralsOut names; tiers from public config; payout request added |
| Creator | no slug (422); `/versions/{vid}`, `/submit`, `/creator/posts` GET (missing) | slug added; upload result used directly; GET `/creator/posts` (own posts) and per-strategy earnings added |
| Admin | `/flags/pending/{id}`, `/payouts/{id}`, `/strategies/{id}/review`, PATCH strategy, `?in_house=` | real routes incl. Approvals queue, kinds, reasons, KYC decisions |
| No-code | per-coin indicators/rules, `c/h/l/v` sources | nocode.py format (cross-tested) |
| Earnings | `earned_micro` required but never filled (500) | optional (null) |

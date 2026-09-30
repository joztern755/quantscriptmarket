# Security review: authentication, authorisation and API surface (backend/app/api)

Reviewer: application-security review (read-only) · Date: 30 Sep 2026 · Scope: `backend/app/api/**` (main, deps,
middleware, adapters, store, validation, ledger_ops, login_events, routers/*), `backend/app/security/{auth,audit,ratelimit,csp}.py`,
`backend/app/kyc/**`, migrations 0001–0008 (roles and grants), `infra/gcp/run/api.service.yaml`, `infra/cloudflare/dns.sh`.
Method: I read the code line by line and reasoned about it statically. For the billing finding I ran a stdlib PoC against `app.domain.billing`.
The FastAPI TestClient PoCs against `app/api/testing.py` could not run: FastAPI is not installed and PyPI is blocked by
the sandbox egress policy (403). Each exploit below is traced through the exact code path instead. Another agent was editing
files at the same time, so line numbers are as of this read.

## Summary table

| # | Sev | Title | Where |
|---|-----|-------|-------|
| F1 | **High** (needs confirmation of HL limits) | Any user can use up the Hyperliquid per-IP budget that the executor shares, which delays exits | `routers/deposits.py:104`, `schemas.py:567`, `adapters.py:395-402`, `routers/positions.py:24-36`, `infra/gcp/bootstrap.sh:135-143` |
| F2 | **High** | Routes that move money to a third party have no step-up: a stolen session can drain the fee balance into an attacker's creator payable | `routers/posts.py:40-42`, `routers/me.py:91-92`, `routers/alerts_settings.py:129-130` |
| F3 | Medium | Pause then unpause resets `reduce_only`/`past_due` to `active`. This bypasses non-payment enforcement and restarts the 72 h grace period indefinitely | `routers/subscriptions.py:228-236` |
| F4 | Medium | Card-funded balance can become withdrawable USDC (the withdrawable calculation ignores spending, and creator earnings bought with card funds have no dispute hold) | `store.py:593-600`, `routers/withdrawals.py:66-70`, `ledger_ops.py:131-143` |
| F5 | Medium | No cooling-off period: a wallet verified seconds ago, or a sign-in after an MFA change or from a new device, can withdraw at once. Admins approving get no context | `routers/wallets.py:64-67`, `routers/withdrawals.py:62-70`, `login_events.py:106-116`, `routers/admin.py:222-226` |
| F6 | Medium | Maker-checker approvals run against stale state: a delisted or paused strategy is re-listed when an old `strategy_list` change is approved, and the reviewed terms are not pinned | `routers/admin.py:144-171, 367-378, 381-398`, `routers/creator.py:130-145` |
| F7 | Medium | Self-referral controls do not work (the device check is never populated, the wallet check can never fire, and binding at creation is unchecked) | `routers/me.py:77-80`, `deps.py:459-474`, `routers/wallets.py:72-81` |
| F8 | Low | USDC crediting has an unbounded, user-controlled lookback (back to 2020) and is not tied to when the wallet was verified | `routers/deposits.py:104`, `schemas.py:567` |
| F9 | Low | Payout `tx_hash` is not unique across rows, so two payouts can race to be settled against one on-chain transfer | `routers/admin.py:322-339`, `store.py:698-701`, `0001_init.sql:878,904` |
| F10 | Low | The public leaderboard is expensive to compute and never cached (Cloudflare bypasses cache for the API) | `routers/public.py:199-219`, `dns.sh:242-246` |
| F11 | Low | Geo gate lets `XX`/missing country and Worker-originated requests through. Geo-IP is weak by nature | `middleware.py:195-213` |
| F12 | Low | Admin hardening gaps: no separate admin allowlist, PII reads are not step-up or audited, and one admin can suspend the other (maker-checker deadlock) | `deps.py:580-589`, `routers/admin.py:434-455` |
| F13 | Low | The admin override in `_owned` lets one admin upload code, change terms or attach posts on any creator's strategy | `routers/creator.py:95-99` |
| F14 | Low | Review eligibility counts cancelled subscriptions ("30 days since first subscribe", not "30 days subscribed") | `store.py:518-522`, `routers/reviews.py:26-28` |
| F15 | Low | A same-day re-upgrade to a plan is silently not charged, but the response and audit say it was | `ledger_ops.py:121-128`, `routers/me.py:106-115` |
| F16 | Low | The public posts list shows posts tied to unlisted or draft strategies and to suspended creators | `store.py:402-412` |
| F17 | Info | `app_api` can UPDATE any column of `users` (including `role`); there is no DB guard against `role='admin'` | `0002_roles.sql:103` |
| F18 | Info | Admin user search passes LIKE wildcards through unescaped; SIWE `Not Before` is ignored; `_create_user` stores the Google `name` uncleaned | `store.py:186-192`, `routers/wallets.py:42-60`, `deps.py:480` |
| F19 | Info | A Firebase revocation check (Auth backend call) runs on every authenticated request. Plan for quota and latency at 30k users | `security/auth.py:307`, `adapters.py:166-176` |

No Critical findings. There is no SQL injection, no IDOR on any id-taking route, and no way to bypass MFA or step-up where step-up is applied.

---

## Findings

### F1 High: user-triggered Hyperliquid calls can starve the executor (availability of exits, asset A9)
*Needs confirmation of HL's current per-IP weights. SPEC §6 says the rate limits are unverified.*

**Where.** `api` and `executor` send all egress through one Cloud NAT static IP (`infra/gcp/bootstrap.sh:135-143`, both
service yamls `vpc-access-egress: all-traffic`). Hyperliquid meters REST `/info` and `/exchange` weight **per IP**. Several
user routes make HL calls on demand:
- `POST /v1/deposits/usdc/confirm` (`routers/deposits.py:93-105`) calls `svc.usdc.detect()`. That calls `ledger_updates(treasury, start)`
  (`adapters.py:395-402`), which pulls the **treasury's entire non-funding ledger** from `start`. `start` = `body.time_ms − 5 min`, and
  `time_ms` is client-supplied with a floor of `1_600_000_000_000` (Sep 2020, `schemas.py:567`). Limit: 10/min per user **per instance**
  (in-memory buckets, `adapters.py:591-619`; up to 20 api instances).
- `GET /v1/positions` (`routers/positions.py:24-36`) makes up to 10 addresses × N dexes `clearinghouseState` calls, 30/min per user per instance.
- Also `POST /agents/{id}/confirm`, `/builder-approval/confirm` and `POST /subscriptions` (`max_builder_fee`, `master_of`), and `POST /creator/strategies` (meta).

**Exploit.** One allow-listed or public user loops `POST /deposits/usdc/confirm {"time_ms":1600000000000}` with fresh
Idempotency-Keys, spread across instances. Each call downloads and parses the treasury's full history (heavy HL weight, large
response, CPU on the API). With a few accounts, the shared NAT IP hits HL 429s. The executor's `/exchange` orders, including
reduce-only exits and kill-switch flattening, then fail or back off. That is the JELLY-style crisis the product promises to exit from.

**Fix.**
1. Give the executor its own egress IP: a separate subnet and NAT with its own static IP. HL budget for trading must never be shared with user-triggered reads.
2. Clamp the `usdc/confirm` lookback server-side: `start = max(now − 48 h, wallet.verified_at − 1 h, time_ms − 5 min)`, and ignore
   `time_ms` older than 48 h. Better: have `confirm` only enqueue or wake the `deposits-scan` job (single-flight, cached cursor)
   and return the credits that job has already found.
3. Put one shared (Redis or DB) token bucket in front of *all* API-originated HL calls, with a global ceiling below HL's
   per-IP budget. Cache `clearinghouseState` per address for 10–30 s, and `max_builder_fee` and `master_of` for about 60 s.
4. Move per-user limits on HL-backed routes to a shared store. Per-instance buckets multiply the limit by the instance count.

### F2 High: value-transfer routes lack step-up, so a stolen session can pay an attacker
**Where.** `POST /v1/posts/{id}/purchase` (`routers/posts.py:40-42`) and `POST /v1/me/plan` (`routers/me.py:91-92`) only
require `consented_user`, meaning any MFA session up to an hour old or refreshed silently. `POST /v1/alerts/telegram/link` (`alerts_settings.py:129-130`)
lets the session re-point the Telegram alert chat (`telegram_bot._link` overwrites `telegram_chat_id`) without step-up.
Changing the email *does* require step-up. SECURITY.md §3.2 relies on step-up as the control against a stolen ID token.

**Exploit.** The attacker is a KYC-approved creator (or has a colluding one). They publish a paid post with no price cap
(`domain/fees.py:102-108`). From a stolen victim session (XSS, malware or a leaked token; no fresh sign-in needed) they:
1. `POST /me/plan {"plan":"pro"}` ($20). Paid posts need Pro or Max.
2. `POST /alerts/telegram/link`, then `/start <token>` from their own Telegram. This moves the victim's Telegram alerts to the attacker; email still receives mandatory alerts.
3. `POST /posts/{attacker_post}/purchase`. The victim's fee balance (USDC-funded, real money) is debited and `creator:{attacker}:payable` is credited with price − $1 (`ledger_ops.py:131-143`).
4. The attacker requests a payout. The two admins see an ordinary creator payout with no link to the victim.

**Fix.** Put `step_up_user` on `POST /posts/{id}/purchase` and `POST /me/plan` (the web already retries with step-up on 401
`step_up_required`). Put it on `POST /alerts/telegram/link` whenever a chat is already linked, and send an `alert_contacts_changed` mandatory
alert to the *old* chat and the email. Defence in depth: set a maximum post price (e.g. $500), and a per-user daily
spend limit on non-subscription purchases that needs step-up above it.

### F3 Medium: pause and unpause launders `reduce_only`/`past_due` back to `active`
**Where.** `routers/subscriptions.py:228-236`. `paused=true` is allowed from any `_CHANGEABLE` status, including `past_due`
and `reduce_only`. `paused=false` then sets `status="active"` without checking the balance or `past_due_since`, and without re-checking
the strategy's status or the builder approval. `update_subscription` does not clear `past_due_since`.

**PoC (stdlib, `app.domain.billing`).** This reproduces the router's transition, then the settlement:
```
settlement on reduce_only: reduce_only entries_allowed: False
after pause/unpause: active entries_allowed: True
next settlement: past_due 2026-10-01 20:00:00+00:00 entries_allowed: True   # fresh 72 h grace
71 h later still entries_allowed: True
```
**Impact.** A user with an empty or negative fee balance keeps opening positions forever by toggling pause every ~3 days.
Profit share and renewals are never collected (revenue loss; the receivable grows). The same toggle undoes the `reduce_only` that
`delist` applies (`store.pause_subscriptions_of_strategy`). That is partly mitigated because signals are only read for listed or paused strategies.

**Fix.** Store the pre-pause status and `past_due_since`, and restore them on unpause. Or run `billing.next_status(prev, balance, due,
past_due_since, now)` on unpause and refuse or keep `reduce_only` when entries are not allowed. Refuse to unpause when the strategy is not
`listed`. Add a test for pause→unpause from `reduce_only`.

### F4 Medium: card-funded balance can be turned into USDC
**Where.** `store.withdrawable_usdc` (`store.py:593-600`) = Σ credited USDC deposits − Σ non-rejected withdrawals. It never
subtracts what was *spent*. `request_withdrawal` allows `amount ≤ min(balance, withdrawable_usdc)` (`routers/withdrawals.py:66-70`).
**Exploit.**
- (a) Deposit 1,000 USDC and spend it on plans or subscriptions (e.g. a colluding creator's strategy, where 97 % goes to the creator payable). Top up 1,000 by card (stolen). `withdrawable = min(1000, 1000 − 0) = 1000`, so withdraw 1,000 USDC. The card money leaves as USDC, and the chargeback lands later on the platform.
- (b) Directly: card top-up, then buy a KYC'd colluder's post (no price cap) or subscribe to their strategy, then a creator payout. Nothing holds earnings funded by card while they are inside the dispute window (up to 120 days).

**Fix.** Track funding source in the ledger. Keep `user:{id}:fee_balance` split into lots (or two sub-accounts), spend card lots
first *at the moment of spending*, and compute withdrawable from the USDC lot. A conservative interim rule:
`withdrawable = max(0, balance − Σ(card credits − card refunds/disputes))`. For creator and referrer earnings, set a `payable_after`
= purchase time + dispute window on the part funded by card, and exclude it from `available` in `POST /payouts`.

### F5 Medium: no cooling-off period after a new payout wallet, an MFA change or a new device
**Where.** `POST /wallets/verify` binds a new destination immediately (`routers/wallets.py:64-67`). `POST /withdrawals` and `/payouts`
accept it straight away (`routers/withdrawals.py:62-70, 101-104`). `login_events` raises `mfa_changed` and `new_device_login` but
does not hold anything (`login_events.py:90-116`). SECURITY.md §5 and §3.9 promise a 48 h hold on payout-address changes and after MFA reset [DESIGN].
The admin payout view (`AdminPayoutOut`, `routers/admin.py:222-226`) does not show wallet age or recent security events.
**Exploit.** An account takeover that includes the TOTP seed (e.g. Google Authenticator seeds synced to the same Google account)
lets the attacker step up legitimately, verify their own wallet and withdraw the USDC-funded balance in minutes. The two admins see
a routine request.
**Fix.** Refuse (403 `reason: payout_address_hold`) withdrawals and payouts to a wallet whose `verified_at` is less than 48 h ago, and every
withdrawal or payout for 48 h after `mfa_changed` (store `users.security_hold_until`). Show `to_address_verified_at`, the latest
`new_device_login`, `mfa_changed` and `alert_email_changed` in `AdminPayoutOut`, and block approve_2 when the hold is active.

### F6 Medium: maker-checker approvals execute against stale state
**Where.** `list_strategy` refuses `delisted` only when the change is *proposed* (`routers/admin.py:375-376`). `_apply_change` for
`strategy_list` (`admin.py:146-171`) never checks the current `st["status"]` and unconditionally `publish_version` + `set_strategy_status('listed')`.
`_set_status_now` (delist, pause, reject; `admin.py:381-398`) does not cancel pending `admin_changes` for that strategy. The
proposal payload pins only `version_id`, not price or profit share, and the creator can still `PATCH` terms while the status is `review`
(`routers/creator.py:130-145`).
**Exploit.**
1. Admin A proposes listing S@v3. Admin C finds malicious behaviour and delists S (one admin, immediate).
2. Admin B later approves A's still-pending change. S is `listed` again and new users can subscribe, bypassing "delisted cannot be relisted".
3. Separately, the creator raises the price and profit share (up to the cap) between A's review and B's approval. B approves terms nobody reviewed.

**Fix.** In `_apply_change('strategy_list')`, require `st.status in ('review','listed')` and compare `(price_monthly_micro,
profit_share_bps, owner_user_id)` with values snapshotted into the proposal payload at propose time (409 if they differ). In
`_set_status_now` for delist, pause and reject, auto-reject pending `admin_changes` for `strategy:{id}`. Lock terms (409) while a
`strategy_list` change is pending.

### F7 Medium: self-referral controls do not work
**Where.**
- `users.device_fp_hash` is never written anywhere (grep: only read in `routers/me.py:78-80`), so `same_device` can never match.
- `wallets.master_address` is globally unique, so a referrer and a referee can never share a wallet and `same_wallet` can never match (`routers/me.py:77-80`, `routers/wallets.py:72-81`).
- Binding at account creation from `X-Ref-Code` (`deps.py:459-474`) runs no check at all beyond the DB `referred_by <> id` constraint.

**Exploit.** One person creates a second Google account with TOTP and a fresh wallet, signs up with `X-Ref-Code: <own code>` and
trades. Their own referrer account receives 50–100 % of the 0.02 % referral pool: a self-rebate of up to 20 % of the builder fee.
Farming the "Elite" tier by volume on sockpuppets is also possible. SPEC §1.2 requires this block.
**Fix.** Use the device table that exists (`user_devices`, 0008). Flag or deny binding when the referrer and referee share any
`device_hash`, `ip_hash` (same /24 or /64 within 30 days), or trading master (`master_of` / sub-account relations). Run this at creation *and*
at wallet verification. Re-evaluate daily in the referral-tiers job, and pay no referral share for pairs flagged `self_referral_suspected`
until ops clears them. Remove the dead `device_fp_hash` column or populate it.

### F8 Low: USDC crediting not bounded by when the wallet was verified
`POST /deposits/usdc/confirm` scans treasury transfers from the user's verified wallets since any `time_ms` ≥ 2020
(`routers/deposits.py:104`). A historical `usdSend` from wallet W to the treasury gets credited, as withdrawable balance, to whoever later
verifies W, even if the transfer was never a deposit (e.g. an ops or officer wallet that funded the treasury). **Fix:** only
credit transfers with `time ≥ wallet.verified_at − 24 h` (or ≥ the time the typed data was issued for that wallet). Route older ones
to manual review. Clamp the lookback as in F1.

### F9 Low: payout `tx_hash` reuse race
`payout_sent` checks `tx_hash_used` in one transaction, verifies on-chain outside it, and marks the payout sent in a second transaction
without re-checking (`routers/admin.py:322-339`). `withdrawals.tx_hash` and `payouts.tx_hash` have no unique index (`0001_init.sql`).
Two identical requests (same user, amount and address) can both be marked sent against one transfer, and the ledger then shows a USDC
outflow that never happened. **Fix:** add a unique index on `tx_hash` in both tables (or one `payout_tx_hashes(tx_hash PK)` table
that both insert into inside the settling transaction), and re-check inside the second transaction.

### F10 Low: expensive public leaderboard
`GET /public/leaderboard` runs `track_record_inputs` for up to 200 strategies per request (`routers/public.py:199-219`). Cloudflare
bypasses cache for the whole API host (`dns.sh:242-246`), so `Cache-Control: public, max-age=60` has no effect. **Fix:** memoise per
`(by, period)` for 60 s in process or DB, or let Cloudflare cache `GET /v1/public/*` (keyed without auth headers).

### F11 Low: geo gate edges
`EdgeGuardMiddleware` blocks restricted countries and `T1` only (`middleware.py:198-213`). `XX` (unknown) and a missing
`CF-IPCountry` pass. Requests relayed through Cloudflare Workers carry `CF-Worker` and geolocate to the Worker's egress. VPNs defeat
geo-IP anyway, so the self-attested jurisdiction plus terms are the real control. **Fix (cheap):** treat `XX` like `T1` for
authenticated money routes, block requests that carry `CF-Worker` at the WAF, and record `edge_country` on the money-moving audit events
(it is recorded only on consent today).

### F12 Low: admin hardening
- `admin_user` checks only the DB role (`deps.py:580-583`). SECURITY.md promises a separate admin allowlist [DESIGN]. Add an
  `ADMIN_EMAILS` or `ADMIN_UIDS` secret that must also match.
- Admin GETs (`/admin/users` with emails, `/admin/payouts`, `/admin/alerts`) do not require step-up and are not audited. Add
  audit rows for user search.
- `suspend_user` lets admin A suspend admin B immediately (`admin.py:445-455`). With two admins, B cannot be unsuspended
  (it needs a *different* second admin) and kill-switch lifts or payouts become impossible. Require maker-checker to suspend another admin, or forbid it.

### F13 Low: admin override in creator routes
`_owned` accepts any non-in-house strategy when `ctx.role == 'admin'` (`routers/creator.py:95-99`). A single admin can upload a
version into a third party's strategy, change a draft's price, or attach posts to it. Listing still needs two admins. **Fix:** drop
the admin bypass. Admin actions on creator strategies belong in `/admin/*`, audited as admin.

### F14 Low: review eligibility
`earliest_subscription` counts any non-`pending` subscription, including ones cancelled after a minute (`store.py:518-522`). A
user who subscribes and cancels at once can review 30 days later. **Fix:** require ≥ 30 days of `active`/`past_due`/`reduce_only` time.

### F15 Low: plan re-upgrade on the same day is silently free
The `charge_plan` key is `plan:{user}:start:{plan}:{date}` (`ledger_ops.py:125`). Going max→free→max on the same day posts identical
content, so the ledger returns the existing transaction and charges nothing. The route still reports `charged_micro=price` and audits it
(`routers/me.py:106-115`). The financial impact is small, but the audit is wrong. **Fix:** include a unique per-change component
(e.g. the Idempotency-Key or a `plan_changes` row id) in the key, or detect `created=false` and report 0.

### F16 Low: public posts list over-exposes
`list_public_posts` (`store.py:402-412`) does not filter on the strategy's status or the creator's `status`. It shows slugs of draft or
unlisted strategies and posts by suspended creators. **Fix:** join and require `st.status IN ('listed','paused') OR p.strategy_id IS NULL`
and `u.status='active'`.

### F17 Info: DB defence in depth for `users.role`
`GRANT SELECT, INSERT, UPDATE ON users TO app_api` (`0002_roles.sql:103`) is table-wide. Any future injection or logic bug could set
`role='admin'`. **Fix:** a `BEFORE UPDATE` trigger that refuses `NEW.role='admin' AND OLD.role<>'admin'` unless `current_user` is
`app_migrator`, or column-level UPDATE grants.

### F18 Info
- `search_users` passes `%`/`_` through unescaped into `LIKE` (admin only; `store.py:186-192`).
- SIWE `Not Before` is parsed but not enforced (`routers/wallets.py:42-60`).
- `_create_user` stores Google `name` (≤ 64 chars) without `_clean_line` (`deps.py:480`); the web must escape it.

### F19 Info
`FirebaseAdminVerifier` makes one Auth backend call per request (`check_revoked=True`, `security/auth.py:307`). At 30k users,
budget for the quota and latency. Consider checking revocation only on step-up or money routes, plus a short (60 s) per-uid cache.

---

## Route-by-route gate check (money and security routes)

| Route | Gate in code | Expected (SPEC §5.2 / SECURITY §5) | OK? |
|---|---|---|---|
| POST /wallets/nonce | consented | consented | ✓ |
| POST /wallets/verify | step-up | step-up (payout address) | ✓ (no 48 h hold, F5) |
| POST /agents, POST /agents/{id}/confirm | step-up | step-up | ✓ |
| POST /builder-approval/confirm | consented | read-only on-chain check | ✓ |
| POST/PATCH/DELETE /subscriptions | step-up (+Idem on POST) | step-up | ✓ (F3 logic) |
| POST /deposits/stripe, /usdc/typed-data, /usdc/confirm | consented (+Idem) | money in; no step-up needed | ✓ (F1/F8) |
| POST /withdrawals, POST /payouts | step-up + Idem + payouts flag | step-up | ✓ (F4/F5) |
| **POST /me/plan** | consented + Idem | charges balance, should be step-up | ✗ F2 |
| **POST /posts/{id}/purchase** | consented + Idem | pays a third party, should be step-up | ✗ F2 |
| **POST /alerts/telegram/link** | consented | re-routes mandatory alerts, should be step-up | ✗ F2 |
| POST /alerts/email/start | step-up | step-up | ✓ |
| POST /alerts/email/confirm-account | step-up when switching back | step-up | ✓ |
| POST /creator/strategies | creator | draft only | ✓ |
| PATCH /creator/strategies/{id}, POST …/versions, POST /creator/posts | creator step-up | step-up | ✓ |
| All admin mutations (flags, changes, payouts ×4, strategies ×5, users ×3, alerts ack) | admin_step_up | step-up + role | ✓ |
| Admin GETs | admin (no step-up) | acceptable | F12 |
| /internal/* | executor only, OIDC aud + iss + SA email + email_verified | same | ✓ |
| /webhooks/stripe, /telegram, /kyc | HMAC / secret token / HMAC + refetch | same | ✓ |

## Verified as correct (brief)

- **Firebase verification** (`security/auth.py`): RS256 only (alg none and HS confusion refused), kid lookup, iss and aud equal to the project,
  `exp`, `iat` and `auth_time` sanity against an injectable clock, 1 h max lifetime, tenant tokens refused, provider allowlist
  {google.com, apple.com}, TOTP-only second factor (SMS refused), a prod chain with firebase-admin `check_revoked`, and the two verifiers must
  agree on uid and auth_time. Emulator tokens are always refused. Checked again in `deps.check_mfa_claims`. Step-up = auth_time ≤ 300 s + TOTP.
- **Admin role** comes from our DB only, never from token claims. Admin routes inherit `consented_user` (active and not suspended).
- **Launch allowlist** is enforced on *every* authenticated request, before user creation, and needs `email_verified`. Prod config
  fails closed without `ALLOWLIST_EMAILS` in the internal phase. Payouts are off by default in prod.
- **IDOR**: every id-taking user route is scoped by `user_id` in SQL: subscriptions get, patch and delete (`WHERE s.user_id`), agents
  confirm, alerts ack, posts (unpublished only to the owner; paid body only if purchased, own or admin), reviews (own subscription), creator
  strategies, versions and posts (`_owned`), withdrawal and payout destinations (the user's verified wallet), USDC credit (credit only when
  `instr.user_id == ctx.user_id`), positions (own addresses only), KYC (self; admin decision separate). Agent creation refuses a
  master that another user has a live agent on. The subscription trading address must be the user's verified wallet or its on-chain sub-account.
- **Maker-checker (payouts)**: FOR UPDATE, then conditional `UPDATE … WHERE status=… AND maker_admin <> :a`, plus DB CHECKs
  (`four_eyes`, `not_self_approved`, `checker_set`). A double-approve race or self-approval is impossible. Flag lift: `pending_by ≠ approver`.
  Changes: the `admin_changes` trigger makes proposals immutable, with a unique pending entry per target.
- **Idempotency**: PK `(user_id, idem_key)` (no cross-user reuse), a fingerprint over method + scope + normalised body (post id in scope),
  the claim sits in the same transaction as the business write (rollback on error), stored responses are final (trigger), and the Stripe
  idempotency token is derived from (user, key).
- **Money race safety**: `lock_user` or `get_user FOR UPDATE` before every balance check and post. Holds are posted at request time.
- **Webhooks**: Stripe signature over the raw body, several v1 signatures, a ±300 s window, constant time, idempotent ledger keys.
  Telegram: constant-time secret, an unset secret rejects everything, and the WAF restricts sources to Telegram IP ranges. KYC: HMAC-SHA256/512
  digest (SHA-1 refused), verdict re-fetched from the provider API, a sandbox notification is ignored in prod, and GREEN is never auto-approved.
- **Internal routes** are mounted only on the executor app (ingress internal), use Google OIDC with audience = executor URL, issuer
  checked, SA email + `email_verified`, and the WAF blocks `/v1/internal/` on the api host.
- **Edge trust**: `X-Edge-Auth` is compared in constant time, and `CF-IPCountry` / `CF-Connecting-IP` are used only when it matches. In prod,
  non-exempt `/v1/*` without it gets 403. The exempt-path match is exact, so a path trick cannot turn a normal route into an exempt one.
  Cloudflare `set` overwrites any client value. Cloud Run ingress = internal + LB.
- **CORS**: a single origin, no credentials, explicit methods and headers.
- **SQL**: every value in `store.py` is a bound parameter. The only f-strings splice module constants. No string-built SQL elsewhere in the API.
- **Mass assignment**: every request model has `extra="forbid"`. There are no role, plan, status, owner or in_house fields in user schemas. Money uses
  strict ints or exact decimal strings.
- **Errors and logging**: 5xx is generic with no details, 422 echoes locations and messages only, OpenAPI is off in prod, and the access log has
  no query strings or bodies. Emails in audit are masked. IPs, UAs, devices and MFA factor ids are HMAC-peppered.
- **Wallet proof (SIWE)**: domain and URI equal the web origin, the address matches, the nonce is 24 random alphanumerics, user-bound, single-use
  (atomic UPDATE … RETURNING), 10 min, Issued-At freshness, and `Expiration Time` honoured. Low-s is enforced in the fallback recovery.
- **Referral**: binding only within 30 days, immutable (DB trigger), and `referred_by <> id` (DB CHECK). The effectiveness gaps are in F7.
- **DB roles**: `app_api` has no SELECT on `agent_keys.key_ciphertext` or `strategy_versions.code_ciphertext` (column grants), no
  UPDATE/DELETE on ledger, audit or consents, and nobody gets DELETE.

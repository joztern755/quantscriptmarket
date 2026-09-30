# Prompt to paste into your LOCAL Claude Code chat

Open a terminal on your own computer, `cd` to an empty folder, start `claude`, and paste everything between the lines.

---

You are finishing and deploying **aijalon.trade**, a Hyperliquid strategy marketplace that will handle real money. The code is written; your job is to install, test, fix what fails, set up the cloud, and deploy — safely, step by step, with me approving every irreversible or money-related step.

**Repo:** `joztern755/quantscriptmarket`, branch `claude/gifted-ptolemy-mvxlgx`.
```
gh repo clone joztern755/quantscriptmarket && cd quantscriptmarket && git checkout claude/gifted-ptolemy-mvxlgx
```

**Always `git pull` on that branch before each phase** (the cloud session may still be pushing final fixes; if `git log -1` is older than the last message from the cloud session, pull again).

**Read first, in this order:** `docs/SPEC.md` (the contract; §12 = my latest decisions), `docs/DEPLOY.md` (the step-by-step deploy guide — follow it), `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/GO_LIVE_CHECKLIST.md`, `docs/API_CONTRACT.md`, `legal/README.md`.

## Hard rules (never break)
1. **No assumptions.** Verify every value from real data, docs or the live system. If unsure, stop and ask me. Before starting, list every question you have and wait for my answers.
2. **Never paste or print secrets** (keys, tokens, passwords, private keys, seed phrases) in chat, commits, logs or issues. Read them with `read -rs` and pipe straight into Secret Manager, exactly as `docs/DEPLOY.md` §5 shows. I type them, not you.
3. **Never touch real funds without asking me first.** You never hold or type a wallet private key. The builder and treasury wallets stay on my hardware wallet.
4. **Never skip, disable or weaken a test, a security check, or a CI gate** to get green. Fix the cause.
5. **Ask before every irreversible or outward step:** creating billing, deploying to production, changing DNS, enabling the scheduler (`make go-live` = trading starts), sending anything to users.
6. Commit to branch `claude/gifted-ptolemy-mvxlgx` with clear messages; merge to `main` only when I say so (pushing `main` triggers the production deploy workflow, which also needs my approval in GitHub).
7. Launch phase is **internal** (`LAUNCH_PHASE=internal`): only allow-listed emails (`ALLOWLIST_EMAILS` secret), payouts off (`PAYOUTS_ENABLED=false`) — until I say "public". There are **no** allocation or leverage caps (my decision, SPEC §12 "No caps": `MAX_*` = 0); the liquidity and other pre-trade guards still apply.

## Phase A — make it build and pass locally
1. Install tools from `docs/DEPLOY.md` §1 (gcloud, firebase-tools, node 22, TypeScript, Python 3.12, psql 16, cloud-sql-proxy, gh, docker).
2. `make lock` (hash-locks Python deps), `make pin` (pins images/actions), `make venv`.
3. Run everything and fix failures properly: `make lint`, `make test` (this is the first time the FastAPI HTTP tests, the Hyperliquid SDK and the Stripe/Firebase libraries run — expect some real fixes), `backend/tests/db/run_db_tests.sh` against a local Postgres 16, `node signals/test.js`, `make web`, `node web/tests/smoke.mjs`, `make validate`, `make csp-check`, `make audit`.
4. **SDK parity checks (money-critical):** verify with the installed `hyperliquid-python-sdk` + `eth_account` that our typed data for ApproveAgent, ApproveBuilderFee and UsdSend (`backend/app/hl/typed_data.py` and `web/src/core/hl.ts`) produce signatures the SDK would produce for the same inputs; verify builder-dex asset ids (`xyz:SILVER` = 110026) against the SDK's own mapping; verify `maxFeeRate` string format. Write these as tests.
5. Push, and make CI (`.github/workflows/ci.yml`) green on GitHub.

## Phase B — cloud setup (follow `docs/DEPLOY.md` §2–§9 exactly)
- Google account **app.aijalon@gmail.com** owns everything. Ask me to turn on 2-Step Verification with a security key and to add billing (Blaze). Project id per DEPLOY.md.
- `make bootstrap`, then secrets (§5), GitHub environment + reviewers (§6), Firebase Auth with **Google + Apple sign-in and TOTP 2FA** (§7), Cloudflare DNS/TLS/WAF for **aijalon.trade** and **api.aijalon.trade** (§8; I create the Cloudflare API token), `make db-bootstrap` (§9).

## Phase C — alerts: Telegram bot + email (Resend) (`docs/DEPLOY.md` §5.1, §5.2, §10.1)
**Telegram bot (all alerts go here):**
1. Ask me to open Telegram → @BotFather → `/newbot` → name "aijalon alerts", username e.g. `aijalon_alerts_bot` → BotFather gives a token. I paste the token into the `read -rs` prompt you give me; you pipe it into Secret Manager `TELEGRAM_BOT_TOKEN`. Set `TELEGRAM_BOT_USERNAME` (not secret, no `@`) as a GitHub environment variable — the deploy renders it into the api service and refuses to run without it.
2. Ops channel (do this BEFORE disabling group joins and BEFORE the webhook exists — `getUpdates` does not work while a webhook is set): ask me to create a private Telegram group "aijalon ops", add the bot, send a message; get the chat id via `getUpdates` (negative number) and store it as `TELEGRAM_OPS_CHAT_ID`.
3. In BotFather: `/setdescription`, `/setabouttext`, `/setuserpic` (logo from `web/public/brand/`), `/setprivacy` → Enable, and last `/setjoingroups` → Disable (the ops group membership stays).
4. After the first deploy (Phase E step 1), register the webhook with the secret token (already generated in Secret Manager as `TELEGRAM_WEBHOOK_SECRET`) exactly as `docs/DEPLOY.md` §10.1 shows: `setWebhook` with `url=https://api.aijalon.trade/v1/webhooks/telegram`, `secret_token=$SECRET`, `allowed_updates=["message","my_chat_member"]`, `drop_pending_updates=true` (read both values from Secret Manager into shell variables; never echo them). Then `getWebhookInfo` must show the URL and no errors.
5. Test (after step 4): link my own account from the site's **#/alerts** page and press "Send test alert".

**Email via Resend (mandatory alerts only):**
1. Ask me to create a Resend account (free plan = 100 emails/day, fine for internal testing) and add domain **aijalon.trade** (region closest to Singapore).
2. Add the DNS records Resend shows (DKIM TXT, MX + SPF TXT on `send.aijalon.trade`) with `make dns` and `EMAIL_DNS_RECORDS` (`docs/DEPLOY.md` §8 step 6); `make dns` already writes DMARC `p=reject` with reports to `app.aijalon@gmail.com` (ask me whether to use `DMARC_POLICY=quarantine` during testing). Wait until Resend shows "Verified".
3. I create an API key with "Sending access" only for aijalon.trade; pipe it into `EMAIL_PROVIDER_API_KEY`. `EMAIL_FROM` is already `alerts@aijalon.trade` in the service templates. Ops recipients and the allowlist are Secret Manager secrets `OPS_EMAILS` / `ALLOWLIST_EMAILS` (bootstrap seeds both with app.aijalon@gmail.com; ask me for the real lists).
4. Test with the #/alerts "Send test alert" and check it lands in the inbox (not spam). Before public launch: upgrade to a paid Resend plan or move to Amazon SES.

## Phase D — payments, wallets, signals
- **Stripe** (§11): I must get Stripe's approval for this business first (describe it honestly: software subscriptions and platform fees; no custody of trading funds). Then restricted key, webhook `https://api.aijalon.trade/v1/webhooks/stripe`, payment-method domain + Apple Pay file. Stripe fees are passed to users (credit = paid − actual fee). Cards, Apple Pay, Google Pay in USD; FPX/GrabPay stay off until an FX rate source is chosen (ask me).
- **Wallets** (§12): builder + treasury addresses from my hardware wallet (read them back to me for confirmation, second person checks). I fund the builder with ≥100 USDC perps account value on Hyperliquid.
- **SILVER signal feed:** run `integrations/terminal/install.sh` against my terminal repo (`joztern755/terminal.aijalon`) only after showing me the diff; generate the Ed25519 keypair with `node signals/keygen.js` (private key → that repo's GitHub secret, public key → `SIGNALS_PUBKEY_B64`). SILVER is a **free showcase** ($0, 0% profit share); its live state is CASH since 1980 — confirm the published feed shows that.
- **KYC for creators:** start with the `manual` provider (`KYC_PROVIDER=manual`, no KYC secrets) — ONE admin approves each creator (note: the code currently makes a KYC approval maker-checker — a second admin confirms it; tell me so I can decide); ask me before signing up for a KYC provider (Sumsub approvals still need one admin to confirm; switching is `docs/DEPLOY.md` §5.4).

## Phase E — first deploy, internal testing with real USDC
1. Deploy (§10) — I approve in GitHub. Smoke tests must pass. Then §10.1: Telegram webhook, and resume only `candles-sync` + `ingest-signals` so the candle backfill (~40 calls, ~7 h) runs while everything else stays paused.
2. Promote the first two admins (§13) after they sign in with 2FA.
3. Go through `docs/GO_LIVE_CHECKLIST.md` internal-phase gates with me, one by one.
4. **First real-money test, tiny amounts, with me watching** (orders need the scheduler: with my OK resume only `tick`, `deposits-scan`, `fills-ingest`, `deliver-alerts` for the test, or run them by hand with `gcloud scheduler jobs run`; orders are jittered up to 10 min per user): connect my wallet, approve agent + builder fee (check on-chain with the info API: `extraAgents`, `maxBuilderFee`), top up fee balance with $10 USDC, subscribe to a creator test strategy that trades `xyz:SILVER` (a builder-dex market, so the same order checks the asset id) with $100 allocation, 1x leverage. Confirm: one order placed with our builder code and `0xa17a1000…` cloid; fill recorded; builder fee split in the ledger; Telegram + email alerts arrived; cancel with "close positions" closes the position; ledger `verify_chain()` clean; reconciliation shows no mismatch. Also confirm: the browser's POST to `https://api.hyperliquid.xyz/exchange` works (no CORS error — otherwise we need a server relay, tell me); the order's asset id for `xyz:SILVER` was 110026 (`backend/app/hl/markets.py`) and it filled on that market; Hyperliquid accepts a reduce-only close below $10 (if not, tell me); the reconcile report's builder-fee reading (`builderRewards` / `rewardsClaim`) matches what the Hyperliquid UI shows for the builder address.
5. Only then, and only if I say so: `make go-live` (resumes the scheduler).

## What to report back to me
After each phase: what you did, test results (real numbers, failures included), anything you could not verify, and the exact next step that needs me.

---

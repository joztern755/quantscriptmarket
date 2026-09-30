# aijalon.trade — Architecture

Version: 2026-09-30 · Source of truth for behaviour: `docs/SPEC.md`. This document describes how the pieces are deployed, how money and keys flow between them, and where the trust boundaries are. Infrastructure code: `infra/`, `.github/workflows/`, `firebase.json`. Deploy procedure: `docs/DEPLOY.md`.

## 1. Picture

```
                        Users (browser + their own wallet; admins with a hardware wallet)
                                          |
                  +-----------------------+------------------------------+
                  | HTTPS                                                | HTTPS (TLS 1.2+, proxied)
                  v                                                      v
     aijalon.trade (DNS only)                        Cloudflare edge: api.aijalon.trade
     Firebase Hosting (Google CDN)                   WAF + rate limits + geo-block + HSTS
     static SPA web/dist, strict CSP                 Transform Rule: set X-Edge-Auth=<secret>
     /__/auth/* Firebase Auth handler                          | HTTPS, Full (strict)
                  |                                            v
                  |                           Global external HTTPS Load Balancer (static IP)
     Firebase Auth / Identity Platform        Cloud Armor: default deny; allow Cloudflare IPs
     Google + Apple sign-in, TOTP MFA         only; deny if X-Edge-Auth wrong; cert via CM
                  |  ID token (MFA claim)                      | serverless NEG
                  +---------------------------+                v
                                              |   +--------------------------+  Direct VPC egress (all traffic)
                                              +-->| Cloud Run "api"          |---------+
                                                  | ingress: internal+LB     |         |
                                                  | SA aijalon-api           |         |
                                                  |  KMS ENCRYPT agent-keys  |         |
                                                  | + cloud-sql-proxy sidecar|         |
                                                  +------------+-------------+         |
 Cloud Scheduler (7 jobs, OIDC as aijalon-scheduler)           | ID token              |
        | POST /v1/internal/*                                  v                       |
        v                                         +--------------------------+         |
 +--------------------------+     ID token        | Cloud Run "sandbox"      |         |
 | Cloud Run "executor"     |-------------------->| gen1 (gVisor), internal  |         |
 | ingress: internal        |                     | SA aijalon-sandbox: none |         |
 | SA aijalon-executor      |                     | egress -> isolated VPC:  |         |
 |  KMS DECRYPT agent-keys  |                     | no NAT, no PGA, no route |         |
 | + cloud-sql-proxy sidecar|                     +--------------------------+         |
 +------------+-------------+                                                          |
              |  aijalon-vpc / subnet aijalon-run (tag run-egress: 443 anywhere, 5432/3307 to SQL only)
              +-------------------+-------------------------------+--------------------+
                                  |                               |
                   Private Service Access                  Cloud NAT (static IP)
                                  v                               v
          Cloud SQL Postgres 16 "aijalon-pg"          Internet: api.hyperliquid.xyz (Info/Exchange),
          private IP only, HA regional, CMEK (HSM),   Stripe API, Telegram, e-mail provider,
          PITR 7d, 30 backups, IAM DB auth, pgaudit   aijalon-terminal.web.app/signals.json (+ .sig)

 Cloud KMS keyring "aijalon" (asia-southeast1): agent-keys (HSM, ENCRYPT_DECRYPT, 90-day rotation), cloudsql (CMEK)
 Secret Manager (asia-southeast1 only): Stripe, Telegram, e-mail, signal pubkey, pepper, edge secret, addresses, ...
 Artifact Registry "aijalon": backend + sandbox images, deployed by digest; Cloud Logging/Monitoring + alerts
 GitHub Actions --(OIDC -> Workload Identity Federation, main + env production + deploy.yml only)--> aijalon-deployer
 Hyperliquid (Tokyo): user master accounts hold the funds; our per-user agent can trade, never withdraw;
                      builder address (hardware wallet) receives builder fees; treasury (hardware wallet)
```

Everything runs in `asia-southeast1` (Singapore). Strategies are daily-bar, so latency to Hyperliquid is irrelevant (SPEC §2).

## 2. Components

| Component | Runs | Ingress | Egress | Identity | Scale / limits |
|---|---|---|---|---|---|
| Web (SPA) | Firebase Hosting, `web/dist` | public | — | — | CDN; CSP + HSTS + XFO from `firebase.json` |
| `api` | Cloud Run, backend image, `uvicorn app.api.main:create_app --factory` | internal + LB (LB admits only Cloudflare) | VPC all-traffic → SQL private IP, sandbox, NAT | `aijalon-api` | min 1 / max 20, 1 vCPU / 1 GiB, concurrency 40, 60 s |
| `executor` | Cloud Run, same image, `create_executor_app` | internal (Scheduler only) | VPC all-traffic → SQL, sandbox, NAT | `aijalon-executor` | min 0 / max 3, 1 vCPU / 2 GiB, concurrency 4, 30 min |
| `sandbox` | Cloud Run, `sandbox/Dockerfile`, gen1 (gVisor) | internal; invoker = api + executor SAs | isolated VPC with **no** route out | `aijalon-sandbox` (no roles) | min 0 / max 5, 2 vCPU / 2 GiB, 4 concurrent runs, 300 s |
| `migrate` job | Cloud Run Job, backend image, `python scripts/migrate.py` | — | VPC private ranges → SQL | `aijalon-migrator` | 1 task, 15 min, no retries |
| Cloud SQL `aijalon-pg` | Postgres 16 Enterprise, 2 vCPU / 8 GB, HA regional | private IP only (PSA) | — | IAM users for api/executor; built-in `migrator` | 200 connections, storage auto-grow |
| Cloud Scheduler | 7 HTTP jobs, OIDC (tick, settle-daily, ingest-signals, reconcile, deposits-scan, referral-tiers, candles-sync) | — | executor URL | `aijalon-scheduler` | created paused; `make go-live` resumes |
| Edge | Cloudflare (DNS, WAF, rate limit, Transform Rule) → Global HTTPS LB + Cloud Armor | public | — | — | see `infra/cloudflare/dns.sh` |

Database access: api and executor connect to `127.0.0.1:5432`, served by a **Cloud SQL Auth Proxy sidecar** (`--auto-iam-authn --private-ip`): no DB password exists for them; the proxy logs in as the service account's IAM database user and encrypts to the instance with mTLS. Per-connection timeouts travel in `DATABASE_URL` (`options=-c statement_timeout=…`). The migrate job uses the built-in `migrator` user (password only in Secret Manager) over TLS to the private IP.

## 3. Identities and least privilege (SPEC §2.1)

| Principal | Granted (exact) | Explicitly NOT granted |
|---|---|---|
| `aijalon-api` | `cloudkms.cryptoKeyEncrypter` on key `agent-keys` only; `cloudsql.client` + `cloudsql.instanceUser` (IAM-conditioned to `aijalon-pg`); `secretAccessor` on STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_OPS_CHAT_ID, TELEGRAM_WEBHOOK_SECRET, EMAIL_PROVIDER_API_KEY, SIGNALS_PUBKEY_B64, AUDIT_PEPPER, EDGE_AUTH_SECRET, BUILDER_ADDRESS, TREASURY_ADDRESS, SANDBOX_SHARED_SECRET; `run.invoker` on sandbox. DB role `app_api` | KMS decrypt; any project role; column `agent_keys.key_ciphertext`; UPDATE/DELETE on ledger/audit/consents |
| `aijalon-executor` | `cloudkms.cryptoKeyDecrypter` on `agent-keys` only; SQL client + instanceUser (conditioned); secrets TELEGRAM_BOT_TOKEN, TELEGRAM_OPS_CHAT_ID, EMAIL_PROVIDER_API_KEY, SIGNALS_PUBKEY_B64, AUDIT_PEPPER, BUILDER_ADDRESS, TREASURY_ADDRESS, SANDBOX_SHARED_SECRET, EDGE_AUTH_SECRET (temporary: `app/config.py` requires it for every prod role); `run.invoker` on sandbox. DB role `app_executor` | KMS encrypt; Stripe secrets; Telegram webhook secret; public ingress |
| `aijalon-sandbox` | `secretAccessor` on SANDBOX_SHARED_SECRET only (its own inbound auth; unreachable from inside — no route to Google APIs) | everything else: no DB, no KMS, no network |
| `aijalon-scheduler` | `run.invoker` on executor only | — |
| `aijalon-migrator` | `secretAccessor` on DB_MIGRATOR_PASSWORD only | KMS; other secrets |
| `aijalon-deployer` (GitHub via WIF) | `run.developer`; `iam.serviceAccountUser` on the api/executor/sandbox/migrator SAs only; `artifactregistry.writer` on repo `aijalon`; `firebasehosting.admin`; `firebase.viewer`; `serviceusage.serviceUsageConsumer` | secret values; KMS; Cloud SQL; IAM admin; creating SA keys |
| Humans (project Owner) | Owner | **no** KMS decrypt binding (Owner can grant it to themselves — alerted: "KMS key admin change", "IAM policy changed") |

WIF provider condition: `repository == joztern755/quantscriptmarket && ref == refs/heads/main && environment == production && workflow_ref == …/deploy.yml@refs/heads/main` (+ `repository_id` when set). Forks, PRs, other branches, other workflows and other environments cannot obtain Google credentials.

## 4. Edge design decision (api.aijalon.trade)

Options considered:

1. **Cloud Run domain mapping** — rejected: still "Preview / not recommended for production" in Google's docs, region availability for `asia-southeast1` could not be confirmed, and it requires `ingress=all`, leaving `*.run.app` reachable around Cloudflare.
2. **Cloudflare Worker proxying to `*.run.app`** — rejected: same `ingress=all` problem; the only protection of the origin would be the header check in the app.
3. **Global external HTTPS LB + serverless NEG + Cloud Armor (chosen).** Cloud Run ingress `internal-and-cloud-load-balancing` makes the run.app URL unreachable from the internet (the deploy smoke test asserts it). The LB is reachable only from Cloudflare IP ranges (Cloud Armor allow-list, default deny) and only with the correct `X-Edge-Auth` (Cloud Armor rule, then again in the app in constant time). The LB certificate is Google-managed via Certificate Manager **DNS authorization**, which works while Cloudflare proxies the hostname; Cloudflare validates it (SSL Full strict). Cost ≈ US$ 20–30/month — negligible against the risk it removes.

The static site stays **DNS-only** to Firebase Hosting: it holds no secrets and serves only static files with strict headers; proxying it would put Firebase's certificate renewals behind a proxy for no security gain. The money-moving surface (the API) is the proxied one.

## 5. Data flows

### 5.1 Subscribe (connect wallet → agent → builder fee → subscription)
1. Browser signs in (Firebase: Google/Apple + TOTP). Every API call carries the ID token; the API rejects tokens without the second-factor claim; step-up (auth ≤ 5 min) for connect/subscribe.
2. `POST /v1/wallets/verify`: the user signs a message with their wallet; the API records the master address.
3. `POST /v1/agents` (step-up): **api** generates a secp256k1 agent key in memory → **KMS Encrypt** (`agent-keys`, HSM) → stores only ciphertext + key version (`agent_keys`, INSERT-only for app_api; it can never SELECT the ciphertext back) → returns the agent address + EIP-712 typed data.
4. User signs `approveAgent` and `approveBuilderFee` in their wallet and submits to Hyperliquid; `POST /v1/agents/{id}/confirm` and `/v1/builder-approval/confirm` verify on-chain (`extraAgents`, `maxBuilderFee`).
5. Subscribe gate (consents recorded, append-only) → `POST /v1/subscriptions` (step-up): allocation, max leverage, fee-balance check → ledger entries for the prepaid subscription.

### 5.2 Tick (every minute)
Cloud Scheduler → OIDC → `executor /v1/internal/tick` (Cloud Run IAM verifies the token; the app re-checks audience and the scheduler SA e-mail). Executor reads due subscriptions and signals, runs the pre-trade guards (SPEC §5.4), **KMS Decrypt** each needed agent key into memory only, signs IOC orders with our builder code and `cloid`, posts to Hyperliquid via NAT, records orders; keys are dropped at the end of the tick and never logged. Per-user jitter (0–10 min) spreads orders across ticks. Kill switches and circuit breakers fail closed.

### 5.3 Signals
Terminal's daily GitHub Action emits `signals.json` + `signals.sig` (Ed25519; private key only in the terminal repo's secret) to `aijalon-terminal.web.app`. `ingest-signals` (every 15 min) fetches both, verifies against `SIGNALS_PUBKEY_B64`, checks schema/staleness (> 36 h rejected), stores rows. Creator scripts: the executor sends bars to the **sandbox** once per bar close; the sandbox returns target weights only.

### 5.3b Candles and alerts
`candles-sync` (hourly at :05) stores closed 1h/4h/1d candles for every perp market (immutable once closed) so backtest history grows beyond Hyperliquid's 5,000-candle window. User and ops alerts go out through Telegram (bot; users link via a one-time `t.me` deep link; Telegram calls `/v1/webhooks/telegram` with the secret token) and e-mail (Resend, DKIM-aligned for `aijalon.trade`), both from the NAT egress IP.

### 5.4 Settlement (00:30 UTC daily)
`settle-daily` → executor computes attributed PnL per subscription (fills + funding), applies the high-water mark, posts profit-share and fee splits as double-entry ledger transactions idempotent on `(subscription_id, settle_date)`, updates balance states (active → past_due → reduce_only). `referral-tiers` (01:15) re-evaluates tiers; `reconcile` (hourly) compares the builder-fee ledger with Hyperliquid builder rewards and treasury USDC with the ledger; mismatches > $1 alert.

### 5.5 Deposits
- **USDC:** the user `usdSend`s to the treasury address on Hyperliquid (typed data from `/v1/deposits/usdc/typed-data`); `deposits-scan` (every minute) finds transfers to the treasury from verified master addresses and credits the fee balance once per tx hash.
- **Stripe:** `/v1/deposits/stripe` creates a PaymentIntent (automatic payment methods: cards, Apple Pay, Google Pay, local); Stripe → `POST /v1/webhooks/stripe` via Cloudflare (exempt from geo-block/rate-limit; signature verified; idempotent) → credit = amount − actual Stripe fee (balance transaction).

### 5.6 Payouts and withdrawals
Server prepares a batch from ledger payables; admin A approves (maker), admin B approves (checker, must differ); an admin signs `usdSend` from the **treasury hardware wallet in the browser**; the server records the tx hash and posts the ledger entry. No server ever holds the treasury or builder private key.

### 5.7 Deploy
PR → CI (lint, tests incl. stdlib-only domain tests, DB tests + migrations as a non-superuser + privilege assertions, bandit, pip-audit, gitleaks, pinning, CSP, web build + Playwright, signals, Docker) → merge to main → deploy workflow re-runs CI → environment approval → WIF → images by digest → migrate job → sandbox → executor → api → smoke (+ auto rollback) → Hosting → web smoke.

## 6. Trust boundaries

| # | Boundary | Controls |
|---|---|---|
| TB1 | Internet → Cloudflare → LB → api | WAF/rate limits/geo-block; Cloud Armor allow-list of Cloudflare IPs + `X-Edge-Auth`; ingress internal+LB; app re-checks the edge secret; only then trusts `CF-Connecting-IP`/`CF-IPCountry`; Firebase ID token + MFA + step-up; CORS `https://aijalon.trade` only; body limits |
| TB2 | Browser ↔ third parties | CSP (no inline script, enumerated origins, `frame-ancestors 'none'`), HSTS preload, XFO DENY, COOP same-origin-allow-popups; wallet signatures happen in the user's wallet UI |
| TB3 | api ↔ executor | Separate services and identities: api can only **encrypt** agent keys; only executor can **decrypt**; the tick endpoint is mounted only on the executor; executor has no public ingress |
| TB4 | Scheduler → executor | Google-signed OIDC token, audience = executor URL, `run.invoker` only for the scheduler SA; app verifies e-mail/audience |
| TB5 | api/executor → sandbox (untrusted creator code) | gVisor; zero-egress VPC; no-role SA; per-request process + rlimits + AST allowlist; Cloud Run IAM (ID token) + shared secret; output = weights only, re-validated by the caller |
| TB6 | Services → Cloud SQL | Private IP only, TLS enforced, IAM DB auth (no passwords) for runtime logins; DB roles: column-level privileges, append-only ledger/audit/consents via triggers, hash chains; migrations only via the `migrator` login |
| TB7 | Services → KMS / Secret Manager | Per-key and per-secret bindings; Data Access audit logs; alerts on any unexpected principal |
| TB8 | GitHub → Google Cloud | WIF with claim conditions (repo, id, branch, environment, workflow); environment reviewers; SHA-pinned actions; hash-locked Python deps; digest-pinned images; no JSON keys |
| TB9 | Terminal → marketplace signals | Ed25519 signature with a pinned public key; staleness and schema checks; vendored script hash |
| TB10 | Platform ↔ Hyperliquid | Agents cannot withdraw; builder fee capped by the user's own approval; pre-trade guards; reconciliation |
| TB11 | Operators → production | 2FA security key on the owner account; break-glass DB access via temporary proxy-only IP (alerted, pgaudit); maker-checker for money and kill-switch lifts; hardware wallets |

## 7. Encryption and data at rest

- Agent keys: envelope encryption — ciphertext in Postgres, KEK `agent-keys` in Cloud HSM (FIPS 140-2 L3), 90-day rotation (old versions kept for decrypt).
- Database disk + backups: CMEK with the HSM key `cloudsql`; PITR 7 days; 30 daily backups (multi-region Asia backup location by default).
- Secrets: Secret Manager, replicated only in `asia-southeast1`.
- Logs: structured JSON with secret redaction (`app/logging.py`); Data Access logs for KMS/Secret Manager/SQL admin/IAM.

## 8. Residual risks (accepted or open)

1. **The deploy pipeline can run arbitrary code as the executor** (it deploys it). Mitigations: environment approval, branch protection, WIF conditions, pinned supply chain, alerting on unexpected decrypt principals. A second reviewer and "prevent self-review" are strongly recommended.
2. **Project Owner can grant itself KMS decrypt.** Alerted; organisation-level controls (Cloud Identity org, PAM/JIT) recommended before public launch.
3. **No Google Cloud organization** (personal account): no org policies, no VPC Service Controls.
4. **Single region.** Regional HA protects against a zone loss, not a region loss; backups are multi-regional; RTO target 4 h (RUNBOOK §8).
5. **Edge secret is visible to Cloudflare account members** and to Cloud Armor viewers — acceptable; the app still requires Firebase auth for every user action.
6. Items listed in `docs/DEPLOY.md` §16 that could not be verified from the authoring environment.

# aijalon.trade — Deploy Guide

Version: 2026-09-30 · Audience: the owner's **local** Claude Code session (it has `gcloud`, `firebase`, network) and the owner.
Related: `docs/ARCHITECTURE.md` (what is built and why), `docs/GO_LIVE_CHECKLIST.md` (business/legal gates), `docs/RUNBOOK.md`, `docs/SECURITY.md`.

This guide takes an empty Google account (`app.aijalon@gmail.com`) to a running production stack. Every command is idempotent unless marked **ONE-TIME**. Steps marked **HUMAN** need a person (a console click, a hardware wallet, a payment method, a legal decision); the Claude session must stop and ask for them. Never paste a secret value into a chat, a commit, an issue or a log; the commands below read secrets from a prompt or a file and pipe them straight into Secret Manager.

**Order at a glance**

| # | Step | Who | Result |
|---|---|---|---|
| 1 | Install tools | session | gcloud, firebase, node, python 3.12, psql, cloud-sql-proxy, gh, docker |
| 2 | Create project + billing (Blaze) | HUMAN + session | project `aijalon-trade-prod` |
| 3 | Lock dependencies, pin images, commit | session | `requirements*.lock`, digests |
| 4 | `make bootstrap` | session | network, KMS, SQL, Run, LB, Scheduler (paused), WIF, alerts |
| 5 | Secrets | HUMAN values, session pipes | Secret Manager versions |
| 6 | GitHub environment, variables, branch protection | session (`gh`) + HUMAN reviewers | deploy gate |
| 7 | Firebase Auth (Identity Platform, TOTP, Google, Apple) | HUMAN console + session | sign-in works with MFA |
| 8 | Cloudflare DNS/TLS/WAF | HUMAN token + session | `aijalon.trade`, `api.aijalon.trade` |
| 9 | `make db-bootstrap` | session | DB, users, migrations, grants, verified privileges |
| 10 | First deploy | session triggers, HUMAN approves | live revisions |
| 11 | Stripe | HUMAN | keys, webhook, Apple Pay domain |
| 12 | Builder + treasury wallets, signals key, terminal patch | HUMAN | addresses, feed |
| 13 | First admins | HUMAN signs in, session promotes | 2 admins |
| 14 | Go-live gates, then `make go-live` | HUMAN decision | scheduler running |

---

## 1. Prerequisites

| Tool | Version | Check |
|---|---|---|
| Google Cloud CLI (`gcloud`, incl. `beta`) | current (≥ 500) | `gcloud version` |
| Firebase CLI | 15.32.0 (same as CI) | `npm i -g firebase-tools@15.32.0 && firebase --version` |
| Node.js | 22 | `node -v` |
| TypeScript | 6.0.2 (same as CI) | `npm i -g typescript@6.0.2` |
| Python | 3.12 | `python3.12 -V` |
| PostgreSQL client | 16 (`psql`) | `psql --version` |
| Cloud SQL Auth Proxy | v2.26.0 | `cloud-sql-proxy --version` (https://github.com/GoogleCloudPlatform/cloud-sql-proxy/releases) |
| GitHub CLI | current | `gh auth status` (logged in as the repo owner, `joztern755`) |
| Docker with buildx | current | `docker buildx version` (for `make lock`, `make pin`) |
| curl, openssl | any | |

Accounts: the Google account `app.aijalon@gmail.com` (turn on 2-Step Verification with a **security key** before anything else — it will own production), a billing payment method, the Cloudflare account that holds `aijalon.trade`, the GitHub account owning `joztern755/quantscriptmarket`, an Apple Developer account (for Sign in with Apple and Apple Pay), a Stripe account in the company's name.

**Cloudflare API token** (HUMAN — Cloudflare dashboard → My Profile → API Tokens → Create Custom Token). Scope it to **one zone, `aijalon.trade`**, with a short TTL and (ideally) your IP in "Client IP Address Filtering":

| Permission group | Level |
|---|---|
| Zone → Zone | Read |
| Zone → DNS | Edit |
| Zone → Zone Settings | Edit |
| Zone → SSL and Certificates | Edit |
| Zone → Zone WAF | Edit (custom rules, managed rules, rate limiting rulesets) |
| Zone → Firewall Services | Edit |
| Zone → Transform Rules | Edit |
| Zone → Cache Rules | Edit |
| Zone → DNSSEC — covered by Zone Settings/DNS on current plans; if the DNSSEC call fails, enable it in the dashboard | — |

Keep the token only in the shell (`read -rs CLOUDFLARE_API_TOKEN; export CLOUDFLARE_API_TOKEN`). Delete it after step 8; create a new one for later changes.

```bash
gcloud auth login app.aijalon@gmail.com
gcloud auth application-default login          # ADC for the Auth Proxy and firebase CLI
firebase login                                 # same Google account
gh auth login
```

## 2. Project and billing — ONE-TIME

1. **HUMAN:** in https://console.cloud.google.com/billing create a billing account (card) if none exists. Firebase "Blaze" = a Firebase project linked to a Cloud billing account; nothing else to buy.
2. Pick the project id. Default `aijalon-trade-prod`. If `gcloud projects describe aijalon-trade-prod` shows it exists and is not yours, choose another id and change it in **three** places: `infra/gcp/env.sh` (`PROJECT_ID` default), `.firebaserc`, and `infra/csp.txt` + `firebase.json` (the CSP names `https://<project>.firebaseapp.com`; regenerate with `make csp-sync PROJECT_ID=<id>`).
3. `gcloud billing accounts list` → note the id `XXXXXX-XXXXXX-XXXXXX`.

The bootstrap's `project` step creates the project and links billing. A personal Gmail account has **no Google Cloud organization**, so organization policies (e.g. "disable service-account key creation", "restrict public IP on Cloud SQL") are unavailable. Compensating controls in this repo: no key is ever created (WIF only) and an alert fires on `CreateServiceAccountKey`; Cloud SQL has no public IP and bootstrap re-verifies it. Recommended later: create a free Cloud Identity organization for `aijalon.trade`, move the project into it and set those org policies.

## 3. Supply-chain pinning — commit before the first deploy

```bash
make lock      # backend/requirements.lock + requirements-dev.lock with sha256 hashes (runs in python:3.12-slim)
make pin       # GitHub Actions -> commit SHAs (already pinned; re-verifies), images -> digests
git diff       # review: new lock files; Dockerfile/env.sh/ci.yml now carry @sha256:... digests
```

- The versions in `backend/requirements.txt` were confirmed as upstream release tags on 2026-09-30 but PyPI was not reachable from the authoring environment (lines marked `# verify`). If `make lock` cannot resolve one, pick the nearest release, re-run, and delete the `# verify` marks once the lock exists. If pip-tools breaks with the image's pip, use `uv pip compile --generate-hashes` with the same inputs.
- The deploy workflow **refuses** to run without `backend/requirements.lock` or with any `sha256:PIN_ME` placeholder left. CI only warns, so development is not blocked.
- The sandbox image (`sandbox/Dockerfile`, owned by the sandbox module) still uses `FROM python:3.12-slim` without a digest: add a `# pin-image: python:3.12-slim` line above it and a `@sha256:PIN_ME` suffix, then `make pin`.

Commit (`chore: lock dependencies and pin images`) and push to a branch; open a PR; CI must be green.

## 4. Google Cloud bootstrap

```bash
BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX GITHUB_REPO_ID=$(gh api repos/joztern755/quantscriptmarket --jq .id) make bootstrap
```

`infra/gcp/bootstrap.sh` runs these steps (re-run any with `./infra/gcp/bootstrap.sh <step> ...`):

| Step | Creates / enforces |
|---|---|
| `project` | project, billing link |
| `apis` | all required APIs |
| `firebase` | adds Firebase to the project, creates the web app `aijalon-web`, writes its public `apiKey`/`appId` to `~/.aijalon-deploy/<project>/firebase.env` |
| `audit` | Data Access audit logs (read + write) for KMS, Secret Manager, Cloud SQL admin, IAM — every agent-key decrypt is logged |
| `network` | `aijalon-vpc` + subnet `aijalon-run` (Private Google Access, flow logs), Private Service Access for Cloud SQL, Cloud Router + Cloud NAT with a **static egress IP**, egress firewall (443 anywhere, 5432/3307 to the SQL range only, deny the rest); `aijalon-sandbox-vpc` with **no NAT, no Private Google Access, the internet route deleted, deny-all egress/ingress** |
| `kms` | keyring `aijalon`; key `agent-keys` (ENCRYPT_DECRYPT, **HSM**, rotation 90 d, destroy delay 90 d); key `cloudsql` (HSM, CMEK for the database disk) + grant to the Cloud SQL service agent |
| `registry` | Artifact Registry `aijalon` (immutable tags; vulnerability scanning) |
| `sa` | service accounts and least-privilege bindings (table in ARCHITECTURE §3) |
| `secrets` | the secret **names** (user-managed replication in `asia-southeast1`), per-secret accessor bindings; generates random values for `AUDIT_PEPPER`, `EDGE_AUTH_SECRET`, `TELEGRAM_WEBHOOK_SECRET`, `DB_MIGRATOR_PASSWORD`, `SANDBOX_SHARED_SECRET` (never printed) |
| `sql` | Cloud SQL `aijalon-pg`: Postgres 16, Enterprise edition `db-custom-2-8192`, **REGIONAL HA**, private IP only, `ENCRYPTED_ONLY`, CMEK, PITR (7 days of WAL), 30 automated daily backups, deletion protection, maintenance Sun 19:00 UTC, flags `cloudsql.iam_authentication=on`, `max_connections=200`, pgaudit (DDL + ROLE), connection/lock/slow-query logging; the built-in `postgres` password is set to a random value and discarded. On re-runs it only **verifies** these settings |
| `run` | placeholder revisions of `api` (ingress internal-and-cloud-load-balancing), `executor` and `sandbox` (ingress internal); invoker IAM (api: allUsers — the LB/Armor/app gate it; executor: scheduler SA only; sandbox: api + executor SAs only) |
| `lb` | global external HTTPS LB → serverless NEG(api); Cloud Armor: default deny, allow Cloudflare IPv4 ranges only, deny when `X-Edge-Auth` is wrong; Certificate Manager cert for `api.aijalon.trade` via DNS authorization; TLS ≥ 1.2 MODERN |
| `scheduler` | the seven OIDC jobs → executor, **created PAUSED** (tick every minute, settle-daily 00:30 UTC, ingest-signals every 15 min, reconcile hourly at :07, deposits-scan every minute, referral-tiers 01:15 UTC, candles-sync hourly at :05) |
| `wif` | pool `github`, provider `github-oidc` accepting only repo `joztern755/quantscriptmarket` (+ repo id), `refs/heads/main`, environment `production`, workflow `deploy.yml` → deployer SA |
| `monitoring` | e-mail channel; alerts: agent-keys decrypt by anyone but executor (CRITICAL), encrypt by anyone but api, KMS admin changes, unexpected secret reads, secret changes, IAM changes, Cloud SQL admin ops, app CRITICAL logs, executor errors, Scheduler failures, sandbox egress attempts, api 5xx, SQL CPU/disk/connections; uptime checks on `https://api.aijalon.trade/healthz` and `https://aijalon.trade/` |
| `budget` | budget alert (default 400 USD/month; 50/90/100 % actual, 120 % forecast) |
| `harden` | removes Editor from the default compute SA; essential contact |
| `outputs` | `~/.aijalon-deploy/<project>/outputs.env` + the exact `gh variable set` commands for step 6 |

Expected first-run duration: 30–45 min (Cloud SQL HA + CMEK is the slow part). Cost at idle: roughly Cloud SQL HA 2 vCPU (~US$ 200/month), LB forwarding rule + Cloud Armor (~US$ 25), NAT (~US$ 5 + traffic), api min-instance 1, HSM key operations (cents). Set `BUDGET_USD` accordingly.

**HUMAN:** add a second notification path for critical alerts (Cloud Monitoring → Alerting → Notification channels → the Google Cloud mobile app or SMS) and attach it to the `SEC:` and `UPTIME:` policies.

## 5. Secrets

Values are added by the owner; the session only runs the pipe. Pattern (no echo, no shell history, no file left behind):

```bash
add_secret() { read -rsp "value for $1: " v; echo; printf '%s' "$v" | gcloud secrets versions add "$1" --data-file=- --project=aijalon-trade-prod; unset v; }
add_secret STRIPE_SECRET_KEY
```

| Secret | Value | Source | Read by |
|---|---|---|---|
| `STRIPE_SECRET_KEY` | Stripe **restricted** key `rk_live_…` (permissions in §11) | Stripe dashboard | api |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` of the endpoint in §11 | Stripe dashboard | api |
| `TELEGRAM_BOT_TOKEN` | bot token from @BotFather (a bot used only for ops) | Telegram | api, executor |
| `TELEGRAM_OPS_CHAT_ID` | numeric id of the private ops group (add the bot, send a message, read `getUpdates`) | Telegram | api, executor |
| `EMAIL_PROVIDER_API_KEY` | transactional e-mail provider API key (sending-only scope) | provider | api, executor |
| `SIGNALS_PUBKEY_B64` | raw 32-byte Ed25519 public key, base64 (step 12) | `node signals/keygen.js` | api, executor |
| `AUDIT_PEPPER` | **generated** by bootstrap (32 random bytes, base64; env `AUDIT_PEPPER_B64`). Never rotate casually: IP hashes become incomparable | — | api, executor |
| `EDGE_AUTH_SECRET` | **generated** (64 hex). Also written into Cloudflare (step 8) and Cloud Armor | — | api (+ executor until `app/config.py` checks it for the api role only) |
| `TELEGRAM_WEBHOOK_SECRET` | **generated** (64 hex). Pass it as `secret_token` when the app registers the bot webhook (`setWebhook` → `https://api.aijalon.trade/v1/webhooks/telegram`); Telegram echoes it in `X-Telegram-Bot-Api-Secret-Token` | — | api |
| `BUILDER_ADDRESS` | builder wallet address, lower-case `0x…` (step 12) | hardware wallet | api, executor |
| `TREASURY_ADDRESS` | treasury wallet address, lower-case `0x…` (step 12) | hardware wallet | api, executor |
| `DB_MIGRATOR_PASSWORD` | **generated**; used only by the migrate job and `db_bootstrap.sh` | — | migrator SA |
| `SANDBOX_SHARED_SECRET` | **generated**; `X-Sandbox-Secret` between api/executor and the sandbox | — | api, executor, sandbox |

`BUILDER_ADDRESS` / `TREASURY_ADDRESS` are not secret, but keeping them in Secret Manager means changing them is an audited, alerted event ("SEC: Secret added/destroyed/IAM changed"). Enter them only while a second person reads the address off the hardware wallet screen.

After adding a version, the running services keep the old value until the next revision (`key: latest` is resolved at revision start). Re-deploy (step 10) to apply.

## 6. GitHub: environment, variables, branch protection

```bash
R=joztern755/quantscriptmarket
# 1) environment "production" with required reviewers (HUMAN: add a second reviewer when there is one and
#    tick "Prevent self-review" then). Deployment branches: main only.
gh api -X PUT repos/$R/environments/production --input - <<EOF
{"reviewers":[{"type":"User","id":$(gh api users/joztern755 --jq .id)}],
 "deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}
EOF
# 2) variables (NOT secrets — none of these is sensitive). Run the lines printed by bootstrap 'outputs', i.e.:
source ~/.aijalon-deploy/aijalon-trade-prod/outputs.env
source ~/.aijalon-deploy/aijalon-trade-prod/firebase.env
for v in GCP_PROJECT_ID GCP_REGION WIF_PROVIDER DEPLOYER_SA DB_PRIVATE_IP FIREBASE_WEB_API_KEY FIREBASE_APP_ID; do
  gh variable set "$v" --env production -R "$R" --body "${!v}"; done
gh variable set STRIPE_PUBLISHABLE_KEY --env production -R "$R" --body "pk_live_..."   # step 11 (pk_test_ until then)
gh variable set LAUNCH_PHASE --env production -R "$R" --body internal
gh variable set ALLOWLIST_EMAILS --env production -R "$R" --body "app.aijalon@gmail.com"
gh variable set PAYOUTS_ENABLED --env production -R "$R" --body false
gh variable set SMOKE_SKIP_PUBLIC --env production -R "$R" --body true   # until DNS + certs work (step 8); then delete
# 3) branch protection on main: PR required, CI checks required, no force-push/deletion, linear history
gh api -X PUT repos/$R/branches/main/protection --input - <<'EOF'
{"required_status_checks":{"strict":true,"contexts":["pinning, CSP, script syntax","gitleaks (full history)",
  "backend (lint, tests, DB, security)","docker build (backend, sandbox)","web (build, unit, Playwright smoke)",
  "signals (Ed25519 feed adapter)"]},
 "enforce_admins":true,"required_pull_request_reviews":{"required_approving_review_count":1,"require_code_owner_reviews":false},
 "restrictions":null,"allow_force_pushes":false,"allow_deletions":false,"required_linear_history":true,
 "required_conversation_resolution":true}
EOF
```

With one human, `required_approving_review_count: 1` blocks self-merges; either add a second reviewer, or set it to 0 until then (the production environment approval remains the human gate). **HUMAN:** Settings → Actions → General: "Allow actions … and reusable workflows" → allow only GitHub-owned, verified creators and `google-github-actions/*`, `gitleaks/*`; tick "Require actions to be pinned to a full-length commit SHA"; Workflow permissions → "Read repository contents". No repository secrets are needed at all (no `FIREBASE_SERVICE_ACCOUNT`, unlike the terminal repo).

## 7. Firebase Authentication (Google + Apple, TOTP MFA)

1. **Identity Platform + TOTP + lockdown** (session):
   ```bash
   make auth-config        # = ./infra/gcp/firebase_auth.sh
   ```
   It performs exactly these REST calls (Identity Toolkit Admin API v2; `TOKEN=$(gcloud auth print-access-token)`, header `X-Goog-User-Project: aijalon-trade-prod`):
   ```bash
   # upgrade to Identity Platform (idempotent)
   curl -X POST "https://identitytoolkit.googleapis.com/v2/projects/aijalon-trade-prod/identityPlatform:initializeAuth" \
     -H "Authorization: Bearer $TOKEN" -H "X-Goog-User-Project: aijalon-trade-prod" -H "Content-Type: application/json" -d '{}'
   # TOTP MFA on, SMS off
   curl -X PATCH "https://identitytoolkit.googleapis.com/admin/v2/projects/aijalon-trade-prod/config?updateMask=mfa" \
     -H "Authorization: Bearer $TOKEN" -H "X-Goog-User-Project: aijalon-trade-prod" -H "Content-Type: application/json" \
     -d '{"mfa":{"state":"ENABLED","enabledProviders":[],"providerConfigs":[{"state":"ENABLED","totpProviderConfig":{"adjacentIntervals":1}}]}}'
   ```
   plus: e-mail/password, phone and anonymous sign-in **off**; one account per e-mail; authorized domains = `aijalon.trade` + `aijalon-trade-prod.firebaseapp.com` (localhost removed); e-mail enumeration protection on; client-side self-deletion off. `./infra/gcp/firebase_auth.sh show` prints the result. MFA is *enforced* by the API (tokens without `firebase.sign_in_second_factor` are rejected, SPEC §5.2), not by Firebase.
2. **Google provider** (HUMAN, console): APIs & Services → OAuth consent screen (External, app name "aijalon.trade", support e-mail, authorized domain `aijalon.trade`, scopes `openid email profile` only; publish). Credentials → Create OAuth client ID → Web application; JS origin `https://aijalon.trade`; redirect URI `https://aijalon.trade/__/auth/handler`. Then either Firebase console → Authentication → Sign-in method → Google → enable with that client, or:
   `GOOGLE_OAUTH_CLIENT_ID=… GOOGLE_OAUTH_CLIENT_SECRET=… ./infra/gcp/firebase_auth.sh google` (read the secret with `read -rs`).
3. **Apple provider** (HUMAN, Apple Developer): Certificates, IDs & Profiles → Identifiers → an App ID with "Sign in with Apple" → a **Services ID** (e.g. `trade.aijalon.web`) with Sign in with Apple configured for domain `aijalon.trade` and return URL `https://aijalon.trade/__/auth/handler` → Keys → a key with Sign in with Apple (download the `.p8` once; note the Key ID) → note the Team ID. Then Firebase console → Sign-in method → Apple (Services ID, Team ID, Key ID, private key), or
   `APPLE_SERVICES_ID=… APPLE_TEAM_ID=… APPLE_KEY_ID=… APPLE_PRIVATE_KEY_FILE=AuthKey_XXXX.p8 ./infra/gcp/firebase_auth.sh apple`, then store the `.p8` offline (password manager) and delete the file.
4. `authDomain` is `aijalon.trade` (web/public/app-config.json): Firebase Hosting serves `/__/auth/*` on our domain, which avoids third-party-storage problems in Safari/Chrome. The hosting headers exclude `/__/*` from our CSP/X-Frame-Options so the auth iframe keeps working.

## 8. Cloudflare and custom domains

1. **Firebase custom domain** (HUMAN, console, after step 4): Firebase console → Hosting → Add custom domain → `aijalon.trade` (and `www.aijalon.trade` → "Redirect to aijalon.trade"). Firebase shows the records to create: one or two **A** records (e.g. `199.36.158.100`) and a **TXT** `hosting-site=aijalon-trade-prod` (sometimes also an `_acme-challenge` TXT). Copy them into the variables below; do not click "Verify" yet.
2. Run (session):
   ```bash
   read -rs CLOUDFLARE_API_TOKEN; export CLOUDFLARE_API_TOKEN
   source ~/.aijalon-deploy/aijalon-trade-prod/outputs.env
   FIREBASE_A_RECORDS="199.36.158.100" FIREBASE_TXT="hosting-site=aijalon-trade-prod" \
   API_LB_IP=$API_LB_IP API_CERT_DNS_AUTH_NAME=$API_CERT_DNS_AUTH_NAME API_CERT_DNS_AUTH_VALUE=$API_CERT_DNS_AUTH_VALUE \
   CF_PLAN=free make dns          # CF_PLAN=pro once upgraded (recommended before public launch)
   ```
   It writes: apex + www A → Firebase (**DNS only**), Firebase TXT, `api` A → LB IP (**proxied**), the Certificate Manager CNAME (DNS only), CAA (letsencrypt.org, pki.goog), DMARC `p=reject`, SPF `-all` (no mail yet); zone settings SSL **Full (strict)**, Always HTTPS, min TLS 1.2, TLS 1.3, HSTS (2 years, subdomains, preload, nosniff), 0-RTT off, e-mail obfuscation and Rocket Loader off (they inject scripts, which our CSP blocks), DNSSEC; the **Transform Rule** that sets `X-Edge-Auth` on every `api.aijalon.trade` request (overwriting any client value); WAF custom rules (block `/v1/internal/*`, anything outside `/v1/*` and `/healthz`, unusual methods, and the restricted jurisdictions except Stripe webhooks and `/healthz`); rate limit (Free: 50 req/10 s per IP; Pro: 300/min + 20/min on money/key endpoints); cache bypass for the API; on Pro+ the Cloudflare Managed and OWASP rulesets.
3. Click **Verify** in Firebase. Firebase's certificate can take up to 24 h. Check the LB cert: `gcloud certificate-manager certificates describe api-cert --format='value(managed.state)'` → `ACTIVE`.
4. Then `curl -sS https://api.aijalon.trade/healthz` (after the first deploy) and `gh variable delete SMOKE_SKIP_PUBLIC --env production -R joztern755/quantscriptmarket`.
5. Later (HUMAN, go-live gate): submit `aijalon.trade` to https://hstspreload.org — only when every subdomain is HTTPS-only for good (preload is hard to undo).
6. E-mail (Resend, SPEC §alerts): Resend dashboard → Domains → add `aijalon.trade` → it shows a DKIM TXT (`resend._domainkey`) and, on the `send.aijalon.trade` return-path subdomain, an MX and an SPF TXT. Re-run `make dns` with them:
   `EMAIL_DNS_RECORDS=$'TXT|resend._domainkey.aijalon.trade|p=MIGf...\nMX|send.aijalon.trade|10 feedback-smtp.<region>.amazonses.com\nTXT|send.aijalon.trade|v=spf1 include:amazonses.com ~all'`
   The apex keeps `v=spf1 -all`; DMARC `p=reject; adkim=s; aspf=r` passes through Resend's aligned DKIM. Create a Resend API key with **sending access only, restricted to aijalon.trade** → `EMAIL_PROVIDER_API_KEY`.
7. Telegram webhook: Cloudflare only admits `/v1/webhooks/telegram` from Telegram's published ranges (149.154.160.0/20, 91.108.4.0/22 — re-check https://core.telegram.org/bots/webhooks) and exempts it from geo-blocking and the per-IP rate limit.

Why the API is not on a Cloud Run domain mapping or a Cloudflare Worker: see ARCHITECTURE §4.

## 9. Database bootstrap

```bash
make db-bootstrap        # = ./infra/gcp/db_bootstrap.sh  (asks before adding the temporary public IP)
```

1. creates database `aijalon`; the built-in user `migrator` (password = `DB_MIGRATOR_PASSWORD`); the IAM service-account users `aijalon-api@<project>.iam` and `aijalon-executor@<project>.iam`;
2. **temporarily** adds a public IP with **no authorized networks** (only the Cloud SQL Auth Proxy, authenticated with your Google identity over mTLS, can connect) and starts the proxy on `127.0.0.1:6543`; a trap removes the public IP on exit (if the script is killed: `./infra/gcp/db_bootstrap.sh close`);
3. `sql/00_pre_migrate.sql`: extensions (pgcrypto, pgaudit), `REVOKE ALL ON DATABASE … FROM PUBLIC`, UTC, and the PG16 fix that lets a non-superuser migrator own the schema (`createrole_self_grant`, pre-created `app_migrator` with CREATE on `public`) — without it `0002_roles.sql` fails on Cloud SQL, where no user is a superuser;
4. `python backend/scripts/migrate.py` (psycopg if installed, else the `psql` CLI);
5. `sql/10_grants.sql`: `GRANT app_api` / `app_executor` to the IAM users (per-login timeouts are also passed in `DATABASE_URL`);
6. `sql/20_verify.sql`: **fails** unless api cannot read `agent_keys.key_ciphertext`, neither login can UPDATE/DELETE/TRUNCATE ledger, audit or consents, neither can CREATE in `public`, and neither is superuser/CREATEROLE/CREATEDB/BYPASSRLS.

Later migrations run automatically in every deploy (Cloud Run Job `migrate`, before traffic moves). The same fix/grant/verify SQL runs in CI against Postgres 16 as a non-superuser, so a migration that breaks the privilege model fails the PR.

## 10. First deploy

```bash
git checkout main && git pull   # the lock/pin PR from step 3 merged
make deploy                     # gh workflow run deploy.yml --ref main  (or merge any PR to main)
```

**HUMAN:** open the run in GitHub → "Review deployments" → approve `production`. The job: preflight (locks, digests, CSP, variables) → WIF auth → build + push images (by digest) → **migrate job** → sandbox → executor → api (`gcloud run services replace` with the rendered `infra/gcp/run/*.yaml`) → API smoke (health; `/v1/internal/*` blocked at the edge; `*.run.app` origins of api **and** executor not publicly reachable) → automatic rollback of api/executor on failure → web build with the production `app-config.json` → CSP of the build must equal `infra/csp.txt` and `firebase.json` → `firebase deploy --only hosting` (WIF credentials, no key) → web smoke (served CSP/HSTS, the new build id live, `app-config` project, Apple Pay file).

If the build's CSP changed (e.g. new Firebase SDK hash, new origin), the deploy stops at the CSP step: run `make csp-sync`, review `git diff infra/csp.txt firebase.json`, commit through a PR.

Rollback: api/executor → `./infra/gcp/deploy.sh rollback` (routes 100 % to the recorded previous revisions) or `gcloud run services update-traffic api --to-revisions=<rev>=100`. Hosting → Firebase console → Hosting → Release history → Rollback. Migrations are forward-only: write them expand/contract so the previous revision still works.

## 11. Stripe — HUMAN

1. Account in the company's name; business category approved for this business (GO_LIVE B1).
2. Developers → API keys → **Create restricted key** "aijalon-api": PaymentIntents **Write**, Charges **Read**, Balance transactions **Read** (needed for the fee pass-through: credit = amount − actual Stripe fee), Refunds **Write**, Disputes **Read**, Customers **Write** only if the app stores customers, everything else **None**. → `STRIPE_SECRET_KEY`.
3. Developers → Webhooks → endpoint `https://api.aijalon.trade/v1/webhooks/stripe`, events `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.refunded`, `charge.dispute.created`, `charge.dispute.closed` → signing secret → `STRIPE_WEBHOOK_SECRET`. (Cloudflare exempts this path from geo-blocking and rate limiting; the app verifies the signature.)
4. Publishable key `pk_live_…` → GitHub variable `STRIPE_PUBLISHABLE_KEY` (public by design).
5. Payment methods: cards, Apple Pay, Google Pay, and local methods (FPX, GrabPay…) as decided. Settings → **Payment method domains** → add `aijalon.trade`; download `apple-developer-merchantid-domain-association` into `infra/hosting/well-known/` (unchanged bytes), commit, deploy, then click Verify in Stripe.
6. Radar: block high-risk, 3-D Secure when required; low limits during the internal phase.
7. Test end-to-end in test mode first (GO_LIVE Gate A) with `sk_test`/`pk_test` values in the same secrets/variable.

## 12. Wallets and the signal feed — HUMAN

**Builder and treasury** (GO_LIVE B2 key ceremony):
1. Two new hardware wallets (bought from the manufacturer), seeds generated on-device, metal backups in two places, never photographed or typed. Use **separate** addresses for builder (collects builder fees) and treasury (receives fee-balance USDC deposits, pays payouts).
2. The builder address needs **≥ 100 USDC perps account value** on Hyperliquid before users can approve its builder fee (SPEC §6): deposit USDC to Hyperliquid from the builder wallet and keep it in the perps account.
3. Record both addresses (lower-case) in `BUILDER_ADDRESS` / `TREASURY_ADDRESS` (step 5) with a second person verifying on the device screen. No private key ever touches a server; payouts are signed in the admin's browser with the hardware wallet after maker-checker approval.
4. Evaluate Hyperliquid native multi-sig for the treasury [VERIFY current Hyperliquid docs] and record the decision.

**Signals keypair and the terminal** (`integrations/terminal/README.md` has the full procedure):
1. In a clean checkout of `joztern755/terminal.aijalon`: `…/quantscriptmarket/integrations/terminal/install.sh .` (copies `market_signals/`, applies `build-deploy.patch`: one new step `node market_signals/emit.js --terminal . --out-dir public` before the Hosting deploy, and no-cache headers for `/signals.json` + `/signals.sig`).
2. `node market_signals/keygen.js` → terminal repo **secret** `SIGNALS_ED25519_PRIVATE_KEY_PEM` (`gh secret set SIGNALS_ED25519_PRIVATE_KEY_PEM -R joztern755/terminal.aijalon` and paste at the prompt), terminal **variable** `SIGNALS_ED25519_PUBLIC_KEY_B64`, marketplace secret `SIGNALS_PUBKEY_B64` (same public key). Close the terminal window afterwards; the private key exists only in that GitHub secret.
3. Follow the terminal's CLAUDE.md rules (skill update), commit, push; after the next 00:30 UTC build, `curl -sS https://aijalon-terminal.web.app/signals.json` shows the feed.
4. Optional hardening of the terminal itself: it deploys with a JSON service-account key (`FIREBASE_SERVICE_ACCOUNT`); the same WIF pattern as this repo removes that key.

## 13. First admins

1. Each admin signs in once at https://aijalon.trade with Google/Apple and **enrols TOTP**.
2. `./infra/gcp/db_bootstrap.sh promote-admin app.aijalon@gmail.com` — refuses unless exactly one active, MFA-enrolled user has that e-mail. Repeat for the second admin (payouts and kill-switch lifts need **two different** admins).
3. Record each promotion in the admin console note / audit log (the SQL path bypasses the app's hash-chained audit writer), and tell the other admin. This is a break-glass path: the "unexpected secret read" alert fires because the script reads `DB_MIGRATOR_PASSWORD` — that is expected.

## 14. Go-live gates (infrastructure) and switching trading on

Business, legal and security gates are in `docs/GO_LIVE_CHECKLIST.md` (Gate A/B/C). The infrastructure gates below must all be true, with evidence, before `make go-live`:

- **G1** `make pin-check` clean; `backend/requirements.lock` committed; no `PIN_ME` anywhere (including `sandbox/Dockerfile`).
- **G2** Bootstrap re-run shows `sql ... OK` (HA, PITR, 30 backups, deletion protection, no public IP, IAM auth).
- **G3** `./infra/gcp/db_bootstrap.sh verify` passes against production.
- **G4** Deploy smoke green with `SMOKE_SKIP_PUBLIC` removed: `https://api.aijalon.trade/healthz` OK, `/v1/internal/*` → 403 at the edge, `*.run.app` of api/executor not reachable, served CSP == `infra/csp.txt`.
- **G5** Negative tests by hand: `curl https://<LB IP>/healthz -k -H 'Host: api.aijalon.trade'` → 403 (Cloud Armor); a request to `api.aijalon.trade` without Cloudflare's header cannot be produced (Cloudflare overwrites it); `gcloud kms decrypt` as the owner is **denied** (no human has decrypt).
- **G6** Test alerts received on two channels: trigger "SEC: Secret accessed by unexpected principal" by reading a secret as the owner (`gcloud secrets versions access latest --secret=TELEGRAM_OPS_CHAT_ID >/dev/null`); confirm the app's Telegram page with the kill-switch drill (GO_LIVE B4).
- **G7** mypy clean → set `continue-on-error: false` in `ci.yml`.
- **G8** PITR restore drill done on a clone (`gcloud sql instances clone aijalon-pg aijalon-pg-drill --point-in-time=…`), then the clone deleted (RUNBOOK §8).
- **G9** Firebase Auth: TOTP enrolment + sign-in tested on iOS Safari, Android Chrome, desktop; sign-in without MFA is refused by the API.
- **G10** Stripe live webhook delivered and credited once; Apple Pay domain verified.
- **G11** Builder ≥ 100 USDC perps value; addresses double-checked; signal feed fresh and verified by `/internal/ingest-signals`, candle store backfilled (run both once by hand while the others stay paused: `gcloud scheduler jobs run ingest-signals --location=asia-southeast1`, same for `candles-sync`).
- **G12** GitHub: production environment reviewers set; branch protection on; "require SHA-pinned actions" on.
- **G13** Cloudflare Pro (managed WAF) — required for Gate C (public), optional for Gate B (internal).
- **G14** HSTS preload submitted (Gate C only).

Then (HUMAN decision):

```bash
make go-live      # resumes every Cloud Scheduler job (SCHEDULER_SPEC in infra/gcp/env.sh) after you type GO-LIVE
```

Emergency stop of all scheduled work: `make pause-all` (the app's kill switches are the finer control; RUNBOOK).

## 15. Routine operations

| Task | How |
|---|---|
| Deploy | merge to `main` → approve `production` |
| Change a secret | `add_secret NAME` (step 5) → redeploy; disable the old version after the new revision is healthy: `gcloud secrets versions disable <n> --secret NAME` |
| Rotate `EDGE_AUTH_SECRET` | low-traffic window: add new version → `./infra/gcp/bootstrap.sh lb` (Cloud Armor) → `make dns` (Cloudflare) → redeploy api. Requests fail with 403 between the Cloudflare update and the new api revision (seconds–minutes); for zero-downtime, have the app accept a comma-separated old,new list first |
| Rotate `DB_MIGRATOR_PASSWORD` | add version → `./infra/gcp/db_bootstrap.sh users` (sets it on the DB) — the next migrate job uses it |
| KMS `agent-keys` | rotates automatically every 90 days; old versions stay enabled for decrypt; never destroy a version while any `agent_keys.kms_key_version` references it |
| Cloudflare IP ranges change | `./infra/gcp/bootstrap.sh lb` (re-reads https://api.cloudflare.com/client/v4/ips) |
| Scale | edit `infra/gcp/run/*.yaml` (instances, CPU, memory) → PR → deploy; DB tier: `gcloud sql instances patch aijalon-pg --tier=…` in the maintenance window |
| Break-glass DB session | `./infra/gcp/db_bootstrap.sh psql` (alerts fire; pgaudit logs DDL/ROLE) |
| Break-glass deploy | same steps locally: `PROJECT_ID=… REGION=asia-southeast1 DB_PRIVATE_IP=… STRIPE_PUBLISHABLE_KEY=… ./infra/gcp/deploy.sh preflight && … images && … migrate && … services` |

## 16. Things that could not be verified from the authoring environment

The authoring environment had no access to Google Cloud, Cloudflare, PyPI or Docker registries. Check these on the first run; each has a fallback:

| Item | Symptom | Fallback |
|---|---|---|
| Cloud Scheduler counted as "internal" for executor's `ingress: internal` | Scheduler jobs get 404/403 | set executor ingress to `internal-and-cloud-load-balancing` (no LB points at it, so it stays unreachable) — or front the jobs with Pub/Sub push |
| Direct VPC egress with the gen1 (gVisor) sandbox | `services replace` rejects the sandbox spec | `SANDBOX_EGRESS_MODE=connector ./infra/gcp/bootstrap.sh network` then deploy (connector inside the isolated VPC, same deny-all firewall) |
| IAM conditions on `roles/cloudsql.client` / `instanceUser` | proxy sidecar logs 403 | `SQL_IAM_CONDITIONS=0 ./infra/gcp/bootstrap.sh sa` |
| `gcloud run services replace` with sidecars / Direct VPC annotations needing `launch-stage: BETA` | replace error mentions launch stage | add `run.googleapis.com/launch-stage: BETA` under `metadata.annotations` of the template |
| firebase-tools with WIF `external_account` credentials | Hosting deploy auth error | upgrade firebase-tools; or deploy Hosting through the Firebase Hosting REST API (`sites.versions` / `releases`) with the WIF access token (`token_format: access_token` in the auth step). Never fall back to a JSON key |
| `firebase.viewer` needed by `firebase deploy` | permission error on project get | keep it; otherwise remove it from bootstrap `sa` step |
| Firebase applies custom headers to `/__/auth/*` | irrelevant — our document headers are scoped by the regex `^/([^_].*|_([^_].*)?)?$`, which excludes `/__/` | — |
| Cloudflare managed ruleset ids (Pro) | PUT error | select "Cloudflare Managed Ruleset" / "OWASP Core Ruleset" in the dashboard and copy their ids into `dns.sh` |
| Apple provider REST payload shape | 400 from `firebase_auth.sh apple` | use the Firebase console |
| Python package versions (`# verify`) | `make lock` resolution error | nearest release |

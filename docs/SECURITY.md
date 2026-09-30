# aijalon.trade — Security Model and Threat Model

Version: 2026-09-30 · Owner: security lead [●] · Review: quarterly and after every SEV1/SEV2 incident
Related: `docs/SPEC.md` §2, §4, §5 · `docs/RUNBOOK.md` · `docs/INCIDENT_RESPONSE.md` · `docs/DATA_PROTECTION.md` · `docs/GO_LIVE_CHECKLIST.md`

> **Status note (honest).** This document describes the *intended* controls from SPEC v1, and the threats they address. Not every control is implemented or verified yet. Items are tagged:
> - **[BUILT]**: present in the code as of this version (spot-checked; not audited);
> - **[DESIGN]**: specified but not yet verified in the deployed system;
> - **[GAP]**: a known missing control or open design question.
>
> The go-live checklist requires every [DESIGN] item on the real-money path to be verified, and every [GAP] to be closed or explicitly accepted, before real money is used. This document is **not** evidence of compliance with any law or standard.

---

## 1. What we protect (assets), ranked

| # | Asset | Why it matters | Worst case |
|---|---|---|---|
| A1 | **Agent private keys** (one per user; trade-only) | Can place orders in users' accounts | Unwanted trades. For example, an attacker trades users into a thin market against their own position, extracting value **without** any withdrawal |
| A2 | **Treasury / builder wallet key** | Holds fee-balance USDC and receives builder fees | Theft of treasury funds (users' Fee Balances and creator payables) |
| A3 | **Ledger integrity** (fee balances, payables, profit share) | This is the money record | Mis-billing, fraudulent payouts, undetectable theft |
| A4 | **Signal integrity** (terminal Ed25519 feed; sandbox outputs) | Drives all trading | Mass wrong trades across all subscribers |
| A5 | **Admin accounts, and the maker-checker workflow** | Can lift kill switches, approve payouts, list strategies | Bypass of every other control |
| A6 | **Personal data** (emails, wallet↔identity link, KYC references, hashed IPs) | PDPA; user safety | Breach notification; doxxing of wallets |
| A7 | **Creator code** | Creators' intellectual property; our marketplace promise | Leak destroys trust and value |
| A8 | **Secrets** (Stripe, Telegram, email, signal public key pin) | Payments and alerts | Fake credits via a forged webhook; alert suppression |
| A9 | **Availability of exits** | Users rely on reduce-only exits | Positions stay open during a crisis |

## 2. Trust boundaries

```
[User browser + wallet] --HTTPS--> Cloudflare (WAF, rate limit) --> Firebase Hosting (SPA)
        |                                                  \--> Cloud Run api (public ingress)
        | EIP-712 signatures (approveAgent, approveBuilderFee, usdSend deposit)
        v
[Hyperliquid L1 / API]  <---- orders (agent-signed) ---- Cloud Run executor (internal ingress; KMS decrypt)
                                                              ^  OIDC
                                                         Cloud Scheduler
Cloud Run sandbox (no egress, no SA perms) <-- code + bars / --> weights
Cloud SQL (private IP, CMEK) <-- api (app_api) / executor (app_executor) / migrator
Cloud KMS HSM key "agent-keys": api=encrypt-only, executor=decrypt-only
Terminal GitHub Action --Ed25519-signed signals.json--> fetched by /internal/ingest-signals
Stripe --signed webhook--> api /webhooks/stripe
Admin browser + hardware wallet --> usdSend (payouts); treasury key never on servers
GitHub (main, 2-person rule) --WIF--> deployer SA --> Cloud Run / Hosting / migrations
```

## 3. STRIDE threat model per component

S = Spoofing, T = Tampering, R = Repudiation, I = Information disclosure, D = Denial of service, E = Elevation of privilege.

### 3.1 Web SPA (Firebase Hosting, behind Cloudflare)

| | Threat | Mitigation | Status |
|---|---|---|---|
| S | Phishing clone of aijalon.trade asking for seed phrases | Never ask for seeds (repeated in UI and emails); HSTS preload; the domain is shown in EIP-712 prompts; users are warned in alert emails | [DESIGN] |
| T | XSS or supply-chain script injection that alters typed data (e.g. swaps the agent address for an attacker's) | Strict CSP (no inline script); SRI; no npm runtime deps; Firebase JS pinned; **server returns typed data, and the server verifies on-chain that the approved agent is the one we generated** (`/agents/{id}/confirm` checks `extraAgents`) | [BUILT: CSP module] / [DESIGN] |
| T | User tricked into signing `approveBuilderFee` above 0.1%, or an attacker's builder | Server only confirms our builder address and rate; wallet shows the fields | [DESIGN] |
| R | User denies accepting the terms | Append-only `consents`, with version, context, hashes and a hash of the rendered text | [DESIGN] |
| I | Wallet addresses leaked via referrer or analytics | Referrer-Policy strict-origin; no third-party trackers | [DESIGN] |
| D | Traffic floods | Cloudflare, CDN | [DESIGN] |
| E | Clickjacking the approve flow | `frame-ancestors 'none'` | [DESIGN] |

### 3.2 API (Cloud Run `api`, FastAPI)

| | Threat | Mitigation | Status |
|---|---|---|---|
| S | Stolen Firebase ID token | Short-lived tokens; **MFA claim required**; **step-up (auth_time ≤ 300 s)** for sensitive actions; alert on login from a new country | [BUILT: auth.py] |
| S | Forged Stripe webhook that credits a balance | Verify the Stripe signature; idempotency on the event id; amount comes from the Stripe object, not the client; credit = net of processor fee | [BUILT: stripe_pay.py] / verify |
| S | Forged USDC deposit claim | Credit only from on-chain `usdSend` to the treasury, found by scanning; idempotent on tx hash; the sender must be the user's verified master address | [DESIGN] |
| T | IDOR: changing another user's subscription, allocation or withdrawal address | Every query is scoped by `user_id` from the token; authorisation tests per router | [DESIGN] |
| T | Race conditions: double spend of the fee balance, double withdrawal | DB transactions with row locks or serialisable isolation on balance-affecting operations; idempotency keys; ledger Σ=0 constraint | [DESIGN] |
| R | Admin denies an action | Hash-chained `audit_log` for every security or financial event | [BUILT: audit.py] |
| I | Agent key ciphertext exposed via the API | `app_api` has no SELECT on `agent_keys.key_ciphertext` (column privilege); api SA can encrypt only | [DESIGN: verify grants] |
| I | Verbose errors or logs leaking secrets | Typed errors; the log filter redacts 64-hex strings and tokens | [BUILT: logging.py] |
| D | API abuse | Per-user and per-IP rate limits; Cloudflare rules; Cloud Run max instances | [BUILT: ratelimit.py] |
| E | Normal user reaches admin routes | Role check plus step-up plus a separate admin allowlist; admin routes separated and tested | [DESIGN] |

### 3.3 Executor (Cloud Run `executor`, internal ingress)

| | Threat | Mitigation | Status |
|---|---|---|---|
| S | Anyone calling `/internal/tick` | Internal ingress only; Scheduler OIDC token verified (audience + SA email) | [DESIGN] |
| T | Wrong orders from bad data (stale, manipulated mark) | Fail-closed guards: whitelist, leverage cap, size ≤ 0.5% of 24h volume and ≤ 2% of OI, IOC within 0.5% of mid, mark/oracle deviation ≤ 2%, data ≤ 60 s old, circuit breaker | [BUILT: domain/risk.py] |
| T | Replay or duplicate orders | `cloid` unique per order; idempotent tick | [BUILT/DESIGN] |
| I | Plaintext agent key leaked from memory or logs | Decrypt per tick; never log; redaction; no core dumps; minimal dependencies in the executor image | [DESIGN] |
| D | Hyperliquid rate limits or outage stop exits | Backoff; prioritise reduce-only exits within the tick; alert on tick failure | [DESIGN] — rate limits **unverified** (SPEC §6) |
| E | Executor compromise leads to mass unwanted trades | Separate SA; **KMS decrypt audit logs with alert on decrypt-count anomaly**; kill switch; per-user caps; no public ingress | [DESIGN] |

### 3.4 Sandbox (Cloud Run `sandbox`, creator code)

| | Threat | Mitigation | Status |
|---|---|---|---|
| E | Sandbox escape from Python (dunder tricks, import hooks) | AST allowlist (math and statistics only; no `_` attributes, no getattr, eval, exec, etc.); separate service with **no egress, no SA permissions, no DB**; CPU 2 s, 256 MB | [BUILT: sandbox/runner.py, validate.py] |
| I | Code exfiltrates other creators' code or subscriber data | The sandbox never holds secrets or other code; one script per request; no egress | [DESIGN] |
| T | Script behaves differently in review vs production (time bombs) | Deterministic (no time, no random); same inputs give the same outputs; output validated (keys ⊆ MARKETS, finite, ≤ MAX_LEVERAGE) | [BUILT] |
| D | Infinite loops or memory bombs | Hard limits; the process is killed; the failure counts toward the strategy's circuit breaker | [DESIGN] |
| I | **Who decrypts creator code?** The sandbox has no KMS rights, and `app_api` has no SELECT on `code_ciphertext`. | **[GAP]** Define the component that decrypts code (recommended: a dedicated KMS key `creator-code`, decrypt granted only to a narrow "sandbox-dispatcher" identity, or to the executor). It passes plaintext to the sandbox over an authenticated internal request, and never stores or logs it. Staff access to code needs maker-checker approval and an audit record. | [GAP] |

### 3.5 Database (Cloud SQL Postgres 16)

| | Threat | Mitigation | Status |
|---|---|---|---|
| T | Ledger or audit tampering | Append-only (UPDATE/DELETE raise), hash chain, deferred Σ=0 trigger; `app_api` has no UPDATE/DELETE on the ledger, audit or consents; daily chain verification | [BUILT: migrations] / verify grants |
| I | DB exfiltration | Private IP only; IAM DB auth; CMEK; agent keys are KMS ciphertext only; creator code is ciphertext | [DESIGN] |
| D | Data loss | PITR + automated backups; restore drill (RUNBOOK §8) | [DESIGN] |
| E | Migrator role misuse | `app_migrator` is used only by the deploy pipeline; no standing human DB superuser; break-glass is logged | [DESIGN] |

### 3.6 KMS and Secret Manager

| | Threat | Mitigation | Status |
|---|---|---|---|
| E | A human or SA gains decrypt on `agent-keys` | IAM: decrypt only for the executor SA; **no human holds decrypt**; an IAM change alert on the keyring; org policy prevents SA key creation | [DESIGN] |
| T | Key destroyed or disabled (sabotage) | Key-version destruction has a scheduled delay (Cloud KMS default); alert on `DestroyCryptoKeyVersion` / disable | [DESIGN] |
| I | Secrets read by deployer | The deployer SA can deploy, but cannot access secret versions | [DESIGN] |

### 3.7 Signals pipeline (terminal → marketplace)

| | Threat | Mitigation | Status |
|---|---|---|---|
| S/T | Forged or modified `signals.json` | Ed25519 signature verified against a **pinned** public key (Secret Manager / config); reject if stale (> 36 h) or future-dated; `engine_hash` recorded | [DESIGN] |
| E | Terminal repo or GitHub secret compromised, leading to validly signed malicious signals | CREST output constrained to long-only weights {0,1,2} on whitelisted markets; guards cap size; alert on an unusual signal change (for example, all strategies flip on the same day); 2-person rule on the terminal repo; key rotation procedure | [DESIGN] / [GAP: 2-person rule on the terminal repo] |

### 3.8 Hyperliquid integration

| | Threat | Mitigation | Status |
|---|---|---|---|
| T | Oracle or market manipulation on HIP-3 markets (JELLY-style) | Size caps vs OI and volume; oracle deviation guard; OI-spike and funding-spike alerts leading to auto-pause of the market; human escalation (§6) | [BUILT: risk.py, alerts_rules.py] |
| T | Validator or deployer forced settlement | Cannot be prevented: disclosed in the Terms and Risk Disclosure; reconciliation and profit-share hold procedure (RUNBOOK §10) | Accepted risk |
| S | The user changes or revokes the agent on-chain | Detected by polling; alert; subscription paused | [DESIGN] |
| I | Builder code reveals which addresses use us | Public by design; disclosed | Accepted risk |

### 3.9 Payments (Stripe) and treasury

| | Threat | Mitigation | Status |
|---|---|---|---|
| S | Card fraud and chargebacks | Stripe Radar; 3-D Secure where available; velocity limits on new accounts; chargeback leads to a balance debit and suspension | [DESIGN] |
| T | Payout to an attacker's address | Maker-checker (two different admins); step-up; payout-address change hold (48 h); hardware wallet with on-device address verification; per-transaction and daily caps; test transfer to new addresses | [DESIGN] |
| E | A single admin drains the treasury | The treasury key is only on hardware wallets held by named officers; server-prepared payouts need two approvals; reconcile treasury vs ledger daily. **Evaluate Hyperliquid native multi-sig for the treasury** [VERIFY availability] | [DESIGN] / [GAP] |

### 3.10 Admin console and ops

| | Threat | Mitigation | Status |
|---|---|---|---|
| S | Admin account takeover | Admin identities separate from personal accounts; **FIDO2 security keys** for admin Google accounts; TOTP at minimum for Firebase; step-up on every admin action; alert on admin login | [DESIGN] / [GAP: FIDO2 for admins] |
| E | One admin lifts a kill switch or lists a malicious strategy alone | **Maker-checker** (maker ≠ checker, enforced in the DB and API) for: lifting kill switches and pauses, payouts and withdrawals, payout-address overrides, listing or delisting, in-house price changes, role elevation, economics config changes | [DESIGN] |
| R | Disputed admin action | Hash-chained audit log; exported daily to write-once storage (bucket retention lock) | [DESIGN] / [GAP: WORM export] |

### 3.11 CI/CD and supply chain

| | Threat | Mitigation | Status |
|---|---|---|---|
| T | Malicious commit to main | Branch protection: **2-person rule** (PR + ≥1 approving review from someone other than the author; CODEOWNERS for `security/`, `ledger/`, `execution/`, `hl/`, `payments/`, `migrations/`, `infra/`); no force pushes; required status checks; signed commits recommended | [DESIGN] |
| T | Compromised dependency or Action | `requirements.txt` pinned **with hashes**; Actions pinned to **commit SHA**; Dependabot or Renovate; `pip-audit`; gitleaks; bandit / semgrep; CodeQL; container image scanning; SBOM | [DESIGN] |
| E | Stolen CI credentials | Workload Identity Federation (no JSON keys); deploy only from `main` via a protected environment with required reviewers; deployer SA cannot read secrets or decrypt | [DESIGN] |

## 4. Key custody

| Key | Where | Who can use | Rotation / recovery |
|---|---|---|---|
| Agent keys (per user) | Generated in the `api` process, **encrypted with KMS (HSM) immediately**, stored as ciphertext in Cloud SQL. Plaintext exists briefly in api memory at generation, and in executor memory during a tick. | Executor SA only (KMS decrypt) | Per-user rotation: the user approves a new agent (step-up), and the old one is revoked on-chain. Mass rotation after compromise requires **every user to re-sign `approveAgent`** (we cannot rotate without them). See RUNBOOK §7. Approving a new agent under the same name is expected to replace the old one **[VERIFY]**. |
| KMS key `agent-keys` | Cloud KMS, HSM protection level, `asia-southeast1` | Encrypt: api SA. Decrypt: executor SA. **No human.** | Automatic rotation of the primary version (e.g. every 90 days) [DESIGN]; re-wrap old ciphertexts in the background; never destroy a version while ciphertexts reference it |
| Treasury / builder key | **Hardware wallets only** (e.g. two devices with the same seed, held by two named officers in separate locations; seed backups on metal, in two separate secure locations). Never typed into a computer. | Signing in the admin browser, after maker-checker approval | Key ceremony (GO_LIVE_CHECKLIST §B). Compromise: move funds to a new address generated in a new ceremony; update config through maker-checker. Consider Hyperliquid multi-sig. |
| Signal signing key (Ed25519) | Terminal repo GitHub secret | Terminal Action | Rotation: publish the new public key, dual-verify during the overlap, then pin the new key through maker-checker config |
| Stripe keys | Secret Manager; **restricted keys** with minimum scopes; webhook secret separate | api SA | Rotate yearly and on suspicion |
| Firebase / Google admin | Google Cloud IAM | Named admins with FIDO2 | Quarterly access review |

## 5. Authentication, MFA and step-up

- Users sign in with Google or Apple only (no passwords stored by us). **TOTP MFA is mandatory.** The API rejects tokens without `firebase.sign_in_second_factor` [BUILT].
- **Step-up** (fresh sign-in ≤ 5 minutes plus MFA) is required for:
  - connecting or rotating an agent;
  - subscribing, and changing allocation or leverage;
  - withdrawals;
  - changing the payout address;
  - creator publishing;
  - all admin actions.
- An **MFA reset** or a **login from a new country** raises an alert. After an MFA reset, withdrawals and payout-address changes are held for [48 h] [DESIGN].
- Admins additionally use FIDO2 hardware keys on their Google identity, a separate admin allowlist, and have **no standing Cloud Console production access** (just-in-time elevation, time-bound, logged) [GAP].
- Passkeys are planned for phase 2.

## 6. Segregation of duties

**Service accounts (IAM).** See SPEC §2.1: api encrypts only; executor decrypts only; sandbox has nothing; deployer deploys but cannot read secrets.

**Database roles.**
- `app_api`: no SELECT on `agent_keys.key_ciphertext` or `strategy_versions.code_ciphertext`; no UPDATE/DELETE on the ledger, audit or consents.
- `app_executor`: can read what it needs to place orders and write orders and fills.
- `app_migrator`: DDL, used by the pipeline only.

**Humans (maker-checker).**

| Action | Maker | Checker | Enforcement |
|---|---|---|---|
| Engage a kill switch or pause | Any admin (or automatic) | Not required (safety first) | Audit log |
| **Lift** a kill switch, pause or circuit breaker | Admin A | Admin B ≠ A | API + DB constraint |
| Payout or fee-balance withdrawal | Admin A (approved_1) | Admin B (approved_2), then signs with a hardware wallet | `checker_admin ≠ maker_admin` |
| List or delist a strategy; approve a creator version | Reviewer A | Admin B | API |
| Economics or risk-limit config change | Engineer (PR) | Reviewer (2-person rule) plus deploy approval | GitHub |
| Grant the admin role | Admin A | Admin B | API + audit |

**For a small team:** with only two admins, both must be available for lifts and payouts. **Engaging** a safety control never needs a second person.

## 7. Anomaly detection and automatic pause (lessons from the JELLY incident)

**The lesson from March 2025.** On Hyperliquid's JELLY perp, a trader built a large position in a thin market, pushed the external price, and left a liquidity vault holding a loss. Validators then delisted the market and settled it at an administratively chosen price. For us, this means:

1. **Thin markets are attack surfaces.** Size is capped relative to OI and volume. A manipulated market can still move against our users.
2. **Venue governance can override market prices.** We cannot rely on the venue to protect our users. A forced settlement is a force majeure event, and needs a specific ops procedure.
3. **Speed matters more than perfection.** Automation must pause first. A human decides next, and a human (two humans) must lift it.

**Detection → action → escalation**

| Signal (alerts_rules.py) | Automatic action | Page | Human decision |
|---|---|---|---|
| Mark vs oracle > 2% on a traded market | Critical: `new_entries_paused:{coin}` (reduce-only on that market) | Telegram + email to ops, ack within 15 min | Assess; escalate to `kill_switch_market:{coin}` (no orders at all) if manipulation is suspected; lift via maker-checker |
| OI +50% in 1 h on a traded market | Critical: market auto-pause | Same | Same |
| Funding spike | Warn or critical per threshold | Ops | Review exposure |
| User drawdown > 20% of allocation in 24 h | Warn to the user and ops | Ops | Check for execution bugs or a manipulated market |
| Order-rejection burst | Per-subscription circuit breaker after 3 rejections | Ops | Fix the cause; lift via maker-checker |
| Agent approval changed or revoked on-chain | Pause that user's subscriptions | User and ops | Contact the user |
| Ledger ↔ on-chain mismatch > $1 | Critical: hold payouts | Ops | RUNBOOK §6 |
| Unusual KMS decrypt volume, or IAM change on the keyring | Critical: `kill_switch_global` [DESIGN] | Ops + security | Incident response |
| Signal set changes abnormally (many strategies flip at once), or bad signature | Reject the feed and hold | Ops | Verify with the terminal owner |

**Escalation rules.**
- If a critical alert is not acknowledged within [30 min], escalate automatically to the second on-call. If it is still not acknowledged after [60 min], set `new_entries_paused` globally [DESIGN].
- **Kill switches are hard stops. They block even exits** (see the `domain/risk.py` contract). If positions must be flattened during a kill switch, ops do it deliberately (RUNBOOK §4).

**Known mismatch [GAP].** The admin flags API schema (`FLAG_KEY_PATTERN` in `api/schemas.py`) accepts `kill_switch_global`, `new_entries_paused` and `kill_switch_market:{coin}`, but not the per-market auto-pause key `new_entries_paused:{coin}` that the executor reads (`ports.Flags.paused_entry_markets`). **Admins must be able to view and lift (maker-checker) per-market auto-pauses.** Please align these.

## 8. Secure SDLC

- **Two-person rule for `main`:** PR required; at least one approval from someone other than the author; CODEOWNERS on sensitive paths; stale approvals dismissed on new commits; no admin bypass; linear history.
- **CI (required checks):**
  - unit tests (the domain layer uses stdlib only);
  - lint and type checks;
  - **gitleaks**;
  - `pip-audit` / OSV;
  - bandit or semgrep (Python);
  - CodeQL;
  - SDK parity test against the official Hyperliquid SDK;
  - migration dry-run;
  - a check that published legal docs contain no `[COUNSEL]`/`[●]` markers.
- **Pinned everything:**
  - Python dependencies pinned **with hashes**;
  - GitHub Actions pinned to **full commit SHAs**;
  - container base images pinned by digest;
  - Firebase JS pinned plus SRI.
- **Secrets:** Secret Manager only; push protection on; never in env files committed; `.gitignore` covers `.env*`, `*.pem`, `*.key`, SA JSON.
- **Deploy:** GitHub Actions → WIF → deployer SA; production environment requires a reviewer; migrations are forward-only and reviewed.
- **Review checklist for money code:** idempotency, integer micro-USD, floor rounding, Σ=0, authorisation scope, audit event emitted.

## 9. Penetration test plan (before real-money scale)

- **When:** after the internal real-USDC phase and **before public launch** (GO_LIVE_CHECKLIST Gate C). Retest the fixes before launch. Repeat yearly and after major changes.
- **Who:** an independent firm with web and cloud experience (and crypto signing experience preferred).
- **Scope:**
  1. Web and API: auth, MFA or step-up bypass, IDOR, business logic (fee balance, profit share, referrals, withdrawals), race conditions and double spend.
  2. Signing flows: typed-data substitution, agent confirmation logic, builder approval.
  3. Sandbox escape and resource exhaustion.
  4. GCP configuration review: IAM, KMS, Cloud SQL, Cloud Run ingress, Scheduler OIDC.
  5. Stripe webhook forgery and replay; USDC deposit crediting.
  6. Admin console and maker-checker bypass.
  7. Signals feed verification.
  8. CI/CD and supply chain.
- **Exit criteria:** no open critical or high findings; mediums have a dated plan.

## 10. Bug bounty (later)

Start after public launch, once there is capacity to triage. Begin with a private programme (invite-only), then go public. Publish `security.txt` and a safe-harbour policy (the AUP already points researchers to [security@aijalon.trade]). Rewards are scaled to impact. Agent-key extraction, the treasury, ledger manipulation and sandbox escape are the top tier.

## 11. Web hardening (reference)

- **CSP:** `default-src 'self'`; `script-src 'self'` plus pinned gstatic with SRI; `connect-src` for the API, Firebase and Hyperliquid; `frame-ancestors 'none'`.
- **Headers:** HSTS preload; `X-Content-Type-Options: nosniff`; `Referrer-Policy: strict-origin`; `Permissions-Policy` minimal.
- **CORS:** only `https://aijalon.trade`.

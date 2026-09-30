# aijalon.trade — Data Protection: Inventory, Classification, Retention, Backups

Version: 2026-09-30 · Owner: DPO [●] with engineering lead [●] · Review: every 6 months, and whenever a table or processor is added
Related: `legal/privacy.md` (the user-facing PDPA notice), `docs/SECURITY.md`, `docs/INCIDENT_RESPONSE.md` §6

> **Status note.** This document records the intended design (SPEC v1 and `backend/migrations/0001_init.sql`). The retention periods marked **[COUNSEL]** are proposals that must be confirmed against Malaysian record-keeping, tax, anti-money-laundering and PDPA requirements. Encryption and backup settings marked **[VERIFY]** must be checked in the live GCP project before go-live. **This is not a statement of compliance.**

---

## 1. Classification levels

| Level | Meaning | Handling |
|---|---|---|
| **S — Secret** | Keys and credentials whose leak directly enables theft or unwanted trading | HSM/KMS or hardware wallet; never in the DB in plaintext; never logged; no human access |
| **R — Restricted** | Personal data; financial records; KYC references; creator code | Encrypted at rest (CMEK); least-privilege roles; access logged; masked in logs |
| **C — Confidential** | Internal operational data, config, non-personal analytics | Staff only |
| **P — Public** | Published strategy stats (k ≥ 5), legal docs, public config | May be published |

## 2. Data inventory

| Data | Table / location | Class | Personal? | Encryption / protection | Access | Retention (proposed) |
|---|---|---|---|---|---|---|
| Agent private keys | `agent_keys.key_ciphertext` | **S** | Linked to a user | KMS HSM envelope encryption; column hidden from `app_api` | executor SA (decrypt) | Until rotated or revoked, then **crypto-shred**: delete the ciphertext within [30] days. Keep only the address and status. |
| Treasury / builder key | Hardware wallets (offline) | **S** | No | Hardware wallet; metal seed backups in 2 locations | Named officers | Life of the business |
| Stripe / Telegram / email secrets | Secret Manager | **S** | No | Google-managed + IAM | api SA | Rotate yearly |
| Signal public key pin; signing key | Secret Manager / terminal GitHub secret | S | No | — | api / terminal Action | Until rotated |
| User profile (email, display name, firebase_uid, role, plan, status, country attested) | `users` | R | **Yes** | CMEK at rest [VERIFY] | api, executor (limited) | Account life + [90 days], then pseudonymise (keep `id`) |
| Auth identities, MFA secrets | Firebase Auth / Identity Platform | R/S | Yes | Google | Google; admins via console | Deleted on account deletion |
| Consents | `consents` (append-only) | R | Yes | CMEK | api (insert/select) | Account life + [7 years] [COUNSEL] |
| Hashed IP, user-agent hash, device-fingerprint hash | `consents`, `audit_log`, referral checks | R | **Yes (treated as personal data)** | Keyed hash (HMAC with the secret pepper `AUDIT_PEPPER_B64` from Secret Manager, in `config.py`) [VERIFY: every hash site uses it] | api | Security logs [12 months]; in consents and audit logs, as for those records |
| Wallet addresses (master, trading, agent) | `wallets`, `agent_keys`, `subscriptions`, `fills` | R | **Yes** (address ↔ identity link) | CMEK; **never shown publicly or to creators** | api, executor | As ledger (financial record) [COUNSEL] |
| Subscriptions, orders, fills, funding | `subscriptions`, `orders`, `fills`, `funding_events` | R | Yes | CMEK | api, executor | [7 years] (financial records) [COUNSEL] |
| Ledger (fee balances, payables, revenue) | `ledger_*` (append-only, hash-chained) | R | Yes | CMEK; hash chain | api (insert/select), executor | [7 years] after the end of the financial year [COUNSEL] |
| Deposits, withdrawals, payouts | `deposits`, `withdrawals`/`payouts` | R | Yes | CMEK | api | [7 years] [COUNSEL] |
| Stripe payment metadata (id, brand, last 4, country) | `deposits.external_ref` + Stripe | R | Yes | Stripe PCI scope; **we store no PAN** | api | [7 years] [COUNSEL]; Stripe keeps its own records |
| Creator KYC documents and biometrics | **At the KYC provider only** | R (sensitive) | Yes | Provider | Provider; we see status and reference | Per the provider's and AML requirements [COUNSEL] |
| KYC status and reference | `kyc_creators` | R | Yes | CMEK | api (admin) | Creator relationship + [7 years] [COUNSEL: AML] |
| Creator code | `strategy_versions.code_ciphertext` | R (trade secret) | Maybe | Encrypted [GAP: KMS key not yet defined; see SECURITY §3.4] | executor (reads the ciphertext per `0002_roles.sql`, passes plaintext to the sandbox); staff via maker-checker | Listing life + wind-down; archive [7 years] for disputes [COUNSEL] |
| Signals, strategy stats, backtests | `signals`, `strategies`, `strategy_versions.backtest` | C/P | No | CMEK | api | Indefinite |
| Reviews, posts, purchases | `reviews`, `posts`, `post_purchases` | R/P | Yes | CMEK | api | Content until deleted by the author or us; purchases as ledger |
| Alerts | `alerts` | R | Yes | CMEK | api | [12 months] |
| Audit log | `audit_log` (append-only, hash chain) + daily WORM export | R | Yes | CMEK; retention lock on export [GAP] | api (insert), admins (read) | [7 years] [COUNSEL] |
| Telegram chat id | `users` / notifier config | R | Yes | CMEK | api | Until the user disconnects |
| Application logs | Cloud Logging | C/R | Possibly (redacted) | Google; redaction filter | Engineers | [30 days] hot, [1 year] archive |
| Showcase wallets | `showcase_wallets` | C → P after its month ends | Only if a person's wallet (in-house or consenting creator only) | CMEK | api | Indefinite once revealed |

## 3. Retention schedule and deletion

- **Automated jobs** [DESIGN]:
  - purge security logs and hashed IPs older than [12 months];
  - purge alerts older than [12 months];
  - crypto-shred revoked or rotated agent keys after [30 days];
  - pseudonymise closed accounts after [90 days].
- **Pseudonymisation on closure:** null out email and display name, replace them with `deleted-<id>`, and delete the Firebase user. **Keep** the ledger, consents, fills and audit data (a legal obligation or a legitimate need to defend claims) [COUNSEL].
- **Append-only tables cannot be edited.** Erasure requests for data in them are answered with the legal-retention reason. The data is excluded from all non-legal processing.
- **On-chain data** cannot be deleted by anyone. This is disclosed in the privacy notice.
- **Legal hold:** the DPO can suspend purges for specific users or records during disputes or investigations.

## 4. Encryption

| Layer | Control | Status |
|---|---|---|
| In transit (public) | TLS 1.2+ at Cloudflare and Firebase Hosting; HSTS preload; API only over HTTPS | [DESIGN] |
| In transit (internal) | Cloud Run ↔ Cloud SQL via the Cloud SQL connector / private IP with TLS; Scheduler → executor over HTTPS with an OIDC token | [VERIFY] |
| At rest (DB) | Cloud SQL with **CMEK** (a separate KMS key from `agent-keys`) | [VERIFY] |
| At rest (secrets) | Agent keys: envelope encryption under the KMS **HSM** key `agent-keys`. Creator code: dedicated key [GAP]. Secret Manager for API secrets. | [BUILT: kms.py] / [GAP] |
| Backups | Encrypted with the instance's CMEK | [VERIFY] |
| Hashing | Personal identifiers (IP, user-agent, device) hashed with a **keyed HMAC** (pepper in Secret Manager). A plain SHA-256 of an IPv4 address can be reversed by brute force. | [VERIFY] |

## 5. Access reviews

| Review | Frequency | Who | Evidence |
|---|---|---|---|
| GCP IAM (project, KMS keyring, Secret Manager, Cloud SQL) | **Quarterly**, plus on any staff change | Security lead + one other admin | Exported IAM policy diff, signed off in the ops log |
| Admin role list (app) and hardware-key enrolment | **Monthly** | Two admins | Audit-log extract |
| DB roles and grants (`app_api`, `app_executor`, `app_migrator`; column privileges on key and code ciphertext; no UPDATE/DELETE on append-only tables) | Quarterly + after every migration touching grants | Engineering lead | `\dp` output checked against the expected grants (CI test recommended) |
| GitHub: org members, branch protection, CODEOWNERS, WIF bindings, Actions pinning | Quarterly | Engineering lead | Screenshot or export |
| Processor list and contracts (DPAs) | Every 6 months | DPO | Processor register |
| **Offboarding** | Within 24 h of departure | Security lead | Revoke Google identity, admin role, hardware keys, GitHub, Telegram ops chat |

## 6. Backups and disaster recovery

| Item | Setting / target | Status |
|---|---|---|
| Cloud SQL automated backups | Daily; retention [30] days | [VERIFY] |
| Point-in-time recovery | Enabled; log retention [7] days | [VERIFY] |
| **RPO** (single-instance failure or corruption) | **≤ 5 minutes** (PITR) | Target |
| **RTO** (restore and resume trading) | **≤ 4 hours** (RUNBOOK §8) | Target; drill monthly |
| High availability | Cloud SQL regional HA (multi-zone within `asia-southeast1`) | [CONFIRM cost] |
| Regional disaster (Singapore region unavailable) | Cross-region backup copy [CONFIRM target region]. **RPO ≤ 24 h, RTO ≤ 48 h.** Hyperliquid on-chain data and Stripe events are re-ingested idempotently to close the gap. | [GAP]. **Cross-border transfer implications (PDPA); DPO sign-off needed.** |
| KMS keys | Cannot be exported (HSM). **A regional loss of the KMS key makes agent-key ciphertexts unrecoverable.** In that case, users re-approve new agents; no funds are at risk (non-custodial). | Accepted; documented |
| Treasury key | Hardware wallets + metal seed backups in 2 separate locations | Key ceremony |
| Config and infrastructure | Infrastructure-as-code in `infra/` (git) | [DESIGN] |
| Legal docs and consents | Git (docs) + DB (consents) | — |
| Restore testing | **Monthly** PITR clone test (RUNBOOK §8) with hash-chain verification; results logged | [DESIGN] |

**Why these targets.** Strategies trade on daily bars, and user funds are non-custodial. So a few hours of downtime usually means delayed trades, not lost funds. **However, users' open positions remain exposed during an outage.** The risk disclosure tells users they can always manage their positions on Hyperliquid directly.

## 7. Data subject requests (PDPA)

1. The request arrives at [dpo@aijalon.trade] or in the app.
2. Verify identity: the request must come from the account email, plus a step-up sign-in.
3. Log it in the DSR register.
4. **Access:** export the user's profile, consents, subscriptions, ledger, deposits and alerts (JSON/CSV) within the PDPA deadline [COUNSEL: confirm; generally 21 days].
5. **Correction:** update mutable fields. For append-only records, add a correction note.
6. **Withdrawal of consent / closure:** cancel subscriptions (reduce-only per the Terms), refund the Fee Balance per the Refund Policy, pseudonymise per §3.
7. **Portability:** machine-readable export, where the right applies [COUNSEL].
8. Close in the register with the date and what was provided.

## 8. Processor register (summary)

| Processor | Purpose | Data | Location | DPA in place? |
|---|---|---|---|---|
| Google Cloud / Firebase | Hosting, DB, KMS, auth | All platform data | Singapore (auth may be global) [VERIFY] | [ ] |
| Cloudflare | DNS, WAF, rate limiting | IPs, request metadata | Global | [ ] |
| Stripe | Payments | Payment data, email | Global (incl. US) | [ ] (Stripe terms) |
| KYC provider [TBD] | Creator verification | ID documents, biometrics | [●] | [ ] |
| Email provider [TBD] | Transactional email | Email, alert content | [●] | [ ] |
| Telegram | Opt-in alerts | Chat id, alert content | Global | n/a (user-initiated) |

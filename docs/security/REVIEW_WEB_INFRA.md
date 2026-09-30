# Security review — Web front-end and cloud infrastructure

Target: aijalon.trade (Hyperliquid strategy marketplace; real funds) · Review date: 2026-09-30 · Reviewer: automated senior-review pass (read-only)
Scope: `web/**` (index.html, build.mjs, src/core/**, src/pages/**, public/**), `firebase.json`, `infra/**` (gcp bootstrap/env/deploy/render/run templates/sql/firebase_auth/monitoring, cloudflare dns.sh, csp.txt, csp_sync), `.github/workflows/**`, `.github/gitleaks.toml`, `backend/Dockerfile`, `sandbox/Dockerfile`, `Makefile`. Read against `docs/SPEC.md`, `docs/SECURITY.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`.

Method: manual code review plus dynamic tests. I built a copy of the SPA (`node web/build.mjs` in `/tmp/qsm`) and drove it with Playwright/Chromium against mocked API responses. Every string field carried XSS payloads (`<img onerror>`, `<svg onload>`, `<script>`, markdown `javascript:` / tab-split `java\tscript:` / `data:` links, and an external phishing link). The Firebase Hosting header rules from `firebase.json` were emulated to test framing. Nothing was deployed and no cloud state was touched.

Line numbers refer to the working tree at commit `b3bee9a` (another agent was editing files at the same time, so re-check the exact lines).

Severity scale: **Critical** means direct loss of funds with no preconditions. **High** means loss of funds or keys after the compromise of one plausible component. **Medium** means a security control is bypassed or the blast radius is materially larger than documented. **Low** is hardening or defence in depth. **Info** is an observation. Findings marked **[UNCERTAIN]** depend on platform behaviour I could not verify from this environment.

---

## Summary of the most important results

1. **The wallet-signing safety net cannot catch a compromised API.** The client "validates" the typed data from the server against addresses that come from the same server: the agent address, treasury, builder, fee cap and payout destination. An attacker who compromises the api service, or who can rewrite API responses at the edge, gets users to approve an attacker agent and to send deposits to an attacker, and gets admins to sign payouts to an attacker. (H1)
2. **The deployer / CI identity holds the keys in practice.** In effect it has executor-level agent-key decryption and api-level secret access, which contradicts SPEC §2.1. The deploy job also runs third-party npm tooling after it has obtained those credentials. (H2)
3. **Clickjacking bypass.** The Hosting header regex leaves `X-Frame-Options` and `frame-ancestors` off every path that starts with `__` (for example `/__x`). A rewrite then serves the full SPA there, and it can be framed. I confirmed this in emulation. (M1)
4. **No XSS found.** The `h()` DOM builder, the markdown renderer and the URL sanitiser held against every payload I tried. No `javascript:`/`data:` links and no `on*` attributes reached the DOM, `window.__xss` stayed unset, and there were no dialogs. `next=` has no open redirect because the router is hash-only. There is no service worker.

---

## HIGH

### H1. Signing trust anchors come from the API they are meant to guard against (agent, treasury, builder, payouts)

- **Files:** `web/src/core/hl.ts:189-229` (the `validateServerTypedData` docstring says *"a compromised or buggy server must not be able to get a different action signed"*), `hl.ts:310-317, 320-329, 336-346`; `web/src/core/api.ts:242-269` (`builder_address`, `treasury_address`, `agent_name`, `hl_chain` and `economics.builder_fee_tenths_bp` all come from `GET /v1/public/config`); `web/src/pages/subscribe.ts:563-577` (agent address and typed data come from `POST /v1/agents`); `web/src/pages/dashboard.ts:442-453` (deposit destination compared with `cfg.treasury_address`, which is also from the API); `web/src/pages/admin.ts:298-313` (`destination: p.to_address, expectDestination: p.to_address`, both from `/admin/payouts`); `web/src/core/hl.ts:102-107` (the fee-rate encoder accepts up to 1 %).
- **What happens:** every "expected" value that validation checks against comes from `api.aijalon.trade`. That host is also the one Cloudflare proxies, so anyone who can change its responses controls both the typed data and the values it is checked against. The UI then shows the values the attacker chose:
  - the agent address, in full;
  - the builder address;
  - the deposit destination, as `0x1234…abcd` (`shortAddr`, first and last 4 hex characters only, which invites address poisoning with vanity addresses);
  - the payout destination.
- **Exploit scenarios:**
  - **Theft of new deposits.** An RCE or a malicious dependency in the api service, a Cloudflare Worker or response rewrite on `api.aijalon.trade/*`, or a malicious api revision returns `treasury_address` = attacker from `/public/config` and the same destination from `/deposits/usdc/typed-data`. Every USDC top-up from then on is a valid `UsdSend` to the attacker. The client's "mismatch" check passes because both values come from the same source, so this path is plain theft.
  - **Attacker agent.** `POST /v1/agents` returns an attacker `agent_address` together with matching typed data. The user signs `ApproveAgent(name="aijalon")`, which also replaces our legitimate named agent. The attacker can now trade the account (SECURITY A1: extracting value through a thin market). The backend `agent-expiry-scan` would only see "our agent missing" after 2 consecutive scans, 6–12 h later (`backend/app/jobs_data/agents.py:11, 75`).
  - **Attacker builder.** `builder_address` = attacker, and `builder_fee_tenths_bp` up to 1000 is encoded without a client cap. Hyperliquid limits perps to 0.1 %.
  - **Admin payout redirect.** An admin clicks "Sign & send", and `/admin/payouts` together with `/typed-data` return `to_address` = attacker. The hardware wallet shows the attacker address, but the admin has nothing independent to compare it against, so maker-checker (enforced server-side) is bypassed.
- **Fix, in order of value:**
  1. **Pin the static trust anchors outside the API.** Add `builderAddress`, `treasuryAddress`, `agentName`, `hlChain` and `maxBuilderFeeTenthsBp` to `web/public/app-config.json`. That file is served by Firebase Hosting on the apex, which is DNS-only and not proxied through Cloudflare, and a change to it needs a reviewed commit. In `liveConfig()` (`hl.ts:303`), refuse to sign when `/public/config` disagrees with it. Hard-code `maxFeeRateFromTenthsBp` to ≤ 100 for perps.
  2. **Add a per-user agent attestation.** Create a KMS asymmetric signing key (`agent-attest`, EC_SIGN_P256_SHA256) that only the executor SA, or a dedicated attestor SA, may use. Have that identity sign `(user_id, master, agent_address, agent_name, created_at)` when the agent row is created or the executor first sees it. Return the signature with `POST /agents`, and verify it in the browser with the public key pinned in `app-config.json`. The api SA must not have `signerVerifier` on that key. After this change, compromising only the api service or the edge can no longer substitute the agent.
  3. **Bind payouts to the beneficiary's own signature.** The beneficiary signs an EIP-4361 message such as "my payout address is X" when they set the address (with the 48 h hold). The admin UI recovers the signer client-side and requires it to equal the beneficiary's verified wallet before `usdSend`. Show the full checksummed address and ask the admin to compare it with the hardware-wallet screen.
  4. **Add detection on the executor side.** In `agent-expiry-scan`, and on any `agent not approved` rejection in the tick, raise `agent_substituted` (critical, pause that user's subscriptions) as soon as `extraAgents` lists an entry named `aijalon` whose address is not ours. Run the scan hourly rather than every 6 h.
  5. **Harden DNS and certificates.** Show full addresses everywhere a signature is requested. Add CT-log monitoring for `aijalon.trade`, and consider CAA `accounturi`. A Cloudflare account holding DNS Edit can re-point the apex and pass DNS-01, so pinning does not protect against that case. Lock the registrar and scope and expire Cloudflare tokens as DEPLOY §1 already advises.

### H2. Deployer / deploy job: effective decrypt and secret access, and third-party code runs with those credentials

- **Files:** `infra/gcp/bootstrap.sh:241-248` (`roles/run.developer` project-wide, `iam.serviceAccountUser` on the api, executor, sandbox and migrator SAs, `firebasehosting.admin`); `.github/workflows/deploy.yml:91-99` (`create_credentials_file: true`, `export_environment_variables: true`), `:132` (`npm install --global typescript@…` after auth), `:155` (`npx --yes firebase-tools@15.32.0` after auth); `docs/SPEC.md:67` / `docs/ARCHITECTURE.md:360` ("cannot read secret values; KMS").
- **What happens:** `run.developer` together with actAs on `aijalon-executor` lets the deployer deploy any image as the executor, or create a new service or job as the executor. That image can then call KMS Decrypt on every agent key. In the same way, a revision running as the api SA can print `STRIPE_SECRET_KEY` and every other api secret. `gcloud run jobs execute migrate --args=…` (included in run.developer) gives a session as the `migrator` user, a member of cloudsqlsuperuser who owns the schema, so it can disable the append-only triggers. ARCHITECTURE §8.1 acknowledges some of this ("The deploy pipeline can run arbitrary code as the executor"), but SPEC §2.1 still says the deployer "cannot read secrets values, decrypt". The job that holds these credentials then installs and runs npm packages (typescript, firebase-tools and its transitive dependency tree, including any lifecycle scripts), with `GOOGLE_APPLICATION_CREDENTIALS` exported and a credentials file in `$GITHUB_WORKSPACE`.
- **Exploit scenario:** a compromised npm package, as in the 2025 debug/chalk and "Shai-Hulud" npm incidents, runs in `npm install -g` or `npx firebase-tools` during a normal deploy. It reads the WIF credentials and runs `gcloud run deploy executor --image=<attacker>` or `gcloud run jobs create … --service-account=aijalon-executor`. The attacker image decrypts every agent key and trades user accounts. The alert "agent-keys DECRYPT by anyone but executor" never fires, because the principal *is* the executor. **[UNCERTAIN]** Whether firebase-tools@15.32.0 ships an `npm-shrinkwrap.json` changes how much of its tree is floating, but the tarball's own install scripts still run with the credentials either way.
- **Fix:**
  1. Split `deploy.yml` into jobs:
     - `build-web` runs with no `id-token`: it installs typescript and builds, then uploads `web/dist` as an artifact.
     - `deploy-run` runs with WIF as the deployer and executes only `gcloud` and `docker`, with no npm after auth.
     - `deploy-hosting` runs with WIF as a new `aijalon-hosting-deployer` SA that has only `firebasehosting.admin` and no actAs. Install firebase-tools before the auth step, from a committed `package-lock.json` with `npm ci --ignore-scripts`, or deploy through the Hosting REST API with `curl`.
  2. Put an IAM condition on the deployer's `run.developer` limiting `resource.name` to the services `api`, `executor`, `sandbox` and the job `migrate`, so it cannot create new services or jobs. Remove `run.jobs.runWithOverrides` through a custom role.
  3. Turn on **Binary Authorization** for Cloud Run. Require an attestation signed with a KMS key that only a separate build identity can use. At minimum, alert on `google.cloud.run.v1.Services.ReplaceService`, `UpdateService`, `CreateService`, `CreateJob` and `RunJob` by any principal, with a CRITICAL page when the service is `executor`.
  4. Correct SPEC §2.1 and SECURITY §3.6 "deployer … cannot read secrets, decrypt" to say the deployer is equivalent to executor plus api, and add a second human reviewer with "Prevent self-review" before public launch.

---

## MEDIUM

### M1. Clickjacking and header bypass on any path starting with `/__` other than `/__/` (verified in emulation)

- **File:** `firebase.json:47`, regex `^/([^_].*|_([^_].*)?)?$`, which carries CSP, `X-Frame-Options: DENY` and COOP.
- **What happens:** the regex is meant to exclude Firebase's reserved `/__/*`. It also excludes `/__`, `/__x`, `/__anything` and so on. The `**` → `/index.html` rewrite serves the SPA on those paths, and every asset path in the SPA is relative, so it loads normally. The meta CSP cannot carry `frame-ancestors`. As a result `https://aijalon.trade/__x#/<route>` can be framed. Test (`/tmp/qsm/pw/frame.mjs`): an iframe of `/#/market` from `evil.example` was blocked, while an iframe of `/__x#/market` rendered the full app.
- **Impact:** SECURITY §3.1 names `frame-ancestors 'none'` as the control for "Clickjacking the approve flow". Third-party storage partitioning (Chrome, Firefox, Safari) means the framed app normally starts signed out, which limits the impact. What remains is UI redress of the entry gate and consent ticks, and wallet prompts where the extension injects into cross-origin frames. **[UNCERTAIN]** This assumes Firebase applies the `**` rewrite to `/__x`; only `/__/` is documented as reserved. Verify with `curl -sI https://aijalon.trade/__x`.
- **Fix:** change the regex to `^/(|_|__|[^_].*|_[^_].*|__[^/].*)$`, which I tested: it matches `/`, `/__x` and `/_x` and excludes `/__/auth/handler` and `/__/firebase/init.json`. Add a frame-buster at the top of `main.ts` boot: `if (window.top !== window.self) { document.documentElement.textContent = ""; return; }`. Extend `deploy.sh smoke-web` to check the CSP and XFO headers on `/__x` and `/s/x` as well as `/`.

### M2. Script supply chain: Firebase SDK without SRI; CSP allows any Firebase version and all of apis.google.com; Stripe.js shares the signing origin

- **Files:** `web/build.mjs:107-124` (`web/sri.json` is absent, so the build only prints a WARN and `deploy.yml` does not enforce SRI; SECURITY §8 says "Firebase JS pinned plus SRI"); `web/build.mjs:150` / `infra/csp.txt` (`script-src … https://www.gstatic.com/firebasejs/ https://apis.google.com https://js.stripe.com https://*.js.stripe.com`); `web/src/core/auth.ts:122-125` (dynamic `import()` from gstatic); `web/src/core/stripe.ts:22-30`.
- **What happens:** a tampered gstatic response, or a CDN or edge compromise, runs with full access to the page's origin. That means the Firebase refresh token in IndexedDB and the ability to drive `eth_signTypedData_v4` prompts. The path-scoped `/firebasejs/` source allows every historical SDK version, not just 12.3.0. The whole `apis.google.com` host is allowed, although Firebase only needs `/js/api.js` (gapi). **[UNCERTAIN]** I have not confirmed a script gadget on that host. Stripe.js is loaded into the same origin, without SRI because Stripe does not support it, and stays loaded for the rest of the SPA session, including later visits to `#/subscribe` and the admin payout signing.
- **Fix:**
  1. Commit `web/sri.json` for 12.3.0. Make `build.mjs` `fail()` when `APP_CONFIG` is set (a production build) and SRI is missing, and add the same check to `deploy.sh preflight`.
  2. Path-scope the CSP to `https://www.gstatic.com/firebasejs/12.3.0/` and `https://apis.google.com/js/`.
  3. Add a report-only `require-trusted-types-for 'script'` together with a `report-to` / `report-uri` endpoint, and enforce Trusted Types once Firebase and Stripe are clean under it.
  4. Serve payments from a separate origin (for example an iframe on `pay.aijalon.trade`), or force a full reload after a Stripe session, so Stripe.js never shares a document with wallet signing or admin pages.
  5. Self-host the two fonts, which removes `fonts.googleapis.com` and `fonts.gstatic.com`.

### M3. Edge origin authentication: a static shared header, a Cloudflare-IP allowlist anyone can meet, and the secret leaks through argv and logs

- **Files:** `infra/gcp/bootstrap.sh:395-398` (the secret is passed to `gcloud … --expression="request.headers['x-edge-auth'] != '<secret>'"`); `infra/gcp/bootstrap.sh:399-405` (allow Cloudflare IPv4 ranges); `infra/cloudflare/dns.sh:198-203`; `backend/Dockerfile:68-71` together with the app, which trusts `CF-Connecting-IP` and `CF-IPCountry` once the header matches.
- **What happens:** any Cloudflare customer can send requests from Cloudflare IPs, for example through a Worker or a zone pointed at our LB IP, so the IP allowlist adds little. The `X-Edge-Auth` value is the real barrier, and it leaks in several ways:
  - it sits in argv, visible in `ps`;
  - gcloud's local command log (`~/.config/gcloud/logs/*`) records it;
  - Admin Activity audit logs capture the `patchRule` request body, which anyone with `logging.viewer` can read;
  - the Armor policy itself stores it, readable with `compute.securityPolicies.get`.

  In contrast, `sql_user_password` in `env.sh:197-231` was written specifically to keep a secret out of argv and gcloud logs.
- **Exploit scenario:** with the secret, an attacker sends requests from their own Cloudflare Worker with `X-Edge-Auth` and forged `CF-Connecting-IP` / `CF-IPCountry`. That bypasses the WAF, geo-blocking (restricted jurisdictions), the Cloudflare rate limits and the app's per-IP limits, and it corrupts the `ip_hash` evidence in audit and consent records.
- **Fix:** replace the header secret with **Cloudflare Authenticated Origin Pulls using a zone-level custom client certificate**, not Cloudflare's shared AOP CA, and have the LB require it (`ServerTlsPolicy` with mTLS and a `TrustConfig` holding only that CA). Until then, patch the Armor rule through the Compute REST API with the body read from stdin, rotate the secret once after setup, keep `logging.viewer` and `compute.viewer` to the owner only, and let the app accept an `old,new` list so rotation causes no downtime.

### M4. Executor rollout: 100 % cutover on real money; the smoke test never exercises the executor

- **Files:** `infra/gcp/run/executor.service.yaml:132` (`latestRevision: true, percent: 100`); `infra/gcp/deploy.sh:147-181` (services, then a smoke test that checks only api health, the edge and origin reachability); `.github/workflows/deploy.yml:112-119` (rollback runs only on failure of `services` or `smoke_api`, and `SMOKE_SKIP_PUBLIC=true` disables both the smoke test and the automatic rollback); `deploy.sh:210-219` (sandbox is never rolled back; Hosting is manual).
- **Exploit or failure scenario:** a faulty executor revision gets 100 % of traffic, and the scheduler calls `/internal/tick` every minute on real funds before any check runs. Sizing, guard or order-signing bugs execute immediately, and the smoke test would pass anyway.
- **Fix:**
  1. Deploy the executor with `--no-traffic --tag=candidate`.
  2. Before shifting traffic, call a side-effect-free `/v1/internal/selftest` on the tagged URL: KMS decrypt of a canary key, DB read, and a `dryRun` of guards and order building for the Hyperliquid info API.
  3. Pause `tick` for the rollout. Either give the deployer a custom role with only `cloudscheduler.jobs.pause`/`resume` on that job, or set `new_entries_paused` automatically.
  4. Roll the executor back on any failure after `services`.
  5. After go-live, fail the workflow if `SMOKE_SKIP_PUBLIC` is still set.

### M5. Sandbox isolation gaps, which matter once the in-process AST jail is escaped

- **Files:** `sandbox/Dockerfile:51` (the service runs as uid 10001); `backend/app/sandbox/runner.py:336-362` (children drop to `nobody` only when the parent is root, so in production they run as the **same uid** as the parent); `backend/app/sandbox/service.py:209` (`SANDBOX_SHARED_SECRET` stays in `os.environ`, and the parent does not set itself non-dumpable); `infra/gcp/run/sandbox.service.yaml:33` (`containerConcurrency: 4`); `infra/gcp/bootstrap.sh:162-177` (the sandbox VPC has no Cloud DNS policy).
- **What happens:**
  - **(a) DNS exfiltration** **[UNCERTAIN]**. Cloud Run resolves names through the metadata server, 169.254.169.254, which uses the VPC's Cloud DNS. The "no egress" design (no NAT, no Private Google Access, no route, deny-all firewall) does not stop recursive DNS to public names, so `<data>.attacker.tld` lookups can leak data.
  - **(b) Reading the parent.** An escaped child with the same uid can read the parent's `/proc/<ppid>/environ`, which holds the shared secret, and possibly `/proc/<ppid>/mem`. With four concurrent requests per instance, that memory can hold other creators' code, which is asset A7.
  - **(c) Error strings as a return channel.** Error messages of up to 300 characters are returned to the caller and can reach the creator.
- **Fix:**
  1. Attach a Cloud DNS response policy to `aijalon-sandbox-vpc`: a wildcard `*.` rule returning NXDOMAIN, or a private zone for `.`. Turn on DNS query logging for that VPC and alert on any query.
  2. Set `containerConcurrency: 1`, or start the service as root and give each slot its own uid (the runner already supports dropping privileges).
  3. In `service.py`, call `prctl(PR_SET_DUMPABLE, 0)` on the parent and `os.environ.pop("SANDBOX_SHARED_SECRET")` after reading it.
  4. Consider dropping the shared secret altogether, since Cloud Run IAM already authenticates the api and executor callers. That also brings the sandbox SA to "nothing", as SPEC §2.1 states.

---

## LOW

| # | Finding | File:line | Exploit / impact | Fix |
|---|---|---|---|---|
| L1 | Creator markdown allows any external `http(s)` link, and the link text can differ from the target. Confirmed: `[ext](https://evil.example/aijalon.trade/reconnect-wallet)` renders as a live link on `#/s/:slug` (description, showcase, teaser), `#/posts` and `#/posts/:id` | `web/src/pages/_shared/markdown.ts:7-11, 71-93`; `util.ts:227-231` | Third-party creators (uploads are ON at launch) can phish users with "re-approve your agent" links inside trusted pages | Show the destination host after each link (`label ↗ evil.example`), add `rel="ugc nofollow noopener noreferrer"`, show a "leaving aijalon.trade" interstitial for off-site links, reject links whose text looks like a URL different from the href, and restrict creator content to an allowlist of hosts |
| L2 | The KYC redirect accepts any `https://` URL returned by the API | `web/src/pages/creator.ts:96-98` | Open redirect or phishing through a compromised API or edge | Allow only `https://in.sumsub.com/` (and the configured KYC base) and compare `new URL(url).host` exactly |
| L3 | The builder-fee encoder allows up to 1 % | `web/src/core/hl.ts:102-107`, `:323-327` | A compromised config causes an over-approval (perps are capped at 0.1 % by Hyperliquid) | Hard cap at 100 tenths-bp (see H1) |
| L4 | Wallet-to-account linkage stays in localStorage after sign-out for 7 days: `aijalon.subwiz.<uid>.<slug>` (master and trading addresses, agent id and address, typed data), `aij.ref.bound` (uid), and the `synced` uid map in `aij.consents.v1`. The Firebase refresh token is in IndexedDB (expected) | `web/src/pages/subscribe.ts:59, 108-112`; `main.ts:40-45`; `gate.ts:36-45`; `auth.ts:128` | Privacy (SECURITY A6) on shared devices; XSS would reach long-lived refresh tokens (step-up with `auth_time` ≤ 300 s limits the damage) | Clear the `aijalon.subwiz.*` and uid-bearing keys in `signOut()`; consider `browserSessionPersistence` for admin sessions |
| L5 | Firebase: `<project>.firebaseapp.com` stays an authorised domain although `authDomain` is `aijalon.trade`; the default Hosting domains (`*.web.app`, `*.firebaseapp.com`) serve the same SPA; the Web API key has no referrer or API restrictions | `infra/gcp/firebase_auth.sh:46-48`; bootstrap `firebase` step | Look-alike origins for phishing; key abuse against other enabled APIs **[UNCERTAIN whether firebaseapp.com can be removed without breaking redirect flows]** | Restrict the API key (`gcloud services api-keys update --allowed-referrers=https://aijalon.trade/* --api-target=service=identitytoolkit.googleapis.com --api-target=service=securetoken.googleapis.com`); drop firebaseapp.com from the authorised domains if redirect sign-in still works; have the backend reject SIWE messages whose domain ≠ `aijalon.trade` (the client uses `location.host`, `wallet.ts:234`) |
| L6 | The migrate job uses password auth with `sslmode=require` (no server identity check) as a cloudsqlsuperuser member | `infra/gcp/run/migrate.job.yaml:37` | MITM inside the VPC (low); a standing password that humans can read | Use the Cloud SQL Auth Proxy sidecar with an IAM login for the migrator (then delete `DB_MIGRATOR_PASSWORD`), or at least `sslmode=verify-ca` with the instance server CA |
| L7 | The privilege assertions miss documented rules | `infra/gcp/sql/20_verify.sql:78-118` | Regressions go unnoticed | Add: api has no SELECT on `strategy_versions.code_ciphertext` (SECURITY §6); runtime logins are not members of `cloudsqlsuperuser`, `app_migrator`, `pg_read_all_data` or `pg_write_all_data`; neither login can UPDATE `system_flags` without maker-checker columns (if that is intended) |
| L8 | Monitoring gaps | `infra/gcp/monitoring.py:148-215` | Malicious redeploys or scheduler retargeting go unnoticed; alert fatigue | Alert on Cloud Run `ReplaceService`/`UpdateService`/`CreateService`/`CreateJob`/`RunJob` (CRITICAL for executor) and on Cloud Scheduler `UpdateJob`/`CreateJob` (URI, audience or OIDC SA change). Add the sandbox SA and `SANDBOX_SHARED_SECRET` to the exclusions of "Secret accessed by unexpected principal", which otherwise fires on every sandbox cold start. Add a decrypt-rate anomaly alert (SECURITY §7) |
| L9 | The build's sink scanner is regex-based: it misses `innerHTML +=`, `srcdoc`, `setHTMLUnsafe`, `DOMParser`, `createContextualFragment`; `svg()` has no tag allowlist (`<animate>`/`<set>` could write `href`) | `web/build.mjs:192-199`; `web/src/core/ui.ts:81-87` | No current exploit (all `svg()` calls use static tags) | Give `svg()` a tag allowlist (`svg, g, path, line, circle, rect, text, title, clipPath`); use Trusted Types (M2) instead of regex |
| L10 | Supply-chain controls claimed in SECURITY §3.11/§8 are not present: no CODEOWNERS, no Dependabot/Renovate, no CodeQL, no image-scan gate, no SBOM, no `security.txt` | `.github/` (only `workflows/`, `gitleaks.toml`), `infra/hosting/well-known/` | Drift between the documents and reality | Add them, or mark them [GAP] in SECURITY.md |
| L11 | The gitleaks allowlist ignores `generic-api-key` in every `backend/tests/test_*.py` | `.github/gitleaks.toml:16-19` | A real key pasted into a test file is not caught | Limit it to specific files or regexes |
| L12 | WIF condition hardening | `infra/gcp/bootstrap.sh:463-465` | Defence in depth | Add `assertion.runner_environment == 'github-hosted'` and `assertion.event_name in ['push','workflow_dispatch']` |
| L13 | DNSSEC/CAA: `dns.sh` turns DNSSEC on in Cloudflare, but DS records at the registrar are not checked; no CT monitoring | `infra/cloudflare/dns.sh:187-188, 155-156` | DNSSEC may stay "pending"; mis-issuance goes unnoticed | Verify DS at the registrar (`dig +dnssec DS aijalon.trade`); add a CT monitor (for example crt.sh or Cert Spotter) |
| L14 | EIP-6963: a later announcer with the same `uuid` replaces an earlier provider; `rdns` is not shown; the legacy `window.ethereum` fallback | `web/src/core/wallet.ts:52-70` | A malicious extension can shadow a wallet (though such an extension already has page access) | Keep the first announcer per uuid, show `rdns` in the picker, and warn when two providers claim the same name |

---

## INFO / verified OK

- **XSS:** no sink found. All rendering goes through `h()`/`svg()`: text nodes, a URL scheme allowlist on `href`/`src`/`action`/`formaction`/`xlink:href`/`srcset`, no string `on*` handlers, and `script/iframe/object/embed/style/base/meta/link` are rejected. The markdown renderer drops `javascript:` and tab-split `java\tscript:` links (verified with Playwright on `#/`, `#/market`, `#/s/:slug`, `#/posts`, `#/posts/:id`, `#/legal/*`). Legal pages are slug-allowlisted and refuse HTML responses.
- **Open redirects:** `next=` stays inside the hash router (`router.ts:112-123`, `auth.ts:474-477`). The Stripe `return_url` is built from `siteOrigin`/`location.origin` and never from data (`dashboard.ts:304-308`). The Telegram link is strictly validated (`contacts.ts:33`).
- **Idempotency:** `api.ts` reuses the key only on the automatic retry of the same payload. The subscribe wizard keeps `subKey` across allocation or leverage edits (`subscribe.ts:727-728, 822-838`), but the backend fingerprints the payload (`deps.py:644-648`), so reusing a key with a different body is refused and the client then drops the key. This is a UX issue only.
- **Hyperliquid signing mechanics:** fresh nonce, the wallet's current `chainId`, account-switch check, strict signature-format checks, and typed data rebuilt locally (never signed as received). Only the trust anchors are the problem (H1).
- **Service worker:** none, so there is no persistence vector.
- **Token handling:** the Bearer token is never sent in URLs; API fetches use `credentials: "omit"` and `referrerPolicy`; MFA is enforced on the server.
- **Headers:** HSTS preload on the apex (Hosting) and the API (Cloudflare); nosniff; strict-origin referrer; COOP `same-origin-allow-popups` (needed for the Firebase popup); `object-src 'none'`; `base-uri 'none'`; `form-action 'self'`. Minor: `headers.json` includes `payment=(self "https://js.stripe.com")` but `firebase.json` does not; keep them identical.
- **Cloud Run ingress:** api is `internal-and-cloud-load-balancing` with allUsers invoker (Armor and the app enforce the rest; the smoke test asserts that run.app is unreachable); executor is `internal` with the scheduler SA as the only invoker; sandbox is `internal` with the api and executor as invokers. The scheduler OIDC audience is the executor URL, and the app re-checks the audience and email.
- **Cloud SQL:** private IP (`--no-assign-ip`), `ENCRYPTED_ONLY`, IAM auth flag, REGIONAL HA, PITR 7 d, 30 backups, deletion protection, CMEK (HSM), pgaudit ddl/role. The temporary public IP in `db_bootstrap.sh` has no authorised networks, is removed by a trap, and is re-verified by bootstrap `sql` and alerted on (`instances.patch`).
- **KMS:** the api SA has `cryptoKeyEncrypter` only and the executor SA `cryptoKeyDecrypter` only on `agent-keys`, with HSM keys, 90-day rotation and a 90-day destroy delay. Data Access logs are on. This split holds against direct use; H2 describes how it is bypassed through deploys.
- **Secrets in scripts:** there is no `set -x` anywhere. Cloudflare and Identity Toolkit tokens go through `-H @<(…)` or stdin, and SQL passwords go over stdin to REST. The one exception is `EDGE_AUTH_SECRET` in bootstrap `lb` (M3).
- **GitHub Actions:** every action is pinned to a full SHA; `permissions: contents: read` at the top, with `id-token: write` only on the deploy job; `persist-credentials: false`; no `pull_request_target`; no repository secrets; `package-manager-cache: false`. The WIF condition checks repo, repo id, `refs/heads/main`, environment `production` and `workflow_ref` of deploy.yml. The deploy concurrency group does not cancel in progress.
- **Docker:** both images are non-root (uid 10001) with root-owned read-only code; the backend installs with `--require-hashes --only-binary=:all:`; the `.dockerignore` allowlist; digest pins are enforced by the deploy preflight (`PIN_ME` makes it fail) and by `render.py` (every image must be `@sha256:`). Info: `backend/Dockerfile:70` `--forwarded-allow-ips "*"` makes `request.client.host` spoofable. It is used only as a fallback when the edge is untrusted (`deps.py:369-376`), but pinning the trusted proxy (or dropping `--proxy-headers`) removes the footgun.
- **Sandbox network:** Direct VPC egress `all-traffic` into a VPC with no NAT, no Private Google Access, the default route deleted, and deny-all egress and ingress with logging. Apart from DNS (M5a), this matches SPEC. Info: with `SANDBOX_EGRESS_MODE=connector`, the deny-all ingress rule at priority 100 will likely block the connector's health-check and serverless ranges, so it fails closed.
- **Cloudflare:** Full (strict), min TLS 1.2, 0-RTT off, the Transform Rule overwrites any client `X-Edge-Auth`, `/v1/internal/*` is blocked at the edge, a path allowlist, the Telegram webhook is IP-restricted, and the cache is bypassed. The Free plan has only the Free Managed Ruleset (Pro is required for Gate C, as documented).

---

## Summary table

| ID | Severity | Area | Title | Primary location | Status |
|---|---|---|---|---|---|
| H1 | **High** | Web / signing | Typed-data validation is circular: agent, treasury, builder and payout anchors come from the API or edge they are meant to guard | `web/src/core/hl.ts:189-346`, `api.ts:242-269`, `subscribe.ts:563-577`, `dashboard.ts:442-453`, `admin.ts:298-313` | Confirmed by code |
| H2 | **High** | IAM / CI | Deployer can in effect decrypt agent keys and read secrets; deploy job runs npm tooling after WIF auth | `infra/gcp/bootstrap.sh:241-248`, `.github/workflows/deploy.yml:91-99,132,155` | Confirmed (npm tree size [UNCERTAIN]) |
| M1 | **Medium** | Web / headers | `/__x`-style paths lose XFO/CSP, so the SPA can be framed | `firebase.json:47` | Verified in emulation; confirm on live Hosting |
| M2 | **Medium** | Web / CSP | Firebase SDK without SRI (not enforced); broad gstatic and apis.google.com sources; Stripe.js in the signing origin; no Trusted Types or CSP reporting | `web/build.mjs:107-124,150`, `infra/csp.txt`, `web/src/core/stripe.ts:22-30` | Confirmed |
| M3 | **Medium** | Edge | Static edge header over a Cloudflare-IP allowlist anyone can meet; secret in argv, gcloud logs and audit logs | `infra/gcp/bootstrap.sh:395-405`, `infra/cloudflare/dns.sh:198-203` | Confirmed |
| M4 | **Medium** | Deploy | Executor cut over to 100 % with ticks live; smoke never tests the executor; rollback gaps | `infra/gcp/run/executor.service.yaml:132`, `infra/gcp/deploy.sh:147-219`, `deploy.yml:112-119` | Confirmed |
| M5 | **Medium** | Sandbox | DNS exfiltration path; children share the parent's uid; secret in parent environ; concurrency 4 mixes creators | `sandbox/Dockerfile:51`, `runner.py:336-362`, `sandbox.service.yaml:33`, `bootstrap.sh:162-177` | Needs an AST-jail escape; (a) [UNCERTAIN] |
| L1 | Low | Web / content | Creator markdown external links (phishing) | `markdown.ts:7-93` | Verified (Playwright) |
| L2 | Low | Web | KYC redirect accepts any https URL | `creator.ts:96-98` | Confirmed |
| L3 | Low | Web / signing | Builder fee encoder allows up to 1 % | `hl.ts:102-107` | Confirmed |
| L4 | Low | Web / storage | Wallet and uid linkage persists in localStorage after sign-out | `subscribe.ts:59,108-112`, `main.ts:40-45` | Confirmed |
| L5 | Low | Firebase | Extra authorised domain, default Hosting domains, unrestricted Web API key | `firebase_auth.sh:46-48` | [UNCERTAIN] impact |
| L6 | Low | Cloud SQL | Migrator password auth with `sslmode=require` | `migrate.job.yaml:37` | Confirmed |
| L7 | Low | Cloud SQL | Privilege-verify SQL misses documented rules | `sql/20_verify.sql` | Confirmed |
| L8 | Low | Monitoring | No alerts on Cloud Run or Scheduler changes; noisy secret-access alert | `monitoring.py:148-215` | Confirmed |
| L9 | Low | Web / build | Regex sink scanner gaps; `svg()` has no tag allowlist | `build.mjs:192-199`, `ui.ts:81-87` | No exploit today |
| L10 | Low | SDLC | CODEOWNERS, Dependabot, CodeQL, SBOM, security.txt missing despite docs | `.github/` | Confirmed |
| L11 | Low | Secrets scan | gitleaks allowlist too broad for backend tests | `gitleaks.toml:16-19` | Confirmed |
| L12 | Low | WIF | Add runner_environment and event_name conditions | `bootstrap.sh:463-465` | Hardening |
| L13 | Low | DNS / TLS | DNSSEC DS unverified; no CT monitoring | `dns.sh:155-188` | Hardening |
| L14 | Low | Web / wallet | EIP-6963 uuid shadowing; rdns not shown | `wallet.ts:52-70` | Hardening |
| I-* | Info | — | XSS, open redirect, idempotency, SW, ingress, SQL, KMS, Actions pinning, Docker verified OK (see INFO) | — | — |

**Before real money:** fix H1 (at least the pinned static anchors, full-address display and the agent-substitution alert), H2 (split the deploy job and use a separate Hosting SA, and correct the SPEC claim), M1 (a one-line regex change) and M4 (tick paused or dry-run before cutover). Fix M2 (commit `sri.json`) and M3 (AOP mTLS) before Gate C. Fix M5 before opening creator uploads beyond trusted creators.

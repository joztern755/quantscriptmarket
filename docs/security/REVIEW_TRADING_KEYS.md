# Security review — trading keys, execution, sandbox, signals (aijalon.trade)

Reviewer: senior security review (trading systems / key custody)
Date: 2026-09-30
Scope: `backend/app/security/{kms,agent_keys,keccak}.py`, `backend/app/execution/**`,
`backend/app/domain/{risk,jitter}.py`, `backend/app/hl/**`, `backend/app/strategies/**`,
`signals/**`, `backend/app/sandbox/** + sandbox/Dockerfile`,
`backend/app/api/routers/{agents,creator,subscriptions,withdrawals,admin}.py`, `infra/gcp/*`,
`web/src/core/hl.ts`. Read-only except this file.

Method: code reading + a few local `python3.12` sandbox probes (exploit scripts under `/tmp` only).
The Cloud Run / IAM boundaries described in SPEC/SECURITY could not be exercised here; findings on them
are from the deploy scripts and are marked accordingly.

Overall: the key-custody core is strong. Envelope encryption binds each agent key to
`(user_id, agent_address)` as AEAD AAD, the API process builds an **encrypt-only** envelope with no
decrypt method, `make_decryptor` refuses any role but `executor` in prod, the DB `app_api` role has no
SELECT on the ciphertext columns, and IAM grants api=encrypt-only / executor=decrypt-only on the
`agent-keys` KMS key. Cross-user key swap is prevented by the AAD **and** an address re-derivation check.
Signal ingestion verifies Ed25519 before parsing, pins the key, enforces canonical re-encoding, staleness,
replay/conflict continuity and an optional script-hash pin. Order placement always carries the builder code,
cloids are deterministic per `(subscription, bar, coin, attempt)`, and the pre-trade guards fail closed.
Maker-checker payouts verify the on-chain `usdSend` before settling. `web/src/core/hl.ts` rebuilds every
user-signed action locally and validates server-offered typed data against config.

The findings below are the deviations worth acting on before real-money scale.

---

## Findings

### F1 — [HIGH] Creator strategies may trade arbitrary, attacker-controlled HIP-3 markets (no trusted-dex allowlist)
File: `backend/app/api/routers/creator.py:113` (`create_strategy` → `svc.hl.unknown_coins`),
`backend/app/api/adapters.py:337` (`unknown_coins` → `MarketCatalog.from_info(dexes=dexes_for(coins))`),
`backend/app/hl/markets.py:283` (`dexes_for`), guard implementation `backend/app/domain/risk.py:151,217-241`.

A creator's `MARKETS` are validated only against the **live Hyperliquid meta of whatever dex prefix the
coin names carry** (`unknown_coins` builds the catalog from `dexes_for(coins)` — i.e. it fetches the very
dex the attacker names). There is no allowlist restricting creator strategies to the validator dex plus
platform-operated builder dexes. A creator can therefore list a strategy whose market is `evildex:COIN`,
a HIP-3 perp on a dex deployed and controlled by a colluding party.

The SPEC §5.4 "thin HIP-3" guards — order ≤ 0.5% of 24h notional volume, ≤ 2% of open interest,
mark/oracle and mid/oracle deviation ≤ 2% — all read values the **dex deployer controls**: `oraclePx`,
`markPx`, `dayNtlVlm`, `openInterest` (`app/hl/markets.py:AssetCtx.parse`, `to_snapshot`). On an
attacker-owned dex the attacker can (a) keep `markPx == midPx == oraclePx` so the deviation guards never
fire, and (b) inflate `dayNtlVlm`/`openInterest` so the 0.5%/2% caps allow large orders. Subscribers'
agents then buy at the attacker's chosen oracle price into the attacker's resting sell — value flows from
subscribers to the attacker purely through trades, i.e. the SPEC A1 worst case (theft without any
withdrawal), across every subscriber of that strategy.

Compensating control: listing is maker-checker (two admins) and KYC-gated, so two admins must approve a
strategy whose `MARKETS` include an unusual dex prefix. That lowers likelihood but is a human check, not a
machine control, and the whole JELLY threat model (SECURITY.md §7) says "we cannot rely on the venue" and
"automation must pause first". The guards are explicitly claimed adequate; against an untrusted market
they are not.

Uncertain: whether the launch `xyz` dex is operated by us or a third party (SPEC calls the launch coins
"builder-deployed"). The control gap holds regardless of `xyz`'s ownership.

Fix: enforce a server-side allowlist of trusted perp dexes for creator/no-code `MARKETS` (validator dex
`""` + explicitly platform-operated dex names in config), rejected at `create_strategy` and re-checked at
`upload_version` and at listing. For any non-allowlisted market, require an explicit, separately-recorded
admin approval of that market's deployer. Consider also deriving liquidity/deviation guards from an
independent price source for non-validator markets.

### F2 — [MEDIUM] Creator code and agent keys are sealed under the SAME KMS key; SECURITY.md GAP 3.4(a) unclosed
File: `backend/app/api/adapters.py:739` (`enc = _Encryptor(s)`), `:749` (`agent_keys=AgentKeyAdapter(enc)`),
`:756` (`code_vault=CodeVaultAdapter(enc)`); `_Encryptor` uses `make_encryptor(settings)` on
`settings.kms_key_name` (`backend/app/api/adapters.py:232`, `backend/app/config.py:91,198`).
Executor side: both `Runtime.key_provider` and `Runtime.code_decryptor` call `make_decryptor(settings)` on
the same key (`backend/app/execution/jobs.py` `key_provider`/`code_decryptor`; `backend/app/execution/keys.py`).
Infra: only one app KMS key `agent-keys` exists (`infra/gcp/env.sh:63`, `infra/gcp/bootstrap.sh:182-222`).

SECURITY.md §3.4 explicitly flags this as a [GAP] and recommends a **dedicated `creator-code` KMS key**,
separate from `agent-keys`, with api=encrypt-only / executor=decrypt-only. As built, agent private keys and
creator IP are encrypted under one KMS key. The envelope's record AAD differs
(`aijalon/agent_keys/v1…` vs `strategy_code:{sid}:{hash}`) and the KMS-level wrap AAD is shared, so a
ciphertext of one class cannot be opened as the other (no direct key-swap) — but there is no key-level
blast-radius separation, no independent rotation, and no ability to grant/deny decrypt on creator code
independently of agent keys. Any future path that reaches the executor's decryptor gets both secret classes.

Fix: implement the recommended dedicated `creator-code` KMS key (own IAM: api encrypt-only, executor
decrypt-only), give `CreatorCodeDecryptor`/`CodeVaultAdapter` their own key name in config, and stop reusing
the single `_Encryptor` instance for both. Also parameterise the KMS wrap AAD per secret class.

### F3 — [MEDIUM] Sandbox "determinism" is defeatable via NaN identity hashing (hidden non-determinism)
File: `backend/app/sandbox/runner.py` (bootstrap sets only `PYTHONHASHSEED=0`; docstring claims
"deterministic (no time, no random)… backtest == live"), `backend/app/sandbox/validate.py` (allows `float`,
`set`, comprehensions; forbids `random`/time).

Confirmed locally: a validated script that builds a `set`/`dict` keyed by distinct `float("nan")` objects
gets **process-dependent iteration order**, because CPython ≥3.10 hashes NaN by object identity (id, which
is ASLR-dependent). Three runs of the same script returned `{"BTC": 1.0}`, `{"BTC": -1.0}`, `{"BTC": 1.0}`.
So a creator can smuggle non-determinism/randomness past the AST allowlist and the "no random" rule.

Impact: the guarantees that a strategy is deterministic and that its backtest represents live behaviour are
broken. A creator can publish code that looks flat/benign in the review backtest (`run_series`) but takes
arbitrary (leverage-bounded) directional positions in the single per-bar live run
(`run_creator_signals` → `sandbox /run`), which all subscribers then trade. Not a sandbox escape or direct
theft (positions stay within MAX_LEVERAGE and the pre-trade guards), but it removes an assurance the model
relies on, and it composes badly with F1.

Fix: neutralise NaN-identity ordering — e.g. reject non-finite float **literals/results inside the script's
data structures**, or canonicalise: the child already validates weights, but the non-determinism is in
intermediate control flow. Practical options: (a) forbid `set`/`frozenset`/`dict` construction from
computed float keys is hard statically; instead (b) run each version twice on identical inputs in review and
reject if outputs differ, and (c) document that determinism is best-effort. Prefer (b) as an enforceable
gate at upload.

### F4 — [LOW] In-house signal script-hash pin is enforced only when the version row carries `script_sha256`
File: `backend/app/jobs_data/signals.py` (`pinned[key]` populated only if
`params.script_sha256` is a 64-hex string; passed as `expected_script_sha256`), consumed in
`backend/app/strategies/signals.py:506-509` (`want is not None`).

The control that defends against a compromised terminal repo signing a *different but validly-signed*
CREST engine (SECURITY.md §3.7 [GAP]) is opt-in per strategy version. If `strategy_versions.params` lacks
`script_sha256`, only the feed's self-consistent `engine_sha256` is checked — which an attacker who controls
the signing key also controls. Ensure every in-house version is provisioned with a pinned `script_sha256`,
and consider making the pin mandatory for in-house strategies (reject the feed when a listed in-house
strategy has no pin configured). Uncertain: whether provisioning already guarantees the pin — verify the
listing path always sets it.

### F5 — [INFO] SECURITY.md §7 documents a flag-schema gap that is already fixed in code
File: `backend/app/api/schemas.py:807-808` (`FLAG_KEY_PATTERN` now accepts
`new_entries_paused:{coin}`), `backend/app/api/routers/admin.py:47-50,102-131`.

SECURITY.md §7 "Known mismatch [GAP]" says the admin flag schema does not accept the per-market auto-pause
key `new_entries_paused:{coin}`. The pattern now does accept it, and admins can view/propose/approve lifts
of per-market pauses via `/flags`. No code action; update the doc. (Engaging a switch is single-admin,
lifting is maker-checker with maker≠checker — correct per SPEC.)

---

## Areas reviewed and found sound (no action)

- **API cannot decrypt / read ciphertext.** `make_encryptor` builds a wrapper with `allow_unwrap=False`;
  `EnvelopeEncryptor` has no `open`. `make_decryptor` raises `Forbidden` unless `service_role=="executor"`
  in prod (`kms.py:379-400`). `app_api` selects exclude `key_ciphertext`/`code_ciphertext`
  (`store.py` `_AGENT_COLS`; `execution/pg.py` module doc). IAM: api=`cryptoKeyEncrypter`,
  executor=`cryptoKeyDecrypter` on `agent-keys` only (`bootstrap.sh:218-222`).
- **Cross-user key swap.** Sealed AAD binds `(user_id, agent_address)` (`agent_keys.py:77-85`), and
  `open_agent_key` re-derives the address and refuses on mismatch (`agent_keys.py:119-129`). Loading is
  scoped by `(user_id, master_address, status='active')` (`execution/keys.py:66-75`). A row pointing at
  another user's ciphertext fails AEAD (AAD mismatch) and the address check.
- **No key material in logs/reprs.** `SealedKey`/`SealedBlob`/wrapper `__repr__` hide secrets; plaintext is
  a `bytearray` zeroised via the `opened_agent_key` context manager; the log filter redacts `(0x)?[0-9a-f]{64}`,
  bearer tokens, `sk_/rk_/whsec_`, JWTs (`logging.py:11-21`). Sandbox logs only a 16-hex code-hash prefix.
- **Sub-account / vault ownership chain.** `subscriptions._resolve_master` derives the master **from** the
  trading address via `userRole` (`adapters.py:306` `master_of`, returns a master only for role
  `subAccount`) and requires that master to be a SIWE-verified wallet of the user; vaults (`role: vault`)
  yield `None` → refused. `verify_trading_address` (`readers.py:206-215`) is the equivalent robust check.
  Agents are approved by/confirmed against the master (`agents.py:99` `extra_agents(master)`), and orders
  set `vault_address = trading_address` only when it differs from master (`executor.py:513`,
  `hl/client.py:271-305`, which refuses to silently drop `vault_address`).
- **Agent approval verification.** `confirm_agent` reads `extraAgents(master)` on-chain, matches address,
  checks the name prefix and rejects approvals expiring within 24h (`agents.py:99-108`);
  `verify_agent_approval` additionally cross-checks `userRole(agent)` (`readers.py:172-191`). Due-subscription
  SQL skips users whose agent is inactive/expired (`execution/pg.py` `due_subscriptions`).
- **Typed data.** Domain (name/version/chainId/verifyingContract=0x0), field order, `maxFeeRate` cap
  (1..100 tenths-bp), UsdSend amount canonicalisation and EIP-712 hashing are correct
  (`hl/typed_data.py`); the web client rebuilds and validates server typed data against config
  (`web/src/core/hl.ts:193-229`, builder rate rejected if `> maxFeeTenthsBp`).
- **Every order carries the builder code**; `BuilderCode` refuses empty address / out-of-range fee;
  `place_ioc` has no path to drop it; cloid platform prefix enforced (`hl/client.py:66-104,218-237`).
- **Cloids / replay.** Deterministic `0x a17a1000 + HMAC(sub_id, "ms|coin|attempt")[:12]`; per-`(sub,bar,coin,attempt)`;
  `orders.cloid` globally UNIQUE with `ON CONFLICT DO NOTHING`; unknown outcomes resolved by cloid, never
  re-sent blindly (`executor.py:111-116,526-552`, `execution/pg.py insert_order`).
- **Reduce-only / flips / closing.** `_sanitize_legs` sizes full closes from exact on-chain `|szi|`, drops
  non-reduce-only legs in reduce-only mode, and forbids a reduce-only leg from flipping or exceeding the
  position (`executor.py:649-669`); planner blocks entries (never increases/flips) under
  `reduce_only`/`past_due`/paused, hard-blocks (incl. exits) under kill switches (`domain/risk.py:217-322`).
- **Contacts gate / past_due / kill switch.** Entries gate is fail-closed (missing DB function → all
  `entries_allowed=False`, `execution/pg.py:299-311`); global kill stops all placement incl. exits
  (`executor.py:255-259`); market kill blocks the coin incl. exits (`executor.py:438-440`).
- **Leverage/size guards.** `effective_max_leverage_x100 = min(user, platform*100, market*100, strategy)`
  (`domain/risk.py:158-162`); target clamped to `alloc × effective_leverage`; Σ|w| ≤ MAX_LEVERAGE enforced
  upstream in the sandbox and in `run_creator_signals` (`jobs.py weight_to_bps` + Σ check). No allocation cap
  by owner decision, guards still apply.
- **Concurrency.** Per-subscription non-blocking advisory xact lock on a dedicated connection
  (`execution/pg.py PgLockProvider`, `executor.py:330`); re-read + `is_bar_done` under the lock; order intent
  row is committed before the send. Job-level locks on settle/reconcile/referral-tiers.
- **Jitter.** HMAC-SHA256 keyed with a Secret-Manager pepper-derived salt; unpredictable to outsiders,
  reproducible for audit; prod requires `AUDIT_PEPPER_B64` (`domain/jitter.py`, `jobs.py jitter_salt`).
- **Signal forgery/replay.** Verify-before-parse, pinned 32-byte Ed25519 key, canonical re-encode equality,
  printable-ASCII, staleness (generated_at ≤36h, as_of ≤4d), future-date, replay (as_of monotonic) and
  conflict (same bar, different weight) checks (`strategies/signals.py verify_and_parse`); HTTPS, no
  redirects, size caps on fetch.
- **Sandbox.** Verify-before-parse on the result frame (random per-run nonce; parent discards anything
  outside one framed message → fails closed), restricted builtins + proxy modules, RLIMIT_CPU/AS/FSIZE/NPROC,
  per-call SIGPROF timer raising an un-catchable `BaseException`, wall-clock `killpg`, non-root uid,
  `PR_SET_NO_NEW_PRIVS`/`PR_SET_DUMPABLE=0`, output re-validation. Probes confirmed the per-call CPU timeout
  is not swallowable by `try/finally: return`, and a time-bomb keyed on bar timestamps is inherent to any
  data-driven strategy (constrained by MAX_LEVERAGE + guards). In-process controls are defence-in-depth; the
  real boundary is the no-egress / no-IAM Cloud Run service (`sandbox/Dockerfile`, `sandbox/service.py`),
  which could not be exercised here — verify the deploy flags at go-live.
- **No-code compiler.** Emits source from `repr()` of validated primitives and then re-runs the AST
  validator on the generated source (`sandbox/nocode.py:436`) — one execution path; injection is caught.
- **Payouts/treasury.** Maker-checker with maker≠checker and beneficiary≠approver, `usdSend` verified
  on-chain (`find_usd_send`, exact amount + destination + unused tx hash) before ledger settle
  (`routers/admin.py:250-344`, `routers/withdrawals.py`); treasury key never on a server.

---

## Summary table

| ID | Sev | Area | File(s) | Issue | Fix |
|----|-----|------|---------|-------|-----|
| F1 | HIGH | Creator markets | `api/routers/creator.py:113`, `api/adapters.py:337`, `hl/markets.py:283`, `domain/risk.py` | No trusted-dex allowlist: creators can trade attacker-controlled HIP-3 markets whose oracle/volume/OI feed the guards → thin-market pump, subscriber funds extracted via trades (A1). Only compensated by maker-checker listing. | Server-side allowlist of trusted dexes for `MARKETS` at create/upload/list; explicit deployer approval for others; independent price source for non-validator markets. |
| F2 | MEDIUM | Key custody / segregation | `api/adapters.py:739,749,756`; `execution/jobs.py`, `execution/keys.py`; `infra/gcp/env.sh:63`, `bootstrap.sh:182-222` | Agent keys and creator code sealed under one KMS key `agent-keys`; SECURITY.md GAP 3.4(a) (dedicated `creator-code` key) unclosed. AAD prevents cross-open but no key-level isolation/rotation. | Add a dedicated `creator-code` KMS key with its own IAM; give code vault/decryptor their own key + AAD; stop sharing one `_Encryptor`. |
| F3 | MEDIUM | Sandbox determinism | `sandbox/runner.py`, `sandbox/validate.py` | NaN identity hashing makes set/dict iteration ASLR-dependent → hidden non-determinism (confirmed); breaks "deterministic / backtest==live", lets creators hide production-only behaviour. | Enforce determinism at upload (run twice, reject differing output) and/or reject non-finite floats in strategy data structures. |
| F4 | LOW | Signal integrity | `jobs_data/signals.py`, `strategies/signals.py:506-509` | Script-hash pin (terminal-repo-compromise defence) enforced only when `params.script_sha256` is set. | Make the pin mandatory for in-house strategies; verify provisioning always sets it. |
| F5 | INFO | Docs vs code | `api/schemas.py:807-808`, SECURITY.md §7 | Documented flag-schema GAP (`new_entries_paused:{coin}`) is already fixed in code. | Update SECURITY.md. |

Uncertain items are flagged inline (ownership of the `xyz` dex in F1; in-house pin provisioning in F4).
Cloud Run / IAM / gVisor boundaries were reviewed only via the deploy scripts and must be verified live at
go-live (Gate C pen-test), consistent with SECURITY.md §9.

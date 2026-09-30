# Web core API (web/src/core) — contract for page modules

Owner: web-core. Pages live in `web/src/pages/` and import ONLY from `../core/*.js` (note the `.js`
extension — tsc emits browser ES modules as written). No npm deps, no frameworks, no `innerHTML`
with data. This file is the contract; if the code and this file disagree, the code is fixed.

Build: `node web/build.mjs` → `web/dist/` (tsc `web/tsconfig.json`, `strict`, ES2022 modules).
Output layout: `dist/index.html`, `dist/app/main.js`, `dist/app/core/*.js`, `dist/app/pages/*.js`,
`dist/styles/app.css`, `dist/brand/*`, `dist/app-config.json`. Any `web/src/pages/**/*.css` is copied
to `dist/app/pages/**` (load it with `loadCss`, below). **tsc errors fail the build.**

---

## 1. Page contract

Each route lazily imports `./pages/<name>.js` and calls its `render`.

```ts
// web/src/pages/market.ts
import type { PageContext } from "../core/router.js";
export const title = "Marketplace";               // optional; document.title = `${title} · aijalon.trade`
export async function render(root: HTMLElement, ctx: PageContext): Promise<void | (() => void)> {
  // build DOM into root with h(); return an optional cleanup fn (also: ctx.onCleanup(fn))
}
```

`root` is an empty `<div class="page">` inside `<main>`. The shell (header/nav/footer/theme
toggle/account menu), the site-entry gate and the auth redirect are handled by core — pages never
render them.

```ts
export interface PageContext {
  name: PageName;                    // "strategy"
  path: string;                      // "/s/silver" (no leading '#', no query)
  params: Record<string, string>;    // { slug: "silver" }  (URI-decoded)
  query: URLSearchParams;            // "#/market?asset=BTC" → query.get("asset")
  user: SessionUser | null;          // non-null on auth routes (MFA already satisfied)
  me: Me | null;                     // GET /v1/me profile (role, plan…) when signed in, else null
  signal: AbortSignal;               // aborted when the user navigates away — pass to api calls
  navigate(to: string, opts?: { replace?: boolean }): void;   // to = "/market" or "#/market"
  setTitle(title: string): void;
  onCleanup(fn: () => void): void;
  isCurrent(): boolean;              // false once navigated away (guard late async DOM writes)
}
```

### Routes (hash routing; `router.ts` → `ROUTES`)

| Hash | Page module | Params | Access |
|---|---|---|---|
| `#/` | `pages/home.js` | — | public |
| `#/market` | `pages/market.js` | query: free | public |
| `#/s/:slug` | `pages/strategy.js` | slug | public |
| `#/subscribe/:slug` | `pages/subscribe.js` | slug | user |
| `#/dashboard` , `#/dashboard/:tab` | `pages/dashboard.js` | tab? | user |
| `#/leaderboard` | `pages/leaderboard.js` | — | public |
| `#/posts` , `#/posts/:id` | `pages/posts.js` | id? | public |
| `#/referrals` | `pages/referrals.js` | — | user |
| `#/creator` , `#/creator/:tab` | `pages/creator.js` | tab? | user (page checks `me.role`) |
| `#/admin` , `#/admin/:tab` | `pages/admin.js` | tab? | admin (`me.role === "admin"`) |
| `#/legal/:doc` | `pages/legal.js` | doc | public, **exempt from the entry gate** |
| `#/signin` | `pages/signin.js` | query `next` | public |

- "user" = signed in with Google/Apple **and** TOTP MFA satisfied; otherwise core redirects to
  `#/signin?next=<path>`. "admin" additionally requires `me.role === "admin"` (UI only — the API
  enforces it).
- Every route except `legal` is behind the site-entry gate (core renders the gate instead).
- Unknown routes and pages that fail to import get a core "not found / couldn't load" view.
- Legal doc slugs: `terms`, `risk-disclosure`, `privacy`, `waiver`, `restricted-jurisdictions`,
  `creator-agreement`, `acceptable-use` (`LEGAL_SLUGS` in gate.ts). The gate's links point to these.
- `pages/signin.ts` should be thin: `export { renderSignIn as render } from "../core/auth.js";`
  (core owns the Google/Apple buttons, MFA enrolment with QR, and the MFA code prompt).

Links: use plain `h("a", { href: "#/s/silver" }, …)` or `href("/s/silver")`.

---

## 2. `core/ui.ts` — DOM, dialogs, tables, charts, badges

**Never** use `innerHTML`/`outerHTML`/`insertAdjacentHTML` with data. `h()` only sets text via
text nodes and attributes via `setAttribute` (event handlers via `on*` functions only; `href`/`src`
values with `javascript:`/`data:` (except `data:image/`) schemes are dropped).

```ts
type Child = Node | string | number | null | undefined | false | Child[];
h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs?: Attrs | null, ...children: Child[]): HTMLElementTagNameMap[K]
//   attrs: { class: "btn primary" | ["btn", cond && "primary"], id, style: {color:"red"} (CSSOM, CSP-safe),
//            dataset: {k: "v"}, onclick: (e)=>…, onX…, disabled: true, hidden: false, ...any attr: string|number|boolean|null }
svg(tag: string, attrs?, ...children): SVGElement        // SVG namespace, same rules
text(s): Text ; frag(...children): DocumentFragment
clear(el): el ; mount(el, ...children): el   // replace children
href(path: string): string                    // "/s/x" → "#/s/x"
loadCss(url: string): void                    // adds <link rel=stylesheet> once (use new URL("./x.css", import.meta.url).href)

toast(message: string, kind?: "info"|"good"|"bad"|"warn", ms?: number): void
modal(opts: { title: string; body: Child; actions?: {label, kind?: "primary"|"danger"|"plain", onClick?: () => unknown | Promise<unknown>, close?: boolean}[];
              dismissible?: boolean (default true); wide?: boolean }): { el: HTMLDialogElement; close(): void; closed: Promise<void> }
confirmDialog(opts: { title: string; message: Child; confirmLabel?: string; cancelLabel?: string; danger?: boolean;
                      requireText?: string /* user must type this to enable confirm */ }): Promise<boolean>
promptDialog(opts: { title; message?; label; placeholder?; inputMode?: "numeric"|"text"; pattern?: RegExp; autocomplete?: string; submitLabel? }): Promise<string | null>

spinner(label?: string): HTMLElement
skeleton(lines?: number): HTMLElement                    // shimmer placeholder
loadingInto(root, promise, render: (v) => Child, opts?): Promise<void>   // skeleton → content or errorState
errorState(err: unknown, retry?: () => void): HTMLElement // friendly text from ApiError
emptyState(title: string, detail?: Child, action?: Child): HTMLElement

button(label: Child, opts?: { kind?: "primary"|"danger"|"plain"|"ghost", onClick?: (e) => unknown | Promise<unknown>, type?, disabled?, title? }): HTMLButtonElement
   // async onClick → button disabled + spinner until settled; errors → toast(bad)
field(label: string, control: HTMLElement, hint?: Child): HTMLElement
checkbox(label: Child, opts?: { checked?, required?, onChange?(checked) , name? }): { el: HTMLLabelElement; input: HTMLInputElement }
copyButton(value: string, label?: string): HTMLButtonElement
tabs(items: {key, label}[], active: string, onSelect: (key) => void): HTMLElement

table<T>(opts: { columns: Column<T>[]; rows: T[]; empty?: string; caption?: string; rowKey?: (r) => string; onRowClick?: (r) => void }): HTMLElement
  interface Column<T> { key: string; label: string; value: (row: T) => Child; align?: "left"|"right"; mono?: boolean; primary?: boolean /* card title on mobile */; hideOnMobile?: boolean }
  // Never scrolls sideways: ≥640px normal table; <640px each row becomes a card (label: value pairs).

lineChart(opts: { series: { name: string; points: { t: number /* ms */; v: number }[]; tone?: "accent"|"brass"|"ink"|"good"|"bad"|"muted" }[];
                  height?: number /* default 240 */; yFormat?: (v) => string; xFormat?: (t) => string;
                  baseline?: number /* dashed ref line, e.g. 0 */; markers?: { t: number; label: string }[] /* e.g. version resets */;
                  ariaLabel: string }): HTMLElement
  // responsive SVG (ResizeObserver), hover/touch crosshair + tooltip, both themes via CSS tokens.
sparkline(points: number[], tone?): SVGElement

badge(label: string, tone?: "good"|"bad"|"warn"|"info"|"muted"): HTMLElement
statusBadge(state: "trades" | "holds" | "not_live_proven" | string): HTMLElement
   // "trades" → "Trades" (good); "holds" → "Holds — no active signals" (muted); "not_live_proven" → "Not live-proven" (warn)
subStatusBadge(status: "pending"|"active"|"past_due"|"reduce_only"|"paused_user"|"cancelled"): HTMLElement
note(children: Child, kind?: "warn"|"info"|"bad"): HTMLElement      // callout box
stat(label: string, value: Child, sub?: Child): HTMLElement          // KPI tile (use inside .stats grid)
kv(pairs: [label: string, value: Child][]): HTMLDListElement
```

### CSS classes available (web/styles/app.css; tokens copied from the terminal)
Tokens: `--bg --surface --sunk --ink --muted --faint --line --line-strong --accent --accent-ink
--accent-soft --brass --brass-soft --good --good-soft --bad --bad-soft --warn --warn-soft --r --shadow
--sans --mono`. Layout: `.stack` (vertical gap), `.stack.tight`, `.row` (wrap, gap, center),
`.row.between`, `.grid-2`, `.grid-3`, `.cards` (auto-fill min 280px), `.panel` (card),
`.panel.accent`, `.panel-head`, `.stats` (KPI grid of `.stat`), `.kv`, `.sec-head`, `.eyebrow`,
`.muted`, `.faint`, `.small`, `.mono`, `.num`, `.pos`/`.neg` (green/red text), `.btn`
(`.primary`, `.danger`, `.ghost`, `.sm`), `.btns`, `.pill` (`.good .bad .warn .info .muted`),
`.note` (`.info .bad`), `.field`, `.hint`, `.check`, `.chip`, `.divider`, `.truncate`,
`.visually-hidden`, `.prose` (long text: legal/posts), `.center`, `.w-full`.
Forms: `input[type=text|number|email|search|url]`, `select`, `textarea` are styled globally.
Everything is mobile-first; nothing may cause horizontal page scroll at 390px.

---

## 3. `core/api.ts` — API client

```ts
class ApiError extends Error { status: number; code: string; message: string; details: Record<string, unknown>; requestId?: string }
  // code: server `error.code` (e.g. "step_up_required", "consent_required", "insufficient_balance",
  //       "guard_rejected", "kill_switch_active", "validation_failed", "rate_limited", "not_found",
  //       "unauthorized", "forbidden", "conflict") or "network_error" | "timeout" | "http_<status>".
api.get<T>(path: string, opts?: ReqOpts): Promise<T>
api.post<T>(path: string, body?: unknown, opts?: ReqOpts): Promise<T>
api.patch<T>(path, body?, opts?): Promise<T>
api.del<T>(path, opts?): Promise<T>
interface ReqOpts { signal?: AbortSignal; auth?: boolean /* default: true unless path starts with /public */;
                    idempotencyKey?: string /* default: new UUID for every POST/PATCH/DELETE */; timeoutMs?: number /* 20000 */;
                    stepUp?: boolean /* default true: on 401 step_up_required run auth.stepUp() then retry ONCE with the same key */ }
newIdempotencyKey(): string              // crypto.randomUUID(); keep & reuse it if YOU let the user retry a money action
publicConfig(force?: boolean): Promise<PublicConfig>     // GET /v1/public/config, cached, defensive defaults
```
- `path` is relative to `/v1`: `api.get("/public/strategies")`, `api.post("/subscriptions", {...})`.
- API origin comes from `app-config.json` (`apiOrigin`). Bearer = Firebase ID token (never put
  tokens in URLs; nothing here logs headers or bodies).
- Automatic, at most once per request: 401 `unauthorized` → force-refresh token and retry;
  401 `step_up_required` → `stepUp()` (Google/Apple re-auth popup + TOTP) then retry;
  403 `consent_required` → re-show gate/`syncConsents()` then retry; 401 `mfa_required` → go to sign-in.
- Returns parsed JSON (or `undefined` for 204).

```ts
interface PublicConfig {
  builder_address: string;           // lower-case 0x…
  treasury_address: string;          // lower-case 0x… (USDC fee-balance deposits)
  agent_name: string;                // "aijalon"
  hl_chain: "Mainnet" | "Testnet";
  stripe_publishable_key: string | null;
  restricted_jurisdictions: string[];  // ISO alpha-2
  legal_versions: { terms: string; risk: string; privacy: string; waiver: string; jurisdiction: string; creator_agreement?: string; subscription_ack?: string };
  economics: { builder_fee_tenths_bp: number; builder_split_creator_bps: number; builder_split_platform_bps: number; builder_split_referral_pool_bps: number;
               profit_share_creator_cap_bps: number; platform_profit_share_bps: number; platform_profit_share_mode: "on_top" | "carved_out";
               subscription_platform_bps: number; post_platform_fee_micro: number; post_min_price_micro: number; min_topup_micro: number; past_due_grace_hours: number };
  plans: { key: "free"|"pro"|"max"; price_monthly_micro: number; max_active_strategies: number | null; features: string[] }[];
  features: { creator_uploads: boolean };
  _fallback?: true;                  // set when the API was unreachable and static defaults are shown
}
```

## 4. `core/auth.ts` — Firebase Auth (Google/Apple) + TOTP MFA

```ts
interface SessionUser { uid: string; email: string | null; displayName: string | null; photoURL: string | null;
                        providerId: "google.com" | "apple.com" | string; mfaEnrolled: boolean; mfaSatisfied: boolean }
initAuth(): Promise<void>                    // called by main.ts; loads Firebase from gstatic (pinned), handles redirect result
authReady(): Promise<void>                   // resolves after the first auth state is known
currentUser(): SessionUser | null
onAuthChange(cb: (u: SessionUser | null) => void): () => void    // returns unsubscribe
signIn(provider: "google" | "apple"): Promise<SessionUser | null>  // popup; redirect fallback (mobile/Safari/popup-blocked); runs MFA prompt/enrolment
signOut(): Promise<void>
getIdToken(forceRefresh?: boolean): Promise<string | null>
stepUp(reason?: string): Promise<void>       // re-auth popup with the user's provider + TOTP code; throws ApiError("step_up_cancelled") if cancelled
ensureMfaEnrolled(): Promise<boolean>        // shows the TOTP enrolment dialog (QR + secret) if needed
renderSignIn(root: HTMLElement, ctx: PageContext): void   // full sign-in page (use as pages/signin.ts render)
isAuthConfigured(): boolean                  // false when app-config.json still has placeholder Firebase config
```

## 5. `core/state.ts` — app state

```ts
getMe(force?: boolean): Promise<Me | null>   // GET /v1/me (null when signed out); cached
peekMe(): Me | null
onMeChange(cb): () => void
interface Me { id: string; email: string | null; display_name: string | null; role: "user"|"creator"|"admin"; plan: "free"|"pro"|"max";
               referral_code?: string; mfa_enrolled?: boolean; status?: string; [k: string]: unknown }
createStore<T>(initial: T): { get(): T; set(v: T | ((prev: T) => T)): void; subscribe(cb: (v: T) => void): () => void }
storage.get(key) / storage.set(key, value) / storage.remove(key)   // localStorage with try/catch (JSON)
```

## 6. `core/gate.ts` — site-entry gate and subscribe gate

```ts
LEGAL_SLUGS: Record<ConsentDoc, string>      // terms→"terms", risk→"risk-disclosure", privacy→"privacy", waiver→"waiver", jurisdiction→"restricted-jurisdictions", creator_agreement→"creator-agreement"
siteGateAccepted(cfg?: PublicConfig): boolean
syncConsents(): Promise<void>                // POSTs locally-recorded site-entry consents to /v1/consents after sign-in (core calls it)
subscribeGate(opts: {
  strategy: { id: string; slug: string; name: string; price_monthly_micro: number; profit_share_bps: number; markets: string[]; risk_ack_text?: string | null };
  allocationMicro?: number;                  // optional, shown in the fee examples
}): Promise<boolean>
  // Modal: strategy-specific risk acknowledgement + fee summary (builder fee 0.1% of notional, monthly
  // price, profit share = creator% + platform 1.5% (on_top) or carved out) + Terms/Risk again. Each box
  // required. On accept POSTs consents {doc: "subscription_ack"|"terms"|"risk", context: "subscribe",
  // strategy_id} to /v1/consents and resolves true; resolves false if dismissed.
feeSummary(cfg: PublicConfig, s: {price_monthly_micro, profit_share_bps}): { label: string; value: string; note?: string }[]
```

## 7. `core/wallet.ts` — EIP-1193 / EIP-6963 wallets

```ts
interface WalletInfo { uuid: string; name: string; icon: string /* data:image/… only */; rdns: string }
discoverWallets(waitMs?: number): Promise<{ info: WalletInfo; provider: Eip1193Provider }[]>  // EIP-6963 + legacy window.ethereum
connectWallet(): Promise<Wallet | null>     // picker dialog when >1 wallet; eth_requestAccounts; null if user cancels
getConnectedWallet(): Wallet | null
class Wallet {
  info: WalletInfo; address: string /* lower-case */; 
  chainId(): Promise<number>; chainIdHex(): Promise<`0x${string}`>;
  signTypedDataV4(typed: TypedData): Promise<`0x${string}`>     // checks the domain chainId equals the wallet's current chain
  personalSign(message: string): Promise<`0x${string}`>
  onAccountsChanged(cb) / onChainChanged(cb): () => void
  disconnect(): void                                             // forgets locally (EIP-1193 has no real disconnect)
}
buildOwnershipMessage(p: { address: string; nonce: string; issuedAt: string; chainId: number; domain?: string; uri?: string }): string   // EIP-4361 (SIWE) text
proveOwnership(wallet: Wallet): Promise<{ address: string }>
  // POST /v1/wallets/nonce → {nonce} ; personal_sign(SIWE message) ; POST /v1/wallets/verify {address, message, signature}
toChecksumAddress(addr: string): string     // EIP-55 (keccak256 implemented in core/keccak.ts)
```

## 8. `core/hl.ts` — Hyperliquid user-signed actions (SPEC §6)

```ts
buildApproveAgent(p: { agentAddress: string; agentName: string; nonce: number; signatureChainId: `0x${string}`; hyperliquidChain: "Mainnet"|"Testnet" }): BuiltAction
buildApproveBuilderFee(p: { builder: string; maxFeeRate: string /* "0.1%" */; nonce: number; signatureChainId; hyperliquidChain }): BuiltAction
buildUsdSend(p: { destination: string; amount: string /* "25" / "10.5" USD */; time: number; signatureChainId; hyperliquidChain }): BuiltAction
interface BuiltAction { action: Record<string, unknown>; nonce: number; typedData: TypedData }
validateServerTypedData(kind: "approveAgent"|"approveBuilderFee"|"usdSend", typed: unknown, expect: {...}): { nonce: number; fields: Record<string,string|number> }
  // throws HlValidationError if primaryType/domain/types/chain/agent/builder/maxFeeRate/destination/amount differ from expectations
splitSignature(sig: string): { r: string; s: string; v: number }
postExchange(body: { action; nonce; signature }): Promise<HlResult>     // POST https://api.hyperliquid.xyz/exchange
signAndSubmit(wallet: Wallet, built: BuiltAction): Promise<HlResult>
approveAgent(wallet, p: { agentAddress: string; serverTypedData?: unknown }): Promise<HlResult>        // uses publicConfig().agent_name / hl_chain
approveBuilderFee(wallet, p?: { serverTypedData?: unknown }): Promise<HlResult>                        // builder + max rate from publicConfig()
usdSend(wallet, p: { destination: string; amountMicro: number | bigint; serverTypedData?: unknown; expectDestination: string }): Promise<HlResult>
hlInfo<T>(body: Record<string, unknown>): Promise<T>                        // POST https://api.hyperliquid.xyz/info (read-only)
maxFeeRateFromTenthsBp(100) === "0.1%"
interface HlResult { ok: boolean; status: "ok"|"err"; response: unknown; error?: string }
```
Always: server typed data (if given) is validated, then the final payload is rebuilt locally with
the wallet's **current** chain id as `signatureChainId` (hex) — never signed as received.

## 9. `core/format.ts`

```ts
fmtUsd(micro: number | bigint | string, opts?: { cents?: boolean /* default true */; sign?: boolean; compact?: boolean }): string   // 1234560000 → "$1,234.56" (truncates toward zero, never rounds up)
microToDecimal(micro): string        // 1234567 → "1.234567" (exact, trailing zeros trimmed)
parseUsdToMicro(input: string): bigint | null   // "12.34" → 12340000n ; null for invalid / >6 decimals / negative
fmtBps(bps: number, digits?): string // 150 → "1.5%"
fmtTenthsBp(t: number): string       // 100 → "0.1%"
fmtPct(value: number, opts?: { sign?: boolean; digits?: number }): string   // 12.3456 → "12.35%" (value already in %)
fmtNum(n: number, digits?): string ; fmtLeverage(x100: number): string  // 200 → "2×"
shortAddr(addr: string, lead?: number, tail?: number): string   // "0x1234…abcd"
fmtDate(iso | ms): string  // "30 Sep 2026" (UTC) ; fmtDateTime(): "30 Sep 2026, 14:05 UTC" ; fmtRelative(): "3 h ago"
```

## 10. Other core modules
- `core/theme.ts`: `getTheme(): "light"|"dark"|"system"`, `setTheme(t)`, `effectiveTheme(): "light"|"dark"`, `onThemeChange(cb)`. (Toggle lives in the shell.)
- `core/config.ts`: `appConfig(): AppConfig` (static `app-config.json`: apiOrigin, firebase web config, firebaseSdkVersion, hlApiUrl, siteOrigin).
- `core/stripe.ts`: `loadStripe(): Promise<StripeLike>` (loads https://js.stripe.com/v3/ once with `publicConfig().stripe_publishable_key`).
- `core/qr.ts`: `qrSvg(text: string, opts?: { ecc?: "L"|"M"|"Q"|"H"; size?: number }): SVGSVGElement`.
- `core/router.ts`: `navigate(to, {replace?})`, `currentPath()`, `ROUTES`, types `PageContext`, `PageName`, `PageModule`.

## 11. Server contract assumptions made by core (backend must match or tell web-core)
- Error JSON: `{"error": {"code": "step_up_required", "message": "…", "details": {…}}, "request_id": "…"}`
  (core also tolerates FastAPI `{"detail": "…"}` / `{"detail": {"code": …}}`).
- CORS must allow headers `Authorization, Content-Type, Idempotency-Key` from `https://aijalon.trade`.
- `POST /v1/consents` body `{"consents": [{"doc": "terms", "doc_version": "2026-09-30", "context": "site_entry", "strategy_id": null, "accepted_at": "<ISO, client clock>"}]}`.
- `POST /v1/wallets/nonce` → `{"nonce": "<≥16 chars alnum>"}` (NOT in SPEC §8 yet); `POST /v1/wallets/verify` `{address, message, signature}`; server must rebuild/parse the SIWE message and check domain `aijalon.trade`, nonce, issued-at freshness.
- Referral: `?ref=CODE` captured first-touch for 30 days, sent once after sign-in via `PATCH /v1/me {"referral_code_used": "CODE"}`.
- `GET /v1/me` returns `Me` (above). `401 mfa_required` when the token has no second factor.

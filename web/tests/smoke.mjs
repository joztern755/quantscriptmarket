#!/usr/bin/env node
// Playwright smoke test for the built SPA (web/dist). No npm deps: uses the globally installed Playwright.
//   node web/build.mjs && node web/tests/smoke.mjs [--shots <dir>]
// Serves dist on a random port, mocks the API + third-party hosts, and checks at 1920×1080 and 390×844,
// light + dark: entry gate first, cannot proceed until every box is ticked, no console errors, no
// horizontal scroll, legal pages readable before entry, theme toggle, gate re-shown on version change,
// protected routes redirect to sign-in, Firebase loads from the pinned gstatic URL (stubbed).

import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const PW_PATHS = ["/opt/node22/lib/node_modules/playwright", "playwright"];
let chromium;
for (const p of PW_PATHS) {
  try {
    ({ chromium } = require(p));
    break;
  } catch { /* try next */ }
}
if (!chromium) {
  console.error("playwright not found");
  process.exit(2);
}
process.env.PLAYWRIGHT_BROWSERS_PATH ||= "/opt/pw-browsers";

const DIST = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
if (!existsSync(join(DIST, "index.html"))) {
  console.error("web/dist missing — run node web/build.mjs first");
  process.exit(2);
}
const shotsIdx = process.argv.indexOf("--shots");
const SHOTS = shotsIdx > 0 ? process.argv[shotsIdx + 1] : null;
if (SHOTS) mkdirSync(SHOTS, { recursive: true });

const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json", ".md": "text/markdown; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".webmanifest": "application/manifest+json", ".txt": "text/plain" };
const server = createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  let p = normalize(decodeURIComponent(url.pathname)).replace(/^(\.\.[/\\])+/, "");
  if (p.endsWith("/")) p += "index.html";
  const file = join(DIST, p);
  if (!file.startsWith(DIST) || !existsSync(file) || statSync(file).isDirectory()) {
    res.writeHead(404, { "content-type": "text/plain" });
    return res.end("not found");
  }
  res.writeHead(200, { "content-type": TYPES[extname(file)] || "application/octet-stream", "cache-control": "no-store" });
  res.end(readFileSync(file));
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const BASE = `http://127.0.0.1:${server.address().port}/`;

// ---- API mocks in the backend's REAL response shapes (backend/app/api/schemas.py; docs/API_CONTRACT.md)
const LEGAL = { terms: "2026-09-30", risk: "2026-09-30", privacy: "2026-09-30", waiver: "2026-09-30", jurisdiction: "2026-09-30", creator_agreement: "2026-09-30", subscription_ack: "2026-09-30" };
const LEGAL_FILES = { terms: "terms", risk: "risk-disclosure", privacy: "privacy", waiver: "liability-waiver", jurisdiction: "jurisdiction", creator_agreement: "creator-agreement", subscription_ack: "subscription-ack" };
const CONSENT_DOCS = new Set(Object.keys(LEGAL_FILES)); // DB enum consent_doc
/** Canonical hashes = what backend config.legal_doc_hashes computes from the same files. */
const LEGAL_SHA = Object.fromEntries(Object.entries(LEGAL_FILES).map(([d, f]) => [d, createHash("sha256").update(readFileSync(join(DIST, "legal", `${f}.md`))).digest("hex")]));
function publicConfig(overrides = {}) {
  return {
    builder_address: "0x1111111111111111111111111111111111111111",
    treasury_address: "0x2222222222222222222222222222222222222222",
    agent_name: "aijalon",
    hl_chain: "Mainnet",
    stripe_publishable_key: null,
    stripe_fee_estimate_bps: 340,
    stripe_fee_estimate_fixed_micro: 500000,
    restricted_jurisdictions: ["US", "CU", "IR", "KP", "SY", "RU", "BY", "MM"],
    legal_versions: { ...LEGAL, ...(overrides.legal_versions || {}) },
    economics: {
      builder_fee_tenths_bp: 100, builder_split_creator_bps: 5000, builder_split_platform_bps: 3000, builder_split_referral_pool_bps: 2000,
      profit_share_creator_cap_bps: 1200, platform_profit_share_bps: 150, platform_profit_share_mode: "on_top", subscription_platform_bps: 300,
      post_platform_fee_micro: 1000000, post_min_price_micro: 2000000, min_topup_micro: 10000000, past_due_grace_hours: 72, stripe_fee_absorbed: false,
    },
    plans: [
      { key: "free", price_monthly_micro: 0, max_active_strategies: 1, features: ["marketplace", "leaderboard", "free_posts", "email_telegram_alerts"] },
      { key: "pro", price_monthly_micro: 20000000, max_active_strategies: 3, features: ["marketplace", "leaderboard", "free_posts", "paid_posts", "email_telegram_alerts"] },
      { key: "max", price_monthly_micro: 50000000, max_active_strategies: null, features: ["marketplace", "leaderboard", "free_posts", "paid_posts", "email_telegram_alerts", "csv_export", "read_api"] },
    ],
    referral_tiers: [
      { name: "starter", min_active_users: 0, min_notional_30d_micro: 0, share_of_pool_bps: 5000 },
      { name: "partner", min_active_users: 10, min_notional_30d_micro: 1000000000000, share_of_pool_bps: 7500 },
      { name: "elite", min_active_users: 100, min_notional_30d_micro: 25000000000000, share_of_pool_bps: 10000 },
    ],
    features: { creator_uploads: true, payouts: false },
    // owner decision (30 Sep 2026): no platform / launch leverage cap — the UI bound is min(strategy, HL market)
    platform_max_leverage: 50,
    max_user_leverage_x100: null,
    min_allocation_micro: 100000000,
    min_listing_history_days: 180,
    short_history_warning_days: 365,
    launch_phase: "internal",
  };
}

const SHOWCASE_TEXT = "Free showcase of the engine: $0/month and 0% profit share. The 0.1% builder fee still applies to any orders placed for you. The live signal has been CASH since 1980-01-15 under the current setting (the M2 filter is blocking entries), so subscribers may see no trades for a long time.";
const HIDDEN_STATS = { subscribers: null, roi_bps: null, pnl_micro: null, since: "2026-09-30T00:00:00Z", hidden_reason: "too_few_subscribers" };
const SILVER = {
  id: "0b8f7c9e-1d2a-4c55-9a6e-3f1e2d4c5b6a", slug: "silver", name: "CREST Silver",
  description: "In-house CREST long-or-cash strategy on xyz:SILVER (daily bars, weight 0/1/2).", in_house: true,
  markets: ["xyz:SILVER"], timeframe: "1d", status: "listed", price_monthly_micro: 0, profit_share_bps: 0,
  platform_profit_share_bps: 150, platform_profit_share_mode: "on_top", holds: true, signal_state: "holds",
  current_version: 1, live_since: "2026-09-30T00:00:00Z", live_days: 0, not_live_proven: true, max_leverage: 2,
  history_days: null, short_history_days: null, free_showcase: true, showcase_text: SHOWCASE_TEXT, stats: HIDDEN_STATS,
};
const MOMO = {
  ...SILVER, id: "5c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f", slug: "btc-momo", name: "BTC Momentum 4h", in_house: false,
  description: "Creator strategy.", markets: ["BTC"], timeframe: "4h", price_monthly_micro: 29000000, profit_share_bps: 1000, max_leverage: 20,
  holds: false, signal_state: "trades", history_days: 208, short_history_days: 208, free_showcase: false, showcase_text: null,
};
const SILVER_DETAIL = {
  ...SILVER,
  versions: [{ version: 1, published_at: "2026-09-30T00:00:00Z", live_since: "2026-09-30T00:00:00Z", is_current: true }],
  backtest: null,
  backtest_warning: "Backtest of a newly uploaded script can be fitted to history; not proven live yet",
  risk_ack_text: "I understand that CREST Silver trades xyz:SILVER perpetual futures on Hyperliquid in my own account with up to 2× leverage on the allocation I choose. " + SHOWCASE_TEXT,
  rating_avg_x100: null,
  rating_count: 0,
};
const DAY = 86400000;
const MOMO_DETAIL = {
  ...MOMO,
  versions: [{ version: 2, published_at: "2026-09-20T00:00:00Z", live_since: "2026-09-20T00:00:00Z", is_current: true }, { version: 1, published_at: "2026-08-01T00:00:00Z", live_since: "2026-08-01T00:00:00Z", is_current: false }],
  backtest: {
    period: { sim_days: 208.4, sim_start_t: 1740000000000, sim_end_t: 1740000000000 + 208 * DAY },
    equity_curve: Array.from({ length: 40 }, (_, i) => [1740000000000 + i * 5 * DAY, 1 + i * 0.01]),
    trade_count: 31, liquidated: false,
    metrics: { split_t: 1740000000000 + 140 * DAY, full: { total_return: 0.39 }, in_sample: { total_return: 0.3, max_drawdown: 0.12, sharpe: 1.1, cagr: 0.5, start_t: 1740000000000, end_t: 1740000000000 + 140 * DAY }, out_of_sample: { total_return: 0.07, max_drawdown: 0.05, sharpe: 0.8, cagr: 0.2, start_t: 1740000000000 + 140 * DAY, end_t: 1740000000000 + 208 * DAY } },
    warnings: [],
  },
  backtest_warning: "Backtest of a newly uploaded script can be fitted to history; not proven live yet",
  risk_ack_text: "I understand that BTC Momentum 4h trades BTC perpetual futures.",
  rating_avg_x100: null,
  rating_count: 0,
};
const page = (items) => ({ items, next_cursor: null });
// GET /v1/public/strategies/{slug}/equity → EquitySeriesOut {slug, version, since, points:[{t (UTC day), pnl_micro, roi_bps}], hidden_reason}
const MOMO_V2_LIVE = Date.parse("2026-09-20T00:00:00Z");
const MOMO_EQUITY = {
  slug: "btc-momo", version: 2, since: "2026-09-20T00:00:00Z",
  points: Array.from({ length: 11 }, (_, i) => ({ t: new Date(MOMO_V2_LIVE + i * DAY).toISOString().slice(0, 10), pnl_micro: Math.round((i * 37 - (i % 3) * 20) * 1e6), roi_bps: i * 31 - (i % 3) * 17 })),
  hidden_reason: null,
};
const SILVER_EQUITY = { slug: "silver", version: 1, since: "2026-09-30T00:00:00Z", points: [], hidden_reason: "too_few_subscribers" };
function publicRoute(pathname) {
  if (pathname === "/v1/public/strategies/silver/equity") return SILVER_EQUITY;
  if (pathname === "/v1/public/strategies/btc-momo/equity") return MOMO_EQUITY;
  if (pathname === "/v1/public/strategies") return page([SILVER, MOMO]);
  if (pathname === "/v1/public/strategies/silver") return SILVER_DETAIL;
  if (pathname === "/v1/public/strategies/btc-momo") return MOMO_DETAIL;
  if (/^\/v1\/public\/strategies\/[a-z0-9-]+\/reviews$/.test(pathname)) return page([]);
  if (pathname.startsWith("/v1/public/showcase/")) return [];
  if (pathname === "/v1/public/leaderboard") return { by: "roi", period: "30d", entries: [] };
  if (pathname === "/v1/public/posts") return page([]);
  return null;
}

const FB_APP_STUB = "export function initializeApp(cfg){ return { options: cfg }; }\n";
const FB_AUTH_STUB = `
export const indexedDBLocalPersistence = {}; export const browserLocalPersistence = {}; export const browserPopupRedirectResolver = {};
export class GoogleAuthProvider { setCustomParameters(){} }
export class OAuthProvider { constructor(id){ this.providerId = id; } addScope(){} }
export function initializeAuth(){ return { currentUser: null }; }
export function onIdTokenChanged(auth, cb){ setTimeout(() => cb(null), 0); return () => {}; }
export async function getRedirectResult(){ return null; }
export async function signInWithPopup(){ const e = new Error("closed"); e.code = "auth/popup-closed-by-user"; throw e; }
export async function signInWithRedirect(){}
export async function reauthenticateWithPopup(){}
export async function signOut(){}
export function multiFactor(){ return { enrolledFactors: [], getSession: async () => ({}), enroll: async () => {} }; }
export function getMultiFactorResolver(){ return { hints: [], resolveSignIn: async () => ({}) }; }
export const TotpMultiFactorGenerator = { FACTOR_ID: "totp", generateSecret: async () => ({ secretKey: "JBSWY3DPEHPK3PXP", generateQrCodeUrl: () => "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP" }), assertionForEnrollment(){ return {}; }, assertionForSignIn(){ return {}; } };
`;

let failures = 0;
const results = [];
function check(name, cond, detail = "") {
  results.push(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
  if (!cond) failures++;
}

const browser = await chromium.launch();

async function newPage(viewport, colorScheme, opts = {}) {
  const context = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: 1 });
  const page = await context.newPage();
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console: ${m.text()}${m.location()?.url ? " @ " + m.location().url : ""}`);
  });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("requestfailed", (r) => {
    const u = r.url();
    if (!/fonts\.(googleapis|gstatic)\.com/.test(u) && r.failure()?.errorText !== "net::ERR_ABORTED") errors.push(`requestfailed: ${u} ${r.failure()?.errorText}`);
  });
  let cfgState = opts.config || publicConfig();
  await context.route("https://fonts.googleapis.com/**", (r) => r.fulfill({ status: 200, contentType: "text/css", body: "" }));
  await context.route("https://fonts.gstatic.com/**", (r) => r.fulfill({ status: 204, body: "" }));
  await context.route("https://www.gstatic.com/firebasejs/**", (r) => {
    const u = r.request().url();
    if (u.endsWith("/firebase-app.js")) return r.fulfill({ status: 200, contentType: "text/javascript", body: FB_APP_STUB, headers: { "access-control-allow-origin": "*" } });
    if (u.endsWith("/firebase-auth.js")) return r.fulfill({ status: 200, contentType: "text/javascript", body: opts.fbAuth ?? FB_AUTH_STUB, headers: { "access-control-allow-origin": "*" } });
    return r.fulfill({ status: 404, body: "" });
  });
  await context.route("https://api.aijalon.trade/**", (r) => {
    const u = new URL(r.request().url());
    const cors = { "access-control-allow-origin": "*", "access-control-allow-headers": "authorization, content-type, idempotency-key", "access-control-allow-methods": "GET, POST, PATCH, DELETE" };
    if (r.request().method() === "OPTIONS") return r.fulfill({ status: 204, headers: cors });
    if (u.pathname === "/v1/public/config") return r.fulfill({ status: 200, contentType: "application/json", headers: cors, body: JSON.stringify(cfgState) });
    const pub = publicRoute(u.pathname);
    if (pub !== null) return r.fulfill({ status: 200, contentType: "application/json", headers: cors, body: JSON.stringify(pub) });
    if (u.pathname.startsWith("/v1/public/")) return r.fulfill({ status: 404, contentType: "application/json", headers: cors, body: JSON.stringify({ error: { code: "not_found", message: "not found" }, request_id: "t" }) });
    if (opts.api) {
      const res = opts.api(r.request(), u);
      if (res) return r.fulfill({ status: res.status ?? 200, contentType: "application/json", headers: cors, body: JSON.stringify(res.body ?? {}) });
    }
    return r.fulfill({ status: 401, contentType: "application/json", headers: cors, body: JSON.stringify({ error: { code: "unauthorized", message: "sign in" } }) });
  });
  if (opts.hl) {
    // Hyperliquid info API (read-only): opts.hl(body) → JSON
    await context.route("https://api.hyperliquid.xyz/**", (r) => {
      const cors = { "access-control-allow-origin": "*", "access-control-allow-headers": "content-type" };
      if (r.request().method() === "OPTIONS") return r.fulfill({ status: 204, headers: cors });
      const out = opts.hl(JSON.parse(r.request().postData() || "{}"));
      return r.fulfill({ status: out ? 200 : 422, contentType: "application/json", headers: cors, body: JSON.stringify(out ?? "unknown") });
    });
  }
  if (opts.appConfig) {
    await context.route(`${BASE}app-config.json`, (r) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(opts.appConfig) }));
  }
  return { context, page, errors, setConfig: (c) => (cfgState = c) };
}

async function noHorizontalScroll(page) {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const wide = [...document.querySelectorAll("body *")].filter((el) => {
      const r = el.getBoundingClientRect();
      // visually-hidden table headers (mobile card tables clip <thead> to 1px) do not paint
      return r.width > 0 && r.right > window.innerWidth + 1 && getComputedStyle(el).position !== "fixed" && !el.closest(".rtable thead");
    }).slice(0, 3).map((el) => `${el.tagName.toLowerCase()}.${el.className}`);
    return { ok: doc.scrollWidth <= window.innerWidth && wide.length === 0, sw: doc.scrollWidth, w: window.innerWidth, wide };
  });
}

const VIEWPORTS = [
  { name: "desktop", viewport: { width: 1920, height: 1080 } },
  { name: "mobile", viewport: { width: 390, height: 844 } },
];

for (const vp of VIEWPORTS) {
  for (const scheme of ["light", "dark"]) {
    const tag = `${vp.name}/${scheme}`;
    const { context, page, errors, setConfig } = await newPage(vp.viewport, scheme);
    await page.goto(BASE, { waitUntil: "networkidle" });

    // Gate first
    await page.waitForSelector("#gate-title", { timeout: 10000 });
    check(`${tag}: gate shown on first visit`, await page.isVisible("#gate-title"));
    check(`${tag}: nav hidden while gated`, !(await page.isVisible("#site-nav a")));
    check(`${tag}: restricted list rendered`, (await page.locator(".juris-list li").count()) === 8);
    const boxes = page.locator(".gate-form input[type=checkbox]");
    check(`${tag}: five required checkboxes`, (await boxes.count()) === 5);
    check(`${tag}: each box links to a legal doc`, (await page.locator(".gate-form .check a[href^='#/legal/']").count()) === 5);
    const btn = page.locator("#gate-accept");
    check(`${tag}: enter disabled with nothing ticked`, await btn.isDisabled());
    for (let i = 0; i < 4; i++) await boxes.nth(i).check();
    check(`${tag}: enter still disabled with 4/5 ticked`, await btn.isDisabled());
    await btn.click({ force: true }).catch(() => undefined);
    check(`${tag}: forced click does not enter`, await page.isVisible("#gate-title"));
    let hs = await noHorizontalScroll(page);
    check(`${tag}: no horizontal scroll on gate`, hs.ok, JSON.stringify(hs));
    if (SHOTS) await page.screenshot({ path: join(SHOTS, `${vp.name}-${scheme}-gate.png`), fullPage: true });

    // Legal page readable before entry; ticks survive the round trip
    await page.click(".gate-form a[href='#/legal/terms']");
    await page.waitForFunction(() => location.hash === "#/legal/terms");
    await page.waitForTimeout(400);
    check(`${tag}: legal page not gated`, !(await page.isVisible("#gate-title")));
    await page.goBack();
    await page.waitForSelector("#gate-title");
    check(`${tag}: ticks kept after reading a doc`, (await page.locator(".gate-form input[type=checkbox]:checked").count()) === 4);

    await page.locator(".gate-form input[type=checkbox]").nth(4).check();
    check(`${tag}: enter enabled when all ticked`, !(await page.locator("#gate-accept").isDisabled()));
    await page.click("#gate-accept");
    await page.waitForSelector("#gate-title", { state: "detached" });
    await page.waitForTimeout(600);
    check(`${tag}: content shown after entry`, (await page.locator("main .page").count()) === 1 && (await page.isVisible("header .brand")));
    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("aij.consents.v1") || "{}"));
    check(`${tag}: consent versions stored locally`, stored?.site?.terms?.version === "2026-09-30" && Object.keys(stored.site).length === 5);
    hs = await noHorizontalScroll(page);
    check(`${tag}: no horizontal scroll on home`, hs.ok, JSON.stringify(hs));
    if (SHOTS) await page.screenshot({ path: join(SHOTS, `${vp.name}-${scheme}-home.png`), fullPage: true });

    // Theme follows system, toggle flips and persists
    const bg1 = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    check(`${tag}: system theme applied`, scheme === "dark" ? bg1 === "rgb(20, 17, 14)" : bg1 === "rgb(246, 244, 240)", bg1);
    await page.click("#theme-toggle");
    const bg2 = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    const stTheme = await page.evaluate(() => localStorage.getItem("aij.theme"));
    check(`${tag}: toggle switches theme`, bg1 !== bg2 && stTheme === (scheme === "dark" ? "light" : "dark"), `${bg1} → ${bg2}`);
    await page.reload({ waitUntil: "networkidle" });
    const bg3 = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    check(`${tag}: theme choice persists`, bg3 === bg2);
    check(`${tag}: gate not shown again after reload`, !(await page.isVisible("#gate-title")));
    await page.click("#theme-toggle");

    // Marketplace + strategy page render the backend's real shapes (SPEC §12 showcase / short history)
    await page.goto(BASE + "#/market", { waitUntil: "networkidle" });
    await page.waitForSelector(".s-card", { timeout: 8000 }).catch(() => undefined);
    check(`${tag}: market lists both strategies`, (await page.locator(".s-card").count()) === 2);
    const cards = (await page.locator(".s-card").allInnerTexts()).join(" | ");
    check(`${tag}: SILVER card says Free showcase + CASH since 1980`, /Free showcase/.test(cards) && /CASH since 1980-01-15/.test(cards), cards.slice(0, 160));
    check(`${tag}: short-history badge on card`, /Short history \(208 days\)/i.test(cards));
    check(`${tag}: holds badge from signal_state`, /Holds — no active signals/i.test(cards));
    await page.goto(BASE + "#/s/btc-momo", { waitUntil: "networkidle" });
    await page.waitForTimeout(300);
    const detail = await page.locator("main .page").innerText();
    check(`${tag}: strategy page shows backtest + both warnings`, /not proven live yet/.test(detail) && /Short history \(208 days\)/i.test(detail) && /Out-of-sample/.test(detail));
    check(`${tag}: version reset timeline`, /v2/.test(detail) && /performance reset/.test(detail));
    // Live equity chart (GET /public/strategies/{slug}/equity): renders, hover tooltip, version-reset marker, themed stroke
    await page.waitForSelector(".equity-live svg.chart", { timeout: 5000 }).catch(() => undefined);
    check(`${tag}: live equity chart renders`, (await page.locator(".equity-live svg.chart path.line").count()) === 1 && (await page.locator("[data-equity=hidden]").count()) === 0);
    check(`${tag}: equity chart marks the v2 reset`, (await page.locator(".equity-live line.marker").count()) >= 1);
    const stroke = await page.locator(".equity-live path.line").evaluate((el) => getComputedStyle(el).stroke);
    check(`${tag}: equity line uses a theme colour`, /^rgb/.test(stroke) && stroke !== "rgb(0, 0, 0)", stroke);
    await page.locator(".equity-live svg.chart").scrollIntoViewIfNeeded();
    const chartBox = await page.locator(".equity-live svg.chart").boundingBox();
    if (chartBox) await page.mouse.move(chartBox.x + chartBox.width * 0.6, chartBox.y + chartBox.height / 2);
    await page.waitForTimeout(100);
    const tipText = await page.locator(".equity-live .lchart-tip").innerText().catch(() => "");
    check(`${tag}: equity chart hover tooltip`, (await page.locator(".equity-live .lchart-tip").isVisible()) && /%/.test(tipText), tipText);
    await page.click(".equity-live .seg button:has-text('$ made')");
    check(`${tag}: equity chart switches to $ made`, /\$/.test(await page.locator(".equity-live svg.chart").innerHTML()));
    check(`${tag}: short-history + not-live-proven warnings kept`, /Short history \(208 days\)/.test(detail) && /Not live-proven/.test(await page.locator("main .page").innerText()));
    hs = await noHorizontalScroll(page);
    check(`${tag}: strategy page with chart without horizontal scroll`, hs.ok, JSON.stringify(hs));
    if (SHOTS) await page.screenshot({ path: join(SHOTS, `${vp.name}-${scheme}-strategy.png`), fullPage: true });
    await page.goto(BASE + "#/s/silver", { waitUntil: "networkidle" });
    await page.waitForTimeout(300);
    const silverText = await page.locator("main .page").innerText();
    check(`${tag}: SILVER page states the free showcase`, /CASH since 1980-01-15/.test(silverText));
    check(`${tag}: equity chart hidden with hidden_reason message`, (await page.locator("[data-equity=hidden][data-reason=too_few_subscribers]").count()) === 1 && (await page.locator(".equity-live").count()) === 0 && /at least 5 subscribers/.test(silverText));
    hs = await noHorizontalScroll(page);
    check(`${tag}: strategy page without horizontal scroll`, hs.ok, JSON.stringify(hs));

    // Other routes
    for (const route of ["#/market", "#/leaderboard", "#/posts", "#/legal/risk-disclosure", "#/legal/liability-waiver", "#/legal/subscription-ack", "#/signin", "#/nope"]) {
      await page.goto(BASE + route, { waitUntil: "networkidle" });
      await page.waitForTimeout(300);
      hs = await noHorizontalScroll(page);
      check(`${tag}: ${route} renders without horizontal scroll`, hs.ok && (await page.locator("main .page").count()) === 1, JSON.stringify(hs));
    }
    if (SHOTS) await page.screenshot({ path: join(SHOTS, `${vp.name}-${scheme}-signin.png`), fullPage: false });
    await page.goto(BASE + "#/dashboard", { waitUntil: "networkidle" });
    await page.waitForFunction(() => location.hash.startsWith("#/signin"), null, { timeout: 12000 }).catch(() => undefined);
    check(`${tag}: protected route redirects to sign-in`, (await page.evaluate(() => location.hash)).startsWith("#/signin?next=%2Fdashboard"));

    // Mobile menu
    if (vp.name === "mobile") {
      await page.goto(BASE + "#/market", { waitUntil: "networkidle" });
      check(`${tag}: nav collapsed on mobile`, !(await page.isVisible("#site-nav a")));
      await page.click(".menu-btn");
      check(`${tag}: menu opens`, await page.isVisible("#site-nav a"));
      hs = await noHorizontalScroll(page);
      check(`${tag}: no horizontal scroll with menu open`, hs.ok, JSON.stringify(hs));
    }

    // Version change → gate again, with "updated" note
    setConfig(publicConfig({ legal_versions: { terms: "2026-10-15" } }));
    await page.goto(BASE + "#/", { waitUntil: "networkidle" });
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForSelector("#gate-title", { timeout: 8000 }).catch(() => undefined);
    check(`${tag}: gate re-shown when a version changes`, await page.isVisible("#gate-title"));
    check(`${tag}: update notice shown`, (await page.locator(".gate-form .note.info").count()) >= 1);

    check(`${tag}: no console errors`, errors.length === 0, errors.slice(0, 5).join(" | "));
    await context.close();
  }
}

// Firebase configured: SDK loads from the pinned gstatic URL (stubbed) and sign-in buttons render.
{
  const appConfig = JSON.parse(readFileSync(join(DIST, "app-config.json"), "utf8"));
  appConfig.firebase = { apiKey: "test-key", authDomain: "aijalon.trade", projectId: "aijalon-test", appId: "1:1:web:1" };
  const { context, page, errors } = await newPage({ width: 390, height: 844 }, "light", { appConfig });
  const fbRequests = [];
  page.on("request", (r) => {
    if (r.url().includes("gstatic.com/firebasejs/")) fbRequests.push(r.url());
  });
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.evaluate(() => {
    localStorage.setItem("aij.consents.v1", JSON.stringify({ site: Object.fromEntries(["jurisdiction", "terms", "risk", "privacy", "waiver"].map((d) => [d, { version: "2026-09-30", accepted_at: new Date().toISOString() }])), synced: {} }));
  });
  await page.goto(BASE + "#/signin", { waitUntil: "networkidle" });
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(500);
  const v = appConfig.firebaseSdkVersion;
  check("firebase: modules requested from pinned version", fbRequests.some((u) => u.endsWith(`/firebasejs/${v}/firebase-app.js`)) && fbRequests.some((u) => u.endsWith(`/firebasejs/${v}/firebase-auth.js`)), fbRequests.join(", "));
  const btns = await page.locator(".signin .btn").allInnerTexts();
  check("firebase: Google + Apple sign-in buttons", btns.some((t) => /Google/.test(t)) && btns.some((t) => /Apple/.test(t)), btns.join(" / "));
  await page.click("text=Continue with Google");
  await page.waitForTimeout(300);
  check("firebase: closed popup handled quietly", await page.isVisible(".signin"));
  if (SHOTS) await page.screenshot({ path: join(SHOTS, "firebase-signin.png") });
  check("firebase: no console errors", errors.length === 0, errors.slice(0, 5).join(" | "));
  await context.close();
}

// Signed-in (TOTP) user: consents carry doc_text_sha256 of the served legal files with backend doc keys; the
// dashboard renders the backend's Page/Balance shapes; cancel = two buttons + double confirm + step-up →
// DELETE /v1/subscriptions/{id} {"positions": "close"} (SPEC §12).
const FB_AUTH_SIGNED_IN = FB_AUTH_STUB
  .replace("export function initializeAuth(){ return { currentUser: null }; }", "export function initializeAuth(){ return { currentUser: USER }; }")
  .replace("export function onIdTokenChanged(auth, cb){ setTimeout(() => cb(null), 0); return () => {}; }", "export function onIdTokenChanged(auth, cb){ setTimeout(() => cb(USER), 0); return () => {}; }")
  .replace("export function multiFactor(){ return { enrolledFactors: [],", "export function multiFactor(){ return { enrolledFactors: [{ factorId: 'totp' }],")
  + `\nconst USER = { uid: "fb-1", email: "u@example.com", displayName: "U", photoURL: null, providerData: [{ providerId: "google.com" }],
    getIdToken: async () => "tok-1", getIdTokenResult: async () => ({ signInSecondFactor: "totp", claims: { firebase: { sign_in_second_factor: "totp" } } }) };\n`;
const SIGNED_IN_APP_CONFIG = JSON.parse(readFileSync(join(DIST, "app-config.json"), "utf8"));
SIGNED_IN_APP_CONFIG.firebase = { apiKey: "test-key", authDomain: "aijalon.trade", projectId: "aijalon-test", appId: "1:1:web:1" };
const SUB = {
  id: "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d", strategy_id: SILVER.id, strategy_slug: "silver", strategy_name: "CREST Silver", strategy_markets: ["xyz:SILVER"],
  trading_address: "0x3333333333333333333333333333333333333333", allocation_micro: 500000000, max_leverage_x100: 100, status: "active",
  cancel_positions: null, cancelled_at: null, current_period_end: "2026-10-30T00:00:00Z", cum_pnl_micro: 0, hwm_micro: 0, created_at: "2026-09-30T00:00:00Z",
};
/** GET /v1/alerts/contacts (ContactsOut) — linked = Telegram linked + email confirmed. */
function contactsOut(linked) {
  return {
    telegram: { status: linked ? "linked" : "unlinked", linked_at: linked ? "2026-09-30T00:00:00Z" : null, lapsed_at: null },
    email: { address: linked ? "u@example.com" : null, verified: linked, pending: null, account_email: "u@example.com" },
    ready: linked, missing: linked ? [] : ["telegram", "email"], entries_allowed: linked, entries_pause_at: null,
  };
}
const ALERT_PREFS = [
  { kind: "trade_opened", label: "Trade opened", group: "trades", group_label: "Trades", mandatory: false, muted: false, channels: ["telegram", "in_app"] },
  { kind: "fee_balance_low", label: "Fee balance low", group: "money", group_label: "Fee balance & money", mandatory: true, muted: false, channels: ["telegram", "email", "in_app"] },
];
const ME = { id: "11111111-2222-4333-8444-555555555555", email: "u@example.com", display_name: "U", role: "user", plan: "free", status: "active", referral_code: "abcd2345", country_attested: null, mfa_enrolled: true, created_at: "2026-09-30T00:00:00Z", consents_complete: true, wallets: [], kyc_status: null };
async function enterSite(pg) {
  await pg.goto(BASE, { waitUntil: "networkidle" });
  await pg.waitForSelector("#gate-title", { timeout: 10000 });
  const bx = pg.locator(".gate-form input[type=checkbox]");
  for (let i = 0; i < 5; i++) await bx.nth(i).check();
  await pg.click("#gate-accept");
  await pg.waitForSelector("#gate-title", { state: "detached" });
  await pg.waitForTimeout(1200);
}

{
  const appConfig = SIGNED_IN_APP_CONFIG;
  const consentBodies = [];
  const deletes = [];
  const unknown = [];
  const api = (req, u) => {
    const m = req.method();
    const p = u.pathname;
    if (m === "GET" && p === "/v1/me") return { body: ME };
    if (m === "GET" && p === "/v1/alerts/contacts") return { body: contactsOut(true) };
    if (m === "POST" && p === "/v1/consents") {
      consentBodies.push(JSON.parse(req.postData() || "{}"));
      return { body: { required: {}, accepted: {}, missing: [], complete: true } };
    }
    if (m === "GET" && p === "/v1/subscriptions") return { body: page([SUB]) };
    if (m === "GET" && p === "/v1/balance") return { body: { fee_balance_micro: 25000000, withdrawable_micro: 0, withdrawals_pending_micro: 0, estimated_monthly_need_micro: 0, reserve_required_micro: 10000000, min_topup_micro: 10000000 } };
    if (m === "GET" && p === "/v1/balance/ledger") return { body: page([{ tx_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", kind: "deposit_stripe", memo: "deposit", amount_micro: 25000000, created_at: "2026-09-30T00:00:00Z" }]) };
    if (m === "GET" && p === "/v1/alerts") return { body: page([{ id: "a1a1a1a1-b2b2-4c3c-8d4d-e5e5e5e5e5e5", severity: "warn", kind: "fee_balance_low", payload: { level: "50%" }, created_at: "2026-09-30T00:00:00Z", acked_at: null }]) };
    if (m === "GET" && p === "/v1/positions") return { body: { positions: [{ trading_address: SUB.trading_address, coin: "xyz:SILVER", size: "-1.5", entry_px: "31.2", position_value: "46.8", unrealized_pnl: "0.4", leverage: "1", liquidation_px: null }], unavailable: [] } };
    if (m === "DELETE" && p === `/v1/subscriptions/${SUB.id}`) {
      deletes.push(JSON.parse(req.postData() || "null"));
      return { body: { ...SUB, status: "closing", cancel_positions: "close" } };
    }
    unknown.push(`${m} ${p}`);
    return { status: 404, body: { error: { code: "not_found", message: "not mocked" } } };
  };
  const { context, page: pg, errors } = await newPage({ width: 1280, height: 900 }, "light", { appConfig, fbAuth: FB_AUTH_SIGNED_IN, api });
  await pg.goto(BASE, { waitUntil: "networkidle" });
  await pg.waitForSelector("#gate-title", { timeout: 10000 });
  const bx = pg.locator(".gate-form input[type=checkbox]");
  for (let i = 0; i < 5; i++) await bx.nth(i).check();
  await pg.click("#gate-accept");
  await pg.waitForSelector("#gate-title", { state: "detached" });
  await pg.waitForTimeout(1200);
  const siteConsents = consentBodies.flatMap((b) => b.consents ?? []);
  check("signed-in: site consents posted for the 5 site docs", siteConsents.length === 5 && siteConsents.every((c) => c.context === "site_entry"), JSON.stringify(siteConsents.map((c) => c.doc)));
  check("signed-in: consent doc keys = backend enum", siteConsents.every((c) => CONSENT_DOCS.has(c.doc)));
  check("signed-in: doc_text_sha256 = sha256 of served legal file", siteConsents.length > 0 && siteConsents.every((c) => c.doc_text_sha256 === LEGAL_SHA[c.doc] && c.doc_version === LEGAL[c.doc]), JSON.stringify(siteConsents.map((c) => [c.doc, c.doc_text_sha256?.slice(0, 8)])));
  check("signed-in: no unknown consent fields", siteConsents.every((c) => Object.keys(c).every((k) => ["doc", "doc_version", "context", "strategy_id", "accepted_at", "doc_text_sha256", "country"].includes(k))));

  await pg.goto(BASE + "#/dashboard", { waitUntil: "networkidle" });
  await pg.waitForSelector("text=CREST Silver", { timeout: 8000 }).catch(() => undefined);
  const dash = await pg.locator("main .page").innerText();
  check("dashboard: fee balance from fee_balance_micro", /\$25\.00/.test(dash), dash.slice(0, 200));
  check("dashboard: subscription row from Page.items", /CREST Silver/.test(dash) && /\$500\.00/.test(dash));
  check("dashboard: no type-CANCEL text box", (await pg.locator("text=Type CANCEL").count()) === 0);
  await pg.click("button:has-text('Cancel…')");
  await pg.waitForSelector("button:has-text('Close positions and cancel')");
  check("cancel: two explicit buttons", (await pg.locator("button:has-text('Close positions and cancel')").count()) === 1 && (await pg.locator("button:has-text('Leave positions open and cancel')").count()) === 1);
  await pg.click("button:has-text('Close positions and cancel')");
  await pg.waitForSelector("dialog >> text=Continue");
  await pg.click("dialog >> button:has-text('Continue')");
  await pg.waitForSelector("dialog >> text=Please confirm again");
  check("cancel: second dialog restates the consequence", (await pg.locator("dialog >> text=/close your open positions/").count()) >= 1);
  await pg.locator("dialog").last().locator("button:has-text('Close positions and cancel')").click();
  await pg.waitForSelector("dialog >> text=Confirm it's you");
  await pg.click("dialog >> button:has-text('Continue with Google')");
  await pg.waitForTimeout(800);
  check("cancel: DELETE body {positions:'close'} after step-up", deletes.length === 1 && deletes[0]?.positions === "close" && Object.keys(deletes[0]).length === 1, JSON.stringify(deletes));
  await pg.goto(BASE + "#/dashboard/balance", { waitUntil: "networkidle" });
  await pg.waitForTimeout(500);
  const bal = await pg.locator("main .page").innerText();
  check("balance tab: ledger from /balance/ledger + fee estimate notice", /Deposit \(card\)/.test(bal) && /processor fee/.test(bal));
  await pg.goto(BASE + "#/dashboard/positions", { waitUntil: "networkidle" });
  await pg.waitForTimeout(500);
  check("positions tab: PositionsOut.size → Short", /Short/i.test(await pg.locator("main .page").innerText()));
  check("signed-in: every API call was a mocked backend route", unknown.length === 0, unknown.join(", "));
  check("signed-in: no console errors", errors.length === 0, errors.slice(0, 5).join(" | "));
  await context.close();
}

// Signed-in, both viewports × both themes: #/alerts (unlinked → linked), dashboard alerts link + set-up note,
// Plans (Free $0 / Pro $20 / Max $50 from public config; confirm + step-up → POST /v1/me/plan; insufficient balance →
// top-up prompt, both client-side and on 402), Creator Studio own posts + per-strategy earnings (cards on phones).
for (const vp of VIEWPORTS) {
  for (const scheme of ["light", "dark"]) {
    const tag = `signed-in ${vp.name}/${scheme}`;
    const st = { linked: false, plan: "free", fee: 25000000 };
    const planPosts = [];
    const unknown = [];
    const PLAN_PRICE = { free: 0, pro: 20000000, max: 50000000 };
    const SUB2 = { ...SUB, id: "7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d", strategy_id: MOMO.id, strategy_slug: "btc-momo", strategy_name: "BTC Momentum 4h", strategy_markets: ["BTC"], trading_address: "0x4444444444444444444444444444444444444444", max_leverage_x100: 300 };
    const CREATOR_STRAT = { id: MOMO.id, slug: "btc-momo", name: "BTC Momentum 4h", status: "listed", markets: ["BTC"], timeframe: "4h", price_monthly_micro: 29000000, profit_share_bps: 1000, description: null, created_at: "2026-08-01T00:00:00Z" };
    const api = (req, u) => {
      const m = req.method();
      const p = u.pathname;
      if (m === "GET" && p === "/v1/me") return { body: { ...ME, plan: st.plan } };
      if (m === "POST" && p === "/v1/consents") return { body: { required: {}, accepted: {}, missing: [], complete: true } };
      if (m === "GET" && p === "/v1/alerts/contacts") return { body: contactsOut(st.linked) };
      if (m === "GET" && p === "/v1/alerts/settings") return { body: { contacts: contactsOut(st.linked), prefs: ALERT_PREFS, telegram_bot: "aijalon_bot", email_policy: "Telegram carries every alert. Email carries only mandatory alerts and security / money events." } };
      if (m === "GET" && p === "/v1/alerts") return { body: page([]) };
      if (m === "GET" && p === "/v1/subscriptions") return { body: page([SUB, SUB2]) };
      if (m === "GET" && p === "/v1/positions") return { body: { positions: [], unavailable: [] } };
      if (m === "GET" && p === "/v1/balance") return { body: { fee_balance_micro: st.fee, withdrawable_micro: 0, withdrawals_pending_micro: 0, estimated_monthly_need_micro: 0, reserve_required_micro: 0, min_topup_micro: 10000000 } };
      if (m === "GET" && p === "/v1/balance/ledger") return { body: page([]) };
      if (m === "POST" && p === "/v1/me/plan") {
        const body = JSON.parse(req.postData() || "{}");
        planPosts.push({ body, key: req.headers()["idempotency-key"] });
        if (body.plan === "max") return { status: 402, body: { error: { code: "insufficient_balance", message: "Not enough fee balance.", details: {} }, request_id: "t" } };
        st.plan = body.plan;
        st.fee -= PLAN_PRICE[body.plan];
        return { body: { plan: body.plan, charged_micro: PLAN_PRICE[body.plan], period_end: "2026-10-30T00:00:00Z", fee_balance_micro: st.fee } };
      }
      if (m === "GET" && p === "/v1/creator/strategies") return { body: [CREATOR_STRAT] };
      if (m === "GET" && p === "/v1/creator/posts") return { body: page([
        { id: "p0000000-0000-4000-8000-000000000001", title: "Weekly notes: why we held cash", price_micro: 0, strategy_slug: "btc-momo", published_at: "2026-09-28T00:00:00Z", created_at: "2026-09-28T00:00:00Z", body: "Free body.", sales: 0, gross_sales_micro: 0 },
        { id: "p0000000-0000-4000-8000-000000000002", title: "Deep dive: the 4h momentum filter, entry timing and why the stop sits where it does", price_micro: 5000000, strategy_slug: "btc-momo", published_at: "2026-09-29T00:00:00Z", created_at: "2026-09-29T00:00:00Z", body: "Paid body.", sales: 7, gross_sales_micro: 35000000 },
      ]) };
      if (m === "GET" && p === "/v1/creator/earnings") return { body: {
        payable_micro: 150000000, payouts_pending_micro: 0, total_earned_micro: 498760000,
        by_strategy: [{ strategy_id: MOMO.id, slug: "btc-momo", active_subscribers: 12, earned_micro: 489760000, builder_share_micro: 4200000, subscription_share_micro: 337560000, profit_share_micro: 120000000, posts_micro: 28000000 }],
        general_posts_micro: 9000000, other_micro: 0,
        recent: [],
      } };
      unknown.push(`${m} ${p}`);
      return { status: 404, body: { error: { code: "not_found", message: "not mocked" } } };
    };
    // HL meta: BTC max 12× (below the strategy's 20× → the market is the bound); builder dex xyz lists SILVER at 10×
    const hlBodies = [];
    const hl = (body) => {
      hlBodies.push(body);
      if (body.type === "meta" && !body.dex) return { universe: [{ name: "BTC", szDecimals: 5, maxLeverage: 12 }, { name: "SOL", szDecimals: 2, maxLeverage: 10 }] };
      if (body.type === "meta" && body.dex === "xyz") return { universe: [{ name: "xyz:SILVER", szDecimals: 2, maxLeverage: 10 }] };
      return null;
    };
    const { context, page: pg, errors } = await newPage(vp.viewport, scheme, { appConfig: SIGNED_IN_APP_CONFIG, fbAuth: FB_AUTH_SIGNED_IN, api, hl });
    await enterSite(pg);
    const hsOk = async (name) => {
      const r = await noHorizontalScroll(pg);
      check(`${tag}: ${name} without horizontal scroll`, r.ok, JSON.stringify(r));
    };
    const text = () => pg.locator("main .page").innerText();

    // Nav entry (desktop bar / mobile menu)
    if (vp.name === "mobile") await pg.click(".menu-btn");
    check(`${tag}: Alerts in the main nav`, await pg.isVisible("#site-nav a[href='#/alerts']"));
    if (vp.name === "mobile") await pg.click(".menu-btn");

    // Dashboard: set-up note while alerts are not linked → link to #/alerts
    await pg.goto(BASE + "#/dashboard", { waitUntil: "networkidle" });
    await pg.waitForSelector("text=Set up alerts", { timeout: 8000 }).catch(() => undefined);
    check(`${tag}: dashboard asks to set up alerts when unlinked`, await pg.isVisible("main a[href='#/alerts']:has-text('Open alert settings')"));
    await hsOk("dashboard");
    await pg.click("main a:has-text('Open alert settings')");
    await pg.waitForFunction(() => location.hash === "#/alerts");
    await pg.waitForSelector("button:has-text('Link Telegram')", { timeout: 8000 }).catch(() => undefined);
    let t = await text();
    check(`${tag}: #/alerts unlinked state`, /Not linked/i.test(t) && /Not confirmed/i.test(t) && !/Alerts are set up/.test(t) && (await pg.isVisible("button:has-text('Link Telegram')")), t.slice(0, 160));
    check(`${tag}: #/alerts prefs with a locked mandatory alert`, (await pg.locator("main .check input[type=checkbox]:disabled").count()) === 1 && /Trade opened/.test(t));
    check(`${tag}: nav marks Alerts current`, (await pg.locator("#site-nav a[href='#/alerts'][aria-current=page]").count()) === 1);
    await hsOk("#/alerts (unlinked)");
    if (SHOTS) await pg.screenshot({ path: join(SHOTS, `${vp.name}-${scheme}-alerts.png`), fullPage: true });
    st.linked = true;
    await pg.reload({ waitUntil: "networkidle" });
    await pg.waitForSelector("text=Alerts are set up", { timeout: 8000 }).catch(() => undefined);
    t = await text();
    check(`${tag}: #/alerts linked state`, /Alerts are set up/.test(t) && /Linked/i.test(t) && !/Not linked/i.test(t) && /Confirmed/i.test(t) && !/Not confirmed/i.test(t) && !(await pg.isVisible("button:has-text('Link Telegram')")));
    await hsOk("#/alerts (linked)");
    await pg.goto(BASE + "#/dashboard/alerts", { waitUntil: "networkidle" });
    await pg.waitForTimeout(300);
    check(`${tag}: dashboard Alerts tab links to #/alerts`, (await pg.locator("#alert-settings-link[href='#/alerts']").count()) === 1);
    await pg.goto(BASE + "#/dashboard", { waitUntil: "networkidle" });
    await pg.waitForTimeout(400);
    check(`${tag}: no set-up note once linked`, !/Set up alerts/.test(await text()));

    // Leverage bound = min(strategy 20×, HL market 12×) — no platform/launch cap; stronger warning above 5×
    await pg.locator(`.rtable tr[data-key='${SUB2.id}'] button:has-text('Edit')`).click();
    await pg.waitForSelector("dialog #e-lev", { timeout: 8000 }).catch(() => undefined);
    let dlg = await pg.locator("dialog[open]").last().innerText().catch(() => "");
    check(`${tag}: edit leverage bounded by HL market max (12×)`, (await pg.locator("dialog #e-lev option").count()) === 12 && /Hyperliquid market maximum 12×/.test(dlg) && /strategy maximum 20×/.test(dlg) && !/launch/i.test(dlg), dlg.slice(0, 200));
    check(`${tag}: edit dialog keeps the lose-all warning`, /You can lose all allocated funds/i.test(dlg) && (await pg.locator("dialog .high-lev").count()) === 0);
    await pg.selectOption("dialog #e-lev", "8");
    check(`${tag}: edit >5× shows the high-leverage warning`, await pg.isVisible("dialog .high-lev"));
    await pg.locator("dialog[open] .dlg-x").last().click();
    await pg.waitForSelector("dialog", { state: "detached", timeout: 3000 }).catch(() => undefined);
    check(`${tag}: edit dialog closes`, (await pg.locator("dialog").count()) === 0);
    check(`${tag}: HL meta queried for BTC`, hlBodies.some((b) => b.type === "meta" && !b.dex));
    // Subscribe wizard step 7 (allocation & leverage), resumed from saved progress
    await pg.evaluate((st) => localStorage.setItem("aijalon.subwiz.fb-1.btc-momo", JSON.stringify({ v: 1, savedAt: Date.now(), ...st })), { gateAccepted: true, master: "0x5555555555555555555555555555555555555555", trading: "0x5555555555555555555555555555555555555555", tradingLabel: "Master account", agentDone: true, builderDone: true });
    await pg.goto(BASE + "#/subscribe/btc-momo", { waitUntil: "networkidle" });
    await pg.waitForSelector("#w-lev", { timeout: 8000 }).catch(() => undefined);
    await pg.waitForFunction(() => document.querySelectorAll("#w-lev option").length === 12, null, { timeout: 5000 }).catch(() => undefined);
    t = await text();
    check(`${tag}: wizard leverage = min(strategy, market), no launch cap text`, (await pg.locator("#w-lev option").count()) === 12 && /Hyperliquid market maximum 12×/.test(t) && !/launch cap|launch-phase/i.test(t), t.slice(0, 120));
    check(`${tag}: wizard keeps the lose-all warning`, /You can lose all allocated funds/i.test(t) && (await pg.locator("main .high-lev").count()) === 0);
    await pg.selectOption("#w-lev", "10");
    check(`${tag}: wizard >5× shows the high-leverage warning`, (await pg.isVisible("main .high-lev")) && /High leverage \(10×\)/.test(await pg.locator("main .high-lev").innerText()));
    await pg.selectOption("#w-lev", "5");
    check(`${tag}: wizard 5× has no high-leverage warning`, (await pg.locator("main .high-lev").count()) === 0);
    await hsOk("subscribe leverage step");
    if (SHOTS) await pg.screenshot({ path: join(SHOTS, `${vp.name}-${scheme}-leverage.png`), fullPage: true });
    // xyz:SILVER (builder dex) strategy max 2× < market 10× → 2 options
    await pg.evaluate((st) => localStorage.setItem("aijalon.subwiz.fb-1.silver", JSON.stringify({ v: 1, savedAt: Date.now(), ...st })), { gateAccepted: true, master: "0x5555555555555555555555555555555555555555", trading: "0x6666666666666666666666666666666666666666", tradingLabel: "Sub-account", agentDone: true, builderDone: true });
    await pg.goto(BASE + "#/subscribe/silver", { waitUntil: "networkidle" });
    await pg.waitForFunction(() => /Hyperliquid market maximum/.test(document.body.innerText), null, { timeout: 5000 }).catch(() => undefined);
    check(`${tag}: builder-dex market read from meta {dex:"xyz"}; strategy 2× is the bound`, (await pg.locator("#w-lev option").count()) === 2 && hlBodies.some((b) => b.type === "meta" && b.dex === "xyz") && /Hyperliquid market maximum 10×/.test(await text()));

    // Plans
    await pg.goto(BASE + "#/dashboard?tab=plan", { waitUntil: "networkidle" });
    await pg.waitForSelector(".plan-card", { timeout: 8000 }).catch(() => undefined);
    const cardsText = await pg.locator(".plan-card").allInnerTexts();
    check(`${tag}: three plans Free $0 / Pro $20 / Max $50`, cardsText.length === 3 && /Free[\s\S]*\$0/.test(cardsText[0]) && /Pro[\s\S]*\$20/.test(cardsText[1]) && /Max[\s\S]*\$50/.test(cardsText[2]), cardsText.map((c) => c.slice(0, 30)).join(" | "));
    check(`${tag}: alerts included on every plan`, cardsText.every((c) => /Telegram \+ email alerts/.test(c)));
    check(`${tag}: current plan marked`, /Current plan/i.test(cardsText[0] ?? "") && !/Current plan/i.test(cardsText[1] ?? ""));
    await hsOk("plans");
    if (SHOTS) await pg.screenshot({ path: join(SHOTS, `${vp.name}-${scheme}-plans.png`), fullPage: true });
    // Max ($50) with $25 balance → top-up prompt, nothing posted
    await pg.click(".plan-card[data-plan=max] button:has-text('Upgrade to Max')");
    await pg.waitForSelector("#plan-topup", { timeout: 5000 }).catch(() => undefined);
    check(`${tag}: insufficient balance → top-up prompt (no charge attempted)`, (await pg.isVisible("#plan-topup")) && planPosts.length === 0);
    await pg.click("dialog >> button:has-text('Top up balance')");
    await pg.waitForFunction(() => location.hash === "#/dashboard/balance", null, { timeout: 5000 }).catch(() => undefined);
    check(`${tag}: top-up prompt opens the fee balance tab`, (await pg.evaluate(() => location.hash)) === "#/dashboard/balance");
    // Pro ($20): confirm → step-up → POST /me/plan {plan:"pro"}
    await pg.goto(BASE + "#/dashboard?tab=plan", { waitUntil: "networkidle" });
    await pg.waitForSelector(".plan-card[data-plan=pro] button", { timeout: 8000 }).catch(() => undefined);
    await pg.click(".plan-card[data-plan=pro] button:has-text('Upgrade to Pro')");
    await pg.waitForSelector("dialog >> text=Switch to Pro?");
    check(`${tag}: plan confirm shows the charge`, (await pg.locator("dialog >> text=/\\$20\\.00 from your fee balance/").count()) >= 1);
    await pg.click("dialog >> button:has-text('Switch to Pro')");
    await pg.waitForSelector("dialog >> text=Confirm it's you", { timeout: 5000 }).catch(() => undefined);
    check(`${tag}: plan change asks for step-up before charging`, (await pg.isVisible("dialog >> text=Confirm it's you")) && planPosts.length === 0);
    await pg.click("dialog >> button:has-text('Continue with Google')");
    await pg.waitForSelector(".plan-card[data-plan=pro].current", { timeout: 8000 }).catch(() => undefined);
    check(`${tag}: POST /me/plan {plan:"pro"} with Idempotency-Key`, planPosts.length === 1 && planPosts[0].body.plan === "pro" && Object.keys(planPosts[0].body).length === 1 && /^[0-9a-f-]{36}$/.test(planPosts[0].key ?? ""), JSON.stringify(planPosts));
    check(`${tag}: Pro shown as current after the change`, /Current plan/i.test(await pg.locator(".plan-card[data-plan=pro]").innerText()));
    // Max with enough client-side balance but the server says 402 → same top-up prompt
    st.fee = 100000000;
    await pg.reload({ waitUntil: "networkidle" });
    await pg.waitForSelector(".plan-card[data-plan=max] button", { timeout: 8000 }).catch(() => undefined);
    await pg.click(".plan-card[data-plan=max] button:has-text('Upgrade to Max')");
    await pg.click("dialog >> button:has-text('Switch to Max')");
    await pg.waitForSelector("dialog >> text=Confirm it's you", { timeout: 5000 }).catch(() => undefined);
    await pg.click("dialog >> button:has-text('Continue with Google')");
    await pg.waitForSelector("#plan-topup", { timeout: 5000 }).catch(() => undefined);
    check(`${tag}: 402 insufficient_balance → top-up prompt`, (await pg.isVisible("#plan-topup")) && planPosts.length === 2 && st.plan === "pro");
    await pg.click("dialog >> button:has-text('Not now')");

    // Creator Studio: own posts + per-strategy earnings
    await pg.goto(BASE + "#/creator/posts", { waitUntil: "networkidle" });
    await pg.waitForSelector("text=Weekly notes", { timeout: 8000 }).catch(() => undefined);
    t = await text();
    check(`${tag}: creator lists own posts`, /Weekly notes: why we held cash/.test(t) && /Deep dive/.test(t) && (await pg.locator("main a[href='#/posts/p0000000-0000-4000-8000-000000000002']").count()) === 1 && /7 · \$35\.00/.test(t));
    await hsOk("creator posts");
    await pg.goto(BASE + "#/creator/earnings", { waitUntil: "networkidle" });
    await pg.waitForSelector(".earnings-by-strategy", { timeout: 8000 }).catch(() => undefined);
    t = await pg.locator(".earnings-by-strategy").innerText();
    check(`${tag}: per-strategy earnings breakdown`, /BTC Momentum 4h/.test(t) && /\$337\.56/.test(t) && /\$120\.00/.test(t) && /\$4\.20/.test(t) && /\$28\.00/.test(t) && /\$489\.76/.test(t) && /Posts without a strategy[\s\S]*\$9\.00/.test(t), t.slice(0, 200));
    const rowDisplay = await pg.locator(".earnings-by-strategy .rtable tbody tr").first().evaluate((el) => getComputedStyle(el).display);
    check(`${tag}: earnings ${vp.name === "mobile" ? "cards on mobile" : "table on desktop"}`, vp.name === "mobile" ? rowDisplay === "block" : rowDisplay === "table-row", rowDisplay);
    await hsOk("creator earnings");
    if (SHOTS) await pg.screenshot({ path: join(SHOTS, `${vp.name}-${scheme}-earnings.png`), fullPage: true });

    const bg = await pg.evaluate(() => getComputedStyle(document.body).backgroundColor);
    check(`${tag}: ${scheme} theme applied`, scheme === "dark" ? bg === "rgb(20, 17, 14)" : bg === "rgb(246, 244, 240)", bg);
    check(`${tag}: every API call was a mocked backend route`, unknown.length === 0, unknown.join(", "));
    // The mocked 402 is an expected server response (Chromium logs it as "Failed to load resource"); nothing else may log.
    const unexpected = errors.filter((e) => !(/status of 402/.test(e) && /\/v1\/me\/plan$/.test(e)));
    check(`${tag}: no console errors`, unexpected.length === 0, unexpected.slice(0, 5).join(" | "));
    await context.close();
  }
}

await browser.close();
server.close();
console.log(results.join("\n"));
console.log(`\n${results.length - failures}/${results.length} checks passed`);
process.exit(failures ? 1 : 0);

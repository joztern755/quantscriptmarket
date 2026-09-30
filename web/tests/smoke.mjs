#!/usr/bin/env node
// Playwright smoke test for the built SPA (web/dist). No npm deps: uses the globally installed Playwright.
//   node web/build.mjs && node web/tests/smoke.mjs [--shots <dir>]
// Serves dist on a random port, mocks the API + third-party hosts, and checks at 1920×1080 and 390×844,
// light + dark: entry gate first, cannot proceed until every box is ticked, no console errors, no
// horizontal scroll, legal pages readable before entry, theme toggle, gate re-shown on version change,
// protected routes redirect to sign-in, Firebase loads from the pinned gstatic URL (stubbed).

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

const LEGAL = { terms: "2026-09-30", risk: "2026-09-30", privacy: "2026-09-30", waiver: "2026-09-30", jurisdiction: "2026-09-30", subscription_ack: "2026-09-30" };
function publicConfig(overrides = {}) {
  return {
    builder_address: "0x1111111111111111111111111111111111111111",
    treasury_address: "0x2222222222222222222222222222222222222222",
    agent_name: "aijalon",
    hl_chain: "Mainnet",
    stripe_publishable_key: null,
    stripe_fee_estimate: null,
    restricted_jurisdictions: ["US", "CU", "IR", "KP", "SY", "RU", "BY", "MM"],
    legal_versions: { ...LEGAL, ...(overrides.legal_versions || {}) },
    economics: { builder_fee_tenths_bp: 100, profit_share_creator_cap_bps: 1200, platform_profit_share_bps: 150, platform_profit_share_mode: "on_top", subscription_platform_bps: 300, min_topup_micro: 10000000, past_due_grace_hours: 72 },
    plans: [],
    features: { creator_uploads: true },
  };
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
    if (m.type() === "error") errors.push(`console: ${m.text()}`);
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
    if (u.endsWith("/firebase-auth.js")) return r.fulfill({ status: 200, contentType: "text/javascript", body: FB_AUTH_STUB, headers: { "access-control-allow-origin": "*" } });
    return r.fulfill({ status: 404, body: "" });
  });
  await context.route("https://api.aijalon.trade/**", (r) => {
    const u = new URL(r.request().url());
    const cors = { "access-control-allow-origin": "*", "access-control-allow-headers": "authorization, content-type, idempotency-key", "access-control-allow-methods": "GET, POST, PATCH, DELETE" };
    if (r.request().method() === "OPTIONS") return r.fulfill({ status: 204, headers: cors });
    if (u.pathname === "/v1/public/config") return r.fulfill({ status: 200, contentType: "application/json", headers: cors, body: JSON.stringify(cfgState) });
    if (u.pathname === "/v1/public/strategies") return r.fulfill({ status: 200, contentType: "application/json", headers: cors, body: JSON.stringify({ items: [], strategies: [] }) });
    if (u.pathname.startsWith("/v1/public/")) return r.fulfill({ status: 200, contentType: "application/json", headers: cors, body: JSON.stringify({ items: [] }) });
    return r.fulfill({ status: 401, contentType: "application/json", headers: cors, body: JSON.stringify({ error: { code: "unauthorized", message: "sign in" } }) });
  });
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
      return r.width > 0 && r.right > window.innerWidth + 1 && getComputedStyle(el).position !== "fixed";
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

    // Other routes
    for (const route of ["#/market", "#/leaderboard", "#/posts", "#/legal/risk-disclosure", "#/signin", "#/nope"]) {
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

await browser.close();
server.close();
console.log(results.join("\n"));
console.log(`\n${results.length - failures}/${results.length} checks passed`);
process.exit(failures ? 1 : 0);

#!/usr/bin/env node
// Firebase Hosting header emulation + framing test (SECURITY M1).
//   node web/build.mjs && node web/tests/headers.test.mjs
//
// 1. Static: applies firebase.json `headers` rules (glob `source` + RE2-style `regex`, every matching rule merges,
//    later rules override the same key — Firebase's documented behaviour) to a list of paths and asserts that
//    CSP (frame-ancestors 'none') + X-Frame-Options DENY are on EVERY path except Firebase's own /__/auth/*.
//    It also proves the regression: the previous regex left /__x (and every /__<anything>) unprotected.
// 2. Dynamic (Playwright/Chromium, skipped if Playwright is absent): serves web/dist exactly like Hosting would
//    (the `**` → /index.html rewrite + the emulated headers) and frames it from a DIFFERENT origin:
//      - /__x#/market and / are blocked by the headers;
//      - a path served WITHOUT headers (simulating a header gap) is neutralised by public/boot-guard.js + main.ts:
//        the framed document is hidden and the app never boots.
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const DIST = join(ROOT, "web", "dist");
const cfg = JSON.parse(readFileSync(join(ROOT, "firebase.json"), "utf8")).hosting;

let pass = 0;
let fail = 0;
function check(name, ok, detail = "") {
  ok ? pass++ : fail++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}

/** Firebase `source` glob → RegExp (the subset firebase.json uses: **, *, @(a|b), literal chars). */
export function globToRegExp(glob) {
  let re = "";
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === "*" && glob[i + 1] === "*") {
      re += ".*";
      i++;
      if (glob[i + 1] === "/") i++; // "**/" also matches zero directories
    } else if (c === "*") re += "[^/]*";
    else if (c === "@" && glob[i + 1] === "(") {
      const end = glob.indexOf(")", i);
      re += "(" + glob.slice(i + 2, end).split("|").map((x) => x.replace(/[.+^${}()|[\]\\?]/g, "\\$&")).join("|") + ")";
      i = end;
    } else re += c.replace(/[.+^${}()|[\]\\?]/g, "\\$&");
  }
  return new RegExp("^" + (glob.startsWith("/") || glob.startsWith("**") ? "" : "/") + re + "$");
}

export function headersFor(path) {
  const out = {};
  for (const rule of cfg.headers) {
    const re = rule.regex ? new RegExp(rule.regex) : globToRegExp(rule.source);
    if (re.test(path)) for (const h of rule.headers) out[h.key.toLowerCase()] = h.value;
  }
  return out;
}

// ---------------------------------------------------------------------------------------------- 1. static
const PROTECTED = ["/", "/index.html", "/__x", "/__", "/_", "/_x", "/__/", "/__/x", "/__/authx", "/__/aut", "/__/auth",
  "/__/firebase/init.json", "/s/x", "/app/main.js", "/legal/terms.md", "/app-config.json", "/.well-known/security.txt", "/a/__/auth/handler"];
const FIREBASE_OWN = ["/__/auth/handler", "/__/auth/iframe", "/__/auth/", "/__/auth/links?x=1"];
for (const p of PROTECTED) {
  const h = headersFor(p.split("?")[0]);
  check(`headers on ${p}`, h["x-frame-options"] === "DENY" && /frame-ancestors 'none'/.test(h["content-security-policy"] ?? "") && /report-to csp/.test(h["content-security-policy"] ?? "") && /csp=/.test(h["reporting-endpoints"] ?? ""), JSON.stringify({ xfo: h["x-frame-options"] }));
}
for (const p of FIREBASE_OWN) {
  const h = headersFor(p.split("?")[0]);
  check(`no document headers on Firebase's own ${p}`, !h["x-frame-options"] && !h["content-security-policy"] && Boolean(h["strict-transport-security"]));
}
const OLD = new RegExp("^/([^_].*|_([^_].*)?)?$");
check("regression: the old regex left /__x without XFO/CSP", !OLD.test("/__x") && !OLD.test("/__") && OLD.test("/"));

// --------------------------------------------------------------------------------------------- 2. dynamic
const require = createRequire(import.meta.url);
let chromium = null;
for (const p of ["/opt/node22/lib/node_modules/playwright", "playwright"]) {
  try {
    ({ chromium } = require(p));
    break;
  } catch { /* next */ }
}
if (!chromium || !existsSync(join(DIST, "index.html"))) {
  console.log(`SKIP dynamic framing test (${!chromium ? "playwright missing" : "web/dist missing — run node web/build.mjs"})`);
} else {
  process.env.PLAYWRIGHT_BROWSERS_PATH ||= "/opt/pw-browsers";
  const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css", ".json": "application/json", ".md": "text/markdown", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon" };
  // Hosting emulation. /nohdrx = a simulated header gap (index.html served with NO document headers).
  const site = createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const gap = url.pathname === "/nohdrx"; // like /__x: no trailing slash, so relative assets resolve to /
    let file = join(DIST, normalize(decodeURIComponent(url.pathname)).replace(/^(\.\.[/\\])+/, ""));
    if (!file.startsWith(DIST) || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, "index.html"); // "**" rewrite
    const hdrs = gap ? {} : headersFor(url.pathname);
    delete hdrs["content-security-policy"]; // meta CSP still applies; the header CSP would upgrade http→https here
    delete hdrs["strict-transport-security"];
    // keep frame-ancestors via a minimal header CSP (the part under test)
    if (!gap && headersFor(url.pathname)["content-security-policy"]) hdrs["content-security-policy"] = "frame-ancestors 'none'";
    res.writeHead(200, { "content-type": TYPES[extname(file)] || "application/octet-stream", ...hdrs });
    res.end(readFileSync(file));
  });
  await new Promise((r) => site.listen(0, "127.0.0.1", r));
  const SITE = `http://127.0.0.1:${site.address().port}`;
  const evil = createServer((req, res) => {
    const target = new URL(req.url, "http://x").searchParams.get("t") ?? "/";
    res.writeHead(200, { "content-type": "text/html" });
    res.end(`<!doctype html><title>evil</title><iframe id="f" src="${SITE}${target}" width="800" height="600"></iframe>`);
  });
  await new Promise((r) => evil.listen(0, "localhost", r)); // different host name = different origin
  const EVIL = `http://localhost:${evil.address().port}`;

  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on("pageerror", () => {});
  const frameState = async (target) => {
    await page.goto(`${EVIL}/?t=${encodeURIComponent(target)}`, { waitUntil: "load" });
    await page.waitForTimeout(1500);
    const f = page.frames().find((x) => x !== page.mainFrame());
    if (!f || !f.url().startsWith(SITE)) return { loaded: false, url: f?.url() ?? "" };
    try {
      return await f.evaluate(() => ({
        loaded: true,
        hidden: getComputedStyle(document.documentElement).display === "none",
        booted: Boolean(document.querySelector("main .page, .gate, header.top, nav")),
        framedFlag: Boolean(window.__aijFramed),
      }));
    } catch (e) {
      return { loaded: false, url: f.url(), err: String(e).slice(0, 80) };
    }
  };
  for (const t of ["/__x#/market", "/#/market", "/s/x#/market"]) {
    const st = await frameState(t);
    check(`framing ${t} from another origin is blocked by the headers`, !st.loaded || st.hidden, JSON.stringify(st));
  }
  const gap = await frameState("/nohdrx#/market");
  check("header gap: framed SPA is hidden by boot-guard.js and never boots", gap.loaded && gap.hidden && gap.framedFlag && !gap.booted, JSON.stringify(gap));
  // top-level still works (the guard must not break the real site)
  await page.goto(`${SITE}/#/market`, { waitUntil: "load" });
  await page.waitForTimeout(800);
  const top = await page.evaluate(() => ({ hidden: getComputedStyle(document.documentElement).display === "none", flag: Boolean(window.__aijFramed), tt: Boolean(window.trustedTypes && window.trustedTypes.defaultPolicy) }));
  check("top-level document is not hidden and installs the Trusted Types default policy", !top.hidden && !top.flag && top.tt, JSON.stringify(top));
  await browser.close();
  site.close();
  evil.close();
}

console.log(`\n${pass}/${pass + fail} header checks passed`);
process.exit(fail ? 1 : 0);

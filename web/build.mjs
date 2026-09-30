#!/usr/bin/env node
// Build: tsc (web/tsconfig.json) → dist/app, copy public/ + styles/ + page CSS, render index.html with
// the Content-Security-Policy (meta) and cache-busting query strings, emit dist/csp.txt + dist/headers.json
// (for firebase.json hosting headers). No npm dependencies.
//
//   node web/build.mjs            production build
//   APP_CONFIG=path.json node web/build.mjs   use another app-config.json (e.g. staging/local)

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = dirname(fileURLToPath(import.meta.url));
const DIST = join(WEB, "dist");
const t0 = Date.now();

function log(msg) {
  process.stdout.write(`[build] ${msg}\n`);
}
function fail(msg) {
  process.stderr.write(`[build] ERROR: ${msg}\n`);
  process.exit(1);
}

// 1. clean
rmSync(DIST, { recursive: true, force: true });
mkdirSync(DIST, { recursive: true });

// 2. TypeScript
function findTsc() {
  const which = spawnSync(process.platform === "win32" ? "where" : "which", ["tsc"], { encoding: "utf8" });
  if (which.status === 0 && which.stdout.trim()) return { cmd: which.stdout.trim().split("\n")[0], args: [] };
  for (const p of ["/opt/node22/lib/node_modules/typescript/bin/tsc", join(WEB, "node_modules/typescript/bin/tsc")]) {
    if (existsSync(p)) return { cmd: process.execPath, args: [p] };
  }
  fail("tsc not found (install TypeScript globally)");
}
const tsc = findTsc();
const res = spawnSync(tsc.cmd, [...tsc.args, "-p", join(WEB, "tsconfig.json"), "--pretty", "false"], { encoding: "utf8" });
if (res.status !== 0) {
  process.stderr.write(res.stdout + res.stderr);
  fail("TypeScript compilation failed (see errors above)");
}
log("tsc ok");

// 3. static files
cpSync(join(WEB, "public"), DIST, { recursive: true });
cpSync(join(WEB, "styles"), join(DIST, "styles"), { recursive: true });
// page CSS (web/src/pages/**/*.css → dist/app/pages/**)
function walk(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}
for (const f of walk(join(WEB, "src"))) {
  if (f.endsWith(".css")) {
    const dest = join(DIST, "app", relative(join(WEB, "src"), f));
    mkdirSync(dirname(dest), { recursive: true });
    cpSync(f, dest);
  }
}
if (process.env.APP_CONFIG) {
  cpSync(process.env.APP_CONFIG, join(DIST, "app-config.json"));
  log(`app-config from ${process.env.APP_CONFIG}`);
}

// 3b. legal drafts (repo legal/*.md → dist/legal/) + fallback versions from their "Version:" lines
const LEGAL_SRC = join(WEB, "..", "legal");
const DOC_FILES = { terms: "terms", risk: "risk-disclosure", privacy: "privacy", waiver: "liability-waiver", jurisdiction: "jurisdiction", creator_agreement: "creator-agreement", subscription_ack: "subscription-ack" };
const ALIASES = { "waiver.md": "liability-waiver.md", "restricted-jurisdictions.md": "jurisdiction.md" };
const legalVersions = {};
if (existsSync(LEGAL_SRC)) {
  mkdirSync(join(DIST, "legal"), { recursive: true });
  for (const f of readdirSync(LEGAL_SRC)) {
    if (!f.endsWith(".md") || f.toLowerCase() === "readme.md") continue;
    cpSync(join(LEGAL_SRC, f), join(DIST, "legal", f));
  }
  for (const [alias, src] of Object.entries(ALIASES)) if (existsSync(join(LEGAL_SRC, src))) cpSync(join(LEGAL_SRC, src), join(DIST, "legal", alias));
  for (const [doc, file] of Object.entries(DOC_FILES)) {
    const p = join(LEGAL_SRC, `${file}.md`);
    if (!existsSync(p)) continue;
    const m = /^Version:\s*(\S+)/m.exec(readFileSync(p, "utf8"));
    if (m) legalVersions[doc] = m[1];
  }
  log(`legal docs copied (versions: ${JSON.stringify(legalVersions)})`);
} else {
  log("WARN: ../legal not found — legal pages will 404");
}

// 4. CSP
const appCfg = JSON.parse(readFileSync(join(DIST, "app-config.json"), "utf8"));
appCfg.fallback = appCfg.fallback || {};
appCfg.fallback.legal_versions = { ...(appCfg.fallback.legal_versions || {}), ...legalVersions };
writeFileSync(join(DIST, "app-config.json"), JSON.stringify(appCfg, null, 2) + "\n");
const apiOrigin = String(appCfg.apiOrigin || "https://api.aijalon.trade").replace(/\/+$/, "");
const hlOrigin = String(appCfg.hlApiUrl || "https://api.hyperliquid.xyz").replace(/\/+$/, "");
const authDomain = String(appCfg.firebase?.authDomain || "");
const fbVersion = String(appCfg.firebaseSdkVersion || "");
if (!/^\d+\.\d+\.\d+$/.test(fbVersion)) fail("app-config.json firebaseSdkVersion must be x.y.z");

// Optional SRI for the Firebase ESM files: web/sri.json {"firebase-app.js":"sha384-…","firebase-auth.js":"sha384-…"}
// Import maps with "integrity" are applied to dynamic import() (Chrome 127+, Safari 18+, Firefox 138+).
let importMap = null;
const sriPath = join(WEB, "sri.json");
if (existsSync(sriPath)) {
  const sri = JSON.parse(readFileSync(sriPath, "utf8"));
  const base = `https://www.gstatic.com/firebasejs/${fbVersion}/`;
  const integrity = {};
  for (const f of ["firebase-app.js", "firebase-auth.js"]) {
    if (!/^sha(256|384|512)-[A-Za-z0-9+/=]+$/.test(sri[f] || "")) fail(`sri.json: missing/invalid hash for ${f}`);
    integrity[base + f] = sri[f];
  }
  if (sri.version && sri.version !== fbVersion) fail(`sri.json is for Firebase ${sri.version}, app-config pins ${fbVersion}`);
  importMap = JSON.stringify({ imports: {}, integrity });
  log("Firebase SRI: import map with integrity");
} else {
  log("WARN: web/sri.json absent — Firebase modules load WITHOUT SRI (see docs/WEB_CORE_API.md)");
}

const authOrigins = new Set();
if (authDomain) authOrigins.add(`https://${authDomain}`);
if (appCfg.firebase?.projectId && !/REPLACE_ME/.test(appCfg.firebase.projectId)) authOrigins.add(`https://${appCfg.firebase.projectId}.firebaseapp.com`);

const importMapHash = importMap ? `'sha256-${createHash("sha256").update(importMap).digest("base64")}'` : null;
const directives = {
  "default-src": ["'self'"],
  "script-src": ["'self'", "https://www.gstatic.com", "https://apis.google.com", "https://js.stripe.com", "https://*.js.stripe.com", ...(importMapHash ? [importMapHash] : [])],
  "style-src": ["'self'", "https://fonts.googleapis.com"],
  "font-src": ["'self'", "https://fonts.gstatic.com"],
  "img-src": ["'self'", "data:", "https://*.stripe.com", "https://lh3.googleusercontent.com"],
  "connect-src": ["'self'", apiOrigin, hlOrigin, "https://identitytoolkit.googleapis.com", "https://securetoken.googleapis.com", "https://www.googleapis.com", "https://api.stripe.com"],
  "frame-src": [...authOrigins, "https://apis.google.com", "https://js.stripe.com", "https://*.js.stripe.com", "https://hooks.stripe.com"],
  "object-src": ["'none'"],
  "base-uri": ["'none'"],
  "form-action": ["'self'"],
  "manifest-src": ["'self'"],
  "worker-src": ["'none'"],
};
const cspMeta = Object.entries(directives).map(([k, v]) => `${k} ${[...new Set(v)].join(" ")}`).join("; ");
const cspHeader = `${cspMeta}; frame-ancestors 'none'; upgrade-insecure-requests`;
writeFileSync(join(DIST, "csp.txt"), cspHeader + "\n");
writeFileSync(join(DIST, "headers.json"), JSON.stringify({
  "Content-Security-Policy": cspHeader,
  "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(self \"https://js.stripe.com\")",
  "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
}, null, 2) + "\n");

// 5. index.html
function fileHash(p) {
  return createHash("sha256").update(readFileSync(p)).digest("hex").slice(0, 10);
}
const buildHash = createHash("sha256");
for (const p of walk(join(DIST, "app")).concat(walk(join(DIST, "styles"))).sort()) buildHash.update(relative(DIST, p)).update(readFileSync(p));
const buildId = buildHash.digest("hex").slice(0, 12);
let html = readFileSync(join(WEB, "index.html"), "utf8");
const escAttr = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
html = html
  .replace("%%CSP%%", escAttr(cspMeta))
  .replace("%%APP_CSS%%", `styles/app.css?v=${fileHash(join(DIST, "styles/app.css"))}`)
  .replace("%%MAIN_JS%%", `app/main.js?v=${buildId}`)
  .replace("%%BUILD_ID%%", buildId)
  .replace("%%IMPORT_MAP%%", importMap ? `<script type="importmap">${importMap}</script>` : "");
if (/%%[A-Z_]+%%/.test(html)) fail("index.html has unreplaced placeholders");
writeFileSync(join(DIST, "index.html"), html);
writeFileSync(join(DIST, "build-info.json"), JSON.stringify({ build: buildId, firebase: fbVersion, built_at: new Date().toISOString() }) + "\n");

// 6. sanity: no innerHTML sinks in emitted JS (XSS guard for the whole app, pages included)
const sinks = [];
for (const f of walk(join(DIST, "app"))) {
  if (!f.endsWith(".js")) continue;
  const src = readFileSync(f, "utf8");
  if (/\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML\s*\(|document\.write\s*\(|\beval\s*\(|new Function\s*\(/.test(src)) sinks.push(relative(DIST, f));
}
if (sinks.length) fail(`unsafe DOM/eval sinks found in: ${sinks.join(", ")}`);

log(`dist ready in ${Date.now() - t0} ms (build ${buildId})`);

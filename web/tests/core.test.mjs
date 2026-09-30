#!/usr/bin/env node
// Unit tests for pure core modules (no browser): format, keccak/EIP-55, QR encoder, Hyperliquid builders.
//   node web/build.mjs && node web/tests/core.test.mjs
import { cpSync, mkdtempSync, writeFileSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const DIST = join(dirname(fileURLToPath(import.meta.url)), "..", "dist", "app", "core");
if (!existsSync(DIST)) {
  console.error("run node web/build.mjs first");
  process.exit(2);
}
// dist .js files are ES modules; copy them next to a package.json {"type":"module"} for Node.
const tmp = mkdtempSync(join(tmpdir(), "aij-core-"));
cpSync(DIST, join(tmp, "core"), { recursive: true });
writeFileSync(join(tmp, "package.json"), '{"type":"module"}');
globalThis.window = globalThis;
const imp = (m) => import(pathToFileURL(join(tmp, "core", m)).href);

let pass = 0, fail = 0;
function eq(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  ok ? pass++ : fail++;
  if (!ok) console.log(`FAIL ${name}: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
}
function throws(name, fn) {
  try {
    fn();
    fail++;
    console.log(`FAIL ${name}: did not throw`);
  } catch {
    pass++;
  }
}

// ---- format
const f = await imp("format.js");
eq("fmtUsd", f.fmtUsd(1234560000), "$1,234.56");
eq("fmtUsd truncates", f.fmtUsd(1999999), "$1.99");
eq("fmtUsd neg", f.fmtUsd(-5000000), "−$5.00");
eq("fmtUsd sign", f.fmtUsd(10000, { sign: true }), "+$0.01");
eq("fmtUsd bigint", f.fmtUsd(12345678901234567890n), "$12,345,678,901,234.56");
eq("fmtUsd compact", f.fmtUsd(1_250_000_000_000, { compact: true }), "$1.2M");
eq("fmtUsd null", f.fmtUsd(null), "—");
eq("fmtUsd float rejected", f.fmtUsd(1.5), "—");
eq("microToDecimal", f.microToDecimal(1234567), "1.234567");
eq("microToDecimal whole", f.microToDecimal(25000000), "25");
eq("parseUsd", String(f.parseUsdToMicro("12.34")), "12340000");
eq("parseUsd $,", String(f.parseUsdToMicro("$1,200.5")), "1200500000");
eq("parseUsd too precise", f.parseUsdToMicro("1.1234567"), null);
eq("parseUsd negative", f.parseUsdToMicro("-1"), null);
eq("fmtBps", f.fmtBps(150), "1.5%");
eq("fmtBps 1200", f.fmtBps(1200), "12%");
eq("fmtTenthsBp", f.fmtTenthsBp(100), "0.1%");
eq("fmtPct", f.fmtPct(12.3456), "12.35%");
eq("fmtLeverage", f.fmtLeverage(150), "1.5×");
eq("shortAddr", f.shortAddr("0x1234567890abcdef1234567890abcdef12345678"), "0x1234…5678");
eq("fmtDate", f.fmtDate("2026-09-30T23:00:00Z"), "30 Sep 2026");

// ---- keccak / EIP-55
const k = await imp("keccak.js");
eq("keccak('')", k.toHex(k.keccak256("")), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470");
eq("keccak('abc')", k.toHex(k.keccak256("abc")), "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45");
for (const a of ["0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359", "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB", "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb"]) {
  eq(`EIP-55 ${a}`, k.toChecksumAddress(a.toLowerCase()), a);
}

// ---- QR vs an independent reference encoder (npm's bundled qrcode-terminal vendor QRCode)
const require = createRequire(import.meta.url);
const REF = "/opt/node22/lib/node_modules/npm/node_modules/qrcode-terminal/vendor/QRCode";
const q = await imp("qr.js");
if (existsSync(REF)) {
  const QRCode = require(`${REF}/index.js`);
  const L = require(`${REF}/QRErrorCorrectLevel.js`);
  const texts = ["otpauth://totp/aijalon.trade:user%40example.com?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=aijalon.trade&algorithm=SHA1&digits=6&period=30"];
  for (let len = 1; len < 300; len += 11) texts.push(Array.from({ length: len }, (_, i) => String.fromCharCode(33 + ((i * 7 + len) % 90))).join(""));
  let compared = 0, mism = 0;
  for (const t of texts) for (const e of ["L", "M", "Q", "H"]) {
    const auto = q.qrMatrix(t, e);
    if (auto.version === 15 && e === "H") continue; // reference table bug at 15-H ([11,36,12] lacks the 7×(37,13) group)
    for (const mask of [0, 2, 4, 6, auto.mask]) {
      const mine = q.qrMatrix(t, e, { version: auto.version, mask });
      const ref = new QRCode(auto.version, L[e]);
      ref.addData(t);
      ref.makeImpl(false, mask);
      compared++;
      let diff = ref.getModuleCount() !== mine.size;
      for (let y = 0; !diff && y < mine.size; y++) for (let x = 0; x < mine.size; x++) if (ref.isDark(y, x) !== mine.modules[y][x]) { diff = true; break; }
      if (diff) mism++;
    }
  }
  eq(`QR matches reference (${compared} symbols)`, mism, 0);
} else {
  console.log("SKIP QR reference comparison (reference encoder not found)");
}

// ---- Hyperliquid
const hl = await imp("hl.js");
const agent = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01";
const nonce = 1790000000000;
const a = hl.buildApproveAgent({ agentAddress: agent, agentName: "aijalon", nonce, signatureChainId: "0xa4b1", hyperliquidChain: "Mainnet" });
eq("approveAgent key order (Python SDK)", Object.keys(a.action), ["type", "agentAddress", "agentName", "nonce", "signatureChainId", "hyperliquidChain"]);
eq("approveAgent primaryType", a.typedData.primaryType, "HyperliquidTransaction:ApproveAgent");
eq("approveAgent domain", a.typedData.domain, { name: "HyperliquidSignTransaction", version: "1", chainId: 42161, verifyingContract: "0x0000000000000000000000000000000000000000" });
eq("approveAgent types", a.typedData.types["HyperliquidTransaction:ApproveAgent"].map((x) => `${x.name}:${x.type}`), ["hyperliquidChain:string", "agentAddress:address", "agentName:string", "nonce:uint64"]);
eq("approveAgent lower-cases address", a.action.agentAddress, agent.toLowerCase());
const b = hl.buildApproveBuilderFee({ builder: "0x1111111111111111111111111111111111111111", maxFeeRate: "0.1%", nonce, signatureChainId: "0x1", hyperliquidChain: "Mainnet" });
eq("builderFee key order", Object.keys(b.action), ["maxFeeRate", "builder", "nonce", "type", "signatureChainId", "hyperliquidChain"]);
eq("builderFee types", b.typedData.types["HyperliquidTransaction:ApproveBuilderFee"].map((x) => x.name), ["hyperliquidChain", "maxFeeRate", "builder", "nonce"]);
const u = hl.buildUsdSend({ destination: "0x2222222222222222222222222222222222222222", amount: "25.5", time: nonce, signatureChainId: "0x66eee", hyperliquidChain: "Mainnet" });
eq("usdSend key order", Object.keys(u.action), ["destination", "amount", "time", "type", "signatureChainId", "hyperliquidChain"]);
eq("usdSend nonce = time", u.nonce, nonce);
eq("usdSend domain chainId", u.typedData.domain.chainId, 0x66eee);
throws("usdSend rejects 0", () => hl.buildUsdSend({ destination: "0x2222222222222222222222222222222222222222", amount: "0", time: nonce, signatureChainId: "0x1", hyperliquidChain: "Mainnet" }));
throws("usdSend rejects 7 decimals", () => hl.buildUsdSend({ destination: "0x2222222222222222222222222222222222222222", amount: "1.0000001", time: nonce, signatureChainId: "0x1", hyperliquidChain: "Mainnet" }));
eq("maxFeeRate 100", hl.maxFeeRateFromTenthsBp(100), "0.1%");
eq("maxFeeRate 1", hl.maxFeeRateFromTenthsBp(1), "0.001%");
eq("maxFeeRate 25", hl.maxFeeRateFromTenthsBp(25), "0.025%");
eq("parse 0.1%", hl.tenthsBpFromMaxFeeRate("0.1%"), 100);
eq("parse 0.0101% rounds up", hl.tenthsBpFromMaxFeeRate("0.0101%"), 11);
eq("parse junk", hl.tenthsBpFromMaxFeeRate("1"), null);
const sig = "0x" + "11".repeat(32) + "22".repeat(32) + "1b";
eq("splitSignature", hl.splitSignature(sig), { r: "0x" + "11".repeat(32), s: "0x" + "22".repeat(32), v: 27 });
eq("splitSignature v=0 → 27", hl.splitSignature("0x" + "11".repeat(64) + "00").v, 27);
eq("splitSignature v=1 → 28", hl.splitSignature("0x" + "11".repeat(64) + "01").v, 28);
throws("splitSignature bad", () => hl.splitSignature("0x1234"));

// server typed-data validation
const expA = { hyperliquidChain: "Mainnet", agentAddress: agent, agentName: "aijalon" };
eq("validate ok", hl.validateServerTypedData("approveAgent", a.typedData, expA).nonce, nonce);
const clone = (x) => JSON.parse(JSON.stringify(x));
let bad = clone(a.typedData); bad.message.agentAddress = "0x9999999999999999999999999999999999999999";
throws("validate wrong agent", () => hl.validateServerTypedData("approveAgent", bad, expA));
bad = clone(a.typedData); bad.primaryType = "HyperliquidTransaction:UsdSend";
throws("validate wrong primaryType", () => hl.validateServerTypedData("approveAgent", bad, expA));
bad = clone(a.typedData); bad.domain.name = "Other";
throws("validate wrong domain", () => hl.validateServerTypedData("approveAgent", bad, expA));
bad = clone(a.typedData); bad.types["HyperliquidTransaction:ApproveAgent"].push({ name: "extra", type: "string" });
throws("validate extra field", () => hl.validateServerTypedData("approveAgent", bad, expA));
bad = clone(a.typedData); bad.message.hyperliquidChain = "Testnet";
throws("validate wrong chain", () => hl.validateServerTypedData("approveAgent", bad, expA));
const expB = { hyperliquidChain: "Mainnet", builder: "0x1111111111111111111111111111111111111111", maxFeeTenthsBp: 100 };
eq("validate builder ok", hl.validateServerTypedData("approveBuilderFee", b.typedData, expB).nonce, nonce);
bad = clone(b.typedData); bad.message.maxFeeRate = "0.2%";
throws("validate fee above cap", () => hl.validateServerTypedData("approveBuilderFee", bad, expB));
bad = clone(b.typedData); bad.message.builder = "0x3333333333333333333333333333333333333333";
throws("validate wrong builder", () => hl.validateServerTypedData("approveBuilderFee", bad, expB));
throws("validate builder w/o config", () => hl.validateServerTypedData("approveBuilderFee", b.typedData, { ...expB, builder: "" }));
const expU = { hyperliquidChain: "Mainnet", destination: "0x2222222222222222222222222222222222222222", amountMicro: 25_500_000 };
eq("validate usdSend ok", hl.validateServerTypedData("usdSend", u.typedData, expU).nonce, nonce);
throws("validate usdSend amount", () => hl.validateServerTypedData("usdSend", u.typedData, { ...expU, amountMicro: 25_000_000 }));
throws("validate usdSend dest", () => hl.validateServerTypedData("usdSend", u.typedData, { ...expU, destination: "0x4444444444444444444444444444444444444444" }));

// ---- /exchange: direct POST first, relay fallback on network/CORS failure (user-signed actions only)
{
  const apiMod = await imp("api.js");
  apiMod.setApiHooks({ getIdToken: async () => "test-token" });
  const realFetch = globalThis.fetch;
  const calls = [];
  const jsonRes = (status, body) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  let mode = "direct-ok";
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/exchange")) {
      if (mode === "direct-ok") return jsonRes(200, { status: "ok", response: { type: "default" } });
      if (mode === "direct-422") return new Response("Failed to deserialize the JSON body", { status: 422 });
      throw new TypeError("Failed to fetch");               // CORS / network: no response at all
    }
    if (String(url).endsWith("/v1/hl/exchange-relay")) {
      if (mode === "relay-down") throw new TypeError("Failed to fetch");
      if (mode === "relay-nonce") return jsonRes(200, { upstream_status: 200, response: { status: "err", response: "Invalid nonce: duplicate nonce" } });
      return jsonRes(200, { upstream_status: 200, response: { status: "ok", response: { type: "default" } } });
    }
    throw new Error("unexpected fetch " + url);
  };
  const body = { action: u.action, nonce: u.nonce, signature: { r: "0x" + "11".repeat(32), s: "0x" + "22".repeat(32), v: 27 } };
  try {
    let r = await hl.postExchange(body);
    eq("direct ok", [r.ok, calls.length, calls[0].url], [true, 1, "https://api.hyperliquid.xyz/exchange"]);
    eq("direct omits credentials", calls[0].init.credentials, "omit");

    calls.length = 0; mode = "direct-422";
    r = await hl.postExchange(body);
    eq("HTTP error is an answer: no relay", [r.ok, calls.length], [false, 1]);

    calls.length = 0; mode = "cors";
    r = await hl.postExchange(body);
    eq("network failure → relay ok", [r.ok, calls.length, calls[1].url], [true, 2, "https://api.aijalon.trade/v1/hl/exchange-relay"]);
    eq("relay gets the SAME body", JSON.parse(calls[1].init.body), JSON.parse(calls[0].init.body));
    eq("relay is authenticated", calls[1].init.headers.Authorization, "Bearer test-token");

    calls.length = 0; mode = "relay-nonce";
    r = await hl.postExchange(body);
    eq("relayed duplicate nonce explained", [r.ok, /may already have gone through/.test(r.error)], [false, true]);

    calls.length = 0; mode = "relay-down";
    let code = null;
    try { await hl.postExchange(body); } catch (e) { code = e.code; }
    eq("relay down → network_error", code, "network_error");

    calls.length = 0; mode = "cors";
    code = null;
    try { await hl.postExchange({ ...body, action: { type: "order", orders: [] } }); } catch (e) { code = e.code; }
    eq("orders are never relayed", [code, calls.length], ["network_error", 1]);
    eq("relayable list", hl.RELAYABLE_ACTIONS, ["approveAgent", "approveBuilderFee", "usdSend"]);
  } finally {
    globalThis.fetch = realFetch;
  }
}

console.log(`\n${pass}/${pass + fail} core checks passed`);
process.exit(fail ? 1 : 0);

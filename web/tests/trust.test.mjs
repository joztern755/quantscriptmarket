#!/usr/bin/env node
// Unit tests for the SECURITY H1/L1-L4 signing-trust code (no browser; Node's WebCrypto + mocked fetch):
//   pinned trust anchors (config.ts), trustedConfig/approveAgent/approveBuilderFee/usdSend refusals (hl.ts),
//   executor agent attestation (attest.ts), secp256k1 recovery (secp256k1.ts), payout wallet proof
//   (pages/_shared/walletproof.ts), full-address helpers (addr.ts), external-link rules (markdown.ts).
//   node web/build.mjs && node web/tests/trust.test.mjs
import { cpSync, existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { generateKeyPairSync, sign as nodeSign, webcrypto } from "node:crypto";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const APP = join(dirname(fileURLToPath(import.meta.url)), "..", "dist", "app");
if (!existsSync(APP)) {
  console.error("run node web/build.mjs first");
  process.exit(2);
}
const tmp = mkdtempSync(join(tmpdir(), "aij-trust-"));
cpSync(APP, join(tmp, "app"), { recursive: true });
writeFileSync(join(tmp, "package.json"), '{"type":"module"}');
globalThis.window = globalThis;
globalThis.document = { baseURI: "https://aijalon.trade/" };
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const imp = (m) => import(pathToFileURL(join(tmp, "app", m)).href);

let pass = 0;
let fail = 0;
function ok(name, cond, detail = "") {
  cond ? pass++ : fail++;
  if (!cond) console.log(`FAIL ${name}${detail ? ": " + detail : ""}`);
}
async function rejects(name, p, re) {
  try {
    await p;
    fail++;
    console.log(`FAIL ${name}: did not throw`);
  } catch (e) {
    const m = String(e?.message ?? e);
    re && !re.test(m) ? (fail++, console.log(`FAIL ${name}: wrong error ${m}`)) : pass++;
  }
}

// ---- keys / fixtures
const { publicKey, privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
const SPKI = publicKey.export({ type: "spki", format: "der" }).toString("base64");
const other = generateKeyPairSync("ec", { namedCurve: "P-256" });
const USER = "5b0f5f2e-3c5d-4d0e-9a51-2f1f7c0d9a11";
const AGENT = "0x1111111111111111111111111111111111111111";
const BUILDER = "0x2222222222222222222222222222222222222222";
const TREASURY = "0x3333333333333333333333333333333333333333";
const attestMsg = (u, a) => Buffer.from(`aijalon-agent-v1|${u}|${a.toLowerCase()}`);
const sigDer = (m, key = privateKey) => nodeSign("sha256", m, { key, dsaEncoding: "der" }).toString("base64");
const sigRaw = (m) => nodeSign("sha256", m, { key: privateKey, dsaEncoding: "ieee-p1363" }).toString("base64");

const APP_CONFIG = {
  siteOrigin: "https://aijalon.trade", apiOrigin: "https://api.aijalon.trade", hlApiUrl: "https://api.hyperliquid.xyz", firebaseSdkVersion: "12.3.0",
  firebase: { apiKey: "k", authDomain: "aijalon.trade", projectId: "p", appId: "a" },
  trust: { hlChain: "Mainnet", agentName: "aijalon", builderAddress: BUILDER, treasuryAddress: TREASURY, maxBuilderFeeTenthsBp: 100, agentAttestPublicKeySpki: SPKI, kycRedirectHosts: ["in.sumsub.com"] },
  fallback: { restricted_jurisdictions: [], legal_versions: {} },
};
const BASE = { builder_address: BUILDER, treasury_address: TREASURY, agent_name: "aijalon", hl_chain: "Mainnet", economics: { builder_fee_tenths_bp: 100 } };
let PUBLIC = { ...BASE };
const exchangeCalls = [];
globalThis.fetch = async (url, init) => {
  const u = String(url);
  const json = (b, status = 200) => new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });
  if (u.endsWith("/app-config.json")) return json(APP_CONFIG);
  if (u.includes("/v1/public/config")) return json(PUBLIC);
  if (u === "https://api.hyperliquid.xyz/exchange") {
    exchangeCalls.push(JSON.parse(init.body));
    return json({ status: "ok", response: { type: "default" } });
  }
  return json({ error: { code: "not_found", message: "nf" } }, 404);
};
function fakeWallet(address) {
  const w = { address, signed: [], chainIdHex: async () => "0xa4b1", signTypedDataV4: async (td) => (w.signed.push(td), "0x" + "11".repeat(32) + "22".repeat(32) + "1b") };
  return w;
}

// ---- config.ts: pinned anchors
const config = await imp("core/config.js");
const t0 = config.parseTrust({ hlChain: "Mainnet", agentName: "aijalon", builderAddress: "REPLACE_ME", treasuryAddress: "0xABCDEF0000000000000000000000000000000001", maxBuilderFeeTenthsBp: 1000, agentAttestPublicKeySpki: "REPLACE_ME", kycRedirectHosts: ["in.sumsub.com", "evil example"] });
ok("parseTrust placeholder -> empty", t0.builderAddress === "" && t0.agentAttestPublicKeySpki === "");
ok("parseTrust lower-cases addresses", t0.treasuryAddress === "0xabcdef0000000000000000000000000000000001");
ok("parseTrust refuses fee ceiling above 0.1%", t0.maxBuilderFeeTenthsBp === 0);
ok("parseTrust keeps only host names", JSON.stringify(t0.kycRedirectHosts) === '["in.sumsub.com"]');
ok("no trust before load", config.trustAnchors().builderAddress === "");
await config.loadAppConfig();
ok("trust loaded", config.trustAnchors().builderAddress === BUILDER && config.trustAnchors().agentAttestPublicKeySpki === SPKI);

// ---- attest.ts
const at = await imp("core/attest.js");
const good = sigDer(attestMsg(USER, AGENT));
ok("attestation message format", at.agentAttestationMessage(USER, AGENT.toUpperCase().replace("0X", "0x")) === `aijalon-agent-v1|${USER}|${AGENT}`);
ok("attestation DER verifies", await at.verifyAgentAttestation({ userId: USER, agentAddress: AGENT, signatureB64: good }));
ok("attestation raw r||s verifies", await at.verifyAgentAttestation({ userId: USER, agentAddress: AGENT, signatureB64: sigRaw(attestMsg(USER, AGENT)) }));
ok("attestation for another agent fails", !(await at.verifyAgentAttestation({ userId: USER, agentAddress: BUILDER, signatureB64: good })));
ok("attestation for another user fails", !(await at.verifyAgentAttestation({ userId: "00000000-0000-4000-8000-000000000000", agentAddress: AGENT, signatureB64: good })));
ok("attestation by another key fails", !(await at.verifyAgentAttestation({ userId: USER, agentAddress: AGENT, signatureB64: sigDer(attestMsg(USER, AGENT), other.privateKey) })));
ok("attestation garbage fails", !(await at.verifyAgentAttestation({ userId: USER, agentAddress: AGENT, signatureB64: "AAAA" })));
ok("attestation without pinned key fails", !(await at.verifyAgentAttestation({ userId: USER, agentAddress: AGENT, signatureB64: good, publicKeySpkiB64: "" })));
ok("derToRawP256 rejects trailing bytes", at.derToRawP256(new Uint8Array([...Buffer.from(good, "base64"), 0])) === null);

// ---- hl.ts: fee cap + trustedConfig + signing refusals
const hl = await imp("core/hl.js");
ok("fee encoder 100 -> 0.1%", hl.maxFeeRateFromTenthsBp(100) === "0.1%");
let threw = false;
try { hl.maxFeeRateFromTenthsBp(101); } catch { threw = true; }
ok("fee encoder refuses > 100 tenths-bp", threw);
const api = await imp("core/api.js");
const reset = async (p) => { PUBLIC = p; await api.publicConfig(true); };
await reset({ ...BASE });
ok("trustedConfig returns the pinned values", (await hl.trustedConfig({ builder: true, treasury: true })).builder_address === BUILDER);
await reset({ ...BASE, builder_address: "0x4444444444444444444444444444444444444444" });
await rejects("builder mismatch refused", hl.approveBuilderFee(fakeWallet(AGENT)), /builder address/);
await reset({ ...BASE, treasury_address: "0x4444444444444444444444444444444444444444" });
await rejects("treasury mismatch refused", hl.usdSend(fakeWallet(AGENT), { destination: TREASURY, amountMicro: 10_000_000, expectDestination: TREASURY }), /treasury/);
await reset({ ...BASE, hl_chain: "Testnet" });
await rejects("chain mismatch refused", hl.trustedConfig(), /network/);
await reset({ ...BASE, agent_name: "other" });
await rejects("agent name mismatch refused", hl.trustedConfig(), /agent name/);
await reset({ ...BASE, economics: { builder_fee_tenths_bp: 101 } });
await rejects("fee above pinned max refused", hl.trustedConfig(), /builder fee/);
await reset({ ...BASE });
const user = fakeWallet("0x5555555555555555555555555555555555555555");
await rejects("deposit to a non-treasury address refused", hl.usdSend(user, { destination: AGENT, amountMicro: 10_000_000, expectDestination: AGENT }), /pinned treasury/);
ok("nothing signed after refusals", user.signed.length === 0);
const dep = await hl.usdSend(user, { destination: TREASURY, amountMicro: 10_000_000, expectDestination: TREASURY });
ok("deposit to the pinned treasury signs", dep.ok && user.signed.length === 1 && user.signed[0].message.destination === TREASURY);
const tre = fakeWallet(TREASURY);
const pay = await hl.usdSend(tre, { destination: AGENT, amountMicro: 1_000_000, expectDestination: AGENT });
ok("treasury wallet may pay an approved destination", pay.ok && tre.signed.length === 1);
const w2 = fakeWallet(user.address);
await rejects("approveAgent without attestation refused", hl.approveAgent(w2, { agentAddress: AGENT, attestation: null }), /not attested/);
await rejects("approveAgent with a forged attestation refused", hl.approveAgent(w2, { agentAddress: AGENT, attestation: { userId: USER, signatureB64: sigDer(attestMsg(USER, AGENT), other.privateKey) } }), /not attested/);
ok("nothing signed without attestation", w2.signed.length === 0);
const ag = await hl.approveAgent(w2, { agentAddress: AGENT, attestation: { userId: USER, signatureB64: good } });
ok("approveAgent with a valid attestation signs the pinned name", ag.ok && w2.signed[0].message.agentName === "aijalon" && w2.signed[0].message.agentAddress === AGENT);

// ---- secp256k1 + wallet proof
const ec = await imp("core/secp256k1.js");
const ADDR = "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23"; // private key 0x4c0883a6…2318 (well-known test key)
const MSG = "aijalon.trade wants you to sign in with your Ethereum account:\n0x2c7536E3605D9C16a7a3D7b1898e529396a65c23\n\nLink this wallet to your aijalon.trade account. This signature proves you own the wallet. It does not authorize any transaction, transfer or trade.\n\nURI: https://aijalon.trade\nVersion: 1\nChain ID: 42161\nNonce: abcdefgh12345678\nIssued At: 2026-09-30T12:00:00Z\nExpiration Time: 2026-09-30T12:10:00Z";
const SIG = "0x29f4d75902a89ae47c00b245bdf991d9066fb966b39d2378c1320f06292edfaf5b3d8914e85a327559f3f4fddda5308f5b382ee2e2952a36c08ebaa0934c73d81c";
ok("ecrecover personal_sign vector", ec.recoverPersonalSign(MSG, SIG) === ADDR, String(ec.recoverPersonalSign(MSG, SIG)));
ok("ecrecover: v as 0/1", ec.recoverPersonalSign(MSG, SIG.slice(0, -2) + "01") === ADDR);
ok("ecrecover: other message -> other address", ec.recoverPersonalSign(MSG + " ", SIG) !== ADDR);
ok("ecrecover: malformed -> null", ec.recoverPersonalSign(MSG, "0x1234") === null && ec.recoverPersonalSign(MSG, SIG.slice(0, -2) + "05") === null);
const wp = await imp("pages/_shared/walletproof.js");
const proof = { user_id: USER, address: ADDR, message: MSG, signature: SIG, recorded_at: null };
ok("wallet proof OK", wp.checkWalletProof(proof, ADDR, USER).ok);
ok("wallet proof: missing", !wp.checkWalletProof(null, ADDR, USER).ok);
ok("wallet proof: other beneficiary", !wp.checkWalletProof(proof, ADDR, "00000000-0000-4000-8000-000000000000").ok);
ok("wallet proof: destination swapped", !wp.checkWalletProof({ ...proof, address: AGENT }, AGENT, USER).ok);
ok("wallet proof: other site", !wp.checkWalletProof({ ...proof, message: MSG.replace("aijalon.trade wants", "evil.example wants") }, ADDR, USER).ok);
ok("wallet proof: forged signature", !wp.checkWalletProof({ ...proof, signature: SIG.replace("29f4", "29f5") }, ADDR, USER).ok);

// ---- addr.ts + markdown rules
const addr = await imp("core/addr.js");
ok("grouped checksum", addr.groupedChecksum(ADDR) === "0x 2c75 36E3 605D 9C16 a7a3 D7b1 898e 5293 96a6 5c23");
ok("sameAddress tolerant of case/space", addr.sameAddress("0x 2c75 36E3 605D 9C16 a7a3 D7b1 898e 5293 96a6 5c23", ADDR));
ok("sameAddress catches a poisoned look-alike", !addr.sameAddress("0x2c7536e3605d9c16a7a3d7b1898e529396a65c24", ADDR));
const md = await imp("pages/_shared/markdown.js");
ok("externalHost", md.externalHost("https://evil.example/aijalon.trade/x") === "evil.example" && md.externalHost("#/market") === null);
ok("misleading label detected", md.misleadingLabel("aijalon.trade/reconnect-wallet", "evil.example"));
ok("honest label allowed", !md.misleadingLabel("Hyperliquid docs", "hyperliquid.gitbook.io") && !md.misleadingLabel("hyperliquid.xyz", "app.hyperliquid.xyz"));

console.log(`\n${pass}/${pass + fail} trust checks passed`);
process.exit(fail ? 1 : 0);

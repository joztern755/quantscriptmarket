// Hyperliquid user-signed actions (SPEC §6): ApproveAgent, ApproveBuilderFee, UsdSend.
//
// Policy: typed data offered by our server is VALIDATED (never signed as received); the payload that
// is actually signed is always rebuilt here with the wallet's CURRENT chain id as signatureChainId and
// a fresh nonce. Addresses/limits (builder, treasury, agent name, chain, fee cap) are the PINNED trust anchors of
// app-config.json; /v1/public/config must agree with them or nothing is signed (trustedConfig, SECURITY H1).
//
// Submission: direct browser POST to Hyperliquid /exchange; if that fails at the network level (e.g. CORS), the
// same body goes through our authenticated relay POST /v1/hl/exchange-relay (only approveAgent / approveBuilderFee
// / usdSend; the server validates every field and the signer, then forwards it unchanged). See postExchange.
//
// Action key order mirrors the Hyperliquid Python SDK (hyperliquid/exchange.py + utils/signing.py
// `sign_user_signed_action`, which APPENDS signatureChainId and hyperliquidChain to the dict built by
// the caller). UNVERIFIED here (no network/SDK source in this environment). For user-signed actions
// the exchange verifies an EIP-712 hash built from the typed fields, so JSON key order should not
// affect validity — but check against the SDK before go-live.

import { api, publicConfig, ApiError } from "./api.js";
import { appConfig, HL_MAX_BUILDER_FEE_TENTHS_BP, trustAnchors } from "./config.js";
import { verifyAgentAttestation } from "./attest.js";
import { microToDecimal, toMicro, type MicroLike } from "./format.js";
import { isAddress } from "./keccak.js";
import type { TypedData, Wallet } from "./wallet.js";

export type HlChain = "Mainnet" | "Testnet";
export type HlKind = "approveAgent" | "approveBuilderFee" | "usdSend";
type Field = { name: string; type: string };

const ZERO = "0x0000000000000000000000000000000000000000";
const DOMAIN_TYPES: Field[] = [
  { name: "name", type: "string" },
  { name: "version", type: "string" },
  { name: "chainId", type: "uint256" },
  { name: "verifyingContract", type: "address" },
];

export const HL_TYPES: Record<HlKind, { primaryType: string; fields: Field[] }> = {
  approveAgent: {
    primaryType: "HyperliquidTransaction:ApproveAgent",
    fields: [
      { name: "hyperliquidChain", type: "string" },
      { name: "agentAddress", type: "address" },
      { name: "agentName", type: "string" },
      { name: "nonce", type: "uint64" },
    ],
  },
  approveBuilderFee: {
    primaryType: "HyperliquidTransaction:ApproveBuilderFee",
    fields: [
      { name: "hyperliquidChain", type: "string" },
      { name: "maxFeeRate", type: "string" },
      { name: "builder", type: "address" },
      { name: "nonce", type: "uint64" },
    ],
  },
  usdSend: {
    primaryType: "HyperliquidTransaction:UsdSend",
    fields: [
      { name: "hyperliquidChain", type: "string" },
      { name: "destination", type: "string" },
      { name: "amount", type: "string" },
      { name: "time", type: "uint64" },
    ],
  },
};

export class HlValidationError extends ApiError {
  constructor(message: string) {
    super(0, "hl_validation_failed", message);
    this.name = "HlValidationError";
  }
}

export interface BuiltAction {
  kind: HlKind;
  action: Record<string, unknown>;
  nonce: number;
  typedData: TypedData;
}

function chainIdNum(hex: string): number {
  if (!/^0x[0-9a-fA-F]{1,16}$/.test(hex)) throw new HlValidationError("Invalid signatureChainId");
  const n = parseInt(hex, 16);
  if (!Number.isSafeInteger(n) || n <= 0) throw new HlValidationError("Invalid signatureChainId");
  return n;
}

function typed(kind: HlKind, chainHex: string, message: Record<string, unknown>): TypedData {
  const t = HL_TYPES[kind];
  return {
    types: { EIP712Domain: DOMAIN_TYPES, [t.primaryType]: t.fields },
    primaryType: t.primaryType,
    domain: { name: "HyperliquidSignTransaction", version: "1", chainId: chainIdNum(chainHex), verifyingContract: ZERO },
    message,
  };
}

function checkNonce(n: number): void {
  if (!Number.isSafeInteger(n) || n < 1_600_000_000_000 || n > 9_999_999_999_999) throw new HlValidationError("Invalid nonce");
}

function checkAddr(a: string, what: string): string {
  if (!isAddress(a)) throw new HlValidationError(`Invalid ${what} address`);
  return a.toLowerCase();
}

/**
 * Tenths of a bp → Hyperliquid percent string: 100 → "0.1%", 10 → "0.01%", 1 → "0.001%".
 * Hard cap 100 (= 0.1 %, Hyperliquid's perps maximum): a compromised config can never make us ask for more.
 */
export function maxFeeRateFromTenthsBp(t: number): string {
  if (!Number.isInteger(t) || t < 0 || t > HL_MAX_BUILDER_FEE_TENTHS_BP) throw new HlValidationError("Invalid builder fee rate");
  const whole = Math.floor(t / 1000);
  const frac = String(t % 1000).padStart(3, "0").replace(/0+$/, "");
  return `${whole}${frac ? "." + frac : ""}%`;
}

/** "0.1%" → 100 tenths-of-bp (rounded UP so a larger rate can never slip through). Null if malformed. */
export function tenthsBpFromMaxFeeRate(s: string): number | null {
  const m = /^(\d{1,3})(?:\.(\d{1,8}))?%$/.exec(s);
  if (!m) return null;
  const frac = m[2] ?? "";
  const scaled = BigInt(m[1]! + frac.padEnd(8, "0")); // percent × 1e8
  const per = 100_000n; // 1 tenth-bp = 0.001% = 1e5 in percent×1e8
  return Number((scaled + per - 1n) / per);
}

export function buildApproveAgent(p: { agentAddress: string; agentName: string; nonce: number; signatureChainId: `0x${string}`; hyperliquidChain: HlChain }): BuiltAction {
  checkNonce(p.nonce);
  const agentAddress = checkAddr(p.agentAddress, "agent");
  if (!/^[A-Za-z0-9_-]{1,16}$/.test(p.agentName)) throw new HlValidationError("Invalid agent name");
  const action = {
    type: "approveAgent",
    agentAddress,
    agentName: p.agentName,
    nonce: p.nonce,
    signatureChainId: p.signatureChainId,
    hyperliquidChain: p.hyperliquidChain,
  };
  return {
    kind: "approveAgent",
    action,
    nonce: p.nonce,
    typedData: typed("approveAgent", p.signatureChainId, { hyperliquidChain: p.hyperliquidChain, agentAddress, agentName: p.agentName, nonce: p.nonce }),
  };
}

export function buildApproveBuilderFee(p: { builder: string; maxFeeRate: string; nonce: number; signatureChainId: `0x${string}`; hyperliquidChain: HlChain }): BuiltAction {
  checkNonce(p.nonce);
  const builder = checkAddr(p.builder, "builder");
  if (tenthsBpFromMaxFeeRate(p.maxFeeRate) === null) throw new HlValidationError("Invalid maxFeeRate");
  const action = {
    maxFeeRate: p.maxFeeRate,
    builder,
    nonce: p.nonce,
    type: "approveBuilderFee",
    signatureChainId: p.signatureChainId,
    hyperliquidChain: p.hyperliquidChain,
  };
  return {
    kind: "approveBuilderFee",
    action,
    nonce: p.nonce,
    typedData: typed("approveBuilderFee", p.signatureChainId, { hyperliquidChain: p.hyperliquidChain, maxFeeRate: p.maxFeeRate, builder, nonce: p.nonce }),
  };
}

export function buildUsdSend(p: { destination: string; amount: string; time: number; signatureChainId: `0x${string}`; hyperliquidChain: HlChain }): BuiltAction {
  checkNonce(p.time);
  const destination = checkAddr(p.destination, "destination");
  if (!/^(0|[1-9]\d{0,11})(\.\d{1,6})?$/.test(p.amount) || /^0(\.0+)?$/.test(p.amount)) throw new HlValidationError("Invalid amount");
  const action = {
    destination,
    amount: p.amount,
    time: p.time,
    type: "usdSend",
    signatureChainId: p.signatureChainId,
    hyperliquidChain: p.hyperliquidChain,
  };
  return {
    kind: "usdSend",
    action,
    nonce: p.time,
    typedData: typed("usdSend", p.signatureChainId, { hyperliquidChain: p.hyperliquidChain, destination, amount: p.amount, time: p.time }),
  };
}

export interface ValidateExpect {
  hyperliquidChain: HlChain;
  agentAddress?: string;
  agentName?: string;
  builder?: string;
  maxFeeTenthsBp?: number;
  destination?: string;
  amountMicro?: MicroLike;
}

/**
 * Validates typed data offered by our server against what the client expects. Throws HlValidationError
 * on ANY deviation (a compromised or buggy server must not be able to get a different action signed).
 */
export function validateServerTypedData(kind: HlKind, raw: unknown, expect: ValidateExpect): { nonce: number; message: Record<string, unknown> } {
  const t = HL_TYPES[kind];
  const td = (typeof raw === "string" ? JSON.parse(raw) : raw) as Partial<TypedData> | null;
  if (!td || typeof td !== "object") throw new HlValidationError("Missing typed data");
  if (td.primaryType !== t.primaryType) throw new HlValidationError("Unexpected action type");
  const d = td.domain as Record<string, unknown> | undefined;
  if (!d || d.name !== "HyperliquidSignTransaction" || d.version !== "1" || String(d.verifyingContract).toLowerCase() !== ZERO) throw new HlValidationError("Unexpected signing domain");
  const fields = td.types?.[t.primaryType];
  if (!Array.isArray(fields) || fields.length !== t.fields.length || fields.some((f, i) => f.name !== t.fields[i]!.name || f.type !== t.fields[i]!.type)) throw new HlValidationError("Unexpected typed-data fields");
  const extraTypes = Object.keys(td.types ?? {}).filter((k) => k !== t.primaryType && k !== "EIP712Domain");
  if (extraTypes.length) throw new HlValidationError("Unexpected extra types");
  const msg = (td.message ?? {}) as Record<string, unknown>;
  if (msg.hyperliquidChain !== expect.hyperliquidChain) throw new HlValidationError("Wrong Hyperliquid chain");
  const nonceKey = kind === "usdSend" ? "time" : "nonce";
  const nonce = Number(msg[nonceKey]);
  checkNonce(nonce);
  const eqAddr = (a: unknown, b: string | undefined, what: string) => {
    if (!b || !isAddress(b)) throw new HlValidationError(`No expected ${what} address configured`);
    if (typeof a !== "string" || a.toLowerCase() !== b.toLowerCase()) throw new HlValidationError(`${what} address does not match`);
  };
  if (kind === "approveAgent") {
    eqAddr(msg.agentAddress, expect.agentAddress, "Agent");
    if (msg.agentName !== expect.agentName) throw new HlValidationError("Agent name does not match");
  } else if (kind === "approveBuilderFee") {
    eqAddr(msg.builder, expect.builder, "Builder");
    const rate = typeof msg.maxFeeRate === "string" ? tenthsBpFromMaxFeeRate(msg.maxFeeRate) : null;
    if (rate === null || expect.maxFeeTenthsBp === undefined || rate > expect.maxFeeTenthsBp) throw new HlValidationError("Builder fee rate is above the published maximum");
  } else {
    eqAddr(msg.destination, expect.destination, "Destination");
    if (expect.amountMicro === undefined || typeof msg.amount !== "string") throw new HlValidationError("Amount missing");
    const m = /^(\d+)(?:\.(\d{1,6}))?$/.exec(msg.amount);
    if (!m) throw new HlValidationError("Invalid amount");
    const micro = BigInt(m[1]!) * 1_000_000n + BigInt((m[2] ?? "").padEnd(6, "0") || "0");
    if (micro !== toMicro(expect.amountMicro)) throw new HlValidationError("Amount does not match");
  }
  return { nonce, message: msg };
}

export function splitSignature(sig: string): { r: string; s: string; v: number } {
  if (!/^0x[0-9a-fA-F]{130}$/.test(sig)) throw new HlValidationError("Invalid signature");
  const r = "0x" + sig.slice(2, 66).toLowerCase();
  const s = "0x" + sig.slice(66, 130).toLowerCase();
  let v = parseInt(sig.slice(130, 132), 16);
  if (v < 27) v += 27;
  if (v !== 27 && v !== 28) throw new HlValidationError("Invalid signature recovery id");
  return { r, s, v };
}

export interface HlResult {
  ok: boolean;
  status: "ok" | "err";
  response: unknown;
  error?: string;
  /** The nonce actually signed (for usdSend = its `time`, ms) — e.g. POST /v1/deposits/usdc/confirm {time_ms}. */
  nonce?: number;
}

function hlUrl(path: "/exchange" | "/info"): string {
  return `${appConfig().hlApiUrl}${path}`;
}

/** Only these user-signed actions may go through our relay (the server refuses everything else anyway). */
export const RELAYABLE_ACTIONS: readonly string[] = ["approveAgent", "approveBuilderFee", "usdSend"];

type ExchangeBody = { action: Record<string, unknown>; nonce: number; signature: { r: string; s: string; v: number } };

/** Hyperliquid /exchange HTTP status + body → HlResult (shared by the direct call and the relay). */
export function interpretExchange(status: number, textBody: string, json: unknown): HlResult {
  if (status < 200 || status >= 300) return { ok: false, status: "err", response: json ?? textBody, error: `Hyperliquid HTTP ${status}: ${textBody.slice(0, 200)}` };
  const j = (json ?? {}) as { status?: string; response?: unknown };
  if (j.status === "ok") {
    // Some actions nest per-item errors (e.g. {"response":{"type":"default"}} is success).
    const inner = j.response as { data?: { statuses?: { error?: string }[] } } | undefined;
    const nestedErr = inner?.data?.statuses?.find((s) => s && typeof s.error === "string")?.error;
    if (nestedErr) return { ok: false, status: "err", response: j.response, error: nestedErr };
    return { ok: true, status: "ok", response: j.response };
  }
  const err = typeof j.response === "string" ? j.response : JSON.stringify(j.response ?? textBody).slice(0, 300);
  return { ok: false, status: "err", response: j.response, error: err };
}

/**
 * Submit a user-signed action. Direct POST to Hyperliquid /exchange first; if that fails at the NETWORK level (fetch
 * throws: offline, DNS, or a CORS/preflight refusal — Hyperliquid's CORS for our origin is UNVERIFIED, DEPLOY §16)
 * and the action is one of RELAYABLE_ACTIONS, the SAME body goes to our authenticated relay
 * (POST /v1/hl/exchange-relay), which validates it against the server's expectations and forwards it unchanged.
 * An HTTP error from Hyperliquid is an answer, not a network failure: it is never retried through the relay.
 */
export async function postExchange(body: ExchangeBody): Promise<HlResult> {
  const payload: ExchangeBody = { action: body.action, nonce: body.nonce, signature: body.signature };
  let res: Response;
  try {
    res = await fetch(hlUrl("/exchange"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      cache: "no-store",
    });
  } catch {
    return relayExchange(payload);
  }
  const textBody = await res.text();
  let json: unknown = null;
  try {
    json = JSON.parse(textBody);
  } catch { /* non-JSON error */ }
  return interpretExchange(res.status, textBody, json);
}

/** Fallback path of postExchange (exported for tests): our relay, same body, Hyperliquid's answer passed through. */
export async function relayExchange(payload: ExchangeBody): Promise<HlResult> {
  const kind = String(payload.action.type ?? "");
  if (!RELAYABLE_ACTIONS.includes(kind)) throw new ApiError(0, "network_error", "Couldn't reach Hyperliquid. Your signature was not submitted.");
  let out: { upstream_status?: number; response?: unknown };
  try {
    out = await api.post<{ upstream_status?: number; response?: unknown }>("/hl/exchange-relay", payload, { stepUp: false });
  } catch (err) {
    if (err instanceof ApiError && (err.status === 0 || err.code === "network_error")) {
      throw new ApiError(0, "network_error", "Couldn't reach Hyperliquid or the aijalon relay. Your signature was not submitted.");
    }
    throw err;
  }
  const status = typeof out.upstream_status === "number" ? out.upstream_status : 502;
  const json = typeof out.response === "string" ? null : out.response ?? null;
  const text = typeof out.response === "string" ? out.response : JSON.stringify(out.response ?? "");
  const r = interpretExchange(status, text, json);
  // The direct POST may have reached Hyperliquid even though the browser could not read the answer (CORS): the
  // relayed copy is then refused as a reused nonce. Say so instead of implying nothing happened.
  if (!r.ok && /nonce/i.test(r.error ?? "")) {
    return { ...r, error: `${r.error} — the first attempt may already have gone through; check the status before signing again.` };
  }
  return r;
}

export async function hlInfo<T>(body: Record<string, unknown>): Promise<T> {
  let res: Response;
  try {
    res = await fetch(hlUrl("/info"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), credentials: "omit", referrerPolicy: "no-referrer" });
  } catch {
    throw new ApiError(0, "network_error", "Couldn't reach Hyperliquid.");
  }
  if (!res.ok) throw new ApiError(res.status, "hl_info_error", `Hyperliquid info error (${res.status})`);
  return (await res.json()) as T;
}

export async function signAndSubmit(wallet: Wallet, built: BuiltAction): Promise<HlResult> {
  const sig = await wallet.signTypedDataV4(built.typedData);
  const res = await postExchange({ action: built.action, nonce: built.nonce, signature: splitSignature(sig) });
  return { ...res, nonce: built.nonce };
}

/**
 * Public config from the API, CROSS-CHECKED against the trust anchors pinned in app-config.json (SECURITY H1).
 * The API is not trusted for anything a wallet signs: chain, agent name, builder, treasury and the fee ceiling must
 * equal the pinned values (any disagreement = a compromised/buggy API or edge → refuse), and the values used for
 * signing are the PINNED ones.
 */
export interface TrustedConfig {
  hl_chain: HlChain;
  agent_name: string;
  builder_address: string;
  treasury_address: string;
  builder_fee_tenths_bp: number;
}

export async function trustedConfig(need: { builder?: boolean; treasury?: boolean } = {}): Promise<TrustedConfig> {
  const cfg = await publicConfig();
  if (cfg._fallback) throw new ApiError(0, "config_unavailable", "Couldn't load the platform configuration. Try again in a moment.");
  const t = trustAnchors();
  const refuse = (what: string) => {
    throw new HlValidationError(`Signing refused: ${what}. Nothing was signed — please contact support.`);
  };
  if (!t.hlChain || !t.agentName || !t.maxBuilderFeeTenthsBp) refuse("this site build has no pinned signing configuration");
  if (cfg.hl_chain !== t.hlChain) refuse("the server's Hyperliquid network does not match this site's pinned network");
  if (cfg.agent_name !== t.agentName) refuse("the server's agent name does not match this site's pinned agent name");
  const fee = cfg.economics.builder_fee_tenths_bp;
  if (!Number.isInteger(fee) || fee <= 0 || fee > t.maxBuilderFeeTenthsBp || fee > HL_MAX_BUILDER_FEE_TENTHS_BP) refuse("the server's builder fee is above this site's pinned maximum");
  if (need.builder) {
    if (!t.builderAddress) refuse("this site build has no pinned builder address");
    if (cfg.builder_address !== t.builderAddress) refuse("the server's builder address does not match this site's pinned builder address");
  }
  if (need.treasury) {
    if (!t.treasuryAddress) refuse("this site build has no pinned treasury address");
    if (cfg.treasury_address !== t.treasuryAddress) refuse("the server's treasury address does not match this site's pinned treasury address");
  }
  return { hl_chain: t.hlChain as HlChain, agent_name: t.agentName, builder_address: t.builderAddress, treasury_address: t.treasuryAddress, builder_fee_tenths_bp: fee };
}

/** Executor-signed proof that `agentAddress` is the user's sealed agent (see core/attest.ts). */
export interface AgentAttestationProof {
  userId: string;
  signatureB64: string;
}

/**
 * Master wallet approves our per-user agent (named `agent_name`). Refuses unless the agent address carries a valid
 * executor attestation verified with the PINNED public key (a compromised API cannot mint one).
 */
export async function approveAgent(wallet: Wallet, p: { agentAddress: string; serverTypedData?: unknown; attestation: AgentAttestationProof | null | undefined }): Promise<HlResult> {
  const cfg = await trustedConfig();
  const agentAddress = checkAddr(p.agentAddress, "agent");
  if (!p.attestation || !(await verifyAgentAttestation({ userId: p.attestation.userId, agentAddress, signatureB64: p.attestation.signatureB64 }))) {
    throw new HlValidationError("Signing refused: this agent address is not attested by aijalon's executor. Nothing was signed — please contact support.");
  }
  if (p.serverTypedData !== undefined) {
    validateServerTypedData("approveAgent", p.serverTypedData, { hyperliquidChain: cfg.hl_chain, agentAddress, agentName: cfg.agent_name });
  }
  const built = buildApproveAgent({ agentAddress, agentName: cfg.agent_name, nonce: Date.now(), signatureChainId: await wallet.chainIdHex(), hyperliquidChain: cfg.hl_chain });
  return signAndSubmit(wallet, built);
}

/** Master wallet approves our builder fee at the published rate (never above the pinned ceiling, never above 0.1 %). */
export async function approveBuilderFee(wallet: Wallet, p: { serverTypedData?: unknown } = {}): Promise<HlResult> {
  const cfg = await trustedConfig({ builder: true });
  const maxT = cfg.builder_fee_tenths_bp;
  if (p.serverTypedData !== undefined) {
    validateServerTypedData("approveBuilderFee", p.serverTypedData, { hyperliquidChain: cfg.hl_chain, builder: cfg.builder_address, maxFeeTenthsBp: maxT });
  }
  const built = buildApproveBuilderFee({ builder: cfg.builder_address, maxFeeRate: maxFeeRateFromTenthsBp(maxT), nonce: Date.now(), signatureChainId: await wallet.chainIdHex(), hyperliquidChain: cfg.hl_chain });
  return signAndSubmit(wallet, built);
}

/**
 * USDC transfer (perps balance).
 *  * From a USER wallet it can only be a fee-balance deposit: the destination must be the PINNED treasury.
 *  * From the PINNED treasury wallet (admin payouts / refunds, maker-checker approved) the destination is the
 *    approved record's address; the caller must have shown it in full and verified the beneficiary's proof.
 * `expectDestination` must come from a trusted source and must equal `destination`.
 */
export async function usdSend(wallet: Wallet, p: { destination: string; amountMicro: MicroLike; expectDestination: string; serverTypedData?: unknown }): Promise<HlResult> {
  const cfg = await trustedConfig({ treasury: true });
  if (!isAddress(p.destination) || p.destination.toLowerCase() !== String(p.expectDestination).toLowerCase()) throw new HlValidationError("Destination does not match the expected address");
  const fromTreasury = wallet.address.toLowerCase() === cfg.treasury_address;
  if (!fromTreasury && p.destination.toLowerCase() !== cfg.treasury_address) {
    throw new HlValidationError("Signing refused: deposits can only go to this site's pinned treasury address. Nothing was signed.");
  }
  const amt = toMicro(p.amountMicro);
  if (amt <= 0n) throw new HlValidationError("Amount must be positive");
  if (p.serverTypedData !== undefined) {
    validateServerTypedData("usdSend", p.serverTypedData, { hyperliquidChain: cfg.hl_chain, destination: p.expectDestination, amountMicro: amt });
  }
  const built = buildUsdSend({ destination: p.destination, amount: microToDecimal(amt), time: Date.now(), signatureChainId: await wallet.chainIdHex(), hyperliquidChain: cfg.hl_chain });
  return signAndSubmit(wallet, built);
}

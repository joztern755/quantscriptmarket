// Payout / withdrawal destination proof (SECURITY H1). The beneficiary proves control of the destination wallet with
// an EIP-4361 personal_sign (POST /v1/wallets/verify stores message + signature). Before an admin approves or signs
// the treasury usdSend, THIS browser recovers the signer itself and checks it against the destination and this
// site's pinned origin — so a compromised API/edge cannot swap in an address whose key it does not hold.
import { api } from "../../core/api.js";
import { appConfig, siteDomain } from "../../core/config.js";
import { toChecksumAddress } from "../../core/keccak.js";
import { recoverPersonalSign } from "../../core/secp256k1.js";
import { connectWallet, getConnectedWallet, proveOwnership } from "../../core/wallet.js";
import { h, note } from "../../core/ui.js";
import { fmtDateTime } from "../../core/format.js";
import { addressCheck } from "../../core/addr.js";

/** GET /v1/admin/payouts/{kind}/{id}/wallet-proof (backend app/api/routers/trust.py). */
export interface WalletProofOut {
  user_id: string;
  address: string;
  message: string;
  signature: string;
  recorded_at: string | null;
}

export interface ProofCheck {
  ok: boolean;
  reason: string;
  issuedAt?: string;
}

function field(lines: string[], name: string): string | null {
  const l = lines.find((x) => x.startsWith(`${name}: `));
  return l ? l.slice(name.length + 2) : null;
}

/** Pure check (exported for tests): signature recovers to `toAddress`, message is our SIWE text for that address. */
export function checkWalletProof(proof: WalletProofOut | null, toAddress: string, beneficiary: string): ProofCheck {
  if (!proof || typeof proof.message !== "string" || typeof proof.signature !== "string") {
    return { ok: false, reason: "No wallet-ownership proof on file for this destination. Ask the beneficiary to request again (they sign with the destination wallet)." };
  }
  const to = toAddress.toLowerCase();
  if (String(proof.address).toLowerCase() !== to) return { ok: false, reason: "The proof is for a different address." };
  if (String(proof.user_id).toLowerCase() !== beneficiary.toLowerCase()) return { ok: false, reason: "The proof belongs to a different user than the beneficiary." };
  const lines = proof.message.split("\n");
  if (lines[0] !== `${siteDomain()} wants you to sign in with your Ethereum account:`) return { ok: false, reason: "The proof was not signed for this site." };
  let checksum = "";
  try {
    checksum = toChecksumAddress(to);
  } catch {
    return { ok: false, reason: "Invalid destination address." };
  }
  if (lines[1] !== checksum) return { ok: false, reason: "The signed message names a different address." };
  const origin = appConfig().siteOrigin.replace(/\/+$/, "");
  if (field(lines, "URI") !== origin) return { ok: false, reason: "The signed message is for a different site URI." };
  const signer = recoverPersonalSign(proof.message, proof.signature);
  if (signer !== to) return { ok: false, reason: "The signature was NOT made by the destination wallet." };
  return { ok: true, reason: "Signed by the destination wallet (verified in this browser).", issuedAt: field(lines, "Issued At") ?? undefined };
}

export async function fetchAndCheckProof(path: string, toAddress: string, beneficiary: string, signal?: AbortSignal): Promise<ProofCheck> {
  let proof: WalletProofOut | null = null;
  try {
    proof = await api.get<WalletProofOut>(path, { signal });
  } catch {
    proof = null;
  }
  return checkWalletProof(proof, toAddress, beneficiary);
}

/** Destination block for admin dialogs: full checksummed address + compare box + the proof verdict. */
export function destinationBlock(toAddress: string, check: ProofCheck): HTMLElement {
  return h(
    "div",
    { class: "stack" },
    addressCheck(toAddress, { label: "Destination — compare with the hardware wallet screen", hint: "Read every character on the hardware wallet before confirming." }),
    note(check.ok ? `Wallet proof OK: ${check.reason}${check.issuedAt ? ` Signed ${fmtDateTime(check.issuedAt)}.` : ""}` : `Wallet proof FAILED: ${check.reason}`, check.ok ? "info" : "bad"),
  );
}

/**
 * Beneficiary side: before a payout / withdrawal request, the user signs a fresh ownership proof WITH the destination
 * wallet (stored server-side and re-verified by the admins' browsers). Throws when the connected wallet differs.
 */
export async function proveDestination(address: string): Promise<boolean> {
  const w = getConnectedWallet() ?? (await connectWallet());
  if (!w) return false;
  if (w.address.toLowerCase() !== address.toLowerCase()) {
    throw new Error(`Connect the destination wallet ${address} in your wallet extension to sign the ownership proof (currently ${w.address}).`);
  }
  await proveOwnership(w);
  return true;
}

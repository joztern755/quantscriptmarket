// Agent attestation (SECURITY H1; executor keygen, migrations/0016). The EXECUTOR — the only service that can
// generate, seal and decrypt agent keys — generates the user's agent key itself, checks the sealed blob re-opens to
// `agent_address`, and then signs
//     aijalon-agent-v2|{user_id}|{agent_address}
// with a Cloud KMS asymmetric key (EC_SIGN_P256_SHA256, HSM) that the api service account cannot use. The browser
// verifies that signature with the public key PINNED in app-config.json (Hosting, reviewed commit) before it asks
// the wallet to sign ApproveAgent. A compromised api or edge can therefore no longer substitute its own agent
// address (it cannot produce a valid attestation), and — since the api can no longer generate or seal agent keys at
// all — cannot get a key it KNOWS attested either. v1 attestations (api-generated keys, before 0016) are refused.
//
// WebCrypto verifies ECDSA P-256 natively; KMS returns an ASN.1 DER ECDSA-Sig-Value, WebCrypto wants raw r||s.

import { trustAnchors } from "./config.js";

export const AGENT_ATTEST_PREFIX = "aijalon-agent-v2";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ADDR = /^0x[0-9a-fA-F]{40}$/;

/** The exact byte string the executor signs (built locally — a message sent by the server is never trusted). */
export function agentAttestationMessage(userId: string, agentAddress: string): string {
  if (!UUID.test(userId)) throw new Error("invalid user id");
  if (!ADDR.test(agentAddress)) throw new Error("invalid agent address");
  return `${AGENT_ATTEST_PREFIX}|${userId.toLowerCase()}|${agentAddress.toLowerCase()}`;
}

export function b64ToBytes(b64: string): Uint8Array | null {
  try {
    const s = b64.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return null;
  }
}

/** DER ECDSA-Sig-Value (SEQUENCE { INTEGER r, INTEGER s }) → raw 64-byte r||s for P-256. Null when malformed. */
export function derToRawP256(der: Uint8Array): Uint8Array | null {
  let i = 0;
  const byte = (): number => {
    if (i >= der.length) throw new Error("short");
    return der[i++]!;
  };
  const len = (): number => {
    const b = byte();
    if (b < 0x80) return b;
    if (b === 0x81) return byte();
    throw new Error("length");
  };
  const int = (): Uint8Array => {
    if (byte() !== 0x02) throw new Error("int");
    const n = len();
    if (n < 1 || n > 33 || i + n > der.length) throw new Error("int len");
    let v = der.slice(i, i + n);
    i += n;
    if (v.length === 33) {
      if (v[0] !== 0) throw new Error("int pad");
      v = v.slice(1);
    }
    const out = new Uint8Array(32);
    out.set(v, 32 - v.length);
    return out;
  };
  try {
    if (byte() !== 0x30) return null;
    const total = len();
    if (i + total !== der.length) return null;
    const r = int();
    const s = int();
    if (i !== der.length) return null;
    const raw = new Uint8Array(64);
    raw.set(r, 0);
    raw.set(s, 32);
    return raw;
  } catch {
    return null;
  }
}

/**
 * True only when `signatureB64` (DER or raw r||s, base64) is a valid P-256/SHA-256 signature by the PINNED
 * attestation key over agentAttestationMessage(userId, agentAddress). Any error → false (fail closed).
 */
export async function verifyAgentAttestation(p: { userId: string; agentAddress: string; signatureB64: string; publicKeySpkiB64?: string }): Promise<boolean> {
  try {
    const spkiB64 = p.publicKeySpkiB64 ?? trustAnchors().agentAttestPublicKeySpki;
    if (!spkiB64) return false;
    const spki = b64ToBytes(spkiB64);
    const sigBytes = b64ToBytes(p.signatureB64);
    if (!spki || !sigBytes) return false;
    const raw = sigBytes.length === 64 ? sigBytes : derToRawP256(sigBytes);
    if (!raw) return false;
    const subtle = globalThis.crypto?.subtle;
    if (!subtle) return false;
    const ab = (u: Uint8Array): ArrayBuffer => u.slice().buffer as ArrayBuffer;
    const key = await subtle.importKey("spki", ab(spki), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    const msg = new TextEncoder().encode(agentAttestationMessage(p.userId, p.agentAddress));
    return await subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, ab(raw), ab(msg));
  } catch {
    return false;
  }
}

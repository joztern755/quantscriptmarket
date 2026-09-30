// Minimal secp256k1 public-key RECOVERY (ecrecover) for verifying wallet signatures in the browser, with no
// dependencies. Used by the admin payout flow (SECURITY H1): the beneficiary's own EIP-4361 wallet-ownership
// signature is re-verified HERE, so a compromised API cannot swap the payout destination for an address it controls
// without also holding that address's key.
//
// Verification only (no secret keys ever touch this code), so constant-time arithmetic is not required.
// BigInt Jacobian arithmetic; a recovery takes a few milliseconds.

import { keccak256, toHex } from "./keccak.js";

const P = 0xfffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2fn;
const N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;
const GX = 0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798n;
const GY = 0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8n;

type J = [bigint, bigint, bigint]; // Jacobian (X, Y, Z); Z = 0 → point at infinity
const INF: J = [0n, 1n, 0n];

function mod(a: bigint, m: bigint = P): bigint {
  const r = a % m;
  return r >= 0n ? r : r + m;
}

function inv(a: bigint, m: bigint = P): bigint {
  let [lm, hm, low, high] = [1n, 0n, mod(a, m), m];
  if (low === 0n) throw new Error("no inverse");
  while (low > 1n) {
    const q = high / low;
    [lm, hm] = [hm - lm * q, lm];
    [low, high] = [high - low * q, low];
  }
  return mod(lm, m);
}

function pow(b: bigint, e: bigint, m: bigint): bigint {
  let r = 1n;
  b = mod(b, m);
  while (e > 0n) {
    if (e & 1n) r = (r * b) % m;
    b = (b * b) % m;
    e >>= 1n;
  }
  return r;
}

function dbl(p: J): J {
  const [X, Y, Z] = p;
  if (Z === 0n || Y === 0n) return INF;
  const YY = mod(Y * Y);
  const S = mod(4n * X * YY);
  const M = mod(3n * X * X); // a = 0
  const X3 = mod(M * M - 2n * S);
  const Y3 = mod(M * (S - X3) - 8n * YY * YY);
  const Z3 = mod(2n * Y * Z);
  return [X3, Y3, Z3];
}

function add(p: J, q: J): J {
  if (p[2] === 0n) return q;
  if (q[2] === 0n) return p;
  const [X1, Y1, Z1] = p;
  const [X2, Y2, Z2] = q;
  const Z1Z1 = mod(Z1 * Z1);
  const Z2Z2 = mod(Z2 * Z2);
  const U1 = mod(X1 * Z2Z2);
  const U2 = mod(X2 * Z1Z1);
  const S1 = mod(Y1 * Z2 * Z2Z2);
  const S2 = mod(Y2 * Z1 * Z1Z1);
  if (U1 === U2) return S1 === S2 ? dbl(p) : INF;
  const H = mod(U2 - U1);
  const R = mod(S2 - S1);
  const HH = mod(H * H);
  const HHH = mod(H * HH);
  const V = mod(U1 * HH);
  const X3 = mod(R * R - HHH - 2n * V);
  const Y3 = mod(R * (V - X3) - S1 * HHH);
  const Z3 = mod(Z1 * Z2 * H);
  return [X3, Y3, Z3];
}

function mul(k: bigint, p: J): J {
  let r = INF;
  let a = p;
  k = mod(k, N);
  while (k > 0n) {
    if (k & 1n) r = add(r, a);
    a = dbl(a);
    k >>= 1n;
  }
  return r;
}

function affine(p: J): [bigint, bigint] | null {
  if (p[2] === 0n) return null;
  const zi = inv(p[2]);
  const zi2 = mod(zi * zi);
  return [mod(p[0] * zi2), mod(p[1] * zi2 * zi)];
}

function be32(n: bigint): Uint8Array {
  const out = new Uint8Array(32);
  for (let i = 31; i >= 0; i--) {
    out[i] = Number(n & 0xffn);
    n >>= 8n;
  }
  return out;
}

function bytesToBig(b: Uint8Array): bigint {
  let n = 0n;
  for (const x of b) n = (n << 8n) | BigInt(x);
  return n;
}

/** Recover the lower-case Ethereum address that produced (r, s, recId) over a 32-byte digest; null if invalid. */
export function recoverAddressFromDigest(digest: Uint8Array, r: bigint, s: bigint, recId: number): string | null {
  if (digest.length !== 32 || recId < 0 || recId > 1) return null;
  if (r <= 0n || r >= N || s <= 0n || s >= N) return null;
  const x = r; // recId 2/3 (x = r + n) cannot occur for honest wallets in practice; refused.
  if (x >= P) return null;
  const y2 = mod(x * x * x + 7n);
  let y = pow(y2, (P + 1n) / 4n, P);
  if (mod(y * y) !== y2) return null;
  if (Number(y & 1n) !== recId) y = P - y;
  const e = mod(bytesToBig(digest), N);
  const rInv = inv(r, N);
  const u1 = mod(-e * rInv, N);
  const u2 = mod(s * rInv, N);
  const q = add(mul(u1, [GX, GY, 1n]), mul(u2, [x, y, 1n]));
  const a = affine(q);
  if (!a) return null;
  const pub = new Uint8Array(64);
  pub.set(be32(a[0]), 0);
  pub.set(be32(a[1]), 32);
  return "0x" + toHex(keccak256(pub).slice(12));
}

/** EIP-191 personal_sign digest: keccak256("\x19Ethereum Signed Message:\n" + len(bytes) + bytes). */
export function personalMessageDigest(message: string): Uint8Array {
  const body = new TextEncoder().encode(message);
  const prefix = new TextEncoder().encode(`\x19Ethereum Signed Message:\n${body.length}`);
  const all = new Uint8Array(prefix.length + body.length);
  all.set(prefix, 0);
  all.set(body, prefix.length);
  return keccak256(all);
}

/** Signer of a personal_sign signature (0x + 65 bytes r||s||v, v ∈ {0,1,27,28}); null when malformed. */
export function recoverPersonalSign(message: string, signature: string): string | null {
  if (typeof signature !== "string" || !/^0x[0-9a-fA-F]{130}$/.test(signature)) return null;
  const r = BigInt("0x" + signature.slice(2, 66));
  const s = BigInt("0x" + signature.slice(66, 130));
  let v = parseInt(signature.slice(130, 132), 16);
  if (v >= 27) v -= 27;
  if (v !== 0 && v !== 1) return null;
  try {
    return recoverAddressFromDigest(personalMessageDigest(message), r, s, v);
  } catch {
    return null;
  }
}

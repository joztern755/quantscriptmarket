// Keccak-256 (Ethereum variant: original Keccak padding 0x01), used for EIP-55 checksums.
// Small inputs only (addresses, short strings) — BigInt lanes favour clarity over speed.

const MASK = (1n << 64n) - 1n;
const RC: bigint[] = [
  0x0000000000000001n, 0x0000000000008082n, 0x800000000000808an, 0x8000000080008000n,
  0x000000000000808bn, 0x0000000080000001n, 0x8000000080008081n, 0x8000000000008009n,
  0x000000000000008an, 0x0000000000000088n, 0x0000000080008009n, 0x000000008000000an,
  0x000000008000808bn, 0x800000000000008bn, 0x8000000000008089n, 0x8000000000008003n,
  0x8000000000008002n, 0x8000000000000080n, 0x000000000000800an, 0x800000008000000an,
  0x8000000080008081n, 0x8000000000008080n, 0x0000000080000001n, 0x8000000080008008n,
];
// Rotation offsets indexed [x + 5*y]
const R = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14];

function rotl(v: bigint, n: number): bigint {
  if (n === 0) return v;
  const b = BigInt(n);
  return ((v << b) | (v >> (64n - b))) & MASK;
}

function keccakF(a: bigint[]): void {
  const c = new Array<bigint>(5);
  const b = new Array<bigint>(25);
  for (let round = 0; round < 24; round++) {
    for (let x = 0; x < 5; x++) c[x] = a[x]! ^ a[x + 5]! ^ a[x + 10]! ^ a[x + 15]! ^ a[x + 20]!;
    for (let x = 0; x < 5; x++) {
      const d = c[(x + 4) % 5]! ^ rotl(c[(x + 1) % 5]!, 1);
      for (let y = 0; y < 25; y += 5) a[y + x] = a[y + x]! ^ d;
    }
    for (let x = 0; x < 5; x++) {
      for (let y = 0; y < 5; y++) {
        b[y + 5 * ((2 * x + 3 * y) % 5)] = rotl(a[x + 5 * y]!, R[x + 5 * y]!);
      }
    }
    for (let y = 0; y < 25; y += 5) {
      for (let x = 0; x < 5; x++) a[y + x] = b[y + x]! ^ (~b[y + ((x + 1) % 5)]! & MASK & b[y + ((x + 2) % 5)]!);
    }
    a[0] = a[0]! ^ RC[round]!;
  }
}

export function keccak256(input: Uint8Array | string): Uint8Array {
  const data = typeof input === "string" ? new TextEncoder().encode(input) : input;
  const rate = 136;
  const padLen = rate - (data.length % rate);
  const msg = new Uint8Array(data.length + padLen);
  msg.set(data);
  msg[data.length] = 0x01;
  msg[msg.length - 1] = msg[msg.length - 1]! | 0x80;
  const state = new Array<bigint>(25).fill(0n);
  for (let off = 0; off < msg.length; off += rate) {
    for (let i = 0; i < rate / 8; i++) {
      let lane = 0n;
      for (let k = 7; k >= 0; k--) lane = (lane << 8n) | BigInt(msg[off + i * 8 + k]!);
      state[i] = state[i]! ^ lane;
    }
    keccakF(state);
  }
  const out = new Uint8Array(32);
  for (let i = 0; i < 4; i++) {
    let lane = state[i]!;
    for (let k = 0; k < 8; k++) {
      out[i * 8 + k] = Number(lane & 0xffn);
      lane >>= 8n;
    }
  }
  return out;
}

export function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** EIP-55 mixed-case checksum address. Throws on a malformed address. */
export function toChecksumAddress(addr: string): string {
  const a = addr.toLowerCase().replace(/^0x/, "");
  if (!/^[0-9a-f]{40}$/.test(a)) throw new Error("invalid address");
  const hash = toHex(keccak256(a));
  let out = "0x";
  for (let i = 0; i < 40; i++) out += parseInt(hash[i]!, 16) >= 8 ? a[i]!.toUpperCase() : a[i]!;
  return out;
}

export function isAddress(addr: unknown): addr is string {
  return typeof addr === "string" && /^0x[0-9a-fA-F]{40}$/.test(addr);
}

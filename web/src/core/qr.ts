// Minimal QR Code encoder (ISO/IEC 18004), byte mode, versions 1–40, ECC L/M/Q/H.
// Algorithm follows Project Nayuki's reference design. Used to show the TOTP otpauth:// URL as a QR code.
// Verified in web/tests/qr.test.mjs against an independent encoder (module-for-module).

import { svg } from "./ui.js";

export type Ecc = "L" | "M" | "Q" | "H";
const ECC_ORD: Record<Ecc, number> = { L: 0, M: 1, Q: 2, H: 3 };
const ECC_FORMAT_BITS: Record<Ecc, number> = { L: 1, M: 0, Q: 3, H: 2 };

const ECC_CODEWORDS_PER_BLOCK: number[][] = [
  [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
  [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
  [-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
  [-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
];
const NUM_ERROR_CORRECTION_BLOCKS: number[][] = [
  [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25],
  [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
  [-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68],
  [-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81],
];

function numRawDataModules(ver: number): number {
  let result = (16 * ver + 128) * ver + 64;
  if (ver >= 2) {
    const numAlign = Math.floor(ver / 7) + 2;
    result -= (25 * numAlign - 10) * numAlign - 55;
    if (ver >= 7) result -= 36;
  }
  return result;
}

function numDataCodewords(ver: number, ecc: Ecc): number {
  const e = ECC_ORD[ecc];
  return Math.floor(numRawDataModules(ver) / 8) - ECC_CODEWORDS_PER_BLOCK[e]![ver]! * NUM_ERROR_CORRECTION_BLOCKS[e]![ver]!;
}

function gfMul(x: number, y: number): number {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z & 0xff;
}

function rsDivisor(degree: number): number[] {
  const result: number[] = new Array(degree - 1).fill(0);
  result.push(1);
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < result.length; j++) {
      result[j] = gfMul(result[j]!, root);
      if (j + 1 < result.length) result[j] = result[j]! ^ result[j + 1]!;
    }
    root = gfMul(root, 0x02);
  }
  return result;
}

function rsRemainder(data: number[], divisor: number[]): number[] {
  const result: number[] = divisor.map(() => 0);
  for (const b of data) {
    const factor = b ^ result.shift()!;
    result.push(0);
    divisor.forEach((coef, i) => (result[i] = result[i]! ^ gfMul(coef, factor)));
  }
  return result;
}

function getBit(x: number, i: number): boolean {
  return ((x >>> i) & 1) !== 0;
}

export interface QrMatrix {
  version: number;
  size: number;
  mask: number;
  modules: boolean[][]; // [y][x], true = dark
}

/** Encodes `text` (UTF-8, byte mode). Options force a version/mask (testing). */
export function qrMatrix(text: string, ecc: Ecc = "M", opts: { version?: number; mask?: number } = {}): QrMatrix {
  const bytes = Array.from(new TextEncoder().encode(text));
  let ver = opts.version ?? 0;
  if (!ver) {
    for (let v = 1; v <= 40; v++) {
      const ccBits = v <= 9 ? 8 : 16;
      if (4 + ccBits + bytes.length * 8 <= numDataCodewords(v, ecc) * 8) {
        ver = v;
        break;
      }
    }
    if (!ver) throw new RangeError("QR: data too long");
  }
  const capacity = numDataCodewords(ver, ecc) * 8;
  // Bit stream
  const bits: number[] = [];
  const push = (val: number, len: number) => {
    for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1);
  };
  push(0b0100, 4);
  push(bytes.length, ver <= 9 ? 8 : 16);
  for (const b of bytes) push(b, 8);
  if (bits.length > capacity) throw new RangeError("QR: data too long for version");
  push(0, Math.min(4, capacity - bits.length));
  push(0, (8 - (bits.length % 8)) % 8);
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) push(pad, 8);
  const data: number[] = [];
  for (let i = 0; i < bits.length; i += 8) {
    let b = 0;
    for (let k = 0; k < 8; k++) b = (b << 1) | bits[i + k]!;
    data.push(b);
  }
  // ECC + interleave
  const e = ECC_ORD[ecc];
  const numBlocks = NUM_ERROR_CORRECTION_BLOCKS[e]![ver]!;
  const blockEccLen = ECC_CODEWORDS_PER_BLOCK[e]![ver]!;
  const rawCodewords = Math.floor(numRawDataModules(ver) / 8);
  const numShortBlocks = numBlocks - (rawCodewords % numBlocks);
  const shortBlockLen = Math.floor(rawCodewords / numBlocks);
  const divisor = rsDivisor(blockEccLen);
  const blocks: number[][] = [];
  for (let i = 0, k = 0; i < numBlocks; i++) {
    const dat = data.slice(k, k + shortBlockLen - blockEccLen + (i < numShortBlocks ? 0 : 1));
    k += dat.length;
    const eccBytes = rsRemainder(dat, divisor);
    if (i < numShortBlocks) dat.push(0);
    blocks.push(dat.concat(eccBytes));
  }
  const codewords: number[] = [];
  for (let i = 0; i < blocks[0]!.length; i++) {
    blocks.forEach((blk, j) => {
      if (i !== shortBlockLen - blockEccLen || j >= numShortBlocks) codewords.push(blk[i]!);
    });
  }
  // Matrix
  const size = ver * 4 + 17;
  const modules: boolean[][] = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  const isFn: boolean[][] = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  const setFn = (x: number, y: number, dark: boolean) => {
    modules[y]![x] = dark;
    isFn[y]![x] = true;
  };
  for (let i = 0; i < size; i++) {
    setFn(6, i, i % 2 === 0);
    setFn(i, 6, i % 2 === 0);
  }
  const finder = (x: number, y: number) => {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const dist = Math.max(Math.abs(dx), Math.abs(dy));
        const xx = x + dx, yy = y + dy;
        if (xx >= 0 && xx < size && yy >= 0 && yy < size) setFn(xx, yy, dist !== 2 && dist !== 4);
      }
    }
  };
  finder(3, 3);
  finder(size - 4, 3);
  finder(3, size - 4);
  const alignPos: number[] = [];
  if (ver > 1) {
    const numAlign = Math.floor(ver / 7) + 2;
    const step = Math.floor((ver * 8 + numAlign * 3 + 5) / (numAlign * 4 - 4)) * 2;
    alignPos.push(6);
    for (let pos = size - 7; alignPos.length < numAlign; pos -= step) alignPos.splice(1, 0, pos);
  }
  const na = alignPos.length;
  for (let i = 0; i < na; i++) {
    for (let j = 0; j < na; j++) {
      if ((i === 0 && j === 0) || (i === 0 && j === na - 1) || (i === na - 1 && j === 0)) continue;
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) setFn(alignPos[i]! + dx, alignPos[j]! + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    }
  }
  const drawFormat = (mask: number) => {
    const d = (ECC_FORMAT_BITS[ecc] << 3) | mask;
    let rem = d;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const fbits = ((d << 10) | rem) ^ 0x5412;
    for (let i = 0; i <= 5; i++) setFn(8, i, getBit(fbits, i));
    setFn(8, 7, getBit(fbits, 6));
    setFn(8, 8, getBit(fbits, 7));
    setFn(7, 8, getBit(fbits, 8));
    for (let i = 9; i < 15; i++) setFn(14 - i, 8, getBit(fbits, i));
    for (let i = 0; i < 8; i++) setFn(size - 1 - i, 8, getBit(fbits, i));
    for (let i = 8; i < 15; i++) setFn(8, size - 15 + i, getBit(fbits, i));
    setFn(8, size - 8, true);
  };
  drawFormat(0);
  if (ver >= 7) {
    let rem = ver;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const vbits = (ver << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const bit = getBit(vbits, i);
      const a = size - 11 + (i % 3), b = Math.floor(i / 3);
      setFn(a, b, bit);
      setFn(b, a, bit);
    }
  }
  // Codewords (zig-zag)
  let bi = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert++) {
      for (let j = 0; j < 2; j++) {
        const x = right - j;
        const upward = ((right + 1) & 2) === 0;
        const y = upward ? size - 1 - vert : vert;
        if (!isFn[y]![x] && bi < codewords.length * 8) {
          modules[y]![x] = getBit(codewords[bi >>> 3]!, 7 - (bi & 7));
          bi++;
        }
      }
    }
  }
  const applyMask = (m: number) => {
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        let inv: boolean;
        switch (m) {
          case 0: inv = (x + y) % 2 === 0; break;
          case 1: inv = y % 2 === 0; break;
          case 2: inv = x % 3 === 0; break;
          case 3: inv = (x + y) % 3 === 0; break;
          case 4: inv = (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0; break;
          case 5: inv = ((x * y) % 2) + ((x * y) % 3) === 0; break;
          case 6: inv = (((x * y) % 2) + ((x * y) % 3)) % 2 === 0; break;
          default: inv = (((x + y) % 2) + ((x * y) % 3)) % 2 === 0; break;
        }
        if (!isFn[y]![x] && inv) modules[y]![x] = !modules[y]![x];
      }
    }
  };
  let mask = opts.mask ?? -1;
  if (mask < 0) {
    let best = Infinity;
    for (let m = 0; m < 8; m++) {
      applyMask(m);
      drawFormat(m);
      const p = penalty(modules, size);
      if (p < best) {
        best = p;
        mask = m;
      }
      applyMask(m); // undo (XOR)
    }
  }
  applyMask(mask);
  drawFormat(mask);
  return { version: ver, size, mask, modules };
}

function penalty(mod: boolean[][], size: number): number {
  let result = 0;
  // Rule 1: runs of ≥5 same-colour modules in rows and columns
  for (let pass = 0; pass < 2; pass++) {
    for (let a = 0; a < size; a++) {
      let run = 1;
      for (let b = 1; b < size; b++) {
        const cur = pass === 0 ? mod[a]![b] : mod[b]![a];
        const prev = pass === 0 ? mod[a]![b - 1] : mod[b - 1]![a];
        if (cur === prev) {
          run++;
          if (run === 5) result += 3;
          else if (run > 5) result++;
        } else run = 1;
      }
    }
  }
  // Rule 2: 2x2 blocks
  for (let y = 0; y < size - 1; y++) {
    for (let x = 0; x < size - 1; x++) {
      const c = mod[y]![x];
      if (c === mod[y]![x + 1] && c === mod[y + 1]![x] && c === mod[y + 1]![x + 1]) result += 3;
    }
  }
  // Rule 3: finder-like 1:1:3:1:1 with 4 light modules on either side
  const pat1 = [true, false, true, true, true, false, true, false, false, false, false];
  const pat2 = [false, false, false, false, true, false, true, true, true, false, true];
  for (let pass = 0; pass < 2; pass++) {
    for (let a = 0; a < size; a++) {
      for (let b = 0; b + 11 <= size; b++) {
        let m1 = true, m2 = true;
        for (let k = 0; k < 11; k++) {
          const v = pass === 0 ? mod[a]![b + k] : mod[b + k]![a];
          if (v !== pat1[k]) m1 = false;
          if (v !== pat2[k]) m2 = false;
          if (!m1 && !m2) break;
        }
        if (m1) result += 40;
        if (m2) result += 40;
      }
    }
  }
  // Rule 4: dark/light balance
  let dark = 0;
  for (const row of mod) for (const c of row) if (c) dark++;
  const total = size * size;
  const k = Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1;
  result += Math.max(0, k) * 10;
  return result;
}

/** Renders a QR code as an SVG (always dark-on-light for scanners, in both themes). */
export function qrSvg(textValue: string, opts: { ecc?: Ecc; size?: number; label?: string } = {}): SVGSVGElement {
  const q = qrMatrix(textValue, opts.ecc ?? "M");
  const quiet = 4;
  const dim = q.size + quiet * 2;
  let d = "";
  for (let y = 0; y < q.size; y++) {
    for (let x = 0; x < q.size; x++) if (q.modules[y]![x]) d += `M${x + quiet} ${y + quiet}h1v1h-1z`;
  }
  const px = opts.size ?? 208;
  return svg("svg", {
    class: "qr", viewBox: `0 0 ${dim} ${dim}`, width: px, height: px, role: "img",
    "aria-label": opts.label ?? "QR code", "shape-rendering": "crispEdges",
  }, svg("rect", { class: "qr-bg", x: 0, y: 0, width: dim, height: dim }), svg("path", { class: "qr-fg", d })) as SVGSVGElement;
}

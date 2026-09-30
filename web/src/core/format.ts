// Display formatting. Money is integer micro-USD (SPEC §1): 1 USD = 1_000_000.
// All money maths uses BigInt — never floats. Display truncates toward zero (never overstates).

const MICRO = 1_000_000n;

export type MicroLike = number | bigint | string;

/** Converts a micro-USD value to BigInt. Throws on non-integers (a money value must never be fractional micro). */
export function toMicro(v: MicroLike): bigint {
  if (typeof v === "bigint") return v;
  if (typeof v === "number") {
    if (!Number.isFinite(v) || !Number.isInteger(v)) throw new RangeError("micro-USD must be an integer");
    return BigInt(v);
  }
  const s = String(v).trim();
  if (!/^-?\d+$/.test(s)) throw new RangeError("micro-USD must be an integer string");
  return BigInt(s);
}

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 1234560000 → "$1,234.56". Truncates toward zero. `sign` adds "+" for positives. `compact` → "$1.2M". */
export function fmtUsd(micro: MicroLike | null | undefined, opts: { cents?: boolean; sign?: boolean; compact?: boolean } = {}): string {
  if (micro === null || micro === undefined || micro === "") return "—";
  let m: bigint;
  try {
    m = toMicro(micro);
  } catch {
    return "—";
  }
  const neg = m < 0n;
  const abs = neg ? -m : m;
  const prefix = neg ? "−" : opts.sign && abs > 0n ? "+" : "";
  if (opts.compact && abs >= 1_000_000n * MICRO / 1000n) {
    // ≥ $1,000 → compact with 1 decimal, truncated
    const units: [bigint, string][] = [
      [1_000_000_000n * MICRO, "B"],
      [1_000_000n * MICRO, "M"],
      [1_000n * MICRO, "K"],
    ];
    for (const [size, suffix] of units) {
      if (abs >= size) {
        const tenths = (abs * 10n) / size;
        const whole = tenths / 10n;
        const frac = tenths % 10n;
        return `${prefix}$${groupThousands(whole.toString())}${frac ? "." + frac.toString() : ""}${suffix}`;
      }
    }
  }
  const cents = opts.cents !== false;
  const dollars = abs / MICRO;
  if (!cents) return `${prefix}$${groupThousands(dollars.toString())}`;
  const c = (abs % MICRO) / 10_000n;
  return `${prefix}$${groupThousands(dollars.toString())}.${c.toString().padStart(2, "0")}`;
}

/** Exact decimal string, trailing zeros trimmed: 1234567 → "1.234567", 25000000 → "25". */
export function microToDecimal(micro: MicroLike): string {
  const m = toMicro(micro);
  const neg = m < 0n;
  const abs = neg ? -m : m;
  const whole = abs / MICRO;
  const frac = (abs % MICRO).toString().padStart(6, "0").replace(/0+$/, "");
  return `${neg ? "-" : ""}${whole.toString()}${frac ? "." + frac : ""}`;
}

/** "12.34" / "$1,200.5" → micro BigInt. Null for invalid, negative, or more than 6 decimals. */
export function parseUsdToMicro(input: string): bigint | null {
  const s = String(input).trim().replace(/^\$/, "").replace(/,/g, "");
  const m = /^(\d{1,12})(?:\.(\d{0,6}))?$/.exec(s);
  if (!m) return null;
  const whole = BigInt(m[1]!);
  const frac = BigInt((m[2] ?? "").padEnd(6, "0") || "0");
  return whole * MICRO + frac;
}

function trimNum(n: number, digits: number): string {
  return n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

/** Basis points → percent: 150 → "1.5%", 1500 → "15%". */
export function fmtBps(bps: number | null | undefined, digits = 2): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) return "—";
  return `${trimNum(bps / 100, digits)}%`;
}

/** Tenths of a basis point (Hyperliquid builder fee `f`) → percent: 100 → "0.1%". */
export function fmtTenthsBp(t: number): string {
  return `${trimNum(t / 1000, 4)}%`;
}

/** A value already in percent: 12.3456 → "12.35%". */
export function fmtPct(value: number | null | undefined, opts: { sign?: boolean; digits?: number } = {}): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const d = opts.digits ?? 2;
  const s = Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const prefix = value < 0 ? "−" : opts.sign && value > 0 ? "+" : "";
  return `${prefix}${s}%`;
}

export function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const s = Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: digits });
  return n < 0 ? `−${s}` : s;
}

/** Leverage stored ×100: 200 → "2×", 150 → "1.5×". */
export function fmtLeverage(x100: number): string {
  return `${trimNum(x100 / 100, 2)}×`;
}

/** "0x1234567890abcdef…" → "0x1234…cdef". */
export function shortAddr(addr: string | null | undefined, lead = 6, tail = 4): string {
  if (!addr) return "—";
  const a = String(addr);
  return a.length <= lead + tail + 1 ? a : `${a.slice(0, lead)}…${a.slice(-tail)}`;
}

function toDate(v: string | number | Date): Date | null {
  const d = v instanceof Date ? v : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "30 Sep 2026" (UTC). */
export function fmtDate(v: string | number | Date | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const d = toDate(v);
  if (!d) return "—";
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** "30 Sep 2026, 14:05 UTC". */
export function fmtDateTime(v: string | number | Date | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const d = toDate(v);
  if (!d) return "—";
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${fmtDate(d)}, ${hh}:${mm} UTC`;
}

/** "just now", "5 min ago", "3 h ago", "2 d ago", else the date. Future → "in 3 h". */
export function fmtRelative(v: string | number | Date | null | undefined, now = Date.now()): string {
  if (v === null || v === undefined) return "—";
  const d = toDate(v);
  if (!d) return "—";
  const diff = now - d.getTime();
  const future = diff < 0;
  const s = Math.abs(diff) / 1000;
  let out: string;
  if (s < 45) return "just now";
  else if (s < 3600) out = `${Math.round(s / 60)} min`;
  else if (s < 86400) out = `${Math.round(s / 3600)} h`;
  else if (s < 86400 * 30) out = `${Math.round(s / 86400)} d`;
  else return fmtDate(d);
  return future ? `in ${out}` : `${out} ago`;
}

// Pure helpers for the no-code builder spec (SPEC §10). The server (sandbox/nocode.py) is the
// authority: it compiles the spec to Python and runs the same validator. These checks only give
// the creator fast feedback before upload.

import type { IndicatorKind, NoCodeSpec, Operand, PriceSource } from "./types.js";

export const INDICATOR_KINDS: { kind: IndicatorKind; label: string; usesSource: boolean }[] = [
  { kind: "sma", label: "SMA — simple moving average", usesSource: true },
  { kind: "ema", label: "EMA — exponential moving average", usesSource: true },
  { kind: "rsi", label: "RSI — relative strength index", usesSource: true },
  { kind: "atr", label: "ATR — average true range", usesSource: false },
  { kind: "highest", label: "Highest value over N bars", usesSource: true },
  { kind: "lowest", label: "Lowest value over N bars", usesSource: true },
  { kind: "roc", label: "ROC — rate of change %", usesSource: true },
];

export const PRICE_SOURCES: { src: PriceSource; label: string }[] = [
  { src: "c", label: "close" },
  { src: "o", label: "open" },
  { src: "h", label: "high" },
  { src: "l", label: "low" },
  { src: "v", label: "volume" },
];

export const MAX_MARKETS = 5;
export const PLATFORM_MAX_LEVERAGE = 5;

export function defaultSpec(coin = "BTC"): NoCodeSpec {
  return {
    version: 1,
    markets: [coin],
    timeframe: "1d",
    lookback: 300,
    max_leverage: 1,
    indicators: [
      { id: "fast", kind: "sma", coin, source: "c", period: 20 },
      { id: "slow", kind: "sma", coin, source: "c", period: 100 },
    ],
    rules: [
      {
        coin,
        combine: "all",
        conditions: [{ left: { ref: "fast" }, op: ">", right: { ref: "slow" } }],
        weight: 1,
      },
    ],
    default_weight: 0,
  };
}

export function operandLabel(o: Operand): string {
  if ("ref" in o) return o.ref;
  if ("price" in o) return `${o.coin} ${o.price}`;
  return String(o.const);
}

export function validateSpec(spec: NoCodeSpec): string[] {
  const errs: string[] = [];
  if (spec.markets.length < 1 || spec.markets.length > MAX_MARKETS) errs.push(`Choose 1–${MAX_MARKETS} markets.`);
  if (new Set(spec.markets).size !== spec.markets.length) errs.push("Markets must be unique.");
  if (!["1h", "4h", "1d"].includes(spec.timeframe)) errs.push("Timeframe must be 1h, 4h or 1d.");
  if (!Number.isInteger(spec.lookback) || spec.lookback < 50 || spec.lookback > 1000) errs.push("Lookback must be 50–1000 bars.");
  if (!Number.isInteger(spec.max_leverage) || spec.max_leverage < 1 || spec.max_leverage > PLATFORM_MAX_LEVERAGE)
    errs.push(`Max leverage must be a whole number 1–${PLATFORM_MAX_LEVERAGE}.`);
  const ids = new Set<string>();
  for (const ind of spec.indicators) {
    if (!/^[a-z][a-z0-9_]{0,23}$/.test(ind.id)) errs.push(`Indicator name "${ind.id}" must be lower-case letters, digits or _ (max 24).`);
    if (ids.has(ind.id)) errs.push(`Indicator name "${ind.id}" is used twice.`);
    ids.add(ind.id);
    if (!spec.markets.includes(ind.coin)) errs.push(`Indicator "${ind.id}" uses ${ind.coin}, which is not in the markets list.`);
    if (!Number.isInteger(ind.period) || ind.period < 2 || ind.period > spec.lookback) errs.push(`Indicator "${ind.id}" period must be 2–lookback (${spec.lookback}).`);
  }
  const checkOperand = (o: Operand, where: string): void => {
    if ("ref" in o && !ids.has(o.ref)) errs.push(`${where}: unknown indicator "${o.ref}".`);
    if ("price" in o && !spec.markets.includes(o.coin)) errs.push(`${where}: ${o.coin} is not in the markets list.`);
    if ("const" in o && !Number.isFinite(o.const)) errs.push(`${where}: number is not valid.`);
  };
  if (spec.rules.length === 0) errs.push("Add at least one rule.");
  spec.rules.forEach((r, i) => {
    const where = `Rule ${i + 1}`;
    if (!spec.markets.includes(r.coin)) errs.push(`${where}: ${r.coin} is not in the markets list.`);
    if (r.conditions.length === 0) errs.push(`${where}: add at least one condition.`);
    r.conditions.forEach((c, j) => {
      checkOperand(c.left, `${where}, condition ${j + 1}`);
      checkOperand(c.right, `${where}, condition ${j + 1}`);
    });
    if (!Number.isFinite(r.weight) || Math.abs(r.weight) > spec.max_leverage) errs.push(`${where}: |weight| must be ≤ max leverage (${spec.max_leverage}).`);
  });
  if (!Number.isFinite(spec.default_weight) || Math.abs(spec.default_weight) > spec.max_leverage) errs.push("Default weight must be within max leverage.");
  // Σ|w| across coins at worst case: sum of max |weight| per coin
  const worst = spec.markets.reduce((acc, coin) => {
    const ws = spec.rules.filter((r) => r.coin === coin).map((r) => Math.abs(r.weight));
    return acc + Math.max(Math.abs(spec.default_weight), ...ws, 0);
  }, 0);
  if (worst > spec.max_leverage + 1e-9) errs.push(`Worst-case total |weight| across markets (${worst}) exceeds max leverage (${spec.max_leverage}).`);
  return errs;
}

/** Basic client-side sanity checks for an uploaded Python script (the sandbox is authoritative). */
export function precheckPython(src: string): string[] {
  const errs: string[] = [];
  const bytes = new TextEncoder().encode(src).length;
  if (bytes === 0) errs.push("The script is empty.");
  if (bytes > 64 * 1024) errs.push("The script is larger than 64 KB.");
  if (!/^\s*MARKETS\s*=/m.test(src)) errs.push("Missing MARKETS = [...].");
  if (!/^\s*TIMEFRAME\s*=/m.test(src)) errs.push('Missing TIMEFRAME = "1h" | "4h" | "1d".');
  if (!/^\s*LOOKBACK\s*=/m.test(src)) errs.push("Missing LOOKBACK = 50–1000.");
  if (!/^\s*MAX_LEVERAGE\s*=/m.test(src)) errs.push("Missing MAX_LEVERAGE = 1–5.");
  if (!/^def\s+signal\s*\(\s*bars\s*\)\s*:/m.test(src)) errs.push("Missing def signal(bars):");
  const imports = src.match(/^\s*(?:from\s+(\S+)\s+import|import\s+([^\n#]+))/gm) ?? [];
  for (const line of imports) {
    const mods = line.replace(/^\s*from\s+/, "").replace(/^\s*import\s+/, "").split(/[\s,]+/).filter(Boolean);
    const first = mods[0]?.split(".")[0];
    if (first && first !== "math" && first !== "statistics") errs.push(`Import not allowed: ${first} (only math, statistics).`);
  }
  if (/\b(open|eval|exec|compile|__import__|globals|locals|getattr|setattr|delattr|vars|dir)\s*\(/.test(src)) errs.push("Uses a forbidden builtin (open/eval/exec/compile/getattr/…).");
  if (/\.\s*_/.test(src)) errs.push("Attribute names starting with _ are not allowed.");
  return errs;
}

export const PYTHON_TEMPLATE = `MARKETS = ["BTC"]        # 1–5 Hyperliquid perp coins, e.g. "xyz:SILVER"
TIMEFRAME = "1d"          # "1h" | "4h" | "1d"
LOOKBACK = 300            # bars supplied per coin (50–1000)
MAX_LEVERAGE = 1          # 1–5; per-coin |weight| and sum of |weights| must stay within this

import math


def sma(xs, n):
    return sum(xs[-n:]) / n


def signal(bars):
    # bars: {coin: [{"t","o","h","l","c","v"}, ...]} oldest -> newest; last = last CLOSED bar
    closes = [b["c"] for b in bars["BTC"]]
    fast = sma(closes, 20)
    slow = sma(closes, 100)
    return {"BTC": 1.0 if fast > slow else 0.0}
`;

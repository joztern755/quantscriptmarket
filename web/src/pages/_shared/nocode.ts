// No-code builder spec helpers (SPEC §10). The JSON produced here is EXACTLY the format that
// backend/app/sandbox/nocode.py validate_spec/compile_spec accept; `validateSpec` mirrors validate_spec rule for
// rule (same paths and limits) so the creator gets the server's verdict before uploading. The server stays the
// authority (it compiles the spec to Python and runs the same validator as uploaded scripts).
// Cross-tested against the Python module by web/tests/nocode_contract.mjs.

import type { IndicatorKind, NcCondition, NcIndicator, NcOperand, NcOp, NcRule, NcSource, NcWhen, NoCodeSpec } from "./types.js";

export const INDICATOR_KINDS: { kind: IndicatorKind; label: string; usesSource: boolean }[] = [
  { kind: "sma", label: "SMA — simple moving average", usesSource: true },
  { kind: "ema", label: "EMA — exponential moving average", usesSource: true },
  { kind: "rsi", label: "RSI — relative strength index (Wilder)", usesSource: true },
  { kind: "atr", label: "ATR — average true range (Wilder)", usesSource: false },
  { kind: "highest", label: "Highest value over N bars", usesSource: true },
  { kind: "lowest", label: "Lowest value over N bars", usesSource: true },
  { kind: "roc", label: "ROC — rate of change %", usesSource: true },
];

/** Indicator sources (nocode.py SOURCES). */
export const SOURCES: NcSource[] = ["close", "high", "low", "open"];
/** Price fields usable directly as operands (nocode.py PRICE_FIELDS; last closed bar). */
export const PRICE_FIELDS = ["close", "open", "high", "low", "volume"] as const;
export const OPS: { op: NcOp; label: string }[] = [
  { op: ">", label: ">" },
  { op: ">=", label: "≥" },
  { op: "<", label: "<" },
  { op: "<=", label: "≤" },
  { op: "crosses_above", label: "crosses above" },
  { op: "crosses_below", label: "crosses below" },
];

// Limits — same values as backend/app/sandbox/validate.py + nocode.py.
export const MAX_MARKETS = 5;
export const PLATFORM_MAX_LEVERAGE = 5;
export const MIN_LOOKBACK = 50;
export const MAX_LOOKBACK = 1000;
export const MAX_INDICATORS = 20;
export const MAX_RULES = 20;
export const MAX_ITEMS = 20;
export const MAX_DEPTH = 3;
export const MAX_PERIOD = 500;
export const MAX_SHIFT = 50;
export const TIMEFRAMES = ["1h", "4h", "1d"] as const;
const ID_RE = /^[a-z][a-z0-9_]{0,31}$/;
const COIN_RE = /^(?:[a-z][a-z0-9]{0,15}:)?[A-Za-z0-9]{1,20}$/; // sandbox/validate.py _COIN_RE
const SPEC_KEYS = new Set(["version", "markets", "timeframe", "lookback", "max_leverage", "indicators", "rules", "default_weight", "name", "description"]);

export function defaultSpec(coin = "BTC"): NoCodeSpec {
  return {
    version: 1,
    markets: [coin],
    timeframe: "1d",
    lookback: 300,
    max_leverage: 1,
    indicators: {
      fast: { type: "sma", source: "close", period: 20 },
      slow: { type: "sma", source: "close", period: 100 },
    },
    rules: [{ when: { all: [{ left: "fast", op: ">", right: "slow" }] }, weight: 1 }],
    default_weight: 0,
  };
}

export function isCondition(x: NcCondition | NcWhen): x is NcCondition {
  return typeof x === "object" && x !== null && "op" in x;
}

export function whenItems(w: NcWhen): { combine: "all" | "any"; items: (NcCondition | NcWhen)[] } {
  return "all" in w ? { combine: "all", items: w.all } : { combine: "any", items: w.any };
}

export function operandLabel(o: NcOperand): string {
  return typeof o === "number" ? String(o) : o;
}

export function opLabel(op: NcOp): string {
  return OPS.find((o) => o.op === op)?.label ?? op;
}

const isNum = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);
const isInt = (x: unknown): x is number => typeof x === "number" && Number.isInteger(x);

export interface SpecError {
  path: string;
  message: string;
}

/** Mirror of nocode.py validate_spec (paths/limits identical). Empty list = the server will accept the spec. */
export function validateSpecErrors(spec: unknown): SpecError[] {
  const errs: SpecError[] = [];
  const e = (path: string, message: string): void => {
    if (errs.length < 100) errs.push({ path, message });
  };
  if (typeof spec !== "object" || spec === null || Array.isArray(spec)) {
    e("", "spec must be a JSON object");
    return errs;
  }
  const sp = spec as Record<string, unknown>;
  for (const k of Object.keys(sp)) if (!SPEC_KEYS.has(k)) e(k.slice(0, 40), "unknown key");
  if ("version" in sp && sp.version !== 1) e("version", "only version 1 is supported");

  let markets = sp.markets;
  if (!Array.isArray(markets) || markets.length < 1 || markets.length > MAX_MARKETS) {
    e("markets", `must be a list of 1–${MAX_MARKETS} coin names`);
    markets = [];
  } else {
    (markets as unknown[]).forEach((c, i) => {
      if (typeof c !== "string" || !COIN_RE.test(c)) e(`markets[${i}]`, "must be a Hyperliquid perp coin like 'BTC' or 'xyz:SILVER'");
    });
    if (new Set((markets as unknown[]).map(String)).size !== (markets as unknown[]).length) e("markets", "duplicate coins");
  }
  const mk = markets as unknown[];
  if (!(TIMEFRAMES as readonly unknown[]).includes(sp.timeframe)) e("timeframe", `must be one of ${TIMEFRAMES.join(", ")}`);
  let lookback = sp.lookback;
  if (!isInt(lookback) || lookback < MIN_LOOKBACK || lookback > MAX_LOOKBACK) {
    e("lookback", `must be an integer ${MIN_LOOKBACK}–${MAX_LOOKBACK}`);
    lookback = MAX_LOOKBACK;
  }
  let maxLev: number | null = sp.max_leverage as number;
  if (!isNum(maxLev) || maxLev < 1 || maxLev > PLATFORM_MAX_LEVERAGE) {
    e("max_leverage", `must be a number 1–${PLATFORM_MAX_LEVERAGE}`);
    maxLev = null;
  }

  let inds = "indicators" in sp ? sp.indicators : {};
  const ids = new Set<string>();
  if (typeof inds !== "object" || inds === null || Array.isArray(inds) || Object.keys(inds).length > MAX_INDICATORS) {
    e("indicators", `must be an object with at most ${MAX_INDICATORS} entries`);
    inds = {};
  }
  for (const [iid, ind] of Object.entries(inds as Record<string, unknown>)) {
    const p = `indicators.${iid.slice(0, 40)}`;
    if (!ID_RE.test(iid)) {
      e(p, "id must match [a-z][a-z0-9_]{0,31}");
      continue;
    }
    if ((PRICE_FIELDS as readonly string[]).includes(iid)) {
      e(p, `id '${iid}' is reserved for the price field`);
      continue;
    }
    ids.add(iid);
    if (typeof ind !== "object" || ind === null || Array.isArray(ind)) {
      e(p, "must be an object");
      continue;
    }
    const d = ind as Record<string, unknown>;
    for (const k of Object.keys(d)) if (!["type", "source", "period", "shift"].includes(k)) e(`${p}.${k.slice(0, 40)}`, "unknown key");
    const typ = d.type;
    if (!INDICATOR_KINDS.some((k) => k.kind === typ)) e(`${p}.type`, `must be one of ${INDICATOR_KINDS.map((k) => k.kind).join(", ")}`);
    if (typ === "atr") {
      if ("source" in d) e(`${p}.source`, "atr uses high/low/close; omit source");
    } else if (!(SOURCES as unknown[]).includes("source" in d ? d.source : "close")) {
      e(`${p}.source`, `must be one of ${SOURCES.join(", ")}`);
    }
    let period = d.period;
    if (!isInt(period) || period < 1 || period > MAX_PERIOD) {
      e(`${p}.period`, `must be an integer 1–${MAX_PERIOD}`);
      period = 1;
    }
    let shift = "shift" in d ? d.shift : 0;
    if (!isInt(shift) || shift < 0 || shift > MAX_SHIFT) {
      e(`${p}.shift`, `must be an integer 0–${MAX_SHIFT}`);
      shift = 0;
    }
    if ((period as number) + (shift as number) + 2 > (lookback as number)) {
      e(p, `period + shift + 2 must be ≤ lookback (${lookback}) so the value and a crossing can be computed`);
    }
  }

  const operand = (x: unknown, path: string): void => {
    if (typeof x === "string") {
      if (!ids.has(x) && !(PRICE_FIELDS as readonly string[]).includes(x)) e(path, `unknown indicator '${x.slice(0, 40)}' (define it under indicators, or use a price field / number)`);
    } else if (!isNum(x)) {
      e(path, "must be an indicator id, a price field, or a finite number");
    }
  };
  const when = (node: unknown, path: string, depth: number): void => {
    const keys = typeof node === "object" && node !== null && !Array.isArray(node) ? Object.keys(node) : [];
    if (keys.length !== 1 || (keys[0] !== "all" && keys[0] !== "any")) {
      e(path, 'must be {"all": [...]} or {"any": [...]}');
      return;
    }
    const key = keys[0];
    const items = (node as Record<string, unknown>)[key];
    if (!Array.isArray(items) || items.length < 1 || items.length > MAX_ITEMS) {
      e(`${path}.${key}`, `must be a list of 1–${MAX_ITEMS} conditions`);
      return;
    }
    items.forEach((it: unknown, i: number) => {
      const ip = `${path}.${key}[${i}]`;
      if (typeof it === "object" && it !== null && !Array.isArray(it) && ("all" in it || "any" in it)) {
        if (depth >= MAX_DEPTH) e(ip, `nesting deeper than ${MAX_DEPTH} is not allowed`);
        else when(it, ip, depth + 1);
        return;
      }
      const ks = typeof it === "object" && it !== null && !Array.isArray(it) ? Object.keys(it).sort().join(",") : "";
      if (ks !== "left,op,right") {
        e(ip, "condition must have exactly left, op, right");
        return;
      }
      const c = it as Record<string, unknown>;
      if (!OPS.some((o) => o.op === c.op)) e(`${ip}.op`, `must be one of ${OPS.map((o) => o.op).join(", ")}`);
      operand(c.left, `${ip}.left`);
      operand(c.right, `${ip}.right`);
      if (isNum(c.left) && isNum(c.right)) e(ip, "at least one side must be an indicator or price field");
    });
  };

  let rules = sp.rules;
  const weights: number[] = [];
  if (!Array.isArray(rules) || rules.length < 1 || rules.length > MAX_RULES) {
    e("rules", `must be a list of 1–${MAX_RULES} rules`);
    rules = [];
  }
  (rules as unknown[]).forEach((r, i) => {
    const p = `rules[${i}]`;
    const ks = typeof r === "object" && r !== null && !Array.isArray(r) ? Object.keys(r).sort().join(",") : "";
    if (ks !== "weight,when") {
      e(p, "rule must have exactly 'when' and 'weight'");
      return;
    }
    const rr = r as Record<string, unknown>;
    when(rr.when, `${p}.when`, 1);
    if (!isNum(rr.weight)) e(`${p}.weight`, "must be a finite number");
    else weights.push(rr.weight);
  });
  const dw = "default_weight" in sp ? sp.default_weight : 0;
  if (!isNum(dw)) e("default_weight", "must be a finite number");
  else weights.push(dw);
  if (maxLev !== null && mk.length && weights.length) {
    const worst = Math.max(...weights.map((w) => Math.abs(w)));
    if (worst > maxLev) e("rules", `a weight of ${worst} exceeds max_leverage ${maxLev}`);
    else if (worst * mk.length > maxLev + 1e-9) e("rules", `weights apply to each of ${mk.length} markets: ${mk.length} × ${worst} exceeds max_leverage ${maxLev}`);
  }
  return errs;
}

/** Human-readable errors for the builder UI. */
export function validateSpec(spec: NoCodeSpec): string[] {
  return validateSpecErrors(spec).map((x) => (x.path ? `${x.path}: ${x.message}` : x.message));
}

/** Clean copy to upload: drops `source` on atr, and `shift: 0` / empty optional keys (the server accepts both). */
export function toServerSpec(spec: NoCodeSpec): NoCodeSpec {
  const indicators: Record<string, NcIndicator> = {};
  for (const [id, ind] of Object.entries(spec.indicators)) {
    const out: NcIndicator = { type: ind.type, period: ind.period };
    if (ind.type !== "atr") out.source = ind.source ?? "close";
    if (ind.shift) out.shift = ind.shift;
    indicators[id] = out;
  }
  const rules: NcRule[] = spec.rules.map((r) => ({ when: structuredCloneJson(r.when), weight: r.weight }));
  return {
    version: 1,
    markets: [...spec.markets],
    timeframe: spec.timeframe,
    lookback: spec.lookback,
    max_leverage: spec.max_leverage,
    indicators,
    rules,
    default_weight: spec.default_weight,
  };
}

function structuredCloneJson<T>(x: T): T {
  return JSON.parse(JSON.stringify(x)) as T;
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

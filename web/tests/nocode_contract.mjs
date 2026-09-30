#!/usr/bin/env node
// Contract test: the no-code JSON produced by the web builder module (web/src/pages/_shared/nocode.ts, built to
// web/dist) must be EXACTLY what backend/app/sandbox/nocode.py accepts (SPEC §10).
//   node web/build.mjs && node web/tests/nocode_contract.mjs
// For every spec below: web validateSpecErrors() and Python validate_spec() must agree on valid/invalid (and on
// the error paths); every valid spec must compile (compile_spec → same validator as uploaded Python) and run once
// through the sandbox runner (run_signal) on synthetic bars, returning a weight from the spec for every market.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const MOD = join(HERE, "..", "dist", "app", "pages", "_shared", "nocode.js");
const BACKEND = join(HERE, "..", "..", "backend");
if (!existsSync(MOD)) {
  console.error("web/dist missing — run node web/build.mjs first");
  process.exit(2);
}
const nc = await import(pathToFileURL(MOD).href);

const clone = (x) => JSON.parse(JSON.stringify(x));
const base = nc.defaultSpec("BTC");

// Builder-shaped input (what the UI edits) → server spec.
const builderShaped = nc.toServerSpec({
  version: 1,
  markets: ["xyz:SILVER"],
  timeframe: "1d",
  lookback: 400,
  max_leverage: 2,
  indicators: {
    trend: { type: "ema", source: "close", period: 200, shift: 0 },
    hh: { type: "highest", source: "high", period: 20, shift: 1 },
    vol: { type: "atr", source: "close", period: 14 }, // source must be dropped for atr
    mom: { type: "roc", source: "close", period: 10 },
    osc: { type: "rsi", source: "close", period: 14 },
    lo: { type: "lowest", source: "low", period: 50 },
    op: { type: "sma", source: "open", period: 5 },
  },
  rules: [
    { when: { all: [{ left: "close", op: "crosses_above", right: "hh" }, { left: "close", op: ">", right: "trend" }] }, weight: 2 },
    { when: { any: [{ left: "osc", op: "<", right: 30 }, { all: [{ left: "mom", op: ">=", right: 0 }, { left: "vol", op: "<=", right: 5 }] }] }, weight: 1 },
    { when: { all: [{ left: "close", op: "crosses_below", right: "lo" }] }, weight: -1 },
    { when: { all: [{ left: "volume", op: ">", right: 0 }, { left: "op", op: "<", right: "high" }] }, weight: 0.5 },
  ],
  default_weight: 0,
});

const multi = nc.toServerSpec({ ...clone(base), markets: ["BTC", "SOL", "xyz:GOLD"], max_leverage: 3, lookback: 150, rules: [{ when: { all: [{ left: "fast", op: ">", right: "slow" }] }, weight: 1 }], default_weight: -1 });

const cases = [
  ["default BTC", base],
  ["default xyz:SILVER", nc.defaultSpec("xyz:SILVER")],
  ["builder-shaped, all indicator types, nested any/all, crosses", builderShaped],
  ["3 markets × |w| 1 ≤ 3", multi],
  ["4h, shift, negative default", { ...clone(base), timeframe: "4h", indicators: { ...clone(base.indicators), prev: { type: "sma", source: "close", period: 20, shift: 5 } }, rules: [{ when: { all: [{ left: "fast", op: ">", right: "prev" }] }, weight: 1 }], default_weight: -1 }],
  // invalid ones — both sides must reject
  ["weight above max_leverage", { ...clone(base), rules: [{ when: { all: [{ left: "fast", op: ">", right: "slow" }] }, weight: 2 }] }],
  ["markets × weight above max_leverage", { ...clone(multi), max_leverage: 2 }],
  ["unknown indicator", { ...clone(base), rules: [{ when: { all: [{ left: "nope", op: ">", right: "slow" }] }, weight: 1 }] }],
  ["period + shift + 2 > lookback", { ...clone(base), lookback: 100, indicators: { ...clone(base.indicators), big: { type: "sma", source: "close", period: 99 } } }],
  ["bad coin", { ...clone(base), markets: ["xyz-SILVER"] }],
  ["no rules", { ...clone(base), rules: [] }],
  ["atr with source", { ...clone(base), indicators: { ...clone(base.indicators), a: { type: "atr", source: "close", period: 14 } } }],
  ["two numbers compared", { ...clone(base), rules: [{ when: { all: [{ left: 1, op: ">", right: 0 }] }, weight: 1 }] }],
  ["unknown op", { ...clone(base), rules: [{ when: { all: [{ left: "fast", op: "==", right: "slow" }] }, weight: 1 }] }],
  ["nesting depth 4", { ...clone(base), rules: [{ when: { all: [{ any: [{ all: [{ any: [{ left: "fast", op: ">", right: "slow" }] }] }] }] }, weight: 1 }] }],
  ["reserved id", { ...clone(base), indicators: { close: { type: "sma", source: "close", period: 5 } } }],
  ["unknown top-level key (old web format)", { ...clone(base), indicators: [{ id: "fast", kind: "sma", coin: "BTC", source: "c", period: 20 }] }],
  ["lookback out of range", { ...clone(base), lookback: 20 }],
];

const payload = cases.map(([name, spec]) => ({ name, spec, web: nc.validateSpecErrors(spec) }));

const PY = String.raw`
import json, math, sys
sys.path.insert(0, sys.argv[1])
from app.sandbox.nocode import validate_spec, compile_spec
from app.sandbox.runner import run_signal
DAY = 86_400_000
cases = json.load(sys.stdin)
out = []
for c in cases:
    spec = c["spec"]
    errs = validate_spec(spec)
    r = {"name": c["name"], "py_paths": [e["path"] for e in errs], "compiled": False, "ran": False, "error": None}
    if not errs:
        try:
            src = compile_spec(spec)
            r["compiled"] = True
            n = max(int(spec["lookback"]), 60)
            bars = {}
            for k, coin in enumerate(spec["markets"]):
                rows = []
                for i in range(n):
                    px = 100.0 + 10.0 * math.sin((i + 7 * k) / 9.0) + i * 0.05
                    rows.append({"t": i * DAY, "o": px - 0.3, "h": px + 1.0, "l": px - 1.0, "c": px, "v": 1000.0 + i})
                bars[coin] = rows
            res = run_signal(src, bars)
            allowed = {float(x["weight"]) for x in spec["rules"]} | {float(spec.get("default_weight", 0))}
            ok = set(res.weights) == set(spec["markets"]) and all(float(w) in allowed for w in res.weights.values())
            r["ran"] = ok
            if not ok:
                r["error"] = "weights " + json.dumps(res.weights) + " not in " + json.dumps(sorted(allowed))
        except Exception as e:  # noqa: BLE001
            r["error"] = type(e).__name__ + ": " + str(e)[:300]
    out.append(r)
print(json.dumps(out))
`;

const py = spawnSync("python3.12", ["-c", PY, BACKEND], { input: JSON.stringify(payload), encoding: "utf8", cwd: BACKEND, timeout: 180_000 });
if (py.status !== 0) {
  console.error(py.stdout, py.stderr);
  process.exit(1);
}
const results = JSON.parse(py.stdout.trim().split("\n").pop());
let failures = 0;
for (let i = 0; i < results.length; i++) {
  const r = results[i];
  const web = payload[i].web.map((e) => e.path);
  const agree = (web.length === 0) === (r.py_paths.length === 0);
  const samePaths = JSON.stringify([...new Set(web)].sort()) === JSON.stringify([...new Set(r.py_paths)].sort());
  const validOk = r.py_paths.length > 0 || (r.compiled && r.ran);
  const ok = agree && samePaths && validOk;
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${r.name}${r.py_paths.length ? ` — rejected at ${[...new Set(r.py_paths)].join(", ")}` : " — compiled + ran"}${ok ? "" : ` | web=${JSON.stringify(web)} py=${JSON.stringify(r.py_paths)} compiled=${r.compiled} ran=${r.ran} ${r.error ?? ""}`}`);
}
console.log(`\n${results.length - failures}/${results.length} no-code contract checks passed`);
process.exit(failures ? 1 : 0);

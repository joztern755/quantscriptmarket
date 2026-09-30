#!/usr/bin/env node
'use strict';
// Deterministic synthetic input for the tests (no market data is committed). Weekday daily bars from 1968-01-02 (the
// SILVER manifest's first_bar) to the given end day, a seeded random walk with alternating bull and bear regimes, plus a
// synthetic M2 series that falls in some years (the SILVER live setting buys only while M2 is below its 20-day EMA).
//   node signals/testdata.js OUT.json [END_YYYY-MM-DD] [SEED]   -> {"silver": {"rows": [...], "ctx": {"M2": [...]}}}
const fs = require('fs');
const DAY = 864e5;

function mulberry32(a) { return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }

function synthetic({ end = '2026-09-29', seed = 4 } = {}) {
  const rnd = mulberry32(seed), gauss = () => { let u = 0, v = 0; while (!u) u = rnd(); v = rnd(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };
  const t0 = Date.UTC(1968, 0, 2), t1 = Date.parse(end + 'T00:00:00Z'), rows = [], m2 = [];
  let px = 2, m = 500, regimeLeft = 0, drift = 0;
  for (let t = t0; t <= t1; t += DAY) {
    const d = new Date(t), dow = d.getUTCDay(), y = d.getUTCFullYear();
    // M2: carried forward every calendar day (as the terminal stores FRED series); falls in years ending in 2, 3, 7 and 9
    m *= 1 + ([2, 3, 7, 9].includes(y % 10) ? -0.00012 : 0.00018);
    m2.push([t, m, m, m, m]);
    if (dow === 0 || dow === 6) continue;
    if (regimeLeft <= 0) { regimeLeft = 150 + Math.floor(rnd() * 700); drift = (rnd() < 0.55 ? 1 : -1) * (0.0006 + rnd() * 0.0022); }
    regimeLeft--;
    const o = px, c = px * Math.exp(drift + 0.017 * gauss()), h = Math.max(o, c) * (1 + Math.abs(0.006 * gauss())), l = Math.min(o, c) * (1 - Math.abs(0.006 * gauss()));
    rows.push([t, +o.toFixed(6), +h.toFixed(6), +l.toFixed(6), +c.toFixed(6), Math.round(1000 + 500 * rnd())]);
    px = c;
  }
  return { silver: { rows, ctx: { M2: m2 } } };
}

module.exports = { synthetic, mulberry32 };
if (require.main === module) {
  const [out, end, seed] = process.argv.slice(2);
  if (!out) { console.error('usage: node signals/testdata.js OUT.json [END] [SEED]'); process.exit(2); }
  fs.writeFileSync(out, JSON.stringify(synthetic({ end: end || undefined, seed: seed ? +seed : undefined })));
}

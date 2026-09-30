'use strict';
// Core of the signal adapter (SPEC §7, §10): runs a vendored CREST script over daily rows, reads its state at the last
// completed bar, builds the canonical signals payload and signs it with Ed25519. Zero dependencies (node:crypto, node:vm).
//
// Bar convention (same as the terminal): one row per UTC calendar day, t = 00:00 UTC of that day (ms), ascending. A bar
// dated D is complete once UTC day D has ended, i.e. at D+1 00:00 UTC; the last completed bar at time `now` is the last
// row with t <= floor(now / DAY) * DAY - DAY (crest/gen_multi.js `TO`).
//
// State (identical to the terminal's own alerts, alerts/notify.js posAfter): walk the trades in order; BUY opens LONG
// (lev 2 if the trade was a 2x entry, else 1), SELL closes to CASH, a TRIM whose reason starts with "leverage" (leverage
// off / leverage stop) drops lev to 1; other TRIMs (partial take-profit) keep LONG. target_weight = CASH 0, LONG lev.
const fs = require('fs'), path = require('path'), crypto = require('crypto'), vm = require('vm');

const DAY = 864e5;
const VENDOR_DIR = path.join(__dirname, 'vendor');
const PRINTABLE_ASCII = /^[\x20-\x7e]*$/;
const TOP_KEYS = ['as_of', 'engine_sha256', 'generated_at', 'strategies'];
const STRATEGY_KEYS = ['last_action', 'last_action_date', 'market', 'script_sha256', 'status', 'target_weight'];

// ---------- canonical JSON (the exact bytes that are signed and served) ----------
// Sorted keys (code-unit order; keys are printable ASCII so this equals Python's sort), no whitespace, only safe
// integers, printable-ASCII strings, null and booleans. Anything else throws, so Node and Python produce the same bytes
// (Python: json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)).
function canonicalJson(v) {
  if (v === null) return 'null';
  if (v === true || v === false) return String(v);
  if (typeof v === 'number') {
    if (!Number.isSafeInteger(v)) throw new Error(`canonical JSON: only safe integers are allowed (got ${v})`);
    return String(v === 0 ? 0 : v);   // -0 -> 0
  }
  if (typeof v === 'string') {
    if (!PRINTABLE_ASCII.test(v)) throw new Error('canonical JSON: strings must be printable ASCII');
    return JSON.stringify(v);
  }
  if (Array.isArray(v)) return '[' + v.map(canonicalJson).join(',') + ']';
  if (typeof v === 'object') {
    const keys = Object.keys(v).sort();
    return '{' + keys.map(k => {
      if (!PRINTABLE_ASCII.test(k)) throw new Error('canonical JSON: keys must be printable ASCII');
      if (v[k] === undefined) throw new Error(`canonical JSON: undefined value at key ${k}`);
      return JSON.stringify(k) + ':' + canonicalJson(v[k]);
    }).join(',') + '}';
  }
  throw new Error(`canonical JSON: unsupported type ${typeof v}`);
}

const sha256 = buf => crypto.createHash('sha256').update(buf).digest('hex');
const ymd = t => new Date(t).toISOString().slice(0, 10);
const lastCompletedBarT = nowMs => Math.floor(nowMs / DAY) * DAY - DAY;

// ---------- vendored scripts ----------
function loadManifest(dir = VENDOR_DIR) {
  const m = JSON.parse(fs.readFileSync(path.join(dir, 'MANIFEST.json'), 'utf8'));
  if (m.schema !== 1 || !m.scripts || typeof m.scripts !== 'object') throw new Error('vendor/MANIFEST.json: unexpected schema');
  return m;
}

// Loads vendor/<file> only if its bytes hash to the manifest's sha256, and evaluates exactly those bytes (no second read).
function loadScript(key, { dir = VENDOR_DIR, manifest = loadManifest(dir) } = {}) {
  const spec = manifest.scripts[key];
  if (!spec) throw new Error(`strategy "${key}" is not in vendor/MANIFEST.json`);
  const file = path.join(dir, spec.file), buf = fs.readFileSync(file), got = sha256(buf);
  if (got !== spec.sha256) throw new Error(`${spec.file}: sha256 ${got} does not match MANIFEST ${spec.sha256} (never edit a vendored script; re-vendor it)`);
  const mod = { exports: {} };
  vm.runInThisContext('(function (module, exports) {' + buf.toString('utf8') + '\n})', { filename: file })(mod, mod.exports);
  const api = mod.exports;
  if (!api || typeof api.run !== 'function' || !api.SETTINGS) throw new Error(`${spec.file}: does not export {run, SETTINGS}`);
  // the manifest's ctx_refs must be exactly the cross-market references the script's live setting reads
  const refs = ['', ...Array.from({ length: 14 }, (_, k) => String(k + 2))].map(s => api.SETTINGS['ctxRef' + s]).filter(Boolean).sort();
  const want = [...(spec.ctx_refs || [])].sort();
  if (refs.join(',') !== want.join(',')) throw new Error(`${key}: script reads ctx [${refs}] but MANIFEST lists [${want}]`);
  if (api.SETTINGS.wfBy) throw new Error(`${key}: group scripts (wfBy) are not supported by this adapter yet`);
  return { key, spec, api, sha256: got };
}

// ---------- rows ----------
// Accepts [t,o,h,l,c(,v)] arrays or {t,o,h,l,c,v} objects; returns fresh objects. Throws on anything the bar convention
// does not allow (non-midnight t, unsorted or duplicate days, non-finite or non-positive close).
function normalizeRows(input, what) {
  if (!Array.isArray(input)) throw new Error(`${what}: rows must be an array`);
  const out = input.map((r, i) => {
    const o = Array.isArray(r) ? { t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r.length > 5 && r[5] != null ? r[5] : 0 } : { t: r.t, o: r.o, h: r.h, l: r.l, c: r.c, v: r.v == null ? 0 : r.v };
    if (!Number.isSafeInteger(o.t) || o.t % DAY !== 0) throw new Error(`${what}: row ${i} t=${o.t} is not a UTC midnight (daily bars only)`);
    for (const k of ['o', 'h', 'l', 'c', 'v']) if (typeof o[k] !== 'number' || !Number.isFinite(o[k])) throw new Error(`${what}: row ${i} (${ymd(o.t)}) ${k} is not a finite number`);
    if (!(o.c > 0)) throw new Error(`${what}: row ${i} (${ymd(o.t)}) close must be > 0`);
    if (i > 0 && !(o.t > (Array.isArray(input[i - 1]) ? input[i - 1][0] : input[i - 1].t))) throw new Error(`${what}: rows must be strictly ascending by day (row ${i}, ${ymd(o.t)})`);
    return o;
  });
  return out;
}

// ---------- state ----------
function posAfter(trades) {
  let p = 'CASH', lev = 1, last = null;
  for (const x of trades) {
    if (x.side === 'BUY') { p = 'LONG'; lev = x.lev > 1 ? 2 : 1; }
    else if (x.side === 'SELL') { p = 'CASH'; lev = 1; }
    else if (x.side === 'TRIM' && /^leverage/.test(x.why || '')) lev = 1;
    else if (x.side !== 'TRIM') throw new Error(`unknown trade side ${x.side}`);
    last = x;
  }
  return {
    position: p,
    target_weight: p === 'LONG' ? lev : 0,
    last_action: last ? last.side : 'NONE',
    last_action_date: last ? last.date : null,
  };
}

// ---------- one strategy ----------
// rows: the script's full history (normalizeRows output), ctx: {REF: rows}. Cuts both at the last completed bar for `now`,
// validates what the live setting needs, runs the vendored script exactly as crest/gen_multi.js does (params {wfId}), and
// returns the state at the last bar. opts.maxLagDays: the last bar may be at most this many days before the last completed
// day (weekends and holidays; the terminal's check_data.js uses 5 for TradFi).
function computeStrategy(script, rowsIn, ctxIn, { now = Date.now(), maxLagDays = 5, ctxMaxLagDays = 7 } = {}) {
  const { key, spec, api } = script, TO = lastCompletedBarT(now);
  const rows = normalizeRows(rowsIn, `${key} rows`).filter(r => r.t <= TO);
  if (rows.length < 400) throw new Error(`${key}: only ${rows.length} completed bars (need the full history)`);
  // the live setting buys at the first bar of the data (dayOne) and its re-entry rules remember every earlier trade, so a
  // history that does not start where the fit started gives a different state: refuse it
  if (spec.first_bar && ymd(rows[0].t) !== spec.first_bar) throw new Error(`${key}: history starts ${ymd(rows[0].t)}, the live setting needs it from ${spec.first_bar}`);
  const lastT = rows[rows.length - 1].t;
  if (lastT < TO - maxLagDays * DAY) throw new Error(`${key}: last bar ${ymd(lastT)} is more than ${maxLagDays} days before the last completed day ${ymd(TO)} (stale data)`);
  const ctx = {};
  for (const ref of spec.ctx_refs || []) {
    if (!ctxIn || !ctxIn[ref]) throw new Error(`${key}: cross-market series ${ref} is missing (the script would silently drop its filter)`);
    const r = normalizeRows(ctxIn[ref], `${key} ctx ${ref}`).filter(x => x.t <= TO);
    if (r.length < 3 || r[0].t >= lastT) throw new Error(`${key}: cross-market series ${ref} has no usable history before ${ymd(lastT)}`);
    if (r[r.length - 1].t < lastT - ctxMaxLagDays * DAY) throw new Error(`${key}: cross-market series ${ref} ends ${ymd(r[r.length - 1].t)}, more than ${ctxMaxLagDays} days before ${ymd(lastT)}`);
    ctx[ref] = r;
  }
  const params = { wfId: spec.ticker };
  const res = api.run(rows, params, ctx), again = api.run(rows, params, ctx);
  if (res.final !== again.final || res.trades.length !== again.trades.length) throw new Error(`${key}: two identical runs differ (non-deterministic)`);
  if (res.bars !== rows.length || res.last !== ymd(lastT)) throw new Error(`${key}: the script dropped rows (${res.bars} of ${rows.length})`);
  if (res.liquidated) throw new Error(`${key}: the live run is liquidated; refusing to emit`);
  const st = posAfter(res.trades);
  if ((st.position === 'LONG') !== (res.position === 'LONG')) throw new Error(`${key}: trade list (${st.position}) and script position (${res.position}) disagree`);
  if (st.target_weight > (spec.max_weight || 2)) throw new Error(`${key}: weight ${st.target_weight} above max ${spec.max_weight}`);
  return { key, as_of: res.last, rows, ctx, result: res, state: st };
}

// ---------- no-look-ahead self-test ----------
// For every cut index i: running the script on rows[0..i] (and the ctx rows up to that day) must give the same state
// (position, weight, last action and date) and the same equity at bar i as the full run read at bar i.
function defaultCuts(n, trades, { tail = 20, spread = 40 } = {}) {
  const s = new Set();
  for (const t of trades) for (const d of [-1, 0, 1]) s.add(t.i + d);
  for (let k = 1; k <= tail; k++) s.add(n - k);
  for (let k = 1; k <= spread; k++) s.add(Math.floor((n - 1) * k / (spread + 1)));
  return [...s].filter(i => i >= 3 && i < n).sort((a, b) => a - b);
}
// opts.params: extra engine params merged over {wfId} (tests use it to exercise leverage and partial sales on the same engine)
function selfTestNoLookahead(script, rows, ctx, { cuts, params: extra } = {}) {
  const params = { wfId: script.spec.ticker, ...(extra || {}) }, full = script.api.run(rows, params, ctx);
  cuts = cuts || defaultCuts(rows.length, full.trades);
  const failures = [];
  for (const i of cuts) {
    const T = rows[i].t, sub = {};
    for (const [k, r] of Object.entries(ctx)) sub[k] = r.filter(x => x.t <= T);
    const cut = script.api.run(rows.slice(0, i + 1), params, sub);
    const a = posAfter(full.trades.filter(t => t.i <= i)), b = posAfter(cut.trades);
    const same = a.target_weight === b.target_weight && a.position === b.position && a.last_action === b.last_action && a.last_action_date === b.last_action_date && full.curve[i] === cut.curve[i];
    if (!same) failures.push({ i, date: ymd(T), full: { ...a, equity: full.curve[i] }, cut: { ...b, equity: cut.curve[i] } });
  }
  return { cuts: cuts.length, failures };
}

// ---------- payload ----------
function isoSeconds(ms) { return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace('.000Z', 'Z'); }

// computed: [{key, as_of, state, script}] -> payload object (see SPEC §7). engine_sha256 = sha256 of the canonical JSON
// {key: script_sha256} over every strategy in the file, so it changes whenever any listed script changes.
function buildPayload(computed, { now = Date.now() } = {}) {
  if (!computed.length) throw new Error('no strategies');
  const asOf = computed[0].as_of;
  for (const c of computed) if (c.as_of !== asOf) throw new Error(`strategies end on different days (${computed.map(x => x.key + ' ' + x.as_of).join(', ')}); one as_of per file`);
  const strategies = {}, hashes = {};
  for (const c of computed) {
    const { spec, sha256: h } = c.script;
    hashes[c.key] = h;
    strategies[c.key] = {
      target_weight: c.state.target_weight,
      last_action: c.state.last_action,
      last_action_date: c.state.last_action_date,
      market: spec.market,
      script_sha256: h,
      status: c.script.api.SETTINGS.holdOnly ? 'holds' : spec.status,
    };
  }
  return { as_of: asOf, generated_at: isoSeconds(now), engine_sha256: sha256(canonicalJson(hashes)), strategies };
}

// ---------- Ed25519 ----------
function privateKeyFromPem(pem) {
  const k = crypto.createPrivateKey({ key: pem, format: 'pem' });
  if (k.asymmetricKeyType !== 'ed25519') throw new Error(`signing key is ${k.asymmetricKeyType}, expected ed25519`);
  return k;
}
function publicKeyB64(key) {   // raw 32-byte public key, base64 (what the backend pins as SIGNALS_PUBKEY_B64)
  const pub = key.type === 'private' ? crypto.createPublicKey(key) : key;
  const der = pub.export({ type: 'spki', format: 'der' });
  return der.subarray(der.length - 32).toString('base64');
}
function publicKeyFromB64(b64) {
  const raw = Buffer.from(b64, 'base64');
  if (raw.length !== 32) throw new Error('public key must be 32 bytes');
  return crypto.createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), raw]), format: 'der', type: 'spki' });
}
const signBytes = (bytes, privateKey) => crypto.sign(null, bytes, privateKey).toString('base64');
const verifyBytes = (bytes, sigB64, publicKey) => crypto.verify(null, bytes, publicKey, Buffer.from(sigB64, 'base64'));

module.exports = {
  DAY, VENDOR_DIR, TOP_KEYS, STRATEGY_KEYS, canonicalJson, sha256, ymd, lastCompletedBarT, loadManifest, loadScript,
  normalizeRows, posAfter, computeStrategy, defaultCuts, selfTestNoLookahead, buildPayload, isoSeconds,
  privateKeyFromPem, publicKeyB64, publicKeyFromB64, signBytes, verifyBytes,
};

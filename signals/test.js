#!/usr/bin/env node
'use strict';
// Zero-dependency tests for the signal adapter:  node signals/test.js
// Optional real-data check: SIGNALS_TERMINAL_DIR=<terminal checkout with data/> node signals/test.js
//   (runs emit.js --terminal on it and compares the state with the terminal's own crest_<key>.js run through its
//   crest/gen_multi.js loader; also checks the vendored file is byte-identical to the terminal's).
const assert = require('assert'), fs = require('fs'), os = require('os'), path = require('path'), crypto = require('crypto');
const { execFileSync, spawnSync } = require('child_process');
const L = require('./lib.js'), { synthetic } = require('./testdata.js');

const NOW = Date.parse('2026-09-30T00:30:00Z');
let passed = 0, skipped = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'signals-test-'));
const newKey = () => crypto.generateKeyPairSync('ed25519').privateKey;
const pemOf = k => k.export({ type: 'pkcs8', format: 'pem' });
const SILVER = L.loadScript('silver');

// ---------------------------------------------------------------- vendoring
test('vendored script matches MANIFEST sha256', () => {
  const m = L.loadManifest(), spec = m.scripts.silver;
  const buf = fs.readFileSync(path.join(L.VENDOR_DIR, spec.file));
  assert.strictEqual(L.sha256(buf), spec.sha256);
  assert.strictEqual(buf.length, spec.bytes);
  assert.strictEqual(spec.market, 'xyz:SILVER');
  assert.strictEqual(SILVER.api.VERSION, spec.version);
});

test('a tampered vendored copy is refused', () => {
  const d = tmp();
  fs.copyFileSync(path.join(L.VENDOR_DIR, 'MANIFEST.json'), path.join(d, 'MANIFEST.json'));
  const src = fs.readFileSync(path.join(L.VENDOR_DIR, 'crest_silver.js'), 'utf8');
  fs.writeFileSync(path.join(d, 'crest_silver.js'), src.replace('"trendLen": 200', '"trendLen": 201'));
  assert.throws(() => L.loadScript('silver', { dir: d }), /does not match MANIFEST/);
});

test('vendored copy is byte-identical to the terminal script (SIGNALS_TERMINAL_DIR)', () => {
  const dir = process.env.SIGNALS_TERMINAL_DIR;
  if (!dir) return 'skip';
  assert.ok(fs.readFileSync(path.join(dir, 'crest_silver.js')).equals(fs.readFileSync(path.join(L.VENDOR_DIR, 'crest_silver.js'))));
});

// ---------------------------------------------------------------- canonical JSON
test('canonical JSON: sorted keys, no whitespace, integers only, printable ASCII', () => {
  assert.strictEqual(L.canonicalJson({ b: 1, a: { d: null, c: 'x"y\\' }, e: [2, true] }), '{"a":{"c":"x\\"y\\\\","d":null},"b":1,"e":[2,true]}');
  assert.strictEqual(L.canonicalJson(-0), '0');
  assert.throws(() => L.canonicalJson({ a: 1.5 }), /safe integers/);
  assert.throws(() => L.canonicalJson({ a: NaN }), /safe integers/);
  assert.throws(() => L.canonicalJson({ a: 'é' }), /ASCII/);
  assert.throws(() => L.canonicalJson({ a: undefined }), /undefined/);
});

// ---------------------------------------------------------------- state
test('posAfter mirrors the terminal alerts (BUY/SELL/TRIM leverage)', () => {
  assert.deepStrictEqual(L.posAfter([]), { position: 'CASH', target_weight: 0, last_action: 'NONE', last_action_date: null });
  const buy2 = { side: 'BUY', lev: 2, date: '2020-01-02', why: 'breakout' };
  assert.strictEqual(L.posAfter([buy2]).target_weight, 2);
  assert.strictEqual(L.posAfter([buy2, { side: 'TRIM', why: 'leverage off', date: '2020-02-03' }]).target_weight, 1);
  assert.strictEqual(L.posAfter([buy2, { side: 'TRIM', why: 'leverage stop', date: '2020-02-03' }]).target_weight, 1);
  assert.strictEqual(L.posAfter([buy2, { side: 'TRIM', why: 'take profit 3x', frac: 0.25, date: '2020-02-03' }]).target_weight, 2);
  const s = L.posAfter([buy2, { side: 'SELL', why: 'trend exit', date: '2020-03-02' }]);
  assert.deepStrictEqual(s, { position: 'CASH', target_weight: 0, last_action: 'SELL', last_action_date: '2020-03-02' });
  assert.throws(() => L.posAfter([{ side: 'SHORT' }]), /unknown trade side/);
});

// ---------------------------------------------------------------- compute + validation
test('synthetic seed 4: completed-bar cut, LONG weight 1, deterministic', () => {
  const d = synthetic({ seed: 4 }).silver;
  // add a bar for "today" (not complete at NOW): it must be ignored
  const last = d.rows[d.rows.length - 1], today = Math.floor(NOW / L.DAY) * L.DAY;
  const rows = [...d.rows, [today, last[4], last[4] * 1.5, last[4], last[4] * 1.4, 1]];
  const c = L.computeStrategy(SILVER, rows, d.ctx, { now: NOW });
  assert.strictEqual(c.as_of, '2026-09-29');
  assert.strictEqual(c.rows[c.rows.length - 1].t, today - L.DAY);
  assert.strictEqual(c.state.position, 'LONG');
  assert.strictEqual(c.state.target_weight, 1);
  assert.strictEqual(c.state.last_action, 'BUY');
  assert.ok(c.result.trades.length >= 10);
});

test('synthetic seed 8: CASH weight 0 with a SELL as last action', () => {
  const d = synthetic({ seed: 8 }).silver, c = L.computeStrategy(SILVER, d.rows, d.ctx, { now: NOW });
  assert.strictEqual(c.state.target_weight, 0);
  assert.strictEqual(c.state.last_action, 'SELL');
});

test('inputs the live setting cannot use are refused', () => {
  const d = synthetic({ seed: 4 }).silver;
  assert.throws(() => L.computeStrategy(SILVER, d.rows.slice(300), d.ctx, { now: NOW }), /needs it from 1968-01-02/);
  assert.throws(() => L.computeStrategy(SILVER, d.rows, {}, { now: NOW }), /M2 is missing/);
  assert.throws(() => L.computeStrategy(SILVER, d.rows, { M2: d.ctx.M2.slice(0, -60) }, { now: NOW }), /M2 ends/);
  assert.throws(() => L.computeStrategy(SILVER, d.rows, d.ctx, { now: NOW + 9 * L.DAY }), /stale data/);
  const bad = d.rows.map(r => r.slice()); bad[500][0] += 3600e3;
  assert.throws(() => L.computeStrategy(SILVER, bad, d.ctx, { now: NOW }), /not a UTC midnight/);
  const unsorted = d.rows.slice(); [unsorted[10], unsorted[11]] = [unsorted[11], unsorted[10]];
  assert.throws(() => L.computeStrategy(SILVER, unsorted, d.ctx, { now: NOW }), /strictly ascending/);
  const zero = d.rows.map(r => r.slice()); zero[700][4] = 0;
  assert.throws(() => L.computeStrategy(SILVER, zero, d.ctx, { now: NOW }), /close must be > 0/);
});

// ---------------------------------------------------------------- no look-ahead
test('no look-ahead: truncating at bar i gives the state at i (live setting, two seeds)', () => {
  for (const seed of [4, 8]) {
    const d = synthetic({ seed }).silver, c = L.computeStrategy(SILVER, d.rows, d.ctx, { now: NOW });
    const r = L.selfTestNoLookahead(SILVER, c.rows, c.ctx);
    assert.ok(r.cuts > 60, `seed ${seed}: ${r.cuts} cuts`);
    assert.deepStrictEqual(r.failures, [], `seed ${seed}`);
  }
});

test('no look-ahead with 2x leverage, leverage stop and partial take-profit (same engine)', () => {
  const d = synthetic({ seed: 6 }).silver, c = L.computeStrategy(SILVER, d.rows, d.ctx, { now: NOW });
  const params = { levWhen: 'first', levStop: 0.2, levExit: 20, tp1: 2, tp1Frac: 0.25 };
  const full = SILVER.api.run(c.rows, { wfId: 'SILVER', ...params }, c.ctx);
  assert.ok(full.trades.some(t => t.side === 'BUY' && t.lev === 2), 'a 2x entry happens');
  assert.ok(full.trades.some(t => t.side === 'TRIM'), 'a TRIM happens');
  const r = L.selfTestNoLookahead(SILVER, c.rows, c.ctx, { params });
  assert.deepStrictEqual(r.failures, []);
  // every weight the state machine produces along the way is in {0, 1, 2}
  for (let i = 0; i < full.trades.length; i++) assert.ok([0, 1, 2].includes(L.posAfter(full.trades.slice(0, i + 1)).target_weight));
});

test('the self-test catches a script that looks ahead', () => {
  const d = synthetic({ seed: 4 }).silver, rows = L.normalizeRows(d.rows, 'rows');
  const cheat = { key: 'cheat', spec: { ticker: 'X' }, api: { run(input) {
    const C = input.map(r => r.c), trades = [], curve = []; let inPos = false;
    for (let i = 0; i < C.length; i++) {
      const up = i + 1 < C.length && C[i + 1] > C[i];   // peeks at tomorrow
      if (up && !inPos) { trades.push({ i, side: 'BUY', lev: 1, date: L.ymd(input[i].t) }); inPos = true; }
      else if (!up && inPos) { trades.push({ i, side: 'SELL', date: L.ymd(input[i].t) }); inPos = false; }
      curve.push(1);
    }
    return { trades, curve };
  } } };
  const r = L.selfTestNoLookahead(cheat, rows, {}, { cuts: [100, 200, 300, 400, 500] });
  assert.ok(r.failures.length > 0);
});

// ---------------------------------------------------------------- payload + signature
test('payload shape, engine hash, canonical bytes, Ed25519 sign/verify, tamper and wrong key', () => {
  const d = synthetic({ seed: 4 }).silver, c = L.computeStrategy(SILVER, d.rows, d.ctx, { now: NOW });
  const p = L.buildPayload([{ ...c, script: SILVER }], { now: NOW });
  assert.deepStrictEqual(Object.keys(p).sort(), L.TOP_KEYS);
  assert.deepStrictEqual(Object.keys(p.strategies.silver).sort(), L.STRATEGY_KEYS);
  assert.strictEqual(p.generated_at, '2026-09-30T00:30:00Z');
  assert.strictEqual(p.strategies.silver.market, 'xyz:SILVER');
  assert.strictEqual(p.strategies.silver.status, 'trades');
  assert.strictEqual(p.strategies.silver.script_sha256, SILVER.sha256);
  assert.strictEqual(p.engine_sha256, L.sha256(`{"silver":"${SILVER.sha256}"}`));
  const body = Buffer.from(L.canonicalJson(p)), k = newKey(), sig = L.signBytes(body, k);
  assert.ok(!/\s/.test(body.toString()));
  assert.strictEqual(Buffer.from(sig, 'base64').length, 64);
  const pub = L.publicKeyFromB64(L.publicKeyB64(k));
  assert.ok(L.verifyBytes(body, sig, pub));
  assert.ok(!L.verifyBytes(Buffer.from(body.toString().replace('"target_weight":1', '"target_weight":2')), sig, pub));
  assert.ok(!L.verifyBytes(body, sig, L.publicKeyFromB64(L.publicKeyB64(newKey()))));
});

test('buildPayload refuses strategies ending on different days', () => {
  const x = { key: 'a', as_of: '2026-09-29', state: L.posAfter([]), script: SILVER }, y = { ...x, key: 'b', as_of: '2026-09-28' };
  assert.throws(() => L.buildPayload([x, y]), /different days/);
});

// ---------------------------------------------------------------- CLI
test('emit.js --input: writes canonical signals.json + valid signals.sig; fails closed without a key', () => {
  const d = tmp(), input = path.join(d, 'in.json'), out = path.join(d, 'out'), k = newKey();
  fs.writeFileSync(input, JSON.stringify(synthetic({ seed: 4 })));
  const emit = path.join(__dirname, 'emit.js'), argv = [emit, '--input', input, '--out-dir', out, '--now', '2026-09-30T00:30:00Z', '--self-test-cuts', '5'];
  execFileSync(process.execPath, argv, { env: { ...process.env, SIGNALS_ED25519_PRIVATE_KEY_PEM: pemOf(k), SIGNALS_ED25519_PUBLIC_KEY_B64: L.publicKeyB64(k) }, stdio: 'pipe' });
  const body = fs.readFileSync(path.join(out, 'signals.json')), sig = fs.readFileSync(path.join(out, 'signals.sig'), 'utf8');
  assert.strictEqual(body.toString(), L.canonicalJson(JSON.parse(body)));
  assert.ok(L.verifyBytes(body, sig, L.publicKeyFromB64(L.publicKeyB64(k))));
  const p = JSON.parse(body);
  assert.strictEqual(p.as_of, '2026-09-29');
  assert.strictEqual(p.strategies.silver.target_weight, 1);
  // no key -> exit 1, nothing written
  const out2 = path.join(d, 'out2'), env = { ...process.env }; delete env.SIGNALS_ED25519_PRIVATE_KEY_PEM;
  const r = spawnSync(process.execPath, [emit, '--input', input, '--out-dir', out2, '--now', '2026-09-30T00:30:00Z'], { env, encoding: 'utf8' });
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /SIGNALS_ED25519_PRIVATE_KEY_PEM is not set/);
  assert.ok(!fs.existsSync(path.join(out2, 'signals.json')));
  // a private key that does not match the expected public key -> exit 1
  const r2 = spawnSync(process.execPath, [emit, '--input', input, '--out-dir', out2, '--now', '2026-09-30T00:30:00Z'], { env: { ...env, SIGNALS_ED25519_PRIVATE_KEY_PEM: pemOf(newKey()), SIGNALS_ED25519_PUBLIC_KEY_B64: L.publicKeyB64(k) }, encoding: 'utf8' });
  assert.strictEqual(r2.status, 1); assert.match(r2.stderr, /does not match/);
  // stale input -> exit 1
  const r3 = spawnSync(process.execPath, [emit, '--input', input, '--out-dir', out2, '--now', '2026-10-20T00:30:00Z', '--no-sign'], { env, encoding: 'utf8' });
  assert.strictEqual(r3.status, 1); assert.match(r3.stderr, /stale data/);
});

test('keygen.js prints a usable PEM + base64 public key', () => {
  const s = execFileSync(process.execPath, [path.join(__dirname, 'keygen.js')], { encoding: 'utf8' });
  const pem = s.match(/-----BEGIN PRIVATE KEY-----[\s\S]+?-----END PRIVATE KEY-----\n/)[0];
  const pub = s.trim().split('\n').pop();
  assert.strictEqual(L.publicKeyB64(L.privateKeyFromPem(pem)), pub);
});

// ---------------------------------------------------------------- real data (optional)
test('real terminal data (SIGNALS_TERMINAL_DIR): emit --terminal equals the terminal\'s own run', () => {
  const dir = process.env.SIGNALS_TERMINAL_DIR;
  if (!dir || !fs.existsSync(path.join(dir, 'data', 'tradfi', 'SILVER.json'))) return 'skip';
  // the clock of the daily build that would run just after the newest bar in the data (00:30 UTC the next day)
  const rowsFile = JSON.parse(fs.readFileSync(path.join(dir, 'data', 'tradfi', 'SILVER.json'))).rows, now = rowsFile[rowsFile.length - 1][0] + L.DAY + 30 * 60e3;
  const out = tmp(), k = newKey();
  const log = execFileSync(process.execPath, [path.join(__dirname, 'emit.js'), '--terminal', dir, '--out-dir', out, '--now', new Date(now).toISOString()], { env: { ...process.env, SIGNALS_ED25519_PRIVATE_KEY_PEM: pemOf(k) }, encoding: 'utf8' });
  process.stdout.write(log.split('\n').map(l => l && '    | ' + l).filter(Boolean).join('\n') + '\n');
  const p = JSON.parse(fs.readFileSync(path.join(out, 'signals.json')));
  // reference: the terminal's own script and loader, exactly as crest/gen_multi.js runScript (run 1)
  const G = require(path.join(dir, 'crest', 'gen_multi.js')), S = require(path.join(dir, 'crest_silver.js'));
  const TO = L.lastCompletedBarT(now);
  const w = G.load('data/tradfi/SILVER.json').rows.filter(r => r.t >= G.fromFor('silver') && r.t <= TO);
  const ref = S.run(w, { wfId: 'SILVER' }, G.ctx()), st = L.posAfter(ref.trades);
  assert.strictEqual(p.as_of, ref.last);
  assert.strictEqual(p.strategies.silver.target_weight, st.target_weight);
  assert.strictEqual(p.strategies.silver.last_action, st.last_action);
  assert.strictEqual(p.strategies.silver.last_action_date, st.last_action_date);
});

// ---------------------------------------------------------------- run
(async () => {
  for (const [name, fn] of tests) {
    const t0 = Date.now();
    try {
      const r = await fn();
      if (r === 'skip') { skipped++; console.log(`SKIP ${name}`); }
      else { passed++; console.log(`ok   ${name} (${Date.now() - t0} ms)`); }
    } catch (e) {
      console.log(`FAIL ${name}\n     ${e && e.stack || e}`);
      process.exitCode = 1;
    }
  }
  console.log(`\n${passed} passed, ${skipped} skipped, ${tests.length - passed - skipped} failed`);
})();

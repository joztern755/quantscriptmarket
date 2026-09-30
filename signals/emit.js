#!/usr/bin/env node
'use strict';
// Emits the signed in-house signal feed (SPEC §7): signals.json (canonical JSON, the exact bytes that are signed) and
// signals.sig (base64 Ed25519 signature over those bytes).
//
//   In the terminal's daily build (integrations/terminal/), after the data download and check:
//     SIGNALS_ED25519_PRIVATE_KEY_PEM=... node market_signals/emit.js --terminal . --out-dir public
//   From prepared rows (tests, dry runs):
//     node signals/emit.js --input rows.json --out-dir out [--now 2026-09-30T00:30:00Z] [--no-sign]
//       rows.json = {"silver": {"rows": [[t,o,h,l,c,v], ...], "ctx": {"M2": [[t,o,h,l,c], ...]}}}
//
// Options
//   --keys a,b              strategies to emit (default: every script in vendor/MANIFEST.json)
//   --terminal DIR          load rows exactly as the terminal does (DIR/crest/gen_multi.js load/ctx/fromFor: the same
//                           cleaning as the page and alerts); also compares DIR/crest_<key>.js with the vendored hash
//   --strict-terminal-match fail (instead of warn) when the terminal's crest_<key>.js differs from the vendored copy
//   --input FILE            rows from a JSON file instead
//   --now ISO               clock override (tests); default: the real time
//   --out-dir DIR           where signals.json and signals.sig are written (atomically; nothing is written on failure)
//   --no-sign               write signals.json only (local dry run); never use in the daily build
//   --max-lag-days N        the last bar may be at most N days before the last completed UTC day (default 3: weekend +
//                           one holiday; the marketplace rejects as_of more than 4 days before today). Dry runs on old data.
//   --self-test-cuts N      evenly spaced extra cut points for the no-look-ahead self-test (default 40; 0 = trades and
//                           last 20 bars only). The self-test always runs; a failure stops the emit.
// Env
//   SIGNALS_ED25519_PRIVATE_KEY_PEM   PKCS#8 PEM Ed25519 private key (GitHub secret; never commit)
//   SIGNALS_ED25519_PUBLIC_KEY_B64    optional: expected public key; the emit fails if the private key does not match it
//
// Exit code 0 = files written; anything else = nothing written (the caller keeps the previously published files).
const fs = require('fs'), path = require('path');
const L = require('./lib.js');

function args(argv) {
  const o = { keys: null, terminal: null, input: null, now: null, outDir: null, sign: true, strict: false, spread: 40, maxLagDays: 3 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i], next = () => { if (i + 1 >= argv.length) throw new Error(`${a} needs a value`); return argv[++i]; };
    if (a === '--keys') o.keys = next().split(',').map(s => s.trim()).filter(Boolean);
    else if (a === '--terminal') o.terminal = path.resolve(next());
    else if (a === '--input') o.input = path.resolve(next());
    else if (a === '--now') { const s = next(), t = Date.parse(s); if (!Number.isFinite(t)) throw new Error(`--now: bad time ${s}`); o.now = t; }
    else if (a === '--out-dir') o.outDir = path.resolve(next());
    else if (a === '--no-sign') o.sign = false;
    else if (a === '--strict-terminal-match') o.strict = true;
    else if (a === '--max-lag-days') o.maxLagDays = Math.max(0, parseInt(next(), 10) || 0);
    else if (a === '--self-test-cuts') o.spread = Math.max(0, parseInt(next(), 10) || 0);
    else throw new Error(`unknown option ${a}`);
  }
  if (!o.outDir) throw new Error('--out-dir is required');
  if (!!o.terminal === !!o.input) throw new Error('give exactly one of --terminal DIR or --input FILE');
  return o;
}

// rows and ctx for one strategy from the terminal checkout, loaded by the terminal's own loader
function fromTerminal(dir, script, strict) {
  const G = require(path.join(dir, 'crest', 'gen_multi.js'));
  for (const f of ['load', 'ctx', 'fromFor']) if (typeof G[f] !== 'function') throw new Error(`terminal crest/gen_multi.js no longer exports ${f}()`);
  const { key, spec } = script, own = path.join(dir, spec.file);
  const h = fs.existsSync(own) ? L.sha256(fs.readFileSync(own)) : null;
  if (h !== spec.sha256) {
    const msg = `terminal ${spec.file} (${h || 'missing'}) differs from the vendored copy ${spec.sha256}: the marketplace keeps emitting the pinned version; re-vendor to publish the new one as a new strategy version`;
    if (strict) throw new Error(msg);
    console.log(`::warning::${msg}`);
  }
  const from = G.fromFor(key), rows = G.load(spec.terminal_rows).rows.filter(r => r.t >= from), all = G.ctx(), ctx = {};
  for (const ref of spec.ctx_refs || []) ctx[ref] = all[ref];
  return { rows, ctx };
}

function writeAtomic(file, data) { const tmp = file + '.tmp-' + process.pid; fs.writeFileSync(tmp, data); fs.renameSync(tmp, file); }

function main() {
  const o = args(process.argv.slice(2)), now = o.now == null ? Date.now() : o.now;
  let privateKey = null;
  if (o.sign) {
    const pem = process.env.SIGNALS_ED25519_PRIVATE_KEY_PEM;
    if (!pem || !pem.trim()) throw new Error('SIGNALS_ED25519_PRIVATE_KEY_PEM is not set (use --no-sign only for local dry runs)');
    privateKey = L.privateKeyFromPem(pem);
    const want = (process.env.SIGNALS_ED25519_PUBLIC_KEY_B64 || '').trim();
    if (want && want !== L.publicKeyB64(privateKey)) throw new Error('the private key does not match SIGNALS_ED25519_PUBLIC_KEY_B64');
  }
  const manifest = L.loadManifest(), keys = o.keys || Object.keys(manifest.scripts);
  const input = o.input ? JSON.parse(fs.readFileSync(o.input, 'utf8')) : null;
  const computed = [];
  for (const key of keys) {
    const script = L.loadScript(key, { manifest });
    const src = o.terminal ? fromTerminal(o.terminal, script, o.strict) : (input && input[key]) || (() => { throw new Error(`--input has no "${key}"`); })();
    const c = L.computeStrategy(script, src.rows, src.ctx || {}, { now, maxLagDays: o.maxLagDays });
    const t = L.selfTestNoLookahead(script, c.rows, c.ctx, { cuts: L.defaultCuts(c.rows.length, c.result.trades, { spread: o.spread }) });
    if (t.failures.length) throw new Error(`${key}: no-look-ahead self-test failed at ${t.failures.length} of ${t.cuts} cuts, first ${JSON.stringify(t.failures[0])}`);
    const lt = c.result.trades[c.result.trades.length - 1];
    console.log(`${key}: ${c.rows.length} bars ${c.result.first}..${c.as_of}, ${c.state.position} weight ${c.state.target_weight}, last ${c.state.last_action} ${c.state.last_action_date || '-'}${lt ? ` (${lt.why})` : ''}; self-test ${t.cuts} cuts OK`);
    computed.push({ ...c, script });
  }
  const payload = L.buildPayload(computed, { now }), body = Buffer.from(L.canonicalJson(payload), 'utf8');
  let sig = null;
  if (privateKey) {
    sig = L.signBytes(body, privateKey);
    if (!L.verifyBytes(body, sig, L.publicKeyFromB64(L.publicKeyB64(privateKey)))) throw new Error('signature does not verify');
  }
  fs.mkdirSync(o.outDir, { recursive: true });
  writeAtomic(path.join(o.outDir, 'signals.json'), body);
  if (sig) writeAtomic(path.join(o.outDir, 'signals.sig'), sig);
  console.log(`signals.json as_of ${payload.as_of} engine ${payload.engine_sha256.slice(0, 12)}… ${sig ? 'signed by public key ' + L.publicKeyB64(privateKey) : 'UNSIGNED (--no-sign)'} -> ${o.outDir}`);
}

if (require.main === module) {
  try { main(); } catch (e) { console.error(`emit.js: ${e.message}`); process.exit(1); }
}
module.exports = { args };

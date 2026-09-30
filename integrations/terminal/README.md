# Terminal → marketplace signal feed

How aijalon.trade gets the daily CREST SILVER signal from the terminal (joztern755/terminal.aijalon) (SPEC §7, §10).
**Nothing here has been applied to the terminal repo.** The owner applies it with `install.sh`, checks it, and commits
it there.

```
terminal daily build (00:30 UTC)                                         aijalon.trade (Cloud Run)
  fetch data -> check_data -> audit -> build page
  -> node market_signals/emit.js --terminal . --out-dir public   ---->   /internal/ingest-signals
       public/signals.json  (canonical JSON)                              fetch_signals(): HTTPS, 1 MB cap, timeout
       public/signals.sig   (Ed25519 over those bytes, base64)            verify against the pinned public key
  -> Firebase Hosting deploy (aijalon-terminal.web.app)                   strict schema + registry + staleness
                                                                          -> signals table (bar_close, coin, weight_bps)
```

## Files

| Here | Installed in the terminal as | What it is |
|---|---|---|
| `signals/emit.js`, `signals/lib.js` | `market_signals/emit.js`, `market_signals/lib.js` | the emitter (zero dependencies) |
| `signals/keygen.js` | `market_signals/keygen.js` | prints a new Ed25519 keypair (writes nothing) |
| `signals/vendor/crest_silver.js` + `MANIFEST.json` | `market_signals/vendor/` | the pinned script, byte-identical to the terminal's `crest_silver.js` at commit `2ef153e` (sha256 `e60119a7…42ed`) |
| `integrations/terminal/build-deploy.patch` | `.github/workflows/build-deploy.yml`, `firebase.json` | one new step before the deploy; no-cache headers for the two files |
| `integrations/terminal/install.sh` | — | copies the files, checks the hashes, applies the patch |

## Install (owner)

1. In a clean checkout of the terminal: `path/to/quantscriptmarket/integrations/terminal/install.sh .`
2. `node market_signals/keygen.js`, then in the terminal repo's GitHub settings (Settings → Secrets and variables →
   Actions):
   - secret `SIGNALS_ED25519_PRIVATE_KEY_PEM` = the PEM block (never commit it, never paste it anywhere else);
   - variable `SIGNALS_ED25519_PUBLIC_KEY_B64` = the public key. emit.js refuses to sign if the secret does not match it.
3. Give the same public key to the marketplace as `SIGNALS_PUBKEY_B64` (Secret Manager). `SIGNALS_URL` defaults to
   `https://aijalon-terminal.web.app/signals.json`; the signature is read from the same path with `.sig`.
4. Dry run on local data (`data/` from the tradfi-data branch plus `data/hl/sample/xyz_SILVER.json`):
   `node market_signals/emit.js --terminal . --out-dir /tmp/sig --no-sign`
5. The terminal's `CLAUDE.md` rule: add the new step and `market_signals/` to `.claude/skills/terminal/SKILL.md`
   Part C, run `npm run skill`, commit, push. The next build publishes `/signals.json` and `/signals.sig`.

## What the new workflow step does

It runs after the page build and the health step, right before `FirebaseExtended/action-hosting-deploy`:

- loads SILVER's rows and the `M2` series with the terminal's own loader (`crest/gen_multi.js` `load`/`ctx`/`fromFor`:
  the same cleaning the page and the alerts use), keeping only bars up to the last completed UTC day;
- runs the **vendored** `crest_silver.js` (hash-checked, evaluated from the checked bytes) as `run(rows, {wfId:'SILVER'}, ctx)`,
  twice (it must be deterministic);
- reads the state after the last trade the same way `alerts/notify.js` does (BUY opens, `lev` 2 on a 2x entry;
  SELL closes; TRIM "leverage off/stop" drops to 1x) → `target_weight` 0 / 1 / 2;
- runs the no-look-ahead self-test (every trade bar ±1, the last 20 bars and 40 spread cut points: running on the rows
  up to bar *i* must give the same position, weight, last action and equity at *i* as the full run) and fails on any
  difference;
- writes `public/signals.json` (canonical JSON: sorted keys, no whitespace, integers only) and `public/signals.sig`
  (base64 Ed25519 signature over exactly those bytes), atomically.

It refuses (writes nothing) when: the key is missing or does not match the variable; the vendored hash is wrong; the
history does not start on 1968-01-02 (the live setting buys at the first bar, so a shorter history changes the state);
`M2` is missing or ends more than 7 days before the last bar (the script would silently drop its filter); the last bar
is more than 3 days before the last completed day; the run is non-deterministic, liquidated or its trade list disagrees
with its position.

**On failure** the step re-publishes the pair that is live now (downloaded from the site), so a Firebase deploy never
deletes the feed; that old pair still verifies and aijalon.trade flags it stale after 36 hours. The page build is never
blocked by the marketplace. (To make a failed emit stop the whole deploy instead, replace the `if … else … fi` with the
bare `node market_signals/emit.js --terminal . --out-dir public`.)

**When the terminal re-fits SILVER** (a new `crest_silver.js`), emit.js prints a `::warning::` and keeps emitting the
pinned version, so the listed strategy version never changes silently. Publishing the new script is a new strategy
version on the marketplace (resets the live record): copy the new file into `signals/vendor/`, update `MANIFEST.json`
(sha256, bytes, commit, `evidence`), run `node signals/test.js`, re-run `install.sh`, and register the new
`script_sha256` as the version's `code_hash`. `--strict-terminal-match` turns the warning into a failure.

## Feed format

```json
{"as_of":"2026-09-25","engine_sha256":"49bc07…9b61","generated_at":"2026-09-30T07:17:14Z",
 "strategies":{"silver":{"last_action":"SELL","last_action_date":"1980-01-15","market":"xyz:SILVER",
               "script_sha256":"e60119…42ed","status":"trades","target_weight":0}}}
```

- `as_of`: the UTC day of the last completed bar used. `bar_close` on the marketplace = `as_of` + 1 day 00:00 UTC.
- `engine_sha256`: sha256 of the canonical JSON `{key: script_sha256}` over the strategies in the file.
- `status`: `trades`, or `holds` if the vendored setting is buy & hold (`holdOnly`); the marketplace refuses a listed
  strategy that holds.
- SPEC §7 calls the top-level hash `engine_hash`; the feed uses `engine_sha256` (plus `script_sha256` per strategy).

Check a published pair by hand (Node 18+):

```sh
curl -fsS https://aijalon-terminal.web.app/signals.json -o s.json && curl -fsS https://aijalon-terminal.web.app/signals.sig -o s.sig
node -e 'const c=require("crypto"),fs=require("fs");const k=c.createPublicKey({key:Buffer.concat([Buffer.from("302a300506032b6570032100","hex"),Buffer.from(process.argv[1],"base64")]),format:"der",type:"spki"});console.log(c.verify(null,fs.readFileSync("s.json"),k,Buffer.from(fs.readFileSync("s.sig","utf8"),"base64")))' "$SIGNALS_PUBKEY_B64"
```

## Security notes

- The private key is only in the GitHub secret and only the emit step's environment sees it. `build-deploy.yml` runs on
  push to `main`, schedule and manual dispatch — not on pull requests — so forks never get it. Anyone who can push to
  the terminal's `main` can sign; protect that branch.
- The marketplace verifies before it parses, pins the key (no key discovery), refuses redirects, caps sizes, rejects
  non-canonical bytes, unknown keys, unknown strategies or markets, weights outside {0,1,2}, a listed strategy that is
  missing or holds, a stale or future feed, an older `as_of` than already accepted (replay) and the same bar with a
  different weight (rebuilt history). Critical rejections raise one alert per listed market, which pauses new entries
  there (exits keep running).
- Rotation: generate a new pair, deploy the marketplace with the new public key and update the secret and variable in the
  same window (the marketplace rejects the old signature from then on; the feed is re-signed at the next build — run
  the workflow by hand to shorten the gap).

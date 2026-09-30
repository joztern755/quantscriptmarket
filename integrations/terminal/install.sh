#!/usr/bin/env bash
# Installs the marketplace signal emitter into a terminal.aijalon checkout (run by the owner, then commit there).
#   integrations/terminal/install.sh /path/to/terminal.aijalon
# Copies signals/{emit.js,lib.js,keygen.js} and signals/vendor/* to <terminal>/market_signals/, verifies the vendored
# hashes, then applies build-deploy.patch (workflow step + Firebase headers). Re-run it after re-vendoring a script.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
T="${1:?usage: install.sh /path/to/terminal.aijalon}"
T="$(cd "$T" && pwd)"
[ -f "$T/crest/gen_multi.js" ] && [ -f "$T/.github/workflows/build-deploy.yml" ] || { echo "not a terminal.aijalon checkout: $T" >&2; exit 1; }

mkdir -p "$T/market_signals/vendor"
cp "$REPO/signals/emit.js" "$REPO/signals/lib.js" "$REPO/signals/keygen.js" "$T/market_signals/"
cp "$REPO/signals/vendor/MANIFEST.json" "$T/market_signals/vendor/"
node -e '
  const fs = require("fs"), path = require("path"), crypto = require("crypto");
  const [src, dst, term] = process.argv.slice(1), m = JSON.parse(fs.readFileSync(path.join(src, "MANIFEST.json")));
  for (const [key, s] of Object.entries(m.scripts)) {
    const buf = fs.readFileSync(path.join(src, s.file)), h = crypto.createHash("sha256").update(buf).digest("hex");
    if (h !== s.sha256) { console.error(`${s.file}: sha256 ${h} != MANIFEST ${s.sha256}`); process.exit(1); }
    fs.writeFileSync(path.join(dst, s.file), buf);
    const own = path.join(term, s.file), th = fs.existsSync(own) ? crypto.createHash("sha256").update(fs.readFileSync(own)).digest("hex") : null;
    console.log(`${key}: vendored ${s.file} ${h.slice(0, 12)}…` + (th === h ? " (same as the terminal copy)" : ` — WARNING: the terminal ${s.file} is ${th ? th.slice(0, 12) + "…" : "missing"}; the marketplace keeps the pinned version`));
  }' "$REPO/signals/vendor" "$T/market_signals/vendor" "$T"

if git -C "$T" apply --check "$HERE/build-deploy.patch" 2>/dev/null; then
  git -C "$T" apply "$HERE/build-deploy.patch"
  echo "applied build-deploy.patch"
elif git -C "$T" apply --reverse --check "$HERE/build-deploy.patch" 2>/dev/null; then
  echo "build-deploy.patch already applied"
else
  echo "build-deploy.patch does not apply cleanly; add the step by hand (see integrations/terminal/README.md)" >&2
  exit 1
fi
cat <<EOF

Next, in $T:
  1. node market_signals/keygen.js      -> set secret SIGNALS_ED25519_PRIVATE_KEY_PEM (Settings > Secrets and variables > Actions)
                                         and variable SIGNALS_ED25519_PUBLIC_KEY_B64; give the public key to the marketplace
                                         (SIGNALS_PUBKEY_B64). Never commit the private key.
  2. dry run with local data:  node market_signals/emit.js --terminal . --out-dir /tmp/sig --no-sign
  3. update .claude/skills/terminal/SKILL.md Part C (new workflow step, market_signals/), npm run skill, commit, push.
EOF

#!/usr/bin/env bash
# DB integration tests for backend/migrations + app.ledger against a REAL PostgreSQL 16.
#
#   PGHOST=localhost PGPORT=55432 PGUSER=postgres backend/tests/db/run_db_tests.sh
#
# Needs: a running cluster where $PGUSER is a superuser (tamper tests disable triggers; role tests create
# LOGIN roles), psql/createdb/dropdb on PATH, python3 (3.11+). Creates a throwaway database
# aijalon_test_<pid> (+ clones for tamper scenarios) and drops everything on exit (KEEP_DB=1 keeps them).
# Local throwaway cluster:
#   runuser -u postgres -- /usr/lib/postgresql/16/bin/initdb -D /tmp/claude-pg/data -U postgres --auth=trust
#   runuser -u postgres -- /usr/lib/postgresql/16/bin/pg_ctl -D /tmp/claude-pg/data \
#       -o "-p 55432 -k /tmp/claude-pg -c listen_addresses=localhost" -l /tmp/claude-pg/server.log start
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$(cd "$HERE/../.." && pwd)"
export PGHOST="${PGHOST:-localhost}" PGPORT="${PGPORT:-55432}" PGUSER="${PGUSER:-postgres}"
PY="${PYTHON:-$(command -v python3.12 || command -v python3)}"
DB="aijalon_test_$$"
URL="postgresql://${PGUSER}@${PGHOST}:${PGPORT}/${DB}"
API_USER="aj_test_api_$$"
EXEC_USER="aj_test_exec_$$"
CLONES=()
PASS=0
FAIL=0
FAILED=()

cleanup() {
    if [[ "${KEEP_DB:-0}" != "1" ]]; then
        for c in "${CLONES[@]}"; do dropdb --if-exists "$c" >/dev/null 2>&1; done
        dropdb --if-exists "$DB" >/dev/null 2>&1
        psql -X -q -d postgres -c "DROP ROLE IF EXISTS ${API_USER}; DROP ROLE IF EXISTS ${EXEC_USER};" >/dev/null 2>&1
    fi
}
trap cleanup EXIT

ok()   { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); FAILED+=("$1"); printf '  FAIL  %s\n%s\n' "$1" "${2:-}" | sed 's/^/        /;1s/^        //'; }

# run SQL from stdin as ONE transaction-per-statement session (autocommit), optionally as another user
_psql() { # $1=db $2=user ; stdin = sql
    psql -X -q -A -t -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -d "$1" -U "$2" -f - 2>&1
}

# expect_ok NAME [DB] [USER] <<SQL
expect_ok() {
    local name=$1 db=${2:-$DB} user=${3:-$PGUSER} out
    if out=$(_psql "$db" "$user"); then ok "$name"; else bad "$name" "$out"; fi
}

# expect_err NAME SQLSTATE [DB] [USER] <<SQL   (the script must FAIL with that SQLSTATE)
expect_err() {
    local name=$1 state=$2 db=${3:-$DB} user=${4:-$PGUSER} out
    if out=$(_psql "$db" "$user"); then
        bad "$name" "expected SQLSTATE $state but it succeeded"
    elif grep -q "ERROR:  $state" <<<"$out"; then
        ok "$name"
    else
        bad "$name" "expected SQLSTATE $state, got: $out"
    fi
}

# expect_eq NAME EXPECTED [DB] [USER] <<SQL  (single-value query)
expect_eq() {
    local name=$1 want=$2 db=${3:-$DB} user=${4:-$PGUSER} out
    out=$(_psql "$db" "$user")
    if [[ "$out" == "$want" ]]; then ok "$name"; else bad "$name" "want: $want"$'\n'"got:  $out"; fi
}

clone_db() { # $1 = clone name
    CLONES+=("$1")
    createdb -T "$DB" "$1" || { echo "cannot clone $DB"; exit 1; }
}

U1=11111111-1111-4111-8111-111111111111   # subscriber
U2=22222222-2222-4222-8222-222222222222   # creator
A1=33333333-3333-4333-8333-333333333333   # admin 1
A2=44444444-4444-4444-8444-444444444444   # admin 2
FEE1="user:${U1}:fee_balance"
PAY2="creator:${U2}:payable"
ADDR=0x00000000000000000000000000000000000000aa

echo "== database $DB on $PGHOST:$PGPORT (python: $PY)"
createdb "$DB" || { echo "createdb failed (is the cluster running?)"; exit 1; }

# ------------------------------------------------------------------------------------------------ migrate.py
echo "-- migrations"
if out=$("$PY" "$BACKEND/scripts/migrate.py" --database-url "$URL" 2>&1) \
   && grep -q "applied 0001_init" <<<"$out" && grep -q "applied 0003_seed" <<<"$out"; then
    ok "migrate applies 0001..0003"
else
    bad "migrate applies 0001..0003" "$out"; echo "cannot continue"; exit 1
fi
if out=$("$PY" "$BACKEND/scripts/migrate.py" --database-url "$URL" 2>&1) && grep -q "up to date" <<<"$out"; then
    ok "migrate is a no-op when up to date"
else bad "migrate is a no-op when up to date" "$out"; fi

TMPM="$(mktemp -d)"
cp "$BACKEND"/migrations/*.sql "$TMPM/"
echo "-- tampered after apply" >> "$TMPM/0003_seed.sql"
out=$("$PY" "$BACKEND/scripts/migrate.py" --database-url "$URL" --dir "$TMPM" 2>&1); rc=$?
if [[ $rc -eq 2 ]] && grep -q "checksum changed" <<<"$out"; then ok "migrate refuses a changed checksum (exit 2)"
else bad "migrate refuses a changed checksum (exit 2)" "rc=$rc $out"; fi
cp "$BACKEND"/migrations/*.sql "$TMPM/"; rm "$TMPM/0002_roles.sql"
out=$("$PY" "$BACKEND/scripts/migrate.py" --database-url "$URL" --dir "$TMPM" 2>&1); rc=$?
if [[ $rc -eq 2 ]] && grep -q "missing on disk" <<<"$out"; then ok "migrate refuses when an applied file is missing"
else bad "migrate refuses when an applied file is missing" "rc=$rc $out"; fi
rm -rf "$TMPM"

expect_eq "objects owned by app_migrator" "0" <<SQL
SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner <> 'app_migrator';
SQL

# ------------------------------------------------------------------------------------------------ seed
echo "-- seed"
expect_eq "platform ledger accounts seeded" "8" <<SQL
SELECT count(*) FROM ledger_accounts WHERE owner_user_id IS NULL;
SQL
expect_eq "kill switches seeded off" "kill_switch_global=false,new_entries_paused=false" <<SQL
SELECT string_agg(key || '=' || value::text, ',' ORDER BY key) FROM system_flags;
SQL
expect_eq "silver listed free (0/0); others draft with no price" "btc:draft::,gold:draft::,hype:draft::,oil:draft::,runners:draft::,silver:listed:0:0:{xyz:SILVER},sol:draft::" <<SQL
SELECT string_agg(slug || ':' || status || ':' || coalesce(price_monthly_micro::text, '') || ':'
                  || coalesce(profit_share_bps::text, '') || CASE WHEN slug = 'silver' THEN ':' || markets::text ELSE '' END,
                  ',' ORDER BY slug)
  FROM strategies WHERE in_house;
SQL
expect_err "cannot list a strategy without a price" 23514 <<SQL
UPDATE strategies SET status = 'listed' WHERE slug = 'btc';
SQL
expect_ok "can list once priced (rolled back)" <<SQL
BEGIN;
UPDATE strategies SET price_monthly_micro = 20000000, profit_share_bps = 1000, status = 'listed' WHERE slug = 'btc';
ROLLBACK;
SQL

# ------------------------------------------------------------------------------------------------ fixtures
expect_ok "fixtures" <<SQL
INSERT INTO users (id, firebase_uid, role) VALUES
  ('$U1', 'fb-u1', 'user'), ('$U2', 'fb-u2', 'creator'), ('$A1', 'fb-a1', 'admin'), ('$A2', 'fb-a2', 'admin');
INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative) VALUES
  ('$FEE1', 'liability', '$U1', true), ('$PAY2', 'liability', '$U2', true);
INSERT INTO strategy_versions (strategy_id, version, code_hash)
  SELECT id, 1, 'engine:test' FROM strategies WHERE slug = 'silver';
SQL

# ------------------------------------------------------------------------------------------------ ledger
echo "-- ledger integrity"
post() { # key kind amount_treasury_to_user(or custom json)
    printf "SELECT created FROM ledger_post('%s', '%s', 'test', 'tests', '%s'::jsonb);\n" "$1" "$2" "$3"
}
DEP10='[{"account":"treasury:hl_usdc","amount_micro":10000000},{"account":"'$FEE1'","amount_micro":-10000000}]'
expect_eq "ledger_post creates a balanced tx" "t" <<<"$(post dep:1 deposit "$DEP10")"
expect_eq "ledger_post replay is idempotent (created=false)" "f" <<<"$(post dep:1 deposit "$DEP10")"
expect_err "same key, different entries -> AJ409" AJ409 <<<"$(post dep:1 deposit '[{"account":"treasury:hl_usdc","amount_micro":1},{"account":"'$FEE1'","amount_micro":-1}]')"
expect_eq "balances view (raw, normal)" "-10000000|10000000" <<SQL
SELECT balance_micro, normal_balance_micro FROM ledger_balances WHERE code = '$FEE1';
SQL
expect_err "ledger_post rejects unbalanced" AJ422 <<<"$(post bad:0 deposit '[{"account":"treasury:hl_usdc","amount_micro":5},{"account":"'$FEE1'","amount_micro":-4}]')"
expect_err "ledger_post rejects unknown account" AJ404 <<<"$(post bad:u deposit '[{"account":"platform:nope","amount_micro":5},{"account":"'$FEE1'","amount_micro":-5}]')"

expect_err "direct insert: unbalanced tx rejected at COMMIT" AJ422 <<SQL
BEGIN;
INSERT INTO ledger_transactions (idempotency_key, kind, created_by, entries_digest)
  VALUES ('bad:1', 'test', 't', ledger_entries_digest(ARRAY['treasury:hl_usdc', 'stripe:clearing'], ARRAY[5, -4]::bigint[]));
INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
  SELECT t.id, a.id, 5 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'bad:1' AND a.code = 'treasury:hl_usdc';
INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
  SELECT t.id, a.id, -4 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'bad:1' AND a.code = 'stripe:clearing';
COMMIT;
SQL
expect_eq "…and nothing from it persisted" "0" <<SQL
SELECT count(*) FROM ledger_transactions WHERE idempotency_key = 'bad:1';
SQL
expect_err "direct insert: single-entry tx rejected at COMMIT" AJ422 <<SQL
BEGIN;
INSERT INTO ledger_transactions (idempotency_key, kind, created_by, entries_digest)
  VALUES ('bad:2', 'test', 't', ledger_entries_digest(ARRAY['treasury:hl_usdc'], ARRAY[5]::bigint[]));
INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
  SELECT t.id, a.id, 5 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'bad:2' AND a.code = 'treasury:hl_usdc';
COMMIT;
SQL
expect_err "direct insert: tx with no entries rejected at COMMIT" AJ422 <<SQL
INSERT INTO ledger_transactions (idempotency_key, kind, created_by, entries_digest)
  VALUES ('bad:3', 'test', 't', ledger_entries_digest('{}', '{}'));
SQL
expect_err "entries cannot be appended to an old tx (digest)" AJ422 <<SQL
BEGIN;
INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
  SELECT t.id, a.id, 7 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'dep:1' AND a.code = 'treasury:hl_usdc';
INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
  SELECT t.id, a.id, -7 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'dep:1' AND a.code = 'stripe:clearing';
COMMIT;
SQL
expect_err "supplied hash must match the canonical hash" AJ422 <<SQL
INSERT INTO ledger_transactions (idempotency_key, kind, created_by, entries_digest, hash)
  VALUES ('bad:4', 'test', 't', ledger_entries_digest('{}', '{}'), repeat('a', 64));
SQL

echo "-- non-negative fee balance"
SUB20='[{"account":"'$FEE1'","amount_micro":20000000},{"account":"platform:revenue:subscription","amount_micro":-20000000}]'
expect_err "charge beyond fee balance -> AJ402" AJ402 <<<"$(post sub:1 subscription_renewal "$SUB20")"
expect_err "direct-insert overdraft rejected at COMMIT -> AJ402" AJ402 <<SQL
BEGIN;
INSERT INTO ledger_transactions (idempotency_key, kind, created_by, entries_digest)
  VALUES ('od:1', 'post_purchase', 't', ledger_entries_digest(ARRAY['$FEE1', 'platform:revenue:posts'], ARRAY[20000000, -20000000]::bigint[]));
INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
  SELECT t.id, a.id, 20000000 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'od:1' AND a.code = '$FEE1';
INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
  SELECT t.id, a.id, -20000000 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'od:1' AND a.code = 'platform:revenue:posts';
COMMIT;
SQL
expect_eq "profit_share may overdraw (debt)" "t" <<<"$(post ps:1 profit_share '[{"account":"'$FEE1'","amount_micro":15000000},{"account":"platform:revenue:profit_share","amount_micro":-15000000}]')"
expect_eq "top-up onto a negative balance is allowed" "t" <<<"$(post dep:2 deposit '[{"account":"stripe:clearing","amount_micro":2000000},{"account":"'$FEE1'","amount_micro":-2000000}]')"
expect_err "spending while in debt -> AJ402" AJ402 <<<"$(post post:1 post_purchase '[{"account":"'$FEE1'","amount_micro":1},{"account":"platform:revenue:posts","amount_micro":-1}]')"
expect_eq "fee balance is -3 USD" "-3000000" <<SQL
SELECT normal_balance_micro FROM ledger_balances WHERE code = '$FEE1';
SQL
expect_err "fee_balance account must be a non-negative liability" 23514 <<SQL
INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative) VALUES ('user:${U2}:fee_balance', 'liability', '$U2', false);
SQL

echo "-- append-only (even for the superuser)"
for stmt in \
    "UPDATE ledger_transactions SET memo = 'x'" \
    "DELETE FROM ledger_transactions" \
    "UPDATE ledger_entries SET amount_micro = amount_micro * 2" \
    "DELETE FROM ledger_entries" \
    "TRUNCATE ledger_entries" \
    "UPDATE ledger_accounts SET non_negative = false" \
    "DELETE FROM ledger_accounts WHERE code = 'stripe:clearing'"; do
    expect_err "$stmt -> AJ403" AJ403 <<<"$stmt;"
done
expect_ok "audit_log + consents inserts" <<SQL
INSERT INTO audit_log (actor, action, target, payload) VALUES ('system:test', 'test.one', '', '{"n": 1}');
INSERT INTO audit_log (actor, action, target, payload, ip_hash) VALUES ('admin:$A1', 'flag.set', 'flag:kill_switch_global', '{"v": true, "s": "é\n"}', 'abc');
INSERT INTO consents (user_id, doc, doc_version, context) VALUES ('$U1', 'terms', '2026-09-30', 'site_entry');
SQL
for stmt in "UPDATE audit_log SET action = 'x'" "DELETE FROM audit_log" "TRUNCATE audit_log" \
            "UPDATE consents SET doc_version = 'x'" "DELETE FROM consents" "TRUNCATE consents"; do
    expect_err "$stmt -> AJ403" AJ403 <<<"$stmt;"
done
expect_err "audit_log: stale prev_hash -> AJ409" AJ409 <<SQL
INSERT INTO audit_log (actor, action, target, payload, prev_hash) VALUES ('x', 'y', '', '{}', repeat('0', 64));
SQL
expect_err "audit_log: backdated created_at -> AJ422" AJ422 <<SQL
INSERT INTO audit_log (actor, action, target, payload, created_at) VALUES ('x', 'y', '', '{}', now() - interval '1 day');
SQL

echo "-- hash chain"
expect_eq "verify_chain() is empty on an intact DB" "0" <<SQL
SELECT count(*) FROM verify_chain();
SQL
expect_eq "chain is contiguous and linked" "t" <<SQL
SELECT bool_and(t.prev_hash = coalesce(p.hash, repeat('0', 64)))
  FROM ledger_transactions t LEFT JOIN ledger_transactions p ON p.seq = t.seq - 1;
SQL

# ------------------------------------------------------------------------------------------------ constraints
echo "-- constraints"
SUBINS="INSERT INTO subscriptions (user_id, strategy_id, strategy_version_id, trading_address, allocation_micro, max_leverage_x100, status)
        SELECT '$U1', s.id, v.id, '$ADDR', 1000000000, 200, CAST(:'st' AS subscription_status)
          FROM strategies s JOIN strategy_versions v ON v.strategy_id = s.id WHERE s.slug = 'silver';"
expect_err "two ACTIVE subscriptions on one trading address -> 23505" 23505 <<SQL
BEGIN;
\set st active
$SUBINS
\set st active
$SUBINS
COMMIT;
SQL
expect_err "pending + past_due on one address -> 23505" 23505 <<SQL
BEGIN;
\set st pending
$SUBINS
\set st past_due
$SUBINS
COMMIT;
SQL
expect_ok "cancelled + paused_user + reduce_only on one address is allowed" <<SQL
\set st cancelled
$SUBINS
\set st cancelled
$SUBINS
\set st paused_user
$SUBINS
\set st reduce_only
$SUBINS
SQL
expect_err "…but not a second live one after that -> 23505" 23505 <<SQL
BEGIN;
\set st active
$SUBINS
COMMIT;
SQL
expect_err "closing occupies the address -> 23505" 23505 <<SQL
BEGIN;
UPDATE subscriptions SET status = 'cancelled' WHERE trading_address = '$ADDR' AND status = 'reduce_only';
\set st active
$SUBINS
UPDATE subscriptions SET status = 'closing', cancel_positions = 'close' WHERE trading_address = '$ADDR' AND status = 'active';
\set st active
$SUBINS
COMMIT;
SQL
expect_err "closing requires cancel_positions = close -> 23514" 23514 <<SQL
UPDATE subscriptions SET status = 'closing' WHERE trading_address = '$ADDR' AND status = 'reduce_only';
SQL
expect_err "profit_share_bps above the 12% cap -> 23514" 23514 <<SQL
UPDATE strategies SET profit_share_bps = 1201 WHERE slug = 'btc';
SQL
expect_ok "profit_share_bps 1200 is allowed" <<SQL
UPDATE strategies SET profit_share_bps = 1200 WHERE slug = 'btc';
SQL
expect_err "upper-case address rejected -> 23514" 23514 <<SQL
INSERT INTO wallets (user_id, master_address) VALUES ('$U1', '0x00000000000000000000000000000000000000AA');
SQL
expect_ok "reviews + post purchases + signals" <<SQL
INSERT INTO reviews (strategy_id, user_id, rating, eligible_since) SELECT id, '$U1', 5, now() FROM strategies WHERE slug = 'silver';
INSERT INTO posts (id, creator_id, title, body, price_micro) VALUES ('55555555-5555-4555-8555-555555555555', '$U2', 't', 'b', 2000000);
INSERT INTO post_purchases (post_id, user_id, price_micro) VALUES ('55555555-5555-4555-8555-555555555555', '$U1', 2000000);
INSERT INTO signals (strategy_id, strategy_version_id, bar_close, coin, target_weight_bps, source, signature)
  SELECT s.id, v.id, '2026-09-29T00:00:00Z', 'xyz:SILVER', 20000, 'terminal', 'sig'
    FROM strategies s JOIN strategy_versions v ON v.strategy_id = s.id WHERE s.slug = 'silver';
SQL
expect_err "second review by the same user -> 23505" 23505 <<SQL
INSERT INTO reviews (strategy_id, user_id, rating, eligible_since) SELECT id, '$U1', 4, now() FROM strategies WHERE slug = 'silver';
SQL
expect_err "rating outside 1..5 -> 23514" 23514 <<SQL
INSERT INTO reviews (strategy_id, user_id, rating, eligible_since) SELECT id, '$U2', 6, now() FROM strategies WHERE slug = 'silver';
SQL
expect_err "second purchase of the same post -> 23505" 23505 <<SQL
INSERT INTO post_purchases (post_id, user_id, price_micro) VALUES ('55555555-5555-4555-8555-555555555555', '$U1', 2000000);
SQL
expect_err "duplicate signal (version, bar_close, coin) -> 23505" 23505 <<SQL
INSERT INTO signals (strategy_id, strategy_version_id, bar_close, coin, target_weight_bps, source, signature)
  SELECT s.id, v.id, '2026-09-29T00:00:00Z', 'xyz:SILVER', 10000, 'terminal', 'sig'
    FROM strategies s JOIN strategy_versions v ON v.strategy_id = s.id WHERE s.slug = 'silver';
SQL
expect_err "unsigned terminal signal -> 23514" 23514 <<SQL
INSERT INTO signals (strategy_id, strategy_version_id, bar_close, coin, target_weight_bps, source)
  SELECT s.id, v.id, '2026-09-30T00:00:00Z', 'xyz:SILVER', 10000, 'terminal'
    FROM strategies s JOIN strategy_versions v ON v.strategy_id = s.id WHERE s.slug = 'silver';
SQL
expect_ok "referral binds once" <<SQL
UPDATE users SET referred_by = '$U2' WHERE id = '$U1';
SQL
expect_err "referral is immutable -> AJ422" AJ422 <<SQL
UPDATE users SET referred_by = '$A1' WHERE id = '$U1';
SQL
expect_err "self-referral -> 23514" 23514 <<SQL
UPDATE users SET referred_by = id WHERE id = '$U2';
SQL
expect_err "payout checker = maker -> 23514" 23514 <<SQL
INSERT INTO withdrawals (beneficiary_user_id, amount_micro, to_address, status, maker_admin, checker_admin)
  VALUES ('$U1', 1000000, '$ADDR', 'approved_2', '$A1', '$A1');
SQL
expect_ok "payout with two different admins" <<SQL
INSERT INTO withdrawals (beneficiary_user_id, amount_micro, to_address, status, maker_admin, checker_admin)
  VALUES ('$U1', 1000000, '$ADDR', 'approved_2', '$A1', '$A2');
SQL

# ------------------------------------------------------------------------------------------------ roles
echo "-- role privileges"
expect_ok "create test login users" postgres <<SQL
CREATE ROLE ${API_USER} LOGIN IN ROLE app_api;
CREATE ROLE ${EXEC_USER} LOGIN IN ROLE app_executor;
SQL
expect_ok "agent key fixture" <<SQL
INSERT INTO agent_keys (user_id, master_address, agent_address, key_ciphertext, kms_key_version)
  VALUES ('$U1', '$ADDR', '0x00000000000000000000000000000000000000bb', '\x00ff', 'v1');
SQL
expect_err "app_api: SELECT key_ciphertext -> 42501" 42501 "$DB" "$API_USER" <<<"SELECT key_ciphertext FROM agent_keys;"
expect_err "app_api: SELECT * FROM agent_keys -> 42501" 42501 "$DB" "$API_USER" <<<"SELECT * FROM agent_keys;"
expect_eq "app_api: other agent_keys columns readable" "0x00000000000000000000000000000000000000bb|active" "$DB" "$API_USER" <<SQL
SELECT agent_address, 'active' FROM agent_keys;
SQL
expect_err "app_api: SELECT code_ciphertext -> 42501" 42501 "$DB" "$API_USER" <<<"SELECT code_ciphertext FROM strategy_versions;"
expect_err "app_api: UPDATE ledger -> 42501" 42501 "$DB" "$API_USER" <<<"UPDATE ledger_transactions SET memo = 'x';"
expect_err "app_api: DELETE ledger_entries -> 42501" 42501 "$DB" "$API_USER" <<<"DELETE FROM ledger_entries;"
expect_err "app_api: DELETE audit_log -> 42501" 42501 "$DB" "$API_USER" <<<"DELETE FROM audit_log;"
expect_err "app_api: UPDATE consents -> 42501" 42501 "$DB" "$API_USER" <<<"UPDATE consents SET doc_version = 'x';"
expect_err "app_api: TRUNCATE ledger_entries -> 42501" 42501 "$DB" "$API_USER" <<<"TRUNCATE ledger_entries;"
expect_eq "app_api: can post via ledger_post" "t" "$DB" "$API_USER" <<<"$(post api:1 deposit '[{"account":"treasury:hl_usdc","amount_micro":1000000},{"account":"'$FEE1'","amount_micro":-1000000}]')"
expect_ok "app_api: can append audit_log and consents" "$DB" "$API_USER" <<SQL
INSERT INTO audit_log (actor, action, target, payload) VALUES ('user:$U1', 'consent.accept', '', '{}');
INSERT INTO consents (user_id, doc, doc_version, context) VALUES ('$U1', 'risk', '2026-09-30', 'site_entry');
SQL
expect_eq "app_api: can run verify_chain()" "0" "$DB" "$API_USER" <<<"SELECT count(*) FROM verify_chain();"
expect_err "app_api: cannot write orders -> 42501" 42501 "$DB" "$API_USER" <<SQL
INSERT INTO orders (subscription_id, cloid, coin, side, sz, limit_px) SELECT id, '0x' || repeat('1', 32), 'BTC', 'buy', 1, 1 FROM subscriptions LIMIT 1;
SQL
expect_eq "app_executor: can read key_ciphertext" "\\x00ff" "$DB" "$EXEC_USER" <<<"SELECT key_ciphertext FROM agent_keys;"
expect_err "app_executor: cannot write consents -> 42501" 42501 "$DB" "$EXEC_USER" <<SQL
INSERT INTO consents (user_id, doc, doc_version, context) VALUES ('$U1', 'risk', 'x', 'site_entry');
SQL
expect_err "app_executor: UPDATE ledger -> 42501" 42501 "$DB" "$EXEC_USER" <<<"UPDATE ledger_entries SET amount_micro = 1;"

# ------------------------------------------------------------------------------------------------ python
echo "-- python ledger service against this database"
if out=$(cd "$BACKEND" && AIJALON_TEST_DATABASE_URL="$URL" "$PY" -m unittest tests.test_ledger 2>&1) \
   && grep -q "^OK" <<<"$out" && ! grep -q "skipped" <<<"$out"; then
    ok "tests/test_ledger.py (in-memory + Postgres, $(grep -o 'Ran [0-9]* tests' <<<"$out"))"
else
    bad "tests/test_ledger.py" "$out"
fi
expect_eq "verify_chain() still empty after python writes" "0" <<<"SELECT count(*) FROM verify_chain();"

# ------------------------------------------------------------------------------------------------ tampering
echo "-- tamper detection (superuser bypasses triggers, verify_chain must notice)"
T1="${DB}_t1"; clone_db "$T1"
expect_eq "tampered entry amount -> entries digest mismatch" "ledger_transactions|1|entries digest mismatch (entries altered)" "$T1" <<SQL
SET session_replication_role = replica;
UPDATE ledger_entries SET amount_micro = amount_micro * 2
 WHERE tx_id = (SELECT id FROM ledger_transactions WHERE seq = 1);
SELECT chain, seq, reason FROM verify_chain();
SQL
T2="${DB}_t2"; clone_db "$T2"
expect_eq "tampered memo -> row hash mismatch" "ledger_transactions|2|row hash mismatch (row altered)" "$T2" <<SQL
SET session_replication_role = replica;
UPDATE ledger_transactions SET memo = 'nothing to see' WHERE seq = 2;
SELECT chain, seq, reason FROM verify_chain();
SQL
T3="${DB}_t3"; clone_db "$T3"
expect_eq "deleted middle tx -> sequence gap" "ledger_transactions|3|sequence gap: expected 2" "$T3" <<SQL
SET session_replication_role = replica;
DELETE FROM ledger_entries WHERE tx_id = (SELECT id FROM ledger_transactions WHERE seq = 2);
DELETE FROM ledger_transactions WHERE seq = 2;
SELECT chain, seq, reason FROM verify_chain();
SQL
T4="${DB}_t4"; clone_db "$T4"
expect_eq "tampered audit payload -> audit row hash mismatch" "audit_log|2|row hash mismatch (row altered)" "$T4" <<SQL
SET session_replication_role = replica;
UPDATE audit_log SET payload = '{"v": false}' WHERE seq = 2;
SELECT chain, seq, reason FROM verify_chain();
SQL
T5="${DB}_t5"; clone_db "$T5"
expect_eq "re-hashed forged row -> next row's prev_hash breaks" "ledger_transactions|2|prev_hash does not match previous row" "$T5" <<SQL
SET session_replication_role = replica;
UPDATE ledger_transactions t SET memo = 'forged' WHERE seq = 1;
UPDATE ledger_transactions t SET hash = ledger_tx_hash(t) WHERE seq = 1;
SELECT chain, seq, reason FROM verify_chain();
SQL

echo
echo "== $PASS passed, $FAIL failed"
if (( FAIL > 0 )); then printf '   - %s\n' "${FAILED[@]}"; exit 1; fi
exit 0

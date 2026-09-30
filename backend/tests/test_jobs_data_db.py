"""Integration tests for app/jobs_data (+ migrations/0006_data.sql) against a REAL migrated Postgres 16.

Every job statement runs AS THE `app_executor` ROLE (the executor service runs /internal/*), so these tests also prove
the 0006 grants. Fixtures (users, strategies, versions, subscriptions, orders, agents) are written as the connecting
superuser. Hyperliquid is a recorded/synthetic fake (never the network).

Runs when AIJALON_TEST_DATABASE_URL points at a SCRATCH database migrated through 0006 and `psql` is on PATH:
    createdb -h localhost -p 55432 -U postgres aj_data_test
    python3.12 backend/scripts/migrate.py --database-url postgresql://postgres@localhost:55432/aj_data_test
    AIJALON_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/aj_data_test \
        python3.12 -m unittest backend/tests/test_jobs_data_db.py
Re-runnable: every run uses fresh random coins / addresses / users (the signals test reuses the seeded `silver`
strategy and resets the market pause it provokes).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB_URL = os.environ.get("AIJALON_TEST_DATABASE_URL", "")


def _psql_ok() -> bool:
    try:
        return subprocess.run(["psql", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


RUN = bool(DB_URL) and _psql_ok()
PREFIX = "0xa17a1000"          # app.hl.client.CLOID_PREFIX
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
T0_MS = int(T0.timestamp() * 1000)
HOUR, DAY = 3_600_000, 86_400_000


class DbError(Exception):
    def __init__(self, sqlstate: str | None, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class RoleRunner:
    """psql-backed SqlRunner that executes each statement as `role` (None = connecting user). Bind parameters are
    rendered as literals the way psycopg would bind them (tests only). Rows come back via json_agg."""

    def __init__(self, url: str, role: str | None = "app_executor") -> None:
        self.url, self.role = url, role

    @staticmethod
    def lit(v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, (bytes, bytearray)):
            return "'\\x" + bytes(v).hex() + "'::bytea"
        if isinstance(v, (list, tuple)):
            return ("ARRAY[" + ",".join(RoleRunner.lit(x) for x in v) + "]::text[]") if v else "ARRAY[]::text[]"
        if isinstance(v, datetime):
            return "'" + v.isoformat() + "'::timestamptz"
        if isinstance(v, (date, Decimal)):
            return "'" + str(v) + "'"
        if isinstance(v, dict):
            v = json.dumps(v)
        return "'" + str(v).replace("'", "''") + "'"

    def render(self, sql: str, params: Mapping[str, Any] | None) -> str:
        params = dict(params or {})

        def sub(m: re.Match[str]) -> str:
            if m.group(1) not in params:
                raise KeyError(f"missing bind parameter {m.group(1)!r}")
            return self.lit(params[m.group(1)])

        return re.sub(r"(?<![:\w]):([A-Za-z_]\w*)(?!:)", sub, sql)

    @staticmethod
    def _final_select_at(body: str) -> int:
        depth, i, quote, last = 0, 0, False, -1
        while i < len(body):
            ch = body[i]
            if quote:
                quote = ch != "'"
            elif ch == "'":
                quote = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and body[i:i + 6].upper() == "SELECT" and (i == 0 or not body[i - 1].isalnum()):
                last = i
            i += 1
        return last

    def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        body = self.render(sql, params).strip().rstrip(";")
        head = body.lstrip().split(None, 1)[0].upper()
        modifying = re.search(r"\b(INSERT|UPDATE|DELETE)\b", body, re.IGNORECASE) is not None
        returns = head in ("SELECT", "WITH") or re.search(r"\bRETURNING\b", body, re.IGNORECASE) is not None
        if head in ("INSERT", "UPDATE", "DELETE") and returns:
            body = f"WITH q AS ({body}) SELECT coalesce(json_agg(q), '[]') FROM q"
        elif head == "WITH" and modifying:
            at = self._final_select_at(body)
            body = f"{body[:at].rstrip()}, __f AS ({body[at:]}) SELECT coalesce(json_agg(__f), '[]') FROM __f"
        elif returns:
            body = f"SELECT coalesce(json_agg(q), '[]') FROM ({body}) q"
        role = f"SET ROLE {self.role};\n" if self.role else ""
        r = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", self.url, "-f", "-"],
                           input=f"\\set VERBOSITY verbose\n{role}{body};\n", capture_output=True, text=True)
        if r.returncode != 0:
            m = re.search(r"ERROR:\s+([0-9A-Z]{5}):\s*(.*)", r.stderr)
            if m:
                from app.db.engine import DbError as AppDbError
                raise AppDbError(m.group(1), m.group(2).strip())
            raise DbError(None, r.stderr.strip())
        if not returns:
            return []
        out = r.stdout.strip()
        return json.loads(out) if out else []


class RunnerDb:
    """DatabasePort stand-in: begin() yields the role runner (each statement autocommits)."""

    def __init__(self, runner: RoleRunner) -> None:
        self.runner = runner

    def begin(self):
        from contextlib import nullcontext
        return nullcontext(self.runner)


# ---------------------------------------------------------------------------------------------------- fake /info
class FakeInfo:
    def __init__(self, tag: str = "") -> None:
        self.tag = tag
        self.candles: dict[tuple[str, str], list[dict]] = {}
        self.fills: dict[str, list[dict]] = {}
        self.funding: dict[str, list[dict]] = {}
        self.ledger: dict[str, list[dict]] = {}
        self.agents: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []

    def candle_snapshot(self, coin: str, interval: str, start: int, end: int) -> list[dict]:
        self.calls.append(("candles", coin, interval, start, end))
        return [dict(c) for c in self.candles.get((coin, interval), []) if start <= c["t"] <= end][-5000:]

    def user_fills_by_time(self, user: str, start: int, end: int | None = None, **_: Any) -> list[dict]:
        self.calls.append(("fills", user, start, end))
        return sorted([f for f in self.fills.get(user, []) if start <= f["time"] <= (end or 1 << 62)],
                      key=lambda f: f["time"])[:2000]

    def user_funding(self, user: str, start: int, end: int | None = None) -> list[dict]:
        self.calls.append(("funding", user, start, end))
        return sorted([f for f in self.funding.get(user, []) if start <= f["time"] <= (end or 1 << 62)],
                      key=lambda f: f["time"])[:500]

    def user_non_funding_ledger_updates(self, user: str, start: int, end: int | None = None) -> list[dict]:
        self.calls.append(("ledger", user, start, end))
        return [u for u in self.ledger.get(user, []) if start <= u["time"] <= (end or 1 << 62)]

    def extra_agents(self, user: str) -> list[dict]:
        self.calls.append(("agents", user))
        return list(self.agents.get(user, []))

    builder_fees: dict[str, int] = {}

    def max_builder_fee(self, user: str, builder: str) -> int:
        self.calls.append(("max_builder_fee", user, builder))
        return int(self.builder_fees.get(user, 0))

    def perp_dexs(self) -> list:
        return [None, {"name": "zz" + self.tag}]

    def meta(self, dex: str = "") -> dict:
        t = self.tag
        if dex:
            return {"universe": [{"name": f"{dex}:GOLD", "szDecimals": 2}, {"name": f"{dex}:OLD", "isDelisted": True}]}
        return {"universe": [{"name": f"A{t}", "szDecimals": 5}, {"name": f"B{t}", "szDecimals": 4},
                             {"name": f"DEAD{t}", "isDelisted": True}]}


def _addr(seed: int) -> str:
    return "0x" + hashlib.sha256(str(seed).encode()).hexdigest()[:40]


def _cloid(n: int) -> str:
    return PREFIX + f"{n:024x}"


def _fill(coin: str, side: str, px: str, sz: str, t: int, tid: int, *, start: str, closed: str = "0.0",
          fee: str = "0.01", builder: str | None = "0.005", cloid: str | None = None, oid: int = 1) -> dict:
    f = {"coin": coin, "px": px, "sz": sz, "side": side, "time": t, "startPosition": start, "dir": "x",
         "closedPnl": closed, "hash": "0x" + "ab" * 32, "oid": oid, "crossed": True, "fee": fee, "tid": tid,
         "feeToken": "USDC", "twapId": None}
    if builder is not None:
        f["builderFee"] = builder
    if cloid is not None:
        f["cloid"] = cloid
    return f


@unittest.skipUnless(RUN, "needs AIJALON_TEST_DATABASE_URL (scratch DB migrated through 0006) and psql")
class JobsDataDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ex = RoleRunner(DB_URL, "app_executor")
        cls.admin = RoleRunner(DB_URL, None)
        cls.api = RoleRunner(DB_URL, "app_api")
        cls.db = RunnerDb(cls.ex)
        cls.tag = uuid.uuid4().hex[:8]
        cls.seed = int(cls.tag, 16)

    # ------------------------------------------------------------------------------------------ fixtures
    def user(self, n: int) -> str:
        return self.admin.fetchall("INSERT INTO users (firebase_uid, email) VALUES (:f, :e) RETURNING id::text AS id",
                                   {"f": f"fb{self.tag}{n}", "e": f"u{n}{self.tag}@x.io"})[0]["id"]

    def strategy(self, markets: list[str]) -> tuple[str, str]:
        sid = self.admin.fetchall("""INSERT INTO strategies (slug, name, in_house, markets, status)
                                     VALUES (:s, 'T', true, :m, 'draft') RETURNING id::text AS id""",
                                  {"s": f"t-{self.tag}-{uuid.uuid4().hex[:6]}", "m": markets})[0]["id"]
        # in-house versions must pin params.script_sha256 (migration 0012, REVIEW_TRADING_KEYS F4)
        vid = self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash, markets, params)
                                     VALUES (CAST(:s AS uuid), 1, 'h', :m, CAST(:p AS jsonb)) RETURNING id::text AS id""",
                                  {"s": sid, "m": markets, "p": {"script_sha256": "d" * 64}})[0]["id"]
        return sid, vid

    def subscription(self, uid: str, sid: str, vid: str, addr: str, created: datetime, status: str = "active") -> str:
        return self.admin.fetchall("""
            INSERT INTO subscriptions (user_id, strategy_id, strategy_version_id, trading_address, master_address,
                                       allocation_micro, max_leverage_x100, status, created_at)
            VALUES (CAST(:u AS uuid), CAST(:s AS uuid), CAST(:v AS uuid), :a, :a2, 1000000000, 100,
                    CAST(:st AS subscription_status), :c) RETURNING id::text AS id""",
                                   {"u": uid, "s": sid, "v": vid, "a": addr, "a2": addr, "st": status, "c": created})[0]["id"]

    def order(self, sub: str, cloid: str, coin: str) -> None:
        self.admin.fetchall("""INSERT INTO orders (subscription_id, cloid, coin, side, sz, limit_px, status)
                               VALUES (CAST(:s AS uuid), :c, :coin, 'buy', 1, 1, 'filled')""",
                            {"s": sub, "c": cloid, "coin": coin})

    def events(self, like: str) -> list[dict]:
        return self.admin.fetchall("""SELECT kind, severity::text AS severity, user_id::text AS user_id, payload,
                                             dedup_key FROM events_outbox WHERE dedup_key LIKE :d ORDER BY id""",
                                   {"d": like})

    # ------------------------------------------------------------------------------------------ grants
    def test_grants_and_guards(self) -> None:
        from app.db.engine import DbError as AppDbError
        coin = f"g{self.tag}:X"
        self.ex.fetchall("""INSERT INTO candles (coin, interval, open_time, o, h, l, c, v)
                            VALUES (:c, '1d', '2026-01-01T00:00:00Z', 1.5, 2, 1, 1.75, 0)""", {"c": coin})
        with self.assertRaises(AppDbError) as cm:
            self.ex.fetchall("UPDATE candles SET o = 2 WHERE coin = :c", {"c": coin})
        self.assertIn(cm.exception.sqlstate, ("AJ403", "42501"))
        with self.assertRaises(AppDbError) as cm:
            self.admin.fetchall("UPDATE candles SET o = 2 WHERE coin = :c", {"c": coin})
        self.assertEqual(cm.exception.sqlstate, "AJ403")                 # immutable even for the owner
        self.assertEqual(self.api.fetchall("SELECT o::text AS o FROM candles WHERE coin = :c", {"c": coin}),
                         [{"o": "1.5"}])
        with self.assertRaises(AppDbError) as cm:
            self.api.fetchall("""INSERT INTO candles (coin, interval, open_time, o, h, l, c, v)
                                 VALUES (:c, '1h', now(), 1, 1, 1, 1, 0)""", {"c": coin})
        self.assertEqual(cm.exception.sqlstate, "42501")
        with self.assertRaises(AppDbError):
            self.ex.fetchall("DELETE FROM candles WHERE coin = :c", {"c": coin})
        # outbox: executor + api insert; delivery columns only; event immutable; no DELETE
        rid = self.api.fetchall("""INSERT INTO events_outbox (kind, payload, dedup_key) VALUES ('test_alert', '{}', :d)
                                   RETURNING id""", {"d": f"g:{self.tag}"})[0]["id"]
        self.ex.fetchall("UPDATE events_outbox SET attempts = 1, last_error = 'x' WHERE id = :i", {"i": rid})
        with self.assertRaises(AppDbError) as cm:
            self.ex.fetchall("UPDATE events_outbox SET kind = 'other' WHERE id = :i", {"i": rid})
        self.assertEqual(cm.exception.sqlstate, "42501")                 # column privilege
        with self.assertRaises(AppDbError) as cm:
            self.admin.fetchall("UPDATE events_outbox SET kind = 'other' WHERE id = :i", {"i": rid})
        self.assertEqual(cm.exception.sqlstate, "AJ422")
        self.ex.fetchall("UPDATE events_outbox SET delivered_at = now() WHERE id = :i", {"i": rid})
        with self.assertRaises(AppDbError) as cm:
            self.ex.fetchall("UPDATE events_outbox SET delivered_at = now() + interval '1 s' WHERE id = :i", {"i": rid})
        self.assertEqual(cm.exception.sqlstate, "AJ403")
        with self.assertRaises(AppDbError):
            self.api.fetchall("DELETE FROM events_outbox WHERE id = :i", {"i": rid})
        with self.assertRaises(AppDbError):
            self.ex.fetchall("INSERT INTO events_outbox (kind) VALUES ('bad.kind')")
        # agent_keys: the api role sees valid_until but not the ciphertext; 'expired' is a status
        self.api.fetchall("SELECT valid_until, valid_until_checked_at FROM agent_keys LIMIT 1")
        self.assertEqual(self.admin.fetchall("SELECT 'expired'::agent_key_status::text AS s"), [{"s": "expired"}])
        self.assertEqual(self.admin.fetchall("""SELECT kind::text AS k, non_negative FROM ledger_accounts
                                                WHERE code = 'suspense:usdc_unattributed'"""),
                         [{"k": "liability", "non_negative": True}])   # REVIEW_MONEY M5 (0010): never overdrawn

    # ------------------------------------------------------------------------------------------ candles
    def test_candles_sync_backfill_incremental_mismatch(self) -> None:
        from app.hl.fake import load_fixture
        from app.jobs_data.candles import DbCandleSource, candles_sync, history_days, listing_history
        from app.sandbox.backtest import StoredFirstFetcher

        coin = f"c{self.tag}:SILVER"
        fx = load_fixture("candleSnapshot_xyz_SILVER_1d")
        info = FakeInfo()
        info.candles[(coin, "1d")] = [{**c, "s": coin} for c in fx]
        last_t = fx[-1]["t"]
        now = datetime.fromtimestamp((last_t + DAY // 2) / 1000, tz=timezone.utc)   # last candle still forming
        rep = candles_sync(self.db, now, info=info, coins=[coin], intervals=("1d",), weight_per_minute=100_000)
        self.assertEqual((rep["processed"], rep["backfilled"], rep["inserted"], rep["errors"]),
                         (1, 1, len(fx) - 1, []))
        got = self.api.fetchall("""SELECT o::text AS o, v::text AS v, trades FROM candles WHERE coin = :c AND interval = '1d'
                                   ORDER BY open_time""", {"c": coin})
        self.assertEqual(len(got), len(fx) - 1)
        self.assertEqual((got[0]["o"], got[0]["trades"]), (fx[0]["o"], fx[0]["n"]))
        self.assertIn("1896261.1799999999", [g["v"] for g in got])        # exact API string, never a float
        # nothing due until the forming candle has closed
        rep = candles_sync(self.db, now + timedelta(hours=1), info=info, coins=[coin], intervals=("1d",),
                           weight_per_minute=100_000)
        self.assertEqual((rep["due"], rep["requests"]), (0, 0))
        # next day: the forming candle is closed → stored; an overlap candle changed upstream → alert, no overwrite
        info.candles[(coin, "1d")][-2] = {**info.candles[(coin, "1d")][-2], "c": "99.999"}
        later = now + timedelta(days=1)
        rep = candles_sync(self.db, later, info=info, coins=[coin], intervals=("1d",), weight_per_minute=100_000)
        self.assertEqual((rep["inserted"], rep["mismatches"]), (1, 1))
        stored_c = self.api.fetchall(f"""SELECT c::text AS c FROM candles WHERE coin = :c AND interval = '1d'
                                         AND open_time = timestamptz 'epoch' + {fx[-2]['t']} * interval '1 millisecond'""",
                                     {"c": coin})
        self.assertEqual(stored_c, [{"c": fx[-2]["c"]}])
        ev = self.events(f"candle_mismatch:{coin}|1d:%")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["user_id"], ev[0]["payload"]["fetched"]["c"]), (None, "99.999"))
        # re-running is idempotent (same alert deduped)
        candles_sync(self.db, later + timedelta(days=1), info=info, coins=[coin], intervals=("1d",),
                     weight_per_minute=100_000)
        self.assertEqual(len(self.events(f"candle_mismatch:{coin}|1d:%")), 1)
        cur = self.admin.fetchall("SELECT cursor_ms FROM job_cursors WHERE job = 'candles' AND key = :k",
                                  {"k": f"{coin}|1d"})
        self.assertEqual(cur, [{"cursor_ms": last_t}])

        # read side: stored first, API only for bars after the store
        class Api:
            calls: list = []

            def candles(self, c: str, iv: str, s: int, e: int) -> list[dict]:
                Api.calls.append((s, e))
                return [{"t": last_t + DAY, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "n": 3}] if s <= last_t + DAY <= e else []

            def funding(self, c: str, s: int, e: int) -> list[dict]:
                return []

        f = StoredFirstFetcher(DbCandleSource(self.db), Api())
        rows = f.candles(coin, "1d", 0, last_t + 2 * DAY)
        self.assertEqual(len(rows), len(fx) + 1)
        self.assertEqual(Api.calls, [(last_t + DAY, last_t + 2 * DAY)])
        self.assertEqual(f.source_notes[coin]["stored"]["bars"], len(fx))
        self.assertEqual(f.source_notes[coin]["api"]["bars"], 1)
        self.assertEqual(rows[0]["o"], fx[0]["o"])
        days = history_days(self.db, coin, "1d")
        self.assertEqual(days, len(fx))
        lh = listing_history(self.db, [coin], "1d")
        self.assertEqual((lh["eligible"], lh["short_history"], lh["label"]),
                         (False, True, f"Short history ({len(fx)} days)"))
        self.assertEqual(history_days(self.api, f"none{self.tag}:X", "1d"), 0)

    def test_candles_universe_budget_and_priority(self) -> None:
        from app.jobs_data.candles import candles_sync

        info = FakeInfo(self.tag)
        names = [f"A{self.tag}", f"B{self.tag}", f"zz{self.tag}:GOLD"]
        base = T0_MS - T0_MS % DAY
        for c in names:
            info.candles[(c, "1d")] = [{"t": base - k * DAY, "T": base - k * DAY + DAY - 1, "s": c, "i": "1d",
                                        "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "n": 5}
                                       for k in range(3, 0, -1)]
        saved = self.admin.fetchall("SELECT cursor_ms, state FROM job_cursors WHERE job = 'candles_universe'")
        self.addCleanup(lambda: self.admin.fetchall(
            "UPDATE job_cursors SET cursor_ms = :c, state = CAST(:s AS jsonb) WHERE job = 'candles_universe'",
            {"c": saved[0]["cursor_ms"], "s": json.dumps(saved[0]["state"])}) if saved else None)
        rep = candles_sync(self.db, T0, info=info, intervals=("1d",), max_requests=2, weight_per_minute=100_000,
                           universe_ttl_seconds=0)
        self.assertTrue(rep["universe_refreshed"])
        self.assertEqual((rep["markets"], rep["requests"], rep["remaining_due"]), (3, 2, 1))
        cached = self.admin.fetchall("SELECT state FROM job_cursors WHERE job = 'candles_universe'")[0]["state"]
        self.assertEqual(cached["coins"], sorted(names))

    # ------------------------------------------------------------------------------------------ fills
    def test_fills_ingest_attribution_events_idempotency(self) -> None:
        from app.jobs_data.fills import fills_ingest

        uid = self.user(1)
        sid, vid = self.strategy(["BTC"])
        addr = _addr(self.seed)
        sub = self.subscription(uid, sid, vid, addr, T0 - timedelta(days=2))
        c_open, c_close, c_orphan, c_spot = _cloid(self.seed * 4), _cloid(self.seed * 4 + 1), _cloid(self.seed * 4 + 2), _cloid(self.seed * 4 + 3)
        self.order(sub, c_open, "BTC")
        self.order(sub, c_close, "BTC")
        t1, t2 = T0_MS - 20 * HOUR, T0_MS - 2 * HOUR
        tid = self.seed * 100
        info = FakeInfo()
        info.fills[addr] = [
            _fill("BTC", "B", "80000.0", "0.001", t1, tid + 1, start="0.0", fee="0.0114", builder="0.008",
                  cloid=c_open, oid=11),
            _fill("BTC", "B", "80010.0", "0.002", t1, tid + 2, start="0.001", fee="0.0229", builder="0.016",
                  cloid=c_open, oid=11),
            _fill("BTC", "A", "81000.0", "0.003", t2, tid + 3, start="0.003", closed="2.97", fee="0.035",
                  builder="0.0243", cloid=c_close, oid=12),
            _fill("BTC", "B", "81000.0", "0.1", t2 + 5, tid + 4, start="0.0", cloid=None, builder=None),   # user's own
            _fill("ETH", "B", "3000.0", "0.01", t1 + 1, tid + 5, start="0.0", cloid=c_orphan),     # ours, no sub
            _fill("@151", "A", "1.0", "1.0", t2, tid + 6, start="1.0", cloid=c_spot),                     # spot w/ prefix
        ]
        rep = fills_ingest(self.db, T0, info=info, weight_per_minute=100_000)
        self.assertEqual(rep["errors"], [])
        self.assertEqual((rep["inserted"], rep["attributed"], rep["unattributed"], rep["foreign"], rep["rejected"]),
                         (4, 3, 1, 1, 1))
        rows = self.admin.fetchall("""SELECT tid, subscription_id::text AS sub, net_pnl_micro, closed_pnl_micro, fee_micro,
                                             builder_fee_micro, attributed_via, px::text AS px
                                        FROM fills WHERE trading_address = :a ORDER BY tid""", {"a": addr})
        by_tid = {r["tid"]: r for r in rows}
        self.assertEqual(set(by_tid), {tid + 1, tid + 2, tid + 3, tid + 5})
        self.assertEqual(by_tid[tid + 3]["net_pnl_micro"], 2_935_000)            # floor((2.97 − 0.035) × 1e6)
        self.assertEqual((by_tid[tid + 3]["sub"], by_tid[tid + 3]["attributed_via"]), (sub, "cloid"))
        self.assertEqual(by_tid[tid + 1]["net_pnl_micro"], -11_400)
        self.assertIsNone(by_tid[tid + 5]["sub"])
        self.assertEqual(by_tid[tid + 1]["px"], "80000.0")
        ev = self.events(f"trade_%:{sub}:%")
        self.assertEqual([e["kind"] for e in ev], ["trade_opened", "trade_closed"])
        opened, closed = ev[0]["payload"], ev[1]["payload"]
        self.assertEqual((opened["size"], opened["avg_px"], opened["fees_micro"], opened["position_after"]),
                         ("0.003", "80006.66666666", 34_300, "0.003"))
        self.assertEqual((closed["realized_pnl_micro"], closed["net_pnl_micro"], closed["position_after"]),
                         (2_970_000, 2_935_000, "0"))
        self.assertEqual(ev[0]["user_id"], uid)
        self.assertNotIn(addr, json.dumps(ev))                                   # never the full address
        self.assertEqual(len(self.events(f"fill_unattributed:{addr}:%")), 1)
        # idempotent re-run (overlap re-fetch) → nothing new; late fill after settlement → ops event (warn)
        self.admin.fetchall("UPDATE subscriptions SET pnl_cursor = :p WHERE id = CAST(:s AS uuid)",
                            {"p": T0, "s": sub})
        c_late = _cloid(self.seed * 4 + 9)
        self.order(sub, c_late, "BTC")
        info.fills[addr].append(_fill("BTC", "B", "80000.0", "0.001", T0_MS - 5 * 60_000, tid + 7, start="0.0",
                                      cloid=c_late, oid=13))
        rep = fills_ingest(self.db, T0 + timedelta(minutes=5), info=info, weight_per_minute=100_000)
        self.assertEqual((rep["inserted"], rep["late_fills"]), (1, 1))
        late = self.events(f"fill_after_settlement:{addr}:%")
        # REVIEW_MONEY M3: booked into the next settlement (claimed), so no longer critical
        self.assertEqual((len(late), late[0]["severity"]), (1, "warn"))
        rep = fills_ingest(self.db, T0 + timedelta(minutes=10), info=info, weight_per_minute=100_000)
        self.assertEqual(rep["inserted"], 0)
        # settlement reads realized PnL from net_pnl_micro of the subscription's fills (pnl_since contract)
        s = self.admin.fetchall("""SELECT sum(net_pnl_micro) AS s FROM fills WHERE subscription_id = CAST(:s AS uuid)""",
                                {"s": sub})[0]["s"]
        self.assertEqual(s, -11_400 - 22_900 + 2_935_000 - 10_000)

    # ------------------------------------------------------------------------------------------ funding
    def test_funding_scan_hourly_attribution_and_buckets(self) -> None:
        from app.jobs_data.fills import fills_ingest
        from app.jobs_data.funding import funding_scan

        uid = self.user(2)
        sid, vid = self.strategy(["BTC"])
        addr = _addr(self.seed + 1)
        start_day = T0_MS - T0_MS % DAY - 12 * DAY
        sub = self.subscription(uid, sid, vid, addr, datetime.fromtimestamp(start_day / 1000, tz=timezone.utc))
        c1 = _cloid(self.seed * 4 + 5)
        self.order(sub, c1, "BTC")
        info = FakeInfo()
        t_open = start_day + 2 * HOUR
        tid = self.seed * 100 + 50
        info.fills[addr] = [_fill("BTC", "B", "80000.0", "0.01", t_open, tid, start="0.0", cloid=c1)]
        fills_ingest(self.db, T0, info=info, weight_per_minute=100_000)

        def ev(t: int, coin: str, usdc: str, szi: str, n: int | None = None) -> dict:
            return {"time": t, "hash": "0x" + "0" * 64, "delta": {"type": "funding", "coin": coin, "usdc": usdc,
                                                                    "szi": szi, "fundingRate": "0.0000125",
                                                                    "nSamples": n}}
        d0 = start_day
        info.funding[addr] = [
            ev(d0, "BTC", "-0.24", "0.01", 22),                         # daily aggregate (sub held from 02:00)
            ev(d0 + DAY, "BTC", "0.30", "0.01", 24),                    # aggregate income → dropped (estimate)
            ev(T0_MS - 3 * HOUR + 7, "BTC", "-0.010001", "0.02"),       # hourly; account holds 2× ours → half
            ev(T0_MS - 2 * HOUR + 3, "BTC", "0.004", "0.01"),           # hourly income, full share
            ev(T0_MS - 2 * HOUR + 3, "ETH", "-1.0", "-3"),              # not a strategy coin → stored, unattributed
        ]
        rep = funding_scan(self.db, T0, info=info, weight_per_minute=100_000)
        self.assertEqual(rep["errors"], [])
        self.assertEqual(rep["inserted"], 5)
        rows = {(r["coin"], r["t"]): r for r in self.admin.fetchall("""
            SELECT coin, (floor(extract(epoch FROM time) * 1000))::bigint AS t, usdc_micro, attributed_micro,
                   subscription_id::text AS sub, n_samples, estimated
              FROM funding_events WHERE trading_address = :a""", {"a": addr})}
        agg0 = rows[("BTC", d0)]
        self.assertTrue(agg0["estimated"])
        self.assertEqual(agg0["sub"], sub)
        self.assertLess(agg0["attributed_micro"], 0)                              # estimated cost kept
        self.assertEqual(rows[("BTC", d0 + DAY)]["attributed_micro"], 0)          # estimated income dropped
        self.assertEqual(rows[("BTC", T0_MS - 3 * HOUR + 7)]["attributed_micro"], -5_001)   # floor(−0.0050005)
        self.assertEqual(rows[("BTC", T0_MS - 2 * HOUR + 3)]["attributed_micro"], 4_000)
        self.assertIsNone(rows[("ETH", T0_MS - 2 * HOUR + 3)]["sub"])
        self.assertEqual(rows[("ETH", T0_MS - 2 * HOUR + 3)]["usdc_micro"], -1_000_000)
        rep = funding_scan(self.db, T0 + timedelta(hours=1), info=info, weight_per_minute=100_000)
        self.assertEqual(rep["inserted"], 0)                                      # idempotent

        # partial daily bucket: hourly rows of a day stored, then (after an outage) only the aggregate comes back
        addr2 = _addr(self.seed + 2)
        sub2 = self.subscription(uid, sid, vid, addr2, datetime.fromtimestamp(start_day / 1000, tz=timezone.utc))
        c2 = _cloid(self.seed * 4 + 6)
        self.order(sub2, c2, "BTC")
        info.fills[addr2] = [_fill("BTC", "B", "80000.0", "0.01", start_day + HOUR, tid + 1, start="0.0", cloid=c2)]
        fills_ingest(self.db, T0, info=info, weight_per_minute=100_000)
        d5 = start_day + 5 * DAY
        info.funding[addr2] = [ev(d5 + h * HOUR + 1, "BTC", "-0.01", "0.01") for h in range(10)]
        funding_scan(self.db, datetime.fromtimestamp((d5 + 10 * HOUR + 60_000) / 1000, tz=timezone.utc), info=info,
                     weight_per_minute=100_000)
        info.funding[addr2] = [ev(d5, "BTC", "-0.24", "0.01", 24)]
        self.admin.fetchall("UPDATE job_cursors SET cursor_ms = :c WHERE job = 'funding' AND key = :k",
                            {"c": d5 + 10 * HOUR, "k": addr2})
        rep = funding_scan(self.db, T0, info=info, weight_per_minute=100_000)
        self.assertEqual(rep["partial_buckets"], 1)
        res = self.admin.fetchall(f"""SELECT usdc_micro, attributed_micro, n_samples FROM funding_events
                                      WHERE trading_address = :a AND time = timestamptz 'epoch' + {d5} * interval '1 millisecond'""",
                                  {"a": addr2})
        self.assertEqual(res, [{"usdc_micro": -140_000, "attributed_micro": -140_000, "n_samples": 14}])
        total = self.admin.fetchall("""SELECT sum(usdc_micro) AS s FROM funding_events WHERE trading_address = :a""",
                                    {"a": addr2})[0]["s"]
        self.assertEqual(total, -240_000)                                         # never double counted

    # ------------------------------------------------------------------------------------------ deposits
    def test_deposits_scan_credit_hold_idempotent(self) -> None:
        from app.jobs_data.deposits import deposits_scan
        from app.money import usd

        treasury = _addr(self.seed + 10)
        uid = self.user(3)
        w = _addr(self.seed + 11)
        # verified BEFORE its transfers (REVIEW_AUTH_API F8: older transfers are held, never credited)
        self.admin.fetchall("INSERT INTO wallets (user_id, master_address, verified_at) VALUES (CAST(:u AS uuid), :a, :t)",
                            {"u": uid, "a": w, "t": (T0 - timedelta(days=1)).isoformat()})
        stranger = _addr(self.seed + 12)

        def send(sender: str, amount: str, t: int, n: int) -> dict:
            return {"time": t, "hash": "0x" + hashlib.sha256(f"{self.tag}{n}".encode()).hexdigest(),
                    "delta": {"type": "send", "user": sender, "destination": treasury, "sourceDex": "",
                              "destinationDex": "", "token": "USDC", "amount": amount, "usdcValue": amount, "fee": "0.0",
                              "nativeTokenFee": "0.0", "nonce": t, "feeToken": ""}}
        info = FakeInfo()
        info.ledger[treasury] = [send(w, "25.5", T0_MS - HOUR, 1), send(stranger, "40.0", T0_MS - HOUR + 1, 2),
                                 send(w, "3.0", T0_MS - HOUR + 2, 3),
                                 {"time": T0_MS - 50, "hash": "0x" + "cd" * 32, "delta": {"type": "deposit", "usdc": "500.0"}}]
        settings = SimpleNamespace(treasury_address=treasury, hl_api_url="https://x",
                                   economics=SimpleNamespace(min_topup_micro=usd(10)))
        rep = deposits_scan(self.db, T0, info=info, settings=settings, weight_per_minute=100_000)
        self.assertEqual(rep["errors"], [])
        self.assertEqual((rep["credited"], rep["credited_micro"], rep["held"], rep["held_micro"], rep["unattributable"]),
                         (1, 25_500_000, 2, 43_000_000, 1))
        bal = self.admin.fetchall("SELECT balance_micro FROM ledger_balances WHERE code = :c",
                                  {"c": f"user:{uid}:fee_balance"})
        self.assertEqual(bal, [{"balance_micro": -25_500_000}])
        held = self.admin.fetchall("""
            SELECT a.code, e.amount_micro FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
              JOIN ledger_transactions t ON t.id = e.tx_id
             WHERE t.kind = 'deposit_held' AND t.idempotency_key IN (:k2, :k3) ORDER BY a.code, e.amount_micro""",
                                   {"k2": "usdc_hl:" + info.ledger[treasury][1]["hash"],
                                    "k3": "usdc_hl:" + info.ledger[treasury][2]["hash"]})
        self.assertEqual(held, [{"code": "suspense:usdc_unattributed", "amount_micro": -40_000_000},
                                {"code": "suspense:usdc_unattributed", "amount_micro": -3_000_000},
                                {"code": "treasury:hl_usdc", "amount_micro": 3_000_000},
                                {"code": "treasury:hl_usdc", "amount_micro": 40_000_000}])
        # 0009: the on-chain sender of each held transfer is recorded (admin refunds go back to exactly it)
        rec = self.admin.fetchall("""SELECT h.tx_hash, h.sender_address, h.amount_micro, t.kind FROM usdc_held_deposits h
                                       JOIN ledger_transactions t ON t.id = h.held_tx_id
                                      WHERE h.tx_hash IN (:h2, :h3) ORDER BY h.amount_micro""",
                                  {"h2": info.ledger[treasury][1]["hash"], "h3": info.ledger[treasury][2]["hash"]})
        self.assertEqual([(r["sender_address"], r["amount_micro"], r["kind"]) for r in rec],
                         [(w, 3_000_000, "deposit_held"), (stranger, 40_000_000, "deposit_held")])
        dep = self.admin.fetchall("""SELECT status::text AS s, amount_micro, withdrawable, method::text AS m
                                     FROM deposits WHERE user_id = CAST(:u AS uuid)""", {"u": uid})
        self.assertEqual(dep, [{"s": "credited", "amount_micro": 25_500_000, "withdrawable": True, "m": "usdc_hl"}])
        credited = self.admin.fetchall("""SELECT kind, user_id::text AS u FROM events_outbox
                                           WHERE dedup_key LIKE 'topup_%' AND user_id = CAST(:u AS uuid) ORDER BY id""",
                                       {"u": uid})
        self.assertEqual([c["kind"] for c in credited], ["topup_credited", "topup_held"])
        ops = self.admin.fetchall("""SELECT payload FROM events_outbox WHERE user_id IS NULL AND kind = 'topup_held'
                                      AND dedup_key LIKE :d""", {"d": f"ops:topup_held:0x{hashlib.sha256(f'{self.tag}2'.encode()).hexdigest()}"})
        self.assertEqual(len(ops), 1)
        self.assertNotIn(stranger, json.dumps(ops))
        # idempotent: a second scan (overlap) books nothing new
        rep = deposits_scan(self.db, T0 + timedelta(minutes=5), info=info, settings=settings, weight_per_minute=100_000)
        self.assertEqual((rep["credited"], rep["held"], rep["already_booked"]), (0, 0, 3))
        # the stranger verifies later: the held transfer is NOT credited a second time
        self.admin.fetchall("INSERT INTO wallets (user_id, master_address, verified_at) VALUES (CAST(:u AS uuid), :a, now())",
                            {"u": self.user(4), "a": stranger})
        self.admin.fetchall("DELETE FROM job_cursors WHERE job = 'deposits' AND key = :k", {"k": treasury})
        rep = deposits_scan(self.db, T0 + timedelta(minutes=10), info=info, settings=settings, weight_per_minute=100_000)
        self.assertEqual((rep["credited"], rep["errors"]), (0, []))
        self.assertEqual(len(self.events(f"topup_held_now_attributable:%{hashlib.sha256(f'{self.tag}2'.encode()).hexdigest()}")), 1)

    # ------------------------------------------------------------------------------------------ agents
    def test_agent_expiry_scan(self) -> None:
        from app.jobs_data.agents import agent_expiry_scan

        info = FakeInfo()
        uid = self.user(5)
        ids = {}
        for n, (valid_days, listed) in enumerate([(10, True), (2.5, True), (-1, False), (30, False), (0.5, True)]):
            master, agent = _addr(self.seed + 20 + n), _addr(self.seed + 40 + n)
            ids[n] = self.admin.fetchall("""
                INSERT INTO agent_keys (user_id, master_address, agent_address, key_ciphertext, kms_key_version, status,
                                        valid_until)
                VALUES (CAST(:u AS uuid), :m, :a, '\\x01'::bytea, 'v1', 'active', :vu) RETURNING id::text AS id""",
                                         {"u": uid, "m": master, "a": agent,
                                          "vu": T0 + timedelta(days=valid_days) if n == 2 else None})[0]["id"]
            if listed:
                info.agents[master] = [{"name": "aijalon", "address": agent,
                                        "validUntil": T0_MS + int(valid_days * DAY)}]
        rep = agent_expiry_scan(self.db, T0, info=info)
        self.assertEqual(rep["errors"], [])
        st = {r["id"]: r for r in self.admin.fetchall("""SELECT id::text AS id, status::text AS status,
                                                                 (floor(extract(epoch FROM valid_until) * 1000))::bigint AS vu
                                                            FROM agent_keys WHERE user_id = CAST(:u AS uuid)""", {"u": uid})}
        self.assertEqual(st[ids[0]]["vu"], T0_MS + 10 * DAY)
        self.assertEqual(st[ids[2]]["status"], "expired")                        # known validUntil passed
        self.assertEqual(st[ids[3]]["status"], "active")                         # first miss: not revoked yet
        ev = self.admin.fetchall("""SELECT kind, severity::text AS s, payload FROM events_outbox
                                     WHERE user_id = CAST(:u AS uuid) ORDER BY id""", {"u": uid})
        kinds = [(e["kind"], e["payload"].get("threshold_days")) for e in ev]
        self.assertIn(("agent_expiring", 14), kinds)
        self.assertIn(("agent_expiring", 3), kinds)
        self.assertIn(("agent_expiring", 1), kinds)
        self.assertIn(("agent_expired", None), kinds)
        # dedupe: thresholds already sent are not re-emitted; the second consecutive miss revokes agent 3
        n_before = len(ev)
        agent_expiry_scan(self.db, T0 + timedelta(hours=1), info=info)
        kinds = [e["kind"] for e in self.admin.fetchall(
            "SELECT kind FROM events_outbox WHERE user_id = CAST(:u AS uuid) ORDER BY id", {"u": uid})]
        self.assertEqual((len(kinds), kinds[-1]), (n_before + 1, "agent_revoked"))
        st = {r["id"]: r["status"] for r in self.admin.fetchall(
            "SELECT id::text AS id, status::text AS status FROM agent_keys WHERE user_id = CAST(:u AS uuid)", {"u": uid})}
        self.assertEqual(st[ids[3]], "revoked")
        agent_expiry_scan(self.db, T0 + timedelta(days=1, hours=13), info=info)   # agent 1: < 1 day; agent 4 expired
        st = {r["id"]: r["status"] for r in self.admin.fetchall(
            "SELECT id::text AS id, status::text AS status FROM agent_keys WHERE user_id = CAST(:u AS uuid)", {"u": uid})}
        self.assertEqual((st[ids[1]], st[ids[4]]), ("active", "expired"))
        ev = self.admin.fetchall("SELECT kind, payload FROM events_outbox WHERE user_id = CAST(:u AS uuid) ORDER BY id",
                                 {"u": uid})
        kinds = [e["kind"] for e in ev]
        self.assertEqual((kinds.count("agent_revoked"), kinds.count("agent_expired")), (1, 2))
        self.assertEqual(ev[-2]["payload"].get("threshold_days") if ev[-2]["kind"] == "agent_expiring"
                         else ev[-1]["payload"].get("threshold_days"), 1)

    def test_agent_scan_builder_approval_missing_daily(self) -> None:
        """SPEC §12 mandatory builder_approval_missing: masters with an active agent AND a live subscription are
        re-checked (maxBuilderFee) once per scan; below the required fee → one critical event per (user, master, day);
        no live subscription / no builder configured → no check."""
        from app.jobs_data.agents import agent_expiry_scan

        info = FakeInfo()
        info.builder_fees = {}
        builder = "0x" + "b" * 40
        settings = SimpleNamespace(builder_address=builder, hl_api_url="http://unused",
                                   economics=SimpleNamespace(builder_fee_tenths_bp=100))
        uid, idle = self.user(60), self.user(61)
        sid, vid = self.strategy(["BTC"])
        masters = {}
        for n, (u, live, fee) in enumerate([(uid, True, 50), (uid, True, 100), (idle, False, 0)]):
            master, agent = _addr(self.seed + 70 + n), _addr(self.seed + 80 + n)
            masters[n] = master
            self.admin.fetchall("""
                INSERT INTO agent_keys (user_id, master_address, agent_address, key_ciphertext, kms_key_version, status)
                VALUES (CAST(:u AS uuid), :m, :a, '\\x01'::bytea, 'v1', 'active')""", {"u": u, "m": master, "a": agent})
            info.agents[master] = [{"name": "aijalon", "address": agent, "validUntil": T0_MS + 100 * DAY}]
            info.builder_fees[master] = fee
            if live:
                self.subscription(u, sid, vid, master, T0 - timedelta(days=1))
        rep = agent_expiry_scan(self.db, T0, info=info, settings=settings)
        self.assertEqual(rep["errors"], [])
        checked = {c[1] for c in info.calls if c[0] == "max_builder_fee"}
        self.assertEqual(checked, {masters[0], masters[1]})                    # idle master (no live sub) skipped
        ev = self.admin.fetchall("""SELECT user_id::text AS u, severity::text AS s, payload FROM events_outbox
                                     WHERE kind = 'builder_approval_missing' AND user_id IN (CAST(:a AS uuid), CAST(:b AS uuid))""",
                                 {"a": uid, "b": idle})
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["u"], ev[0]["s"], ev[0]["payload"]["approved_tenths_bp"],
                          ev[0]["payload"]["required_tenths_bp"], ev[0]["payload"]["where"]),
                         (uid, "critical", 50, 100, "daily_scan"))
        self.assertNotIn(masters[0], json.dumps(ev[0]["payload"]))              # short address only
        agent_expiry_scan(self.db, T0 + timedelta(hours=3), info=info, settings=settings)   # same day: no repeat
        agent_expiry_scan(self.db, T0 + timedelta(days=1), info=info, settings=settings)    # next day: again
        n = self.admin.fetchall("""SELECT count(*) AS n FROM events_outbox WHERE kind = 'builder_approval_missing'
                                    AND user_id = CAST(:u AS uuid)""", {"u": uid})[0]["n"]
        self.assertEqual(n, 2)
        # no builder configured → no check at all
        calls = len(info.calls)
        agent_expiry_scan(self.db, T0 + timedelta(days=2), info=info,
                          settings=SimpleNamespace(builder_address="", hl_api_url="x", economics=settings.economics))
        self.assertFalse([c for c in info.calls[calls:] if c[0] == "max_builder_fee"])

    # ------------------------------------------------------------------------------------------ signals
    def test_signals_ingest_store_duplicate_and_reject(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        from app.config import RiskLimits
        from app.strategies.signals import canonical_json
        from app.strategies.signals import ingest as signals_ingest

        silver = self.admin.fetchall("SELECT id::text AS id FROM strategies WHERE slug = 'silver'")[0]["id"]
        if not self.admin.fetchall("SELECT 1 FROM strategy_versions WHERE strategy_id = CAST(:s AS uuid)", {"s": silver}):
            self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash) VALUES
                                   (CAST(:s AS uuid), 1, 'terminal')""", {"s": silver})
        pinned = self.admin.fetchall("""SELECT params->>'script_sha256' AS p FROM strategy_versions
                                        WHERE strategy_id = CAST(:s AS uuid) ORDER BY version DESC LIMIT 1""",
                                     {"s": silver})[0]["p"]
        self.admin.fetchall("""UPDATE system_flags SET value = 'false', updated_by = 'test'
                                WHERE key = 'new_entries_paused:xyz:SILVER'""")
        key = Ed25519PrivateKey.generate()
        pub = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        as_of = (now - timedelta(days=1)).date()
        script = pinned or "a" * 64                       # 0005 pins version 1 to the vendored script hash
        feed = {"as_of": as_of.isoformat(), "engine_sha256": hashlib.sha256(canonical_json({"silver": script})).hexdigest(),
                "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "strategies": {"silver": {"last_action": "SELL", "last_action_date": "1980-01-15",
                                          "market": "xyz:SILVER", "script_sha256": script, "status": "trades",
                                          "target_weight": 0}}}
        body = canonical_json(feed)
        sig = base64.b64encode(key.sign(body))

        class Resp:
            def __init__(self, data: bytes) -> None:
                self.status_code, self.headers, self._d = 200, {}, data

            def iter_content(self, chunk_size: int = 0):
                yield self._d

            def close(self) -> None:
                pass

        class Session:
            def __init__(self, b: bytes, s: bytes) -> None:
                self.b, self.s = b, s

            def get(self, url: str, **_: Any) -> Resp:
                return Resp(self.s if url.endswith(".sig") else self.b)

        settings = SimpleNamespace(signals_pubkey_b64=pub, signals_url="https://t.example/signals.json",
                                   in_house_listed=("silver",), risk=RiskLimits())
        out = signals_ingest(db=self.db, now=now, settings=settings, session=Session(body, sig))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["stored"] + out["duplicates"], 1)
        out2 = signals_ingest(db=self.db, now=now, settings=settings, session=Session(body, sig))
        self.assertEqual((out2["stored"], out2["duplicates"], out2["conflicts"]), (0, 1, 0))
        row = self.admin.fetchall("""SELECT s.target_weight_bps, s.source::text AS src, s.signature, s.raw
                                       FROM signals s JOIN strategy_versions v ON v.id = s.strategy_version_id
                                      WHERE v.strategy_id = CAST(:s AS uuid) AND s.bar_close = :b""",
                                  {"s": silver, "b": datetime.combine(as_of + timedelta(days=1), datetime.min.time(),
                                                                       tzinfo=timezone.utc)})
        self.assertEqual((row[0]["target_weight_bps"], row[0]["src"]), (0, "terminal"))
        if out["stored"] == 1:                                    # (a re-run on the same day keeps the first row)
            self.assertEqual(row[0]["signature"], sig.decode())
        self.assertEqual(row[0]["raw"]["as_of"], as_of.isoformat())
        # tampered body → rejected, ops event, SILVER entries paused (reset afterwards)
        try:
            bad = signals_ingest(db=self.db, now=now, settings=settings,
                                 session=Session(body.replace(b'"trades"', b'"holds!"'), sig))
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["error"], "signature_invalid")
            self.assertEqual(bad["paused_markets"] in (["xyz:SILVER"], []), True)
            flag = self.admin.fetchall("SELECT value::text AS v FROM system_flags WHERE key = 'new_entries_paused:xyz:SILVER'")
            self.assertEqual(flag, [{"v": "true"}])
            self.assertTrue(self.admin.fetchall("""SELECT 1 FROM events_outbox WHERE kind = 'signals_signature_invalid'
                                                   AND user_id IS NULL AND severity = 'critical'"""))
        finally:
            self.admin.fetchall("""UPDATE system_flags SET value = 'false', updated_by = 'test'
                                    WHERE key = 'new_entries_paused:xyz:SILVER'""")


if __name__ == "__main__":
    unittest.main()

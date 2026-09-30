"""End-to-end execution integration against a REAL PostgreSQL 16 (+ FakeHyperliquid).

What runs for real: every SQL statement of app.execution.pg (as the ``app_executor`` role — so grants are proven),
the ledger (ledger_post, deferred balance checks, hash chain), advisory locks, transactions (UnitOfWork), KMS
envelope decryption of agent keys and creator code (local AES wrapper), the executor/settlement/reconcile logic,
the Hyperliquid readers over FakeInfo, the notifier's in-app alert rows, and the sandbox HTTP service (/run).
Faked: the exchange (app.hl.fake), candles (synthetic), on-chain builder rewards / treasury readers.

Needs ``psql`` and a cluster where the admin URL's user is a superuser. Creates a throwaway
database (migrations 0001…0005 at least, all present ones when they apply) and drops it afterwards
(AIJALON_KEEP_TEST_DB=1 keeps it). Admin URL: AIJALON_TEST_PG_ADMIN_URL (default
postgresql://postgres@localhost:55432/postgres). Skipped when the cluster is unreachable.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.db.engine import DbError  # noqa: E402

logging.getLogger("app").setLevel(logging.CRITICAL)
logging.getLogger("sandbox").setLevel(logging.CRITICAL)

UTC = timezone.utc
ADMIN_URL = os.environ.get("AIJALON_TEST_PG_ADMIN_URL", "postgresql://postgres@localhost:55432/postgres")
SILVER = "xyz:SILVER"
BUILDER = "0x" + "b1" * 20
TREASURY = "0x" + "7e" * 20
SANDBOX_SECRET = "s" * 24


def _tools_ok() -> bool:
    if not shutil.which("psql"):
        return False
    try:
        r = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-d", ADMIN_URL, "-c", "SELECT 1"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "1"


RUN = _tools_ok()


# ======================================================================================================== SQL runners

def lit(v: Any) -> str:
    """Render a bind value as a SQL literal the way psycopg would bind it (tests only)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return "'\\x" + bytes(v).hex() + "'::bytea"
    if isinstance(v, (list, tuple)):
        return ("ARRAY[" + ",".join(lit(x) for x in v) + "]::text[]") if v else "ARRAY[]::text[]"
    if isinstance(v, datetime):
        return "'" + v.isoformat() + "'::timestamptz"
    if isinstance(v, (date, Decimal)):
        return "'" + str(v) + "'"
    if isinstance(v, dict):
        v = json.dumps(v)
    return "'" + str(v).replace("'", "''") + "'"


_BIND = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)(?!:)")


def render(sql: str, params: Mapping[str, Any] | None) -> str:
    params = dict(params or {})

    def sub(m: re.Match[str]) -> str:
        if m.group(1) not in params:
            raise KeyError(f"missing bind parameter {m.group(1)!r}")
        return lit(params[m.group(1)])

    return _BIND.sub(sub, sql)


def _final_select_at(body: str) -> int:
    depth, i, quote, last = 0, 0, False, -1
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == "'":
                quote = False
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


def wrap(sql: str, params: Mapping[str, Any] | None) -> tuple[str, bool]:
    """Statement → (SQL that prints one JSON array line when it returns rows, returns_rows)."""
    body = render(sql, params).strip().rstrip(";")
    head = body.lstrip().split(None, 1)[0].upper()
    modifying = re.search(r"\b(INSERT|UPDATE|DELETE)\b", body, re.IGNORECASE) is not None
    returns = head in ("SELECT", "WITH") or re.search(r"\bRETURNING\b", body, re.IGNORECASE) is not None
    if head in ("INSERT", "UPDATE", "DELETE") and returns:
        body = f"WITH q AS ({body}) SELECT coalesce(json_agg(q), '[]') FROM q"
    elif head == "WITH" and modifying:
        at = _final_select_at(body)
        body = f"{body[:at].rstrip()}, __final AS ({body[at:]}) SELECT coalesce(json_agg(__final), '[]') FROM __final"
    elif returns:
        body = f"SELECT coalesce(json_agg(q), '[]') FROM ({body}) q"
    return body, returns


_ERR = re.compile(r"ERROR:\s+([0-9A-Z]{5}):\s*(.*)")


class PsqlSession:
    """One long-lived psql process = one DB session (real transactions / advisory locks across statements)."""

    MARK = "__AJ_END_OF_STATEMENT__"

    def __init__(self, url: str, role: str | None) -> None:
        self.p = subprocess.Popen(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=0", "-d", url],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  bufsize=1)
        self._sp = 0
        self.execute("\\set VERBOSITY verbose", meta=True)
        if role:
            self.execute(f"SET ROLE {role}")

    def execute(self, body: str, *, meta: bool = False) -> list[str]:
        assert self.p.stdin is not None and self.p.stdout is not None
        self.p.stdin.write(body + ("\n" if meta else ";\n") + f"\\echo {self.MARK}\n")
        self.p.stdin.flush()
        lines = []
        while True:
            line = self.p.stdout.readline()
            if line == "":
                raise RuntimeError("psql session died")
            line = line.rstrip("\n")
            if line == self.MARK:
                break
            lines.append(line)
        for ln in lines:
            m = _ERR.search(ln)
            if m:
                raise DbError(m.group(1), m.group(2).strip())
            if ln.startswith("ERROR:"):
                raise DbError(None, ln)
        return lines

    def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        body, returns = wrap(sql, params)
        lines = self.execute(body)
        if not returns:
            return []
        text = "\n".join(lines).strip()
        if not text:
            return []
        return json.loads(text[text.index("["):])

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        self._sp += 1
        name = f"sp_{self._sp}"
        self.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.execute(f"ROLLBACK TO SAVEPOINT {name}")
            raise
        self.execute(f"RELEASE SAVEPOINT {name}")

    def close(self) -> None:
        try:
            if self.p.stdin:
                self.p.stdin.write("\\q\n")
                self.p.stdin.flush()
            self.p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.p.kill()
            self.p.wait(timeout=10)
        finally:
            for f in (self.p.stdin, self.p.stdout):
                if f is not None:
                    f.close()


class RoleRunner:
    """SqlRunner for the tests: each ``fetchall`` = its own session (autocommit) as ``role``; ``atomic()`` /
    ``session()`` open a real transaction on a long-lived psql session (UnitOfWork, advisory xact locks)."""

    def __init__(self, url: str, role: str | None = "app_executor") -> None:
        self.url = url
        self.role = role
        self.statements = 0

    def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        self.statements += 1
        body, returns = wrap(sql, params)
        role = f"SET ROLE {self.role};\n" if self.role else ""
        r = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", self.url, "-f", "-"],
                           input=f"\\set VERBOSITY verbose\n{role}{body};\n", capture_output=True, text=True)
        if r.returncode != 0:
            m = _ERR.search(r.stderr)
            raise DbError(m.group(1) if m else None, (m.group(2) if m else r.stderr).strip())
        if not returns:
            return []
        out = r.stdout.strip()
        return json.loads(out[out.index("["):]) if out else []

    @contextmanager
    def _tx(self) -> Iterator[PsqlSession]:
        s = PsqlSession(self.url, self.role)
        try:
            s.execute("BEGIN")
            try:
                yield s
            except BaseException:
                s.execute("ROLLBACK")
                raise
            s.execute("COMMIT")
        finally:
            s.close()

    def atomic(self) -> Any:
        return self._tx()

    def session(self) -> Any:
        return self._tx()


# ======================================================================================================== database

def _migrate(url: str, files: list[Path]) -> subprocess.CompletedProcess:
    d = Path(tempfile.mkdtemp(prefix="aj_mig_"))
    for f in files:
        shutil.copy(f, d / f.name)
    try:
        return subprocess.run([sys.executable, str(BACKEND / "scripts" / "migrate.py"), "--database-url", url,
                               "--dir", str(d), "--driver", "psql"], capture_output=True, text=True, timeout=300)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _db_url(name: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{name}"


class LiveDb:
    """Throwaway database migrated with every file ≤ 0005 plus later ones when they apply cleanly."""

    def __init__(self) -> None:
        self.name = f"aj_exec_{os.getpid()}_{secrets.token_hex(3)}"
        self.url = _db_url(self.name)
        self.applied: list[str] = []
        self.note = ""

    def create(self) -> None:
        files = sorted((BACKEND / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
        upto = os.environ.get("AIJALON_TEST_MIGRATIONS_UPTO")          # e.g. 0005: schema without later modules
        if upto:
            files = [f for f in files if f.name[:4] <= upto]
        mine = [f for f in files if f.name[:4] <= "0005"]
        for attempt in (files, mine):
            subprocess.run(["psql", "-X", "-q", "-d", ADMIN_URL, "-c", f'CREATE DATABASE "{self.name}"'],
                           capture_output=True)
            r = _migrate(self.url, attempt)
            if r.returncode == 0:
                self.applied = [f.name for f in attempt]
                return
            self.note = (r.stdout + r.stderr)[-2000:]
            self.drop()
        raise RuntimeError(f"migrations failed: {self.note}")

    def drop(self) -> None:
        if os.environ.get("AIJALON_KEEP_TEST_DB") == "1":
            return
        subprocess.run(["psql", "-X", "-q", "-d", ADMIN_URL, "-c",
                        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{self.name}'"],
                       capture_output=True)
        subprocess.run(["psql", "-X", "-q", "-d", ADMIN_URL, "-c", f'DROP DATABASE IF EXISTS "{self.name}"'],
                       capture_output=True)


# ======================================================================================================== fixtures

def addr(tag: str) -> str:
    h = uuid.uuid5(uuid.NAMESPACE_URL, tag).hex + uuid.uuid5(uuid.NAMESPACE_DNS, tag).hex
    return "0x" + h[:40]


class ZeroJitter:
    def delay_seconds(self, user_id: str, bar_close: datetime) -> int:
        return 0

    def fair_order(self, ids, bar_close):  # noqa: ANN001
        return sorted(ids)


class Const:
    def __init__(self, v: int) -> None:
        self.v = v

    def cumulative_builder_rewards_micro(self) -> int:
        return self.v

    def treasury_usdc_micro(self) -> int:
        return self.v


class SyntheticCandleInfo:
    """FakeInfo whose candle_snapshot serves synthetic closed daily bars ending at any requested range."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.candle_calls: list[tuple] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def candle_snapshot(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        self.candle_calls.append((coin, interval, start_ms, end_ms))
        iv = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
        out = []
        t = start_ms - start_ms % iv
        while t <= end_ms:
            px = Decimal(60) + Decimal((t // iv) % 7)
            out.append({"t": t, "T": t + iv - 1, "s": coin, "i": interval, "o": str(px), "h": str(px + 1),
                        "l": str(px - 1), "c": str(px + Decimal("0.5")), "v": "1000", "n": 10})
            t += iv
        return out


CREATOR_CODE = '''MARKETS = ["xyz:SILVER"]
TIMEFRAME = "1d"
LOOKBACK = 50
MAX_LEVERAGE = 2


def signal(bars):
    rows = bars["xyz:SILVER"]
    if rows[-1]["c"] > rows[0]["c"]:
        return {"xyz:SILVER": 1.5}
    return {"xyz:SILVER": 0.25}
'''


@unittest.skipUnless(RUN, "needs psql and a reachable PostgreSQL (AIJALON_TEST_PG_ADMIN_URL)")
class ExecIntegrationDbTest(unittest.TestCase):
    db_: LiveDb

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_ = LiveDb()
        cls.db_.create()
        cls.admin = RoleRunner(cls.db_.url, role=None)          # fixtures (what the API / data jobs would write)
        cls.exe = RoleRunner(cls.db_.url, role="app_executor")   # everything under test
        cls.api = RoleRunner(cls.db_.url, role="app_api")
        from app.config import get_settings

        cls.settings = replace(get_settings(), env="test", kms_key_name="", service_role="executor",
                               local_dev_kek_b64=base64.b64encode(secrets.token_bytes(32)).decode(),
                               builder_address=BUILDER, treasury_address=TREASURY,
                               audit_pepper_b64=base64.b64encode(b"p" * 32).decode(),
                               sandbox_shared_secret=SANDBOX_SECRET, email_provider_api_key="",
                               telegram_bot_token="")
        from app.security.kms import make_encryptor

        cls.enc = make_encryptor(cls.settings)
        cls.silver = cls.admin.fetchall("""SELECT st.id::text AS sid, v.id::text AS vid FROM strategies st
                                           JOIN strategy_versions v ON v.strategy_id = st.id AND v.version = 1
                                           WHERE st.slug = 'silver'""")[0]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db_.drop()

    # ------------------------------------------------------------------------------------------------ helpers
    def setUp(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.created_subs: list[str] = []

    def tearDown(self) -> None:
        # never let one test's live subscriptions be traded by another test's tick
        for sid in self.created_subs:
            self.admin.fetchall("""UPDATE subscriptions SET status = 'cancelled', cancel_positions = 'leave',
                                   cancelled_at = coalesce(cancelled_at, now()) WHERE id = CAST(:s AS uuid)""", {"s": sid})
        self.admin.fetchall("UPDATE system_flags SET value = 'false'::jsonb WHERE value <> 'false'::jsonb")

    def hl_world(self) -> tuple[Any, Any, Any]:
        from app.hl.client import BuilderCode
        from app.hl.fake import FakeGatewayFactory, FakeHyperliquid, FakeInfo
        from app.hl.markets import MarketCatalog

        hl = FakeHyperliquid(now_ms=int(self.now.timestamp() * 1000) - 600_000)
        info = FakeInfo(hl)
        hl.catalog = MarketCatalog.from_info(info, ["xyz"])
        mid = hl.catalog.ctx(SILVER).mid_px
        hl.set_mid(SILVER, mid)
        return hl, info, FakeGatewayFactory(hl, BuilderCode(BUILDER, 100))

    def runtime(self, hl: Any, info: Any, gw: Any, **kw: Any) -> Any:
        from app.execution.executor import ExecutorConfig
        from app.execution.jobs import Runtime

        kw.setdefault("jitter", ZeroJitter())
        kw.setdefault("builder_rewards", Const(0))
        kw.setdefault("treasury", Const(0))
        kw.setdefault("executor_config", ExecutorConfig.from_settings(self.settings, unknown_order_grace_seconds=0))
        return Runtime(self.settings, info=info, gateways=gw, **kw)

    def user(self, tag: str, *, referred_by: str | None = None, contacts: bool = True) -> str:
        """A user; with ``contacts`` (default) Telegram is linked and the alert email confirmed (SPEC §12: without
        them the executor's entries gate runs every subscription of the user reduce-only)."""
        t = f"{tag}{uuid.uuid4().hex[:8]}"
        uid = self.admin.fetchall("""
            INSERT INTO users (firebase_uid, email, referral_code, referred_by, mfa_enrolled)
            VALUES (:u, :e, :c, CAST(:r AS uuid), true) RETURNING id::text AS id""",
            {"u": "fb" + t, "e": f"{t}@example.test", "c": "R" + t[:20], "r": referred_by})[0]["id"]
        if contacts and "0007_alerts.sql" in self.db_.applied:
            self.link_contacts(uid)
        return uid

    def link_contacts(self, uid: str) -> None:
        chat = int(uuid.uuid4().int % 10**12) + 10**12
        self.admin.fetchall("""
            INSERT INTO user_contacts (user_id, telegram_chat_id, telegram_linked_at, email, email_verified_at)
            VALUES (CAST(:u AS uuid), :c, now(), :e, now())
            ON CONFLICT (user_id) DO UPDATE SET telegram_chat_id = EXCLUDED.telegram_chat_id,
                   telegram_linked_at = EXCLUDED.telegram_linked_at, telegram_blocked_at = NULL,
                   telegram_block_reason = NULL, email = EXCLUDED.email, email_verified_at = EXCLUDED.email_verified_at
            RETURNING user_id""", {"u": uid, "c": chat, "e": f"c{chat}@example.test"})

    def connect_wallet(self, user_id: str, master: str) -> str:
        """What the API does on POST /agents + confirm: seal a fresh agent key for this user (AAD = user + agent)."""
        from app.security.agent_keys import generate_sealed_agent_key

        sk = generate_sealed_agent_key(self.enc, user_id=user_id)
        self.admin.fetchall("INSERT INTO wallets (user_id, master_address, verified_at) VALUES (CAST(:u AS uuid), :m, now())",
                            {"u": user_id, "m": master})
        self.admin.fetchall("""INSERT INTO agent_keys (user_id, master_address, agent_address, key_ciphertext, kms_key_version,
                                                       status, approved_at)
                               VALUES (CAST(:u AS uuid), :m, :a, :k, :v, 'active', now())""",
                            {"u": user_id, "m": master, "a": sk.address, "k": sk.ciphertext, "v": sk.key_version})
        return sk.address

    def strategy(self, *, slug: str | None = None) -> tuple[str, str]:
        """An isolated in-house strategy + published version (tests must not share signal streams)."""
        slug = slug or f"t-{uuid.uuid4().hex[:10]}"
        sid = self.admin.fetchall("""INSERT INTO strategies (slug, name, in_house, markets, timeframe, price_monthly_micro,
                                                             profit_share_bps, status)
                                     VALUES (:s, :s, true, ARRAY['xyz:SILVER'], '1d', 0, 0, 'listed')
                                     RETURNING id::text AS id""", {"s": slug})[0]["id"]
        vid = self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash, markets, timeframe,
                                                                    max_leverage, published_at, live_since)
                                     VALUES (CAST(:s AS uuid), 1, :h, ARRAY['xyz:SILVER'], '1d', 2, now(), now())
                                     RETURNING id::text AS id""", {"s": sid, "h": "c" * 64})[0]["id"]
        return sid, vid

    def subscribe(self, user_id: str, sid: str, vid: str, master: str, *, allocation: int = 1_000_000_000,
                  status: str = "active") -> str:
        sub = self.admin.fetchall("""
            INSERT INTO subscriptions (user_id, strategy_id, strategy_version_id, trading_address, master_address,
                                       allocation_micro, max_leverage_x100, status, current_period_end)
            VALUES (CAST(:u AS uuid), CAST(:s AS uuid), CAST(:v AS uuid), :a, :a, :al, 200,
                    CAST(:st AS subscription_status), now() + interval '30 days')
            RETURNING id::text AS id""", {"u": user_id, "s": sid, "v": vid, "a": master, "al": allocation,
                                          "st": status})[0]["id"]
        self.created_subs.append(sub)
        return sub

    def signal(self, sid: str, vid: str, bar: datetime, weight_bps: int, source: str = "terminal") -> None:
        self.admin.fetchall("""INSERT INTO signals (strategy_id, strategy_version_id, bar_close, coin, target_weight_bps,
                                                    source, signature)
                               VALUES (CAST(:s AS uuid), CAST(:v AS uuid), CAST(:b AS timestamptz), 'xyz:SILVER', :w,
                                       CAST(:src AS signal_source), 'sig')""",
                            {"s": sid, "v": vid, "b": bar, "w": weight_bps, "src": source})

    def topup(self, user_id: str, amount: int) -> None:
        from app.ledger.service import post_transaction

        post_transaction(self.admin, f"test-topup:{user_id}:{uuid.uuid4().hex}", "deposit_usdc", "test top-up",
                         [("treasury:hl_usdc", amount), (f"user:{user_id}:fee_balance", -amount)], "test")

    def ingest_fills(self, hl: Any, address: str) -> int:
        """Stand-in for the data-jobs fills sync: attribute by cloid (orders table) and store (idempotent)."""
        from app.hl.fills import attribute_fills

        orders = self.admin.fetchall("""SELECT o.cloid, o.subscription_id::text AS s FROM orders o
                                        JOIN subscriptions s ON s.id = o.subscription_id WHERE s.trading_address = :a""",
                                     {"a": address})
        res = attribute_fills(hl.account(address).fills, trading_address=address,
                              cloid_to_subscription={o["cloid"]: o["s"] for o in orders})
        cols = {r["column_name"] for r in self.admin.fetchall(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'fills'")}
        n = 0
        for a in res.attributed:
            f = a.fill
            extra_cols, extra_vals = "", ""
            params = {"s": a.subscription_id, "a": address, "coin": f.coin, "tid": f.tid, "oid": f.oid,
                      "px": str(f.px), "sz": str(f.sz), "side": "buy" if f.side == "B" else "sell",
                      "cp": f.closed_pnl_micro, "fee": f.fee_micro, "bf": f.builder_fee_micro, "cl": f.cloid,
                      "t": datetime.fromtimestamp(f.time_ms / 1000, UTC), "raw": dict(f.raw)}
            if "net_pnl_micro" in cols:
                extra_cols, extra_vals = ", net_pnl_micro, attributed_via", ", :net, :via"
                params.update(net=f.net_pnl_micro, via=a.via)
            n += len(self.admin.fetchall(f"""
                INSERT INTO fills (subscription_id, trading_address, coin, tid, oid, px, sz, side, closed_pnl_micro,
                                   fee_micro, builder_fee_micro, cloid, time, raw{extra_cols})
                VALUES (CAST(:s AS uuid), :a, :coin, :tid, :oid, CAST(:px AS numeric), CAST(:sz AS numeric),
                        CAST(:side AS order_side), :cp, :fee, :bf, :cl, :t, CAST(:raw AS jsonb){extra_vals})
                ON CONFLICT (trading_address, tid) DO NOTHING RETURNING id""", params))
        return n

    def ledger_ok(self) -> None:
        total = self.admin.fetchall("SELECT coalesce(sum(amount_micro), 0)::bigint AS s FROM ledger_entries")[0]["s"]
        self.assertEqual(total, 0)
        unbalanced = self.admin.fetchall("""SELECT tx_id FROM ledger_entries GROUP BY tx_id
                                            HAVING sum(amount_micro) <> 0""")
        self.assertEqual(unbalanced, [])
        self.assertEqual(self.admin.fetchall("SELECT * FROM verify_chain()"), [])

    def orders_of(self, sub: str) -> list[dict]:
        return self.admin.fetchall("""SELECT cloid, coin, side::text AS side, sz::text AS sz, reduce_only,
                                             status::text AS status, attempt, bar_close
                                        FROM orders WHERE subscription_id = CAST(:s AS uuid)
                                       ORDER BY created_at, attempt""", {"s": sub})

    def sub_row(self, sub: str) -> dict:
        return self.admin.fetchall("""SELECT status::text AS status, cancelled_at, cum_pnl_micro, hwm_micro, pnl_cursor,
                                             consecutive_rejections FROM subscriptions WHERE id = CAST(:s AS uuid)""",
                                   {"s": sub})[0]

    def alerts_of(self, kind: str) -> list[dict]:
        return self.admin.fetchall("""SELECT user_id::text AS user_id, severity::text AS severity, payload
                                        FROM alerts WHERE kind = :k ORDER BY created_at""", {"k": kind})

    # ================================================================================================ tests
    def test_00_migration_seed_and_grants(self) -> None:
        v = self.admin.fetchall("""SELECT v.code_hash, v.params, v.markets, v.timeframe, v.max_leverage,
                                          v.published_at IS NOT NULL AS pub, v.live_since IS NOT NULL AS live
                                     FROM strategy_versions v JOIN strategies st ON st.id = v.strategy_id
                                    WHERE st.slug = 'silver' AND v.version = 1""")[0]
        manifest = json.loads((BACKEND.parent / "signals" / "vendor" / "MANIFEST.json").read_text())
        self.assertEqual(v["code_hash"], manifest["scripts"]["silver"]["sha256"])
        self.assertEqual(v["params"]["script_sha256"], manifest["scripts"]["silver"]["sha256"])
        self.assertEqual(v["params"]["source"], "terminal")
        self.assertEqual((v["markets"], v["timeframe"], v["max_leverage"], v["pub"], v["live"]),
                         (["xyz:SILVER"], "1d", 2, True, True))
        # grants: executor reads/writes its tables; api reads reports; nobody deletes
        self.exe.fetchall("SELECT count(*) FROM reconciliation_reports")
        self.api.fetchall("SELECT count(*) FROM reconciliation_reports")
        with self.assertRaises(DbError) as cm:
            self.api.fetchall("INSERT INTO reconciliation_reports (date_key, report) VALUES ('2026-01-01', '{}')")
        self.assertEqual(cm.exception.sqlstate, "42501")
        with self.assertRaises(DbError):
            self.exe.fetchall("DELETE FROM reconciliation_reports")
        with self.assertRaises(DbError):
            self.exe.fetchall("DELETE FROM orders")

    def test_10_end_to_end_signal_order_fill_settle_ledger(self) -> None:
        from app.execution import jobs
        from app.execution.executor import make_cloid
        from app.hl.client import CLOID_PREFIX

        hl, info, gw = self.hl_world()
        rt = self.runtime(hl, info, gw)
        sid, vid = self.silver["sid"], self.silver["vid"]
        referrer = self.user("ref")
        u = self.user("sub", referred_by=referrer)
        master = addr("e2e-" + u)
        self.connect_wallet(u, master)
        sub = self.subscribe(u, sid, vid, master, allocation=2_000_000_000)   # $2,000
        self.topup(u, 50_000_000)
        bar1 = self.now - timedelta(minutes=10)
        self.signal(sid, vid, bar1, 10_000)                                   # weight 1 → long $2,000

        r1 = jobs.run_tick(db=self.exe, now=self.now, runtime=rt)
        self.assertEqual((r1["orders_placed"], r1["orders_filled"], r1["errors"]), (1, 1, 0), r1)
        o = self.orders_of(sub)
        self.assertEqual(len(o), 1)
        self.assertEqual(o[0]["cloid"], make_cloid(sub, bar1, SILVER, 0))
        self.assertTrue(o[0]["cloid"].startswith("0x" + CLOID_PREFIX))
        self.assertEqual((o[0]["side"], o[0]["status"], o[0]["reduce_only"]), ("buy", "filled", False))
        wire = hl.orders_log[-1]
        self.assertEqual(wire["builder"], {"b": BUILDER, "f": 100})
        self.assertEqual(wire["tif"], "Ioc")
        self.assertEqual(wire["cloid"], o[0]["cloid"])
        self.assertGreater(hl.position(master, SILVER), 0)
        self.assertEqual(self.admin.fetchall("""SELECT outcome FROM subscription_bar_runs
                                                WHERE subscription_id = CAST(:s AS uuid)""", {"s": sub}),
                         [{"outcome": "ok"}])
        tgt = self.admin.fetchall("SELECT target_notional_micro FROM subscription_targets WHERE subscription_id = CAST(:s AS uuid)",
                                  {"s": sub})[0]["target_notional_micro"]
        self.assertGreater(tgt, 1_900_000_000)

        # idempotent retry: same bar again → nothing new
        r1b = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=30), runtime=rt)
        self.assertEqual(r1b["orders_placed"], 0)
        self.assertEqual(len(self.orders_of(sub)), 1)

        # next bar: flat (weight 0), price moved up → profit on the reduce-only close
        hl.set_mid(SILVER, hl.mids[SILVER] * Decimal("1.004"))
        hl.tick(60_000)
        bar2 = self.now - timedelta(minutes=5)
        self.signal(sid, vid, bar2, 0)
        r2 = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=40), runtime=rt)
        self.assertEqual((r2["orders_placed"], r2["orders_filled"]), (1, 1), r2)
        o2 = self.orders_of(sub)[-1]
        self.assertEqual((o2["side"], o2["reduce_only"], o2["status"]), ("sell", True, "filled"))
        self.assertEqual(hl.position(master, SILVER), 0)

        # fills sync (data jobs) → settlement at 00:30 next day
        self.assertEqual(self.ingest_fills(hl, master), 2)
        fills = self.admin.fetchall("""SELECT tid::text AS tid, builder_fee_micro, closed_pnl_micro, fee_micro
                                         FROM fills WHERE trading_address = :a ORDER BY time""", {"a": master})
        builder_total = sum(f["builder_fee_micro"] for f in fills)
        realized = sum(f["closed_pnl_micro"] - f["fee_micro"] for f in fills)
        self.assertGreater(builder_total, 0)
        self.assertGreater(realized, 0)
        settle_now = datetime.combine(self.now.date() + timedelta(days=1), datetime.min.time(), UTC) + timedelta(minutes=30)
        rep = jobs.settle_daily(db=self.exe, now=settle_now, settle_date=self.now.date(), runtime=rt)
        self.assertEqual(rep["errors"], [], rep)
        self.assertEqual(rep["cutoff"], settle_now.replace(minute=0).isoformat())
        charge = realized * 150 // 10_000                                  # SILVER: 0% creator + 1.5% platform on top
        self.assertEqual(rep["profit_share_charged_micro"], charge)
        self.assertGreaterEqual(rep["builder_fills_recognised"], 2)
        s = self.sub_row(sub)
        self.assertEqual((s["cum_pnl_micro"], s["hwm_micro"]), (realized, realized))
        from app.ledger.service import get_balance

        self.assertEqual(get_balance(self.admin, f"user:{u}:fee_balance"), -(50_000_000 - charge))
        # builder fee split: in-house → creator share to platform; referral pool 20% × starter 50% to the referrer
        bf_keys = [r["idempotency_key"] for r in self.admin.fetchall(
            "SELECT idempotency_key FROM ledger_transactions WHERE idempotency_key LIKE :p", {"p": f"bf:{master}:%"})]
        self.assertEqual(sorted(bf_keys), sorted(f"bf:{master}:{f['tid']}" for f in fills))
        ref_payable = -get_balance(self.admin, f"referrer:{referrer}:payable")
        self.assertEqual(ref_payable, sum(f["builder_fee_micro"] * 2000 // 10_000 * 5000 // 10_000 for f in fills))
        self.assertTrue(self.admin.fetchall(
            "SELECT 1 FROM ledger_transactions WHERE idempotency_key = :k", {"k": f"ps:{sub}:{settle_now.date()}"}))
        self.ledger_ok()

        # user event: profit_share_charged (events_outbox, same tx as the ledger post), with amount + period
        if "0006_data.sql" in self.db_.applied:
            ev = self.admin.fetchall("""SELECT payload, severity::text AS severity FROM events_outbox
                                         WHERE kind = 'profit_share_charged' AND user_id = CAST(:u AS uuid)""", {"u": u})
            self.assertEqual(len(ev), 1)
            self.assertEqual((ev[0]["payload"]["amount_micro"], ev[0]["payload"]["profit_micro"],
                              ev[0]["payload"]["rate_bps"], ev[0]["payload"]["strategy_id"]),
                             (charge, realized, 150, sid))
            self.assertEqual(ev[0]["payload"]["period_end"], rep["cutoff"])

        # settlement is idempotent
        n_tx = self.admin.fetchall("SELECT count(*) AS n FROM ledger_transactions")[0]["n"]
        rep2 = jobs.settle_daily(db=self.exe, now=settle_now + timedelta(minutes=5), settle_date=self.now.date(), runtime=rt)
        self.assertEqual(rep2["profit_share_charged_micro"], 0)
        self.assertEqual(self.admin.fetchall("SELECT count(*) AS n FROM ledger_transactions")[0]["n"], n_tx)
        if "0006_data.sql" in self.db_.applied:
            self.assertEqual(self.admin.fetchall("""SELECT count(*) AS n FROM events_outbox WHERE kind = 'profit_share_charged'
                                                    AND user_id = CAST(:u AS uuid)""", {"u": u})[0]["n"], 1)
        self.ledger_ok()

        # reconcile: stored + readable by the API role; builder fees DB vs chain
        rt_ok = self.runtime(hl, info, gw, builder_rewards=Const(self.exe.fetchall(
            "SELECT coalesce(sum(builder_fee_micro), 0)::bigint AS s FROM fills")[0]["s"]))
        rec = jobs.reconcile(db=self.exe, now=settle_now + timedelta(minutes=30), runtime=rt_ok)
        self.assertFalse(rec["builder_mismatch"])
        latest = jobs.latest_reconciliation(self.api)
        self.assertEqual(latest["date_key"], settle_now.date().isoformat())
        self.assertEqual(latest["builder_db_micro"], rec["builder_db_micro"])

    def test_20_unknown_outcome_resolved_never_resent(self) -> None:
        from app.execution import jobs

        hl, info, gw = self.hl_world()
        rt = self.runtime(hl, info, gw)
        sid, vid = self.strategy()
        u = self.user("crash")
        master = addr("crash-" + u)
        self.connect_wallet(u, master)
        sub = self.subscribe(u, sid, vid, master)
        bar = self.now - timedelta(minutes=3)
        self.signal(sid, vid, bar, 10_000)
        hl.script("raise_after_fill")      # the order executed, but the response was lost
        r1 = jobs.run_tick(db=self.exe, now=self.now, runtime=rt)
        self.assertEqual(r1["orders_unknown"], 1, r1)
        self.assertEqual([o["status"] for o in self.orders_of(sub)], ["unknown"])
        r2 = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=60), runtime=rt)
        self.assertEqual(r2["orders_placed"], 0, r2)
        self.assertEqual([o["status"] for o in self.orders_of(sub)], ["filled"])
        self.assertEqual(len(hl.orders_log), 1)
        self.assertEqual(self.sub_row(sub)["consecutive_rejections"], 0)

    def test_30_closing_flow_then_cancelled(self) -> None:
        from app.execution import jobs

        hl, info, gw = self.hl_world()
        rt = self.runtime(hl, info, gw)
        sid, vid = self.strategy()
        u = self.user("close")
        master = addr("close-" + u)
        self.connect_wallet(u, master)
        sub = self.subscribe(u, sid, vid, master)
        self.signal(sid, vid, self.now - timedelta(minutes=5), 10_000)
        jobs.run_tick(db=self.exe, now=self.now, runtime=rt)
        self.assertGreater(hl.position(master, SILVER), 0)
        # the API's DELETE {"positions": "close"}; the signal still says long — closing ignores it
        self.admin.fetchall("""UPDATE subscriptions SET status = 'closing', cancel_positions = 'close'
                               WHERE id = CAST(:s AS uuid)""", {"s": sub})
        r = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=30), runtime=rt)
        self.assertEqual((r["closing_seen"], r["orders_filled"]), (1, 1), r)
        last = self.orders_of(sub)[-1]
        self.assertEqual((last["side"], last["reduce_only"]), ("sell", True))
        self.assertTrue(hl.orders_log[-1]["reduce_only"])
        self.assertEqual(hl.position(master, SILVER), 0)
        self.assertEqual(self.sub_row(sub)["status"], "closing")            # flat is confirmed on the next tick
        r = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=90), runtime=rt)
        self.assertEqual(r["closings_completed"], 1, r)
        row = self.sub_row(sub)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["cancelled_at"])
        self.assertTrue([a for a in self.alerts_of("positions_closed") if a["user_id"] == u])
        n = len(hl.orders_log)
        jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=150), runtime=rt)
        self.assertEqual(len(hl.orders_log), n)

    def test_31_closing_residual_alerts_and_keeps_closing(self) -> None:
        from app.execution import jobs

        hl, info, gw = self.hl_world()
        rt = self.runtime(hl, info, gw)
        sid, vid = self.strategy()
        u = self.user("resid")
        master = addr("resid-" + u)
        self.connect_wallet(u, master)
        sub = self.subscribe(u, sid, vid, master)
        self.signal(sid, vid, self.now - timedelta(minutes=5), 10_000)
        jobs.run_tick(db=self.exe, now=self.now, runtime=rt)
        self.admin.fetchall("UPDATE subscriptions SET status = 'closing', cancel_positions = 'close' WHERE id = CAST(:s AS uuid)",
                            {"s": sub})
        for _ in range(4):
            hl.script("partial", ratio="0.5")
        t = self.now
        for i in range(6):   # simulated time stays within the market data's 60 s freshness window
            t += timedelta(seconds=5)
            jobs.run_tick(db=self.exe, now=t, runtime=rt)
        closes = [o for o in self.orders_of(sub) if o["reduce_only"]]
        self.assertLessEqual(len(closes), 4)                          # bounded per epoch (max_attempts_per_bar)
        self.assertNotEqual(hl.position(master, SILVER), 0)
        self.assertEqual(self.sub_row(sub)["status"], "closing")
        if len({o["bar_close"] for o in closes}) == 1:                # all attempts in one retry epoch
            self.assertEqual(len(closes), 4)
            self.assertTrue([a for a in self.alerts_of("closing_residual") if a["user_id"] == u])

    def test_32_leave_is_never_touched(self) -> None:
        from app.execution import jobs

        hl, info, gw = self.hl_world()
        rt = self.runtime(hl, info, gw)
        sid, vid = self.strategy()
        u = self.user("leave")
        master = addr("leave-" + u)
        self.connect_wallet(u, master)
        sub = self.subscribe(u, sid, vid, master)
        self.signal(sid, vid, self.now - timedelta(minutes=5), 10_000)
        jobs.run_tick(db=self.exe, now=self.now, runtime=rt)
        self.admin.fetchall("""UPDATE subscriptions SET status = 'cancelled', cancel_positions = 'leave', cancelled_at = now()
                               WHERE id = CAST(:s AS uuid)""", {"s": sub})
        n = len(hl.orders_log)
        self.signal(sid, vid, self.now - timedelta(minutes=1), 0)
        jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=30), runtime=rt)
        self.assertEqual(len(hl.orders_log), n)
        self.assertGreater(hl.position(master, SILVER), 0)

    def test_35_alert_contacts_entries_gate(self) -> None:
        """SPEC §12: no confirmed email / no working Telegram (past the 24 h grace) → reduce-only for the tick:
        no entry from flat, exits still run; linking contacts lifts the gate."""
        if "0007_alerts.sql" not in self.db_.applied:
            self.skipTest("needs 0007")
        from app.execution import jobs
        from app.execution.pg import PgDatabase, PgSubscriptionRepo

        hl, info, gw = self.hl_world()
        rt = self.runtime(hl, info, gw)
        sid, vid = self.strategy()
        u = self.user("gate", contacts=False)
        master = addr("gate-" + u)
        self.connect_wallet(u, master)
        sub = self.subscribe(u, sid, vid, master)
        repo = PgSubscriptionRepo(PgDatabase(self.exe), clock=lambda: self.now)
        self.assertFalse(repo.get_subscription(sub).entries_allowed)
        self.signal(sid, vid, self.now - timedelta(minutes=5), 10_000)          # long
        r = jobs.run_tick(db=self.exe, now=self.now, runtime=rt)
        self.assertEqual((r["orders_placed"], r["contacts_gated"]), (0, 1), r)
        self.assertEqual(hl.position(master, SILVER), 0)
        # contacts linked → entries allowed on the next bar
        self.link_contacts(u)
        self.assertTrue(repo.get_subscription(sub).entries_allowed)
        self.signal(sid, vid, self.now - timedelta(minutes=4), 10_000)
        r = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=20), runtime=rt)
        self.assertEqual(r["orders_filled"], 1, r)
        self.assertGreater(hl.position(master, SILVER), 0)
        # Telegram lapsed: still allowed inside the 24 h grace, gated after it — the exit still runs
        lapsed = self.now - timedelta(hours=30)
        self.admin.fetchall("""UPDATE user_contacts SET telegram_blocked_at = :t, telegram_block_reason = 'blocked'
                               WHERE user_id = CAST(:u AS uuid)""", {"t": lapsed, "u": u})
        self.assertTrue(PgSubscriptionRepo(PgDatabase(self.exe), clock=lambda: lapsed + timedelta(hours=1))
                        .get_subscription(sub).entries_allowed)
        self.assertFalse(repo.get_subscription(sub).entries_allowed)
        self.signal(sid, vid, self.now - timedelta(minutes=3), 20_000)          # "add" → refused
        r = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=40), runtime=rt)
        self.assertEqual((r["orders_placed"], r["contacts_gated"]), (0, 1), r)
        self.signal(sid, vid, self.now - timedelta(minutes=2), 0)               # exit → runs
        r = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=50), runtime=rt)
        self.assertEqual(r["orders_filled"], 1, r)
        last = self.orders_of(sub)[-1]
        self.assertEqual((last["side"], last["reduce_only"]), ("sell", True))
        self.assertEqual(hl.position(master, SILVER), 0)

    def test_40_kill_switches(self) -> None:
        from app.execution import jobs

        hl, info, gw = self.hl_world()
        rt = self.runtime(hl, info, gw)
        sid, vid = self.strategy()
        u = self.user("kill")
        master = addr("kill-" + u)
        self.connect_wallet(u, master)
        sub = self.subscribe(u, sid, vid, master)
        self.signal(sid, vid, self.now - timedelta(minutes=5), 10_000)
        self.admin.fetchall("UPDATE system_flags SET value = 'true'::jsonb WHERE key = 'kill_switch_global'")
        r = jobs.run_tick(db=self.exe, now=self.now, runtime=rt)
        self.assertTrue(r["kill_switch_global"])
        self.assertEqual(hl.orders_log, [])
        self.admin.fetchall("UPDATE system_flags SET value = 'false'::jsonb WHERE key = 'kill_switch_global'")
        self.admin.fetchall("""INSERT INTO system_flags (key, value, updated_by) VALUES ('kill_switch_market:xyz:SILVER',
                               'true'::jsonb, 'test') ON CONFLICT (key) DO UPDATE SET value = 'true'::jsonb""")
        r = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=20), runtime=rt)
        self.assertGreaterEqual(r["market_killed"], 1, r)
        self.assertEqual(hl.orders_log, [])
        self.admin.fetchall("UPDATE system_flags SET value = 'false'::jsonb WHERE key = 'kill_switch_market:xyz:SILVER'")
        r = jobs.run_tick(db=self.exe, now=self.now + timedelta(seconds=40), runtime=rt)
        self.assertEqual(r["orders_filled"], 1, r)
        self.assertEqual(len(self.orders_of(sub)), 1)

    def test_50_agent_key_aad_binding(self) -> None:
        from app.execution.keys import AgentKeyMissing, DbAgentKeyProvider
        from app.execution.pg import PgDatabase
        from app.security.kms import DecryptionFailed, make_decryptor

        u1, u2 = self.user("k1"), self.user("k2")
        m1 = addr("k1-" + u1)
        agent = self.connect_wallet(u1, m1)
        dec = make_decryptor(self.settings)
        kp = DbAgentKeyProvider(PgDatabase(self.exe), decryptor_factory=lambda: dec)
        with kp.agent_key(u1, m1) as priv:
            from app.security.agent_keys import address_from_private_key

            self.assertEqual(address_from_private_key(priv), agent)
            held = priv
        self.assertEqual(bytes(held), b"\x00" * 32)                     # zeroized on exit
        with self.assertRaises(AgentKeyMissing):
            with kp.agent_key(u2, m1):
                pass
        # u1's sealed key re-labelled as u2's: the AAD (user binding) refuses it
        ct = self.admin.fetchall("SELECT key_ciphertext, kms_key_version FROM agent_keys WHERE agent_address = :a",
                                 {"a": agent})[0]
        m2 = addr("k2-" + u2)
        self.admin.fetchall("""INSERT INTO agent_keys (user_id, master_address, agent_address, key_ciphertext,
                                                       kms_key_version, status, approved_at)
                               VALUES (CAST(:u AS uuid), :m, :a, decode(:k, 'hex'), :v, 'active', now())""",
                            {"u": u2, "m": m2, "a": addr("fake-agent-" + u2), "k": ct["key_ciphertext"][2:],
                             "v": ct["kms_key_version"]})
        with self.assertRaises(DecryptionFailed):
            with kp.agent_key(u2, m2):
                pass

    def test_60_locks_and_unit_of_work(self) -> None:
        from app.execution.pg import PgDatabase, PgLedger, PgLockProvider

        pdb = PgDatabase(self.exe)
        locks = PgLockProvider(pdb)
        with locks.try_lock("unit:x") as a:
            self.assertTrue(a)
            with locks.try_lock("unit:x") as b:
                self.assertFalse(b)
            with locks.try_lock("unit:y") as c:
                self.assertTrue(c)
        with locks.try_lock("unit:x") as d:
            self.assertTrue(d)
        # UnitOfWork: a ledger post and a business write roll back together
        u = self.user("uow")
        led = PgLedger(pdb)
        key = f"test-uow:{u}"
        with self.assertRaises(RuntimeError):
            with pdb.atomic():
                led.post_transaction(idempotency_key=key, kind="adjustment", memo="x",
                                     lines=[("treasury:hl_usdc", 5), (f"user:{u}:fee_balance", -5)], created_by="t")
                pdb.all("UPDATE users SET referral_tier = 'elite' WHERE id = CAST(:u AS uuid)", u=u)
                raise RuntimeError("boom")
        self.assertFalse(led.has_transaction(key))
        self.assertEqual(self.admin.fetchall("SELECT referral_tier::text AS t FROM users WHERE id = CAST(:u AS uuid)",
                                             {"u": u})[0]["t"], "starter")
        with pdb.atomic():
            led.post_transaction(idempotency_key=key, kind="adjustment", memo="x",
                                 lines=[("treasury:hl_usdc", 5), (f"user:{u}:fee_balance", -5)], created_by="t")
        self.assertTrue(led.has_transaction(key))
        self.ledger_ok()

    def test_65_settlement_low_balance_thresholds_fire_once(self) -> None:
        """Renewal posted by settlement (outside ledger_ops) → on_balance_changed in the same transaction: the 50 %
        threshold of the monthly need fires exactly once, re-runs add nothing; a plan renewal crossing 20 % and 0 %
        fires those once each; subscription_renewed is emitted once."""
        if "0007_alerts.sql" not in self.db_.applied:
            self.skipTest("needs 0006/0007")
        from app.execution import jobs

        hl, info, gw = self.hl_world()
        rt = self.runtime(hl, info, gw)
        sid, vid = self.strategy()
        self.admin.fetchall("UPDATE strategies SET price_monthly_micro = 10000000 WHERE id = CAST(:s AS uuid)", {"s": sid})
        u = self.user("lowbal")
        sub = self.subscribe(u, sid, vid, addr("lowbal-" + u))
        past = self.now - timedelta(days=1)
        self.admin.fetchall("UPDATE subscriptions SET current_period_end = :t WHERE id = CAST(:s AS uuid)",
                            {"t": past, "s": sub})
        self.topup(u, 14_000_000)                                   # $14 = 140 % of the $10 need

        def lows() -> list[tuple[str, int]]:
            return [(r["kind"], int(r["payload"]["threshold_bps"])) for r in self.admin.fetchall(
                """SELECT kind, payload FROM alerts WHERE user_id = CAST(:u AS uuid)
                    AND kind IN ('balance_low', 'balance_empty') ORDER BY created_at, id""", {"u": u})]

        settle_now = self.now + timedelta(minutes=1)
        rep = jobs.settle_daily(db=self.exe, now=settle_now, runtime=rt)
        self.assertEqual(rep["errors"], [], rep)
        self.assertEqual(rep["renewals_charged"], 1, rep)
        self.assertEqual(lows(), [("balance_low", 5000)])           # $14 → $4 = 40 %: crossed 50 % once
        self.assertEqual(self.admin.fetchall("""SELECT count(*) AS n FROM events_outbox WHERE kind = 'subscription_renewed'
                                                AND user_id = CAST(:u AS uuid)""", {"u": u})[0]["n"], 1)
        jobs.settle_daily(db=self.exe, now=settle_now + timedelta(minutes=2), runtime=rt)
        self.assertEqual(lows(), [("balance_low", 5000)])           # idempotent: nothing new
        # plan renewal ($20 pro): need becomes $30; balance must cover the price → top up $16 → $20, renew → $0
        self.topup(u, 16_000_000)
        self.admin.fetchall("""UPDATE users SET plan = 'pro', plan_started_at = :t, plan_period_end = :t
                               WHERE id = CAST(:u AS uuid)""", {"t": past, "u": u})
        rep = jobs.settle_daily(db=self.exe, now=settle_now + timedelta(minutes=4), runtime=rt)
        self.assertEqual(rep["plans_renewed"], 1, rep)
        # $20 → $0 with a $30 need (66 % → 0 %): 50 %, 20 % and 0 % each fire once (the top-up re-armed 50 %)
        self.assertEqual(sorted(lows()), sorted([("balance_low", 5000), ("balance_low", 5000), ("balance_low", 2000),
                                                 ("balance_empty", 0)]))   # same created_at: order by value
        jobs.settle_daily(db=self.exe, now=settle_now + timedelta(minutes=6), runtime=rt)
        self.assertEqual(len(lows()), 4)

    def test_70_referral_tiers(self) -> None:
        from app.config import Economics, ReferralTier
        from app.execution import jobs

        hl, info, gw = self.hl_world()
        econ = replace(Economics(), referral_tiers=(ReferralTier("starter", 0, 0, 5000),
                                                    ReferralTier("partner", 2, 10**15, 7500),
                                                    ReferralTier("elite", 100, 10**16, 10000)))
        rt = self.runtime(hl, info, gw, economics=econ)
        sid, vid = self.strategy()
        ref = self.user("tierref")
        for i in range(2):
            u = self.user(f"tier{i}", referred_by=ref)
            self.subscribe(u, sid, vid, addr(f"tier{i}-{u}"))
        out = jobs.referral_tiers(db=self.exe, now=self.now, runtime=rt)
        self.assertIn({"user_id": ref, "from": "starter", "to": "partner", "active_users": 2, "notional_micro": 0},
                      out["changes"])
        from app.execution.pg import PgDatabase, PgReferralLookup

        self.assertEqual(PgReferralLookup(PgDatabase(self.exe), econ).referrer_share(
            self.admin.fetchall("SELECT id::text AS id FROM users WHERE referred_by = CAST(:r AS uuid) LIMIT 1",
                                {"r": ref})[0]["id"]), (ref, 7500))
        again = jobs.referral_tiers(db=self.exe, now=self.now, runtime=rt)
        self.assertNotIn(ref, [c["user_id"] for c in again["changes"]])

    def test_80_creator_signals_via_sandbox_http(self) -> None:
        import hashlib

        from app.execution import jobs
        from app.execution.jobs import SandboxClient
        from app.sandbox.service import make_server

        hl, info, gw = self.hl_world()
        cinfo = SyntheticCandleInfo(info)
        srv = make_server("127.0.0.1", 0, SANDBOX_SECRET, max_concurrent=2)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
            sandbox = SandboxClient(url, SANDBOX_SECRET, require_id_token=False, token_provider=lambda aud: None,
                                    opener=opener)
            rt = self.runtime(hl, cinfo, gw, sandbox=sandbox)
            creator = self.user("creator")
            self.admin.fetchall("UPDATE users SET role = 'creator' WHERE id = CAST(:u AS uuid)", {"u": creator})
            slug = f"c-{uuid.uuid4().hex[:10]}"
            sid = self.admin.fetchall("""INSERT INTO strategies (slug, name, owner_user_id, in_house, markets, timeframe,
                                                                 price_monthly_micro, profit_share_bps, status)
                                         VALUES (:s, :s, CAST(:o AS uuid), false, ARRAY['xyz:SILVER'], '1d', 0, 500,
                                                 'listed') RETURNING id::text AS id""", {"s": slug, "o": creator})[0]["id"]
            raw = CREATOR_CODE.encode()
            code_hash = hashlib.sha256(raw).hexdigest()
            blob = self.enc.seal(raw, f"strategy_code:{sid}:{code_hash}".encode())   # exactly as the API seals it
            vid = self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash, code_ciphertext,
                                                                        params, markets, timeframe, lookback,
                                                                        max_leverage, published_at, live_since)
                                         VALUES (CAST(:s AS uuid), 1, :h, :ct, '{}'::jsonb, ARRAY['xyz:SILVER'], '1d',
                                                 50, 2, now(), now()) RETURNING id::text AS id""",
                                      {"s": sid, "h": code_hash, "ct": blob.blob})[0]["id"]
            day = 86_400_000
            now_ms = int(self.now.timestamp() * 1000)
            bar_close = datetime.fromtimestamp((now_ms - now_ms % day) / 1000, UTC)
            just_closed = bar_close + timedelta(seconds=5)
            early = jobs.run_creator_signals(db=self.exe, now=just_closed, runtime=rt)
            self.assertGreaterEqual(early["not_closed"], 1)                     # waits bar_settle_seconds
            later = bar_close + timedelta(minutes=2)
            rep = jobs.run_creator_signals(db=self.exe, now=later, runtime=rt)
            self.assertEqual((rep["stored"], rep["errors"]), (1, 0), rep)
            rows = self.admin.fetchall("""SELECT coin, target_weight_bps, source::text AS source, raw FROM signals
                                           WHERE strategy_version_id = CAST(:v AS uuid)""", {"v": vid})
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["coin"], rows[0]["source"]), (SILVER, "sandbox"))
            self.assertIn(rows[0]["target_weight_bps"], (15_000, 2_500))
            self.assertEqual(rows[0]["raw"]["code_hash"], code_hash)
            self.assertEqual(rows[0]["raw"]["bars_last_t"][SILVER], int(bar_close.timestamp() * 1000) - day)
            again = jobs.run_creator_signals(db=self.exe, now=later + timedelta(minutes=1), runtime=rt)
            self.assertEqual((again["stored"], again["already"]), (0, 1))
            # tampered binding (code sealed for another strategy id) → refused, alert, nothing stored
            sid2 = self.admin.fetchall("""INSERT INTO strategies (slug, name, owner_user_id, in_house, markets, timeframe,
                                                                  price_monthly_micro, profit_share_bps, status)
                                          VALUES (:s, :s, CAST(:o AS uuid), false, ARRAY['xyz:SILVER'], '1d', 0, 0,
                                                  'listed') RETURNING id::text AS id""",
                                       {"s": slug + "-x", "o": creator})[0]["id"]
            self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash, code_ciphertext, markets,
                                                                  timeframe, lookback, max_leverage, published_at)
                                   VALUES (CAST(:s AS uuid), 1, :h, :ct, ARRAY['xyz:SILVER'], '1d', 50, 2, now())""",
                                {"s": sid2, "h": code_hash, "ct": blob.blob})
            bad = jobs.run_creator_signals(db=self.exe, now=later + timedelta(minutes=2), runtime=rt)
            self.assertEqual(bad["errors"], 1, bad)
            self.assertTrue(self.alerts_of("creator_signal_failed"))
            self.admin.fetchall("UPDATE strategies SET status = 'paused' WHERE id IN (CAST(:a AS uuid), CAST(:b AS uuid))",
                                {"a": sid, "b": sid2})
            self.admin.fetchall("UPDATE strategies SET status = 'delisted' WHERE id IN (CAST(:a AS uuid), CAST(:b AS uuid))",
                                {"a": sid, "b": sid2})
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()

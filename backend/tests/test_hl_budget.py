"""Shared Hyperliquid rate budget (app/hl/budget.py, config.HlLimits, jobs_data WeightPacer, InfoClient hook).

Unit part (no DB): request weights, the InfoClient hook (charged before EVERY attempt, per-item extra after), pool
selection, pacer back-off against a fake shared budget. DB part (AIJALON_TEST_DATABASE_URL, migrated through 0009,
run AS app_executor): the atomic counter — jobs ceiling, tick reserve/priority, forced tick overrun, window roll-over,
ring reset, two independent egress keys, fail-open without the table — and a real data job (fills_ingest) backing off
and stopping cleanly when the shared budget is spent.
"""
from __future__ import annotations

import json
import sys
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import HlLimits  # noqa: E402
from app.hl.budget import (  # noqa: E402
    POOL_JOBS,
    POOL_TICK,
    BudgetHook,
    Charge,
    HlBudgetExhausted,
    HlRateBudget,
    budget_pool,
    current_pool,
)
from app.hl.info import InfoClient  # noqa: E402
from app.jobs_data.hl import WeightPacer, make_pacer  # noqa: E402

UTC = timezone.utc


class FakeBudget:
    """In-memory twin of HlRateBudget's contract (same ceiling rules, controllable clock)."""

    def __init__(self, limits: HlLimits, now: datetime) -> None:
        self.limits = limits
        self.now = now
        self.windows: dict[datetime, list[int]] = {}
        self.calls: list[tuple[int, str, bool]] = []

    def window(self) -> datetime:
        return self.now.replace(second=0, microsecond=0)

    def seconds_to_next_window(self) -> float:
        return (self.window() + timedelta(minutes=1) - self.now).total_seconds()

    def try_acquire(self, weight: int, pool: str = POOL_JOBS, *, force: bool = False) -> Charge:
        self.calls.append((weight, pool, force))
        tick, jobs = self.windows.setdefault(self.window(), [0, 0])
        total, reserve = self.limits.budget_weight_per_minute, self.limits.tick_reserve_per_minute
        ok = force or (tick + jobs + weight <= total if pool == POOL_TICK
                       else jobs + weight <= total - max(tick, reserve))
        if ok:
            self.windows[self.window()][0 if pool == POOL_TICK else 1] += weight
        return Charge(granted=ok)

    acquire_wait = HlRateBudget.acquire_wait


class Resp:
    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self._raw = json.dumps(body).encode()
        self.headers: dict = {}

    def iter_content(self, chunk_size: int = 65536):
        yield self._raw

    def close(self) -> None:
        pass


class Session:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.posts = 0

    def post(self, url, json=None, **_kw):
        self.posts += 1
        return self.responses.pop(0)


class Hook:
    def __init__(self) -> None:
        self.before_calls: list[str] = []
        self.after_calls: list[tuple[str, int]] = []

    def before(self, body) -> None:
        self.before_calls.append(body["type"])

    def after(self, body, out) -> None:
        self.after_calls.append((body["type"], len(out) if isinstance(out, list) else -1))


class WeightsTest(unittest.TestCase):
    def test_weights_configurable_and_documented_defaults(self):
        lim = HlLimits()
        self.assertEqual((lim.weight("l2Book"), lim.weight("clearinghouseState"), lim.weight("orderStatus")), (2, 2, 2))
        self.assertEqual((lim.weight("userRole"), lim.weight("meta"), lim.weight("extraAgents"), lim.weight(None)),
                         (60, 20, 20, 20))
        self.assertEqual(lim.extra_weight("candleSnapshot", 5000), 84)
        self.assertEqual(lim.extra_weight("userFillsByTime", 2000), 100)
        self.assertEqual(lim.extra_weight("userFillsByTime", 1), 1)
        self.assertEqual(lim.extra_weight("meta", 999), 0)
        self.assertEqual(lim.jobs_ceiling, 500)
        custom = HlLimits(type_weights=(("userRole", 40), ("meta", 5)), light_weight=1)
        self.assertEqual((custom.weight("userRole"), custom.weight("meta"), custom.weight("l2Book")), (40, 5, 1))
        with self.assertRaises(ValueError):
            HlLimits(budget_weight_per_minute=300, tick_reserve_per_minute=300)

    def test_settings_carry_limits(self):
        from app.config import get_settings

        self.assertEqual(get_settings().hl_limits.budget_weight_per_minute, 800)


class InfoClientHookTest(unittest.TestCase):
    def test_before_every_attempt_after_once_with_item_count(self):
        hook = Hook()
        sess = Session([Resp(429, {}), Resp(200, [{"t": i} for i in range(7)])])
        c = InfoClient(session=sess, sleep=lambda s: None, rng=lambda: 0.0, rate_hook=hook)
        out = c.user_fills_by_time("0x" + "11" * 20, 1_700_000_000_000)
        self.assertEqual(len(out), 7)
        self.assertEqual(hook.before_calls, ["userFillsByTime", "userFillsByTime"])   # the retry cost weight too
        self.assertEqual(hook.after_calls, [("userFillsByTime", 7)])

    def test_budget_refusal_is_not_retried_or_sent(self):
        class Refuse(Hook):
            def before(self, body):
                raise HlBudgetExhausted("no budget")

        sess = Session([Resp(200, {})])
        c = InfoClient(session=sess, sleep=lambda s: None, rate_hook=Refuse())
        with self.assertRaises(HlBudgetExhausted):
            c.meta()
        self.assertEqual(sess.posts, 0)

    def test_budget_refusal_is_503_service_unavailable(self):
        from app.errors import ExternalServiceError

        e = HlBudgetExhausted("hyperliquid rate budget exhausted")
        self.assertEqual((e.http_status, e.code), (503, "service_unavailable"))   # API_CONTRACT: busy, retry later
        self.assertIsInstance(e, ExternalServiceError)


class BudgetHookTest(unittest.TestCase):
    def setUp(self):
        self.lim = HlLimits(budget_weight_per_minute=100, tick_reserve_per_minute=40)
        self.clock = [datetime(2026, 9, 30, 12, 0, 10, tzinfo=UTC)]
        self.b = FakeBudget(self.lim, self.clock[0])
        self.slept: list[float] = []
        self.mono = [0.0]

        def sleep(s):
            self.slept.append(s)
            self.mono[0] += s
            self.b.now = self.b.now + timedelta(seconds=s)
        self.sleep = sleep

    def hook(self, **kw):
        return BudgetHook(self.b, self.lim, monotonic=lambda: self.mono[0], sleep=self.sleep, **kw)

    def test_tick_is_forced_never_blocked(self):
        h = self.hook()
        for _ in range(10):
            h.before({"type": "meta"})                     # 10 × 20 = 200 > budget 100: still never refused
        self.assertTrue(all(force for _w, pool, force in self.b.calls if pool == POOL_TICK))
        self.assertEqual(self.slept, [])

    def test_jobs_pool_waits_for_next_window_then_raises_after_max_wait(self):
        h = self.hook(max_wait_seconds=65)
        with budget_pool(POOL_JOBS):
            self.assertEqual(current_pool(), POOL_JOBS)
            for _ in range(3):
                h.before({"type": "meta"})                 # 60 of the 60 jobs may use (100 − reserve 40)
            h.before({"type": "meta"})                     # waits into the next minute, then granted
            self.assertEqual(len(self.slept), 1)
            self.assertGreaterEqual(self.slept[0], 50)
            h2 = self.hook(max_wait_seconds=5)
            for _ in range(2):
                h2.before({"type": "meta"})
            with self.assertRaises(HlBudgetExhausted):
                h2.before({"type": "meta"})                # would need > 5 s: refused
        self.assertEqual(current_pool(), POOL_TICK)

    def test_after_charges_extra_to_current_pool(self):
        h = self.hook()
        with budget_pool(POOL_JOBS):
            h.after({"type": "candleSnapshot"}, [{}] * 121)
        self.assertEqual(self.b.calls[-1], (3, POOL_JOBS, True))
        h.after({"type": "meta"}, {"universe": []})
        self.assertEqual(len(self.b.calls), 1)

    def test_no_budget_bound_yet_is_a_noop(self):
        h = BudgetHook(lambda: None, self.lim)
        h.before({"type": "meta"})
        h.after({"type": "userFills"}, [1, 2, 3])


class PacerSharedTest(unittest.TestCase):
    def test_can_start_prepays_spend_consumes_and_stops_at_deadline(self):
        lim = HlLimits(budget_weight_per_minute=100, tick_reserve_per_minute=40)
        b = FakeBudget(lim, datetime(2026, 9, 30, 12, 0, 50, tzinfo=UTC))
        mono = [0.0]

        def sleep(s):
            mono[0] += s
            b.now += timedelta(seconds=s)
        p = WeightPacer(6000, max_seconds=30, clock=lambda: mono[0], sleep=sleep, shared=b)
        self.assertTrue(p.can_start(20))
        p.spend(20)
        self.assertTrue(p.can_start(40))
        p.spend(40)                                          # 60 = the jobs ceiling this minute
        self.assertTrue(p.can_start(20))                     # waited ~10 s into the next minute
        self.assertEqual(p.budget_waits, 1)
        p.spend(20)
        p.settle(25, 20)                                     # +5 forced into the shared budget
        self.assertEqual(b.calls[-1], (5, POOL_JOBS, True))
        self.assertTrue(p.can_start(35))                     # 20 + 5 + 35 = 60
        p.spend(35)
        self.assertFalse(p.can_start(20))                    # next window is ~50 s away > 30 s deadline
        self.assertTrue(p.budget_refused)
        with self.assertRaises(HlBudgetExhausted):
            p.spend(20)

    def test_make_pacer_uses_shared_budget_only_for_real_hyperliquid(self):
        class S:
            hl_limits = HlLimits()

        class Db:
            def begin(self):
                raise AssertionError("not used")
        self.assertIsInstance(make_pacer(Db(), S(), info=None).shared, HlRateBudget)
        self.assertIsNone(make_pacer(Db(), S(), info=object()).shared)              # injected fake: no sharing
        self.assertIs(make_pacer(Db(), S(), info=object(), rate_budget="B").shared, "B")

        class Off:
            hl_limits = replace(HlLimits(), shared_budget=False)
        self.assertIsNone(make_pacer(Db(), Off(), info=None).shared)


class RuntimeWiringTest(unittest.TestCase):
    def test_default_info_client_carries_hook_bound_to_first_job_db(self):
        import types

        from app.config import get_settings
        from app.execution.jobs import Runtime
        from app.execution.pg import PgDatabase

        class Db:
            def begin(self):
                raise AssertionError("not used")
        fake_requests = types.ModuleType("requests")
        fake_requests.Session = lambda: object()
        saved = sys.modules.get("requests")
        sys.modules["requests"] = fake_requests
        try:
            rt = Runtime(get_settings())
            hook = rt.info.rate_hook
        finally:
            if saved is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = saved
        self.assertIsInstance(hook, BudgetHook)
        self.assertIsNone(hook.budget())                        # no DB yet → no accounting
        rt.bind_db(PgDatabase(Db()))
        self.assertIsInstance(hook.budget(), HlRateBudget)
        self.assertIs(hook.budget(), rt.rate_budget)

    def test_injected_fake_info_is_never_budgeted(self):
        from app.config import get_settings
        from app.execution.jobs import Runtime
        from app.execution.pg import PgDatabase

        class Db:
            def begin(self):
                raise AssertionError("not used")
        rt = Runtime(get_settings(), info=object())
        rt.bind_db(PgDatabase(Db()))
        self.assertIsNone(rt.rate_budget)


# ================================================================================================= DB
try:
    from test_jobs_data_db import RUN, DB_URL, RoleRunner, RunnerDb, FakeInfo, _addr, _cloid, _fill  # noqa: E402
except Exception:  # noqa: BLE001
    RUN = False


@unittest.skipUnless(RUN, "needs AIJALON_TEST_DATABASE_URL (migrated through 0009) and psql")
class HlRateBudgetDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = RoleRunner(DB_URL, "app_executor")
        cls.admin = RoleRunner(DB_URL, None)

    def budget(self, now: datetime, key: str | None = None, **kw) -> HlRateBudget:
        lim = HlLimits(budget_weight_per_minute=kw.pop("total", 100), tick_reserve_per_minute=kw.pop("reserve", 40),
                       egress_key=key or ("t" + uuid.uuid4().hex[:12]))
        self.clock = [now]
        return HlRateBudget(self.exe, lim, clock=lambda: self.clock[0])

    def test_jobs_ceiling_tick_priority_and_forced_overrun(self):
        b = self.budget(datetime(2026, 9, 30, 12, 7, 3, tzinfo=UTC))
        self.assertTrue(b.try_acquire(50, POOL_JOBS).granted)
        self.assertFalse(b.try_acquire(20, POOL_JOBS).granted)       # 70 > 100 − reserve 40
        self.assertTrue(b.try_acquire(10, POOL_JOBS).granted)        # exactly 60
        self.assertTrue(b.try_acquire(40, POOL_TICK).granted)        # the reserve is still there for the tick
        self.assertFalse(b.try_acquire(1, POOL_TICK).granted)        # 100 used
        c = b.try_acquire(30, POOL_TICK, force=True)                 # the tick is never refused in practice
        self.assertTrue(c.granted and c.over)
        self.assertEqual((c.spent_tick, c.spent_jobs), (70, 60))
        u = b.usage()
        self.assertEqual((u["spent_tick"], u["spent_jobs"], u["spent"], u["budget"]), (70, 60, 130, 100))

    def test_tick_beyond_reserve_takes_room_from_jobs(self):
        b = self.budget(datetime(2026, 9, 30, 12, 8, 3, tzinfo=UTC))
        self.assertTrue(b.try_acquire(70, POOL_TICK).granted)
        self.assertFalse(b.try_acquire(31, POOL_JOBS).granted)       # 100 − max(70, 40) = 30
        self.assertTrue(b.try_acquire(30, POOL_JOBS).granted)

    def test_window_rollover_ring_reset_and_stale_clock(self):
        t0 = datetime(2026, 9, 30, 12, 9, 59, tzinfo=UTC)
        b = self.budget(t0)
        self.assertTrue(b.try_acquire(60, POOL_JOBS).granted)
        self.assertFalse(b.try_acquire(1, POOL_JOBS).granted)
        self.clock[0] = t0 + timedelta(seconds=2)                    # next minute: fresh window
        self.assertTrue(b.try_acquire(60, POOL_JOBS).granted)
        self.clock[0] = t0 + timedelta(hours=1)                      # same slot one hour later: the ring row resets
        self.assertTrue(b.try_acquire(60, POOL_JOBS).granted)
        rows = self.admin.fetchall("SELECT slot, window_start, spent_jobs FROM hl_rate_budget WHERE egress_key = :k "
                                   "ORDER BY slot", {"k": b.limits.egress_key})
        self.assertEqual([(r["slot"], r["spent_jobs"]) for r in rows], [(9, 60), (10, 60)])
        # an instance whose clock is an hour behind charges the NEWER window, never resets it backwards
        self.clock[0] = t0
        self.assertFalse(b.try_acquire(1, POOL_JOBS).granted)
        self.assertEqual(self.admin.fetchall("SELECT spent_jobs FROM hl_rate_budget WHERE egress_key = :k AND slot = 9",
                                             {"k": b.limits.egress_key})[0]["spent_jobs"], 60)

    def test_egress_keys_are_independent_and_api_role_can_read(self):
        now = datetime(2026, 9, 30, 12, 11, 0, tzinfo=UTC)
        a, b = self.budget(now), self.budget(now)
        self.assertTrue(a.try_acquire(60, POOL_JOBS).granted)
        self.assertTrue(b.try_acquire(60, POOL_JOBS).granted)
        RoleRunner(DB_URL, "app_api").fetchall("SELECT count(*) FROM hl_rate_budget")

    def test_acquire_wait_crosses_into_next_minute(self):
        t0 = datetime(2026, 9, 30, 12, 12, 58, tzinfo=UTC)
        b = self.budget(t0)
        mono = [0.0]

        def sleep(s):
            mono[0] += s
            self.clock[0] += timedelta(seconds=s)
        self.assertTrue(b.acquire_wait(60, POOL_JOBS, deadline=10, monotonic=lambda: mono[0], sleep=sleep))
        self.assertTrue(b.acquire_wait(60, POOL_JOBS, deadline=10, monotonic=lambda: mono[0], sleep=sleep))
        self.assertGreater(mono[0], 2)
        self.assertFalse(b.acquire_wait(60, POOL_JOBS, deadline=mono[0] + 5, monotonic=lambda: mono[0], sleep=sleep))

    def test_fail_open_when_table_unreachable(self):
        b = HlRateBudget(RoleRunner(DB_URL.rsplit("/", 1)[0] + "/aj_no_such_db_" + uuid.uuid4().hex[:6], None),
                         HlLimits())
        c = b.try_acquire(20, POOL_JOBS)
        self.assertTrue(c.granted and c.fail_open)

    def test_fills_ingest_backs_off_and_stops_when_budget_spent(self):
        """A real data job with the shared budget injected: the first address fits, the rest do not fit before the
        job's deadline → it stops (remaining > 0), no error, cursors untouched for the rest (resumed next call)."""
        from app.jobs_data.fills import fills_ingest

        tag = uuid.uuid4().hex[:8]
        seed = int(tag, 16)
        now = datetime(2026, 9, 30, 12, 13, 1, tzinfo=UTC)
        now_ms = int(now.timestamp() * 1000)
        admin = self.admin
        sid = admin.fetchall("SELECT id::text AS id FROM strategies WHERE slug = 'silver'")[0]["id"]
        vid = admin.fetchall("SELECT id::text AS id FROM strategy_versions WHERE strategy_id = CAST(:s AS uuid) "
                             "ORDER BY version DESC LIMIT 1", {"s": sid})[0]["id"]
        info = FakeInfo(tag)
        addrs = []
        for i in range(3):
            uid = admin.fetchall("""INSERT INTO users (firebase_uid, referral_code) VALUES (:f, :r)
                                    RETURNING id::text AS id""", {"f": f"hb{tag}{i}", "r": f"H{tag}{i}"})[0]["id"]
            a = _addr(seed * 10 + i)
            addrs.append(a)
            admin.fetchall("""INSERT INTO subscriptions (user_id, strategy_id, strategy_version_id, trading_address,
                                                         master_address, allocation_micro, max_leverage_x100, status,
                                                         created_at)
                              VALUES (CAST(:u AS uuid), CAST(:s AS uuid), CAST(:v AS uuid), :a, :a, 1000000000, 100,
                                      'active', CAST(:c AS timestamptz)) RETURNING id""",
                           {"u": uid, "s": sid, "v": vid, "a": a, "c": now - timedelta(hours=2)})
            info.fills[a] = [_fill("xyz:SILVER", "B", "30", "1", now_ms - 60_000, seed * 100 + i, start="0",
                                   cloid=_cloid(seed * 100 + i))]
        # other traffic already used most of this minute's jobs pool on this egress key
        b = self.budget(now, total=400, reserve=100)                  # jobs ceiling 300; a fills page estimate = 120
        self.assertTrue(b.try_acquire(55, POOL_JOBS).granted)
        mono = [0.0]

        def sleep(s):
            mono[0] += s
        pacer = WeightPacer(6000, max_seconds=20, clock=lambda: mono[0], sleep=sleep, shared=b)
        try:
            rep = fills_ingest(RunnerDb(self.exe), now, info=info, pacer=pacer)
        finally:
            admin.fetchall("""UPDATE subscriptions SET status = 'cancelled', cancel_positions = 'leave',
                              cancelled_at = now() - interval '30 days', status_changed_at = now() - interval '30 days'
                              WHERE trading_address IN (SELECT jsonb_array_elements_text(CAST(:a AS jsonb)))""",
                           {"a": json.dumps(addrs)})
        self.assertEqual(rep["errors"], [])
        self.assertGreaterEqual(rep["processed"], 1)
        self.assertGreater(rep["remaining"], 0)                       # stopped cleanly, resumes next call
        self.assertTrue(pacer.budget_refused)
        u = b.usage()
        self.assertLessEqual(u["spent_jobs"], 300)                    # never above budget − reserve


if __name__ == "__main__":
    unittest.main()

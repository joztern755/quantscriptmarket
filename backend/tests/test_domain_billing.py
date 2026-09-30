"""Tests for app.domain.billing (fee-balance state machine, renewals, low-balance thresholds)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import billing as b  # noqa: E402
from app.money import usd  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class NextStatusTest(unittest.TestCase):
    def test_active_sufficient(self):
        d = b.next_status("active", usd(100), usd(20), None, NOW)
        self.assertEqual((d.status, d.changed, d.past_due_since), ("active", False, None))

    def test_exact_balance_is_enough(self):
        self.assertEqual(b.next_status("active", usd(20), usd(20), None, NOW).status, "active")

    def test_active_insufficient_goes_past_due(self):
        d = b.next_status("active", usd(19), usd(20), None, NOW)
        self.assertEqual((d.status, d.past_due_since, d.changed, d.reason), ("past_due", NOW, True, "insufficient_balance"))

    def test_negative_balance_is_insufficient_even_with_nothing_due(self):
        self.assertEqual(b.next_status("active", -1, 0, None, NOW).status, "past_due")

    def test_past_due_within_grace(self):
        since = NOW - timedelta(hours=71, minutes=59)
        d = b.next_status("past_due", 0, usd(20), since, NOW)
        self.assertEqual((d.status, d.past_due_since, d.changed), ("past_due", since, False))

    def test_past_due_grace_expired_exactly(self):
        since = NOW - timedelta(hours=72)
        d = b.next_status("past_due", 0, usd(20), since, NOW, grace_hours=72)
        self.assertEqual((d.status, d.past_due_since, d.reason), ("reduce_only", since, "grace_expired"))

    def test_past_due_missing_timestamp_fails_closed(self):
        d = b.next_status("past_due", 0, usd(20), None, NOW)
        self.assertEqual((d.status, d.reason), ("reduce_only", "past_due_since_missing"))

    def test_recovery_from_past_due_and_reduce_only(self):
        for cur in ("past_due", "reduce_only"):
            d = b.next_status(cur, usd(50), usd(20), NOW - timedelta(days=5), NOW)
            self.assertEqual((d.status, d.past_due_since, d.changed, d.reason), ("active", None, True, "paid"))

    def test_reduce_only_stays(self):
        since = NOW - timedelta(days=5)
        d = b.next_status("reduce_only", 0, usd(20), since, NOW)
        self.assertEqual((d.status, d.changed), ("reduce_only", False))

    def test_user_states_untouched(self):
        for cur in ("paused_user", "cancelled", "pending"):
            for bal in (0, usd(100)):
                d = b.next_status(cur, bal, usd(20), None, NOW)
                self.assertEqual((d.status, d.changed), (cur, False))

    def test_custom_grace(self):
        since = NOW - timedelta(hours=2)
        self.assertEqual(b.next_status("past_due", 0, 1, since, NOW, grace_hours=1).status, "reduce_only")
        self.assertEqual(b.next_status("past_due", 0, 1, since, NOW, grace_hours=3).status, "past_due")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            b.next_status("frozen", 0, 0, None, NOW)
        with self.assertRaises(ValueError):
            b.next_status("active", 0, -1, None, NOW)
        with self.assertRaises(ValueError):
            b.next_status("active", 0, 0, None, datetime(2026, 1, 1))
        with self.assertRaises(TypeError):
            b.next_status("active", 1.0, 0, None, NOW)  # type: ignore[arg-type]

    def test_full_cycle(self):
        st, since = "active", None
        t = NOW
        d = b.next_status(st, 0, usd(20), since, t)          # renewal fails
        st, since = d.status, d.past_due_since
        self.assertEqual(st, "past_due")
        t += timedelta(hours=73)
        d = b.next_status(st, 0, usd(20), since, t)
        st, since = d.status, d.past_due_since
        self.assertEqual(st, "reduce_only")
        d = b.next_status(st, usd(20), usd(20), since, t)   # top-up
        self.assertEqual((d.status, d.past_due_since), ("active", None))


class EntriesTest(unittest.TestCase):
    def test_entries(self):
        self.assertTrue(b.entries_allowed("active", None, NOW))
        self.assertTrue(b.entries_allowed("past_due", NOW - timedelta(hours=10), NOW))
        self.assertFalse(b.entries_allowed("past_due", NOW - timedelta(hours=72), NOW))
        self.assertFalse(b.entries_allowed("past_due", None, NOW))
        for s in ("reduce_only", "paused_user", "cancelled", "pending", "bogus"):
            self.assertFalse(b.entries_allowed(s, None, NOW))

    def test_exits(self):
        for s in ("active", "past_due", "reduce_only"):
            self.assertTrue(b.exits_allowed(s))
        for s in ("paused_user", "cancelled", "pending", "bogus"):
            self.assertFalse(b.exits_allowed(s))


class RenewalTest(unittest.TestCase):
    def test_month_end_clamping(self):
        a = datetime(2026, 1, 31, 10, 30, tzinfo=UTC)
        self.assertEqual(b.add_months(a, 1), datetime(2026, 2, 28, 10, 30, tzinfo=UTC))
        self.assertEqual(b.add_months(datetime(2028, 1, 31, tzinfo=UTC), 1), datetime(2028, 2, 29, tzinfo=UTC))
        self.assertEqual(b.add_months(datetime(2026, 3, 31, tzinfo=UTC), 1), datetime(2026, 4, 30, tzinfo=UTC))
        self.assertEqual(b.add_months(datetime(2026, 12, 15, tzinfo=UTC), 1), datetime(2027, 1, 15, tzinfo=UTC))
        self.assertEqual(b.add_months(datetime(2026, 3, 31, tzinfo=UTC), -1), datetime(2026, 2, 28, tzinfo=UTC))
        self.assertEqual(b.add_months(datetime(2028, 2, 29, tzinfo=UTC), 12), datetime(2029, 2, 28, tzinfo=UTC))

    def test_schedule_does_not_drift(self):
        a = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)
        got = [d.date().isoformat() for d in b.renewal_schedule(a, 5)]
        self.assertEqual(got, ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30"])
        self.assertEqual(b.renewal_at(a, 0), a)
        with self.assertRaises(ValueError):
            b.renewal_at(a, -1)

    def test_next_renewal_after(self):
        a = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)
        self.assertEqual(b.next_renewal_after(a, datetime(2026, 2, 10, tzinfo=UTC)), datetime(2026, 2, 28, 10, tzinfo=UTC))
        self.assertEqual(b.next_renewal_after(a, datetime(2026, 2, 28, 10, tzinfo=UTC)), datetime(2026, 3, 31, 10, tzinfo=UTC))
        self.assertEqual(b.next_renewal_after(a, datetime(2026, 2, 28, 9, 59, tzinfo=UTC)), datetime(2026, 2, 28, 10, tzinfo=UTC))
        self.assertEqual(b.next_renewal_after(a, a), datetime(2026, 2, 28, 10, tzinfo=UTC))
        self.assertEqual(b.next_renewal_after(a, a - timedelta(days=3)), datetime(2026, 2, 28, 10, tzinfo=UTC))
        self.assertEqual(b.next_renewal_after(a, datetime(2029, 7, 31, 11, tzinfo=UTC)), datetime(2029, 8, 31, 10, tzinfo=UTC))
        # exhaustive: result is strictly after now and the previous anniversary is not
        start = datetime(2026, 1, 29, 0, 0, tzinfo=UTC)
        for day in range(0, 800, 7):
            now = start + timedelta(days=day, hours=5)
            nxt = b.next_renewal_after(start, now)
            self.assertGreater(nxt, now)

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            b.renewal_at(datetime(2026, 1, 1), 1)


class LowBalanceTest(unittest.TestCase):
    NEED = usd(100)

    def x(self, prev, new):
        return b.crossed_low_balance_thresholds(prev, new, self.NEED)

    def test_crossings(self):
        self.assertEqual(self.x(usd(60), usd(50)), (5000,))        # inclusive at level
        self.assertEqual(self.x(usd(60), usd(10)), (5000, 2000))
        self.assertEqual(self.x(usd(60), 0), (5000, 2000, 0))
        self.assertEqual(self.x(usd(60), -5), (5000, 2000, 0))
        self.assertEqual(self.x(usd(40), usd(30)), ())
        self.assertEqual(self.x(usd(50), usd(45)), ())             # already at/below 50% before
        self.assertEqual(self.x(usd(20) + 1, usd(20)), (2000,))
        self.assertEqual(self.x(usd(10), usd(60)), ())             # increases never alert

    def test_first_observation(self):
        self.assertEqual(self.x(None, usd(15)), (5000, 2000))
        self.assertEqual(self.x(None, usd(80)), ())

    def test_rearm_after_topup(self):
        self.assertEqual(self.x(usd(10), usd(60)), ())
        self.assertEqual(self.x(usd(60), usd(45)), (5000,))

    def test_no_need(self):
        self.assertEqual(b.crossed_low_balance_thresholds(usd(10), 0, 0), ())

    def test_estimate(self):
        self.assertEqual(b.estimate_monthly_need([usd(20), usd(30)], usd(20), usd(5)), usd(75))
        self.assertEqual(b.estimate_monthly_need(), 0)
        with self.assertRaises(ValueError):
            b.estimate_monthly_need([-1])


if __name__ == "__main__":
    unittest.main()

"""Tests for app.domain.track_record."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.track_record import AllocationSpan, PnlEvent, compute_track_record, public_stats  # noqa: E402
from app.money import usd  # noqa: E402

UTC = timezone.utc
LIVE = datetime(2026, 1, 1, tzinfo=UTC)
NOW = LIVE + timedelta(days=100)


def spans(n, alloc=usd(10_000), start=LIVE, end=None, same_user=False):
    return [AllocationSpan(f"s{i}", "u0" if same_user else f"u{i}", alloc, start, end) for i in range(n)]


class TrackRecordTest(unittest.TestCase):
    def test_basic_roi(self):
        ev = [PnlEvent(f"s{i}", LIVE + timedelta(days=10), usd(1_000)) for i in range(5)]
        tr = compute_track_record(LIVE, ev, spans(5), NOW)
        self.assertEqual(tr.total_pnl_micro, usd(5_000))
        self.assertEqual(tr.time_weighted_capital_micro, usd(50_000))
        self.assertEqual(tr.roi_bps, 1000)
        self.assertEqual((tr.subscriber_count, tr.active_subscribers, tr.subscription_count), (5, 5, 5))
        self.assertEqual(tr.profitable_subscriptions, 5)
        self.assertEqual(tr.live_days, 100)
        self.assertFalse(tr.not_live_proven)
        ps = public_stats(tr)
        self.assertIsNotNone(ps)
        self.assertEqual(ps.roi_bps, 1000)

    def test_version_reset_excludes_old_and_future_events(self):
        ev = [
            PnlEvent("s0", LIVE - timedelta(seconds=1), usd(999)),   # previous version
            PnlEvent("s0", LIVE, usd(10)),                            # boundary counts
            PnlEvent("s0", NOW + timedelta(seconds=1), usd(999)),    # future
        ]
        tr = compute_track_record(LIVE, ev, spans(5), NOW)
        self.assertEqual(tr.total_pnl_micro, usd(10))

    def test_k_anonymity(self):
        tr = compute_track_record(LIVE, [], spans(4), NOW)
        self.assertEqual(tr.subscriber_count, 4)
        self.assertIsNone(public_stats(tr))
        self.assertIsNone(public_stats(tr, min_subscribers=5))
        self.assertIsNotNone(public_stats(tr, min_subscribers=4))

    def test_distinct_users_counted_once(self):
        tr = compute_track_record(LIVE, [], spans(6, same_user=True), NOW)
        self.assertEqual(tr.subscriber_count, 1)
        self.assertEqual(tr.subscription_count, 6)
        self.assertIsNone(public_stats(tr))

    def test_time_weighting(self):
        half = LIVE + timedelta(days=50)
        sp = [AllocationSpan("s0", "u0", usd(10_000), LIVE, half)]
        tr = compute_track_record(LIVE, [PnlEvent("s0", LIVE + timedelta(days=1), usd(500))], sp, NOW)
        self.assertEqual(tr.time_weighted_capital_micro, usd(5_000))
        self.assertEqual(tr.roi_bps, 1000)
        self.assertEqual(tr.active_subscribers, 0)
        self.assertEqual(tr.subscriber_count, 1)

    def test_span_starting_before_live_is_clipped(self):
        sp = [AllocationSpan("s0", "u0", usd(10_000), LIVE - timedelta(days=100), None)]
        tr = compute_track_record(LIVE, [], sp, NOW)
        self.assertEqual(tr.time_weighted_capital_micro, usd(10_000))

    def test_negative_roi_truncates_toward_zero(self):
        sp = [AllocationSpan("s0", "u0", 3, LIVE, None)]
        tr = compute_track_record(LIVE, [PnlEvent("s0", LIVE, -1)], sp, NOW)
        self.assertEqual(tr.roi_bps, -3333)
        self.assertEqual(tr.profitable_subscriptions, 0)

    def test_not_live(self):
        tr = compute_track_record(None, [PnlEvent("s0", NOW, 5)], spans(10), NOW)
        self.assertTrue(tr.not_live_proven)
        self.assertIsNone(tr.roi_bps)
        self.assertIsNone(public_stats(tr))

    def test_live_proven_boundary(self):
        self.assertTrue(compute_track_record(LIVE, [], [], LIVE + timedelta(days=89, hours=23)).not_live_proven)
        self.assertFalse(compute_track_record(LIVE, [], [], LIVE + timedelta(days=90)).not_live_proven)

    def test_zero_window(self):
        tr = compute_track_record(LIVE, [], spans(5), LIVE)
        self.assertIsNone(tr.roi_bps)
        self.assertEqual(tr.time_weighted_capital_micro, 0)

    def test_period_window(self):
        p = NOW - timedelta(days=30)
        ev = [PnlEvent("s0", p - timedelta(days=1), usd(100)), PnlEvent("s0", p + timedelta(days=1), usd(30))]
        tr = compute_track_record(LIVE, ev, spans(5), NOW, period_start=p)
        self.assertEqual(tr.total_pnl_micro, usd(30))
        self.assertEqual(tr.window_start, p)
        self.assertEqual(tr.live_days, 100)
        # period start before live_since is clamped to live_since
        tr2 = compute_track_record(LIVE, ev, spans(5), NOW, period_start=LIVE - timedelta(days=500))
        self.assertEqual(tr2.window_start, LIVE)

    def test_mixed_profitability(self):
        ev = [PnlEvent("s0", NOW, 5), PnlEvent("s1", NOW, -5), PnlEvent("s2", NOW, 0)]
        tr = compute_track_record(LIVE, ev, spans(5), NOW)
        self.assertEqual(tr.profitable_subscriptions, 1)
        self.assertEqual(tr.total_pnl_micro, 0)
        self.assertEqual(tr.roi_bps, 0)

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            compute_track_record(LIVE, [PnlEvent("s0", datetime(2026, 2, 1), 1)], [], NOW)


if __name__ == "__main__":
    unittest.main()

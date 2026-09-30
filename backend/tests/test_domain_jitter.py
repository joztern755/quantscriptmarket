"""Tests for app.domain.jitter."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import jitter as j  # noqa: E402

SALT = b"s" * 32
BAR = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)


class DelayTest(unittest.TestCase):
    def test_deterministic_and_in_range(self):
        ds = [j.delay_seconds(f"user-{i}", BAR, SALT, 600) for i in range(2000)]
        self.assertEqual(ds, [j.delay_seconds(f"user-{i}", BAR, SALT, 600) for i in range(2000)])
        self.assertTrue(all(0 <= d <= 600 for d in ds))
        self.assertLess(min(ds), 30)
        self.assertGreater(max(ds), 570)
        self.assertGreater(len(set(ds)), 400)

    def test_changes_per_bar_and_salt(self):
        a = [j.delay_seconds(f"u{i}", BAR, SALT, 600) for i in range(50)]
        b = [j.delay_seconds(f"u{i}", BAR + timedelta(days=1), SALT, 600) for i in range(50)]
        c = [j.delay_seconds(f"u{i}", BAR, b"t" * 32, 600) for i in range(50)]
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_timezone_normalized(self):
        other_tz = BAR.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(j.delay_seconds("u", BAR, SALT, 600), j.delay_seconds("u", other_tz, SALT, 600))

    def test_zero_max(self):
        self.assertEqual(j.delay_seconds("u", BAR, SALT, 0), 0)

    def test_inclusive_upper_bound_reachable(self):
        seen = {j.delay_seconds(f"u{i}", BAR, SALT, 1) for i in range(100)}
        self.assertEqual(seen, {0, 1})

    def test_validation(self):
        with self.assertRaises(ValueError):
            j.delay_seconds("u", BAR, b"short", 600)
        with self.assertRaises(TypeError):
            j.delay_seconds("u", BAR, "not-bytes" * 4, 600)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            j.delay_seconds("u", datetime(2026, 9, 30), SALT, 600)
        with self.assertRaises(ValueError):
            j.delay_seconds("u", BAR, SALT, -1)

    def test_due_at(self):
        d = j.delay_seconds("u", BAR, SALT, 600)
        self.assertEqual(j.due_at("u", BAR, SALT, 600), BAR + timedelta(seconds=d))


class FairOrderTest(unittest.TestCase):
    IDS = [f"sub-{i}" for i in range(30)]

    def test_permutation_and_deterministic(self):
        seed = j.bar_seed(SALT, BAR)
        o = j.fair_order(self.IDS, seed)
        self.assertEqual(sorted(o), sorted(self.IDS))
        self.assertEqual(o, j.fair_order(list(reversed(self.IDS)), seed))  # input order irrelevant
        self.assertNotEqual(o, self.IDS)

    def test_changes_per_bar(self):
        a = j.fair_order(self.IDS, j.bar_seed(SALT, BAR))
        b = j.fair_order(self.IDS, j.bar_seed(SALT, BAR + timedelta(days=1)))
        self.assertNotEqual(a, b)

    def test_duplicates_and_empty(self):
        seed = j.bar_seed(SALT, BAR)
        self.assertEqual(j.fair_order([], seed), [])
        self.assertEqual(sorted(j.fair_order(["a", "a", "b"], seed)), ["a", "a", "b"])

    def test_seed_validation(self):
        with self.assertRaises(ValueError):
            j.fair_order(self.IDS, b"x")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import math
import unittest

from app.errors import RateLimited
from app.security.ratelimit import DEFAULT_POLICIES, InMemoryRateLimiter, RateLimit


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TokenBucketTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.rl = InMemoryRateLimiter(RateLimit(capacity=3, refill_per_s=1.0), clock=self.clock)

    def test_burst_then_deny_then_refill(self):
        for _ in range(3):
            self.assertEqual(self.rl.try_acquire("u1"), (True, 0.0))
        ok, retry = self.rl.try_acquire("u1")
        self.assertFalse(ok)
        self.assertAlmostEqual(retry, 1.0)
        self.clock.t += 1.0
        self.assertTrue(self.rl.try_acquire("u1")[0])
        self.assertFalse(self.rl.try_acquire("u1")[0])

    def test_keys_independent(self):
        for _ in range(3):
            self.rl.try_acquire("a")
        self.assertTrue(self.rl.try_acquire("b")[0])

    def test_capped_at_capacity(self):
        self.clock.t += 1000
        for _ in range(3):
            self.assertTrue(self.rl.try_acquire("u")[0])
        self.assertFalse(self.rl.try_acquire("u")[0])

    def test_check_raises(self):
        for _ in range(3):
            self.rl.check("u")
        with self.assertRaises(RateLimited) as cm:
            self.rl.check("u")
        self.assertEqual(cm.exception.details["retry_after"], 1)

    def test_cost(self):
        self.assertEqual(self.rl.try_acquire("u", cost=4), (False, math.inf))
        with self.assertRaises(RateLimited):
            self.rl.check("u", cost=4)
        with self.assertRaises(ValueError):
            self.rl.try_acquire("u", cost=0)

    def test_clock_going_backwards_does_not_mint_tokens(self):
        for _ in range(3):
            self.rl.try_acquire("u")
        self.clock.t -= 100
        self.assertFalse(self.rl.try_acquire("u")[0])

    def test_eviction_bounds_memory(self):
        rl = InMemoryRateLimiter(RateLimit(1, 1.0), clock=self.clock, max_keys=10)
        for i in range(100):
            rl.try_acquire(f"k{i}")
        self.assertEqual(len(rl._buckets), 10)

    def test_policies(self):
        self.assertTrue(all(isinstance(p, RateLimit) for p in DEFAULT_POLICIES.values()))
        with self.assertRaises(ValueError):
            RateLimit(0, 1.0)
        with self.assertRaises(ValueError):
            RateLimit(1, 0)


if __name__ == "__main__":
    unittest.main()

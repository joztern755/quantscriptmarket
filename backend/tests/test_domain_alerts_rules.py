"""Tests for app.domain.alerts_rules."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import alerts_rules as a  # noqa: E402
from app.money import usd  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 15, tzinfo=timezone.utc)


class MarketRulesTest(unittest.TestCase):
    def test_mark_oracle(self):
        self.assertIsNone(a.mark_oracle_divergence("xyz:SILVER", D("30.3"), D("30"), NOW))      # 100 bps = warn edge
        w = a.mark_oracle_divergence("xyz:SILVER", D("30.31"), D("30"), NOW)
        self.assertEqual((w.severity, w.auto_pause_market), ("warn", False))
        self.assertEqual(a.mark_oracle_divergence("xyz:SILVER", D("30.6"), D("30"), NOW).severity, "warn")  # 200 = cap
        c = a.mark_oracle_divergence("xyz:SILVER", D("29.39"), D("30"), NOW)
        self.assertEqual((c.severity, c.auto_pause_market, c.market, c.page_ops), ("critical", True, "xyz:SILVER", True))
        self.assertEqual(c.key, "mark_oracle_divergence:xyz:SILVER:2026-09-30T12")
        z = a.mark_oracle_divergence("X", D("1"), D("0"), NOW)
        self.assertEqual(z.severity, "critical")
        with self.assertRaises(TypeError):
            a.mark_oracle_divergence("X", 1.0, D("1"), NOW)  # type: ignore[arg-type]

    def test_dedupe_key_hour_bucket(self):
        k1 = a.mark_oracle_divergence("BTC", D("110"), D("100"), NOW).key
        k2 = a.mark_oracle_divergence("BTC", D("111"), D("100"), NOW + timedelta(minutes=40)).key
        k3 = a.mark_oracle_divergence("BTC", D("110"), D("100"), NOW + timedelta(minutes=50)).key
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_oi_spike(self):
        self.assertIsNone(a.oi_spike("BTC", usd(150), usd(100), NOW))    # exactly +50% not above
        s = a.oi_spike("BTC", usd(150) + 1, usd(100), NOW)
        self.assertEqual((s.severity, s.auto_pause_market), ("critical", True))
        self.assertIsNone(a.oi_spike("BTC", usd(50), usd(100), NOW))
        z = a.oi_spike("BTC", 1, 0, NOW)
        self.assertEqual(z.severity, "critical")
        self.assertIsNone(a.oi_spike("BTC", 0, 0, NOW))

    def test_funding(self):
        self.assertIsNone(a.funding_spike("BTC", D("0.001"), NOW))
        self.assertIsNone(a.funding_spike("BTC", D("-0.001"), NOW))
        w = a.funding_spike("BTC", D("-0.0011"), NOW)
        self.assertEqual((w.severity, w.auto_pause_market), ("warn", False))
        c = a.funding_spike("BTC", D("0.006"), NOW, critical_threshold=D("0.005"))
        self.assertEqual((c.severity, c.auto_pause_market), ("critical", True))
        self.assertEqual(a.funding_spike("BTC", D("0.0005"), NOW, threshold=D("0.0004")).severity, "warn")

    def test_market_alerts(self):
        out = a.market_alerts("BTC", D("110"), D("100"), usd(200), usd(100), D("0.002"), NOW)
        self.assertEqual({x.kind for x in out}, {"mark_oracle_divergence", "oi_spike", "funding_spike"})
        self.assertEqual(a.market_alerts("BTC", D("100"), D("100"), usd(100), usd(100), D("0"), NOW), [])
        for x in out:
            if x.auto_pause_market:
                self.assertIn(x.kind, a.AUTO_PAUSE_KINDS)
                self.assertEqual(x.severity, "critical")


class UserRulesTest(unittest.TestCase):
    def test_drawdown(self):
        self.assertIsNone(a.user_drawdown("u", "s", usd(1000), -usd(200), NOW))          # exactly 20%
        d = a.user_drawdown("u", "s", usd(1000), -usd(200) - 1, NOW)
        self.assertEqual((d.severity, d.user_id, d.key), ("warn", "u", "user_drawdown:s:2026-09-30"))
        self.assertIsNone(a.user_drawdown("u", "s", usd(1000), usd(500), NOW))
        self.assertIsNone(a.user_drawdown("u", "s", 0, -usd(5), NOW))

    def test_reject_burst(self):
        self.assertIsNone(a.reject_burst("s", "u", 2, NOW))
        self.assertEqual(a.reject_burst("s", "u", 3, NOW).severity, "warn")

    def test_agent_revoked(self):
        ok = a.agent_revoked("u", "0xMaster", "0xAgent", ["0xagent", "0xother"], NOW)
        self.assertIsNone(ok)
        r = a.agent_revoked("u", "0xMaster", "0xAgent", ["0xother"], NOW)
        self.assertEqual((r.severity, r.auto_pause_market, r.key), ("critical", False, "agent_revoked:0xmaster:0xagent"))
        self.assertEqual(a.agent_revoked("u", "0xM", "0xA", [], NOW).severity, "critical")

    def test_new_country(self):
        self.assertIsNone(a.new_country_login("u", "MY", [], NOW))       # first login
        self.assertIsNone(a.new_country_login("u", "my", ["MY"], NOW))
        self.assertIsNone(a.new_country_login("u", "", ["MY"], NOW))
        n = a.new_country_login("u", "sg", ["MY"], NOW)
        self.assertEqual((n.severity, n.key), ("warn", "new_country_login:u:SG"))

    def test_mfa_reset(self):
        m = a.mfa_reset("u", "evt1", NOW)
        self.assertEqual((m.severity, m.key), ("warn", "mfa_reset:u:evt1"))

    def test_withdrawal(self):
        self.assertEqual(a.withdrawal_request("withdrawal", "w1", "u", usd(100), NOW).severity, "info")
        big = a.withdrawal_request("payout", "p1", None, usd(10_000), NOW)
        self.assertEqual((big.severity, big.kind, big.key), ("warn", "payout_request", "payout_request:p1"))
        with self.assertRaises(ValueError):
            a.withdrawal_request("transfer", "x", "u", 1, NOW)

    def test_reconciliation(self):
        self.assertIsNone(a.reconciliation_mismatch("builder_fees", usd(100), usd(101), NOW))   # exactly $1
        r = a.reconciliation_mismatch("builder_fees", usd(100), usd(101) + 1, NOW)
        self.assertEqual((r.severity, r.payload["diff_micro"], r.page_ops), ("critical", usd(1) + 1, True))
        self.assertEqual(a.reconciliation_mismatch("treasury", usd(100), usd(98), NOW).severity, "critical")


if __name__ == "__main__":
    unittest.main()

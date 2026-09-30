"""Sandbox HTTP service: auth, routing, error mapping, size caps, end-to-end endpoints."""
from __future__ import annotations

import http.client
import json
import logging
import threading
import unittest

from app.sandbox.service import MAX_BODY_BYTES, SECRET_HEADER, make_server

SECRET = "s3cret-for-tests-only-0123456789"
DAY = 86_400_000
SRC = 'MARKETS = ["BTC"]\nTIMEFRAME = "1d"\nLOOKBACK = 50\nMAX_LEVERAGE = 1\n' \
      'def signal(bars):\n    c = [b["c"] for b in bars["BTC"]]\n    return {"BTC": 1.0 if c[-1] > c[0] else 0.0}\n'


def bars(n):
    return {"BTC": [{"t": i * DAY, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 1} for i in range(n)]}


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger("sandbox.service").setLevel(logging.WARNING)
        cls.srv = make_server("127.0.0.1", 0, SECRET, max_concurrent=2)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None, secret=SECRET, raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Content-Type": "application/json"}
        if secret is not None:
            h[SECRET_HEADER] = secret
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def test_health_no_auth(self):
        self.assertEqual(self.req("GET", "/healthz", secret=None), (200, {"ok": True}))

    def test_auth(self):
        self.assertEqual(self.req("POST", "/validate", {"source": SRC}, secret=None)[0], 401)
        self.assertEqual(self.req("POST", "/validate", {"source": SRC}, secret="wrong")[0], 401)
        self.assertEqual(self.req("POST", "/nope", {}, secret="wrong")[0], 401)  # no route probing without auth

    def test_validate(self):
        st, body = self.req("POST", "/validate", {"source": SRC})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["meta"]["markets"], ["BTC"])
        st, body = self.req("POST", "/validate", {"source": "import os\n" + SRC})
        self.assertEqual(st, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["errors"][0]["line"], 1)
        st, body = self.req("POST", "/validate", {"source": SRC, "known_markets": ["ETH"]})
        self.assertFalse(body["ok"])

    def test_run(self):
        st, body = self.req("POST", "/run", {"source": SRC, "bars": bars(60)})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["weights"], {"BTC": 1.0})
        self.assertEqual(len(body["code_hash"]), 64)

    def test_run_errors_mapped(self):
        st, body = self.req("POST", "/run", {"source": "import os\n" + SRC, "bars": bars(60)})
        self.assertEqual((st, body["error"]), (422, "validation_failed"))
        bad = SRC.replace('return {"BTC"', 'while True:\n        pass\n    return {"BTC"')
        st, body = self.req("POST", "/run", {"source": bad, "bars": bars(60)})
        self.assertEqual((st, body["error"], body["details"]["kind"]), (422, "script_runtime_error", "timeout"))
        st, body = self.req("POST", "/run", {"source": SRC, "bars": {"BTC": [{"t": 0, "o": "x"}]}})
        self.assertEqual(st, 422)

    def test_bad_bodies(self):
        self.assertEqual(self.req("POST", "/run", raw=b"{not json")[0], 422)
        self.assertEqual(self.req("POST", "/run", raw=b"[1,2]")[0], 422)
        self.assertEqual(self.req("POST", "/run", raw=b'{"source": NaN}')[0], 422)
        st, body = self.req("POST", "/run", raw=b"{}", headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assertEqual(st, 422)
        self.assertEqual(self.req("POST", "/unknown", {})[0], 404)

    def test_backtest(self):
        n = 200
        candles = {"BTC": [[i * DAY, 100 + i, 101 + i, 99 + i, 100 + i, 1] for i in range(n)]}
        data = {"timeframe": "1d", "candles": candles, "funding": {"BTC": [[i * 3_600_000, 0.00001] for i in range(n * 24)]}}
        st, body = self.req("POST", "/backtest", {"source": SRC, "data": data, "params": {"taker_fee_bps": 4.5}})
        self.assertEqual(st, 200, body)
        self.assertIn("metrics", body)
        self.assertIn("out_of_sample", body["metrics"])
        self.assertEqual(len(body["code_hash"]), 64)
        st, body = self.req("POST", "/backtest", {"source": SRC, "data": data, "params": {"evil": 1}})
        self.assertEqual(st, 422)

    def test_nocode(self):
        spec = {"markets": ["BTC"], "timeframe": "1d", "lookback": 60, "max_leverage": 1,
                "indicators": {"s": {"type": "sma", "period": 5}},
                "rules": [{"when": {"all": [{"left": "close", "op": ">", "right": "s"}]}, "weight": 1}]}
        st, body = self.req("POST", "/nocode/compile", {"spec": spec})
        self.assertEqual(st, 200, body)
        st2, run = self.req("POST", "/run", {"source": body["source"], "bars": bars(80)})
        self.assertEqual((st2, run["weights"]), (200, {"BTC": 1.0}))
        st, body = self.req("POST", "/nocode/compile", {"spec": {**spec, "timeframe": "9d"}})
        self.assertEqual(st, 422)
        self.assertEqual(body["details"]["errors"][0]["path"], "timeframe")

    def test_requires_secret(self):
        with self.assertRaises(RuntimeError):
            make_server("127.0.0.1", 0, "")
        with self.assertRaises(RuntimeError):
            make_server("127.0.0.1", 0, "short")


if __name__ == "__main__":
    unittest.main()

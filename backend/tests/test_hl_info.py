"""app.hl.info.InfoClient: retries, backoff, size cap, request bodies, shape checks (no network)."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ExternalServiceError, ValidationFailed  # noqa: E402
from app.hl.fake import load_fixture  # noqa: E402
from app.hl.info import InfoClient  # noqa: E402

USER = "0x1111111111111111111111111111111111111111"


class Resp:
    def __init__(self, status: int, body: object = None, headers: dict | None = None, raw: bytes | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._raw = raw if raw is not None else json.dumps(body).encode()
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._raw), chunk_size):
            yield self._raw[i:i + chunk_size]

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, json=None, timeout=None, stream=None, headers=None):  # noqa: A002
        self.requests.append({"url": url, "json": json, "timeout": timeout, "stream": stream})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses: list, **kw) -> tuple[InfoClient, Session, list]:
    s = Session(responses)
    sleeps: list[float] = []
    kw.setdefault("rng", lambda: 1.0)
    return InfoClient("https://api.hyperliquid.xyz", session=s, sleep=sleeps.append, **kw), s, sleeps


class Transport(unittest.TestCase):
    def test_retries_429_then_succeeds_with_jittered_backoff(self) -> None:
        c, s, sleeps = client([Resp(429), Resp(503), Resp(200, [None])], backoff_base=0.5)
        self.assertEqual(c.perp_dexs(), [None])
        self.assertEqual(len(s.requests), 3)
        self.assertEqual(sleeps, [0.5, 1.0])  # rng=1.0 → full backoff: base·2^attempt
        self.assertEqual(s.requests[0]["url"], "https://api.hyperliquid.xyz/info")
        self.assertEqual(s.requests[0]["timeout"], 10.0)

    def test_retry_after_is_honoured_and_capped(self) -> None:
        c, _, sleeps = client([Resp(429, headers={"Retry-After": "3"}), Resp(200, [None])], rng=lambda: 0.0)
        c.perp_dexs()
        self.assertEqual(sleeps, [3.0])
        c, _, sleeps = client([Resp(429, headers={"Retry-After": "9999"}), Resp(200, [None])], rng=lambda: 0.0,
                              backoff_cap=2.0)
        c.perp_dexs()
        self.assertEqual(sleeps, [8.0])

    def test_gives_up_after_max_retries(self) -> None:
        c, s, _ = client([Resp(500)] * 3, max_retries=2)
        with self.assertRaises(ExternalServiceError) as cm:
            c.meta()
        self.assertEqual(cm.exception.details["upstream_status"], 500)
        self.assertEqual(len(s.requests), 3)

    def test_connection_errors_are_retried(self) -> None:
        c, s, _ = client([ConnectionError("reset"), Resp(200, {"universe": []})])
        self.assertEqual(c.meta(), {"universe": []})
        c, s, _ = client([TimeoutError("t")] * 2, max_retries=1)
        with self.assertRaises(ExternalServiceError):
            c.meta()

    def test_4xx_is_not_retried(self) -> None:
        c, s, _ = client([Resp(422, raw=b"Failed to deserialize")])
        with self.assertRaises(ExternalServiceError) as cm:
            c.meta()
        self.assertEqual(cm.exception.details["upstream_status"], 422)
        self.assertEqual(len(s.requests), 1)

    def test_size_cap(self) -> None:
        c, _, _ = client([Resp(200, raw=b"[" + b"1," * 600 + b"1]")], max_response_bytes=1000)
        with self.assertRaises(ExternalServiceError):
            c.perp_dexs()
        c, _, _ = client([Resp(200, [None], headers={"Content-Length": "5000000"})], max_response_bytes=1000)
        with self.assertRaises(ExternalServiceError):
            c.perp_dexs()

    def test_invalid_json(self) -> None:
        c, _, _ = client([Resp(200, raw=b"<html>")])
        with self.assertRaises(ExternalServiceError):
            c.perp_dexs()

    def test_https_only(self) -> None:
        with self.assertRaises(ValidationFailed):
            InfoClient("http://api.hyperliquid.xyz", session=Session([]))


class Bodies(unittest.TestCase):
    def body(self, fn, response=None):
        c, s, _ = client([Resp(200, response)])
        out = fn(c)
        return s.requests[0]["json"], out

    def test_request_bodies(self) -> None:
        b, _ = self.body(lambda c: c.meta("xyz"), {"universe": []})
        self.assertEqual(b, {"type": "meta", "dex": "xyz"})
        b, _ = self.body(lambda c: c.meta(), {"universe": []})
        self.assertEqual(b, {"type": "meta"})
        b, out = self.body(lambda c: c.meta_and_asset_ctxs("xyz"), load_fixture("metaAndAssetCtxs_xyz"))
        self.assertEqual(b, {"type": "metaAndAssetCtxs", "dex": "xyz"})
        self.assertEqual(len(out[0]["universe"]), len(out[1]))
        b, _ = self.body(lambda c: c.candle_snapshot("xyz:SILVER", "1d", 1, 2), [])
        self.assertEqual(b, {"type": "candleSnapshot", "req": {"coin": "xyz:SILVER", "interval": "1d",
                                                               "startTime": 1, "endTime": 2}})
        b, _ = self.body(lambda c: c.user_fills_by_time("0x" + USER[2:].upper(), 5, 9), [])
        self.assertEqual(b, {"type": "userFillsByTime", "user": USER, "startTime": 5, "aggregateByTime": False,
                             "endTime": 9})
        b, _ = self.body(lambda c: c.user_funding(USER, 5), [])
        self.assertEqual(b, {"type": "userFunding", "user": USER, "startTime": 5})
        b, _ = self.body(lambda c: c.user_non_funding_ledger_updates(USER, 0, 7), [])
        self.assertEqual(b, {"type": "userNonFundingLedgerUpdates", "user": USER, "startTime": 0, "endTime": 7})
        b, _ = self.body(lambda c: c.clearinghouse_state(USER, "xyz"), load_fixture("clearinghouseState_xyz"))
        self.assertEqual(b, {"type": "clearinghouseState", "user": USER, "dex": "xyz"})
        b, out = self.body(lambda c: c.extra_agents(USER), load_fixture("extraAgents"))
        self.assertEqual(b, {"type": "extraAgents", "user": USER})
        self.assertEqual(set(out[0]), {"name", "address", "validUntil"})
        b, out = self.body(lambda c: c.max_builder_fee(USER, "0x" + "2" * 40), 0)
        self.assertEqual(b, {"type": "maxBuilderFee", "user": USER, "builder": "0x" + "2" * 40})
        self.assertEqual(out, 0)
        b, _ = self.body(lambda c: c.user_role(USER), {"role": "user"})
        self.assertEqual(b, {"type": "userRole", "user": USER})
        b, out = self.body(lambda c: c.order_status(USER, "0xA17A1000" + "0" * 24), load_fixture("orderStatus_unknown"))
        self.assertEqual(b["oid"], "0xa17a1000" + "0" * 24)
        self.assertEqual(out, {"status": "unknownOid"})
        b, _ = self.body(lambda c: c.order_status(USER, 123), load_fixture("orderStatus_filled"))
        self.assertEqual(b["oid"], 123)
        b, out = self.body(lambda c: c.sub_accounts(USER), None)
        self.assertEqual(out, [])

    def test_validation(self) -> None:
        c, _, _ = client([])
        for fn in (lambda: c.user_fills("0x123"), lambda: c.meta("XYZ!"), lambda: c.candle_snapshot("BTC", "7m", 0, 1),
                   lambda: c.user_funding(USER, -1), lambda: c.order_status(USER, "0x12"),
                   lambda: c.user_funding(USER, 1.5)):  # type: ignore[arg-type]
            with self.assertRaises(ValidationFailed):
                fn()

    def test_shape_checks(self) -> None:
        for fn, bad in ((lambda c: c.perp_dexs(), [{"name": "xyz"}]), (lambda c: c.meta(), []),
                        (lambda c: c.meta_and_asset_ctxs(), [{"universe": [1]}, []]),
                        (lambda c: c.max_builder_fee(USER, USER), "0.1%"), (lambda c: c.max_builder_fee(USER, USER), True),
                        (lambda c: c.user_role(USER), []), (lambda c: c.l2_book("BTC"), {"levels": [[]]}),
                        (lambda c: c.extra_agents(USER), [{"name": "x"}])):
            c, _, _ = client([Resp(200, bad)])
            with self.assertRaises(ExternalServiceError):
                fn(c)


class Pagination(unittest.TestCase):
    def test_cursor_restarts_at_last_time_and_dedupes(self) -> None:
        pages = [
            [{"tid": 1, "time": 10}, {"tid": 2, "time": 20}, {"tid": 3, "time": 20}],
            [{"tid": 3, "time": 20}, {"tid": 4, "time": 30}],
            [{"tid": 4, "time": 30}],
        ]
        c, s, _ = client([Resp(200, p) for p in pages])
        got = [f["tid"] for f in c.iter_user_fills_by_time(USER, 0, 100)]
        self.assertEqual(got, [1, 2, 3, 4])
        self.assertEqual([r["json"]["startTime"] for r in s.requests], [0, 20, 30])

    def test_non_convergence_raises(self) -> None:
        pages = [[{"time": i, "delta": {"coin": "BTC"}}] for i in range(1, 5)]
        c, _, _ = client([Resp(200, p) for p in pages])
        with self.assertRaises(ExternalServiceError):
            list(c.iter_user_funding(USER, 0, 100, max_pages=3))


if __name__ == "__main__":
    unittest.main()

"""REVIEW_TRADING_KEYS F3 (sandbox determinism) + REVIEW_WEB_INFRA M5 (sandbox isolation).

F3 reproduce → blocked:
* a validated script that keys a set by distinct NaN objects (CPython hashes NaN by address) returns
  process-dependent weights: two single runs with different heap layouts differ (reproduction);
* the upload gate (``backtest_on_data`` default runner = ``run_series_checked``) and the live ``/run``
  (``run_signal_checked``) run the script twice and reject it with ``kind="nondeterministic"``;
* NaN literals are rejected statically; a deterministic script passes the double run unchanged.

M5 reproduce → blocked:
* child processes get a fresh environment (never ``SANDBOX_SHARED_SECRET``) and, when the parent is root, their own
  uid per run (not the parent's, not shared between runs); the service reads the secret once and deletes it from
  ``os.environ``; the parent can be made non-dumpable; concurrency 1 (service default + Cloud Run yaml);
* the sandbox VPC gets a Cloud DNS response policy that answers every name locally (bootstrap.sh).
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sandbox import runner, service  # noqa: E402
from app.sandbox.backtest import MarketData, backtest_on_data  # noqa: E402
from app.sandbox.validate import ensure_valid, validate_source  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DAY = 86_400_000
HEAD = 'MARKETS = ["BTC"]\nTIMEFRAME = "1d"\nLOOKBACK = 50\nMAX_LEVERAGE = 1\n\n'

NAN_SET = HEAD + '''def signal(bars):
    inf = float("inf")
    keys = [inf - inf for _ in range(8)]
    order = {}
    for i, k in enumerate(keys):
        order[k] = i
    for k in set(keys):
        return {"BTC": 1.0 if order[k] % 2 == 0 else -1.0}
    return {}
'''
DETERMINISTIC = HEAD + '''def signal(bars):
    closes = [b["c"] for b in bars["BTC"]]
    names = set(["BTC", "ETH", "SOL"])
    w = 0.0
    for n in sorted(names):
        w += 0.25 if closes[-1] > closes[-2] else -0.25
    return {"BTC": max(-1.0, min(1.0, w))}
'''


def bars(n: int = 120) -> dict:
    return {"BTC": [[i * DAY, 100.0, 101.0, 99.0, 100.0 + (i * 7919) % 11, 10.0] for i in range(n)]}


class NanNondeterminismTest(unittest.TestCase):
    def test_reproduction_heap_layout_changes_output(self) -> None:
        """Before the fix only one run happened: the output depends on object addresses."""
        meta = ensure_valid(NAN_SET)
        self.assertTrue(validate_source(NAN_SET).ok)            # passes the static allowlist (computed NaN)
        runs = {tuple(s.weights["BTC"] for s in runner._run_series(NAN_SET, bars(), meta=meta, perturb=p).steps)
                for p in (0, 1, 50, 777, 3000)}
        self.assertGreater(len(runs), 1, "NaN-keyed set iteration should depend on the heap layout")

    def test_upload_backtest_rejects_nondeterministic_script(self) -> None:
        data = MarketData.from_json({"timeframe": "1d", "candles": bars(), "funding": {}})
        for _ in range(3):
            with self.assertRaises(runner.ScriptRuntimeError) as cm:
                backtest_on_data(NAN_SET, data)
            self.assertEqual(cm.exception.details["kind"], "nondeterministic")

    def test_series_checked_rejects_every_time(self) -> None:
        meta = ensure_valid(NAN_SET)
        for _ in range(10):
            with self.assertRaises(runner.ScriptRuntimeError) as cm:
                runner.run_series_checked(NAN_SET, bars(), meta=meta)
            self.assertEqual(cm.exception.details["kind"], "nondeterministic")

    def test_live_run_checked_catches_it_often(self) -> None:
        """A single live bar has a binary output here, so detection per run is probabilistic (the enforceable
        gate is the upload backtest over every historical bar); over 20 runs at least one must be caught."""
        caught = 0
        for _ in range(20):
            try:
                runner.run_signal_checked(NAN_SET, bars())
            except runner.ScriptRuntimeError as e:
                self.assertEqual(e.details["kind"], "nondeterministic")
                caught += 1
        self.assertGreater(caught, 0)

    def test_deterministic_script_passes_and_matches_single_run(self) -> None:
        meta = ensure_valid(DETERMINISTIC)
        checked = runner.run_series_checked(DETERMINISTIC, bars(), meta=meta)
        single = runner.run_series(DETERMINISTIC, bars(), meta=meta)
        self.assertEqual([s.weights for s in checked.steps], [s.weights for s in single.steps])
        data = MarketData.from_json({"timeframe": "1d", "candles": bars(), "funding": {}})
        self.assertIn("equity_curve", backtest_on_data(DETERMINISTIC, data))
        self.assertEqual(runner.run_signal_checked(DETERMINISTIC, bars()).weights,
                         runner.run_signal(DETERMINISTIC, bars()).weights)

    def test_nan_literals_rejected_statically(self) -> None:
        for body in ('import math\ndef signal(bars):\n    return {"BTC": 0.0} if math.nan else {}\n',
                     'from math import nan\ndef signal(bars):\n    return {}\n',
                     'def signal(bars):\n    s = {float("NaN"): 1}\n    return {}\n',
                     'def signal(bars):\n    s = {float(" -nan "): 1}\n    return {}\n'):
            res = validate_source(HEAD + body)
            self.assertFalse(res.ok, body)
            self.assertIn("nondeterministic", [e.code for e in res.errors])

    def test_service_run_uses_checked_runner(self) -> None:
        with self.assertRaises(runner.ScriptRuntimeError):
            for _ in range(20):
                service.h_run({"source": NAN_SET, "bars": bars()})


class IsolationTest(unittest.TestCase):
    def _capture(self, fn):
        seen: list[dict] = []
        real = subprocess.Popen

        def spy(*a, **kw):
            seen.append(kw)
            return real(*a, **kw)
        runner.subprocess.Popen = spy
        try:
            fn()
        finally:
            runner.subprocess.Popen = real
        return seen

    def test_child_env_never_carries_the_shared_secret(self) -> None:
        os.environ["SANDBOX_SHARED_SECRET"] = "s" * 32
        try:
            seen = self._capture(lambda: runner.run_signal(DETERMINISTIC, bars()))
        finally:
            os.environ.pop("SANDBOX_SHARED_SECRET", None)
        self.assertEqual(len(seen), 1)
        self.assertEqual(set(seen[0]["env"]), {"PYTHONHASHSEED", "LC_ALL"})

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "per-run uid needs a root parent")
    def test_each_run_gets_its_own_non_root_uid(self) -> None:
        seen = self._capture(lambda: [runner.run_signal(DETERMINISTIC, bars()) for _ in range(3)])
        uids = [kw["user"] for kw in seen]
        self.assertEqual(len(set(uids)), 3)
        for kw in seen:
            self.assertGreaterEqual(kw["user"], runner.CHILD_UID_BASE)
            self.assertEqual(kw["group"], kw["user"])
            self.assertEqual(kw["extra_groups"], [])
            self.assertNotEqual(kw["user"], os.geteuid())
        self.assertEqual(runner._uids_in_use, set())            # released after each run
        # the two runs of a checked execution never share a uid either
        both = self._capture(lambda: runner.run_series_checked(DETERMINISTIC, bars()))
        self.assertEqual(len({kw["user"] for kw in both}), 2)

    def test_service_takes_secret_once_and_removes_it(self) -> None:
        env = {"SANDBOX_SHARED_SECRET": "x" * 32, "PORT": "1"}
        self.assertEqual(service.take_secret(env), "x" * 32)
        self.assertNotIn("SANDBOX_SHARED_SECRET", env)
        self.assertEqual(service.take_secret(env), "")

    @unittest.skipUnless(sys.platform.startswith("linux"), "prctl is Linux-only")
    def test_parent_becomes_non_dumpable(self) -> None:
        code = ("import ctypes, sys; sys.path.insert(0, %r); from app.sandbox.service import harden_parent;"
                "ok = harden_parent(); print(ok, ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))"
                % str(ROOT / "backend"))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, ["True", "0"])                     # PR_GET_DUMPABLE → 0

    def test_concurrency_one(self) -> None:
        srv = service.make_server("127.0.0.1", 0, "z" * 32)
        try:
            self.assertEqual(srv.RequestHandlerClass.slots._value, 1)
        finally:
            srv.server_close()
        for name in ("sandbox.service.yaml", "sandbox.connector.service.yaml"):
            y = (ROOT / "infra/gcp/run" / name).read_text()
            self.assertIn("containerConcurrency: 1\n", y, name)
            self.assertIn('{name: SANDBOX_MAX_CONCURRENT, value: "1"}', y, name)
        self.assertIn('os.environ.get("SANDBOX_MAX_CONCURRENT", "1")', (ROOT / "backend/app/sandbox/service.py")
                      .read_text())

    def test_dns_response_policy_on_sandbox_vpc(self) -> None:
        boot = (ROOT / "infra/gcp/bootstrap.sh").read_text()
        self.assertIn('gcloud dns response-policies create "${SANDBOX_DNS_POLICY}" --networks="${SANDBOX_VPC}"', boot)
        self.assertIn('--dns-name="*."', boot)
        self.assertIn('gcloud dns policies create "${SANDBOX_DNS_LOG_POLICY}" --networks="${SANDBOX_VPC}" '
                      '--enable-logging', boot)

    def test_dockerfile_root_parent_only_for_privilege_drop(self) -> None:
        df = (ROOT / "sandbox/Dockerfile").read_text()
        self.assertIn("USER 0:0", df)
        self.assertIn("own fresh uid/gid", df)


if __name__ == "__main__":
    unittest.main()

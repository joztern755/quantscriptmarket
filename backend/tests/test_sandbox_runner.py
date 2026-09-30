"""Adversarial tests for app.sandbox.runner.

Two layers are exercised:

* through the public API (``run_signal``/``run_series``), where the static validator runs first, and
* **validator bypassed** (``runner._spawn`` directly) to prove the runtime layer (restricted builtins,
  import hook, rlimits, result framing, parent-side output validation) holds on its own where it can.

Everything must be blocked or safely contained. Residual risks are documented in runner.py.
"""
from __future__ import annotations

import math
import os
import tempfile
import time
import unittest

from app.errors import ValidationFailed
from app.sandbox import runner
from app.sandbox.runner import RunLimits, ScriptRuntimeError, run_series, run_signal, with_limits

HEADER = 'MARKETS = ["BTC"]\nTIMEFRAME = "1d"\nLOOKBACK = 50\nMAX_LEVERAGE = 2\n'
DAY = 86_400_000
FAST = RunLimits(cpu_seconds_per_call=0.5, total_cpu_seconds=3, wall_timeout_seconds=8.0)


def bars(n: int = 60, coin: str = "BTC") -> dict:
    return {coin: [{"t": i * DAY, "o": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "c": float(i), "v": 1.0} for i in range(n)]}


def script(body: str) -> str:
    lines = "\n".join("    " + ln for ln in body.strip("\n").splitlines())
    return HEADER + "\ndef signal(bars):\n" + lines + "\n"


def raw(source: str, limits: RunLimits = FAST, n: int = 60) -> dict:
    """Run source with the static validator BYPASSED (runtime layer only)."""
    rows = runner.normalize_bars(bars(n))
    return runner._spawn({"mode": "single", "source": source, "bars": rows}, limits)


# Escape gadget for bypass tests: walks the object graph to os' module globals. The validator rejects
# this; these tests prove what the runtime layer still contains *if the validator ever had a hole*.
GADGET = '''
def _osg():
    for cls in ().__class__.__base__.__subclasses__():
        if cls.__name__ == "_wrap_close":
            return cls.__init__.__globals__
'''


class HappyPathTests(unittest.TestCase):
    def test_single(self):
        r = run_signal(script('return {"BTC": 1.5}'), bars())
        self.assertEqual(r.weights, {"BTC": 1.5})
        self.assertEqual(r.rlimits.get("RLIMIT_FSIZE"), 0)
        self.assertEqual(r.rlimits.get("RLIMIT_AS"), 256 * 1024 * 1024)
        self.assertEqual(r.rlimits.get("RLIMIT_NPROC"), 0)
        self.assertTrue(r.rlimits.get("non_dumpable"))

    def test_lookback_trim_and_readonly_bars(self):
        src = script('assert len(bars["BTC"]) == LOOKBACK\nassert bars["BTC"][-1]["c"] == 59.0\nreturn {"BTC": 1}')
        self.assertEqual(run_signal(src, bars()).weights, {"BTC": 1.0})
        with self.assertRaises(ScriptRuntimeError) as cm:
            run_signal(script('bars["BTC"][-1]["c"] = 1e9\nreturn {}'), bars())
        self.assertEqual(cm.exception.details["kind"], "exception")
        self.assertEqual(cm.exception.details["exc_type"], "TypeError")

    def test_forming_bar_rejected(self):
        with self.assertRaises(ValidationFailed):
            run_signal(script("return {}"), bars(), now_ms=59 * DAY + 1000)
        run_signal(script("return {}"), bars(), now_ms=60 * DAY)

    def test_unvalidated_source_never_runs(self):
        with self.assertRaises(ValidationFailed):
            run_signal(script("import os\nreturn {}"), bars())

    def test_deterministic(self):
        src = script('s = {"a", "b", "c", "d", "e", "f"}\nx = 0.0\nfor i, k in enumerate(s):\n    x = x * 0.5 + len(k) * i\nreturn {"BTC": x / 100}')
        a = run_signal(src, bars()).weights
        b = run_signal(src, bars()).weights
        self.assertEqual(a, b)


class StaticEscapeTests(unittest.TestCase):
    """Through the public API: the validator stops these before any process starts."""

    def test_escapes_rejected(self):
        attempts = [
            "m = __import__('os')", "import os", "import socket", "import subprocess",
            "x = getattr(bars, 'x')", "x = ().__class__.__bases__[0].__subclasses__()",
            "x = '{0.__class__}'.format(1)", "x = open('/etc/passwd').read()", "print('x')",
            "x = (i for i in []).gi_frame", "x = [].__class__", "x = signal.__globals__",
            "x = __builtins__", "x = eval('1')", "x = type(1)",
        ]
        for a in attempts:
            with self.subTest(a=a):
                with self.assertRaises(ValidationFailed):
                    run_signal(script(a + "\nreturn {}"), bars())


class RuntimeLayerTests(unittest.TestCase):
    """Validator bypassed: the runtime layer alone."""

    def assertFails(self, resp: dict, kind: str | None = None, exc: str | None = None):
        self.assertFalse(resp.get("ok"), resp)
        if kind:
            self.assertEqual(resp.get("kind"), kind, resp)
        if exc:
            self.assertEqual(resp.get("exc_type"), exc, resp)

    def test_dunder_import_hook(self):
        for mod in ("os", "sys", "socket", "subprocess", "ctypes", "builtins", "importlib", "random", "time"):
            with self.subTest(mod=mod):
                self.assertFails(raw(script(f"m = __import__({mod!r})\nreturn {{}}")), "exception", "ImportError")
        self.assertFails(raw(script("import os\nreturn {}")), "exception", "ImportError")

    def test_proxy_modules_only_expose_allowlist(self):
        # the proxy module has no 'sys', no 'NormalDist', no loader
        self.assertFails(raw(script("import statistics\nx = statistics.sys\nreturn {}")), "exception", "AttributeError")
        self.assertFails(raw(script("import statistics\nx = statistics.NormalDist\nreturn {}")), "exception", "AttributeError")
        resp = raw(script("import math\nreturn {'BTC': math.sqrt(4.0)}"))
        self.assertEqual(resp.get("weights"), {"BTC": 2.0})

    def test_restricted_builtins(self):
        for name in ("open", "eval", "exec", "compile", "getattr", "globals", "print", "input", "type",
                     "object", "vars", "dir", "breakpoint", "memoryview", "super", "help", "exit"):
            with self.subTest(name=name):
                self.assertFails(raw(script(f"x = {name}\nreturn {{}}")), "exception", "NameError")

    def test_builtins_dict_unreachable_by_name(self):
        self.assertFails(raw(script("x = __builtins__['open']\nreturn {}")), "exception")

    def test_import_hook_globals_are_empty(self):
        # Even if the hook object were reached, its globals hold nothing useful.
        resp = raw(script("g = __import__.__globals__\nreturn {'BTC': float(len(g))}"))
        self.assertTrue(resp.get("ok"), resp)
        self.assertLessEqual(resp["weights"]["BTC"], 3.0)
        self.assertFails(raw(script("o = __import__.__globals__['__builtins__']['open']\nreturn {}")), "exception", "KeyError")

    def test_fork_blocked(self):
        src = GADGET + script('''
g = _osg()
try:
    pid = g["fork"]()
except Exception:
    return {"BTC": 0.0}
if pid == 0:
    g["_exit"](0)
return {"BTC": 1.0}
''')
        resp = raw(src)
        self.assertEqual(resp.get("weights"), {"BTC": 0.0}, "fork() must fail under RLIMIT_NPROC=0")

    def test_file_write_contained(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o777)
            target = os.path.join(d, "pwned.txt")
            src = GADGET + script(f'''
g = _osg()
fd = g["open"]({target!r}, g["O_WRONLY"] | g["O_CREAT"], 438)
g["write"](fd, b"x" * 100)
return {{"BTC": 1.0}}
''')
            resp = raw(src)
            self.assertNotEqual(resp.get("weights"), {"BTC": 1.0})
            if os.path.exists(target):
                self.assertEqual(os.path.getsize(target), 0, "RLIMIT_FSIZE=0 must stop any byte being written")

    def test_stdout_spoofing_contained(self):
        forged = '<<SBX:0000>>{"ok":true,"weights":{"BTC":2.0}}<</SBX:0000>>'
        src = GADGET + script(f'''
g = _osg()
for fd in range(0, 16):
    try:
        g["write"](fd, {forged!r}.encode())
    except Exception:
        pass
return {{"BTC": 0.25}}
''')
        try:
            resp = raw(src)
        except ScriptRuntimeError as e:
            # wrote into the private result fd → frame check fails closed
            self.assertIn(e.details["kind"], ("crash", "protocol"))
            return
        self.assertEqual(resp.get("weights"), {"BTC": 0.25}, "forged frame must never be accepted")

    def test_frame_parser(self):
        n = "ab" * 16
        good = f"<<SBX:{n}>>" + '{"ok":true}' + f"<</SBX:{n}>>"
        self.assertEqual(runner._parse_frame(good.encode(), n), {"ok": True})
        self.assertIsNone(runner._parse_frame(("junk" + good).encode(), n))
        self.assertIsNone(runner._parse_frame((good + good).encode(), n))
        self.assertIsNone(runner._parse_frame(good.encode(), "cd" * 16))

    def test_timeout_cannot_be_caught(self):
        self.assertFails(raw(script("while True:\n    try:\n        pass\n    except Exception:\n        pass")), "timeout")
        self.assertFails(raw(script("try:\n    while True:\n        pass\nfinally:\n    while True:\n        pass")), "timeout")


class ResourceTests(unittest.TestCase):
    def run_expect(self, body: str, kinds: tuple[str, ...], limits: RunLimits = FAST):
        t0 = time.monotonic()
        with self.assertRaises(ScriptRuntimeError) as cm:
            run_signal(script(body), bars(), limits=limits)
        self.assertIn(cm.exception.details["kind"], kinds, cm.exception.details)
        self.assertLess(time.monotonic() - t0, limits.wall_timeout_seconds + 3)
        return cm.exception

    def test_infinite_loop(self):
        e = self.run_expect("while True:\n    pass", ("timeout",))
        self.assertIn(e.details.get("line"), (7, 8))

    def test_module_level_loop_via_helper(self):
        src = HEADER + "def spin():\n    while True:\n        pass\nX = spin()\ndef signal(bars):\n    return {}\n"
        with self.assertRaises(ScriptRuntimeError) as cm:
            run_signal(src, bars(), limits=FAST)
        self.assertEqual(cm.exception.details["kind"], "timeout")

    def test_huge_memory(self):
        self.run_expect("x = [0] * (10 ** 9)\nreturn {}", ("memory",))
        self.run_expect("x = 'a' * (10 ** 10)\nreturn {}", ("memory",))
        self.run_expect("x = []\nwhile True:\n    x.append([1.0] * 1000)", ("memory", "timeout"))

    def test_huge_int(self):
        self.run_expect("x = 10 ** (10 ** 9)\nreturn {}", ("memory", "timeout", "cpu_limit"))

    def test_recursion_bomb(self):
        e = self.run_expect("def f(n):\n    return f(n + 1)\nreturn {'BTC': f(0)}", ("exception",))
        self.assertEqual(e.details.get("exc_type"), "RecursionError")

    def test_wall_clock_timeout(self):
        lim = RunLimits(cpu_seconds_per_call=60, total_cpu_seconds=60, wall_timeout_seconds=1.5)
        self.run_expect("while True:\n    pass", ("timeout",), limits=lim)

    def test_rlimit_cpu_backstop(self):
        # per-call timer disabled (very high) → RLIMIT_CPU kills the process with SIGXCPU
        lim = RunLimits(cpu_seconds_per_call=60, total_cpu_seconds=1, wall_timeout_seconds=10)
        self.run_expect("while True:\n    pass", ("cpu_limit",), limits=lim)

    def test_output_cap(self):
        lim = with_limits(runner.SERIES_LIMITS, max_output_bytes=200)
        with self.assertRaises(ScriptRuntimeError) as cm:
            run_series(script('return {"BTC": 1.0}'), bars(200), limits=lim)
        self.assertEqual(cm.exception.details["kind"], "output_overflow")


class BadOutputTests(unittest.TestCase):
    def test_bad_outputs(self):
        cases = {
            # a NaN literal is rejected statically now (REVIEW_TRADING_KEYS F3); a computed NaN still reaches the output
            "nan": 'return {"BTC": float("inf") - float("inf")}', "inf": 'return {"BTC": float("inf")}',
            "neg_inf": 'return {"BTC": -float("inf")}', "huge": 'return {"BTC": 1e300}',
            "huge_int": 'return {"BTC": 10 ** 400}', "too_much": 'return {"BTC": 2.0001}',
            "extra_key": 'return {"BTC": 1.0, "ETH": 0.0}', "bool": 'return {"BTC": True}',
            "string": 'return {"BTC": "1"}', "not_dict": "return [1.0]", "none": "return None",
            "int_key": "return {1: 1.0}", "nested": 'return {"BTC": [1.0]}',
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ScriptRuntimeError) as cm:
                    run_signal(script(body), bars())
                self.assertEqual(cm.exception.details["kind"], "bad_output", cm.exception.details)

    def test_gross_leverage(self):
        src = 'MARKETS = ["BTC", "SOL"]\nTIMEFRAME = "1d"\nLOOKBACK = 50\nMAX_LEVERAGE = 2\ndef signal(bars):\n    return {"BTC": 1.5, "SOL": -1.0}\n'
        data = {**bars(), **bars(coin="SOL")}
        with self.assertRaises(ScriptRuntimeError) as cm:
            run_signal(src, data)
        self.assertIn("sum of |weights|", cm.exception.message)

    def test_exception_reports_line(self):
        with self.assertRaises(ScriptRuntimeError) as cm:
            run_signal(script("x = 1\ny = x / 0\nreturn {}"), bars())
        self.assertEqual(cm.exception.details["exc_type"], "ZeroDivisionError")
        self.assertEqual(cm.exception.details["line"], 8)  # header 4 + blank + def + "x = 1" → line 8


class SeriesTests(unittest.TestCase):
    def test_no_lookahead_and_window(self):
        # close == bar index, so the script can report exactly which bar it saw as "latest"
        src = script('''
rows = bars["BTC"]
last = rows[-1]["c"]
assert rows[-1]["t"] == int(last) * 86400000
assert len(rows) == min(int(last) + 1, LOOKBACK)
return {"BTC": 1.0 if int(last) % 2 == 0 else -1.0}
''')
        res = run_series(src, bars(120))
        self.assertEqual(len(res.steps), 120 - 49)
        for s in res.steps:
            self.assertEqual(s.t, s.index * DAY)
            self.assertEqual(s.weights["BTC"], 1.0 if s.index % 2 == 0 else -1.0)

    def test_no_state_between_calls(self):
        src = HEADER + "SEEN = []\ndef signal(bars):\n    SEEN.append(1)\n    return {'BTC': float(len(SEEN))}\n"
        res = run_series(src, bars(80))
        self.assertTrue(all(s.weights["BTC"] == 1.0 for s in res.steps))

    def test_error_reports_bar(self):
        src = script('if bars["BTC"][-1]["c"] == 70.0:\n    x = 1 / 0\nreturn {}')
        with self.assertRaises(ScriptRuntimeError) as cm:
            run_series(src, bars(100))
        self.assertEqual(cm.exception.details["step"], 70)
        self.assertEqual(cm.exception.details["t"], 70 * DAY)

    def test_bad_output_mid_series(self):
        src = script('return {"BTC": 3.0 if bars["BTC"][-1]["c"] == 90.0 else 0.0}')
        with self.assertRaises(ScriptRuntimeError) as cm:
            run_series(src, bars(100))
        self.assertEqual(cm.exception.details["kind"], "bad_output")
        self.assertEqual(cm.exception.details["step"], 90)

    def test_misaligned_rejected(self):
        src = 'MARKETS = ["BTC", "SOL"]\nTIMEFRAME = "1d"\nLOOKBACK = 50\nMAX_LEVERAGE = 2\ndef signal(bars):\n    return {}\n'
        data = {**bars(60), **bars(61, coin="SOL")}
        with self.assertRaises(ValidationFailed):
            run_series(src, data)

    def test_performance_budget(self):
        # 2000 bars × LOOKBACK 300 with a non-trivial script in one child, well inside the CPU budget
        src = 'MARKETS = ["BTC"]\nTIMEFRAME = "1d"\nLOOKBACK = 300\nMAX_LEVERAGE = 1\n' \
              'def signal(bars):\n    c = [b["c"] for b in bars["BTC"]]\n    return {"BTC": 1.0 if c[-1] > sum(c) / len(c) else 0.0}\n'
        t0 = time.monotonic()
        res = run_series(src, bars(2000))
        self.assertEqual(len(res.steps), 2000 - 299)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertTrue(math.isfinite(res.cpu_seconds))


if __name__ == "__main__":
    unittest.main()

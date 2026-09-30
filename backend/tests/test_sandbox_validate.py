"""Static validator (app.sandbox.validate) — contract checks and AST allowlist."""
from __future__ import annotations

import glob
import os
import unittest

from app.errors import ValidationFailed
from app.sandbox.validate import (
    MAX_SOURCE_BYTES, BadOutput, StrategyMeta, ensure_valid, validate_source, validate_weights,
)

HEADER = 'MARKETS = ["BTC", "xyz:SILVER"]\nTIMEFRAME = "1d"\nLOOKBACK = 100\nMAX_LEVERAGE = 2\n'
GOOD = HEADER + '''
import math
from statistics import mean

def helper(xs, n):
    return sum(xs[-n:]) / n

def signal(bars):
    closes = [b["c"] for b in bars["BTC"]]
    m = mean(closes)
    w = 1.0 if closes[-1] > m else -0.5
    s = math.sqrt(abs(w))
    label = f"w={w:.2f}"
    try:
        x = 1 / 0
    except ZeroDivisionError:
        x = 0
    ys = sorted(closes, key=lambda v: -v)[:3]
    return {"BTC": w, "xyz:SILVER": 0.0}
'''

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "examples", "strategies")


def body(code: str) -> str:
    """Wrap statements inside signal()."""
    lines = "\n".join("    " + ln for ln in code.strip("\n").splitlines())
    return HEADER + "\ndef signal(bars):\n" + lines + "\n    return {}\n"


class ValidSourceTests(unittest.TestCase):
    def test_good_source(self):
        r = validate_source(GOOD)
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.meta, StrategyMeta(("BTC", "xyz:SILVER"), "1d", 100, 2.0))
        self.assertEqual(len(r.code_hash), 64)

    def test_examples_validate(self):
        files = sorted(glob.glob(os.path.join(EXAMPLES, "*.py")))
        self.assertGreaterEqual(len(files), 3)
        for f in files:
            with open(f, encoding="utf-8") as fh:
                r = validate_source(fh.read())
            self.assertTrue(r.ok, (f, [e.to_dict() for e in r.errors]))

    def test_known_markets(self):
        self.assertTrue(validate_source(GOOD, known_markets=["BTC", "xyz:SILVER", "ETH"]).ok)
        r = validate_source(GOOD, known_markets=["BTC"])
        self.assertFalse(r.ok)
        self.assertIn("xyz:SILVER", r.errors[0].message)

    def test_ensure_valid_raises_structured(self):
        with self.assertRaises(ValidationFailed) as cm:
            ensure_valid(body("import os"))
        errs = cm.exception.details["errors"]
        self.assertTrue(errs and errs[0]["line"] is not None)
        self.assertEqual(errs[0]["code"], "forbidden_import")


class ContractTests(unittest.TestCase):
    def check_bad(self, src: str, code: str | None = None):
        r = validate_source(src)
        self.assertFalse(r.ok, src)
        if code:
            self.assertIn(code, [e.code for e in r.errors], [e.to_dict() for e in r.errors])
        return r

    def test_size_limit(self):
        src = GOOD + "\n# " + "x" * MAX_SOURCE_BYTES
        self.check_bad(src, "too_large")

    def test_nul_and_encoding(self):
        self.check_bad(GOOD + "\x00", "bad_encoding")
        self.check_bad(b"\xff\xfe" + GOOD.encode(), "bad_encoding")  # type: ignore[arg-type]

    def test_syntax_error_has_line(self):
        r = self.check_bad(HEADER + "def signal(bars):\n    return {\n", "syntax_error")
        self.assertIsNotNone(r.errors[0].line)

    def test_deep_nesting_does_not_crash(self):
        self.check_bad(HEADER + "x = " + "(" * 5000 + "1" + ")" * 5000 + "\ndef signal(bars):\n    return {}\n")
        self.check_bad(HEADER + "x = " + "-" * 200000 + "1\ndef signal(bars):\n    return {}\n")

    def test_missing_constants(self):
        self.check_bad('TIMEFRAME = "1d"\nLOOKBACK = 100\nMAX_LEVERAGE = 1\ndef signal(bars):\n    return {}\n', "bad_contract")

    def test_bad_constant_values(self):
        cases = [
            ('MARKETS = []', "bad_markets"), ('MARKETS = ["A","B","C","D","E","F"]', "bad_markets"),
            ('MARKETS = ["BTC", "BTC"]', "bad_markets"), ('MARKETS = ["../etc"]', "bad_markets"),
            ('MARKETS = "BTC"', "bad_markets"),
            ('TIMEFRAME = "15m"', "bad_timeframe"), ('LOOKBACK = 49', "bad_lookback"),
            ('LOOKBACK = 1001', "bad_lookback"), ('LOOKBACK = 100.0', "bad_lookback"), ('LOOKBACK = True', "bad_lookback"),
            ('MAX_LEVERAGE = 6', "bad_leverage"), ('MAX_LEVERAGE = 0.5', "bad_leverage"),
            ('MAX_LEVERAGE = True', "bad_leverage"),
        ]
        base = {"MARKETS": 'MARKETS = ["BTC"]', "TIMEFRAME": 'TIMEFRAME = "1d"', "LOOKBACK": "LOOKBACK = 100", "MAX_LEVERAGE": "MAX_LEVERAGE = 1"}
        for line, code in cases:
            parts = dict(base)
            parts[line.split(" ")[0]] = line
            src = "\n".join(parts.values()) + "\ndef signal(bars):\n    return {}\n"
            with self.subTest(line=line):
                self.check_bad(src, code)

    def test_constants_must_be_literals_and_single(self):
        self.check_bad(HEADER.replace("LOOKBACK = 100", "LOOKBACK = 50 + 50") + "def signal(bars):\n    return {}\n", "bad_contract")
        self.check_bad(HEADER + "LOOKBACK = 200\ndef signal(bars):\n    return {}\n", "bad_contract")
        self.check_bad(HEADER + "LOOKBACK += 1\ndef signal(bars):\n    return {}\n", "bad_contract")
        self.check_bad(HEADER.replace("LOOKBACK = 100", "LOOKBACK = X = 100") + "def signal(bars):\n    return {}\n", "bad_contract")

    def test_signal_signature(self):
        self.check_bad(HEADER + "def other(bars):\n    return {}\n", "bad_contract")
        self.check_bad(HEADER + "def signal(bars, extra):\n    return {}\n", "bad_contract")
        self.check_bad(HEADER + "def signal(*bars):\n    return {}\n", "bad_contract")
        self.check_bad(HEADER + "def signal(bars):\n    return {}\ndef signal(bars):\n    return {}\n", "bad_contract")
        self.check_bad(HEADER + "signal = lambda bars: {}\n", "bad_contract")

    def test_toplevel_statements(self):
        self.check_bad(HEADER + "for i in range(3):\n    pass\ndef signal(bars):\n    return {}\n", "forbidden_toplevel")
        self.check_bad(HEADER + "if True:\n    X = 1\ndef signal(bars):\n    return {}\n", "forbidden_toplevel")


class ForbiddenConstructTests(unittest.TestCase):
    """Every escape primitive must be rejected statically, with a line number."""

    CASES = {
        "import_os": "import os",
        "import_sys_alias": "import sys as math2",
        "from_os": "from os import system",
        "from_math_private": "from math import _private",
        "from_star": "from math import *",
        "relative": "from . import x",
        "dunder_import": "m = __import__('os')",
        "builtins_name": "b = __builtins__",
        "getattr": "f = getattr((), 'x')",
        "setattr": "setattr(bars, 'x', 1)",
        "eval": "eval('1')",
        "exec": "exec('1')",
        "compile": "compile('1', 'x', 'exec')",
        "open": "open('/etc/passwd').read()",
        "globals": "g = globals()",
        "locals": "g = locals()",
        "vars": "g = vars()",
        "dir": "g = dir()",
        "type": "t = type(1)",
        "object": "o = object",
        "super": "s = super",
        "breakpoint": "breakpoint()",
        "input": "input()",
        "print": "print('x')",
        "memoryview": "memoryview(b'')",
        "class_chain": "x = ().__class__.__bases__[0].__subclasses__()",
        "dunder_attr": "x = bars.__class__",
        "private_attr": "x = bars._x",
        "mro": "x = int.mro()",
        "format_attack": "x = '{0.__class__}'.format(1)",
        "format_map": "x = '{a}'.format_map({})",
        "format_builtin": "x = format(1, 'x')",
        "fstring_dunder": "x = f'{bars.__class__}'",
        "gen_frame": "x = (i for i in []).gi_frame.f_back.f_globals",
        "func_globals": "x = signal.__globals__",
        "tb_frame": "x = 1\ntry:\n    1/0\nexcept Exception as e:\n    x = e.__traceback__.tb_frame",
        "with": "with x as y:\n    pass",
        "class": "class A:\n    pass",
        "global": "global Q",
        "nonlocal_in_nested": "def f():\n    nonlocal bars\n    return 1",
        "yield": "yield 1",
        "async_def": "async def f():\n    pass",
        "await": "async def f():\n    await g()",
        "match_class_pattern": "match bars:\n    case object(__class__=c):\n        pass",
        "decorator": "@abs\ndef f():\n    pass",
        "bare_except": "try:\n    pass\nexcept:\n    pass",
        "except_star": "try:\n    pass\nexcept* ValueError:\n    pass",
        "attr_store": "bars.x = 1",
        "bytes_literal": "x = b'abc'",
        "matmul": "x = bars @ bars",
        "dunder_name_store": "__name__ = 'x'",
        "dunder_kwarg": "abs(__x=1)",
        "dunder_param": "f = lambda __x: 1",
        "iter_next": "x = next(iter([1]))",
        "hasattr": "hasattr(1, 'x')",
        "statistics_random": "import statistics\nx = statistics.NormalDist(0, 1).samples(3)",
        "statistics_sys": "import statistics\nx = statistics.sys.modules",
        "except_as_dunder": "try:\n    pass\nexcept Exception as __e:\n    pass",
        # identifiers are NFKC-normalised by the parser, so look-alikes are caught (or don't parse)
        "unicode_open": "x = \uff4f\uff50\uff45\uff4e('/etc/passwd')",
        "unicode_dunder_attr": "x = bars.\uff3f\uff3fclass\uff3f\uff3f",
        "unicode_getattr": "x = \U0001d420etattr(bars, 'x')",
    }

    def test_each_case_rejected_with_line(self):
        for name, code in self.CASES.items():
            with self.subTest(case=name):
                r = validate_source(body(code))
                self.assertFalse(r.ok, f"{name} was accepted")
                self.assertTrue(any(e.line for e in r.errors), [e.to_dict() for e in r.errors])

    def test_allowed_constructs(self):
        ok = [
            "x = [i * 2 for i in range(3) if i]", "d = {k: v for k, v in {}.items()}",
            "s = {1, 2}", "g = sum(i for i in range(3))", "f = lambda a, b=1: a + b",
            "x = 1 if bars else 2", "y = (z := 3)", "a, *rest = [1, 2, 3]",
            "while True:\n    break", "assert bars is not None", "x = 'a' + str(1)",
            "x = '%s' % 1", "x = [1, 2][::-1]", "del bars",
            "try:\n    pass\nexcept (ValueError, ZeroDivisionError) as err:\n    x = err.args",
            "import math\nx = math.floor(math.pi)", "from statistics import linear_regression\nr = linear_regression([1, 2, 3], [1, 2, 4])\ns = r.slope",
            "x = isinstance(1, int)", "raise ValueError('bad')", "_tmp = 1",
        ]
        for code in ok:
            with self.subTest(code=code):
                r = validate_source(body(code))
                self.assertTrue(r.ok, [e.to_dict() for e in r.errors])


class OutputValidationTests(unittest.TestCase):
    meta = StrategyMeta(("BTC", "SOL"), "1d", 100, 2.0)

    def test_ok_and_defaults(self):
        self.assertEqual(validate_weights({"BTC": 1}, self.meta), {"BTC": 1.0, "SOL": 0.0})
        self.assertEqual(validate_weights({}, self.meta), {"BTC": 0.0, "SOL": 0.0})
        self.assertEqual(validate_weights({"BTC": -1.0, "SOL": 1.0}, self.meta), {"BTC": -1.0, "SOL": 1.0})

    def test_rejections(self):
        bad = [None, [], {"ETH": 1}, {"BTC": "1"}, {"BTC": True}, {"BTC": float("nan")},
               {"BTC": float("inf")}, {"BTC": -float("inf")}, {"BTC": 2.5}, {"BTC": 1.5, "SOL": -1.0},
               {"BTC": 10 ** 400}, {1: 1.0}]
        for w in bad:
            with self.subTest(w=w):
                with self.assertRaises(BadOutput):
                    validate_weights(w, self.meta)


if __name__ == "__main__":
    unittest.main()

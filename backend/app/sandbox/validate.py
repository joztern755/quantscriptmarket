"""Static validation of creator strategy source (SPEC §10, §5.9).

This runs in the *trusted* process (api service and the sandbox service) and never executes the
uploaded code: it only parses it with :func:`ast.parse` and walks the tree.

Policy (an allowlist, not a blocklist):

* size ≤ 64 KB of UTF-8, no NUL bytes, must parse as Python 3;
* top level may contain only: a docstring, ``import``/``from … import`` of ``math`` / ``statistics``,
  assignments, and ``def`` statements;
* the four contract constants ``MARKETS``, ``TIMEFRAME``, ``LOOKBACK``, ``MAX_LEVERAGE`` must each be
  assigned exactly once at top level to a literal with a valid value;
* exactly one top-level ``def signal(bars)`` taking exactly one positional parameter;
* only the AST node types in :data:`ALLOWED_NODES` may appear. Notably **forbidden**: ``class``,
  ``with``, ``async``/``await``, ``global``/``nonlocal``, ``yield`` (generator *expressions* are
  allowed), ``match`` (class patterns read attributes by name without an Attribute node),
  ``try/except*``, bare ``except:``, decorators, the ``@`` operator, bytes/complex/Ellipsis literals;
* every attribute access must be a *load* of a name in :data:`ALLOWED_ATTRIBUTES` (public methods of
  list/dict/str/set/tuple/int/float plus the exported math/statistics names). Anything starting
  with ``_`` is rejected, as is ``format``/``format_map`` (``"{0.__class__}".format(x)`` attack) and
  every frame/code/generator/traceback attribute (``gi_frame``, ``f_globals``, ``tb_frame`` …);
* identifiers starting with ``__`` and the names in :data:`FORBIDDEN_NAMES` are rejected anywhere.

Decisions on items the SPEC left open: ``lambda`` allowed; ``try/except`` allowed (but not bare
``except:`` — the runner's timeout exception derives from ``BaseException`` and must not be
swallowable); ``class`` forbidden; ``yield`` forbidden; f-strings allowed (their expressions are
ordinary AST nodes and go through the same checks); ``str.format``/``format()`` forbidden.

The runner re-enforces the important parts at runtime (restricted builtins, proxy modules exposing
only allowlisted names), and the real boundary is the sandbox Cloud Run service — see runner.py.
"""
from __future__ import annotations

import ast
import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.errors import ValidationFailed

__all__ = [
    "MAX_SOURCE_BYTES", "ALLOWED_TIMEFRAMES", "TIMEFRAME_MS", "MIN_LOOKBACK", "MAX_LOOKBACK",
    "MAX_MARKETS", "PLATFORM_MAX_LEVERAGE", "SAFE_BUILTINS", "MODULE_EXPORTS", "ALLOWED_ATTRIBUTES",
    "FORBIDDEN_NAMES", "Issue", "StrategyMeta", "ValidationResult", "validate_source", "ensure_valid",
    "validate_weights", "BadOutput", "is_valid_coin",
]

MAX_SOURCE_BYTES = 64 * 1024
ALLOWED_TIMEFRAMES = ("1h", "4h", "1d")
TIMEFRAME_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
MIN_LOOKBACK, MAX_LOOKBACK = 50, 1000
MAX_MARKETS = 5
PLATFORM_MAX_LEVERAGE = 5  # mirrors config.RiskLimits.platform_max_leverage; callers may pass theirs
REQUIRED_CONSTANTS = ("MARKETS", "TIMEFRAME", "LOOKBACK", "MAX_LEVERAGE")
WEIGHT_TOLERANCE = 1e-9

# Builtins exposed to strategy code at runtime (runner.py builds the dict from this list).
SAFE_BUILTINS: tuple[str, ...] = (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "len", "list", "map",
    "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip",
    "isinstance", "ValueError", "ZeroDivisionError", "Exception", "True", "False", "None",
)

# Names each allowed module exposes. The runner hands scripts *proxy* modules containing only these.
# statistics.NormalDist is excluded (NormalDist.samples() draws random numbers → non-deterministic).
MODULE_EXPORTS: dict[str, tuple[str, ...]] = {
    "math": (
        "acos", "acosh", "asin", "asinh", "atan", "atan2", "atanh", "cbrt", "ceil", "comb", "copysign",
        "cos", "cosh", "degrees", "dist", "e", "erf", "erfc", "exp", "exp2", "expm1", "fabs",
        "factorial", "floor", "fmod", "frexp", "fsum", "gamma", "gcd", "hypot", "inf", "isclose",
        "isfinite", "isinf", "isnan", "isqrt", "lcm", "ldexp", "lgamma", "log", "log10", "log1p", "log2",
        "modf", "nan", "nextafter", "perm", "pi", "pow", "prod", "radians", "remainder", "sin", "sinh",
        "sqrt", "tan", "tanh", "tau", "trunc", "ulp",
    ),
    "statistics": (
        "StatisticsError", "correlation", "covariance", "fmean", "geometric_mean", "harmonic_mean",
        "linear_regression", "mean", "median", "median_grouped", "median_high", "median_low", "mode",
        "multimode", "pstdev", "pvariance", "quantiles", "stdev", "variance",
    ),
}

_TYPE_METHODS = (
    # list
    "append", "clear", "copy", "count", "extend", "index", "insert", "pop", "remove", "reverse", "sort",
    # dict (and mappingproxy, which bars are delivered as)
    "fromkeys", "get", "items", "keys", "popitem", "setdefault", "update", "values",
    # str (minus format/format_map/encode)
    "capitalize", "casefold", "center", "endswith", "expandtabs", "find", "isalnum", "isalpha",
    "isascii", "isdecimal", "isdigit", "isidentifier", "islower", "isnumeric", "isprintable", "isspace",
    "istitle", "isupper", "join", "ljust", "lower", "lstrip", "partition", "removeprefix",
    "removesuffix", "replace", "rfind", "rindex", "rjust", "rpartition", "rsplit", "rstrip", "split",
    "splitlines", "startswith", "strip", "swapcase", "title", "upper", "zfill",
    # set / frozenset
    "add", "difference", "difference_update", "discard", "intersection", "intersection_update",
    "isdisjoint", "issubset", "issuperset", "symmetric_difference", "symmetric_difference_update",
    "union",
    # int / float
    "as_integer_ratio", "bit_count", "bit_length", "conjugate", "denominator", "imag", "is_integer",
    "numerator", "real",
    # statistics.linear_regression result
    "slope", "intercept",
    # exceptions
    "args",
)
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(_TYPE_METHODS) | frozenset(
    n for names in MODULE_EXPORTS.values() for n in names
)

FORBIDDEN_NAMES: frozenset[str] = frozenset({
    "open", "eval", "exec", "compile", "__import__", "globals", "locals", "getattr", "setattr",
    "delattr", "hasattr", "vars", "dir", "type", "breakpoint", "input", "print", "help", "memoryview",
    "object", "super", "classmethod", "staticmethod", "property", "format", "id", "hash", "iter",
    "next", "exit", "quit", "copyright", "credits", "license", "__builtins__", "__loader__",
    "__spec__", "__name__", "__file__", "__dict__", "__class__", "BaseException", "SystemExit",
    "KeyboardInterrupt", "GeneratorExit", "bytearray", "bytes", "callable", "chr", "ord", "ascii",
    "repr", "slice", "frozenset", "complex", "aiter", "anext",
})

ALLOWED_NODES: frozenset[type] = frozenset({
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.FunctionDef, ast.Return,
    ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass, ast.Import, ast.ImportFrom,
    ast.alias, ast.Try, ast.ExceptHandler, ast.Raise, ast.Assert, ast.Delete,
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Lambda, ast.IfExp, ast.Dict, ast.Set, ast.ListComp,
    ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension, ast.Compare, ast.Call,
    ast.keyword, ast.Constant, ast.Attribute, ast.Subscript, ast.Starred, ast.Name, ast.List,
    ast.Tuple, ast.Slice, ast.JoinedStr, ast.FormattedValue, ast.NamedExpr, ast.arguments, ast.arg,
    ast.Load, ast.Store, ast.Del,
    ast.And, ast.Or,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.LShift, ast.RShift,
    ast.BitOr, ast.BitXor, ast.BitAnd,
    ast.Invert, ast.Not, ast.UAdd, ast.USub,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
})

_NODE_HINTS = {
    "ClassDef": "class definitions are not allowed",
    "With": "'with' is not allowed (no I/O or context managers)",
    "AsyncWith": "async code is not allowed",
    "AsyncFunctionDef": "async code is not allowed",
    "AsyncFor": "async code is not allowed",
    "Await": "async code is not allowed",
    "Global": "'global' is not allowed; pass values as arguments",
    "Nonlocal": "'nonlocal' is not allowed",
    "Yield": "'yield' is not allowed (generator expressions are fine)",
    "YieldFrom": "'yield from' is not allowed",
    "Match": "'match' statements are not allowed",
    "TryStar": "'except*' is not allowed",
    "MatMult": "the '@' operator is not allowed",
}

_COIN_RE = re.compile(r"^(?:[a-z][a-z0-9]{0,15}:)?[A-Za-z0-9]{1,20}$")


def is_valid_coin(coin: Any) -> bool:
    """Syntactic check for a Hyperliquid perp coin name: ``BTC``, ``kPEPE``, ``xyz:SILVER``."""
    return isinstance(coin, str) and bool(_COIN_RE.match(coin))


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    line: int | None = None
    col: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "line": self.line, "col": self.col}


@dataclass(frozen=True)
class StrategyMeta:
    markets: tuple[str, ...]
    timeframe: str
    lookback: int
    max_leverage: float

    @property
    def interval_ms(self) -> int:
        return TIMEFRAME_MS[self.timeframe]

    def to_dict(self) -> dict[str, Any]:
        return {"markets": list(self.markets), "timeframe": self.timeframe, "lookback": self.lookback,
                "max_leverage": self.max_leverage}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[Issue] = field(default_factory=list)
    meta: StrategyMeta | None = None
    code_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": [e.to_dict() for e in self.errors],
                "meta": self.meta.to_dict() if self.meta else None, "code_hash": self.code_hash}


class BadOutput(ValidationFailed):
    """signal() returned something that violates the output contract."""
    code = "bad_output"


# ---------------------------------------------------------------------------------------------------
# Source validation
# ---------------------------------------------------------------------------------------------------

def _pos(node: ast.AST) -> tuple[int | None, int | None]:
    return getattr(node, "lineno", None), getattr(node, "col_offset", None)


class _Checker:
    def __init__(self) -> None:
        self.errors: list[Issue] = []

    def err(self, code: str, message: str, node: ast.AST | None = None) -> None:
        line, col = _pos(node) if node is not None else (None, None)
        if len(self.errors) < 200:
            self.errors.append(Issue(code, message, line, col))

    def check_ident(self, name: str, node: ast.AST, what: str) -> None:
        if name.startswith("__"):
            self.err("forbidden_name", f"{what} {name!r}: names starting with '__' are not allowed", node)
        elif name in FORBIDDEN_NAMES:
            self.err("forbidden_name", f"{what} {name!r} is not allowed", node)

    def walk(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            t = type(node)
            if t not in ALLOWED_NODES:
                name = t.__name__
                self.err("forbidden_syntax", _NODE_HINTS.get(name, f"syntax '{name}' is not allowed"), node)
                continue
            if t is ast.Name:
                self.check_ident(node.id, node, "name")
            elif t is ast.Attribute:
                attr = node.attr
                if attr.startswith("_"):
                    self.err("forbidden_attribute", f"attribute {attr!r}: names starting with '_' are not allowed", node)
                elif attr not in ALLOWED_ATTRIBUTES:
                    self.err("forbidden_attribute", f"attribute {attr!r} is not in the allowlist", node)
                if not isinstance(node.ctx, ast.Load):
                    self.err("forbidden_attribute", "assigning or deleting attributes is not allowed", node)
            elif t is ast.FunctionDef:
                self.check_ident(node.name, node, "function")
                if node.decorator_list:
                    self.err("forbidden_syntax", "decorators are not allowed", node)
            elif t is ast.arg:
                self.check_ident(node.arg, node, "parameter")
            elif t is ast.keyword:
                if node.arg is not None:
                    self.check_ident(node.arg, node, "keyword")
            elif t is ast.ExceptHandler:
                if node.type is None:
                    self.err("forbidden_syntax", "bare 'except:' is not allowed; use 'except Exception:'", node)
                if node.name:
                    self.check_ident(node.name, node, "name")
            elif t is ast.Import:
                for a in node.names:
                    self._check_module(a.name, node)
                    if a.asname:
                        self.check_ident(a.asname, node, "alias")
            elif t is ast.ImportFrom:
                if node.level:
                    self.err("forbidden_import", "relative imports are not allowed", node)
                    continue
                mod = node.module or ""
                if not self._check_module(mod, node):
                    continue
                for a in node.names:
                    if a.name == "*":
                        self.err("forbidden_import", "'import *' is not allowed", node)
                    elif a.name not in MODULE_EXPORTS[mod]:
                        self.err("forbidden_import", f"{mod}.{a.name} is not allowed", node)
                    if a.asname:
                        self.check_ident(a.asname, node, "alias")
            elif t is ast.Constant:
                v = node.value
                if not (v is None or isinstance(v, (bool, int, float, str))):
                    self.err("forbidden_syntax", f"literal of type {type(v).__name__} is not allowed", node)

    def _check_module(self, name: str, node: ast.AST) -> bool:
        if name not in MODULE_EXPORTS:
            self.err("forbidden_import", f"import of {name!r} is not allowed (only {', '.join(sorted(MODULE_EXPORTS))})", node)
            return False
        return True


def _literal(node: ast.AST) -> tuple[bool, Any]:
    try:
        return True, ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False, None


def _check_toplevel(tree: ast.Module, chk: _Checker, known_markets: Iterable[str] | None,
                    platform_max_leverage: float) -> StrategyMeta | None:
    consts: dict[str, tuple[ast.AST, Any]] = {}
    signal_defs: list[ast.FunctionDef] = []
    body = tree.body
    for idx, stmt in enumerate(body):
        if idx == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            continue  # module docstring
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, ast.FunctionDef):
            if stmt.name == "signal":
                signal_defs.append(stmt)
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [n.id for tgt in targets for n in ast.walk(tgt) if isinstance(n, ast.Name)]
            for n in names:
                if n == "signal":
                    chk.err("bad_contract", "'signal' must be defined only by 'def signal(bars)'", stmt)
                if n in REQUIRED_CONSTANTS:
                    simple = isinstance(stmt, ast.Assign) and len(targets) == 1 and isinstance(targets[0], ast.Name)
                    if not simple:
                        chk.err("bad_contract", f"{n} must be assigned with a plain '{n} = <literal>'", stmt)
                    elif n in consts:
                        chk.err("bad_contract", f"{n} is assigned more than once", stmt)
                    else:
                        consts[n] = (stmt, stmt.value)
            continue
        chk.err("forbidden_toplevel", "only imports, assignments and function definitions are allowed at top level", stmt)

    if len(signal_defs) != 1:
        chk.err("bad_contract", "exactly one top-level 'def signal(bars)' is required"
                + (f" (found {len(signal_defs)})" if signal_defs else ""),
                signal_defs[1] if len(signal_defs) > 1 else None)
    else:
        a = signal_defs[0].args
        if (len(a.args) + len(a.posonlyargs) != 1 or a.vararg or a.kwarg or a.kwonlyargs or a.defaults):
            chk.err("bad_contract", "signal must take exactly one positional parameter: def signal(bars)", signal_defs[0])

    values: dict[str, Any] = {}
    for name in REQUIRED_CONSTANTS:
        if name not in consts:
            chk.err("bad_contract", f"missing top-level constant {name}")
            continue
        stmt, vnode = consts[name]
        ok, val = _literal(vnode)
        if not ok:
            chk.err("bad_contract", f"{name} must be a literal value", stmt)
            continue
        values[name] = (stmt, val)

    meta_ok = len(values) == len(REQUIRED_CONSTANTS)
    markets: tuple[str, ...] = ()
    if "MARKETS" in values:
        stmt, v = values["MARKETS"]
        if not isinstance(v, (list, tuple)) or not (1 <= len(v) <= MAX_MARKETS):
            chk.err("bad_markets", f"MARKETS must be a list of 1–{MAX_MARKETS} coin names", stmt); meta_ok = False
        elif not all(is_valid_coin(c) for c in v):
            chk.err("bad_markets", "MARKETS entries must be Hyperliquid perp coin names like 'BTC' or 'xyz:SILVER'", stmt); meta_ok = False
        elif len(set(v)) != len(v):
            chk.err("bad_markets", "MARKETS contains duplicates", stmt); meta_ok = False
        else:
            markets = tuple(v)
            if known_markets is not None:
                known = set(known_markets)
                unknown = [c for c in markets if c not in known]
                if unknown:
                    chk.err("bad_markets", f"unknown or unsupported markets: {', '.join(unknown)}", stmt); meta_ok = False
    if "TIMEFRAME" in values:
        stmt, v = values["TIMEFRAME"]
        if v not in ALLOWED_TIMEFRAMES:
            chk.err("bad_timeframe", f"TIMEFRAME must be one of {', '.join(ALLOWED_TIMEFRAMES)}", stmt); meta_ok = False
    if "LOOKBACK" in values:
        stmt, v = values["LOOKBACK"]
        if type(v) is not int or not (MIN_LOOKBACK <= v <= MAX_LOOKBACK):
            chk.err("bad_lookback", f"LOOKBACK must be an integer {MIN_LOOKBACK}–{MAX_LOOKBACK}", stmt); meta_ok = False
    if "MAX_LEVERAGE" in values:
        stmt, v = values["MAX_LEVERAGE"]
        if type(v) not in (int, float) or not math.isfinite(v) or not (1 <= v <= platform_max_leverage):
            chk.err("bad_leverage", f"MAX_LEVERAGE must be a number 1–{platform_max_leverage:g}", stmt); meta_ok = False

    if not meta_ok:
        return None
    return StrategyMeta(markets=markets, timeframe=values["TIMEFRAME"][1], lookback=values["LOOKBACK"][1],
                        max_leverage=float(values["MAX_LEVERAGE"][1]))


def validate_source(source: str | bytes, *, known_markets: Iterable[str] | None = None,
                    platform_max_leverage: float = PLATFORM_MAX_LEVERAGE) -> ValidationResult:
    """Validate strategy source. Never raises for bad input; returns structured errors with line numbers.

    ``known_markets``: if given (the live Hyperliquid meta incl. builder dexes), MARKETS must be a subset.
    """
    if isinstance(source, str):
        try:
            raw = source.encode("utf-8")
        except UnicodeEncodeError:
            return ValidationResult(False, [Issue("bad_encoding", "source must be valid UTF-8 text")])
    elif isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    else:
        return ValidationResult(False, [Issue("bad_encoding", "source must be text")])
    code_hash = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_SOURCE_BYTES:
        return ValidationResult(False, [Issue("too_large", f"source is {len(raw)} bytes; limit is {MAX_SOURCE_BYTES}")], code_hash=code_hash)
    if b"\x00" in raw:
        return ValidationResult(False, [Issue("bad_encoding", "source contains NUL bytes")], code_hash=code_hash)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ValidationResult(False, [Issue("bad_encoding", "source must be valid UTF-8")], code_hash=code_hash)
    try:
        tree = ast.parse(text, filename="<strategy>", mode="exec", type_comments=False)
    except SyntaxError as e:
        return ValidationResult(False, [Issue("syntax_error", f"syntax error: {e.msg}", e.lineno, e.offset)], code_hash=code_hash)
    except (ValueError, MemoryError, RecursionError) as e:
        return ValidationResult(False, [Issue("syntax_error", f"could not parse: {type(e).__name__}")], code_hash=code_hash)

    chk = _Checker()
    try:
        chk.walk(tree)
        meta = _check_toplevel(tree, chk, known_markets, platform_max_leverage)
    except RecursionError:
        return ValidationResult(False, [Issue("too_complex", "source is nested too deeply")], code_hash=code_hash)
    if not chk.errors:
        # compile() catches what the parser accepts but the compiler rejects ('return' outside function,
        # 'break' outside loop …). Constant folding in CPython is size-bounded, so this is cheap/safe.
        try:
            compile(tree, "<strategy>", "exec", dont_inherit=True)
        except SyntaxError as e:
            chk.err("syntax_error", f"syntax error: {e.msg}")
            chk.errors[-1] = Issue("syntax_error", f"syntax error: {e.msg}", e.lineno, e.offset)
        except (ValueError, MemoryError, RecursionError) as e:
            chk.err("too_complex", f"could not compile: {type(e).__name__}")
    chk.errors.sort(key=lambda i: (i.line or 0, i.col or 0))
    ok = not chk.errors and meta is not None
    return ValidationResult(ok, chk.errors, meta if ok else None, code_hash)


def ensure_valid(source: str | bytes, **kw: Any) -> StrategyMeta:
    """Like :func:`validate_source` but raises :class:`app.errors.ValidationFailed` (with ``errors``)."""
    res = validate_source(source, **kw)
    if not res.ok or res.meta is None:
        first = res.errors[0] if res.errors else Issue("invalid", "invalid strategy")
        loc = f" (line {first.line})" if first.line else ""
        raise ValidationFailed(f"strategy rejected: {first.message}{loc}",
                               errors=[e.to_dict() for e in res.errors], code_hash=res.code_hash)
    return res.meta


# ---------------------------------------------------------------------------------------------------
# Output validation (trusted side; applied to whatever the sandbox child reports)
# ---------------------------------------------------------------------------------------------------

def validate_weights(raw: Any, meta: StrategyMeta) -> dict[str, float]:
    """SPEC §10 output rules: dict, keys ⊆ MARKETS, finite numbers, |w| ≤ MAX_LEVERAGE,
    Σ|w| ≤ MAX_LEVERAGE. Missing markets mean weight 0. Returns weights for *all* markets."""
    if not isinstance(raw, dict):
        raise BadOutput(f"signal() must return a dict, got {type(raw).__name__}")
    out = {c: 0.0 for c in meta.markets}
    for k, v in raw.items():
        if not isinstance(k, str) or k not in out:
            raise BadOutput(f"signal() returned key {str(k)[:40]!r} which is not in MARKETS")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise BadOutput(f"weight for {k} must be a number, got {type(v).__name__}")
        try:
            f = float(v)
        except OverflowError:
            raise BadOutput(f"weight for {k} is too large") from None
        if not math.isfinite(f):
            raise BadOutput(f"weight for {k} is not finite ({f})")
        if abs(f) > meta.max_leverage + WEIGHT_TOLERANCE:
            raise BadOutput(f"|weight| for {k} = {abs(f):g} exceeds MAX_LEVERAGE {meta.max_leverage:g}")
        out[k] = f + 0.0  # normalise -0.0
    gross = sum(abs(w) for w in out.values())
    if gross > meta.max_leverage + WEIGHT_TOLERANCE:
        raise BadOutput(f"sum of |weights| = {gross:g} exceeds MAX_LEVERAGE {meta.max_leverage:g}")
    return out

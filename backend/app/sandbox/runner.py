"""Execute validated creator strategy code in a separate, resource-limited process (SPEC §10, §5.9).

SECURITY MODEL — READ THIS FIRST
================================
Everything in this module is **defense in depth only**. CPython cannot be made into a secure sandbox
from the inside: a determined attacker with arbitrary Python execution will eventually find an
interpreter bug. The *real* security boundary is the ``sandbox`` Cloud Run service (SPEC §2):

* gVisor (Cloud Run first-generation execution environment) as the kernel boundary;
* **no network egress** — Direct VPC egress with ``all-traffic`` into a VPC/subnet that has no Cloud NAT,
  no routes to the internet, and firewall rules denying all egress; ingress ``internal`` only;
* a dedicated service account with **no IAM roles** (no DB, KMS, Secret Manager, metadata-token value);
* nothing secret in the container: no env secrets besides the service's own inbound shared secret,
  no credentials files, no data other than what a request carries;
* per-request process isolation (this module) + ``--concurrency`` kept low + max instances capped;
* the container runs as an unprivileged uid (10001); if this module ever runs as root (dev/CI) the child
  is dropped to ``nobody``. The child also sets ``PR_SET_NO_NEW_PRIVS`` and ``PR_SET_DUMPABLE=0`` so a
  concurrently running script (same uid) cannot ptrace it or read its memory.

What this module adds on top of the static AST allowlist (validate.py):

1. A fresh interpreter per request: ``python3 -s -S -B -P`` with an **empty environment** (only
   ``PYTHONHASHSEED=0`` for determinism — that is why ``-I``'s ``-E`` is not used; ``-P -s`` give the
   rest of ``-I``), cwd = a fresh empty temp dir, ``close_fds``, its own session/process group (so the
   whole group is SIGKILLed on timeout).
2. Resource limits set by the child on itself before any user code runs: ``RLIMIT_CPU`` (hard CPU
   ceiling → SIGXCPU), ``RLIMIT_AS`` 256 MB, ``RLIMIT_FSIZE`` 0 (no file writes), ``RLIMIT_NOFILE``
   16, ``RLIMIT_NPROC`` 0 (no fork; not enforced for root — the container runs as non-root), and
   ``RLIMIT_CORE`` 0. Plus a per-call CPU timer (``ITIMER_PROF``) that raises a ``BaseException``
   subclass the script cannot name or catch (bare ``except:`` is rejected statically).
3. User code runs with a **restricted builtins dict** (explicit allowlist, :data:`SAFE_BUILTINS`)
   and ``import`` resolves only to *proxy* modules holding the allowlisted ``math``/``statistics``
   names. The import hook itself is built with an empty globals dict so even reaching it yields nothing.
4. Bars are passed as read-only ``mappingproxy`` objects, and in series (backtest) mode each call sees a
   slice that ends at the bar being evaluated — no look-ahead — and a **freshly executed module** so
   no state leaks between bars (live runs are one process per bar close, so backtest == live).
5. Result channel: before user code runs, the child ``dup()``s stdout to a private fd and points fds
   0/1/2 at ``/dev/null``. The result is written to the private fd inside a frame keyed by a random
   per-run nonce; the parent accepts exactly one well-formed frame, caps total output size, and
   re-validates every weight itself (:func:`app.sandbox.validate.validate_weights`). Nothing the script
   prints (it has no ``print`` anyway) can spoof a result.
6. Wall-clock timeout enforced by the parent with ``killpg(SIGKILL)``.

Only stdlib is used (SPEC §11).
"""
from __future__ import annotations

import json
import math
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from app.errors import ValidationFailed
from app.sandbox.validate import (
    MODULE_EXPORTS, SAFE_BUILTINS, BadOutput, StrategyMeta, ensure_valid, validate_weights,
)

__all__ = [
    "RunLimits", "SINGLE_LIMITS", "SERIES_LIMITS", "ScriptRuntimeError", "SignalResult", "SeriesStep",
    "SeriesResult", "run_signal", "run_series", "normalize_bars", "BadOutput",
]

MB = 1024 * 1024


@dataclass(frozen=True)
class RunLimits:
    cpu_seconds_per_call: float = 2.0      # SPEC §10: 2 s CPU per signal() call (incl. module exec)
    total_cpu_seconds: int = 4             # RLIMIT_CPU for the whole child (hard backstop)
    memory_bytes: int = 256 * MB           # RLIMIT_AS
    wall_timeout_seconds: float = 10.0     # parent-side SIGKILL of the process group
    max_output_bytes: int = 256 * 1024     # parent refuses (and kills) beyond this
    max_open_files: int = 16
    recursion_limit: int = 400


SINGLE_LIMITS = RunLimits()
SERIES_LIMITS = RunLimits(total_cpu_seconds=120, wall_timeout_seconds=240.0, max_output_bytes=32 * MB)


class ScriptRuntimeError(ValidationFailed):
    """The script failed at runtime (exception, timeout, memory, crash, bad output).

    ``details``: ``kind`` ∈ {exception, timeout, cpu_limit, memory, bad_output, crash, output_overflow,
    protocol}, plus ``line``/``exc_type`` for exceptions and ``step``/``t`` in series mode."""
    code = "script_runtime_error"


@dataclass(frozen=True)
class SignalResult:
    weights: dict[str, float]
    cpu_seconds: float
    rlimits: dict[str, Any] = field(default_factory=dict)   # what the child actually applied


@dataclass(frozen=True)
class SeriesStep:
    index: int          # index into the aligned bar arrays: the last CLOSED bar this signal saw
    t: int              # open time (ms) of that bar
    weights: dict[str, float]


@dataclass(frozen=True)
class SeriesResult:
    steps: list[SeriesStep] = field(default_factory=list)
    cpu_seconds: float = 0.0
    rlimits: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------
# Child bootstrap. Passed with -c; reads one JSON request from stdin; writes one framed JSON response.
# Keep it small and self-contained (stdlib only, no app imports: -S and no sys.path games).
# ---------------------------------------------------------------------------------------------------
_BOOTSTRAP = r'''
import sys, os, json, math, resource, signal, types, builtins
def _main():
    req = json.loads(sys.stdin.buffer.read())
    nonce = req["nonce"].encode("ascii")
    out_fd = os.dup(1)
    dn = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(dn, fd)
    os.close(dn)

    def emit(obj):
        try:
            body = json.dumps(obj, allow_nan=True, separators=(",", ":")).encode()
        except Exception as e:  # pragma: no cover
            body = json.dumps({"ok": False, "kind": "protocol", "message": "unserialisable result"}).encode()
        data = b"<<SBX:" + nonce + b">>" + body + b"<</SBX:" + nonce + b">>"
        mv = memoryview(data)
        while mv:
            n = os.write(out_fd, mv)
            mv = mv[n:]

    lim = req["limits"]
    applied = {}
    # Not ptrace-able / not /proc/<pid>/mem-readable by other same-uid processes (concurrent scripts),
    # and no privilege gain through setuid binaries even after an escape.
    try:
        import ctypes
        _libc = ctypes.CDLL(None, use_errno=True)
        applied["no_new_privs"] = _libc.prctl(38, 1, 0, 0, 0) == 0   # PR_SET_NO_NEW_PRIVS
        applied["non_dumpable"] = _libc.prctl(4, 0, 0, 0, 0) == 0    # PR_SET_DUMPABLE = 0
        del _libc, ctypes
    except Exception:
        applied["no_new_privs"] = applied["non_dumpable"] = False
    def setl(name, value):
        r = getattr(resource, name, None)
        if r is None:
            applied[name] = "unsupported"; return
        try:
            soft, hard = resource.getrlimit(r)
            v = value if hard == resource.RLIM_INFINITY else min(value, hard)
            h = v
            if name == "RLIMIT_CPU":
                h = v + 1 if hard == resource.RLIM_INFINITY else min(v + 1, hard)
            resource.setrlimit(r, (v, h))
            applied[name] = v
        except (ValueError, OSError) as e:
            applied[name] = "failed"
    setl("RLIMIT_CORE", 0)
    setl("RLIMIT_CPU", int(lim["total_cpu_seconds"]))
    setl("RLIMIT_AS", int(lim["memory_bytes"]))
    setl("RLIMIT_FSIZE", 0)
    setl("RLIMIT_NOFILE", int(lim["max_open_files"]))
    setl("RLIMIT_NPROC", 0)
    sys.setrecursionlimit(int(lim["recursion_limit"]))

    pol = req["policy"]
    safe = {}
    for n in pol["builtins"]:
        if hasattr(builtins, n):
            safe[n] = getattr(builtins, n)
    mods = {}
    for mname, names in pol["modules"].items():
        real = __import__(mname)
        m = types.ModuleType(mname)
        for n in names:
            if hasattr(real, n):
                setattr(m, n, getattr(real, n))
        mods[mname] = m
    # The import hook is created in an (almost) empty globals dict: reaching it exposes nothing.
    ig = {"__builtins__": {"ImportError": ImportError}, "_mods": mods}
    exec("def _imp(name, globals=None, locals=None, fromlist=(), level=0):\n"
         "    if level == 0 and name in _mods:\n"
         "        return _mods[name]\n"
         "    raise ImportError('import of %r is not allowed' % (name,))\n", ig)
    safe["__import__"] = ig["_imp"]

    class _CallTimeout(BaseException):
        pass
    def _on_prof(signum, frame):
        raise _CallTimeout()
    signal.signal(signal.SIGPROF, _on_prof)
    per_call = float(lim["cpu_seconds_per_call"])

    code = compile(req["source"], "<strategy>", "exec", dont_inherit=True)
    del req["source"]

    def cpu():
        ru = resource.getrusage(resource.RUSAGE_SELF)
        return ru.ru_utime + ru.ru_stime

    def mkbars(rows):
        MP = types.MappingProxyType
        return [MP({"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}) for r in rows]

    def call(view):
        g = {"__builtins__": safe, "__name__": "strategy"}
        signal.setitimer(signal.ITIMER_PROF, per_call, 0.05)
        try:
            exec(code, g)
            fn = g.get("signal")
            if not isinstance(fn, types.FunctionType):
                raise _ContractError("signal is not a function")
            return fn(view)
        finally:
            signal.setitimer(signal.ITIMER_PROF, 0, 0)

    class _ContractError(Exception):
        pass

    def conv(res):
        if type(res) is not dict:
            return None, "signal() must return a dict, got " + type(res).__name__
        if len(res) > 64:
            return None, "signal() returned too many keys"
        out = {}
        for k, v in res.items():
            if type(k) is not str or len(k) > 64:
                return None, "signal() returned a non-string or overlong key"
            if type(v) not in (int, float):
                return None, "weight for " + k + " must be int or float, got " + type(v).__name__
            try:
                out[k] = float(v)
            except OverflowError:
                return None, "weight for " + k + " is too large"
        return out, None

    def describe(e):
        line = None
        tb = e.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == "<strategy>":
                line = tb.tb_lineno
            tb = tb.tb_next
        if isinstance(e, _CallTimeout):
            return {"kind": "timeout", "message": "signal() exceeded %.1fs CPU" % per_call, "line": line}
        if isinstance(e, MemoryError):
            return {"kind": "memory", "message": "memory limit exceeded", "line": line}
        if isinstance(e, RecursionError):
            return {"kind": "exception", "exc_type": "RecursionError", "message": "maximum recursion depth exceeded", "line": line}
        if isinstance(e, _ContractError):
            return {"kind": "bad_output", "message": str(e)[:300], "line": None}
        try:
            msg = str(e)[:300]
        except BaseException:
            msg = "<unprintable>"
        return {"kind": "exception", "exc_type": type(e).__name__[:60], "message": msg, "line": line}

    mode = req["mode"]
    ts_by = {c: [r[0] for r in rows] for c, rows in req["bars"].items()}
    bars = {c: mkbars(rows) for c, rows in req["bars"].items()}
    del req["bars"]
    if mode == "single":
        try:
            res = call(bars)
        except BaseException as e:
            emit(dict(ok=False, cpu=cpu(), rlimits=applied, **describe(e))); return
        w, err = conv(res)
        if err:
            emit({"ok": False, "kind": "bad_output", "message": err, "cpu": cpu(), "rlimits": applied}); return
        emit({"ok": True, "weights": w, "cpu": cpu(), "rlimits": applied}); return

    # series mode: evaluate at each index i in [start, end); the script sees bars[: i + 1][-lookback:]
    L = int(req["lookback"])
    start, end = int(req["start"]), int(req["end"])
    coins = list(bars)
    ts = ts_by[coins[0]]
    steps = []
    for i in range(start, end):
        lo = max(0, i + 1 - L)
        view = {}
        for c in coins:
            sl = bars[c][lo:i + 1]
            # no look-ahead: the newest bar handed to the script is exactly bar i
            assert sl[-1]["t"] == ts[i] and ts_by[c][i] == ts[i], "look-ahead guard"
            view[c] = sl
        try:
            res = call(view)
        except BaseException as e:
            d = describe(e); d.update(ok=False, step=i, t=ts[i], cpu=cpu(), rlimits=applied)
            emit(d); return
        w, err = conv(res)
        if err:
            emit({"ok": False, "kind": "bad_output", "message": err, "step": i, "t": ts[i], "cpu": cpu(), "rlimits": applied}); return
        steps.append([i, ts[i], w])
    emit({"ok": True, "steps": steps, "cpu": cpu(), "rlimits": applied})

try:
    _main()
except MemoryError:
    os._exit(3)
'''


def _python_argv() -> list[str]:
    return [sys.executable, "-s", "-S", "-B", "-P", "-c", _BOOTSTRAP]


_CHILD_ENV = {"PYTHONHASHSEED": "0", "LC_ALL": "C.UTF-8"}
# If the parent runs as root (it must not in prod — the image runs as uid 10001 — but dev/CI often do),
# drop the child to nobody so RLIMIT_NPROC=0 is enforced and root-only files stay out of reach.
_NOBODY = 65534


def _privilege_kwargs() -> dict[str, Any]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return {"user": _NOBODY, "group": _NOBODY, "extra_groups": []}
    return {}
_SIG_NAMES = {getattr(signal, n): n for n in ("SIGXCPU", "SIGKILL", "SIGSEGV", "SIGXFSZ", "SIGABRT", "SIGBUS") if hasattr(signal, n)}


def _spawn(request: dict[str, Any], limits: RunLimits) -> dict[str, Any]:
    """Run the bootstrap child with ``request``; return its decoded response or raise ScriptRuntimeError."""
    nonce = secrets.token_hex(16)
    request = dict(request, nonce=nonce, limits={
        "cpu_seconds_per_call": limits.cpu_seconds_per_call, "total_cpu_seconds": limits.total_cpu_seconds,
        "memory_bytes": limits.memory_bytes, "max_open_files": limits.max_open_files,
        "recursion_limit": limits.recursion_limit,
    }, policy={"builtins": list(SAFE_BUILTINS), "modules": {k: list(v) for k, v in MODULE_EXPORTS.items()}})
    payload = json.dumps(request, allow_nan=False, separators=(",", ":")).encode()

    out = bytearray()
    state = {"overflow": False, "killed": False}
    with tempfile.TemporaryDirectory(prefix="sbx-") as cwd:
        proc = subprocess.Popen(
            _python_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=cwd, env=dict(_CHILD_ENV), close_fds=True, start_new_session=True, **_privilege_kwargs(),
        )

        def kill() -> None:
            state["killed"] = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        def writer() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(payload)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        def reader() -> None:
            assert proc.stdout is not None
            while True:
                try:
                    chunk = proc.stdout.read1(65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                if len(out) + len(chunk) > limits.max_output_bytes:
                    state["overflow"] = True
                    kill()
                    break
                out.extend(chunk)

        tw = threading.Thread(target=writer, daemon=True)
        tr = threading.Thread(target=reader, daemon=True)
        tw.start(); tr.start()
        timed_out = False
        try:
            proc.wait(timeout=limits.wall_timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill()
            proc.wait()
        # Also reap anything the child may have left in its process group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        tr.join(5); tw.join(5)
        for s in (proc.stdout, proc.stdin):
            try:
                if s:
                    s.close()
            except OSError:
                pass

    rc = proc.returncode
    if state["overflow"]:
        raise ScriptRuntimeError("sandbox output exceeded the size cap", kind="output_overflow")
    if timed_out:
        raise ScriptRuntimeError(f"wall-clock timeout ({limits.wall_timeout_seconds:g}s)", kind="timeout")
    resp = _parse_frame(bytes(out), nonce)
    if resp is None:
        if rc is not None and rc < 0:
            sig = _SIG_NAMES.get(-rc, f"signal {-rc}")
            if sig == "SIGXCPU" or (sig == "SIGKILL" and not state["killed"]):
                raise ScriptRuntimeError("CPU time limit exceeded", kind="cpu_limit", signal=sig)
            raise ScriptRuntimeError(f"sandbox process died ({sig})", kind="crash", signal=sig)
        if rc == 3:
            raise ScriptRuntimeError("memory limit exceeded", kind="memory")
        if out:
            # bytes arrived but not exactly one frame with our nonce: the result channel was tampered with
            raise ScriptRuntimeError("sandbox result channel corrupted; result discarded", kind="protocol")
        raise ScriptRuntimeError(f"sandbox produced no result (exit {rc})", kind="crash", exit_code=rc)
    return resp


def _parse_frame(data: bytes, nonce: str) -> dict[str, Any] | None:
    start = b"<<SBX:" + nonce.encode() + b">>"
    end = b"<</SBX:" + nonce.encode() + b">>"
    if data.count(start) != 1 or data.count(end) != 1:
        return None
    if not data.startswith(start) or not data.endswith(end):
        return None  # anything outside the frame means someone else wrote to our channel
    body = data[len(start):-len(end)]
    try:
        resp = json.loads(body, parse_constant=lambda c: float(c))
    except (ValueError, RecursionError):
        return None
    return resp if isinstance(resp, dict) else None


def _raise_child_error(resp: dict[str, Any]) -> None:
    kind = str(resp.get("kind", "exception"))
    msg = str(resp.get("message", ""))[:300]
    details = {k: resp[k] for k in ("line", "exc_type", "step", "t") if k in resp and resp[k] is not None}
    prefix = {"exception": f"{resp.get('exc_type', 'Error')}: ", "timeout": "", "memory": "", "bad_output": ""}.get(kind, "")
    where = f" (line {details['line']})" if "line" in details else ""
    raise ScriptRuntimeError(f"{prefix}{msg}{where}", kind=kind, **details)


# ---------------------------------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------------------------------

def _num(x: Any) -> float:
    if isinstance(x, bool):
        raise ValueError("bool is not a price")
    f = float(x)
    if not math.isfinite(f):
        raise ValueError("non-finite price")
    return f


def normalize_bars(bars: Mapping[str, Sequence[Any]]) -> dict[str, list[list[float | int]]]:
    """Accept bars as dicts ``{"t","o","h","l","c","v"}`` (numbers or Hyperliquid strings) or rows
    ``[t, o, h, l, c, v]``; return rows sorted by ``t`` with strictly increasing timestamps."""
    out: dict[str, list[list[float | int]]] = {}
    for coin, seq in bars.items():
        rows: list[list[float | int]] = []
        for b in seq:
            if isinstance(b, Mapping):
                row = [int(b["t"]), _num(b["o"]), _num(b["h"]), _num(b["l"]), _num(b["c"]), _num(b.get("v", 0.0))]
            else:
                t, o, h, l, c, v = b
                row = [int(t), _num(o), _num(h), _num(l), _num(c), _num(v)]
            rows.append(row)
        rows.sort(key=lambda r: r[0])
        for a, b in zip(rows, rows[1:]):
            if a[0] >= b[0]:
                raise ValidationFailed(f"duplicate bar timestamp for {coin}: {b[0]}")
        out[coin] = rows
    return out


# ---------------------------------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------------------------------

def run_signal(source: str, bars: Mapping[str, Sequence[Any]], *, meta: StrategyMeta | None = None,
               limits: RunLimits = SINGLE_LIMITS, now_ms: int | None = None) -> SignalResult:
    """Evaluate ``signal(bars)`` once (live: once per bar close per strategy version).

    ``bars`` must contain every coin in MARKETS and only CLOSED bars. Each coin is trimmed to the last
    LOOKBACK bars. If ``now_ms`` is given, bars whose close time (t + interval) is after ``now_ms`` are
    rejected (look-ahead / forming-bar guard). Returns validated weights for every market."""
    if meta is None:
        meta = ensure_valid(source)
    else:
        ensure_valid(source)  # never execute unvalidated source, even if the caller has meta
    rows = normalize_bars(bars)
    missing = [c for c in meta.markets if c not in rows or not rows[c]]
    if missing:
        raise ValidationFailed(f"bars missing for {', '.join(missing)}")
    view = {}
    for c in meta.markets:
        r = rows[c][-meta.lookback:]
        if now_ms is not None and r[-1][0] + meta.interval_ms > now_ms:
            raise ValidationFailed(f"last bar for {c} is not closed yet (look-ahead guard)")
        view[c] = r
    resp = _spawn({"mode": "single", "source": source, "bars": view}, limits)
    if not resp.get("ok"):
        _raise_child_error(resp)
    try:
        weights = validate_weights(resp.get("weights"), meta)
    except BadOutput as e:
        raise ScriptRuntimeError(e.message, kind="bad_output") from None
    return SignalResult(weights=weights, cpu_seconds=float(resp.get("cpu", 0.0)), rlimits=dict(resp.get("rlimits") or {}))


def run_series(source: str, aligned_bars: Mapping[str, Sequence[Any]], *, meta: StrategyMeta | None = None,
               start_index: int | None = None, end_index: int | None = None,
               limits: RunLimits = SERIES_LIMITS) -> SeriesResult:
    """Evaluate ``signal`` at every bar index ``i`` in ``[start_index, end_index)`` in ONE child process.

    ``aligned_bars``: every coin in MARKETS with identical timestamp sequences (see
    ``backtest.align_bars``). At step ``i`` the script sees, per coin, ``bars[max(0, i+1-LOOKBACK) : i+1]``
    — bar ``i`` is the last closed bar; nothing after it exists in the view (asserted in the child, and
    re-checked here on the returned step timestamps). Default ``start_index`` = LOOKBACK − 1 so every call
    sees exactly LOOKBACK bars, as live does. The module is re-executed for every call (no carried state)."""
    if meta is None:
        meta = ensure_valid(source)
    else:
        ensure_valid(source)
    rows = normalize_bars(aligned_bars)
    missing = [c for c in meta.markets if c not in rows]
    if missing:
        raise ValidationFailed(f"bars missing for {', '.join(missing)}")
    rows = {c: rows[c] for c in meta.markets}
    ts = [r[0] for r in rows[meta.markets[0]]]
    for c in meta.markets[1:]:
        if [r[0] for r in rows[c]] != ts:
            raise ValidationFailed("bars are not aligned across markets; use backtest.align_bars")
    n = len(ts)
    start = meta.lookback - 1 if start_index is None else start_index
    end = n if end_index is None else end_index
    if not (0 <= start <= end <= n):
        raise ValidationFailed(f"bad step range [{start}, {end}) for {n} bars")
    if start == end:
        return SeriesResult([], 0.0)
    resp = _spawn({"mode": "series", "source": source, "bars": rows, "lookback": meta.lookback,
                   "start": start, "end": end}, limits)
    if not resp.get("ok"):
        _raise_child_error(resp)
    steps: list[SeriesStep] = []
    expected = start
    for item in resp.get("steps", []):
        i, t, w = item
        if i != expected or t != ts[i]:
            raise ScriptRuntimeError("sandbox returned out-of-order steps", kind="protocol")
        expected += 1
        try:
            steps.append(SeriesStep(i, t, validate_weights(w, meta)))
        except BadOutput as e:
            raise ScriptRuntimeError(f"{e.message} (at bar t={t})", kind="bad_output", step=i, t=t) from None
    if expected != end:
        raise ScriptRuntimeError("sandbox returned an incomplete series", kind="protocol")
    return SeriesResult(steps=steps, cpu_seconds=float(resp.get("cpu", 0.0)), rlimits=dict(resp.get("rlimits") or {}))


def with_limits(base: RunLimits, **changes: Any) -> RunLimits:
    """Convenience: ``with_limits(SERIES_LIMITS, total_cpu_seconds=30)``."""
    return replace(base, **changes)

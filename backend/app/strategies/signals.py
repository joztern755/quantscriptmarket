"""Ingest the Ed25519-signed in-house signal feed from the terminal (SPEC §7, §10).

Wire format (produced by ``signals/emit.js``; ``signals.json`` is canonical JSON — sorted keys, no whitespace, integers
only, printable-ASCII strings — and those exact bytes are what ``signals.sig`` signs)::

    {"as_of":"2026-09-25","engine_sha256":"<64 hex>","generated_at":"2026-09-30T00:41:07Z",
     "strategies":{"silver":{"last_action":"SELL","last_action_date":"1980-01-15","market":"xyz:SILVER",
                             "script_sha256":"<64 hex>","status":"trades","target_weight":0}}}

    signals.sig = base64(Ed25519(private_key, bytes of signals.json))     (64-byte signature)

``engine_sha256`` = sha256 of the canonical JSON ``{key: script_sha256}`` over every strategy in the file.

Order of checks (fail closed; nothing is parsed before it is authenticated):
  1. transport: HTTPS only, no redirects, timeout, 1 MB cap on the body and 1 KiB on the signature;
  2. signature: the raw body bytes against the pinned public key (``Settings.signals_pubkey_b64``, raw 32 bytes, base64);
  3. strict parse: UTF-8, no duplicate keys, no floats / NaN, and the body must equal its own canonical re-encoding;
  4. schema: exactly the known keys at every level, types, formats; weights ∈ registry ``allowed_weights`` ({0,1,2});
     market == registry mapping; engine hash consistent; optional pinned ``script_sha256`` per strategy;
  5. time: ``generated_at`` not in the future (5 min skew) and at most ``signal_max_age_hours`` (36 h) old; the bar is
     closed (``bar_close <= generated_at`` and ``<= now``) and ``as_of`` is at most ``max_bar_age_days`` (default 4)
     calendar days before today;
  6. continuity (optional, with the last accepted records): no older ``as_of`` than already accepted (replay), and the
     same ``as_of`` must carry the same weight (a changed history is a conflict, never silently re-traded).

What ``bar_close`` means for a daily bar
  The terminal labels a daily bar with the UTC calendar day D it belongs to (row ``t`` = D 00:00 UTC, as Hyperliquid's
  1d candles do) and treats it as final only once UTC day D has ended: its last completed bar at time ``now`` is
  ``floor(now/1d) − 1d`` (crest/gen_multi.js ``TO``). So ``bar_close = as_of + 1 day, 00:00 UTC`` — the first instant
  at which the terminal can have used that bar. For SILVER the COMEX session of day D actually settles earlier
  (~21:00–22:00 UTC on D), so this is conservative: it is never earlier than the data could be known. It is also the
  value stored in ``signals.bar_close`` (UNIQUE(strategy_version_id, bar_close, coin)).

Why the ``as_of`` window is in days, not 36 hours
  TradFi bars exist only on trading days: at Monday 00:30 UTC the newest SILVER bar is Friday's (bar_close Saturday
  00:00, 48.5 h old), after a Monday holiday it is 4 days old. The 36 h limit therefore applies to ``generated_at``
  (is the pipeline alive?) and the bar itself may be up to ``max_bar_age_days`` (default 4: weekend + one holiday)
  calendar days old. A daily run that fails keeps re-publishing nothing new; after 36 h the feed is stale.

Errors are typed (``SignalRejected`` subclasses of ``app.errors.AppError``) and each carries the alerts it should raise
(``alerts()`` → ``app.alerts.notifier.Alert`` objects; ``alert_payloads()`` → plain dicts for the ``alerts`` table).
Critical failures carry one alert per listed in-house market (``coin`` set), which the notifier's auto-pause hook turns
into "no new entries on that market" (SPEC §5.5); exits keep running.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.errors import AppError, ValidationFailed
from app.money import BPS
from app.strategies import registry

try:  # needed only to fetch; verification works without it (tests inject a session). Pinned in backend requirements.
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

_NET_ERRORS: tuple[type[BaseException], ...] = (requests.RequestException, OSError) if requests else (OSError,)

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_SIG_BYTES",
    "DEFAULT_MAX_BAR_AGE_DAYS",
    "SignalRecord",
    "SignalBatch",
    "IngestResult",
    "SignalRejected",
    "SignalConfigError",
    "SignalFetchError",
    "SignalTooLarge",
    "SignalSignatureInvalid",
    "SignalMalformed",
    "SignalUnknownStrategy",
    "SignalMarketMismatch",
    "SignalWeightInvalid",
    "SignalEngineMismatch",
    "SignalStatusNotTrading",
    "SignalMissingStrategy",
    "SignalStale",
    "SignalFromFuture",
    "SignalReplay",
    "SignalConflict",
    "canonical_json",
    "bar_close_for",
    "sig_url_for",
    "verify_and_parse",
    "fetch_signals",
    "ingest_signals",
]

MAX_BODY_BYTES = 1_000_000          # 1 MB cap on signals.json (SILVER-only file is ~350 bytes)
MAX_SIG_BYTES = 1024                # base64 of a 64-byte signature is 88 chars
DEFAULT_TIMEOUT = (5.0, 10.0)       # (connect, read) seconds
DEFAULT_MAX_BAR_AGE_DAYS = 4        # weekend + one holiday for TradFi daily bars (see module docstring)
CLOCK_SKEW = timedelta(minutes=5)
MAX_STRATEGIES = 64

TOP_KEYS = frozenset({"as_of", "engine_sha256", "generated_at", "strategies"})
STRATEGY_KEYS = frozenset({"last_action", "last_action_date", "market", "script_sha256", "status", "target_weight"})
LAST_ACTIONS = frozenset({"BUY", "SELL", "TRIM", "NONE"})
STATUSES = frozenset({"trades", "holds"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]*$")


# ---------------------------------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SignalRecord:
    """One target weight for one coin at one daily bar close (the ``signals`` table row, minus DB ids)."""

    strategy_key: str
    bar_close: datetime                 # as_of + 1 day, 00:00 UTC (see module docstring)
    coin: str                           # Hyperliquid coin, e.g. "xyz:SILVER"
    target_weight_bps: int              # weight × 10000 (0, 10000, 20000 for CREST)
    as_of: date | None = None           # the bar's UTC day
    generated_at: datetime | None = None
    last_action: str = "NONE"           # BUY | SELL | TRIM | NONE
    last_action_date: date | None = None
    status: str = "trades"
    script_sha256: str = ""
    engine_sha256: str = ""
    source: str = "terminal"

    @property
    def target_weight_x100(self) -> int:
        """SPEC §4 ``signals.target_weight_x100`` view (0, 100, 200)."""
        return self.target_weight_bps // 100


@dataclass(frozen=True)
class SignalBatch:
    """A verified feed: the records for the listed strategies plus what must be stored alongside them."""

    as_of: date
    generated_at: datetime
    engine_sha256: str
    records: tuple[SignalRecord, ...]           # listed strategies only, sorted by key
    all_records: tuple[SignalRecord, ...]       # every strategy in the feed (validated), sorted by key
    raw: bytes                                  # exact signed bytes (store in signals.raw as parsed JSON)
    signature_b64: str                          # store in signals.signature
    body_sha256: str

    def payload(self) -> dict[str, Any]:
        return json.loads(self.raw)


@dataclass(frozen=True)
class IngestResult:
    ok: bool
    batch: SignalBatch | None = None
    error: "SignalRejected | None" = None

    def alert_payloads(self) -> list[dict[str, Any]]:
        return self.error.alert_payloads() if self.error else []


# ---------------------------------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------------------------------
class SignalRejected(AppError):
    """Base: the feed was not accepted. Nothing from it may be stored or traded."""

    http_status, code = 502, "signal_rejected"
    reason = "rejected"
    severity = "critical"               # info | warn | critical (alerts.severity)

    def __init__(self, message: str = "", *, markets: Sequence[str] = (), strategy_key: str | None = None, **details: Any):
        super().__init__(message, **details)
        self.markets = tuple(markets)
        self.strategy_key = strategy_key

    @property
    def kind(self) -> str:
        return f"signals_{self.reason}"

    def alert_payloads(self) -> list[dict[str, Any]]:
        """Plain dicts shaped like ``app.alerts.notifier.Alert`` (kind, severity, user_id, coin, data, key).

        Critical errors produce one ops alert per affected market (coin set → notifier auto-pauses new entries there);
        others one ops alert without a coin.
        """
        data = {"reason": self.reason, "message": self.message[:500], **_safe_details(self.details)}
        if self.strategy_key:
            data["strategy_key"] = self.strategy_key
        coins: Iterable[str | None] = self.markets if (self.severity == "critical" and self.markets) else (None,)
        return [
            {"kind": self.kind, "severity": self.severity, "user_id": None, "coin": c, "data": dict(data),
             "key": f"{self.kind}:{c or '-'}"}
            for c in coins
        ]

    def alerts(self) -> list[Any]:
        """``app.alerts.notifier.Alert`` objects (imported lazily so this module has no hard dependency on it)."""
        from app.alerts.notifier import Alert, Severity  # noqa: PLC0415

        return [Alert(kind=p["kind"], severity=Severity(p["severity"]), user_id=None, coin=p["coin"], data=p["data"],
                      key=p["key"]) for p in self.alert_payloads()]


class SignalConfigError(SignalRejected):
    reason = "config"                   # no / malformed pinned public key, bad URL


class SignalFetchError(SignalRejected):
    http_status, code = 502, "external_service_error"
    reason = "fetch_failed"
    severity = "warn"                   # transient; staleness turns it critical after 36 h


class SignalTooLarge(SignalRejected):
    reason = "too_large"


class SignalSignatureInvalid(SignalRejected):
    reason = "signature_invalid"


class SignalMalformed(SignalRejected):
    reason = "malformed"


class SignalUnknownStrategy(SignalRejected):
    reason = "unknown_strategy"


class SignalMarketMismatch(SignalRejected):
    reason = "market_mismatch"


class SignalWeightInvalid(SignalRejected):
    reason = "weight_invalid"


class SignalEngineMismatch(SignalRejected):
    reason = "engine_mismatch"          # script hash differs from the pinned strategy version


class SignalStatusNotTrading(SignalRejected):
    reason = "not_trading"              # a listed strategy's script now "holds" (owner must decide: delist / re-list)


class SignalMissingStrategy(SignalRejected):
    reason = "missing_strategy"


class SignalStale(SignalRejected):
    reason = "stale"


class SignalFromFuture(SignalRejected):
    reason = "from_future"


class SignalReplay(SignalRejected):
    reason = "replay"


class SignalConflict(SignalRejected):
    reason = "conflict"


def _safe_details(details: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in details.items():
        if isinstance(v, (str, int, bool)) or v is None:
            out[k] = v if not isinstance(v, str) else v[:200]
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x)[:100] for x in list(v)[:20]]
        else:
            out[k] = str(v)[:200]
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------------
def canonical_json(obj: Any) -> bytes:
    """Canonical bytes (must equal signals/lib.js ``canonicalJson``): sorted keys, no whitespace, integers only."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def bar_close_for(as_of: date) -> datetime:
    """Daily bar dated ``as_of`` (UTC day) closes at the next UTC midnight."""
    return datetime.combine(as_of + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)


def sig_url_for(url: str) -> str:
    return url[: -len(".json")] + ".sig" if url.endswith(".json") else url + ".sig"


def _pubkey(pubkey_b64: str) -> Ed25519PublicKey:
    if not pubkey_b64:
        raise SignalConfigError("no pinned signal public key (SIGNALS_PUBKEY_B64)")
    try:
        raw = base64.b64decode(pubkey_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SignalConfigError("pinned signal public key is not valid base64") from None
    if len(raw) != 32:
        raise SignalConfigError("pinned signal public key must be 32 raw bytes", length=len(raw))
    return Ed25519PublicKey.from_public_bytes(raw)


def _signature(sig_text: bytes | str) -> tuple[bytes, str]:
    if isinstance(sig_text, str):
        sig_text = sig_text.encode("ascii", "replace")
    if len(sig_text) > MAX_SIG_BYTES:
        raise SignalTooLarge("signature file too large", size=len(sig_text))
    s = sig_text.strip()
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise SignalSignatureInvalid("signature is not valid base64") from None
    if len(raw) != 64:
        raise SignalSignatureInvalid("signature must be 64 bytes", length=len(raw))
    return raw, s.decode("ascii")


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise SignalMalformed("duplicate key in signals.json", key=k)
        out[k] = v
    return out


def _reject_float(s: str) -> Any:
    raise SignalMalformed("non-integer number in signals.json", value=s[:40])


def _reject_constant(s: str) -> Any:
    raise SignalMalformed("NaN/Infinity in signals.json", value=s)


def _parse_date(v: Any, what: str) -> date:
    if not isinstance(v, str) or not _DATE_RE.match(v):
        raise SignalMalformed(f"{what} must be YYYY-MM-DD", field=what)
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise SignalMalformed(f"{what} is not a calendar date", field=what) from None


def _parse_ts(v: Any, what: str) -> datetime:
    if not isinstance(v, str) or not _TS_RE.match(v):
        raise SignalMalformed(f"{what} must be YYYY-MM-DDTHH:MM:SSZ", field=what)
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SignalMalformed(f"{what} is not a valid time", field=what) from None


def _exact_keys(obj: Any, keys: frozenset[str], what: str) -> None:
    if not isinstance(obj, dict):
        raise SignalMalformed(f"{what} must be an object")
    got = set(obj)
    if got != keys:
        raise SignalMalformed(f"{what} keys differ from the schema", field=what,
                              unknown=sorted(got - keys), missing=sorted(keys - got))


def _listed_markets(listed_keys: Iterable[str]) -> tuple[str, ...]:
    return tuple(m for s in registry.listed(listed_keys) for m in s.markets)


# ---------------------------------------------------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------------------------------------------------
def verify_and_parse(
    body: bytes,
    sig_text: bytes | str,
    *,
    pubkey_b64: str,
    now: datetime,
    listed_keys: Iterable[str] = ("silver",),
    max_age_hours: int = 36,
    max_bar_age_days: int = DEFAULT_MAX_BAR_AGE_DAYS,
    expected_script_sha256: Mapping[str, str] | None = None,
    last_accepted: Mapping[str, SignalRecord] | None = None,
) -> SignalBatch:
    """Authenticate and strictly validate one feed. Raises a ``SignalRejected`` subclass on any mismatch."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    now = now.astimezone(timezone.utc)
    listed = tuple(sorted({k.strip().lower() for k in listed_keys if k.strip()}))
    markets = _listed_markets(listed)

    def fail(cls: type[SignalRejected], msg: str, **kw: Any) -> SignalRejected:
        return cls(msg, markets=kw.pop("markets", markets), **kw)

    if len(body) > MAX_BODY_BYTES:
        raise fail(SignalTooLarge, "signals.json too large", size=len(body))
    try:
        key = _pubkey(pubkey_b64)
        sig, sig_b64 = _signature(sig_text)
    except SignalRejected as e:                             # re-raise with the listed markets (alerts pause them)
        raise fail(type(e), e.message, **e.details) from None
    try:
        key.verify(sig, body)                               # 1. authenticate the exact bytes before parsing
    except InvalidSignature:
        raise fail(SignalSignatureInvalid, "Ed25519 signature does not verify against the pinned public key",
                   body_sha256=hashlib.sha256(body).hexdigest()) from None

    try:                                                    # 2. strict parse
        text = body.decode("utf-8")
        obj = json.loads(text, object_pairs_hook=_no_dupes, parse_float=_reject_float, parse_constant=_reject_constant)
    except SignalRejected as e:
        raise fail(type(e), e.message, **e.details) from None
    except (UnicodeDecodeError, ValueError) as e:
        raise fail(SignalMalformed, "signals.json is not valid UTF-8 JSON", error=str(e)[:120]) from None
    try:
        canon = canonical_json(obj)
    except (TypeError, ValueError):
        raise fail(SignalMalformed, "signals.json cannot be canonicalised") from None
    if canon != body or not _PRINTABLE_ASCII_RE.match(text):
        raise fail(SignalMalformed, "signals.json is not in canonical form")

    _exact_keys(obj, TOP_KEYS, "top level")                 # 3. schema
    as_of = _parse_date(obj["as_of"], "as_of")
    generated_at = _parse_ts(obj["generated_at"], "generated_at")
    engine = obj["engine_sha256"]
    if not isinstance(engine, str) or not _HEX64_RE.match(engine):
        raise fail(SignalMalformed, "engine_sha256 must be 64 lower-case hex", field="engine_sha256")
    strategies = obj["strategies"]
    if not isinstance(strategies, dict) or not strategies or len(strategies) > MAX_STRATEGIES:
        raise fail(SignalMalformed, "strategies must be a non-empty object", field="strategies")

    bar_close = bar_close_for(as_of)
    records: list[SignalRecord] = []
    hashes: dict[str, str] = {}
    for skey in sorted(strategies):
        entry = strategies[skey]
        if not _KEY_RE.match(skey):
            raise fail(SignalMalformed, "bad strategy key", strategy_key=skey[:32])
        try:
            spec = registry.get(skey)
        except ValidationFailed:
            raise fail(SignalUnknownStrategy, "strategy not in the in-house registry", strategy_key=skey) from None
        _exact_keys(entry, STRATEGY_KEYS, f"strategies.{skey}")
        w = entry["target_weight"]
        if type(w) is not int or w not in spec.allowed_weights or (spec.long_only and w < 0) or w > spec.max_weight():
            raise fail(SignalWeightInvalid, "target_weight not allowed", strategy_key=skey, target_weight=repr(w)[:20],
                       allowed=list(spec.allowed_weights))
        if not spec.markets or len(spec.markets) != 1:
            raise fail(SignalMarketMismatch, "strategy has no single-market mapping in the registry", strategy_key=skey)
        if entry["market"] != spec.markets[0]:
            raise fail(SignalMarketMismatch, "signal market does not match the registry", strategy_key=skey,
                       feed_market=str(entry["market"])[:40], expected=spec.markets[0])
        la = entry["last_action"]
        if la not in LAST_ACTIONS:
            raise fail(SignalMalformed, "last_action not allowed", strategy_key=skey, field="last_action")
        lad = entry["last_action_date"]
        if la == "NONE":
            if lad is not None:
                raise fail(SignalMalformed, "last_action_date must be null when last_action is NONE", strategy_key=skey)
            lad_d = None
        else:
            lad_d = _parse_date(lad, "last_action_date")
            if lad_d > as_of:
                raise fail(SignalMalformed, "last_action_date after as_of", strategy_key=skey)
        # position consistency: LONG ends on BUY/TRIM, CASH on SELL/NONE
        if (w > 0) != (la in ("BUY", "TRIM")):
            raise fail(SignalMalformed, "target_weight inconsistent with last_action", strategy_key=skey,
                       target_weight=w, last_action=la)
        status = entry["status"]
        if status not in STATUSES:
            raise fail(SignalMalformed, "status not allowed", strategy_key=skey, field="status")
        sh = entry["script_sha256"]
        if not isinstance(sh, str) or not _HEX64_RE.match(sh):
            raise fail(SignalMalformed, "script_sha256 must be 64 lower-case hex", strategy_key=skey)
        hashes[skey] = sh
        records.append(SignalRecord(
            strategy_key=skey, bar_close=bar_close, coin=spec.markets[0], target_weight_bps=w * BPS, as_of=as_of,
            generated_at=generated_at, last_action=la, last_action_date=lad_d, status=status, script_sha256=sh,
            engine_sha256=engine,
        ))
    if hashlib.sha256(canonical_json(hashes)).hexdigest() != engine:
        raise fail(SignalEngineMismatch, "engine_sha256 does not match the per-strategy script hashes")

    by_key = {r.strategy_key: r for r in records}
    for lk in listed:                                       # every listed strategy must be present and trading
        r = by_key.get(lk)
        lk_markets = registry.get(lk).markets
        if r is None:
            raise fail(SignalMissingStrategy, "listed strategy missing from the feed", strategy_key=lk, markets=lk_markets)
        if r.status != "trades":
            raise fail(SignalStatusNotTrading, "listed strategy's script no longer trades (holds)", strategy_key=lk,
                       markets=lk_markets)
        want = (expected_script_sha256 or {}).get(lk)
        if want is not None and want != r.script_sha256:
            raise fail(SignalEngineMismatch, "script hash differs from the pinned strategy version", strategy_key=lk,
                       expected=want, got=r.script_sha256, markets=lk_markets)

    # 4. time
    if generated_at > now + CLOCK_SKEW:
        raise fail(SignalFromFuture, "generated_at is in the future", generated_at=obj["generated_at"])
    if now - generated_at > timedelta(hours=max_age_hours):
        raise fail(SignalStale, f"feed older than {max_age_hours} h", generated_at=obj["generated_at"])
    if bar_close > generated_at or bar_close > now + CLOCK_SKEW:
        raise fail(SignalFromFuture, "as_of bar was not closed when the feed was generated", as_of=obj["as_of"])
    if (now.date() - as_of).days > max_bar_age_days:
        raise fail(SignalStale, f"as_of more than {max_bar_age_days} days old", as_of=obj["as_of"])

    # 5. continuity against what was accepted before
    for r in records:
        prev = (last_accepted or {}).get(r.strategy_key)
        if prev is None or prev.as_of is None:
            continue
        sm = registry.get(r.strategy_key).markets
        if r.as_of < prev.as_of:
            raise fail(SignalReplay, "feed is older than the last accepted signal", strategy_key=r.strategy_key,
                       as_of=obj["as_of"], last_as_of=prev.as_of.isoformat(), markets=sm)
        if r.as_of == prev.as_of and (r.target_weight_bps != prev.target_weight_bps or r.coin != prev.coin):
            raise fail(SignalConflict, "same bar re-published with a different weight (history rebuilt?)",
                       strategy_key=r.strategy_key, as_of=obj["as_of"], was=prev.target_weight_bps,
                       now=r.target_weight_bps, markets=sm)

    all_records = tuple(records)
    return SignalBatch(
        as_of=as_of, generated_at=generated_at, engine_sha256=engine,
        records=tuple(r for r in all_records if r.strategy_key in listed), all_records=all_records,
        raw=body, signature_b64=sig_b64, body_sha256=hashlib.sha256(body).hexdigest(),
    )


# ---------------------------------------------------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------------------------------------------------
def _get_capped(session: Any, url: str, cap: int, timeout: Any, markets: Sequence[str]) -> bytes:
    try:
        resp = session.get(url, timeout=timeout, stream=True, allow_redirects=False,
                           headers={"Accept": "application/json, text/plain", "Cache-Control": "no-cache"})
    except _NET_ERRORS as e:
        raise SignalFetchError("signal fetch failed", url=url, error=type(e).__name__, markets=markets) from None
    try:
        if resp.status_code != 200:
            raise SignalFetchError("signal fetch returned non-200", url=url, status=resp.status_code, markets=markets)
        cl = resp.headers.get("Content-Length")
        if cl is not None and cl.isdigit() and int(cl) > cap:
            raise SignalTooLarge("signal file larger than the cap", url=url, size=int(cl), cap=cap, markets=markets)
        buf = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    buf += chunk
                    if len(buf) > cap:
                        raise SignalTooLarge("signal file larger than the cap", url=url, cap=cap, markets=markets)
        except _NET_ERRORS as e:
            raise SignalFetchError("signal fetch failed while reading", url=url, error=type(e).__name__,
                                   markets=markets) from None
        return bytes(buf)
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()


def fetch_signals(
    url: str | None = None,
    *,
    sig_url: str | None = None,
    session: Any = None,
    now: datetime | None = None,
    pubkey_b64: str | None = None,
    listed_keys: Iterable[str] | None = None,
    max_age_hours: int | None = None,
    max_bar_age_days: int = DEFAULT_MAX_BAR_AGE_DAYS,
    expected_script_sha256: Mapping[str, str] | None = None,
    last_accepted: Mapping[str, SignalRecord] | None = None,
    timeout: Any = DEFAULT_TIMEOUT,
    allow_http: bool = False,
) -> SignalBatch:
    """Fetch ``signals.json`` + ``signals.sig`` (HTTPS, no redirects, timeout, 1 MB cap) and ``verify_and_parse`` them.

    Unset arguments come from ``app.config.get_settings()`` (``signals_url``, ``signals_pubkey_b64``,
    ``in_house_listed``, ``risk.signal_max_age_hours``).
    """
    if url is None or pubkey_b64 is None or listed_keys is None or max_age_hours is None:
        from app.config import get_settings  # noqa: PLC0415 — only when not injected

        st = get_settings()
        url = st.signals_url if url is None else url
        pubkey_b64 = st.signals_pubkey_b64 if pubkey_b64 is None else pubkey_b64
        listed_keys = st.in_house_listed if listed_keys is None else listed_keys
        max_age_hours = st.risk.signal_max_age_hours if max_age_hours is None else max_age_hours
    listed_keys = tuple(listed_keys)
    markets = _listed_markets(listed_keys)
    sig_url = sig_url or sig_url_for(url)
    for u in (url, sig_url):
        if not (u.startswith("https://") or (allow_http and u.startswith("http://"))):
            raise SignalConfigError("signal URLs must be https://", url=u, markets=markets)
    try:
        _pubkey(pubkey_b64 or "")                           # config errors before any network I/O
    except SignalConfigError as e:
        raise SignalConfigError(e.message, markets=markets, **e.details) from None
    if session is None and requests is None:
        raise SignalConfigError("the requests package is not installed", markets=markets)
    sess = session if session is not None else requests.Session()
    try:
        body = _get_capped(sess, url, MAX_BODY_BYTES, timeout, markets)
        sig = _get_capped(sess, sig_url, MAX_SIG_BYTES, timeout, markets)
    finally:
        if session is None:
            sess.close()
    return verify_and_parse(
        body, sig, pubkey_b64=pubkey_b64 or "", now=now or datetime.now(timezone.utc), listed_keys=listed_keys,
        max_age_hours=max_age_hours, max_bar_age_days=max_bar_age_days,
        expected_script_sha256=expected_script_sha256, last_accepted=last_accepted,
    )


def ingest_signals(url: str | None = None, **kwargs: Any) -> IngestResult:
    """``fetch_signals`` that never raises ``SignalRejected``: returns the batch, or the error with its alerts.

    The caller (``/internal/ingest-signals``) stores ``batch.records`` idempotently (UNIQUE(version, bar_close, coin))
    and sends ``result.error.alerts()`` through the notifier. Nothing is stored on error.
    """
    try:
        return IngestResult(ok=True, batch=fetch_signals(url, **kwargs))
    except SignalRejected as e:
        return IngestResult(ok=False, error=e)

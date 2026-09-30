"""Thin read-only client for Hyperliquid's ``POST {base}/info`` endpoint (SPEC §6).

* Never talks to ``/exchange`` — order placement lives in ``app.hl.client`` (official SDK, agent key).
* Timeouts on every request; retries with full-jitter exponential backoff on 429 / 5xx / connection errors
  (``Retry-After`` honoured, capped); responses are streamed and refused above ``max_response_bytes``.
* ``rate_hook`` (optional, ``app.hl.budget.BudgetHook``): ``before(body)`` is called before EVERY HTTP attempt
  (it charges the shared per-IP weight budget and may raise ``HlBudgetExhausted`` for non-priority callers) and
  ``after(body, parsed)`` once a response is parsed (per-item extra weight).
* Addresses are validated and lower-cased before they leave the process. Hyperliquid numeric strings are returned
  untouched (callers parse them with ``app.money.parse_decimal`` — never float).

Response shapes below were VERIFIED against mainnet on 2026-09-30 (fixtures in ``tests/fixtures/hl``), except
where marked UNVERIFIED.
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Callable, Iterator, Mapping

from app.errors import ExternalServiceError, ValidationFailed
from app.logging import get_logger

__all__ = ["InfoClient", "MAINNET_API_URL", "TESTNET_API_URL", "normalize_address", "CANDLE_INTERVALS"]

MAINNET_API_URL = "https://api.hyperliquid.xyz"
TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
CANDLE_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M")

_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
_DEX_RE = re.compile(r"^[a-z0-9]{0,16}$")
_COIN_RE = re.compile(r"^[A-Za-z0-9@:/_.\-]{1,64}$")

log = get_logger("app.hl.info")


def normalize_address(value: Any, what: str = "address") -> str:
    s = str(value or "").strip().lower()
    if not _ADDR_RE.fullmatch(s):
        raise ValidationFailed(f"bad {what}")
    return s


def _dex(dex: str) -> str:
    if not isinstance(dex, str) or not _DEX_RE.fullmatch(dex):
        raise ValidationFailed("bad dex name")
    return dex


def _coin(coin: str) -> str:
    if not isinstance(coin, str) or not _COIN_RE.fullmatch(coin):
        raise ValidationFailed("bad coin")
    return coin


def _ms(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"bad {what} (integer ms expected)")
    return value


class InfoClient:
    """``InfoClient(base_url).meta()`` etc. Thread-safe as long as the injected session is."""

    def __init__(
        self,
        base_url: str = MAINNET_API_URL,
        *,
        timeout: float = 10.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        max_response_bytes: int = 8 * 1024 * 1024,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        rate_hook: Any = None,
    ) -> None:
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ValidationFailed("Hyperliquid base_url must be https")
        self.url = base_url.rstrip("/") + "/info"
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_response_bytes = max_response_bytes
        if session is None:
            import requests  # local import: keeps the module importable where requests is absent

            session = requests.Session()
        self._session = session
        self._sleep = sleep
        self._rng = rng
        self.rate_hook = rate_hook

    # ------------------------------------------------------------------------------------------------ transport

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        delay = self._rng() * min(self.backoff_cap, self.backoff_base * (2 ** attempt))  # full jitter
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), self.backoff_cap * 4))
            except ValueError:
                pass
        return delay

    def _read_capped(self, resp: Any) -> bytes:
        length = resp.headers.get("Content-Length") if getattr(resp, "headers", None) else None
        if length is not None:
            try:
                if int(length) > self.max_response_bytes:
                    raise ExternalServiceError("hyperliquid response too large", size=int(length))
            except ValueError:
                pass
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                buf.extend(chunk)
                if len(buf) > self.max_response_bytes:
                    raise ExternalServiceError("hyperliquid response too large", size=len(buf))
        return bytes(buf)

    def post(self, body: Mapping[str, Any]) -> Any:
        """POST one info request and return the parsed JSON. Raises ExternalServiceError when exhausted."""
        req_type = body.get("type")
        last_status: int | None = None
        last_err: str = ""
        for attempt in range(self.max_retries + 1):
            resp = None
            if self.rate_hook is not None:
                self.rate_hook.before(body)          # outside the try: a budget refusal is not retried here
            try:
                resp = self._session.post(self.url, json=dict(body), timeout=self.timeout, stream=True,
                                          headers={"Content-Type": "application/json"})
                status = int(resp.status_code)
                if status == 429 or status >= 500:
                    last_status, last_err = status, f"http {status}"
                    retry_after = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
                    if attempt < self.max_retries:
                        delay = self._backoff(attempt, retry_after)
                        log.info("hl_info_retry", extra={"fields": {"type": req_type, "status": status,
                                                                    "attempt": attempt, "delay": round(delay, 3)}})
                        self._sleep(delay)
                        continue
                    break
                raw = self._read_capped(resp)
                if status != 200:
                    raise ExternalServiceError(f"hyperliquid info http {status}", upstream_status=status,
                                               type=req_type, body=raw[:200].decode("utf-8", "replace"))
                try:
                    parsed = json.loads(raw)
                except ValueError as e:
                    raise ExternalServiceError("hyperliquid info returned invalid JSON", type=req_type) from e
                if self.rate_hook is not None:
                    try:
                        self.rate_hook.after(body, parsed)
                    except Exception:  # noqa: BLE001 - accounting never fails a successful read
                        log.warning("hl_rate_hook_after_failed", exc_info=True)
                return parsed
            except ExternalServiceError:
                raise
            except Exception as e:  # noqa: BLE001 - network errors (requests.ConnectionError, Timeout, ...)
                last_status, last_err = None, type(e).__name__
                if attempt < self.max_retries:
                    self._sleep(self._backoff(attempt, None))
                    continue
                break
            finally:
                if resp is not None and hasattr(resp, "close"):
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass
        raise ExternalServiceError("hyperliquid info unavailable", upstream_status=last_status, error=last_err,
                                   type=req_type)

    # ----------------------------------------------------------------------------------------- market metadata

    def perp_dexs(self) -> list[dict | None]:
        """``[null, {"name": "xyz", "fullName", "deployer", ...}, ...]`` — index 0 is the validator-perp dex."""
        out = self.post({"type": "perpDexs"})
        if not isinstance(out, list) or not out or out[0] is not None:
            raise ExternalServiceError("unexpected perpDexs shape (index 0 must be null)")
        return out

    def meta(self, dex: str = "") -> dict:
        """``{"universe": [{"name", "szDecimals", "maxLeverage", "marginTableId", "onlyIsolated"?, "isDelisted"?,
        "marginMode"?, ...}], "marginTables": [...], "collateralToken": int}``."""
        body: dict[str, Any] = {"type": "meta"}
        if _dex(dex):
            body["dex"] = dex
        out = self.post(body)
        if not isinstance(out, dict) or not isinstance(out.get("universe"), list):
            raise ExternalServiceError("unexpected meta shape", dex=dex)
        return out

    def meta_and_asset_ctxs(self, dex: str = "") -> tuple[dict, list[dict]]:
        """``[meta, [ctx per universe index]]``; ctx = {markPx, oraclePx, midPx|null, dayNtlVlm, openInterest (coin
        units), funding, premium|null, prevDayPx, impactPxs|null, dayBaseVlm}."""
        body: dict[str, Any] = {"type": "metaAndAssetCtxs"}
        if _dex(dex):
            body["dex"] = dex
        out = self.post(body)
        if (not isinstance(out, list) or len(out) != 2 or not isinstance(out[0], dict)
                or not isinstance(out[1], list) or len(out[0].get("universe") or []) != len(out[1])):
            raise ExternalServiceError("unexpected metaAndAssetCtxs shape", dex=dex)
        return out[0], out[1]

    def candle_snapshot(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        """``[{"t","T","s","i","o","c","h","l","v","n"}]`` oldest first. Builder-dex coins use ``dex:COIN``."""
        if interval not in CANDLE_INTERVALS:
            raise ValidationFailed("bad candle interval")
        out = self.post({"type": "candleSnapshot", "req": {"coin": _coin(coin), "interval": interval,
                                                           "startTime": _ms(start_ms, "start"),
                                                           "endTime": _ms(end_ms, "end")}})
        if not isinstance(out, list):
            raise ExternalServiceError("unexpected candleSnapshot shape")
        return out

    def l2_book(self, coin: str) -> dict:
        """``{"coin", "time", "levels": [bids, asks]}``, each level ``{"px","sz","n"}`` (20 per side)."""
        out = self.post({"type": "l2Book", "coin": _coin(coin)})
        if not isinstance(out, dict) or not isinstance(out.get("levels"), list) or len(out["levels"]) != 2:
            raise ExternalServiceError("unexpected l2Book shape")
        return out

    # ------------------------------------------------------------------------------------------- user history

    def user_fills(self, user: str) -> list[dict]:
        """Most recent fills (≤2000), NEWEST FIRST. Prefer ``user_fills_by_time`` for scanning."""
        return self._list({"type": "userFills", "user": normalize_address(user, "user")}, "userFills")

    def user_fills_by_time(self, user: str, start_ms: int, end_ms: int | None = None, *,
                           aggregate_by_time: bool = False) -> list[dict]:
        """Fills in [start, end], OLDEST FIRST, ≤2000 per call. Fill fields: coin, px, sz, side (B/A), time,
        startPosition, dir, closedPnl, hash, oid, crossed, fee, builderFee? , tid, cloid?, feeToken, twapId,
        liquidation?."""
        body: dict[str, Any] = {"type": "userFillsByTime", "user": normalize_address(user, "user"),
                                "startTime": _ms(start_ms, "start"), "aggregateByTime": bool(aggregate_by_time)}
        if end_ms is not None:
            body["endTime"] = _ms(end_ms, "end")
        return self._list(body, "userFillsByTime")

    def user_funding(self, user: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """``[{"time","hash"(zero),"delta":{"type":"funding","coin","usdc","szi","fundingRate","nSamples"}}]``,
        oldest first. ``usdc`` > 0 = received by the user. Entries older than ~8 days come back as DAILY aggregates
        (time = UTC midnight, ``nSamples`` = payments merged, szi/fundingRate averaged); recent ones are hourly
        (``nSamples`` null) — scan at least daily."""
        body: dict[str, Any] = {"type": "userFunding", "user": normalize_address(user, "user"),
                                "startTime": _ms(start_ms, "start")}
        if end_ms is not None:
            body["endTime"] = _ms(end_ms, "end")
        return self._list(body, "userFunding")

    def user_non_funding_ledger_updates(self, user: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """``[{"time","hash","delta":{"type": send|spotTransfer|internalTransfer|deposit|withdraw|...}}]``."""
        body: dict[str, Any] = {"type": "userNonFundingLedgerUpdates", "user": normalize_address(user, "user"),
                                "startTime": _ms(start_ms, "start")}
        if end_ms is not None:
            body["endTime"] = _ms(end_ms, "end")
        return self._list(body, "userNonFundingLedgerUpdates")

    def historical_orders(self, user: str) -> list[dict]:
        return self._list({"type": "historicalOrders", "user": normalize_address(user, "user")}, "historicalOrders")

    # -------------------------------------------------------------------------------------------- user state

    def clearinghouse_state(self, user: str, dex: str = "") -> dict:
        """Perp account state for one dex (builder-dex positions are only returned with ``dex=<name>``; coins are
        named ``dex:COIN`` there)."""
        body: dict[str, Any] = {"type": "clearinghouseState", "user": normalize_address(user, "user")}
        if _dex(dex):
            body["dex"] = dex
        out = self.post(body)
        if not isinstance(out, dict) or not isinstance(out.get("assetPositions"), list):
            raise ExternalServiceError("unexpected clearinghouseState shape")
        return out

    def extra_agents(self, user: str) -> list[dict]:
        """Approved agents of a MASTER account: ``[{"name", "address", "validUntil" (ms)}]`` (VERIFIED; the
        request type is ``extraAgents``). Sub-accounts return ``[]`` — agents belong to the master."""
        out = self._list({"type": "extraAgents", "user": normalize_address(user, "user")}, "extraAgents")
        for a in out:
            if not isinstance(a, dict) or "address" not in a:
                raise ExternalServiceError("unexpected extraAgents entry")
        return out

    def max_builder_fee(self, user: str, builder: str) -> int:
        """Approved max builder fee in TENTHS OF A BASIS POINT (bare integer; 0 = not approved). VERIFIED shape;
        unit per SDK/docs (UNVERIFIED on a non-zero live value from this environment)."""
        out = self.post({"type": "maxBuilderFee", "user": normalize_address(user, "user"),
                         "builder": normalize_address(builder, "builder")})
        if isinstance(out, bool) or not isinstance(out, int) or out < 0:
            raise ExternalServiceError("unexpected maxBuilderFee shape")
        return out

    def user_role(self, user: str) -> dict:
        """``{"role":"user"}`` | ``{"role":"agent","data":{"user":master}}`` |
        ``{"role":"subAccount","data":{"master":...}}`` | ``{"role":"vault"}``. Unknown addresses return "user"."""
        out = self.post({"type": "userRole", "user": normalize_address(user, "user")})
        if not isinstance(out, dict) or "role" not in out:
            raise ExternalServiceError("unexpected userRole shape")
        return out

    def sub_accounts(self, user: str) -> list[dict]:
        """``[{"name","subAccountUser","master","clearinghouseState","spotState"}]`` (``null`` → [])."""
        out = self.post({"type": "subAccounts", "user": normalize_address(user, "user")})
        return [] if out is None else self._check_list(out, "subAccounts")

    def frontend_open_orders(self, user: str, dex: str = "") -> list[dict]:
        body: dict[str, Any] = {"type": "frontendOpenOrders", "user": normalize_address(user, "user")}
        if _dex(dex):
            body["dex"] = dex  # UNVERIFIED: dex param on open-order queries (mirrors clearinghouseState)
        return self._list(body, "frontendOpenOrders")

    def order_status(self, user: str, oid_or_cloid: int | str) -> dict:
        """``{"status":"order","order":{"order":{...,"origSz","sz"(remaining),"cloid"?},"status":...,
        "statusTimestamp"}}`` or ``{"status":"unknownOid"}``. Accepts a numeric oid or a 0x cloid (VERIFIED)."""
        if isinstance(oid_or_cloid, str):
            oid: int | str = oid_or_cloid.lower()
            if not _CLOID_RE.fullmatch(oid):
                raise ValidationFailed("bad cloid")
        else:
            oid = _ms(oid_or_cloid, "oid")
        out = self.post({"type": "orderStatus", "user": normalize_address(user, "user"), "oid": oid})
        if not isinstance(out, dict) or "status" not in out:
            raise ExternalServiceError("unexpected orderStatus shape")
        return out

    # ------------------------------------------------------------------------------------------- pagination

    def iter_user_fills_by_time(self, user: str, start_ms: int, end_ms: int, *, max_pages: int = 50) -> Iterator[dict]:
        yield from self._paginate(lambda s: self.user_fills_by_time(user, s, end_ms), start_ms,
                                  key=lambda x: x.get("tid"), max_pages=max_pages)

    def iter_user_funding(self, user: str, start_ms: int, end_ms: int, *, max_pages: int = 50) -> Iterator[dict]:
        yield from self._paginate(lambda s: self.user_funding(user, s, end_ms), start_ms,
                                  key=lambda x: (x.get("time"), (x.get("delta") or {}).get("coin")),
                                  max_pages=max_pages)

    def iter_user_non_funding_ledger_updates(self, user: str, start_ms: int, end_ms: int, *,
                                             max_pages: int = 50) -> Iterator[dict]:
        yield from self._paginate(lambda s: self.user_non_funding_ledger_updates(user, s, end_ms), start_ms,
                                  key=lambda x: (x.get("hash"), x.get("time"), json.dumps(x.get("delta"),
                                                                                         sort_keys=True)),
                                  max_pages=max_pages)

    @staticmethod
    def _paginate(fetch: Callable[[int], list[dict]], start_ms: int, *, key: Callable[[dict], Any],
                  max_pages: int) -> Iterator[dict]:
        """Time-cursor pagination for the oldest-first endpoints (page sizes are not documented here, so we keep
        going until a page adds nothing new). The cursor restarts AT the last time seen (inclusive) because
        several events can share one millisecond; duplicates are dropped by ``key``."""
        seen: set[Any] = set()
        cursor = start_ms
        for _ in range(max_pages):
            page = fetch(cursor)
            new = 0
            last_time = cursor
            for item in page:
                k = key(item)
                t = item.get("time")
                if isinstance(t, int):
                    last_time = max(last_time, t)
                if k in seen:
                    continue
                seen.add(k)
                new += 1
                yield item
            if new == 0 or not page:
                return
            cursor = last_time
        raise ExternalServiceError("pagination did not converge", pages=max_pages)

    # ---------------------------------------------------------------------------------------------- helpers

    def _list(self, body: Mapping[str, Any], what: str) -> list[dict]:
        return self._check_list(self.post(body), what)

    @staticmethod
    def _check_list(out: Any, what: str) -> list:
        if not isinstance(out, list):
            raise ExternalServiceError(f"unexpected {what} shape")
        return out


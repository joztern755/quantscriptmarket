"""Order placement through the official ``hyperliquid-python-sdk`` with the user's agent key (SPEC §5.3, §6).

Invariants
- EVERY order carries our builder code ``{"b": builder, "f": fee_tenths_bp}``. The builder is bound when the
  gateway is constructed (``BuilderCode`` refuses an empty address or a fee outside 1..100) and ``place_ioc`` has
  no parameter that could drop it. There is no other order path in this module.
- Only IOC limit orders. Sizes/prices must already be on the Hyperliquid grid (``app.hl.markets``); the gateway
  re-checks and refuses off-grid values instead of letting the SDK round them.
- Every cloid must carry our platform prefix (``CLOID_PREFIX``) so fills can be attributed (SPEC §1.1).

Sub-accounts (SDK semantics, recalled from ``hyperliquid/exchange.py``; the SDK is not installable here):
``Exchange(wallet=<agent LocalAccount>, base_url, meta, vault_address=<sub-account or None>,
account_address=<master>)``. The agent is approved by the MASTER; to trade a sub-account the signed L1 action
carries ``vaultAddress = sub-account`` (the SDK puts ``vault_address`` into both the signature's active pool and the
POST body). ``account_address`` is only used by the SDK's info helpers (e.g. market_close). A sub-account itself
has no agents (VERIFIED: ``extraAgents`` of sub-accounts is ``[]``).

Cloid: ``0x`` + ``CLOID_PREFIX`` (4 bytes, ``a17a1000`` — same constant as ``app.execution.executor``) + first 12
bytes of ``HMAC-SHA256(key=CLOID_SECRET, msg="aijalon/cloid/v1|{subscription_id}|{bar_close_ms}|{leg}")``.
Deterministic, so a retry after a crash reuses the same cloid and ``orderStatus`` by cloid tells whether the first
attempt reached the exchange. The key is a SERVER secret (config ``cloid_secret`` ← env ``CLOID_SECRET``, required in
prod for the executor): REVIEW_MONEY H2 — keyed with the subscription id, a user could compute our past and future
cloids. The prefix only IDENTIFIES platform-looking orders; attribution never trusts it (fills-ingest attributes a
fill only when its cloid AND exchange-assigned oid match an order we recorded, see ``app.hl.fills``).
PRIVACY: the prefix makes our orders recognisable on-chain as platform orders (cloids are public in fills);
the builder code already reveals that to anyone reading order actions. Disclosed in the risk disclosure (§5.8).
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from app.errors import ValidationFailed
from app.execution.ports import PlaceResult
from app.hl.markets import MarketCatalog, canonical, is_valid_px, is_valid_sz
from app.logging import get_logger

__all__ = [
    "CLOID_PREFIX", "OrderResult", "LeverageResult", "BuilderCode", "ExchangeGateway",
    "make_cloid", "cloid_secret", "is_platform_cloid", "normalize_order_response", "wire_float",
    "SdkExchangeGateway", "SdkGatewayFactory", "sdk_available",
]

log = get_logger("app.hl.client")

CLOID_PREFIX = "a17a1000"  # 4 bytes; must equal app.execution.executor.CLOID_PREFIX
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
IOC_ORDER_TYPE = {"limit": {"tif": "Ioc"}}

# The executor's port type IS our result type (status: filled | partial | rejected | resting | unknown).
OrderResult = PlaceResult


@dataclass(frozen=True)
class LeverageResult:
    ok: bool
    error: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BuilderCode:
    address: str
    fee_tenths_bp: int

    def __post_init__(self) -> None:
        if not isinstance(self.address, str) or not _ADDR_RE.fullmatch(self.address):
            raise ValidationFailed("builder address must be a lower-case 0x address (refusing to trade without it)")
        if isinstance(self.fee_tenths_bp, bool) or not isinstance(self.fee_tenths_bp, int) \
                or not 1 <= self.fee_tenths_bp <= 100:
            raise ValidationFailed("builder fee must be 1..100 tenths of a bp")

    def wire(self) -> dict:
        return {"b": self.address, "f": self.fee_tenths_bp}


def _bar_close_ms(bar_close: datetime | int) -> int:
    if isinstance(bar_close, datetime):
        if bar_close.tzinfo is None:
            raise ValidationFailed("bar_close must be tz-aware")
        return int(bar_close.timestamp() * 1000)
    if isinstance(bar_close, bool) or not isinstance(bar_close, int) or bar_close < 0:
        raise ValidationFailed("bar_close must be a datetime or ms int")
    return bar_close


_DEV_CLOID_SECRET = b"aijalon-dev-only-cloid-secret-not-for-prod"


def cloid_secret(secret: str | bytes | None = None) -> bytes:
    """The cloid HMAC key: explicit ``secret``, else config ``cloid_secret`` (env CLOID_SECRET). Outside prod a fixed,
    clearly non-prod key is used when unset; in prod an empty key refuses to make cloids (fail closed)."""
    if secret:
        return secret if isinstance(secret, bytes) else str(secret).encode()
    from app.config import get_settings

    s = get_settings()
    raw = getattr(s, "cloid_secret", "") or ""
    if raw:
        return raw.encode()
    if getattr(s, "is_prod", False):
        raise ValidationFailed("CLOID_SECRET is required in prod (cloids must not be computable by users)")
    return _DEV_CLOID_SECRET


def make_cloid(subscription_id: str, bar_close: datetime | int, leg: str | int, *,
               secret: str | bytes | None = None) -> str:
    """Deterministic 16-byte client order id: ``0x`` + ``a17a1000`` +
    HMAC-SHA256(CLOID_SECRET, "aijalon/cloid/v1|{subscription_id}|{ms}|{leg}")[:12].
    ``leg`` should identify coin and attempt, e.g. ``"xyz:SILVER|0"``."""
    if not subscription_id:
        raise ValidationFailed("subscription_id required")
    msg = f"aijalon/cloid/v1|{subscription_id}|{_bar_close_ms(bar_close)}|{leg}".encode()
    mac = hmac.new(cloid_secret(secret), msg, hashlib.sha256).hexdigest()[:24]
    return "0x" + CLOID_PREFIX + mac


def is_platform_cloid(cloid: str | None) -> bool:
    return isinstance(cloid, str) and bool(_CLOID_RE.fullmatch(cloid.lower())) \
        and cloid.lower().startswith("0x" + CLOID_PREFIX)


def wire_float(value: str | Decimal) -> float:
    """Our exact grid string → the float the SDK expects, refusing any value the SDK's ``float_to_wire`` would
    change (it formats with 8 decimals and raises on rounding). Money never touches float elsewhere."""
    d = value if isinstance(value, Decimal) else Decimal(value)
    f = float(d)
    rounded = f"{f:.8f}"
    if abs(float(rounded) - f) >= 1e-12 or Decimal(rounded).normalize() != d.normalize():
        raise ValidationFailed("value not exactly representable on the wire", value=str(value))
    return f


def _dec_or_none(v: Any) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def normalize_order_response(resp: Any, requested_sz: Decimal) -> OrderResult:
    """SDK/exchange response → OrderResult.

    ``{"status":"ok","response":{"type":"order","data":{"statuses":[{"filled":{"totalSz","avgPx","oid"}}]}}}``
    → filled (or partial when totalSz < requested; the IOC remainder is cancelled by the exchange);
    ``{"resting":{"oid"}}`` → resting; ``{"error": "..."}`` → rejected; ``{"status":"err","response": "..."}`` →
    rejected; anything else → unknown (executor resolves it later by cloid)."""
    raw = resp if isinstance(resp, Mapping) else {"unparsed": repr(resp)[:500]}
    if not isinstance(resp, Mapping):
        return OrderResult(status="unknown", error="unparsed response", raw=raw)
    if resp.get("status") == "err":
        return OrderResult(status="rejected", error=str(resp.get("response"))[:500], raw=raw)
    if resp.get("status") != "ok":
        return OrderResult(status="unknown", error="unexpected status", raw=raw)
    try:
        statuses = resp["response"]["data"]["statuses"]
        st = statuses[0]
    except (KeyError, IndexError, TypeError):
        return OrderResult(status="unknown", error="no order status", raw=raw)
    if not isinstance(st, Mapping):
        return OrderResult(status="unknown", error=str(st)[:200], raw=raw)
    if "filled" in st:
        f = st["filled"] or {}
        total = _dec_or_none(f.get("totalSz")) or Decimal(0)
        status = "filled" if total >= requested_sz else ("partial" if total > 0 else "rejected")
        return OrderResult(status=status, filled_sz=total, avg_px=_dec_or_none(f.get("avgPx")), oid=f.get("oid"),
                           raw=raw)
    if "resting" in st:
        return OrderResult(status="resting", oid=(st["resting"] or {}).get("oid"), raw=raw)
    if "error" in st:
        return OrderResult(status="rejected", error=str(st["error"])[:500], raw=raw)
    return OrderResult(status="unknown", error="unrecognised order status", raw=raw)


@runtime_checkable
class ExchangeGateway(Protocol):
    """Superset of ``app.execution.ports.ExchangeGateway``."""

    def place_ioc(self, *, coin: str, is_buy: bool, sz: Decimal | str, limit_px: Decimal | str, reduce_only: bool,
                  cloid: str) -> OrderResult: ...

    def update_leverage(self, *, coin: str, leverage: int, is_cross: bool) -> LeverageResult: ...


def sdk_available() -> bool:
    try:
        import eth_account  # noqa: F401
        import hyperliquid.exchange  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class SdkExchangeGateway:
    """One gateway per (agent key, trading account) per tick. Not shared across users."""

    def __init__(self, exchange: Any, catalog: MarketCatalog, builder: BuilderCode, *,
                 cloid_factory: Callable[[str], Any] | None = None, expires_after_ms: int | None = 30_000,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not isinstance(builder, BuilderCode):
            raise ValidationFailed("builder code required")
        self._ex = exchange
        self._catalog = catalog
        self._builder = builder
        self._cloid_factory = cloid_factory
        self._expires_after_ms = expires_after_ms
        self._now_ms = now_ms
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_ms = 0

    @property
    def builder(self) -> BuilderCode:
        return self._builder

    def _distinct_ms(self) -> None:
        """The SDK uses the current ms as the action nonce; two actions of one signer in the same ms collide.
        Hold the lock and wait for the clock to move before each call."""
        while self._now_ms() <= self._last_ms:
            self._sleep(0.001)
        self._last_ms = self._now_ms()
        if self._expires_after_ms and hasattr(self._ex, "set_expires_after"):
            # Exchange rejects the action if it arrives after this instant (stale-order protection).
            self._ex.set_expires_after(self._last_ms + self._expires_after_ms)

    def _cloid(self, cloid: str) -> Any:
        if self._cloid_factory is not None:
            return self._cloid_factory(cloid)
        from hyperliquid.utils.types import Cloid  # type: ignore

        return Cloid.from_str(cloid)

    def place_ioc(self, *, coin: str, is_buy: bool, sz: Decimal | str, limit_px: Decimal | str, reduce_only: bool,
                  cloid: str) -> OrderResult:
        m = self._catalog.market(coin)
        if m.is_delisted:
            raise ValidationFailed("market delisted", coin=coin)
        sz_d = sz if isinstance(sz, Decimal) else Decimal(str(sz))
        px_d = limit_px if isinstance(limit_px, Decimal) else Decimal(str(limit_px))
        if not is_valid_sz(sz_d, m.sz_decimals):
            raise ValidationFailed("size not on lot grid", coin=coin, sz=str(sz))
        if not is_valid_px(px_d, m.sz_decimals):
            raise ValidationFailed("price not on tick grid", coin=coin, px=str(limit_px))
        if not is_platform_cloid(cloid):
            raise ValidationFailed("cloid must carry the platform prefix", cloid=str(cloid))
        sz_f, px_f = wire_float(canonical(sz_d)), wire_float(canonical(px_d))
        builder = self._builder.wire()
        with self._lock:
            self._distinct_ms()
            resp = self._ex.order(coin, bool(is_buy), sz_f, px_f, IOC_ORDER_TYPE, reduce_only=bool(reduce_only),
                                  cloid=self._cloid(cloid.lower()), builder=builder)
        return normalize_order_response(resp, sz_d)

    def update_leverage(self, *, coin: str, leverage: int, is_cross: bool) -> LeverageResult:
        m = self._catalog.market(coin)
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= m.max_leverage:
            raise ValidationFailed("leverage out of range", coin=coin, max=m.max_leverage)
        if is_cross and not m.allows_cross:
            raise ValidationFailed("market is isolated-only", coin=coin, margin_mode=m.margin_mode)
        with self._lock:
            self._distinct_ms()
            resp = self._ex.update_leverage(leverage, coin, bool(is_cross))
        if not isinstance(resp, Mapping):
            return LeverageResult(ok=False, error="unparsed response", raw={"unparsed": repr(resp)[:500]})
        ok = resp.get("status") == "ok"
        return LeverageResult(ok=ok, error=None if ok else str(resp.get("response"))[:500], raw=resp)


class SdkGatewayFactory:
    """``GatewayFactory`` port: builds an SDK ``Exchange`` for one agent key without extra network calls.

    The SDK's ``Info`` normally downloads meta (and spot meta) on construction; we pass the validator ``meta`` and
    an empty spot meta, then register builder-dex coins from OUR catalog (so the asset-id formula lives in one
    place: ``app.hl.markets``). Fails closed if the SDK's internal maps are missing (incompatible version)."""

    def __init__(self, catalog_provider: Callable[[], MarketCatalog], builder: BuilderCode, *, base_url: str,
                 exchange_cls: Any = None, account_from_key: Callable[[Any], Any] | None = None,
                 timeout: float | None = 10.0) -> None:
        self._catalog_provider = catalog_provider
        self._builder = builder
        self._base_url = base_url
        self._exchange_cls = exchange_cls
        self._account_from_key = account_from_key
        self._timeout = timeout

    def create(self, key: Any, account_address: str, vault_address: str | None) -> SdkExchangeGateway:
        catalog = self._catalog_provider()
        exchange_cls = self._exchange_cls
        if exchange_cls is None:
            from hyperliquid.exchange import Exchange  # type: ignore

            exchange_cls = Exchange
        acct_from_key = self._account_from_key
        if acct_from_key is None:
            from eth_account import Account  # type: ignore

            acct_from_key = Account.from_key
        wallet = acct_from_key(bytes(key) if isinstance(key, (bytes, bytearray, memoryview)) else key)
        master = str(account_address).lower()
        vault = vault_address.lower() if vault_address and vault_address.lower() != master else None
        meta = catalog.raw_meta.get("")
        if not meta:
            raise ValidationFailed("catalog lacks validator meta")
        kwargs: dict[str, Any] = {"meta": meta, "vault_address": vault, "account_address": master}
        optional: dict[str, Any] = {"spot_meta": {"universe": [], "tokens": []}, "timeout": self._timeout}
        try:
            params = inspect.signature(exchange_cls).parameters
        except (TypeError, ValueError):
            params = None
        if params is not None and not any(p.kind == p.VAR_KEYWORD for p in params.values()):
            missing = [k for k in kwargs if k not in params]
            if missing:  # never silently drop vault_address: that would trade the master instead of the sub-account
                raise ValidationFailed("incompatible hyperliquid SDK Exchange signature", missing=missing)
            kwargs.update({k: v for k, v in optional.items() if k in params})
        else:
            kwargs.update(optional)
        ex = exchange_cls(wallet, self._base_url, **kwargs)
        _register_builder_dex_coins(ex, catalog)
        return SdkExchangeGateway(ex, catalog, self._builder)


def _register_builder_dex_coins(exchange: Any, catalog: MarketCatalog) -> None:
    info = getattr(exchange, "info", None)
    maps = [getattr(info, n, None) for n in ("coin_to_asset", "name_to_coin", "asset_to_sz_decimals")]
    if info is None or not all(isinstance(mp, dict) for mp in maps):
        raise ValidationFailed("incompatible hyperliquid SDK: Info asset maps missing")
    coin_to_asset, name_to_coin, asset_to_szd = maps
    for m in catalog.markets.values():
        existing = coin_to_asset.get(m.coin)
        if existing is not None and existing != m.asset_id:
            raise ValidationFailed("SDK and catalog disagree on asset id", coin=m.coin, sdk=existing, ours=m.asset_id)
        if m.dex:  # validator coins come from meta already
            coin_to_asset[m.coin] = m.asset_id
            name_to_coin[m.coin] = m.coin
            asset_to_szd[m.asset_id] = m.sz_decimals

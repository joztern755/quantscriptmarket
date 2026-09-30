"""Concrete implementations of the API ports (app/api/deps.py). The ONLY place the API imports other teams'
modules — always lazily, inside methods, so `app.api` imports even when a module is missing. A missing module
fails CLOSED at call time (503 service_unavailable), except where a local fallback is equivalent and
side-effect free (rate limiter, EIP-191 recovery).

External contract each adapter expects (the lead reconciles names):
  auth        app.security.auth.build_verifier(settings).verify(token) -> AuthContext; require_mfa(ctx);
              require_step_up(ctx, max_age=)
  audit       INSERT INTO audit_log(actor, action, target, payload, ip_hash) — the DB trigger builds the hash
              chain (0001_init.sql); payload validated with app.security.audit.canonical_json (no floats)
  ledger      app.ledger.service.{post_transaction(conn, key, kind, memo, entries, created_by) -> PostedTx,
              ensure_account(conn, code, kind, owner, non_negative=), get_balance(conn, code)} (no fallback)
  database    app.db.engine.{create_db_engine(settings, application_name=, statement_timeout_ms=), sqlstate_of}
  agent keys  app.security.kms.make_encryptor(settings) (encrypt-only);
              app.security.agent_keys.generate_sealed_agent_key(encryptor, user_id=) -> SealedKey
  typed data  app.hl.typed_data.{approve_agent_request, approve_builder_fee_request, usd_send_request}
              -> UserSignedRequest.public_view() {typed_data, action, nonce}
  hl info     app.hl.info.InfoClient(url).{extra_agents, max_builder_fee, clearinghouse_state(user, dex),
              user_role, user_non_funding_ledger_updates}; app.hl.markets.MarketCatalog.from_info(client, dexes)
  stripe      app.payments.stripe_pay.{create_topup_intent, verify_webhook, handle_event, StripeTopupConfig,
              default_gateway}; optional make_fee_lookup(gateway) when the Stripe fee is passed to the user
  usdc        app.payments.usdc.{build_topup_request, credit_from_detection};
              app.hl.deposits.detect_deposits(treasury ledger updates, treasury_address=, verified_wallets=,
              since_ms=) -> DepositScan
  notifier    alerts table (in-app, same tx) + app.alerts.notifier.{Notifier, TelegramSink, Alert, Severity}
  sandbox     app.sandbox.validate.validate_source, app.sandbox.nocode.{validate_spec, compile_spec},
              app.sandbox.backtest.{fetch_market_data, HyperliquidInfoFetcher, backtest_on_data (dev only)};
              sandbox service POST {sandbox_url}/backtest {source, data, params} with a Cloud Run ID token and
              X-Sandbox-Secret (settings.sandbox_shared_secret)
  kyc         app.kyc.create_session(user_id=, return_url=) -> {url, provider, provider_ref, status}
  jobs        see JOB_ENTRYPOINTS
  domain      app.domain.{fees, referrals, billing, track_record}
  oidc        google.oauth2.id_token.verify_oauth2_token
  wallet sig  eth_account (fallback: app.api.ethsig)
"""
from __future__ import annotations

import importlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR
from typing import Any, Callable, Iterator, Optional

from app.api import ledger_ops
from app.api.deps import (
    ApiConfig,
    NotImplementedYet,
    SealedAgentKey,
    ServiceUnavailable,
    Services,
    api_config,
)
from app.config import Settings, get_settings
from app.errors import (
    AppError,
    Conflict,
    ExternalServiceError,
    InsufficientBalance,
    Unauthorized,
    ValidationFailed,
)
from app.logging import get_logger
from app.money import to_micro

log = get_logger("app.api.adapters")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _mod(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _require(name: str) -> Any:
    m = _mod(name)
    if m is None:
        raise ServiceUnavailable(f"{name} is not available")
    return m


def _fn(module: str, name: str) -> Optional[Callable[..., Any]]:
    m = _mod(module)
    f = getattr(m, name, None) if m is not None else None
    return f if callable(f) else None


# =============================================================================================================
# Database
# =============================================================================================================
_SQLSTATE_MAP: dict[str, Callable[[], AppError]] = {
    "AJ402": lambda: InsufficientBalance("insufficient balance"),
    "AJ409": lambda: Conflict("idempotency key reused with different content"),
    "23505": lambda: Conflict("already exists"),
    "40001": lambda: Conflict("concurrent update, please retry"),
    "40P01": lambda: Conflict("concurrent update, please retry"),
    "55P03": lambda: Conflict("resource busy, please retry"),
    "57014": lambda: ServiceUnavailable("database timeout"),
}


def map_db_error(exc: BaseException) -> BaseException:
    """SQLSTATE → AppError for the states a client can act on; everything else stays a 500."""
    if isinstance(exc, AppError):
        return exc
    sqlstate_of = _fn("app.db.engine", "sqlstate_of")
    state = sqlstate_of(exc) if sqlstate_of else getattr(getattr(exc, "orig", None), "sqlstate", None)
    factory = _SQLSTATE_MAP.get(state or "")
    return factory() if factory else exc


class SqlDatabase:
    """Engine from app.db.engine.create_db_engine (UTC sessions, statement timeout, pool pre-ping)."""

    def __init__(self, settings: Settings, *, application_name: str = "aijalon-api") -> None:
        self._settings = settings
        self._engine: Any = None
        self._lock = threading.Lock()
        self._app_name = application_name

    def engine(self) -> Any:
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    create = _fn("app.db.engine", "create_db_engine")
                    if create is None:
                        raise ServiceUnavailable("database layer not available")
                    self._engine = create(self._settings, application_name=self._app_name,
                                          statement_timeout_ms=15_000)
        return self._engine

    @contextmanager
    def begin(self) -> Iterator[Any]:
        try:
            with self.engine().begin() as conn:
                yield conn
        except Exception as e:  # noqa: BLE001 - re-raised (mapped when the SQLSTATE is actionable)
            mapped = map_db_error(e)
            if mapped is e:
                raise
            raise mapped from e


# =============================================================================================================
# Auth
# =============================================================================================================
class FirebaseAuthAdapter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._verifier: Any = None
        self._lock = threading.Lock()

    def _mod(self) -> Any:
        return _require("app.security.auth")

    def verify(self, token: str) -> dict[str, Any]:
        mod = self._mod()
        if self._verifier is None:
            with self._lock:
                if self._verifier is None:
                    self._verifier = mod.build_verifier(self._settings)
        ctx = self._verifier.verify(token)
        claims = dict(ctx.claims)
        claims["uid"] = ctx.uid
        claims["_ctx"] = ctx
        return claims

    def require_mfa(self, claims: dict[str, Any]) -> None:
        self._mod().require_mfa(claims["_ctx"])

    def require_step_up(self, claims: dict[str, Any], max_age_seconds: int) -> None:
        self._mod().require_step_up(claims["_ctx"], max_age=max_age_seconds)


# =============================================================================================================
# Audit / ledger
# =============================================================================================================
class SqlAuditAdapter:
    def __init__(self, store: Any) -> None:
        self._store = store

    def write(self, conn: Any, *, actor: str, action: str, target: str, payload: dict[str, Any],
              ip_hash: Optional[str]) -> None:
        canonical = _fn("app.security.audit", "canonical_json")
        if canonical is not None:
            canonical(payload)  # rejects floats / non-JSON types before anything is written
        self._store.insert_audit(conn, actor=actor[:200], action=action[:200], target=target[:256],
                                 payload=payload, ip_hash=ip_hash)


class LedgerAdapter:
    """All ledger writes go through app.ledger.service (validation, idempotency, hash chain via ledger_post)."""

    def _svc(self) -> Any:
        return _require("app.ledger.service")

    def ensure_account(self, conn: Any, code: str) -> None:
        kind, non_negative, owner = ledger_ops.account_spec(code)
        self._svc().ensure_account(conn, code, kind, owner, non_negative=non_negative)

    def post(self, conn: Any, *, idempotency_key: str, kind: str, memo: str, entries: list[tuple[str, int]],
             created_by: str) -> str:
        return str(self._svc().post_transaction(conn, idempotency_key, kind, memo, entries, created_by).id)

    def balance(self, conn: Any, account_code: str) -> int:
        return int(self._svc().get_balance(conn, account_code))


# =============================================================================================================
# Keys (ENCRYPT ONLY — the API process never builds a decryptor)
# =============================================================================================================
class _Encryptor:
    """Lazy encrypt-only envelope. ``factory``: ``make_encryptor`` (agent-keys KMS key) or ``make_code_encryptor``
    (the DEDICATED creator-code KMS key, REVIEW_TRADING_KEYS F2) — never one instance for both secret classes."""

    def __init__(self, settings: Settings, factory: str = "make_encryptor") -> None:
        if factory not in ("make_encryptor", "make_code_encryptor"):
            raise ValueError("unknown encryptor factory")
        self._settings = settings
        self._factory = factory
        self._enc: Any = None
        self._lock = threading.Lock()

    def get(self) -> Any:
        if self._enc is None:
            with self._lock:
                if self._enc is None:
                    self._enc = getattr(_require("app.security.kms"), self._factory)(self._settings)
        return self._enc


class AgentKeyAdapter:
    def __init__(self, enc: _Encryptor) -> None:
        self._enc = enc

    def generate_sealed(self, user_id: str) -> SealedAgentKey:
        sk = _require("app.security.agent_keys").generate_sealed_agent_key(self._enc.get(), user_id=user_id)
        return SealedAgentKey(agent_address=sk.address.lower(), key_ciphertext=bytes(sk.ciphertext),
                              kms_key_version=str(sk.key_version))


class CodeVaultAdapter:
    """Creator strategy code, sealed under the creator-code KMS key (``_Encryptor(s, "make_code_encryptor")``)."""

    def __init__(self, enc: _Encryptor) -> None:
        self._enc = enc

    def seal(self, plaintext: bytes, aad: bytes) -> tuple[bytes, str]:
        blob = self._enc.get().seal(plaintext, aad)
        return bytes(blob.blob), str(blob.key_version)


# =============================================================================================================
# Hyperliquid: EIP-712 user-signed actions (app.hl.typed_data) and read-only /info (app.hl.info.InfoClient)
# =============================================================================================================
class TypedDataAdapter:
    def __init__(self, settings: Settings) -> None:
        self._mainnet = settings.hl_is_mainnet

    def approve_agent(self, *, agent_address: str, agent_name: str, nonce: int, signature_chain_id: str) -> dict:
        req = _require("app.hl.typed_data").approve_agent_request(
            agent_address, nonce_ms=nonce, signature_chain_id=signature_chain_id, is_mainnet=self._mainnet,
            agent_name=agent_name)
        return req.public_view()

    def approve_builder_fee(self, *, builder: str, max_fee_tenths_bp: int, nonce: int,
                            signature_chain_id: str) -> dict:
        req = _require("app.hl.typed_data").approve_builder_fee_request(
            builder, nonce_ms=nonce, signature_chain_id=signature_chain_id, is_mainnet=self._mainnet,
            max_fee_tenths_bp=max_fee_tenths_bp)
        return req.public_view()

    def usd_send(self, *, destination: str, amount: str, time_ms: int, signature_chain_id: str) -> dict:
        req = _require("app.hl.typed_data").usd_send_request(
            destination, amount, time_ms=time_ms, signature_chain_id=signature_chain_id, is_mainnet=self._mainnet)
        return req.public_view()


#: longest an API request waits for room in the shared Hyperliquid budget before failing (503) — user requests
#: never queue behind a minute window the way data jobs do
API_HL_MAX_WAIT_SECONDS = 2.0


class HlInfoAdapter:
    """Read-only Hyperliquid /info via app.hl.info.InfoClient (retries, size caps, shape checks live there).

    Every call (each HTTP attempt, retries included) is charged to the shared per-egress-IP weight budget in Postgres
    (``app.hl.budget``, table hl_rate_budget) in the low-priority ``jobs`` pool with a short wait
    (``API_HL_MAX_WAIT_SECONDS``): when the budget is spent the request fails with ``HlBudgetExhausted`` (503) instead
    of pushing the IP into Hyperliquid 429s (REVIEW_AUTH_API F1). The API has its OWN egress IP (``HL_EGRESS_KEY=api``,
    infra/gcp NAT split), so user-triggered reads can never spend the executor's budget. Without a database handle
    (tests) or with ``HL_SHARED_BUDGET=false`` no accounting is done."""

    def __init__(self, settings: Settings, db: Any = None) -> None:
        self._url = settings.hl_api_url
        self._settings = settings
        self._db = db
        self._client: Any = None
        self._lock = threading.Lock()

    def _rate_hook(self) -> Any:
        limits = getattr(self._settings, "hl_limits", None)
        if self._db is None or limits is None or not getattr(limits, "shared_budget", False):
            return None
        budget = _mod("app.hl.budget")
        if budget is None:
            return None
        return budget.BudgetHook(budget.HlRateBudget(self._db, limits), limits, default_pool=budget.POOL_JOBS,
                                 max_wait_seconds=API_HL_MAX_WAIT_SECONDS)

    def client(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = _require("app.hl.info").InfoClient(self._url, timeout=8.0, max_retries=2,
                                                                      rate_hook=self._rate_hook())
        return self._client

    def extra_agents(self, user: str) -> list[dict[str, Any]]:
        return [{"address": str(a.get("address", "")).lower(), "name": a.get("name") or "",
                 "validUntil": a.get("validUntil")} for a in self.client().extra_agents(user)]

    def max_builder_fee(self, user: str, builder: str) -> int:
        return int(self.client().max_builder_fee(user, builder))

    def clearinghouse_state(self, user: str, dex: str = "") -> dict[str, Any]:
        return self.client().clearinghouse_state(user, dex)

    def master_of(self, address: str) -> Optional[str]:
        """Master wallet of a sub-account (None when `address` is not a sub-account)."""
        role = self.client().user_role(address)
        if role.get("role") == "subAccount":
            master = (role.get("data") or {}).get("master")
            return str(master).lower() if master else None
        return None

    def ledger_updates(self, user: str, start_ms: int) -> list[dict[str, Any]]:
        return list(self.client().user_non_funding_ledger_updates(user, start_ms))

    def find_usd_send(self, *, sender: str, destination: str, amount_micro: int, tx_hash: str) -> bool:
        """True iff `sender`'s ledger shows a USDC transfer with this hash to `destination` for exactly this amount."""
        types = getattr(_mod("app.hl.deposits"), "TRANSFER_TYPES", ("send", "usdSend", "internalTransfer"))
        start = int((time.time() - 14 * 86400) * 1000)
        for e in self.ledger_updates(sender, start):
            d = e.get("delta") or {}
            if str(e.get("hash", "")).lower() != tx_hash.lower() or d.get("type") not in types:
                continue
            if str(d.get("destination", "")).lower() != destination.lower():
                continue
            if str(d.get("user", sender)).lower() != sender.lower() or str(d.get("token") or "USDC") != "USDC":
                continue
            raw = d.get("amount", d.get("usdc"))
            try:
                if to_micro(str(raw), rounding=ROUND_FLOOR) == amount_micro:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def relay_exchange(self, body: dict[str, Any]) -> tuple[int, Any]:
        """Forward an already-VALIDATED user-signed action unchanged to Hyperliquid /exchange (app.hl.relay; the
        route validates first). Returns (upstream HTTP status, parsed body)."""
        return _require("app.hl.relay").forward_exchange(self._url.rstrip("/") + "/exchange", body)

    def unknown_coins(self, coins: list[str]) -> list[str]:
        mk = _require("app.hl.markets")
        catalog = mk.MarketCatalog.from_info(self.client(), dexes=mk.MarketCatalog.dexes_for(coins))
        return [c for c in coins if c not in catalog]


# =============================================================================================================
# Payments
# =============================================================================================================
class StripeAdapter:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._gw: Any = None

    def _m(self) -> Any:
        return _require("app.payments.stripe_pay")

    def _gateway(self) -> Any:
        if self._gw is None:
            self._gw = self._m().default_gateway(self._s)
        return self._gw

    def _cfg(self) -> Any:
        return self._m().StripeTopupConfig.from_settings(self._s)

    def create_topup_intent(self, *, user_id: str, amount_micro: int, token: str) -> dict[str, Any]:
        ti = self._m().create_topup_intent({"id": user_id, "status": "active"}, amount_micro, "usd",
                                           gateway=self._gateway(), idempotency=token, config=self._cfg())
        return ti.public_view()

    def verify_webhook(self, payload: bytes, sig_header: str) -> dict[str, Any]:
        secrets = [x.strip() for x in (self._s.stripe_webhook_secret or "").split(",") if x.strip()]
        return self._m().verify_webhook(payload, sig_header, secrets)

    def handle_event(self, event: dict[str, Any]) -> Any:
        m, cfg = self._m(), self._cfg()
        fee_lookup = None
        if not cfg.stripe_fee_absorbed:
            maker = getattr(m, "make_fee_lookup", None)
            fee_lookup = maker(self._gateway()) if callable(maker) else None  # None → handler flags manual review
        return m.handle_event(event, config=cfg, gateway=self._gateway(), fee_lookup=fee_lookup)


class UsdcAdapter:
    def __init__(self, settings: Settings, hl: HlInfoAdapter) -> None:
        self._s = settings
        self._hl = hl

    def build_topup(self, *, master_address: str, amount_micro: int, signature_chain_id: str,
                    time_ms: int) -> dict[str, Any]:
        req = _require("app.payments.usdc").build_topup_request(
            user_master_address=master_address, amount_micro=amount_micro, treasury_address=self._s.treasury_address,
            signature_chain_id=signature_chain_id.lower(), time_ms=time_ms, is_mainnet=self._s.hl_is_mainnet,
            min_topup_micro=self._s.economics.min_topup_micro)
        out = req.public_view()
        out.update({"source": req.source, "destination": req.destination})
        return out

    #: never scan the treasury ledger further back than this from an API process (REVIEW_AUTH_API F1)
    MAX_LOOKBACK_MS = 48 * 3600 * 1000

    def detect(self, *, senders: list[str], since_ms: Optional[int]) -> list[Any]:
        """USDC transfers from `senders` (the user's verified wallets) into the treasury, from the TREASURY's
        on-chain ledger (app.hl.deposits.detect_deposits) — never from anything the client says. The lookback is
        clamped to 48 h (a client-supplied time can never make us download the treasury's whole history). NOT used
        by POST /deposits/usdc/confirm any more (that only records a scan request; deposits-scan books transfers)."""
        floor = int(time.time() * 1000) - self.MAX_LOOKBACK_MS
        start = max(int(since_ms), floor) if since_ms else floor
        updates = self._hl.ledger_updates(self._s.treasury_address, start)
        scan = _require("app.hl.deposits").detect_deposits(
            updates, treasury_address=self._s.treasury_address, verified_wallets=senders, since_ms=start)
        return list(scan.deposits)

    def credit_from_detection(self, detection: Any, user_for_address: Callable[[str], Optional[str]]) -> Any:
        return _require("app.payments.usdc").credit_from_detection(
            detection, treasury_address=self._s.treasury_address,
            min_topup_micro=self._s.economics.min_topup_micro, user_for_address=user_for_address)


# =============================================================================================================
# Notifications
# =============================================================================================================
class NotifierAdapter:
    """In-app alert row in the SAME transaction (durable), plus best-effort out-of-band delivery of ops alerts
    (warn/critical, user_id=None) via app.alerts.notifier on a background thread (never blocks a request)."""

    _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")

    def __init__(self, settings: Settings, store: Any) -> None:
        self._s = settings
        self._store = store
        self._notifier: Any = None

    def notify(self, conn: Any, *, user_id: Optional[str], severity: str, kind: str, payload: dict[str, Any],
               dedup_key: Optional[str] = None) -> None:
        """``dedup_key``: at most one alert row per key (alerts.dedup_key unique) — repeats are silently dropped."""
        if dedup_key:
            self._store.insert_alert(conn, user_id=user_id, severity=severity, kind=kind, payload=payload,
                                     dedup_key=dedup_key)
        else:
            self._store.insert_alert(conn, user_id=user_id, severity=severity, kind=kind, payload=payload)
        if user_id is None and severity in ("warn", "critical"):
            self._pool.submit(self._deliver, kind, severity, dict(payload))

    def notify_alert(self, conn: Any, alert: Any) -> None:
        sev = getattr(getattr(alert, "severity", None), "value", None) or str(getattr(alert, "severity", "info"))
        data = dict(getattr(alert, "data", {}) or {})
        if getattr(alert, "coin", None):
            data["coin"] = alert.coin
        self.notify(conn, user_id=getattr(alert, "user_id", None), severity=sev, kind=str(alert.kind), payload=data)

    def _deliver(self, kind: str, severity: str, payload: dict[str, Any]) -> None:
        try:
            m = _mod("app.alerts.notifier")
            if m is None or not self._s.telegram_bot_token:
                return
            if self._notifier is None:
                self._notifier = m.Notifier(telegram=m.TelegramSink(self._s.telegram_bot_token,
                                                                    self._s.telegram_ops_chat_id))
            self._notifier.notify(m.Alert(kind=kind, severity=m.Severity(severity), user_id=None, data=payload))
        except Exception:  # noqa: BLE001 - best effort; the in-app row is the durable record
            log.warning("ops alert delivery failed", extra={"fields": {"kind": kind}})


# =============================================================================================================
# Sandbox (validation is static & local; execution only in the sandbox service)
# =============================================================================================================
class SandboxAdapter:
    def __init__(self, settings: Settings, cfg: ApiConfig, db: Any = None) -> None:
        self._s = settings
        self._cfg = cfg
        self._db = db  # DatabasePort: stored candles (SPEC §12 own candle history) are read before the API

    def _fetcher(self) -> Any:
        bt = _require("app.sandbox.backtest")
        api = bt.HyperliquidInfoFetcher(self._s.hl_api_url)
        if self._db is None:
            return api
        try:
            store = importlib.import_module("app.jobs_data.candles").DbCandleSource(self._db)
        except ImportError:
            return api
        return bt.StoredFirstFetcher(store, api)

    @property
    def _max_lev(self) -> float:
        return float(self._s.risk.platform_max_leverage)

    def compile_nocode(self, spec: dict[str, Any]) -> str:
        nc = _require("app.sandbox.nocode")
        errs = nc.validate_spec(spec, platform_max_leverage=self._max_lev)
        if errs:
            raise ValidationFailed("no-code strategy rejected", errors=errs[:50])
        return nc.compile_spec(spec, platform_max_leverage=self._max_lev)

    def validate(self, code: str, known_markets: Optional[set[str]]) -> dict[str, Any]:
        vm = _require("app.sandbox.validate")
        return vm.validate_source(code, known_markets=known_markets, platform_max_leverage=self._max_lev).to_dict()

    def backtest(self, code: str, meta: dict[str, Any]) -> dict[str, Any]:
        bt, vm = _require("app.sandbox.backtest"), _require("app.sandbox.validate")
        smeta = vm.StrategyMeta(markets=tuple(meta["markets"]), timeframe=meta["timeframe"],
                                lookback=int(meta["lookback"]), max_leverage=float(meta["max_leverage"]))
        data = bt.fetch_market_data(smeta, self._fetcher())  # trusted side has egress; stored candles first
        if self._cfg.sandbox_url:
            return self._remote(code, data.to_json())
        if self._s.env in ("dev", "test"):
            return bt.backtest_on_data(code, data, meta=smeta)  # local subprocess runner — never in prod
        raise ServiceUnavailable("sandbox service not configured")

    def _remote(self, code: str, data: dict[str, Any]) -> dict[str, Any]:
        url = self._cfg.sandbox_url
        try:
            from google.auth.transport.requests import Request as GRequest
            from google.oauth2 import id_token
            token = id_token.fetch_id_token(GRequest(), url)  # Cloud Run service-to-service auth (audience = URL)
        except Exception as e:  # noqa: BLE001
            raise ServiceUnavailable("cannot obtain sandbox identity token", error=type(e).__name__) from None
        body = json.dumps({"source": code, "data": data, "params": {}}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        secret = getattr(self._s, "sandbox_shared_secret", "") or ""
        if secret:
            headers["X-Sandbox-Secret"] = secret   # defence in depth (app/sandbox/service.py layer 2)
        req = urllib.request.Request(url + "/backtest", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:  # noqa: S310 - URL from config
                raw = r.read(16 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            if e.code in (400, 422):
                try:
                    detail = json.loads(e.read(65536) or b"{}")
                except ValueError:
                    detail = {}
                raise ValidationFailed(str(detail.get("message") or "backtest rejected the strategy")[:300],
                                       sandbox_error=str(detail.get("error") or "")[:64]) from None
            raise ExternalServiceError("sandbox backtest failed", status=e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ExternalServiceError("sandbox unavailable", error=type(e).__name__) from None
        try:
            report = json.loads(raw)
        except ValueError:
            raise ExternalServiceError("sandbox returned invalid JSON") from None
        if not isinstance(report, dict):
            raise ExternalServiceError("sandbox returned an unexpected report")
        return report


# =============================================================================================================
# KYC / jobs / rate limit / OIDC / wallet signature / domain
# =============================================================================================================
class KycAdapter:
    def create_session(self, *, user_id: str, return_url: str) -> dict[str, Any]:
        fn = _fn("app.kyc", "create_session")
        if fn is None:
            raise ServiceUnavailable("KYC provider not configured")
        return dict(fn(user_id=user_id, return_url=return_url))


#: internal job name → candidate (module, function). Each is called as fn(db=<DatabasePort>, now=<datetime>,
#: **params) and returns a dict/dataclass summary. Tick is only mounted on the executor service.
JOB_ENTRYPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "tick": (("app.execution.jobs", "run_tick"), ("app.execution.executor", "run_tick")),
    "settle-daily": (("app.execution.jobs", "settle_daily"), ("app.execution.settlement", "settle_daily")),
    "ingest-signals": (("app.strategies.signals", "ingest"), ("app.execution.jobs", "ingest_signals")),
    "reconcile": (("app.execution.jobs", "reconcile"), ("app.execution.reconcile", "run")),
    "deposits-scan": (("app.hl.deposits", "scan"), ("app.execution.jobs", "deposits_scan")),
    "referral-tiers": (("app.execution.jobs", "referral_tiers"), ("app.domain.referrals_job", "run")),
    "candles-sync": (("app.jobs_data", "candles_sync"),),
    "fills-ingest": (("app.jobs_data", "fills_ingest"),),
    "funding-scan": (("app.jobs_data", "funding_scan"),),
    "agent-expiry-scan": (("app.jobs_data", "agent_expiry_scan"),),
}


def _jsonable(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    if isinstance(obj, dict):
        return json.loads(json.dumps(obj, default=str))
    return {"result": str(obj)[:2000]}


class JobsAdapter:
    def run(self, job: str, *, db: Any, now: datetime, params: dict[str, Any]) -> dict[str, Any]:
        for module, name in JOB_ENTRYPOINTS.get(job, ()):
            fn = _fn(module, name)
            if fn is not None:
                return _jsonable(fn(db=db, now=now, **params))
        raise NotImplementedYet(f"job {job} has no entrypoint installed")

    def latest_reconciliation(self, conn: Any) -> Optional[dict[str, Any]]:
        for module, name in (("app.execution.jobs", "latest_reconciliation"), ("app.execution.reconcile", "latest_report")):
            fn = _fn(module, name)
            if fn is not None:
                return _jsonable(fn(conn))
        return None


class RateLimitAdapter:
    """Per-process token buckets (app.security.ratelimit.InMemoryRateLimiter). Cloudflare WAF is the global
    first layer; replace with a Redis-backed RateLimiter for strict shared limits."""

    def __init__(self) -> None:
        self._limiters: dict[tuple[int, int], Any] = {}
        self._lock = threading.Lock()
        self._fallback: OrderedDict[str, list[float]] = OrderedDict()

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        rl = _mod("app.security.ratelimit")
        if rl is not None:
            with self._lock:
                lim = self._limiters.get((limit, window_seconds))
                if lim is None:
                    lim = rl.InMemoryRateLimiter(rl.RateLimit(capacity=limit, refill_per_s=limit / window_seconds))
                    self._limiters[(limit, window_seconds)] = lim
            allowed, _ = lim.try_acquire(key)
            return bool(allowed)
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._fallback.pop(key, []) if now - t < window_seconds]
            ok = len(hits) < limit
            if ok:
                hits.append(now)
            self._fallback[key] = hits
            while len(self._fallback) > 100_000:
                self._fallback.popitem(last=False)
            return ok


class GoogleOidcAdapter:
    ISSUERS = ("accounts.google.com", "https://accounts.google.com")

    def __init__(self) -> None:
        self._request: Any = None

    def verify(self, token: str, audience: str) -> dict[str, Any]:
        try:
            from google.auth.transport import requests as greq
            from google.oauth2 import id_token
        except ImportError:
            raise Unauthorized("internal auth unavailable") from None
        if self._request is None:
            self._request = greq.Request()
        try:
            claims = id_token.verify_oauth2_token(token, self._request, audience=audience, clock_skew_in_seconds=10)
        except Exception:  # noqa: BLE001 - any verification failure is a 401; never leak why
            raise Unauthorized("invalid identity token") from None
        if claims.get("iss") not in self.ISSUERS:
            raise Unauthorized("invalid token issuer")
        return dict(claims)


class WalletSigAdapter:
    def recover(self, message: str, signature: str) -> str:
        try:
            from eth_account import Account
            from eth_account.messages import encode_defunct
            return str(Account.recover_message(encode_defunct(text=message), signature=signature)).lower()
        except ImportError:
            pass
        except Exception:  # noqa: BLE001
            raise ValidationFailed("invalid signature") from None
        from app.api.ethsig import recover_personal_sign
        try:
            return recover_personal_sign(message, signature)
        except ValueError:
            raise ValidationFailed("invalid signature") from None


class DomainAdapter:
    def __init__(self, settings: Settings) -> None:
        self._e = settings.economics

    def subscription_split(self, price_micro: int) -> tuple[int, int]:
        return _require("app.domain.fees").subscription_split(price_micro, self._e)

    def post_sale_split(self, price_micro: int) -> tuple[int, int]:
        return _require("app.domain.fees").post_sale_split(price_micro, self._e)

    def validate_post_price(self, price_micro: int) -> int:
        return _require("app.domain.fees").validate_post_price(price_micro, self._e)

    def plan_price(self, plan: str) -> int:
        return _require("app.domain.fees").plan_price(plan, self._e)

    def plan_allows(self, plan: str, active_count: int) -> bool:
        return _require("app.domain.fees").plan_allows_active_strategies(plan, active_count, self._e)

    def evaluate_tier(self, active_users: int, notional_micro: int) -> Any:
        return _require("app.domain.referrals").evaluate_tier(active_users, notional_micro, self._e.referral_tiers)

    def generate_referral_code(self) -> str:
        return _require("app.domain.referrals").generate_referral_code()

    def normalize_referral_code(self, raw: Optional[str]) -> Optional[str]:
        return _require("app.domain.referrals").normalize_referral_code(raw)

    def self_referral_reasons(self, *, referrer: tuple, referee: tuple) -> tuple[str, ...]:
        """referrer / referee = (user_id, wallets, devices[, networks]) — networks optional (F7)."""
        r = _require("app.domain.referrals")
        a = r.ReferralIdentity.of(referrer[0], wallets=referrer[1], devices=referrer[2],
                                  networks=referrer[3] if len(referrer) > 3 else ())
        b = r.ReferralIdentity.of(referee[0], wallets=referee[1], devices=referee[2],
                                  networks=referee[3] if len(referee) > 3 else ())
        return tuple(r.self_referral_reasons(a, b))

    def estimate_monthly_need(self, prices: Any, plan_price: int) -> int:
        return _require("app.domain.billing").estimate_monthly_need(list(prices), plan_price)

    def add_months(self, dt: datetime, months: int) -> datetime:
        return _require("app.domain.billing").add_months(dt, months)

    def track_record(self, *, live_since: Optional[datetime], events: list[dict], spans: list[dict], now: datetime,
                     period_start: Optional[datetime], min_subscribers: int) -> dict[str, Any]:
        tr = _require("app.domain.track_record")
        rec = tr.compute_track_record(
            live_since,
            [tr.PnlEvent(str(e["subscription_id"]), e["time"], int(e["pnl_micro"])) for e in events],
            [tr.AllocationSpan(str(s["subscription_id"]), str(s["user_id"]), int(s["allocation_micro"]), s["start"],
                               s.get("end")) for s in spans],
            now, period_start=period_start)
        pub = tr.public_stats(rec, min_subscribers)
        return {"not_live_proven": rec.not_live_proven, "live_days": rec.live_days,
                "public": None if pub is None else {"subscribers": pub.subscriber_count, "roi_bps": pub.roi_bps,
                                                    "pnl_micro": pub.total_pnl_micro}}


    def equity_series(self, *, live_since: Optional[datetime], events: list[dict], spans: list[dict], now: datetime,
                      min_subscribers: int) -> tuple[list[dict[str, Any]], Optional[str]]:
        """Daily public live series (app.domain.track_record.daily_series) → ([{t, pnl_micro, roi_bps}], hidden)."""
        tr = _require("app.domain.track_record")
        pts, hidden = tr.daily_series(
            live_since,
            [tr.PnlEvent(str(e["subscription_id"]), e["time"], int(e["pnl_micro"])) for e in events],
            [tr.AllocationSpan(str(s["subscription_id"]), str(s["user_id"]), int(s["allocation_micro"]), s["start"],
                               s.get("end")) for s in spans],
            now, min_subscribers=min_subscribers)
        return [{"t": p.day, "pnl_micro": p.pnl_micro, "roi_bps": p.roi_bps} for p in pts], hidden


# =============================================================================================================
# Factory
# =============================================================================================================
def build_services(settings: Optional[Settings] = None) -> Services:
    s = settings or get_settings()
    cfg = api_config(s)
    from app.api.store import SqlStore  # imports SQLAlchemy; kept lazy so tests with fakes need no DB libs
    store = SqlStore()
    enc = _Encryptor(s)                                   # agent-keys KMS key (agent private keys only)
    code_enc = _Encryptor(s, "make_code_encryptor")      # creator-code KMS key (creator strategy code only)
    db = SqlDatabase(s)
    hl = HlInfoAdapter(s, db)
    return Services(
        settings=s,
        db=db,
        store=store,
        auth=FirebaseAuthAdapter(s),
        audit=SqlAuditAdapter(store),
        ledger=LedgerAdapter(),
        agent_keys=AgentKeyAdapter(enc),
        typed_data=TypedDataAdapter(s),
        hl=hl,
        stripe=StripeAdapter(s),
        usdc=UsdcAdapter(s, hl),
        notifier=NotifierAdapter(s, store),
        sandbox=SandboxAdapter(s, cfg, db),
        code_vault=CodeVaultAdapter(code_enc),
        kyc=KycAdapter(),
        jobs=JobsAdapter(),
        ratelimit=RateLimitAdapter(),
        oidc=GoogleOidcAdapter(),
        wallet_sig=WalletSigAdapter(),
        domain=DomainAdapter(s),
        clock=lambda: datetime.now(timezone.utc),
    )


__all__ = ["build_services", "map_db_error", "JOB_ENTRYPOINTS"]

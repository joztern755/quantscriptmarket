"""Concrete implementations of the API ports (app/api/deps.py). The ONLY place the API imports other teams'
modules — always lazily, inside methods, so `app.api` imports even when a module is missing. A missing module
fails CLOSED at call time (503 service_unavailable), except where a local fallback is equivalent and
side-effect free (rate limiter, EIP-712 builders from SPEC §6, Hyperliquid read-only /info, EIP-191 recovery).

External contract each adapter expects (the lead reconciles names):
  auth        app.security.auth.build_verifier(settings).verify(token) -> AuthContext; require_mfa(ctx);
              require_step_up(ctx, max_age=)
  audit       INSERT INTO audit_log(actor, action, target, payload, ip_hash) — the DB trigger builds the hash
              chain (0001_init.sql); payload validated with app.security.audit.canonical_json (no floats)
  ledger      app.ledger.post_transaction(conn, idempotency_key=, kind=, memo=, entries=[(code, micro)],
              created_by=) -> tx id | obj.tx_id; app.ledger.ensure_account(conn, code) (optional);
              fallback: SQL ledger_post() function
  agent keys  app.security.kms.make_encryptor(settings) (encrypt-only);
              app.security.agent_keys.generate_sealed_agent_key(encryptor, user_id=) -> SealedKey
  typed data  app.hl.typed_data.approve_agent_typed_data(agent_address=, agent_name=, nonce=,
              signature_chain_id=, is_mainnet=), approve_builder_fee_typed_data(builder=, max_fee_rate=, nonce=,
              signature_chain_id=, is_mainnet=) (fallback: SPEC §6 builders below);
              app.payments.usdc.usd_send_typed_data(...)
  hl info     app.hl.info.{extra_agents, max_builder_fee, clearinghouse_state, sub_accounts, known_coins,
              user_non_funding_ledger_updates} (fallback: POST {hl_api_url}/info)
  stripe      app.payments.stripe_pay.{create_topup_intent, verify_webhook, handle_event, StripeTopupConfig,
              default_gateway}; optional make_fee_lookup(gateway) when the Stripe fee is passed to the user
  usdc        app.payments.usdc.{build_topup_request, credit_from_detection};
              app.hl.deposits.detect_transfers(sender=, since_ms=) (fallback: /info userNonFundingLedgerUpdates)
  notifier    alerts table (in-app, same tx) + app.alerts.notifier.{Notifier, TelegramSink, Alert, Severity}
  sandbox     app.sandbox.validate.validate_source, app.sandbox.nocode.{validate_spec, compile_spec},
              app.sandbox.backtest.{fetch_market_data, HyperliquidInfoFetcher, backtest_on_data (dev only)};
              sandbox service POST {sandbox_url}/v1/backtest {source, data, params} with a Cloud Run ID token
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
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
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
    state = getattr(getattr(exc, "orig", None), "sqlstate", None)
    factory = _SQLSTATE_MAP.get(state or "")
    return factory() if factory else exc


class SqlDatabase:
    """Engine from app.db if it exposes get_engine(); else our own psycopg 3 engine."""

    def __init__(self, settings: Settings, *, statement_timeout_ms: int = 15_000) -> None:
        self._settings = settings
        self._engine: Any = None
        self._lock = threading.Lock()
        self._timeout = int(statement_timeout_ms)

    def engine(self) -> Any:
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    get_engine = _fn("app.db", "get_engine")
                    if get_engine is not None:
                        self._engine = get_engine()
                    else:
                        from sqlalchemy import create_engine
                        url = self._settings.database_url
                        if url.startswith("postgresql://"):
                            url = "postgresql+psycopg://" + url[len("postgresql://"):]
                        self._engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=5,
                                                     pool_recycle=1800, future=True)
        return self._engine

    @contextmanager
    def begin(self) -> Iterator[Any]:
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        try:
            with self.engine().begin() as conn:
                # Constant, not user input. Bounds every API transaction (fail fast instead of piling up).
                conn.execute(text("SET LOCAL statement_timeout = " + str(self._timeout)))
                yield conn
        except DBAPIError as e:
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
    def __init__(self, store: Any) -> None:
        self._store = store

    def ensure_account(self, conn: Any, code: str) -> None:
        fn = _fn("app.ledger", "ensure_account")
        if fn is not None:
            fn(conn, code)
            return
        kind, non_negative, owner = ledger_ops.account_spec(code)
        self._store.ensure_account(conn, code=code, kind=kind, owner_user_id=owner, non_negative=non_negative)

    def post(self, conn: Any, *, idempotency_key: str, kind: str, memo: str, entries: list[tuple[str, int]],
             created_by: str) -> str:
        fn = _fn("app.ledger", "post_transaction")
        if fn is not None:
            res = fn(conn, idempotency_key=idempotency_key, kind=kind, memo=memo, entries=entries,
                     created_by=created_by)
            return str(getattr(res, "tx_id", None) or (res.get("tx_id") if isinstance(res, dict) else res))
        return self._store.ledger_post(conn, idempotency_key=idempotency_key, kind=kind, memo=memo,
                                       created_by=created_by, entries=entries)

    def balance(self, conn: Any, account_code: str) -> int:
        return self._store.raw_balance(conn, account_code)


# =============================================================================================================
# Keys (ENCRYPT ONLY — the API process never builds a decryptor)
# =============================================================================================================
class _Encryptor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._enc: Any = None
        self._lock = threading.Lock()

    def get(self) -> Any:
        if self._enc is None:
            with self._lock:
                if self._enc is None:
                    self._enc = _require("app.security.kms").make_encryptor(self._settings)
        return self._enc


class AgentKeyAdapter:
    def __init__(self, enc: _Encryptor) -> None:
        self._enc = enc

    def generate_sealed(self, user_id: str) -> SealedAgentKey:
        sk = _require("app.security.agent_keys").generate_sealed_agent_key(self._enc.get(), user_id=user_id)
        return SealedAgentKey(agent_address=sk.address.lower(), key_ciphertext=bytes(sk.ciphertext),
                              kms_key_version=str(sk.key_version))


class CodeVaultAdapter:
    def __init__(self, enc: _Encryptor) -> None:
        self._enc = enc

    def seal(self, plaintext: bytes, aad: bytes) -> tuple[bytes, str]:
        blob = self._enc.get().seal(plaintext, aad)
        return bytes(blob.blob), str(blob.key_version)


# =============================================================================================================
# Hyperliquid: EIP-712 typed data (SPEC §6) and read-only /info
# =============================================================================================================
_EIP712_DOMAIN = [
    {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"},
]
_APPROVE_AGENT = [
    {"name": "hyperliquidChain", "type": "string"}, {"name": "agentAddress", "type": "address"},
    {"name": "agentName", "type": "string"}, {"name": "nonce", "type": "uint64"},
]
_APPROVE_BUILDER = [
    {"name": "hyperliquidChain", "type": "string"}, {"name": "maxFeeRate", "type": "string"},
    {"name": "builder", "type": "address"}, {"name": "nonce", "type": "uint64"},
]


def _typed(primary: str, fields: list[dict[str, str]], message: dict[str, Any], chain_hex: str) -> dict[str, Any]:
    return {
        "domain": {"name": "HyperliquidSignTransaction", "version": "1", "chainId": int(chain_hex, 16),
                   "verifyingContract": ZERO_ADDRESS},
        "types": {"EIP712Domain": _EIP712_DOMAIN, primary: fields},
        "primaryType": primary,
        "message": message,
    }


class TypedDataAdapter:
    def __init__(self, settings: Settings) -> None:
        self._mainnet = settings.hl_is_mainnet

    @property
    def _chain(self) -> str:
        return "Mainnet" if self._mainnet else "Testnet"

    def approve_agent(self, *, agent_address: str, agent_name: str, nonce: int, signature_chain_id: str) -> dict:
        chain = signature_chain_id.lower()
        fn = _fn("app.hl.typed_data", "approve_agent_typed_data")
        if fn is not None:
            td = fn(agent_address=agent_address, agent_name=agent_name, nonce=nonce, signature_chain_id=chain,
                    is_mainnet=self._mainnet)
        else:
            td = _typed("HyperliquidTransaction:ApproveAgent", _APPROVE_AGENT,
                        {"hyperliquidChain": self._chain, "agentAddress": agent_address, "agentName": agent_name,
                         "nonce": nonce}, chain)
        action = {"type": "approveAgent", "signatureChainId": chain, "hyperliquidChain": self._chain,
                  "agentAddress": agent_address, "agentName": agent_name, "nonce": nonce}
        return {"typed_data": td, "action": action, "nonce": nonce}

    def approve_builder_fee(self, *, builder: str, max_fee_rate: str, nonce: int, signature_chain_id: str) -> dict:
        chain = signature_chain_id.lower()
        fn = _fn("app.hl.typed_data", "approve_builder_fee_typed_data")
        if fn is not None:
            td = fn(builder=builder, max_fee_rate=max_fee_rate, nonce=nonce, signature_chain_id=chain,
                    is_mainnet=self._mainnet)
        else:
            td = _typed("HyperliquidTransaction:ApproveBuilderFee", _APPROVE_BUILDER,
                        {"hyperliquidChain": self._chain, "maxFeeRate": max_fee_rate, "builder": builder,
                         "nonce": nonce}, chain)
        action = {"type": "approveBuilderFee", "signatureChainId": chain, "hyperliquidChain": self._chain,
                  "maxFeeRate": max_fee_rate, "builder": builder, "nonce": nonce}
        return {"typed_data": td, "action": action, "nonce": nonce}

    def usd_send(self, *, destination: str, amount: str, time_ms: int, signature_chain_id: str) -> dict:
        chain = signature_chain_id.lower()
        usdc = _require("app.payments.usdc")
        td = usdc.usd_send_typed_data(destination=destination, amount=amount, time_ms=time_ms,
                                      signature_chain_id=chain, is_mainnet=self._mainnet)
        action = {"type": "usdSend", "signatureChainId": chain, "hyperliquidChain": self._chain,
                  "destination": destination, "amount": amount, "time": time_ms}
        return {"typed_data": td, "action": action, "nonce": time_ms}


class HlInfoAdapter:
    """Read-only Hyperliquid /info. Uses app.hl.info functions when present; else a minimal urllib client."""

    MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def __init__(self, settings: Settings, *, timeout: float = 8.0) -> None:
        self._url = settings.hl_api_url.rstrip("/") + "/info"
        self._timeout = timeout
        self._coins: tuple[float, set[str]] = (0.0, set())
        self._lock = threading.Lock()

    def _post(self, body: dict[str, Any]) -> Any:
        req = urllib.request.Request(self._url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:  # noqa: S310 - fixed https URL from config
                raw = r.read(self.MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ExternalServiceError("hyperliquid info unavailable", error=type(e).__name__) from None
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise ExternalServiceError("hyperliquid info response too large")
        try:
            return json.loads(raw)
        except ValueError:
            raise ExternalServiceError("hyperliquid info returned invalid JSON") from None

    def extra_agents(self, user: str) -> list[dict[str, Any]]:
        fn = _fn("app.hl.info", "extra_agents")
        rows = fn(user) if fn else self._post({"type": "extraAgents", "user": user})
        out = []
        for r in rows or []:
            if isinstance(r, dict) and isinstance(r.get("address"), str):
                out.append({"address": r["address"].lower(), "name": r.get("name") or "",
                            "validUntil": r.get("validUntil")})
        return out

    def max_builder_fee(self, user: str, builder: str) -> int:
        fn = _fn("app.hl.info", "max_builder_fee")
        val = fn(user, builder) if fn else self._post({"type": "maxBuilderFee", "user": user, "builder": builder})
        if isinstance(val, bool) or not isinstance(val, int):
            raise ExternalServiceError("unexpected maxBuilderFee response")
        return val

    def clearinghouse_state(self, user: str) -> dict[str, Any]:
        fn = _fn("app.hl.info", "clearinghouse_state")
        res = fn(user) if fn else self._post({"type": "clearinghouseState", "user": user})
        if not isinstance(res, dict):
            raise ExternalServiceError("unexpected clearinghouseState response")
        return res

    def sub_accounts(self, user: str) -> list[str]:
        fn = _fn("app.hl.info", "sub_accounts")
        if fn:
            return [a.lower() for a in fn(user)]
        rows = self._post({"type": "subAccounts", "user": user}) or []
        return [str(r["subAccountUser"]).lower() for r in rows if isinstance(r, dict) and r.get("subAccountUser")]

    def ledger_updates(self, user: str, start_ms: int) -> list[dict[str, Any]]:
        fn = _fn("app.hl.info", "user_non_funding_ledger_updates")
        rows = fn(user, start_ms) if fn else self._post(
            {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": int(start_ms)})
        return [r for r in (rows or []) if isinstance(r, dict)]

    @staticmethod
    def parse_usd_transfer(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
        """A USDC send between perp accounts. VERIFY field names against live data before go-live
        (newer API: delta.type "send" {user, destination, token, amount}; older: "internalTransfer" {usdc})."""
        d = entry.get("delta") or {}
        t = d.get("type")
        if t == "send" and str(d.get("token", "USDC")).upper() in ("USDC", ""):
            amount = d.get("amount") or d.get("usdcValue")
        elif t == "internalTransfer":
            amount = d.get("usdc")
        else:
            return None
        try:
            micro = to_micro(str(amount), rounding=ROUND_FLOOR)
        except (TypeError, ValueError):
            return None
        return {"tx_hash": str(entry.get("hash", "")).lower(), "from_address": str(d.get("user", "")).lower(),
                "to_address": str(d.get("destination", "")).lower(), "amount_micro": micro,
                "time_ms": int(entry.get("time") or 0), "token": "USDC"}

    def find_usd_send(self, *, sender: str, destination: str, amount_micro: int, tx_hash: str) -> bool:
        start = int((time.time() - 14 * 86400) * 1000)
        for e in self.ledger_updates(sender, start):
            t = self.parse_usd_transfer(e)
            if t and t["tx_hash"] == tx_hash.lower() and t["to_address"] == destination.lower() \
                    and t["amount_micro"] == amount_micro:
                return True
        return False

    def known_coins(self) -> set[str]:
        fn = _fn("app.hl.markets", "known_coins")
        if fn:
            return set(fn())
        ts, coins = self._coins
        if coins and time.time() - ts < 300:
            return coins
        out: set[str] = set()
        meta = self._post({"type": "meta"}) or {}
        out |= {u["name"] for u in meta.get("universe", []) if isinstance(u, dict) and u.get("name")}
        for dex in self._post({"type": "perpDexs"}) or []:
            if isinstance(dex, dict) and dex.get("name"):
                dm = self._post({"type": "meta", "dex": dex["name"]}) or {}
                for u in dm.get("universe", []):
                    if isinstance(u, dict) and u.get("name"):
                        n = u["name"]
                        out.add(n if ":" in n else f"{dex['name']}:{n}")
        with self._lock:
            self._coins = (time.time(), out)
        return out


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

    def detect(self, *, sender: str, since_ms: Optional[int]) -> list[Any]:
        fn = _fn("app.hl.deposits", "detect_transfers")
        start = since_ms if since_ms else int((time.time() - 2 * 86400) * 1000)
        if fn is not None:
            return list(fn(sender=sender, since_ms=start))
        treasury = self._s.treasury_address.lower()
        out = []
        for e in self._hl.ledger_updates(sender, start):
            t = HlInfoAdapter.parse_usd_transfer(e)
            if t and t["to_address"] == treasury and t["from_address"] == sender.lower():
                out.append(t)
        return out

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

    def notify(self, conn: Any, *, user_id: Optional[str], severity: str, kind: str, payload: dict[str, Any]) -> None:
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
    def __init__(self, settings: Settings, cfg: ApiConfig) -> None:
        self._s = settings
        self._cfg = cfg

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
        data = bt.fetch_market_data(smeta, bt.HyperliquidInfoFetcher(self._s.hl_api_url))
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
        req = urllib.request.Request(url + "/v1/backtest", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:  # noqa: S310 - URL from config
                raw = r.read(16 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            if e.code == 422:
                try:
                    detail = json.loads(e.read(65536) or b"{}")
                except ValueError:
                    detail = {}
                raise ValidationFailed("backtest rejected the strategy", sandbox=detail.get("error")) from None
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

    def self_referral_reasons(self, *, referrer: tuple[str, list[str], list[Optional[str]]],
                              referee: tuple[str, list[str], list[Optional[str]]]) -> tuple[str, ...]:
        r = _require("app.domain.referrals")
        a = r.ReferralIdentity.of(referrer[0], wallets=referrer[1], devices=referrer[2])
        b = r.ReferralIdentity.of(referee[0], wallets=referee[1], devices=referee[2])
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


# =============================================================================================================
# Factory
# =============================================================================================================
def build_services(settings: Optional[Settings] = None) -> Services:
    s = settings or get_settings()
    cfg = api_config(s)
    from app.api.store import SqlStore  # imports SQLAlchemy; kept lazy so tests with fakes need no DB libs
    store = SqlStore()
    enc = _Encryptor(s)
    hl = HlInfoAdapter(s)
    return Services(
        settings=s,
        db=SqlDatabase(s),
        store=store,
        auth=FirebaseAuthAdapter(s),
        audit=SqlAuditAdapter(store),
        ledger=LedgerAdapter(store),
        agent_keys=AgentKeyAdapter(enc),
        typed_data=TypedDataAdapter(s),
        hl=hl,
        stripe=StripeAdapter(s),
        usdc=UsdcAdapter(s, hl),
        notifier=NotifierAdapter(s, store),
        sandbox=SandboxAdapter(s, cfg),
        code_vault=CodeVaultAdapter(enc),
        kyc=KycAdapter(),
        jobs=JobsAdapter(),
        ratelimit=RateLimitAdapter(),
        oidc=GoogleOidcAdapter(),
        wallet_sig=WalletSigAdapter(),
        domain=DomainAdapter(s),
        clock=lambda: datetime.now(timezone.utc),
    )


__all__ = ["build_services", "map_db_error", "JOB_ENTRYPOINTS", "timedelta"]

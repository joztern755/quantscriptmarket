"""Internal job entrypoints (``/internal/*`` on the executor service; SPEC §2, §8) and the executor's composition root.

Contract with ``app.api.adapters.JOB_ENTRYPOINTS``: every job is called ``fn(db=<DatabasePort>, now=<aware UTC
datetime>, **params)`` and returns a JSON-able dict. ``db`` is the API's ``SqlDatabase`` (``begin()`` → SQLAlchemy
connection; on the executor service it connects as ``app_executor``). ``latest_reconciliation(conn)`` is called with
an open connection by the admin console.

Scheduler cadence (Cloud Scheduler → OIDC → executor service; source of truth: infra/gcp/env.sh SCHEDULER_SPEC):
  /internal/tick            * * * * *      1. ``run_creator_signals`` — runs each listed creator version in the sandbox
                            (every minute)    ONCE per closed bar of its TIMEFRAME (1h / 4h / 1d): it acts only when
                                              the bar has closed + ``bar_settle_seconds`` (30 s) and no signal exists
                                              for it yet, waits up to ``missing_bar_grace_seconds`` for the closed
                                              candle to be served; every other minute it is a no-op;
                                           2. the executor tick (signals → jittered, guarded IOC orders; closing
                                              subscriptions flattened; see app.execution.executor).
  /internal/settle-daily    30 0 * * *     ``settle_daily`` — after the fills/funding sync of the previous day.
                            30 2,6 * * *   Router param ``settle_date`` = the trading day being settled (default
                            (settle-daily-  yesterday); PnL cut-off = settle_date + 1 day 00:00 UTC (never after now).
                            retry)         A subscription whose trading address fills-ingest / funding-scan have not
                                           synced past the cut-off is DEFERRED (no posting; ops event
                                           ``settlement_deferred``); the retry slots settle it (app.execution.settlement).
  /internal/reconcile       0 * * * *      ``reconcile`` (hourly) — positions vs targets, builder fees DB vs on-chain,
                                           treasury; alert dedup keys are per day, every run's report is stored.
  /internal/verify-chain    40 3 * * *     ``verify_chain`` (REVIEW_MONEY M7(a), L4, L5) — verify_chain() over the ledger
                                           transactions, audit log and ledger accounts chains plus the running
                                           balances, verify_chain_anchors() (anchored heads unchanged), then stores
                                           today's heads (ledger_chain_anchors) and sends them to ops (Telegram +
                                           email) as the external anchor. Any problem → critical ops alert.
  /internal/referral-tiers  15 1 * * *     ``referral_tiers`` — re-evaluates users.referral_tier (SPEC §1.2). Runs
                                           AFTER the 00:30 settlement on purpose: the NEXT day's settlement uses the
                                           new tier for its builder-fee referral split.
All jobs are idempotent (ledger idempotency keys, UNIQUE constraints, cursors); settle/reconcile/referral-tiers take
a job-level advisory lock and the tick locks per subscription (and per creator version), so Scheduler retries and
overlapping runs are safe.

Hyperliquid rate budget: the default ``InfoClient`` carries an ``app.hl.budget.BudgetHook`` over the shared DB
budget (``hl_rate_budget``; config ``HlLimits``) bound to the first job's database (``Runtime.bind_db``). Tick reads
are charged to the priority TICK pool (never blocked); reconcile runs in the JOBS pool (waits / gives up like the
data jobs, which pace themselves in app/jobs_data).

Composition: ``Runtime`` builds every dependency from ``Settings`` (Hyperliquid info client + readers, SDK gateway
factory with our builder code, KMS decryptors, notifier, sandbox client, jitter, planner, clock). Every piece is a
constructor argument, so tests inject fakes (``Runtime(settings, info=FakeInfo(...), gateways=FakeGatewayFactory(...),
...)``) and pass ``runtime=`` to the job functions. The process-wide default runtime is built lazily
(``get_runtime`` / ``set_runtime``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Mapping

from app.config import Economics, get_settings
from app.errors import ExternalServiceError, ValidationFailed
from app.https_only import https_open
from app.logging import get_logger
from app.money import BPS

from .executor import Executor, ExecutorConfig
from .pg import (
    PgAlertRepo,
    PgChainVerifier,
    PgCreatorSignalRepo,
    PgDatabase,
    PgFlagRepo,
    PgLedger,
    PgLockProvider,
    PgMarketPauseFlags,
    PgPendingReleaser,
    PgReconcileRepo,
    PgReconciliationStore,
    PgReferralLookup,
    PgReferralTierRepo,
    PgSettlementRepo,
    PgSignalRepo,
    PgSubscriptionRepo,
    PgUnitOfWork,
    PgUserEvents,
    open_time_ms,
)
from .ports import AlertEvent
from .reconcile import ReconcileConfig, Reconciler
from .settlement import Settlement
from .treasury_books import HlBuilderRewardsReader, HlRewardsSchema, HlTreasuryReader, stripe_reader_from_settings
from .wiring import (
    CatalogMarketData,
    DomainBilling,
    DomainFees,
    DomainJitter,
    DomainProfitShare,
    NotifierAlertSink,
    RiskPlanner,
    SystemClock,
)

__all__ = [
    "run_tick", "run_creator_signals", "settle_daily", "reconcile", "referral_tiers", "latest_reconciliation",
    "verify_chain", "OpsAnchorPublisher",
    "Runtime", "get_runtime", "set_runtime", "SandboxClient", "HlBuilderRewardsReader", "HlTreasuryReader",
    "build_notifier", "jitter_salt", "INTERVAL_MS", "weight_to_bps",
]

log = get_logger("app.execution.jobs")

UTC = timezone.utc
INTERVAL_MS = {"1h": 3_600_000, "4h": 4 * 3_600_000, "1d": 86_400_000}


# ======================================================================================================== helpers

def _aware(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def jitter_salt(settings: Any) -> bytes:
    """Secret per-deployment salt for execution delays / ordering (SPEC §5.8), derived from the audit pepper
    (Secret Manager) with domain separation. Dev/test without a pepper use a fixed, clearly non-prod salt."""
    raw = getattr(settings, "audit_pepper_b64", "") or ""
    pepper = base64.b64decode(raw) if raw else b""
    if not pepper:
        if getattr(settings, "is_prod", False):
            raise RuntimeError("AUDIT_PEPPER_B64 is required in prod (jitter salt)")
        pepper = b"aijalon-dev-only-jitter-pepper"
    return hmac.new(pepper, b"aijalon/execution/jitter-salt/v1", hashlib.sha256).digest()


def weight_to_bps(weight: Any, max_leverage: int) -> int:
    """Sandbox float weight → integer bps (× 10000), truncated toward zero (never more exposure than asked)."""
    if isinstance(weight, bool) or not isinstance(weight, (int, float, str, Decimal)):
        raise ValidationFailed("weight must be a number")
    d = Decimal(str(weight))
    if not d.is_finite():
        raise ValidationFailed("weight must be finite")
    bps = int((d * BPS).to_integral_value(rounding=ROUND_DOWN))
    cap = int(max_leverage) * BPS
    if abs(bps) > cap:
        raise ValidationFailed("weight above the version's MAX_LEVERAGE", weight=str(weight))
    return bps


# ======================================================================================================== on-chain readers
# HlBuilderRewardsReader (builder rewards: cumulative, still claimable, claims — REVIEW_MONEY M7(b)) and HlTreasuryReader
# (perp + spot USDC + each trusted builder dex — M7(g)) live in app.execution.treasury_books; re-exported here.


# ======================================================================================================== sandbox client

class SandboxClient:
    """POST ``{sandbox_url}/run`` (app/sandbox/service.py). Auth: ``X-Sandbox-Secret`` + a Google-signed ID token
    for Cloud Run IAM (audience = service URL, ``google-auth``; import-guarded: required in prod, optional in dev)."""

    def __init__(self, url: str, secret: str, *, require_id_token: bool, timeout: float = 60.0,
                 token_provider: Callable[[str], str | None] | None = None,
                 opener: Callable[..., Any] = https_open, max_response_bytes: int = 1 << 20) -> None:
        self.url = (url or "").rstrip("/")
        self.secret = secret or ""
        self.require_id_token = require_id_token
        self.timeout = timeout
        self._token_provider = token_provider or self._google_id_token
        self._open = opener
        self._max = max_response_bytes

    @staticmethod
    def _google_id_token(audience: str) -> str | None:
        try:
            from google.auth.transport.requests import Request as GRequest  # type: ignore[import-not-found]
            from google.oauth2 import id_token  # type: ignore[import-not-found]
        except ImportError:
            return None
        return id_token.fetch_id_token(GRequest(), audience)

    def _token(self) -> str | None:
        try:
            tok = self._token_provider(self.url)
        except Exception as e:  # noqa: BLE001 - metadata server / credentials unavailable
            if self.require_id_token:
                raise ExternalServiceError("cannot obtain sandbox identity token", error=type(e).__name__) from None
            return None
        if not tok and self.require_id_token:
            raise ExternalServiceError("cannot obtain sandbox identity token (google-auth missing)")
        return tok or None

    def run(self, source: str, bars: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
        if not self.url:
            raise ExternalServiceError("sandbox_url not configured")
        if not self.secret:
            raise ExternalServiceError("sandbox shared secret not configured")
        headers = {"Content-Type": "application/json", "X-Sandbox-Secret": self.secret}
        tok = self._token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        body = json.dumps({"source": source, "bars": bars, "now_ms": int(now_ms)}, separators=(",", ":")).encode()
        req = urllib.request.Request(self.url + "/run", data=body, method="POST", headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as r:  # default opener: https only
                raw = r.read(self._max + 1)
        except urllib.error.HTTPError as e:
            detail: dict[str, Any] = {}
            try:
                detail = json.loads(e.read(65536) or b"{}")
            except ValueError:
                pass
            raise ExternalServiceError("sandbox run failed", status=e.code,
                                       sandbox_error=str(detail.get("error") or "")[:64],
                                       sandbox_message=str(detail.get("message") or "")[:200]) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:  # ValueError: non-https URL
            raise ExternalServiceError("sandbox unavailable", error=type(e).__name__) from None
        if len(raw) > self._max:
            raise ExternalServiceError("sandbox response too large")
        try:
            out = json.loads(raw)
        except ValueError:
            raise ExternalServiceError("sandbox returned invalid JSON") from None
        if not isinstance(out, dict) or not isinstance(out.get("weights"), dict):
            raise ExternalServiceError("sandbox returned no weights")
        return out


# ======================================================================================================== notifier

_DEDUPE_LOCK = threading.Lock()
_DEDUPE: Any = None


def _shared_dedupe() -> Any:
    global _DEDUPE
    with _DEDUPE_LOCK:
        if _DEDUPE is None:
            from app.alerts.notifier import InMemoryDedupeStore

            _DEDUPE = InMemoryDedupeStore()
        return _DEDUPE


def build_notifier(settings: Any, db: PgDatabase, *, dedupe_store: Any = None) -> Any:
    """Notifier for the executor service: in-app rows (alerts table), email (Resend) to OPS for warn/critical,
    Telegram ops page for critical, auto-pause (new_entries_paused:{coin}) for critical market alerts.
    The dedupe store is per process; alert ROWS are additionally deduped across instances (PgAlertRepo).
    User Telegram/email is NOT sent here: app.alerts.delivery.deliver_outbox (/v1/internal/deliver-alerts, every
    minute) delivers every user `alerts` row per the email policy, mutes and confirmed contacts (0007)."""
    from app.alerts import notifier as n
    from app.alerts.user_sinks import NoUserEmailContacts

    email = None
    if getattr(settings, "email_provider_api_key", ""):
        email = n.EmailSink(n.ResendProvider(settings.email_provider_api_key, settings.email_from))
    telegram = None
    if getattr(settings, "telegram_bot_token", "") and getattr(settings, "telegram_ops_chat_id", ""):
        telegram = n.TelegramSink(settings.telegram_bot_token, settings.telegram_ops_chat_id)
    return n.Notifier(in_app=n.InAppSink(PgAlertRepo(db)), email=email, telegram=telegram,
                      contacts=NoUserEmailContacts(), ops_emails=getattr(settings, "ops_emails", ()),
                      flags=PgMarketPauseFlags(db), dedupe_store=dedupe_store or _shared_dedupe())


# ======================================================================================================== runtime

class Runtime:
    """Composition root. Every argument overrides the settings-derived default (tests inject fakes).

    Factories taking the job's ``PgDatabase``: ``key_provider_factory(db)``, ``alert_sink_factory(db)``.
    """

    def __init__(self, settings: Any = None, *, info: Any = None, market_source: Any = None, market_data: Any = None,
                 positions: Any = None, order_status: Any = None, gateways: Any = None,
                 key_provider_factory: Callable[[PgDatabase], Any] | None = None,
                 code_decryptor: Any = None, alert_sink_factory: Callable[[PgDatabase], Any] | None = None,
                 clock: Any = None, jitter: Any = None, planner: Any = None,
                 executor_config: ExecutorConfig | None = None, sandbox: Any = None, builder_rewards: Any = None,
                 treasury: Any = None, economics: Economics | None = None,
                 reconcile_config: ReconcileConfig | None = None, bar_settle_seconds: int = 30,
                 missing_bar_grace_seconds: int = 600, creator_signal_budget_seconds: float = 20.0,
                 anchor_publisher: Any = None, stripe_clearing: Any = None) -> None:
        self.settings = settings or get_settings()
        s = self.settings
        self.economics = economics or getattr(s, "economics", None) or Economics()
        self.clock = clock or SystemClock()
        self._info = info
        self._market_source = market_source
        self._market_data = market_data
        self._positions = positions
        self._order_status = order_status
        self._gateways = gateways
        self._key_provider_factory = key_provider_factory
        self._code_decryptor = code_decryptor
        self._alert_sink_factory = alert_sink_factory
        self._jitter = jitter
        self._planner = planner
        self.executor_config = executor_config or ExecutorConfig.from_settings(s)
        self._sandbox = sandbox
        self._builder_rewards = builder_rewards
        self._treasury = treasury
        self.reconcile_config = reconcile_config or ReconcileConfig()
        self.bar_settle_seconds = bar_settle_seconds
        self.missing_bar_grace_seconds = missing_bar_grace_seconds
        self.creator_signal_budget_seconds = creator_signal_budget_seconds
        self._traded_coins: tuple[str, ...] = ()
        self._agent_decryptor: Any = None
        self._lock = threading.RLock()
        self._rate_budget: Any = None
        self._anchor_publisher = anchor_publisher
        self.stripe_clearing = stripe_clearing       # StripeClearingReader (REVIEW_MONEY M7(c)); None: from settings

    def anchor_publisher(self) -> Any:
        if self._anchor_publisher is None:
            self._anchor_publisher = OpsAnchorPublisher(self.settings)
        return self._anchor_publisher

    def bind_db(self, db: PgDatabase) -> None:
        """Give the shared Hyperliquid rate budget its database (first job call; all jobs share one database)."""
        if self._rate_budget is not None or self._info is not None and not hasattr(self._info, "rate_hook"):
            return
        limits = getattr(self.settings, "hl_limits", None)
        if limits is None or not getattr(limits, "shared_budget", False):
            return
        with self._lock:
            if self._rate_budget is None:
                from app.hl.budget import HlRateBudget

                self._rate_budget = HlRateBudget(db, limits)

    @property
    def rate_budget(self) -> Any:
        return self._rate_budget

    # ---- Hyperliquid ------------------------------------------------------------------------------------------
    @property
    def info(self) -> Any:
        if self._info is None:
            with self._lock:
                if self._info is None:
                    from app.hl.budget import BudgetHook
                    from app.hl.info import InfoClient

                    limits = getattr(self.settings, "hl_limits", None)
                    hook = BudgetHook(lambda: self._rate_budget, limits) if limits is not None else None
                    self._info = InfoClient(self.settings.hl_api_url, rate_hook=hook)
        return self._info

    @property
    def market_source(self) -> Any:
        if self._market_source is None:
            from app.hl.readers import HlMarketData

            self._market_source = HlMarketData(self.info)
        return self._market_source

    @property
    def market_data(self) -> Any:
        if self._market_data is None:
            self._market_data = CatalogMarketData(self.market_source)
        return self._market_data

    @property
    def positions(self) -> Any:
        if self._positions is None:
            from app.hl.readers import HlPositionReader

            self._positions = HlPositionReader(self.info)
        return self._positions

    @property
    def order_status(self) -> Any:
        if self._order_status is None:
            from app.hl.readers import HlOrderStatusReader

            self._order_status = HlOrderStatusReader(self.info)
        return self._order_status

    def _catalog_for_gateway(self) -> Any:
        return self.market_source.catalog(self._traded_coins)

    @property
    def gateways(self) -> Any:
        if self._gateways is None:
            from app.hl.client import BuilderCode, SdkGatewayFactory

            builder = BuilderCode(self.settings.builder_address, self.economics.builder_fee_tenths_bp)
            self._gateways = SdkGatewayFactory(self._catalog_for_gateway, builder, base_url=self.settings.hl_api_url)
        return self._gateways

    # ---- secrets ----------------------------------------------------------------------------------------------
    def key_provider(self, db: PgDatabase) -> Any:
        if self._key_provider_factory is not None:
            return self._key_provider_factory(db)
        from .keys import DbAgentKeyProvider

        with self._lock:
            if self._agent_decryptor is None:
                from app.security.kms import make_decryptor

                self._agent_decryptor = make_decryptor(self.settings)   # one per process, agent keys only
        dec = self._agent_decryptor
        return DbAgentKeyProvider(db, decryptor_factory=lambda: dec)

    @property
    def code_decryptor(self) -> Any:
        if self._code_decryptor is None:
            from .keys import CreatorCodeDecryptor

            self._code_decryptor = CreatorCodeDecryptor(settings=self.settings)   # dedicated instance
        return self._code_decryptor

    # ---- alerts / misc ----------------------------------------------------------------------------------------
    def alerts(self, db: PgDatabase) -> Any:
        if self._alert_sink_factory is not None:
            return self._alert_sink_factory(db)
        return NotifierAlertSink(build_notifier(self.settings, db))

    @property
    def jitter(self) -> Any:
        if self._jitter is None:
            self._jitter = DomainJitter(jitter_salt(self.settings), self.settings.risk.jitter_max_seconds)
        return self._jitter

    @property
    def planner(self) -> Any:
        if self._planner is None:
            self._planner = RiskPlanner(self.settings.risk, grace_hours=self.economics.past_due_grace_hours)
        return self._planner

    @property
    def sandbox(self) -> Any:
        if self._sandbox is None:
            self._sandbox = SandboxClient(self.settings.sandbox_url, self.settings.sandbox_shared_secret,
                                          require_id_token=bool(self.settings.is_prod))
        return self._sandbox

    @property
    def builder_rewards(self) -> Any:
        if self._builder_rewards is None:
            self._builder_rewards = HlBuilderRewardsReader(
                self.info, self.settings.builder_address, treasury_address=self.settings.treasury_address,
                schema=HlRewardsSchema.from_settings(self.settings))
        return self._builder_rewards

    @property
    def treasury(self) -> Any:
        if self._treasury is None:
            self._treasury = HlTreasuryReader(self.info, self.settings.treasury_address)
        return self._treasury

    # ---- composed services ------------------------------------------------------------------------------------
    def executor(self, db: PgDatabase, *, alerts: Any = None, now: datetime | None = None) -> Executor:
        """``now``: the tick's time — the alert-contacts entries gate is evaluated at it (default: the clock)."""
        subs = PgSubscriptionRepo(db, clock=(lambda: now) if now is not None else self.clock)
        try:
            self._traded_coins = tuple(subs.traded_markets())
        except Exception:  # noqa: BLE001 - only narrows the gateway catalog; fall back to the validator dex
            log.warning("traded_markets_unavailable", exc_info=True)
        return Executor(
            signals=PgSignalRepo(db), subscriptions=subs, market_data=self.market_data, positions=self.positions,
            order_status=self.order_status, keys=self.key_provider(db), gateways=self.gateways,
            flags=PgFlagRepo(db), alerts=alerts or self.alerts(db), clock=self.clock, locks=PgLockProvider(db),
            planner=self.planner, jitter=self.jitter, config=self.executor_config)

    def settlement(self, db: PgDatabase, *, alerts: Any = None) -> Settlement:
        grace = self.economics.past_due_grace_hours
        return Settlement(
            repo=PgSettlementRepo(db, self.economics), ledger=PgLedger(db), uow=PgUnitOfWork(db),
            profit_share=DomainProfitShare(self.economics), fees=DomainFees(self.economics),
            billing=DomainBilling(grace), referrals=PgReferralLookup(db, self.economics),
            alerts=alerts or self.alerts(db), clock=self.clock, events=PgUserEvents(db),
            pending=PgPendingReleaser(db))

    def treasury_for(self, db: PgDatabase) -> Any:
        """The treasury reader of one reconcile run: an injected one as is; else perp + spot USDC + each ACTIVE
        trusted builder dex (read from ``trusted_dexes`` at run time — REVIEW_MONEY M7(g))."""
        if self._treasury is not None:
            return self._treasury
        from app.strategies.dexes import VALIDATOR_DEX, load_trusted

        return HlTreasuryReader(self.info, self.settings.treasury_address,
                                dexes=lambda: sorted(d for d in load_trusted(db) if d != VALIDATOR_DEX))

    def stripe_reader(self) -> Any:
        """REVIEW_MONEY M7(c): the injected ``stripe_clearing`` reader, else a Stripe balance reader when this service
        has a Stripe key (None → reconcile reports ``not_configured``)."""
        if self.stripe_clearing is None and getattr(self.settings, "stripe_secret_key", ""):
            with self._lock:
                if self.stripe_clearing is None:
                    self.stripe_clearing = stripe_reader_from_settings(self.settings)
        return self.stripe_clearing

    def reconciler(self, db: PgDatabase, *, alerts: Any = None) -> Reconciler:
        repo = PgReconcileRepo(db, self.economics)
        rewards = self.builder_rewards
        claims = rewards if hasattr(rewards, "builder_reward_claims") else None   # M7(b): same reader
        return Reconciler(repo=repo, positions=self.positions, builder_rewards=rewards,
                          treasury=self.treasury_for(db), ledger=PgLedger(db), alerts=alerts or self.alerts(db),
                          config=self.reconcile_config, solvency=repo, stripe=self.stripe_reader(),
                          builder_claims=claims, uow=PgUnitOfWork(db))


_RUNTIME: Runtime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime() -> Runtime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = Runtime()
        return _RUNTIME


def set_runtime(runtime: Runtime | None) -> None:
    """Install (or with None: reset) the process-wide runtime."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = runtime


def _db(db: Any) -> PgDatabase:
    if db is None:
        raise ValueError("db is required")
    return db if isinstance(db, PgDatabase) else PgDatabase(db)


def _emit(alerts: Any, severity: str, kind: str, payload: dict[str, Any], *, dedup: str | None = None,
          user_id: str | None = None) -> None:
    try:
        alerts.emit(AlertEvent(severity=severity, kind=kind, payload=payload, user_id=user_id, dedup_key=dedup))
    except Exception:  # noqa: BLE001
        log.error("alert_emit_failed", exc_info=True, extra={"fields": {"kind": kind}})


# ======================================================================================================== jobs

def run_tick(*, db: Any, now: datetime, runtime: Runtime | None = None, creator_signals: bool = True,
             agent_keygen: bool = True) -> dict[str, Any]:
    """/internal/tick (every minute): pending agent-key requests (executor keygen), creator signals for bars that just
    closed, then the executor tick."""
    rt = runtime or get_runtime()
    now = _aware(now)
    pdb = _db(db)
    rt.bind_db(pdb)
    alerts = rt.alerts(pdb)
    out: dict[str, Any] = {}
    if agent_keygen:
        # agent keys requested since the last run (POST /v1/agents) are generated HERE, in the executor (0016); a
        # small budget so trading is never delayed, and any failure is left to the every-minute attest-agents job.
        try:
            kg = tick_generate_agents(db=pdb, now=now, runtime=rt)
            if kg:
                out["agent_keygen"] = kg
        except Exception as e:  # noqa: BLE001 - never block trading on key generation
            log.error("tick_agent_keygen_failed", extra={"fields": {"error": type(e).__name__}})
            out["agent_keygen"] = {"error": type(e).__name__}
    if creator_signals:
        try:
            out["creator_signals"] = run_creator_signals(db=pdb, now=now, runtime=rt, alerts=alerts)
        except Exception as e:  # noqa: BLE001 - never block the executor on the sandbox
            log.error("creator_signals_failed", exc_info=True)
            out["creator_signals"] = {"error": type(e).__name__}
    report = rt.executor(pdb, alerts=alerts, now=now).run_tick(now)
    out.update(report.as_dict())
    return out


def settle_daily(*, db: Any, now: datetime, settle_date: date | str | None = None,
                 runtime: Runtime | None = None) -> dict[str, Any]:
    """/internal/settle-daily (00:30 UTC; retried by ``settle-daily-retry`` at 02:30 and 06:30). ``settle_date`` = the
    trading day to settle (router default: yesterday); its PnL cut-off is the next midnight, clamped to today's
    midnight. Without it: cut-off = today 00:00 UTC. Subscriptions whose data (fills-ingest, funding-scan) is not
    synced past the cut-off are deferred (``deferred`` in the result); re-running is always safe (idempotent)."""
    rt = runtime or get_runtime()
    now = _aware(now)
    pdb = _db(db)
    if isinstance(settle_date, str):
        settle_date = date.fromisoformat(settle_date)
    cutoff_day = now.date() if settle_date is None else min(settle_date + timedelta(days=1), now.date())
    with PgLockProvider(pdb).try_lock("job:settle-daily") as held:
        if not held:
            return {"skipped": "another settlement is running", "cutoff_day": cutoff_day.isoformat()}
        report = rt.settlement(pdb).settle_daily(cutoff_day, now)
    out = report.as_dict()
    out["status_changes"] = [list(x) for x in out.get("status_changes", [])]
    out["business_day"] = (cutoff_day - timedelta(days=1)).isoformat()
    out["cutoff"] = datetime.combine(cutoff_day, datetime.min.time(), tzinfo=UTC).isoformat()
    return out


def reconcile(*, db: Any, now: datetime, runtime: Runtime | None = None) -> dict[str, Any]:
    """/internal/reconcile (hourly, :00). The report is stored (reconciliation_reports) for the admin console; alert
    dedup keys are per UTC day, so a persisting mismatch alerts once a day, not every hour. Its Hyperliquid reads are
    charged to the shared rate budget's JOBS pool (the tick keeps its reserve)."""
    rt = runtime or get_runtime()
    now = _aware(now)
    pdb = _db(db)
    date_key = now.date().isoformat()
    rt.bind_db(pdb)
    from app.hl.budget import POOL_JOBS, budget_pool

    with PgLockProvider(pdb).try_lock("job:reconcile") as held:
        if not held:
            return {"skipped": "another reconciliation is running"}
        with budget_pool(POOL_JOBS):
            report = rt.reconciler(pdb).run(date_key).as_dict()
        PgReconciliationStore(pdb).save(date_key, report, now)
    return {"date_key": date_key, **report}


class OpsAnchorPublisher:
    """Sends the daily chain heads OFF the database (REVIEW_MONEY M7(a)): the ops Telegram chat (plain text, the heads
    are not secret) — the ops email copy goes through the notifier (warn alert ``ledger_chain_anchor``). Whoever can
    rewrite the database cannot rewrite those messages, so a truncated or re-hashed chain is detectable later."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def publish(self, text: str) -> dict[str, Any]:
        out: dict[str, Any] = {"telegram": False}
        token = getattr(self.settings, "telegram_bot_token", "")
        chat = getattr(self.settings, "telegram_ops_chat_id", "")
        if token and chat:
            from app.alerts.notifier import TelegramSink

            try:
                TelegramSink(token, chat).send_text(text)
                out["telegram"] = True
            except Exception as e:  # noqa: BLE001 - the anchor row is still stored; the email copy still goes out
                out["telegram_error"] = type(e).__name__
        return out


def verify_chain(*, db: Any, now: datetime, runtime: Runtime | None = None) -> dict[str, Any]:
    """/internal/verify-chain (daily 03:40 UTC; REVIEW_MONEY M7(a), L4, L5). Verifies every hash chain (ledger
    transactions, audit log, ledger accounts), the running balances against the full Σ of entries, and that every
    previously anchored head is still there unchanged; then anchors today's heads (DB row + ops Telegram/email).
    Problems → one critical ops alert per day (``ledger_chain_broken``). Idempotent per UTC day."""
    rt = runtime or get_runtime()
    now = _aware(now)
    pdb = _db(db)
    alerts = rt.alerts(pdb)
    verifier = PgChainVerifier(pdb)
    day = now.date()
    with PgLockProvider(pdb).try_lock("job:verify-chain") as held:
        if not held:
            return {"skipped": "another chain verification is running"}
        problems = verifier.problems()
        anchor_problems = verifier.anchor_problems()
        heads = verifier.heads()
        text = "aijalon ledger anchor " + day.isoformat() + "\n" + "\n".join(
            f"{h['chain']} seq={h['seq']} hash={h['hash']}" for h in heads)
        if problems or anchor_problems:
            text += f"\nVERIFY FAILED: {len(problems)} chain problem(s), {len(anchor_problems)} anchor problem(s)"
        published: dict[str, Any] = {}
        try:
            published = dict(rt.anchor_publisher().publish(text))
        except Exception as e:  # noqa: BLE001
            published = {"error": type(e).__name__}
        _emit(alerts, "warn", "ledger_chain_anchor", {"date": day.isoformat(), "heads": heads,
                                                      "ok": not (problems or anchor_problems)},
              dedup=f"ledger_chain_anchor:{day.isoformat()}")
        published["email"] = "ops_alert"
        stored = verifier.store_anchor(day, heads, published)
        if problems or anchor_problems:
            _emit(alerts, "critical", "ledger_chain_broken", {"problems": problems[:20],
                                                              "anchor_problems": anchor_problems[:20]},
                  dedup=f"ledger_chain_broken:{day.isoformat()}")
    log.info("verify_chain_done", extra={"fields": {"problems": len(problems), "anchor_problems": len(anchor_problems),
                                                    "stored": stored}})
    return {"ok": not (problems or anchor_problems), "problems": problems[:50], "anchor_problems": anchor_problems[:50],
            "heads": heads, "anchors_stored": stored, "published": published, "date": day.isoformat()}


def latest_reconciliation(conn: Any = None, *, db: Any = None, now: datetime | None = None) -> dict[str, Any] | None:
    """Latest stored reconciliation report (admin console). ``conn``: an open SQLAlchemy connection (API) or a
    SqlRunner; ``db``: a DatabasePort."""
    target = conn if conn is not None else db
    if target is None:
        raise ValueError("conn or db required")
    return PgReconciliationStore(_db(target)).latest()


def referral_tiers(*, db: Any, now: datetime, runtime: Runtime | None = None, window_days: int = 30) -> dict[str, Any]:
    """/internal/referral-tiers (daily 01:15 UTC — AFTER the 00:30 settlement on purpose, so the next day's
    settlement uses the new tier): SPEC §1.2 tiers on trailing-30-day stats of each referrer's referred users (active
    users OR referred notional) → users.referral_tier."""
    from app.domain.referrals import evaluate_tier

    rt = runtime or get_runtime()
    now = _aware(now)
    pdb = _db(db)
    repo = PgReferralTierRepo(pdb)
    since = now - timedelta(days=int(window_days))
    evaluated = 0
    changes: list[dict[str, Any]] = []
    with PgLockProvider(pdb).try_lock("job:referral-tiers") as held:
        if not held:
            return {"skipped": "another referral-tier run is in progress"}
        for r in repo.referrer_stats(since, now):
            evaluated += 1
            tier = evaluate_tier(int(r["active_users"]), int(r["notional_micro"]), rt.economics.referral_tiers)
            if tier.name != r["tier"] and repo.set_tier(r["referrer_id"], tier.name):
                changes.append({"user_id": r["referrer_id"], "from": r["tier"], "to": tier.name,
                                "active_users": int(r["active_users"]), "notional_micro": int(r["notional_micro"])})
    log.info("referral_tiers_done", extra={"fields": {"evaluated": evaluated, "changed": len(changes)}})
    return {"evaluated": evaluated, "changed": len(changes), "changes": changes[:200],
            "window_start": since.isoformat()}


# ---------------------------------------------------------------------------------------------------- creator signals

def _bars_for(rt: Runtime, repo: PgCreatorSignalRepo, coin: str, interval: str, lookback: int, bar_close_ms: int,
              use_stored: bool) -> list[dict[str, Any]]:
    """Closed bars (open time t, t + interval ≤ bar_close) oldest → newest, at most ``lookback``. Stored candles
    first (immutable once closed); whatever is missing — the newest bars, or older history — from candleSnapshot."""
    iv = INTERVAL_MS[interval]
    need_from = bar_close_ms - lookback * iv
    rows: dict[int, dict[str, Any]] = {}
    if use_stored:
        try:
            for r in repo.stored_candles(coin, interval, lookback + 2):
                t = open_time_ms(r["open_time"])
                rows[t] = {"t": t, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}
        except Exception:  # noqa: BLE001 - the API path below still works
            log.warning("stored_candles_unavailable", exc_info=True, extra={"fields": {"coin": coin}})
    last_needed = bar_close_ms - iv
    if not rows or min(rows) > need_from:
        start = need_from
    else:
        start = max(rows) + iv
    if start <= last_needed:
        for c in rt.info.candle_snapshot(coin, interval, start, bar_close_ms):
            t = int(c["t"])
            rows.setdefault(t, {"t": t, "o": str(c["o"]), "h": str(c["h"]), "l": str(c["l"]), "c": str(c["c"]),
                                "v": str(c.get("v", "0"))})
    closed = [rows[t] for t in sorted(rows) if t + iv <= bar_close_ms]
    return closed[-lookback:]


def run_creator_signals(*, db: Any, now: datetime, runtime: Runtime | None = None,
                        alerts: Any = None) -> dict[str, Any]:
    """For every listed creator version whose TIMEFRAME bar has closed (and settled ``bar_settle_seconds``) and has
    no signal yet: fetch bars, decrypt the code (dedicated decryptor, AAD bound to strategy + code hash), run it
    ONCE in the sandbox, store one ``signals`` row per market (source sandbox). The executor then fans out.

    Trusted dexes (SPEC §12, REVIEW_TRADING_KEYS F1): the allowlist is read once per run (unreadable → fail closed:
    validator perps only). A market on a builder dex that is not on the ACTIVE allowlist (never added, or removed
    after the version was listed) gets NO signal row — the other markets of the version are still stored — and a
    critical ops alert ``creator_signal_untrusted_dex`` is raised (deduplicated per version / bar)."""
    from app.strategies.dexes import dex_of, is_trusted_coin, load_trusted

    rt = runtime or get_runtime()
    now = _aware(now)
    pdb = _db(db)
    alerts = alerts or rt.alerts(pdb)
    repo = PgCreatorSignalRepo(pdb)
    locks = PgLockProvider(pdb)
    now_ms = _ms(now)
    t0 = time.monotonic()
    rep: dict[str, Any] = {"versions": 0, "not_closed": 0, "already": 0, "waiting_data": 0, "stored": 0,
                           "errors": 0, "deferred": 0, "untrusted": 0}
    versions = repo.creator_versions()
    trusted: frozenset[str] | None
    try:
        trusted = load_trusted(pdb)
    except Exception as e:  # noqa: BLE001 - fail closed: only validator perps get signals
        trusted = None
        log.error("trusted_dexes_unavailable", extra={"fields": {"error": type(e).__name__}})
    rep["versions"] = len(versions)
    use_stored: bool | None = None
    for v in versions:
        interval = str(v["timeframe"])
        iv = INTERVAL_MS.get(interval)
        if iv is None:
            rep["errors"] += 1
            continue
        bar_close_ms = now_ms - now_ms % iv
        bar_close = datetime.fromtimestamp(bar_close_ms / 1000, tz=UTC)
        if now_ms - bar_close_ms < rt.bar_settle_seconds * 1000:
            rep["not_closed"] += 1
            continue
        if repo.has_signal(v["version_id"], bar_close):
            rep["already"] += 1
            continue
        if time.monotonic() - t0 > rt.creator_signal_budget_seconds:
            rep["deferred"] += 1
            continue
        try:
            with locks.try_lock(f"sig:{v['version_id']}") as held:
                if not held or repo.has_signal(v["version_id"], bar_close):
                    rep["already"] += 1
                    continue
                if use_stored is None:
                    use_stored = pdb.table_exists("candles")
                lookback = int(v["lookback"])
                bars = {c: _bars_for(rt, repo, c, interval, lookback, bar_close_ms, use_stored) for c in v["markets"]}
                missing = sorted(c for c, b in bars.items() if not b or b[-1]["t"] != bar_close_ms - iv)
                if missing and now_ms - bar_close_ms < rt.missing_bar_grace_seconds * 1000:
                    rep["waiting_data"] += 1
                    continue
                source = rt.code_decryptor.open_source(strategy_id=v["strategy_id"], code_hash=v["code_hash"],
                                                       ciphertext=v["code_ciphertext"])
                result = rt.sandbox.run(source, bars, now_ms=bar_close_ms)
                del source
                if result.get("code_hash") and result["code_hash"] != v["code_hash"]:
                    raise ExternalServiceError("sandbox ran different code than the version's code_hash")
                weights = result["weights"]
                unknown = sorted(set(weights) - set(v["markets"]))
                if unknown:
                    raise ValidationFailed("sandbox returned weights for markets outside MARKETS", markets=unknown)
                bps = {c: weight_to_bps(weights.get(c, 0), int(v["max_leverage"])) for c in v["markets"]}
                if sum(abs(x) for x in bps.values()) > int(v["max_leverage"]) * BPS:
                    raise ValidationFailed("Σ|weights| above MAX_LEVERAGE")
                untrusted = sorted(c for c in bps if not is_trusted_coin(c, trusted))
                if untrusted:
                    rep["untrusted"] += len(untrusted)
                    _emit(alerts, "critical", "creator_signal_untrusted_dex",
                          {"strategy_version_id": v["version_id"], "strategy_id": v["strategy_id"],
                           "bar_close": bar_close.isoformat(), "markets": untrusted,
                           "dexes": sorted({dex_of(c) for c in untrusted}), "allowlist_loaded": trusted is not None},
                          dedup=f"creator_signal_untrusted_dex:{v['version_id']}:{bar_close.isoformat()}")
                    bps = {c: w for c, w in bps.items() if c not in untrusted}
                    if not bps:
                        continue
                raw = {"weights": {c: str(weights.get(c, 0)) for c in v["markets"]}, "code_hash": v["code_hash"],
                       "cpu_seconds": result.get("cpu_seconds"), "bars_last_t": {c: (b[-1]["t"] if b else None)
                                                                                for c, b in bars.items()},
                       "missing_last_bar": missing, "untrusted_not_stored": untrusted,
                       "computed_at": now.isoformat()}
                rep["stored"] += repo.insert_signals(strategy_id=v["strategy_id"], version_id=v["version_id"],
                                                     bar_close=bar_close, weights_bps=bps, raw=raw)
                log.info("creator_signal_stored", extra={"fields": {"strategy_version_id": v["version_id"],
                                                                    "bar_close": bar_close.isoformat(),
                                                                    "weights_bps": bps}})
        except Exception as e:  # noqa: BLE001 - one version never blocks the others
            rep["errors"] += 1
            log.error("creator_signal_failed", exc_info=True,
                      extra={"fields": {"strategy_version_id": v["version_id"], "error": type(e).__name__}})
            _emit(alerts, "warn", "creator_signal_failed",
                  {"strategy_version_id": v["version_id"], "bar_close": bar_close.isoformat(),
                   "error": type(e).__name__, "detail": str(getattr(e, "message", ""))[:200]},
                  dedup=f"creator_signal_failed:{v['version_id']}:{bar_close.isoformat()}")
    return rep


# ================================================================== signing-trust jobs (REVIEW_WEB_INFRA H1, M4)
# Logic in app/execution/trust_jobs.py; these wrappers bind the process runtime (KMS decryptor, info client, rate
# budget) like the other jobs. Routes: app/api/routers/internal_trust.py (executor only, Scheduler OIDC).
_ATTEST_SIGNER: Any = None
_ATTEST_LOCK = threading.Lock()


def _attest_signer(rt: Runtime) -> Any:
    global _ATTEST_SIGNER
    with _ATTEST_LOCK:
        if _ATTEST_SIGNER is None:
            from app.security.kms import make_attestation_signer

            _ATTEST_SIGNER = make_attestation_signer(rt.settings)
        return _ATTEST_SIGNER


def _agent_decryptor(rt: Runtime) -> Any:
    with rt._lock:
        if rt._agent_decryptor is None:
            from app.security.kms import make_decryptor

            rt._agent_decryptor = make_decryptor(rt.settings)   # the same single agent-key decryptor as the tick
        return rt._agent_decryptor


def _agent_encryptor(rt: Runtime) -> Any:
    """Agent-key SEALING (executor only, migrations/0016): ``make_encryptor`` refuses every other service role."""
    with rt._lock:
        if getattr(rt, "_agent_encryptor", None) is None:
            from app.security.kms import make_encryptor

            rt._agent_encryptor = make_encryptor(rt.settings)
        return rt._agent_encryptor


def _optional_signer(rt: Runtime) -> Any:
    try:
        return _attest_signer(rt)
    except Exception as e:  # noqa: BLE001 - keys are still generated; attest_agents retries the signature
        log.error("attestation_signer_unavailable", extra={"fields": {"error": type(e).__name__}})
        return None


def generate_agents(*, db: Any, now: datetime, runtime: Runtime | None = None, encryptor: Any = None,
                    decryptor: Any = None, signer: Any = None, limit: int = 20,
                    max_seconds: float = 30.0) -> dict[str, Any]:
    """/internal/generate-agents (and the first half of /internal/attest-agents): executor-side agent key generation
    for pending agent requests (see trust_jobs.generate_agents)."""
    from .trust_jobs import generate_agents as _run

    rt = runtime or get_runtime()
    return _run(db, _aware(now), encryptor=encryptor or _agent_encryptor(rt),
                decryptor=decryptor or _agent_decryptor(rt), signer=signer or _optional_signer(rt), limit=limit,
                max_seconds=max_seconds)


def tick_generate_agents(*, db: Any, now: datetime, runtime: Runtime | None = None) -> dict[str, Any] | None:
    """Tick hook: a cheap SELECT first; KMS is only touched when a request is waiting. Budget: 5 keys / 5 s."""
    from app.jobs_data import _db as jdb

    with jdb.transaction(db) as conn:
        waiting = jdb.one(conn, """SELECT 1 AS x FROM agent_keys WHERE status = 'requested' AND key_ciphertext IS NULL
                                     AND attestation_failed_at IS NULL LIMIT 1""")
    if not waiting:
        return None
    return generate_agents(db=db, now=now, runtime=runtime, limit=5, max_seconds=5.0)


def attest_agents(*, db: Any, now: datetime, runtime: Runtime | None = None, signer: Any = None,
                  decryptor: Any = None, encryptor: Any = None, limit: int = 100) -> dict[str, Any]:
    """/internal/attest-agents (every minute): generate keys for pending agent requests (executor keygen, 0016), then
    attest executor-generated keys whose attestation is still missing (see trust_jobs)."""
    from .trust_jobs import attest_agents as _run

    rt = runtime or get_runtime()
    dec = decryptor or _agent_decryptor(rt)
    sig = signer or _attest_signer(rt)
    keygen = generate_agents(db=db, now=now, runtime=rt, encryptor=encryptor, decryptor=dec, signer=sig,
                             limit=min(int(limit), 50))
    rep = _run(db, _aware(now), decryptor=dec, signer=sig, limit=limit)
    rep["keygen"] = keygen
    return rep


def agent_substitution_scan(*, db: Any, now: datetime, runtime: Runtime | None = None, info: Any = None) -> dict[str, Any]:
    """/internal/agent-substitution-scan (every 10 min): a foreign agent named like ours → critical alert."""
    from .trust_jobs import agent_substitution_scan as _run

    rt = runtime or get_runtime()
    pdb = _db(db)
    rt.bind_db(pdb)
    return _run(db, _aware(now), info=info, settings=rt.settings, rate_budget=rt.rate_budget)


def executor_selftest(*, db: Any, now: datetime, runtime: Runtime | None = None) -> dict[str, Any]:
    """/internal/selftest (deploy: new revision, before traffic): side-effect-free dry-run of the tick's inputs."""
    from .trust_jobs import executor_selftest as _run

    rt = runtime or get_runtime()
    pdb = _db(db)
    rt.bind_db(pdb)
    return _run(pdb, _aware(now), runtime=rt, signer_factory=lambda: _attest_signer(rt))


__all__ += ["generate_agents", "tick_generate_agents", "attest_agents", "agent_substitution_scan", "executor_selftest"]

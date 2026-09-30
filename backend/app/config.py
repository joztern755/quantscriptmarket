"""All configuration. Read env ONLY here. Economics defaults = docs/SPEC.md §1 (owner decisions 30 Sep 2026)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from app.money import usd


@dataclass(frozen=True)
class ReferralTier:
    name: str
    min_active_users: int
    min_notional_30d_micro: int
    share_of_pool_bps: int  # share of the referral pool paid to the referrer


@dataclass(frozen=True)
class Plan:
    key: str
    price_monthly_micro: int
    max_active_strategies: int | None  # None = unlimited
    features: tuple[str, ...]


@dataclass(frozen=True)
class Economics:
    builder_fee_tenths_bp: int = 100                # 0.1% of notional (Hyperliquid perp max)
    builder_split_creator_bps: int = 5000           # of the collected builder fee -> 0.05% notional
    builder_split_platform_bps: int = 3000          # -> 0.03% notional
    builder_split_referral_pool_bps: int = 2000     # -> 0.02% notional
    profit_share_creator_cap_bps: int = 1200        # creators may set 0–12% (owner 30 Sep 2026)
    platform_profit_share_bps: int = 150            # 1.5% of profit
    platform_profit_share_mode: str = "on_top"      # owner: user pays creator% + 1.5% (max 13.5%)
    subscription_platform_bps: int = 300            # 3% of creator subscription sales
    post_platform_fee_micro: int = usd(1)           # $1 per paid-post sale
    post_min_price_micro: int = usd(2)
    min_topup_micro: int = usd(10)
    past_due_grace_hours: int = 72
    stripe_fee_absorbed: bool = False               # owner: Stripe fee passed to user (credit net of actual fee)
    referral_tiers: tuple[ReferralTier, ...] = (
        ReferralTier("starter", 0, 0, 5000),
        ReferralTier("partner", 10, usd(1_000_000), 7500),
        ReferralTier("elite", 100, usd(25_000_000), 10000),
    )
    plans: tuple[Plan, ...] = (
        Plan("free", 0, 1, ("marketplace", "leaderboard", "free_posts", "email_telegram_alerts")),
        Plan("pro", usd(20), 3, ("marketplace", "leaderboard", "free_posts", "paid_posts", "email_telegram_alerts")),
        Plan("max", usd(50), None, ("marketplace", "leaderboard", "free_posts", "paid_posts", "email_telegram_alerts", "csv_export", "read_api")),
    )

    def plan(self, key: str) -> Plan:
        for p in self.plans:
            if p.key == key:
                return p
        raise KeyError(key)


@dataclass(frozen=True)
class RiskLimits:
    max_order_pct_of_day_volume_bps: int = 50       # 0.5% of 24h notional volume
    max_order_pct_of_oi_bps: int = 200              # 2% of open interest
    max_slippage_bps: int = 50                      # IOC limit within 0.5% of mid
    max_mark_oracle_dev_bps: int = 200              # reject if mark vs oracle > 2%
    max_data_age_seconds: int = 60
    consecutive_reject_breaker: int = 3
    platform_max_leverage: int = 50                 # owner: no platform leverage cap — ceiling only for input sanity;
                                                    # each market's own max leverage (Hyperliquid meta) always applies
    min_order_notional_micro: int = usd(10)
    min_rebalance_pct_bps: int = 200                # skip deltas < 2% of allocation
    jitter_max_seconds: int = 600                   # per-user random delay 0–10 min (privacy)
    user_drawdown_alert_bps: int = 2000             # 20% of allocation in 24h
    oi_spike_alert_bps: int = 5000                  # +50% OI in 1h
    signal_max_age_hours: int = 36                  # applies to the feed's generated_at
    signal_max_bar_age_days: int = 4                # as_of bar may be older (TradFi weekends: Monday's newest bar is Friday)
    min_subscribers_for_public_stats: int = 5
    min_listing_history_days: int = 180             # owner: ≥180 days to list
    short_history_warning_days: int = 365           # "Short history" warning below this


@dataclass(frozen=True)
class HlLimits:
    """Hyperliquid ``/info`` request weights and OUR shared per-egress-IP budget (SPEC §6; app/hl/budget.py).

    Hyperliquid's documented limit (recalled — UNVERIFIED from this environment, check the "Rate limits and user
    limits" docs page before go-live): 1200 weight / minute / IP across ``/info`` (and ``/exchange``); ``/info``
    requests weigh 20, except a light set (l2Book, allMids, clearinghouseState, orderStatus,
    spotClearinghouseState, exchangeStatus) at 2 and ``userRole`` at 60; list endpoints add 1 per 20 items returned
    (candleSnapshot: 1 per 60). Every weight below is configuration, never trusted as fact.

    We budget ``budget_weight_per_minute`` (default 800, i.e. ~2/3 of the recalled limit, leaving headroom for the
    API service and retries) per egress IP (``egress_key``: every service that shares one NAT IP must share one
    key). The executor tick has priority: ``tick_reserve_per_minute`` of each minute is kept for it — data jobs may
    only use ``budget − max(tick used, reserve)``, and back off to the next minute when that is spent; the tick
    itself is charged but never blocked."""
    budget_weight_per_minute: int = 800
    tick_reserve_per_minute: int = 300
    egress_key: str = "default"
    shared_budget: bool = True
    default_weight: int = 20
    light_weight: int = 2
    light_types: tuple[str, ...] = ("l2Book", "allMids", "clearinghouseState", "orderStatus",
                                    "spotClearinghouseState", "exchangeStatus")
    type_weights: tuple[tuple[str, int], ...] = (("userRole", 60),)
    exchange_weight: int = 1              # one unbatched /exchange action (the API's user-signed relay)
    # extra weight: +1 per N items returned
    items_per_extra_weight: tuple[tuple[str, int], ...] = (
        ("candleSnapshot", 60), ("userFills", 20), ("userFillsByTime", 20), ("userFunding", 20),
        ("userNonFundingLedgerUpdates", 20), ("historicalOrders", 20), ("fundingHistory", 20),
        ("userTwapSliceFills", 20), ("recentTrades", 20))
    max_wait_seconds: float = 90.0        # longest a non-tick caller waits for budget before giving up

    def __post_init__(self) -> None:
        if self.budget_weight_per_minute <= 0 or not 0 <= self.tick_reserve_per_minute < self.budget_weight_per_minute:
            raise ValueError("HlLimits: need 0 <= tick_reserve_per_minute < budget_weight_per_minute")

    @property
    def jobs_ceiling(self) -> int:
        return self.budget_weight_per_minute - self.tick_reserve_per_minute

    def weight(self, request_type: str | None) -> int:
        """Base weight of one ``/info`` request of this type."""
        t = str(request_type or "")
        for name, w in self.type_weights:
            if name == t:
                return int(w)
        return int(self.light_weight if t in self.light_types else self.default_weight)

    def extra_weight(self, request_type: str | None, n_items: int) -> int:
        """Extra weight charged once the response size is known."""
        for name, per in self.items_per_extra_weight:
            if name == str(request_type or "") and per > 0:
                return -(-max(0, int(n_items)) // int(per))
        return 0


@dataclass(frozen=True)
class Settings:
    env: str                                        # "dev" | "test" | "prod"
    hl_api_url: str
    hl_is_mainnet: bool
    builder_address: str                            # lower-case 0x…
    treasury_address: str                           # lower-case 0x… (fee-balance USDC deposits)
    agent_name: str
    database_url: str
    kms_key_name: str                               # projects/…/locations/…/keyRings/…/cryptoKeys/agent-keys
    local_dev_kek_b64: str                          # dev/test ONLY; refused when env == "prod"
    firebase_project_id: str
    web_origin: str
    api_origin: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    telegram_bot_token: str
    telegram_ops_chat_id: str
    email_provider_api_key: str
    signals_url: str
    signals_pubkey_b64: str
    restricted_countries: tuple[str, ...]
    feature_creator_uploads: bool
    in_house_listed: tuple[str, ...]
    service_role: str                               # "api" | "executor" | "sandbox" | "all" (dev only)
    audit_pepper_b64: str                           # ≥32 random bytes, Secret Manager; hashes IPs / user agents
    firebase_auth_domain: str                       # e.g. aijalon.trade (recommended) or <project>.firebaseapp.com
    edge_auth_secret: str                           # shared secret header set by Cloudflare Transform Rule
    launch_phase: str                               # "internal" (allowlisted emails, small caps) | "public"
    allowlist_emails: tuple[str, ...]               # internal phase: only these emails may create accounts
    max_allocation_per_user_micro: int | None       # None = no cap (owner 30 Sep 2026: no per-user cap)
    max_total_platform_allocation_micro: int | None # None = no cap (owner: no platform total cap)
    max_user_leverage: int | None                   # None = no launch cap; each market's own max leverage applies
    payouts_enabled: bool
    stripe_max_topup_micro: int
    feature_stripe_myr: bool                        # FPX / GrabPay need MYR; off until an FX source is chosen
    stripe_myr_fx_spread_bps: int
    stripe_api_version: str
    stripe_fee_estimate_bps: int                    # estimate shown BEFORE payment; set from Stripe MY pricing [CONFIRM]
    stripe_fee_estimate_fixed_micro: int
    ops_emails: tuple[str, ...]
    email_from: str
    telegram_webhook_secret: str                    # X-Telegram-Bot-Api-Secret-Token for /v1/webhooks/telegram
    telegram_bot_username: str                      # for t.me/<bot>?start=<token> link
    sandbox_url: str                                # Cloud Run sandbox service URL
    sandbox_shared_secret: str                      # X-Sandbox-Secret (plus Cloud Run IAM)
    scheduler_sa_email: str                         # Cloud Scheduler OIDC service account (internal routes)
    internal_audience: str                          # OIDC audience for internal routes (executor service URL)
    stripe_publishable_key: str
    legal_versions: dict                            # {doc: version} — filled from legal/*.md "Version:" lines at build
    legal_doc_hashes: dict                          # {doc: sha256 of the exact served text}
    kyc_provider: str                               # "manual" (internal phase) | "sumsub"
    kyc_app_token: str
    kyc_secret_key: str
    kyc_webhook_secret: str
    kyc_level_name: str
    kyc_api_base: str
    economics: Economics = field(default_factory=Economics)
    risk: RiskLimits = field(default_factory=RiskLimits)
    hl_limits: HlLimits = field(default_factory=HlLimits)
    # REVIEW_TRADING_KEYS F2 / SECURITY §3.4: creator strategy code is sealed under its OWN KMS key (api encrypt-only,
    # executor decrypt-only), never under agent-keys. Empty outside prod → local dev KEK with a separate KMS-level AAD.
    creator_code_kms_key_name: str = ""             # projects/…/locations/…/keyRings/…/cryptoKeys/creator-code
    # REVIEW_MONEY H2: HMAC key of our order cloids (app.hl.client.make_cloid) — a user must not be able to compute
    # the cloids of our orders. Secret Manager CLOID_SECRET; required in prod for the executor. Never rotate while
    # orders may be unresolved (a retry recomputes the cloid of the same bar/attempt).
    cloid_secret: str = ""

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


def _b(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _opt_usd(name: str) -> int | None:
    """Optional USD cap from env: unset, empty or 0 → None (no cap)."""
    raw = os.environ.get(name, "").strip()
    return usd(raw) if raw and raw != "0" else None


def _opt_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw and raw != "0" else None


# DRAFT list for counsel review (legal/jurisdiction.md). ISO 3166-1 alpha-2.
DEFAULT_RESTRICTED = ("US", "CU", "IR", "KP", "SY", "RU", "BY", "MM")


@lru_cache(maxsize=1)
def _legal_meta() -> tuple[dict, dict]:
    """Versions and sha256 of legal/*.md (the exact files the web serves under /legal/). LEGAL_DIR overrides."""
    import hashlib
    import re
    from pathlib import Path

    root = Path(os.environ.get("LEGAL_DIR", Path(__file__).resolve().parents[2] / "legal"))
    versions: dict = {}
    hashes: dict = {}
    if root.is_dir():
        for f in sorted(root.glob("*.md")):
            if f.stem.upper() == "README":
                continue
            raw = f.read_bytes()
            m = re.search(rb"^\**Version:?\**:?\s*([0-9A-Za-z._-]+)", raw, re.M)
            versions[f.stem] = m.group(1).decode() if m else "unversioned"
            hashes[f.stem] = hashlib.sha256(raw).hexdigest()
    return versions, hashes


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env = os.environ.get("APP_ENV", "dev")
    s = Settings(
        env=env,
        hl_api_url=os.environ.get("HL_API_URL", "https://api.hyperliquid.xyz"),
        hl_is_mainnet=_b("HL_IS_MAINNET", "true"),
        builder_address=os.environ.get("BUILDER_ADDRESS", "").lower(),
        treasury_address=os.environ.get("TREASURY_ADDRESS", "").lower(),
        agent_name=os.environ.get("AGENT_NAME", "aijalon"),
        database_url=os.environ.get("DATABASE_URL", "postgresql://localhost/aijalon"),
        kms_key_name=os.environ.get("KMS_KEY_NAME", ""),
        local_dev_kek_b64=os.environ.get("LOCAL_DEV_KEK_B64", ""),
        firebase_project_id=os.environ.get("FIREBASE_PROJECT_ID", ""),
        web_origin=os.environ.get("WEB_ORIGIN", "https://aijalon.trade"),
        api_origin=os.environ.get("API_ORIGIN", "https://api.aijalon.trade"),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_ops_chat_id=os.environ.get("TELEGRAM_OPS_CHAT_ID", ""),
        email_provider_api_key=os.environ.get("EMAIL_PROVIDER_API_KEY", ""),
        signals_url=os.environ.get("SIGNALS_URL", "https://aijalon-terminal.web.app/signals.json"),
        signals_pubkey_b64=os.environ.get("SIGNALS_PUBKEY_B64", ""),
        restricted_countries=tuple(
            c.strip().upper() for c in os.environ.get("RESTRICTED_COUNTRIES", ",".join(DEFAULT_RESTRICTED)).split(",") if c.strip()
        ),
        feature_creator_uploads=_b("FEATURE_CREATOR_UPLOADS", "true"),
        in_house_listed=tuple(x for x in os.environ.get("IN_HOUSE_LISTED", "silver").split(",") if x),
        service_role=os.environ.get("SERVICE_ROLE", "all" if env != "prod" else ""),
        audit_pepper_b64=os.environ.get("AUDIT_PEPPER_B64", ""),
        firebase_auth_domain=os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
        edge_auth_secret=os.environ.get("EDGE_AUTH_SECRET", ""),
        launch_phase=os.environ.get("LAUNCH_PHASE", "internal"),
        allowlist_emails=tuple(e.strip().lower() for e in os.environ.get("ALLOWLIST_EMAILS", "").split(",") if e.strip()),
        max_allocation_per_user_micro=_opt_usd("MAX_ALLOCATION_PER_USER_USD"),
        max_total_platform_allocation_micro=_opt_usd("MAX_TOTAL_PLATFORM_ALLOCATION_USD"),
        max_user_leverage=_opt_int("MAX_USER_LEVERAGE"),
        payouts_enabled=_b("PAYOUTS_ENABLED", "false"),
        stripe_max_topup_micro=usd(os.environ.get("STRIPE_MAX_TOPUP_USD", "10000")),
        feature_stripe_myr=_b("FEATURE_STRIPE_MYR", "false"),
        stripe_myr_fx_spread_bps=int(os.environ.get("STRIPE_MYR_FX_SPREAD_BPS", "150")),
        stripe_api_version=os.environ.get("STRIPE_API_VERSION", ""),
        stripe_fee_estimate_bps=int(os.environ.get("STRIPE_FEE_ESTIMATE_BPS", "0")),
        stripe_fee_estimate_fixed_micro=usd(os.environ.get("STRIPE_FEE_ESTIMATE_FIXED_USD", "0")),
        ops_emails=tuple(e.strip() for e in os.environ.get("OPS_EMAILS", "").split(",") if e.strip()),
        email_from=os.environ.get("EMAIL_FROM", "alerts@aijalon.trade"),
        telegram_webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
        telegram_bot_username=os.environ.get("TELEGRAM_BOT_USERNAME", ""),
        sandbox_url=os.environ.get("SANDBOX_URL", ""),
        sandbox_shared_secret=os.environ.get("SANDBOX_SHARED_SECRET", ""),
        scheduler_sa_email=os.environ.get("SCHEDULER_SA_EMAIL", ""),
        internal_audience=os.environ.get("INTERNAL_AUDIENCE", ""),
        stripe_publishable_key=os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        legal_versions=_legal_meta()[0],
        legal_doc_hashes=_legal_meta()[1],
        kyc_provider=os.environ.get("KYC_PROVIDER", "manual"),
        kyc_app_token=os.environ.get("KYC_APP_TOKEN", ""),
        kyc_secret_key=os.environ.get("KYC_SECRET_KEY", ""),
        kyc_webhook_secret=os.environ.get("KYC_WEBHOOK_SECRET", ""),
        kyc_level_name=os.environ.get("KYC_LEVEL_NAME", ""),
        kyc_api_base=os.environ.get("KYC_API_BASE", "https://api.sumsub.com"),
        hl_limits=HlLimits(
            budget_weight_per_minute=int(os.environ.get("HL_BUDGET_WEIGHT_PER_MINUTE", "800")),
            tick_reserve_per_minute=int(os.environ.get("HL_TICK_RESERVE_PER_MINUTE", "300")),
            egress_key=os.environ.get("HL_EGRESS_KEY", "default").strip().lower() or "default",
            shared_budget=_b("HL_SHARED_BUDGET", "true"),
        ),
        creator_code_kms_key_name=os.environ.get("CREATOR_CODE_KMS_KEY_NAME", ""),
        cloid_secret=os.environ.get("CLOID_SECRET", ""),
    )
    if s.is_prod:
        required = ["builder_address", "treasury_address", "kms_key_name", "firebase_project_id", "signals_pubkey_b64",
                    "audit_pepper_b64", "service_role"]
        if s.service_role == "api":
            required += ["edge_auth_secret", "telegram_webhook_secret", "sandbox_url", "sandbox_shared_secret",
                         "stripe_publishable_key"]
        if s.service_role == "executor":
            required += ["scheduler_sa_email", "internal_audience"]
        if s.service_role == "executor":
            required += ["sandbox_url", "sandbox_shared_secret"]
        if s.service_role in ("api", "executor"):
            required += ["creator_code_kms_key_name"]
        if s.service_role == "executor":
            required += ["cloid_secret"]
        missing = [k for k in required if not getattr(s, k)]
        if missing:
            raise RuntimeError(f"prod config missing: {missing}")
        if s.local_dev_kek_b64:
            raise RuntimeError("LOCAL_DEV_KEK_B64 must not be set in prod")
        if s.creator_code_kms_key_name and s.creator_code_kms_key_name == s.kms_key_name:
            raise RuntimeError("CREATOR_CODE_KMS_KEY_NAME must be a different KMS key than KMS_KEY_NAME (agent-keys)")
        if s.launch_phase not in ("internal", "public"):
            raise RuntimeError("LAUNCH_PHASE must be internal|public")
        if s.launch_phase == "internal" and not s.allowlist_emails:
            raise RuntimeError("internal launch phase requires ALLOWLIST_EMAILS")
        if s.service_role not in ("api", "executor", "sandbox"):
            raise RuntimeError("SERVICE_ROLE must be api|executor|sandbox in prod")
    return s

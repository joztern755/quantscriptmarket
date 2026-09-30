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
        Plan("free", 0, 1, ("marketplace", "leaderboard", "free_posts")),
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
    platform_max_leverage: int = 5
    min_order_notional_micro: int = usd(10)
    min_rebalance_pct_bps: int = 200                # skip deltas < 2% of allocation
    jitter_max_seconds: int = 600                   # per-user random delay 0–10 min (privacy)
    user_drawdown_alert_bps: int = 2000             # 20% of allocation in 24h
    oi_spike_alert_bps: int = 5000                  # +50% OI in 1h
    signal_max_age_hours: int = 36
    min_subscribers_for_public_stats: int = 5


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
    max_allocation_per_user_micro: int              # cap per user across all subscriptions
    max_total_platform_allocation_micro: int        # cap across all users
    max_user_leverage: int                          # launch-phase leverage cap (≤ risk.platform_max_leverage)
    payouts_enabled: bool
    stripe_max_topup_micro: int
    feature_stripe_myr: bool                        # FPX / GrabPay need MYR; off until an FX source is chosen
    stripe_myr_fx_spread_bps: int
    stripe_api_version: str
    stripe_fee_estimate_bps: int                    # estimate shown BEFORE payment; set from Stripe MY pricing [CONFIRM]
    stripe_fee_estimate_fixed_micro: int
    ops_emails: tuple[str, ...]
    email_from: str
    economics: Economics = field(default_factory=Economics)
    risk: RiskLimits = field(default_factory=RiskLimits)

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


def _b(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# DRAFT list for counsel review (legal/jurisdiction.md). ISO 3166-1 alpha-2.
DEFAULT_RESTRICTED = ("US", "CU", "IR", "KP", "SY", "RU", "BY", "MM")


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
        max_allocation_per_user_micro=usd(os.environ.get("MAX_ALLOCATION_PER_USER_USD", "1000")),
        max_total_platform_allocation_micro=usd(os.environ.get("MAX_TOTAL_PLATFORM_ALLOCATION_USD", "25000")),
        max_user_leverage=int(os.environ.get("MAX_USER_LEVERAGE", "2")),
        payouts_enabled=_b("PAYOUTS_ENABLED", "false"),
        stripe_max_topup_micro=usd(os.environ.get("STRIPE_MAX_TOPUP_USD", "10000")),
        feature_stripe_myr=_b("FEATURE_STRIPE_MYR", "false"),
        stripe_myr_fx_spread_bps=int(os.environ.get("STRIPE_MYR_FX_SPREAD_BPS", "150")),
        stripe_api_version=os.environ.get("STRIPE_API_VERSION", ""),
        stripe_fee_estimate_bps=int(os.environ.get("STRIPE_FEE_ESTIMATE_BPS", "0")),
        stripe_fee_estimate_fixed_micro=usd(os.environ.get("STRIPE_FEE_ESTIMATE_FIXED_USD", "0")),
        ops_emails=tuple(e.strip() for e in os.environ.get("OPS_EMAILS", "").split(",") if e.strip()),
        email_from=os.environ.get("EMAIL_FROM", "alerts@aijalon.trade"),
    )
    if s.is_prod:
        missing = [k for k in ("builder_address", "treasury_address", "kms_key_name", "firebase_project_id", "signals_pubkey_b64",
                                "audit_pepper_b64", "edge_auth_secret", "service_role") if not getattr(s, k)]
        if missing:
            raise RuntimeError(f"prod config missing: {missing}")
        if s.local_dev_kek_b64:
            raise RuntimeError("LOCAL_DEV_KEK_B64 must not be set in prod")
        if s.launch_phase not in ("internal", "public"):
            raise RuntimeError("LAUNCH_PHASE must be internal|public")
        if s.launch_phase == "internal" and not s.allowlist_emails:
            raise RuntimeError("internal launch phase requires ALLOWLIST_EMAILS")
        if s.service_role not in ("api", "executor", "sandbox"):
            raise RuntimeError("SERVICE_ROLE must be api|executor|sandbox in prod")
    return s

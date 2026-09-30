// Fee math for UI previews. Integer micro-USD and bps only; mirrors SPEC §1 and
// backend/app/domain/fees.py rounding: user charges floor; splits give the remainder to the platform.
// Values always come from GET /v1/public/config (normalised here); defaults = SPEC §1 only as a fallback
// so a partially-populated config never renders NaN.

export interface ReferralTierCfg {
  name: string;
  min_active_users: number;
  min_notional_30d_micro: number;
  share_of_pool_bps: number;
}

export interface PlanCfg {
  key: string;
  price_monthly_micro: number;
  max_active_strategies: number | null;
  features: string[];
}

export interface EconomicsCfg {
  builder_fee_tenths_bp: number;
  builder_split_creator_bps: number;
  builder_split_platform_bps: number;
  builder_split_referral_pool_bps: number;
  profit_share_creator_cap_bps: number;
  platform_profit_share_bps: number;
  platform_profit_share_mode: "on_top" | "carved_out";
  subscription_platform_bps: number;
  post_platform_fee_micro: number;
  post_min_price_micro: number;
  min_topup_micro: number;
  past_due_grace_hours: number;
  referral_tiers: ReferralTierCfg[];
}

export interface NormalizedConfig {
  economics: EconomicsCfg;
  plans: PlanCfg[];
  restricted_countries: string[];
  builder_address: string;
  treasury_address: string;
  agent_name: string;
  feature_creator_uploads: boolean;
  platform_max_leverage: number;
  min_subscribers_for_public_stats: number;
  raw: unknown;
}

const USD = 1_000_000;

export const DEFAULT_ECONOMICS: EconomicsCfg = {
  builder_fee_tenths_bp: 100,
  builder_split_creator_bps: 5000,
  builder_split_platform_bps: 3000,
  builder_split_referral_pool_bps: 2000,
  profit_share_creator_cap_bps: 1500,
  platform_profit_share_bps: 150,
  platform_profit_share_mode: "on_top",
  subscription_platform_bps: 300,
  post_platform_fee_micro: 1 * USD,
  post_min_price_micro: 2 * USD,
  min_topup_micro: 10 * USD,
  past_due_grace_hours: 72,
  referral_tiers: [
    { name: "starter", min_active_users: 0, min_notional_30d_micro: 0, share_of_pool_bps: 5000 },
    { name: "partner", min_active_users: 10, min_notional_30d_micro: 1_000_000 * USD, share_of_pool_bps: 7500 },
    { name: "elite", min_active_users: 100, min_notional_30d_micro: 25_000_000 * USD, share_of_pool_bps: 10000 },
  ],
};

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown, d: number): number => (typeof v === "number" && Number.isFinite(v) ? v : typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v)) ? Number(v) : d);
const str = (v: unknown, d: string): string => (typeof v === "string" ? v : d);

export function normalizeConfig(raw: unknown): NormalizedConfig {
  const r: Rec = isRec(raw) ? raw : {};
  const e: Rec = isRec(r.economics) ? r.economics : isRec(r.fees) ? r.fees : r;
  const D = DEFAULT_ECONOMICS;
  const tiersRaw = Array.isArray(e.referral_tiers) ? e.referral_tiers : null;
  const economics: EconomicsCfg = {
    builder_fee_tenths_bp: num(e.builder_fee_tenths_bp, D.builder_fee_tenths_bp),
    builder_split_creator_bps: num(e.builder_split_creator_bps, D.builder_split_creator_bps),
    builder_split_platform_bps: num(e.builder_split_platform_bps, D.builder_split_platform_bps),
    builder_split_referral_pool_bps: num(e.builder_split_referral_pool_bps, D.builder_split_referral_pool_bps),
    profit_share_creator_cap_bps: num(e.profit_share_creator_cap_bps, D.profit_share_creator_cap_bps),
    platform_profit_share_bps: num(e.platform_profit_share_bps, D.platform_profit_share_bps),
    platform_profit_share_mode: e.platform_profit_share_mode === "carved_out" ? "carved_out" : "on_top",
    subscription_platform_bps: num(e.subscription_platform_bps, D.subscription_platform_bps),
    post_platform_fee_micro: num(e.post_platform_fee_micro, D.post_platform_fee_micro),
    post_min_price_micro: num(e.post_min_price_micro, D.post_min_price_micro),
    min_topup_micro: num(e.min_topup_micro, D.min_topup_micro),
    past_due_grace_hours: num(e.past_due_grace_hours, D.past_due_grace_hours),
    referral_tiers: tiersRaw
      ? tiersRaw.filter(isRec).map((t) => ({
          name: str(t.name, "tier"),
          min_active_users: num(t.min_active_users, 0),
          min_notional_30d_micro: num(t.min_notional_30d_micro, 0),
          share_of_pool_bps: num(t.share_of_pool_bps, 0),
        }))
      : D.referral_tiers,
  };
  const plansRaw = Array.isArray(r.plans) ? r.plans : Array.isArray(e.plans) ? (e.plans as unknown[]) : [];
  const plans: PlanCfg[] = plansRaw.filter(isRec).map((p) => ({
    key: str(p.key, "plan"),
    price_monthly_micro: num(p.price_monthly_micro, 0),
    max_active_strategies: p.max_active_strategies === null || p.max_active_strategies === undefined ? null : num(p.max_active_strategies, 0),
    features: Array.isArray(p.features) ? p.features.filter((x): x is string => typeof x === "string") : [],
  }));
  const risk: Rec = isRec(r.risk) ? r.risk : {};
  return {
    economics,
    plans,
    restricted_countries: Array.isArray(r.restricted_countries) ? r.restricted_countries.filter((x): x is string => typeof x === "string") : [],
    builder_address: str(r.builder_address, ""),
    treasury_address: str(r.treasury_address, ""),
    agent_name: str(r.agent_name, "aijalon"),
    feature_creator_uploads: r.feature_creator_uploads === undefined ? true : r.feature_creator_uploads === true,
    platform_max_leverage: num(risk.platform_max_leverage ?? r.platform_max_leverage, 5),
    min_subscribers_for_public_stats: num(risk.min_subscribers_for_public_stats ?? r.min_subscribers_for_public_stats, 5),
    raw,
  };
}

/** floor(amount * bps / 10000) for non-negative integers. */
export function bpsOf(amountMicro: number, bps: number): number {
  if (amountMicro <= 0 || bps <= 0) return 0;
  return Math.floor((amountMicro * bps) / 10000);
}

/** Builder fee rate in percent of notional (e.g. 100 tenths-bp -> 0.1). */
export function builderFeePct(e: EconomicsCfg): number {
  return e.builder_fee_tenths_bp / 1000;
}

/** Builder fee as the percent string Hyperliquid's approveBuilderFee expects, e.g. "0.1%". */
export function builderFeePctString(e: EconomicsCfg): string {
  return `${builderFeePct(e)}%`;
}

export interface ProfitShareBreakdown {
  userPaysBps: number; // total rate charged to the subscriber
  creatorBps: number; // what the creator receives
  platformBps: number;
  userPaysMicro: number;
  creatorMicro: number;
  platformMicro: number;
}

/** Profit share on `profitMicro` of new profit above the high-water mark. */
export function profitShare(e: EconomicsCfg, creatorBpsSet: number, profitMicro: number): ProfitShareBreakdown {
  const c = Math.max(0, Math.min(creatorBpsSet, e.profit_share_creator_cap_bps));
  let userPaysBps: number;
  let creatorBps: number;
  if (e.platform_profit_share_mode === "carved_out") {
    userPaysBps = c;
    creatorBps = Math.max(0, c - e.platform_profit_share_bps);
  } else {
    userPaysBps = c + e.platform_profit_share_bps;
    creatorBps = c;
  }
  const userPaysMicro = bpsOf(profitMicro, userPaysBps);
  const creatorMicro = Math.min(userPaysMicro, bpsOf(profitMicro, creatorBps));
  return {
    userPaysBps,
    creatorBps,
    platformBps: userPaysBps - creatorBps,
    userPaysMicro,
    creatorMicro,
    platformMicro: userPaysMicro - creatorMicro,
  };
}

export interface SplitResult {
  totalMicro: number;
  creatorMicro: number;
  platformMicro: number;
}

export function subscriptionSplit(e: EconomicsCfg, priceMicro: number): SplitResult {
  const creatorMicro = bpsOf(priceMicro, 10000 - e.subscription_platform_bps);
  return { totalMicro: priceMicro, creatorMicro, platformMicro: priceMicro - creatorMicro };
}

export function postSplit(e: EconomicsCfg, priceMicro: number): SplitResult {
  const creatorMicro = Math.max(0, priceMicro - e.post_platform_fee_micro);
  return { totalMicro: priceMicro, creatorMicro, platformMicro: priceMicro - creatorMicro };
}

export interface BuilderSplit {
  feeMicro: number;
  creatorMicro: number;
  referralPoolMicro: number;
  platformMicro: number;
}

/** Builder fee on `notionalMicro` traded, and its split (creator share goes to platform for in-house). */
export function builderSplit(e: EconomicsCfg, notionalMicro: number): BuilderSplit {
  // fee = notional * tenths_bp / 100000
  const feeMicro = notionalMicro > 0 ? Math.floor((notionalMicro * e.builder_fee_tenths_bp) / 100000) : 0;
  const creatorMicro = bpsOf(feeMicro, e.builder_split_creator_bps);
  const referralPoolMicro = bpsOf(feeMicro, e.builder_split_referral_pool_bps);
  return { feeMicro, creatorMicro, referralPoolMicro, platformMicro: feeMicro - creatorMicro - referralPoolMicro };
}

export function bpsToPct(bps: number): string {
  const v = bps / 100;
  return (Number.isInteger(v) ? v.toFixed(0) : v.toFixed(2).replace(/0$/, "")) + "%";
}

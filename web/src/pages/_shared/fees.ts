// Fee math for UI previews. Integer micro-USD and bps only; mirrors SPEC §1 and
// backend/app/domain/fees.py rounding: user charges floor; splits give the remainder to the platform.
// Rates always come from publicConfig().economics (core/api.ts).

import type { PublicConfig } from "../../core/api.js";

export type EconomicsCfg = PublicConfig["economics"];

export interface ReferralTierCfg {
  name: string;
  min_active_users: number;
  min_notional_30d_micro: number;
  share_of_pool_bps: number;
}

const USD = 1_000_000;

/** SPEC §1.2 defaults; GET /v1/referrals may return `tiers` which then take precedence. */
export const DEFAULT_REFERRAL_TIERS: ReferralTierCfg[] = [
  { name: "starter", min_active_users: 0, min_notional_30d_micro: 0, share_of_pool_bps: 5000 },
  { name: "partner", min_active_users: 10, min_notional_30d_micro: 1_000_000 * USD, share_of_pool_bps: 7500 },
  { name: "elite", min_active_users: 100, min_notional_30d_micro: 25_000_000 * USD, share_of_pool_bps: 10000 },
];

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

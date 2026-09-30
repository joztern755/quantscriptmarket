// Stripe.js loader (must come from js.stripe.com; Stripe does not support SRI/self-hosting) and the
// processor-fee estimate. Owner decision 30 Sep 2026: Stripe fees are PASSED TO THE USER — the fee
// balance is credited with (amount paid − actual Stripe fee). The client can only ESTIMATE the fee.

import { ApiError, publicConfig, type PublicConfig } from "./api.js";
import { fmtUsd } from "./format.js";

export interface StripeLike {
  elements(opts: Record<string, unknown>): any;
  confirmPayment(opts: Record<string, unknown>): Promise<{ error?: { message?: string; type?: string }; paymentIntent?: { id: string; status: string } }>;
  retrievePaymentIntent(clientSecret: string): Promise<{ paymentIntent?: { id: string; status: string } }>;
}

let loading: Promise<StripeLike> | null = null;

export function loadStripe(): Promise<StripeLike> {
  if (loading) return loading;
  loading = (async () => {
    const cfg = await publicConfig();
    if (!cfg.stripe_publishable_key) throw new ApiError(0, "stripe_unavailable", "Card payments are not available right now.");
    const w = window as unknown as { Stripe?: (key: string, opts?: Record<string, unknown>) => StripeLike };
    if (!w.Stripe) {
      await new Promise<void>((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "https://js.stripe.com/v3/";
        s.async = true;
        s.onload = () => resolve();
        s.onerror = () => reject(new ApiError(0, "stripe_unavailable", "Couldn't load Stripe. Check your connection or content blockers."));
        document.head.appendChild(s);
      });
    }
    if (!w.Stripe) throw new ApiError(0, "stripe_unavailable", "Couldn't load Stripe.");
    return w.Stripe(cfg.stripe_publishable_key);
  })().catch((e) => {
    loading = null;
    throw e;
  });
  return loading;
}

/**
 * Estimated credit for a Stripe top-up of `amountMicro`: amount − estimated processor fee.
 * `estimated` is false when the config has no fee estimate (then feeMicro is null).
 * The ACTUAL credit is decided by the server from Stripe's balance transaction.
 */
export function estimateStripeCredit(cfg: PublicConfig, amountMicro: number): { feeMicro: number | null; creditMicro: number | null; estimated: boolean } {
  const bps = cfg.stripe_fee_estimate_bps;
  if (bps === null || cfg.economics.stripe_fee_absorbed) return { feeMicro: null, creditMicro: null, estimated: false };
  const fee = Math.ceil((amountMicro * bps) / 10_000) + (cfg.stripe_fee_estimate_fixed_micro ?? 0); // round fee UP → credit estimate never overstated
  return { feeMicro: fee, creditMicro: Math.max(0, amountMicro - fee), estimated: true };
}

/** Standard wording for deposit UIs. */
export function stripeFeeNotice(cfg: PublicConfig, amountMicro?: number): string {
  const base = "Card and other processor fees are deducted: your fee balance is credited with the amount paid minus the actual processor fee.";
  if (!amountMicro || amountMicro <= 0) return base;
  const e = estimateStripeCredit(cfg, amountMicro);
  if (!e.estimated) return `${base} The fee depends on the payment method and is shown on your receipt.`;
  return `${base} Estimate for ${fmtUsd(amountMicro)}: fee about ${fmtUsd(e.feeMicro!)}, credit about ${fmtUsd(e.creditMicro!)} (estimate only — the final amount depends on your payment method).`;
}

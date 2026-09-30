// #/ — landing page: what it is, how funds stay in your wallet, fees (from /v1/public/config),
// featured SILVER, creators CTA, honest risk line.
import type { PageContext } from "../core/router.js";
import { h, mount, table, note, skeleton, errorState, emptyState, type Column } from "../core/ui.js";
import { api, publicConfig, type PublicConfig } from "../core/api.js";
import { fmtUsd, fmtBps, fmtTenthsBp } from "../core/format.js";
import type { StrategySummary } from "./_shared/types.js";
import { strategyCard } from "./_shared/strategy-card.js";
import { ensurePageCss, listOf, isAbortError, RISK_LINE } from "./_shared/util.js";

export const title = "Strategies on Hyperliquid";

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const featuredBox = h("div", { class: "cards" }, skeleton(4));
  const feesBox = h("div", { class: "stack" }, skeleton(6));
  const plansBox = h("div", { class: "stack" });

  mount(
    root,
    h(
      "div",
      { class: "stack" },
      h(
        "section",
        { class: "hero stack" },
        h("div", { class: "eyebrow" }, "aijalon.trade · Hyperliquid strategy marketplace"),
        h("h1", null, "Rule-based strategies that trade your own Hyperliquid account."),
        h(
          "p",
          { class: "lead" },
          "Subscribe to a strategy, approve a trade-only agent and choose how much to allocate. Your funds stay in your Hyperliquid account the whole time — we can place trades for you, but we can never withdraw or transfer your money.",
        ),
        h(
          "div",
          { class: "btns" },
          h("a", { class: "btn primary", href: "#/market" }, "Browse strategies"),
          h("a", { class: "btn", href: "#/leaderboard" }, "Leaderboard"),
          h("a", { class: "btn ghost", href: "#/legal/risk-disclosure" }, "Read the risk disclosure"),
        ),
        note(RISK_LINE, "warn"),
      ),

      h(
        "section",
        { class: "stack", "aria-labelledby": "how-h" },
        h("div", { class: "sec-head" }, h("div", null, h("div", { class: "eyebrow" }, "Non-custodial"), h("h2", { id: "how-h" }, "How your funds stay in your wallet"))),
        h(
          "ol",
          { class: "steps-list" },
          step("Connect your wallet", "Sign a message to prove you own your Hyperliquid account. No funds move."),
          step("Approve a trade-only agent", "You approve a dedicated agent wallet on Hyperliquid. Hyperliquid agents can place and cancel orders; they cannot withdraw or transfer funds."),
          step("Approve the builder fee", "A 0.1% fee on each strategy order's notional, collected by Hyperliquid itself. You can revoke it on Hyperliquid at any time."),
          step("Set allocation and max leverage", "The strategy only sizes positions within the USD allocation and leverage cap you choose. One strategy per trading account (master or sub-account)."),
          step("Prepaid fee balance", "Subscriptions, profit share and paid posts are deducted from a separate prepaid balance — never from your trading account."),
        ),
      ),

      h(
        "section",
        { class: "stack", "aria-labelledby": "feat-h" },
        h(
          "div",
          { class: "sec-head" },
          h("div", null, h("div", { class: "eyebrow" }, "Featured"), h("h2", { id: "feat-h" }, "In-house: SILVER")),
          h("a", { href: "#/market" }, "All strategies →"),
        ),
        featuredBox,
      ),

      h(
        "section",
        { class: "stack", "aria-labelledby": "fees-h" },
        h("div", { class: "sec-head" }, h("div", null, h("div", { class: "eyebrow" }, "Pricing"), h("h2", { id: "fees-h" }, "Fees, in full"))),
        feesBox,
        plansBox,
      ),

      h(
        "section",
        { class: "panel accent stack", "aria-labelledby": "creators-h" },
        h("div", { class: "eyebrow" }, "For creators"),
        h("h2", { id: "creators-h" }, "Publish a strategy. Your code stays private."),
        h(
          "p",
          { class: "muted" },
          "Upload a Python script or build one with the no-code builder. Scripts run only on our servers in a sandbox — subscribers never see your code. Every upload is validated, walk-forward backtested and reviewed before listing. Identity verification (KYC) is required before listing or payouts.",
        ),
        h("div", { class: "btns" }, h("a", { class: "btn primary", href: "#/creator" }, "Open Creator Studio"), h("a", { class: "btn", href: "#/legal/creator-agreement" }, "Creator agreement")),
      ),

      h("p", { class: "small muted" }, RISK_LINE, " ", h("a", { href: "#/legal/risk-disclosure" }, "Risk disclosure"), " · ", h("a", { href: "#/legal/terms" }, "Terms")),
    ),
  );

  // Load both in parallel; each box handles its own error state.
  const loadFeatured = async (): Promise<void> => {
    mount(featuredBox, skeleton(4));
    try {
      const res = await api.get<unknown>("/public/strategies", { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      const all = listOf<StrategySummary>(res, "strategies");
      const featured =
        all.find((s) => s.slug === "silver") ??
        all.find((s) => s.featured) ??
        all.find((s) => s.markets?.includes("xyz:SILVER")) ??
        all.find((s) => s.in_house);
      if (!featured) {
        mount(featuredBox, emptyState("No strategies listed yet", "Check back soon."));
        return;
      }
      const others = all.filter((s) => s !== featured).slice(0, 2);
      mount(featuredBox, strategyCard(featured, { featured: true }), ...others.map((s) => strategyCard(s)));
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(featuredBox, errorState(err, () => void loadFeatured()));
    }
  };
  const loadFees = async (): Promise<void> => {
    mount(feesBox, skeleton(6));
    try {
      const cfg = await publicConfig();
      if (!ctx.isCurrent()) return;
      mount(feesBox, ...feesTable(cfg));
      mount(plansBox, ...plansTable(cfg));
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(feesBox, errorState(err, () => void loadFees()));
    }
  };
  await Promise.all([loadFeatured(), loadFees()]);
}

function step(title: string, detail: string): HTMLElement {
  return h("li", null, h("b", null, title), h("span", null, detail));
}

interface FeeRow {
  item: string;
  rate: string;
  who: string;
}

function feesTable(cfg: PublicConfig): Node[] {
  const e = cfg.economics;
  const psTotalMax = e.platform_profit_share_mode === "on_top" ? e.profit_share_creator_cap_bps + e.platform_profit_share_bps : e.profit_share_creator_cap_bps;
  const rows: FeeRow[] = [
    {
      item: "Builder fee",
      rate: `${fmtTenthsBp(e.builder_fee_tenths_bp)} of each order's notional`,
      who: `Collected on-chain by Hyperliquid on every strategy order. Split: creator ${fmtBps(e.builder_split_creator_bps)}, platform ${fmtBps(e.builder_split_platform_bps)}, referral pool ${fmtBps(e.builder_split_referral_pool_bps)} of the fee.`,
    },
    {
      item: "Strategy subscription",
      rate: "Monthly price set by the creator",
      who: `Prepaid from your fee balance at start and each renewal. Platform keeps ${fmtBps(e.subscription_platform_bps)}, creator receives the rest.`,
    },
    {
      item: "Profit share",
      rate:
        e.platform_profit_share_mode === "on_top"
          ? `Creator 0–${fmtBps(e.profit_share_creator_cap_bps)} + platform ${fmtBps(e.platform_profit_share_bps)} (max ${fmtBps(psTotalMax)})`
          : `Creator 0–${fmtBps(e.profit_share_creator_cap_bps)} (platform ${fmtBps(e.platform_profit_share_bps)} comes out of it)`,
      who: "Only on net realized profit above your high-water mark, settled daily. Losses must be recovered before any new profit share is charged.",
    },
    {
      item: "Paid posts",
      rate: `Price set by the creator (min ${fmtUsd(e.post_min_price_micro)})`,
      who: `Platform keeps ${fmtUsd(e.post_platform_fee_micro)} per sale.`,
    },
    {
      item: "Fee balance top-up",
      rate: `Min ${fmtUsd(e.min_topup_micro)}`,
      who: "USDC on Hyperliquid, or card / Apple Pay / Google Pay / local methods via Stripe. For card and wallet payments the processor fee is deducted from the amount credited; you see it before paying.",
    },
    {
      item: "If your fee balance runs out",
      rate: `${e.past_due_grace_hours}h grace`,
      who: "The subscription becomes past due, then reduce-only: no new positions are opened, exits still run.",
    },
  ];
  const cols: Column<FeeRow>[] = [
    { key: "item", label: "Fee", value: (r) => h("b", null, r.item), primary: true },
    { key: "rate", label: "Rate", value: (r) => r.rate },
    { key: "who", label: "Details", value: (r) => h("span", { class: "muted" }, r.who) },
  ];
  const out: Node[] = [table({ columns: cols, rows, rowKey: (r) => r.item, caption: "Fees charged by aijalon.trade" })];
  if (cfg._fallback) out.push(note("Showing default fees — live configuration could not be loaded. The fees shown at checkout are authoritative.", "info"));
  return out;
}

function plansTable(cfg: PublicConfig): Node[] {
  if (!cfg.plans?.length) return [];
  type P = PublicConfig["plans"][number];
  const featureLabel: Record<string, string> = {
    marketplace: "Marketplace",
    leaderboard: "Leaderboard",
    free_posts: "Free posts",
    paid_posts: "Paid posts",
    email_telegram_alerts: "Email/Telegram alerts",
    csv_export: "CSV / tax export",
    read_api: "Read API",
  };
  const cols: Column<P>[] = [
    { key: "plan", label: "Plan", value: (p) => h("b", null, p.key[0].toUpperCase() + p.key.slice(1)), primary: true },
    { key: "price", label: "Price / month", value: (p) => (p.price_monthly_micro > 0 ? fmtUsd(p.price_monthly_micro) : "Free"), mono: true },
    { key: "max", label: "Active strategies", value: (p) => (p.max_active_strategies === null ? "Unlimited" : String(p.max_active_strategies)), mono: true },
    { key: "features", label: "Includes", value: (p) => h("span", { class: "muted" }, p.features.map((f) => featureLabel[f] ?? f).join(", ")) },
  ];
  return [h("h3", null, "Platform plans"), table({ columns: cols, rows: cfg.plans, rowKey: (p) => p.key })];
}

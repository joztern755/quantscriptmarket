import { h } from "../../core/ui.js";
import { fmtUsd, fmtPct, fmtBps, fmtNum } from "../../core/format.js";
import type { StrategySummary } from "./types.js";
import { strategyBadges, marketChips, roiPct, MIN_SUBSCRIBERS_FOR_STATS } from "./util.js";

/** Marketplace card for one strategy (link to #/s/:slug). */
export function strategyCard(s: StrategySummary, opts: { featured?: boolean } = {}): HTMLElement {
  const subs = s.stats.subscribers;
  const pnlHidden = s.stats.pnl_micro === null || (subs !== null && subs < MIN_SUBSCRIBERS_FOR_STATS);
  const roi = roiPct(s.stats.roi_bps);
  const price = s.price_monthly_micro ?? 0;
  return h(
    "a",
    { class: ["s-card", opts.featured && "featured"], href: `#/s/${encodeURIComponent(s.slug)}` },
    h(
      "div",
      { class: "row between" },
      h("h3", null, s.name),
      opts.featured ? h("span", { class: "pill info" }, "Featured") : null,
    ),
    strategyBadges(s),
    s.showcase_text ? h("p", { class: "desc showcase" }, s.showcase_text) : s.description ? h("p", { class: "desc" }, s.description) : null,
    marketChips(s.markets),
    h(
      "div",
      { class: "metrics" },
      metric("Live ROI", roi === null ? "—" : fmtPct(roi, { sign: true }), roi === null ? undefined : roi >= 0 ? "pos" : "neg"),
      metric("Made for users", pnlHidden ? "hidden" : fmtUsd(s.stats.pnl_micro as number, { sign: true, compact: true })),
      metric("Subscribers", subs === null ? "—" : fmtNum(subs, 0)),
    ),
    h(
      "div",
      { class: "row between small" },
      h("span", null, h("b", { class: "mono" }, price > 0 ? fmtUsd(price) : "Free"), h("span", { class: "muted" }, " / month")),
      h("span", { class: "muted" }, "Profit share ", h("b", { class: "mono" }, fmtBps(s.profit_share_bps))),
    ),
  );
}

function metric(label: string, value: string, cls?: string): HTMLElement {
  return h("div", { class: "m" }, h("div", { class: "l" }, label), h("div", { class: ["v", cls] }, value));
}

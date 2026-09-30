// #/leaderboard — GET /v1/public/leaderboard?by=roi|pnl|subscribers&period=30d|90d|all
import type { PageContext } from "../core/router.js";
import { h, mount, table, tabs, skeleton, errorState, note, type Column } from "../core/ui.js";
import { api } from "../core/api.js";
import { fmtUsd, fmtPct, fmtNum } from "../core/format.js";
import type { LeaderRow } from "./_shared/types.js";
import { ensurePageCss, listOf, isAbortError, pageHead, replaceQuery, marketChips, MIN_SUBSCRIBERS_FOR_STATS, LIVE_PROVEN_DAYS, RISK_LINE } from "./_shared/util.js";

export const title = "Leaderboard";

const BY = ["roi", "pnl", "subscribers"] as const;
const PERIODS = ["30d", "90d", "all"] as const;
type By = (typeof BY)[number];
type Period = (typeof PERIODS)[number];

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  let by: By = (BY as readonly string[]).includes(ctx.query.get("by") ?? "") ? (ctx.query.get("by") as By) : "roi";
  let period: Period = (PERIODS as readonly string[]).includes(ctx.query.get("period") ?? "") ? (ctx.query.get("period") as Period) : "30d";

  const byTabs = h("div");
  const periodTabs = h("div");
  const body = h("div", { class: "stack" }, skeleton(6));
  mount(
    root,
    h(
      "div",
      { class: "stack" },
      pageHead("Leaderboard", "Top strategies", "Ranked on live on-chain results since each strategy's current version. Backtests are never ranked."),
      h("div", { class: "row between" }, byTabs, periodTabs),
      body,
      note(
        `$ made for users is shown only when a strategy has at least ${MIN_SUBSCRIBERS_FOR_STATS} subscribers (privacy). Strategies live for fewer than ${LIVE_PROVEN_DAYS} days are not yet live-proven. ${RISK_LINE}`,
        "info",
      ),
    ),
  );

  const drawTabs = (): void => {
    mount(byTabs, tabs([{ key: "roi", label: "ROI" }, { key: "pnl", label: "$ made" }, { key: "subscribers", label: "Subscribers" }], by, (k) => { by = k as By; drawTabs(); void load(); }));
    mount(periodTabs, tabs([{ key: "30d", label: "30 days" }, { key: "90d", label: "90 days" }, { key: "all", label: "All time" }], period, (k) => { period = k as Period; drawTabs(); void load(); }));
  };

  let seq = 0;
  const load = async (): Promise<void> => {
    const my = ++seq;
    replaceQuery("/leaderboard", { by: by !== "roi" ? by : null, period: period !== "30d" ? period : null });
    mount(body, skeleton(6));
    try {
      const res = await api.get<unknown>(`/public/leaderboard?by=${by}&period=${period}`, { signal: ctx.signal });
      if (!ctx.isCurrent() || my !== seq) return;
      const rows = listOf<LeaderRow>(res, "rows", "leaderboard", "strategies");
      const cols: Column<LeaderRow & { rank: number }>[] = [
        { key: "rank", label: "#", value: (r) => String(r.rank), mono: true, hideOnMobile: true },
        { key: "name", label: "Strategy", value: (r) => h("a", { href: `#/s/${encodeURIComponent(r.slug)}` }, r.name), primary: true },
        { key: "markets", label: "Markets", value: (r) => marketChips(r.markets), hideOnMobile: true },
        { key: "roi", label: "ROI", value: (r) => (typeof r.roi_pct === "number" ? h("span", { class: r.roi_pct >= 0 ? "pos" : "neg" }, fmtPct(r.roi_pct, { sign: true })) : "—"), align: "right", mono: true },
        {
          key: "pnl",
          label: "$ made for users",
          value: (r) => (typeof r.pnl_micro === "number" && (r.subscribers ?? 0) >= MIN_SUBSCRIBERS_FOR_STATS ? fmtUsd(r.pnl_micro, { sign: true }) : h("span", { class: "muted" }, "hidden")),
          align: "right",
          mono: true,
        },
        { key: "subs", label: "Subscribers", value: (r) => (typeof r.subscribers === "number" ? fmtNum(r.subscribers, 0) : "—"), align: "right", mono: true },
        { key: "live", label: "Live days", value: (r) => (typeof r.live_days === "number" ? fmtNum(r.live_days, 0) : "—"), align: "right", mono: true, hideOnMobile: true },
      ];
      mount(
        body,
        table({
          columns: cols,
          rows: rows.map((r, i) => ({ ...r, rank: i + 1 })),
          rowKey: (r) => r.slug,
          empty: "No live results for this period yet.",
          caption: `Leaderboard by ${by}, ${period}`,
        }),
      );
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent() || my !== seq) return;
      mount(body, errorState(err, () => void load()));
    }
  };
  drawTabs();
  await load();
}

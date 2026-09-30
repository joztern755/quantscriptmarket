// #/market — strategy list with filters (asset group, market) and sort (ROI / PnL / subscribers).
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note } from "../core/ui.js";
import { api } from "../core/api.js";
import type { StrategySummary } from "./_shared/types.js";
import { strategyCard } from "./_shared/strategy-card.js";
import { ensurePageCss, listOf, isAbortError, pageHead, replaceQuery, assetGroup, RISK_LINE, LIVE_PROVEN_DAYS } from "./_shared/util.js";

export const title = "Marketplace";

type SortKey = "roi" | "pnl" | "subscribers";
type Group = "all" | "crypto" | "tradfi";

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const state = {
    group: (["all", "crypto", "tradfi"].includes(ctx.query.get("asset") ?? "") ? ctx.query.get("asset") : "all") as Group,
    market: ctx.query.get("market") ?? "",
    sort: (["roi", "pnl", "subscribers"].includes(ctx.query.get("sort") ?? "") ? ctx.query.get("sort") : "roi") as SortKey,
    status: ctx.query.get("status") ?? "",
  };
  let all: StrategySummary[] = [];

  const groupSel = h(
    "select",
    { id: "f-asset", onchange: () => { state.group = groupSel.value as Group; state.market = ""; update(); } },
    opt("all", "All assets"),
    opt("crypto", "Crypto perps"),
    opt("tradfi", "Commodities & TradFi (HIP-3)"),
  );
  const marketSel = h("select", { id: "f-market", onchange: () => { state.market = marketSel.value; update(); } });
  const statusSel = h(
    "select",
    { id: "f-status", onchange: () => { state.status = statusSel.value; update(); } },
    opt("", "Any status"),
    opt("trades", "Trades (active signal)"),
    opt("holds", "Holds — no active signals"),
    opt("proven", `Live-proven (≥ ${LIVE_PROVEN_DAYS} days)`),
  );
  const sortSel = h(
    "select",
    { id: "f-sort", onchange: () => { state.sort = sortSel.value as SortKey; update(); } },
    opt("roi", "Sort: live ROI"),
    opt("pnl", "Sort: $ made for users"),
    opt("subscribers", "Sort: subscribers"),
  );
  groupSel.value = state.group;
  statusSel.value = state.status;
  sortSel.value = state.sort;

  const count = h("p", { class: "small muted", "aria-live": "polite" });
  const list = h("div", { class: "cards" }, skeleton(4), skeleton(4));

  mount(
    root,
    h(
      "div",
      { class: "stack" },
      pageHead("Marketplace", "Strategies", "Live track records are measured on-chain since each strategy's current version. A new version resets the record."),
      h(
        "div",
        { class: "filters", role: "search" },
        fieldBox("Asset", groupSel, "f-asset"),
        fieldBox("Market", marketSel, "f-market"),
        fieldBox("Status", statusSel, "f-status"),
        fieldBox("Sort", sortSel, "f-sort"),
      ),
      count,
      list,
      note(RISK_LINE, "warn"),
    ),
  );

  function fillMarkets(): void {
    const markets = [...new Set(all.flatMap((s) => s.markets ?? []))]
      .filter((m) => state.group === "all" || assetGroup(m) === state.group)
      .sort();
    if (state.market && !markets.includes(state.market)) markets.push(state.market); // keep deep-linked market selectable
    mount(marketSel, opt("", "All markets"), ...markets.map((m) => opt(m, m)));
    marketSel.value = state.market;
  }

  function update(): void {
    replaceQuery("/market", {
      asset: state.group !== "all" ? state.group : null,
      market: state.market || null,
      sort: state.sort !== "roi" ? state.sort : null,
      status: state.status || null,
    });
    fillMarkets();
    const rows = all
      .filter((s) => state.group === "all" || (s.markets ?? []).some((m) => assetGroup(m) === state.group))
      .filter((s) => !state.market || (s.markets ?? []).includes(state.market))
      .filter((s) => {
        if (!state.status) return true;
        if (state.status === "proven") return typeof s.live_days === "number" && s.live_days >= LIVE_PROVEN_DAYS;
        return s.signal_state === state.status;
      })
      .sort((a, b) => sortVal(b, state.sort) - sortVal(a, state.sort) || a.name.localeCompare(b.name));
    count.textContent = `${rows.length} of ${all.length} strategies`;
    if (!rows.length) {
      mount(
        list,
        emptyState(
          all.length ? "No strategies match these filters" : "No strategies listed yet",
          all.length ? "Try another market or asset group." : "Check back soon.",
          all.length ? h("button", { class: "btn", type: "button", onclick: reset }, "Clear filters") : null,
        ),
      );
      return;
    }
    mount(list, ...rows.map((s) => strategyCard(s, { featured: !!s.featured })));
  }

  function reset(): void {
    state.group = "all";
    state.market = "";
    state.status = "";
    groupSel.value = "all";
    statusSel.value = "";
    update();
  }

  const load = async (): Promise<void> => {
    mount(list, skeleton(4), skeleton(4));
    try {
      const res = await api.get<unknown>("/public/strategies", { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      all = listOf<StrategySummary>(res, "strategies");
      update();
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      count.textContent = "";
      mount(list, errorState(err, () => void load()));
    }
  };
  await load();
}

function sortVal(s: StrategySummary, key: SortKey): number {
  const v = key === "roi" ? s.roi_pct : key === "pnl" ? s.pnl_micro : s.subscribers;
  return typeof v === "number" && Number.isFinite(v) ? v : -Infinity;
}

function opt(value: string, label: string): HTMLOptionElement {
  return h("option", { value }, label);
}

function fieldBox(label: string, control: HTMLElement, id: string): HTMLElement {
  return h("div", { class: "field" }, h("label", { for: id }, label), control);
}

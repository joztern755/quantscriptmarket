// Walk-forward backtest panel, shared by the strategy page and Creator Studio.
import { h, note, kv, lineChart, skeleton } from "../../core/ui.js";
import { fmtPct, fmtNum, fmtDate } from "../../core/format.js";
import type { Backtest, BacktestPart } from "./types.js";
import { equityPoints, toMs, panel, BACKTEST_WARNING } from "./util.js";

function partStats(label: string, p: BacktestPart | undefined): HTMLElement {
  return h(
    "div",
    { class: "stack tight" },
    h("div", { class: "eyebrow" }, label),
    kv([
      ["Return", typeof p?.roi_pct === "number" ? fmtPct(p.roi_pct, { sign: true }) : "—"],
      ["Max drawdown", typeof p?.max_drawdown_pct === "number" ? fmtPct(-Math.abs(p.max_drawdown_pct)) : "—"],
      ["Sharpe", typeof p?.sharpe === "number" ? fmtNum(p.sharpe, 2) : "—"],
      ["Trades", typeof p?.trades === "number" ? fmtNum(p.trades, 0) : "—"],
      ["Period", p?.start && p?.end ? `${fmtDate(p.start)} – ${fmtDate(p.end)}` : "—"],
    ]),
  );
}

/** Walk-forward backtest panel (shared with Creator Studio). Always shows the mandatory warning. */
export function backtestPanel(bt: Backtest | null, titleText = "Walk-forward backtest"): HTMLElement {
  const warning = note(h("span", null, h("b", null, "Warning: "), BACKTEST_WARNING), "warn");
  if (!bt) return panel(titleText, warning, h("p", { class: "muted small" }, "No backtest available."));
  if (bt.status === "failed" || bt.error) return panel(titleText, warning, note(`Backtest failed: ${bt.error ?? "unknown error"}`, "bad"));
  if (bt.status === "pending" || bt.status === "running") return panel(titleText, warning, h("p", { class: "muted" }, "Backtest is running…"), skeleton(4));
  const isPts = equityPoints(bt.in_sample?.equity);
  const oosPts = equityPoints(bt.out_of_sample?.equity);
  let series: { name: string; points: { t: number; v: number }[]; tone: "accent" | "brass" }[] = [];
  if (isPts.length || oosPts.length) {
    if (isPts.length) series.push({ name: "In-sample (full history)", points: isPts, tone: "accent" });
    if (oosPts.length) series.push({ name: "Out-of-sample (last 30%)", points: oosPts, tone: "brass" });
  } else {
    const all = equityPoints(bt.equity);
    const cut = bt.oos_start !== null && bt.oos_start !== undefined ? toMs(bt.oos_start) : NaN;
    if (Number.isFinite(cut)) {
      series = [
        { name: "In-sample", points: all.filter((p) => p.t <= cut), tone: "accent" },
        { name: "Out-of-sample (last 30%)", points: all.filter((p) => p.t >= cut), tone: "brass" },
      ];
    } else if (all.length) series = [{ name: "Backtest equity", points: all, tone: "accent" }];
  }
  return panel(
    titleText,
    warning,
    h(
      "p",
      { class: "small muted" },
      "Simulated on Hyperliquid candles with taker fee + 0.1% builder fee and approximate funding. We report the full history (in-sample) and the last 30% (out-of-sample) separately.",
      bt.generated_at ? ` Generated ${fmtDate(bt.generated_at)}.` : "",
    ),
    h("div", { class: "grid-2" }, partStats("In-sample (full)", bt.in_sample), partStats("Out-of-sample (last 30%)", bt.out_of_sample)),
    series.some((x) => x.points.length >= 2)
      ? lineChart({ series, ariaLabel: "Backtest equity: in-sample vs out-of-sample", yFormat: (v) => fmtNum(v, 1) })
      : null,
    bt.fees_note ? h("p", { class: "small muted" }, bt.fees_note) : null,
  );
}


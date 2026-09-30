// Walk-forward backtest panel, shared by the strategy page, Creator Studio and the admin review queue.
// Input = the sandbox report (backend/app/sandbox/backtest.py; public copy drops trades/latest_signal/data_notes):
// metrics.{in_sample,out_of_sample,full}.{total_return,max_drawdown,sharpe,cagr} are FRACTIONS, split_t = start
// of the out-of-sample segment (ms), equity_curve = [[t_ms, equity], …].
import { h, note, kv, lineChart } from "../../core/ui.js";
import { fmtPct, fmtNum, fmtDate } from "../../core/format.js";
import type { BacktestMetrics, BacktestReport } from "./types.js";
import { equityPoints, panel, BACKTEST_WARNING } from "./util.js";

const pct = (x: unknown, sign = true): string => (typeof x === "number" && Number.isFinite(x) ? fmtPct(x * 100, { sign }) : "—");

function partStats(label: string, p: BacktestMetrics | undefined): HTMLElement {
  return h(
    "div",
    { class: "stack tight" },
    h("div", { class: "eyebrow" }, label),
    kv([
      ["Return", pct(p?.total_return)],
      ["CAGR", pct(p?.cagr)],
      ["Max drawdown", typeof p?.max_drawdown === "number" ? fmtPct(-Math.abs(p.max_drawdown) * 100) : "—"],
      ["Sharpe", typeof p?.sharpe === "number" ? fmtNum(p.sharpe, 2) : "—"],
      ["Period", typeof p?.start_t === "number" && typeof p?.end_t === "number" ? `${fmtDate(p.start_t)} – ${fmtDate(p.end_t)}` : "—"],
    ]),
  );
}

/** Walk-forward backtest panel. Always shows the mandatory warning (SPEC §9/§10) and, when given, the
 *  "Short history (N days)" warning (SPEC §12). */
export function backtestPanel(bt: BacktestReport | null, opts: { title?: string; warning?: string | null; shortHistoryDays?: number | null } = {}): HTMLElement {
  const titleText = opts.title ?? "Walk-forward backtest";
  const warning = note(h("span", null, h("b", null, "Warning: "), opts.warning || BACKTEST_WARNING), "warn");
  const short = typeof opts.shortHistoryDays === "number"
    ? note(h("span", null, h("b", null, `Short history (${opts.shortHistoryDays} days). `), "Hyperliquid serves limited candle history for this market and timeframe; results over such a short window are weak evidence."), "warn")
    : null;
  if (!bt) return panel(titleText, warning, short, h("p", { class: "muted small" }, "No backtest available."));
  const all = equityPoints(bt.equity_curve);
  const cut = typeof bt.metrics?.split_t === "number" ? bt.metrics.split_t : NaN;
  const series: { name: string; points: { t: number; v: number }[]; tone: "accent" | "brass" }[] = Number.isFinite(cut)
    ? [
        { name: "In-sample", points: all.filter((p) => p.t <= cut), tone: "accent" },
        { name: "Out-of-sample (last 30%)", points: all.filter((p) => p.t >= cut), tone: "brass" },
      ]
    : all.length
      ? [{ name: "Backtest equity", points: all, tone: "accent" }]
      : [];
  const days = typeof bt.history_days === "number" ? bt.history_days : bt.period?.sim_days;
  return panel(
    titleText,
    warning,
    short,
    h(
      "p",
      { class: "small muted" },
      "Simulated on Hyperliquid candles with taker fee + 0.1% builder fee and approximate funding. We report the in-sample part and the last 30% (out-of-sample) separately.",
      typeof days === "number" ? ` History: ${fmtNum(Math.floor(days), 0)} days.` : "",
      typeof bt.trade_count === "number" ? ` Trades: ${fmtNum(bt.trade_count, 0)}.` : "",
      bt.liquidated ? " The simulation was liquidated." : "",
    ),
    h("div", { class: "grid-2" }, partStats("In-sample", bt.metrics?.in_sample), partStats("Out-of-sample (last 30%)", bt.metrics?.out_of_sample)),
    series.some((x) => x.points.length >= 2)
      ? lineChart({ series, ariaLabel: "Backtest equity: in-sample vs out-of-sample", yFormat: (v) => fmtNum(v, 1) })
      : null,
    bt.warnings?.length ? h("ul", { class: "small muted" }, ...bt.warnings.map((w) => h("li", null, w))) : null,
  );
}

// #/s/:slug — strategy detail: live on-chain record (current version), $ made for users (k-anonymous),
// equity chart with version-reset markers, version history, walk-forward backtest (with mandatory warning),
// revealed showcase wallets, fees breakdown, reviews, posts, Subscribe.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, stat, kv, lineChart, table, button, toast, field, badge, type Column, type Child } from "../core/ui.js";
import { api, publicConfig } from "../core/api.js";
import { feeSummary } from "../core/gate.js";
import { fmtUsd, fmtPct, fmtBps, fmtNum, fmtDate, shortAddr } from "../core/format.js";
import type { StrategyDetail, StrategyVersionPublic, ShowcaseWallet, Review, PostSummary, EquityOut, EquityPoint } from "./_shared/types.js";
import { profitShare, builderSplit } from "./_shared/fees.js";
import { backtestPanel } from "./_shared/backtest.js";
import {
  ensurePageCss,
  listOf,
  isAbortError,
  roiPct,
  toMs,
  isRec,
  strategyBadges,
  marketChips,
  explorerAddressUrl,
  errCode,
  stars,
  panel,
  feesList,
  inlineMd,
  RISK_LINE,
  LOSS_WARNING,
  MIN_SUBSCRIBERS_FOR_STATS,
  LIVE_PROVEN_DAYS,
} from "./_shared/util.js";

export const title = "Strategy";

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const slug = ctx.params.slug ?? "";
  if (!/^[a-z0-9-]{1,64}$/.test(slug)) {
    mount(root, emptyState("Strategy not found", "Check the link or browse the marketplace.", h("a", { class: "btn", href: "#/market" }, "Marketplace")));
    return;
  }
  const load = async (): Promise<void> => {
    mount(root, skeleton(10));
    try {
      const s = await api.get<StrategyDetail>(`/public/strategies/${encodeURIComponent(slug)}`, { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      ctx.setTitle(s.name);
      draw(root, ctx, s);
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      if (errCode(err) === "not_found") {
        mount(root, emptyState("Strategy not found", "It may have been delisted.", h("a", { class: "btn", href: "#/market" }, "Marketplace")));
        return;
      }
      mount(root, errorState(err, () => void load()));
    }
  };
  await load();
}

function draw(root: HTMLElement, ctx: PageContext, s: StrategyDetail): void {
  const subscribeHref = `#/subscribe/${encodeURIComponent(s.slug)}`;
  const canSubscribe = s.status === "listed" && s.price_monthly_micro !== null;
  const price = s.price_monthly_micro ?? 0;
  const subBtn = canSubscribe
    ? h("a", { class: "btn primary", href: subscribeHref }, "Subscribe")
    : h("span", { class: "muted small" }, "Not accepting new subscribers");

  const showcaseBox = h("div", { class: "stack" }, skeleton(3));
  const reviewsBox = h("div", { class: "stack" }, skeleton(3));
  const postsBox = h("div", { class: "stack" }, skeleton(3));
  const feesBox = h("div", { class: "stack" }, skeleton(4));
  const equityBox = h("div", { class: "stack equity-box" }, skeleton(4));

  mount(
    root,
    h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "stack tight page-head" },
        h("div", { class: "eyebrow" }, s.in_house ? "In-house strategy" : "Creator strategy"),
        h("div", { class: "row between" }, h("h1", { class: "page-title" }, s.name), subBtn),
        strategyBadges(s),
        marketChips(s.markets),
        s.showcase_text ? note(h("span", null, h("b", null, "Free showcase. "), s.showcase_text), "info") : null,
        s.description ? h("p", { class: "muted" }, s.description) : null,
        typeof s.short_history_days === "number"
          ? note(`Short history (${s.short_history_days} days): this version's backtest covers less than a year of market data, next to the "not proven live" warning below.`, "warn")
          : null,
        h(
          "p",
          { class: "small" },
          h("b", { class: "mono" }, price > 0 ? fmtUsd(price) : "Free"),
          " / month · profit share ",
          h("b", { class: "mono" }, fmtBps(s.profit_share_bps)),
          s.max_leverage ? [" · strategy max leverage ", h("b", { class: "mono" }, `${s.max_leverage}×`)] : null,
          s.timeframe ? [" · ", s.timeframe, " bars"] : null,
        ),
      ),
      liveRecord(s, equityBox),
      versionHistory(s),
      backtestPanel(s.backtest, { warning: s.backtest_warning, shortHistoryDays: s.short_history_days }),
      panel("Fees", feesBox),
      panel(
        "Showcase wallets",
        h("p", { class: "small muted" }, "Each month one subscriber wallet (with consent) is revealed after that month ends, so anyone can check the actual trades on Hyperliquid's explorer. Other subscriber wallets are never shown."),
        showcaseBox,
      ),
      panel("Reviews", reviewsBox),
      panel(h("div", { class: "row between w-full" }, h("h2", null, "Posts"), h("a", { href: `#/posts?strategy=${encodeURIComponent(s.slug)}` }, "All posts →")), postsBox),
      h("div", { class: "panel accent stack" }, h("h2", null, "Ready to subscribe?"), note(LOSS_WARNING, "warn"), h("div", { class: "btns" }, subBtn.cloneNode(true) as HTMLElement, h("a", { class: "btn", href: "#/legal/risk-disclosure" }, "Risk disclosure"))),
      h("p", { class: "small muted" }, RISK_LINE),
    ),
  );

  void (async () => {
    const cfg = await publicConfig();
    if (!ctx.isCurrent()) return;
    const rows = feeSummary(cfg, { price_monthly_micro: price, profit_share_bps: s.profit_share_bps });
    const e = cfg.economics;
    const ps = profitShare(e, s.profit_share_bps, 1_000_000_000); // on $1,000 of new profit
    const bs = builderSplit(e, 10_000_000_000); // on $10,000 of notional traded
    mount(
      feesBox,
      feesList(rows),
      h(
        "div",
        { class: "preview-box small" },
        h("b", null, "Examples"),
        h("div", null, `If the strategy makes $1,000 of new profit above your high-water mark, you pay ${fmtUsd(ps.userPaysMicro)} profit share (creator ${fmtUsd(ps.creatorMicro)}, platform ${fmtUsd(ps.platformMicro)}).`),
        h("div", null, `Every $10,000 of order notional carries a ${fmtUsd(bs.feeMicro)} builder fee, collected by Hyperliquid from your trading account.`),
        h("div", { class: "muted" }, "Subscription and profit share are deducted from your prepaid fee balance, never from your trading account."),
      ),
    );
  })();

  void loadEquity(ctx, s, equityBox);
  void loadShowcase(ctx, s, showcaseBox);
  void loadReviews(ctx, s, reviewsBox);
  void loadPosts(ctx, s, postsBox);
}

function liveRecord(s: StrategyDetail, equityBox: HTMLElement): HTMLElement {
  const st = s.stats;
  const subs = st.subscribers;
  const hidden = st.pnl_micro === null || (subs !== null && subs < MIN_SUBSCRIBERS_FOR_STATS);
  const liveSince = s.live_since;
  const roi = roiPct(st.roi_bps);
  return panel(
    "Live on-chain record",
    h(
      "p",
      { class: "small muted" },
      s.current_version !== null ? `Current version v${s.current_version}` : "Current version",
      liveSince ? ` live since ${fmtDate(liveSince)}.` : " — not live yet.",
      " Results are measured from real fills on Hyperliquid for this version only; earlier versions do not count.",
    ),
    h(
      "div",
      { class: "stats" },
      stat("Live ROI", roi === null ? "—" : h("span", { class: roi >= 0 ? "pos" : "neg" }, fmtPct(roi, { sign: true })), "since current version"),
      stat("Made for users", hidden ? "hidden" : fmtUsd(st.pnl_micro as number, { sign: true }), hidden ? (st.hidden_reason === "not_live" ? "not live yet" : `needs ≥ ${MIN_SUBSCRIBERS_FOR_STATS} subscribers`) : "aggregate, net of fees"),
      stat("Subscribers", subs === null ? "—" : fmtNum(subs, 0)),
      stat("Days live", typeof s.live_days === "number" ? fmtNum(s.live_days, 0) : "—", s.not_live_proven ? `< ${LIVE_PROVEN_DAYS} days: not proven` : "live-proven"),
    ),
    hidden && st.hidden_reason !== "not_live"
      ? note(`Aggregate $ made for users is hidden until the strategy has at least ${MIN_SUBSCRIBERS_FOR_STATS} subscribers, so no individual subscriber's results can be inferred.`, "info")
      : null,
    equityBox,
  );
}

// ------------------------------------------------------------------------------------------ live equity chart
/** GET /v1/public/strategies/{slug}/equity → {points:[{t, pnl_micro, roi_bps}], hidden_reason}. Current version only;
 *  version resets (live_since of every later version) are drawn as markers when they fall inside the range. */
function parseEquity(raw: unknown): EquityOut {
  const r = isRec(raw) ? raw : {};
  const pts: EquityPoint[] = [];
  for (const p of Array.isArray(r.points) ? r.points : []) {
    if (!isRec(p)) continue;
    const t = toMs(p.t);
    const roi = typeof p.roi_bps === "number" && Number.isFinite(p.roi_bps) ? p.roi_bps : null;
    const pnl = typeof p.pnl_micro === "number" && Number.isFinite(p.pnl_micro) ? p.pnl_micro : null;
    if (Number.isFinite(t) && (roi !== null || pnl !== null)) pts.push({ t, roi_bps: roi, pnl_micro: pnl });
  }
  pts.sort((a, b) => a.t - b.t);
  return { points: pts, hidden_reason: typeof r.hidden_reason === "string" && r.hidden_reason ? r.hidden_reason : null };
}

function hiddenText(reason: string): string {
  if (reason === "not_live") return "The live chart starts once the current version is live and has real fills on Hyperliquid.";
  if (reason === "too_few_subscribers") return `The live chart is hidden until the strategy has at least ${MIN_SUBSCRIBERS_FOR_STATS} subscribers, so no individual subscriber's results can be inferred.`;
  return "The live chart is hidden right now.";
}

function resetMarkers(s: StrategyDetail): { t: number; label: string }[] {
  return s.versions
    .filter((v) => v.version > 1 && v.live_since)
    .map((v) => ({ t: toMs(v.live_since), label: `v${v.version} live — performance reset` }))
    .filter((m) => Number.isFinite(m.t));
}

async function loadEquity(ctx: PageContext, s: StrategyDetail, box: HTMLElement): Promise<void> {
  let eq: EquityOut;
  try {
    eq = parseEquity(await api.get<unknown>(`/public/strategies/${encodeURIComponent(s.slug)}/equity`, { signal: ctx.signal }));
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    if (errCode(err) === "not_found") {
      mount(box, h("p", { class: "small muted", dataset: { equity: "unavailable" } }, "The live chart isn't available for this strategy yet."));
      return;
    }
    mount(box, errorState(err, () => void loadEquity(ctx, s, box)));
    return;
  }
  if (!ctx.isCurrent()) return;
  const provenNote = s.not_live_proven
    ? note(`Not live-proven: ${typeof s.live_days === "number" ? `${fmtNum(s.live_days, 0)} day${s.live_days === 1 ? "" : "s"}` : "less than " + LIVE_PROVEN_DAYS + " days"} of live trading for this version (< ${LIVE_PROVEN_DAYS} days). Treat the live chart as early evidence only.`, "warn")
    : null;
  if (eq.hidden_reason || eq.points.length < 2) {
    const msg = eq.hidden_reason ? hiddenText(eq.hidden_reason) : "Not enough live history for a chart yet — it appears after the first days of live fills.";
    mount(box, h("div", { class: "equity-hidden", dataset: { equity: "hidden", reason: eq.hidden_reason ?? "empty" } }, note(msg, "info")), provenNote);
    return;
  }
  const hasRoi = eq.points.some((p) => p.roi_bps !== null);
  const hasPnl = eq.points.some((p) => p.pnl_micro !== null);
  let mode: "roi" | "pnl" = hasRoi ? "roi" : "pnl";
  const chartBox = h("div");
  const markers = resetMarkers(s);
  const draw = (): void => {
    const pts = eq.points
      .map((p) => ({ t: p.t, v: mode === "roi" ? (p.roi_bps === null ? NaN : p.roi_bps / 100) : p.pnl_micro === null ? NaN : p.pnl_micro / 1_000_000 }))
      .filter((p) => Number.isFinite(p.v));
    const last = pts[pts.length - 1]?.v ?? 0;
    mount(
      chartBox,
      lineChart({
        series: [{ name: mode === "roi" ? "Live ROI" : "Made for users", points: pts, tone: last >= 0 ? "good" : "bad" }],
        baseline: 0,
        markers,
        yFormat: mode === "roi" ? (v) => fmtPct(v, { sign: true, digits: 1 }) : (v) => fmtUsd(Math.trunc(v * 1_000_000), { sign: true, compact: true }),
        ariaLabel: mode === "roi" ? `Live ROI of ${s.name} since the current version went live` : `Aggregate PnL made for users by ${s.name} since the current version went live`,
      }),
    );
  };
  const seg: HTMLElement | null = hasRoi && hasPnl
    ? h(
        "div",
        { class: "seg", role: "group", "aria-label": "Chart metric" },
        ...(["roi", "pnl"] as const).map((k) => {
          const b = h("button", { type: "button", "aria-pressed": String(mode === k), onclick: () => {
            mode = k;
            seg?.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
            draw();
          } }, k === "roi" ? "ROI %" : "$ made");
          return b;
        }),
      )
    : null;
  draw();
  mount(
    box,
    h(
      "div",
      { class: "stack tight equity-live", dataset: { equity: "shown" } },
      h("div", { class: "row between" }, h("h3", null, "Live record"), seg),
      chartBox,
      h(
        "p",
        { class: "small muted" },
        "Real fills on Hyperliquid for the current version, net of trading, builder and platform fees plus funding.",
        markers.length ? " Vertical dashed lines mark version resets (a new version restarts the record)." : "",
      ),
    ),
    provenNote,
  );
}

function versionHistory(s: StrategyDetail): HTMLElement {
  const versions = [...s.versions].sort((a, b) => b.version - a.version);
  const curV = versions.find((v) => v.is_current)?.version ?? s.current_version ?? versions[0]?.version;
  if (!versions.length) return panel("Version history", h("p", { class: "muted small" }, "No published versions."));
  return panel(
    "Version history",
    h("p", { class: "small muted" }, "Publishing a new version resets the live track record. Each reset is marked below and on the chart."),
    h(
      "ol",
      { class: "timeline" },
      ...versions.map((v: StrategyVersionPublic) =>
        h(
          "li",
          { class: [v.version === curV && "current", v.version > 1 && "reset"] },
          h("div", { class: "row" }, h("b", null, `v${v.version}`), v.version === curV ? badge("current", "good") : null, v.version > 1 ? h("span", { class: "reset-tag" }, "performance reset") : null),
          h("div", { class: "small muted" }, v.published_at ? `Published ${fmtDate(v.published_at)}` : "Unpublished", v.live_since ? ` · live since ${fmtDate(v.live_since)}` : ""),
        ),
      ),
    ),
  );
}

async function loadShowcase(ctx: PageContext, s: StrategyDetail, box: HTMLElement): Promise<void> {
  const draw = (rows: ShowcaseWallet[]): void => {
    const revealed = rows.filter((r) => explorerAddressUrl(r.address));
    if (!revealed.length) {
      mount(box, emptyState("No wallets revealed yet", "The first wallet is revealed after the first full month of live trading."));
      return;
    }
    const cols: Column<ShowcaseWallet>[] = [
      { key: "month", label: "Month", value: (r) => r.period_month.slice(0, 7), primary: true, mono: true },
      {
        key: "addr",
        label: "Wallet",
        value: (r) => h("a", { href: explorerAddressUrl(r.address) ?? "#", target: "_blank", rel: "noopener noreferrer", class: "mono" }, shortAddr(r.address), " ↗"),
      },
      { key: "rev", label: "Revealed", value: (r) => fmtDate(r.revealed_at), hideOnMobile: true },
    ];
    mount(box, table({ columns: cols, rows: revealed.sort((a, b) => b.period_month.localeCompare(a.period_month)), rowKey: (r) => r.period_month + r.address }));
  };
  try {
    const res = await api.get<unknown>(`/public/showcase/${encodeURIComponent(s.slug)}`, { signal: ctx.signal });
    if (!ctx.isCurrent()) return;
    draw(listOf<ShowcaseWallet>(res));
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    mount(box, errorState(err, () => void loadShowcase(ctx, s, box)));
  }
}

async function loadReviews(ctx: PageContext, s: StrategyDetail, box: HTMLElement): Promise<void> {
  let reviews: Review[] = [];
  try {
    const res = await api.get<unknown>(`/public/strategies/${encodeURIComponent(s.slug)}/reviews`, { signal: ctx.signal });
    reviews = listOf<Review>(res);
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    if (errCode(err) !== "not_found") {
      mount(box, errorState(err, () => void loadReviews(ctx, s, box)));
      return;
    }
  }
  if (!ctx.isCurrent()) return;
  const avg = s.rating_avg_x100 !== null && s.rating_count > 0 ? s.rating_avg_x100 / 100 : reviews.length ? reviews.reduce((a, r) => a + r.rating, 0) / reviews.length : null;
  const count = Math.max(s.rating_count, reviews.length);
  mount(
    box,
    avg !== null ? h("p", null, h("span", { class: "stars", "aria-hidden": "true" }, stars(avg)), " ", h("b", null, fmtNum(avg, 1)), h("span", { class: "muted" }, ` · ${count} review${count === 1 ? "" : "s"}`)) : null,
    reviews.length
      ? h(
          "div",
          { class: "stack tight" },
          ...reviews.map((r) =>
            h(
              "div",
              { class: "review stack tight" },
              h("div", { class: "row between small" }, h("span", { class: "stars", "aria-label": `${r.rating} of 5` }, stars(r.rating)), h("span", { class: "muted" }, r.author, ` · ${fmtDate(r.created_at)}`)),
              r.body ? h("p", { class: "break" }, r.body) : null,
            ),
          ),
        )
      : emptyState("No reviews yet", "Reviews can be written by subscribers after 30 days."),
    reviewForm(ctx, s, () => void loadReviews(ctx, s, box)),
  );
}

function reviewForm(ctx: PageContext, s: StrategyDetail, onDone: () => void): HTMLElement {
  if (!ctx.user) return h("p", { class: "small muted" }, h("a", { href: `#/signin?next=${encodeURIComponent("/s/" + s.slug)}` }, "Sign in"), " to review (subscribers with 30+ days only).");
  const rating = h("select", { id: "rv-rating" }, ...[5, 4, 3, 2, 1].map((n) => h("option", { value: String(n) }, `${n} — ${stars(n)}`)));
  const body = h("textarea", { id: "rv-body", rows: 4, maxlength: 2000, placeholder: "What was your experience? (facts, not advice)" });
  const form = h(
    "details",
    null,
    h("summary", null, "Write a review"),
    h(
      "div",
      { class: "stack", style: { marginTop: "10px" } },
      h("p", { class: "small muted" }, "Only subscribers with at least 30 days on this strategy can review. One review per strategy."),
      field("Rating", rating),
      field("Review", body),
      h(
        "div",
        { class: "btns" },
        button("Submit review", {
          kind: "primary",
          onClick: async () => {
            const text = body.value.trim();
            if (text.length > 0 && text.length < 10) {
              toast("Please write at least a sentence (or leave it empty).", "warn");
              return;
            }
            try {
              await api.post("/reviews", { strategy_id: s.id, rating: Number(rating.value), body: text || null }, { signal: ctx.signal });
              toast("Review submitted.", "good");
              onDone();
            } catch (err) {
              const c = errCode(err);
              if (c === "forbidden") toast("You can review after 30 days subscribed.", "warn");
              else throw err;
            }
          },
        }),
      ),
    ),
  );
  return form;
}

async function loadPosts(ctx: PageContext, s: StrategyDetail, box: HTMLElement): Promise<void> {
  try {
    const res = await api.get<unknown>(`/public/posts?strategy=${encodeURIComponent(s.slug)}`, { signal: ctx.signal });
    if (!ctx.isCurrent()) return;
    const posts = listOf<PostSummary>(res).slice(0, 5);
    if (!posts.length) {
      mount(box, emptyState("No posts yet"));
      return;
    }
    mount(
      box,
      ...posts.map((p) =>
        h(
          "div",
          { class: "post-item" },
          h("a", { class: "title", href: `#/posts/${encodeURIComponent(p.id)}` }, p.title),
          h("div", { class: "small muted" }, p.price_micro > 0 ? `Paid · ${fmtUsd(p.price_micro)}` : "Free", p.published_at ? ` · ${fmtDate(p.published_at)}` : ""),
          p.preview ? inlineMd("p", p.preview, "small") : null,
        ),
      ),
    );
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    mount(box, errorState(err, () => void loadPosts(ctx, s, box)));
  }
}

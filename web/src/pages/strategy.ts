// #/s/:slug — strategy detail: live on-chain record (current version), $ made for users (k-anonymous),
// equity chart with version-reset markers, version history, walk-forward backtest (with mandatory warning),
// revealed showcase wallets, fees breakdown, reviews, posts, Subscribe.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, stat, kv, lineChart, table, button, toast, field, badge, type Column, type Child } from "../core/ui.js";
import { api, publicConfig } from "../core/api.js";
import { feeSummary } from "../core/gate.js";
import { fmtUsd, fmtPct, fmtBps, fmtNum, fmtDate, shortAddr } from "../core/format.js";
import type { StrategyDetail, StrategyVersion, ShowcaseWallet, Review, Post } from "./_shared/types.js";
import { profitShare, builderSplit } from "./_shared/fees.js";
import { backtestPanel } from "./_shared/backtest.js";
import {
  ensurePageCss,
  listOf,
  isAbortError,
  equityPoints,
  toMs,
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
  const canSubscribe = !s.status || s.status === "listed";
  const subBtn = canSubscribe
    ? h("a", { class: "btn primary", href: subscribeHref }, "Subscribe")
    : h("span", { class: "muted small" }, "Not accepting new subscribers");

  const showcaseBox = h("div", { class: "stack" }, skeleton(3));
  const reviewsBox = h("div", { class: "stack" }, skeleton(3));
  const postsBox = h("div", { class: "stack" }, skeleton(3));
  const feesBox = h("div", { class: "stack" }, skeleton(4));

  mount(
    root,
    h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "stack tight page-head" },
        h("div", { class: "eyebrow" }, s.in_house ? "In-house strategy" : s.creator_name ? `By ${s.creator_name}` : "Strategy"),
        h("div", { class: "row between" }, h("h1", { class: "page-title" }, s.name), subBtn),
        strategyBadges(s),
        marketChips(s.markets),
        s.description ? h("p", { class: "muted" }, s.description) : null,
        h(
          "p",
          { class: "small" },
          h("b", { class: "mono" }, s.price_monthly_micro > 0 ? fmtUsd(s.price_monthly_micro) : "Free"),
          " / month · profit share ",
          h("b", { class: "mono" }, fmtBps(s.profit_share_bps)),
          s.max_leverage ? [" · strategy max leverage ", h("b", { class: "mono" }, `${s.max_leverage}×`)] : null,
          s.timeframe ? [" · ", s.timeframe, " bars"] : null,
        ),
      ),
      liveRecord(s),
      versionHistory(s),
      backtestPanel(s.backtest ?? null),
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
    const rows = feeSummary(cfg, { price_monthly_micro: s.price_monthly_micro, profit_share_bps: s.profit_share_bps });
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

  void loadShowcase(ctx, s, showcaseBox);
  void loadReviews(ctx, s, reviewsBox);
  void loadPosts(ctx, s, postsBox);
}

function liveRecord(s: StrategyDetail): HTMLElement {
  const subs = typeof s.subscribers === "number" ? s.subscribers : null;
  const hidden = s.stats_hidden === true || s.pnl_micro === null || s.pnl_micro === undefined || (subs !== null && subs < MIN_SUBSCRIBERS_FOR_STATS);
  const cur = s.current_version ?? null;
  const liveSince = cur?.live_since ?? s.live_since ?? null;
  const pts = equityPoints(s.equity);
  const markers = (s.versions ?? [])
    .filter((v) => v.live_since && (v.version > 1 || v.reset))
    .map((v) => ({ t: toMs(v.live_since), label: `v${v.version} — reset` }))
    .filter((m) => Number.isFinite(m.t));
  const roi = typeof s.roi_pct === "number" ? s.roi_pct : null;
  return panel(
    "Live on-chain record",
    h(
      "p",
      { class: "small muted" },
      cur ? `Current version v${cur.version}` : "Current version",
      liveSince ? ` live since ${fmtDate(liveSince)}.` : " — not live yet.",
      " Results are measured from real fills on Hyperliquid for this version only; earlier versions do not count.",
    ),
    h(
      "div",
      { class: "stats" },
      stat("Live ROI", roi === null ? "—" : h("span", { class: roi >= 0 ? "pos" : "neg" }, fmtPct(roi, { sign: true })), "since current version"),
      stat("Made for users", hidden ? "hidden" : fmtUsd(s.pnl_micro as number, { sign: true }), hidden ? `needs ≥ ${MIN_SUBSCRIBERS_FOR_STATS} subscribers` : "aggregate, net of fees"),
      stat("Subscribers", subs === null ? "—" : fmtNum(subs, 0)),
      stat("Days live", typeof s.live_days === "number" ? fmtNum(s.live_days, 0) : "—", typeof s.live_days === "number" && s.live_days >= LIVE_PROVEN_DAYS ? "live-proven" : `< ${LIVE_PROVEN_DAYS} days: not proven`),
    ),
    hidden
      ? note(`Aggregate $ made for users is hidden until the strategy has at least ${MIN_SUBSCRIBERS_FOR_STATS} subscribers, so no individual subscriber's results can be inferred.`, "info")
      : null,
    pts.length >= 2
      ? lineChart({ series: [{ name: "Live equity (index)", points: pts, tone: "accent" }], markers, ariaLabel: `${s.name} live equity since current version`, yFormat: (v) => fmtNum(v, 1) })
      : emptyState("No live history yet", "The chart appears after the current version has traded on-chain."),
  );
}

function versionHistory(s: StrategyDetail): HTMLElement {
  const versions = [...(s.versions ?? [])].sort((a, b) => b.version - a.version);
  const curV = s.current_version?.version ?? versions[0]?.version;
  if (!versions.length) return panel("Version history", h("p", { class: "muted small" }, "No published versions."));
  return panel(
    "Version history",
    h("p", { class: "small muted" }, "Publishing a new version resets the live track record. Each reset is marked below and on the chart."),
    h(
      "ol",
      { class: "timeline" },
      ...versions.map((v: StrategyVersion) =>
        h(
          "li",
          { class: [v.version === curV && "current", (v.version > 1 || v.reset) && "reset"] },
          h("div", { class: "row" }, h("b", null, `v${v.version}`), v.version === curV ? badge("current", "good") : null, v.version > 1 || v.reset ? h("span", { class: "reset-tag" }, "performance reset") : null),
          h("div", { class: "small muted" }, v.published_at ? `Published ${fmtDate(v.published_at)}` : "Unpublished", v.live_since ? ` · live since ${fmtDate(v.live_since)}` : ""),
          v.note ? h("div", { class: "small" }, v.note) : null,
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
      { key: "month", label: "Month", value: (r) => r.period_month, primary: true, mono: true },
      {
        key: "addr",
        label: "Wallet",
        value: (r) => h("a", { href: explorerAddressUrl(r.address) ?? "#", target: "_blank", rel: "noopener noreferrer", class: "mono" }, shortAddr(r.address), " ↗"),
      },
      { key: "roi", label: "Month ROI", value: (r) => (typeof r.roi_pct === "number" ? fmtPct(r.roi_pct, { sign: true }) : "—"), align: "right", mono: true },
    ];
    mount(box, table({ columns: cols, rows: revealed.sort((a, b) => b.period_month.localeCompare(a.period_month)), rowKey: (r) => r.period_month + r.address }));
  };
  if (Array.isArray(s.showcase)) {
    draw(s.showcase);
    return;
  }
  try {
    const res = await api.get<unknown>(`/public/showcase/${encodeURIComponent(s.slug)}`, { signal: ctx.signal });
    if (!ctx.isCurrent()) return;
    draw(listOf<ShowcaseWallet>(res, "wallets", "showcase"));
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    mount(box, errorState(err, () => void loadShowcase(ctx, s, box)));
  }
}

async function loadReviews(ctx: PageContext, s: StrategyDetail & { reviews?: Review[]; rating_avg?: number | null }, box: HTMLElement): Promise<void> {
  let reviews: Review[] = [];
  try {
    if (Array.isArray(s.reviews)) reviews = s.reviews;
    else {
      const res = await api.get<unknown>(`/public/strategies/${encodeURIComponent(s.slug)}/reviews`, { signal: ctx.signal });
      reviews = listOf<Review>(res, "reviews");
    }
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    if (errCode(err) !== "not_found") {
      mount(box, errorState(err, () => void loadReviews(ctx, s, box)));
      return;
    }
  }
  if (!ctx.isCurrent()) return;
  const avg = reviews.length ? reviews.reduce((a, r) => a + r.rating, 0) / reviews.length : null;
  mount(
    box,
    avg !== null ? h("p", null, h("span", { class: "stars", "aria-hidden": "true" }, stars(avg)), " ", h("b", null, fmtNum(avg, 1)), h("span", { class: "muted" }, ` · ${reviews.length} review${reviews.length === 1 ? "" : "s"}`)) : null,
    reviews.length
      ? h(
          "div",
          { class: "stack tight" },
          ...reviews.map((r) =>
            h(
              "div",
              { class: "review stack tight" },
              h("div", { class: "row between small" }, h("span", { class: "stars", "aria-label": `${r.rating} of 5` }, stars(r.rating)), h("span", { class: "muted" }, r.author ?? "Subscriber", r.created_at ? ` · ${fmtDate(r.created_at)}` : "")),
              h("p", { class: "break" }, r.body),
            ),
          ),
        )
      : emptyState("No reviews yet", "Reviews can be written by subscribers after 30 days."),
    reviewForm(ctx, s, () => void loadReviews(ctx, { ...s, reviews: undefined }, box)),
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
            if (text.length < 10) {
              toast("Please write at least a sentence.", "warn");
              return;
            }
            try {
              await api.post("/reviews", { strategy_id: s.id, rating: Number(rating.value), body: text }, { signal: ctx.signal });
              toast("Review submitted.", "good");
              onDone();
            } catch (err) {
              const c = errCode(err);
              if (c === "forbidden" || c === "conflict" || c === "validation_failed") toast(c === "conflict" ? "You have already reviewed this strategy." : "You can review after 30 days subscribed.", "warn");
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
    const posts = listOf<Post>(res, "posts").slice(0, 5);
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
          p.excerpt ? inlineMd("p", p.excerpt, "small") : null,
        ),
      ),
    );
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    mount(box, errorState(err, () => void loadPosts(ctx, s, box)));
  }
}

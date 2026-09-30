// #/creator[/:tab] — Creator Studio: KYC status/CTA, my strategies, new strategy (with live fee preview),
// upload Python or build with the no-code builder → validate → backtest → submit for review,
// posts editor (free/paid ≥ min price), earnings.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, stat, kv, table, tabs, button, toast, confirmDialog, field, checkbox, badge, type Column, type Child } from "../core/ui.js";
import { api, publicConfig, newIdempotencyKey, type PublicConfig } from "../core/api.js";
import { LEGAL_SLUGS, legalDocHash } from "../core/gate.js";
import { fmtUsd, fmtBps, fmtDate, fmtDateTime, fmtTenthsBp } from "../core/format.js";
import type { CreatorStrategy, CreatorVersion, CreatorPost, Earnings, NoCodeSpec } from "./_shared/types.js";
import { payoutForm } from "./_shared/payout.js";
import { profitShare, subscriptionSplit, builderSplit, postSplit } from "./_shared/fees.js";
import { backtestPanel } from "./_shared/backtest.js";
import { noCodeBuilder } from "./_shared/nocode-ui.js";
import { precheckPython, PYTHON_TEMPLATE, defaultSpec } from "./_shared/nocode.js";
import { renderMarkdown } from "./_shared/markdown.js";
import { trustAnchors } from "../core/config.js";
import { ensurePageCss, listOf, isAbortError, errCode, errMessage, pageHead, panel, usdInput, pctToBps, bpsToPctInput, isRec, kvWide, BACKTEST_WARNING } from "./_shared/util.js";

export const title = "Creator Studio";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "new", label: "New strategy" },
  { key: "upload", label: "Upload version" },
  { key: "posts", label: "Posts" },
  { key: "earnings", label: "Earnings" },
];

const COIN_RE = /^(?:[a-z0-9]{1,12}:)?[A-Za-z0-9]{1,20}$/;
const LAUNCH_MARKETS = ["BTC", "SOL", "HYPE", "xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:BRENTOIL"];

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const tab = TABS.some((t) => t.key === ctx.params.tab) ? (ctx.params.tab as string) : "overview";
  const cfg = await publicConfig();
  if (!ctx.isCurrent()) return;
  const body = h("div", { class: "stack" });
  mount(
    root,
    h(
      "div",
      { class: "stack" },
      pageHead("Creator Studio", "Build and publish strategies", "Your code runs only in our sandbox; subscribers never see it. Every version is validated, backtested and reviewed before listing."),
      !cfg.features.creator_uploads ? note("Creator uploads are paused right now. You can still manage posts and view earnings.", "warn") : null,
      kycBanner(ctx, cfg),
      tabs(TABS, tab, (k) => ctx.navigate(k === "overview" ? "/creator" : `/creator/${k}`)),
      body,
    ),
  );
  if (tab === "new") return newStrategyTab(body, ctx, cfg);
  if (tab === "upload") return uploadTab(body, ctx, cfg);
  if (tab === "posts") return postsTab(body, ctx, cfg);
  if (tab === "earnings") return earningsTab(body, ctx, cfg);
  return overviewTab(body, ctx);
}

// ------------------------------------------------------------------------------------------ KYC
/** MeOut.kyc_status: pending | approved | rejected | null (not started). */
function kycStatus(ctx: PageContext): string {
  const v = (ctx.me as Record<string, unknown> | null)?.kyc_status;
  return typeof v === "string" ? v : "none";
}

function kycBanner(ctx: PageContext, cfg: PublicConfig): HTMLElement {
  const status = kycStatus(ctx);
  if (status === "approved") return h("p", { class: "small" }, badge("Identity verified", "good"), " You can list strategies and receive payouts.");
  const manualBox = h("div");
  const agree = checkbox(h("span", null, "I have read and accept the ", h("a", { href: `#/legal/${LEGAL_SLUGS.creator_agreement ?? "creator-agreement"}`, target: "_blank", rel: "noopener" }, "Creator Agreement"), " and ", h("a", { href: "#/legal/acceptable-use", target: "_blank", rel: "noopener" }, "Acceptable Use Policy"), "."), { required: true });
  return h(
    "div",
    { class: "panel stack" },
    h("div", { class: "row between" }, h("h2", null, "Identity verification (KYC)"), badge(status === "pending" ? "Pending review" : status === "rejected" ? "Rejected" : "Not started", status === "rejected" ? "bad" : "warn")),
    h("p", { class: "small muted" }, "Required before any strategy can be listed or any payout is made. Documents are handled by our verification provider and are not stored by aijalon.trade. You can draft strategies and run backtests meanwhile."),
    status === "pending"
      ? h("p", { class: "small" }, "Your verification is being reviewed. This usually takes less than a day.")
      : h(
          "div",
          { class: "stack tight" },
          agree.el,
          h(
            "div",
            { class: "btns" },
            button(status === "rejected" ? "Retry verification" : "Start verification", {
              kind: "primary",
              onClick: async () => {
                if (!agree.input.checked) return toast("Please accept the Creator Agreement first.", "warn");
                const version = cfg.legal_versions.creator_agreement;
                if (!version) throw new Error("Couldn't load the current Creator Agreement. Try again in a moment.");
                await api.post("/consents", {
                  consents: [{ doc: "creator_agreement", doc_version: version, context: "creator", strategy_id: null, accepted_at: new Date().toISOString(), doc_text_sha256: await legalDocHash("creator_agreement", version) }],
                });
                const res = await api.post<{ url: string; provider: string; status: string; manual: boolean }>("/creator/kyc/session", {}, { signal: ctx.signal });
                if (res.manual) {
                  mount(manualBox, note("Your identity check will be reviewed by our team. We'll contact you by email; no documents are uploaded here.", "info"));
                  return;
                }
                const url = typeof res.url === "string" ? res.url : "";
                // SECURITY L2: only the KYC provider's own host (pinned in app-config.json), exact match, https.
                let target: URL | null = null;
                try {
                  target = new URL(url);
                } catch {
                  target = null;
                }
                const allowed = trustAnchors().kycRedirectHosts;
                if (!target || target.protocol !== "https:" || target.username || target.password || target.port || !allowed.includes(target.host.toLowerCase())) {
                  throw new Error("Verification could not be started (unexpected verification address). Please contact support.");
                }
                window.location.assign(target.href);
              },
            }),
          ),
          manualBox,
        ),
  );
}

// ------------------------------------------------------------------------------------------ overview
async function loadMyStrategies(ctx: PageContext): Promise<CreatorStrategy[]> {
  try {
    const res = await api.get<unknown>("/creator/strategies", { signal: ctx.signal });
    return listOf<CreatorStrategy>(res);
  } catch (err) {
    if (errCode(err) === "not_found") return [];
    throw err;
  }
}

async function overviewTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" }, skeleton(6));
  const earn = h("div", { class: "stats" }, skeleton(2));
  mount(body, earn, panel(h("div", { class: "row between w-full" }, h("h2", null, "My strategies"), h("a", { class: "btn sm primary", href: "#/creator/new" }, "New strategy")), box));
  const load = async (): Promise<void> => {
    mount(box, skeleton(6));
    try {
      const [list, e] = await Promise.all([loadMyStrategies(ctx), api.get<Earnings>("/creator/earnings", { signal: ctx.signal }).catch(() => null)]);
      const subsBy = new Map((e?.by_strategy ?? []).map((x) => [x.strategy_id, x.active_subscribers ?? null]));
      if (!ctx.isCurrent()) return;
      mount(
        earn,
        stat("Strategies", String(list.length)),
        stat("Listed", String(list.filter((s) => s.status === "listed").length)),
        stat("Earned (all time)", e ? fmtUsd(e.total_earned_micro) : "—"),
        stat("Payable", e ? fmtUsd(e.payable_micro) : "—"),
      );
      const cols: Column<CreatorStrategy>[] = [
        { key: "name", label: "Strategy", value: (s) => (s.status === "listed" ? h("a", { href: `#/s/${encodeURIComponent(s.slug)}` }, s.name) : s.name), primary: true },
        { key: "status", label: "Status", value: (s) => badge(s.status, s.status === "listed" ? "good" : s.status === "review" ? "info" : s.status === "delisted" ? "bad" : "muted") },
        { key: "tf", label: "Timeframe", value: (s) => s.timeframe },
        { key: "markets", label: "Markets", value: (s) => s.markets.join(", "), hideOnMobile: true },
        { key: "price", label: "Price / mo", value: (s) => (typeof s.price_monthly_micro === "number" ? fmtUsd(s.price_monthly_micro) : "—"), align: "right", mono: true },
        { key: "ps", label: "Profit share", value: (s) => (typeof s.profit_share_bps === "number" ? fmtBps(s.profit_share_bps) : "—"), align: "right", mono: true },
        { key: "subs", label: "Subscribers", value: (s) => { const n = subsBy.get(s.id); return typeof n === "number" ? String(n) : "—"; }, align: "right", mono: true },
        { key: "act", label: "", value: (s) => h("a", { class: "btn sm", href: `#/creator/upload?strategy=${encodeURIComponent(s.id)}` }, "Upload version") },
      ];
      mount(box, list.length ? table({ columns: cols, rows: list, rowKey: (s) => s.id }) : emptyState("No strategies yet", "Create your first strategy, then upload code or use the no-code builder.", h("a", { class: "btn primary", href: "#/creator/new" }, "New strategy")));
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(box, errorState(err, () => void load()));
    }
  };
  await load();
}

// ------------------------------------------------------------------------------------------ new strategy
async function newStrategyTab(body: HTMLElement, ctx: PageContext, cfg: PublicConfig): Promise<void> {
  const e = cfg.economics;
  const cap = e.profit_share_creator_cap_bps;
  const name = h("input", { type: "text", maxlength: 64, placeholder: "e.g. Silver trend 1D", id: "ns-name" });
  // slug (URL id, unique, immutable): backend SLUG_RE ^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])$
  const slug = h("input", { type: "text", maxlength: 48, placeholder: "silver-trend-1d", id: "ns-slug", spellcheck: "false", autocomplete: "off" });
  let slugTouched = false;
  const slugify = (x: string): string => x.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48).replace(/-+$/g, "");
  name.addEventListener("input", () => { if (!slugTouched) slug.value = slugify(name.value); });
  slug.addEventListener("input", () => { slugTouched = true; });
  const desc = h("textarea", { rows: 4, maxlength: 2000, id: "ns-desc", placeholder: "What the strategy does, when it trades, when it holds. No performance promises." });
  const markets = h("input", { type: "text", id: "ns-markets", placeholder: "BTC, xyz:SILVER", spellcheck: "false" });
  const chips = h(
    "div",
    { class: "row tight-row" },
    ...LAUNCH_MARKETS.map((m) =>
      h("button", {
        type: "button",
        class: "chip",
        onclick: () => {
          const cur = parseMarkets(markets.value);
          if (!cur.includes(m) && cur.length < 5) cur.push(m);
          markets.value = cur.join(", ");
          preview();
        },
      }, `+ ${m}`),
    ),
  );
  const tf = h("select", { id: "ns-tf" }, h("option", { value: "1d" }, "1 day"), h("option", { value: "4h" }, "4 hours"), h("option", { value: "1h" }, "1 hour"));
  const price = usdInput({ placeholder: "e.g. 29", id: "ns-price", value: "0" });
  const ps = h("input", { type: "text", inputmode: "decimal", id: "ns-ps", value: "10", placeholder: `0–${bpsToPctInput(cap)}` });
  const prev = h("div", { class: "preview-box small", "aria-live": "polite" });

  const preview = (): void => {
    const p = price.micro() ?? 0;
    const bps = pctToBps(ps.value);
    const sub = subscriptionSplit(e, p);
    const profit = profitShare(e, bps ?? 0, 1_000_000_000);
    const bs = builderSplit(e, 100_000_000_000);
    mount(
      prev,
      h("b", null, "What a subscriber pays and what you receive"),
      bps === null || bps > cap ? h("p", { class: "neg" }, `Profit share must be 0–${fmtBps(cap)}.`) : null,
      kvWide([
        ["Subscription (per month)", `Subscriber pays ${fmtUsd(p)} → you receive ${fmtUsd(sub.creatorMicro)} (platform ${fmtBps(e.subscription_platform_bps)} = ${fmtUsd(sub.platformMicro)})`],
        [
          "Profit share on $1,000 new profit",
          `Subscriber pays ${fmtUsd(profit.userPaysMicro)} (${fmtBps(profit.userPaysBps)}) → you receive ${fmtUsd(profit.creatorMicro)}, platform ${fmtUsd(profit.platformMicro)}` +
            (e.platform_profit_share_mode === "on_top" ? ` (platform ${fmtBps(e.platform_profit_share_bps)} is added on top)` : ` (platform ${fmtBps(e.platform_profit_share_bps)} comes out of your share)`),
        ],
        [
          "Builder fee on $100,000 traded",
          `${fmtTenthsBp(e.builder_fee_tenths_bp)} = ${fmtUsd(bs.feeMicro)} paid by subscribers → you receive ${fmtUsd(bs.creatorMicro)}, platform ${fmtUsd(bs.platformMicro)}, referral pool ${fmtUsd(bs.referralPoolMicro)}`,
        ],
      ]),
      h("p", { class: "muted" }, "Profit share is charged only on net realized profit above each subscriber's high-water mark."),
    );
  };
  for (const el of [price.el, ps, markets]) el.addEventListener("input", preview);
  preview();

  const key = newIdempotencyKey();
  mount(
    body,
    panel(
      "New strategy",
      h(
        "div",
        { class: "form-grid two-col" },
        field("Name", name),
        field("URL name (slug)", slug, "Lower-case letters, digits and dashes; cannot be changed later."),
        field("Timeframe", tf),
        h("div", { class: "stack tight" }, field("Markets (1–5)", markets, "Hyperliquid perp coins. Builder-deployed markets use dex:COIN."), chips),
        field("Description", desc),
        field("Price per month (USD)", price.el, "0 for free."),
        field(`Profit share % (0–${bpsToPctInput(cap)})`, ps, "Of net realized profit above the high-water mark."),
      ),
      prev,
      note("After creating the strategy, upload a script or build one with the no-code builder. Listing requires identity verification and admin review.", "info"),
      h(
        "div",
        { class: "btns" },
        button("Create strategy", {
          kind: "primary",
          onClick: async () => {
            const n = name.value.trim();
            const mk = parseMarkets(markets.value);
            const p = price.micro() ?? (price.el.value.trim() === "" ? 0 : null);
            const bps = pctToBps(ps.value);
            if (n.length < 3) return toast("Name must be at least 3 characters.", "warn");
            const sl = slug.value.trim();
            if (!/^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])$/.test(sl)) return toast("Slug: 3–48 lower-case letters, digits or dashes (not at the ends).", "warn");
            if (!mk.length || mk.length > 5 || !mk.every((m) => COIN_RE.test(m))) return toast("Enter 1–5 valid markets.", "warn");
            if (p === null) return toast("Enter a valid monthly price.", "warn");
            if (bps === null || bps > cap) return toast(`Profit share must be 0–${fmtBps(cap)}.`, "warn");
            const res = await api.post<CreatorStrategy>(
              "/creator/strategies",
              { slug: sl, name: n, description: desc.value.trim() || null, markets: mk, timeframe: tf.value, price_monthly_micro: p, profit_share_bps: bps },
              { signal: ctx.signal, idempotencyKey: key },
            );
            toast("Strategy created. Now upload a version.", "good");
            const id = typeof res.id === "string" ? res.id : "";
            ctx.navigate(id ? `/creator/upload?strategy=${encodeURIComponent(id)}` : "/creator");
          },
        }),
      ),
    ),
  );
}

function parseMarkets(s: string): string[] {
  return [...new Set(s.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean))];
}

// ------------------------------------------------------------------------------------------ upload version
async function uploadTab(body: HTMLElement, ctx: PageContext, cfg: PublicConfig): Promise<void> {
  mount(body, skeleton(8));
  let list: CreatorStrategy[];
  try {
    list = await loadMyStrategies(ctx);
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    mount(body, errorState(err, () => void uploadTab(body, ctx, cfg)));
    return;
  }
  if (!ctx.isCurrent()) return;
  const editable = list.filter((s) => s.status !== "delisted");
  if (!editable.length) {
    mount(body, emptyState("Create a strategy first", undefined, h("a", { class: "btn primary", href: "#/creator/new" }, "New strategy")));
    return;
  }
  const pre = ctx.query.get("strategy") ?? "";
  const stratSel = h("select", { id: "up-strat" }, ...editable.map((s) => h("option", { value: s.id }, `${s.name} (${s.status})`)));
  if (editable.some((s) => s.id === pre)) stratSel.value = pre;
  const selected = (): CreatorStrategy => editable.find((s) => s.id === stratSel.value) ?? editable[0];

  let mode: "python" | "nocode" = "python";
  const modeBox = h("div");
  const editorBox = h("div", { class: "stack" });
  const resultBox = h("div", { class: "stack" });
  const resetWarn = h("div");

  const code = h("textarea", { class: "code", spellcheck: "false", autocomplete: "off", "aria-label": "Python source", rows: 18 });
  code.value = PYTHON_TEMPLATE;
  const pyIssues = h("div", { "aria-live": "polite" });
  const checkPy = (): void => {
    const errs = precheckPython(code.value);
    mount(pyIssues, errs.length ? h("ul", { class: "small neg" }, ...errs.map((e) => h("li", null, e))) : h("p", { class: "small pos" }, "Quick checks passed. The sandbox runs the full validator on upload."));
  };
  code.addEventListener("input", checkPy);
  // Tab key inserts 4 spaces in the code editor (Esc then Tab still moves focus)
  let escaped = false;
  code.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") escaped = true;
    else if (ev.key === "Tab" && !ev.shiftKey && !escaped) {
      ev.preventDefault();
      code.setRangeText("    ", code.selectionStart, code.selectionEnd, "end");
      checkPy();
    } else escaped = false;
  });
  const file = h("input", { type: "file", accept: ".py,text/x-python,text/plain", "aria-label": "Upload .py file" });
  file.addEventListener("change", async () => {
    const f = file.files?.[0];
    if (!f) return;
    if (f.size > 64 * 1024) {
      toast("File is larger than 64 KB.", "warn");
      file.value = "";
      return;
    }
    code.value = await f.text();
    checkPy();
  });
  checkPy();
  let spec: NoCodeSpec | undefined;
  // The spec's MARKETS/TIMEFRAME must match the strategy (server: 422 otherwise) → start from the strategy's.
  const specFor = (s: CreatorStrategy): NoCodeSpec => ({
    ...defaultSpec(s.markets[0] ?? "BTC"),
    markets: [...s.markets],
    timeframe: (["1h", "4h", "1d"].includes(s.timeframe) ? s.timeframe : "1d") as NoCodeSpec["timeframe"],
    max_leverage: Math.min(5, Math.max(1, s.markets.length)),
  });
  let builder = noCodeBuilder(specFor(selected()), (sp) => { spec = sp; });

  const drawMode = (): void => {
    mount(
      modeBox,
      h(
        "div",
        { class: "seg", role: "group", "aria-label": "Source" },
        h("button", { type: "button", "aria-pressed": String(mode === "python"), onclick: () => { mode = "python"; drawMode(); } }, "Python"),
        h("button", { type: "button", "aria-pressed": String(mode === "nocode"), onclick: () => { mode = "nocode"; drawMode(); } }, "No-code builder"),
      ),
    );
    mount(
      editorBox,
      mode === "python"
        ? h(
            "div",
            { class: "stack" },
            h("p", { class: "small muted" }, "Single module ≤ 64 KB. Imports: math, statistics only. Define MARKETS, TIMEFRAME, LOOKBACK, MAX_LEVERAGE and signal(bars) returning {coin: weight}. No I/O, randomness or time."),
            field("Load a .py file", file),
            code,
            pyIssues,
          )
        : builder.el,
    );
  };
  // GET /creator/strategies/{id}/versions → the live (published) version, if any: uploading + listing a new one resets it.
  let published: CreatorVersion | null = null;
  const drawWarn = async (): Promise<void> => {
    const s = selected();
    mount(resetWarn);
    try {
      const versions = listOf<CreatorVersion>(await api.get<unknown>(`/creator/strategies/${encodeURIComponent(s.id)}/versions`, { signal: ctx.signal }));
      if (!ctx.isCurrent() || selected().id !== s.id) return;
      published = versions.filter((v) => v.published_at).sort((a, b) => b.version - a.version)[0] ?? null;
      mount(
        resetWarn,
        published
          ? note(h("span", null, h("b", null, "Listing a new version resets your live track record. "), `v${published.version}'s live ROI and $ made will no longer be shown as current; the new version starts from zero and is marked "Not live-proven" for 90 days.`), "bad")
          : null,
        versions.length ? h("p", { class: "small muted" }, `Uploaded versions: ${versions.map((v) => `v${v.version}${v.published_at ? " (listed)" : ""}`).join(", ")}.`) : null,
      );
    } catch (err) {
      if (!isAbortError(err)) mount(resetWarn, h("p", { class: "small muted" }, "Couldn't load this strategy's versions."));
    }
  };
  stratSel.addEventListener("change", () => {
    builder = noCodeBuilder(specFor(selected()), (sp) => { spec = sp; });
    spec = undefined;
    drawMode();
    void drawWarn();
    mount(resultBox);
  });
  drawMode();
  void drawWarn();

  /** Upload result: validation already passed (422 otherwise) and the walk-forward backtest ran in the sandbox.
   *  The strategy moves to "review" automatically; an admin lists the version (maker-checker). */
  const showResult = (s: CreatorStrategy, r: CreatorVersion | { errors: string[] }): void => {
    if ("errors" in r) {
      mount(resultBox, panel("Validation", h("div", { class: "stack tight" }, h("p", { class: "neg" }, "The sandbox rejected this version:"), h("ul", { class: "small" }, ...r.errors.map((e) => h("li", { class: "break" }, e))))));
      return;
    }
    const bt = r.backtest;
    const days = typeof bt?.history_days === "number" ? bt.history_days : bt?.period?.sim_days;
    const shortDays = typeof days === "number" && days < cfg.short_history_warning_days ? Math.floor(days) : null;
    mount(
      resultBox,
      panel(
        `Validation — v${r.version}`,
        h("p", { class: "pos" }, "Passed the sandbox validator."),
        kvWide([["Code hash", h("span", { class: "mono break" }, r.code_hash)], ["Uploaded", fmtDateTime(r.created_at)]]),
      ),
      backtestPanel(bt, { title: "Backtest results", warning: r.warning, shortHistoryDays: shortDays }),
      typeof days === "number" && days < cfg.min_listing_history_days
        ? note(`Only ${Math.floor(days)} days of history: at least ${cfg.min_listing_history_days} days on every market are required to list.`, "bad")
        : null,
      panel(
        "Review",
        note(BACKTEST_WARNING, "warn"),
        h("p", { class: "small muted" }, s.status === "draft" || s.status === "review"
          ? "This version is now waiting for admin review (code, backtest, description). Listing requires identity verification."
          : "An admin must list this version before it replaces the live one."),
      ),
    );
  };

  mount(
    body,
    panel(
      "Upload a new version",
      field("Strategy", stratSel),
      resetWarn,
      modeBox,
      editorBox,
      h(
        "div",
        { class: "btns" },
        button("Validate & backtest", {
          kind: "primary",
          disabled: !cfg.features.creator_uploads,
          onClick: async () => {
            const s = selected();
            let payload: Record<string, unknown>;
            if (published && !(await confirmDialog({
              title: "Upload a new version?",
              message: "When an admin lists it, this version replaces the live one and resets your live track record to zero.",
              confirmLabel: "Upload",
              danger: true,
              requireText: "RESET",
            }))) return;
            if (mode === "python") {
              const errs = precheckPython(code.value);
              if (errs.length && !(await confirmDialog({ title: "Upload anyway?", message: `Quick checks found ${errs.length} issue(s). The sandbox will probably reject it.`, confirmLabel: "Upload anyway" }))) return;
              payload = { source: "python", code: code.value };
            } else {
              const errs = builder.errors();
              if (errs.length) return toast(`Fix the builder errors first (${errs.length}).`, "warn");
              payload = { source: "nocode", spec: spec ?? builder.spec() };
            }
            mount(resultBox, panel("Validating…", skeleton(4)));
            let r: CreatorVersion;
            try {
              r = await api.post<CreatorVersion>(`/creator/strategies/${encodeURIComponent(s.id)}/versions`, payload, { signal: ctx.signal, timeoutMs: 180_000 });
            } catch (err) {
              if (errCode(err) === "validation_failed") {
                // details.errors: validator strings, or no-code {path, message}; details.fields for schema errors
                const d = isRec(err) ? (err as { details?: Record<string, unknown> }).details : undefined;
                const raw = Array.isArray(d?.errors) ? (d?.errors as unknown[]) : Array.isArray(d?.fields) ? (d?.fields as unknown[]) : [];
                const errs = raw.length
                  ? raw.map((x) => (isRec(x) ? [Array.isArray(x.loc) ? x.loc.join(".") : x.path, x.message ?? x.msg].filter(Boolean).join(": ") : String(x)))
                  : [errMessage(err)];
                showResult(s, { errors: errs });
                return;
              }
              mount(resultBox);
              throw err;
            }
            if (!ctx.isCurrent()) return;
            showResult(s, r);
            void drawWarn();
          },
        }),
      ),
    ),
    resultBox,
  );
}

// ------------------------------------------------------------------------------------------ posts
async function postsTab(body: HTMLElement, ctx: PageContext, cfg: PublicConfig): Promise<void> {
  const e = cfg.economics;
  const listBox = h("div", { class: "stack" }, skeleton(4));
  const strategies = await loadMyStrategies(ctx).catch(() => [] as CreatorStrategy[]);
  if (!ctx.isCurrent()) return;
  const title = h("input", { type: "text", maxlength: 140, id: "po-title" });
  const bodyIn = h("textarea", { rows: 14, maxlength: 50000, id: "po-body", placeholder: "Markdown: # headings, **bold**, *italic*, lists, [links](https://…)" });
  const strat = h("select", { id: "po-strat" }, h("option", { value: "" }, "— none —"), ...strategies.map((s) => h("option", { value: s.id }, s.name)));
  const paid = checkbox("Paid post", { onChange: () => drawPrice() });
  const price = usdInput({ id: "po-price", value: String(e.post_min_price_micro / 1_000_000) });
  const priceBox = h("div", { class: "stack tight" });
  const preview = h("div", { class: "prose panel" });
  const drawPrice = (): void => {
    if (!paid.input.checked) {
      mount(priceBox, h("p", { class: "small muted" }, "Free posts are visible to everyone."));
      return;
    }
    const p = price.micro() ?? 0;
    const sp = postSplit(e, p);
    mount(
      priceBox,
      field(`Price (min ${fmtUsd(e.post_min_price_micro)})`, price.el),
      h("p", { class: ["small", p < e.post_min_price_micro ? "neg" : "muted"] }, p < e.post_min_price_micro ? `Minimum price is ${fmtUsd(e.post_min_price_micro)}.` : `Buyer pays ${fmtUsd(p)} → you receive ${fmtUsd(sp.creatorMicro)} (platform keeps ${fmtUsd(sp.platformMicro)}).`),
    );
  };
  price.el.addEventListener("input", drawPrice);
  drawPrice();
  const drawPreview = (): void => void mount(preview, renderMarkdown(bodyIn.value || "_Preview appears here._"));
  bodyIn.addEventListener("input", drawPreview);
  drawPreview();
  let key = newIdempotencyKey();

  mount(
    body,
    panel(
      "New post",
      field("Title", title),
      field("Related strategy", strat),
      h("div", { class: "grid-2" }, field("Body (Markdown)", bodyIn), h("div", { class: "stack tight" }, h("span", { class: "fl" }, "Preview"), preview)),
      paid.el,
      priceBox,
      note("No performance promises or personalised advice (Acceptable Use Policy).", "info"),
      h(
        "div",
        { class: "btns" },
        button("Publish post", {
          kind: "primary",
          onClick: async () => {
            const t = title.value.trim();
            const b = bodyIn.value.trim();
            if (t.length < 3) return toast("Title is too short.", "warn");
            if (b.length < 20) return toast("Body is too short.", "warn");
            let priceMicro = 0;
            if (paid.input.checked) {
              const p = price.micro();
              if (p === null || p < e.post_min_price_micro) return toast(`Minimum price is ${fmtUsd(e.post_min_price_micro)}.`, "warn");
              priceMicro = p;
            }
            await api.post("/creator/posts", { title: t, body: b, price_micro: priceMicro, strategy_id: strat.value || null }, { signal: ctx.signal, idempotencyKey: key });
            key = newIdempotencyKey();
            toast("Post published.", "good");
            title.value = "";
            bodyIn.value = "";
            drawPreview();
            void loadPosts();
          },
        }),
      ),
    ),
    panel(
      "My posts",
      h("p", { class: "small muted" }, "Published posts appear on the public ", h("a", { href: "#/posts" }, "Posts"), " page. Free posts show a 280-character preview there; paid posts show only the title until bought."),
      listBox,
    ),
  );

  // GET /v1/creator/posts → Page<CreatorPost> (own posts, newest first)
  const byId = new Map(strategies.map((s) => [s.id, s]));
  const bySlug = new Map(strategies.map((s) => [s.slug, s]));
  const loadPosts = async (): Promise<void> => {
    try {
      const res = await api.get<unknown>("/creator/posts?limit=100", { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      const ts = (p: CreatorPost): number => Date.parse(p.published_at ?? p.created_at ?? "") || 0;
      const posts = listOf<CreatorPost>(res).sort((a, b) => ts(b) - ts(a));
      const cols: Column<CreatorPost>[] = [
        { key: "t", label: "Title", value: (p) => h("a", { href: `#/posts/${encodeURIComponent(p.id)}`, class: "break" }, p.title), primary: true },
        {
          key: "s",
          label: "Strategy",
          value: (p) => {
            const st = (p.strategy_id ? byId.get(p.strategy_id) : undefined) ?? (p.strategy_slug ? bySlug.get(p.strategy_slug) : undefined);
            if (st) return st.status === "listed" ? h("a", { href: `#/s/${encodeURIComponent(st.slug)}` }, st.name) : st.name;
            return p.strategy_slug ? h("span", { class: "mono" }, p.strategy_slug) : h("span", { class: "muted" }, "—");
          },
          hideOnMobile: true,
        },
        { key: "p", label: "Price", value: (p) => (p.price_micro > 0 ? fmtUsd(p.price_micro) : badge("Free", "info")), align: "right", mono: true },
        {
          key: "n",
          label: "Sales",
          value: (p) => {
            if (!(p.price_micro > 0)) return h("span", { class: "muted" }, "n/a");
            const n = typeof p.sales === "number" ? p.sales : typeof p.sales_count === "number" ? p.sales_count : null;
            if (n === null) return "—";
            return typeof p.gross_sales_micro === "number" && p.gross_sales_micro > 0 ? `${n} · ${fmtUsd(p.gross_sales_micro)}` : String(n);
          },
          align: "right",
          mono: true,
        },
        { key: "d", label: "Published", value: (p) => (p.published_at ? fmtDate(p.published_at) : "Not published") },
      ];
      mount(listBox, posts.length ? table({ columns: cols, rows: posts, rowKey: (p) => p.id }) : emptyState("No posts yet", "Your published posts will be listed here."));
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(listBox, errorState(err, () => void loadPosts()));
    }
  };
  await loadPosts();
}

// ------------------------------------------------------------------------------------------ earnings
async function earningsTab(body: HTMLElement, ctx: PageContext, cfg: PublicConfig): Promise<void> {
  mount(body, skeleton(8));
  try {
    const [e, strategies] = await Promise.all([
      api.get<Earnings>("/creator/earnings", { signal: ctx.signal }),
      loadMyStrategies(ctx).catch(() => [] as CreatorStrategy[]),
    ]);
    if (!ctx.isCurrent()) return;
    mount(
      body,
      h(
        "div",
        { class: "stats" },
        stat("Earned (all time)", fmtUsd(e.total_earned_micro)),
        stat("Payable", fmtUsd(e.payable_micro)),
        stat("Payouts pending", fmtUsd(e.payouts_pending_micro)),
      ),
      panel("By strategy", earningsByStrategy(e, strategies)),
      panel("Request a payout", note("Payouts require verified identity (KYC) and are approved by two administrators, then sent as USDC on Hyperliquid.", "info"), payoutForm(ctx, cfg, "creator", e.payable_micro, () => void earningsTab(body, ctx, cfg))),
      panel(
        "Recent earnings",
        table({
          columns: [
            { key: "d", label: "Date", value: (r) => fmtDateTime(r.created_at), primary: true },
            { key: "k", label: "Source", value: (r) => r.kind.replace(/_/g, " ") },
            { key: "m", label: "Details", value: (r) => r.memo ?? "", hideOnMobile: true },
            { key: "a", label: "Amount", value: (r) => fmtUsd(r.amount_micro, { sign: true }), align: "right", mono: true },
          ],
          rows: e.recent,
          rowKey: (r) => r.tx_id,
          empty: "No earnings yet.",
        }),
      ),
    );
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    mount(body, errorState(err, () => void earningsTab(body, ctx, cfg)));
  }
}

/** Per-strategy earnings by source (EarningsOut.by_strategy + general_posts_micro / other_micro). Core `table`
 *  turns rows into cards below 640px. */
interface EarnRow {
  key: string;
  name: Child;
  subscribers: number | null;
  subscription: number | null;
  profitShare: number | null;
  builder: number | null;
  posts: number | null;
  total: number | null;
}

function earningsByStrategy(e: Earnings, strategies: CreatorStrategy[]): HTMLElement {
  const num = (...xs: unknown[]): number | null => {
    for (const x of xs) if (typeof x === "number" && Number.isFinite(x)) return x;
    return null;
  };
  const byId = new Map(strategies.map((s) => [s.id, s]));
  const rows: EarnRow[] = e.by_strategy.map((r) => {
    const st = r.strategy_id ? byId.get(r.strategy_id) : undefined;
    const parts = [num(r.subscription_share_micro, r.subscription_micro), num(r.profit_share_micro), num(r.builder_share_micro, r.builder_micro), num(r.posts_micro)];
    const sum = parts.every((x) => x === null) ? null : parts.reduce<number>((a, x) => a + (x ?? 0), 0);
    return {
      key: r.strategy_id ?? r.slug ?? "strategy",
      name: h("span", { class: "break" }, r.name || st?.name || r.slug || "Strategy"),
      subscribers: num(r.active_subscribers),
      subscription: parts[0]!,
      profitShare: parts[1]!,
      builder: parts[2]!,
      posts: parts[3]!,
      total: num(r.total_micro, r.earned_micro) ?? sum,
    };
  });
  const general = num(e.general_posts_micro);
  if (general) rows.push({ key: "general-posts", name: h("span", { class: "muted" }, "Posts without a strategy"), subscribers: null, subscription: null, profitShare: null, builder: null, posts: general, total: general });
  const other = num(e.other_micro);
  if (other) rows.push({ key: "other", name: h("span", { class: "muted" }, "Other (adjustments)"), subscribers: null, subscription: null, profitShare: null, builder: null, posts: null, total: other });
  const money = (v: number | null): Child => (v === null ? "—" : fmtUsd(v));
  const cols: Column<EarnRow>[] = [{ key: "s", label: "Strategy", value: (r) => r.name, primary: true }];
  if (rows.some((r) => r.subscribers !== null)) cols.push({ key: "a", label: "Subscribers", value: (r) => (r.subscribers === null ? "—" : String(r.subscribers)), align: "right", mono: true });
  const hasBreakdown = rows.some((r) => r.subscription !== null || r.profitShare !== null || r.builder !== null || r.posts !== null);
  if (hasBreakdown) {
    cols.push(
      { key: "sub", label: "Subscriptions", value: (r) => money(r.subscription), align: "right", mono: true },
      { key: "ps", label: "Profit share", value: (r) => money(r.profitShare), align: "right", mono: true },
      { key: "b", label: "Builder fees", value: (r) => money(r.builder), align: "right", mono: true },
      { key: "p", label: "Posts", value: (r) => money(r.posts), align: "right", mono: true },
    );
  }
  cols.push({ key: "t", label: "Total", value: (r) => (r.total === null ? "—" : h("b", null, fmtUsd(r.total))), align: "right", mono: true });
  rows.sort((a, b) => (b.total ?? -1) - (a.total ?? -1));
  return h(
    "div",
    { class: "stack tight earnings-by-strategy" },
    table({ columns: cols, rows, rowKey: (r) => r.key, empty: "No strategies yet." }),
    hasBreakdown ? h("p", { class: "small muted" }, "Your share after the platform's cut, all time. Builder fees are your part of the builder fee on subscribers' orders; profit share is charged only above each subscriber's high-water mark.") : null,
  );
}

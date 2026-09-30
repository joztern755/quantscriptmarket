// #/creator[/:tab] — Creator Studio: KYC status/CTA, my strategies, new strategy (with live fee preview),
// upload Python or build with the no-code builder → validate → backtest → submit for review,
// posts editor (free/paid ≥ min price), earnings.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, stat, kv, table, tabs, button, toast, confirmDialog, field, checkbox, badge, type Column, type Child } from "../core/ui.js";
import { api, publicConfig, newIdempotencyKey, type PublicConfig } from "../core/api.js";
import { LEGAL_SLUGS } from "../core/gate.js";
import { fmtUsd, fmtBps, fmtDate, fmtDateTime, fmtTenthsBp } from "../core/format.js";
import type { Backtest, NoCodeSpec } from "./_shared/types.js";
import { profitShare, subscriptionSplit, builderSplit, postSplit } from "./_shared/fees.js";
import { backtestPanel } from "./_shared/backtest.js";
import { noCodeBuilder } from "./_shared/nocode-ui.js";
import { precheckPython, PYTHON_TEMPLATE } from "./_shared/nocode.js";
import { renderMarkdown } from "./_shared/markdown.js";
import { ensurePageCss, listOf, isAbortError, errCode, errMessage, pageHead, panel, usdInput, pctToBps, bpsToPctInput, isRec, BACKTEST_WARNING } from "./_shared/util.js";

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

interface CreatorStrategy {
  id: string;
  slug: string;
  name: string;
  status: string;
  markets?: string[];
  price_monthly_micro?: number;
  profit_share_bps?: number;
  current_version?: { version: number; live_since?: string | null } | null;
  versions?: { id?: string; version: number; status?: string; published_at?: string | null }[];
  subscribers?: number | null;
  in_house?: boolean;
}

interface VersionResult {
  id?: string;
  version_id?: string;
  version?: number;
  status?: string; // validating | backtesting | ready | failed | review | published
  validation?: { ok: boolean; errors?: string[] } | null;
  errors?: string[];
  backtest?: Backtest | null;
}

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
  if (tab === "earnings") return earningsTab(body, ctx);
  return overviewTab(body, ctx);
}

// ------------------------------------------------------------------------------------------ KYC
function kycStatus(ctx: PageContext): string {
  const me = ctx.me as Record<string, unknown> | null;
  const v = me?.kyc_status ?? (isRec(me?.kyc) ? (me?.kyc as Record<string, unknown>).status : undefined);
  return typeof v === "string" ? v : "none";
}

function kycBanner(ctx: PageContext, cfg: PublicConfig): HTMLElement {
  const status = kycStatus(ctx);
  if (status === "verified" || status === "approved") return h("p", { class: "small" }, badge("Identity verified", "good"), " You can list strategies and receive payouts.");
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
                await api.post("/consents", {
                  consents: [{ doc: "creator_agreement", doc_version: cfg.legal_versions.creator_agreement ?? "draft", context: "creator", strategy_id: null, accepted_at: new Date().toISOString() }],
                });
                const res = await api.post<Record<string, unknown>>("/creator/kyc/session", {}, { signal: ctx.signal });
                const url = typeof res.url === "string" ? res.url : "";
                if (!/^https:\/\/[^\s]+$/i.test(url)) throw new Error("Verification could not be started. Please try again later.");
                window.location.assign(url);
              },
            }),
          ),
        ),
  );
}

// ------------------------------------------------------------------------------------------ overview
async function loadMyStrategies(ctx: PageContext): Promise<CreatorStrategy[]> {
  try {
    const res = await api.get<unknown>("/creator/strategies", { signal: ctx.signal });
    return listOf<CreatorStrategy>(res, "strategies");
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
      const [list, e] = await Promise.all([loadMyStrategies(ctx), api.get<Record<string, unknown>>("/creator/earnings", { signal: ctx.signal }).catch(() => null)]);
      if (!ctx.isCurrent()) return;
      mount(
        earn,
        stat("Strategies", String(list.length)),
        stat("Listed", String(list.filter((s) => s.status === "listed").length)),
        stat("Earned (all time)", e && typeof e.total_micro === "number" ? fmtUsd(e.total_micro) : "—"),
        stat("Payable", e && typeof e.payable_micro === "number" ? fmtUsd(e.payable_micro) : "—"),
      );
      const cols: Column<CreatorStrategy>[] = [
        { key: "name", label: "Strategy", value: (s) => (s.status === "listed" ? h("a", { href: `#/s/${encodeURIComponent(s.slug)}` }, s.name) : s.name), primary: true },
        { key: "status", label: "Status", value: (s) => badge(s.status, s.status === "listed" ? "good" : s.status === "review" ? "info" : s.status === "delisted" ? "bad" : "muted") },
        { key: "version", label: "Live version", value: (s) => (s.current_version ? `v${s.current_version.version}${s.current_version.live_since ? " · since " + fmtDate(s.current_version.live_since) : ""}` : "—") },
        { key: "markets", label: "Markets", value: (s) => (s.markets ?? []).join(", "), hideOnMobile: true },
        { key: "price", label: "Price / mo", value: (s) => (typeof s.price_monthly_micro === "number" ? fmtUsd(s.price_monthly_micro) : "—"), align: "right", mono: true },
        { key: "ps", label: "Profit share", value: (s) => (typeof s.profit_share_bps === "number" ? fmtBps(s.profit_share_bps) : "—"), align: "right", mono: true },
        { key: "subs", label: "Subscribers", value: (s) => (typeof s.subscribers === "number" ? String(s.subscribers) : "—"), align: "right", mono: true },
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
  const name = h("input", { type: "text", maxlength: 60, placeholder: "e.g. Silver trend 1D", id: "ns-name" });
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
      kv([
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
            if (!mk.length || mk.length > 5 || !mk.every((m) => COIN_RE.test(m))) return toast("Enter 1–5 valid markets.", "warn");
            if (p === null) return toast("Enter a valid monthly price.", "warn");
            if (bps === null || bps > cap) return toast(`Profit share must be 0–${fmtBps(cap)}.`, "warn");
            const res = await api.post<Record<string, unknown>>(
              "/creator/strategies",
              { name: n, description: desc.value.trim(), markets: mk, timeframe: tf.value, price_monthly_micro: p, profit_share_bps: bps },
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
  const editable = list.filter((s) => !s.in_house && s.status !== "delisted");
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
  const builder = noCodeBuilder(undefined, (sp) => { spec = sp; });

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
  const drawWarn = (): void => {
    const s = selected();
    const hasLive = !!s.current_version || (s.versions ?? []).some((v) => v.published_at);
    mount(resetWarn, hasLive ? note(h("span", null, h("b", null, "Publishing a new version resets your live track record. "), `v${s.current_version?.version ?? "?"}'s live ROI and $ made will no longer be shown as current; the new version starts from zero and is marked "Not live-proven" for 90 days.`), "bad") : null);
  };
  stratSel.addEventListener("change", () => { drawWarn(); mount(resultBox); });
  drawMode();
  drawWarn();

  let lastVersion: { strategyId: string; versionId: string } | null = null;

  const showResult = (s: CreatorStrategy, r: VersionResult): void => {
    const vid = String(r.version_id ?? r.id ?? "");
    const errs = r.validation?.errors ?? r.errors ?? [];
    const valid = r.validation ? r.validation.ok : errs.length === 0 && r.status !== "failed";
    lastVersion = vid ? { strategyId: s.id, versionId: vid } : null;
    const ready = valid && r.backtest && r.backtest.status !== "pending" && r.backtest.status !== "running" && !r.backtest.error;
    mount(
      resultBox,
      panel(
        `Validation${r.version ? ` — v${r.version}` : ""}`,
        valid ? h("p", { class: "pos" }, "Passed the sandbox validator.") : h("div", { class: "stack tight" }, h("p", { class: "neg" }, "The sandbox rejected this version:"), h("ul", { class: "small" }, ...errs.map((e) => h("li", { class: "break" }, e)))),
      ),
      valid ? backtestPanel(r.backtest ?? { status: "pending" }, "Backtest results") : null,
      ready && lastVersion
        ? panel(
            "Submit for review",
            note(BACKTEST_WARNING, "warn"),
            h("p", { class: "small muted" }, "An admin reviews the code, backtest and description. Listing requires identity verification. Submitting needs a fresh sign-in."),
            h(
              "div",
              { class: "btns" },
              button("Submit for review", {
                kind: "primary",
                onClick: async () => {
                  const hasLive = !!s.current_version;
                  const ok = await confirmDialog({
                    title: "Submit this version for review?",
                    message: hasLive ? "When approved and published, this version replaces the live one and resets your live track record to zero." : "An admin will review it before it can be listed.",
                    confirmLabel: "Submit",
                    danger: hasLive,
                    requireText: hasLive ? "RESET" : undefined,
                  });
                  if (!ok || !lastVersion) return;
                  await api.post(`/creator/strategies/${encodeURIComponent(lastVersion.strategyId)}/versions/${encodeURIComponent(lastVersion.versionId)}/submit`, {}, { signal: ctx.signal });
                  toast("Submitted for review.", "good");
                  ctx.navigate("/creator");
                },
              }),
            ),
          )
        : null,
    );
  };

  const poll = async (s: CreatorStrategy, vid: string): Promise<void> => {
    for (let i = 0; i < 90 && ctx.isCurrent(); i++) {
      await new Promise((r) => setTimeout(r, 5000));
      if (!ctx.isCurrent()) return;
      let r: VersionResult;
      try {
        r = await api.get<VersionResult>(`/creator/strategies/${encodeURIComponent(s.id)}/versions/${encodeURIComponent(vid)}`, { signal: ctx.signal });
      } catch (err) {
        if (isAbortError(err)) return;
        continue;
      }
      showResult(s, r);
      const bst = r.backtest?.status;
      if (r.status === "failed" || (r.backtest && bst !== "pending" && bst !== "running")) return;
    }
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
            let r: VersionResult;
            try {
              r = await api.post<VersionResult>(`/creator/strategies/${encodeURIComponent(s.id)}/versions`, payload, { signal: ctx.signal, timeoutMs: 120_000 });
            } catch (err) {
              if (errCode(err) === "validation_failed") {
                const d = isRec(err) ? (err as { details?: Record<string, unknown> }).details : undefined;
                const errs = Array.isArray(d?.errors) ? (d?.errors as unknown[]).map(String) : [errMessage(err)];
                showResult(s, { status: "failed", validation: { ok: false, errors: errs } });
                return;
              }
              mount(resultBox);
              throw err;
            }
            if (!ctx.isCurrent()) return;
            showResult(s, r);
            const vid = String(r.version_id ?? r.id ?? "");
            const pending = !r.backtest || r.backtest.status === "pending" || r.backtest.status === "running" || r.status === "validating" || r.status === "backtesting";
            if (vid && pending && r.status !== "failed" && r.validation?.ok !== false) void poll(s, vid);
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
  const excerpt = h("textarea", { rows: 2, maxlength: 400, id: "po-ex", placeholder: "Teaser shown before purchase (optional)" });
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
      field("Teaser", excerpt),
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
            await api.post("/creator/posts", { title: t, body: b, excerpt: excerpt.value.trim() || null, price_micro: priceMicro, strategy_id: strat.value || null }, { signal: ctx.signal, idempotencyKey: key });
            key = newIdempotencyKey();
            toast("Post published.", "good");
            title.value = "";
            bodyIn.value = "";
            excerpt.value = "";
            drawPreview();
            void loadList();
          },
        }),
      ),
    ),
    panel("My posts", listBox),
  );

  const loadList = async (): Promise<void> => {
    try {
      const res = await api.get<unknown>("/creator/posts", { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      const posts = listOf<{ id: string; title: string; price_micro: number; published_at?: string | null; sales?: number }>(res, "posts");
      mount(
        listBox,
        table({
          columns: [
            { key: "t", label: "Title", value: (p) => h("a", { href: `#/posts/${encodeURIComponent(p.id)}` }, p.title), primary: true },
            { key: "p", label: "Price", value: (p) => (p.price_micro > 0 ? fmtUsd(p.price_micro) : "Free"), align: "right", mono: true },
            { key: "s", label: "Sales", value: (p) => (typeof p.sales === "number" ? String(p.sales) : "—"), align: "right", mono: true },
            { key: "d", label: "Published", value: (p) => (p.published_at ? fmtDate(p.published_at) : "—") },
          ],
          rows: posts,
          rowKey: (p) => p.id,
          empty: "No posts yet.",
        }),
      );
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      if (errCode(err) === "not_found") mount(listBox, h("p", { class: "small muted" }, "Your published posts appear on the public Posts page."));
      else mount(listBox, errorState(err, () => void loadList()));
    }
  };
  await loadList();
}

// ------------------------------------------------------------------------------------------ earnings
async function earningsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  mount(body, skeleton(8));
  try {
    const e = await api.get<Record<string, unknown>>("/creator/earnings", { signal: ctx.signal });
    if (!ctx.isCurrent()) return;
    const labels: Record<string, string> = {
      total_micro: "Earned (all time)",
      earned_30d_micro: "Earned (30 days)",
      payable_micro: "Payable",
      pending_micro: "Pending",
      paid_out_micro: "Paid out",
      subscriptions_micro: "Subscriptions",
      profit_share_micro: "Profit share",
      builder_micro: "Builder-fee share",
      posts_micro: "Paid posts",
    };
    const bySource = isRec(e.by_source) ? e.by_source : {};
    const tiles: Child[] = [];
    for (const [k, l] of Object.entries(labels)) {
      const v = typeof e[k] === "number" ? e[k] : typeof bySource[k.replace(/_micro$/, "")] === "number" ? bySource[k.replace(/_micro$/, "")] : undefined;
      if (typeof v === "number") tiles.push(stat(l, fmtUsd(v)));
    }
    const hist = listOf<{ created_at: string; kind: string; amount_micro: number; memo?: string | null }>(e, "history", "entries", "ledger");
    mount(
      body,
      tiles.length ? h("div", { class: "stats" }, ...tiles) : null,
      note("Payouts require verified identity and are approved by two administrators, then sent as USDC on Hyperliquid.", "info"),
      panel(
        "Earnings history",
        table({
          columns: [
            { key: "d", label: "Date", value: (r) => fmtDateTime(r.created_at), primary: true },
            { key: "k", label: "Source", value: (r) => r.kind.replace(/_/g, " ") },
            { key: "m", label: "Details", value: (r) => r.memo ?? "", hideOnMobile: true },
            { key: "a", label: "Amount", value: (r) => fmtUsd(r.amount_micro, { sign: true }), align: "right", mono: true },
          ],
          rows: hist,
          empty: "No earnings yet.",
        }),
      ),
    );
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    mount(body, errorState(err, () => void earningsTab(body, ctx)));
  }
}

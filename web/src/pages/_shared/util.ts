// Page-level helpers shared by web/src/pages/*. Built only on the documented core API.

import { h, badge, loadCss, type Child } from "../../core/ui.js";
import { parseUsdToMicro } from "../../core/format.js";
import { renderInline } from "./markdown.js";
import type { StrategySummary } from "./types.js";

/** Days of live signals required before a strategy counts as live-proven (SPEC §10). */
export const LIVE_PROVEN_DAYS = 90;
/** k-anonymity: aggregate $ stats are only public with ≥ this many subscribers (SPEC §5.8). */
export const MIN_SUBSCRIBERS_FOR_STATS = 5;

export const BACKTEST_WARNING = "Backtest of a newly uploaded script can be fitted to history; not proven live yet.";
export const LOSS_WARNING = "You can lose all allocated funds. Leverage magnifies losses. Past performance does not predict future results.";
export const RISK_LINE =
  "Trading perpetual futures is high risk. Strategies can and do lose money, including all funds you allocate. Nothing here is investment advice.";

let cssLoaded = false;
/** Loads the pages' own stylesheet (copied by the build next to the compiled page modules). */
export function ensurePageCss(): void {
  if (cssLoaded) return;
  cssLoaded = true;
  try {
    loadCss(new URL("./pages.css", import.meta.url).href);
  } catch {
    /* non-fatal: pages still work with core styles */
  }
}

type Rec = Record<string, unknown>;
export const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);

/** Items of a backend list response: `Page<T>` = `{items, next_cursor}` or a plain array (e.g. /agents,
 *  /creator/strategies, /admin/flags, /public/showcase/{slug}); `key` picks a named array (e.g. PositionsOut.positions). */
export function listOf<T>(x: unknown, key = "items"): T[] {
  if (Array.isArray(x)) return x as T[];
  if (isRec(x) && Array.isArray(x[key])) return x[key] as T[];
  return [];
}

/** Live ROI in percent from StrategyStats.roi_bps (null when hidden). */
export function roiPct(roiBps: number | null | undefined): number | null {
  return typeof roiBps === "number" && Number.isFinite(roiBps) ? roiBps / 100 : null;
}

/** ms epoch from ISO string / seconds / ms. NaN if invalid. */
export function toMs(t: unknown): number {
  if (typeof t === "number") return t < 1e12 ? t * 1000 : t;
  if (typeof t === "string") {
    if (/^\d+$/.test(t)) return toMs(Number(t));
    return Date.parse(t);
  }
  return NaN;
}

/** Sandbox equity curves are `[[t_ms, equity], …]`. */
export function equityPoints(arr: unknown): { t: number; v: number }[] {
  if (!Array.isArray(arr)) return [];
  const out: { t: number; v: number }[] = [];
  for (const p of arr) {
    if (Array.isArray(p) && p.length >= 2) {
      const t = toMs(p[0]);
      const v = Number(p[1]);
      if (Number.isFinite(t) && Number.isFinite(v)) out.push({ t, v });
    }
  }
  return out.sort((a, b) => a.t - b.t);
}

/** Hyperliquid decimal strings → number, for DISPLAY only (never money math). */
export function hlNum(x: unknown): number {
  const n = typeof x === "number" ? x : typeof x === "string" ? Number(x) : NaN;
  return Number.isFinite(n) ? n : NaN;
}

export function isAddress(a: unknown): a is string {
  return typeof a === "string" && /^0x[0-9a-fA-F]{40}$/.test(a);
}

export function explorerAddressUrl(addr: string): string | null {
  return isAddress(addr) ? `https://app.hyperliquid.xyz/explorer/address/${addr.toLowerCase()}` : null;
}

export function isAbortError(err: unknown): boolean {
  return (err instanceof DOMException && err.name === "AbortError") || (isRec(err) && (err as Rec).name === "AbortError");
}

export function errCode(err: unknown): string {
  return isRec(err) && typeof (err as Rec).code === "string" ? ((err as Rec).code as string) : "";
}

export function errMessage(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  if (isRec(err) && typeof err.message === "string") return err.message;
  return "Something went wrong.";
}

export function isNotLiveProven(s: Pick<StrategySummary, "not_live_proven">): boolean {
  return s.not_live_proven !== false;
}

/** Status badges for a strategy card/header (SPEC §9, §12). */
export function strategyBadges(s: StrategySummary): HTMLElement {
  const wrap = h("span", { class: "row tight-row" });
  if (s.free_showcase) wrap.appendChild(badge("Free showcase", "info"));
  if (s.signal_state === "trades") wrap.appendChild(badge("Trades", "good"));
  else if (s.signal_state === "holds") wrap.appendChild(badge("Holds — no active signals", "muted"));
  if (isNotLiveProven(s)) wrap.appendChild(badge(`Not live-proven (< ${LIVE_PROVEN_DAYS} days)`, "warn"));
  if (typeof s.short_history_days === "number") wrap.appendChild(badge(`Short history (${s.short_history_days} days)`, "warn"));
  if (s.status && s.status !== "listed") wrap.appendChild(badge(s.status, s.status === "paused" ? "warn" : "muted"));
  return wrap;
}

export function marketChips(markets: string[] | undefined): HTMLElement {
  return h("span", { class: "row tight-row" }, ...(markets ?? []).map((m) => h("span", { class: "chip" }, m)));
}

/** Page header block. */
export function pageHead(eyebrow: string, title: string, sub?: Child, actions?: Child): HTMLElement {
  return h(
    "div",
    { class: "sec-head page-head" },
    h("div", { class: "stack tight" }, eyebrow ? h("div", { class: "eyebrow" }, eyebrow) : null, h("h1", { class: "page-title" }, title), sub ? h("p", { class: "muted" }, sub) : null),
    actions ? h("div", { class: "btns" }, actions) : null,
  );
}

export function panel(title: Child, ...children: Child[]): HTMLElement {
  return h("section", { class: "panel stack" }, title ? h("div", { class: "panel-head" }, typeof title === "string" ? h("h2", null, title) : title) : null, ...children);
}

/** Accessible progress bar (native <progress>, styled in pages.css). */
export function progressBar(value: number, max: number, label: string): HTMLElement {
  const v = Math.max(0, Math.min(max, Number.isFinite(value) ? value : 0));
  const pct = max > 0 ? Math.floor((v / max) * 100) : 0;
  return h(
    "div",
    { class: "progress-wrap" },
    h("div", { class: "row between small" }, h("span", null, label), h("span", { class: "mono" }, `${pct}%`)),
    h("progress", { max: String(max), value: String(v), "aria-label": label }),
  );
}

/** USD text input that parses to integer micro-USD (bigint → number; amounts here stay far below 2^53). */
export function usdInput(opts: { value?: string; placeholder?: string; id?: string; oninput?: () => void } = {}): {
  el: HTMLInputElement;
  micro(): number | null;
} {
  const el = h("input", {
    type: "text",
    inputmode: "decimal",
    autocomplete: "off",
    placeholder: opts.placeholder ?? "0.00",
    id: opts.id,
    value: opts.value ?? "",
    oninput: opts.oninput,
  });
  return {
    el,
    micro(): number | null {
      const raw = el.value.replace(/[$,\s]/g, "");
      if (!raw) return null;
      const m = parseUsdToMicro(raw);
      if (m === null) return null;
      if (m > BigInt(Number.MAX_SAFE_INTEGER)) return null;
      return Number(m);
    },
  };
}

/** Percent input (e.g. "12.5") → integer bps, or null. */
export function pctToBps(input: string): number | null {
  const s = input.trim().replace(/%$/, "");
  if (!/^\d{1,3}(\.\d{1,2})?$/.test(s)) return null;
  const [i, f = ""] = s.split(".");
  return Number(i) * 100 + Number((f + "00").slice(0, 2));
}

export function bpsToPctInput(bps: number): string {
  const i = Math.trunc(bps / 100);
  const f = bps % 100;
  return f ? `${i}.${String(f).padStart(2, "0").replace(/0$/, "")}` : String(i);
}

/** Replace the current hash's query without re-rendering (no hashchange fires for replaceState). */
export function replaceQuery(path: string, params: Record<string, string | null | undefined>): void {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
  const s = q.toString();
  try {
    history.replaceState(history.state, "", `#${path}${s ? "?" + s : ""}`);
  } catch {
    /* ignore */
  }
}

/** Run `fn` every `ms` while the page is current; returns stop(). */
export function every(ctx: { onCleanup(fn: () => void): void; isCurrent(): boolean }, ms: number, fn: () => void): () => void {
  const id = window.setInterval(() => {
    if (!ctx.isCurrent()) {
      window.clearInterval(id);
      return;
    }
    fn();
  }, ms);
  const stop = (): void => window.clearInterval(id);
  ctx.onCleanup(stop);
  return stop;
}

export function stars(rating: number): string {
  const r = Math.max(0, Math.min(5, Math.round(rating)));
  return "★".repeat(r) + "☆".repeat(5 - r);
}

/** Classify a coin into an asset group for market filters. */
export function assetGroup(coin: string): "crypto" | "tradfi" {
  return coin.includes(":") ? "tradfi" : "crypto";
}

/** Fee summary rows (from core gate.feeSummary) as a readable two-column list (core `.kv.fees`). */
export function feesList(rows: { label: string; value: string; note?: string }[]): HTMLDListElement {
  return h("dl", { class: "kv fees" }, rows.flatMap((f) => [h("dt", null, f.label), h("dd", null, h("b", null, f.value), f.note ? h("div", { class: "small muted" }, f.note) : null)]));
}

/** Short markdown (teasers) rendered inline: bold/italic/code/safe links only. */
export function inlineMd(tag: "p" | "span" | "div", text: string, cls?: string): HTMLElement {
  const el = h(tag, cls ? { class: cls } : null);
  renderInline(el, text.replace(/\s+/g, " "));
  return el;
}

/** Label/value list with room for sentence-length values (core `.kv.fees` layout, stacks on phones). */
export function kvWide(pairs: [string, Child][]): HTMLDListElement {
  return h("dl", { class: "kv fees" }, pairs.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)]));
}

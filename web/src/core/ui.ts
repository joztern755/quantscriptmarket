// Safe DOM builder + UI primitives. NO innerHTML anywhere: text goes into text nodes, attributes via
// setAttribute, styles via CSSOM (allowed under a strict CSP without 'unsafe-inline').

export type Child = Node | string | number | null | undefined | false | Child[];
type Handler = (ev: any) => unknown;
export interface Attrs {
  class?: string | null | false | (string | false | null | undefined)[];
  style?: Partial<Record<string, string>>;
  dataset?: Record<string, string>;
  [attr: string]: unknown;
}

const URL_ATTRS = new Set(["href", "src", "action", "formaction", "xlink:href", "poster", "srcset"]);
const SAFE_URL = /^(?:https?:|mailto:|tel:|otpauth:|#|\/|\.{0,2}\/|[^:]*$)/i;
const SVG_NS = "http://www.w3.org/2000/svg";

function safeUrl(v: string): string | null {
  const s = v.trim();
  if (/^data:image\/(png|gif|jpeg|webp|svg\+xml)[;,]/i.test(s)) return s;
  // Strip control chars/whitespace that browsers ignore when parsing schemes.
  const probe = s.replace(/[\u0000- \u007f-\u009f]/g, "");
  return SAFE_URL.test(probe) ? s : null;
}

function applyAttrs(el: Element, attrs: Attrs | null | undefined): void {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") {
      const cls = Array.isArray(v) ? v.filter(Boolean).join(" ") : String(v);
      if (cls) el.setAttribute("class", cls);
    } else if (k === "style" && typeof v === "object") {
      const st = (el as HTMLElement).style;
      for (const [p, val] of Object.entries(v as Record<string, string>)) {
        if (val === undefined || val === null) continue;
        if (p.startsWith("--")) st.setProperty(p, String(val));
        else st.setProperty(p.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()), String(val));
      }
    } else if (k === "dataset" && typeof v === "object") {
      for (const [dk, dv] of Object.entries(v as Record<string, string>)) (el as HTMLElement).dataset[dk] = String(dv);
    } else if (k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2).toLowerCase(), v as Handler);
    } else if (k.startsWith("on")) {
      continue; // never set string event-handler attributes
    } else if (k === "innerHTML" || k === "outerHTML" || k === "srcdoc") {
      continue; // forbidden
    } else if (URL_ATTRS.has(k.toLowerCase())) {
      const u = safeUrl(String(v));
      if (u !== null) el.setAttribute(k, u);
    } else if (v === true) {
      el.setAttribute(k, "");
    } else if (k === "value" && (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement)) {
      el.value = String(v);
    } else if (k === "checked" && el instanceof HTMLInputElement) {
      el.checked = Boolean(v);
    } else {
      el.setAttribute(k, String(v));
    }
  }
}

function appendChildren(el: Node, children: Child[]): void {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) appendChildren(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs?: Attrs | null, ...children: Child[]): HTMLElementTagNameMap[K];
export function h(tag: string, attrs?: Attrs | null, ...children: Child[]): HTMLElement;
export function h(tag: string, attrs?: Attrs | null, ...children: Child[]): HTMLElement {
  if (/^(script|iframe|object|embed|style|base|meta|link)$/i.test(tag)) throw new Error(`h(): <${tag}> not allowed`);
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

export function svg(tag: string, attrs?: Attrs | null, ...children: Child[]): SVGElement {
  if (/^(script|foreignObject)$/i.test(tag)) throw new Error(`svg(): <${tag}> not allowed`);
  const el = document.createElementNS(SVG_NS, tag) as SVGElement;
  applyAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

export function text(s: unknown): Text {
  return document.createTextNode(s === null || s === undefined ? "" : String(s));
}

export function frag(...children: Child[]): DocumentFragment {
  const f = document.createDocumentFragment();
  appendChildren(f, children);
  return f;
}

export function clear<T extends Node>(el: T): T {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function mount<T extends Node>(el: T, ...children: Child[]): T {
  clear(el);
  appendChildren(el, children);
  return el;
}

export function href(path: string): string {
  if (path.startsWith("#")) return path;
  return "#" + (path.startsWith("/") ? path : "/" + path);
}

const loadedCss = new Set<string>();
export function loadCss(url: string): void {
  const abs = new URL(url, document.baseURI);
  if (abs.origin !== location.origin) throw new Error("loadCss: same-origin only");
  if (loadedCss.has(abs.href)) return;
  loadedCss.add(abs.href);
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = abs.href;
  document.head.appendChild(link);
}

// ---------------------------------------------------------------- toasts
let toastHost: HTMLElement | null = null;
export function toast(message: string, kind: "info" | "good" | "bad" | "warn" = "info", ms = 4500): void {
  if (!toastHost || !toastHost.isConnected) {
    toastHost = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.appendChild(toastHost);
  }
  const t = h("div", { class: ["toast", kind] }, h("span", null, message), h("button", { class: "toast-x", type: "button", "aria-label": "Dismiss", onclick: () => t.remove() }, "×"));
  toastHost.appendChild(t);
  if (kind === "bad") t.setAttribute("role", "alert");
  window.setTimeout(() => t.remove(), kind === "bad" ? Math.max(ms, 8000) : ms);
}

// ---------------------------------------------------------------- dialogs
export interface ModalAction {
  label: string;
  kind?: "primary" | "danger" | "plain";
  onClick?: () => unknown | Promise<unknown>;
  close?: boolean; // default true
  disabled?: boolean;
  id?: string;
}
export interface ModalHandle {
  el: HTMLDialogElement;
  body: HTMLElement;
  actions: HTMLButtonElement[];
  close(): void;
  closed: Promise<void>;
}

export function modal(opts: { title: string; body: Child; actions?: ModalAction[]; dismissible?: boolean; wide?: boolean }): ModalHandle {
  const dismissible = opts.dismissible !== false;
  let resolveClosed!: () => void;
  const closed = new Promise<void>((r) => (resolveClosed = r));
  const titleId = "dlg-" + Math.random().toString(36).slice(2, 9);
  const body = h("div", { class: "dlg-body" }, opts.body);
  const buttons: HTMLButtonElement[] = [];
  const dlg = h("dialog", { class: ["dlg", opts.wide && "wide"], "aria-labelledby": titleId });
  const close = () => {
    if (dlg.open) dlg.close();
  };
  for (const a of opts.actions ?? []) {
    const b = button(a.label, {
      kind: a.kind === "plain" ? "plain" : a.kind,
      disabled: a.disabled,
      onClick: async () => {
        if (a.onClick) {
          const r = await a.onClick();
          if (r === false) return; // handler vetoed close
        }
        if (a.close !== false) close();
      },
    });
    if (a.id) b.id = a.id;
    buttons.push(b);
  }
  appendChildren(dlg, [
    h("div", { class: "dlg-head" },
      h("h2", { id: titleId }, opts.title),
      dismissible ? h("button", { class: "dlg-x", type: "button", "aria-label": "Close", onclick: close }, "×") : null),
    body,
    buttons.length ? h("div", { class: "dlg-actions btns" }, buttons) : null,
  ]);
  dlg.addEventListener("cancel", (e) => {
    if (!dismissible) e.preventDefault();
  });
  if (dismissible) {
    dlg.addEventListener("click", (e) => {
      if (e.target === dlg) close(); // backdrop click
    });
  }
  dlg.addEventListener("close", () => {
    dlg.remove();
    resolveClosed();
  });
  document.body.appendChild(dlg);
  dlg.showModal();
  return { el: dlg, body, actions: buttons, close, closed };
}

export function confirmDialog(opts: { title: string; message: Child; confirmLabel?: string; cancelLabel?: string; danger?: boolean; requireText?: string }): Promise<boolean> {
  return new Promise((resolve) => {
    let ok = false;
    const input = opts.requireText ? h("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": `Type ${opts.requireText} to confirm` }) : null;
    const m = modal({
      title: opts.title,
      body: [
        h("div", { class: "stack" }, typeof opts.message === "string" ? h("p", null, opts.message) : opts.message,
          input ? field(`Type "${opts.requireText}" to confirm`, input) : null),
      ],
      actions: [
        { label: opts.cancelLabel ?? "Cancel", kind: "plain" },
        { label: opts.confirmLabel ?? "Confirm", kind: opts.danger ? "danger" : "primary", disabled: Boolean(input), onClick: () => { ok = true; } },
      ],
    });
    if (input) {
      const confirmBtn = m.actions[1]!;
      input.addEventListener("input", () => (confirmBtn.disabled = input.value.trim() !== opts.requireText));
    }
    m.closed.then(() => resolve(ok));
  });
}

export function promptDialog(opts: { title: string; message?: Child; label: string; placeholder?: string; inputMode?: "numeric" | "text"; pattern?: RegExp; autocomplete?: string; submitLabel?: string; dismissible?: boolean }): Promise<string | null> {
  return new Promise((resolve) => {
    let value: string | null = null;
    const input = h("input", {
      type: "text", placeholder: opts.placeholder ?? "", inputmode: opts.inputMode ?? "text",
      autocomplete: opts.autocomplete ?? "off", spellcheck: "false", "aria-label": opts.label,
    });
    const err = h("div", { class: "status err", "aria-live": "polite" });
    const submit = () => {
      const v = input.value.trim();
      if (opts.pattern && !opts.pattern.test(v)) {
        err.textContent = "That doesn't look right. Check and try again.";
        input.focus();
        return false;
      }
      value = v;
      return true;
    };
    const form = h("form", { class: "stack", novalidate: true }, opts.message ? h("div", null, opts.message) : null, field(opts.label, input), err);
    const m = modal({
      title: opts.title,
      body: form,
      dismissible: opts.dismissible,
      actions: [
        { label: "Cancel", kind: "plain" },
        { label: opts.submitLabel ?? "Continue", kind: "primary", onClick: () => submit() },
      ],
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (submit()) m.close();
    });
    window.setTimeout(() => input.focus(), 30);
    m.closed.then(() => resolve(value));
  });
}

// ---------------------------------------------------------------- loading / states
export function spinner(label = "Loading…"): HTMLElement {
  return h("span", { class: "spin-wrap", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", { class: "visually-hidden" }, label));
}

export function skeleton(lines = 3): HTMLElement {
  const el = h("div", { class: "skeleton", "aria-busy": "true", "aria-label": "Loading" });
  for (let i = 0; i < lines; i++) el.appendChild(h("div", { class: "sk-line", style: { width: `${[92, 78, 85, 64, 88][i % 5]}%` } }));
  return el;
}

export function errorMessage(err: unknown): string {
  if (err && typeof err === "object") {
    const e = err as { code?: string; message?: string; status?: number };
    switch (e.code) {
      case "network_error": return "Can't reach aijalon.trade right now. Check your connection and try again.";
      case "timeout": return "The request timed out. Please try again.";
      case "rate_limited": return "Too many requests. Wait a moment and try again.";
      case "not_found": return "Not found.";
      case "forbidden": return "You don't have access to this.";
      case "kill_switch_active": return "Trading is paused by risk controls right now. Exits still run.";
      case "step_up_cancelled": return "Confirmation cancelled.";
    }
    if (typeof e.message === "string" && e.message && e.message.length < 300) return e.message;
  }
  return "Something went wrong. Please try again.";
}

export function errorState(err: unknown, retry?: () => void): HTMLElement {
  return h("div", { class: "state err", role: "alert" },
    h("p", null, errorMessage(err)),
    retry ? button("Try again", { onClick: retry }) : null);
}

export function emptyState(title: string, detail?: Child, action?: Child): HTMLElement {
  return h("div", { class: "state empty" }, h("h3", null, title), detail ? h("p", { class: "muted" }, detail) : null, action ?? null);
}

export async function loadingInto<T>(root: HTMLElement, promise: Promise<T> | (() => Promise<T>), render: (v: T) => Child, opts: { lines?: number } = {}): Promise<void> {
  mount(root, skeleton(opts.lines ?? 4));
  const run = typeof promise === "function" ? promise : () => promise;
  try {
    const v = await run();
    mount(root, render(v));
  } catch (err) {
    if ((err as { name?: string })?.name === "AbortError") return;
    mount(root, errorState(err, typeof promise === "function" ? () => void loadingInto(root, promise, render, opts) : undefined));
  }
}

// ---------------------------------------------------------------- controls
export function button(label: Child, opts: { kind?: "primary" | "danger" | "plain" | "ghost"; onClick?: (e: MouseEvent) => unknown | Promise<unknown>; type?: "button" | "submit"; disabled?: boolean; title?: string; small?: boolean } = {}): HTMLButtonElement {
  const b = h("button", {
    type: opts.type ?? "button",
    class: ["btn", opts.kind && opts.kind !== "plain" && opts.kind, opts.small && "sm"],
    disabled: opts.disabled,
    title: opts.title,
  }, label);
  if (opts.onClick) {
    const handler = opts.onClick;
    b.addEventListener("click", async (e) => {
      if (b.getAttribute("aria-busy") === "true") return;
      const r = handler(e);
      if (r && typeof (r as Promise<unknown>).then === "function") {
        const wasDisabled = b.disabled;
        b.disabled = true;
        b.setAttribute("aria-busy", "true");
        try {
          await r;
        } catch (err) {
          if ((err as { code?: string })?.code !== "step_up_cancelled") toast(errorMessage(err), "bad");
        } finally {
          b.removeAttribute("aria-busy");
          if (b.isConnected) b.disabled = wasDisabled;
        }
      }
    });
  }
  return b;
}

export function field(label: string, control: HTMLElement, hint?: Child): HTMLElement {
  const id = control.id || "f-" + Math.random().toString(36).slice(2, 9);
  control.id = id;
  return h("div", { class: "field" }, h("label", { for: id }, label), control, hint ? h("div", { class: "hint" }, hint) : null);
}

export function checkbox(label: Child, opts: { checked?: boolean; required?: boolean; onChange?: (checked: boolean) => void; name?: string } = {}): { el: HTMLLabelElement; input: HTMLInputElement } {
  const input = h("input", { type: "checkbox", name: opts.name, required: opts.required, checked: opts.checked });
  if (opts.onChange) input.addEventListener("change", () => opts.onChange!(input.checked));
  const el = h("label", { class: "check" }, input, h("span", null, label));
  return { el, input };
}

export function copyButton(value: string, label = "Copy"): HTMLButtonElement {
  const b = button(label, {
    small: true,
    onClick: async () => {
      try {
        await navigator.clipboard.writeText(value);
        toast("Copied", "good", 1800);
      } catch {
        toast("Copy failed — select and copy manually", "warn");
      }
    },
  });
  return b;
}

export function tabs(items: { key: string; label: string }[], active: string, onSelect: (key: string) => void): HTMLElement {
  const nav = h("div", { class: "seg tabs", role: "tablist" });
  for (const it of items) {
    nav.appendChild(h("button", {
      type: "button", role: "tab", "aria-selected": String(it.key === active), "aria-pressed": String(it.key === active),
      onclick: () => onSelect(it.key),
    }, it.label));
  }
  return nav;
}

// ---------------------------------------------------------------- data display
export function badge(label: string, tone: "good" | "bad" | "warn" | "info" | "muted" = "muted"): HTMLElement {
  return h("span", { class: ["pill", tone] }, label);
}

export function statusBadge(state: string | null | undefined): HTMLElement {
  switch (state) {
    case "trades": return badge("Trades", "good");
    case "holds": return badge("Holds — no active signals", "muted");
    case "not_live_proven": return badge("Not live-proven", "warn");
    default: return badge(state ? String(state) : "Unknown", "muted");
  }
}

export function subStatusBadge(status: string): HTMLElement {
  const map: Record<string, [string, "good" | "bad" | "warn" | "info" | "muted"]> = {
    pending: ["Pending", "info"],
    active: ["Active", "good"],
    past_due: ["Past due", "warn"],
    reduce_only: ["Exits only", "bad"],
    paused_user: ["Paused", "muted"],
    cancelled: ["Cancelled", "muted"],
  };
  const [label, tone] = map[status] ?? [status, "muted"];
  return badge(label, tone);
}

export function note(children: Child, kind: "warn" | "info" | "bad" = "warn"): HTMLElement {
  return h("div", { class: ["note", kind !== "warn" && kind], role: kind === "bad" ? "alert" : null }, children);
}

export function stat(label: string, value: Child, sub?: Child): HTMLElement {
  return h("div", { class: "stat" }, h("div", { class: "l" }, label), h("div", { class: "v" }, value), sub ? h("div", { class: "s" }, sub) : null);
}

export function kv(pairs: [string, Child][]): HTMLDListElement {
  const dl = h("dl", { class: "kv" });
  for (const [k, v] of pairs) dl.append(h("dt", null, k), h("dd", null, v));
  return dl;
}

export interface Column<T> {
  key: string;
  label: string;
  value: (row: T) => Child;
  align?: "left" | "right";
  mono?: boolean;
  primary?: boolean;
  hideOnMobile?: boolean;
}

export function table<T>(opts: { columns: Column<T>[]; rows: T[]; empty?: string; caption?: string; rowKey?: (r: T) => string; onRowClick?: (r: T) => void }): HTMLElement {
  if (!opts.rows.length) return emptyState(opts.empty ?? "Nothing here yet.");
  const thead = h("thead", null, h("tr", null, opts.columns.map((c) => h("th", { scope: "col", class: [c.align === "right" ? "r" : "l", c.hideOnMobile && "hide-m"] }, c.label))));
  const tbody = h("tbody");
  for (const row of opts.rows) {
    const tr = h("tr", { dataset: opts.rowKey ? { key: opts.rowKey(row) } : undefined, class: opts.onRowClick ? "clickable" : null });
    if (opts.onRowClick) {
      tr.tabIndex = 0;
      tr.addEventListener("click", (e) => {
        if ((e.target as Element).closest("a,button,input,select,label")) return;
        opts.onRowClick!(row);
      });
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter") opts.onRowClick!(row);
      });
    }
    for (const c of opts.columns) {
      tr.appendChild(h("td", {
        "data-label": c.label,
        class: [c.align === "right" ? "r" : "l", c.mono && "mono", c.primary && "primary", c.hideOnMobile && "hide-m"],
      }, c.value(row)));
    }
    tbody.appendChild(tr);
  }
  return h("div", { class: "rtable" }, h("table", null, opts.caption ? h("caption", { class: "visually-hidden" }, opts.caption) : null, thead, tbody));
}

// ---------------------------------------------------------------- charts
export type Tone = "accent" | "brass" | "ink" | "good" | "bad" | "muted";
export interface ChartSeries {
  name: string;
  points: { t: number; v: number }[];
  tone?: Tone;
}

const TONES: Tone[] = ["accent", "brass", "ink", "good", "bad", "muted"];

function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) {
    const d = Math.abs(min) || 1;
    min -= d * 0.5;
    max += d * 0.5;
  }
  const span = max - min;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out: number[] = [];
  for (let v = Math.floor(min / step) * step; v <= max + step * 0.5; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

export function lineChart(opts: {
  series: ChartSeries[];
  height?: number;
  yFormat?: (v: number) => string;
  xFormat?: (t: number) => string;
  baseline?: number;
  markers?: { t: number; label: string }[];
  ariaLabel: string;
}): HTMLElement {
  const H = opts.height ?? 240;
  const yFmt = opts.yFormat ?? ((v: number) => v.toLocaleString("en-US", { maximumFractionDigits: 2 }));
  const xFmt = opts.xFormat ?? ((t: number) => {
    const d = new Date(t);
    return `${d.getUTCDate()} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
  });
  const series = opts.series.map((s, i) => ({ ...s, tone: s.tone ?? TONES[i % TONES.length]!, points: s.points.filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v)).sort((a, b) => a.t - b.t) }));
  const wrap = h("figure", { class: "lchart" });
  const tip = h("div", { class: "lchart-tip", hidden: true, "aria-hidden": "true" });
  const legend = series.length > 1 ? h("figcaption", { class: "legend" }, series.map((s) => h("span", null, h("i", { class: `tone-${s.tone}` }), s.name))) : null;
  const all = series.flatMap((s) => s.points);
  if (!all.length) {
    wrap.append(emptyState("No data yet"));
    return wrap;
  }
  let W = 640;
  const pad = { l: 56, r: 12, t: 12, b: 26 };
  const draw = () => {
    const tMin = Math.min(...all.map((p) => p.t)), tMax = Math.max(...all.map((p) => p.t));
    let vMin = Math.min(...all.map((p) => p.v)), vMax = Math.max(...all.map((p) => p.v));
    if (opts.baseline !== undefined) {
      vMin = Math.min(vMin, opts.baseline);
      vMax = Math.max(vMax, opts.baseline);
    }
    const yt = niceTicks(vMin, vMax, 4);
    const y0 = yt[0] ?? vMin, y1 = yt[yt.length - 1] ?? vMax;
    const x = (t: number) => pad.l + (tMax === tMin ? (W - pad.l - pad.r) / 2 : ((t - tMin) / (tMax - tMin)) * (W - pad.l - pad.r));
    const y = (v: number) => pad.t + (y1 === y0 ? (H - pad.t - pad.b) / 2 : (1 - (v - y0) / (y1 - y0)) * (H - pad.t - pad.b));
    const g = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": opts.ariaLabel });
    for (const v of yt) {
      g.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v) }), svg("text", { x: pad.l - 6, y: y(v) + 3.5, "text-anchor": "end" }, yFmt(v)));
    }
    const nx = Math.max(2, Math.min(6, Math.floor((W - pad.l - pad.r) / 110)));
    for (let i = 0; i < nx; i++) {
      const t = tMin + ((tMax - tMin) * i) / (nx - 1);
      g.append(svg("text", { x: x(t), y: H - 8, "text-anchor": i === 0 ? "start" : i === nx - 1 ? "end" : "middle" }, xFmt(t)));
    }
    if (opts.baseline !== undefined) g.append(svg("line", { class: "baseline", x1: pad.l, x2: W - pad.r, y1: y(opts.baseline), y2: y(opts.baseline) }));
    for (const m of opts.markers ?? []) {
      if (m.t < tMin || m.t > tMax) continue;
      g.append(svg("line", { class: "marker", x1: x(m.t), x2: x(m.t), y1: pad.t, y2: H - pad.b }, svg("title", null, m.label)));
    }
    for (const s of series) {
      if (!s.points.length) continue;
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
      g.append(svg("path", { class: `line tone-${s.tone}`, d }));
    }
    const cross = svg("line", { class: "cross", x1: 0, x2: 0, y1: pad.t, y2: H - pad.b, visibility: "hidden" });
    const dots = series.map((s) => svg("circle", { class: `dot tone-${s.tone}`, r: 3.5, cx: 0, cy: 0, visibility: "hidden" }));
    g.append(cross, ...dots);
    const hover = (clientX: number) => {
      const rect = g.getBoundingClientRect();
      const px = ((clientX - rect.left) / rect.width) * W;
      const t = tMin + ((px - pad.l) / (W - pad.l - pad.r)) * (tMax - tMin);
      const rows: HTMLElement[] = [];
      let tx = 0;
      series.forEach((s, i) => {
        if (!s.points.length) return;
        let best = s.points[0]!;
        for (const p of s.points) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
        tx = x(best.t);
        dots[i]!.setAttribute("cx", String(x(best.t)));
        dots[i]!.setAttribute("cy", String(y(best.v)));
        dots[i]!.setAttribute("visibility", "visible");
        if (!rows.length) rows.push(h("div", { class: "tip-t" }, xFmt(best.t)));
        rows.push(h("div", null, h("i", { class: `tone-${s.tone}` }), `${series.length > 1 ? s.name + ": " : ""}${yFmt(best.v)}`));
      });
      cross.setAttribute("x1", String(tx));
      cross.setAttribute("x2", String(tx));
      cross.setAttribute("visibility", "visible");
      mount(tip, rows);
      tip.hidden = false;
      const left = (tx / W) * rect.width;
      tip.style.left = `${Math.min(Math.max(left, 60), rect.width - 60)}px`;
    };
    const leave = () => {
      tip.hidden = true;
      cross.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    };
    g.addEventListener("pointermove", (e) => hover((e as PointerEvent).clientX));
    g.addEventListener("pointerdown", (e) => hover((e as PointerEvent).clientX));
    g.addEventListener("pointerleave", leave);
    return g;
  };
  const holder = h("div", { class: "lchart-plot" });
  holder.append(draw(), tip);
  wrap.append(holder);
  if (legend) wrap.append(legend);
  if (typeof ResizeObserver !== "undefined") {
    let last = 0;
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0]?.contentRect.width ?? 0);
      if (!w || Math.abs(w - last) < 4) return;
      last = w;
      W = Math.max(260, w);
      const old = holder.querySelector("svg");
      const fresh = draw();
      if (old) holder.replaceChild(fresh, old);
    });
    ro.observe(holder);
  }
  return wrap;
}

export function sparkline(points: number[], tone: Tone = "accent"): SVGElement {
  const W = 100, H = 28;
  const vals = points.filter(Number.isFinite);
  const g = svg("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  if (vals.length < 2) return g;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const d = vals.map((v, i) => `${i ? "L" : "M"}${((i / (vals.length - 1)) * W).toFixed(1)},${(hi === lo ? H / 2 : H - 2 - ((v - lo) / (hi - lo)) * (H - 4)).toFixed(1)}`).join("");
  g.append(svg("path", { class: `line tone-${tone}`, d }));
  return g;
}

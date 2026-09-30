// Site-entry gate (shown before ANY content, SPEC §9) and the per-strategy subscribe gate.
// Acceptance is stored locally (versions + time) and POSTed to /v1/consents after sign-in.
// The gate re-appears whenever a document version in /v1/public/config changes.

import { api, ApiError, publicConfig, type ConsentDoc, type PublicConfig } from "./api.js";
import { currentUser } from "./auth.js";
import { fmtBps, fmtTenthsBp, fmtUsd } from "./format.js";
import { storage } from "./state.js";
import { button, checkbox, h, modal, note, type Child } from "./ui.js";

/** Consent doc → legal page slug (#/legal/<slug>, file dist/legal/<slug>.md). */
export const LEGAL_SLUGS: Record<ConsentDoc, string> = {
  terms: "terms",
  risk: "risk-disclosure",
  privacy: "privacy",
  waiver: "liability-waiver",
  jurisdiction: "jurisdiction",
  creator_agreement: "creator-agreement",
  subscription_ack: "subscription-ack",
};

export const SITE_DOCS = ["jurisdiction", "terms", "risk", "privacy", "waiver"] as const;
type SiteDoc = (typeof SITE_DOCS)[number];

interface LocalConsent {
  version: string;
  accepted_at: string;
  /** sha256 (hex) of the exact legal/<file>.md bytes shown when the box was ticked (sent as doc_text_sha256). */
  sha256?: string;
}
interface LocalState {
  site: Partial<Record<SiteDoc, LocalConsent>>;
  synced: Record<string, string>; // uid → fingerprint of synced versions
}

const KEY = "aij.consents.v1";
const TICKS_KEY = "aij.gate.ticks";

function load(): LocalState {
  const s = storage.get<LocalState>(KEY);
  return { site: s?.site ?? {}, synced: s?.synced ?? {} };
}
function save(s: LocalState): void {
  storage.set(KEY, s);
}

// Session-only memory (works even when localStorage is blocked).
let memAccepted: LocalState | null = null;

function state(): LocalState {
  const s = load();
  if (memAccepted && Object.keys(s.site).length === 0) return memAccepted;
  return s;
}

function fingerprint(cfg: PublicConfig): string {
  return SITE_DOCS.map((d) => `${d}:${cfg.legal_versions[d]}`).join("|");
}

export function siteGateAccepted(cfg: PublicConfig): boolean {
  const s = state();
  return SITE_DOCS.every((d) => s.site[d]?.version === cfg.legal_versions[d]);
}

function recordSite(cfg: PublicConfig, hashes: Partial<Record<SiteDoc, string>> = {}): void {
  const s = load();
  const now = new Date().toISOString();
  for (const d of SITE_DOCS) s.site[d] = { version: cfg.legal_versions[d], accepted_at: now, sha256: hashes[d] };
  save(s);
  memAccepted = { site: { ...s.site }, synced: { ...s.synced } };
}

function forgetSite(): void {
  const s = load();
  s.site = {};
  save(s);
  memAccepted = null;
}

// ---------------------------------------------------------------- legal text evidence (doc_text_sha256)

const hashCache = new Map<string, Promise<string>>();

function hex(buf: ArrayBuffer): string {
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * sha256 (hex) of the exact bytes of dist/legal/<file>.md — the file the legal page renders and the backend
 * hashes (config.legal_doc_hashes). Fetched as an ArrayBuffer (no text decoding) and hashed with WebCrypto.
 * When `expectVersion` is given, the file's `Version:` line must match it (a stale cached copy is refused).
 */
export function legalDocHash(doc: ConsentDoc, expectVersion?: string): Promise<string> {
  const slug = LEGAL_SLUGS[doc];
  const key = `${slug}@${expectVersion ?? ""}`;
  const hit = hashCache.get(key);
  if (hit) return hit;
  const p = (async () => {
    const res = await fetch(new URL(`legal/${slug}.md`, document.baseURI).href, { credentials: "omit", cache: "no-cache" });
    const ct = res.headers.get("content-type") ?? "";
    if (!res.ok || /text\/html/i.test(ct)) throw new ApiError(0, "legal_doc_unavailable", "Couldn't load the legal documents. Check your connection and try again.");
    const bytes = await res.arrayBuffer();
    if (expectVersion) {
      const head = new TextDecoder().decode(bytes.slice(0, 2048));
      const m = /^\**Version:?\**:?\s*([0-9A-Za-z._-]+)/m.exec(head);
      if (m && m[1] !== expectVersion) throw new ApiError(0, "legal_doc_stale", "The legal documents were just updated. Please reload the page.");
    }
    return hex(await crypto.subtle.digest("SHA-256", bytes));
  })();
  hashCache.set(key, p);
  p.catch(() => hashCache.delete(key));
  return p;
}

async function hashesFor<D extends ConsentDoc>(cfg: PublicConfig, docs: readonly D[]): Promise<Record<D, string>> {
  const v = cfg.legal_versions as Partial<Record<ConsentDoc, string>>;
  const out = {} as Record<D, string>;
  await Promise.all(docs.map(async (d) => (out[d] = await legalDocHash(d, v[d]))));
  return out;
}

let syncing: Promise<void> | null = null;

/** POSTs locally-recorded site-entry consents to /v1/consents once per user per version set. */
export function syncConsents(): Promise<void> {
  if (syncing) return syncing;
  syncing = (async () => {
    const u = currentUser();
    if (!u || !u.mfaSatisfied) return;
    const cfg = await publicConfig();
    if (cfg._fallback || !siteGateAccepted(cfg)) return;
    const s = state();
    const fp = fingerprint(cfg);
    if (s.synced[u.uid] === fp) return;
    const missing = SITE_DOCS.filter((d) => !s.site[d]!.sha256);
    const fresh = missing.length ? await hashesFor(cfg, missing) : ({} as Record<SiteDoc, string>);
    try {
      await api.post("/consents", {
        consents: SITE_DOCS.map((d) => ({
          doc: d,
          doc_version: s.site[d]!.version,
          context: "site_entry",
          strategy_id: null,
          accepted_at: s.site[d]!.accepted_at,
          doc_text_sha256: s.site[d]!.sha256 ?? fresh[d],
        })),
      });
    } catch (err) {
      // 409: a document changed since it was accepted here (version or text) → ask again.
      if (err instanceof ApiError && err.status === 409) forgetSite();
      throw err;
    }
    const s2 = load();
    s2.synced[u.uid] = fp;
    save(s2);
  })().finally(() => (syncing = null));
  return syncing;
}

function countryNames(codes: string[]): string[] {
  let dn: Intl.DisplayNames | null = null;
  try {
    dn = new Intl.DisplayNames(["en"], { type: "region" });
  } catch { /* old browser */ }
  return codes.map((c) => {
    const n = dn?.of(c);
    return n && n !== c ? `${n} (${c})` : c;
  });
}

function legalLink(doc: ConsentDoc, label: string, newTab = false): HTMLAnchorElement {
  return h("a", { href: `#/legal/${LEGAL_SLUGS[doc]}`, target: newTab ? "_blank" : null, rel: newTab ? "noopener" : null }, label);
}

function readTicks(): Record<string, boolean> {
  try {
    return JSON.parse(sessionStorage.getItem(TICKS_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}
function writeTicks(t: Record<string, boolean>): void {
  try {
    sessionStorage.setItem(TICKS_KEY, JSON.stringify(t));
  } catch { /* ignore */ }
}

/** The gate form (used full-page by the router and in a modal on 403 consent_required). */
export function gateForm(cfg: PublicConfig, onAccept: () => void): HTMLElement {
  const ticks = readTicks();
  const juris = countryNames(cfg.restricted_jurisdictions);
  const prior = state().site;
  const changed = SITE_DOCS.some((d) => prior[d] && prior[d]!.version !== cfg.legal_versions[d]);
  const items: { key: SiteDoc; label: Child }[] = [
    { key: "jurisdiction", label: ["I am not a resident of, and not located in, a restricted jurisdiction (listed above), and I am allowed to use this service where I live. ", legalLink("jurisdiction", "Eligibility attestation")] },
    { key: "terms", label: ["I have read and agree to the ", legalLink("terms", "Terms of Service"), "."] },
    { key: "risk", label: ["I have read and understand the ", legalLink("risk", "Risk Disclosure"), ": trading perpetual futures can lose all of the money I allocate, and past or backtested results do not predict future results."] },
    { key: "privacy", label: ["I have read the ", legalLink("privacy", "Privacy Policy"), " and consent to the processing it describes."] },
    { key: "waiver", label: ["I accept the ", legalLink("waiver", "Liability Waiver"), " (assumption of risk and release)."] },
  ];
  const boxes = items.map((it) => {
    const cb = checkbox(it.label, { checked: Boolean(ticks[it.key]), required: true, name: `gate-${it.key}` });
    cb.input.dataset.doc = it.key;
    return cb;
  });
  const status = h("p", { class: "status", "aria-live": "polite" });
  const go = button("Enter aijalon.trade", {
    kind: "primary",
    onClick: async () => {
      if (!boxes.every((b) => b.input.checked)) {
        status.className = "status err";
        status.textContent = "Tick every box to continue.";
        return;
      }
      // Evidence of which text was accepted; offline → recorded without it and hashed again at sync time.
      const hashes = cfg._fallback ? {} : await hashesFor(cfg, SITE_DOCS).catch(() => ({}));
      recordSite(cfg, hashes);
      try {
        sessionStorage.removeItem(TICKS_KEY);
      } catch { /* ignore */ }
      void syncConsents().catch(() => undefined);
      onAccept();
    },
  });
  go.id = "gate-accept";
  const sync = () => {
    const t: Record<string, boolean> = {};
    boxes.forEach((b) => (t[b.input.dataset.doc!] = b.input.checked));
    writeTicks(t);
    const all = boxes.every((b) => b.input.checked);
    go.disabled = !all;
    go.setAttribute("aria-disabled", String(!all));
    if (all) status.textContent = "";
  };
  boxes.forEach((b) => b.input.addEventListener("change", sync));
  sync();
  return h("form", { class: "gate-form stack", novalidate: true, onsubmit: (e: Event) => e.preventDefault() },
    changed ? note("Our legal documents were updated. Please review and accept them again.", "info") : null,
    cfg._fallback ? note("We couldn't reach our servers, so this list is the built-in copy. You may be asked again once we're back online.", "info") : null,
    h("div", { class: "juris" },
      h("div", { class: "eyebrow" }, "Restricted jurisdictions"),
      h("p", { class: "small" }, "This service is not available to residents of, or people located in:"),
      h("ul", { class: "juris-list" }, juris.map((n) => h("li", null, n))),
      h("p", { class: "small muted" }, "Using a VPN or other means to get around this restriction breaks our Terms.")),
    h("fieldset", { class: "stack tight" }, h("legend", { class: "visually-hidden" }, "Required acknowledgements"), boxes.map((b) => b.el)),
    status,
    h("div", { class: "btns" }, go),
    h("p", { class: "small faint" }, "Your choices are saved on this device and recorded with your account when you sign in. Documents: version ", cfg.legal_versions.terms, "."));
}

/** Full-page gate (router renders this instead of any page until accepted). */
export function renderSiteGate(root: HTMLElement, cfg: PublicConfig, onAccept: () => void): void {
  root.append(h("section", { class: "gate", "aria-labelledby": "gate-title" },
    h("div", { class: "gate-card panel" },
      h("div", { class: "eyebrow" }, "Before you enter"),
      h("h1", { id: "gate-title", class: "h2" }, "aijalon.trade is a strategy marketplace for Hyperliquid perpetuals"),
      h("p", { class: "muted" }, "Strategies trade real money in your own Hyperliquid account through an agent that can trade but can never withdraw. Losses can be total. Please confirm each item below."),
      gateForm(cfg, onAccept))));
}

/** Gate as a modal (used when the API answers 403 consent_required). Resolves true when accepted. */
export async function showGateModal(): Promise<boolean> {
  const cfg = await publicConfig(true);
  let ok = false;
  const m = modal({ title: "Please review our updated terms", body: gateForm(cfg, () => { ok = true; m.close(); }), dismissible: true, wide: true });
  await m.closed;
  if (ok) await syncConsents().catch(() => undefined);
  return ok;
}

// ---------------------------------------------------------------- fees + subscribe gate

export function feeSummary(cfg: PublicConfig, s: { price_monthly_micro: number; profit_share_bps: number }): { label: string; value: string; note?: string }[] {
  const e = cfg.economics;
  const creatorPs = Math.min(Math.max(0, s.profit_share_bps), e.profit_share_creator_cap_bps);
  const onTop = e.platform_profit_share_mode === "on_top";
  const totalPs = onTop ? creatorPs + e.platform_profit_share_bps : Math.max(creatorPs, e.platform_profit_share_bps);
  return [
    { label: "Monthly subscription", value: s.price_monthly_micro > 0 ? `${fmtUsd(s.price_monthly_micro)} / month` : "Free", note: s.price_monthly_micro > 0 ? "Prepaid from your fee balance at start and each renewal." : undefined },
    {
      label: "Profit share",
      value: fmtBps(totalPs),
      note: onTop
        ? `${fmtBps(creatorPs)} to the strategy creator + ${fmtBps(e.platform_profit_share_bps)} platform, on net realized profit above your high-water mark only. Charged daily from your fee balance.`
        : `${fmtBps(creatorPs)} of net realized profit above your high-water mark (includes the ${fmtBps(e.platform_profit_share_bps)} platform share).`,
    },
    { label: "Builder fee", value: `${fmtTenthsBp(e.builder_fee_tenths_bp)} of traded notional`, note: "Collected on-chain by Hyperliquid on every order we place for you (on top of Hyperliquid's own trading fees)." },
    { label: "If your fee balance runs out", value: "Exits only", note: `After ${e.past_due_grace_hours} h past due, no new positions are opened; exits still run.` },
  ];
}

export interface SubscribeGateStrategy {
  id: string;
  slug: string;
  name: string;
  price_monthly_micro: number;
  profit_share_bps: number;
  markets: string[];
  risk_ack_text?: string | null;
}

/**
 * Subscribe gate: strategy-specific acknowledgement + fee summary + Terms/Risk/Waiver again.
 * On accept POSTs the consents (context "subscribe") and resolves true. Dismiss → false.
 */
export async function subscribeGate(opts: { strategy: SubscribeGateStrategy; allocationMicro?: number }): Promise<boolean> {
  const cfg = await publicConfig();
  const s = opts.strategy;
  const ackText = s.risk_ack_text?.trim() ||
    `I understand that "${s.name}" trades ${s.markets.join(", ")} perpetuals with leverage in my own Hyperliquid account, that it can lose some or all of my allocation, that its backtest is not a promise of future results, and that a new script version resets its live track record.`;
  const acks = [
    checkbox(ackText, { required: true }),
    checkbox(["I have read the ", legalLink("subscription_ack", "Subscription Acknowledgement", true), " and agree to the fees above and to the ", legalLink("terms", "Terms of Service", true), "."], { required: true }),
    checkbox(["I have read the ", legalLink("risk", "Risk Disclosure", true), " and accept the ", legalLink("waiver", "Liability Waiver", true), " for this subscription."], { required: true }),
  ];
  const fees = feeSummary(cfg, s);
  const example = opts.allocationMicro && opts.allocationMicro > 0
    ? h("p", { class: "small muted" }, `Example: with ${fmtUsd(opts.allocationMicro)} allocated at 1× and one full round trip, the builder fee is about ${fmtUsd(Math.floor((opts.allocationMicro * 2 * cfg.economics.builder_fee_tenths_bp) / 100_000))}.`)
    : null;
  let accepted = false;
  const status = h("p", { class: "status", "aria-live": "polite" });
  const m = modal({
    title: `Subscribe to ${s.name}`,
    wide: true,
    body: h("div", { class: "stack" },
      h("h3", null, "Fees"),
      h("dl", { class: "kv fees" }, fees.flatMap((f) => [h("dt", null, f.label), h("dd", null, h("b", null, f.value), f.note ? h("div", { class: "small muted" }, f.note) : null)])),
      example,
      h("h3", null, "Acknowledgements"),
      h("div", { class: "stack tight" }, acks.map((a) => a.el)),
      status),
    actions: [
      { label: "Cancel", kind: "plain" },
      {
        label: "I agree — continue",
        kind: "primary",
        id: "subgate-accept",
        onClick: async () => {
          if (!acks.every((a) => a.input.checked)) {
            status.className = "status err";
            status.textContent = "Tick every box to continue.";
            return false;
          }
          const now = new Date().toISOString();
          const base = { context: "subscribe", strategy_id: s.id, accepted_at: now };
          const v = cfg.legal_versions;
          try {
            if (!v.subscription_ack) throw new ApiError(0, "config_unavailable", "Couldn't load the current acknowledgement. Try again in a moment.");
            const docs = ["subscription_ack", "terms", "risk", "waiver"] as const;
            const hx = await hashesFor(cfg, docs);
            await api.post("/consents", {
              consents: docs.map((d) => ({ doc: d, doc_version: (v as Partial<Record<ConsentDoc, string>>)[d], doc_text_sha256: hx[d], ...base })),
            });
          } catch (err) {
            status.className = "status err";
            status.textContent = (err as Error)?.message || "Couldn't record your acceptance. Try again.";
            return false;
          }
          accepted = true;
          return true;
        },
      },
    ],
  });
  const acceptBtn = m.actions[1]!;
  const sync = () => (acceptBtn.disabled = !acks.every((a) => a.input.checked));
  acks.forEach((a) => a.input.addEventListener("change", sync));
  sync();
  await m.closed;
  return accepted;
}

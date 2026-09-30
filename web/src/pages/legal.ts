// #/legal/:doc — renders /legal/<doc>.md with the SAFE markdown renderer (DOM nodes only, no innerHTML).
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState } from "../core/ui.js";
import { LEGAL_SLUGS } from "../core/gate.js";
import { renderMarkdown } from "./_shared/markdown.js";
import { ensurePageCss, isAbortError } from "./_shared/util.js";

export const title = "Legal";

const TITLES: Record<string, string> = {
  terms: "Terms of Service",
  "risk-disclosure": "Risk Disclosure",
  privacy: "Privacy Notice",
  waiver: "Liability Waiver",
  "restricted-jurisdictions": "Restricted Jurisdictions",
  "creator-agreement": "Creator Agreement",
  "acceptable-use": "Acceptable Use Policy",
};

/** File name on the static host for a slug (the waiver page is published as liability-waiver.md). */
const FILES: Record<string, string[]> = {
  waiver: ["waiver", "liability-waiver"],
};

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const doc = (ctx.params.doc ?? "").toLowerCase();
  const allowed = new Set<string>([...Object.values(LEGAL_SLUGS), ...Object.keys(TITLES)]);
  const nav = h(
    "nav",
    { class: "row small", "aria-label": "Legal documents" },
    ...Object.entries(TITLES).map(([slug, t]) => h("a", { href: `#/legal/${slug}`, "aria-current": slug === doc ? "page" : null }, t)),
  );
  if (!/^[a-z0-9-]{1,40}$/.test(doc) || !allowed.has(doc)) {
    mount(root, emptyState("Document not found", "Choose one of the documents below.", nav));
    return;
  }
  const docTitle = TITLES[doc] ?? doc;
  ctx.setTitle(docTitle);
  const body = h("article", { class: "prose" }, skeleton(8));
  mount(
    root,
    h(
      "div",
      { class: "stack" },
      h("div", { class: "stack tight" }, h("div", { class: "eyebrow" }, "Legal"), h("h1", { class: "page-title" }, docTitle)),
      nav,
      body,
    ),
  );

  const load = async (): Promise<void> => {
    mount(body, skeleton(8));
    try {
      const text = await fetchDoc(doc, ctx.signal);
      if (!ctx.isCurrent()) return;
      mount(body, renderMarkdown(text));
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(body, errorState(err, () => void load()));
    }
  };
  await load();
}

async function fetchDoc(doc: string, signal: AbortSignal): Promise<string> {
  let lastErr: unknown = null;
  for (const name of FILES[doc] ?? [doc]) {
    const res = await fetch(`/legal/${name}.md`, { signal, credentials: "omit", cache: "no-cache" });
    if (res.ok) {
      const ct = res.headers.get("content-type") ?? "";
      // SPA hosts often rewrite unknown paths to index.html — never render that as a document.
      if (/text\/html/i.test(ct)) {
        lastErr = new Error("Document not available.");
        continue;
      }
      return await res.text();
    }
    lastErr = new Error(res.status === 404 ? "Document not available." : `Could not load the document (HTTP ${res.status}).`);
  }
  throw lastErr ?? new Error("Document not available.");
}

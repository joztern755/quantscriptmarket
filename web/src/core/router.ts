// Hash router: "#/s/silver?tab=x" → lazy import("../pages/strategy.js").render(root, ctx).
// Enforces (in order): site-entry gate (all routes but legal) → auth + MFA → admin role (UI only).

import { publicConfig } from "./api.js";
import { authReady, currentUser, type SessionUser } from "./auth.js";
import { renderSiteGate, siteGateAccepted } from "./gate.js";
import { getMe, peekMe, type Me } from "./state.js";
import { clear, emptyState, errorState, h, note } from "./ui.js";

export type PageName = "home" | "market" | "strategy" | "subscribe" | "dashboard" | "leaderboard" | "posts" | "referrals" | "alerts" | "creator" | "admin" | "legal" | "signin";
export type Access = "public" | "user" | "admin";

export interface PageContext {
  name: PageName;
  path: string;
  params: Record<string, string>;
  query: URLSearchParams;
  user: SessionUser | null;
  me: Me | null;
  signal: AbortSignal;
  navigate(to: string, opts?: { replace?: boolean }): void;
  setTitle(title: string): void;
  onCleanup(fn: () => void): void;
  isCurrent(): boolean;
}

export interface PageModule {
  title?: string;
  render(root: HTMLElement, ctx: PageContext): void | (() => void) | Promise<void | (() => void)>;
}

interface RouteDef {
  pattern: string;
  name: PageName;
  access: Access;
  gate: boolean;
  title: string;
}

export const ROUTES: RouteDef[] = [
  { pattern: "/", name: "home", access: "public", gate: true, title: "Strategy marketplace on Hyperliquid" },
  { pattern: "/market", name: "market", access: "public", gate: true, title: "Marketplace" },
  { pattern: "/s/:slug", name: "strategy", access: "public", gate: true, title: "Strategy" },
  { pattern: "/subscribe/:slug", name: "subscribe", access: "user", gate: true, title: "Subscribe" },
  { pattern: "/dashboard", name: "dashboard", access: "user", gate: true, title: "Dashboard" },
  { pattern: "/dashboard/:tab", name: "dashboard", access: "user", gate: true, title: "Dashboard" },
  { pattern: "/leaderboard", name: "leaderboard", access: "public", gate: true, title: "Leaderboard" },
  { pattern: "/posts", name: "posts", access: "public", gate: true, title: "Posts" },
  { pattern: "/posts/:id", name: "posts", access: "public", gate: true, title: "Post" },
  { pattern: "/referrals", name: "referrals", access: "user", gate: true, title: "Referrals" },
  { pattern: "/alerts", name: "alerts", access: "user", gate: true, title: "Alerts" },
  { pattern: "/creator", name: "creator", access: "user", gate: true, title: "Creator Studio" },
  { pattern: "/creator/:tab", name: "creator", access: "user", gate: true, title: "Creator Studio" },
  { pattern: "/admin", name: "admin", access: "admin", gate: true, title: "Admin" },
  { pattern: "/admin/:tab", name: "admin", access: "admin", gate: true, title: "Admin" },
  { pattern: "/legal/:doc", name: "legal", access: "public", gate: false, title: "Legal" },
  { pattern: "/signin", name: "signin", access: "public", gate: true, title: "Sign in" },
];

const SITE = "aijalon.trade";
let mainEl: HTMLElement | null = null;
let current: { ctrl: AbortController; cleanups: (() => void)[]; token: number } | null = null;
let token = 0;
let lastRouteHash = "#/";
const routeListeners = new Set<(name: PageName | null, path: string) => void>();

export function onRouteChange(cb: (name: PageName | null, path: string) => void): () => void {
  routeListeners.add(cb);
  return () => routeListeners.delete(cb);
}

function matchRoute(path: string): { route: RouteDef; params: Record<string, string> } | null {
  const segs = path.split("/").filter(Boolean);
  for (const route of ROUTES) {
    const ps = route.pattern.split("/").filter(Boolean);
    if (ps.length !== segs.length) continue;
    const params: Record<string, string> = {};
    let ok = true;
    for (let i = 0; i < ps.length; i++) {
      const p = ps[i]!;
      const s = segs[i]!;
      if (p.startsWith(":")) {
        let v: string;
        try {
          v = decodeURIComponent(s);
        } catch {
          ok = false;
          break;
        }
        if (v.length > 128) {
          ok = false;
          break;
        }
        params[p.slice(1)] = v;
      } else if (p !== s) {
        ok = false;
        break;
      }
    }
    if (ok) return { route, params };
  }
  return null;
}

export function currentPath(): { path: string; query: URLSearchParams } {
  const raw = location.hash.startsWith("#/") ? location.hash.slice(1) : "/";
  const qi = raw.indexOf("?");
  const path = (qi >= 0 ? raw.slice(0, qi) : raw).replace(/\/+$/, "") || "/";
  return { path, query: new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : "") };
}

export function navigate(to: string, opts: { replace?: boolean } = {}): void {
  let target = to.startsWith("#") ? to : "#" + (to.startsWith("/") ? to : "/" + to);
  if (!target.startsWith("#/")) target = "#/";
  if (opts.replace) {
    history.replaceState(history.state, "", target);
    void render();
  } else if (location.hash === target) {
    void render();
  } else {
    location.hash = target;
  }
}

function setTitle(t: string): void {
  document.title = t ? `${t} · ${SITE}` : SITE;
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T | "timeout"> {
  return Promise.race([p, new Promise<"timeout">((r) => window.setTimeout(() => r("timeout"), ms))]);
}

function teardown(): void {
  if (!current) return;
  current.ctrl.abort();
  for (const fn of current.cleanups) {
    try {
      fn();
    } catch { /* ignore */ }
  }
  current = null;
  // Close any dialogs a page left open.
  document.querySelectorAll("dialog[open]").forEach((d) => (d as HTMLDialogElement).close());
}

export async function render(): Promise<void> {
  if (!mainEl) return;
  const main = mainEl;
  // Non-route fragments (e.g. the skip link "#main"): focus the target, restore the route hash.
  if (location.hash && !location.hash.startsWith("#/")) {
    const id = location.hash.slice(1);
    history.replaceState(history.state, "", lastRouteHash);
    const el = id ? document.getElementById(id) : null;
    if (el) {
      if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
      el.focus();
      return;
    }
  }
  const my = ++token;
  const { path, query } = currentPath();
  lastRouteHash = location.hash.startsWith("#/") ? location.hash : "#/";
  teardown();
  const m = matchRoute(path);
  document.body.classList.remove("gated");
  routeListeners.forEach((cb) => cb(m?.route.name ?? null, path));

  const show = (...nodes: Node[]) => {
    if (my !== token) return false;
    clear(main);
    main.append(...nodes);
    return true;
  };

  if (!m) {
    setTitle("Not found");
    show(h("div", { class: "page" }, emptyState("Page not found", "The link may be broken or the page may have moved.", h("a", { class: "btn", href: "#/" }, "Go home"))));
    return;
  }
  const { route, params } = m;

  // 1. Site-entry gate
  const cfg = await publicConfig();
  if (my !== token) return;
  const gateOk = siteGateAccepted(cfg);
  if (route.gate && !gateOk) {
    setTitle("Welcome");
    document.body.classList.add("gated");
    routeListeners.forEach((cb) => cb(null, path));
    const root = h("div", { class: "page" });
    if (show(root)) renderSiteGate(root, cfg, () => void render());
    return;
  }

  // 2. Auth + MFA
  let user: SessionUser | null = null;
  if (route.access !== "public") {
    const r = await withTimeout(authReady(), 10000);
    if (my !== token) return;
    user = currentUser();
    if (r === "timeout" || !user || !user.mfaSatisfied) {
      navigate(`/signin?next=${encodeURIComponent(path + (query.toString() ? "?" + query.toString() : ""))}`, { replace: true });
      return;
    }
  } else {
    user = currentUser();
  }
  let me: Me | null = user?.mfaSatisfied ? peekMe() : null;
  if (route.access === "admin" || (user?.mfaSatisfied && !me)) {
    me = await getMe().catch(() => null);
    if (my !== token) return;
  }
  if (route.access === "admin" && me?.role !== "admin") {
    setTitle("Not allowed");
    show(h("div", { class: "page" }, emptyState("Admins only", "Your account doesn't have access to this area.", h("a", { class: "btn", href: "#/" }, "Go home"))));
    return;
  }

  // 3. Page module
  const root = h("div", { class: "page", dataset: { page: route.name } });
  if (!gateOk && !route.gate) {
    root.append(note(["You haven't entered the site yet. ", h("a", { href: "#/" }, "Back to the entry page"), " when you're done reading."], "info"));
  }
  const pageRoot = h("div");
  root.append(pageRoot);
  show(root);
  pageRoot.append(h("div", { class: "page-loading" }, h("span", { class: "spinner", "aria-hidden": "true" })));
  let mod: PageModule;
  try {
    mod = (await import(`../pages/${route.name}.js`)) as PageModule;
  } catch {
    if (my !== token) return;
    setTitle(route.title);
    clear(pageRoot).append(errorState({ message: "This page couldn't be loaded. Check your connection and try again." }, () => void render()));
    return;
  }
  if (my !== token) return;
  const ctrl = new AbortController();
  const state = { ctrl, cleanups: [] as (() => void)[], token: my };
  current = state;
  setTitle(mod.title ?? route.title);
  const ctx: PageContext = {
    name: route.name,
    path,
    params,
    query,
    user,
    me,
    signal: ctrl.signal,
    navigate,
    setTitle: (t) => {
      if (my === token) setTitle(t);
    },
    onCleanup: (fn) => state.cleanups.push(fn),
    isCurrent: () => my === token,
  };
  clear(pageRoot);
  try {
    const ret = await mod.render(pageRoot, ctx);
    if (typeof ret === "function") state.cleanups.push(ret);
  } catch (err) {
    if (my !== token || (err as { name?: string })?.name === "AbortError") return;
    clear(pageRoot).append(errorState(err, () => void render()));
  }
}

export function startRouter(main: HTMLElement): void {
  mainEl = main;
  window.addEventListener("hashchange", () => {
    void render();
    window.scrollTo({ top: 0 });
  });
  void render();
}

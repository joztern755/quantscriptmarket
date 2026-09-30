// Boot: theme → static config → API hooks → shell → auth (non-blocking) → router.

import { api, ApiError, setApiHooks } from "./core/api.js";
import { currentUser, getIdToken, initAuth, onAuthChange, signOut, stepUp, takeRedirectNext, type SessionUser } from "./core/auth.js";
import { loadAppConfig } from "./core/config.js";
import { showGateModal, syncConsents } from "./core/gate.js";
import { currentPath, navigate, onRouteChange, render, startRouter, type PageName } from "./core/router.js";
import { clearMe, getMe, onMeChange, setMeLoader, storage, type Me } from "./core/state.js";
import { effectiveTheme, initTheme, onThemeChange, toggleTheme } from "./core/theme.js";
import { clear, h, mount, svg } from "./core/ui.js";

// ---------------------------------------------------------------- referral (first touch, 30 days)
const REF_KEY = "aij.ref";
const REF_BOUND_KEY = "aij.ref.bound";
const REF_RE = /^[A-Za-z0-9_-]{3,32}$/;

function captureReferral(): void {
  let code: string | null = null;
  const url = new URL(location.href);
  if (url.searchParams.has("ref")) {
    code = url.searchParams.get("ref");
    url.searchParams.delete("ref");
    history.replaceState(history.state, "", url.pathname + (url.searchParams.toString() ? "?" + url.searchParams : "") + url.hash);
  }
  const { path, query } = currentPath();
  if (!code && query.has("ref")) {
    code = query.get("ref");
    query.delete("ref");
    history.replaceState(history.state, "", `${location.pathname}${location.search}#${path}${query.toString() ? "?" + query : ""}`);
  }
  if (!code || !REF_RE.test(code)) return;
  const existing = storage.get<{ code: string; ts: number }>(REF_KEY);
  if (existing && Date.now() - existing.ts < 30 * 86400_000) return; // first touch wins
  storage.set(REF_KEY, { code, ts: Date.now() });
}

async function bindReferral(u: SessionUser): Promise<void> {
  const ref = storage.get<{ code: string; ts: number }>(REF_KEY);
  if (!ref || Date.now() - ref.ts > 30 * 86400_000) return;
  if (storage.get<string>(REF_BOUND_KEY) === u.uid) return;
  try {
    await api.patch("/me", { referral_code_used: ref.code });
    storage.set(REF_BOUND_KEY, u.uid);
  } catch (e) {
    if (e instanceof ApiError && (e.status === 409 || e.status === 422 || e.status === 400)) storage.set(REF_BOUND_KEY, u.uid);
  }
}

// ---------------------------------------------------------------- shell
const NAV: { href: string; label: string; name: PageName; show?: (me: Me | null, u: SessionUser | null) => boolean }[] = [
  { href: "#/market", label: "Marketplace", name: "market" },
  { href: "#/leaderboard", label: "Leaderboard", name: "leaderboard" },
  { href: "#/posts", label: "Posts", name: "posts" },
  { href: "#/dashboard", label: "Dashboard", name: "dashboard", show: (_m, u) => Boolean(u?.mfaSatisfied) },
  { href: "#/alerts", label: "Alerts", name: "alerts", show: (_m, u) => Boolean(u?.mfaSatisfied) },
  { href: "#/referrals", label: "Referrals", name: "referrals", show: (_m, u) => Boolean(u?.mfaSatisfied) },
  { href: "#/creator", label: "Creator Studio", name: "creator", show: (_m, u) => Boolean(u?.mfaSatisfied) },
  { href: "#/admin", label: "Admin", name: "admin", show: (m) => m?.role === "admin" },
];

function brandMark(): SVGElement {
  return svg("svg", { class: "brand-mark", viewBox: "8 -6 132 132", "aria-hidden": "true" },
    svg("circle", { class: "bm-ink", cx: 58, cy: 62, r: 40 }),
    svg("clipPath", { id: "bm-clip" }, svg("circle", { cx: 104, cy: 44, r: 26 })),
    svg("circle", { class: "bm-amber", cx: 58, cy: 62, r: 40, "clip-path": "url(#bm-clip)" }),
    svg("circle", { class: "bm-ring", cx: 104, cy: 44, r: 26, fill: "none", "stroke-width": 6.5 }));
}

function themeIcon(): SVGElement {
  const dark = effectiveTheme() === "dark";
  return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", class: "ico" },
    dark
      ? svg("path", { d: "M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z" })
      : svg("path", { d: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" }));
}

function buildShell(): { main: HTMLElement } {
  const app = document.getElementById("app")!;
  const navList = h("nav", { class: "nav", id: "site-nav", "aria-label": "Main" });
  const account = h("div", { class: "account" });
  const themeBtn = h("button", { type: "button", class: "icon-btn", id: "theme-toggle", "aria-label": "Toggle light or dark theme", title: "Toggle theme", onclick: () => toggleTheme() }, themeIcon());
  onThemeChange(() => mount(themeBtn, themeIcon()));
  const menuBtn = h("button", { type: "button", class: "icon-btn menu-btn", "aria-expanded": "false", "aria-controls": "site-nav", "aria-label": "Menu" },
    svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", class: "ico" }, svg("path", { d: "M4 7h16M4 12h16M4 17h16" })));
  const header = h("header", { class: "bar" },
    h("div", { class: "wrap bar-inner" },
      h("a", { class: "brand", href: "#/", "aria-label": "aijalon.trade home" }, brandMark(), h("b", null, "aijalon"), h("span", null, ".trade")),
      navList,
      h("div", { class: "bar-tools" }, themeBtn, account, menuBtn)));
  menuBtn.addEventListener("click", () => {
    const open = header.classList.toggle("nav-open");
    menuBtn.setAttribute("aria-expanded", String(open));
  });
  const main = h("main", { id: "main", class: "wrap", tabindex: "-1" });
  const year = new Date().getUTCFullYear();
  const footer = h("footer", { class: "foot" },
    h("div", { class: "wrap stack" },
      h("nav", { class: "foot-links", "aria-label": "Legal" },
        h("a", { href: "#/legal/terms" }, "Terms"),
        h("a", { href: "#/legal/risk-disclosure" }, "Risk Disclosure"),
        h("a", { href: "#/legal/privacy" }, "Privacy"),
        h("a", { href: "#/legal/liability-waiver" }, "Liability Waiver"),
        h("a", { href: "#/legal/jurisdiction" }, "Restricted jurisdictions"),
        h("a", { href: "#/legal/acceptable-use" }, "Acceptable use")),
      h("p", { class: "small" }, "Trading perpetual futures carries a high risk of losing all of the money you allocate. Nothing on this site is investment advice. Backtests and past results do not predict future results. Your funds stay in your own Hyperliquid account; our agent can trade but cannot withdraw."),
      h("p", { class: "small faint" }, `© ${year} aijalon.trade · Operated from Malaysia`)));
  clear(app).append(header, main, footer);

  let routeName: PageName | null = null;
  const drawNav = () => {
    const u = currentUser();
    const me = u?.mfaSatisfied ? meCache : null;
    clear(navList);
    for (const n of NAV) {
      if (n.show && !n.show(me, u)) continue;
      navList.append(h("a", { href: n.href, "aria-current": routeName === n.name ? "page" : null, onclick: () => header.classList.remove("nav-open") }, n.label));
    }
    clear(account);
    if (u) {
      const label = u.email ?? u.displayName ?? "Account";
      const menu = h("details", { class: "acct-menu" },
        h("summary", { class: "btn sm", "aria-label": `Account: ${label}` }, h("span", { class: "acct-name truncate" }, label)),
        h("div", { class: "acct-pop panel" },
          h("div", { class: "small muted truncate" }, label),
          !u.mfaSatisfied ? h("a", { href: "#/signin" }, "Finish two-factor setup") : h("a", { href: "#/dashboard" }, "Dashboard"),
          h("button", { type: "button", class: "btn sm", onclick: async () => { await signOut(); navigate("/"); } }, "Sign out")));
      account.append(menu);
    } else {
      account.append(h("a", { class: "btn sm primary", href: "#/signin" }, "Sign in"));
    }
  };
  let meCache: Me | null = null;
  onMeChange((me) => {
    meCache = me;
    drawNav();
  });
  onRouteChange((name) => {
    routeName = name;
    header.classList.remove("nav-open");
    drawNav();
  });
  onAuthChange(() => drawNav());
  drawNav();
  return { main };
}

// ---------------------------------------------------------------- boot
async function boot(): Promise<void> {
  // SECURITY M1: never run inside a frame (boot-guard.js has already hidden the page; see also firebase.json XFO).
  let framed = true;
  try {
    framed = window.top !== window.self;
  } catch {
    framed = true;
  }
  if (framed || (window as unknown as { __aijFramed?: boolean }).__aijFramed) {
    document.body.replaceChildren(document.createTextNode("aijalon.trade cannot be displayed inside another site. Open https://aijalon.trade directly."));
    return;
  }
  initTheme();
  await loadAppConfig();
  setApiHooks({
    getIdToken,
    stepUp,
    onMfaRequired: () => navigate(`/signin?next=${encodeURIComponent(currentPath().path)}`),
    onConsentRequired: () => showGateModal(),
  });
  setMeLoader(async () => {
    const u = currentUser();
    if (!u?.mfaSatisfied) return null;
    try {
      return await api.get<Me>("/me");
    } catch {
      return null;
    }
  });
  captureReferral();
  const { main } = buildShell();

  let prevUid: string | null | undefined;
  onAuthChange((u) => {
    const uid = u?.mfaSatisfied ? u.uid : null;
    if (uid === prevUid) return;
    const wasSignedIn = Boolean(prevUid);
    prevUid = uid;
    if (u && uid) {
      void syncConsents().catch(() => undefined);
      void bindReferral(u);
      void getMe(true);
    } else {
      clearMe();
    }
    // Re-render on sign-in/out so protected routes redirect and pages see the new user.
    if (wasSignedIn || uid) {
      const next = uid ? takeRedirectNext() : null;
      if (next && next.startsWith("#/") && !next.startsWith("#/signin")) navigate(next, { replace: true });
      else void render();
    }
  });
  void initAuth();
  startRouter(main);
}

void boot();

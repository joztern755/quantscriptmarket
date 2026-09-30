// Firebase Auth (Google + Apple only) with mandatory TOTP MFA (SPEC §5.2).
// The Firebase JS SDK is loaded at runtime as ES modules from gstatic at the version pinned in
// app-config.json (SRI via import map when web/sri.json is present — see build.mjs).

import { appConfig } from "./config.js";
import { ApiError } from "./api.js";
import type { PageContext } from "./router.js";
import { button, checkbox, clear, copyButton, field, h, modal, mount, note, promptDialog, spinner, toast } from "./ui.js";
import { qrSvg } from "./qr.js";
import { clearUserLocalData } from "./state.js";
import { getConnectedWallet } from "./wallet.js";

export interface SessionUser {
  uid: string;
  email: string | null;
  displayName: string | null;
  photoURL: string | null;
  providerId: string;
  mfaEnrolled: boolean;
  mfaSatisfied: boolean;
}

// ---- minimal typings for the parts of the Firebase modular SDK we use ----
/* eslint-disable @typescript-eslint/no-explicit-any */
type FbUser = any;
type FbAuth = any;
interface FbAuthModule {
  initializeAuth(app: unknown, deps: Record<string, unknown>): FbAuth;
  indexedDBLocalPersistence: unknown;
  browserLocalPersistence: unknown;
  browserPopupRedirectResolver: unknown;
  GoogleAuthProvider: new () => any;
  OAuthProvider: new (id: string) => any;
  signInWithPopup(auth: FbAuth, provider: unknown): Promise<any>;
  signInWithRedirect(auth: FbAuth, provider: unknown): Promise<never>;
  getRedirectResult(auth: FbAuth): Promise<any>;
  reauthenticateWithPopup(user: FbUser, provider: unknown): Promise<any>;
  onIdTokenChanged(auth: FbAuth, cb: (u: FbUser | null) => void): () => void;
  signOut(auth: FbAuth): Promise<void>;
  multiFactor(user: FbUser): { enrolledFactors: { factorId: string; uid: string; displayName?: string }[]; getSession(): Promise<unknown>; enroll(assertion: unknown, name?: string): Promise<void> };
  getMultiFactorResolver(auth: FbAuth, err: unknown): { hints: { factorId: string; uid: string; displayName?: string }[]; resolveSignIn(assertion: unknown): Promise<any> };
  TotpMultiFactorGenerator: {
    FACTOR_ID: string;
    generateSecret(session: unknown): Promise<{ secretKey: string; generateQrCodeUrl(account?: string, issuer?: string): string }>;
    assertionForEnrollment(secret: unknown, code: string): unknown;
    assertionForSignIn(enrollmentId: string, code: string): unknown;
  };
}

let fb: FbAuthModule | null = null;
let auth: FbAuth | null = null;
let current: SessionUser | null = null;
let readyResolve!: () => void;
const ready = new Promise<void>((r) => (readyResolve = r));
const listeners = new Set<(u: SessionUser | null) => void>();
let initStarted = false;
let loadError: string | null = null;

const NEXT_KEY = "aij.auth.next";

export function isAuthConfigured(): boolean {
  const f = appConfig().firebase;
  return Boolean(f.apiKey && f.projectId && f.appId && f.authDomain && !/REPLACE_ME/.test(f.apiKey + f.projectId + f.appId));
}

export function authLoadError(): string | null {
  return loadError;
}

export function authReady(): Promise<void> {
  return ready;
}

export function currentUser(): SessionUser | null {
  return current;
}

export function onAuthChange(cb: (u: SessionUser | null) => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function emit(): void {
  listeners.forEach((cb) => {
    try {
      cb(current);
    } catch { /* listener errors must not break auth */ }
  });
}

function code(err: unknown): string {
  return (err as { code?: string })?.code ?? "";
}

async function toSession(u: FbUser | null): Promise<SessionUser | null> {
  if (!u || !fb) return null;
  let satisfied = false;
  try {
    const res = await u.getIdTokenResult();
    satisfied = Boolean(res?.signInSecondFactor || res?.claims?.firebase?.sign_in_second_factor);
  } catch { /* offline */ }
  const enrolled = fb.multiFactor(u).enrolledFactors.some((f) => f.factorId === fb!.TotpMultiFactorGenerator.FACTOR_ID);
  return {
    uid: u.uid,
    email: u.email ?? null,
    displayName: u.displayName ?? null,
    photoURL: u.photoURL ?? null,
    providerId: u.providerData?.[0]?.providerId ?? "unknown",
    mfaEnrolled: enrolled,
    mfaSatisfied: satisfied,
  };
}

export async function initAuth(): Promise<void> {
  if (initStarted) return ready;
  initStarted = true;
  if (!isAuthConfigured()) {
    loadError = "not_configured";
    readyResolve();
    return;
  }
  const v = appConfig().firebaseSdkVersion;
  try {
    const appUrl = `https://www.gstatic.com/firebasejs/${v}/firebase-app.js`;
    const authUrl = `https://www.gstatic.com/firebasejs/${v}/firebase-auth.js`;
    const appMod = (await import(appUrl)) as { initializeApp(cfg: unknown): unknown };
    fb = (await import(authUrl)) as FbAuthModule;
    const app = appMod.initializeApp({ ...appConfig().firebase });
    auth = fb.initializeAuth(app, {
      persistence: [fb.indexedDBLocalPersistence, fb.browserLocalPersistence],
      popupRedirectResolver: fb.browserPopupRedirectResolver,
    });
  } catch {
    loadError = "load_failed";
    readyResolve();
    return;
  }
  let first = true;
  fb.onIdTokenChanged(auth, async (u) => {
    current = await toSession(u);
    if (first) {
      first = false;
      readyResolve();
    }
    emit();
  });
  // Complete a redirect sign-in (mobile/Safari fallback), including its MFA challenge.
  try {
    const res = await fb.getRedirectResult(auth);
    if (res?.user) await afterPrimarySignIn();
  } catch (err) {
    if (code(err) === "auth/multi-factor-auth-required") {
      try {
        await resolveMfa(err);
        await afterPrimarySignIn();
      } catch (e2) {
        if ((e2 as ApiError)?.code !== "step_up_cancelled") toast(friendlyAuthError(e2), "bad");
      }
    } else if (code(err)) {
      toast(friendlyAuthError(err), "bad");
    }
  }
}

function friendlyAuthError(err: unknown): string {
  switch (code(err)) {
    case "auth/popup-closed-by-user":
    case "auth/cancelled-popup-request":
      return "Sign-in window closed.";
    case "auth/invalid-verification-code":
    case "auth/invalid-verification-id":
      return "That code is not valid. Check your authenticator app and try again.";
    case "auth/account-exists-with-different-credential":
      return "An account already exists with this email using the other provider. Sign in with that provider.";
    case "auth/user-disabled":
      return "This account is disabled. Contact support.";
    case "auth/network-request-failed":
      return "Network error during sign-in. Try again.";
    case "auth/unverified-email":
      return "Your email address must be verified before adding two-factor authentication.";
    case "auth/too-many-requests":
      return "Too many attempts. Wait a few minutes and try again.";
    case "auth/unauthorized-domain":
      return "Sign-in is not allowed from this domain.";
    default:
      return (err as Error)?.message && !(err as { code?: string }).code ? (err as Error).message : "Sign-in failed. Please try again.";
  }
}

function providerFor(id: "google" | "apple" | string): unknown {
  if (!fb) throw new Error("auth not loaded");
  if (id === "google" || id === "google.com") {
    const p = new fb.GoogleAuthProvider();
    p.setCustomParameters?.({ prompt: "select_account" });
    return p;
  }
  if (id === "apple" || id === "apple.com") {
    const p = new fb.OAuthProvider("apple.com");
    p.addScope?.("email");
    p.addScope?.("name");
    return p;
  }
  throw new Error("Unsupported sign-in provider");
}

function preferRedirect(): boolean {
  const standalone = window.matchMedia?.("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true;
  return Boolean(standalone);
}

/** Prompts for the 6-digit TOTP code and completes an MFA sign-in / re-auth challenge. */
async function resolveMfa(err: unknown): Promise<unknown> {
  if (!fb || !auth) throw err;
  const resolver = fb.getMultiFactorResolver(auth, err);
  const hint = resolver.hints.find((x) => x.factorId === fb!.TotpMultiFactorGenerator.FACTOR_ID);
  if (!hint) throw new ApiError(0, "mfa_unsupported", "Your account uses a second factor this site doesn't support. Contact support.");
  let message: string | null = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    const c = await promptDialog({
      title: "Two-factor code",
      message: h("div", { class: "stack tight" },
        h("p", null, "Enter the 6-digit code from your authenticator app", hint.displayName ? ` (${hint.displayName})` : "", "."),
        message ? h("p", { class: "status err" }, message) : null),
      label: "Authentication code",
      placeholder: "123456",
      inputMode: "numeric",
      autocomplete: "one-time-code",
      pattern: /^\d{6}$/,
      submitLabel: "Verify",
    });
    if (c === null) throw new ApiError(0, "step_up_cancelled", "Verification cancelled.");
    try {
      return await resolver.resolveSignIn(fb.TotpMultiFactorGenerator.assertionForSignIn(hint.uid, c));
    } catch (e) {
      if (code(e) === "auth/invalid-verification-code") {
        message = "That code didn't work. Wait for the next code and try again.";
        continue;
      }
      throw e;
    }
  }
  throw new ApiError(0, "mfa_failed", "Too many wrong codes. Try again later.");
}

async function refreshSession(): Promise<void> {
  if (!auth?.currentUser) return;
  await auth.currentUser.getIdToken(true);
  current = await toSession(auth.currentUser);
  emit();
}

async function afterPrimarySignIn(): Promise<void> {
  await refreshSession();
  if (current && !current.mfaEnrolled) await ensureMfaEnrolled();
}

export async function signIn(provider: "google" | "apple"): Promise<SessionUser | null> {
  await initAuth();
  if (!fb || !auth) throw new ApiError(0, "auth_unavailable", isAuthConfigured() ? "Sign-in could not be loaded. Check your connection or content blockers." : "Sign-in is not configured on this deployment.");
  const p = providerFor(provider);
  try {
    if (preferRedirect()) {
      stashNext();
      await fb.signInWithRedirect(auth, p);
      return null;
    }
    await fb.signInWithPopup(auth, p);
  } catch (err) {
    const c = code(err);
    if (c === "auth/multi-factor-auth-required") {
      await resolveMfa(err);
    } else if (c === "auth/popup-blocked" || c === "auth/operation-not-supported-in-this-environment" || c === "auth/web-storage-unsupported") {
      stashNext();
      await fb.signInWithRedirect(auth, p);
      return null;
    } else if (c === "auth/popup-closed-by-user" || c === "auth/cancelled-popup-request") {
      return null;
    } else {
      throw new ApiError(0, c || "auth_failed", friendlyAuthError(err));
    }
  }
  await afterPrimarySignIn();
  return current;
}

function stashNext(): void {
  try {
    sessionStorage.setItem(NEXT_KEY, location.hash || "#/");
  } catch { /* ignore */ }
}

/** Where to go after a redirect sign-in completed (consumed once). */
export function takeRedirectNext(): string | null {
  try {
    const v = sessionStorage.getItem(NEXT_KEY);
    sessionStorage.removeItem(NEXT_KEY);
    return v;
  } catch {
    return null;
  }
}

export async function signOut(): Promise<void> {
  if (fb && auth) await fb.signOut(auth);
  current = null;
  clearUserLocalData(); // SECURITY L4: no wallet/uid linkage left behind on shared devices
  getConnectedWallet()?.disconnect();
  emit();
}

export async function getIdToken(forceRefresh = false): Promise<string | null> {
  await ready;
  const u = auth?.currentUser;
  if (!u) return null;
  return (await u.getIdToken(forceRefresh)) as string;
}

/**
 * Step-up for money/security actions (SPEC §5.2): fresh Google/Apple re-auth + TOTP.
 * Opens a dialog first so the popup is launched from a user click (popup blockers).
 */
export async function stepUp(reason?: string): Promise<void> {
  await ready;
  if (!fb || !auth?.currentUser) throw new ApiError(401, "unauthorized", "Please sign in.");
  const user = auth.currentUser;
  const providerId = user.providerData?.[0]?.providerId ?? "google.com";
  const providerName = providerId === "apple.com" ? "Apple" : "Google";
  let failure: unknown = null;
  let done = false;
  const m = modal({
    title: "Confirm it's you",
    body: h("div", { class: "stack" },
      h("p", null, reason ?? "This action moves money or changes security settings, so we need a fresh sign-in."),
      h("p", { class: "muted small" }, `You'll sign in with ${providerName} again and enter your authenticator code.`)),
    actions: [
      { label: "Cancel", kind: "plain" },
      {
        label: `Continue with ${providerName}`,
        kind: "primary",
        onClick: async () => {
          try {
            await fb!.reauthenticateWithPopup(user, providerFor(providerId));
            // Accounts with MFA always get the MFA challenge on re-auth; if not, enrolment is missing.
            done = true;
          } catch (err) {
            if (code(err) === "auth/multi-factor-auth-required") {
              try {
                await resolveMfa(err);
                done = true;
              } catch (e2) {
                failure = e2;
              }
            } else if (code(err) === "auth/popup-closed-by-user" || code(err) === "auth/cancelled-popup-request") {
              return false; // keep dialog open
            } else {
              failure = err;
            }
          }
          return true;
        },
      },
    ],
  });
  await m.closed;
  if (!done) {
    if (failure && (failure as ApiError).code !== "step_up_cancelled") throw new ApiError(0, code(failure) || "step_up_failed", friendlyAuthError(failure));
    throw new ApiError(0, "step_up_cancelled", "Confirmation cancelled.");
  }
  await refreshSession();
  if (current && !current.mfaEnrolled) {
    await ensureMfaEnrolled();
    throw new ApiError(0, "step_up_cancelled", "Two-factor authentication was just set up. Please retry the action.");
  }
}

function groupSecret(s: string): string {
  return s.replace(/(.{4})/g, "$1 ").trim();
}

/** Shows TOTP enrolment (QR + secret) when the user has no TOTP factor. Resolves true when enrolled. */
export async function ensureMfaEnrolled(): Promise<boolean> {
  await ready;
  if (!fb || !auth?.currentUser) return false;
  const user = auth.currentUser;
  const TOTP = fb.TotpMultiFactorGenerator;
  if (fb.multiFactor(user).enrolledFactors.some((f) => f.factorId === TOTP.FACTOR_ID)) return true;

  const body = h("div", { class: "stack" }, spinner("Preparing…"));
  let enrolled = false;
  const m = modal({ title: "Set up two-factor authentication", body, dismissible: true, wide: false });
  const renderError = (msg: string, retry: () => void) => mount(body, note(msg, "bad"), button("Try again", { onClick: retry }));

  const start = async (): Promise<void> => {
    mount(body, spinner("Preparing…"));
    let secret: Awaited<ReturnType<typeof TOTP.generateSecret>>;
    try {
      const session = await fb!.multiFactor(user).getSession();
      secret = await TOTP.generateSecret(session);
    } catch (err) {
      if (code(err) === "auth/requires-recent-login") {
        mount(body, h("p", null, "For security, sign in again before adding two-factor authentication."),
          button("Sign in again", {
            kind: "primary",
            onClick: async () => {
              await fb!.reauthenticateWithPopup(user, providerFor(user.providerData?.[0]?.providerId ?? "google.com"));
              await start();
            },
          }));
        return;
      }
      renderError(friendlyAuthError(err), () => void start());
      return;
    }
    const url = secret.generateQrCodeUrl(user.email ?? user.uid, "aijalon.trade");
    const codeInput = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", placeholder: "123456", maxlength: 6, pattern: "[0-9]{6}" });
    const status = h("div", { class: "status", "aria-live": "polite" });
    const saved = checkbox("I saved this in my authenticator app. I understand that losing it can lock me out.", { required: true });
    const verify = button("Verify and turn on", {
      kind: "primary",
      disabled: true,
      onClick: async () => {
        const c = codeInput.value.trim();
        if (!/^\d{6}$/.test(c)) {
          status.className = "status err";
          status.textContent = "Enter the 6-digit code.";
          return;
        }
        try {
          await fb!.multiFactor(user).enroll(TOTP.assertionForEnrollment(secret, c), "Authenticator app");
          enrolled = true;
          m.close();
        } catch (err) {
          status.className = "status err";
          status.textContent = friendlyAuthError(err);
        }
      },
    });
    const sync = () => (verify.disabled = !(saved.input.checked && /^\d{6}$/.test(codeInput.value.trim())));
    saved.input.addEventListener("change", sync);
    codeInput.addEventListener("input", sync);
    let qr: Node;
    try {
      qr = qrSvg(url, { ecc: "M", size: 208, label: "QR code for your authenticator app" });
    } catch {
      qr = note("QR could not be drawn — use the key below.", "info");
    }
    mount(body,
      h("p", null, "Two-factor authentication is required on aijalon.trade. Scan this QR code with an authenticator app (Google Authenticator, 1Password, Authy, Aegis…)."),
      h("div", { class: "qr-box" }, qr),
      h("details", null,
        h("summary", null, "Can't scan? Enter the key manually"),
        h("div", { class: "stack tight" },
          h("code", { class: "secret mono" }, groupSecret(secret.secretKey)),
          h("div", { class: "btns" }, copyButton(secret.secretKey, "Copy key"), h("a", { class: "btn sm", href: url }, "Open in authenticator app")),
          h("p", { class: "small muted" }, "Type: time-based (TOTP), 6 digits, 30 seconds."))),
      field("Code from the app", codeInput),
      saved.el,
      status,
      h("div", { class: "btns" }, verify));
    codeInput.focus();
  };
  void start();
  await m.closed;
  if (!enrolled) return false;
  // The ID token after enrolment should carry the second-factor claim; if not, a fresh sign-in is needed.
  await refreshSession();
  if (current && !current.mfaSatisfied) {
    toast("Two-factor is on. Please sign in again to finish.", "info", 7000);
    await signOut();
  } else {
    toast("Two-factor authentication is on.", "good");
  }
  return true;
}

// ---------------------------------------------------------------- sign-in page

function safeNext(q: string | null): string {
  if (q && /^\/(?!\/)[^\\\s]*$/.test(q) && !q.startsWith("/signin")) return q;
  return "/dashboard";
}


export function renderSignIn(root: HTMLElement, ctx: PageContext): void {
  const next = safeNext(ctx.query.get("next"));
  const box = h("div", { class: "signin panel" });
  root.append(h("div", { class: "signin-wrap" }, box));

  const draw = () => {
    if (!ctx.isCurrent()) return;
    clear(box);
    box.append(h("div", { class: "eyebrow" }, "Account"), h("h1", { class: "h2" }, "Sign in to aijalon.trade"));
    if (!isAuthConfigured() || loadError) {
      box.append(note(loadError === "load_failed"
        ? "Sign-in couldn't load. Check your connection or disable content blockers for aijalon.trade, then reload."
        : "Sign-in is not configured on this deployment yet.", "warn"));
      return;
    }
    const u = current;
    if (u && u.mfaSatisfied) {
      box.append(h("p", null, "You're signed in as ", h("b", null, u.email ?? u.displayName ?? "your account"), "."),
        h("div", { class: "btns" }, button("Continue", { kind: "primary", onClick: () => ctx.navigate(next, { replace: true }) }), button("Sign out", { onClick: () => signOut() })));
      return;
    }
    if (u && !u.mfaEnrolled) {
      box.append(h("p", null, "One more step: two-factor authentication (TOTP) is required before you can use your account."),
        h("div", { class: "btns" },
          button("Set up two-factor", { kind: "primary", onClick: async () => { if (await ensureMfaEnrolled()) draw(); } }),
          button("Sign out", { onClick: () => signOut() })));
      return;
    }
    if (u && !u.mfaSatisfied) {
      box.append(h("p", null, "Your session needs to be verified with your authenticator code. Sign out and sign in again."),
        button("Sign out", { kind: "primary", onClick: () => signOut() }));
      return;
    }
    box.append(
      h("p", { class: "muted" }, "Use Google or Apple. Two-factor authentication with an authenticator app is required for every account."),
      h("div", { class: "stack tight provs" },
        button("Continue with Google", { onClick: async () => { const r = await signIn("google"); if (r?.mfaSatisfied) ctx.navigate(next, { replace: true }); else draw(); } }),
        button("Continue with Apple", { onClick: async () => { const r = await signIn("apple"); if (r?.mfaSatisfied) ctx.navigate(next, { replace: true }); else draw(); } })),
      h("p", { class: "small muted" }, "By continuing you confirm the terms you accepted on entry. We never see your Google or Apple password."));
  };

  draw();
  ctx.onCleanup(onAuthChange(() => draw()));
  if (!initStarted) void initAuth().then(draw);
}

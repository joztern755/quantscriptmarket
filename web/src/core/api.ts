// API client for https://api.aijalon.trade/v1 (origin from app-config.json).
// - Bearer Firebase ID token (never in URLs, never logged).
// - Idempotency-Key on every POST/PATCH/DELETE (reused on the automatic retry).
// - JSON errors → ApiError. One automatic retry for: expired token, step-up, consent re-record.

import { appConfig } from "./config.js";

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;
  requestId?: string;
  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

export interface ReqOpts {
  signal?: AbortSignal;
  auth?: boolean;
  idempotencyKey?: string;
  timeoutMs?: number;
  stepUp?: boolean;
}

interface Hooks {
  getIdToken(force: boolean): Promise<string | null>;
  stepUp(reason?: string): Promise<void>;
  onMfaRequired(): void;
  onConsentRequired(): Promise<boolean>;
}

let hooks: Hooks = {
  getIdToken: async () => null,
  stepUp: async () => {
    throw new ApiError(401, "step_up_cancelled", "Confirmation cancelled.");
  },
  onMfaRequired: () => undefined,
  onConsentRequired: async () => false,
};

/** Wired by main.ts so api.ts has no import cycle with auth.ts/gate.ts. */
export function setApiHooks(h: Partial<Hooks>): void {
  hooks = { ...hooks, ...h };
}

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6]! & 0x0f) | 0x40;
  b[8] = (b[8]! & 0x3f) | 0x80;
  const hx = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${hx.slice(0, 8)}-${hx.slice(8, 12)}-${hx.slice(12, 16)}-${hx.slice(16, 20)}-${hx.slice(20)}`;
}

function buildUrl(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//") || /[\s\\]/.test(path)) throw new ApiError(0, "bad_path", "Invalid API path");
  return `${appConfig().apiOrigin}/v1${path}`;
}

async function parseError(res: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch { /* not JSON */ }
  const requestId = res.headers.get("x-request-id") ?? undefined;
  let code = `http_${res.status}`;
  let message = res.statusText || "Request failed";
  let details: Record<string, unknown> = {};
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const e = (b.error && typeof b.error === "object" ? b.error : typeof b.detail === "object" && b.detail ? b.detail : b) as Record<string, unknown>;
    if (typeof e.code === "string") code = e.code;
    if (typeof e.message === "string") message = e.message;
    else if (typeof b.detail === "string") message = b.detail;
    if (e.details && typeof e.details === "object") details = e.details as Record<string, unknown>;
    if (typeof b.request_id === "string") return new ApiError(res.status, code, message, details, b.request_id);
  }
  if (code.startsWith("http_")) {
    const map: Record<number, string> = { 400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 422: "validation_failed", 429: "rate_limited" };
    code = map[res.status] ?? code;
  }
  return new ApiError(res.status, code, message, details, requestId);
}

async function once(method: string, path: string, body: unknown, opts: ReqOpts, idemKey: string | null, forceToken: boolean): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const wantsAuth = opts.auth ?? !path.startsWith("/public/");
  if (wantsAuth) {
    const token = await hooks.getIdToken(forceToken);
    if (!token) throw new ApiError(401, "unauthorized", "Please sign in.");
    headers.Authorization = `Bearer ${token}`;
  }
  if (idemKey) headers["Idempotency-Key"] = idemKey;
  let payload: string | undefined;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const ctrl = new AbortController();
  const timeout = window.setTimeout(() => ctrl.abort(new DOMException("timeout", "TimeoutError")), opts.timeoutMs ?? 20000);
  const onAbort = () => ctrl.abort(opts.signal?.reason);
  opts.signal?.addEventListener("abort", onAbort, { once: true });
  try {
    return await fetch(buildUrl(path), {
      method,
      headers,
      body: payload,
      signal: ctrl.signal,
      credentials: "omit",
      mode: "cors",
      cache: "no-store",
      referrerPolicy: "strict-origin",
    });
  } catch (err) {
    if (opts.signal?.aborted) throw err; // caller navigated away: propagate AbortError
    if (ctrl.signal.aborted) throw new ApiError(0, "timeout", "The request timed out.");
    throw new ApiError(0, "network_error", "Network error");
  } finally {
    window.clearTimeout(timeout);
    opts.signal?.removeEventListener("abort", onAbort);
  }
}

async function request<T>(method: string, path: string, body: unknown, opts: ReqOpts = {}): Promise<T> {
  const idemKey = method === "GET" ? null : opts.idempotencyKey ?? newIdempotencyKey();
  let res = await once(method, path, body, opts, idemKey, false);
  if (!res.ok) {
    const err = await parseError(res);
    let retry = false;
    let forceToken = false;
    if (err.status === 401 && err.code === "step_up_required" && opts.stepUp !== false) {
      await hooks.stepUp(typeof err.details.reason === "string" ? err.details.reason : undefined);
      retry = true;
      forceToken = true;
    } else if (err.status === 401 && err.code === "unauthorized" && (opts.auth ?? !path.startsWith("/public/"))) {
      retry = true;
      forceToken = true;
    } else if (err.status === 401 && err.code === "mfa_required") {
      hooks.onMfaRequired();
      throw err;
    } else if (err.status === 403 && err.code === "consent_required") {
      retry = await hooks.onConsentRequired();
    }
    if (!retry) throw err;
    res = await once(method, path, body, opts, idemKey, forceToken);
    if (!res.ok) throw await parseError(res);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") ?? "";
  if (!ct.includes("application/json")) return (await res.text()) as unknown as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string, opts?: ReqOpts) => request<T>("GET", path, undefined, opts),
  post: <T>(path: string, body?: unknown, opts?: ReqOpts) => request<T>("POST", path, body ?? {}, opts),
  patch: <T>(path: string, body?: unknown, opts?: ReqOpts) => request<T>("PATCH", path, body ?? {}, opts),
  del: <T>(path: string, opts?: ReqOpts) => request<T>("DELETE", path, undefined, opts),
};

// ---------------------------------------------------------------- public config

export type ConsentDoc = "terms" | "risk" | "privacy" | "waiver" | "jurisdiction" | "creator_agreement" | "subscription_ack";

export interface PublicConfig {
  builder_address: string;
  treasury_address: string;
  agent_name: string;
  hl_chain: "Mainnet" | "Testnet";
  stripe_publishable_key: string | null;
  /** Stripe processor fees are passed to the user (credit = paid − actual fee). Estimate only; null = unknown. */
  stripe_fee_estimate: { pct_bps: number; fixed_micro: number } | null;
  restricted_jurisdictions: string[];
  legal_versions: Record<"terms" | "risk" | "privacy" | "waiver" | "jurisdiction", string> & Partial<Record<ConsentDoc, string>>;
  economics: {
    builder_fee_tenths_bp: number;
    builder_split_creator_bps: number;
    builder_split_platform_bps: number;
    builder_split_referral_pool_bps: number;
    profit_share_creator_cap_bps: number;
    platform_profit_share_bps: number;
    platform_profit_share_mode: "on_top" | "carved_out";
    subscription_platform_bps: number;
    post_platform_fee_micro: number;
    post_min_price_micro: number;
    min_topup_micro: number;
    past_due_grace_hours: number;
  };
  plans: { key: "free" | "pro" | "max"; price_monthly_micro: number; max_active_strategies: number | null; features: string[] }[];
  features: { creator_uploads: boolean };
  _fallback?: true;
}

// Defaults mirror backend/app/config.py (SPEC §1) — used only for display when a field is missing.
const ECON_DEFAULTS: PublicConfig["economics"] = {
  builder_fee_tenths_bp: 100,
  builder_split_creator_bps: 5000,
  builder_split_platform_bps: 3000,
  builder_split_referral_pool_bps: 2000,
  profit_share_creator_cap_bps: 1200,
  platform_profit_share_bps: 150,
  platform_profit_share_mode: "on_top",
  subscription_platform_bps: 300,
  post_platform_fee_micro: 1_000_000,
  post_min_price_micro: 2_000_000,
  min_topup_micro: 10_000_000,
  past_due_grace_hours: 72,
};

const ADDR = /^0x[0-9a-f]{40}$/;

function num(v: unknown, d: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : d;
}

export function normalizePublicConfig(raw: unknown, fallback = false): PublicConfig {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const ac = appConfig();
  const econRaw = (r.economics && typeof r.economics === "object" ? r.economics : {}) as Record<string, unknown>;
  const economics = { ...ECON_DEFAULTS };
  for (const k of Object.keys(ECON_DEFAULTS) as (keyof typeof ECON_DEFAULTS)[]) {
    if (k === "platform_profit_share_mode") {
      economics.platform_profit_share_mode = econRaw[k] === "carved_out" ? "carved_out" : "on_top";
    } else {
      (economics as Record<string, number | string>)[k] = num(econRaw[k], ECON_DEFAULTS[k] as number);
    }
  }
  const lvRaw = (r.legal_versions && typeof r.legal_versions === "object" ? r.legal_versions : {}) as Record<string, unknown>;
  const legal: Record<string, string> = { ...ac.fallback.legal_versions };
  for (const [k, v] of Object.entries(lvRaw)) if (typeof v === "string" && v) legal[k] = v;
  const juris = Array.isArray(r.restricted_jurisdictions)
    ? r.restricted_jurisdictions.filter((x): x is string => typeof x === "string" && /^[A-Z]{2}$/.test(x))
    : ac.fallback.restricted_jurisdictions;
  const lower = (v: unknown) => (typeof v === "string" && ADDR.test(v.toLowerCase()) ? v.toLowerCase() : "");
  const sfe = r.stripe_fee_estimate as Record<string, unknown> | null | undefined;
  const cfg: PublicConfig = {
    builder_address: lower(r.builder_address),
    treasury_address: lower(r.treasury_address),
    agent_name: typeof r.agent_name === "string" && /^[A-Za-z0-9_-]{1,16}$/.test(r.agent_name) ? r.agent_name : "aijalon",
    hl_chain: r.hl_chain === "Testnet" ? "Testnet" : "Mainnet",
    stripe_publishable_key: typeof r.stripe_publishable_key === "string" && /^pk_(live|test)_[A-Za-z0-9]+$/.test(r.stripe_publishable_key) ? r.stripe_publishable_key : null,
    stripe_fee_estimate: sfe && typeof sfe === "object" && typeof sfe.pct_bps === "number" && typeof sfe.fixed_micro === "number" ? { pct_bps: sfe.pct_bps, fixed_micro: sfe.fixed_micro } : null,
    restricted_jurisdictions: juris,
    legal_versions: legal as PublicConfig["legal_versions"],
    economics,
    plans: Array.isArray(r.plans) ? (r.plans as PublicConfig["plans"]) : [
      { key: "free", price_monthly_micro: 0, max_active_strategies: 1, features: [] },
      { key: "pro", price_monthly_micro: 20_000_000, max_active_strategies: 3, features: [] },
      { key: "max", price_monthly_micro: 50_000_000, max_active_strategies: null, features: [] },
    ],
    features: { creator_uploads: Boolean((r.features as Record<string, unknown> | undefined)?.creator_uploads ?? true) },
  };
  if (fallback) cfg._fallback = true;
  return cfg;
}

let cfgPromise: Promise<PublicConfig> | null = null;
let cfgCached: PublicConfig | null = null;

/** GET /v1/public/config (cached). Never rejects: on failure returns static fallbacks with `_fallback: true`. */
export function publicConfig(force = false): Promise<PublicConfig> {
  if (!force && cfgPromise) return cfgPromise;
  cfgPromise = api
    .get<unknown>("/public/config", { auth: false, timeoutMs: 10000 })
    .then((raw) => normalizePublicConfig(raw))
    .catch(() => {
      cfgPromise = null; // retry next time
      return normalizePublicConfig({}, true);
    })
    .then((c) => (cfgCached = c));
  return cfgPromise;
}

export function peekPublicConfig(): PublicConfig | null {
  return cfgCached;
}

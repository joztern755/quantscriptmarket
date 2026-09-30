// Static app configuration (dist/app-config.json). Loaded once at boot by main.ts, before anything else.
// Only PUBLIC values live here (Firebase web config is a public identifier, not a secret).

export interface FirebaseWebConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}

/**
 * Signing trust anchors (SECURITY H1). They are PINNED in the Hosting-served app-config.json (a reviewed commit,
 * deployed by a hosting-only identity) and are the ONLY values the wallet flows sign against: any value the API
 * returns (GET /v1/public/config, typed data, POST /agents) must equal them or nothing is signed.
 * An empty string means "not pinned in this build" → every flow that needs it refuses to sign.
 */
export interface TrustAnchors {
  hlChain: "Mainnet" | "Testnet" | "";
  agentName: string;
  /** lower-case 0x address or "" */
  builderAddress: string;
  /** lower-case 0x address or "" */
  treasuryAddress: string;
  /** hard ceiling for the builder fee we ask users to approve, tenths of a bp (never above 100 = 0.1 %) */
  maxBuilderFeeTenthsBp: number;
  /** base64 DER SubjectPublicKeyInfo of the executor's KMS agent-attestation key (EC P-256) or "" */
  agentAttestPublicKeySpki: string;
  /** exact host names a KYC redirect may point to (provider host only) */
  kycRedirectHosts: string[];
}

export interface AppConfig {
  siteOrigin: string;
  apiOrigin: string;
  hlApiUrl: string;
  firebaseSdkVersion: string;
  firebase: FirebaseWebConfig;
  trust: TrustAnchors;
  fallback: {
    restricted_jurisdictions: string[];
    legal_versions: Record<string, string>;
  };
}

/** Hyperliquid perps cap builder fees at 0.1 % = 100 tenths of a bp; nothing above it is ever signed. */
export const HL_MAX_BUILDER_FEE_TENTHS_BP = 100;

const NO_TRUST: TrustAnchors = {
  hlChain: "",
  agentName: "",
  builderAddress: "",
  treasuryAddress: "",
  maxBuilderFeeTenthsBp: 0,
  agentAttestPublicKeySpki: "",
  kycRedirectHosts: [],
};

const DEFAULTS: AppConfig = {
  siteOrigin: "https://aijalon.trade",
  apiOrigin: "https://api.aijalon.trade",
  hlApiUrl: "https://api.hyperliquid.xyz",
  firebaseSdkVersion: "12.3.0",
  firebase: { apiKey: "", authDomain: "", projectId: "", appId: "" },
  trust: NO_TRUST,
  fallback: {
    restricted_jurisdictions: ["US", "CU", "IR", "KP", "SY", "RU", "BY", "MM"],
    legal_versions: { terms: "draft", risk: "draft", privacy: "draft", waiver: "draft", jurisdiction: "draft" },
  },
};

let cfg: AppConfig = DEFAULTS;

const HTTPS_ORIGIN = /^https:\/\/[a-z0-9.-]+(:\d+)?$/i;
const LOCAL_ORIGIN = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/i;
const VERSION = /^\d+\.\d+\.\d+$/;

function origin(v: unknown, fallback: string): string {
  if (typeof v !== "string") return fallback;
  const s = v.replace(/\/+$/, "");
  return HTTPS_ORIGIN.test(s) || LOCAL_ORIGIN.test(s) ? s : fallback;
}

function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

const ADDR = /^0x[0-9a-fA-F]{40}$/;
const HOST = /^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
const SPKI_B64 = /^[A-Za-z0-9+/]{40,400}={0,2}$/;

/** Parses the pinned trust block. Anything malformed or still a placeholder becomes "" (= not pinned → refuse). */
export function parseTrust(raw: unknown): TrustAnchors {
  const t = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const addr = (v: unknown) => (typeof v === "string" && ADDR.test(v) ? v.toLowerCase() : "");
  const fee = t.maxBuilderFeeTenthsBp;
  const hosts = Array.isArray(t.kycRedirectHosts)
    ? t.kycRedirectHosts.filter((x): x is string => typeof x === "string" && HOST.test(x.toLowerCase())).map((x) => x.toLowerCase())
    : [];
  return {
    hlChain: t.hlChain === "Mainnet" || t.hlChain === "Testnet" ? t.hlChain : "",
    agentName: typeof t.agentName === "string" && /^[A-Za-z0-9_-]{1,16}$/.test(t.agentName) ? t.agentName : "",
    builderAddress: addr(t.builderAddress),
    treasuryAddress: addr(t.treasuryAddress),
    maxBuilderFeeTenthsBp:
      typeof fee === "number" && Number.isInteger(fee) && fee > 0 && fee <= HL_MAX_BUILDER_FEE_TENTHS_BP ? fee : 0,
    agentAttestPublicKeySpki: typeof t.agentAttestPublicKeySpki === "string" && SPKI_B64.test(t.agentAttestPublicKeySpki) ? t.agentAttestPublicKeySpki : "",
    kycRedirectHosts: hosts,
  };
}

export async function loadAppConfig(): Promise<AppConfig> {
  try {
    const res = await fetch(new URL("app-config.json", document.baseURI).href, { cache: "no-cache", credentials: "same-origin" });
    if (!res.ok) throw new Error(String(res.status));
    const raw = (await res.json()) as Record<string, unknown>;
    const fb = (raw.firebase ?? {}) as Record<string, unknown>;
    const fallback = (raw.fallback ?? {}) as Record<string, unknown>;
    const juris = Array.isArray(fallback.restricted_jurisdictions)
      ? fallback.restricted_jurisdictions.filter((x): x is string => typeof x === "string" && /^[A-Z]{2}$/.test(x))
      : DEFAULTS.fallback.restricted_jurisdictions;
    const lv: Record<string, string> = { ...DEFAULTS.fallback.legal_versions };
    if (fallback.legal_versions && typeof fallback.legal_versions === "object") {
      for (const [k, v] of Object.entries(fallback.legal_versions as Record<string, unknown>)) if (typeof v === "string") lv[k] = v;
    }
    const sdk = str(raw.firebaseSdkVersion);
    cfg = {
      siteOrigin: origin(raw.siteOrigin, DEFAULTS.siteOrigin),
      apiOrigin: origin(raw.apiOrigin, DEFAULTS.apiOrigin),
      hlApiUrl: origin(raw.hlApiUrl, DEFAULTS.hlApiUrl),
      firebaseSdkVersion: VERSION.test(sdk) ? sdk : DEFAULTS.firebaseSdkVersion,
      firebase: { apiKey: str(fb.apiKey), authDomain: str(fb.authDomain), projectId: str(fb.projectId), appId: str(fb.appId) },
      trust: parseTrust(raw.trust),
      fallback: { restricted_jurisdictions: juris, legal_versions: lv },
    };
  } catch {
    cfg = DEFAULTS;
  }
  return cfg;
}

export function appConfig(): AppConfig {
  return cfg;
}

/** The pinned signing trust anchors of this build (see TrustAnchors). */
export function trustAnchors(): TrustAnchors {
  return cfg.trust;
}

/** The site's own host name, used in wallet ownership messages (EIP-4361 domain). */
export function siteDomain(): string {
  try {
    return new URL(cfg.siteOrigin).host;
  } catch {
    return "aijalon.trade";
  }
}

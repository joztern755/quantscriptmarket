// Static app configuration (dist/app-config.json). Loaded once at boot by main.ts, before anything else.
// Only PUBLIC values live here (Firebase web config is a public identifier, not a secret).

export interface FirebaseWebConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}

export interface AppConfig {
  siteOrigin: string;
  apiOrigin: string;
  hlApiUrl: string;
  firebaseSdkVersion: string;
  firebase: FirebaseWebConfig;
  fallback: {
    restricted_jurisdictions: string[];
    legal_versions: Record<string, string>;
  };
}

const DEFAULTS: AppConfig = {
  siteOrigin: "https://aijalon.trade",
  apiOrigin: "https://api.aijalon.trade",
  hlApiUrl: "https://api.hyperliquid.xyz",
  firebaseSdkVersion: "12.3.0",
  firebase: { apiKey: "", authDomain: "", projectId: "", appId: "" },
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

/** The site's own host name, used in wallet ownership messages (EIP-4361 domain). */
export function siteDomain(): string {
  try {
    return new URL(cfg.siteOrigin).host;
  } catch {
    return "aijalon.trade";
  }
}

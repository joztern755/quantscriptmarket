// Small reactive store + the signed-in user's profile (GET /v1/me) + safe localStorage.

export interface Store<T> {
  get(): T;
  set(v: T | ((prev: T) => T)): void;
  subscribe(cb: (v: T) => void): () => void;
}

export function createStore<T>(initial: T): Store<T> {
  let value = initial;
  const subs = new Set<(v: T) => void>();
  return {
    get: () => value,
    set(v) {
      value = typeof v === "function" ? (v as (p: T) => T)(value) : v;
      subs.forEach((cb) => cb(value));
    },
    subscribe(cb) {
      subs.add(cb);
      return () => subs.delete(cb);
    },
  };
}

/** localStorage wrapper: JSON values, every access in try/catch (private mode / blocked storage → no-op). */
export const storage = {
  get<T = unknown>(key: string): T | null {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? null : (JSON.parse(raw) as T);
    } catch {
      return null;
    }
  },
  set(key: string, value: unknown): void {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch { /* ignore */ }
  },
  remove(key: string): void {
    try {
      localStorage.removeItem(key);
    } catch { /* ignore */ }
  },
};

/**
 * Sign-out hygiene (SECURITY L4): remove every localStorage entry that links this browser to an account or wallet —
 * subscribe-wizard progress (master/trading/agent addresses, typed data), the referral-bound uid and the per-uid
 * consent sync map. Device-level choices (theme, site entry-gate acceptance) stay.
 */
export function clearUserLocalData(): void {
  try {
    const keys: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k) keys.push(k);
    }
    for (const k of keys) {
      if (k.startsWith("aijalon.subwiz.") || k === "aij.ref.bound") localStorage.removeItem(k);
    }
    const c = storage.get<Record<string, unknown>>("aij.consents.v1");
    if (c && c.synced) storage.set("aij.consents.v1", { ...c, synced: {} });
  } catch {
    /* storage blocked: nothing persisted */
  }
}

export interface Me {
  id: string;
  email: string | null;
  display_name: string | null;
  role: "user" | "creator" | "admin";
  plan: "free" | "pro" | "max";
  referral_code?: string;
  mfa_enrolled?: boolean;
  status?: string;
  [k: string]: unknown;
}

const meStore = createStore<Me | null>(null);
let meInflight: Promise<Me | null> | null = null;
let meLoader: (() => Promise<Me | null>) | null = null;

/** Wired by main.ts (avoids an import cycle with api.ts/auth.ts). */
export function setMeLoader(fn: () => Promise<Me | null>): void {
  meLoader = fn;
}

export function peekMe(): Me | null {
  return meStore.get();
}

export function getMe(force = false): Promise<Me | null> {
  if (!force && meStore.get()) return Promise.resolve(meStore.get());
  if (meInflight && !force) return meInflight;
  if (!meLoader) return Promise.resolve(null);
  const p = meLoader()
    .then((me) => {
      meStore.set(me);
      return me;
    })
    .finally(() => {
      if (meInflight === p) meInflight = null;
    });
  meInflight = p;
  return p;
}

export function clearMe(): void {
  meStore.set(null);
}

export function onMeChange(cb: (me: Me | null) => void): () => void {
  return meStore.subscribe(cb);
}

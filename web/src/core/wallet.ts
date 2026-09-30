// EIP-1193 wallets with EIP-6963 multi-wallet discovery. The user picks which injected wallet to use.
// Signing: eth_signTypedData_v4 (Hyperliquid user-signed actions) and personal_sign (ownership proof, EIP-4361 text).

import { api, ApiError } from "./api.js";
import { isAddress, toChecksumAddress } from "./keccak.js";
import { emptyState, h, modal, note } from "./ui.js";

export { toChecksumAddress } from "./keccak.js";

export interface Eip1193Provider {
  request(args: { method: string; params?: unknown[] | Record<string, unknown> }): Promise<unknown>;
  on?(event: string, cb: (...args: any[]) => void): void;
  removeListener?(event: string, cb: (...args: any[]) => void): void;
}

export interface WalletInfo {
  uuid: string;
  name: string;
  icon: string;
  rdns: string;
}

export interface TypedData {
  types: Record<string, { name: string; type: string }[]>;
  primaryType: string;
  domain: { name: string; version: string; chainId: number; verifyingContract: string };
  message: Record<string, unknown>;
}

interface Announced {
  info: WalletInfo;
  provider: Eip1193Provider;
}

const announced = new Map<string, Announced>();
let listening = false;

function sanitizeInfo(raw: unknown): WalletInfo | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const name = typeof r.name === "string" ? r.name.slice(0, 40) : "";
  const uuid = typeof r.uuid === "string" ? r.uuid.slice(0, 64) : "";
  if (!name || !uuid) return null;
  const icon = typeof r.icon === "string" && /^data:image\/(png|svg\+xml|webp|jpeg|gif)[;,]/i.test(r.icon) && r.icon.length < 200_000 ? r.icon : "";
  const rdns = typeof r.rdns === "string" ? r.rdns.slice(0, 100) : "";
  return { uuid, name, icon, rdns };
}

function listen(): void {
  if (listening) return;
  listening = true;
  window.addEventListener("eip6963:announceProvider", ((e: CustomEvent) => {
    const info = sanitizeInfo(e.detail?.info);
    const provider = e.detail?.provider as Eip1193Provider | undefined;
    if (!info || !provider || typeof provider.request !== "function") return;
    announced.set(info.uuid, { info, provider });
  }) as EventListener);
}

/** EIP-6963 discovery plus the legacy window.ethereum (only when nothing announced itself). */
export async function discoverWallets(waitMs = 350): Promise<Announced[]> {
  listen();
  window.dispatchEvent(new Event("eip6963:requestProvider"));
  await new Promise((r) => window.setTimeout(r, waitMs));
  const list = [...announced.values()];
  const legacy = (window as unknown as { ethereum?: Eip1193Provider & { isMetaMask?: boolean } }).ethereum;
  if (!list.length && legacy && typeof legacy.request === "function") {
    list.push({ info: { uuid: "legacy", name: legacy.isMetaMask ? "MetaMask" : "Browser wallet", icon: "", rdns: "" }, provider: legacy });
  }
  return list;
}

function utf8ToHex(s: string): string {
  return "0x" + Array.from(new TextEncoder().encode(s), (b) => b.toString(16).padStart(2, "0")).join("");
}

function walletError(err: unknown): ApiError {
  const e = err as { code?: number; message?: string };
  if (e?.code === 4001) return new ApiError(0, "wallet_rejected", "You rejected the request in your wallet.");
  if (e?.code === 4100) return new ApiError(0, "wallet_unauthorized", "The wallet hasn't authorised this site. Connect again.");
  if (e?.code === -32002) return new ApiError(0, "wallet_pending", "Your wallet already has a pending request. Open the wallet to continue.");
  if (e?.code === 4902) return new ApiError(0, "wallet_chain", "That network isn't added to your wallet.");
  return new ApiError(0, "wallet_error", e?.message ? `Wallet error: ${String(e.message).slice(0, 200)}` : "Wallet error.");
}

export class Wallet {
  info: WalletInfo;
  provider: Eip1193Provider;
  address: string;
  private subs: (() => void)[] = [];

  constructor(a: Announced, address: string) {
    this.info = a.info;
    this.provider = a.provider;
    this.address = address.toLowerCase();
    const onAcc = (accs: unknown) => {
      const first = Array.isArray(accs) && typeof accs[0] === "string" ? (accs[0] as string).toLowerCase() : "";
      if (first && isAddress(first)) this.address = first;
    };
    this.provider.on?.("accountsChanged", onAcc);
    this.subs.push(() => this.provider.removeListener?.("accountsChanged", onAcc));
  }

  get checksumAddress(): string {
    return toChecksumAddress(this.address);
  }

  async chainId(): Promise<number> {
    const raw = await this.provider.request({ method: "eth_chainId" }).catch((e) => {
      throw walletError(e);
    });
    const n = typeof raw === "string" ? parseInt(raw, 16) : Number(raw);
    if (!Number.isSafeInteger(n) || n <= 0) throw new ApiError(0, "wallet_chain", "Couldn't read the wallet's network.");
    return n;
  }

  async chainIdHex(): Promise<`0x${string}`> {
    return `0x${(await this.chainId()).toString(16)}`;
  }

  /** Ensures the wallet is still on `this.address` (user may have switched accounts). */
  async assertAccount(): Promise<void> {
    const accs = (await this.provider.request({ method: "eth_accounts" }).catch((e) => {
      throw walletError(e);
    })) as unknown[];
    const cur = typeof accs?.[0] === "string" ? (accs[0] as string).toLowerCase() : "";
    if (cur !== this.address) throw new ApiError(0, "wallet_account_changed", "Your wallet switched to a different account. Switch back or reconnect.");
  }

  async signTypedDataV4(typed: TypedData): Promise<`0x${string}`> {
    await this.assertAccount();
    const chainId = await this.chainId();
    if (typed.domain.chainId !== chainId) throw new ApiError(0, "wallet_chain_mismatch", "Your wallet changed network. Please try again.");
    let sig: unknown;
    try {
      sig = await this.provider.request({ method: "eth_signTypedData_v4", params: [this.address, JSON.stringify(typed)] });
    } catch (e) {
      throw walletError(e);
    }
    if (typeof sig !== "string" || !/^0x[0-9a-fA-F]{130}$/.test(sig)) throw new ApiError(0, "wallet_bad_signature", "The wallet returned an invalid signature.");
    return sig as `0x${string}`;
  }

  async personalSign(message: string): Promise<`0x${string}`> {
    await this.assertAccount();
    let sig: unknown;
    try {
      sig = await this.provider.request({ method: "personal_sign", params: [utf8ToHex(message), this.address] });
    } catch (e) {
      throw walletError(e);
    }
    if (typeof sig !== "string" || !/^0x[0-9a-fA-F]{130}$/.test(sig)) throw new ApiError(0, "wallet_bad_signature", "The wallet returned an invalid signature.");
    return sig as `0x${string}`;
  }

  onAccountsChanged(cb: (address: string | null) => void): () => void {
    const f = (accs: unknown) => cb(Array.isArray(accs) && typeof accs[0] === "string" ? (accs[0] as string).toLowerCase() : null);
    this.provider.on?.("accountsChanged", f);
    const off = () => this.provider.removeListener?.("accountsChanged", f);
    this.subs.push(off);
    return off;
  }

  onChainChanged(cb: (chainId: number) => void): () => void {
    const f = (c: unknown) => cb(typeof c === "string" ? parseInt(c, 16) : Number(c));
    this.provider.on?.("chainChanged", f);
    const off = () => this.provider.removeListener?.("chainChanged", f);
    this.subs.push(off);
    return off;
  }

  disconnect(): void {
    this.subs.forEach((f) => f());
    this.subs = [];
    if (connected === this) connected = null;
  }
}

let connected: Wallet | null = null;

export function getConnectedWallet(): Wallet | null {
  return connected;
}

function pickDialog(list: Announced[]): Promise<Announced | null> {
  return new Promise((resolve) => {
    let chosen: Announced | null = null;
    const m = modal({
      title: "Choose a wallet",
      body: h("div", { class: "stack" },
        h("p", { class: "muted small" }, "Use the wallet that controls your Hyperliquid account."),
        h("div", { class: "wallet-list" }, list.map((w) =>
          h("button", {
            type: "button", class: "wallet-opt",
            onclick: () => { chosen = w; m.close(); },
          }, w.info.icon ? h("img", { src: w.info.icon, alt: "", width: 28, height: 28 }) : h("span", { class: "wallet-ph", "aria-hidden": "true" }), h("span", null, w.info.name))))),
      actions: [{ label: "Cancel", kind: "plain" }],
    });
    m.closed.then(() => resolve(chosen));
  });
}

/** Discovers wallets, lets the user pick one (if several), requests accounts. Null when cancelled / none. */
export async function connectWallet(): Promise<Wallet | null> {
  const list = await discoverWallets();
  if (!list.length) {
    const m = modal({
      title: "No wallet found",
      body: h("div", { class: "stack" },
        emptyState("No browser wallet detected", "Install or unlock a wallet extension (for example Rabby or MetaMask) that holds your Hyperliquid account, then reload this page."),
        note("Never paste your seed phrase or private key into any website. aijalon.trade will never ask for it.", "warn")),
      actions: [{ label: "OK", kind: "primary" }],
    });
    await m.closed;
    return null;
  }
  const pick = list.length === 1 ? list[0]! : await pickDialog(list);
  if (!pick) return null;
  let accs: unknown;
  try {
    accs = await pick.provider.request({ method: "eth_requestAccounts" });
  } catch (e) {
    throw walletError(e);
  }
  const first = Array.isArray(accs) && typeof accs[0] === "string" ? accs[0] : "";
  if (!isAddress(first)) throw new ApiError(0, "wallet_no_account", "The wallet didn't share an account.");
  connected?.disconnect();
  connected = new Wallet(pick, first);
  return connected;
}

/** EIP-4361 ("Sign-In with Ethereum") text. The server must parse and check domain, address, nonce, time. */
export function buildOwnershipMessage(p: { address: string; nonce: string; issuedAt: string; chainId: number; domain?: string; uri?: string; expirationTime?: string }): string {
  const domain = p.domain ?? location.host;
  const uri = p.uri ?? location.origin;
  if (!/^[A-Za-z0-9]{8,64}$/.test(p.nonce)) throw new ApiError(0, "bad_nonce", "Invalid nonce from server.");
  const lines = [
    `${domain} wants you to sign in with your Ethereum account:`,
    toChecksumAddress(p.address),
    "",
    "Link this wallet to your aijalon.trade account. This signature proves you own the wallet. It does not authorize any transaction, transfer or trade.",
    "",
    `URI: ${uri}`,
    "Version: 1",
    `Chain ID: ${p.chainId}`,
    `Nonce: ${p.nonce}`,
    `Issued At: ${p.issuedAt}`,
  ];
  if (p.expirationTime) lines.push(`Expiration Time: ${p.expirationTime}`);
  return lines.join("\n");
}

/** Wallet ownership proof: server nonce → personal_sign(SIWE text) → POST /v1/wallets/verify. */
export async function proveOwnership(wallet: Wallet): Promise<{ address: string }> {
  const { nonce } = await api.post<{ nonce: string; expires_at: string }>("/wallets/nonce");
  const issuedAt = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const expirationTime = new Date(Date.now() + 10 * 60_000).toISOString().replace(/\.\d{3}Z$/, "Z");
  const message = buildOwnershipMessage({ address: wallet.address, nonce, issuedAt, expirationTime, chainId: await wallet.chainId() });
  const signature = await wallet.personalSign(message);
  await api.post("/wallets/verify", { address: wallet.address, message, signature });
  return { address: wallet.address };
}

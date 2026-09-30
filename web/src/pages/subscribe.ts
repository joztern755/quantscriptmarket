// #/subscribe/:slug — resumable subscribe wizard (SPEC §9):
//  1 sign-in + MFA · 2 subscribe gate (acks + fees + T&C) · 3 connect wallet + verify ownership ·
//  4 trading account (master or sub-account) · 5 approve agent · 6 approve builder fee ·
//  7 allocation + max leverage · 8 fee balance check · 9 confirm (step-up) → POST /v1/subscriptions.
// Progress is kept per user+strategy in localStorage (no secrets: addresses and server ids only) and every
// on-chain/server step is re-checkable, so the wizard can be resumed after a reload or a top-up.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, kv, button, toast, checkbox, field, badge, type Child } from "../core/ui.js";
import { api, publicConfig, newIdempotencyKey, type PublicConfig } from "../core/api.js";
import { storage } from "../core/state.js";
import { ensureMfaEnrolled } from "../core/auth.js";
import { subscribeGate, feeSummary } from "../core/gate.js";
import { connectWallet, getConnectedWallet, proveOwnership, type Wallet } from "../core/wallet.js";
import { approveAgent, approveBuilderFee, hlInfo } from "../core/hl.js";
import { fmtUsd, fmtBps, fmtTenthsBp, shortAddr, microToDecimal } from "../core/format.js";
import type { StrategyDetail, Subscription, Balance, AgentOut, AgentCreateOut } from "./_shared/types.js";
import { ApiError } from "../core/api.js";
import { ensurePageCss, isAbortError, errCode, errMessage, listOf, isAddress, hlNum, usdInput, LOSS_WARNING, isRec, feesList } from "./_shared/util.js";
import { marketMaxLeverage, leverageBound, leverageHint, highLeverageWarning } from "./_shared/leverage.js";
import { requireAlertContacts } from "./_shared/contacts.js";

/** `details.reason` of an API error (backend Conflict/Forbidden reasons, docs/API_CONTRACT.md). */
function errReason(err: unknown): string {
  return err instanceof ApiError && typeof err.details.reason === "string" ? err.details.reason : "";
}

export const title = "Subscribe";

interface WizState {
  v: 1;
  savedAt: number;
  gateAccepted?: boolean;
  master?: string;
  trading?: string;
  tradingLabel?: string;
  agentId?: string;
  agentAddress?: string;
  agentTypedData?: unknown;
  agentDone?: boolean;
  builderDone?: boolean;
  allocation?: string;
  leverage?: number;
  riskAck?: boolean;
  subKey?: string;
}

const STEP_TITLES = [
  "Sign in & two-factor check",
  "Risks, fees and terms",
  "Connect & verify your wallet",
  "Choose the trading account",
  "Approve the trade-only agent",
  "Approve the builder fee",
  "Allocation & max leverage",
  "Fee balance",
  "Confirm",
];

const MAX_AGE_MS = 7 * 24 * 3600 * 1000;

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const slug = ctx.params.slug ?? "";
  if (!/^[a-z0-9-]{1,64}$/.test(slug)) {
    mount(root, emptyState("Strategy not found", undefined, h("a", { class: "btn", href: "#/market" }, "Marketplace")));
    return;
  }
  mount(root, skeleton(10));
  let s: StrategyDetail;
  let cfg: PublicConfig;
  let existing: Subscription[] = [];
  try {
    [s, cfg, existing] = await Promise.all([
      api.get<StrategyDetail>(`/public/strategies/${encodeURIComponent(slug)}`, { signal: ctx.signal }),
      publicConfig(),
      api.get<unknown>("/subscriptions?limit=100", { signal: ctx.signal }).then((r) => listOf<Subscription>(r)),
    ]);
  } catch (err) {
    if (isAbortError(err) || !ctx.isCurrent()) return;
    mount(root, errorState(err, () => void render(root, ctx)));
    return;
  }
  if (!ctx.isCurrent()) return;
  ctx.setTitle(`Subscribe · ${s.name}`);
  new Wizard(root, ctx, s, cfg, existing).start();
}

class Wizard {
  private key: string;
  private st: WizState;
  private wallet: Wallet | null = getConnectedWallet();
  private editing: number | null = null;
  private balance: Balance | null = null;
  private balanceErr: unknown = null;
  private balanceOk = false;
  private tradingAccounts: { address: string; label: string; value: number }[] | null = null;
  /** Lowest Hyperliquid max leverage across the strategy's markets (undefined = loading, null = unknown). */
  private marketMaxLev: number | null | undefined = undefined;
  private box = h("div", { class: "wizard" });
  private status = h("p", { class: "status", "aria-live": "polite" });

  constructor(
    private root: HTMLElement,
    private ctx: PageContext,
    private s: StrategyDetail,
    private cfg: PublicConfig,
    private existing: Subscription[],
  ) {
    this.key = `aijalon.subwiz.${ctx.user?.uid ?? "anon"}.${s.slug}`;
    const saved = storage.get<WizState>(this.key);
    this.st = saved && saved.v === 1 && Date.now() - (saved.savedAt ?? 0) < MAX_AGE_MS ? saved : { v: 1, savedAt: Date.now() };
    if (this.st.master && !isAddress(this.st.master)) this.st = { v: 1, savedAt: Date.now() };
  }

  start(): void {
    const s = this.s;
    const active = this.existing.filter((x) => x.status !== "cancelled" && (x.strategy_slug === s.slug || x.strategy_id === s.id));
    mount(
      this.root,
      h(
        "div",
        { class: "stack" },
        h(
          "div",
          { class: "stack tight page-head" },
          h("a", { href: `#/s/${encodeURIComponent(s.slug)}`, class: "small" }, `← ${s.name}`),
          h("div", { class: "eyebrow" }, "Subscribe"),
          h("h1", { class: "page-title" }, s.name),
          h("p", { class: "muted" }, "Nine short steps. Your progress is saved on this device — you can leave (for example to top up) and come back."),
        ),
        active.length
          ? note(
              h("span", null, `You already have ${active.length === 1 ? "a subscription" : "subscriptions"} to this strategy (`, active.map((a) => shortAddr(a.trading_address)).join(", "), "). You can add another trading account below, or ", h("a", { href: "#/dashboard" }, "manage it in your dashboard"), "."),
              "info",
            )
          : null,
        s.status !== "listed" ? note("This strategy is not accepting new subscribers right now.", "bad") : null,
        s.showcase_text ? note(s.showcase_text, "info") : null,
        this.box,
        this.status,
        h("div", { class: "row small" }, button("Start over", { kind: "ghost", onClick: () => this.reset() })),
      ),
    );
    if (this.wallet) {
      const off1 = this.wallet.onAccountsChanged(() => {
        this.wallet = getConnectedWallet();
        this.draw();
      });
      this.ctx.onCleanup(off1);
    }
    this.draw();
    void marketMaxLeverage(s.markets).then((v) => {
      this.marketMaxLev = v;
      if (this.ctx.isCurrent() && this.current() === 6) this.draw();
    });
  }

  private save(): void {
    this.st.savedAt = Date.now();
    storage.set(this.key, this.st);
  }

  private reset(): void {
    storage.remove(this.key);
    this.st = { v: 1, savedAt: Date.now() };
    this.editing = null;
    this.balanceOk = false;
    this.tradingAccounts = null;
    this.draw();
  }

  private say(msg: string, kind: "" | "ok" | "err" = ""): void {
    this.status.textContent = msg;
    this.status.className = `status ${kind}`;
  }

  // ------------------------------------------------------------------ step completion
  private done(i: number): boolean {
    const st = this.st;
    switch (i) {
      case 0:
        return !!this.ctx.user && this.ctx.user.mfaSatisfied !== false;
      case 1:
        return !!st.gateAccepted;
      case 2:
        return !!st.master;
      case 3:
        return !!st.trading;
      case 4:
        return !!st.agentDone;
      case 5:
        return !!st.builderDone;
      case 6:
        return !!st.allocation && !!st.leverage && !!st.riskAck && this.allocationMicro() !== null;
      case 7:
        return this.balanceOk;
      default:
        return false;
    }
  }

  private current(): number {
    for (let i = 0; i < STEP_TITLES.length; i++) if (!this.done(i)) return i;
    return STEP_TITLES.length - 1;
  }

  private summary(i: number): string {
    const st = this.st;
    switch (i) {
      case 0:
        return this.ctx.user?.email ?? "Signed in";
      case 1:
        return st.gateAccepted ? "Accepted" : "";
      case 2:
        return st.master ? shortAddr(st.master) : "";
      case 3:
        return st.trading ? `${st.tradingLabel ?? "Account"} · ${shortAddr(st.trading)}` : "";
      case 4:
        return st.agentDone ? `Agent ${shortAddr(st.agentAddress ?? "")} active` : st.agentId ? "Waiting for approval" : "";
      case 5:
        return st.builderDone ? `${fmtTenthsBp(this.cfg.economics.builder_fee_tenths_bp)} approved` : "";
      case 6: {
        const m = this.allocationMicro();
        return m !== null && st.leverage ? `${fmtUsd(m)} · max ${st.leverage}×` : "";
      }
      case 7:
        return this.balance ? `Balance ${fmtUsd(this.balance.fee_balance_micro)}` : "";
      default:
        return "";
    }
  }

  private allocationMicro(): number | null {
    const raw = (this.st.allocation ?? "").replace(/[$,\s]/g, "");
    if (!/^\d{1,12}(\.\d{1,6})?$/.test(raw)) return null;
    const [i, f = ""] = raw.split(".");
    const m = Number(i) * 1_000_000 + Number((f + "000000").slice(0, 6));
    return m > 0 && Number.isSafeInteger(m) ? m : null;
  }

  /** min(strategy MAX_LEVERAGE, Hyperliquid market max leverage) — no platform or launch cap (owner decision). The
   *  backend re-checks (422 details.max_x100). */
  private maxLeverage(): number {
    return leverageBound(this.s.max_leverage, this.marketMaxLev, this.cfg.platform_max_leverage);
  }

  private price(): number {
    return this.s.price_monthly_micro ?? 0;
  }

  /** Backend reserve at subscribe: min top-up whenever profit share can accrue (creator % or platform 1.5% > 0). */
  private reserve(): number {
    const e = this.cfg.economics;
    return this.s.profit_share_bps > 0 || e.platform_profit_share_bps > 0 ? e.min_topup_micro : 0;
  }

  // ------------------------------------------------------------------ rendering
  draw(): void {
    if (!this.ctx.isCurrent()) return;
    const cur = this.editing ?? this.current();
    mount(
      this.box,
      ...STEP_TITLES.map((t, i) => {
        const isDone = this.done(i) && i !== cur;
        const isCur = i === cur;
        const locked = !isCur && !isDone && i > this.current();
        return h(
          "section",
          { class: ["wstep", isDone && "done", isCur && "current", locked && "locked"], "aria-current": isCur ? "step" : null, "aria-labelledby": `ws-${i}` },
          h(
            "div",
            { class: "whead" },
            h("span", { class: "num", "aria-hidden": "true" }, isDone ? "✓" : String(i + 1)),
            h("div", { class: "t" }, h("div", { id: `ws-${i}` }, t), !isCur && this.summary(i) ? h("div", { class: "s" }, this.summary(i)) : null),
            isDone && i > 0 && i < 8 ? h("button", { class: "btn ghost sm", type: "button", onclick: () => { this.editing = i; this.draw(); } }, "Change") : null,
          ),
          isCur ? h("div", { class: "wbody" }, this.body(i)) : null,
        );
      }),
    );
  }

  private next(): void {
    this.editing = null;
    this.save();
    this.say("");
    this.draw();
    const el = this.box.querySelector(".wstep.current");
    if (el instanceof HTMLElement) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  private body(i: number): Child {
    switch (i) {
      case 0:
        return this.stepAuth();
      case 1:
        return this.stepGate();
      case 2:
        return this.stepWallet();
      case 3:
        return this.stepAccount();
      case 4:
        return this.stepAgent();
      case 5:
        return this.stepBuilder();
      case 6:
        return this.stepAllocation();
      case 7:
        return this.stepBalance();
      default:
        return this.stepConfirm();
    }
  }

  private stepAuth(): Child {
    const u = this.ctx.user;
    if (!u) return h("a", { class: "btn primary", href: `#/signin?next=${encodeURIComponent(this.ctx.path)}` }, "Sign in");
    return [
      h("p", null, "Signed in as ", h("b", null, u.email ?? u.displayName ?? "you"), "."),
      u.mfaSatisfied ? h("p", { class: "small muted" }, "Two-factor authentication is active.") : note("Two-factor authentication (TOTP) is required before you can subscribe.", "warn"),
      h(
        "div",
        { class: "btns" },
        button("Set up / verify two-factor", {
          kind: "primary",
          onClick: async () => {
            if (await ensureMfaEnrolled()) this.next();
          },
        }),
      ),
    ];
  }

  private stepGate(): Child {
    const e = this.cfg.economics;
    const rows = feeSummary(this.cfg, { price_monthly_micro: this.price(), profit_share_bps: this.s.profit_share_bps });
    return [
      h("p", null, "Before connecting anything, review the strategy's specific risks and every fee you'll pay. You'll accept the Terms and Risk Disclosure again for this subscription."),
      feesList(rows),
      h(
        "p",
        { class: "small muted" },
        `Builder fee ${fmtTenthsBp(e.builder_fee_tenths_bp)} of each order's notional (collected by Hyperliquid) · subscription ${this.price() > 0 ? fmtUsd(this.price()) + "/month" : "free"} · profit share ${fmtBps(this.s.profit_share_bps)} creator` +
          (e.platform_profit_share_mode === "on_top" ? ` + ${fmtBps(e.platform_profit_share_bps)} platform = ${fmtBps(this.s.profit_share_bps + e.platform_profit_share_bps)} of new profit above your high-water mark.` : ` (platform's ${fmtBps(e.platform_profit_share_bps)} is included).`),
      ),
      h(
        "div",
        { class: "btns" },
        button("Review and accept", {
          kind: "primary",
          onClick: async () => {
            const ok = await subscribeGate({
              strategy: {
                id: this.s.id,
                slug: this.s.slug,
                name: this.s.name,
                price_monthly_micro: this.price(),
                profit_share_bps: this.s.profit_share_bps,
                markets: this.s.markets,
                risk_ack_text: this.s.risk_ack_text,
              },
              allocationMicro: this.allocationMicro() ?? undefined,
            });
            if (!ok) return;
            this.st.gateAccepted = true;
            this.next();
          },
        }),
        h("a", { class: "btn", href: "#/legal/terms", target: "_blank", rel: "noopener" }, "Terms"),
        h("a", { class: "btn", href: "#/legal/risk-disclosure", target: "_blank", rel: "noopener" }, "Risk disclosure"),
      ),
    ];
  }

  private walletLine(): Child {
    const w = this.wallet;
    if (!w) return h("p", { class: "small muted" }, "No wallet connected in this browser session.");
    const mismatch = this.st.master && w.address.toLowerCase() !== this.st.master;
    return h(
      "p",
      { class: ["small", mismatch ? "neg" : "muted"] },
      `Connected: ${w.info.name} · ${shortAddr(w.address)}`,
      mismatch ? ` — this is not your verified wallet ${shortAddr(this.st.master ?? "")}. Switch accounts in your wallet.` : "",
    );
  }

  /** Returns the connected wallet if it matches the verified master address; otherwise prompts to (re)connect. */
  private async requireWallet(): Promise<Wallet | null> {
    let w = getConnectedWallet() ?? this.wallet;
    if (!w) w = await connectWallet();
    if (!w) return null;
    this.wallet = w;
    if (this.st.master && w.address.toLowerCase() !== this.st.master) {
      this.say(`Connected wallet ${shortAddr(w.address)} is not your verified wallet ${shortAddr(this.st.master)}. Switch accounts in your wallet and try again.`, "err");
      this.draw();
      return null;
    }
    return w;
  }

  private stepWallet(): Child {
    return [
      h("p", null, "Connect the wallet that owns your Hyperliquid account, then sign a message to prove you control it. Signing a message costs nothing and moves no funds."),
      this.walletLine(),
      h(
        "div",
        { class: "btns" },
        button(this.wallet ? "Verify this wallet" : "Connect wallet", {
          kind: "primary",
          onClick: async () => {
            const w = getConnectedWallet() ?? (await connectWallet());
            if (!w) return;
            this.wallet = w;
            this.say("Check your wallet to sign the verification message…");
            const res = await proveOwnership(w);
            const addr = (res.address || w.address).toLowerCase();
            if (this.st.master && this.st.master !== addr) {
              // a different wallet invalidates everything that depended on the old one
              this.st = { v: 1, savedAt: Date.now(), gateAccepted: this.st.gateAccepted, allocation: this.st.allocation, leverage: this.st.leverage };
            }
            this.st.master = addr;
            this.tradingAccounts = null;
            this.say("Wallet verified.", "ok");
            this.next();
          },
        }),
        this.wallet ? button("Use a different wallet", { kind: "ghost", onClick: async () => { this.wallet?.disconnect(); this.wallet = await connectWallet(); this.draw(); } }) : null,
      ),
    ];
  }

  private stepAccount(): Child {
    const master = this.st.master ?? "";
    const listBox = h("div", { class: "acct-list" }, skeleton(3));
    const used = new Map(this.existing.filter((x) => x.status !== "cancelled").map((x) => [x.trading_address.toLowerCase(), x.strategy_name ?? x.strategy_slug ?? "another strategy"]));
    const drawList = (): void => {
      const accts = this.tradingAccounts ?? [{ address: master, label: "Master account", value: NaN }];
      mount(
        listBox,
        ...accts.map((a) => {
          const inUse = used.get(a.address.toLowerCase());
          const input = h("input", { type: "radio", name: "acct", value: a.address, disabled: !!inUse, checked: this.st.trading === a.address && !inUse });
          return h(
            "label",
            { class: ["acct", inUse && "disabled"] },
            input,
            h(
              "div",
              { class: "stack tight" },
              h("b", null, a.label),
              h("span", { class: "addr" }, a.address),
              h("span", { class: "small muted" }, Number.isFinite(a.value) ? `Account value ${fmtUsd(Math.floor(a.value * 1e6))}` : "Account value unavailable", inUse ? ` · already runs ${inUse}` : ""),
            ),
          );
        }),
      );
    };
    if (!this.tradingAccounts) {
      void this.loadAccounts(master).then(() => {
        if (this.ctx.isCurrent()) drawList();
      });
    } else drawList();
    return [
      h("p", null, "Choose which Hyperliquid account this strategy trades: your master account or one of its sub-accounts."),
      note(
        "One strategy per trading account. Each account can run only one strategy at a time so positions never collide. To run several strategies, create sub-accounts in Hyperliquid (Portfolio → Sub-accounts), move funds there, and subscribe each one separately.",
        "info",
      ),
      listBox,
      h(
        "div",
        { class: "btns" },
        button("Use selected account", {
          kind: "primary",
          onClick: () => {
            const sel = listBox.querySelector<HTMLInputElement>('input[name="acct"]:checked');
            if (!sel || !isAddress(sel.value)) {
              toast("Choose an account.", "warn");
              return;
            }
            const acct = (this.tradingAccounts ?? []).find((a) => a.address === sel.value);
            this.st.trading = sel.value.toLowerCase();
            this.st.tradingLabel = acct?.label ?? "Account";
            this.next();
          },
        }),
        button("Refresh", { kind: "ghost", onClick: async () => { this.tradingAccounts = null; await this.loadAccounts(master); drawList(); } }),
      ),
    ];
  }

  private async loadAccounts(master: string): Promise<void> {
    const out: { address: string; label: string; value: number }[] = [];
    let masterValue = NaN;
    try {
      const ch = await hlInfo<unknown>({ type: "clearinghouseState", user: master });
      masterValue = accountValue(ch);
    } catch {
      /* show unavailable */
    }
    out.push({ address: master, label: "Master account", value: masterValue });
    try {
      const subs = await hlInfo<unknown>({ type: "subAccounts", user: master });
      if (Array.isArray(subs)) {
        for (const sa of subs) {
          if (!isRec(sa)) continue;
          const addr = typeof sa.subAccountUser === "string" ? sa.subAccountUser.toLowerCase() : "";
          if (!isAddress(addr)) continue;
          const name = typeof sa.name === "string" ? sa.name.slice(0, 40) : "Sub-account";
          out.push({ address: addr, label: `Sub-account “${name}”`, value: accountValue(sa.clearinghouseState) });
        }
      }
    } catch {
      /* master only */
    }
    this.tradingAccounts = out;
  }

  private stepAgent(): Child {
    const st = this.st;
    const confirm = async (): Promise<void> => {
      if (!st.agentId) return;
      this.say("Checking Hyperliquid for your approval…");
      try {
        await api.post<AgentOut>(`/agents/${encodeURIComponent(st.agentId)}/confirm`, {}, { signal: this.ctx.signal });
      } catch (err) {
        const c = errCode(err);
        if (c === "not_found" || c === "conflict") {
          // server discarded / replaced the pending agent — start again
          st.agentId = undefined;
          st.agentAddress = undefined;
          st.agentTypedData = undefined;
          this.save();
          this.draw();
        }
        this.say(`Not confirmed yet: ${errMessage(err)} If you just signed, wait a few seconds and check again.`, "err");
        return;
      }
      st.agentDone = true;
      this.say("Agent approved.", "ok");
      this.next();
    };
    /** One live agent per master wallet: reuse an ACTIVE one (e.g. a second strategy on a sub-account). */
    const useActive = async (): Promise<boolean> => {
      const agents = listOf<AgentOut>(await api.get<unknown>("/agents", { signal: this.ctx.signal }));
      const active = agents.find((a) => a.status === "active" && a.master_address.toLowerCase() === st.master);
      if (!active) return false;
      st.agentId = active.id;
      st.agentAddress = active.agent_address.toLowerCase();
      st.agentDone = true;
      this.say("Your agent for this wallet is already approved.", "ok");
      this.next();
      return true;
    };
    const sign = async (): Promise<void> => {
      const w = await this.requireWallet();
      if (!w) return;
      if (!st.agentId || !st.agentAddress) {
        if (await useActive()) return;
        let res: AgentCreateOut;
        try {
          res = await api.post<AgentCreateOut>("/agents", { master_address: st.master, signature_chain_id: await w.chainIdHex() }, { signal: this.ctx.signal });
        } catch (err) {
          if (errCode(err) === "conflict" && (await useActive())) return;
          throw err;
        }
        const id = res.agent?.id ?? "";
        const addr = String(res.agent?.agent_address ?? "").toLowerCase();
        if (!id || !isAddress(addr)) throw new Error("The server returned an invalid agent. Please try again.");
        st.agentId = id;
        st.agentAddress = addr;
        st.agentTypedData = res.approve_agent?.typed_data;
        this.save();
        this.draw();
      }
      this.say("Check your wallet: approve the agent (Hyperliquid ApproveAgent)…");
      const r = await approveAgent(w, { agentAddress: st.agentAddress as string, serverTypedData: st.agentTypedData });
      if (!r.ok) {
        this.say(`Hyperliquid rejected the approval: ${r.error ?? "unknown error"}`, "err");
        return;
      }
      await confirm();
    };
    return [
      h(
        "p",
        null,
        "We create a dedicated agent wallet for you. You approve it with your wallet on Hyperliquid. ",
        h("b", null, "An agent can place and cancel orders but can never withdraw or transfer your funds."),
        " You can revoke it any time in Hyperliquid's API settings (all strategy trading stops).",
      ),
      st.agentAddress ? kv([["Agent address", h("span", { class: "mono break" }, st.agentAddress)], ["Agent name", this.cfg.agent_name]]) : null,
      this.walletLine(),
      h(
        "div",
        { class: "btns" },
        button(st.agentId ? "Sign approval in wallet again" : "Create agent & approve in wallet", { kind: "primary", onClick: sign }),
        st.agentId ? button("I've approved it — check again", { onClick: confirm }) : null,
      ),
      h("p", { class: "small muted" }, "Creating the agent needs a fresh sign-in (step-up). Your wallet signs an EIP-712 message; no gas is paid."),
    ];
  }

  private stepBuilder(): Child {
    const e = this.cfg.economics;
    const st = this.st;
    const pre = h("p", { class: "small muted" });
    void (async () => {
      if (!this.cfg.builder_address || !st.master) return;
      try {
        const cur = await hlInfo<unknown>({ type: "maxBuilderFee", user: st.master, builder: this.cfg.builder_address });
        const n = typeof cur === "number" ? cur : Number(cur);
        if (Number.isFinite(n) && n >= e.builder_fee_tenths_bp && this.ctx.isCurrent()) pre.textContent = "Hyperliquid shows this builder fee is already approved for your account — press Confirm to record it.";
      } catch {
        /* ignore */
      }
    })();
    const confirm = async (): Promise<void> => {
      this.say("Checking Hyperliquid for your builder-fee approval…");
      try {
        const r = await api.post<{ sufficient: boolean; max_fee_rate_tenths_bp: number; required_tenths_bp: number }>("/builder-approval/confirm", { master_address: st.master }, { signal: this.ctx.signal });
        if (!r.sufficient) {
          this.say(`Hyperliquid shows a builder-fee approval of ${fmtTenthsBp(r.max_fee_rate_tenths_bp)}; ${fmtTenthsBp(r.required_tenths_bp)} is required. Approve in your wallet, then confirm again.`, "err");
          return;
        }
      } catch (err) {
        this.say(`Not confirmed yet: ${errMessage(err)}`, "err");
        return;
      }
      st.builderDone = true;
      this.say("Builder fee approved.", "ok");
      this.next();
    };
    return [
      h(
        "p",
        null,
        `Every strategy order carries our builder code. Hyperliquid collects a ${fmtTenthsBp(e.builder_fee_tenths_bp)} fee on the order's notional from your trading account. Approving sets the maximum we are allowed to charge; you can revoke it on Hyperliquid at any time (strategy trading then stops).`,
      ),
      kv([
        ["Max builder fee", fmtTenthsBp(e.builder_fee_tenths_bp)],
        ["Builder address", h("span", { class: "mono break" }, this.cfg.builder_address || "—")],
        ["Example", `${fmtUsd(10_000_000_000)} order → ${fmtUsd(Math.floor((10_000_000_000 * e.builder_fee_tenths_bp) / 100_000))} fee`],
      ]),
      pre,
      this.walletLine(),
      !this.cfg.builder_address ? note("Builder address is not configured yet — subscriptions are temporarily unavailable.", "bad") : null,
      h(
        "div",
        { class: "btns" },
        button("Approve in wallet", {
          kind: "primary",
          disabled: !this.cfg.builder_address,
          onClick: async () => {
            const w = await this.requireWallet();
            if (!w) return;
            this.say("Check your wallet: approve the builder fee (Hyperliquid ApproveBuilderFee)…");
            const r = await approveBuilderFee(w);
            if (!r.ok) {
              this.say(`Hyperliquid rejected the approval: ${r.error ?? "unknown error"}`, "err");
              return;
            }
            await confirm();
          },
        }),
        button("Already approved — confirm", { onClick: confirm }),
      ),
    ];
  }

  private stepAllocation(): Child {
    const st = this.st;
    const maxLev = this.maxLeverage();
    const alloc = usdInput({ value: st.allocation ?? "", placeholder: "e.g. 500", id: "w-alloc" });
    const lev = h("select", { id: "w-lev" }, ...Array.from({ length: maxLev }, (_, i) => h("option", { value: String(i + 1) }, `${i + 1}×`)));
    lev.value = String(Math.min(st.leverage ?? 1, maxLev));
    const acctVal = (this.tradingAccounts ?? []).find((a) => a.address === st.trading)?.value ?? NaN;
    const warn = h("div");
    const levWarn = h("div", { "aria-live": "polite" });
    const ack = checkbox("I understand I can lose all allocated funds, and that leverage magnifies losses.", { checked: !!st.riskAck, required: true });
    const check = (): void => {
      const m = alloc.micro();
      const lv = Number(lev.value);
      mount(levWarn, highLeverageWarning(lv));
      mount(
        warn,
        m !== null && Number.isFinite(acctVal) && m / 1e6 > acctVal
          ? note(`This allocation is larger than the account's current value (${fmtUsd(Math.floor(acctVal * 1e6))}). Orders may be rejected or sized down until you add funds on Hyperliquid.`, "warn")
          : null,
        m !== null ? h("p", { class: "small muted" }, `Largest position the strategy may open: about ${fmtUsd(m * lv)} notional (${fmtUsd(m)} × ${lv}×).`) : null,
      );
    };
    alloc.el.addEventListener("input", check);
    lev.addEventListener("change", check);
    check();
    return [
      h("p", null, "Set how much of the trading account this strategy may use, and the most leverage it may take. The strategy sizes every position from these limits."),
      h(
        "div",
        { class: "form-grid two-col" },
        field("Allocation (USD)", alloc.el, "Funds stay in your Hyperliquid account; this is a sizing limit, not a transfer."),
        field("Max leverage", lev, this.marketMaxLev === undefined ? `${leverageHint(this.s.max_leverage, null, maxLev)} Checking Hyperliquid's market limit…` : leverageHint(this.s.max_leverage, this.marketMaxLev, maxLev)),
      ),
      warn,
      note(h("span", null, h("b", null, "You can lose all allocated funds. "), LOSS_WARNING), "bad"),
      levWarn,
      ack.el,
      h(
        "div",
        { class: "btns" },
        button("Continue", {
          kind: "primary",
          onClick: () => {
            const m = alloc.micro();
            if (m === null || m <= 0) {
              toast("Enter a valid USD amount (up to 6 decimals).", "warn");
              return;
            }
            if (m < this.cfg.min_allocation_micro) {
              toast(`Minimum allocation is ${fmtUsd(this.cfg.min_allocation_micro)}.`, "warn");
              return;
            }
            if (!ack.input.checked) {
              toast("Please confirm you understand you can lose all allocated funds.", "warn");
              return;
            }
            st.allocation = microToDecimal(m);
            st.leverage = Math.max(1, Math.min(maxLev, Number(lev.value) || 1));
            st.riskAck = true;
            this.next();
          },
        }),
      ),
    ];
  }

  private stepBalance(): Child {
    const price = this.price();
    const reserve = this.reserve();
    const box = h("div", { class: "stack tight" });
    const drawBal = (): void => {
      if (this.balanceErr) {
        mount(box, errorState(this.balanceErr, () => void load()));
        return;
      }
      if (!this.balance) {
        mount(box, skeleton(3));
        return;
      }
      const bal = this.balance.fee_balance_micro;
      const need = price + reserve;
      const enough = bal >= need;
      mount(
        box,
        kv([
          ["Fee balance", fmtUsd(bal)],
          ["Due now (first month)", price > 0 ? fmtUsd(price) : "Free"],
          ["Reserve for profit share", reserve > 0 ? fmtUsd(reserve) : "—"],
          ["Estimated monthly need", fmtUsd(this.balance.estimated_monthly_need_micro)],
        ]),
        enough
          ? note("Your fee balance covers the first month and the profit-share reserve. If the balance runs out, the subscription stops opening new positions.", "info")
          : note(`Top up at least ${fmtUsd(need - bal)} to continue (first month ${fmtUsd(price)} + reserve ${fmtUsd(reserve)} kept for daily profit share). Your progress here is saved.`, "warn"),
        h(
          "div",
          { class: "btns" },
          enough ? button("Continue", { kind: "primary", onClick: () => { this.balanceOk = true; this.next(); } }) : null,
          h("a", { class: ["btn", !enough && "primary"], href: "#/dashboard/balance" }, "Top up fee balance"),
          button("Refresh balance", { kind: "ghost", onClick: () => load() }),
        ),
      );
    };
    const load = async (): Promise<void> => {
      this.balanceErr = null;
      this.balance = null;
      drawBal();
      try {
        this.balance = await api.get<Balance>("/balance", { signal: this.ctx.signal });
      } catch (err) {
        if (isAbortError(err)) return;
        this.balanceErr = err;
      }
      if (this.ctx.isCurrent()) drawBal();
    };
    if (!this.balance && !this.balanceErr) void load();
    else drawBal();
    return [
      h("p", null, "Subscriptions and profit share are paid from your prepaid fee balance (separate from your trading funds). The first month is charged when you confirm."),
      box,
    ];
  }

  private stepConfirm(): Child {
    const st = this.st;
    const m = this.allocationMicro();
    const e = this.cfg.economics;
    const psTotal = e.platform_profit_share_mode === "on_top" ? this.s.profit_share_bps + e.platform_profit_share_bps : this.s.profit_share_bps;
    const price = this.price();
    const missing = [0, 1, 2, 3, 4, 5, 6, 7].filter((i) => !this.done(i));
    return [
      kv([
        ["Strategy", this.s.name],
        ["Trading account", h("span", { class: "mono break" }, `${st.tradingLabel ?? ""} ${st.trading ?? ""}`)],
        ["Allocation", m !== null ? fmtUsd(m) : "—"],
        ["Max leverage", st.leverage ? `${st.leverage}×` : "—"],
        ["Subscription", price > 0 ? `${fmtUsd(price)} / month, charged now` : "Free"],
        ["Profit share", `${fmtBps(psTotal)} of new profit above high-water mark`],
        ["Builder fee", `${fmtTenthsBp(e.builder_fee_tenths_bp)} of each order's notional`],
      ]),
      highLeverageWarning(st.leverage ?? 1),
      missing.length ? note(`Finish step${missing.length > 1 ? "s" : ""} ${missing.map((i) => i + 1).join(", ")} first.`, "warn") : null,
      h("p", { class: "small muted" }, "Confirming requires a fresh sign-in with two-factor (step-up). Trading starts at the next signal; execution is delayed by a random 0–10 minutes per subscriber for privacy."),
      h(
        "div",
        { class: "btns" },
        badge("Can lose all allocated funds", "bad"),
        button("Confirm subscription", {
          kind: "primary",
          disabled: missing.length > 0 || m === null,
          onClick: async () => {
            if (!(await requireAlertContacts(this.ctx.signal))) return; // SPEC §12: Telegram + email before the first subscription
            if (!st.subKey) {
              st.subKey = newIdempotencyKey();
              this.save();
            }
            try {
              await api.post(
                "/subscriptions",
                {
                  strategy_id: this.s.id,
                  trading_address: st.trading,
                  allocation_micro: m,
                  max_leverage_x100: (st.leverage ?? 1) * 100,
                  // terms the user reviewed; the server refuses with 409 reason "terms_changed" if they moved
                  expected_price_monthly_micro: price,
                  expected_profit_share_bps: this.s.profit_share_bps,
                },
                { signal: this.ctx.signal, idempotencyKey: st.subKey },
              );
            } catch (err) {
              const c = errCode(err);
              const reason = errReason(err);
              const restart = (patch: Partial<WizState>, msg: string): void => {
                Object.assign(this.st, patch, { subKey: undefined }); // payload will change → new Idempotency-Key
                this.save();
                this.say(msg, "err");
                this.draw();
              };
              if (c === "insufficient_balance") {
                this.balanceOk = false;
                this.balance = null;
                return restart({}, "Your fee balance is too low. Top up and try again.");
              }
              if (c === "contacts_required") return restart({}, "Link Telegram and confirm your email for alerts first (Alerts page), then confirm again.");
              if (reason === "trading_address_in_use") return restart({ trading: undefined }, "That trading account already runs a strategy. Choose another account.");
              if (reason === "subscription_ack_required") return restart({ gateAccepted: false }, "Please review and accept the strategy's risks and fees again (valid for 30 minutes).");
              if (reason === "terms_changed") return restart({ gateAccepted: false }, "The strategy's price or profit share changed. Please review the new terms.");
              if (reason === "agent_not_active") return restart({ agentDone: false, agentId: undefined, agentAddress: undefined }, "Approve the trading agent in your wallet first.");
              if (reason === "builder_fee_not_approved") return restart({ builderDone: false }, "Approve the builder fee in your wallet first.");
              const maxX100 = isRec(err) && isRec((err as { details?: unknown }).details) ? Number((err as { details: Record<string, unknown> }).details.max_x100) : NaN;
              if (c === "validation_failed" && Number.isFinite(maxX100) && maxX100 >= 100) {
                const cap = Math.floor(maxX100 / 100);
                return restart({ leverage: Math.min(st.leverage ?? 1, cap), riskAck: false }, `This strategy and market allow at most ${cap}× leverage right now. Check the leverage step and continue.`);
              }
              if (c === "validation_failed" || c === "guard_rejected" || c === "conflict" || c === "forbidden") {
                this.st.subKey = undefined;
                this.save();
              }
              throw err;
            }
            storage.remove(this.key);
            toast(`Subscribed to ${this.s.name}.`, "good");
            this.ctx.navigate("/dashboard");
          },
        }),
      ),
    ];
  }
}

function accountValue(ch: unknown): number {
  if (!isRec(ch)) return NaN;
  const ms = ch.marginSummary;
  if (!isRec(ms)) return NaN;
  return hlNum(ms.accountValue);
}

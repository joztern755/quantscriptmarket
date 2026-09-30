// #/dashboard[/:tab] — subscriptions (pause/resume/cancel/edit, step-up), positions, PnL per subscription,
// fee balance + ledger + deposits (USDC UsdSend / Stripe Payment Element) + withdraw (step-up), alerts inbox.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, stat, kv, table, tabs, button, toast, confirmDialog, modal, field, badge, subStatusBadge, type Column } from "../core/ui.js";
import { cancelButtons } from "../core/subscriptions.js";
import { api, publicConfig, peekPublicConfig, newIdempotencyKey, type PublicConfig } from "../core/api.js";
import { appConfig } from "../core/config.js";
import { loadStripe, stripeFeeNotice } from "../core/stripe.js";
import { connectWallet, getConnectedWallet } from "../core/wallet.js";
import { usdSend } from "../core/hl.js";
import { fmtUsd, fmtLeverage, fmtDate, fmtDateTime, fmtRelative, fmtNum, shortAddr } from "../core/format.js";
import type { Subscription, Position, Balance, LedgerRow, Alert, UsdcTypedDataOut, UsdcConfirmOut, StripeDepositOut, PayoutOut } from "./_shared/types.js";
import { ensurePageCss, listOf, isAbortError, errCode, errMessage, isAddress, hlNum, usdInput, pageHead, panel, every, isRec, LOSS_WARNING } from "./_shared/util.js";

export const title = "Dashboard";

const TABS = [
  { key: "overview", label: "Subscriptions" },
  { key: "positions", label: "Positions" },
  { key: "balance", label: "Fee balance" },
  { key: "alerts", label: "Alerts" },
];

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const tab = TABS.some((t) => t.key === ctx.params.tab) ? (ctx.params.tab as string) : "overview";
  const body = h("div", { class: "stack" });
  mount(
    root,
    h(
      "div",
      { class: "stack" },
      pageHead("Dashboard", "Your account", "Your trading funds stay in your Hyperliquid account. Fees are paid from your prepaid fee balance."),
      tabs(TABS, tab, (k) => ctx.navigate(k === "overview" ? "/dashboard" : `/dashboard/${k}`)),
      body,
    ),
  );
  if (tab === "positions") return positionsTab(body, ctx);
  if (tab === "balance") return balanceTab(body, ctx);
  if (tab === "alerts") return alertsTab(body, ctx);
  return overviewTab(body, ctx);
}

// ------------------------------------------------------------------------------------------ overview
async function overviewTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const kpis = h("div", { class: "stats" }, skeleton(2));
  const subsBox = h("div", { class: "stack" }, skeleton(6));
  mount(body, kpis, panel(h("div", { class: "row between w-full" }, h("h2", null, "Subscriptions"), h("a", { class: "btn sm", href: "#/market" }, "Add strategy")), subsBox));

  const load = async (): Promise<void> => {
    mount(subsBox, skeleton(6));
    try {
      const [subsRaw, bal, alertsRaw] = await Promise.all([
        api.get<unknown>("/subscriptions?limit=100", { signal: ctx.signal }),
        api.get<Balance>("/balance", { signal: ctx.signal }).catch(() => null),
        api.get<unknown>("/alerts", { signal: ctx.signal }).catch(() => null),
      ]);
      if (!ctx.isCurrent()) return;
      const subs = listOf<Subscription>(subsRaw);
      const alerts = listOf<Alert>(alertsRaw);
      const live = subs.filter((x) => x.status !== "cancelled");
      const pnl = live.reduce((a, x) => a + (x.cum_pnl_micro ?? 0), 0);
      const unread = alerts.filter((a) => !a.acked_at).length;
      mount(
        kpis,
        stat("Active subscriptions", fmtNum(live.filter((x) => x.status === "active").length, 0), `${live.length} total`),
        stat("Strategy PnL", h("span", { class: pnl >= 0 ? "pos" : "neg" }, fmtUsd(pnl, { sign: true })), "attributed, net of fees"),
        stat("Fee balance", bal ? fmtUsd(bal.fee_balance_micro) : "—", bal ? h("a", { href: "#/dashboard/balance" }, "Top up") : "unavailable"),
        stat("Unread alerts", fmtNum(unread, 0), h("a", { href: "#/dashboard/alerts" }, "Open inbox")),
      );
      const troubled = live.filter((x) => x.status === "past_due" || x.status === "reduce_only");
      mount(
        subsBox,
        troubled.length
          ? note(
              h(
                "span",
                null,
                `${troubled.length} subscription${troubled.length > 1 ? "s are" : " is"} past due or reduce-only because the fee balance is too low. Reduce-only means no new positions are opened; exits still run. `,
                h("a", { href: "#/dashboard/balance" }, "Top up now"),
                ".",
              ),
              "bad",
            )
          : null,
        subsTable(ctx, subs, () => void load()),
      );
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(subsBox, errorState(err, () => void load()));
    }
  };
  await load();
}

function subsTable(ctx: PageContext, subs: Subscription[], reload: () => void): HTMLElement {
  if (!subs.length) return emptyState("No subscriptions yet", "Browse the marketplace to subscribe to a strategy.", h("a", { class: "btn primary", href: "#/market" }, "Browse strategies"));
  const cols: Column<Subscription>[] = [
    {
      key: "strategy",
      label: "Strategy",
      value: (s) => (s.strategy_slug ? h("a", { href: `#/s/${encodeURIComponent(s.strategy_slug)}` }, s.strategy_name ?? s.strategy_slug) : s.strategy_name ?? "Strategy"),
      primary: true,
    },
    { key: "status", label: "Status", value: (s) => subStatusBadge(s.status) },
    { key: "acct", label: "Account", value: (s) => h("span", { class: "mono", title: s.trading_address }, shortAddr(s.trading_address)) },
    { key: "alloc", label: "Allocation", value: (s) => fmtUsd(s.allocation_micro), align: "right", mono: true },
    { key: "lev", label: "Max lev.", value: (s) => fmtLeverage(s.max_leverage_x100), align: "right", mono: true },
    {
      key: "pnl",
      label: "PnL",
      value: (s) => (typeof s.cum_pnl_micro === "number" ? h("span", { class: s.cum_pnl_micro >= 0 ? "pos" : "neg" }, fmtUsd(s.cum_pnl_micro, { sign: true })) : "—"),
      align: "right",
      mono: true,
    },
    { key: "hwm", label: "High-water mark", value: (s) => (typeof s.hwm_micro === "number" ? fmtUsd(s.hwm_micro) : "—"), align: "right", mono: true, hideOnMobile: true },
    { key: "renew", label: "Renews", value: (s) => (s.current_period_end ? fmtDate(s.current_period_end) : "—"), hideOnMobile: true },
    { key: "actions", label: "Actions", value: (s) => subActions(ctx, s, reload) },
  ];
  return h(
    "div",
    { class: "stack tight" },
    table({ columns: cols, rows: subs, rowKey: (s) => s.id }),
    h("p", { class: "small muted" }, "PnL is attributed from fills our agent placed for each subscription, net of trading and builder fees, plus funding. Profit share is charged only above the high-water mark. Changing allocation, leverage, pausing or cancelling needs a fresh sign-in."),
  );
}

function subActions(ctx: PageContext, s: Subscription, reload: () => void): HTMLElement {
  if (s.status === "cancelled") return h("span", { class: "muted small" }, s.cancel_positions === "leave" ? "Cancelled · positions left open" : "Cancelled");
  if (s.status === "closing") return h("span", { class: "muted small" }, "Closing positions…");
  const path = `/subscriptions/${encodeURIComponent(s.id)}`;
  const paused = s.status === "paused_user";
  return h(
    "div",
    { class: "btns" },
    button(paused ? "Resume" : "Pause", {
      kind: "ghost",
      onClick: async () => {
        if (!paused) {
          const ok = await confirmDialog({
            title: "Pause this subscription?",
            message: "While paused the strategy opens no new positions on this account. Existing positions are not closed automatically — manage them on Hyperliquid if needed.",
            confirmLabel: "Pause",
          });
          if (!ok) return;
        }
        await api.patch(path, { paused: !paused }, { signal: ctx.signal });
        toast(paused ? "Resumed." : "Paused.", "good");
        reload();
      },
    }),
    button("Edit", { kind: "ghost", onClick: () => editSubscription(ctx, s, reload) }),
    button("Cancel…", { kind: "danger", onClick: () => cancelChooser(s, reload) }),
  );
}

/** SPEC §12 cancel flow: the two core buttons (close / leave), each with a double confirmation and step-up,
 *  → DELETE /v1/subscriptions/{id} {"positions": "close"|"leave"} (core/subscriptions.ts). */
function cancelChooser(s: Subscription, reload: () => void): void {
  const m = modal({
    title: `Cancel ${s.strategy_name ?? "subscription"}`,
    body: h(
      "div",
      { class: "stack" },
      h("p", null, `Choose what happens to the open positions on ${shortAddr(s.trading_address)}. Your prepaid period is not refunded.`),
      cancelButtons({ id: s.id, strategy_name: s.strategy_name ?? "this strategy", markets: s.strategy_markets }, () => {
        m.close();
        reload();
      }),
    ),
    actions: [{ label: "Keep subscription", kind: "plain" }],
  });
}

function editSubscription(ctx: PageContext, s: Subscription, reload: () => void): void {
  // Upper bound from platform / launch caps; the server also applies the strategy's MAX_LEVERAGE (422 details.max_x100).
  const cfg = peekPublicConfig();
  let maxLev = cfg?.platform_max_leverage ?? 5;
  if (cfg?.max_user_leverage_x100) maxLev = Math.min(maxLev, Math.floor(cfg.max_user_leverage_x100 / 100));
  maxLev = Math.max(1, maxLev);
  const minAlloc = cfg?.min_allocation_micro ?? 100_000_000;
  const alloc = usdInput({ value: String(s.allocation_micro / 1_000_000), id: "e-alloc" });
  const lev = h("select", { id: "e-lev" }, ...Array.from({ length: maxLev }, (_, i) => h("option", { value: String(i + 1) }, `${i + 1}×`)));
  lev.value = String(Math.min(maxLev, Math.max(1, Math.round(s.max_leverage_x100 / 100))));
  const key = newIdempotencyKey();
  const m = modal({
    title: `Edit ${s.strategy_name ?? "subscription"}`,
    body: h(
      "div",
      { class: "stack" },
      field("Allocation (USD)", alloc.el, "Sizing limit — funds stay in your Hyperliquid account."),
      field("Max leverage", lev, `Strategy maximum ${maxLev}×.`),
      note(LOSS_WARNING, "warn"),
      h(
        "div",
        { class: "btns" },
        button("Save changes", {
          kind: "primary",
          onClick: async () => {
            const micro = alloc.micro();
            if (micro === null || micro < minAlloc) {
              toast(`Enter an allocation of at least ${fmtUsd(minAlloc)}.`, "warn");
              return;
            }
            await api.patch(`/subscriptions/${encodeURIComponent(s.id)}`, { allocation_micro: micro, max_leverage_x100: Number(lev.value) * 100 }, { signal: ctx.signal, idempotencyKey: key });
            toast("Subscription updated.", "good");
            m.close();
            reload();
          },
        }),
      ),
    ),
  });
}

// ------------------------------------------------------------------------------------------ positions
async function positionsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" }, skeleton(6));
  const updated = h("span", { class: "small muted" });
  mount(body, panel(h("div", { class: "row between w-full" }, h("h2", null, "Open positions"), updated), box, h("p", { class: "small muted" }, "Live from Hyperliquid for every trading account with a subscription. Values are indicative.")));
  const load = async (quiet = false): Promise<void> => {
    if (!quiet) mount(box, skeleton(6));
    try {
      const res = await api.get<unknown>("/positions", { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      const rows = listOf<Position>(res, "positions");
      const unavailable = listOf<string>(res, "unavailable");
      updated.textContent = `Updated ${fmtDateTime(Date.now())}`;
      const cols: Column<Position>[] = [
        { key: "coin", label: "Market", value: (p) => h("b", null, p.coin), primary: true },
        { key: "acct", label: "Account", value: (p) => h("span", { class: "mono" }, shortAddr(p.trading_address)) },
        { key: "side", label: "Side", value: (p) => (hlNum(p.size) >= 0 ? badge("Long", "good") : badge("Short", "bad")) },
        { key: "size", label: "Size", value: (p) => fmtNum(Math.abs(hlNum(p.size)), 4), align: "right", mono: true },
        { key: "entry", label: "Entry", value: (p) => dispNum(p.entry_px), align: "right", mono: true },
        { key: "value", label: "Value", value: (p) => dispUsd(p.position_value), align: "right", mono: true },
        {
          key: "upnl",
          label: "Unrealized PnL",
          value: (p) => {
            const v = hlNum(p.unrealized_pnl);
            return Number.isFinite(v) ? h("span", { class: v >= 0 ? "pos" : "neg" }, dispUsd(p.unrealized_pnl, true)) : "—";
          },
          align: "right",
          mono: true,
        },
        { key: "lev", label: "Leverage", value: (p) => (p.leverage === null || p.leverage === undefined ? "—" : `${fmtNum(hlNum(p.leverage), 1)}×`), align: "right", mono: true, hideOnMobile: true },
        { key: "liq", label: "Liq. price", value: (p) => dispNum(p.liquidation_px), align: "right", mono: true, hideOnMobile: true },
      ];
      mount(
        box,
        unavailable.length ? note(`Couldn't load positions for ${unavailable.map((a) => shortAddr(a)).join(", ")} right now.`, "warn") : null,
        table({ columns: cols, rows, rowKey: (p) => p.trading_address + p.coin, empty: "No open positions." }),
      );
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      if (!quiet) mount(box, errorState(err, () => void load()));
    }
  };
  every(ctx, 30_000, () => void load(true));
  await load();
}

/** Return URL for redirect-based Stripe methods: the site's own origin (never taken from data). */
function returnUrl(): string {
  const origin = appConfig().siteOrigin;
  const base = origin && origin === location.origin ? origin : location.origin;
  return `${base}${location.pathname}#/dashboard/balance`;
}

/** HL decimal string → "$1,234.56" — DISPLAY ONLY (float), never used for money math. */
function dispUsd(v: unknown, sign = false): string {
  const n = hlNum(v);
  if (!Number.isFinite(n)) return "—";
  return fmtUsd(Math.trunc(n * 1_000_000), { sign });
}
function dispNum(v: unknown): string {
  const n = hlNum(v);
  return Number.isFinite(n) ? fmtNum(n, n >= 100 ? 2 : 4) : "—";
}

// ------------------------------------------------------------------------------------------ balance
async function balanceTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const summary = h("div", { class: "stack" }, skeleton(3));
  const ledgerBox = h("div", { class: "stack" }, skeleton(6));
  const depositBox = h("div", { class: "stack" });
  const withdrawBox = h("div", { class: "stack" });
  mount(
    body,
    summary,
    h("div", { class: "grid-2" }, panel("Add funds", depositBox), panel("Withdraw", withdrawBox)),
    panel("Ledger history", ledgerBox),
  );

  // Stripe redirect-based payment methods come back here with ?redirect_status=…
  const sp = new URLSearchParams(location.search);
  const rs = sp.get("redirect_status");
  if (rs) {
    toast(rs === "succeeded" ? "Payment received. Your balance updates within a minute." : rs === "processing" ? "Payment processing. Your balance updates when it completes." : "Payment was not completed.", rs === "failed" ? "bad" : "good");
    try {
      history.replaceState(history.state, "", `${location.pathname}${location.hash}`);
    } catch {
      /* ignore */
    }
  }

  const cfg = await publicConfig();
  if (!ctx.isCurrent()) return;

  let bal: Balance | null = null;
  const load = async (): Promise<void> => {
    try {
      const [b, ledger] = await Promise.all([
        api.get<Balance>("/balance", { signal: ctx.signal }),
        api.get<unknown>("/balance/ledger?limit=100", { signal: ctx.signal }),
      ]);
      bal = b;
      if (!ctx.isCurrent()) return;
      mount(
        summary,
        h(
          "div",
          { class: "stats" },
          stat("Fee balance", fmtUsd(b.fee_balance_micro)),
          stat("Withdrawable", fmtUsd(b.withdrawable_micro), "USDC deposits only"),
          stat("Pending withdrawals", fmtUsd(b.withdrawals_pending_micro)),
          stat("Estimated monthly need", fmtUsd(b.estimated_monthly_need_micro), b.reserve_required_micro > 0 ? `+ ${fmtUsd(b.reserve_required_micro)} reserve` : undefined),
        ),
        b.fee_balance_micro < b.estimated_monthly_need_micro + b.reserve_required_micro
          ? note(`Your balance is below one month of estimated fees. If it runs out, subscriptions go past due and, after ${cfg.economics.past_due_grace_hours}h, reduce-only (no new positions).`, "warn")
          : null,
      );
      const rows = listOf<LedgerRow>(ledger);
      const cols: Column<LedgerRow>[] = [
        { key: "date", label: "Date", value: (r) => fmtDateTime(r.created_at), primary: true },
        { key: "kind", label: "Type", value: (r) => kindLabel(r.kind) },
        { key: "memo", label: "Details", value: (r) => h("span", { class: "muted break" }, r.memo ?? ""), hideOnMobile: true },
        { key: "amt", label: "Amount", value: (r) => h("span", { class: r.amount_micro >= 0 ? "pos" : "neg" }, fmtUsd(r.amount_micro, { sign: true })), align: "right", mono: true },
      ];
      mount(ledgerBox, table({ columns: cols, rows, rowKey: (r) => r.tx_id, empty: "No ledger entries yet." }));
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(summary, errorState(err, () => void load()));
      mount(ledgerBox, h("span"));
    }
  };

  mount(depositBox, depositPanel(ctx, cfg, () => void load()));
  mount(withdrawBox, withdrawPanel(ctx, cfg, () => bal, () => void load()));
  await load();
}

function kindLabel(k: string): string {
  const map: Record<string, string> = {
    deposit: "Deposit",
    deposit_usdc: "Deposit (USDC)",
    deposit_stripe: "Deposit (card)",
    subscription_start: "Subscription",
    subscription_renewal: "Subscription renewal",
    profit_share: "Profit share",
    post_purchase: "Paid post",
    plan_purchase: "Plan",
    withdrawal_hold: "Withdrawal (held)",
    withdrawal_release: "Withdrawal rejected (released)",
    withdrawal_sent: "Withdrawal sent",
    stripe_refund: "Refund",
    stripe_dispute: "Dispute",
  };
  return map[k] ?? k.replace(/_/g, " ");
}

interface StripeMin {
  elements(opts: Record<string, unknown>): { create(type: string, opts?: Record<string, unknown>): { mount(el: HTMLElement): void; destroy?(): void } };
  confirmPayment(opts: Record<string, unknown>): Promise<{ error?: { message?: string }; paymentIntent?: { status?: string } }>;
}

function depositPanel(ctx: PageContext, cfg: PublicConfig, onDone: () => void): HTMLElement {
  const min = cfg.economics.min_topup_micro;
  const amount = usdInput({ placeholder: `min ${fmtUsd(min)}`, id: "dep-amt" });
  const feeLine = h("p", { class: "small muted", "aria-live": "polite" });
  const stripeBox = h("div", { class: "stack" });
  const usdcStatus = h("p", { class: "status", "aria-live": "polite" });
  const updateFee = (): void => {
    const m = amount.micro();
    feeLine.textContent = `${stripeFeeNotice(cfg, m ?? undefined)} USDC deposits have no processor fee.`;
  };
  amount.el.addEventListener("input", updateFee);
  updateFee();

  const valid = (): number | null => {
    const m = amount.micro();
    if (m === null || m < min) {
      toast(`Enter at least ${fmtUsd(min)}.`, "warn");
      return null;
    }
    return m;
  };

  const payUsdc = async (): Promise<void> => {
    const m = valid();
    if (m === null) return;
    if (!isAddress(cfg.treasury_address)) {
      toast("USDC deposits are not configured yet.", "bad");
      return;
    }
    const w = getConnectedWallet() ?? (await connectWallet());
    if (!w) return;
    usdcStatus.className = "status";
    usdcStatus.textContent = "Preparing…";
    const td = await api.post<UsdcTypedDataOut>("/deposits/usdc/typed-data", { amount_micro: m, from_address: w.address, signature_chain_id: await w.chainIdHex() }, { signal: ctx.signal });
    if (td.destination.toLowerCase() !== cfg.treasury_address) throw new Error("The server's deposit address does not match the published treasury address. Nothing was sent.");
    usdcStatus.textContent = `Check your wallet: send ${fmtUsd(m)} USDC from ${shortAddr(w.address)} to ${shortAddr(cfg.treasury_address)} (Hyperliquid UsdSend)…`;
    const r = await usdSend(w, { destination: cfg.treasury_address, amountMicro: m, serverTypedData: td.payload.typed_data, expectDestination: cfg.treasury_address });
    if (!r.ok) {
      usdcStatus.className = "status err";
      usdcStatus.textContent = `Hyperliquid rejected the transfer: ${r.error ?? "unknown error"}`;
      return;
    }
    usdcStatus.textContent = "Sent. Confirming with the server…";
    try {
      const c = await api.post<UsdcConfirmOut>("/deposits/usdc/confirm", { from_address: w.address, time_ms: r.nonce ?? td.time_ms }, { signal: ctx.signal });
      usdcStatus.className = "status ok";
      usdcStatus.textContent = c.credited.length
        ? `Deposit received. Your balance is ${fmtUsd(c.fee_balance_micro)}.`
        : "Transfer sent. It will be credited automatically once Hyperliquid shows it (usually within a minute).";
    } catch (err) {
      usdcStatus.className = "status";
      usdcStatus.textContent = `Transfer sent. It will be credited automatically once detected (${errMessage(err)}).`;
    }
    onDone();
  };

  const payStripe = async (): Promise<void> => {
    const m = valid();
    if (m === null) return;
    if (!cfg.stripe_publishable_key) {
      toast("Card payments are not available right now.", "bad");
      return;
    }
    mount(stripeBox, skeleton(4));
    const [stripeRaw, pi] = await Promise.all([loadStripe(), api.post<StripeDepositOut>("/deposits/stripe", { amount_micro: m }, { signal: ctx.signal })]);
    if (!ctx.isCurrent()) return;
    const clientSecret = typeof pi.client_secret === "string" ? pi.client_secret : "";
    if (!/^pi_[A-Za-z0-9]+_secret_[A-Za-z0-9]+$/.test(clientSecret)) throw new Error("Payment could not be started. Please try again.");
    const stripe = stripeRaw as unknown as StripeMin;
    const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    const elements = stripe.elements({ clientSecret, appearance: { theme: dark ? "night" : "stripe", variables: { colorPrimary: dark ? "#E8A94A" : "#9A5B14", borderRadius: "10px" } } });
    const pe = elements.create("payment", { layout: "tabs" });
    const mountEl = h("div", { class: "stripe-mount" });
    const msg = h("p", { class: "status", "aria-live": "polite" });
    mount(
      stripeBox,
      h("p", { class: "small" }, `Paying ${fmtUsd(m)}. Card, Apple Pay, Google Pay and local methods are shown when available for your device and country.`),
      mountEl,
      msg,
      h(
        "div",
        { class: "btns" },
        button(`Pay ${fmtUsd(m)}`, {
          kind: "primary",
          onClick: async () => {
            msg.className = "status";
            msg.textContent = "Processing…";
            const res = await stripe.confirmPayment({ elements, confirmParams: { return_url: returnUrl() }, redirect: "if_required" });
            if (res.error) {
              msg.className = "status err";
              msg.textContent = res.error.message ?? "Payment failed.";
              return;
            }
            const st = res.paymentIntent?.status ?? "";
            msg.className = "status ok";
            msg.textContent = st === "succeeded" ? "Payment received. Your balance updates within a minute." : "Payment processing. Your balance updates when it completes.";
            let n = 0;
            const id = window.setInterval(() => {
              if (!ctx.isCurrent() || ++n > 6) return window.clearInterval(id);
              onDone();
            }, 10_000);
            ctx.onCleanup(() => window.clearInterval(id));
          },
        }),
        button("Cancel", { kind: "ghost", onClick: () => { pe.destroy?.(); mount(stripeBox); } }),
      ),
    );
    pe.mount(mountEl);
  };

  return h(
    "div",
    { class: "stack" },
    field("Amount (USD)", amount.el, `Minimum top-up ${fmtUsd(min)}.`),
    feeLine,
    h(
      "div",
      { class: "stack tight" },
      h("b", null, "USDC on Hyperliquid"),
      h("p", { class: "small muted" }, "Sends USDC from your connected wallet's Hyperliquid account to our treasury with a signed UsdSend. Credited as withdrawable balance."),
      h("div", { class: "btns" }, button("Deposit USDC", { kind: "primary", onClick: payUsdc })),
      usdcStatus,
    ),
    h("hr", { class: "divider" }),
    h(
      "div",
      { class: "stack tight" },
      h("b", null, "Card, Apple Pay, Google Pay, local methods"),
      h("p", { class: "small muted" }, "Processed by Stripe. Card-funded balance can be spent on fees but cannot be withdrawn as USDC."),
      h("div", { class: "btns" }, button("Pay with Stripe", { onClick: payStripe })),
      stripeBox,
    ),
  );
}

function withdrawPanel(ctx: PageContext, cfg: PublicConfig, getBal: () => Balance | null, onDone: () => void): HTMLElement {
  const amount = usdInput({ id: "wd-amt" });
  const to = h("input", { type: "text", id: "wd-to", placeholder: "0x… your Hyperliquid address", autocomplete: "off", spellcheck: "false" });
  const w = getConnectedWallet();
  if (w) to.value = w.address;
  let key = newIdempotencyKey();
  return h(
    "div",
    { class: "stack" },
    h("p", { class: "small muted" }, "Withdraw unused fee balance that came from USDC deposits. Withdrawals are reviewed and approved by two administrators, then sent as USDC on Hyperliquid."),
    cfg.features.payouts ? null : note("Withdrawals are not enabled yet during the internal launch phase.", "info"),
    field("Amount (USD)", amount.el),
    field("Destination address", to, "Must be one of your verified wallets."),
    h(
      "div",
      { class: "btns" },
      button("Request withdrawal", {
        onClick: async () => {
          const m = amount.micro();
          const addr = to.value.trim();
          if (m === null || m <= 0) return toast("Enter a valid amount.", "warn");
          if (!isAddress(addr)) return toast("Enter a valid 0x address.", "warn");
          const b = getBal();
          if (m < cfg.economics.min_topup_micro) return toast(`The minimum withdrawal is ${fmtUsd(cfg.economics.min_topup_micro)}.`, "warn");
          if (b && m > b.withdrawable_micro) return toast(`You can withdraw up to ${fmtUsd(b.withdrawable_micro)}.`, "warn");
          const ok = await confirmDialog({
            title: "Request withdrawal?",
            message: kv([
              ["Amount", fmtUsd(m)],
              ["To", h("span", { class: "mono break" }, addr.toLowerCase())],
              ["Network", `Hyperliquid (${cfg.hl_chain})`],
            ]),
            confirmLabel: "Request withdrawal",
          });
          if (!ok) return;
          try {
            await api.post<PayoutOut>("/withdrawals", { amount_micro: m, to_address: addr.toLowerCase() }, { signal: ctx.signal, idempotencyKey: key });
          } catch (err) {
            if (errCode(err) === "insufficient_balance") return toast("Not enough withdrawable balance.", "warn");
            if (errCode(err) === "forbidden") return toast(errMessage(err), "warn");
            throw err;
          }
          key = newIdempotencyKey();
          toast("Withdrawal requested. You'll get an alert when it's sent.", "good");
          onDone();
        },
      }),
    ),
  );
}

// ------------------------------------------------------------------------------------------ alerts
async function alertsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" }, skeleton(6));
  mount(body, panel("Alerts", box));
  const load = async (): Promise<void> => {
    try {
      const res = await api.get<unknown>("/alerts", { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      const alerts = listOf<Alert>(res).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
      if (!alerts.length) {
        mount(box, emptyState("No alerts", "Low balance, fills, errors and security events will appear here."));
        return;
      }
      mount(box, ...alerts.map((a) => alertItem(ctx, a, () => void load())));
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(box, errorState(err, () => void load()));
    }
  };
  await load();
}

function alertItem(ctx: PageContext, a: Alert, reload: () => void): HTMLElement {
  const tone = a.severity === "critical" ? "bad" : a.severity === "warn" ? "warn" : "info";
  const payloadMsg = isRec(a.payload) && typeof a.payload.message === "string" ? a.payload.message : "";
  const detail = payloadMsg || Object.entries(a.payload ?? {}).filter(([, v]) => typeof v === "string" || typeof v === "number").slice(0, 4).map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v)}`).join(" · ");
  return h(
    "div",
    { class: ["alert-item", !a.acked_at && "unread"] },
    h("div", { class: "row between" }, h("span", { class: "row" }, badge(a.severity, tone), h("span", { class: "alert-title" }, kindLabel(a.kind))), h("span", { class: "small muted", title: fmtDateTime(a.created_at) }, fmtRelative(a.created_at))),
    detail ? h("p", { class: "small break" }, detail) : null,
    !a.acked_at
      ? h(
          "div",
          { class: "btns" },
          button("Mark as read", {
            kind: "ghost",
            onClick: async () => {
              await api.post(`/alerts/${encodeURIComponent(a.id)}/ack`, {}, { signal: ctx.signal });
              reload();
            },
          }),
        )
      : null,
  ) as HTMLElement;
}


// #/admin[/:tab] — admin console over backend/app/api/routers/admin.py (docs/API_CONTRACT.md). Every mutation is
// step-up + audit-logged server-side (core api retries after step-up automatically). Maker-checker: protective
// actions apply at once; permissive ones (lift a switch, list a version, in-house price, unsuspend, KYC approval,
// payouts) need a second, different admin. UI role check only; the API enforces role=admin from its own DB.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, table, tabs, button, toast, confirmDialog, promptDialog, badge, kv, field, type Column } from "../core/ui.js";
import { api, publicConfig } from "../core/api.js";
import { connectWallet, getConnectedWallet } from "../core/wallet.js";
import { usdSend, hlInfo } from "../core/hl.js";
import { fmtUsd, fmtBps, fmtDateTime, fmtRelative, shortAddr, microToDecimal } from "../core/format.js";
import type { CreatorVersion, Page, Alert } from "./_shared/types.js";
import { backtestPanel } from "./_shared/backtest.js";
import { ensurePageCss, listOf, isAbortError, pageHead, panel, isAddress, usdInput, isRec, errMessage } from "./_shared/util.js";
import { trustAnchors } from "../core/config.js";
import { addressCheck } from "../core/addr.js";
import { destinationBlock, fetchAndCheckProof } from "./_shared/walletproof.js";

export const title = "Admin";

const TABS = [
  { key: "flags", label: "Kill switches" },
  { key: "approvals", label: "Approvals" },
  { key: "payouts", label: "Payouts" },
  { key: "held", label: "Held deposits" },
  { key: "strategies", label: "Strategies" },
  { key: "dexes", label: "Trusted dexes" },
  { key: "alerts", label: "Alerts" },
  { key: "recon", label: "Reconciliation" },
  { key: "users", label: "Users" },
];

const MARKETS = ["BTC", "SOL", "HYPE", "xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:BRENTOIL"];
/** schemas.Reason: 5–500 chars. */
const REASON = /^[\s\S]{5,500}$/;

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  if (ctx.me?.role !== "admin") {
    mount(root, emptyState("Admins only", "You don't have access to this page."));
    return;
  }
  const tab = TABS.some((t) => t.key === ctx.params.tab) ? (ctx.params.tab as string) : "flags";
  const body = h("div", { class: "stack" });
  mount(
    root,
    h(
      "div",
      { class: "stack" },
      pageHead("Admin", "Operations console", "Every action is audit-logged. Lifting a kill switch, listing a version, prices, KYC approvals and payouts need a second, different admin."),
      tabs(TABS, tab, (k) => ctx.navigate(k === "flags" ? "/admin" : `/admin/${k}`)),
      body,
    ),
  );
  const fn = { flags: flagsTab, approvals: approvalsTab, payouts: payoutsTab, held: heldTab, strategies: strategiesTab, dexes: dexesTab, alerts: alertsTab, recon: reconTab, users: usersTab }[tab as "flags"] ?? flagsTab;
  await fn(body, ctx);
}

/** Generic loader: skeleton → draw(data) or error state with retry. */
async function loadInto<T>(box: HTMLElement, ctx: PageContext, fetcher: () => Promise<T>, draw: (v: T, reload: () => void) => void): Promise<void> {
  const run = async (): Promise<void> => {
    mount(box, skeleton(6));
    try {
      const v = await fetcher();
      if (!ctx.isCurrent()) return;
      draw(v, () => void run());
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(box, errorState(err, () => void run()));
    }
  };
  await run();
}

async function askReason(titleText: string, placeholder?: string): Promise<string | null> {
  const r = await promptDialog({ title: titleText, label: "Reason (audit log, 5–500 characters)", placeholder, pattern: REASON });
  return r && r.trim().length >= 5 ? r.trim() : null;
}

const meId = (ctx: PageContext): string => String(ctx.me?.id ?? "");

// ------------------------------------------------------------------------------------------ flags
/** FlagOut. pending_by / updated_by are audit actors ("admin:<uuid>"). */
interface Flag {
  key: string;
  value: unknown;
  pending_value: unknown;
  pending_by: string | null;
  pending_at: string | null;
  updated_by: string | null;
  updated_at: string | null;
}

async function flagsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<unknown>("/admin/flags", { signal: ctx.signal }).then((r) => listOf<Flag>(r)),
    (flags, reload) => {
      const byKey = new Map(flags.map((f) => [f.key, f]));
      const keys = ["kill_switch_global", "new_entries_paused", ...MARKETS.map((m) => `kill_switch_market:${m}`)];
      for (const f of flags) if (!keys.includes(f.key)) keys.push(f.key);
      const pending = flags.filter((f) => f.pending_by);
      const set = async (key: string, on: boolean): Promise<void> => {
        const reason = await askReason(on ? `Engage ${key}` : `Request to lift ${key}`, "e.g. oracle divergence on xyz:SILVER");
        if (!reason) return;
        if (!on && !(await confirmDialog({ title: "Lift kill switch?", message: "Lifting requires approval by a second admin. Trading resumes only after approval.", confirmLabel: "Request lift", danger: true, requireText: "LIFT" }))) return;
        const r = await api.post<{ status: string }>("/admin/flags", { key, value: on, reason }, { signal: ctx.signal });
        toast(r.status === "pending" ? "Request created — waiting for a second admin." : "Engaged.", "good");
        reload();
      };
      const decide = async (f: Flag, approve: boolean): Promise<void> => {
        const reason = await askReason(`${approve ? "Approve" : "Reject"} lifting ${f.key}`);
        if (!reason) return;
        await api.post(`/admin/flags/${encodeURIComponent(f.key)}/${approve ? "approve" : "reject"}`, { reason }, { signal: ctx.signal });
        toast(approve ? "Approved." : "Rejected.", "good");
        reload();
      };
      mount(
        box,
        note("Engaging a kill switch takes effect immediately. Lifting one creates a request another admin must approve (maker-checker).", "info"),
        panel(
          "Pending approvals",
          pending.length
            ? table<Flag>({
                columns: [
                  { key: "k", label: "Flag", value: (p) => h("span", { class: "mono break" }, p.key), primary: true },
                  { key: "v", label: "New value", value: (p) => (p.pending_value === true ? badge("ON", "bad") : badge("OFF", "good")) },
                  { key: "by", label: "Requested by", value: (p) => `${p.pending_by}${p.pending_at ? " · " + fmtRelative(p.pending_at) : ""}` },
                  {
                    key: "a",
                    label: "",
                    value: (p) =>
                      p.pending_by === `admin:${meId(ctx)}`
                        ? h("span", { class: "small muted" }, "Needs another admin")
                        : h("div", { class: "btns" }, button("Approve", { kind: "primary", onClick: () => decide(p, true) }), button("Reject", { kind: "ghost", onClick: () => decide(p, false) })),
                  },
                ],
                rows: pending,
                rowKey: (p) => p.key,
              })
            : h("p", { class: "small muted" }, "Nothing waiting."),
        ),
        panel(
          "Switches",
          ...keys.map((k) => {
            const f = byKey.get(k);
            const on = f?.value === true;
            return h(
              "div",
              { class: "kill-row" },
              h("div", { class: "stack tight" }, h("span", { class: "k" }, k), h("span", { class: "small muted" }, f?.updated_by ? `by ${f.updated_by}${f.updated_at ? " · " + fmtDateTime(f.updated_at) : ""}` : "never set")),
              h(
                "div",
                { class: "btns" },
                on ? badge("ENGAGED", "bad") : badge("off", "muted"),
                on ? button(f?.pending_by ? "Lift requested" : "Request lift", { kind: "ghost", disabled: !!f?.pending_by, onClick: () => set(k, false) }) : button("Engage", { kind: "danger", onClick: () => set(k, true) }),
              ),
            );
          }),
          h(
            "div",
            { class: "btns" },
            button("Other market…", {
              kind: "ghost",
              onClick: async () => {
                const coin = await promptDialog({ title: "Kill switch for market", label: "Coin", placeholder: "xyz:SILVER", pattern: /^(?:[a-z0-9]{1,16}:)?[A-Za-z0-9]{1,32}$/ });
                if (coin) await set(`kill_switch_market:${coin.trim()}`, true);
              },
            }),
          ),
        ),
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ approvals (admin_changes)
interface Change {
  id: string;
  kind: string; // strategy_list | strategy_price | user_unsuspend | kyc_approve
  target: string;
  payload: Record<string, unknown>;
  reason: string;
  status: string;
  maker_admin: string;
  checker_admin: string | null;
  created_at: string;
  decided_at: string | null;
}

async function approvalsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<Page<Change>>("/admin/changes?status=pending", { signal: ctx.signal }).then((r) => listOf<Change>(r)),
    (rows, reload) => {
      const decide = async (c: Change, approve: boolean): Promise<void> => {
        const reason = await askReason(`${approve ? "Approve" : "Reject"} ${c.kind.replace(/_/g, " ")}`);
        if (!reason) return;
        await api.post(`/admin/changes/${encodeURIComponent(c.id)}/${approve ? "approve" : "reject"}`, { reason }, { signal: ctx.signal });
        toast(approve ? "Approved and applied." : "Rejected.", "good");
        reload();
      };
      mount(
        box,
        note("Changes proposed by one admin (listing a version, in-house prices, un-suspending a user, approving KYC). A different admin must approve.", "info"),
        table<Change>({
          columns: [
            { key: "k", label: "Change", value: (c) => h("b", null, c.kind.replace(/_/g, " ")), primary: true },
            { key: "t", label: "Target", value: (c) => h("span", { class: "mono break" }, c.target) },
            { key: "p", label: "Details", value: (c) => h("span", { class: "small break" }, changeDetails(c)) },
            { key: "r", label: "Reason", value: (c) => h("span", { class: "small break" }, c.reason), hideOnMobile: true },
            { key: "w", label: "Proposed", value: (c) => fmtRelative(c.created_at), hideOnMobile: true },
            {
              key: "a",
              label: "",
              value: (c) =>
                c.maker_admin === meId(ctx)
                  ? h("span", { class: "small muted" }, "Needs another admin")
                  : h("div", { class: "btns" }, button("Approve", { kind: "primary", onClick: () => decide(c, true) }), button("Reject", { kind: "ghost", onClick: () => decide(c, false) })),
            },
          ],
          rows,
          rowKey: (c) => c.id,
          empty: "Nothing waiting for approval.",
        }),
      );
    },
  );
}

function changeDetails(c: Change): string {
  const p = c.payload;
  if (c.kind === "strategy_list") return `list version v${String(p.version ?? "?")}`;
  if (c.kind === "strategy_price") return `price ${typeof p.previous_micro === "number" ? fmtUsd(p.previous_micro) : "—"} → ${typeof p.price_monthly_micro === "number" ? fmtUsd(p.price_monthly_micro) : "—"}`;
  if (c.kind === "kyc_approve") return `provider ${String(p.provider ?? "")}`;
  return "";
}

// ------------------------------------------------------------------------------------------ payouts
/** AdminPayoutOut. maker_admin / checker_admin / beneficiary are user ids. */
interface Payout {
  id: string;
  kind: "withdrawal" | "payout";
  beneficiary: string;
  amount_micro: number;
  to_address: string;
  status: string; // requested | approved_1 | approved_2 | sent | rejected
  maker_admin: string | null;
  checker_admin: string | null;
  tx_hash: string | null;
  created_at: string;
  // REVIEW_AUTH_API F5 — context for the approving admins (open requests only)
  to_address_verified_at?: string | null;
  wallet_age_hours?: number | null;
  security_hold_until?: string | null;
  hold_reasons?: string[];
  recent_security_events?: { action: string; at: string }[];
  send_issued_at?: string | null; // M4: typed data issued → no reject for 72 h
}

const HOLD_LABEL: Record<string, string> = {
  payout_address_hold: "wallet verified < 48 h ago",
  security_hold: "security hold (MFA change / new device)",
};

/** Wallet age, security hold and recent security events of the beneficiary (shown before every approval). */
function securityContext(p: Payout): HTMLElement {
  const events = p.recent_security_events ?? [];
  return h(
    "div",
    { class: "stack" },
    kv([
      ["Wallet age", p.wallet_age_hours == null ? "unknown" : p.wallet_age_hours < 48 ? badge(`${p.wallet_age_hours} h`, "bad") : `${Math.floor(p.wallet_age_hours / 24)} days`],
      ["Security hold", p.security_hold_until && Date.parse(p.security_hold_until) > Date.now() ? badge(`until ${fmtRelative(p.security_hold_until)}`, "bad") : "none"],
      ["Recent security events (30 d)", events.length ? events.map((e) => `${e.action} · ${fmtRelative(e.at)}`).join("; ") : "none"],
    ]),
    (p.hold_reasons ?? []).length ? note(`The second approval is refused while: ${(p.hold_reasons ?? []).map((r) => HOLD_LABEL[r] ?? r).join(", ")}.`, "warn") : null,
  );
}

/** After a treasury usdSend, find its hash in the treasury's ledger (userNonFundingLedgerUpdates). */
async function findSendHash(treasury: string, to: string, amountMicro: number, sentAtMs: number): Promise<string | null> {
  const want = microToDecimal(amountMicro);
  for (let i = 0; i < 6; i++) {
    try {
      const ups = await hlInfo<unknown>({ type: "userNonFundingLedgerUpdates", user: treasury, startTime: sentAtMs - 120_000 });
      if (Array.isArray(ups)) {
        for (const u of ups) {
          if (!isRec(u) || !isRec(u.delta) || typeof u.hash !== "string") continue;
          const d = u.delta;
          const amt = String(d.amount ?? d.usdc ?? "");
          if (String(d.destination ?? "").toLowerCase() === to.toLowerCase() && Number(amt) === Number(want) && /^0x[0-9a-fA-F]{64}$/.test(u.hash)) return u.hash.toLowerCase();
        }
      }
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
  return null;
}

async function payoutsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  const cfg = await publicConfig();
  if (!ctx.isCurrent()) return;
  await loadInto(
    box,
    ctx,
    async () => {
      const [w, p] = await Promise.all([
        api.get<Page<Payout>>("/admin/payouts?kind=withdrawal&limit=100", { signal: ctx.signal }),
        api.get<Page<Payout>>("/admin/payouts?kind=payout&limit=100", { signal: ctx.signal }),
      ]);
      return [...listOf<Payout>(w), ...listOf<Payout>(p)].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
    },
    (rows, reload) => {
      const open = rows.filter((p) => p.status !== "sent" && p.status !== "rejected");
      const done = rows.filter((p) => p.status === "sent" || p.status === "rejected");
      const base = (p: Payout): string => `/admin/payouts/${p.kind}/${encodeURIComponent(p.id)}`;
      const recordSent = async (p: Payout, txHash: string, timeMs: number): Promise<void> => {
        await api.post(`${base(p)}/sent`, { tx_hash: txHash, time_ms: timeMs }, { signal: ctx.signal, idempotencyKey: `payout-sent-${p.id}-${txHash.slice(2, 18)}` });
        toast("Sent and recorded.", "good");
        reload();
      };
      const proofPath = (p: Payout): string => `${base(p)}/wallet-proof`;
      const signAndSend = async (p: Payout): Promise<void> => {
        if (!isAddress(p.to_address)) throw new Error("Invalid destination address.");
        const treasury = trustAnchors().treasuryAddress; // PINNED (app-config.json), never the API's value
        if (cfg._fallback || !isAddress(treasury)) throw new Error("The treasury address is not pinned in this site build / config unavailable. Nothing was signed.");
        if (cfg.treasury_address !== treasury) throw new Error("The server's treasury address does not match the pinned treasury. Nothing was signed.");
        // SECURITY H1: the beneficiary's own signature over the destination, re-verified in THIS browser.
        const proof = await fetchAndCheckProof(proofPath(p), p.to_address, p.beneficiary, ctx.signal);
        if (!proof.ok) throw new Error(`Wallet proof failed: ${proof.reason} Nothing was signed.`);
        const w = getConnectedWallet() ?? (await connectWallet());
        if (!w) return;
        // The treasury key never touches a server: only the treasury hardware wallet may sign this usdSend.
        if (w.address.toLowerCase() !== treasury.toLowerCase()) {
          throw new Error(`The connected wallet ${shortAddr(w.address)} is not the treasury ${shortAddr(treasury)}. Connect the treasury hardware wallet; nothing was signed.`);
        }
        if (!(await confirmDialog({ title: "Send USDC from treasury?", message: h("div", { class: "stack" }, kv([["Amount", fmtUsd(p.amount_micro)], ["Beneficiary", h("span", { class: "mono" }, p.beneficiary)]]), destinationBlock(p.to_address, proof), addressCheck(treasury, { label: "From (pinned treasury)" })), confirmLabel: "Sign in wallet", danger: true }))) return;
        const td = await api.post<{ payout: Payout; payload: { typed_data?: unknown }; exchange_url: string }>(`${base(p)}/typed-data`, { signature_chain_id: await w.chainIdHex() }, { signal: ctx.signal });
        if (td.payout.to_address.toLowerCase() !== p.to_address.toLowerCase() || td.payout.amount_micro !== p.amount_micro) throw new Error("The payout changed on the server. Reload and check again.");
        const r = await usdSend(w, { destination: p.to_address, amountMicro: p.amount_micro, serverTypedData: td.payload.typed_data, expectDestination: p.to_address });
        if (!r.ok) throw new Error(`Hyperliquid rejected the transfer: ${r.error ?? "unknown error"}`);
        const sentAt = r.nonce ?? Date.now();
        toast("Transfer submitted. Looking up the transaction hash…", "info");
        const hash = (await findSendHash(treasury, p.to_address, p.amount_micro, sentAt)) ??
          (await promptDialog({ title: "Transaction hash", message: "Couldn't find the transfer automatically. Paste its hash from the Hyperliquid explorer (treasury address).", label: "Tx hash (0x…64 hex)", pattern: /^0x[0-9a-fA-F]{64}$/ }));
        if (!hash) return;
        await recordSent(p, hash.toLowerCase(), sentAt);
      };
      const action = (p: Payout): HTMLElement => {
        if (p.status === "requested" || p.status === "approved_1") {
          if (p.beneficiary === meId(ctx)) return h("span", { class: "small muted" }, "Your own request");
          if (p.status === "approved_1" && p.maker_admin === meId(ctx)) return h("span", { class: "small muted" }, "Waiting for a second admin");
          return h(
            "div",
            { class: "btns" },
            button(p.status === "requested" ? "Approve (1st)" : "Approve (2nd)", {
              kind: "primary",
              onClick: async () => {
                const proof = await fetchAndCheckProof(proofPath(p), p.to_address, p.beneficiary, ctx.signal);
                if (!proof.ok) {
                  await confirmDialog({ title: "Cannot approve", message: h("div", { class: "stack" }, destinationBlock(p.to_address, proof), note("Reject this request, or ask the beneficiary to request again: they must sign the ownership proof with the destination wallet.", "warn")), confirmLabel: "OK" });
                  return;
                }
                if (!(await confirmDialog({ title: "Approve payout?", message: h("div", { class: "stack" }, kv([["Kind", p.kind], ["Amount", fmtUsd(p.amount_micro)], ["Beneficiary", h("span", { class: "mono" }, p.beneficiary)]]), destinationBlock(p.to_address, proof), securityContext(p)), confirmLabel: "Approve" }))) return;
                await api.post(`${base(p)}/approve`, {}, { signal: ctx.signal });
                toast("Approved.", "good");
                reload();
              },
            }),
            button("Reject", {
              kind: "ghost",
              onClick: async () => {
                const reason = await askReason("Reject payout");
                if (!reason) return;
                await api.post(`${base(p)}/reject`, { reason }, { signal: ctx.signal });
                reload();
              },
            }),
          );
        }
        if (p.status === "approved_2") {
          return h(
            "div",
            { class: "btns" },
            button("Sign & send (treasury wallet)", { kind: "primary", disabled: !cfg.features.payouts, onClick: () => signAndSend(p) }),
            button("Record tx hash…", {
              kind: "ghost",
              onClick: async () => {
                const hash = await promptDialog({ title: "Record a sent transfer", message: "Only if the usdSend was already submitted from the treasury.", label: "Tx hash (0x…64 hex)", pattern: /^0x[0-9a-fA-F]{64}$/ });
                if (hash) await recordSent(p, hash.toLowerCase(), Date.now());
              },
            }),
          );
        }
        return h("span", { class: "small muted" }, p.tx_hash ? shortAddr(p.tx_hash, 10, 6) : p.status);
      };
      const cols: Column<Payout>[] = [
        { key: "who", label: "Beneficiary", value: (p) => h("span", { class: "break" }, `${p.kind} · `, h("span", { class: "mono" }, shortAddr(p.beneficiary, 8, 4))), primary: true },
        { key: "amt", label: "Amount", value: (p) => fmtUsd(p.amount_micro), align: "right", mono: true },
        { key: "to", label: "To", value: (p) => h("span", { class: "mono", title: p.to_address }, shortAddr(p.to_address)) },
        { key: "st", label: "Status", value: (p) => badge(p.status.replace(/_/g, " "), p.status === "sent" ? "good" : p.status === "rejected" ? "bad" : "warn") },
        { key: "chk", label: "Checks", value: (p) => ((p.hold_reasons ?? []).length ? badge((p.hold_reasons ?? []).map((r) => HOLD_LABEL[r] ?? r).join(", "), "bad") : p.wallet_age_hours != null ? `wallet ${Math.floor(p.wallet_age_hours / 24)} d` : "—"), hideOnMobile: true },
        { key: "mk", label: "Maker / checker", value: (p) => `${p.maker_admin ? shortAddr(p.maker_admin, 8, 4) : "—"} / ${p.checker_admin ? shortAddr(p.checker_admin, 8, 4) : "—"}`, hideOnMobile: true },
        { key: "at", label: "Requested", value: (p) => fmtRelative(p.created_at), hideOnMobile: true },
        { key: "a", label: "", value: action },
      ];
      mount(
        box,
        cfg.features.payouts ? null : note("Payouts are disabled in this launch phase (PAYOUTS_ENABLED=false): approvals and sending are refused by the server.", "warn"),
        note(`Two different admins must approve before sending. Each approval and the final send re-verify, in this browser, the beneficiary's signature proving control of the destination. The final UsdSend is signed here with the treasury hardware wallet (${trustAnchors().treasuryAddress || "not pinned in this build"}); any other connected wallet is refused.`, "info"),
        panel("Queue", table({ columns: cols, rows: open, rowKey: (p) => p.id, empty: "Queue is empty." })),
        panel("Recent", table({ columns: cols.filter((c) => c.key !== "a").concat([{ key: "tx", label: "Tx", value: (p) => (p.tx_hash ? h("span", { class: "mono" }, shortAddr(p.tx_hash, 10, 6)) : "—") }]), rows: done.slice(0, 50), rowKey: (p) => p.id, empty: "Nothing yet." })),
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ held deposits
/** HeldDepositOut: a treasury USDC transfer booked to suspense:usdc_unattributed by deposits-scan. */
interface HeldDeposit {
  tx_hash: string;
  held_tx_id: string;
  amount_micro: number;
  sender_address: string | null;
  reason: string | null;
  transfer_time: string | null;
  created_at: string;
  release_id: string | null;
  release_status: string | null;
  release_action: string | null;
}

/** SuspenseReleaseOut (maker-checker release of one held transfer). */
interface SuspenseRelease {
  id: string;
  created_at: string;
  tx_hash: string;
  amount_micro: number;
  action: "attribute" | "refund";
  user_id: string | null;
  sender_address: string;
  sender_source: string;
  evidence: string;
  status: "proposed" | "approved" | "sent" | "rejected";
  maker_admin: string;
  checker_admin: string | null;
  decision_reason: string | null;
  refund_tx_hash: string | null;
}

const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const EVIDENCE = /^[\s\S]{5,1000}$/;

/**
 * RUNBOOK §13.3 — release of held USDC (maker-checker). Admin A proposes: attribute the transfer to the user whose
 * VERIFIED wallet sent it (server-checked), or refund it to the on-chain sender. Admin B approves (never the maker) →
 * ONE ledger posting out of suspense. A refund is then signed here with the treasury hardware wallet (usdSend to the
 * recorded sender) and its tx hash recorded (verified on-chain by the server).
 */
async function heldTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  const cfg = await publicConfig();
  if (!ctx.isCurrent()) return;
  await loadInto(
    box,
    ctx,
    async () => {
      const [held, rel] = await Promise.all([
        api.get<{ items: HeldDeposit[]; suspense_balance_micro: number }>("/admin/held-deposits?open=true&limit=100", { signal: ctx.signal }),
        api.get<Page<SuspenseRelease>>("/admin/held-deposits/releases?limit=100", { signal: ctx.signal }),
      ]);
      return { held: held.items ?? [], balance: held.suspense_balance_micro ?? 0, releases: listOf<SuspenseRelease>(rel) };
    },
    ({ held, balance, releases }, reload) => {
      const relBase = (r: SuspenseRelease): string => `/admin/held-deposits/releases/${encodeURIComponent(r.id)}`;
      const propose = async (d: HeldDeposit, action: "attribute" | "refund"): Promise<void> => {
        let userId: string | null = null;
        if (action === "attribute") {
          userId = await promptDialog({ title: "Attribute to a user", message: "The sending address must already be one of this user's VERIFIED wallets (they prove control with a signed message). The server refuses otherwise.", label: "User id (uuid)", pattern: UUID_RE });
          if (!userId) return;
        }
        let sender: string | null = d.sender_address;
        if (!sender) {
          sender = await promptDialog({ title: "Sender address", message: "This transfer was held before senders were recorded. Give the on-chain sender (Hyperliquid explorer); the server verifies it against the transfer.", label: "Sender (0x…)", pattern: /^0x[0-9a-fA-F]{40}$/ });
          if (!sender) return;
        }
        const evidence = await promptDialog({ title: action === "attribute" ? "Evidence of ownership" : "Refund reason", message: "Ops-log reference and what proves the owner (audit log, 5–1000 characters).", label: "Evidence", pattern: EVIDENCE });
        if (!evidence) return;
        const summary = kv([["Transfer", h("span", { class: "mono break" }, d.tx_hash)], ["Amount", fmtUsd(d.amount_micro)], ["Sender", h("span", { class: "mono break" }, sender)],
          action === "attribute" ? ["Credit to user", h("span", { class: "mono" }, String(userId))] : ["Refund to", h("span", { class: "mono break" }, sender)]]);
        if (!(await confirmDialog({ title: action === "attribute" ? "Propose attribution?" : "Propose refund?", message: summary, confirmLabel: "Propose" }))) return;
        await api.post(`/admin/held-deposits/${encodeURIComponent(d.tx_hash)}/release`,
          { action, user_id: userId, sender_address: d.sender_address ? null : sender!.toLowerCase(), evidence },
          { signal: ctx.signal, idempotencyKey: `held-propose-${d.tx_hash.slice(2, 34)}-${action}` });
        toast("Proposed. A second, different admin must approve.", "good");
        reload();
      };
      const decide = async (r: SuspenseRelease, verb: "approve" | "reject"): Promise<void> => {
        const what = r.action === "attribute" ? `credit ${fmtUsd(r.amount_micro)} to user ${shortAddr(r.user_id ?? "", 8, 4)}` : `refund ${fmtUsd(r.amount_micro)} to ${shortAddr(r.sender_address)}`;
        const reason = await askReason(verb === "approve" ? `Approve: ${what}` : "Reject this proposal");
        if (!reason) return;
        await api.post(`${relBase(r)}/${verb}`, { reason }, { signal: ctx.signal, idempotencyKey: `held-${verb}-${r.id}` });
        toast(verb === "approve" ? "Approved and posted to the ledger." : "Rejected.", "good");
        reload();
      };
      const recordRefund = async (r: SuspenseRelease, txHash: string, timeMs: number): Promise<void> => {
        await api.post(`${relBase(r)}/sent`, { tx_hash: txHash, time_ms: timeMs }, { signal: ctx.signal, idempotencyKey: `held-sent-${r.id}-${txHash.slice(2, 18)}` });
        toast("Refund recorded.", "good");
        reload();
      };
      const signRefund = async (r: SuspenseRelease): Promise<void> => {
        if (!isAddress(r.sender_address)) throw new Error("Invalid refund destination.");
        const treasury = cfg.treasury_address;
        if (cfg._fallback || !isAddress(treasury)) throw new Error("The treasury address is not configured / config unavailable. Nothing was signed.");
        const w = getConnectedWallet() ?? (await connectWallet());
        if (!w) return;
        if (w.address.toLowerCase() !== treasury.toLowerCase()) {
          throw new Error(`The connected wallet ${shortAddr(w.address)} is not the treasury ${shortAddr(treasury)}. Connect the treasury hardware wallet; nothing was signed.`);
        }
        if (!(await confirmDialog({ title: "Refund USDC from treasury?", message: kv([["Amount", fmtUsd(r.amount_micro)], ["To (original sender)", h("span", { class: "mono break" }, r.sender_address)], ["From (treasury)", h("span", { class: "mono break" }, treasury)]]), confirmLabel: "Sign in wallet", danger: true }))) return;
        const td = await api.post<{ release: SuspenseRelease; payload: { typed_data?: unknown }; exchange_url: string }>(`${relBase(r)}/typed-data`, { signature_chain_id: await w.chainIdHex() }, { signal: ctx.signal });
        if (td.release.sender_address.toLowerCase() !== r.sender_address.toLowerCase() || td.release.amount_micro !== r.amount_micro) throw new Error("The refund changed on the server. Reload and check again.");
        const res = await usdSend(w, { destination: r.sender_address, amountMicro: r.amount_micro, serverTypedData: td.payload.typed_data, expectDestination: r.sender_address });
        if (!res.ok) throw new Error(`Hyperliquid rejected the transfer: ${res.error ?? "unknown error"}`);
        const sentAt = res.nonce ?? Date.now();
        toast("Refund submitted. Looking up the transaction hash…", "info");
        const hash = (await findSendHash(treasury, r.sender_address, r.amount_micro, sentAt)) ??
          (await promptDialog({ title: "Transaction hash", message: "Couldn't find the transfer automatically. Paste its hash from the Hyperliquid explorer (treasury address).", label: "Tx hash (0x…64 hex)", pattern: /^0x[0-9a-fA-F]{64}$/ }));
        if (!hash) return;
        await recordRefund(r, hash.toLowerCase(), sentAt);
      };
      const heldAction = (d: HeldDeposit): HTMLElement => {
        if (d.release_id) return badge(`${d.release_action ?? "release"} ${d.release_status ?? ""}`.trim(), "warn");
        return h("div", { class: "btns" },
          button("Attribute…", { kind: "primary", onClick: () => propose(d, "attribute") }),
          button("Refund…", { kind: "ghost", onClick: () => propose(d, "refund") }));
      };
      const relAction = (r: SuspenseRelease): HTMLElement => {
        if (r.status === "proposed") {
          if (r.maker_admin === meId(ctx)) return h("span", { class: "small muted" }, "Waiting for a second admin");
          if (r.user_id && r.user_id === meId(ctx)) return h("span", { class: "small muted" }, "Credits you — another admin must decide");
          return h("div", { class: "btns" },
            button("Approve", { kind: "primary", onClick: () => decide(r, "approve") }),
            button("Reject", { kind: "ghost", onClick: () => decide(r, "reject") }));
        }
        if (r.status === "approved" && r.action === "refund") {
          return h("div", { class: "btns" },
            button("Sign & send refund (treasury wallet)", { kind: "primary", onClick: () => signRefund(r) }),
            button("Record tx hash…", {
              kind: "ghost",
              onClick: async () => {
                const hash = await promptDialog({ title: "Record a sent refund", message: "Only if the usdSend was already submitted from the treasury.", label: "Tx hash (0x…64 hex)", pattern: /^0x[0-9a-fA-F]{64}$/ });
                if (hash) await recordRefund(r, hash.toLowerCase(), Date.now());
              },
            }));
        }
        return h("span", { class: "small muted" }, r.refund_tx_hash ? shortAddr(r.refund_tx_hash, 10, 6) : r.status);
      };
      const heldCols: Column<HeldDeposit>[] = [
        { key: "tx", label: "Transfer", value: (d) => h("span", { class: "mono", title: d.tx_hash }, shortAddr(d.tx_hash, 10, 6)), primary: true },
        { key: "amt", label: "Amount", value: (d) => fmtUsd(d.amount_micro), align: "right", mono: true },
        { key: "from", label: "Sender", value: (d) => (d.sender_address ? h("span", { class: "mono", title: d.sender_address }, shortAddr(d.sender_address)) : h("span", { class: "small muted" }, "not recorded")) },
        { key: "why", label: "Held because", value: (d) => d.reason ?? "—", hideOnMobile: true },
        { key: "at", label: "Held", value: (d) => fmtRelative(d.created_at), hideOnMobile: true },
        { key: "a", label: "", value: heldAction },
      ];
      const relCols: Column<SuspenseRelease>[] = [
        { key: "tx", label: "Transfer", value: (r) => h("span", { class: "mono", title: r.tx_hash }, shortAddr(r.tx_hash, 10, 6)), primary: true },
        { key: "act", label: "Action", value: (r) => (r.action === "attribute" ? h("span", null, "credit user ", h("span", { class: "mono" }, shortAddr(r.user_id ?? "", 8, 4))) : h("span", null, "refund to ", h("span", { class: "mono" }, shortAddr(r.sender_address)))) },
        { key: "amt", label: "Amount", value: (r) => fmtUsd(r.amount_micro), align: "right", mono: true },
        { key: "st", label: "Status", value: (r) => badge(r.status, r.status === "sent" || (r.status === "approved" && r.action === "attribute") ? "good" : r.status === "rejected" ? "bad" : "warn") },
        { key: "ev", label: "Evidence", value: (r) => h("span", { class: "small break" }, r.evidence), hideOnMobile: true },
        { key: "mk", label: "Maker / checker", value: (r) => `${shortAddr(r.maker_admin, 8, 4)} / ${r.checker_admin ? shortAddr(r.checker_admin, 8, 4) : "—"}`, hideOnMobile: true },
        { key: "a", label: "", value: relAction },
      ];
      const openRel = releases.filter((r) => r.status === "proposed" || (r.status === "approved" && r.action === "refund"));
      const doneRel = releases.filter((r) => !openRel.includes(r));
      mount(
        box,
        note(`Held USDC (suspense) ${fmtUsd(balance)} = sum of open held transfers. Release is maker-checker: one admin proposes, a different admin approves; the ledger moves only on approval. Refunds go back to the recorded on-chain sender and are signed with the treasury hardware wallet (${cfg.treasury_address ? shortAddr(cfg.treasury_address) : "not configured"}).`, "info"),
        panel("Held transfers", table({ columns: heldCols, rows: held, rowKey: (d) => d.tx_hash, empty: "No held deposits." })),
        panel("Releases in progress", table({ columns: relCols, rows: openRel, rowKey: (r) => r.id, empty: "Nothing pending." })),
        panel("Recent releases", table({ columns: relCols.filter((c) => c.key !== "a").concat([{ key: "tx2", label: "Refund tx", value: (r) => (r.refund_tx_hash ? h("span", { class: "mono" }, shortAddr(r.refund_tx_hash, 10, 6)) : "—") }]), rows: doneRel.slice(0, 50), rowKey: (r) => r.id, empty: "Nothing yet." })),
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ strategies
/** AdminStrategyOut (versions: latest 5, newest first). */
interface AdminStrategy {
  id: string;
  slug: string;
  name: string;
  status: string;
  in_house: boolean;
  owner_user_id: string | null;
  owner_kyc_status: string | null;
  price_monthly_micro: number | null;
  profit_share_bps: number;
  versions: CreatorVersion[];
}

async function strategiesTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  let status = ctx.query.get("status") ?? "review";
  const sel = h("select", { "aria-label": "Status" }, ...["review", "listed", "paused", "draft", "delisted"].map((x) => h("option", { value: x }, x)));
  sel.value = status;
  const box = h("div", { class: "stack" });
  const cfg = await publicConfig();
  const load = (): Promise<void> =>
    loadInto(
      box,
      ctx,
      () => api.get<Page<AdminStrategy>>(`/admin/strategies?status=${encodeURIComponent(status)}&limit=50`, { signal: ctx.signal }).then((r) => listOf<AdminStrategy>(r)),
      (rows, reload) => {
        if (!rows.length) {
          mount(box, emptyState(`No ${status} strategies`));
          return;
        }
        const act = async (s: AdminStrategy, path: string, label: string, danger = false): Promise<void> => {
          const reason = await askReason(`${label}: ${s.name}`);
          if (!reason) return;
          if (danger && !(await confirmDialog({ title: `${label}?`, message: "Live subscriptions of a delisted strategy go reduce-only (exits only).", confirmLabel: label, danger: true }))) return;
          const r = await api.post<{ status: string }>(`/admin/strategies/${encodeURIComponent(s.id)}/${path}`, { reason }, { signal: ctx.signal });
          toast(r.status === "pending" ? "Proposed — a second admin must approve." : "Done.", "good");
          reload();
        };
        mount(
          box,
          ...rows.map((s) => {
            const unpublished = s.versions.filter((v) => !v.published_at);
            const price = usdInput({ value: String((s.price_monthly_micro ?? 0) / 1_000_000) });
            return panel(
              h("div", { class: "row between w-full" }, h("h2", null, s.name), h("span", { class: "row" }, badge(s.status, s.status === "listed" ? "good" : "muted"), s.in_house ? badge("in-house", "info") : badge(`KYC ${s.owner_kyc_status ?? "not started"}`, s.owner_kyc_status === "approved" ? "good" : "bad"))),
              kv([
                ["Slug", h("span", { class: "mono" }, s.slug)],
                ["Price / profit share", `${s.price_monthly_micro === null ? "not set" : fmtUsd(s.price_monthly_micro)} / ${fmtBps(s.profit_share_bps)}`],
                ["Owner", s.owner_user_id ? h("span", { class: "mono break" }, s.owner_user_id) : "in-house"],
              ]),
              ...s.versions.map((v) =>
                h(
                  "details",
                  null,
                  h("summary", null, `v${v.version} · ${v.published_at ? `listed ${fmtDateTime(v.published_at)}` : "unpublished"} · ${String(v.params.source ?? "?")} · hash ${v.code_hash.slice(0, 12)}…`),
                  (() => {
                    const bt = v.backtest;
                    const days = typeof bt?.history_days === "number" ? bt.history_days : bt?.period?.sim_days;
                    return h(
                      "div",
                      { class: "stack" },
                      typeof days === "number" && days < cfg.min_listing_history_days ? note(`Only ${Math.floor(days)} days of history — listing needs ≥ ${cfg.min_listing_history_days} days (the server refuses).`, "bad") : null,
                      backtestPanel(bt, { shortHistoryDays: typeof days === "number" && days < cfg.short_history_warning_days ? Math.floor(days) : null }),
                      !v.published_at && s.status !== "delisted"
                        ? h(
                            "div",
                            { class: "btns" },
                            button(`Propose listing v${v.version}`, {
                              kind: "primary",
                              onClick: async () => {
                                const reason = await askReason(`List ${s.name} v${v.version}`);
                                if (!reason) return;
                                await api.post(`/admin/strategies/${encodeURIComponent(s.id)}/list`, { version_id: v.id, reason }, { signal: ctx.signal });
                                toast("Proposed — a second admin approves it under Approvals.", "good");
                                reload();
                              },
                            }),
                          )
                        : null,
                    );
                  })(),
                ),
              ),
              unpublished.length === 0 && !s.versions.length ? h("p", { class: "small muted" }, "No versions uploaded.") : null,
              s.in_house
                ? h(
                    "div",
                    { class: "row" },
                    field("In-house price / month (USD)", price.el),
                    button("Propose price", {
                      onClick: async () => {
                        const p = price.micro() ?? (price.el.value.trim() === "0" ? 0 : null);
                        if (p === null) return toast("Invalid price.", "warn");
                        const reason = await askReason(`Set ${s.name} price to ${fmtUsd(p)}`);
                        if (!reason) return;
                        await api.post(`/admin/strategies/${encodeURIComponent(s.id)}/price`, { price_monthly_micro: p, reason }, { signal: ctx.signal });
                        toast("Proposed — a second admin must approve.", "good");
                        reload();
                      },
                    }),
                  )
                : null,
              h(
                "div",
                { class: "btns" },
                s.status === "review" ? button("Reject to draft", { kind: "ghost", onClick: () => act(s, "reject", "Reject") }) : null,
                s.status === "listed" ? button("Pause", { kind: "ghost", onClick: () => act(s, "pause", "Pause") }) : null,
                s.status !== "delisted" ? button("Delist", { kind: "danger", onClick: () => act(s, "delist", "Delist", true) }) : null,
                !s.in_house && s.owner_user_id && s.owner_kyc_status && s.owner_kyc_status !== "approved"
                  ? button("KYC decision…", { kind: "ghost", onClick: () => kycDecision(ctx, s.owner_user_id as string, reload) })
                  : null,
              ),
            );
          }),
        );
      },
    );
  sel.addEventListener("change", () => {
    status = sel.value;
    void load();
  });
  mount(body, h("div", { class: "filters" }, h("div", { class: "field" }, h("span", { class: "fl" }, "Status"), sel)), box);
  await load();
}

async function kycDecision(ctx: PageContext, userId: string, reload: () => void): Promise<void> {
  const approve = await confirmDialog({ title: "KYC decision", message: "Record the verification provider's verdict. Approval unlocks listing, paid posts and payouts and needs a second admin; rejection applies now.", confirmLabel: "Approve (propose)", cancelLabel: "Reject…" });
  const reason = await askReason(approve ? "Approve creator KYC" : "Reject creator KYC");
  if (!reason) return;
  const r = await api.post<{ status: string }>(`/admin/users/${encodeURIComponent(userId)}/kyc`, { decision: approve ? "approved" : "rejected", reason }, { signal: ctx.signal });
  toast(r.status === "pending" ? "Proposed — a second admin must approve." : "Recorded.", "good");
  reload();
}

// ------------------------------------------------------------------------------------------ alerts
async function alertsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  let sev = ctx.query.get("severity") ?? "";
  let unacked = true;
  const sel = h("select", { "aria-label": "Severity" }, h("option", { value: "" }, "All severities"), h("option", { value: "critical" }, "Critical"), h("option", { value: "warn" }, "Warn"), h("option", { value: "info" }, "Info"));
  sel.value = sev;
  const ack = h("select", { "aria-label": "Acknowledged" }, h("option", { value: "1" }, "Unacknowledged"), h("option", { value: "0" }, "All"));
  const box = h("div", { class: "stack" });
  const load = (): Promise<void> =>
    loadInto(
      box,
      ctx,
      () => api.get<Page<Alert>>(`/admin/alerts?unacked=${unacked}${sev ? `&severity=${encodeURIComponent(sev)}` : ""}&limit=100`, { signal: ctx.signal }).then((r) => listOf<Alert>(r)),
      (rows, reload) => {
        mount(
          box,
          table<Alert>({
            columns: [
              { key: "t", label: "When", value: (a) => h("span", { title: fmtDateTime(a.created_at) }, fmtRelative(a.created_at)), primary: true },
              { key: "s", label: "Severity", value: (a) => badge(a.severity, a.severity === "critical" ? "bad" : a.severity === "warn" ? "warn" : "info") },
              { key: "k", label: "Kind", value: (a) => h("span", { class: "mono" }, a.kind) },
              { key: "m", label: "Details", value: (a) => h("span", { class: "small break" }, JSON.stringify(a.payload).slice(0, 300)) },
              {
                key: "a",
                label: "",
                value: (a) => (a.acked_at ? h("span", { class: "small muted" }, `acked ${fmtRelative(a.acked_at)}`) : button("Acknowledge", { kind: "ghost", onClick: async () => { await api.post(`/admin/alerts/${encodeURIComponent(a.id)}/ack`, {}, { signal: ctx.signal }); reload(); } })),
              },
            ],
            rows,
            rowKey: (a) => a.id,
            empty: "No alerts.",
          }),
        );
      },
    );
  sel.addEventListener("change", () => {
    sev = sel.value;
    void load();
  });
  ack.addEventListener("change", () => {
    unacked = ack.value === "1";
    void load();
  });
  mount(body, h("div", { class: "filters" }, h("div", { class: "field" }, h("span", { class: "fl" }, "Severity"), sel), h("div", { class: "field" }, h("span", { class: "fl" }, "Show"), ack)), box);
  await load();
}

// ------------------------------------------------------------------------------------------ reconciliation
/** ReconciliationOut.report = app.execution.reconcile.ReconcileReport.as_dict(). */
interface ReconReport {
  positions_checked?: number;
  drifts?: { subscription_id: string; coin: string; expected_micro: number; actual_micro: number }[];
  builder_db_micro?: number | null;
  builder_chain_micro?: number | null;
  builder_mismatch?: boolean;
  treasury_ledger_micro?: number | null;
  treasury_chain_micro?: number | null;
  treasury_mismatch?: boolean;
  errors?: string[];
}

async function reconTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<{ report: ReconReport | null; generated_at: string | null }>("/admin/reconciliation", { signal: ctx.signal }),
    (res) => {
      const r = res.report;
      if (!r) {
        mount(box, emptyState("No reconciliation report yet", "The daily job has not produced a report."));
        return;
      }
      const usd = (v: unknown): string => (typeof v === "number" ? fmtUsd(v) : "—");
      const diff = (a: unknown, b: unknown): string => (typeof a === "number" && typeof b === "number" ? fmtUsd(b - a, { sign: true }) : "—");
      const checks = [
        { name: "Builder fees: ledger vs Hyperliquid rewards", l: r.builder_db_micro, o: r.builder_chain_micro, bad: !!r.builder_mismatch },
        { name: "Treasury USDC: ledger vs on-chain", l: r.treasury_ledger_micro, o: r.treasury_chain_micro, bad: !!r.treasury_mismatch },
      ];
      const drifts = r.drifts ?? [];
      const bad = checks.filter((c) => c.bad).length + drifts.length + (r.errors?.length ?? 0);
      mount(
        box,
        h("p", { class: "small muted" }, res.generated_at ? `Generated ${fmtDateTime(res.generated_at)} · ` : "", `${r.positions_checked ?? 0} positions checked. Mismatch > $1 raises a critical alert.`),
        bad ? note(`${bad} issue${bad > 1 ? "s" : ""} found.`, "bad") : note("All checks passing.", "info"),
        table({
          columns: [
            { key: "n", label: "Check", value: (c: (typeof checks)[number]) => c.name, primary: true },
            { key: "l", label: "Ledger", value: (c: (typeof checks)[number]) => usd(c.l), align: "right", mono: true },
            { key: "o", label: "On-chain", value: (c: (typeof checks)[number]) => usd(c.o), align: "right", mono: true },
            { key: "d", label: "Diff", value: (c: (typeof checks)[number]) => diff(c.l, c.o), align: "right", mono: true },
            { key: "s", label: "Status", value: (c: (typeof checks)[number]) => (c.bad ? badge("mismatch", "bad") : badge("ok", "good")) },
          ],
          rows: checks,
          rowKey: (c) => c.name,
        }),
        drifts.length
          ? panel(
              "Position drifts",
              table({
                columns: [
                  { key: "s", label: "Subscription", value: (d: (typeof drifts)[number]) => h("span", { class: "mono" }, shortAddr(d.subscription_id, 8, 4)), primary: true },
                  { key: "c", label: "Coin", value: (d: (typeof drifts)[number]) => d.coin },
                  { key: "e", label: "Expected", value: (d: (typeof drifts)[number]) => usd(d.expected_micro), align: "right", mono: true },
                  { key: "a", label: "Actual", value: (d: (typeof drifts)[number]) => usd(d.actual_micro), align: "right", mono: true },
                ],
                rows: drifts,
                rowKey: (d) => d.subscription_id + d.coin,
              }),
            )
          : null,
        r.errors?.length ? panel("Errors", h("ul", { class: "small" }, ...r.errors.map((e) => h("li", { class: "break" }, e)))) : null,
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ users
/** AdminUserOut. */
interface AdminUser {
  id: string;
  email: string | null;
  display_name: string | null;
  role: string;
  plan: string;
  status: string;
  country_attested: string | null;
  created_at: string;
}

async function usersTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const q = h("input", { type: "search", placeholder: "Email or name (3+ characters)", "aria-label": "Search users", value: ctx.query.get("q") ?? "" });
  const box = h("div", { class: "stack" });
  const search = (): Promise<void> => {
    const term = q.value.trim();
    if (term.length < 3) {
      mount(box, h("p", { class: "small muted" }, "Type at least 3 characters."));
      return Promise.resolve();
    }
    return loadInto(
      box,
      ctx,
      () => api.get<Page<AdminUser>>(`/admin/users?q=${encodeURIComponent(term)}`, { signal: ctx.signal }).then((r) => listOf<AdminUser>(r)),
      (rows, reload) => {
        mount(
          box,
          table<AdminUser>({
            columns: [
              { key: "e", label: "User", value: (u) => h("span", { class: "break" }, u.email ?? u.display_name ?? u.id), primary: true },
              { key: "r", label: "Role", value: (u) => u.role },
              { key: "p", label: "Plan", value: (u) => u.plan, hideOnMobile: true },
              { key: "k", label: "Country", value: (u) => u.country_attested ?? "—", hideOnMobile: true },
              { key: "s", label: "Status", value: (u) => badge(u.status, u.status === "active" ? "good" : "bad") },
              { key: "c", label: "Joined", value: (u) => fmtRelative(u.created_at), hideOnMobile: true },
              {
                key: "a",
                label: "",
                value: (u) =>
                  u.id === meId(ctx)
                    ? h("span", { class: "small muted" }, "you")
                    : h(
                        "div",
                        { class: "btns" },
                        u.status === "suspended"
                          ? button("Unsuspend", {
                              kind: "ghost",
                              onClick: async () => {
                                const reason = await askReason(`Unsuspend ${u.email ?? u.id}`);
                                if (!reason) return;
                                await api.post(`/admin/users/${encodeURIComponent(u.id)}/unsuspend`, { reason }, { signal: ctx.signal });
                                toast("Proposed — a second admin must approve.", "good");
                                reload();
                              },
                            })
                          : button("Suspend", {
                              kind: "danger",
                              onClick: async () => {
                                const reason = await askReason(`Suspend ${u.email ?? u.id}`);
                                if (!reason) return;
                                if (!(await confirmDialog({ title: "Suspend user?", message: "The user is blocked from account actions. This is audit-logged; lifting it needs two admins.", confirmLabel: "Suspend", danger: true, requireText: "SUSPEND" }))) return;
                                await api.post(`/admin/users/${encodeURIComponent(u.id)}/suspend`, { reason }, { signal: ctx.signal });
                                toast("Suspended.", "good");
                                reload();
                              },
                            }),
                        u.role === "creator" ? button("KYC…", { kind: "ghost", onClick: () => kycDecision(ctx, u.id, reload) }) : null,
                      ),
              },
            ],
            rows,
            rowKey: (u) => u.id,
            empty: "No users found.",
          }),
        );
      },
    ).catch((err) => {
      toast(errMessage(err), "bad");
    });
  };
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") void search();
  });
  mount(body, h("div", { class: "filters", role: "search" }, h("div", { class: "field" }, h("span", { class: "fl" }, "Search"), q), h("div", null, button("Search", { onClick: () => search() }))), box);
  await search();
}

// ------------------------------------------------------------------------------------------ trusted builder dexes
// SPEC §12 (owner, 30 Sep 2026): strategies may trade validator perps and HIP-3 perps only on allowlisted dexes.
// ONE admin adds a dex (step-up, audit-logged); removing one immediately stops new entries on its markets (the
// executor re-reads the list every tick; exits keep running). Server: /admin/dexes (backend/app/api/routers/admin.py).
interface TrustedDex {
  dex: string;
  active: boolean;
  added_by: string;
  reason: string;
  created_at: string;
  removed_at: string | null;
  removed_by: string | null;
  removal_reason: string | null;
}

const DEX_NAME = /^[a-z][a-z0-9]{0,15}$/;

async function dexesTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<unknown>("/admin/dexes", { signal: ctx.signal }).then((r) => listOf<TrustedDex>(r)),
    (rows, reload) => {
      const builder = rows.filter((d) => d.dex !== "");
      const add = async (): Promise<void> => {
        const raw = await promptDialog({ title: "Trust a builder dex", label: "Dex name (as in the coin prefix, e.g. xyz for xyz:SILVER)", placeholder: "xyz", pattern: DEX_NAME });
        const dex = raw?.trim() ?? "";
        if (!DEX_NAME.test(dex)) return;
        if (!(await confirmDialog({ title: `Trust dex "${dex}"?`, message: "Its deployer controls the oracle, mark price, volume and open interest our guards read. Strategies will be able to list and trade its markets. Confirm you know who operates it.", confirmLabel: "Trust dex", danger: true, requireText: dex }))) return;
        const reason = await askReason(`Why is "${dex}" trusted?`, "operator, due diligence done, …");
        if (!reason) return;
        await api.post("/admin/dexes", { dex, reason }, { signal: ctx.signal });
        toast(`Dex "${dex}" trusted.`, "good");
        reload();
      };
      const remove = async (d: TrustedDex): Promise<void> => {
        if (!(await confirmDialog({ title: `Remove dex "${d.dex}"?`, message: "New entries on every market of this dex stop at the next executor tick (exits keep running). Creators can no longer create, upload or list strategies on it.", confirmLabel: "Remove", danger: true, requireText: d.dex }))) return;
        const reason = await askReason(`Remove "${d.dex}"`, "e.g. oracle manipulation suspected");
        if (!reason) return;
        const r = await api.post<{ affected_strategies?: string[]; markets_entries_paused?: string[] }>(`/admin/dexes/${encodeURIComponent(d.dex)}/remove`, { reason }, { signal: ctx.signal });
        const n = (r.affected_strategies ?? []).length;
        toast(`Removed. ${n} strateg${n === 1 ? "y" : "ies"} affected; new entries paused on ${(r.markets_entries_paused ?? []).length} market(s).`, "good");
        reload();
      };
      mount(
        box,
        note("Validator perps (BTC, SOL, …) are always allowed. A builder (HIP-3) dex must be on this list before any strategy can list or trade its markets: its deployer controls the prices and liquidity data our thin-market guards rely on.", "info"),
        panel(
          "Builder dexes",
          builder.length
            ? table<TrustedDex>({
                columns: [
                  { key: "d", label: "Dex", value: (d) => h("span", { class: "mono" }, d.dex), primary: true },
                  { key: "s", label: "Status", value: (d) => (d.active ? badge("trusted", "good") : badge("removed", "bad")) },
                  { key: "r", label: "Reason", value: (d) => h("span", { class: "small break" }, d.active ? d.reason : d.removal_reason ?? "") },
                  { key: "by", label: "By", value: (d) => h("span", { class: "small muted" }, `${d.active ? d.added_by : d.removed_by ?? ""} · ${fmtDateTime(d.active ? d.created_at : d.removed_at ?? d.created_at)}`) },
                  { key: "a", label: "", value: (d) => (d.active ? button("Remove", { kind: "danger", onClick: () => remove(d) }) : button("Trust again", { kind: "ghost", onClick: () => void add() })) },
                ],
                rows: builder,
                rowKey: (d) => d.dex,
              })
            : h("p", { class: "small muted" }, "No builder dexes trusted — only validator perps can trade."),
          h("div", { class: "btns" }, button("Trust a dex…", { kind: "primary", onClick: () => add() })),
        ),
      );
    },
  );
}

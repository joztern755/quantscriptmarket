// #/admin[/:tab] — admin console. Every action is maker-checker / audit-logged server-side and needs step-up
// (core api retries after step-up automatically). UI role check only; the API enforces role=admin.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, emptyState, note, table, tabs, button, toast, confirmDialog, promptDialog, badge, kv, field, type Column } from "../core/ui.js";
import { api, newIdempotencyKey } from "../core/api.js";
import { connectWallet, getConnectedWallet } from "../core/wallet.js";
import { usdSend } from "../core/hl.js";
import { fmtUsd, fmtBps, fmtDateTime, fmtRelative, shortAddr } from "../core/format.js";
import type { Backtest } from "./_shared/types.js";
import { backtestPanel } from "./_shared/backtest.js";
import { ensurePageCss, listOf, isAbortError, pageHead, panel, isAddress, usdInput, pctToBps, bpsToPctInput, isRec, errMessage } from "./_shared/util.js";

export const title = "Admin";

const TABS = [
  { key: "flags", label: "Kill switches" },
  { key: "payouts", label: "Payouts" },
  { key: "review", label: "Strategy review" },
  { key: "prices", label: "In-house prices" },
  { key: "alerts", label: "Alerts" },
  { key: "recon", label: "Reconciliation" },
  { key: "users", label: "Users" },
];

const MARKETS = ["BTC", "SOL", "HYPE", "xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:BRENTOIL"];

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
      pageHead("Admin", "Operations console", "Every action is audit-logged. Lifting a kill switch, approving payouts and similar actions need a second, different admin."),
      tabs(TABS, tab, (k) => ctx.navigate(k === "flags" ? "/admin" : `/admin/${k}`)),
      body,
    ),
  );
  const fn = { flags: flagsTab, payouts: payoutsTab, review: reviewTab, prices: pricesTab, alerts: alertsTab, recon: reconTab, users: usersTab }[tab as "flags"] ?? flagsTab;
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

const isMine = (ctx: PageContext, who: unknown): boolean => typeof who === "string" && !!ctx.me && (who === ctx.me.id || who === ctx.me.email);

// ------------------------------------------------------------------------------------------ flags
interface Flag {
  key: string;
  value: unknown;
  updated_by?: string | null;
  updated_at?: string | null;
}
interface PendingFlag {
  id: string;
  key: string;
  value: unknown;
  requested_by: string;
  requested_at?: string;
  reason?: string | null;
}

function flagOn(v: unknown): boolean {
  return v === true || (isRec(v) && v.enabled === true) || v === "true";
}

async function flagsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<unknown>("/admin/flags", { signal: ctx.signal }),
    (res, reload) => {
      const flags = listOf<Flag>(res, "flags");
      const pending = listOf<PendingFlag>(isRec(res) ? res.pending : [], "pending");
      const byKey = new Map(flags.map((f) => [f.key, f]));
      const keys = ["kill_switch_global", "new_entries_paused", ...MARKETS.map((m) => `kill_switch_market:${m}`)];
      for (const f of flags) if (!keys.includes(f.key)) keys.push(f.key);
      const set = async (key: string, on: boolean): Promise<void> => {
        const reason = await promptDialog({ title: on ? `Engage ${key}` : `Request to lift ${key}`, label: "Reason (audit log)", placeholder: "e.g. oracle divergence on xyz:SILVER", submitLabel: on ? "Engage now" : "Request lift" });
        if (!reason || reason.trim().length < 4) return;
        if (!on && !(await confirmDialog({ title: "Lift kill switch?", message: "Lifting requires approval by a second admin. Trading resumes only after approval.", confirmLabel: "Request lift", danger: true, requireText: "LIFT" }))) return;
        const r = await api.post<Record<string, unknown>>("/admin/flags", { key, value: on, reason: reason.trim() }, { signal: ctx.signal });
        toast(r && r.status === "pending" ? "Request created — waiting for a second admin." : on ? "Engaged." : "Updated.", "good");
        reload();
      };
      mount(
        box,
        note("Engaging a kill switch takes effect immediately. Lifting one creates a request another admin must approve (maker-checker).", "info"),
        panel(
          "Pending approvals",
          pending.length
            ? table<PendingFlag>({
                columns: [
                  { key: "k", label: "Flag", value: (p) => h("span", { class: "mono break" }, p.key), primary: true },
                  { key: "v", label: "New value", value: (p) => (flagOn(p.value) ? badge("ON", "bad") : badge("OFF", "good")) },
                  { key: "by", label: "Requested by", value: (p) => `${p.requested_by}${p.requested_at ? " · " + fmtRelative(p.requested_at) : ""}` },
                  { key: "r", label: "Reason", value: (p) => h("span", { class: "break" }, p.reason ?? "") },
                  {
                    key: "a",
                    label: "",
                    value: (p) =>
                      isMine(ctx, p.requested_by)
                        ? h("span", { class: "small muted" }, "Needs another admin")
                        : h(
                            "div",
                            { class: "btns" },
                            button("Approve", {
                              kind: "primary",
                              onClick: async () => {
                                if (!(await confirmDialog({ title: `Approve ${p.key} → ${flagOn(p.value) ? "ON" : "OFF"}?`, message: "You are the checker for this change.", confirmLabel: "Approve", danger: !flagOn(p.value) }))) return;
                                await api.post(`/admin/flags/pending/${encodeURIComponent(p.id)}/approve`, {}, { signal: ctx.signal });
                                toast("Approved.", "good");
                                reload();
                              },
                            }),
                            button("Reject", { kind: "ghost", onClick: async () => { await api.post(`/admin/flags/pending/${encodeURIComponent(p.id)}/reject`, {}, { signal: ctx.signal }); reload(); } }),
                          ),
                  },
                ],
                rows: pending,
                rowKey: (p) => p.id,
              })
            : h("p", { class: "small muted" }, "Nothing waiting."),
        ),
        panel(
          "Switches",
          ...keys.map((k) => {
            const f = byKey.get(k);
            const on = flagOn(f?.value);
            const hasPending = pending.some((p) => p.key === k);
            return h(
              "div",
              { class: "kill-row" },
              h("div", { class: "stack tight" }, h("span", { class: "k" }, k), h("span", { class: "small muted" }, f?.updated_by ? `by ${f.updated_by}${f.updated_at ? " · " + fmtDateTime(f.updated_at) : ""}` : "never set")),
              h(
                "div",
                { class: "btns" },
                on ? badge("ENGAGED", "bad") : badge("off", "muted"),
                on
                  ? button(hasPending ? "Lift requested" : "Request lift", { kind: "ghost", disabled: hasPending, onClick: () => set(k, false) })
                  : button("Engage", { kind: "danger", onClick: () => set(k, true) }),
              ),
            );
          }),
          h(
            "div",
            { class: "btns" },
            button("Other market…", {
              kind: "ghost",
              onClick: async () => {
                const coin = await promptDialog({ title: "Kill switch for market", label: "Coin", placeholder: "xyz:SILVER", pattern: /^(?:[a-z0-9]{1,12}:)?[A-Za-z0-9]{1,20}$/ });
                if (coin) await set(`kill_switch_market:${coin.trim()}`, true);
              },
            }),
          ),
        ),
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ payouts
interface Payout {
  id: string;
  kind?: string; // payout | withdrawal
  beneficiary?: string;
  beneficiary_email?: string | null;
  amount_micro: number;
  to_address: string;
  status: string; // requested | approved_1 | approved_2 | sent | rejected
  maker_admin?: string | null;
  checker_admin?: string | null;
  created_at?: string;
  tx_hash?: string | null;
}

async function payoutsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<unknown>("/admin/payouts", { signal: ctx.signal }).then((r) => listOf<Payout>(r, "payouts", "items")),
    (rows, reload) => {
      const open = rows.filter((p) => p.status !== "sent" && p.status !== "rejected");
      const done = rows.filter((p) => p.status === "sent" || p.status === "rejected");
      const action = (p: Payout): HTMLElement => {
        if (p.status === "requested" || p.status === "approved_1") {
          const mine = isMine(ctx, p.maker_admin);
          if (p.status === "approved_1" && mine) return h("span", { class: "small muted" }, "Waiting for a second admin");
          return h(
            "div",
            { class: "btns" },
            button(p.status === "requested" ? "Approve (1st)" : "Approve (2nd)", {
              kind: "primary",
              onClick: async () => {
                if (!(await confirmDialog({ title: "Approve payout?", message: kv([["Amount", fmtUsd(p.amount_micro)], ["To", h("span", { class: "mono break" }, p.to_address)], ["Beneficiary", p.beneficiary_email ?? p.beneficiary ?? "—"]]), confirmLabel: "Approve" }))) return;
                await api.post(`/admin/payouts/${encodeURIComponent(p.id)}/approve`, {}, { signal: ctx.signal });
                toast("Approved.", "good");
                reload();
              },
            }),
            button("Reject", {
              kind: "ghost",
              onClick: async () => {
                const reason = await promptDialog({ title: "Reject payout", label: "Reason" });
                if (!reason) return;
                await api.post(`/admin/payouts/${encodeURIComponent(p.id)}/reject`, { reason }, { signal: ctx.signal });
                reload();
              },
            }),
          );
        }
        if (p.status === "approved_2") {
          return button("Sign & send (hardware wallet)", {
            kind: "primary",
            onClick: async () => {
              if (!isAddress(p.to_address)) throw new Error("Invalid destination address.");
              const w = getConnectedWallet() ?? (await connectWallet());
              if (!w) return;
              if (!(await confirmDialog({ title: "Send USDC from treasury?", message: kv([["Amount", fmtUsd(p.amount_micro)], ["To", h("span", { class: "mono break" }, p.to_address)], ["From", h("span", { class: "mono break" }, w.address)]]), confirmLabel: "Sign in wallet", danger: true }))) return;
              const td = await api.post<Record<string, unknown>>(`/admin/payouts/${encodeURIComponent(p.id)}/typed-data`, { from_address: w.address }, { signal: ctx.signal });
              const r = await usdSend(w, { destination: p.to_address, amountMicro: p.amount_micro, serverTypedData: td.typed_data ?? td.typedData, expectDestination: p.to_address });
              if (!r.ok) throw new Error(`Hyperliquid rejected the transfer: ${r.error ?? "unknown error"}`);
              await api.post(`/admin/payouts/${encodeURIComponent(p.id)}/sent`, { from_address: w.address }, { signal: ctx.signal, idempotencyKey: `payout-sent-${p.id}` });
              toast("Sent.", "good");
              reload();
            },
          });
        }
        return h("span", { class: "small muted" }, p.tx_hash ? shortAddr(p.tx_hash, 10, 6) : p.status);
      };
      const cols: Column<Payout>[] = [
        { key: "who", label: "Beneficiary", value: (p) => h("span", { class: "break" }, `${p.kind ?? "payout"} · ${p.beneficiary_email ?? p.beneficiary ?? "—"}`), primary: true },
        { key: "amt", label: "Amount", value: (p) => fmtUsd(p.amount_micro), align: "right", mono: true },
        { key: "to", label: "To", value: (p) => h("span", { class: "mono", title: p.to_address }, shortAddr(p.to_address)) },
        { key: "st", label: "Status", value: (p) => badge(p.status.replace(/_/g, " "), p.status === "sent" ? "good" : p.status === "rejected" ? "bad" : "warn") },
        { key: "mk", label: "Maker / checker", value: (p) => `${p.maker_admin ?? "—"} / ${p.checker_admin ?? "—"}`, hideOnMobile: true },
        { key: "at", label: "Requested", value: (p) => (p.created_at ? fmtRelative(p.created_at) : "—"), hideOnMobile: true },
        { key: "a", label: "", value: action },
      ];
      mount(
        box,
        note("Two different admins must approve before sending. The treasury key never touches a server: the final UsdSend is signed here with the treasury hardware wallet.", "info"),
        panel("Queue", table({ columns: cols, rows: open, rowKey: (p) => p.id, empty: "Queue is empty." })),
        panel("Recent", table({ columns: cols.filter((c) => c.key !== "a").concat([{ key: "tx", label: "Tx", value: (p) => (p.tx_hash ? h("span", { class: "mono" }, shortAddr(p.tx_hash, 10, 6)) : "—") }]), rows: done.slice(0, 50), rowKey: (p) => p.id, empty: "Nothing yet." })),
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ review
interface ReviewItem {
  id: string;
  name: string;
  slug?: string;
  owner_email?: string | null;
  owner_kyc_status?: string | null;
  markets?: string[];
  description?: string | null;
  price_monthly_micro?: number;
  profit_share_bps?: number;
  status: string;
  version?: { id: string; version: number; code_hash?: string; source?: string; backtest?: Backtest | null; submitted_at?: string } | null;
}

async function reviewTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<unknown>("/admin/strategies?status=review", { signal: ctx.signal }).then((r) => listOf<ReviewItem>(r, "strategies")),
    (rows, reload) => {
      if (!rows.length) {
        mount(box, emptyState("No strategies waiting for review"));
        return;
      }
      mount(
        box,
        ...rows.map((s) => {
          const decide = async (decision: "approve" | "reject"): Promise<void> => {
            const noteText = await promptDialog({ title: decision === "approve" ? `Approve ${s.name}` : `Reject ${s.name}`, label: "Review note (sent to creator, audit-logged)" });
            if (noteText === null) return;
            if (decision === "approve" && !(await confirmDialog({ title: "List this version?", message: "The strategy becomes visible and subscribable. A new version resets its live record.", confirmLabel: "Approve & list" }))) return;
            await api.post(`/admin/strategies/${encodeURIComponent(s.id)}/review`, { version_id: s.version?.id, decision, note: noteText }, { signal: ctx.signal });
            toast(decision === "approve" ? "Approved." : "Rejected.", "good");
            reload();
          };
          return panel(
            h("div", { class: "row between w-full" }, h("h2", null, s.name, s.version ? ` · v${s.version.version}` : ""), badge(s.owner_kyc_status === "verified" ? "KYC verified" : `KYC ${s.owner_kyc_status ?? "unknown"}`, s.owner_kyc_status === "verified" ? "good" : "bad")),
            kv([
              ["Creator", s.owner_email ?? "—"],
              ["Markets", (s.markets ?? []).join(", ") || "—"],
              ["Price / profit share", `${fmtUsd(s.price_monthly_micro ?? 0)} / ${fmtBps(s.profit_share_bps ?? 0)}`],
              ["Source", s.version?.source ?? "—"],
              ["Code hash", h("span", { class: "mono break" }, s.version?.code_hash ?? "—")],
              ["Submitted", s.version?.submitted_at ? fmtDateTime(s.version.submitted_at) : "—"],
            ]),
            s.description ? h("p", { class: "small break" }, s.description) : null,
            backtestPanel(s.version?.backtest ?? null),
            h("div", { class: "btns" }, button("Approve & list", { kind: "primary", onClick: () => decide("approve") }), button("Reject", { kind: "danger", onClick: () => decide("reject") })),
          );
        }),
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ in-house prices
async function pricesTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<unknown>("/admin/strategies?in_house=true", { signal: ctx.signal }).then((r) => listOf<ReviewItem>(r, "strategies")),
    (rows, reload) => {
      if (!rows.length) {
        mount(box, emptyState("No in-house strategies"));
        return;
      }
      mount(
        box,
        note("Price changes apply from each subscriber's next renewal. Changes are audit-logged and need step-up.", "info"),
        ...rows.map((s) => {
          const price = usdInput({ value: String((s.price_monthly_micro ?? 0) / 1_000_000) });
          const ps = h("input", { type: "text", inputmode: "decimal", value: bpsToPctInput(s.profit_share_bps ?? 0) });
          const statusSel = h("select", null, ...["draft", "listed", "paused", "delisted"].map((x) => h("option", { value: x }, x)));
          statusSel.value = s.status;
          const key = newIdempotencyKey();
          return panel(
            h("div", { class: "row between w-full" }, h("h2", null, s.name), badge(s.status, s.status === "listed" ? "good" : "muted")),
            h("div", { class: "form-grid two-col" }, field("Price / month (USD)", price.el), field("Profit share %", ps), field("Status", statusSel)),
            h(
              "div",
              { class: "btns" },
              button("Save", {
                kind: "primary",
                onClick: async () => {
                  const p = price.micro() ?? (price.el.value.trim() === "0" ? 0 : null);
                  const bps = pctToBps(ps.value);
                  if (p === null) return toast("Invalid price.", "warn");
                  if (bps === null) return toast("Invalid profit share.", "warn");
                  if (!(await confirmDialog({ title: `Update ${s.name}?`, message: kv([["Price / month", fmtUsd(p)], ["Profit share", fmtBps(bps)], ["Status", statusSel.value]]), confirmLabel: "Save" }))) return;
                  await api.patch(`/admin/strategies/${encodeURIComponent(s.id)}`, { price_monthly_micro: p, profit_share_bps: bps, status: statusSel.value }, { signal: ctx.signal, idempotencyKey: key });
                  toast("Saved.", "good");
                  reload();
                },
              }),
            ),
          );
        }),
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ alerts
interface AdminAlert {
  id: string;
  severity: string;
  kind: string;
  user_id?: string | null;
  payload?: Record<string, unknown> | null;
  message?: string | null;
  created_at: string;
  acked_at?: string | null;
  acked_by?: string | null;
}

async function alertsTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  let sev = ctx.query.get("severity") ?? "";
  const sel = h("select", { "aria-label": "Severity" }, h("option", { value: "" }, "All severities"), h("option", { value: "critical" }, "Critical"), h("option", { value: "warn" }, "Warn"), h("option", { value: "info" }, "Info"));
  sel.value = sev;
  const box = h("div", { class: "stack" });
  const load = (): Promise<void> =>
    loadInto(
      box,
      ctx,
      () => api.get<unknown>(`/admin/alerts${sev ? `?severity=${encodeURIComponent(sev)}` : ""}`, { signal: ctx.signal }).then((r) => listOf<AdminAlert>(r, "alerts")),
      (rows, reload) => {
        mount(
          box,
          table<AdminAlert>({
            columns: [
              { key: "t", label: "When", value: (a) => h("span", { title: fmtDateTime(a.created_at) }, fmtRelative(a.created_at)), primary: true },
              { key: "s", label: "Severity", value: (a) => badge(a.severity, a.severity === "critical" ? "bad" : a.severity === "warn" ? "warn" : "info") },
              { key: "k", label: "Kind", value: (a) => h("span", { class: "mono" }, a.kind) },
              { key: "m", label: "Details", value: (a) => h("span", { class: "small break" }, a.message ?? (a.payload ? JSON.stringify(a.payload).slice(0, 300) : "")) },
              {
                key: "a",
                label: "",
                value: (a) =>
                  a.acked_at
                    ? h("span", { class: "small muted" }, `acked${a.acked_by ? " by " + a.acked_by : ""}`)
                    : button("Acknowledge", { kind: "ghost", onClick: async () => { await api.post(`/admin/alerts/${encodeURIComponent(a.id)}/ack`, {}, { signal: ctx.signal }); reload(); } }),
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
  mount(body, h("div", { class: "filters" }, h("div", { class: "field" }, h("span", { class: "fl" }, "Severity"), sel)), box);
  await load();
}

// ------------------------------------------------------------------------------------------ reconciliation
interface ReconCheck {
  name: string;
  ledger_micro?: number | null;
  onchain_micro?: number | null;
  diff_micro?: number | null;
  ok: boolean;
  note?: string | null;
}

async function reconTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const box = h("div", { class: "stack" });
  mount(body, box);
  await loadInto(
    box,
    ctx,
    () => api.get<Record<string, unknown>>("/admin/reconciliation", { signal: ctx.signal }),
    (res) => {
      const checks = listOf<ReconCheck>(res, "checks", "items");
      const bad = checks.filter((c) => !c.ok);
      mount(
        box,
        h("p", { class: "small muted" }, typeof res.generated_at === "string" ? `Generated ${fmtDateTime(res.generated_at)} · ` : "", "Daily: Σ builder-fee ledger vs Hyperliquid builder rewards; treasury USDC vs ledger; positions vs expected. Mismatch > $1 raises a critical alert."),
        bad.length ? note(`${bad.length} check${bad.length > 1 ? "s" : ""} failing.`, "bad") : note("All checks passing.", "info"),
        table<ReconCheck>({
          columns: [
            { key: "n", label: "Check", value: (c) => c.name, primary: true },
            { key: "l", label: "Ledger", value: (c) => (typeof c.ledger_micro === "number" ? fmtUsd(c.ledger_micro) : "—"), align: "right", mono: true },
            { key: "o", label: "On-chain", value: (c) => (typeof c.onchain_micro === "number" ? fmtUsd(c.onchain_micro) : "—"), align: "right", mono: true },
            { key: "d", label: "Diff", value: (c) => (typeof c.diff_micro === "number" ? h("span", { class: c.diff_micro === 0 ? "" : "neg" }, fmtUsd(c.diff_micro, { sign: true })) : "—"), align: "right", mono: true },
            { key: "s", label: "Status", value: (c) => (c.ok ? badge("ok", "good") : badge("mismatch", "bad")) },
            { key: "x", label: "Note", value: (c) => h("span", { class: "small break" }, c.note ?? ""), hideOnMobile: true },
          ],
          rows: checks,
          rowKey: (c) => c.name,
          empty: "No reconciliation report yet.",
        }),
      );
    },
  );
}

// ------------------------------------------------------------------------------------------ users
interface AdminUser {
  id: string;
  email?: string | null;
  display_name?: string | null;
  role: string;
  plan?: string;
  status: string;
  kyc_status?: string | null;
  created_at?: string;
}

async function usersTab(body: HTMLElement, ctx: PageContext): Promise<void> {
  const q = h("input", { type: "search", placeholder: "Email, user id or wallet 0x…", "aria-label": "Search users", value: ctx.query.get("q") ?? "" });
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
      () => api.get<unknown>(`/admin/users?q=${encodeURIComponent(term)}`, { signal: ctx.signal }).then((r) => listOf<AdminUser>(r, "users")),
      (rows, reload) => {
        mount(
          box,
          table<AdminUser>({
            columns: [
              { key: "e", label: "User", value: (u) => h("span", { class: "break" }, u.email ?? u.display_name ?? u.id), primary: true },
              { key: "r", label: "Role", value: (u) => u.role },
              { key: "p", label: "Plan", value: (u) => u.plan ?? "—", hideOnMobile: true },
              { key: "k", label: "KYC", value: (u) => u.kyc_status ?? "—", hideOnMobile: true },
              { key: "s", label: "Status", value: (u) => badge(u.status, u.status === "active" ? "good" : "bad") },
              { key: "c", label: "Joined", value: (u) => (u.created_at ? fmtRelative(u.created_at) : "—"), hideOnMobile: true },
              {
                key: "a",
                label: "",
                value: (u) =>
                  u.id === ctx.me?.id
                    ? h("span", { class: "small muted" }, "you")
                    : u.status === "suspended"
                      ? button("Unsuspend", {
                          kind: "ghost",
                          onClick: async () => {
                            const reason = await promptDialog({ title: `Unsuspend ${u.email ?? u.id}`, label: "Reason (audit log)" });
                            if (!reason) return;
                            await api.post(`/admin/users/${encodeURIComponent(u.id)}/unsuspend`, { reason }, { signal: ctx.signal });
                            reload();
                          },
                        })
                      : button("Suspend", {
                          kind: "danger",
                          onClick: async () => {
                            const reason = await promptDialog({ title: `Suspend ${u.email ?? u.id}`, label: "Reason (audit log)" });
                            if (!reason) return;
                            if (!(await confirmDialog({ title: "Suspend user?", message: "The user is blocked from account actions. This is audit-logged and reversible by an admin.", confirmLabel: "Suspend", danger: true, requireText: "SUSPEND" }))) return;
                            await api.post(`/admin/users/${encodeURIComponent(u.id)}/suspend`, { reason }, { signal: ctx.signal });
                            toast("Suspended.", "good");
                            reload();
                          },
                        }),
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

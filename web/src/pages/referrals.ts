// #/referrals — referral code, share link, tier progress toward Partner / Elite, earnings.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, note, stat, copyButton, table, type Column } from "../core/ui.js";
import { api } from "../core/api.js";
import { appConfig } from "../core/config.js";
import { fmtUsd, fmtBps, fmtNum, fmtDate } from "../core/format.js";
import type { ReferralInfo } from "./_shared/types.js";
import { DEFAULT_REFERRAL_TIERS, type ReferralTierCfg } from "./_shared/fees.js";
import { ensurePageCss, isAbortError, pageHead, panel, progressBar, listOf, isRec } from "./_shared/util.js";

export const title = "Referrals";

interface ReferralResponse extends ReferralInfo {
  tiers?: ReferralTierCfg[];
  history?: { created_at: string; amount_micro: number; memo?: string | null }[];
  earnings?: { created_at: string; amount_micro: number; memo?: string | null }[];
}

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const body = h("div", { class: "stack" }, skeleton(8));
  mount(
    root,
    h(
      "div",
      { class: "stack" },
      pageHead("Referrals", "Invite traders, share the builder fee", "You earn a share of the referral pool (20% of the builder fee) on trades placed for users you refer. Your share grows with your tier."),
      body,
    ),
  );
  const load = async (): Promise<void> => {
    mount(body, skeleton(8));
    try {
      const r = await api.get<ReferralResponse>("/referrals", { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      draw(body, r);
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(body, errorState(err, () => void load()));
    }
  };
  await load();
}

function draw(body: HTMLElement, r: ReferralResponse): void {
  const origin = appConfig().siteOrigin || "https://aijalon.trade";
  const code = typeof r.code === "string" && /^[A-Za-z0-9_-]{1,32}$/.test(r.code) ? r.code : "";
  const link = code ? `${origin}/?ref=${encodeURIComponent(code)}` : "";
  const tiers = (Array.isArray(r.tiers) && r.tiers.length ? r.tiers : DEFAULT_REFERRAL_TIERS).slice().sort((a, b) => a.share_of_pool_bps - b.share_of_pool_bps);
  const curIdx = Math.max(0, tiers.findIndex((t) => t.name.toLowerCase() === String(r.tier ?? "").toLowerCase()));
  const cur = tiers[curIdx];
  const users = r.active_users_30d ?? 0;
  const notional = r.notional_30d_micro ?? 0;
  const history = listOf<{ created_at: string; amount_micro: number; memo?: string | null }>(isRec(r) ? (r.history ?? r.earnings ?? []) : []);
  const cap = (s: string): string => s.charAt(0).toUpperCase() + s.slice(1);

  mount(
    body,
    panel(
      "Your link",
      code
        ? h(
            "div",
            { class: "stack tight" },
            h("div", { class: "row" }, h("span", { class: "eyebrow" }, "Code"), h("b", { class: "mono" }, code), copyButton(code, "Copy code")),
            h("div", { class: "row" }, h("span", { class: "mono small break" }, link), copyButton(link, "Copy link")),
            h("p", { class: "small muted" }, "A referral is recorded the first time someone visits with your link (valid 30 days) and is fixed once they sign up. Self-referrals are blocked."),
          )
        : note("Your referral code isn't available yet. Try again shortly.", "info"),
    ),
    h(
      "div",
      { class: "stats" },
      stat("Tier", cap(cur?.name ?? String(r.tier ?? "starter")), cur ? `${fmtBps(cur.share_of_pool_bps)} of the pool` : undefined),
      stat("Active referred users (30d)", fmtNum(users, 0)),
      stat("Referred notional (30d)", fmtUsd(notional, { compact: true })),
      stat("Earned (30d)", fmtUsd(r.earnings_30d_micro ?? 0)),
      stat("Earned (all time)", fmtUsd(r.earnings_total_micro ?? 0)),
      stat("Payable", fmtUsd(r.payable_micro ?? 0)),
    ),
    panel(
      "Tier progress",
      h("p", { class: "small muted" }, "Tiers are evaluated daily on the trailing 30 days. Reach either condition to move up."),
      ...tiers
        .filter((_, i) => i > 0)
        .map((t) => {
          const reached = tiers.indexOf(t) <= curIdx;
          return h(
            "div",
            { class: "stack tight", style: { paddingTop: "8px" } },
            h("div", { class: "row between" }, h("b", null, `${cap(t.name)} — ${fmtBps(t.share_of_pool_bps)} of the pool`), reached ? h("span", { class: "pill good" }, "Reached") : null),
            progressBar(users, Math.max(1, t.min_active_users), `${fmtNum(users, 0)} / ${fmtNum(t.min_active_users, 0)} active referred users`),
            h("div", { class: "small muted center" }, "or"),
            progressBar(notional, Math.max(1, t.min_notional_30d_micro), `${fmtUsd(notional, { compact: true })} / ${fmtUsd(t.min_notional_30d_micro, { compact: true })} referred notional (30d)`),
          );
        }),
    ),
    panel(
      "Earnings history",
      table<{ created_at: string; amount_micro: number; memo?: string | null }>({
        columns: [
          { key: "date", label: "Date", value: (x) => fmtDate(x.created_at), primary: true },
          { key: "memo", label: "Details", value: (x) => x.memo ?? "Referral reward" },
          { key: "amt", label: "Amount", value: (x) => fmtUsd(x.amount_micro, { sign: true }), align: "right", mono: true },
        ] as Column<{ created_at: string; amount_micro: number; memo?: string | null }>[],
        rows: history,
        empty: "No referral earnings yet.",
      }),
    ),
    note("Referral rewards are paid from the platform's builder-fee revenue. Do not promise returns when sharing your link; strategies can lose money.", "info"),
  );
}

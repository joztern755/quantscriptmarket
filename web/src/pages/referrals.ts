// #/referrals — GET /v1/referrals (ReferralsOut): code, share link, tier progress (tiers from public config),
// earnings; payout request (POST /v1/payouts source "referrer").
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, note, stat, copyButton } from "../core/ui.js";
import { api, publicConfig, type PublicConfig } from "../core/api.js";
import { fmtUsd, fmtBps, fmtNum } from "../core/format.js";
import type { ReferralInfo } from "./_shared/types.js";
import { payoutForm } from "./_shared/payout.js";
import { ensurePageCss, isAbortError, pageHead, panel, progressBar } from "./_shared/util.js";

export const title = "Referrals";

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
      const [r, cfg] = await Promise.all([api.get<ReferralInfo>("/referrals", { signal: ctx.signal }), publicConfig()]);
      if (!ctx.isCurrent()) return;
      draw(body, ctx, r, cfg, () => void load());
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(body, errorState(err, () => void load()));
    }
  };
  await load();
}

function draw(body: HTMLElement, ctx: PageContext, r: ReferralInfo, cfg: PublicConfig, reload: () => void): void {
  const code = /^[A-Za-z0-9_-]{1,32}$/.test(r.code) ? r.code : "";
  const link = code && /^https:\/\/[^\s]+$/.test(r.link) ? r.link : "";
  const tiers = cfg.referral_tiers.slice().sort((a, b) => a.share_of_pool_bps - b.share_of_pool_bps);
  const curIdx = Math.max(0, tiers.findIndex((t) => t.name.toLowerCase() === r.tier.toLowerCase()));
  const users = r.active_referred_users_30d;
  const notional = r.referred_notional_30d_micro;
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
            link ? h("div", { class: "row" }, h("span", { class: "mono small break" }, link), copyButton(link, "Copy link")) : null,
            h("p", { class: "small muted" }, "A referral is recorded the first time someone visits with your link (valid 30 days) and is fixed once they sign up. Self-referrals are blocked."),
          )
        : note("Your referral code isn't available yet. Try again shortly.", "info"),
    ),
    h(
      "div",
      { class: "stats" },
      stat("Tier", cap(r.tier), `${fmtBps(r.share_of_pool_bps)} of the pool`),
      stat("Active referred users (30d)", fmtNum(users, 0)),
      stat("Referred notional (30d)", fmtUsd(notional, { compact: true })),
      stat("Referred users (all time)", fmtNum(r.referred_users_total, 0)),
      stat("Earned (all time)", fmtUsd(r.earnings_total_micro)),
      stat("Payable", fmtUsd(r.earnings_payable_micro)),
    ),
    panel(
      "Tier progress",
      h("p", { class: "small muted" }, "Tiers are evaluated daily on the trailing 30 days. Reach either condition to move up."),
      r.next_tier ? h("p", { class: "small" }, `Next: ${cap(r.next_tier.name)} — ${fmtBps(r.next_tier.share_of_pool_bps)} of the pool.`) : h("p", { class: "small" }, "You are on the top tier."),
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
    panel("Request a payout", payoutForm(ctx, cfg, "referrer", r.earnings_payable_micro, reload)),
    note("Referral rewards are paid from the platform's builder-fee revenue. Do not promise returns when sharing your link; strategies can lose money.", "info"),
  );
}

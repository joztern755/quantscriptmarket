// "Request payout" form shared by Creator Studio earnings and Referrals: POST /v1/payouts
// {amount_micro, source: "creator"|"referrer", to_address} (step-up + Idempotency-Key; maker-checker by two admins).
import { h, mount, button, field, note, toast, confirmDialog, kv } from "../../core/ui.js";
import { api, newIdempotencyKey, type PublicConfig } from "../../core/api.js";
import { fmtUsd } from "../../core/format.js";
import type { PageContext } from "../../core/router.js";
import { usdInput, isAddress, errCode, errMessage } from "./util.js";
import { addressCheck } from "../../core/addr.js";
import { proveDestination } from "./walletproof.js";

export function payoutForm(ctx: PageContext, cfg: PublicConfig, source: "creator" | "referrer", availableMicro: number, onDone: () => void): HTMLElement {
  const box = h("div", { class: "stack" });
  if (!cfg.features.payouts) {
    mount(box, note("Payouts are not enabled yet during the internal launch phase. Your earnings stay payable.", "info"));
    return box;
  }
  const min = cfg.economics.min_topup_micro;
  const amount = usdInput({ id: `po-${source}-amt`, placeholder: `min ${fmtUsd(min)}` });
  const verified = (ctx.me?.wallets as { address: string; verified_at: string | null }[] | undefined)?.filter((w) => w.verified_at) ?? [];
  const to = h("select", { id: `po-${source}-to` }, ...verified.map((w) => h("option", { value: w.address }, w.address)));
  let key = newIdempotencyKey();
  mount(
    box,
    h("p", { class: "small muted" }, `Available: ${fmtUsd(availableMicro)}. Payouts go only to one of your verified wallets, are approved by two administrators, then sent as USDC on Hyperliquid.`),
    verified.length ? null : note("Verify a wallet first (Subscribe → connect & verify your wallet).", "warn"),
    field("Amount (USD)", amount.el),
    field("To (verified wallet)", to),
    h(
      "div",
      { class: "btns" },
      button("Request payout", {
        disabled: !verified.length,
        onClick: async () => {
          const m = amount.micro();
          if (m === null || m < min) return toast(`Enter at least ${fmtUsd(min)}.`, "warn");
          if (m > availableMicro) return toast(`You can request up to ${fmtUsd(availableMicro)}.`, "warn");
          if (!isAddress(to.value)) return toast("Choose a verified wallet.", "warn");
          if (!(await confirmDialog({ title: "Request payout?", message: h("div", { class: "stack" }, kv([["Amount", fmtUsd(m)]]), addressCheck(to.value, { label: "To (your verified wallet)" }), h("p", { class: "small muted" }, "Next, your wallet asks you to sign a short ownership message WITH this wallet. Administrators re-check that signature before paying.")), confirmLabel: "Sign proof & request" }))) return;
          if (!(await proveDestination(to.value))) return;
          try {
            await api.post("/payouts", { amount_micro: m, source, to_address: to.value.toLowerCase() }, { signal: ctx.signal, idempotencyKey: key });
          } catch (err) {
            const c = errCode(err);
            if (c === "insufficient_balance" || c === "forbidden") return toast(errMessage(err), "warn");
            throw err;
          }
          key = newIdempotencyKey();
          toast("Payout requested. You'll get an alert when it's sent.", "good");
          onDone();
        },
      }),
    ),
  );
  return box;
}

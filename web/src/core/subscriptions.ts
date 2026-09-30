// Subscription cancel flow (SPEC §12, owner decision 30 Sep 2026):
// two explicit choices → first confirm → second confirm restating the consequence → step-up →
// DELETE /v1/subscriptions/{id} {"positions": "close"|"leave"} → "revoke agent" guidance.

import { api } from "./api.js";
import { stepUp } from "./auth.js";
import { button, confirmDialog, h, modal, note, toast } from "./ui.js";

export type CancelMode = "close" | "leave";

export interface CancelTarget {
  id: string;
  strategy_name: string;
  markets: string[];
}

const LABELS: Record<CancelMode, string> = {
  close: "Close positions and cancel",
  leave: "Leave positions open and cancel",
};

function consequence(mode: CancelMode, markets: string[]): string {
  const list = markets.length ? markets.join(", ") : "this strategy's markets";
  return mode === "close"
    ? `We will close your open positions on ${list} at market with IOC orders; slippage and fees apply. The subscription is cancelled when the positions are closed.`
    : "Your positions stay open and we will no longer manage them — you must manage or close them yourself on Hyperliquid.";
}

/** Runs the full cancel flow. Resolves true when the API accepted the cancellation. */
export async function cancelSubscription(sub: CancelTarget, mode: CancelMode): Promise<boolean> {
  const first = await confirmDialog({
    title: LABELS[mode],
    message: h("div", { class: "stack tight" },
      h("p", null, `Cancel your subscription to ${sub.strategy_name}?`),
      h("p", { class: "muted small" }, "Your prepaid period is not refunded. Profit share settles on realized profit up to cancellation.")),
    confirmLabel: "Continue",
    danger: true,
  });
  if (!first) return false;
  const second = await confirmDialog({
    title: "Please confirm again",
    message: h("div", { class: "stack tight" }, note(consequence(mode, sub.markets), "warn")),
    confirmLabel: LABELS[mode],
    danger: true,
  });
  if (!second) return false;
  await stepUp(mode === "close" ? "Closing positions and cancelling needs a fresh sign-in." : "Cancelling a subscription needs a fresh sign-in.");
  await api.del(`/subscriptions/${encodeURIComponent(sub.id)}`, { body: { positions: mode } });
  toast(mode === "close" ? "Closing positions… the subscription will be cancelled once they are closed." : "Subscription cancelled. Your positions are now yours to manage.", "good", 7000);
  showRevokeAgentGuidance();
  return true;
}

/** Two buttons, as required by the owner. `onDone` runs after a successful cancel. */
export function cancelButtons(sub: CancelTarget, onDone?: (mode: CancelMode) => void): HTMLElement {
  const mk = (mode: CancelMode) =>
    button(LABELS[mode], {
      kind: mode === "close" ? "danger" : "plain",
      onClick: async () => {
        if (await cancelSubscription(sub, mode)) onDone?.(mode);
      },
    });
  return h("div", { class: "btns" }, mk("close"), mk("leave"));
}

export function showRevokeAgentGuidance(): void {
  modal({
    title: "Optional: revoke our agent",
    body: h("div", { class: "stack" },
      h("p", null, "Your aijalon agent wallet stays approved on your Hyperliquid account until you remove it. It can trade but can never withdraw or transfer funds. We will not place orders for a cancelled subscription."),
      h("p", null, "To remove it completely: open Hyperliquid, go to the API / agent wallets page, and remove the agent named ", h("b", { class: "mono" }, "aijalon"), ". You can also leave it if you plan to subscribe again."),
      h("p", null, h("a", { href: "https://app.hyperliquid.xyz/API", target: "_blank", rel: "noopener noreferrer" }, "Open Hyperliquid API settings ↗"))),
    actions: [{ label: "Done", kind: "primary" }],
  });
}

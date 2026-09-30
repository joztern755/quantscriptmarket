// Identity verification (KYC) helpers shared by Creator Studio and Referrals. One KYC record per user: a user verified
// once (as creator or referrer) is verified for both; ONE admin confirms (POST /v1/admin/users/{id}/kyc).
//   POST /v1/creator/kyc/session | /v1/referrals/kyc/session → {url ("" when manual), provider, status, manual}
//   409 approved · 409 reason `awaiting_admin` (provider passed, our admin confirms)
import { h, mount, note, badge } from "../../core/ui.js";
import { api, ApiError } from "../../core/api.js";
import { trustAnchors } from "../../core/config.js";
import type { PageContext } from "../../core/router.js";

/** none | pending | provider_approved | approved | rejected (anything else is shown as not started). */
export function kycLabel(status: string | null | undefined): string {
  switch (status) {
    case "approved":
      return "Identity verified";
    case "pending":
      return "Pending review";
    case "provider_approved":
      return "Awaiting admin review";
    case "rejected":
      return "Rejected";
    default:
      return "Not started";
  }
}

export function kycBadge(status: string | null | undefined): HTMLElement {
  const tone = status === "approved" ? "good" : status === "rejected" ? "bad" : "warn";
  return badge(kycLabel(status), tone);
}

/** Verification is under way (nothing for the user to do): pending at the provider / with our team, or passed and
 *  awaiting the admin's confirmation. */
export function kycInProgress(status: string | null | undefined): boolean {
  return status === "pending" || status === "provider_approved";
}

export const KYC_MANUAL_TEXT = "Your identity check will be reviewed by an admin. We'll contact you by email; no documents are uploaded here.";
export const KYC_AWAITING_TEXT = "Your verification passed. An admin will confirm it shortly — nothing more to do.";

/** Starts a KYC session at `path` and either shows the manual-review notice in `msgBox` or redirects to the pinned
 *  provider host (SECURITY L2: exact host from app-config.json, https only, no credentials / port). Returns true when a
 *  notice was shown (the caller may reload its status). */
export async function startKycSession(path: "/creator/kyc/session" | "/referrals/kyc/session", ctx: PageContext, msgBox: HTMLElement): Promise<boolean> {
  let res: { url: string; provider: string; status: string; manual: boolean };
  try {
    res = await api.post<{ url: string; provider: string; status: string; manual: boolean }>(path, {}, { signal: ctx.signal });
  } catch (err) {
    if (err instanceof ApiError && err.status === 409 && err.details?.reason === "awaiting_admin") {
      mount(msgBox, note(KYC_AWAITING_TEXT, "info"));
      return true;
    }
    if (err instanceof ApiError && err.status === 409) {
      mount(msgBox, note(err.message || "Your identity is already verified.", "info"));
      return true;
    }
    throw err;
  }
  if (res.manual) {
    mount(msgBox, note(KYC_MANUAL_TEXT, "info"));
    return true;
  }
  const url = typeof res.url === "string" ? res.url : "";
  let target: URL | null = null;
  try {
    target = new URL(url);
  } catch {
    target = null;
  }
  const allowed = trustAnchors().kycRedirectHosts;
  if (!target || target.protocol !== "https:" || target.username || target.password || target.port || !allowed.includes(target.host.toLowerCase())) {
    throw new Error("Verification could not be started (unexpected verification address). Please contact support.");
  }
  window.location.assign(target.href);
  return false;
}

/** Short explanation shown instead of a usable payout form while KYC is not approved (POST /payouts → 403 kyc_required). */
export function payoutKycBlocked(status: string | null | undefined): HTMLElement {
  return note(
    h(
      "span",
      null,
      h("b", null, "Payout requests are blocked until your identity is verified. "),
      kycInProgress(status) ? "Your verification is in progress; you can request a payout once an admin has approved it." : "Verify your identity above to withdraw your earnings.",
    ),
    "warn",
  );
}

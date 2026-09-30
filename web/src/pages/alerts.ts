// #/alerts — alert settings (SPEC §12 "User alerts on Telegram + email", "Email volume policy"):
// link Telegram (one-time t.me link, page polls until linked), confirm the alert email (account email in one click,
// or a 6-digit code + step-up for another address), per-kind mute switches (mandatory kinds shown locked), and
// "Send test alert".
//   GET   /v1/alerts/settings  → { contacts, prefs: [{kind, label, group, group_label, mandatory, muted, channels}], telegram_bot, email_policy }
//   PATCH /v1/alerts/prefs     { muted: { <kind>: boolean } } → same shape as GET /settings
//   POST  /v1/alerts/test      → { results: { telegram: "sent"|…, email: "sent"|… } }
// Route: core registers { pattern: "/alerts", name: "alerts", access: "user" } (router.ts; nav entry in main.ts, and the
// dashboard's Alerts tab links here). `renderAlertsSettings` can also be embedded without a route.
import type { PageContext } from "../core/router.js";
import { h, mount, skeleton, errorState, note, badge, button, toast, type Child } from "../core/ui.js";
import { api } from "../core/api.js";
import { contactsPanel, parseContacts, type ContactsStatus } from "./_shared/contacts.js";
import { ensurePageCss, isAbortError, isRec, pageHead, panel } from "./_shared/util.js";

export const title = "Alerts";

interface PrefRow {
  kind: string;
  label: string;
  group: string;
  group_label: string;
  mandatory: boolean;
  muted: boolean;
  channels: string[];
}

interface SettingsOut {
  contacts: ContactsStatus;
  prefs: PrefRow[];
  telegram_bot: string | null;
  email_policy: string;
}

const RESULT_TEXT: Record<string, string> = {
  sent: "sent",
  not_linked: "not linked",
  not_verified: "email not confirmed",
  not_configured: "not available right now",
  blocked: "blocked — the bot can't reach you",
  failed: "failed",
  retry_later: "temporarily unavailable, try again shortly",
};

function asPrefs(x: unknown): PrefRow[] {
  if (!isRec(x) || !Array.isArray(x.prefs)) return [];
  return x.prefs.filter(isRec).map((p) => ({
    kind: String(p.kind ?? ""),
    label: String(p.label ?? p.kind ?? ""),
    group: String(p.group ?? ""),
    group_label: String(p.group_label ?? p.group ?? ""),
    mandatory: p.mandatory === true,
    muted: p.muted === true,
    channels: Array.isArray(p.channels) ? p.channels.map(String) : [],
  })).filter((p) => /^[a-z][a-z0-9_]{1,63}$/.test(p.kind));
}

export async function render(root: HTMLElement, ctx: PageContext): Promise<void> {
  ensurePageCss();
  const body = h("div", { class: "stack" }, skeleton(10));
  mount(
    root,
    h(
      "div",
      { class: "stack" },
      pageHead("Alerts", "Telegram & email alerts", "Required before your first subscription starts. Telegram carries every alert; email carries only the mandatory, security and money alerts."),
      body,
    ),
  );
  await renderAlertsSettings(body, ctx);
}

export async function renderAlertsSettings(body: HTMLElement, ctx: Pick<PageContext, "signal" | "isCurrent">): Promise<void> {
  const load = async (): Promise<void> => {
    mount(body, skeleton(10));
    try {
      const s = await api.get<unknown>("/alerts/settings", { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      draw(s);
    } catch (err) {
      if (isAbortError(err) || !ctx.isCurrent()) return;
      mount(body, errorState(err, () => void load()));
    }
  };

  const draw = (raw: unknown): void => {
    const prefs = asPrefs(raw);
    const policy = isRec(raw) && typeof raw.email_policy === "string" ? raw.email_policy : "";
    const initial = isRec(raw) && isRec(raw.contacts) ? parseContacts(raw.contacts) : undefined;
    const contacts = contactsPanel({ initial, signal: ctx.signal });
    mount(
      body,
      panel("Where alerts go", contacts),
      panel(
        "Test",
        h("p", { class: "small muted" }, "Sends a test message to Telegram and to your alert email now."),
        h("div", { class: "btns" }, button("Send test alert", { kind: "primary", onClick: sendTest })),
      ),
      prefsPanel(prefs, policy),
    );
  };

  const sendTest = async (): Promise<void> => {
    const out = await api.post<unknown>("/alerts/test", undefined, { signal: ctx.signal });
    const res = isRec(out) && isRec(out.results) ? out.results : {};
    const tg = String(res.telegram ?? "failed");
    const em = String(res.email ?? "failed");
    const ok = tg === "sent" && em === "sent";
    toast(`Telegram: ${RESULT_TEXT[tg] ?? tg}. Email: ${RESULT_TEXT[em] ?? em}.`, ok ? "good" : "warn", 7000);
  };

  const prefsPanel = (prefs: PrefRow[], policy: string): HTMLElement => {
    const groups = new Map<string, { label: string; rows: PrefRow[] }>();
    for (const p of prefs) {
      const g = groups.get(p.group) ?? { label: p.group_label, rows: [] };
      g.rows.push(p);
      groups.set(p.group, g);
    }
    const sections: Child[] = [];
    for (const [, g] of groups) {
      sections.push(
        h("h3", null, g.label),
        h("div", { class: "stack tight" }, g.rows.map((p) => prefRow(p))),
      );
    }
    return panel(
      "Which alerts you receive",
      policy ? h("p", { class: "small muted" }, policy) : null,
      h("p", { class: "small muted" }, "Muting stops Telegram (and email) delivery for that alert; it still appears in your dashboard. Locked alerts are mandatory and can't be muted."),
      prefs.length ? sections : note("Alert preferences couldn't be loaded.", "info"),
    );
  };

  const prefRow = (p: PrefRow): HTMLElement => {
    const input = h("input", { type: "checkbox", checked: !p.muted || p.mandatory, disabled: p.mandatory, "aria-describedby": `ch-${p.kind}` });
    const channels = p.channels.map((c) => (c === "in_app" ? "in-app" : c === "telegram" ? "Telegram" : "email")).join(" + ");
    if (!p.mandatory) {
      input.addEventListener("change", async () => {
        const muted = !input.checked;
        input.disabled = true;
        try {
          await api.patch<unknown>("/alerts/prefs", { muted: { [p.kind]: muted } }, { signal: ctx.signal });
          p.muted = muted;
          toast(muted ? `Muted: ${p.label}` : `On: ${p.label}`, "info", 2500);
        } catch (err) {
          input.checked = !muted;
          if (!isAbortError(err)) toast(err instanceof Error ? err.message : "Couldn't save.", "bad");
        } finally {
          if (input.isConnected) input.disabled = false;
        }
      });
    }
    return h(
      "label",
      { class: "check" },
      input,
      h(
        "span",
        null,
        p.label,
        " ",
        p.mandatory ? badge("Required", "info") : null,
        h("span", { class: "small muted", id: `ch-${p.kind}` }, ` — ${channels}`),
      ),
    );
  };

  await load();
}

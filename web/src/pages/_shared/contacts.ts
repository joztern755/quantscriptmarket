// Alert contacts (SPEC §12 "User alerts on Telegram + email"): every user must link Telegram AND confirm an email
// before their first subscription can start. Used by pages/alerts.ts (#/alerts) and by the subscribe wizard
// (requireAlertContacts) so the requirement can be met in place, without leaving the wizard.
//
// API (backend/app/api/routers/alerts_settings.py):
//   GET  /v1/alerts/contacts                → ContactsStatus
//   POST /v1/alerts/telegram/link           → { url: "https://t.me/<bot>?start=<one-time token>", expires_at }
//   POST /v1/alerts/email/confirm-account   → ContactsStatus   (409 reason=email_not_verified → use a code)
//   POST /v1/alerts/email/start {email}     → { sent_to, expires_at }   (step-up; the api client handles it)
//   POST /v1/alerts/email/verify {code}     → ContactsStatus   (422 invalid_code {reason, attempts_left})
import { h, mount, button, badge, note, field, modal, toast, type Child } from "../../core/ui.js";
import { api } from "../../core/api.js";
import { fmtDateTime } from "../../core/format.js";
import { qrSvg } from "../../core/qr.js";
import { errCode, errMessage, isAbortError, isRec } from "./util.js";

export type TelegramState = "linked" | "blocked" | "stopped" | "unlinked";

export interface ContactsStatus {
  telegram: { status: TelegramState; linked_at: string | null; lapsed_at: string | null };
  email: { address: string | null; verified: boolean; pending: string | null; account_email: string | null };
  ready: boolean;
  missing: string[];
  entries_allowed: boolean;
  entries_pause_at: string | null;
}

interface LinkOut {
  url: string;
  expires_at: string;
}

const TG_URL = /^https:\/\/t\.me\/[A-Za-z0-9_]{4,32}\?start=[A-Za-z0-9_-]{16,64}$/;
const EMAIL = /^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$/;
const POLL_MS = 3000;
const POLL_FOR_MS = 10 * 60 * 1000;

export function parseContacts(x: unknown): ContactsStatus {
  const r = isRec(x) ? x : {};
  const tg = isRec(r.telegram) ? r.telegram : {};
  const em = isRec(r.email) ? r.email : {};
  const s = String(tg.status ?? "unlinked");
  const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
  return {
    telegram: {
      status: (["linked", "blocked", "stopped", "unlinked"].includes(s) ? s : "unlinked") as TelegramState,
      linked_at: str(tg.linked_at),
      lapsed_at: str(tg.lapsed_at),
    },
    email: { address: str(em.address), verified: em.verified === true, pending: str(em.pending), account_email: str(em.account_email) },
    ready: r.ready === true,
    missing: Array.isArray(r.missing) ? r.missing.map(String) : [],
    entries_allowed: r.entries_allowed === true,
    entries_pause_at: str(r.entries_pause_at),
  };
}

export async function fetchContacts(signal?: AbortSignal): Promise<ContactsStatus> {
  return parseContacts(await api.get<unknown>("/alerts/contacts", { signal }));
}

/** The small exported check the subscribe wizard calls before POST /v1/subscriptions. Resolves true when Telegram is
 *  linked and an email is confirmed (possibly after the user completes both in a dialog); false if they close it. */
export async function requireAlertContacts(signal?: AbortSignal): Promise<boolean> {
  let st: ContactsStatus;
  try {
    st = await fetchContacts(signal);
  } catch (err) {
    if (isAbortError(err)) return false;
    throw err;
  }
  if (st.ready) return true;
  return new Promise<boolean>((resolve) => {
    let ok = false;
    const ctrl = new AbortController();
    const cont = button("Continue", { kind: "primary", disabled: true, onClick: () => { ok = true; m.close(); } });
    const panel = contactsPanel({
      initial: st,
      signal: ctrl.signal,
      onChange: (c) => { cont.disabled = !c.ready; },
    });
    const m = modal({
      title: "Set up alerts first",
      wide: true,
      body: h(
        "div",
        { class: "stack" },
        h("p", { class: "muted" }, "Alerts are required on every plan before a subscription can start: link Telegram and confirm an email. We use them for trades, fee balance, agent expiry and security notices."),
        panel,
        h("div", { class: "btns" }, cont),
      ),
    });
    void m.closed.then(() => {
      ctrl.abort();
      resolve(ok);
    });
  });
}

function tgBadge(s: TelegramState): HTMLElement {
  switch (s) {
    case "linked":
      return badge("Linked", "good");
    case "blocked":
      return badge("Bot blocked", "bad");
    case "stopped":
      return badge("Unlinked (/stop)", "warn");
    default:
      return badge("Not linked", "warn");
  }
}

/** Link-Telegram + confirm-email controls. Re-renders itself on every change and reports it through onChange. */
export function contactsPanel(opts: { initial?: ContactsStatus; signal: AbortSignal; onChange?: (c: ContactsStatus) => void }): HTMLElement {
  const root = h("div", { class: "stack" });
  let st: ContactsStatus | null = opts.initial ?? null;
  let link: LinkOut | null = null;
  let pollTimer = 0;
  let emailMode: "idle" | "enter" | "code" = "idle";
  let codeSentTo = "";
  const status = h("p", { class: "status", "aria-live": "polite" });

  const stopPoll = (): void => {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = 0;
  };
  opts.signal.addEventListener("abort", stopPoll);

  const set = (c: ContactsStatus): void => {
    const hadStatus = st !== null;
    const wasLinked = st?.telegram.status === "linked";
    st = c;
    if (c.telegram.status === "linked") {
      stopPoll();
      link = null;
      if (hadStatus && !wasLinked) toast("Telegram linked.", "good");
    }
    draw();
    opts.onChange?.(c);
  };

  const refresh = async (): Promise<void> => {
    try {
      set(await fetchContacts(opts.signal));
    } catch (err) {
      if (!isAbortError(err)) status.textContent = errMessage(err);
    }
  };

  const startPoll = (): void => {
    stopPoll();
    const until = Date.now() + POLL_FOR_MS;
    pollTimer = window.setInterval(() => {
      if (opts.signal.aborted || !root.isConnected || Date.now() > until) {
        stopPoll();
        return;
      }
      void refresh();
    }, POLL_MS);
  };

  const linkTelegram = async (): Promise<void> => {
    const out = await api.post<LinkOut>("/alerts/telegram/link", undefined, { signal: opts.signal });
    if (!isRec(out) || typeof out.url !== "string" || !TG_URL.test(out.url)) throw new Error("Unexpected link from the server.");
    link = out;
    // Best effort (popup blockers may refuse after an await); the button + QR below always work.
    try {
      window.open(out.url, "_blank", "noopener,noreferrer");
    } catch {
      /* ignore */
    }
    startPoll();
    draw();
  };

  const confirmAccount = async (): Promise<void> => {
    try {
      set(parseContacts(await api.post<unknown>("/alerts/email/confirm-account", undefined, { signal: opts.signal })));
      toast("Email confirmed for alerts.", "good");
    } catch (err) {
      if (errCode(err) === "conflict") {
        emailMode = "enter";
        status.textContent = "Your sign-in email isn't verified by the provider — we'll send it a 6-digit code instead.";
        draw();
        return;
      }
      throw err;
    }
  };

  const sendCode = async (email: string): Promise<void> => {
    if (!EMAIL.test(email)) {
      status.textContent = "Enter a valid email address.";
      return;
    }
    const out = await api.post<unknown>("/alerts/email/start", { email }, { signal: opts.signal });
    codeSentTo = isRec(out) && typeof out.sent_to === "string" ? out.sent_to : email;
    emailMode = "code";
    status.textContent = "";
    draw();
    toast(`Code sent to ${codeSentTo}. It expires in 10 minutes.`, "good");
  };

  const verifyCode = async (code: string): Promise<void> => {
    if (!/^\d{6}$/.test(code)) {
      status.textContent = "The code has 6 digits.";
      return;
    }
    try {
      set(parseContacts(await api.post<unknown>("/alerts/email/verify", { code }, { signal: opts.signal })));
      emailMode = "idle";
      status.textContent = "";
      toast("Email confirmed for alerts.", "good");
      draw();
    } catch (err) {
      if (errCode(err) === "invalid_code") {
        const d = isRec(err) && isRec((err as { details?: unknown }).details) ? ((err as { details: Record<string, unknown> }).details) : {};
        const left = typeof d.attempts_left === "number" ? d.attempts_left : null;
        status.textContent = errMessage(err) + (left !== null && left > 0 ? ` (${left} attempt${left === 1 ? "" : "s"} left)` : "");
        if (d.reason === "too_many_attempts" || d.reason === "expired" || d.reason === "no_code") {
          emailMode = "enter";
          draw();
        }
        return;
      }
      throw err;
    }
  };

  function telegramSection(c: ContactsStatus): Child {
    const t = c.telegram;
    const rows: Child[] = [h("div", { class: "row between" }, h("h3", null, "Telegram"), tgBadge(t.status))];
    if (t.status === "linked") {
      rows.push(h("p", { class: "small muted" }, "Every alert is sent here. To unlink, send /stop to the bot."));
    } else {
      if (t.status === "blocked" || t.status === "stopped") {
        rows.push(note(
          [
            "We can't reach you on Telegram. ",
            c.entries_pause_at ? `New entries on your subscriptions pause at ${fmtDateTime(c.entries_pause_at)} unless you link again. ` : "",
            t.status === "blocked" ? "Unblock the bot (or send /start to it) or create a new link." : "Create a new link.",
          ],
          "warn",
        ));
      }
      rows.push(h("p", { class: "small muted" }, "Open our bot with a one-time link (valid 10 minutes) and press Start. This page updates by itself once it's linked."));
      if (link) {
        rows.push(
          h(
            "div",
            { class: "row" },
            h("a", { class: "btn primary", href: link.url, target: "_blank", rel: "noopener noreferrer" }, "Open Telegram"),
            button("New link", { onClick: linkTelegram }),
          ),
          h("div", { class: "row" }, qrSvg(link.url, { size: 160, label: "QR code: open the aijalon.trade bot in Telegram" }),
            h("p", { class: "small muted" }, "On a computer? Scan this with your phone's camera. Waiting for Telegram…")),
        );
      } else {
        rows.push(h("div", { class: "btns" }, button("Link Telegram", { kind: "primary", onClick: linkTelegram })));
      }
    }
    return h("div", { class: "stack tight" }, rows);
  }

  function emailSection(c: ContactsStatus): Child {
    const e = c.email;
    const rows: Child[] = [h("div", { class: "row between" }, h("h3", null, "Email"), e.verified ? badge("Confirmed", "good") : badge("Not confirmed", "warn"))];
    if (e.verified && emailMode === "idle") {
      rows.push(
        h("p", { class: "small" }, "Alerts go to ", h("b", { class: "mono break" }, e.address ?? ""), "."),
        h("div", { class: "btns" }, button("Use another address", { onClick: () => { emailMode = "enter"; draw(); } })),
      );
    } else if (emailMode === "code") {
      const input = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, pattern: "[0-9]{6}", placeholder: "123456" });
      rows.push(
        h("p", { class: "small muted" }, `Enter the 6-digit code we sent to ${codeSentTo}. It expires in 10 minutes; 5 attempts.`),
        field("Verification code", input),
        h("div", { class: "btns" },
          button("Verify", { kind: "primary", onClick: () => verifyCode(input.value.trim()) }),
          button("Change address", { kind: "ghost", onClick: () => { emailMode = "enter"; draw(); } })),
      );
    } else if (emailMode === "enter") {
      const input = h("input", { type: "email", autocomplete: "email", maxlength: 254, placeholder: "you@example.com", value: e.pending ?? "" });
      rows.push(
        h("p", { class: "small muted" }, "We'll send a 6-digit code to the new address. Changing the alert email needs a fresh sign-in with two-factor."),
        field("Alert email", input),
        h("div", { class: "btns" },
          button("Send code", { kind: "primary", onClick: () => sendCode(input.value.trim()) }),
          e.pending ? button("I have a code", { kind: "ghost", onClick: () => { codeSentTo = e.pending ?? ""; emailMode = "code"; draw(); } }) : null,
          button("Cancel", { kind: "ghost", onClick: () => { emailMode = "idle"; status.textContent = ""; draw(); } })),
      );
    } else {
      rows.push(h("p", { class: "small muted" }, "Mandatory alerts (fee balance, agent expiry, deposits, withdrawals, security) are also emailed. Apple private-relay addresses work."));
      rows.push(
        h(
          "div",
          { class: "btns" },
          e.account_email ? button(`Use ${e.account_email}`, { kind: "primary", onClick: confirmAccount }) : null,
          button("Use another address", { kind: e.account_email ? "plain" : "primary", onClick: () => { emailMode = "enter"; draw(); } }),
        ),
      );
    }
    return h("div", { class: "stack tight" }, rows);
  }

  function draw(): void {
    if (!st) {
      mount(root, h("p", { class: "muted" }, "Loading…"));
      return;
    }
    mount(
      root,
      st.ready ? note("Alerts are set up: Telegram is linked and your email is confirmed.", "info") : null,
      telegramSection(st),
      h("hr", { class: "divider" }),
      emailSection(st),
      status,
    );
  }

  draw();
  if (!st) void refresh();
  else if (st.telegram.status !== "linked" && link) startPoll();
  return root;
}

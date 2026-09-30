# aijalon.trade — Incident Response Plan

Version: 2026-09-30 · Owner: security lead [●] · Contacts sheet: [private link; not in the repo]
Related: `docs/RUNBOOK.md` (technical procedures), `docs/SECURITY.md`, `docs/DATA_PROTECTION.md`, `legal/privacy.md`

> **Status note.** This plan has not been exercised yet. The go-live checklist requires a tabletop exercise before real money is used. The legal notification duties described in §6 are our understanding of the Personal Data Protection Act 2010 as amended in 2024, and of the Commissioner's guidance. **Malaysian counsel must confirm them [COUNSEL]**, as well as any duties to other regulators (for example, the Securities Commission Malaysia or Bank Negara Malaysia, if the business is found to be regulated).

---

## 1. Principles

1. **Contain first.** Pausing trading is cheap; unwanted trades are not. Any admin may engage a kill switch without approval (RUNBOOK §4).
2. **Preserve evidence.** Do not delete logs, instances or data. Clone; don't overwrite.
3. **Communicate early and honestly.** Say what we know, what we don't, and when the next update is.
4. **One Incident Commander (IC).** Everyone else works through the IC.
5. **Blameless post-mortems.** Fix systems, not people.

## 2. Severity levels

| Sev | Definition | Examples | Response |
|---|---|---|---|
| **SEV1 — Critical** | Actual or likely loss of user or treasury funds; unauthorised trading; key compromise; personal data breach likely to cause significant harm; complete trading outage with users exposed during a market event | Unexplained treasury outflow; agent keys or executor compromised; mass wrong orders; admin takeover with money actions; KYC or identity data exfiltrated | Page immediately, 24/7. IC within 15 min. Kill switch as needed. Updates every 1 h. Legal/DPO engaged at once. |
| **SEV2 — High** | Significant risk to funds or integrity, contained or partial; a market incident affecting subscribers; limited personal data exposure | HIP-3 oracle manipulation on a traded market; forced settlement; ledger mismatch > $[100] unexplained; sandbox escape attempt; payout sent to the wrong address | Page. IC within 30 min. Updates every 2 h. |
| **SEV3 — Medium** | Degraded service or integrity issue with no immediate loss | Signal feed stale for a day; reconciliation mismatch $1–100; Stripe webhook outage; a single user's unexplained trades | Business hours plus the on-call judgement. Daily updates. |
| **SEV4 — Low** | Minor bug or near miss | A failed alert delivery; a single circuit breaker trip | Ticket. |

**When unsure, choose the higher severity.** Downgrading later is fine.

## 3. Roles

| Role | Responsibilities | Default holder |
|---|---|---|
| **Incident Commander (IC)** | Owns the incident, sets severity, decides and delegates; keeps the timeline; declares the end | On-call admin [●] |
| **Tech lead** | Investigation, containment, fixes; runs RUNBOOK procedures | Engineer on call [●] |
| **Comms lead** | User, creator and public messages; status banner; support inbox | [●] |
| **Legal / DPO** | Regulatory and PDPA notifications; legal holds; counsel liaison | DPO [●], external counsel [●] |
| **Scribe** | Timestamped log of facts, decisions and actions | Anyone not doing the above |
| **Second admin** | The checker for any lift, payout or correction during the incident | [●] |

In a small team, one person may hold several roles. **However, the IC must never be both maker and checker.** Lifting a kill switch always needs a second admin.

## 4. Phases

1. **Detect.** Sources: an alert (automatic), a user report, a creator, a researcher ([security@aijalon.trade]), a provider notice (Stripe, Google, KYC), or an announcement by Hyperliquid or a HIP-3 deployer.
2. **Triage** (≤ 15 min for pages). What is affected: funds, trading, data, availability? Set the severity; open an incident channel and doc `INC-YYYYMMDD-n`.
3. **Contain.** Use the kill switch or pause (RUNBOOK §3–4). Revoke credentials or IAM bindings (RUNBOOK §7.2). Hold payouts. Suspend accounts. Block IPs at Cloudflare.
4. **Assess impact.** Which users, amounts, data categories and time window are involved? Snapshot the evidence (logs export, DB clone, on-chain tx list).
5. **Eradicate and recover.** Fix the root cause through the 2-person rule. Restore (RUNBOOK §8) and reconcile. Lift the controls gradually (RUNBOOK §4.3).
6. **Notify.** Users, creators, regulators and providers, as required (§5–6).
7. **Close.** Hold the post-mortem within [5] business days (§8). Track the action items to completion.

## 5. Communication templates

Fill `[…]`. Keep the language plain. **Never** include seed or recovery instructions that could be phished; always point users to Hyperliquid's official interface for revoking agents.

**A. Status banner or in-app notice (initial)**
> **[Investigating]** — We are investigating [issue: e.g. "a pricing problem on xyz:SILVER"]. As a precaution, we have [paused new entries / paused all trading] on [scope] since [HH:MM UTC]. Your funds remain in your own Hyperliquid account; aijalon.trade cannot withdraw them. You can always manage or close positions directly on Hyperliquid. Next update by [HH:MM UTC].

**B. Affected subscribers (market incident)**
> Subject: Action on your [Strategy] subscription — [market] incident
>
> On [date/time UTC], we detected [unusual oracle/mark prices / a trading halt / a delisting] on [market]. We [paused new entries / stopped all orders] on this market at [time]. Your current position: [size/side, if we show it]. [What we are doing next.] What you can do: you can hold, or close your position yourself on Hyperliquid at any time. [If there is a forced settlement: Hyperliquid/the market deployer settled the market at [price] on [time]. We have put profit-share settlement for this subscription on hold while we review it.] Next update: [time]. Questions: [support@aijalon.trade].

**C. Suspected agent-key compromise (all users)**
> Subject: Important — please revoke the aijalon trading agent on your Hyperliquid account
>
> We have stopped all trading on aijalon.trade because we suspect [a security issue affecting the trading agent keys]. By Hyperliquid's design, agents can place trades but cannot withdraw or transfer your funds. As a precaution, please revoke the agent named "aijalon" in your Hyperliquid account settings using the official Hyperliquid app, and review your open positions. We will never ask for your seed phrase or private key. [What happened, what we know, what we are doing.] Next update: [time].

**D. Personal data breach (to affected individuals)**
> Subject: Notice of a personal data incident at aijalon.trade
>
> On [date], we became aware that [description of the breach]. The personal data involved: [categories]. It [does / does not] include [wallet addresses / email / KYC status…]. Likely consequences: [e.g. phishing risk; your wallet address may be linked to your email]. What we have done: [containment, notified the Personal Data Protection Commissioner on [date]]. What you can do: [be alert to phishing; we will never ask for your seed phrase; revoke/rotate…]. Contact our Data Protection Officer: [dpo@aijalon.trade]. [Bahasa Malaysia version below / attached.] [COUNSEL: confirm the required content and language]

**E. Resolution**
> **[Resolved]** — [Issue] was resolved at [HH:MM UTC]. Trading has [resumed / resumed for …]. Impact: [summary]. We will publish a summary of what happened and what we changed by [date].

**F. Creators (strategy paused or delisted due to an incident)**
> Your strategy [name] was [paused/delisted] on [date] because of [reason]. Subscribers were [notified / moved to reduce-only]. Earnings during [period] are [held pending review / unaffected]. [Next steps.]

## 6. PDPA personal data breach notification

**Our understanding, for counsel to confirm [COUNSEL]:**

- The 2024 amendments to the PDPA introduced a duty on the data controller to **notify the Personal Data Protection Commissioner** of a personal data breach **as soon as practicable**. The Commissioner's guidance has been reported as expecting notification **within 72 hours** of becoming aware of it.
- The controller must also **notify affected data subjects without unnecessary delay** where the breach causes, or is likely to cause, **significant harm**. The guidance has been reported as setting a period of about **7 days** after notifying the Commissioner.
- Confirm the current thresholds ("significant harm"), timelines, required content, and form (online form or portal).

**Our internal process (applies whatever the legal thresholds turn out to be):**
1. Any suspected personal-data incident goes to the DPO **immediately** (at SEV2 or above, the DPO joins the incident).
2. The DPO starts the **breach register** entry at once: date and time of awareness, facts, data categories, number of individuals, likely consequences, and measures taken. **The register is kept for every breach**, notifiable or not.
3. Within **24 h** of awareness: the DPO and counsel assess whether notification is required. Draft the Commissioner notification.
4. **Within 72 h** of awareness: submit to the Commissioner if required. Where facts are incomplete, submit a preliminary notification and follow up.
5. Individuals: notify (template D, **in Bahasa Malaysia and English** [COUNSEL]) where required, without unnecessary delay.
6. **Processors** (Google Cloud, Firebase, Stripe, the KYC provider, the email provider): their contracts must require them to notify us of breaches promptly. When a processor notifies us, we start from step 1.
7. **Cross-border:** if affected users are in other jurisdictions, counsel checks those laws' notification duties.
8. Keep evidence and the register for at least [2] years [COUNSEL: confirm the retention period].

**Other notifications to consider:**
- Stripe: account security issues, card-data incidents (we do not store card data), fraud.
- Google Cloud: abuse or compromise.
- Hyperliquid: via their official channels, if a market or protocol issue.
- Police or CyberSecurity Malaysia (for cybercrime).
- Insurers.
- Any financial regulator if the business becomes licensed or registered [COUNSEL].

## 7. Evidence handling

- Export Cloud Logging for the window to a locked bucket (retention lock).
- Clone the DB (RUNBOOK §8); never modify the original.
- Record on-chain tx hashes and block times.
- Save the audit-log chain head hashes at the start of the incident.
- Record who accessed what during the incident.

## 8. Post-mortem template

```
# Post-mortem: INC-YYYYMMDD-n — <title>
Severity: SEV_   Status: draft/final   IC: _   Authors: _   Date: _

## Summary
2–4 sentences: what happened, impact, how it ended.

## Impact
- Users affected: _ (subscriptions: _; markets: _)
- Funds: user trading losses attributable _ ; treasury/fee balance impact _
- Data: categories _ ; individuals _ ; notified Commissioner? (Y/N, when) ; individuals? (Y/N, when)
- Duration: detected _ ; contained _ ; resolved _ (UTC)

## Timeline (UTC)
| time | event / decision / action | who |

## Detection
How did we find out? How long after it started? Did alerts fire as designed?

## Root cause(s)
Technical and process causes (5 whys). Contributing factors.

## What went well / what went badly / where we got lucky

## Controls review
Which controls worked (guards, auto-pause, maker-checker, reconciliation)? Which failed or were missing?
Update docs/SECURITY.md STRIDE table if a threat was missing.

## Action items
| # | action | owner | due | ticket | status |

## Customer/regulator communications sent
Links/copies.

## Legal follow-up
Refunds/credits decided (with maker-checker ref), counsel advice, open obligations.
```

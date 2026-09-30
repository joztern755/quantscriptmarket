# aijalon.trade — Privacy Notice (Personal Data Protection Notice)

Version: 2026-09-30
Consent record: `doc=privacy` (contexts: `site_entry`, `creator`)
Status: **DRAFT — NOT IN FORCE**

> **DRAFT FOR REVIEW BY A MALAYSIAN-QUALIFIED LAWYER. NOT LEGAL ADVICE. NOT YET IN FORCE.**
>
> This notice is intended to meet the Notice and Choice Principle of the **Personal Data Protection Act 2010 (PDPA)**, as amended by the Personal Data Protection (Amendment) Act 2024. **We have not verified compliance.**
>
> **Bilingual requirement (FLAG).** The PDPA requires the written notice to be given in **both the national language (Bahasa Malaysia) and English**. A Bahasa Malaysia version must be prepared and reviewed before launch. The app must show both, or let the user choose, and record which version was accepted.
>
> Items for counsel:
> - the 2024 amendments. As we understand them, they introduce mandatory data breach notification, a data protection officer requirement, a data portability right, direct security obligations on data processors, a revised cross-border transfer regime, and use the term "data controller". Confirm which provisions are in force, and the related Commissioner's guidelines;
> - whether registration with the Commissioner is required for this class of business;
> - whether biometric data processed by the KYC provider is "sensitive personal data" for which **we** need explicit consent.
>
> Technical facts marked **[VERIFY]** (regions, processors) must match the actual deployment.

---

## 1. Who we are

[Operator legal name] Sdn. Bhd. (Company No. [●]), [registered address], Malaysia ("**we**", "**us**") is the data controller for personal data processed through aijalon.trade.

**Data Protection Officer:** [Name / role], [dpo@aijalon.trade], [phone]. [COUNSEL: confirm whether a DPO appointment is mandatory for us under the amended PDPA and the Commissioner's thresholds, and whether the appointment must be notified to the Commissioner.]

## 2. Personal data we collect

| Category | Examples | Source | Required? |
|---|---|---|---|
| Identity and sign-in | Name, email address, and account identifiers from **Google** or **Apple** (Apple may give a private relay email); profile photo if your provider shares it | You, via Google or Apple sign-in | Required |
| Security | MFA enrolment status; sign-in times; hashed IP address; hashed user-agent; country derived from IP; security alerts (for example, login from a new country) | Automatically | Required |
| Device signal | **Hashed device fingerprint**, used only to prevent self-referral and abuse [PRODUCT: describe exactly what is fingerprinted] | Automatically | Required (if referral or anti-abuse is used) |
| Consents | Which documents and versions you accepted, when, and in what context (site entry, subscribe, creator), with hashed IP and user-agent | Automatically, when you tick | Required |
| Jurisdiction | Your attested country and eligibility | You | Required |
| Wallets and trading | Your Hyperliquid master address and trading (sub-)account address; agent address; builder-fee approval; subscriptions, allocation, leverage; orders, fills, positions, PnL and funding related to our orders | You, and the public Hyperliquid blockchain | Required to trade |
| Payments | Fee Balance ledger; top-ups (amount, method, time); Stripe payment identifiers; limited card details from Stripe (for example, brand, last 4 digits, country). **We never receive your full card number.** USDC transaction hashes | You, Stripe, and the blockchain | Required to pay fees |
| Communications | Support messages; email alerts; **Telegram chat ID**, if you connect Telegram alerts | You | Optional |
| Referrals | Referral code, who referred you, and aggregated referral statistics | You and automatically | Optional |
| Content | Reviews, display name, posts | You | Optional |
| **Creators only** | KYC result and status, and the provider's reference; payout address; tax information where required [COUNSEL]; strategy code (confidential business information, not personal data unless it identifies you) | You and our KYC provider | Required to be a creator |

**KYC documents and biometrics (creators).** Our KYC provider, [KYC provider — TBD], collects identity documents and may collect a selfie or liveness check, which may be **biometric data**. The provider keeps these documents. **We receive only the outcome and a reference**, not copies of the documents [VERIFY with the chosen provider]. Where the law requires it, we will ask for your **explicit consent** before this processing.

**We do not knowingly collect data from anyone under 18.**

**No advertising trackers.** We do not currently use third-party advertising or cross-site tracking cookies [PRODUCT: confirm; update this section if analytics are added]. Before you sign in, we store your gate acknowledgements in your browser's local storage, so we can record them against your account when you sign in.

## 3. Why we use it (purposes)

1. To create and secure your account (authentication, MFA, step-up, fraud and abuse prevention).
2. To check eligibility and jurisdiction, and to comply with sanctions and other legal obligations.
3. To provide the service: connecting your agent, executing strategies in your account, attributing fills to subscriptions, calculating profit share and fees, and keeping the ledger.
4. To process payments and top-ups, and to handle refunds and disputes.
5. To send alerts and service messages, by email, in the app and, if you choose, on Telegram.
6. To run the referral programme and prevent self-referral.
7. To show aggregated strategy statistics (only when a strategy has at least 5 subscribers; your address is never shown).
8. To verify creators (KYC), pay creators and referrers, and detect prohibited trading.
9. To keep audit logs, and to reconcile and investigate security or financial incidents.
10. To meet accounting, tax, legal and regulatory obligations, and to establish, exercise or defend legal claims.
11. To improve the service, using aggregated or pseudonymised data.
12. **Marketing,** only with your separate consent. You can opt out at any time.

## 4. Who we share it with

We share personal data only as needed for the purposes above.

- **Google Cloud / Firebase** (hosting, Cloud Run, Cloud SQL, KMS, Secret Manager; Firebase Authentication / Identity Platform). Data processor. [VERIFY: the location of the authentication data, which may differ from the Singapore region]
- **Cloudflare** (DNS, web application firewall, rate limiting). Processes IP addresses and request data, possibly globally.
- **Stripe** (card and other payments; fraud screening). Stripe acts as an independent controller for some data under its own privacy policy.
- **KYC provider** [TBD] (creator identity verification).
- **Email provider** [TBD], and **Telegram** (only if you connect it).
- **Google and Apple** (sign-in identity providers).
- **Hyperliquid (public blockchain).** When we place orders for you, they are recorded **publicly and permanently** on Hyperliquid, together with your address. Hyperliquid is not our processor. We cannot delete or change on-chain data.
- **Creators** receive **only aggregated, non-identifying** subscriber statistics. They never receive your identity or wallet address.
- **Professional advisers, auditors and insurers,** under confidentiality obligations.
- **Authorities:** regulators, law enforcement, courts and tax authorities, where required or permitted by law.
- **A buyer or successor** of our business, under equivalent protections.

**We do not sell personal data.**

## 5. Transfers outside Malaysia

Our main infrastructure is in **Google Cloud's Singapore region (`asia-southeast1`)**. Some processors (Stripe, Cloudflare, Firebase Authentication, the email provider, the KYC provider, and Telegram) may process data in other countries, including the United States. We transfer data outside Malaysia only as allowed under the PDPA's cross-border transfer rules, for example:
- where the destination has laws substantially similar to the PDPA, or that ensure an adequate level of protection; or
- where the transfer is needed to perform our contract with you; or
- with your consent.

We use contractual safeguards with processors. [COUNSEL: confirm the correct legal basis under the amended cross-border provisions and any Commissioner guidelines, and whether a transfer impact assessment is expected.]

## 6. How long we keep it

See `docs/DATA_PROTECTION.md` for the full schedule. In summary:

- **Account and profile:** while your account is open, then deleted or pseudonymised within [90] days of closure, except as below.
- **Ledger, payments, fee and payout records:** [7] years from the end of the relevant financial year (accounting and tax record-keeping) [COUNSEL: confirm the period].
- **Consent records and audit logs:** kept for the life of the account plus [7] years, as evidence. These are append-only.
- **Security logs** (hashed IPs, user-agents): [12] months.
- **KYC (creators):** at the provider, for the period that the provider's and our legal obligations require [COUNSEL: anti-money-laundering record retention].
- **On-chain data** stays permanently on the public blockchain. We have no control over it.

## 7. Your rights

Subject to the PDPA and its exceptions, you may:
- **access** your personal data, and get a copy (we may charge a fee where the law allows);
- **correct** inaccurate, incomplete, misleading or out-of-date data;
- **withdraw consent,** or limit processing, including for direct marketing. If you withdraw consent to processing that the service needs, we may have to stop providing the service and close your account;
- ask us to **stop processing** that is likely to cause you substantial damage or distress, where the PDPA gives this right;
- **data portability:** where the right is in force and technically feasible, ask us to transmit your data to another controller [COUNSEL: confirm the commencement and scope of this right];
- **complain** to the Personal Data Protection Commissioner of Malaysia.

To exercise a right, email [dpo@aijalon.trade] from your account email. We will verify your identity and respond within the time the PDPA requires [COUNSEL: confirm the response period, generally 21 days for access requests].

## 8. Is providing data obligatory?

Data marked "Required" in section 2 is needed to provide the service. **If you do not provide it, we cannot open or keep your account, execute strategies, or pay you** (creators and referrers). Optional data can be withheld without affecting the core service.

## 9. Security

We use measures designed to protect personal data, including:
- TLS in transit;
- encryption at rest with customer-managed keys;
- hardware-backed key management for trading keys;
- mandatory MFA;
- least-privilege access with separated duties;
- hashed IPs;
- append-only, tamper-evident audit logs;
- monitoring.

**No system is perfectly secure.** See `docs/SECURITY.md`.

## 10. Data breaches

If a personal data breach occurs, we will notify the Personal Data Protection Commissioner, and affected individuals where required, as the PDPA (as amended) and the Commissioner's guidelines require. See `docs/INCIDENT_RESPONSE.md`.

## 11. Changes

We may update this notice. We will ask you to review and accept material changes at the site gate. The version and date are shown at the top.

## 12. Contact

[DPO name], Data Protection Officer, [Operator legal name] Sdn. Bhd., [address], [dpo@aijalon.trade].

## Open questions for counsel (this document)

1. Arrange a Bahasa Malaysia translation and a mechanism for showing both languages. Confirm the exact bilingual requirement.
2. Status and commencement of the 2024 amendments: breach notification, the DPO requirement, portability, processor obligations, and cross-border rules. Which Commissioner guidelines apply?
3. Is a hashed IP, a user-agent hash or a device-fingerprint hash still personal data? (Treat it as personal data by default.) Is consent needed for device fingerprinting?
4. KYC biometrics: are we a controller or a joint controller with the provider? Is explicit consent for sensitive personal data needed? Where is the data located?
5. Is registration with the Commissioner required for our class of data user?
6. Retention periods for ledger, KYC and AML records, and how the append-only ledger and consent logs fit with erasure and withdrawal of consent.
7. Is an explicit notice needed that on-chain data is public and cannot be erased? (Included in section 4; confirm it is adequate.)

# legal/ — index, consent mapping and questions for counsel

Version: 2026-09-30
Status: **ALL DOCUMENTS ARE DRAFTS FOR REVIEW BY A MALAYSIAN-QUALIFIED LAWYER. NONE IS IN FORCE.**

> **Owner instruction:** push away all liability. **What these drafts actually do:** they exclude and limit liability as far as is *plausibly enforceable* under Malaysian law. Every exclusion is qualified by "to the maximum extent permitted by law", and there are explicit carve-outs for fraud, wilful misconduct, death and personal injury, [gross negligence], and non-excludable statutory rights. Going further tends to make clauses *less* likely to survive challenge.
>
> **User tick-boxes and waivers do not replace any licence, registration or approval the business may need.** The licensing questions below (Q1–Q3) are the most important items for counsel. They decide whether the product can launch as designed at all.
>
> Nothing here has been verified by a lawyer. Statutes are described in general terms. Where we were unsure of section numbers, they were deliberately left out.

## Documents

| File | Consent `doc` | Shown at | Purpose |
|---|---|---|---|
| `terms.md` | `terms` | site entry, subscribe, creator | Terms of Service |
| `risk-disclosure.md` | `risk` | site entry, subscribe | Risk Disclosure |
| `privacy.md` | `privacy` | site entry, creator | PDPA notice (**needs a Bahasa Malaysia version**) |
| `liability-waiver.md` | `waiver` | site entry, subscribe | Assumption of risk and release |
| `jurisdiction.md` | `jurisdiction` | site entry, subscribe | Eligibility attestation and DRAFT restricted list (`config.py` → `DEFAULT_RESTRICTED`) |
| `subscription-ack.md` | `subscription_ack` | subscribe (per strategy) | Per-strategy acknowledgement template |
| `creator-agreement.md` | `creator_agreement` | creator onboarding | Creator terms |
| `acceptable-use.md` | (part of `terms`) | linked | Conduct rules |
| `refund-policy.md` | (part of `terms`) | linked | Refunds and chargebacks |

Note: `docs/SPEC.md` §3 and a comment in `config.py` refer to `legal/restricted-jurisdictions.md`. The corresponding document is **`jurisdiction.md`**.

## Versioning rules (for implementers)

- Every document starts with `Version: YYYY-MM-DD`. The web gate and `POST /v1/consents` record `(doc, doc_version, context, strategy_id, ip_hash, user_agent_hash, accepted_at)`, append-only.
- **A material change means a new version, and users must re-accept it at the gate.** Typo fixes may keep the version.
- Checkboxes must never be pre-ticked. Each document must be linked, and readable in full, before its box can be ticked.
- **Store a hash of the exact rendered text** (including filled placeholders in `subscription-ack.md`), so we can prove what was shown.
- Remove every `[COUNSEL]`, `[CONFIRM]`, `[VERIFY]`, `[PRODUCT]` and `[●]` marker before publishing. **CI could fail the build if any marker remains in a published document** (suggestion for the web/CI owner).

## Owner decisions reflected (30 Sep 2026)

- Creator profit share capped at **12%**; platform **1.5% charged on top** (the subscriber pays at most 13.5%).
- **Stripe/processor fees passed to the user.** Top-ups are credited net of the processor fee, and the fee must be shown before payment.
- **US stays restricted.** Removing it requires counsel's written sign-off.
- Launch strategy: SILVER (`xyz:SILVER`, a HIP-3 market) only. Creator uploads are on at launch, and creator KYC is required.

## Consolidated open questions for counsel

### A. Licensing and regulatory perimeter (critical: decide before any public launch)

1. **Capital Markets and Services Act 2007.** Does the service, as designed, require a Capital Markets Services Licence, or registration, for any of these activities: fund management or portfolio management, dealing in derivatives, investment advice, or financial planning? The service executes strategies in users' own Hyperliquid accounts through a trade-only agent, for a subscription, a builder fee and a **performance-based profit share**. Is the answer different for in-house strategies and for third-party creators? Does paid content ("posts") count as investment advice?
2. **Characterisation of Hyperliquid perps** (on crypto and on commodities such as silver, gold and oil via HIP-3). Are they "derivatives" or "securities" under Malaysian law, including under the Securities Commission's prescription order for digital currencies and digital tokens? How do the Securities Commission's digital-asset guidelines (for example, on recognised markets and digital asset exchanges or custodians) apply to a front-end that routes orders to a foreign decentralised exchange?
3. **Serving Malaysian residents.** Given Q1–Q2, should Malaysia itself be a restricted jurisdiction until the position is clear? Is there any Securities Commission investor-alert risk?
4. **Fee Balance.** Is a prepaid, multi-purpose USD balance funded by card or USDC a designated payment instrument or e-money under the Financial Services Act 2013, needing Bank Negara Malaysia approval? Is there a single-purpose exemption? Does refund on demand (Refund Policy §2.1) change the answer? Does holding users' USDC amount to digital-asset custody?
5. **Anti-money-laundering (AMLA 2001).** Is the operator a reporting institution? Is customer due diligence needed for **all** users (currently only creators do KYC)? What are the record-keeping periods? Is sanctions and wallet screening required?
6. **Foreign regimes via the user base.** What is the minimum restricted list (see `jurisdiction.md` §1.1: GB, CA, EU and EEA, SG, HK, JP, KR, CN, AU)? What is the test for ever un-restricting the US?

### B. Contract enforceability

7. Does the **Consumer Protection Act 1999**, including Part IIIA on unfair contract terms, apply to this service, given its exclusions for certain financial and securities matters? Which clauses are exposed?
8. Enforceability, against consumers, of:
   - the liability cap (Terms §17.2: the greater of 3 months' fees or USD 100);
   - the shortened claim period (§17.3), given the Limitation Act 1953 and the Contracts Act 1950;
   - the consumer indemnity (§18);
   - the waiver covenant not to sue (Waiver §4);
   - AIAC arbitration and the class-action waiver (§21).
9. Should "gross negligence" be expressly carved out? How should "good faith" in the kill-switch clauses be framed?
10. Is click-wrap assent (Electronic Commerce Act 2006) valid with a hashed-IP audit trail? Must a durable copy be given to users?
11. **Consumer Protection (Electronic Trade Transactions) Regulations 2012:** what disclosures are required on the site (entity, registration number, contact details, prices, payment terms, error correction)?
12. Is there any liability exposure from **unilaterally closing positions** on termination, kill switch or reduce-only? Should users make an explicit election?
13. Is a Bahasa Malaysia version of the consumer-facing terms advisable or required?

### C. Payments, fees and tax

14. Passing Stripe fees on to users (crediting net): what are the card-scheme, Stripe and consumer price-transparency constraints?
15. Is Stripe's approval needed for this business category? What is the contingency if Stripe closes the account while balances are outstanding?
16. SST or service tax on platform fees; e-invoicing; withholding tax on payments to non-resident creators and referrers; characterisation of creator payouts (service fee or royalty).
17. Is passing chargeback dispute fees to users permitted?

### D. Personal data (PDPA 2010, as amended 2024)

18. Arrange the **Bahasa Malaysia translation** of the privacy notice, and confirm how the bilingual requirement must be met in the UI.
19. Which 2024 amendments are in force, and which Commissioner's guidelines apply? This covers: breach notification (timelines to the Commissioner and to data subjects), the DPO requirement and its notification, data portability, processor obligations, and cross-border transfer conditions.
20. Is registration with the Commissioner required for this class of data user?
21. Are hashed IPs, user-agent hashes and device-fingerprint hashes personal data? Is consent needed for fingerprinting?
22. KYC biometrics: is our role that of controller or joint controller? Is explicit consent for sensitive personal data needed?
23. Retention: how do the **append-only ledger, consent and audit logs** fit with erasure and withdrawal-of-consent requests, and what periods apply?

### E. Programme-specific

24. Referral programme (single-level, paid from actual builder fees): does it comply with the Direct Sales and Anti-Pyramid Scheme Act 1993 and advertising rules?
25. Creators: can we monitor their personal trading and require them to disclose their accounts? Is a front-running blackout window enforceable? What limitation period applies to clawback?
26. User content (posts and reviews): what are the obligations under the Communications and Multimedia Act 1998 and any online-safety legislation in force? Is a notice-and-takedown procedure needed?
27. Should the force majeure clause name specific venue incidents (for example, a validator-forced market settlement)? Frustration interplay under the Contracts Act 1950.

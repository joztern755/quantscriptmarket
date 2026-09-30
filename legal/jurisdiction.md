# aijalon.trade — Jurisdiction and Eligibility Attestation

Version: 2026-09-30
Consent record: `doc=jurisdiction` (contexts: `site_entry`, `subscribe`)
Status: **DRAFT — NOT IN FORCE**

> **DRAFT FOR REVIEW BY A MALAYSIAN-QUALIFIED LAWYER. NOT LEGAL ADVICE. NOT YET IN FORCE.**
>
> The restricted list below is a **DRAFT**. It is based on the owner's instruction and on common sanctions and regulatory practice. **Counsel has not verified it.** The owner (30 Sep 2026) has kept the **United States** on the list for now. **Removing any country, and in particular the US, requires counsel's written sign-off first.** A user's attestation is evidence that we asked, and it shifts some responsibility to the user. **It does not make it lawful to serve a jurisdiction where a licence is required**, and it does not replace sanctions screening. Hyperliquid's own terms and geo-restrictions must also be checked **[VERIFY]**, because our users must be eligible to use Hyperliquid itself.
>
> Implementation: `backend/app/config.py` → `DEFAULT_RESTRICTED` / env `RESTRICTED_COUNTRIES` (ISO 3166-1 alpha-2), exposed through `GET /v1/public/config`. The config comment references `legal/restricted-jurisdictions.md`; this file (`jurisdiction.md`) is that document.

---

## 1. Restricted Jurisdictions (DRAFT)

You may not use aijalon.trade if you are resident in, located in, a citizen of, or (for an entity) incorporated or based in, any of these jurisdictions:

| ISO | Jurisdiction | Draft reason (for counsel) |
|---|---|---|
| US | United States of America (including its territories) | Offering leveraged crypto derivatives and trading services to US persons generally needs US registration (for example, under the CFTC and SEC regimes). US sanctions exposure also arises through service providers. Owner decision: stays restricted. Removal needs counsel sign-off. |
| CU | Cuba | Comprehensive sanctions (US), and provider restrictions (Stripe, Google) |
| IR | Iran | Comprehensive sanctions (UN, US, EU); FATF high-risk "call for action" jurisdiction |
| KP | North Korea (DPRK) | Comprehensive sanctions (UN, US, EU); FATF high-risk "call for action" jurisdiction |
| SY | Syria | Sanctions (US, EU) [COUNSEL: check the current status, as sanctions regimes have been changing] |
| RU | Russia | Sanctions (US, EU, UK) |
| BY | Belarus | Sanctions (US, EU, UK) |
| MM | Myanmar | Sanctions; FATF high-risk "call for action" jurisdiction [COUNSEL: confirm current listing] |

**Also restricted (draft):** any region under comprehensive sanctions, for example the Crimea, so-called Donetsk People's Republic and Luhansk People's Republic regions of Ukraine [COUNSEL]. The same applies to anyone on an applicable sanctions list.

### 1.1 Candidates for counsel to consider (NOT currently restricted in config)

These are suggestions for review only. We have **not** verified the legal position in any of them.

- **Malaysia (MY) — highest priority.** We are based in Malaysia. If serving Malaysian residents requires a licence from the Securities Commission Malaysia (for example, under the Capital Markets and Services Act 2007, or its digital-asset framework) that we do not hold, Malaysia may need to be restricted until the licensing position is resolved.
- **United Kingdom (GB):** the FCA has banned the sale of crypto-asset derivatives to retail consumers.
- **Canada (CA),** especially Ontario and other provinces with crypto-trading-platform rules.
- **EU and EEA member states:** rules on marketing derivatives to retail clients, and MiFID II licensing for investment services.
- **Singapore (SG), Hong Kong (HK), Japan (JP), South Korea (KR), mainland China (CN), Australia (AU):** each has specific rules on crypto derivatives, retail access or unlicensed offers.
- Any jurisdiction that **Hyperliquid itself** restricts, or that our payment, identity or KYC providers do not support.

## 2. How we enforce this

- An attestation at site entry, and again at each subscription, recorded with the document version, time and hashed network identifiers.
- Blocking of Restricted Jurisdictions based on IP geolocation (through Cloudflare) [VERIFY: implemented].
- Signals that someone is circumventing the restrictions. Examples: logins from a new country (alerted), VPN or hosting-provider IP ranges [PRODUCT], mismatched payment-card country (Stripe), and KYC country (for creators).
- **What we do on detection:** we may suspend your account and move subscriptions to reduce-only or closed, as the Terms allow.

[COUNSEL: whether we need positive sanctions screening (for example, name screening at signup, or wallet screening against sanctions-listed addresses), rather than attestation plus IP blocking only.]

## 3. Attestation text: site entry

> ☐ **I confirm that:**
> - I am at least 18 years old.
> - I am **not** a resident, citizen or entity of, and I am not located in, any Restricted Jurisdiction ([list shown here from `/v1/public/config`]).
> - I am not subject to sanctions.
> - I am not using a VPN, proxy or other means to hide my location.
> - Using aijalon.trade and trading perpetual futures on Hyperliquid is lawful for me where I live and where I am.
> - I am allowed to use Hyperliquid under its own terms.
> - I will stop using aijalon.trade and tell aijalon.trade if any of this changes.
>
> I understand that aijalon.trade relies on this confirmation, and that giving false information breaches the Terms. If I do, my account may be closed, and I may be liable for resulting losses.

## 4. Attestation text: subscribe (repeated each time)

> ☐ I confirm again that I am not in, or from, a Restricted Jurisdiction. I confirm that subscribing to this strategy and allowing automated trading of perpetual futures in my Hyperliquid account is lawful for me. My eligibility has not changed since I last confirmed it.

## 5. Change of circumstances

If you move to, or become resident in, a Restricted Jurisdiction, you must stop using the Platform and let us know. We will move your subscriptions to reduce-only or cancel them, as described in the Terms. We will handle your Fee Balance under the Refund Policy, subject to any legal restrictions (for example, sanctions may prevent a refund).

## Open questions for counsel (this document)

1. Confirm or adjust the restricted list. Should Malaysia be restricted until licensing is clear?
2. What written test must be met before removing the US? (For example: US regulatory status of Hyperliquid perps, Hyperliquid's own US policy, and whether we would need CFTC or NFA registration.)
3. Which sanctions regimes must we (a Malaysian company) comply with directly, and which only apply contractually through Stripe, Google and Cloudflare? Is wallet-address screening needed?
4. Is attestation plus IP geo-blocking a sufficient "reasonable steps" defence, or is identity verification needed for all users?

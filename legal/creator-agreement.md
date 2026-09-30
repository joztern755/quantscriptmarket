# aijalon.trade — Creator Agreement

Version: 2026-09-30
Consent record: `doc=creator_agreement`, `context=creator`
Status: **DRAFT — NOT IN FORCE**

> **DRAFT FOR REVIEW BY A MALAYSIAN-QUALIFIED LAWYER. NOT LEGAL ADVICE. NOT YET IN FORCE.**
>
> This Agreement sits on top of the Terms of Service, for users who publish strategies or paid posts. Exclusions and limits in it apply only to the extent permitted by Malaysian law (see the header of `terms.md`).
>
> **Regulatory flag [COUNSEL — CRITICAL].** A creator who designs a strategy that trades other people's accounts, for a subscription fee and a performance-based profit share, may be doing a regulated activity (for example, investment advice or fund management under the Capital Markets and Services Act 2007, or the equivalent in the creator's own country). The Platform, by hosting and executing the strategy and taking a share, may be treated as doing so too, or as facilitating it. **Making the creator responsible for their own licences (section 11) does not protect the Platform if the Platform itself needs a licence.**
>
> Owner decisions (30 Sep 2026): creator profit share is capped at **12%**; the platform's **1.5% is charged on top** (the subscriber pays at most 13.5%); creator uploads are enabled at launch; creator KYC is required before listing or payout.

---

## 1. Parties and scope

This Agreement is between [Operator legal name] Sdn. Bhd. ("**we**", "**us**") and you, a registered user who applies to become a creator ("**Creator**", "**you**"). It covers:
- strategies you upload or build with the no-code builder ("**Scripts**" or "**Strategies**");
- paid and free posts;
- your earnings.

The Terms of Service, the Acceptable Use Policy and the Privacy Notice also apply to you.

## 2. Eligibility and KYC

2.1 To be a creator, you must be eligible under the Terms, and you must successfully complete **identity verification (KYC)** with our provider **before** any Strategy can be listed or any payout made. We may ask for more information at any time. That may include source of funds, tax residency, or proof of any required licence. We may refuse or revoke creator status at our discretion.

2.2 Creators must be individuals acting for themselves, or a verified entity whose beneficial owners have been verified [COUNSEL/PRODUCT: whether to allow entities].

## 3. Uploading, review and listing

3.1 Each Script must meet the technical contract in the Platform documentation:
- a single Python module, with an allow-listed standard library only;
- 1–5 Hyperliquid perp markets;
- the declared `MAX_LEVERAGE`, which is capped by the platform;
- deterministic output of target weights only.

3.2 Every version goes through sandbox validation, a walk-forward backtest (at least one year of data) and **admin review** before it can be listed. **Listing is at our sole discretion.** A review is not an endorsement, and it does not guarantee that the Script is correct, profitable or lawful.

3.3 **A new version resets the Strategy's live track record.** Subscribers are shown its backtest together with the warning that it may be fitted to history.

3.4 We may reject, pause or delist any Strategy or version at any time, for any reason. Examples: security, risk, performance anomalies, suspected manipulation, complaints, legal or regulatory concerns, a market becoming unavailable, or breach of this Agreement.

## 4. Your warranties

You warrant, and repeat on every upload, post and payout request, that:

- (a) **Originality and rights.** The Script and your posts are your original work, or you have every right needed to license them to us. They do not infringe anyone's intellectual property, confidentiality or other rights. They do not contain code or strategies taken from your employer or any other person in breach of an obligation.
- (b) **No malicious code.** The Script contains no attempt to escape the sandbox, gain access to anything other than its inputs, exfiltrate data, deny service, or behave differently under review than in production.
- (c) **Accurate descriptions.** Your descriptions, markets, timeframe, leverage and risk statements are accurate and not misleading. You make **no performance promises**, and no performance claims other than statistics produced by the Platform.
- (d) **Compliance.** Your activities as a Creator comply with the laws that apply to you, including licensing, tax, sanctions and anti-money-laundering laws.
- (e) **Independence.** You are not subject to any restriction that prevents you from entering into this Agreement.

## 5. Market integrity: prohibited conduct

You must not, directly or indirectly (including through related persons, entities, accounts, bots or agreements with others), do any of the following:

1. **Wash trading,** or self-dealing between accounts you control.
2. **Trading against your subscribers.** For example, taking the other side of your Strategy's expected orders, or profiting from subscribers' fills, liquidations or slippage.
3. **Front-running.** For example, trading ahead of your Strategy's signals, or ahead of subscribers' orders, using your knowledge of the Script's logic or its upcoming output. This includes trading in a Strategy market in the window from [●] hours before to [●] hours after a bar close on which your Strategy's target changes [CONFIRM/COUNSEL: define the window, or require disclosure of personal positions].
4. **Market manipulation.** For example, spoofing or layering; pushing prices, oracle inputs or funding (especially in thin HIP-3 markets); or designing a Script whose purpose or effect is to move a market for your benefit.
5. **Gaming the Platform.** For example, inflating statistics, subscriber numbers or reviews (including paying for reviews, or using sock-puppet subscribers); breaking a Strategy into versions to hide losses; or misusing referral rewards.
6. **Soliciting subscribers off-platform** to avoid fees, or collecting subscribers' credentials, keys or funds.
7. Giving **personalised investment advice** to subscribers through posts or messages. [COUNSEL]

You agree that we may **monitor trading activity** that we can observe, including public on-chain activity linked to you. You agree to respond to our requests for information, and to disclose your accounts in the relevant markets when we ask [COUNSEL: consider whether this is proportionate]. **A breach of this section is a material breach.** It entitles us to delist you, withhold and claw back earnings, report to authorities, and recover losses.

## 6. Code confidentiality and our licence

6.1 **You own your Script.**

6.2 **Licence to us.** You grant us a worldwide, non-exclusive, royalty-free, sub-licensable (to our service providers only) licence to host, store, copy, encrypt, validate, compile, analyse, test, backtest and **execute** each Script, and to use its outputs to trade for subscribers. The licence also covers displaying performance, backtests and descriptions, and keeping copies for audit, security, dispute and legal purposes. It lasts:
- (a) while the Script is listed or any subscription to it is active;
- (b) for a wind-down period of [30] days after delisting or termination; and
- (c) for archived copies, as long as the law or legitimate record-keeping requires.

6.3 **Our confidentiality commitment.**
- (a) We store Scripts encrypted, and run them only in an isolated sandbox with no network access.
- (b) **We never show Script code to subscribers or other users.**
- (c) Our staff access code only where it is needed for review, security, incident response, or legal compliance. Such access is logged [PRODUCT: needs to be implemented as audited, maker-checker-approved access].
- (d) We may disclose code where the law, a court or a regulator requires it.

6.4 **What we cannot protect.** Your Strategy's **trades are public** on Hyperliquid. Third parties may infer your logic from outputs, statistics or on-chain behaviour. **We are not liable for such inference or copying**, but we will act on clear breaches of our Terms by users.

6.5 **Posts.** You grant us a licence to host and display your posts. Paid posts are shown only to buyers. We cannot prevent buyers from copying them, but copying breaches the Acceptable Use Policy.

## 7. Revenue shares

7.1 Current rates (all set in platform config and shown in Creator Studio). We may change them prospectively with [30] days' notice.

| Revenue source | Creator share | Platform share |
|---|---|---|
| Builder fee (0.1% of subscriber notional, collected on-chain) | 50% of the builder fee **actually collected** on your Strategy's fills (= 0.05% of notional) | 30%; the remaining 20% is the referral pool, of which any part not paid to referrers goes to the platform |
| Strategy subscription | 97% of the price you set | 3% |
| Profit share | The rate you set, **0–12%** of the subscriber's net realised profit above the high-water mark | **1.5% charged on top** to the subscriber (the subscriber pays at most 13.5%) |
| Paid posts | Price minus $1 (minimum price $2) | $1 per sale |

7.2 **Earnings accrue only when the amount is actually collected** (for example, builder fees reported on-chain for fills, or deductions actually made from Fee Balances). Uncollected amounts are not owed. This includes subscribers in reduce-only mode, unpaid balances, and fills without the builder fee.

7.3 For in-house strategies, the creator share goes to the platform.

7.4 Our ledger is the record of your earnings. You must raise any dispute within [60] days of the statement.

## 8. Payouts

8.1 Payouts are made in **USDC on Hyperliquid** to the payout address you register and verify. They are made [monthly] [CONFIRM], subject to a minimum payout of [$●]. They are prepared by our system, approved by two different administrators, and signed with a hardware wallet.

8.2 **Changing your payout address** requires step-up authentication, and may be followed by a [48]-hour security hold. You are responsible for the address being correct. **Transfers on the blockchain cannot be reversed.**

8.3 **Holds.** We may delay or withhold a payout in any of these cases:
- while KYC is incomplete;
- during an investigation of suspected breach, fraud, manipulation or chargebacks;
- where the law requires it (including sanctions);
- during a reconciliation mismatch.

8.4 **Taxes.** You are responsible for all taxes on your earnings. We may withhold or report as the law requires, and may ask for tax information. [COUNSEL: Malaysian withholding tax on payments to non-resident creators; e-invoicing; whether creator payments are royalties or service fees.]

## 9. Clawback and set-off

We may reverse credited earnings, deduct them from future earnings, or require you to repay them, where they relate to any of the following:
- (a) refunds, chargebacks or payment disputes by subscribers;
- (b) calculation or system errors;
- (c) fraud, manipulation or other breach of this Agreement;
- (d) amounts later found not to have been collected.

We may set off any amount you owe us against any amount we owe you. [COUNSEL: set a limitation period for clawback, e.g. 12 months.]

## 10. Delisting and ending your participation

10.1 **You may delist** a Strategy with [30] days' notice through Creator Studio. During the notice period, existing subscribers continue until their current period ends, or are moved to reduce-only, as the Platform decides and tells subscribers.

10.2 **We may delist** under section 3.4. We may end this Agreement with [14] days' notice, or immediately for breach or for legal, regulatory or security reasons.

10.3 **After termination:**
- your licence to us continues under section 6.2(b) and (c);
- earnings that were properly accrued and collected are paid after deducting clawbacks and set-offs;
- sections 4, 5, 6.3, 6.4, 9, 11 and 12 survive.

## 11. Your regulatory responsibility

You are responsible for any licence, registration or authorisation you need, where you live, to create and offer strategies or posts. If you need a licence you do not hold, you must not act as a Creator. **You must tell us immediately** if a regulator contacts you about your Creator activities. [COUNSEL: see the header flag. This clause allocates responsibility between the parties. It does not answer the Platform's own licensing position.]

## 12. Liability and indemnity

12.1 To the maximum extent permitted by law:
- the limitation-of-liability, disclaimer and force majeure clauses of the Terms apply to you as a Creator;
- **we are not liable for lost earnings** caused by delisting, pausing, kill switches, market changes, subscribers' actions or Hyperliquid events.

12.2 You indemnify us against all claims, losses, fines and costs arising from any of the following:
- a breach of your warranties or of section 5;
- your Scripts or posts;
- your regulatory status;
- claims by subscribers or third parties relating to your conduct.

12.3 Nothing limits liability that cannot lawfully be limited (see Terms clause 17.4).

## 13. Relationship

You are an independent contractor. You are not our employee, agent, partner or joint venturer. You may not bind us or present yourself as speaking for us.

## 14. Governing law and disputes

Terms clauses 21 and 22 apply: Malaysian law, and AIAC arbitration seated in Kuala Lumpur [COUNSEL]. Because creators are business users, the consumer-protection concerns about arbitration are reduced. [COUNSEL: confirm.]

## Open questions for counsel (this document)

1. Does hosting and executing third-party strategies for a share of subscription and performance fees require the Platform (and/or creators) to hold a Capital Markets Services Licence, or to register as a registered person? Is it relevant whether creators are Malaysian or foreign?
2. Can we legally monitor creators' personal on-chain trading, and require disclosure of their accounts? Is a front-running "blackout window" enforceable?
3. Withholding tax, e-invoicing and characterisation of payments to resident and non-resident creators.
4. Anti-money-laundering: whether our creator KYC is sufficient, and which records we must keep.
5. Enforceability of clawback and set-off, and whether a limitation period is needed.
6. IP: is the licence scope (including archival copies after termination) adequate and fair?

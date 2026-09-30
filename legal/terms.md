# aijalon.trade — Terms of Service

Version: 2026-09-30
Consent record: `doc=terms` (contexts: `site_entry`, `subscribe`, `creator`)
Status: **DRAFT — NOT IN FORCE**

> **DRAFT FOR REVIEW BY A MALAYSIAN-QUALIFIED LAWYER. NOT LEGAL ADVICE. NOT YET IN FORCE.**
>
> The owner asked for these Terms to push liability away from the operator as far as possible. They are drafted to be as protective as the law allows, but **no contract can exclude all liability**. In particular:
>
> - **Malaysian law limits exclusions.** The Contracts Act 1950 makes agreements with an unlawful object void and restricts agreements that stop a party from enforcing their rights through the courts. The Consumer Protection Act 1999 has provisions on unfair contract terms (procedural and substantive unfairness) and says its protections cannot be contracted out of. **Whether that Act covers this service is an open question** (it has exclusions for some financial and securities matters). Liability for fraud or wilful misconduct cannot be excluded. Liability for gross negligence, or for death or personal injury caused by negligence, very probably cannot be excluded either.
> - **Overbroad clauses can backfire.** A court or tribunal may strike down an exclusion clause that goes too far, and then no limit applies at all. So each exclusion below carries a "to the maximum extent permitted by law" qualifier and a carve-out (clause 19.4). Counsel should decide whether to narrow any clause to make it more likely to hold.
> - **A tick-box does not replace a licence.** Users' acknowledgements and waivers do not replace any licence, registration or approval the operator may need. Examples: a Capital Markets Services Licence under the Capital Markets and Services Act 2007 (possibly for fund management, investment advice or dealing in derivatives); compliance with the Securities Commission Malaysia's digital asset framework; Bank Negara Malaysia rules on stored-value / e-money (the prepaid Fee Balance). **We have not verified that the operator holds, or does not need, any licence.** Calling ourselves "only a software provider" does not bind a regulator.
> - The Personal Data Protection Act 2010 (as amended in 2024) applies separately. See `privacy.md`.
>
> Markers used: **[COUNSEL]** = needs legal input. **[CONFIRM]** = owner business decision. **[VERIFY]** = technical fact to check before go-live. **[●]** = placeholder to fill in. Remove this box, and all markers, before publishing.

---

## Key points in plain English

This summary is part of these Terms, but the detailed clauses below take priority over it.

1. **We never hold your trading money.** Your funds stay in your own Hyperliquid account. You let our software trade for you by approving an "agent wallet". On Hyperliquid, an agent wallet can place and cancel trades but **cannot withdraw or transfer funds**. You can revoke it at any time.
2. **You can lose all the money in your trading account**, and possibly more than your chosen allocation. Perpetual futures are leveraged and can be liquidated. Read the Risk Disclosure.
3. **We do not give investment advice.** Strategies are automated rules written by us or by independent creators. Nothing on the site is a recommendation for you personally.
4. **Nothing is guaranteed.** That includes profits, execution, uptime, strategy behaviour, and the Hyperliquid network itself.
5. **Fees are charged automatically.** A builder fee of 0.1% of notional is charged on every order we place (Hyperliquid collects it on-chain). Subscriptions, profit share, paid posts and plan fees are deducted automatically from your prepaid Fee Balance. If your Fee Balance runs out, your strategies go **reduce-only**: they can close positions but will not open new ones.
6. **Our liability is limited** to the maximum extent the law allows. You also agree to compensate us for losses caused by your breach of these Terms.
7. **Malaysian law governs these Terms.** Disputes go to arbitration in Kuala Lumpur [COUNSEL].

---

## 1. About these Terms

1.1 **Who we are.** aijalon.trade (the "**Platform**") is operated by [Operator legal name] Sdn. Bhd. (Company No. [●]), a company incorporated in Malaysia with its registered address at [●] ("**we**", "**us**", "**our**"). [COUNSEL: confirm entity, and whether a separate entity should operate the creator marketplace or receive fees.]

1.2 **Agreement.** These Terms form a binding contract between you and us. You accept them by ticking the acceptance box at the site entry gate and again when you subscribe to a strategy. Under Malaysian law, a contract formed electronically is generally valid. We record the version you accepted, the time, and a hashed network identifier. [COUNSEL: confirm that click-wrap acceptance with a recorded version is enough under the Electronic Commerce Act 2006 and the Contracts Act 1950, and whether a copy must be sent to the user.]

1.3 **Other documents.** These Terms include the following documents, each as updated from time to time:
- Risk Disclosure (`risk-disclosure.md`)
- Privacy Notice (`privacy.md`)
- Liability Waiver and Assumption of Risk (`liability-waiver.md`)
- Jurisdiction and Eligibility Attestation (`jurisdiction.md`)
- each Strategy Subscription Acknowledgement you accept (`subscription-ack.md`)
- Acceptable Use Policy (`acceptable-use.md`)
- Refund Policy (`refund-policy.md`)
- if you are a creator, the Creator Agreement (`creator-agreement.md`)

If these documents conflict, the order of priority is: (a) the Creator Agreement, for creator matters only; (b) these Terms; (c) the Liability Waiver; (d) the Subscription Acknowledgement; (e) the others. The Privacy Notice always governs how personal data is handled.

1.4 **Language.** These Terms are written in English. If we provide a translation, the English version prevails to the extent permitted by law. [COUNSEL: whether a Bahasa Malaysia version is required or advisable for consumer terms. A Malay version *is* required for the PDPA notice; see `privacy.md`.]

## 2. Eligibility

2.1 You may use the Platform only if **all** of the following are true:
- (a) you are at least **18 years old** and have full legal capacity to contract;
- (b) you are not resident in, located in, incorporated in, or a citizen of a **Restricted Jurisdiction** listed in `jurisdiction.md`, and you are not accessing the Platform from one;
- (c) you are not on, or owned or controlled by anyone on, any sanctions list that applies to us or our service providers (for example, lists of the United Nations, Malaysia, the United States, the European Union or the United Kingdom) [COUNSEL: which lists should apply];
- (d) using the Platform and trading perpetual futures is lawful for you where you live and where you are;
- (e) you are allowed to use Hyperliquid under Hyperliquid's own terms and access restrictions [VERIFY: review Hyperliquid's current terms and geo-restrictions];
- (f) you act on your own behalf, not for anyone else, and you are not using the Platform to manage other people's money.

2.2 You must not use a VPN, proxy or any other method to hide your location or get around these restrictions.

2.3 We may ask you to prove eligibility at any time. We may refuse, suspend or close any account, at our discretion, if we think these conditions are not met. [COUNSEL: whether user KYC or AML checks are legally required at any stage, e.g. if the operator is a reporting institution under anti-money-laundering law. Currently only creators do KYC.]

2.4 You may have only one account. Accounts are personal and cannot be transferred.

## 3. Your account and security

3.1 You sign in with Google or Apple. You must set up time-based one-time-password (TOTP) multi-factor authentication before doing anything on your account. Some sensitive actions need a fresh sign-in ("step-up"). Examples: connecting an agent, subscribing, changing allocation or leverage, and withdrawing.

3.2 You are responsible for keeping these secure: your Google or Apple account, your MFA device, your own Hyperliquid master wallet and its private keys or seed phrase, and every device you use. **We never ask for your seed phrase or master private key. Never give them to anyone.**

3.3 Tell us straight away at [security@aijalon.trade] if you suspect unauthorised access. To the maximum extent permitted by law, we are not liable for losses caused by the compromise of your credentials, devices, email or master wallet.

## 4. What the Platform is (and is not)

4.1 The Platform is a **technology service**. Creators and we publish automated trading strategies. You can subscribe to them. Our software then places orders on Hyperliquid perpetual futures markets in **your own** Hyperliquid account, within limits you choose.

4.2 **Non-custodial.** We do not hold, control or have the ability to withdraw the funds in your Hyperliquid account. Hyperliquid is an independent decentralised network that we do not own, operate or control. Your positions, margin and collateral exist on that network, not with us. The only funds we hold for you are your prepaid **Fee Balance** (clause 10).

4.3 **We are not your broker, adviser, fiduciary, trustee or agent.** Nothing in these Terms creates any such relationship. We do not act as a counterparty to your trades. [COUNSEL: this is a description, not a legal shield. Whether running third-party strategies in users' accounts, with performance-based fees, is "fund management", "dealing in derivatives" or "investment advice" under the Capital Markets and Services Act 2007 must be answered separately. See Open Questions.]

4.4 **Creators are independent.** Third-party creators are not our employees, agents or partners. We review strategies before listing them, but the review is limited. It does not mean we endorse, verify or guarantee a strategy.

## 5. Connecting your Hyperliquid account

5.1 **Agent wallet.** When you subscribe, we generate a dedicated agent key for you (named `aijalon`). You approve it on Hyperliquid by signing an `approveAgent` message with your master wallet. We encrypt the agent's private key with a hardware-backed key management service. Only our execution service can use it.

5.2 **Authority you give us.** By approving the agent, you authorise us, until you revoke that authority, to:
- (a) place, modify and cancel orders on your chosen trading address, but only in the markets of the strategies you subscribe to;
- (b) size orders using your allocation, the strategy's target weight and your maximum leverage, subject to our risk limits;
- (c) place **reduce-only** orders to reduce or close positions opened for your subscriptions. This includes when a subscription is past due, paused, cancelled or suspended, or when a kill switch or risk control is triggered (subject to clause 16.3);
- (d) set leverage and margin mode on the relevant markets as needed for the above [VERIFY: which actions the executor actually sends].

5.3 **What an agent cannot do.** Based on Hyperliquid's design as we understand it, an agent wallet **cannot withdraw or transfer** funds from your account. We rely on that design and do not control it. If Hyperliquid changes how agents work, we will try to notify you, but we are not liable for such changes. [VERIFY at go-live against Hyperliquid documentation.]

5.4 **Builder fee approval.** You also sign `approveBuilderFee`. This allows our builder address to charge up to 0.1% of notional on orders we place. Hyperliquid collects this fee on-chain at the time of each fill.

5.5 **Revoking.** You can revoke the agent or the builder-fee approval at any time, either directly on Hyperliquid or through the Platform where available. Once you revoke, we cannot place **any** orders for you, **including orders to close positions**. Any open positions then become entirely your responsibility to manage.

5.6 **Use a dedicated account.** We strongly recommend a dedicated Hyperliquid sub-account funded only with money you can afford to lose. If you trade manually, or run other bots or agents, on the same account and markets, this can conflict with our orders, change margin and liquidation levels, and distort your results and profit share. You accept those consequences.

5.7 **Margin across the whole account.** Your allocation is a **sizing target, not a loss limit**. Depending on your margin mode and Hyperliquid's rules, losses on positions we open can use up collateral across your whole trading account and lead to liquidation.

## 6. Strategies and subscriptions

6.1 **How it works.** Each strategy outputs a target position (weight) per market at each bar close. Our execution service converts that into orders for your account using your allocation and maximum leverage. It applies risk guards: market allow-lists, leverage caps, size limits relative to market volume and open interest, price-deviation checks, data-freshness checks, circuit breakers and kill switches. **Any guard may cause an order to be skipped, reduced, delayed or not filled.** This is intended behaviour, not a failure.

6.2 **Timing.** Orders are placed after a bar closes, not at the exact close. They include a **deliberate random delay** (by default up to about 10 minutes) and a randomised order across users. We do this for privacy and fairness. Your fills will differ from other subscribers' fills, from the strategy's published results, and from backtests.

6.3 **Order types.** We generally use immediate-or-cancel limit orders within a slippage cap. They may fill partly or not at all. Small changes may be skipped to avoid churn.

6.4 **Strategy status.** A strategy may at any time:
- hold a position;
- show "HOLDS — no active signals";
- stop producing signals (for example, if its data feed is stale or its signature is invalid, in which case we do not trade on it);
- be paused, changed to a new version, or delisted, by its creator or by us.

A new version resets the strategy's live track record. We are not obliged to keep any strategy available.

6.5 **Duration and renewal.** Subscriptions are monthly and renew automatically. Each renewal is prepaid from your Fee Balance until you cancel. Cancelling stops future renewals. [CONFIRM/PRODUCT: what happens to open positions when a subscription is cancelled. Options: (a) automatic reduce-only close-out, or (b) positions left open for the user. The acknowledgement text must match whichever is chosen.]

6.6 **Your settings.** You choose your allocation and maximum leverage. You are solely responsible for these choices. They should suit your circumstances.

## 7. No investment advice

7.1 Nothing on the Platform is investment, financial, legal, tax or other professional advice, or a recommendation or solicitation to buy, sell or hold any asset or derivative. This includes strategy descriptions, statistics, backtests, leaderboards, reviews, posts, alerts, emails and support messages.

7.2 We do not assess whether any strategy is suitable or appropriate for you. You decide on your own whether to subscribe, how much to allocate and what leverage to use. Consider getting independent advice.

7.3 Creator posts and reviews reflect the author's views only. They are not our views and we do not verify them.

[COUNSEL: whether publishing strategies with performance statistics, together with the paid-posts ("subletter") feature, amounts to "investment advice" as defined in the Capital Markets and Services Act 2007, whatever this disclaimer says.]

## 8. No guarantee

8.1 **Past performance, live or backtested, does not indicate future results.** We do not promise any return, any level of risk, or that a strategy will behave as it did before or as described.

8.2 Backtests are hypothetical. They can be fitted to history, especially after a script changes. Live track records may be short, may be based on few subscribers, and reset whenever a new version is released.

## 9. Referral programme

9.1 You may refer others with your referral code. A referral is fixed at the referred user's signup and cannot be changed afterwards. Self-referrals are not allowed: that includes the same person, the same wallet, the same device, or any arrangement designed to get around this rule.

9.2 Referrers earn a share of the referral part of the builder fees on referred users' orders. The share depends on a tier (currently Starter 50%, Partner 75% or Elite 100% of a pool equal to 0.02% of notional), as shown on the Platform. We may change, suspend or end the programme, its tiers or its rates at any time. Accrued, unpaid amounts will be handled fairly. [CONFIRM]

9.3 Referrers are **not our agents**. They must not:
- make performance claims or promises;
- give investment advice;
- target Restricted Jurisdictions;
- send spam or post misleading content.

We may withhold or claw back rewards earned in breach of these Terms or the Acceptable Use Policy.

[COUNSEL: check the programme against the Direct Sales and Anti-Pyramid Scheme Act 1993 and against advertising and financial-promotion rules. It is single-level and paid only from actual trading fees. Also check whether referral rewards are taxable, and what reporting that requires.]

## 10. Fees, Fee Balance and automatic deductions

10.1 **Fees.** The current fees are shown on the Platform and in each Subscription Acknowledgement. At the date of this version they are:

| Fee | Amount | How it is collected |
|---|---|---|
| Builder fee | 0.1% of the notional value of **every** order we place for you. This includes entries, exits, reduce-only and rebalancing orders, whether the trade wins or loses. | Collected on-chain by Hyperliquid when each order fills. It is on top of Hyperliquid's own trading fees. |
| Strategy subscription | Monthly price set by the creator | Prepaid from your Fee Balance at the start of each period |
| Profit share | Up to 15% (set by the creator) plus a 1.5% platform share, charged on **net realised profit above the high-water mark** for each subscription [CONFIRM: platform share charged "on top" of or "carved out" of the creator rate] | Calculated and deducted from your Fee Balance daily |
| Paid posts | Price set by the creator | Deducted from your Fee Balance at purchase |
| Platform plan | Free $0; Pro $20/month; Max $50/month | Deducted from your Fee Balance monthly |

10.2 **How profit share is calculated.** For each subscription separately, we add up the realised PnL of fills we placed for that subscription, minus trading fees, plus funding while the subscription held a position. When that cumulative total goes above its previous high point (the "high-water mark"), profit share is charged on the increase. Losses are never refunded, but they must be recovered before any new profit share is charged. **Profit share on one subscription is not reduced by losses on another.** You may pay profit share even when your account overall is down. Settlement runs daily at about 00:30 UTC.

10.3 **Fee Balance.** Your Fee Balance is a prepaid balance, recorded in US dollars, used only to pay Platform fees. You top it up with USDC sent on Hyperliquid, or by card or other methods through Stripe. The minimum top-up is $10. The Fee Balance:
- is **not** a deposit, savings account or investment;
- earns no interest;
- is not trading collateral;
- can be refunded only under the Refund Policy.

[COUNSEL — HIGH PRIORITY: whether a prepaid, multi-purpose balance funded by card or stablecoin is a "designated payment instrument" or e-money under the Financial Services Act 2013 (Bank Negara Malaysia approval), and whether holding USDC for users raises digital-asset custody issues under Securities Commission rules. The product may need redesigning, for example as a single-purpose prepaid service credit, or by charging directly per transaction.]

10.4 **Authorisation to deduct.** **You authorise us to deduct automatically from your Fee Balance, without asking you each time, every fee and amount you owe under these Terms.** This includes subscription renewals, daily profit share, paid-post purchases, plan fees, and corrections under clause 10.7. This authorisation lasts until your account is closed and all amounts are settled.

10.5 **Insufficient balance.** If your Fee Balance cannot cover an amount due:
- (a) the affected subscription becomes **past due**;
- (b) after a grace period (currently 72 hours), it becomes **reduce-only**. We will not open or increase positions, but may still reduce or close them;
- (c) we may suspend plan features.

We aim to alert you when your balance falls to about 50%, 20% and 0% of your estimated monthly need, but **we do not guarantee that alerts are delivered**. While a subscription is reduce-only you may miss trades, and your results will differ from the strategy's. **To the maximum extent permitted by law, we are not liable for any loss, missed gain or tracking difference caused by an insufficient Fee Balance.** Amounts that remain unpaid are a debt you owe us. We may deduct them from future top-ups.

10.6 **Price changes.** We may change fees prospectively. We will give at least [30] days' notice of any increase to a fee on an existing subscription or plan [COUNSEL: whether this is sufficient]. The builder fee can never exceed the maximum rate you approved on-chain.

10.7 **Records and errors.** Our ledger is our record of fees and balances. If we make a calculation error, we may correct it by crediting or debiting your Fee Balance. You must raise any fee dispute within [60] days of the charge.

10.8 **Taxes.** Fees do not include any applicable taxes (for example, service tax), unless stated otherwise. You are responsible for your own taxes on trading results. [COUNSEL: whether SST or service tax applies to these fees, and how that must be shown to customers.]

10.9 **Currency.** Fees are in US dollars. Your card issuer or payment provider may charge currency conversion and other fees. We are not responsible for those.

## 11. Your conduct

You must comply with the Acceptable Use Policy. In particular, you must not:
- manipulate markets;
- try to extract, reverse-engineer or reconstruct strategy code;
- interfere with the Platform's security;
- use the Platform for money laundering, sanctions evasion or any other unlawful purpose.

## 12. Intellectual property

12.1 We, our licensors and creators own the Platform, including its software, design, content, data and trade marks. We give you a limited, personal, revocable, non-transferable licence to use it as these Terms allow.

12.2 **Strategy code is confidential.** Subscribers never receive it and get no rights in it. You must not try to get, infer, decompile or reconstruct any strategy's code or logic, whether systematically (for example, by scraping, or by collecting other subscribers' trades) or in any other way. However, you may freely use publicly available on-chain data in general.

12.3 **Your content.** When you post reviews, comments or other content, you give us a worldwide, royalty-free, non-exclusive licence to host, display, adapt and distribute it for running and promoting the Platform. You confirm you have the rights to grant this licence.

12.4 **Feedback.** We may use any feedback you give us without restriction or payment.

## 13. Third-party services

The Platform relies on third parties we do not control. These include Hyperliquid (including its validators, oracles, bridge, and HIP-3 market deployers), your wallet software, Stripe, Google, Apple, Firebase and Google Cloud, Cloudflare, our KYC provider, and email and Telegram. Their terms apply to your use of them. **To the maximum extent permitted by law, we are not liable for their acts, omissions, failures, changes or outages.**

## 14. Suspension and protective actions

14.1 We may, at any time and without prior notice, suspend or limit your account, any subscription, any strategy or any market, or the entire Platform. We may do this if we reasonably believe it is needed:
- for security;
- for risk management or market integrity;
- to follow the law, a regulator or a court;
- because of suspected fraud, abuse or breach of these Terms;
- because of a Hyperliquid or market incident;
- to protect users or us.

14.2 **Kill switches.** We operate global and per-market "kill switches" and "pause new entries" controls. Some are triggered automatically by anomaly detection, for example unusual oracle deviation, open-interest spikes or reconciliation mismatches. **Using, or not using, a kill switch or risk control may cause losses or missed gains. For example, a position may be left open during a pause, or closed at a bad price. To the maximum extent permitted by law, we are not liable for these outcomes, provided we act in good faith.** [COUNSEL: whether "in good faith" should be kept; it supports enforceability but opens a factual dispute.]

## 15. Termination

15.1 **By you.** You can cancel subscriptions and close your account at any time in the app. Closing your account does not close your positions on Hyperliquid. Revoke the agent if you do not want us to place any further orders.

15.2 **By us.** We may end these Terms or close your account at any time with [14] days' notice. We may do so immediately where clause 14.1 applies, or where continuing would be unlawful or expose us to regulatory, legal or reputational risk.

15.3 **What happens at termination.**
- (a) We stop opening new positions.
- (b) We may place reduce-only orders to close positions opened for your subscriptions, if we reasonably believe that is in your interest or required. We are not obliged to do so. Otherwise, positions remain open and are your responsibility. [CONFIRM/COUNSEL]
- (c) Fees already accrued stay payable.
- (d) Any Fee Balance left over is handled under the Refund Policy.
- (e) Clauses 7, 8, 10 (for accrued amounts), 12, 13 and 16 to 21 survive termination.

## 16. Disclaimers

16.1 **The Platform, strategies, data, statistics, backtests and all content are provided "as is" and "as available".** To the maximum extent permitted by law, we disclaim all warranties, conditions and representations, whether express, implied or statutory. These include merchantability, satisfactory quality, fitness for a particular purpose, accuracy, non-infringement, and uninterrupted or error-free operation.

16.2 We do not warrant any of the following:
- that orders will be placed, placed on time, filled, or filled at any particular price;
- that data, prices, oracle values, statistics or backtests are accurate or complete;
- that a strategy's code works as its creator intends;
- that the Platform is free of bugs, viruses or security vulnerabilities.

16.3 We try to place reduce-only exits when a subscription is reduce-only, paused or cancelled, or when protective controls apply. **We do not guarantee that any exit will be placed or filled.** It may not be, for example, because of guards, outages, revoked approvals, a lack of liquidity or a kill switch.

## 17. Limitation of liability

17.1 **Excluded losses.** To the maximum extent permitted by law, we (and our directors, officers, employees, contractors and affiliates) are not liable to you, whether in contract, tort (including negligence), breach of statutory duty or otherwise, for any of the following:
- (a) trading losses, including liquidations, slippage, funding, missed trades, and any difference between your results and a strategy's published results or backtests;
- (b) loss of profit, revenue, opportunity, goodwill or anticipated savings;
- (c) loss or corruption of data;
- (d) losses caused by Hyperliquid or any other third-party service, including validator or deployer decisions, oracle failures, market halts, delistings and forced settlements;
- (e) losses caused by an insufficient Fee Balance, or by your settings, other activity on your account, or failure to secure your credentials or wallet;
- (f) losses caused by a strategy, a creator, or any creator's acts or omissions;
- (g) indirect, consequential, special, incidental or punitive loss;

in each case even if the loss was foreseeable or we were told it was possible.

17.2 **Overall cap.** To the maximum extent permitted by law, our total liability to you for all claims arising out of or relating to the Platform or these Terms is limited to the **greater of**:
- (a) the fees you actually paid **to us** (excluding amounts passed on to creators and referrers) in the **three months** before the event giving rise to the first claim; or
- (b) **USD 100**.

[COUNSEL: whether the cap is reasonable and enforceable. A very low cap is more likely to be found unfair. Consider a 12-month look-back.]

17.3 **Time limit.** To the maximum extent permitted by law, you must bring any claim within [one year] of becoming aware of it. [COUNSEL: the Limitation Act 1953 sets statutory limitation periods, and contractual shortening may be void or restricted (Contracts Act 1950). Likely remove this clause, or recast it as a notice-of-claim requirement.]

17.4 **Nothing in these Terms excludes or limits liability that cannot lawfully be excluded or limited.** This includes liability for:
- (a) fraud or fraudulent misrepresentation;
- (b) our wilful misconduct;
- (c) death or personal injury caused by our negligence;
- (d) [gross negligence] [COUNSEL];
- (e) any rights you have under consumer protection or other law that cannot be excluded by contract.

## 18. Indemnity

To the maximum extent permitted by law, you will indemnify us, and keep us and our directors, officers, employees and contractors indemnified, against all claims, losses, liabilities, penalties, costs and expenses (including reasonable legal fees) arising from:
- (a) your breach of these Terms or of the law;
- (b) your false attestation of eligibility or jurisdiction;
- (c) your content;
- (d) your trading or other activity on your Hyperliquid account;
- (e) a chargeback or payment dispute you raise without good grounds.

[COUNSEL: indemnities from consumers may be treated as unfair. Consider limiting this clause to breach, fraud and false attestation.]

## 19. Force majeure

19.1 To the maximum extent permitted by law, we are not liable for any failure or delay caused by events beyond our reasonable control. Examples:

**Hyperliquid and markets**
- (a) outages, halts, congestion, forks, rollbacks, bugs or upgrades of the Hyperliquid L1, its API, its bridge or its order books;
- (b) **decisions or actions of Hyperliquid validators or governance.** Examples: delisting a market, forcing settlement of positions at a price they choose, changing margin, fee or agent rules, or intervening in liquidations or liquidity vaults;
- (c) **oracle failures, manipulation or incorrect prices**, including on builder-deployed (HIP-3) markets, where the market deployer may control or influence oracle prices, trading halts or settlement;
- (d) market manipulation by third parties, extreme volatility, or a lack of liquidity;
- (e) a stablecoin losing its peg, or being frozen, blocked or discontinued (including USDC);

**Other events**
- (f) failures of cloud, DNS, CDN, payment, identity, email or messaging providers;
- (g) cyber-attacks, including denial-of-service attacks, where we took reasonable precautions;
- (h) changes in law or regulation, regulatory or court action, sanctions, or government orders;
- (i) natural disasters, epidemics, war, terrorism, civil unrest, strikes, or power or telecommunication failures.

19.2 Where such an event affects market prices or settlement, positions are settled on whatever terms Hyperliquid, its validators or the market deployer determine. **We have no obligation to compensate you for the difference.** [COUNSEL: check the wording given the doctrine of frustration under the Contracts Act 1950.]

## 20. Changes

20.1 **Service changes.** We may change, add or remove features, markets, strategies, fees (see clause 10.6) or limits at any time.

20.2 **Changes to these Terms.** We may update these Terms. For a material change, we will show you the new version at the site gate, or next time you subscribe, and ask you to accept it. **Until you accept, you may not be able to open new subscriptions or use some features.** Existing subscriptions may continue on a reduce-only basis. [PRODUCT: confirm the gate behaviour for existing subscribers who do not re-accept.] For non-material changes, the new version applies from its publication date.

## 21. Governing law and disputes

21.1 **Governing law.** Malaysian law governs these Terms and any dispute or claim arising out of or relating to them or to the Platform, including non-contractual disputes and claims.

21.2 **Talk to us first.** Before starting proceedings, contact us at [disputes@aijalon.trade] with details. We will both try in good faith to resolve the dispute within 30 days.

21.3 **Arbitration** [COUNSEL: draft option]. Any dispute not resolved under clause 21.2 will be finally resolved by arbitration administered by the **Asian International Arbitration Centre (AIAC)** under the AIAC Arbitration Rules in force at the time. The arbitration will have:
- a sole arbitrator;
- its seat in Kuala Lumpur, Malaysia;
- English as its language.

Each party pays its own costs, unless the arbitrator decides otherwise. The arbitration and the award are confidential.

21.4 **Exceptions.** Either party may seek urgent interim or injunctive relief from a court of competent jurisdiction. Nothing in this clause stops you from bringing a claim before a tribunal or court that you have a non-excludable right to use, for example the Tribunal for Consumer Claims, if it has jurisdiction. [COUNSEL: whether binding arbitration and any class-action waiver are enforceable against consumers in Malaysia, and whether the AIAC cost burden makes the clause unfair. Consider AIAC's fast-track or small-claims procedures, or Malaysian courts as the forum.]

21.5 **Individual claims.** To the extent permitted by law, disputes will be dealt with individually and not as a class or representative action. [COUNSEL]

## 22. General

22.1 **Notices.** We may give notices by email to your account email, in the app, or on the Platform. You agree to receive communications electronically.

22.2 **Assignment.** You may not transfer these Terms. We may transfer them to an affiliate, or to a successor of the Platform business, with notice to you.

22.3 **Severability.** If any part of these Terms is invalid or unenforceable, the rest stays in effect. The invalid part will be applied to the maximum extent permitted.

22.4 **No waiver.** If we delay or do not enforce a right, we have not waived it.

22.5 **Entire agreement.** These Terms, including the documents in clause 1.3, are the entire agreement between you and us about the Platform.

22.6 **Third-party rights.** No one else has rights under these Terms, except our directors, officers and employees to the extent of clauses 17 and 18.

## 23. Contact

- [Operator legal name] Sdn. Bhd., [registered address]
- Support: [support@aijalon.trade]
- Security: [security@aijalon.trade]
- Legal and disputes: [legal@aijalon.trade]
- Data protection officer: see `privacy.md`

[COUNSEL: the Consumer Protection (Electronic Trade Transactions) Regulations 2012 generally require online suppliers to show their name, registration number, contact details, a description of what they sell, prices and payment terms, and to let users correct errors before confirming an order. Confirm these apply and are met.]

---

## Open questions for counsel (this document)

1. **Licensing (critical).** Does running creator strategies in users' own accounts through an agent wallet, with a performance-based profit share, require a Capital Markets Services Licence under the Capital Markets and Services Act 2007? The candidate activities are fund management (portfolio management), dealing in derivatives, and investment advice. Are Hyperliquid perpetual futures "derivatives" (or securities, including via the Securities Commission's prescription order for digital currencies and tokens) for this purpose? Is the answer different for in-house strategies and for third-party creator strategies?
2. **Fee Balance.** Is it e-money or a designated payment instrument under the Financial Services Act 2013? Is holding USDC for users digital-asset custody? Is there a single-purpose or limited-network exemption?
3. **Consumer Protection Act 1999.** Does the Act, including Part IIIA on unfair terms, apply to this service? Given the answer, which clauses in sections 10 and 14 to 19 are at risk?
4. Enforceability of the liability cap (clause 17.2), the shortened claim period (17.3), the consumer indemnity (18), and the arbitration and class waiver (21).
5. Are click-wrap acceptance and a hashed-IP audit trail sufficient evidence of assent? Must a durable copy be given to the user?
6. **Serving Malaysian residents.** Should Malaysia itself be a restricted jurisdiction until licensing is clear? (See `jurisdiction.md`.)
7. Anti-money-laundering: is the operator a reporting institution (for example, as a digital-asset-related business)? Is customer due diligence required for all users?
8. SST or service tax on platform fees; tax treatment of creator and referral payouts; e-invoicing obligations.
9. Is the referral programme compliant with the Direct Sales and Anti-Pyramid Scheme Act 1993 and advertising rules?
10. What approval does Stripe need for this business category? (Stripe restricts investment and crypto-related businesses.) Could an account closure strand users' Fee Balances?
11. Unilateral closing of positions on termination or kill switch (clauses 5.2(c) and 15.3(b)): is there any liability exposure, and should users give a specific election?

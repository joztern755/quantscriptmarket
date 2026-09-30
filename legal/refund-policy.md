# aijalon.trade — Refund Policy

Version: 2026-09-30
Consent record: part of `doc=terms` (incorporated by reference)
Status: **DRAFT — NOT IN FORCE**

> **DRAFT FOR REVIEW BY A MALAYSIAN-QUALIFIED LAWYER. NOT LEGAL ADVICE. NOT YET IN FORCE.**
>
> A "no refunds" position is limited by law. It does not override non-excludable consumer rights (if the Consumer Protection Act 1999 applies), or card-scheme and Stripe dispute rules. Owner decisions (30 Sep 2026): **Stripe/processor fees are passed to the user**, so a top-up is credited net of the processor fee; and the refund position for the Fee Balance must be consistent with how the Fee Balance is legally characterised (see Terms clause 10.3 and the open questions). **[CONFIRM]** marks business choices still open.

---

## 1. Summary

| Item | Refundable? |
|---|---|
| Unused **Fee Balance** | Yes, on request or on account closure, as set out in section 2, less non-refundable items |
| **Processor fees** on Stripe top-ups | No. They are deducted when you top up and are not returned |
| **Builder fees** | No. Hyperliquid collects them on-chain for each fill |
| **Strategy subscription** (current month) | No, once the period has started, except as set out in section 3 |
| **Profit share** | No. Calculation errors are corrected |
| **Paid posts** | No, once unlocked, except as set out in section 5 |
| **Platform plan** (current month) | No, except as set out in section 6 |
| Promotional or bonus credits | No. They have no cash value |

## 2. Fee Balance refunds

2.1 **When.** You may ask for your unused Fee Balance to be refunded at any time. It will be refunded automatically if you close your account. [CONFIRM: whether withdrawal on demand is allowed while the account stays open, or only on closure. This choice may affect the Fee Balance's regulatory characterisation; see COUNSEL.]

2.2 **Amounts first deducted.** Before refunding, we deduct:
- amounts you owe, including accrued profit share not yet settled (we run a final settlement first);
- current subscription and plan fees;
- chargeback losses.

2.3 **How.**
- **USDC top-ups** are refunded in USDC on Hyperliquid, to your **verified master address**.
- **Stripe top-ups** are refunded to the original payment method where Stripe allows it. Otherwise they are refunded in USDC to your verified address, or by another method we agree with you.
- We refund the amount that was **credited** to your balance, not the gross amount you paid. **Processor fees are not refunded.**
- We refund [CONFIRM: most recent top-ups first, by method].

2.4 **Security and approval.** Refunds and withdrawals require step-up authentication. They are prepared by our system and approved by two different administrators, then signed with a hardware wallet (for USDC). We aim to process them within [5] business days. They may take longer during security reviews, reconciliation or legal holds. The minimum refund is [$●], except on account closure.

2.5 **Blockchain transfers are irreversible.** We are not responsible for losses caused by an address you provided that is wrong or has been compromised.

2.6 **Legal restrictions.** We may delay or refuse a refund where the law requires it. Examples: sanctions, a court order, or an anti-money-laundering investigation.

## 3. Strategy subscriptions

3.1 Subscriptions are prepaid monthly and renew automatically. **Cancelling stops the next renewal.** The current period is not refunded.

3.2 **Exceptions.** We credit your Fee Balance with a **pro-rata** amount for the unused days of the period if, during that period:
- **we** delist the strategy for reasons other than your breach; or
- the Platform is unavailable for trading for more than [72] consecutive hours because of a fault on **our** side (not Hyperliquid's, and not a force majeure event).

We decide in good faith whether an exception applies. [CONFIRM]

3.3 A strategy holding or showing "HOLDS — no active signals", having a losing period, or being paused by its creator is **not** grounds for a refund.

## 4. Profit share and builder fees

4.1 **Profit share** is charged only on net realised profit above the high-water mark. It is not refunded if the profit is later lost. Instead, the high-water mark ensures that you do not pay again until the loss is recovered.

4.2 If we find a **calculation error**, we correct it by crediting or debiting your Fee Balance.

4.3 **Builder fees** are collected on-chain by Hyperliquid for each fill. We cannot reverse them. If an order was placed in error because of **our** system fault, we may, at our discretion, credit the builder fee on that order to your Fee Balance. [CONFIRM]

## 5. Paid posts

Paid posts are digital content, available immediately. **They are not refundable once unlocked.** The exception is where the content is materially different from its description, or not accessible because of our fault. In that case, email us within [7] days.

## 6. Platform plans

Plan fees are prepaid monthly. A downgrade takes effect at the next renewal. [CONFIRM: whether an upgrade in the middle of a period is charged pro-rata.] Plan fees are not refunded for the current period, except as set out in section 3.2 (Platform unavailability).

## 7. Payment disputes and chargebacks

7.1 **Contact us first.** Please contact [support@aijalon.trade] before disputing a payment with your bank or card issuer. Most issues can be solved faster this way.

7.2 **If you file a chargeback,** we may do any of the following:
- (a) debit the disputed amount, and any dispute fee charged to us, from your Fee Balance [COUNSEL: whether passing on dispute fees is permitted];
- (b) suspend top-ups by card, suspend new subscriptions, or move subscriptions to reduce-only while the dispute is open;
- (c) provide Stripe and your card issuer with evidence of your use of the service. That evidence includes: consent records, subscription acknowledgements, ledger entries and trade records.

7.3 If a dispute is decided **in your favour**, we will not recover the amount again from you, unless the dispute was fraudulent. If it is decided **in our favour**, we restore any amounts we had debited.

7.4 **Filing chargebacks for services you received** is a breach of the Acceptable Use Policy.

## 8. Your statutory rights

Nothing in this Policy affects any right to a refund or remedy that you have under law and that cannot be excluded.

## 9. Contact

[support@aijalon.trade]

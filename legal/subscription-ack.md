# aijalon.trade — Strategy Subscription Acknowledgement (template)

Version: 2026-09-30
Consent record: `doc=subscription_ack`, `context=subscribe`, `strategy_id=<id>` (plus `terms`, `risk`, `waiver`, `jurisdiction` re-accepted with `context=subscribe`)
Status: **DRAFT — NOT IN FORCE**

> **DRAFT FOR REVIEW BY A MALAYSIAN-QUALIFIED LAWYER. NOT LEGAL ADVICE. NOT YET IN FORCE.**
>
> This is the per-strategy acknowledgement shown in the Subscribe wizard, just before the step-up confirmation. Every box must be ticked. Nothing may be pre-ticked. **These acknowledgements are evidence of disclosure. They do not exclude liability that cannot be excluded by law, and they do not replace any licence, suitability or appropriateness assessment that may be required** (for example, under the Capital Markets and Services Act 2007) **[COUNSEL]**.
>
> Implementation notes:
> - `{{placeholders}}` are filled from the strategy record, the user's inputs, and `Economics` in `backend/app/config.py`. Render the fees from config, never hard-coded.
> - Record the rendered text's hash along with the version, so we can later prove exactly what the user saw [PRODUCT].
> - Profit share: creator 0–12%, plus platform 1.5% **on top** (maximum 13.5% total).
> - Stripe processor fees are deducted from top-ups before crediting.

---

## You are subscribing to: **{{strategy_name}}** (version {{version_number}})

| | |
|---|---|
| Creator | {{creator_display_name}} {{#in_house}}(in-house strategy operated by aijalon.trade){{/in_house}}{{^in_house}}(independent third-party creator, not aijalon.trade){{/in_house}} |
| Markets | {{markets}}, e.g. `xyz:SILVER` |
| Market type | {{market_types}}, e.g. "Builder-deployed (HIP-3) market: thin liquidity, deployer-controlled oracle, may be halted, delisted or force-settled" |
| Direction | {{direction}}, e.g. "Long-only" / "Long and short" |
| Timeframe | {{timeframe}} bars; orders are placed after each bar closes, with a random delay of up to {{jitter_max_minutes}} minutes |
| Strategy maximum leverage | {{strategy_max_leverage}}x |
| **Your maximum leverage** | **{{user_max_leverage}}x** (the effective leverage is the lowest of your setting, the strategy maximum and the market maximum) |
| **Your allocation** | **{{allocation_usd}}**. This is a sizing target, **not a loss limit**. |
| Trading account | {{trading_address_short}} ({{account_kind}}, e.g. "sub-account", recommended) |
| Live track record | Since {{live_since}} ({{live_days}} days) {{#stats_hidden}}. Fewer than 5 subscribers, so live statistics are not shown.{{/stats_hidden}} |
| Status | {{status_badge}}, e.g. "HOLDS — no active signals" |

### Fees for this subscription

| Fee | Amount |
|---|---|
| Builder fee | **{{builder_fee_pct}}** (0.1%) of the notional value of **every** order, including exits, collected on-chain by Hyperliquid. This is on top of Hyperliquid's own trading fees. |
| Subscription | **{{price_monthly_usd}} per month**, prepaid now and at each renewal from your Fee Balance |
| Profit share | **{{profit_share_creator_pct}}** to the creator **+ {{platform_profit_share_pct}}** (1.5%) to the platform = **{{profit_share_total_pct}}** of net realised profit above this subscription's high-water mark. Settled daily. |
| Your platform plan | {{plan_name}}, {{plan_price_usd}} per month ({{active_strategies_used}} of {{plan_max_strategies}} strategies used) |
| Estimated monthly fee need | ~{{est_monthly_need_usd}} (an estimate only, based on {{estimate_basis}}) |
| Fee Balance now | {{fee_balance_usd}} {{#low_balance}}. **Low: top up to avoid reduce-only mode.**{{/low_balance}} |

{{#backtest_warning}}
> **Warning:** Backtest after a script change can be fitted to history; not proven live yet.
{{/backtest_warning}}

## Acknowledgements (tick each one)

- ☐ **1. Risk of total loss.** I understand that {{strategy_name}} trades leveraged perpetual futures in my own Hyperliquid account, and that **I can lose all the funds in that account**, including more than my allocation of {{allocation_usd}}.
- ☐ **2. Markets.** I understand the specific risks of {{markets}}. {{#has_hip3}}These include builder-deployed (HIP-3) market risks: thin liquidity, manipulation, a deployer-controlled oracle, halts, delisting and forced settlement.{{/has_hip3}}
- ☐ **3. Leverage.** I have chosen a maximum leverage of {{user_max_leverage}}x. I understand that liquidation can occur, and that prices can gap past my liquidation level.
- ☐ **4. Agent authority.** I authorise aijalon.trade's agent wallet to place, modify and cancel orders in {{markets}} on {{trading_address_short}} for this subscription, including reduce-only exits. I understand that the agent cannot withdraw my funds. I understand that if I revoke it, aijalon.trade cannot close my positions for me.
- ☐ **5. Fees and automatic deduction.** I have read the fees above. **I authorise aijalon.trade to deduct the subscription price, the profit share ({{profit_share_total_pct}}) and all other fees automatically from my Fee Balance.** I understand that the builder fee is charged on every order, whether the trade wins or loses.
- ☐ **6. Fee Balance and reduce-only.** I understand that if my Fee Balance cannot cover what is due, this subscription becomes past due. After {{grace_hours}} hours it becomes **reduce-only**: no new positions, exits only. I may then miss trades.
- ☐ **7. No advice, no guarantee.** I understand that aijalon.trade and {{creator_display_name}} do not give me investment advice, and have not assessed whether this strategy suits me. I understand that no return is guaranteed.
- ☐ **8. Performance.** I understand that backtests are hypothetical and may be fitted to history. I understand that live results may be short, may reset with new versions, and do not predict future results. I understand that **my results will differ** from the published results, because of delays, guards, fees and fills.
- ☐ **9. Execution.** I understand that orders are delayed on purpose by a random amount, that they may be skipped, partly filled or not filled, and that kill switches and risk guards may stop trading, including in some cases exits.
- ☐ **10. Strategy changes.** I understand that this strategy may hold, stop, be updated to a new version, paused or delisted at any time. [PRODUCT: say what happens to my positions on cancellation or delisting: {{on_cancel_behaviour}}]
- ☐ **11. Account hygiene.** I understand that manual trading or other bots on the same account and markets can conflict with this strategy. I have been advised to use a dedicated sub-account.
- ☐ **12. On-chain visibility.** I understand that my trades are public on Hyperliquid, and that my wallet may be linked to this strategy and copied or traded against by others.
- ☐ **13. Terms (again).** I have read and accept again the current **Terms of Service** (v{{terms_version}}), **Risk Disclosure** (v{{risk_version}}), **Liability Waiver** (v{{waiver_version}}) and **Jurisdiction Attestation** (v{{jurisdiction_version}}). I confirm that I am not in, or from, a Restricted Jurisdiction.

**[ Confirm subscription ]** (requires a fresh sign-in with MFA)

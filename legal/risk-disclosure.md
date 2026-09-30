# aijalon.trade — Risk Disclosure

Version: 2026-09-30
Consent record: `doc=risk` (contexts: `site_entry`, `subscribe`)
Status: **DRAFT — NOT IN FORCE**

> **DRAFT FOR REVIEW BY A MALAYSIAN-QUALIFIED LAWYER. NOT LEGAL ADVICE. NOT YET IN FORCE.**
>
> This disclosure aims to be complete and frank. A good risk disclosure is the strongest honest protection the operator has. It does **not** exclude liability on its own, and it does not replace any licence, suitability or appropriateness assessment, or product-disclosure format that Malaysian law may require. For example, the Capital Markets and Services Act 2007 regime and Securities Commission Malaysia guidelines may apply if the service is found to be a regulated activity. **[COUNSEL]** Check whether a prescribed form of risk disclosure statement exists for derivatives or digital-asset products, and whether a knowledge or appropriateness test must be passed before a user can subscribe. Technical statements marked **[VERIFY]** must be checked against the live system and Hyperliquid documentation before publication.

---

**Read this whole document before using aijalon.trade. If you do not understand any part of it, do not use the Platform.**

**You can lose all of the money in your Hyperliquid trading account. Only trade with money you can afford to lose completely.**

## 1. Perpetual futures and leverage

- Perpetual futures ("perps") are leveraged derivatives. A small price move against you can cause a large loss compared with your margin.
- Leverage makes both gains and losses bigger. At 2x leverage, a 50% adverse move can wipe out the margin behind the position. At 5x, a 20% move can.
- Prices can **gap**: they can jump past your liquidation price or intended exit without trading at the prices in between. This is especially likely over weekends, around news, or in illiquid markets.
- Your **allocation is a sizing target, not a stop-loss or a loss limit.** Depending on your margin mode and Hyperliquid's rules, losses can use up collateral across your whole trading account.

## 2. Liquidation

- If your account equity falls below Hyperliquid's maintenance margin, Hyperliquid may **liquidate** your positions. This can happen at unfavourable prices, and you may lose all margin tied to those positions. You may also have to pay liquidation-related fees.
- Hyperliquid may use backstop mechanisms such as liquidity vaults or auto-deleveraging. These can close positions, including profitable ones, at prices you did not choose. [VERIFY current Hyperliquid mechanics]
- We do not monitor your account to prevent liquidation. We do not top up your margin.

## 3. Builder-deployed (HIP-3) markets

Some markets, for example `xyz:SILVER` and `xyz:GOLD`, are **deployed by third parties** under Hyperliquid's HIP-3 framework, not by Hyperliquid validators. These markets may have:

- **thin liquidity:** wide spreads, low volume and small open interest. Your orders may move the price, fill partly, or not fill at all;
- **manipulation risk:** a well-funded actor may be able to push prices, oracle inputs or funding in a thin market. The goal may be to trigger liquidations or profit at others' expense;
- **deployer control:** the market deployer may control or influence the oracle price, trading halts, margin parameters and settlement. The deployer may act wrongly, be hacked, or stop running the market [VERIFY exact deployer powers];
- **delisting and forced settlement:** a market may be halted, delisted or **settled at a price set by the deployer or validators**. That price may differ greatly from the price you could otherwise have traded at;
- **reference-price risk:** a market that tracks an external asset (for example, silver or oil) may not track it closely, especially when the reference market is closed.

Our pre-trade guards limit order size relative to volume and open interest, and check the mark price against the oracle price. These guards **reduce but cannot remove** these risks. They can also stop the strategy from trading, which means your results will differ from the strategy's.

## 4. Funding

Perps charge or pay **funding** between longs and shorts, usually hourly. Funding can be large and can move quickly, especially in thin or trending markets. It can turn a winning position into a losing one. Funding is included in the profit-share calculation.

## 5. Execution: slippage, delays and missed trades

- **Slippage.** We use limit orders within a slippage cap. They may fill at worse prices than expected, fill partly, or not fill at all.
- **Deliberate random delays.** To protect your privacy and to be fair between subscribers, each user's orders are delayed by a random amount (by default up to about 10 minutes), and users are processed in random order. So:
  - you trade later than the bar close, and later than some other subscribers;
  - prices may move against you during the delay;
  - your results will differ from other subscribers' results and from published results.
- **Scheduling and outages.** Orders depend on our servers, cloud providers, the data feed, Hyperliquid's API and network connectivity. Any of these can fail or be delayed. Trades may then be late or missed.
- **Risk guards and kill switches** may skip, shrink or delay orders. Examples: market allow-lists, leverage caps, size limits, price-deviation and data-freshness checks, circuit breakers, and global or per-market kill switches, some of which trigger automatically. This includes exits in some situations.
- **Small changes are skipped** to avoid excessive trading. Your position may not match the strategy's target exactly.

## 6. Strategy behaviour

- A strategy may **stop trading, hold a position for a long time, or show "HOLDS — no active signals"**. Signal feeds can go stale. A signal with an invalid signature is rejected. Either way, no trades are placed.
- A strategy may be **paused, changed to a new version, or delisted** by its creator or by us. A new version may behave very differently.
- Creator code runs in a restricted sandbox. It may still contain **bugs** or behave unexpectedly in conditions it was not tested on.
- In-house strategies are long-only on daily bars. They may sit in drawdown or stay flat for long periods.
- **Creators are independent.** They may have conflicts of interest. We prohibit creators from trading against subscribers, front-running and manipulating markets, and we monitor for it, but we cannot guarantee that no creator does.
- **You never see the code.** You are relying on the strategy's description, statistics and our limited review.

## 7. Backtests and past performance

- **Backtests are hypothetical.** They are computed on historical data with assumed fees, funding and fills. They leave out real slippage, delays, guards, partial fills and market impact.
- **Backtests can be "fitted to history".** A script that has been changed until its backtest looks good may fail live. This is especially likely just after a script is uploaded or changed. That is why we show the warning: *"Backtest after a script change can be fitted to history; not proven live yet."*
- "Out-of-sample" periods reduce this risk, but do not remove it.
- **Past performance, live or backtested, does not indicate future results.** Live track records may be short. They reset with each new version. Aggregate figures (such as "$ made for users") come from subscribers whose timing, settings and fees differ from yours.
- Public statistics are shown only when a strategy has at least 5 subscribers. So some strategies show little or no live data.

## 8. Fees reduce returns

- The **builder fee (0.1% of notional) is charged on every order**, including exits and rebalancing, **whether the trade wins or loses**. It is on top of Hyperliquid's own trading fees. Frequent rebalancing or high leverage makes these fees much larger compared with your allocation.
- **Profit share** is calculated per subscription against a high-water mark. Losses on one subscription do not offset profit on another. **You can pay profit share while losing money overall.**
- Subscriptions and plan fees are payable whether or not the strategy trades or makes money.
- If your Fee Balance runs out, the subscription becomes **reduce-only** after a grace period. It will not open new positions, and your results will diverge from the strategy's.

## 9. On-chain transparency and privacy

- **Hyperliquid is a public blockchain.** Your trades, positions, fills, liquidations and account balances are visible to anyone who knows or can find your address.
- **Orders we place carry our builder code, which is publicly visible.** Anyone may be able to see that a trade was placed through aijalon.trade. [VERIFY how builder attribution appears in public data]
- Our random delays, per-user ordering and per-user agents make it **harder, but not impossible**, to link wallets or copy strategies. Analysts can **cluster** wallets that trade the same markets at similar times. Once your address is linked to a strategy, others may **copy, front-run or trade against** your positions and the strategy's.
- We never publish your address or show it to creators. But we cannot stop on-chain analysis.

## 9A. Platform orders are identifiable on-chain

- **Every order we place for you carries two public markers.** The first is our **builder code**, which is attached to every order so that Hyperliquid can collect our builder fee. The second is the order's **client order ID**, which always starts with the same fixed aijalon.trade prefix. Both markers appear in Hyperliquid's public order and fill data. [VERIFY exactly where each marker is shown in public Hyperliquid data]
- **This means anyone can tell that a trade came from aijalon.trade.** They can also make a list of the Hyperliquid accounts that trade through the Platform.
- **The markers do not say who you are or which strategy you follow.** They do not contain your name, your email or the strategy's name. The rest of the order ID is a scrambled code that only we can link to your subscription. [VERIFY]
- **Your identity or strategy can still be worked out.** Someone may connect your address to you from other sources, such as an exchange withdrawal, a social media post or an ENS-style name. Analysts can also **group together** Platform accounts that trade the same markets at around the same time, and so guess which accounts follow the same strategy. The markers make this easier, because they show which accounts to look at. Section 9 explains what can happen once this link is made.
- **We cannot switch these markers off.** The builder code is how our fee is charged. The order ID prefix is how we match fills to your subscription, which lets us calculate your results and profit share correctly.
- **What you can do:** use a separate Hyperliquid account just for the Platform, not your main wallet, and do not publicly link that account to your identity.

## 10. Hyperliquid network, smart contract and L1 risk

- Hyperliquid is a relatively new L1 blockchain with its own validator set, bridge and software. It may suffer bugs, exploits, outages, chain halts, rollbacks or consensus failures.
- **Validators and governance can intervene.** For example, they may delist a market and force settlement at a price they choose, change fee or margin rules, or change how agents and builder codes work. There are past examples of validators delisting a market and settling it at an administratively chosen price during a manipulation incident. [COUNSEL/VERIFY: whether to name specific incidents]
- **Bridge risk.** USDC reaches Hyperliquid through a bridge that could be exploited or frozen.
- **Regulatory action** against Hyperliquid, its frontends, or services connected to it could restrict your access.

## 11. Agent key compromise

- We hold an encrypted agent key for your account. It **can place trades but, by Hyperliquid's design, cannot withdraw or transfer funds** [VERIFY at go-live].
- If that key were stolen, an attacker could still place **unwanted trades** that cause losses. For example, they could trade your account into a thin market against a position they control. This would effectively extract value from your account.
- We protect agent keys with hardware-backed encryption, strict access separation, monitoring and kill switches. **No security is perfect.**
- **You can limit this risk.** Use a dedicated sub-account holding only what you are prepared to lose, and revoke the agent whenever you stop using the Platform.
- **Your master wallet is your responsibility.** If your master wallet's seed phrase or private key is compromised, all your funds may be stolen. We never ask for it.

## 12. Stablecoin risk

- Your account and your Fee Balance top-ups use **USDC**, a stablecoin issued by a third party. USDC could lose its peg to the US dollar, or be frozen, blacklisted, or redeemed at less than face value. So could any successor stablecoin that Hyperliquid uses.
- Fee Balance amounts are recorded in US dollars. We do not insure them. They are not a bank deposit, and no deposit insurance scheme covers them.

## 13. Platform and counterparty risk

- We are a young business. We could suffer outages, security incidents, regulatory action, or insolvency, or stop operating. Any of these could affect your subscriptions and your Fee Balance.
- The services we depend on can fail. These include Stripe, Google or Firebase, Cloudflare, our KYC provider, and email and Telegram.
- **The Platform may be restricted where you live**, now or in the future. If we must stop serving you, positions may be left open or closed at inconvenient times.

## 14. Legal and tax risk

- The law on crypto derivatives and automated trading services is changing and differs between countries. **You are responsible for making sure your use is lawful where you are.**
- Trading profits, funding, referral rewards and creator income may be taxable. **You are responsible for your own tax reporting.** We may provide exports (Max plan), but these are not tax advice, and they may be incomplete.

## 15. Your acknowledgement

By ticking the box, you confirm that you have read and understood this Risk Disclosure. You also confirm that you accept these risks, and that you are trading at your own risk with money you can afford to lose.

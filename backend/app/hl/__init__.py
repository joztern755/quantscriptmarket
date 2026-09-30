"""Hyperliquid integration (SPEC §6). Import submodules directly:

- ``app.hl.info``        InfoClient — read-only ``/info`` (retries, size cap, typed methods, pagination)
- ``app.hl.markets``     MarketCatalog — asset ids (incl. builder dexes), grid formatting, risk snapshots
- ``app.hl.client``      SDK order gateway (builder code on every order), cloids, response normalisation
- ``app.hl.readers``     executor port adapters (market data, positions, order status) + approval checks
- ``app.hl.fills``       fill / funding attribution to subscriptions
- ``app.hl.deposits``    USDC transfers into the treasury
- ``app.hl.typed_data``  EIP-712 payloads for user-signed actions (approveAgent, approveBuilderFee, usdSend)
- ``app.hl.fake``        in-memory exchange + info for tests
"""

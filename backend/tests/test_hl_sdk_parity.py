"""SDK parity (money-critical): our EIP-712 payloads and asset ids against the installed hyperliquid-python-sdk.

What is proven here, for the same inputs:
  * ApproveAgent / ApproveBuilderFee / UsdSend: the digest of OUR typed data equals the digest of the SDK's
    ``user_signed_payload`` for OUR action, and a signature over our typed data equals the SDK's signature
    (``sign_agent`` / ``sign_approve_builder_fee`` / ``sign_usd_transfer_action``, which force chain 0x66eee) and
    recovers, with the SDK's own recovery, to the signer — i.e. Hyperliquid would accept what our users sign.
  * The wallet chain id (e.g. Arbitrum 0xa4b1) is carried in the action as ``signatureChainId``, which is what the
    SDK/Hyperliquid use to rebuild the domain.
  * ``maxFeeRate`` strings: the exact string we sign is the string in the action (the SDK passes it through verbatim;
    the server-side parse of "0.1%" is confirmed on mainnet via ``maxBuilderFee`` — GO_LIVE B2b).
  * Builder-dex asset ids: ``MarketCatalog`` equals the SDK ``Info.coin_to_asset`` for every validator and ``xyz``
    coin in the recorded mainnet fixtures (``xyz:SILVER`` = 110026).
  * ``fixtures/hl/sdk_vectors.json`` pins the SDK digests; ``web/tests/core.test.mjs`` checks ``web/src/core/hl.ts``
    against the same file, so Python, web and SDK are tied to one set of bytes.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from hyperliquid.info import Info  # noqa: E402
from hyperliquid.utils import signing as sdk  # noqa: E402

from app.hl import typed_data as td  # noqa: E402
from app.hl.fake import load_fixture  # noqa: E402
from app.hl.markets import MarketCatalog  # noqa: E402

VECTORS_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "hl", "sdk_vectors.json")
# Deterministic TEST key — never funded, never used outside tests.
TEST_KEY = "0x" + "6a" * 32


def _sdk_digest(primary: str, types: list, action: dict) -> str:
    msg = encode_typed_data(full_message=sdk.user_signed_payload(primary, types, dict(action)))
    return Account.sign_message(msg, TEST_KEY).message_hash.hex().removeprefix("0x")


def _our_sig(typed: dict) -> dict:
    s = Account.sign_message(encode_typed_data(full_message=copy.deepcopy(typed)), TEST_KEY)
    return {"r": sdk.to_hex(s.r), "s": sdk.to_hex(s.s), "v": s.v}


def _load_vectors() -> dict:
    with open(VECTORS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _build(case: dict) -> td.UserSignedRequest:
    kind, inp = case["kind"], case["input"]
    mainnet = inp["hyperliquidChain"] == "Mainnet"
    if kind == "approveAgent":
        return td.approve_agent_request(inp["agentAddress"], nonce_ms=inp["nonce"],
                                        signature_chain_id=inp["signatureChainId"], is_mainnet=mainnet,
                                        agent_name=inp["agentName"])
    if kind == "approveBuilderFee":
        return td.approve_builder_fee_request(inp["builder"], nonce_ms=inp["nonce"],
                                              signature_chain_id=inp["signatureChainId"], is_mainnet=mainnet,
                                              max_fee_tenths_bp=inp["maxFeeTenthsBp"])
    if kind == "usdSend":
        return td.usd_send_request(inp["destination"], inp["amount"], time_ms=inp["time"],
                                   signature_chain_id=inp["signatureChainId"], is_mainnet=mainnet)
    raise AssertionError(kind)


SDK_TYPES = {
    "approveAgent": ("HyperliquidTransaction:ApproveAgent", sdk.sign_agent),
    "approveBuilderFee": ("HyperliquidTransaction:ApproveBuilderFee", sdk.sign_approve_builder_fee),
    "usdSend": ("HyperliquidTransaction:UsdSend", sdk.sign_usd_transfer_action),
}


class UserSignedParity(unittest.TestCase):
    """Every vector: ours == SDK digest == pinned digest; signatures equal; SDK recovery returns the signer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.vectors = _load_vectors()
        cls.signer = Account.from_key(TEST_KEY).address

    def test_vectors_present_for_every_kind(self) -> None:
        self.assertEqual({c["kind"] for c in self.vectors["cases"]}, set(SDK_TYPES))

    def test_each_vector(self) -> None:
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                req = _build(case)
                primary, sdk_signer = SDK_TYPES[case["kind"]]
                self.assertEqual(req.typed_data["primaryType"], primary)
                sdk_types = sdk.user_signed_payload(primary, self._sdk_field_types(sdk_signer, primary), {
                    "signatureChainId": "0x1"})["types"][primary]
                self.assertEqual(req.typed_data["types"][primary], sdk_types, "field list/order differs from SDK")
                ours = td.hash_typed_data(req.typed_data).hex()
                self.assertEqual(ours, _sdk_digest(primary, sdk_types, req.action), "digest != SDK digest")
                self.assertEqual(ours, case["digest"], "digest != pinned vector (SDK or our code changed)")
                sig = _our_sig(req.typed_data)
                recovered = sdk.recover_user_from_user_signed_action(
                    dict(req.action), sig, sdk_types, primary, case["input"]["hyperliquidChain"] == "Mainnet")
                self.assertEqual(recovered.lower(), self.signer.lower())
                if req.action["signatureChainId"] == "0x66eee":
                    # the SDK's own signer (forces 0x66eee) must produce the identical signature
                    self.assertEqual(sdk_signer(Account.from_key(TEST_KEY), dict(req.action),
                                                case["input"]["hyperliquidChain"] == "Mainnet"), sig)

    @staticmethod
    def _sdk_field_types(sdk_signer, primary: str) -> list:
        """The field list the SDK signs with, captured from its own call (no copy of SDK constants here)."""
        seen: dict = {}
        orig = sdk.sign_user_signed_action

        def spy(wallet, action, payload_types, primary_type, is_mainnet):
            seen[primary_type] = payload_types
            return {"r": "0x0", "s": "0x0", "v": 27}

        sdk.sign_user_signed_action = spy
        try:
            sdk_signer(None, {}, True)
        finally:
            sdk.sign_user_signed_action = orig
        return seen[primary]

    def test_action_carries_what_was_signed(self) -> None:
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                req = _build(case)
                for k, v in req.typed_data["message"].items():
                    self.assertEqual(req.action[k], v)
                self.assertEqual(int(req.action["signatureChainId"], 16), req.typed_data["domain"]["chainId"])

    def test_max_fee_rate_strings(self) -> None:
        # SDK passes max_fee_rate through verbatim; its builder-fee example signs "0.001%" (= 1 tenth-bp)
        self.assertEqual(td.fee_rate_percent(1), "0.001%")
        self.assertEqual(td.fee_rate_percent(100), "0.1%")
        req = td.approve_builder_fee_request("0x" + "b" * 40, nonce_ms=1_790_000_000_000, signature_chain_id="0x66eee")
        self.assertEqual(req.action["maxFeeRate"], req.typed_data["message"]["maxFeeRate"])


class _FixtureInfo(Info):
    """SDK Info whose /info calls are served from the recorded mainnet fixtures (no network)."""

    def __init__(self) -> None:  # pylint: disable=super-init-not-called
        m, _ = load_fixture("metaAndAssetCtxs")
        mx, _ = load_fixture("metaAndAssetCtxs_xyz")
        self._fx = {("perpDexs", ""): load_fixture("perpDexs"), ("meta", ""): m, ("meta", "xyz"): mx}
        Info.__init__(self, "https://api.hyperliquid.xyz", skip_ws=True, meta=m,
                      spot_meta={"tokens": [], "universe": []}, perp_dexs=["", "xyz"])

    def post(self, url_path, payload=None):  # type: ignore[override]
        payload = payload or {}
        return self._fx[(payload["type"], payload.get("dex", ""))]


class AssetIdParity(unittest.TestCase):
    def test_catalog_matches_sdk(self) -> None:
        from datetime import datetime, timezone
        m, c = load_fixture("metaAndAssetCtxs")
        mx, cx = load_fixture("metaAndAssetCtxs_xyz")
        cat = MarketCatalog.build(load_fixture("perpDexs"), {"": m, "xyz": mx}, {"": c, "xyz": cx},
                                  datetime(2026, 9, 30, 7, 10, tzinfo=timezone.utc))
        info = _FixtureInfo()
        checked = 0
        for meta in (m, mx):
            names = [a["name"] for a in meta["universe"]]
            for name in names:
                if names.count(name) > 1:
                    continue  # duplicate listings: our catalog prefers the live one (tested in test_hl_markets)
                self.assertEqual(cat.asset_id(name), info.coin_to_asset[name], name)
                checked += 1
        self.assertGreater(checked, 50)
        self.assertEqual(info.coin_to_asset["xyz:SILVER"], 110026)
        self.assertEqual(cat.asset_id("xyz:SILVER"), 110026)


if __name__ == "__main__":
    unittest.main()

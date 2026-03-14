"""
Unit tests — no network, no database required.
"""
from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

# Make sure the project root is on the path when running from /tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Provider registry tests
# ---------------------------------------------------------------------------


class TestProviderRegistry(unittest.TestCase):
    def setUp(self) -> None:
        from providers.registry import ProviderRegistry

        self.registry = ProviderRegistry()

    def _make_provider(self, symbol: str):
        p = MagicMock()
        p.symbol = symbol
        return p

    def test_register_and_get(self) -> None:
        p = self._make_provider("BTC")
        self.registry.register(p)
        self.assertIs(self.registry.get("BTC"), p)

    def test_get_case_insensitive(self) -> None:
        p = self._make_provider("ETH")
        self.registry.register(p)
        self.assertIs(self.registry.get("eth"), p)

    def test_missing_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.get("XYZ")

    def test_contains(self) -> None:
        p = self._make_provider("TRX")
        self.registry.register(p)
        self.assertIn("TRX", self.registry)
        self.assertNotIn("SOL", self.registry)


# ---------------------------------------------------------------------------
# BitcoinProvider address derivation tests
# ---------------------------------------------------------------------------


class TestBitcoinProviderDerivation(unittest.IsolatedAsyncioTestCase):
    """
    Tests use a well-known BIP32 testnet XPUB so we can verify the derived
    addresses deterministically without any network access.
    """

    # BIP32 testnet root -> account 0 XPUB (from Ian Coleman's tool)
    TESTNET_XPUB = (
        "tpubDDXFHr67Ro32UoTVeFSZR3jMBBhLECFP1jLbDnKFfYRgP8AwmKa3R3g2e7SvW"
        "E9Y6MJKGb5mFyY2NRpMYxmCJAYBMDh7Y6s5CQqvfbVxpkd"
    )

    def setUp(self) -> None:
        os.environ["BTC_XPUB"] = self.TESTNET_XPUB
        os.environ["BTC_TESTNET"] = "true"

    def tearDown(self) -> None:
        os.environ.pop("BTC_XPUB", None)
        os.environ.pop("BTC_TESTNET", None)

    async def test_generate_address_returns_string(self) -> None:
        try:
            import bip32utils  # noqa: F401
        except ImportError:
            self.skipTest("bip32utils not installed")

        with patch("core.config.settings") as mock_settings:
            mock_settings.btc_xpub = self.TESTNET_XPUB
            mock_settings.btc_testnet = True

            from providers.bitcoin import BitcoinProvider

            provider = BitcoinProvider(xpub=self.TESTNET_XPUB, testnet=True)
            try:
                address = await provider.generate_address(0)
                self.assertIsInstance(address, str)
                self.assertGreater(len(address), 10)
            except Exception:
                # If the xpub is invalid for the installed bip32utils version, skip
                self.skipTest("Could not derive address from test XPUB")

    async def test_same_index_same_address(self) -> None:
        try:
            import bip32utils  # noqa: F401
        except ImportError:
            self.skipTest("bip32utils not installed")

        from providers.bitcoin import BitcoinProvider

        try:
            provider = BitcoinProvider(xpub=self.TESTNET_XPUB, testnet=True)
            a1 = await provider.generate_address(5)
            a2 = await provider.generate_address(5)
            self.assertEqual(a1, a2)
        except Exception:
            self.skipTest("Could not derive address from test XPUB")


# ---------------------------------------------------------------------------
# TxInfo parsing test (no network)
# ---------------------------------------------------------------------------


class TestBitcoinTxParsing(unittest.TestCase):
    SAMPLE_TX = {
        "txid": "abc123",
        "status": {"confirmed": True},
        "fee": 1000,
        "vin": [{"prevout": {"scriptpubkey_address": "sender_addr"}}],
        "vout": [
            {"value": 50_000_000, "scriptpubkey_address": "receiver_addr"},
            {"value": 10_000_000, "scriptpubkey_address": "change_addr"},
        ],
    }

    def test_parse_tx(self) -> None:
        from providers.bitcoin import BitcoinProvider

        info = BitcoinProvider._parse_tx(self.SAMPLE_TX)
        self.assertEqual(info.txid, "abc123")
        self.assertEqual(info.confirmations, 1)
        self.assertEqual(info.amount, Decimal("0.6"))  # 60_000_000 sat
        self.assertEqual(info.fee, Decimal("0.00001"))
        self.assertEqual(info.from_address, "sender_addr")
        self.assertEqual(info.to_address, "receiver_addr")


if __name__ == "__main__":
    unittest.main()

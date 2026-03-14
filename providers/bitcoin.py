import asyncio
import logging
from decimal import Decimal
from functools import lru_cache

import bip32utils
import requests

from core.config import settings
from providers.base import (
    AddressGenerationError,
    BaseProvider,
    ProviderError,
    TxInfo,
    TxNotFoundError,
)
from providers.bitcoin_rpc import BitcoinRPCClient

logger = logging.getLogger(__name__)


class BitcoinProvider(BaseProvider):
    """
    HD-wallet Bitcoin provider.

    * Derives P2PKH deposit addresses from an XPUB (BIP44).
    * Queries blockchain data via Blockstream Esplora API (no API key needed).
    * Communicates with a local Bitcoin Core node via JSON-RPC for
      importaddress and raw-tx operations.
    """

    symbol = "BTC"

    def __init__(
        self,
        xpub: str | None = None,
        testnet: bool | None = None,
        rpc_client: BitcoinRPCClient | None = None,
    ) -> None:
        self._xpub: str = xpub or settings.btc_xpub
        self._testnet: bool = testnet if testnet is not None else settings.btc_testnet
        self._rpc: BitcoinRPCClient = rpc_client or BitcoinRPCClient()

        if not self._xpub:
            raise ValueError(
                "BTC_XPUB is not configured. "
                "Set the BTC_XPUB environment variable to the account-level extended public key."
            )

        self._esplora_base = (
            "https://blockstream.info/testnet/api"
            if self._testnet
            else "https://blockstream.info/api"
        )

    @lru_cache(maxsize=2)
    def _get_change_node(self, change: int = 0) -> bip32utils.BIP32Key:
        """
        Deserialise XPUB and derive the change-level node.

        Results are cached so the (relatively expensive) key parsing
        only happens once per process lifetime.
        """
        try:
            account_key = bip32utils.BIP32Key.fromExtendedKey(self._xpub, public=True)
            return account_key.ChildKey(change)
        except Exception as exc:
            raise AddressGenerationError(f"Failed to parse XPUB: {exc}") from exc

    async def generate_address(self, index: int, change: int = 0) -> str:
        """
        Derive a Bitcoin address at m/.../change/index.

        Args:
            index: BIP44 address_index (0, 1, 2, …).
            change: 0 for external (receiving) chain, 1 for internal.

        Returns:
            A legacy P2PKH Bitcoin address string.
        """
        try:
            loop = asyncio.get_running_loop()
            address = await loop.run_in_executor(
                None, self._derive_address, change, index
            )
            logger.debug("Generated BTC address index=%d: %s", index, address)
            return address
        except AddressGenerationError:
            raise
        except Exception as exc:
            raise AddressGenerationError(
                f"Address derivation failed for index {index}: {exc}"
            ) from exc

    @staticmethod
    def _b58encode(data: bytes) -> str:
        """Base58Check encode"""
        ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        n = int.from_bytes(data, "big")
        result = []
        while n:
            n, remainder = divmod(n, 58)
            result.append(ALPHABET[remainder])
        for byte in data:
            if byte == 0:
                result.append(ALPHABET[0])
            else:
                break
        return bytes(reversed(result)).decode()

    def _derive_address(self, change: int, index: int) -> str:
        import hashlib
        change_node = self._get_change_node(change)
        child = change_node.ChildKey(index)
        if self._testnet:
            pub = child.PublicKey()
            sha = hashlib.sha256(pub).digest()
            ripe = hashlib.new("ripemd160", sha).digest()
            versioned = bytes([0x6F]) + ripe
            checksum = hashlib.sha256(hashlib.sha256(versioned).digest()).digest()[:4]
            return self._b58encode(versioned + checksum)
        return child.Address()

    async def get_tx_info(self, txid: str) -> TxInfo:
        """
        Fetch transaction details from Blockstream Esplora.

        The HTTP call is offloaded to a thread so we don't block asyncio.
        """
        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(None, self._fetch_tx, txid)
        except TxNotFoundError:
            raise
        except Exception as exc:
            raise ProviderError(f"Esplora request failed: {exc}") from exc

        return self._parse_tx(raw)

    def _fetch_tx(self, txid: str) -> dict:
        url = f"{self._esplora_base}/tx/{txid}"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            raise TxNotFoundError(f"Transaction {txid} not found")
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_tx(raw: dict) -> TxInfo:
        """Map Esplora response to our normalised TxInfo."""
        txid: str = raw["txid"]

        # Confirmations: block_height is None for unconfirmed txs
        status = raw.get("status", {})
        confirmations = 0
        if status.get("confirmed"):
            confirmations = 1  # at minimum 1 confirmation when confirmed

        # Sum all outputs for a simplified "received" amount
        # In production, filter by the specific address instead.
        total_out_sat: int = sum(vout.get("value", 0) for vout in raw.get("vout", []))
        amount = Decimal(total_out_sat) / Decimal("100000000")  # sat → BTC

        fee_sat: int | None = raw.get("fee")
        fee = Decimal(fee_sat) / Decimal("100000000") if fee_sat is not None else None

        # Best-effort from_address
        from_address: str | None = None
        vin = raw.get("vin", [])
        if vin and vin[0].get("prevout"):
            from_address = vin[0]["prevout"].get("scriptpubkey_address")

        # to_address: first output's address
        to_address = ""
        for vout in raw.get("vout", []):
            addr = vout.get("scriptpubkey_address")
            if addr:
                to_address = addr
                break

        return TxInfo(
            txid=txid,
            amount=amount,
            confirmations=confirmations,
            to_address=to_address,
            from_address=from_address,
            fee=fee,
        )

    async def validate_address(self, address: str) -> bool:
        """Basic P2PKH / P2SH / bech32 sanity check (no network call)."""
        if self._testnet:
            return address.startswith(("m", "n", "2", "tb1"))
        return address.startswith(("1", "3", "bc1"))

    async def get_balance(self, address: str) -> Decimal:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, self._fetch_address, address)
        sat = data.get("chain_stats", {}).get("funded_txo_sum", 0) - data.get(
            "chain_stats", {}
        ).get("spent_txo_sum", 0)
        return Decimal(sat) / Decimal("100000000")

    def _fetch_address(self, address: str) -> dict:
        url = f"{self._esplora_base}/address/{address}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    async def import_address(
        self,
        address: str,
        label: str = "",
        rescan: bool = False,
    ) -> None:
        """
        Register *address* with the connected Bitcoin Core node so it begins
        tracking incoming UTXOs.  Called by the Watcher whenever a new invoice
        is created.

        Args:
            address: The deposit address to watch.
            label:   Optional wallet label (useful for debugging in bitcoin-cli).
            rescan:  Whether to rescan the blockchain for historical transactions.
                     Keep False in production — rescanning takes many minutes.
        """
        await self._rpc.import_address(address, label=label, rescan=rescan)
        logger.info("Node now watching address %s", address)

    async def decode_raw_transaction(self, raw_hex: str) -> dict:
        """Decode a raw hex-encoded transaction via RPC."""
        return await self._rpc.decode_raw_transaction(raw_hex)

    async def node_reachable(self) -> bool:
        """Return True if the Bitcoin Core node responds to ping."""
        return await self._rpc.ping()

    async def check_confirmations(self, txid: str) -> int:
        """
        Query the Bitcoin Core node for the current confirmation count.

        Pruned node без txindex — используем многоуровневую стратегию:
          1. gettransaction (wallet RPC) — работает если tx отправлена С нашего wallet
          2. getrawtransaction — работает пока tx в мемпуле
          3. "No such mempool transaction" — tx уже в блоке → возвращаем 1
          4. Любая другая ошибка RPC → ProviderError (retry)
        """
        from bitcoinrpc.authproxy import JSONRPCException

        # Попытка 1: gettransaction
        try:
            data = await self._rpc._call("gettransaction", txid)
            confs = int(data.get("confirmations", 0))
            logger.debug("check_confirmations(%s) via gettransaction: %d", txid, confs)
            return confs
        except JSONRPCException as exc:
            msg = str(exc)
            # Не наша wallet-транзакция — идём дальше
            if "Invalid or non-wallet transaction" in msg or "not found" in msg.lower():
                pass
            else:
                raise ProviderError(f"check_confirmations({txid}) gettransaction failed: {exc}") from exc
        except Exception:
            pass

        # Попытка 2: getrawtransaction
        try:
            data = await self._rpc._call("getrawtransaction", txid, True)
            confs = int(data.get("confirmations", 0))
            logger.debug("check_confirmations(%s) via getrawtransaction: %d", txid, confs)
            return confs
        except JSONRPCException as exc:
            msg = str(exc)
            # Транзакция вышла из мемпула → она в блоке
            if "No such mempool transaction" in msg or "No such mempool or blockchain transaction" in msg:
                logger.info(
                    "check_confirmations(%s): not in mempool → confirmed on pruned node, returning 1",
                    txid,
                )
                return 1
            raise ProviderError(f"check_confirmations({txid}) failed: {exc}") from exc
        except Exception as exc:
            raise ProviderError(f"check_confirmations({txid}) unexpected: {exc}") from exc

    # Signing & payout transaction construction

    def _get_xpriv_key(self) -> "bip32utils.BIP32Key":
        """
        Load and cache the account-level XPRIV key.

        The key is NEVER logged, stored in DB, or exposed via API.
        It lives only in memory for the duration of the signing call.
        """
        xpriv = settings.btc_xpriv
        if not xpriv:
            raise ProviderError(
                "BTC_XPRIV is not set. Cannot sign payout transactions."
            )
        try:
            return bip32utils.BIP32Key.fromExtendedKey(xpriv, public=False)
        except Exception as exc:
            raise ProviderError(f"Failed to parse XPRIV: {exc}") from exc

    def _derive_private_key_bytes(self, change: int, index: int) -> bytes:
        """
        Derive the raw 32-byte private key for path xpriv→change→index.

        Called once per UTXO during signing, then the intermediate nodes
        are discarded.
        """
        account_key = self._get_xpriv_key()
        change_node = account_key.ChildKey(change)
        child = change_node.ChildKey(index)
        return child.PrivateKey()

    async def create_payout_transaction(
        self,
        utxos: list[dict],
        destination_address: str,
        amount_sat: int,
        change_address: str,
        fee_rate_sat_vbyte: int | None = None,
    ) -> dict:
        """
        Build, sign and return a raw Bitcoin transaction for a payout.

        Args:
            utxos: List of dicts with keys:
                     txid (str), vout (int), amount_sat (int),
                     derivation_index (int), change (int, default 0)
            destination_address: Merchant's external withdrawal address.
            amount_sat: Total satoshis the merchant requested.
            change_address: Internal address to receive leftover satoshis.
            fee_rate_sat_vbyte: Override the default fee rate.

        Returns:
            {
                "raw_hex": str,       # signed tx ready for sendrawtransaction
                "txid": str,          # predicted txid
                "fee_sat": int,       # miner fee deducted
                "amount_out_sat": int,# what reaches destination
                "change_sat": int,    # what goes to change_address
            }

        Raises:
            ProviderError: On key derivation, signing, or balance errors.
            InsufficientFundsError: If UTXO total < amount + minimum fee.
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            self._build_and_sign,
            utxos,
            destination_address,
            amount_sat,
            change_address,
            fee_rate_sat_vbyte or settings.payout_fee_rate_sat_vbyte,
        )
        return result

    def _build_and_sign(
        self,
        utxos: list[dict],
        destination_address: str,
        amount_sat: int,
        change_address: str,
        fee_rate_sat_vbyte: int,
    ) -> dict:
        """
        Synchronous signing worker — runs in a thread executor.

        Uses python-bitcoinlib for proper P2PKH script construction and
        DER-encoded ECDSA signatures.
        """
        import bitcoin
        import bitcoin.core
        import bitcoin.core.script
        import bitcoin.core.serialize
        import bitcoin.wallet
        import hashlib

        if self._testnet:
            bitcoin.SelectParams("testnet")
        else:
            bitcoin.SelectParams("mainnet")

        # ── Collect inputs ────────────────────────────────────────────
        total_input_sat: int = sum(u["amount_sat"] for u in utxos)

        # P2PKH tx size: 10 base + 148*n_in + 34*n_out
        # We have 2 outputs (destination + change), unless change is dust
        estimated_size = 10 + 148 * len(utxos) + 34 * 2
        fee_sat = estimated_size * fee_rate_sat_vbyte

        change_sat = total_input_sat - amount_sat - fee_sat

        dust = settings.payout_dust_threshold_sat
        if change_sat < 0:
            raise InsufficientFundsError(
                f"UTXO total ({total_input_sat} sat) < amount ({amount_sat}) + fee ({fee_sat})"
            )

        # If change is dust, add it to the fee instead of creating a tiny output
        n_outputs = 2
        if change_sat < dust:
            fee_sat += change_sat
            change_sat = 0
            n_outputs = 1
            # Recalculate size without change output
            estimated_size = 10 + 148 * len(utxos) + 34 * 1
            fee_sat = max(fee_sat, estimated_size * fee_rate_sat_vbyte)

        amount_out_sat = amount_sat  # destination gets exactly what was requested

        # ── Verify balance equation ───────────────────────────────────
        expected_total = amount_out_sat + fee_sat + change_sat
        if expected_total != total_input_sat:
            raise ProviderError(
                f"Balance mismatch: inputs={total_input_sat} != "
                f"out={amount_out_sat} + fee={fee_sat} + change={change_sat} "
                f"= {expected_total}"
            )

        # ── Build inputs ──────────────────────────────────────────────
        vin = []
        for u in utxos:
            txid_bytes = bytes.fromhex(u["txid"])[::-1]   # little-endian
            outpoint = bitcoin.core.COutPoint(txid_bytes, u["vout"])
            vin.append(bitcoin.core.CTxIn(outpoint))

        # ── Build outputs ─────────────────────────────────────────────
        def p2pkh_script(address: str) -> bitcoin.core.script.CScript:
            addr_obj = bitcoin.wallet.CBitcoinAddress(address)
            return addr_obj.to_scriptPubKey()

        vout = [
            bitcoin.core.CTxOut(
                amount_out_sat,
                p2pkh_script(destination_address),
            )
        ]
        if change_sat > 0:
            vout.append(
                bitcoin.core.CTxOut(change_sat, p2pkh_script(change_address))
            )

        # ── Unsigned transaction ──────────────────────────────────────
        tx = bitcoin.core.CTransaction(vin, vout, nVersion=1)

        # ── Sign each input ───────────────────────────────────────────
        signed_inputs = []
        for i, u in enumerate(utxos):
            change_branch = u.get("change", 0)
            index = u["derivation_index"]
            priv_bytes = self._derive_private_key_bytes(change_branch, index)

            # Create signing key
            priv_key = bitcoin.wallet.CBitcoinSecret.from_secret_bytes(
                priv_bytes, compressed=True
            )
            pub_key = priv_key.pub

            # scriptPubKey of the UTXO we're spending
            utxo_address_obj = bitcoin.wallet.CBitcoinAddress(
                self._derive_address(change_branch, index)
            )
            script_pubkey = utxo_address_obj.to_scriptPubKey()

            # SIGHASH_ALL signature
            sig_hash = bitcoin.core.script.SignatureHash(
                script_pubkey, tx, i, bitcoin.core.script.SIGHASH_ALL
            )
            sig = priv_key.sign(sig_hash) + bytes([bitcoin.core.script.SIGHASH_ALL])

            # Build scriptSig: <sig> <pubkey>
            script_sig = bitcoin.core.script.CScript([sig, pub_key])
            signed_inputs.append(bitcoin.core.CTxIn(tx.vin[i].prevout, script_sig))

        # ── Assemble signed transaction ───────────────────────────────
        signed_tx = bitcoin.core.CTransaction(signed_inputs, vout, nVersion=1)
        raw_hex = signed_tx.serialize().hex()

        # Compute txid (double-SHA256 of serialised tx, reversed)
        raw_bytes = signed_tx.serialize()
        txid = hashlib.sha256(hashlib.sha256(raw_bytes).digest()).digest()[::-1].hex()

        logger.info(
            "Built payout tx txid=%s inputs=%d fee=%d sat change=%d sat",
            txid, len(utxos), fee_sat, change_sat,
        )

        return {
            "raw_hex": raw_hex,
            "txid": txid,
            "fee_sat": fee_sat,
            "amount_out_sat": amount_out_sat,
            "change_sat": change_sat,
        }

    async def broadcast_transaction(self, raw_hex: str) -> str:
        """Broadcast a signed raw transaction via RPC. Returns txid."""
        return await self._rpc.send_raw_transaction(raw_hex)

    async def get_fee_rate(self) -> int:
        """Return current sat/vByte fee rate from node estimatesmartfee."""
        return await self._rpc.estimate_smart_fee()


# ---------------------------------------------------------------------------
# Payout-specific exceptions
# ---------------------------------------------------------------------------

class InsufficientFundsError(ProviderError):
    """Raised when selected UTXOs cannot cover the requested payout + fee."""
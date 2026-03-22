"""
Thin async wrapper around python-bitcoinrpc (AuthServiceProxy).

python-bitcoinrpc is synchronous; we run every call in a thread-pool
executor so the event loop is never blocked.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException

from core.config import settings

logger = logging.getLogger(__name__)


class BitcoinRPCClient:
    """
    Async Bitcoin Core RPC client.

    The underlying AuthServiceProxy is NOT thread-safe, so we instantiate
    a new proxy per call (connection is cheap — HTTP keep-alive handles reuse).
    """

    def __init__(
        self,
        url: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self._url = url or settings.bitcoin_rpc_url
        self._user = user or settings.bitcoin_rpc_user
        self._password = password or settings.bitcoin_rpc_password

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _make_proxy(self) -> AuthServiceProxy:
        rpc_url = self._url
        # Inject credentials into URL: http://user:pass@host:port
        if "://" in rpc_url and "@" not in rpc_url:
            scheme, rest = rpc_url.split("://", 1)
            rpc_url = f"{scheme}://{self._user}:{self._password}@{rest}"
        return AuthServiceProxy(rpc_url)

    async def _call(self, method: str, *args: Any) -> Any:
        """Execute an RPC method in a thread-pool executor."""
        loop = asyncio.get_running_loop()

        def _sync() -> Any:
            proxy = self._make_proxy()
            return getattr(proxy, method)(*args)

        try:
            result = await loop.run_in_executor(None, _sync)
            return result
        except JSONRPCException as exc:
            logger.error("RPC %s failed: %s", method, exc)
            raise
        except Exception as exc:
            logger.exception("RPC %s unexpected error: %s", method, exc)
            raise

    # -----------------------------------------------------------------------
    # Public RPC methods
    # -----------------------------------------------------------------------

    async def import_address(
        self,
        address: str,
        label: str = "",
        rescan: bool = False,
        p2sh: bool = False,
    ) -> None:
        """
        Call importaddress so the node watches the address for incoming UTXOs.

        rescan=False by default — rescanning the entire chain is very slow.
        Set rescan=True only when you need historical data.
        """
        await self._call("importaddress", address, label, rescan, p2sh)
        logger.info("importaddress OK: %s (label=%r, rescan=%s)", address, label, rescan)

    async def get_raw_transaction(self, txid: str, verbose: bool = True) -> dict:
        """Fetch a raw transaction; requires txindex=1 in bitcoin.conf."""
        return await self._call("getrawtransaction", txid, verbose)

    async def decode_raw_transaction(self, raw_hex: str) -> dict:
        """Decode a raw hex-encoded transaction."""
        return await self._call("decoderawtransaction", raw_hex)

    async def get_block_count(self) -> int:
        return await self._call("getblockcount")

    async def get_connection_count(self) -> int:
        return await self._call("getconnectioncount")

    async def get_block_hash(self, height: int) -> str:
        return await self._call("getblockhash", height)

    async def get_blockchain_info(self) -> dict:
        return await self._call("getblockchaininfo")

    async def get_network_info(self) -> dict:
        return await self._call("getnetworkinfo")

    async def get_uptime(self) -> int:
        return await self._call("uptime")

    async def get_transaction(self, txid: str) -> dict:
        """
        High-level gettransaction — only works for wallet-tracked transactions.
        For arbitrary txns use get_raw_transaction.
        """
        return await self._call("gettransaction", txid)

    async def ping(self) -> bool:
        """Return True if the node is reachable."""
        try:
            await self._call("getblockcount")
            return True
        except Exception:
            return False

    async def get_confirmations(self, txid: str) -> int:
        """
        Return the current confirmation count for *txid*.

        Strategy (pruned node без txindex):
          1. gettransaction — работает для транзакций в wallet (descriptor wallet)
          2. Если нода возвращает "Invalid or non-wallet transaction" — транзакция
             не в wallet. В этом случае пробуем getrawtransaction (работает пока
             tx в мемпуле). Если и она недоступна — возвращаем 0 (не падаем).
        """
        from bitcoinrpc.authproxy import JSONRPCException
        # Попытка 1: gettransaction (wallet method, не требует txindex)
        try:
            data = await self._call("gettransaction", txid)
            return int(data.get("confirmations", 0))
        except JSONRPCException as exc:
            msg = str(exc)
            if "Invalid or non-wallet transaction" not in msg and "not found" not in msg.lower():
                logger.warning("get_confirmations(%s) gettransaction failed: %s", txid, exc)
                raise

        # Попытка 2: getrawtransaction (только мемпул — без txindex)
        try:
            data = await self._call("getrawtransaction", txid, True)
            return int(data.get("confirmations", 0))
        except JSONRPCException as exc:
            msg = str(exc)
            # "No such mempool transaction" = уже в блоке но нет txindex
            # Это нормально для pruned ноды — возвращаем 1 (считаем подтверждённой)
            if "No such mempool transaction" in msg or "No such mempool or blockchain transaction" in msg:
                logger.info("get_confirmations(%s): not in mempool → assumed confirmed (pruned node)", txid)
                return 1
            logger.warning("get_confirmations(%s) getrawtransaction failed: %s", txid, exc)
            raise
        except Exception as exc:
            logger.warning("get_confirmations(%s) unexpected: %s", txid, exc)
            raise

    async def send_raw_transaction(self, raw_hex: str) -> str:
        """
        Broadcast a signed raw transaction.

        Returns:
            The txid of the broadcast transaction.
        Raises:
            JSONRPCException: e.g. if the tx is invalid or already spent.
        """
        txid: str = await self._call("sendrawtransaction", raw_hex)
        logger.info("sendrawtransaction → txid=%s", txid)
        return txid

    async def estimate_smart_fee(self, conf_target: int = 3) -> int:
        """
        Call estimatesmartfee and return the fee rate in sat/vByte.

        Falls back to the configured default if the node returns -1
        (not enough data — common on testnet).
        """
        from core.config import settings
        try:
            result = await self._call("estimatesmartfee", conf_target)
            feerate_btc_per_kb = result.get("feerate", -1)
            if feerate_btc_per_kb > 0:
                sat_per_vbyte = int(feerate_btc_per_kb * 100_000)   # BTC/kB → sat/vB
                return max(1, sat_per_vbyte)
        except Exception as exc:
            logger.warning("estimatesmartfee failed: %s — using default", exc)
        return settings.payout_fee_rate_sat_vbyte
"""
Bitcoin ZMQ Watcher Service
============================

Subscribes to Bitcoin Core's ZMQ publisher on two topics:

  * ``rawtx``    — fires for every new unconfirmed transaction
  * ``rawblock`` — fires for every new block (used to update confirmations)

For each raw transaction the watcher:
  1. Decodes the binary payload with the pure-Python tx decoder.
  2. Extracts all output addresses.
  3. Checks them against the in-memory AddressCache (O(1) lookup).
  4. On a match, pushes a TxMatch JSON object to the Redis ``tx_processing`` queue.

Block handler:
  On every new block the address cache is refreshed so expired / paid invoices
  drop out of matching automatically.

importaddress integration:
  On startup the watcher calls ``bitcoind.importaddress`` for every active
  invoice address so the node starts watching UTXOs even if the watcher
  was restarted mid-session.  New invoices get imported via the same method
  triggered from the API (InvoiceService → BitcoinProvider.import_address).

Run:
    python -m services.watcher.zmq_watcher
"""
from __future__ import annotations

import sys

# Windows: ProactorEventLoop не поддерживает add_reader() нужный ZMQ
if sys.platform == "win32":
    import asyncio as _asyncio
    _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())

import asyncio
import hashlib
import json
import logging
import struct
from decimal import Decimal

import redis.asyncio as aioredis
import zmq
import zmq.asyncio

from core.config import settings
from core.database import AsyncSessionLocal
from core.models import Invoice, InvoiceStatus
from providers.bitcoin import BitcoinProvider
from providers.registry import bootstrap_providers, registry
from services.watcher.address_cache import AddressCache, CachedInvoice
from services.watcher.tx_decoder import RawTx, TxOutput, decode_raw_tx
from services.watcher.tx_queue import TxMatch, TxQueue

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

ZMQ_TOPIC_RAW_TX = b"rawtx"
ZMQ_TOPIC_RAW_BLOCK = b"rawblock"

# ─────────────────────────────────────────────────────────────────────────────
# Watcher
# ─────────────────────────────────────────────────────────────────────────────


class ZmqWatcher:
    """
    Async ZMQ-based Bitcoin transaction watcher.

    Architecture
    ------------
    Two separate ZMQ SUB sockets are maintained:
      - *tx_socket*    → ``BITCOIN_ZMQ_RAW_TX``
      - *block_socket* → ``BITCOIN_ZMQ_RAW_BLOCK``

    Both are polled concurrently via asyncio tasks.  A shared
    :class:`AddressCache` provides O(1) address lookup without touching
    the database on every transaction.

    Matched transactions are forwarded to the :class:`TxQueue` (Redis LPUSH).
    """

    def __init__(self) -> None:
        self._ctx = zmq.asyncio.Context.instance()
        self._cache = AddressCache(refresh_interval=60.0)
        self._queue = TxQueue()
        self._running = False

    # ─────────────────────────────────────────────────────────────────────
    # Public entry-point
    # ─────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Bootstrap everything and run until cancelled."""
        bootstrap_providers()

        logger.info("Starting ZMQ Watcher …")
        logger.info("  rawtx   → %s", settings.bitcoin_zmq_raw_tx)
        logger.info("  rawblock→ %s", settings.bitcoin_zmq_raw_block)

        await self._queue.connect()
        await self._cache.start()
        await self._import_existing_addresses()

        self._running = True
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._listen_tx(), name="zmq-rawtx")
                tg.create_task(self._listen_block(), name="zmq-rawblock")
                tg.create_task(self._listen_invoice_events(), name="redis-invoice-events")
        except* asyncio.CancelledError:
            pass
        finally:
            self._running = False
            await self._cache.stop()
            await self._queue.close()
            self._ctx.term()
            logger.info("ZMQ Watcher stopped.")

    # ─────────────────────────────────────────────────────────────────────
    # ZMQ listeners
    # ─────────────────────────────────────────────────────────────────────

    async def _listen_tx(self) -> None:
        """Subscribe to rawtx and process each transaction."""
        while True:
            sock = self._make_socket(settings.bitcoin_zmq_raw_tx, ZMQ_TOPIC_RAW_TX)
            try:
                logger.info("rawtx socket connected")
                while True:
                    # ZMQ multipart: [topic, payload, sequence_number]
                    parts = await sock.recv_multipart()
                    if len(parts) < 2:
                        continue
                    topic, payload = parts[0], parts[1]
                    if topic == ZMQ_TOPIC_RAW_TX:
                        await self._handle_raw_tx(payload)
            except zmq.ZMQError as exc:
                logger.warning("rawtx ZMQ error: %s — reconnecting in %.1fs", exc, settings.zmq_reconnect_delay)
                await asyncio.sleep(settings.zmq_reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("rawtx unexpected error: %s", exc)
                await asyncio.sleep(settings.zmq_reconnect_delay)
            finally:
                sock.close()

    async def _listen_block(self) -> None:
        """Subscribe to rawblock and refresh the address cache on each block."""
        while True:
            sock = self._make_socket(settings.bitcoin_zmq_raw_block, ZMQ_TOPIC_RAW_BLOCK)
            try:
                logger.info("rawblock socket connected")
                while True:
                    parts = await sock.recv_multipart()
                    if len(parts) < 2:
                        continue
                    topic = parts[0]
                    if topic == ZMQ_TOPIC_RAW_BLOCK:
                        await self._handle_raw_block(parts[1])
            except zmq.ZMQError as exc:
                logger.warning("rawblock ZMQ error: %s — reconnecting in %.1fs", exc, settings.zmq_reconnect_delay)
                await asyncio.sleep(settings.zmq_reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("rawblock unexpected error: %s", exc)
                await asyncio.sleep(settings.zmq_reconnect_delay)
            finally:
                sock.close()

    # ─────────────────────────────────────────────────────────────────────
    # Handlers
    # ─────────────────────────────────────────────────────────────────────

    async def _handle_raw_tx(self, payload: bytes) -> None:
        """
        Decode a raw transaction and match its outputs against active invoices.
        """
        try:
            tx = decode_raw_tx(payload, testnet=settings.btc_testnet)
        except Exception as exc:
            logger.warning("Failed to decode rawtx (%d bytes): %s", len(payload), exc)
            return

        # Собираем все адреса для отладки
        addresses = [o.address for o in tx.outputs if o.address]
        logger.info("rawtx %s — %d outputs, addresses: %s", tx.txid, len(tx.outputs), addresses)

        for output in tx.outputs:
            address = output.address
            if address is None:
                continue  # OP_RETURN, bare multisig, etc.

            cached = self._cache.get(address)
            if cached is None:
                continue  # not one of our invoice addresses

            logger.info(
                "Match! txid=%s address=%s value=%d sat invoice=%s",
                tx.txid,
                address,
                output.value_sat,
                cached.invoice_id,
            )

            match = TxMatch(
                txid=tx.txid,
                invoice_id=cached.invoice_id,
                address=address,
                amount_sat=output.value_sat,
                coin_symbol=cached.coin_symbol,
                merchant_id=cached.merchant_id,
                confirmations=0,
            )
            await self._queue.push(match)

    async def _handle_raw_block(self, payload: bytes) -> None:
        """
        On each new block:
          - Extract the block hash (first 80 bytes are the header).
          - Log it.
          - Refresh the address cache (paid invoices drop out).
        """
        if len(payload) < 80:
            return

        header = payload[:80]
        block_hash = hashlib.sha256(hashlib.sha256(header).digest()).digest()[::-1].hex()
        logger.info("New block: %s — refreshing address cache", block_hash)

        await self._cache.refresh()

    # ─────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────

    def _make_socket(self, endpoint: str, topic: bytes) -> zmq.asyncio.Socket:
        sock = self._ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.RCVTIMEO, -1)          # block forever (async recv)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RECONNECT_IVL, 1000)   # 1 s base reconnect
        sock.setsockopt(zmq.RECONNECT_IVL_MAX, 30_000)
        sock.setsockopt_string(zmq.SUBSCRIBE, "")
        sock.connect(endpoint)
        return sock

    async def _import_existing_addresses(self) -> None:
        """
        On startup, call importaddress for every active invoice so the node
        watches them even if it missed activity while the watcher was down.
        """
        if "BTC" not in registry:
            logger.warning("BitcoinProvider not registered — skipping importaddress bootstrap")
            return

        provider: BitcoinProvider = registry.get("BTC")  # type: ignore[assignment]

        reachable = await provider.ping()
        if not reachable:
            logger.warning(
                "Bitcoin node not reachable at %s — skipping importaddress bootstrap",
                settings.bitcoin_rpc_url,
            )
            return

        async with AsyncSessionLocal() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(Invoice).where(
                    Invoice.status.in_([InvoiceStatus.NEW, InvoiceStatus.PARTIAL])
                )
            )
            invoices = result.scalars().all()

        if not invoices:
            logger.info("No active invoices to import")
            return

        logger.info("Importing %d active invoice addresses into the node …", len(invoices))
        for inv in invoices:
            try:
                await provider.import_address(
                    address=inv.address,
                    label=f"invoice:{inv.id}",
                    rescan=False,
                )
            except Exception as exc:
                logger.warning("importaddress failed for %s: %s", inv.address, exc)

        logger.info("importaddress bootstrap complete")


    # ─────────────────────────────────────────────────────────────────────────────
    # Redis Events
    # ─────────────────────────────────────────────────────────────────────────────

    async def _listen_invoice_events(self) -> None:
        """Subscribes to Redis for new invoices and updates the local cache immediately."""
        logger.info("Subscribing to Redis channel: %s", settings.redis_channel_invoice_created)

        try:
            r = await aioredis.from_url(settings.redis_url, decode_responses=True)
            pubsub = r.pubsub()
            await pubsub.subscribe(settings.redis_channel_invoice_created)

            async for message in pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue

                try:
                    data = json.loads(message["data"])
                    invoice = CachedInvoice(
                        invoice_id=data["invoice_id"],
                        address=data["address"],
                        amount_expected=Decimal(str(data["amount_expected"])),
                        coin_symbol=data["coin_symbol"],
                        merchant_id=data["merchant_id"],
                    )
                    await self._cache.add(invoice)
                    logger.info("AddressCache: Real-time update for %s", invoice.address)
                except Exception as e:
                    logger.error("Failed to process invoice event: %s", e)

        except Exception as exc:
            logger.error("Redis Pub/Sub error: %s", exc)
        finally:
            logger.info("Unsubscribing from Redis invoice events.")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    watcher = ZmqWatcher()
    try:
        await watcher.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
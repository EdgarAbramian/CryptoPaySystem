"""
Redis queue publisher used by the ZMQ Watcher.

Pushes detected transaction matches onto the `tx_processing` list
so the Ledger service can pick them up and update invoice statuses.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from decimal import Decimal

import redis.asyncio as aioredis

from core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class TxMatch:
    """Payload pushed to the Redis queue when a tx output matches an invoice."""

    txid: str
    invoice_id: str
    address: str
    amount_sat: int          # raw satoshis — no floating-point issues
    coin_symbol: str
    merchant_id: str
    confirmations: int = 0   # 0 for unconfirmed; updated by block handler

    @property
    def amount_btc(self) -> Decimal:
        return Decimal(self.amount_sat) / Decimal("100_000_000")

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "TxMatch":
        return cls(**json.loads(raw))


class TxQueue:
    """Async Redis LPUSH publisher."""

    def __init__(self, redis_url: str | None = None, queue_name: str | None = None) -> None:
        self._url = redis_url or settings.redis_url
        self._queue = queue_name or settings.redis_queue_tx_processing
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._redis = aioredis.from_url(
            self._url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        # Verify connection
        await self._redis.ping()
        logger.info("TxQueue connected to Redis (queue=%r)", self._queue)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def push(self, match: TxMatch) -> None:
        """Push a TxMatch to the left of the Redis list (LPUSH)."""
        if self._redis is None:
            raise RuntimeError("TxQueue.connect() must be called before push()")
        payload = match.to_json()
        await self._redis.lpush(self._queue, payload)
        logger.debug(
            "TxQueue: pushed txid=%s invoice=%s amount=%s sat",
            match.txid,
            match.invoice_id,
            match.amount_sat,
        )

    async def __aenter__(self) -> "TxQueue":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

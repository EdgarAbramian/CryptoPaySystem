"""
Redis queue consumer + delayed-retry publisher for the Ledger service.

Flow
----
BRPOP  tx_processing  →  process  →  OK
                                  ↘  not enough confirmations
                                      ZADD tx_retry  score=now()+delay  payload
                                          ↑
                               background loop pops due items back to tx_processing
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal

import redis.asyncio as aioredis

from core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LedgerJob:
    """
    Everything the Ledger needs to process one transaction.

    Extends TxMatch with retry tracking — stored as JSON in Redis.
    """
    txid: str
    invoice_id: str
    address: str
    amount_sat: int
    coin_symbol: str
    merchant_id: str
    confirmations: int = 0
    retry_count: int = 0

    @property
    def amount_coin(self) -> Decimal:
        """Convert satoshis to BTC (works for any 8-decimal coin)."""
        return Decimal(self.amount_sat) / Decimal("100_000_000")

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "LedgerJob":
        d = json.loads(raw)
        # Backwards-compat: TxMatch payloads from Watcher have no retry_count
        d.setdefault("retry_count", 0)
        return cls(**d)


class LedgerQueue:
    """
    Wraps two Redis structures:

    * ``tx_processing`` — LIST (LPUSH / BRPOP) — hot queue
    * ``tx_retry``      — SORTED SET scored by next-check timestamp
    """

    def __init__(self) -> None:
        self._url = settings.redis_url
        self._hot_queue = settings.redis_queue_tx_processing
        self._retry_zset = settings.redis_queue_tx_retry
        self._retry_delay = settings.ledger_retry_delay
        self._r: aioredis.Redis | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._r = aioredis.from_url(
            self._url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
        await self._r.ping()
        logger.info(
            "LedgerQueue connected (hot=%r retry=%r)",
            self._hot_queue, self._retry_zset,
        )

    async def close(self) -> None:
        if self._r:
            await self._r.aclose()

    # ── Consumer ──────────────────────────────────────────────────────────

    async def pop(self, timeout: float = 2.0) -> LedgerJob | None:
        """
        Blocking pop from the hot queue.

        Returns None on timeout (allows the caller's loop to run housekeeping).
        """
        assert self._r, "call connect() first"
        result = await self._r.brpop(self._hot_queue, timeout=timeout)
        if result is None:
            return None
        _, payload = result
        return LedgerJob.from_json(payload)

    # ── Publisher ─────────────────────────────────────────────────────────

    async def push_hot(self, job: LedgerJob) -> None:
        """Push directly to the hot queue (immediate processing)."""
        assert self._r
        await self._r.lpush(self._hot_queue, job.to_json())

    async def push_retry(self, job: LedgerJob) -> None:
        """
        Schedule a job for re-processing after ``retry_delay`` seconds.

        Uses a Redis Sorted Set (ZADD) scored by the Unix timestamp at
        which the job should be re-queued.  A background promoter task
        moves due items back to the hot queue.
        """
        assert self._r
        job.retry_count += 1
        score = time.time() + self._retry_delay
        await self._r.zadd(self._retry_zset, {job.to_json(): score})
        logger.debug(
            "Scheduled retry #%d for txid=%s in %ds",
            job.retry_count, job.txid, self._retry_delay,
        )

    # ── Retry promoter ────────────────────────────────────────────────────

    async def promote_due_retries(self) -> int:
        """
        Move all jobs whose retry timestamp has passed from the sorted set
        back into the hot queue.

        Returns the number of jobs promoted.
        """
        assert self._r
        now = time.time()
        # Atomically fetch and remove all members with score <= now
        due: list[str] = await self._r.zrangebyscore(
            self._retry_zset, "-inf", now
        )
        if not due:
            return 0

        pipe = self._r.pipeline(transaction=True)
        for payload in due:
            pipe.lpush(self._hot_queue, payload)
            pipe.zrem(self._retry_zset, payload)
        await pipe.execute()

        logger.info("Promoted %d retry jobs to hot queue", len(due))
        return len(due)

    async def retry_queue_size(self) -> int:
        assert self._r
        return await self._r.zcard(self._retry_zset)

    async def hot_queue_size(self) -> int:
        assert self._r
        return await self._r.llen(self._hot_queue)

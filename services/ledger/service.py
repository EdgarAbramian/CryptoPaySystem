"""
Ledger Service — entry point.

Architecture
────────────

                ┌──────────────────────────────────┐
                │           LedgerService           │
                │                                  │
  Redis         │  ┌──────────┐   asyncio.Semaphore│
  tx_processing ├─►│ consumer │──►  TxProcessor    │
  (hot queue)   │  │  loop   │   (max N concurrent)│
                │  └──────────┘                    │
                │                                  │
  Redis         │  ┌──────────────┐                │
  tx_retry      │  │retry promoter│ (every 10 s)   │
  (sorted set)  │  └──────────────┘                │
                │                                  │
                │  ┌──────────────┐                │
                │  │ health stats │ (logged 60 s)  │
                │  └──────────────┘                │
                └──────────────────────────────────┘

Run:
    python -m services.ledger
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from core.config import settings
from providers.registry import bootstrap_providers
from services.ledger.processor import TxProcessor
from services.ledger.queue import LedgerJob, LedgerQueue

logger = logging.getLogger(__name__)

_RETRY_PROMOTER_INTERVAL = 10   # seconds between retry-queue sweeps
_STATS_INTERVAL = 60            # seconds between queue-size log lines


class LedgerService:
    """
    Async consumer loop that drives the entire Ledger subsystem.

    Concurrency
    -----------
    Up to ``ledger_concurrency`` jobs are processed simultaneously via
    an asyncio.Semaphore.  Each job runs in its own coroutine so one slow
    RPC call never blocks the rest.
    """

    def __init__(self) -> None:
        self._queue = LedgerQueue()
        self._processor = TxProcessor(self._queue)
        self._sem = asyncio.Semaphore(settings.ledger_concurrency)
        self._running = False

    # ─────────────────────────────────────────────────────────────────────
    # Public
    # ─────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start all background tasks and block until shutdown."""
        bootstrap_providers()
        await self._queue.connect()

        self._running = True
        logger.info(
            "Ledger started | concurrency=%d required_conf=%d retry_delay=%ds max_retries=%d",
            settings.ledger_concurrency,
            settings.ledger_required_confirmations,
            settings.ledger_retry_delay,
            settings.ledger_max_retries,
        )

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._consume_loop(),   name="ledger-consumer")
                tg.create_task(self._promoter_loop(),  name="ledger-retry-promoter")
                tg.create_task(self._stats_loop(),     name="ledger-stats")
        except* asyncio.CancelledError:
            pass
        finally:
            self._running = False
            await self._queue.close()
            logger.info("Ledger stopped.")

    def stop(self) -> None:
        self._running = False

    # ─────────────────────────────────────────────────────────────────────
    # Consumer loop
    # ─────────────────────────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """
        Continuously pop jobs from the hot queue and dispatch them.

        BRPOP blocks for up to ``ledger_consumer_timeout`` seconds, then
        loops — this lets the task respond to cancellation promptly.
        """
        timeout = settings.ledger_consumer_timeout
        while self._running:
            try:
                job = await self._queue.pop(timeout=timeout)
            except Exception as exc:
                logger.warning("Queue pop error: %s — retrying in 2s", exc)
                await asyncio.sleep(2)
                continue

            if job is None:
                continue  # BRPOP timed out — loop back

            # Dispatch under semaphore so we don't spawn unlimited coroutines
            asyncio.create_task(self._dispatch(job), name=f"ledger-job-{job.txid[:12]}")

    async def _dispatch(self, job: LedgerJob) -> None:
        async with self._sem:
            try:
                await self._processor.process(job)
            except Exception as exc:
                # processor.process() should never raise, but be safe
                logger.exception("Unhandled error in processor for txid=%s: %s", job.txid, exc)

    # ─────────────────────────────────────────────────────────────────────
    # Retry promoter loop
    # ─────────────────────────────────────────────────────────────────────

    async def _promoter_loop(self) -> None:
        """
        Periodically move due retry jobs back into the hot queue.

        The sorted set stores jobs scored by their next-check Unix timestamp.
        """
        while self._running:
            try:
                promoted = await self._queue.promote_due_retries()
                if promoted:
                    logger.debug("Promoter: moved %d jobs to hot queue", promoted)
            except Exception as exc:
                logger.warning("Promoter error: %s", exc)
            await asyncio.sleep(_RETRY_PROMOTER_INTERVAL)

    # ─────────────────────────────────────────────────────────────────────
    # Stats loop
    # ─────────────────────────────────────────────────────────────────────

    async def _stats_loop(self) -> None:
        while self._running:
            await asyncio.sleep(_STATS_INTERVAL)
            try:
                hot = await self._queue.hot_queue_size()
                retry = await self._queue.retry_queue_size()
                logger.info("Queue stats — hot=%d retry=%d", hot, retry)
            except Exception as exc:
                logger.warning("Stats error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    service = LedgerService()

    # add_signal_handler не поддерживается на Windows — используем try/except
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, service.stop)

    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
"""
In-memory address cache for the ZMQ watcher.

Keeps a set of all active invoice deposit addresses so that transaction
matching is O(1) without hitting the database on every incoming raw-tx.

The cache is refreshed periodically and also updated immediately when a
new invoice is created via the API (see the notification flow).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from core.database import AsyncSessionLocal
from core.models import Invoice, InvoiceStatus

logger = logging.getLogger(__name__)


@dataclass
class CachedInvoice:
    invoice_id: str
    address: str
    amount_expected: Decimal
    coin_symbol: str
    merchant_id: str


class AddressCache:
    """
    Thread-safe (asyncio-safe) in-memory cache of active invoice addresses.
    """

    def __init__(self, refresh_interval: float = 60.0) -> None:
        self._refresh_interval = refresh_interval
        # address -> CachedInvoice
        self._cache: dict[str, CachedInvoice] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def start(self) -> None:
        """Load initial data and start background refresh loop."""
        await self.refresh()
        self._task = asyncio.create_task(self._refresh_loop(), name="address-cache-refresh")
        logger.info("AddressCache started (refresh_interval=%.0fs)", self._refresh_interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def get(self, address: str) -> CachedInvoice | None:
        """Return the cached invoice for *address*, or None."""
        return self._cache.get(address)

    def __contains__(self, address: str) -> bool:
        return address in self._cache

    def size(self) -> int:
        return len(self._cache)

    async def add(self, invoice: CachedInvoice) -> None:
        """Immediately add a single invoice (called after invoice creation)."""
        async with self._lock:
            self._cache[invoice.address] = invoice
            logger.debug("AddressCache: added %s (total=%d)", invoice.address, len(self._cache))

    async def remove(self, address: str) -> None:
        async with self._lock:
            self._cache.pop(address, None)

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    async def refresh(self) -> None:
        """Reload all NEW + PARTIAL invoice addresses from the database."""
        try:
            new_cache: dict[str, CachedInvoice] = {}

            # ВАЖНО: строим кеш ВНУТРИ session — иначе lazy-load inv.coin упадёт
            # с DetachedInstanceError после закрытия сессии.
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Invoice)
                    .join(Invoice.coin)
                    .where(Invoice.status.in_([InvoiceStatus.NEW, InvoiceStatus.PARTIAL, InvoiceStatus.PENDING]))
                    .options(joinedload(Invoice.coin))
                )
                invoices = result.scalars().all()

                for inv in invoices:
                    new_cache[inv.address] = CachedInvoice(
                        invoice_id=str(inv.id),
                        address=inv.address,
                        amount_expected=inv.amount_expected,
                        coin_symbol=inv.coin.symbol,   # coin уже загружен (joinedload)
                        merchant_id=str(inv.merchant_id),
                    )

            async with self._lock:
                self._cache = new_cache

            logger.debug("AddressCache refreshed: %d active addresses", len(new_cache))
        except Exception as exc:
            logger.exception("AddressCache refresh failed: %s", exc)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval)
            await self.refresh()
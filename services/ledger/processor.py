"""
Transaction processor — handles one LedgerJob end-to-end.

Responsibilities
----------------
1. Upsert Transaction row (idempotent on txid + invoice_id).
2. Ask the appropriate blockchain provider for the current confirmation count.
3. Update Invoice status:
       0 conf  → PENDING
       1..N-1  → PENDING  (keep waiting)
       N+ conf → trigger AccountingEngine.credit()
4. On insufficient confirmations → re-queue via LedgerQueue.push_retry().
5. On PAID → optionally fire webhook (hook point, not implemented here).

Multi-chain
-----------
The processor is coin-agnostic: it calls provider.check_confirmations(txid)
and delegates all balance logic to AccountingEngine.  Adding ETH/TRX means
only registering a new provider — zero changes here.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import AsyncSessionLocal
from core.models import Coin, Invoice, InvoiceStatus, Merchant, Transaction
from providers.base import ProviderError
from providers.registry import registry
from services.ledger.accounting import AccountingEngine, AlreadyCreditedError
from services.ledger.queue import LedgerJob, LedgerQueue

logger = logging.getLogger(__name__)

REQUIRED_CONFIRMATIONS = settings.ledger_required_confirmations
MAX_RETRIES = settings.ledger_max_retries


class TxProcessor:
    """Stateless processor — one instance can handle many jobs concurrently."""

    def __init__(self, queue: LedgerQueue) -> None:
        self._queue = queue

    # ─────────────────────────────────────────────────────────────────────
    # Public
    # ─────────────────────────────────────────────────────────────────────

    async def process(self, job: LedgerJob) -> None:
        """
        Process a single LedgerJob.  Never raises — errors are logged and
        the job is re-queued or discarded after max_retries.
        """
        logger.info(
            "Processing job txid=%s invoice=%s retry=%d",
            job.txid, job.invoice_id, job.retry_count,
        )

        # ── 1. Discard if exceeded max retries ────────────────────────
        if job.retry_count > MAX_RETRIES:
            logger.error(
                "txid=%s exceeded max retries (%d) — discarding",
                job.txid, MAX_RETRIES,
            )
            return

        # ── 2. Check confirmations via provider ───────────────────────
        try:
            confirmations = await self._get_confirmations(job)
        except ProviderError as exc:
            logger.warning("Provider error for txid=%s: %s — will retry", job.txid, exc)
            await self._queue.push_retry(job)
            return

        logger.info("txid=%s confirmations=%d (required=%d)", job.txid, confirmations, REQUIRED_CONFIRMATIONS)
        job.confirmations = confirmations

        # ── 3. DB work inside one transaction ────────────────────────
        async with AsyncSessionLocal() as session:
            async with session.begin():
                try:
                    await self._process_in_session(session, job, confirmations)
                except AlreadyCreditedError as exc:
                    logger.info(str(exc))   # idempotent — not an error
                except Exception as exc:
                    logger.exception("DB error processing txid=%s: %s", job.txid, exc)
                    # session.begin() will rollback on __aexit__ with exception
                    raise

    # ─────────────────────────────────────────────────────────────────────
    # Private
    # ─────────────────────────────────────────────────────────────────────

    async def _get_confirmations(self, job: LedgerJob) -> int:
        """Delegate to the coin-specific provider."""
        if job.coin_symbol not in registry:
            raise ProviderError(f"No provider for coin '{job.coin_symbol}'")
        provider = registry.get(job.coin_symbol)
        return await provider.check_confirmations(job.txid)

    async def _process_in_session(
        self,
        session: AsyncSession,
        job: LedgerJob,
        confirmations: int,
    ) -> None:
        """All DB reads and writes happen inside this method (one transaction)."""

        # ── Load related objects ──────────────────────────────────────
        invoice = await self._load_invoice(session, job.invoice_id)
        if invoice is None:
            logger.error("Invoice %s not found — discarding txid=%s", job.invoice_id, job.txid)
            return

        if invoice.status == InvoiceStatus.PAID:
            logger.info("Invoice %s already PAID — skipping", invoice.id)
            return

        await session.refresh(invoice, ["merchant", "coin"])
        merchant: Merchant = invoice.merchant
        coin: Coin = invoice.coin

        # ── Upsert Transaction row ────────────────────────────────────
        tx = await self._upsert_transaction(session, job, invoice)

        # ── Update confirmations on the tx row ────────────────────────
        tx.confirmations = confirmations
        tx.retry_count = job.retry_count

        # ── Status machine ────────────────────────────────────────────
        if confirmations < REQUIRED_CONFIRMATIONS:
            # Mark invoice PENDING so the API shows "waiting for confirmations"
            if invoice.status == InvoiceStatus.NEW:
                invoice.status = InvoiceStatus.PENDING

            await session.flush()
            # Re-queue for later; no commit needed (just flush for status update)
            await self._queue.push_retry(job)
            logger.info(
                "Invoice %s → PENDING (%d/%d conf) — retry #%d scheduled",
                invoice.id, confirmations, REQUIRED_CONFIRMATIONS, job.retry_count + 1,
            )
            return

        # ── Enough confirmations → credit the balance ─────────────────
        engine = AccountingEngine(session)
        result = await engine.credit(
            transaction=tx,
            invoice=invoice,
            merchant=merchant,
            coin=coin,
        )

        logger.info(
            "Invoice %s PAID ✓ | gross=%s fee=%s net=%s credited to merchant %s",
            invoice.id, result.gross, result.fee, result.net, merchant.id,
        )

    async def _load_invoice(
        self, session: AsyncSession, invoice_id: str
    ) -> Invoice | None:
        result = await session.execute(
            select(Invoice).where(Invoice.id == uuid.UUID(invoice_id))
        )
        return result.scalar_one_or_none()

    async def _upsert_transaction(
        self,
        session: AsyncSession,
        job: LedgerJob,
        invoice: Invoice,
    ) -> Transaction:
        """
        Insert Transaction if it doesn't exist yet, otherwise return existing.

        Keyed on (txid, invoice_id) — the same txid can pay multiple invoices
        in theory (one tx, multiple outputs), so we key on both.
        """
        result = await session.execute(
            select(Transaction).where(
                Transaction.txid == job.txid,
                Transaction.invoice_id == invoice.id,
            )
        )
        tx = result.scalar_one_or_none()

        if tx is None:
            # Calculate USD value based on the proportion of the payment
            amount_usd = Decimal("0")
            if invoice.amount_expected > 0:
                amount_usd = (job.amount_coin / invoice.amount_expected) * (invoice.amount_usd or Decimal("0"))
            
            tx = Transaction(
                invoice_id=invoice.id,
                txid=job.txid,
                amount_received=job.amount_coin,
                amount_usd=amount_usd.quantize(Decimal("0.01")),
                confirmations=job.confirmations,
                credited=False,
                retry_count=job.retry_count,
            )
            session.add(tx)
            await session.flush()
            logger.debug("Inserted Transaction row for txid=%s", job.txid)
        else:
            logger.debug("Existing Transaction row found for txid=%s", job.txid)

        return tx

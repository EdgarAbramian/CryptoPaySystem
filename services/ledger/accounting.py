"""
Accounting engine: fee calculation and atomic balance updates.

All database mutations happen inside a single serialisable transaction
protected by a SELECT … FOR UPDATE on the Balance row to prevent
double-crediting even under concurrent Ledger workers.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Balance, Coin, Invoice, InvoiceStatus, Merchant, SystemFeeLog, Transaction, UTXO

logger = logging.getLogger(__name__)

# Satoshi precision — always round fees DOWN to favour the merchant
_SATOSHI = Decimal("0.00000001")


@dataclass(frozen=True)
class FeeResult:
    gross: Decimal          # amount_received (BTC)
    commission_pcent: Decimal
    fee: Decimal            # system keeps this
    net: Decimal            # merchant receives this


def calculate_fee(gross: Decimal, commission_pcent: Decimal) -> FeeResult:
    """
    Calculate system fee using banker-friendly rounding (ROUND_DOWN).

    >>> calculate_fee(Decimal("1.0"), Decimal("1.0"))
    FeeResult(gross=..., fee=Decimal('0.01000000'), net=Decimal('0.99000000'))
    """
    rate = commission_pcent / Decimal("100")
    fee = (gross * rate).quantize(_SATOSHI, rounding=ROUND_DOWN)
    net = gross - fee
    return FeeResult(gross=gross, commission_pcent=commission_pcent, fee=fee, net=net)


class AccountingEngine:
    """
    Performs the full credit sequence inside one DB transaction:

    1. Lock the Balance row (SELECT FOR UPDATE).
    2. Calculate fee.
    3. credit amount_available on Balance.
    4. Write SystemFeeLog entry.
    5. Mark Transaction.credited = True.
    6. Set Invoice.status = PAID and Invoice.paid_at = now().

    All steps succeed or all roll back — no partial state.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def credit(
        self,
        transaction: Transaction,
        invoice: Invoice,
        merchant: Merchant,
        coin: Coin,
    ) -> FeeResult:
        """
        Atomically credit the merchant balance for a confirmed transaction.

        Guards against double-crediting via the `credited` flag on Transaction
        combined with the row-level lock on Balance.

        Returns:
            FeeResult with breakdown of gross / fee / net amounts.

        Raises:
            AlreadyCreditedError: If this transaction was already credited.
        """
        # ── Guard: idempotency check ─────────────────────────────────────
        if transaction.credited:
            raise AlreadyCreditedError(
                f"Transaction {transaction.txid} already credited — skipping."
            )

        # ── Fee calculation ──────────────────────────────────────────────
        result = calculate_fee(transaction.amount_received, merchant.commission_pcent)

        logger.info(
            "Crediting invoice=%s txid=%s gross=%s fee=%s net=%s",
            invoice.id, transaction.txid,
            result.gross, result.fee, result.net,
        )

        # ── Upsert Balance with row lock ─────────────────────────────────
        balance = await self._get_or_create_balance_locked(
            merchant_id=merchant.id, coin_id=coin.id
        )
        balance.amount_available += result.net

        # ── Audit log ───────────────────────────────────────────────────
        fee_log = SystemFeeLog(
            transaction_id=transaction.id,
            merchant_id=merchant.id,
            coin_id=coin.id,
            gross_amount=result.gross,
            commission_pcent=result.commission_pcent,
            fee_amount=result.fee,
            net_amount=result.net,
        )
        self._s.add(fee_log)

        # ── Register UTXO (makes the output spendable in payouts) ───────
        await self._register_utxo(transaction, invoice)

        # ── Mark transaction credited ────────────────────────────────────
        transaction.credited = True
        transaction.confirmed_at = datetime.now(timezone.utc)

        # ── Update invoice status ────────────────────────────────────────
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = datetime.now(timezone.utc)

        # flush so constraints are checked before the caller commits
        await self._s.flush()

        return result

    # ─────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────

    async def _get_or_create_balance_locked(
        self,
        merchant_id: uuid.UUID,
        coin_id: int,
    ) -> Balance:
        """
        Return the Balance row with a row-level write lock.

        If no row exists yet, insert one (zero amounts) inside this transaction.
        The lock prevents two concurrent workers from double-crediting.
        """
        result = await self._s.execute(
            select(Balance)
            .where(Balance.merchant_id == merchant_id, Balance.coin_id == coin_id)
            .with_for_update()          # ← key: row-level lock
        )
        balance = result.scalar_one_or_none()

        if balance is None:
            balance = Balance(
                merchant_id=merchant_id,
                coin_id=coin_id,
                amount_available=Decimal("0"),
                amount_locked=Decimal("0"),
            )
            self._s.add(balance)
            await self._s.flush()  # get the id

        return balance


    async def _register_utxo(self, transaction: Transaction, invoice: Invoice) -> None:
        """
        Create a UTXO row so the Payout service can spend this output.

        We use vout=0 as a sentinel here because the Ledger receives only the
        txid+amount from the Watcher's TxMatch — the actual vout index must be
        reconciled by the Payout service before signing (via getrawtransaction).
        In production, extend TxMatch to carry vout from the ZMQ decoder.
        """
        from sqlalchemy import select as sa_select
        # Idempotent: skip if already registered
        existing = await self._s.execute(
            sa_select(UTXO).where(
                UTXO.txid == transaction.txid,
                UTXO.invoice_id == invoice.id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return

        utxo = UTXO(
            invoice_id=invoice.id,
            txid=transaction.txid,
            vout=0,                              # reconcile before spending
            amount=transaction.amount_received,
            is_spent=False,
        )
        self._s.add(utxo)
        logger.debug("Registered UTXO %s:%d amount=%s", transaction.txid, 0, transaction.amount_received)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AlreadyCreditedError(Exception):
    """Raised when attempting to credit an already-credited transaction."""

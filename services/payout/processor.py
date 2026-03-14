"""
PayoutProcessor — orchestrates the full payout lifecycle for one Payout row.

Lifecycle
─────────
PENDING
  └─► lock balance (amount_locked += amount_requested)
  └─► select UTXOs (SELECT FOR UPDATE)
  └─► estimate fee via RPC
      ↓
PROCESSING
  └─► build + sign transaction (BitcoinProvider.create_payout_transaction)
  └─► verify balance equation: Σutxo = amount_net + fee + change
  └─► store raw_tx_hex in Payout row
      ↓
SENT
  └─► sendrawtransaction via RPC
  └─► mark UTXOs is_spent=True, spent_by_payout_id=payout.id
  └─► release lock: amount_locked -= amount_requested
  └─► deduct from amount_available: amount_available -= amount_net
  └─► store txid, clear raw_tx_hex

If any step after SENT fails → FAILED (with error_message).

All DB changes within a single step use session.begin() for atomicity.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import AsyncSessionLocal
from core.models import Balance, Coin, Invoice, Merchant, Payout, PayoutStatus, UTXO
from providers.bitcoin import BitcoinProvider, InsufficientFundsError
from providers.registry import registry
from services.payout.selector import InsufficientUTXOError, SelectedUTXO, UTXOSelector

logger = logging.getLogger(__name__)

_SAT = Decimal("100_000_000")


class PayoutProcessor:
    """Processes a single Payout row end-to-end."""

    async def execute(self, payout_id: uuid.UUID) -> None:
        """
        Run the full payout pipeline.  Errors are caught and written to
        the Payout row; this method never propagates exceptions.
        """
        logger.info("PayoutProcessor: starting payout %s", payout_id)
        try:
            await self._run(payout_id)
        except Exception as exc:
            logger.exception("PayoutProcessor: unhandled error for %s: %s", payout_id, exc)
            await self._mark_failed(payout_id, str(exc))

    # ─────────────────────────────────────────────────────────────────────
    # Main pipeline
    # ─────────────────────────────────────────────────────────────────────

    async def _run(self, payout_id: uuid.UUID) -> None:

        # ── Step 1: Lock balance & select UTXOs ───────────────────────
        async with AsyncSessionLocal() as session:
            async with session.begin():
                payout, selected, fee_sat = await self._prepare(session, payout_id)

        # ── Step 2: Build & sign (CPU-bound, outside DB transaction) ──
        provider = self._get_btc_provider()
        change_address = await self._get_change_address(provider)
        amount_sat = int(payout.amount_requested * _SAT)

        try:
            signed = await provider.create_payout_transaction(
                utxos=[u.as_signing_dict() for u in selected],
                destination_address=payout.destination_address,
                amount_sat=amount_sat,
                change_address=change_address,
                fee_rate_sat_vbyte=fee_sat,
            )
        except InsufficientFundsError as exc:
            await self._mark_failed(payout_id, str(exc))
            return

        # ── Step 3: Persist signed tx + update to PROCESSING ──────────
        async with AsyncSessionLocal() as session:
            async with session.begin():
                p = await self._load_payout_locked(session, payout_id)
                p.status = PayoutStatus.PROCESSING
                p.miner_fee = Decimal(signed["fee_sat"]) / _SAT
                p.amount_net = Decimal(signed["amount_out_sat"]) / _SAT
                p.raw_tx_hex = signed["raw_hex"]
                await session.flush()

        # ── Step 4: Broadcast ─────────────────────────────────────────
        try:
            txid = await provider.broadcast_transaction(signed["raw_hex"])
        except Exception as exc:
            await self._mark_failed(payout_id, f"broadcast failed: {exc}")
            return

        logger.info("PayoutProcessor: broadcast OK txid=%s payout=%s", txid, payout_id)

        # ── Step 5: Finalise DB atomically ────────────────────────────
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await self._finalise(
                    session=session,
                    payout_id=payout_id,
                    txid=txid,
                    selected=selected,
                    signed=signed,
                    change_sat=signed["change_sat"],
                    amount_net_sat=signed["amount_out_sat"],
                )

    # ─────────────────────────────────────────────────────────────────────
    # Step helpers
    # ─────────────────────────────────────────────────────────────────────

    async def _prepare(
        self,
        session: AsyncSession,
        payout_id: uuid.UUID,
    ) -> tuple[Payout, list[SelectedUTXO], int]:
        """
        Inside a DB transaction:
          1. Load & lock the Payout row.
          2. Load & lock the Balance row.
          3. Validate balance.
          4. Select UTXOs (also locked).
          5. Lock (freeze) amount_requested in Balance.

        Returns: (payout, selected_utxos, fee_rate_sat_vbyte)
        """
        payout = await self._load_payout_locked(session, payout_id)

        if payout.status != PayoutStatus.PENDING:
            raise ValueError(f"Payout {payout_id} is not PENDING (status={payout.status})")

        # Load balance with row lock
        balance = await self._load_balance_locked(
            session, payout.merchant_id, payout.coin_id
        )

        amount_sat = int(payout.amount_requested * _SAT)

        if balance.amount_available < payout.amount_requested:
            raise ValueError(
                f"Insufficient balance: available={balance.amount_available} "
                f"requested={payout.amount_requested}"
            )

        # Get fee rate from node
        provider = self._get_btc_provider()
        fee_rate = await provider.get_fee_rate()

        # Estimate fee for UTXO selection (will be recalculated precisely during signing)
        rough_fee = 10 + 148 * 10 + 34 * 2   # assume up to 10 inputs
        rough_fee_sat = rough_fee * fee_rate

        # Select UTXOs
        selector = UTXOSelector(session)
        try:
            selected = await selector.select(
                merchant_id=payout.merchant_id,
                coin_id=payout.coin_id,
                target_sat=amount_sat,
                fee_estimate_sat=rough_fee_sat,
            )
        except InsufficientUTXOError as exc:
            raise ValueError(str(exc)) from exc

        # Freeze the requested amount in Balance
        balance.amount_available -= payout.amount_requested
        balance.amount_locked += payout.amount_requested

        await session.flush()
        logger.info(
            "Payout %s: locked %s BTC, selected %d UTXOs",
            payout_id, payout.amount_requested, len(selected),
        )
        return payout, selected, fee_rate

    async def _finalise(
        self,
        session: AsyncSession,
        payout_id: uuid.UUID,
        txid: str,
        selected: list[SelectedUTXO],
        signed: dict,
        change_sat: int,
        amount_net_sat: int,
    ) -> None:
        """
        After successful broadcast — atomic commit of all final state:
          - Payout → SENT
          - UTXOs → is_spent=True
          - Balance.amount_locked released (deducted permanently)
        """
        payout = await self._load_payout_locked(session, payout_id)

        # Mark UTXOs spent
        for s_utxo in selected:
            result = await session.execute(
                select(UTXO).where(UTXO.id == uuid.UUID(s_utxo.utxo_id)).with_for_update()
            )
            utxo = result.scalar_one()
            utxo.is_spent = True
            utxo.spent_by_payout_id = payout_id

        # Release the lock from Balance
        balance = await self._load_balance_locked(
            session, payout.merchant_id, payout.coin_id
        )
        balance.amount_locked -= payout.amount_requested
        # amount_available was already deducted in _prepare

        # Update payout
        payout.status = PayoutStatus.SENT
        payout.txid = txid
        payout.raw_tx_hex = None          # clear sensitive data
        payout.sent_at = datetime.now(timezone.utc)
        payout.miner_fee = Decimal(signed["fee_sat"]) / _SAT
        payout.amount_net = Decimal(amount_net_sat) / _SAT

        await session.flush()
        logger.info(
            "Payout %s SENT ✓ txid=%s fee=%d sat net=%d sat",
            payout_id, txid, signed["fee_sat"], amount_net_sat,
        )

    # ─────────────────────────────────────────────────────────────────────
    # DB helpers
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _load_payout_locked(session: AsyncSession, payout_id: uuid.UUID) -> Payout:
        result = await session.execute(
            select(Payout).where(Payout.id == payout_id).with_for_update()
        )
        payout = result.scalar_one_or_none()
        if payout is None:
            raise ValueError(f"Payout {payout_id} not found")
        return payout

    @staticmethod
    async def _load_balance_locked(
        session: AsyncSession,
        merchant_id: uuid.UUID,
        coin_id: int,
    ) -> Balance:
        result = await session.execute(
            select(Balance)
            .where(Balance.merchant_id == merchant_id, Balance.coin_id == coin_id)
            .with_for_update()
        )
        balance = result.scalar_one_or_none()
        if balance is None:
            raise ValueError(
                f"No balance row for merchant={merchant_id} coin={coin_id}"
            )
        return balance

    # ─────────────────────────────────────────────────────────────────────
    # Provider helpers
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_btc_provider() -> BitcoinProvider:
        provider = registry.get("BTC")
        if not isinstance(provider, BitcoinProvider):
            raise RuntimeError("BTC provider is not a BitcoinProvider instance")
        return provider

    async def _get_change_address(self, provider: BitcoinProvider) -> str:
        """
        Derive the internal change address (BIP44 change=1 branch).
        Uses a fixed high index to keep change separate from deposit addresses.
        """
        return await provider.generate_address(
            index=settings.payout_change_index,
            change=1,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Error handling
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _mark_failed(payout_id: uuid.UUID, reason: str) -> None:
        """
        Mark payout as FAILED and release the balance lock if it was frozen.
        Best-effort: a second DB error here is logged and swallowed.
        """
        try:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    result = await session.execute(
                        select(Payout).where(Payout.id == payout_id).with_for_update()
                    )
                    payout = result.scalar_one_or_none()
                    if payout is None:
                        return

                    was_locked = payout.status in (
                        PayoutStatus.PENDING, PayoutStatus.PROCESSING
                    )

                    payout.status = PayoutStatus.FAILED
                    payout.error_message = reason[:2000]
                    payout.raw_tx_hex = None

                    # Release the frozen balance if we had locked it
                    if was_locked and payout.amount_requested:
                        balance_result = await session.execute(
                            select(Balance)
                            .where(
                                Balance.merchant_id == payout.merchant_id,
                                Balance.coin_id == payout.coin_id,
                            )
                            .with_for_update()
                        )
                        balance = balance_result.scalar_one_or_none()
                        if balance:
                            refund = min(balance.amount_locked, payout.amount_requested)
                            balance.amount_locked -= refund
                            balance.amount_available += refund

                    await session.flush()
            logger.error("Payout %s marked FAILED: %s", payout_id, reason)
        except Exception as exc:
            logger.exception(
                "Could not mark payout %s as FAILED: %s", payout_id, exc
            )

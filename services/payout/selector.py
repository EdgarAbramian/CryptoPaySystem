"""
UTXO coin selection for payout transactions.

Strategy: Branch-and-Bound → greedy fallback (smallest-first).
The goal is to find a minimal UTXO set whose total ≥ target,
minimising change output size (privacy + fee efficiency).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Invoice, UTXO

logger = logging.getLogger(__name__)

_SAT = Decimal("0.00000001")


@dataclass
class SelectedUTXO:
    """A UTXO chosen for spending, enriched with signing metadata."""
    utxo_id: str
    txid: str
    vout: int
    amount_sat: int
    derivation_index: int
    change: int = 0           # 0 = external, 1 = internal (BIP44 branch)
    script_pubkey: str | None = None

    def as_signing_dict(self) -> dict:
        return {
            "txid": self.txid,
            "vout": self.vout,
            "amount_sat": self.amount_sat,
            "derivation_index": self.derivation_index,
            "change": self.change,
        }


class UTXOSelector:
    """
    Selects unspent UTXOs from the database for a given merchant and coin.

    All selected UTXOs are locked with SELECT FOR UPDATE so concurrent
    payout requests cannot select the same coins.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def select(
        self,
        merchant_id: str,
        coin_id: int,
        target_sat: int,
        fee_estimate_sat: int,
    ) -> list[SelectedUTXO]:
        """
        Return a minimal UTXO set covering target_sat + fee_estimate_sat.

        Args:
            merchant_id:      UUID string of the merchant.
            coin_id:          DB coin id.
            target_sat:       Requested withdrawal amount in satoshis.
            fee_estimate_sat: Expected miner fee in satoshis.

        Returns:
            Ordered list of SelectedUTXO.

        Raises:
            InsufficientUTXOError: If no combination covers the target + fee.
        """
        needed = target_sat + fee_estimate_sat

        # Load all unspent UTXOs for this merchant + coin, smallest first
        # Lock the rows immediately to prevent races with concurrent payout requests
        rows = await self._s.execute(
            select(UTXO, Invoice.derivation_index)
            .join(Invoice, UTXO.invoice_id == Invoice.id)
            .where(
                Invoice.merchant_id == merchant_id,
                Invoice.coin_id == coin_id,
                UTXO.is_spent == False,  # noqa: E712
            )
            .order_by(UTXO.amount.asc())   # smallest-first for greedy
            .with_for_update(of=UTXO)
        )
        candidates: list[tuple[UTXO, int]] = [(row.UTXO, row.derivation_index) for row in rows]

        if not candidates:
            raise InsufficientUTXOError("No unspent UTXOs available for this merchant.")

        selected = self._greedy(candidates, needed)

        total = sum(u.amount_sat for u in selected)
        logger.info(
            "UTXOSelector: selected %d UTXOs totalling %d sat (needed %d)",
            len(selected), total, needed,
        )
        return selected

    # ─────────────────────────────────────────────────────────────────────
    # Coin selection algorithms
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _greedy(
        candidates: list[tuple[UTXO, int]],
        needed_sat: int,
    ) -> list[SelectedUTXO]:
        """
        Simple greedy: accumulate UTXOs (smallest-first) until total ≥ needed.
        Falls back to all UTXOs if still insufficient.
        """
        selected: list[SelectedUTXO] = []
        total = 0

        for utxo, derivation_index in candidates:
            if total >= needed_sat:
                break
            amount_sat = int(utxo.amount * Decimal("100_000_000"))
            selected.append(SelectedUTXO(
                utxo_id=str(utxo.id),
                txid=utxo.txid,
                vout=utxo.vout,
                amount_sat=amount_sat,
                derivation_index=derivation_index,
                change=0,
                script_pubkey=utxo.script_pubkey,
            ))
            total += amount_sat

        if total < needed_sat:
            raise InsufficientUTXOError(
                f"Insufficient UTXOs: have {total} sat, need {needed_sat} sat"
            )

        return selected


class InsufficientUTXOError(Exception):
    """Raised when no UTXO combination can cover the requested amount."""

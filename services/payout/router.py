"""
Payout API endpoints.

POST /api/v1/payouts/create  — merchant requests a withdrawal
GET  /api/v1/payouts/{id}    — check payout status
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from core.config import settings
from core.database import DbSession
from core.models import Balance, Coin, Merchant, Payout, PayoutStatus
from providers.registry import registry
from services.payout.processor import PayoutProcessor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payouts", tags=["payouts"])

_SAT = Decimal("100_000_000")
_MIN_SAT = settings.payout_min_amount_sat


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class PayoutCreateRequest(BaseModel):
    merchant_id: uuid.UUID
    coin_symbol: str = Field(min_length=1, max_length=16)
    destination_address: str = Field(min_length=10, max_length=128)
    amount: Decimal = Field(gt=Decimal("0"), description="Amount in coin units (BTC)")

    @field_validator("coin_symbol")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper().strip()


class PayoutOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    destination_address: str
    amount_requested: Decimal
    amount_net: Decimal | None
    miner_fee: Decimal | None
    status: PayoutStatus
    txid: str | None

    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/create",
    response_model=PayoutOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a merchant payout",
)
async def create_payout(
    payload: PayoutCreateRequest,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> PayoutOut:
    """
    Create a withdrawal request and immediately enqueue it for processing.

    The response is 202 Accepted — the payout is processed asynchronously.
    Poll GET /payouts/{id} to track status.
    """
    # ── Validate coin ─────────────────────────────────────────────────
    coin_result = await db.execute(
        select(Coin).where(Coin.symbol == payload.coin_symbol, Coin.is_active == True)  # noqa
    )
    coin = coin_result.scalar_one_or_none()
    if coin is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Coin '{payload.coin_symbol}' not found or inactive.",
        )

    # ── Validate merchant ─────────────────────────────────────────────
    merchant_result = await db.execute(
        select(Merchant).where(Merchant.id == payload.merchant_id)
    )
    merchant = merchant_result.scalar_one_or_none()
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Merchant '{payload.merchant_id}' not found.",
        )

    # ── Dust check ────────────────────────────────────────────────────
    amount_sat = int(payload.amount * _SAT)
    if amount_sat < _MIN_SAT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Amount too small. Minimum is {_MIN_SAT} sat.",
        )

    # ── Validate destination address ──────────────────────────────────
    if payload.coin_symbol in registry:
        provider = registry.get(payload.coin_symbol)
        if hasattr(provider, "validate_address"):
            valid = await provider.validate_address(payload.destination_address)
            if not valid:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid destination address for this network.",
                )

    # ── Check available balance (non-locking preview) ─────────────────
    balance_result = await db.execute(
        select(Balance).where(
            Balance.merchant_id == payload.merchant_id,
            Balance.coin_id == coin.id,
        )
    )
    balance = balance_result.scalar_one_or_none()
    if balance is None or balance.amount_available < payload.amount:
        available = balance.amount_available if balance else Decimal("0")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Insufficient balance: available={available} requested={payload.amount}",
        )

    # ── Create Payout row ─────────────────────────────────────────────
    payout = Payout(
        merchant_id=payload.merchant_id,
        coin_id=coin.id,
        destination_address=payload.destination_address,
        amount_requested=payload.amount,
        status=PayoutStatus.PENDING,
    )
    db.add(payout)
    await db.flush()   # get payout.id before background task runs

    payout_id = payout.id
    logger.info(
        "Payout %s created for merchant=%s amount=%s %s → %s",
        payout_id, payload.merchant_id, payload.amount,
        payload.coin_symbol, payload.destination_address,
    )

    # ── Enqueue processing ────────────────────────────────────────────
    background_tasks.add_task(_process_payout, payout_id)

    return PayoutOut.model_validate(payout)


@router.get(
    "/{payout_id}",
    response_model=PayoutOut,
    summary="Get payout status",
)
async def get_payout(payout_id: uuid.UUID, db: DbSession) -> PayoutOut:
    result = await db.execute(select(Payout).where(Payout.id == payout_id))
    payout = result.scalar_one_or_none()
    if payout is None:
        raise HTTPException(status_code=404, detail="Payout not found")
    return PayoutOut.model_validate(payout)


# ─────────────────────────────────────────────────────────────────────────────
# Background task
# ─────────────────────────────────────────────────────────────────────────────

async def _process_payout(payout_id: uuid.UUID) -> None:
    """FastAPI background task — runs after the HTTP response is sent."""
    processor = PayoutProcessor()
    await processor.execute(payout_id)

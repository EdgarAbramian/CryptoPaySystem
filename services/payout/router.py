"""
Payout endpoints — требуют X-API-Key аутентификации.

POST /api/v1/payouts/create    — запросить вывод средств
GET  /api/v1/payouts           — список payout мерчанта
GET  /api/v1/payouts/{id}      — статус конкретного payout
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from core.config import settings
from core.database import DbSession
from core.models import Balance, Coin, Merchant, Payout, PayoutStatus
from providers.registry import registry
from services.api.auth import get_current_merchant
from services.payout.processor import PayoutProcessor

logger = logging.getLogger(__name__)

CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]

router = APIRouter(prefix="/payouts", tags=["payouts"])

_SAT = Decimal("100_000_000")
_MIN_SAT = settings.payout_min_amount_sat


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class PayoutCreateRequest(BaseModel):
    coin_symbol: str = Field(min_length=1, max_length=16)
    destination_address: str = Field(min_length=10, max_length=128)
    amount: Decimal = Field(gt=Decimal("0"), description="Сумма в единицах монеты (BTC)")

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
    created_at: str
    sent_at: str | None

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, p: Payout) -> "PayoutOut":
        return cls(
            id=p.id,
            merchant_id=p.merchant_id,
            destination_address=p.destination_address,
            amount_requested=p.amount_requested,
            amount_net=p.amount_net,
            miner_fee=p.miner_fee,
            status=p.status,
            txid=p.txid,
            created_at=p.created_at.isoformat(),
            sent_at=p.sent_at.isoformat() if p.sent_at else None,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/create",
    response_model=PayoutOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Запросить вывод средств",
)
async def create_payout(
    payload: PayoutCreateRequest,
    db: DbSession,
    merchant: CurrentMerchant,
    background_tasks: BackgroundTasks,
) -> PayoutOut:
    """
    Создаёт заявку на вывод и немедленно запускает обработку в фоне.

    Ответ 202 Accepted — payout обрабатывается асинхронно.
    Для проверки статуса: GET /api/v1/payouts/{id}

    **Аутентификация**: X-API-Key заголовок.
    """
    # Проверяем монету
    coin_result = await db.execute(
        select(Coin).where(Coin.symbol == payload.coin_symbol, Coin.is_active == True)  # noqa
    )
    coin = coin_result.scalar_one_or_none()
    if coin is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Coin '{payload.coin_symbol}' not found or inactive.",
        )

    # Проверяем минимальную сумму
    amount_sat = int(payload.amount * _SAT)
    if amount_sat < _MIN_SAT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Amount too small. Minimum is {_MIN_SAT} sat ({Decimal(_MIN_SAT) / _SAT} BTC).",
        )

    # Проверяем адрес назначения (если провайдер поддерживает)
    if payload.coin_symbol in registry:
        provider = registry.get(payload.coin_symbol)
        if hasattr(provider, "validate_address"):
            valid = await provider.validate_address(payload.destination_address)
            if not valid:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid destination address for this network.",
                )

    # Проверяем баланс (предварительная, без блокировки)
    balance_result = await db.execute(
        select(Balance).where(
            Balance.merchant_id == merchant.id,
            Balance.coin_id == coin.id,
        )
    )
    balance = balance_result.scalar_one_or_none()
    available = balance.amount_available if balance else Decimal("0")
    if balance is None or available < payload.amount:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Insufficient balance: available={available} {payload.coin_symbol}, requested={payload.amount}.",
        )

    # Создаём payout запись
    payout = Payout(
        merchant_id=merchant.id,
        coin_id=coin.id,
        destination_address=payload.destination_address,
        amount_requested=payload.amount,
        status=PayoutStatus.PENDING,
    )
    db.add(payout)
    await db.flush()

    logger.info(
        "Payout %s created: merchant=%s amount=%s %s → %s",
        payout.id, merchant.id, payload.amount,
        payload.coin_symbol, payload.destination_address,
    )

    background_tasks.add_task(_process_payout, payout.id)
    return PayoutOut.from_orm(payout)


@router.get(
    "",
    response_model=list[PayoutOut],
    summary="Список выплат мерчанта",
)
async def list_payouts(
    db: DbSession,
    merchant: CurrentMerchant,
    status_filter: PayoutStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[PayoutOut]:
    """Возвращает payout текущего мерчанта с фильтрацией по статусу."""
    q = select(Payout).where(Payout.merchant_id == merchant.id)
    if status_filter is not None:
        q = q.where(Payout.status == status_filter)
    q = q.order_by(Payout.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(q)
    return [PayoutOut.from_orm(p) for p in result.scalars().all()]


@router.get(
    "/{payout_id}",
    response_model=PayoutOut,
    summary="Статус выплаты",
)
async def get_payout(
    payout_id: uuid.UUID,
    db: DbSession,
    merchant: CurrentMerchant,
) -> PayoutOut:
    """Мерчант может видеть только свои payout."""
    result = await db.execute(
        select(Payout).where(
            Payout.id == payout_id,
            Payout.merchant_id == merchant.id,  # изоляция данных
        )
    )
    payout = result.scalar_one_or_none()
    if payout is None:
        raise HTTPException(status_code=404, detail="Payout not found.")
    return PayoutOut.from_orm(payout)


# ─────────────────────────────────────────────────────────────────────────────
# Background task
# ─────────────────────────────────────────────────────────────────────────────

async def _process_payout(payout_id: uuid.UUID) -> None:
    processor = PayoutProcessor()
    await processor.execute(payout_id)
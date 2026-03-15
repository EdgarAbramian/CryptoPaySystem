"""
Merchant self-service endpoints.

GET   /api/v1/merchant/me          — профиль и балансы
PATCH /api/v1/merchant/me          — обновить webhook
GET   /api/v1/merchant/me/balance  — только балансы
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.database import DbSession
from core.models import Balance, Coin, Merchant
from services.api.auth import get_current_merchant

logger = logging.getLogger(__name__)

CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]

router = APIRouter(prefix="/merchant", tags=["merchant"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class BalanceOut(BaseModel):
    coin_symbol: str
    amount_available: Decimal
    amount_locked: Decimal


class MerchantProfileOut(BaseModel):
    id: str
    commission_pcent: Decimal
    webhook_url: str | None
    created_at: str
    balances: list[BalanceOut]


class MerchantUpdateRequest(BaseModel):
    webhook_url: str | None = Field(
        default=None,
        max_length=512,
        description="URL для webhook-уведомлений. Передай пустую строку чтобы удалить.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model=MerchantProfileOut,
    summary="Профиль и балансы текущего мерчанта",
)
async def get_profile(db: DbSession, merchant: CurrentMerchant) -> MerchantProfileOut:
    """Возвращает профиль и текущие балансы аутентифицированного мерчанта."""
    rows_result = await db.execute(
        select(Balance, Coin)
        .join(Coin, Balance.coin_id == Coin.id)
        .where(Balance.merchant_id == merchant.id)
    )
    rows = rows_result.all()
    balances = [
        BalanceOut(
            coin_symbol=row[1].symbol,
            amount_available=row[0].amount_available,
            amount_locked=row[0].amount_locked,
        )
        for row in rows
    ]
    return MerchantProfileOut(
        id=str(merchant.id),
        commission_pcent=merchant.commission_pcent,
        webhook_url=merchant.webhook_url,
        created_at=merchant.created_at.isoformat(),
        balances=balances,
    )


@router.patch(
    "/me",
    response_model=MerchantProfileOut,
    summary="Обновить webhook URL",
)
async def update_profile(
    payload: MerchantUpdateRequest,
    db: DbSession,
    merchant: CurrentMerchant,
) -> MerchantProfileOut:
    """
    Мерчант может самостоятельно обновить webhook_url.

    Передай `null` или пустую строку чтобы удалить webhook.
    """
    if payload.webhook_url is not None:
        merchant.webhook_url = payload.webhook_url if payload.webhook_url else None
        await db.flush()

    return await get_profile(db, merchant)


@router.get(
    "/me/balance",
    response_model=list[BalanceOut],
    summary="Балансы по всем монетам",
)
async def get_balance(db: DbSession, merchant: CurrentMerchant) -> list[BalanceOut]:
    """Компактный эндпоинт — только балансы без лишних данных."""
    rows_result = await db.execute(
        select(Balance, Coin)
        .join(Coin, Balance.coin_id == Coin.id)
        .where(Balance.merchant_id == merchant.id)
    )
    return [
        BalanceOut(
            coin_symbol=row[1].symbol,
            amount_available=row[0].amount_available,
            amount_locked=row[0].amount_locked,
        )
        for row in rows_result.all()
    ]
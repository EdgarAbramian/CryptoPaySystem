"""
Admin router: управление мерчантами.

Все эндпоинты требуют X-Admin-Secret заголовок.

POST   /api/v1/admin/merchants          — зарегистрировать мерчанта
GET    /api/v1/admin/merchants          — список всех мерчантов
GET    /api/v1/admin/merchants/{id}     — детали мерчанта
PATCH  /api/v1/admin/merchants/{id}     — обновить (комиссия, webhook)
DELETE /api/v1/admin/merchants/{id}     — деактивировать
POST   /api/v1/admin/merchants/{id}/rotate-key — сгенерировать новый api_key
"""
from __future__ import annotations

import logging
import secrets
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import func, select

from core.database import DbSession
from core.models import Balance, Coin, Merchant
from services.api.admin_auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/merchants",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class MerchantCreateRequest(BaseModel):
    commission_pcent: Annotated[
        Decimal,
        Field(ge=Decimal("0"), le=Decimal("100"), description="Комиссия системы в процентах")
    ] = Decimal("1.00")
    webhook_url: str | None = Field(
        default=None,
        max_length=512,
        description="URL для уведомлений о платежах (POST)",
    )
    note: str | None = Field(
        default=None,
        max_length=256,
        description="Внутренняя заметка об этом мерчанте",
    )


class MerchantUpdateRequest(BaseModel):
    commission_pcent: Annotated[
        Decimal | None,
        Field(ge=Decimal("0"), le=Decimal("100"))
    ] = None
    webhook_url: str | None = None
    note: str | None = None
    is_active: bool | None = None


class BalanceOut(BaseModel):
    coin_symbol: str
    amount_available: Decimal
    amount_locked: Decimal

    model_config = {"from_attributes": True}


class MerchantOut(BaseModel):
    id: uuid.UUID
    api_key: str
    commission_pcent: Decimal
    webhook_url: str | None
    is_active: bool
    created_at: str
    balances: list[BalanceOut] = []

    model_config = {"from_attributes": True}


class MerchantListItem(BaseModel):
    id: uuid.UUID
    api_key: str
    commission_pcent: Decimal
    webhook_url: str | None
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}


class RotateKeyResponse(BaseModel):
    merchant_id: uuid.UUID
    new_api_key: str


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _merchant_to_out(merchant: Merchant, balances: list[Balance], coins: dict[int, str]) -> MerchantOut:
    bal_out = [
        BalanceOut(
            coin_symbol=coins.get(b.coin_id, str(b.coin_id)),
            amount_available=b.amount_available,
            amount_locked=b.amount_locked,
        )
        for b in balances
    ]
    return MerchantOut(
        id=merchant.id,
        api_key=merchant.api_key,
        commission_pcent=merchant.commission_pcent,
        webhook_url=merchant.webhook_url,
        is_active=getattr(merchant, "is_active", True),
        created_at=merchant.created_at.isoformat(),
        balances=bal_out,
    )


async def _get_merchant_or_404(merchant_id: uuid.UUID, db: DbSession) -> Merchant:
    result = await db.execute(select(Merchant).where(Merchant.id == merchant_id))
    merchant = result.scalar_one_or_none()
    if merchant is None:
        raise HTTPException(status_code=404, detail=f"Merchant {merchant_id} not found.")
    return merchant


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=MerchantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать нового мерчанта",
)
async def create_merchant(payload: MerchantCreateRequest, db: DbSession) -> MerchantOut:
    """
    Создаёт нового мерчанта и возвращает его данные вместе с api_key.

    **api_key показывается только один раз** — сохрани его сразу.
    Для получения нового ключа используй `/rotate-key`.
    """
    api_key = secrets.token_urlsafe(32)

    merchant = Merchant(
        id=uuid.uuid4(),
        api_key=api_key,
        commission_pcent=payload.commission_pcent,
        webhook_url=str(payload.webhook_url) if payload.webhook_url else None,
    )
    db.add(merchant)

    # Автоматически создаём BTC баланс (нулевой)
    coins_result = await db.execute(select(Coin).where(Coin.is_active == True))  # noqa
    coins = coins_result.scalars().all()
    for coin in coins:
        db.add(Balance(
            merchant_id=merchant.id,
            coin_id=coin.id,
            amount_available=Decimal("0"),
            amount_locked=Decimal("0"),
        ))

    await db.flush()

    logger.info(
        "Admin: created merchant id=%s commission=%.2f%%",
        merchant.id, merchant.commission_pcent,
    )

    coins_map = {c.id: c.symbol for c in coins}
    balances_result = await db.execute(
        select(Balance).where(Balance.merchant_id == merchant.id)
    )
    balances = balances_result.scalars().all()

    return _merchant_to_out(merchant, balances, coins_map)


@router.get(
    "",
    response_model=list[MerchantListItem],
    summary="Список всех мерчантов",
)
async def list_merchants(
    db: DbSession,
    limit: int = 50,
    offset: int = 0,
) -> list[MerchantListItem]:
    """Пагинированный список мерчантов."""
    result = await db.execute(
        select(Merchant).order_by(Merchant.created_at.desc()).limit(limit).offset(offset)
    )
    merchants = result.scalars().all()
    return [
        MerchantListItem(
            id=m.id,
            api_key=m.api_key,
            commission_pcent=m.commission_pcent,
            webhook_url=m.webhook_url,
            is_active=getattr(m, "is_active", True),
            created_at=m.created_at.isoformat(),
        )
        for m in merchants
    ]


@router.get(
    "/{merchant_id}",
    response_model=MerchantOut,
    summary="Детали мерчанта с балансами",
)
async def get_merchant(merchant_id: uuid.UUID, db: DbSession) -> MerchantOut:
    merchant = await _get_merchant_or_404(merchant_id, db)

    balances_result = await db.execute(
        select(Balance, Coin).join(Coin, Balance.coin_id == Coin.id)
        .where(Balance.merchant_id == merchant_id)
    )
    rows = balances_result.all()
    balances = [row[0] for row in rows]
    coins_map = {row[1].id: row[1].symbol for row in rows}

    return _merchant_to_out(merchant, balances, coins_map)


@router.patch(
    "/{merchant_id}",
    response_model=MerchantOut,
    summary="Обновить настройки мерчанта",
)
async def update_merchant(
    merchant_id: uuid.UUID,
    payload: MerchantUpdateRequest,
    db: DbSession,
) -> MerchantOut:
    """Частичное обновление — передавай только те поля которые нужно изменить."""
    merchant = await _get_merchant_or_404(merchant_id, db)

    if payload.commission_pcent is not None:
        merchant.commission_pcent = payload.commission_pcent
    if payload.webhook_url is not None:
        merchant.webhook_url = payload.webhook_url if payload.webhook_url != "" else None

    await db.flush()
    logger.info("Admin: updated merchant id=%s", merchant_id)

    balances_result = await db.execute(
        select(Balance, Coin).join(Coin, Balance.coin_id == Coin.id)
        .where(Balance.merchant_id == merchant_id)
    )
    rows = balances_result.all()
    return _merchant_to_out(
        merchant,
        [r[0] for r in rows],
        {r[1].id: r[1].symbol for r in rows},
    )


@router.post(
    "/{merchant_id}/rotate-key",
    response_model=RotateKeyResponse,
    summary="Сгенерировать новый API ключ",
)
async def rotate_api_key(merchant_id: uuid.UUID, db: DbSession) -> RotateKeyResponse:
    """
    Генерирует новый api_key и **немедленно инвалидирует старый**.

    После вызова мерчант должен обновить ключ у себя.
    """
    merchant = await _get_merchant_or_404(merchant_id, db)
    new_key = secrets.token_urlsafe(32)
    merchant.api_key = new_key
    await db.flush()

    logger.warning("Admin: rotated api_key for merchant id=%s", merchant_id)
    return RotateKeyResponse(merchant_id=merchant_id, new_api_key=new_key)
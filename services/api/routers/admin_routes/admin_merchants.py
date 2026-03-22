from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
import sqlalchemy as sa

from core.database import DbSession
from core.models import Balance, Coin, Merchant, MerchantStatus
from services.api.admin_auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/merchants",
    tags=["admin-merchants"],
    dependencies=[Depends(require_admin)],
)


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class MerchantCreateRequest(BaseModel):
    name: str = Field(..., max_length=255)
    email: str = Field(..., max_length=255)
    commission_pcent: Annotated[
        Decimal,
        Field(ge=Decimal("0"), le=Decimal("100"), description="Комиссия системы в процентах")
    ] = Decimal("1.00")
    webhook_url: str | None = Field(
        default=None,
        max_length=1024,
        description="URL для уведомлений о платежах (POST)",
    )
    note: str | None = Field(
        default=None,
        description="Внутренняя заметка об этом мерчанте",
    )


class MerchantUpdateRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    commission_pcent: Annotated[
        Decimal | None,
        Field(ge=Decimal("0"), le=Decimal("100"))
    ] = None
    webhook_url: str | None = None
    note: str | None = None
    status: MerchantStatus | None = None
    tier: str | None = None


class MerchantStatusRequest(BaseModel):
    status: MerchantStatus


class MerchantConfigRequest(BaseModel):
    commission_pcent: Decimal
    tier: str | None = None


class BalanceOut(BaseModel):
    coin_symbol: str
    amount_available: Decimal
    amount_locked: Decimal

    model_config = {"from_attributes": True}


class MerchantOut(BaseModel):
    id: uuid.UUID
    api_key: str
    name: str | None
    email: str | None
    commission_pcent: Decimal
    webhook_url: str | None
    status: MerchantStatus
    tier: str
    volume_usd: Decimal = Decimal("0")
    transaction_count: int = 0
    created_at: str
    balances: list[BalanceOut] = []

    model_config = {"from_attributes": True}


class MerchantListItem(BaseModel):
    id: uuid.UUID
    api_key: str
    name: str | None
    email: str | None
    commission_pcent: Decimal
    webhook_url: str | None
    status: MerchantStatus
    tier: str
    volume_usd: Decimal = Decimal("0")
    transaction_count: int = 0
    created_at: str

    model_config = {"from_attributes": True}


class MerchantStatsOut(BaseModel):
    total: int
    active: int
    pending: int
    total_volume_usd: Decimal


class MerchantCompactOut(BaseModel):
    id: uuid.UUID
    name: str | None


class TopMerchantOut(BaseModel):
    id: uuid.UUID
    name: str | None
    total_volume_usd: Decimal
    transaction_count: int


class RotateKeyResponse(BaseModel):
    merchant_id: uuid.UUID
    new_api_key: str


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _merchant_to_out(
    merchant: Merchant, 
    balances: list[Balance], 
    coins: dict[int, str],
    volume_usd: Decimal = Decimal("0"),
    tx_count: int = 0
) -> MerchantOut:
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
        name=merchant.name,
        email=merchant.email,
        commission_pcent=merchant.commission_pcent,
        webhook_url=merchant.webhook_url,
        status=merchant.status,
        tier=merchant.tier,
        volume_usd=volume_usd,
        transaction_count=tx_count,
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

@router.get("/list-compact", response_model=list[MerchantCompactOut], summary="Компактный список мерчантов для фильтров")
async def list_merchants_compact(db: DbSession) -> list[MerchantCompactOut]:
    """
    Возвращает список ID и имен всех мерчантов.
    Используется для заполнения выпадающих списков в фильтрах.
    """
    result = await db.execute(select(Merchant.id, Merchant.name).order_by(sa.asc(Merchant.name)))
    return [MerchantCompactOut(id=row.id, name=row.name) for row in result.all()]


@router.get("/top", response_model=list[TopMerchantOut], summary="Топ мерчантов по обороту")
async def get_top_merchants(
    db: DbSession,
    period: str = Query("all", regex="^(day|week|month|all)$"),
    limit: int = Query(5, ge=1, le=20),
) -> list[TopMerchantOut]:
    """
    Рейтинг лучших мерчантов по обороту за указанный период.
    """
    from core.models import SystemFeeLog
    
    query = select(
        Merchant.id,
        Merchant.name,
        func.sum(SystemFeeLog.gross_amount_usd).label("total_vol"),
        func.count(SystemFeeLog.id).label("tx_cnt")
    )
    
    join_cond = Merchant.id == SystemFeeLog.merchant_id
    
    if period != "all":
        now = datetime.utcnow()
        if period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            # Start of the current week (Monday)
            start = now - timedelta(days=now.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "month":
            # Start of the current month
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        join_cond = sa.and_(join_cond, SystemFeeLog.created_at >= start)

    query = (
        query.outerjoin(SystemFeeLog, join_cond)
        .group_by(Merchant.id, Merchant.name)
        # Handle coalesce for NULL sorting
        .order_by(sa.desc(func.coalesce(func.sum(SystemFeeLog.gross_amount_usd), 0)))
        .limit(limit)
    )

    result = await db.execute(query)
    rows = result.all()
    
    return [
        TopMerchantOut(
            id=row.id,
            name=row.name,
            total_volume_usd=row.total_vol or Decimal("0"),
            transaction_count=row.tx_cnt or 0
        )
        for row in rows
    ]


@router.get("/stats", response_model=MerchantStatsOut, summary="Статистика по всем мерчантам")
async def get_merchants_stats(db: DbSession) -> MerchantStatsOut:
    """Агрегированная статистика для раздела управления мерчантами."""
    from core.models import SystemFeeLog
    
    total_result = await db.execute(select(func.count(Merchant.id)))
    total = total_result.scalar() or 0
    
    active_result = await db.execute(
        select(func.count(Merchant.id)).where(Merchant.status == MerchantStatus.ACTIVE)
    )
    active = active_result.scalar() or 0
    
    pending_result = await db.execute(
        select(func.count(Merchant.id)).where(Merchant.status == MerchantStatus.PENDING)
    )
    pending = pending_result.scalar() or 0
    
    volume_result = await db.execute(select(func.sum(SystemFeeLog.gross_amount_usd)))
    total_volume_usd = volume_result.scalar() or Decimal("0")
    
    return MerchantStatsOut(
        total=total,
        active=active,
        pending=pending,
        total_volume_usd=total_volume_usd
    )


@router.post(
    "",
    response_model=MerchantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать нового мерчанта",
)
async def create_merchant(payload: MerchantCreateRequest, db: DbSession) -> MerchantOut:
    """
    Создаёт нового мерчанта (обычно в статусе PENDING).
    """
    api_key = secrets.token_urlsafe(32)

    merchant = Merchant(
        id=uuid.uuid4(),
        api_key=api_key,
        name=payload.name,
        email=payload.email,
        note=payload.note,
        commission_pcent=payload.commission_pcent,
        webhook_url=str(payload.webhook_url) if payload.webhook_url else None,
        status=MerchantStatus.PENDING,
    )
    db.add(merchant)

    # Автоматически создаём балансы для всех активных монет
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
        "Admin: created merchant name=%s email=%s id=%s status=PENDING",
        merchant.name, merchant.email, merchant.id,
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
    summary="Список мерчантов с поиском и фильтрацией",
)
async def list_merchants(
    db: DbSession,
    limit: int = 50,
    offset: int = 0,
    search: str | None = Query(None, description="Поиск по ID, Key, Name или Email"),
    status: MerchantStatus | None = Query(None, description="Фильтр по статусу"),
) -> list[MerchantListItem]:
    """Пагинированный список мерчантов с оборотами."""
    from core.models import SystemFeeLog
    
    # Подзапрос для подсчета оборота и кол-ва транзакций
    stats_subq = (
        select(
            SystemFeeLog.merchant_id,
            func.sum(SystemFeeLog.gross_amount_usd).label("volume_usd"),
            func.count(SystemFeeLog.id).label("tx_count")
        )
        .group_by(SystemFeeLog.merchant_id)
        .subquery()
    )
    
    query = (
        select(Merchant, stats_subq.c.volume_usd, stats_subq.c.tx_count)
        .outerjoin(stats_subq, Merchant.id == stats_subq.c.merchant_id)
        .order_by(Merchant.created_at.desc())
    )
    
    if status:
        query = query.where(Merchant.status == status)
    
    if search:
        query = query.where(
            (Merchant.api_key.ilike(f"%{search}%")) |
            (Merchant.name.ilike(f"%{search}%")) |
            (Merchant.email.ilike(f"%{search}%")) |
            (sa.cast(Merchant.id, sa.String).ilike(f"%{search}%"))
        )

    result = await db.execute(query.limit(limit).offset(offset))
    rows = result.all()
    
    return [
        MerchantListItem(
            id=m.id,
            api_key=m.api_key,
            name=m.name,
            email=m.email,
            commission_pcent=m.commission_pcent,
            webhook_url=m.webhook_url,
            status=m.status,
            tier=m.tier,
            volume_usd=vol or Decimal("0"),
            transaction_count=cnt or 0,
            created_at=m.created_at.isoformat(),
        )
        for m, vol, cnt in rows
    ]


@router.get(
    "/{merchant_id}",
    response_model=MerchantOut,
    summary="Детальная информация о мерчанте",
)
async def get_merchant(merchant_id: uuid.UUID, db: DbSession) -> MerchantOut:
    from core.models import SystemFeeLog
    merchant = await _get_merchant_or_404(merchant_id, db)

    # 1. Считаем балансы
    balances_result = await db.execute(
        select(Balance, Coin).join(Coin, Balance.coin_id == Coin.id)
        .where(Balance.merchant_id == merchant_id)
    )
    balance_rows = balances_result.all()
    balances = [row[0] for row in balance_rows]
    coins_map = {row[1].id: row[1].symbol for row in balance_rows}
    
    # 2. Считаем обороты
    stats_result = await db.execute(
        select(
            func.sum(SystemFeeLog.gross_amount_usd),
            func.count(SystemFeeLog.id)
        ).where(SystemFeeLog.merchant_id == merchant_id)
    )
    vol, cnt = stats_result.one()

    return _merchant_to_out(
        merchant, 
        balances, 
        coins_map, 
        volume_usd=vol or Decimal("0"), 
        tx_count=cnt or 0
    )


@router.patch(
    "/{merchant_id}/status",
    response_model=MerchantOut,
    summary="Смена статуса мерчанта (модерация)",
)
async def update_merchant_status(
    merchant_id: uuid.UUID,
    payload: MerchantStatusRequest,
    db: DbSession,
) -> MerchantOut:
    """Одобрение регистрации или блокировка за нарушения."""
    merchant = await _get_merchant_or_404(merchant_id, db)
    merchant.status = payload.status
    await db.flush()
    
    logger.info("Admin: updated merchant id=%s status to %s", merchant_id, payload.status)
    return await get_merchant(merchant_id, db)


@router.patch(
    "/{merchant_id}/config",
    response_model=MerchantOut,
    summary="Изменение комиссий и тарифного плана",
)
async def update_merchant_config(
    merchant_id: uuid.UUID,
    payload: MerchantConfigRequest,
    db: DbSession,
) -> MerchantOut:
    """Изменение комиссии для конкретного бизнеса."""
    merchant = await _get_merchant_or_404(merchant_id, db)
    merchant.commission_pcent = payload.commission_pcent
    if payload.tier:
        merchant.tier = payload.tier.upper()
    
    await db.flush()
    logger.info("Admin: updated merchant id=%s config", merchant_id)
    return await get_merchant(merchant_id, db)


@router.patch(
    "/{merchant_id}",
    response_model=MerchantOut,
    summary="Обновить данные мерчанта",
)
async def update_merchant(
    merchant_id: uuid.UUID,
    payload: MerchantUpdateRequest,
    db: DbSession,
) -> MerchantOut:
    """Общий эндпоинт для редактирования мерчанта."""
    merchant = await _get_merchant_or_404(merchant_id, db)
    
    if payload.name is not None:
        merchant.name = payload.name
    if payload.email is not None:
        merchant.email = payload.email
    if payload.commission_pcent is not None:
        merchant.commission_pcent = payload.commission_pcent
    if payload.webhook_url is not None:
        merchant.webhook_url = payload.webhook_url
    if payload.note is not None:
        merchant.note = payload.note
    if payload.status is not None:
        merchant.status = payload.status
    if payload.tier is not None:
        merchant.tier = payload.tier.upper()

    await db.flush()
    logger.info("Admin: updated merchant id=%s", merchant_id)
    return await get_merchant(merchant_id, db)


@router.post(
    "/{merchant_id}/rotate-key",
    response_model=RotateKeyResponse,
    summary="Сгенерировать новый API ключ",
)
async def rotate_api_key(merchant_id: uuid.UUID, db: DbSession) -> RotateKeyResponse:
    """
    Генерирует новый api_key и **немедленно инвалидирует старый**.
    """
    merchant = await _get_merchant_or_404(merchant_id, db)
    new_key = secrets.token_urlsafe(32)
    merchant.api_key = new_key
    await db.flush()

    logger.warning("Admin: rotated api_key for merchant id=%s", merchant_id)
    return RotateKeyResponse(merchant_id=merchant_id, new_api_key=new_key)
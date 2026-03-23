from datetime import datetime, timedelta
from decimal import Decimal
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from typing import List

from core.database import DbSession
from core.models import SystemFeeLog, Coin
from services.api.admin_auth import require_admin

router = APIRouter(
    prefix="/charts",
    tags=["admin-charts"],
    dependencies=[Depends(require_admin)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class VolumePoint(BaseModel):
    label: str  # e.g., "14:00" or "2024-03-22"
    volume: float
    transactions: int

class PaymentMethodPoint(BaseModel):
    name: str # e.g., "BTC"
    value_usd: float
    percentage: float

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/volume", response_model=List[VolumePoint], summary="График объема транзакций")
async def get_volume_chart(
    db: DbSession,
    period: str = Query("day", enum=["day", "week", "month"])
) -> List[VolumePoint]:
    """
    Возвращает данные для графика объема транзакций за выбранный период.
    - day: последние 24 часа (почасово)
    - week: последние 7 дней (посуточно)
    - month: последние 30 дней (посуточно)
    """
    now = datetime.utcnow()
    
    if period == "day":
        start_time = now - timedelta(hours=23)
        start_time = start_time.replace(minute=0, second=0, microsecond=0)
        trunc_level = "hour"
        date_format = "%H:00"
        delta = timedelta(hours=1)
        steps = 24
    elif period == "week":
        start_time = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        trunc_level = "day"
        date_format = "%Y-%m-%d"
        delta = timedelta(days=1)
        steps = 7
    elif period == "month":
        start_time = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        trunc_level = "day"
        date_format = "%Y-%m-%d"
        delta = timedelta(days=1)
        steps = 30
    else:
        # Fallback to day
        start_time = now - timedelta(hours=23)
        start_time = start_time.replace(minute=0, second=0, microsecond=0)
        trunc_level = "hour"
        date_format = "%H:00"
        delta = timedelta(hours=1)
        steps = 24
    
    # Query aggregated data
    time_series = func.date_trunc(trunc_level, SystemFeeLog.created_at)
    query = (
        select(
            time_series.label("time_bin"),
            func.sum(SystemFeeLog.gross_amount_usd).label("volume"),
            func.count(SystemFeeLog.id).label("transactions")
        )
        .where(SystemFeeLog.created_at >= start_time)
        .group_by(time_series)
        .order_by(time_series)
    )
    
    result = await db.execute(query)
    rows = result.all()
    
    # Map results by formatted date string
    data_map = {row.time_bin.strftime(date_format): row for row in rows}
    
    final_points = []
    for i in range(steps):
        target_time = start_time + (delta * i)
        ts_str = target_time.strftime(date_format)
        
        if ts_str in data_map:
            row = data_map[ts_str]
            final_points.append(VolumePoint(
                label=ts_str,
                volume=float(row.volume or 0),
                transactions=int(row.transactions or 0)
            ))
        else:
            final_points.append(VolumePoint(label=ts_str, volume=0.0, transactions=0))
            
    return final_points


@router.get("/payment-methods", response_model=List[PaymentMethodPoint], summary="Распределение по монетам")
async def get_payment_methods_chart(db: DbSession) -> List[PaymentMethodPoint]:
    """
    Разбивка общего объема по криптовалютам для круговой диаграммы.
    """
    # Total volume
    total_vol_query = select(func.sum(SystemFeeLog.gross_amount_usd))
    total_vol_res = await db.execute(total_vol_query)
    total_volume = float(total_vol_res.scalar() or 0)
    
    # By coin
    query = (
        select(
            Coin.symbol,
            func.sum(SystemFeeLog.gross_amount_usd).label("value_usd")
        )
        .join(Coin, SystemFeeLog.coin_id == Coin.id)
        .group_by(Coin.symbol)
        .order_by(func.sum(SystemFeeLog.gross_amount_usd).desc())
    )
    
    result = await db.execute(query)
    rows = result.all()
    
    return [
        PaymentMethodPoint(
            name=row.symbol,
            value_usd=float(row.value_usd or 0),
            percentage=round((float(row.value_usd) / total_volume * 100), 1) if total_volume > 0 else 0.0
        )
        for row in rows
    ]

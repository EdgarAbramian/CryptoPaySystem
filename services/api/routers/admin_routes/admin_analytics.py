import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, distinct

from core.database import DbSession
from core.models import Invoice, InvoiceStatus, Merchant, SystemFeeLog, Transaction
from services.api.admin_auth import require_admin

router = APIRouter(
    prefix="/analytics",
    tags=["admin-analytics"],
    dependencies=[Depends(require_admin)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class KPIPoint(BaseModel):
    current: float
    previous: float
    change_pcent: float

class AnalyticsOverviewOut(BaseModel):
    total_volume: KPIPoint
    avg_transaction_value: KPIPoint
    conversion_rate: KPIPoint
    active_merchants: KPIPoint

class HourlyVolumePoint(BaseModel):
    hour: str
    volume: float
    transactions: int

class PaymentMethodPoint(BaseModel):
    name: str
    value_usd: float
    percentage: float

class GeoDistributionPoint(BaseModel):
    name: str
    code: str
    volume: float
    percentage: float
    growth: float

# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

def calculate_change(current: float, previous: float) -> float:
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return round(((current - previous) / previous) * 100, 1)

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/overview", response_model=AnalyticsOverviewOut, summary="KPI за последние 30 дней")
async def get_analytics_overview(db: DbSession) -> AnalyticsOverviewOut:
    """
    Сравнивает показатели за последние 30 дней с предыдущими 30 днями.
    """
    now = datetime.utcnow()
    t1_end = now
    t1_start = now - timedelta(days=30)
    t2_end = t1_start
    t2_start = t1_start - timedelta(days=30)

    async def get_period_metrics(start: datetime, end: datetime):
        # 1. Volume
        vol_query = select(func.sum(SystemFeeLog.gross_amount_usd)).where(
            SystemFeeLog.created_at >= start,
            SystemFeeLog.created_at < end
        )
        vol_res = await db.execute(vol_query)
        volume = float(vol_res.scalar() or 0)

        # 2. Avg Transaction Value
        avg_query = select(func.avg(SystemFeeLog.gross_amount_usd)).where(
            SystemFeeLog.created_at >= start,
            SystemFeeLog.created_at < end
        )
        avg_res = await db.execute(avg_query)
        avg_val = float(avg_res.scalar() or 0)

        # 3. Conversion Rate (PAID Invoices / Total Invoices)
        total_inv_query = select(func.count(Invoice.id)).where(
            Invoice.created_at >= start,
            Invoice.created_at < end
        )
        total_inv_res = await db.execute(total_inv_query)
        total_inv = total_inv_res.scalar() or 0

        paid_inv_query = select(func.count(Invoice.id)).where(
            Invoice.created_at >= start,
            Invoice.created_at < end,
            Invoice.status == InvoiceStatus.PAID
        )
        paid_inv_res = await db.execute(paid_inv_query)
        paid_inv = paid_inv_res.scalar() or 0
        conv_rate = (paid_inv / total_inv * 100) if total_inv > 0 else 0.0

        # 4. Active Merchants
        active_m_query = select(func.count(distinct(Invoice.merchant_id))).where(
            Invoice.created_at >= start,
            Invoice.created_at < end
        )
        active_m_res = await db.execute(active_m_query)
        active_m = active_m_res.scalar() or 0

        return volume, avg_val, conv_rate, active_m

    v1, a1, c1, m1 = await get_period_metrics(t1_start, t1_end)
    v2, a2, c2, m2 = await get_period_metrics(t2_start, t2_end)

    return AnalyticsOverviewOut(
        total_volume=KPIPoint(current=v1, previous=v2, change_pcent=calculate_change(v1, v2)),
        avg_transaction_value=KPIPoint(current=a1, previous=a2, change_pcent=calculate_change(a1, a2)),
        conversion_rate=KPIPoint(current=c1, previous=c2, change_pcent=calculate_change(c1, c2)),
        active_merchants=KPIPoint(current=m1, previous=m2, change_pcent=calculate_change(m1, m2)),
    )


@router.get("/volume-hourly", response_model=list[HourlyVolumePoint], summary="Распределение оборота за 24 часа")
async def get_volume_hourly(db: DbSession) -> list[HourlyVolumePoint]:
    """
    Возвращает объем и количество транзакций за каждый из последних 24 часов.
    """
    start_time = datetime.utcnow() - timedelta(hours=23)
    start_time = start_time.replace(minute=0, second=0, microsecond=0)

    # Note: date_trunc is Postgres specific. 
    # For SQLite we would use strftime but assuming Postgres as per previous logs.
    hour_series = func.date_trunc('hour', SystemFeeLog.created_at)
    query = (
        select(
            hour_series.label("hour"),
            func.sum(SystemFeeLog.gross_amount_usd).label("volume"),
            func.count(SystemFeeLog.id).label("transactions")
        )
        .where(SystemFeeLog.created_at >= start_time)
        .group_by(hour_series)
        .order_by(hour_series)
    )
    result = await db.execute(query)
    rows = result.all()

    # Fill gaps for hours with no data
    data_map = {row.hour.strftime("%H:00"): row for row in rows}
    final_points = []
    for i in range(24):
        target_time = start_time + timedelta(hours=i)
        ts_str = target_time.strftime("%H:00")
        if ts_str in data_map:
            row = data_map[ts_str]
            final_points.append(HourlyVolumePoint(hour=ts_str, volume=float(row.volume), transactions=row.transactions))
        else:
            final_points.append(HourlyVolumePoint(hour=ts_str, volume=0.0, transactions=0))

    return final_points


@router.get("/payment-methods", response_model=list[PaymentMethodPoint], summary="Распределение по методам оплаты")
async def get_payment_methods(db: DbSession) -> list[PaymentMethodPoint]:
    """
    Разбивка общего объема по криптовалютам.
    """
    from core.models import Coin
    total_vol_query = select(func.sum(SystemFeeLog.gross_amount_usd))
    total_vol_res = await db.execute(total_vol_query)
    total_volume = float(total_vol_res.scalar() or 0)

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
            value_usd=float(row.value_usd),
            percentage=round((float(row.value_usd) / total_volume * 100), 1) if total_volume > 0 else 0.0
        )
        for row in rows
    ]


@router.get("/geographic", response_model=list[GeoDistributionPoint], summary="Географическое распределение")
async def get_geographic_stats(db: DbSession) -> list[GeoDistributionPoint]:
    """
    Разбивка объема по странам на основе country_code в Invoice.
    """
    COUNTRY_NAMES = {
        "US": "United States", "DE": "Germany", "GB": "United Kingdom",
        "FR": "France", "ES": "Spain", "IT": "Italy", "CA": "Canada",
        "RU": "Russia", "CN": "China", "JP": "Japan"
    }

    # Total volume for percentage calculation
    total_vol_query = select(func.sum(SystemFeeLog.gross_amount_usd))
    total_vol_res = await db.execute(total_vol_query)
    total_volume = float(total_vol_res.scalar() or 0)

    # Current period (30 days) volume by country
    now = datetime.utcnow()
    t1_start = now - timedelta(days=30)
    
    query = (
        select(
            Invoice.country_code,
            func.sum(SystemFeeLog.gross_amount_usd).label("volume")
        )
        .join(Transaction, Transaction.invoice_id == Invoice.id)
        .join(SystemFeeLog, SystemFeeLog.transaction_id == Transaction.id)
        .where(SystemFeeLog.created_at >= t1_start)
        .group_by(Invoice.country_code)
        .order_by(func.sum(SystemFeeLog.gross_amount_usd).desc())
    )
    result = await db.execute(query)
    rows = result.all()

    return [
        GeoDistributionPoint(
            name=COUNTRY_NAMES.get(row.country_code, "Unknown") if row.country_code else "Other",
            code=row.country_code or "OTH",
            volume=float(row.volume),
            percentage=round((float(row.volume) / total_volume * 100), 1) if total_volume > 0 else 0.0,
            growth=0.0 # Growth calculation can be added later if needed
        )
        for row in rows
    ]

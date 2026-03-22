from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from core.database import DbSession
from core.models import Invoice, InvoiceStatus, Merchant, MerchantStatus, SystemFeeLog, Transaction
from services.api.admin_auth import require_admin

router = APIRouter(
    prefix="",
    tags=["admin-stats"],
    dependencies=[Depends(require_admin)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

from pydantic import BaseModel

class CoinBreakdown(BaseModel):
    revenue: Decimal
    volume: Decimal


class AdminStatsOut(BaseModel):
    total_revenue: Decimal
    transaction_volume: Decimal
    active_merchants: int
    success_rate: float
    pending_merchants: int
    total_transactions: int
    today_transactions: int
    active_nodes: int
    breakdown: dict[str, CoinBreakdown]


class RevenueChartPoint(BaseModel):
    date: str
    revenue: Decimal


class RevenueChartOut(BaseModel):
    data: list[RevenueChartPoint]


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=AdminStatsOut, summary="Глобальная аналитика и KPI")
async def get_admin_stats(db: DbSession) -> AdminStatsOut:
    """
    Возвращает основные метрики платформы.
    """
    # 1. Total Revenue USD (sum of fees in USD)
    revenue_result = await db.execute(select(func.sum(SystemFeeLog.fee_amount_usd)))
    total_revenue_usd = revenue_result.scalar() or Decimal("0")

    # 2. Active Merchants
    active_result = await db.execute(
        select(func.count(Merchant.id)).where(Merchant.status == MerchantStatus.ACTIVE)
    )
    active_merchants = active_result.scalar() or 0

    # 3. Transaction Volume USD (gross amount in USD)
    volume_result = await db.execute(select(func.sum(SystemFeeLog.gross_amount_usd)))
    transaction_volume_usd = volume_result.scalar() or Decimal("0")

    # 4. Success Rate (PAID invoices / Total invoices)
    total_invoices_result = await db.execute(select(func.count(Invoice.id)))
    total_invoices = total_invoices_result.scalar() or 0
    
    paid_invoices_result = await db.execute(
        select(func.count(Invoice.id)).where(Invoice.status == InvoiceStatus.PAID)
    )
    paid_invoices = paid_invoices_result.scalar() or 0

    success_rate = (paid_invoices / total_invoices * 100) if total_invoices > 0 else 0.0

    # 5. Pending Merchants
    pending_result = await db.execute(
        select(func.count(Merchant.id)).where(Merchant.status == MerchantStatus.PENDING)
    )
    pending_merchants = pending_result.scalar() or 0

    # 6. Total Transactions
    total_tx_result = await db.execute(select(func.count(Transaction.id)))
    total_transactions = total_tx_result.scalar() or 0

    # 6.5. Today Transactions
    now = datetime.utcnow()
    midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_tx_result = await db.execute(
        select(func.count(Transaction.id)).where(Transaction.detected_at >= midnight_today)
    )
    today_transactions = today_tx_result.scalar() or 0

    # 7. Active Nodes (Count synced providers)
    from providers.registry import registry
    import logging
    logger = logging.getLogger(__name__)
    active_nodes = 0
    # For MVP, we only check providers that have a ping method
    for coin_sym in list(registry.all().keys()):
        try:
            provider = registry.get(coin_sym)
            if hasattr(provider, "ping") and await provider.ping():
                active_nodes += 1
        except Exception as e:
            logger.warning(f"Failed to ping node {coin_sym}: {e}")
            continue

    # 8. Breakdown per Coin (Real amounts, not USD)
    from core.models import Coin
    breakdown_query = (
        select(
            Coin.symbol,
            func.sum(SystemFeeLog.fee_amount).label("revenue"),
            func.sum(SystemFeeLog.gross_amount).label("volume")
        )
        .join(Coin, SystemFeeLog.coin_id == Coin.id)
        .group_by(Coin.symbol)
    )
    breakdown_result = await db.execute(breakdown_query)
    breakdown = {
        row.symbol: CoinBreakdown(revenue=row.revenue, volume=row.volume)
        for row in breakdown_result
    }

    return AdminStatsOut(
        total_revenue=total_revenue_usd,
        transaction_volume=transaction_volume_usd,
        active_merchants=active_merchants,
        success_rate=round(success_rate, 2),
        pending_merchants=pending_merchants,
        total_transactions=total_transactions,
        today_transactions=today_transactions,
        active_nodes=active_nodes,
        breakdown=breakdown,
    )


@router.get("/charts/revenue", response_model=RevenueChartOut, summary="Данные для графиков выручки")
async def get_revenue_chart(
    db: DbSession,
    period: str = Query("month", enum=["day", "week", "month"]),
) -> RevenueChartOut:
    """
    Данные для графиков выручки с фильтрацией по времени (в USD).
    """
    now = datetime.utcnow()
    if period == "day":
        start_date = now - timedelta(days=1)
        group_by = func.date_trunc('hour', SystemFeeLog.created_at)
    elif period == "week":
        start_date = now - timedelta(weeks=1)
        group_by = func.date_trunc('day', SystemFeeLog.created_at)
    else:  # month
        start_date = now - timedelta(days=30)
        group_by = func.date_trunc('day', SystemFeeLog.created_at)

    result = await db.execute(
        select(group_by.label("date"), func.sum(SystemFeeLog.fee_amount_usd).label("revenue_usd"))
        .where(SystemFeeLog.created_at >= start_date)
        .group_by(group_by)
        .order_by(group_by)
    )
    
    rows = result.all()
    data = [
        RevenueChartPoint(date=row.date.isoformat(), revenue=row.revenue_usd or Decimal("0"))
        for row in rows
    ]

    return RevenueChartOut(data=data)

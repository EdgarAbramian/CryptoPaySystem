import logging
import secrets
import io
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, AnyHttpUrl
from sqlalchemy import func, select, case
from sqlalchemy.orm import joinedload

from core.database import DbSession
from core.models import Balance, Coin, Merchant, Invoice, Transaction, InvoiceStatus, SystemFeeLog
from core.config import settings
from services.api.admin_auth import require_merchant
from services.price.service import PriceService

logger = logging.getLogger(__name__)

CurrentMerchant = Annotated[Merchant, Depends(require_merchant)]

router = APIRouter(prefix="/merchant", tags=["merchant"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class BalanceOut(BaseModel):
    coin_symbol: str
    amount_available: Decimal
    amount_locked: Decimal


class CoinOutWithRate(BaseModel):
    id: int
    symbol: str
    name: str
    rate_usd: Decimal


class MerchantProfileOut(BaseModel):
    id: str
    name: str | None
    email: str | None
    commission_pcent: Decimal
    webhook_url: str | None
    status: str
    created_at: str
    balances: list[BalanceOut]


class MerchantUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    webhook_url: AnyHttpUrl | None = Field(
        default=None,
        description="URL для webhook-уведомлений. Передай null чтобы удалить.",
    )


class MerchantStatsOut(BaseModel):
    total_volume_usd: Decimal
    total_transactions: int
    success_rate: float
    balance_available_usd: Decimal
    balance_pending_usd: Decimal


class MerchantInvoiceOut(BaseModel):
    id: str
    address: str
    amount_expected: Decimal
    coin_symbol: str
    status: str
    created_at: str


class MerchantTransactionOut(BaseModel):
    id: str
    txid: str
    amount_received: Decimal
    amount_usd: Decimal
    coin_symbol: str
    fee: Decimal
    fee_usd: Decimal
    status: str
    confirmations: int
    created_at: str


class ReportSummaryOut(BaseModel):
    total_volume_usd: Decimal
    successful_transactions: int
    average_order_value: Decimal
    abandonment_rate: float


class CoinDistributionOut(BaseModel):
    symbol: str
    volume_usd: Decimal
    percentage: float


class GeoStatOut(BaseModel):
    country_code: str
    volume_usd: Decimal
    success_rate: float


class RevenueDataPoint(BaseModel):
    date: str
    revenue: Decimal
    count: int


class RevenueOverviewOut(BaseModel):
    total_revenue_usd: Decimal
    change_percentage: float
    data_points: list[RevenueDataPoint]


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/me",
    # response_model=MerchantProfileOut, # Conflict with recursion in get_profile call if not careful, but fine here
    summary="Профиль и балансы текущего мерчанта",
)
async def get_profile(db: DbSession, merchant: CurrentMerchant):
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
        name=merchant.name,
        email=merchant.email,
        commission_pcent=merchant.commission_pcent,
        webhook_url=merchant.webhook_url,
        status=merchant.status.value,
        created_at=merchant.created_at.isoformat(),
        balances=balances,
    )


@router.patch(
    "/me",
    summary="Обновить настройки профиля",
)
async def update_profile(
    payload: MerchantUpdateRequest,
    db: DbSession,
    merchant: CurrentMerchant,
):
    """
    Мерчант может самостоятельно обновить имя и webhook_url.
    """
    if payload.name is not None:
        merchant.name = payload.name
    if payload.webhook_url is not None:
        merchant.webhook_url = str(payload.webhook_url)
    
    await db.commit()
    return await get_profile(db, merchant)


@router.post("/me/rotate-key", summary="Сгенерировать новый API Key")
async def rotate_api_key(db: DbSession, merchant: CurrentMerchant):
    """Генерирует новый API Key для внешних интеграций."""
    new_key = f"sk_live_{secrets.token_urlsafe(32)}"
    merchant.api_key = new_key
    await db.commit()
    return {"api_key": new_key}


@router.get("/stats", response_model=MerchantStatsOut, summary="Статистика мерчанта")
async def get_merchant_stats(db: DbSession, merchant: CurrentMerchant):
    """Оборот, количество транзакций и успех оплат только для этого мерчанта."""
    # Volume and Tx Count
    result = await db.execute(
        select(
            func.sum(Transaction.amount_usd),
            func.count(Transaction.id)
        ).join(Invoice, Transaction.invoice_id == Invoice.id)
        .where(Invoice.merchant_id == merchant.id)
    )
    volume, total_tx = result.one()
    
    # Success Rate based on Invoices
    inv_total_res = await db.execute(
        select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id)
    )
    inv_total = inv_total_res.scalar() or 0
    
    inv_paid_res = await db.execute(
        select(func.count(Invoice.id))
        .where(Invoice.merchant_id == merchant.id, Invoice.status == InvoiceStatus.PAID)
    )
    inv_paid = inv_paid_res.scalar() or 0
    
    success_rate = (inv_paid / inv_total * 100) if inv_total > 0 else 0.0
    
    # Balance calculations (USD)
    bal_res = await db.execute(
        select(Balance, Coin.symbol)
        .join(Coin, Balance.coin_id == Coin.id)
        .where(Balance.merchant_id == merchant.id)
    )
    balances = bal_res.all()
    
    available_usd = Decimal("0")
    pending_usd = Decimal("0")
    
    for bal, symbol in balances:
        rate = PriceService.get_rate(symbol)
        available_usd += bal.amount_available * rate
        pending_usd += bal.amount_locked * rate

    return MerchantStatsOut(
        total_volume_usd=volume or Decimal("0.00"),
        total_transactions=total_tx or 0,
        success_rate=round(success_rate, 2),
        balance_available_usd=available_usd.quantize(Decimal("0.01")),
        balance_pending_usd=pending_usd.quantize(Decimal("0.01"))
    )


@router.get("/transactions", response_model=list[MerchantTransactionOut], summary="Список транзакций мерчанта")
async def list_merchant_transactions(
    db: DbSession, 
    merchant: CurrentMerchant,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    coin_id: int | None = Query(None),
    status: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    min_amount_usd: float | None = Query(None),
    max_amount_usd: float | None = Query(None),
    search: str | None = Query(None),
):
    query = (
        select(Transaction, Coin.symbol, SystemFeeLog)
        .join(Invoice, Transaction.invoice_id == Invoice.id)
        .join(Coin, Invoice.coin_id == Coin.id)
        .outerjoin(SystemFeeLog, Transaction.id == SystemFeeLog.transaction_id)
        .options(joinedload(Transaction.invoice))
        .where(Invoice.merchant_id == merchant.id)
        .order_by(Transaction.detected_at.desc())
    )

    # Filters
    if coin_id:
        query = query.where(Invoice.coin_id == coin_id)
    if date_from:
        query = query.where(Transaction.detected_at >= date_from)
    if date_to:
        query = query.where(Transaction.detected_at <= date_to)
    if min_amount_usd:
        query = query.where(Transaction.amount_usd >= min_amount_usd)
    if max_amount_usd:
        query = query.where(Transaction.amount_usd <= max_amount_usd)
    
    if search:
        query = query.where(
            (Transaction.txid.ilike(f"%{search}%")) |
            (Invoice.address.ilike(f"%{search}%")) |
            (Invoice.customer_email.ilike(f"%{search}%"))
        )

    if status and status != "all":
        if status in ["completed", "PAID"]:
            query = query.where(
                (Transaction.confirmations >= settings.ledger_required_confirmations) |
                (Invoice.status == InvoiceStatus.PAID)
            )
        elif status in ["pending", "PENDING", "PARTIAL", "NEW"]:
            query = query.where(
                Transaction.confirmations < settings.ledger_required_confirmations,
                Invoice.status != InvoiceStatus.PAID,
                Invoice.status != InvoiceStatus.FAILED,
                Invoice.status != InvoiceStatus.EXPIRED
            )
        elif status in ["failed", "FAILED"]:
            query = query.where(Invoice.status == InvoiceStatus.FAILED)

    result = await db.execute(query.limit(limit).offset(offset))
    return [
        MerchantTransactionOut(
            id=str(tx.id),
            txid=tx.txid,
            amount_received=tx.amount_received,
            amount_usd=tx.amount_usd if tx.amount_usd is not None else PriceService.to_usd(tx.amount_received, symbol),
            coin_symbol=symbol,
            fee=sfl.fee_amount if sfl else Decimal("0"),
            fee_usd=sfl.fee_amount_usd if (sfl and sfl.fee_amount_usd is not None) else (PriceService.to_usd(sfl.fee_amount, symbol) if sfl else Decimal("0")),
            status="completed" if (tx.confirmations >= settings.ledger_required_confirmations or tx.invoice.status == InvoiceStatus.PAID) else ("failed" if tx.invoice.status == InvoiceStatus.FAILED else "pending"),
            confirmations=tx.confirmations,
            created_at=tx.detected_at.isoformat()
        )
        for tx, symbol, sfl in result.all()
    ]


@router.get("/transactions/export", summary="Экспорт транзакций мерчанта (CSV/XLSX)")
async def export_merchant_transactions(
    db: DbSession,
    merchant: CurrentMerchant,
    format: str = Query("csv", description="Формат: csv или xlsx"),
    coin_id: int | None = Query(None),
    status: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    min_amount_usd: float | None = Query(None),
    max_amount_usd: float | None = Query(None),
    search: str | None = Query(None),
):
    query = (
        select(Transaction, Coin.symbol, SystemFeeLog, Invoice)
        .join(Invoice, Transaction.invoice_id == Invoice.id)
        .join(Coin, Invoice.coin_id == Coin.id)
        .outerjoin(SystemFeeLog, Transaction.id == SystemFeeLog.transaction_id)
        .where(Invoice.merchant_id == merchant.id)
        .order_by(Transaction.detected_at.desc())
    )

    # Filters
    if coin_id:
        query = query.where(Invoice.coin_id == coin_id)
    if date_from:
        query = query.where(Transaction.detected_at >= date_from)
    if date_to:
        query = query.where(Transaction.detected_at <= date_to)
    if min_amount_usd:
        query = query.where(Transaction.amount_usd >= min_amount_usd)
    if max_amount_usd:
        query = query.where(Transaction.amount_usd <= max_amount_usd)
    
    if search:
        query = query.where(
            (Transaction.txid.ilike(f"%{search}%")) |
            (Invoice.address.ilike(f"%{search}%")) |
            (Invoice.customer_email.ilike(f"%{search}%"))
        )

    if status and status != "all":
        if status in ["completed", "PAID"]:
            query = query.where(
                (Transaction.confirmations >= settings.ledger_required_confirmations) |
                (Invoice.status == InvoiceStatus.PAID)
            )
        elif status in ["pending", "PENDING", "PARTIAL", "NEW"]:
            query = query.where(
                Transaction.confirmations < settings.ledger_required_confirmations,
                Invoice.status != InvoiceStatus.PAID,
                Invoice.status != InvoiceStatus.FAILED,
                Invoice.status != InvoiceStatus.EXPIRED
            )
        elif status in ["failed", "FAILED"]:
            query = query.where(Invoice.status == InvoiceStatus.FAILED)

    result = await db.execute(query)
    rows = result.all()

    filename = f"merchant_transactions_{datetime.utcnow().strftime('%Y-%m-%d')}"

    if format == "xlsx":
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            headers = ["ID", "TXID", "Amount", "USD", "Coin", "Fee", "Fee USD", "Status", "Address", "Customer", "Created At"]
            ws.append(headers)
            for tx, symbol, sfl, inv in rows:
                tx_status = "completed" if (tx.confirmations >= settings.ledger_required_confirmations or inv.status == InvoiceStatus.PAID) else ("failed" if inv.status == InvoiceStatus.FAILED else "pending")
                ws.append([
                    str(tx.id), tx.txid, float(tx.amount_received),
                    float(tx.amount_usd if tx.amount_usd is not None else PriceService.to_usd(tx.amount_received, symbol)), 
                    symbol,
                    float(sfl.fee_amount if sfl else 0),
                    float(sfl.fee_amount_usd if (sfl and sfl.fee_amount_usd is not None) else (PriceService.to_usd(sfl.fee_amount, symbol) if sfl else 0)),
                    tx_status, inv.address, inv.customer_email or "N/A",
                    tx.detected_at.strftime("%Y-%m-%d %H:%M:%S")
                ])
                
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            return Response(
                content=output.getvalue(),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={filename}.xlsx"}
            )
        except ImportError:
            pass

    # Default: CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "TXID", "Amount", "USD", "Coin", "Fee", "Fee USD", "Status", "Address", "Customer", "Created At"])

    for tx, symbol, sfl, inv in rows:
        tx_status = "completed" if (tx.confirmations >= settings.ledger_required_confirmations or inv.status == InvoiceStatus.PAID) else ("failed" if inv.status == InvoiceStatus.FAILED else "pending")
        writer.writerow([
            str(tx.id), tx.txid, str(tx.amount_received),
            str(tx.amount_usd if tx.amount_usd is not None else PriceService.to_usd(tx.amount_received, symbol)),
            symbol,
            str(sfl.fee_amount if sfl else 0),
            str(sfl.fee_amount_usd if (sfl and sfl.fee_amount_usd is not None) else (PriceService.to_usd(sfl.fee_amount, symbol) if sfl else 0)),
            tx_status, inv.address, inv.customer_email or "N/A",
            tx.detected_at.isoformat()
        ])

    output.seek(0)
    data = output.getvalue()
    
    return Response(
        content=data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
    )


@router.get("/invoices", response_model=list[MerchantInvoiceOut], summary="Список инвойсов мерчанта")
async def list_merchant_invoices(
    db: DbSession, 
    merchant: CurrentMerchant,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    query = (
        select(Invoice, Coin.symbol)
        .join(Coin, Invoice.coin_id == Coin.id)
        .where(Invoice.merchant_id == merchant.id)
        .order_by(Invoice.created_at.desc())
        .limit(limit).offset(offset)
    )
    result = await db.execute(query)
    return [
        MerchantInvoiceOut(
            id=str(inv.id),
            address=inv.address,
            amount_expected=inv.amount_expected,
            coin_symbol=symbol,
            status=inv.status.value,
            created_at=inv.created_at.isoformat()
        )
        for inv, symbol in result.all()
    ]


@router.get("/coins", response_model=list[CoinOutWithRate], summary="Список доступных монет для мерчанта")
async def list_available_coins(db: DbSession, merchant: CurrentMerchant):
    """Возвращает список активных монет с их текущим курсом к USD."""
    result = await db.execute(select(Coin).where(Coin.is_active == True))
    coins = result.scalars().all()
    
    return [
        CoinOutWithRate(
            id=c.id,
            symbol=c.symbol,
            name=c.name,
            rate_usd=PriceService.get_rate(c.symbol)
        )
        for c in coins
    ]

@router.get("/reports/summary", response_model=ReportSummaryOut, summary="Общая статистика за период")
async def get_report_summary(
    db: DbSession,
    merchant: CurrentMerchant,
    start_date: datetime = Query(default=None),
    end_date: datetime = Query(default=None),
):
    """Возвращает сводные показатели: объем, кол-во успешных транзакций, средний чек и % брошенных корзин."""
    stmt = select(Invoice).where(Invoice.merchant_id == merchant.id)
    if start_date:
        stmt = stmt.where(Invoice.created_at >= start_date)
    if end_date:
        stmt = stmt.where(Invoice.created_at <= end_date)
    
    result = await db.execute(stmt)
    invoices = result.scalars().all()
    
    paid_invoices = [inv for inv in invoices if inv.status == InvoiceStatus.PAID]
    expired_invoices = [inv for inv in invoices if inv.status == InvoiceStatus.EXPIRED]
    failed_invoices = [inv for inv in invoices if inv.status == InvoiceStatus.FAILED]
    
    total_volume_usd = sum(((inv.amount_usd or Decimal("0")) for inv in paid_invoices), Decimal("0"))
    successful_count = len(paid_invoices)
    
    avg_order_value = (total_volume_usd / successful_count).quantize(Decimal("0.01")) if successful_count > 0 else Decimal("0")
    
    # Abandonment rate: Expired / (Paid + Expired + Failed)
    total_attempts = len(paid_invoices) + len(expired_invoices) + len(failed_invoices)
    abandonment_rate = round((len(expired_invoices) / total_attempts * 100), 2) if total_attempts > 0 else 0.0
    
    return ReportSummaryOut(
        total_volume_usd=total_volume_usd,
        successful_transactions=successful_count,
        average_order_value=avg_order_value,
        abandonment_rate=abandonment_rate
    )


@router.get("/reports/coin-distribution", response_model=list[CoinDistributionOut], summary="Распределение по монетам")
async def get_coin_distribution(
    db: DbSession,
    merchant: CurrentMerchant,
    start_date: datetime = Query(default=None),
    end_date: datetime = Query(default=None),
):
    """Возвращает статистику использования разных криптовалют для оплаты."""
    stmt = (
        select(Coin.symbol, func.sum(Invoice.amount_usd))
        .join(Invoice, Invoice.coin_id == Coin.id)
        .where(Invoice.merchant_id == merchant.id, Invoice.status == InvoiceStatus.PAID)
    )
    if start_date:
        stmt = stmt.where(Invoice.created_at >= start_date)
    if end_date:
        stmt = stmt.where(Invoice.created_at <= end_date)
    
    stmt = stmt.group_by(Coin.symbol)
    result = await db.execute(stmt)
    rows = result.all()
    
    total_volume = sum(((row[1] or Decimal("0")) for row in rows), Decimal("0"))
    
    return [
        CoinDistributionOut(
            symbol=row[0],
            volume_usd=(row[1] or Decimal("0")).quantize(Decimal("0.01")),
            percentage=round(float((row[1] or Decimal("0")) / total_volume * 100), 2) if total_volume > 0 else 0.0
        )
        for row in rows
    ]


@router.get("/reports/geo-stats", response_model=list[GeoStatOut], summary="Статистика по странам")
async def get_geo_stats(
    db: DbSession,
    merchant: CurrentMerchant,
    start_date: datetime = Query(default=None),
    end_date: datetime = Query(default=None),
):
    """Группирует данные по коду страны (из инвойса)."""
    stmt = (
        select(
            Invoice.country_code,
            func.sum(case((Invoice.status == InvoiceStatus.PAID, Invoice.amount_usd), else_=0)),
            func.count(case((Invoice.status == InvoiceStatus.PAID, 1))),
            func.count(Invoice.id)
        )
        .where(Invoice.merchant_id == merchant.id)
        .where(Invoice.status.in_([InvoiceStatus.PAID, InvoiceStatus.EXPIRED, InvoiceStatus.FAILED]))
    )
    if start_date:
        stmt = stmt.where(Invoice.created_at >= start_date)
    if end_date:
        stmt = stmt.where(Invoice.created_at <= end_date)
        
    stmt = stmt.group_by(Invoice.country_code)
    result = await db.execute(stmt)
    rows = result.all()
    
    return [
        GeoStatOut(
            country_code=row[0] or "Unknown",
            volume_usd=(row[1] or Decimal("0")).quantize(Decimal("0.01")),
            success_rate=round(float(row[2] / row[3] * 100), 2) if row[3] > 0 else 0.0
        )
        for row in rows
    ]


@router.get("/analytics/revenue", response_model=RevenueOverviewOut, summary="График выручки")
async def get_revenue_analytics(
    db: DbSession,
    merchant: CurrentMerchant,
    period: str = Query("month", regex="^(day|week|month|year)$"),
    coin_id: int = Query(None),
):
    """Данные для графика выручки с группировкой по датам."""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    
    if period == "day":
        start_current = now - timedelta(days=1)
        start_prev = start_current - timedelta(days=1)
        trunc_kind = "hour"
    elif period == "week":
        start_current = now - timedelta(days=7)
        start_prev = start_current - timedelta(days=7)
        trunc_kind = "day"
    elif period == "year":
        start_current = now - timedelta(days=365)
        start_prev = start_current - timedelta(days=365)
        trunc_kind = "month"
    else: # month
        start_current = now - timedelta(days=30)
        start_prev = start_current - timedelta(days=30)
        trunc_kind = "day"

    # 1. Current period data
    stmt = (
        select(
            func.date_trunc(trunc_kind, Invoice.created_at).label("dt"),
            func.sum(Invoice.amount_usd),
            func.count(Invoice.id)
        )
        .where(Invoice.merchant_id == merchant.id, Invoice.status == InvoiceStatus.PAID)
        .where(Invoice.created_at >= start_current)
    )
    if coin_id:
        stmt = stmt.where(Invoice.coin_id == coin_id)
    
    stmt = stmt.group_by("dt").order_by("dt")
    result = await db.execute(stmt)
    rows = result.all()
    
    data_points = [
        RevenueDataPoint(
            date=row[0].strftime("%Y-%m-%d %H:%M" if trunc_kind == "hour" else "%Y-%m-%d"),
            revenue=(row[1] or Decimal("0")).quantize(Decimal("0.01")),
            count=row[2]
        )
        for row in rows
    ]
    
    total_revenue_usd = sum(((row[1] or Decimal("0")) for row in rows), Decimal("0"))
    
    # 2. Previous period volume for growth calculation
    stmt_prev = (
        select(func.sum(Invoice.amount_usd))
        .where(
            Invoice.merchant_id == merchant.id, 
            Invoice.status == InvoiceStatus.PAID,
            Invoice.created_at >= start_prev,
            Invoice.created_at < start_current
        )
    )
    if coin_id:
        stmt_prev = stmt_prev.where(Invoice.coin_id == coin_id)
        
    result_prev = await db.execute(stmt_prev)
    total_revenue_prev = result_prev.scalar() or Decimal("0")
    
    change_percentage = 0.0
    if total_revenue_prev > 0:
        change_percentage = float((total_revenue_usd - total_revenue_prev) / total_revenue_prev * 100)
    elif total_revenue_usd > 0:
        change_percentage = 100.0
        
    return RevenueOverviewOut(
        total_revenue_usd=total_revenue_usd.quantize(Decimal("0.01")),
        change_percentage=round(change_percentage, 2),
        data_points=data_points
    )

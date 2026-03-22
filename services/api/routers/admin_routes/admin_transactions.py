from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from core.database import DbSession
from core.models import Coin, Invoice, Merchant, SystemFeeLog, Transaction, WebhookLog
from services.api.admin_auth import require_admin

from datetime import datetime

def map_tx_status(confirmations: int, invoice_status: str) -> str:
    if invoice_status == "FAILED":
        return "failed"
    return "completed" if confirmations >= 6 else "pending"

def _apply_filters(
    query,
    status: str | None = None,
    merchant_id: uuid.UUID | None = None,
    coin_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    min_amount_usd: float | None = None,
    max_amount_usd: float | None = None,
    search: str | None = None,
):
    if merchant_id:
        query = query.where(Invoice.merchant_id == merchant_id)
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
            (Invoice.customer_email.ilike(f"%{search}%")) |
            (Merchant.name.ilike(f"%{search}%"))
        )

    if status and status != "all":
        # Supports both InvoiceStatus enum values and legacy simplified labels
        if status in ["completed", "PAID"]:
            query = query.where(Transaction.confirmations >= 6)
        elif status in ["pending", "PENDING", "PARTIAL", "NEW"]:
            query = query.where(Transaction.confirmations < 6, Invoice.status != "FAILED")
        elif status in ["failed", "FAILED"]:
            query = query.where(Invoice.status == "FAILED")
        elif status == "EXPIRED":
            query = query.where(Invoice.status == "EXPIRED")
    
    return query

router = APIRouter(
    prefix="/transactions",
    tags=["admin-transactions"],
    dependencies=[Depends(require_admin)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class WebhookLogOut(BaseModel):
    id: uuid.UUID
    url: str
    response_status: int | None
    error_message: str | None
    created_at: str

    model_config = {"from_attributes": True}


class TransactionOut(BaseModel):
    id: uuid.UUID
    txid: str
    merchant_id: uuid.UUID
    merchant_name: str | None
    amount_received: Decimal
    amount_usd: Decimal | None
    coin_symbol: str
    fiat_currency: str = "USD"
    fee: Decimal
    status: str  # completed, pending, failed
    confirmations: int
    created_at: str
    confirmed_at: str | None

    model_config = {"from_attributes": True}


class MerchantInside(BaseModel):
    id: uuid.UUID
    name: str | None


class TransactionDetailOut(TransactionOut):
    merchant: MerchantInside
    customer_email: str | None
    deposit_address: str | None
    amount_expected: Decimal
    webhook_logs: list[WebhookLogOut] = []
    network_info: dict = {}


class RecentTransactionOut(BaseModel):
    id: uuid.UUID
    txid: str
    merchant_name: str | None
    amount_usd: Decimal | None
    status: str
    confirmed_at: str | None


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/recent", response_model=list[RecentTransactionOut], summary="Виджет последних транзакций")
async def get_recent_transactions(
    db: DbSession,
    limit: int = Query(5, ge=1, le=50),
) -> list[RecentTransactionOut]:
    """
    Эндпоинт для виджета "Последние транзакции" на главной странице.
    """
    from core.models import Merchant
    query = (
        select(Transaction, Merchant.name)
        .join(Invoice, Transaction.invoice_id == Invoice.id)
        .join(Merchant, Invoice.merchant_id == Merchant.id)
        .order_by(Transaction.detected_at.desc())
        .limit(limit)
    )
    result = await db.execute(query)
    rows = result.all()
    
    return [
        RecentTransactionOut(
            id=tx.id,
            txid=tx.txid,
            merchant_name=m_name,
            amount_usd=tx.amount_usd,
            status="CONFIRMED" if tx.confirmations >= 6 else "PENDING",
            confirmed_at=tx.confirmed_at.isoformat() if tx.confirmed_at else None,
        )
        for tx, m_name in rows
    ]


@router.get("", response_model=list[TransactionOut], summary="Список всех транзакций")
async def list_transactions(
    db: DbSession,
    limit: int = 50,
    offset: int = 0,
    merchant_id: uuid.UUID | None = Query(None),
    coin_id: int | None = Query(None),
    status: str | None = Query(None, description="Фильтр статуса (completed, pending, failed или PAID, PENDING...)"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    min_amount_usd: float | None = Query(None),
    max_amount_usd: float | None = Query(None),
    search: str | None = Query(None, description="Поиск по TXID, адресу, email или имени мерчанта"),
) -> list[TransactionOut]:
    """
    Глобальный лог всех операций через шлюз с расширенной фильтрацией и пагинацией.
    """
    query = (
        select(Transaction, Invoice, Merchant, Coin, SystemFeeLog)
        .join(Invoice, Transaction.invoice_id == Invoice.id)
        .join(Merchant, Invoice.merchant_id == Merchant.id)
        .join(Coin, Invoice.coin_id == Coin.id)
        .outerjoin(SystemFeeLog, Transaction.id == SystemFeeLog.transaction_id)
        .order_by(Transaction.detected_at.desc())
    )

    query = _apply_filters(
        query=query,
        status=status,
        merchant_id=merchant_id,
        coin_id=coin_id,
        date_from=date_from,
        date_to=date_to,
        min_amount_usd=min_amount_usd,
        max_amount_usd=max_amount_usd,
        search=search
    )

    result = await db.execute(query.limit(limit).offset(offset))
    rows = result.all()

    return [
        TransactionOut(
            id=tx.id,
            txid=tx.txid,
            merchant_id=m.id,
            merchant_name=m.name,
            amount_received=tx.amount_received,
            amount_usd=tx.amount_usd,
            coin_symbol=c.symbol,
            fiat_currency="USD",
            fee=sfl.fee_amount if sfl else Decimal("0"),
            status=map_tx_status(tx.confirmations, inv.status.value),
            confirmations=tx.confirmations,
            created_at=tx.detected_at.isoformat(),
            confirmed_at=tx.confirmed_at.isoformat() if tx.confirmed_at else None,
        )
        for tx, inv, m, c, sfl in rows
    ]


@router.get("/export", summary="Экспорт транзакций (CSV/XLSX)")
async def export_transactions(
    db: DbSession,
    format: str = Query("csv", description="Формат: csv или xlsx"),
    merchant_id: uuid.UUID | None = Query(None),
    coin_id: int | None = Query(None),
    status: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    min_amount_usd: float | None = Query(None),
    max_amount_usd: float | None = Query(None),
    search: str | None = Query(None),
):
    """
    Выгрузка транзакций в выбранном формате с учетом всех фильтров.
    """
    from fastapi.responses import StreamingResponse
    import io
    import csv

    query = (
        select(Transaction, Invoice, Merchant, Coin, SystemFeeLog)
        .join(Invoice, Transaction.invoice_id == Invoice.id)
        .join(Merchant, Invoice.merchant_id == Merchant.id)
        .join(Coin, Invoice.coin_id == Coin.id)
        .outerjoin(SystemFeeLog, Transaction.id == SystemFeeLog.transaction_id)
        .order_by(Transaction.detected_at.desc())
    )

    query = _apply_filters(
        query=query,
        status=status,
        merchant_id=merchant_id,
        coin_id=coin_id,
        date_from=date_from,
        date_to=date_to,
        min_amount_usd=min_amount_usd,
        max_amount_usd=max_amount_usd,
        search=search
    )

    result = await db.execute(query)
    rows = result.all()

    filename = f"transactions_{datetime.utcnow().strftime('%Y-%m-%d')}"

    if format == "xlsx":
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            headers = ["ID", "TXID", "Merchant", "Amount", "USD", "Coin", "Fee", "Status", "Created At"]
            ws.append(headers)
            for tx, inv, m, c, sfl in rows:
                ws.append([
                    str(tx.id), tx.txid, m.name, float(tx.amount_received),
                    float(tx.amount_usd or 0), c.symbol, float(sfl.fee_amount if sfl else 0),
                    map_tx_status(tx.confirmations, inv.status.value),
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
    writer.writerow([
        "ID", "TXID", "Merchant", "Amount", "USD", "Coin", "Fee", "Status", "Created At"
    ])

    for tx, inv, m, c, sfl in rows:
        writer.writerow([
            str(tx.id),
            tx.txid,
            m.name,
            str(tx.amount_received),
            str(tx.amount_usd),
            c.symbol,
            str(sfl.fee_amount if sfl else 0),
            map_tx_status(tx.confirmations, inv.status.value),
            tx.detected_at.isoformat()
        ])

    output.seek(0)
    data = output.getvalue()
    
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
    )


@router.get("/{tx_id}", response_model=TransactionDetailOut, summary="Детали конкретного платежа")
async def get_transaction(tx_id: uuid.UUID, db: DbSession) -> TransactionDetailOut:
    """
    Детали платежа: подтверждения, вебхуки и подробная информация из ТЗ.
    """
    query = (
        select(Transaction, Invoice, Merchant, Coin, SystemFeeLog)
        .join(Invoice, Transaction.invoice_id == Invoice.id)
        .join(Merchant, Invoice.merchant_id == Merchant.id)
        .join(Coin, Invoice.coin_id == Coin.id)
        .outerjoin(SystemFeeLog, Transaction.id == SystemFeeLog.transaction_id)
        .where(Transaction.id == tx_id)
    )
    result = await db.execute(query)
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    tx, inv, m, c, sfl = row

    # Webhook logs
    logs_result = await db.execute(
        select(WebhookLog).where(WebhookLog.invoice_id == inv.id).order_by(WebhookLog.created_at.desc())
    )
    webhook_logs = logs_result.scalars().all()

    return TransactionDetailOut(
        id=tx.id,
        txid=tx.txid,
        merchant_id=m.id,
        merchant_name=m.name,
        amount_received=tx.amount_received,
        amount_usd=tx.amount_usd,
        coin_symbol=c.symbol,
        fiat_currency="USD",
        fee=sfl.fee_amount if sfl else Decimal("0"),
        status=map_tx_status(tx.confirmations, inv.status.value),
        confirmations=tx.confirmations,
        created_at=tx.detected_at.isoformat(),
        confirmed_at=tx.confirmed_at.isoformat() if tx.confirmed_at else None,
        merchant=MerchantInside(id=m.id, name=m.name),
        customer_email=inv.customer_email,
        deposit_address=inv.address,
        amount_expected=inv.amount_expected,
        webhook_logs=[
            WebhookLogOut(
                id=log.id,
                url=log.url,
                response_status=log.response_status,
                error_message=log.error_message,
                created_at=log.created_at.isoformat(),
            )
            for log in webhook_logs
        ],
        network_info={}
    )

"""
Invoice endpoints — требуют X-API-Key аутентификации.

POST /api/v1/invoices/create       — создать инвойс
GET  /api/v1/invoices/{id}         — статус инвойса
GET  /api/v1/invoices              — список инвойсов мерчанта
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from core.database import DbSession
from core.models import Invoice, InvoiceStatus, Merchant
from core.schemas import InvoiceCreateRequest, InvoiceOut
from services.api.auth import get_current_merchant
from services.api.invoice_service import InvoiceService

logger = logging.getLogger(__name__)

CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post(
    "/create",
    response_model=InvoiceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать инвойс на оплату",
)
async def create_invoice(
    payload: InvoiceCreateRequest,
    db: DbSession,
    merchant: CurrentMerchant,
) -> InvoiceOut:
    """
    Создаёт новый инвойс и возвращает уникальный Bitcoin-адрес для оплаты.

    **Аутентификация**: X-API-Key заголовок.

    merchant_id из тела запроса игнорируется — используется ID
    из аутентифицированного мерчанта (защита от подмены чужих данных).
    """
    service = InvoiceService(db)
    try:
        invoice = await service.create_invoice(
            merchant_id=merchant.id,
            coin_symbol=payload.coin_symbol,
            amount=payload.amount,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error creating invoice: %s", exc)
        raise HTTPException(status_code=500, detail="Internal error. Please try again.")

    return InvoiceOut.model_validate(invoice)


@router.get(
    "",
    response_model=list[InvoiceOut],
    summary="Список инвойсов мерчанта",
)
async def list_invoices(
    db: DbSession,
    merchant: CurrentMerchant,
    status_filter: InvoiceStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[InvoiceOut]:
    """
    Возвращает инвойсы текущего мерчанта.

    Пример: GET /api/v1/invoices?status=PAID&limit=20
    """
    q = select(Invoice).where(Invoice.merchant_id == merchant.id)
    if status_filter is not None:
        q = q.where(Invoice.status == status_filter)
    q = q.order_by(Invoice.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(q)
    return [InvoiceOut.model_validate(inv) for inv in result.scalars().all()]


@router.get(
    "/{invoice_id}",
    response_model=InvoiceOut,
    summary="Детали инвойса",
)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: DbSession,
    merchant: CurrentMerchant,
) -> InvoiceOut:
    """Мерчант может видеть только свои инвойсы."""
    result = await db.execute(
        select(Invoice).where(
            Invoice.id == invoice_id,
            Invoice.merchant_id == merchant.id,
        )
    )
    invoice = result.scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    return InvoiceOut.model_validate(invoice)
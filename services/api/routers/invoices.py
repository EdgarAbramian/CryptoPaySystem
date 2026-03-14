"""
Invoice API endpoints.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from core.database import DbSession
from core.schemas import InvoiceCreateRequest, InvoiceOut
from services.api.invoice_service import InvoiceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post(
    "/create",
    response_model=InvoiceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new payment invoice",
)
async def create_invoice(
    payload: InvoiceCreateRequest,
    db: DbSession,
) -> InvoiceOut:
    """
    Create a new invoice for a merchant.

    - Validates the coin symbol and merchant ID.
    - Derives a unique deposit address using the blockchain provider.
    - Persists and returns the invoice.
    """
    service = InvoiceService(db)
    try:
        invoice = await service.create_invoice(
            merchant_id=payload.merchant_id,
            coin_symbol=payload.coin_symbol,
            amount=payload.amount,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        logger.exception("Unexpected error creating invoice: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again later.",
        )

    return InvoiceOut.model_validate(invoice)

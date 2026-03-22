import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select

from core.database import DbSession
from core.models import Invoice, Merchant, Transaction
from services.api.admin_auth import require_admin

router = APIRouter(
    prefix="/search",
    tags=["admin-search"],
    dependencies=[Depends(require_admin)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class SearchMerchantItem(BaseModel):
    id: uuid.UUID
    name: str | None
    email: str | None
    status: str

    model_config = {"from_attributes": True}

class SearchTransactionItem(BaseModel):
    id: uuid.UUID
    txid: str
    amount_usd: float
    status: str

    model_config = {"from_attributes": True}

class SearchInvoiceItem(BaseModel):
    id: uuid.UUID
    address: str
    amount: str
    status: str

    model_config = {"from_attributes": True}

class GlobalSearchResponse(BaseModel):
    merchants: list[SearchMerchantItem]
    transactions: list[SearchTransactionItem]
    invoices: list[SearchInvoiceItem]

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=GlobalSearchResponse, summary="Глобальный поиск")
async def global_search(
    q: str,
    db: DbSession,
    limit: int = Query(5, ge=1, le=20),
) -> GlobalSearchResponse:
    """
    Глобальный поиск по мерчантам, транзакциям и инвойсам.
    """
    search_term = f"%{q}%"

    # Search Merchants
    merchant_query = select(Merchant).where(
        or_(
            Merchant.name.ilike(search_term),
            Merchant.email.ilike(search_term)
        )
    ).limit(limit)
    merchants_result = await db.execute(merchant_query)
    merchants = merchants_result.scalars().all()

    # Search Transactions
    tx_query = select(Transaction).where(
        Transaction.txid.ilike(search_term)
    ).limit(limit)
    tx_result = await db.execute(tx_query)
    transactions = tx_result.scalars().all()

    # Search Invoices
    # Can also search by customer_email
    inv_query = select(Invoice).where(
        or_(
            Invoice.address.ilike(search_term),
            Invoice.customer_email.ilike(search_term)
        )
    ).limit(limit)
    inv_result = await db.execute(inv_query)
    invoices = inv_result.scalars().all()

    return GlobalSearchResponse(
        merchants=[
            SearchMerchantItem(
                id=m.id, name=m.name, email=m.email, status=m.status.value
            ) for m in merchants
        ],
        transactions=[
            SearchTransactionItem(
                id=t.id, txid=t.txid, amount_usd=float(t.amount_usd), 
                status="CONFIRMED" if t.confirmations >= 6 else "PENDING"
            ) for t in transactions
        ],
        invoices=[
            SearchInvoiceItem(
                id=i.id, address=i.address, amount=str(i.amount_expected), status=i.status.value
            ) for i in invoices
        ]
    )

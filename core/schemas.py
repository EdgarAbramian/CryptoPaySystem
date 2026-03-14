import uuid
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from core.models import InvoiceStatus


class _BaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CoinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    name: str
    is_active: bool


class MerchantCreate(BaseModel):
    commission_pcent: Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("100"))] = Decimal(
        "1.00"
    )
    webhook_url: HttpUrl | None = None


class MerchantOut(_BaseSchema):
    id: uuid.UUID
    commission_pcent: Decimal
    webhook_url: str | None


class InvoiceCreateRequest(BaseModel):
    """Payload for POST /api/v1/invoices/create"""

    merchant_id: uuid.UUID
    coin_symbol: Annotated[str, Field(min_length=1, max_length=16)]
    amount: Annotated[Decimal, Field(gt=Decimal("0"))]

    @field_validator("coin_symbol")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        return v.upper().strip()


class InvoiceOut(_BaseSchema):
    id: uuid.UUID
    coin_id: int
    merchant_id: uuid.UUID
    address: str
    amount_expected: Decimal
    status: InvoiceStatus
    derivation_index: int


class TransactionOut(_BaseSchema):
    id: uuid.UUID
    invoice_id: uuid.UUID
    txid: str
    amount_received: Decimal
    confirmations: int


class ErrorResponse(BaseModel):
    detail: str


class SuccessResponse(BaseModel):
    ok: bool = True
    message: str = "success"

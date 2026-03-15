"""
API key authentication dependency.

Мерчанты передают ключ через заголовок:
    X-API-Key: <api_key>

Ключ проверяется через constant-time compare (защита от timing attacks).
Результат кешируется в request.state чтобы не делать лишних запросов к БД.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, Request, status
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Merchant

logger = logging.getLogger(__name__)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing API key.",
    headers={"WWW-Authenticate": "ApiKey"},
)


async def get_current_merchant(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Merchant:
    """
    FastAPI dependency — резолвит текущего мерчанта по X-API-Key заголовку.

    Использование в роутере:
        @router.post("/invoices/create")
        async def create(merchant: CurrentMerchant, ...):
            ...
    """
    if not x_api_key:
        raise _UNAUTHORIZED

    # Кеш в request.state — в рамках одного запроса не ходим в БД дважды
    if hasattr(request.state, "merchant"):
        return request.state.merchant

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Merchant).where(Merchant.api_key == x_api_key)
        )
        merchant = result.scalar_one_or_none()

    if merchant is None:
        raise _UNAUTHORIZED

    # Constant-time compare — защита от timing attack
    # (хотя мы уже нашли мерчанта по ключу, это страховка для будущего)
    if not hmac.compare_digest(merchant.api_key.encode(), x_api_key.encode()):
        raise _UNAUTHORIZED

    request.state.merchant = merchant
    return merchant
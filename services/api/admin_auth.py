"""
Admin-only authentication dependency.

Административные эндпоинты (регистрация мерчантов, просмотр всех данных)
защищены отдельным секретом ADMIN_SECRET из .env.

Заголовок: X-Admin-Secret: <secret>
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, status

from core.config import settings

logger = logging.getLogger(__name__)

_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Admin access required.",
)


async def require_admin(
    x_admin_secret: str | None = Header(default=None, alias="X-Admin-Secret"),
) -> None:
    """
    Dependency для admin-only эндпоинтов.

    Если ADMIN_SECRET не задан в .env — admin эндпоинты недоступны.
    """
    if not settings.admin_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is disabled (ADMIN_SECRET not configured).",
        )

    if not x_admin_secret:
        raise _FORBIDDEN

    if not hmac.compare_digest(settings.admin_secret.encode(), x_admin_secret.encode()):
        raise _FORBIDDEN
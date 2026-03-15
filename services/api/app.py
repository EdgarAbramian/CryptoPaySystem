"""
FastAPI application factory.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import settings
from providers.registry import bootstrap_providers

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="CryptoPayments API",
        version="1.0.0",
        description="""
## Custodial Bitcoin Payment Gateway

### Аутентификация мерчантов
Все мерчант-эндпоинты требуют заголовок:
```
X-API-Key: <your_api_key>
```

### Admin эндпоинты
Требуют заголовок:
```
X-Admin-Secret: <admin_secret>
```

### Порядок работы
1. Админ регистрирует мерчанта → получает `api_key`
2. Мерчант создаёт инвойс → получает Bitcoin-адрес
3. Клиент оплачивает → система автоматически зачисляет баланс
4. Мерчант запрашивает payout → деньги отправляются на его адрес
        """,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── CORS ─────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Startup / Shutdown ───────────────────────────────────────────────────
    @app.on_event("startup")
    async def on_startup() -> None:
        logger.info("Starting %s …", settings.app_name)
        bootstrap_providers()
        if not settings.admin_secret:
            logger.warning(
                "ADMIN_SECRET is not set — admin API is DISABLED. "
                "Set ADMIN_SECRET in .env to enable merchant registration."
            )

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        logger.info("Shutting down %s …", settings.app_name)

    # ── Routers ──────────────────────────────────────────────────────────────
    from services.api.routers.invoices import router as invoices_router
    from services.api.routers.merchant import router as merchant_router
    from services.api.routers.admin_merchants import router as admin_router
    from services.payout.router import router as payouts_router

    app.include_router(invoices_router,  prefix="/api/v1")
    app.include_router(payouts_router,   prefix="/api/v1")
    app.include_router(merchant_router,  prefix="/api/v1")
    app.include_router(admin_router,     prefix="/api/v1")

    # ── Health ───────────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"], summary="Health check")
    async def health() -> dict:
        return {"status": "ok", "service": settings.app_name, "version": "1.0.0"}

    return app


app = create_app()
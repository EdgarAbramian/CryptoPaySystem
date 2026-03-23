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
Требуют JWT токен в заголовке:
```
Authorization: Bearer <token>
```
Токен можно получить через `POST /api/admin/auth/login`.

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
    from services.api.routers import admin_routes, invoices, merchant, auth
    from services.payout.router import router as payout_router

    app.include_router(invoices.router,         prefix="/api/v1")
    app.include_router(auth.router,             prefix="/api/v1")
    app.include_router(payout_router,           prefix="/api/v1")
    app.include_router(merchant.router,         prefix="/api/v1")
    # Admin Routes
    app.include_router(admin_routes.auth_router,       prefix="/api/admin")
    app.include_router(admin_routes.merchants_router,  prefix="/api/admin")
    app.include_router(admin_routes.dashboard_router,  prefix="/api/admin")
    app.include_router(admin_routes.transactions_router, prefix="/api/admin")
    app.include_router(admin_routes.nodes_router,      prefix="/api/admin")
    app.include_router(admin_routes.system_router,     prefix="/api/admin")
    app.include_router(admin_routes.search_router,     prefix="/api/admin")
    app.include_router(admin_routes.notifications_router, prefix="/api/admin")
    app.include_router(admin_routes.analytics_router,    prefix="/api/admin")
    app.include_router(admin_routes.charts_router,       prefix="/api/admin")

    # ── Dev / Testnet helpers (DEBUG only) ───────────────────────────────────
    # Safe to delete dev.py in production — this block is never executed when
    # DEBUG=false, so there will be no ImportError even if the file is absent.
    if settings.debug:
        from services.api.routers.dev import router as dev_router  # noqa: PLC0415
        app.include_router(dev_router, prefix="/api/v1")
        logger.warning(
            "⚠️  DEV MODE ENABLED — /api/v1/dev/* endpoints are active. "
            "Set DEBUG=false before deploying to production."
        )

    # ── Health ───────────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"], summary="Health check")
    async def health() -> dict:
        return {"status": "ok", "service": settings.app_name, "version": "1.0.0"}

    return app


app = create_app()
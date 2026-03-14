"""
FastAPI application factory.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import settings
from providers.registry import bootstrap_providers
from services.api.routers.invoices import router as invoices_router

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # -----------------------------------------------------------------------
    # CORS (adjust origins for production)
    # -----------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------------
    # Startup / shutdown
    # -----------------------------------------------------------------------

    @app.on_event("startup")
    async def on_startup() -> None:
        logger.info("Starting %s …", settings.app_name)
        bootstrap_providers()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        logger.info("Shutting down %s …", settings.app_name)

    # -----------------------------------------------------------------------
    # Routers
    # -----------------------------------------------------------------------
    from services.payout.router import router as payouts_router

    app.include_router(invoices_router, prefix="/api/v1")
    app.include_router(payouts_router, prefix="/api/v1")

    @app.get("/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok", "service": settings.app_name}

    return app


app = create_app()

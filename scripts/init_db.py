"""
Database initialisation script — creates all tables.

Usage (dev):
    python scripts/init_db.py

For production use Alembic migrations instead.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import create_async_engine

from core.config import settings
from core.models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def init() -> None:
    engine = create_async_engine(settings.database_url, echo=True)
    async with engine.begin() as conn:
        logger.info("Creating tables …")
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(init())

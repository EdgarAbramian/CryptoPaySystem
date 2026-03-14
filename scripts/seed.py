"""
Seed the database with initial Coins and a test Merchant.

Usage:
    python scripts/seed.py
"""
from __future__ import annotations

import asyncio
import secrets
import uuid

from core.database import AsyncSessionLocal
from core.models import Coin, Merchant


async def seed() -> None:
    async with AsyncSessionLocal() as session:
        # Coins
        btc = Coin(symbol="BTC", name="Bitcoin", is_active=True)
        eth = Coin(symbol="ETH", name="Ethereum", is_active=True)
        trx = Coin(symbol="TRX", name="TRON", is_active=False)  # provider not yet implemented
        session.add_all([btc, eth, trx])

        # Demo merchant
        merchant = Merchant(
            id=uuid.uuid4(),
            api_key=secrets.token_urlsafe(32),
            commission_pcent=1.0,
            webhook_url="https://example.com/webhook",
        )
        session.add(merchant)
        await session.commit()
        print(f"Seeded merchant: id={merchant.id} | api_key={merchant.api_key}")


if __name__ == "__main__":
    asyncio.run(seed())

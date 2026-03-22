import asyncio
from sqlalchemy import select, func
from core.database import AsyncSessionLocal
from core.models import Merchant, MerchantStatus, SystemFeeLog

async def check():
    async with AsyncSessionLocal() as db:
        # 1. Merchant counts
        total = (await db.execute(select(func.count(Merchant.id)))).scalar()
        active = (await db.execute(select(func.count(Merchant.id)).where(Merchant.status == MerchantStatus.ACTIVE))).scalar()
        pending = (await db.execute(select(func.count(Merchant.id)).where(Merchant.status == MerchantStatus.PENDING))).scalar()
        
        # 2. Total Volume
        volume = (await db.execute(select(func.sum(SystemFeeLog.gross_amount_usd)))).scalar()
        
        print(f"Total Merchants: {total}")
        print(f"Active: {active}")
        print(f"Pending: {pending}")
        print(f"Total Volume USD: {volume}")

if __name__ == "__main__":
    asyncio.run(check())

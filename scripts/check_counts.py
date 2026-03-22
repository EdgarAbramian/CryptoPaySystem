import asyncio
import os
import sys

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.database import AsyncSessionLocal
from core.models import Transaction, SystemFeeLog
from sqlalchemy import func, select

async def check_counts():
    async with AsyncSessionLocal() as db:
        tx_count = (await db.execute(select(func.count(Transaction.id)))).scalar()
        fee_count = (await db.execute(select(func.count(SystemFeeLog.id)))).scalar()
        print(f"Transactions in DB: {tx_count}")
        print(f"SystemFeeLogs in DB: {fee_count}")

if __name__ == "__main__":
    asyncio.run(check_counts())

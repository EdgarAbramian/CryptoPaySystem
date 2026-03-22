import asyncio
from decimal import Decimal
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Transaction, Invoice, Merchant, Balance, Coin

async def diagnose():
    async with AsyncSessionLocal() as session:
        # Find all transactions for a merchant with total volume around 458
        res = await session.execute(
            select(Merchant)
            .join(Invoice, Merchant.id == Invoice.merchant_id)
            .join(Transaction, Invoice.id == Transaction.invoice_id)
            .group_by(Merchant.id)
            .having(select(func.sum(Transaction.amount_usd)).label('vol') > 0)
        )
        # Wait, func.sum needs a subquery or better way. Let's just list all transactions and group here.
        
        res = await session.execute(select(Transaction).order_by(Transaction.detected_at.desc()))
        txs = res.scalars().all()
        
        print(f"Total Transactions in DB: {len(txs)}")
        for tx in txs[:20]:
            print(f"TX: {tx.txid[:16]} | Conf: {tx.confirmations} | Credited: {tx.credited} | Amount: ${tx.amount_usd}")

if __name__ == "__main__":
    from sqlalchemy import func # ensure it's imported
    asyncio.run(diagnose())

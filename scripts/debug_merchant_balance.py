import asyncio
import uuid
from decimal import Decimal
from sqlalchemy import select, func
from core.database import AsyncSessionLocal
from core.models import Transaction, Invoice, Merchant, Balance, Coin

async def check_merchant_status(email):
    async with AsyncSessionLocal() as session:
        # Find merchant
        res = await session.execute(select(Merchant).where(Merchant.email == email))
        merchant = res.scalar_one_or_none()
        if not merchant:
            print(f"Merchant {email} not found")
            return

        print(f"Merchant: {merchant.name} ({merchant.id})")
        print(f"Commission: {merchant.commission_pcent}%")

        # Check transactions
        res = await session.execute(
            select(Transaction, Invoice.status)
            .join(Invoice, Transaction.invoice_id == Invoice.id)
            .where(Invoice.merchant_id == merchant.id)
        )
        txs = res.all()
        print(f"\nTransactions ({len(txs)} total):")
        total_vol = Decimal("0")
        for tx, inv_status in txs:
            print(f"  TXID: {tx.txid[:16]}... | Amount: ${tx.amount_usd} | Conf: {tx.confirmations} | Credited: {tx.credited} | InvStatus: {inv_status}")
            if tx.amount_usd:
                total_vol += tx.amount_usd
        
        print(f"Calculated Volume: ${total_vol}")

        # Check balances
        res = await session.execute(
            select(Balance, Coin.symbol)
            .join(Coin, Balance.coin_id == Coin.id)
            .where(Balance.merchant_id == merchant.id)
        )
        balances = res.all()
        print(f"\nBalances ({len(balances)} total):")
        for balance, symbol in balances:
            print(f"  Coin: {symbol} | Available: {balance.amount_available} | Locked: {balance.amount_locked}")

if __name__ == "__main__":
    import sys
    # Assume we are looking for the merchant who has the volume. 
    # Since I don't know the email, I'll search for all merchants with volume > 0
    async def find_busy_merchant():
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Merchant.email)
                .join(Invoice, Merchant.id == Invoice.merchant_id)
                .join(Transaction, Invoice.id == Transaction.invoice_id)
                .group_by(Merchant.email)
            )
            emails = res.scalars().all()
            for email in emails:
                await check_merchant_status(email)
                print("-" * 40)

    asyncio.run(find_busy_merchant())

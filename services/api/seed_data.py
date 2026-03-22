import asyncio
import uuid
import sys
import os
from decimal import Decimal
from datetime import datetime, timedelta

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import select, delete
from core.database import AsyncSessionLocal
from core.models import (
    Coin, Merchant, MerchantStatus, Invoice, InvoiceStatus, 
    Transaction, Balance, User, UserRole, SystemFeeLog, AuditLog, UTXO
)
from services.api.auth_utils import hash_password
from services.price.service import PriceService

async def seed():
    print("Starting comprehensive seeding with unique transactions...")
    async with AsyncSessionLocal() as db:
        # 0. Clean up existing logs
        print("Cleaning up old logs...")
        await db.execute(delete(UTXO))
        await db.execute(delete(SystemFeeLog))
        await db.execute(delete(AuditLog))
        await db.execute(delete(Transaction))
        await db.execute(delete(Invoice))
        await db.execute(delete(Balance))
        await db.execute(delete(Merchant))

        # 1. Coins
        print("Seeding coins...")
        coins_symbols = ["BTC", "USDT", "ETH", "LTC"]
        coins = {}
        for sym in coins_symbols:
            res = await db.execute(select(Coin).where(Coin.symbol == sym))
            coin = res.scalar_one_or_none()
            if not coin:
                coin = Coin(symbol=sym, name=sym)
                db.add(coin); await db.flush()
            coins[sym] = coin

        # 2. Merchants
        print("Seeding merchants...")
        merchants_data = [
            {"api_key": "m_key_premium_1", "name": "Premium Store", "email": "premium@store.com", "status": MerchantStatus.ACTIVE, "tier": "GOLD", "commission_pcent": Decimal("1.00")},
            {"api_key": "m_key_basic_2", "name": "Basic Shop", "email": "shop@example.com", "status": MerchantStatus.ACTIVE, "tier": "BRONZE", "commission_pcent": Decimal("2.00")},
            {"api_key": "m_key_pending_3", "name": "New Startup", "email": "new@startup.io", "status": MerchantStatus.PENDING, "tier": "BRONZE", "commission_pcent": Decimal("1.50")},
        ]
        
        seeded_merchants = []
        for m_row in merchants_data:
            res = await db.execute(select(Merchant).where(Merchant.api_key == m_row["api_key"]))
            merchant = res.scalar_one_or_none()
            if not merchant:
                merchant = Merchant(**m_row)
                db.add(merchant); await db.flush()
            seeded_merchants.append(merchant)
        
        # Primary merchant for most transactions
        primary_merchant = seeded_merchants[0]

        # 3. Generating 10 Unique Transactions (7 for the user's specific count + 3 more)
        print("Generating 10 unique transactions over the last 10 days...")
        for i in range(10):
            days_ago = i
            coin_sym = coins_symbols[i % len(coins_symbols)]
            coin = coins[coin_sym]
            rate = PriceService.get_rate(coin_sym)
            
            # Вращаем мерчантов для распределения транзакций
            current_merchant = seeded_merchants[i % len(seeded_merchants)]
            
            # Vary amounts
            amt = (Decimal("0.01") * (i + 1)) if coin_sym == "BTC" else (Decimal("100") * (i + 1))
            usd_val = PriceService.to_usd(amt, coin_sym)
            
            countries = ["US", "DE", "GB", "FR", "ES", "IT", "CA"]
            country = countries[i % len(countries)] if i % 4 != 0 else None # Some nulls

            # Invoice
            inv = Invoice(
                merchant_id=current_merchant.id, coin_id=coin.id,
                address=f"addr_{coin_sym.lower()}_{i}", amount_expected=amt,
                amount_usd=usd_val, status=InvoiceStatus.PAID,
                description=f"Order #{1000 + i}", derivation_index=i,
                country_code=country,
                created_at=datetime.utcnow() - timedelta(days=days_ago, hours=2),
                paid_at=datetime.utcnow() - timedelta(days=days_ago, hours=1)
            )
            db.add(inv); await db.flush()
            
            # Transaction
            tx = Transaction(
                invoice_id=inv.id, txid=f"txid_{coin_sym.lower()}_{uuid.uuid4().hex[:8]}",
                amount_received=amt, amount_usd=usd_val,
                confirmations=6, credited=True,
                confirmed_at=inv.paid_at, detected_at=inv.created_at
            )
            db.add(tx); await db.flush()
            
            # Fee Log
            db.add(SystemFeeLog(
                transaction_id=tx.id, merchant_id=current_merchant.id, coin_id=coin.id,
                gross_amount=amt, gross_amount_usd=usd_val,
                commission_pcent=current_merchant.commission_pcent,
                fee_amount=amt * (current_merchant.commission_pcent / 100),
                fee_amount_usd=usd_val * (current_merchant.commission_pcent / 100),
                net_amount=amt * (1 - current_merchant.commission_pcent / 100),
                net_amount_usd=usd_val * (1 - current_merchant.commission_pcent / 100),
                usd_rate=rate, created_at=tx.confirmed_at
            ))

        # 4. Auth
        print("Seeding Users...")
        admin_email = "admin@nexuspay.com"
        res = await db.execute(select(User).where(User.email == admin_email))
        if not res.scalar_one_or_none():
            db.add(User(email=admin_email, hashed_password=hash_password("admin123"), role=UserRole.ADMIN))

        # 5. Admin Notifications
        print("Seeding Notifications...")
        from core.models import AdminNotification
        db.add(AdminNotification(type="MERCHANT_REGISTERED", message="New merchant Premium Store registered."))
        db.add(AdminNotification(type="PAYMENT_RECEIVED", message=f"Large payment of $120.50 received."))
        db.add(AdminNotification(type="SYSTEM_ALERT", message="Node BTC is experiencing high latency.", is_read=True))
        db.add(AdminNotification(type="WITHDRAWAL_REQUEST", message="New withdrawal request from Basic Shop."))
        await db.flush()

        await db.commit()
        print(f"Seeding completed. Created 10 unique transactions and 4 notifications.")

if __name__ == "__main__":
    asyncio.run(seed())

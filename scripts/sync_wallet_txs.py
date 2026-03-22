import asyncio
import os
import sys
from decimal import Decimal
from sqlalchemy import select

# Добавляем корневую директорию проекта в sys.path
sys.path.append(os.getcwd())

from core.database import AsyncSessionLocal
from core.models import Invoice, InvoiceStatus
from providers.bitcoin_rpc import BitcoinRPCClient
from services.ledger.queue import LedgerJob, LedgerQueue

async def sync():
    print("--- Starting Wallet Sync ---")
    rpc = BitcoinRPCClient()
    queue = LedgerQueue()
    try:
        await queue.connect()
    except Exception as e:
        print(f"Error connecting to Redis: {e}")
        return

    async with AsyncSessionLocal() as session:
        # 1. Получаем все активные инвойсы
        res = await session.execute(
            select(Invoice).where(Invoice.status.in_([InvoiceStatus.NEW, InvoiceStatus.PARTIAL, InvoiceStatus.PENDING]))
        )
        active_invoices = {inv.address: inv for inv in res.scalars().all()}
        print(f"Tracking {len(active_invoices)} active invoices in DB.")

        # 2. Получаем последние транзакции из кошелька ноды
        try:
            # * means all accounts, 200 is count
            txs = await rpc._call("listtransactions", "*", 200)
            print(f"Fetched {len(txs)} transactions from Bitcoin node wallet.")
        except Exception as e:
            print(f"Error listing transactions: {e}")
            await queue.close()
            return

        # 3. Сопоставляем
        matched_count = 0
        for tx in txs:
            if tx.get("category") != "receive":
                continue
            
            addr = tx.get("address")
            if addr in active_invoices:
                inv = active_invoices[addr]
                print(f"MATCH: txid={tx['txid'][:16]}... address={addr} amount={tx['amount']} BTC")
                
                job = LedgerJob(
                    txid=tx["txid"],
                    invoice_id=str(inv.id),
                    address=addr,
                    amount_sat=int(Decimal(str(tx["amount"])) * Decimal("100000000")),
                    coin_symbol="BTC",
                    merchant_id=str(inv.merchant_id),
                    confirmations=tx.get("confirmations", 0)
                )
                await queue.push_hot(job)
                matched_count += 1

        print(f"--- Sync Complete. Pushed {matched_count} matches to hot queue. ---")
    
    await queue.close()

if __name__ == "__main__":
    asyncio.run(sync())

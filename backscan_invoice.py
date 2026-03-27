import asyncio
import sys
import os
import json
from decimal import Decimal

# Add project root to sys.path
sys.path.append(os.getcwd())

from providers.bitcoin_rpc import BitcoinRPCClient
from services.watcher.tx_queue import TxMatch, TxQueue
from core.database import AsyncSessionLocal
from core.models import Invoice, Coin

async def backscan_invoice(invoice_id):
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        stmt = select(Invoice, Coin.symbol).join(Coin, Invoice.coin_id == Coin.id).where(Invoice.id == invoice_id)
        result = await session.execute(stmt)
        row = result.fetchone()
        if not row:
            print(f"Invoice {invoice_id} not found.")
            return
        inv, symbol = row
        target_address = inv.address
        merchant_id = inv.merchant_id

    rpc = BitcoinRPCClient()
    queue = TxQueue()
    await queue.connect()

    print(f"Backscanning for address: {target_address} (Invoice: {invoice_id})")
    
    try:
        # Scan last 50 blocks
        curr_height = await rpc._call("getblockcount")
        for height in range(curr_height, curr_height - 50, -1):
            block_hash = await rpc._call("getblockhash", height)
            block = await rpc._call("getblock", block_hash, 2)
            
            for tx in block['tx']:
                txid = tx['txid']
                for out in tx['vout']:
                    spk = out['scriptPubKey']
                    addrs = []
                    if 'address' in spk: addrs.append(spk['address'])
                    if 'addresses' in spk: addrs.extend(spk['addresses'])
                    
                    if target_address in addrs:
                        print(f"FOUND MATCH in block {height}! TXID: {txid}")
                        value_sat = int(Decimal(str(out['value'])) * 100_000_000)
                        
                        match = TxMatch(
                            txid=txid,
                            invoice_id=str(inv.id),
                            address=target_address,
                            amount_sat=value_sat,
                            coin_symbol=symbol,
                            merchant_id=str(merchant_id),
                            confirmations=curr_height - height + 1
                        )
                        await queue.push(match)
                        print(f"Pushed to queue: {txid}")
                        await queue.close()
                        return

        print("No matches found in the last 50 blocks.")
    except Exception as e:
        print(f"Backscan error: {e}")
    finally:
        await queue.close()

if __name__ == "__main__":
    invoice_id = "4c22b153-7cb4-4b5e-b4bc-568afb9caea1"
    asyncio.run(backscan_invoice(invoice_id))

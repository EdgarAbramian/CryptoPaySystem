import asyncio
import os
import sys

# Добавляем корневую директорию проекта в sys.path
sys.path.append(os.getcwd())

from providers.bitcoin_rpc import BitcoinRPCClient

async def main():
    rpc = BitcoinRPCClient()
    txid = 'fa0689d4e49ddf5ec21f08586394fbe8d6d9425b93f674c801622c7b6d6ffa84'
    try:
        res = await rpc._call('gettransaction', txid)
        print(f"FOUND: {res['txid']}")
        print(f"CONFIRMATIONS: {res.get('confirmations', 0)}")
        print(f"DETAILS: {res.get('details')}")
    except Exception as e:
        print(f"[ERROR] {e}")

if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import os
import sys

# Добавляем корневую директорию проекта в sys.path
sys.path.append(os.getcwd())

from providers.bitcoin_rpc import BitcoinRPCClient

async def check():
    rpc = BitcoinRPCClient()
    try:
        confirmed = await rpc._call("getbalance")
        unconfirmed = await rpc._call("getunconfirmedbalance")
        info = await rpc._call("getblockchaininfo")
        print(f"CONFIRMED: {confirmed} BTC")
        print(f"UNCONFIRMED: {unconfirmed} BTC")
        print(f"BLOCKS: {info.get('blocks')}")
        print(f"HEADERS: {info.get('headers')}")
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(check())

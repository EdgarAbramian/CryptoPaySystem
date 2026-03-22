import asyncio
import sys
import os

# Добавляем корневую директорию проекта в sys.path, чтобы импорта работали
sys.path.append(os.getcwd())

from providers.bitcoin_rpc import BitcoinRPCClient

async def get_fresh_address():
    rpc = BitcoinRPCClient()
    try:
        address = await rpc._call("getnewaddress", "dev-simulation")
        balance = await rpc._call("getbalance")
        print(f"\n[INFO] Fresh Testnet Address: {address}")
        print(f"[INFO] Current Node Balance: {balance} BTC")
        print(f"\nFund this address via: https://coinfaucet.eu/en/btc-testnet/")
    except Exception as e:
        print(f"[ERROR] {e}")

if __name__ == "__main__":
    asyncio.run(get_fresh_address())

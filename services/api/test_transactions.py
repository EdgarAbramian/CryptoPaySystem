import asyncio
import httpx

async def test_transactions():
    base_url = "http://127.0.0.1:8001/api/admin"
    
    async with httpx.AsyncClient() as client:
        # 1. Login
        print("Logging in...")
        login_res = await client.post(
            f"{base_url}/auth/login",
            json={"email": "admin@nexuspay.com", "password": "admin123"}
        )
        token = login_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        
        # 2. Get Transactions
        print("Fetching transactions...")
        tx_res = await client.get(f"{base_url}/transactions", headers=headers)
        print(f"Status: {tx_res.status_code}")
        if tx_res.status_code == 200:
            data = tx_res.json()
            print(f"Items returned: {len(data)}")
            for tx in data[:3]:
                print(f"  TXID: {tx['txid']}, Coin: {tx['merchant_id']}") # just checking keys
        else:
            print(f"Error: {tx_res.text}")

if __name__ == "__main__":
    asyncio.run(test_transactions())

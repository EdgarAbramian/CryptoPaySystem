import asyncio
import httpx
import json

async def test_top_merchants():
    base_url = "http://127.0.0.1:8001/api/admin"
    
    async with httpx.AsyncClient() as client:
        # 1. Login
        login_res = await client.post(
            f"{base_url}/auth/login",
            json={"email": "admin@nexuspay.com", "password": "admin123"}
        )
        token = login_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        
        # 2. Get Top Merchants
        print("\n--- Top Merchants (month, limit 5) ---")
        res = await client.get(f"{base_url}/merchants/top?period=month&limit=5", headers=headers)
        print(json.dumps(res.json(), indent=2))

if __name__ == "__main__":
    asyncio.run(test_top_merchants())

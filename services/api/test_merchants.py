import asyncio
import httpx
import json

async def test_merchants():
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
        
        # 2. Get Merchant Stats
        print("\n--- Merchant Stats ---")
        stats_res = await client.get(f"{base_url}/merchants/stats", headers=headers)
        print(json.dumps(stats_res.json(), indent=2))
        
        # 3. List Merchants
        print("\n--- Merchant List ---")
        list_res = await client.get(f"{base_url}/merchants", headers=headers)
        print(json.dumps(list_res.json()[:2], indent=2)) # show first 2
        
        # 4. Create New Merchant
        print("\n--- Create Merchant ---")
        create_res = await client.post(
            f"{base_url}/merchants",
            headers=headers,
            json={
                "name": "Test Merchant App",
                "email": "test@app.com",
                "commission_pcent": 0.5,
                "note": "Manual creation test"
            }
        )
        print(f"Status: {create_res.status_code}")
        print(json.dumps(create_res.json(), indent=2))

if __name__ == "__main__":
    asyncio.run(test_merchants())

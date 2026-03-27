import asyncio
import redis.asyncio as aioredis
import os

# Use .env or hardcoded for quick check
REDIS_URL = "redis://default:siski_123@80.89.238.37:6379/0"

async def check_queues():
    r = await aioredis.from_url(REDIS_URL)
    hot_len = await r.llen("tx_processing")
    retry_len = await r.llen("tx_retry")
    print(f"Queue 'tx_processing' length: {hot_len}")
    print(f"Queue 'tx_retry' length: {retry_len}")
    
    if hot_len > 0:
        items = await r.lrange("tx_processing", 0, -1)
        print(f"Hot items: {items}")
    if retry_len > 0:
        items = await r.lrange("tx_retry", 0, -1)
        print(f"Retry items: {items}")
        
    await r.close()

if __name__ == "__main__":
    asyncio.run(check_queues())

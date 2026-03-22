import json
import logging
import redis.asyncio as aioredis
from core.config import settings

logger = logging.getLogger(__name__)

async def publish_invoice_created(invoice_id: str, address: str, amount_expected: float, coin_symbol: str, merchant_id: str):
    """Publishes an invoice creation event to Redis Pub/Sub."""
    try:
        r = await aioredis.from_url(settings.redis_url)
        payload = {
            "invoice_id": invoice_id,
            "address": address,
            "amount_expected": amount_expected,
            "coin_symbol": coin_symbol,
            "merchant_id": merchant_id,
        }
        await r.publish(settings.redis_channel_invoice_created, json.dumps(payload))
        await r.aclose()
        logger.debug("Published invoice_created event for %s", invoice_id)
    except Exception as exc:
        logger.warning("Failed to publish invoice_created event: %s", exc)

"""
Invoice service — all business logic for invoice lifecycle.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Coin, Invoice, InvoiceStatus, Merchant
from providers.registry import registry
from services.api.events import publish_invoice_created

logger = logging.getLogger(__name__)


class InvoiceService:
    """Encapsulates invoice creation and status management."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def create_invoice(
        self,
        merchant_id: uuid.UUID,
        coin_symbol: str,
        amount: Decimal,
        amount_usd: Decimal | None = None,
        customer_email: str | None = None,
        description: str | None = None,
        country_code: str | None = None,
    ) -> Invoice:
        """
        Create a new invoice:

        1. Validate coin and merchant exist.
        2. Compute next derivation index for (merchant, coin) pair.
        3. Derive a deposit address via the appropriate blockchain provider.
        4. Persist and return the Invoice.

        Raises:
            ValueError: For business-rule violations (unknown coin / merchant).
            KeyError: If no provider is registered for the coin.
        """
        coin = await self._get_active_coin(coin_symbol)
        merchant = await self._get_merchant(merchant_id)
        index = await self._next_derivation_index(merchant.id, coin.id)

        provider = registry.get(coin.symbol)
        address = await provider.generate_address(index)

        invoice = Invoice(
            coin_id=coin.id,
            merchant_id=merchant.id,
            address=address,
            amount_expected=amount,
            amount_usd=amount_usd,
            status=InvoiceStatus.NEW,
            customer_email=customer_email,
            description=description,
            derivation_index=index,
            country_code=country_code,
        )
        self._session.add(invoice)
        await self._session.flush()  # populate invoice.id before commit
        logger.info(
            "Created invoice %s for merchant %s | coin=%s | amount=%s | index=%d",
            invoice.id,
            merchant.id,
            coin.symbol,
            amount,
            index,
        )

        # Tell the Bitcoin node to watch the new address immediately.
        # This is best-effort: if the node is unreachable the invoice is still
        # created and the Watcher's bootstrap will retry on next restart.
        await self._register_address_with_node(coin.symbol, address, str(invoice.id))

        # Notify the Watcher to add the address to its cache immediately
        await publish_invoice_created(
            invoice_id=str(invoice.id),
            address=address,
            amount_expected=float(amount),
            coin_symbol=coin.symbol,
            merchant_id=str(merchant.id),
        )

        return invoice

    async def _register_address_with_node(
        self, coin_symbol: str, address: str, invoice_id: str
    ) -> None:
        """Best-effort: import the address into the Bitcoin node wallet."""
        try:
            from providers.registry import registry
            if coin_symbol not in registry:
                return
            provider = registry.get(coin_symbol)
            if hasattr(provider, "import_address"):
                await provider.import_address(  # type: ignore[attr-defined]
                    address=address,
                    label=f"invoice:{invoice_id}",
                    rescan=False,
                )
        except Exception as exc:
            msg = str(exc)
            if "Only legacy wallets are supported" in msg:
                logger.warning(
                    "Node is using a descriptor wallet. importaddress skipped for %s. "
                    "Incoming payments will still be detected by the Watcher if it uses raw-tx polling.",
                    address
                )
            else:
                logger.warning(
                    "Could not import address %s into node: %s", address, exc
                )

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    async def _get_active_coin(self, symbol: str) -> Coin:
        result = await self._session.execute(
            select(Coin).where(Coin.symbol == symbol, Coin.is_active.is_(True))
        )
        coin = result.scalar_one_or_none()
        if coin is None:
            raise ValueError(f"Coin '{symbol}' not found or is not active.")
        return coin

    async def _get_merchant(self, merchant_id: uuid.UUID) -> Merchant:
        result = await self._session.execute(
            select(Merchant).where(Merchant.id == merchant_id)
        )
        merchant = result.scalar_one_or_none()
        if merchant is None:
            raise ValueError(f"Merchant '{merchant_id}' not found.")
        return merchant

    async def _next_derivation_index(
        self, merchant_id: uuid.UUID, coin_id: int
    ) -> int:
        """
        Return the next available derivation index for this (merchant, coin) pair.

        The index is merchant-scoped so that different merchants get independent
        address spaces derived from the same XPUB.  A global index per coin
        would also be valid — choose based on your HD wallet design.
        """
        result = await self._session.execute(
            select(func.coalesce(func.max(Invoice.derivation_index), -1)).where(
                Invoice.merchant_id == merchant_id,
                Invoice.coin_id == coin_id,
            )
        )
        max_index: int = result.scalar_one()
        return max_index + 1

"""
Provider registry — a lightweight service-locator for blockchain adapters.

Usage
-----
    from providers.registry import registry

    provider = registry.get("BTC")
    address = await provider.generate_address(42)
"""
from __future__ import annotations

import logging

from providers.base import BaseProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider) -> None:
        """Register a provider instance under its symbol (e.g. "BTC")."""
        key = provider.symbol.upper()
        self._providers[key] = provider
        logger.info("Registered blockchain provider: %s", key)

    def get(self, symbol: str) -> BaseProvider:
        """
        Return the provider for *symbol*.

        Raises:
            KeyError: If no provider is registered for the given symbol.
        """
        key = symbol.upper()
        try:
            return self._providers[key]
        except KeyError:
            available = ", ".join(self._providers) or "<none>"
            raise KeyError(
                f"No provider registered for symbol '{key}'. "
                f"Available: {available}"
            )

    def all(self) -> dict[str, BaseProvider]:
        return dict(self._providers)

    def __contains__(self, symbol: str) -> bool:
        return symbol.upper() in self._providers


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

registry = ProviderRegistry()


def bootstrap_providers() -> None:
    """Instantiate and register all supported blockchain providers."""
    from providers.bitcoin import BitcoinProvider  # local import to avoid cycles

    try:
        btc = BitcoinProvider()
        registry.register(btc)
    except ValueError as exc:
        # XPUB not set — log a warning so the app still boots in dev mode
        logger.warning("BitcoinProvider not registered: %s", exc)

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class NodeStatus:
    """Technical metrics for a blockchain node."""
    id: str
    coin: str
    status: str           # synced, syncing, error, offline
    block_height: int
    peers: int
    uptime: str
    version: str
    last_sync: datetime


@dataclass
class TxInfo:
    """Normalised transaction data returned by any provider."""

    txid: str
    amount: Decimal  # unit in the coin's base
    confirmations: int
    to_address: str
    from_address: str | None = None
    fee: Decimal | None = None


class BaseProvider(ABC):
    """
    Blockchain adapter interface.

    All methods are async so that network I/O never blocks the event loop.
    """

    symbol: str

    @abstractmethod
    async def generate_address(self, index: int) -> str:
        """
        Derive a deposit address at the given BIP44 index.

        Args:
            index: Monotonically increasing account-level child index.

        Returns:
            A blockchain address string.
        """

    @abstractmethod
    async def get_tx_info(self, txid: str) -> TxInfo:
        """
        Fetch and normalise transaction data from the network.

        Args:
            txid: The transaction hash / ID.

        Returns:
            A populated :class:`TxInfo` instance.

        Raises:
            TxNotFoundError: If the transaction does not exist.
            ProviderError: For any other network-level failure.
        """

    async def validate_address(self, address: str) -> bool:
        """Return True if the address is valid for this network."""
        raise NotImplementedError

    async def get_balance(self, address: str) -> Decimal:
        """Return the current balance of *address* in coin units."""
        raise NotImplementedError

    async def check_confirmations(self, txid: str) -> int:
        """
        Return the current on-chain confirmation count for *txid*.

        Returns 0 for unconfirmed / mempool transactions.
        Raises ProviderError on network failure.
        """
        raise NotImplementedError

    async def ping(self) -> bool:
        """Return True if the node/provider is reachable and healthy."""
        return True

    @abstractmethod
    async def get_node_status(self) -> NodeStatus:
        """Fetch real-time technical metrics from the node."""


class ProviderError(Exception):
    """Base exception for provider failures."""


class TxNotFoundError(ProviderError):
    """Raised when a transaction cannot be found."""


class AddressGenerationError(ProviderError):
    """Raised when address derivation fails."""

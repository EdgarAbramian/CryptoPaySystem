from decimal import Decimal
from typing import Dict

class PriceService:
    """
    Price Service.
    Responsible for currency conversion and fetching exchange rates.
    
    In production, this should integrate with external APIs (e.g., CoinGecko, Binance)
    and potentially cache rates in Redis.
    """
    
    # Hardcoded rates for initial version/dev environment
    _rates: Dict[str, Decimal] = {
        "BTC": Decimal("65000.00"),
        "ETH": Decimal("3500.00"),
        "USDT": Decimal("1.00"),
        "LTC": Decimal("85.00"),
    }

    @classmethod
    def get_rate(cls, symbol: str) -> Decimal:
        """Returns the current USD rate for the given coin symbol."""
        return cls._rates.get(symbol.upper(), Decimal("1.00"))

    @classmethod
    def to_usd(cls, amount: Decimal, symbol: str) -> Decimal:
        """Converts a cryptocurrency amount to its USD equivalent."""
        rate = cls.get_rate(symbol)
        # Round to 2 decimal places for USD
        return (amount * rate).quantize(Decimal("0.01"))

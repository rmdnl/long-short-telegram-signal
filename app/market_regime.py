"""
Market regime detection based on ADX.

Determines market regime for a timeframe:
- RANGING: ADX < adx_min (choppy, no reliable direction)
- WEAK_TREND: adx_min <= ADX < adx_min + 4
- TRENDING: ADX >= adx_min + 4

All regime logic must be deterministic and reproducible.
"""
from decimal import Decimal
from typing import Optional

from app.models import MarketRegime


class MarketRegimeError(Exception):
    """Raised when regime calculation fails"""
    pass


def detect_regime(
    adx_value: Decimal,
    adx_min: Decimal,
) -> MarketRegime:
    """
    Detect market regime from a single ADX reading.

    Args:
        adx_value: ADX 14 value for the timeframe
        adx_min: ADX threshold from config (default 22)

    Returns:
        MarketRegime enum value

    Raises:
        MarketRegimeError if adx_value is None or invalid
    """
    if adx_value is None:
        raise MarketRegimeError("adx_value cannot be None")

    if adx_value < 0 or adx_value > 100:
        raise MarketRegimeError(f"ADX value {adx_value} out of valid range [0, 100]")

    if adx_value < adx_min:
        return MarketRegime.RANGING

    weak_threshold = adx_min + Decimal('4')
    if adx_value < weak_threshold:
        return MarketRegime.WEAK_TREND

    return MarketRegime.TRENDING

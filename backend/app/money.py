"""Money helpers. All money is integer micro-USD (1 USD = 1_000_000). Never use float for money."""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation

MICRO = 1_000_000
BPS = 10_000  # 1 = 0.01%

__all__ = ["MICRO", "BPS", "to_micro", "from_micro", "usd", "parse_decimal", "apply_bps_floor", "split_exact", "fmt_usd"]


def parse_decimal(value: str | int | Decimal) -> Decimal:
    """Parse a Hyperliquid numeric string (or int/Decimal) exactly. Floats are rejected."""
    if isinstance(value, float):
        raise TypeError("float not allowed for money; pass the original string")
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"not a number: {value!r}") from e
    if not d.is_finite():
        raise ValueError(f"not finite: {value!r}")
    return d


def to_micro(value: str | int | Decimal, rounding: str = ROUND_FLOOR) -> int:
    """USD amount -> integer micro-USD. Default FLOOR (fees charged to users round down)."""
    d = parse_decimal(value) * MICRO
    return int(d.to_integral_value(rounding=rounding))


def from_micro(micro: int) -> Decimal:
    return Decimal(micro) / MICRO


def usd(amount: int | str) -> int:
    """Convenience for whole/str USD literals in config and tests: usd(20) == 20_000_000."""
    return to_micro(str(amount), rounding=ROUND_HALF_EVEN)


def apply_bps_floor(amount_micro: int, bps: int) -> int:
    """amount × bps / 10000, floored toward zero for non-negative amounts. Negative amounts -> 0 is NOT implied; caller decides."""
    if bps < 0:
        raise ValueError("bps must be >= 0")
    if amount_micro >= 0:
        return (amount_micro * bps) // BPS
    return -((-amount_micro * bps) // BPS)


def split_exact(total_micro: int, weights_bps: dict[str, int], remainder_to: str) -> dict[str, int]:
    """Split total by weights (bps, must sum to 10000). Each part floored; the remainder goes to `remainder_to`
    so parts always sum exactly to total."""
    if sum(weights_bps.values()) != BPS:
        raise ValueError("weights must sum to 10000 bps")
    if remainder_to not in weights_bps:
        raise ValueError("remainder_to must be one of the keys")
    parts = {k: apply_bps_floor(total_micro, w) for k, w in weights_bps.items()}
    parts[remainder_to] += total_micro - sum(parts.values())
    return parts


def fmt_usd(micro: int) -> str:
    sign = "-" if micro < 0 else ""
    d = (Decimal(abs(micro)) / MICRO).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return f"{sign}${d:,.2f}"

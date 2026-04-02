from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.dtypes import NonNegativeInt


def format_ordinal(n: NonNegativeInt) -> str:
    """Return the number with the correct ordinal suffix (e.g., 1st, 2nd)."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"**{n}{suffix}**"

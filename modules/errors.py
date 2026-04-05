"""Domain error types for Result returns.

Distinct from modules/exceptions.py which defines exceptions for the ErrorHandler cog.
Use these with Result for operations where the caller decides how to handle each failure case.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.dtypes import NonNegativeInt, PositiveInt


@dataclass(slots=True, frozen=True)
class InsufficientFunds:
    available: NonNegativeInt
    required: PositiveInt


@dataclass(slots=True, frozen=True)
class SelfTransfer:
    pass


type BurnError = InsufficientFunds
type TransferError = InsufficientFunds | SelfTransfer

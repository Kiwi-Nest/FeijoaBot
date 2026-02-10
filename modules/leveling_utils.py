import asyncio
import logging
import math
import struct
from typing import TYPE_CHECKING

from modules.dtypes import NonNegativeInt, UserId

if TYPE_CHECKING:
    from cogs.leveling import LevelingCog

log = logging.getLogger(__name__)

# --- Level Calculation Helpers ---


def get_raw_level(xp: NonNegativeInt) -> float:
    """Calculate the raw, fractional level for a given XP amount."""
    # The formula implies a base requirement of 6 XP to start leveling.
    return max(xp - 6, 0) ** (1 / 2.5)


def get_level(xp: NonNegativeInt) -> NonNegativeInt:
    """Calculate the whole number level for a given XP amount."""
    return NonNegativeInt(math.floor(get_raw_level(xp)))


def to_next_level(xp: NonNegativeInt) -> NonNegativeInt:
    """Calculate the XP needed to reach the next level."""
    # Add a small epsilon to handle floating point inaccuracies at level boundaries
    current_level = get_level(xp)
    next_level_target_xp = round((current_level + 1) ** 2.5 + 10)
    return next_level_target_xp - xp


# --- UDP Server Protocol ---
class LevelBotProtocol(asyncio.DatagramProtocol):
    """Handle incoming UDP packets for the leveling system."""

    def __init__(self, cog_instance: LevelingCog) -> None:
        self.cog = cog_instance
        super().__init__()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Process received data and grant XP."""
        try:
            if not ((addr[0] == "127.0.0.1" or addr[0].startswith("10.0.0.")) and len(data) == 8):  # User ID is 8 bytes wide
                log.warning("Found data from unprivileged client %s", addr)
                raise ValueError

            user_id = UserId(int(struct.unpack(">Q", data)[0]))
            # Schedule the coroutine to run on the bot's event loop
            asyncio.create_task(self.cog.grant_udp_xp(user_id))  # noqa: RUF006
        except ValueError, UnicodeDecodeError:
            log.warning("Received invalid UDP data: %s from %s", data, addr)

    def error_received(self, exc: Exception) -> None:
        log.error("UDP server error: %s", exc)

from typing import TYPE_CHECKING, Literal, NewType, TypeIs, cast

import discord

# --- Nominal Types for Discord IDs ---
# Using NewType creates distinct types that are not interchangeable.
# A function expecting a GuildId will raise a type error if given a UserId.
UserId = NewType("UserId", int)
GuildId = NewType("GuildId", int)
ChannelId = NewType("ChannelId", int)
RoleId = NewType("RoleId", int)
MessageId = NewType("MessageId", int)
type RoleIdList = list[RoleId]

# --- Semantic Type Aliases ---
# For complex types that appear in multiple places.
type UserGuildPair = tuple[UserId, GuildId]


# --- Literals for Closed Sets of Values ---
# Enforces that a variable must be one of these specific string values.
type ReminderPreference = Literal["ONCE", "ALWAYS", "NEVER"]
type AnalysisStatus = Literal["OK", "ERROR", "WARN"]

# A specific type for an inviter ID, which can be a real user or unknown (None).
type InviterId = UserId | None

# A new nominal type for integers that represent quantities and should be positive.
PositiveInt = NewType("PositiveInt", int)
NonNegativeInt = NewType("NonNegativeInt", int)


def is_positive(num: int) -> TypeIs[PositiveInt]:
    """Safely cast an int to a PositiveInt."""
    return num > 0


def is_non_negative(num: int) -> TypeIs[NonNegativeInt]:
    """Check if a number is a non-negative integer (>= 0)."""
    return num >= 0


# Define a more specific type for a message we know is from a guild
if TYPE_CHECKING:

    class GuildMessage(discord.Message):
        author: discord.Member = cast("discord.Member", None)
        guild: discord.Guild = cast("discord.Guild", None)


def is_guild_message(message: discord.Message) -> "TypeIs[GuildMessage]":
    """Check if a message is from a guild context."""
    return message.guild is not None and isinstance(message.author, discord.Member)

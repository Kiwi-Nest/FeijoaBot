from __future__ import annotations

import contextlib
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from modules.BotCore import BotCore

from modules.clean_string import sanitize_chat
from modules.dtypes import ChannelId, GuildId, UserId, is_guild_message

log = logging.getLogger(__name__)

WINDOW: float = 300.0
THRESHOLD: int = 3
TIMEOUT_DURATION: timedelta = timedelta(hours=1)


class ModSpamCog(commands.Cog):
    """Cog for auto-timing out users who spam identical messages across channels."""

    def __init__(self, bot: BotCore) -> None:
        self.bot = bot
        # (UserId, GuildId) → list of (normalized_content, message_type, ChannelId, monotonic_timestamp)
        self._log: dict[tuple[UserId, GuildId], list[tuple[str, discord.MessageType, ChannelId, float]]] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not is_guild_message(message) or message.author.bot or message.author.is_timed_out():
            return
        member = message.author

        # Check even empty messages because some scams only use images and we can't compare those
        content = sanitize_chat(message.content).casefold()
        guild = message.guild
        key = (UserId(member.id), GuildId(guild.id))
        now = time.monotonic()

        # Prune stale entries, then record this message.
        self._log[key] = [e for e in self._log.get(key, []) if now - e[3] <= WINDOW]
        self._log[key].append((content, message.type, ChannelId(message.channel.id), now))

        # Cap the total number of tracked keys to prevent unbounded growth.
        cap = len(self.bot.guilds) * 10
        if len(self._log) > cap:
            oldest = min(self._log, key=lambda k: self._log[k][-1][3])
            del self._log[oldest]

        distinct = {ch for c, mt, ch, _ in self._log[key] if c == content and mt == message.type}
        if len(distinct) < THRESHOLD:
            return

        with contextlib.suppress(discord.Forbidden):
            await message.delete()

        # --- Apply timeout ---
        if not guild.me.guild_permissions.moderate_members:
            self.bot.dispatch(
                "security_alert",
                guild_id=guild.id,
                risk_level="HIGH",
                details=(
                    "**Auto-Timeout Failed: Missing Permission**\n"
                    f"Cross-channel spam detected from {member.mention} but I lack "
                    "`Moderate Members` to apply a timeout."
                ),
                warning_type="modspam_no_permission",
            )
            return

        if member.is_timed_out():
            return

        # Clear before the await to prevent re-entrant re-trigger during HTTP round-trip.
        del self._log[key]

        try:
            await member.timeout(TIMEOUT_DURATION, reason="Auto: cross-channel spam")
            log.info("Auto-timed out %s in guild %s for cross-channel spam", member, guild.id)
        except discord.Forbidden:
            log.warning("Missing permissions to timeout %s in guild %s", member, guild.id)
            self.bot.dispatch(
                "security_alert",
                guild_id=guild.id,
                risk_level="HIGH",
                details=(
                    "**Auto-Timeout Failed: Forbidden**\n"
                    f"Cross-channel spam detected from {member.mention} but the timeout "
                    "request was rejected (role hierarchy or missing permission)."
                ),
                warning_type="modspam_forbidden",
            )
            return

        self.bot.dispatch(
            "security_alert",
            guild_id=guild.id,
            risk_level="HIGH",
            details=(
                f"**Auto-Timeout: Cross-Channel Spam**\n"
                f"{member.mention} sent the same message in {len(distinct)} channels.\n"
                f"Content: `{content[:100]}`\n"
                f"Duration: 1 hour"
            ),
            warning_type=f"modspam_{member.id}",
        )


async def setup(bot: BotCore) -> None:
    """Add the ModSpamCog to the bot."""
    await bot.add_cog(ModSpamCog(bot=bot))

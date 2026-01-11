from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from modules.ConfigDB import ConfigDB
    from modules.KiwiBot import KiwiBot

from modules.dtypes import GuildId, is_guild_message

log = logging.getLogger(__name__)


class ForwardCog(commands.Cog):
    """Cog for forwarding messages from a specific source."""

    def __init__(self, bot: KiwiBot, config_db: ConfigDB) -> None:
        self.bot = bot
        self.config_db = config_db

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Process messages to forward them when applicable."""
        # 1. Ensure the message is from a guild
        if not is_guild_message(message):
            return

        # 2. Get the guild-specific configuration
        config = await self.config_db.get_guild_config(GuildId(message.guild.id))

        # 3. Check if this guild has forwarding enabled
        if not config.qotd_source_bot_id or not config.qotd_target_channel_id:
            return

        # 4. Check if the message is from the configured source bot
        #    (Added check for message.embeds to prevent crash)
        if message.author.id == config.qotd_source_bot_id and message.embeds and (embed := message.embeds[0]):
            # 5. Get the configured target channel
            target_channel = self.bot.get_channel(config.qotd_target_channel_id)
            if not isinstance(target_channel, discord.TextChannel):
                try:
                    # Fallback to fetching if not in cache
                    target_channel = await self.bot.fetch_channel(config.qotd_target_channel_id)
                except (discord.NotFound, discord.Forbidden):
                    log.warning(
                        "Could not find or fetch qotd_target_channel_id %s for guild %s",
                        config.qotd_target_channel_id,
                        message.guild.id,
                    )
                    self.bot.dispatch(
                        "security_alert",
                        guild_id=message.guild.id,
                        risk_level="HIGH",
                        details=(
                            f"**Message Forwarder Failed**\n"
                            f"The message forwarder failed because the target channel (`{config.qotd_target_channel_id}`) "
                            "could not be found. It may have been deleted."
                        ),
                        warning_type="forwarder_channel_missing",
                    )
                    return

            # 6. Send the embed
            try:
                await target_channel.send(embed=embed.remove_footer())
            except discord.Forbidden:
                log.warning(
                    "Missing permissions to send forwarded embed in channel %s for guild %s",
                    target_channel.id,
                    message.guild.id,
                )
                self.bot.dispatch(
                    "security_alert",
                    guild_id=message.guild.id,
                    risk_level="HIGH",
                    details=(
                        f"**Message Forwarder Permission Error**\n"
                        f"I failed to forward an embed to {target_channel.mention} because I am "
                        "missing the `Send Messages` or `Embed Links` permission in that channel."
                    ),
                    warning_type="forwarder_permission",
                )


async def setup(bot: KiwiBot) -> None:
    """Add the ForwardCog to the bot."""
    await bot.add_cog(ForwardCog(bot=bot, config_db=bot.config_db))

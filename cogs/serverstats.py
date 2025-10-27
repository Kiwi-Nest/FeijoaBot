import logging

import discord
from discord.ext import tasks

from modules.dtypes import GuildId
from modules.guild_cog import GuildOnlyHybridCog
from modules.KiwiBot import KiwiBot

# Set up basic logging
log = logging.getLogger(__name__)

UPDATE_INTERVAL_MINUTES = 5


class ServerStats(GuildOnlyHybridCog):
    """A cog that automatically updates server statistics in designated voice channels."""

    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot
        self.update_stats.start()

    def cog_unload(self) -> None:
        """Clean up when the cog is unloaded."""
        self.update_stats.cancel()

    @tasks.loop(minutes=UPDATE_INTERVAL_MINUTES)
    async def update_stats(self) -> None:
        """Update the stats for all guilds."""
        for guild in self.bot.guilds:
            await self._update_guild_stats(guild)

    async def _update_guild_stats(self, guild: discord.Guild) -> None:
        """Handle the statistics update for a single guild."""
        # 1. Fetch the configuration for this specific guild
        config = await self.bot.config_db.get_guild_config(GuildId(guild.id))

        # 2. Get channel and role objects from the config IDs
        member_channel = guild.get_channel(config.member_count_channel_id) if config.member_count_channel_id else None
        tag_role = guild.get_role(config.tag_role_id) if config.tag_role_id else None
        tag_channel = guild.get_channel(config.tag_role_channel_id) if config.tag_role_channel_id else None

        # 3. Update Member Count Channel
        if isinstance(member_channel, discord.VoiceChannel):
            member_count = len([m for m in guild.members if not m.bot])
            new_name = f"All members: {member_count}"
            if member_channel.name != new_name:
                try:
                    await member_channel.edit(name=new_name, reason="Automated server stats update")
                    log.info(
                        "Updated 'All members' count for '%s' to %s.",
                        guild.name,
                        member_count,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Failed to update member count for guild %s", guild.name)
                    await self.bot.log_admin_warning(
                        guild_id=GuildId(guild.id),
                        warning_type="serverstats_fail",
                        description=(
                            f"I failed to update the Member Count channel ({member_channel.mention}).\n\n"
                            "**Reason**: `discord.Forbidden` or `discord.HTTPException`. "
                            "Please check my permissions in that channel (must have `Manage Channel` and `Connect`)."
                        ),
                        level="ERROR",
                    )

        # 4. Update Tag Role Count Channel
        if isinstance(tag_channel, discord.VoiceChannel) and tag_role:
            tag_members_count = len(tag_role.members)
            new_name = f"Tag Users: {tag_members_count}"
            if tag_channel.name != new_name:
                try:
                    await tag_channel.edit(name=new_name, reason="Automated server stats update")
                    log.info(
                        "Updated 'Tag Users' count for '%s' to %s.",
                        guild.name,
                        tag_members_count,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Failed to update tag role count for guild %s", guild.name)
                    await self.bot.log_admin_warning(
                        guild_id=GuildId(guild.id),
                        warning_type="serverstats_fail",
                        description=(
                            f"I failed to update the Tag Role Count channel ({tag_channel.mention}).\n\n"
                            "**Reason**: `discord.Forbidden` or `discord.HTTPException`. "
                            "Please check my permissions in that channel (must have `Manage Channel` and `Connect`)."
                        ),
                        level="ERROR",
                    )


async def setup(bot: KiwiBot) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(ServerStats(bot))

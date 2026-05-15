import logging
from typing import TYPE_CHECKING, Final

import discord
from discord import app_commands
from discord.ext import commands

from modules.discord_utils import InvalidRoleError, ping_online_role
from modules.dtypes import GuildId, RoleId
from modules.guild_cog import GuildOnlyHybridCog

if TYPE_CHECKING:
    from modules.BotCore import BotCore
    from modules.ConfigDB import ConfigDB
    from modules.UserDB import UserDB

log = logging.getLogger(__name__)
SECOND_COOLDOWN: Final[int] = 1


class PingRoleCog(GuildOnlyHybridCog):
    """Allow users to ping admin-configured event and game roles."""

    def __init__(self, bot: BotCore, *, user_db: UserDB, config_db: ConfigDB) -> None:
        self.bot = bot
        self.user_db = user_db
        self.config_db = config_db

    async def role_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        config = await self.config_db.get_guild_config(GuildId(interaction.guild.id))
        roles = [
            role
            for r_id in (config.event_ping_roles or [])
            if (role := interaction.guild.get_role(r_id)) and current.lower() in role.name.lower()
        ]
        roles.sort(key=lambda r: len(r.members), reverse=True)
        return [app_commands.Choice(name=r.name, value=str(r.id)) for r in roles][:25]

    @commands.hybrid_command(name="pingrole", description="Ping an event or game role.")
    @commands.cooldown(3, SECOND_COOLDOWN * 60, commands.BucketType.user)
    @app_commands.describe(role="The event or game role to ping.")
    @app_commands.autocomplete(role=role_autocomplete)
    async def pingrole(self, ctx: commands.Context, *, role: str) -> None:
        """Ping active members of a configured event or game role."""
        guild_id = GuildId(ctx.guild.id)
        config = await self.config_db.get_guild_config(guild_id)
        event_roles = config.event_ping_roles or []

        if not event_roles:
            await ctx.send("No pingable roles have been configured for this server.", ephemeral=True)
            return

        resolved = ctx.guild.get_role(int(role)) if role.isdigit() else discord.utils.get(ctx.guild.roles, name=role)

        if resolved is None or RoleId(resolved.id) not in event_roles:
            available = " ".join(f"<@&{r}>" for r in event_roles)
            await ctx.send(f"That role isn't available for pinging. Available: {available}", ephemeral=True)
            return

        try:
            ping_text = await ping_online_role(resolved, self.user_db)
        except InvalidRoleError:
            await ctx.send("❌ That role cannot be pinged.", ephemeral=True)
            return

        await ctx.send(f"{ctx.author.mention} is looking for **{resolved.name}**\n{ping_text}")


async def setup(bot: BotCore) -> None:
    """Load the cog."""
    await bot.add_cog(PingRoleCog(bot, user_db=bot.user_db, config_db=bot.config_db))

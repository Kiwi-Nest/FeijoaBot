from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    import discord
    from discord.ext.commands import Context


class GuildOnlyHybridCog(commands.Cog):
    """A cog where all commands (prefix and app) are guild-only by using cog-wide checks."""

    # 1. Check for the PREFIX command side
    async def cog_check(self, ctx: Context) -> bool:
        """Check if all prefix commands in this cog are used in a guild."""
        if ctx.guild is None:
            # logging.warning("User %s tried %s in DMs.", ctx.author, ctx.command)
            raise commands.NoPrivateMessage
        return True

    # 2. Check for the APPLICATION command side
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if all app commands in this cog are used in a guild."""
        if interaction.guild is None:
            # logging.warning("User %s tried %s in DMs.", interaction.user, interaction.command)
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False
        return True

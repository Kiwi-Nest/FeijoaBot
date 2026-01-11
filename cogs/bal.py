import logging
from typing import TYPE_CHECKING, Final

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, UserId
from modules.enums import StatName
from modules.guild_cog import GuildOnlyHybridCog

if TYPE_CHECKING:
    from modules.KiwiBot import KiwiBot
    from modules.UserDB import UserDB

log = logging.getLogger(__name__)
SECOND_COOLDOWN: Final[int] = 1


class Bal(GuildOnlyHybridCog):
    bot: KiwiBot

    def __init__(self, bot: KiwiBot, user_db: UserDB) -> None:
        self.bot = bot
        self.user_db = user_db

    @commands.hybrid_command(name="bal", description="Displays a user's balance")
    @commands.cooldown(2, SECOND_COOLDOWN, commands.BucketType.user)
    @app_commands.describe(member="User whose balance to show")
    async def bal(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        # If no member is provided, default to the command author.
        target_member = member or ctx.author

        user_id = UserId(target_member.id)
        guild_id = GuildId(ctx.guild.id)

        currency_balance = await self.user_db.get_stat(user_id, guild_id, StatName.CURRENCY)
        bump_count = await self.user_db.get_stat(user_id, guild_id, StatName.BUMPS)
        description = f"{target_member.mention}\nWallet: ${currency_balance:,}"
        if bump_count > 0:
            description += f"\nBumps: {bump_count}"

        embed = discord.Embed(
            title="Balance",
            description=description,
            color=discord.Colour.green(),
        )
        embed.set_author(name=target_member.name, icon_url=target_member.display_avatar)
        embed.set_footer(text=f"{ctx.author.display_name} | Balance")
        embed.timestamp = discord.utils.utcnow()
        await ctx.send(embed=embed)
        log.info(
            "Bal command executed by %s for %s.",
            ctx.author.display_name,
            target_member.display_name,
        )


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(Bal(bot=bot, user_db=bot.user_db))

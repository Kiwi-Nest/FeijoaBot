import logging
import pathlib
import random
from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    from modules.BotCore import BotCore

log = logging.getLogger(__name__)


class Starter(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot

    @commands.hybrid_command(name="starter", description="Get a random conversation starter")
    @commands.cooldown(1, 1, commands.BucketType.user)
    async def starter(self, ctx: commands.Context) -> None:
        with pathlib.Path("data/conversation_starters.txt").open() as f:
            starters = [line.strip() for line in f if line.strip()]

        await ctx.send(random.choice(starters))
        log.info("Starter command executed by %s.", ctx.author.display_name)


async def setup(bot: BotCore) -> None:
    await bot.add_cog(Starter(bot))

import logging
import os
import random
import string
from typing import Final

from discord import Object, app_commands
from discord.ext import commands

from modules.dtypes import GuildId, PositiveInt, UserId
from modules.guild_cog import GuildOnlyHybridCog
from modules.KiwiBot import KiwiBot

log = logging.getLogger(__name__)

# --- Configuration ---
# Define your guild ID here. Using a constant is best practice.
GUILD_ID: Final[int] = int(os.getenv("SWL_GUILD_ID", "0"))
GUILD: Final[Object] = Object(GUILD_ID)
COOLDOWN: Final[int] = 3600 * 6  # 6 hours


class Harvest(GuildOnlyHybridCog):
    LIMBS: Final[tuple[str, ...]] = (
        "Left Arm",
        "Right Arm",
        "Left Hand",
        "Right Hand",
        "Head",
        "Torso",
    )
    ORGANS: Final[tuple[str, ...]] = (
        "Brain",
        "Heart",
        "Left Kidney",
        "Right Kidney",
        "Left Lung",
        "Right Lung",
        "Liver",
        "Bone Marrow",
    )

    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot

    async def _process_sale(
        self,
        ctx: commands.Context,
        item: str | None,
        item_list: tuple[str, ...],
        action_name: str,
    ) -> None:
        """Handle the sale/harvest logic.

        Args:
            ctx: The command context.
            item: The specific item to take/harvest, or None to choose randomly.
            item_list: The tuple of available items.
            action_name: The name of the action being performed.

        """
        # Verify that the command is used in the correct guild.
        if ctx.guild is None or ctx.guild.id != self.bot.config.swl_guild_id:
            await ctx.send("Shh don't worry about it.")
            return

        if item is None:
            item = random.choice(item_list)

        item = string.capwords(item.replace("_", " "))
        if item not in item_list:
            item_type = "limb" if item_list is self.LIMBS else "organ"
            await ctx.send(f"Invalid {item_type}", ephemeral=True)
            return

        if random.choice((True, False)):
            await ctx.send("You got caught by the police and made no money.")
            return

        random_num = PositiveInt(random.randint(1, 20))
        guild_id = GuildId(ctx.guild.id)
        user_id = UserId(ctx.author.id)
        await self.bot.user_db.mint_currency(
            user_id=user_id,
            guild_id=guild_id,
            amount=random_num,
            event_reason="HARVEST_SALE",
            ledger_db=self.bot.ledger_db,
            initiator_id=user_id,
        )

        log.info(
            "User %s has sold wndx2's %s for %d.",
            ctx.author.display_name,
            item.lower(),
            random_num,
        )
        await ctx.send(
            f"{ctx.author.mention}, you sold wndx2's {item.lower()} for ${random_num}.",
        )

        log.info(
            "%s command executed by %s.",
            action_name.capitalize(),
            ctx.author.display_name,
        )

    @commands.hybrid_command(name="take", description="Take one of wndx2's limbs")
    @commands.cooldown(1, COOLDOWN, commands.BucketType.user)
    @app_commands.describe(limb="Limb to take")
    @app_commands.choices(
        limb=[app_commands.Choice(name=limb, value=limb.lower()) for limb in LIMBS],
    )
    async def take(self, ctx: commands.Context, limb: str | None = None) -> None:
        """Take a limb."""
        await self._process_sale(ctx, limb, self.LIMBS, "take")

    @commands.hybrid_command(
        name="harvest",
        description="Harvest one of wndx2's organs",
    )
    @commands.cooldown(1, COOLDOWN, commands.BucketType.user)
    @app_commands.describe(organ="Organ to harvest")
    @app_commands.choices(
        organ=[app_commands.Choice(name=organ, value=organ.lower()) for organ in ORGANS],
    )
    async def harvest(self, ctx: commands.Context, organ: str | None = None) -> None:
        """Harvest an organ."""
        await self._process_sale(ctx, organ, self.ORGANS, "harvest")


async def setup(bot: KiwiBot) -> None:
    """Set up the cog."""
    if not bot.config.swl_guild_id:
        log.error(
            "Harvest cog not loaded. Missing 'SWL_GUILD_ID'.",
        )
        return
    await bot.add_cog(Harvest(bot), guild=Object(bot.config.swl_guild_id))

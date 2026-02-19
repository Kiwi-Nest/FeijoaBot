import logging
from typing import Final

import discord
from discord import app_commands
from discord.ext import commands

from modules.CurrencyLedgerDB import CurrencyLedgerDB
from modules.dtypes import GuildId, UserId, is_positive
from modules.enums import StatName
from modules.exceptions import InsufficientFundsError
from modules.guild_cog import GuildOnlyHybridCog
from modules.KiwiBot import KiwiBot
from modules.UserDB import UserDB

log = logging.getLogger(__name__)
SECOND_COOLDOWN: Final[int] = 1


class Donate(GuildOnlyHybridCog):
    def __init__(self, bot: KiwiBot, user_db: UserDB, ledger_db: CurrencyLedgerDB) -> None:
        self.bot = bot
        self.user_db = user_db
        self.ledger_db = ledger_db

    @commands.hybrid_command(
        name="donate",
        description="Donate to the poor",
        aliases=["give"],
    )
    @commands.cooldown(2, SECOND_COOLDOWN, commands.BucketType.user)
    @app_commands.describe(receiver="User you want to donate to")
    @app_commands.describe(amount="Amount to donate")
    async def donate(
        self,
        ctx: commands.Context,
        receiver: discord.Member,
        amount: commands.Range[int, 1],
    ) -> None:
        # Optional: Add checks to prevent donating to self or bots
        if receiver.id == ctx.author.id:
            await ctx.send("You cannot donate to yourself.", ephemeral=True)
            return

        guild_id = GuildId(ctx.guild.id)
        sender_id = UserId(ctx.author.id)
        receiver_id = UserId(receiver.id)

        if (balance := await self.user_db.get_stat(sender_id, guild_id, StatName.CURRENCY)) < amount:
            msg = f"Insufficient funds! You have ${balance}"
            raise InsufficientFundsError(msg)

        if not is_positive(amount):
            # This branch is logically unreachable due to commands.Range,
            # but it satisfies the type checker.
            await ctx.send("Amount must be positive.", ephemeral=True)
            return

        success = await self.user_db.transfer_currency(
            sender_id=sender_id,
            receiver_id=receiver_id,
            guild_id=guild_id,
            amount=amount,
            ledger_db=self.ledger_db,
        )

        if success:
            await ctx.send(
                f"{ctx.author.mention} donated ${amount} to {receiver.mention}.",
            )
        else:
            await ctx.send("Transaction failed. Please try again.")

        log.info("Donate command executed by %s.\n", ctx.author.display_name)


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(Donate(bot=bot, user_db=bot.user_db, ledger_db=bot.ledger_db))

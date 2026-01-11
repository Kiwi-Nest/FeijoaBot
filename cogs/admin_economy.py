from typing import TYPE_CHECKING

import discord
from discord import app_commands

from modules.dtypes import GuildId, GuildInteraction, NonNegativeInt, PositiveInt, UserId
from modules.exceptions import UserError
from modules.guild_cog import GuildOnlyHybridCog

if TYPE_CHECKING:
    from modules.CurrencyLedgerDB import CurrencyLedgerDB
    from modules.KiwiBot import KiwiBot
    from modules.UserDB import UserDB


class AdminEconomy(GuildOnlyHybridCog):
    def __init__(self, bot: KiwiBot, *, user_db: UserDB, ledger_db: CurrencyLedgerDB) -> None:
        self.bot = bot
        self.user_db = user_db
        self.ledger_db = ledger_db

    group = app_commands.Group(
        name="economy",
        description="Admin economy management",
        default_permissions=discord.Permissions(administrator=True),
    )

    @group.command(name="set", description="Set a user's exact cash balance.")
    async def set_balance(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        amount: app_commands.Range[int, 0, 1_000_000],
    ) -> None:
        """Set a user's balance to a specific amount."""
        await self.user_db.set_currency_balance_and_log(
            UserId(member.id),
            GuildId(interaction.guild.id),
            NonNegativeInt(amount),
            "ADMIN_SET",
            self.ledger_db,
            UserId(interaction.user.id),
        )
        await interaction.response.send_message(f"✅ Set {member.mention}'s balance to ${amount:,}.")

    @group.command(name="mint", description="Print money for a user.")
    async def mint(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 100_000],
    ) -> None:
        """Mint new currency for a user."""
        new_bal = await self.user_db.mint_currency(
            UserId(member.id),
            GuildId(interaction.guild.id),
            PositiveInt(amount),
            "ADMIN_MINT",
            self.ledger_db,
            UserId(interaction.user.id),
        )
        await interaction.response.send_message(
            f"✅ Minted ${amount:,} for {member.mention}. New Balance: ${new_bal:,}",
        )

    @group.command(name="burn", description="Destroy money from a user.")
    async def burn(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 100_000],
    ) -> None:
        """Burn currency from a user."""
        new_bal = await self.user_db.burn_currency(
            UserId(member.id),
            GuildId(interaction.guild.id),
            PositiveInt(amount),
            "ADMIN_REMOVE",
            self.ledger_db,
            UserId(interaction.user.id),
        )
        if new_bal is None:
            msg = f"User has insufficient funds to burn ${amount:,}."
            raise UserError(msg)

        await interaction.response.send_message(
            f"🔥 Burned ${amount:,} from {member.mention}. New Balance: ${new_bal:,}",
        )

    @group.command(
        name="wealth-tax",
        description="Apply a progressive tax (val^x) to Cash AND Stocks.",
    )
    @app_commands.describe(exponent="Power to apply (e.g. 0.9). Values < 1 reduce wealth.")
    async def wealth_tax(
        self,
        interaction: GuildInteraction,
        exponent: app_commands.Range[float, 0.9, 0.99],
    ) -> None:
        """Apply a server-wide wealth tax."""
        await interaction.response.defer()

        users, burned = await self.user_db.apply_wealth_tax(
            GuildId(interaction.guild.id),
            exponent,
            self.ledger_db,
            UserId(interaction.user.id),
        )

        await interaction.followup.send(
            f"""📉 **Global Wealth Tax Applied**\nFormula: `Value = Value ^ {exponent}`
**Affected:** {users} users\n**Total Vaporized:** ${burned:,} (Cash & Collateral)""",
        )


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(AdminEconomy(bot, user_db=bot.user_db, ledger_db=bot.ledger_db))

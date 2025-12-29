import discord
from discord import app_commands

from modules.dtypes import GuildId, PositiveInt, UserId
from modules.guild_cog import GuildOnlyHybridCog
from modules.KiwiBot import KiwiBot


class AdminEconomy(GuildOnlyHybridCog):
    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot

    group = app_commands.Group(name="economy", description="Admin economy management")

    @group.command(name="set", description="Set a user's exact cash balance.")
    @app_commands.default_permissions(administrator=True)
    async def set_balance(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 0, 1_000_000],
    ) -> None:
        """Set a user's balance to a specific amount."""
        await self.bot.user_db.set_currency_balance_and_log(
            UserId(member.id),
            GuildId(interaction.guild.id),
            amount,
            "ADMIN_SET",
            self.bot.ledger_db,
            UserId(interaction.user.id),
        )
        await interaction.response.send_message(f"✅ Set {member.mention}'s balance to ${amount:,}.", ephemeral=True)

    @group.command(name="mint", description="Print money for a user.")
    @app_commands.default_permissions(administrator=True)
    async def mint(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 100_000],
    ) -> None:
        """Mint new currency for a user."""
        new_bal = await self.bot.user_db.mint_currency(
            UserId(member.id),
            GuildId(interaction.guild.id),
            PositiveInt(amount),
            "ADMIN_MINT",
            self.bot.ledger_db,
            UserId(interaction.user.id),
        )
        await interaction.response.send_message(
            f"✅ Minted ${amount:,} for {member.mention}. New Balance: ${new_bal:,}",
            ephemeral=True,
        )

    @group.command(name="burn", description="Destroy money from a user.")
    @app_commands.default_permissions(administrator=True)
    async def burn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 100_000],
    ) -> None:
        """Burn currency from a user."""
        new_bal = await self.bot.user_db.burn_currency(
            UserId(member.id),
            GuildId(interaction.guild.id),
            PositiveInt(amount),
            "ADMIN_REMOVE",
            self.bot.ledger_db,
            UserId(interaction.user.id),
        )
        if new_bal is None:
            await interaction.response.send_message("❌ User has insufficient funds.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"🔥 Burned ${amount:,} from {member.mention}. New Balance: ${new_bal:,}",
                ephemeral=True,
            )

    @group.command(
        name="wealth-tax",
        description="Apply a progressive tax (val^x) to Cash AND Stocks.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(exponent="Power to apply (e.g. 0.9). Values < 1 reduce wealth.")
    async def wealth_tax(
        self,
        interaction: discord.Interaction,
        exponent: app_commands.Range[float, 0.9, 0.99],
    ) -> None:
        """Apply a server-wide wealth tax."""
        await interaction.response.defer()

        users, burned = await self.bot.user_db.apply_wealth_tax(
            GuildId(interaction.guild.id),
            exponent,
            self.bot.ledger_db,
            UserId(interaction.user.id),
        )

        await interaction.followup.send(
            f"""📉 **Global Wealth Tax Applied**\nFormula: `Value = Value ^ {exponent}`
**Affected:** {users} users\n**Total Vaporized:** ${burned:,} (Cash & Collateral)""",
        )


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(AdminEconomy(bot))

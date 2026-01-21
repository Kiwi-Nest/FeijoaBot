import contextlib
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, GuildInteraction, NonNegativeInt, PositiveInt, UserId
from modules.exceptions import UserError

if TYPE_CHECKING:
    from modules.ConfigDB import ConfigDB
    from modules.CurrencyLedgerDB import CurrencyLedgerDB
    from modules.KiwiBot import KiwiBot
    from modules.UserDB import UserDB


@commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.cooldown(5, 10.0, key=lambda i: (i.guild_id, i.user.id))
class AdminEconomy(
    commands.GroupCog,
    group_name="economy",
    group_description="Admin economy management",
):
    def __init__(
        self,
        bot: KiwiBot,
        *,
        user_db: UserDB,
        ledger_db: CurrencyLedgerDB,
        config_db: ConfigDB,
    ) -> None:
        self.bot = bot
        self.user_db = user_db
        self.ledger_db = ledger_db
        self.config_db = config_db
        super().__init__()

    async def _log_economy_action(
        self,
        *,
        title: str,
        color: discord.Colour,
        member: discord.Member | discord.User | None = None,
        moderator: discord.Member | discord.User,
        amount: str,
        guild_id: GuildId,
        reason: str | None = None,
        details: str | None = None,
    ) -> None:
        """Log an economy action to the moderation log channel."""
        config = await self.config_db.get_guild_config(guild_id)
        mod_channel_id = config.mod_log_channel_id
        if not mod_channel_id:
            return

        mod_channel = self.bot.get_channel(mod_channel_id)
        if not isinstance(mod_channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        if member:
            name = f"{member.name} ({member.display_name})"
            embed.set_author(name=name, icon_url=member.display_avatar)
            embed.add_field(name="Target", value=member.mention, inline=True)

        embed.add_field(name="Moderator", value=moderator.mention, inline=True)
        embed.add_field(name="Amount", value=amount, inline=True)

        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)

        if details:
            embed.description = details

        # Fail silently if we can't log, as the action itself succeeded
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await mod_channel.send(embed=embed, allowed_mentions=None)

    @app_commands.command(name="set", description="Set a user's exact cash balance.")
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
        await self._log_economy_action(
            title="Economy: Set Balance",
            color=discord.Colour.blue(),
            member=member,
            moderator=interaction.user,
            amount=f"${amount:,}",
            guild_id=GuildId(interaction.guild.id),
        )
        await interaction.response.send_message(f"✅ Set {member.mention}'s balance to ${amount:,}.")

    @app_commands.command(name="mint", description="Print money for a user.")
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
        await self._log_economy_action(
            title="Economy: Mint",
            color=discord.Colour.green(),
            member=member,
            moderator=interaction.user,
            amount=f"${amount:,}",
            guild_id=GuildId(interaction.guild.id),
            details=f"**New Balance:** ${new_bal:,}",
        )
        await interaction.response.send_message(
            f"✅ Minted ${amount:,} for {member.mention}. New Balance: ${new_bal:,}",
        )

    @app_commands.command(name="burn", description="Destroy money from a user.")
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

        await self._log_economy_action(
            title="Economy: Burn",
            color=discord.Colour.brand_red(),
            member=member,
            moderator=interaction.user,
            amount=f"${amount:,}",
            guild_id=GuildId(interaction.guild.id),
            details=f"**New Balance:** ${new_bal:,}",
        )

        await interaction.response.send_message(
            f"🔥 Burned ${amount:,} from {member.mention}. New Balance: ${new_bal:,}",
        )

    @app_commands.command(
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

        await self._log_economy_action(
            title="Economy: Wealth Tax",
            color=discord.Colour.orange(),
            moderator=interaction.user,
            amount=f"${burned:,}",
            guild_id=GuildId(interaction.guild.id),
            details=(f"**Algorithm:** `Value ^ {exponent}`\n**Users Affected:** {users}\n**Total Removed:** ${burned:,}"),
        )

        await interaction.followup.send(
            f"""📉 **Progressive Wealth Tax Applied**\nFormula: `Value = Value ^ {exponent}`
**Affected:** {users} users\n**Total Vaporized:** ${burned:,} (Cash & Collateral)""",
        )


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(
        AdminEconomy(
            bot,
            user_db=bot.user_db,
            ledger_db=bot.ledger_db,
            config_db=bot.config_db,
        ),
    )

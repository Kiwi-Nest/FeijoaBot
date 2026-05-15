"""User privacy commands: /my-data (GDPR Art. 15) and /forget-me (GDPR Art. 17)."""

import asyncio
import logging
from typing import TYPE_CHECKING, Final

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, UserId

SECOND_COOLDOWN: Final[int] = 1

if TYPE_CHECKING:
    from modules.BotCore import BotCore
    from modules.ConfigDB import ConfigDB
    from modules.PrivacyDB import PrivacyDB

log = logging.getLogger(__name__)


class ConfirmErasureView(discord.ui.View):
    """Confirmation view for irreversible user erasure."""

    def __init__(self, user_id: UserId, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.confirmed = False

    @discord.ui.button(label="Confirm Deletion", style=discord.ButtonStyle.red)
    async def confirm_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Confirm user erasure."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the user who initiated erasure can confirm.", ephemeral=True)
            return

        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    async def on_timeout(self) -> None:
        """Handle view timeout (do nothing, user didn't confirm)."""
        log.debug("Erasure confirmation view timed out for user %s", self.user_id)


@app_commands.checks.cooldown(2, SECOND_COOLDOWN * 30, key=lambda i: i.user.id)
class Privacy(commands.Cog):
    """Privacy commands for GDPR compliance: /my-data and /forget-me."""

    bot: BotCore

    def __init__(self, bot: BotCore, privacy_db: PrivacyDB, config_db: ConfigDB) -> None:
        self.bot = bot
        self.privacy_db = privacy_db
        self.config_db = config_db

    @app_commands.command(
        name="my-data",
        description="Export all personal data stored for your Discord account (GDPR Art. 15)",
    )
    async def my_data(self, interaction: discord.Interaction) -> None:
        """Fetch and display all user data in an ephemeral embed."""
        user_id = UserId(interaction.user.id)

        await interaction.response.defer(ephemeral=True)

        try:
            data = await self.privacy_db.get_user_data(user_id)

            embed = discord.Embed(
                title="Your Personal Data",
                description="All data stored for your account across all servers.",
                color=discord.Colour.blue(),
            )

            # Guild memberships
            if data.guilds:
                guild_info = "\n".join(
                    [
                        f"Guild {row.guild_id}: {row.currency} currency, {row.xp} XP, {row.bumps} bumps, level {row.level}"
                        for row in data.guilds
                    ],
                )
                embed.add_field(
                    name=f"Guild Memberships ({len(data.guilds)})",
                    value=guild_info[:1024],
                    inline=False,
                )
            else:
                embed.add_field(name="Guild Memberships", value="None", inline=False)

            # Invites
            if data.invites:
                invite_info = f"{len(data.invites)} guilds joined"
                embed.add_field(name="Invites", value=invite_info, inline=False)
            else:
                embed.add_field(name="Invites", value="None", inline=False)

            # Reminders
            if data.reminders:
                reminder_count = len(data.reminders)
                embed.add_field(
                    name="Reminders",
                    value=f"{reminder_count} active reminders",
                    inline=False,
                )
            else:
                embed.add_field(name="Reminders", value="None", inline=False)

            # Trading positions
            if data.positions:
                position_info = f"{len(data.positions)} open positions"
                embed.add_field(name="Trading Positions", value=position_info, inline=False)
            else:
                embed.add_field(name="Trading Positions", value="None", inline=False)

            # Voice activity
            if data.voice and data.voice.total_minutes > 0:
                embed.add_field(
                    name="Voice Activity",
                    value=(
                        f"{data.voice.total_minutes} minutes recorded\n"
                        f"Last seen: {data.voice.last_seen}\n"
                        f"Most active day: {data.voice.peak_day}"
                    ),
                    inline=False,
                )
            else:
                embed.add_field(name="Voice Activity", value="None", inline=False)

            embed.set_footer(text="Use /forget-me to request erasure of all data.")
            embed.timestamp = discord.utils.utcnow()

            await interaction.followup.send(embed=embed, ephemeral=True)
            log.info("User %s requested data export", user_id)

        except Exception:
            log.exception("Error fetching user data for %s", user_id)
            await interaction.followup.send(
                "Failed to fetch your data. Please try again later.",
                ephemeral=True,
            )

    @app_commands.command(
        name="forget-me",
        description="Permanently delete all personal data (GDPR Art. 17 - Right to Erasure)",
    )
    async def forget_me(self, interaction: discord.Interaction) -> None:
        """Request irreversible user erasure with confirmation."""
        user_id = UserId(interaction.user.id)

        # Pre-fetch guild list to show user what will be deleted
        try:
            guild_ids = await self.privacy_db.get_user_guild_ids(user_id)
        except Exception:
            log.exception("Error fetching guild list for user %s", user_id)
            await interaction.response.send_message(
                "Failed to process your request. Please try again later.",
                ephemeral=True,
            )
            return

        # Show confirmation view
        guild_count = len(guild_ids)
        view = ConfirmErasureView(user_id)
        embed = discord.Embed(
            title="⚠️ Permanent Data Deletion",
            description=(
                f"This will permanently delete your account data from {guild_count} "
                "server(s).\n\n**This action cannot be undone.**"
            ),
            color=discord.Colour.red(),
        )
        embed.add_field(
            name="What will be deleted:",
            value=(
                "• Account stats (currency, XP, bumps, level)\n"
                "• Invite records\n• Reminders\n• Trading positions\n"
                "• Voice chat presence snapshots"
            ),
            inline=False,
        )
        embed.add_field(
            name="What won't be deleted:",
            value="• Transaction ledger (kept for audit purposes)",
            inline=False,
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        # Wait for confirmation
        await view.wait()

        if not view.confirmed:
            await interaction.followup.send("Erasure cancelled. Your data has been preserved.", ephemeral=True)
            return

        # Perform erasure
        try:
            report = await self.privacy_db.erase_user(user_id)

            # Build result embed
            result_embed = discord.Embed(
                title="✅ Data Erasure Complete",
                description="Your personal data has been permanently deleted.",
                color=discord.Colour.green(),
            )
            result_embed.add_field(
                name="Deleted records",
                value=(
                    f"Guild memberships: {report.users}\n"
                    f"Reminders: {report.reminders}\n"
                    f"Invites: {report.invites}\n"
                    f"Trade positions: {report.positions}\n"
                    f"VC sessions deleted: {report.voicechat_sessions_deleted}"
                ),
                inline=False,
            )
            result_embed.set_footer(text="This action has been logged and cannot be reversed.")
            result_embed.timestamp = discord.utils.utcnow()

            await interaction.followup.send(embed=result_embed, ephemeral=True)

            # Notify mod_log of each affected guild (fire-and-forget)
            for guild_id in guild_ids:
                task = asyncio.create_task(
                    self._notify_guild_mod_log(user_id, GuildId(guild_id)),
                )
                # Keep reference to prevent task from being garbage collected
                task.add_done_callback(lambda _: None)

            log.info(
                "User %s erasure completed: %d guilds, %d reminders, %d invites, %d positions",
                user_id,
                report.users,
                report.reminders,
                report.invites,
                report.positions,
            )

        except Exception:
            log.exception("Error during erasure for user %s", user_id)
            await interaction.followup.send(
                "An error occurred during erasure. Please contact support.",
                ephemeral=True,
            )

    async def _notify_guild_mod_log(self, user_id: UserId, guild_id: GuildId) -> None:
        """Post a notification to a guild's mod_log channel about user erasure."""
        try:
            config = await self.config_db.get_guild_config(guild_id)
            if not config.mod_log_channel_id:
                return

            channel = self.bot.get_channel(config.mod_log_channel_id)
            if not isinstance(channel, discord.TextChannel):
                return

            embed = discord.Embed(
                title="User Data Erasure",
                description=f"User <@{user_id}> has requested permanent erasure of their data.",
                color=discord.Colour.orange(),
            )
            embed.timestamp = discord.utils.utcnow()
            await channel.send(embed=embed)

        except discord.HTTPException, discord.Forbidden:
            log.warning(
                "Failed to notify mod_log for guild %s of user %s erasure",
                guild_id,
                user_id,
            )


async def setup(bot: BotCore) -> None:
    """Load the privacy cog."""
    await bot.add_cog(Privacy(bot=bot, privacy_db=bot.privacy_db, config_db=bot.config_db))

import asyncio
import logging
import string
import time
from typing import TYPE_CHECKING, Final

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, GuildInteraction, NonNegativeInt, PositiveInt, UserGuildPair, UserId
from modules.enums import StatName

# Import the refactored logic and helpers
from modules.guild_cog import GuildOnlyHybridCog
from modules.leveling_utils import LevelBotProtocol, get_level, to_next_level
from modules.security_utils import SecurityCheckError, ensure_bot_hierarchy, ensure_role_safety

if TYPE_CHECKING:
    from modules.ConfigDB import ConfigDB
    from modules.KiwiBot import KiwiBot
    from modules.UserDB import UserDB

log = logging.getLogger(__name__)

# UDP Server Configuration
UDP_HOST: Final[str] = "127.0.0.1"

# --- Constants ---
COOLDOWN_SECONDS: Final[int] = 5 * 60
LONG_ABSENCE_BONUS_HOURS: Final[int] = 6
lowercase_letters = set(string.ascii_lowercase)


class LevelingCog(GuildOnlyHybridCog):
    """Handle the leveling system, including XP gain, ranks, and leaderboards."""

    # Define the parent group for all leveling commands
    level = app_commands.Group(name="level", description="Commands for the leveling system.")

    def __init__(
        self,
        bot: KiwiBot,
        *,
        udp_port: int | None,
        privileged_guild_id: (GuildId | None),  # Special case for UDP, keep this global guild ID
        user_db: UserDB,
        config_db: ConfigDB,
    ) -> None:
        self.bot = bot
        self.user_db = user_db
        self.config_db = config_db
        self.last_activity_timestamps: dict[UserGuildPair, float] = {}
        self.udp_transport: asyncio.DatagramTransport | None = None
        self.udp_port = udp_port
        self.privileged_guild_id = privileged_guild_id  # For UDP special case

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Handle errors for all commands in this Cog."""
        # Unwrap CommandInvokeError if present
        original_error = error.original if isinstance(error, app_commands.CommandInvokeError) else error

        if isinstance(original_error, SecurityCheckError):
            await interaction.response.send_message(
                "⚠️ This command is misconfigured due to a security risk. Please contact an admin.",
                ephemeral=True,
            )
            # Log this for the admin, as it's a critical misconfiguration
            # Determine the appropriate warning type based on the command
            command_name = interaction.command.name if interaction.command else "unknown"

            self.bot.dispatch(
                "security_alert",
                guild_id=interaction.guild.id,
                risk_level="HIGH",
                details=f"**Level Command Blocked**\nThe `/level {command_name}` command was blocked: {original_error}",
                warning_type="role_hierarchy_error",
            )
        elif isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                f"❌ You do not have the required permissions: {', '.join(error.missing_permissions)}",
                ephemeral=True,
            )
        elif isinstance(error, app_commands.AppCommandError):
            # Generic AppCommandError (e.g., from transformers or validators)
            await interaction.response.send_message(str(error), ephemeral=True)
        else:
            log.exception("Unhandled error in Leveling cog: %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.",
                    ephemeral=True,
                )

    async def cog_load(self) -> None:
        """Start the UDP server when the cog is loaded."""
        if not self.udp_port:
            return  # Do not start if the port is not configured.

        loop = asyncio.get_running_loop()
        try:
            self.udp_transport, _ = await loop.create_datagram_endpoint(
                lambda: LevelBotProtocol(self),
                local_addr=(UDP_HOST, self.udp_port),
            )
            log.info("Leveling UDP server started on %s:%d.", UDP_HOST, self.udp_port)
        except OSError:
            log.exception("Failed to start leveling UDP server")

    async def cog_unload(self) -> None:
        """Stop the UDP server when the cog is unloaded."""
        if self.udp_transport:
            self.udp_transport.close()
            log.info("Leveling UDP server stopped.")

    def _get_addable_xp(self, user_id: UserId, guild_id: GuildId) -> NonNegativeInt:
        """Determine if a user is eligible for XP based on cooldowns."""
        now = time.time()
        user_key = (user_id, guild_id)
        seconds_since_last = now - self.last_activity_timestamps.get(user_key, 0)

        if seconds_since_last > COOLDOWN_SECONDS:
            self.last_activity_timestamps[user_key] = now
            # Bonus for being away for more than 6 hours
            if seconds_since_last > (LONG_ABSENCE_BONUS_HOURS * 3600):  # 6 hours in seconds
                return NonNegativeInt(4)  # Bonus for long absence
            return NonNegativeInt(1)
        return NonNegativeInt(0)

    async def _handle_level_up_announcement(
        self,
        user_id: UserId,
        guild_id: GuildId,
        new_level: NonNegativeInt,
        new_xp: NonNegativeInt,
        source: str,
    ) -> None:
        """Format and send a level-up announcement to the configured channel."""
        config = await self.config_db.get_guild_config(guild_id)
        level_up_channel_id = config.level_up_channel_id

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = None
        if level_up_channel_id:
            channel = guild.get_channel(level_up_channel_id)

        # 2. If no channel yet, fall back to searching by name in any guild
        if not channel:
            channel = discord.utils.find(lambda c: "level" in c.name.lower(), guild.text_channels)

        if not isinstance(channel, discord.TextChannel):
            if not level_up_channel_id:
                log.debug(
                    "No level-up announcement channel configured or found in guild %s.",
                    guild.name,
                )
            return
        user = self.bot.get_user(user_id)
        if not user:
            return

        source_text = "from in-game activity! 🚀" if source == "udp" else "from chatting! 💬"
        embed = discord.Embed(
            description=f"🎉 **{user.mention} has reached level {new_level}!**",
            color=discord.Color.blue(),
        ).set_author(name=f"Leveled up {source_text}", icon_url=user.display_avatar.url)

        # Restored Feature: Add the helpful footer back to the embed.
        xp_for_next = to_next_level(new_xp)
        embed.set_footer(text=f"You need {xp_for_next:,} more XP for the next level.")
        await channel.send(embed=embed)

    async def _is_opted_out(self, member: discord.Member) -> bool:
        """Check if a member has the XP opt-out role."""
        config = await self.config_db.get_guild_config(GuildId(member.guild.id))
        role_id = config.xp_opt_out_role_id
        if not role_id:
            return False
        return member.get_role(role_id) is not None

    async def _grant_xp(self, user_id: UserId, guild_id: GuildId, source: str, amount: PositiveInt) -> None:
        """Check eligibility and grant XP."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return  # Guild not found

        member = guild.get_member(user_id)
        if not member:
            return  # Member not found in guild

        if await self._is_opted_out(member):
            return

        # Single atomic call to the enhanced database method
        new_xp = await self.user_db.increment_stat(UserId(user_id), GuildId(guild_id), StatName.XP, amount)

        # Safely derive the old XP from the result
        old_xp = NonNegativeInt(new_xp - amount)

        old_level = get_level(old_xp)
        new_level = get_level(new_xp)

        if new_level > old_level:
            # Pass new_xp to the handler so it can calculate the footer
            await self._handle_level_up_announcement(user_id, guild_id, new_level, new_xp, source)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Grant XP to users for sending messages."""
        if message.author.bot or not message.guild:
            return

        # Short message filter from original implementation
        if len(set(message.clean_content.lower()).intersection(lowercase_letters)) <= 3:
            return

        xp_to_add = self._get_addable_xp(UserId(message.author.id), GuildId(message.guild.id))
        if xp_to_add > 0:
            await self._grant_xp(  # Cast to PositiveInt as _grant_xp expects it
                UserId(message.author.id),
                GuildId(message.guild.id),
                "message",
                PositiveInt(xp_to_add),
            )

    async def grant_udp_xp(self, user_id: UserId) -> None:
        """Handle UDP-based XP for one specific guild."""
        if not self.privileged_guild_id:
            log.warning("Cannot grant UDP XP: UDP_GUILD_ID environment variable is not set.")
            return

        guild = self.bot.get_guild(self.privileged_guild_id)
        if not guild or not guild.get_member(user_id):
            # Don't grant XP if the user isn't in the specified server
            return

        xp_to_add = self._get_addable_xp(UserId(user_id), self.privileged_guild_id)
        if xp_to_add > 0:
            await self._grant_xp(UserId(user_id), self.privileged_guild_id, "udp", PositiveInt(xp_to_add))

    @level.command(
        name="opt-out",
        description="Exclude yourself from the leveling system by getting the opt-out role.",
    )
    async def level_opt_out(self, interaction: GuildInteraction) -> None:
        """Allow a user to opt-out by gaining the opt-out role."""
        config = await self.config_db.get_guild_config(GuildId(interaction.guild.id))
        role_id = config.xp_opt_out_role_id

        if not role_id:
            await interaction.response.send_message("ℹ️ This server has not configured an XP Opt-Out role.", ephemeral=True)  # noqa: RUF001
            return

        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message(
                "⚠️ The configured XP Opt-Out role no longer exists. Please contact an admin.",
                ephemeral=True,
            )
            self.bot.dispatch(
                "security_alert",
                guild_id=interaction.guild.id,
                risk_level="HIGH",
                details=(
                    "**Missing Role Configuration**\n"
                    "The `/level opt-out` command failed because the "
                    f"configured `xp_opt_out_role_id` ({role_id}) could not be found."
                ),
                warning_type="missing_role",
            )
            return

        # Run our centralized security checks (raises SecurityCheckError on failure)
        # This role MUST have no permissions
        ensure_role_safety(role)
        ensure_bot_hierarchy(interaction, role)

        if role in interaction.user.roles:
            await interaction.response.send_message("ℹ️ You already have the XP Opt-Out role.", ephemeral=True)  # noqa: RUF001
            return

        try:
            await interaction.user.add_roles(role, reason="User opted out of leveling")
            await interaction.response.send_message(
                f"✅ You have been given the {role.mention} role and are now opted out of leveling.",
                ephemeral=True,
            )
        except discord.Forbidden:
            # This is now a fallback, as validate_bot_hierarchy should catch most issues
            await interaction.response.send_message(
                "⚠️ I do not have permission to assign that role. Please contact an admin.",
                ephemeral=True,
            )

    @level.command(
        name="opt-in",
        description="Re-include yourself in the leveling system by removing the opt-out role.",
    )
    async def level_opt_in(self, interaction: GuildInteraction) -> None:
        """Allow a user to opt-in by removing the opt-out role."""
        config = await self.config_db.get_guild_config(GuildId(interaction.guild.id))
        role_id = config.xp_opt_out_role_id

        if not role_id:
            await interaction.response.send_message("ℹ️ This server has not configured an XP Opt-Out role.", ephemeral=True)  # noqa: RUF001
            return

        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message(
                "⚠️ The configured XP Opt-Out role no longer exists. Please contact an admin.",
                ephemeral=True,
            )
            return

        # We only need to check hierarchy here, as we are removing the role.
        # The permissions don't matter for removal, but hierarchy does.
        ensure_bot_hierarchy(interaction, role)

        if role not in interaction.user.roles:
            await interaction.response.send_message("ℹ️ You are already opted in.", ephemeral=True)  # noqa: RUF001
            return

        try:
            await interaction.user.remove_roles(role, reason="User opted in to leveling")
            await interaction.response.send_message(
                f"✅ The {role.mention} role has been removed. You will now gain XP again.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ I do not have permission to remove that role. Please contact an admin.",
                ephemeral=True,
            )

    @level.command(name="rank", description="Check your or another user's rank.")
    async def level_rank(self, interaction: GuildInteraction, member: discord.Member | None = None) -> None:
        """Display the level, XP, and rank of a user, with a progress bar."""
        target_user = member or interaction.user
        ephemeral = member is None or member.id == interaction.user.id
        guild_id = GuildId(interaction.guild.id)

        if target_user.bot:
            await interaction.response.send_message("Nice try but us bots don't do that.", ephemeral=True)
            return

        xp = await self.user_db.get_stat(UserId(target_user.id), guild_id, StatName.XP)
        level = get_level(xp)
        xp_for_next = to_next_level(xp)

        # Progress bar calculation from original implementation
        current_level_xp_req = round(level**2.5 + 10)
        next_level_xp_req = round((level + 1) ** 2.5 + 10)
        progress_in_level = xp - current_level_xp_req
        total_for_level = next_level_xp_req - current_level_xp_req

        progress_bar = "■" * int((progress_in_level / total_for_level) * 10) + "□" * (
            10 - int((progress_in_level / total_for_level) * 10)
        )

        embed = discord.Embed(
            title=f"📊 Level Stats for {target_user.display_name}",
            color=discord.Color.random(),
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="Total XP", value=f"**{xp:,}**", inline=True)
        embed.add_field(
            name=f"Progress to Level {level + 1}",
            value=f"`{progress_bar}`\n({progress_in_level:,} / {total_for_level:,} XP)",
            inline=False,
        )
        embed.set_footer(text=f"You need {xp_for_next:,} more XP for the next level.")
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    @level.command(name="reset-xp", description="[Admin] Resets a user's XP.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def level_reset_xp(self, interaction: GuildInteraction, member: discord.Member) -> None:
        # This command is simple and can be defined locally.
        view = discord.ui.View(timeout=30)

        async def confirm_callback(interaction: GuildInteraction) -> None:
            user_id, guild_id = UserId(member.id), GuildId(interaction.guild.id)
            # First, get the user's current XP
            current_xp = await self.user_db.get_stat(user_id, guild_id, StatName.XP)

            if current_xp > 0:
                # If they have XP, reset it and confirm
                await self.user_db.set_stat(user_id, guild_id, StatName.XP, 0)
                await interaction.response.edit_message(
                    content=f"✅ Successfully reset all XP for **{member.display_name}**.",
                    view=None,
                )
            else:
                # If they had no XP, provide contextual feedback
                await interaction.response.edit_message(
                    content=f"ℹ️ **{member.display_name}** had no XP to reset.",  # noqa: RUF001
                    view=None,
                )

        async def cancel_callback(interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(content="❌ Action cancelled.", view=None)

        confirm_button = discord.ui.Button(label="Confirm Reset", style=discord.ButtonStyle.danger)
        confirm_button.callback = confirm_callback

        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = cancel_callback

        view.add_item(confirm_button)
        view.add_item(cancel_button)

        await interaction.response.send_message(
            f"⚠️ Are you sure you want to permanently delete all XP for **{member.display_name}**?",
            view=view,
            ephemeral=True,
        )


async def setup(bot: KiwiBot) -> None:
    """Add the LevelingCog to the bot."""
    await bot.add_cog(
        LevelingCog(
            bot=bot,
            udp_port=bot.config.udp_port,  # UDP port is a global bot setting
            privileged_guild_id=bot.config.guild_id,  # Special case for UDP, still uses global guild_id
            user_db=bot.user_db,
            config_db=bot.config_db,
        ),
    )

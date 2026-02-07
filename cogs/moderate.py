import contextlib
import datetime
import logging
import math
from typing import TYPE_CHECKING, Final, Literal

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from modules.ConfigDB import ConfigDB
    from modules.KiwiBot import KiwiBot

from modules.dtypes import GuildId, GuildInteraction
from modules.security_utils import SecurityCheckError, ensure_bot_hierarchy, ensure_moderation_action

log = logging.getLogger(__name__)

# A dictionary to convert user-friendly time units to timedelta objects
TIME_UNITS: Final[dict[str, str]] = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}


class DurationTransformer(app_commands.Transformer):
    """A transformer to convert a string like '10m' or '1d' into a timedelta."""

    async def transform(
        self,
        _interaction: GuildInteraction,
        value: str,
    ) -> datetime.timedelta:
        """Do the conversion."""
        value = value.lower().strip()
        unit = value[-1]

        if unit not in TIME_UNITS:
            # Using AppCommandError to provide a clean error message to the user.
            msg = "Invalid duration unit. Use 's', 'm', 'h', or 'd'."
            raise app_commands.AppCommandError(msg)

        try:
            time_value = int(value[:-1])
            delta = datetime.timedelta(**{TIME_UNITS[unit]: time_value})

            # Add Discord's 28-day timeout limit check directly in the transformer
            if delta > datetime.timedelta(days=28):
                msg = "Duration cannot exceed 28 days."
                raise app_commands.AppCommandError(msg)

        except ValueError as e:
            msg = "Invalid duration format. Example: `10m`, `2h`, `7d`"
            raise app_commands.AppCommandError(msg) from e

        return delta


@commands.guild_only()
# Set default permissions for the entire command group
@app_commands.default_permissions(moderate_members=True, manage_roles=True)
# Add a cog-wide cooldown: 5 actions per 60 seconds, per user, per guild.
@app_commands.checks.cooldown(5, 60.0, key=lambda i: (i.guild_id, i.user.id))
class Moderate(
    commands.GroupCog,
    group_name="moderate",
    group_description="Moderation commands for server management.",
):
    """A cog for moderation commands using GroupCog for central checks.

    Provides slash commands for banning, kicking, muting, and timing out members.
    Includes built-in rate limiting and centralized hierarchy checks.
    """

    def __init__(self, bot: KiwiBot, *, config_db: ConfigDB) -> None:
        self.bot = bot
        self.config_db = config_db
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Centralized pre-action check for all moderation commands.

        Run before any command callback and before the cooldown.
        """
        if not interaction.data:
            # Not a command interaction, let it pass (or fail)
            return True

        # Find the subcommand's options
        try:
            # interaction.data['options'][0] is the subcommand (e.g., 'ban', 'kick')
            subcommand_data = interaction.data["options"][0]
            options = subcommand_data.get("options", [])
        except IndexError, KeyError, AttributeError:
            log.warning(
                "Could not find subcommand options in interaction_check: %s",
                interaction.data.get("name"),
            )
            # This is not a command this check is designed for, so let it pass.
            return True

        # Find the 'member' argument in the subcommand's options
        member_data = next((opt for opt in options if opt["name"] == "member"), None)

        if not member_data:
            # This command doesn't have a 'member' arg (e.g., a future 'purge' command).
            # We don't need to run moderation checks.
            return True

        try:
            member_id = int(member_data["value"])
        except ValueError as e:
            msg = "Invalid option selected."
            raise app_commands.AppCommandError(msg) from e

        if not interaction.guild:
            # Should be impossible due to guild_only=True, but good practice.
            msg = "This command can only be used in a server."
            raise app_commands.CheckFailure(msg)

        # Get the discord.Member object
        member = interaction.guild.get_member(member_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(member_id)
            except discord.NotFound:
                # Use AppCommandError for a user-facing error that on_app_command_error won't log
                msg = "❌ I could not find that member."
                raise app_commands.AppCommandError(msg) from None
            except discord.HTTPException as e:
                log.warning("Failed to fetch member %s: %s", member_id, e)
                msg = f"❌ Failed to fetch member: {e}"
                raise app_commands.AppCommandError(msg) from e

        # Run the centralized validation logic (raises SecurityCheckError on failure)
        try:
            ensure_moderation_action(interaction, member)
        except SecurityCheckError as e:
            # Raise a CheckFailure, which our error handler will catch and report.
            raise app_commands.CheckFailure(str(e)) from e

        # All checks passed
        return True

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Handle errors for all commands in this Cog."""
        ephemeral = True

        # Unwrap CommandInvokeError if present
        original_error = error.original if isinstance(error, app_commands.CommandInvokeError) else error

        if isinstance(error, app_commands.CommandOnCooldown):
            retry_after = math.ceil(error.retry_after)
            await interaction.response.send_message(
                f"⏳ You are on cooldown. Please try again in {retry_after} second(s).",
                ephemeral=ephemeral,
            )
        elif isinstance(error, app_commands.CheckFailure):
            # Catches hierarchy checks from interaction_check
            await interaction.response.send_message(f"❌ {error}", ephemeral=ephemeral)
        elif isinstance(original_error, SecurityCheckError):
            # Catches SecurityCheckError from commands (e.g., mute role hierarchy)
            await interaction.response.send_message(
                f"❌ **Configuration Error:**\n{original_error}",
                ephemeral=ephemeral,
            )
        elif isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                f"❌ You do not have the required permissions: {', '.join(error.missing_permissions)}",
                ephemeral=ephemeral,
            )
        elif isinstance(error, app_commands.BotMissingPermissions):
            await interaction.response.send_message(
                f"❌ I do not have the required permissions: {', '.join(error.missing_permissions)}",
                ephemeral=ephemeral,
            )
        elif isinstance(error, app_commands.AppCommandError):
            # Generic AppCommandError (e.g., from transformers)
            await interaction.response.send_message(str(error), ephemeral=ephemeral)
        else:
            log.exception("Unhandled error in Moderate cog: %s", error)
            if not interaction.response.is_done():
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.send_message(
                        "❌ An unexpected error occurred.",
                        ephemeral=ephemeral,
                    )

    async def _notify_member(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        action: str,
        reason: str | None,
        duration: str | None = None,
    ) -> None:
        """Send a DM to the member about the moderation action."""
        embed = discord.Embed(
            title=f"You have been {action} in {interaction.guild.name}",
            color=discord.Colour.red(),
        )
        embed.add_field(
            name="Reason",
            value=reason or "No reason provided.",
            inline=False,
        )
        if duration:
            embed.add_field(name="Duration", value=duration, inline=False)
        embed.set_footer(text=f"Moderator: {interaction.user.display_name}")

        try:
            await member.send(embed=embed)
        except discord.Forbidden:
            log.warning(
                "Failed to DM %s (%s) - they may have DMs disabled.",
                member.display_name,
                member.id,
            )
        except discord.HTTPException:
            log.exception(
                "Failed to DM %s (%s) due to an HTTP error.",
                member.display_name,
                member.id,
            )

    # --- MODERATION COMMANDS ---

    @app_commands.command(name="ban", description="Bans a member from the server.")
    @app_commands.checks.has_permissions(ban_members=True)  # Still need specific perm
    async def ban(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        reason: str | None = None,
        delete_messages: Literal[
            "Don't delete any",
            "Last 24 hours",
            "Last 7 days",
        ] = "Don't delete any",
        notify_member: bool = True,
    ) -> None:
        """Bans a member and optionally deletes their recent messages."""
        # _pre_action_checks is handled by interaction_check
        delete_seconds = 0
        if delete_messages == "Last 24 hours":
            delete_seconds = 86400
        elif delete_messages == "Last 7 days":
            delete_seconds = 604800

        if notify_member:
            await self._notify_member(interaction, member, "banned", reason)

        try:
            await member.ban(reason=reason, delete_message_seconds=delete_seconds)
            await interaction.response.send_message(
                f"✅ **{member.display_name}** has been banned.",
                ephemeral=True,
            )
            log.info("%s banned %s for: %s", interaction.user, member, reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have the required permissions to ban this member.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"A {e.status} {e.code} error has occurred.",
                ephemeral=True,
            )
            log.exception("Failed to ban %s", member)

    @app_commands.command(name="kick", description="Kicks a member from the server.")
    @app_commands.checks.has_permissions(kick_members=True)  # Still need specific perm
    async def kick(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        reason: str | None = None,
        notify_member: bool = True,
    ) -> None:
        """Kicks a member from the server."""
        # _pre_action_checks is handled by interaction_check
        if notify_member:
            await self._notify_member(interaction, member, "kicked", reason)

        try:
            await member.kick(reason=reason)
            await interaction.response.send_message(
                f"✅ **{member.display_name}** has been kicked.",
                ephemeral=True,
            )
            log.info("%s kicked %s for: %s", interaction.user, member, reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have the required permissions to kick this member.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"A {e.status} {e.code} error has occurred.",
                ephemeral=True,
            )
            log.exception("Failed to kick %s", member)

    @app_commands.command(
        name="timeout",
        description="Times out a member for a specified duration.",
    )
    @app_commands.describe(
        duration="Duration of the timeout (e.g., 10m, 1h, 3d). Max 28 days.",
    )
    async def timeout(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        duration: app_commands.Transform[datetime.timedelta, DurationTransformer],
        reason: str | None = None,
        notify_member: bool = True,
    ) -> None:
        """Time out a member for a given duration."""
        # _pre_action_checks is handled by interaction_check
        end_timestamp = discord.utils.utcnow() + duration

        if notify_member:
            await self._notify_member(
                interaction,
                member,
                "timed out",
                reason,
                duration=f"until {discord.utils.format_dt(end_timestamp, 'F')}",
            )

        try:
            await member.timeout(duration, reason=reason)
            await interaction.response.send_message(
                f"✅ **{member.display_name}** has been timed out until {discord.utils.format_dt(end_timestamp, 'F')}.",
                ephemeral=True,
            )
            log.info(
                "%s timed out %s for %s. Reason: %s",
                interaction.user,
                member,
                str(duration),
                reason,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have the required permissions to timeout this member.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"A {e.status} {e.code} error has occurred.",
                ephemeral=True,
            )
            log.exception("Failed to timeout %s", member)

    @app_commands.command(name="untimeout", description="Removes a timeout from a member.")
    async def untimeout(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        reason: str | None = None,
        notify_member: bool = True,
    ) -> None:
        """Remove a timeout from a member."""
        # _pre_action_checks is handled by interaction_check
        if not member.is_timed_out():
            await interaction.response.send_message(
                "This member is not currently timed out.",
                ephemeral=True,
            )
            return

        if notify_member:
            await self._notify_member(interaction, member, "timeout removed", reason)

        try:
            await member.timeout(None, reason=reason)
            await interaction.response.send_message(
                f"✅ The timeout for **{member.display_name}** has been removed.",
                ephemeral=True,
            )
            log.info(
                "%s removed timeout from %s. Reason: %s",
                interaction.user,
                member,
                reason,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have the required permissions to remove this timeout.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"A {e.status} {e.code} error has occurred.",
                ephemeral=True,
            )
            log.exception("Failed to untimeout %s", member)

    @app_commands.command(
        name="mute",
        description="Mutes a member by assigning the muted role.",
    )
    async def mute(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        reason: str | None = None,
        notify_member: bool = True,
    ) -> None:
        """Mutes a member by adding a 'Muted' role."""
        # _pre_action_checks is handled by interaction_check
        config = await self.config_db.get_guild_config(
            GuildId(interaction.guild.id),
        )
        muted_role_id = config.muted_role_id
        if not muted_role_id:
            await interaction.response.send_message(
                "The Muted Role has not been configured for this server. Use `/config set role`.",
                ephemeral=True,
            )
            return

        muted_role = interaction.guild.get_role(muted_role_id)
        if not muted_role:
            await interaction.response.send_message(
                "The configured muted role could not be found on this server. It may have been deleted.",
                ephemeral=True,
            )
            self.bot.dispatch(
                "security_alert",
                guild_id=interaction.guild.id,
                risk_level="HIGH",
                details=(
                    "**Missing Muted Role**\n"
                    "The `/moderate mute` command failed because the "
                    f"configured `muted_role_id` ({muted_role_id}) could not be found."
                ),
                warning_type="missing_role",
            )
            return

        # This check must be inside the command, as it depends on the configured role.
        # It will be caught by on_app_command_error if it fails.
        ensure_bot_hierarchy(interaction, muted_role)

        if muted_role in member.roles:
            await interaction.response.send_message(
                "This member is already muted.",
                ephemeral=True,
            )
            return

        if notify_member:
            await self._notify_member(interaction, member, "muted", reason)

        try:
            await member.add_roles(muted_role, reason=reason)
            await interaction.response.send_message(
                f"✅ **{member.display_name}** has been muted.",
                ephemeral=True,
            )
            log.info("%s muted %s. Reason: %s", interaction.user, member, reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have the permissions to assign the muted role.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"A {e.status} {e.code} error has occurred.",
                ephemeral=True,
            )
            log.exception("Failed to mute %s", member)

    @app_commands.command(
        name="unmute",
        description="Unmutes a member by removing the muted role.",
    )
    async def unmute(
        self,
        interaction: GuildInteraction,
        member: discord.Member,
        reason: str | None = None,
        notify_member: bool = True,
    ) -> None:
        """Unmutes a member by removing the 'Muted' role."""
        # _pre_action_checks is handled by interaction_check
        config = await self.config_db.get_guild_config(
            GuildId(interaction.guild.id),
        )
        muted_role_id = config.muted_role_id
        if not muted_role_id:
            await interaction.response.send_message(
                "The Muted Role has not been configured for this server. Use `/config set role`.",
                ephemeral=True,
            )
            return

        muted_role = interaction.guild.get_role(muted_role_id)
        if not muted_role:
            await interaction.response.send_message(
                "The configured muted role could not be found on this server. It may have been deleted.",
                ephemeral=True,
            )
            return

        # This check must be inside the command, as it depends on the configured role.
        # It will be caught by on_app_command_error if it fails.
        ensure_bot_hierarchy(interaction, muted_role)

        if muted_role not in member.roles:
            await interaction.response.send_message(
                "This member is not currently muted.",
                ephemeral=True,
            )
            return

        if notify_member:
            await self._notify_member(interaction, member, "unmuted", reason)

        try:
            await member.remove_roles(muted_role, reason=reason)
            await interaction.response.send_message(
                f"✅ **{member.display_name}** has been unmuted.",
                ephemeral=True,
            )
            log.info("%s unmuted %s. Reason: %s", interaction.user, member, reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have the permissions to remove the muted role.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"A {e.status} {e.code} error has occurred.",
                ephemeral=True,
            )
            log.exception("Failed to unmute %s", member)


async def setup(bot: KiwiBot) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(Moderate(bot, config_db=bot.config_db))

from __future__ import annotations

import logging
from typing import NewType, TypeIs

import discord
from discord import Interaction
from discord.app_commands import errors as app_errors
from discord.ext import commands

from modules.exceptions import UserError

# Configure logging
logger = logging.getLogger(__name__)

# Type Aliases and NewTypes
type Context = commands.Context[commands.Bot]
type ErrorContext = Context | Interaction
PermissionList = NewType("PermissionList", list[str])


def is_ext_error(err: Exception) -> TypeIs[commands.MissingPermissions]:
    """Check if the error is an extension MissingPermissions error."""
    return isinstance(err, commands.MissingPermissions)


def is_app_error(err: Exception) -> TypeIs[app_errors.MissingPermissions]:
    """Check if the error is an app command MissingPermissions error."""
    return isinstance(err, app_errors.MissingPermissions)


class ErrorHandler(commands.Cog):
    """Handle errors for both prefix and application commands.

    This cog centralizes error handling to ensure consistent user feedback
    regardless of the command source.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_command_error(self, ctx: Context, error: commands.CommandError) -> None:
        """Handle errors from prefix commands."""
        # Unwrap the error if it's an InvokeError
        original_error = getattr(error, "original", error)

        # Check for UserError first (custom exceptions)
        if isinstance(original_error, (UserError, app_errors.TransformerError)):
            await ctx.send(f"❌ {original_error}")
            return

        # Handle MissingPermissions via unified handler
        if isinstance(error, commands.MissingPermissions):
            await self._handle_permissions_error(ctx, error)
            return

        # Handle other specific errors
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore commands that don't exist

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"This command is on cooldown. Try again in {error.retry_after:.2f}s.",
            )
            return

        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send(
                "You may not use this command in DMs.",
            )
            return

        # Fallback to generic handling which might overlap with permissions logic above
        # but since we return early on matches, we only get here for unhandled stuff.

        # If it wasn't one of the above, delegate to unified permission handler just in case
        # (though likely caught by explicit check) or log it.
        # But wait, logic from KiwiBot.py had a catch-all for unknown errors.

        logger.exception("Unhandled command error in '%s'", ctx.command, exc_info=original_error)
        await ctx.send("An unexpected error occurred.")

    async def on_tree_error(self, interaction: Interaction, error: app_errors.AppCommandError) -> None:
        """Handle errors from application commands (Slash/Context Menu)."""
        # Unwrap if needed, though app errors are often direct
        original_error = getattr(error, "original", error)

        if isinstance(original_error, UserError):
            msg = f"❌ {original_error}"
            if interaction.response.is_done():
                await interaction.followup.send(msg)
            else:
                await interaction.response.send_message(msg)
            return

        await self._handle_permissions_error(interaction, original_error)

    async def _handle_permissions_error(self, source: ErrorContext, error: Exception) -> None:
        """Process the error and notify the user if permissions are missing.

        Args:
            source: The context or interaction where the error occurred.
            error: The exception raised.

        """
        missing: list[str] = []

        # Python 3.13 TypeIs narrowing
        if is_ext_error(error):
            missing = error.missing_permissions
        elif is_app_error(error):
            # app_commands permissions might be raw strings or objects depending on version,
            # usually strings similar to ext.commands
            missing = error.missing_permissions
        else:
            # Not a permission error, we might have ended up here from on_tree_error fallback
            # Log it if it's an interaction error we haven't handled yet
            if isinstance(source, discord.Interaction):
                logger.exception("Ignoring unhandled interaction error: %s", error, exc_info=error)
                msg = "An unexpected error occurred."
                if source.response.is_done():
                    await source.followup.send(msg)
                else:
                    await source.response.send_message(msg)
            return

        formatted_perms = self._format_permissions(PermissionList(missing))
        message = f"You are missing the following permissions: {formatted_perms}"

        if isinstance(source, commands.Context):
            await source.send(message)
        elif isinstance(source, discord.Interaction):
            if source.response.is_done():
                await source.followup.send(message)
            else:
                await source.response.send_message(message)

    def _format_permissions(self, perms: PermissionList) -> str:
        """Format permission strings into a readable comma-separated list."""
        return ", ".join(perm.replace("_", " ").title() for perm in perms)


# Setup function to load the cog
async def setup(bot: commands.Bot) -> None:
    cog = ErrorHandler(bot)
    await bot.add_cog(cog)
    # Register the tree error handler explicitly
    bot.tree.on_error = cog.on_tree_error

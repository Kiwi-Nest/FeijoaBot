from __future__ import annotations

import logging

import discord
from discord import Interaction
from discord.app_commands import errors as app_errors
from discord.ext import commands

from modules.exceptions import UserError
from modules.security_utils import SecurityCheckError

# Configure logging
logger = logging.getLogger(__name__)

type Context = commands.Context[commands.Bot]
type ErrorContext = Context | Interaction


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
        original_error = getattr(error, "original", error)

        if isinstance(original_error, (UserError, app_errors.TransformerError)):
            await ctx.send(f"❌ {original_error}")
            return

        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"This command is on cooldown. Try again in {error.retry_after:.2f}s.",
            )
            return

        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("You may not use this command in DMs.")
            return

        if isinstance(error, (commands.MissingPermissions, commands.BotMissingPermissions)):
            await self._handle_permissions_error(ctx, error)
            return

        if isinstance(error, commands.CheckFailure):
            await ctx.send(f"❌ {error}")
            return

        logger.exception("Unhandled command error in '%s'", ctx.command, exc_info=original_error)
        await ctx.send("An unexpected error occurred.")

    async def on_tree_error(self, interaction: Interaction, error: app_errors.AppCommandError) -> None:
        """Handle errors from application commands (Slash/Context Menu)."""
        if isinstance(error, app_errors.CommandInvokeError):
            error = error.original

        if isinstance(error, (UserError, SecurityCheckError)):
            await self._send_interaction(interaction, f"❌ {error}")
            return

        if isinstance(error, (app_errors.MissingPermissions, app_errors.BotMissingPermissions)):
            await self._handle_permissions_error(interaction, error)
            return

        if isinstance(error, app_errors.CommandOnCooldown):
            await self._send_interaction(interaction, f"This command is on cooldown. Try again in {error.retry_after:.2f}s.")
            return

        if isinstance(error, app_errors.AppCommandError):
            # User-facing error raised directly by transformers or validators
            await self._send_interaction(interaction, f"❌ {error}")
            return

        logger.exception(
            "Unexpected error in app command '%s'",
            interaction.command.name if interaction.command else "Unknown",
            exc_info=error,
        )
        await self._send_interaction(interaction, "An unexpected error occurred.")

    async def _send_interaction(self, interaction: Interaction, msg: str, *, ephemeral: bool = True) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(msg, ephemeral=ephemeral)

    async def _handle_permissions_error(self, source: ErrorContext, error: Exception) -> None:
        """Notify the user of missing permissions."""
        if isinstance(error, (commands.MissingPermissions, app_errors.MissingPermissions)):
            message = f"You are missing the following permissions: {self._format_permissions(error.missing_permissions)}"
        elif isinstance(error, (commands.BotMissingPermissions, app_errors.BotMissingPermissions)):
            message = f"I am missing the following permissions: {self._format_permissions(error.missing_permissions)}"
        else:
            return

        if isinstance(source, commands.Context):
            await source.send(message)
        elif isinstance(source, discord.Interaction):
            if source.response.is_done():
                await source.followup.send(message, ephemeral=True)
            else:
                await source.response.send_message(message, ephemeral=True)

    def _format_permissions(self, perms: list[str]) -> str:
        """Format permission strings into a readable comma-separated list."""
        return ", ".join(perm.replace("_", " ").title() for perm in perms)


# Setup function to load the cog
async def setup(bot: commands.Bot) -> None:
    cog = ErrorHandler(bot)
    await bot.add_cog(cog)
    # Register the tree error handler explicitly
    bot.tree.on_error = cog.on_tree_error

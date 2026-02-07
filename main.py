import logging

import discord
from discord import app_commands
from dotenv import load_dotenv

from modules.config import BotConfig
from modules.exceptions import UserError
from modules.KiwiBot import KiwiBot
from modules.security_utils import SecurityCheckError

# Loads environment variables
load_dotenv()


# 1. Create and configure your file handler separately.
# Using 'a' for append mode is a good choice.
file_handler = logging.FileHandler(filename="bot.log", encoding="utf-8", mode="a")
dt_fmt = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter("[{asctime}] [{levelname:<8}] {name}: {message}", dt_fmt, style="{")
file_handler.setFormatter(formatter)

# 2. Call setup_logging WITHOUT the handler kwarg to get the default console logger.
# root=True ensures your cogs' loggers are also configured for the console.
discord.utils.setup_logging(level=logging.INFO, root=True)

# 3. Add your file handler to the root logger.
logging.getLogger().addHandler(file_handler)

# Get the top-level logger for your application
log = logging.getLogger(__name__)

try:
    # Create the config from environment first
    config = BotConfig.from_environment()

    # Pass the config object into the bot's constructor
    bot: KiwiBot = KiwiBot(config=config)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Global error handler for app commands."""
        # Unwrap the error if it's a CommandInvokeError
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original

        if isinstance(error, (UserError, SecurityCheckError)):
            # Send the friendly error message to the user
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        else:
            # Log unexpected errors
            log.exception(
                "An unexpected error occurred while processing command: %s",
                interaction.command.name if interaction.command else "Unknown",
                exc_info=error,
            )
            # Notify the user generically
            msg = "An unexpected error occurred. Please try again later."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

    bot.run(config.token)

except KeyError, ValueError:
    log.exception(
        "A critical configuration error occurred. Please check your environment variables.",
    )

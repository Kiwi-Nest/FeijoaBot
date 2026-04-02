import logging

import discord
from dotenv import load_dotenv

from modules.config import BotConfig
from modules.KiwiBot import KiwiBot

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

    bot.run(config.token)

except KeyError, ValueError:
    log.exception(
        "A critical configuration error occurred. Please check your environment variables.",
    )

import logging
import pathlib
from typing import ClassVar

import aiohttp
import discord
from discord import Forbidden, HTTPException, MissingApplicationID
from discord.app_commands import CommandSyncFailure, TranslationError
from discord.ext import commands
from discord.ext.commands import ExtensionAlreadyLoaded, ExtensionFailed, ExtensionNotFound, NoEntryPointError

from modules.config import BotConfig
from modules.ConfigDB import ConfigDB
from modules.CurrencyLedgerDB import CurrencyLedgerDB
from modules.Database import Database
from modules.InvitesDB import InvitesDB
from modules.ReminderDB import ReminderDB
from modules.server_admin import ServerManager
from modules.TaskDB import TaskDB
from modules.trading_logic import TradingLogic
from modules.UserDB import UserDB

log = logging.getLogger(__name__)


class KiwiBot(commands.Bot):
    loaded_extensions: ClassVar[list[str]] = []

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        # Define the bot and its command prefix
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        intents.reactions = True
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )
        self.server_manager: ServerManager | None = None
        self.trading_logic: TradingLogic | None = None

    # Event to notify when the bot has connected
    async def setup_hook(self) -> None:
        log.info("Logged in as %s", self.user)

        # Create the shared session
        self.http_session = aiohttp.ClientSession()

        # Initialize the database first
        self.database: Database = Database()
        self.user_db = UserDB(self.database)
        self.task_db = TaskDB(self.database)
        self.invites_db = InvitesDB(self.database, self.http_session)
        self.ledger_db = CurrencyLedgerDB(self.database)
        self.config_db = ConfigDB(self.database)
        self.reminder_db = ReminderDB(self.database)

        # AWAIT the post-initialization tasks to ensure tables are created
        # UserDB must be first as other tables have foreign keys to it.
        await self.user_db.post_init()
        await self.task_db.post_init()
        await self.invites_db.post_init()
        await self.ledger_db.post_init()
        await self.config_db.post_init()
        await self.reminder_db.post_init()

        # Initialize TradingLogic if API key is present
        if self.config.twelvedata_api_key:
            self.trading_logic = TradingLogic(
                self.database,
                self.user_db,
                self.ledger_db,
                self.config.twelvedata_api_key,
                self.http_session,
            )
            log.info("TradingLogic initialized.")
            # Create the portfolios table
            await self.trading_logic.post_init()
            # Start the trading backend
            await self.trading_logic.start()
        else:
            log.warning(
                "TWELVEDATA_API_KEY not set. Paper trading module will be unavailable.",
            )

        log.info("All database tables initialized.")

        # Initialize the Server Manager if configured
        if self.config.servers_path:
            self.server_manager = ServerManager(servers_path=self.config.servers_path)
            await self.server_manager.__aenter__()  # Start its background tasks

        await self.register_global_commands()

        # Special one server cogs
        for guild_id in (self.config.mc_guild_id, self.config.swl_guild_id):
            if guild_id:
                guild = discord.Object(guild_id)
                try:
                    synced_guild = await self.tree.sync(guild=guild)
                    if synced_guild:
                        log.info(
                            "Synced %d command(s) for guild %d: %s",
                            len(synced_guild),
                            guild_id,
                            [i.name for i in synced_guild],
                        )
                except HTTPException, CommandSyncFailure, Forbidden:
                    log.exception(
                        "Error syncing guild commands for guild %d",
                        guild_id,
                    )

        try:
            from tzbot4py import TZBot, TZFlags

            if not (self.config.tzbot_host and self.config.tzbot_port and self.config.tzbot_api_key):
                log.warning("TZBot support is enabled but it's not configured! Falling back to defaults...")
                self.tzbot = None
            else:
                self.tzbot: TZBot | None = TZBot(
                    self.config.tzbot_host,
                    self.config.tzbot_port,
                    self.config.tzbot_api_key,
                    self.config.tzbot_encryption_key,
                )
                self.tzbot.set_flags(TZFlags.AES, TZFlags.MSGPACK)
        except ImportError:
            self.tzbot: None = None

        log.info("Setup complete.")

    async def on_ready(self) -> None:
        if self.guilds:
            log.info("Bot is in %d guilds.", len(self.guilds))
        else:
            log.error("Bot is not in any guilds!")

    async def register_global_commands(self) -> None:
        try:
            await self.load_extension("cogs.modlog")
            log.info("Loaded cogs.modlog (Critical)")
        except Exception:
            log.critical(
                "Failed to load cogs.modlog! Security alerts will be lost.",
                exc_info=True,
            )

        log.info("Loading extensions...")
        # Now it's safe to load cogs
        try:
            # Add 'cogs.' prefix to the path for loading
            for file in sorted(pathlib.Path("cogs/").glob("*.py")):  # noqa: ASYNC240
                if file.is_file():
                    # Skip loading paper_trading if logic isn't available
                    if file.stem == "paper_trading" and not self.trading_logic:
                        log.warning(
                            "Skipping load of cogs.paper_trading: API key not configured.",
                        )
                        continue

                    if file.stem == "modlog":
                        continue  # We already loaded it

                    # Try to load each extension individually
                    try:
                        await self.load_extension(f"cogs.{file.stem}")
                        log.info("Loaded %s", file.stem)
                    except ImportError, ModuleNotFoundError:
                        log.exception(
                            "Failed to load dependencies for extension 'cogs.%s'.",
                            file.stem,
                        )
                    except (
                        ExtensionNotFound,
                        ExtensionAlreadyLoaded,
                        NoEntryPointError,
                        ExtensionFailed,
                    ):
                        # Log the specific extension that failed and continue
                        log.exception("Failed to load extension 'cogs.%s'.", file.stem)

            # Sync commands AFTER attempting to load all cogs
            synced = await self.tree.sync()  # Sync global slash commands with Discord
            log.info(
                "Synced %d global command(s) %s",
                len(synced),
                [i.name for i in synced],
            )
        except (
            HTTPException,
            CommandSyncFailure,
            Forbidden,
            MissingApplicationID,
            TranslationError,
        ):
            log.exception("Error syncing global commands")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Send a welcome and setup guide when joining a new guild."""
        log.info("Joined new guild: %s (%s)", guild.name, guild.id)

        # Try to send a message to the system channel, which is usually the best bet.
        # Fallback to the first available text channel if the system channel isn't usable.
        target_channel = guild.system_channel
        if not target_channel or not target_channel.permissions_for(guild.me).send_messages:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages and "staff" in channel.name.lower():
                    target_channel = channel
                    break

        if target_channel:
            embed = discord.Embed(
                title="👋 Quick Setup!",
                description="Admins use the `/config autodiscover` command. I'll suggest settings for you to approve.",
                color=discord.Colour.green(),
            )
            await target_channel.send(embed=embed)

    async def on_error(
        self,
        event_method: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        """Log unhandled exceptions."""
        log.exception(
            "Unhandled exception in %s",
            event_method,
            extra={"*args": args, "**kwargs": kwargs},
        )

    async def close(self) -> None:
        """Gracefully close bot resources."""
        if self.trading_logic:
            await self.trading_logic.close()

        if self.server_manager:
            await self.server_manager.__aexit__(
                None,
                None,
                None,
            )  # Ensure graceful shutdown

        if self.http_session:
            await self.http_session.close()
            log.info("Closed shared aiohttp session.")
        log.info("Closing bot gracefully.")
        await super().close()

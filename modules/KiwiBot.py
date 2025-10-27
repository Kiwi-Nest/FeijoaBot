import logging
import pathlib
import time
from typing import ClassVar, Literal

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
from modules.dtypes import GuildId
from modules.InvitesDB import InvitesDB
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
        self.http_session: aiohttp.ClientSession | None = None
        self.server_manager: ServerManager | None = None
        self._warning_cooldowns: dict[str, float] = {}
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

        # AWAIT the post-initialization tasks to ensure tables are created
        # UserDB must be first as other tables have foreign keys to it.
        await self.user_db.post_init()
        await self.task_db.post_init()
        await self.invites_db.post_init()
        await self.ledger_db.post_init()
        await self.config_db.post_init()

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
        else:
            log.warning(
                "TWELVEDATA_API_KEY not set. Paper trading module will be unavailable.",
            )

        log.info("All database tables initialized.")

        # Initialize the Server Manager if configured
        if self.config.servers_path:
            self.server_manager = ServerManager(servers_path=self.config.servers_path)
            await self.server_manager.__aenter__()  # Start its background tasks

        log.info("Loading extensions...")
        # Now it's safe to load cogs
        try:
            # Add 'cogs.' prefix to the path for loading
            for file in pathlib.Path("cogs/").glob("*.py"):
                if file.is_file():
                    # Skip loading paper_trading if logic isn't available
                    if file.stem == "paper_trading" and not self.trading_logic:
                        log.warning(
                            "Skipping load of cogs.paper_trading: API key not configured.",
                        )
                        continue

                    # Try to load each extension individually
                    try:
                        await self.load_extension(f"cogs.{file.stem}")
                        log.info("Loaded %s", file.stem)
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
                except (HTTPException, CommandSyncFailure, Forbidden):
                    log.exception(
                        "Error syncing guild commands for guild %d",
                        guild_id,
                    )

        log.info("Setup complete.")

    async def on_ready(self) -> None:
        if self.guilds:
            log.info("Bot is in %d guilds.", len(self.guilds))
        else:
            log.error("Bot is not in any guilds!")

    async def log_admin_warning(
        self,
        guild_id: GuildId,
        warning_type: str,
        description: str,
        level: Literal["WARN", "ERROR"] = "WARN",
        cooldown_seconds: int = 3600,
    ) -> None:
        """Send a standardized warning to the guild's configured bot warning channel.

        Includes a cooldown to prevent spam.
        """
        # 1. Check Cooldown
        now = time.time()
        cooldown_key = f"{guild_id}:{warning_type}"
        if (last_warn_time := self._warning_cooldowns.get(cooldown_key)) and (now - last_warn_time) < cooldown_seconds:
            return  # Still on cooldown

        # 2. Get Config & Channel
        try:
            config = await self.config_db.get_guild_config(guild_id)
            if not config.bot_warning_channel_id:
                return  # Feature not configured

            channel = self.get_channel(config.bot_warning_channel_id)
            if not isinstance(channel, discord.TextChannel):
                channel = await self.fetch_channel(config.bot_warning_channel_id)

            if not isinstance(channel, discord.TextChannel):
                log.warning(
                    "Bot warning channel %d for guild %d not found or not a text channel.",
                    config.bot_warning_channel_id,
                    guild_id,
                )
                return

        except (discord.NotFound, discord.Forbidden):
            log.warning(
                "Could not fetch or send to bot warning channel for guild %d.",
                guild_id,
            )
            return
        except Exception:
            log.exception("Error during bot warning channel retrieval.")
            return

        # 3. Build Embed
        if level == "ERROR":
            title = "❌ Bot Error"
            color = discord.Colour.red()
        else:
            title = "⚠️ Bot Warning"
            color = discord.Colour.orange()

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Warning Type: {warning_type}")

        # 4. Send Message and Set Cooldown
        try:
            await channel.send(embed=embed)
            self._warning_cooldowns[cooldown_key] = now
        except (discord.Forbidden, discord.HTTPException):
            log.exception(
                "Failed to send message to bot warning channel %d in guild %d",
                channel.id,
                guild_id,
            )

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

    async def on_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        """Log unhandled command exceptions."""
        try:
            raise error
        except commands.CommandNotFound:
            pass  # Ignore commands that don't exist
        except commands.CommandOnCooldown as e:
            await ctx.send(
                f"This command is on cooldown. Try again in {e.retry_after:.2f}s.",
                ephemeral=True,
            )
        except commands.NoPrivateMessage:
            await ctx.send(
                "You may not use this command in DMs.",
                ephemeral=True,
            )
        except commands.MissingPermissions as e:
            await ctx.send(
                f"You're missing the permissions to run this command: {', '.join(e.missing_permissions)}",
                ephemeral=True,
            )
        except Exception:
            log.exception("Unhandled command error in '%s'", ctx.command)
            await ctx.send("An unexpected error occurred.", ephemeral=True)

    async def close(self) -> None:
        """Gracefully close bot resources."""
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

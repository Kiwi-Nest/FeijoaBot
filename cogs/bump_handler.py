import asyncio
import datetime
import logging
import random
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from modules.discord_utils import InvalidRoleError, ping_online_role
from modules.dtypes import GuildId, PositiveInt, RoleId, UserId
from modules.enums import StatName
from modules.utils import format_ordinal

if TYPE_CHECKING:
    from modules.KiwiBot import KiwiBot
    from modules.UserDB import UserDB

log = logging.getLogger(__name__)

# --- Constants ---
BUMP_REMINDER_DELAY = datetime.timedelta(hours=2)
BACKUP_REMINDER_DELAY = datetime.timedelta(minutes=10)


class BumpHandlerCog(commands.Cog):
    """Handle rewards and reminders for server bumps from bots like Disboard."""

    def __init__(
        self,
        bot: "KiwiBot",
        disboard_bot_id: UserId,
    ) -> None:
        self.bot = bot
        self.user_db: UserDB = bot.user_db
        self.reminder_tasks: dict[GuildId, asyncio.Task | None] = {}
        self.disboard_bot_id = disboard_bot_id

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Find the last bump and process it to schedule a reminder.

        This method is idempotent and can handle reconnections.
        """
        log.info(
            "BumpHandlerCog loaded. Searching for the last bump in %s guilds.",
            len(self.bot.guilds),
        )
        for guild in self.bot.guilds:
            last_bump_message = await self._find_last_bump_message(guild)
            if last_bump_message:
                log.info(
                    "Found historical bump message %s in guild %s. Processing it.",
                    last_bump_message.id,
                    guild.name,
                )
                # Process the found message, but don't re-reward the user.
                await self._process_bump(last_bump_message, is_new_bump=False)
            else:
                log.info(
                    "No recent bump message found in guild %s. No reminder scheduled.",
                    guild.name,
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen for new messages to detect and process a successful bump."""
        if message.guild and self._is_successful_bump_message(message):
            log.info("Detected a new bump in message %s. Processing it.", message.id)
            await self._process_bump(message, is_new_bump=True)

    async def _process_bump(self, message: discord.Message, *, is_new_bump: bool) -> None:
        """Unified method to handle all bump logic.

        Args:
        ----
            message: The successful bump message from Disboard.
            is_new_bump: If True, grants a reward. If False, only schedules a reminder.

        """
        if not message.interaction_metadata:
            log.warning("Bump message %s has no interaction metadata.", message.id)
            return

        guild_id = GuildId(message.guild.id)
        config = await self.bot.config_db.get_guild_config(guild_id)
        bumper_role_id = config.bumper_role_id

        if not bumper_role_id:
            log.debug(
                "Skipping bump processing for guild %d: Bumper role not configured.",
                guild_id,
            )
            return

        bumper = message.interaction_metadata.user
        channel = message.channel

        try:
            if is_new_bump:
                reward = PositiveInt(random.randint(50, 80))
                user_id = UserId(bumper.id)
                # Reward Currency
                await self.bot.user_db.mint_currency(
                    user_id,
                    guild_id,
                    amount=reward,
                    event_reason="BUMP_SERVER",
                    ledger_db=self.bot.ledger_db,
                )

                new_bump_count = await self.bot.user_db.increment_stat(user_id, guild_id, StatName.BUMPS, PositiveInt(1))
                log.info("Rewarded %s with $%d for bumping.", bumper.display_name, reward)
                # Ensure the channel is a TextChannel before sending
                if not isinstance(channel, discord.TextChannel):
                    return
                await channel.send(
                    f"🎉 Thanks for your {format_ordinal(new_bump_count)} bump, {bumper.mention}! You've received **${reward}**.",
                )

            # --- Unified Reminder Scheduling ---
            if is_new_bump:
                delay_seconds = BUMP_REMINDER_DELAY.total_seconds()
            else:
                # Calculate remaining time for a historical bump
                time_since_bump = discord.utils.utcnow() - message.created_at
                remaining_delay = BUMP_REMINDER_DELAY - time_since_bump
                delay_seconds = remaining_delay.total_seconds()

            await self._schedule_reminder(
                guild_id,
                channel,
                bumper.mention,
                delay_seconds,
            )

        except (discord.HTTPException, discord.Forbidden):
            log.exception("Error processing bump message %s.", message.id)

    async def _schedule_reminder(
        self,
        guild_id: GuildId,  # Added guild_id to manage tasks per guild
        channel: discord.TextChannel,  # Kept for sending the message
        last_bumper: str,  # Kept for the message content
        delay_seconds: float,
    ) -> None:
        """Schedules or reschedules the bump reminder task."""
        if (existing_task := self.reminder_tasks.get(guild_id)) and not existing_task.done():
            existing_task.cancel()

        if delay_seconds <= 0:
            log.info("Reminder delay is zero or negative, sending now.")
            # Fetch config dynamically for immediate send
            config = await self.bot.config_db.get_guild_config(guild_id)
            if config.bumper_role_id:
                await self._send_reminder_message(channel, last_bumper, config.bumper_role_id)
            return

        delay_seconds = int(delay_seconds)  # floor it (sending a bit early is good)
        log.info("Scheduling bump reminder in %s seconds.", delay_seconds)

        # Calculate the delay for the backup reminder. If the primary reminder is already late,
        # this will be negative, and we'll adjust accordingly.
        backup_delay_seconds = delay_seconds + BACKUP_REMINDER_DELAY.total_seconds()

        async def reminder_coro() -> None:
            # Stage 1: Primary Reminder
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)  # Wait for the primary reminder

            # Fetch config again inside the coro to ensure it's up-to-date
            current_config = await self.bot.config_db.get_guild_config(guild_id)
            if current_config.bumper_role_id:
                await self._send_reminder_message(
                    channel,
                    last_bumper,
                    current_config.bumper_role_id,
                    is_backup=False,
                )

            # Stage 2: Backup Reminder (if configured)
            if current_config.backup_bumper_role_id:
                # Calculate how long to wait from *now* until the backup is due.
                # If the backup time is already in the past, this will be <= 0.
                remaining_backup_wait = backup_delay_seconds - delay_seconds
                if remaining_backup_wait > 0:
                    await asyncio.sleep(remaining_backup_wait)
                if current_config.backup_bumper_role_id:  # Re-check in case it was removed
                    await self._send_reminder_message(
                        channel,
                        last_bumper,
                        current_config.backup_bumper_role_id,
                        is_backup=True,
                    )

        self.reminder_tasks[guild_id] = asyncio.create_task(reminder_coro())

    async def _send_reminder_message(
        self,
        channel: discord.TextChannel,
        last_bumper_mention: str,
        role_id: RoleId,
        *,
        is_backup: bool = False,
    ) -> None:
        """Construct and send the bump reminder message."""
        log.info(
            "Sending %s bump reminder to #%s.",
            "backup" if is_backup else "primary",
            channel.name,
        )
        try:
            if is_backup:
                title = "⚠️ Still Need a Bump! ⚠️"
                prefix = "It's been a while! Can a backup bumper help out?"
                color = discord.Colour.orange()
            else:
                title = "⏰ Time to Bump! ⏰"
                prefix = "It's time to bump the server again!"
                color = discord.Colour.blue()

            description = f"{prefix} Use `/bump`.\n*Thanks to {last_bumper_mention} for the last one!*"
            reminder_embed = discord.Embed(title=title, description=description, color=color)
            role_to_ping = await channel.guild.fetch_role(role_id)
            ping_text = await ping_online_role(role_to_ping, self.user_db) if role_to_ping else ""

            await channel.send(content=ping_text, embed=reminder_embed)
        except InvalidRoleError:
            log.exception(
                "Failed to send %s reminder to %s. Role is invalid.",
                "backup" if is_backup else "primary",
                channel.name,
            )
        except (discord.HTTPException, discord.Forbidden):
            log.exception(
                "Failed to send %s reminder to %s.",
                "backup" if is_backup else "primary",
                channel.name,
            )

    async def _find_last_bump_message(self, guild: discord.Guild) -> discord.Message | None:
        """Scan channels to find the last successful bump message."""
        # Use cached text_channels to avoid API calls on startup
        candidate_channels = [c for c in guild.text_channels if "bump" in c.name.lower()]
        latest_bump_message: discord.Message | None = None

        for channel in candidate_channels:
            try:
                async for message in channel.history(limit=50):
                    if self._is_successful_bump_message(message):
                        if not latest_bump_message or message.created_at > latest_bump_message.created_at:
                            latest_bump_message = message
                        # Since history is newest-first, we can stop after finding the first one.
                        break
            except (discord.Forbidden, discord.HTTPException):
                continue
        return latest_bump_message

    def _is_successful_bump_message(self, message: discord.Message) -> bool:
        """Check if a message is a successful Disboard bump."""
        return (
            message.author.id == self.disboard_bot_id
            and message.embeds
            and message.embeds[0].description
            and "Bump done!" in message.embeds[0].description
        )


async def setup(bot: "KiwiBot") -> None:
    """Load the cog."""
    if not bot.config.disboard_bot_id:
        log.error("BumpHandlerCog not loaded: DISBOARD_BOT_ID is not configured.")
        return
    # BumpHandlerCog is now mostly stateless, disboard_bot_id is global.
    await bot.add_cog(BumpHandlerCog(bot, bot.config.disboard_bot_id))

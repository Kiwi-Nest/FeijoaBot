"""Reminders cog for KiwiBot.

If a user deletes a recent reminder message, it will be removed from the database even if snoozed.
"""

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from dateparser.search import search_dates
from discord import app_commands
from discord.ext import commands

from modules.dtypes import ChannelId, GuildId, MessageId, UserId

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from modules.KiwiBot import KiwiBot
    from modules.ReminderDB import ReminderDB
    from modules.UserDB import UserDB

log = logging.getLogger(__name__)


class SnoozeView(discord.ui.View):
    def __init__(self, user_id: int, message_id: int, context_url: str | None = None) -> None:
        super().__init__(timeout=None)

        # Custom ID format: remind:action:payload:user_id:message_id
        self.add_item(
            discord.ui.Button(
                label="15m",
                style=discord.ButtonStyle.blurple,
                emoji="💤",
                custom_id=f"remind:snooze:15:{user_id}:{message_id}",
            ),
        )
        self.add_item(
            discord.ui.Button(
                label="1h",
                style=discord.ButtonStyle.blurple,
                emoji="💤",
                custom_id=f"remind:snooze:60:{user_id}:{message_id}",
            ),
        )
        self.add_item(
            discord.ui.Button(
                label="1d",
                style=discord.ButtonStyle.blurple,
                emoji="💤",
                custom_id=f"remind:snooze:1440:{user_id}:{message_id}",
            ),
        )
        self.add_item(
            discord.ui.Button(
                label="Done",
                style=discord.ButtonStyle.green,
                emoji="✅",
                custom_id=f"remind:done:0:{user_id}:{message_id}",
            ),
        )

        if context_url:
            self.add_item(discord.ui.Button(label="Context", style=discord.ButtonStyle.link, url=context_url))


class Reminders(commands.Cog):
    def __init__(self, bot: KiwiBot, *, reminder_db: ReminderDB, user_db: UserDB) -> None:
        self.bot = bot
        self.reminder_db = reminder_db
        self.user_db = user_db
        self._timer_task: asyncio.Task | None = None
        self._next_reminder_msg_id: int | None = None

        # Start scheduling once the bot is ready
        self.bot.loop.create_task(self.start_scheduler())

    async def start_scheduler(self) -> None:
        await self.bot.wait_until_ready()
        await self.schedule_next()

    async def cog_unload(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()

    async def schedule_next(self) -> None:
        """Determine the next reminder in the DB and schedules the timer.

        Idempotent: safe to call multiple times; will efficiently update the running timer.
        """
        reminder = await self.reminder_db.get_next_reminder()

        if not reminder:
            # No reminders left in DB. Stop the timer.
            if self._timer_task:
                self._timer_task.cancel()
                self._timer_task = None
                self._next_reminder_msg_id = None
            return

        message_id, _, _, _, _, remind_at_str = reminder

        # Parse DB string back to UTC datetime
        remind_at = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

        # If we are already waiting for this specific reminder, do nothing.
        if self._timer_task and not self._timer_task.done() and self._next_reminder_msg_id == message_id:
            return

        # If we have a running task for a DIFFERENT (later) reminder, cancel it to make way for this earlier one.
        if self._timer_task:
            self._timer_task.cancel()

        self._next_reminder_msg_id = message_id

        # Calculate delay (negative delay means it's overdue and will fire immediately)
        now = datetime.now(UTC)
        delay = (remind_at - now).total_seconds()

        self._timer_task = self.bot.loop.create_task(self.wait_and_dispatch(max(0, delay), reminder))

    async def wait_and_dispatch(self, delay: float, reminder_data: tuple) -> None:
        """Sleeps for 'delay' seconds, then fires the reminder."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)

            # --- FIRE REMINDER LOGIC ---
            # 1. Send the Message
            # Unpack data safely (ignoring the 6th element which varies between get_next_reminder and get_due_reminders)
            message_id, user_id, guild_id, channel_id, msg_content = reminder_data[:5]

            # 1. Send the Message
            # 1. Send the Message
            await self.send_reminder(
                UserId(user_id),
                ChannelId(channel_id),
                MessageId(message_id),
                msg_content,
                GuildId(guild_id),
            )

            # 2. Cleanup
            # Delete from DB *after* attempt (or before, depending on your safety preference)
            # Deleting by message_id acts as the unique key
            await self.reminder_db.delete_reminder_by_message_id(message_id)

        except asyncio.CancelledError:
            # Task was cancelled because a newer, earlier reminder was added.
            # We explicitly pass here to allow the task to die gracefully.
            log.warning("Reminder timer cancelled.")
        except Exception:
            log.exception("Error in reminder dispatch")
        finally:
            self._next_reminder_msg_id = None
            await self.schedule_next()

    async def send_reminder(
        self,
        user_id: UserId,
        channel_id: ChannelId,
        message_id: MessageId,
        msg: str,
        guild_id: GuildId,
    ) -> None:
        """Refactored sending logic isolated from the loop."""
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.NotFound as err:
                    msg = "Channel not found"
                    raise discord.Forbidden(msg) from err

            # Check if user has access to the channel
            if channel.guild:
                member = channel.guild.get_member(user_id)
                if not member:
                    try:
                        member = await channel.guild.fetch_member(user_id)
                    except discord.NotFound as err:
                        msg = "User not in guild"
                        raise discord.Forbidden(msg) from err

                if not channel.permissions_for(member).view_channel:
                    msg = "User cannot view channel"
                    raise discord.Forbidden(msg)

            # Try to fetch original message for context
            original_msg = None
            with contextlib.suppress(discord.NotFound, discord.Forbidden):
                original_msg = await channel.fetch_message(message_id)

            # View Construction
            context_url = None
            if original_msg:
                context_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

            view = SnoozeView(user_id=user_id, message_id=message_id, context_url=context_url)

            content = f"⏰ <@{user_id}> Reminder: **{msg}**"

            if original_msg:
                await original_msg.reply(content, view=view)
            else:
                await channel.send(content, view=view)

        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Fallback to DM logic
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                if user:
                    view = SnoozeView(user_id=user_id, message_id=message_id)
                    await user.send(
                        f"⏰ Reminder: **{msg}**\n_(I couldn't message the original channel)_",
                        view=view,
                    )
            except Exception:  # noqa: BLE001
                log.warning("Reminder %s failed to deliver even to DM.", message_id)

    # --- Helper: Time Parsing Logic ---
    def _parse_time(self, text: str, tz: ZoneInfo) -> tuple[datetime | None, str]:
        """Parse text to find a date relative to the given timezone using dateparser."""
        now_in_tz = datetime.now(tz)

        settings = {
            "RELATIVE_BASE": now_in_tz,
            "TIMEZONE": str(tz),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        }

        dates = search_dates(text, languages=["en"], settings=settings)
        if not dates:
            return None, text

        captured_text, dt = dates[0]

        # Safe slicing removal using regex to respect word boundaries
        # This prevents "Sat" from matching inside "Saturn"
        esc_captured = re.escape(captured_text)
        pattern = re.compile(rf"\b{esc_captured}\b", re.IGNORECASE)

        match = pattern.search(text)
        if match:
            start, end = match.span()
            clean_text = text[:start] + text[end:]
        else:
            # Fallback if boundaries fail (e.g. strict punctuation handling)
            idx = text.find(captured_text)
            clean_text = text[:idx] + text[idx + len(captured_text) :] if idx != -1 else text

        # Clean up leading/trailing "at", "in" etc
        clean_text = re.sub(r"^(at|in|on)\s+", "", clean_text.strip(), flags=re.IGNORECASE).strip()
        if not clean_text:
            clean_text = text

        return dt, clean_text

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        """If a message is deleted, remove its associated reminder."""
        if message.id:
            await self.reminder_db.delete_reminder_by_message_id(message.id)
            # TRIGGER SCHEDULER: If we just deleted the reminder we were waiting for,
            # schedule_next() will pick the NEXT one in line.
            await self.schedule_next()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        if self.bot.user not in message.mentions:
            return

        # Check for "remind" keyword (case-insensitive)
        content = message.content.lower()
        if "remind" not in content:
            return

        clean_content = message.content
        clean_content = re.sub(r"<@!?\d+>", "", clean_content)
        clean_content = re.sub(r"^\s*remind\s*(me\s*)?(to\s*)?", "", clean_content, flags=re.IGNORECASE).strip()

        if not clean_content:
            return

        user_id = UserId(message.author.id)
        guild_id = GuildId(message.guild.id)
        tz = await self.user_db.get_timezone(user_id, guild_id)

        dt, reminder_msg = self._parse_time(clean_content, tz)

        if not dt:
            await message.reply("I couldn't figure out when to remind you. Try: `in 5 minutes` or `tomorrow at 5pm`.")
            return

        # Past Date Validation
        now_tz = datetime.now(dt.tzinfo)
        if dt < now_tz:
            await message.reply("❌ that time is in the past! Please provide a future date/time.")
            return

        await self.reminder_db.add_reminder(
            user_id,
            guild_id,
            ChannelId(message.channel.id),
            message.id,
            reminder_msg,
            dt,
        )

        # TRIGGER SCHEDULER: If this new reminder is sooner than the current one,
        # schedule_next() will automatically cancel the current wait and switch to this one.
        await self.schedule_next()

        ts = int(dt.timestamp())
        await message.add_reaction("⏰")
        await message.reply(f"Got it! I'll remind you: **{reminder_msg}** <t:{ts}:R>.")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle stateless interactions for reminders."""
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id")
        if not custom_id or not custom_id.startswith("remind:"):
            return

        # "remind:action:payload:user_id:message_id"
        parts = custom_id.split(":")
        if len(parts) < 5:
            return

        action = parts[1]
        payload = int(parts[2])
        target_user_id = int(parts[3])
        target_message_id = int(parts[4])

        # Security Check
        if interaction.user.id != target_user_id:
            await interaction.response.send_message("This isn't your reminder!", ephemeral=True)
            return

        if action == "done":
            content = interaction.message.content
            await interaction.response.edit_message(content=f"~~{content}~~ (Completed)", view=None)

        elif action == "snooze":
            minutes = payload
            await interaction.response.defer()

            # Extract content from the bot's own message
            # Expected format: "⏰ <@user_id> Reminder: **{msg}**"
            msg_content = "Reminder"
            if interaction.message and interaction.message.content:
                match = re.search(r"\*\*(.+?)\*\*", interaction.message.content)
                if match:
                    msg_content = match.group(1)

            remind_at = datetime.now(UTC) + timedelta(minutes=minutes)

            # We use target_message_id (original command ID) to keep the chain alive if possible,
            # or it just acts as a unique ID for this new reminder instance.
            await self.reminder_db.add_reminder(
                UserId(interaction.user.id),
                GuildId(interaction.guild.id),
                ChannelId(interaction.channel_id),
                target_message_id,
                msg_content,
                remind_at,
            )

            # TRIGGER SCHEDULER
            await self.schedule_next()

            ts = int(remind_at.timestamp())
            await interaction.followup.send(f"💤 Snoozed for {minutes}m! (Due: <t:{ts}:R>)", ephemeral=True)
            await interaction.message.delete()

    # --- Slash Command ---
    reminders_group = app_commands.Group(name="reminders", description="Manage your reminders")

    @reminders_group.command(name="list", description="List your active reminders.")
    async def remind_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        user_id = UserId(interaction.user.id)

        reminders = await self.reminder_db.get_active_reminders(user_id)
        if not reminders:
            await interaction.followup.send("You have no active reminders.", ephemeral=True)
            return

        embed = discord.Embed(title="Your Reminders", color=discord.Color.blue())
        tz = await self.user_db.get_timezone(user_id, GuildId(interaction.guild.id))

        for message_id, msg, remind_at_str in reminders:
            utc_dt = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            local_dt = utc_dt.astimezone(tz)
            embed.add_field(
                name=f"ID: {message_id} | {local_dt.strftime('%Y-%m-%d %H:%M')}",
                value=msg,
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @reminders_group.command(name="delete", description="Delete a reminder by ID.")
    @app_commands.describe(reminder_id="The ID of the reminder to delete (check /reminders list)")
    async def remind_delete(self, interaction: discord.Interaction, reminder_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        user_id = UserId(interaction.user.id)
        success = await self.reminder_db.delete_reminder(reminder_id, user_id)
        if success:
            await interaction.followup.send(f"✅ Reminder **{reminder_id}** deleted.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"❌ Reminder **{reminder_id}** not found or it doesn't belong to you.",
                ephemeral=True,
            )


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(Reminders(bot, reminder_db=bot.reminder_db, user_db=bot.user_db))

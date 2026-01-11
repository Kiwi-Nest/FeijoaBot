# new_daily.py
"""Handle '/daily' command.

Uses ephemeral messages to reduce channel spam.
"""

import asyncio
import datetime
import logging
import random
import time
from typing import TYPE_CHECKING, override
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from modules.dtypes import GuildId, PositiveInt, ReminderPreference, UserId
from modules.guild_cog import GuildOnlyHybridCog

if TYPE_CHECKING:
    # This avoids circular imports while providing type hints for the bot class
    from collections.abc import Iterable

    from modules.CurrencyLedgerDB import CurrencyLedgerDB
    from modules.KiwiBot import KiwiBot
    from modules.TaskDB import TaskDB
    from modules.UserDB import UserDB

log = logging.getLogger(__name__)

# Constants for magic values
JACKPOT_THRESHOLD = 1000


class DailyView(discord.ui.View):
    """An interactive UI for the ephemeral /daily command response.

    It provides buttons for reminder preferences and sharing the result publicly.
    """

    def __init__(
        self,
        bot: KiwiBot,
        user_db: UserDB,
        owner_id: UserId,
        daily_mon: int,
        new_balance: int,
        author: discord.User,
        channel: discord.TextChannel,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.user_db = user_db
        self.owner_id = owner_id
        self.daily_mon = daily_mon
        self.new_balance = new_balance
        self.author = author
        self.channel = channel

    def _create_share_embed(self) -> discord.Embed:
        """Create the public embed for when a user shares their daily claim."""
        is_jackpot = self.daily_mon > JACKPOT_THRESHOLD
        if is_jackpot:
            title = "🎉 JACKPOT! 🎉"
            description = f"**{self.author.mention} hit the jackpot and received ${self.daily_mon:,}!**"
            color = discord.Colour.orange()
        else:
            title = "💰 Daily Claim! 💰"
            description = f"{self.author.mention} received **${self.daily_mon:,}**!"
            color = discord.Colour.gold()

        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="New Balance", value=f"${self.new_balance:,}")
        embed.set_author(name=self.author.name, icon_url=self.author.display_avatar)
        embed.set_footer(text="What will they buy with it?")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _update_reminder_preference(
        self,
        interaction: discord.Interaction,
        preference: ReminderPreference,
    ) -> None:
        """Handle the logic for all reminder button clicks."""
        messages = {
            "ONCE": "Success! You will be reminded for your next claim.",
            "ALWAYS": "Success! Your reminder preference is now set to 'Always'.",
            "NEVER": "Success! Reminders have been disabled.",
        }
        if interaction.guild:
            await self.user_db.set_daily_reminder_preference(self.owner_id, preference, GuildId(interaction.guild.id))
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(messages[preference], ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure that only the user who ran the command can interact with the view."""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This isn't your daily claim UI.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Remind Me Once",
        style=discord.ButtonStyle.secondary,
        emoji="⏰",
    )
    async def remind_once_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self._update_reminder_preference(interaction, "ONCE")

    @discord.ui.button(
        label="Always Remind Me",
        style=discord.ButtonStyle.success,
        emoji="🔁",
    )
    async def always_remind_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self._update_reminder_preference(interaction, "ALWAYS")

    @discord.ui.button(
        label="Disable Reminders",
        style=discord.ButtonStyle.danger,
        emoji="🔕",
    )
    async def disable_reminders_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self._update_reminder_preference(interaction, "NEVER")

    @discord.ui.button(
        label="Share to Channel",
        style=discord.ButtonStyle.primary,
        emoji="📢",
    )
    async def share_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        embed = self._create_share_embed()
        await self.channel.send(embed=embed)  # Use the stored channel for robustness

        button.disabled = True
        button.label = "Shared!"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "Your daily claim has been shared!",
            ephemeral=True,
        )


class Daily(GuildOnlyHybridCog):
    def __init__(
        self,
        bot: KiwiBot,
        *,
        user_db: UserDB,
        task_db: TaskDB,
        ledger_db: CurrencyLedgerDB,
    ) -> None:
        self.bot = bot
        self.user_db = user_db
        self.task_db = task_db
        self.ledger_db = ledger_db
        self.daily_management_task.start()

    @override
    async def cog_unload(self) -> None:
        """Clean up when the cog is unloaded."""
        self.daily_management_task.cancel()

    @tasks.loop(time=datetime.time(0, 0, tzinfo=ZoneInfo("Pacific/Auckland")))
    async def daily_management_task(self) -> None:
        """Handle daily resets and reminders using pure bulk operations."""
        log.info("Starting daily management task...")

        try:
            # One atomic database call for all guilds.
            users_to_remind = await self.user_db.process_daily_reset_all()

            if users_to_remind:
                log.info("Preparing to send %d daily reminders.", len(users_to_remind))
                # Use a set to automatically handle duplicate user IDs if they exist across guilds
                await self.send_reminders(set(users_to_remind))

        except Exception:
            log.exception("An error occurred during the daily management task.")
        finally:
            # After running, persist the *next* run time to the database.
            next_run_time = self.daily_management_task.next_iteration
            if next_run_time:
                log.info("Persisting next DAILY_RESET time: %s", next_run_time.isoformat())
                await self.task_db.schedule_task("DAILY_RESET", next_run_time.timestamp())

    @daily_management_task.before_loop
    async def before_daily_management_task(self) -> None:
        """Wait until the bot is ready and handle any missed runs."""
        await self.bot.wait_until_ready()

        # Check if a reset was missed while the bot was offline.
        pending_task = await self.task_db.get_pending_task("DAILY_RESET")
        if pending_task:
            _task_type, due_timestamp = pending_task
            if due_timestamp - time.time() <= 0:
                log.info("DAILY_RESET was missed. Running it now before starting loop.")
                # Run the task logic directly, not the loop itself
                await self.daily_management_task.coro(self)
                # The task will persist its *next* run time inside the `finally` block.

        # Persist the next scheduled run time to the DB before the loop starts sleeping.
        next_run_time = self.daily_management_task.next_iteration
        if next_run_time:
            log.info("Persisting next DAILY_RESET time: %s", next_run_time.isoformat())
            await self.task_db.schedule_task("DAILY_RESET", next_run_time.timestamp())
        else:
            log.info("Daily task loop has no missed run.")

    async def send_reminders(self, user_ids: Iterable[UserId]) -> None:
        """Send reminders to a list of users sequentially to avoid rate limits."""
        reminder_message = "⏰ Your daily reward is ready to claim! Use `/daily` to get your reward."
        success_count = 0
        total_count = 0

        for user_id in user_ids:
            total_count += 1
            try:
                user = await self.bot.fetch_user(user_id)
                await user.send(reminder_message)
                success_count += 1
            except (discord.Forbidden, discord.NotFound):
                log.warning(
                    "Could not send reminder to user %d (DMs disabled or user not found).",
                    user_id,
                )
            except discord.HTTPException:
                log.exception("Failed to send reminder to user %d due to an HTTP error.", user_id)
            finally:
                # Wait for a short duration between messages to respect rate limits.
                await asyncio.sleep(1)

        log.info(
            "Successfully sent %d out of %d daily reminders.",
            success_count,
            total_count,
        )

    @commands.hybrid_command(name="daily", description="Claim your daily currency.")
    async def daily(self, ctx: commands.Context) -> None:
        # Atomically attempt to claim the daily. If it fails, they've already claimed.
        if not await self.user_db.attempt_daily_claim(UserId(ctx.author.id), GuildId(ctx.guild.id)):
            embed = discord.Embed(
                title="Already Claimed",
                description="You have already claimed your daily reward! Wait for the next reset at midnight Auckland time.",
                color=discord.Colour.red(),
            )
            await ctx.send(embed=embed, ephemeral=True)
            return

        # Simplified reward logic: 1% chance for a jackpot, 99% for a standard reward.
        daily_mon = PositiveInt(random.randint(100, 1000) if random.random() < 0.01 else random.randint(20, 50))

        new_balance = await self.user_db.mint_currency(
            user_id=UserId(ctx.author.id),
            guild_id=GuildId(ctx.guild.id),
            amount=daily_mon,
            event_reason="DAILY_CLAIM",
            ledger_db=self.ledger_db,
        )

        log.info(
            "User %s claimed $%s, new balance is $%s",
            ctx.author.display_name,
            daily_mon,
            new_balance,
        )

        title = "🎉 Daily Claim Successful! 🎉"
        if daily_mon > JACKPOT_THRESHOLD:
            title = "🎊 JACKPOT! 🎊"

        response_content = (
            f"### {title}\n"
            f"You have received **${daily_mon:,}**!\n"
            f"Your new balance is **${new_balance:,}**.\n\n"
            f"Your next claim will be available after the daily reset at midnight NZ time."
        )

        view = DailyView(
            bot=self.bot,
            user_db=self.user_db,
            owner_id=UserId(ctx.author.id),
            daily_mon=daily_mon,
            new_balance=new_balance,
            author=ctx.author,
            channel=ctx.channel,
        )

        await ctx.send(response_content, view=view, ephemeral=True)


async def setup(bot: KiwiBot) -> None:
    """Add the Daily cog to the bot."""
    await bot.add_cog(
        Daily(
            bot=bot,
            user_db=bot.user_db,
            task_db=bot.task_db,
            ledger_db=bot.ledger_db,
        ),
    )

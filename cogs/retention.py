"""Automated data retention and cleanup task."""

import logging
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

if TYPE_CHECKING:
    from modules.BotCore import BotCore

log = logging.getLogger(__name__)


class Retention(commands.Cog):
    """Weekly data retention and cleanup task for GDPR compliance."""

    bot: BotCore

    def __init__(self, bot: BotCore) -> None:
        self.bot = bot
        self.cleanup_task.start()

    async def cog_unload(self) -> None:
        """Cancel the cleanup task when the cog is unloaded."""
        self.cleanup_task.cancel()

    @tasks.loop(hours=168)  # Weekly
    async def cleanup_task(self) -> None:
        """Weekly cleanup: purge stale reminders, inactive users, and old VC snapshots."""
        try:
            stale_reminders = await self.bot.reminder_db.purge_stale(days=90)
            inactive_users = await self.bot.user_db.purge_inactive(days=730)
            old_vc = await self.bot.voicechat_db.delete_old_sessions(days=90)

            log.info(
                "Data retention run: %d stale reminders, %d inactive users, %d VC sessions deleted",
                stale_reminders,
                inactive_users,
                old_vc,
            )

        except Exception:
            log.exception("Error during data retention cleanup")

    @cleanup_task.before_loop
    async def before_cleanup_task(self) -> None:
        """Wait for the bot to be ready before starting the cleanup task."""
        await self.bot.wait_until_ready()


async def setup(bot: BotCore) -> None:
    """Load the retention cog."""
    await bot.add_cog(Retention(bot=bot))

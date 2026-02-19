import logging

import discord
from discord.ext import commands, tasks

from modules.dtypes import GuildId, UserGuildPair, UserId
from modules.KiwiBot import KiwiBot
from modules.UserDB import UserDB

log = logging.getLogger(__name__)


class Activity(commands.Cog):
    """Handle user activity tracking and database updates."""

    def __init__(self, bot: KiwiBot, *, user_db: UserDB) -> None:
        self.bot = bot
        self.user_db = user_db
        self.activity_cache: set[UserGuildPair] = set()
        self.flush_activity_cache.start()

    async def cog_unload(self) -> None:
        """Cancel the background task when the cog is unloaded."""
        self.flush_activity_cache.cancel()

    def _cache_user_activity(self, user: discord.User | discord.Member, guild_id: GuildId) -> None:
        """Add a user and their guild to the activity cache."""
        if user.bot:
            return

        self.activity_cache.add((UserId(user.id), guild_id))
        log.debug("Cached activity for user %d in guild %d", user.id, guild_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen to messages to track user activity."""
        if message.guild:
            self._cache_user_activity(message.author, GuildId(message.guild.id))

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Listen to interactions to track user activity."""
        if interaction.guild and interaction.user:
            self._cache_user_activity(interaction.user, GuildId(interaction.guild.id))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Listen to voice state changes to track user activity."""
        # Any change in voice state (joining, leaving, moving, muting, etc.)
        # counts as activity in that guild.
        # We check if they are moving to or from a channel.
        if (after.channel or before.channel) and not member.bot:
            self._cache_user_activity(member, GuildId(member.guild.id))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Listen to raw reaction additions to track user activity."""
        # payload.member is available on reaction_add, making the bot check easy
        if payload.member:
            self._cache_user_activity(payload.member, GuildId(payload.member.guild.id))

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Listen to raw reaction removals to track user activity."""
        if not payload.guild_id:
            return

        # member is not provided on reaction_remove, so we get from cache
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if member:  # Only log if member is cached
            self._cache_user_activity(member, GuildId(guild.id))

    @commands.Cog.listener()
    async def on_raw_typing(self, payload: discord.RawTypingEvent) -> None:
        """Listen to typing events to track user activity."""
        # payload.user is a Member object if in a guild and cached
        if payload.user and payload.guild_id:
            self._cache_user_activity(payload.user, GuildId(payload.guild_id))

    @commands.Cog.listener()
    async def on_raw_poll_vote_add(self, payload: discord.RawPollVoteActionEvent) -> None:
        """Listen to poll votes to track user activity."""
        if not payload.guild_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if member:  # Only log if member is cached
            self._cache_user_activity(member, GuildId(guild.id))

    @commands.Cog.listener()
    async def on_raw_poll_vote_remove(self, payload: discord.RawPollVoteActionEvent) -> None:
        """Listen to poll vote removals to track user activity."""
        if not payload.guild_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if member:  # Only log if member is cached
            self._cache_user_activity(member, GuildId(guild.id))

    @commands.Cog.listener()
    async def on_thread_member_join(self, member: discord.ThreadMember) -> None:
        """Listen for when a member actively joins a thread."""
        guild = member.thread.guild
        cached_member = guild.get_member(member.id)
        if cached_member:
            self._cache_user_activity(cached_member, GuildId(guild.id))

    @commands.Cog.listener()
    async def on_thread_member_remove(self, member: discord.ThreadMember) -> None:
        """Listen for when a member actively leaves a thread."""
        guild = member.thread.guild
        cached_member = guild.get_member(member.id)
        if cached_member:
            self._cache_user_activity(cached_member, GuildId(guild.id))

    @commands.Cog.listener()
    async def on_scheduled_event_user_add(
        self,
        event: discord.ScheduledEvent,
        user: discord.User,
    ) -> None:
        """Listen for when a user RSVPs 'Interested' to an event."""
        if event.guild:
            self._cache_user_activity(user, GuildId(event.guild.id))

    @commands.Cog.listener()
    async def on_scheduled_event_user_remove(
        self,
        event: discord.ScheduledEvent,
        user: discord.User,
    ) -> None:
        """Listen for when a user removes their 'Interested' RSVP."""
        if event.guild:
            self._cache_user_activity(user, GuildId(event.guild.id))

    @tasks.loop(seconds=60)
    async def flush_activity_cache(self) -> None:
        """Periodically flush the activity cache to the database."""
        if not self.activity_cache:
            return

        # We need to know which guilds to log to *before* we clear the cache
        guild_ids_in_batch = {guild_id for _, guild_id in self.activity_cache}

        # Create a copy to avoid race conditions if new activity comes in
        # while the database operation is running.
        activity_to_flush = list(self.activity_cache)
        self.activity_cache.clear()

        try:
            await self.user_db.update_active_users(activity_to_flush)
            log.debug(
                "Flushed %d user activities to database",
                len(activity_to_flush),
            )
        except Exception:
            log.exception("Error in flush_activity_cache background task")
            # If the flush fails, put the data back in the cache to try again next time.
            # This prevents data loss on a temporary DB error.
            self.activity_cache.update(activity_to_flush)

            for guild_id in guild_ids_in_batch:
                self.bot.dispatch(
                    "security_alert",
                    guild_id=guild_id,
                    risk_level="HIGH",
                    details=(
                        "**Database Flush Failed**\n"
                        "An error occurred in the `flush_activity_cache` background task. "
                        "User activity is not being saved. This may be a database issue. "
                        "Check console logs for details."
                    ),
                    warning_type="db_flush_fail",
                    cooldown_seconds=7200,  # 2 hours
                )

    @flush_activity_cache.before_loop
    async def before_flush_activity_cache(self) -> None:
        """Wait for the bot to be ready before starting the loop."""
        await self.bot.wait_until_ready()


async def setup(bot: KiwiBot) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(Activity(bot, user_db=bot.user_db))

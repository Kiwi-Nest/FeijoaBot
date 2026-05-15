from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules import heatmap as _heatmap
from modules.dtypes import GuildId, GuildInteraction, UserId
from modules.guild_cog import GuildOnlyHybridCog
from modules.vc_color import streak_color

if TYPE_CHECKING:
    from modules.BotCore import BotCore
    from modules.ConfigDB import ConfigDB
    from modules.VoiceChatDB import VoiceChatDB

log = logging.getLogger(__name__)


@dataclass
class GuildStreak:
    started_at: datetime
    peak: int
    participants: set[UserId] = field(default_factory=set)


def _fmt_duration(d: timedelta) -> str:
    total = int(d.total_seconds())
    h, remainder = divmod(total, 3600)
    m = remainder // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _build_activity_embed(duration: timedelta, peak: int, unique_count: int) -> discord.Embed:
    r, g, b = streak_color(duration)
    embed = discord.Embed(
        title="VC Session Ended",
        colour=discord.Colour.from_rgb(r, g, b),
        timestamp=datetime.now(UTC),
    )
    embed.add_field(name="Duration", value=_fmt_duration(duration), inline=True)
    embed.add_field(name="Peak", value=str(peak), inline=True)
    embed.add_field(name="Unique", value=str(unique_count), inline=True)
    return embed


class VoiceChatLogger(GuildOnlyHybridCog):
    def __init__(self, bot: BotCore, *, voicechat_db: VoiceChatDB, config_db: ConfigDB) -> None:
        self.bot = bot
        self.voicechat_db = voicechat_db
        self.config_db = config_db
        self._streaks: dict[GuildId, GuildStreak] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._flush.start()

    async def cog_unload(self) -> None:
        self._flush.cancel()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        was_in_vc = before.channel is not None
        is_in_vc = after.channel is not None
        if was_in_vc == is_in_vc:
            return

        guild_id = GuildId(member.guild.id)
        user_id = UserId(member.id)

        if is_in_vc:
            await self.voicechat_db.record_join(guild_id, user_id)
        else:
            await self.voicechat_db.record_leave(guild_id, user_id)

        live_count = sum(1 for vc in member.guild.voice_channels for m in vc.members if not m.bot)
        if live_count > 0:
            await self.voicechat_db.write_slot_snapshot(guild_id, live_count)

        now = datetime.now(UTC)

        if is_in_vc:
            if guild_id not in self._streaks:
                self._streaks[guild_id] = GuildStreak(started_at=now, peak=live_count, participants={user_id})
            else:
                streak = self._streaks[guild_id]
                streak.participants.add(user_id)
                streak.peak = max(streak.peak, live_count)
        elif live_count == 0 and guild_id in self._streaks:
            streak = self._streaks.pop(guild_id)
            task = asyncio.create_task(self._on_vc_death(member.guild, streak, now))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _on_vc_death(self, guild: discord.Guild, streak: GuildStreak, ended_at: datetime) -> None:
        duration = ended_at - streak.started_at
        if duration < timedelta(seconds=60):
            return
        config = await self.config_db.get_guild_config(GuildId(guild.id))
        if not config.vc_activity_channel_id:
            return
        channel = guild.get_channel(int(config.vc_activity_channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        if streak.peak < 2:
            return
        unique = len(streak.participants)
        try:
            await channel.send(embed=_build_activity_embed(duration, streak.peak, unique))
        except discord.HTTPException:
            log.exception("Failed to post VC activity report for guild %s", guild.id)

    @tasks.loop(seconds=60)
    async def _flush(self) -> None:
        for guild in self.bot.guilds:
            guild_id = GuildId(guild.id)
            live_count = sum(1 for vc in guild.voice_channels for m in vc.members if not m.bot)

            if live_count > 0:
                await self.voicechat_db.write_slot_snapshot(guild_id, live_count)

            streak = self._streaks.get(guild_id)
            if streak is None:
                continue
            config = await self.config_db.get_guild_config(guild_id)
            if not config.vc_rgb_role_id:
                continue
            role = guild.get_role(int(config.vc_rgb_role_id))
            if role is None:
                continue
            try:
                r, g, b = streak_color(datetime.now(UTC) - streak.started_at)
                await role.edit(colour=discord.Colour.from_rgb(r, g, b))
            except Exception:
                log.exception("Failed to update RGB role for guild %s", guild_id)

    @_flush.before_loop
    async def _before_flush(self) -> None:
        await self.bot.wait_until_ready()

        now = datetime.now(UTC)

        for guild in self.bot.guilds:
            guild_id = GuildId(guild.id)
            current = {UserId(m.id) for vc in guild.voice_channels for m in vc.members if not m.bot}
            await self.voicechat_db.reconcile_sessions(guild_id, current)

            if not current:
                continue

            result = await self.voicechat_db.infer_streak(guild_id)
            if result is None:
                self._streaks[guild_id] = GuildStreak(
                    started_at=now,
                    peak=len(current),
                    participants=set(current),
                )
            else:
                started_ts, peak = result
                started_at = datetime.fromtimestamp(started_ts, UTC)
                raw_participants = await self.voicechat_db.get_streak_participants(guild_id, started_ts)
                self._streaks[guild_id] = GuildStreak(
                    started_at=started_at,
                    peak=max(peak, len(current)),
                    participants={UserId(uid) for uid in raw_participants} | current,
                )

    @app_commands.command(name="vcinfo", description="Voice channel activity for this guild or a specific user.")
    async def vcinfo(self, interaction: GuildInteraction, user: discord.Member | None = None) -> None:
        await interaction.response.defer(ephemeral=False)
        guild_id = GuildId(interaction.guild.id)

        if user is None:
            peak = await self.voicechat_db.guild_peak_today(guild_id)
            live_count = sum(1 for vc in interaction.guild.voice_channels for m in vc.members if not m.bot)
            embed = discord.Embed(title="VC Activity", color=discord.Color.blurple())
            if peak:
                max_count, slot = peak
                hour = (slot * 5) // 60
                minute = (slot * 5) % 60
                embed.add_field(name="Peak today", value=f"{max_count} users at {hour:02d}:{minute:02d} UTC", inline=False)
            else:
                embed.add_field(name="Peak today", value="No data yet", inline=False)
            embed.add_field(name="Currently live", value=str(live_count), inline=False)
        else:
            minutes, last_seen = await self.voicechat_db.user_stats_today(guild_id, UserId(user.id))
            embed = discord.Embed(title=f"VC Activity - {user.display_name}", color=discord.Color.blurple())
            embed.add_field(name="Minutes in VC today", value=str(minutes), inline=False)
            embed.add_field(name="Last seen", value=last_seen or "Not today", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="vcheatmap", description="VC activity heatmap for this guild.")
    async def vcheatmap(self, interaction: GuildInteraction) -> None:
        if not _heatmap.AVAILABLE:
            await interaction.response.send_message("Heatmap unavailable (matplotlib not installed).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)
        rows = await self.voicechat_db.read_heatmap_data(GuildId(interaction.guild.id))
        if not rows:
            await interaction.followup.send("No data yet.", ephemeral=True)
            return
        buf = await asyncio.to_thread(_heatmap.create_heatmap, rows, "VC Activity")
        await interaction.followup.send(file=discord.File(buf, filename="vcheatmap.png"), ephemeral=False)


async def setup(bot: BotCore) -> None:
    await bot.add_cog(VoiceChatLogger(bot, voicechat_db=bot.voicechat_db, config_db=bot.config_db))

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import ChannelId, GuildId, MessageId
from modules.result import Ok

if TYPE_CHECKING:
    import aiohttp

    from modules.BotCore import BotCore


@dataclass(slots=True)
class RangeCount:
    count: int
    is_approximate: bool
    error_reason: str | None = None


@dataclass(slots=True)
class DeletionState:
    cancelled: bool = False
    deleted: int = 0


def _preview(content: str) -> str:
    """Truncate message content to 50 chars with ellipsis."""
    return content[:50] + ("…" if len(content) > 50 else "")


def _parse_message_id(value: str) -> MessageId | None:
    """Parse message ID from raw snowflake or Discord message link."""
    value = value.strip()
    msg_id = None

    if value.isdigit():
        msg_id = int(value)
    else:
        parts = value.rstrip("/").split("/")
        if len(parts) >= 1 and parts[-1].isdigit():
            msg_id = int(parts[-1])

    if msg_id is not None and 1e17 <= msg_id < 1e19:
        return MessageId(msg_id)
    return None


def _same_channel(raw: str, channel: discord.TextChannel) -> bool:
    """Check if a message link/ID is in the same channel."""
    if "discord.com/channels/" in raw:
        parts = raw.rstrip("/").split("/")
        if len(parts) >= 3 and parts[-2].isdigit():
            return int(parts[-2]) == channel.id
    return True


async def count_range(
    channel: discord.TextChannel,
    guild: discord.Guild,
    lo_id: MessageId,
    hi_id: MessageId,
    http_session: aiohttp.ClientSession,
) -> RangeCount:
    """Count exact messages in range using Discord search API.

    Falls back to history fetch (capped at 100) if API fails.
    """
    from modules.discord_search import (  # noqa: PLC0415
        count_messages_in_range,
    )

    result = await count_messages_in_range(
        http_session,
        GuildId(guild.id),
        ChannelId(channel.id),
        MessageId(lo_id - 1),
        MessageId(hi_id + 1),
    )

    if isinstance(result, Ok):
        return RangeCount(result.value, is_approximate=False)

    msgs = [
        m
        async for m in channel.history(
            after=discord.Object(MessageId(lo_id - 1)),
            before=discord.Object(MessageId(hi_id + 1)),
            limit=100,
        )
    ]
    return RangeCount(len(msgs), is_approximate=True, error_reason=result.error.reason)


def _phase2_embed(
    msg_a: discord.Message,
    msg_b: discord.Message,
    range_count: RangeCount,
) -> discord.Embed:
    """Embed showing deletion range confirmation."""
    title = "⚠️ Deletion Range Preview" if range_count.count > 1000 else "✓ Deletion Range Preview"
    color = discord.Color.orange() if range_count.count > 1000 else discord.Color.blue()
    embed = discord.Embed(title=title, color=color)

    embed.add_field(
        name="START",
        value=f'{msg_a.author} · {discord.utils.format_dt(msg_a.created_at)}\n"{_preview(msg_a.content)}"',
        inline=False,
    )
    embed.add_field(
        name="END",
        value=f'{msg_b.author} · {discord.utils.format_dt(msg_b.created_at)}\n"{_preview(msg_b.content)}"',
        inline=False,
    )

    embed.add_field(name="Messages in range", value=f"**{range_count.count:,}**", inline=False)

    if range_count.is_approximate:
        reason = range_count.error_reason or "search unavailable"
        embed.add_field(
            name="⚠️ Approximate Count",
            value=f"Count capped at 100 messages ({reason}). Actual range may be larger.",
            inline=False,
        )

    all_msgs = [msg_a, msg_b]
    authors = {}
    for m in all_msgs:
        authors[m.author] = authors.get(m.author, 0) + 1
    author_str = ", ".join(f"{a} ({c})" for a, c in sorted(authors.items(), key=lambda x: -x[1])[:3])
    embed.add_field(name="Authors", value=author_str, inline=False)

    if range_count.count > 1000:
        embed.add_field(
            name="⚠️ Large Range",
            value="Administrator permission required to proceed.",
            inline=False,
        )

    embed.set_footer(text="This action cannot be undone.")
    return embed


def _inflight_embed(state: DeletionState) -> discord.Embed:
    """Embed showing deletion in progress."""
    return discord.Embed(
        title="🗑️ Deleting…",
        description=f"Deleted **{state.deleted}** messages so far",
        color=discord.Color.greyple(),
    )


def _done_embed(deleted: int, cancelled: bool, user: discord.User | None = None) -> discord.Embed:
    """Embed showing deletion result."""
    title = "🛑 Cancelled" if cancelled else "✅ Done"
    user_mention = f" by {user.mention}" if user else ""
    desc = f"Deleted **{deleted}** messages{user_mention}" + (" before cancellation." if cancelled else ".")
    color = discord.Color.red() if cancelled else discord.Color.green()
    return discord.Embed(title=title, description=desc, color=color)


class InvokerOnlyView(discord.ui.View):
    """Base view that restricts interaction to the invoker and staff."""

    def __init__(self, invoker: discord.User, **kwargs) -> None:
        super().__init__(**kwargs)
        self.invoker = invoker

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        can_interact = interaction.user.id == self.invoker.id or interaction.user.guild_permissions.manage_messages
        if not can_interact:
            await interaction.response.send_message(
                "You cannot interact with this.",
                ephemeral=True,
            )
            return False
        return True


class ConfirmRangeView(InvokerOnlyView):
    """Confirmation and deletion control for a message range."""

    message: discord.Message | None = None

    def __init__(
        self,
        cog: DeleteRangeCog,
        invoker: discord.User,
        lo_id: MessageId,
        hi_id: MessageId,
        channel: discord.TextChannel,
        range_count: RangeCount,
        msg_a: discord.Message,
        msg_b: discord.Message,
    ) -> None:
        super().__init__(invoker, timeout=cog.CONFIRM_TIMEOUT)
        self.cog = cog
        self.lo_id = lo_id
        self.hi_id = hi_id
        self.channel = channel
        self.range_count = range_count
        self.msg_a = msg_a
        self.msg_b = msg_b

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(
                    content="⏰ Confirmation expired.",
                    embed=None,
                    view=self,
                )

    @discord.ui.button(label="Delete Range", style=discord.ButtonStyle.danger, emoji="✅")
    async def delete_range(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.range_count.count > 1000 and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ Ranges over 1000 messages require Administrator permission.",
                ephemeral=True,
            )
            return

        state = DeletionState()
        inflight_view = InFlightView(state, interaction.user)

        await interaction.response.edit_message(
            embed=_inflight_embed(state),
            view=inflight_view,
        )

        guild_config = await self.cog.bot.config_db.get_guild_config(GuildId(interaction.guild_id))
        if guild_config.mod_log_channel_id:
            modlog_channel = interaction.guild.get_channel(guild_config.mod_log_channel_id)
            if modlog_channel and isinstance(modlog_channel, discord.TextChannel):
                with contextlib.suppress(discord.HTTPException):
                    await modlog_channel.send(
                        content=f"Deletion started by {interaction.user.mention}",
                        embed=_phase2_embed(self.msg_a, self.msg_b, self.range_count),
                    )

        task = asyncio.create_task(
            _run_and_report(
                interaction,
                self.channel,
                self.lo_id,
                self.hi_id,
                state,
            ),
        )
        self.cog._deletion_tasks.add(task)
        task.add_done_callback(self.cog._deletion_tasks.discard)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="❌ Cancelled.",
            embed=None,
            view=None,
        )


class InFlightView(InvokerOnlyView):
    """Control panel for active deletion."""

    def __init__(self, state: DeletionState, invoker: discord.User) -> None:
        super().__init__(invoker, timeout=600.0)
        self.state = state

    @discord.ui.button(label="Cancel Deletion", style=discord.ButtonStyle.red, emoji="🛑")
    async def cancel_deletion(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.state.cancelled = True
        button.disabled = True
        await interaction.response.edit_message(
            content="Cancelling…",
            view=self,
        )


class DeleteRangeModal(discord.ui.Modal, title="Delete Range"):
    """Modal to select the end boundary message."""

    end_input = discord.ui.TextInput(
        label="End message - paste a link or ID",
        placeholder="https://discord.com/channels/... or 1234567890",
        min_length=17,
        max_length=120,
    )

    def __init__(self, cog: DeleteRangeCog, start_message: discord.Message) -> None:
        super().__init__()
        self.cog = cog
        self.start_message = start_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            channel = self.start_message.channel
        except AttributeError, ValueError:
            await interaction.response.send_message(
                "⚠️ Start message is no longer available.",
                ephemeral=True,
            )
            return

        end_id = _parse_message_id(self.end_input.value)
        if end_id is None:
            await interaction.response.send_message(
                "⚠️ Couldn't parse that as a message ID or link.",
                ephemeral=True,
            )
            return

        if not _same_channel(self.end_input.value, channel):
            await interaction.response.send_message(
                "⚠️ That message is in a different channel.",
                ephemeral=True,
            )
            return

        try:
            end_message = await channel.fetch_message(end_id)
        except discord.NotFound:
            await interaction.response.send_message(
                "⚠️ Couldn't find that message.",
                ephemeral=True,
            )
            return
        except discord.Forbidden, discord.HTTPException:
            await interaction.response.send_message(
                "⚠️ Couldn't fetch that message.",
                ephemeral=True,
            )
            return

        if end_message.id == self.start_message.id:
            await interaction.response.send_message(
                "⚠️ Start and end are the same message.",
                ephemeral=True,
            )
            return

        lo_id = MessageId(min(self.start_message.id, end_message.id))
        hi_id = MessageId(max(self.start_message.id, end_message.id))
        msg_a = self.start_message if self.start_message.id < end_message.id else end_message
        msg_b = end_message if self.start_message.id < end_message.id else self.start_message

        range_count = await count_range(
            channel,
            interaction.guild,
            lo_id,
            hi_id,
            self.cog.bot.http_session,
        )
        view = ConfirmRangeView(self.cog, interaction.user, lo_id, hi_id, channel, range_count, msg_a, msg_b)

        await interaction.response.send_message(
            embed=_phase2_embed(msg_a, msg_b, range_count),
            view=view,
            ephemeral=False,
        )
        view.message = await interaction.original_response()


async def _delete_range(
    channel: discord.TextChannel,
    lo_id: MessageId,
    hi_id: MessageId,
    state: DeletionState,
) -> None:
    """Stateless deletion loop: re-query the same window until empty."""
    cutoff = discord.utils.utcnow() - timedelta(days=14)

    while not state.cancelled:
        batch = [
            m
            async for m in channel.history(
                after=discord.Object(MessageId(lo_id - 1)),
                before=discord.Object(MessageId(hi_id + 1)),
                limit=100,
            )
        ]
        if not batch:
            break

        recent = [m for m in batch if m.created_at > cutoff]
        old = [m for m in batch if m.created_at <= cutoff]

        if recent:
            try:
                await channel.delete_messages(recent)
                state.deleted += len(recent)
            except discord.HTTPException as e:
                retry_delay = getattr(e, "retry_after", 1) or 1
                for msg in recent:
                    if state.cancelled:
                        return
                    try:
                        await msg.delete()
                        state.deleted += 1
                    except discord.HTTPException:
                        pass
                    await asyncio.sleep(retry_delay)

        for msg in old:
            if state.cancelled:
                return
            try:
                await msg.delete()
                state.deleted += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(1)


async def _run_and_report(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    lo_id: MessageId,
    hi_id: MessageId,
    state: DeletionState,
) -> None:
    """Run deletion and update the interaction with final result."""
    await _delete_range(channel, lo_id, hi_id, state)
    embed = _done_embed(state.deleted, state.cancelled, interaction.user)
    with contextlib.suppress(discord.HTTPException):
        await interaction.edit_original_response(embed=embed, view=None)


class DeleteRangeCog(commands.Cog):
    """Delete messages in a range selected via context menu."""

    CONFIRM_TIMEOUT: ClassVar[float] = 120.0

    def __init__(self, bot: BotCore) -> None:
        self.bot = bot
        self._deletion_tasks: set = set()
        self._menu = app_commands.ContextMenu(
            name="Delete Range",
            callback=self.delete_range_menu,
        )
        self._menu.default_permissions = discord.Permissions(manage_messages=True)
        bot.tree.add_command(self._menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self._menu.name, type=self._menu.type)
        for task in self._deletion_tasks:
            task.cancel()

    async def delete_range_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in servers.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(DeleteRangeModal(self, message))


async def setup(bot: BotCore) -> None:
    await bot.add_cog(DeleteRangeCog(bot))

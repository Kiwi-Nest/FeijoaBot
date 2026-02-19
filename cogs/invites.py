import logging
from datetime import datetime
from typing import Final

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, InviterId, UserId
from modules.guild_cog import GuildOnlyHybridCog
from modules.InvitesDB import InvitesDB
from modules.KiwiBot import KiwiBot

log = logging.getLogger(__name__)

SECOND_COOLDOWN: Final[int] = 1


class InvitesCog(GuildOnlyHybridCog):
    """A cog for tracking and displaying invite information."""

    # 1. Define the parent group for all invite commands
    invites = app_commands.Group(name="invites", description="Commands for invite tracking.")

    def __init__(self, bot: KiwiBot, invites_db: InvitesDB) -> None:  # Removed guild_id, alert_channel_id from init
        self.bot = bot
        self.invites_db = invites_db
        self.invites: dict[GuildId, dict[str, int]] = {}  # Cache still needed for invite diffing

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Cache invites for all guilds on startup."""
        await self.recache_all_invites()

    async def recache_all_invites(self) -> None:
        """Clear and re-populate the invite cache for all guilds."""
        self.invites.clear()
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                # Store invites with the code as the key and the uses as the value.
                self.invites[GuildId(guild.id)] = {invite.code: invite.uses for invite in await guild.invites()}
                log.info(
                    "Successfully cached %s invites for guild %s.",
                    len(self.invites[GuildId(guild.id)]),
                    guild.name,
                )
            except discord.Forbidden:
                log.warning(
                    "Bot lacks 'Manage Server' permissions to fetch invites for guild %s.",
                    guild.name,
                )
                self.bot.dispatch(
                    "security_alert",
                    guild_id=guild.id,
                    risk_level="HIGH",
                    details=(
                        "**Invite Tracking Failed**\n"
                        "I cannot track invites because I am missing the `Manage Server` permission.\n"
                        "Invite tracking will be disabled until this is fixed."
                    ),
                    warning_type="invite_tracking_fail",
                )
            except discord.HTTPException:
                log.exception(
                    "An HTTP error occurred while fetching invites for guild %s.",
                    guild.name,
                )

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        """Re-cache invites when the bot resumes a session to prevent stale data."""
        log.info("Session resumed, re-caching all invites.")
        await self.recache_all_invites()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Handle new members joining the server and finds the inviter by diffing invite uses."""
        if member.bot:
            return

        inviter_id: InviterId = None
        invite_code: str | None = None

        try:
            inviter = None
            try:
                guild_invites = self.invites.get(member.guild.id, {})
                current_invites = await member.guild.invites()

                # Compare current invites with the cached invites to find the one that was used
                for invite in current_invites:
                    if invite.uses is not None and (
                        invite.code not in guild_invites or invite.uses > guild_invites.get(invite.code, 0)
                    ):
                        inviter = invite.inviter
                        invite_code = invite.code
                        break  # Found the invite, stop searching.

                # Update the cache with the new uses
                self.invites[GuildId(member.guild.id)] = {
                    invite.code: invite.uses for invite in current_invites if invite.uses is not None
                }

            except discord.Forbidden:
                log.warning("Missing 'Manage Server' permissions for guild %d.", member.guild.id)
                self.bot.dispatch(
                    "security_alert",
                    guild_id=member.guild.id,
                    risk_level="MEDIUM",
                    details=(
                        "**Invite Permission Missing**\n"
                        "I am missing the `Manage Server` permission. "
                        "I cannot track who is inviting new members until this is granted."
                    ),
                    warning_type="invite_permission",
                )
            except discord.HTTPException:
                log.exception("HTTP error fetching invites.")
                self.bot.dispatch(
                    "security_alert",
                    guild_id=member.guild.id,
                    risk_level="HIGH",
                    details=(
                        f"**Invite API Error**\n"
                        f"An API error occurred while trying to find the inviter for {member.mention}. "
                        "This is likely a temporary Discord issue."
                    ),
                    warning_type="invite_api_fail",
                )

            # Determine the inviter's ID, defaulting to None if not found.
            inviter_id = UserId(inviter.id) if inviter else None

            # This cog's only job is to find the inviter and save it.
            # The JoinLeaveLogCog will handle all logging.
            await self.invites_db.insert_invite(
                UserId(member.id),
                inviter_id,
                GuildId(member.guild.id),
            )
        except Exception:
            log.exception("Critical error during invite tracking")
            # Ensure we don't crash before dispatching

        # Must ALWAYS happen, guaranteeing the join log appears
        self.bot.dispatch("invite_recorded", member, inviter_id, invite_code)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        """Handle new invite creation to keep the cache updated."""
        if invite.guild and invite.uses is not None:  # Ensure guild exists and uses is not None
            guild_id = GuildId(invite.guild.id)
            if guild_id not in self.invites:
                self.invites[guild_id] = {}
            self.invites[guild_id][invite.code] = invite.uses
            log.info("Cached new invite '%s' for guild '%s'.", invite.code, invite.guild.name)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        """Handle invite deletion to keep the cache updated."""
        if invite.guild and GuildId(invite.guild.id) in self.invites and invite.code in self.invites[GuildId(invite.guild.id)]:
            del self.invites[GuildId(invite.guild.id)][invite.code]
            log.info(
                "Removed deleted invite '%s' from cache for guild '%s'.",
                invite.code,
                invite.guild.name,
            )

    @invites.command(name="top", description="Shows the invite leaderboard.")
    @commands.cooldown(1, SECOND_COOLDOWN * 10, commands.BucketType.user)
    async def invites_top(self, interaction: discord.Interaction) -> None:
        """Display the top 10 inviters in an embed."""
        await interaction.response.defer()
        # This new method returns a sorted list of (user_id, invite_count) tuples
        leaderboard_data = await self.invites_db.get_invite_leaderboard(GuildId(interaction.guild.id))

        embed = discord.Embed(title="🏆 Top Invites Leaderboard", color=discord.Color.gold())

        if not leaderboard_data:
            embed.description = "No invites have been tracked yet."
            await interaction.followup.send(embed=embed)
            return

        leaderboard_text = []
        emojis = ["🥇", "🥈", "🥉"]
        for i, (user_id, invite_count) in enumerate(leaderboard_data):
            rank = emojis[i] if i < len(emojis) else f"**#{i + 1}**"
            user_display = f"<@{user_id}>" if user_id is not None else "Unknown Inviter"
            leaderboard_text.append(f"{rank} {user_display} — **{invite_count}** invites")

        embed.description = "\n".join(leaderboard_text)
        await interaction.followup.send(embed=embed)

    @invites.command(name="mylist", description="Shows who you have invited to the server.")
    @commands.cooldown(1, SECOND_COOLDOWN * 10, commands.BucketType.user)
    async def invites_mylist(self, interaction: discord.Interaction) -> None:
        """Show a list of members invited by the user."""
        await interaction.response.defer(ephemeral=True)
        all_invites = await self.invites_db.get_invites_by_inviter(GuildId(interaction.guild.id))
        user_invites = all_invites.get(UserId(interaction.user.id), [])

        embed = discord.Embed(title="Your Invited Members", color=discord.Color.purple())

        if not user_invites:
            embed.description = "You haven't invited anyone yet."
        else:
            names = " ".join(f"<@{i}>" for i in user_invites)
            embed.description = f"You have invited **{len(user_invites)}** people:\n\n{names}"

        await interaction.followup.send(embed=embed)

    @invites.command(
        name="sync",
        description="[Owner] Sync all current members against the invite database.",
    )
    @commands.cooldown(1, SECOND_COOLDOWN * 3600, commands.BucketType.user)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def invites_sync(self, interaction: discord.Interaction) -> None:
        """Fetch all guild members and syncs their data with the database.

        This corrects any misattributions from 'on_member_join' and
        backfills data for members who joined while the bot was offline.
        """
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("Starting member sync... this may take a while.")

        try:
            guild_id = GuildId(interaction.guild.id)
            all_members = await self.invites_db.get_all_guild_members_api(guild_id)
        except Exception:
            log.exception("Error during invites sync preparation.")
            await interaction.followup.send("An error occurred during preparation")
            return

        if not all_members:
            await interaction.followup.send("Could not fetch any members from the Discord API.")
            return

        member_data_list = []
        for member_data in all_members:
            inviter_id_str = member_data.get("inviter_id")
            # We still process members without an inviter_id to update their joined_at
            inviter_id: InviterId = UserId(int(inviter_id_str)) if inviter_id_str else None

            try:
                member_info = member_data["member"]
                invitee_id = UserId(int(member_info["user"]["id"]))
                joined_at_str = member_info.get("joined_at")
            except KeyError, ValueError:
                continue

            joined_at_db: str | None = None
            if joined_at_str:
                try:
                    dt_object = datetime.fromisoformat(joined_at_str)
                    joined_at_db = dt_object.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    log.warning("Could not parse joined_at timestamp: %s", joined_at_str)
                    joined_at_db = None  # Let the DB handle it

            member_data_list.append((invitee_id, inviter_id, guild_id, joined_at_db))

        rows_affected = await self.invites_db.bulk_sync_invites(member_data_list)
        await interaction.followup.send(f"Sync complete. {rows_affected} records were created or updated.")


async def setup(bot: KiwiBot) -> None:
    """Entry point for loading the cog."""
    # InvitesCog is now stateless and will fetch config per guild.
    await bot.add_cog(InvitesCog(bot=bot, invites_db=bot.invites_db))

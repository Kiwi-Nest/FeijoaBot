import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from modules.dtypes import GuildId, InviterId, RoleId, UserId
from modules.security_utils import check_bot_hierarchy, check_verifiable_role
from modules.utils import format_ordinal

if TYPE_CHECKING:
    from modules.ConfigDB import ConfigDB
    from modules.InvitesDB import InvitesDB
    from modules.KiwiBot import KiwiBot

log = logging.getLogger(__name__)


class JoinLeaveLogCog(commands.Cog):
    """A cog for logging member join and leave events to a specified channel."""

    def __init__(self, bot: KiwiBot, config_db: ConfigDB, invites_db: InvitesDB) -> None:
        self.bot = bot
        self.config_db = config_db
        self.invites_db = invites_db

    async def _log_event(
        self,
        member: discord.Member,
        title: str,
        color: discord.Colour,
        description_parts: list[str],
    ) -> None:
        config = await self.config_db.get_guild_config(GuildId(member.guild.id))
        log_channel_id = config.join_leave_log_channel_id

        if not log_channel_id:
            log.warning("Log channel not available, cannot send join/leave log.")
            return

        embed = discord.Embed(
            color=color,
            timestamp=discord.utils.utcnow(),
            description="\n".join(description_parts),
        )

        # Use member's display name and avatar for the author field
        embed.set_author(
            name=f"{member.name} ({member.display_name})",
            icon_url=member.display_avatar,
        )
        embed.set_thumbnail(url=member.display_avatar)
        embed.title = title

        log_channel = self.bot.get_channel(log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            log.warning(
                "Configured join/leave log channel %d not found or is not a text channel for guild %d.",
                log_channel_id,
                member.guild.id,
            )
            return

        try:
            # Defensively disable all pings. Only display mentions.
            await log_channel.send(embed=embed, allowed_mentions=None)
        except discord.Forbidden:
            log.warning("Permission denied sending join/leave log in guild %s", member.guild.id)
            self.bot.dispatch(
                "security_alert",
                guild_id=member.guild.id,
                risk_level="HIGH",
                details=(
                    f"**Join/Leave Log Permission Error**\n"
                    f"I cannot send messages to the configured **Join/Leave Log** channel ({log_channel.mention}).\n"
                    "Please check that I have `Send Messages` and `Embed Links` permissions."
                ),
                warning_type="join_leave_log_permission",
            )
        except discord.HTTPException:
            log.exception("Failed to send message to join/leave log channel")

    async def _autovalidate_member(
        self,
        verified_role_id: RoleId,
        member: discord.Member,
        invite: discord.Invite | None = None,
    ) -> discord.Role | None:
        """Automatically assign verified role id if user looks safe.

        Logic:
        1. BOTS are never auto-verified.
        2. If invite info is available, reject if the INVITER is a bot.
        3. If member has strong 'user_indicators' (nitro, avatar, etc), verify them.
           (This works even if the invite is None/Deleted).
        """
        verified_role: discord.Role | None = None

        if not verified_role_id:
            return verified_role

        # Explicitly exclude bots from verification
        if member.bot:
            return None

        # 2. User Account Indicators
        # Log properties that indicate a real, established user account
        user_indicators: list[str] = []
        if member.accent_colour:
            user_indicators.append(f"accent_colour={member.accent_colour}")
        if member.display_banner:
            user_indicators.append("has_display_banner=True")
        if member.avatar_decoration:
            user_indicators.append("has_avatar_decoration=True")
        if member.guild_avatar:
            user_indicators.append("has_guild_avatar=True")
        if member.premium_since:
            user_indicators.append(f"boosting_since={member.premium_since}")

        # PublicUserFlags
        public_flags = [flag_name for flag_name, has_flag in member.public_flags if has_flag and flag_name != "spammer"]
        if public_flags:
            user_indicators.append(f"public_flags={','.join(public_flags)}")

        # MemberFlags
        if member.flags.completed_onboarding:
            user_indicators.append("completed_onboarding=True")
        if member.flags.bypasses_verification:
            user_indicators.append("bypasses_verification=True")

        log.info(
            "Member join: %s (%d). Indicators: %s",
            str(member),
            member.id,
            "; ".join(user_indicators) if user_indicators else "None",
        )

        # 1. Invite Security Checks
        if invite and invite.inviter and not invite.inviter.bot and invite.uses < 5 and invite.created_at:
            age = discord.utils.utcnow() - invite.created_at
            if age.days < 3:
                user_indicators.append("safe_invite=True")

        if user_indicators:
            role = member.guild.get_role(verified_role_id)
            if role:
                # Check that the role only has permissions from the allowed list
                verified_result = check_verifiable_role(role)
                hierarchy_result = check_bot_hierarchy(member.guild, role)

                if not verified_result.ok:
                    self.bot.dispatch(
                        "security_alert",
                        guild_id=member.guild.id,
                        risk_level="HIGH",
                        details=(
                            f"**Auto-Verification Blocked**\n"
                            f"**Blocked** auto-verification for {member.mention}. "
                            f"The configured `verified_role_id` ({role.mention}) has disallowed permissions: "
                            f"{verified_result.reason}"
                        ),
                        warning_type="dangerous_role_assignment",
                    )
                elif not hierarchy_result.ok:
                    self.bot.dispatch(
                        "security_alert",
                        guild_id=member.guild.id,
                        risk_level="HIGH",
                        details=(
                            f"**Auto-Verification Failed**\n"
                            f"**Failed** auto-verification for {member.mention}. "
                            f"I cannot assign the `verified_role_id` ({role.mention}): {hierarchy_result.reason}"
                        ),
                        warning_type="role_hierarchy",
                    )
                else:
                    # All checks passed, assign the role
                    await member.add_roles(role, reason="Auto-verified on join")
                    verified_role = role  # Save for logging

        return verified_role

    async def _handle_join_logging(
        self,
        member: discord.Member,
        inviter_id: InviterId,
        verified_role: discord.Role | None,
    ) -> None:
        config = await self.config_db.get_guild_config(GuildId(member.guild.id))
        if not config.join_leave_log_channel_id:
            return

        # --- Determine Title and Color ---
        if member.flags.did_rejoin:
            title = "Member Rejoined"
            color = discord.Colour.blue()
        else:
            title = "Member Joined"
            color = discord.Colour.green()

        if member.bot:
            title += " [BOT]"

        # --- Build Description ---
        member_count = len(
            [m for m in member.guild.members if not m.bot and m.flags.completed_onboarding and len(m.roles) > 1],
        )

        description = [
            f"{member.mention} was the {format_ordinal(member_count)} member to join.",
            f"Account created: {discord.utils.format_dt(member.created_at, 'F')} \
({discord.utils.format_dt(member.created_at, 'R')})",
        ]

        if inviter_id:
            description.append(f"**Invited by:** <@{inviter_id}>")

        if verified_role:
            description.append(f"**Auto-verified with:** {verified_role.mention}")

        await self._log_event(member, title, color, description)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Handle logging when a new member joins or rejoins the server.

        Bots are logged immediately.
        Humans are logged via `on_invite_recorded` to ensure invite tracking is complete.
        """
        # Bots do not get auto-verified by this system, but we log them immediately.
        if member.bot:
            await self._handle_join_logging(member, inviter_id=None, verified_role=None)

    @commands.Cog.listener()
    async def on_invite_recorded(
        self,
        member: discord.Member,
        inviter_id: InviterId,
        invite_code: str | None,
    ) -> None:
        """Handle logging and verification for human members after invite processing is complete."""
        # Refresh member from cache to ensure roles are up-to-date
        member = member.guild.get_member(member.id) or member

        config = await self.config_db.get_guild_config(GuildId(member.guild.id))

        # Attempt to find the specific invite object to perform security checks
        invite: discord.Invite | None = None
        if not member.bot and invite_code:
            try:
                # Iterate active invites to find the object (for age/metadata checks)
                for inv in await member.guild.invites():
                    if inv.code == invite_code:
                        invite = inv
                        break
            except Exception:
                log.exception("Failed to resolve invite object for verification checks")

        # Perform deferred verification for humans
        verified_role: discord.Role | None = None
        if config.verified_role_id:
            # Check if role is already there
            role = member.guild.get_role(config.verified_role_id)
            if role and role in member.roles:
                verified_role = role
            # If not, try to autovalidate
            elif not member.bot:
                verified_role = await self._autovalidate_member(config.verified_role_id, member, invite)

        await self._handle_join_logging(member, inviter_id, verified_role)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Handle logging when a member leaves the server."""
        config = await self.config_db.get_guild_config(GuildId(member.guild.id))
        if not config.join_leave_log_channel_id:
            return  # This guild hasn't configured this feature, so we do nothing.

        title = "Member Left"
        color = discord.Colour.orange()

        if member.bot:
            title += " [BOT]"

        # Format the roles the member had
        roles = [r.mention for r in member.roles if r.id != member.guild.default_role.id]
        roles_str = " ".join(roles) if roles else "None"

        # Prepare description lines
        description = [f"{member.mention} has left the server."]
        if member.joined_at:
            description.append(
                f"**Joined:** {discord.utils.format_dt(member.joined_at, 'F')} \
({discord.utils.format_dt(member.joined_at, 'R')})",
            )

        description.append(f"**Roles:** {roles_str}")

        # --- Get Inviter from DB (New) ---
        if not member.bot:
            try:
                inviter_id = await self.invites_db.get_inviter_by_invitee(
                    UserId(member.id),
                    GuildId(member.guild.id),
                )
                if inviter_id:
                    description.append(f"**Invited by:** <@{inviter_id}>")
            except Exception:
                log.exception("Failed to get inviter from DB for leave log")

        await self._log_event(member, title, color, description)


async def setup(bot: KiwiBot) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(JoinLeaveLogCog(bot=bot, config_db=bot.config_db, invites_db=bot.invites_db))

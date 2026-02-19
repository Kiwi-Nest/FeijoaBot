import logging
import re
import time
from collections import defaultdict
from typing import TypedDict, cast, override

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import AnalysisStatus, MessageId, is_guild_message
from modules.KiwiBot import KiwiBot
from modules.security_utils import check_bot_hierarchy, check_verifiable_role

# A structured dictionary for analysis results, improving code clarity.


class AnalysisResult(TypedDict):
    """Represents the analysis of a single line in a reaction role message."""

    status: AnalysisStatus
    line_content: str
    emoji_str: str | None
    role: discord.Role | None
    error_message: str | None


# Regex to find custom emojis (<:name:id> or <a:name:id>) and a broad range of Unicode emojis.
# While not 100% exhaustive of all Unicode emojis, this covers the vast majority.
EMOJI_REGEX = re.compile(
    r"<a?:\w+:\d+>|"
    r"[\U0001F1E6-\U0001F1FF]|"  # flags (iOS)
    r"[\U0001F300-\U0001F5FF]|"  # symbols & pictographs
    r"[\U0001F600-\U0001F64F]|"  # emoticons
    r"[\U0001F680-\U0001F6FF]|"  # transport & map symbols
    r"[\U0001F700-\U0001F77F]|"  # alchemical symbols
    r"[\U0001F780-\U0001F7FF]|"  # Geometric Shapes Extended
    r"[\U0001F800-\U0001F8FF]|"  # Supplemental Arrows-C
    r"[\U0001F900-\U0001F9FF]|"  # Supplemental Symbols and Pictographs
    r"[\U0001FA00-\U0001FA6F]|"  # Chess Symbols
    r"[\U0001FA70-\U0001FAFF]|"  # Symbols and Pictographs Extended-A
    r"[\u2600-\u26FF]|"  # miscellaneous symbols
    r"[\u2700-\u27BF]|"  # dingbats
    r"[\u2B50]",  # star
)
ROLE_MENTION_REGEX = re.compile(r"<@&(\d+)>")

log = logging.getLogger(__name__)


class ReactionRoles(commands.Cog):
    """Reaction role system for reactions on admin-authored messages.

    Includes a crucial security check to ensure roles have no permissions.
    """

    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot
        # --- Message Caching ---
        self.analysis_cache: dict[MessageId, list[AnalysisResult]] = {}
        self.MAX_CACHE_SIZE = 128
        self.COOLDOWN_DURATION = 1.5  # seconds
        self.user_reaction_cooldowns: dict[tuple[int, int, str], float] = {}
        self.MAX_COOLDOWN_CACHE_SIZE = 1024

        self.debug_reaction_role_menu = app_commands.ContextMenu(
            name="Debug Reaction Role",
            callback=self.debug_reaction_role,
        )
        self.bot.tree.add_command(self.debug_reaction_role_menu)

    @override
    async def cog_unload(self) -> None:
        """Remove the context menu command when the cog is unloaded."""
        self.bot.tree.remove_command(
            self.debug_reaction_role_menu.name,
            type=self.debug_reaction_role_menu.type,
        )

    async def _analyze_reaction_message(
        self,
        message: discord.Message,
    ) -> list[AnalysisResult]:
        """Analyze a message to determine its validity as a reaction role message.

        Caches results to avoid re-computing for the same message.
        This is the single source of truth for all reaction role logic.

        Returns
        -------
            A list of AnalysisResult dictionaries, one for each parsed line.
            Returns an empty list if the message author is not an administrator.

        """
        message_id = MessageId(message.id)
        # 1. Check Cache
        if message_id in self.analysis_cache:
            return self.analysis_cache[message_id]

        # The rest of the function performs the analysis if not found in cache.
        # This is the single source of truth for all reaction role logic.

        # 2. Perform Analysis
        results: list[AnalysisResult] = []
        if not is_guild_message(message):
            return []

        # 1. Author Validation: Must be an manage_roles.
        if not message.author.guild_permissions.manage_roles:  # ty now knows message.author is a Member
            return []

        # 3. Line-by-Line Analysis
        for line in message.content.splitlines():
            clean_line = line.strip()
            if not clean_line:
                continue

            role_mentions = ROLE_MENTION_REGEX.findall(clean_line)
            emojis_found = EMOJI_REGEX.findall(clean_line)

            if len(emojis_found) != 1 or len(role_mentions) != 1:
                results.append(
                    {
                        "status": "WARN",
                        "line_content": clean_line,
                        "emoji_str": emojis_found[0] if emojis_found else None,
                        "role": None,
                        "error_message": "Line must contain exactly one emoji and one role mention.",
                    },
                )
                continue

            emoji_str = emojis_found[0]
            role = message.guild.get_role(int(role_mentions[0]))

            if not role:
                results.append(
                    {
                        "status": "WARN",
                        "line_content": clean_line,
                        "emoji_str": emoji_str,
                        "role": None,
                        "error_message": f"Role with ID {role_mentions[0]} not found.",
                    },
                )
                continue

            # 4. Security Check: Use centralized boolean function
            safe_result = check_verifiable_role(role)
            if not safe_result.ok:
                results.append(
                    {
                        "status": "ERROR",
                        "line_content": clean_line,
                        "emoji_str": emoji_str,
                        "role": role,
                        "error_message": safe_result.reason,  # Use the message from our util
                    },
                )
                continue

            # 5. Bot Hierarchy Check: Use centralized boolean function
            hierarchy_result = check_bot_hierarchy(message.guild, role)
            if not hierarchy_result.ok:
                results.append(
                    {
                        "status": "ERROR",
                        "line_content": clean_line,
                        "emoji_str": emoji_str,
                        "role": role,
                        "error_message": hierarchy_result.reason,  # Use the message from our util
                    },
                )
                continue

            # If all checks pass
            results.append(
                {
                    "status": "OK",
                    "line_content": clean_line,
                    "emoji_str": emoji_str,
                    "role": role,
                    "error_message": None,
                },
            )

        # 6. Manage Cache Size and Store Result
        if len(self.analysis_cache) >= self.MAX_CACHE_SIZE:
            # Remove the oldest item (FIFO)
            del self.analysis_cache[next(iter(self.analysis_cache))]

        self.analysis_cache[message_id] = results
        return results

    @commands.Cog.listener()
    @override
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Invalidate the cache if a potential reaction role message is edited."""
        message_id = MessageId(payload.message_id)
        if message_id in self.analysis_cache:
            del self.analysis_cache[message_id]
            log.info(
                "Invalidated reaction role cache for edited message ID %s.",
                payload.message_id,
            )

    @commands.Cog.listener()
    @override
    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Listen for a reaction being added to any message."""
        await self._handle_reaction_event(payload)

    @commands.Cog.listener()
    @override
    async def on_raw_reaction_remove(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Listen for a reaction being removed from any message."""
        await self._handle_reaction_event(payload)

    def _is_user_on_cooldown(
        self,
        user_id: int,
        message_id: int,
        emoji_str: str,
    ) -> bool:
        """Check and update the per-user, per-reaction cooldown."""
        key = (message_id, user_id, emoji_str)
        current_time = time.time()

        if last_event_time := self.user_reaction_cooldowns.get(key):
            time_since = current_time - last_event_time
            if time_since < self.COOLDOWN_DURATION:
                log.debug(
                    "User %d on cooldown for reaction %s on message %d.",
                    user_id,
                    emoji_str,
                    message_id,
                )
                return True  # User is on cooldown

        # Manage cache size
        if len(self.user_reaction_cooldowns) >= self.MAX_COOLDOWN_CACHE_SIZE:
            del self.user_reaction_cooldowns[next(iter(self.user_reaction_cooldowns))]

        # Update timestamp
        self.user_reaction_cooldowns[key] = current_time
        return False  # User is not on cooldown

    async def _fetch_reaction_context(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> tuple[discord.Guild, discord.Member, discord.Message] | None:
        """Fetch Guild, Member, and Message objects required for reaction roles."""
        # Guild is guaranteed to be present from the initial guard clause
        if not payload.guild_id:
            return None

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return None

        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            log.debug("Member %d not found in guild %d.", payload.user_id, guild.id)
            return None

        try:
            channel = await self.bot.fetch_channel(payload.channel_id)
            message = await cast("discord.TextChannel", channel).fetch_message(
                payload.message_id,
            )
        except discord.NotFound, discord.Forbidden:
            log.debug(
                "Could not fetch message %d from channel %d.",
                payload.message_id,
                payload.channel_id,
            )
            return None

        return guild, member, message

    async def _validate_role_for_assignment(
        self,
        guild: discord.Guild,
        member: discord.Member,
        role: discord.Role,
    ) -> bool:
        """Perform security checks before assigning a role."""
        # 1. Stale Cache Security Check (Permissions)
        safe_result = check_verifiable_role(role)
        if not safe_result.ok:
            log.warning(
                "Role '%s' failed stale cache security check (permissions no longer safe). User: '%s', Guild: '%s'",
                role.name,
                member.display_name,
                guild.name,
            )
            self.bot.dispatch(
                "security_alert",
                guild_id=guild.id,
                risk_level="HIGH",
                details=(
                    f"**Reaction Role Blocked**\n"
                    f"I blocked the assignment of {role.mention} to {member.mention} because the role is no longer safe.\n"
                    f"**Reason:** {safe_result.reason}"
                ),
                warning_type="reaction_role_unsafe",
            )
            return False

        # 2. Stale Cache Security Check (Hierarchy)
        hierarchy_result = check_bot_hierarchy(guild, role)
        if not hierarchy_result.ok:
            log.warning(
                "Role '%s' failed stale cache security check (hierarchy no longer sufficient). User: '%s', Guild: '%s'",
                role.name,
                member.display_name,
                guild.name,
            )
            self.bot.dispatch(
                "security_alert",
                guild_id=guild.id,
                risk_level="HIGH",
                details=(
                    f"**Reaction Role Hierarchy Issue**\n"
                    f"I could not assign/remove the reaction role {role.mention} "
                    f"for {member.mention} because my bot role is no longer higher than it. "
                    "Please move my role up in the server settings."
                ),
                warning_type="reaction_role_hierarchy",
            )
            return False

        return True

    async def _apply_reaction_role_change(
        self,
        member: discord.Member,
        role: discord.Role,
        event_type: str,
        message_id: int,
    ) -> None:
        """Add or remove the target role from the member."""
        try:
            reason = f"Reaction Role {message_id}"
            if event_type == "REACTION_ADD":
                await member.add_roles(role, reason=reason)
                log.info(
                    "Added role '%s' to '%s' in guild '%s'",
                    role.name,
                    member.display_name,
                    member.guild.name,
                )
            elif event_type == "REACTION_REMOVE":
                await member.remove_roles(role, reason=reason)
                log.info(
                    "Removed role '%s' from '%s' in guild '%s'",
                    role.name,
                    member.display_name,
                    member.guild.name,
                )
        except discord.Forbidden:
            log.warning(
                "Failed to modify role '%s' for '%s'. Check permissions and role hierarchy.",
                role.name,
                member.display_name,
            )
            self.bot.dispatch(
                "security_alert",
                guild_id=member.guild.id,
                risk_level="HIGH",
                details=(
                    f"**Reaction Role Permission Error**\n"
                    f"I failed to assign/remove the reaction role {role.mention} "
                    f"for {member.mention}.\n\n"
                    "**Reason**: `discord.Forbidden`. This is a role hierarchy "
                    f"problem. Please ensure my bot role is higher than the `{role.name}` role."
                ),
                warning_type="reaction_role_permission",
            )
        except discord.HTTPException:
            log.exception(
                "Network error while modifying role for '%s'",
                member.display_name,
            )

    async def _process_reaction_analysis(
        self,
        analysis_results: list[AnalysisResult],
        guild: discord.Guild,
        member: discord.Member,
        emoji_str: str,
        event_type: str,
        message_id: int,
    ) -> None:
        """Iterate analysis results and apply the correct role change."""
        for result in analysis_results:
            if result["status"] == "OK" and result["emoji_str"] == emoji_str:
                target_role = cast("discord.Role", result["role"])

                # Run security validation
                if not await self._validate_role_for_assignment(
                    guild,
                    member,
                    target_role,
                ):
                    continue  # Role is not safe, skip

                # All checks passed, apply the role change
                await self._apply_reaction_role_change(
                    member,
                    target_role,
                    event_type,
                    message_id,
                )
                # Found our match, no need to check other lines.
                break

    async def _handle_reaction_event(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Shared logic for processing both reaction add and remove events."""
        # 1. Initial Guard Clauses
        if not self.bot.user or not payload.guild_id or payload.user_id == self.bot.user.id:
            return

        user_id = payload.user_id
        message_id = payload.message_id
        emoji_str = str(payload.emoji)

        # 2. Cooldown Check
        if self._is_user_on_cooldown(user_id, message_id, emoji_str):
            return

        # 3. Fetch Discord Objects
        context = await self._fetch_reaction_context(payload)
        if not context:
            return
        guild, member, message = context

        # 4. Quick check: Is the emoji even in the message?
        if emoji_str not in message.content:
            return

        # 5. Get (or compute) message analysis
        analysis_results = await self._analyze_reaction_message(message)
        if not analysis_results:
            return

        # 6. Process the results and apply role changes
        await self._process_reaction_analysis(
            analysis_results,
            guild,
            member,
            emoji_str,
            payload.event_type,
            payload.message_id,
        )

    @staticmethod
    def _format_analysis_report(
        analysis: list[AnalysisResult],
    ) -> list[str]:
        """Format the analysis results into a list of strings for a report."""
        if not analysis:
            return [
                "⚠️ This message is **not a valid reaction role message**.\n"
                "(Reason: The message author does not have `Manage Roles` permissions).",
            ]

        report_lines: list[str] = []
        aggregated_results: defaultdict[str, list[str]] = defaultdict(list)

        for result in analysis:
            line = result["line_content"]
            status_map = {
                "OK": "✅ **VALID**",
                "ERROR": f"❌ **ERROR**: {result['error_message']}",
                "WARN": f"⚠️ **WARN**: {result['error_message']}",
            }
            aggregated_results[status_map[result["status"]]].append(line)

        for header, lines in aggregated_results.items():
            report_lines.extend([f"\n{header}", "```", *lines, "```"])

        return report_lines

    @app_commands.default_permissions(manage_roles=True)
    async def debug_reaction_role(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        """Analyze a message for reaction role validity and DM an aggregated report."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        report_lines = [
            f"**🔎 Debug Report for Message ID:** `{message.id}` in {message.channel.mention}\n",
        ]

        if not interaction.guild or not isinstance(
            interaction.guild.me,
            discord.Member,
        ):
            await interaction.followup.send(
                "Error: This command can only be used in a server.",
            )
            return

        if not interaction.guild.me.guild_permissions.manage_roles:
            report_lines.append(
                "❌ **CRITICAL: I do not have the `Manage Roles` permission! I cannot assign or remove any roles.**\n",
            )

        analysis = await self._analyze_reaction_message(message)

        report_lines.extend(self._format_analysis_report(analysis))
        report = "\n".join(report_lines)
        log.info(report)

        try:
            CHAR_LIMIT = 2000
            if len(report) <= CHAR_LIMIT:
                await interaction.user.send(report)
            else:
                report_chunks = [report[i : i + CHAR_LIMIT] for i in range(0, len(report), CHAR_LIMIT)]
                for chunk in report_chunks:
                    await interaction.user.send(chunk)

            # Confirm to the admin that the DM was sent
            await interaction.followup.send(
                "I have sent the debug report to your DMs.",
                ephemeral=True,
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't send you a DM. Please check your privacy settings. Here is the report:\n\n" + report,
                ephemeral=True,
            )


async def setup(bot: KiwiBot) -> None:
    """Add the ReactionRoles cog to the bot."""
    await bot.add_cog(ReactionRoles(bot))
    log.info("ReactionRoles cog loaded.")

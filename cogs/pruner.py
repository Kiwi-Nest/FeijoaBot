import logging
from typing import TYPE_CHECKING

from discord import Forbidden, HTTPException
from discord.ext import commands, tasks

from modules.dtypes import GuildId
from modules.security_utils import check_bot_hierarchy, check_verifiable_role

if TYPE_CHECKING:
    from modules.ConfigDB import GuildConfig
    from modules.KiwiBot import KiwiBot
    from modules.UserDB import UserDB

# Use Python's logging module for better diagnostics in a reusable component
log = logging.getLogger(__name__)


class PrunerCog(commands.Cog):
    def __init__(
        self,
        bot: KiwiBot,  # Removed guild_id, role_ids_to_prune, inactivity_days from init
        user_db: UserDB,  # UserDB is still needed
    ) -> None:
        self.bot = bot
        self.user_db = user_db

    # This is a special event that runs when the cog is loaded
    async def cog_load(self) -> None:
        self.prune_loop.start()

    async def cog_unload(self) -> None:
        """Clean up when the cog is unloaded."""
        self.prune_loop.cancel()

    @tasks.loop(hours=1)
    async def prune_loop(self) -> None:  # noqa: PLR0912, PLR0915
        """Check for and prune inactive members."""
        # Add this check
        leveling_cog = self.bot.get_cog("LevelingCog")
        if not leveling_cog:
            log.error(
                "Prune loop cannot run because the LevelingCog is not loaded. "
                "This is a safety measure to prevent accidental role pruning of all members.",
            )
            return

        log.info(
            "Running automatic prune check for inactive members across all guilds...",
        )

        for guild in self.bot.guilds:
            config: GuildConfig = await self.bot.config_db.get_guild_config(
                GuildId(guild.id),
            )
            roles_to_prune = config.roles_to_prune
            inactivity_days = config.inactivity_days

            if not roles_to_prune:
                log.debug(
                    "Skipping prune for guild '%s': No roles configured for pruning.",
                    guild.name,
                )
                continue
            if not inactivity_days or inactivity_days <= 0:
                log.debug(
                    "Skipping prune for guild '%s': Inactivity days not configured or invalid.",
                    guild.name,
                )
                continue

            # Fetch roles from the guild that are configured for pruning
            prunable_roles = {guild.get_role(role_id) for role_id in roles_to_prune}
            prunable_roles.discard(None)  # Remove None if a role ID wasn't found
            if not prunable_roles:
                log.warning(
                    "Pruning skipped for guild '%s': Configured roles not found in guild.",
                    guild.name,
                )
                continue

            # Get inactive user IDs from the database
            inactive_user_ids = set(
                await self.user_db.get_inactive_users(
                    GuildId(guild.id),
                    inactivity_days,
                ),
            )
            if not inactive_user_ids:
                log.debug(
                    "No inactive users found in the database to prune for guild '%s'.",
                    guild.name,
                )
                continue

            total_members_pruned = 0
            # Iterate through cached members (safe approximation)
            for member in guild.members:
                if member.id not in inactive_user_ids:
                    continue

                # Find which of the prunable roles the member actually has
                roles_to_remove = [role for role in member.roles if role in prunable_roles]

                # Also add any roles that start with matching prefix
                gradient_roles = [
                    role
                    for role in member.roles
                    if role.name.startswith("Colour: ") or role.name.startswith("Ping: ") or role.name.startswith("Gradient: ")
                ]
                roles_to_remove.extend(gradient_roles)

                # Remove duplicates in case a gradient role was already in prunable_roles
                roles_to_remove = [r for r in set(roles_to_remove) if not r.managed]

                safe_roles_to_remove = []
                for role in roles_to_remove:
                    safe_result = check_verifiable_role(role)
                    hierarchy_result = check_bot_hierarchy(guild, role)

                    if not safe_result.ok:
                        self.bot.dispatch(
                            "security_alert",
                            guild_id=guild.id,
                            risk_level="MEDIUM",
                            details=(
                                f"**Role Pruning Skipped**\n"
                                f"I skipped pruning role {role.mention} from {member.mention}. Reason: {safe_result.reason}"
                            ),
                            warning_type="prune_security",
                        )
                        continue  # Skip this unsafe role

                    if hierarchy_result.ok:
                        safe_roles_to_remove.append(role)
                    else:
                        # Log a warning for the specific role that failed
                        self.bot.dispatch(
                            "security_alert",
                            guild_id=guild.id,
                            risk_level="HIGH",
                            details=(
                                f"**Role Pruning Failed**\n"
                                f"I failed to prune role {role.mention} from {member.mention}. Reason: {hierarchy_result.reason}"
                            ),
                            warning_type="prune_permission",
                        )

                if safe_roles_to_remove:  # Use the filtered list
                    try:
                        await member.remove_roles(
                            *safe_roles_to_remove,  # Only attempt to remove roles we can manage
                            reason=f"Pruned for {inactivity_days}+ days of inactivity.",
                        )
                        role_names = ", ".join(f"'{r.name}'" for r in safe_roles_to_remove)
                        log.info("Pruned %s from %s.", role_names, member.display_name)
                        total_members_pruned += 1
                    except Forbidden:
                        # This "shouldn't" be hit for hierarchy anymore, but good as a fallback
                        log.exception(
                            "Failed to prune %s: Missing Permissions.",
                            member.display_name,
                        )
                        self.bot.dispatch(
                            "security_alert",
                            guild_id=guild.id,
                            risk_level="HIGH",
                            details=(
                                f"**Role Pruning Permission Error**\n"
                                f"I failed to prune roles from {member.mention}.\n\n"
                                "**Reason**: `discord.Forbidden`. This is a role hierarchy problem. "
                                "Please ensure my bot role is higher than the roles I am trying to remove."
                            ),
                            warning_type="prune_permission",
                        )
                    except HTTPException:
                        log.exception("Failed to prune %s", member.display_name)

            log.info(
                "Pruning complete for guild '%s'. Roles removed from %s member(s).",
                guild.name,
                total_members_pruned,
            )


# The setup function that discord.py calls when loading the extension
async def setup(bot: KiwiBot) -> None:
    # The cog needs the UserDB instance from the bot
    user_db = getattr(bot, "user_db", None)
    if not user_db:
        msg = "Bot is missing the 'user_db' attribute."
        raise RuntimeError(msg)

    # PrunerCog is now stateless and will fetch config per guild.
    # It still needs user_db passed in.
    await bot.add_cog(PrunerCog(bot=bot, user_db=user_db))

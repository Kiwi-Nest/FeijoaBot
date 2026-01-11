import datetime
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from modules.dtypes import GuildId
from modules.security_utils import check_bot_hierarchy, check_verifiable_role

if TYPE_CHECKING:
    from modules.ConfigDB import ConfigDB, GuildConfig
    from modules.KiwiBot import KiwiBot

# Set up logging for this cog
log = logging.getLogger(__name__)


class RolePrunerCog(commands.Cog):
    """A cog that automatically prunes old roles with a specific prefix."""

    def __init__(self, bot: KiwiBot, *, config_db: ConfigDB) -> None:
        self.bot = bot
        self.config_db = config_db
        # Start the pruning loop as soon as the cog is loaded
        self.prune_roles_loop.start()

    async def cog_unload(self) -> None:
        """Clean up when the cog is unloaded."""
        self.prune_roles_loop.cancel()

    @tasks.loop(hours=1)  # Hourly
    async def prune_roles_loop(self) -> None:
        """Iterate through all guilds and prunes roles that match the criteria."""
        log.info("Starting daily check for old custom roles to prune.")

        # Calculate the cutoff date for roles to be considered old

        # Iterate over all the guilds the bot is in
        for guild in self.bot.guilds:
            log.info("Checking roles in guild: %s", guild.name)

            config: GuildConfig = await self.config_db.get_guild_config(GuildId(guild.id))
            custom_role_prefix = config.custom_role_prefix
            custom_role_prune_days = config.custom_role_prune_days

            if not custom_role_prefix or not custom_role_prune_days or custom_role_prune_days <= 0:
                log.debug(
                    "Skipping custom role prune for guild '%s': Feature not configured or invalid.",
                    guild.name,
                )
                continue

            cutoff_date = discord.utils.utcnow() - datetime.timedelta(days=custom_role_prune_days)

            # Find all roles that meet the pruning criteria
            roles_to_prune = [
                role
                for role in guild.roles
                if not role.managed and role.name.startswith(custom_role_prefix) and role.created_at < cutoff_date
            ]

            if not roles_to_prune:
                log.info("No roles to prune in %s.", guild.name)
                continue

            log.info("Found %d roles to prune in %s.", len(roles_to_prune), guild.name)

            # Prune the identified roles
            for role in roles_to_prune:
                safe_result = check_verifiable_role(role)
                hierarchy_result = check_bot_hierarchy(guild, role)

                if not safe_result.ok:
                    log.warning(
                        "Skipping deletion of role '%s' in %s: %s",
                        role.name,
                        guild.name,
                        safe_result.reason,
                    )
                    continue

                if not hierarchy_result.ok:
                    log.warning(
                        "Skipping deletion of role '%s' in %s: %s",
                        role.name,
                        guild.name,
                        hierarchy_result.reason,
                    )
                    continue

                try:
                    await role.delete(reason=f"Pruning old role created more than {custom_role_prune_days} days ago.")
                    log.info("Successfully pruned role '%s' from %s.", role.name, guild.name)
                except discord.Forbidden:
                    log.warning(
                        "Failed to prune role '%s' in %s: Missing Permissions.",
                        role.name,
                        guild.name,
                    )
                    self.bot.dispatch(
                        "security_alert",
                        guild_id=guild.id,
                        risk_level="HIGH",
                        details=(
                            f"**Role Prune Hierarchy Failure**\n"
                            f"I failed to prune the old role **{role.name}**.\n"
                            "This is likely a **hierarchy issue**. Ensure my role is higher than the roles I need to manage."
                        ),
                        warning_type="prune_hierarchy_fail",
                    )
                except discord.HTTPException:
                    log.exception(
                        "Failed to prune role '%s' in %s due to an API error.",
                        role.name,
                        guild.name,
                    )

        log.info("Finished daily role pruning check.")


async def setup(bot: KiwiBot) -> None:
    """Add the cog to the bot."""
    # RolePrunerCog is now stateless and will fetch config per guild.
    await bot.add_cog(RolePrunerCog(bot, config_db=bot.config_db))

import datetime
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from modules.KiwiBot import KiwiBot
from modules.security_utils import is_bot_hierarchy_sufficient, is_role_safe

if TYPE_CHECKING:
    from modules.ConfigDB import GuildConfig

# Set up logging for this cog
log = logging.getLogger(__name__)


class RolePrunerCog(commands.Cog):
    """A cog that automatically prunes old roles with a specific prefix."""

    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot
        # Start the pruning loop as soon as the cog is loaded
        self.prune_roles_loop.start()

    def cog_unload(self) -> None:
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

            config: GuildConfig = await self.bot.config_db.get_guild_config(guild.id)
            custom_role_prefix = config.custom_role_prefix
            custom_role_prune_days = config.custom_role_prune_days

            if not custom_role_prefix or not custom_role_prune_days or custom_role_prune_days <= 0:
                log.debug("Skipping custom role prune for guild '%s': Feature not configured or invalid.", guild.name)
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
                safe_result = is_role_safe(role, require_no_permissions=True)  # Checks for 0 perms
                hierarchy_result = is_bot_hierarchy_sufficient(guild, role)

                if not safe_result.ok:
                    log.warning("Skipping deletion of role '%s' in %s: %s", role.name, guild.name, safe_result.reason)
                    continue

                if not hierarchy_result.ok:
                    log.warning("Skipping deletion of role '%s' in %s: %s", role.name, guild.name, hierarchy_result.reason)
                    continue

                try:
                    await role.delete(reason=f"Pruning old role created more than {custom_role_prune_days} days ago.")
                    log.info("Successfully pruned role '%s' from %s.", role.name, guild.name)
                except discord.Forbidden:
                    log.exception(
                        "Failed to prune role '%s' in %s: Missing Permissions.",
                        role.name,
                        guild.name,
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
    await bot.add_cog(RolePrunerCog(bot))

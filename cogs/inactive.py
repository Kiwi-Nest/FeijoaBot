import logging
from typing import TYPE_CHECKING

from discord import Forbidden, HTTPException
from discord.ext import commands, tasks

from modules.dtypes import GuildId
from modules.security_utils import check_bot_hierarchy, check_role_safety

if TYPE_CHECKING:
    import discord

    from modules.BotCore import BotCore
    from modules.ConfigDB import GuildConfig
    from modules.UserDB import UserDB

log = logging.getLogger(__name__)


class InactiveCog(commands.Cog):
    def __init__(self, bot: BotCore, user_db: UserDB) -> None:
        self.bot = bot
        self.user_db = user_db

    async def cog_load(self) -> None:
        self.inactive_loop.start()

    async def cog_unload(self) -> None:
        self.inactive_loop.cancel()

    def _check_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        """Run safety and hierarchy checks, dispatching alerts on failure. Returns True if safe."""
        safety = check_role_safety(role)
        if not safety.ok:
            self.bot.dispatch(
                "security_alert",
                guild_id=guild.id,
                risk_level="MEDIUM",
                details=f"**Inactive Role Skipped**\nI skipped managing the inactive role {role.mention}. Reason: {safety.reason}",
                warning_type="inactive_security",
            )
            return False

        hierarchy = check_bot_hierarchy(guild, role)
        if not hierarchy.ok:
            self.bot.dispatch(
                "security_alert",
                guild_id=guild.id,
                risk_level="HIGH",
                details=f"**Inactive Role Failed**\nI cannot manage the inactive role {role.mention}. Reason: {hierarchy.reason}",
                warning_type="inactive_permission",
            )
            return False

        return True

    async def _reconcile_guild(self, guild: discord.Guild) -> None:
        config: GuildConfig = await self.bot.config_db.get_guild_config(GuildId(guild.id))
        if not config.inactive_role_id:
            return

        role = guild.get_role(config.inactive_role_id)
        if not role:
            log.warning("Inactive role ID %s not found in guild '%s'. Skipping.", config.inactive_role_id, guild.name)
            return

        if not self._check_role(guild, role):
            return

        inactive_ids = set(await self.user_db.get_inactive_users(GuildId(guild.id), config.inactive_role_threshold_days))
        current_in_role = {m.id for m in role.members if not m.bot}

        added = removed = 0

        for user_id in inactive_ids - current_in_role:
            member = guild.get_member(user_id)
            if not member or member.bot:
                continue
            try:
                await member.add_roles(role, reason=f"Inactive for {config.inactive_role_threshold_days}+ days.")
                added += 1
            except Forbidden, HTTPException:
                log.exception("Failed to add inactive role to %s.", member.display_name)

        for user_id in current_in_role - inactive_ids:
            member = guild.get_member(user_id)
            if not member:
                continue
            try:
                await member.remove_roles(role, reason="User is no longer inactive.")
                removed += 1
            except Forbidden, HTTPException:
                log.exception("Failed to remove inactive role from %s.", member.display_name)

        log.info("Inactive reconciliation complete for guild '%s': +%s added, -%s removed.", guild.name, added, removed)

    @tasks.loop(hours=1)
    async def inactive_loop(self) -> None:
        """Reconcile Inactive role membership across all guilds."""
        log.info("Running inactive role reconciliation across all guilds...")
        for guild in self.bot.guilds:
            await self._reconcile_guild(guild)

    @inactive_loop.before_loop
    async def before_inactive_loop(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_user_activity(self, member: discord.Member) -> None:
        """Remove the Inactive role when a user becomes active."""
        config: GuildConfig = await self.bot.config_db.get_guild_config(GuildId(member.guild.id))
        if not config.inactive_role_id:
            return

        role = member.guild.get_role(config.inactive_role_id)
        if not role or role not in member.roles:
            return

        safety = check_role_safety(role)
        if not safety.ok:
            log.warning("Inactive role safety check failed on activity: %s", safety.reason)
            return

        hierarchy = check_bot_hierarchy(member.guild, role)
        if not hierarchy.ok:
            log.warning("Bot hierarchy check failed for inactive role on activity: %s", hierarchy.reason)
            return

        try:
            await member.remove_roles(role, reason="User became active.")
        except Forbidden, HTTPException:
            log.exception(
                "Failed to remove inactive role from %s on activity.",
                member.display_name,
            )


async def setup(bot: BotCore) -> None:
    user_db = getattr(bot, "user_db", None)
    if not user_db:
        msg = "Bot is missing the 'user_db' attribute."
        raise RuntimeError(msg)

    await bot.add_cog(InactiveCog(bot=bot, user_db=user_db))

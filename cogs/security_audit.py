from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from modules import audit_utils, security_utils

if TYPE_CHECKING:
    from modules.KiwiBot import KiwiBot

log = logging.getLogger(__name__)

type AuditResult = list[str]


@commands.guild_only()
# Set default permissions for the entire command group
@app_commands.default_permissions(manage_guild=True, manage_roles=True)
# Add a cog-wide cooldown: 9 actions per minute, per user, per guild.
@app_commands.checks.cooldown(9, 60.0, key=lambda i: (i.guild_id, i.user.id))
class SecurityAudit(
    commands.GroupCog,
    group_name="audit",
    group_description="Commands for auditing server security.",
):
    """Audit server security settings, roles, and permissions.

    For security, all responses should be ephemeral.
    """

    def __init__(self, bot: KiwiBot) -> None:
        """Initialize the SecurityAudit cog."""
        self.bot = bot
        super().__init__()

    async def _send_audit_embed(
        self,
        ctx: commands.Context,
        title: str,
        results: AuditResult,
        color: discord.Colour,
        conclusion: str = "Audit complete.",
    ) -> None:
        """Send a standardized, paginated embed for audit results."""
        if not results:
            embed = discord.Embed(
                title=title,
                description="✅ No issues found.",
                color=discord.Colour.green(),
            )
            await ctx.send(embed=embed, ephemeral=True)
            return

        # Create the first embed
        embed = discord.Embed(title=title, color=color)
        first_page = True

        full_description = "\n".join(results)
        for char_chunk_list in discord.utils.as_chunks(full_description, 4000):
            chunk = "".join(char_chunk_list)  # 4096 limit
            if first_page:
                embed.description = chunk
                await ctx.send(embed=embed, ephemeral=True)
                first_page = False
            else:
                # Send subsequent pages as new, ephemeral embeds
                followup_embed = discord.Embed(
                    title=f"{title} (continued)",
                    description=chunk,
                    color=color,
                )
                await ctx.send(embed=followup_embed, ephemeral=True)

        # Send a final conclusion message
        await ctx.send(conclusion, ephemeral=True)

    @app_commands.command(
        name="validate_config",
        description="Check that all configured role/channel IDs are valid.",
    )
    async def validate_config(self, interaction: discord.Interaction) -> None:
        """Check configured IDs against the guild."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return  # Should be caught by cog_check, but for type safety

        config = await self.bot.config_db.get_guild_config(ctx.guild.id)
        results = audit_utils.validate_config(ctx.guild, config)
        await self._send_audit_embed(
            ctx,
            "Configuration Validation",
            results,
            color=discord.Colour.orange() if results else discord.Colour.green(),
        )

    @app_commands.command(
        name="dangerous_roles",
        description="List all roles with high-risk permissions.",
    )
    async def dangerous_roles(self, interaction: discord.Interaction) -> None:
        """List roles with dangerous permissions."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        config = await self.bot.config_db.get_guild_config(ctx.guild.id)
        results = audit_utils.check_dangerous_roles(ctx.guild, config)
        await self._send_audit_embed(
            ctx,
            "Dangerous Roles Report",
            results,
            color=discord.Colour.red(),
        )

    @app_commands.command(
        name="risky_bot_permissions",
        description="List all bots and their dangerous permissions.",
    )
    async def risky_bot_permissions(self, interaction: discord.Interaction) -> None:
        """List bots with dangerous permissions."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        results = audit_utils.check_bot_permissions(ctx.guild)
        await self._send_audit_embed(
            ctx,
            "Bot Permissions Report",
            results,
            color=discord.Colour.teal(),
        )

    @app_commands.command(
        name="risky_overwrites",
        description="Scan for channel overwrites that could cause issues (e.g., mute bypass).",
    )
    async def risky_overwrites(self, interaction: discord.Interaction) -> None:
        """Scan for problematic channel overwrites."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        config = await self.bot.config_db.get_guild_config(ctx.guild.id)
        results = audit_utils.check_risky_overwrites(ctx.guild, config)
        await self._send_audit_embed(
            ctx,
            "Risky Channel Overwrites",
            results,
            color=discord.Colour.gold(),
        )

    @app_commands.command(
        name="desynced_channels",
        description="List all channels not synced with their category permissions.",
    )
    async def desynced_channels(self, interaction: discord.Interaction) -> None:
        """List desynchronized channels."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        results = audit_utils.check_desynced_channels(ctx.guild)
        await self._send_audit_embed(
            ctx,
            "Desynchronized Channels",
            results,
            color=discord.Colour.light_grey(),
        )

    @app_commands.command(
        name="hidden_channels",
        description="List all channels hidden from @everyone.",
    )
    async def hidden_channels(self, interaction: discord.Interaction) -> None:
        """List channels hidden from @everyone."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        results = audit_utils.check_hidden_channels(ctx.guild)
        await self._send_audit_embed(
            ctx,
            "Hidden Channels",
            results,
            color=discord.Colour.dark_grey(),
        )

    @app_commands.command(
        name="who_has",
        description="List all members who have a specific permission.",
    )
    @app_commands.describe(permission="The permission to check for.")
    @app_commands.choices(
        permission=[app_commands.Choice(name=desc, value=perm) for perm, desc in security_utils.DANGEROUS_PERMISSIONS.items()],
    )
    async def who_has(
        self,
        interaction: discord.Interaction,
        permission: app_commands.Choice[str],
    ) -> None:
        """Check who has a specific permission."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        results = audit_utils.check_who_has_permission(ctx.guild, permission.value)
        await self._send_audit_embed(
            ctx,
            f"Members with `{permission.name}`",
            results,
            color=discord.Colour.purple(),
        )

    @app_commands.command(
        name="list_roles",
        description="List all roles, sorted by permission status.",
    )
    async def list_roles(self, interaction: discord.Interaction) -> None:
        """List roles sorted by permission status."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        with_perms, no_perms = audit_utils.get_role_lists(ctx.guild)

        embed = discord.Embed(
            title=f"Roles in {ctx.guild.name}",
            color=discord.Colour.blue(),
        )
        embed.add_field(
            name="Roles with Permissions",
            value="\n".join(with_perms) or "None",
            inline=False,
        )
        await ctx.send(embed=embed, ephemeral=True)

        embed = discord.Embed(
            title=f"Roles in {ctx.guild.name}",
            color=discord.Colour.blue(),
        )
        embed.add_field(
            name="Roles with No Permissions",
            value="\n".join(no_perms) or "None",
            inline=False,
        )
        await ctx.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="unused_roles",
        description="List all roles with 0 members (for cleanup).",
    )
    async def unused_roles(self, interaction: discord.Interaction) -> None:
        """List roles with zero members."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        results = audit_utils.check_unused_roles(ctx.guild)
        await self._send_audit_embed(
            ctx,
            "Unused Roles (0 Members)",
            results,
            color=discord.Colour.dark_green(),
        )


async def setup(bot: KiwiBot) -> None:
    """Load the SecurityAudit cog."""
    await bot.add_cog(SecurityAudit(bot))
    log.info("Cog 'SecurityAudit' loaded.")

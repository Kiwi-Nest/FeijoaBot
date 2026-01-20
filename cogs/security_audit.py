from __future__ import annotations

import asyncio
import logging
import textwrap
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules import audit_utils, security_utils

if TYPE_CHECKING:
    from modules.ConfigDB import ConfigDB
    from modules.KiwiBot import KiwiBot

log = logging.getLogger(__name__)


# Use the type alias from audit_utils
AuditReport = audit_utils.AuditReport
AuditResult = audit_utils.AuditResult


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

    def __init__(self, bot: KiwiBot, *, config_db: ConfigDB) -> None:
        """Initialize the SecurityAudit cog."""
        self.bot = bot
        self.config_db = config_db
        super().__init__()
        # Start the automated background check
        self.audit_loop.start()

    async def cog_unload(self) -> None:
        """Cancel the background loop when unloading the cog."""
        self.audit_loop.cancel()

    @tasks.loop(hours=24)
    async def audit_loop(self) -> None:
        """Daily scan for high-priority security risks."""
        log.info("Running daily security audit loop.")

        for guild in self.bot.guilds:
            try:
                config = await self.config_db.get_guild_config(guild.id)
                issues = audit_utils.validate_config(guild, config)

                # We iterate the list of AuditIssues returned
                for issue in issues:
                    if issue.category == "Config Error":
                        self.bot.dispatch(
                            "security_alert",
                            guild_id=guild.id,
                            risk_level="HIGH",
                            details=f"**Issue:** {issue.category}\n{issue.details}",
                        )

            except Exception:
                log.exception("Error during security audit for guild %s", guild.name)

    @audit_loop.before_loop
    async def before_audit_loop(self) -> None:
        """Wait until the bot is ready before starting the audit loop."""
        await self.bot.wait_until_ready()

    @staticmethod
    def _smart_chunk(text: str, limit: int = 1024) -> list[str]:
        """Split text into chunks of at most `limit` chars, trying to break on newlines/spaces."""
        if len(text) <= limit:
            return [text]

        # Use textwrap to handle intelligent line breaking
        return textwrap.wrap(
            text,
            width=limit,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
            expand_tabs=False,
        )

    async def _send_audit_embed(
        self,
        ctx: commands.Context,
        title: str,
        results: AuditResult | AuditReport,
        color: discord.Colour,
        conclusion: str = "Audit complete.",
    ) -> None:
        """Send a standardized, paginated embed for audit results.

        Args:
            ctx: The command context.
            title: Title for the embed.
            results: Either a list of AuditIssue or an AuditReport instance.
            color: Color for the embed.
            conclusion: Concluding message for the audit (default: "Audit complete.").

        """
        if not results:
            embed = discord.Embed(
                title=title,
                description="✅ No issues found.",
                color=discord.Colour.green(),
            )
            await ctx.send(embed=embed, ephemeral=True)
            return

        # Canonicalize input to AuditReport for uniform handling
        report = AuditReport()
        if isinstance(results, list):
            for issue in results:
                report.add(issue)
        else:
            report = results

        summary = report.get_summary()

        # Create the first embed
        embed = discord.Embed(title=title, color=color)

        # We will split by fields. Discord limit is 25 fields per embed.
        # AND 6000 characters total per embed.

        field_count = 0
        current_embed_char_count = len(title)

        first_embed = True

        for category, text in summary.items():
            # If text is too long for a single field value (1024), we might need to split it
            # But the requirement was "Fix Pagination with field chunks"
            # Let's handle simple 1024 chunks for safety

            # Use smart chunking to avoid breaking mentions or markdown
            chunks = self._smart_chunk(text, 1024)

            for i, chunk in enumerate(chunks):
                name = category if i == 0 else f"{category} (Cont.)"

                # Check limits
                if field_count >= 25 or (current_embed_char_count + len(name) + len(chunk)) > 5800:
                    # Send current embed and start new one
                    await ctx.send(embed=embed, ephemeral=True)
                    first_embed = False
                    embed = discord.Embed(title=f"{title} (Continued)", color=color)
                    field_count = 0
                    current_embed_char_count = len(title) + 12  # approximation

                embed.add_field(name=name, value=chunk, inline=False)
                field_count += 1
                current_embed_char_count += len(name) + len(chunk)

        if field_count > 0:
            await ctx.send(embed=embed, ephemeral=True)

        # Send a final conclusion message if it was a multi-page report or just to be polite
        if not first_embed:
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

        config = await self.config_db.get_guild_config(ctx.guild.id)
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

        config = await self.config_db.get_guild_config(ctx.guild.id)
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

        config = await self.config_db.get_guild_config(ctx.guild.id)
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

    def _add_issues_to_report(
        self,
        report: AuditReport,
        *results: AuditResult,
    ) -> None:
        """Add issues from multiple audit results to the report."""
        for result in results:
            for issue in result:
                report.add(issue)

    async def _run_full_audit(self, ctx: commands.Context) -> AuditReport:
        """Run all audit checks concurrently and return grouped results.

        This uses asyncio.TaskGroup (Python 3.11+) for modern concurrency.
        """
        report = AuditReport()
        if not ctx.guild:
            return report

        config = await self.config_db.get_guild_config(ctx.guild.id)

        # 1. Synchronous Checks
        sync_results = [
            audit_utils.validate_config(ctx.guild, config),
            audit_utils.check_dangerous_roles(ctx.guild, config),
            audit_utils.check_role_hierarchy(ctx.guild),
            audit_utils.check_bot_permissions(ctx.guild),
            audit_utils.check_risky_overwrites(ctx.guild, config),
            audit_utils.check_desynced_channels(ctx.guild),
            audit_utils.check_hidden_channels(ctx.guild),
            audit_utils.check_unused_roles(ctx.guild),
            audit_utils.check_server_config(ctx.guild),
        ]
        self._add_issues_to_report(report, *sync_results)

        # 2. Asynchronous Checks
        async with asyncio.TaskGroup() as tg:
            t_invites = tg.create_task(audit_utils.check_invites(ctx.guild))
            t_webhooks = tg.create_task(audit_utils.check_webhooks(ctx.guild))
            t_automod = tg.create_task(audit_utils.check_automod(ctx.guild))

        # Collect async results
        async_results = [t_invites.result(), t_webhooks.result(), t_automod.result()]
        self._add_issues_to_report(report, *async_results)

        return report

    @app_commands.command(
        name="full",
        description="Run a simplified, comprehensive security audit of the server.",
    )
    async def audit_full(self, interaction: discord.Interaction) -> None:
        """Run a full security audit and report grouped issues."""
        ctx = await self.bot.get_context(interaction, cls=commands.Context)
        if not ctx.guild:
            return

        # Defer immediately as this might take a few seconds
        await ctx.defer(ephemeral=True)

        try:
            full_report = await self._run_full_audit(ctx)
        except Exception:
            log.exception("Failed to run full audit")
            await ctx.send(
                "❌ An error occurred while running the audit. Please check logs.",
                ephemeral=True,
            )
            return

        await self._send_audit_embed(
            ctx,
            "🛡️ Comprehensive Security Audit",
            full_report,
            color=discord.Colour.orange(),
        )


async def setup(bot: KiwiBot) -> None:
    """Load the SecurityAudit cog."""
    await bot.add_cog(SecurityAudit(bot, config_db=bot.config_db))
    log.info("Cog 'SecurityAudit' loaded.")

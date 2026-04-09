from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules import audit_utils, security_utils

if TYPE_CHECKING:
    from modules.BotCore import BotCore
    from modules.ConfigDB import ConfigDB

log = logging.getLogger(__name__)


# Use the type alias from audit_utils
AuditReport = audit_utils.AuditReport
AuditResult = audit_utils.AuditResult
AuditIssue = audit_utils.AuditIssue

ENTITY_THRESHOLD = 15


class IssueEntityView(discord.ui.View):
    """Paginated view for a single AuditIssue whose entity list exceeds ENTITY_THRESHOLD."""

    def __init__(self, issue: AuditIssue, color: discord.Colour, per_page: int = ENTITY_THRESHOLD) -> None:
        super().__init__(timeout=300)
        self.issue = issue
        self.color = color
        self.per_page = per_page
        self.current_page = 0
        self.max_page = (len(issue.entities) - 1) // per_page

    async def get_embed(self) -> discord.Embed:
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.max_page

        start = self.current_page * self.per_page
        end = start + self.per_page
        mentions = ", ".join(e.mention for e in self.issue.entities[start:end])

        embed = discord.Embed(title=self.issue.category, color=self.color)
        if self.issue.details:
            embed.description = self.issue.details
        embed.add_field(name="Affected", value=mentions or "None", inline=False)
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.max_page + 1} · {len(self.issue.entities)} total")
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.current_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="➡️")
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.current_page = min(self.max_page, self.current_page + 1)
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)


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

    def __init__(self, bot: BotCore, *, config_db: ConfigDB) -> None:
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
    def _render_compact_category(category: str, issue_list: list[AuditIssue]) -> tuple[str, str]:
        """Combine all compact issues for a category into one bullet-list field value."""
        lines = []
        for issue in issue_list:
            line = issue.details or ""
            if issue.entities:
                mentions = ", ".join(e.mention for e in issue.entities)
                line += (" - " if line else "") + mentions
            if line:
                lines.append(f"• {line}")
        return category, "\n".join(lines) or "No details."

    async def _send_audit_embed(
        self,
        ctx: commands.Context,
        title: str,
        results: AuditResult | AuditReport,
        color: discord.Colour,
    ) -> None:
        """Send audit results as category-atomic embeds with entity pagination for large lists."""
        if not results:
            embed = discord.Embed(
                title=title,
                description="✅ No issues found.",
                color=discord.Colour.green(),
            )
            await ctx.send(embed=embed, ephemeral=True)
            return

        # Canonicalize to AuditReport
        report = AuditReport()
        if isinstance(results, list):
            for issue in results:
                report.add(issue)
        else:
            report = results

        compact_embed = discord.Embed(title=title, color=color)
        compact_field_count = 0
        compact_char_count = len(title)

        for category, issue_list in report.issues.items():
            large = [i for i in issue_list if len(i.entities) > ENTITY_THRESHOLD]
            compact = [i for i in issue_list if len(i.entities) <= ENTITY_THRESHOLD]

            # Batch all compact issues for this category into one field
            if compact:
                name, value = self._render_compact_category(category, compact)
                new_chars = len(name) + len(value)
                if compact_field_count > 0 and (compact_field_count >= 25 or compact_char_count + new_chars > 5800):
                    await ctx.send(embed=compact_embed, ephemeral=True)
                    compact_embed = discord.Embed(title=f"{title} (Continued)", color=color)
                    compact_field_count = 0
                    compact_char_count = len(title) + 12
                field_value = value[:1021] + "…" if len(value) > 1024 else value
                compact_embed.add_field(name=name, value=field_value, inline=False)
                compact_field_count += 1
                compact_char_count += new_chars

            # Each large-entity issue gets its own paginated message
            for issue in large:
                if compact_field_count > 0:
                    await ctx.send(embed=compact_embed, ephemeral=True)
                    compact_embed = discord.Embed(title=f"{title} (Continued)", color=color)
                    compact_field_count = 0
                    compact_char_count = len(title) + 12

                view = IssueEntityView(issue, color)
                await ctx.send(embed=await view.get_embed(), view=view, ephemeral=True)

        if compact_field_count > 0:
            await ctx.send(embed=compact_embed, ephemeral=True)

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

        def _join_truncated(mentions: list[str]) -> str:
            joined = ", ".join(mentions)
            return joined[:1021] + "…" if len(joined) > 1024 else joined or "None"

        embed = discord.Embed(title=f"Roles in {ctx.guild.name}", color=discord.Colour.blue())
        embed.add_field(name="Roles with Permissions", value=_join_truncated(with_perms), inline=False)
        embed.add_field(name="Roles with No Permissions", value=_join_truncated(no_perms), inline=False)
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


async def setup(bot: BotCore) -> None:
    """Load the SecurityAudit cog."""
    await bot.add_cog(SecurityAudit(bot, config_db=bot.config_db))
    log.info("Cog 'SecurityAudit' loaded.")

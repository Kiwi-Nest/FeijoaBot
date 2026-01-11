"""Core logic functions for performing security audits on a guild.

This module is intentionally decoupled from the command interface.
It takes discord.py models and GuildConfig objects, performs checks,
and returns string-based results, leaving the "frontend" (e.g., an embed)
to the calling cog.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from modules import security_utils

if TYPE_CHECKING:
    from collections.abc import Iterator

    from modules.ConfigDB import GuildConfig


# A simple type alias for audit results to improve readability.
@dataclass
class AuditIssue:
    category: str
    entities: list[discord.abc.User | discord.Role | discord.abc.GuildChannel]
    details: str | None = None


class AuditReport:
    def __init__(self) -> None:
        self.issues: dict[str, list[AuditIssue]] = defaultdict(list)

    def add(self, issue: AuditIssue) -> None:
        self.issues[issue.category].append(issue)

    def __iter__(self) -> Iterator[AuditIssue]:
        """Make AuditReport iterable by yielding all issues."""
        for issues in self.issues.values():
            yield from issues

    def get_summary(self) -> dict[str, str]:
        """Return a dictionary mapping categories to their display string."""
        summary = {}
        for category, issues in self.issues.items():
            category_text = ""

            for issue in issues:
                # 1. Add the Details (if present)
                if issue.details:
                    category_text += f"{issue.details}\n"

                # 2. Add the Entities
                if not issue.entities:
                    continue

                # If we have a massive amount of entities, summarize
                if len(issue.entities) > 20:
                    category_text += f"👉 **{len(issue.entities)} entities** (Too many to list)\n"
                else:
                    # Clean comma-separated list
                    mentions = ", ".join(e.mention for e in issue.entities)
                    category_text += f"👉 {mentions}\n"

                category_text += "\n"  # Spacing between issues

            summary[category] = category_text.strip()
        return summary

    def __bool__(self) -> bool:
        """Return true if any issues to report."""
        return bool(self.issues)


# Type alias for audit function return values
# All audit functions return a list of issues, which can be aggregated into an AuditReport
type AuditResult = list[AuditIssue]

# --- Enhanced Audit Functions ---


def _check_fake_admin_roles(guild: discord.Guild) -> AuditResult:
    """Check for roles with dangerous permissions below cosmetic roles."""
    highest_cosmetic_role: discord.Role | None = None
    found_cosmetic = False
    fake_admin_roles = []

    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.is_default() or role.managed:
            continue

        # Check if this is a cosmetic role (no permissions)
        if not found_cosmetic and role.permissions == discord.Permissions.none():
            highest_cosmetic_role = role
            found_cosmetic = True
            continue

        # If we've found the cosmetic threshold, check roles below it
        if found_cosmetic:
            dangerous_perms = [p for p in security_utils.DANGEROUS_PERMISSIONS if getattr(role.permissions, p, False)]
            if dangerous_perms:
                fake_admin_roles.append(role)

    # Add ONE aggregated issue if offenders exist
    if fake_admin_roles and highest_cosmetic_role:
        return [
            AuditIssue(
                category="Fake Admin",
                entities=fake_admin_roles,
                details=f"These roles have dangerous permissions but are positioned BELOW the cosmetic separator {highest_cosmetic_role.mention}. This breaks hierarchy logic and may allow privilege escalation.",  # noqa: E501
            ),
        ]
    return []


def _check_mention_sensitivity(guild: discord.Guild) -> AuditResult:
    """Check for roles with mention_everyone held by majority of members."""
    results: AuditResult = []
    total_members = guild.member_count or 0
    if total_members <= 10:
        return results

    for role in guild.roles:
        if role.is_default() or role.managed:
            continue
        if role.permissions.mention_everyone:
            role_member_count = len(role.members)
            if role_member_count > (total_members / 2):
                results.append(
                    AuditIssue(
                        category="Mention Sensitivity",
                        entities=[role],
                        details=f"Allows `mention_everyone` and is held by {role_member_count}/{total_members} members.",
                    ),
                )
    return results


def _check_privilege_escalation(guild: discord.Guild) -> AuditResult:
    """Check for roles with Manage Roles above Administrator roles."""
    admin_roles = [r for r in guild.roles if r.permissions.administrator and not r.managed and not r.is_default()]
    manager_roles = [
        r
        for r in guild.roles
        if r.permissions.manage_roles and not r.permissions.administrator and not r.managed and not r.is_default()
    ]

    if not (admin_roles and manager_roles):
        return []

    lowest_admin = min(admin_roles, key=lambda r: r.position)
    escalation_risks = [r for r in manager_roles if r.position > lowest_admin.position]

    if escalation_risks:
        escalation_detail = (
            f"Has `Manage Roles` and is above an Admin role ({lowest_admin.mention}). Can assign admin role to self."
        )
        return [
            AuditIssue(
                category="Privilege Escalation Risk",
                entities=escalation_risks,
                details=escalation_detail,
            ),
        ]
    return []


def check_role_hierarchy(guild: discord.Guild) -> AuditResult:
    """Analyze role hierarchy for "Fake Admin" and @everyone sensitivity.

    Args:
        guild: The guild to scan.

    Returns:
        A list of warnings.

    """
    return [
        *_check_fake_admin_roles(guild),
        *_check_mention_sensitivity(guild),
        *_check_privilege_escalation(guild),
    ]


async def check_invites(guild: discord.Guild) -> AuditResult:
    """Audit active invites for infinite expiration/uses.

    Args:
        guild: The guild to scan.

    Returns:
        A list of warnings.

    """
    results: AuditResult = []
    try:
        invites = await guild.invites()
        for invite in invites:
            if invite.max_age == 0 and invite.max_uses == 0:
                inviter = invite.inviter.mention if invite.inviter else "Unknown"
                results.append(
                    AuditIssue(
                        category="Infinite Invite",
                        entities=[],  # Invite is not a standard entity we listed, putting details in text
                        details=f"Code `{invite.code}` by {inviter} never expires and has no use limit.",
                    ),
                )
    except discord.Forbidden:
        results.append(
            AuditIssue(
                category="Missing Permissions",
                entities=[],
                details="Cannot audit invites (Missing `Manage Guild`).",
            ),
        )

    return results


async def check_webhooks(guild: discord.Guild) -> AuditResult:
    """Audit webhooks for orphans.

    Args:
        guild: The guild to scan.

    Returns:
        A list of warnings.

    """
    results: AuditResult = []
    try:
        webhooks = await guild.webhooks()
        for webhook in webhooks:
            # Check if creator is still in guild
            creator = webhook.user
            if creator is None:
                results.append(
                    AuditIssue(
                        category="Orphaned Webhook",
                        entities=[webhook.channel] if webhook.channel else [],
                        details=f"`{webhook.name}` - Creator deleted account.",
                    ),
                )
                continue

            member = guild.get_member(creator.id)
            if member is None:
                # Cache miss - try fetching from API to verify they actually left
                try:
                    member = await guild.fetch_member(creator.id)
                except (discord.NotFound, discord.HTTPException):
                    # Member truly not in server
                    results.append(
                        AuditIssue(
                            category="Orphaned Webhook",
                            entities=[webhook.channel] if webhook.channel else [],
                            details=f"`{webhook.name}` - Creator {creator.mention} not in server.",
                        ),
                    )

    except discord.Forbidden:
        results.append(
            AuditIssue(
                category="Missing Permissions",
                entities=[],
                details="Cannot audit webhooks (Missing `Manage Webhooks`).",
            ),
        )

    return results


def check_server_config(guild: discord.Guild) -> AuditResult:
    """Audit global server configuration.

    Args:
        guild: The guild to scan.

    Returns:
        A list of warnings.

    """
    results: AuditResult = []

    # Verification Level Safety
    # Risk: "None" or "Low" verification levels allow raids.
    if guild.member_count and guild.member_count > 50 and guild.verification_level.value < discord.VerificationLevel.medium.value:
        results.append(
            AuditIssue(
                category="Weak Verification",
                entities=[],
                details=f"Level is `{guild.verification_level.name}`. Recommended: `medium` or higher.",
            ),
        )

    # 2FA Requirement (for admins)
    if guild.mfa_level == discord.MFALevel.disabled:
        # Check if we have admins
        has_admins = any(r.permissions.administrator for r in guild.roles if not r.managed)
        if has_admins:
            results.append(
                AuditIssue(
                    category="Weak Security",
                    entities=[],
                    details="Server 2FA (MFA) is disabled. Admins should require 2FA.",
                ),
            )

    return results


async def check_automod(guild: discord.Guild) -> AuditResult:
    """Audit AutoMod rules for blind spots.

    Args:
        guild: The guild to scan.

    Returns:
        A list of warnings.

    """
    results: AuditResult = []

    # Check if AutoMod is even possible (Community enabled?) - actually AutoMod is available for all now generally.
    try:
        rules = await guild.fetch_automod_rules()
    except discord.Forbidden:
        return [
            AuditIssue(
                category="Missing Permissions",
                entities=[],
                details="Cannot check AutoMod rules (Missing `Manage Guild`).",
            ),
        ]

    has_mention_spam = False

    for rule in rules:
        if rule.trigger.type == discord.AutoModRuleTriggerType.mention_spam:
            has_mention_spam = True

        # Check for Blind Spots
        # Users/Roles exempted that are NOT admins/mods
        if rule.exempt_roles:
            exempt_roles_list = []
            for role_id in rule.exempt_roles:
                role = guild.get_role(role_id)
                if role:
                    # If the role is NOT dangerous (i.e. regular user role), why is it exempt?
                    # We use our danger list. If it has NO dangerous perms, it's a "regular" role?
                    # Or simpler: Is it distinct from the "admin/mod" set?
                    # Let's check permissions.
                    is_mod_admin = any(getattr(role.permissions, p, False) for p in security_utils.DANGEROUS_PERMISSIONS)
                    if not is_mod_admin and not role.is_default():
                        exempt_roles_list.append(role)

            if exempt_roles_list:
                results.append(
                    AuditIssue(
                        category="AutoMod Blind Spot",
                        entities=exempt_roles_list,
                        details=f"Rule `{rule.name}` exempts these non-admin roles.",
                    ),
                )

    if not has_mention_spam:
        results.append(
            AuditIssue(
                category="Missing Protection",
                entities=[],
                details="No 'Mention Spam' AutoMod rule detected.",
            ),
        )

    return results


def _group_entities_by_permissions(
    entities: list[discord.Role | discord.Member],
    perm_source_attr: str = "permissions",
) -> list[AuditIssue]:
    """Group entities by their dangerous permission set."""
    results: list[AuditIssue] = []
    entity_map: dict[tuple[str, ...], list[discord.Role | discord.Member]] = {}

    for entity in entities:
        # For roles it is entity.permissions, for members it is entity.guild_permissions
        if isinstance(entity, discord.Member) and perm_source_attr == "guild_permissions":
            perms = entity.guild_permissions
        else:
            perms = entity.permissions

        dangerous_perms_found = sorted([name for name in security_utils.DANGEROUS_PERMISSIONS if getattr(perms, name, False)])

        if dangerous_perms_found:
            perm_key = tuple(dangerous_perms_found)
            if perm_key not in entity_map:
                entity_map[perm_key] = []
            entity_map[perm_key].append(entity)

    # Convert groups to AuditIssues
    for perm_tuple, group in entity_map.items():
        formatted_perms = ", ".join(f"`{p}`" for p in perm_tuple)
        results.append(
            AuditIssue(
                category=(
                    f"Entities with {formatted_perms}"
                    if not isinstance(group[0], discord.Member)
                    else f"Bots with {formatted_perms}"
                ),
                entities=group,
                details=None,
            ),
        )
    return results


def validate_config(guild: discord.Guild, config: GuildConfig) -> AuditResult:
    """Check if all role/channel IDs in the config exist in the guild.

    Args:
        guild: The guild to check against.
        config: The guild's configuration from the database.

    Returns:
        A list of warning strings for missing items.

    """
    results: AuditResult = []

    # Schema: (ID value, Fetch Method, Name)
    checks = [
        (config.muted_role_id, guild.get_role, "Muted Role"),
        (config.verified_role_id, guild.get_role, "Verified Role"),
        (config.automute_role_id, guild.get_role, "Auto-Mute Role"),
        (config.mod_log_channel_id, guild.get_channel, "Mod Log Channel"),
        (config.join_leave_log_channel_id, guild.get_channel, "Join/Leave Log Channel"),
    ]

    for item_id, fetch_method, name in checks:
        if item_id and not fetch_method(item_id):
            results.append(
                AuditIssue(
                    category="Config Error",
                    entities=[],
                    details=f"{name} ID is set but not found.",
                ),
            )

    return results


def check_dangerous_roles(guild: discord.Guild, config: GuildConfig) -> AuditResult:
    """Audit all roles for dangerous permissions and misconfigurations.

    Args:
        guild: The guild to check roles from.
        config: The guild's configuration for awareness.

    Returns:
        A list of warning strings for risky roles.

    """
    results: AuditResult = []
    muted_role = guild.get_role(config.muted_role_id) if config.muted_role_id else None

    # Filter for relevant roles first
    relevant_roles = [role for role in guild.roles if not role.is_default() and not role.managed]

    # 1. Permission Grouping (Delegated to helper)
    # The helper now returns semantic categories like "🚨 Roles with Administrator"
    perm_issues = _group_entities_by_permissions(relevant_roles, perm_source_attr="permissions")
    results.extend(perm_issues)

    # 2. Specific Muted Role Checks
    if muted_role and (
        muted_role.permissions.send_messages or muted_role.permissions.add_reactions or muted_role.permissions.speak
    ):
        results.append(
            AuditIssue(
                category="Critical Muted Role Issue",
                entities=[muted_role],
                details=f"Muted Role ({muted_role.mention}) can talk/react/speak!",
            ),
        )

    return results


def check_bot_permissions(guild: discord.Guild) -> AuditResult:
    """Audit all bots and list any dangerous permissions they have.

    Args:
        guild: The guild to check bots in.

    Returns:
        A list of warning strings for risky bots.

    """
    results: AuditResult = []

    bots = [m for m in guild.members if m.bot]

    # Delegate to helper
    # Accessing guild_permissions for members
    perm_issues = _group_entities_by_permissions(bots, perm_source_attr="guild_permissions")
    results.extend(perm_issues)

    return results


def check_risky_overwrites(guild: discord.Guild, config: GuildConfig) -> AuditResult:  # noqa: PLR0912
    """Scan all channels for dangerous permission overwrites.

    Args:
        guild: The guild to scan.
        config: The guild's configuration (for Muted Role).

    Returns:
        A list of warnings about channels with risky overwrites.

    """
    results: AuditResult = []
    muted_role = guild.get_role(config.muted_role_id) if config.muted_role_id else None

    # Grouping overwrite risks
    mute_bypass_channels = []
    spam_risk_channels = []

    for channel in guild.channels:
        # Check for mute bypass
        if muted_role:
            overwrites = channel.overwrites_for(muted_role)
            if overwrites.send_messages or overwrites.add_reactions or overwrites.speak:
                mute_bypass_channels.append(channel)

        # Check for @everyone/@here spam risk
        everyone_overwrites = channel.overwrites_for(guild.default_role)
        if everyone_overwrites.mention_everyone:
            spam_risk_channels.append(channel)

    if mute_bypass_channels:
        results.append(
            AuditIssue(
                category="Mute Bypass",
                entities=mute_bypass_channels,
                details=(f"{muted_role.mention} can talk/react/speak here." if muted_role else "Muted role issue"),
            ),
        )

    if spam_risk_channels:
        results.append(
            AuditIssue(
                category="Spam Risk",
                entities=spam_risk_channels,
                details="@everyone can `mention_everyone` here.",
            ),
        )

    # Find channels where a specific Role has `mention_everyone` explicitly set to True in overrides.
    # This overrides the server-wide setting.

    ghost_ping_channels = []
    for channel in guild.channels:
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, (discord.Role, discord.Member)) and overwrite.mention_everyone is True:
                # If the role/member already has it globally, it's redundant but not a "hidden" risk per se,
                # but if they do NOT have it globally, this is a dangerous override.

                has_global_perm = False
                if isinstance(target, discord.Role):
                    has_global_perm = target.permissions.mention_everyone
                elif isinstance(target, discord.Member):
                    has_global_perm = target.guild_permissions.mention_everyone

                if not has_global_perm:
                    ghost_ping_channels.append(channel)
                    break  # One hit per channel is enough for report

    if ghost_ping_channels:
        results.append(
            AuditIssue(
                category="Ghost Ping Risk",
                entities=ghost_ping_channels,
                details="Members/roles granted `mention_everyone` via override (bypassing global setting).",
            ),
        )

    return results


def check_desynced_channels(guild: discord.Guild) -> AuditResult:
    """Find all channels not synced with their parent category.

    Args:
        guild: The guild to scan.

    Returns:
        A list of desynchronized channels.

    """
    desynced = [channel for channel in guild.channels if channel.category and not channel.permissions_synced]
    if desynced:
        return [
            AuditIssue(
                category="Desynchronized Channels",
                entities=desynced,
                details="Channels not synced with their category.",
            ),
        ]
    return []


def check_hidden_channels(guild: discord.Guild) -> AuditResult:
    """Find all channels hidden from the @everyone role.

    Args:
        guild: The guild to scan.

    Returns:
        A list of hidden channels.

    """
    results: AuditResult = []
    everyone_role = guild.default_role
    hidden_channels = []

    for channel in guild.channels:
        # Only check channels that have viewable permissions
        if not isinstance(
            channel,
            (
                discord.TextChannel,
                discord.VoiceChannel,
                discord.ForumChannel,
                discord.StageChannel,
            ),
        ):
            continue

        overwrites = channel.overwrites_for(everyone_role)
        if overwrites.view_channel is False:
            hidden_channels.append(channel)

    if hidden_channels:
        results.append(
            AuditIssue(
                category="Hidden Channels",
                entities=hidden_channels,
                details="Hidden from @everyone",
            ),
        )

    return results


def check_who_has_permission(guild: discord.Guild, permission: str) -> AuditResult:
    """List all members who have a specific permission.

    Args:
        guild: The guild to scan.
        permission: The string name of the permission.

    Returns:
        A list of members with that permission, or an error string.

    """
    if not hasattr(discord.Permissions, permission):
        return [
            AuditIssue(
                category="Error",
                entities=[],
                details=f"'{permission}' is not a valid permission name.",
            ),
        ]

    results: AuditResult = []
    members_with_perm = [member for member in guild.members if getattr(member.guild_permissions, permission, False)]

    if members_with_perm:
        results.append(AuditIssue(category=f"Members with {permission}", entities=members_with_perm))

    return results


def get_role_lists(guild: discord.Guild) -> tuple[list[str], list[str]]:
    """Sort all roles into two lists: with and without permissions.

    Args:
        guild: The guild to scan.

    Returns:
        A tuple of (roles_with_permissions, roles_without_permissions).

    """
    roles_with_permissions: list[str] = []
    roles_without_permissions: list[str] = []

    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        # Ignore @everyone and managed bot roles
        if role.is_default() or role.managed:
            continue

        if role.permissions == discord.Permissions.none():
            roles_without_permissions.append(role.mention)
        else:
            roles_with_permissions.append(role.mention)

    return roles_with_permissions, roles_without_permissions


def check_unused_roles(guild: discord.Guild) -> AuditResult:
    """Find all roles with 0 members that are not managed.

    Args:
        guild: The guild to scan.

    Returns:
        A list of unused roles.

    """
    unused = [role for role in guild.roles if not role.managed and not role.is_default() and len(role.members) == 0]
    if unused:
        return [
            AuditIssue(
                category="Unused Roles",
                entities=unused,
                details="Roles with 0 members.",
            ),
        ]
    return []

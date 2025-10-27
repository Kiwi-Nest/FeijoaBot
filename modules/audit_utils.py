"""Core logic functions for performing security audits on a guild.

This module is intentionally decoupled from the command interface.
It takes discord.py models and GuildConfig objects, performs checks,
and returns string-based results, leaving the "frontend" (e.g., an embed)
to the calling cog.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from modules import security_utils

if TYPE_CHECKING:
    from modules.ConfigDB import GuildConfig

log = logging.getLogger(__name__)

# A simple type alias for audit results to improve readability.
type AuditResult = list[str]


def validate_config(guild: discord.Guild, config: GuildConfig) -> AuditResult:
    """Check if all role/channel IDs in the config exist in the guild.

    Args:
        guild: The guild to check against.
        config: The guild's configuration from the database.

    Returns:
        A list of warning strings for missing items.

    """
    results: AuditResult = []

    # Check critical roles
    if config.muted_role_id and not guild.get_role(config.muted_role_id):
        results.append("⚠️ **Config Error:** Muted Role ID is set but not found.")
    if config.verified_role_id and not guild.get_role(config.verified_role_id):
        results.append("⚠️ **Config Error:** Verified Role ID is set but not found.")
    if config.automute_role_id and not guild.get_role(config.automute_role_id):
        results.append("⚠️ **Config Error:** Auto-Mute Role ID is set but not found.")

    # Check critical channels
    if config.mod_log_channel_id and not guild.get_channel(config.mod_log_channel_id):
        results.append("⚠️ **Config Error:** Mod Log Channel ID is set but not found.")
    if config.join_leave_log_channel_id and not guild.get_channel(config.join_leave_log_channel_id):
        results.append(
            "⚠️ **Config Error:** Join/Leave Log Channel ID is set but not found.",
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

    for role in guild.roles:
        if role.is_default() or role.managed:
            continue

        # Check for dangerous permissions
        dangerous_perms_found = [
            f"`{name}`" for name in security_utils.DANGEROUS_PERMISSIONS if getattr(role.permissions, name, False)
        ]
        if dangerous_perms_found:
            results.append(f"{role.mention} has: {', '.join(dangerous_perms_found)}")

        if role == muted_role and (role.permissions.send_messages or role.permissions.add_reactions or role.permissions.speak):
            results.append(
                f"🚫 **CRITICAL:** Muted Role ({role.mention}) can talk/react/speak!",
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
    for member in guild.members:
        if member.bot:
            dangerous_perms_found = [
                f"`{name}`" for name in security_utils.DANGEROUS_PERMISSIONS if getattr(member.guild_permissions, name, False)
            ]
            if dangerous_perms_found:
                results.append(
                    f"{member.mention} ({member.name}) has: {', '.join(dangerous_perms_found)}",
                )
    return results


def check_risky_overwrites(guild: discord.Guild, config: GuildConfig) -> AuditResult:
    """Scan all channels for dangerous permission overwrites.

    Args:
        guild: The guild to scan.
        config: The guild's configuration (for Muted Role).

    Returns:
        A list of warnings about channels with risky overwrites.

    """
    results: AuditResult = []
    muted_role = guild.get_role(config.muted_role_id) if config.muted_role_id else None

    for channel in guild.channels:
        # Check for mute bypass
        if muted_role:
            overwrites = channel.overwrites_for(muted_role)
            if overwrites.send_messages or overwrites.add_reactions or overwrites.speak:
                results.append(
                    f"🚫 **Mute Bypass:** {muted_role.mention} can talk/react/speak in {channel.mention}",
                )

        # Check for @everyone/@here spam risk
        everyone_overwrites = channel.overwrites_for(guild.default_role)
        if everyone_overwrites.mention_everyone:
            results.append(
                f"⚠️ **Spam Risk:** @everyone can `mention_everyone` in {channel.mention}",
            )

    return results


def check_desynced_channels(guild: discord.Guild) -> AuditResult:
    """Find all channels not synced with their parent category.

    Args:
        guild: The guild to scan.

    Returns:
        A list of desynchronized channels.

    """
    return [
        f"{channel.mention} is not synced with its category."
        for channel in guild.channels
        if channel.category and not channel.permissions_synced
    ]


def check_hidden_channels(guild: discord.Guild) -> AuditResult:
    """Find all channels hidden from the @everyone role.

    Args:
        guild: The guild to scan.

    Returns:
        A list of hidden channels.

    """
    results: AuditResult = []
    everyone_role = guild.default_role
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
            results.append(f"{channel.mention} (`{channel.name}`)")
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
        return [f"Error: '{permission}' is not a valid permission name."]

    results: AuditResult = []
    for member in guild.members:
        if getattr(member.guild_permissions, permission, False):
            results.append(f"{member.mention} (`{member.display_name}`)")
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
    return [
        f"{role.mention} (`{role.name}`)"
        for role in guild.roles
        if not role.managed and not role.is_default() and len(role.members) == 0
    ]

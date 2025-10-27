from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

import discord

# A context that has an actor (a user/member) and a guild.
# Used for functions that can be triggered by either a message or interaction.
type ActorContext = discord.Interaction | discord.Message

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Holds the result of a boolean security check.

    This is preferable to returning tuples, as it's more explicit
    and avoids complex Union return types.
    """

    ok: bool
    reason: str | None = None


# --- Custom Exception ---


class SecurityCheckError(Exception):
    """Base exception for a failed security validation."""


# --- Validator Functions (Raise Exceptions) ---


def validate_role_safety(
    role: discord.Role,
    *,
    require_no_permissions: bool = False,
) -> None:
    """Check if a role is safe to be configured.

    Raises:
        SecurityCheckError: If the role is dangerous or has permissions
            when `require_no_permissions` is True.

    """
    # 1. Check for roles that must be purely cosmetic
    if require_no_permissions and role.permissions != discord.Permissions.none():
        msg = f"Role {role.mention} must have **no permissions** to be used for this feature."
        raise SecurityCheckError(msg)

    # 2. Always reject @everyone
    if role.is_default():
        msg = "The @everyone role cannot be used for this feature."
        raise SecurityCheckError(msg)

    # 3. Check for dangerous permissions on ANY role
    if role.permissions.administrator:
        msg = f"Role {role.mention} has **Administrator** permissions and cannot be used."
        raise SecurityCheckError(msg)

    dangerous_perms = {
        "Manage Guild": role.permissions.manage_guild,
        "Manage Roles": role.permissions.manage_roles,
        "Manage Messages": role.permissions.manage_messages,
        "Kick Members": role.permissions.kick_members,
        "Ban Members": role.permissions.ban_members,
        "Move Members": role.permissions.move_members,
        "Moderate Members": role.permissions.moderate_members,
    }

    if any(dangerous_perms.values()):
        # Create a clean list of the dangerous perms found
        found_perms = [name for name, has in dangerous_perms.items() if has]
        msg = f"Role {role.mention} has dangerous permissions ({', '.join(found_perms)}) and cannot be used."
        raise SecurityCheckError(msg)


def validate_bot_hierarchy(context: ActorContext, role: discord.Role) -> None:
    """Check if the bot's role is high enough to manage the target role.

    Raises:
        SecurityCheckError: If the check is not in a guild or if the bot's
            hierarchy is too low.

    """
    if not context.guild:
        msg = "Role hierarchy can only be validated in a server."
        raise SecurityCheckError(msg)

    if context.guild.me.top_role <= role:
        msg = (
            f"I cannot manage the {role.mention} role. It is higher than "
            "(or equal to) my own top role. Please move my bot role "
            "higher in the server's role list."
        )
        raise SecurityCheckError(msg)


def validate_moderation_action(
    interaction: discord.Interaction,
    target_member: discord.Member,
) -> None:
    """Perform all pre-action checks for a moderation command.

    Raises:
        SecurityCheckError: On any failure.

    """
    guild = interaction.guild
    if not guild:
        msg = "Moderation actions cannot be performed in DMs."
        raise SecurityCheckError(msg)

    # This check is vital. interaction.user can be discord.User in DMs,
    # but inside a guild, it *should* be a discord.Member.
    # We must have the Member object to check hierarchy.
    actor = interaction.user
    if not isinstance(actor, discord.Member):
        # This case should be rare in a guild context, but it's a good safeguard.
        msg = "Cannot verify your permissions. Are you in this server?"
        raise SecurityCheckError(msg)

    bot_member = guild.me

    if target_member.id == actor.id:
        msg = "You cannot perform this action on yourself."
        raise SecurityCheckError(msg)

    if target_member.id == bot_member.id:
        msg = "You cannot perform this action on me."
        raise SecurityCheckError(msg)

    if target_member.id == guild.owner_id:
        msg = "You cannot perform moderation actions on the server owner."
        raise SecurityCheckError(msg)

    # Server owner bypasses role hierarchy checks
    if guild.owner_id != actor.id and target_member.top_role >= actor.top_role:
        msg = "You cannot moderate a member with an equal or higher role."
        raise SecurityCheckError(msg)

    if target_member.top_role >= bot_member.top_role:
        msg = f"I cannot moderate {target_member.mention}. Their role is higher than (or equal to) my own."
        raise SecurityCheckError(msg)


# --- Boolean-Check Functions (Return ValidationResult) ---


def is_role_safe(
    role: discord.Role,
    *,
    require_no_permissions: bool = False,
) -> ValidationResult:
    """Check if a role is safe for non-command logic.

    Checks for dangerous permissions AND optionally requires zero permissions.
    We also never allow the bot to use @everyone.
    """
    try:
        validate_role_safety(
            role,
            require_no_permissions=require_no_permissions,
        )
    except SecurityCheckError as e:
        # Log the reason for internal debugging
        log.debug("Role %d failed safety check: %s", role.id, e)
        return ValidationResult(False, str(e))

    return ValidationResult(True)


def is_bot_hierarchy_sufficient(
    guild: discord.Guild,
    role: discord.Role,
) -> ValidationResult:
    """Check if the bot's role is high enough to manage the target role.

    This is a boolean-returning version for non-command logic.
    """
    if guild.me.top_role <= role:
        reason = "I cannot manage this role as it is higher than or equal to my own top role."
        log.debug("Hierarchy check failed for role %d: %s", role.id, reason)
        return ValidationResult(False, reason)

    return ValidationResult(True)


# A dictionary of dangerous permissions and a brief reason why.
# This is used by audit commands to check roles and bots.
DANGEROUS_PERMISSIONS: Final[dict[str, str]] = {
    "administrator": "Bypasses all permissions",
    "manage_guild": "Can edit server settings",
    "manage_roles": "Can create/edit/delete roles",
    "manage_channels": "Can create/edit/delete channels",
    "kick_members": "Can kick members",
    "ban_members": "Can ban members",
    "moderate_members": "Can timeout members",
    "mention_everyone": "Can ping @everyone, @here, and all roles",
    "manage_webhooks": "Can create/edit/delete webhooks",
    "manage_emojis_and_stickers": "Can create/edit/delete emojis/stickers",
}

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

import discord
from discord.permissions import Permissions

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


# A permissions object representing all perms allowed for a "verified" role.
# This is a role that is safe to assign, but not purely cosmetic.
# It grants basic participation permissions without any moderation capabilities.
VERIFIED_ROLE_PERMISSIONS: Final[Permissions] = Permissions(
    view_channel=True,
    view_audit_log=True,
    create_instant_invite=True,
    change_nickname=True,
    send_messages=True,
    send_messages_in_threads=True,
    create_public_threads=True,
    create_private_threads=True,
    embed_links=True,
    attach_files=True,
    add_reactions=True,
    use_external_emojis=True,
    use_external_stickers=True,
    read_message_history=True,
    send_voice_messages=True,
    set_voice_channel_status=True,
    connect=True,
    speak=True,
    stream=True,  # 'video' permission
    use_soundboard=True,
    use_external_sounds=True,
    use_voice_activation=True,
    request_to_speak=True,
    use_application_commands=True,
    use_embedded_activities=True,
    use_external_apps=True,
)


# --- Custom Exception ---


class SecurityCheckError(Exception):
    """Base exception for a failed security validation."""


# --- Validator Functions (Raise Exceptions) ---


def validate_role_safety(
    role: discord.Role,
) -> None:
    """Check if a role is safe (i.e., has **no permissions**).

    This is for purely cosmetic roles. For roles with allowed
    permissions (e.g., a "verified" role), use
    `validate_verifiable_role` instead.

    Raises:
        SecurityCheckError: If the role has any permissions or is @everyone.

    """
    # 1. Always reject @everyone
    if role.is_default():
        msg = "The @everyone role cannot be used for this feature."
        raise SecurityCheckError(msg)

    # 2. Check for roles that must be purely cosmetic
    if role.permissions != discord.Permissions.none():
        msg = f"Role {role.mention} must have **no permissions** to be used for this feature."
        raise SecurityCheckError(msg)


def validate_verifiable_role(role: discord.Role) -> None:
    """Check if a role is safe to be used as a "verified" role.

    This checks that the role does not have dangerous permissions
    by ensuring it only has permissions from an explicit allow-list
    (VERIFIED_ROLE_PERMISSIONS).

    Raises:
        SecurityCheckError: If the role is @everyone or has permissions
            that are not on the allowed list.

    """
    # 1. Always reject @everyone
    if role.is_default():
        msg = "The @everyone role cannot be used for this feature."
        raise SecurityCheckError(msg)

    # 2. Check if all permissions are within the allowed set
    # (role.permissions | VERIFIED_ROLE_PERMISSIONS) is the union of perms
    # If this union is *different* from the allowed perms, it means
    # the role has permissions that are *not* in the allowed list.
    if (role.permissions | VERIFIED_ROLE_PERMISSIONS) != VERIFIED_ROLE_PERMISSIONS:
        # Find the extra permissions
        disallowed_perms = role.permissions & ~VERIFIED_ROLE_PERMISSIONS

        # Get the names of the disallowed perms
        found_perms = [name for name, has in disallowed_perms if has]

        if not found_perms:
            # If the list is empty but the check failed, it means there are
            # residual bits set that discord.py does not have a name for yet.
            # We report the raw value so the developer can investigate.
            msg = (
                f"Role {role.mention} has unknown disallowed permissions "
                f"(raw bitfield value: {disallowed_perms.value}). "
                "This usually indicates a new Discord permission not yet supported by your library version."
            )
            raise SecurityCheckError(msg)

        msg = f"Role {role.mention} has permissions that are not allowed for a verified role: {', '.join(found_perms)}"
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
) -> ValidationResult:
    """Check if a role is safe (i.e., has **no permissions**).

    This is a boolean-returning version for non-command logic.
    We also never allow the bot to use @everyone.
    """
    try:
        validate_role_safety(role)
    except SecurityCheckError as e:
        # Log the reason for internal debugging
        log.debug("Role %d failed safety check: %s", role.id, e)
        return ValidationResult(False, str(e))

    return ValidationResult(True)


def is_verifiable_role(role: discord.Role) -> ValidationResult:
    """Check if a role is safe for a verified member.

    Checks that the role only contains permissions from the
    VERIFIED_ROLE_PERMISSIONS allow-list.
    """
    try:
        validate_verifiable_role(role)
    except SecurityCheckError as e:
        # Log the reason for internal debugging
        log.debug("Role %d failed verifiable role check: %s", role.id, e)
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

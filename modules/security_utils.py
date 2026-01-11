from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, NewType

import discord
from discord.permissions import Permissions

# A context that has an actor (a user/member) and a guild.
# Used for functions that can be triggered by either a message or interaction.
type ActorContext = discord.Interaction | discord.Message

WebhookID = NewType("WebhookID", int)
RuleID = NewType("RuleID", int)

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
    send_polls=True,
    request_to_speak=True,
    use_application_commands=True,
    use_embedded_activities=True,
    use_external_apps=True,
)


# --- Custom Exception ---


class SecurityCheckError(Exception):
    """Base exception for a failed security validation."""


# --- Check Functions (Return ValidationResult) ---
# These hold the actual validation logic and return a result object.
# Use these in background/automated contexts where you need to handle
# failures gracefully (e.g., loops, event handlers).


def check_role_safety(role: discord.Role) -> ValidationResult:
    """Check if a role is safe (i.e., has **no permissions**).

    This is for purely cosmetic roles. For roles with allowed
    permissions (e.g., a "verified" role), use `check_verifiable_role` instead.

    Returns:
        ValidationResult with ok=True if safe, or ok=False with reason.

    """
    # 1. Always reject @everyone
    if role.is_default():
        return ValidationResult(False, "The @everyone role cannot be used for this feature.")

    # 2. Check for roles that must be purely cosmetic
    if role.permissions != discord.Permissions.none():
        return ValidationResult(
            False,
            f"Role {role.mention} must have **no permissions** to be used for this feature.",
        )

    return ValidationResult(True)


def check_verifiable_role(role: discord.Role) -> ValidationResult:
    """Check if a role is safe to be used as a "verified" role.

    This checks that the role does not have dangerous permissions
    by ensuring it only has permissions from an explicit allow-list
    (VERIFIED_ROLE_PERMISSIONS).

    Returns:
        ValidationResult with ok=True if safe, or ok=False with reason.

    """
    # 1. Always reject @everyone
    if role.is_default():
        return ValidationResult(False, "The @everyone role cannot be used for this feature.")

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
            return ValidationResult(
                False,
                f"Role {role.mention} has unknown disallowed permissions "
                f"(raw bitfield value: {disallowed_perms.value}). "
                "This usually indicates a new Discord permission not yet supported by your library version.",
            )

        return ValidationResult(
            False,
            f"Role {role.mention} has permissions that are not allowed for a verified role: {', '.join(found_perms)}",
        )

    return ValidationResult(True)


def check_bot_hierarchy(
    guild: discord.Guild,
    role: discord.Role,
) -> ValidationResult:
    """Check if the bot's role is high enough to manage the target role.

    Returns:
        ValidationResult with ok=True if hierarchy is sufficient, or ok=False with reason.

    """
    if guild.me.top_role <= role:
        return ValidationResult(
            False,
            f"I cannot manage the {role.mention} role. It is higher than "
            "(or equal to) my own top role. Please move my bot role "
            "higher in the server's role list.",
        )

    return ValidationResult(True)


def check_moderation_action(
    interaction: discord.Interaction,
    target_member: discord.Member,
) -> ValidationResult:
    """Perform all pre-action checks for a moderation command.

    Returns:
        ValidationResult with ok=True if action is allowed, or ok=False with reason.

    """
    guild = interaction.guild
    actor = interaction.user

    # Validate context: must be in guild with member actor
    if not guild or not isinstance(actor, discord.Member):
        error = (
            "Moderation actions cannot be performed in DMs."
            if not guild
            else "Cannot verify your permissions. Are you in this server?"
        )
        return ValidationResult(False, error)

    bot_member = guild.me

    # Check if target is a protected entity (self, bot, or owner)
    if target_member.id == actor.id:
        return ValidationResult(False, "You cannot perform this action on yourself.")
    if target_member.id in (bot_member.id, guild.owner_id):
        entity_name = "me" if target_member.id == bot_member.id else "the server owner"
        return ValidationResult(False, f"You cannot perform this action on {entity_name}.")

    # Check role hierarchy
    is_owner = guild.owner_id == actor.id
    if not is_owner and target_member.top_role >= actor.top_role:
        return ValidationResult(False, "You cannot moderate a member with an equal or higher role.")
    if target_member.top_role >= bot_member.top_role:
        error_msg = f"I cannot moderate {target_member.mention}. Their role is higher than (or equal to) my own."
        return ValidationResult(False, error_msg)

    return ValidationResult(True)


# --- Ensure Functions (Raise Exceptions) ---
# These are thin wrappers that call check_* and raise SecurityCheckError if not OK.
# Use these in interactive command contexts where you want to abort on failure.


def ensure_role_safety(role: discord.Role) -> None:
    """Ensure a role is safe (i.e., has **no permissions**).

    Raises:
        SecurityCheckError: If the role has any permissions or is @everyone.

    """
    result = check_role_safety(role)
    if not result.ok:
        raise SecurityCheckError(result.reason)


def ensure_verifiable_role(role: discord.Role) -> None:
    """Ensure a role is safe to be used as a "verified" role.

    Raises:
        SecurityCheckError: If the role is @everyone or has disallowed permissions.

    """
    result = check_verifiable_role(role)
    if not result.ok:
        raise SecurityCheckError(result.reason)


def ensure_bot_hierarchy(context: ActorContext, role: discord.Role) -> None:
    """Ensure the bot's role is high enough to manage the target role.

    Raises:
        SecurityCheckError: If not in a guild or if hierarchy is insufficient.

    """
    if not context.guild:
        msg = "Role hierarchy can only be validated in a server."
        raise SecurityCheckError(msg)

    result = check_bot_hierarchy(context.guild, role)
    if not result.ok:
        raise SecurityCheckError(result.reason)


def ensure_moderation_action(
    interaction: discord.Interaction,
    target_member: discord.Member,
) -> None:
    """Ensure all pre-action checks pass for a moderation command.

    Raises:
        SecurityCheckError: On any failure.

    """
    result = check_moderation_action(interaction, target_member)
    if not result.ok:
        raise SecurityCheckError(result.reason)


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

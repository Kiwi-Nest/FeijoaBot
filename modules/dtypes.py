from typing import Literal, NewType, TypeIs, cast, Final, ClassVar, Self, TYPE_CHECKING, overload

import discord
from discord import Permissions
from discord.app_commands import AppCommand, Command, AppCommandGroup, Group, MissingPermissions

if TYPE_CHECKING:
    from discord.app_commands.transformers import CommandParameter

# --- Nominal Types for Discord IDs ---
# Using NewType creates distinct types that are not interchangeable.
# A function expecting a GuildId will raise a type error if given a UserId.
UserId = NewType("UserId", int)
GuildId = NewType("GuildId", int)
ChannelId = NewType("ChannelId", int)
RoleId = NewType("RoleId", int)
MessageId = NewType("MessageId", int)
type RoleIdList = list[RoleId]

# --- Semantic Type Aliases ---
# For complex types that appear in multiple places.
type UserGuildPair = tuple[UserId, GuildId]

# --- Literals for Closed Sets of Values ---
# Enforces that a variable must be one of these specific string values.
type ReminderPreference = Literal["ONCE", "ALWAYS", "NEVER"]
type AnalysisStatus = Literal["OK", "ERROR", "WARN"]

# A specific type for an inviter ID, which can be a real user or unknown (None).
type InviterId = UserId | None

# A new nominal type for integers that represent quantities and should be positive.
PositiveInt = NewType("PositiveInt", int)
NonNegativeInt = NewType("NonNegativeInt", int)


def is_positive(num: int) -> TypeIs[PositiveInt]:
    """Safely cast an int to a PositiveInt."""
    return num > 0


def is_non_negative(num: int) -> TypeIs[NonNegativeInt]:
    """Check if a number is a non-negative integer (>= 0)."""
    return num >= 0


# Define a more specific type for a message we know is from a guild
class GuildMessage(discord.Message):
    author: discord.Member = cast("discord.Member", None)
    guild: discord.Guild = cast("discord.Guild", None)


class GuildInteraction(discord.Interaction):
    # We explicitly tell the type checker: "In this subclass, guild is NOT None"
    guild: discord.Guild
    # You might also want to assert user is a Member, not just User
    user: discord.Member


def is_guild_message(message: discord.Message) -> TypeIs[GuildMessage]:
    """Check if a message is from a guild context."""
    return message.guild is not None and isinstance(message.author, discord.Member)


# Type used to represent a pair of command's local and server version
type FullCommand = tuple[Command, AppCommand]
type FullSubcommand = tuple[Command, AppCommandGroup]

# Data class used for fake interactions
class FakeInteraction:
    permissions: Permissions

    def __init__(self, permissions: Permissions):
        self.permissions = permissions

# Data class used to represent Feijoa's command, used in help
class FeijoaCommand:
    STAFF_PERMS: ClassVar[set[PositiveInt]] = {Permissions.manage_guild.flag, Permissions.manage_roles.flag, Permissions.moderate_members.flag, Permissions.kick_members.flag}

    name: Final[str]
    description: Final[str]
    args: Final[dict[str, CommandParameter]]
    permissions: Final[int]
    command_id: Final[PositiveInt]

    def __init__(self, name: str, description: str, params: dict[str, CommandParameter], permissions: PositiveInt, command_id: PositiveInt):
        self.name = name
        self.description = description
        self.args = params
        self.command_id = command_id
        self.permissions = permissions

    @staticmethod
    def _permission_walker(command: Command | Group) -> PositiveInt:
        perms = 0

        if command.default_permissions:
            perms |= command.default_permissions.value

        if command.parent:
            perms |= FeijoaCommand._permission_walker(command.parent)

        return PositiveInt(perms)

    @staticmethod
    def _check_walker(command: Command | Group) -> PositiveInt:
        permissions = 0

        def check(cmd: Command) -> int:
            perms = 0
            for ch in cmd.checks:
                try:
                    ch(FakeInteraction(permissions=Permissions(0)))
                except MissingPermissions as e:
                    missing = Permissions()

                    for name in e.missing_permissions:
                        setattr(missing, name, True)

                    perms |= missing.value

            return perms

        permissions |= check(command)
        if command.parent and isinstance(command.parent, Command):
            permissions |= FeijoaCommand._check_walker(command.parent)

        return PositiveInt(permissions)

    @classmethod
    def from_app_command(cls, command: FullCommand) -> Self:
        return cls(command[0].name, command[0].description, command[0]._params, PositiveInt(FeijoaCommand._permission_walker(command[0]) | FeijoaCommand._check_walker(command[0])), PositiveInt(command[1].id))

    @classmethod
    def from_app_subcommand(cls, command: FullSubcommand) -> Self:
        return cls(command[1].qualified_name, command[0].description, command[0]._params, PositiveInt(FeijoaCommand._permission_walker(command[0]) | FeijoaCommand._check_walker(command[0])), PositiveInt(command[1].parent.id))

    def get_pretty_printed_perms(self) -> str | None:
        if self.permissions & Permissions.administrator.flag: return "administrator"

        # Fix: Check where bitwise AND is NOT zero (meaning permission is required)
        return ", ".join([
                flag_name.replace("_", " ").title()
                for flag_name, flag_val in Permissions.VALID_FLAGS.items()
                if self.permissions & flag_val == 0
        ])

    def can_be_executed_by(self, user_perms: Permissions):
        if self.permissions == 0: return True

        return self.permissions & user_perms.value

    def is_staff(self) -> bool:
        if not self.permissions: return False
        if self.permissions & Permissions.administrator.flag: return True

        for perm in FeijoaCommand.STAFF_PERMS:
            if self.permissions & perm:
                return True

        return False

    def has_args(self) -> bool:
        return self.args is not None and len(self.args) > 0

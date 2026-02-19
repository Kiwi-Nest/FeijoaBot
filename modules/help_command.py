from typing import TYPE_CHECKING, ClassVar, Final, Self

if TYPE_CHECKING:
    from discord.app_commands import AppCommand, AppCommandGroup

from discord import Permissions
from discord.app_commands import Command, Group, MissingPermissions
from discord.app_commands.transformers import CommandParameter

from modules.dtypes import PositiveInt

# Type used to represent a pair of command's local and server version
type FullCommand = tuple[Command, AppCommand]
type FullSubcommand = tuple[Command, AppCommandGroup]


# Data class used for fake interactions
class FakeInteraction:
    permissions: Permissions

    def __init__(self, permissions: Permissions) -> None:
        self.permissions = permissions


# Data class used to represent Feijoa's command, used in help
class FeijoaCommand:
    STAFF_PERMS: ClassVar[set[PositiveInt]] = {
        Permissions.manage_guild.flag,
        Permissions.manage_roles.flag,
        Permissions.moderate_members.flag,
        Permissions.kick_members.flag,
    }

    name: Final[str]
    description: Final[str]
    args: Final[dict[str, CommandParameter]]
    permissions: Final[int]
    command_id: Final[PositiveInt]

    def __init__(
        self,
        name: str,
        description: str,
        params: dict[str, CommandParameter],
        permissions: PositiveInt,
        command_id: PositiveInt,
    ) -> None:
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
        return cls(
            command[0].name,
            command[0].description,
            command[0]._params,
            PositiveInt(FeijoaCommand._permission_walker(command[0]) | FeijoaCommand._check_walker(command[0])),
            PositiveInt(command[1].id),
        )

    @classmethod
    def from_app_subcommand(cls, command: FullSubcommand) -> Self:
        return cls(
            command[1].qualified_name,
            command[0].description,
            command[0]._params,
            PositiveInt(FeijoaCommand._permission_walker(command[0]) | FeijoaCommand._check_walker(command[0])),
            PositiveInt(command[1].parent.id),
        )

    def get_pretty_printed_perms(self) -> str | None:
        if self.permissions & Permissions.administrator.flag:
            return "administrator"

        # Fix: Check where bitwise AND is NOT zero (meaning permission is required)
        return ", ".join(
            [
                flag_name.replace("_", " ").title()
                for flag_name, flag_val in Permissions.VALID_FLAGS.items()
                if self.permissions & flag_val == 0
            ],
        )

    def can_be_executed_by(self, user_perms: Permissions) -> bool:
        if self.permissions == 0:
            return True

        return bool(self.permissions & user_perms.value)

    def is_staff(self) -> bool:
        if not self.permissions:
            return False
        if self.permissions & Permissions.administrator.flag:
            return True

        return any(self.permissions & perm for perm in FeijoaCommand.STAFF_PERMS)

    def has_args(self) -> bool:
        return self.args is not None and len(self.args) > 0

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.app_commands import AppCommandGroup, Command, ContextMenu, Group
from discord.ext import commands, tasks

from modules.dtypes import FeijoaCommand

if TYPE_CHECKING:
    from modules.KiwiBot import KiwiBot

log = logging.getLogger(__name__)


class Help(commands.Cog):
    command_list: dict[str, FeijoaCommand]

    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot
        self.command_list = {}
        self.refresh_command_list.start()

    @tasks.loop(minutes=10)
    async def refresh_command_list(self) -> None:
        log.info("Command list is being refreshed...")

        # 1. Fetch Local State
        local_command_list = self.bot.tree.get_commands()

        # 2. Fetch Server State (Global first)
        server_command_list = []
        try:
            server_command_list = await self.bot.tree.fetch_commands()
        except discord.HTTPException:
            log.exception("Failed to fetch global commands")
            # We continue; server_command_list is empty, so we rely on guild commands or fail gracefully later

        server_by_name = {cmd.name: cmd for cmd in server_command_list}

        # 3. Fetch Guild State and Merge
        # Iterate over all guilds the bot is in to find guild-specific commands
        if self.bot.guilds:
            for guild in self.bot.guilds:
                try:
                    guild_commands = await self.bot.tree.fetch_commands(guild=guild)
                    for cmd in guild_commands:
                        # Overwrite global command with guild version if specific, or add new guild-only command
                        server_by_name[cmd.name] = cmd
                except (discord.HTTPException, discord.Forbidden):
                    log.warning(f"Failed to fetch commands for guild {guild.id} ({guild.name})")

        # 4. Build NEW dictionary to avoid Race Condition
        new_command_list: dict[str, FeijoaCommand] = {}

        # Process Root Commands
        for local in local_command_list:
            if isinstance(local, ContextMenu | Group):
                continue

            server = server_by_name.get(local.name)
            if server:
                new_command_list[local.name] = FeijoaCommand.from_app_command((local, server))

        # Process Subcommands (Local)
        local_subcommands: list[Command] = []
        for group in local_command_list:
            if isinstance(group, Group):
                local_subcommands.extend(cmd for cmd in group.commands if isinstance(cmd, Command))

        # Process Subcommands (Server)
        server_subcommands: list[AppCommandGroup] = []
        for cmd in server_by_name.values():
            if hasattr(cmd, "options") and cmd.options:
                server_subcommands.extend(option for option in cmd.options if isinstance(option, AppCommandGroup))

        server_subcommand_by_name = {cmd.name: cmd for cmd in server_subcommands}

        for local in local_subcommands:
            if isinstance(local, ContextMenu):
                continue

            server = server_subcommand_by_name.get(local.name)
            if server:
                # Construct key as "group_name subcommand_name"
                key = f"{server.parent.name} {server.name}"
                new_command_list[key] = FeijoaCommand.from_app_subcommand((local, server))

        # Atomic assignment
        self.command_list = new_command_list

    @refresh_command_list.before_loop
    async def before_refresh_command_list(self) -> None:
        """Wait for the bot to be ready before starting the loop."""
        await self.bot.wait_until_ready()

    async def command_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=name, value=name) for name in self.command_list if name.lower().startswith(current.lower())
        ][:25]

    @app_commands.command(
        name="help",
        description="Display a list of all available commands or detailed info for a specific command.",
    )
    @app_commands.describe(command="Command you want to show documentation for.")
    @app_commands.autocomplete(command=command_autocomplete)
    async def help(self, interaction: discord.Interaction, command: str | None = None) -> None:
        embed = discord.Embed()
        # Fix: Use utcnow()
        embed.timestamp = discord.utils.utcnow()

        # Handle avatar gracefully if None
        avatar_url = interaction.user.avatar.url if interaction.user.avatar else None
        embed.set_footer(text=f"Requested by {interaction.user.name}", icon_url=avatar_url)

        if not command:
            # Show generic command list
            embed.colour = discord.Color.green()
            embed.title = "Command List"

            # Simple list generation
            lines = [f"- `/{name}`" for name in self.command_list]
            embed.description = "\n".join(lines)

        elif command in self.command_list:
            requested_cmd = self.command_list[command]
            embed.title = f"Documentation for </{requested_cmd.name}:{requested_cmd.id}>"
            embed.colour = discord.Color.green()

            # Fix: Trailing commas and clean formatting
            embed.add_field(
                name="Command Information",
                inline=False,
                value="\n".join(
                    [
                        f"Command: `{requested_cmd.name}`",
                        f"Description: `{requested_cmd.description}`",
                        f"Is Staff-Only: `{requested_cmd.is_staff()}`",
                        f"[Required Permissions](<https://discord.com/developers/docs/topics/permissions>): `{requested_cmd.get_pretty_printed_perms()}`",
                    ],
                ),
            )

            args_usage: list[str] = []
            if requested_cmd.has_args():
                args_str_list = []
                for argument in requested_cmd.args.values():
                    args_str_list.append(
                        "\n".join(
                            [
                                f"Name: `{argument.name}`",
                                f"Description: `{argument.description}`",
                                f"Type: `{argument.type.name}`",
                                f"Required: `{argument.required}`",
                            ],
                        ),
                    )

                    if argument.required:
                        args_usage.append(f"<{argument.name}: {argument.type.name}>")
                    else:
                        args_usage.append(f"({argument.name}: {argument.type.name})")

                embed.add_field(
                    name="Arguments",
                    inline=False,
                    value="\n----------\n".join(args_str_list),
                )

            usage_str = f"/{requested_cmd.name} {' '.join(args_usage)}"
            embed.add_field(
                name="Usage",
                inline=False,
                value=f"<> = required; () = optional\n```\n{usage_str}\n```",
            )

        else:
            embed.title = "Error"
            embed.colour = discord.Color.red()
            embed.description = f"Command `{command}` not found."

        await interaction.response.send_message(embed=embed)


async def setup(bot: KiwiBot) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(Help(bot=bot))

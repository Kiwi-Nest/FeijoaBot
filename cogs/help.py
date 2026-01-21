import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.app_commands import ContextMenu, Group, AppCommandGroup, Command
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
        log.info(msg="Command list is being refreshed...")
        local_command_list_temp = self.bot.tree.get_commands()
        server_command_list_temp = await self.bot.tree.fetch_commands()

        server_by_name = {cmd.name: cmd for cmd in server_command_list_temp}
        self.command_list.clear()

        for local in local_command_list_temp:
            if isinstance(local, ContextMenu) or \
               isinstance(local, Group): continue

            server = server_by_name.get(local.name)
            if server:
                self.command_list[local.name] = FeijoaCommand.from_app_command((local, server))

        local_subcommands_temp: list[Command] = []
        for group in local_command_list_temp:
            if isinstance(group, Group):
                for cmd in group.commands:
                    if isinstance(cmd, Command):
                        local_subcommands_temp.append(cmd)


        server_subcommands_temp: list[AppCommandGroup] = []
        for group in server_command_list_temp:
            for option in group.options:
                if isinstance(option, AppCommandGroup):
                    server_subcommands_temp.append(option)

        server_subcommand_by_name = {cmd.name: cmd for cmd in server_subcommands_temp}
        for local in local_subcommands_temp:
            if isinstance(local, ContextMenu): continue

            server = server_subcommand_by_name.get(local.name)
            if server:
                self.command_list[server.parent.name + " " + server.name] = FeijoaCommand.from_app_subcommand((local, server))

    @refresh_command_list.before_loop
    async def before_refresh_command_list(self):
        """Wait for the bot to be ready before starting the loop."""
        await self.bot.wait_until_ready()

    async def command_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=name, value=name)
                for name in self.command_list.keys()
                if name.lower().startswith(current.lower())][:25]

    @app_commands.command(
        name="help",
        description="Displays a list of all available commands or detailed info for a specific command."
    )
    @app_commands.describe(command="Command you want to show documentation for.")
    @app_commands.autocomplete(command=command_autocomplete)
    async def help(self, interaction: discord.Interaction, command: str = None) -> None:
        embed = discord.Embed()
        embed.set_footer(text=f"Requested by {interaction.user.name}", icon_url=interaction.user.avatar.url)
        embed.timestamp = datetime.now()

        if not command:  # Show generic command list
            embed.colour = discord.Color.green()
            command_list_str = ""
            for command_name in self.command_list.keys():
                command_list_str += f"- /{command_name}\n"

            embed.title = "Command List"
            embed.description = command_list_str

        elif command in self.command_list.keys():
            requested_cmd = self.command_list[command]
            embed.title = f"Documentation for </{requested_cmd.name}:{requested_cmd.id}>"
            embed.colour = discord.Color.green()

            embed.add_field(name="Command Information", inline=False, value="\n".join([
                f"Command: `{requested_cmd.name}`",
                f"Description: `{requested_cmd.description}`",
                f"Is Staff-Only: `{requested_cmd.is_staff()}`",
                f"[Required Permissions](<https://discord.com/developers/docs/topics/permissions>): `{requested_cmd.permissions.value}`"
            ]))

            args_usage: list[str] = []
            if requested_cmd.has_args():
                args_str = ""
                for index, argument in enumerate(requested_cmd.args.values()):
                    args_str += "\n".join([
                        f"Name: `{argument.name}`",
                        f"Description: `{argument.description}`",
                        f"Type: `{argument.type.name}`",
                        f"Required: `{argument.required}`",
                    ])

                    if argument.required:
                        args_usage.append(f"<{argument.name}: {argument.type.name}>")
                    else:
                        args_usage.append(f"({argument.name}: {argument.type.name})")

                    if index != len(requested_cmd.args) - 1:
                        args_str += "\n----------\n"
                
                embed.add_field(name="Arguments", inline=False, value=args_str)

            embed.add_field(name="Usage", inline=False, value=f"<> = required; () = optional\n```/{requested_cmd.name} {" ".join(args_usage)}```")

        else:  # Invalid command
            embed.title = "Error"
            embed.colour = discord.Color.red()
            embed.description = f"Command `{command}` not found."

        await interaction.response.send_message(embed=embed)
        return

async def setup(bot: KiwiBot) -> None:
    """Add the cog to the bot."""
    await bot.add_cog(Help(bot=bot))
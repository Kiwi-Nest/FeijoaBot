import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from modules.KiwiBot import KiwiBot

# Assuming server_admin.py is in a location Python can import from
from modules.server_admin import CommandExecutionError, RCONConnectionError, ServerManager, ServerNotFoundError, ServerStateError

log = logging.getLogger(__name__)


# --- The Main Cog Class ---
@commands.guild_only()
@app_commands.default_permissions(ban_members=True, kick_members=True)
@app_commands.checks.cooldown(5, 10.0, key=lambda i: (i.guild_id, i.user.id))
class GameAdmin(
    commands.GroupCog,
    group_name="server",
    group_description="Commands for game server administration.",
):
    """A cog for managing game servers via Discord, using GroupCog."""

    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot
        # The manager now comes directly from the bot instance
        self.manager: ServerManager | None = self.bot.server_manager
        super().__init__()

    # --- Centralized Pre-Command Check ---

    async def interaction_check(self, _interaction: discord.Interaction) -> bool:
        """Central check to ensure the Server Manager is running."""
        if not self.manager:
            msg = "❌ The Server Manager is not running. Check bot logs."
            raise app_commands.CheckFailure(msg)
        return True

    # --- Autocomplete Callbacks (Unaffected by interaction_check) ---

    async def _autocomplete_all_servers(
        self,
        _interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.manager:
            return []
        return [app_commands.Choice(name=srv, value=srv) for srv in self.manager.all_servers if current.lower() in srv.lower()][
            :25
        ]

    async def _autocomplete_online_servers(
        self,
        _interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.manager:
            return []
        return [
            app_commands.Choice(name=srv, value=srv) for srv in self.manager.online_servers if current.lower() in srv.lower()
        ][:25]

    async def _autocomplete_offline_servers(
        self,
        _interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.manager:
            return []
        return [
            app_commands.Choice(name=srv, value=srv) for srv in self.manager.offline_servers if current.lower() in srv.lower()
        ][:25]

    async def _autocomplete_rcon_servers(
        self,
        _interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.manager:
            return []

        choices = []
        for srv_name in self.manager.online_servers:
            if current.lower() in srv_name.lower():
                server_info = self.manager.all_servers.get(srv_name)
                if server_info and server_info.rcon_enabled:
                    choices.append(app_commands.Choice(name=srv_name, value=srv_name))
        return choices[:25]

    # --- Logging Helper ---

    async def _log_action(
        self,
        *,
        interaction: discord.Interaction,
        action: str,
        server: str,
        reason: str | None,
        color: discord.Color,
        details: str | None = None,
    ) -> None:
        """Send a standardized log message to the configured log channel."""
        log_channel_id = self.bot.config.game_admin_log_channel_id
        if not log_channel_id:
            return  # Logging is disabled

        log_channel = self.bot.get_channel(log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            log.warning(
                "Log channel with ID %s not found or is not a text channel.",
                log_channel_id,
            )
            return

        embed = discord.Embed(
            title=f"Server Action: {action}",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Server", value=server, inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        if details:
            embed.add_field(name="Details", value=details, inline=False)

        embed.set_footer(text=f"User ID: {interaction.user.id}")

        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            log.exception(
                "Missing permissions to send to log channel %s.",
                log_channel.name,
            )
        except discord.HTTPException:
            log.exception("Failed to send to log channel")

    # --- Commands ---

    @app_commands.command(name="list", description="Shows the status of all managed servers.")
    async def list_servers(self, interaction: discord.Interaction) -> None:
        """Display an overview of all online and offline servers."""
        await interaction.response.defer()
        # No 'if not self.manager' check needed here.

        embed = discord.Embed(
            title="Server Status Overview",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )

        online_list = "\n".join(f"- `{s}`" for s in self.manager.online_servers) or "None"
        offline_list = "\n".join(f"- `{s}`" for s in self.manager.offline_servers) or "None"

        embed.add_field(name="🟢 Online Servers", value=online_list, inline=False)
        embed.add_field(name="🔴 Offline Servers", value=offline_list, inline=False)
        embed.set_footer(text="Use /server status [name] for more details.")

        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="status",
        description="Shows detailed status for a specific server.",
    )
    @app_commands.autocomplete(server=_autocomplete_all_servers)
    @app_commands.describe(server="The name of the server to inspect.")
    async def status(self, interaction: discord.Interaction, server: str) -> None:
        """Show detailed information about a single server."""
        await interaction.response.defer()
        # No 'if not self.manager' check needed here.

        info = self.manager.all_servers.get(server)
        if not info:
            # This check is specific to this command and remains.
            await interaction.followup.send(f"❌ Server `{server}` not found.")
            return

        color = discord.Color.green() if info.status.value == "online" else discord.Color.red()
        embed = discord.Embed(title=f"Status for `{info.name}`", color=color)
        embed.add_field(name="Status", value=info.status.value.title(), inline=True)
        embed.add_field(name="Address", value=f"`{info.ip}:{info.port}`", inline=True)
        embed.add_field(
            name="RCON",
            value=f"Enabled (`{info.rcon_port}`)" if info.rcon_enabled else "Disabled",
            inline=True,
        )
        # Don't leak server path

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="start", description="Starts an offline server.")
    @app_commands.autocomplete(server=_autocomplete_offline_servers)
    @app_commands.describe(
        server="The server to start.",
        reason="The reason for starting the server.",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        server: str,
        reason: str | None = None,
    ) -> None:
        """Handle the logic to start a game server."""
        await interaction.response.defer()
        # No 'if not self.manager' check needed here.

        await self.manager.start(server)
        await interaction.followup.send(
            f"✅ **Start** command sent for `{server}` by {interaction.user.mention}.",
        )
        await self._log_action(
            interaction=interaction,
            action="Start",
            server=server,
            reason=reason,
            color=discord.Color.green(),
        )

    @app_commands.command(name="stop", description="Stops an online server.")
    @app_commands.autocomplete(server=_autocomplete_online_servers)
    @app_commands.describe(
        server="The server to stop.",
        reason="The reason for stopping the server.",
    )
    async def stop(
        self,
        interaction: discord.Interaction,
        server: str,
        reason: str | None = None,
    ) -> None:
        """Handle the logic to stop a game server."""
        await interaction.response.defer()
        # No 'if not self.manager' check needed here.

        await self.manager.stop(server)
        await interaction.followup.send(
            f"✅ **Stop** command sent for `{server}` by {interaction.user.mention}.",
        )
        await self._log_action(
            interaction=interaction,
            action="Stop",
            server=server,
            reason=reason,
            color=discord.Color.orange(),
        )

    @app_commands.command(name="rcon", description="Sends a command to a server via RCON.")
    @app_commands.autocomplete(server=_autocomplete_rcon_servers)
    @app_commands.describe(
        server="The server to send the command to.",
        command="The RCON command to execute.",
        reason="The reason for running this command.",
    )
    async def rcon(
        self,
        interaction: discord.Interaction,
        server: str,
        command: str,
        reason: str | None = None,
    ) -> None:
        """Send an RCON command to an online server."""
        # Whitelist for safe RCON commands. Allows alphanumeric, space, and _, ., -, /, #, "
        # Admins aren't expected to use anything else.
        if not re.match(r"^[a-zA-Z0-9_.\- /#\"]+$", command):
            await interaction.response.send_message("❌ Error: Command contains invalid characters.", ephemeral=True)
            return

        await interaction.response.defer()
        response = await self.manager.run_rcon(server, command)

        response_content = response.strip() if response else "No response from server."
        # Truncate response to fit within Discord's message limit
        MAX_LENGTH = 1950
        if len(response_content) > MAX_LENGTH:
            response_content = response_content[:MAX_LENGTH] + "\n... (response truncated)"

        await interaction.followup.send(
            f"✅ RCON command sent to `{server}` by {interaction.user.mention}.\n```\n{response_content}\n```",
        )
        await self._log_action(
            interaction=interaction,
            action="RCON",
            server=server,
            reason=reason,
            color=discord.Color.dark_blue(),
            details=f"Command: `{command}`",
        )

    @app_commands.command(
        name="refresh",
        description="Forces the bot to re-scan all server statuses.",
    )
    @app_commands.describe(reason="The reason for forcing a refresh.")
    async def refresh(
        self,
        interaction: discord.Interaction,
        reason: str | None = None,
    ) -> None:
        """Trigger a manual refresh of the server list."""
        await interaction.response.defer()
        # No 'if not self.manager' check needed here.

        await self.manager.force_refresh()
        await interaction.followup.send(
            f"✅ Server list refresh initiated by {interaction.user.mention}.",
        )
        await self._log_action(
            interaction=interaction,
            action="Refresh",
            server="All",
            reason=reason,
            color=discord.Color.purple(),
        )

    # --- Centralized Error Handler ---

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Handle errors for all commands in this cog."""
        # Get the root cause of the error
        original = getattr(error, "original", error)

        # Ensure we have a response to send to
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        if isinstance(error, app_commands.CheckFailure):
            # Catches the "Manager not running" check from interaction_check
            await interaction.followup.send(f"{error}", ephemeral=True)
        elif isinstance(original, (ServerNotFoundError, ServerStateError, RCONConnectionError)):
            # These are "safe" errors to show the user
            await interaction.followup.send(f"⚠️ {original}", ephemeral=True)
        elif isinstance(original, CommandExecutionError):
            log.exception(
                "A server script failed for '%s': %s",
                interaction.command.name,
                {original.stderr},
            )
            await interaction.followup.send(
                "❌ The server script failed to execute. Check bot logs for details.",
                ephemeral=True,
            )
        else:
            log.exception("An unexpected error occurred in a game admin command.")
            await interaction.followup.send("❌ An unexpected error occurred.", ephemeral=True)


async def setup(bot: KiwiBot) -> None:
    """Add the GameAdmin cog to the bot."""
    if not bot.config.mc_guild_id or not bot.config.servers_path:
        log.error(
            "GameAdmin cog not loaded. Missing 'MC_GUILD_ID' or 'SERVERS_PATH' in config.",
        )
        return
    await bot.add_cog(GameAdmin(bot), guild=discord.Object(bot.config.mc_guild_id))

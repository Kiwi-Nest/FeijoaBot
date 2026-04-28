import logging
from typing import TYPE_CHECKING, Final, Literal

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, GuildInteraction, RoleId, UserId
from modules.security_utils import (
    SecurityCheckError,
    check_role_safety,
    ensure_bot_hierarchy,
    ensure_role_safety,
    ensure_verifiable_role,
)

if TYPE_CHECKING:
    from modules.BotCore import BotCore
    from modules.ConfigDB import ConfigDB

_ROLE_LIST_PER_PAGE: Final[int] = 20  # rows 0 to 3; row 4 reserved for ◀/▶ nav
_AUTODISCOVER_EXCLUDED: Final[frozenset[str]] = frozenset(
    {"warn", "boy", "girl", "non-binary", "muted", "bot", "red", "orange", "yellow", "green", "blue", "purple", "black"},
)

log = logging.getLogger(__name__)

# Type aliases for clarity
ChannelSetting = Literal[
    "mod_log_channel_id",
    "join_leave_log_channel_id",
    "level_up_channel_id",
    "member_count_channel_id",
    "tag_role_channel_id",
    "bot_warning_channel_id",
]
RoleSetting = Literal[
    "bumper_role_id",
    "backup_bumper_role_id",
    "muted_role_id",
    "tag_role_id",
    "verified_role_id",
    "automute_role_id",
    "xp_opt_out_role_id",
    "inactive_role_id",
]


class RoleListButton(discord.ui.Button):
    def __init__(self, role: discord.Role, style: discord.ButtonStyle) -> None:
        super().__init__(label=role.name, style=style)
        self.role = role

    async def callback(self, interaction: discord.Interaction) -> None:
        view: RoleListView = self.view  # type: ignore[assignment]
        guild_id = GuildId(interaction.guild_id)
        config = await view.config_db.get_guild_config(guild_id)
        current: list[RoleId] = list(getattr(config, view.setting) or [])
        role_id = RoleId(self.role.id)

        if view.action == "add" and role_id not in current:
            current.append(role_id)
            self.style = discord.ButtonStyle.success
        elif view.action == "remove" and role_id in current:
            current.remove(role_id)
            self.style = discord.ButtonStyle.secondary

        self.disabled = True
        await view.config_db.set_setting(guild_id, view.setting, current)
        await interaction.response.edit_message(view=view)


class RoleListView(discord.ui.View):
    """Paginated view of role buttons for add/remove list management."""

    def __init__(
        self,
        all_roles: list[discord.Role],
        config_db: ConfigDB,
        guild_id: GuildId,
        setting: str,
        author_id: UserId,
        label: str,
        *,
        action: Literal["add", "remove"],
    ) -> None:
        super().__init__(timeout=180)
        self.all_roles = all_roles
        self.page = 0
        self.label = label
        self.config_db = config_db
        self.guild_id = guild_id
        self.setting = setting
        self.action = action
        self.author_id = author_id
        self._rebuild()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.all_roles) + _ROLE_LIST_PER_PAGE - 1) // _ROLE_LIST_PER_PAGE)

    @property
    def page_header(self) -> str:
        verb = "add to" if self.action == "add" else "remove from"
        page_info = f" - Page {self.page + 1}/{self.total_pages}" if self.total_pages > 1 else ""
        return f"Click a role to {verb} the {self.label} list{page_info}:"

    def _rebuild(self) -> None:
        self.clear_items()
        start = self.page * _ROLE_LIST_PER_PAGE
        btn_style = discord.ButtonStyle.secondary if self.action == "add" else discord.ButtonStyle.danger
        for role in self.all_roles[start : start + _ROLE_LIST_PER_PAGE]:
            self.add_item(RoleListButton(role, btn_style))
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.secondary,
                disabled=self.page == 0,
                row=4,
            )
            next_btn = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= self.total_pages - 1,
                row=4,
            )
            prev_btn.callback = self._make_page_callback(-1)
            next_btn.callback = self._make_page_callback(1)
            self.add_item(prev_btn)
            self.add_item(next_btn)

    def _make_page_callback(self, delta: int):
        async def callback(interaction: discord.Interaction) -> None:
            self.page += delta
            self._rebuild()
            await interaction.response.edit_message(content=self.page_header, view=self)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return False
        return True


class AutodiscoverView(discord.ui.View):
    """An interactive UI for the /config autodiscover command."""

    def __init__(
        self,
        bot: BotCore,
        author_id: UserId,
        suggestions: dict[str, int],
        *,
        config_db: ConfigDB,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.config_db = config_db
        self.author_id = author_id
        self.suggestions = suggestions

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the command author can interact."""
        if interaction.user.id != self.author_id or not interaction.guild_id:
            await interaction.response.send_message(
                "This isn't your confirmation menu.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Save Suggestions",
        style=discord.ButtonStyle.success,
        emoji="💾",
    )
    async def save_button(
        self,
        interaction: GuildInteraction,
        _button: discord.ui.Button,
    ) -> None:
        """Save the discovered settings to the database."""
        if not interaction.guild_id:
            return

        guild_id = GuildId(interaction.guild_id)
        saved_count = 0
        for setting, value in self.suggestions.items():
            if value is not None:
                await self.config_db.set_setting(guild_id, setting, value)
                saved_count += 1

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content=f"✅ Saved **{saved_count}** suggested settings! You can view them with `/config view`.",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self,
        interaction: GuildInteraction,
        _button: discord.ui.Button,
    ) -> None:
        """Cancel the operation and disable the view."""
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❌ Operation cancelled. No settings were changed.",
            view=self,
        )


def get_suggestions(guild: discord.Guild) -> dict[str, int | None]:  # noqa: PLR0912
    suggestions: dict[str, int | None] = {
        "mod_log_channel_id": None,
        "join_leave_log_channel_id": None,
        "bot_warning_channel_id": None,
        "level_up_channel_id": None,
        "bumper_role_id": None,
        "backup_bumper_role_id": None,
        "muted_role_id": None,
        "member_count_channel_id": None,
        "tag_role_id": None,
        "tag_role_channel_id": None,
        "verified_role_id": None,
        "automute_role_id": None,
        "xp_opt_out_role_id": None,
        "inactive_role_id": None,
    }

    # Scan Text Channels for logs and fallbacks
    for channel in guild.text_channels:
        name = channel.name.lower()
        if "mod" in name and "log" in name:
            suggestions["mod_log_channel_id"] = channel.id
        if "bot" in name and ("warn" in name or "log" in name or "alert" in name):
            suggestions["bot_warning_channel_id"] = channel.id
        if "join" in name or "leave" in name or "welcome" in name:
            suggestions["join_leave_log_channel_id"] = channel.id
        if "level" in name:
            suggestions["level_up_channel_id"] = channel.id

    # Scan Voice Channels for stats
    for channel in guild.voice_channels:
        name = channel.name.lower()
        if "member" in name:
            suggestions["member_count_channel_id"] = channel.id
        if "tag" in name and "user" in name:
            suggestions["tag_role_channel_id"] = channel.id

    # Scan Roles
    for role in guild.roles:
        name = role.name.lower()
        if any(excl in name for excl in _AUTODISCOVER_EXCLUDED):
            continue
        if "mute" in name:
            suggestions["muted_role_id"] = role.id
        if "bumper" in name and "backup" not in name:
            suggestions["bumper_role_id"] = role.id
        if "backup" in name and "bumper" in name:
            suggestions["backup_bumper_role_id"] = role.id
        if "tag" in name and "user" in name:
            suggestions["tag_role_id"] = role.id
        if "member" in name:
            suggestions["verified_role_id"] = role.id
        if "auto" in name and "mute" in name:
            suggestions["automute_role_id"] = role.id
        if "xp" in name and "opt" in name and "out" in name:
            suggestions["xp_opt_out_role_id"] = role.id
        if "inactive" in name:
            suggestions["inactive_role_id"] = role.id

    return suggestions


@app_commands.default_permissions(manage_guild=True)
@commands.guild_only()
class Config(
    commands.GroupCog,
    name="config",
    description="Manage server-specific bot settings.",
):
    """A cog for guild-specific configuration with slash commands."""

    def __init__(self, bot: BotCore, *, config_db: ConfigDB) -> None:
        self.bot = bot
        self.config_db = config_db
        super().__init__()

    async def _manage_role_list(
        self,
        interaction: GuildInteraction,
        role: discord.Role | None,
        setting: str,
        label: str,
        *,
        action: Literal["add", "remove"],
        add_msg: str | None = None,
        remove_msg: str | None = None,
    ) -> None:
        """Add or remove a role from a RoleIdList config setting.

        When role is None, shows a paginated button view to pick interactively.
        """
        guild_id = GuildId(interaction.guild_id)
        config = await self.config_db.get_guild_config(guild_id)

        if role is None:
            if action == "add":
                in_list = set(getattr(config, setting) or [])
                roles = [
                    r
                    for r in interaction.guild.roles
                    if not r.is_default()
                    and RoleId(r.id) not in in_list
                    and check_role_safety(r).ok
                    and interaction.guild.me.top_role > r
                ]
                if not roles:
                    await interaction.response.send_message(
                        f"No eligible roles to add to the {label} list. "
                        "Roles must have zero permissions and be below the bot in hierarchy.",
                        ephemeral=True,
                    )
                    return
            else:
                roles = [r for r_id in (getattr(config, setting) or []) if (r := interaction.guild.get_role(r_id))]
                if not roles:
                    await interaction.response.send_message(f"No roles in the {label} list to remove.", ephemeral=True)
                    return
            view = RoleListView(roles, self.config_db, guild_id, setting, UserId(interaction.user.id), label, action=action)
            await interaction.response.send_message(view.page_header, view=view, ephemeral=True)
            return

        # Role provided - immediate add/remove
        current: list[RoleId] = list(getattr(config, setting) or [])
        role_id = RoleId(role.id)

        if action == "add":
            if role_id in current:
                await interaction.response.send_message(f"{role.mention} is already in the {label} list.", ephemeral=True)
                return
            current.append(role_id)
            msg = add_msg or f"✅ {role.mention} added to the {label} list."
        else:
            if role_id not in current:
                await interaction.response.send_message(f"{role.mention} is not in the {label} list.", ephemeral=True)
                return
            current.remove(role_id)
            msg = remove_msg or f"✅ {role.mention} removed from the {label} list."

        await self.config_db.set_setting(guild_id, setting, current)
        await interaction.response.send_message(msg, ephemeral=True)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Handle errors for all commands in this Cog."""
        # Unwrap CommandInvokeError if present
        original_error = error.original if isinstance(error, app_commands.CommandInvokeError) else error

        if isinstance(original_error, SecurityCheckError):
            await interaction.response.send_message(
                f"❌ **Configuration Blocked:**\n{original_error}",
                ephemeral=True,
            )
        elif isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                f"❌ You do not have the required permissions: {', '.join(error.missing_permissions)}",
                ephemeral=True,
            )
        elif isinstance(error, app_commands.BotMissingPermissions):
            await interaction.response.send_message(
                f"❌ I do not have the required permissions: {', '.join(error.missing_permissions)}",
                ephemeral=True,
            )
        elif isinstance(error, app_commands.AppCommandError):
            # Generic AppCommandError (e.g., from transformers or validators)
            await interaction.response.send_message(str(error), ephemeral=True)
        else:
            log.exception("Unhandled error in Config cog: %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.",
                    ephemeral=True,
                )

    @app_commands.command(
        name="view",
        description="Display the current bot configuration for this server.",
    )
    async def view_config(self, interaction: GuildInteraction) -> None:
        """Display the current configuration in an embed."""
        if not interaction.guild:
            return  # Should be unreachable due to guild_only

        config = await self.config_db.get_guild_config(
            GuildId(interaction.guild.id),
        )

        embed = discord.Embed(
            title=f"Configuration for {interaction.guild.name}",
            color=discord.Colour.blue(),
            description="Use `/config set` commands to change these settings.",
        )

        channels = {
            "Moderation Log": config.mod_log_channel_id,
            "Join/Leave Log": config.join_leave_log_channel_id,
            "Level-Up Announcements": config.level_up_channel_id,
            "Bot Warning Log": config.bot_warning_channel_id,
            "Member Count Channel": config.member_count_channel_id,
            "Tag Role Count Channel": config.tag_role_channel_id,
        }
        forwarding = {
            "Forwarding Source Bot": (f"<@{config.qotd_source_bot_id}>" if config.qotd_source_bot_id else "*Not Set*"),
            "Forwarding Target Channel": (
                f"<#{config.qotd_target_channel_id}>" if config.qotd_target_channel_id else "*Not Set*"
            ),
        }
        roles = {
            "Bumper Role": config.bumper_role_id,
            "Backup Bumper Role": config.backup_bumper_role_id,
            "Muted Role": config.muted_role_id,
            "Tag Role": config.tag_role_id,
            "Verified Role": config.verified_role_id,
            "Automute Role": config.automute_role_id,
            "XP Opt-Out Role": config.xp_opt_out_role_id,
            "Inactive Role": config.inactive_role_id,
        }
        other = {
            "Inactive Member Prune Days": f"{config.inactivity_days} days",
            "Inactive Role Threshold": f"{config.inactive_role_threshold_days} days",
            "Custom Role Prefix": f"`{config.custom_role_prefix}`",
            "Custom Role Prune Days": f"{config.custom_role_prune_days} days",
        }

        prune_roles_value = " ".join(f"<@&{r_id}>" for r_id in config.roles_to_prune) if config.roles_to_prune else "*Not Set*"
        event_roles_value = (
            " ".join(f"<@&{r_id}>" for r_id in config.event_ping_roles) if config.event_ping_roles else "*Not Set*"
        )

        embed.add_field(
            name="🛡️ Inactive Pruning",
            value=f"**Roles to Prune**: {prune_roles_value}",
            inline=False,
        )
        embed.add_field(
            name="🎮 Event Ping Roles",
            value=event_roles_value,
            inline=False,
        )
        embed.add_field(
            name="📝 Channels",
            value="\n".join(f"**{name}**: {f'<#{value}>' if value else '*Not Set*'} " for name, value in channels.items()),
            inline=False,
        )
        embed.add_field(
            name="👑 Roles",
            value="\n".join(f"**{name}**: {f'<@&{value}>' if value else '*Not Set*'} " for name, value in roles.items()),
            inline=False,
        )
        embed.add_field(
            name="↪️ Forwarding",
            value="\n".join(f"**{name}**: {value}" for name, value in forwarding.items()),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Other",
            value="\n".join(f"**{name}**: {value}" for name, value in other.items()),
            inline=False,
        )
        embed.set_footer(
            text="A setting that is 'Not Set' means the related feature is disabled.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="channel",
        description="Set or clear a feature's channel.",
    )
    @app_commands.describe(
        feature="The feature to configure the channel for.",
        channel="The channel to use. Omit to disable the feature.",
    )
    @app_commands.choices(
        feature=[
            app_commands.Choice(name="Moderation Log", value="mod_log_channel_id"),
            app_commands.Choice(
                name="Join/Leave Log",
                value="join_leave_log_channel_id",
            ),
            app_commands.Choice(
                name="Level-Up Announcements",
                value="level_up_channel_id",
            ),
            app_commands.Choice(
                name="Member Count Channel",
                value="member_count_channel_id",
            ),
            app_commands.Choice(
                name="Tag Role Count Channel",
                value="tag_role_channel_id",
            ),
            app_commands.Choice(name="Bot Warning Log", value="bot_warning_channel_id"),
        ],
    )
    async def set_channel(
        self,
        interaction: GuildInteraction,
        feature: app_commands.Choice[str],  # ChannelSetting
        channel: discord.TextChannel | discord.VoiceChannel | None = None,
    ) -> None:
        """Set or unset a channel configuration."""
        if not interaction.guild_id:
            return

        value = channel.id if channel else None
        await self.config_db.set_setting(
            GuildId(interaction.guild_id),
            feature.value,
            value,
        )

        if channel:
            await interaction.response.send_message(
                f"✅ Successfully set the **{feature.name}** channel to {channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ Successfully disabled **{feature.name}** by clearing its channel.",
                ephemeral=True,
            )

    @app_commands.command(name="role", description="Set or clear a feature's role.")
    @app_commands.describe(
        feature="The feature to configure the role for.",
        role="The role to use. Omit to disable the feature.",
    )
    @app_commands.choices(
        feature=[
            app_commands.Choice(name="Bumper Role", value="bumper_role_id"),
            app_commands.Choice(
                name="Backup Bumper Role",
                value="backup_bumper_role_id",
            ),
            app_commands.Choice(name="Muted Role", value="muted_role_id"),
            app_commands.Choice(name="Tag Role", value="tag_role_id"),
            app_commands.Choice(name="Verified Role", value="verified_role_id"),
            app_commands.Choice(name="Automute Role", value="automute_role_id"),
            app_commands.Choice(name="XP Opt-Out Role", value="xp_opt_out_role_id"),
            app_commands.Choice(name="Inactive Role", value="inactive_role_id"),
        ],
    )
    async def set_role(
        self,
        interaction: GuildInteraction,
        feature: app_commands.Choice[str],  # RoleSetting
        role: discord.Role | None = None,
    ) -> None:
        """Set or unset a role configuration."""
        if not interaction.guild_id:
            return

        if role:
            # Define which roles must be cosmetic (no permissions)
            cosmetic_features = [
                "bumper_role_id",
                "backup_bumper_role_id",
                "tag_role_id",
                "xp_opt_out_role_id",
                "inactive_role_id",
            ]
            require_no_perms = feature.value in cosmetic_features

            # Run our centralized security checks (raises SecurityCheckError on failure)
            if require_no_perms:
                ensure_role_safety(role)
            else:
                ensure_verifiable_role(role)
            ensure_bot_hierarchy(interaction, role)

        value = role.id if role else None
        await self.config_db.set_setting(
            GuildId(interaction.guild_id),
            feature.value,
            value,
        )

        if role:
            await interaction.response.send_message(
                f"✅ Successfully set the **{feature.name}** role to {role.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ Successfully disabled **{feature.name}** by clearing its role.",
                ephemeral=True,
            )

    @app_commands.command(
        name="autodiscover",
        description="Automatically discover and suggest settings.",
    )
    async def autodiscover(self, interaction: GuildInteraction) -> None:
        """Scan the server and suggest settings based on channel and role names."""
        if not interaction.guild:
            return
        await interaction.response.defer(ephemeral=True)

        suggestions = get_suggestions(interaction.guild)

        found_items = []
        description_lines = [
            "I've scanned your server and found these potential settings:",
            "",
        ]
        for setting, value in suggestions.items():
            if value is not None:
                # Format name nicely for display
                display_name = setting.replace("_id", "").replace("_", " ").title()
                mention = f"<#{(value)}>" if "channel" in setting else f"<@&{(value)}>"
                description_lines.append(f"**{display_name}**: {mention}")
                found_items.append(setting)

        if not found_items:
            await interaction.followup.send(
                "Couldn't find any channels or roles with common names to suggest.",
            )
            return

        description_lines.append("\nDo you want to apply these suggestions?")
        embed = discord.Embed(
            title="🔎 Autodiscovery Results",
            description="\n".join(description_lines),
            color=discord.Colour.green(),  # Green for success/suggestions
        )
        view = AutodiscoverView(
            self.bot,
            UserId(interaction.user.id),
            {k: v for k, v in suggestions.items() if v is not None},
            config_db=self.config_db,
        )
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="language", description="Set the server's default language.")
    @app_commands.choices(
        lang=[
            app_commands.Choice(name="English 🇬🇧", value="en"),
            app_commands.Choice(name="Romanian 🇷🇴", value="ro"),
        ],
    )
    async def set_server_language(self, interaction: GuildInteraction, lang: str) -> None:
        """Set the default language for the server."""
        if not interaction.guild_id:
            return

        await self.config_db.set_setting(GuildId(interaction.guild.id), "default_language", lang)

        # Friendly response with flag if possible, though simple bold text is fine as per spec
        await interaction.response.send_message(f"✅ Server language set to **{lang}**.")

    # Sub-group for Forwarding Config
    forward = app_commands.Group(
        name="forward",
        description="Configure message forwarding settings.",
    )

    @forward.command(
        name="set-source",
        description="Set the bot to forward messages from.",
    )
    @app_commands.describe(
        bot="The bot user (e.g., QOTD) whose embeds you want to forward.",
    )
    async def set_forward_source(
        self,
        interaction: GuildInteraction,
        bot: discord.User,
    ) -> None:
        """Set the source bot for forwarding."""
        if not interaction.guild_id:
            return

        if not bot.bot:
            await interaction.response.send_message(
                "❌ This must be a bot user.",
                ephemeral=True,
            )
            return

        await self.config_db.set_setting(
            GuildId(interaction.guild_id),
            "qotd_source_bot_id",
            bot.id,
        )
        await interaction.response.send_message(
            f"✅ Embeds from {bot.mention} will now be forwarded.",
            ephemeral=True,
        )

    @forward.command(
        name="set-target",
        description="Set the channel to forward embeds to.",
    )
    @app_commands.describe(
        channel="The text channel where forwarded embeds should be sent.",
    )
    async def set_forward_target(
        self,
        interaction: GuildInteraction,
        channel: discord.TextChannel,
    ) -> None:
        """Set the target channel for forwarding."""
        if not interaction.guild_id:
            return

        await self.config_db.set_setting(
            GuildId(interaction.guild_id),
            "qotd_target_channel_id",
            channel.id,
        )
        await interaction.response.send_message(
            f"✅ Embeds will now be forwarded to {channel.mention}.",
            ephemeral=True,
        )

    @forward.command(
        name="disable",
        description="Disable the message forwarder for this server.",
    )
    async def disable_forwarder(self, interaction: GuildInteraction) -> None:
        """Disable the forwarder by clearing both settings."""
        await self.config_db.set_setting(
            GuildId(interaction.guild_id),
            "qotd_source_bot_id",
            None,
        )
        await self.config_db.set_setting(
            GuildId(interaction.guild_id),
            "qotd_target_channel_id",
            None,
        )
        await interaction.response.send_message(
            "✅ Message forwarding has been disabled for this server.",
            ephemeral=True,
        )

    # Sub-group for Pruning Config
    prune = app_commands.Group(
        name="prune",
        description="Configure automatic pruning settings.",
    )

    @prune.command(
        name="set-inactive-threshold",
        description="Set the number of days of inactivity before assigning the inactive role.",
    )
    @app_commands.describe(days="Number of days (e.g., 50). Must be greater than 0.")
    async def set_inactive_threshold(
        self,
        interaction: GuildInteraction,
        days: app_commands.Range[int, 1],
    ) -> None:
        """Set the inactivity threshold for the inactive role."""
        if not interaction.guild_id:
            return

        await self.config_db.set_setting(
            GuildId(interaction.guild_id),
            "inactive_role_threshold_days",
            days,
        )
        await interaction.response.send_message(
            f"✅ Members will receive the inactive role after **{days}** days of inactivity.",
            ephemeral=True,
        )

    @prune.command(
        name="set-days",
        description="Set the number of days of inactivity before pruning roles.",
    )
    @app_commands.describe(days="Number of days (e.g., 14). Must be greater than 0.")
    async def set_prune_days(
        self,
        interaction: GuildInteraction,
        days: app_commands.Range[int, 1],
    ) -> None:
        """Set the inactivity period for pruning."""
        if not interaction.guild_id:
            return

        await self.config_db.set_setting(
            GuildId(interaction.guild_id),
            "inactivity_days",
            days,
        )
        await interaction.response.send_message(
            f"✅ Inactive members will now have their roles pruned after **{days}** days.",
            ephemeral=True,
        )

    @prune.command(
        name="add-role",
        description="Add a (cosmetic) role to be pruned from inactive members.",
    )
    @app_commands.describe(role="Role to add. Omit to pick from a list.")
    async def add_prune_role(
        self,
        interaction: GuildInteraction,
        role: discord.Role | None = None,
    ) -> None:
        """Add a role to the prune list."""
        if not interaction.guild_id:
            return
        if role is not None:
            ensure_role_safety(role)
            ensure_bot_hierarchy(interaction, role)
        await self._manage_role_list(
            interaction,
            role,
            "roles_to_prune",
            "prune",
            action="add",
            add_msg=f"✅ The role {role.mention} will now be pruned from inactive members." if role else None,
        )

    @prune.command(name="remove-role", description="Remove a role from the prune list.")
    @app_commands.describe(role="Role to remove. Omit to pick from a list.")
    async def remove_prune_role(
        self,
        interaction: GuildInteraction,
        role: discord.Role | None = None,
    ) -> None:
        """Remove a role from the prune list."""
        if not interaction.guild_id:
            return
        await self._manage_role_list(
            interaction,
            role,
            "roles_to_prune",
            "prune",
            action="remove",
            remove_msg=f"✅ The role {role.mention} will no longer be pruned from inactive members." if role else None,
        )

    # Sub-group for Event Role Config
    event_roles = app_commands.Group(
        name="event-roles",
        description="Configure roles users can ping for games and events.",
    )

    @event_roles.command(name="add", description="Allow users to ping a role for events or games.")
    @app_commands.describe(role="Role to add. Omit to pick from a list.")
    async def add_event_role(
        self,
        interaction: GuildInteraction,
        role: discord.Role | None = None,
    ) -> None:
        """Add a role to the event ping list."""
        if not interaction.guild_id:
            return
        if role is not None:
            ensure_role_safety(role)
            ensure_bot_hierarchy(interaction, role)
        await self._manage_role_list(interaction, role, "event_ping_roles", "event ping", action="add")

    @event_roles.command(name="remove", description="Remove a role from the pingable list.")
    @app_commands.describe(role="Role to remove. Omit to pick from a list.")
    async def remove_event_role(
        self,
        interaction: GuildInteraction,
        role: discord.Role | None = None,
    ) -> None:
        """Remove a role from the event ping list."""
        if not interaction.guild_id:
            return
        await self._manage_role_list(interaction, role, "event_ping_roles", "event ping", action="remove")


async def setup(bot: BotCore) -> None:
    """Add the Config cog to the bot."""
    await bot.add_cog(Config(bot, config_db=bot.config_db))

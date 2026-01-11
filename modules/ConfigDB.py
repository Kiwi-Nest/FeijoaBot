"""Manages guild-specific configurations in the database.

This module provides the `guild_configs` table, which serves as the single
source of truth for all server-specific settings. It replaces the reliance on
global environment variables for configurable IDs and values, allowing for
true multi-guild support.

The `ConfigDB` class includes an in-memory cache to minimize database queries
for frequently accessed settings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, ClassVar, Self

from .dtypes import RoleId

if TYPE_CHECKING:  # For type hinting only, avoids circular imports
    from modules.Database import Database

    from .dtypes import ChannelId, GuildId, RoleIdList, UserId

log = logging.getLogger(__name__)


@dataclass(slots=True)
class GuildConfig:
    """A type-safe dataclass to hold all configuration for a single guild."""

    guild_id: GuildId
    mod_log_channel_id: ChannelId | None = None
    join_leave_log_channel_id: ChannelId | None = None
    level_up_channel_id: ChannelId | None = None
    bot_warning_channel_id: ChannelId | None = None
    bumper_role_id: RoleId | None = None
    backup_bumper_role_id: RoleId | None = None
    muted_role_id: RoleId | None = None
    verified_role_id: RoleId | None = None
    automute_role_id: RoleId | None = None
    xp_opt_out_role_id: RoleId | None = None
    roles_to_prune: RoleIdList | None = None
    # Server Stats
    member_count_channel_id: ChannelId | None = None
    tag_role_id: RoleId | None = None
    tag_role_channel_id: ChannelId | None = None
    # Pruning
    inactivity_days: int = 14
    custom_role_prefix: str = "Custom: "
    custom_role_prune_days: int = 30
    # QOTD Forwarder
    qotd_source_bot_id: UserId | None = None
    qotd_target_channel_id: ChannelId | None = None
    default_language: str = "en"  # Default to English

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        """Create a GuildConfig object from a database row tuple."""
        # Manually map row to fields to handle roles_to_prune conversion
        field_values = list(row)
        field_names = [f.name for f in fields(cls)]

        try:
            # Find the index for roles_to_prune and convert it
            prune_roles_index = field_names.index("roles_to_prune")
            prune_roles_str: str | None = field_values[prune_roles_index]
            if prune_roles_str:
                field_values[prune_roles_index] = [RoleId(int(r_id)) for r_id in prune_roles_str.split(",") if r_id.isdigit()]
            else:
                field_values[prune_roles_index] = None
        except (ValueError, IndexError):
            log.exception("Failed to parse roles_to_prune from database row.")
        return cls(*field_values)


class ConfigDB:
    """Manages the `guild_configs` table with an in-memory cache."""

    TABLE_NAME: ClassVar[str] = "guild_configs"

    def __init__(self, database: Database) -> None:
        self.database = database
        self._cache: dict[GuildId, GuildConfig] = {}

    async def post_init(self) -> None:
        """Initialize the database table for guild configurations."""
        async with self.database.get_conn() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    -- Core Identity
                    guild_id INTEGER PRIMARY KEY CHECK(guild_id > 1000000 AND
                                                guild_id < 10000000000000000000),

                    -- Configurable Channel IDs (Nullable)
                    mod_log_channel_id      INTEGER CHECK(mod_log_channel_id > 1000000 AND
                                                mod_log_channel_id < 10000000000000000000),
                    join_leave_log_channel_id INTEGER CHECK(join_leave_log_channel_id > 1000000 AND
                                                join_leave_log_channel_id < 10000000000000000000),
                    level_up_channel_id     INTEGER CHECK(level_up_channel_id > 1000000 AND
                                                level_up_channel_id < 10000000000000000000),
                    bot_warning_channel_id  INTEGER CHECK(bot_warning_channel_id > 1000000 AND
                                                bot_warning_channel_id < 10000000000000000000),

                    -- Configurable Role IDs (Nullable)
                    bumper_role_id          INTEGER CHECK(bumper_role_id > 1000000 AND
                                                bumper_role_id < 10000000000000000000),
                    backup_bumper_role_id   INTEGER CHECK(backup_bumper_role_id > 1000000 AND
                                                backup_bumper_role_id < 10000000000000000000),
                    muted_role_id           INTEGER CHECK(muted_role_id > 1000000 AND
                                                muted_role_id < 10000000000000000000),
                    verified_role_id        INTEGER CHECK(verified_role_id > 1000000 AND
                                                verified_role_id < 10000000000000000000),
                    automute_role_id        INTEGER CHECK(automute_role_id > 1000000 AND
                                                automute_role_id < 10000000000000000000),
                    xp_opt_out_role_id      INTEGER CHECK(xp_opt_out_role_id > 1000000 AND
                                                xp_opt_out_role_id < 10000000000000000000),

                    -- Server Stats Channel/Role IDs (Nullable)
                    member_count_channel_id INTEGER CHECK(member_count_channel_id > 1000000 AND
                                                member_count_channel_id < 10000000000000000000),
                    tag_role_id             INTEGER CHECK(tag_role_id > 1000000 AND
                                                tag_role_id < 10000000000000000000),
                    tag_role_channel_id     INTEGER CHECK(tag_role_channel_id > 1000000 AND
                                                tag_role_channel_id < 10000000000000000000),

                    -- Pruning Settings
                    roles_to_prune          TEXT, -- Comma-separated list of role IDs
                    inactivity_days         INTEGER NOT NULL DEFAULT 14 CHECK(inactivity_days > 0),

                    -- Other Settings with Defaults
                    custom_role_prefix      TEXT NOT NULL DEFAULT 'Custom: ',
                    custom_role_prune_days  INTEGER NOT NULL DEFAULT 30 CHECK(custom_role_prune_days > 0),

                    qotd_source_bot_id      INTEGER CHECK(qotd_source_bot_id > 1000000 AND
                                                qotd_source_bot_id < 10000000000000000000),
                    qotd_target_channel_id  INTEGER CHECK(qotd_target_channel_id > 1000000 AND
                                                qotd_target_channel_id < 10000000000000000000),

                    default_language        TEXT NOT NULL DEFAULT 'en'

                ) STRICT, WITHOUT ROWID;
                """,
            )
            await conn.commit()
            log.info("Initialized guild_configs database table.")

    def _invalidate_cache(self, guild_id: GuildId) -> None:
        """Remove a guild's configuration from the cache."""
        if guild_id in self._cache:
            del self._cache[guild_id]
            log.debug("Invalidated cache for guild ID %s.", guild_id)

    async def get_guild_config(self, guild_id: GuildId) -> GuildConfig:
        """Fetch all settings for a guild, using the cache if available.

        If no configuration exists, a default one is returned but not saved.
        """
        if guild_id in self._cache:
            return self._cache[guild_id]

        async with self.database.get_cursor() as cursor:
            column_names = ", ".join(f.name for f in fields(GuildConfig))
            await cursor.execute(
                f"SELECT {column_names} FROM {self.TABLE_NAME} WHERE guild_id = ?",  # noqa: S608
                (guild_id,),
            )
            row = await cursor.fetchone()

        config = GuildConfig.from_row(row) if row else GuildConfig(guild_id=guild_id)

        self._cache[guild_id] = config
        return config

    async def set_setting(self, guild_id: GuildId, setting: str, value: int | str | RoleIdList | None) -> None:
        """Update a single configuration value for a guild."""
        # Validate that the setting is a valid field in the dataclass
        if setting not in {f.name for f in fields(GuildConfig)}:
            msg = f"'{setting}' is not a valid configuration setting."
            raise ValueError(msg)  # Raise an error for invalid setting names

        # Special handling for list of roles
        if setting == "roles_to_prune" and isinstance(value, list):
            # Convert list of ints to a comma-separated string for storage
            value = ",".join(map(str, value)) if value else None

        # Use INSERT ... ON CONFLICT to create or update the setting
        sql = f"""
            INSERT INTO {self.TABLE_NAME} (guild_id, {setting}) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET {setting} = excluded.{setting}
        """  # noqa: S608
        async with self.database.get_conn() as conn:
            await conn.execute(sql, (guild_id, value))
            await conn.commit()

        self._invalidate_cache(guild_id)
        log.info("Updated setting '%s' for guild %s.", setting, guild_id)

    async def on_guild_remove(self, guild_id: GuildId) -> None:
        """Clean up data when the bot is removed from a guild."""
        async with self.database.get_conn() as conn:
            await conn.execute(f"DELETE FROM {self.TABLE_NAME} WHERE guild_id = ?", (guild_id,))  # noqa: S608
            await conn.commit()

        self._invalidate_cache(guild_id)
        log.info("Removed configuration for guild %s.", guild_id)

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .dtypes import ChannelId, GuildId, UserId

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotConfig:
    """A frozen dataclass to hold all bot configuration values."""

    token: str
    disboard_bot_id: UserId | None
    # Special case for leveling system which may operate on a privileged guild
    guild_id: GuildId | None
    swl_guild_id: GuildId | None
    host: str | None
    udp_port: int | None
    mc_guild_id: GuildId | None
    game_admin_log_channel_id: ChannelId | None
    servers_path: Path | None
    twelvedata_api_key: str | None
    libretranslate_host: str | None
    tzbot_host: str | None
    tzbot_port: int | None
    tzbot_api_key: str | None
    tzbot_encryption_key: str | None


    @classmethod
    def from_environment(cls) -> Self:
        """Load all configuration from environment variables."""

        def get_env_int(name: str, required: bool = True) -> int | None:
            """Safely get and convert an environment variable to an int."""
            value = os.getenv(name)
            if value:
                try:
                    return int(value)
                except ValueError:
                    log.exception("'%s' is not a valid integer. Check your .env file.", name)
                    if required:
                        raise
                    return None
            if required:
                msg = f"Required environment variable '{name}' is not set."
                raise KeyError(msg)
            return None

        def get_path(name: str) -> Path | None:
            """Safely get an environment variable as a Path object."""
            val = os.getenv(name)
            if not val:
                return None

            if not Path(val).exists():
                return None

            return Path(val)

        token = os.getenv("TOKEN")
        if not token:
            msg = "Required environment variable 'TOKEN' is not set."
            raise KeyError(msg)

        return cls(
            token=token,
            disboard_bot_id=(UserId(val) if (val := get_env_int("DISBOARD_BOT_ID", required=False)) else None),
            # Optional guild features
            guild_id=(GuildId(val) if (val := get_env_int("UDP_GUILD_ID", required=False)) else None),
            swl_guild_id=(GuildId(val) if (val := get_env_int("SWL_GUILD_ID", required=False)) else None),
            host=os.getenv("HOST"),
            udp_port=get_env_int("UDP_PORT", required=False),
            # Game Admin cog settings
            mc_guild_id=(GuildId(val) if (val := get_env_int("MC_GUILD_ID", required=False)) else None),
            game_admin_log_channel_id=(
                ChannelId(val) if (val := get_env_int("GAME_ADMIN_LOG_CHANNEL_ID", required=False)) else None
            ),
            servers_path=get_path("SERVERS_PATH"),
            twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
            libretranslate_host=os.getenv("LIBRETRANSLATE_HOST", "http://localhost:5000"),
            tzbot_host=os.getenv("TZBOT_HOST"),
            tzbot_port=get_env_int("TZBOT_PORT", required=False),
            tzbot_api_key=os.getenv("TZBOT_API_KEY"),
            tzbot_encryption_key=os.getenv("TZBOT_ENCRYPTION_KEY"),
        )

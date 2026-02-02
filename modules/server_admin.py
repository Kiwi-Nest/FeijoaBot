# server_admin.py
# A modern, asyncio-based library for administering game servers.
#
# To enable RCON functionality, install the optional dependency:
# pip install aiomcrcon

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final, Self

# Conditional import for aiomcrcon
AIOMCRCON_AVAILABLE: Final[bool]
try:
    import aiomcrcon

    AIOMCRCON_AVAILABLE = True
except ImportError:
    aiomcrcon = None
    AIOMCRCON_AVAILABLE = False

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

# --- Basic Setup ---
log = logging.getLogger(__name__)


# --- Custom Exceptions ---
class ServerAdminError(Exception):
    """Base exception for all errors in this library."""


class ServerNotFoundError(ServerAdminError):
    """Raised when a server name cannot be found."""


class ServerStateError(ServerAdminError):
    """Raised when an action is attempted on a server in an invalid state."""


class PropertiesError(ServerAdminError):
    """Raised for missing or malformed server.properties files."""


class CommandExecutionError(ServerAdminError):
    """Raised when a shell command fails."""

    def __init__(self, message: str, return_code: int, stderr: str) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.stderr = stderr


class CommandTimeoutError(CommandExecutionError):
    """Raised when a shell command times out."""

    def __init__(self, message: str, stderr: str) -> None:
        super().__init__(message, -1, stderr)


class RCONConnectionError(ServerAdminError):
    """Raised when an RCON connection fails."""


# --- Data Structures ---
class ServerStatus(Enum):
    """Represents the discovered status of a server."""

    ONLINE = "online"
    OFFLINE = "offline"


@dataclass(frozen=True)
class ServerInfo:
    """A read-only container for a server's cached properties."""

    name: str
    path: Path
    status: ServerStatus
    ip: str
    port: int
    rcon_port: int
    rcon_enabled: bool


# --- The Main Library Class ---
class ServerManager:
    """Manager for discovering and administrating servers."""

    _POST_ACTION_REFRESH_DELAY: Final[float] = 5.0
    _SUBPROCESS_TIMEOUT: Final[float] = 3.0
    _PORT_CHECK_TIMEOUT: Final[float] = 1.0

    def __init__(
        self,
        servers_path: Path,
        refresh_interval_seconds: int = 300,
        log_max_age_days: int = 7,
    ) -> None:
        """Initialize the ServerManager.

        Args:
            servers_path: The absolute path to the parent directory containing all server folders.
            refresh_interval_seconds: How often to automatically refresh the server list.
            log_max_age_days: Servers with logs older than this will be ignored.

        """
        self._servers_path = servers_path.expanduser()
        self._refresh_interval = refresh_interval_seconds
        self._log_max_age_seconds = log_max_age_days * 86400

        self._servers: dict[str, ServerInfo] = {}
        self._lock = asyncio.Lock()
        self._refresh_event = asyncio.Event()
        self._background_task: asyncio.Task | None = None

    async def __aenter__(self) -> Self:
        """Enter the async context, starting background tasks."""
        if not self._servers_path.is_dir():
            log.warning("Servers directory not found: %s", self._servers_path)
            return self

        log.info("ServerManager starting up...")
        self._background_task = asyncio.create_task(self._refresh_loop())
        # Await the first scan to ensure the manager is populated on entry.
        await self.force_refresh()
        log.info("ServerManager startup complete. Initial scan finished.")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the async context, shutting down background tasks."""
        if self._background_task and not self._background_task.done():
            self._background_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._background_task
            log.info("ServerManager background task shut down.")

    # --- Public Properties for State Access ---

    @property
    def online_servers(self) -> tuple[str, ...]:
        """Return a sorted tuple of names for all online servers."""
        return tuple(
            sorted(name for name, info in self._servers.items() if info.status == ServerStatus.ONLINE),
        )

    @property
    def offline_servers(self) -> tuple[str, ...]:
        """Return a sorted tuple of names for all offline servers."""
        return tuple(
            sorted(name for name, info in self._servers.items() if info.status == ServerStatus.OFFLINE),
        )

    @property
    def all_servers(self) -> dict[str, ServerInfo]:
        """Return a copy of the internal server information dictionary."""
        return self._servers.copy()

    # --- Public Methods for Actions ---

    async def start(self, server_name: str) -> None:
        """Start a server by running its tmux.sh script."""
        log.info("Received start command for server '%s'.", server_name)
        async with self._lock:
            server = self._servers.get(server_name)
            if not server:
                msg = f"Server '{server_name}' not found."
                raise ServerNotFoundError(msg)
            if server.status == ServerStatus.ONLINE:
                msg = f"Server '{server_name}' is already online."
                raise ServerStateError(msg)

            script_path = server.path / "tmux.sh"
            await self._run_tmux_command(script_path, "sstart")
            log.info("Start script executed for '%s'.", server_name)
            self._schedule_refresh(delay=self._POST_ACTION_REFRESH_DELAY)

    async def stop(self, server_name: str) -> None:
        """Stop a server by running its tmux.sh script."""
        log.info("Received stop command for server '%s'.", server_name)
        async with self._lock:
            server = self._servers.get(server_name)
            if not server:
                msg = f"Server '{server_name}' not found."
                raise ServerNotFoundError(msg)
            if server.status == ServerStatus.OFFLINE:
                msg = f"Server '{server_name}' is already offline."
                raise ServerStateError(msg)

            script_path = server.path / "tmux.sh"
            await self._run_tmux_command(script_path, "sstop")
            log.info("Stop script executed for '%s'.", server_name)
            self._schedule_refresh(delay=self._POST_ACTION_REFRESH_DELAY)

    async def run_rcon(self, server_name: str, command: str) -> str:
        """Run an RCON command on a server.

        Requires the 'aiomcrcon' library to be installed.
        """
        if not AIOMCRCON_AVAILABLE:
            msg = (
                "The 'aiomcrcon' library is not installed, which is required for RCON "
                "functionality. Please install it with 'pip install aiomcrcon'."
            )
            raise ServerAdminError(msg)
        log.info("Executing RCON command on '%s': %s", server_name, command)
        async with self._lock:
            server = self._servers.get(server_name)
            if not server:
                msg = f"Server '{server_name}' not found."
                raise ServerNotFoundError(msg)
            if server.status == ServerStatus.OFFLINE:
                msg = f"Cannot run RCON on offline server '{server_name}'."
                raise ServerStateError(
                    msg,
                )
            if not server.rcon_enabled:
                msg = f"RCON is not enabled for server '{server_name}'."
                raise PropertiesError(
                    msg,
                )

            password = self._read_rcon_password(server.path / "server.properties")
            if not password:
                msg = f"RCON password not found for '{server_name}'."
                raise PropertiesError(msg)

        try:  # aiomcrcon is guaranteed to be imported here due to the guard above
            async with aiomcrcon.Client(  # ty: ignore [possibly-missing-attribute]
                server.ip,
                server.rcon_port,
                password,
            ) as client:
                response, _ = await client.send_cmd(command)
                return response
        except aiomcrcon.errors.RCONConnectionError as e:
            msg = f"Failed to connect to RCON on '{server_name}'."
            raise RCONConnectionError(
                msg,
            ) from e

    async def force_refresh(self) -> None:
        """Trigger an immediate server scan and waits for it to complete."""
        async with self._lock:
            log.info("Forcing a manual refresh...")
            await self._perform_scan_and_update()
            log.info("Manual refresh complete.")

    # --- Internal Background Loop and Helpers ---

    def _schedule_refresh(self, delay: float = 0) -> None:
        """Schedule a refresh to run after a specified delay."""

        async def delayed_set() -> None:
            if delay > 0:
                await asyncio.sleep(delay)
            self._refresh_event.set()

        asyncio.create_task(delayed_set())  # noqa: RUF006

    async def _refresh_loop(self) -> None:
        """Update the cache of servers."""
        log.info("Background refresh loop started.")
        while True:
            try:
                await asyncio.wait_for(
                    self._refresh_event.wait(),
                    timeout=self._refresh_interval,
                )
            except TimeoutError:
                pass  # This is the normal periodic refresh trigger
            finally:
                self._refresh_event.clear()

            async with self._lock:
                log.info("Starting periodic server scan...")
                await self._perform_scan_and_update()
                log.info(
                    "Scan complete. Status: %s online, %s offline.",
                    len(self.online_servers),
                    len(self.offline_servers),
                )

    async def _perform_scan_and_update(self) -> None:
        """Scan the filesystem for servers, checks their status.

        Atomically updates the internal state.
        """
        latest_log_paths = self._servers_path.glob("*/logs/latest.log")
        new_servers: dict[str, ServerInfo] = {}
        scan_tasks = []

        for log_path in latest_log_paths:
            server_path = log_path.parent.parent
            if self._is_server_candidate(server_path, log_path):
                scan_tasks.append(self._build_server_info(server_path))

        results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, ServerInfo):
                new_servers[res.name] = res
            elif res is not None:
                log.warning("Failed to process a server: %s", res)

        self._servers = new_servers

    def _is_server_candidate(self, server_path: Path, log_path: Path) -> bool:
        """Perform initial synchronous checks on a potential server directory."""
        now = time.time()
        try:
            if (now - log_path.stat().st_mtime) > self._log_max_age_seconds:
                return False
        except FileNotFoundError:
            return False

        if not (server_path / "server.properties").is_file():
            return False

        tmux_script = server_path / "tmux.sh"
        return tmux_script.is_file() and os.access(tmux_script, os.X_OK)

    async def _build_server_info(self, server_path: Path) -> ServerInfo | None:
        """Parse config and checks the port to build a full ServerInfo object."""
        name = server_path.name
        try:
            props = self._parse_properties(server_path / "server.properties")
            ip = props.get("server-ip", "127.0.0.1")
            port = int(props.get("server-port", 0))
            if not port:
                msg = "server-port is missing or invalid."
                raise PropertiesError(msg)

            is_online = await self._is_port_open(ip, port)
            status = ServerStatus.ONLINE if is_online else ServerStatus.OFFLINE

            return ServerInfo(
                name=name,
                path=server_path,
                status=status,
                ip=ip,
                port=port,
                rcon_enabled=props.get("enable-rcon", "false").lower() == "true",
                rcon_port=int(props.get("rcon.port", 0)),
            )
        except (PropertiesError, ValueError) as e:
            log.warning("Could not load server '%s': %s", name, e)
            return None

    async def _run_tmux_command(self, script_path: Path, command: str) -> None:
        """Execute a command via the server's tmux.sh script."""
        cmd = f"{shlex.quote(str(script_path))} {shlex.quote(command)}"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                self._SUBPROCESS_TIMEOUT,
            )
        except TimeoutError as err:
            proc.kill()
            await proc.wait()
            msg = f"Command timed out: {cmd}"
            raise CommandTimeoutError(msg, stderr.decode()) from err

        if proc.returncode != 0 and proc.returncode is not None:
            msg = f"Command failed with exit code {proc.returncode}: {cmd}"
            raise CommandExecutionError(
                msg,
                proc.returncode,
                stderr.decode(),
            )

    def _parse_properties(self, path: Path) -> dict[str, str]:
        """Parse Java-style .properties files."""
        if not path.is_file():
            msg = f"Properties file not found: {path}"
            raise PropertiesError(msg)
        props = {}
        with path.open("r") as f:
            for raw_line in f:
                line = raw_line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    props[key.strip()] = value.strip()
        return props

    def _read_rcon_password(self, path: Path) -> str:
        """Read the RCON password from a properties file."""
        try:
            props = self._parse_properties(path)
            return props.get("rcon.password", "")
        except PropertiesError:
            return ""

    async def _is_port_open(self, host: str, port: int) -> bool:
        """Check if a TCP port is open and connectable."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self._PORT_CHECK_TIMEOUT,
            )
            writer.close()
            await writer.wait_closed()
        except (TimeoutError, ConnectionRefusedError, OSError):
            return False
        else:
            return True


ANSI_END = "\033[0m"

MC_TO_ANSI = {
    # Colors
    "§c": "\033[0;31m",
    "§4": "\033[0;31m",  # Red
    "§6": "\033[0;33m",
    "§e": "\033[0;33m",  # Yellow
    "§a": "\033[0;32m",
    "§2": "\033[0;32m",  # Green
    "§3": "\033[0;36m",
    "§b": "\033[0;36m",  # Cyan
    "§1": "\033[0;34m",
    "§9": "\033[0;34m",  # Blue
    "§5": "\033[0;35m",
    "§d": "\033[0;35m",  # Pink
    "§f": "\033[0;37m",  # White
    "§0": "\033[0;30m",
    "§8": "\033[0;30m",  # Gray
    # Formatting
    "§l": "\033[1;2m",  # Bold
    "§n": "\033[4;2m",  # Underline
    # Resets
    "§r": ANSI_END,
    "§x": ANSI_END,
    "§o": ANSI_END,
    "§m": ANSI_END,
    "§7": "",  # Default grey — strip
}


def format_mc_ansi(rawtext: str) -> str:
    if "§" not in rawtext:
        return rawtext

    text = rawtext
    for code, ansi in MC_TO_ANSI.items():
        text = text.replace(code, ansi)

    if text == rawtext:
        return rawtext

    text = text.replace("\n", "\n" + ANSI_END)
    return f"```ansi\n{text}{ANSI_END}\n```"

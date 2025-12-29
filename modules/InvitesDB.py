"""Manages invite tracking data, including API interactions and database storage.

This module handles the persistence of invite relationships between users.
The schema for the `invites` table includes strict checks to ensure data integrity,
such as validating that IDs are legitimate Discord Snowflakes and handling the
special case where an inviter is unknown (represented by `inviter_id = 0`).
"""

import logging
import os
from typing import Any

import aiohttp

from modules.Database import Database  # For type hinting
from modules.dtypes import GuildId, InviterId, UserId

log = logging.getLogger(__name__)

# --- Configuration & Session ---
TOKEN = os.getenv("TOKEN")
API_HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "User-Agent": "Kiwibot/InviteTracker (aiohttp, 1.0)",
}


class InvitesDB:
    """Manages all database and API interactions for invite tracking."""

    def __init__(self, database: Database, session: aiohttp.ClientSession) -> None:
        self.database = database
        self.http_session = session

    async def post_init(self) -> None:
        """Initialize the database table for invites."""
        async with self.database.get_conn() as conn:
            # Use INSERT OR IGNORE to replicate MariaDB's INSERT IGNORE behavior
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS invites (
                    invitee_id INTEGER NOT NULL CHECK(invitee_id > 1000000 AND invitee_id < 10000000000000000000),
                    guild_id INTEGER NOT NULL CHECK(guild_id > 1000000 AND guild_id < 10000000000000000000),
                    inviter_id INTEGER, -- Can be NULL if inviter is unknown
                    joined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
                    PRIMARY KEY (invitee_id, guild_id),
                    CHECK(invitee_id <> inviter_id)
                ) STRICT, WITHOUT ROWID;
                """,
            )
            await conn.commit()
            log.info("Initialized invites database table.")

    async def insert_invite(
        self,
        invitee_id: UserId,
        inviter_id: InviterId,
        guild_id: GuildId,
        joined_at: str | None = None,
    ) -> bool:
        """Insert a new invite record.

        Returns True if a new row was added, False otherwise.
        """
        if invitee_id == inviter_id:
            log.info("User %s invited themself in %s", invitee_id, guild_id)
            return False

        async with self.database.get_conn() as conn:
            if joined_at:
                sql = """
                    INSERT INTO invites (invitee_id, guild_id, inviter_id, joined_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(invitee_id, guild_id) DO NOTHING
                """
                params: tuple[int | str | None, ...] = (
                    invitee_id,
                    guild_id,
                    inviter_id,
                    joined_at,
                )
            else:
                sql = """
                    INSERT INTO invites (invitee_id, inviter_id, guild_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(invitee_id, guild_id) DO NOTHING
                """
                params = (invitee_id, inviter_id, guild_id)

            cursor = await conn.execute(sql, params)
            await conn.commit()
            return cursor.rowcount == 1

    async def bulk_sync_invites(self, member_data: list[tuple[UserId, InviterId, GuildId, str | None]]) -> int:
        """Insert or update multiple invite records in a single transaction.

        This is used by the sync command to correct data from the API.
        Returns the number of rows affected.
        """
        sql = """
            INSERT INTO invites (invitee_id, guild_id, inviter_id, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(invitee_id, guild_id) DO UPDATE SET
                inviter_id = excluded.inviter_id,
                joined_at = COALESCE(excluded.joined_at, invites.joined_at)
            WHERE
                invites.inviter_id IS NOT excluded.inviter_id OR
                (excluded.joined_at IS NOT NULL AND invites.joined_at IS NOT excluded.joined_at);
        """
        async with self.database.get_conn() as conn:
            # Filter out self-invites before executing
            valid_member_data = [
                (invitee, guild, inviter, joined) for invitee, inviter, guild, joined in member_data if invitee != inviter
            ]
            cursor = await conn.executemany(sql, valid_member_data)
            await conn.commit()
            return cursor.rowcount

    async def get_all_invitee_ids(self, guild_id: GuildId) -> set[UserId]:
        """Retrieve a set of all user IDs that have been invited in a guild."""
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                "SELECT DISTINCT invitee_id FROM invites WHERE guild_id = ?",
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return {UserId(row[0]) for row in rows}

    async def get_invites_by_inviter(self, guild_id: GuildId) -> dict[InviterId, list[UserId]]:
        """Retrieve a dictionary mapping each inviter to a list of their invitees' IDs."""
        query = """
            SELECT inviter_id, GROUP_CONCAT(invitee_id)
            FROM invites
            WHERE guild_id = ?
            GROUP BY inviter_id
        """
        result: dict[InviterId, list[UserId]] = {}
        async with self.database.get_cursor() as cursor:
            await cursor.execute(query, (guild_id,))
            rows = await cursor.fetchall()
            for inviter, invitees_str in rows:
                if invitees_str:
                    inviter_id: InviterId = UserId(inviter) if inviter is not None else None
                    typed_invitees = [UserId(int(i)) for i in invitees_str.split(",")]
                    result[inviter_id] = typed_invitees
        return result

    async def get_invite_leaderboard(self, guild_id: GuildId) -> list[tuple[UserId, int]]:
        """Retrieve the top 10 inviters and their invite counts for a guild."""
        query = """
            SELECT inviter_id, COUNT(invitee_id) as invite_count
            FROM invites
            WHERE guild_id = ? AND inviter_id IS NOT NULL
            GROUP BY inviter_id
            ORDER BY invite_count DESC
            LIMIT 10;
        """
        async with self.database.get_cursor() as cursor:
            await cursor.execute(query, (guild_id,))
            rows = await cursor.fetchall()
            # Ensure inviter_id is not None before casting to UserId
            return [(UserId(inviter_id), invite_count) for inviter_id, invite_count in rows if inviter_id is not None]

    async def get_inviter_by_invitee(self, invitee_id: UserId, guild_id: GuildId) -> InviterId | None:
        """Retrieve the inviter ID for a specific invitee in a guild."""
        query = "SELECT inviter_id FROM invites WHERE invitee_id = ? AND guild_id = ?"
        async with self.database.get_cursor() as cursor:
            await cursor.execute(query, (invitee_id, guild_id))
            row = await cursor.fetchone()
            # Return the inviter_id if it exists and is not NULL
            if row and row[0] is not None:
                # The type checker knows row[0] is an int here
                return UserId(row[0])
            return None

    # --- Discord Raw API Operations ---

    async def get_member_details_api(self, username: str, guild_id: GuildId) -> dict[str, Any] | None:
        """Find a specific member by username and returns the one that joined most recently."""
        api_url = f"https://discord.com/api/v10/guilds/{guild_id}/members-search"
        payload = {"query": username, "limit": 5}

        try:
            async with self.http_session.get(api_url, params=payload, headers=API_HEADERS) as resp:
                resp.raise_for_status()
                members = await resp.json()
                if not members:
                    return None
                # Return the member with the most recent join date
                return max(members, key=lambda m: m.get("joined_at", ""))
        except aiohttp.ClientError:
            log.exception("API request failed for user %s", username)
            return None

    async def get_all_guild_members_api(self, guild_id: GuildId) -> list[dict[str, Any]]:
        """Fetch all members from a guild using the members-search endpoint."""
        api_url = f"https://discord.com/api/v10/guilds/{guild_id}/members-search"
        # This endpoint uses POST with an empty query to return all members.
        payload = {"limit": 1000}
        try:
            async with self.http_session.post(api_url, json=payload, headers=API_HEADERS) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("members", [])
        except aiohttp.ClientError:
            log.exception("API request to fetch all guild members failed")
            return []

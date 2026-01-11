from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from modules.Database import Database
    from modules.dtypes import ChannelId, GuildId, UserId

log = logging.getLogger(__name__)


class ReminderDB:
    TABLE_NAME: ClassVar[str] = "reminders"

    def __init__(self, database: Database) -> None:
        self.database = database

    async def post_init(self) -> None:
        """Initialize the reminders table."""
        async with self.database.get_conn() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    message_id INTEGER PRIMARY KEY, -- Snowflake ID acts as PK
                    user_id INTEGER NOT NULL CHECK (user_id > 1000000),
                    guild_id INTEGER NOT NULL CHECK (guild_id > 1000000),
                    channel_id INTEGER NOT NULL CHECK (channel_id > 1000000),
                    message TEXT NOT NULL,
                    remind_at TEXT NOT NULL, -- Stored as UTC ISO string
                    failures INTEGER NOT NULL DEFAULT 0,
                    last_attempt TEXT DEFAULT NULL,
                    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
                ) STRICT;
                """,
            )
            await conn.execute(f"CREATE INDEX IF NOT EXISTS idx_reminders_due ON {self.TABLE_NAME}(remind_at)")
            await conn.commit()

    async def add_reminder(
        self,
        user_id: UserId,
        guild_id: GuildId,
        channel_id: ChannelId,
        message_id: int,
        message: str,
        remind_at: datetime,
    ) -> int:
        """Add a reminder (UPSERT). remind_at must be timezone-aware (UTC)."""
        sql = f"""
            INSERT INTO {self.TABLE_NAME} (message_id, user_id, guild_id, channel_id, message, remind_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                remind_at = excluded.remind_at,
                message = excluded.message,
                failures = 0,
                last_attempt = NULL
        """  # noqa: S608
        # Ensure we store strictly as UTC
        dt_str = remind_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

        async with self.database.get_conn() as conn:
            await conn.execute(sql, (message_id, user_id, guild_id, channel_id, message, dt_str))
            await conn.commit()
            return message_id

    async def get_due_reminders(self) -> list[tuple]:
        """Fetch reminders that are due (PEEK). Does NOT delete."""
        now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"""
                SELECT message_id, user_id, guild_id, channel_id, message, failures
                FROM {self.TABLE_NAME}
                WHERE remind_at <= ?
                ORDER BY remind_at ASC
                """,  # noqa: S608
                (now_str,),
            )
            return await cursor.fetchall()

    async def get_next_reminder(self) -> tuple | None:
        """Fetch the single earliest reminder (future or past due)."""
        async with self.database.get_conn() as conn:
            # We want the absolute earliest time, regardless of if it's past or future
            cursor = await conn.execute(
                f"""
                SELECT message_id, user_id, guild_id, channel_id, message, remind_at
                FROM {self.TABLE_NAME}
                ORDER BY remind_at ASC
                LIMIT 1
                """,  # noqa: S608
            )
            return await cursor.fetchone()

    async def get_active_reminders(self, user_id: UserId) -> list[tuple]:
        """Get all pending reminders for a user."""
        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"SELECT message_id, message, remind_at FROM {self.TABLE_NAME} WHERE user_id = ? ORDER BY remind_at ASC",  # noqa: S608
                (user_id,),
            )
            return await cursor.fetchall()

    async def delete_reminder(self, reminder_id: int, user_id: UserId) -> bool:
        """Delete a specific reminder if it belongs to the user."""
        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"DELETE FROM {self.TABLE_NAME} WHERE message_id = ? AND user_id = ?",  # noqa: S608
                (reminder_id, user_id),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_reminder_by_message_id(self, message_id: int) -> None:
        """Delete a reminder solely by its message ID (used by system cleanup)."""
        async with self.database.get_conn() as conn:
            await conn.execute(
                f"DELETE FROM {self.TABLE_NAME} WHERE message_id = ?",  # noqa: S608
                (message_id,),
            )
            await conn.commit()

    async def handle_failure(self, message_id: int, current_failures: int) -> None:
        """Increment failure count and backoff."""
        new_failures = current_failures + 1
        # Backoff: 10^failures minutes from NOW
        minutes = 10**new_failures
        # Give up after 3 failures
        if new_failures > 3:
            await self.delete_reminder_by_message_id(message_id)
            return

        # Calculate new time (UTC)
        next_attempt = datetime.now(UTC).timestamp() + (minutes * 60)
        next_attempt_str = datetime.fromtimestamp(next_attempt, UTC).strftime("%Y-%m-%d %H:%M:%S")
        now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

        async with self.database.get_conn() as conn:
            await conn.execute(
                f"""
                UPDATE {self.TABLE_NAME}
                SET failures = ?, remind_at = ?, last_attempt = ?
                WHERE message_id = ?
                """,  # noqa: S608
                (new_failures, next_attempt_str, now_str, message_id),
            )
            await conn.commit()

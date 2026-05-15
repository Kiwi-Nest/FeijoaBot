from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modules.dtypes import UserVoiceStats

if TYPE_CHECKING:
    import aiosqlite

    from modules.Database import Database
    from modules.dtypes import GuildId, UserId

log = logging.getLogger(__name__)


class VoiceChatDB:
    TABLE_SLOTS = "vc_slots"
    TABLE_SESSIONS = "vc_sessions"

    def __init__(self, database: Database) -> None:
        self.database = database

    async def post_init(self) -> None:
        async with self.database.get_conn() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_SLOTS} (
                    guildID   TEXT    NOT NULL,
                    day       TEXT    NOT NULL,
                    slot      INTEGER NOT NULL,
                    sum_count INTEGER NOT NULL DEFAULT 0,
                    n_samples INTEGER NOT NULL DEFAULT 0,
                    max_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guildID, day, slot)
                ) STRICT, WITHOUT ROWID
            """)
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_SESSIONS} (
                    guildID   TEXT    NOT NULL,
                    userID    TEXT    NOT NULL,
                    joined_at INTEGER NOT NULL,
                    left_at   INTEGER,
                    PRIMARY KEY (guildID, userID, joined_at)
                ) STRICT, WITHOUT ROWID
            """)
            await conn.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vc_open
                ON {self.TABLE_SESSIONS}(guildID, userID) WHERE left_at IS NULL
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_vc_user
                ON {self.TABLE_SESSIONS}(userID, joined_at)
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_vc_guild_time
                ON {self.TABLE_SESSIONS}(guildID, joined_at)
            """)
            await conn.commit()

    async def write_slot_snapshot(self, guild_id: GuildId, count: int) -> None:
        try:
            async with self.database.get_conn() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {self.TABLE_SLOTS} (guildID, day, slot, sum_count, n_samples, max_count)
                    VALUES (?, date('now'), unixepoch('now') % 86400 / 300, ?, 1, ?)
                    ON CONFLICT(guildID, day, slot) DO UPDATE SET
                        sum_count = sum_count + excluded.sum_count,
                        n_samples = n_samples + 1,
                        max_count = MAX(max_count, excluded.max_count)
                    """,
                    (str(guild_id), count, count),
                )
                await conn.commit()
        except Exception:
            log.exception("Failed to write VC slot snapshot for guild %s", guild_id)

    async def record_join(self, guild_id: GuildId, user_id: UserId) -> None:
        try:
            async with self.database.get_conn() as conn:
                await conn.execute(
                    f"INSERT OR IGNORE INTO {self.TABLE_SESSIONS} (guildID, userID, joined_at) VALUES (?, ?, unixepoch())",
                    (str(guild_id), str(user_id)),
                )
                await conn.commit()
        except Exception:
            log.exception("Failed to record VC join for user %s guild %s", user_id, guild_id)

    async def record_leave(self, guild_id: GuildId, user_id: UserId) -> None:
        try:
            async with self.database.get_conn() as conn:
                await conn.execute(
                    f"UPDATE {self.TABLE_SESSIONS} SET left_at = unixepoch() WHERE guildID = ? AND userID = ? AND left_at IS NULL",
                    (str(guild_id), str(user_id)),
                )
                await conn.commit()
        except Exception:
            log.exception("Failed to record VC leave for user %s guild %s", user_id, guild_id)

    async def reconcile_sessions(self, guild_id: GuildId, current_user_ids: set[int]) -> None:
        """Close stale open sessions and open sessions for users already in VC."""
        try:
            async with self.database.get_conn() as conn:
                cursor = await conn.execute(
                    f"SELECT userID FROM {self.TABLE_SESSIONS} WHERE guildID = ? AND left_at IS NULL",
                    (str(guild_id),),
                )
                open_rows = await cursor.fetchall()
                open_ids = {int(row[0]) for row in open_rows}

                stale = open_ids - current_user_ids
                if stale:
                    await conn.executemany(
                        f"UPDATE {self.TABLE_SESSIONS} SET left_at = unixepoch() WHERE guildID = ? AND userID = ? AND left_at IS NULL",
                        [(str(guild_id), str(uid)) for uid in stale],
                    )

                new_members = current_user_ids - open_ids
                if new_members:
                    await conn.executemany(
                        f"INSERT OR IGNORE INTO {self.TABLE_SESSIONS} (guildID, userID, joined_at) VALUES (?, ?, unixepoch())",
                        [(str(guild_id), str(uid)) for uid in new_members],
                    )

                await conn.commit()
        except Exception:
            log.exception("Failed to reconcile VC sessions for guild %s", guild_id)

    async def guild_peak_today(self, guild_id: GuildId) -> tuple[int, int] | None:
        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"SELECT max_count, slot FROM {self.TABLE_SLOTS} WHERE guildID = ? AND day = date('now') ORDER BY max_count DESC LIMIT 1",
                (str(guild_id),),
            )
            row = await cursor.fetchone()
            if row:
                return (row[0], row[1])
            return None

    async def user_stats_today(
        self,
        guild_id: GuildId,
        user_id: UserId,
    ) -> tuple[int, str | None]:
        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"""
                SELECT
                    SUM(
                        MIN(COALESCE(left_at, unixepoch()), unixepoch('now', 'start of day') + 86400)
                        - MAX(joined_at, unixepoch('now', 'start of day'))
                    ) / 60,
                    datetime(MAX(COALESCE(left_at, joined_at)), 'unixepoch')
                FROM {self.TABLE_SESSIONS}
                WHERE guildID = ? AND userID = ?
                  AND joined_at < unixepoch('now', 'start of day') + 86400
                  AND COALESCE(left_at, unixepoch()) >= unixepoch('now', 'start of day')
                """,
                (str(guild_id), str(user_id)),
            )
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return (int(row[0]), row[1])
            return (0, None)

    async def get_user_voice_stats(self, user_id: UserId) -> UserVoiceStats:
        async with self.database.get_conn() as conn:
            totals_row = await (
                await conn.execute(
                    f"""
                    SELECT
                        SUM(COALESCE(left_at, unixepoch()) - joined_at) / 60,
                        datetime(MAX(COALESCE(left_at, joined_at)), 'unixepoch')
                    FROM {self.TABLE_SESSIONS} WHERE userID = ?
                    """,
                    (str(user_id),),
                )
            ).fetchone()
            total_minutes: int = int(totals_row[0]) if totals_row and totals_row[0] else 0
            last_seen: str | None = totals_row[1] if totals_row else None

            peak_row = await (
                await conn.execute(
                    f"""
                    SELECT date(joined_at, 'unixepoch') AS day,
                           SUM(COALESCE(left_at, unixepoch()) - joined_at) AS secs
                    FROM {self.TABLE_SESSIONS} WHERE userID = ?
                    GROUP BY day ORDER BY secs DESC LIMIT 1
                    """,
                    (str(user_id),),
                )
            ).fetchone()
            peak_day: str | None = peak_row[0] if peak_row else None

        return UserVoiceStats(total_minutes=total_minutes, peak_day=peak_day, last_seen=last_seen)

    async def erase_on_conn(self, conn: aiosqlite.Connection, user_id: UserId) -> int:
        cursor = await conn.execute(
            f"DELETE FROM {self.TABLE_SESSIONS} WHERE userID = ?",
            (str(user_id),),
        )
        return cursor.rowcount

    async def delete_old_sessions(self, days: int = 90) -> int:
        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"DELETE FROM {self.TABLE_SESSIONS} WHERE left_at IS NOT NULL AND left_at < unixepoch('now', ?)",
                (f"-{days} days",),
            )
            await conn.commit()
            deleted = cursor.rowcount
        log.info("Deleted %d VC sessions older than %d days", deleted, days)
        return deleted

    async def infer_streak(self, guild_id: GuildId) -> tuple[int, int] | None:
        """Return (started_at_unix, peak_concurrent) for the current streak, or None if no open sessions.

        Single sweep-line pass: finds the last gap (concurrent → 0), then the first join after it,
        and computes peak concurrent from that point. Handles bot downtime gaps correctly.
        """
        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"""
                WITH horizon AS (
                    SELECT MIN(joined_at) AS t
                    FROM {self.TABLE_SESSIONS} WHERE guildID = ? AND left_at IS NULL
                ),
                events AS (
                    SELECT joined_at AS ts, 1 AS d FROM {self.TABLE_SESSIONS}
                    WHERE guildID = ? AND joined_at >= (SELECT t FROM horizon)
                    UNION ALL
                    SELECT left_at AS ts, -1 AS d FROM {self.TABLE_SESSIONS}
                    WHERE guildID = ? AND left_at IS NOT NULL
                      AND left_at >= (SELECT t FROM horizon)
                ),
                sweep AS (
                    SELECT ts, d,
                           SUM(d) OVER (ORDER BY ts ROWS UNBOUNDED PRECEDING) AS n
                    FROM events
                ),
                last_empty AS (SELECT MAX(ts) AS t FROM sweep WHERE n = 0),
                streak_start AS (
                    SELECT CASE
                        WHEN (SELECT t FROM last_empty) IS NOT NULL
                        THEN (SELECT MIN(ts) FROM sweep
                              WHERE ts > (SELECT t FROM last_empty) AND d = 1)
                        ELSE (SELECT MIN(ts) FROM sweep WHERE d = 1)
                    END AS t
                )
                SELECT
                    (SELECT t FROM streak_start) AS started_at,
                    MAX(CASE WHEN ts >= (SELECT t FROM streak_start) THEN n ELSE 0 END) AS peak
                FROM sweep
                """,
                (str(guild_id), str(guild_id), str(guild_id)),
            )
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return (int(row[0]), int(row[1]) if row[1] is not None else 0)
            return None

    async def get_streak_participants(self, guild_id: GuildId, started_at: int) -> set[int]:
        """All user IDs who were in VC at any point since started_at. Used on restart reconstruction."""
        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"""
                SELECT userID FROM {self.TABLE_SESSIONS}
                WHERE guildID = ? AND joined_at >= ?
                  AND (left_at IS NULL OR left_at > ?)
                """,
                (str(guild_id), started_at, started_at),
            )
            rows = await cursor.fetchall()
            return {int(row[0]) for row in rows}

    async def read_heatmap_data(self, guild_id: GuildId) -> list[tuple[str, int, float]]:
        async with self.database.get_conn() as conn:
            cursor = await conn.execute(
                f"""
                SELECT day, slot, CAST(sum_count AS REAL) / n_samples
                FROM {self.TABLE_SLOTS}
                WHERE guildID = ?
                ORDER BY day, slot
                """,
                (str(guild_id),),
            )
            return await cursor.fetchall()

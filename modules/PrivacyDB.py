"""User privacy operations: erasure (GDPR Art. 17) and data access (GDPR Art. 15)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modules.dtypes import (
    ErasureReport,
    UserDataReport,
    UserGuildRow,
    UserId,
    UserInvite,
    UserPosition,
    UserReminder,
)

if TYPE_CHECKING:
    from modules.Database import Database
    from modules.VoiceChatDB import VoiceChatDB

log = logging.getLogger(__name__)


class PrivacyDB:
    """Manages user erasure and data export across all tables."""

    def __init__(self, database: Database, voicechat_db: VoiceChatDB) -> None:
        self.database = database
        self.voicechat_db = voicechat_db

    async def erase_user(self, user_id: UserId) -> ErasureReport:
        """Atomically erase a user from all tables in a single transaction.

        Deletion order is critical due to FK constraints:
        1. positions (FK child to users)
        2. reminders, invites (no FK constraints)
        3. users (FK parent)
        4. voicechat snapshots (same SQLite file, no FK - joins the same tx)

        currency_ledger rows are intentionally NOT deleted (deferred).
        """
        async with self.database.get_conn() as conn:
            positions_cursor = await conn.execute(
                "DELETE FROM positions WHERE user_id = ?",
                (user_id,),
            )
            reminders_cursor = await conn.execute(
                "DELETE FROM reminders WHERE user_id = ?",
                (user_id,),
            )
            invites_cursor = await conn.execute(
                "DELETE FROM invites WHERE invitee_id = ? OR inviter_id = ?",
                (user_id, user_id),
            )
            users_cursor = await conn.execute(
                "DELETE FROM users WHERE discord_id = ?",
                (user_id,),
            )
            vc_cursor = await conn.execute(
                "DELETE FROM vc_sessions WHERE userID = ?",
                (str(user_id),),
            )
            vc_deleted = vc_cursor.rowcount

            await conn.commit()

        report = ErasureReport(
            positions=positions_cursor.rowcount,
            reminders=reminders_cursor.rowcount,
            invites=invites_cursor.rowcount,
            users=users_cursor.rowcount,
            voicechat_sessions_deleted=vc_deleted,
        )
        log.info(
            "Erased user %s: positions=%d, reminders=%d, invites=%d, users=%d, vc_snapshots=%d",
            user_id,
            report.positions,
            report.reminders,
            report.invites,
            report.users,
            report.voicechat_snapshots_modified,
        )
        return report

    async def get_user_data(self, user_id: UserId) -> UserDataReport:
        """Fetch all personal data stored for a user across all tables."""
        async with self.database.get_conn() as conn:
            # Fetch from users (all guild memberships)
            users_cursor = await conn.execute(
                """
                SELECT guild_id, currency, xp, bumps, level,
                       last_active_timestamp, native_language, timezone
                FROM users WHERE discord_id = ?
                """,
                (user_id,),
            )
            users_rows = await users_cursor.fetchall()
            guilds = [
                UserGuildRow(
                    guild_id=int(row[0]),
                    currency=int(row[1]),
                    xp=int(row[2]),
                    bumps=int(row[3]),
                    level=int(row[4]),
                    last_active_timestamp=str(row[5]),
                    native_language=row[6],
                    timezone=str(row[7]),
                )
                for row in users_rows
            ]

            # Fetch invites
            invites_cursor = await conn.execute(
                "SELECT inviter_id, guild_id, joined_at FROM invites WHERE invitee_id = ?",
                (user_id,),
            )
            invites_rows = await invites_cursor.fetchall()
            invites = [
                UserInvite(
                    inviter_id=int(row[0]) if row[0] is not None else None,
                    guild_id=int(row[1]),
                    joined_at=str(row[2]),
                )
                for row in invites_rows
            ]

            # Fetch reminders
            reminders_cursor = await conn.execute(
                "SELECT message, remind_at, created_at FROM reminders WHERE user_id = ? ORDER BY remind_at",
                (user_id,),
            )
            reminders_rows = await reminders_cursor.fetchall()
            reminders = [
                UserReminder(
                    message=str(row[0]),
                    remind_at=str(row[1]),
                    created_at=str(row[2]) if row[2] else "unknown",
                )
                for row in reminders_rows
            ]

            # Fetch positions
            positions_cursor = await conn.execute(
                """
                SELECT ticker, notional_dollars, entry_price, timestamp
                FROM positions WHERE user_id = ? ORDER BY timestamp DESC
                """,
                (user_id,),
            )
            positions_rows = await positions_cursor.fetchall()
            positions = [
                UserPosition(
                    ticker=str(row[0]),
                    notional_dollars=int(row[1]),
                    entry_price=float(row[2]),
                    timestamp=str(row[3]),
                )
                for row in positions_rows
            ]

        voice = await self.voicechat_db.get_user_voice_stats(user_id)

        return UserDataReport(
            user_id=user_id,
            guilds=guilds,
            invites=invites,
            reminders=reminders,
            positions=positions,
            voice=voice,
        )

    async def get_user_guild_ids(self, user_id: UserId) -> list[int]:
        """Fetch all guild IDs where a user has a record (for mod_log notification lookup)."""
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                "SELECT DISTINCT guild_id FROM users WHERE discord_id = ?",
                (user_id,),
            )
            rows = await cursor.fetchall()
        return [int(row[0]) for row in rows]

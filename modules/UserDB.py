"""Manage user-specific data and preferences in the database.

The `users` table is the single source of truth for all user-specific data,
including stats (currency, bumps, XP), preferences (reminders, opt-outs),
and state (daily claims, activity). The schema is defined with strict
constraints and generated columns to enforce data integrity.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # Added for timezone support

from modules.CurrencyLedgerDB import COLLATERAL_POOL_ID, SYSTEM_USER_ID
from modules.dtypes import (
    GuildId,
    NonNegativeInt,
    PositiveInt,
    ReminderPreference,
    UserGuildPair,
    UserId,
)
from modules.enums import StatName

if TYPE_CHECKING:
    from modules.CurrencyLedgerDB import CurrencyLedgerDB, EventReason
    from modules.Database import Database


# False S608: CURRENCY_TABLE is a constant, not user input. And stat.value is enum.
class UserDB:
    USERS_TABLE: ClassVar[str] = "users"

    def __init__(self, database: Database) -> None:
        self.database = database
        self.log = logging.getLogger(__name__)

    async def post_init(self) -> None:
        """Initialize the database table for users."""
        async with self.database.get_conn() as conn:
            # CHECK > 1000000 for snowflakes
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.USERS_TABLE} (
                    -- Core Identity
                    discord_id                  INTEGER NOT NULL CHECK(discord_id > 1000000 AND
                                                    discord_id < 10000000000000000000),
                    guild_id                    INTEGER NOT NULL CHECK(guild_id > 1000000 AND
                                                    guild_id < 10000000000000000000),

                    -- Stats (from former user_stats table)
                    currency                    INTEGER NOT NULL DEFAULT 0 CHECK(currency >= 0),
                    bumps                       INTEGER NOT NULL DEFAULT 0 CHECK(bumps >= 0),
                    xp                          INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),

                    -- Generated Level Column
                    level                       INTEGER GENERATED ALWAYS AS
                                                (CAST(floor(pow(max(xp - 6, 0), 1.0/2.5)) AS INTEGER))
                    STORED,

                    -- Preferences & State
                    last_active_timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
                    daily_reminder_preference   TEXT NOT NULL DEFAULT 'NEVER'
                    CHECK(daily_reminder_preference IN ('NEVER', 'ONCE', 'ALWAYS')),

                    has_claimed_daily           INTEGER NOT NULL DEFAULT 0 CHECK(has_claimed_daily IN (0, 1)),
                    native_language             TEXT DEFAULT NULL,
                    autotranslate               INTEGER NOT NULL DEFAULT 0 CHECK(autotranslate IN (0, 1)),
                    timezone                    TEXT DEFAULT 'UTC',

                    -- Keys & Constraints
                    PRIMARY KEY (discord_id, guild_id)
                ) STRICT;
                """,
            )
            # Add an index for guild_id and last_active_timestamp to speed up activity queries.
            await conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_users_activity ON {self.USERS_TABLE}(guild_id, last_active_timestamp);
                """,
            )
            # Create a partial index to optimize fetching users who need a daily reminder.
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_users_pending_reminders
                ON users(guild_id)
                WHERE daily_reminder_preference IN ('ALWAYS', 'ONCE');
                """,
            )
            # Invariant: Bumps are append-only.
            await conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS prevent_bump_decrement
                BEFORE UPDATE ON {self.USERS_TABLE}
                WHEN NEW.bumps < OLD.bumps
                BEGIN
                    SELECT RAISE(ABORT, 'Bump count cannot be decreased');
                END;
                """,
            )
            # Create a view for user stats to abstract the underlying table.
            await conn.execute(
                """
                CREATE VIEW IF NOT EXISTS v_user_stats AS
                SELECT
                    discord_id,
                    guild_id,
                    currency,
                    bumps,
                    xp,
                    level
                FROM users;
                """,
            )
            await conn.commit()

    async def update_last_message(self, user_id: UserId, guild_id: GuildId) -> None:
        """Update the timestamp of the last message for a user."""
        async with self.database.get_conn() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.USERS_TABLE} (discord_id, guild_id)
                VALUES (?, ?)
                ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                last_active_timestamp = strftime('%Y-%m-%d %H:%M:%S', 'now')
                """,  # noqa: S608
                (user_id, guild_id),
            )
            await conn.commit()

    async def set_daily_reminder_preference(
        self,
        user_id: UserId,
        preference: ReminderPreference,
        guild_id: GuildId,
    ) -> None:
        """Set the daily reminder preference ('ONCE', 'ALWAYS', 'NEVER') for a user."""
        async with self.database.get_conn() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, daily_reminder_preference)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                daily_reminder_preference = excluded.daily_reminder_preference,
                last_active_timestamp = excluded.last_active_timestamp
                """,  # noqa: S608
                (user_id, guild_id, preference),
            )
            await conn.commit()

    async def get_active_users(self, guild_id: GuildId, days: int) -> list[UserId]:
        """Get a list of user IDs that have been active within a specified number of days."""
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT discord_id FROM {self.USERS_TABLE}
                WHERE guild_id = ? AND julianday('now') - julianday(last_active_timestamp) <= ?
                """,  # noqa: S608
                (guild_id, days),
            )
            active_users = await cursor.fetchall()
        return [UserId(row[0]) for row in active_users]

    async def get_inactive_users(self, guild_id: GuildId, days: int) -> list[UserId]:
        """Get a list of user IDs that have been inactive for more than a specified number of days."""
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT discord_id FROM {self.USERS_TABLE}
                WHERE guild_id = ? AND julianday('now') - julianday(last_active_timestamp) > ?
                """,  # noqa: S608
                (guild_id, days),
            )
            inactive_users = await cursor.fetchall()
        return [UserId(row[0]) for row in inactive_users]

    async def update_active_users(self, user_guild_pairs: list[UserGuildPair]) -> None:
        """Bulk update the last active timestamp for a list of users."""
        if not user_guild_pairs:
            return

        async with self.database.get_conn() as conn:
            await conn.executemany(
                f"""
                INSERT INTO {self.USERS_TABLE} (discord_id, guild_id) VALUES (?, ?)
                ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                    last_active_timestamp = strftime('%Y-%m-%d %H:%M:%S', 'now')
                """,  # noqa: S608
                user_guild_pairs,
            )
            await conn.commit()

    async def attempt_daily_claim(self, user_id: UserId, guild_id: GuildId) -> bool:
        """Atomically attempt to claim a daily reward for a user.

        This method ensures a user is in the database and then tries to update
        their `has_claimed_daily` status from 0 to 1. This is done in a single
        transaction to prevent race conditions.

        Returns
        -------
            bool: True if the claim was successful, False if already claimed.

        """
        async with self.database.get_conn() as conn:
            # This single atomic operation ensures the user exists, then attempts
            # to update the claim status only if `has_claimed_daily` is 0.
            cursor = await conn.execute(
                f"""
                INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, has_claimed_daily) VALUES (?, ?, 1)
                ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                    has_claimed_daily = 1
                WHERE {self.USERS_TABLE}.has_claimed_daily = 0
                """,  # noqa: S608
                (user_id, guild_id),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def process_daily_reset_for_guild(self, guild_id: GuildId) -> list[UserId]:
        """Atomically reset all daily claims and fetch users who need a reminder.

        This is currently dead code but remains separate to leave room for different timezones per server.

        This single transaction performs three actions:
        1. Fetches all users who have not claimed their daily and have reminders enabled.
        2. Resets `has_claimed_daily` to 0 for ALL users.
        3. Resets `daily_reminder_preference` to 'NEVER' for users who had it set to 'ONCE'.

        Returns
        -------
            A list of user IDs to be reminded.

        """
        async with self.database.get_conn() as conn:
            # 1. Fetch users who need a reminder BEFORE resetting claims.
            cursor = await conn.execute(
                f"""
                SELECT discord_id FROM {self.USERS_TABLE}
                WHERE guild_id = ? AND daily_reminder_preference IN ('ALWAYS', 'ONCE')
                """,  # noqa: S608
                (guild_id,),
            )
            user_ids_to_remind = [UserId(row[0]) for row in await cursor.fetchall()]

            # 2. Atomically reset daily claims and 'ONCE' preferences for all users.
            await conn.execute(
                f"""
                UPDATE {self.USERS_TABLE} SET
                    has_claimed_daily = 0,
                    daily_reminder_preference = CASE
                        WHEN daily_reminder_preference = 'ONCE' THEN 'NEVER'
                        ELSE daily_reminder_preference END
                WHERE guild_id = ?
                """,  # noqa: S608
                (guild_id,),
            )
            await conn.commit()
            return user_ids_to_remind

    async def process_daily_reset_all(self) -> list[UserId]:
        """Atomically reset all daily claims across all guilds and fetch users who need a reminder.

        This single transaction performs three actions for the entire database:
        1. Fetches all users who have not claimed their daily and have reminders enabled.
        2. Resets `has_claimed_daily` to 0 for ALL users.
        3. Resets `daily_reminder_preference` to 'NEVER' for users who had it set to 'ONCE'.

        Returns
        -------
            A list of user IDs to be reminded.

        """
        async with self.database.get_conn() as conn:
            # 1. Fetch all users who need a reminder from any guild.
            cursor = await conn.execute(
                f"""
                SELECT DISTINCT discord_id FROM {self.USERS_TABLE}
                WHERE daily_reminder_preference IN ('ALWAYS', 'ONCE')
                """,  # noqa: S608
            )
            user_ids_to_remind = [UserId(row[0]) for row in await cursor.fetchall()]

            # 2. Atomically reset daily claims and 'ONCE' preferences for all users.
            await conn.execute(
                f"""
                UPDATE {self.USERS_TABLE} SET
                    has_claimed_daily = 0,
                    daily_reminder_preference = CASE
                        WHEN daily_reminder_preference = 'ONCE' THEN 'NEVER'
                        ELSE daily_reminder_preference END
                """,  # noqa: S608
            )
            await conn.commit()
            return user_ids_to_remind

    async def mint_currency(
        self,
        user_id: UserId,
        guild_id: GuildId,
        amount: PositiveInt,
        event_reason: EventReason,
        ledger_db: CurrencyLedgerDB,
        initiator_id: UserId | None = None,
    ) -> NonNegativeInt:
        """Atomically increment a user's currency and logs it as a MINT event."""
        sql = f"""
            INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, currency)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                currency = currency + excluded.currency
            RETURNING currency
        """  # noqa: S608
        async with self.database.get_conn() as conn:
            try:
                cursor = await conn.execute(sql, (user_id, guild_id, amount))
                new_value_row = await cursor.fetchone()
                await ledger_db.log_event(
                    conn=conn,
                    guild_id=guild_id,
                    event_type="MINT",
                    event_reason=event_reason,
                    sender_id=SYSTEM_USER_ID,
                    receiver_id=user_id,
                    amount=amount,
                    initiator_id=initiator_id or user_id,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                self.log.exception(
                    "Currency minting failed and was rolled back for user %s",
                    user_id,
                )
                # Re-raise or return a failure indicator
                raise
            return NonNegativeInt(int(new_value_row[0]) if new_value_row else 0)

    async def burn_currency(
        self,
        user_id: UserId,
        guild_id: GuildId,
        amount: PositiveInt,
        event_reason: EventReason,
        ledger_db: CurrencyLedgerDB,
        initiator_id: UserId,
    ) -> int | None:
        """Atomically decrement a user's currency if they have sufficient funds.

        Logs it as a BURN event. Returns the new balance or None on failure.
        """
        sql = f"""
            UPDATE {self.USERS_TABLE}
            SET currency = currency - ?
            WHERE discord_id = ? AND guild_id = ? AND currency >= ?
            RETURNING currency
        """  # noqa: S608
        async with self.database.get_conn() as conn:
            try:
                cursor = await conn.execute(sql, (amount, user_id, guild_id, amount))
                new_value_row = await cursor.fetchone()

                if new_value_row is None:
                    # Insufficient funds, or user not found. Rollback.
                    await conn.rollback()
                    return None

                await ledger_db.log_event(
                    conn=conn,
                    guild_id=guild_id,
                    event_type="BURN",
                    event_reason=event_reason,
                    sender_id=user_id,
                    receiver_id=SYSTEM_USER_ID,  # Money goes to the void
                    amount=amount,
                    initiator_id=initiator_id,
                )

                await conn.commit()
                return int(new_value_row[0])

            except Exception:
                await conn.rollback()
                self.log.exception(
                    "Currency burning failed and was rolled back for user %s",
                    user_id,
                )
                raise

    async def set_currency_balance_and_log(
        self,
        user_id: UserId,
        guild_id: GuildId,
        new_balance: NonNegativeInt,
        event_reason: EventReason,  # e.g., "ADMIN_SET"
        ledger_db: CurrencyLedgerDB,
        initiator_id: UserId,
    ) -> None:
        """Atomically set a user's balance and logs the *delta* to the currency ledger as a MINT or BURN."""
        async with self.database.get_conn() as conn:
            try:
                # 1. Get current balance (or 0) inside the transaction
                cursor = await conn.execute(
                    f"SELECT currency FROM {self.USERS_TABLE} WHERE discord_id = ? AND guild_id = ?",  # noqa: S608
                    (user_id, guild_id),
                )
                row = await cursor.fetchone()
                current_balance = int(row[0]) if row else 0

                delta = new_balance - current_balance

                # 2. Update the user's balance in the cache
                await conn.execute(
                    f"""
                    INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, currency) VALUES (?, ?, ?)
                    ON CONFLICT(discord_id, guild_id) DO UPDATE SET currency = excluded.currency
                    """,  # noqa: S608
                    (user_id, guild_id, new_balance),
                )

                # 3. Log the delta to the ledger
                if delta > 0:
                    # This was a MINT
                    await ledger_db.log_event(
                        conn=conn,
                        guild_id=guild_id,
                        event_type="MINT",
                        event_reason=event_reason,
                        sender_id=SYSTEM_USER_ID,
                        receiver_id=user_id,
                        amount=delta,
                        initiator_id=initiator_id,
                    )
                elif delta < 0:
                    # This was a BURN
                    await ledger_db.log_event(
                        conn=conn,
                        guild_id=guild_id,
                        event_type="BURN",
                        event_reason=event_reason,
                        sender_id=user_id,
                        receiver_id=SYSTEM_USER_ID,
                        amount=abs(delta),
                        initiator_id=initiator_id,
                    )
                # if delta == 0, no change, nothing to log.

                await conn.commit()

            except Exception:
                await conn.rollback()
                self.log.exception(
                    "Setting currency balance failed and was rolled back for user %s",
                    user_id,
                )
                raise

    async def get_stat(
        self,
        user_id: UserId,
        guild_id: GuildId,
        stat: StatName,
    ) -> NonNegativeInt:
        """Get a single stat for a user, returning 0 if they don't exist."""
        async with self.database.get_cursor() as cursor:
            # The stat name is from an enum, so it's safe to use in an f-string.
            await cursor.execute(
                f"SELECT {stat.value} FROM {self.USERS_TABLE} WHERE discord_id = ? AND guild_id = ?",  # noqa: S608
                (user_id, guild_id),
            )
            result = await cursor.fetchone()
        return NonNegativeInt(int(result[0])) if result else NonNegativeInt(0)

    async def increment_stat(
        self,
        user_id: UserId,
        guild_id: GuildId,
        stat: StatName,
        amount: PositiveInt,
    ) -> NonNegativeInt:
        """Atomically increments a user's stat and returns the new value."""
        if stat == StatName.CURRENCY:
            msg = "Cannot use increment_stat for currency. Use mint_currency instead."
            raise PermissionError(msg)

        # stat.value is 'currency', 'bumps', or 'xp' which we safely use to build the query
        sql = f"""
            INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, {stat.value})
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                {stat.value} = {stat.value} + excluded.{stat.value}
            RETURNING {stat.value}
        """  # noqa: S608

        async with self.database.get_conn() as conn:
            cursor = await conn.execute(sql, (user_id, guild_id, amount))
            new_value_row = await cursor.fetchone()
            await conn.commit()

        return NonNegativeInt(int(new_value_row[0]) if new_value_row else 0)

    async def decrement_stat(
        self,
        user_id: UserId,
        guild_id: GuildId,
        stat: StatName,
        amount: PositiveInt,
    ) -> int | None:
        """Atomically decrements a user's stat if they have sufficient value."""
        if stat == StatName.CURRENCY:
            msg = "Cannot use decrement_stat for currency. Use burn_currency instead."
            raise PermissionError(msg)

        sql = f"""
            UPDATE {self.USERS_TABLE}
            SET {stat.value} = {stat.value} - ?
            WHERE discord_id = ? AND guild_id = ? AND {stat.value} >= ?
            RETURNING {stat.value}
        """  # noqa: S608

        async with self.database.get_conn() as conn:
            cursor = await conn.execute(sql, (amount, user_id, guild_id, amount))
            new_value_row = await cursor.fetchone()
            await conn.commit()

        return int(new_value_row[0]) if new_value_row else None

    async def set_stat(
        self,
        user_id: UserId,
        guild_id: GuildId,
        stat: StatName,
        value: int,
    ) -> None:
        """Atomically sets a user's stat to a specific value."""
        if stat == StatName.CURRENCY:
            msg = "Cannot use set_stat for currency. Use set_currency_balance_and_log instead."
            raise PermissionError(msg)

        sql = f"""
            INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, {stat.value})
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                {stat.value} = excluded.{stat.value}
        """  # noqa: S608
        async with self.database.get_conn() as conn:
            await conn.execute(sql, (user_id, guild_id, value))
            await conn.commit()

    async def transfer_currency(
        self,
        sender_id: UserId,
        receiver_id: UserId,
        guild_id: GuildId,
        amount: PositiveInt,
        ledger_db: CurrencyLedgerDB,
    ) -> bool:
        """Atomically transfers currency and logs the transaction."""
        async with self.database.get_conn() as conn:
            try:
                # 1. Check sender's balance and decrement in one atomic step
                cursor = await conn.execute(
                    f"""UPDATE {self.USERS_TABLE} SET currency = currency - ?
                    WHERE discord_id = ? AND guild_id = ? AND currency >= ?""",  # noqa: S608
                    (amount, sender_id, guild_id, amount),
                )
                if cursor.rowcount == 0:
                    return False  # Insufficient funds or user not found

                # 2. Increment receiver's balance (UPSERT to be safe)
                await conn.execute(
                    f"""
                    INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, currency) VALUES (?, ?, ?)
                    ON CONFLICT(discord_id, guild_id) DO UPDATE SET currency = currency + excluded.currency
                    """,  # noqa: S608
                    (receiver_id, guild_id, amount),
                )

                # 3. Log the transaction to the new ledger
                await ledger_db.log_event(
                    conn=conn,
                    guild_id=guild_id,
                    event_type="TRANSFER",
                    event_reason="P2P_TRANSFER",
                    sender_id=sender_id,
                    receiver_id=receiver_id,
                    amount=amount,
                    initiator_id=sender_id,
                )

                await conn.commit()
            except Exception:
                await conn.rollback()
                self.log.exception(
                    "Currency transfer failed and was rolled back. From %s to %s, amount %d",
                    sender_id,
                    receiver_id,
                    amount,
                )
                return False
            else:
                return True

    async def get_leaderboard(
        self,
        guild_id: GuildId,
        stat: StatName,
        limit: int = 10,
    ) -> list[tuple[int, UserId, int]]:
        """Retrieve the top users by a stat."""
        query_stat = stat.value

        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT
                    RANK() OVER (ORDER BY {query_stat} DESC) as rank,
                    discord_id,
                    {query_stat}
                FROM v_user_stats
                WHERE guild_id = ? AND {query_stat} > 0
                LIMIT ?
                """,  # noqa: S608
                (guild_id, limit),
            )
            rows = await cursor.fetchall()
            return [(row[0], UserId(row[1]), row[2]) for row in rows]

    async def get_level_and_xp(
        self,
        user_id: UserId,
        guild_id: GuildId,
    ) -> tuple[int, int] | None:
        """Fetch the level and XP for a user."""
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                f"SELECT level, xp FROM {self.USERS_TABLE} WHERE discord_id = ? AND guild_id = ?",  # noqa: S608
                (user_id, guild_id),
            )
            return await cursor.fetchone()

    async def apply_wealth_tax(
        self,
        guild_id: GuildId,
        exponent: float,
        ledger_db: CurrencyLedgerDB,
        initiator_id: UserId,
    ) -> tuple[int, int]:
        """Apply a progressive wealth tax (val^x) to all users' cash and stock collateral.

        Returns:
            tuple[int, int]: (count_of_users_affected, total_amount_burned)

        """
        total_burned = 0
        affected_users = set()
        ledger_events = []

        # We will perform updates in batches
        cash_updates = []
        stock_updates = []

        async with self.database.get_conn() as conn:
            # 1. Calculate Tax on Cash
            async with conn.execute(
                f"SELECT discord_id, currency FROM {self.USERS_TABLE} WHERE guild_id = ? AND currency > 10",  # noqa: S608
                (guild_id,),
            ) as cursor:
                rows = await cursor.fetchall()

            for uid, old_balance in rows:
                new_balance = math.ceil(pow(old_balance, exponent))
                tax = old_balance - new_balance

                if tax > 0:
                    total_burned += tax
                    affected_users.add(UserId(uid))
                    cash_updates.append((new_balance, uid, guild_id))
                    ledger_events.append(
                        (
                            guild_id,
                            "BURN",
                            "WEALTH_TAX",
                            uid,
                            SYSTEM_USER_ID,
                            tax,
                            initiator_id,
                        ),
                    )

            # 2. Calculate Tax on Stocks
            async with conn.execute(
                """SELECT position_id, user_id, collateral_dollars,
                notional_dollars FROM positions WHERE guild_id = ? AND collateral_dollars > 10""",
                (guild_id,),
            ) as cursor:
                rows = await cursor.fetchall()

            for pos_id, uid, old_collat, old_notional in rows:
                new_collat = math.ceil(pow(old_collat, exponent))
                tax = old_collat - new_collat

                if tax > 0:
                    # Calculate new notional to maintain leverage ratio
                    ratio = new_collat / old_collat
                    new_notional = int(old_notional * ratio)

                    # Safety check: Prevent calculation from rounding to 0
                    if abs(new_notional) < 1:
                        continue

                    total_burned += tax
                    affected_users.add(UserId(uid))
                    stock_updates.append((new_collat, new_notional, pos_id))

                    ledger_events.append(
                        (
                            guild_id,
                            "BURN",
                            "WEALTH_TAX_COLLATERAL",
                            COLLATERAL_POOL_ID,
                            SYSTEM_USER_ID,
                            tax,
                            initiator_id,
                        ),
                    )

            # 3. Execute Updates
            if cash_updates:
                await conn.executemany(
                    f"UPDATE {self.USERS_TABLE} SET currency = ? WHERE discord_id = ? AND guild_id = ?",  # noqa: S608
                    cash_updates,
                )

            if stock_updates:
                await conn.executemany(
                    "UPDATE positions SET collateral_dollars = ?, notional_dollars = ? WHERE position_id = ?",
                    stock_updates,
                )

            # 4. Log Events
            if ledger_events:
                await ledger_db.bulk_log_event(conn, ledger_events)

            await conn.commit()

        return len(affected_users), total_burned

    async def set_native_language(
        self,
        user_id: UserId,
        guild_id: GuildId,
        language: str | None,
    ) -> None:
        """Set the user's native language for auto-translation."""
        async with self.database.get_conn() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, native_language)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                    native_language = excluded.native_language
                """,  # noqa: S608
                (user_id, guild_id, language),
            )
            await conn.commit()

    async def get_native_language(
        self,
        user_id: UserId,
        guild_id: GuildId,
    ) -> str | None:
        """Get the user's native language preference."""
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                f"SELECT native_language FROM {self.USERS_TABLE} WHERE discord_id = ? AND guild_id = ?",  # noqa: S608
                (user_id, guild_id),
            )
            result = await cursor.fetchone()
            # result[0] is the language code string
            return result[0] if result else None

    async def get_timezone(self, user_id: UserId, guild_id: GuildId) -> ZoneInfo:
        """Fetch the user's timezone, defaulting to UTC if not set."""
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                f"SELECT timezone FROM {self.USERS_TABLE} WHERE discord_id = ? AND guild_id = ?",  # noqa: S608
                (user_id, guild_id),
            )
            row = await cursor.fetchone()

        if row and row[0]:
            try:
                return ZoneInfo(row[0])
            except ZoneInfoNotFoundError, ValueError:
                pass  # Fallback to UTC on bad data
        return ZoneInfo("UTC")

    async def set_timezone(self, user_id: UserId, guild_id: GuildId, tz_name: str) -> bool:
        """Set the user's timezone. Returns False if the timezone is invalid."""
        try:
            ZoneInfo(tz_name)  # Validate
        except ZoneInfoNotFoundError:
            return False

        async with self.database.get_conn() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, timezone)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                    timezone = excluded.timezone
                """,  # noqa: S608
                (user_id, guild_id, tz_name),
            )
            await conn.commit()
        return True

    async def set_autotranslate(
        self,
        user_id: UserId,
        guild_id: GuildId,
        enabled: bool,
    ) -> None:
        """Set the user's autotranslate preference (opt-in)."""
        value = 1 if enabled else 0
        async with self.database.get_conn() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.USERS_TABLE} (discord_id, guild_id, autotranslate)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                    autotranslate = excluded.autotranslate
                """,  # noqa: S608
                (user_id, guild_id, value),
            )
            await conn.commit()

    async def get_autotranslate(
        self,
        user_id: UserId,
        guild_id: GuildId,
    ) -> bool:
        """Check if the user has opted in to autotranslate."""
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                f"SELECT autotranslate FROM {self.USERS_TABLE} WHERE discord_id = ? AND guild_id = ?",  # noqa: S608
                (user_id, guild_id),
            )
            result = await cursor.fetchone()
            # result[0] is 1 or 0
            return bool(result[0]) if result else False

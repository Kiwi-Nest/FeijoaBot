"""Backend for handling trades.

There's an intentional design decision.
UserDB manages currency in dollars. But stock prices are in cents.
We restrict stock quantities to two decimal places.
And the rounding error of quantity * stock price is the slippage.
We communicate this slippage as a "transaction fee".
(While misleading it doubles as a subtle money sink.)

There's also a known issue where someone can observe
price changes IRL and then trade on the stale price.
But this is necessary due to heavy API rate limits.
Responsibility of guild owner to moderate abuse.
"""

from __future__ import annotations  # Defer type annotation evaluation

import asyncio
import datetime
import logging
import math
from typing import TYPE_CHECKING, Any, Final, Literal, cast

# --- Local Imports ---
if TYPE_CHECKING:
    import aiohttp
    import aiosqlite

    from modules.CurrencyLedgerDB import CurrencyLedgerDB
    from modules.Database import Database
    from modules.dtypes import GuildId, PositiveInt, UserId
    from modules.UserDB import UserDB

# Import new exceptions
from modules.aio_twelvedata import AioTwelveDataClient, AioTwelveDataError, AioTwelveDataRequestError, Ticker
from modules.CurrencyLedgerDB import COLLATERAL_POOL_ID, SYSTEM_USER_ID
from modules.enums import StatName
from modules.exceptions import UserError

# --- Logging ---
log = logging.getLogger(__name__)


# --- Constants ---
# Tier 1: Only allowed stocks given rate limit
ALLOWED_STOCKS: Final[set[Ticker]] = {
    "TQQQ",
    "TNA",
    "SOXL",
    "FAZ",
    "TMF",
    "UGL",
    "BITX",
}
# Time-To-Live for cached prices in seconds
CACHE_TTL: Final[int] = 300  # 5 minutes


def _parse_api_time(time_str: str) -> datetime.timedelta | None:
    """Parse 'HH:MM:SS' duration string into a timedelta."""
    if not time_str or time_str == "00:00:00":
        return None

    try:
        # Split the string into components
        parts = time_str.split(":")
        if len(parts) != 3:
            msg = "Time string not in HH:MM:SS format"
            raise ValueError(msg)

        # Manually parse hours, minutes, and seconds as integers
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])

        # Create a timedelta directly from the duration components
        return datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds)
    except ValueError, TypeError:
        # This catches format mismatches or non-integer parts
        log.warning("Could not parse API time string: %s", time_str)
        return None


class PriceCache:
    """Manage fetching and caching of stock prices on-demand.

    Using an intelligent, market-aware TTL.
    """

    def __init__(self, api_client: AioTwelveDataClient) -> None:
        self.api_client = api_client
        self._lock = asyncio.Lock()  # Protects against simultaneous refreshes
        # Store prices as Decimals
        self._prices: dict[Ticker, float] = {}
        self._timestamps: dict[Ticker, datetime.datetime] = {}

        # Refresh with an "intelligent TTL"
        self._next_check_time = datetime.datetime.min.replace(tzinfo=datetime.UTC)

        # Caches the *actual* market state from the API
        self._market_state: dict[str, Any] | None = None
        self._next_market_open: datetime.datetime | None = None

    def is_us_market_open(self) -> bool:
        """Check the *cached* market state. This is a synchronous, 0-cost check of the last known state."""
        if not self._market_state:
            return False  # Default to closed if we've never checked
        return self._market_state.get("is_market_open", False)

    async def get_fresh_prices(self) -> dict[Ticker, float]:  # noqa: PLR0912 PLR0915
        """Return a dictionary of fresh prices.

        Refreshing from the API *only* if the intelligent TTL has expired.
        """
        async with self._lock:
            now_utc = datetime.datetime.now(datetime.UTC)
            is_empty = not self._prices

            # 1. Fast Path: Serve cache if our intelligent TTL hasn't expired
            if now_utc < self._next_check_time and not is_empty:
                log.debug(
                    "Serving cached prices; next check scheduled for %s",
                    self._next_check_time,
                )
                return self._prices

            # 2. Slow Path: Cache is stale or empty. We MUST check market state.
            log.info(
                "Triggering cache refresh. Reason: %s",
                "Cache is empty" if is_empty else "Intelligent TTL expired",
            )

            # --- API Call 1: Market State (Uses 1 API credit) ---
            market_state = await self.api_client.get_market_state("NASDAQ")

            if market_state is None:
                # API call failed.
                log.error("Failed to fetch market state. Cannot refresh prices.")
                # Set a short retry window
                self._next_check_time = now_utc + datetime.timedelta(minutes=1)
                if is_empty:
                    msg = "Failed to fetch initial market state. Bot cannot get prices."
                    raise ConnectionError(msg)
                # Serve stale data as a fallback
                log.warning("Serving stale prices due to market state API failure.")
                return self._prices

            # Save the new state
            self._market_state = market_state
            is_open = self._market_state.get("is_market_open", False)

            # 3. Decide action based on market state
            if is_open:
                # --- MARKET IS OPEN ---
                self._next_market_open = None
                # We must refresh prices
                log.info("Market is OPEN. Refreshing batch prices.")
                # --- API Call 2: Batch Prices (Uses 7 API credits) ---
                try:
                    price_map = await self.api_client.get_batch_prices(ALLOWED_STOCKS)
                    update_time = datetime.datetime.now(datetime.UTC)

                    for ticker, price_float in price_map.items():
                        if price_float is not None:
                            self._prices[ticker.upper()] = price_float
                            self._timestamps[ticker.upper()] = update_time
                    log.info("Full batch refresh complete.")

                    # Set the standard 5-minute TTL
                    self._next_check_time = update_time + datetime.timedelta(
                        seconds=CACHE_TTL,
                    )

                except (AioTwelveDataError, AioTwelveDataRequestError) as e:
                    log.exception("Batch refresh API error")
                    # Set a short retry window
                    self._next_check_time = now_utc + datetime.timedelta(minutes=1)
                    if is_empty:
                        msg = "Could not refresh prices. The data provider may be unavailable."
                        raise ConnectionError(msg) from e
                    log.warning("Serving stale prices due to batch price API error.")

            else:
                # --- MARKET IS CLOSED ---
                log.info("Market is CLOSED. Serving existing prices.")

                # If cache is empty (cold start), we must fetch prices once
                if is_empty:
                    log.info(
                        "Cold start: Fetching initial prices while market is closed.",
                    )
                    try:
                        # --- API Call 2 (Cold Start): Batch Prices (7 credits) ---
                        price_map = await self.api_client.get_batch_prices(
                            ALLOWED_STOCKS,
                        )
                        update_time = datetime.datetime.now(datetime.UTC)
                        for ticker, price_float in price_map.items():
                            if price_float is not None:
                                self._prices[ticker.upper()] = price_float
                                self._timestamps[ticker.upper()] = update_time
                    except Exception:
                        log.exception(
                            "Failed to get *initial* prices while market closed.",
                        )
                        # Set a short retry and return empty dict
                        self._next_check_time = now_utc + datetime.timedelta(minutes=1)
                        return self._prices  # Returns empty {}

                # Now, set the "intelligent TTL"
                time_to_open_str = cast("str", self._market_state.get("time_to_open"))
                time_to_open_delta = _parse_api_time(time_to_open_str)

                if time_to_open_delta:
                    # We have a valid time! Schedule the next check.
                    buffer = datetime.timedelta(seconds=15)

                    # Store the *actual* open time
                    actual_open_time = now_utc + time_to_open_delta
                    self._next_market_open = actual_open_time
                    self._next_check_time = actual_open_time + buffer  # Use the buffered time for the check
                    log.info(
                        "Next market check scheduled for %s (in %s)",
                        self._next_check_time,
                        time_to_open_delta + buffer,
                    )
                else:
                    # API gave no 'time_to_open' (e.g., "00:00:00" on a weekend)
                    self._next_market_open = None
                    # Fall back to a long poll (e.g., 1 hour).
                    log.warning(
                        "No 'time_to_open' provided. Falling back to 1-hour poll.",
                    )
                    self._next_check_time = now_utc + datetime.timedelta(hours=1)

            # 4. Always return the current cache state
            return self._prices

    def get_cached_price(
        self,
        ticker: Ticker,
    ) -> tuple[float, datetime.datetime] | tuple[None, None]:
        """Get a single price and its timestamp directly from the cache."""
        price = self._prices.get(ticker)
        timestamp = self._timestamps.get(ticker)

        # Ensure we only return (float, datetime) or (None, None)
        if price is not None and timestamp is not None:
            return price, timestamp
        return None, None

    def get_next_market_open_time(self) -> datetime.datetime | None:
        """Get the cached next market open time, if available."""
        return self._next_market_open


# --- Custom Exceptions (can be shared or defined here) ---


class InsufficientFundsError(UserError):
    """Raised when a user doesn't have enough cash for a transaction."""


class PortfolioNotFoundError(UserError):
    """Raised when a user's portfolio doesn't exist yet."""


class PriceNotAvailableError(UserError):
    """Raised when a known ticker's price is not in the cache (e.g., pending update)."""


# --- Middleware Class ---
class TradingLogic:
    """Handle interactions with the trading data API and database.

    Manage a local cache of prices and a set of known tickers to
    respect API rate limits.
    """

    def __init__(
        self,
        database: Database,
        user_db: UserDB,
        ledger_db: CurrencyLedgerDB,
        api_key: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self.database = database
        self.user_db = user_db
        self.api_client = AioTwelveDataClient(api_key=api_key, session=session)
        self.ledger_db = ledger_db
        self.price_cache = PriceCache(self.api_client)
        log.info("TradingLogic initialized with PriceCache and AioTwelveDataClient.")

    async def post_init(self) -> None:
        """Initialize the database table for portfolios."""
        async with self.database.get_conn() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    position_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER NOT NULL CHECK(user_id > 1000000 AND user_id < 10000000000000000000),
                    guild_id        INTEGER NOT NULL CHECK(guild_id > 1000000 AND guild_id < 10000000000000000000),
                    ticker          TEXT NOT NULL,
                    notional_dollars INTEGER NOT NULL CHECK(notional_dollars != 0),
                    collateral_dollars INTEGER NOT NULL CHECK(collateral_dollars > 0),
                    entry_price     REAL NOT NULL CHECK(entry_price > 0),    -- Price at open
                    timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
                    FOREIGN KEY (user_id, guild_id) REFERENCES users(discord_id, guild_id)
                ) STRICT;
                """,
            )
            # Add an index for guild_id and last_active_timestamp to speed up activity queries.
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_positions_user ON positions (user_id, guild_id);
                """,
            )
            await conn.commit()
            log.info("Initialized positions database table.")

    async def start(self) -> None:
        """Start the backend services."""
        await self.api_client.__aenter__()
        log.info("TradingLogic backend started.")

    async def close(self) -> None:
        """Close the backend services."""
        await self.api_client.__aexit__(None, None, None)
        log.info("TradingLogic backend closed.")

    async def _update_cash_balance(
        self,
        conn: aiosqlite.Connection,  # The transaction connection
        user_id: UserId,
        guild_id: GuildId,
        cash_change_int: int,
    ) -> int:
        """Atomically update a user's cash balance.

        Raises InsufficientFundsError if a debit fails.
        Returns the new cash balance (as a Decimal).
        """
        if cash_change_int > 0:
            # A. Credit Cash (Safe)
            await conn.execute(
                f"""INSERT INTO {self.user_db.USERS_TABLE} (discord_id, guild_id, currency) VALUES (?, ?, ?)
                     ON CONFLICT(discord_id, guild_id) DO UPDATE SET currency = currency + ?""",  # noqa: S608
                (user_id, guild_id, cash_change_int, cash_change_int),
            )
        elif cash_change_int < 0:
            # B. Debit Cash (Atomic Check)
            debit_amount = abs(cash_change_int)
            cursor = await conn.execute(
                f"""UPDATE {self.user_db.USERS_TABLE}
                     SET currency = currency - ?
                     WHERE discord_id = ? AND guild_id = ? AND currency >= ?""",  # noqa: S608
                (debit_amount, user_id, guild_id, debit_amount),
            )
            if cursor.rowcount == 0:
                msg = f"Insufficient funds. You need ${debit_amount} to execute this trade."
                raise InsufficientFundsError(msg)

        # C. Get the new balance *after* the update
        cursor = await conn.execute(
            f"SELECT currency FROM {self.user_db.USERS_TABLE} WHERE discord_id = ? AND guild_id = ?",  # noqa: S608
            (user_id, guild_id),
        )
        return (await cursor.fetchone())[0]

    async def open_position(
        self,
        user_id: UserId,
        guild_id: GuildId,
        ticker: Ticker,
        trade_type: Literal["BUY", "SHORT"],  # Literal for specific string values
        dollar_amount: PositiveInt,
        leverage: PositiveInt,
    ) -> tuple[float, int, str, datetime.datetime, bool, int]:
        """Open a new long or short position (a new 'lot').

        This is a 'lossless' open, where the user's bank is debited
        by exactly the amount they specified.

        Returns:
            A tuple of (filled_price, notional_amount_trade, action_string,
            timestamp, was_stacked, total_notional_in_position).

        """
        ticker = ticker.upper()

        # 1. Get Price
        await self.price_cache.get_fresh_prices()  # Ensures cache is fresh

        # Get price and timestamp *from the cache*
        current_price, timestamp = self.price_cache.get_cached_price(ticker)

        # Check both price and timestamp
        if current_price is None or float(current_price) <= 0 or timestamp is None:
            msg = f"Trade rejected: {ticker} is not a supported trading symbol or has no price."
            raise PriceNotAvailableError(msg)

        # From here, current_price is a float and timestamp is a datetime.datetime
        current_price_float = float(current_price)

        # 2. Determine Position Details
        if trade_type == "BUY":
            notional_dollars_int = dollar_amount * leverage
            collateral_dollars_int = dollar_amount
            cash_change_int = -dollar_amount  # Debit user
            action = "BOUGHT"
        else:  # "SHORT"
            notional_dollars_int = -(dollar_amount * leverage)
            collateral_dollars_int = dollar_amount  # Collateral is always a positive debit
            cash_change_int = -dollar_amount  # Debit for collateral
            action = "SHORTED"

        async with self.database.get_conn() as conn:
            try:
                # 1. Update Cash (raises InsufficientFundsError)
                await self._update_cash_balance(
                    conn,
                    user_id,
                    guild_id,
                    cash_change_int,
                )

                # 2. Log the collateral transfer to the ledger
                await self.ledger_db.log_event(
                    conn=conn,
                    guild_id=guild_id,
                    event_type="TRANSFER",
                    event_reason="TRADE_OPEN_COLLATERAL",
                    sender_id=user_id,
                    receiver_id=COLLATERAL_POOL_ID,
                    amount=dollar_amount,
                    initiator_id=user_id,
                    reference_id=f"TICKER:{ticker.upper()}",  # Placeholder, since position_id doesn't exist yet
                )

                # 2. Check for a stackable position OF THE SAME TYPE
                # We must only stack longs on longs (notional_dollars > 0)
                # and shorts on shorts (notional_dollars < 0)
                stack_direction_check = "notional_dollars > 0" if trade_type == "BUY" else "notional_dollars < 0"

                cursor = await conn.execute(
                    f"""SELECT position_id, notional_dollars, collateral_dollars FROM positions
                        WHERE user_id = ? AND guild_id = ? AND ticker = ?
                        AND entry_price = ? AND {stack_direction_check}""",  # noqa: S608 (f-string is safe)
                    (user_id, guild_id, ticker, current_price_float),
                )
                existing_position = await cursor.fetchone()

                was_stacked = False
                total_notional_in_position = 0

                if existing_position:
                    # Stack the position
                    was_stacked = True
                    position_id, existing_notional, existing_collateral = existing_position
                    new_notional = existing_notional + notional_dollars_int
                    new_collateral = existing_collateral + collateral_dollars_int
                    total_notional_in_position = new_notional

                    await conn.execute(
                        "UPDATE positions SET notional_dollars = ?, collateral_dollars = ? WHERE position_id = ?",
                        (new_notional, new_collateral, position_id),
                    )
                else:
                    # Create a new position lot
                    was_stacked = False
                    total_notional_in_position = notional_dollars_int
                    await conn.execute(
                        """
                        INSERT INTO positions (user_id, guild_id, ticker, notional_dollars, collateral_dollars, entry_price)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            guild_id,
                            ticker,
                            notional_dollars_int,
                            collateral_dollars_int,
                            current_price_float,
                        ),
                    )

                # 3. Commit
                await conn.commit()

                log.info(
                    "Position %s for user %s: %s $%s of %s @ $%s",
                    "stacked" if was_stacked else "opened",
                    user_id,
                    action,
                    dollar_amount,
                    ticker,
                    current_price_float,
                )

            except Exception:
                await conn.rollback()
                log.exception(
                    "Position open failed and was rolled back for user %s",
                    user_id,
                )
                raise
            else:
                return (
                    current_price_float,
                    notional_dollars_int,
                    action,
                    timestamp,
                    was_stacked,
                    total_notional_in_position,
                )

    async def close_position(  # noqa: PLR0915
        self,
        user_id: UserId,
        guild_id: GuildId,
        position_id: int,
        close_amount: PositiveInt | None = None,
    ) -> tuple[str, float, float, int, int, bool]:
        """Close a position ('lot') fully.

        This is a 'lossy' transaction where the slippage fee is realized
        by flooring the final credit.

        Returns
        -------
            Tuple of (ticker, pnl_precise, total_credit_precise, final_credit_int, collateral_dollars_to_close, is_partial_close)

        """
        async with self.database.get_conn() as conn:
            try:
                # 1. Get the position
                cursor = await conn.execute(
                    """
                    SELECT ticker, notional_dollars, collateral_dollars, entry_price
                    FROM positions
                    WHERE position_id = ? AND user_id = ? AND guild_id = ?
                    """,
                    (position_id, user_id, guild_id),
                )
                row = await cursor.fetchone()

                if not row:
                    msg = f"Position ID {position_id} not found or does not belong to you."
                    raise PortfolioNotFoundError(msg)

                (
                    ticker,
                    notional_dollars_total,
                    collateral_dollars_total,
                    entry_price,
                ) = row
                ticker = str(ticker)
                notional_dollars_total = int(notional_dollars_total)
                collateral_dollars_total = int(collateral_dollars_total)
                entry_price = float(entry_price)

                # 2. Determine Amount to Close
                # Default to a full close
                proportion_to_close = 1.0
                is_partial_close = False

                if close_amount is not None and close_amount < collateral_dollars_total:
                    is_partial_close = True
                    proportion_to_close = close_amount / collateral_dollars_total

                # Calculate the amounts for *this* close
                notional_dollars_to_close = notional_dollars_total * proportion_to_close
                collateral_dollars_to_close = collateral_dollars_total * proportion_to_close

                # 2. Get Current Price
                price_map = await self.price_cache.get_fresh_prices()
                current_price = price_map.get(ticker)
                if current_price is None or float(current_price) <= 0:
                    msg = f"Could not close position: Price for {ticker} is currently unavailable."
                    raise PriceNotAvailableError(msg)

                current_price_float = float(current_price)

                # 3. Calculate P&L and Credit
                # This simple math works for both long (invested_dollars > 0)
                # and short (invested_dollars < 0)
                price_change_pct = (current_price_float / entry_price) - 1.0
                pnl_precise = notional_dollars_to_close * price_change_pct

                # The total value to return to the user
                # For Long: 100 (principal) + 16.66 (pnl) = 116.66
                # For Short: 100 (abs(principal)) + 16.66 (pnl) = 116.66
                total_credit_precise = collateral_dollars_to_close + pnl_precise

                # 4. Apply Slippage (The Money Sink)
                # We floor the credit. e.g., 116.66 -> 116.
                final_credit_int = math.floor(max(0, total_credit_precise))

                # 5. Update Bank Account
                await self._update_cash_balance(
                    conn,
                    user_id,
                    guild_id,
                    final_credit_int,  # This is the final credit
                )

                # 6. Log the close events
                pnl_int = final_credit_int - int(collateral_dollars_to_close)
                reference_str = f"position_id:{position_id}"

                if pnl_int >= 0:
                    # Close with Profit
                    # 6a. Return the Collateral
                    await self.ledger_db.log_event(
                        conn=conn,
                        guild_id=guild_id,
                        event_type="TRANSFER",
                        event_reason="TRADE_CLOSE_COLLATERAL",
                        sender_id=COLLATERAL_POOL_ID,
                        receiver_id=user_id,
                        amount=int(collateral_dollars_to_close),
                        initiator_id=user_id,
                        reference_id=reference_str,
                    )
                    # 6b. Mint the *Integer* Profit
                    if pnl_int > 0:
                        await self.ledger_db.log_event(
                            conn=conn,
                            guild_id=guild_id,
                            event_type="MINT",
                            event_reason="TRADE_PROFIT",
                            sender_id=SYSTEM_USER_ID,
                            receiver_id=user_id,
                            amount=pnl_int,
                            initiator_id=user_id,
                            reference_id=reference_str,
                        )
                else:
                    # Close with Loss
                    loss_int = abs(pnl_int)  # pnl_int is negative here
                    # 6a. Return the Remaining Collateral (if any)
                    if final_credit_int > 0:
                        await self.ledger_db.log_event(
                            conn=conn,
                            guild_id=guild_id,
                            event_type="TRANSFER",
                            event_reason="TRADE_CLOSE_COLLATERAL",
                            sender_id=COLLATERAL_POOL_ID,
                            receiver_id=user_id,
                            amount=final_credit_int,
                            initiator_id=user_id,
                            reference_id=reference_str,
                        )
                    # 6b. Burn the *Integer* Loss
                    await self.ledger_db.log_event(
                        conn=conn,
                        guild_id=guild_id,
                        event_type="BURN",
                        event_reason="TRADE_LOSS",
                        sender_id=COLLATERAL_POOL_ID,
                        receiver_id=SYSTEM_USER_ID,
                        amount=loss_int,
                        initiator_id=user_id,
                        reference_id=reference_str,
                    )

                # 7. Update or Delete the position
                if is_partial_close:
                    new_notional = notional_dollars_total - notional_dollars_to_close
                    new_collateral = collateral_dollars_total - collateral_dollars_to_close
                    await conn.execute(
                        "UPDATE positions SET notional_dollars = ?, collateral_dollars = ? WHERE position_id = ?",
                        (new_notional, new_collateral, position_id),
                    )
                else:
                    await conn.execute(
                        "DELETE FROM positions WHERE position_id = ?",
                        (position_id,),
                    )

                # 8. Commit
                await conn.commit()

                log.info(
                    "Position %s closed for user %s. P&L: %s",
                    position_id,
                    user_id,
                    pnl_precise,
                )

            except Exception:
                await conn.rollback()
                log.exception(
                    "Position close failed and was rolled back for user %s",
                    user_id,
                )
                raise

            else:
                return (
                    ticker,
                    pnl_precise,
                    total_credit_precise,
                    final_credit_int,
                    int(collateral_dollars_to_close),
                    is_partial_close,
                )

    # --- Other Business Logic (e.g., portfolio P&L calculation) ---
    def is_market_open(self) -> bool:
        """Pass-through check to see if the US market is currently open based on the last API check."""
        # This now correctly checks the *cached* API state
        return self.price_cache.is_us_market_open()

    async def calculate_portfolio_value(
        self,
        user_id: UserId,
        guild_id: GuildId,
    ) -> dict | None:
        """Calculate the current cached market value and P&L of a user's portfolio."""
        await self.price_cache.get_fresh_prices()

        # 1. Fetch cash from UserDB
        user_cash_int = await self.user_db.get_stat(
            user_id,
            guild_id,
            StatName.CURRENCY,
        )
        user_cash_balance = float(user_cash_int)  # Use float for calculations

        # 2. Fetch all position lots
        async with self.database.get_cursor() as cursor:
            await cursor.execute(
                """
                SELECT position_id, ticker, notional_dollars, collateral_dollars, entry_price, timestamp
                FROM positions
                WHERE user_id = ? AND guild_id = ?
                ORDER BY timestamp ASC
                """,
                (user_id, guild_id),
            )
            rows = await cursor.fetchall()

        total_holdings_equity = 0.0
        total_collateral_held = 0.0
        total_pnl = 0.0
        positions_list = []
        long_value = 0.0
        short_value = 0.0

        for row in rows:
            (
                pos_id,
                ticker,
                notional_dollars,
                collateral_dollars,
                entry_price,
                timestamp,
            ) = row
            notional_dollars = int(notional_dollars)
            collateral_dollars = int(collateral_dollars)
            entry_price = float(entry_price)

            current_price, _ = self.price_cache.get_cached_price(ticker)

            pos_data = {
                "id": pos_id,
                "ticker": ticker,
                "collateral": collateral_dollars,
                "notional": notional_dollars,
                "entry": entry_price,
                "timestamp": timestamp,
                "current_price": current_price,
                "current_value": None,
                "pnl": None,
            }

            if current_price is not None:
                current_price_float = float(current_price)
                price_change_pct = (current_price_float / entry_price) - 1.0
                pnl = notional_dollars * price_change_pct
                current_equity = collateral_dollars + pnl

                pos_data["current_price"] = current_price_float
                pos_data["current_value"] = current_equity
                pos_data["pnl"] = pnl

                total_pnl += pnl
                total_collateral_held += collateral_dollars
                total_holdings_equity += current_equity

                if notional_dollars > 0:
                    long_value += current_equity
                else:
                    short_value += current_equity

            positions_list.append(pos_data)

        # Net Equity = Cash + (Equity of all positions)
        # Note: total_holdings_equity = total_collateral_held + total_pnl
        total_portfolio_value = user_cash_balance + total_holdings_equity

        return {
            "cash_balance": user_cash_balance,
            "holdings_equity": total_holdings_equity,
            "collateral_held": total_collateral_held,
            "total_value": total_portfolio_value,  # True Net Equity
            "total_pnl": total_pnl,
            "positions": positions_list,  # List of dictionaries
            "long_value": long_value,
            "short_value": short_value,
        }

    async def get_liquidatable_positions(
        self,
    ) -> list[tuple[int, UserId, GuildId, Ticker, float]]:
        """Fetch all positions where unrealized P&L has wiped out the collateral.

        Returns
        -------
            A list of (position_id, user_id, guild_id, ticker, pnl) tuples.

        """
        try:
            prices = await self.price_cache.get_fresh_prices()
        except ConnectionError:
            log.warning("Skipping liquidation check; cannot get fresh prices.")
            return []

        query = """
            SELECT position_id, user_id, guild_id, ticker,
                   notional_dollars, collateral_dollars, entry_price
            FROM positions
        """
        positions_to_close = []
        async with self.database.get_cursor() as cursor:
            await cursor.execute(query)
            for row in await cursor.fetchall():
                pos_id, user_id, guild_id, ticker, notional, collateral, entry = row

                current_price = prices.get(ticker)
                if not current_price or float(current_price) <= 0:
                    continue  # Can't liquidate without a price

                price_change_pct = (float(current_price) / entry) - 1.0
                pnl = notional * price_change_pct

                # --- THIS IS THE MARGIN CALL ---
                if pnl <= -collateral:
                    # The loss has exceeded or equaled the collateral.
                    positions_to_close.append((pos_id, user_id, guild_id, ticker, pnl))

        return positions_to_close

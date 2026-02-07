from __future__ import annotations  # Defer type annotation evaluation

import json
import logging
from typing import TYPE_CHECKING, Any, Self  # Self requires Python 3.11+

import aiohttp  # Ensure aiohttp is installed: pip install aiohttp

# --- Type Hinting Setup ---
type Ticker = str
type Price = float

# --- Logging ---
log = logging.getLogger(__name__)

if TYPE_CHECKING:
    import types


# --- Custom Exceptions ---
class AioTwelveDataError(Exception):
    """Base exception for this module."""


class AioTwelveDataApiError(AioTwelveDataError):
    """Represents an error returned by the Twelve Data API."""

    def __init__(
        self,
        status_code: int,
        message: str,
        api_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.api_code = api_code  # Specific code from Twelve Data JSON error
        super().__init__(f"API Error {status_code} (Code: {api_code}): {message}")


class AioTwelveDataRequestError(AioTwelveDataError, ConnectionError):
    """Represents network or request-related errors (timeout, connection refused)."""


# --- Client Class ---
class AioTwelveDataClient:
    """Asynchronous client for interacting with select Twelve Data API endpoints."""

    BASE_URL = "https://api.twelvedata.com"

    def __init__(
        self,
        api_key: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not api_key:
            msg = "Twelve Data API key is required."
            raise ValueError(msg)
        self._api_key = api_key
        # If no session is provided, we manage one internally
        self._session = session
        self._owns_session = session is None
        log.info("AioTwelveDataClient initialized.")

    async def __aenter__(self) -> Self:
        """Enter the async context manager, creating a session if needed."""
        if self._owns_session and self._session is None:
            self._session = aiohttp.ClientSession()
            log.debug("Created internal aiohttp session.")
        # If session was provided externally, the caller manages its lifecycle
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Exit the async context manager, closing the session if owned."""
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None
            log.debug("Closed internal aiohttp session.")
        # If session was provided externally, do nothing

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an asynchronous request to the Twelve Data API."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        # Use the existing session
        return await self._perform_request(self._session, method, endpoint, params)

    async def _perform_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform the actual request logic."""
        url = f"{self.BASE_URL}{endpoint}"
        req_params = params.copy() if params else {}
        req_params["apikey"] = self._api_key
        req_params["source"] = "aiohttp-custom"  # Identify our client

        try:
            log_params = req_params.copy()
            if "apikey" in log_params:
                log_params["apikey"] = "[REDACTED]"
            log.debug("Requesting %s %s with params: %s", method, url, log_params)

            async with session.request(
                method,
                url,
                params=req_params,
                timeout=10,
            ) as response:
                # Check for HTTP errors first
                # Special case: 401 usually means bad API key, treat as config error
                if response.status == 401:
                    log.error(
                        "API request failed with 401 Unauthorized - check API key.",
                    )
                    raise AioTwelveDataApiError(
                        response.status,
                        "Invalid API Key provided.",
                        401,
                    )  # Use API error but signal config issue

                # Raise other HTTP errors (404, 5xx etc.)
                response.raise_for_status()

                # Process successful response
                data = await response.json()
                log.debug("API Response (%s): %s", response.status, data)

                # Check for API-level errors within the JSON payload (e.g., status: "error")
                if isinstance(data, dict) and data.get("status") == "error":
                    msg = data.get("message", "Unknown API error occurred.")
                    code = data.get("code")
                    log.warning("API returned error status: %s (Code: %s)", msg, code)
                    raise AioTwelveDataApiError(response.status, msg, code)

                return data

        except aiohttp.ClientResponseError as e:  # Catch errors raised by raise_for_status
            log.exception("HTTP error during API request")
            msg = f"API request failed: {e.status} {e.message}"
            raise AioTwelveDataRequestError(
                msg,
            ) from e
        except TimeoutError as e:
            log.exception("API request timed out: %s %s", method, url)
            msg = "API request timed out."
            raise AioTwelveDataRequestError(msg) from e
        except aiohttp.ClientConnectionError as e:
            log.exception("API connection error: %s %s", method, url)
            msg = f"Could not connect to API: {e}"
            raise AioTwelveDataRequestError(msg) from e
        except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
            log.exception("Failed to decode API JSON response")
            msg = "Invalid JSON response received from API."
            raise AioTwelveDataError(msg) from e
        except Exception as e:
            # Catch any other unexpected errors during the request process
            log.exception("Unexpected error during API request")
            msg = f"An unexpected error occurred during the API request: {e}"
            raise AioTwelveDataError(
                msg,
            ) from e

    async def get_market_state(self, exchange: str) -> dict[str, Any] | None:
        """Fetch the market state for a specific exchange.

        Returns the first matching exchange dictionary or None if not found/error.
        """
        log.debug("Fetching market state for: %s", exchange)
        try:
            # The API returns a list, even for a single exchange
            data = await self._request(
                "GET",
                "/market_state",
                params={"exchange": exchange, "source": "aiohttp-cache-check"},
            )

            # API returns a list, find the first entry matching the name
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and entry.get("name", "").upper() == exchange.upper():
                        log.debug("Found market state for %s: %s", exchange, entry)
                        return entry
                log.warning(
                    "Market state list received, but no entry matched name: %s",
                    exchange,
                )
                return None  # Found list but not the exchange

            log.error(
                "Unexpected market state response format. Expected list, got %s",
                type(data).__name__,
            )

        except AioTwelveDataRequestError, AioTwelveDataApiError:
            # Log and return None, don't raise. Let the caller decide how to handle.
            log.exception("Failed to fetch market state for (%s)", exchange)
            return None
        except Exception:
            log.exception(
                "Unexpected error during market state fetch for (%s)",
                exchange,
            )
            return None
        else:
            return None

    # --- Public API Methods ---

    async def get_batch_prices(
        self,
        tickers: list[Ticker] | set[Ticker],
    ) -> dict[Ticker, Price | None]:
        """Fetch the latest prices for multiple tickers in a single request.

        Return a dictionary mapping ticker to price (float) or None if fetching failed for that ticker.
        """
        if not tickers:
            return {}

        # Normalize and prepare unique tickers for the API call
        unique_tickers = {t.upper() for t in tickers}
        symbol_param = ",".join(
            sorted(unique_tickers),
        )  # Sort for consistency/caching
        log.debug("Fetching batch prices for: %s", symbol_param)

        # Initialize result map with None for all requested tickers
        price_map: dict[Ticker, Price | None] = dict.fromkeys(unique_tickers)

        try:
            # *** /price endpoint supports batch via comma-separated symbols ***
            # *** AND returns a dictionary keyed by symbol. ***
            data = await self._request(
                "GET",
                "/price",
                params={
                    "symbol": symbol_param,
                },
            )

            # --- Parse the batch response ---
            # API return is a dictionary keyed by symbol, like time_series batch responses:
            if isinstance(data, dict):
                for ticker in unique_tickers:
                    ticker_data = data.get(ticker)
                    if isinstance(ticker_data, dict):
                        # Check individual status if the API provides it per symbol
                        if ticker_data.get("status", "ok") == "ok" and "price" in ticker_data:  # Assume ok if no status
                            try:
                                price_map[ticker] = float(ticker_data["price"])
                                log.debug(
                                    "Parsed batch price for %s: %.2f",
                                    ticker,
                                    price_map[ticker],
                                )
                            except ValueError, TypeError:
                                log.warning(
                                    "Could not parse price for %s in batch response: %s",
                                    ticker,
                                    ticker_data.get("price"),
                                )
                        else:
                            # Log error reported by API for this specific ticker
                            error_msg = ticker_data.get("message", "Unknown error")
                            error_code = ticker_data.get("code")
                            log.warning(
                                "API error for ticker %s in batch response (Code: %s): %s",
                                ticker,
                                error_code,
                                error_msg,
                            )
                    else:
                        # Ticker requested was not present in the response dict keys
                        # This can happen if the API just omits bad tickers from the response
                        log.warning(
                            "Ticker %s requested in batch was missing from API response.",
                            ticker,
                        )
            else:
                # Handle unexpected overall response format (e.g., a list, or single object)
                log.error(
                    "Unexpected batch price response format. Expected dict keyed by symbol, got %s",
                    type(data).__name__,
                )
                # Cannot reliably map prices, return None for all

        # Handle broader request errors (ConnectionError, InvalidTickerError if 400 applies to *all* symbols, etc.)
        except AioTwelveDataRequestError, AioTwelveDataApiError:
            log.exception("Failed to fetch batch prices for (%s)", symbol_param)
            # Depending on the error, you might want to raise it or just return None for all
            # For now, we return the map with Nones as initialized
        except Exception:
            log.exception(
                "Unexpected error during batch price fetch for (%s)",
                symbol_param,
            )  # TRY400
            # Return the map with Nones

        log.info(
            "Batch price fetch complete for %d unique tickers.",
            len(unique_tickers),
        )
        return price_map

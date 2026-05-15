"""Discord guild message search API wrapper."""

import asyncio
from typing import TYPE_CHECKING

import aiohttp

from modules.errors import SearchError
from modules.result import Err, Ok, Result

if TYPE_CHECKING:
    from modules.dtypes import ChannelId, GuildId, MessageId


async def count_messages_in_range(
    http_session: aiohttp.ClientSession,
    guild_id: GuildId,
    channel_id: ChannelId,
    lo_id: MessageId,
    hi_id: MessageId,
    max_retries: int = 3,
) -> Result[int, SearchError]:
    """Count exact number of messages in a range using Discord's guild search API.

    Returns Ok(count) on success, Err(SearchError) with reason on failure.
    Handles 202 (index not ready) with exponential backoff.
    """
    url = f"https://discord.com/api/v10/guilds/{guild_id}/messages/search"
    params = {
        "channel_id": [channel_id],
        "min_id": lo_id,
        "max_id": hi_id,
        "limit": 1,
        "include_nsfw": "true",
    }

    for attempt in range(max_retries):
        try:
            async with http_session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return Ok(data.get("total_results", 0))
                if resp.status == 202:
                    # Index not ready, retry with exponential backoff
                    delay = 2**attempt
                    await asyncio.sleep(delay)
                    continue
                if resp.status == 429:
                    return Err(SearchError("Rate limited by Discord"))
                if resp.status >= 500:
                    return Err(SearchError("Discord server error"))
                return Err(SearchError(f"HTTP {resp.status} from Discord API"))
        except aiohttp.ClientError as e:
            return Err(SearchError(f"Network error: {type(e).__name__}"))
        except (TimeoutError, ValueError) as e:
            return Err(SearchError(f"API error: {type(e).__name__}"))

    return Err(SearchError("Search index not ready after retries"))

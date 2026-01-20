#!/usr/bin/env python3
"""Populate the environment variables then run this to populate counts.json."""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Generator

# --- Configuration ---
try:
    BOT_TOKEN = os.environ["TOKEN"]
    CHANNEL_ID = os.environ["JOIN_LEAVE_LOG_CHANNEL_ID"]
    CARL_BOT_ID = os.environ["CARL_BOT_ID"]
    KIWI_BOT_ID = os.environ["KIWI_BOT_ID"]
except KeyError as e:
    msg = f"Error: Missing required environment variable: {e}"
    raise SystemExit(msg) from e

TARGET_BOT_IDS = frozenset([CARL_BOT_ID, KIWI_BOT_ID])
TARGET_TIMEZONE = os.environ.get("TARGET_TIMEZONE", "Pacific/Auckland")
API_URL = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages"

# Modern type alias
type MessageList = list[dict[str, Any]]


def fetch_all_messages(session: requests.Session) -> MessageList:
    """Fetch all messages from the channel by paginating through the API."""
    all_messages: MessageList = []
    before_id: str | None = None
    params = {"limit": 100}

    print(f"Fetching messages from channel {CHANNEL_ID}...")
    while True:
        if before_id:
            params["before"] = before_id

        response = session.get(API_URL, params=params)

        if response.status_code == 429:
            retry_after = response.json().get("retry_after", 1)
            print(f"Rate limited. Retrying in {retry_after}s...")
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        batch = response.json()

        if not batch:
            break

        all_messages.extend(batch)
        print(f"Fetched {len(batch)} messages (total: {len(all_messages)})")

        if len(batch) < 100:
            break

        before_id = batch[-1]["id"]
        time.sleep(1)

    return all_messages


def parse_member_events(
    messages: MessageList,
) -> Generator[tuple[datetime, int], None, None]:
    """Parse messages to extract member count data points, oldest to newest."""
    count = 0
    tz = ZoneInfo(TARGET_TIMEZONE)

    for msg in reversed(messages):
        author_id = msg.get("author", {}).get("id")
        if author_id not in TARGET_BOT_IDS or not msg.get("embeds"):
            continue

        embed = msg["embeds"][0]
        title = embed.get("title", "").lower()
        date = datetime.fromisoformat(msg["timestamp"]).astimezone(tz)

        if title.lower() == "member joined":
            try:
                suffix = embed.get("description", "").split("> ", 1)[-1]
                if author_id == CARL_BOT_ID:
                    index = suffix.index(" ") - 2
                    count = int(suffix[:index])
                else:  # KIWI_BOT_ID
                    line = suffix.split("\n", 1)[0]
                    count = int("".join(c for c in line if c.isdigit()))
                yield (date, count)
            except (ValueError, IndexError):
                continue  # Skip malformed embeds

        elif "member left" in title.lower() and count > 0:
            count -= 1
            yield (date, count)


def save_results(data: list[tuple[datetime, int]]) -> None:
    """Save the processed data to CSV and JSON files."""
    if not data:
        print("No data to save.")
        return

    # Newest-first for CSV, oldest-first for JSON
    data.sort(key=lambda item: item[0], reverse=True)
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    # --- Save CSV ---
    csv_path = output_dir / "counts.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "count"])
        writer.writerows([date.strftime("%Y-%m-%dT%H:%M"), count] for date, count in data)
    print(f"Saved {len(data)} records to {csv_path}")

    # --- Save JSON ---
    json_path = output_dir / "counts.json"
    json_data = [{"date": date.strftime("%Y-%m-%dT%H:%M"), "value": value} for date, value in reversed(data)]
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)
    print(f"Saved {len(data)} records to {json_path}")


def main() -> None:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bot {BOT_TOKEN}",
            "User-Agent": "MemberCountScript/3.1",
        },
    )

    messages = fetch_all_messages(session)
    print(f"\nFetching complete. Found {len(messages)} messages.")

    member_data = list(parse_member_events(messages))

    save_results(member_data)
    print("\nScript finished.")


if __name__ == "__main__":
    main()

"""Handle interactions with the LibreTranslate API.

This module provides the client and parsing logic for the auto-translation
features.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

import aiohttp

log = logging.getLogger(__name__)

# Regex to detect code blocks (inline ` or block ```)
CODE_BLOCK_REGEX = re.compile(r"(`{1,3}).*?\1", re.DOTALL)
# Regex to detect breadcrumbs like [:flag_ro: ➡️ :flag_gb:]
BREADCRUMB_REGEX = re.compile(r"\[([a-zA-Z-]+) -> ([a-zA-Z-]+)\]")


class TranslationContext(NamedTuple):
    """Holds the source and target language derived from a breadcrumb."""

    source_lang: str
    target_lang: str


class TranslationClient:
    """A client for interacting with a self-hosted LibreTranslate instance."""

    def __init__(self, host: str, session: aiohttp.ClientSession) -> None:
        self.host = host.rstrip("/")
        self.session = session
        self.endpoint = f"{self.host}/translate"

    def _should_ignore(self, text: str) -> bool:
        """Check if text should be ignored.

        Ignores text that is too short, mostly numbers, or contained entirely
        within code blocks.
        """
        cleaned = CODE_BLOCK_REGEX.sub("", text).strip()

        # Ignore empty after code removal
        if not cleaned:
            return True

        # Ignore very short messages (e.g. "ok", "lol", "da")
        # < 2 words AND < 5 chars
        return bool(len(cleaned.split()) < 2 and len(cleaned) < 5)

    async def translate(
        self,
        text: str,
        source: str,
        target: str,
        bypass_ignore: bool = False,
    ) -> str | None:
        """Translate text using the LibreTranslate API.

        Returns None if the translation fails, is ignored, or is identical
        to the input.
        """
        # Strip breadcrumbs from the input text so we don't translate them
        clean_text = BREADCRUMB_REGEX.sub("", text).strip()

        if not bypass_ignore and self._should_ignore(clean_text):
            return None

        # Payload for LibreTranslate
        payload = {
            "q": clean_text,
            "source": source,
            "target": target,
            "format": "html",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=3)
            async with self.session.post(self.endpoint, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    log.warning(
                        "LibreTranslate API error %s: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return None

                data = await resp.json()
                translated_text = data.get("translatedText")

                # No-op check: If translation is identical (case-insensitive), ignore it.
                # This handles the "User set to RO but speaks EN" case.
                if not translated_text or translated_text.strip().lower() == clean_text.strip().lower():
                    return None

                return translated_text

        except Exception:
            log.exception("Failed to connect to translation service")
            return None

    @staticmethod
    def get_breadcrumb_string(source_lang: str, target_lang: str) -> str:
        """Generate the emoji breadcrumb string."""
        return f"[{source_lang.upper()} -> {target_lang.upper()}]"

    @staticmethod
    def parse_breadcrumb(content: str) -> TranslationContext | None:
        """Extract source and target languages from a message's breadcrumb.

        Returns None if no breadcrumb is found.
        """
        match = BREADCRUMB_REGEX.search(content)
        if match:
            # If the bot said RO -> GB, and user replies, we want to go GB -> RO.
            raw_src, raw_tgt = match.groups()

            # Context for the *reply*:
            # We assume the replier is speaking the TARGET of the breadcrumb
            # and wants to translate back to the SOURCE.
            return TranslationContext(
                source_lang=raw_tgt.lower(),
                target_lang=raw_src.lower(),
            )
        return None

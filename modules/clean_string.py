"""Chat Sanitizer.

English + full emoji, nuisance filter only.
Uses `regex` when available, falls back to `unicodedata`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Final

__all__ = ["BACKEND", "sanitize_chat"]

log = logging.getLogger(__name__)

MAX_LEN: Final = 4_000

# ---------------------------------------------------------------------------
# Backend - resolved once at import
# ---------------------------------------------------------------------------

try:
    import regex as _re

    _V1 = _re.UNICODE | _re.VERSION1
    _junk = _re.compile(r"[[\p{Cc}]--[\t\n]]|[[\p{Cf}]--[\u200D]]|\p{Co}|\p{Cn}", _V1)
    _zalgo = _re.compile(r"(\p{M}{2})\p{M}+", _V1)
    _spaces = _re.compile(r"\p{Z}+", _V1)

    def _scrub(text: str) -> str:
        return _spaces.sub(" ", _zalgo.sub(r"\1", _junk.sub("", text)))

    BACKEND = "regex"

except ModuleNotFoundError:
    _JUNK_CATS: Final = frozenset({"Cc", "Cf", "Co", "Cn"})
    _SPACE_CATS: Final = frozenset({"Zs", "Zl", "Zp"})
    _KEEP: Final = frozenset({"\t", "\n", "\u200d"})

    def _scrub(text: str) -> str:
        """Single-pass fallback: strips junk, prunes Zalgo, collapses spaces."""
        out: list[str] = []
        marks = 0
        for ch in text:
            cat = unicodedata.category(ch)
            if cat[0] == "M":  # combining mark
                marks += 1
                if marks <= 2:
                    out.append(ch)
            elif ch in _KEEP or cat not in _JUNK_CATS:  # keeper
                marks = 0
                if cat in _SPACE_CATS:
                    if not out or out[-1] != " ":  # collapse Unicode spaces
                        out.append(" ")
                else:
                    out.append(ch)
            # else: junk - drop silently, don't touch `marks`
        return "".join(out)

    BACKEND = "unicodedata"

# ASCII-only patterns; stdlib `re` is fine for both backends
_tabs = re.compile(r"\t+")
_newlines = re.compile(r"\n{3,}")

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize_chat(text: str) -> str:
    r"""Nuisance-filter a chat message.

    1. NFC normalize
    2. Strip Cc / Cf / Co / Cn  (keeping \\t  \\n  ZWJ)
    3. Cap combining marks at 2  (Zalgo)
    4. Collapse whitespace  (Unicode spaces → ' ', tabs, newlines capped)
    """
    if not isinstance(text, str):
        msg = f"Expected str, got {type(text).__name__!r}"
        raise TypeError(msg)

    original = text
    clean = unicodedata.normalize("NFC", text[:MAX_LEN])
    clean = _scrub(clean)
    clean = _tabs.sub("\t", clean)
    clean = _newlines.sub("\n\n", clean)
    clean = clean.strip()

    if clean != original:
        log.warning("Sanitized message changed: %r → %r", original[:200], clean[:200])

    return clean

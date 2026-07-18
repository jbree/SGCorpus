"""Text sanitizing, Unicode normalization, and lookup-folding.

Wiktextract mostly strips wikitext, but residual markup can appear anywhere
(plan §3.5), so every user-facing string runs through :func:`clean`. Two folded
forms back the lookup indexes: ``fold`` (case-insensitive exact/prefix) and
``search_fold`` (diacritic-stripped, lenient — never displayed).
"""

from __future__ import annotations

import re
import unicodedata

# [[target|display]] or [[target]] — keep the display text.
_WIKILINK = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]+)\]\]")
# {{template|...}} — drop entirely (residual, rare after Wiktextract).
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
# '''bold''' / ''italic'' wiki emphasis markers.
_EMPHASIS = re.compile(r"'{2,5}")
# Any HTML/XML tag.
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def clean(text: str | None) -> str:
    """Strip residual wikitext/HTML, collapse whitespace, normalize to NFC.

    Returns an empty string for ``None`` so callers can treat "missing" and
    "empty after cleanup" uniformly.
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFC", text)
    # Templates first (may nest a link), then links, then emphasis/tags.
    prev = None
    while prev != s:
        prev = s
        s = _TEMPLATE.sub("", s)
    s = _WIKILINK.sub(r"\1", s)
    s = _EMPHASIS.sub("", s)
    s = _HTML_TAG.sub("", s)
    s = s.replace("​", "")  # zero-width space
    s = _WHITESPACE.sub(" ", s)
    return s.strip()


def has_residual_markup(text: str) -> bool:
    """QA helper (plan §9): true if shipped text still carries markup."""
    return "[[" in text or "{{" in text or "<" in text and ">" in text


def fold(text: str) -> str:
    """Case-insensitive, NFC lookup key for exact/prefix matching."""
    return unicodedata.normalize("NFC", text).strip().casefold()


def search_fold(text: str) -> str:
    """Lenient search key: fold plus combining-mark removal (café -> cafe).

    Never displayed — powers ``word_search`` so diacritic-insensitive typing
    finds the headword. Falls back to the plain fold if stripping empties it
    (e.g. a headword written entirely in combining characters).
    """
    folded = fold(text)
    decomposed = unicodedata.normalize("NFD", folded)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    stripped = unicodedata.normalize("NFC", stripped)
    return stripped or folded

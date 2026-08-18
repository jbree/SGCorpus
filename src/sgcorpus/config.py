"""Build configuration and Wiktextract source resolution.

Two things live here: :class:`BuildConfig` (the tunable knobs that decide what
goes into a pack — the "open decisions" of plan §10 given concrete defaults),
and :func:`resolve_source` which maps an edition + language onto a kaikki.org
download URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Source resolution -----------------------------------------------------
#
# kaikki.org publishes one extract per Wiktionary edition. The English edition
# additionally exposes a per-word-language slice under a human language-name
# path (verified live):
#
#   https://kaikki.org/dictionary/<LanguageName>/kaikki.org-dictionary-<LanguageName>.jsonl
#
# That path is the default source for enwiktionary-based packs. Native editions
# (frwiktionary, dewiktionary, …) live under their own host prefix and change
# shape occasionally, so those are best supplied with an explicit --url. The
# --url flag always overrides everything below.

ENWIKTIONARY_SLICE = (
    "https://kaikki.org/dictionary/{name}/kaikki.org-dictionary-{name}.jsonl"
)

# lang_code -> human language name used in the enwiktionary slice path.
# Extend as packs are added; unknown codes require an explicit --lang-name/--url.
LANG_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "sv": "Swedish",
    "pl": "Polish",
    "ru": "Russian",
    "ja": "Japanese",
    "la": "Latin",
}


@dataclass(frozen=True)
class SourceSpec:
    edition: str          # e.g. "enwiktionary"
    lang_code: str        # e.g. "en" — the PRIMARY partition key
    word_lang: str        # human name of the headword language, e.g. "English"
    definition_lang: str  # human name of the gloss language
    url: str


def resolve_source(
    edition: str,
    lang_code: str,
    *,
    url: str | None = None,
    lang_name: str | None = None,
) -> SourceSpec:
    """Resolve a download URL and language metadata for a pack build."""
    name = lang_name or LANG_NAMES.get(lang_code)
    if edition == "enwiktionary":
        definition_lang = "English"
        if url is None:
            if not name:
                raise ValueError(
                    f"No known language name for lang_code={lang_code!r}; "
                    f"pass --lang-name or --url."
                )
            url = ENWIKTIONARY_SLICE.format(name=name)
    else:
        # Native edition (Approach A): definitions in the edition's own language.
        definition_lang = name or lang_code
        if url is None:
            raise ValueError(
                f"Edition {edition!r} has no built-in URL template; pass --url."
            )
    return SourceSpec(
        edition=edition,
        lang_code=lang_code,
        word_lang=name or lang_code,
        definition_lang=definition_lang,
        url=url,
    )


# --- Build knobs -----------------------------------------------------------

# Relation types we recognize; the app treats synonym/antonym as core thesaurus
# and related/hypernym as optional "related" chips. `derived` is carried but the
# app groups it nowhere; `form_of` links an inflected form to its lemma (plural →
# singular) and is not sourced from a linkage array — see `_normalize_relations`.
# `hyponym` is deliberately absent: the narrower-term list is mostly noise (every
# species under "bird") and the app shows it nowhere, so it is not even extracted.
ALL_RELATION_TYPES = (
    "synonym",
    "antonym",
    "hypernym",
    "related",
    "derived",
    "form_of",
)

# Wiktextract stores linkage under plural array keys; map to our rel_type. Note
# `form_of` has no entry here — it lives on senses (form_of/alt_of), not in a
# top-level linkage array, and is captured separately.
RELATION_SOURCE_KEYS = {
    "synonyms": "synonym",
    "antonyms": "antonym",
    "hypernyms": "hypernym",
    "related": "related",
    "derived": "derived",
}


@dataclass
class BuildConfig:
    """Knobs controlling what a pack contains (plan §8.4 size levers, §10)."""

    keep_examples: bool = True
    keep_etymology: bool = False          # plan §10.4: leaning drop for size
    # Skip the "plural of X" stub *sense* (the app shows the lemma's definition
    # instead). The plural→singular link itself is still captured as a `form_of`
    # relation, and such an inflection-only headword is kept, not dropped.
    drop_form_of: bool = True
    # Proper-noun handling: names/proper nouns are ~18% of the raw corpus and
    # the obscure long tail (surnames, minor places, disambiguation stubs) mostly
    # degrades lookup/thesaurus results. Keep a name only when its headword's
    # wordfreq Zipf frequency is >= this cutoff, so famous names stay (London
    # 5.27, God 5.57, Dylan 4.05, Hogwarts 3.18) and the tail is dropped (McStay
    # 1.70). ``None`` disables the filter entirely (keep every name).
    name_min_zipf: float | None = 3.0
    # Multi-element glosses: "join" (broad→specific joined by "; ") or
    # "last" (most specific only). Plan §10.2 — pick one, apply consistently.
    gloss_hierarchy: str = "join"
    relation_types: tuple[str, ...] = ALL_RELATION_TYPES
    max_relations_per_type: int = 50      # plan §3.4 list cap
    max_senses_per_word: int = 60
    page_size: int = 4096                 # plan §5 — set before table creation

    def gloss_join(self, glosses: list[str]) -> str:
        if self.gloss_hierarchy == "last":
            return glosses[-1]
        return "; ".join(glosses)

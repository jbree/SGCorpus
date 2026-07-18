"""Turn one Wiktextract entry into normalized rows (plan §3).

One input line = one (word, pos, etymology) entry. We emit at most one
:class:`NormWord` per line (its senses/pronunciations/relations attached),
applying the sanitizer, folding, form-of dropping, list caps and dialect-tagged
IPA retention. Missing/None fields never abort — they degrade to empty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import BuildConfig, RELATION_SOURCE_KEYS
from .sanitize import clean, fold, search_fold

# Part-of-speech normalization: Wiktextract is open-vocabulary; collapse the
# common variants to a small stable set. Unknown values pass through cleaned.
POS_MAP = {
    "noun": "noun",
    "proper noun": "noun",
    "verb": "verb",
    "adj": "adjective",
    "adjective": "adjective",
    "adv": "adverb",
    "adverb": "adverb",
    "pron": "pronoun",
    "pronoun": "pronoun",
    "prep": "preposition",
    "preposition": "preposition",
    "conj": "conjunction",
    "conjunction": "conjunction",
    "intj": "interjection",
    "interjection": "interjection",
    "num": "numeral",
    "numeral": "numeral",
    "article": "article",
    "det": "determiner",
    "determiner": "determiner",
    "phrase": "phrase",
    "proverb": "phrase",
    "name": "name",
}


@dataclass
class NormSense:
    ordinal: int
    gloss: str
    example: str | None
    example_en: str | None
    tags: list[str]


@dataclass
class NormPron:
    ipa: str
    dialects: list[str]
    rhyme_key: str | None


@dataclass
class NormRelation:
    rel_type: str
    target: str
    target_folded: str
    sense_hint: str | None
    tags: list[str]


@dataclass
class NormWord:
    word: str
    word_folded: str
    word_search: str
    pos: str
    senses: list[NormSense] = field(default_factory=list)
    pronunciations: list[NormPron] = field(default_factory=list)
    relations: list[NormRelation] = field(default_factory=list)

    def sort_key(self) -> tuple:
        # Deterministic ordering for insert + etym_index assignment (plan §5/§7).
        return (self.word_search, self.word_folded, self.word, self.pos)


def normalize_pos(pos: str | None) -> str:
    p = clean(pos or "").lower()
    return POS_MAP.get(p, p or "unknown")


def _first_str(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return value[0] if isinstance(value[0], str) else None
    return None


def _normalize_senses(entry: dict, cfg: BuildConfig) -> list[NormSense]:
    out: list[NormSense] = []
    for raw in entry.get("senses", []) or []:
        if not isinstance(raw, dict):
            continue
        # Drop pure form-of / alt-of senses (plan §3.2).
        if cfg.drop_form_of and (raw.get("form_of") or raw.get("alt_of")):
            continue
        glosses = raw.get("glosses") or raw.get("raw_glosses") or []
        glosses = [clean(g) for g in glosses if isinstance(g, str)]
        glosses = [g for g in glosses if g]
        if not glosses:
            continue  # empty/whitespace gloss -> skip sense (plan §3.2)
        gloss = cfg.gloss_join(glosses)

        example = example_en = None
        if cfg.keep_examples:
            for ex in raw.get("examples", []) or []:
                if not isinstance(ex, dict):
                    continue
                text = clean(ex.get("text"))
                if text:
                    example = text
                    example_en = clean(ex.get("english")) or None
                    break

        tags = [clean(t) for t in (raw.get("tags") or []) if isinstance(t, str)]
        tags = [t for t in tags if t]

        out.append(
            NormSense(
                ordinal=len(out),
                gloss=gloss,
                example=example,
                example_en=example_en,
                tags=tags,
            )
        )
        if len(out) >= cfg.max_senses_per_word:
            break
    return out


def _normalize_pronunciations(entry: dict) -> list[NormPron]:
    out: list[NormPron] = []
    seen: set[tuple] = set()
    for raw in entry.get("sounds", []) or []:
        if not isinstance(raw, dict):
            continue
        ipa = clean(raw.get("ipa"))
        rhyme = clean(raw.get("rhymes")) or None
        if not ipa and not rhyme:
            continue  # audio-only / enpr-only entry contributes no IPA row
        if not ipa:
            # Keep a rhyme-only row so the future rhyme engine has the key.
            ipa = ""
        dialects = [clean(t) for t in (raw.get("tags") or []) if isinstance(t, str)]
        dialects = [d for d in dialects if d]
        key = (ipa, tuple(dialects), rhyme)
        if key in seen:
            continue
        seen.add(key)
        out.append(NormPron(ipa=ipa, dialects=dialects, rhyme_key=rhyme))
    # Drop rows with neither ipa nor rhyme_key (defensive).
    return [p for p in out if p.ipa or p.rhyme_key]


def _normalize_relations(entry: dict, cfg: BuildConfig) -> list[NormRelation]:
    out: list[NormRelation] = []
    # Merge entry-level and sense-level linkage, de-dup by (rel_type, fold(word)).
    seen: dict[str, set[str]] = {}
    counts: dict[str, int] = {}

    def add_from(container: dict) -> None:
        for src_key, rel_type in RELATION_SOURCE_KEYS.items():
            if rel_type not in cfg.relation_types:
                continue
            for item in container.get(src_key, []) or []:
                if not isinstance(item, dict):
                    continue
                target = clean(item.get("word"))
                if not target:
                    continue
                tfold = fold(target)
                bucket = seen.setdefault(rel_type, set())
                if tfold in bucket:
                    continue
                if counts.get(rel_type, 0) >= cfg.max_relations_per_type:
                    continue
                # Skip self-references.
                if tfold == fold(clean(entry.get("word", ""))):
                    continue
                bucket.add(tfold)
                counts[rel_type] = counts.get(rel_type, 0) + 1
                tags = [clean(t) for t in (item.get("tags") or []) if isinstance(t, str)]
                out.append(
                    NormRelation(
                        rel_type=rel_type,
                        target=target,
                        target_folded=tfold,
                        sense_hint=clean(item.get("sense")) or None,
                        tags=[t for t in tags if t],
                    )
                )

    add_from(entry)
    for sense in entry.get("senses", []) or []:
        if isinstance(sense, dict):
            add_from(sense)
    return out


def normalize_entry(
    entry: dict, cfg: BuildConfig, target_lang: str | None
) -> NormWord | None:
    """Normalize one entry to a :class:`NormWord`, or None if it should be dropped.

    Dropped when: not the target language, no surface word, or zero senses after
    filtering (the graceful-degradation contract, plan §8.5, guarantees every
    stored headword has word + ≥1 gloss).
    """
    if not isinstance(entry, dict):
        return None
    lang_code = entry.get("lang_code")
    if target_lang and lang_code and lang_code != target_lang:
        return None

    word = clean(entry.get("word"))
    if not word:
        return None

    senses = _normalize_senses(entry, cfg)
    if not senses:
        return None

    nw = NormWord(
        word=word,
        word_folded=fold(word),
        word_search=search_fold(word),
        pos=normalize_pos(entry.get("pos")),
        senses=senses,
        pronunciations=_normalize_pronunciations(entry),
        relations=_normalize_relations(entry, cfg),
    )
    return nw


def iter_entries(lines):
    """Yield parsed JSON objects from an iterable of raw lines.

    Malformed lines are counted, never fatal (plan §3.5). Yields
    ``(obj_or_None, was_error)`` so the caller can tally skips.
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line), False
        except (json.JSONDecodeError, ValueError):
            yield None, True

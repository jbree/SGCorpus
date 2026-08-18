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
    "proper noun": "name",
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


# Raw Wiktextract pos strings that denote a proper noun / name. enwiktionary
# emits "name"; the others are defensive for cross-edition / future data. Checked
# against the RAW pos (before POS_MAP folds "proper noun" -> "name"), so the
# proper-noun filter fires regardless of which spelling the source used.
PROPER_NOUN_POS = frozenset({"name", "proper noun", "proper-noun", "propn"})


def normalize_pos(pos: str | None) -> str:
    p = clean(pos or "").lower()
    return POS_MAP.get(p, p or "unknown")


def is_proper_noun_pos(pos: str | None) -> bool:
    """True if a raw Wiktextract ``pos`` marks the entry as a name/proper noun."""
    return clean(pos or "").lower() in PROPER_NOUN_POS


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

    # Inflection / alternative-form links: "mountains" → "mountain", "ran" →
    # "run". Wiktextract records these on a sense as form_of/alt_of (a list of
    # {"word": lemma}) alongside a "plural of mountain"-style gloss. We keep the
    # link even though the stub sense itself is dropped, so the app can offer the
    # lemma as a see-also and borrow its definition. The gloss rides along as
    # sense_hint so the app can label the relationship ("plural of mountain").
    if "form_of" in cfg.relation_types:
        self_fold = fold(clean(entry.get("word", "")))
        bucket = seen.setdefault("form_of", set())
        for sense in entry.get("senses", []) or []:
            if not isinstance(sense, dict):
                continue
            links = sense.get("form_of") or sense.get("alt_of")
            if not links:
                continue
            glosses = sense.get("glosses") or sense.get("raw_glosses") or []
            hint = next((clean(g) for g in glosses if isinstance(g, str) and clean(g)), None)
            tags = [clean(t) for t in (sense.get("tags") or []) if isinstance(t, str)]
            tags = [t for t in tags if t]
            for item in links:
                target = clean(item.get("word")) if isinstance(item, dict) else clean(item)
                if not target:
                    continue
                tfold = fold(target)
                if not tfold or tfold in bucket or tfold == self_fold:
                    continue
                if counts.get("form_of", 0) >= cfg.max_relations_per_type:
                    continue
                bucket.add(tfold)
                counts["form_of"] = counts.get("form_of", 0) + 1
                out.append(
                    NormRelation(
                        rel_type="form_of",
                        target=target,
                        target_folded=tfold,
                        sense_hint=hint,
                        tags=tags,
                    )
                )
    return out


def normalize_entry(entry: dict, cfg: BuildConfig) -> NormWord | None:
    """Normalize one entry to a :class:`NormWord`, or None if it should be dropped.

    Language filtering is **not** done here — it is the caller's single
    responsibility (see :func:`sgcorpus.build._stage_entries`), so this function
    has one job: extract and clean. Dropped when there is no surface word, or
    when it has neither a real sense nor a `form_of` link: a bare inflected form
    ("mountains", whose only sense is the dropped "plural of mountain" stub) is
    kept as a redirect to its lemma; every other stored headword still has word +
    ≥1 gloss (the graceful-degradation contract, plan §8.5).
    """
    if not isinstance(entry, dict):
        return None

    word = clean(entry.get("word"))
    if not word:
        return None

    senses = _normalize_senses(entry, cfg)
    relations = _normalize_relations(entry, cfg)
    if not senses and not any(r.rel_type == "form_of" for r in relations):
        return None

    nw = NormWord(
        word=word,
        word_folded=fold(word),
        word_search=search_fold(word),
        pos=normalize_pos(entry.get("pos")),
        senses=senses,
        pronunciations=_normalize_pronunciations(entry),
        relations=relations,
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

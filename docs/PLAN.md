# Wiktextract → SQLite Extraction Pipeline — Implementation Plan

**Status:** planning · **Runs in:** a separate repository (not the Song Garden app repo) · **Consumed by:** the Song Garden macOS/iOS app's reference inspector (dictionary + thesaurus panels).

This document is self-contained so it can be dropped into the pipeline repo as its spec. It covers the source data and the **assumptions we make about its format**, the output artifact and its schema (the contract the app depends on), the build stages, and — because these are hard requirements from day one — **multi-language packaging, downloadable/delta updates, and the cross-platform app's consumption constraints.**

---

## 1. Goal & scope

Produce, per language, a compact **read-only SQLite database** derived from Wiktionary (via Wiktextract) that powers two panels in the app:

- **Dictionary** — headword → pronunciations (IPA) + senses (definition, part of speech, examples, tags).
- **Thesaurus** — headword → synonyms / antonyms / related terms.

Out of scope for this pipeline:
- **Rhyme.** Handled separately in the app by a CMUdict phoneme engine (English-only). This pipeline may *optionally* emit IPA/rhyme-key columns that a future language-agnostic rhyme engine could use, but it does not build the rhyme index.
- Translations between languages, inflection tables, etymology prose beyond a short optional field, audio byte files.

Design priorities, in order: **(1) correct, useful data for songwriters** (modern vocabulary, examples, IPA); **(2) small on-device footprint**; **(3) reproducible, diffable builds** so delta updates are possible; **(4) identical behavior on macOS and iOS.**

---

## 2. Source data

- **Source:** Wiktextract machine-readable extracts published at **kaikki.org** (parser: <https://github.com/tatuylonen/wiktextract>).
- **Format:** JSON Lines (JSONL) — one JSON object per line, one object per (headword, part-of-speech, etymology) entry.
- **License:** **CC BY-SA 4.0** (inherited from Wiktionary). Obligations the app must honor: attribution to Wiktionary + Wiktextract, and share-alike **on the lexical data** (not on app code — the app merely bundles/reads the data). Action items: an acknowledgements screen listing sources, and a `LICENSE`/`ATTRIBUTION` record shipped *inside each language pack* and surfaced in the manifest (see §6). Record the exact source-dump date and kaikki URL per pack.
- **Update cadence:** kaikki re-extracts roughly weekly. We do **not** track that cadence; we cut a pack version deliberately (see §7).
- **Size (English, raw):** the English-Wiktionary word extract is ≈2.7 GB JSONL uncompressed. This is a **build-time input**, never shipped. Compressed downloads are available and preferred for the build.

### 2.1 Which edition = which language — the decision that shapes packaging

Wiktextract publishes a separate extract **per Wiktionary edition**. This creates two different meanings of "a language pack," and we must pick deliberately:

| Approach | Source | You get | Trade-off |
|---|---|---|---|
| **A — one edition per pack (recommended)** | `frwiktionary`, `dewiktionary`, … each language's own edition | Headwords in language X, **defined in language X** (French words explained in French) | Native-language definitions — right for someone writing lyrics *in* that language. Definition quality/coverage varies by edition. |
| **B — slice the English edition** | `enwiktionary`, filtered by `lang_code` | Headwords in language X, **defined in English** | One source to master; consistent English glosses; but a French speaker gets English definitions. Good as a *secondary* "learner" pack. |

**Recommendation:** default to **Approach A** — each downloadable pack is one edition, definitions in that pack's own language, keyed by `lang_code`. Treat Approach B as an optional alternate pack family (e.g. `fr-glossed-en`) if product wants it later. Either way the **output schema is identical**; only the input source and the `definition_lang` metadata differ. Encode both `word_lang` and `definition_lang` in the pack manifest so the app can label packs correctly.

> **Rhyme caveat for non-English:** CMUdict covers English only. Non-English packs ship dictionary + thesaurus but **no rhyme** until a per-language phonetic source is added. Wiktionary's own `sounds[].ipa` / `sounds[].rhymes` (see §3) are the natural seed for a future language-agnostic rhyme key — emit them so that door stays open.

---

## 3. Assumptions about the extracted format

These are the fields we rely on and the quirks we normalize around. **Treat them as assumptions to validate against a real dump before trusting them** (§9), because Wiktextract's schema drifts and coverage varies by edition. Fields we don't list are ignored.

### 3.1 Per-line (entry) object — top level

```jsonc
{
  "word": "ocean",              // headword (may be multi-word / contain spaces, hyphens, apostrophes, diacritics)
  "pos": "noun",                // part of speech; open vocabulary (see 3.4)
  "lang": "English",            // human-readable language name
  "lang_code": "en",            // BCP-ish code; PRIMARY language key — filter/partition on this
  "senses": [ ... ],            // see 3.2 — the definitions
  "sounds": [ ... ],            // see 3.3 — IPA etc.; OFTEN ABSENT
  "synonyms":  [ {"word": "..."}, ... ],   // entry-level linkage; see 3.4
  "antonyms":  [ ... ],
  "hypernyms": [ ... ], "hyponyms": [ ... ], "related": [ ... ], "derived": [ ... ],
  "etymology_text": "…",        // optional, prose; we keep at most a trimmed snippet or drop
  "forms": [ {"form":"oceans","tags":["plural"]} ],  // inflections — DROP (rhyme/search don't need them here)
  "translations": [ ... ],      // DROP
  "hyphenation": [...],         // optional; ignore unless product wants it
  "head_templates": [...],      // ignore
  "categories": [...], "topics": [...]  // optional; ignore or keep topics as tags
}
```

**Key assumptions & quirks:**

- **One headword spans many lines.** A word appears once per (pos, etymology). The pipeline **must group by `(word, lang_code)`** and merge, or store per-entry rows and aggregate at query time. We store normalized rows and let the app query by `word_folded` (§5).
- **`word` is a surface string, not an id.** It can contain spaces, hyphens, apostrophes, combining diacritics. Store it verbatim (NFC) and also a **folded** form for lookup (lowercased, NFC, optionally diacritic-stripped for a secondary search column — never overwrite the real word).
- Some editions/entries omit `lang_code`; fall back to a fixed value derived from the source edition. Never emit a row with an unknown language.

### 3.2 `senses[]` — definitions

```jsonc
{
  "glosses": ["A body of salt water …"],   // definition text; USUALLY 1 element, sometimes hierarchical (see below)
  "raw_glosses": ["…"],                     // occasional; prefer glosses, fall back to raw_glosses
  "tags": ["informal"],                     // register/usage: informal, obsolete, transitive, dated, slang, …
  "examples": [ {"text":"The wide ocean.", "ref":"…", "english":"…"} ],  // usage examples
  "id": "ocean-en-noun-abc123",             // stable-ish sense id (see §7 caveat)
  "form_of": [ {"word":"ocean"} ],          // if present, this sense is just "inflected form of X"
  "alt_of":  [ ... ],                        // "alternative spelling of X"
  "categories": [...], "topics": [...]
}
```

**Assumptions & quirks:**

- **`glosses` is a list.** When it has >1 element it's a hierarchy (broad → specific); join with "; " or keep the **last** (most specific) — decide once, apply consistently. Empty/whitespace glosses exist → skip the sense.
- **Drop pure form-of/alt-of senses** (`form_of`/`alt_of` present and gloss is just "plural of…/alternative of…"). They bloat the DB and add no dictionary value here (inflection isn't this pipeline's job). Keep a config flag to retain them if a later feature needs redirects.
- **`examples[].text`** is the display string. In non-English editions an `english` translation may accompany it; keep `text` (native) and optionally `english`. `ref` is a citation — keep optionally, low priority.
- **`tags`** are valuable for songwriters (flagging *slang/informal/archaic*). Persist them; they're a JSON array of short strings.
- **Sense-level `synonyms`/`antonyms`** can appear here in addition to entry level; merge both (§3.4).

### 3.3 `sounds[]` — pronunciation / IPA

```jsonc
"sounds": [
  {"ipa": "/ˈoʊ.ʃən/", "tags": ["US"]},
  {"ipa": "/ˈəʊ.ʃən/", "tags": ["UK","Received-Pronunciation"]},
  {"enpr": "ō′shən"},                       // enPR respelling — optional
  {"audio": "en-us-ocean.ogg", "ogg_url":"…", "mp3_url":"…"},  // audio — DROP the bytes
  {"rhymes": "-əʊʃən"}                       // Wiktionary rhyme key — keep if present (future rhyme engine)
]
```

**Assumptions & quirks:**

- **IPA is frequently missing** — expect a large null ratio (varies wildly by edition; can be >50% for less-common words). The app must render gracefully when absent. Measure the null ratio per pack in QA (§9).
- **Multiple IPAs per word, dialect-tagged.** Store *all* with their `tags`; let the app pick a preferred dialect (e.g. prefer `US` → `General-American` → first available). Do not collapse to one at build time.
- **Do not download audio.** We drop audio bytes; on-device pronunciation is the app's `AVSpeechSynthesizer` (TTS), not shipped files. Optionally keep the boolean "has audio" if useful — low priority.
- Keep `sounds[].rhymes` and `enpr` if present; cheap and future-proofing.

### 3.4 Linkage arrays — thesaurus (`synonyms`, `antonyms`, `related`, …)

```jsonc
"synonyms": [
  {"word": "sea", "tags": ["informal"], "sense": "body of salt water", "_dis": "…"},
  {"word": "the deep"}
]
```

**Assumptions & quirks:**

- Each item is an object with **`word`** (surface string) plus optional `sense` (a gloss hint tying it to a specific sense), `tags`, `_dis` (numeric sense-disambiguation), `roman`, `alt`. **We rely only on `word`**; keep `sense`/`tags` as optional context.
- **Targets are strings, not guaranteed to be headwords in the DB.** Do not build foreign keys between linkage targets and the word table. Store the target as text; the app resolves it lazily by looking the string up when the user taps it (mirrors how tappable chips work today).
- Synonyms can be **entry-level and sense-level**; merge and de-duplicate by `(rel_type, lower(word))`.
- Expect noise: self-references, multiword phrases, occasional residual wikitext (`[[link]]`, templates). Run a light cleanup: strip `[[ ]]`, collapse whitespace, drop empties and self-links, cap list length (e.g. 50) to bound size.

### 3.5 General text assumptions

- **UTF-8, normalize to NFC.** Non-Latin scripts and combining diacritics are expected in non-English packs.
- Wiktextract mostly strips wikitext, but assume residual markup can appear anywhere and run a shared sanitizer.
- Field presence is **not** guaranteed; every access must have a fallback. Never let one malformed line abort the build — count and log skips.

---

## 4. Pipeline stages

Language-agnostic; stages 2–6 run once per source edition.

1. **Acquire** — download the chosen edition's compressed JSONL from kaikki. Record URL, dump date, byte size, sha256 of the input. Never mutate the input.
2. **Stream-parse** — read line-by-line (`json.loads` per line; do **not** load the file into memory). Skip/log malformed lines. Filter to the target `lang_code`(s) for this pack.
3. **Normalize** — per entry, extract only the fields in §3, apply the sanitizer, NFC, folding, form-of dropping, list caps, dialect-tagged IPA retention. Emit intermediate normalized records (in-memory batched, or a temp table).
4. **Load into SQLite** — insert into the schema in §5 within a single transaction (or batched transactions). Deterministic insert order (sort by `word_folded, pos, sense ordinal`).
5. **Index & optimize** — create indexes (§5), `PRAGMA page_size` set **before** any table creation, `PRAGMA journal_mode=DELETE` (ship without WAL sidecars), then `VACUUM` to compact and produce a stable page layout.
6. **Emit artifact + metadata** — write `<lang>-<dumpdate>-schema<N>.sqlite`, compute row counts + coverage stats + sha256, compress to `.sqlite.zst`, and update the **catalog manifest** (§6).

**Tech:** Python 3 (the Wiktextract ecosystem is Python; stdlib `sqlite3` + `json` are sufficient; `zstandard` for compression). No heavy deps required. Keep the whole thing runnable as `python -m pipeline build --edition enwiktionary --lang en`.

---

## 5. Output schema — the app contract

This SQLite schema **is** the interface between pipeline and app. The app has no legacy struct to preserve (explicit product decision), so the model is designed around the data. Keep it stable; bump `schema_version` in `meta` on any breaking change.

```sql
-- Build-time pragmas (set page_size BEFORE creating tables):
--   PRAGMA page_size = 4096; PRAGMA journal_mode = DELETE;

CREATE TABLE meta (             -- one-row key/value pack metadata
  key   TEXT PRIMARY KEY,
  value TEXT
);  -- keys: schema_version, word_lang, definition_lang, source_edition,
    --       source_dump_date, pack_version, wiktextract_version, license, attribution, built_at

CREATE TABLE word (
  id            INTEGER PRIMARY KEY,
  word          TEXT NOT NULL,          -- surface form, NFC, verbatim
  word_folded   TEXT NOT NULL,          -- lowercased NFC for exact/prefix lookup
  word_search   TEXT NOT NULL,          -- diacritic-stripped fold for lenient search (never displayed)
  pos           TEXT NOT NULL,          -- normalized part of speech
  etym_index    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_word_folded ON word(word_folded);
CREATE INDEX idx_word_search ON word(word_search);

CREATE TABLE sense (
  id        INTEGER PRIMARY KEY,
  word_id   INTEGER NOT NULL REFERENCES word(id),
  ordinal   INTEGER NOT NULL,           -- display order within the word/pos
  gloss     TEXT NOT NULL,
  example   TEXT,                        -- primary example text (native)
  example_en TEXT,                       -- optional English translation (non-en packs)
  tags      TEXT                         -- JSON array of strings, e.g. ["slang"]
);
CREATE INDEX idx_sense_word ON sense(word_id, ordinal);

CREATE TABLE pronunciation (
  id        INTEGER PRIMARY KEY,
  word_id   INTEGER NOT NULL REFERENCES word(id),
  ipa       TEXT NOT NULL,
  dialects  TEXT,                         -- JSON array, e.g. ["US"]
  rhyme_key TEXT                          -- Wiktionary sounds[].rhymes if present (future use)
);
CREATE INDEX idx_pron_word ON pronunciation(word_id);

CREATE TABLE relation (
  id         INTEGER PRIMARY KEY,
  word_id    INTEGER NOT NULL REFERENCES word(id),
  rel_type   TEXT NOT NULL,               -- 'synonym' | 'antonym' | 'hypernym' | 'hyponym' | 'related' | 'derived'
  target     TEXT NOT NULL,               -- surface string; resolved lazily by lookup, NOT a FK
  target_folded TEXT NOT NULL,            -- for tap-through lookup
  sense_hint TEXT,                         -- optional gloss hint
  tags       TEXT                          -- JSON array
);
CREATE INDEX idx_rel_word ON relation(word_id, rel_type);
```

**Query patterns the app will use** (informs the indexes above):
- Exact lookup: `SELECT … FROM word WHERE word_folded = ?` then fan out to `sense`/`pronunciation`/`relation` by `word_id`, grouped by `pos`.
- Prefix/autocomplete: `WHERE word_folded LIKE ? || '%'` (index-backed) — **FTS is deliberately deferred** (see §8.3).
- Tap-through on a thesaurus chip: look up `relation.target_folded` as a new headword.

---

## 6. Multi-language packaging & distribution

**Each language is its own artifact.** No monolithic DB. One SQLite file per language pack, versioned and independently downloadable.

- **Naming:** `<lang_code>-<source_dump_date>-schema<N>.sqlite(.zst)`, e.g. `en-2026-07-06-schema1.sqlite.zst`.
- **Catalog manifest** — a single small JSON hosted on the CDN, the entry point the app fetches first:

```jsonc
{
  "manifest_version": 1,
  "generated_at": "2026-07-17",
  "packs": [
    {
      "lang_code": "en",
      "word_lang": "English",
      "definition_lang": "English",
      "source_edition": "enwiktionary",
      "latest": {
        "pack_version": "2026-07-06",
        "schema_version": 1,
        "url": "https://cdn.example/packs/en-2026-07-06-schema1.sqlite.zst",
        "compressed_size": 41234567,
        "uncompressed_size": 128000000,
        "sha256": "…",
        "row_counts": {"word": 850000, "sense": 1200000, "pronunciation": 300000, "relation": 900000},
        "ipa_coverage": 0.41,
        "license": "CC BY-SA 4.0",
        "attribution": "Wiktionary contributors; extracted via Wiktextract"
      },
      "deltas": [
        {"from": "2026-05-01", "to": "2026-07-06", "url": "…/en-2026-05-01_to_2026-07-06.patch.zst",
         "compressed_size": 3456789, "sha256": "…"}
      ]
    }
  ]
}
```

- **Distribution channel:** a **plain CDN + this manifest**, not iOS On-Demand Resources or App Thinning. Reason: ODR is iOS-only and awkward, and we need identical behavior on macOS. A manifest + HTTPS download works the same on both platforms and keeps full control over versioning and deltas.
- **Bundled vs downloaded:** optionally bundle the English pack in the app for zero-setup first run; download all other languages on demand. Bundled and downloaded packs use the identical schema and manifest metadata so the app treats them uniformly.

---

## 7. Delta updates

**Enabled by deterministic builds.** Two builds of the same source dump must be byte-identical; two builds of different dumps must differ only where the data differs. Requirements: fixed `page_size`, stable deterministic insert order, `VACUUM`, no timestamps inside data tables (build time lives only in `meta`, which can be excluded from the diff).

**Recommended delta = row-level semantic patch, not binary diff.**

- **Why not binary diff (bsdiff/xdelta) of the .sqlite files:** SQLite page layout shifts after inserts/`VACUUM`, so a small semantic change can rewrite many pages → large, brittle binary diffs. Avoid.
- **Natural key for diffing:** `(word_folded, pos, etym_index)` for `word`, and content hashes for child rows. **Do not rely on Wiktextract `sense.id` or our autoincrement `id`s** for identity across versions — sense ids are not stable across dumps and autoincrement ids reshuffle. Diff on content, not surrogate keys.
- **Patch format:** a small SQLite file (or JSONL) describing `upsert` / `delete` operations keyed by the natural key, applied by the app inside one transaction against a copy of the old pack, then atomically swapped in. Ship patches as `from_version → to_version`; the manifest lists available hops.
- **App chooses the cheapest path:** if a delta chain from the installed version exists and totals less than the full download, apply deltas; otherwise fetch the full pack. Always keep full packs available (first install, or when the installed version is too old to chain).
- **Verification:** every full pack and every patch carries a sha256 in the manifest; the app verifies after download and after applying a patch (a post-apply `PRAGMA integrity_check` + a recomputed content digest stored in `meta`).

Ship the row-level delta mechanism, but treat **"download the full pack"** as the always-present fallback so a broken/absent delta never blocks an update.

---

## 8. Cross-platform app consumption (constraints the pipeline must respect)

### 8.1 SQLite portability
SQLite database files are **portable across architecture and endianness**, so one built artifact serves Intel macOS, Apple-silicon macOS, and iOS with no per-arch builds. Keep the file format vanilla (no custom extensions, no encryption). The app reads via **GRDB.swift** over the system SQLite present on both platforms.

### 8.2 On-device storage location — must not sync via iCloud
The app's user data lives in an iCloud-synced folder and the app is **highly sensitive to anything touching that tree.** Reference packs are re-downloadable and must never enter it:
- Store downloaded/derived packs under **Application Support** (macOS) / the app container's **Library/Application Support** (iOS) — **not** Documents, and **not** inside the synced `Song Library` folder.
- Set `URLResourceValues.isExcludedFromBackup = true` on every pack file so packs don't bloat iCloud/iTunes backups.
- Open packs **read-only** (GRDB read-only config) so the runtime never writes into the pack (delta application writes to a temp copy then atomically renames).

### 8.3 FTS is deferred (matches the app plan)
The first cut needs only **exact + prefix** lookup, served by the `word_folded`/`word_search` indexes — no FTS5 in the shipped pack. Rationale: keeps the pack smaller, avoids any risk of a build-time FTS5 tokenizer/version mismatch with the on-device SQLite, and covers the actual UX (tap a word, or type a headword). If fuzzy search-as-you-type is wanted later, build the FTS index **on-device on first open** (portable, version-matched) rather than shipping it. The pipeline should leave that option open by keeping the base tables FTS-friendly (a single text column per searchable field).

### 8.4 Size budgets
- Target compressed pack sizes so an on-demand language download is quick and a bundled English pack is acceptable in the app binary. Working budget: **English ≈ 40–150 MB uncompressed** depending on how much is kept (examples + relations dominate). Provide build flags to trim aggressively: drop examples, drop `related`/`derived`, cap relation lists, drop rare/obscure `tags`-only senses.
- Report both compressed and uncompressed sizes per pack in the manifest so product can set per-platform policy (e.g. bundle English on macOS, download-only on iOS).

### 8.5 Graceful-degradation contract
The app must render when data is partial, because it will be: **IPA often absent**, examples often absent, thesaurus relations often sparse. The pipeline guarantees only that `word` + at least one `sense.gloss` exist for every stored headword; everything else is optional. Any headword that would have zero senses after filtering is **not** emitted.

---

## 9. Validation / QA (run before publishing any pack)

- **Schema/version:** `meta` populated; `schema_version` matches this doc; `PRAGMA integrity_check` clean.
- **Spot-check known words** per language (e.g. en: "ocean", "run", "love", plus a slang term and an archaic term) — verify senses, POS grouping, IPA, synonyms look right.
- **Coverage metrics** (also written to the manifest): total headwords; % of headwords with ≥1 IPA; % with ≥1 synonym; avg senses/word; count of dropped/malformed input lines.
- **Determinism:** build twice from the same input → identical sha256 (excluding `meta.built_at`). This gates the delta mechanism.
- **Size:** compressed/uncompressed within budget (§8.4).
- **Delta round-trip:** apply `old → new` patch to the old pack and confirm the result's content digest equals the freshly built new pack.
- **Sanitizer:** sample N rows across tables for residual wikitext/templates; zero tolerance for `[[`, `{{`, or stray HTML in shipped text.

---

## 10. Open decisions to confirm before/while building

1. **Language model (A vs B, §2.1):** confirm native-language definitions (Approach A) as default; decide whether to also produce English-glossed packs for non-English words.
2. **Gloss hierarchy (§3.2):** join multi-element `glosses` vs keep last-most-specific — pick one.
3. **Keep examples?** Big size lever and high songwriter value — likely yes for the default pack, but confirm the size trade.
4. **Etymology:** keep a trimmed snippet or drop entirely? (Leaning drop for size.)
5. **Relation types to ship:** synonyms + antonyms are core; are hypernyms/hyponyms/related wanted in the thesaurus panel, or noise?
6. **Initial language set** and which (if any) is bundled in the app vs download-only.
7. **CDN host** for packs + manifest, and the URL scheme.

---

## Appendix — quick reference of relied-upon Wiktextract fields

| Purpose | Path | Notes |
|---|---|---|
| Headword | `word` | surface string; NFC; may have spaces/diacritics |
| Language key | `lang_code` | filter/partition; fallback from edition |
| Part of speech | `pos` | open vocabulary; normalize |
| Definition | `senses[].glosses` (fallback `raw_glosses`) | list; join or take last |
| Example | `senses[].examples[].text` (+ `.english`) | optional |
| Usage register | `senses[].tags` | slang/informal/archaic etc. |
| Drop inflections | `senses[].form_of` / `alt_of` | skip pure form-of senses |
| IPA | `sounds[].ipa` (+ `.tags`) | often absent; keep all, dialect-tagged |
| Rhyme key (future) | `sounds[].rhymes`, `sounds[].enpr` | keep if present |
| Thesaurus | `synonyms[].word`, `antonyms[].word`, `related[]`, `hypernyms[]`, `hyponyms[]`, `derived[]` | + sense/sense-level; targets are strings, resolve lazily |
| Ignore | `translations`, `forms`, `head_templates`, `categories`, `audio bytes` | — |

# Integrating an SGCorpus pack into Song Garden

**Audience:** an LLM (or engineer) wiring a built `.sqlite` pack into the Song
Garden macOS/iOS app — specifically the reference inspector's **Dictionary** and
**Thesaurus** panels (`InspectorPanel.dictionary` / `.thesaurus`; the `.rhyme`
panel already has its own offline CMUdict engine and does **not** use these
packs).

This document is the app-side contract. The pipeline guarantees the schema and
storage-format properties described here; the app must honor the storage-location
and read-only rules, which are load-bearing given the app's iCloud sensitivity.

---

## 1. What the artifact is

- A single **read-only SQLite file per language**: `<lang>-<dumpdate>-schema<N>.sqlite`
  (e.g. `en-2026-07-16-schema1.sqlite`), optionally shipped compressed as `.xz`
  (LZMA — decoded natively on Apple platforms, no third-party codec needed).
- **Portable across architecture and endianness** — one file serves Intel mac,
  Apple-silicon mac, and iOS. No per-arch builds, no encryption, no custom
  extensions.
- **Vanilla format**: `journal_mode=DELETE` (no `-wal`/`-shm` sidecars),
  `page_size=4096`, already `VACUUM`ed.
- Tables: `meta`, `word`, `sense`, `pronunciation`, `relation`. Full DDL below.
- `schema_version` lives in `meta`; the app must check it and refuse a pack whose
  version it doesn't understand. **This doc describes `schema_version = 1`.**

Produce one locally with:

```bash
uv run sgcorpus build --lang en          # -> build/en-<dumpdate>-schema1.sqlite(.xz)
uv run sgcorpus verify build/en-*.sqlite --spot-check ocean run love
```

---

## 2. Schema (the query contract)

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- keys: schema_version, word_lang, definition_lang, source_edition,
--       source_dump_date, pack_version, wiktextract_version,
--       license, attribution, built_at, content_digest

CREATE TABLE word (
  id          INTEGER PRIMARY KEY,
  word        TEXT NOT NULL,   -- surface form, NFC, verbatim — DISPLAY THIS
  word_folded TEXT NOT NULL,   -- lowercased NFC — exact/prefix lookup key
  word_search TEXT NOT NULL,   -- diacritic-stripped fold — lenient search, never display
  pos         TEXT NOT NULL,   -- normalized part of speech (noun/verb/adjective/…)
  etym_index  INTEGER NOT NULL DEFAULT 0  -- disambiguates same (word,pos), N etymologies
);
CREATE INDEX idx_word_folded ON word(word_folded);
CREATE INDEX idx_word_search ON word(word_search);

CREATE TABLE sense (
  id         INTEGER PRIMARY KEY,
  word_id    INTEGER NOT NULL,   -- -> word.id
  ordinal    INTEGER NOT NULL,   -- display order within the word/pos
  gloss      TEXT NOT NULL,      -- the definition — always present
  example    TEXT,               -- usage example (native language); often NULL
  example_en TEXT,               -- English translation of example (non-en packs); often NULL
  tags       TEXT                -- JSON array of strings, e.g. ["slang","obsolete"]; may be NULL
);
CREATE INDEX idx_sense_word ON sense(word_id, ordinal);

CREATE TABLE pronunciation (
  id        INTEGER PRIMARY KEY,
  word_id   INTEGER NOT NULL,   -- -> word.id
  ipa       TEXT NOT NULL,      -- IPA string (may be "" if only a rhyme_key exists)
  dialects  TEXT,               -- JSON array, e.g. ["US"] or ["Received-Pronunciation"]; may be NULL
  rhyme_key TEXT                -- Wiktionary rhyme key if present; usually NULL (future use)
);
CREATE INDEX idx_pron_word ON pronunciation(word_id);

CREATE TABLE relation (
  id            INTEGER PRIMARY KEY,
  word_id       INTEGER NOT NULL,  -- -> word.id
  rel_type      TEXT NOT NULL,     -- 'synonym'|'antonym'|'hypernym'|'related'|'derived'|'form_of'
  target        TEXT NOT NULL,     -- surface string — DISPLAY THIS on the chip (form_of: the lemma)
  target_folded TEXT NOT NULL,     -- lookup key for tap-through (NOT a foreign key)
  sense_hint    TEXT,              -- optional gloss hint; for form_of, the "plural of X"-style descriptor
  tags          TEXT               -- JSON array; may be NULL
);
CREATE INDEX idx_rel_word ON relation(word_id, rel_type);
```

**Identity rules the app must respect:**
- A headword is `word_folded`. One headword fans out to multiple `word` rows —
  one per `(pos, etym_index)`. Group results by `pos` for display.
- `relation.target` is a *string*, deliberately **not** a foreign key. Resolve it
  lazily: when the user taps a synonym chip, look up `target_folded` as a new
  headword (it may or may not exist in the pack — handle "not found" gracefully).

### Canonical queries

```sql
-- Exact lookup (the primary path): tap a word / type a headword.
SELECT id, word, pos, etym_index FROM word WHERE word_folded = ? ORDER BY pos, etym_index;

-- Then fan out per word_id:
SELECT ordinal, gloss, example, example_en, tags FROM sense        WHERE word_id = ? ORDER BY ordinal;
SELECT ipa, dialects, rhyme_key                  FROM pronunciation WHERE word_id = ?;
SELECT rel_type, target, target_folded, sense_hint, tags FROM relation WHERE word_id = ? ORDER BY rel_type;

-- Prefix / autocomplete (index-backed; FTS is intentionally NOT shipped):
SELECT DISTINCT word FROM word WHERE word_folded LIKE ? || '%' ORDER BY word_folded LIMIT 25;

-- Lenient, diacritic-insensitive search (user typed "cafe", pack has "café"):
SELECT DISTINCT word FROM word WHERE word_search LIKE ? || '%' ORDER BY word_search LIMIT 25;
```

Fold the user's query string the same way the pipeline does before binding it:
lowercase + NFC for `word_folded`; additionally strip combining marks for
`word_search`.

---

## 3. Where the pack lives on disk — non-negotiable

The app is highly sensitive to anything touching the iCloud-synced `Song Library`
tree. Reference packs are re-downloadable data and **must never enter it.**

- Store packs under **Application Support** (`FileManager.default.url(for:
  .applicationSupportDirectory, ...)`), in a `ReferencePacks/` subdirectory —
  **not** `Documents`, **not** inside `Song Library`.
- Set `URLResourceValues.isExcludedFromBackup = true` on every pack file, so packs
  don't bloat iCloud/device backups.
- Open **read-only** (see GRDB config below) so the runtime never writes into the
  pack. Delta application (if used) writes to a temp copy and atomically renames.

```swift
func referencePacksDirectory() throws -> URL {
    let base = try FileManager.default.url(
        for: .applicationSupportDirectory, in: .userDomainMask,
        appropriateFor: nil, create: true)
    let dir = base.appendingPathComponent("ReferencePacks", isDirectory: true)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir
}

func excludeFromBackup(_ url: URL) throws {
    var u = url
    var values = URLResourceValues()
    values.isExcludedFromBackup = true
    try u.setResourceValues(values)
}
```

---

## 4. Reading it with GRDB

The plan commits the app to **GRDB.swift** over the system SQLite. Add the SPM
dependency if not already present, then open each pack read-only.

```swift
import GRDB

struct ReferencePack {
    let dbQueue: DatabaseQueue
    let meta: [String: String]

    init(path: String) throws {
        var config = Configuration()
        config.readonly = true              // never write into the pack
        self.dbQueue = try DatabaseQueue(path: path, configuration: config)
        self.meta = try dbQueue.read { db in
            try Row.fetchAll(db, sql: "SELECT key, value FROM meta")
                .reduce(into: [:]) { $0[$1["key"]] = $1["value"] }
        }
        guard meta["schema_version"] == "1" else {
            throw ReferenceError.unsupportedSchema(meta["schema_version"] ?? "?")
        }
    }
}
```

Record types and a lookup that returns everything the Dictionary/Thesaurus panels
need for one headword:

```swift
struct WordEntry: Identifiable {          // one (pos, etymology) block
    let id: Int64
    let word: String
    let pos: String
    var senses: [Sense] = []
    var pronunciations: [Pronunciation] = []
    var relations: [Relation] = []
}
struct Sense { let gloss: String; let example: String?; let exampleEN: String?; let tags: [String] }
struct Pronunciation { let ipa: String; let dialects: [String]; let rhymeKey: String? }
struct Relation { let type: String; let target: String; let targetFolded: String; let senseHint: String? }

extension ReferencePack {
    /// All entries for a headword, grouped by (pos, etym_index).
    func lookup(_ query: String) throws -> [WordEntry] {
        let folded = query.folding(options: [], locale: nil).lowercased()  // NFC + lowercase
        return try dbQueue.read { db in
            let words = try Row.fetchAll(db, sql: """
                SELECT id, word, pos FROM word WHERE word_folded = ?
                ORDER BY pos, etym_index
                """, arguments: [folded])
            return try words.map { row in
                let wid: Int64 = row["id"]
                var e = WordEntry(id: wid, word: row["word"], pos: row["pos"])
                e.senses = try Row.fetchAll(db, sql:
                    "SELECT gloss, example, example_en, tags FROM sense WHERE word_id = ? ORDER BY ordinal",
                    arguments: [wid]).map {
                    Sense(gloss: $0["gloss"], example: $0["example"], exampleEN: $0["example_en"],
                          tags: decodeJSONArray($0["tags"]))
                }
                e.pronunciations = try Row.fetchAll(db, sql:
                    "SELECT ipa, dialects, rhyme_key FROM pronunciation WHERE word_id = ?",
                    arguments: [wid]).compactMap {
                    let ipa: String = $0["ipa"]
                    guard !ipa.isEmpty else { return nil }   // skip rhyme-only rows in the dictionary UI
                    return Pronunciation(ipa: ipa, dialects: decodeJSONArray($0["dialects"]),
                                         rhymeKey: $0["rhyme_key"])
                }
                e.relations = try Row.fetchAll(db, sql:
                    "SELECT rel_type, target, target_folded, sense_hint FROM relation WHERE word_id = ? ORDER BY rel_type",
                    arguments: [wid]).map {
                    Relation(type: $0["rel_type"], target: $0["target"],
                             targetFolded: $0["target_folded"], senseHint: $0["sense_hint"])
                }
                return e
            }
        }
    }

    func completions(prefix: String, limit: Int = 25) throws -> [String] {
        let folded = prefix.folding(options: [], locale: nil).lowercased()
        return try dbQueue.read { db in
            try String.fetchAll(db, sql: """
                SELECT DISTINCT word FROM word WHERE word_folded LIKE ? ORDER BY word_folded LIMIT ?
                """, arguments: ["\(folded)%", limit])
        }
    }
}

func decodeJSONArray(_ value: String?) -> [String] {
    guard let v = value, let data = v.data(using: .utf8),
          let arr = try? JSONDecoder().decode([String].self, from: data) else { return [] }
    return arr
}
```

### Wiring into the panels

- **Dictionary panel**: `lookup(query)` → render each `WordEntry` as a POS-grouped
  block; senses in order; show `tags` as small register badges (slang/archaic/…);
  show `pronunciations` (prefer a dialect via `dialects`, e.g. US → General-American
  → first). Example lines under each sense when present.
- **Thesaurus panel**: from the same `WordEntry.relations`, render synonym/antonym
  chips (and optionally hypernym/related). Each chip's label is `target`;
  tapping it calls `lookup(targetFolded)` — the existing tap-through behavior.
- **Inflections (`form_of`)**: a plural or conjugated headword ("mountains") carries
  a `form_of` relation whose `target` is the lemma ("mountain") and whose
  `sense_hint` is a descriptor ("plural of mountain"). Offer the lemma as a
  tap-through; when the inflected form has no sense of its own (a pure stub), show
  the lemma's definition in its place.

---

## 5. Graceful degradation — the pipeline only guarantees `word` + ≥1 `gloss`

Everything else is frequently absent and the UI **must** render without it:
- **IPA** is often missing (coverage varies widely by word/edition). No pronunciation row ⇒ just omit the pronunciation line.
- **Examples** are often absent ⇒ omit the example line.
- **Relations** are often sparse or empty ⇒ show "No synonyms" rather than an error.
- A tapped relation `target` may not resolve to any headword ⇒ show "not found", don't crash.

Never assume a field is present. Any headword that would have zero senses is not
emitted, so `word` + at least one `sense.gloss` is the only safe invariant.

---

## 6. Getting packs onto the device

Two supported paths; both use the identical schema, so app code treats them the same.

### Bundled (recommended for first-run English, zero setup)
Ship the **uncompressed** `.sqlite` in the app bundle and open it read-only in
place (or copy it out to Application Support on first launch if you want a uniform
read/relocate path). Don't compress the bundled pack: the App Store already
compresses the app for delivery, and you need the file uncompressed at runtime
anyway — compressing it in-bundle just adds a first-launch decompress step for no
download savings. Build it with `--no-compress`. If the uncompressed size is too
big to bundle, trim *content* (`--no-examples`, or `--relations synonym antonym`),
not compression.

### Downloaded on demand (other languages)
1. Fetch the catalog manifest (`catalog.json`, produced by `sgcorpus manifest`) —
   see its shape in `docs/PLAN.md` §6. It lists, per `lang_code`, the `latest`
   pack URL, `sha256`, sizes, and coverage stats.
2. Download `latest.url`, **verify `sha256`** before use.
3. Decompress the `.xz` (LZMA) — natively, no dependency:

```swift
import Compression  // or AppleArchive

// Streaming LZMA decode of a downloaded .xz into the destination .sqlite.
func decompressXZ(at src: URL, to dst: URL) throws {
    let input = try FileHandle(forReadingFrom: src)
    FileManager.default.createFile(atPath: dst.path, contents: nil)
    let output = try FileHandle(forWritingTo: dst)
    defer { try? input.close(); try? output.close() }

    let filter = try InputFilter(.decompress, using: .lzma) { _ in
        input.readData(ofLength: 1 << 16)
    }
    while let chunk = try filter.readData(ofLength: 1 << 16) {
        output.write(chunk)
    }
}
```

4. Move into `ReferencePacks/`, set `isExcludedFromBackup = true`, open read-only.
5. Run `PRAGMA integrity_check` once after install; optionally recompute and
   compare `meta.content_digest`.

**Why LZMA/.xz:** it decodes natively on Apple platforms (`Compression`'s
`.lzma`, or `AppleArchive`) so the app needs **no third-party codec**, and it
compresses this text-heavy data ~13% smaller than zstd. Verify the `sha256`
against the *compressed* artifact from the manifest before decompressing.

### Delta updates (optional, later)
The pipeline can emit row-level patches (`sgcorpus delta`) keyed on
`(word_folded, pos, etym_index)`. The app applies one to a *copy* of the installed
pack in a temp file, verifies the resulting `content_digest` equals the manifest's,
then atomically swaps it in. **The full pack download is always the fallback** — a
missing or broken delta must never block an update. See `docs/PLAN.md` §7.

---

## 7. Attribution — required

The lexical data is **CC BY-SA 4.0** (from Wiktionary). The app must surface
attribution. Read it straight from the pack:

```swift
let license     = pack.meta["license"]     // "CC BY-SA 4.0"
let attribution = pack.meta["attribution"] // "Wiktionary contributors; extracted via Wiktextract (kaikki.org)"
```

Show these on an acknowledgements/about screen alongside the pack's
`source_edition` and `source_dump_date`.

---

## 8. Quick checklist for the integrating agent

- [ ] Add GRDB (SPM) if absent.
- [ ] `ReferencePack` type: open read-only, validate `meta.schema_version == 1`.
- [ ] Store packs in Application Support `ReferencePacks/`, `isExcludedFromBackup = true`, never in `Song Library`/`Documents`.
- [ ] `lookup(_:)`, `completions(prefix:)` fold queries to match `word_folded`/`word_search`.
- [ ] Dictionary panel: POS-grouped senses + tags + preferred-dialect IPA, all optional.
- [ ] Thesaurus panel: relation chips with lazy tap-through via `target_folded`.
- [ ] Every optional field guarded (§5).
- [ ] First-run: bundled English pack (uncompressed) OR manifest-driven download with sha256 verify.
- [ ] Attribution shown from `meta`.

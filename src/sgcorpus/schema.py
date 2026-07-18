"""The output SQLite schema — the contract between pipeline and app (plan §5).

Keep this stable. Any breaking change bumps :data:`sgcorpus.SCHEMA_VERSION`.
"""

from __future__ import annotations

import sqlite3

from . import SCHEMA_VERSION

# meta keys written by the build (plan §5).
META_KEYS = (
    "schema_version",
    "word_lang",
    "definition_lang",
    "source_edition",
    "source_dump_date",
    "pack_version",
    "wiktextract_version",
    "license",
    "attribution",
    "built_at",
    "content_digest",  # deterministic digest of data rows (plan §7 verification)
)

SCHEMA_SQL = """
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE word (
  id            INTEGER PRIMARY KEY,
  word          TEXT NOT NULL,          -- surface form, NFC, verbatim
  word_folded   TEXT NOT NULL,          -- lowercased NFC for exact/prefix lookup
  word_search   TEXT NOT NULL,          -- diacritic-stripped fold (never displayed)
  pos           TEXT NOT NULL,          -- normalized part of speech
  etym_index    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sense (
  id         INTEGER PRIMARY KEY,
  word_id    INTEGER NOT NULL REFERENCES word(id),
  ordinal    INTEGER NOT NULL,          -- display order within the word/pos
  gloss      TEXT NOT NULL,
  example    TEXT,                       -- primary example text (native)
  example_en TEXT,                       -- optional English translation (non-en packs)
  tags       TEXT                        -- JSON array of strings, e.g. ["slang"]
);

CREATE TABLE pronunciation (
  id        INTEGER PRIMARY KEY,
  word_id   INTEGER NOT NULL REFERENCES word(id),
  ipa       TEXT NOT NULL,
  dialects  TEXT,                         -- JSON array, e.g. ["US"]
  rhyme_key TEXT                          -- Wiktionary sounds[].rhymes if present
);

CREATE TABLE relation (
  id            INTEGER PRIMARY KEY,
  word_id       INTEGER NOT NULL REFERENCES word(id),
  rel_type      TEXT NOT NULL,            -- synonym | antonym | hypernym | ...
  target        TEXT NOT NULL,            -- surface string; resolved lazily, NOT a FK
  target_folded TEXT NOT NULL,            -- for tap-through lookup
  sense_hint    TEXT,                     -- optional gloss hint
  tags          TEXT                      -- JSON array
);
"""

INDEX_SQL = """
CREATE INDEX idx_word_folded ON word(word_folded);
CREATE INDEX idx_word_search ON word(word_search);
CREATE INDEX idx_sense_word  ON sense(word_id, ordinal);
CREATE INDEX idx_pron_word   ON pronunciation(word_id);
CREATE INDEX idx_rel_word    ON relation(word_id, rel_type);
"""


def open_build_db(path: str, page_size: int) -> sqlite3.Connection:
    """Open a fresh DB with build-time pragmas set BEFORE any table exists.

    ``page_size`` must be set before table creation to take effect; WAL is
    disabled so the shipped file has no ``-wal``/``-shm`` sidecars (plan §5).
    """
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA page_size = {int(page_size)};")
    conn.execute("PRAGMA journal_mode = DELETE;")
    conn.execute("PRAGMA foreign_keys = OFF;")  # deterministic bulk load
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEX_SQL)


def write_meta(conn: sqlite3.Connection, meta: dict[str, str]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        [(k, str(v)) for k, v in meta.items() if v is not None],
    )


def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {k: v for k, v in conn.execute("SELECT key, value FROM meta")}


def assert_schema_version(conn: sqlite3.Connection) -> None:
    meta = read_meta(conn)
    got = meta.get("schema_version")
    if got != str(SCHEMA_VERSION):
        raise ValueError(
            f"schema_version mismatch: pack={got} expected={SCHEMA_VERSION}"
        )

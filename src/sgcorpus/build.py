"""Stages 2-6 — stream-parse → normalize → load → index → emit (plan §4).

Produces a deterministic, read-only SQLite pack plus a ``.meta.json`` sidecar
(the manifest ingests these). Determinism (plan §7): fixed page_size, stable
insert order, ``built_at`` derived from the source dump date (not wall-clock),
no other timestamps in data tables, ``VACUUM`` to a stable page layout.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sqlite3
from dataclasses import dataclass

from . import SCHEMA_VERSION
from .config import BuildConfig, SourceSpec
from .normalize import NormWord, iter_entries, normalize_entry
from . import schema

LICENSE = "CC BY-SA 4.0"
ATTRIBUTION = "Wiktionary contributors; extracted via Wiktextract (kaikki.org)"


@dataclass
class BuildStats:
    """Coverage/QA metrics (plan §9), mirrored into the manifest."""

    lines_read: int = 0
    lines_malformed: int = 0
    entries_skipped_lang: int = 0
    entries_skipped_nolang: int = 0
    entries_skipped_empty: int = 0
    words: int = 0
    senses: int = 0
    pronunciations: int = 0
    relations: int = 0
    words_with_ipa: int = 0
    words_with_synonym: int = 0

    @property
    def ipa_coverage(self) -> float:
        return round(self.words_with_ipa / self.words, 4) if self.words else 0.0

    @property
    def synonym_coverage(self) -> float:
        return round(self.words_with_synonym / self.words, 4) if self.words else 0.0

    @property
    def avg_senses(self) -> float:
        return round(self.senses / self.words, 3) if self.words else 0.0


def open_text(path: str) -> io.TextIOBase:
    """Open a JSONL source, transparently decompressing ``.gz``."""
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8")
    return open(path, "r", encoding="utf-8")


_STAGE_BATCH = 5000


def _serialize_children(nw: NormWord) -> str:
    """Pack a NormWord's senses/pronunciations/relations into one JSON payload
    for the staging table (the sort-key columns are stored separately)."""
    return json.dumps(
        {
            "s": [[s.ordinal, s.gloss, s.example, s.example_en, s.tags] for s in nw.senses],
            "p": [[p.ipa, p.dialects, p.rhyme_key] for p in nw.pronunciations],
            "r": [[r.rel_type, r.target, r.target_folded, r.sense_hint, r.tags]
                  for r in nw.relations],
        },
        ensure_ascii=False,
    )


def _stage_entries(
    path: str, cfg: BuildConfig, target_lang: str, stats: BuildStats,
    stage: sqlite3.Connection,
) -> None:
    """Stream the source line-by-line into a disk-backed staging table.

    Memory stays bounded (one batch of rows) regardless of source size — the
    full English dump never lives in RAM. Language filtering is done here, the
    single authority: an entry is kept only when its ``lang_code`` is present and
    equals ``target_lang`` (a missing ``lang_code`` is an unknown language and is
    dropped, never blindly emitted — plan §3.1). ``seq`` records file order so the
    later ORDER BY has a deterministic tie-breaker.
    """
    stage.execute(
        "CREATE TABLE stage (seq INTEGER PRIMARY KEY, word_search TEXT, "
        "word_folded TEXT, word TEXT, pos TEXT, payload TEXT)"
    )
    batch: list[tuple] = []
    seq = 0

    def flush() -> None:
        if batch:
            stage.executemany(
                "INSERT INTO stage(seq, word_search, word_folded, word, pos, payload) "
                "VALUES (?,?,?,?,?,?)",
                batch,
            )
            batch.clear()

    with open_text(path) as f:
        for obj, err in iter_entries(f):
            stats.lines_read += 1
            if err:
                stats.lines_malformed += 1
                continue
            if target_lang:
                lc = obj.get("lang_code")
                if not lc:
                    stats.entries_skipped_nolang += 1
                    continue
                if lc != target_lang:
                    stats.entries_skipped_lang += 1
                    continue
            nw = normalize_entry(obj, cfg)
            if nw is None:
                stats.entries_skipped_empty += 1
                continue
            seq += 1
            batch.append(
                (seq, nw.word_search, nw.word_folded, nw.word, nw.pos, _serialize_children(nw))
            )
            if len(batch) >= _STAGE_BATCH:
                flush()
    flush()


def _load_staged(
    conn: sqlite3.Connection, stage: sqlite3.Connection, stats: BuildStats
) -> None:
    """Stream staged rows in deterministic order into the final schema.

    The staging ORDER BY is disk-backed (SQLite external sort), so this pass is
    also memory-bounded. Entries sharing ``(word_folded, pos)`` are contiguous in
    this order (identical word_folded implies identical word_search), so
    ``etym_index`` is assigned in one pass by tracking the previous key.
    """
    schema.create_schema(conn)
    word_id = sense_id = pron_id = rel_id = 0
    prev_key: tuple[str, str] | None = None
    etym = 0

    cur = stage.execute(
        "SELECT word_search, word_folded, word, pos, payload FROM stage "
        "ORDER BY word_search, word_folded, word, pos, seq"
    )
    for word_search, word_folded, word, pos, payload in cur:
        key = (word_folded, pos)
        etym = etym + 1 if key == prev_key else 0
        prev_key = key

        word_id += 1
        conn.execute(
            "INSERT INTO word(id, word, word_folded, word_search, pos, etym_index) "
            "VALUES (?,?,?,?,?,?)",
            (word_id, word, word_folded, word_search, pos, etym),
        )
        stats.words += 1
        children = json.loads(payload)

        for ordv, gloss, ex, exen, tag_list in children["s"]:
            sense_id += 1
            tags = json.dumps(tag_list, ensure_ascii=False) if tag_list else None
            conn.execute(
                "INSERT INTO sense(id, word_id, ordinal, gloss, example, example_en, tags) "
                "VALUES (?,?,?,?,?,?,?)",
                (sense_id, word_id, ordv, gloss, ex, exen, tags),
            )
            stats.senses += 1

        has_ipa = False
        for ipa, dialect_list, rk in children["p"]:
            pron_id += 1
            dialects = json.dumps(dialect_list, ensure_ascii=False) if dialect_list else None
            conn.execute(
                "INSERT INTO pronunciation(id, word_id, ipa, dialects, rhyme_key) "
                "VALUES (?,?,?,?,?)",
                (pron_id, word_id, ipa, dialects, rk),
            )
            stats.pronunciations += 1
            if ipa:
                has_ipa = True
        if has_ipa:
            stats.words_with_ipa += 1

        has_syn = False
        for rel_type, target, target_folded, sense_hint, tag_list in children["r"]:
            rel_id += 1
            tags = json.dumps(tag_list, ensure_ascii=False) if tag_list else None
            conn.execute(
                "INSERT INTO relation(id, word_id, rel_type, target, target_folded, sense_hint, tags) "
                "VALUES (?,?,?,?,?,?,?)",
                (rel_id, word_id, rel_type, target, target_folded, sense_hint, tags),
            )
            stats.relations += 1
            if rel_type == "synonym":
                has_syn = True
        if has_syn:
            stats.words_with_synonym += 1


def compute_content_digest(conn: sqlite3.Connection) -> str:
    """Canonical, order-independent digest of the pack's data (plan §7).

    Keyed on natural identity ``(word_folded, pos, etym_index)`` and row content
    — never surrogate ``id``s or insert order — so a delta-applied pack and a
    freshly-built one of the same dump produce the same digest.
    """
    lines: list[str] = []
    for wid, word, wf, pos, etym in conn.execute(
        "SELECT id, word, word_folded, pos, etym_index FROM word"
    ):
        parts = [f"W|{wf}|{pos}|{etym}|{word}"]
        for ordv, gloss, ex, exen, tags in conn.execute(
            "SELECT ordinal, gloss, example, example_en, tags FROM sense "
            "WHERE word_id=? ORDER BY ordinal",
            (wid,),
        ):
            parts.append(f"S|{ordv}|{gloss}|{ex or ''}|{exen or ''}|{tags or ''}")
        prons = [
            f"P|{ipa}|{dia or ''}|{rk or ''}"
            for ipa, dia, rk in conn.execute(
                "SELECT ipa, dialects, rhyme_key FROM pronunciation WHERE word_id=?",
                (wid,),
            )
        ]
        parts.extend(sorted(prons))
        rels = [
            f"R|{rt}|{tf}|{sh or ''}|{tags or ''}"
            for rt, tf, sh, tags in conn.execute(
                "SELECT rel_type, target_folded, sense_hint, tags FROM relation WHERE word_id=?",
                (wid,),
            )
        ]
        parts.extend(sorted(rels))
        lines.append("\n".join(parts))

    lines.sort()
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode())
        h.update(b"\x1e")
    return h.hexdigest()


@dataclass
class BuildResult:
    sqlite_path: str
    compressed_path: str
    meta_path: str
    sha256: str
    compressed_sha256: str
    stats: BuildStats
    meta: dict


def build_pack(
    input_path: str,
    out_dir: str,
    source: SourceSpec,
    *,
    cfg: BuildConfig | None = None,
    dump_date: str | None = None,
    wiktextract_version: str = "unknown",
    compress: bool = True,
) -> BuildResult:
    cfg = cfg or BuildConfig()
    os.makedirs(out_dir, exist_ok=True)
    dump_date = dump_date or "unknown"
    pack_version = dump_date

    stats = BuildStats()

    base = f"{source.lang_code}-{pack_version}-schema{SCHEMA_VERSION}"
    sqlite_path = os.path.join(out_dir, base + ".sqlite")
    stage_path = sqlite_path + ".stage"
    for p in (sqlite_path, stage_path):
        if os.path.exists(p):
            os.remove(p)

    # Stage the whole (filtered, normalized) corpus to disk, then stream it in
    # sorted order into the final schema. Both passes are memory-bounded, so the
    # multi-GB English dump never lives in RAM (plan §4.2/§4.3).
    stage = sqlite3.connect(stage_path)
    try:
        stage.execute("PRAGMA journal_mode = OFF;")  # throwaway; rebuilt each run
        stage.execute("BEGIN")
        _stage_entries(input_path, cfg, source.lang_code, stats, stage)
        stage.execute("COMMIT")

        conn = schema.open_build_db(sqlite_path, cfg.page_size)
        try:
            conn.execute("BEGIN")
            _load_staged(conn, stage, stats)
            conn.execute("COMMIT")
            schema.create_indexes(conn)
            content_digest = compute_content_digest(conn)
            # built_at is derived from the dump date, NOT wall-clock, so two
            # builds of the same source dump are byte-identical (plan §7).
            meta = {
                "schema_version": SCHEMA_VERSION,
                "word_lang": source.word_lang,
                "definition_lang": source.definition_lang,
                "source_edition": source.edition,
                "source_dump_date": dump_date,
                "pack_version": pack_version,
                "wiktextract_version": wiktextract_version,
                "license": LICENSE,
                "attribution": ATTRIBUTION,
                "built_at": dump_date,
                "content_digest": content_digest,
            }
            schema.write_meta(conn, meta)
            conn.commit()
            conn.execute("PRAGMA optimize;")
            conn.execute("VACUUM;")
            conn.commit()
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"integrity_check failed: {integrity}")
        finally:
            conn.close()
    finally:
        stage.close()
        if os.path.exists(stage_path):
            os.remove(stage_path)

    sha = _sha256_file(sqlite_path)
    uncompressed_size = os.path.getsize(sqlite_path)

    compressed_path = ""
    compressed_sha = ""
    compressed_size = 0
    if compress:
        compressed_path = sqlite_path + ".xz"
        _compress_xz(sqlite_path, compressed_path)
        compressed_sha = _sha256_file(compressed_path)
        compressed_size = os.path.getsize(compressed_path)

    meta_out = {
        "lang_code": source.lang_code,
        "word_lang": source.word_lang,
        "definition_lang": source.definition_lang,
        "source_edition": source.edition,
        "pack_version": pack_version,
        "schema_version": SCHEMA_VERSION,
        "source_dump_date": dump_date,
        "sha256": sha,
        "uncompressed_size": uncompressed_size,
        "compressed_sha256": compressed_sha,
        "compressed_size": compressed_size,
        "content_digest": content_digest,
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "row_counts": {
            "word": stats.words,
            "sense": stats.senses,
            "pronunciation": stats.pronunciations,
            "relation": stats.relations,
        },
        "ipa_coverage": stats.ipa_coverage,
        "synonym_coverage": stats.synonym_coverage,
        "avg_senses": stats.avg_senses,
        "qa": {
            "lines_read": stats.lines_read,
            "lines_malformed": stats.lines_malformed,
            "entries_skipped_lang": stats.entries_skipped_lang,
            "entries_skipped_nolang": stats.entries_skipped_nolang,
            "entries_skipped_empty": stats.entries_skipped_empty,
        },
        "artifact": os.path.basename(compressed_path or sqlite_path),
    }
    meta_path = os.path.join(out_dir, base + ".meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta_out, f, indent=2, ensure_ascii=False, sort_keys=True)

    return BuildResult(
        sqlite_path=sqlite_path,
        compressed_path=compressed_path,
        meta_path=meta_path,
        sha256=sha,
        compressed_sha256=compressed_sha,
        stats=stats,
        meta=meta_out,
    )


def _compress_xz(src: str, dst: str) -> None:
    """Compress to .xz (LZMA). Apple's Compression framework / AppleArchive
    decode LZMA natively, so the app needs no third-party codec. LZMA also beats
    zstd on ratio for this text-heavy data, and lzma is Python stdlib (no dep).
    Deterministic: same input bytes -> same .xz bytes (no timestamps/filenames).
    """
    import lzma

    with open(src, "rb") as fin, lzma.open(
        dst, "wb", format=lzma.FORMAT_XZ, preset=9 | lzma.PRESET_EXTREME
    ) as fout:
        for chunk in iter(lambda: fin.read(_CHUNK), b""):
            fout.write(chunk)


_CHUNK = 1 << 20


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()

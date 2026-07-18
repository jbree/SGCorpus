"""Row-level semantic deltas between two packs (plan §7).

A patch is a small SQLite file of ``upsert`` / ``delete`` operations keyed by the
natural key ``(word_folded, pos, etym_index)`` — never surrogate ids or
Wiktextract sense ids, which are not stable across dumps. The app applies a
patch to a copy of the old pack inside one transaction, then verifies the
result's content digest equals the target's. The full pack is always the
fallback, so a broken/absent patch never blocks an update.
"""

from __future__ import annotations

import json
import os
import sqlite3

from . import SCHEMA_VERSION
from .build import compute_content_digest, _compress_xz, _sha256_file
from . import schema

_NATURAL_KEY_SQL = "SELECT id, word_folded, pos, etym_index FROM word"


def _word_records(conn: sqlite3.Connection) -> dict[tuple, dict]:
    """Map natural key -> full word record (word row + all children)."""
    records: dict[tuple, dict] = {}
    for wid, wf, pos, etym in conn.execute(_NATURAL_KEY_SQL):
        key = (wf, pos, etym)
        word = conn.execute(
            "SELECT word, word_folded, word_search, pos, etym_index FROM word WHERE id=?",
            (wid,),
        ).fetchone()
        senses = conn.execute(
            "SELECT ordinal, gloss, example, example_en, tags FROM sense WHERE word_id=? ORDER BY ordinal",
            (wid,),
        ).fetchall()
        prons = conn.execute(
            "SELECT ipa, dialects, rhyme_key FROM pronunciation WHERE word_id=? ORDER BY id",
            (wid,),
        ).fetchall()
        rels = conn.execute(
            "SELECT rel_type, target, target_folded, sense_hint, tags FROM relation WHERE word_id=? ORDER BY id",
            (wid,),
        ).fetchall()
        records[key] = {
            "word": list(word),
            "senses": [list(s) for s in senses],
            "prons": [list(p) for p in prons],
            "rels": [list(r) for r in rels],
        }
    return records


def _content_of(rec: dict) -> str:
    """Stable serialization of a word record for change detection."""
    return json.dumps(
        [rec["word"], rec["senses"], sorted(rec["prons"]), sorted(rec["rels"])],
        ensure_ascii=False,
        sort_keys=True,
    )


def create_delta(
    old_pack: str, new_pack: str, out_dir: str, *, compress: bool = True
) -> dict:
    """Diff two built packs and emit a patch SQLite + ``.delta.json`` sidecar."""
    os.makedirs(out_dir, exist_ok=True)
    old = sqlite3.connect(old_pack)
    new = sqlite3.connect(new_pack)
    try:
        old_meta = schema.read_meta(old)
        new_meta = schema.read_meta(new)
        from_v = old_meta["pack_version"]
        to_v = new_meta["pack_version"]
        lang_code = _lang_code(new_meta, new_pack)

        old_recs = _word_records(old)
        new_recs = _word_records(new)

        upserts = [
            (key, rec)
            for key, rec in new_recs.items()
            if key not in old_recs or _content_of(rec) != _content_of(old_recs[key])
        ]
        deletes = [key for key in old_recs if key not in new_recs]

        base = f"{lang_code}-{from_v}_to_{to_v}-schema{SCHEMA_VERSION}"
        patch_path = os.path.join(out_dir, base + ".patch")
        if os.path.exists(patch_path):
            os.remove(patch_path)
        _write_patch(
            patch_path,
            lang_code=lang_code,
            from_v=from_v,
            to_v=to_v,
            target_digest=new_meta.get("content_digest", ""),
            upserts=upserts,
            deletes=deletes,
        )
    finally:
        old.close()
        new.close()

    artifact = os.path.basename(patch_path)
    compressed_size = 0
    sha = _sha256_file(patch_path)
    if compress:
        cpath = patch_path + ".xz"
        _compress_xz(patch_path, cpath)
        sha = _sha256_file(cpath)
        compressed_size = os.path.getsize(cpath)
        artifact = os.path.basename(cpath)

    sidecar = {
        "lang_code": lang_code,
        "from": from_v,
        "to": to_v,
        "schema_version": SCHEMA_VERSION,
        "upserts": len(upserts),
        "deletes": len(deletes),
        "sha256": sha,
        "compressed_size": compressed_size,
        "artifact": artifact,
    }
    with open(os.path.join(out_dir, base + ".delta.json"), "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2, sort_keys=True)
    return sidecar


def _lang_code(meta: dict, pack_path: str) -> str:
    # lang_code isn't in meta directly; derive from the filename "<lc>-<ver>-...".
    name = os.path.basename(pack_path)
    return name.split("-", 1)[0]


def _write_patch(
    path: str,
    *,
    lang_code: str,
    from_v: str,
    to_v: str,
    target_digest: str,
    upserts: list,
    deletes: list,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE patch_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE upsert_word (
              word_folded TEXT, pos TEXT, etym_index INTEGER,
              payload TEXT,
              PRIMARY KEY (word_folded, pos, etym_index)
            );
            CREATE TABLE delete_word (
              word_folded TEXT, pos TEXT, etym_index INTEGER,
              PRIMARY KEY (word_folded, pos, etym_index)
            );
            """
        )
        conn.executemany(
            "INSERT INTO patch_meta(key,value) VALUES (?,?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("lang_code", lang_code),
                ("from_version", from_v),
                ("to_version", to_v),
                ("target_content_digest", target_digest),
            ],
        )
        conn.executemany(
            "INSERT INTO upsert_word VALUES (?,?,?,?)",
            [(k[0], k[1], k[2], _content_of(rec)) for k, rec in upserts],
        )
        conn.executemany(
            "INSERT INTO delete_word VALUES (?,?,?)",
            [(k[0], k[1], k[2]) for k in deletes],
        )
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()


def apply_delta(base_pack: str, patch_path: str, out_pack: str) -> str:
    """Apply a patch to a copy of ``base_pack``, write ``out_pack``, verify.

    Returns the resulting content digest; raises if it doesn't match the patch's
    ``target_content_digest`` or if ``integrity_check`` fails.
    """
    import shutil

    shutil.copyfile(base_pack, out_pack)
    conn = sqlite3.connect(out_pack)
    patch = sqlite3.connect(patch_path)
    try:
        target_digest = dict(
            patch.execute("SELECT key, value FROM patch_meta")
        ).get("target_content_digest", "")

        conn.execute("BEGIN")
        # Deletes and the "delete" half of upserts: remove word + children by key.
        keys_to_remove = list(
            patch.execute("SELECT word_folded, pos, etym_index FROM delete_word")
        ) + list(
            patch.execute("SELECT word_folded, pos, etym_index FROM upsert_word")
        )
        for wf, pos, etym in keys_to_remove:
            row = conn.execute(
                "SELECT id FROM word WHERE word_folded=? AND pos=? AND etym_index=?",
                (wf, pos, etym),
            ).fetchone()
            if row:
                wid = row[0]
                conn.execute("DELETE FROM sense WHERE word_id=?", (wid,))
                conn.execute("DELETE FROM pronunciation WHERE word_id=?", (wid,))
                conn.execute("DELETE FROM relation WHERE word_id=?", (wid,))
                conn.execute("DELETE FROM word WHERE id=?", (wid,))

        next_id = (conn.execute("SELECT COALESCE(MAX(id),0) FROM word").fetchone()[0])
        for wf, pos, etym, payload in patch.execute(
            "SELECT word_folded, pos, etym_index, payload FROM upsert_word"
        ):
            next_id += 1
            _insert_payload(conn, next_id, json.loads(payload))
        conn.execute("COMMIT")
        conn.execute("VACUUM")
        conn.commit()

        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"post-apply integrity_check failed: {integrity}")
        digest = compute_content_digest(conn)
        # Refresh meta so the applied pack advertises the new version + digest.
        to_v = dict(patch.execute("SELECT key, value FROM patch_meta")).get(
            "to_version", ""
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('pack_version',?)", (to_v,)
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('content_digest',?)",
            (digest,),
        )
        conn.commit()
    finally:
        conn.close()
        patch.close()

    if target_digest and digest != target_digest:
        raise RuntimeError(
            f"delta apply digest mismatch: got {digest[:12]} want {target_digest[:12]}"
        )
    return digest


def _insert_payload(conn: sqlite3.Connection, wid: int, payload: list) -> None:
    word, senses, prons, rels = payload
    conn.execute(
        "INSERT INTO word(id, word, word_folded, word_search, pos, etym_index) "
        "VALUES (?,?,?,?,?,?)",
        (wid, *word),
    )
    for ordv, gloss, ex, exen, tags in senses:
        conn.execute(
            "INSERT INTO sense(word_id, ordinal, gloss, example, example_en, tags) "
            "VALUES (?,?,?,?,?,?)",
            (wid, ordv, gloss, ex, exen, tags),
        )
    for ipa, dia, rk in prons:
        conn.execute(
            "INSERT INTO pronunciation(word_id, ipa, dialects, rhyme_key) VALUES (?,?,?,?)",
            (wid, ipa, dia, rk),
        )
    for rt, target, tf, sh, tags in rels:
        conn.execute(
            "INSERT INTO relation(word_id, rel_type, target, target_folded, sense_hint, tags) "
            "VALUES (?,?,?,?,?,?)",
            (wid, rt, target, tf, sh, tags),
        )

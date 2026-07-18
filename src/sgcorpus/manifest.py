"""Catalog manifest generation (plan §6).

Scans a directory of ``*.meta.json`` sidecars (emitted by :mod:`build`) and any
``*.delta.json`` sidecars (:mod:`delta`) and writes the single ``catalog.json``
the app fetches first. The newest ``pack_version`` per ``lang_code`` becomes
``latest``; deltas ending at that version are listed under ``deltas``.
"""

from __future__ import annotations

import glob
import json
import os

MANIFEST_VERSION = 1


def _pack_entry(meta: dict, base_url: str) -> dict:
    return {
        "pack_version": meta["pack_version"],
        "schema_version": meta["schema_version"],
        "url": _join(base_url, meta["artifact"]),
        "compressed_size": meta.get("compressed_size", 0),
        "uncompressed_size": meta.get("uncompressed_size", 0),
        "sha256": meta.get("compressed_sha256") or meta.get("sha256"),
        "uncompressed_sha256": meta["sha256"],
        "content_digest": meta.get("content_digest"),
        "row_counts": meta.get("row_counts", {}),
        "ipa_coverage": meta.get("ipa_coverage", 0.0),
        "synonym_coverage": meta.get("synonym_coverage", 0.0),
        "license": meta.get("license"),
        "attribution": meta.get("attribution"),
    }


def _join(base_url: str, name: str) -> str:
    if not base_url:
        return name
    return base_url.rstrip("/") + "/" + name


def build_manifest(
    build_dir: str, base_url: str = "", generated_at: str = ""
) -> dict:
    packs_by_lang: dict[str, list[dict]] = {}
    for path in sorted(glob.glob(os.path.join(build_dir, "*.meta.json"))):
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        packs_by_lang.setdefault(meta["lang_code"], []).append(meta)

    deltas_by_lang: dict[str, list[dict]] = {}
    for path in sorted(glob.glob(os.path.join(build_dir, "*.delta.json"))):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        deltas_by_lang.setdefault(d["lang_code"], []).append(d)

    packs = []
    for lang_code in sorted(packs_by_lang):
        metas = sorted(packs_by_lang[lang_code], key=lambda m: m["pack_version"])
        latest = metas[-1]
        deltas = [
            {
                "from": d["from"],
                "to": d["to"],
                "url": _join(base_url, d["artifact"]),
                "compressed_size": d.get("compressed_size", 0),
                "sha256": d.get("sha256"),
            }
            for d in deltas_by_lang.get(lang_code, [])
            if d["to"] == latest["pack_version"]
        ]
        packs.append(
            {
                "lang_code": lang_code,
                "word_lang": latest["word_lang"],
                "definition_lang": latest["definition_lang"],
                "source_edition": latest["source_edition"],
                "latest": _pack_entry(latest, base_url),
                "deltas": deltas,
            }
        )

    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": generated_at,
        "packs": packs,
    }


def write_manifest(
    build_dir: str, out_path: str, base_url: str = "", generated_at: str = ""
) -> dict:
    manifest = build_manifest(build_dir, base_url, generated_at)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, sort_keys=True)
    return manifest

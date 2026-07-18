"""Command-line entry point.

    sgcorpus acquire   --edition enwiktionary --lang en
    sgcorpus build     --edition enwiktionary --lang en
    sgcorpus manifest  --base-url https://cdn.example/packs
    sgcorpus delta     --old <a.sqlite> --new <b.sqlite>
    sgcorpus apply-delta --base <a.sqlite> --patch <p.patch> --out <b.sqlite>
    sgcorpus verify    <pack.sqlite>

``build`` acquires the source first unless ``--input`` points at a local dump.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import SCHEMA_VERSION, __version__
from .config import ALL_RELATION_TYPES, BuildConfig, resolve_source


def _add_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--edition", default="enwiktionary", help="Wiktionary edition (default: enwiktionary)")
    p.add_argument("--lang", required=True, help="lang_code to build, e.g. en")
    p.add_argument("--lang-name", help="human language name (for the enwiktionary slice URL)")
    p.add_argument("--url", help="explicit source JSONL URL (overrides edition/lang mapping)")


def cmd_acquire(args) -> int:
    from .acquire import download

    src = resolve_source(args.edition, args.lang, url=args.url, lang_name=args.lang_name)
    dest = args.output or os.path.join(args.download_dir, f"{args.edition}-{args.lang}.jsonl")
    print(f"Source: {src.url}")
    prov = download(src.url, dest, resume=not args.no_resume, offline=args.offline)
    print(f"Downloaded {prov.bytes:,} B -> {dest}")
    print(f"  dump_date={prov.dump_date}  sha256={prov.sha256[:16]}…")
    return 0


def _build_config(args) -> BuildConfig:
    return BuildConfig(
        keep_examples=not args.no_examples,
        keep_etymology=args.keep_etymology,
        drop_form_of=not args.keep_form_of,
        gloss_hierarchy=args.gloss_hierarchy,
        relation_types=tuple(args.relations),
        max_relations_per_type=args.max_relations,
    )


def cmd_build(args) -> int:
    from .acquire import download
    from .build import build_pack

    src = resolve_source(args.edition, args.lang, url=args.url, lang_name=args.lang_name)
    cfg = _build_config(args)

    if args.input:
        input_path = args.input
        dump_date = args.dump_date or "unknown"
    else:
        input_path = os.path.join(args.download_dir, f"{args.edition}-{args.lang}.jsonl")
        cached = os.path.exists(input_path + ".done")
        print(f"Source: {src.url}" + ("  [cached — no network]" if cached else ""))
        prov = download(src.url, input_path, resume=not args.no_resume, offline=args.offline)
        dump_date = args.dump_date or prov.dump_date

    print(f"Building {src.lang_code} pack from {input_path} (dump {dump_date})…")
    result = build_pack(
        input_path,
        args.build_dir,
        src,
        cfg=cfg,
        dump_date=dump_date,
        wiktextract_version=args.wiktextract_version,
        compress=not args.no_compress,
    )
    s = result.stats
    print(f"  words={s.words:,}  senses={s.senses:,}  pron={s.pronunciations:,}  rel={s.relations:,}")
    print(f"  ipa_coverage={s.ipa_coverage:.1%}  synonym_coverage={s.synonym_coverage:.1%}  avg_senses={s.avg_senses}")
    print(f"  malformed_lines={s.lines_malformed}  skipped_lang={s.entries_skipped_lang}  "
          f"skipped_nolang={s.entries_skipped_nolang}  skipped_empty={s.entries_skipped_empty}")
    print(f"  -> {result.sqlite_path}")
    if result.compressed_path:
        ratio = result.meta["compressed_size"] / max(result.meta["uncompressed_size"], 1)
        print(f"  -> {result.compressed_path} ({result.meta['compressed_size']:,} B, {ratio:.1%} of raw)")
    print(f"  -> {result.meta_path}")
    return 0


def cmd_manifest(args) -> int:
    from .manifest import write_manifest

    out = args.output or os.path.join(args.build_dir, "catalog.json")
    m = write_manifest(args.build_dir, out, base_url=args.base_url, generated_at=args.generated_at)
    print(f"Wrote {out} — {len(m['packs'])} pack(s)")
    for p in m["packs"]:
        latest = p["latest"]
        print(f"  {p['lang_code']}: v{latest['pack_version']} "
              f"({latest['row_counts'].get('word', 0):,} words, {len(p['deltas'])} delta(s))")
    return 0


def cmd_delta(args) -> int:
    from .delta import create_delta

    sidecar = create_delta(args.old, args.new, args.build_dir, compress=not args.no_compress)
    print(f"Delta {sidecar['from']} -> {sidecar['to']}: "
          f"{sidecar['upserts']} upserts, {sidecar['deletes']} deletes")
    print(f"  -> {os.path.join(args.build_dir, sidecar['artifact'])}")
    return 0


def cmd_apply_delta(args) -> int:
    from .delta import apply_delta

    digest = apply_delta(args.base, args.patch, args.out)
    print(f"Applied patch -> {args.out}")
    print(f"  content_digest={digest[:16]}…  (verified against target)")
    return 0


def cmd_verify(args) -> int:
    """Lightweight QA against a built pack (plan §9)."""
    from .build import compute_content_digest
    from .schema import read_meta

    conn = sqlite3.connect(args.pack)
    try:
        meta = read_meta(conn)
        problems = []
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            problems.append(f"schema_version={meta.get('schema_version')} != {SCHEMA_VERSION}")
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if integrity != "ok":
            problems.append(f"integrity_check={integrity}")
        digest = compute_content_digest(conn)
        if meta.get("content_digest") and digest != meta["content_digest"]:
            problems.append("content_digest mismatch vs meta")

        # Residual-markup scan across shipped text (zero tolerance, plan §9).
        from .sanitize import has_residual_markup
        dirty = 0
        for (g,) in conn.execute("SELECT gloss FROM sense LIMIT ?", (args.sample,)):
            if has_residual_markup(g):
                dirty += 1
        if dirty:
            problems.append(f"{dirty} sampled glosses carry residual markup")

        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("word", "sense", "pronunciation", "relation")
        }
        print(f"Pack: {args.pack}")
        print(f"  meta: {json.dumps({k: meta.get(k) for k in ('word_lang','definition_lang','source_edition','pack_version')})}")
        print(f"  counts: {counts}")
        print(f"  content_digest={digest[:16]}…")

        for spot in args.spot_check or []:
            rows = conn.execute(
                "SELECT w.pos, s.gloss FROM word w JOIN sense s ON s.word_id=w.id "
                "WHERE w.word_folded=? ORDER BY w.pos, s.ordinal LIMIT 3",
                (spot.casefold(),),
            ).fetchall()
            status = "OK" if rows else "MISSING"
            print(f"  spot-check {spot!r}: {status}" + (f" — {rows[0][0]}: {rows[0][1][:60]}…" if rows else ""))
            if not rows:
                problems.append(f"spot-check word not found: {spot}")

        if problems:
            print("FAIL:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("PASS")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sgcorpus", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"sgcorpus {__version__} (schema {SCHEMA_VERSION})")
    p.add_argument("--download-dir", default="downloads", help="where source dumps are cached")
    p.add_argument("--build-dir", default="build", help="where packs/manifests are written")
    sub = p.add_subparsers(dest="command", required=True)

    ap = sub.add_parser("acquire", help="download a source extract")
    _add_source_args(ap)
    ap.add_argument("--output", help="destination path (default: <download-dir>/<edition>-<lang>.jsonl)")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--offline", action="store_true", help="use the cached download only; never touch the network")
    ap.set_defaults(func=cmd_acquire)

    bp = sub.add_parser("build", help="build a SQLite pack")
    _add_source_args(bp)
    bp.add_argument("--input", help="local JSONL(.gz) instead of downloading")
    bp.add_argument("--dump-date", help="override source dump date (YYYY-MM-DD)")
    bp.add_argument("--wiktextract-version", default="unknown")
    bp.add_argument("--no-resume", action="store_true")
    bp.add_argument("--offline", action="store_true", help="rebuild from the cached download only; never touch the network")
    bp.add_argument("--no-compress", action="store_true", help="skip .xz emission")
    bp.add_argument("--no-examples", action="store_true", help="drop examples (size lever)")
    bp.add_argument("--keep-etymology", action="store_true")
    bp.add_argument("--keep-form-of", action="store_true", help="retain inflection senses")
    bp.add_argument("--gloss-hierarchy", choices=["join", "last"], default="join")
    bp.add_argument("--relations", nargs="*", default=list(ALL_RELATION_TYPES),
                    choices=list(ALL_RELATION_TYPES))
    bp.add_argument("--max-relations", type=int, default=50)
    bp.set_defaults(func=cmd_build)

    mp = sub.add_parser("manifest", help="write catalog.json from built packs")
    mp.add_argument("--base-url", default="", help="CDN prefix for pack URLs")
    mp.add_argument("--generated-at", default="", help="ISO date stamp for the manifest")
    mp.add_argument("--output", help="manifest path (default: <build-dir>/catalog.json)")
    mp.set_defaults(func=cmd_manifest)

    dp = sub.add_parser("delta", help="diff two packs into a patch")
    dp.add_argument("--old", required=True)
    dp.add_argument("--new", required=True)
    dp.add_argument("--no-compress", action="store_true")
    dp.set_defaults(func=cmd_delta)

    apd = sub.add_parser("apply-delta", help="apply a patch to a base pack")
    apd.add_argument("--base", required=True)
    apd.add_argument("--patch", required=True)
    apd.add_argument("--out", required=True)
    apd.set_defaults(func=cmd_apply_delta)

    vp = sub.add_parser("verify", help="QA a built pack")
    vp.add_argument("pack")
    vp.add_argument("--spot-check", nargs="*", help="headwords expected to exist")
    vp.add_argument("--sample", type=int, default=5000, help="rows to markup-scan")
    vp.set_defaults(func=cmd_verify)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

# SGCorpus

Wiktextract → SQLite extraction pipeline. Produces compact, read-only,
per-language dictionary + thesaurus packs derived from Wiktionary (via
[Wiktextract](https://github.com/tatuylonen/wiktextract) / kaikki.org) that
power the reference inspector in the [Song Garden](../SongGarden) app.

The full specification lives in [`docs/PLAN.md`](docs/PLAN.md); this README is
the operational quick-start. To wire a built pack into the Song Garden app, see
[`docs/INTEGRATION.md`](docs/INTEGRATION.md) (written to be read by an LLM
working in the app repo). The output SQLite schema in
[`src/sgcorpus/schema.py`](src/sgcorpus/schema.py) **is** the contract the app
depends on — keep it stable and bump `SCHEMA_VERSION` on any breaking change.

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) for dependency management

```bash
uv sync --extra dev      # create .venv and install
```

## Usage

Everything runs through the `sgcorpus` CLI (`uv run sgcorpus …`):

```bash
# Download + build the English pack in one step
uv run sgcorpus build --edition enwiktionary --lang en

# Just download the source extract (resumable)
uv run sgcorpus acquire --edition enwiktionary --lang en

# Build from an already-downloaded dump
uv run sgcorpus build --lang en --input downloads/enwiktionary-en.jsonl --dump-date 2026-07-16

# QA a built pack (plan §9)
uv run sgcorpus verify build/en-2026-07-16-schema1.sqlite --spot-check ocean run love

# Generate the catalog manifest the app fetches first
uv run sgcorpus manifest --base-url https://cdn.example/packs --generated-at 2026-07-16

# Row-level delta between two dumps + verify it round-trips
uv run sgcorpus delta --old build/en-2026-05-01-schema1.sqlite --new build/en-2026-07-16-schema1.sqlite
uv run sgcorpus apply-delta --base build/en-2026-05-01-schema1.sqlite \
    --patch build/en-2026-05-01_to_2026-07-16-schema1.patch --out /tmp/applied.sqlite
```

Source files are cached under `downloads/`, artifacts written to `build/`
(both git-ignored — the multi-GB inputs are never committed).

### Local workflow & offline rebuilds

The first `build` (or `acquire`) downloads the source dump to
`downloads/<edition>-<lang>.jsonl` and writes a `.done` provenance sidecar next
to it. **Every subsequent `build` reuses that cache and makes no network call at
all** — so you only wait on the ~3 GB download once, then iterate on build flags
freely:

```bash
uv run sgcorpus build --lang en                 # first run: downloads, then builds
uv run sgcorpus build --lang en --no-examples   # instant re-download skip; rebuilds from cache
uv run sgcorpus build --lang en --offline       # hard-guarantee no network (errors if uncached)
```

To force a fresh download, delete the `.done` sidecar (or the `.jsonl`) in
`downloads/`. `--offline` never touches the network: it builds from the cache or
fails with a clear message.

### Build size levers (plan §8.4)

| Flag | Effect |
|---|---|
| `--no-examples` | drop usage examples (large size lever) |
| `--relations synonym antonym` | ship only these relation types |
| `--max-relations N` | cap each relation list (default 50) |
| `--gloss-hierarchy last` | keep only the most-specific gloss (default `join`) |
| `--keep-etymology` | retain a trimmed etymology snippet (default: drop) |
| `--keep-form-of` | retain inflection senses (default: drop) |

## Output

Per language: `<lang>-<dumpdate>-schema<N>.sqlite(.zst)` plus a `.meta.json`
sidecar with row counts, coverage metrics, and sha256s. `sgcorpus manifest`
aggregates the sidecars into `catalog.json`.

## Determinism

Builds are byte-deterministic (fixed `page_size`, stable insert order,
`built_at` derived from the dump date rather than wall-clock, `VACUUM`). Two
builds of the same source dump produce an identical file — this is what makes
row-level delta updates possible. See `docs/PLAN.md` §7.

## Licensing

Pipeline code: MIT (`LICENSE`). The **lexical data** in every pack is
**CC BY-SA 4.0**, inherited from Wiktionary; attribution and license strings
ship inside each pack's `meta` table and are surfaced in the manifest. The
consuming app must show an acknowledgements screen (Wiktionary contributors;
extracted via Wiktextract).

## Tests

```bash
uv run pytest
```

Tests run entirely against the small fixture in `tests/fixtures/` — no network,
no large downloads.

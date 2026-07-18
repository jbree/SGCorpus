"""SGCorpus — Wiktextract → SQLite extraction pipeline.

Produces compact, read-only, per-language SQLite dictionary/thesaurus packs
derived from Wiktionary (via Wiktextract / kaikki.org) for the Song Garden
reference inspector. See README.md and docs/PLAN.md for the full contract.
"""

__version__ = "0.1.0"

# Bump on any breaking change to the output SQLite schema (schema.py).
SCHEMA_VERSION = 1

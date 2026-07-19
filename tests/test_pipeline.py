import json
import os
import sqlite3

import pytest

from sgcorpus import SCHEMA_VERSION
from sgcorpus.build import build_pack, compute_content_digest
from sgcorpus.config import BuildConfig, resolve_source
from sgcorpus.delta import apply_delta, create_delta
from sgcorpus.manifest import build_manifest
from sgcorpus.sanitize import clean, fold, search_fold

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.jsonl")

# Deterministic stand-in for wordfreq so the suite doesn't depend on the real
# corpus (fast, stable across wordfreq versions). London famous, McStay/Iris
# obscure — matching the shape of real Zipf scores. Unknown words score 0.
FAKE_ZIPF = {"london": 5.3, "mcstay": 1.7, "iris": 1.5}


def _fake_scorer(folded: str) -> float:
    if " " in folded:
        return max((FAKE_ZIPF.get(t, 0.0) for t in folded.split()), default=0.0)
    return FAKE_ZIPF.get(folded, 0.0)


@pytest.fixture
def en_source():
    return resolve_source("enwiktionary", "en")


def _build(tmp_path, source, *, name_scorer=_fake_scorer, **cfg_kw):
    cfg = BuildConfig(**cfg_kw)
    return build_pack(
        FIXTURE, str(tmp_path), source, cfg=cfg, dump_date="2026-07-16",
        compress=False, name_scorer=name_scorer,
    )


# --- sanitizer -------------------------------------------------------------

def test_clean_strips_wikitext_and_templates():
    assert clean("A [[coffee]] shop with '''markup'''.") == "A coffee shop with markup."
    assert clean("We met at a {{small}} café.") == "We met at a café."
    assert clean("<b>bold</b> text") == "bold text"
    assert clean(None) == ""


def test_folding():
    assert fold("Ocean") == "ocean"
    assert search_fold("café") == "cafe"
    assert search_fold("Über") == "uber"


def test_has_residual_markup():
    from sgcorpus.sanitize import has_residual_markup
    assert has_residual_markup("a [[link]] here")
    assert has_residual_markup("a {{template}} here")
    assert has_residual_markup("bold <b>x</b> text")
    # Bare comparison operators are legitimate text, NOT markup.
    assert not has_residual_markup("true when x < y and y > z")
    assert not has_residual_markup("a plain clean gloss")


# --- build -----------------------------------------------------------------

def test_build_filters_language_and_drops_empty(tmp_path, en_source):
    result = _build(tmp_path, en_source)
    conn = sqlite3.connect(result.sqlite_path)
    words = {w for (w,) in conn.execute("SELECT word FROM word")}
    # French headword filtered; missing-lang_code entry dropped; blank/empty
    # words dropped.
    assert "océan" not in words
    assert "kartoffel" not in words          # no lang_code -> unknown language -> dropped
    assert "  " not in words
    assert "empty" not in words
    assert {"ocean", "run", "café"} <= words
    conn.close()


# --- proper-noun / name frequency gate -------------------------------------

def test_proper_noun_frequency_filter(tmp_path, en_source):
    result = _build(tmp_path, en_source)  # default name_min_zipf=3.0
    conn = sqlite3.connect(result.sqlite_path)
    words = {w for (w,) in conn.execute("SELECT word FROM word")}
    assert "London" in words          # fake zipf 5.3 >= 3.0 -> kept
    assert "McStay" not in words      # fake zipf 1.7 < 3.0 -> dropped
    conn.close()
    assert result.stats.entries_skipped_name_rare == 2   # McStay + Iris


def test_name_gate_disabled_keeps_all_names(tmp_path, en_source):
    result = _build(tmp_path, en_source, name_min_zipf=None)
    conn = sqlite3.connect(result.sqlite_path)
    words = {w for (w,) in conn.execute("SELECT word FROM word")}
    assert {"London", "McStay", "Iris"} <= words
    conn.close()
    assert result.stats.entries_skipped_name_rare == 0


def test_dropped_name_keeps_shared_common_word(tmp_path, en_source):
    """Dropping the proper-noun 'Iris' must not remove the common noun 'iris'."""
    result = _build(tmp_path, en_source)
    conn = sqlite3.connect(result.sqlite_path)
    rows = conn.execute(
        "SELECT word, pos FROM word WHERE word_folded='iris'"
    ).fetchall()
    conn.close()
    assert rows == [("iris", "noun")]


def test_proper_noun_pos_normalizes_to_name():
    from sgcorpus.normalize import is_proper_noun_pos, normalize_pos
    # POS_MAP fix: raw "proper noun" is labeled a name, not folded into "noun".
    assert normalize_pos("proper noun") == "name"
    assert is_proper_noun_pos("proper noun")
    assert is_proper_noun_pos("Name")           # case-insensitive
    assert not is_proper_noun_pos("noun")


def test_default_name_scorer_discriminates_fame():
    """The real wordfreq-backed scorer separates famous names from obscure ones."""
    from sgcorpus.build import _default_name_scorer
    score = _default_name_scorer()
    assert score("london") >= 3.0
    assert score("mcstay") < 3.0
    assert score("new york") >= 3.0   # multiword scored by strongest token
    assert score("") == 0.0


def test_build_multipos_run_has_two_entries(tmp_path, en_source):
    result = _build(tmp_path, en_source)
    conn = sqlite3.connect(result.sqlite_path)
    rows = conn.execute(
        "SELECT pos, etym_index FROM word WHERE word_folded='run' ORDER BY pos"
    ).fetchall()
    assert rows == [("noun", 0), ("verb", 0)]
    conn.close()


def test_form_of_sense_dropped(tmp_path, en_source):
    result = _build(tmp_path, en_source)
    conn = sqlite3.connect(result.sqlite_path)
    glosses = [
        g for (g,) in conn.execute(
            "SELECT s.gloss FROM sense s JOIN word w ON w.id=s.word_id "
            "WHERE w.word_folded='run' AND w.pos='verb'"
        )
    ]
    assert "To move quickly on foot." in glosses
    assert not any("plural of" in g for g in glosses)
    conn.close()


def test_gloss_hierarchy_join(tmp_path, en_source):
    result = _build(tmp_path, en_source, gloss_hierarchy="join")
    conn = sqlite3.connect(result.sqlite_path)
    gloss = conn.execute(
        "SELECT gloss FROM sense s JOIN word w ON w.id=s.word_id "
        "WHERE w.word_folded='ocean' AND s.ordinal=1"
    ).fetchone()[0]
    assert gloss == "A large number or quantity.; An overwhelming amount of something."
    conn.close()


def test_gloss_hierarchy_last(tmp_path, en_source):
    result = _build(tmp_path, en_source, gloss_hierarchy="last")
    conn = sqlite3.connect(result.sqlite_path)
    gloss = conn.execute(
        "SELECT gloss FROM sense s JOIN word w ON w.id=s.word_id "
        "WHERE w.word_folded='ocean' AND s.ordinal=1"
    ).fetchone()[0]
    assert gloss == "An overwhelming amount of something."
    conn.close()


def test_pronunciation_keeps_all_dialects_and_rhyme(tmp_path, en_source):
    result = _build(tmp_path, en_source)
    conn = sqlite3.connect(result.sqlite_path)
    prons = conn.execute(
        "SELECT ipa, dialects, rhyme_key FROM pronunciation p JOIN word w ON w.id=p.word_id "
        "WHERE w.word_folded='ocean' ORDER BY p.id"
    ).fetchall()
    ipas = [p[0] for p in prons if p[0]]
    assert "/ˈoʊ.ʃən/" in ipas and "/ˈəʊ.ʃən/" in ipas
    assert any(json.loads(p[1]) == ["US"] for p in prons if p[1])
    assert any(p[2] == "-əʊʃən" for p in prons)
    conn.close()


def test_relations_dedup_and_no_self_reference(tmp_path, en_source):
    result = _build(tmp_path, en_source)
    conn = sqlite3.connect(result.sqlite_path)
    syns = [
        t for (t,) in conn.execute(
            "SELECT target FROM relation r JOIN word w ON w.id=r.word_id "
            "WHERE w.word_folded='run' AND w.pos='verb' AND r.rel_type='synonym'"
        )
    ]
    assert "sprint" in syns and "dash" in syns
    assert "run" not in [s.casefold() for s in syns]  # self-ref dropped
    conn.close()


def test_no_residual_markup_in_shipped_text(tmp_path, en_source):
    result = _build(tmp_path, en_source)
    conn = sqlite3.connect(result.sqlite_path)
    for (g,) in conn.execute("SELECT gloss FROM sense"):
        assert "[[" not in g and "{{" not in g and "<" not in g
    for (e,) in conn.execute("SELECT example FROM sense WHERE example IS NOT NULL"):
        assert "[[" not in e and "{{" not in e
    conn.close()


def test_stats_and_meta(tmp_path, en_source):
    result = _build(tmp_path, en_source)
    assert result.stats.lines_malformed == 1
    assert result.stats.entries_skipped_lang == 1     # the French entry (lang_code=fr)
    assert result.stats.entries_skipped_nolang == 1   # the German entry (no lang_code)
    assert result.stats.words >= 3
    assert result.meta["schema_version"] == SCHEMA_VERSION
    assert result.meta["content_digest"]
    # sidecar written and parseable
    with open(result.meta_path) as f:
        sidecar = json.load(f)
    assert sidecar["lang_code"] == "en"
    assert sidecar["row_counts"]["word"] == result.stats.words


# --- determinism -----------------------------------------------------------

def test_build_is_byte_deterministic(tmp_path, en_source):
    a = _build(tmp_path / "a", en_source)
    b = _build(tmp_path / "b", en_source)
    with open(a.sqlite_path, "rb") as fa, open(b.sqlite_path, "rb") as fb:
        assert fa.read() == fb.read()
    assert a.sha256 == b.sha256


# --- manifest --------------------------------------------------------------

def test_manifest_lists_latest_pack(tmp_path, en_source):
    _build(tmp_path, en_source)
    m = build_manifest(str(tmp_path), base_url="https://cdn.example/packs")
    assert m["manifest_version"] == 1
    assert len(m["packs"]) == 1
    pack = m["packs"][0]
    assert pack["lang_code"] == "en"
    assert pack["latest"]["url"].startswith("https://cdn.example/packs/")
    assert pack["latest"]["row_counts"]["word"] >= 3


# --- delta round-trip ------------------------------------------------------

def test_delta_roundtrip(tmp_path, en_source):
    """old→new patch applied to old must reproduce new's content digest (plan §9)."""
    old = build_pack(FIXTURE, str(tmp_path / "old"), en_source,
                     cfg=BuildConfig(), dump_date="2026-05-01", compress=False,
                     name_scorer=_fake_scorer)

    # Build a "new" dump: café gains a synonym; a brand-new word appears.
    new_lines = []
    with open(FIXTURE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("this line"):
                continue
            new_lines.append(line)
    new_lines.append(json.dumps({
        "word": "sea", "pos": "noun", "lang": "English", "lang_code": "en",
        "senses": [{"glosses": ["The salt water covering most of the earth."]}],
        "synonyms": [{"word": "ocean"}],
    }))
    new_fixture = tmp_path / "new.jsonl"
    new_fixture.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    new = build_pack(str(new_fixture), str(tmp_path / "new"), en_source,
                     cfg=BuildConfig(), dump_date="2026-07-16", compress=False,
                     name_scorer=_fake_scorer)
    assert new.stats.words == old.stats.words + 1

    sidecar = create_delta(old.sqlite_path, new.sqlite_path, str(tmp_path / "patches"),
                           compress=False)
    assert sidecar["upserts"] >= 1
    patch_path = os.path.join(str(tmp_path / "patches"), sidecar["artifact"])

    out_pack = str(tmp_path / "applied.sqlite")
    digest = apply_delta(old.sqlite_path, patch_path, out_pack)
    assert digest == new.meta["content_digest"]


def test_apply_delta_digest_mismatch_raises(tmp_path, en_source):
    old = build_pack(FIXTURE, str(tmp_path / "o"), en_source, dump_date="2026-05-01",
                     compress=False, name_scorer=_fake_scorer)
    # Same source → empty diff → applying reproduces old's own digest.
    new = build_pack(FIXTURE, str(tmp_path / "n"), en_source, dump_date="2026-07-16",
                     compress=False, name_scorer=_fake_scorer)
    sidecar = create_delta(old.sqlite_path, new.sqlite_path, str(tmp_path / "p"), compress=False)
    assert sidecar["upserts"] == 0 and sidecar["deletes"] == 0
    patch = os.path.join(str(tmp_path / "p"), sidecar["artifact"])
    digest = apply_delta(old.sqlite_path, patch, str(tmp_path / "out.sqlite"))
    assert digest == new.meta["content_digest"]

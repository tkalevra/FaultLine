"""THE SENSE LAYER (migration 323) + the recall-side disambiguation (§2 + §8.1) — pins.

THE SPEC (DEV/research/LANGUAGE-STANDARDS-SPEC.md, ratified): §2 builds the
OntoLex-inspired relational subset — the `senses` table (one row per (lemma,
sense_class), NOT a triple store), the `verbnet_classes` citable inventory, the
SENSE-SCOPED binding shape (sense_id, verbnet_class_id), and the lexical-entry FK
wiring (rel_types.sense_id — the WGM rel_type rows ARE the lexical entries). §8.1
ships the recall half WITH it: the frame-resolution step reading the sense, the UD
anchor, and the query context — the storage half and the recall half are two halves
of one fix. §4 is the recall projection: token-minimal, the dropped-field list is the
guard — the standardized layers (UD bindings, VerbNet class ids, FrameNet frame
names, OntoLex sense refs, TIMEX3 anchors) are STORAGE/INTERCHANGE fields that never
ride the recall response.

THE LITMUS (the planted decoy reproduction, BUG-RECALL-15): the seat with the family
entity rows + the decoy document ("language family" / "family history" collocations)
must serve the ENTITY rows on "tell me about my family?" (the decoy ABSENT) and the
DECOY content on "what do I know about language families?" (the taxonomy sense
grounds correctly) — BOTH directions.

The WGM gate is never bypassed: nothing in the runtime lane writes to the senses
table — growth rides the WGM like the rel_types (source='wgm' when that lane lands).

House style: monkeypatched seams, no DB for the unit arms (the resolver's ONE table
read is faked at the psycopg2 boundary); the DB arms skip without a live DSN (same
contract as test_bug_recall15_topic_scope.py).
"""
from __future__ import annotations

import os
import pathlib
import re
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.mcp import server as S  # noqa: E402

_REPO = pathlib.Path(__file__).resolve().parent.parent
_MIGRATION = _REPO / "migrations" / "323_sense_layer_seed.sql"
_TEMPLATE = _REPO / "src" / "provisioning" / "templates" / "user_schema.sql"

_SEED_SENSE_ROWS = [
    # (sense_key, lemma, pos, sense_class, discriminators, default_sense) — the
    # migration's seed set, the columns the resolver reads.
    ("family#kinship", "family", "NOUN", "kinship", [], True),
    ("family#taxonomy", "family", "NOUN", "taxonomy", ["language", "linguistic"], False),
    ("family#medical_history", "family", "NOUN", "medical_history", ["history"], False),
    ("use#instrumentality", "use", "VERB", "instrumentality", [], True),
    ("use#consumption", "use", "VERB", "consumption", [], False),
    ("use#utility", "use", "NOUN", "utility", [], False),
]


# ---------------------------------------------------------------------------
# A. the migration/template SHAPE (static parity pins, no DB)
# ---------------------------------------------------------------------------
def test_migration_file_exists_and_is_additive_idempotent():
    body = _MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.senses" in body
    assert "CREATE TABLE IF NOT EXISTS public.verbnet_classes" in body
    assert "CREATE TABLE IF NOT EXISTS public.sense_verbnet_bindings" in body
    # the fan-out: the pg_namespace probe pattern (308/309/313/314/317) over every
    # schema holding rel_types — the FK carrier
    assert "c.relname = 'rel_types'" in body
    assert body.count("ADD COLUMN IF NOT EXISTS sense_id BIGINT") == 1
    # the §2 shape: the binding primary key IS (sense_id, verbnet_class_id)
    assert "PRIMARY KEY (sense_id, verbnet_class_id)" in body
    # idempotent seeds
    assert "ON CONFLICT (sense_key) DO NOTHING" in body
    assert "ON CONFLICT (class_id) DO NOTHING" in body
    # the guarded constraint add (the 313 pattern)
    assert "WHEN duplicate_object THEN NULL" in body


def test_migration_check_domains_present():
    body = _MIGRATION.read_text(encoding="utf-8")
    for constraint in ("senses_pos_domain", "senses_sense_class_domain",
                       "senses_source_domain", "verbnet_classes_source_domain",
                       "sense_verbnet_bindings_source_domain"):
        assert f"CONSTRAINT {constraint} CHECK" in body
    # the sense_class domain is the closed seed set + 'other' (the WGM-grown escape)
    assert "'kinship', 'taxonomy', 'medical_history', 'instrumentality'" in body


def test_template_parity_sense_layer_tables_precede_rel_types():
    body = _TEMPLATE.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS senses (" in body
    assert "CREATE TABLE IF NOT EXISTS verbnet_classes (" in body
    assert "CREATE TABLE IF NOT EXISTS sense_verbnet_bindings (" in body
    assert "CREATE TABLE IF NOT EXISTS rel_types (" in body
    # the template MUST precede rel_types (the inline FK on sense_id resolves)
    assert body.index("CREATE TABLE IF NOT EXISTS senses (")
    assert (body.index("CREATE TABLE IF NOT EXISTS senses (")
            < body.index("CREATE TABLE IF NOT EXISTS rel_types ("))
    # column-set parity: every column the migration's fan seeds exists in the template
    for col in ("sense_key", "lemma", "pos", "sense_class", "definition",
                "standard_ref", "discriminators", "default_sense", "source"):
        assert re.search(rf"^{col}\b", body, re.M) or f"\n    {col} " in body


def test_rel_types_sense_id_appended_last_ordinal_alignment():
    """The bootstrap `INSERT INTO rel_types SELECT * FROM public.rel_types` contract:
    sense_id must be the LAST column of the template's rel_types (matching the
    migration's ALTER ordinal)."""
    body = _TEMPLATE.read_text(encoding="utf-8")
    start = body.index("CREATE TABLE IF NOT EXISTS rel_types (")
    # the block terminator is ");" at COLUMN 0 — a bare substring search would hit
    # the prose inside the column comments ("...the default);")
    end = start + re.search(r"^\);", body[start:], re.M).start()
    create_block = body[start:end]
    # the COLUMN list ends at the first CONSTRAINT line (the CHECK lists continue on
    # bare lines after it — they are not columns)
    col_lines = []
    for ln in create_block.splitlines():
        s = ln.strip()
        if s.startswith("CONSTRAINT"):
            break
        if (s and not s.startswith("--") and not s.startswith("CREATE")):
            col_lines.append(s)
    cols = [cl.split()[0] for cl in col_lines]
    assert cols[-1] == "sense_id", f"sense_id must be appended LAST, got {cols[-3:]}"
    assert "sense_id BIGINT REFERENCES senses(id) ON DELETE SET NULL" in create_block
    # the migration adds the same column (IF NOT EXISTS) + the same-named FK
    mig = _MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS sense_id BIGINT" in mig
    assert "rel_types_sense_id_fk" in mig
    assert "ON DELETE SET NULL" in mig


def test_seed_set_is_the_filing_examples():
    """The seed set is the example/verification set from the filings — NOT a full
    inventory: the family kinship/taxonomy/medical-history senses (BUG-RECALL-15) and
    the use instrumentality/consumption/utility senses (the VerbNet scoping)."""
    body = _MIGRATION.read_text(encoding="utf-8")
    for key in ("family#kinship", "family#taxonomy", "family#medical_history",
                "use#instrumentality", "use#consumption", "use#utility"):
        assert f"'{key}'" in body
    # the ONE sense-scoped binding: use#instrumentality → use-105 — the §2 shape made
    # concrete ("which class which sense selects"); the same lemma's consumption sense
    # binds NOTHING.
    assert "'use-105'" in body
    assert "s.sense_key = 'use#instrumentality'" in body
    # the verified VerbNet inventory: 14 core classes, member lists verbatim from the
    # v3.2 online index (verbs.colorado.edu/verb-index, fetched + verified 2026-11-13)
    for class_id in ("own-100", "give-13.1", "want-32.1", "admire-31.2", "say-37.7",
                     "tell-37.2", "put-9.1", "send-11.1", "exist-47.1", "hold-15.1",
                     "keep-15.2", "meet-36.3", "consume-66", "use-105"):
        assert f"('{class_id}'" in body
    # the landscape's "use-42" is NOT a real VerbNet class id — the citable one is
    # use-105; guard against the unverified id ever seeding.
    assert "use-42" not in body


def test_wgm_gate_never_bypassed_no_runtime_writes():
    """The senses table grows through the WGM like the rel_types — the recall lane is
    read-only: SELECT-only SQL in the runtime module, and the migration's seeds carry
    source='seed' (growth rows are the WGM lane's, source='wgm', future work)."""
    server_src = (_REPO / "src" / "mcp" / "server.py").read_text(encoding="utf-8")
    assert "FROM senses" in server_src
    for write_kw in ("INSERT INTO senses", "UPDATE senses", "DELETE FROM senses"):
        assert write_kw not in server_src
    mig = _MIGRATION.read_text(encoding="utf-8")
    assert "WHERE source = 'seed'" in mig


# ---------------------------------------------------------------------------
# B. the resolver harness — the ONE table read faked at the psycopg2 boundary
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, sense_rows):
        self._sense_rows = sense_rows
        self._executed = []

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        self._last = (sql, params)

    def fetchone(self):
        sql, _ = self._last
        if "nspname" in sql:
            return ("faultline_test_schema",)
        return None

    def fetchall(self):
        sql, params = self._last
        if "FROM senses" in sql:
            lemmas = set(params[0])
            return [r for r in self._sense_rows if r[1] in lemmas]
        return []

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


class _FakeConn:
    def __init__(self, sense_rows):
        self._cur = _FakeCursor(sense_rows)

    def cursor(self):
        return self._cur

    def close(self):
        return None


def _resolve(monkeypatch, text, sense_rows=_SEED_SENSE_ROWS, flag=None):
    if flag is not None:
        monkeypatch.setattr(S, "RECALL_SENSE_DISAMBIGUATION", flag)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake@/fake_test")
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda dsn: _FakeConn(sense_rows))
    return S._query_sense_resolution(text, "01234567-89ab-cdef-0123-456789abcdef")


# ---------------------------------------------------------------------------
# B. THE SENSE LOOKUP — the UD anchor + the query context select the sense
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected_class,expected_signal", [
    # the possessive-personal signature binds the kinship sense (BUG-RECALL-15 dir 1)
    ("Tell me about my family?", "kinship", "possessive_personal"),
    ("how is our family doing", "kinship", "possessive_personal"),
    # the compound collocation binds the non-personal senses (dir 2)
    ("what do I know about language families?", "taxonomy", "compound_collocation"),
    ("the family history of diabetes", "medical_history", "compound_collocation"),
    # the core-argument anchor binds the lemma's default sense
    ("I use the metric wrench daily", "instrumentality", "core_argument_default"),
])
def test_resolution_selects_the_grounded_sense(monkeypatch, text, expected_class,
                                               expected_signal):
    res = _resolve(monkeypatch, text)
    assert res is not None, text
    assert res["sense_class"] == expected_class
    assert res["signal"] == expected_signal


def test_resolution_family_ask_returns_kinship_sense_key(monkeypatch):
    res = _resolve(monkeypatch, "Tell me about my family?")
    assert res["sense_key"] == "family#kinship"
    assert res["lemma"] == "family"
    assert "language" in res["nonpersonal_discriminators"].get("taxonomy", [])
    assert "history" in res["nonpersonal_discriminators"].get("medical_history", [])


def test_resolution_language_families_returns_taxonomy_sense_key(monkeypatch):
    res = _resolve(monkeypatch, "what do I know about language families?")
    assert res["sense_key"] == "family#taxonomy"


def test_resolution_none_without_a_sense_row(monkeypatch):
    assert _resolve(monkeypatch, "what do I know about ducks?") is None
    assert _resolve(monkeypatch, "tell me about duck anatomy") is None


def test_resolution_flag_off_is_none(monkeypatch):
    assert _resolve(monkeypatch, "Tell me about my family?", flag=False) is None


def test_resolution_failsafe_on_db_error(monkeypatch):
    import psycopg2

    def _boom(dsn):
        raise RuntimeError("db down")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake@/fake_test")
    monkeypatch.setattr(psycopg2, "connect", _boom)
    assert S._query_sense_resolution(
        "Tell me about my family?",
        "01234567-89ab-cdef-0123-456789abcdef") is None


def test_resolution_none_without_a_dsn(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    assert S._query_sense_resolution(
        "Tell me about my family?",
        "01234567-89ab-cdef-0123-456789abcdef") is None


def test_resolution_failsafe_when_parser_unavailable(monkeypatch):
    import src.extraction.linguistics as L
    monkeypatch.setattr(L, "linguistics_available", lambda: False)
    assert S._query_sense_resolution(
        "Tell me about my family?",
        "01234567-89ab-cdef-0123-456789abcdef") is None


# ---------------------------------------------------------------------------
# C. THE SENSE-SCOPED SERVE — the collocation guard (kinship grounding)
# ---------------------------------------------------------------------------
def _facts():
    return [
        {"subject": "user", "rel_type": "has_pet", "object": "Moss the border collie",
         "definition": "You have a border collie named Moss.", "fact_class": "A",
         "confidence": 1.0, "provenance": "user_stated", "source": "db",
         "category": "pet", "temporal_status": "now", "polarity": "affirmed"},
        {"subject": "wren", "rel_type": "child_of", "object": "user",
         "definition": "Wren is your child.", "fact_class": "A",
         "confidence": 1.0, "provenance": "user_stated", "source": "db",
         "category": "kinship", "temporal_status": "now", "polarity": "affirmed"},
        # the NON-kinship token-match: a doc-minted row riding the bare "family" token
        {"subject": "algonquian", "rel_type": "instance_of",
         "object": "language family",
         "definition": "Algonquian is an language family of the Cree language family.",
         "fact_class": "B", "confidence": 0.6, "provenance": "doc_derived",
         "source": "staged", "category": "taxonomy", "temporal_status": "now",
         "polarity": "affirmed"},
        {"subject": "user", "rel_type": "has_state", "object": "family history",
         "definition": "A family history of diabetes and hypertension.",
         "fact_class": "B", "confidence": 0.6, "provenance": "doc_derived",
         "source": "staged", "category": "medical", "temporal_status": "now",
         "polarity": "affirmed"},
    ]


_KINSHIP_RES = {
    "sense_key": "family#kinship", "lemma": "family", "pos": "NOUN",
    "sense_class": "kinship", "ud_anchor": "pobj",
    "signal": "possessive_personal",
    "nonpersonal_discriminators": {"taxonomy": ["language", "linguistic"],
                                   "medical_history": ["history"]},
}
_TAXONOMY_RES = {
    "sense_key": "family#taxonomy", "lemma": "family", "pos": "NOUN",
    "sense_class": "taxonomy", "ud_anchor": "pobj",
    "signal": "compound_collocation", "nonpersonal_discriminators": {},
}


def test_guard_drops_nonkinship_collocation_rows_keeps_entities():
    out = S._sense_collocation_guard(_KINSHIP_RES, _facts())
    surfaces = [str(f.get("object")) for f in out]
    assert any("Moss" in s for s in surfaces)          # the entity row serves
    assert any(s == "user" for s in surfaces)          # the kinship row serves
    assert not any("language family" in s for s in surfaces)   # the decoy row DROPS
    assert not any(s == "family history" for s in surfaces)    # the medical row DROPS


def test_guard_plural_surfaces_drop():
    facts = [{"subject": "x", "rel_type": "instance_of",
              "object": "language families",
              "definition": "Language families evolve.", "fact_class": "B"}]
    assert S._sense_collocation_guard(_KINSHIP_RES, facts) == []


def test_guard_taxonomy_grounding_keeps_the_decoy_rows():
    """The OTHER direction: on a taxonomy-grounded ask the collocation rows stay —
    the guard never fires against its own grounded sense."""
    out = S._sense_collocation_guard(_TAXONOMY_RES, _facts())
    assert len(out) == len(_facts())


def test_guard_flag_off_is_byte_parity(monkeypatch):
    monkeypatch.setattr(S, "RECALL_SENSE_DISAMBIGUATION", False)
    assert S._sense_collocation_guard(_KINSHIP_RES, _facts()) == _facts()


def test_guard_no_resolution_is_byte_parity():
    assert S._sense_collocation_guard(None, _facts()) == _facts()


def test_guard_never_raises_on_junk_rows():
    junk = [None, "not-a-dict", {"subject": "language family"}]
    out = S._sense_collocation_guard(_KINSHIP_RES, junk)
    assert "not-a-dict" in out
    assert len(out) == 2  # the bare-subject collocation row drops, None passes


# ---------------------------------------------------------------------------
# D. THE RECALL PROJECTION (§4) — token-minimal, the dropped-field list is the guard
# ---------------------------------------------------------------------------
def _full_row():
    row = {
        "subject": "user", "rel_type": "has_pet", "object": "Moss",
        "definition": "You have a dog named Moss.", "fact_class": "A",
        "confidence": 1.0, "provenance": "user_stated",
        "fact_provenance": "user_stated", "event_date": "2026-11-12T00:00:00Z",
        "source": "db", "category": "pet", "valid_from": None, "valid_until": None,
        "temporal_status": "now", "polarity": "affirmed",
    }
    # §4's standardized layers, present as they will be when the receipt lands:
    row.update({
        "ud_bindings": {"nsubj": "wife"},
        "verbnet_class_id": "use-105",
        "framenet_frames": ["Kinship"],
        "ontolex_sense_ref": "family#kinship",
        "timex3_anchors": {"type": "DATE", "value": "1991-03-12"},
    })
    return row


def test_projection_keeps_exactly_the_serve_consumed_fields():
    out = S._recall_token_minimal_projection([_full_row()])
    assert len(out) == 1
    assert set(out[0].keys()) == set(S._RECALL_PROJECTION_KEEP)


def test_projection_dropped_field_list_is_the_guard():
    for field in S._RECALL_PROJECTION_DROPPED:
        assert field not in S._RECALL_PROJECTION_KEEP
    # §4's explicit list rides the guard list verbatim
    for field in ("ud_bindings", "verbnet_class_id", "framenet_frames",
                  "ontolex_sense_ref", "timex3_anchors"):
        assert field in S._RECALL_PROJECTION_DROPPED


def test_projection_strips_standardized_layers_when_present():
    out = S._recall_token_minimal_projection([_full_row()])
    assert "ud_bindings" not in out[0]
    assert "verbnet_class_id" not in out[0]
    assert "framenet_frames" not in out[0]
    assert "ontolex_sense_ref" not in out[0]
    assert "timex3_anchors" not in out[0]
    assert out[0]["definition"] == "You have a dog named Moss."
    assert out[0]["provenance"] == "user_stated"


def test_projection_preserves_order_and_passes_non_dicts():
    rows = [_full_row(), "keep-me", _full_row()]
    out = S._recall_token_minimal_projection(rows)
    assert out[0]["object"] == "Moss"
    assert out[1] == "keep-me"
    assert isinstance(out[2], dict) and out[2]["object"] == "Moss"
    assert len(out) == 3


def test_projection_flag_off_is_byte_parity(monkeypatch):
    monkeypatch.setattr(S, "RECALL_TOKEN_MINIMAL_PROJECTION", False)
    rows = [_full_row()]
    out = S._recall_token_minimal_projection(rows)
    assert out is rows


def test_projection_empty_and_junk_inputs_are_noops():
    assert S._recall_token_minimal_projection([]) == []
    assert S._recall_token_minimal_projection(None) is None


# ---------------------------------------------------------------------------
# E. THE GROUNDING EXEMPTION — the doc-only verdict's OTHER direction
# ---------------------------------------------------------------------------
_DOC_ONLY_MEMORY = "=== YOUR MEMORY ===\nfrom your document Boreal Forest Notes\n"


def test_grounding_doc_only_exempt_on_nonpersonal_sense():
    gv = S._self_verify_grounding_check(
        _DOC_ONLY_MEMORY, "what do I know about language families?",
        sense_resolution=_TAXONOMY_RES)
    assert gv["ok"] is True
    assert gv["doc_only"] is True
    assert "nonpersonal_sense_grounded" in gv["why"]


def test_grounding_doc_only_still_fails_without_a_resolution():
    """The deployed verdict is preserved when the table grounds nothing: the duck
    topic-ask keeps failing the doc-only band (the existing pin's contract)."""
    gv = S._self_verify_grounding_check(
        _DOC_ONLY_MEMORY, "what do I know about ducks?", sense_resolution=None)
    assert gv["ok"] is False
    assert "doc_only_fallback_grounding_violation" in gv["why"]


def test_grounding_doc_only_still_fails_on_kinship_grounding():
    gv = S._self_verify_grounding_check(
        _DOC_ONLY_MEMORY, "tell me about my family?",
        sense_resolution=_KINSHIP_RES)
    assert gv["ok"] is False


def test_grounding_sense_exempt_flag_off_restores_byte_parity(monkeypatch):
    monkeypatch.setattr(S, "RECALL_SENSE_DISAMBIGUATION", False)
    gv = S._self_verify_grounding_check(
        _DOC_ONLY_MEMORY, "what do I know about language families?",
        sense_resolution=_TAXONOMY_RES)
    assert gv["ok"] is False


def test_grounding_fact_bearing_serve_untouched_by_sense():
    memory = ("=== YOUR MEMORY ===\nYou are the parent of Reya.\n"
              "from your document Boreal Forest Notes\n")
    gv = S._self_verify_grounding_check(
        memory, "Tell me about my family?", sense_resolution=_KINSHIP_RES)
    assert gv["ok"] is True and gv["doc_only"] is False


# ---------------------------------------------------------------------------
# F. the DB-gated arm — the real senses table read (skip without a live DSN)
# ---------------------------------------------------------------------------
_DSN = os.environ.get("LADDER_TEST_DSN") or (
    "postgresql://fl_sense_test:sense323@127.0.0.1:5432/fl_sense_check_test"
    if os.environ.get("SENSE_LOCAL_DB") else None)


@pytest.mark.skipif(not _DSN, reason="LADDER_TEST_DSN not set")
def test_live_senses_table_read_and_migrations_applied():
    """On a migrated DB: the seeds are present, the rel_types FK wiring exists, and
    the resolver resolves the family ask against the REAL tenant table."""
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute("SELECT sense_key FROM senses ORDER BY sense_key")
            keys = {r[0] for r in cur.fetchall()}
            assert {"family#kinship", "family#taxonomy", "family#medical_history",
                    "use#instrumentality", "use#consumption", "use#utility"} <= keys
            cur.execute("SELECT count(*) FROM verbnet_classes")
            assert cur.fetchone()[0] >= 10
            cur.execute(
                "SELECT s.sense_key, v.class_id FROM sense_verbnet_bindings b "
                "JOIN senses s ON s.id = b.sense_id "
                "JOIN verbnet_classes v ON v.id = b.verbnet_class_id")
            assert ("use#instrumentality", "use-105") in cur.fetchall()
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'rel_types' AND column_name = 'sense_id'")
            assert cur.fetchone() is not None
            cur.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'rel_types'::regclass AND conname = 'rel_types_sense_id_fk'")
            assert cur.fetchone() is not None
    finally:
        conn.close()

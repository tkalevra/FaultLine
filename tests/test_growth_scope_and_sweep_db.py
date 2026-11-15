"""GROWTH-INTO-UNKNOWN scope + growth-loop pins, executed against a REAL PostgreSQL.

Two measured faceplants (fresh seat, eea12a11, pre-prod test DB) that the scripted
pins cannot reach:

1. SCOPE REACH. Engine-grown groupings are named by the OBSERVED group alias --
   usually MULTI-WORD ("astronomy club"). The taxonomy-name lane matched ONE keyword
   by exact equality, so "who is in my astronomy club" resolved NO scope (0 of 3
   probe queries scoped) even though the grouping existed, was healthy, and the
   INTENT matcher (_query_references_grouping) already accepted the same multi-word
   name by substring. Fix pinned here: contiguous query n-grams (2..4 words,
   stopword edges skipped, exact equality on taxonomy_name and its space-normalized
   form) bind the named grouping's scope.

2. THE GROWTH LOOP. A unit_scalar candidate recorded in ontology_evaluations must
   GROW into the tenant map (carved value = the keyed-map rel), not resolve away as
   cue_skipped. Pre-fix the sweep grew 0 of N unit candidates -- the decade/quarter
   faceplant.

Skips cleanly without a throwaway database: set GROWTH_SCOPE_TEST_DSN. The DDL
mirrors the per-tenant column shapes that matter (facts/aliases TEXT ids,
text[] constraint arrays) so the real statements run unmodified.
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

main = pytest.importorskip("src.api.main")
emb = pytest.importorskip("src.re_embedder.embedder")
ling = pytest.importorskip("src.extraction.linguistics")
lco = pytest.importorskip("src.api.linguistic_cue_overlay")
rto = pytest.importorskip("src.api.rel_type_overlay")

DSN = os.environ.get("GROWTH_SCOPE_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set GROWTH_SCOPE_TEST_DSN to a THROWAWAY postgres to run the DB-backed growth pins",
)

_DDL = """
CREATE SCHEMA {sch};
SET search_path TO {sch};
CREATE TABLE rel_types (rel_type text PRIMARY KEY, natural_language text,
    is_hierarchy_rel boolean DEFAULT false, wikidata_pid text,
    label text, source text, engine_generated boolean DEFAULT false,
    head_types text[] DEFAULT '{{}}', tail_types text[] DEFAULT '{{}}');
CREATE TABLE rel_type_aliases (alias text PRIMARY KEY, canonical_rel_type text);
CREATE TABLE entities (id text PRIMARY KEY, entity_type text DEFAULT 'unknown');
CREATE TABLE entity_aliases (entity_id text, alias text, is_preferred boolean DEFAULT false,
    PRIMARY KEY (entity_id, alias));
CREATE TABLE entity_taxonomies (taxonomy_name text PRIMARY KEY, description text,
    member_entity_types text[] DEFAULT '{{}}', rel_types_defining_group text[] DEFAULT '{{}}',
    has_transitivity boolean DEFAULT false, transitive_rel_types text[] DEFAULT '{{}}',
    is_hierarchical boolean DEFAULT false, parent_rel_type text,
    member_taxonomies text[] DEFAULT '{{}}', severed_taxonomies text[] DEFAULT '{{}}',
    source text DEFAULT 'seeded');
CREATE TABLE linguistic_cues (cue text, category text, description text,
    source text, global_confidence float, frequency int DEFAULT 0,
    confirmed_count int DEFAULT 0, is_active boolean DEFAULT true,
    archived_at timestamptz, last_matched_at timestamptz,
    UNIQUE (cue, category));
CREATE TABLE ontology_evaluations (id serial PRIMARY KEY, candidate_rel_type text,
    candidate_subject_type text, candidate_object_type text, sample_subject_id text,
    sample_object text, extraction_method text, decision_reason text,
    occurrence_count int DEFAULT 1, last_seen_at timestamptz DEFAULT now(),
    re_embedder_decision text, decision_timestamp timestamptz);
CREATE TABLE extraction_patterns (pattern_id serial PRIMARY KEY, pattern_text text, category text);
CREATE TABLE negation_patterns (id serial PRIMARY KEY, pattern_text text);
CREATE TABLE correction_patterns (id serial PRIMARY KEY, pattern_text text);
CREATE TABLE intent_pattern_cache (id serial PRIMARY KEY, pattern_text text);
CREATE TABLE temporal_patterns (id serial PRIMARY KEY, pattern_text text);
INSERT INTO rel_types VALUES ('member_of', NULL, true, NULL, '{{ANY}}', '{{ANY}}');
INSERT INTO rel_types VALUES ('located_in', NULL, true, 'P131', '{{ANY}}', '{{Location}}');
INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types,
    rel_types_defining_group, source)
VALUES ('astronomy club', 'auto-grown grouping from observed member_of membership',
        ARRAY['organization','person'], ARRAY['member_of'], 'engine_grown_ingest');
INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types,
    rel_types_defining_group, source)
VALUES ('computer_system', 'seeded demo', ARRAY['concept','object'],
        ARRAY['located_at','part_of'], 'seeded');
"""


@pytest.fixture()
def db():
    sch = "growth_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute(_DDL.format(sch=sch))
    conn.commit()
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {sch}")
    yield conn, sch  # determine_path takes a CONNECTION (it opens its own cursors)
    conn.rollback()
    with conn.cursor() as c:
        c.execute(f"DROP SCHEMA {sch} CASCADE")
    conn.commit()
    conn.close()


def test_query_naming_a_grown_grouping_scopes_it(db):
    cur, _sch = db
    path = main.determine_path("who is in my astronomy club", cur, None)
    assert "astronomy club" in path.taxonomy_groups, path.taxonomy_groups
    assert path.scope_active is True
    assert "member_of" in path.relationship_rels, path.relationship_rels


def test_underscored_taxonomy_answers_the_spaced_phrase(db):
    cur, _sch = db
    path = main.determine_path("what is in my computer system", cur, None)
    assert "computer_system" in path.taxonomy_groups, path.taxonomy_groups


def test_unrelated_query_binds_no_grown_group_by_name(db):
    cur, _sch = db
    path = main.determine_path("tell me about myself", cur, None)
    assert "astronomy club" not in path.taxonomy_groups
    assert "computer_system" not in path.taxonomy_groups


def test_single_word_taxonomy_lane_unchanged(db):
    cur, _sch = db
    with cur.cursor() as c:
        c.execute(
            "INSERT INTO entity_taxonomies (taxonomy_name, description, "
            "member_entity_types, rel_types_defining_group, source) "
            "VALUES ('work', 'seeded', ARRAY['person'], ARRAY['works_for'], 'seeded')")
        cur.commit()
    path = main.determine_path("tell me about my work", cur, None)
    assert "work" in path.taxonomy_groups, path.taxonomy_groups


def test_unit_candidate_grows_into_the_tenant_map(db):
    cur, sch = db
    with cur.cursor() as c:
        c.execute(
            "INSERT INTO ontology_evaluations (candidate_rel_type, candidate_subject_type, "
            "candidate_object_type, sample_subject_id, sample_object, extraction_method, "
            "occurrence_count) VALUES ('unit_scalar','unknown','unit_scalar','__cue__','decade', "
            "'linguistic_cue_candidate', %s)",
            (emb.LINGUISTIC_CUE_GROWTH_THRESHOLD,),
        )
        cur.commit()
    stats = emb.grow_linguistic_cue_candidates(cur, sch)
    assert stats["grown"] == 1, stats
    with cur.cursor() as c:
        c.execute("SELECT description, source FROM linguistic_cues WHERE cue='decade' AND category='unit_scalar'")
        row = c.fetchone()
        assert row is not None, "the grown row must land in the TENANT map"
        assert row[0] == "related_measure", (
            "the grown value must be the KEYED-MAP VALUE (the scalar rel), never a prose note")
        assert row[1] == "grown"
        c.execute("SELECT re_embedder_decision FROM ontology_evaluations WHERE sample_object='decade'")
        assert c.fetchone()[0] == "cue_grown"


def test_post_growth_second_sighting_keeps_magnitude_and_unit(db, monkeypatch):
    # NEXT-SIGHTING pin (critic R1 MINOR 1; the MAJOR-2 tripwire). Live-measured at
    # fa24e717: once the map row (decade -> related_measure) grew, the SAME sentence
    # routed through the MAPPED arm whose emit wrote MAGNITUDE-ONLY --
    # (warranty, related_measure, '3') with no datatype, '3 of nothing', unit
    # irrecoverable, family recognition NOT lighting up under a generic rel. The
    # contract now: when the map VALUE is the GENERIC rel, the mapped emit keeps the W2
    # verbatim (magnitude + unit, quantity) -- the exact capture the unmapped arm
    # produced PRE-growth, byte for byte. This pin stops at the grown row's NEXT
    # sighting, which is the only place the regression is observable.
    cur, sch = db
    with cur.cursor() as c:
        c.execute(
            "INSERT INTO linguistic_cues (cue, category, description, source, "
            "global_confidence, frequency, confirmed_count, is_active) "
            "VALUES ('decade', 'unit_scalar', 'related_measure', 'grown', 0.75, 1, 1, true)")
    cur.commit()
    tok = rto.set_current_schema(sch)
    try:
        lco.invalidate(sch)  # the 5s TTL overlay cache must see the grown row
        # The deriver resolves the map via POSTGRES_DSN at CALL time, so binding the env
        # here points the overlay read at this throwaway schema-bound DSN.
        monkeypatch.setenv("POSTGRES_DSN", DSN)
        growth: list = []
        facts = ling.derive_sentence_facts(
            "The warranty is 3 decades.", "2026-11-06", growth_out=growth)
    finally:
        rto.reset_current_schema(tok)
    rels = {(f.subject, f.rel_type, f.object) for f in facts}
    assert ("warranty", "related_measure", "3 decades") in rels, (
        "post-growth second sighting MUST keep magnitude+unit verbatim under the "
        "generic rel (W2) -- a bare '3' here is the measured fa24e717 degradation", rels)
    assert ("warranty", "related_measure", "3") not in rels, rels
    # Mapped now: the unmapped arm must NOT re-propose the already-grown unit.
    assert not any(cat == "unit_scalar" for (_c, cat) in growth), growth


def test_post_growth_degree_adjective_without_dimension_keeps_one_unit(db, monkeypatch):
    # THE MISSING SHAPE (critic R2 NEW MAJOR, through a REAL engine-minted row): a
    # degree adjective with NO WordNet dimension ('tight' -- NOT 'strong') still sets
    # the agreeing-degree token; Q8 is skipped (related_measure admits every subject);
    # the #52 metadata-extension block saw the REAL grown row's metadata dict (truthy,
    # datatype undeclared) and extended the value, and the generic branch appended
    # num.head AGAIN -> '3 decades decades'. Live-measured at the R2 verdict. The #52
    # block is FAMILY-ONLY: for a generic map value the generic branch is the SINGLE
    # owner of unit-extension. This pin drives the REAL row path (a genuine grown row
    # in a real rel_types table) so no simulated metadata can mask the wound.
    cur, sch = db
    with cur.cursor() as c:
        c.execute(
            "INSERT INTO linguistic_cues (cue, category, description, source, "
            "global_confidence, frequency, confirmed_count, is_active) "
            "VALUES ('decade', 'unit_scalar', 'related_measure', 'grown', 0.75, 1, 1, true)")
        # the engine MINTS the generic rel on first use -- recreate that steady state
        c.execute(
            "INSERT INTO rel_types (rel_type, source) "
            "VALUES ('related_measure', 'engine')")
    cur.commit()
    tok = rto.set_current_schema(sch)
    try:
        lco.invalidate(sch)
        monkeypatch.setenv("POSTGRES_DSN", DSN)
        growth: list = []
        facts = ling.derive_sentence_facts(
            "The knot is 3 decades tight.", "2026-11-06", growth_out=growth)
    finally:
        rto.reset_current_schema(tok)
    rels = {(f.subject, f.rel_type, f.object) for f in facts}
    assert ("knot", "related_measure", "3 decades") in rels, rels
    assert ("knot", "related_measure", "3 decades decades") not in rels, (
        "the unit was appended TWICE (#52 + generic branch) on a REAL grown row -- "
        "the #52 block must skip generic map values", rels)
    assert ("knot", "related_measure", "3") not in rels, rels

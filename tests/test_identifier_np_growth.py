"""Issue #225 — the label+code NP (identifier designation) grows the ontology around its label.

THE WOUND (reproduced at the deriver on this branch pre-fix, DEV/review/gauntlet/grow-shape-225/
round_1/PHASE1-REPRO.txt): "My opnsense box sits at 192.168.0.1 on VLAN 30." derived
``(opnsense box, sit_at_on, vlan)`` — a verb-mash rel with the bare label noun minted as a
relational object entity — and the numeral 30 was ANNIHILATED (residue-uncovered).

THE OWNER RULING (2026-10-07): a label+code construction GROWS the ontology around the label
(bounded, via the existing growth rails) and stores the value as a grounded memory hanging off
that structure — smart on ingest, dumb on recall, for ALL things. NOT a networking lane: the
detector is cue-class-gated (identifier_noun role 'label'), the vocabulary is DB-held
(vlan/port/channel/rack seeded by migration 293; every other domain GROWS per-tenant), and the
engine code carries ZERO domain nouns.

Pinned here (subject-agnostic — FRESH subjects: pottery/sailing/brewing, never networking):
  (a) label+code turn → the code lands as a SCALAR named by the noun on the subject + zero junk
      edges (no verb-mash, no noun-as-object);
  (c) a grown tenant cue row overrides the floor; a suppressed (is_active=false) cue does not
      fire; the floor alone still captures;
  (d) no rival entity minted for the noun (deriver-level: the noun never appears as a
      subject/object surface; the /ingest-level evidence is the E2E in round_1/);
  (e) the verb-mash rel class (sit_at_on — compound verb+PP) reduces to its particle-stripped
      root / canonical, never mints raw;
  plus: a COLD noun (not yet in the class) declines honestly and queues the identifier_noun
  growth candidate (the V4a growth_out rail), and the #23-family planner skip keeps a label
  scalar flat on the subject (never re-homed onto a concept entity).

DB-bound pins run against the throwaway test DB (POSTGRES_DSN); each test mints its own FRESH
tenant schema so tenant-state sensitivity (the #31 lesson) cannot hide a wound.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import rel_type_overlay, linguistic_cue_overlay
from src.extraction import linguistics


def _cue_invalidate(schema):
    """Drop the CUE overlay's per-tenant caches (its own 5s-TTL keyed-map cache is separate
    from rel_type_overlay's)."""
    linguistic_cue_overlay.invalidate(schema)

DSN = os.environ.get("POSTGRES_DSN", "")
_db = pytest.mark.skipif(not DSN, reason="POSTGRES_DSN unset — DB-bound pin cannot run")

_WOUND = "My opnsense box sits at 192.168.0.1 on VLAN 30."


def _edges(sentence, growth=None):
    g = growth if growth is not None else []
    facts = linguistics.derive_sentence_facts(sentence, reference="user", prior_nps=[],
                                              growth_out=g)
    return facts, g


def _mk_schema(dsn):
    """A FRESH tenant schema carrying migration 293's identifier label rows (the real rails:
    boot-apply the migration file against public, mint the tenant from the template)."""
    import psycopg2
    user_id = str(uuid.uuid4())
    schema = f"faultline_{user_id.replace('-', '_')[:63]}"
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            # minimal tenant tables FIRST (the migration file's fan-out targets every
            # faultline_% schema and must find the table; provisioning creates them from the
            # template before any migration sweep would run)
            cur.execute(f'CREATE TABLE IF NOT EXISTS "{schema}".linguistic_cues '
                        '(LIKE public.linguistic_cues INCLUDING ALL)')
            for tbl in ("rel_type_aliases", "rel_types"):
                cur.execute(f'CREATE TABLE IF NOT EXISTS "{schema}".{tbl} '
                            f'(LIKE public.{tbl} INCLUDING ALL)')
                cur.execute(f'INSERT INTO "{schema}".{tbl} SELECT * FROM public.{tbl}')
            # then apply the migration file — its fan-out seeds THIS schema exactly as the
            # boot ledger would (public side is idempotent via ON CONFLICT DO NOTHING)
            mig = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "migrations", "293_linguistic_cues_identifier_label.sql")
            cur.execute(open(mig).read())
        conn.commit()
    finally:
        conn.close()
    return schema


def _grow(dsn, schema, cue, role, active=True):
    """Simulate a TENANT-GROWN identifier cue row (what the re_embedder's freq-gated candidate
    drain writes) — the rail a pottery/sailing/brewing tenant uses to teach itself its own
    designators. Invalidates the CUE overlay's own keyed-map cache (linguistic_cue_overlay keeps
    its own 5s-TTL cache; rel_type_overlay.invalidate does not touch it)."""
    import psycopg2
    from src.api import linguistic_cue_overlay
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{schema}".linguistic_cues (cue, category, description, source, is_active) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT (cue, category) DO UPDATE '
                'SET description = EXCLUDED.description, is_active = EXCLUDED.is_active',
                (cue, "identifier_noun", role, "grown", active))
        conn.commit()
    finally:
        conn.close()
    linguistic_cue_overlay.invalidate(schema)


# ── (a) the wound + fresh subjects: the code lands as a scalar named by the noun ─────────────

@_db
def test_wound_sentence_lands_designation_scalar():
    """THE MEASURED WOUND: 'on VLAN 30' → (opnsense box, vlan, "30") — scalar on the subject —
    and NO verb-mash edge, NO bare-noun object, NO annihilated numeral."""
    schema = _mk_schema(DSN)
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        facts, _ = _edges(_WOUND)
        by_rel = {(f.subject, f.rel_type): f for f in facts}
        scalar = by_rel.get(("opnsense box", "vlan"))
        assert scalar is not None, f"designation scalar missing: {[ (f.subject, f.rel_type, f.object) for f in facts]}"
        assert scalar.object == "30" and scalar.scalar_datatype == "string"
        # zero junk: no verb-mash rel, the noun is never an edge surface
        for f in facts:
            assert "_" not in f.rel_type or f.rel_type in ("sit_at",), \
                f"verb-mash rel survived: {f.rel_type}"
            assert f.object != "vlan", "the label noun became a relational object"
            assert f.subject != "vlan", "the label noun became a referent subject"
        # the IP still rides its own lane (atomic has_ip at /ingest)
        assert by_rel.get(("opnsense box", "sit_at")) is not None
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema)


@_db
def test_fresh_subjects_capture_on_grown_rows():
    """Subject-agnostic proof on FRESH domains (pottery / sailing / brewing — never networking):
    with the tenant's OWN grown cue rows (the freq-gated rail a real tenant uses), each
    label+code NP lands the code as a scalar named by the noun on the subject, and the SVO
    verb-mash twin never fires."""
    schema = _mk_schema(DSN)
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        for noun, sent, subj in (
            ("cone", "I fire my glazes at cone 6.", "user"),
            ("berth", "My Laser is moored at berth 14.", "laser"),
            ("vessel", "My stout fermented in vessel 7.", "stout"),
        ):
            _grow(DSN, schema, noun, "label")
            facts, growth = _edges(sent)
            scalars = [f for f in facts if f.rel_type == noun]
            assert scalars, f"{noun}: designation scalar missing in {[(f.subject, f.rel_type, f.object) for f in facts]}"
            assert scalars[0].subject == subj and scalars[0].scalar_datatype == "string"
            # the noun never appears as a relational surface
            for f in facts:
                assert f.object != noun, f"{noun}: noun became a relational object"
                assert not f.rel_type.endswith(("_at", "_in", "_on")) or f.rel_type == "sit_at", \
                    f"{noun}: verb-mash twin survived: {f.rel_type}"
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema)


@_db
def test_nested_designations_each_land():
    """"on rack 4 in bay 2" — EACH designation lands its own scalar once 'bay' is grown; the
    nested PP is NOT read as rack's value (the _chain_measure_pp twin is suppressed)."""
    schema = _mk_schema(DSN)
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        _grow(DSN, schema, "bay", "label")
        _cue_invalidate(schema)
        facts, _ = _edges("The kiln sits on rack 4 in bay 2.")
        vals = {(f.subject, f.rel_type): f.object for f in facts if f.scalar_datatype}
        assert vals.get(("kiln", "rack")) == "4", vals
        assert vals.get(("kiln", "bay")) == "2", vals
        assert ("kiln", "rack") in vals and vals[("kiln", "rack")] != "bay 2", \
            "the nested designation leaked in as rack's value"
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema)


# ── (c) cue-rail discipline: floor ∪ tenant, tenant wins, suppression honored ───────────────

@_db
def test_floor_alone_captures():
    """A tenant holding ZERO identifier rows still captures the wound sentence — the code floor
    (mirroring migration 293) is the DB-DOWN fail-safe, never the authority."""
    import psycopg2
    schema = _mk_schema(DSN)
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM "{schema}".linguistic_cues')
        conn.commit()
    finally:
        conn.close()
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        _cue_invalidate(schema)
        facts, _ = _edges(_WOUND)
        assert any(f.rel_type == "vlan" and f.object == "30" for f in facts), \
            "empty tenant lost the floor (the REPLACE regression shape)"
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema)


@_db
def test_tenant_row_overrides_the_floor():
    """AUTHORITY ORDER (user > seed > growth applied over the floor): a tenant row re-roling
    'vlan' as 'suffix' WINS over the floor's 'label' — the designation chain then declines
    (a suffix head without a strong compound is not a designation head)."""
    schema = _mk_schema(DSN)
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        _grow(DSN, schema, "vlan", "suffix")
        _cue_invalidate(schema)
        facts, growth = _edges(_WOUND)
        assert not any(f.rel_type == "vlan" for f in facts), \
            "tenant override did not win over the floor"
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema)


@_db
def test_suppressed_cue_does_not_fire():
    """A suppressed (is_active=false) tenant cue does not fire — the union honors the
    suppression; the construction declines honestly and the growth rail re-proposes the noun
    (freq-gated; the existing suppressed ROW survives an ON CONFLICT DO NOTHING re-grow)."""
    schema = _mk_schema(DSN)
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        _grow(DSN, schema, "berth", "label", active=False)
        _cue_invalidate(schema)
        facts, growth = _edges("My Laser is moored at berth 14.")
        assert not any(f.rel_type == "berth" for f in facts), \
            "a suppressed cue fired"
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema)


# ── miss → grow: a cold noun declines honestly and rides the growth rail ────────────────────

@_db
def test_cold_noun_declines_and_queues_growth_candidate():
    """A noun NOT yet in the class captures NOTHING by shape alone (no junk mint — the fail-safe
    that keeps 'four movies' style noise out) and carries the noun out on the V4a growth rail so
    the re_embedder's freq-gate (>=3) grows the class."""
    schema = _mk_schema(DSN)
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        facts, growth = _edges("My Laser is moored at berth 14.")
        assert not any(f.rel_type == "berth" for f in facts), \
            "captured on structure alone — the cue gate is not holding"
        assert ("berth", "identifier_noun") in [tuple(g) for g in growth], growth
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema)


@_db
def test_prenominal_quantifier_never_fires_or_queues():
    """The count lanes' territory: a PREnominal cardinal ('I bought four movies') never fires the
    designation chain and never queues its noun — position IS the meaning (post-nominal numeral
    = unique reference; prenominal = quantifier)."""
    schema = _mk_schema(DSN)
    tok = rel_type_overlay.set_current_schema(schema)
    try:
        facts, growth = _edges("I bought four movies at the festival.")
        assert not any(f.rel_type == "movie" for f in facts)
        assert ("movie", "identifier_noun") not in [tuple(g) for g in growth], growth
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema)


# ── (e) the verb-mash rel class reduces structurally — never mints raw ──────────────────────

@_db
def test_compound_verb_pp_reduces_to_root():
    """'sit_at_on' (both PPs of the wound glued onto the verb) reduces to the bare verb root —
    'sit' has no alias, so the ROOT routes to the standing pending_placement rail; the raw
    multi-particle surface never survives."""
    from src.api.main import _get_canonical_rel_type
    schema = _mk_schema(DSN)
    try:
        assert _get_canonical_rel_type("sit_at_on", schema=schema) == "sit"
        assert _get_canonical_rel_type("reside_on_at", schema=schema) == "reside"
    finally:
        _drop(schema)


@_db
def test_compound_verb_pp_resolves_root_alias():
    """'work_at_on' reduces to 'work', whose seeded alias resolves works_for — the dprompt-125
    single-strip behavior extended to the compound class."""
    from src.api.main import _get_canonical_rel_type
    schema = _mk_schema(DSN)
    try:
        assert _get_canonical_rel_type("work_at_on", schema=schema) == "works_for"
        # single-particle unknowns keep today's behavior (the mash guard is >=2 particles)
        assert _get_canonical_rel_type("care_for", schema=schema) == "care_for"
        # a known canonical rel is never stripped (parent_of ends in a prep by NAMING CONVENTION)
        import psycopg2
        conn = psycopg2.connect(DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
                cur.execute("SELECT count(*) FROM rel_types WHERE rel_type = 'parent_of'")
                if cur.fetchone()[0]:
                    assert _get_canonical_rel_type("parent_of", schema=schema) == "parent_of"
        finally:
            conn.close()
    finally:
        _drop(schema)


# ── the #23-family planner skip keeps a label scalar FLAT on the subject ─────────────────────

def test_planner_never_rehomes_a_label_rel():
    """The pending-grounding planner must keep a label+code scalar flat on the subject — the
    re-home would register the rel surface as an entity and collide at THE HARD LINE (a rel_type
    name cannot be an entity), moving the designation one hop off the subject where the
    attribute-interrogative walk reads the ANCHOR'S OWN attributes. Pure-planner pin (no DB)."""
    from src.api.main import _plan_pending_scalar_grounding, _PENDING_GROUND_SKIP_RELS
    edges = [{"subject": "opnsense box", "rel_type": "vlan", "object": "30"}]
    plan = _plan_pending_scalar_grounding(
        edges, {}, {"unit": "quantity"}, skip_rels=_PENDING_GROUND_SKIP_RELS | frozenset({"vlan"}))
    assert plan["edges"] == edges and not plan["concepts"], plan
    # without the skip the SAME edge (a unit-carrying variant) WOULD re-home — proving the skip
    # is what holds it, not an accident of shape
    edges_unit = [{"subject": "opnsense box", "rel_type": "vlan", "object": "4 units"}]
    plan2 = _plan_pending_scalar_grounding(edges_unit, {}, {"unit": "quantity"})
    assert plan2["changed"] and plan2["concepts"] == ["vlan"], plan2


def _drop(schema):
    import psycopg2
    try:
        conn = psycopg2.connect(DSN)
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 — teardown is best-effort
        pass


# ── the miss→grow rail end-to-end (critic round 1 gap: _CARVED lacked identifier_noun) ──────

@_db
def test_growth_sweep_grows_identifier_noun_candidate():
    """The freq-gated rail must actually GROW a novel label noun: candidate queued by the
    spine (record_cue_candidate) → re_embedder sweep → tenant linguistic_cues row with
    description='label' (THE ROLE, not a prose note — resolve_identifier_noun_roles parses
    description as the role value) → the overlay resolves the noun as a 'label' head.
    Critic round 1 measured this rail BUILT AND DARK (occ=3 candidates, sweep {'grown': 0}
    — every candidate 'cue_skipped' because _CARVED lacked the category)."""
    import psycopg2
    from src.api import linguistic_cue_overlay
    from src.re_embedder.embedder import grow_linguistic_cue_candidates

    schema = _mk_schema(DSN)
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE IF NOT EXISTS "{schema}".ontology_evaluations '
                        f'(LIKE public.ontology_evaluations INCLUDING ALL)')
            cur.execute(
                f'INSERT INTO "{schema}".ontology_evaluations '
                '(user_id, candidate_rel_type, candidate_subject_type, candidate_object_type,'
                ' sample_subject_id, sample_object, extraction_method, decision_reason,'
                ' occurrence_count, last_seen_at) '
                "VALUES ('pin-seat', 'identifier_noun', 'unknown', 'identifier_noun',"
                " '__cue__', 'treadle', 'linguistic_cue_candidate',"
                " 'carved cue-class candidate — freq-gated per-tenant growth', 3, now()) "
                "RETURNING id")
            cand_id = cur.fetchone()[0]
            cur.execute(
                f'INSERT INTO "{schema}".ontology_evaluations '
                '(user_id, candidate_rel_type, candidate_subject_type, candidate_object_type,'
                ' sample_subject_id, sample_object, extraction_method, decision_reason,'
                ' occurrence_count, last_seen_at) '
                "VALUES ('pin-seat', 'not_a_carved_class', 'unknown', 'not_a_carved_class',"
                " '__cue__', 'widget', 'linguistic_cue_candidate',"
                " 'carved cue-class candidate — freq-gated per-tenant growth', 3, now())")
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        stats = grow_linguistic_cue_candidates(conn, schema)
        assert stats["grown"] == 1 and stats["errors"] == 0, stats
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT cue, category, description, is_active FROM "{schema}".linguistic_cues '
                "WHERE cue = 'treadle' AND category = 'identifier_noun'")
            row = cur.fetchone()
            assert row == ("treadle", "identifier_noun", "label", True), row
            cur.execute(
                f'SELECT re_embedder_decision FROM "{schema}".ontology_evaluations WHERE id = %s',
                (cand_id,))
            assert cur.fetchone()[0] == "cue_grown"
            cur.execute(
                f"SELECT count(*) FROM \"{schema}\".linguistic_cues WHERE cue = 'widget'")
            assert cur.fetchone()[0] == 0  # un-carved category resolved away, nothing grown
            cur.execute(
                f"SELECT re_embedder_decision FROM \"{schema}\".ontology_evaluations "
                "WHERE sample_object = 'widget'")
            assert cur.fetchone()[0] == "cue_skipped"
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        conn.close()
        _drop(schema)

    # the overlay resolves the GROWN row as a label head (invalidate first — 5s TTL cache)
    schema2 = _mk_schema(DSN)
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE IF NOT EXISTS "{schema2}".ontology_evaluations '
                        f'(LIKE public.ontology_evaluations INCLUDING ALL)')
            cur.execute(
                f'INSERT INTO "{schema2}".ontology_evaluations '
                '(user_id, candidate_rel_type, candidate_subject_type, candidate_object_type,'
                ' sample_subject_id, sample_object, extraction_method, decision_reason,'
                ' occurrence_count, last_seen_at) '
                "VALUES ('pin-seat', 'identifier_noun', 'unknown', 'identifier_noun',"
                " '__cue__', 'berth', 'linguistic_cue_candidate',"
                " 'carved cue-class candidate — freq-gated per-tenant growth', 3, now())")
            cur.execute(f'SET search_path TO "{schema2}"')
        conn.commit()
        stats = grow_linguistic_cue_candidates(conn, schema2)
        assert stats["grown"] == 1, stats
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        conn.close()
    linguistic_cue_overlay.invalidate(schema2)
    tok = rel_type_overlay.set_current_schema(schema2)
    try:
        roles = linguistic_cue_overlay.resolve_identifier_noun_roles(DSN)
        assert roles.get("berth") == "label", roles.get("berth")
    finally:
        rel_type_overlay.reset_current_schema(tok)
        _drop(schema2)

"""RECALL-SCALAR-SURFACING (issue #13) — the measure interrogative renders the type node's
own corrected scalar alongside the ladder.

THE BUG (measured live, deploy #7, and reproduced in-process in
DEV/review/gauntlet/recall-scalar-surfacing/round_1/repro.txt): the chain
kind-statement → verb-measure scalar → /retract/correct correction lands
``entity_attributes: measure = 7 centimeters | user_corrected`` on the ladder, but
recall("How big does the blue-ringed octopus get?") rendered ONLY the ladder — the
scalar was retrieved (anchor lane / traversed-attribute lane) and then DROPPED by the
render-selection gates (convert_to_prose's topic-grounding lexical gate; on the
compound-anchor shape the admission itself admitted nothing because the row lives one
classification rung down, on the head).

THE FIX (two seams, both retrieve-and-present — no ingest changes, nothing invented):
  1. main.py::determine_path measure-interrogative admission — when the anchor's OWN
     rows admit nothing, resolve the family from the classification ladder the walk is
     about to descend (metadata rels, BFS bounded by QUERY_MULTIHOP_CLIMB_DEPTH,
     attributes-only, same cue-class family test).
  2. main.py::convert_to_prose topic-gate fork — an attribute-store row
     (source == "attributes") whose rel the aspect admission bound (path.scalar_rels)
     is ON-TOPIC BY CONSTRUCTION (same authority as the aspect_bound_rels exemption)
     and is exempt from the lexical gate. Voice unchanged: the row bands by its own
     stored provenance (user_corrected reads user_stated → asserted).

FROZEN BARS (issue #13):
  1. E2E fresh-seat chain: kind → measure → /retract/correct → recall interrogative
     renders the corrected value ASSERTED alongside the ladder (both anchor shapes).
  2. Aspect noise matrix: "How big is X?" / "How long is X?" / "What size is X?" admit
     the measure family; "What colour is X?" invents nothing.
  3. Ladder behaviour unchanged: the stated subclass rung still renders.
  4. Provenance voice: user_corrected → asserted; the E2E reads the row back.

Runs the REAL harvest → /ingest → /retract/correct → /query against the throwaway
POSTGRES_DSN tenant (brain mocked to the live deploy's extraction shape, exactly like
tests/test_correction_scalar_addressing_cx12.py). Skips without a test-named DSN —
disclosed, never a silent pass.
"""

import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

# --------------------------------------------------------------------------- #
# 0. Unit seams — the admission and the exemption, no DB required
# --------------------------------------------------------------------------- #


def test_measure_interrogative_frame_is_grammatical_not_a_word_list():
    from src.api.main import _is_measure_interrogative
    # the degree frame: BOTH spaCy parses (eventive "how big does X get" and copular
    # "how long is X" — issue #13's unanswered shape)
    assert _is_measure_interrogative("How big does the blue-ringed octopus get?")
    assert _is_measure_interrogative("How big is the octopus?")
    assert _is_measure_interrogative("How long is the king cobra?")
    assert _is_measure_interrogative("How heavy does the parchment get?")
    # count/sum asks are refused GRAMMATICALLY (many/much modifies a NOUN, never a
    # clause predicate) — the aggregation parser owns them
    assert not _is_measure_interrogative("How many octopuses are there?")
    assert not _is_measure_interrogative("How much money do I have?")
    assert not _is_measure_interrogative("What is an octopus?")
    assert not _is_measure_interrogative("")


def test_measure_aspect_np_frame_binds_through_the_tenant_class_only():
    """The wh-attr frame ("what size is X?") is GRAMMAR + tenant metadata, never a word
    list: the frame returns the aspect NOUN; whether that noun measures is the tenant's
    ``measure_verb`` cue class decision."""
    from src.api.main import _is_measure_aspect_np
    assert _is_measure_aspect_np("What size is the octopus?") == "size"
    assert _is_measure_aspect_np("What measure is the octopus?") == "measure"
    assert _is_measure_aspect_np("What colour is the octopus?") == "colour"
    assert _is_measure_aspect_np("What is an octopus?") is None
    assert _is_measure_aspect_np("How big is the octopus?") is None


def test_topic_gate_exemption_keys_on_attribute_store_rows_only():
    """A relational/ladder row on the admitted rel name is NOT exempt — only an
    entity_attributes-sourced row (source == 'attributes') is the anchor's own value."""
    from src.api.main import _scalar_canon_provenance
    # the voice contract this fix relies on: user_corrected reads as user_stated
    assert _scalar_canon_provenance("user_corrected") == "user_stated"
    assert _scalar_canon_provenance(None) == "llm_inferred"


# --------------------------------------------------------------------------- #
# 1. E2E — the fresh-seat chain, rows read back, both anchor shapes
# --------------------------------------------------------------------------- #

pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")


@pytest.fixture()
def rs13_tenant(monkeypatch):
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
    monkeypatch.delenv("SAAS_MODE", raising=False)
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid, derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    schema = derive_schema_name(slug)
    conn = psycopg2.connect(_DSN)
    try:
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
    finally:
        conn.close()
    yield user_id, schema
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


def _rows(schema, sql, params=()):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


_KIND = "The blue-ringed octopus is a cephalopod."
_MEASURE = "An adult blue-ringed octopus measures about 5 centimeters across."
_CORRECTION = "Actually, the blue-ringed octopus measures 7 centimeters across, not 5."
_EXTRACTION = {
    "subject_name": "blue-ringed octopus",
    "old_rel_type": "measure",
    "old_value": "5 centimeters",
    "new_rel_type": "measure",
    "new_value": "7 centimeters",
    "dimension": "RELATIONAL",
    "confidence": 0.95,
}


def _drive_chain(client, user_id):
    """kind → measure (real harvest + /ingest) → /retract/correct (brain mocked to the
    live deploy's extraction shape). Returns nothing; rows are read back by callers."""
    for text in (_KIND, _MEASURE):
        h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        assert edges, f"harvest produced no edges for {text!r}"
        r = client.post("/ingest", json={"text": text, "user_id": user_id,
                                         "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
    r = client.post("/retract/correct", json={"text": _CORRECTION, "user_id": user_id,
                                              "attested": True})
    assert r.status_code == 200, r.text[:300]
    return r.json()


@pytest.fixture()
def rs13_client(monkeypatch):
    import src.api.main as M
    from starlette.testclient import TestClient
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True

    class _D:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def setex(self, k, t, v):
            self.d[k] = v
            return True

        def ping(self):
            return True

    class _Idem:
        redis_url = "dict://test"
        ttl = 3600
        client = _D()

    M._idempotency_mgr = _Idem()

    with TestClient(M.app) as client:
        from src.extraction import linguistics as _L
        t0 = time.time()
        while _L._nlp is None and time.time() - t0 < 120:
            time.sleep(0.5)
        assert _L._nlp is not None, "spaCy layer never loaded — the spine would no-op"
        monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                            lambda **k: dict(_EXTRACTION))
        yield client


def test_e2e_compound_anchor_renders_corrected_measure_alongside_ladder(
        rs13_tenant, rs13_client):
    """FROZEN BAR 1 (compound anchor): the question names the compound; the corrected
    row renders ASSERTED alongside the stated ladder rung.

    ⚠️ UPDATED BY #16 (compound-subject-narrowing, lane 19): the row USED to live on the
    head one subclass_of rung down — the #16 ingest fix closed that narrowing, so the
    verb-measure scalar now files ON the compound type node itself (the #11 ruling
    "measurement files ON the type node"). The render bar is unchanged; the walk now
    reaches it from the anchor's OWN rows (the #13 ladder-reachable BFS stays for
    genuinely cross-node rows — the head-anchor bar below pins that)."""
    user_id, schema = rs13_tenant
    out = _drive_chain(rs13_client, user_id)
    assert out["status"] == "corrected" and out["dimension"] == "SCALAR", out

    # rows read back: the corrected scalar is LIVE on the ladder
    attrs = _rows(schema, "SELECT entity_id, attribute, value_text, provenance, "
                          "superseded_at FROM entity_attributes")
    assert [("measure", "7 centimeters", "user_corrected", None)] == [
        (a, v, p, s) for (_e, a, v, p, s) in attrs], attrs
    compound = _rows(schema, "SELECT entity_id FROM entity_aliases "
                             "WHERE alias = 'blue-ringed octopus'")[0][0]
    holder = attrs[0][0]
    assert holder == compound  # #16: the compound type node holds its own measurement

    q = rs13_client.post("/query", json={
        "text": "How big does the blue-ringed octopus get?", "user_id": user_id}).json()
    got = {(f.get("rel_type"), f.get("object")) for f in (q.get("facts") or [])}
    # THE BAR: the corrected measure renders, asserted, alongside the ladder
    assert ("measure", "7 centimeters") in got, got
    measure_fact = [f for f in q["facts"] if f.get("rel_type") == "measure"][0]
    assert measure_fact.get("fact_provenance") == "user_stated"  # user_corrected canon
    assert measure_fact.get("fact_class") == "A"
    assert any(r == "subclass_of" for (r, _o) in got), got  # ladder unchanged
    # and it is the CORRECTED value — the superseded 5 must not answer
    assert not any(o == "about 5 centimeters" for (_r, o) in got), got


def test_e2e_head_anchor_renders_corrected_measure(rs13_tenant, rs13_client):
    """FROZEN BAR 1 (head anchor): the node holding the row is asked directly — this
    was the measured `facts: []` shape (#12 residual 1); the measure must render.
    #16 note: the row now lives on the compound SUBCLASS of the asked head — the
    genuinely cross-node shape — so this bar pins the #13 ladder-reachable BFS's
    SYMMETRIC reach (#16): the bare-parent question still renders the family measure."""
    user_id, schema = rs13_tenant
    out = _drive_chain(rs13_client, user_id)
    assert out["status"] == "corrected", out

    q = rs13_client.post("/query", json={
        "text": "How big is the octopus?", "user_id": user_id}).json()
    got = {(f.get("rel_type"), f.get("object")) for f in (q.get("facts") or [])}
    assert ("measure", "7 centimeters") in got, got
    measure_fact = [f for f in q["facts"] if f.get("rel_type") == "measure"][0]
    assert measure_fact.get("fact_provenance") == "user_stated"
    assert measure_fact.get("fact_class") == "A"


def test_e2e_noise_matrix_colour_invents_nothing(rs13_tenant, rs13_client):
    """FROZEN BAR 2: the measure-family questions bind the aspect; a colour question
    must NOT invent the measure as its answer (subject-agnostic: the grammar of the
    interrogative + the tenant's own cue class decide — no word zoo).

    "What size is X?" rides the INTERROGATIVE-ASPECT-NP frame, whose aspect noun binds
    through the tenant's ``measure_verb`` cue class — the growth rail. The seed floor is
    deliberately verbs-only (a "size" literal in code is the forbidden zoo), so the pin
    drives the rail the way tenant growth would: it inserts the measurement-noun sense
    into the TENANT'S OWN cue table (data, not code) and the frame then binds."""
    user_id, schema = rs13_tenant
    _drive_chain(rs13_client, user_id)

    def rels(text):
        q = rs13_client.post("/query", json={"text": text, "user_id": user_id}).json()
        return q, {f.get("rel_type") for f in (q.get("facts") or [])}

    # the degree frames admit the measure on the node that holds it
    for text in ("How big is the octopus?", "How long is the octopus?"):
        _q, rs = rels(text)
        assert "measure" in rs, (text, rs)

    # SEED STATE: "what size" declines loudly (unbound aspect noun — the class does not
    # carry it yet) and invents NOTHING
    import psycopg2
    q, rs = rels("What size is the octopus?")
    assert "measure" not in rs, (q.get("facts"), rs)
    # …and "what colour" invents nothing at seed state
    q, rs = rels("What colour is the octopus?")
    assert not any(f.get("rel_type") == "measure"
                   for f in (q.get("facts") or [])), (q.get("facts"), rs)

    # TENANT GROWTH (the miss→grow rail, simulated as data): the tenant's own cue table
    # grows the measurement-noun sense "size" — the frame then binds and the measure
    # surfaces, while "colour" STILL declines (only the grown word binds).
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "INSERT INTO linguistic_cues (category, cue, description, is_active) "
                "VALUES ('measure_verb', 'size', 'grown: measurement-noun sense', true) "
                "ON CONFLICT DO NOTHING")
        conn.commit()
    finally:
        conn.close()
    q, rs = rels("What size is the octopus?")
    assert "measure" in rs, (q.get("facts"), rs)
    q, rs = rels("What colour is the octopus?")
    assert not any(f.get("rel_type") == "measure"
                   for f in (q.get("facts") or [])), (q.get("facts"), rs)


def test_e2e_ladder_question_still_renders_the_ladder(rs13_tenant, rs13_client):
    """FROZEN BAR 3: the determiner/ladder behaviour is unchanged — a genuine
    classification ask still renders the stated subclass rung (the measure frame,
    absent here, binds nothing)."""
    user_id, _schema = rs13_tenant
    _drive_chain(rs13_client, user_id)
    q = rs13_client.post("/query", json={
        "text": "What is the blue-ringed octopus a subclass of?",
        "user_id": user_id}).json()
    got = q.get("facts") or []
    assert any(f.get("rel_type") == "subclass_of" and f.get("object") == "cephalopod"
               for f in got), got

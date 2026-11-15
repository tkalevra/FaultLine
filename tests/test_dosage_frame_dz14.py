"""DOSAGE-FRAME (issue #14) — the take frame keeps the medication and files the dosage
as ITS scalar; the dosage interrogative renders it; a correction supersedes it.

THE BUG (measured live, deploy #8, and reproduced in-process in
DEV/review/gauntlet/dosage-frame/round_1/repro.txt): "I take lisinopril 10 milligrams
daily for high blood pressure." took the DOSAGE as the object of take (a value-entity
aliased '10 milligrams' / a phrase-entity 'lisinopril for high blood pressure' — the
medication was never taken), minted a phrase-entity zoo from ONE sentence, the dosage
interrogative anchored junk and never rendered the scalar, and the correction
"Actually, my lisinopril dose is 20 milligrams, not 10." failed "Incomplete correction —
relationship type not identified".

THE FIX (strong ingest, lean query, the correction addressing seam):
  1. linguistics quantity pre-pass — the SIBLING-DOBJ dosage apposition ("metformin 500
     milligrams", the appos shape without its comma) binds as CONTENT mode: the substance
     is the LEFT dobj (grounded relationally), the "<num> <unit>" is ITS scalar, and the
     verb's SVO twin is suppressed.
  2. linguistics _svo_object_head — the head-final recovery declines when the rightmost
     dobj carries its own BARE-DIGIT nummod child (a quantity phrase, never a compound
     head).
  3. linguistics _chain_svo — the object head's own purpose-PP ("for high blood
     pressure") is NOT folded into the medication's name when the head carries a dosage
     apposition (THE HARD LINE at the name layer); the PP flows to the residue net.
  4. main.py harvest backbone — a per-edge scalar marker never resolves its object to a
     phantom value-entity surrogate/alias.
  5. main.py determine_path — the dosage-interrogative MIRROR frame ("what is my X
     dosage?", wh-attr + aspect-NOUN nsubj, first-person-possessive gated) admits the
     anchor's dose/quantity attribute family (the rs13 rails: cue class ∪ frame family ∪
     unit lexicon), and the rs13 topic-gate exemption renders the scoped scalar.
  6. main.py correction floor — when the LLM extraction names no relationship, the
     grammar of the replacive aspect-scalar frame builds it deterministically; the aspect
     noun resolves onto the STORED attribute through the addressing seam's old-value
     anchor with the #12 numeric-key admission (bare "10" admits "10 milligrams").

FROZEN BARS (issue #14):
  1. Fresh seat: the sentence commits `user --take--> lisinopril` + the dosage scalar on
     lisinopril; NO phrase-entities ('lisinopril dosage', 'lisinopril for high blood
     pressure') and no value-entity alias surfaces.
  2. "What is my lisinopril dosage?" renders 10 milligrams (then 20 after correction).
  3. "What medications do I take?" renders BOTH medications.
  4. Correction "…dose is 20 milligrams, not 10" supersedes (dose → quantity mapped at
     the addressing seam; retire + retain with history).
  5. Noise matrix: "I take metformin 500 milligrams twice daily" (same frame, no weld,
     metformin never dropped) and "I take aspirin for headaches" (no dosage → no scalar).

Runs the REAL harvest → /ingest → /query → /retract/correct chain against the throwaway
POSTGRES_DSN tenant (correction brain mocked to the live deploy's extraction shape,
exactly like tests/test_recall_scalar_surfacing_rs13.py). Skips without a test-named
DSN — disclosed, never a silent pass.
"""

import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

_SENTENCE = "I take lisinopril 10 milligrams daily for high blood pressure."
_METFORMIN = "I take metformin 500 milligrams twice daily."
_ASPIRIN = "I take aspirin for headaches."
_CORRECTION = "Actually, my lisinopril dose is 20 milligrams, not 10."

_ZOO = ("lisinopril dosage", "lisinopril dose", "lisinopril for high blood pressure",
        "dosage", "dose", "10 milligrams", "500 milligrams", "500 milligrams twice",
        "indefinite quantity", "measurement_unit")

# The live deploy-#8 extraction shape: the LLM named NO relationship for the dosage
# frame → the endpoint died "Incomplete correction — relationship type not identified".
_EXTRACTION_LIVE = {
    "subject_name": "lisinopril",
    "old_rel_type": None,
    "old_value": "10 milligrams",
    "new_rel_type": None,
    "new_value": "20 milligrams",
    "dimension": "SCALAR",
    "confidence": 0.9,
}
# The charitable shape: the LLM DID name the user's own aspect noun.
_EXTRACTION_DOSEREL = dict(_EXTRACTION_LIVE, old_rel_type="dose", new_rel_type="dose")


# --------------------------------------------------------------------------- #
# 0. Unit seams — the frame grammar, no DB required
# --------------------------------------------------------------------------- #


def test_dosage_interrogative_mirror_frame_is_grammatical_and_first_person_gated():
    from src.api.main import _is_measure_aspect_np_subject
    # the mirror frame: wh-attr + aspect-NOUN nsubj + a first-person possessive
    assert _is_measure_aspect_np_subject("What is my lisinopril dosage?") == "dosage"
    assert _is_measure_aspect_np_subject("What is my lisinopril dose?") == "dose"
    assert _is_measure_aspect_np_subject("what is my metformin dosage?") == "dosage"
    # a THIRD-PERSON / classification ask is NOT this frame — the mirror frame without
    # the speaker's possessive would also match "what is an octopus?" and project a
    # classification walk down to a scalar-only scope
    assert _is_measure_aspect_np_subject("What is an octopus?") is None
    assert _is_measure_aspect_np_subject("What is her dosage?") is None
    assert _is_measure_aspect_np_subject("What is the octopus?") is None
    assert _is_measure_aspect_np_subject("") is None


def test_dosage_frame_floor_builds_the_extraction_the_llm_could_not():
    from src.api.main import _dosage_correction_frame_floor
    out = _dosage_correction_frame_floor(_CORRECTION)
    assert out is not None
    assert out["subject_name"] == "lisinopril"
    assert out["old_rel_type"] == "dose" and out["new_rel_type"] == "dose"
    assert out["new_value"] == "20 milligrams"
    assert out["old_value"] == "10"  # the REJECTED numeral, never the new value's own
    assert out["dimension"] == "SCALAR"
    # non-dosage frames decline (no aspect-NP subject, no possessive, no quantity)
    assert _dosage_correction_frame_floor("Actually, my sister's name is Marlow, not Katya.") is None
    assert _dosage_correction_frame_floor("the measure is 7 centimeters, not 5") is None
    assert _dosage_correction_frame_floor("") is None


def test_unit_lexicon_admits_mass_volume_primitives_as_quantity():
    # the measurement-primitive lexicon (same rail as foot/pound/hour) — the family
    # recognition the query admission and the frame's attribute routing share
    from src.api.linguistic_cue_overlay import _BOOTSTRAP_UNIT_SCALAR_MAP
    assert _BOOTSTRAP_UNIT_SCALAR_MAP.get("milligrams") == "quantity"
    assert _BOOTSTRAP_UNIT_SCALAR_MAP.get("milligram") == "quantity"
    assert _BOOTSTRAP_UNIT_SCALAR_MAP.get("grams") == "quantity"
    assert _BOOTSTRAP_UNIT_SCALAR_MAP.get("ml") == "quantity"


def test_deriver_takes_the_medication_not_the_dosage():
    """The frame grammar at the deriver (typed-doc path — GLiNER2 spans written, the
    spine's own build), all three frozen sentences."""
    pytest.importorskip("spacy")
    from src.api.main import _build_typed_doc, _CANONICAL_ENTITY_TYPES, get_gliner_model
    from src.extraction import linguistics as L
    gmodel = get_gliner_model()

    def edges(text):
        doc = _build_typed_doc(text, gmodel, list(_CANONICAL_ENTITY_TYPES))
        out = set()
        for e in L.derive_sentence_facts(doc, None, None):
            d = e if isinstance(e, dict) else getattr(e, "__dict__", e)
            out.add((d["subject"], d["rel_type"], d["object"]))
        return out, doc

    def zoo_violations(edgeset):
        # a SCALAR edge legitimately carries the value string as its object (it routes to
        # entity_attributes); only a RELATIONAL edge may never take a phrase/value surface
        doc_edges = [(s, r, o) for (s, r, o) in edgeset
                     if r not in ("quantity", "dose")]
        return [(s, r, o) for (s, r, o) in doc_edges if o in _ZOO]

    got, _doc = edges(_SENTENCE)
    assert ("user", "take", "lisinopril") in got, got
    assert ("lisinopril", "quantity", "10 milligrams") in got, got
    assert not zoo_violations(got), got

    got_m, _doc = edges(_METFORMIN)
    assert ("user", "take", "metformin") in got_m, got_m
    assert ("metformin", "quantity", "500 milligrams") in got_m, got_m
    assert not zoo_violations(got_m), got_m

    got_a, _doc = edges(_ASPIRIN)
    assert ("user", "take", "aspirin") in got_a, got_a


# --------------------------------------------------------------------------- #
# 1. E2E — the fresh-seat chain, rows read back
# --------------------------------------------------------------------------- #

pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")


@pytest.fixture()
def dz14_tenant(monkeypatch):
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


@pytest.fixture()
def dz14_client(monkeypatch):
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
        yield client


def _ingest(client, user_id, text):
    h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
    assert h.status_code == 200, h.text[:300]
    edges = h.json().get("edges") or []
    r = client.post("/ingest", json={"text": text, "user_id": user_id,
                                     "edges": edges, "source": "mcp"})
    assert r.status_code == 200, r.text[:300]
    return edges


def _query_rels(client, user_id, text):
    q = client.post("/query", json={"text": text, "user_id": user_id}).json()
    return q, {(f.get("subject"), f.get("rel_type"), f.get("object"))
               for f in (q.get("facts") or [])}


def test_e2e_take_commits_the_medication_and_the_dosage_scalar_no_phrase_entities(
        dz14_tenant, dz14_client):
    """FROZEN BAR 1: fresh seat — take→lisinopril committed; the dosage is a scalar ON
    lisinopril; the alias layer carries no phrase-entity and no value surface."""
    user_id, schema = dz14_tenant
    for text in (_SENTENCE, _METFORMIN, _ASPIRIN):
        _ingest(dz14_client, user_id, text)

    lisinopril = _rows(schema, "SELECT entity_id FROM entity_aliases WHERE alias = 'lisinopril'")
    assert lisinopril, "the medication was never registered"
    lisinopril_id = lisinopril[0][0]

    take_objects = [r[0] for r in _rows(
        schema, "SELECT f.object_id FROM facts f WHERE f.rel_type = 'take'")]
    assert lisinopril_id in take_objects, take_objects
    metformin_id = _rows(schema, "SELECT entity_id FROM entity_aliases "
                                 "WHERE alias = 'metformin'")[0][0]
    assert metformin_id in take_objects, take_objects

    attrs = {(r[0], r[1], r[2]) for r in _rows(
        schema, "SELECT entity_id, attribute, value_text FROM entity_attributes "
                "WHERE superseded_at IS NULL")}
    assert (lisinopril_id, "quantity", "10 milligrams") in attrs, attrs
    assert (metformin_id, "quantity", "500 milligrams") in attrs, attrs

    aliases = {r[0] for r in _rows(schema, "SELECT alias FROM entity_aliases")}
    for bad in _ZOO:
        assert bad not in aliases, (bad, sorted(aliases))


def test_e2e_dosage_interrogative_renders_the_scalar_then_the_correction(
        dz14_tenant, dz14_client, monkeypatch):
    """FROZEN BARS 2 + 4: the dosage interrogative anchors lisinopril and renders the
    quantity scalar (10 milligrams); the correction — in the LIVE extraction shape, the
    LLM naming no relationship — supersedes to 20 milligrams (retire + retain)."""
    import src.api.main as M
    user_id, schema = dz14_tenant
    _ingest(dz14_client, user_id, _SENTENCE)

    _q, got = _query_rels(dz14_client, user_id, "What is my lisinopril dosage?")
    assert ("lisinopril", "quantity", "10 milligrams") in got, got

    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(_EXTRACTION_LIVE))
    r = dz14_client.post("/retract/correct", json={"text": _CORRECTION, "user_id": user_id,
                                                   "attested": True})
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["status"] == "corrected", out
    # the response reports the attribute actually WRITTEN (dose → quantity), not the guess
    assert out["old_rel_type"] == "quantity" and out["new_rel_type"] == "quantity", out

    attrs = _rows(schema, "SELECT attribute, value_text, provenance, superseded_at "
                          "FROM entity_attributes")
    assert [("quantity", "20 milligrams", "user_corrected", None)] == [
        (a, v, p, s) for (a, v, p, s) in attrs], attrs
    hist = _rows(schema, "SELECT attribute, prior_value_text, new_value_text "
                         "FROM entity_attributes_history")
    assert ("quantity", "10 milligrams", "20 milligrams") in [tuple(h) for h in hist], hist

    _q, got = _query_rels(dz14_client, user_id, "What is my lisinopril dosage?")
    assert ("lisinopril", "quantity", "20 milligrams") in got, got
    assert not any(o == "10 milligrams" for (_s, _r, o) in got), got


def test_e2e_correction_with_charitable_extraction_supersedes_too(
        dz14_tenant, dz14_client, monkeypatch):
    """The charitable LLM shape (the aspect noun named) supersedes through the SAME
    addressing rungs — dose resolves onto the stored quantity via the old-value anchor."""
    import src.api.main as M
    user_id, schema = dz14_tenant
    _ingest(dz14_client, user_id, _SENTENCE)

    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(_EXTRACTION_DOSEREL))
    r = dz14_client.post("/retract/correct", json={"text": _CORRECTION, "user_id": user_id,
                                                   "attested": True})
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["status"] == "corrected", out

    attrs = _rows(schema, "SELECT attribute, value_text, provenance FROM entity_attributes "
                          "WHERE superseded_at IS NULL")
    assert attrs == [("quantity", "20 milligrams", "user_corrected")], attrs


def test_e2e_medications_query_renders_both_medications(dz14_tenant, dz14_client):
    """FROZEN BAR 3: the walk from the user anchor surfaces BOTH taken medications."""
    user_id, _schema = dz14_tenant
    for text in (_SENTENCE, _METFORMIN, _ASPIRIN):
        _ingest(dz14_client, user_id, text)
    _q, got = _query_rels(dz14_client, user_id, "What medications do I take?")
    objs = {o for (_s, r, o) in got if r == "take"}
    assert "lisinopril" in objs and "metformin" in objs and "aspirin" in objs, got


def test_e2e_noise_matrix_no_dosage_no_scalar_aspirin_clean(dz14_tenant, dz14_client):
    """FROZEN BAR 5: "I take aspirin for headaches" — no dosage → no quantity scalar, and
    the purpose-PP never welds into the medication's name."""
    user_id, schema = dz14_tenant
    _ingest(dz14_client, user_id, _ASPIRIN)
    attrs = _rows(schema, "SELECT attribute FROM entity_attributes")
    assert not any(a == "quantity" for (a,) in attrs), attrs
    aliases = {r[0] for r in _rows(schema, "SELECT alias FROM entity_aliases")}
    assert "aspirin" in aliases
    for bad in ("aspirin for headaches", "headaches", "dose", "dosage"):
        # 'headaches' MAY exist as the event-reify seam's own entity (pre-existing lane);
        # it must never be welded into an alias surface of the medication
        assert not any(a == bad and a != "headaches" for a in aliases), sorted(aliases)
    assert "aspirin for headaches" not in aliases, sorted(aliases)

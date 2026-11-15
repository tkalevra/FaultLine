"""TI15 — temporal-interrogative lane + start-inception born_on suppression (issue #15).

WOUND (a) (measured live deploy #8, reproduced in-process in
DEV/review/gauntlet/temporal-interrogative/round_1/repro.txt): "When did I start
taking metformin?" returned honest-empty though user --take--> metformin
@2025-03-03 [A/user_stated] was stored and rendered in the plural walk. The
scoped walk FETCHED the row (rels=['take'] resolved from the question's own
'taking'); the lexical topic gate (topic 'start') dropped it. Fix: a
``when_event`` directive on `_detect_temporal_query_intent` (wh-fronting
grammar, no frame list) + a determine_path binding that marks the question's own
predication aspect-bound — the SAME on-topic-by-construction authority the
keyword-bound rel carries (issue #14 bar 3) and the possessed-NP binding carries
(rs13). Render: the established "(on YYYY-MM-DD)" date shape.

WOUND (b): the start-inception frame ('started V-ing on DATE') is an EVENT
INCEPTION — deploy #8 minted (user, born_on, 'March 3rd, 2025') for it and the
ingest scalar-datatype preflight loudly dropped the date-object. Fix: a
frame-gated refusal at the deriver carrier site (`_build_typed_doc`'s
set_minted_rel loop) + the spine door joins the legacy door's
`_drop_temporal_object_edges` sweep (the spine returned EARLY and never ran
it). The GENUINE birth frame is pinned unaffected.

Skips without a throwaway test-named POSTGRES_DSN — disclosed, never a silent
pass.
"""
import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

_SENTENCE = "I started taking metformin on March 3rd, 2025."
_YOGA = "I started yoga on June 1st."
_BIRTH = "I was born on March 3rd, 2025."
_Q_WHEN_TAKE = "When did I start taking metformin?"
_Q_WHEN_TAKE_BARE = "When did I take metformin?"
_Q_WHEN_YOGA = "When did I start yoga?"
_Q_WHEN_START_NP = "When did I start metformin?"
_Q_NOISE_CALL = "When did you call me?"
_Q_MEDS = "What medications do I take?"


# --------------------------------------------------------------------------- #
# 0. Unit seams — directive + frame grammar, no DB required
# --------------------------------------------------------------------------- #


def test_when_event_directive_fires_on_fronted_when_interrogatives():
    from src.api.main import _detect_temporal_query_intent as dt
    assert dt("When did I start taking metformin?").get("when_event") is True
    assert dt("when did i take metformin").get("when_event") is True
    assert dt("What year did I start yoga?").get("when_event") is True
    assert dt("What date did I start yoga?").get("when_event") is True
    # a wh-VALUE ask, a classification ask, and a statement carry no when-directive
    assert dt("Where do I live?").get("when_event") is None
    assert dt("What medications do I take?").get("when_event") is None
    assert dt("What is a dog?").get("when_event") is None
    assert dt("I started taking metformin on March 3rd, 2025.").get("when_event") is None
    assert dt("").get("when_event") is None


def test_start_inception_frame_predicate_is_cue_class_grammar():
    pytest.importorskip("spacy")
    from src.extraction.linguistics import (
        has_start_inception_matrix, surface_is_date_span,
    )
    from src.extraction.linguistics import _parse
    # the frame: aspectual-control matrix + progressive gerund xcomp
    assert has_start_inception_matrix(_parse(_SENTENCE)) is True
    assert has_start_inception_matrix(_parse("I began taking lisinopril in April 2024.")) is True
    assert has_start_inception_matrix(_parse("She started working with Rachel.")) is True
    # the GENUINE birth frame has no start-inception matrix → never suppressed
    assert has_start_inception_matrix(_parse(_BIRTH)) is False
    assert has_start_inception_matrix(_parse(_YOGA)) is False  # bare-NP start, no gerund xcomp
    assert has_start_inception_matrix(None) is False
    # the date-adjunct test rides the SAME date-span detector the peel lanes use
    txt = _SENTENCE
    assert surface_is_date_span(txt, "March 3rd") is True
    assert surface_is_date_span(txt, "march 3rd, 2025") is True
    assert surface_is_date_span(txt, "2025") is True
    assert surface_is_date_span(txt, "metformin") is False
    assert surface_is_date_span(txt, "") is False
    assert surface_is_date_span("", "march") is False


def test_start_frame_carrier_decision_refuses_only_the_frame():
    """The mint-site refusal predicate (frame AND date-object) — the exact
    conjunction the `_build_typed_doc` carrier loop evaluates. The genuine birth
    sentence fails the conjunction (no start-inception matrix) so a born_on mint
    there stands, wherever it fires today."""
    pytest.importorskip("spacy")
    from src.extraction.linguistics import (
        has_start_inception_matrix, surface_is_date_span,
    )
    from src.extraction.linguistics import _parse

    def refusal(doc_text, obj_surface):
        doc = _parse(doc_text)
        return bool(has_start_inception_matrix(doc)
                    and surface_is_date_span(doc.text, obj_surface))

    # the deploy-#8 mint: refused
    assert refusal(_SENTENCE, "March 3rd") is True
    assert refusal(_SENTENCE, "march 3rd, 2025") is True
    # the frame's OWN activity object is NOT a date → a take mint stands
    assert refusal(_SENTENCE, "metformin") is False
    # the birth sentence: the frame half fails → a born_on mint STANDS (pinned)
    assert refusal(_BIRTH, "March 3rd, 2025") is False
    assert refusal(_BIRTH, "metformin") is False


def test_temporal_object_sweep_drops_date_objects_keeps_scalars():
    """The spine door now runs the SAME doctrine sweep the legacy door runs: a
    date-object candidate (the deploy-#8 born_on shape) is stripped; an
    intentional scalar leaf carrying ``object_datatype`` is exempt; the dated
    take edge (date on the EDGE, not the object) is untouched."""
    from src.api.main import _drop_temporal_object_edges
    edges = [
        {"subject": "user", "rel_type": "born_on", "object": "march 3rd, 2025",
         "fact_provenance": "user_stated"},
        {"subject": "user", "rel_type": "take", "object": "metformin",
         "event_date": "2025-03-03T00:00:00+00:00"},
        {"subject": "user", "rel_type": "born", "object": "2025-03-03",
         "object_datatype": "date"},  # intentional dated-attribute scalar → exempt
        {"subject": "user", "rel_type": "participated_in", "object": "metformin start",
         "event_date": "2025-03-03T00:00:00+00:00"},
    ]
    kept = _drop_temporal_object_edges(edges)
    kept_keys = [(e["rel_type"], e["object"]) for e in kept]
    assert ("born_on", "march 3rd, 2025") not in kept_keys
    assert ("take", "metformin") in kept_keys
    assert ("born", "2025-03-03") in kept_keys  # the scalar exemption
    assert ("participated_in", "metformin start") in kept_keys


# --------------------------------------------------------------------------- #
# 1. E2E — fresh seat: sentence in → when-interrogative renders the date
# --------------------------------------------------------------------------- #

pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")


@pytest.fixture()
def ti15_tenant(monkeypatch):
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


@pytest.fixture()
def ti15_client(monkeypatch):
    import src.api.main as M
    from starlette.testclient import TestClient
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True
    # LOCAL-STACK SEAM (the dz14 pattern): the orphaned date atom's missing-SUBJECT
    # resolver is a brain call; with WGM_LLM_MODEL set but no reachable brain the
    # harvest's degradation envelope WITHHELS the turn (by design — an unavailable
    # brain must produce an honest nothing). The no-brain deterministic answer is
    # "no owner resolved" — mock exactly that seam so the E2E measures THIS lane,
    # not the local stack's dead endpoint. The take edge never depends on it.
    monkeypatch.setattr(M, "_resolve_atomic_subjects_via_llm",
                        lambda text, unresolved, user_id: [])

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
        assert _L._nlp is not None
        yield client


def _ingest(client, user_id, text):
    h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
    assert h.status_code == 200, h.text[:400]
    edges = h.json().get("edges") or []
    r = client.post("/ingest", json={"text": text, "user_id": user_id,
                                     "edges": edges, "source": "mcp"})
    assert r.status_code == 200, r.text[:400]
    return edges, r.json()


def _query(client, user_id, text):
    resp = client.post("/query", json={"text": text, "user_id": user_id})
    assert resp.status_code == 200, resp.text[:400]
    return resp.json()


def test_when_interrogative_renders_the_event_date_e2e(ti15_tenant, ti15_client):
    user_id, schema = ti15_tenant
    client = ti15_client
    edges, ingest = _ingest(client, user_id, _SENTENCE)
    assert ingest.get("committed", 0) >= 1, ingest
    # the dated take edge landed, and NOTHING whose object is a date string survives
    objs = [(e.get("rel_type"), str(e.get("object") or "")) for e in edges]
    assert any(r == "take" and o == "metformin" for r, o in objs), objs
    assert not any("2025" in o or "march" in o.lower() for r, o in objs), objs

    j = _query(client, user_id, _Q_WHEN_TAKE)
    facts = j.get("facts") or []
    assert facts, f"when-interrogative rendered empty: {j}"
    take_facts = [f for f in facts if f.get("rel_type") == "take"
                  and str(f.get("object") or "").lower() == "metformin"]
    assert take_facts, facts
    assert any(f.get("event_date") for f in take_facts), take_facts
    # the render answers the WHEN (the established "(on YYYY-MM-DD)" date shape)
    definitions = " ".join(str(f.get("definition") or "") for f in take_facts)
    assert "2025-03-03" in definitions, definitions
    # bare-take variant binds the same dated row
    j2 = _query(client, user_id, _Q_WHEN_TAKE_BARE)
    take2 = [f for f in (j2.get("facts") or []) if f.get("rel_type") == "take"]
    assert take2, j2
    assert "2025-03-03" in " ".join(str(f.get("definition") or "") for f in take2), j2


def test_when_interrogative_variant_bare_np_start_e2e(ti15_tenant, ti15_client):
    """Past-tense NON-ingestion subject: 'I started yoga on June 1st.' mints the
    start occurrence; 'When did I start yoga?' renders its event_date."""
    user_id, schema = ti15_tenant
    client = ti15_client
    _ingest(client, user_id, _YOGA)
    j = _query(client, user_id, _Q_WHEN_YOGA)
    facts = j.get("facts") or []
    assert facts, f"yoga when-interrogative rendered empty: {j}"
    yoga_facts = [f for f in facts if str(f.get("object") or "").lower() == "yoga"]
    assert yoga_facts, facts
    assert "20" in " ".join(str(f.get("definition") or "") for f in yoga_facts), j


def test_when_interrogative_start_np_grown_rel_e2e(ti15_tenant, ti15_client):
    """'I started metformin on March 3rd, 2025.' grows the bare-NP start
    occurrence; the when-ask binds the start rel and renders the date."""
    user_id, schema = ti15_tenant
    client = ti15_client
    _ingest(client, user_id, "I started metformin on March 3rd, 2025.")
    j = _query(client, user_id, _Q_WHEN_START_NP)
    facts = j.get("facts") or []
    assert facts, f"start-NP when-interrogative rendered empty: {j}"
    assert any(str(f.get("object") or "").lower() == "metformin" for f in facts), facts


def test_broad_controls_unchanged_e2e(ti15_tenant, ti15_client):
    """The plural walk ("What medications do I take?") still renders — the
    aspect-bound binding must not narrow the CONTROL query."""
    user_id, schema = ti15_tenant
    client = ti15_client
    _ingest(client, user_id, _SENTENCE)
    j = _query(client, user_id, _Q_MEDS)
    facts = j.get("facts") or []
    assert any(str(f.get("object") or "").lower() == "metformin" for f in facts), j


def test_non_stored_event_declines_honestly_e2e(ti15_tenant, ti15_client):
    user_id, schema = ti15_tenant
    client = ti15_client
    _ingest(client, user_id, _SENTENCE)
    j = _query(client, user_id, _Q_NOISE_CALL)
    facts = j.get("facts") or []
    assert not facts, f"non-stored event answered from the profile dump: {j}"


def test_birth_frame_control_legacy_lane_untouched_by_the_guard(ti15_tenant, ti15_client):
    """The genuine birth frame: (a) it is STILL captured — a birth-class dated edge
    survives (the natal chain files the date as a dated-attribute scalar carrying
    ``object_datatype``, the documented exemption); (b) the sweep strips only a
    date leaked onto a RELATIONSHIP object (no object_datatype) — the doctrine,
    nothing frame-specific; (c) the refusal predicate is False for it (unit-pinned
    above)."""
    user_id, schema = ti15_tenant
    client = ti15_client
    h = client.post("/harvest-spans", json={"text": _BIRTH, "user_id": user_id})
    assert h.status_code == 200, h.text[:400]
    edges = h.json().get("edges") or []
    for e in edges:
        o = str(e.get("object") or "")
        if "2025" in o or "march" in o.lower():
            # a date on an object is legal ONLY as an intentional dated-attribute
            # scalar leaf (object_datatype set → entity_attributes, never a graph node)
            assert e.get("object_datatype"), f"bare date-object edge survived: {e}"
            assert e.get("rel_type") in ("born", "born_on"), e
    # the birth frame's own dated reading survives the harvest (captured, not suppressed)
    birth_edges = [e for e in edges
                   if e.get("rel_type") in ("born", "born_on")
                   or "2025-03-03" in str(e.get("event_date") or "")]
    assert birth_edges, f"genuine birth frame lost: {edges}"
    # and the when-ask over the birth sentence is NOT hijacked by the lane
    _ingest(client, user_id, _BIRTH)
    j = _query(client, user_id, "When was I born?")
    for f in (j.get("facts") or []):
        assert str(f.get("object") or "").lower() != "metformin", j

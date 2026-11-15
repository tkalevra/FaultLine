"""WQ17 — when-ask object-pinning + measure-axis grown-rung voice (issue #17).

WOUND (a) (the #15 disclosed residual, measured in
DEV/review/gauntlet/when-ask-precision/round_1/repro.txt): the when-interrogative
exemption is REL-granular — on a mixed tenant "When did I start taking metformin?"
rendered the SIBLING dated rows of the bound rels too (the lisinopril take, the
yoga start): accurate rows, wrong breadth. Fix: when the question names a SPECIFIC
entity, `determine_path` pins it (`path.when_pinned_entity_ids`, the question's own
concept tokens grounded against THIS tenant's alias registry) and the render drops
dated rows (and exemption-riding siblings) that belong to no asked entity.
Entity-less shapes ("When did I take anything?", "When did you call me?") resolve no
pin and keep the #15 behaviour byte-for-byte.

WOUND (b) (the #13 disclosed residual): on the measure question the ASKED CHAIN's
engine-grown classification rung ("blue-ringed Octopus is a subclass of Octopus")
faced the rung-silencing gate and was dropped while the user-stated rung rendered —
asymmetric voice for real memory. Fix: when the measure-interrogative admission
actually resolved the axis from the anchor's own rows, the asked chain's grown
classification rungs keep their voice on THIS question — banded HELD by their own
provenance, the same contract the ladder question already carries. The growth gate
(`_is_grown_ontology_row`) and everything the walk ADMITS are untouched.

Skips without a throwaway test-named POSTGRES_DSN — disclosed, never a silent pass.
"""
import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")

_MEDS = [
    "I started taking metformin on March 3rd, 2025.",
    "I started taking lisinopril on April 19th, 2024.",
    "I started yoga on June 1st.",
]
_Q_WHEN_METFORMIN = "When did I start taking metformin?"
_Q_WHEN_YOGA = "When did I start yoga?"
_Q_NOISE_CALL = "When did you call me?"

_KIND = "The blue-ringed octopus is a cephalopod."
_MEASURE = "An adult blue-ringed octopus measures about 5 centimeters across."
_CORRECTION = "Actually, the blue-ringed octopus measures 7 centimeters across, not 5."
_EXTRACTION = {
    "subject_name": "blue-ringed octopus",
    "old_rel_type": "measure",
    "old_value": "5 centimeters",
    "new_rel_type": "measure",
    "new_value": "7 centimeters",
    "dimension": "SCALAR",
    "confidence": 0.95,
}
_Q_MEASURE = "How big does the blue-ringed octopus get?"


# --------------------------------------------------------------------------- #
# 0. Unit seams — pin grammar + defaults + the unchanged growth gate
# --------------------------------------------------------------------------- #


def test_when_pin_candidate_forms_is_grammar_only():
    """Concept tokens only, wh-phrase scaffolding excluded: the interrogative's own
    determiner-governed noun ('what DATE') is never a pin candidate; the asked entity
    ('yoga') is. No word lists — spaCy POS + head-chain only."""
    pytest.importorskip("spacy")
    from src.api.main import _when_pin_candidate_forms
    forms = _when_pin_candidate_forms("When did I start yoga?")
    assert "yoga" in forms
    assert "date" not in _when_pin_candidate_forms("What date did I start yoga?")
    assert "yoga" in _when_pin_candidate_forms("What date did I start yoga?")
    assert "year" not in _when_pin_candidate_forms("What year did I start yoga?")
    # an entity-less shape names nothing ("anything" is a PRON; "call" a VERB)
    assert _when_pin_candidate_forms("When did I take anything?") == []
    assert _when_pin_candidate_forms("When did you call me?") == []
    assert _when_pin_candidate_forms("") == []


def test_when_pin_never_pins_speaker_or_places_or_a_huge_breadth(monkeypatch):
    """The pin excludes the speaker (a subject=user row belongs to every ask), L4
    places (a TYPE node is the shelf, never the memory a WHEN is about), and anything
    beyond the breadth bound (a pin that wide is no pin — the #15 breadth stands)."""
    from src.api import main as M

    _ALIASES = [("eeEEE111-1111-1111-1111-111111111111", "metformin"),
                ("aaAAA111-1111-1111-1111-111111111111", "meditation"),
                ("bbBBB222-2222-2222-2222-222222222222", "pottery"),
                ("ccCCC333-3333-3333-3333-333333333333", "chess"),
                ("ddDDD444-4444-4444-4444-444444444444", "choir"),
                ("ffFFF555-5555-5555-5555-555555555555", "drama"),
                ("8d1ebf0f-7777-7777-7777-777777777777", "sam")]

    class _Cur:
        def execute(self, sql, params=()):
            self.rows = []
            if "entity_aliases" in sql:
                if "= ANY" in sql:
                    want = set(params[0])
                    self.rows = [(e,) for e, a in _ALIASES if a in want]
                else:
                    import re as _re
                    # the Postgres word-boundary markers are not Python syntax
                    pat = params[0].replace(r"\m(", "(").replace(r")\M", ")")
                    self.rows = [(e,) for e, a in _ALIASES
                                 if _re.search(pat, a, _re.IGNORECASE)]
            elif "rel_types" in sql:
                self.rows = []  # nothing is an L4 place unless the test says so

        def fetchall(self):
            return self.rows

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _DB:
        def cursor(self):
            return _Cur()

    _USER = "8d1ebf0f-7777-7777-7777-777777777777"
    monkeypatch.setattr(M, "_entity_is_l4_place", lambda db, eid: False)
    ids = M._when_asked_entity_ids(
        "When did I start taking metformin?", _DB(), exclude_ids={_USER})
    assert ids == ["eeEEE111-1111-1111-1111-111111111111"]

    # the SPEAKER is never a pin even when the alias registry holds them (the caller
    # passes the user id — the real determine_path call shape)
    ids2 = M._when_asked_entity_ids(
        "When did I start taking metformin, sam?", _DB(), exclude_ids={_USER})
    assert _USER not in ids2

    # a place is never a pin: "metformin" stored as an L4 type node → no pin → #15 breadth
    monkeypatch.setattr(M, "_entity_is_l4_place", lambda db, eid: True)
    assert M._when_asked_entity_ids(
        "When did I start taking metformin?", _DB(), exclude_ids={_USER}) == []

    # breadth bound: more hits than the bound → no pin (fail safe to the #15 breadth)
    monkeypatch.setattr(M, "_entity_is_l4_place", lambda db, eid: False)
    assert M._when_asked_entity_ids(
        "When did I start meditation, pottery, chess, choir, drama?",
        _DB(), exclude_ids={_USER}) == []

    # a head noun inside a stored multi-word alias still grounds (word-boundary lane)
    _ALIASES.append(("12341234-1234-1234-1234-123412341234", "stunning crystal chandelier"))
    assert M._when_asked_entity_ids(
        "When did I buy the chandelier?", _DB(), exclude_ids={_USER}) == \
        ["12341234-1234-1234-1234-123412341234"]

    # fail-safe: no db → no pin
    assert M._when_asked_entity_ids("When did I start yoga?", None) == []


def test_query_path_defaults_and_growth_gate_unchanged():
    """The growth gate (membership) is untouched by the voice fix: the sanctioned
    predicate still silences a grown rung and never silences the user's own; the new
    QueryPath fields default to the byte-for-byte-legacy values."""
    from src.api.main import _is_grown_ontology_row
    from src.api.models import QueryPath
    p = QueryPath()
    assert p.when_pinned_entity_ids == []
    assert p.measure_axis_admission is False
    # the gate's own decisions (what the walk ADMITS) — unchanged:
    assert _is_grown_ontology_row("subclass_of", None, "llm_inferred") is True
    assert _is_grown_ontology_row("subclass_of", "user_stated", None) is False
    assert _is_grown_ontology_row("subclass_of", None, "user_stated") is False
    assert _is_grown_ontology_row("take", "llm_learn", None) is True


# --------------------------------------------------------------------------- #
# 1. E2E — mixed tenant + measure tenant on the throwaway db
# --------------------------------------------------------------------------- #

pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")


@pytest.fixture()
def wq17_tenant(monkeypatch):
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
def wq17_client(monkeypatch):
    import src.api.main as M
    from starlette.testclient import TestClient
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True
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


def test_wq1_mixed_tenant_when_ask_renders_only_the_asked_entity(
        wq17_tenant, wq17_client):
    """THE BAR (WQ1): on the mixed tenant the metformin when-ask renders ONLY the
    metformin dated row — the lisinopril take and the yoga start stay out — and the
    asked row always renders WITH its date. The yoga when-ask renders only yoga's
    rows. Nothing false, and now nothing over-broad."""
    user_id, _schema = wq17_tenant
    client = wq17_client
    for text in _MEDS:
        _ingest(client, user_id, text)

    j = _query(client, user_id, _Q_WHEN_METFORMIN)
    facts = j.get("facts") or []
    assert facts, f"metformin when-ask rendered empty: {j}"
    # ONLY the asked entity: no lisinopril, no yoga
    leaked = [(f.get("rel_type"), f.get("object")) for f in facts
              if str(f.get("object") or "").lower() != "metformin"]
    assert not leaked, f"sibling rows leaked into the metformin when-ask: {leaked}"
    take = [f for f in facts if f.get("rel_type") == "take"]
    assert take, facts
    assert any(f.get("event_date") for f in take), take
    # the asked row renders WITH its date (the established "(on YYYY-MM-DD)" shape)
    defs = " ".join(str(f.get("definition") or "") for f in take)
    assert "2025-03-03" in defs, defs

    # the pin is per-ask: the yoga when-ask renders YOGA's rows only
    j2 = _query(client, user_id, _Q_WHEN_YOGA)
    facts2 = j2.get("facts") or []
    assert facts2, f"yoga when-ask rendered empty: {j2}"
    leaked2 = [(f.get("rel_type"), f.get("object")) for f in facts2
               if str(f.get("object") or "").lower() != "yoga"]
    assert not leaked2, f"sibling rows leaked into the yoga when-ask: {leaked2}"
    assert "on 20" in " ".join(str(f.get("definition") or "") for f in facts2), j2


def test_wq3_entityless_when_ask_keeps_the_ti15_decline(wq17_tenant, wq17_client):
    """WQ3 noise: an entity-less when-ask that binds nothing keeps the #15 decline —
    the honest empty stands (no pin resolved, and no profile dump either)."""
    user_id, _schema = wq17_tenant
    client = wq17_client
    for text in _MEDS:
        _ingest(client, user_id, text)
    j = _query(client, user_id, _Q_NOISE_CALL)
    assert not (j.get("facts") or []), \
        f"non-stored event answered from the profile dump: {j}"


def test_wq2_measure_question_renders_scalar_stated_and_grown_held(
        wq17_tenant, wq17_client, monkeypatch):
    """THE BAR (WQ2): the measure question renders the scalar (ASSERTED), the
    user-stated rung (ASSERTED) AND the grown rung (HELD — its own provenance), the
    same voice contract the ladder question already carries. Membership is the
    growth gate's decision and is untouched: the grown rung was already FETCHED
    (the walk admits it); only its silencing on THIS question is lifted."""
    import src.api.main as M
    user_id, _schema = wq17_tenant
    client = wq17_client
    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(_EXTRACTION))
    _ingest(client, user_id, _KIND)
    _ingest(client, user_id, _MEASURE)
    r = client.post("/retract/correct", json={"text": _CORRECTION, "user_id": user_id,
                                              "attested": True})
    assert r.status_code == 200, r.text[:300]

    j = _query(client, user_id, _Q_MEASURE)
    facts = j.get("facts") or []
    from src.mcp.server import _fact_is_asserted

    def band(f):
        prov = f.get("fact_provenance") or f.get("provenance")
        return _fact_is_asserted(prov, f.get("confidence"), f.get("fact_class"))

    # 1. the scalar — ASSERTED
    scalars = [f for f in facts if f.get("rel_type") == "measure"]
    assert scalars, f"measure question dropped the scalar: {j}"
    assert all(band(f) for f in scalars), \
        f"scalar did not render asserted: {[(f.get('fact_provenance'), band(f)) for f in scalars]}"
    assert any("7 centimeters" in str(f.get("object") or "") for f in scalars), scalars
    # 2. the user-stated rung — ASSERTED
    stated = [f for f in facts if f.get("rel_type") == "subclass_of"
              and (f.get("fact_provenance") in ("user_stated", None)
                   and f.get("fact_class") == "A"
                   and band(f))]
    assert stated, f"the stated rung no longer renders: {facts}"
    # 3. the GROWN rung — renders, HELD (real memory, held voice)
    grown = [f for f in facts if f.get("rel_type") == "subclass_of"
             and f.get("fact_provenance") == "llm_inferred"]
    assert grown, f"the grown rung is still silenced on the measure question: {facts}"
    assert all(not band(f) for f in grown), \
        f"a grown rung rendered ASSERTED — the voice ladder is broken: {grown}"
    assert any(str(f.get("object") or "").lower() == "octopus" for f in grown), grown

"""Smoke regressions — ingest-side issues #35 / #36 / #37.

#35  A discourse-frame correction ("Correction: …", "I was wrong earlier, …") was never routed as
     a correction (spaCy roots the turn on the label noun; the clause-initial marker scan and the
     finite-ROOT frame peel both missed it) and the frame itself was harvested as content — an
     entity named ``correction`` carrying the new measurement, ``(user, feels, wrong)``, and the
     REJECTED value filed as a state ``(mineral, has_state, fluorite)``.
#36  "<N> <unit> long" filed the dimension from the UNIT (foot → height) instead of the degree
     adjective, and on a typed non-Person subject the generic measure was vetoed by the coverage gate
     while ``(x, has_state, long)`` survived — the shared ``long`` node bled one boat into another.
#37  "my X is called/named Y" bound nothing (the passive naming frame had no reader), and a
     mis-parsed object compound minted three entities for "my sourdough starter"; the correction
     message named "user" as the subject of an alias that moved on another entity.

Behaviour-level pins; every fix carries a fresh-domain control (kayak / violin / shed / pond) that
is in no smoke fixture, to prove the mechanism is grammatical, not lexical. The E2E section drives
the REAL harvest → /ingest → /query on a throwaway ``*_test`` POSTGRES_DSN tenant (skips without one
— disclosed, never a silent pass) with the PRODUCTION values of the spine post-pass flags.
"""
import asyncio
import datetime
import os
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
# PRODUCTION spine flags are read into MODULE CONSTANTS at src.api.main import time; the
# module-scoped autouse fixture below sets env + those constants for this module only.

import src.extraction.linguistics as L  # noqa: E402


def _apply_flags(mp, flags):
    """Set each flag in the env AND on the already-imported module constant of the same name, so
    this module's pins run on production values WITHOUT leaking them into the rest of the suite
    (a module-level os.environ.setdefault is applied at collection and never undone)."""
    import sys as _sys
    for k, v in flags.items():
        mp.setenv(k, v)
        for modname in ("src.api.main", "src.extraction.linguistics", "src.api.turn_budget"):
            mod = _sys.modules.get(modname)
            if mod is None or not hasattr(mod, k):
                continue
            cur = getattr(mod, k)
            if isinstance(cur, bool):
                new = v.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, float):
                new = float(v)
            else:
                new = v
            mp.setattr(mod, k, new)


@pytest.fixture(scope="module", autouse=True)
def _module_flags():
    import src.api.main  # noqa: F401 — the constants must exist before they are patched
    import src.api.turn_budget  # noqa: F401
    import src.extraction.linguistics  # noqa: F401
    mp = pytest.MonkeyPatch()
    _apply_flags(mp, _PROD_FLAGS)
    yield
    mp.undo()

_REF = datetime.datetime(2026, 9, 28)

_spine = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — the deriver no-ops")


def _triples(sentence, **kw):
    return [(f.subject, f.rel_type, (f.object or "").strip().lower(),
             getattr(f, "scalar_datatype", None))
            for f in L.derive_sentence_facts(sentence, _REF, **kw)]


def _plain(sentence, **kw):
    return [(s, r, o) for (s, r, o, _d) in _triples(sentence, **kw)]


# ═════════════════════════════════════════════════════════════════════════════
# #35 — discourse-frame corrections
# ═════════════════════════════════════════════════════════════════════════════

@_spine
@pytest.mark.parametrize("text,kind,main", [
    ("Correction: my favourite mineral is labradorite, not fluorite.", "nominal",
     "my favourite mineral is labradorite, not fluorite."),
    ("Correction: my workbench is 200 centimeters long, not 180.", "nominal",
     "my workbench is 200 centimeters long, not 180."),
    ("I was wrong earlier, my sailboat is 30 feet long.", "clausal",
     "my sailboat is 30 feet long."),
    # fresh domain
    ("Small correction: my kayak is 5 meters long.", "nominal", "my kayak is 5 meters long."),
])
def test_frame_split_finds_the_frame_and_the_scoped_clause(text, kind, main):
    fr = L.split_utterance_frame(text)
    assert fr is not None, text
    assert fr.kind == kind
    assert fr.main_clause == main


@_spine
@pytest.mark.parametrize("text", [
    "Actually, my beehive has 10 frames, not 12.",          # ADV marker: owned by the marker scan
    "When I was young, my dog was big.",                    # fronted subordinate clause
    "quick question for you: what is my shelf number",      # framed QUESTION
    "one more thing: forget my dog",                        # framed imperative (no subject)
    "My workbench is made of white oak, and it is 180 centimeters long.",  # coordination
    "my sable group meets on tuesdays",                     # no delimiter
])
def test_frame_split_declines_non_frames(text):
    assert L.split_utterance_frame(text) is None


def _main():
    import src.api.main as M
    return M


@_spine
@pytest.mark.parametrize("text", [
    "Correction: my favourite mineral is labradorite, not fluorite.",
    "Correction: my kayak is 5 meters long.",               # fresh domain, no contrast
    "Small correction: my violin is 90 years old.",         # fresh domain, premodified label
])
def test_label_frame_routes_correction_confident(text):
    M = _main()
    assert M._spacy_cue_route(text) == ("CORRECTION", "confident")


_SEEDED_WAS_WRONG = [("was wrong", "contradiction", 0.85), ("mistake", "contradiction", 0.75),
                     ("actually", "reclarification", 0.85)]


@_spine
@pytest.mark.parametrize("text", [
    "I was wrong earlier, my sailboat is 30 feet long.",
    "I was wrong, my violin is 60 years old.",               # fresh domain
])
def test_self_report_frame_routes_by_the_grown_signal(monkeypatch, text):
    M = _main()
    monkeypatch.setattr(M, "_tenant_correction_signals", lambda _u: list(_SEEDED_WAS_WRONG))
    assert M._spacy_cue_route(text, "u") == ("CORRECTION", "confident")
    # the GROWN table decides, not a code list: with no grown cue the frame is not a repair
    monkeypatch.setattr(M, "_tenant_correction_signals", lambda _u: [])
    assert M._spacy_cue_route(text, "u") == (None, None)


@_spine
def test_self_report_below_gate_weight_is_only_ambiguous(monkeypatch):
    M = _main()
    monkeypatch.setattr(M, "_tenant_correction_signals",
                        lambda _u: [("was wrong", "contradiction", 0.5)])
    assert M._spacy_cue_route("I was wrong earlier, my sailboat is 30 feet long.", "u") == \
        ("CORRECTION", "ambiguous")


@_spine
@pytest.mark.parametrize("text,expected", [
    ("Actually I do like pizza.", ("CORRECTION", "ambiguous")),        # marker policy unchanged
    ("small update: actually, the bosterway moved to murvale", (None, None)),  # lead-in pin
    ("I was happy, my dog is 3 years old.", (None, None)),             # self frame, no repair cue
    ("small heads up for whenever it matters: my sable group meets on tuesdays", (None, None)),
])
def test_route_controls_unchanged(monkeypatch, text, expected):
    M = _main()
    monkeypatch.setattr(M, "_tenant_correction_signals", lambda _u: list(_SEEDED_WAS_WRONG))
    assert M._spacy_cue_route(text, "u") == expected


@_spine
def test_rejected_value_is_never_filed_as_a_state():
    # "labradorite, NOT fluorite": the rejected noun is the acomp carrying its own neg child
    assert not any(r == "has_state" and o == "fluorite"
                   for (_s, r, o) in _plain("my favourite mineral is labradorite, not fluorite."))
    # fresh domain
    assert not any(r == "has_state" and o == "cedar"
                   for (_s, r, o) in _plain("my favourite wood is walnut, not cedar."))


@_spine
def test_negated_state_on_the_copula_still_captured():
    # control: a NEGATED STATE ("is not down") hangs neg off the copula and is still captured
    facts = L.derive_sentence_facts("The server is not down.", _REF)
    assert any(f.subject == "server" and f.rel_type == "has_state" and f.object == "down"
               for f in facts), [(f.subject, f.rel_type, f.object) for f in facts]


# ═════════════════════════════════════════════════════════════════════════════
# #36 — the measure's dimension is the degree adjective's, the value keeps its unit
# ═════════════════════════════════════════════════════════════════════════════

def test_wordnet_degree_dimension():
    from src.api.wordnet_ladder import degree_adjective_dimension as d
    known = {"height", "age", "weight", "duration"}
    assert d("long", "height", known) == "length"       # foot/metre scale → length
    assert d("long", "duration", known) == "duration"   # hour scale → duration
    assert d("tall", "height", known) == "height"       # stature.n.02 lemma 'height' is known
    assert d("wide", "height", known) == "width"
    assert d("deep", "height", known) == "depth"
    assert d("heavy", "weight", known) == "weight"
    assert d("old", "age", known) == "age"
    assert d("xyzzy", "height", known) is None


@_spine
@pytest.mark.parametrize("sentence,subj,rel,val", [
    ("My workbench is 180 centimeters long.", "workbench", "length", "180 centimeters"),
    ("My sailboat is 28 feet long.", "sailboat", "length", "28 feet"),
    ("my workbench is 200 centimeters long, not 180.", "workbench", "length", "200 centimeters"),
    # fresh domains
    ("My kayak is 60 centimeters wide.", "kayak", "width", "60 centimeters"),
    ("My pond is 2 meters deep.", "pond", "depth", "2 meters"),
])
def test_dimension_from_the_degree_adjective_value_verbatim(sentence, subj, rel, val):
    t = _triples(sentence)
    assert (subj, rel, val, "quantity") in t, t
    assert not any(r == "height" for (_s, r, _o, _d) in t), t
    assert not any(r == "has_state" for (_s, r, _o, _d) in t), t


# Issue #52 amended this pin. The dimension still agrees with the unit (height/age/duration,
# no override), but the VALUE now keeps its unit ("6 feet", "2 hours") unless the dimension's own
# rel metadata declares a bare count (age: scalar_datatype integer -> "62"). The old pin was
# environment-dependent (it read whatever overlay the DSN exposed) and encoded the unit drop; the
# seeded metadata is pinned explicitly so it reads the same with or without a DSN.
_SEEDED_DIM_META = {"height": {"scalar_datatype": "quantity", "head_types": ["Person"],
                               "tail_types": ["SCALAR"]},
                    "age": {"scalar_datatype": "integer", "head_types": ["ANY"],
                            "tail_types": ["SCALAR"]},
                    "duration": {"scalar_datatype": "duration", "head_types": ["ANY"],
                                 "tail_types": ["SCALAR"]}}


@_spine
@pytest.mark.parametrize("sentence,triple", [
    ("He is 6 feet tall.", ("he", "height", "6 feet")),
    ("She is 62 years old.", ("she", "age", "62")),
    ("The movie is 2 hours long.", ("movie", "duration", "2 hours")),
])
def test_agreeing_dimension_is_byte_identical(monkeypatch, sentence, triple):
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_SEEDED_DIM_META))
    assert triple in _plain(sentence), _plain(sentence)


def _typed(sentence, label="Object"):
    from spacy.tokens import Span
    doc = L._parse(sentence)
    subj = next(t for t in doc if t.dep_ in ("nsubj", "nsubjpass"))
    doc.set_ents([Span(doc, subj.i, subj.i + 1, label=label)])
    return doc


_OVERLAY_GROWN = {
    # the live shape: /ingest mints a novel scalar rel with head/tail {ANY}
    "related_measure": {"head_types": ["ANY"], "tail_types": ["ANY"]},
    "length": {"head_types": ["ANY"], "tail_types": ["ANY"]},
    "height": {"head_types": ["Person"], "tail_types": ["SCALAR"]},
    "has_state": {"head_types": ["ANY"], "tail_types": ["Concept"]},
}


@pytest.fixture
def grown_gate(monkeypatch):
    monkeypatch.setattr(L, "SPINE_COVERAGE_GATE", True, raising=False)
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_OVERLAY_GROWN))
    monkeypatch.setattr(L, "_rel_head_types", lambda rt: tuple(
        h.upper() for h in (_OVERLAY_GROWN.get((rt or "").lower()) or {}).get("head_types", [])))
    monkeypatch.setattr(L, "_unit_scalar_map",
                        lambda: {"foot": "height", "meter": "height", "centimeter": "height"})
    yield


@_spine
def test_typed_object_dimension_survives_the_grown_coverage_gate(grown_gate):
    # warm-seat shape: 'length' already minted {ANY}. The value must land, and no state twin.
    t = [(f.subject, f.rel_type, (f.object or "").lower(), f.scalar_datatype)
         for f in L.derive_sentence_facts(_typed("My sailboat is 28 feet long."), _REF)]
    assert ("sailboat", "length", "28 feet", "quantity") in t, t
    assert not any(r == "has_state" for (_s, r, _o, _d) in t), t


@_spine
def test_typed_object_person_scoped_dimension_keeps_magnitude(grown_gate):
    # "tall" → height, Person-scoped: the Object subject steps aside to the generic measure, which
    # must NOT be vetoed by the grown {ANY} range (the live 'coverage_gate_veto' wound), and the
    # degree word never becomes a state. Fresh domain.
    t = [(f.subject, f.rel_type, (f.object or "").lower(), f.scalar_datatype)
         for f in L.derive_sentence_facts(_typed("My shed is 3 meters tall."), _REF)]
    assert ("shed", "related_measure", "3 meters", "quantity") in t, t
    assert not any(r == "has_state" for (_s, r, _o, _d) in t), t


# ═════════════════════════════════════════════════════════════════════════════
# #37 — the passive naming frame names the subject; one NP = one entity
# ═════════════════════════════════════════════════════════════════════════════

@_spine
@pytest.mark.parametrize("sentence,typ,name", [
    ("My sailboat is called Windrift.", "sailboat", "windrift"),
    ("My sourdough starter is named Clint.", "sourdough starter", "clint"),
    # fresh domains
    ("My kayak is called Minnow.", "kayak", "minnow"),
    ("My violin is named Stradi.", "violin", "stradi"),
])
def test_passive_naming_names_the_subject(monkeypatch, sentence, typ, name):
    monkeypatch.setattr(L, "SPINE_NAMING_CHAIN", True)  # production value (deploy compose)
    t = _plain(sentence)
    assert (typ, "pref_name", name) in t, t
    # ONE NP = ONE entity: the name is a label ON the subject, never a second referent
    assert not any(s == name for (s, _r, _o) in t), t
    assert not any(r == "instance_of" for (_s, r, _o) in t), t


@_spine
def test_passive_naming_controls(monkeypatch):
    monkeypatch.setattr(L, "SPINE_NAMING_CHAIN", True)
    # negation binds nothing
    assert not any("stradi" in (s, o) for (s, _r, o) in _plain("My violin is not named Stradi."))
    # a determiner-introduced complement is a TYPE, not a name
    assert not any(r in ("pref_name", "also_known_as")
                   for (_s, r, _o) in _plain("My sailboat is called a sloop."))
    # a kin role stays with the kin chain (the named PERSON is the entity)
    t = _plain("My mother is named Sarah.")
    assert ("sarah", "parent_of", "user") in t and not any(r == "pref_name" for (_s, r, _o) in t), t
    # the reduced relative keeps its named-instance reading (untouched)
    assert any(r == "instance_of" for (_s, r, _o) in _plain("I have a kayak named Minnow."))


def _manual_doc(words, heads, deps, pos):
    from spacy.tokens import Doc
    return Doc(L._get_nlp().vocab, words=words, heads=heads, deps=deps, pos=pos)


@_spine
def test_split_object_compound_is_rejoined():
    # the live mis-parse: two contiguous NOUN dobj siblings of one verb, poss on the first
    doc = _manual_doc(["I", "wax", "my", "kayak", "paddle"], [1, 1, 3, 1, 1],
                      ["nsubj", "ROOT", "poss", "dobj", "dobj"], ["PRON", "VERB", "PRON", "NOUN", "NOUN"])
    assert L._repair_split_nominal_compound(doc) == 1
    assert doc[3].dep_ == "compound" and doc[3].head.i == 4
    assert doc[2].head.i == 4  # the possessor moved to the head noun
    assert L._np_phrase(doc[4]) == "kayak paddle"


@_spine
def test_split_compound_repair_leaves_coordination_and_names():
    doc = _manual_doc(["I", "bought", "apples", "and", "pears"], [1, 1, 1, 2, 2],
                      ["nsubj", "ROOT", "dobj", "cc", "conj"], ["PRON", "VERB", "NOUN", "CCONJ", "NOUN"])
    assert L._repair_split_nominal_compound(doc) == 0
    doc = _manual_doc(["I", "call", "Rex", "dog"], [1, 1, 1, 1],
                      ["nsubj", "ROOT", "dobj", "dobj"], ["PRON", "VERB", "PROPN", "NOUN"])
    assert L._repair_split_nominal_compound(doc) == 0


@_spine
def test_sourdough_atom_is_one_entity():
    t = _plain("I feed my sourdough starter every 12 hours.")
    surfaces = {s for (s, _r, _o) in t} | {o for (_s, _r, o) in t}
    assert "sourdough starter" in surfaces, t
    assert "sourdough" not in surfaces and "starter" not in {s for (s, _r, _o) in t}, t
    assert ("user", "owns", "sourdough") not in t, t


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self.rows


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self.rows)


def test_correction_label_names_the_entity_not_user():
    M = _main()
    # the name correction moved the preferred alias to the new value → label by an unmoved alias
    db = _DB([("doughbi",), ("sourdough starter",), ("clint",)])
    assert M._correction_subject_label(db, "e-1", "seat-1", "Clint", "Doughbi",
                                       fallback="user") == "sourdough starter"
    # the seat's own entity keeps the extractor's first-person label
    assert M._correction_subject_label(db, "seat-1", "seat-1", "a", "b", fallback="user") == "user"
    # unreadable store → fallback, never a failure
    assert M._correction_subject_label(None, "e-1", "seat-1", "a", "b", fallback="user") == "user"


# ═════════════════════════════════════════════════════════════════════════════
# E2E — real harvest → /ingest → /query on a throwaway tenant, production post-pass flags
# ═════════════════════════════════════════════════════════════════════════════

_DSN = os.environ.get("POSTGRES_DSN", "")
_PROD_FLAGS = {
    "SENTENCE_PIPELINE": "true", "SPINE_COVERAGE_GATE": "true", "SPINE_IMPLICATIVE_XCOMP": "true",
    "SPINE_NAMING_CHAIN": "true", "SPINE_PA_CORE": "1", "SPINE_PENDING_GROUNDING": "true",
    "SPINE_POSSESSIVE_ALIENABILITY": "true", "QUERY_NAME_INTENT_SURFACING": "1",
    "QUERY_WALK_READS_ATTRIBUTES": "1", "INGEST_QUEUE_CONCEPT_GROUNDING": "1",
}


@pytest.fixture(scope="module")
def e2e():
    if not _DSN or not _DSN.rsplit("/", 1)[-1].endswith("_test"):
        pytest.skip("E2E needs a throwaway *_test POSTGRES_DSN (skipped — disclosed)")
    mp = pytest.MonkeyPatch()
    for k, v in _PROD_FLAGS.items():
        mp.setenv(k, v)
    mp.setenv("FAULTLINE_MIGRATIONS_DIR", "./migrations")
    mp.setenv("QDRANT_URL", "http://127.0.0.1:1")
    import psycopg2
    from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid
    import src.api.main as M
    for flag in ("SPINE_NAMING_CHAIN", "SPINE_COVERAGE_GATE", "SPINE_PENDING_GROUNDING"):
        mp.setattr(L, flag, True, raising=False)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    conn = psycopg2.connect(_DSN)
    schema, status = create_user_schema(user_id, slug, conn)
    assert status == "ready", status
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES "
                    "(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                    "VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING", (user_id, schema))
    conn.commit()
    conn.close()
    mp.setattr(M, "_INGEST_ENABLED", True)
    M._TENANT_READY_CACHE.add(user_id)

    class _D:
        def get(self, k):
            return None

        def setex(self, k, t, v):
            return True

        def ping(self):
            return True

    class _Idem:
        redis_url = "dict://test"
        ttl = 3600
        client = _D()

    mp.setattr(M, "_idempotency_mgr", _Idem())
    from starlette.testclient import TestClient
    with TestClient(M.app) as client:
        import time
        t0 = time.time()
        while L._nlp is None and time.time() - t0 < 180:
            time.sleep(0.5)
        yield {"client": client, "user_id": user_id, "schema": schema, "M": M, "mp": mp}
    mp.undo()
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
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


def _state(env, text, atoms=None):
    """One STATEMENT turn through the live harvest. ``atoms`` = the live atomizer shape captured
    from the live log (the atomizer is the only substitution); None = deterministic segmentation."""
    M = env["M"]
    import src.extraction.reframe as RF
    mp = pytest.MonkeyPatch()
    try:
        if atoms is not None:
            async def _stub(t, u, m=None):
                # the live atoms for the turn as stated; if the harvest handed the atomizer a
                # DIFFERENT text (a repair frame was peeled) the atomizer sees only that text
                use = list(atoms) if t.strip() == text.strip() else [t]
                return RF.ReframeResult(atoms=[RF.Atom(text=a, source_span=a) for a in use],
                                        used_llm=True, rejected_count=0)
            mp.setattr(RF, "reframe_to_atomic", _stub)
            mp.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", False, raising=False)
        else:
            mp.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True, raising=False)
        c = env["client"]
        h = c.post("/harvest-spans", json={"text": text, "user_id": env["user_id"]})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        if edges:
            r = c.post("/ingest", json={"text": text, "user_id": env["user_id"], "edges": edges,
                                        "source": "mcp"})
            assert r.status_code == 200, r.text[:300]
        return edges
    finally:
        mp.undo()


def _ask(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return [(f.get("subject"), f.get("rel_type"), str(f.get("object"))) for f in (r.get("facts") or [])]


def _alias_owner(env, alias):
    rows = _rows(env["schema"], "SELECT entity_id FROM entity_aliases WHERE alias = %s", (alias,))
    return {r[0] for r in rows}


@_spine
def test_e2e_measure_lands_on_its_dimension_and_does_not_bleed(e2e):
    _state(e2e, "My workbench is made of white oak and it is 180 centimeters long.",
           ["My workbench is made of white oak.", "My workbench is 180 centimeters long."])
    _state(e2e, "My sailboat is called Windrift and it is 28 feet long.",
           ["My sailboat is called Windrift.", "My sailboat is 28 feet long."])
    _state(e2e, "My kayak is called Minnow and it is 4 meters long.",
           ["My kayak is called Minnow.", "My kayak is 4 meters long."])  # fresh domain
    attrs = _rows(e2e["schema"], "SELECT attribute, value_text FROM entity_attributes "
                                 "WHERE superseded_at IS NULL")
    assert ("length", "180 centimeters") in attrs and ("length", "28 feet") in attrs, attrs
    assert ("length", "4 meters") in attrs, attrs
    states = _rows(e2e["schema"], "SELECT rel_type FROM facts WHERE rel_type = 'has_state'")
    assert not states, states
    got = _ask(e2e, "How long is my workbench?")
    assert any("180 centimeters" in o for (_s, _r, o) in got), got
    got = _ask(e2e, "How long is my kayak?")
    assert any("4 meters" in o for (_s, _r, o) in got), got
    assert not any("180" in o or "28" in o for (_s, _r, o) in got), got   # no bleed


@_spine
def test_e2e_passive_names_land_as_aliases(e2e):
    _state(e2e, "My sourdough starter is named Clint and I feed it every 12 hours.",
           ["My sourdough starter is named Clint.", "I feed my sourdough starter every 12 hours."])
    _state(e2e, "My beehive has 12 frames and the queen is named Beatrix.",
           ["My beehive has 12 frames.", "The queen is named Beatrix."])
    _state(e2e, "My violin is called Stradi.", ["My violin is called Stradi."])  # fresh domain
    # the name is the PREFERRED label of the SAME entity its type noun names (one thing, one
    # node) — the kinship-chain shape (Sarah pref, mother alt) applied to a named owned thing
    for typ, name in (("sailboat", "windrift"), ("sourdough starter", "clint"),
                      ("queen", "beatrix"), ("kayak", "minnow"), ("violin", "stradi")):
        owners = _alias_owner(e2e, name)
        assert owners, f"{name!r} alias never registered"
        assert owners & _alias_owner(e2e, typ), f"{name!r} is not a label of the {typ!r} entity"
        pref = _rows(e2e["schema"], "SELECT is_preferred FROM entity_aliases WHERE alias = %s",
                     (name,))
        assert any(p for (p,) in pref), f"{name!r} registered non-preferred"
    got = _ask(e2e, "What is my sailboat's name?")
    assert any("windrift" in str(x).lower() for f in got for x in f), got
    # ONE NP = ONE entity: no standalone modifier/head entity for the compound
    assert not _alias_owner(e2e, "sourdough"), "the modifier 'sourdough' minted its own entity"
    # 'starter' may exist only as the L4 TYPE node the named instance is filed at (clint
    # instance_of starter) — never as a second user-owned/fed THING
    _starter = _alias_owner(e2e, "starter")
    user_objs = {r[0] for r in _rows(e2e["schema"],
                                     "SELECT object_id FROM facts WHERE subject_id = %s "
                                     "AND rel_type <> 'instance_of'", (e2e["user_id"],))}
    assert not (_starter & user_objs), "'starter' is a second user-held entity"


@_spine
def test_e2e_repair_frame_is_never_content(e2e):
    edges = _state(e2e, "I was wrong earlier, my sailboat is 30 feet long.",
                   ["I was wrong earlier, my sailboat is 30 feet long."])
    assert not any(e.get("rel_type") == "feels" or (e.get("object") or "") == "wrong"
                   for e in edges), edges
    edges = _state(e2e, "Correction: my workbench is 200 centimeters long, not 180.",
                   ["Correction: my workbench is 200 centimeters long, not 180."])
    assert not any((e.get("subject") or "") == "correction" for e in edges), edges
    assert not _alias_owner(e2e, "correction"), "the frame noun minted an entity"
    got = _ask(e2e, "How long is my sailboat?")
    assert any("30 feet" in o for (_s, _r, o) in got), got


@_spine
def test_e2e_self_report_routes_on_the_seeded_signal(e2e):
    # the tenant's OWN seeded correction_signals row ('was wrong', contradiction) decides
    M = e2e["M"]
    e2e["mp"].setenv("POSTGRES_DSN", _DSN)
    assert M._spacy_cue_route("I was wrong earlier, my sailboat is 30 feet long.",
                              e2e["user_id"]) == ("CORRECTION", "confident")
    assert M._spacy_cue_route("I was wrong, my violin is 60 years old.",
                              e2e["user_id"]) == ("CORRECTION", "confident")


# ── #35 harvest seam (no DB): the repair frame never reaches the deriver ────────────────────────
def _harvest_one(monkeypatch, text):
    import src.api.main as M
    import src.extraction.reframe as RF
    from src.api.models import RewriteRequest
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", False, raising=False)

    async def _one_atom(t, u, m=None):  # the live atomizer shape: atom_count=1, the turn itself
        return RF.ReframeResult(atoms=[RF.Atom(text=t, source_span=t)], used_llm=True,
                                rejected_count=0)
    monkeypatch.setattr(RF, "reframe_to_atomic", _one_atom)
    uid = "00000000-0000-4000-8000-000000000035"
    r = asyncio.run(M._harvest_via_sentence_pipeline(RewriteRequest(text=text, user_id=uid), uid))
    return [(e.get("subject"), e.get("rel_type"), (e.get("object") or "").lower())
            for e in (r or {}).get("edges", [])]


@_spine
@pytest.mark.parametrize("text,frame_noun", [
    ("Correction: my workbench is 200 centimeters long, not 180.", "correction"),
    ("Small correction: my kayak is 5 meters long.", "correction"),     # fresh domain
])
def test_harvest_label_frame_is_not_content(monkeypatch, text, frame_noun):
    edges = _harvest_one(monkeypatch, text)
    assert edges, "the scoped clause must still be harvested"
    assert not any(frame_noun in (s or "") for (s, _r, _o) in edges), edges


@_spine
def test_harvest_self_report_frame_is_not_content(monkeypatch):
    import src.api.main as M
    monkeypatch.setattr(M, "_tenant_correction_signals", lambda _u: list(_SEEDED_WAS_WRONG),
                        raising=False)
    edges = _harvest_one(monkeypatch, "I was wrong earlier, my sailboat is 30 feet long.")
    assert edges, "the scoped clause must still be harvested"
    assert not any(o == "wrong" or r == "feels" for (_s, r, o) in edges), edges



# ═════════════════════════════════════════════════════════════════════════════
# ROUND 2 (critic gaps)
# ═════════════════════════════════════════════════════════════════════════════

# GAP 1 — the passive-naming arm must never rebind a name across referents
@_spine
@pytest.mark.parametrize("sentence,wrong_subject", [
    ("My neighbour's dog is named Fido.", "dog"),      # third-party possessor
    ("My sister's cat is named Tom.", "cat"),          # kin possessor, fresh domain
])
def test_third_party_possessed_name_never_lands_on_bare_np(monkeypatch, sentence, wrong_subject):
    monkeypatch.setattr(L, "SPINE_NAMING_CHAIN", True)
    t = _plain(sentence)
    assert not any(r in ("pref_name", "also_known_as") and s == wrong_subject
                   for (s, r, _o) in t), t


@_spine
def test_two_same_head_nps_owned_by_different_people_stay_apart(monkeypatch):
    monkeypatch.setattr(L, "SPINE_NAMING_CHAIN", True)
    mine = _plain("My dog is named Rex.")
    theirs = _plain("My neighbour's dog is named Fido.")
    assert ("dog", "pref_name", "rex") in mine, mine
    names_on_dog = {o for (s, r, o) in mine + theirs if s == "dog" and r == "pref_name"}
    assert names_on_dog == {"rex"}, names_on_dog


@_spine
def test_bare_definite_names_only_with_a_turn_antecedent(monkeypatch):
    monkeypatch.setattr(L, "SPINE_NAMING_CHAIN", True)
    # no antecedent: a generic definite is not the speaker's thing
    assert not any(r == "pref_name" for (_s, r, _o) in _plain("The disease is called Lyme."))
    assert not any(r == "pref_name" for (_s, r, _o) in _plain("The queen is named Beatrix."))
    # bridging definite with an antecedent already in the turn (the smoke's beehive→queen)
    assert ("queen", "pref_name", "beatrix") in _plain("The queen is named Beatrix.",
                                                       prior_nps=["beehive"])


# GAP 2 — a marker lemma in a label's MODIFIER is not a repair frame
@_spine
@pytest.mark.parametrize("text", [
    "Correction tape: my desk drawer has a roll of it.",
    "Correction officers: my brother works with them.",
])
def test_marker_in_frame_modifier_is_not_a_correction(monkeypatch, text):
    M = _main()
    monkeypatch.setattr(M, "_tenant_correction_signals", lambda _u: list(_SEEDED_WAS_WRONG))
    assert M._repair_frame(text, "u") is None
    assert M._spacy_cue_route(text, "u") == (None, None)


# GAP 3 — the contrasted-away value is dropped, the ASSERTED value is filed
@_spine
@pytest.mark.parametrize("sentence,subj,good,bad", [
    ("My tent is blue, not orange.", "tent", "blue", "orange"),
    ("my canoe is green, not red.", "canoe", "green", "red"),
])
def test_contrast_files_the_asserted_value(sentence, subj, good, bad):
    t = _plain(sentence)
    assert (subj, "has_state", good) in t, t
    assert not any(o == bad for (_s, _r, o) in t), t


# M14 / M15 — the clausal frame routes only for the SPEAKER and only on contradiction rows
@_spine
def test_third_person_self_report_is_not_a_repair(monkeypatch):
    M = _main()
    monkeypatch.setattr(M, "_tenant_correction_signals", lambda _u: list(_SEEDED_WAS_WRONG))
    assert M._spacy_cue_route("My friend was wrong, my sailboat is 30 feet long.", "u") == \
        (None, None)


@_spine
def test_clausal_frame_needs_a_contradiction_row(monkeypatch):
    M = _main()
    monkeypatch.setattr(M, "_tenant_correction_signals",
                        lambda _u: [("was wrong", "reclarification", 0.95)])
    assert M._spacy_cue_route("I was wrong earlier, my sailboat is 30 feet long.", "u") == \
        (None, None)


# M2 — /classify-intent hands the SEAT to the cue route (the grown clausal route is per-tenant)
@_spine
def test_classify_intent_passes_the_seat_to_the_cue_route(monkeypatch):
    import httpx
    M = _main()
    uid = "00000000-0000-4000-8000-000000000035"
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    async def _ready(*_a, **_k):
        return None
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)
    monkeypatch.setattr(M, "CORRECTION_BYPASS_GATE", True)
    monkeypatch.setattr(M, "QUERY_INTERROGATIVE_PREROUTE", False)
    monkeypatch.setattr(M, "_tenant_correction_signals",
                        lambda u: list(_SEEDED_WAS_WRONG) if u == uid else [])
    M.app.dependency_overrides[M.get_gliner_model] = lambda: object()
    try:
        async def _go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=M.app),
                                         base_url="http://t") as c:
                return await c.post("/classify-intent", params={"user_id": uid},
                                    json={"text": "I was wrong earlier, my sailboat is 30 feet long."})
        r = asyncio.run(_go())
    finally:
        M.app.dependency_overrides.pop(M.get_gliner_model, None)
    assert r.status_code == 200, r.text
    assert r.json().get("intent") == "CORRECTION", r.json()


# M4 — correct_fact reports the label helper, never the extractor's raw subject
def test_correct_fact_response_is_wired_to_the_subject_label():
    import ast
    import inspect
    M = _main()
    tree = ast.parse(inspect.getsource(M.correct_fact).lstrip())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "FactCorrectionResponse"
             and any(k.arg == "status" and getattr(k.value, "value", None) == "corrected"
                     for k in n.keywords)
             and any(k.arg == "old_rel_type" for k in n.keywords)]
    assert calls, "corrected response not found"
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert getattr(kw["subject_name"], "id", None) == "_subject_label", ast.dump(kw["subject_name"])
        names = {n.id for n in ast.walk(kw["message"]) if isinstance(n, ast.Name)}
        assert "_subject_label" in names and "extraction" not in names, names


# M11a — the anaphor (distributive) measure is a PLACED quantity (survives the grown gate)
@_spine
def test_anaphor_measure_is_placed_quantity():
    topic = L.discourse_topic_from_doc(L._parse("My tomato plants were planted on May 15th."), [])
    t = [(f.subject, f.rel_type, (f.object or "").lower(), f.scalar_datatype)
         for f in L.derive_sentence_facts("Each one is roughly 90 centimeters tall.", None,
                                          discourse_topic=topic)]
    assert any(r == "related_measure" and "90 centimeters" in o and d == "quantity"
               for (_s, r, o, d) in t), t


# M12c — a KNOWN dimension that refuses the subject falls to the generic measure, never the unit's rel
@_spine
def test_refused_known_dimension_falls_to_generic_measure(monkeypatch):
    ov = {"size": {"head_types": ["ANY"], "tail_types": ["ANY"]},
          "length": {"head_types": ["Person"], "tail_types": ["SCALAR"]}}
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(ov))
    monkeypatch.setattr(L, "_rel_head_types", lambda rt: tuple(
        h.upper() for h in (ov.get((rt or "").lower()) or {}).get("head_types", [])))
    monkeypatch.setattr(L, "_unit_scalar_map", lambda: {"foot": "size"})
    t = [(f.subject, f.rel_type, (f.object or "").lower())
         for f in L.derive_sentence_facts(_typed("My sailboat is 28 feet long."), _REF)]
    assert ("sailboat", "related_measure", "28 feet") in t, t
    assert not any(r == "size" for (_s, r, _o) in t), t



@_spine
def test_e2e_round2_no_cross_referent_rebind_and_asserted_colour(e2e):
    _state(e2e, "My dog is named Rex.", ["My dog is named Rex."])
    _state(e2e, "My neighbour's dog is named Fido.", ["My neighbour's dog is named Fido."])
    rex = _alias_owner(e2e, "rex")
    assert rex, "rex never registered"
    assert not (_alias_owner(e2e, "fido") & rex), "fido rebound onto the user's own dog"
    got = _ask(e2e, "What is my dog's name?")
    flat = " ".join(str(x).lower() for f in got for x in f)
    assert "fido" not in flat, got
    _state(e2e, "My tent is blue, not orange.", ["My tent is blue, not orange."])
    blob = " ".join(str(x).lower() for f in _ask(e2e, "What color is my tent?") for x in f)
    assert "orange" not in blob, blob
    states = _rows(e2e["schema"], "SELECT a.alias FROM facts f JOIN entity_aliases a "
                                  "ON a.entity_id = f.object_id WHERE f.rel_type = 'has_state'")
    assert ("blue",) in states and ("orange",) not in states, states

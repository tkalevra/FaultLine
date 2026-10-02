"""smoke-read (issues #31 R2 + #38 items 2-5): reader/render defects where the rows exist.

Measured on a live deployment and reproduced in-process by
replaying that seat's rows into a fresh throwaway seat with production flag values:

  #31 R2a "What do I know about woodworking?" -> honest-empty. `_topic_grounded_facts` took
          its topic from `_topic_lemmas(query)`; `woodworking` is SemCor-unmeasured, so the
          knowledge-ask FRAME verb `know` won the argmin and the gate dropped 13 -> 0.
  #31 R2b "What do I know about beekeeping?" -> only the interest line. The grown-topic chain
          recognised an expanded topic ONLY by an entity_taxonomies row, which /expand
          beekeeping never grew (one part_of edge < the grouping mint's freq gate), so
          `apiary subclass_of beekeeping` was silenced as grown ontology.
  #31 R2c the /learn validator staged `queen bee instance_of hive` / `beekeeper instance_of
          apiary` — an is-a whose object cannot be a class of the subject.
  #38 2   "How many frames does my beehive have?" -> "You have 3 frames does my beehive on
          record." (count Shape C swallowed the do-support clause).
  #38 3   "Your chess rating is 1520 (age 506)" (a bare 4-digit count read as a year).
  #38 4   "I don't have any information about about woodworking" (stranded preposition).
  #38 5   "What do I know about sailing?" -> the informative abstention voiced sixteen
          unrelated owned rows (the queried noun typed only to a residual upper bucket).

Every seeded row below copies the live staged-row SHAPE (fact_class B, confidence 0.8,
provenance llm_learn, fact_provenance llm_learned, is_hierarchy_rel true). Unit pins run
everywhere; E2E pins skip without a throwaway test-named POSTGRES_DSN — disclosed.
"""
import os
import time
import uuid
from unittest.mock import patch

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
# production values of the query-side flags, applied per module by _module_flags below
_MODULE_FLAGS = {
    "SENTENCE_PIPELINE": "true",
    "QUERY_WALK_READS_ATTRIBUTES": "1",
    "QUERY_NAME_INTENT_SURFACING": "1",
    "L4_WORDNET_LADDER": "true",
    "SCOPE_SURFACE_CONTEXT_C": "0",
    "VALUE_PLACE_FIRST_CLASS": "1",
    "TURN_BUDGET_S": "120",  # local warm-up is slow; budget is not behaviour
    "WGM_LLM_MODEL": "qwen/qwen3.5-9b",  # /learn needs a model id (LLM is stubbed)
}


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
    _apply_flags(mp, _MODULE_FLAGS)
    yield
    mp.undo()


_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = pytest.mark.skipif("test" not in _DSN.lower(),
                          reason="no throwaway test-named POSTGRES_DSN")


# ─────────────────────────────────────────────────────────────────────────────
# UNITS
# ─────────────────────────────────────────────────────────────────────────────

def _f(subj, rel, obj):
    return {"subject": subj, "rel_type": rel, "object": obj,
            "definition": f"{subj} is related to {obj}"}


def test_knowledge_ask_gate_judges_the_residual_not_the_frame():
    """#31 R2a: the frame verb never becomes the topic; the residual's own word decides."""
    from src.api.main import _topic_grounded_facts
    rows = [_f("joinery", "subclass_of", "woodworking"),
            _f("woodworking tool", "part_of", "woodworking"),
            _f("ccd camera", "part_of", "astrophotography")]
    out = _topic_grounded_facts("What do I know about woodworking?", rows)
    subs = sorted(r["subject"] for r in out)
    assert subs == ["joinery", "woodworking tool"], subs


def test_knowledge_ask_gate_fresh_domain_control():
    """Fresh domain, SemCor-measured residual: the lexical gate still works as before
    (topical row kept, off-topic row dropped)."""
    from src.api.main import _topic_grounded_facts
    rows = [_f("watering schedule", "subclass_of", "orchid care"),
            _f("ccd camera", "part_of", "astrophotography")]
    out = _topic_grounded_facts("What do I know about orchid care?", rows)
    assert [r["subject"] for r in out] == ["watering schedule"], out


def test_non_knowledge_ask_gate_unchanged():
    """Control: a non-frame query keeps today's topic derivation (undecidable → unchanged)."""
    from src.api.main import _topic_grounded_facts
    rows = [_f("joinery", "subclass_of", "woodworking")]
    assert _topic_grounded_facts("", rows) == rows


def test_bare_year_count_gets_no_age():
    """#38 item 3: a four-digit count on a non-date rel is not a year."""
    from src.api import main as M
    fact = {"source": "attributes", "object": "1520", "rel_type": "chess_rating"}
    with patch.object(M, "_rel_meta", return_value={"category": "pending_placement"}):
        out = M._annotate_date_derivation(fact, "Your chess rating is 1520")
    assert out == "Your chess rating is 1520", out


def test_bare_year_on_a_date_rel_still_derives_age():
    """Control: a bare year on a rel DECLARED date/temporal keeps its derived age, and an
    ISO date derives regardless of metadata."""
    from src.api import main as M
    with patch.object(M, "_rel_meta",
                      return_value={"category": "temporal", "scalar_datatype": "date"}):
        out = M._annotate_date_derivation(
            {"source": "attributes", "object": "1990", "rel_type": "born_on"},
            "You were born on 1990")
    assert "(age " in out, out
    with patch.object(M, "_rel_meta", return_value={}):
        out2 = M._annotate_date_derivation(
            {"source": "attributes", "object": "2020-04-03", "rel_type": "born_in"},
            "Fraggle was born in 2020-04-03")
    assert "(age " in out2, out2


def test_honest_empty_template_no_doubled_preposition():
    """#38 item 4."""
    from src.mcp.server import _render_abstention
    out = _render_abstention("What do I know about woodworking?")
    assert "about about" not in out, out
    assert "about woodworking" in out, out


def test_honest_empty_template_controls():
    from src.mcp.server import _query_subject_phrase
    assert _query_subject_phrase("when did I book the Airbnb in Sacramento") == \
        "the airbnb in sacramento"
    assert _query_subject_phrase("what is my daily commute") == "daily commute"
    assert _query_subject_phrase("where did I go to school") == "school"


def test_count_intent_declines_non_speaker_do_support():
    """#38 item 2: the question text must never become the count noun phrase."""
    from src.api.main import _detect_count_intent
    assert _detect_count_intent("How many frames does my beehive have?") == {}
    assert _detect_count_intent("how many legs does a spider have") == {}


def test_count_intent_controls_unchanged():
    from src.api.main import _detect_count_intent
    assert _detect_count_intent("How many tanks do I have?")["type_phrase"] == "tanks"
    assert _detect_count_intent(
        "how many babies were born to friends and family")["type_phrase"] == "babies"
    assert _detect_count_intent("how many stories have I written?")["type_phrase"] == "stories"


# ─────────────────────────────────────────────────────────────────────────────
# E2E — fresh seat, planted live staged-row shapes, the real /query walk + render
# ─────────────────────────────────────────────────────────────────────────────

LEARN_TEXTS = {
    # the live /expand beekeeping output shape, incl. the two wrong is-a edges
    "beekeeping": (
        "beekeeping (Concept) is a subclass of agriculture (Concept)\n"
        "apiary (Concept) is a subclass of beekeeping (Concept)\n"
        "queen bee (Animal) is a part of beekeeping (Concept)\n"
        "queen bee (Animal) is an instance of hive (Object)\n"
        "worker bee (Animal) is an instance of hive (Object)\n"
        "beekeeper (Person) is an instance of apiary (Concept)\n"
    ),
    # fresh domain control for the validator: all is-a edges are well-kinded
    "falconry": (
        "falconry (Concept) is a subclass of hunting (Concept)\n"
        "hawking (Concept) is a subclass of falconry (Concept)\n"
        "peregrine falcon (Animal) is a subclass of falcon (Animal)\n"
        "falcon hood (Object) is a part of falconry (Concept)\n"
    ),
}


@pytest.fixture()
def seat(monkeypatch):
    if "test" not in _DSN.lower():
        pytest.skip("no throwaway test-named POSTGRES_DSN")
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    conn = psycopg2.connect(_DSN)
    schema, status = create_user_schema(user_id, slug, conn)
    assert status == "ready", status
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.users (user_id, email, display_name, slug) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute(
            "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
            "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING", (user_id, schema))
    conn.commit()
    conn.close()
    yield user_id, schema
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for _sch in (schema, "flagent_" + user_id.replace("-", "_")):
                try:
                    cur.execute("SELECT format('DROP SCHEMA IF EXISTS %I CASCADE', %s)", (_sch,))
                    _drop = cur.fetchone()
                    if _drop and _drop[0]:
                        cur.execute(_drop[0])
                except Exception:
                    pass
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


@pytest.fixture(scope="module")
def client():
    import src.api.main as M
    from starlette.testclient import TestClient
    _mp = pytest.MonkeyPatch()  # module globals restored after this module (no suite leak)
    _mp.setattr(M, "_INGEST_ENABLED", True)

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

    _mp.setattr(M, "_idempotency_mgr", _Idem())
    import src.api.llm_calls as LC
    # the breaker is process-global: earlier modules in a whole-suite run may have opened it
    # against the unreachable test endpoint, which would fail /learn before the stub is reached
    LC.reset_circuit_breaker()

    class _FakeResp:
        status_code = 200
        _content = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": _FakeResp._content}}]}

    def _fake_post_llm_sync(client_, endpoint, **kw):
        payload = kw.get("json") or {}
        text = " ".join(m.get("content", "") for m in payload.get("messages", []))
        for topic, canned in LEARN_TEXTS.items():
            if f"'{topic}'" in text.lower():
                _FakeResp._content = canned
                return _FakeResp()
        _FakeResp._content = ""
        return _FakeResp()

    async def _fake_call_llm_async(*a, **kw):
        return {"parent": None}

    class _FakeClient:
        """/learn posts through the shared sync client directly (imported at call time)."""
        def post(self, endpoint, **kw):
            return _fake_post_llm_sync(self, endpoint, **kw)

        def close(self):  # the app's shutdown closes the shared client
            pass

    # The sync LLM POST seams: llm_source_ip.post_sync (the retry wrapper) and the shared
    # client itself (learn_topic). Replacing the client also sidesteps an earlier module's
    # TestClient shutdown having closed the process-global one.
    with patch.object(LC.llm_source_ip, "post_sync", _fake_post_llm_sync), \
            patch.object(LC, "_llm_http_client", _FakeClient()), \
            patch.object(LC, "call_llm_with_retry_async", _fake_call_llm_async):
        with TestClient(M.app) as c:
            # #121: the backend requires its service secret (auto-minted at lifespan boot).
            from src.api.backend_auth import backend_headers
            c.headers.update(backend_headers())
            from src.extraction import linguistics as _L
            t0 = time.time()
            while _L._nlp is None and time.time() - t0 < 180:
                time.sleep(0.5)
            assert _L._nlp is not None
            yield c
    _mp.undo()


def _exec(schema, sql, params=()):
    import psycopg2
    c = psycopg2.connect(_DSN)
    try:
        with c.cursor() as cur:
            cur.execute("SET search_path TO %s", (schema,))
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else None
        c.commit()
        return rows
    finally:
        c.close()


def _entity(schema, name, etype):
    eid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{schema}/{name}"))
    _exec(schema, "INSERT INTO entities (id, entity_type) VALUES (%s, %s) "
                  "ON CONFLICT DO NOTHING", (eid, etype))
    _exec(schema, "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                  "VALUES (%s, %s, true) ON CONFLICT DO NOTHING", (eid, name))
    return eid


def _learned(schema, sid, rel, oid):
    """The live /learn staged-row shape (live staged_facts rows)."""
    _exec(schema,
          "INSERT INTO staged_facts (subject_id, object_id, rel_type, fact_class, provenance,"
          " fact_provenance, confidence, unified_confidence, is_hierarchy_rel, storage_type)"
          " VALUES (%s, %s, %s, 'B', 'llm_learn', 'llm_learned', 0.8, 0.8, %s, %s)"
          " ON CONFLICT DO NOTHING",
          (sid, oid, rel, rel != "has_interest_in",
           "relational" if rel == "has_interest_in" else "hierarchical"))


def _plant_topic(schema, user_id, topic, down, up=None, taxonomy=False):
    t = _entity(schema, topic, "Concept")
    for name, etype, rel in down:
        _learned(schema, _entity(schema, name, etype), rel, t)
    for name in (up or []):
        _learned(schema, t, "subclass_of", _entity(schema, name, "Concept"))
    _learned(schema, user_id, "has_interest_in", t)
    if taxonomy:
        _exec(schema,
              "INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types,"
              " rel_types_defining_group, source) VALUES (%s, %s, %s, %s, 'engine_grown_ingest')"
              " ON CONFLICT DO NOTHING",
              (topic, "auto-grown grouping from observed part_of membership",
               ["Concept", "Object"], ["part_of"]))


def _defs(c, user_id, text):
    r = c.post("/query", json={"text": text, "user_id": user_id})
    assert r.status_code == 200, r.text[:400]
    return [str(f.get("definition") or "") for f in (r.json().get("facts") or [])]


@_E2E
def test_e2e_beekeeping_learned_ladder_renders_without_taxonomy(seat, client):
    """#31 R2b: no taxonomy row (the live beekeeping shape) — the learned rungs still speak."""
    user_id, schema = seat
    _plant_topic(schema, user_id, "beekeeping",
                 down=[("apiary", "Concept", "subclass_of"),
                       ("queen bee", "Animal", "part_of")],
                 up=["agriculture"])
    joined = " | ".join(_defs(client, user_id, "What do I know about beekeeping?")).lower()
    assert "apiary is a subclass of beekeeping" in joined, joined
    assert "queen bee is a part of beekeeping" in joined, joined
    assert "beekeeping is a subclass of agriculture" in joined, joined


@_E2E
def test_e2e_woodworking_unmeasured_topic_renders_and_does_not_leak(seat, client):
    """#31 R2a: taxonomy row present (the live woodworking shape), SemCor-unmeasured topic;
    a foreign grown topic's row reached by the membership walk must not ride along."""
    user_id, schema = seat
    _plant_topic(schema, user_id, "woodworking",
                 down=[("joinery", "Concept", "subclass_of"),
                       ("woodturning", "Concept", "subclass_of"),
                       ("woodworking tool", "Object", "part_of"),
                       ("woodworking material", "Object", "part_of")],
                 taxonomy=True)
    _plant_topic(schema, user_id, "astrophotography",
                 down=[("ccd camera", "Object", "part_of"),
                       ("star tracker", "Object", "part_of")],
                 taxonomy=True)
    defs = _defs(client, user_id, "What do I know about woodworking?")
    joined = " | ".join(defs).lower()
    assert "joinery is a subclass of woodworking" in joined, defs
    assert "woodworking tool is a part of woodworking" in joined, defs
    assert "ccd camera" not in joined and "star tracker" not in joined, defs


@_E2E
def test_e2e_fresh_domain_control_and_never_expanded(seat, client):
    """Fresh domain (bookbinding, no taxonomy) renders; a never-expanded topic stays empty."""
    user_id, schema = seat
    _plant_topic(schema, user_id, "bookbinding",
                 down=[("coptic binding", "Concept", "subclass_of"),
                       ("bone folder", "Object", "part_of")])
    joined = " | ".join(_defs(client, user_id, "What do you know about bookbinding?")).lower()
    assert "coptic binding is a subclass of bookbinding" in joined, joined
    assert not _defs(client, user_id, "What do I know about quantum embroidery?")


@_E2E
def test_e2e_learn_validator_refuses_wrong_kind_isa(seat, client):
    """#31 R2c: the live wrong edges are not staged; the well-kinded rungs are."""
    user_id, schema = seat
    r = client.post("/learn", json={"topic": "beekeeping", "user_id": user_id})
    assert r.status_code == 200 and r.json().get("status") == "learned", r.text[:300]
    rows = _exec(schema,
                 "SELECT s.alias, f.rel_type, o.alias FROM staged_facts f"
                 " JOIN entity_aliases s ON s.entity_id = f.subject_id"
                 " JOIN entity_aliases o ON o.entity_id = f.object_id"
                 " WHERE f.provenance = 'llm_learn'")
    got = {(a, r_, b) for a, r_, b in rows}
    assert ("queen bee", "instance_of", "hive") not in got, got
    assert ("worker bee", "instance_of", "hive") not in got, got
    # RESIDUAL (round-2 G3): beekeeper (Person) instance_of apiary (Concept) is now ADMITTED —
    # the object kind is a residual bucket, and the lexname heuristic that refused it was
    # removed (it refused valid edges). Deliberately NOT asserted either way.
    assert ("apiary", "subclass_of", "beekeeping") in got, got
    assert ("queen bee", "part_of", "beekeeping") in got, got


@_E2E
def test_e2e_learn_validator_fresh_domain_control(seat, client):
    user_id, schema = seat
    r = client.post("/learn", json={"topic": "falconry", "user_id": user_id})
    assert r.status_code == 200 and r.json().get("status") == "learned", r.text[:300]
    rows = _exec(schema,
                 "SELECT s.alias, f.rel_type, o.alias FROM staged_facts f"
                 " JOIN entity_aliases s ON s.entity_id = f.subject_id"
                 " JOIN entity_aliases o ON o.entity_id = f.object_id"
                 " WHERE f.provenance = 'llm_learn'")
    got = {(a, r_, b) for a, r_, b in rows}
    assert ("hawking", "subclass_of", "falconry") in got, got
    assert ("peregrine falcon", "subclass_of", "falcon") in got, got


def _plant_owned(schema, user_id, name, etype, rel="owns"):
    oid = _entity(schema, name, etype)
    _exec(schema,
          "INSERT INTO facts (subject_id, object_id, rel_type, fact_class, provenance,"
          " fact_provenance, confidence) VALUES (%s, %s, %s, 'A', 'mcp', 'user_stated', 1.0)"
          " ON CONFLICT DO NOTHING", (user_id, oid, rel))


@_E2E
def test_e2e_absent_concept_residual_class_does_not_dump(seat, client):
    """#38 item 5: an absent concept typed only to a residual bucket abstains bare; a real
    kind (animal) still gets its informative context (fresh-domain control)."""
    import src.api.main as M
    user_id, schema = seat
    for n in ("firewall rules", "sailboat", "workbench", "tomato plants"):
        _plant_owned(schema, user_id, n, "Object")
    _plant_owned(schema, user_id, "luna", "Animal", rel="has_pet")
    kinds = {"sailing": "object", "hamster": "animal", "kitesurfing": "object"}
    with patch.object(M, "get_gliner_model", return_value=object()), \
            patch.object(M, "_gliner_canonical_type",
                         side_effect=lambda t, _m: kinds.get(t)):
        sail = " | ".join(_defs(client, user_id, "What do I know about sailing?")).lower()
        ham = " | ".join(_defs(client, user_id, "What do I know about hamsters?")).lower()
        kite = " | ".join(_defs(client, user_id, "What do I know about kitesurfing?")).lower()
    for n in ("firewall rules", "workbench", "tomato plants"):
        assert n not in sail, sail
        assert n not in kite, kite  # round-2 G2: WordNet-unknown hobby word
    assert "luna" in ham and "hamster" in ham, ham


def test_residual_class_needs_lexical_kinship():
    """#38 item 5 unit: under a residual class (Object/Concept) a same-class neighbour needs
    lexical kinship with the queried noun; the pinned guitar->piano context survives."""
    from src.api.main import _residual_class_kinship
    assert _residual_class_kinship("sailing", {"sailboat"}, "object") is False
    assert _residual_class_kinship("sailing", {"object"}, "object") is False  # label != kind
    assert _residual_class_kinship("mineralogy", {"fluorite"}, "concept") is False
    assert _residual_class_kinship("guitar", {"piano"}, "object") is True
    # round-2 G2: a term WordNet does not know gives NO kind evidence under a residual
    # class -> no neighbour context (kitesurfing / pickleball / bouldering)
    assert _residual_class_kinship("qzxwvtermunknown", {"sailboat"}, "object") is False
    assert _residual_class_kinship("kitesurfing", {"sailboat"}, "concept") is False



# ─────────────────────────────────────────────────────────────────────────────
# ROUND 2 (critic gaps)
# ─────────────────────────────────────────────────────────────────────────────

@_E2E
def test_e2e_shared_parent_sibling_topic_does_not_bleed(seat, client):
    """G1: two expanded topics under one parent — the knowledge-ask about one must not
    render the sibling's tree (the chain used to seed its down-walk from ancestors)."""
    user_id, schema = seat
    _plant_topic(schema, user_id, "cheesemaking",
                 down=[("cheddar", "Concept", "subclass_of"),
                       ("cheese press", "Object", "part_of"),
                       ("rennet", "Object", "part_of")],
                 up=["food preparation"], taxonomy=True)
    _plant_topic(schema, user_id, "brewing",
                 down=[("mash tun", "Object", "part_of"),
                       ("wort chiller", "Object", "part_of")],
                 up=["food preparation"], taxonomy=True)
    defs = _defs(client, user_id, "What do I know about cheesemaking?")
    joined = " | ".join(defs).lower()
    assert "cheddar is a subclass of cheesemaking" in joined, defs
    assert "mash tun" not in joined and "wort chiller" not in joined, defs
    assert "brewing" not in joined, defs


def _type_clash(edge):
    from src.api.main import _learned_classification_type_clash
    return _learned_classification_type_clash(edge, {}, {"instance_of", "subclass_of"})


@pytest.mark.parametrize("subj,st,rel,obj,ot", [
    ("nurse", "Person", "instance_of", "profession", "Concept"),
    ("pilot", "Person", "instance_of", "occupation", "Concept"),
    ("carpenter", "Person", "instance_of", "trade", "Concept"),
    ("sourdough", "Concept", "subclass_of", "bread", "Concept"),
    ("sourdough", "Object", "subclass_of", "bread", "Object"),
    ("mocha", "Animal", "instance_of", "cat", "Animal"),
    ("apiary", "Concept", "subclass_of", "beekeeping", "Concept"),
])
def test_valid_learned_isa_edges_are_admitted(subj, st, rel, obj, ot):
    """G3: the removed lexname branch refused these valid is-a edges."""
    assert _type_clash({"subject": subj, "subject_type": st, "rel_type": rel,
                        "object": obj, "object_type": ot}) is None


def test_kind_clash_still_refuses_queen_bee_hive():
    """G3 control: the concrete-kind branch stays — Animal is never an instance of Object."""
    why = _type_clash({"subject": "queen bee", "subject_type": "Animal",
                       "rel_type": "instance_of", "object": "hive", "object_type": "Object"})
    assert why and why.startswith("kind_clash"), why
    # part_of is never judged
    assert _type_clash({"subject": "queen bee", "subject_type": "Animal",
                        "rel_type": "part_of", "object": "hive", "object_type": "Object"}) is None


@_E2E
def test_e2e_learn_facts_door_is_type_checked_at_ingest(seat, client):
    """G3b: the MCP learn_facts tool POSTs its parsed edges straight to /ingest with
    source=llm_learn — the type check must run at that seam. Body built by the tool's own
    parser (server._parse_ontological_statements), exactly as the tool sends it."""
    from src.mcp.server import _parse_ontological_statements
    user_id, schema = seat
    text = ("queen bee (Animal) is an instance of hive (Object)\n"
            "smoker (Object) is a part of apiculture (Concept)\n"
            "drone bee (Animal) is a subclass of honey bee (Animal)\n")
    edges = _parse_ontological_statements(text)
    assert len(edges) == 3, edges
    r = client.post("/ingest", json={"text": text, "user_id": user_id, "edges": edges,
                                     "source": "llm_learn"})
    assert r.status_code == 200, r.text[:300]
    rows = _exec(schema,
                 "SELECT s.alias, f.rel_type, o.alias FROM staged_facts f"
                 " JOIN entity_aliases s ON s.entity_id = f.subject_id"
                 " JOIN entity_aliases o ON o.entity_id = f.object_id"
                 " UNION SELECT s.alias, f.rel_type, o.alias FROM facts f"
                 " JOIN entity_aliases s ON s.entity_id = f.subject_id"
                 " JOIN entity_aliases o ON o.entity_id = f.object_id")
    got = {(a, r_, b) for a, r_, b in rows}
    assert ("queen bee", "instance_of", "hive") not in got, got
    assert ("smoker", "part_of", "apiculture") in got, got
    assert ("drone bee", "subclass_of", "honey bee") in got, got



@_E2E
def test_e2e_scopeless_non_user_anchor_surfaces_its_own_row(seat, client):
    """#39: a scopeless NON-user anchor hits the safety fallback (fetch_all_details=True) but
    kept the structural-only `_direct_scope_rels`, so its non-structural 1-hop row was
    projected out. Shape from the issue's smoke: (figlia, chiamare, anna), a grown novel rel.
    Control: the user-anchor default scope ({pref_name, has_interest_in}) is unchanged — a
    scopeless user ask still does NOT firehose the user's owned rows."""
    user_id, schema = seat
    d = _entity(schema, "figlia", "Person")
    a = _entity(schema, "anna", "Person")
    _exec(schema,
          "INSERT INTO rel_types (rel_type, label, head_types, tail_types, category, source,"
          " engine_generated) VALUES ('chiamare', 'chiamare', '{ANY}', '{ANY}',"
          " 'pending_placement', 'engine', true) ON CONFLICT DO NOTHING")
    _exec(schema,
          "INSERT INTO facts (subject_id, object_id, rel_type, fact_class, provenance,"
          " fact_provenance, confidence) VALUES (%s, %s, 'chiamare', 'A', 'mcp',"
          " 'user_stated', 1.0) ON CONFLICT DO NOTHING", (d, a))
    joined = " | ".join(_defs(client, user_id, "What about figlia?")).lower()
    assert "anna" in joined, joined
    # the seam itself: a scopeless path on the non-user anchor returns its 1-hop row
    import src.api.main as M
    from src.api.models import QueryPath
    rows = M.fetch_facts_from_anchor(d, user_id, QueryPath())
    assert any(str(r.get("rel_type")) == "chiamare" for r in rows), rows
    # control: the USER-anchor default scope is unchanged — a scopeless path on the user
    # installs {pref_name, has_interest_in} and does NOT firehose the owned rows
    _plant_owned(schema, user_id, "sailboat", "Object")
    _learned(schema, user_id, "has_interest_in", _entity(schema, "knitting", "Concept"))
    up = QueryPath()
    urows = M.fetch_facts_from_anchor(user_id, user_id, up)
    assert up.relationship_rels == ["has_interest_in"] and up.scalar_rels == ["pref_name"], up
    assert not up.fetch_all_details, up
    assert not any(str(r.get("rel_type")) == "owns" for r in urows), urows

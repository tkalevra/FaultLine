"""Issue #52 — a measure is a scalar in every store and lane.

Live turns: ``My canoe is 4 meters long.`` → ``Correction: my canoe is 5 meters long, not 4.`` →
``How long is my canoe?``. Measured from the live log (04:43, deploy #19), the three defects
have ONE lineage, and it starts in the correction turn, not the first one:

1. The live atomizer split the contrast into two atoms, ``My canoe is 5 meters long.`` and
   ``My canoe is not 4 meters long.``. The negated measure edge hit the ``_edge_is_scalar``
   negation firewall (a negation is routed to ``facts``, where polarity is representable), so the
   QUANTITY ``4 meters`` was resolved as an OBJECT and minted as an entity:
   ``(canoe, length, <entity "4 meters">)``. A quantity is never an entity.
2. The negation-retire block then retired the live ``length`` attribute WITHOUT comparing values
   — so ``not 4 meters`` retired the ``5 meters`` the same turn had just asserted. ``correct_fact``
   then upserted the correction LLM's bare ``5`` (the LLM drops the unit), un-retiring the slot
   with the unit gone.
3. ``How long is my canoe?``: the measure-interrogative admission only admits a value carrying a
   unit, so ``5`` admitted nothing; the degree word stayed in the keyword set and in the
   entity-centric expansion words, so the unscoped fetch-all dragged in every pre-#36
   ``has_state long`` junk row ("Sailboat is Long / Workbench is Long").

Fresh-domain controls (shed/wide, dog/tall) prove the mechanism is grammatical, not lexical.
The E2E drives the REAL harvest → /ingest → /retract/correct → /query on a throwaway ``*_test``
tenant with the production post-pass flags; the atomizer and the correction LLM are the only
substitutions, both stubbed to the LIVE shapes captured in the live log.
"""
import pytest

from tests.test_smoke_ingest_35_36_37 import (  # noqa: F401 — e2e is a module fixture
    L, _spine, _REF, e2e, _state, _ask, _rows,
    _module_flags,  # autouse: production flags for this module, undone after it
)


def _main():
    import src.api.main as M
    return M


def _t4(sentence):
    return [(f.subject, f.rel_type, (f.object or "").strip().lower(),
             getattr(f, "scalar_datatype", None)) for f in L.derive_sentence_facts(sentence, _REF)]


# ── ingest: every degree-adjective measure keeps its unit ──────────────────────────────────────

# the SEEDED rel_types metadata of a provisioned tenant (public.rel_types: height/weight are
# `quantity`, age is `integer`, duration is `duration`) — the harvest binds the tenant overlay
# around the derive loop, so the deriver reads exactly this on a live seat
_SEED_META = {"height": {"scalar_datatype": "quantity", "head_types": ["Person"],
                         "tail_types": ["SCALAR"]},
              "weight": {"scalar_datatype": "quantity", "head_types": ["Person"],
                         "tail_types": ["SCALAR"]},
              "age": {"scalar_datatype": "integer", "head_types": ["ANY"],
                      "tail_types": ["SCALAR"]},
              "duration": {"scalar_datatype": "duration", "head_types": ["ANY"],
                           "tail_types": ["SCALAR"]}}


@_spine
@pytest.mark.parametrize("sentence,subj,num,unit", [
    ("My canoe is 4 meters long.", "canoe", "4", "meters"),
    ("My shed is 3 meters wide.", "shed", "3", "meters"),          # fresh domain
    ("My dog is 60 centimeters tall.", "dog", "60", "centimeters"),  # unit attr AGREES with dim
    ("He is 6 feet tall.", "he", "6", "feet"),                       # agreeing, Person
    ("The movie is 2 hours long.", "movie", "2", "hours"),           # agreeing, duration
])
def test_degree_measure_value_keeps_its_unit(monkeypatch, sentence, subj, num, unit):
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_SEED_META))
    got = [(s, r, o) for (s, r, o, _d) in _t4(sentence) if s == subj and num in o.split()]
    assert got, _t4(sentence)
    assert all(o == f"{num} {unit}" for (_s, _r, o) in got), got


@_spine
def test_bare_count_dimension_keeps_its_magnitude(monkeypatch):
    # control: a dimension whose metadata declares a bare count (age: integer) keeps the magnitude
    monkeypatch.setattr(L, "_rel_overlay_meta_map", lambda: dict(_SEED_META))
    assert ("she", "age", "62") in [(s, r, o) for (s, r, o, _d) in _t4("She is 62 years old.")]


# ── correction: the corrected value is the stated measure, unit included ───────────────────────

@_spine
@pytest.mark.parametrize("text,attr,llm_value,want", [
    ("Correction: my canoe is 5 meters long, not 4.", "length", "5", "5 meters"),
    ("Correction: my shed is 4 meters wide, not 3.", "width", "4", "4 meters"),       # fresh
    ("Correction: my canoe is 5 meters long, not 4.", "length", "5 meters", "5 meters"),  # no-op
    ("Actually my favourite colour is blue, not red.", "favourite_colour", "blue", "blue"),
    ("Correction: I am 43, not 42.", "age", "43", "43"),                             # unitless
])
def test_correction_value_recovers_the_stated_measure(text, attr, llm_value, want):
    M = _main()
    assert M._stated_measure_value(text, attr, llm_value) == want


# ── E2E: real harvest → /ingest → /retract/correct → /query ────────────────────────────────────

_ALIASES = ("SELECT a.alias FROM entity_aliases a")


def _correct(env, text, extraction):
    """One CORRECTION turn the way the MCP drives it: the intent-independent harvest
    (/harvest-spans + /ingest), then /retract/correct. The correction LLM is stubbed to the
    live extraction shape (it drops the unit: new_value='5')."""
    M = env["M"]
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(M, "_unified_correction_extraction_llm", lambda **kw: dict(extraction))
        r = env["client"].post("/retract/correct",
                               json={"text": text, "user_id": env["user_id"]})
        assert r.status_code == 200, r.text[:300]
        return r.json()
    finally:
        mp.undo()


def _live_attr(env, subj, attr):
    return _rows(env["schema"],
                 "SELECT ea.value_text FROM entity_attributes ea JOIN entity_aliases a "
                 "ON a.entity_id = ea.entity_id WHERE a.alias = %s AND ea.attribute = %s "
                 "AND ea.superseded_at IS NULL", (subj, attr))


def _live_rel_rows(env, subj, rel):
    return _rows(env["schema"],
                 "SELECT o.alias, f.polarity FROM facts f JOIN entity_aliases s "
                 "ON s.entity_id = f.subject_id JOIN entity_aliases o ON o.entity_id = f.object_id "
                 "WHERE s.alias = %s AND f.rel_type = %s AND f.superseded_at IS NULL", (subj, rel))


_CASES = [
    # (subject, degree, dim, v1, v2 (asserted in correction), rejected numeral)
    ("canoe", "long", "length", "4 meters", "5 meters", "4"),        # the smoke case
    ("shed", "wide", "width", "3 meters", "4 meters", "3"),          # fresh domain
]


@_spine
@pytest.mark.parametrize("subj,deg,dim,v1,v2,rej", _CASES)
def test_e2e_1_measure_correction_keeps_unit_and_mints_no_quantity(e2e, subj, deg, dim, v1, v2,
                                                                   rej):
    t1 = f"My {subj} is {v1} {deg}."
    _state(e2e, t1, [t1])
    assert _live_attr(e2e, subj, dim) == [(v1,)]
    num2, unit = v2.split()
    text = f"Correction: my {subj} is {v2} {deg}, not {rej}."
    # the LIVE atomizer shape (live 04:43:45 reframe.done atom_count=2): the contrast is split
    # into the asserted measure and a NEGATED restatement of the rejected one
    main_clause = text.split(": ", 1)[1]
    atoms = [f"My {subj} is {v2} {deg}.", f"My {subj} is not {rej} {unit} {deg}."]
    import src.extraction.reframe as RF
    mp = pytest.MonkeyPatch()
    try:
        async def _stub(t, u, m=None):
            use = atoms if t.strip() in (text.strip(), main_clause.strip()) else [t]
            return RF.ReframeResult(atoms=[RF.Atom(text=a, source_span=a) for a in use],
                                    used_llm=True, rejected_count=0)
        mp.setattr(RF, "reframe_to_atomic", _stub)
        mp.setattr(e2e["M"], "SPINE_DETERMINISTIC_SEGMENTATION", False, raising=False)
        h = e2e["client"].post("/harvest-spans", json={"text": text, "user_id": e2e["user_id"]})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        assert edges
        r = e2e["client"].post("/ingest", json={"text": text, "user_id": e2e["user_id"],
                                                "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
    finally:
        mp.undo()
    out = _correct(e2e, text, {
        "subject_uuid": None, "subject_name": subj, "old_rel_type": dim,
        "old_value": rej, "new_rel_type": dim, "new_value": num2, "dimension": "SCALAR",
        "confidence": 0.98, "reason": "explicit"})
    assert out.get("status") in ("corrected", "success", "valid"), out
    # (2) the corrected value keeps its unit, and it is the ONLY live value of the slot
    assert _live_attr(e2e, subj, dim) == [(v2,)], _live_attr(e2e, subj, dim)
    # (1) a quantity is never an entity: no relational row carries the measure, no alias names it
    assert not _live_rel_rows(e2e, subj, dim), _live_rel_rows(e2e, subj, dim)
    aliases = {a for (a,) in _rows(e2e["schema"], _ALIASES)}
    assert not ({v1, f"{rej} {unit}"} & aliases), sorted(aliases)
    # (3) the question answers the corrected measure
    got = _ask(e2e, f"How {deg} is my {subj}?")
    assert any(v2 in o for (_s, _r, o) in got), got
    assert not any(o in (deg, v1) for (_s, _r, o) in got), got


@_spine
def test_e2e_1b_agreeing_measure_correction_keeps_unit(e2e):
    """Fresh domain where the unit's attribute AGREES with the degree word (tall → height)."""
    t1 = "My dog is 60 centimeters tall."
    _state(e2e, t1, [t1])
    text = "Correction: my dog is 65 centimeters tall, not 60."
    _state(e2e, text, ["My dog is 65 centimeters tall.", "My dog is not 60 centimeters tall."])
    _correct(e2e, text, {
        "subject_uuid": None, "subject_name": "dog", "old_rel_type": "height",
        "old_value": "60", "new_rel_type": "height", "new_value": "65", "dimension": "SCALAR",
        "confidence": 0.98, "reason": "explicit"})
    live = _rows(e2e["schema"],
                 "SELECT ea.attribute, ea.value_text FROM entity_attributes ea JOIN entity_aliases a "
                 "ON a.entity_id = ea.entity_id WHERE a.alias = 'dog' AND ea.superseded_at IS NULL")
    assert [v for (_a, v) in live if "65" in v] == ["65 centimeters"], live
    assert not [v for (_a, v) in live if "60" in v.split()], live
    aliases = {a for (a,) in _rows(e2e["schema"], _ALIASES)}
    assert not ({"60 centimeters", "65 centimeters", "60", "65"} & aliases), sorted(aliases)
    got = _ask(e2e, "How tall is my dog?")
    assert any("65 centimeters" in o for (_s, _r, o) in got), got


@_spine
def test_e2e_1c_negated_agreeing_measure_retires_only_its_value(e2e):
    """A negated measure on a Person-scoped dimension whose value carries no per-edge datatype
    (the agreeing emit rides `height`'s own quantity metadata): the negation retires the value it
    names, never another one, and its quantity never becomes an entity."""
    _state(e2e, "My brother is 180 centimeters tall.", ["My brother is 180 centimeters tall."])
    got = _rows(e2e["schema"],
                "SELECT ea.attribute, ea.value_text FROM entity_attributes ea JOIN entity_aliases a "
                "ON a.entity_id = ea.entity_id WHERE a.alias = 'brother' "
                "AND ea.superseded_at IS NULL")
    assert ("height", "180 centimeters") in got, got
    # a negation of a DIFFERENT value retires nothing
    _state(e2e, "My brother is not 170 centimeters tall.",
           ["My brother is not 170 centimeters tall."])
    got = _rows(e2e["schema"],
                "SELECT ea.value_text FROM entity_attributes ea JOIN entity_aliases a "
                "ON a.entity_id = ea.entity_id WHERE a.alias = 'brother' AND ea.attribute = 'height' "
                "AND ea.superseded_at IS NULL")
    assert got == [("180 centimeters",)], got
    # the negation of THE value retires it
    _state(e2e, "My brother is not 180 centimeters tall.",
           ["My brother is not 180 centimeters tall."])
    got = _rows(e2e["schema"],
                "SELECT ea.value_text FROM entity_attributes ea JOIN entity_aliases a "
                "ON a.entity_id = ea.entity_id WHERE a.alias = 'brother' AND ea.attribute = 'height' "
                "AND ea.superseded_at IS NULL")
    assert got == [], got
    aliases = {a for (a,) in _rows(e2e["schema"], _ALIASES)}
    assert not ({"170 centimeters", "180 centimeters"} & aliases), sorted(aliases)


@_spine
def test_e2e_2_degree_word_binds_the_dimension_on_a_junk_seat(e2e):
    """The seat carries every pre-fix wound at once: ``has_state long`` junk nodes on other
    subjects, the negated relational ``(raft, length, <entity "4 meters">)`` row, and the
    unit-dropped attribute ``length = 5`` — exactly the live canoe seat after the correction."""
    c = e2e["client"]
    t1 = "My raft is 4 meters long."
    _state(e2e, t1, [t1])
    r = c.post("/ingest", json={"text": "legacy junk", "user_id": e2e["user_id"], "source": "mcp",
                                "edges": [{"subject": s, "rel_type": "has_state", "object": "long"}
                                          for s in ("sailboat", "workbench")]})
    assert r.status_code == 200, r.text[:300]
    import psycopg2
    from tests.test_smoke_ingest_35_36_37 import _DSN
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {e2e['schema']}")
            cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = 'raft'")
            (raft,) = cur.fetchone()
            junk = "00000000-0000-5000-8000-00000000052a"
            cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'unknown') "
                        "ON CONFLICT DO NOTHING", (junk,))
            cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                        "VALUES (%s, '4 meters', true) ON CONFLICT DO NOTHING", (junk,))
            cur.execute("INSERT INTO facts (subject_id, object_id, rel_type, "
                        "fact_provenance, fact_class, polarity) VALUES "
                        "(%s, %s, 'length', 'user_stated', 'A', 'negated')", (raft, junk))
            cur.execute("UPDATE entity_attributes SET value_text = '5', value_float = NULL, "
                        "unit = NULL WHERE entity_id = %s AND attribute = 'length'", (raft,))
        conn.commit()
    finally:
        conn.close()
    M = e2e["M"]
    events = []
    real = M.log.info

    def _spy(event, *a, **kw):
        events.append((event, kw))
        return real(event, *a, **kw)
    mp = pytest.MonkeyPatch()
    mp.setattr(M.log, "info", _spy)
    try:
        got = _ask(e2e, "How long is my raft?")
    finally:
        mp.undo()
    assert any(r == "length" and o == "5" for (_s, r, o) in got), got
    assert not any(o == "long" for (_s, _r, o) in got), got      # no fetch-all junk
    # the RESOLUTION, not just the render (the in-process topic gate happens to hide the junk
    # rows the live render showed): the degree word binds the dimension — scope active on
    # `length`, the degree word is no keyword, and no entity-centric expansion of "long"
    dp = [kw for (ev, kw) in events if ev == "determine_path" and "raft" in kw.get("query", "")]
    assert dp and dp[-1].get("scope_active") is True, dp
    assert "length" in (dp[-1].get("scalar_rels") or []), dp
    assert "long" not in (dp[-1].get("keywords") or []), dp
    assert not [kw for (ev, kw) in events if ev == "query.phase5.entity_centric_expanded"], events
    # control: the fresh measure on another subject still answers through its unit
    t2 = "My dinghy is 3 meters wide."
    _state(e2e, t2, [t2])
    got = _ask(e2e, "How wide is my dinghy?")
    assert any("3 meters" in o for (_s, _r, o) in got), got
    assert not any(o == "long" for (_s, _r, o) in got), got


@_spine
def test_determine_path_drops_the_degree_word_from_keywords(monkeypatch):
    """The #48 degree-word drop lived only in the anchor resolver; determine_path kept the degree
    word as a keyword (live: ``keywords=['canoe', 'long']``) and resolved scope off it."""
    M = _main()
    seen = {}
    real = M.log.info

    def _spy(event, **kw):
        if event == "determine_path":
            seen.update(kw)
        return real(event, **kw)
    monkeypatch.setattr(M.log, "info", _spy)

    class _C:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _D:
        def cursor(self):
            return _C()

    M.determine_path("How long is my canoe?", _D())
    assert "canoe" in (seen.get("keywords") or []), seen
    assert "long" not in (seen.get("keywords") or []), seen

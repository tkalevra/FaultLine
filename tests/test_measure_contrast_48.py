"""Issue #48 — measure contrast + measure interrogative.

(a) "Correction: my kayak is 16 feet long, not 14." also filed (user, kayak, "feet long 14").
    Root cause: the possessive-preference seam (``analyze_possessive_predication``) declines a
    MEASURED predicate only when the complement is POS ``ADJ``; in the contrast sentence spaCy tags
    the degree word "long" ``ADV`` (acomp), so the seam read the whole measure + the REJECTED
    numeral as a preference value. The dependency (acomp) is the grammar; a dependent carrying its
    own ``neg`` is the rejected half of a contrast and never part of a value phrase.
(b) "How long is my kayak?" anchored on a pre-#36 junk entity named ``long`` (sailboat/workbench
    ``has_state long``) and never rendered ``length = 16 feet``. Root cause: ``resolve_anchor``'s
    n-gram lane scans left-to-right, so the degree word (1-gram "long") beat "kayak". The degree
    word of a measure interrogative is the ASPECT, never a referent; ``determine_path`` binds it
    to its dimension through the same WordNet attribute pointer ingest used to name the stored
    attribute.

Fresh-domain controls (canoe / trench / shed / pond) prove the mechanism is grammatical, not
lexical. The E2E plants the pre-fix junk nodes through the real writer, then asks.
"""
import pytest

from tests.test_smoke_ingest_35_36_37 import (  # noqa: F401 — e2e is a module fixture
    L, _spine, _harvest_one, e2e, _state, _ask, _rows,
    _module_flags,  # autouse: production flags for this module, undone after it
)


# ── (a) the rejected numeral of a measure contrast is never a preference value ─────────────────

_CONTRASTS = [
    "my kayak is 16 feet long, not 14.",        # the smoke sentence
    "my canoe is 12 feet long, not 10.",        # fresh domain
    "my trench is 4 meters long, not 3.",       # fresh domain, metric
]


@_spine
@pytest.mark.parametrize("sentence", _CONTRASTS)
def test_measure_contrast_is_not_a_preference(sentence):
    # guard: the parse under test really is the POS-unstable shape (acomp tagged ADV)
    deg = next(t for t in L._parse(sentence) if t.text == "long")
    assert deg.dep_ == "acomp", [(t.text, t.dep_, t.pos_) for t in L._parse(sentence)]
    assert L.analyze_possessive_predication(sentence) is None


@_spine
@pytest.mark.parametrize("text,subj,good", [
    ("Correction: my kayak is 16 feet long, not 14.", "kayak", "16 feet"),
    ("Correction: my canoe is 12 feet long, not 10.", "canoe", "12 feet"),   # fresh domain
])
def test_harvest_files_only_the_asserted_measure(monkeypatch, text, subj, good):
    edges = _harvest_one(monkeypatch, text)
    assert (subj, "length", good) in edges, edges
    # no rel named after the NP, no value carrying the rejected numeral
    assert not any(r == subj for (_s, r, _o) in edges), edges
    rejected = text.split("not ")[1].strip(". ")
    assert not any(rejected in (o or "").split() for (_s, _r, o) in edges), edges


@_spine
@pytest.mark.parametrize("sentence", [
    "my shed is 3 meters tall, not 2.",          # tall variant (ADJ tag) — already declined
    "my pond is 2 meters deep, not 3.",          # deep variant
    "My kayak is 14 feet long.",                 # plain measure, no contrast
])
def test_measure_controls_stay_declined(sentence):
    assert L.analyze_possessive_predication(sentence) is None


@_spine
def test_plain_preferences_unchanged():
    pp = L.analyze_possessive_predication("my favourite colour is blue.")
    assert pp is not None and pp.possessed == "favourite colour" and pp.value == "blue"
    # an asserted adjective contrast is still read negated (the deriver owns the asserted value)
    pp = L.analyze_possessive_predication("my tent is blue, not orange.")
    assert pp is not None and pp.negated
    # a bare (unmeasured) degree predicate keeps today's reading
    pp = L.analyze_possessive_predication("my kayak is long.")
    assert pp is not None and pp.value == "long"


@_spine
def test_feeling_seam_measure_guard_reads_the_dependency():
    # the same POS-only gate guarded the self-predication seam; a measured acomp is never a feeling
    tok = next(t for t in L._parse("my kayak is 16 feet long, not 14.") if t.text == "long")
    assert L._adj_has_numeric_measure(tok)
    tok = next(t for t in L._parse("I am sad.") if t.text == "sad")
    assert not L._adj_has_numeric_measure(tok)


# ── (b) the degree word is the aspect, never the anchor ────────────────────────────────────────

def _main():
    import src.api.main as M
    return M


@_spine
@pytest.mark.parametrize("q,word", [
    ("How long is my kayak?", "long"),
    ("How tall is my shed?", "tall"),
    ("How deep is my pond?", "deep"),
    ("How big does the blue-ringed octopus get?", "big"),
    ("How many dogs do I have?", None),
    ("What is my kayak's name?", None),
])
def test_measure_interrogative_degree_word(q, word):
    M = _main()
    assert M._measure_interrogative_degree(q) == word
    assert M._is_measure_interrogative(q) is (word is not None)


def test_degree_dimensions_meet_the_ingest_name():
    from src.api.wordnet_ladder import degree_adjective_dimension, degree_adjective_dimensions
    for adj, unit_attr in (("long", "height"), ("tall", "height"), ("wide", "height"),
                           ("deep", "height")):
        stored = degree_adjective_dimension(adj, unit_attr, {"height", "age"})
        assert stored in degree_adjective_dimensions(adj), (adj, stored)
    assert degree_adjective_dimensions("xyzzy") == frozenset()


class _Cur:
    def __init__(self, table):
        self.table, self.row = table, None

    def execute(self, sql, params=()):
        self.row = None
        if "FROM entity_aliases" in sql and params:
            hit = self.table.get(params[0])
            self.row = (hit,) if hit else None

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _DB:
    def __init__(self, table):
        self.table = table

    def cursor(self):
        return _Cur(self.table)


@_spine
@pytest.mark.parametrize("q,good", [
    ("How long is my kayak?", "kayak"),
    ("How long is my canoe?", "canoe"),        # fresh domain
    ("How tall is my shed?", "shed"),          # tall variant
    ("How deep is my pond?", "pond"),          # deep variant
])
def test_degree_word_never_anchors_on_a_junk_node(monkeypatch, q, good):
    """The seat carries pre-#36 junk nodes named after every degree word."""
    M = _main()
    table = {"long": "junk-long", "tall": "junk-tall", "deep": "junk-deep",
             "kayak": "e-kayak", "canoe": "e-canoe", "shed": "e-shed", "pond": "e-pond"}
    monkeypatch.setattr(M, "_get_semantic_word_mappings", lambda db, w: {"category": "entity"})
    monkeypatch.setattr(M, "_entity_is_l4_place", lambda db, e: False)
    monkeypatch.setattr(M, "resolve_entity_synonym", lambda *a, **k: None)
    got = M.resolve_anchor(q, [], "seat-1", _DB(table))
    assert got == table[good], (q, got)


# ── E2E: real harvest → /ingest → /query, pre-fix junk planted through the real writer ─────────

_JUNK = [{"subject": s, "rel_type": "has_state", "object": o}
         for s, o in (("sailboat", "long"), ("workbench", "long"), ("ladder", "tall"),
                      ("well", "deep"))]


def _plant(env):
    c = env["client"]
    r = c.post("/ingest", json={"text": "legacy junk", "user_id": env["user_id"],
                                "source": "mcp", "edges": _JUNK})
    assert r.status_code == 200, r.text[:300]
    rows = _rows(env["schema"], "SELECT a.alias FROM facts f JOIN entity_aliases a "
                                "ON a.entity_id = f.object_id WHERE f.rel_type = 'has_state'")
    assert {"long", "tall", "deep"} <= {a for (a,) in rows}, rows


@_spine
def test_e2e_1_degree_word_binds_dimension_on_a_junk_seat(e2e):
    _state(e2e, "My kayak is named Otter and it is 14 feet long.",
           ["My kayak is named Otter.", "My kayak is 14 feet long."])
    _state(e2e, "My kayak is 60 centimeters wide.", ["My kayak is 60 centimeters wide."])
    _state(e2e, "My canoe is 4 meters long.", ["My canoe is 4 meters long."])   # fresh domain
    _state(e2e, "My pond is 2 meters deep.", ["My pond is 2 meters deep."])     # deep variant
    _plant(e2e)
    got = _ask(e2e, "How long is my kayak?")
    assert any("14 feet" in o for (_s, _r, o) in got), got
    assert not any(o == "long" for (_s, _r, o) in got), got
    assert not any("60 centimeters" in o for (_s, _r, o) in got), got   # length, not width
    got = _ask(e2e, "How wide is my kayak?")
    assert any("60 centimeters" in o for (_s, _r, o) in got), got
    assert not any("14 feet" in o for (_s, _r, o) in got), got
    got = _ask(e2e, "How long is my canoe?")
    assert any("4 meters" in o for (_s, _r, o) in got), got
    assert not any(o == "long" for (_s, _r, o) in got), got
    got = _ask(e2e, "How deep is my pond?")
    assert any("2 meters" in o for (_s, _r, o) in got), got
    assert not any(o == "deep" for (_s, _r, o) in got), got


@_spine
def test_e2e_2_measure_contrast_files_no_junk(e2e):
    # runs on the seat test 1 built (kayak 14 feet + planted junk); the correction turn's harvest
    # (the MCP intent-independent lane) must file the asserted value and nothing else
    text = "Correction: my kayak is 16 feet long, not 14."
    edges = _state(e2e, text, [text])
    assert not any(e.get("rel_type") == "kayak" for e in edges), edges
    junk = _rows(e2e["schema"], "SELECT rel_type FROM facts WHERE rel_type IN ('kayak', 'canoe')")
    assert not junk, junk
    attrs = dict(_rows(e2e["schema"], "SELECT value_text, attribute FROM entity_attributes "
                                      "WHERE superseded_at IS NULL"))
    assert attrs.get("16 feet") == "length", attrs
    got = _ask(e2e, "How long is my kayak?")
    assert any("16 feet" in o for (_s, _r, o) in got), got
    assert not any(o == "long" or "feet long" in o for (_s, _r, o) in got), got


# ── (c) the one-shot repair helper (never auto-run) ────────────────────────────────────────────

@_spine
def test_e2e_3_repair_retires_degree_state_twins_only(e2e):
    import psycopg2
    from tests.test_smoke_ingest_35_36_37 import _DSN
    from src.api.junk_repair import retire_degree_state_junk
    _state(e2e, "My sailboat is 28 feet long.", ["My sailboat is 28 feet long."])
    r = e2e["client"].post("/ingest", json={
        "text": "control", "user_id": e2e["user_id"], "source": "mcp",
        "edges": [{"subject": "kayak", "rel_type": "has_state", "object": "wet"}]})
    assert r.status_code == 200, r.text[:300]

    def _run(apply):
        conn = psycopg2.connect(_DSN)
        try:
            got = retire_degree_state_junk(conn, e2e["schema"], apply=apply)
            conn.commit()
            return got
        finally:
            conn.close()

    dry = _run(False)
    # sailboat holds length → its 'long' state is a twin; workbench/ladder/well hold no measure
    assert [(c["degree"], c["dimension"]) for c in dry] == [("long", "length")], dry
    assert _run(False) == dry                            # a dry run writes nothing
    assert _run(True) == dry
    assert _run(False) == [], "not idempotent"          # re-run touches nothing
    live = _rows(e2e["schema"], "SELECT a.alias FROM facts f JOIN entity_aliases a "
                                "ON a.entity_id = f.object_id WHERE f.rel_type = 'has_state' "
                                "AND f.superseded_at IS NULL")
    names = sorted(a for (a,) in live)
    assert "wet" in names, names                        # non-twin control survives
    assert names.count("long") == 1, names              # workbench (no measure held) survives
    got = _ask(e2e, "How long is my sailboat?")
    assert any("28 feet" in o for (_s, _r, o) in got), got
    with pytest.raises(ValueError):
        retire_degree_state_junk(None, "public; DROP TABLE facts")

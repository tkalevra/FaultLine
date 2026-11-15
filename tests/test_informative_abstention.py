"""INFORMATIVE ABSTENTION via the deterministic L4 walk (LME qid 0862e8bf_abs cluster).

Problem (2 sentences): On a specific-entity MISS ("What is the name of my hamster?") the system
correctly abstained but only said a bare "No relevant facts found" — accurate yet uninformative.
The gold answer surfaces the REAL neighbour ("you mentioned your cat Luna") plus the SPECIFIC
absence ("but not your hamster"), which is a deterministic L4 type-walk, not fuzzy matching.

Fix (2 sentences): On the scoped-absent-concept abstention, `_fetch_informative_abstention`
grounds the queried noun's TYPE via GLiNER2 (hamster -> animal), climbs to that type's L4 place
scoped to the user, and WALKS it — surfacing every real neighbour whose own L4 type-signals land
in the SAME class (cat Luna) as CONTEXT, then states the specific grounded absence. The type match
is independent GLiNER2 classification of each side into the same ontology class (NOT a cosine
hamster<->cat similarity), so Luna is surfaced as context while we REFUSE to call her the hamster,
and a queried type with no grounded neighbour falls back to the honest bare abstention.

These tests exercise `_fetch_informative_abstention` directly against a fake GLiNER2 model + fake
DB (the heavy hierarchy/prose seams — `_resolve_type_signals`, `_get_classification_rels`,
`convert_to_prose` — are monkeypatched with canned per-entity fixtures, since they are covered by
their own tests). Two DIFFERENT domains (pet + non-pet) prove subject-agnosticity; the negative
guards prove no-fabrication and the honest fallback.
"""
import os

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import pytest

import src.api.main as main
from src.api.main import (
    _fetch_informative_abstention,
    _gliner_canonical_type,
    _query_scoped_to_absent_concept,
)

USER = "user-uuid-abc"

# 36-char UUID-shaped object ids for the user's neighbours.
LUNA = "11111111-1111-1111-1111-111111111111"   # a cat  (animal)
CAR = "22222222-2222-2222-2222-222222222222"    # a Honda (object)
SISTER = "33333333-3333-3333-3333-333333333333"  # a person
PIANO = "44444444-4444-4444-4444-444444444444"   # an instrument (object)

# The user's RELATIONAL neighbour edges (rel_type, object_id, fact_class, confidence, prov).
_NEIGHBOUR_ROWS = {
    "pets": [
        ("has_pet", LUNA, "A", 1.0, "user_stated"),
        ("owns", CAR, "B", 0.8, "user_stated"),
    ],
    "instruments": [
        ("owns", PIANO, "B", 0.8, "user_stated"),
        ("sibling_of", SISTER, "A", 1.0, "user_stated"),
    ],
    "person_only": [
        ("sibling_of", SISTER, "A", 1.0, "user_stated"),
    ],
}

# Each entity's L4 type-signals (what `_resolve_type_signals` would return) — the MOST-specific
# class word its instance_of/subclass_of ladder grounds to.
_TYPE_SIGNALS = {
    LUNA: {"cat"},
    CAR: {"car"},
    SISTER: {"sister", "woman"},
    PIANO: {"piano"},
}

# The fake GLiNER2 canonical-class map (independent typing of each noun into a top-level class).
_GLINER_CLASS = {
    "hamster": "Animal", "cat": "Animal",
    "guitar": "Object", "piano": "Object", "car": "Object", "honda": "Object",
    "sister": "Person", "woman": "Person",
}


class _FakeGliner:
    def extract_entities(self, text, labels, **kwargs):
        cls = _GLINER_CLASS.get((text or "").strip().lower())
        if not cls:
            return {"entities": {}}
        return {"entities": {cls: [text.strip().lower()]}}


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = sql.lower()
        if "from facts" in s and "subject_id" in s:
            self._result = list(self._rows)
        elif "from entities" in s:
            self._result = []  # not hit — _resolve_type_signals is monkeypatched to supply signals
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


@pytest.fixture
def patched(monkeypatch):
    """Wire the fake GLiNER2 + canned hierarchy/prose seams."""
    monkeypatch.setattr(main, "get_gliner_model", lambda: _FakeGliner())
    monkeypatch.setattr(main, "_get_classification_rels",
                        lambda db=None: frozenset({"instance_of", "subclass_of", "is_a"}))
    monkeypatch.setattr(main, "_resolve_type_signals",
                        lambda db, oid, max_depth=4: (set(_TYPE_SIGNALS.get(oid, set())), True))

    def _fake_prose(facts, db, anchor=None, user_id=None, **kw):
        # Render each matched neighbour as grounded "you have …" prose keyed on object_id.
        _names = {LUNA: "Luna", CAR: "a Honda", PIANO: "a piano", SISTER: "your sister"}
        out = []
        for f in facts:
            oid = f.get("object_id") or f.get("_object_id")
            name = _names.get(oid, oid)
            out.append(f"You have {name}" if name and name[0].islower()
                       else f"You have a pet named {name}")
        return out

    monkeypatch.setattr(main, "convert_to_prose", _fake_prose)
    return monkeypatch


# ── PET domain: absent hamster surfaces the real cat Luna + the specific absence ────────────
def test_pet_domain_surfaces_sibling_and_absence(patched):
    out = _fetch_informative_abstention(
        "What is the name of my hamster?", {"hamster"}, _FakeDB(_NEIGHBOUR_ROWS["pets"]),
        USER, schema_name=None)
    assert out is not None, "should produce an informative abstention (cat neighbour exists)"
    d = out["definition"]
    assert "Luna" in d                                  # the REAL grounded neighbour surfaced
    assert "have not mentioned a hamster" in d.lower()  # the SPECIFIC deterministic absence
    # BOUNDED: the type-mismatched car neighbour (Object) is NOT surfaced for a pet (Animal) ask.
    assert "honda" not in d.lower()
    # ASSERT voice (grounded context is user truth).
    assert out.get("fact_provenance") == "user_stated"
    assert out.get("rel_type") != "context"  # must take the MCP `definition` render branch


# ── NON-PET domain (subject-agnostic): absent guitar surfaces the real piano ─────────────────
def test_non_pet_domain_generalizes(patched):
    out = _fetch_informative_abstention(
        "What brand is my guitar?", {"guitar"}, _FakeDB(_NEIGHBOUR_ROWS["instruments"]),
        USER, schema_name=None)
    assert out is not None
    d = out["definition"]
    assert "piano" in d.lower()                          # same-class (Object) neighbour surfaced
    assert "have not mentioned a guitar" in d.lower()
    # BOUNDED: the Person sibling is NOT surfaced for an Object ask.
    assert "sister" not in d.lower()


# ── NEGATIVE GUARD A: no grounded neighbour of the queried type → honest bare abstention ─────
def test_no_matching_neighbour_falls_back_to_bare(patched):
    out = _fetch_informative_abstention(
        "What is the name of my hamster?", {"hamster"},
        _FakeDB(_NEIGHBOUR_ROWS["person_only"]), USER, schema_name=None)
    assert out is None, "no animal neighbour → do NOT invent context; bare abstention stands"


# ── NEGATIVE GUARD B: the queried entity is NEVER echoed as a stored row ─────────────────────
def test_never_echoes_queried_entity_as_stored(patched):
    out = _fetch_informative_abstention(
        "What is the name of my hamster?", {"hamster"}, _FakeDB(_NEIGHBOUR_ROWS["pets"]),
        USER, schema_name=None)
    assert out is not None
    d = out["definition"].lower()
    # 'hamster' may appear ONLY inside the explicit absence clause — never as "your hamster is …"
    # or "a hamster named …" (the archived anti-goal). Strip the sanctioned clause and assert the
    # word is gone.
    residue = d.replace("have not mentioned a hamster", "")
    assert "hamster" not in residue


# ── FAIL-SAFE: no model / empty terms / no db → None (never fabricate) ───────────────────────
def test_failsafe_paths(monkeypatch):
    monkeypatch.setattr(main, "get_gliner_model", lambda: None)
    assert _fetch_informative_abstention("q", {"hamster"}, _FakeDB([]), USER, None) is None
    monkeypatch.setattr(main, "get_gliner_model", lambda: _FakeGliner())
    assert _fetch_informative_abstention("q", set(), _FakeDB([]), USER, None) is None
    assert _fetch_informative_abstention("q", {"hamster"}, None, USER, None) is None


# ── canonical typing: independent GLiNER2 class, not a string match ──────────────────────────
def test_gliner_canonical_type(patched):
    g = _FakeGliner()
    assert _gliner_canonical_type("hamster", g) == "animal"
    assert _gliner_canonical_type("cat", g) == "animal"      # SAME class → the moat
    assert _gliner_canonical_type("guitar", g) == "object"
    assert _gliner_canonical_type("nonsense-xyz", g) is None
    assert _gliner_canonical_type("hamster", None) is None


# ── the gate now exposes the absent probe token for the informative walk ─────────────────────
def test_gate_probe_out_populates_absent_token():
    from src.extraction.linguistics import linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy en_core_web_sm required")

    _ALIASES = {"luna"}
    _REL_ROWS = [("has_pet", "has pet"), ("pref_name", "preferred name"), ("age", "age")]

    class _GateCursor:
        def __init__(self):
            self._result = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            s = sql.lower()
            if "from entity_aliases" in s:
                toks = set(params[1]) if params and len(params) > 1 else set()
                self._result = [(1,)] if (toks & _ALIASES) else []
            elif "from rel_types" in s:
                self._result = list(_REL_ROWS)
            elif "from entity_attributes" in s:
                self._result = [("age",)]
            elif "from entity_taxonomies" in s:
                self._result = [("pets",)]
            else:
                self._result = []

        def fetchone(self):
            return self._result[0] if self._result else None

        def fetchall(self):
            return list(self._result)

    class _GateDB:
        def cursor(self):
            return _GateCursor()

    probe: set = set()
    fired = _query_scoped_to_absent_concept(
        "What is the name of my hamster?", _GateDB(), USER, probe_out=probe)
    assert fired is True
    assert "hamster" in probe
    assert "name" not in probe  # 'name' is an ontology slot word, never an absent entity

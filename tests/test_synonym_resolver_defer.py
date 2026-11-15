"""Behavioral tests for ``resolve_entity_synonym`` — the QUERY-side synonym
resolver (IMPL-3 READ half) and its precision contract.

THE CONTRACT UNDER TEST (post density-tiebreak removal):

  • single candidate            → resolve to it          (strengthen preserved)
  • §11 type filter → exactly 1 → resolve to that one    (deterministic discriminator)
  • >1 candidate, no discrim.   → return None (DEFER)    ← THE BEHAVIOR CHANGE
  • previously equal-density tie → still None             (subsumed by the DEFER rule)

The resolver does PURE SQL + one-hop ontology lookup — no GLiNER2, no LLM, no
embeddings, no fuzzy/substring match, and (now) NO popularity/graph-density guess.
A mislinked synonym is worse than a miss: on genuine ambiguity it DEFERS.

These are fake-DB behavioral tests. We model the only DB shapes the resolver
touches: the ``entity_synonyms`` term lookup and the ``entities`` liveness probe.
The §11 type signal (``_resolve_type_signals``) is monkeypatched per-entity so the
test exercises the resolver's OWN branching, not the hierarchy walk (covered
elsewhere). SUBJECT-AGNOSTIC: opaque tokens / synthetic UUIDs only, no personas.
"""

import pytest

import src.api.main as main
from src.api.main import resolve_entity_synonym


# --------------------------------------------------------------------------- #
# Fake DB — recognizes the resolver's exact query set by SQL fragment.
# --------------------------------------------------------------------------- #

class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())  # collapse whitespace for robust matching
        if "FROM entity_synonyms" in s and "WHERE term" in s:
            term = params[0]
            self._result = list(self._db.synonym_rows.get(term, []))
        elif "FROM entities WHERE id" in s:
            # liveness probe: SELECT 1 FROM entities WHERE id = %s
            eid = params[0]
            self._result = [(1,)] if eid in self._db.live_entities else []
        else:
            # The resolver under test should not issue any other query directly;
            # _resolve_type_signals (which would) is monkeypatched out. Fail loud.
            raise AssertionError(f"unexpected SQL hit fake cursor: {s!r}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeDB:
    def __init__(self, synonym_rows, live_entities):
        # synonym_rows: {normalized_term: [(entity_id, link_basis, role_rel_type), ...]}
        self.synonym_rows = synonym_rows
        self.live_entities = set(live_entities)

    def cursor(self):
        return _FakeCursor(self)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

# Synthetic UUID-shaped ids (resolver treats them as opaque strings).
E_DENSE = "00000000-0000-0000-0000-0000000000aa"
E_SPARSE = "00000000-0000-0000-0000-0000000000bb"
E_ANIMAL = "00000000-0000-0000-0000-0000000000cc"
E_PERSON = "00000000-0000-0000-0000-0000000000dd"
ANCHOR = "00000000-0000-0000-0000-000000000099"


@pytest.fixture
def patch_type_signals(monkeypatch):
    """Install a per-entity type-signal map and an implied-types value.

    Returns a setter so each test declares exactly what §11 sees, without
    touching the real hierarchy walk.
    """
    signal_map = {}
    implied_box = {"types": set()}

    def fake_signals(db, eid, *a, **k):
        sig = signal_map.get(eid, set())
        return (sig, bool(sig))

    def fake_implied(db, nterm):
        return set(implied_box["types"])

    monkeypatch.setattr(main, "_resolve_type_signals", fake_signals)
    monkeypatch.setattr(main, "_synonym_term_implied_types", fake_implied)

    def configure(signals=None, implied=None):
        if signals:
            signal_map.update(signals)
        if implied is not None:
            implied_box["types"] = set(implied)

    return configure


# --------------------------------------------------------------------------- #
# 1. STRENGTHEN PRESERVED — single candidate resolves.
# --------------------------------------------------------------------------- #

def test_single_candidate_resolves(patch_type_signals):
    """One unambiguous nickname → its one entity. MUST NOT regress."""
    patch_type_signals(implied=set())
    db = _FakeDB(
        synonym_rows={"nick": [(E_DENSE, "entity", None)]},
        live_entities={E_DENSE},
    )
    assert resolve_entity_synonym("nick", ANCHOR, db) == E_DENSE


def test_single_candidate_dead_target_defers(patch_type_signals):
    """Single row but the target entity is dead → no live candidate → None."""
    patch_type_signals(implied=set())
    db = _FakeDB(
        synonym_rows={"nick": [(E_DENSE, "entity", None)]},
        live_entities=set(),  # target not live
    )
    assert resolve_entity_synonym("nick", ANCHOR, db) is None


def test_no_row_defers(patch_type_signals):
    """Term not in the table (empty/miss) → None (fall-through == today)."""
    patch_type_signals(implied=set())
    db = _FakeDB(synonym_rows={}, live_entities=set())
    assert resolve_entity_synonym("ghost", ANCHOR, db) is None


# --------------------------------------------------------------------------- #
# 2. §11 TYPE FILTER narrows to exactly one → resolves to the consistent one.
# --------------------------------------------------------------------------- #

def test_type_filter_narrows_to_one(patch_type_signals):
    """Two same-term homonyms of DIFFERENT types; the term implies one type.

    The Animal-typed and Person-typed candidates both carry the term, but the
    term's implied type is {animal}. The §11 filter keeps only the Animal-typed
    candidate → deterministic single winner (NOT density).
    """
    patch_type_signals(
        signals={E_ANIMAL: {"animal"}, E_PERSON: {"person"}},
        implied={"animal"},
    )
    db = _FakeDB(
        synonym_rows={"crit": [(E_ANIMAL, "entity", None),
                               (E_PERSON, "entity", None)]},
        live_entities={E_ANIMAL, E_PERSON},
    )
    assert resolve_entity_synonym("crit", ANCHOR, db) == E_ANIMAL


# --------------------------------------------------------------------------- #
# 3. THE BEHAVIOR CHANGE — two same-typed candidates, no deterministic
#    discriminator → DEFER (None), NOT the denser one.
# --------------------------------------------------------------------------- #

def test_two_same_type_candidates_defer_not_densest(patch_type_signals):
    """THE motivating case. Two Person-typed homonyms; no §11 discriminator.

    Previously this picked whichever entity had MORE facts (density argmax) —
    a confident mislink. The contract now: DEFER → None. We make E_DENSE the
    one the old code would have picked (more incident facts) to prove density
    is no longer consulted: the result must be None regardless.
    """
    patch_type_signals(
        signals={E_DENSE: {"person"}, E_SPARSE: {"person"}},
        implied=set(),  # opaque term → §11 is a no-op
    )
    db = _FakeDB(
        synonym_rows={"buddy": [(E_DENSE, "entity", None),
                                (E_SPARSE, "entity", None)]},
        live_entities={E_DENSE, E_SPARSE},
    )
    assert resolve_entity_synonym("buddy", ANCHOR, db) is None


def test_type_filter_cannot_narrow_still_defers(patch_type_signals):
    """Implied type matches BOTH candidates → filter no-ops → still >1 → DEFER."""
    patch_type_signals(
        signals={E_DENSE: {"person"}, E_SPARSE: {"person"}},
        implied={"person"},  # both survive
    )
    db = _FakeDB(
        synonym_rows={"buddy": [(E_DENSE, "entity", None),
                                (E_SPARSE, "entity", None)]},
        live_entities={E_DENSE, E_SPARSE},
    )
    assert resolve_entity_synonym("buddy", ANCHOR, db) is None


# --------------------------------------------------------------------------- #
# 4. Previously equal-density tie — still None (now subsumed by the DEFER rule).
# --------------------------------------------------------------------------- #

def test_equal_density_tie_still_defers(patch_type_signals):
    """Two candidates that the OLD code would have refused on an exact density
    tie. New code defers on ANY residual ambiguity → same None outcome, broader
    rule. (Density is no longer computed at all.)"""
    patch_type_signals(
        signals={E_DENSE: set(), E_SPARSE: set()},  # type-unknown both
        implied=set(),
    )
    db = _FakeDB(
        synonym_rows={"twin": [(E_DENSE, "entity", None),
                               (E_SPARSE, "entity", None)]},
        live_entities={E_DENSE, E_SPARSE},
    )
    assert resolve_entity_synonym("twin", ANCHOR, db) is None


# --------------------------------------------------------------------------- #
# 5. Density helper is gone — guard against accidental reintroduction.
# --------------------------------------------------------------------------- #

def test_information_degree_helper_removed():
    """The graph-density tiebreak helper must no longer exist on the module."""
    assert not hasattr(main, "_synonym_information_degree")

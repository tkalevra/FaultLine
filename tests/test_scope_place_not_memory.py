"""SCOPEPROJ — THE HARD LINE, query side: a PLACE must never be returned AS the memory.

The reported defect had two faces on one root:

  A) scope resolved to the L4 CLASSIFICATION LADDER only, yet stayed ACTIVE, so the walk was
     projected through a scope that admits nothing grounded — and, separately, a query whose
     keywords resolved NOTHING got neither scope NOR fetch-all (a dead lane), so the NARROW
     question returned strictly LESS than the BROAD one.
  B) "What are my preferences?" ANCHORED on the L4 type node `preferences` and climbed
     `subclass_of`, answering "Preferences is a subclass of Predilection" — the PLACE
     (the type index) returned as if it were the MEMORY.

These tests pin the three deterministic seams that close it. The DB-bound ones use a fake
cursor shaped to the REAL statements (see the dead-mock lesson in CLAUDE.md — the fixture
must match the SQL the code actually issues, not a remembered shape).
"""
import os

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import pytest

from src.api.main import (
    _query_has_first_person_possessive,
    _entity_is_l4_place,
)
from src.extraction.linguistics import linguistics_available


# ─────────────────────────── A. the grammatical hook ────────────────────────────
# CLAUDE.md: the ONE language hook is that "I"/"me"/"my" binds to the speaking subject, and
# it is detected GRAMMATICALLY (Person=1 / Poss=Yes), NEVER by a hardcoded pronoun list.

@pytest.mark.skipif(not linguistics_available(),
                    reason="spaCy en_core_web_sm required (set SPACY_MODEL)")
@pytest.mark.parametrize("query", [
    "What are my preferences?",
    "tell me about my dog",
    "our network hierarchy",
    "What is my favorite colour?",
])
def test_first_person_possessive_detected(query):
    assert _query_has_first_person_possessive(query) is True


@pytest.mark.skipif(not linguistics_available(),
                    reason="spaCy en_core_web_sm required (set SPACY_MODEL)")
@pytest.mark.parametrize("query", [
    "What is a dog?",              # no possessive at all — a genuine classification ask
    "I like blue",                 # Person=1 but PLAIN personal pronoun (no Poss=Yes)
    "What are your preferences?",  # 2nd person possessive — not the speaker
    "tell me about Sarah's family",  # 3rd-person genitive
    "",
])
def test_non_first_person_possessive_rejected(query):
    assert _query_has_first_person_possessive(query) is False


# ───────────────────── B. THE HARD LINE: place vs memory ────────────────────────
# A PLACE is an L4 type/class node: something is filed AT it (it is the OBJECT of a
# classification rel) or it declares its own superclass (SUBJECT of P279). A named INSTANCE
# (SUBJECT of P31) is a MEMORY and wins the tie. Metadata-driven — the rels come from
# rel_types.wikidata_pid ∈ {P31, P279}, never a hardcoded name list.

_P31 = "instance_of"
_P279 = "subclass_of"

PLACE_CLASS_NODE = "aaaaaaaa-0000-0000-0000-000000000001"   # subject of subclass_of
PLACE_HAS_INSTANCES = "aaaaaaaa-0000-0000-0000-000000000002"  # object of instance_of
NAMED_INSTANCE = "bbbbbbbb-0000-0000-0000-000000000003"     # subject of instance_of
BARE_ENTITY = "cccccccc-0000-0000-0000-000000000004"        # on no ladder at all


class _FakeCursor:
    """Shaped to the REAL statements `_entity_is_l4_place` issues, in order:
      1. SELECT rel_type FROM rel_types WHERE wikidata_pid = 'P31'
      2. the P31 SUBJECT probe (instance wins the tie)          [only when P31 resolved]
      3. the PLACE probe (object-of-classification OR subject-of-subclass)
    Also serves `_get_classification_rels`'s own rel_types read."""

    def __init__(self):
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        params = params or ()
        if "from rel_types" in s and "wikidata_pid = %s" in s:
            self._result = [(_P31,)]
            return
        if "from rel_types" in s:
            # _get_classification_rels: the P31/P279 hierarchy rels for this tenant.
            self._result = [(_P31,), (_P279,)]
            return
        if "from facts" in s or "from staged_facts" in s:
            eid = params[0] if params else None
            rels = set(params[1] or []) if len(params) > 1 else set()
            is_subject_probe = "object_id = %s" not in s
            if is_subject_probe:
                # the P31 instance-wins probe
                self._result = [(1,)] if (eid == NAMED_INSTANCE and _P31 in rels) else []
                return
            # the combined PLACE probe (object-of-classification ∪ subject-of-subclass)
            hit = (
                (eid == PLACE_HAS_INSTANCES)        # something is filed AT it
                or (eid == PLACE_CLASS_NODE)        # it declares its own superclass
            )
            self._result = [(1,)] if hit else []
            return
        self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


class _FakeDB:
    def cursor(self):
        return _FakeCursor()


def test_class_node_declaring_a_superclass_is_a_place():
    assert _entity_is_l4_place(_FakeDB(), PLACE_CLASS_NODE) is True


def test_node_with_instances_filed_at_it_is_a_place():
    assert _entity_is_l4_place(_FakeDB(), PLACE_HAS_INSTANCES) is True


def test_named_instance_is_a_memory_not_a_place():
    """`Fraggle instance_of dog` — Fraggle is the MEMORY, dog is the place. The instance
    must win the tie or "my Aurora"/"my router" would stop anchoring on themselves."""
    assert _entity_is_l4_place(_FakeDB(), NAMED_INSTANCE) is False


def test_entity_off_the_ladder_is_not_a_place():
    assert _entity_is_l4_place(_FakeDB(), BARE_ENTITY) is False


def test_place_check_is_fail_safe():
    """No db / no entity / a broken cursor → False (not a place → today's behaviour).
    The guards keyed on this are ADDITIVE; uncertainty must never change resolution."""
    class _Boom:
        def cursor(self):
            raise RuntimeError("db gone")
    assert _entity_is_l4_place(None, PLACE_CLASS_NODE) is False
    assert _entity_is_l4_place(_FakeDB(), "") is False
    assert _entity_is_l4_place(_Boom(), PLACE_CLASS_NODE) is False


# ────────────── C. the absent-concept gate's per-tenant SQL contract ─────────────
# The gate's grounding probes must NOT filter on `entity_aliases.user_id`: the PER-TENANT
# table has no such column (CLAUDE.md "Entity Lifecycle", corrected 2026-07-30 — the UNIQUE
# is `(entity_id, alias)`; `user_id` is the LEGACY `public` shape). Binding it raised
# UndefinedColumn, which the gate's fail-safe swallowed (so the gate was DEAD) AND which
# ABORTED the caller's transaction, blanking every downstream read in that /query request.

def test_defer_to_fetch_all_is_gated_on_the_absent_concept_partner():
    """PRECISION: the unresolved-aspect defer must consult the anti-fabrication predicate.

    "how long was I in Korea for" carries a 1st-person reference, so the /query abstention
    gate (which only runs for anchor method=="default") would NOT catch it — the defer has to
    ask directly, or a never-mentioned concept would newly dump the user's profile. Pinned at
    the source so the conjunction cannot be quietly dropped back to a bare person check."""
    import inspect
    from src.api.main import determine_path
    src = inspect.getsource(determine_path)
    i = src.find("unresolved_aspect_defers_to_fetch_all")
    assert i > 0, "expected the return-to-sender defer in determine_path"
    guard = src[max(0, i - 1400):i]
    assert "_has_first_or_second_person_reference(query_text)" in guard
    assert "not _query_scoped_to_absent_concept(query_text, db, user_id)" in guard


def test_absent_concept_gate_does_not_bind_user_id_on_entity_aliases():
    import inspect
    from src.api.main import _query_scoped_to_absent_concept
    src = inspect.getsource(_query_scoped_to_absent_concept)
    stmts = [ln for ln in src.splitlines() if "FROM entity_aliases" in ln]
    assert stmts, "expected the gate to probe entity_aliases"
    for ln in stmts:
        assert "user_id" not in ln, (
            "per-tenant entity_aliases has NO user_id column — binding it raises "
            "UndefinedColumn and aborts the request transaction: " + ln.strip()
        )

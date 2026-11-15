"""Unit tests for the /learn (/extend) hardening trio (directives A, B, C).

These cover the DETERMINISTIC seams added to harden the topic-learning path:

  A — topic UP-rung backstop + over-learning guard:
      * `_validate_topic_up_parent` — the deterministic gate the LLM proposal must pass
        (no scalar, no upper-ontology root, ≤3-word category token, != topic). Proves the
        topic classes upward into a CONCRETE category, never a bare abstract catch-all.
      * the survival check that decides whether the backstop fires (no topic subclass_of/
        instance_of edge survived → fire).
      * the over-learning guard refuses trivial / over-broad (bare upper-root) topics.

  B — read-time GLiNER2 label safety:
      * `_filter_to_canonical_types(..., allow_wildcards=False)` drops non-canonical member
        types (Pitfall 11) AND the documented 6-label cap holds — defense-in-depth at the read.

  C — deliberate growth is a SOLID Class B (no aging-out):
      * `assign_class_and_confidence` keeps llm_learned facts off Class A;
      * a novel rel_type emitted in a /learn batch floors to Class B (the durable-B flag is ON
        by default), so it lands on the promotion track and is exempt from Class-C expiry.

Pure / deterministic — no DB, no model, no live LLM. LLM-call helpers are not exercised here
(they are bounded + fail-safe by construction); the gate they feed is what these tests pin.
NOTE: tests/ is gitignored; commit with `git add -f`.
"""
import importlib

import pytest

main = importlib.import_module("src.api.main")


# ─────────────────────────── Directive A: topic UP-rung ───────────────────────────

@pytest.mark.parametrize("parent,ok", [
    ("software", True),
    ("configuration_management_tool", True),
    ("operating system", True),          # ≤3 words allowed
    ("network_protocol", True),
    ("thing", False),                    # bare upper-ontology root
    ("entity", False),
    ("object", False),
    ("concept", False),
    ("item", False),
    ("stuff", False),
    ("", False),                         # empty
    ("null", False),                     # LLM "no parent"
    ("none", False),
    ("192.168.1.1", False),              # scalar/literal
    ("a@b.com", False),                  # literal punctuation
    ("a very long descriptive phrase here", False),  # not a category token (>3 words)
])
def test_validate_topic_up_parent_gate(parent, ok):
    got, _why = main._validate_topic_up_parent("ansible", parent)
    assert got is ok


def test_validate_topic_up_parent_rejects_self():
    # The parent must differ from the topic — no self-bridge.
    ok, why = main._validate_topic_up_parent("ansible", "ansible")
    assert ok is False
    assert why == "parent_equals_topic"


def test_topic_typing_rels_are_the_up_rung_rels():
    # The survival check keys off these two rels only.
    assert main._TOPIC_TYPING_RELS == frozenset({"subclass_of", "instance_of"})


def _topic_has_up_edge(edges, topic_lower):
    """Mirror of the in-handler survival check (directive A) for unit assertion."""
    return any(
        (e.get("subject") or "").strip().lower() == topic_lower
        and e.get("rel_type") in main._TOPIC_TYPING_RELS
        for e in edges
    )


def test_backstop_fires_when_no_topic_up_edge_survives():
    # Only component (part_of) edges — the topic has NO is-a up-edge → backstop must fire.
    edges = [
        {"subject": "playbook", "rel_type": "part_of", "object": "ansible"},
        {"subject": "role", "rel_type": "part_of", "object": "playbook"},
    ]
    assert _topic_has_up_edge(edges, "ansible") is False


def test_backstop_skipped_when_topic_already_typed():
    # The LLM already typed the topic upward → backstop must NOT fire (no duplicate rung).
    edges = [
        {"subject": "ansible", "rel_type": "subclass_of", "object": "software"},
        {"subject": "playbook", "rel_type": "part_of", "object": "ansible"},
    ]
    assert _topic_has_up_edge(edges, "ansible") is True


def test_backstop_instance_of_also_counts_as_up_edge():
    edges = [{"subject": "spot", "rel_type": "instance_of", "object": "dog"}]
    assert _topic_has_up_edge(edges, "spot") is True


def test_backstop_object_side_topic_does_not_count():
    # An edge where the topic is the OBJECT (a child pointing UP at it) is NOT the topic's own
    # up-rung — the survival check must require the topic to be the SUBJECT.
    edges = [{"subject": "playbook", "rel_type": "subclass_of", "object": "ansible"}]
    assert _topic_has_up_edge(edges, "ansible") is False


def test_topic_up_backstop_flag_defaults_on():
    # Behaviour change (injects a new edge) but a pure safety/completeness narrowing of the
    # island problem; documented default ON.
    assert main._LEARN_TOPIC_UP_BACKSTOP is True


# ─────────────── Directive A: over-learning guard (size/metadata-driven) ───────────

def test_overbroad_root_words_are_the_guard_set():
    # The over-broad refusal keys off the SAME upper-root set as the parent validator —
    # subject-agnostic, not a topic word-list.
    for w in ("thing", "entity", "object", "concept", "item", "stuff"):
        assert w in main._TOPIC_UP_ROOT_WORDS


def test_learn_caps_are_bounded_and_small():
    # Breadth/depth cannot run away: hard per-call edge cap + operational rel_type cap + depth.
    assert main._LEARN_MAX_EDGES <= 12
    assert main._LEARN_MAX_DEPTH <= 2
    assert main._LEARN_BOUNDED_GROWTH is True


# ─────────────────────── Directive B: read-time GLiNER2 label safety ───────────────

def test_read_label_filter_drops_non_canonical_types():
    # member_entity_types containing architecture nouns (Pitfall 11 / RC-expand-taxonomy-
    # pollution) must be filtered to the canonical closed set at the READ.
    polluted = ["Person", "KnowledgeGraph", "MemoryManager", "Object", "FaultLineCore", "Node"]
    out = main._filter_to_canonical_types(polluted, allow_wildcards=False)
    assert out == ["Person", "Object"]  # only canonical survive, order-preserving


def test_read_label_filter_excludes_wildcards():
    # ANY/SCALAR are type-constraint tokens, NOT GLiNER2 entity-discovery labels.
    out = main._filter_to_canonical_types(["Person", "ANY", "SCALAR", "Concept"],
                                          allow_wildcards=False)
    assert out == ["Person", "Concept"]


def test_read_label_cap_ceiling_is_canonical_count():
    # The documented Pitfall-11 ceiling == the 6 canonical general types.
    assert main._GLINER2_MAX_LABELS == len(main._CANONICAL_ENTITY_TYPES) == 6


def test_read_label_filter_defaults_on():
    # Pure safety narrowing (drops/caps only) → default ON.
    assert main._GLINER2_READ_LABEL_FILTER is True


def test_read_label_cap_trims_to_ceiling():
    # If more than 6 canonical-ish labels somehow survive, the cap trims to the ceiling.
    survivors = main._filter_to_canonical_types(
        ["Person", "Animal", "Organization", "Location", "Object", "Concept"],
        allow_wildcards=False)
    capped = survivors[: main._GLINER2_MAX_LABELS]
    assert len(capped) == 6


# ─────────────── Directive C: deliberate growth is a SOLID Class B ─────────────────

@pytest.fixture
def _seed_rel_meta(monkeypatch):
    """Seed the in-process rel metadata cache so assign_class_and_confidence resolves a
    defined fact_class without a DB (the caches are DB-populated at runtime)."""
    def _set(rel, fact_class):
        monkeypatch.setitem(main._REL_TYPE_META, rel, {"fact_class": fact_class})
        # _REL_TYPE_CACHE is consulted first; clear our key there so _REL_TYPE_META wins.
        if rel in getattr(main, "_REL_TYPE_CACHE", {}):
            monkeypatch.delitem(main._REL_TYPE_CACHE, rel, raising=False)
    return _set


def test_llm_learned_never_class_a_even_for_class_a_rel(_seed_rel_meta):
    # llm_learned (the /learn provenance) is NEVER Class A — even when the rel's defined class is A.
    _seed_rel_meta("spouse", "A")
    cls, conf = main.assign_class_and_confidence(
        {"storage": "relational"}, fact_provenance="llm_learned", rel_type="spouse")
    assert cls == "B"            # A-defined rel degrades to a SOLID Class B under llm_learned
    assert conf == pytest.approx(0.6)


def test_llm_learned_class_b_rel_stays_b(_seed_rel_meta):
    # A known rel whose defined class is B stays a SOLID Class B (never ages out).
    _seed_rel_meta("has_interest_in", "B")
    cls, conf = main.assign_class_and_confidence(
        {"storage": "relational"}, fact_provenance="llm_learned", rel_type="has_interest_in")
    assert cls == "B"
    assert conf <= 0.6


def test_durable_class_b_flag_defaults_on():
    # Consistency/durability fix — default ON, reversible.
    assert main._LEARN_DURABLE_CLASS_B is True

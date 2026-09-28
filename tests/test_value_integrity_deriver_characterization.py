"""CHARACTERIZATION pins for the value-integrity lane at the deriver — the #1 failure class.

The most damaging failure the perfect-flow gauntlet exists to fix (opencode-divergence
observation, ranked #1): a possessive-scalar whose attribute noun the tenant does not yet
know minted the NOUN as an entity and ANNIHILATED the value, while the client was told
success (facts_superseded=1 over an empty graph). "the quarrel's latch code is 4408" and
"my vetch count is nine" both lost their number; only a known attribute noun ("shelf number")
captured.

These pins drive `derive_sentence_facts` directly (deterministic, verified stable over repeated
runs; no LLM brain — the spine's LLM is atomize-only and is not on this path) and assert the
lane's core invariant: THE VALUE SURVIVES, and the attribute noun is NOT minted as a type. They
run on the SEED path (no grown cue class), which is the point — value integrity must hold even
BEFORE the per-tenant attribute_noun class grows, so a first-exposure unknown noun never
annihilates its value. The end-to-end verdict-vs-DB equality bar (1.2, "no false successes")
still needs the front-door battery on the spec'd brain, gated to the 2026-08-27 reset; this is
the deriver-level floor beneath it.

Measured against en_core_web_sm. Introduced by the value-integrity lane (`_attribute_nouns`,
the growth-out carrier). See gauntlet ledger VERIFY-006.
"""

import os

import pytest

import src.extraction.linguistics as ling

_MODEL = os.environ.get("SPACY_MODEL", "en_core_web_sm")


@pytest.fixture(scope="module", autouse=True)
def _require_model():
    spacy = pytest.importorskip("spacy")
    try:
        spacy.load(_MODEL)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"spaCy model {_MODEL!r} not loadable: {e}")


def _derive(sentence):
    residue = []
    edges = ling.derive_sentence_facts(sentence, reference="user", residue_out=residue)
    return edges, sorted(residue)


def _has_value(edges, value):
    """The VALUE appears as the object of some edge — the property that must never be lost."""
    return any(e.object == value for e in edges)


def _minted_as_type(edges, noun):
    """The attribute noun became a classified ENTITY (the annihilation signature)."""
    return any(
        e.rel_type in ("instance_of", "subclass_of") and noun in (e.subject or "").lower()
        for e in edges
    )


# ── KNOWN attribute noun — the case that always worked ────────────────────────────────────

def test_known_attribute_noun_captures_value():
    edges, _ = _derive("My shelf number is 305.")
    assert _has_value(edges, "305")
    assert not _minted_as_type(edges, "shelf")


# ── UNKNOWN id-shaped attribute noun — "latch code" annihilated the value before the lane ──

def test_unknown_identifier_attribute_keeps_its_value():
    """"My latch code is 4408." — the original #1 failure annihilated 4408 and minted
    'latch code' as an entity. The value must now survive (routed to has_reference_id) and
    the noun must NOT be minted as a type."""
    edges, residue = _derive("My latch code is 4408.")
    assert _has_value(edges, "4408"), f"value 4408 annihilated: edges={edges} residue={residue}"
    assert not _minted_as_type(edges, "latch")
    assert "4408" not in residue  # the value is captured, not dropped to uncovered residue


# ── UNKNOWN count attribute noun — "vetch count" typed 'count' as entity before the lane ──

def test_unknown_count_attribute_keeps_its_value():
    """"My vetch count is nine." — before the lane this typed 'vetch count' as an entity
    (subclass_of count) and lost the number. The value must now survive."""
    edges, residue = _derive("My vetch count is nine.")
    assert _has_value(edges, "nine"), f"value 'nine' annihilated: edges={edges} residue={residue}"
    assert not _minted_as_type(edges, "count")
    assert "nine" not in residue

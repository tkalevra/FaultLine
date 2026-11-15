"""THE HARD LINE (ingest enforcement): a bare common TYPE-noun is not a NAME.

Root cause #3 of the Capital One seat corruption: on the /extract/rewrite LLM ingest path a
dense multi-entity statement produced bare COMMON TYPE-NOUNS ("ticket", "issue", "dispute",
"refund", "address") registered as ``is_preferred=true`` standalone ENTITIES — a category error
across the hard line (a common noun is a TYPE / an L4 place, not a NAME / the naming layer) that
renders as garbage recall ("Issues is address").

These tests pin the DETERMINISTIC type-vs-name classifier ``_surface_is_bare_common_type_noun``
(``src/api/main.py``) that gates the ingest-side demotion. The signal is spaCy POS + NER on the
ORIGINAL-CASED surface (casing is load-bearing and only exists at ingest — entity_aliases
lowercases at storage, which DESTROYS the NER name-signal). NO noun/name word list.

Contract:
  * bare common type-nouns  -> True  (demote: a type-word must never be a preferred NAME)
  * proper / product names  -> False (keep: authority user>seed>growth, never strip a real name)
  * values / identifiers    -> False (leave: owned by the value/scalar lane)

Model-backed; skips cleanly if en_core_web_sm is not installed.
"""

import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")


def _classifier():
    """Import the classifier; skip if spaCy en_core_web_sm is unavailable."""
    try:
        import spacy  # noqa: F401
        spacy.load("en_core_web_sm")
    except Exception:  # pragma: no cover - env guard
        pytest.skip("en_core_web_sm not installed")
    from src.api.main import _surface_is_bare_common_type_noun
    return _surface_is_bare_common_type_noun


# The exact bug-set common nouns (+ determiner / compound variants) — all TYPE-words.
@pytest.mark.parametrize(
    "surface",
    [
        "ticket", "issue", "refund", "address", "catastrophic failure",
        "the ticket", "refund request", "a dispute case",
    ],
)
def test_bare_common_type_noun_is_demoted(surface):
    f = _classifier()
    assert f(surface) is True, f"{surface!r} is a TYPE-word and must be demoted, not a NAME"


# Real proper names AND styled product names must STAY names (never stripped).
@pytest.mark.parametrize(
    "surface",
    [
        "Capital One",   # NER ORG
        "Luna",       # PROPN / NER
        "Diane",         # NER PERSON
        "Dan",         # NER PERSON
        "New York",      # NER GPE
        "Rex",           # named instance
        "OPNsense",      # acronym-prefixed product (interior caps)
        "iPhone",        # CamelCase product
        "TrueNAS",       # CamelCase product
        "ITIL",          # all-caps acronym
    ],
)
def test_proper_and_product_names_stay_names(surface):
    f = _classifier()
    assert f(surface) is False, f"{surface!r} is a NAME and must NOT be demoted"


# Values / identifiers belong to the scalar/value lane, never the name/type decision.
@pytest.mark.parametrize("surface", ["May 3rd", "192.168.1.1", "Room 204", "", "   "])
def test_values_and_empty_are_left_alone(surface):
    f = _classifier()
    assert f(surface) is False


def test_determinism_repeated_calls_agree():
    f = _classifier()
    for s in ("ticket", "Diane", "OPNsense"):
        results = {f(s) for _ in range(5)}
        assert len(results) == 1, f"non-deterministic classification for {s!r}: {results}"

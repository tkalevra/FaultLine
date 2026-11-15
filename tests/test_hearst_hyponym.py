"""G9 — Hearst (1992) lexico-syntactic hyponym/exemplar capture.

Marti A. Hearst, "Automatic Acquisition of Hyponyms from Large Text Corpora", COLING 1992 Vol. 2,
pp. 539-545. These tests pin the DETERMINISTIC spaCy matcher ``linguistics.detect_hearst_hyponyms``
and the HARD-LINE routing (common-noun hyponym → ``subclass_of`` the category; proper-name exemplar →
``instance_of`` + alias, NEVER ``subclass_of`` on a name). The matcher is pure spaCy dependency/POS —
no DB, no cosine, no LLM — so these run without POSTGRES_DSN. The DB staging + end-to-end count walk
are proven separately by DEV/review/_hearst_live_driver.py against the real TEST_SEAT schema.

Model-backed tests skip cleanly when en_core_web_sm is absent.
"""
import pytest

import src.extraction.linguistics as ling


def _model_available() -> bool:
    try:
        import spacy
        spacy.load("en_core_web_sm")
        return True
    except Exception:  # noqa: BLE001
        return False


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")


@pytest.fixture(scope="module")
def nlp():
    import spacy
    return spacy.load("en_core_web_sm")


def _one_hit(nlp, text):
    hits = ling.detect_hearst_hyponyms(nlp(text))
    assert len(hits) == 1, f"expected exactly one Hearst hit for {text!r}, got {hits}"
    return hits[0]


def _member_map(hit):
    return {m["surface"]: m["is_proper"] for m in hit["members"]}


# ── ROUTING PRECONDITION: is_proper drives THE HARD LINE at the caller ────────────────────────────
def _routed_edges(hit):
    """Replicate the ``_attach_hearst_hyponyms`` routing (pure): (member, rel, category)."""
    cat = hit["category"]
    out = []
    for m in hit["members"]:
        rel = "instance_of" if m["is_proper"] else "subclass_of"
        out.append((m["surface"], rel, cat))
    return out


# ── P1 "such as" — COMMON-NOUN members → subclass_of ──────────────────────────────────────────────
@requires_model
def test_such_as_common_nouns_are_subclass_of(nlp):
    hit = _one_hit(nlp, "I've cooked with citrus fruits such as lemon, lime, and orange.")
    assert hit["marker"] == "such as"
    assert hit["category"] == "citrus fruits"
    assert _member_map(hit) == {"lemon": False, "lime": False, "orange": False}
    # THE HARD LINE: every common-noun member routes to subclass_of, none to instance_of.
    edges = _routed_edges(hit)
    assert edges == [("lemon", "subclass_of", "citrus fruits"),
                     ("lime", "subclass_of", "citrus fruits"),
                     ("orange", "subclass_of", "citrus fruits")]


@requires_model
def test_such_as_captures_all_three_members_for_count(nlp):
    """The end-to-end count precondition: all three coordinated members are captured (not just the
    first), so 'how many citrus fruits have I used?' can count 3."""
    hit = _one_hit(nlp, "I've cooked with citrus fruits such as lemon, lime, and orange.")
    assert len(hit["members"]) == 3


# ── HARD LINE: "such as" — PROPER-NAME members → instance_of + alias, NEVER subclass_of ────────────
@requires_model
def test_such_as_proper_names_are_instance_of_never_subclass(nlp):
    hit = _one_hit(nlp, "I love restaurants such as Nobu and Carbone.")
    assert hit["marker"] == "such as"
    assert hit["category"] == "restaurants"
    assert _member_map(hit) == {"nobu": True, "carbone": True}
    edges = _routed_edges(hit)
    # Proper names → instance_of the category; ZERO subclass_of on a name (the category error).
    assert edges == [("nobu", "instance_of", "restaurants"),
                     ("carbone", "instance_of", "restaurants")]
    assert all(rel != "subclass_of" for (_, rel, _) in edges)


# ── COMMON-vs-PROPER discriminator is capitalization+PROPN (a lowercase mistag stays common) ──────
@requires_model
def test_lowercase_mistagged_propn_stays_common(nlp):
    # en_core_web_sm mistags "mango" as PROPN; the capitalization guard keeps it a COMMON noun.
    hit = _one_hit(nlp, "I enjoy fruits, especially mango.")
    assert hit["marker"] == "especially"
    assert hit["category"] == "fruits"
    assert _member_map(hit) == {"mango": False}
    assert _routed_edges(hit) == [("mango", "subclass_of", "fruits")]


# ── P4 "including" ────────────────────────────────────────────────────────────────────────────────
@requires_model
def test_including_common_nouns(nlp):
    hit = _one_hit(nlp, "I take supplements including vitamin C and zinc.")
    assert hit["marker"] == "including"
    assert hit["category"] == "supplements"
    assert _member_map(hit) == {"vitamin c": False, "zinc": False}


# ── P3 "and other" — category is the 'other'-marked conjunct; others are members ──────────────────
@requires_model
def test_and_other_pattern(nlp):
    hit = _one_hit(nlp, "I like lemon, lime, and other citrus fruits.")
    assert hit["marker"] == "other"
    assert hit["category"] == "citrus fruits"          # "other" stripped from the category surface
    assert set(_member_map(hit)) == {"lemon", "lime"}
    assert all(not p for p in _member_map(hit).values())


# ── P2 "such NP as" — the marker is stripped from the category ("such pets" → "pets") ─────────────
@requires_model
def test_such_np_as_strips_marker_from_category(nlp):
    hit = _one_hit(nlp, "I have such pets as a dog and a cat.")
    assert hit["marker"] == "such as"
    assert hit["category"] == "pets"
    assert set(_member_map(hit)) == {"dog", "cat"}


# ── P6 "like" — prepositional sense only; the VERB "I like X" must NOT trigger ────────────────────
@requires_model
def test_prepositional_like_triggers(nlp):
    hit = _one_hit(nlp, "I use tools like a hammer and a wrench.")
    assert hit["marker"] == "like"
    assert hit["category"] == "tools"
    assert set(_member_map(hit)) == {"hammer", "wrench"}


@requires_model
def test_verb_like_does_not_trigger(nlp):
    assert ling.detect_hearst_hyponyms(nlp("I like pizza.")) == []


# ── NO marker → NO hit (subject-agnostic; never fabricates a taxonomy) ────────────────────────────
@requires_model
def test_no_marker_no_hit(nlp):
    assert ling.detect_hearst_hyponyms(nlp("I read books and magazines.")) == []
    assert ling.detect_hearst_hyponyms(nlp("My dog is brown.")) == []


# ── Determinism: same input → same hit set across repeats ─────────────────────────────────────────
@requires_model
def test_deterministic_repeat(nlp):
    text = "I've cooked with citrus fruits such as lemon, lime, and orange."
    runs = [tuple((h["category"], tuple(m["surface"] for m in h["members"]))
                  for h in ling.detect_hearst_hyponyms(nlp(text))) for _ in range(5)]
    assert len(set(runs)) == 1


# ── Fail-safe: None doc → [] (never raises) ───────────────────────────────────────────────────────
def test_none_doc_is_empty():
    assert ling.detect_hearst_hyponyms(None) == []

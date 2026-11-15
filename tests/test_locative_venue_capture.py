"""Regression — PP-LOCATIVE VENUE CAPTURE (LME multi-session count cluster, qid 80ec1f4f).

CLUSTER: "How many different museums or galleries did I visit in February?" (gold=2). The two venues
are stated with a LOCATIVE / GOAL prepositional phrase ("attended a workshop AT the Modern Art Museum",
"took my niece TO the Natural History Museum", "at the opening night OF The Art Cube"). The occurrence
seam captured the ACTIVITY (the eventive head) but DROPPED the venue as a circumstantial adjunct — and
worse, the title seam mis-filed the venue place as the occurrence's also_known_as NAME — so a "which /
how many museums did I visit" walk, which scopes by the PLACE type, had NO place entity to count.

THE FIX (deterministic, subject-agnostic, evidence-grounded — Universal Dependencies locative oblique
argument obl:lmod / goal obl:lto; de Marneffe, Manning, Nivre & Zeman 2021, Computational Linguistics
47(2)):
  • the deriver (_locative_venue_phrase / _build_event_analysis) surfaces the named place pobj of a
    verb-/event-noun-governed locative preposition on EventAnalysis.venue (grammatical ADP prep→pobj,
    NER person-guard, NO venue word-list), and STOPS the title seam from stealing that locative place;
  • the event seams emit (user, located_in, <venue>)@<event_date> typed Location (the ANY/ANY place
    rel, NOT the SCALAR-tail located_at) so the place is a walkable, dated, countable entity.
Also pins the QUERY-side coordinate/disjunction split of a count TYPE phrase ("museums OR galleries")
so "how many X or Y" unions the per-coordinate counts (each proven to resolve to 1 on the injected
clean data → union = gold 2).

FAIL-ON-OLD: old EventAnalysis has NO `venue` attribute (AttributeError) and the seams emit NO
located_in venue edge; old _count_type_candidates does not split a coordinated phrase. PASS-ON-FIX:
venue captured, located_in edge emitted, coordinate phrase split.

NON-VENUE NEGATIVES (must NOT mis-capture as a venue): an ADJ-governed topic PP ("interested IN art"),
a comparison PP ("similar TO James Parker" — also a PERSON, guarded), and a comitative "with X".

PURE — spaCy only (overlays degrade with no DB), no network/LLM/GLiNER2.
Run: SPACY_MODEL=en_core_web_sm python3 -m pytest tests/test_locative_venue_capture.py -q
(tests/ is gitignored → git add -f)
"""
import datetime as dt
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("OCCURRENCE_REIFY", "1")
os.environ.setdefault("EVENT_VENUE_CAPTURE", "1")

import src.api.main as m  # noqa: E402

_ling = pytest.importorskip("src.extraction.linguistics")

pytestmark = pytest.mark.skipif(
    not _ling.linguistics_available(), reason="spaCy model not installed"
)

_REF = dt.date(2023, 2, 1)


def _venues(text):
    """Every non-empty EventAnalysis.venue the deriver surfaces for ``text``."""
    return [
        (e.venue or "").strip().lower()
        for e in (_ling.analyze_events(text) or [])
        if getattr(e, "venue", None)
    ]


def _located_in_edges(text):
    """{venue: event_date} for every (user, located_in, venue) edge the live reified seam emits."""
    return {
        (e.get("object") or "").lower(): e.get("event_date")
        for e in m._detect_event_states_reified(text, _REF)
        if (e.get("rel_type") or "").lower() == "located_in"
    }


# ── DERIVER: the locative/goal venue is surfaced (obl:lmod static + obl:lto goal) ────────────────

@pytest.mark.parametrize(
    "text, venue",
    [
        # obl:lmod static locative "AT <place>" hanging off the eventive noun.
        ("I attended a guided workshop at the Modern Art Museum.", "modern art museum"),
        # obl:lto goal "TO <place>" hanging off the support verb (full nested-compound name kept).
        ("I took my niece to the Natural History Museum.", "natural history museum"),
        # generalizes to any named venue — no museum/gallery/domain word-list involved.
        ("I went to a concert at Madison Square Garden.", "madison square garden"),
    ],
)
def test_locative_venue_is_captured(text, venue):
    got = _venues(text)
    assert venue in got, f"venue {venue!r} not captured from {text!r}: got {got}"


def test_venue_not_mis_filed_as_occurrence_title():
    """The locative place is a VENUE, not the occurrence's NAME — the title seam must not steal it."""
    evs = _ling.analyze_events("I attended a guided workshop at the Modern Art Museum.")
    titles = [(e.title or "").strip().lower() for e in evs if e.title]
    assert "modern art museum" not in titles, (
        f"venue was mis-filed as the occurrence title: {titles}"
    )


# ── NON-VENUE NEGATIVES: a non-locative PP must NOT be mis-captured as a venue ────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "I'm interested in abstract art.",          # ADJ-governed TOPIC "in" — not a place
        "I recommend artists similar to James Parker.",  # comparison "to" + PERSON (guarded)
        "I had a meeting with Sarah.",              # comitative "with" co-participant, not a venue
        "I had lunch at the mall.",                 # common-noun locative (left to adjunct handling)
    ],
)
def test_non_venue_pp_not_captured_as_venue(text):
    assert _venues(text) == [], f"non-venue PP mis-captured as a venue in {text!r}: {_venues(text)}"


# ── LIVE SEAM: the venue rides a dated (user, located_in, <place>) edge typed Location ────────────

@pytest.mark.parametrize(
    "text, venue, iso_prefix",
    [
        ("I attended a guided workshop at the Modern Art Museum on February 15th 2023.",
         "modern art museum", "2023-02-15"),
        ("I took my niece to the Natural History Museum on February 8th 2023.",
         "natural history museum", "2023-02-08"),
    ],
)
def test_venue_emits_dated_located_in_edge(text, venue, iso_prefix):
    edges = _located_in_edges(text)
    assert venue in edges, f"no located_in venue edge for {venue!r}: got {edges}"
    assert str(edges[venue]).startswith(iso_prefix), (
        f"{venue}: expected date {iso_prefix}, got {edges[venue]}"
    )


def test_located_at_scalar_rel_not_used_for_venue():
    """The venue must land on the ENTITY-tail located_in, never the SCALAR-tail located_at (which
    would route the place to entity_attributes as a string, unreachable by the count/where walk)."""
    edges = m._detect_event_states_reified(
        "I attended a workshop at the Modern Art Museum on February 15th 2023.", _REF)
    rels = {(e.get("rel_type") or "").lower() for e in edges}
    assert "located_at" not in rels, "venue leaked onto the SCALAR-tail located_at rel"


# ── QUERY: a coordinated / disjunctive count TYPE phrase is split ("museums OR galleries") ────────

def test_count_type_phrase_disjunction_split():
    cands = m._count_type_candidates("different museums or galleries")
    assert "museum" in cands and "gallery" in cands, (
        f"coordinated count phrase not split into both coordinates: {cands}"
    )


def test_count_type_phrase_non_coordinated_not_over_split():
    """A single type token that merely CONTAINS 'or'/'and' must not be split apart."""
    cands = m._count_type_candidates("keyboards")
    # 'keyboards' / 'keyboard' only — never a bogus 'keyb'/'ds' coordinate fragment.
    assert all(c in ("keyboards", "keyboard") for c in cands), f"over-split a bare noun: {cands}"

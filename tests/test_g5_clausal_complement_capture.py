"""G5 — clausal / gerund complement capture (clean sub-cases ONLY).

CAPTURE-GAP G5 (LongMemEval single-session-preference cluster). A preference verb over an
EMBEDDED clause carries the payload, but the deriver kept only the matrix verb (or nothing) and
dropped the complement:

    "I prefer winding down by 9:30 pm."   → NOTHING emitted (payload = the activity "winding down")
    "I enjoy hiking in the mountains."    → NOTHING emitted (payload = "hiking")

UD grounding: the desired activity is an ``xcomp`` (open clausal complement) of the preference
verb. When that xcomp is a GERUND (``VBG`` / VerbForm=Ger|Part, NOT an infinitival "to VERB") and
governs NO direct object of its own, the ACTIVITY NAMED BY THE GERUND is the preferred thing. The
fix extends the EXISTING first-person desire lane (``analyze_desire_predication``,
src/extraction/linguistics.py, wired on the spine at src/api/main.py SPINE_AFFECT_PREFERENCE) to
HEAD-PROMOTE the gerund head + its phrasal particle ("winding down", "working out") as the
preference object — never the whole clause glued into one string, and never a trailing
PP / manner / time adjunct ("in the mountains", "by 9:30 pm").

⛔ THE LOAD-BEARING RULE — UNDER-CAPTURE OVER GARBAGE. The clean sub-case (first-person preference
+ intransitive gerund) is captured. Everything noisy emits NOTHING (nets to Class C), rather than a
garbage concatenated-clause object:
  • advisory 3rd-person subject ("MY DOCTOR recommended taking vitamin D") — the subject is the
    advisor, not the user; routing it through the first-person desire lane would mint the FALSE
    edge (user, recommend, …). Deferred → nothing here.
  • deeply-nested PP payload ("trying to learn more about … Adobe Premiere Pro") — matrix is not a
    preference verb, dobj is an ADJ ("more"), payload is ≥3 PPs deep. Folding it = garbage.

All deterministic, subject-agnostic, grammar-driven (the bounded volition-verb class + UD
xcomp/VBG morphology; NO domain / preference word zoo).
"""
import pytest

from src.extraction.linguistics import (
    analyze_desire_predication,
    linguistics_available,
)

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine seams no-op",
)


def _pairs(text):
    return {(e["rel_type"], e["object"]) for e in analyze_desire_predication(text)
            if not e.get("negated")}


# ── CLEAN sub-case: intransitive-gerund activity is the preference object ────────────────

@pytest.mark.parametrize("text,rel,obj", [
    ("I prefer winding down by 9:30 pm.", "prefer", "winding down"),  # phrasal verb + prt kept
    ("I enjoy hiking in the mountains.", "enjoy", "hiking"),          # PP adjunct excluded
    ("We prefer working out in the morning.", "prefer", "working out"),
    ("I like going hiking.", "like", "hiking"),                       # nested light gerund unwrapped
])
def test_intransitive_gerund_activity_captured(text, rel, obj):
    assert (rel, obj) in _pairs(text)


def test_gerund_activity_excludes_trailing_pp_and_time():
    # HEAD-PROMOTION, not clause-concat: the object is exactly the activity head (+ particle),
    # NEVER the whole clause with its PP / time adjunct folded in.
    objs = {o for _, o in _pairs("I prefer winding down by 9:30 pm.")}
    assert "winding down" in objs
    for bad in ("winding down by 9:30 pm", "9:30", "pm", "9:30 pm"):
        assert bad not in objs, f"garbage/adjunct object leaked: {bad!r}"


# ── NO REGRESSION: the transitive gerund / dobj path is untouched ────────────────────────

@pytest.mark.parametrize("text,rel,obj", [
    ("I enjoy playing video games.", "enjoy", "video games"),  # gerund WITH dobj → dobj wins
    ("I want to learn Spanish.", "want", "spanish"),           # infinitival WITH dobj
    ("I like jazz.", "like", "jazz"),                          # plain dobj
    ("I prefer reading books.", "prefer", "books"),
    ("I love swimming.", "love", "swimming"),                  # nominal gerund dobj
])
def test_transitive_path_unchanged(text, rel, obj):
    assert (rel, obj) in _pairs(text)


# ── UNDER-CAPTURE (the noisy sub-cases emit NOTHING, never garbage) ─────────────────────

def test_bare_infinitive_no_content_emits_nothing():
    # "want to go" / "prefer to relax": thin bare infinitive, no dobj → nothing (not "go"/"relax").
    assert analyze_desire_predication("I want to go.") == []
    assert analyze_desire_predication("I prefer to relax.") == []


def test_advisory_third_person_emits_no_false_user_edge():
    # NEGATIVE / NO-GARBAGE: the desire lane is first-person only. A 3rd-person advisory
    # ("my doctor recommended taking vitamin D") must NOT mint a false (user, recommend, …) edge,
    # nor a garbage clause object. It is UNDER-CAPTURED here (deferred to a proper advisor lane).
    edges = analyze_desire_predication("My doctor recommended taking vitamin D.")
    assert edges == [], f"advisory leaked a garbage/false edge: {edges}"


def test_deeply_nested_pp_payload_emits_no_garbage():
    # NEGATIVE / NO-GARBAGE: payload buried ≥3 PPs deep behind a non-preference matrix + ADJ dobj.
    # Must emit NOTHING — never a concatenated-clause object.
    text = ("I'm trying to learn more about advanced settings "
            "for video editing with Adobe Premiere Pro.")
    edges = analyze_desire_predication(text)
    assert edges == [], f"nested-PP clause leaked a garbage edge: {edges}"


def test_negated_gerund_preference_dropped():
    # Parity with the affect seams: negation-as-absence deferred → negated preference dropped.
    assert _pairs("I don't like winding down.") == set()

"""CHARACTERIZATION pins for the spine deriver on CESSATION constructions.

These are not correctness assertions — they pin what `derive_sentence_facts` ACTUALLY DOES
today, deterministically (verified stable over repeated runs), on three cessation shapes.
The perfect-flow gauntlet's negation bars (3.3 event/polarity, 3.5 zero reinforcement) are
only fully provable through the front-door battery on the spec'd brain; that run is gated to
the 2026-08-27 glm-5.3 quota reset. Until then these row-level pins give the reset-day work a
BASELINE it can diff against — a fix that improves any KNOWN-IMPERFECT case below will flip a
pin here loudly instead of silently.

Measured against en_core_web_sm (spaCy dependency parses drive every outcome). If a spaCy
model bump shifts a parse, a pin here changes — that is the intended alarm, not a flake.

WHAT THE THREE CASES ESTABLISH, and why the prose caveat was an approximation:
the earlier summary ("contracted stores a negated state, multiword stores nothing") is really
construction-by-construction. Adverbial-plus-direct-object goes SILENT; a phrasal verb emits
POSITIVE JUNK (worse than silence); adverbial-plus-PP DOES capture the cessation as a negated
state. All three are pinned so the reset-day owner sees the real surface, not a slogan.
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
    triples = {(e.subject, e.rel_type, e.object, bool(e.negated)) for e in edges}
    return triples, sorted(residue)


# ── CASE 1: adverbial cessation + direct object → SILENCE (the reinforcement WIN + the partial)

def test_adverbial_cessation_is_captured_negated_not_silent():
    """"I no longer drink coffee." — UPDATED, and the update is the point of the pin.

    This case previously pinned SILENCE ("no affirmative edge, but nothing negated either;
    the object drops to residue"), with its own docstring saying a future negated-capture fix
    must flip the assertion. That fix landed: the grandchild-aware negation index applied at the
    single ``_emit`` chokepoint. The negator in "no longer" hangs off the ADV ``longer``, which
    hangs off ``drink`` — a GRANDCHILD that every single-hop ``dep_ == "neg"`` test misses — so
    the habit used to be emitted AFFIRMED (reinforcement) and then, after a partial fix, not at
    all (silence). It is now CAPTURED and NEGATED, which is the actual product requirement:
    silence leaves the stale affirmative standing by omission, a negated row retires it."""
    triples, residue = _derive("I no longer drink coffee.")
    # No affirmative habit edge survives the cancellation (the original WIN, still held).
    assert ("user", "drink", "coffee", False) not in triples
    # And the cancellation is now CAPTURED, not dropped.
    assert ("user", "drink", "coffee", True) in triples
    assert not residue, f"nothing should be uncovered now: {residue}"


# ── CASE 2: phrasal-verb cessation → POSITIVE JUNK (KNOWN-IMPERFECT, the worst of the three) ──

def test_phrasal_verb_cessation_currently_emits_positive_junk():
    """"I gave up smoking." — spaCy heads the phrasal verb on the light verb "give", which
    carries no negation, so the deriver emits an AFFIRMATIVE (user, give, smoking). This is a
    mis-capture, NOT a cessation, and it is affirmative — the worst of the three shapes.
    Pinned as KNOWN-IMPERFECT: when a phrasal-cessation fix lands, this positive edge must
    disappear (or become negated), flipping this pin. Do not 'fix the test' — fix the deriver."""
    triples, _residue = _derive("I gave up smoking.")
    # STILL KNOWN-IMPERFECT, and deliberately still pinned. The phrasal cessation GROWTH lane
    # proposes only when the gerundive complement parses as a VERB; here en_core_web_sm tags
    # "smoking" as a NOUN, so the aspectual frame is not recognised and nothing is proposed or
    # contained. The sibling case with a verbal complement ("gave up practising on fridays") IS
    # recognised and proposes "give up" onto the growth rail — see the growth-lane tests.
    assert ("user", "give", "smoking", False) in triples  # current reality, deliberately pinned


# ── CASE 3: adverbial cessation + PP → cessation CAPTURED as a negated state (the good shape) ─

def test_adverbial_pp_cessation_captures_a_negated_state():
    """"The sable no longer meets on Tuesdays." — here the cessation IS captured: a NEGATED
    (sable, has_state, meet) edge is emitted (the polarity flip lane N exists for). It also
    emits an affirmative (sable, meet_on, tuesdays) alongside — the residual schedule assertion,
    pinned so its future suppression is visible. Both halves recorded, neither smoothed over."""
    triples, _residue = _derive("The sable no longer meets on Tuesdays.")
    assert ("sable", "has_state", "meet", True) in triples          # cessation captured, negated
    # UPDATED: the schedule edge alongside it used to stay AFFIRMED — pinned above as "the residual
    # schedule assertion … so its future suppression is visible". It is no longer affirmed: the
    # central grandchild-negation flip reaches BOTH edges of the clause, so the cancelled schedule
    # is retired rather than left standing beside its own cancellation.
    assert ("sable", "meet_on", "tuesdays", False) not in triples
    assert ("sable", "meet_on", "tuesdays", True) in triples

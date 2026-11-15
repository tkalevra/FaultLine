"""A date that RESOLVED but attached to no fact must be LOUD, not silent.

WHY THIS EXISTS. `extract_event_date` correctly resolves a date, and then the deriver drops it
on the floor without a single log line, because `_date_for_verb` binds a date only to the verb
that syntactically GOVERNS its PP — and for a relative-clause predicate the deriver emits no
fact at all. No claimant, no date, no trace.

Measured 2026-07-30 on the LME bench (question 2c63a862, a duration question):

    "I recently saw a house that I really love on 3/1."
        extract_event_date   -> 2022-03-01              resolved fine
        derive_sentence_facts-> (user, see, house)       event_date=None   <- DROPPED, silently

Both operands of that question were lost this way, so recall returned no dates at all and the
duration calc correctly reported it could not find dated records. The render was honest; the
capture was not.

The whole temporal suite (76 tests) was GREEN throughout, because nothing in it executes a
relative-clause-hosted date — "a green suite is not evidence" applied to a live data loss.

This file pinned the OBSERVABILITY, which is what was fixed first. It deliberately did NOT assert
the date got captured: the real repair was to emit the missing relative-clause fact (UD `acl:relcl`
— ClausIE, Del Corro & Gemulla WWW'13, derives clauses from relative clauses as a first-class
type), and re-homing the date onto some other verb would INVENT a fact — on
"The house I saw on March 1st really checks all the boxes" it would stamp Mar-1 onto a
present-tense stative.

⚠️ UPDATED 2026-07-30 — THAT CAPTURE FIX HAS LANDED (`SPINE_RELCL_PREDICATE`, chain
`_chain_relcl` in `src/extraction/linguistics.py`), so the expectation below is INVERTED exactly as
the note above instructed: the relative clause is now emitted, the date BINDS to it, and the
orphan CRIT correctly stops firing for this sentence. The observability guard itself is unchanged
and still covered — `test_no_false_positive_when_the_date_binds` pins that it stays quiet, and the
new `tests/test_spine_relcl_predicate.py` pins that the un-emitted-clause case is what fires it.
The thing that must NOT happen — Mar-1 landing on the present-tense stative "checks" — is asserted
directly below and in the new file.
"""
import datetime

import pytest

REF = datetime.datetime(2022, 3, 5)


def _facts(sentence):
    from src.extraction.linguistics import derive_sentence_facts
    return derive_sentence_facts(sentence, REF) or []


def _dates_on(facts):
    return {getattr(f, "event_date", None) for f in facts} - {None}


def test_date_resolves_even_when_it_is_dropped():
    """Precondition: the date layer is NOT the broken part — it resolves this correctly."""
    from src.extraction.linguistics import extract_event_date
    iso, gran = extract_event_date("I recently saw a house that I really love on 3/1.", REF)
    assert iso and iso.startswith("2022-03-01"), iso
    assert gran == "day"


# NOTE ON CAPTURE CHANNEL: this project logs via STRUCTLOG, which renders to stdout — pytest's
# `caplog` only sees stdlib logging records and stays EMPTY here. Asserting on caplog would
# silently pass/fail for the wrong reason (it failed for the wrong reason once already). Use
# capsys, which captures what structlog actually writes.
def test_relative_clause_date_now_binds_and_the_crit_goes_quiet(capsys):
    """FLIPPED once `_chain_relcl` landed: the relative clause is emitted, so the resolved date has
    a claimant and binds to it. The orphan CRIT must therefore go quiet for this sentence."""
    sentence = "I recently saw a house that I really love on 3/1."
    facts = _facts(sentence)
    assert facts
    assert "2022-03-01" in {str(d)[:10] for d in _dates_on(facts)}, (
        "the relative-clause capture fix must bind the date", [
            (f.subject, f.rel_type, f.object, getattr(f, "event_date", None)) for f in facts])
    out = capsys.readouterr()
    assert "date_resolved_but_unclaimed" not in (out.out + out.err), (
        "the date now has a claimant — the orphan CRIT must stop firing here")


def test_the_date_is_never_re_homed_onto_a_present_tense_stative():
    """The forbidden 'fix'. "The house I saw on March 1st really checks all the boxes" — March 1st
    belongs to the relative clause "I saw", NOT to the present-tense stative "checks"."""
    facts = _facts("The house I saw on March 1st really checks all the boxes.")
    dated = {(f.subject, f.rel_type, f.object): str(getattr(f, "event_date", None) or "")[:10]
             for f in facts}
    assert dated.get(("user", "see", "house")) == "2022-03-01", dated
    assert not dated.get(("house", "check", "boxes")), (
        "stamping the relcl's date onto the matrix stative INVENTS a fact", dated)


def test_no_false_positive_when_the_date_binds(capsys):
    """The guard must stay quiet on the ordinary case, or it becomes noise and gets ignored."""
    facts = _facts("I attended a workshop on January 10th.")
    assert any(str(getattr(f, "event_date", "") or "").startswith("2022-01-10") for f in facts), \
        "the plain adverbial-date case must still bind its date"
    out = capsys.readouterr()
    assert "date_resolved_but_unclaimed" not in (out.out + out.err), \
        "must NOT fire when the date attached — a noisy guard is a disabled guard"


def test_observability_never_breaks_capture():
    """Fail-safe: the guard is wrapped, so a malformed sentence still returns facts, not a crash."""
    for weird in ("", "   ", "on 3/1", "!!!", "The house I saw on March 1st checks the boxes."):
        try:
            _facts(weird)
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"derive_sentence_facts raised on {weird!r}: {exc!r}")

"""FIX C — RELATIVE-CLAUSE PREDICATES: emit the clause, and the orphaned date follows.

THE MEASURED LOSS (2026-07-30, offline, ref 2022-03-05):

    "I recently saw a house that I really love on 3/1."
        extract_event_date    -> 2022-03-01                    resolved fine
        derive_sentence_facts -> (user, see, house)  event_date=None      <- the date DROPPED

`_date_for_verb` binds a date only to the verb that syntactically GOVERNS its PP — here "love",
a relative-clause predicate for which the deriver emitted NO fact at all. No claimant, no date.
Both operands of a live LongMemEval duration question were lost exactly this way. The observability
for it shipped first (CRIT `linguistics.date_resolved_but_unclaimed`, ad32a90e); this is the
CAPTURE.

GROUNDING: UD `acl:relcl` (de Marneffe et al. 2021, Computational Linguistics 47(2)); ClausIE
(Del Corro & Gemulla, WWW'13 §3) derives clauses from relative clauses as a first-class clause type;
ISO-TimeML / ISO 24617-1 anchors a TIMEX3 to an EVENT via TLINK — with no EVENT emitted there is
nothing to anchor to.

⚠️ THE FORBIDDEN FIX, pinned here as a negative: re-homing an unclaimed date onto another verb. On
"The house I saw on March 1st really checks all the boxes" that stamps March-1 onto a present-tense
stative and INVENTS a fact. The date must land on the relative clause we now emit, and nowhere else.

Pure/offline: spaCy only (no DB, no network, no LLM).
"""
import datetime

import pytest

REF = datetime.datetime(2022, 3, 5)


def _facts(sentence):
    from src.extraction.linguistics import derive_sentence_facts
    return derive_sentence_facts(sentence, REF) or []


def _dated(facts):
    return {(f.subject, f.rel_type, f.object): str(getattr(f, "event_date", None) or "")[:10]
            for f in facts}


# Five UNRELATED sentences, five UNRELATED verbs, three different subjects (first-person, third-
# person pronoun, named third party). Object-gap with and without an overt relativizer.
@pytest.mark.parametrize("sentence,triple,date", [
    # overt relativizer "that", gap = object of the relative clause
    ("I recently saw a house that I really love on 3/1.", ("user", "love", "house"), "2022-03-01"),
    ("I bought a bike that I still ride on 4/2.", ("user", "ride", "bike"), "2022-04-02"),
    ("She mentioned a clinic that she visited on 2/8.", ("she", "visit", "clinic"), "2022-02-08"),
    ("I read the report that Sarah wrote in January.",
     ("sarah", "write", "report"), "2022-01-05"),
    # ZERO relativizer (bare gapped object) — the shape the deriver was completely blind to
    ("The house I saw on March 1st really checks all the boxes.",
     ("user", "see", "house"), "2022-03-01"),
    ("I kept the receipt that I found last Tuesday.", ("user", "find", "receipt"), "2022-03-01"),
])
def test_relative_clause_is_emitted_with_the_antecedent_and_its_date(sentence, triple, date):
    """The antecedent fills the gapped argument, and the date binds because the clause now exists."""
    dated = _dated(_facts(sentence))
    assert triple in dated, (sentence, dated)
    assert dated[triple] == date, (sentence, dated)


@pytest.mark.parametrize("sentence,stative", [
    ("The house I saw on March 1st really checks all the boxes.", ("house", "check", "boxes")),
])
def test_the_date_is_not_re_homed_onto_the_matrix_predicate(sentence, stative):
    """The date belongs to the relative clause. Stamping it on the matrix stative invents a fact."""
    dated = _dated(_facts(sentence))
    assert stative in dated, (sentence, dated)
    assert not dated[stative], (sentence, dated)


@pytest.mark.parametrize("sentence", [
    "I recently saw a house that I really love on 3/1.",
    "The house I saw on March 1st really checks all the boxes.",
    "I bought a bike that I still ride on 4/2.",
])
def test_no_orphaned_date_crit_remains(sentence, capsys):
    """The whole point: the observability CRIT must go quiet because the loss is repaired."""
    _facts(sentence)
    out = capsys.readouterr()
    assert "date_resolved_but_unclaimed" not in (out.out + out.err), (sentence, out.out[-800:])


def test_the_matrix_clause_is_still_emitted_unchanged():
    """Additive only — the main-clause edges the deriver already produced must survive."""
    dated = _dated(_facts("I recently saw a house that I really love on 3/1."))
    assert ("user", "see", "house") in dated
    assert not dated[("user", "see", "house")], "the matrix verb governs no date here"


def test_a_relcl_with_its_own_object_is_left_alone():
    """Not a gapped-object clause: "the man who runs the shop" has its own content object, so the
    antecedent must NOT be forced into the object slot (that would mint (man, run, man))."""
    got = {(f.subject, f.rel_type, f.object) for f in _facts("I met the man who runs the shop.")}
    assert ("man", "run", "man") not in got, got
    assert ("user", "run", "man") not in got, got


# NOTE ON HOW OFF⇒LEGACY IS PINNED: by comparing ON vs OFF IN THE SAME PROCESS, never by hardcoding
# a legacy surface — a hardcoded legacy triple is not stable across the full suite (other tests mutate
# module-level deriver state) and pinning one produced a false red once already.
def test_flag_off_removes_exactly_the_relative_clause(monkeypatch):
    """SPINE_RELCL_PREDICATE=off → the relcl edge (and its date) is gone; the matrix edge stays."""
    import src.extraction.linguistics as L
    sentence = "I recently saw a house that I really love on 3/1."
    on = _dated(_facts(sentence))
    monkeypatch.setattr(L, "SPINE_RELCL_PREDICATE", False)
    off = _dated(_facts(sentence))
    assert ("user", "love", "house") in on and ("user", "love", "house") not in off, (on, off)
    assert ("user", "see", "house") in on and ("user", "see", "house") in off, (on, off)
    assert set(off) == set(on) - {("user", "love", "house")}, (on, off)


def test_flag_off_is_identical_where_there_is_no_relative_clause(monkeypatch):
    """A sentence with no relative clause must be byte-identical with the flag either way."""
    import src.extraction.linguistics as L
    sents = ("I attended a workshop on January 10th.",
             "I bought a bike last April.",
             "My car's GPS broke last week.")
    on = {s: _dated(_facts(s)) for s in sents}
    monkeypatch.setattr(L, "SPINE_RELCL_PREDICATE", False)
    off = {s: _dated(_facts(s)) for s in sents}
    assert on == off, {k: (on[k], off[k]) for k in on if on[k] != off[k]}

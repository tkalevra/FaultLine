"""Unit tests for the FREQUENCY / RATE ADVERBIAL chain (G6 — LongMemEval frequency/rate capture).

THE GAP: a rate / recurrence adverbial — "I do yoga three times a week", "I go to the gym twice a
week", "we meet every Monday", "get a wax done every 3-4 months" — was DROPPED by the SVO backbone
(the "times"/"week" residue uncovered) or MIS-GLUED as the object ("meet every Monday" → junk
(user, meet, monday)). The rate is now captured as a SCALAR VALUE (the verbatim span
"three times a week") on the ACTIVITY ENTITY — the verb's own object (yoga / gym / wax) when it has
one, else the clause subject — rel = the user's own VERB LEMMA (a GROWN/novel rel — NO seeded
frequency/rate rel_type, NO period→rel map), routed to entity_attributes by the object's
``scalar_datatype`` marker (main.py ``_edge_is_scalar`` / "forced scalar: edge carries
object_datatype"). Object-attachment is required for correctness — a user-attached (user, do, <rate>)
would collide across two "do" activities on the entity_attributes UNIQUE (entity_id, attribute) key.

THE DISCRIMINATOR is pure GRAMMAR (dep/POS/morph), NO period/quantifier word list: an ``npadvmod``
NOUN/PROPN adverbial on a content verb carrying (A) a count-over-period ("N times a/per period"),
(B) a multiplicative predet ("once/twice a period"), or (C) a distributive DET with NO PronType
("every/each period" — an article "a/the" is PronType=Art and a deictic "this/that" is PronType=Dem,
both excluded). A resolved WHEN ("last week") is firewalled via the dateparser date-token peel.

PURE tests — no DB, no network, no GLiNER2, no LLM; they call the deriver DIRECTLY on the real spaCy
parse.

Run: python3 -m pytest tests/test_spine_rate_adverbial.py -q   (tests/ is gitignored → git add -f)
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402


def _facts(sentence, reference=None):
    """(subject, rel_type, object, scalar_datatype) tuples for one clean sentence."""
    return [(f.subject, f.rel_type, f.object, f.scalar_datatype)
            for f in derive_sentence_facts(sentence, reference=reference)]


def _rate_scalar_on(sentence, subject, value, reference=None):
    """True iff SOME emitted fact is a SCALAR (scalar_datatype set) on ``subject`` whose object
    contains the rate ``value`` (the frequency was captured, not dropped or mis-glued)."""
    subject = subject.lower()
    value = value.lower()
    for s, _r, obj, sdt in _facts(sentence, reference):
        if s == subject and sdt and value in (obj or "").lower():
            return True
    return False


def _has_scalar_containing(sentence, value, reference=None):
    value = value.lower()
    return any(sdt and value in (obj or "").lower() for _s, _r, obj, sdt in _facts(sentence, reference))


# ── CAPTURE: the rate lands as a scalar on the ACTIVITY entity (the verb's object) ────────────────
@pytest.mark.parametrize(
    "sentence,subject,value",
    [
        # count-over-period "N times a week" → on the dobj activity
        ("I do yoga three times a week.", "yoga", "three times a week"),
        # compound object → the FULL NP carries the scalar ("yoga classes", not bare "classes")
        ("I attend yoga classes three times a week.", "yoga classes", "three times a week"),
        # multiplicative "twice a week" → on the governed-prep pobj activity
        ("I go to the gym twice a week.", "gym", "twice a week"),
        # multiplicative "once a month"
        ("I visit the dentist once a year.", "dentist", "once a year"),
        # distributive "every <period>" on a dobj
        ("I water the plants every day.", "plants", "every day"),
        # distributive "every week" on a relcl host noun ("the book I read")
        ("I read the book every week.", "book", "every week"),
        # "each" distributive
        ("I check the mailbox each morning.", "mailbox", "each morning"),
    ],
)
def test_rate_captured_as_scalar_on_activity(sentence, subject, value):
    """The full rate span is captured as a SCALAR on the activity entity (the verb's object)."""
    assert _rate_scalar_on(sentence, subject, value), (
        f"rate {value!r} not captured as a scalar on {subject!r} for {sentence!r}: {_facts(sentence)}"
    )


# ── The RELATIONAL activity edge SURVIVES alongside the rate scalar (they co-locate on the object) ─
def test_relational_edge_survives_with_rate():
    facts = _facts("I do yoga three times a week.")
    rels = {(s, r, o) for s, r, o, sdt in facts if not sdt}
    assert ("user", "do", "yoga") in rels, f"activity link dropped: {facts}"
    assert _rate_scalar_on("I do yoga three times a week.", "yoga", "three times a week")


# ── COPULA-RELATIVE HOST (LME-945e3d21): the rate rides a relcl copula "which is N times a period" ──
@pytest.mark.parametrize(
    "sentence,subject,value",
    [
        # the LongMemEval knowledge-update phrasing — rate in a non-restrictive relative clause
        ("I attend yoga classes, which is three times a week.", "yoga classes", "three times a week"),
        # multiplicative rate in the same relcl-copula host
        ("I visit my parents, which is twice a month.", "parents", "twice a month"),
    ],
)
def test_rate_in_relative_clause_copula(sentence, subject, value):
    """A recurrence adverbial expressed as the attr complement of a relative-clause copula
    ("<activity>, which is <N times a period>") is captured as a scalar on the activity entity —
    the same reachable shape as the direct npadvmod form, so "how often" surfaces it."""
    assert _rate_scalar_on(sentence, subject, value), (
        f"relcl-copula rate {value!r} not captured on {subject!r} for {sentence!r}: {_facts(sentence)}"
    )


# ── A copula MEASURE (no governed period) is NOT a rate — "my commute is 45 minutes" stays a scalar ─
def test_copula_measure_not_mistaken_for_rate():
    """A plain measure attr ("45 minutes" — a NUM+unit with no per-period child) must NOT be picked
    up by the copula-relative rate branch; the recurrence signature fails, so no rate is emitted."""
    facts = _facts("My commute, which is 45 minutes, is long.")
    # no fact carries the rate-style "per week/month/..." — only the measure scalar exists
    assert not any(sdt and "times a" in (obj or "").lower() for _s, _r, obj, sdt in facts), facts


# ── INTRANSITIVE: no object → attach to the clause subject AND suppress the mis-glued period twin ──
def test_intransitive_rate_on_subject_no_misglue():
    facts = _facts("We meet every Monday.")
    # the rate lands as a scalar on the user
    assert _rate_scalar_on("We meet every Monday.", "user", "every monday"), facts
    # the mis-glued relational (user, meet, monday) twin is SUPPRESSED
    rels = {(s, r, o) for s, r, o, sdt in facts if not sdt}
    assert ("user", "meet", "monday") not in rels, f"period mis-glued as object: {facts}"


# ── PARTICIPLE / CONJUNCT nested rate: the shared object carries the scalar ────────────────────────
def test_participle_nested_rate_on_shared_object():
    s = "I get a wax and detailing done every 3-4 months."
    assert _has_scalar_containing(s, "every 3-4 months"), _facts(s)
    # it attaches to the activity object "wax", not spuriously to a function word
    assert _rate_scalar_on(s, "wax", "every 3-4 months"), _facts(s)


# ── TEMPORAL FIREWALL (dual-clock): a WHEN is NEVER captured as a rate ─────────────────────────────
@pytest.mark.parametrize(
    "sentence,when_word",
    [
        ("I read the book last week.", "last week"),   # deictic ADJ amod → resolved date
        ("I saw the movie this week.", "this week"),    # PronType=Dem → excluded
        ("I met her that day.", "that day"),            # PronType=Dem → excluded
        ("I finished it a week ago.", "a week ago"),    # resolved date span
    ],
)
def test_when_not_captured_as_rate(sentence, when_word):
    """A specific WHEN must never emit a rate scalar (it belongs to the temporal lane)."""
    ref = datetime.datetime(2026, 7, 16)
    for _s, _r, obj, sdt in _facts(sentence, ref):
        assert not (sdt and "week" in (obj or "").lower() and "every" not in (obj or "").lower()
                    and "time" not in (obj or "").lower()), (
            f"a WHEN was captured as a rate scalar for {sentence!r}: {_facts(sentence, ref)}"
        )


# ── UNDER-CAPTURE guard: a bare count with NO distributive period is NOT a rate (safe drop) ────────
@pytest.mark.parametrize(
    "sentence",
    [
        "I visited Paris three times.",   # bare count, no "a/per <period>" → not a rate
        "I waited a moment.",             # no count/distributive marker
    ],
)
def test_bare_count_or_plain_adverbial_not_a_rate(sentence):
    """No governed-period / distributive marker → we do NOT invent a rate scalar (safe under-capture)."""
    # (a bare duration on a DOBJ like "I waited three hours" is G2/duration territory, not tested here)
    for _s, r, obj, sdt in _facts(sentence):
        assert not (sdt and ("times" in (obj or "").lower() and "a " not in (obj or "").lower())), (
            f"bare count spuriously captured as a rate for {sentence!r}: {_facts(sentence)}"
        )


# ── SUBJECT-AGNOSTIC: an arbitrary (non-first-person) subject + arbitrary domain works the same ────
def test_subject_agnostic_third_person_and_arbitrary_domain():
    # third-person subject, non-"yoga" domain — no first-person, no domain vocabulary involved
    assert _rate_scalar_on("Sarah takes the medication twice a day.", "medication", "twice a day"), \
        _facts("Sarah takes the medication twice a day.")
    assert _rate_scalar_on("The team deploys the build every Friday.", "build", "every friday"), \
        _facts("The team deploys the build every Friday.")


# ── NEGATION: a negated clause defers (no rate emitted) — parity with the SVO/state chains ─────────
def test_negated_clause_defers():
    facts = _facts("I do not go to the gym every day.")
    assert not any(sdt and "every day" in (obj or "").lower() for _s, _r, obj, sdt in facts), facts

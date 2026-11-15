"""Regression gate for TWO independently-reproduced live defects on the identifier lane.

Both were found by driving the REAL seams, not by reading code, and both were confirmed on
`saas` @ f15ffcb5 before a line was changed.

BUG 1 — A SEEDED CUE THAT COULD NEVER FIRE.
Migration 186 seeds `id` into the per-tenant `linguistic_cues` `identifier_noun` class. Every
cue consumer matches by spaCy LEMMA/TEXT, and spaCy's English tokenizer holds an EXCEPTION
mapping `id`/`Id` to the apostrophe-less "I'd" contraction, so lowercase `id` tokenizes to
['i','d'] and the lemma `id` never exists. Measured before the fix:
    "my order id is AB123"  -> [('user','owns','order'), ('order','also_known_as','i')]
i.e. no identifier captured at all, plus a junk alias minted from the shard. `token_match`
CANNOT fix this (spaCy applies special cases first — verified empirically), which is why the
existing `_install_identifier_tokenizer` did not already cover it.

The uppercase form was dead too, for a SECOND reason: spaCy tags `ID`/`Id` as PROPN, and
`_identifier_context_binding` gated its head on `pos_ != "NOUN"`. Measured before the fix:
    "my ID is 4471"  -> [('user','owns','id'), ('id','age','4471')]
— the cue noun became an ENTITY carrying an AGE of 4471.

FIX: (a) `_reconcile_cue_tokenizer_exceptions` removes any tokenizer special case whose ORTH
casefolds to a DB-held cue surface and expands to >1 token — driven entirely by the cue
inventory, no word list, monotonic (only removes), loud, fail-safe; (b) the head-POS gate is a
closed UPOS inventory `{NOUN, PROPN}` — the DB cue class is the discriminator, not the tagger's
capitalisation guess.

BUG 2 — AN ATOMIC PATTERN THAT CLAIMS HALF A VALUE, AND AN EQUALITY TEST THAT LETS IT THROUGH.
Migration 185's identifier pattern requires >= 2 digits, so on "My postal code is N2G 3M1." it
rejects "N2G" (one digit) and matches "3M1" ALONE. Every downstream guard compared values by
EQUALITY, so "3m1" never read as already-hosted by the correct "n2g 3m1" and a SECOND, WRONG
has_reference_id was minted beside it. Confirmed end to end: when the half-value write lands
first, the scalar is pinned to "3m1" and the correct full value is then REFUSED — recall answers
"your reference identifier is 3m1".

FIX: `_atomic_claim_is_run_fragment` — a claim that is a PROPER FRAGMENT of an identifier RUN
already hosted is an overclaim and is dropped at all three claim guards. Containment, not
equality. One-directional: only the shorter CLAIM is ever dropped, never the longer capture.

NOTE: every fixture value is a synthetic placeholder of a structural shape — none is real data.
"""
import warnings

import pytest

warnings.filterwarnings("ignore")  # spaCy/torch CUDA warnings are noise in the test env

from src.api.main import (  # noqa: E402
    _atomic_claim_is_run_fragment,
    _is_identifier_run,
    _suppress_atomic_claimed_twins,
)
from src.extraction.linguistics import derive_sentence_facts, linguistics_available  # noqa: E402

spine = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine seams no-op",
)


def _facts(text):
    return [(f.subject, f.rel_type, f.object) for f in derive_sentence_facts(text, "user")]


# ── BUG 1: the `id` cue actually fires, in every casing the user might write ──────────

@spine
@pytest.mark.parametrize("text,value", [
    ("my order id is AB123", "ab123"),   # lowercase — the tokenizer-shattered form
    ("my order ID is AB123", "ab123"),   # uppercase — the PROPN-tagged form
    ("my order Id is AB123", "ab123"),   # title case — shattered AND PROPN
    ("my id is 7788", "7788"),           # bare strong cue, pure-numeric value
])
def test_identifier_cue_id_fires_in_every_casing(text, value):
    """`id` is a seeded identifier_noun cue; it must reach the identifier chain however written."""
    facts = _facts(text)
    assert ("user", "has_reference_id", value) in facts, \
        f"the seeded `id` cue did not capture {value!r} for {text!r}; got {facts}"


@spine
def test_id_cue_no_longer_mints_a_shard_alias_or_a_bogus_age():
    """The two concrete corruptions the dead cue produced, asserted as ABSENT.

    Before the fix "my order id is AB123" minted ('order','also_known_as','i') — the tokenizer
    shard bound as an alias of the cue noun — and "my ID is 4471" minted ('id','age','4471').
    """
    facts = _facts("my order id is AB123")
    assert not any(o == "i" for (_s, _r, o) in facts), \
        f"a tokenizer shard was captured as a value; got {facts}"

    facts = _facts("my ID is 4471")
    assert not any(r == "age" for (_s, r, _o) in facts), \
        f"the cue noun was given an AGE — the identifier chain did not own the clause; got {facts}"
    assert ("user", "has_reference_id", "4471") in facts, facts


@spine
def test_non_identifier_possessive_copula_is_untouched():
    """CONTROL — widening the head-POS gate must not make a non-cue clause fire.

    "my favorite number is 7" carries only the AMBIGUOUS 'suffix' cue (`number`), never a
    'strong' one, so the identifier chain must still decline and the preference/scalar lane keeps
    the clause. This is the guard that stops a bare count being filed as an identifier.
    """
    facts = _facts("my favorite number is 7")
    assert not any(r == "has_reference_id" for (_s, r, _o) in facts), \
        f"a plain count was mis-filed as a reference identifier; got {facts}"


def test_cue_tokenizer_reconciliation_is_monotonic_and_fail_safe():
    """The reconciler only ever REMOVES rules, is idempotent, and never raises."""
    from src.extraction.linguistics import _reconcile_cue_tokenizer_exceptions

    class _FakeTokenizer:
        def __init__(self):
            self.rules = {"id": [{}, {}], "wontbeacue": [{}, {}], "solo": [{}]}

    class _FakeNlp:
        def __init__(self):
            self.tokenizer = _FakeTokenizer()

    nlp = _FakeNlp()
    before = dict(nlp.tokenizer.rules)
    _reconcile_cue_tokenizer_exceptions(nlp)
    after = nlp.tokenizer.rules
    # monotonic: never adds, never rewrites an expansion it kept
    assert set(after) <= set(before)
    for k, v in after.items():
        assert v is before[k]
    # a SINGLE-token special case changes no boundary and is always kept, cue or not
    assert "solo" in after
    # idempotent
    _reconcile_cue_tokenizer_exceptions(nlp)
    assert set(nlp.tokenizer.rules) == set(after)

    # fail-safe: a broken pipeline object must not raise
    class _Exploding:
        @property
        def tokenizer(self):
            raise RuntimeError("boom")

    assert _reconcile_cue_tokenizer_exceptions(_Exploding()) == 0


# ── BUG 2: an atomic claim covering only PART of a captured run is an overclaim ───────

@pytest.mark.parametrize("value,expected", [
    ("n2g 3m1", True),      # every part identifier-shaped AND digit-bearing
    ("k1a 0b1", True),
    ("abc-12345", True),    # single part, separators admitted
    ("ab123 thing", False),  # "thing" carries no digit → prose, not a run
    ("my code", False),
    ("", False),
])
def test_is_identifier_run(value, expected):
    assert _is_identifier_run(value) is expected


def test_fragment_claim_is_detected_against_a_captured_run():
    hosted = ["n2g 3m1"]
    assert _atomic_claim_is_run_fragment("3M1", hosted) is True
    assert _atomic_claim_is_run_fragment("N2G", hosted) is True
    # the WHOLE value is never a fragment of itself — the guard is one-directional
    assert _atomic_claim_is_run_fragment("n2g 3m1", hosted) is False
    # an unrelated value is untouched
    assert _atomic_claim_is_run_fragment("ZZ99", hosted) is False


def test_a_code_embedded_in_PROSE_is_still_claimed():
    """THE SAFETY DIRECTION. The atomic lane exists to rescue a value the deriver swallowed
    inside a bigger phrase; the fragment guard must NEVER take that away. Only a run whose EVERY
    part is digit-bearing counts, so ordinary prose containing a code does not trigger it."""
    assert _atomic_claim_is_run_fragment("ab123", ["ab123 thing"]) is False
    assert _atomic_claim_is_run_fragment("abc-12345", ["my ticket abc-12345 was filed"]) is False


@spine
def test_postal_shaped_value_yields_ONE_identifier_not_two():
    """THE HEADLINE CASE, driven through the real seam order: deriver -> fragment guard.

    The assertion that matters is the NEGATIVE one — the spurious half-value claim is ABSENT,
    not merely that the good one is present (both co-existed before the fix).
    """
    import psycopg2
    import os
    from src.api.main import _detect_atomic_values

    text = "My postal code is N2G 3M1."
    edges = [{"subject": f.subject, "rel_type": f.rel_type, "object": f.object}
             for f in derive_sentence_facts(text, "user")]
    assert ("user", "has_reference_id", "n2g 3m1") in [
        (e["subject"], e["rel_type"], e["object"]) for e in edges], edges

    dsn = os.environ.get("POSTGRES_DSN", "")
    conn = None
    if dsn:
        try:
            conn = psycopg2.connect(dsn)
        except Exception:  # noqa: BLE001 — bootstrap patterns cover the DB-down case
            conn = None
    try:
        atomic = [a for a in (_detect_atomic_values(text, conn) or [])
                  if (a.get("type") or "").lower() != "date"]
    finally:
        if conn is not None:
            conn.close()

    hosted = [e.get("object") or "" for e in edges]
    kept = [a for a in atomic
            if not _atomic_claim_is_run_fragment(a.get("value"), hosted)]
    survived, _dropped = _suppress_atomic_claimed_twins(list(edges), kept)

    values = {(e.get("object") or "").strip().lower()
              for e in survived if e.get("rel_type") == "has_reference_id"}
    values |= {(a.get("value") or "").strip().lower() for a in kept}
    assert "n2g 3m1" in values, f"the whole value was lost; got {values}"
    assert "3m1" not in values, \
        f"the HALF-VALUE identifier survived beside the whole one; got {values}"

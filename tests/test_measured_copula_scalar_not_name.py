"""MEASURED-COPULA-SCALAR cluster (LME 118b2229 / ad7109d1 / 726462e0).

The user states a MEASURED value in a possessive-attribute copula — "my <NP> is/was <NUMBER UNIT>":

  118b2229  "My daily commute to work is 45 minutes each way."  GOLD 45 minutes
  ad7109d1  "My new internet plan is 500 Mbps."                 GOLD 500 Mbps
  726462e0  "My first-purchase discount was 10%."               GOLD 10%

CAPTURE (already correct on HEAD): the possessive-attribute scalar seam (``_attr_scalar_binding``
copula branch, digit-bearing SCALAR-LITERAL gate) captures the value VERBATIM as the possessed-NP
scalar — (user, daily_commute, "45 minutes each way") etc., routed to entity_attributes.

THE DEFECT THIS PINS — the co-emitted HARD-LINE junk twin: the shared (ProperName ↔ Type) binding
detector ``_bound_name_for_type`` (copula branch 3) accepted a NUM-quantified NOUN complement
("45 minutes", "500 Mbps", "10 %") as a NAME, so ``_chain_object_named_value`` minted
(daily commute, also_known_as, "minutes") — registering a bare UNIT as an alias of the concept.
THE HARD LINE: a value/unit is a memory, NEVER a place/name. A measured quantity is a scalar,
never a proper name.

THE FIX (deterministic, subject-agnostic, NO unit/domain word list): ``_bound_name_for_type``
branch 3 now rejects a complement carrying a ``nummod`` NUM child — a measured quantity is a
scalar owned by the attr-scalar / copula-measure seams, not a name. A genuine name ("Sam", "Sarah",
"Rex") carries no ``nummod`` → unaffected.

fail-on-old:
  * Pre-fix, ``derive_sentence_facts("My new internet plan is 500 Mbps.")`` emitted BOTH the scalar
    AND the junk (new internet plan, also_known_as, "mbps"); ``derive_sentence_facts("My address is
    123 Main Street, ...")`` emitted (address, also_known_as, "main street"). The measured-value
    ``also_known_as`` assertions below FAILED on old code, PASS on the fix.
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts, linguistics_available  # noqa: E402

requires_model = pytest.mark.skipif(
    not linguistics_available(), reason="en_core_web_sm not installed in test env")


def _facts(sentence, reference=None):
    return [(f.subject, f.rel_type, (f.object or "").lower(), f.scalar_datatype)
            for f in derive_sentence_facts(sentence, reference=reference)]


# (sentence, gold-value-fragment) — the MEASURED-COPULA-SCALAR cluster exemplars.
_MEASURED = [
    ("My daily commute to work is 45 minutes each way.", "45 minute"),
    ("My new internet plan is 500 Mbps.", "500 mbp"),
    ("My first-purchase discount was 10%.", "10%"),
]


@requires_model
@pytest.mark.parametrize("sentence,gold", _MEASURED)
def test_measured_value_lands_as_np_scalar(sentence, gold):
    """The measured value is captured VERBATIM as a SCALAR on the user (the possessed-NP key),
    so recall can surface it. (Capture side — must not regress.)"""
    facts = _facts(sentence)
    scalar_hit = [
        (s, r, o) for (s, r, o, sdt) in facts
        if s == "user" and sdt and gold.split()[0] in o
    ]
    assert scalar_hit, f"measured value {gold!r} not captured as a user scalar: {facts}"


@requires_model
@pytest.mark.parametrize("sentence,gold", _MEASURED)
def test_measured_unit_is_never_filed_as_a_name(sentence, gold):
    """THE HARD LINE (fail-on-old): a measured quantity is a scalar VALUE, never a proper NAME.
    No ``also_known_as`` edge may register the bare unit as an alias of the possessed concept."""
    facts = _facts(sentence)
    aka = [(s, r, o) for (s, r, o, _sdt) in facts if r == "also_known_as"]
    assert not aka, f"measured value mis-filed as a name (HARD-LINE junk twin): {aka}"


@requires_model
def test_address_scalar_no_name_twin():
    """A digit-bearing street address is a SCALAR VALUE; its street fragment must not be filed as a
    name of the abstract concept 'address' (same nummod-NUM junk class). fail-on-old:
    (address, also_known_as, "main street") was emitted."""
    facts = _facts("My address is 123 Main Street, Hamilton, Ontario.")
    assert any(s == "user" and r == "address" and sdt for (s, r, o, sdt) in facts), facts
    assert not [(s, r, o) for (s, r, o, _s) in facts if r == "also_known_as"], facts


@requires_model
def test_genuine_name_binding_preserved():
    """Guard against over-suppression: a genuine copula NAME ("my dog is Rex") carries no nummod →
    the naming binding MUST survive (dog, also_known_as, rex)."""
    facts = _facts("My dog is Rex.")
    assert ("dog", "also_known_as", "rex") in [(s, r, o) for (s, r, o, _s) in facts], facts

"""Regression: the perfect/progressive AUXILIARY ``have``/``has`` must NOT be misread as the
possession verb by ``_chain_has_attr_value`` (inside ``derive_sentence_facts``).

BUG (LME gpt4_6ed717ea): "I've been using those eco-friendly training pads from Chewy.com" was
captured as the junk scalar ``(user, using_eco_friendly_training_pads, chewy.com)`` — the aspect
auxiliary "have" (``dep_=="aux"`` of the participle "using") was treated as the possession verb
"have", the content between it and a scalar-shaped token (the domain "chewy.com", which matches the
FQDN value shape) became the "attribute", and the domain became the "value". The real object
("training pads") was buried inside the rel_type string and never surfaced to recall — so the gold
token "pad" could never be matched.

FIX (src/extraction/linguistics.py, ``_chain_has_attr_value`` value collection): a genuine
"SUBJECT has ATTR VALUE" value is NEVER the object of a circumstantial preposition — it abuts its
attribute noun (dobj / nummod / appos) or rides the appositional/genitive "of" ("has ip address OF
10.0.0.1"). So a scalar value that is the ``pobj`` of a preposition whose lemma is not "of" (the
"from"/"at"/"with" source/locative adjunct of the aux'd content verb) is rejected; the clause then
falls through to the SVO lane, which captures the real object. The value's VERB head is deliberately
NOT gated on — spaCy mis-tags the attribute noun itself as a VERB ("core-1 has IP 10.0.0.1" → "ip"
ROOT VERB), so a VERB-head test would drop the very scalar the chain exists to capture.

Subject-agnostic, deterministic (dep_/pos_ + the closed-class genitive link "of"), no domain words.

  - FAIL-ON-OLD: the pathological sentences emit a fused ``using_*`` rel with the domain as object
    and NO object containing "pad" → both assertions fail on the pre-fix code.
  - PASS-ON-FIX + NO-REGRESSION: the genuine network/id scalar patterns (ip/mac/hostname/email,
    incl. the "of" bridge) still emit their scalar edge exactly as before.

Run: python3 -m pytest tests/test_spine_progressive_aux_has_attr.py -q
"""
import pytest

import src.extraction.linguistics as ling


def _model_available() -> bool:
    return ling.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(
    not _HAS_MODEL, reason="en_core_web_sm not installed in test env")


def _facts(sent):
    return ling.derive_sentence_facts(sent, reference=None)


# ── the bug: progressive/perfect aux must not fuse the object into the predicate ──
@requires_model
@pytest.mark.parametrize("sent", [
    "I have been using those eco-friendly training pads from Chewy.com.",
    "I have been using training pads from Chewy.com.",
])
def test_progressive_aux_does_not_fuse_object_into_predicate(sent):
    facts = _facts(sent)
    rels = [(f.rel_type or "").lower() for f in facts]
    objs = [str(f.object or "").lower() for f in facts]
    # FAIL-ON-OLD: old code emits the fused junk rel "using_..._training_pads" (domain as object).
    assert not any(r.startswith("using_") for r in rels), (rels, objs)
    # PASS-ON-FIX: the real object ("training pads") is captured, not buried in the rel_type.
    assert any("pad" in o for o in objs), (rels, objs)


# ── no-regression: a perfect aux over a plain verb no longer mints a junk scalar ──
@requires_model
def test_perfect_aux_plain_verb_no_junk_scalar():
    facts = _facts("We have purchased items from store.com.")
    rels = [(f.rel_type or "").lower() for f in facts]
    # old code minted (user, purchased_items, store.com); the fused-predicate junk must be gone.
    assert not any("_items" in r or "purchased_" in r for r in rels), \
        [(f.rel_type, f.object) for f in facts]


# ── no-regression: the genuine "SUBJECT has ATTR VALUE" scalar cluster is preserved ──
@requires_model
@pytest.mark.parametrize("sent,attr_frag,value", [
    ("core-1 has ip address 10.0.0.1.", "ip", "10.0.0.1"),
    ("core-1 has ip address of 10.0.0.1.", "ip", "10.0.0.1"),          # appositional "of" bridge
    ("core-1 has hostname core1.example.com.", "hostname", "core1.example.com"),
    ("Sample smp-7 has mac 00:1A:2B:3C:4D:5E.", "mac", "00:1a:2b:3c:4d:5e"),
    ("I have an email address john@example.com.", "email", "john@example.com"),
])
def test_genuine_has_attr_scalar_preserved(sent, attr_frag, value):
    facts = _facts(sent)
    hit = [
        (f.rel_type, f.object) for f in facts
        if attr_frag in (f.rel_type or "").lower() and value in str(f.object or "").lower()
    ]
    assert hit, [(f.rel_type, f.object) for f in facts]

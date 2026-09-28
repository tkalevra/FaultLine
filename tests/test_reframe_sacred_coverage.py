"""Unit tests for the SET-LEVEL NO-DROP guard in src/extraction/reframe.py.

THE GAP (LongMemEval e47becba, single-session-user): "I graduated with a degree in Business
Administration, which has definitely helped me in my new role." The LLM atomizer split the
relative clause and, in doing so, DROPPED the nominal PP-complement "in Business Administration"
from the degree atom — emitting only "I graduated with a degree". Every atom passed the per-atom
guardrail (a DROP is a pure token-SUBSET: no invented token, no altered literal, subject kept),
so the degree FIELD — the answer — never reached the deterministic spine and recall surfaced only
"Degree".

THE FIX: a SET-LEVEL coverage check over the FULL atom set. SACRED source tokens — PROPER NOUNS
(names/titles) ∪ digit-bearing LITERALS (numbers/dates/IPs/values) — must each survive somewhere
in the atom union. A dropped sacred token means the atomization lost user truth → discard the LLM
atomization → caller falls back to the LOSSLESS deterministic segmentation.

PURE tests (no DB / no network / no LLM). The PROPN discriminator needs the real spaCy model, so
SPACY_MODEL is set (fltest sets it too).

Run: python3 tools/fltest.py --bug LME-e47becba --test tests/test_reframe_sacred_coverage.py --no-publish
"""
import os

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import reframe as R  # noqa: E402


def _atoms(*texts):
    return [R.Atom(text=t, source_span=t) for t in texts]


class TestSacredSourceTokens:
    def test_proper_noun_is_sacred(self):
        sac = R._sacred_source_tokens("I graduated with a degree in Business Administration.")
        assert "business" in sac and "administration" in sac

    def test_literal_is_sacred(self):
        sac = R._sacred_source_tokens("My server ip is 10.0.0.21.")
        assert "10.0.0.21" in sac

    def test_common_nouns_not_sacred(self):
        sac = R._sacred_source_tokens("I graduated with a degree.")
        # "degree" is a common noun — de-noiseable, NOT sacred; nothing to protect here.
        assert "degree" not in sac


class TestDropDetection:
    def test_dropped_proper_noun_flagged(self):
        raw = "I graduated with a degree in Business Administration, which has helped me."
        # Atomizer dropped "in Business Administration".
        dropped = R._atomization_drops_sacred_token(raw, _atoms("I graduated with a degree."))
        assert "business" in dropped or "administration" in dropped

    def test_covered_proper_noun_not_flagged(self):
        raw = "I graduated with a degree in Business Administration, which has helped me."
        dropped = R._atomization_drops_sacred_token(
            raw, _atoms("I graduated with a degree in Business Administration.",
                        "The degree in Business Administration has helped me."))
        assert dropped == set()

    def test_dropped_number_flagged(self):
        raw = "We have three kids: Mia who is 10 and Theo who is 12."
        # Split lost Theo's age.
        dropped = R._atomization_drops_sacred_token(
            raw, _atoms("We have kids Mia and Theo.", "Mia is 10."))
        assert "12" in dropped

    def test_all_names_and_numbers_covered_ok(self):
        raw = "We have three kids: Mia who is 10 and Theo who is 12."
        dropped = R._atomization_drops_sacred_token(
            raw, _atoms("We have kids Mia and Theo.",
                        "Mia is 10.", "Theo is 12."))
        assert dropped == set()


class TestReframeReturnsEmptyOnDrop:
    """The public contract: a sacred-token drop makes reframe DISCARD the atomization (empty),
    so the harvest falls back to the lossless deterministic segmentation."""

    def test_empty_atoms_survive_everything(self):
        # No atoms at all → nothing covered → every sacred token 'dropped'. The guard treats an
        # empty LLM result as a drop candidate, but reframe_to_atomic returns [] on empty BEFORE
        # the guard, so this only asserts the pure helper's behavior.
        raw = "My name is Carol."
        dropped = R._atomization_drops_sacred_token(raw, [])
        assert "carol" in dropped

    def test_flag_default_on(self):
        assert R.reframe_sacred_coverage_enabled() is True

"""A REALIZED implicative narrates what the user DID — it must not erase the clause.

THE BUG. `_aspectual_activity_xcomp` rejected ANY xcomp carrying an infinitival `to`, on the
stated grounds that "want TO buy / plan TO visit is UNREALIZED INTENT". Correct for
non-implicatives, wrong for implicatives — and a flat `to` test cannot tell them apart. So an
entire clause (event, object AND date) was erased whenever the user phrased something they did
as a realized obligation. Measured 2026-07-30 on the LME bench:

    "I had to take my stand mixer to a repair shop last month."  -> NO FACTS AT ALL
    "I took my stand mixer to a repair shop last month."         -> (user, take, ...) @date

THE ENTAILMENT. Karttunen 1971, "Implicative Verbs", Language 47:340-358: an implicative
matrix's truth entails its complement's truth. Non-implicatives (want/plan/hope/decide) carry no
such entailment.

THE IRREALIS HALF OF THIS FILE IS THE LOAD-BEARING HALF. It is what stops this fix becoming
"the engine assumed it happened" — asserting things the user only contemplated would violate
user-is-truth far more seriously than the miss it repairs. Parametrized over unrelated verbs and
objects so no word list could satisfy it.
"""
import datetime

import pytest

REF = datetime.datetime(2023, 5, 22)


def _facts(sentence):
    from src.extraction.linguistics import derive_sentence_facts
    return [(getattr(f, "subject", None), getattr(f, "rel_type", None),
             getattr(f, "object", None), str(getattr(f, "event_date", None) or "")[:10])
            for f in (derive_sentence_facts(sentence, REF) or [])]


# ── REALIS: the user is narrating what they DID. Capture it, with its date. ──────────────
@pytest.mark.parametrize("sentence,verb,expect_date", [
    ("I had to take my stand mixer to a repair shop last month.", "take", "2023-04-22"),
    ("I had to buy a new coffee maker last month.", "buy", "2023-04-22"),
    ("I had to cancel my gym membership last week.", "cancel", "2023-05-15"),
])
def test_realized_implicative_is_captured_with_its_date(sentence, verb, expect_date):
    facts = _facts(sentence)
    assert facts, f"the whole clause was erased: {sentence!r}"
    hit = [f for f in facts if f[1] == verb and f[0] == "user"]
    assert hit, f"expected a (user, {verb}, ...) event; got {facts}"
    assert any(f[3] == expect_date for f in hit), \
        f"the event must carry its date {expect_date}; got {hit}"


# ── IRREALIS FIREWALL: the user only CONTEMPLATED this. Assert nothing. ──────────────────
@pytest.mark.parametrize("sentence", [
    "I want to buy a new coffee maker.",
    "I plan to attend the workshop next Saturday.",
    "I hope to visit Tokyo next year.",
    "I would like to replace my dishwasher.",
])
def test_unrealized_intent_is_never_asserted(sentence):
    """The load-bearing guard: an intention is not a thing the user did."""
    facts = _facts(sentence)
    events = [f for f in facts if f[1] not in ("owns", "instance_of", "also_known_as", "pref_name")]
    assert not events, (
        f"UNREALIZED INTENT was asserted as an event — this is the failure mode the "
        f"implicative fix must never introduce. {sentence!r} -> {events}")


def test_present_tense_obligation_is_not_yet_discharged():
    """Tense is the realis signal. "I have to take my car in tomorrow" has NOT happened."""
    facts = _facts("I have to take my car in tomorrow.")
    events = [f for f in facts if f[1] == "take"]
    assert not events, (
        f"a PENDING obligation must not be asserted as a completed event; got {events}")


def test_flag_off_is_byte_for_byte_legacy(monkeypatch):
    """OFF must restore today's behaviour exactly — the clause stays erased."""
    import importlib
    monkeypatch.setenv("SPINE_IMPLICATIVE_XCOMP", "false")
    import src.extraction.linguistics as ling
    importlib.reload(ling)
    try:
        out = ling.derive_sentence_facts(
            "I had to take my stand mixer to a repair shop last month.", REF) or []
        takes = [f for f in out if getattr(f, "rel_type", None) == "take"]
        assert not takes, "flag OFF must not descend into the infinitival complement"
    finally:
        monkeypatch.setenv("SPINE_IMPLICATIVE_XCOMP", "true")
        importlib.reload(ling)

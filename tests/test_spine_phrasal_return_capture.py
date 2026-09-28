"""Phrasal-particle RETURN/motion capture — the LongMemEval duration/count cluster root cause.

A phrasal motion/return verb whose load-bearing PP hangs off an adverbial PARTICLE — "got BACK from
my trip", "came BACK from the break" — parses so the content ``pobj`` (the trip / the break) is a
GRANDCHILD of the verb: ``got → advmod:back → prep:from → pobj:trip``. The direct-child scans in
``_svo_object_head`` / ``_svo_predicate_token`` missed it, the verb looked objectless, and
``_chain_intransitive`` minted a junk ``(user, has_state, get)`` that DROPPED the trip/break and
STRANDED its ``event_date`` — so the duration/between calc could never find the referent's other
dated endpoint.

This pins the fix: the phrasal-particle prep object is captured as the fact's object WITH its date,
and the junk ``has_state, get`` twin is gone. Covers BOTH cluster exemplars:
  • gpt4_1d80365e (temporal-reasoning) — "got back from my solo camping trip to Yosemite today"
    surfaces the RETURN occurrence dated to the session day, so the trip's start (05-15) and return
    (05-17) are both dated + reachable → the consuming model computes the 2-day span.
  • 6cb6f249 (multi-session) — "got back from a 10-day break in mid-February" surfaces the break
    event dated, instead of junk ``has_state, get``.

Subject-agnostic (advmod ADV particle → keep-particle prep → content pobj; NO verb/domain list),
deterministic. Also guards the NON-regression: a genuine objectless change-of-state ("the server
crashed") still routes to ``has_state``.
"""

import datetime

import pytest

from src.extraction.linguistics import derive_sentence_facts


def _facts(sentence, ref):
    return derive_sentence_facts(sentence, ref)


def _find_obj(facts, needle):
    needle = needle.lower()
    return [f for f in facts if f.object and needle in f.object.lower()]


def test_yosemite_return_occurrence_captured_and_dated():
    # gpt4_1d80365e — session 1 (2023-05-17): the RETURN endpoint of the camping trip.
    ref = datetime.date(2023, 5, 17)
    facts = _facts(
        "I just got back from an amazing solo camping trip to Yosemite National Park today, "
        "and I am already itching to get back out into the mountains.",
        ref,
    )
    hits = _find_obj(facts, "camping trip to yosemite")
    assert hits, f"return occurrence of the camping trip was dropped: {[ (f.rel_type, f.object) for f in facts ]}"
    # The referent is dated to the session day (the deictic 'today') so the duration calc can
    # reach the trip's OTHER endpoint (the 05-15 start) and span the two.
    assert any(str(f.event_date)[:10] == "2023-05-17" for f in hits), \
        f"return occurrence lost its event_date: {[ (f.object, f.event_date) for f in hits ]}"
    # The junk (user, has_state, 'get') twin must be gone — 'get' is not a state.
    assert not any((f.rel_type == "has_state" and (f.object or "").lower() in ("get", "got"))
                   for f in facts), \
        f"junk has_state/get twin still emitted: {[ (f.rel_type, f.object) for f in facts ]}"


def test_yosemite_start_occurrence_still_dated():
    # The START endpoint (session 0, 2023-05-15) — unchanged by the fix, dated by the deictic.
    ref = datetime.date(2023, 5, 15)
    facts = _facts(
        "I just started my solo camping trip to Yosemite National Park today and I am really "
        "excited to explore the park.",
        ref,
    )
    hits = _find_obj(facts, "camping trip to yosemite")
    assert hits, "start occurrence of the camping trip was dropped"
    assert any(str(f.event_date)[:10] == "2023-05-15" for f in hits)


def test_social_media_break_return_captured_and_dated():
    # 6cb6f249 — the '10-day break' return event, previously junk (user, has_state, get).
    ref = datetime.date(2023, 2, 15)
    facts = _facts("I got back from a 10-day break in mid-February.", ref)
    hits = _find_obj(facts, "break")
    assert hits, f"the break event was dropped to junk: {[ (f.rel_type, f.object) for f in facts ]}"
    assert any(str(f.event_date)[:10] == "2023-02-15" for f in hits), \
        f"break event lost its event_date: {[ (f.object, f.event_date) for f in hits ]}"
    assert not any((f.rel_type == "has_state" and (f.object or "").lower() in ("get", "got"))
                   for f in facts)


def test_generic_come_back_from_generalizes():
    # Subject-agnostic: any phrasal return construction, no verb list.
    facts = _facts("I came back from vacation.", datetime.date(2023, 6, 1))
    hits = _find_obj(facts, "vacation")
    assert hits, f"'came back from vacation' dropped the referent: {[ (f.rel_type, f.object) for f in facts ]}"
    assert not any((f.rel_type == "has_state" and (f.object or "").lower() in ("get", "come", "came"))
                   for f in facts)


def test_week_long_break_keeps_measure_premodifier():
    # 6cb6f249 — the OTHER operand (S0, mid-January): "took a week-long break". The compound-
    # ADJECTIVE measure premodifier ("week" →npadvmod "long"[amod of break]) was STRANDED, so the
    # object read "long break" — DROPPING the 7-day magnitude, which made a total-days aggregation
    # (7 + 10 = 17) unreachable. The captured object must retain the "week" measure token.
    facts = _facts("I even took a week-long break from it in mid-January.", datetime.date(2023, 1, 15))
    hits = _find_obj(facts, "break")
    assert hits, f"the break event was dropped: {[ (f.rel_type, f.object) for f in facts ]}"
    assert any("week" in (f.object or "").lower() for f in hits), \
        f"measure premodifier 'week' stranded — duration magnitude lost: {[ f.object for f in hits ]}"


def test_measure_adjective_generalizes_subject_agnostic():
    # Subject-agnostic — any compound measure-adjective, no unit/duration word list. "year-long" /
    # "day-long" premodifiers must survive on the object noun phrase.
    for sent, needle in [
        ("I attended a year-long training program.", "year"),
        ("She hosted a day-long workshop.", "day"),
    ]:
        facts = _facts(sent, datetime.date(2023, 6, 1))
        assert any(needle in (f.object or "").lower() for f in facts), \
            f"{needle!r} measure premodifier dropped for {sent!r}: {[ f.object for f in facts ]}"


def test_numeric_day_measure_premodifier_kept_on_trip_object():
    # b5ef892d (multi-session "how many days did I spend on camping trips … this year") — the two
    # US camping-trip operands: "a 3-day camping trip to Big Sur", "a 5-day camping trip to
    # Yellowstone". spaCy tags the MERGED numeral-unit premodifier as ``nummod`` (a COUNT dep) when
    # the head noun also carries a compound + a "to <place>" PP tail, so ``_object_value_phrase``
    # DROPPED "3-day"/"5-day" from the object — stranding the day-magnitude off the walkable referent,
    # so a cross-session total-days aggregation (3 + 5 = 8) could never reach either trip's length.
    # The merged numeral-unit token ("3-day") must survive ON the object phrase (parity with the
    # ``compound``-tagged "10-day break" case). Subject-agnostic, no unit/duration word list.
    for sent, needle in [
        ("I just got back from a 3-day solo camping trip to Big Sur in early April.", "3-day"),
        ("I just got back from an amazing 5-day camping trip to Yellowstone National Park last month.",
         "5-day"),
    ]:
        facts = _facts(sent, datetime.date(2023, 4, 29))
        trip_hits = _find_obj(facts, "camping trip")
        assert trip_hits, f"camping-trip referent dropped for {sent!r}: {[ f.object for f in facts ]}"
        assert any(needle in (f.object or "").lower() for f in trip_hits), \
            f"{needle!r} day-magnitude stranded off the trip object for {sent!r}: " \
            f"{[ f.object for f in trip_hits ]}"


def test_numeric_measure_premodifier_generalizes_non_temporal():
    # Subject-agnostic — ANY merged numeral-unit premodifier ("2-bedroom", "5-star"), not just
    # durations. The magnitude belongs to the referent's description, never dropped as a count.
    for sent, needle in [
        ("I went on a 2-bedroom apartment tour.", "2-bedroom"),
        ("I stayed at a 5-star hotel.", "5-star"),
    ]:
        facts = _facts(sent, datetime.date(2023, 6, 1))
        assert any(needle in (f.object or "").lower() for f in facts), \
            f"{needle!r} merged premodifier dropped for {sent!r}: {[ f.object for f in facts ]}"


def test_count_firewall_not_regressed():
    # NON-REGRESSION: a bare head-noun COUNT ("3 cats") must NOT fold the quantifier into the
    # object — the measure-adjective descent is scoped to NESTED modifiers of an admitted ADJ only.
    facts = _facts("I have 3 cats.", datetime.date(2023, 6, 1))
    assert any((f.object or "").lower().strip() == "cats" for f in facts), \
        f"count firewall regressed — quantifier folded into object: {[ f.object for f in facts ]}"


def test_intransitive_state_not_regressed():
    # NON-REGRESSION: a genuine objectless change-of-state (no advmod-prep grandchild) still
    # routes to has_state — the phrasal-particle branch must NOT swallow it.
    facts = _facts("The server crashed yesterday.", datetime.date(2023, 6, 1))
    assert any(f.rel_type == "has_state" and (f.object or "").lower() == "crash" for f in facts), \
        f"objectless state capture regressed: {[ (f.rel_type, f.object) for f in facts ]}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

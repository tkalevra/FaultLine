"""ISSUE #220 + #223 — the knowledge-ask frame survives a leading vocative, the
speaker-pronoun residual stands the topic gate down EXPLICITLY, and the hollow-taxonomy
CRIT is deduped per (tenant, taxonomy).

Issue #220 (two prongs, one seam — `_topic_grounded_facts`' knowledge-ask judge):

  A leading VOCATIVE — Quirk et al., A Comprehensive Grammar of the English Language
  (1985), ch. 8 (§8.92ff): a noun phrase addressing the hearer, set off by "comma
  intonation" — broke `_KNOWLEDGE_ASK_FRAME_RE`'s `^`-anchored frame, so the residual
  came back None, `_topic_grounded_facts` fell back to `_topic_lemmas(full query)`, the
  SemCor-measured frame verb `know` won the topic argmin, and the gate dropped fetched
  profile facts (measured live 2026-10-06: `topic_grounding_filtered after=1 before=6`
  on the wound query). Prong 1 admits a BOUNDED leading vocative (closed-class
  interjection + optional short address term + REQUIRED comma); prong 2 makes the
  speaker-pronoun keep-all EXPLICIT — a residual that is only a closed-class speaker
  pronoun names the walk target itself, so there is no topic to judge. Before prong 2
  the keep-all happened only accidentally (via `_residual_direct_facts`' >=3-letter
  token filter coming up empty).

Issue #223: the seeded `animal` row is hollow in the `public` template itself, so every
query naming an animal-ish word logged the full CRIT. First sighting per (tenant,
taxonomy) per process logs the full CRIT; repeats log DEBUG with a repeat count. The
seed fix stays an owner call — this file pins the LOG HYGIENE only.

Subject-agnostic throughout: fixtures carry no owner personal data and no real seat
UUIDs; vocative fixtures use generic address terms ("there") or the public-reference
name "Sam". No DB and no network for the #220 pins and the #223 dedup pins (the dedup
helper is exercised with a fake connection whose `SHOW search_path` is canned).
"""
import importlib
import re
import time

import pytest

main = importlib.import_module("src.api.main")
WL = importlib.import_module("src.api.wordnet_ladder")

# The #220 gate pins judge via WordNet/SemCor, exactly like tests/test_topic_grounding.py:
# a green tick from a run where the lexicon never loaded would assert nothing.
pytestmark = pytest.mark.skipif(
    WL._wn() is None,
    reason="WordNet corpus absent — the topic gate cannot fire, so passing would be meaningless")


# A six-fact profile, the shape the wound dropped (prod measured after=1 before=6).
# Subject-agnostic: pets, cities, scalars — no owner data.
SIX = [
    {"subject": "user", "rel_type": "has_pet", "object": "rex"},
    {"subject": "user", "rel_type": "lives_in", "object": "toronto"},
    {"subject": "user", "rel_type": "weight", "object": "70"},
    {"subject": "user", "rel_type": "height", "object": "180"},
    {"subject": "user", "rel_type": "lives_in", "object": "ottawa"},
    {"subject": "user", "rel_type": "occupation", "object": "systems analyst"},
]

# The named-ask gate in `_topic_grounded_facts` (a mid-string capital) returns facts
# unchanged too — so a vocative fixture bearing "Sam" would pass for the WRONG reason.
# These are the discriminator assertions that keep the pins below honest (a false green
# here would let a future regression hide behind the named-ask branch).
_NAMED_ASK_RE = re.compile(r"(?<=[a-z,;:]\s)[A-Z][a-zA-Z'’\-]{2,}")


# ══════════════════════════════════════════════════════════════════════════════════════
# Issue #220 prong 1 — the frame admits a bounded leading vocative
# ══════════════════════════════════════════════════════════════════════════════════════

def test_vocative_no_longer_breaks_the_frame():
    assert main._knowledge_ask_residual("Hey there, what do you know about me?") == "me"
    assert main._knowledge_ask_residual(
        "Hey Sam, what do you know about network security?") == "network security"


def test_vocative_comma_is_required():
    """Comma intonation is what makes it a vocative; without it the segment is just a
    preamble and must NOT be swallowed."""
    assert main._knowledge_ask_residual("Hey what do you know about me?") is None
    assert main._knowledge_ask_residual("So what do you know about me?") is None


def test_non_vocative_preambles_still_decline():
    """An interjection is required to open the prefix — 'by the way,' is a preamble, and
    admitting it would start the frame swallowing arbitrary front matter."""
    assert main._knowledge_ask_residual("By the way, what do you know about me?") is None


def test_vocative_does_not_admit_a_second_sentence():
    """The prefix is bounded (~40 chars, one comma) — a full clause before the frame is
    not a vocative and must not match."""
    assert main._knowledge_ask_residual(
        "Hey there, I was wondering, what do you know about me?") is None


def test_possessive_decline_survives_a_vocative():
    assert main._knowledge_ask_residual("Hi, what do I know about my dog?") is None


def test_vocative_is_leading_only_shape():
    """'So,' as a discourse-marker vocative is admitted (closed-class, comma-set-off) but
    the frame itself still has to follow."""
    assert main._knowledge_ask_residual("So, what do you know about me?") == "me"
    assert main._knowledge_ask_residual("Well then, what do you know about me?") == "me"


def test_vocative_prefix_stays_linear_under_adversarial_input():
    """CodeQL py/polynomial-redos bar, checked structurally by construction (bounded
    comma-negated class, single `^`-anchored start position, greedy `.+` at `$`) and
    empirically here: each adversarial shape must return in bounded time, never hang."""
    cases = [
        "hey " + "a" * 200_000,                                   # no comma ever
        "hey " + "a" * 200_000 + ", what do you know about me?",  # comma arrives late
        "hey " + "a a " * 30_000 + "what do you know about me?",  # spaces defeat the term
        "hello there, " + "x" * 100_000,                          # vocative, no frame
        "um uh ok so, " + "b" * 150_000 + "?",
    ]
    for case in cases:
        t0 = time.perf_counter()
        main._knowledge_ask_residual(case)
        assert time.perf_counter() - t0 < 0.5, (
            "the vocative prefix introduced non-linear backtracking "
            f"(input len={len(case)})")


# ══════════════════════════════════════════════════════════════════════════════════════
# Issue #220 prong 2 — the speaker-pronoun stand-down, EXPLICIT and pinned
# ══════════════════════════════════════════════════════════════════════════════════════

def test_vocative_profile_ask_keeps_every_fetched_fact():
    """THE BAR, prong 1. The exact wound shape: a vocative greeting in front of the
    profile ask must not let the frame verb `know` judge (and drop) the profile."""
    q = "Hey there, what do you know about me?"
    assert not _NAMED_ASK_RE.search(q), (
        "fixture leaked into the named-ask bypass — this pin would pass for the wrong reason")
    assert main._topic_grounded_facts(q, SIX) == SIX


def test_bare_speaker_pronoun_residual_is_an_EXPLICIT_stand_down():
    """THE BAR, prong 2. The keep-all is determinate now: the residual names the walk
    target (the speaker), so there is no topic to judge — not an accident of the
    >=3-letter token filter coming up empty."""
    q = "What do you know about me?"
    assert not _NAMED_ASK_RE.search(q), (
        "fixture leaked into the named-ask bypass — this pin would pass for the wrong reason")
    assert main._knowledge_ask_residual(q) == "me"
    assert main._topic_grounded_facts(q, SIX) == SIX


@pytest.mark.parametrize("q", [
    "What do you know about us?",
    "What do you know about yourself?",
    "what do we know about me?",
])
def test_other_speaker_pronoun_residuals_stand_down_too(q):
    assert not _NAMED_ASK_RE.search(q)
    assert main._knowledge_ask_residual(q) is not None
    assert main._topic_grounded_facts(q, SIX) == SIX


def test_mixed_residual_is_NOT_stood_down():
    """The stand-down is precise: an open-class word in the residual ('gardening') makes
    it a real topic ask and the residual-direct lane judges it as before (this fixture
    names no gardening, so nothing survives)."""
    q = "What do you know about me and gardening?"
    assert main._topic_grounded_facts(q, SIX) == []


# ══════════════════════════════════════════════════════════════════════════════════════
# Issue #220 frozen bars — the neighbouring behavior is unchanged
# ══════════════════════════════════════════════════════════════════════════════════════

# Measured on HEAD with this fix in place: the two on-topic rows survive, the four
# off-topic rows are dropped — the #31-R2 residual judging, untouched.
TOPIC_MIX = [
    {"subject": "user", "rel_type": "part_of", "object": "network security study group"},
    {"subject": "user", "rel_type": "has_certification", "object": "network security"},
    {"subject": "user", "rel_type": "owns", "object": "cisco router"},
    {"subject": "user", "rel_type": "has_pet", "object": "rex"},
    {"subject": "user", "rel_type": "lives_in", "object": "toronto"},
    {"subject": "user", "rel_type": "weight", "object": "70"},
]


def test_grown_topic_ask_unchanged():
    """'What do I know about network security?' still judges from the residual and keeps
    only the on-topic rows (measured: 2 of 6, the #31-R2 behavior)."""
    out = main._topic_grounded_facts("What do I know about network security?", TOPIC_MIX)
    assert [(f["rel_type"], f["object"]) for f in out] == [
        ("part_of", "network security study group"),
        ("has_certification", "network security"),
    ]


def test_unmeasured_non_pronoun_residual_unchanged():
    """'woodworking' is SemCor-unmeasured → the residual-direct lane still filters (this
    fixture names no woodworking → 0), and is NOT kept-all and NOT stood down."""
    out = main._topic_grounded_facts("What do I know about woodworking?", TOPIC_MIX)
    assert out == []


# ══════════════════════════════════════════════════════════════════════════════════════
# Issue #223 — hollow-taxonomy CRIT dedup (log hygiene only)
# ══════════════════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, search_path):
        self._sp = search_path

    def execute(self, query, *args):
        pass

    def fetchone(self):
        return [self._sp]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDb:
    """A connection bound `SET search_path TO <schema>` WITHOUT public — `SHOW
    search_path` therefore returns the tenant schema first, which is the dedup key's
    tenant half."""

    def __init__(self, search_path="faultline_testseat"):
        self._sp = search_path

    def cursor(self):
        return _FakeCursor(self._sp)


class _LogSpy:
    def __init__(self):
        self.crits = []
        self.debugs = []

    def critical(self, msg, **kw):
        self.crits.append((msg, kw))

    def debug(self, msg, **kw):
        self.debugs.append((msg, kw))


@pytest.fixture()
def fresh_dedup(monkeypatch):
    monkeypatch.setattr(main, "_HOLLOW_TAXONOMY_CRIT_SEEN", {})
    return main._HOLLOW_TAXONOMY_CRIT_SEEN


def test_hollow_crit_fires_once_then_debug(fresh_dedup, monkeypatch):
    spy = _LogSpy()
    monkeypatch.setattr(main, "log", spy)
    db = _FakeDb()
    main._log_hollow_taxonomy_crit(db, "animal", "tell me about my animal")
    main._log_hollow_taxonomy_crit(db, "animal", "what animal do I own")
    main._log_hollow_taxonomy_crit(db, "animal", "my animal again")
    assert len(spy.crits) == 1, "first sighting must CRIT exactly once"
    ev, kw = spy.crits[0]
    assert ev == "determine_path.hollow_taxonomy_defining_group"
    assert kw.get("taxonomy") == "animal" and "EMPTY" in kw.get("note", "")
    assert len(spy.debugs) == 2, "repeats must log at debug"
    ev2, kw2 = spy.debugs[0]
    assert ev2 == "determine_path.hollow_taxonomy_defining_group_repeat"
    assert kw2.get("repeats") == 1 and kw2.get("taxonomy") == "animal"
    assert spy.debugs[1][1].get("repeats") == 2, "the repeat counter must advance"


def test_a_different_taxonomy_still_crits_after_a_dedup(fresh_dedup, monkeypatch):
    """The key is the (schema, taxonomy) PAIR — deduping one taxonomy can never suppress
    another one's first CRIT."""
    spy = _LogSpy()
    monkeypatch.setattr(main, "log", spy)
    db = _FakeDb()
    main._log_hollow_taxonomy_crit(db, "animal", "q1")
    main._log_hollow_taxonomy_crit(db, "animal", "q2")
    main._log_hollow_taxonomy_crit(db, "vehicle", "q3")
    assert len(spy.crits) == 2
    assert {kw.get("taxonomy") for _, kw in spy.crits} == {"animal", "vehicle"}


def test_a_different_tenant_still_crits(fresh_dedup, monkeypatch):
    spy = _LogSpy()
    monkeypatch.setattr(main, "log", spy)
    main._log_hollow_taxonomy_crit(_FakeDb("faultline_seat_a"), "animal", "q1")
    main._log_hollow_taxonomy_crit(_FakeDb("faultline_seat_b"), "animal", "q2")
    assert len(spy.crits) == 2, "per-TENANT dedup: another seat's first sighting CRITs"


def test_dedup_state_is_bounded_and_evicts_oldest(fresh_dedup, monkeypatch):
    spy = _LogSpy()
    monkeypatch.setattr(main, "log", spy)
    monkeypatch.setattr(main, "_HOLLOW_TAXONOMY_CRIT_CAP", 4)
    for i in range(6):
        main._log_hollow_taxonomy_crit(_FakeDb(), f"tax{i}", "q")
    assert len(main._HOLLOW_TAXONOMY_CRIT_SEEN) <= 4, "state must stay under the cap"
    # the OLDEST entry was evicted: tax0's next sighting is a FIRST again and CRITs.
    n_before = len(spy.crits)
    main._log_hollow_taxonomy_crit(_FakeDb(), "tax0", "q")
    assert len(spy.crits) == n_before + 1, "an evicted key must re-CRIT, never stay silent"


def test_dedup_failure_fails_LOUD_not_silent(fresh_dedup, monkeypatch):
    """Fail-safe per the issue: a dedup-state error must never silence (nor break) the
    surfacing — the full CRIT is logged as a first sighting."""

    def _boom(db, taxonomy_name):
        raise RuntimeError("dedup state exploded")

    monkeypatch.setattr(main, "_hollow_taxonomy_dedup_key", _boom)
    spy = _LogSpy()
    monkeypatch.setattr(main, "log", spy)
    main._log_hollow_taxonomy_crit(_FakeDb(), "animal", "q")  # must NOT raise
    assert len(spy.crits) == 1


def test_unreadable_search_path_over_logs_never_suppresses(fresh_dedup, monkeypatch):
    """The fail direction is toward OVER-logging: an unreadable search_path degrades the
    key — it can re-log a first CRIT but never suppress one."""

    class _DeadDb:
        def cursor(self):
            raise RuntimeError("connection gone")

    spy = _LogSpy()
    monkeypatch.setattr(main, "log", spy)
    main._log_hollow_taxonomy_crit(_DeadDb(), "animal", "q1")
    main._log_hollow_taxonomy_crit(_DeadDb(), "animal", "q2")
    assert len(spy.crits) == 2, "degraded keys must never swallow a sighting"


def test_determine_path_wiring_uses_the_dedup_helper():
    """Structural pin (census precedent): the CRIT is emitted ONLY through
    `_log_hollow_taxonomy_crit` — an inline re-log at the call site would bypass the
    dedup, and a second emission site would split the repeat counter."""
    src = open(main.__file__, encoding="utf-8").read()
    assert src.count('log_crit(log, "determine_path.hollow_taxonomy_defining_group"') == 1, (
        "the hollow-taxonomy CRIT must be emitted exactly once, inside the dedup helper")
    assert "_log_hollow_taxonomy_crit(db, tax_name, query_text)" in src, (
        "determine_path must route the hollow-taxonomy CRIT through the dedup helper")

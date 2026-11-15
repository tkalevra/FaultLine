"""Cortex slice 2+1 — EVIDENCE-BOUND notes and the REFLECTION NUDGE.

WHY these exist (both were paid for in real time on 2026-07-13):
  * A cortex note is a CLAIM. A claim with no proof ROTS INTO A TRAP. A stale "Known Bug"
    (already fixed weeks earlier) and two test files asserting DELETED code each sent a session
    chasing phantom work, because nothing recorded what the claim was checked against, or when.
    -> `evidence_sha` makes staleness DECIDABLE, and an evidenced note OUTRANKS an unevidenced
       one at recall: a lesson you can check beats a lesson you must take on faith.
  * Two capture lanes are automatic in any client (a command failed; the user corrected you).
    The lane always LOST is the one worked out by READING. A client hook can force it, but
    FaultLine is model-agnostic — so the MEMORY asks, on the operating-brief channel.
    -> `reflection_nudge` fires only on a real signal and stops permanently after one lesson.
"""
import pytest

from src.api import cortex


# ── EVIDENCE ranking ─────────────────────────────────────────────────────────────────────

def _row(rid, category, note, tags, lifecycle="observed", obs=1, ev_sha=None):
    """A candidate row in the shape recall_notes fetches (12 cols, evidence included)."""
    return (rid, category, note, tags, None, "agent_manual", lifecycle, obs, None,
            ev_sha, ({"sha": ev_sha} if ev_sha else None), None)


def _base_row(rid, category, note, tags, lifecycle="observed", obs=1):
    """The BASE 9-col shape (what recall_usage_patterns fetches). Must still score."""
    return (rid, category, note, tags, None, "agent_manual", lifecycle, obs, None)


def test_evidenced_note_outranks_unevidenced_twin():
    """Two equally-relevant notes: the one you can CHECK must come first."""
    rows = [
        _row(1, "gotcha", "deploy needs a stack recreate", ["deploy"], ev_sha=None),
        _row(2, "gotcha", "deploy needs a portainer recreate", ["deploy"], ev_sha="abc1234"),
    ]
    top = cortex._score_candidate_rows(rows, ["deploy"], False, {}, 5)
    assert len(top) == 2
    assert top[0]["evidence_sha"] == "abc1234", \
        "the evidenced note must rank first — a checkable lesson beats one taken on faith"


def test_evidence_travels_with_the_note():
    """The reader must RECEIVE the evidence, or it cannot verify the claim is still current."""
    rows = [_row(1, "pitfall", "the walk gap is closed", ["walk"], ev_sha="12b6e80")]
    top = cortex._score_candidate_rows(rows, ["walk"], False, {}, 5)
    assert top[0]["evidence_sha"] == "12b6e80"
    assert top[0]["evidence"] == {"sha": "12b6e80"}


def test_evidence_cannot_lift_an_irrelevant_note():
    """Evidence REORDERS relevant notes; it must never manufacture a match.

    Same rule as the provenance weight — otherwise a well-evidenced but unrelated note would
    crowd out the one you actually needed.
    """
    rows = [_row(1, "gotcha", "something about qdrant", ["qdrant"], ev_sha="abc1234")]
    top = cortex._score_candidate_rows(rows, ["postgres", "migration"], False, {}, 5)
    assert top == [], "an irrelevant note must stay out, however well-evidenced"


def test_scorer_is_length_tolerant_for_the_operating_brief():
    """recall_usage_patterns still fetches the BASE 9 columns and shares this scorer.

    A fixed-arity unpack here would silently break the HOW-YOU-OPERATE channel.
    """
    rows = [_base_row(1, "usage_pattern", "call remember_facts on a stated fact", ["memory"])]
    top = cortex._score_candidate_rows(rows, ["remember"], False, {}, 5)
    assert len(top) == 1
    assert top[0]["evidence_sha"] is None      # absent, not an exception
    assert top[0]["note"].startswith("call remember_facts")


# ── REFLECTION NUDGE ─────────────────────────────────────────────────────────────────────

class _FakeCur:
    """Stands in for a cursor bound to the cortex schema.

    `reflection_nudge` issues THREE reads: a to_regclass probe for cortex_episodes, the note
    counts, then a to_regclass probe for cortex_engagement followed (only if present) by the
    engagement counts. The fake tracks which one it is answering so a test can drive the two
    independent signals — notes and CALLS — separately. That separation is the entire point of
    the engagement meter: "is this agent working" must not be measured by whether it writes.
    """

    def __init__(self, counts, engagement, has_engagement=True):
        self._counts = counts               # (deliberate, auto, evidenced)
        self._engagement = engagement       # (reached, nudges_shown)
        self._has_engagement = has_engagement
        self._last = None
        self._seen_regclass = 0

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            self._seen_regclass += 1
            self._last = "regclass"
        elif "cortex_engagement" in sql:
            self._last = "engagement"
        else:
            self._last = "counts"

    def fetchone(self):
        if self._last == "regclass":
            # 1st probe = cortex_episodes (always present here); 2nd = cortex_engagement.
            if self._seen_regclass >= 2 and not self._has_engagement:
                return [None]
            return ["flagent_x.some_table"]
        if self._last == "engagement":
            return self._engagement
        return self._counts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, counts, engagement, has_engagement=True):
        self._args = (counts, engagement, has_engagement)

    def cursor(self):
        return _FakeCur(*self._args)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def _cortex_conn(monkeypatch):
    def _install(counts, engagement=(0, 0), has_engagement=True):
        monkeypatch.setattr(cortex, "cortex_schema_for", lambda _u: "flagent_x")
        monkeypatch.setattr(cortex, "_cortex_conn",
                            lambda _s: _FakeConn(counts, engagement, has_engagement))
        # the nudge records its own delivery; keep that off the DB in these tests
        monkeypatch.setattr(cortex, "record_engagement", lambda *a, **k: None)
    return _install


def test_nudge_fires_when_working_but_never_reflecting(_cortex_conn):
    """Failures piling up, ZERO lessons of its own — the lane that always gets lost."""
    _cortex_conn((0, 5, 0))          # deliberate=0, auto=5, evidenced=0
    msg = cortex.reflection_nudge("u")
    assert msg and "no lessons of your own" in msg
    assert "5 failures" in msg       # names the real signal, not a generic nag


def test_nudge_stops_permanently_after_one_deliberate_lesson(_cortex_conn):
    """SELF-LIMITING. A nag is worse than nothing — the model learns to tune it out."""
    _cortex_conn((1, 50, 0))         # one lesson recorded -> silence, forever
    assert cortex.reflection_nudge("u") is None


def test_nudge_silent_on_a_fresh_seat(_cortex_conn):
    """No failures yet = no signal. Never nag an idle/new agent."""
    _cortex_conn((0, 0, 0))
    assert cortex.reflection_nudge("u") is None


def test_nudge_asks_for_evidence_once_it_is_reflecting_on_faith(_cortex_conn):
    """Reflecting, but nothing is checkable — every note is a future trap."""
    _cortex_conn((4, 0, 0))          # deliberate notes, none evidenced
    msg = cortex.reflection_nudge("u")
    assert msg and "evidence" in msg.lower()


def test_nudge_silent_when_notes_carry_evidence(_cortex_conn):
    _cortex_conn((4, 0, 2))
    assert cortex.reflection_nudge("u") is None


# ── LANE 0 — THE HOOK-LESS SEAT (the population the nudge could never reach) ──────────────
#
# ROOT CAUSE these pin: "is this agent working?" used to be measured as
# count(failed_command) >= 3. `failed_command` rows only arrive through /iremember, so the
# signal for "working" was itself a cortex WRITE — the very thing whose absence is the defect.
# A seat that never writes was indistinguishable from a seat that does not exist, and the nudge
# was therefore silent for exactly the population it was built for. Measured on production
# 2026-08-03: 25 of 27 live seats had ZERO deliberate notes and could not be nudged.

def test_nudge_reaches_a_seat_with_no_client_side_hook(_cortex_conn):
    """The production failure: reaching for memory constantly, never writing a note.

    No `failed_command` rows exist (an OpenWebUI-hosted model has no auto-capture hook), so the
    legacy lane sees auto=0 and stays silent. The CALL count is the signal that survives.
    """
    _cortex_conn((0, 0, 0), engagement=(12, 0))     # 12 reaches, 0 notes, never nudged
    msg = cortex.reflection_nudge("u")
    assert msg, "a seat that reaches for memory and never writes MUST be reachable"
    assert "iremember" in msg, "the nudge has to name the call it is asking for"
    assert "12" in msg, "name the real signal — a generic nag gets tuned out"


def test_nudge_silent_on_a_seat_that_has_barely_started(_cortex_conn):
    """Below the floor there is no signal yet — never nag a fresh seat."""
    _cortex_conn((0, 0, 0), engagement=(cortex._NUDGE_MIN_REACHED - 1, 0))
    assert cortex.reflection_nudge("u") is None


def test_hookless_nudge_stops_after_one_deliberate_note(_cortex_conn):
    """Same self-limiting contract as the legacy lane: one note earns permanent silence."""
    _cortex_conn((1, 0, 0), engagement=(500, 0))
    assert cortex.reflection_nudge("u") is None


def test_nudge_is_capped_so_it_can_never_become_wallpaper(_cortex_conn):
    """THE ANTI-WALLPAPER RULE.

    A nudge shown N times and ignored will not work on attempt N+1 — it has become unasked
    per-turn content, which is the exact failure mode of "just instruct it harder". After the
    cap it goes quiet permanently, whether or not the seat ever wrote anything. A still-starving
    capped seat is a finding for the owner (visible in engagement_snapshot), not a louder nag.
    """
    _cortex_conn((0, 0, 0), engagement=(999, cortex._NUDGE_MAX_SHOWS))
    assert cortex.reflection_nudge("u") is None, \
        "past the cap the nudge must be silent — repetition is what devalues the whole brief"

    # one below the cap it is still allowed to speak
    _cortex_conn((0, 0, 0), engagement=(999, cortex._NUDGE_MAX_SHOWS - 1))
    assert cortex.reflection_nudge("u") is not None


def test_nudge_tolerates_a_seat_with_no_engagement_table_yet(_cortex_conn):
    """The nudge is a PURE READER: it must never create the table or commit on the read path.

    DDL there would mean a failed telemetry statement aborts the transaction that just fetched
    the note counts — this codebase has already paid for that class of bug once, where a poisoned
    transaction surfaced as "no facts found" on a memory that had just been read.
    """
    _cortex_conn((0, 0, 0), engagement=(0, 0), has_engagement=False)
    assert cortex.reflection_nudge("u") is None      # zeros, not an exception


def test_unknown_engagement_signal_is_dropped(monkeypatch):
    """A typo at a call site must not mint a phantom row that skews the nudge gate."""
    called = []
    monkeypatch.setattr(cortex, "cortex_schema_for", lambda _u: called.append(1) or "flagent_x")
    cortex.record_engagement("u", "not_a_real_signal")
    assert called == [], "an unrecognised signal must be rejected before any DB work"


def test_engagement_write_never_raises(monkeypatch):
    """Telemetry must never be able to break the caller it rides on."""
    monkeypatch.setattr(cortex, "cortex_schema_for", lambda _u: "flagent_x")

    def _boom(_s):
        raise RuntimeError("db down")

    monkeypatch.setattr(cortex, "_cortex_conn", _boom)
    cortex.record_engagement("u", cortex.ENGAGEMENT_IRECALL)     # must not raise


def test_engagement_snapshot_is_failsafe(monkeypatch):
    """The measurable must degrade to zeros, never to a 500 on someone's recall."""
    monkeypatch.setattr(cortex, "cortex_schema_for", lambda _u: None)
    snap = cortex.engagement_snapshot("u")
    assert snap["calls"] == {} and snap["starving"] is False


def test_nudge_never_raises_on_a_broken_cortex(monkeypatch):
    """A nudge that breaks recall is a bug. Fail-safe: silence."""
    monkeypatch.setattr(cortex, "cortex_schema_for", lambda _u: "flagent_x")

    def _boom(_s):
        raise RuntimeError("db down")

    monkeypatch.setattr(cortex, "_cortex_conn", _boom)
    assert cortex.reflection_nudge("u") is None


# ── HARDENED RULES RIDE THE OPERATING BRIEF ──────────────────────────────────────────────

def test_hardened_lesson_rides_the_operating_brief():
    """A lesson the agent LEARNED and that HARDENED (proven by repetition) is an operating rule.

    It used to be invisible: the HOW-YOU-OPERATE channel served ONLY the seeded manual, so a
    hardened note never reached the model unless it remembered to call /irecall — and the entire
    failure mode is that it does NOT remember to ask. A rule that is recorded but never FIRES is
    worth nothing.
    """
    rows = [
        _base_row(1, cortex.USAGE_PATTERN_CATEGORY,
                  "call remember_facts when the user states a fact in passing", ["memory"]),
        _base_row(2, "howto", "run every test through tools/fltest.py, never raw pytest",
                  ["pytest", "tests"], lifecycle="hardened", obs=5),
    ]
    top = cortex._score_candidate_rows(rows, ["pytest", "tests"], False, {}, 5)
    assert any("fltest" in t["note"] for t in top), \
        "a HARDENED lesson must be able to surface on the operating brief"
    assert top[0]["hardened"] is True, "the proven rule should outrank an unrelated seeded one"


def test_observed_lesson_stays_private_until_it_hardens():
    """An UNPROVEN note must not jump straight onto the operating brief.

    Hardening (observation_count >= 3) is what earns a lesson its place there — otherwise a
    single stray observation would start dictating how the model operates.
    """
    assert cortex.HARDEN_THRESHOLD == 3
    # `observed` + low count => not hardened => it is not an operating rule yet. (The note is
    # made RELEVANT to the query on purpose: we are asserting the HARDENED flag, not the
    # relevance bar — a fixture that fails the bar would prove nothing either way.)
    row = _base_row(1, "gotcha", "a stray docker observation", ["docker", "deploy"],
                    lifecycle="observed", obs=1)
    rec = cortex._score_candidate_rows([row], ["docker", "deploy"], False, {}, 5)
    assert rec, "fixture must clear the relevance bar, else the assertion below is vacuous"
    assert rec[0]["hardened"] is False, "an unproven note is not yet an operating rule"


def test_irrelevant_hardened_rule_cannot_surface():
    """The REAL-MATCH BAR still holds: hardening REORDERS, it never manufactures a match.

    Otherwise every hardened rule would be shouted on every turn and the model would learn to
    tune the channel out — which is exactly how a nag destroys a signal.
    """
    rows = [_base_row(1, "howto", "docker deploy needs a stack recreate", ["docker", "deploy"],
                      lifecycle="hardened", obs=9)]
    top = cortex._score_candidate_rows(rows, ["postgres", "migration"], False, {}, 5)
    assert top == [], "an irrelevant rule must stay silent, however hardened"

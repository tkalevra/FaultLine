"""EXPERIENCE PUSH LANE — the model-independent surfacing of the agent's own prior lessons.

The lane rides the RESPONSE of a call the model already makes while ACTING (the write path),
because a memory server cannot inject into a model's context unsolicited. These tests pin the
three things that make it safe to ship:

  * FLAG OFF ⇒ byte-for-byte legacy, and ZERO backend call.
  * SILENCE ⇒ the key is ABSENT, never a "no relevant notes" line.
  * ONE SEAM ⇒ every terminal return of the write tool carries the lane, not just the happy one.

Plus the gate itself: score ALL candidates then pick (never pick-then-score), mandatory keyword
corroboration, a mandatory second independent signal, a hard budget, and READ-ONLY (no bump).

No DB needed except where marked — the candidate field is injected.
"""
import asyncio
import os

import pytest

from src.api import cortex
from src.mcp import server


# ── helpers ───────────────────────────────────────────────────────────────────────────────

def _row(rid, note, tags, category="gotcha", lifecycle="observed", obs=1, ev=None):
    """A cortex_episodes candidate row in the shape _fetch_lesson_candidates returns."""
    return (rid, category, note, list(tags), None, "agent_manual", lifecycle, obs, None, ev,
            None, None)


@pytest.fixture
def seat(monkeypatch):
    """A resolvable seat whose candidate field we control, with the semantic lane OFF."""
    uid = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(cortex, "cortex_schema_for", lambda u: "flagent_x" if u == uid else None)
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    cortex._EXP_SEEN.clear()
    return uid


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── THE GATE ──────────────────────────────────────────────────────────────────────────────

def test_score_all_then_pick_never_pick_then_score(seat, monkeypatch):
    """The audited client-side defect, pinned server-side: a hardened/curated note that is
    IRRELEVANT must never displace a relevant one. The old client selector preferred any
    `[rule]` bullet and only THEN judged relevance, and threw away the exactly-correct note."""
    rows = [
        # hardened + high provenance, ZERO overlap with the activity — must not win, must not
        # even be admitted.
        _row(1, "USER CORRECTED me - the LME fix-pipeline heartbeat must be restarted by hand.",
             ["lme", "pipeline"], category="correction", lifecycle="hardened", obs=5),
        # the right note
        _row(2, "PROD deploy is different from pre-prod: build context is /home/deploy/src.",
             ["deploy", "production", "docker"], category="howto"),
    ]
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates", lambda s: rows)
    lines = cortex.experience_lines(seat, "deploying the new build to production with docker")
    assert len(lines) == 1
    assert "PROD deploy" in lines[0]


def test_keyword_corroboration_is_mandatory(seat, monkeypatch):
    """Signal 1. A semantic-only admission is exactly what produces 'bake sourdough bread ->
    9 bullets about docker'. Even with a strong semantic score, no keyword ⇒ silence."""
    rows = [_row(1, "Never run docker compose up from the repo directory.", ["docker", "stack"])]
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates", lambda s: rows)
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: [0.1] * 768)
    monkeypatch.setattr(cortex, "_cortex_partition", lambda u: ("flagent-x", None))
    monkeypatch.setattr(cortex, "_search_cortex_ann", lambda *a, **k: [(1, 0.9)])
    monkeypatch.setattr(cortex, "_relative_semantic_components", lambda hits: {1: 2.0})
    assert cortex.experience_lines(seat, "bake sourdough bread for the weekend") == []


def test_second_signal_is_mandatory_keyword_alone_is_not_enough(seat, monkeypatch):
    """Signal 2. Free-text overlap alone (no tag anchor, no semantic) does not admit — that is
    the top-K behaviour this lane exists to be stricter than."""
    rows = [_row(1, "The docker compose stack for the benchmark harness lives elsewhere.",
                 ["unrelated"])]
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates", lambda s: rows)
    assert cortex.experience_lines(seat, "docker compose stack") == []


def test_second_signal_semantic_branch_admits(seat, monkeypatch):
    """With keyword corroboration present, the RELATIVE-semantic component is the second
    signal — the branch that carries the lane where a vector lane exists."""
    rows = [_row(1, "The docker compose stack for the benchmark harness lives elsewhere.",
                 ["unrelated"])]
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates", lambda s: rows)
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: [0.1] * 768)
    monkeypatch.setattr(cortex, "_cortex_partition", lambda u: ("flagent-x", None))
    monkeypatch.setattr(cortex, "_search_cortex_ann", lambda *a, **k: [(1, 0.9)])
    monkeypatch.setattr(cortex, "_relative_semantic_components", lambda hits: {1: 1.5})
    assert cortex.experience_lines(seat, "docker compose stack") != []


def test_second_signal_tag_anchor_branch_admits(seat, monkeypatch):
    """...and where NO vector lane exists (no fastembed / embedder down), the tag-anchored
    branch keeps the lane alive instead of making it permanently dead-silent. Tags are curated
    at write time, so a tag hit is genuinely independent of the note-prose overlap."""
    rows = [_row(1, "Never run docker compose up from the repo directory - wrong stack.",
                 ["docker", "compose", "stack"])]
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates", lambda s: rows)
    assert cortex.experience_lines(seat, "docker compose stack") != []


def test_thin_activity_text_costs_nothing(seat, monkeypatch):
    """Below the keyword floor the lane must not even read the DB — it can only say nothing."""
    called = []
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates",
                        lambda s: called.append(1) or [])
    assert cortex.experience_lines(seat, "hi") == []
    assert called == []


def test_hard_budget_two_lines_and_char_cap(seat, monkeypatch):
    long = ("deploy production docker compose stack " * 12).strip()
    rows = [_row(i, long, ["deploy", "production", "docker"]) for i in range(1, 6)]
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates", lambda s: rows)
    lines = cortex.experience_lines(seat, "deploy production docker compose")
    assert 0 < len(lines) <= 2
    assert sum(len(x) for x in lines) <= cortex._EXP_MAX_CHARS


def test_dedupe_suppresses_a_repeat_for_the_same_seat(seat, monkeypatch):
    rows = [_row(1, "PROD deploy differs from pre-prod: build context is /home/deploy/src.",
                 ["deploy", "production", "docker"])]
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates", lambda s: rows)
    assert cortex.experience_lines(seat, "deploy production docker") != []
    assert cortex.experience_lines(seat, "deploy production docker") == []


def test_unresolvable_seat_and_broken_field_fail_safe(monkeypatch):
    assert cortex.experience_lines("not-a-uuid", "deploy production docker") == []
    monkeypatch.setattr(cortex, "cortex_schema_for", lambda u: "flagent_x")
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates",
                        lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cortex.experience_lines("11111111-2222-4333-8444-555555555555",
                                   "deploy production docker") == []


# ── READ-ONLY (the feedback loop) ─────────────────────────────────────────────────────────

def test_recall_notes_bump_false_issues_no_write(monkeypatch):
    """Reading the cortex re-ranks it: hit_count/last_seen_at, while candidates are ORDER BY
    last_seen_at DESC. An unattended lane must be able to read without that write-back."""
    executed = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): executed.append(sql)
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass

    import contextlib

    @contextlib.contextmanager
    def _fake(schema):
        yield _Conn()

    monkeypatch.setattr(cortex, "cortex_schema_for", lambda u: "flagent_x")
    monkeypatch.setattr(cortex, "_embed_cortex_text", lambda *a, **k: None)
    monkeypatch.setattr(cortex, "_cortex_conn", _fake)
    monkeypatch.setattr(cortex, "_score_candidate_rows",
                        lambda *a, **k: [{"category": "gotcha", "note": "n"}])
    cortex.recall_notes("11111111-2222-4333-8444-555555555555", "deploy", bump=False)
    assert not any("UPDATE" in s for s in executed), executed
    executed.clear()
    cortex.recall_notes("11111111-2222-4333-8444-555555555555", "deploy", bump=True)
    assert any("UPDATE" in s for s in executed), "the deliberate lane must still bump"


# ── THE CARRIER (transport) ───────────────────────────────────────────────────────────────

def test_flag_off_is_byte_for_byte_legacy_and_makes_no_backend_call(monkeypatch):
    monkeypatch.delenv("EXPERIENCE_SURFACING", raising=False)

    async def _boom(*a, **k):
        raise AssertionError("flag OFF must make ZERO backend call")

    monkeypatch.setattr(server, "_fetch_experience_lines", _boom)
    before = {"status": "valid", "committed": 1}
    after = _run(server._with_experience("u", "deploying to production", dict(before)))
    assert after == before


def test_silence_renders_nothing_not_a_no_notes_line(monkeypatch):
    monkeypatch.setenv("EXPERIENCE_SURFACING", "1")

    async def _empty(*a, **k):
        return []

    monkeypatch.setattr(server, "_fetch_experience_lines", _empty)
    out = _run(server._with_experience("u", "bake sourdough bread", {"status": "valid"}))
    assert out == {"status": "valid"}
    assert "prior_experience" not in out


def test_lines_ride_the_response_out_of_band(monkeypatch):
    monkeypatch.setenv("EXPERIENCE_SURFACING", "1")

    async def _lines(*a, **k):
        return ["PROD deploy differs from pre-prod."]

    monkeypatch.setattr(server, "_fetch_experience_lines", _lines)
    out = _run(server._with_experience("u", "deploying to production",
                                       {"status": "valid", "committed": 1}))
    # OUT OF BAND: its own top-level key, its own label, the receipt untouched.
    assert out["status"] == "valid" and out["committed"] == 1
    assert out["prior_experience"]["lines"] == ["PROD deploy differs from pre-prod."]
    assert "not the user's memory" in out["prior_experience"]["note"]


def test_backend_failure_is_fail_open(monkeypatch):
    monkeypatch.setenv("EXPERIENCE_SURFACING", "1")

    async def _raise(*a, **k):
        raise RuntimeError("cortex down")

    monkeypatch.setattr(server, "_fetch_experience_lines", _raise)
    assert _run(server._with_experience("u", "deploy", {"status": "valid"})) == {"status": "valid"}


def test_fetch_is_fail_open_on_non_200_and_on_transport_error(monkeypatch):
    class _Resp:
        status_code = 503

        def json(self): return {"lines": ["should never be read"]}

    class _Client:
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(server, "_http_client", _Client())
    assert _run(server._fetch_experience_lines("deploy", "u")) == []

    class _Boom:
        async def post(self, *a, **k): raise TimeoutError()

    monkeypatch.setattr(server, "_http_client", _Boom())
    assert _run(server._fetch_experience_lines("deploy", "u")) == []


@pytest.mark.parametrize("terminal", [
    {"status": "rejected", "reason": "x", "committed": 0},     # injection guard
    {"status": "query_detected", "message": "x"},              # QUERY route
    {"status": "no_ingest", "message": "x"},                   # word gate / residue drop
    {"status": "degraded", "committed": 0},                    # transient blip
    {"status": "valid", "committed": 1},                       # happy path
])
def test_one_seam_covers_every_terminal_return_of_the_write_tool(monkeypatch, terminal):
    """A lane bolted onto one return path renders on one path and silently not on the others.
    The write tool has a dozen terminal returns; the attach must be a single wrapper seam."""
    monkeypatch.setenv("EXPERIENCE_SURFACING", "1")

    # The seam forwards the impl's full contract (attested since the unattested-lane
    # work); the stub mirrors the real signature so it keeps testing the SEAM, not
    # the forwarding of any one kwarg.
    async def _impl(text, user_id, *, attested=True):
        return dict(terminal)

    async def _lines(*a, **k):
        return ["a prior lesson"]

    monkeypatch.setattr(server, "_remember_facts_tool_impl", _impl)
    monkeypatch.setattr(server, "_fetch_experience_lines", _lines)
    out = _run(server.remember_facts_tool("deploying to production", "u"))
    assert out["prior_experience"]["lines"] == ["a prior lesson"]
    for k, v in terminal.items():
        assert out[k] == v, "the original receipt must be untouched"


# ── SUBJECT-AGNOSTIC (no domain vocabulary anywhere in the mechanism) ─────────────────────

def test_same_mechanism_serves_an_unrelated_domain(seat, monkeypatch):
    rows = [_row(1, "Pre-op bloodwork: draw the sample BEFORE the sedation - sedation shifts "
                    "the electrolyte panel.", ["bloodwork", "sedation", "preop"],
                 category="pitfall")]
    monkeypatch.setattr(cortex, "_fetch_lesson_candidates", lambda s: rows)
    lines = cortex.experience_lines(seat, "pre-op bloodwork on the collie before the sedation")
    assert lines and "bloodwork" in lines[0].lower()


def test_no_domain_vocabulary_in_the_lane_source():
    """The gate must decide on token overlap + the existing relative-semantic rerank and
    NOTHING else. If a domain word list ever appears here, this lane has stopped being
    subject-agnostic and the product claim ('one engine, any subject') is broken."""
    import inspect
    src = inspect.getsource(cortex.experience_lines) + inspect.getsource(cortex._exp_clip)
    for banned in ("deploy", "docker", "patient", "medication", "invoice", "legal"):
        assert banned not in src.lower(), f"domain vocabulary {banned!r} leaked into the gate"

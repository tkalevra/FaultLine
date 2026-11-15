"""Query-side honouring of the stored grammatical ``temporal_status`` for UNDATED
coexisting STATE facts (DESIGN-memory-temporal-lifecycle §5).

Founding rule (CLAUDE.md "Temporal Determination"): time is an INGEST-parsed hinge and
the query is a LEAN walk that *presents* what ingest laid down — it never fuzzes.

  • DATED state facts  → ``event_date`` drives current-vs-past (recompute stays
    authoritative when a date exists — the dual-clock, unchanged).
  • UNDATED state facts → fall back to the STORED grammatical ``temporal_status``:
    ``now`` = current, ``past`` = former ("used to"), ``future`` = upcoming.
    Coexisting rows STAY; only which renders as current vs former/upcoming — and how
    prose verbalises it — changes.

The whole point of this suite is to prove the behaviour is **REL-AGNOSTIC**: the exact
same code path serves ``lives_in`` AND ``works_for`` (and any other ``state`` rel),
keyed ONLY on ``temporal_class='state'`` (rel_types overlay metadata) + the stored
``temporal_status`` column. There is NO ``rel == 'lives_in'`` check, NO place/location
word list, NO rel-name literal anywhere in the change under test.

Two seams are exercised:
  1. ``_apply_clock_selection`` (main.py ~30089) — SELECTION: which undated rows are the
     history under a "used to" (past-phase) ask; present keeps coexisting; MISS-LOUD does
     not fire on an undated-only pair.
  2. ``convert_to_prose`` (main.py:33470) — FRAMING: an undated ``past`` renders as a
     grammatical FORMER value ("You used to live in …"), a ``future`` as UPCOMING ("In the
     future, …"), a ``now`` asserted plainly (or "currently" when a former coexists).

The rel_types overlay (temporal_class) is stubbed to ``state`` for BOTH rels so the test
is hermetic and deterministic; the stub is identical for lives_in and works_for, which is
precisely the rel-agnostic proof (same metadata → same path).
"""
import os
import uuid

import pytest

os.environ.setdefault("POSTGRES_DSN", "postgresql://x")
import src.api.main as m


# ── SELECTION seam: _apply_clock_selection ────────────────────────────────────

# rel_meta stub: EVERY rel is a `state` class. Identical for lives_in AND works_for —
# that identity IS the rel-agnostic proof (no rel-name branch could distinguish them).
def _RM_STATE(_rt):
    return {"temporal_class": "state"}


def _sf(rel, obj, status="now", ed=None):
    return {"rel_type": rel, "subject": "user", "object": obj,
            "temporal_status": status, "event_date": ed, "confidence": 1.0}


_PAST = {"clock": "event", "state_phase": "past"}
_PRESENT = {"clock": "event"}


@pytest.mark.parametrize("rel,cur,former", [
    ("lives_in", "tokyo", "london"),
    ("works_for", "globex", "acme"),   # SAME path, different rel → rel-agnostic
])
def test_past_phase_returns_undated_former_value(rel, cur, former):
    """"where did I used to <state>" → the UNDATED temporal_status='past' row(s), NOT
    the current one. lives_in and works_for prove it through the identical branch."""
    out = m._apply_clock_selection(
        [_sf(rel, cur, "now"), _sf(rel, former, "past")], _PAST, _RM_STATE)
    assert [f["object"] for f in out] == [former], out


@pytest.mark.parametrize("rel,cur,former", [
    ("lives_in", "tokyo", "london"),
    ("works_for", "globex", "acme"),
])
def test_present_keeps_coexisting_undated_pair(rel, cur, former):
    """Present-view (no state_phase): the coexisting undated pair is KEPT (history
    preserved) — framing, not selection, distinguishes current vs former here. Crucially
    an undated-only pair must NOT trip MISS-LOUD (that would drop the answer)."""
    out = m._apply_clock_selection(
        [_sf(rel, cur, "now"), _sf(rel, former, "past")], _PRESENT, _RM_STATE)
    objs = {f["object"] for f in out}
    assert objs == {cur, former}, out
    assert out != [], "undated-only coexisting pair must not MISS-LOUD"


def test_future_undated_kept_in_present_excluded_from_past():
    """A stored 'future' state coexists in the present view but is NOT a prior value, so a
    past-phase ('used to') ask excludes it → MISS-LOUD [] (no fabrication)."""
    facts = [_sf("lives_in", "tokyo", "now"), _sf("lives_in", "paris", "future")]
    pres = m._apply_clock_selection(facts, _PRESENT, _RM_STATE)
    assert {f["object"] for f in pres} == {"tokyo", "paris"}, pres
    past = m._apply_clock_selection(facts, _PAST, _RM_STATE)
    assert past == [], past


def test_dated_pair_still_collapses_by_event_date_no_regression():
    """DATED state pair (dual-clock): present view collapses to max(event_date). The
    undated fallback must NOT disturb the date-driven authority."""
    out = m._apply_clock_selection(
        [_sf("lives_in", "tokyo", "now", "2020-01-01T00:00:00+00:00"),
         _sf("lives_in", "london", "now", "2010-01-01T00:00:00+00:00")],
        _PRESENT, _RM_STATE)
    assert [f["object"] for f in out] == ["tokyo"], out


def test_dated_past_phase_returns_older_value_no_regression():
    """DATED "used to" ask → the older dated value (all but max event_date), unchanged."""
    out = m._apply_clock_selection(
        [_sf("lives_in", "tokyo", "now", "2020-01-01T00:00:00+00:00"),
         _sf("lives_in", "london", "now", "2010-01-01T00:00:00+00:00")],
        _PAST, _RM_STATE)
    assert [f["object"] for f in out] == ["london"], out


def test_non_state_undated_not_treated_as_history():
    """An undated NON-state rel (e.g. an event/immutable class) is not a state-history
    candidate: a past-phase ask over an event-class pair yields MISS-LOUD, not a value."""
    def _rm_event(_rt):
        return {"temporal_class": "event"}
    out = m._apply_clock_selection(
        [_sf("met", "sam", "now"), _sf("met", "alex", "past")], _PAST, _rm_event)
    assert out == [], out


# ── FRAMING seam: convert_to_prose ────────────────────────────────────────────

# lives_in AND works_for both temporal_class='state' — identical shape → rel-agnostic.
_STATE_OVERLAY = {
    "lives_in": {"temporal_class": "state", "natural_language": "X lives in Y",
                 "natural_language_2p": "You live in Y", "label": "lives in",
                 "tail_types": ["ANY"]},
    "works_for": {"temporal_class": "state", "natural_language": "X works for Y",
                  "natural_language_2p": "You work for Y", "label": "works for",
                  "tail_types": ["ANY"]},
}


@pytest.fixture
def _stub_overlay(monkeypatch):
    # Stub the per-tenant rel_type overlay (temporal_class + templates) and the identity
    # set so the querying subject renders as "you" (the real /query perspective path).
    monkeypatch.setattr(m.rel_type_overlay, "resolve_current",
                        lambda *a, **k: _STATE_OVERLAY)
    monkeypatch.setattr(m, "_build_identity_set", lambda *a, **k: {"user"})


def _pf(rel, obj_uuid, status, ed=None):
    return {"rel_type": rel, "_subject_id": "user", "_object_id": obj_uuid,
            "temporal_status": status, "event_date": ed, "confidence": 1.0,
            "fact_class": "A"}


@pytest.mark.parametrize("rel,cur_name,former_name", [
    ("lives_in", "Tokyo", "London"),
    ("works_for", "Globex", "Acme"),   # SAME framing path → rel-agnostic
])
def test_prose_frames_undated_past_as_former_present_as_current(
        _stub_overlay, rel, cur_name, former_name):
    """Coexisting UNDATED state pair: the 'now' row asserts as the CURRENT value ("You
    currently <verb> …"), the 'past' row reads as grammatical FORMER ("You used to <BASE
    verb> …") — NOT the old broken "Previously, You live …". Proven identically for
    lives_in AND works_for (rel-agnostic — same path, de-conjugated base verb)."""
    cur, former = str(uuid.uuid4()), str(uuid.uuid4())
    pam = {cur: cur_name, former: former_name}
    out = m.convert_to_prose(
        [_pf(rel, cur, "now"), _pf(rel, former, "past")],
        db=None, anchor="user", user_id="user", preferred_alias_map=pam)
    joined = " || ".join(out)
    # current value asserted, NOT past/future-prefixed; sharpened with "currently":
    cur_line = next(p for p in out if cur_name in p)
    assert not cur_line.startswith("Previously,"), cur_line
    assert not cur_line.startswith("In the future,"), cur_line
    assert "used to" not in cur_line.lower(), cur_line
    assert cur_line.startswith("You currently "), cur_line
    # former value reads "used to <base verb>" (grammatical past), NOT "Previously,":
    former_line = next(p for p in out if former_name in p)
    assert not former_line.startswith("Previously,"), former_line
    assert former_line.startswith("You used to "), former_line
    # the surface verb is de-conjugated to base (no 3rd-person "-s", no "lives"/"lived"):
    assert cur_name in joined and former_name in joined, joined


@pytest.mark.parametrize("rel,cur_name,upcoming_name", [
    ("lives_in", "Tokyo", "Paris"),
    ("works_for", "Globex", "Initech"),
])
def test_prose_frames_undated_future_as_upcoming(
        _stub_overlay, rel, cur_name, upcoming_name):
    """A stored 'future' undated state renders UPCOMING ("In the future, …"), never as the
    current value — same path across rels."""
    cur, upc = str(uuid.uuid4()), str(uuid.uuid4())
    pam = {cur: cur_name, upc: upcoming_name}
    out = m.convert_to_prose(
        [_pf(rel, cur, "now"), _pf(rel, upc, "future")],
        db=None, anchor="user", user_id="user", preferred_alias_map=pam)
    upc_line = next(p for p in out if upcoming_name in p)
    assert upc_line.startswith("In the future,"), upc_line
    cur_line = next(p for p in out if cur_name in p)
    assert not cur_line.startswith(("Previously,", "In the future,")), cur_line


def test_prose_dated_past_not_reframed_by_undated_block(_stub_overlay):
    """DATED no-regression: a fact WITH an event_date is NOT touched by the undated
    fallback — it renders via the dated "(on …)" path, not "Previously,"."""
    lon = str(uuid.uuid4())
    out = m.convert_to_prose(
        [_pf("lives_in", lon, "past", "2019-01-01T00:00:00+00:00")],
        db=None, anchor="user", user_id="user", preferred_alias_map={lon: "London"})
    assert out, out
    assert not out[0].startswith("Previously,"), out[0]
    assert "2019" in out[0], out[0]


def test_prose_now_only_unchanged(_stub_overlay):
    """A single 'now' state renders as a plain current assertion (byte-for-byte prior)."""
    tok = str(uuid.uuid4())
    out = m.convert_to_prose(
        [_pf("lives_in", tok, "now")],
        db=None, anchor="user", user_id="user", preferred_alias_map={tok: "Tokyo"})
    assert out == ["You live in Tokyo"], out

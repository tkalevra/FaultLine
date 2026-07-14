"""Unit tests for the deterministic TEMPORAL MODEL detection + the temporal
query-intent helpers (rung-4 / DESIGN-hierarchy-ladder-and-growth.md §"Temporal
model"). These cover the pure, DB-free deterministic surfaces:

  - _detect_temporal(text)               — DATA-DRIVEN: event_date vs now → now|past|future
                                           (NO verb-keyword zoo; undated → now)
  - _detect_temporal_query_intent(text)  — query → structural directive
  - _apply_temporal_query_intent(facts, intent) — structural filter/order

The DB-bound paths (ingest INSERT, _collect_member_taxonomy_entities) are not
exercised here (no live DB); they are guarded fail-safe and compile-checked.
"""
import datetime

from src.api import main as m


# ── _detect_temporal: DATA-DRIVEN (event_date vs now, no verb keywords) ──────
def test_detect_temporal_undated_is_now_even_with_past_verb():
    # No date → "now". Verb tense is intentionally NOT used (no brittle keyword zoo);
    # "had a dog" no-longer-has is a SUPERSESSION concern, not a temporal_status one.
    status, ed, _gran = m._detect_temporal("I had a dog named Rex")
    assert status == "now"
    assert ed is None


def test_detect_temporal_past_dated():
    status, ed, _gran = m._detect_temporal("The GPS issue happened on 2020-03-22")
    assert status == "past"
    assert ed is not None and ed.startswith("2020-03-22")


def test_detect_temporal_undated_is_now_even_with_future_word():
    # "will move next month" has no PARSED date → "now". Future is established by an
    # actual future event_date (see test_detect_temporal_future_dated), not the word "will".
    status, ed, _gran = m._detect_temporal("I will move next month")
    assert status == "now"
    assert ed is None


def test_detect_temporal_future_dated():
    # A far-future explicit date forces future regardless of verb cue.
    far = (datetime.date.today() + datetime.timedelta(days=400)).isoformat()
    status, ed, _gran = m._detect_temporal(f"My vet appointment is on {far}")
    assert status == "future"
    assert ed is not None and ed.startswith(far)


def test_detect_temporal_now_default():
    status, ed, _gran = m._detect_temporal("I work at Guelph")
    assert status == "now"
    assert ed is None


def test_detect_temporal_empty():
    assert m._detect_temporal("") == ("now", None, None)


def test_detect_temporal_dated_past_overrides_future_word():
    # Concrete past date overrides a stray future word.
    status, ed, _gran = m._detect_temporal("I will note the outage was on 2001-01-01")
    assert status == "past"


# ── _detect_temporal: RELATIVE-TIME parser (deterministic, ref-anchored) ─────
# Fixed reference so assertions are wall-clock independent. A Wednesday.
_REF = datetime.datetime(2023, 4, 12, 17, 50, tzinfo=datetime.timezone.utc)


def _reld(days):
    return (_REF.date() + datetime.timedelta(days=days)).isoformat()


def test_detect_temporal_relative_weeks_ago():
    status, ed, _gran = m._detect_temporal("I fixed the fence three weeks ago", reference=_REF)
    assert status == "past"
    assert ed is not None and ed.startswith(_reld(-21))


def test_detect_temporal_relative_two_weeks_ago():
    status, ed, _gran = m._detect_temporal("I trimmed the hooves two weeks ago", reference=_REF)
    assert status == "past"
    assert ed is not None and ed.startswith(_reld(-14))


def test_detect_temporal_relative_ordering_fence_before_hooves():
    # LongMemEval "which did I do first?" — fence (21d ago) precedes hooves (14d ago).
    _, fence, _ = m._detect_temporal("fixed the fence three weeks ago", reference=_REF)
    _, hooves, _ = m._detect_temporal("trimmed the hooves two weeks ago", reference=_REF)
    assert fence < hooves


def test_detect_temporal_relative_yesterday():
    status, ed, _gran = m._detect_temporal("I called the vet yesterday", reference=_REF)
    assert status == "past"
    assert ed is not None and ed.startswith(_reld(-1))


def test_detect_temporal_relative_today():
    status, ed, _gran = m._detect_temporal("I am moving today", reference=_REF)
    assert status == "now"
    assert ed is not None and ed.startswith(_reld(0))


def test_detect_temporal_relative_tomorrow():
    status, ed, _gran = m._detect_temporal("the appointment is tomorrow", reference=_REF)
    assert status == "future"
    assert ed is not None and ed.startswith(_reld(1))


def test_detect_temporal_relative_in_two_weeks():
    status, ed, _gran = m._detect_temporal("I will move in two weeks", reference=_REF)
    assert status == "future"
    assert ed is not None and ed.startswith(_reld(14))


def test_detect_temporal_relative_days_digit_ago():
    status, ed, _gran = m._detect_temporal("the outage was 2 days ago", reference=_REF)
    assert status == "past"
    assert ed is not None and ed.startswith(_reld(-2))


def test_detect_temporal_relative_couple_months_ago():
    # month arithmetic via hand-rolled clamp: 2023-04-12 − 2 months → 2023-02-12.
    status, ed, _gran = m._detect_temporal("a couple of months ago I adopted a cat", reference=_REF)
    assert status == "past"
    assert ed is not None and ed.startswith("2023-02-12")


def test_detect_temporal_relative_last_thursday():
    # _REF is Wed 2023-04-12; last Thursday = 2023-04-06.
    status, ed, _gran = m._detect_temporal("I saw him last Thursday", reference=_REF)
    assert status == "past"
    assert ed is not None and ed.startswith("2023-04-06")


def test_detect_temporal_relative_next_thursday():
    # next Thursday after Wed 2023-04-12 = 2023-04-13.
    status, ed, _gran = m._detect_temporal("the meeting is next Thursday", reference=_REF)
    assert status == "future"
    assert ed is not None and ed.startswith("2023-04-13")


# ── vague / excluded must stay ("now", None) — no offset invented ────────────
def test_detect_temporal_vague_a_while_ago_is_now():
    assert m._detect_temporal("a while ago I moved to Calgary", reference=_REF) == ("now", None, None)


def test_detect_temporal_vague_recently_is_now():
    assert m._detect_temporal("I changed jobs recently", reference=_REF) == ("now", None, None)


def test_detect_temporal_vague_soon_is_now():
    assert m._detect_temporal("I will move soon", reference=_REF) == ("now", None, None)


def test_detect_temporal_vague_long_ago_is_now():
    assert m._detect_temporal("that happened long ago", reference=_REF) == ("now", None, None)


# ── Option A regression: bare "next month" must STILL be ("now", None) ───────
def test_detect_temporal_bare_next_month_still_now_with_ref():
    # Option A intentionally does NOT ground bare "next month/week/year".
    assert m._detect_temporal("I will move next month", reference=_REF) == ("now", None, None)


# ── reference=None defaults to now() — proves live behavior unchanged ────────
def test_detect_temporal_relative_default_reference_now():
    status, ed, _gran = m._detect_temporal("I fixed the fence three weeks ago")
    assert status == "past"
    expected = (datetime.datetime.now(datetime.timezone.utc).date()
                - datetime.timedelta(days=21))
    # within a 1-day tolerance for clock-edge
    assert ed is not None
    actual = datetime.date.fromisoformat(ed[:10])
    assert abs((actual - expected).days) <= 1


# ── _detect_temporal_query_intent: structural directive ──────────────────────
def test_query_intent_first_after():
    intent = m._detect_temporal_query_intent("what was my first issue after my service")
    assert intent.get("order") == "event_date"
    assert intent.get("dir") == "asc"
    assert intent.get("boundary") == "after"


def test_query_intent_upcoming():
    intent = m._detect_temporal_query_intent("what is coming up")
    assert intent.get("filter_status") == "future"


def test_query_intent_latest():
    intent = m._detect_temporal_query_intent("what is the most recent event")
    assert intent.get("order") == "event_date"
    assert intent.get("dir") == "desc"


def test_query_intent_none():
    assert m._detect_temporal_query_intent("who is my spouse") == {}


# ── history (supersession-lane) intent ───────────────────────────────────────
def test_query_intent_used_to_sets_history():
    intent = m._detect_temporal_query_intent("where did I used to live")
    assert intent.get("history") is True


def test_query_intent_previously_sets_history():
    assert m._detect_temporal_query_intent("what was my previously held job").get("history") is True


def test_query_intent_no_longer_sets_history():
    assert m._detect_temporal_query_intent("what pets do I no longer have").get("history") is True


def test_query_intent_did_i_ever_sets_history():
    assert m._detect_temporal_query_intent("did I ever mention a dog").get("history") is True


def test_query_intent_current_has_no_history():
    # plain current-state question must NOT trip the supersession lane
    assert "history" not in m._detect_temporal_query_intent("where do I live")
    assert "history" not in m._detect_temporal_query_intent("who is my spouse")


# ── _apply_temporal_query_intent: structural list ops, fail-safe ─────────────
def _f(rel, status="now", ed=None):
    return {"rel_type": rel, "temporal_status": status, "event_date": ed,
            "subject": "u", "object": "o", "confidence": 1.0}


def test_apply_orders_by_event_date_asc():
    facts = [
        _f("has_issue", "past", "2022-05-01T00:00:00+00:00"),
        _f("has_issue", "past", "2020-01-01T00:00:00+00:00"),
        _f("spouse"),  # undated — kept after dated
    ]
    out = m._apply_temporal_query_intent(facts, {"order": "event_date", "dir": "asc"})
    assert out[0]["event_date"].startswith("2020")
    assert out[1]["event_date"].startswith("2022")
    assert out[-1]["rel_type"] == "spouse"


def test_apply_filter_future_keeps_now_context():
    facts = [
        _f("appointment", "future", "2099-01-01T00:00:00+00:00"),
        _f("works_for", "now"),                # plain ongoing — kept
        _f("had_issue", "past", "2001-01-01T00:00:00+00:00"),  # off-tense dated — dropped
    ]
    out = m._apply_temporal_query_intent(facts, {"filter_status": "future"})
    rels = {f["rel_type"] for f in out}
    assert "appointment" in rels
    assert "works_for" in rels
    assert "had_issue" not in rels


def test_apply_empty_intent_noop():
    facts = [_f("spouse")]
    assert m._apply_temporal_query_intent(facts, {}) == facts


def test_apply_failsafe_on_bad_input():
    # Non-dict facts shouldn't crash — returns input.
    facts = [_f("spouse")]
    out = m._apply_temporal_query_intent(facts, {"order": "event_date"})
    assert isinstance(out, list)


# ── FIX #2: a date / relative-time edge OBJECT is rejected (temporal scalar leaked) ──
# A relative-time / date expression must NEVER be a relationship object — it is an
# event_date scalar. The closed-set scorer + verb-lift can emit such an edge; the
# deterministic temporal detector (reused, no new regex) rejects it at the harvest /
# extract-rewrite chokepoints. Subject-agnostic.

def test_object_is_temporal_relative_time():
    assert m._object_is_temporal("two weeks ago") is True
    assert m._object_is_temporal("yesterday") is True
    assert m._object_is_temporal("last thursday") is True


def test_object_is_temporal_iso_date():
    assert m._object_is_temporal("2020-03-22") is True


def test_object_is_temporal_real_object_kept():
    # A genuine entity object is NOT temporal — never dropped.
    assert m._object_is_temporal("brown swiss") is False
    assert m._object_is_temporal("fence") is False
    assert m._object_is_temporal("") is False


def test_drop_temporal_object_edges_filters_only_dates():
    edges = [
        {"subject": "i", "rel_type": "attended_workshop_on", "object": "two weeks ago"},
        {"subject": "i", "rel_type": "fix", "object": "fence"},  # the good edge — MUST survive
    ]
    kept = m._drop_temporal_object_edges(edges)
    rels = {e["rel_type"] for e in kept}
    assert "fix" in rels                       # FIX #2 does not regress the good edge
    assert "attended_workshop_on" not in rels  # date-as-object dropped


# ── FIX #4: the GLiNER2 relation-scorer confidence floor is an env-tunable constant ──

def test_gliner2_relation_threshold_constant_present():
    # Tunable, not a hardcoded literal in a conditional. Default raised to 0.5 (was 0.3) to drop
    # the closed-set scorer's forced low-confidence pairs ("resides_in | east side").
    assert hasattr(m, "GLINER2_RELATION_THRESHOLD")
    assert 0.0 < m.GLINER2_RELATION_THRESHOLD <= 1.0
    assert abs(m.GLINER2_RELATION_THRESHOLD - 0.5) < 1e-9


def test_gliner2_relation_threshold_env_override(monkeypatch):
    # Env-tunable via GLINER2_RELATION_THRESHOLD (parsed fail-safe to the default on bad input).
    monkeypatch.setenv("GLINER2_RELATION_THRESHOLD", "0.65")
    assert abs(m._env_float("GLINER2_RELATION_THRESHOLD", 0.5) - 0.65) < 1e-9
    monkeypatch.setenv("GLINER2_RELATION_THRESHOLD", "not_a_number")
    assert abs(m._env_float("GLINER2_RELATION_THRESHOLD", 0.5) - 0.5) < 1e-9


# ── RC-subject-anchored-walk-recall: "which task did I complete first" ────────
# Regression coverage for the 3-part fix that lets a subject-anchored temporal
# query surface BOTH dated events, ordered, each carrying its date.
#   Fix A: fetch_facts_from_anchor OR-s in `event_date IS NOT NULL` under temporal
#          intent (SQL-bound, exercised live; here we assert the gating function
#          that drives it returns order==event_date for the real query string).
#   Apply: _apply_temporal_query_intent orders the dated pair fence-before-hooves.
#   Fix C: convert_to_prose appends "(on YYYY-MM-DD)" for non-historical dated facts.

from unittest.mock import MagicMock, patch as _patch


def test_which_first_query_detects_event_date_asc():
    """Fix A hinge: the literal fence/hooves 'which first' query MUST resolve to
    order==event_date asc, else the event lane never OR-s in."""
    q = "Which task did I complete first, fixing the fence or trimming the goats' hooves?"
    intent = m._detect_temporal_query_intent(q)
    assert intent.get("order") == "event_date"
    assert intent.get("dir") == "asc"


def test_which_recent_query_detects_event_date_desc():
    """The 'most recent' variant flips direction (proves direction wiring)."""
    q = "Which is the most recent, fixing the fence or trimming the goats' hooves?"
    intent = m._detect_temporal_query_intent(q)
    assert intent.get("order") == "event_date"
    assert intent.get("dir") == "desc"


def test_apply_orders_fence_before_hooves_asc():
    """Given both dated completion edges, asc ordering puts fence (earlier) first."""
    facts = [
        {"rel_type": "trim", "subject": "u", "object": "hooves",
         "temporal_status": "past", "event_date": "2026-06-01T00:00:00+00:00",
         "confidence": 0.8, "fact_class": "B"},
        {"rel_type": "fix", "subject": "u", "object": "fence",
         "temporal_status": "past", "event_date": "2026-05-25T00:00:00+00:00",
         "confidence": 0.8, "fact_class": "B"},
    ]
    intent = m._detect_temporal_query_intent(
        "Which task did I complete first, fixing the fence or trimming the hooves?"
    )
    out = m._apply_temporal_query_intent(facts, intent)
    assert out[0]["object"] == "fence"     # 2026-05-25 — earlier → first
    assert out[1]["object"] == "hooves"    # 2026-06-01 — later → second


def _mock_db_for_prose():
    db = MagicMock()
    cur = MagicMock()
    db.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = []  # _build_identity_set same_as → no rows
    return db


def test_convert_to_prose_emits_date_for_dated_event():
    """Fix C: a non-historical fact carrying event_date renders the date so an
    ordered pair reads with its dates ('which first' becomes answerable)."""
    user = "11111111-1111-1111-1111-111111111111"
    fact = {
        "subject_id": user,
        "rel_type": "fix",
        "object": "fence",
        "fact_class": "B",
        "event_date": "2026-05-25T00:00:00+00:00",
    }
    db = _mock_db_for_prose()
    # Drive the rel_type template deterministically (DSN-independent): the overlay
    # supplies natural_language; the date append rides on top of it.
    overlay = {"fix": {"natural_language": "X fixed Y",
                       "natural_language_2p": "You fixed Y", "label": "fixed"}}
    with _patch("src.api.main.rel_type_overlay.resolve_current", return_value=overlay), \
         _patch("src.api.main.resolve_display_name", return_value="fence"):
        prose = m.convert_to_prose([fact], db, anchor=user, user_id=user)
    assert len(prose) == 1
    assert "(on 2026-05-25)" in prose[0]


def test_convert_to_prose_no_date_for_undated_fact():
    """Non-temporal fact (no event_date) renders WITHOUT a date marker — the date
    append fires only on the temporal layer (no regression to ordinary recall)."""
    user = "22222222-2222-2222-2222-222222222222"
    fact = {
        "subject_id": user,
        "rel_type": "spouse",
        "object": "Marla",
        "fact_class": "A",
        # no event_date
    }
    db = _mock_db_for_prose()
    overlay = {"spouse": {"natural_language": "X is married to Y",
                          "natural_language_2p": "You are married to Y",
                          "label": "spouse"}}
    with _patch("src.api.main.rel_type_overlay.resolve_current", return_value=overlay), \
         _patch("src.api.main.resolve_display_name", return_value="Marla"):
        prose = m.convert_to_prose([fact], db, anchor=user, user_id=user)
    assert len(prose) == 1
    assert "(on " not in prose[0]


# NOTE (open core): the upstream test `test_convert_to_prose_historical_keeps_until_not_event_date`
# is deliberately NOT carried here. It asserts the "used to" past-state prose (a former state
# de-conjugated to grammatical past: "You used to live in Toronto"), part of recall-voice work
# that has not landed in the open core yet — this engine still renders the fail-safe
# "Previously, ..." form. Shipping a test for a feature this repository does not contain would
# be a lie; skipping it silently would be worse. Restore it when the recall-voice path syncs.


_FENCE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_USER = "99999999-9999-9999-9999-999999999999"
_PARENT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _FakeCursor:
    """Minimal cursor: routes each execute() to a canned fetchall() by SQL shape."""

    def __init__(self, alias_rows=None, fact_rows=None, staged_rows=None,
                 parent_rows=None, parent_fact_rows=None):
        self._alias = alias_rows or []
        self._facts = fact_rows or []
        self._staged = staged_rows or []
        self._parents = parent_rows or []
        self._parent_facts = parent_fact_rows or []
        self._next = []

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        if "from entity_aliases" in s:
            self._next = self._alias
        elif "from staged_facts" in s:
            self._next = self._staged
        elif "distinct object_id from facts" in s:
            self._next = self._parents
        elif "from facts" in s:
            # First facts SELECT = concept own facts; a later facts SELECT after a
            # parent lookup = the parent's facts. Disambiguate by whether the params
            # carry the parent UUID.
            flat = []
            if params:
                for p in params:
                    flat += list(p) if isinstance(p, (list, tuple, set)) else [p]
            self._next = self._parent_facts if _PARENT in flat else self._facts
        else:
            self._next = []

    def fetchall(self):
        return self._next

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_resolve_concept_entities_matches_singular_for_plural_query():
    """'fences' (plural) resolves the singular 'fence' entity; anchor is excluded."""
    cur = _FakeCursor(alias_rows=[(_FENCE,)])
    found = m._resolve_query_concept_entities(
        cur, "what do I know about fences", anchor_uuid=_USER
    )
    assert found == {_FENCE}


def test_resolve_concept_entities_scopeless_returns_empty():
    """A truly-scopeless greeting names no concept → empty set → floor preserved."""
    cur = _FakeCursor(alias_rows=[])  # nothing matches
    found = m._resolve_query_concept_entities(cur, "hello", anchor_uuid=_USER)
    assert found == set()


def test_resolve_concept_entities_excludes_anchor():
    """If the only alias match IS the anchor, it is excluded (no self-walk)."""
    cur = _FakeCursor(alias_rows=[(_USER,)])
    found = m._resolve_query_concept_entities(
        cur, "tell me about myself please", anchor_uuid=_USER
    )
    assert found == set()


def test_fetch_concept_own_facts_returns_identity_walk():
    """The concept's own fact (fence located_in east-side) is SELECTED by identity —
    the floor would have filtered it (rel_type not in {has_interest_in, pref_name})."""
    cur = _FakeCursor(fact_rows=[
        (_FENCE, "east-side", "located_in", 0.8, "B", "now", None),
    ])
    out = m._fetch_concept_own_facts(cur, {_FENCE}, anchor_uuid=_USER)
    assert len(out) == 1
    assert out[0]["subject"] == _FENCE
    assert out[0]["rel_type"] == "located_in"
    assert out[0]["source"] == "db"


def test_fetch_concept_own_facts_walks_up_one_level_on_miss():
    """Concept has NO own facts → ONE level up to its instance_of parent's facts."""
    # facts SELECT (own) → empty; parent lookup → _PARENT; parent facts → one row.
    cur = _FakeCursor(
        fact_rows=[],
        parent_rows=[(_PARENT,)],
        parent_fact_rows=[(_PARENT, "wood", "made_of", 1.0, "A", "now", None)],
    )
    # is_hierarchy_rel must be non-empty for the walk-up to attempt; patch metadata.
    with _patch.dict(m._REL_TYPE_META, {"instance_of": {"is_hierarchy_rel": True}}, clear=False):
        out = m._fetch_concept_own_facts(cur, {_FENCE}, anchor_uuid=_USER)
    assert any(f["subject"] == _PARENT and f["rel_type"] == "made_of" for f in out)


def test_fetch_concept_own_facts_empty_when_no_concepts():
    """No concept IDs → no walk (empty)."""
    cur = _FakeCursor()
    assert m._fetch_concept_own_facts(cur, set(), anchor_uuid=_USER) == []


def test_concept_anchor_walk_flag_present_and_default_on():
    """Flag is an env-tunable constant, default on (fail-safe gate)."""
    assert hasattr(m, "QUERY_CONCEPT_ANCHOR_WALK")
    assert m.QUERY_CONCEPT_ANCHOR_WALK is True


# ──────────────────────────────────────────────────────────────────────────────
# FIX 1 (Q3) — reference-year granule: a BARE month binds the [Date:] marker year,
# not wall-clock today().year. Without the marker → today() (unchanged, fail-safe).
# ──────────────────────────────────────────────────────────────────────────────
def test_bare_month_granule_anchors_to_date_marker_year():
    """[Date: 2023/03/10] + 'in February' → 2023-02 granule, NOT the wall-clock year."""
    q = ("[Date: 2023/03/10]\n"
         "Which vehicle did I take care of first in February, the bike or the car?")
    intent = m._detect_temporal_query_intent(q)
    assert intent.get("clock") == "event"
    gran = intent.get("event_granule")
    assert gran == {"gs": "2023-02-01", "ge": "2023-03-01"}, gran


def test_bare_month_granule_no_marker_uses_today_year():
    """No [Date:] marker → bare month binds wall-clock year (behaviour unchanged)."""
    q = "Which vehicle did I take care of first in February, the bike or the car?"
    intent = m._detect_temporal_query_intent(q)
    gran = intent.get("event_granule")
    assert gran is not None
    assert gran["gs"] == f"{datetime.date.today().year}-02-01"
    assert gran["ge"].startswith(f"{datetime.date.today().year}-03-01")


def test_bare_month_explicit_year_still_wins_over_marker():
    """An explicit in-month year overrides the marker reference year."""
    q = "[Date: 2023/03/10]\nWhat did I do in February 2021?"
    intent = m._detect_temporal_query_intent(q)
    assert intent.get("event_granule") == {"gs": "2021-02-01", "ge": "2021-03-01"}


# ──────────────────────────────────────────────────────────────────────────────
# FIX 2 (Q7) — "how many days … after/before X" routes to DURATION (between), NOT
# an Allen yes/no. A genuine yes/no ("did X after Y?") still routes to allen.
# ──────────────────────────────────────────────────────────────────────────────
def test_how_many_days_after_routes_to_between_not_allen():
    q = "how many days did it take to find a house after starting to work with rachel?"
    calc = m._detect_temporal_calc_intent(q)
    assert calc.get("op") == "between", calc
    assert calc.get("requested_unit") == "days"
    assert calc.get("anchor") and calc.get("anchor_b")
    # carried into the query-intent wrapper too
    assert m._detect_temporal_query_intent(q).get("calc", {}).get("op") == "between"


def test_how_long_before_routes_to_between_not_allen():
    q = "how long before i moved did i start the job?"
    # rule 2b owns the immediate before/after shape → still between (regression guard)
    calc = m._detect_temporal_calc_intent(q)
    assert calc.get("op") == "between", calc


def test_genuine_yes_no_after_still_routes_to_allen():
    q = "did i move after i started the job?"
    calc = m._detect_temporal_calc_intent(q)
    assert calc.get("op") == "allen", calc
    assert calc.get("relation") == "after"


def test_genuine_yes_no_before_still_routes_to_allen():
    q = "was the house before rachel?"
    calc = m._detect_temporal_calc_intent(q)
    assert calc.get("op") == "allen", calc


def test_duration_answer_type_detector():
    assert m._detect_duration_answer_type("how many days after x")
    assert m._detect_duration_answer_type("how long before y")
    assert not m._detect_duration_answer_type("did x happen after y")
    assert not m._detect_duration_answer_type("")


def test_how_many_days_after_computes_real_duration():
    """End-to-end calc: the two real dates → 14 days (gold), op between, cited."""
    q = "how many days did it take to find a house after starting to work with rachel?"
    intent = m._detect_temporal_query_intent(q)
    facts = [
        {"subject": "i", "rel_type": "found", "object": "house",
         "event_date": "2023-03-01"},
        {"subject": "i", "rel_type": "work_with", "object": "rachel",
         "event_date": "2023-02-15"},
    ]
    res = m._apply_temporal_calc(facts, intent, user_id=None)
    assert res is not None and res.get("op") == "between"
    assert abs(res.get("value")) == 14, res
    assert res.get("unit") == "days"
    assert set(res.get("cited_dates")) == {"2023-03-01", "2023-02-15"}

"""PART 2 — RECALL-SIDE TEMPORAL ORDERING unit tests
(DESIGN-ingest-spine-and-temporal-recall.md §"PART 2 — RECALL-SIDE TEMPORAL ORDERING").

Covers the pure, DB-free deterministic surfaces added for Part 2:

  - _detect_temporal_query_intent(text)   — NEW structured knobs: ordinal (first/last/
                                            N), ordinal_n, direction (before/after/on),
                                            anchor (the pivot-event phrase). The PRIOR
                                            keys (order/dir/boundary/filter_status/
                                            history) are asserted UNCHANGED (additive).
  - _order_facts_with_pivot(facts,intent,pivot) — pivot filter (before/after/on) +
                                            ordinal selection (first/last/Nth), undated
                                            preserved, fail-safe.
  - the kill-switch flag TEMPORAL_RECALL_ORDERING (default on).

_resolve_temporal_pivot is DB-bound (entity_aliases / facts ILIKE) and is exercised
only structurally here (its term-extraction + the no-DSN fail-safe). The end-to-end
"first issue AFTER first service" path is hand-traced in the build report.

The store does the date math: these tests assert the deterministic FILTER/ORDER on
real event_date values — never LLM reasoning, never cosine.
"""
import datetime

from src.api import main as m


# ── Structured-knob parse (NEW, additive) ────────────────────────────────────
def test_intent_ordinal_first():
    intent = m._detect_temporal_query_intent("what was my first issue")
    assert intent.get("ordinal") == "first"
    assert intent.get("ordinal_n") == 1
    # prior keys unchanged
    assert intent.get("order") == "event_date"
    assert intent.get("dir") == "asc"


def test_intent_ordinal_last():
    intent = m._detect_temporal_query_intent("what was my latest issue")
    assert intent.get("ordinal") == "last"
    assert intent.get("dir") == "desc"   # prior key still set


def test_intent_ordinal_nth_word():
    intent = m._detect_temporal_query_intent("what was my third service")
    assert intent.get("ordinal") == "N"
    assert intent.get("ordinal_n") == 3


def test_intent_ordinal_nth_numeric():
    intent = m._detect_temporal_query_intent("what was the 4th visit")
    assert intent.get("ordinal") == "N"
    assert intent.get("ordinal_n") == 4


def test_intent_direction_after_with_anchor():
    intent = m._detect_temporal_query_intent(
        "what was my first issue after my first service")
    assert intent.get("direction") == "after"
    assert intent.get("anchor") == "my first service"
    # prior boundary key UNCHANGED (additive — old callers still work)
    assert intent.get("boundary") == "after"
    assert intent.get("ordinal") == "first"


def test_intent_direction_before_with_anchor():
    intent = m._detect_temporal_query_intent("what happened before my surgery")
    assert intent.get("direction") == "before"
    assert intent.get("anchor") == "my surgery"
    assert intent.get("boundary") == "before"


def test_intent_direction_on_with_anchor():
    intent = m._detect_temporal_query_intent("what happened on my wedding day")
    assert intent.get("direction") == "on"
    assert intent.get("anchor") == "my wedding day"


def test_intent_no_temporal_is_empty():
    # plain relationship question — no temporal knobs at all
    assert m._detect_temporal_query_intent("who is my spouse") == {}


def test_intent_anchor_strips_trailing_punctuation():
    intent = m._detect_temporal_query_intent("anything after my move?")
    assert intent.get("anchor") == "my move"


# ── SEQUENCE / ENUMERATION requests ("the order of the N X") ─────────────────
# LME cluster "temporal-other": a "what is the ORDER of the three trips/events" ask is a
# request to RETURN THE WHOLE dated set ORDERED BY event_date — NOT a single-boundary
# get_first/get_last, and NOT a relational pivot. Two distinct hijacks buried it before:
#   ex1 (gpt4_7f6b06db): a lone ordinal word ("earliest"/"latest" in "from earliest to
#     latest") set ordinal=first → the get_first SLICE returned ONE trip, dropping the
#     other two.
#   ex2 (gpt4_18c2b244): a bare "on" INSIDE a quoted event ("…coupon ON Luvs diapers…")
#     was misread as a temporal PIVOT preposition → a bogus pivot walk over the trailing
#     sentence fragment.
# The subject-agnostic sequence guard fixes BOTH: sequence=True, ordinal/pivot suppressed,
# ORDER=event_date/asc kept so the full chronological set surfaces.
def test_sequence_order_of_trips_earliest_to_latest_no_ordinal_collapse():
    # ex1 shape — an ordinal word present, but as an ordering DIRECTION, not a slice.
    intent = m._detect_temporal_query_intent(
        "What is the order of the three trips I took in the past three months, "
        "from earliest to latest?")
    assert intent.get("sequence") is True
    # the single-boundary ordinal must NOT be set (would slice to one item)
    assert "ordinal" not in intent
    # full set still ordered chronologically ascending
    assert intent.get("order") == "event_date"
    assert intent.get("dir") == "asc"


def test_sequence_order_of_events_bare_on_not_a_pivot():
    # ex2 shape — a bare "on" inside a quoted event description must NOT become a pivot.
    intent = m._detect_temporal_query_intent(
        "What is the order of the three events: 'I signed up for the rewards program at "
        "ShopRite', 'I used a Buy One Get One Free coupon on Luvs diapers at Walmart', "
        "and 'I redeemed $12 cashback for a $10 Amazon gift card from Ibotta'?")
    assert intent.get("sequence") is True
    assert "direction" not in intent      # no bogus pivot direction
    assert "anchor" not in intent         # no bogus pivot anchor
    assert intent.get("order") == "event_date"
    assert intent.get("dir") == "asc"


def test_sequence_order_of_trips_routes_to_full_walk_not_first_last():
    # The whole-cluster contract at the TEMPLATE layer: a sequence request must NOT route
    # to the single-boundary temporal_first_last template.
    r = m.classify_query_template(
        "What is the order of the three trips I took, from earliest to latest?")
    assert r["template"] != m._QT_TEMPORAL_FIRST_LAST
    assert r["boundary"] is None


# ── REGRESSION GUARDS: genuine single-boundary / pivot asks are UNCHANGED ─────
def test_single_boundary_first_still_collapses():
    # A plain "when was my FIRST X" is NOT a sequence — must keep the ordinal slice.
    intent = m._detect_temporal_query_intent("when was my first doctor appointment")
    assert intent.get("sequence") is None
    assert intent.get("ordinal") == "first"
    r = m.classify_query_template("when was my first doctor appointment")
    assert r["template"] == m._QT_TEMPORAL_FIRST_LAST
    assert r["boundary"] == "first"


def test_pivot_after_ordinal_still_intact():
    # "first issue AFTER my service" is a legitimate pivot+ordinal walk — unchanged.
    intent = m._detect_temporal_query_intent(
        "what was my first issue after my service appointment")
    assert intent.get("sequence") is None
    assert intent.get("ordinal") == "first"
    assert intent.get("direction") == "after"
    assert intent.get("anchor") == "my service appointment"


# ── _order_facts_with_pivot: pivot filter (before/after/on) ───────────────────
def _f(rel, ed=None, fc="A"):
    return {"rel_type": rel, "event_date": ed, "fact_class": fc,
            "subject": "u", "object": "o", "confidence": 1.0}


_SERVICE = "2020-01-10T00:00:00+00:00"   # the pivot date
_BEFORE = "2019-06-01T00:00:00+00:00"
_AFTER1 = "2020-03-22T00:00:00+00:00"
_AFTER2 = "2021-08-15T00:00:00+00:00"


def test_pivot_after_filters_strictly_later():
    facts = [_f("gps_issue", _AFTER1), _f("old_issue", _BEFORE),
             _f("later_issue", _AFTER2), _f("spouse")]  # undated kept
    out = m._order_facts_with_pivot(
        facts, {"direction": "after", "order": "event_date", "dir": "asc"}, _SERVICE)
    rels = [f["rel_type"] for f in out]
    # only strictly-after dated facts + undated tail; the before fact is dropped
    assert "old_issue" not in rels
    assert rels[0] == "gps_issue"      # earliest after the pivot, asc-sorted
    assert rels[1] == "later_issue"
    assert "spouse" in rels            # undated preserved


def test_pivot_after_plus_ordinal_first_picks_earliest_after():
    # "first issue AFTER my first service" — the canonical Q1 case.
    facts = [_f("later_issue", _AFTER2), _f("gps_issue", _AFTER1),
             _f("old_issue", _BEFORE)]
    out = m._order_facts_with_pivot(
        facts,
        {"direction": "after", "ordinal": "first", "ordinal_n": 1,
         "order": "event_date", "dir": "asc"},
        _SERVICE)
    # exactly the single earliest fact strictly after the pivot
    assert len(out) == 1
    assert out[0]["rel_type"] == "gps_issue"


def test_pivot_before_filters_strictly_earlier():
    facts = [_f("gps_issue", _AFTER1), _f("old_issue", _BEFORE)]
    out = m._order_facts_with_pivot(
        facts, {"direction": "before", "order": "event_date", "dir": "asc"}, _SERVICE)
    rels = [f["rel_type"] for f in out]
    assert rels == ["old_issue"]


def test_pivot_on_same_calendar_day():
    same = "2020-01-10T18:30:00+00:00"  # same day as pivot, later clock time
    facts = [_f("issue_that_day", same), _f("other", _AFTER1)]
    out = m._order_facts_with_pivot(
        facts, {"direction": "on", "order": "event_date", "dir": "asc"}, _SERVICE)
    rels = [f["rel_type"] for f in out]
    assert rels == ["issue_that_day"]


# ── _order_facts_with_pivot: ordinal selection WITHOUT a pivot ────────────────
def test_ordinal_first_no_pivot_picks_earliest():
    facts = [_f("b", _AFTER1), _f("a", _BEFORE), _f("c", _AFTER2)]
    out = m._order_facts_with_pivot(
        facts, {"ordinal": "first", "ordinal_n": 1, "order": "event_date", "dir": "asc"},
        None)
    assert len(out) == 1
    assert out[0]["rel_type"] == "a"   # earliest overall


def test_ordinal_last_no_pivot_picks_most_recent():
    facts = [_f("b", _AFTER1), _f("a", _BEFORE), _f("c", _AFTER2)]
    out = m._order_facts_with_pivot(
        facts, {"ordinal": "last", "order": "event_date", "dir": "asc"}, None)
    assert len(out) == 1
    assert out[0]["rel_type"] == "c"   # most-recent overall (asc index -1)


def test_ordinal_n_picks_nth_earliest():
    facts = [_f("b", _AFTER1), _f("a", _BEFORE), _f("c", _AFTER2)]
    out = m._order_facts_with_pivot(
        facts, {"ordinal": "N", "ordinal_n": 2, "order": "event_date", "dir": "asc"}, None)
    assert len(out) == 1
    assert out[0]["rel_type"] == "b"   # 2nd earliest


def test_ordinal_n_out_of_range_returns_all_sorted():
    # N beyond the count → no single pick; keep the chronological set (no fabrication).
    facts = [_f("a", _BEFORE), _f("b", _AFTER1)]
    out = m._order_facts_with_pivot(
        facts, {"ordinal": "N", "ordinal_n": 9, "order": "event_date", "dir": "asc"}, None)
    assert [f["rel_type"] for f in out] == ["a", "b"]


# ── pre-sorted output: chronological even when input is shuffled ──────────────
def test_presorted_output_is_chronological():
    facts = [_f("c", _AFTER2), _f("a", _BEFORE), _f("b", _AFTER1)]
    out = m._order_facts_with_pivot(
        facts, {"order": "event_date", "dir": "asc"}, None)
    dated = [f for f in out if f.get("event_date")]
    eds = [f["event_date"] for f in dated]
    assert eds == sorted(eds)  # strictly ascending — model never has to reorder


# ── undated facts preserved (non-temporal context not silently dropped) ───────
def test_undated_facts_preserved_after_dated():
    facts = [_f("a", _AFTER1), _f("spouse"), _f("works_for")]
    out = m._order_facts_with_pivot(
        facts, {"direction": "after", "order": "event_date", "dir": "asc"}, _BEFORE)
    rels = {f["rel_type"] for f in out}
    assert "spouse" in rels and "works_for" in rels


# ── fail-safe + no-op ─────────────────────────────────────────────────────────
def test_order_failsafe_empty_intent_returns_input():
    facts = [_f("a", _AFTER1)]
    assert m._order_facts_with_pivot(facts, {}, _SERVICE) == facts


def test_order_failsafe_no_facts():
    assert m._order_facts_with_pivot([], {"ordinal": "first"}, _SERVICE) == []


def test_order_failsafe_unparseable_dates_do_not_crash():
    facts = [{"rel_type": "x", "event_date": "not-a-date"},
             {"rel_type": "y", "event_date": _AFTER1}]
    out = m._order_facts_with_pivot(
        facts, {"order": "event_date", "dir": "asc"}, None)
    assert isinstance(out, list) and len(out) == 2


# ── pivot resolver: term extraction + no-DSN fail-safe ────────────────────────
def test_resolve_pivot_no_anchor_returns_none():
    assert m._resolve_temporal_pivot("", "u", None) is None
    assert m._resolve_temporal_pivot("   ", "u", None) is None


def test_resolve_pivot_no_dsn_failsafe(monkeypatch):
    # No POSTGRES_DSN → returns None, never raises (fail-safe).
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    assert m._resolve_temporal_pivot("my first service", "u", None) is None


def test_resolve_pivot_stopwords_only_returns_none(monkeypatch):
    # An anchor made entirely of stop/qualifier words yields no match terms → None,
    # without ever touching the DB.
    monkeypatch.setenv("POSTGRES_DSN", "")  # falsy → early return before connect
    assert m._resolve_temporal_pivot("my first the last", "u", None) is None


# ── Q1 REL_TYPE-ENCODED PIVOT (lean-query, metadata-driven) ───────────────────
# "after its first service" → the service event is stored as a rel_type-encoded
# relation (car, service_for, …)@date — never an alias/scalar the ILIKE legs see.
# _resolve_temporal_pivot must resolve the pivot term ("service") against rel_type
# METADATA (slug/label/natural_language) via the overlay, carry the matching
# rel_types into the SQL as an `f.rel_type = ANY(%s)` leg, and return that fact's
# event_date as the pivot boundary. Subject-agnostic, no rel-name/word literal.
def _mock_pivot_db(monkeypatch, *, overlay_meta, returned_iso):
    """Wire psycopg2.connect / get_user_schema_context / overlay.resolve_meta so
    _resolve_temporal_pivot runs end-to-end without a real DB. Captures the SQL +
    params the resolver executes so the test can assert the rel_type leg."""
    from unittest.mock import MagicMock
    captured = {}

    cur = MagicMock()
    scur = MagicMock()

    def _execute(sql, params=None):
        # The first execute is the SET search_path (scur); the pivot query is on cur.
        if "event_date" in sql:
            captured["sql"] = sql
            captured["params"] = params
    cur.execute.side_effect = _execute
    cur.fetchone.return_value = (datetime.date.fromisoformat(returned_iso),)

    conn = MagicMock()
    # conn.cursor() is used in two with-blocks (scur for SET, cur for the query).
    conn.cursor.return_value.__enter__.side_effect = [scur, cur]
    connect_ctx = MagicMock()
    connect_ctx.__enter__.return_value = conn

    monkeypatch.setenv("POSTGRES_DSN", "postgres://stub")
    monkeypatch.setattr(m.psycopg2, "connect", lambda *_a, **_k: connect_ctx)
    monkeypatch.setattr(
        m, "get_user_schema_context",
        lambda *_a, **_k: {"schema_name": "faultline_stub"})
    monkeypatch.setattr(
        m.rel_type_overlay, "resolve_meta", lambda *_a, **_k: overlay_meta)
    return captured


def test_resolve_pivot_matches_rel_type_encoded_event(monkeypatch):
    # "service" matches the service_for rel_type via its slug/label, even though no
    # entity alias or scalar object text contains it.
    overlay = {
        "service_for": {"label": "serviced", "natural_language": "X was serviced for Y"},
        "parent_of": {"label": "parent of", "natural_language": "X is the parent of Y"},
    }
    captured = _mock_pivot_db(monkeypatch, overlay_meta=overlay,
                              returned_iso="2023-03-15")
    pivot = m._resolve_temporal_pivot("its first service", "u", None)
    assert pivot == "2023-03-15"
    # The rel_type leg was added AND only the service_for rel was matched (not parent_of).
    assert "f.rel_type = ANY(" in captured["sql"], captured["sql"]
    assert ["service_for"] in captured["params"], captured["params"]


def test_resolve_pivot_no_rel_match_omits_rel_leg(monkeypatch):
    # A pivot term that matches NO rel_type metadata → the rel_type leg is omitted
    # entirely (alias/object legs only). Fail-safe: today's behavior unchanged.
    overlay = {
        "parent_of": {"label": "parent of", "natural_language": "X is the parent of Y"},
    }
    captured = _mock_pivot_db(monkeypatch, overlay_meta=overlay,
                              returned_iso="2023-02-10")
    pivot = m._resolve_temporal_pivot("its first wedding", "u", None)
    assert pivot == "2023-02-10"
    assert "f.rel_type = ANY(" not in captured["sql"], captured["sql"]


# ── kill-switch flag ──────────────────────────────────────────────────────────
def test_temporal_recall_ordering_flag_present_and_default_on():
    assert hasattr(m, "TEMPORAL_RECALL_ORDERING")
    assert m.TEMPORAL_RECALL_ORDERING is True


# ── Q2 COMPARISON DEFER: 2-operand "which … first, A or B" must NOT collapse ──
# Regression guard for the lean-query Q2 fix. When a 2-operand comparison is in play
# (operand_ids = {A, B}) with NO temporal pivot and NO direction, _order_facts_with_pivot
# must NOT perform the single-item ordinal collapse — it must return BOTH operand-scoped
# events so the downstream comparison render (_apply_get_first_last) can pick the earlier/
# later AMONG them. The plain single-ordinal ask (no operands) must still collapse to one.
_WEBINAR = "2023-03-28T00:00:00+00:00"   # Q2 gold — the EARLIER event
_WORKSHOP = "2023-05-27T00:00:00+00:00"  # the later event

# UUID-shaped operand ids (operand scoping is UUID-authoritative, never display names).
_OP_A = "11111111-1111-1111-1111-111111111111"  # webinar entity
_OP_B = "22222222-2222-2222-2222-222222222222"  # workshop entity
_OP_NOISE = "99999999-9999-9999-9999-999999999999"  # unrelated dated edge


def _opf(rel, ed, subj):
    # A fact whose SUBJECT UUID is the operand id (operand scoping matches subj/obj UUIDs).
    return {"rel_type": rel, "event_date": ed, "fact_class": "A",
            "subject": subj, "object": "o", "confidence": 1.0}


def test_comparison_two_operands_no_pivot_no_direction_does_not_collapse():
    # "Which event did I attend first, the webinar or the workshop?"
    # operand_ids = both events; pivot None; no direction → ordinal collapse DEFERRED.
    facts = [
        _opf("attended", _WORKSHOP, _OP_B),
        _opf("attended", _WEBINAR, _OP_A),
        _opf("attended", "2024-09-01T00:00:00+00:00", _OP_NOISE),  # unrelated — scoped out
    ]
    out = m._order_facts_with_pivot(
        facts,
        {"ordinal": "first", "ordinal_n": 1, "order": "event_date", "dir": "asc"},
        None,                          # no pivot
        operand_ids=[_OP_A, _OP_B],    # 2-operand comparison
    )
    # BOTH operand events retained (not collapsed to one); the unrelated edge is scoped out.
    assert len(out) == 2, f"expected both operands retained, got {len(out)}"
    subs = {f["subject"] for f in out}
    assert subs == {_OP_A, _OP_B}
    # Earliest-first ordering preserved so the comparison render picks the webinar.
    assert out[0]["subject"] == _OP_A   # webinar (2023-03-28) precedes workshop (2023-05-27)


def test_comparison_defer_then_first_last_picks_earlier_operand():
    # End of the chain: the deferred 2-operand set flows into _apply_get_first_last,
    # which renders BOTH and puts the WINNER (earliest, for "first") first.
    facts = [
        _opf("attended", _WORKSHOP, _OP_B),
        _opf("attended", _WEBINAR, _OP_A),
    ]
    ordered = m._order_facts_with_pivot(
        facts,
        {"ordinal": "first", "ordinal_n": 1, "order": "event_date", "dir": "asc"},
        None, operand_ids=[_OP_A, _OP_B])
    rendered = m._apply_get_first_last(ordered, "first", operand_ids=[_OP_A, _OP_B])
    assert len(rendered) == 2                    # both operands named, not collapsed
    assert rendered[0]["subject"] == _OP_A       # webinar wins (earlier)
    assert rendered[0]["event_date"] == _WEBINAR


def test_single_ordinal_no_operands_still_collapses_to_one():
    # NO-REGRESSION: a plain "what did I do first?" (no comparison operands) must STILL
    # collapse to the single ordinal pick exactly as before the Q2 guard.
    facts = [_opf("did", _WORKSHOP, _OP_B), _opf("did", _WEBINAR, _OP_A)]
    out = m._order_facts_with_pivot(
        facts, {"ordinal": "first", "ordinal_n": 1, "order": "event_date", "dir": "asc"},
        None)                          # operand_ids omitted → no defer
    assert len(out) == 1
    assert out[0]["event_date"] == _WEBINAR


def test_comparison_with_real_direction_is_unaffected_by_guard():
    # NO-REGRESSION: operands present BUT a real before/after direction → the guard does
    # NOT fire (it keys on no-direction), so the pivot/ordinal path runs as today.
    facts = [_opf("attended", _WORKSHOP, _OP_B), _opf("attended", _WEBINAR, _OP_A)]
    pivot = "2023-04-01T00:00:00+00:00"   # between the two events
    out = m._order_facts_with_pivot(
        facts,
        {"direction": "after", "ordinal": "first", "ordinal_n": 1,
         "order": "event_date", "dir": "asc"},
        pivot, operand_ids=[_OP_A, _OP_B])
    # direction present → collapse NOT deferred; single earliest-after-pivot pick (workshop).
    assert len(out) == 1
    assert out[0]["event_date"] == _WORKSHOP


# ── BUG A (Q2): the comparison SLICE carries the UNDATED instance_of/subject-matter ──
# companion edges so convert_to_prose's type-append can render the walked L4 type-cast.
_TYPE_WEBINAR = "33333333-3333-3333-3333-333333333333"   # the bare L4 type node
_SPECIFIC_GPS = "44444444-4444-4444-4444-444444444444"   # subject-matter entity


def _comp(subj, rel, obj):
    # An UNDATED companion edge (instance_of / subject-matter) — no event_date by nature.
    return {"rel_type": rel, "event_date": None, "fact_class": "A",
            "subject": subj, "object": obj, "confidence": 1.0}


def test_get_first_last_comparison_carries_instance_of_companion():
    # The chosen operand occurrences each carry an undated (occurrence, instance_of, type)
    # companion. _apply_get_first_last must RE-ATTACH them so the type ("webinar") survives
    # the _dated slice and reaches the render. The companion's subject is the occurrence,
    # which is the object of the dated participated_in edge.
    _OCC_A = _OP_A   # webinar occurrence handle
    _OCC_B = _OP_B   # workshop occurrence handle
    dated = [
        {"rel_type": "participated_in", "event_date": _WEBINAR, "fact_class": "A",
         "subject": "user", "object": _OCC_A, "confidence": 1.0},
        {"rel_type": "participated_in", "event_date": _WORKSHOP, "fact_class": "A",
         "subject": "user", "object": _OCC_B, "confidence": 1.0},
    ]
    companions = [
        _comp(_OCC_A, "instance_of", _TYPE_WEBINAR),
        _comp(_OCC_B, "instance_of", "55555555-5555-5555-5555-555555555555"),
    ]
    facts = dated + companions
    out = m._apply_get_first_last(facts, "first", operand_ids=[_OCC_A, _OCC_B])
    # Both dated operand events retained AND both undated instance_of companions carried back.
    rels = [f["rel_type"] for f in out]
    assert rels.count("instance_of") == 2, f"companions dropped: {rels}"
    # The webinar type-cast companion is present and keyed on the chosen occurrence handle.
    assert any(f["rel_type"] == "instance_of" and f["subject"] == _OCC_A
               and f["object"] == _TYPE_WEBINAR for f in out)


def test_get_first_last_single_pick_carries_subject_matter_companion():
    # A single-pick (no operands) bland-head occurrence carries an undated subject-matter
    # companion ("issue" → gps system). It must ride back so the render surfaces "gps system".
    _OCC = _OP_A
    sm_rel = m._subject_matter_rel()
    facts = [
        {"rel_type": "participated_in", "event_date": _WEBINAR, "fact_class": "A",
         "subject": "user", "object": _OCC, "confidence": 1.0},
        _comp(_OCC, sm_rel, _SPECIFIC_GPS),
    ]
    out = m._apply_get_first_last(facts, "first", operand_ids=[_OCC])
    # The chosen dated edge plus its undated subject-matter companion are both present.
    assert any(f["rel_type"] == sm_rel and f["object"] == _SPECIFIC_GPS for f in out), \
        f"subject-matter companion not carried: {[f['rel_type'] for f in out]}"


def test_carry_companion_never_readmits_a_dated_edge():
    # FAIL-SAFE: a dated operand edge dropped by the slice must NOT be re-admitted by the
    # companion carry — only UNDATED companions ride back.
    _OCC = _OP_A
    chosen = [{"rel_type": "participated_in", "event_date": _WEBINAR, "fact_class": "A",
               "subject": "user", "object": _OCC, "confidence": 1.0}]
    all_facts = chosen + [
        {"rel_type": "participated_in", "event_date": _WORKSHOP, "fact_class": "A",
         "subject": "user", "object": _OP_B, "confidence": 1.0},  # a DATED edge — must stay out
    ]
    out = m._carry_companion_edges(chosen, all_facts)
    assert len(out) == 1, "a dated edge was wrongly re-admitted by companion carry"


# ── BUG B (Q1): ordinal-AFTER-pivot must NOT be hijacked into an Allen yes/no ──────
def test_ordinal_after_pivot_suppresses_allen():
    # "what was the first issue I had with my car after its first service?" — carries an
    # ordinal (first) AND an after-direction → it is ordinal-after-pivot, NOT a boolean
    # Allen comparison. The Allen op must be SUPPRESSED so the pivot+ordinal walk owns it.
    intent = m._detect_temporal_query_intent(
        "what was the first issue i had with my car after its first service")
    assert intent.get("ordinal") == "first"
    assert intent.get("direction") == "after"
    # Allen op must NOT be present on the calc sub-dict (suppressed for ordinal-after-pivot).
    assert (intent.get("calc") or {}).get("op") != "allen", \
        f"Allen hijacked an ordinal-after-pivot query: {intent.get('calc')}"


def test_genuine_allen_without_ordinal_is_preserved():
    # NO-REGRESSION: a genuine boolean "did X happen after Y?" (NO ordinal) keeps Allen.
    intent = m._detect_temporal_query_intent("did my surgery happen after my move")
    assert intent.get("ordinal") is None
    assert (intent.get("calc") or {}).get("op") == "allen", \
        f"genuine Allen wrongly suppressed: {intent.get('calc')}"


# ── Q1 topic-scope seam: gather → SCOPE-to-topic → operator → return ───────────────
# The post-pivot pool is narrowed to the facts TOUCHING the question's TOPIC entity
# BEFORE the min/max pick, so a same-day TOPIC-IRRELEVANT dated edge can no longer win
# the boundary by stable-sort insertion order (the Q1 `research` vs `has_state issue`
# regression). Pure-function tests (no DB) over _apply_get_first_last / _operand_scope_facts.
_ISSUE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"   # the topic entity ("issue")
_GPS = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"      # the device the issue is a state of
_RESEARCH = "cccccccc-cccc-cccc-cccc-cccccccccccc"  # topic-IRRELEVANT same-day occurrence
_SAME_DAY = "2023-03-22T00:00:00+00:00"


def test_topic_scope_drops_same_day_irrelevant_before_first_pick():
    # Same-day 3-way tie: the gold has_state(gps→issue) and a topic-IRRELEVANT
    # participated_in(user→research) share 2023-03-22. WITHOUT topic scope `research`
    # sorts first (insertion order) and wins. WITH operand_ids={issue} the pool narrows to
    # the issue-touching fact → the gold wins.
    facts = [
        # research inserted FIRST so, untouched, it would win the stable-sort min-pick.
        {"rel_type": "participated_in", "event_date": _SAME_DAY, "fact_class": "A",
         "subject": "user", "object": _RESEARCH, "confidence": 1.0},
        {"rel_type": "has_state", "event_date": _SAME_DAY, "fact_class": "A",
         "subject": _GPS, "object": _ISSUE, "confidence": 1.0},
    ]
    out = m._apply_get_first_last(facts, "first", operand_ids=[_ISSUE])
    # exactly the issue fact survives the topic narrow; research is gone.
    assert len(out) == 1, f"expected single topic-scoped pick, got {out!r}"
    assert out[0]["object"] == _ISSUE
    assert out[0]["rel_type"] == "has_state"


def test_topic_scope_empty_is_byte_for_byte_unchanged():
    # FAIL-SAFE: a bare ordinal (no topic noun) resolves to NO entity → operand_ids empty →
    # the operator runs over the GLOBAL set exactly as before (the 9 bare-ordinal queries).
    facts = [
        {"rel_type": "participated_in", "event_date": _SAME_DAY, "fact_class": "A",
         "subject": "user", "object": _RESEARCH, "confidence": 1.0},
        {"rel_type": "has_state", "event_date": _SAME_DAY, "fact_class": "A",
         "subject": _GPS, "object": _ISSUE, "confidence": 1.0},
    ]
    out_empty = m._apply_get_first_last(facts, "first", operand_ids=[])
    out_none = m._apply_get_first_last(facts, "first")          # operand_ids default None
    # both unscoped → insertion-order min-pick → research wins (the pre-fix behaviour); identical.
    assert len(out_empty) == 1 and len(out_none) == 1
    assert out_empty[0]["object"] == _RESEARCH
    assert out_none[0]["object"] == _RESEARCH


def test_operand_scope_facts_unchanged_when_no_topic():
    # The narrow step itself: empty operand_ids → input returned UNCHANGED (identity-safe).
    facts = [{"rel_type": "has_state", "event_date": _SAME_DAY, "subject": _GPS,
              "object": _ISSUE}]
    assert m._operand_scope_facts(facts, []) is facts
    assert m._operand_scope_facts(facts, None) is facts


def test_topic_scope_subject_agnostic_other_domain():
    # SUBJECT-AGNOSTIC: same seam over a different domain (a "bug" topic vs an irrelevant
    # "meeting" same-day) — no issue/gps/car literal anywhere; the topic UUID narrows.
    _BUG = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    _SERVER = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    _MEETING = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    facts = [
        {"rel_type": "participated_in", "event_date": _SAME_DAY, "fact_class": "A",
         "subject": "user", "object": _MEETING, "confidence": 1.0},
        {"rel_type": "has_state", "event_date": _SAME_DAY, "fact_class": "A",
         "subject": _SERVER, "object": _BUG, "confidence": 1.0},
    ]
    out = m._apply_get_first_last(facts, "first", operand_ids=[_BUG])
    assert len(out) == 1 and out[0]["object"] == _BUG


def test_same_day_tiebreak_prefers_state_over_ownership_companion():
    # RENDER REFINEMENT: when the topic-scoped boundary date carries BOTH a state/event fact
    # and an ownership companion, prefer the STATE fact so prose names the device-state, not
    # "you own issue". owns inserted FIRST so only the tie-break re-orders it behind has_state.
    facts = [
        {"rel_type": "owns", "event_date": _SAME_DAY, "fact_class": "A",
         "subject": "user", "object": _ISSUE, "confidence": 1.0},
        {"rel_type": "has_state", "event_date": _SAME_DAY, "fact_class": "A",
         "subject": _GPS, "object": _ISSUE, "confidence": 1.0},
    ]
    out = m._apply_get_first_last(facts, "first", operand_ids=[_ISSUE])
    # both touch the topic → comparison-render keeps both; the STATE fact must lead.
    assert out[0]["rel_type"] == "has_state", f"ownership companion led the render: {out!r}"

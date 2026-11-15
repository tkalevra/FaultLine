"""SCALAR RETRACT / RECALL LEAK (gauntlet scalar-retract-recall-leak, bars RL1-RL5).

THE WOUND, reproduced end-to-end on pre-prod through production code paths before any
fix (nothing SQL-seeded): a scalar the user RETRACTED was served straight back by
recall, in the ASSERTED voice.

    ingest (MCP remember_facts, spine)  -> fragments "vorlance quandle" (user-linked,
                                          0 own scalars) and "55-inch smart quandle for
                                          living room" (height=55-inch, user_stated)
    POST /retract/correct RETRACTION    -> correct_fact.scalar_retraction_retired retired=1
                                          row RETAINED, superseded_at + valid_until stamped,
                                          entity_attributes_history action=retired
    POST /query "What is the height of the vorlance quandle?"
                                        -> {"object": "55-inch", "fact_class": "A",
                                            "confidence": 1.0, "source": "attributes"}
    MCP recall_memory, same question     -> "=== YOUR MEMORY ===\\n55-inch smart quandle
                                            for living room's height is 55-inch"
                                           (the ASSERTED band of server.py::_emit)

Mechanism: the SCALAR SIBLING-FRAGMENT lane in `main.py::fetch_facts_from_anchor` (log
event `fetch_facts.scalar_sibling_fragment_surfaced`) was the ONE current-state attribute
read in the recall path with no `superseded_at IS NULL` filter, and it stamped a flat
Class A / confidence 1.0 on whatever it found. Harmless while a scalar retraction
hard-DELETEd the row; a live leak the moment `456981b7` correctly made retraction
non-destructive — so this is a regression that campaign INTRODUCED, and the very comment
justifying it asserted the opposite ("every attribute read filters superseded_at IS NULL").

Conventions follow tests/test_scalar_supersession.py and tests/fact_store/test_replay_guard.py:
the SQL TEXT and the emitted fact dict are asserted directly against a dispatching fake
cursor. Row-level truth lives in the pre-prod battery
(DEV/review/gauntlet/scalar-retract-recall-leak/).
"""

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.api.main import (  # noqa: E402
    _scalar_read_class_confidence,
    fetch_facts_from_anchor,
)
from src.api.models import QueryPath  # noqa: E402


ANCHOR = "92016beb-4232-5985-998e-cf19b4fdc689"    # "vorlance quandle" — NO own scalar
SIBLING = "53426bb4-565a-5dfe-bee8-bdde120050f1"   # holds height
USER = "04c0787b-8959-48de-a07b-4f0b60d11e43"
VALUE_Q = "What is the height of the vorlance quandle?"


class _DispatchCursor:
    """Answers by SQL TEXT (never call order) and RECORDS every statement, so a test can
    assert on the SQL the lane actually issued as well as on what it emitted."""

    def __init__(self, sibling_rows):
        self._last = ""
        self.seen = []
        self.sibling_rows = sibling_rows

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())
        self.seen.append(self._last)

    def _matches(self, *needles):
        return all(n in self._last for n in needles)

    def sibling_sql(self):
        for s in self.seen:
            if "FROM entity_attributes ea" in s and "JOIN entity_aliases al" in s:
                return s
        return None

    def fetchone(self):
        if self._matches("SELECT alias FROM entity_aliases",
                         "is_preferred = true", "LIMIT 1"):
            return ("vorlance quandle",)
        return None

    def fetchall(self):
        if self._matches("FROM entity_attributes ea", "JOIN entity_aliases al"):
            return self.sibling_rows
        if self._matches("FROM entity_attributes", "WHERE entity_id"):
            return []          # the anchor carries no scalar of its own
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(query_text, sibling_rows):
    with patch.dict(os.environ, {"POSTGRES_DSN": "postgresql://test"}):
        with patch("src.api.main.psycopg2.connect") as mock_connect:
            cur = _DispatchCursor(sibling_rows)
            conn = MagicMock()
            conn.cursor.return_value = cur
            conn.cursor.return_value.__enter__ = lambda s: cur
            conn.cursor.return_value.__exit__ = lambda s, *a: False
            mock_connect.return_value.__enter__.return_value = conn
            facts = fetch_facts_from_anchor(
                ANCHOR, USER, QueryPath(fetch_all_details=True), query_text=query_text)
            return facts, cur


def _row(provenance="user_stated", value="55-inch", attribute="height"):
    return (SIBLING, "55-inch smart quandle for living room", attribute, value,
            None, None, None, provenance)


def _surfaced(facts, value="55-inch"):
    return [f for f in facts
            if f.get("source") == "attributes" and f.get("subject") == SIBLING
            and str(f.get("object", "")).lower() == value.lower()]


# ── RL1 — a RETIRED scalar is never read by the current-state lane ────────────────────

def test_rl1_sibling_lane_filters_retired_rows():
    """The retired row must be excluded IN SQL. The retraction lane RETAINS the row
    (superseded_at + valid_until stamped, value intact), so a lane that does not filter
    reads it back — which is exactly what happened live."""
    _facts, cur = _run(VALUE_Q, [_row()])
    sql = cur.sibling_sql()
    assert sql is not None, "sibling-fragment lane never issued its query"
    assert "ea.superseded_at IS NULL" in sql, (
        "SCALAR RETRACTION LEAK: the sibling-fragment lane reads entity_attributes "
        "without the retirement filter, so a value the user explicitly deleted is "
        f"served back. SQL={sql!r}")


def test_rl1_filter_targets_the_attribute_row_not_the_alias():
    """The predicate must be qualified to `ea` — entity_aliases has no superseded_at, and
    an unqualified `superseded_at` here would be ambiguous or bind the wrong table."""
    _facts, cur = _run(VALUE_Q, [_row()])
    sql = cur.sibling_sql()
    assert "ea.superseded_at IS NULL" in sql
    assert "al.superseded_at" not in sql


# ── RL2 — the lane still does its job (regression bar) ────────────────────────────────

def test_rl2_live_sibling_scalar_still_surfaces():
    """A LIVE user-stated sibling scalar is surfaced exactly as before the fix — Class A,
    confidence 1.0. A fix that blinds the lane fails here."""
    facts, _cur = _run(VALUE_Q, [_row()])
    hits = _surfaced(facts)
    assert hits, f"live sibling scalar no longer surfaces — facts={facts!r}"
    assert hits[0]["fact_class"] == "A"
    assert hits[0]["confidence"] == 1.0
    assert hits[0]["rel_type"] == "height"


def test_rl2_non_interrogative_still_gated_off():
    """The lane's own value-question gate is untouched by this fix."""
    facts, _cur = _run("Tell me about the vorlance quandle", [_row()])
    assert not _surfaced(facts)


# ── RL3 — a RETIRED relationship does not qualify a sibling ───────────────────────────

def test_rl3_user_link_subquery_filters_facts_tombstones():
    """A sibling qualifies only through a LIVE link to the user. The subquery filtered
    none of the facts tombstones, so a retracted `(user, owns, X)` still made X's values
    answer as the user's — the same retire-and-retain asymmetry, one join out."""
    _facts, cur = _run(VALUE_Q, [_row()])
    sql = cur.sibling_sql()
    body = sql[sql.index("ea.entity_id IN ("):]
    # BOTH facts branches (object side and subject side) must carry all three tombstones.
    assert body.count("superseded_at IS NULL AND archived_at IS NULL AND deleted_at IS NULL") == 2, (
        f"facts user-link branches not tombstone-filtered — subquery={body!r}")


def test_rl3_user_link_subquery_filters_staged_deleted():
    """staged_facts carries only deleted_at (verified against the live per-tenant schema);
    both staged branches must filter it."""
    _facts, cur = _run(VALUE_Q, [_row()])
    sql = cur.sibling_sql()
    body = sql[sql.index("ea.entity_id IN ("):]
    staged = [seg for seg in body.split("UNION") if "staged_facts" in seg]
    assert len(staged) == 2, f"expected 2 staged branches, got {len(staged)}: {body!r}"
    for seg in staged:
        assert "deleted_at IS NULL" in seg, f"staged branch not tombstone-filtered: {seg!r}"
    # staged_facts has no superseded_at/archived_at column — asking for one is an
    # UndefinedColumn that ABORTS the caller's transaction (the class of bug that made
    # `_query_scoped_to_absent_concept` poison whole /query requests).
    for seg in staged:
        assert "superseded_at" not in seg and "archived_at" not in seg


# ── RL4 — the emitted class/confidence is honest ──────────────────────────────────────

def test_rl4_chat_lane_provenance_is_user_truth():
    """`entity_attributes.provenance` cannot be ASSUMED canonical. The scalar WRITE path
    already canonicalises (`main.py::ingest`, log `ingest.scalar_provenance_routed`), so
    NEW rows are canonical; what is not is the LEGACY population written before that
    landed, and the column has no CHECK constraint. Measured across all 45 pre-prod
    schemas it takes six values, 251 of 384 rows non-canonical. `mcp`/`assistant`/
    `user_corrected` are the ordinary chat lanes and ARE the user's words — canonicalising
    them to anything else would DEMOTE user truth, the opposite error to the leak.

    ⚠️ This docstring used to say the column "stores the ingest SOURCE" flat. That was the
    overstatement ledger entry 16 retracted and `_SCALAR_READ_PROVENANCE_CANON` corrects —
    and it shipped HERE, one file over, in the same commit set as its own correction. A
    blind critic found it. Correcting a claim means correcting EVERY copy of it, including
    the ones in test docstrings, which is exactly where a retracted claim hides."""
    for prov in ("user_stated", "user_corrected", "mcp", "assistant"):
        cls, conf = _scalar_read_class_confidence(prov, "height")
        assert (cls, conf) == ("A", 1.0), f"{prov} -> {(cls, conf)}"


def test_rl4_inferred_provenance_is_not_asserted_as_user_truth():
    """An engine-inferred scalar must NOT be stamped Class A / 1.0 — that promotes it into
    the ASSERTED band of server.py::_emit, where the model states it as its own knowledge."""
    for prov in ("llm_inferred", "reextract", "unattested_correction", None, "", "garbage"):
        cls, conf = _scalar_read_class_confidence(prov, "height")
        assert not (cls == "A" and conf == 1.0), (
            f"provenance {prov!r} fabricated user-stated authority: {(cls, conf)}")


def test_rl4_engine_growth_provenance_never_class_a():
    """llm_learned is /expand self-growth — never Class A, per the provenance router."""
    for prov in ("llm_learned", "llm_learn"):
        cls, _conf = _scalar_read_class_confidence(prov, "height")
        assert cls != "A", f"{prov} reached Class A"


def test_rl4_lane_emits_the_honest_class():
    """End of the seam: an inferred sibling row surfaces (we do not forget) but NOT in the
    asserted-user-truth tier."""
    facts, _cur = _run(VALUE_Q, [_row(provenance="llm_inferred")])
    hits = _surfaced(facts)
    assert hits, "inferred sibling scalar was dropped — RL4 must retier, not delete"
    assert not (hits[0]["fact_class"] == "A" and hits[0]["confidence"] == 1.0), (
        f"lane still fabricates user-stated authority: {hits[0]!r}")


def test_rl4_emitted_provenance_drives_emit_banding():
    """RL4's second half — "verify what `_emit` then does with it".

    `src/mcp/server.py::_fact_is_asserted` decides the recall VOICE on PROVENANCE first
    and only falls back to the capture tier when provenance is ABSENT (its own docstring:
    "Class-C membership is a CAPTURE/STRUCTURAL state ... not a credibility verdict"). So
    the lane carries the canonical provenance, and an inferred scalar is HELD because it is
    a guess — not incidentally because its tier happened to be C."""
    from src.mcp.server import _fact_is_asserted

    facts, _cur = _run(VALUE_Q, [_row(provenance="user_stated")])
    hit = _surfaced(facts)[0]
    assert hit["fact_provenance"] == "user_stated"
    assert _fact_is_asserted(hit["fact_provenance"], hit["confidence"],
                             hit["fact_class"]) is True

    facts, _cur = _run(VALUE_Q, [_row(provenance="llm_inferred")])
    hit = _surfaced(facts)[0]
    assert hit["fact_provenance"] == "llm_inferred"
    assert _fact_is_asserted(hit["fact_provenance"], hit["confidence"],
                             hit["fact_class"]) is False, (
        "an engine-inferred scalar still reads in the ASSERTED voice — the model would "
        "state a guess as its own knowledge")


def test_rl4_chat_lane_row_is_asserted_not_hedged():
    """The inverse guard: `mcp`/`assistant` are the ordinary chat lanes and ARE the user's
    words. Canonicalising them anywhere below user_stated would HEDGE user truth — the same
    error as the leak, pointed the other way."""
    from src.mcp.server import _fact_is_asserted

    for prov in ("mcp", "assistant", "user_corrected"):
        facts, _cur = _run(VALUE_Q, [_row(provenance=prov)])
        hit = _surfaced(facts)[0]
        assert hit["fact_provenance"] == "user_stated", f"{prov} -> {hit['fact_provenance']}"
        assert _fact_is_asserted(hit["fact_provenance"], hit["confidence"],
                                 hit["fact_class"]) is True


# ── RL4, LANE-LEVEL — these must go RED if a lane fix is reverted ─────────────────────
#
# ⚠️ The earlier RL4 tests were VACUOUS and two critics proved it the same way: revert all
# three round-4 lane fixes to a flat ("A", 1.0) and the suite stayed green, because every
# assertion went through the sibling lane only. A test that cannot detect the removal of the
# fix it is named after is decoration. These call the lanes DIRECTLY.

def _prov_cursor(rows):
    """Minimal cursor returning `rows` for any SELECT."""
    class _C:
        def execute(self, sql, params=None): self._sql = sql
        def fetchall(self): return rows
        def fetchone(self): return rows[0] if rows else None
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _C()


def _conn_for(rows):
    from unittest.mock import MagicMock
    cur = _prov_cursor(rows)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    return conn


def test_rl4_traversed_lane_reports_the_stored_provenance():
    """`_fetch_traversed_entity_attributes` must not stamp a flat A/1.0. It re-emits the SAME
    (subject, rel_type, object) as the sibling lane, so a fabricated 1.0 here wins
    `deduplicate_facts`' highest-confidence tiebreak and silently overwrites the honest tier
    — measured live before the fix as `A / 1.0 / fact_provenance None`."""
    from src.api.main import _fetch_traversed_entity_attributes as f
    # (entity_id, attribute, v_text, v_int, v_float, v_date, sensitivity, provenance)
    inferred = f(_conn_for([("e1", "weight", "47 pounds", None, None, None,
                             "public", "llm_inferred")]), {"e1"}, anchor_uuid="anchor")
    assert inferred, "lane dropped the row instead of retiering it"
    assert not (inferred[0]["fact_class"] == "A" and inferred[0]["confidence"] == 1.0), (
        f"traversed lane still fabricates user authority: {inferred[0]!r}")
    assert inferred[0]["fact_provenance"] == "llm_inferred"

    stated = f(_conn_for([("e1", "weight", "47 pounds", None, None, None,
                           "public", "user_stated")]), {"e1"}, anchor_uuid="anchor")
    assert (stated[0]["fact_class"], stated[0]["confidence"]) == ("A", 1.0)
    assert stated[0]["fact_provenance"] == "user_stated"


def test_rl4_entity_facts_lane_reports_the_stored_provenance():
    """Same seam, the entity-centric expansion half."""
    from src.api.main import _fetch_entity_facts as f
    rows = [("e1", "weight", "47 pounds", None, None, None, "llm_inferred")]
    out = [x for x in f(_conn_for(rows), ["e1"], None) or []
           if x.get("source") == "attributes"]
    if out:  # the lane also emits relational rows from the same cursor; scalars are enough
        assert not (out[0]["fact_class"] == "A" and out[0]["confidence"] == 1.0), (
            f"entity-facts lane still fabricates user authority: {out[0]!r}")
        assert out[0]["fact_provenance"] == "llm_inferred"


def test_rl4_typed_walk_scalar_answer_reports_the_stored_provenance():
    """`resolve_typed_walk` returns `status:"answer"` and SHORT-CIRCUITS — it never reaches
    `deduplicate_facts`, so it cannot be corrected downstream by an honest copy the way the
    expansion lanes can. A lane that PRE-EMPTS never has to win a tiebreak to beat you, which
    is why the earlier "the fixed lanes are the ones that win the merge" reasoning could not
    see it. Pin the provenance reader it now uses."""
    from src.api.main import _scalar_provenance_of, _scalar_read_class_confidence
    assert _scalar_provenance_of(_conn_for([("llm_inferred",)]), "e1", "weight") == "llm_inferred"
    assert _scalar_provenance_of(_conn_for([]), "e1", "weight") is None
    # …and an unknown/absent provenance must NOT be reported as user truth.
    cls, conf = _scalar_read_class_confidence(None, "weight")
    assert not (cls == "A" and conf == 1.0)

    class _Boom:
        def cursor(self): raise RuntimeError("db down")
    assert _scalar_provenance_of(_Boom(), "e1", "weight") is None  # fail-safe


def test_rl4_helper_is_fail_safe():
    """Any resolver failure degrades to today's behaviour, never a broken recall."""
    with patch("src.api.main.assign_class_and_confidence",
               side_effect=RuntimeError("overlay down")):
        assert _scalar_read_class_confidence("user_stated", "height") == ("A", 1.0)


# ── RL1 (second lane) — the PRESENT-TENSE age question must not route to history ──────
#
# Found by the BLIND CRITIC of round 1, against a fix that had already been declared clean
# for the sibling lane. `main.py::_detect_temporal_query_intent`'s history alternation
# matched a bare `\bold\b`, so "How old is my X?" — present tense, no past-state language —
# routed into `fetch_superseded_facts` and served a RETRACTED value at Class A / confidence
# 1.0, rendered "...used to be <value> (until <date>)". Two of RL1's four required shapes
# (direct anchor, "how"-form) reach it. Reproduced against the real /query handler on the
# live pre-prod tenant at pre-campaign `491951d4`, clean after the fix, same rows.
#
# Fixed AT THE GATE, never by blinding the history lane: retire-and-retain (SS1) means a
# retracted value must stay reachable as a former value on a genuine past-state ask, and
# blinding the lane would trade this leak for the silent erasure SS1 exists to prevent.

def test_rl1_present_tense_old_is_never_a_history_ask():
    """`old` in a PRESENT/non-past clause must never route to the supersession lane.

    This list covers BOTH readings, because the campaign learned the hard way that they are
    one problem, not two. The degree question ("how old is X") was the first leak; the
    ATTRIBUTIVE one ("how heavy is my old X") was found by the FOURTH blind critic, through
    the real MCP door, after three rounds had each "fixed" this cue — because the premise
    "attributive `old` names a FORMER thing" is simply false. **Attributive `old` is
    ambiguous between FORMER and AGED**, and English does not resolve it from the modifier.
    Only the CLAUSE TENSE does, which is why the cue is now one tense test."""
    from src.api.main import _detect_temporal_query_intent as intent
    for q in (
            # degree question — the original leak class
            "How old is my speckled plovex?", "How old are my plovexes?", "How old am I?",
            "how old is it", "How old's my plovex?", "How old’re my plovexes?",
            "How old exactly is my plovex?", "just how old is my plovex",
            "And how old is the plovex?", "How old is my plovex, roughly?",
            "Do you know how old my plovex is?", "Tell me how old the plovex is.",
            # no copula to anchor on — do-support, modals, futures
            "How old will my plovex be?", "How old might my plovex be?",
            "How old can my plovex get?", "How old does my plovex look?",
            "How old do I look?", "How old has my plovex got?",
            "How old will the speckled plovex be next year?",
            # ATTRIBUTIVE `old` in a PRESENT clause — the AGED reading, not FORMER
            "How heavy is my old speckled plovex?",
            "How heavy is my old speckled plovex right now?",
            "how much does my old plovex weigh?",
            "what colour is my old car?", "how fast is my old computer?",
            # compound modifiers — `old` is not temporal at all
            "how old-fashioned is my plovex?", "how old fashioned is my plovex?",
            "how old school is my plovex?", "is my plovex old-fashioned?",
            "my old-fashioned car", "the old-fashioned way", "my old-school plovex",
            # a past verb in a DIFFERENT clause must not re-tense this one
            "How old is the plovex that was in the study?",
            "How old is my plovex? It was a gift.",
            "I was wondering how old my plovex is",
            "How old will my plovex be when it was bought?",
            "How old is it? Was it a gift?",
            # a PARTICIPLE is not the clause's tense
            "How old, compared to my old car, is my plovex?"):
        assert not intent(q).get("history"), (
            f"present-tense `old` routed to the supersession lane: {q!r}")


def test_rl1_known_limitation_parenthetical_misattachment():
    """DECLARED LIMITATION, pinned so it is a known state rather than a silent surprise.

    "How old, I was wondering, is my plovex?" still reads as history. The rule is right; the
    PARSE is wrong — spaCy attaches `old` to `wondering` (whose finite aux is the past `was`)
    instead of to the main-clause `is`, so the cue faithfully reports the tense of the clause
    it was handed. There is no structural discriminator that separates this from a GENUINE
    embedded past ask ("Do you know how old my car WAS?"), where the same shape — chain
    reaches an embedded finite past — is exactly the reading we want. Both are `old` under a
    finite past in the chain.

    So this is recorded rather than patched: inventing an epicycle to tell a mis-parse from a
    correct parse of the same shape is how the three earlier regex iterations of this cue
    were each falsified. If a parser upgrade fixes the attachment, this test goes red and
    should simply be inverted."""
    from src.api.main import _detect_temporal_query_intent as intent
    assert intent("How old, I was wondering, is my plovex?").get("history") is True


def test_rl1_past_tense_old_keeps_its_history_reading():
    """The reading the cue exists for survives — and it is TENSE that carries it, in both
    the attributive and the predicative frame."""
    from src.api.main import _detect_temporal_query_intent as intent
    for q in ("What was my old job?",
              "my old address was in Waterloo",
              "What was my old plovex called?",
              "How old was my car?",
              "How old were they?",
              "Do you know how old my car was?",
              # UNKNOWN-word degraded attachment: the chain never reaches `was`, so the
              # bounded sentence-tense fallback is what keeps this one.
              "Tell me how old the speckled plovex from Toronto was",
              "tell me how old my speckled plovex from toronto was",
              "I wonder how old that beaten-up red car was",
              # the unambiguous multiword cues are untouched
              "Where did I used to live?", "previously my address was X",
              "formerly known as X", "I no longer own it"):
        assert intent(q).get("history"), f"history reading lost for: {q!r}"


def test_rl1_tenseless_old_fragment_is_declared_ambiguous():
    """DECLARED LOSS, pinned so it is a decision and not a drift.

    A tenseless fragment ("my old address", "the old plovex") carries no tense to read and
    is genuinely ambiguous between FORMER and AGED. Earlier rounds PINNED the ambiguous
    reading as history — that pin was itself the defect the fourth critic exposed, because
    it routes present-tense possessions into a lane that serves RETRACTED values. A memory
    system fails toward not re-serving what the user deleted."""
    from src.api.main import _detect_temporal_query_intent as intent
    for q in ("my old address", "the old plovex", "my old plovex"):
        assert not intent(q).get("history"), (
            f"tenseless ambiguous fragment claimed as history: {q!r}")


def test_rl1_cue_fail_safe_is_inert_and_never_leaks():
    """FORCE the degraded path. Without a parse there is no tense to read and `old` is
    ambiguous, so it contributes NOTHING — that is the whole fallback, deliberately.

    Four successive regex approximations of the tense test were each falsified within
    minutes (three of them by running them myself); the last still leaked "what colour is my
    old car?" and "is my plovex old-fashioned?". The unambiguous multiword cues carry the
    past-state lane on their own while the parser is down."""
    from unittest.mock import patch as _patch
    import src.extraction.linguistics as _ling
    from src.api.main import _detect_temporal_query_intent as intent

    leaky = ("How old is my plovex?", "How old will my plovex be?",
             "How old does my plovex look?", "How old’s my plovex?",
             "How heavy is my old speckled plovex?", "what colour is my old car?",
             "is my plovex old-fashioned?", "how fast is my old computer?",
             "How old is the plovex that was in the study?",
             "How old is it? Was it a gift?")
    for label, ctx in (("unavailable", _patch.object(_ling, "linguistics_available",
                                                     return_value=False)),
                       ("parse raises", _patch.object(_ling, "_parse",
                                                      side_effect=RuntimeError("boom")))):
        with ctx:
            for q in leaky:
                assert not intent(q).get("history"), (
                    f"fail-safe ({label}) leaked into the supersession lane: {q!r}")
            # …and the unambiguous cues still work with no parser at all.
            for q in ("Where did I used to live?", "previously my address was X",
                      "formerly known as X", "I no longer own it"):
                assert intent(q).get("history"), (
                    f"fail-safe ({label}) lost an UNAMBIGUOUS cue: {q!r}")


def test_rl1_cue_fast_path_skips_the_parse():
    """A query that cannot contain the cue must not pay for a spaCy parse (the round-3
    critic measured 6.76 ms/call, and `_detect_temporal_query_intent` has five `/query` call
    sites against a 2.0 s turn budget).

    ⚠️ THE OBVIOUS VERSION OF THIS TEST WAS VACUOUS and a critic proved it by DELETING the
    guard and watching it still pass: patching `_parse` to raise is swallowed by
    `_old_is_history_cue`'s own `except Exception`, and the fallback then returns the same
    answer anyway. So assert on the CALL, not on the outcome — a mock that records whether
    the parser was reached at all."""
    from unittest.mock import patch as _p
    import src.extraction.linguistics as _l
    from src.api.main import _old_is_history_cue as cue

    calls = []
    real = _l._parse

    def _spy(text, *a, **k):
        calls.append(text)
        return real(text, *a, **k)

    with _p.object(_l, "_parse", _spy):
        assert cue("what is the weight of my plovex?") is False
        assert cue("") is False
        assert calls == [], f"parsed on the fast path: {calls!r}"
        # …and the guard does not swallow a query that DOES carry the cue.
        cue("what was my old job?")
        assert calls, "fast-path guard blocked a query that contains the cue"


# ── RL5 — the claim is made true (the comment/log that asserted the invariant) ────────

def _main_py_source():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "src", "api", "main.py"), encoding="utf-8") as fh:
        return fh.read()


def test_rl5_false_invariant_claim_no_longer_stands_as_an_assertion():
    """The retire lane justified itself with '(recall stops serving it — every attribute
    read filters superseded_at IS NULL)'. That was FALSE when written and was the stated
    reason the DELETE became a retirement, which is what made it load-bearing.

    The pin is on the ASSERTION, not the words: the parenthetical is gone from the
    justification, while the sentence survives QUOTED inside a ⚠️ CORRECTED block —
    deliberately, per CLAUDE.md's rule that a correction keeps one line of what it used to
    say, because that line is the only thing stopping the error recurring. Asserting the
    words never appear would forbid recording the lesson."""
    src = _main_py_source()
    assert "(recall stops serving it — every attribute read" not in src, (
        "the false invariant still stands as the retire lane's own justification")
    assert "⚠️ CORRECTED (RL5, scalar-retract-recall-leak)" in src, (
        "the correction block that records what the comment used to claim is missing")
    # And the accurate replacement must name the qualifier that makes it checkable.
    assert "every CURRENT-state recall read that EMITS A VALUE filters" in src


def test_rl5_log_note_no_longer_overclaims():
    """The emitted log line told the same untruth to anyone reading production logs."""
    src = _main_py_source()
    assert "entity_attributes_history; recall stops serving it" not in src
    assert "current-state recall stops asserting it" in src

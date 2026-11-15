"""SCALAR SUPERSESSION (gauntlet scalar-supersession, bars SS1-SS5) — store-half pins.

Mock-based, same conventions as tests/fact_store/test_replay_guard.py: SQL text and
positional params are asserted directly. The row-level truth (a correction RETAINS the
prior value in entity_attributes_history, exactly one live row per (entity_id, attribute),
same-entity siblings carrying the old value retired in both stores, a MARKED replay of a
retired value frozen, a genuine fresh re-statement still landing) is exercised end-to-end
on pre-prod through the real MCP door by the gauntlet battery
(DEV/review/gauntlet/scalar-supersession/).

Helpers under test live in src/api/main.py (search the SS block after _pin_rel_type_to_known):
_record_scalar_supersession, _retire_scalar_value_siblings, _scalar_retired_value_texts,
_scalar_slot_replay_frozen, _address_scalar_target, _scalar_authority_rank,
_scalar_value_texts.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.api.main import (  # noqa: E402
    _address_scalar_target,
    _record_scalar_supersession,
    _retire_scalar_value_siblings,
    _scalar_authority_rank,
    _scalar_canon_provenance,
    _scalar_gate_hold,
    _scalar_gate_stored_rank,
    _scalar_retired_value_texts,
    _scalar_slot_replay_frozen,
    _scalar_value_texts,
)


# ── SS5 — authority ladder ────────────────────────────────────────────────────────────────

def test_authority_user_trumps_inference():
    assert _scalar_authority_rank("user_stated") > _scalar_authority_rank("llm_inferred")
    assert _scalar_authority_rank("user_stated") > _scalar_authority_rank("llm_learned")
    assert _scalar_authority_rank("llm_learned") > _scalar_authority_rank("llm_inferred")


def test_authority_correction_lane_tokens_ranked():
    """A user-routed correction ranks WITH user truth; the recall auto-divert (a machine
    side-effect) ranks with engine inference — it can never hold against a human statement."""
    assert _scalar_authority_rank("user_corrected") == _scalar_authority_rank("user_stated")
    assert _scalar_authority_rank("unattested_correction") == _scalar_authority_rank("llm_inferred")
    assert _scalar_authority_rank("user_corrected") > _scalar_authority_rank("unattested_correction")


def test_authority_unknown_is_lowest():
    assert _scalar_authority_rank(None) == 0
    assert _scalar_authority_rank("garbage") == 0
    assert _scalar_authority_rank("USER_STATED ") == 3  # case/whitespace normalized


# ── SS1/SS3 substrate — deterministic value representations ───────────────────────────────

def test_value_texts_covers_every_storage_column():
    out = _scalar_value_texts("eleven", None, None, None)
    assert out == ["eleven"]
    out = _scalar_value_texts(None, 2019, None, None)
    assert out == ["2019"]
    out = _scalar_value_texts(None, None, 3.5, None)
    assert "3.5" in out
    out = _scalar_value_texts(None, None, 12.0, None)
    assert "12.0" in out and "12" in out  # integral float carries its int form
    out = _scalar_value_texts(None, None, None, __import__("datetime").date(2019, 5, 1))
    assert "2019-05-01" in out


def test_value_texts_dedupes():
    assert _scalar_value_texts("twelve", None, None, None) == ["twelve"]
    assert len(_scalar_value_texts("7", 7, 7.0, None)) == 2  # '7' text + int/float collapse


# ── SS1 — history append (SQL/params pins) ───────────────────────────────────────────────

def _cur_of(db):
    return db.cursor.return_value.__enter__.return_value


def test_record_supersession_inserts_audit_row():
    db = MagicMock()
    prior = {"value_text": "eleven", "value_int": None, "value_float": None,
             "value_date": None, "provenance": "user_stated", "datatype": None,
             "created_at": "2026-09-03T17:13:27"}
    new = {"value_text": "twelve", "value_int": None, "value_float": None,
           "value_date": None, "provenance": "user_stated"}
    ok = _record_scalar_supersession(
        db, "u1", "e1", "member_of", prior, new, "superseded", "ingest_restate")
    assert ok is True
    sql = " ".join(_cur_of(db).execute.call_args[0][0].split())
    params = _cur_of(db).execute.call_args[0][1]
    assert "INSERT INTO entity_attributes_history" in sql
    assert "prior_value_text" in sql and "new_value_text" in sql
    assert "prior_provenance" in sql and "new_provenance" in sql
    # prior snapshot, new snapshot, action, cause — the recoverable-history contract
    assert params[3] == "eleven" and params[10] == "twelve"
    assert params[7] == "user_stated"
    assert params[-2] == "superseded" and params[-1] == "ingest_restate"


def test_record_supersession_refuses_unverifiable_inputs():
    db = MagicMock()
    # no prior → nothing to retain → refused (never write a hollow audit row)
    assert _record_scalar_supersession(db, "u1", "e1", "a", None, {}, "superseded", "x") is False
    # unknown action → refused (the CHECK constraint would reject it anyway; fail before SQL)
    assert _record_scalar_supersession(
        db, "u1", "e1", "a", {"value_text": "v"}, {}, "annihilated", "x") is False
    _cur_of(db).execute.assert_not_called()


def test_record_supersession_never_sinks_parent_write():
    db = MagicMock()
    db.cursor.side_effect = RuntimeError("history table missing (pre-migration tenant)")
    ok = _record_scalar_supersession(
        db, "u1", "e1", "a", {"value_text": "v"}, None, "retired", "retract_lane")
    assert ok is False  # logged, returned — the supersede itself proceeds


# ── SS3 — same-entity sibling retirement (facts + staged, text and value-entity objects) ──

def test_retire_siblings_both_stores_value_scoped():
    db = MagicMock()
    _cur_of(db).rowcount = 0  # deterministic: nothing pre-exists in this mock store
    n = _retire_scalar_value_siblings(db, "e1", "member_of", ["eleven"])
    assert n == 0
    calls = _cur_of(db).execute.call_args_list
    facts_sql = " ".join(calls[0][0][0].split())
    staged_sql = " ".join(calls[1][0][0].split())
    # facts: soft archive, byte-identical to _soft_supersede_conflicting_fact's mechanism
    assert "UPDATE facts SET superseded_at = now(), qdrant_synced = false" in facts_sql
    assert "WHERE subject_id = %s AND rel_type = %s AND superseded_at IS NULL" in facts_sql
    # value-scoped: object matches a candidate text OR the minted value-entity's alias
    assert "object_id = ANY(%s)" in facts_sql
    assert "SELECT entity_id FROM entity_aliases WHERE lower(alias) = ANY(%s)" in facts_sql
    # staged: the Phase-4 tombstone every staged reader filters
    assert "UPDATE staged_facts SET deleted_at = now()" in staged_sql
    assert "deleted_at IS NULL" in staged_sql
    assert "object_id = ANY(%s)" in staged_sql
    for c in calls:
        assert c[0][1] == ("e1", "member_of", ["eleven"], ["eleven"])


def test_retire_siblings_no_candidates_is_noop():
    db = MagicMock()
    assert _retire_scalar_value_siblings(db, "e1", "member_of", []) == 0
    _cur_of(db).execute.assert_not_called()


# ── SS3 — replay parity (only ALREADY-RETIRED values freeze) ──────────────────────────────

def _db_scripting(rows_by_query):
    """MagicMock db whose cursor answers fetchall/fetchone from a list keyed by the
    first SQL token pair, consumed in order per key."""
    db = MagicMock()
    cur = db.cursor.return_value.__enter__.return_value
    state = {"q": [], "rows": list(rows_by_query.get("rows", []))}
    def _execute(sql, params=None):
        state["q"].append(" ".join(sql.split()))
    cur.execute.side_effect = _execute
    _it = iter(state["rows"])
    cur.fetchall.side_effect = lambda: next(_it, [])
    cur.fetchone.side_effect = lambda: None
    return db


def test_retired_value_texts_reads_history_and_retired_live_row():
    db = _db_scripting(rows_by_query={"rows": [[("eleven", None, None, None)],
                                              [("twelve", None, None, None)]]})
    out = _scalar_retired_value_texts(db, "e1", "member_of")
    assert "eleven" in out and "twelve" in out  # history prior + retired live row


def test_replay_frozen_on_retired_text_value():
    # history holds the superseded prior 'eleven'; a marked replay carrying 'eleven' freezes
    db = _db_scripting(rows_by_query={"rows": [[("eleven", None, None, None)], []]})
    assert _scalar_slot_replay_frozen(db, "e1", "member_of", "eleven") is True


def test_replay_not_frozen_when_no_history():
    # a first write / a value never retired at this slot: replay proceeds like today
    db = _db_scripting(rows_by_query={"rows": [[], []]})
    assert _scalar_slot_replay_frozen(db, "e1", "member_of", "eleven") is False


def test_replay_frozen_via_minted_value_entity_alias():
    # the explicit-edge path resolves scalar values to entities: the replay's object is the
    # value-entity UUID; its own alias ('eleven') identifies the retired value
    db = _db_scripting(rows_by_query={"rows": [
        [("eleven", None, None, None)],   # history priors
        [],                               # live row: not retired (empty result)
        [("eleven",)],                    # aliases of the object UUID
    ]})
    assert _scalar_slot_replay_frozen(db, "e1", "member_of", "f0a809ac-2686") is True


def test_replay_frozen_on_retired_live_row_value():
    # live row itself retired (negation/retraction stamp) — its value is a retired value
    db = _db_scripting(rows_by_query={"rows": [
        [],                               # no history rows
        [("twelve", None, None, None)],   # the retired live row's value
    ]})
    assert _scalar_slot_replay_frozen(db, "e1", "member_of", "twelve") is True


# ── SS4 — deterministic row-addressing for the retract lane ──────────────────────────────

def _addr_db(script):
    """cur.execute collects SQL; fetchone/fetchall pop from the script in call order.
    Each script entry: ('one'|'all', result)."""
    db = MagicMock()
    cur = db.cursor.return_value.__enter__.return_value
    state = {"script": list(script), "calls": []}
    cur.execute.side_effect = lambda sql, params=None: state["calls"].append(" ".join(sql.split()))
    _one, _all = [], []
    for kind, res in script:
        (_one if kind == "one" else _all).append(res)
    _oi, _ai = iter(_one), iter(_all)
    cur.fetchone.side_effect = lambda: next(_oi, None)
    cur.fetchall.side_effect = lambda: next(_ai, [])
    return cur, state


def test_address_rung1_exact_on_subject():
    # subject holds the exact attribute → unchanged, via='exact'
    cur, st = _addr_db([("one", (1,))])
    subj, attr, via = _address_scalar_target(cur, "e1", "member_of", None, "u1")
    assert (subj, attr, via) == ("e1", "member_of", "exact")


def test_address_rung3_unique_holder_retargets():
    # subject holds NOTHING under any pin; exactly one other entity holds the attribute —
    # the tenant-wide unique-holder rung (the live wound: extractor said (wrong-twin,
    # 'member_count'), the row is (holder, 'member_of'); rung 2.5 re-pins the name via the
    # shared head token 'member', rung 3 finds the holder)
    cur, st = _addr_db([
        ("one", None),   # rung 1 exact probe on subject: miss
        ("all", []),     # rung 2 (_pin_scalar_attribute_to_known) subject attrs: none known
        ("all", [("member_of",)]),  # rung 2.5 tenant names: single shared-head candidate
        ("all", [("24c2ea53", "eleven")]),  # rung 3 holder scan: exactly one, value unchecked
    ])
    subj, attr, via = _address_scalar_target(cur, "6719a70e", "member_count", None, "u1")
    assert (subj, attr, via) == ("24c2ea53", "member_of", "unique_holder")


def test_address_rung25_declines_on_two_shared_head_names():
    # two stored names share the extractor's head token — genuine ambiguity: no re-pin,
    # and the un-re-pinned guess has no holders anywhere
    cur, st = _addr_db([
        ("one", None),
        ("all", []),
        ("all", [("member_of",), ("member_count",)]),
        ("all", []),   # rung 3 scans the UNPINNED guess: no holders
    ])
    subj, attr, via = _address_scalar_target(cur, "6719a70e", "member_of_x", None, "u1")
    assert via is None


def test_address_rung3_declines_on_ambiguity():
    # TWO entities hold the attribute — genuine ambiguity: no retarget, caller clarifies
    cur, st = _addr_db([
        ("one", None),
        ("all", []), ("all", [("member_of",)]),
        ("all", [("e1", "v1"), ("e2", "v2")]),
    ])
    subj, attr, via = _address_scalar_target(cur, "6719a70e", "member_of", None, "u1")
    assert (subj, attr, via) == ("6719a70e", "member_of", None)


def test_address_rung3_value_check_when_old_value_extracted():
    # old_value extracted: the unique holder must HOLD THAT VALUE or the rung declines
    # (a correction must never retire a fact the user did not name)
    cur, st = _addr_db([
        ("one", None),
        ("all", []), ("all", [("member_of",)]),
        ("all", [("24c2ea53", "eleven")]),
    ])
    subj, attr, via = _address_scalar_target(cur, "6719a70e", "member_of", "twelve", "u1")
    assert via is None  # holder's value 'eleven' != named old value 'twelve'



# ── AR — stored-rank canonicalisation in the SS5 gate (gauntlet scalar-authority-   ──────
#    rank-legacy, bars AR1-AR5, 2026-09-04). The gate ranks the EXISTING row through
#    _scalar_gate_stored_rank: the RAW stored provenance canonicalised via the ONE
#    router mirror (_SCALAR_READ_PROVENANCE_CANON) before _scalar_authority_rank.
#    Wound being pinned: legacy 'mcp'/'assistant' rows (234 live) ranked RAW at 0,
#    below llm_inferred(1), so an incoming inferred value change retired the user's
#    own chat-lane truth. Revert-red: reverting the canonicalisation (gate back to a
#    raw _scalar_authority_rank on the stored column, or removing the helpers) makes
#    test_ar1_* RED — ImportError on these imports or a 0-rank assertion failure.

def test_ar1_legacy_chat_tokens_rank_as_user_truth():
    """AR1: a stored 'mcp'/'assistant' row's effective gate rank is 3 (user_stated),
    so the gate's hold condition — incoming(llm_inferred=1) < existing — is TRUE and
    the inferred value change is held, never a retirement of the stored value."""
    for legacy in ("mcp", "assistant"):
        assert _scalar_gate_stored_rank(legacy) == 3
        assert _scalar_authority_rank("llm_inferred") < _scalar_gate_stored_rank(legacy)
        # the wound, pinned: the RAW rank was 0 — the canonicalisation is the whole fix
        assert _scalar_authority_rank(legacy) == 0
        assert _scalar_authority_rank("llm_inferred") > _scalar_authority_rank(legacy)


def test_ar1_legacy_hold_symmetric_across_chat_lanes():
    """AR1 repeat-with-'assistant' folded in as a first-class case, not an afterthought."""
    assert _scalar_gate_stored_rank("assistant") == _scalar_gate_stored_rank("mcp")
    assert _scalar_gate_stored_rank("ASSISTANT ") == _scalar_gate_stored_rank("mcp")  # normalised


def test_ar2_correct_authority_still_supersedes():
    """AR2: user_stated / user_corrected incoming (3) still passes the gate against a
    legacy 'mcp'-stored row (3) — hold fires only when incoming < existing — and the
    authority ORDER itself is unchanged: this is a rank-SOURCE fix, not a rank-table
    change (the ladder pins above stay the definition)."""
    for incoming in ("user_stated", "user_corrected"):
        assert not (_scalar_authority_rank(incoming) < _scalar_gate_stored_rank("mcp"))
    # growth tiers against legacy chat truth: llm_learned(2) < mcp(3) -> held;
    # llm_inferred(1) < mcp(3) -> held
    assert _scalar_authority_rank("llm_learned") < _scalar_gate_stored_rank("mcp")
    assert _scalar_authority_rank("llm_inferred") < _scalar_gate_stored_rank("mcp")
    # and no ordering changed for canonical stored tokens: gate rank == raw rank there
    for canon in ("user_stated", "user_corrected", "llm_learned", "llm_inferred",
                  "unattested_correction"):
        assert _scalar_gate_stored_rank(canon) == _scalar_authority_rank(canon)


def test_ar3_one_mapping_one_source():
    """AR3: the gate canonicalises through the SAME map the read lanes use — a stored
    'mcp' row and a stored 'user_stated' row rank identically through the gate, and the
    gate's canonicalisation is byte-identical to _scalar_canon_provenance (the read
    lanes' resolver). No second, divergent mapping exists."""
    for incoming in ("user_stated", "user_corrected", "llm_learned", "llm_inferred",
                     "unattested_correction", "garbage", None):
        # same stored rank -> same gate decision for EVERY incoming value
        assert (_scalar_authority_rank(incoming or "") <
                _scalar_gate_stored_rank("mcp")) == \
               (_scalar_authority_rank(incoming or "") <
                _scalar_gate_stored_rank("user_stated"))
    assert _scalar_canon_provenance("mcp") == _scalar_canon_provenance("assistant") \
        == _scalar_canon_provenance("user_corrected") == "user_stated"
    assert _scalar_canon_provenance("llm_learn") == "llm_learned"


def test_ar3_no_forked_vocabulary_in_the_rank_table():
    """AR3 footgun pin: 'mcp'/'assistant' are NOT literal ranks in
    _SCALAR_PROVENANCE_AUTHORITY — the gate reaches them only through the router
    mirror. If someone 'fixes' it by forking the vocabulary, this goes red."""
    from src.api.main import _SCALAR_PROVENANCE_AUTHORITY
    assert "mcp" not in _SCALAR_PROVENANCE_AUTHORITY
    assert "assistant" not in _SCALAR_PROVENANCE_AUTHORITY



# ── AR non-vacuity — the GATE's own decision + wiring (revert-red contract) ──────
# test_ar1_* pin the helpers; these pin the gate BEHAVIOUR (the decision function
# the ingest site calls) and the WIRING (that the site actually calls it). Reverting
# the canonicalisation inside _scalar_gate_stored_rank/_scalar_gate_hold turns
# test_gate_hold_* RED; reverting the call site back to a raw comparison turns
# test_gate_wiring_uses_canonicalised_rank RED.

def test_gate_hold_legacy_chat_truth_holds_inference():
    """AR1 behavioural: the GATE holds an llm_inferred value change against a
    stored 'mcp'/'assistant' row (pre-fix this returned False -> retirement)."""
    for legacy in ("mcp", "assistant"):
        assert _scalar_gate_hold(legacy, "llm_inferred") is True
        assert _scalar_gate_hold(legacy, "llm_learned") is True  # growth < user truth


def test_gate_hold_correct_authority_passes():
    """AR2 behavioural: user_stated/user_corrected incoming passes against legacy;
    canonical-stored behaviour is byte-identical to the pre-fix gate."""
    for incoming in ("user_stated", "user_corrected"):
        assert _scalar_gate_hold("mcp", incoming) is False
    assert _scalar_gate_hold("user_stated", "llm_inferred") is True
    assert _scalar_gate_hold("llm_inferred", "user_stated") is False
    assert _scalar_gate_hold("llm_inferred", "llm_inferred") is False


def test_gate_wiring_uses_canonicalised_rank():
    """Source-shape pin: the ingest SS5 site ranks the EXISTING row through
    _scalar_gate_stored_rank and decides through _scalar_gate_hold — not a raw
    _scalar_authority_rank(prior_row) comparison (the wound). If a revert puts
    the raw comparison back at the call site, this goes RED."""
    import src.api.main as _m
    with open(_m.__file__, encoding="utf-8") as _f:
        src_text = _f.read()
    assert "_existing_rank = _scalar_gate_stored_rank(" in src_text
    assert "if _scalar_gate_hold(" in src_text
    # the wound shape is gone: the gate never ranks the stored prior row raw
    assert "_existing_rank = _scalar_authority_rank(" not in src_text

def test_ar4_unknown_tokens_fail_toward_inference_not_user_truth():
    """AR4-adjacent + router fail-direction: an unmapped stored token canonicalises to
    llm_inferred(1) — never user truth — and that changes NO hold outcome (a hold fires
    only when incoming < existing; the incoming side is canonical by construction and
    nothing canonical ranks below 1). The stored rows themselves are NOT rewritten."""
    assert _scalar_gate_stored_rank("reextract") == _scalar_authority_rank("llm_inferred")
    assert _scalar_gate_stored_rank("garbage") == _scalar_authority_rank("llm_inferred")
    assert _scalar_gate_stored_rank(None) == _scalar_authority_rank("llm_inferred")
    # ...and it can NEVER out-rank the user's own words
    assert _scalar_gate_stored_rank("garbage") < _scalar_gate_stored_rank("user_stated")


"""REEXTRACT-REPLAY-RESURRECTION (gauntlet reextract-replay-resurrection, bars RR1-RR6).

Unit/store-half pins, same conventions as tests/test_scalar_supersession.py:
  - TRANSPORT (RR1 half): the re_embedder episodic re-mine POST to /ingest carries the
    X-FL-Deferred-Replay marker + the X-FL-Replay-Lane hint - the reextract lane is a
    THIRD replaying writer and now lives inside the replay-transport contract. Pinned by
    driving reextract_episodic end-to-end against a scripted db + mocked httpx.
  - RR3: the retired-text probe is unconditional of _replay (marked -> hold with lane
    attribution; unmarked -> LOUD adjudication log, write proceeds - user-is-truth), and
    the upsert retirement memory does not depend on the sender marker or on any
    caller-side probe: the SQL CASE keeps a retired row retired on a same-value re-send
    (RR4) and on a prior-probe FAILURE (fail-safe direction: a lookup failure NEVER
    un-retires). Pinned by source/SQL assertions (the block is inline in main.py::ingest,
    ~57k lines - the suite established convention for inline SQL) plus
    ingest_transport parse pins.

The row-level live truth (retired value stays retired across a real reextract replay;
genuine fresh re-statement still resurrects) is exercised on pre-prod through the real
MCP door by the gauntlet battery (the internal design record).
"""

import os
import sys

quote = chr(34)  # string-literal delimiter stripped for flattened-source SQL pins
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.api import ingest_transport  # noqa: E402


# -- Transport parse pins --------------------------------------------------------------

def test_replay_marker_parse_unchanged():
    assert ingest_transport.replay_from_raw("1") is True
    assert ingest_transport.replay_from_raw("TRUE") is True
    assert ingest_transport.replay_from_raw(None) is False
    assert ingest_transport.replay_from_raw("") is False
    assert ingest_transport.replay_from_raw("garbage") is False  # fail-safe: not a replay


def test_replay_lane_hint_parse():
    assert ingest_transport.lane_from_raw("reextract") == "reextract"
    assert ingest_transport.lane_from_raw(" ReExtract ") == "reextract"
    assert ingest_transport.lane_from_raw(None) == ""      # unattributed
    assert ingest_transport.lane_from_raw("anything-else") == "anything-else"  # info-only


def test_lane_hint_constants_exist():
    # the marker is the decision; the lane hint only NAMES the sender
    assert ingest_transport.REPLAY_LANE_HEADER != ingest_transport.REPLAY_MARKER_HEADER
    assert ingest_transport.REPLAY_LANE_REEXTRACT == "reextract"


# -- RR1 (transport half): the reextract /ingest POST is a marked replay ----------------

def _scripted_reextract_db(row):
    db = MagicMock()
    cur = db.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [row]
    cur.fetchone.return_value = None
    cur.rowcount = 1
    return db


def _drive_reextract(monkeypatch, emb, row):
    posts = []

    class _Resp:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        posts.append((url, dict(headers or {}), json))
        if url.endswith("/extract/rewrite"):
            return _Resp({"status": "success",
                          "edges": [{"subject": "farxen", "rel_type": "weight",
                                     "object": "61 pounds"}]})
        if url.endswith("/ingest"):
            return _Resp({"status": "valid"})
        return _Resp({})

    monkeypatch.setattr(emb.httpx, "post", fake_post)
    monkeypatch.setattr(emb, "release_read_transaction", lambda *a, **k: None)
    processed = emb.reextract_episodic(
        _scripted_reextract_db(row), "http://backend", "u" * 32, schema_name=None,
        batch_size=5, statement_route="rewrite")
    return processed, posts


def test_reextract_ingest_post_carries_replay_marker(monkeypatch):
    """The wound exact lane: reextract_episodic -> httpx.post(/ingest) with ONLY the
    llm-lane header. The POST must now carry the replay marker (header-only - the
    idempotency key hashes edges and stays byte-identical) plus the lane hint."""
    import src.re_embedder.embedder as emb
    row = ("r1", "My wobbly farxen weighs 61 pounds.", None, None,
           datetime(2026, 9, 4, 21, 6, tzinfo=timezone.utc), "mcp")
    processed, posts = _drive_reextract(monkeypatch, emb, row)
    assert processed == 1
    ingest_posts = [p for p in posts if p[0].endswith("/ingest")]
    assert len(ingest_posts) == 1
    _url, headers, _body = ingest_posts[0]
    assert (headers.get(ingest_transport.REPLAY_MARKER_HEADER)
            == ingest_transport.REPLAY_MARKER_VALUE)
    assert (headers.get(ingest_transport.REPLAY_LANE_HEADER)
            == ingest_transport.REPLAY_LANE_REEXTRACT)


def test_reextract_marker_rides_header_never_body(monkeypatch):
    """Marker contract: HEADER-only. The /ingest body must not grow any replay key -
    the idempotency key hashes edges and byte-identity between attempts is the
    double-write guard."""
    import src.re_embedder.embedder as emb
    row = ("r2", "My wobbly farxen weighs 61 pounds.", None, None,
           datetime(2026, 9, 4, 21, 6, tzinfo=timezone.utc), "mcp")
    _processed, posts = _drive_reextract(monkeypatch, emb, row)
    _url, _headers, body = [p for p in posts if p[0].endswith("/ingest")][0]
    assert set(body.keys()) == {"text", "user_id", "edges", "source", "source_ref"}
    assert body["source"] == "mcp"  # preserved-origin lane keeps the ORIGINAL live source


# -- RR3/RR4: the upsert own retirement memory (inline-SQL pins, suite convention) --------

_MAIN_SRC = os.path.join(os.path.dirname(__file__), "..", "src", "api", "main.py")


def _scalar_upsert_sql():
    # normalize the WHOLE source (string literals are split across lines in main.py,
    # so raw substring search misses); then slice insert -> conflict -> updated_at.
    flat = " ".join(open(_MAIN_SRC).read().replace(quote, " ").split())
    i = flat.index("INSERT INTO entity_attributes (user_id, entity_id, attribute,"
                   " value_text, value_int,")
    j = flat.index("ON CONFLICT (entity_id, attribute)", i)
    k = flat.index("updated_at = now()", j)
    return flat[i:k]


def test_upsert_superseded_at_is_no_longer_unconditionally_null():
    sql = _scalar_upsert_sql()
    assert "superseded_at = CASE" in sql
    assert "superseded_at = NULL," not in sql  # the wound: unconditional un-retire


def test_upsert_rr4_same_value_keeps_retirement_stamp():
    sql = _scalar_upsert_sql()
    assert "EXCLUDED.value_text IS NOT DISTINCT FROM entity_attributes.value_text" in sql
    assert "EXCLUDED.value_int IS NOT DISTINCT FROM" in sql
    assert "EXCLUDED.value_float IS NOT DISTINCT FROM" in sql
    assert "EXCLUDED.value_date IS NOT DISTINCT FROM" in sql
    # same-value, UNMARKED (not a replay) -> un-retires (RR2: genuine re-statement)
    assert "AND NOT %s THEN NULL" in sql
    # same-value, MARKED replay -> keeps the stamp (RR4)
    assert "THEN entity_attributes.superseded_at" in sql
    src = open(_MAIN_SRC).read()
    assert "bool(_replay), bool(_prior_probe_ok))" in src  # both params threaded, in order


def test_upsert_rr3_failsafe_probe_flag_param():
    sql = _scalar_upsert_sql()
    assert "WHEN %s THEN NULL" in sql          # probe OK -> genuine re-statement un-retires
    assert "ELSE entity_attributes.superseded_at END" in sql  # probe FAILED -> keep stamp
    src = open(_MAIN_SRC).read()
    assert "_prior_probe_ok = False" in src  # set on the probe-exception path only
    assert "bool(_prior_probe_ok)" in src    # threaded as the CASE %s


def test_upsert_live_slot_write_never_blocked():
    sql = _scalar_upsert_sql()
    # the CASE FIRST branch: an already-live row goes to NULL unconditionally - a
    # probe failure or replay marker can never delay or corrupt a live-slot write
    assert "WHEN entity_attributes.superseded_at IS NULL THEN NULL" in sql


def test_rr3_retired_text_probe_unconditional_of_replay():
    """The retired-text probe must run for UNMARKED writes too (marked -> hold + lane
    attribution; unmarked hit -> LOUD adjudication log, write proceeds). The hit must
    be computed BEFORE the marker branch, with both branches keyed on it."""
    src = open(_MAIN_SRC).read()
    j = src.index("_hit_retired_text")
    i = src.index("ingest.scalar_replay_frozen")
    assert j < i  # the hit is computed BEFORE the freeze log, not inside an if _replay
    assert "ingest.scalar_retired_value_replay_unmarked" in src  # the RR3 loud event
    k = src.index("ingest.scalar_retired_value_replay_unmarked")
    frag = src[j:k]
    assert "if _replay:" in frag  # the SAME hit drives hold (marked) and log (unmarked)


def test_rr4_samevalue_split_log_and_history_events():
    src = open(_MAIN_SRC).read()
    assert "ingest.scalar_replay_samevalue_held" in src  # greppable RR4 hold event
    assert "_same_value_retired = " in src
    # the held log fires only for a MARKED replay on a retired same-value slot
    assert "if _same_value_retired and _replay:" in src
    # the UNMARKED same-value path RESURRECTS but never silently: the honest
    # 'restated' history row is appended plus the RR3 adjudication warning
    assert "elif _same_value_retired:" in src
    k = src.index("elif _same_value_retired:")
    frag2 = src[k:k + 2000]
    assert 'action="restated", cause="ingest_restate"' in frag2
    assert "ingest.scalar_retired_value_replay_unmarked" in frag2

# -*- coding: utf-8 -*-
"""SERVE-GAP ROUND 4 — the retrieval-gap seams (LongMemEval pilot
compose_20261114T1645Z: 31 of 38 residual misses were gold-in-store-but-never-served).

THE CLASSIFICATION (evidence: the run's scored detail JSONLs + autopsy_split.json;
every one of the 31 miss golds is EPISODIC-BAND gold — in_epis true, in_facts/
in_attrs false, per pilot_20261114T0648Z/autopsy.json):

  S1 (4)  WALK-NON-EMPTY-SKIPS-BAND   q118b2229 q58bf7951 q3f1e9474 q01e8e2f
          — the walk served SOME structured row (the intent-blind attribute laundry
          list: "How long is my daily commute?" answered with occupations) and the
          episodic band only rides empty/abstention arms.
  S2 (5)  NEGATIVE-SPACE-EARLY-RETURN-SKIPS-BAND  qc5e8278d q6b168ec8 qec81a493
          q19b5f2b3 q545bd2b5 — the "Less certain…" negative-space serve returns
          before the band is computed.
  S3 (8)  BAND-RETURNED-EMPTY  q7527f7e2 qad7109d1 qdccbc061 q75499fd8 qcaf9ead2
          q86b68151 q577d4d32 q15745da0 — sel-word gate (min-5 drops 'dog'/'ikea'),
          <2-token floor, ≥2-overlap admission, LIMIT-5 recency fetch bias.
  S4 (14) BAND-WRONG-ROWS  the remaining 14 — the band rode 2 distractor rows
          (assistant advice turns beating the gold user turn on recency ties).

TWO flags, both DEFAULT OFF (OFF = byte-for-byte today's path — pinned here):
  EPISODIC_BAND_UNDER_SERVE (S1+S2), EPISODIC_BAND_QA_SHAPE (S3+S4).
The mini-eval (tools/longmemeval/serve_gap_minieval.py) measures the hit-rate delta.
"""
import uuid

import pytest

import src.mcp.server as S


# ── flag defaults + OFF byte-parity ─────────────────────────────────────────────

class TestFlagDefaults:
    def test_both_flags_default_off(self):
        assert S.EPISODIC_BAND_UNDER_SERVE is False
        assert S.EPISODIC_BAND_QA_SHAPE is False

    def test_off_sel_words_is_the_legacy_extraction(self):
        # legacy: min 5 chars + legacy stop — 'breed' passes, 'dog' does not
        assert S._band_sel_words("What breed is my dog?") == ["breed"]
        assert S._band_sel_words("How long did it take to move to the new apartment?") \
            == ["apartment"]

    def test_off_band_under_serve_is_identity_with_zero_db(self):
        # OFF must do ZERO work and return "" (byte-parity on every non-empty serve)
        assert S._band_under_serve("u", "any query", "any render") == ""

    def test_off_negative_space_serve_path_unchanged(self, monkeypatch):
        # OFF: the wrapper returns _negative_space_serve_once's text untouched
        monkeypatch.setattr(S, "RECALL_NEGATIVE_SPACE", True)
        monkeypatch.setattr(S, "_negative_space_serve_once",
                            lambda *a, **k: "Less certain: nothing here")
        assert S._negative_space_serve("u", "q") == "Less certain: nothing here"


# ── S3: QA-shape sel words ──────────────────────────────────────────────────────

class TestQaShapeSelWords:
    def test_min_three_admits_short_content_words(self, monkeypatch):
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
        # measured misses: 'dog' (75499fd8), 'bookshelf'-only (86b68151)
        assert S._band_sel_words("What breed is my dog?") == ["breed", "dog"]
        assert "bookshelf" in S._band_sel_words(
            "Where did I buy my new bookshelf from?")

    def test_qa_stop_drops_question_function_words(self, monkeypatch):
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
        sel = S._band_sel_words(
            "What time do I stop checking work emails and messages?")
        assert "time" not in sel and "what" not in sel
        # content words survive ('stop' is IN the gold turn: "I stop checking…")
        assert "stop" in sel and "emails" in sel


# ── S4: speaker rank signal in the RRF fusion ───────────────────────────────────

class TestSpeakerRankSignal:
    def _rows(self):
        # the measured 8550ddae shape: assistant BBQ advice turn vs the gold USER
        # turn, equal overlap (2), gold OLDER (planted earlier) — legacy fusion
        # loses the gold on the recency signal.
        import datetime as dt
        t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        return [
            {"text": "[assistant] I'm planning a backyard BBQ in June and looking "
                     "for refreshing summer cocktail recipes",
             "matches": 2, "recency_key": t0 + dt.timedelta(hours=2),
             "specificity": 1.0, "intent": "", "user_speaker": False},
            {"text": "[user] I tried a lavender gin fizz cocktail recipe last "
                     "weekend and loved it",
             "matches": 2, "recency_key": t0,
             "specificity": 1.0, "intent": "", "user_speaker": True},
        ]

    def test_user_turn_ranks_first_with_the_signal(self):
        out = S._rrf_fuse_rows(self._rows())
        assert out[0]["text"].startswith("[user]")

    def test_speaker_outranks_extra_overlap_is_a_pinned_tradeoff(self):
        # DELIBERATE, pinned: at the ×4 weight a USER row with FEWER matched sel
        # words can outrank an assistant row — for "what did I …" questions an
        # assistant turn matching more question words is usually advice ABOUT the
        # topic (the c8c3f81d wound: assistants discuss running-shoe brands at
        # length) while the gold is the user's own statement. Measured no-regression
        # on the mini-eval (serve_gap_minieval_results.json).
        import datetime as dt
        t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        rows = [
            {"text": "[assistant] advice about running shoes brands and models",
             "matches": 3, "recency_key": t0, "specificity": 1.0, "intent": "",
             "user_speaker": False},
            {"text": "[user] my running shoes are Nike",
             "matches": 2, "recency_key": t0, "specificity": 1.0, "intent": "",
             "user_speaker": True},
        ]
        assert S._rrf_fuse_rows(rows)[0]["text"].startswith("[user]")

    def test_rows_without_the_key_keep_legacy_order(self):
        # byte-parity: strip the key and the older assistant turn wins exactly as
        # today's 3-signal fusion orders it (recency rank 1)
        rows = [{k: v for k, v in r.items() if k != "user_speaker"}
                for r in self._rows()]
        out = S._rrf_fuse_rows(rows)
        assert out[0]["text"].startswith("[assistant]")


# ── S1+S2: the band under a NON-EMPTY serve ─────────────────────────────────────

@pytest.mark.usefixtures("requires_postgres")
class TestBandUnderServe:
    """The measured S1 wound, end to end on a throwaway schema: q118b2229's
    occupation laundry list vs the episodic-only commute gold."""

    def _plant(self, postgres_dsn):
        import psycopg2
        seat = str(uuid.uuid4())
        schema = "faultline_" + seat.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'''
                CREATE TABLE "{schema}".episodic_log (
                    id bigserial PRIMARY KEY,
                    raw_text text NOT NULL,
                    intent text,
                    created_at timestamptz DEFAULT now())''')
            cur.execute(f'''
                INSERT INTO "{schema}".episodic_log (raw_text, intent, created_at)
                VALUES ('[user] My daily commute to work is 45 minutes each way, '
                        'mostly highway.', '', '2026-01-02 09:00:00+00'),
                       ('[assistant] Have you considered podcasts for the drive? '
                        'Here are some recommendations.', '',
                        '2026-01-02 09:05:00+00')''')
        conn.close()
        return seat, schema

    def _drop(self, postgres_dsn, schema):
        import psycopg2
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()

    def test_new_aspect_band_rides_under_the_laundry_list(
            self, postgres_dsn, monkeypatch):
        import psycopg2  # noqa: F401 — presence check
        seat, schema = self._plant(postgres_dsn)
        monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
        try:
            laundry = ("User's occupation is Student with part-time job\n"
                       "User's occupation is software engineer\n"
                       "User work email cutoff time 19:00")
            monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
            monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
            out = S._band_under_serve_append(
                laundry, seat, "How long is my daily commute to work?")
            # the band rode, inside the answer plane, carrying the gold
            assert out.startswith(laundry)
            assert S._EPISODIC_BAND_LABEL in out
            assert "45 minutes each way" in out
        finally:
            self._drop(postgres_dsn, schema)

    def test_no_new_aspect_never_rides(self, postgres_dsn, monkeypatch):
        seat, schema = self._plant(postgres_dsn)
        monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
        try:
            # the serve ALREADY carries every sel aspect ('daily commute … work')
            # — a band echoing them adds nothing and must not ride
            serve = ("Your daily commute to work is on record, 45 minutes "
                     "each way.")
            monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
            monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
            assert S._band_under_serve(
                seat, "How long is my daily commute to work?", serve) == ""
        finally:
            self._drop(postgres_dsn, schema)

    def test_under_serve_inserts_before_the_ops_fence(self, postgres_dsn,
                                                      monkeypatch):
        seat, schema = self._plant(postgres_dsn)
        monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
        try:
            serve = ("User's occupation is Student\n\n"
                     "=== HOW YOU OPERATE ===\nAdvisory notes live below the fence.")
            monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
            monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
            out = S._band_under_serve_append(
                serve, seat, "How long is my daily commute to work?")
            assert out.index(S._EPISODIC_BAND_LABEL) \
                < out.index("=== HOW YOU OPERATE ===")
        finally:
            self._drop(postgres_dsn, schema)


# ── S3 admission + fetch depth on the real band ─────────────────────────────────

@pytest.mark.usefixtures("requires_postgres")
class TestQaShapeBand:
    def test_short_question_gold_serves_under_qa_and_misses_off(
            self, postgres_dsn, monkeypatch):
        import psycopg2
        seat = str(uuid.uuid4())
        schema = "faultline_" + seat.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'''
                CREATE TABLE "{schema}".episodic_log (
                    id bigserial PRIMARY KEY,
                    raw_text text NOT NULL,
                    intent text,
                    created_at timestamptz DEFAULT now())''')
            cur.execute(f'''
                INSERT INTO "{schema}".episodic_log (raw_text, intent, created_at)
                VALUES ('[user] My dog is a Golden Retriever named Biscuit.', '',
                        '2026-01-02 09:00:00+00'),
                       ('[assistant] Golden Retrievers need lots of exercise and '
                        'grooming.', '', '2026-01-02 09:10:00+00')''')
        conn.close()
        monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
        try:
            monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
            band = S._episodic_verbatim_band_once(seat, "What breed is my dog?")
            assert band.startswith(S._EPISODIC_BAND_LABEL)
            assert "Golden Retriever" in band
            # OFF: 'breed' alone < the 2-token floor → the measured empty
            monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", False)
            assert S._episodic_verbatim_band_once(seat, "What breed is my dog?") == ""
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()

    def test_probe_depth_12_beats_the_limit_5_recency_bias(
            self, postgres_dsn, monkeypatch):
        import psycopg2
        seat = str(uuid.uuid4())
        schema = "faultline_" + seat.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'''
                CREATE TABLE "{schema}".episodic_log (
                    id bigserial PRIMARY KEY,
                    raw_text text NOT NULL,
                    intent text,
                    created_at timestamptz DEFAULT now())''')
            # the gold turn is the OLDEST 'internet' row; 6 newer one-word
            # 'internet' turns push it past LIMIT 5 (the S4 fetch bias shape)
            rows = [("[user] My new internet plan gives me 500 Mbps.",
                     "2026-01-01 08:00:00+00")]
            for i in range(6):
                rows.append((f"[assistant] note {i} about internet providers "
                             f"and router placement",
                             f"2026-01-0{2 + i} 08:00:00+00"))
            for text, ts in rows:
                cur.execute(
                    f'INSERT INTO "{schema}".episodic_log '
                    "(raw_text, intent, created_at) VALUES (%s, '', %s)",
                    (text, ts))
        conn.close()
        monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
        try:
            monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
            band = S._episodic_verbatim_band_once(
                seat, "What speed is my new internet plan?")
            assert "500 Mbps" in band, band
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()


# ── source-level parity receipts (the item-2 discipline) ────────────────────────

class TestSourceParity:
    def test_off_probe_sql_is_the_legacy_literal(self):
        src = open("src/mcp/server.py").read()
        # ANCHOR RENAMED BY DESIGN (the compose2 band-leg fix): the RRF branch's
        # empty-gate line became `if not overlapped and _echo_row is None:` —
        # the split keeps the segment bounded to the RRF branch; assertions
        # byte-identical.
        seg = src.split("if RECALL_RRF_FUSION:")[1].split(
            "if not overlapped and _echo_row is None:")[0]
        assert "ORDER BY created_at DESC LIMIT 5" in seg  # the legacy literal stays
        assert "_probe_sql.replace(" in seg               # the QA depth is opt-in

    def test_qa_admission_is_flag_guarded(self):
        src = open("src/mcp/server.py").read()
        assert "_qa_short = (EPISODIC_BAND_QA_SHAPE" in src
        assert "if matches < 2 and not _rare and not _qa_short:" in src

    def test_speaker_key_only_set_under_qa(self):
        src = open("src/mcp/server.py").read()
        assert 'if EPISODIC_BAND_QA_SHAPE:\n                    _row["user_speaker"]' \
            in src


# ── the negative-space gate under serve (the abflags_20261114T2210Z conviction) ──
#
# THE WOUND (the on-image A/B — tools/longmemeval/runs/abflags_20261114T2210Z/REPORT.md):
# the five negative-space gates ran the structured serve only `if not _band and not
# _arch_band` — written when the episodic band fired only on the EMPTY arm. Once
# EPISODIC_BAND_UNDER_SERVE fired the band under non-empty serves too (band present
# 44/50 vs 19/50), those gates SUPPRESSED the negative-space serve that carried the
# gold and flipped the serve to "I don't have anything on that yet." + a distractor
# band — judged accuracy 16%→10%, 4 regressions, 0/13 conversions. THE FIX: one gate
# contract, `_negative_space_gate_open`, living under the EXISTING
# EPISODIC_BAND_UNDER_SERVE flag (no new flag): a non-empty band OPENS the gate
# (the structured serve runs; the band rides UNDER it via the lane's own
# new-aspect-gated ride helper `_band_under_serve_append`), an empty serve still
# gets band-replaces-empty, the archive band keeps its original arm under BOTH
# flags, and OFF is exactly the legacy rule (byte-parity).
#
# The A/B regression questions' payload shapes below are quoted from the run's
# artifacts (results_off.jsonl OFF serves / results_on.jsonl ON band bytes):
#   0862e8bf "What is the name of my cat?"          OFF = the cat negative-space serve
#                                                   (Luna leads) — ON flipped it.
#   1e043500 "…the playlist I created on Spotify?"  OFF = the playlist rows (Summer
#                                                   Vibes leads) — ON flipped it.
#   853b0a1d "How old was I when my grandma…"       the structured age row led BOTH
#                                                   arms; the band rode under it.
#   37d43f65 "How much RAM did I upgrade…"          the walk's non-empty render led
#                                                   BOTH arms; the band rode under.
import asyncio
import io
from contextlib import redirect_stderr

_GATE_UID = "7c2a51f0-3a44-4dbe-9a1f-8f0d3a2b61c9"
_PLAIN_EMPTY = "I don't have anything on that yet."

_NS_CAT = ("Here's what I remember about cat:\n"
           "- user — pet species: cat\n"
           "- Luna — instance of: cat")
_NS_PLAYLIST = ("Here's what I remember about playlist:\n"
                "- Focus Flow — instance of: playlist\n"
                "- Morning Boost — instance of: playlist\n"
                "- Summer Vibes — instance of: playlist")
_NS_AGE = "User age at Grandma necklace gift 18"
_NS_TOY = "You have a cat named Luna."

_BAND_CAT = ("=== FROM YOUR PAST TURNS (verbatim) ===\n"
             "- [assistant] There are many incredible museums and galleries around "
             "the world that specialize in contemporary sculpture.\n"
             "- [user] I'm interested in the Coursera - Design Thinking "
             "Specialization by University of Virginia.")
# + one row carrying a sel word ('name') the structured serve lacks → rides
_BAND_CAT_RIDE = _BAND_CAT + "\n- [user] I named her Luna when she was a kitten"
_BAND_PLAYLIST = ("=== FROM YOUR PAST TURNS (verbatim) ===\n"
                  "- [user] That's a great idea about using emojis or keywords in "
                  "playlist names.\n"
                  "- [user] I have 20 playlists on Spotify already, and I'm "
                  "looking to organize them better.")
_BAND_AGE = ("=== FROM YOUR PAST TURNS (verbatim) ===\n"
             "- [user] I've been really enjoying the silver necklace with a "
             "pendant that says \"sister\" in cursive my sister gave me 3 weeks ago.")
_BAND_RAM = ("=== FROM YOUR PAST TURNS (verbatim) ===\n"
             "- [user] I upgraded my laptop to 16GB RAM last weekend and it flies now.")
_ARCH_BAND = ("=== FROM MEMORY'S ARCHIVE (superseded values) ===\n"
              "- staging api's port used to be 8001")


class TestNegativeSpaceGateContract:
    """The helper's truth table = the whole fix in one place."""

    def test_off_is_exactly_the_legacy_rule(self, monkeypatch):
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", False)
        # legacy `not band and not arch_band`, byte-for-byte:
        assert S._negative_space_gate_open("", "") is True
        assert S._negative_space_gate_open("band", "") is False   # the old suppression
        assert S._negative_space_gate_open("", "arch") is False
        assert S._negative_space_gate_open("band", "arch") is False

    def test_on_opens_for_a_non_empty_band_when_the_archive_abstains(self, monkeypatch):
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        assert S._negative_space_gate_open("band", "") is True   # THE FIX: ride-under
        assert S._negative_space_gate_open("", "") is True       # the empty arm, unchanged

    def test_the_archive_band_keeps_its_original_arm_under_both_flags(self, monkeypatch):
        for on in (False, True):
            monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", on)
            assert S._negative_space_gate_open("band", "arch") is False
            assert S._negative_space_gate_open("", "arch") is False


class TestNegativeSpaceGateSites:
    """The five faces all condition on the one contract (no legacy site survives)."""

    def test_four_helper_sites_and_the_inline_fifth_face(self):
        src = open("src/mcp/server.py", encoding="utf-8").read()
        assert src.count("if _negative_space_gate_open(_band, _arch_band):") == 4
        assert "if not _band and not _arch_band:" not in src
        # the fifth face (operand-grounding) keeps the band read in its exact
        # short-circuit position and adds the same flag-scoped disjunct:
        lines = src.splitlines()
        idx = [i for i, ln in enumerate(lines)
               if ln.strip() == "and (not _episodic_verbatim_band(user_id, query)"]
        assert len(idx) == 1, idx
        nxt = lines[idx[0] + 1]
        assert nxt.strip() == "or EPISODIC_BAND_UNDER_SERVE)):", nxt
        _indent = len(lines[idx[0]]) - len(lines[idx[0]].lstrip())
        assert nxt == " " * (_indent + 5) + nxt.strip(), nxt  # aligned under `not`


class _Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _GateBackend:
    """Scripted backend (the test_mcp_no_silent_route_downgrade pattern): routes are
    answered from `script` (last entry repeats), unknown routes get {"status": "ok"}."""

    def __init__(self, script=None):
        self.script = dict(script or {})
        self.calls: list[str] = []

    def _step(self, url):
        key = next((k for k in self.script if k in url), None)
        self.calls.append(key or url)
        body = self.script.get(key, {"status": "ok"}) if key else {"status": "ok"}
        return _Resp(body)

    async def post(self, url, *a, **k):
        return self._step(url)

    async def get(self, url, *a, **k):
        return self._step(url)


def _gate_run(coro):
    buf = io.StringIO()
    with redirect_stderr(buf):
        out = asyncio.run(coro)
    return out, buf.getvalue()


async def _sleep_forever(*a, **k):
    await asyncio.sleep(3600)


def _passthrough(user_id, text, result):
    return result


@pytest.fixture
def gate_env(monkeypatch):
    """The recall tool driven in-process against the scripted backend (no DB, no LLM).
    The episodic band / negative-space rows / token bind / archive band are planted
    per-test by monkeypatching their seams — the pins exercise the REAL gate logic."""
    S._FAULTLINE_URL_DETECTED = True
    S._provisioned_users.add(_GATE_UID)
    if hasattr(S, "_brain_slow_reset"):
        S._brain_slow_reset("test isolation")
    S._statement_defer_queue = None
    S._statement_defer_task = None
    monkeypatch.setattr(S, "_statement_defer_worker", _sleep_forever)
    monkeypatch.setattr(S, "_with_experience", _passthrough)
    monkeypatch.setattr(S, "_send_progress", lambda *a, **k: None)
    if hasattr(S, "_EPISODIC_REAPPEND_BASE_S"):
        import tempfile
        monkeypatch.setattr(S, "_EPISODIC_REAPPEND_BASE_S", 0.0)
        monkeypatch.setattr(S, "_EPISODIC_REAPPEND_MAX_DELAY_S", 0.0)
        monkeypatch.setattr(S, "_EPISODIC_UNCONFIRMED_CRIT_S", 3600.0)
        monkeypatch.setattr(S, "_EPISODIC_SPOOL_DIR",
                            tempfile.mkdtemp(prefix="gate-spool-"))
        S._episodic_reappend_queue = None
        S._episodic_reappend_task = None
        S._episodic_reappend_inflight = None
    yield
    S._provisioned_users.discard(_GATE_UID)


def _plant(monkeypatch, *, band="", arch="", ns=None, tb=None):
    """Plant the four seams the gates consult. Returns the ns-call sentinel list."""
    ns_calls: list = []
    monkeypatch.setattr(S, "_episodic_verbatim_band", lambda uid, q: band)
    monkeypatch.setattr(S, "_archive_query_band", lambda uid, q: arch)
    def _ns_once(uid, q, anchor):
        ns_calls.append(q)
        return ns  # None = the structured serve holds nothing (honest abstention)
    monkeypatch.setattr(S, "_negative_space_serve_once", _ns_once)
    monkeypatch.setattr(S, "_token_bind_refusal_serve",
                        (lambda uid, q: tb) if tb is not None else (lambda uid, q: None))
    return ns_calls


_EMPTY_PAYLOAD = {"anchor": "user", "facts": [], "preferred_names": {}, "attributes": {}}


def _recall(monkeypatch, query, payload):
    monkeypatch.setattr(S, "_http_client",
                        _GateBackend({"/classify-intent": {"intent": "QUERY",
                                                           "confidence": 0.99},
                                      "/confidence-gate": {"threshold": 0.7},
                                      "/internal/ingest-route":
                                          {"statement_extractor": "spine"},
                                      "/query": payload}))
    out, _log = _gate_run(S.recall_memory_tool(query, _GATE_UID))
    return str(out.get("memory", ""))


class TestNegativeSpaceGateUnderServe:
    """The four A/B regression shapes + the two-arm contract, end-to-end through the
    recall tool with the genuinely-empty gate (site 2) as the face under test."""

    def test_0862e8bf_luna_serves_the_structured_serve_not_the_flip(
            self, gate_env, monkeypatch):
        # the A/B ON arm flipped this to the empty line + distractor band; the fix
        # opens the gate and the cat serve leads again (echo-only band dropped by
        # the new-aspect ride gate — QA ON was the A/B's arm)
        calls = _plant(monkeypatch, band=_BAND_CAT, ns=_NS_CAT)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
        memory = _recall(monkeypatch, "What is the name of my cat?", _EMPTY_PAYLOAD)
        assert memory == _NS_CAT, memory
        assert _PLAIN_EMPTY not in memory
        assert calls == ["What is the name of my cat?"], calls

    def test_0862e8bf_band_rides_under_when_it_adds_a_new_aspect(
            self, gate_env, monkeypatch):
        calls = _plant(monkeypatch, band=_BAND_CAT_RIDE, ns=_NS_CAT)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
        memory = _recall(monkeypatch, "What is the name of my cat?", _EMPTY_PAYLOAD)
        # 'name' (in 'named') is a sel word the structured serve lacks → rides under
        assert memory == _NS_CAT + "\n\n" + _BAND_CAT_RIDE, memory
        assert calls, "the structured serve must have been attempted"

    def test_0862e8bf_off_reproduces_the_legacy_flip_bytes(
            self, gate_env, monkeypatch):
        # OFF byte-parity: the legacy suppression stands and the band replaces the
        # empty — the exact shape the A/B measured ON (now provably flag-scoped)
        calls = _plant(monkeypatch, band=_BAND_CAT, ns=_NS_CAT)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", False)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", False)
        memory = _recall(monkeypatch, "What is the name of my cat?", _EMPTY_PAYLOAD)
        assert memory == _PLAIN_EMPTY + "\n\n" + _BAND_CAT, memory
        assert calls == [], "OFF must keep suppressing the structured serve"

    def test_1e043500_summer_vibes_serves_the_playlist_rows_with_band_under(
            self, gate_env, monkeypatch):
        calls = _plant(monkeypatch, band=_BAND_PLAYLIST, ns=_NS_PLAYLIST)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", False)
        memory = _recall(monkeypatch, "What is the name of the playlist I created "
                                      "on Spotify?", _EMPTY_PAYLOAD)
        # 'spotify' rides (absent from the structured rows); the gold leads
        assert memory == _NS_PLAYLIST + "\n\n" + _BAND_PLAYLIST, memory
        assert "Summer Vibes" in memory and _PLAIN_EMPTY not in memory
        assert calls, calls

    def test_853b0a1d_age_row_serves_with_band_under(
            self, gate_env, monkeypatch):
        calls = _plant(monkeypatch, band=_BAND_AGE, ns=_NS_AGE)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", False)
        memory = _recall(monkeypatch, "How old was I when my grandma gave me the "
                                      "silver necklace?", _EMPTY_PAYLOAD)
        # 'silver' is a sel word the age row lacks → the band rides under, exactly
        # the A/B ON payload's shape (minus the flip risk this face never had)
        assert memory == _NS_AGE + "\n\n" + _BAND_AGE, memory
        assert calls, calls

    def test_37d43f65_walk_render_leads_and_the_band_rides_under(
            self, gate_env, monkeypatch):
        # the walk NON-empty path (this face never hit the gates): the structured
        # render leads BOTH arms; the band rides under it ON (S1) and the OFF arm
        # is byte-identical-minus-band. No flip is possible on this path.
        payload = {"anchor": "user", "facts": [], "preferred_names": {},
                   "attributes": {"age": "18", "laptop_ram": "16GB"}}
        monkeypatch.setattr(S, "SERVE_RENDER_TOKEN_RANK", False)
        monkeypatch.setattr(S, "SERVE_BOUND_ANSWER_FRAMING", False)
        monkeypatch.setattr(S, "RECALL_SELF_VERIFY_SERVE", False)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", False)
        monkeypatch.setattr(S, "_token_bind_refusal_serve", lambda uid, q: None)
        monkeypatch.setattr(S, "_episodic_verbatim_band", lambda uid, q: _BAND_RAM)
        memory_on = _recall(monkeypatch, "How much RAM did I upgrade my laptop to?",
                            payload)
        assert memory_on == "age: 18\nlaptop_ram: 16GB" + "\n\n" + _BAND_RAM, memory_on
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", False)
        memory_off = _recall(monkeypatch, "How much RAM did I upgrade my laptop to?",
                             payload)
        assert memory_off == "age: 18\nlaptop_ram: 16GB", memory_off

    def test_empty_arm_unchanged_when_the_band_is_empty(self, gate_env, monkeypatch):
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
        monkeypatch.setattr(S, "_episodic_verbatim_band", lambda uid, q: "")
        monkeypatch.setattr(S, "_negative_space_serve_once",
                            lambda uid, q, anchor: _NS_CAT)
        on = _recall(monkeypatch, "What is the name of my cat?", _EMPTY_PAYLOAD)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", False)
        off = _recall(monkeypatch, "What is the name of my cat?", _EMPTY_PAYLOAD)
        assert on == off == _NS_CAT, (on, off)

    def test_band_replaces_empty_when_the_serve_abstains_even_on(
            self, gate_env, monkeypatch):
        calls = _plant(monkeypatch, band=_BAND_CAT, ns=None)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
        memory = _recall(monkeypatch, "What is the name of my cat?", _EMPTY_PAYLOAD)
        # the structured serve holds nothing → band-replaces-empty, as before;
        # the attempt itself RAN (the gate opened) and decided honestly
        assert memory == _PLAIN_EMPTY + "\n\n" + _BAND_CAT, memory
        assert calls, "the gate must still ATTEMPT the serve when it opens"

    def test_token_bind_hit_outranks_the_band(self, gate_env, monkeypatch):
        calls = _plant(monkeypatch, band=_BAND_CAT, ns=_NS_CAT,
                       tb="You named your cat: Luna")
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
        memory = _recall(monkeypatch, "What is the name of my cat?", _EMPTY_PAYLOAD)
        assert memory == "You named your cat: Luna", memory
        assert calls == [], "a bind hit answers; the negative-space serve never runs"

    def test_archive_band_still_suppresses_under_on(self, gate_env, monkeypatch):
        calls = _plant(monkeypatch, band=_BAND_CAT, arch=_ARCH_BAND, ns=_NS_CAT)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", True)
        memory = _recall(monkeypatch, "What is the name of my cat?", _EMPTY_PAYLOAD)
        # the archive band IS the archive-cue answer (BUG-RECALL-11): its arm is
        # untouched by the fix — abstention + archive + band, serve never runs
        assert memory == (_PLAIN_EMPTY + "\n\n" + _ARCH_BAND
                          + "\n\n" + _BAND_CAT), memory
        assert calls == [], calls

    def test_fifth_face_operand_refusal_opens_under_on(self, gate_env, monkeypatch):
        payload = {"anchor": "user",
                   "facts": [{"subject": "user", "rel_type": "likes", "object": "tea",
                              "definition": "You like tea.", "fact_class": "A",
                              "fact_provenance": "user_stated", "confidence": 1.0}],
                   "preferred_names": {}, "attributes": {}}
        calls = _plant(monkeypatch, band=_BAND_CAT, ns=_NS_TOY)
        monkeypatch.setattr(S, "ABSTENTION_OPERAND_GROUNDING", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", False)
        memory = _recall(monkeypatch, "What is Luna's favorite toy?", payload)
        # 'Luna' is grounded nowhere in the payload, but a non-empty band no longer
        # suppresses the negative-space serve at the fifth face
        assert memory == _NS_TOY, memory
        assert _PLAIN_EMPTY not in memory
        assert calls, calls

    def test_fifth_face_off_keeps_the_operand_refusal_alone(
            self, gate_env, monkeypatch):
        payload = {"anchor": "user",
                   "facts": [{"subject": "user", "rel_type": "likes", "object": "tea",
                              "definition": "You like tea.", "fact_class": "A",
                              "fact_provenance": "user_stated", "confidence": 1.0}],
                   "preferred_names": {}, "attributes": {}}
        calls = _plant(monkeypatch, band=_BAND_CAT, ns=_NS_TOY)
        monkeypatch.setattr(S, "ABSTENTION_OPERAND_GROUNDING", True)
        monkeypatch.setattr(S, "EPISODIC_BAND_UNDER_SERVE", False)
        monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", False)
        memory = _recall(monkeypatch, "What is Luna's favorite toy?", payload)
        assert memory == _PLAIN_EMPTY, memory  # band-replaces-empty as before; no ride
        assert calls == [], calls

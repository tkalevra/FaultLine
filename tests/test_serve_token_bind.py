"""THE SERVE TOKEN-BIND PINS (wound-map round-5, map100 ranked fixes 1–4).

Spec: tools/longmemeval/runs/map100/REPORT.md — ALL 50 pilot golds are stored
structurally; the 16% is pure serve-side (binding 21 / fetch 9 / render 12).
These pins hold the four independently flag-gated fixes over the ONE shared
matcher (src/mcp/token_bind.py):

  fix 1  SERVE_REFUSAL_TOKEN_BIND  — the five abstention faces bind a live row
        before the empty line ("You X: <value>"), filling ONLY the empty arm;
  fix 2  SERVE_RENDER_TOKEN_RANK   — the top question-overlap line leads the
        assert band (occupation-dump-for-commute, "3 playlists vs stored 20");
  fix 3  SERVE_BOUND_ANSWER_FRAMING — a directly-bound payload row frames the
        ANSWER, never dump framing;
  fix 4  NEGATIVE_SPACE_HONESTY_GUARD — "no <token> on record" only ever voices
        after the SAME token-alias search ran empty.

Every flag: default ON; OFF ⇒ byte-parity (pinned per flag below).
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


# ── THE MATCHER: the map100 table's own example bindings (pure, hermetic) ─────────

_ATTR_ROWS = [
    ("handbag_price_usd", "800"),
    ("volunteer_event_date", "February 14th (Valentine's Day)"),
    ("bike_count", "3"),
    ("costa_rica_shirts_packed", "7"),
    ("debut_album_limited_copies", "500"),
    ("first_purchase_discount_pct", "10"),
    ("previous_family_name", "Johnson"),
    ("internet_speed_mbps", "500"),
    ("spotify_playlist_count", "20"),
    ("commute_duration_minutes_each_way", "45"),
    ("japan_trip_duration_weeks", "2"),
    ("instagram_daily_screen_time_hours", "2"),
    ("work_email_cutoff_time", "19:00"),
    ("moving_duration_hours", "5"),
    ("vintage_camera_collecting_duration_months", "3"),
    ("ikea_bookshelf_assembly_hours", "4"),
    ("comedians_watched_at_open_mic", "10"),
    ("occupation", "teacher"),
    ("occupation", "marketing specialist"),
]

_FACT_ROWS = [
    ("studied_abroad_at", "University of Melbourne"),
    ("gave_gift_sister", "yellow dress"),
    ("graduated_with", "Business Administration"),
    ("attended_play", "Glass Menagerie"),
    ("owns_item", "Philips LED bulb"),
    ("streams_music_on", "Spotify"),
    ("attended_concert_at", "Xfinity Center"),
]

_BINDS = [
    ("How much did I spend on my handbag?", _ATTR_ROWS, "800"),
    ("When is my volunteer event?", _ATTR_ROWS, "February 14th"),
    ("How many bikes do I own?", _ATTR_ROWS, "3"),
    ("How many shirts did I pack for Costa Rica?", _ATTR_ROWS, "7"),
    ("How many copies of my debut album were made?", _ATTR_ROWS, "500"),
    ("What was the discount on my first purchase?", _ATTR_ROWS, "10"),
    ("What was my last name before I changed it?", _ATTR_ROWS, "Johnson"),
    ("What is my internet speed?", _ATTR_ROWS, "500"),
    ("How many playlists do I have on Spotify?", _ATTR_ROWS, "20"),
    ("How long is my commute each way?", _ATTR_ROWS, "45"),
    ("How long was my trip to Japan?", _ATTR_ROWS, "2"),
    ("How much screen time do I spend on Instagram daily?", _ATTR_ROWS, "2"),
    ("When do I stop checking my work email?", _ATTR_ROWS, "19:00"),
    ("How long did the move take?", _ATTR_ROWS, "5"),
    ("How long have I been collecting vintage cameras?", _ATTR_ROWS, "3"),
    ("How long did the IKEA bookshelf take to assemble?", _ATTR_ROWS, "4"),
    ("How many comedians did I watch at the open mic?", _ATTR_ROWS, "10"),
    ("Where did I study abroad?", _FACT_ROWS, "University of Melbourne"),
    ("What did I give my sister for her birthday gift?", _FACT_ROWS, "yellow dress"),
    ("What degree did I graduate with?", _FACT_ROWS, "Business Administration"),
    ("What play did I attend?", _FACT_ROWS, "Glass Menagerie"),
    ("What brand of light bulbs do I own?", _FACT_ROWS, "Philips LED bulb"),
    ("What app do I use for streaming music?", _FACT_ROWS, "Spotify"),
    ("What venue was the concert at?", _FACT_ROWS, "Xfinity Center"),
]


@pytest.mark.parametrize("question,rows,gold", _BINDS)
def test_tb_map100_example_bindings(question, rows, gold):
    """Every map100 example binding shape: the question's tokens bind the stored
    row whose VALUE carries the gold (substring — the gold text rides the lead)."""
    from src.mcp.token_bind import best_row, query_weights
    best = best_row(rows, query_weights(question))
    assert best is not None, f"no bind for {question!r}"
    assert gold.lower() in str(best[1]).lower(), (question, best)


_NO_BINDS = [
    ("Do I have a pet dragon?", _ATTR_ROWS),
    ("What's the weather like on Mars?", _ATTR_ROWS),
    ("", _ATTR_ROWS),
    # WEAK-ONLY: "how many" alone never binds (below the fire floor)
    ("How many?", _ATTR_ROWS),
    ("How long?", _ATTR_ROWS),
]


@pytest.mark.parametrize("question,rows", _NO_BINDS)
def test_tb_no_match_leg_never_fabricates(question, rows):
    """The negative-space guard's no-match leg: an absent topic stays UNBOUND —
    the honest abstention stands, the guard never invents a row."""
    from src.mcp.token_bind import best_row, query_weights
    assert best_row(rows, query_weights(question)) is None


def test_tb_lead_line_plain_answers_voice():
    from src.mcp.token_bind import lead_line
    assert lead_line("handbag_price_usd", 800) == "You handbag price usd: 800"
    assert lead_line("studied_abroad_at", "University of Melbourne") == \
        "You studied abroad at: University of Melbourne"


def test_tb_specificity_then_richer_name_win_on_tie():
    """Tie-breaks (map-measured classes): matched-stem SPECIFICITY first
    (commute ⊐ work), then the RICHER name (volunteer_event_date ⊐
    volunteers_at — the answer-bearing column over the topical rel)."""
    from src.mcp.token_bind import best_row, query_weights
    rows = [("works_as", "analyst"),
            ("commute_duration_minutes_each_way", "45")]
    assert best_row(rows, query_weights(
        "How long is my daily commute to work?")) == \
        ("commute_duration_minutes_each_way", "45")
    rows = [("volunteers_at", "Harvard Athletic Complex"),
            ("volunteer_event_date", "February 14th")]
    assert best_row(rows, query_weights(
        "When did I volunteer at the shelter?")) == \
        ("volunteer_event_date", "February 14th")
    # equal score AND equal specificity → the richer (answer-bearing) name
    rows = [("handbag_note", "leather"), ("handbag_price_usd", "800")]
    assert best_row(rows, query_weights(
        "How much was my handbag?")) == ("handbag_price_usd", "800")


# ── FIX 2: SERVE_RENDER_TOKEN_RANK (pure reorder; OFF ⇒ order untouched) ──────────


def test_tb_rank_commute_leads_over_occupation_dump():
    """The map's fetch-dump class: occupations dump for a commute question — the
    commute row's line moves to the front of the assert band."""
    from src.mcp.token_bind import move_top_overlap_lead
    lines = [
        "occupation: teacher",
        "occupation: marketing specialist",
        "commute_duration_minutes_each_way: 45",
        "favorite_color: blue",
    ]
    assert move_top_overlap_lead("How long is my commute each way?", lines) is True
    assert lines[0] == "commute_duration_minutes_each_way: 45"


def test_tb_rank_playlist_20_outranks_distractors():
    """The "3 playlists vs stored 20" misrank: spotify_playlist_count leads."""
    from src.mcp.token_bind import move_top_overlap_lead
    lines = [
        "created_playlist: Chill Mix",
        "created_playlist: Road Trip",
        "created_playlist: Study Beats",
        "spotify_playlist_count: 20",
    ]
    assert move_top_overlap_lead(
        "How many playlists do I have on Spotify?", lines) is True
    assert lines[0] == "spotify_playlist_count: 20"


def test_tb_rank_parity_no_clear_winner():
    """Byte-parity leg: a question with no clear winner keeps today's order —
    no reorder, no exception, no data loss."""
    from src.mcp.token_bind import move_top_overlap_lead
    lines = ["occupation: teacher", "favorite_color: blue"]
    before = list(lines)
    assert move_top_overlap_lead("What did I eat for breakfast?", lines) is False
    assert lines == before
    # tie with the current lead → no move (strict winner only)
    lines2 = ["handbag_price_usd: 800", "handbag_seller: Macy's"]
    before2 = list(lines2)
    assert move_top_overlap_lead("How much was my handbag?", lines2) is False
    assert lines2 == before2


# ── FIX 1 + FIX 3: the server helpers (flag OFF ⇒ None ⇒ byte-parity) ─────────────


def test_tb_refusal_serve_flag_off_is_none(monkeypatch):
    """OFF ⇒ byte-parity: the helper never fires, the refusal path is today's."""
    import src.mcp.server as S
    monkeypatch.setattr(S, "SERVE_REFUSAL_TOKEN_BIND", False)
    assert S._token_bind_refusal_serve(
        "11111111-1111-1111-1111-111111111111",
        "How much did I spend on my handbag?") is None


def test_tb_refusal_serve_failsafe_without_db(monkeypatch):
    """ON but no DB reachable → fail-safe None (the abstention stands)."""
    import src.mcp.server as S
    monkeypatch.setattr(S, "SERVE_REFUSAL_TOKEN_BIND", True)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    assert S._token_bind_refusal_serve(
        "11111111-1111-1111-1111-111111111111",
        "How much did I spend on my handbag?") is None


def test_tb_payload_lead_binds_and_frames():
    """FIX 3: the walk payload's own bound row frames the ANSWER lead."""
    import src.mcp.server as S
    lead = S._token_bind_payload_lead(
        "How much did I spend on my handbag?",
        [{"rel_type": "owns_pet", "object": "Luna"}],
        {"handbag_price_usd": 800, "occupation": "teacher"})
    assert lead == "You handbag price usd: 800"


def test_tb_payload_lead_flag_off_is_none(monkeypatch):
    import src.mcp.server as S
    monkeypatch.setattr(S, "SERVE_BOUND_ANSWER_FRAMING", False)
    assert S._token_bind_payload_lead(
        "How much did I spend on my handbag?", [], {"handbag_price_usd": 800}) is None


def test_tb_payload_lead_no_match_is_none():
    """No bound row ⇒ None ⇒ today's render byte-for-byte (never fabricates)."""
    import src.mcp.server as S
    assert S._token_bind_payload_lead(
        "Do I have a pet dragon?", [], {"occupation": "teacher"}) is None


def test_tb_flags_default_on():
    import src.mcp.server as S
    import src.api.main as M
    assert S.SERVE_REFUSAL_TOKEN_BIND is True
    assert S.SERVE_RENDER_TOKEN_RANK is True
    assert S.SERVE_BOUND_ANSWER_FRAMING is True
    assert S.NEGATIVE_SPACE_HONESTY_GUARD is True
    assert M._NEGATIVE_SPACE_HONESTY_GUARD is True


# ── FIX 4: the negative-space honesty guard (pure render + lookup legs) ───────────


def test_tb_guard_count_definition_hit_numeric():
    """The map's fetch-lookup class: bike_count=3 over "no bikes on record" —
    the stored count IS the answer, asserted, verbatim.
    ROUND-6 (by-design vehicle update, mechanism assertion unchanged):
    SERVE_VALUE_SURFACE_NORMALIZE ON appends the word-voiced lead line —
    the numeral sentence stays line one (additive; 6b168ec8's gold is the
    word "three"), OFF keeps the round-5 receipt bytes verbatim."""
    import src.api.main as M
    _on = M._SERVE_VALUE_SURFACE_NORMALIZE
    try:
        M._SERVE_VALUE_SURFACE_NORMALIZE = False
        d, cls = M._honesty_guard_count_definition(
            "bikes", {"attribute": "bike_count", "value": "3"})
        assert (d, cls) == ("You have 3 bikes on record.", "A")
        M._SERVE_VALUE_SURFACE_NORMALIZE = True
        d, cls = M._honesty_guard_count_definition(
            "bikes", {"attribute": "bike_count", "value": "3"})
        assert (d, cls) == \
            ("You have 3 bikes on record.\nYou bike count: three", "A")
    finally:
        M._SERVE_VALUE_SURFACE_NORMALIZE = _on


def test_tb_guard_count_definition_hit_non_numeric():
    import src.api.main as M
    d, cls = M._honesty_guard_count_definition(
        "shirts", {"attribute": "asylum_decision_wait", "value": "over a year"})
    assert cls == "A" and "over a year" in d


def test_tb_guard_count_definition_miss_is_byte_parity():
    """The no-match leg: the lookup ran empty ⇒ today's miss render verbatim."""
    import src.api.main as M
    assert M._honesty_guard_count_definition("dragons", None) == \
        (M._count_miss_definition("dragons"), "C")


def test_tb_guard_lookup_failsafe_no_db():
    import src.api.main as M
    assert M._token_alias_attr_lookup(None, "u", "bikes") is None
    assert M._token_alias_attr_lookup(object(), None, "bikes") is None


# ── DB-GATED: the combined leg (fixture store through the real helpers) ───────────

_DSN = os.environ.get("POSTGRES_DSN", "")
_tb_db = pytest.mark.skipif(
    not _DSN, reason="POSTGRES_DSN unset — the serve DB gate stays dark")


@_tb_db
def test_tb_db_refusal_bind_through_store_read():
    """FIX 1, DB-gated on a THROWAWAY schema in the test DB (dropped in finally):
    the refusal-gate lookup reads the schema's live attrs + fact object aliases,
    binds the question token, and renders the answer lead — while a SUPERSEDED
    twin never rides and an absent topic stays unbound (no-match leg, live)."""
    import psycopg2
    import src.mcp.server as S

    uid = "2222tb99-9999-4999-8999-999999999999"
    schema = "faultline_" + uid.replace("-", "_")
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "CREATE TABLE entity_attributes (id serial, entity_id text,"
                " attribute text, value_text text, value_int int, value_float float,"
                " value_date timestamptz, value_normalized text, superseded_at timestamptz,"
                " valid_until timestamptz, updated_at timestamptz default now())")
            cur.execute(
                "CREATE TABLE facts (id serial, subject_id text, rel_type text,"
                " object_id text, superseded_at timestamptz, archived_at timestamptz,"
                " deleted_at timestamptz, polarity text default 'affirmed',"
                " valid_until timestamptz, created_at timestamptz default now())")
            cur.execute(
                "CREATE TABLE entity_aliases (id serial, entity_id text, alias text,"
                " is_preferred boolean default true, display_form text)")
            mel = "3333ee99-9999-4999-8999-9999999999ee"
            cur.execute(
                "INSERT INTO entity_attributes (entity_id, attribute, value_int)"
                " VALUES (%s, 'bike_count', 3), (%s, 'handbag_price_usd', 800)",
                (uid, uid))
            cur.execute(  # superseded twin must never ride
                "INSERT INTO entity_attributes (entity_id, attribute, value_int,"
                " superseded_at) VALUES (%s, 'bike_count', 99, now())", (uid,))
            cur.execute(
                "INSERT INTO facts (subject_id, rel_type, object_id)"
                " VALUES (%s, 'studied_abroad_at', %s)", (uid, mel))
            cur.execute(
                "INSERT INTO entity_aliases (entity_id, alias, is_preferred)"
                " VALUES (%s, 'University of Melbourne', true)", (mel,))
        import os as _os
        _os.environ["POSTGRES_DSN"] = _DSN
        # ROUND-6 (by-design vehicle update, mechanism assertion unchanged):
        # the OFF arm is the round-5 receipt byte ("You bike count: 3"); the
        # ON arm voices the count per the closed rule and keeps the raw form
        # as the lead's second line (additive — never loses the stored
        # surface). The Melbourne fact-alias leg is voice-neutral both arms.
        _on = S.SERVE_VALUE_SURFACE_NORMALIZE
        try:
            S.SERVE_VALUE_SURFACE_NORMALIZE = False
            out = S._token_bind_refusal_serve(uid, "How many bikes do I own?")
            assert out == "You bike count: 3"
            out2 = S._token_bind_refusal_serve(uid, "Where did I study abroad?")
            assert out2 == "You studied abroad at: University of Melbourne"
            S.SERVE_VALUE_SURFACE_NORMALIZE = True
            out = S._token_bind_refusal_serve(uid, "How many bikes do I own?")
            assert out == "You bike count: three\nbike count: 3"
            out2 = S._token_bind_refusal_serve(uid, "Where did I study abroad?")
            assert out2 == "You studied abroad at: University of Melbourne"
            # the no-match leg, LIVE: an absent topic never binds
            assert S._token_bind_refusal_serve(uid, "Do I have a pet dragon?") is None
        finally:
            S.SERVE_VALUE_SURFACE_NORMALIZE = _on
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


@_tb_db
def test_tb_db_guard_lookup_over_fixture_attrs():
    """FIX 4, DB-gated on TEMP tables (search_path pg_temp shadows tenant names):
    the token-alias lookup finds bike_count over "no bikes on record", and stays
    empty for an absent topic (the absence claim is then honest)."""
    import psycopg2
    import src.api.main as M

    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO pg_temp, public")
            cur.execute(
                "CREATE TEMP TABLE entity_attributes (entity_id text, attribute text,"
                " value_text text, value_int int, value_float float,"
                " value_date timestamptz, value_normalized text,"
                " superseded_at timestamptz)")
            cur.execute(
                "INSERT INTO entity_attributes VALUES"
                " ('u1', 'bike_count', NULL, 3, NULL, NULL, NULL, NULL),"
                " ('u1', 'costa_rica_shirts_packed', NULL, 7, NULL, NULL, NULL, NULL),"
                " ('u1', 'occupation', 'teacher', NULL, NULL, NULL, NULL, NULL)")
        hit = M._token_alias_attr_lookup(conn, "u1", "How many bikes do I own? bikes")
        assert hit == {"attribute": "bike_count", "value": "3"}
        miss = M._token_alias_attr_lookup(conn, "u1", "Do I have a pet dragon? dragons")
        assert miss is None
    finally:
        conn.close()


# ── THE BAND LEG (the compose2 re-measure's fix): the binding-echo slot guard ─────
# tools/longmemeval/runs/compose2/REPORT.md — the measured −1 real regression,
# attributed to this lane: with SERVE_REFUSAL_TOKEN_BIND armed, the recall turn's
# OWN question (re-captured verbatim by the fast-path retention arm with intent
# NULL — a classified QUERY is never captured, BUG-RECALL-3, so the intent-aware
# echo arm misses exactly this class) matched every sel word, won the top fused
# rank, and under the band's cap-2 truncation it DISPLACED the gold-bearing turn.
# qid 001be529: the band rode [echo, assistant turn] and "Over a year of
# uncertainty" fell out → the composer honestly answered "I don't know" → composed
# 24%→18%. THE FIX: under the flag, the directly-addressed echo rides as ONE extra
# band row that never consumes a cap slot; the ranked answer rows fill the cap
# exactly as the flag-OFF selection does. THE INVARIANT (pinned below): with
# binding ON, every row present in the OFF-state band remains present ON — the
# echo's cost comes out of the band's slack, never out of the gold.

_001BE529_Q = "How long did I wait for the decision on my asylum application?"
# The measured 240-char SQL cuts, byte-for-byte from the compose2 serve record
# (results_raw.jsonl qid 001be529) + the full turns from the abflags record —
# the fixtures extend them so `left(raw_text, 240)` reproduces the measured rows.
_001BE529_ECHO_ROW = (
    "How long did I wait for the decision on my asylum application?")
_001BE529_ASST_240 = (
    "[assistant] I'm so glad to hear that your asylum application has been "
    "approved! That must be a huge weight off your shoulders. The waiting "
    "process can be incredibly stressful and uncertain, and it's amazing that "
    "you've made it through that ")
_001BE529_ASST_FULL = _001BE529_ASST_240 + "for so long. You should be proud."
_001BE529_GOLD_240 = (
    "[user] I've been using Zillow and HotPads, but I haven't tried the others. "
    "I'll definitely check them out. By the way, speaking of waiting, it's crazy "
    "how long it took for my asylum application to get approved. Over a year of "
    "uncertainty wa")
_001BE529_GOLD_FULL = _001BE529_GOLD_240 + (
    "s really tough. But at least it's behind me now, and I can focus on "
    "building my new life here.")
assert len(_001BE529_ASST_240) == 240 and len(_001BE529_GOLD_240) == 240
assert "Over a year of uncertainty" in _001BE529_GOLD_240


def _band_rows(band: str) -> list[str]:
    """The band's row lines (the '- ' prefixed lines under the label)."""
    return [ln[2:] for ln in band.split("\n") if ln.startswith("- ")]


def _plant_001be529(cur, schema: str) -> None:
    """The 001be529 episodic shape: gold user turn OLDEST, the assistant reply
    next, and the recall turn's own just-captured question NEWEST with intent
    NULL (the fast-path retention arm's shape — the compose2 capture)."""
    cur.execute(
        f'INSERT INTO "{schema}".episodic_log (raw_text, intent, created_at)'
        " VALUES (%s, '', '2026-01-02 09:00:00+00'),"      # the gold-bearing turn
        " (%s, '', '2026-01-02 09:05:00+00'),"             # the assistant reply
        " (%s, NULL, '2026-11-15 00:47:00+00')",           # the just-captured echo
        (_001BE529_GOLD_FULL, _001BE529_ASST_FULL, _001BE529_Q))


class TestBindingEchoSlotGuard:
    """THE BAND LEG pins (DB-gated; the band is a Postgres read)."""

    def test_001be529_shape_on_both_survive_off_the_echo_displaces(
            self, postgres_dsn, monkeypatch):
        """THE MEASURED REGRESSION, both legs byte-pinned. OFF (byte-parity =
        today's path): the NULL-intent echo consumes cap slot 1 and the gold
        turn is EVICTED — the exact compose2 band. ON: the echo rides as the
        extra row and BOTH the echo and the gold-bearing turn survive."""
        import psycopg2
        import src.mcp.server as S

        seat = str(uuid.uuid4())
        schema = "faultline_" + seat.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(
                    f'CREATE TABLE "{schema}".episodic_log ('
                    " id bigserial PRIMARY KEY, raw_text text NOT NULL,"
                    " intent text, created_at timestamptz DEFAULT now())")
                _plant_001be529(cur, schema)
            monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
            # ── OFF: byte-parity with today's path — the measured eviction ──
            monkeypatch.setattr(S, "SERVE_REFUSAL_TOKEN_BIND", False)
            band_off = S._episodic_verbatim_band_once(seat, _001BE529_Q)
            assert band_off == (
                S._EPISODIC_BAND_LABEL + "\n- " + _001BE529_ECHO_ROW
                + "\n- " + _001BE529_ASST_240), band_off
            assert "Over a year of uncertainty" not in band_off
            assert len(_band_rows(band_off)) == 2  # the cap held, the gold lost
            # ── ON: the echo appends, the gold returns ──
            monkeypatch.setattr(S, "SERVE_REFUSAL_TOKEN_BIND", True)
            band_on = S._episodic_verbatim_band_once(seat, _001BE529_Q)
            rows_on = _band_rows(band_on)
            assert "Over a year of uncertainty" in band_on, band_on
            assert _001BE529_ECHO_ROW in rows_on  # the directly-addressed turn
            assert _001BE529_ASST_240 in rows_on
            assert rows_on[0] == _001BE529_ECHO_ROW  # the echo LEADS (top fused)
            assert len(rows_on) == 3  # the cap-2 answer rows + the echo extra
            # THE INVARIANT, on the measured shape: every OFF row survives ON
            for row in _band_rows(band_off):
                assert row in rows_on
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()

    def test_no_echo_on_band_is_byte_identical_off(self, postgres_dsn,
                                                   monkeypatch):
        """The guard is a NO-OP without the echo class (no all-sel-word
        NULL-intent row): ON band == OFF band byte-for-byte — the other 49
        questions' shapes are untouched."""
        import psycopg2
        import src.mcp.server as S

        seat = str(uuid.uuid4())
        schema = "faultline_" + seat.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(
                    f'CREATE TABLE "{schema}".episodic_log ('
                    " id bigserial PRIMARY KEY, raw_text text NOT NULL,"
                    " intent text, created_at timestamptz DEFAULT now())")
                cur.execute(
                    f'INSERT INTO "{schema}".episodic_log'
                    " (raw_text, intent, created_at) VALUES"
                    " (%s, '', '2026-01-02 09:00:00+00'),"
                    " (%s, '', '2026-01-02 09:10:00+00')",
                    (_001BE529_GOLD_FULL, _001BE529_ASST_FULL))
            monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
            monkeypatch.setattr(S, "SERVE_REFUSAL_TOKEN_BIND", False)
            band_off = S._episodic_verbatim_band_once(seat, _001BE529_Q)
            monkeypatch.setattr(S, "SERVE_REFUSAL_TOKEN_BIND", True)
            band_on = S._episodic_verbatim_band_once(seat, _001BE529_Q)
            assert band_on == band_off
            assert "Over a year of uncertainty" in band_on
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()

    def test_general_invariant_on_band_supersets_off_band(
            self, postgres_dsn, monkeypatch):
        """THE GENERAL INVARIANT LEG, richer shape: a properly-classified
        QUERY-intent echo stays excluded in BOTH arms (BUG-RECALL-3 discipline);
        an all-sel-word STATEMENT row (the qF3 seed-turn class the intent-aware
        echo arm keeps) competes for cap slots in BOTH arms; the OFF band rows
        are a subset of the ON band rows in every shape (legacy cap-2 and
        QA-shape cap-3), and the ON extra is exactly the fused-first echo."""
        import psycopg2
        import src.mcp.server as S

        statement_allmatch = (
            "[user] Honestly the decision on my asylum application was the "
            "hardest part; the waiting itself I could handle.")
        distractor_old = (
            "[user] My asylum application paperwork finally got filed today.")
        seat = str(uuid.uuid4())
        schema = "faultline_" + seat.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(
                    f'CREATE TABLE "{schema}".episodic_log ('
                    " id bigserial PRIMARY KEY, raw_text text NOT NULL,"
                    " intent text, created_at timestamptz DEFAULT now())")
                cur.execute(
                    f'INSERT INTO "{schema}".episodic_log'
                    " (raw_text, intent, created_at) VALUES"
                    " (%s, 'QUERY', '2026-01-03 08:00:00+00'),"   # classified echo
                    " (%s, '', '2026-01-03 08:10:00+00'),"        # all-match stmt
                    " (%s, '', '2026-01-03 08:05:00+00'),"        # distractor
                    " (%s, '', '2026-01-03 08:22:00+00'),"        # gold-bearing
                    " (%s, NULL, '2026-11-15 00:47:00+00')",      # the NULL echo
                    (_001BE529_Q, statement_allmatch, distractor_old,
                     _001BE529_GOLD_FULL, _001BE529_Q))
            monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
            for qa_shape, cap in ((False, 2), (True, 3)):
                monkeypatch.setattr(S, "EPISODIC_BAND_QA_SHAPE", qa_shape)
                monkeypatch.setattr(S, "SERVE_REFUSAL_TOKEN_BIND", False)
                band_off = S._episodic_verbatim_band_once(seat, _001BE529_Q)
                rows_off = _band_rows(band_off)
                monkeypatch.setattr(S, "SERVE_REFUSAL_TOKEN_BIND", True)
                band_on = S._episodic_verbatim_band_once(seat, _001BE529_Q)
                rows_on = _band_rows(band_on)
                assert len(rows_off) <= cap
                assert len(rows_on) <= cap + 1  # cap + the one echo extra
                # the classified echo NEVER serves (both arms, BUG-RECALL-3)
                assert not any(r.startswith("[user] How long did I wait")
                               for r in rows_off + rows_on)
                # THE INVARIANT: OFF ⊆ ON, the extra is bounded by one row,
                # and the directly-addressed echo is never the evicted row
                assert set(rows_off) <= set(rows_on), (qa_shape, rows_off, rows_on)
                assert "Over a year of uncertainty" in band_on
                assert _001BE529_ECHO_ROW in rows_on
                assert len([r for r in rows_on if r not in rows_off]) <= 1
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()

    def test_band_leg_rides_the_serve_flag_not_a_new_flag(self):
        """No new flag exists: the band leg is keyed to the EXISTING
        SERVE_REFUSAL_TOKEN_BIND (the compose2 arms' attribution switch) — the
        guard source must reference it, and it must sit inside the RRF branch
        downstream of the intent-aware echo arm (a classified QUERY echo is
        still dropped unconditionally; the legacy non-RRF path keeps its
        shape-blind exclusion and gains no echo-extra)."""
        import src.mcp.server as S

        src = open("src/mcp/server.py").read()
        rrf = src.split("if RECALL_RRF_FUSION:")[1].split(
            "for w in sel_words:\n            cur.execute(")[0]
        # the guard: flag-keyed, one slot, downstream of the intent-aware arm
        guard = rrf.split("INTENT-AWARE ECHO ARM")[1]
        assert "SERVE_REFUSAL_TOKEN_BIND and _echo_row is None" in guard
        assert "matches >= len(sel_l)" in guard
        # the empty-gate and the render lead with the extra slot
        assert "if not overlapped and _echo_row is None:" in rrf
        assert rrf.index("if _echo_row is not None:") < \
            rrf.index("for r in overlapped:")
        # the LEGACY path keeps its unconditional echo exclusion — no extra slot
        legacy = src.split("for w in sel_words:\n            cur.execute(")[1]
        assert "_echo_row" not in legacy.split("except Exception:")[0]
        assert S.SERVE_REFUSAL_TOKEN_BIND is True  # the measured default-ON arm

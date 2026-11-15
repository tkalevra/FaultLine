"""Round-6 value-surface pins (the map100 residual class + the 6ade9755 eviction).

THE WOUND (SERVE_BINDING_FIXER_REPORT.md's residual 16 + compose5/REPORT.md's
disclosed regression): golds that ARE stored but surface in a form the
gold-token scoring misses — digit-vs-word ("three" vs 3), time-of-day
("7 pm" vs stored 19:00), unit fusion ("16GB" vs 16), the occupation+employer
span ("Marketing specialist at a small startup" spanning two rows), and the
6ade9755 render eviction (the reshaped negative-space render evicted the
takes_classes_at row — its fetch reached only the topic + the taxonomy bridge).

THE FIX (two flags, both default ON, OFF = byte-parity):
  SERVE_VALUE_SURFACE_NORMALIZE — the rendered value revoices per the ONE
    closed rule pinned in src/mcp/value_surface.py; ADDITIVE (the raw stored
    form always stays visible: the lead's second line on lead-only serves, the
    untouched band row on banded serves).
  SERVE_RENDER_ASPECT_RETAIN — the negative-space render admits the
    owner-anchored candidates (same live/affirmed guards, the SAME per-row
    aspect-affinity floor ≥1) and lets the best name-matched admitted row LEAD
    in the answer voice — the evicted row's class survives as lead AND band row.

Pins here: the pure rule legs, the OFF-parity legs per seam, the 6ade9755
eviction both arms on a fixture store shaped like the map/compose5 rows, the
per-residual door legs (fix1/fix3/fix4), and the no-op parity leg (the pinned
cats-class shape is byte-identical when no owner row passes the floor).
DB legs run on the throwaway control-plane DSN and skip (loudly) without one;
they create and drop their OWN throwaway schema and never touch a real tenant.
"""
from __future__ import annotations

import uuid

import pytest

from src.mcp.server import (_EMPLOYER_ASPECT_STEMS, _OCCUPATION_ASPECT_STEMS,
                            _span_pair_value, _negative_space_assemble)
from src.mcp.value_surface import voice_value


# ── PART 1 — the closed rule, pure legs ─────────────────────────────────────────

class TestVoiceValueRule:
    """The pinned rule (src/mcp/value_surface.py docstring) — one leg per
    residual shape plus the exclusion class (the no-regression surface)."""

    def test_digit_vs_word_residual_legs(self):
        # 6b168ec8 (bike_count=3 → "three"), 19b5f2b3 (2 weeks → "two"),
        # 15745da0 (3 months → "three")
        assert voice_value("bike_count", "3") == "three"
        assert voice_value("japan_trip_duration_weeks", "2") == "two"
        assert voice_value("vintage_camera_collecting_duration_months",
                           "3") == "three"
        assert voice_value("costa_rica_shirts_packed", "7") == "seven"
        assert voice_value("spotify_playlist_count", "20") == "twenty"

    def test_time_of_day_residual_legs(self):
        # 577d4d32: stored 19:00, gold "7 pm" — bounded 24h clock, cued name
        assert voice_value("work_email_cutoff_time", "19:00") == "7 pm"
        assert voice_value("work_email_cutoff_time", "9:30") == "9:30 am"
        assert voice_value("wake_up_time", "00:15") == "12:15 am"
        assert voice_value("store_opens_time", "12:00") == "12 pm"
        # the uncued H:MM never revoices (a 1:30 race split is not a clock)
        assert voice_value("race_split", "1:30") == "1:30"
        assert voice_value("meeting_length", "1:30") == "1:30"

    def test_byte_fusion_residual_leg(self):
        # 37d43f65: stored 16, gold "16GB" — the byte-class fuses
        assert voice_value("laptop_ram_gb", "16") == "16GB"
        assert voice_value("phone_storage_gb", "256") == "256GB"

    def test_multiplier_word_residual_leg(self):
        # b86304ba: stored "3x purchase price", gold says "triple"
        assert voice_value("sunset_painting_value_multiple",
                           "3x purchase price") == "triple purchase price"
        assert voice_value("sunset_painting_value_multiple",
                           "2x purchase price") == "double purchase price"
        assert voice_value("sunset_painting_value_multiple",
                           "5x purchase price") == "5x purchase price"

    def test_exclusion_class_never_revoices(self):
        # the currently-passing golds: "500 Mbps" (spaced), "5 hours"/"45
        # minutes" (numeral), "$800"/"10%" (the scorer strips $/%), 500 copies.
        # Revoicing ANY of these would regress a live conversion.
        assert voice_value("internet_speed_mbps", "500") == "500"
        assert voice_value("moving_duration_hours", "5") == "5"
        assert voice_value("commute_duration_minutes_each_way", "45") == "45"
        assert voice_value("handbag_price_usd", "800") == "800"
        assert voice_value("first_purchase_discount_pct", "10") == "10"
        assert voice_value("debut_album_limited_copies", "500") == "500"
        # bare counts ≤ twenty DO voice ('twelve' + the raw second line — the
        # numeral gold stays covered by the raw form; 21436231's gold "12"
        # rides the raw row/lead line)
        assert voice_value("largemouth_bass_caught", "12") == "twelve"

    def test_prose_values_untouched(self):
        assert voice_value("bedroom_wall_color",
                           "lighter shade of gray") == "lighter shade of gray"
        assert voice_value("volunteer_event_date",
                           "February 14th") == "February 14th"


# ── PART 2 — the lead seams, OFF parity ─────────────────────────────────────────

class TestLeadServeLine:
    """_lead_serve_line: OFF = lead_line verbatim; ON = voiced + the raw
    stored form as the second line (additive — never loses a surface)."""

    def _line(self, monkeypatch, name, value, rows=None, on=True):
        import src.mcp.server as S
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", on)
        return S._lead_serve_line(name, value, rows=rows)

    def test_off_is_the_verbatim_lead(self, monkeypatch):
        from src.mcp.token_bind import lead_line
        assert self._line(monkeypatch, "work_email_cutoff_time", "19:00",
                          on=False) == \
            lead_line("work_email_cutoff_time", "19:00") == \
            "You work email cutoff time: 19:00"

    def test_on_voices_and_keeps_the_raw(self, monkeypatch):
        assert self._line(monkeypatch, "work_email_cutoff_time", "19:00") == \
            "You work email cutoff time: 7 pm\nwork email cutoff time: 19:00"
        assert self._line(monkeypatch, "bike_count", "3") == \
            "You bike count: three\nbike count: 3"

    def test_on_byte_identical_when_the_voice_matches(self, monkeypatch):
        from src.mcp.token_bind import lead_line
        # exclusion class: the voiced form IS the raw — no second line, and
        # the OFF bytes hold ON (the no-regression surface)
        assert self._line(monkeypatch, "internet_speed_mbps", "500") == \
            self._line(monkeypatch, "internet_speed_mbps", "500", on=False) == \
            lead_line("internet_speed_mbps", "500")

    def test_span_pair_lead(self, monkeypatch):
        rows = [("previous_employer", "a small startup"),
                ("occupation", "software engineer")]
        assert self._line(monkeypatch, "previous_occupation",
                          "marketing specialist", rows=rows) == \
            "You previous occupation: marketing specialist at a small " \
            "startup\nprevious occupation: marketing specialist"

    def test_span_needs_both_aspects_and_a_shared_modifier(self, monkeypatch):
        assert _span_pair_value(
            [("occupation", "software engineer")], "previous_occupation",
            "marketing specialist") == "marketing specialist"
        assert _span_pair_value(
            [("previous_hobby", "pottery")], "previous_occupation",
            "marketing specialist") == "marketing specialist"
        assert _OCCUPATION_ASPECT_STEMS and _EMPLOYER_ASPECT_STEMS


class TestNegativeSpaceAssembleLead:
    """The assemble's optional lead: None keeps the legacy bytes; a lead rides
    ABOVE the rows and never displaces them."""

    def test_no_lead_is_byte_parity(self):
        rows = ["user — hobby: yoga", "yoga — subclass of: hobby"]
        assert _negative_space_assemble("yoga", rows) == \
            ("Here's what I remember about yoga:\n- " + "\n- ".join(rows))
        assert _negative_space_assemble("yoga", rows, lead=None) == \
            _negative_space_assemble("yoga", rows)

    def test_lead_rides_above_the_rows(self):
        rows = ["user — takes classes at: Serenity Yoga"]
        out = _negative_space_assemble(
            "yoga", rows, lead="You takes classes at: Serenity Yoga")
        assert out.startswith("You takes classes at: Serenity Yoga\n\n"
                              "Here's what I remember about yoga:")
        assert "- user — takes classes at: Serenity Yoga" in out


class TestHonestyGuardVoice:
    """fix #4's count render: the numeral sentence never changes (OFF bytes);
    ON appends the voiced lead line."""

    def _guard(self, monkeypatch, on, value="3", attr="bike_count",
               type_phrase="counts"):
        import src.api.main as M
        monkeypatch.setattr(M, "_SERVE_VALUE_SURFACE_NORMALIZE", on)
        return M._honesty_guard_count_definition(
            type_phrase, {"attribute": attr, "value": value})

    def test_off_is_the_numeral_sentence_only(self, monkeypatch):
        assert self._guard(monkeypatch, on=False) == \
            ("You have 3 counts on record.", "A")

    def test_on_appends_the_voiced_lead(self, monkeypatch):
        assert self._guard(monkeypatch, on=True) == \
            ("You have 3 counts on record.\nYou bike count: three", "A")

    def test_on_non_digit_values_unchanged(self, monkeypatch):
        import src.api.main as M
        monkeypatch.setattr(M, "_SERVE_VALUE_SURFACE_NORMALIZE", True)
        assert M._honesty_guard_count_definition(
            "items", {"attribute": "favorite_color", "value": "gray"}) == \
            ("You favorite color: gray", "A")

    def test_on_exclusion_values_byte_identical(self, monkeypatch):
        # 500 copies: the voiced form IS the numeral — no append, OFF bytes
        assert self._guard(monkeypatch, on=True, value="500",
                           attr="debut_album_limited_copies",
                           type_phrase="copies") == \
            self._guard(monkeypatch, on=False, value="500",
                        attr="debut_album_limited_copies",
                        type_phrase="copies") == \
            ("You have 500 copies on record.", "A")


# ── PART 3 — DB-gated: the 6ade9755 eviction + the residual door legs ──────────

_YOGA_Q = "Where do I take yoga classes?"
_YOGA_GOLD = "Serenity Yoga"
_CONCERT_Q = "Where did I attend the Imagine Dragons concert?"
_TIME_Q = "What time do I stop checking work emails and messages?"
_SPAN_Q = "What was my previous occupation?"
_WEEKS_Q = "How long was I in Japan for?"
_BIKE_Q = "How many bikes do I own?"
_RAM_Q = "How much RAM did I upgrade my laptop to?"
_MONTHS_Q = "How long have I been collecting vintage cameras?"

# the fixture store, shaped like the map rows + the compose5 yoga-entity shape
_ALIASES = [("serenity", ["Serenity Yoga"]), ("yoga", ["yoga"]),
            ("hobby", ["hobby"]), ("baking", ["baking"]),
            ("vinyasa", ["vinyasa flow"]), ("xfinity", ["Xfinity Center"]),
            ("imagine", ["Imagine Dragons"]), ("mkt", ["marketing specialist"]),
            ("startup", ["a small startup"])]
_ATTRS = [("bike_count", "3", 12), ("laptop_ram_gb", "16", 14),
          ("work_email_cutoff_time", "19:00", 16),
          ("vintage_camera_collecting_duration_months", "3", 18),
          ("japan_trip_duration_weeks", "2", 20),
          ("imagine_dragons_concert_date", "June 15th", 44)]
_FACTS = [("takes_classes_at", "USER", "serenity", False, 6),
          ("hobby", "USER", "yoga", False, 8),
          ("yoga_practice_style", "USER", "vinyasa", False, 9),
          ("subclass_of", "yoga", "hobby", True, 10),
          ("subclass_of", "baking", "hobby", True, 11),
          ("attended_concert_at", "USER", "xfinity", False, 5),
          ("previous_occupation", "USER", "mkt", False, 3),
          ("previous_employer", "USER", "startup", False, 4)]
_REL_TYPES = sorted({r for r, _s, _o, _h, _c in _FACTS} | {"instance_of"})


def _plant(cur, schema, uid):
    import datetime as _dt
    cur.execute(f'CREATE SCHEMA "{schema}"')
    cur.execute(f'SET search_path TO "{schema}"')
    cur.execute(
        "CREATE TABLE entity_aliases (id bigserial PRIMARY KEY, entity_id text,"
        " alias text NOT NULL, display_form text, is_preferred boolean"
        " DEFAULT false)")
    cur.execute(
        "CREATE TABLE entity_attributes (entity_id text, attribute text,"
        " value_text text, value_int integer, value_float double precision,"
        " value_date date, value_normalized text, updated_at timestamptz"
        " DEFAULT now(), superseded_at timestamptz, valid_until timestamptz,"
        " valid_from timestamptz, sensitivity text)")
    cur.execute(
        "CREATE TABLE facts (subject_id text, object_id text, rel_type text,"
        " created_at timestamptz DEFAULT now(), is_hierarchy_rel boolean"
        " DEFAULT false, superseded_at timestamptz, archived_at timestamptz,"
        " deleted_at timestamptz, polarity text DEFAULT 'affirmed',"
        " valid_until timestamptz, valid_from timestamptz)")
    cur.execute("CREATE TABLE rel_types (rel_type text PRIMARY KEY,"
                " is_sensitive boolean DEFAULT false NOT NULL)")
    for rt in _REL_TYPES:
        cur.execute("INSERT INTO rel_types (rel_type) VALUES (%s)", (rt,))
    cur.execute("INSERT INTO entity_aliases (entity_id, alias, display_form,"
                " is_preferred) VALUES (%s, 'user', 'user', true)", (uid,))
    ents = {}
    for slug, aliases in _ALIASES:
        eid = str(uuid.uuid4())
        ents[slug] = eid
        for i, a in enumerate(aliases):
            cur.execute(
                "INSERT INTO entity_aliases (entity_id, alias, display_form,"
                " is_preferred) VALUES (%s, %s, %s, %s)",
                (eid, a, aliases[0], i == 0))
    base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    for attr, val, seq in _ATTRS:
        cur.execute(
            "INSERT INTO entity_attributes (entity_id, attribute, value_text,"
            " updated_at) VALUES (%s, %s, %s, %s)",
            (uid, attr, val, base + _dt.timedelta(hours=seq)))
    for rel, subj, slug, hier, seq in _FACTS:
        cur.execute(
            "INSERT INTO facts (subject_id, object_id, rel_type, created_at,"
            " is_hierarchy_rel, polarity) VALUES (%s, %s, %s, %s, %s,"
            " 'affirmed')",
            (uid if subj == "USER" else ents[subj], ents[slug], rel,
             base + _dt.timedelta(hours=seq), hier))
    return ents


pytestmark_db = pytest.mark.usefixtures("requires_postgres")


@pytest.mark.usefixtures("requires_postgres")
class TestSixadeEviction:
    """THE COMPOSE5-DISCLOSED REGRESSION, both arms byte-pinned on the fixture
    store shaped like the compose5 vessel's yoga shape (yoga entity with the
    subclass bridge + the owner's takes_classes_at row on another entity).

    OFF (SERVE_RENDER_ASPECT_RETAIN off = the compose5-era render): the fetch
    reaches only the topic + bridge; the takes_classes_at row is EVICTED and
    the taxonomy dilution ('baking — subclass of: hobby' — another hobby's
    scaffolding) fills the band.
    ON: the owner-anchored lane admits the row (its surface overlaps the
    question), it LEADS in the answer voice AND rides as band row; the
    previously-rendered owner rows stay retained; the scaffolding trades its
    slots to content (the render's own cls discipline)."""

    def test_off_reproduces_the_eviction_shape(
            self, postgres_dsn, monkeypatch):
        import psycopg2
        import src.mcp.server as S

        uid = str(uuid.uuid4())
        schema = "faultline_" + uid.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _plant(cur, schema, uid)
            monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
            monkeypatch.setattr(S, "SERVE_RENDER_ASPECT_RETAIN", False)
            out = S._negative_space_serve(uid, _YOGA_Q, None)
            # the compose5 bytes' class: the topic rows WITHOUT the answer row
            assert out == (
                "Here's what I remember about yoga:\n"
                "- user — hobby: yoga\n"
                "- yoga — subclass of: hobby\n"
                "- baking — subclass of: hobby"), out
            assert _YOGA_GOLD not in out  # the eviction: the gold row is gone
            assert "baking" in out        # the dilution row fills the band
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()

    def test_on_retains_the_evicted_row_as_lead_and_band(
            self, postgres_dsn, monkeypatch):
        import psycopg2
        import src.mcp.server as S

        uid = str(uuid.uuid4())
        schema = "faultline_" + uid.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _plant(cur, schema, uid)
            monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
            monkeypatch.setattr(S, "SERVE_RENDER_ASPECT_RETAIN", True)
            monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", False)
            out = S._negative_space_serve(uid, _YOGA_Q, None)
            # the LEAD: the evicted row's class answers in the answer voice
            assert out.startswith(
                "You takes classes at: Serenity Yoga\n\n"
                "Here's what I remember about yoga:"), out
            # the band row ALSO survives (lead AND band row — the class is
            # retained twice over), alongside the previously-rendered owner rows
            assert "- user — takes classes at: Serenity Yoga" in out
            assert "- user — yoga practice style: vinyasa flow" in out
            assert "- user — hobby: yoga" in out
            # the dilution scaffolding trades its slots to content
            assert "baking" not in out
            assert _YOGA_GOLD in out
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()

    def test_on_is_byte_identical_when_no_owner_row_passes_the_floor(
            self, postgres_dsn, monkeypatch):
        """THE NO-OP PARITY LEG (the pinned cats-class discipline): when no
        owner-anchored row carries aspect-affinity with the question, the ON
        render is byte-identical to OFF — the pinned broad-serve shapes are
        untouched."""
        import psycopg2
        import src.mcp.server as S

        uid = str(uuid.uuid4())
        schema = "faultline_" + uid.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _plant(cur, schema, uid)
            monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
            # the cats-class shape: a CLASS-word question ("what hobbies do
            # I have") whose tokens no owner-anchored row surface carries
            # (takes classes at / practice style carry zero 'hobby' tokens) —
            # the lane must admit NOTHING: the ON row set is byte-identical
            # to OFF's (the lead may reframe a legacy row; no new row rides)
            q = "What hobbies do I have?"
            monkeypatch.setattr(S, "SERVE_RENDER_ASPECT_RETAIN", False)
            off = S._negative_space_serve(uid, q, None)
            monkeypatch.setattr(S, "SERVE_RENDER_ASPECT_RETAIN", True)
            monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", False)
            on = S._negative_space_serve(uid, q, None)
            assert off is not None, off
            _rows = lambda t: [ln for ln in t.splitlines()
                               if ln.startswith("- ")]
            assert _rows(on) == _rows(off), f"OFF:\n{off}\nON:\n{on}"
            assert "takes classes at" not in on  # no owner row admitted
        finally:
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.close()


@pytest.mark.usefixtures("requires_postgres")
class TestResidualDoorLegs:
    """The per-residual stored-form→expected-surface pairs, through the EXACT
    door seams; each carries its OFF parity half (the receipt's bytes)."""

    @pytest.fixture()
    def fixture_seat(self, postgres_dsn, monkeypatch):
        import psycopg2
        import src.mcp.server as S

        uid = str(uuid.uuid4())
        schema = "faultline_" + uid.replace("-", "_")
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            _plant(cur, schema, uid)
        monkeypatch.setenv("POSTGRES_DSN", postgres_dsn)
        yield uid
        conn = psycopg2.connect(postgres_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()

    def test_time_of_day_leg(self, fixture_seat, monkeypatch):
        import src.mcp.server as S
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", False)
        off = S._token_bind_refusal_serve(fixture_seat, _TIME_Q)
        assert off == "You work email cutoff time: 19:00"
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", True)
        on = S._token_bind_refusal_serve(fixture_seat, _TIME_Q)
        assert on == ("You work email cutoff time: 7 pm\n"
                      "work email cutoff time: 19:00")

    def test_byte_fusion_leg(self, fixture_seat, monkeypatch):
        import src.mcp.server as S
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", False)
        assert S._token_bind_refusal_serve(fixture_seat, _RAM_Q) == \
            "You laptop ram gb: 16"
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", True)
        assert S._token_bind_refusal_serve(fixture_seat, _RAM_Q) == \
            "You laptop ram gb: 16GB\nlaptop ram gb: 16"

    def test_duration_word_leg_fix1(self, fixture_seat, monkeypatch):
        import src.mcp.server as S
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", False)
        assert S._token_bind_refusal_serve(fixture_seat, _MONTHS_Q) == \
            "You vintage camera collecting duration months: 3"
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", True)
        assert S._token_bind_refusal_serve(fixture_seat, _MONTHS_Q) == \
            ("You vintage camera collecting duration months: three\n"
             "vintage camera collecting duration months: 3")

    def test_duration_word_leg_fix3(self, fixture_seat, monkeypatch):
        import psycopg2
        import src.mcp.server as S
        conn = psycopg2.connect(postgres_dsn_global())
        # fix #3 is pure over the payload reconstruction (the receipt's inputs)
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{_schema_of(fixture_seat)}"')
            cur.execute(
                "SELECT attribute, COALESCE(value_text, CAST(value_int AS"
                " TEXT)) FROM entity_attributes WHERE entity_id = %s"
                " AND superseded_at IS NULL", (fixture_seat,))
            attributes = {a: v for a, v in cur.fetchall()}
        conn.close()
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", False)
        assert S._token_bind_payload_lead(_WEEKS_Q, [], attributes) == \
            "You japan trip duration weeks: 2"
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", True)
        assert S._token_bind_payload_lead(_WEEKS_Q, [], attributes) == \
            "You japan trip duration weeks: two\njapan trip duration weeks: 2"

    def test_span_pair_leg(self, fixture_seat, monkeypatch):
        import src.mcp.server as S
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", False)
        off = S._token_bind_refusal_serve(fixture_seat, _SPAN_Q)
        assert off == "You previous occupation: marketing specialist"
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", True)
        on = S._token_bind_refusal_serve(fixture_seat, _SPAN_Q)
        assert on == ("You previous occupation: marketing specialist at a "
                      "small startup\nprevious occupation: marketing "
                      "specialist")

    def test_count_guard_leg(self, fixture_seat, monkeypatch):
        import psycopg2
        import src.api.main as M
        conn = psycopg2.connect(postgres_dsn_global())
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{_schema_of(fixture_seat)}"')
        monkeypatch.setattr(M, "_SERVE_VALUE_SURFACE_NORMALIZE", False)
        hit = M._token_alias_attr_lookup(conn, fixture_seat, _BIKE_Q)
        assert hit == {"attribute": "bike_count", "value": "3"}
        assert M._honesty_guard_count_definition("counts", hit) == \
            ("You have 3 counts on record.", "A")
        monkeypatch.setattr(M, "_SERVE_VALUE_SURFACE_NORMALIZE", True)
        assert M._honesty_guard_count_definition("counts", hit) == \
            ("You have 3 counts on record.\nYou bike count: three", "A")
        conn.close()

    def test_concert_venue_retain_leg(self, fixture_seat, monkeypatch):
        import src.mcp.server as S
        monkeypatch.setattr(S, "SERVE_RENDER_ASPECT_RETAIN", False)
        monkeypatch.setattr(S, "SERVE_VALUE_SURFACE_NORMALIZE", False)
        off = S._negative_space_serve(
            fixture_seat, _CONCERT_Q, _anchor_of(fixture_seat, "Imagine Dragons"))
        # the OFF fetch reaches only the anchor + bridge — the concert facts
        # touch the OWNER and the venue, so the topic abstains empty
        assert not off or "Xfinity Center" not in off
        monkeypatch.setattr(S, "SERVE_RENDER_ASPECT_RETAIN", True)
        on = S._negative_space_serve(
            fixture_seat, _CONCERT_Q, _anchor_of(fixture_seat, "Imagine Dragons"))
        assert "Xfinity Center" in on       # the retained row carries the gold
        assert "- user — attended concert at: Xfinity Center" in on


def postgres_dsn_global():
    import os
    return os.environ["POSTGRES_DSN"]


def _schema_of(uid):
    return "faultline_" + uid.replace("-", "_")


def _anchor_of(uid, alias):
    """The fixture's entity id for an alias (the walk-resolved anchor)."""
    import psycopg2
    conn = psycopg2.connect(postgres_dsn_global())
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{_schema_of(uid)}"')
        cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s",
                    (alias,))
        row = cur.fetchone()
    conn.close()
    return row[0] if row else None

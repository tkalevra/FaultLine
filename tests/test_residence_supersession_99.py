"""#99 — a move never ended the previous residence.

Pre-prod smoke (saas after #89b, seat c3d62ac4): the seat carried toronto[now], hamilton[now],
regina[now], york[now] lives_in rows; after "This year I moved from Regina to Saskatoon." the answer
to "Where do I live?" rendered "You currently live in Regina … / You currently live in Saskatoon".

A rel whose metadata marks it single-valued (rel_types.is_single_valued, migration 168) holds one
CURRENT value per subject. A new current value supersedes the subject's previous current one; the
old row becomes history (never deleted) and renders "used to". A value dated EARLIER than the
current one does not supersede it.

E2E: every pin runs on a FRESH seat through the real harvest -> /ingest -> /query with the
production flags; the RENDERED answer is pinned at phrase level.
"""
import uuid

import pytest

from tests.test_smoke_ingest_35_36_37 import (  # noqa: F401 — e2e + the production-flag fixture
    _DSN, _spine, e2e, _state, _module_flags, _rows,
)


@pytest.fixture
def seat(e2e):
    """A fresh tenant on the module's running app (the shared e2e seat accumulates state)."""
    import psycopg2
    from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid
    M = e2e["M"]
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    conn = psycopg2.connect(_DSN)
    schema, status = create_user_schema(user_id, slug, conn)
    assert status == "ready", status
    with conn.cursor() as cur:
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES "
                    "(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                    "VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING", (user_id, schema))
    conn.commit()
    conn.close()
    M._TENANT_READY_CACHE.add(user_id)
    yield dict(e2e, user_id=user_id, schema=schema)
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    conn.close()


def _say(env, s):
    _state(env, s, [s])


def _render(env, q, rel):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return [(f.get("definition") or "") for f in (r.get("facts") or [])
            if (f.get("rel_type") or "") == rel]


def _line(lines, obj):
    hits = [x for x in lines if obj.lower() in x.lower()]
    assert len(hits) == 1, (obj, lines)
    return hits[0].lower()


def _one_current(lines, obj):
    cur = [x for x in lines if "currently" in x.lower()]
    assert len(cur) == 1, lines
    assert obj.lower() in cur[0].lower(), lines


def _rows_of(env, rel):
    """(object alias, temporal_status, event_date) of every live row of ``rel``, facts ∪ staged."""
    q = ("SELECT (SELECT min(a.alias) FROM entity_aliases a WHERE a.entity_id = f.object_id),"
         "       f.temporal_status, f.event_date::date FROM {t} f"
         " WHERE f.rel_type = %s AND f.deleted_at IS NULL {extra}")
    return (_rows(env["schema"], q.format(t="facts", extra="AND f.superseded_at IS NULL"), (rel,))
            + _rows(env["schema"], q.format(t="staged_facts", extra="AND f.promoted_at IS NULL"),
                    (rel,)))


def _store_one_current(env, rel, obj, history):
    """The STORE holds exactly one live 'now' value and keeps every former one as history."""
    rows = _rows_of(env, rel)
    now = [o for (o, t, _d) in rows if (t or "now") == "now"]
    assert now == [obj.lower()], rows
    for h in history:
        assert any(o == h.lower() and t == "past" for (o, t, _d) in rows), (h, rows)


def _check(env, current, history, rel="lives_in", q="Where do I live?"):
    lines = _render(env, q, rel)
    print("ROWS", _rows_of(env, rel), "LINES", lines)
    _one_current(lines, current)
    assert "used to" not in _line(lines, current), lines
    for h in history:
        assert "used to" in _line(lines, h), lines
        assert "currently" not in _line(lines, h), lines
    return lines


@_spine
def test_relocation_ends_the_named_residence(seat):
    _say(seat, "I live in Regina.")
    _say(seat, "I moved from Regina to Saskatoon.")
    _check(seat, "Saskatoon", ["Regina"])
    _store_one_current(seat, "lives_in", "Saskatoon", ["Regina"])


@_spine
def test_now_live_in_supersedes_the_previous(seat):
    _say(seat, "I live in Leeds.")
    _say(seat, "I now live in York.")
    _check(seat, "York", ["Leeds"])
    _store_one_current(seat, "lives_in", "York", ["Leeds"])


@_spine
def test_plain_live_in_supersedes_the_previous_and_a_restatement_returns(seat):
    _say(seat, "I live in Halifax.")
    _say(seat, "I live in Moncton.")
    _check(seat, "Moncton", ["Halifax"])
    _store_one_current(seat, "lives_in", "Moncton", ["Halifax"])
    _say(seat, "I live in Halifax.")          # moved back: the stored former is current again
    _check(seat, "Halifax", ["Moncton"])
    _store_one_current(seat, "lives_in", "Halifax", ["Moncton"])


@_spine
def test_dated_moves_in_chronological_order(seat):
    _say(seat, "I live in Toronto.")
    _say(seat, "In 2019 I moved from Toronto to Ottawa.")
    _say(seat, "In 2023 I moved from Ottawa to Montreal.")
    lines = _check(seat, "Montreal", ["Toronto", "Ottawa"])
    assert "2019" in _line(lines, "Ottawa"), lines          # the former keeps its date
    _store_one_current(seat, "lives_in", "Montreal", ["Toronto", "Ottawa"])


@_spine
def test_an_earlier_dated_move_does_not_supersede_a_later_residence(seat):
    _say(seat, "In 2023 I moved to Victoria.")
    _say(seat, "In 2015 I moved to Kelowna.")
    _check(seat, "Victoria", ["Kelowna"])
    _store_one_current(seat, "lives_in", "Victoria", ["Kelowna"])


@_spine
def test_a_seat_already_carrying_several_live_now_rows(seat):
    """Legacy seats (written before #99) hold several live 'now' rows. Ruling: exactly one is
    "currently" — the value newer than every other. Two DATED values order by date; otherwise
    the later-told wins (owner ruling 2026-10-01: a relocation told later ends the earlier
    residence whatever its date). Toronto (told first) < Victoria 2023 (told later) and
    Kelowna 2015 (told last) < Victoria by date → Victoria. Every other value renders
    "used to"."""
    _say(seat, "I live in Toronto.")
    _say(seat, "In 2023 I moved to Victoria.")
    _say(seat, "In 2015 I moved to Kelowna.")
    import psycopg2
    conn = psycopg2.connect(_DSN)
    with conn.cursor() as cur:
        cur.execute(f'UPDATE "{seat["schema"]}".facts SET temporal_status = %s'
                    " WHERE rel_type = %s", ("now", "lives_in"))
    conn.commit()
    conn.close()
    assert all(t == "now" for (_o, t, _d) in _rows_of(seat, "lives_in"))
    lines = _check(seat, "Victoria", ["Toronto", "Kelowna"])
    assert "2015" in _line(lines, "Kelowna"), lines
    assert "2023" in _line(lines, "Victoria"), lines


def _mark_single_valued(env, rel):
    import psycopg2
    from src.api import rel_type_overlay
    conn = psycopg2.connect(_DSN)
    with conn.cursor() as cur:
        cur.execute(f'UPDATE "{env["schema"]}".rel_types SET is_single_valued = true'
                    " WHERE rel_type = %s", (rel,))
        assert cur.rowcount == 1
    conn.commit()
    conn.close()
    rel_type_overlay.invalidate(env["schema"])


@_spine
def test_fresh_domain_any_rel_marked_single_valued(seat):
    # works_for is multi-valued in the seed; a tenant that marks it single-valued gets the same
    # supersession — the decision reads the rel's metadata, never a rel name.
    _mark_single_valued(seat, "works_for")
    _say(seat, "I work for Acme.")
    _say(seat, "I work for Globex.")
    _check(seat, "Globex", ["Acme"], rel="works_for", q="Who do I work for?")
    _store_one_current(seat, "works_for", "Globex", ["Acme"])


@_spine
def test_control_a_multi_valued_rel_keeps_every_value_current(seat):
    _say(seat, "I work for Acme.")
    _say(seat, "I work for Globex.")
    lines = _render(seat, "Who do I work for?", "works_for")
    print("ROWS", _rows_of(seat, "works_for"), "LINES", lines)
    assert not any("used to" in x.lower() for x in lines), lines
    assert sorted(o for (o, t, _d) in _rows_of(seat, "works_for") if t == "now") == \
        ["acme", "globex"]


# ─────────────────────────────── #101 / #102 (critic round 1) ───────────────────────────────

@_spine
def test_undated_restatement_of_a_dated_former_value_is_current_again(seat):
    # #101 R1: the restated value's stored valid-from is a stale 2015 statement; the incoming
    # assertion is undated, so assertion recency decides.
    _say(seat, "In 2015 I moved to Toronto.")
    _say(seat, "In 2020 I moved to Ottawa.")
    _say(seat, "I live in Toronto.")
    _check(seat, "Toronto", ["Ottawa"])
    _store_one_current(seat, "lives_in", "Toronto", ["Ottawa"])


@_spine
def test_a_relocation_told_later_ends_the_present_residence_whatever_its_date(seat):
    # OWNER RULING (2026-10-01): a relocation told later ends the earlier residence, whatever
    # its date — "In 2015 I moved to Kelowna" told after "I live in Toronto" makes Kelowna current.
    _say(seat, "I live in Toronto.")
    _say(seat, "In 2015 I moved to Kelowna.")
    _check(seat, "Kelowna", ["Toronto"])
    _store_one_current(seat, "lives_in", "Kelowna", ["Toronto"])


@_spine
def test_the_live_bug_moved_last_year_makes_the_move_current(seat):
    # the original live bug: "I live in Toronto" then "I moved to Vancouver last year"
    _say(seat, "I live in Toronto.")
    _say(seat, "I moved to Vancouver last year.")
    _check(seat, "Vancouver", ["Toronto"])
    _store_one_current(seat, "lives_in", "Vancouver", ["Toronto"])


@_spine
def test_a_plain_past_dated_state_never_supersedes_the_present_one(seat):
    # owner scope: a plain past-tense state ("I lived in Paris in 2015") is history, never a
    # rival for the current slot — it does not end "I live in Toronto".
    _say(seat, "I live in Toronto.")
    _say(seat, "I lived in Paris in 2015.")
    lines = _render(seat, "Where do I live?", "lives_in")
    print("ROWS", _rows_of(seat, "lives_in"), "LINES", lines)
    assert "used to" not in _line(lines, "Toronto"), lines
    assert "used to" in _line(lines, "Paris"), lines
    assert [o for (o, t, _d) in _rows_of(seat, "lives_in") if t == "now"] == ["toronto"]


@_spine
def test_an_address_is_lives_at_and_never_retires_the_residence(seat):
    # #102.1: "live AT <address>" keeps its preposition -> lives_at, its own slot.
    _say(seat, "I live in Kitchener.")
    _say(seat, "I live at 123 King Street.")
    lines = _render(seat, "Where do I live?", "lives_in")
    print("ROWS", _rows_of(seat, "lives_in"), "LINES", lines)
    assert "used to" not in _line(lines, "Kitchener"), lines
    assert [o for (o, t, _d) in _rows_of(seat, "lives_in") if t == "now"] == ["kitchener"]
    assert not any("king street" in (o or "") for (o, _t, _d) in _rows_of(seat, "lives_in"))
    # the address lands on its own seeded slot (lives_at, stored as the address scalar)
    assert ("lives_at", "123 king street") in _rows(
        seat["schema"], "SELECT attribute, value_text FROM entity_attributes"
                        " WHERE superseded_at IS NULL")


@_spine
def test_a_planned_move_is_not_the_current_residence(seat):
    # #102.2: a modal / futurate move is a planned residence, never the current slot.
    _say(seat, "I live in Kitchener.")
    _say(seat, "I will move to Berlin next year.")
    lines = _render(seat, "Where do I live?", "lives_in")
    print("ROWS", _rows_of(seat, "lives_in"), "LINES", lines)
    assert "used to" not in _line(lines, "Kitchener"), lines
    assert not any("currently" in x.lower() and "berlin" in x.lower() for x in lines), lines
    assert [o for (o, t, _d) in _rows_of(seat, "lives_in") if t == "now"] == ["kitchener"]


@_spine
def test_a_contained_granularity_coexists(seat):
    # _sv_objects_nested: Toronto located_in Ontario — restating the city never retires the
    # province it sits in.
    _say(seat, "I live in Toronto, Ontario.")
    _say(seat, "I live in Toronto.")
    rows = _rows_of(seat, "lives_in")
    print("ROWS", rows)
    assert sorted(o for (o, t, _d) in rows if t == "now") == ["ontario", "toronto"], rows


def _hook_seat_rows(env, values):
    """Insert live 'now' lives_in rows (object name, entity_type, created_at) for the seat."""
    import uuid as _uuid
    import psycopg2
    conn = psycopg2.connect(_DSN)
    ids = {}
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{env["schema"]}"')
        for (name, etype, created) in values:
            oid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, env["schema"] + name))
            cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, %s)"
                        " ON CONFLICT DO NOTHING", (oid, etype))
            cur.execute("INSERT INTO facts (subject_id, object_id, rel_type, temporal_status,"
                        " created_at) VALUES (%s, %s, 'lives_in', 'now', %s)",
                        (env["user_id"], oid, created))
            ids[name] = oid
    conn.commit()
    conn.close()
    return ids


def _run_hook(env, batch):
    import psycopg2
    from src.api import rel_type_overlay
    conn = psycopg2.connect(_DSN)
    tok = rel_type_overlay.set_current_schema(env["schema"])
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{env["schema"]}"')
        conn.commit()
        return env["M"]._retire_superseded_single_valued_states(conn, batch)
    finally:
        rel_type_overlay.reset_current_schema(tok)
        conn.close()


def _status_of(env, ids):
    st = dict(_rows(env["schema"], "SELECT object_id, temporal_status FROM facts"
                                   " WHERE rel_type = 'lives_in'"))
    return {n: st[i] for n, i in ids.items()}


def test_hook_values_co_asserted_in_one_turn_coexist(seat):
    # two single-valued values arriving in ONE request with no containment edge both stay now
    ids = _hook_seat_rows(seat, [("lagos", "Location", "2026-01-01"),
                                 ("accra", "Location", "2026-01-01")])
    _run_hook(seat, [(seat["user_id"], ids["lagos"], "lives_in", "now", None, "affirmed"),
                     (seat["user_id"], ids["accra"], "lives_in", "now", None, "affirmed")])
    assert _status_of(seat, ids) == {"lagos": "now", "accra": "now"}


def test_hook_a_different_type_class_is_not_a_rival(seat):
    # stopgap (#102): values of different entity types never retire each other
    ids = _hook_seat_rows(seat, [("quito", "Location", "2026-01-01"),
                                 ("unit 4b", "Object", "2026-02-01")])
    _run_hook(seat, [(seat["user_id"], ids["unit 4b"], "lives_in", "now", None, "affirmed")])
    assert _status_of(seat, ids) == {"quito": "now", "unit 4b": "now"}
    # control: a new value of the SAME type class does supersede
    ids2 = _hook_seat_rows(seat, [("cuenca", "Location", "2026-03-01")])
    _run_hook(seat, [(seat["user_id"], ids2["cuenca"], "lives_in", "now", None, "affirmed")])
    assert _status_of(seat, ids)["quito"] == "past"


def test_render_collapse_orders_two_dated_values_told_together_by_date():
    # _recency_collapse_single_valued_state: same created_at and id (indeterminate telling
    # order) — only the dates order them, and the later date is current.
    import src.api.main as M
    meta = {"is_single_valued": True, "temporal_class": "state"}
    facts = [
        {"rel_type": "lives_in", "subject": "u", "object": "a", "temporal_status": "now",
         "event_date": "2023-01-01T00:00:00", "created_at": "2026-01-01", "_ingest_id": 1},
        {"rel_type": "lives_in", "subject": "u", "object": "b", "temporal_status": "now",
         "event_date": "2015-01-01T00:00:00", "created_at": "2026-01-01", "_ingest_id": 1},
    ]
    M._recency_collapse_single_valued_state(facts, lambda _r: meta)
    assert [f["temporal_status"] for f in facts] == ["now", "past"]

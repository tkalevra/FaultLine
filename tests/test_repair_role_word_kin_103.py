"""#103 — retire pre-fix role-word kin junk ((dad, parent_of, user), (user, sibling_of, brother)).

The ingest fix stops new rows; ``junk_repair.retire_role_word_kin_junk`` retires the old ones and
``census_role_word_kin_junk_all`` counts them read-only. The operator SQL in scripts/repairs/ is
rendered from the SAME template; this file pins helper ⇔ SQL parity by running both.

Runs on a throwaway ``*_test`` POSTGRES_DSN tenant (skipped, disclosed, without one). Plants rows
directly — this is a data-repair pin, not an ingest pin, so no ingest flags are involved.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _dsn() -> str:
    return os.environ.get("POSTGRES_DSN", "")


def _new_seat():
    from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid
    user_id = str(uuid.uuid4())
    conn = psycopg2.connect(_dsn())
    try:
        schema, status = create_user_schema(user_id, derive_user_slug_from_uuid(user_id), conn)
        conn.commit()
    finally:
        conn.close()
    assert status == "ready", status
    return user_id, schema


def _drop(schema):
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{schema[len("faultline_"):]}" CASCADE')
    conn.close()


def _q(schema, sql, params=()):
    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []
    finally:
        conn.commit()
        conn.close()


def _plant(schema, seat):
    """The issue's two shapes, three controls, and the fresh-domain cases. Returns name→entity id."""
    ids = {}

    def ent(name, *aliases, etype="Person"):
        eid = str(uuid.uuid4())
        ids[name] = eid
        _q(schema, "INSERT INTO entities (id, entity_type) VALUES (%s, %s)", (eid, etype))
        for a in aliases or (name,):
            _q(schema, "INSERT INTO entity_aliases (entity_id, alias, is_preferred, "
                       "preference_source) VALUES (%s, %s, false, 'rel_default')", (eid, a))
        return eid

    def fact(s, rel, o, cls="B", prov="llm_inferred"):
        return _q(schema, "INSERT INTO facts (subject_id, object_id, rel_type, fact_class, "
                          "fact_provenance, provenance) VALUES (%s,%s,%s,%s,%s,'unattested') "
                          "RETURNING id", (s, o, rel, cls, prov))[0][0]

    def staged(s, rel, o, cls="B", prov="llm_inferred"):
        return _q(schema, "INSERT INTO staged_facts (subject_id, object_id, rel_type, fact_class, "
                          "fact_provenance, provenance) VALUES (%s,%s,%s,%s,%s,'unattested') "
                          "RETURNING id", (s, o, rel, cls, prov))[0][0]

    rows = {}
    # (a) the issue's dad: a person minted from the bare role word, staged B
    rows["dad"] = ("staged_facts", staged(ent("dad"), "parent_of", seat))
    # (b) the issue's brother: the instance_of TYPE node bound as the user's sibling, facts B
    ent("brother", etype="Concept")
    fact(ent("pablo"), "instance_of", ids["brother"], cls="A", prov="user_stated")
    rows["brother"] = ("facts", fact(seat, "sibling_of", ids["brother"]))
    # CONTROL (prod seat 591d25af shape): a real NAMED child that a stray instance_of made a
    # "type node" — a name is never a role word, so it must never be retired
    rows["lucas"] = ("facts", fact(seat, "parent_of", ent("lucas")))
    fact(ent("kid"), "instance_of", ids["lucas"])
    # CONTROL (prod seat 3f9c21ae shape): a product type node on a social rel — not this class
    rows["portainer"] = ("facts", fact(seat, "knows", ent("portainer", etype="Concept")))
    fact(ent("stack55"), "instance_of", ids["portainer"])
    # CONTROL: a real NAMED mother (unattested, but a name is never a role word)
    rows["diane"] = ("facts", fact(ent("diane"), "parent_of", seat))
    # CONTROL: Class-A user_stated "my dad is Robert" — and a role-only entity stated by the user
    rows["robert"] = ("facts", fact(ent("robert"), "parent_of", seat, cls="A", prov="user_stated"))
    rows["father"] = ("facts", fact(ent("father"), "parent_of", seat, cls="A", prov="user_stated"))
    # CONTROL: a role-word entity that HAS other facts — the user's real, unnamed relative
    rows["mom"] = ("staged_facts", staged(ent("mom"), "parent_of", seat))
    fact(ids["mom"], "lives_in", ent("toronto", etype="Location"), cls="A", prov="user_stated")
    # FRESH DOMAIN: the social_role class (a different cue class, a different rel)
    rows["roommate"] = ("facts", fact(seat, "knows", ent("roommate")))
    # FRESH DOMAIN: a cue the TENANT grew — no code knows the word 'stepdad'
    _q(schema, "INSERT INTO linguistic_cues (cue, category, description, source) "
               "VALUES ('stepdad', 'kinship_noun', 'parent_of', 'test_grown') "
               "ON CONFLICT (cue, category) DO NOTHING")
    rows["stepdad"] = ("staged_facts", staged(ent("stepdad"), "parent_of", seat, cls="C"))
    # CONTROL: a bare word in NO cue class and on no ladder
    rows["plumber"] = ("facts", fact(seat, "knows", ent("plumber")))
    # CONTROL: the kin class maps uncle → related_to, an unconstrained {ANY} rel: out of scope
    rows["uncle"] = ("facts", fact(seat, "related_to", ent("uncle")))
    # CONTROL: a cue the tenant RETIRED (is_active=false) is not a role word any more
    _q(schema, "UPDATE linguistic_cues SET is_active = false WHERE cue = 'classmate' "
               "AND category = 'social_role'")
    rows["classmate"] = ("facts", fact(seat, "knows", ent("classmate")))
    return ids, rows


RETIRED = {"dad", "brother", "roommate", "stepdad"}
ALIAS_DELETED = {"dad", "roommate", "stepdad"}
UNTOUCHED = {"diane", "robert", "father", "mom", "plumber", "uncle", "classmate",
             "lucas", "portainer"}


def _live(schema, table, fid):
    col = "superseded_at" if table == "facts" else "deleted_at"
    return _q(schema, f"SELECT {col} IS NULL FROM {table} WHERE id = %s", (fid,))[0][0]


@pytest.fixture(scope="module")
def seat_a():
    d = _dsn()
    if not d or not d.rsplit("/", 1)[-1].endswith("_test"):
        pytest.skip("needs a throwaway *_test POSTGRES_DSN (skipped — disclosed)")
    os.environ.setdefault("FAULTLINE_MIGRATIONS_DIR", os.path.join(ROOT, "migrations"))
    user_id, schema = _new_seat()
    ids, rows = _plant(schema, user_id)
    yield {"seat": user_id, "schema": schema, "ids": ids, "rows": rows}
    _drop(schema)


def test_dry_run_lists_exactly_the_junk_and_writes_nothing(seat_a):
    from src.api import junk_repair
    s, rows, ids = seat_a["schema"], seat_a["rows"], seat_a["ids"]
    conn = psycopg2.connect(_dsn())
    try:
        cands = junk_repair.retire_role_word_kin_junk(conn, s, apply=False)
        conn.rollback()
    finally:
        conn.close()
    got_rows = {(c["table"], c["id"]) for c in cands if c["kind"] == "row"}
    assert got_rows == {rows[n] for n in RETIRED}, cands
    by_id = {(c["table"], c["id"]): c for c in cands if c["kind"] == "row"}
    dad = by_id[rows["dad"]]
    assert dad["rel_type"] == "parent_of" and dad["subject_aliases"] == "dad"
    assert dad["fact_class"] == "B" and dad["endpoint"] == "role_word_entity"
    bro = by_id[rows["brother"]]
    assert bro["object_aliases"] == "brother" and bro["endpoint"] == "type_node"
    assert bro["subject_id"] == seat_a["seat"]
    aliases = {c["alias"] for c in cands if c["kind"] == "alias"}
    assert aliases == ALIAS_DELETED, cands
    assert all(c["entity_id"] in {ids[n] for n in ALIAS_DELETED}
               for c in cands if c["kind"] == "alias")
    for name in RETIRED | UNTOUCHED:
        assert _live(s, *rows[name]), f"dry run wrote to {name}"


def test_census_all_is_read_only_and_counts(seat_a):
    from src.api import junk_repair
    conn = psycopg2.connect(_dsn())
    try:
        out = junk_repair.census_role_word_kin_junk_all(conn)
    finally:
        conn.close()
    mine = [r for r in out if r["schema"] == seat_a["schema"]]
    assert len(mine) == 1, out
    m = mine[0]
    assert (m["facts"], m["staged_facts"], m["aliases"]) == (2, 2, 3), m
    assert (m["role_word_entity"], m["type_node"]) == (3, 1), m
    assert m["sample"], m
    for name in RETIRED:
        assert _live(seat_a["schema"], *seat_a["rows"][name]), "census wrote"


def test_apply_retires_the_junk_keeps_the_controls_and_is_idempotent(seat_a):
    from src.api import junk_repair
    s, rows, ids = seat_a["schema"], seat_a["rows"], seat_a["ids"]
    conn = psycopg2.connect(_dsn())
    try:
        junk_repair.retire_role_word_kin_junk(conn, s, apply=True)
        conn.commit()
        again = junk_repair.retire_role_word_kin_junk(conn, s, apply=True)
        conn.commit()
    finally:
        conn.close()
    assert again == [], f"second apply changed rows: {again}"
    for name in RETIRED:
        assert not _live(s, *rows[name]), f"{name} still live"
        # soft retirement, never a hard delete
        assert _q(s, f"SELECT count(*) FROM {rows[name][0]} WHERE id = %s", (rows[name][1],))[0][0] == 1
    for name in UNTOUCHED:
        assert _live(s, *rows[name]), f"control {name} was retired"
    left = {a for (a,) in _q(s, "SELECT alias FROM entity_aliases WHERE entity_id = ANY(%s)",
                             ([ids[n] for n in ids],))}
    assert not (left & ALIAS_DELETED), left
    assert {"brother", "mom", "diane", "robert", "father", "pablo"} <= left, left
    # the issue's render: the seat holds no live parent_of/sibling_of to a bare role word
    assert _q(s, "SELECT count(*) FROM staged_facts WHERE id = %s AND deleted_at IS NULL",
              (rows["dad"][1],))[0][0] == 0


def test_refuses_non_tenant_and_seatless_schema(seat_a):
    from src.api import junk_repair
    conn = psycopg2.connect(_dsn())
    try:
        with pytest.raises(ValueError):
            junk_repair.retire_role_word_kin_junk(conn, "public")
        with pytest.raises(ValueError):
            junk_repair.retire_role_word_kin_junk(conn, "faultline_x; drop table facts")
    finally:
        conn.close()


# ── operator SQL: rendered from the same template, and behaviourally identical ───────────────────

def _render():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "render_repair103", os.path.join(ROOT, "scripts", "repairs", "render_repair103.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_sql_matches_the_helper_template(tmp_path):
    mod = _render()
    fresh = mod.render(str(tmp_path))
    from src.api.junk_repair import ROLE_WORD_KIN_CANDIDATES_SQL
    for name, text in fresh.items():
        with open(os.path.join(ROOT, "scripts", "repairs", name), encoding="utf-8") as fh:
            assert fh.read() == text, f"{name} drifted — re-run scripts/repairs/render_repair103.py"
        assert ROLE_WORD_KIN_CANDIDATES_SQL in text


def _psql(*args, stdin):
    return subprocess.run(["psql", _dsn(), "-X", "-q", *args], input=stdin, text=True,
                          capture_output=True, timeout=120)


def test_sql_census_and_apply_match_the_helper(seat_a):
    if not shutil.which("psql"):
        pytest.skip("psql not on PATH (skipped — disclosed)")
    from src.api import junk_repair
    user_id, schema = _new_seat()
    try:
        _ids, rows = _plant(schema, user_id)
        conn = psycopg2.connect(_dsn())
        try:
            census = junk_repair.census_role_word_kin_junk_all(conn)
        finally:
            conn.close()
        exp_rows = sum(r.get("facts", 0) + r.get("staged_facts", 0) for r in census)
        exp_als = sum(r.get("aliases", 0) for r in census)
        sql_dir = os.path.join(ROOT, "scripts", "repairs")
        census_sql = open(os.path.join(sql_dir, "repair103_census.sql")).read()
        apply_sql = open(os.path.join(sql_dir, "repair103_apply.sql")).read()

        out = _psql(stdin=census_sql)
        assert out.returncode == 0, out.stderr
        assert f"rows={exp_rows} aliases={exp_als}" in out.stderr, out.stderr
        assert _live(schema, *rows["dad"]), "census SQL wrote"

        refused = _psql(stdin=apply_sql)  # no expected counts → refuses, writes nothing
        assert "refusing" in (refused.stdout + refused.stderr)
        assert _live(schema, *rows["dad"])

        wrong = _psql("-v", f"expected_rows={exp_rows + 1}", "-v", f"expected_aliases={exp_als}",
                      stdin=apply_sql)
        assert wrong.returncode != 0 and "repair103 ABORT" in wrong.stderr, wrong.stderr
        assert _live(schema, *rows["dad"]), "a failed count guard must roll everything back"

        ok = _psql("-v", f"expected_rows={exp_rows}", "-v", f"expected_aliases={exp_als}",
                   stdin=apply_sql)
        assert ok.returncode == 0, ok.stderr
        for name in RETIRED:
            assert not _live(schema, *rows[name]), name
        for name in UNTOUCHED:
            assert _live(schema, *rows[name]), name
        conn = psycopg2.connect(_dsn())
        try:
            assert junk_repair.retire_role_word_kin_junk(conn, schema) == []
            assert junk_repair.retire_role_word_kin_junk(conn, seat_a["schema"]) == []
        finally:
            conn.close()
        logged = _q(schema, "SELECT count(*) FROM fl_repair_audit.repair103_log "
                            "WHERE schema_name = %s", (schema,))[0][0]
        assert logged == len(RETIRED) + len(ALIAS_DELETED)

        again = _psql("-v", "expected_rows=0", "-v", "expected_aliases=0", stdin=apply_sql)
        assert again.returncode == 0, again.stderr
    finally:
        _drop(schema)
        conn = psycopg2.connect(_dsn())
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS fl_repair_audit CASCADE")
        conn.close()

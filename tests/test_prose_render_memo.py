"""Pins: PROSE_RENDER_MEMO (label_fallback flood lane, 2026-11-14).

THE WOUND (critic BLOCKER, pre-prod 18:50-19:20Z window + /tmp/slpv2_passA2.txt):
a planted synthetic hierarchy lattice (300 class nodes, 1800 subclass edges, ~251
rel_types with NO natural_language templates) on a heavy seat wedged a uvicorn worker
at 127% CPU for 38+ MINUTES inside a ``convert_to_prose.label_fallback`` flood (~2,500
debug lines/min, the same 3 rel_types repeating). Root cause (local repro,
/tmp/lf_repro.py methodology, committed here as the DB-gated pin):
  • the downward expansion returns the SAME member set ON and OFF
    WALK_QUERY_BATCHING (sets byte-equal) — the batching train did NOT change what
    reaches the render; the flood is a PRE-EXISTING per-fact render defect the
    lattice exposed at scale;
  • every template-miss fact pays ~3 UNBATCHED single-row SELECTs
    (resolve_entity_display ×2 slots + the named-instance instance_of fallback)
    — measured 17,037 statements for 5,677 facts, 2.86s of 4.06s on a LOCAL UNIX
    SOCKET — plus one debug line PER FACT for the same handful of rel_types.

THE FIX (flag PROSE_RENDER_MEMO, default ON; OFF = byte-for-byte legacy):
per-RENDER memos — display-name per entity, instance_of fallback per object,
label_fallback debug line per rel_type. All three helpers are pure in their key
within one render, so outputs are identical BY CONSTRUCTION (pinned byte-equal on
the lattice, ON vs OFF).

Pins here:
  A. flag default ON;
  B. hermetic statement collapse (memo ON: alias lookups == DISTINCT entities;
     OFF: == 2 × facts) and the label_fallback debug flood bound (ON: one line per
     rel_type; OFF: one line per fact);
  C. hermetic byte-parity ON vs OFF — template-missing AND template-bearing
     renders (no regression on template-bearing output);
  D. DB-gated repro pin (requires_postgres): scaled lattice — expansion-set
     equality ON/OFF (the old-vs-new discriminator), render wall-time CEILING with
     the memo ON, flood bounded, and ON/OFF prose byte-equality.
"""
from __future__ import annotations

import os
import random
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import src.api.main as M  # noqa: E402
import src.api.rel_type_overlay as RTO  # noqa: E402


# ── shared fakes ──────────────────────────────────────────────────────────────

class _Stmt:
    def __init__(self, sql, params=None):
        self.sql = " ".join(sql.lower().split())
        self.params = params


class _FakeCursor:
    """Records statements; answers the render's single-row lookups."""

    def __init__(self, aliases, log):
        self._aliases = aliases        # entity_id → preferred alias
        self._log = log
        self._result: list = []

    def execute(self, sql, params=None):
        self._log.append(_Stmt(sql, params))
        s = " ".join(sql.lower().split())
        if s.startswith("select alias, display_form from entity_aliases"):
            self._result = [(self._aliases.get(str(params[0])), None)]
        elif s.startswith("select object_id from facts where subject_id::text"):
            self._result = []          # no instance_of rows (template-miss lattice)
        elif "same_as" in s:
            self._result = []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, aliases, log):
        self._aliases = aliases
        self._log = log

    def cursor(self):
        return _FakeCursor(self._aliases, self._log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _LogShim:
    """Counts log events by event name without touching output."""

    def __init__(self, inner):
        self._inner = inner
        self.counts: dict = {}

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in ("debug", "info", "warning", "error", "critical"):
            def wrapper(event=None, **kw):
                self.counts[event] = self.counts.get(event, 0) + 1
                try:
                    return attr(event, **kw)
                except Exception:  # noqa: BLE001 — counting shim must never break a log
                    return None
            return wrapper
        return attr


def _mkfact(s, o, r):
    return {"subject": s, "object": o, "rel_type": r, "confidence": 0.9,
            "fact_class": "A", "source": "db", "fact_provenance": "user_stated",
            "temporal_status": "now", "event_date": None, "polarity": "affirmed",
            "provenance": "user_stated"}


@pytest.fixture
def render_env(monkeypatch):
    """Template-less overlay + counting log; returns (log, conn-factory)."""
    monkeypatch.setattr(M.rel_type_overlay, "resolve_current", lambda dsn: {})
    monkeypatch.setattr(RTO, "resolve_current", lambda dsn: {})
    shim = _LogShim(M.log)
    monkeypatch.setattr(M, "log", shim)
    def make_conn(aliases):
        log: list = []
        return _FakeConn(aliases, log), log
    return shim, make_conn


# ── A. flag default ───────────────────────────────────────────────────────────

def test_flag_default_on():
    assert M.PROSE_RENDER_MEMO is True, "PROSE_RENDER_MEMO must default ON"


# ── B/C. hermetic statement collapse + byte-parity ────────────────────────────

def test_memo_collapses_alias_lookups_to_distinct_entities(render_env, monkeypatch):
    """60 template-miss facts over 6 entities ⇒ with the memo ON the alias probe
    runs once per DISTINCT entity; OFF it runs per SLOT (2 × facts)."""
    shim, make_conn = render_env
    ids = [str(uuid.uuid4()) for _ in range(6)]
    aliases = {i: f"thing {n}" for n, i in enumerate(ids)}
    facts = [_mkfact(ids[i % 6], ids[(i + 1) % 6], "r_flood_a") for i in range(60)]

    conn_on, log_on = make_conn(aliases)
    monkeypatch.setattr(M, "PROSE_RENDER_MEMO", True)
    shim.counts.clear()
    M.convert_to_prose([dict(f) for f in facts], conn_on)
    alias_on = [s for s in log_on
                if s.sql.startswith("select alias, display_form from entity_aliases")]
    assert len(alias_on) == 6, (
        f"memo ON: one alias read per DISTINCT entity, got {len(alias_on)}")
    assert shim.counts.get("convert_to_prose.label_fallback", 0) == 1, (
        "memo ON: label_fallback debug line fires ONCE per rel_type per render")

    conn_off, log_off = make_conn(aliases)
    monkeypatch.setattr(M, "PROSE_RENDER_MEMO", False)
    shim.counts.clear()
    M.convert_to_prose([dict(f) for f in facts], conn_off)
    alias_off = [s for s in log_off
                 if s.sql.startswith("select alias, display_form from entity_aliases")]
    assert len(alias_off) == 2 * len(facts), (
        f"memo OFF (byte-parity): one alias read per slot, expected "
        f"{2 * len(facts)}, got {len(alias_off)}")
    assert shim.counts.get("convert_to_prose.label_fallback", 0) == len(facts), (
        "memo OFF (byte-parity): label_fallback fires per fact, exactly as legacy")


def test_memo_prose_byte_parity_on_vs_off(render_env, monkeypatch):
    """The render's OUTPUT must be byte-identical with the memo ON and OFF —
    template-missing rels AND a rel with a real template (no regression on
    template-bearing renders)."""
    shim, make_conn = render_env
    ids = [str(uuid.uuid4()) for _ in range(5)]
    aliases = {i: f"thing {n}" for n, i in enumerate(ids)}
    facts = ([_mkfact(ids[i % 5], ids[(i + 1) % 5], "r_miss") for i in range(24)]
             + [_mkfact(ids[0], ids[1], "has_widget") for _ in range(6)])
    # second leg: has_widget carries a REAL template
    real_meta = {"has_widget": {"natural_language": "X has a widget called Y",
                                "natural_language_2p": None, "label": "has widget",
                                "tail_types": [], "is_hierarchy_rel": False}}
    results = {}
    for templ in ({}, real_meta):
        monkeypatch.setattr(M.rel_type_overlay, "resolve_current", lambda dsn, t=templ: dict(t))
        for flag in (True, False):
            monkeypatch.setattr(M, "PROSE_RENDER_MEMO", flag)
            conn, _ = make_conn(aliases)
            results[(bool(templ), flag)] = M.convert_to_prose(
                [dict(f) for f in facts], conn)
    assert results[(False, True)] == results[(False, False)], (
        "template-missing render must be byte-identical ON vs OFF")
    assert results[(True, True)] == results[(True, False)], (
        "template-bearing render must be byte-identical ON vs OFF (no regression)")
    templ_prose = results[(True, True)]
    assert any("widget called" in p for p in templ_prose), (
        "the real template must actually render (guard against a stubbed overlay)")


# ── D. DB-gated lattice repro pin ──────────────────────────────────────────────

_N_CLASSES = 150
_N_EDGES = 900
_N_SYN = 1500


def _build_lattice(cur, schema, rng):
    cur.execute(f"CREATE SCHEMA {schema}")
    cur.execute(f"SET search_path TO {schema}")
    cur.execute("""
        CREATE TABLE rel_types (rel_type TEXT PRIMARY KEY, category TEXT,
          is_hierarchy_rel BOOLEAN, label TEXT, natural_language TEXT,
          natural_language_2p TEXT, source TEXT)""")
    cur.execute("""
        CREATE TABLE facts (subject_id TEXT, object_id TEXT, rel_type TEXT,
          superseded_at TIMESTAMPTZ, archived_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ,
          valid_until TIMESTAMPTZ, provenance TEXT, confidence REAL,
          confirmed_count INT, fact_class TEXT, fact_provenance TEXT,
          temporal_status TEXT, event_date DATE, polarity TEXT)""")
    cur.execute("""
        CREATE TABLE staged_facts (subject_id TEXT, object_id TEXT, rel_type TEXT,
          deleted_at TIMESTAMPTZ, expires_at TIMESTAMPTZ, promoted_at TIMESTAMPTZ,
          provenance TEXT, confidence REAL, confirmed_count INT, fact_class TEXT,
          fact_provenance TEXT, temporal_status TEXT, event_date DATE, polarity TEXT)""")
    cur.execute("""CREATE TABLE linguistic_cues (cue TEXT, description TEXT,
          category TEXT, is_active BOOLEAN, mapping TEXT)""")
    cur.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, entity_type TEXT)")
    cur.execute("""CREATE TABLE entity_aliases (entity_id TEXT, alias TEXT,
          is_preferred BOOLEAN, display_form TEXT, preference_source TEXT)""")
    cur.execute("""CREATE TABLE entity_taxonomies (entity_id TEXT, taxonomy TEXT,
          taxonomy_name TEXT, root_entity_id TEXT)""")
    cur.execute("""CREATE TABLE entity_attributes (entity_id TEXT, attribute TEXT,
          value_text TEXT, value_int INT, value_float REAL, value_date DATE,
          provenance TEXT, superseded_at TIMESTAMPTZ)""")
    hier = ["subclass_of", "instance_of", "member_of"]
    rows = [(r, None, True) for r in hier] + [
        (f"r_syn_{i:03d}", "engine", False) for i in range(248)]
    cur.executemany(
        "INSERT INTO rel_types (rel_type, category, is_hierarchy_rel, source)"
        " VALUES (%s,%s,%s,'engine')", rows)
    user = str(uuid.uuid4()); gadget = str(uuid.uuid4())
    classes = [str(uuid.uuid4()) for _ in range(_N_CLASSES)]
    cur.executemany("INSERT INTO entities VALUES (%s,%s)",
                    [(user, "Person"), (gadget, "Object")]
                    + [(c, "Concept") for c in classes])
    cur.executemany(
        "INSERT INTO entity_aliases (entity_id, alias, is_preferred) VALUES (%s,%s,true)",
        [(user, "Alex"), (gadget, "gadget")]
        + [(c, f"class {i:03d}") for i, c in enumerate(classes)])
    cur.executemany(
        "INSERT INTO facts (subject_id, object_id, rel_type, confidence, fact_class,"
        " fact_provenance) VALUES (%s,%s,%s,0.9,'A','user_stated')",
        [(user, gadget, "r_syn_000"), (gadget, classes[0], "instance_of")])
    edges = set()
    while len(edges) < _N_EDGES:
        a, b = rng.randrange(_N_CLASSES), rng.randrange(_N_CLASSES)
        if a != b:
            edges.add((a, b))
    cur.executemany(
        "INSERT INTO facts (subject_id, object_id, rel_type, confidence, fact_class,"
        " fact_provenance) VALUES (%s,%s,'subclass_of',0.9,'A','user_stated')",
        [(classes[a], classes[b]) for a, b in edges])
    syn = []
    while len(syn) < _N_SYN:
        a, b = rng.randrange(_N_CLASSES), rng.randrange(_N_CLASSES)
        if a != b:
            syn.append((classes[a], classes[b], f"r_syn_{rng.randrange(3):03d}"))
    cur.executemany(
        "INSERT INTO facts (subject_id, object_id, rel_type, confidence, fact_class,"
        " fact_provenance) VALUES (%s,%s,%s,0.9,'A','user_stated')", syn)
    return user, gadget


@pytest.mark.usefixtures("requires_postgres")
def test_lattice_repro_bounded_and_parity(postgres_dsn, monkeypatch):
    """The BLOCKER repro, scaled to test size:
      1. expansion-set equality ON vs OFF WALK_QUERY_BATCHING (old-vs-new
         discriminator — the batching lever does NOT change the walked set);
      2. with PROSE_RENDER_MEMO ON the render is BOUNDED: wall-time ceiling and
         label_fallback bounded to one line per rel_type (no flood);
      3. ON vs OFF render prose is BYTE-IDENTICAL.
    """
    import psycopg2 as real_psycopg2
    rng = random.Random(0xF10F1)
    schema = f"fl_prm_pin_{uuid.uuid4().hex[:8]}"
    conn = real_psycopg2.connect(postgres_dsn, connect_timeout=5)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            user, gadget = _build_lattice(cur, schema, rng)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT subject_id, object_id, rel_type FROM facts"
                " WHERE subject_id = %s AND rel_type = 'instance_of'", (gadget,))
            row = cur.fetchone()
        assert row, "anchor bootstrap missing"
        class_uuids = {row[1]}
        # 1. expansion-set equality ON vs OFF
        sets = {}
        for flag in (True, False):
            monkeypatch.setattr(M, "WALK_QUERY_BATCHING", flag)
            members: set = set()
            if flag:
                batches = M._hierarchy_expand_batch(conn, user, class_uuids, "down", 3)
                for c in class_uuids:
                    members.update((batches.get(c) or {c}) - {c})
            else:
                for c in class_uuids:
                    members.update(M._hierarchy_expand(conn, user, c, "down", 3) - {c})
            sets[flag] = members
        assert sets[True] == sets[False], (
            "old-vs-new discriminator: batched and legacy expansion must return "
            "the SAME member set on the lattice")
        assert len(sets[True]) > 20, "the lattice must actually fan out for the pin"

        # fetch the walk's fact set (the flood's input)
        hier_f = M._fetch_hierarchy_facts(conn, user, sets[True])
        ent_f = M._fetch_entity_facts(conn, user, sets[True], anchor_uuid=None)
        hier_f = [f for f in hier_f
                  if not M._is_grown_ontology_row(f.get("rel_type"), f.get("provenance"))]
        ent_f = [f for f in ent_f
                 if not M._is_grown_ontology_row(f.get("rel_type"), f.get("provenance"))]
        facts = hier_f + ent_f
        assert len(facts) > 500, (
            f"the lattice walk must pull a large fact set, got {len(facts)}")

        shim = _LogShim(M.log)
        monkeypatch.setattr(M, "log", shim)
        monkeypatch.setattr(M.rel_type_overlay, "resolve_current", lambda dsn: {})
        monkeypatch.setattr(RTO, "resolve_current", lambda dsn: {})
        tok = RTO.set_current_schema(schema)
        try:
            monkeypatch.setattr(M, "PROSE_RENDER_MEMO", True)
            shim.counts.clear()
            t0 = time.time()
            prose_on = M.convert_to_prose([dict(f) for f in facts], conn,
                                          anchor=user, user_id=user)
            dt_on = time.time() - t0
            fb_on = shim.counts.get("convert_to_prose.label_fallback", 0)

            monkeypatch.setattr(M, "PROSE_RENDER_MEMO", False)
            shim.counts.clear()
            t0 = time.time()
            prose_off = M.convert_to_prose([dict(f) for f in facts], conn,
                                           anchor=user, user_id=user)
            dt_off = time.time() - t0
            fb_off = shim.counts.get("convert_to_prose.label_fallback", 0)
        finally:
            RTO.reset_current_schema(tok)
            monkeypatch.setattr(M, "PROSE_RENDER_MEMO", True)

        distinct_rels = {f.get("rel_type") for f in facts}
        # 2. bounded: ceiling on wall-time and on debug emission
        assert dt_on < 5.0, (
            f"memo ON render must respect the wall-time ceiling, took {dt_on:.2f}s "
            f"for {len(facts)} facts")
        assert fb_on <= len(distinct_rels), (
            f"memo ON: label_fallback bounded to one line per rel_type "
            f"(≤{len(distinct_rels)}), got {fb_on}")
        # the flood shape is what OFF still pays (the pre-existing defect)
        assert fb_off == len(facts), (
            "memo OFF (legacy): label_fallback fires per fact — the flood shape")
        assert dt_off > dt_on, (
            "the memo must actually collapse the per-fact work on the lattice")
        # 3. byte-parity of output
        assert prose_on == prose_off, (
            "lattice render must be byte-identical with the memo ON and OFF")
        assert len(prose_on) > 500, "the render must actually produce the lines"
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        finally:
            conn.close()

"""THE WOUND-MAP ROUND-4 WIFE-CASCADE PINS (BUG-WM3-2, 2026-11-13) — the s2 wife leg.

MEASURED WOUND (twice by the round-4 critic, wife_clean=0 both runs; replayed on the
pre-prod door 2026-11-13, seats 36215e4a/bd496b83, forensics via psql): the correction
"Actually my wife's name is Odile, not Rosalind." lands the DB-perfect alias supersession
(rosalind demoted, odile preferred) and `_file_role_first_bind` files the (user, spouse,
incumbent) slot edge — then CORRECTION_SCHEDULE_RETIRE's wire-side value sweep
(`_supersede_wire_old_value_rows`, main.py) RETIRES the slot edge itself: its SHAPE 1
retires every live supersede-class fact whose object value-node's ALIAS carries the
old-value span, and on a rename the slot edge's object IS the renamed incumbent — the
entity whose alias holds the rejected name. The edge died 26ms after creation, stamped
`superseded_at + polarity='negated'` (the R7 denial marker — the fingerprint that named
the arm), the wife was severed from the user anchor, and "what's my wife's name?" served
EMPTY while the envelope read facts_superseded=2 (alias demote 1 + slot retire 1).

FIX (the filed round-3 direction, flag CORRECTION_ROLE_SLOT_REBIND, default ON, OFF =
byte-parity): the sweep is handed the correction's bound subject — the RENAMED INCUMBENT —
and never retires a fact whose object IS that entity. A rename never invalidates the slot;
the incumbent's alias demote IS the name supersession.

Pins here (DB-gated, fresh throwaway tenant on POSTGRES_DSN, same harness contract as
tests/test_correction_designation_assist.py — the ONLY substitution vs the live path is
the correction brain, stubbed to the measured extraction shape; every deterministic arm
(name-repair intercept, walk, alias lane, value sweep) runs for real):

  1. THE WOUND SHAPE (empty role neighbourhood → first-bind): post-correction the spouse
     edge is LIVE, pointing at the incumbent entity whose PREFERRED alias is the new
     name; the rejected alias is demoted; the envelope counts only real retirements.
  2. THE NON-EMPTY NEIGHBOURHOOD (the pre-existing plant slot edge): same survival — the
     sweep skips it because its object is the renamed incumbent, not because it is young.
  3. OFF BYTE-PARITY: with the flag OFF the same turn severs the slot exactly as the
     wound measured (the edge superseded with the R7 marker) — the pre-fix behavior,
     reproduced deterministically.
  4. NEGATIVE — a genuine slot value correction (subject = the USER, a DIFFERENT
     referent's edge in the sweep's path): the old referent's edge still retires.
  5. NEGATIVE — a pure retraction still retires the slot edge (the sweep arm is
     retraction-gated at the call site; the retraction lane's own supersession runs).
  6. HELPER-LEVEL — `_supersede_wire_old_value_rows` with `renamed_entity` skips exactly
     the incumbent-object row and retires the value rows; with None it retires both
     (byte-parity of the unguarded sweep).
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

_DSN = os.environ.get("POSTGRES_DSN", "")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

import src.api.main as M  # noqa: E402

from src.extraction import linguistics as _L  # noqa: E402
assert _L.linguistics_available(), (
    "spaCy model unavailable — the wife-cascade pins require the deriver "
    "(SPACY_MODEL must resolve)")

WIFE_PLANT = "My wife is named Rosalind; she teaches second violin at the conservatory."
WIFE_CORR = "Actually my wife's name is Odile, not Rosalind."


# ── throwaway tenant (same contract as tests/test_correction_designation_assist.py) ────

@pytest.fixture(scope="function")
def tenant():
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid, derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    schema = derive_schema_name(slug)
    conn = psycopg2.connect(_DSN)
    try:
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
    finally:
        conn.close()
    yield user_id, schema
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


def _conn_schema(schema):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    cur = conn.cursor()
    cur.execute(f'SET search_path TO "{schema}"')
    return conn, cur


def _rows(schema, sql, params=()):
    conn, cur = _conn_schema(schema)
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _seed_entity(schema, name, preferred=True, provenance="user_stated"):
    """A stored Person entity + its alias (the plant's shape)."""
    entity_id = str(uuid.uuid4())
    conn, cur = _conn_schema(schema)
    try:
        cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Person') "
                    "ON CONFLICT (id) DO NOTHING", (entity_id,))
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (entity_id, alias) DO UPDATE SET "
            "is_preferred = EXCLUDED.is_preferred",
            (entity_id, name, preferred, provenance))
        conn.commit()
    finally:
        conn.close()
    return entity_id


def _seed_role_alias(schema, entity_id, role):
    """The plant's hard-line role alias (SPINE_NAMING_PLANT_SLOT files the ROLE the user
    used as a non-preferred alias of the person it named — the walk's slot test key)."""
    conn, cur = _conn_schema(schema)
    try:
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source) "
            "VALUES (%s, %s, false, 'inferred') ON CONFLICT (entity_id, alias) DO NOTHING",
            (entity_id, role))
        conn.commit()
    finally:
        conn.close()


def _seed_fact(schema, subject, rel, obj, provenance="mcp"):
    conn, cur = _conn_schema(schema)
    try:
        cur.execute(
            "INSERT INTO facts (subject_id, object_id, rel_type, provenance, fact_provenance, "
            "fact_class, confidence, created_at, last_seen_at) "
            "VALUES (%s, %s, %s, %s, 'user_stated', 'A', 1.0, now() - interval '1 hour', now()) "
            "ON CONFLICT DO NOTHING",
            (subject, obj, rel, provenance))
        conn.commit()
    finally:
        conn.close()
    return _rows(schema, "SELECT id FROM facts WHERE subject_id=%s AND rel_type=%s "
                         "AND object_id=%s", (subject, rel, obj))[0][0]


def _spouse_edge(schema, user_id):
    return _rows(schema,
                 "SELECT f.id, f.superseded_at, f.polarity FROM facts f "
                 "JOIN rel_types r ON r.rel_type = f.rel_type "
                 "WHERE f.subject_id = %s AND f.rel_type = 'spouse' "
                 "AND f.object_id IS NOT NULL", (user_id,))


def _preferred_alias(schema, entity_id):
    rows = _rows(schema, "SELECT alias FROM entity_aliases WHERE entity_id = %s "
                         "AND is_preferred = true", (entity_id,))
    return sorted(r[0] for r in rows)


class _D:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def setex(self, k, t, v):
        self.d[k] = v
        return True

    def ping(self):
        return True


class _Idem:
    redis_url = "dict://test"
    ttl = 3600
    client = _D()


# The measured LLM extraction shape for the wife turn (the deterministic name-repair
# intercept overrides the subject/rel anyway; the stub only has to be complete enough
# to pass the endpoint's completeness gates).
_WIFE_EXTRACTION = {
    "subject_name": "user", "old_rel_type": "pref_name", "old_value": "Rosalind",
    "new_rel_type": "pref_name", "new_value": "Odile", "dimension": "SCALAR",
    "confidence": 0.85, "action": "correct", "reason": "name correction",
}


def _correct(user_id, monkeypatch, text, extraction):
    from starlette.testclient import TestClient
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)
    M._idempotency_mgr = _Idem()
    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(extraction))
    with TestClient(M.app) as client:
        r = client.post("/retract/correct", json={"text": text, "user_id": user_id,
                                                  "attested": True})
    assert r.status_code == 200, r.text[:400]
    return r.json()


# ── PIN 1 — the wound shape (empty neighbourhood → first-bind; the edge survives) ──────

def test_pin1_rename_never_severs_the_slot_first_bind(tenant, monkeypatch):
    user_id, schema = tenant
    rosalind = _seed_entity(schema, "rosalind")          # the plant's named wife, NO slot edge
    _seed_fact(schema, rosalind, "teach", str(uuid.uuid4()))

    out = _correct(user_id, monkeypatch, WIFE_CORR, _WIFE_EXTRACTION)
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))

    # the slot edge is LIVE and points at the incumbent whose preferred alias is odile
    edges = _spouse_edge(schema, user_id)
    assert edges, "no spouse edge filed at all — the corrected name is unreachable"
    for fid, sup, pol in edges:
        assert sup is None, f"spouse edge {fid} superseded (polarity={pol}) — the rename severed the slot"
    live = _rows(schema, "SELECT object_id FROM facts WHERE subject_id = %s "
                         "AND rel_type = 'spouse' AND superseded_at IS NULL", (user_id,))
    assert live and str(live[0][0]) == rosalind, live
    assert _preferred_alias(schema, rosalind) == ["odile"]
    demoted = _rows(schema, "SELECT is_preferred FROM entity_aliases "
                            "WHERE entity_id = %s AND alias = 'rosalind'", (rosalind,))
    assert demoted and demoted[0][0] is False

    # envelope honesty: facts_superseded == the real DB retirements (alias demotes +
    # facts flips). No facts row retired here → the count is the demote alone.
    facts_flips = _rows(schema, "SELECT count(*) FROM facts WHERE superseded_at IS NOT NULL")[0][0]
    assert facts_flips == 0, "the rename retired facts rows — only the alias should move"
    assert out["facts_superseded"] == 1, out["facts_superseded"]


# ── PIN 2 — the non-empty neighbourhood (a pre-existing plant slot edge survives) ───────

def test_pin2_rename_keeps_the_preexisting_slot_edge(tenant, monkeypatch):
    user_id, schema = tenant
    rosalind = _seed_entity(schema, "rosalind")
    _seed_role_alias(schema, rosalind, "wife")
    _seed_fact(schema, user_id, "spouse", rosalind)      # the live plant slot edge

    out = _correct(user_id, monkeypatch, WIFE_CORR, _WIFE_EXTRACTION)
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))

    edges = _spouse_edge(schema, user_id)
    assert edges and edges[0][1] is None, edges       # live, never superseded
    live = _rows(schema, "SELECT object_id FROM facts WHERE subject_id = %s "
                         "AND rel_type = 'spouse' AND superseded_at IS NULL", (user_id,))
    assert live and str(live[0][0]) == rosalind
    assert _preferred_alias(schema, rosalind) == ["odile"]
    facts_flips = _rows(schema, "SELECT count(*) FROM facts WHERE superseded_at IS NOT NULL")[0][0]
    assert facts_flips == 0


# ── PIN 3 — OFF byte-parity: the flag OFF reproduces the wound deterministically ───────

def test_pin3_flag_off_severs_the_slot_as_before(tenant, monkeypatch):
    user_id, schema = tenant
    rosalind = _seed_entity(schema, "rosalind")
    _seed_role_alias(schema, rosalind, "wife")
    _seed_fact(schema, user_id, "spouse", rosalind)

    monkeypatch.setattr(M, "CORRECTION_ROLE_SLOT_REBIND", False)
    out = _correct(user_id, monkeypatch, WIFE_CORR, _WIFE_EXTRACTION)
    monkeypatch.setattr(M, "CORRECTION_ROLE_SLOT_REBIND", True)

    # the wounded behavior: the sweep matched the slot edge through the incumbent's
    # alias and retired it with the R7 denial marker
    edges = _spouse_edge(schema, user_id)
    assert edges, "expected the seeded slot edge"
    assert edges[0][1] is not None, "OFF must reproduce the pre-fix sweep (byte-parity)"
    assert edges[0][2] == "negated", edges


# ── PIN 4 — negative: a genuine slot VALUE correction still supersedes ─────────────────

def test_pin4_genuine_slot_correction_still_supersedes(tenant, monkeypatch):
    user_id, schema = tenant
    # a DIFFERENT referent holding the old value's edge; the correction's subject is the
    # USER (a slot replacement, not a rename of the bound entity)
    maren = _seed_entity(schema, "maren")
    _seed_fact(schema, user_id, "spouse", maren)

    out = _correct(user_id, monkeypatch,
                   "Actually my wife is Greta now.",
                   {"subject_name": "user", "old_rel_type": "spouse", "old_value": "Maren",
                    "new_rel_type": "spouse", "new_value": "Greta",
                    "dimension": "RELATIONAL", "confidence": 0.9,
                    "action": "correct", "reason": "slot correction"})
    assert out["status"] == "corrected", (out.get("status"), out.get("message"))
    # the old referent's edge is superseded and the new slot value is live — the guard
    # exists ONLY for the rename-of-the-bound-incumbent shape, never for a replacement
    edges = _spouse_edge(schema, user_id)
    maren_edges = [e for e in edges
                   if _rows(schema, "SELECT 1 FROM facts WHERE id = %s AND object_id = %s",
                            (e[0], maren))]
    assert maren_edges and maren_edges[0][1] is not None, (
        "the old referent's slot edge survived a genuine slot correction")
    live = _rows(schema, "SELECT count(*) FROM facts WHERE subject_id = %s "
                         "AND rel_type = 'spouse' AND superseded_at IS NULL", (user_id,))
    assert live[0][0] >= 1


# ── PIN 5 — negative: a pure retraction still retires the slot edge ────────────────────

def test_pin5_pure_retraction_still_retires(tenant, monkeypatch):
    user_id, schema = tenant
    rosalind = _seed_entity(schema, "rosalind")
    _seed_fact(schema, user_id, "spouse", rosalind)

    out = _correct(user_id, monkeypatch,
                   "I'm not married anymore — forget my wife.",
                   {"subject_name": "user", "old_rel_type": "spouse", "old_value": "Rosalind",
                    "new_rel_type": None, "new_value": None,
                    "dimension": "RELATIONAL", "confidence": 0.9,
                    "action": "remove", "reason": "retraction",
                    "is_retraction": True})
    edges = _spouse_edge(schema, user_id)
    assert edges, "expected the seeded slot edge"
    assert edges[0][1] is not None, "a pure retraction must retire the slot edge"


# ── PIN 6 — helper level: the guard skips exactly the incumbent-object row ─────────────

def test_pin6_helper_guard_skips_only_the_renamed_incumbent(tenant):
    user_id, schema = tenant
    rosalind = _seed_entity(schema, "rosalind")
    cadence = _seed_entity(schema, "every other weekend")   # a VALUE node, not a person
    edge_slot = _seed_fact(schema, user_id, "spouse", rosalind)
    edge_value = _seed_fact(schema, user_id, "has_setting", cadence)

    import psycopg2
    conn = psycopg2.connect(_DSN)
    cur = conn.cursor()
    cur.execute(f'SET search_path TO "{schema}"')
    try:
        # guarded sweep: the slot edge survives, the value edge retires
        n = M._supersede_wire_old_value_rows(conn, user_id, "every other weekend",
                                             renamed_entity=rosalind)
        conn.commit()
        assert n >= 1
        slot_sup = _rows(schema, "SELECT superseded_at FROM facts WHERE id = %s",
                         (edge_slot,))[0][0]
        val_sup = _rows(schema, "SELECT superseded_at FROM facts WHERE id = %s",
                        (edge_value,))[0][0]
        assert slot_sup is None, "the guard must keep the incumbent-object edge"
        assert val_sup is not None, "the genuine value row must still retire"
    finally:
        conn.close()

    # unguarded sweep (byte-parity shape): the old-value alias row retires
    greta = _seed_entity(schema, "greta")
    edge2 = _seed_fact(schema, user_id, "spouse", greta)
    conn = psycopg2.connect(_DSN)
    cur = conn.cursor()
    cur.execute(f'SET search_path TO "{schema}"')
    try:
        n = M._supersede_wire_old_value_rows(conn, user_id, "greta", renamed_entity=None)
        conn.commit()
        sup = _rows(schema, "SELECT superseded_at FROM facts WHERE id = %s",
                    (edge2,))[0][0]
        assert sup is not None, "renamed_entity=None must behave exactly as the pre-fix sweep"
    finally:
        conn.close()

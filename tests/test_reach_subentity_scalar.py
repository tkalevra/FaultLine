"""
REACH-SUBENTITY: a scalar captured on a SUB-ENTITY the user is linked to must be
reachable when the query NAMES that sub-entity — WITHOUT over-projecting on a query
that names nothing.

Canonical case (LME 945e3d21): "I attend yoga classes three times a week." captures
the rate as a scalar on the ACTIVITY node ("yoga classes"), NOT on the user:
    (user, educated_at, yoga classes)                  [relationship, staged-B]
    (yoga classes, educated_at, "three times a week")  [scalar attribute]
"How often do I attend yoga classes?" anchors on the USER (the "I" identity hook), so
the user-anchored walk only reaches the rate if determine_path pulls the anchor<->named-
entity linking rel into scope. The single-token Phase-0 alias match MISSES a multi-word
alias ("yoga classes"), so the linking rel is never scoped and the descent never seeds
the activity node -> the rate is unreachable. The fix adds a multi-word (phrase) Phase-0
match that feeds ONLY the bounded connectivity-scope pull.

This is a POSTGRES_DSN-gated integration test (real per-tenant metadata + overlays). It
skips cleanly when the dev DB / throwaway tenant is unavailable.
"""
import os
import pytest

_DSN = os.environ.get("POSTGRES_DSN")
_USER = "c0ffee00-0000-4000-8000-000000000001"
_SCHEMA = "faultline_" + _USER.replace("-", "_")
_YOGA = "99c7f1e0-ae4a-574d-a478-d4319dfc21c5"
_RATE = "three times a week"


def _connect_or_skip():
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set (dev-DB integration test)")
    import psycopg2
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (_SCHEMA,),
        )
        if not c.fetchone():
            conn.close()
            pytest.skip(f"throwaway tenant schema {_SCHEMA} not present")
        c.execute(f"SET search_path TO {_SCHEMA}")
    return conn


def _ensure_shape(conn):
    """Ensure the yoga sub-entity + linking edge + rate scalar exist. Best-effort,
    per-statement (autocommit) — the shape is normally already present from ingest;
    a differing local DDL for the housekeeping columns must not fail the test. If the
    linking edge + rate scalar are absent AND cannot be seeded, skip."""
    stmts = [
        ("INSERT INTO entities (id, entity_type) VALUES (%s,'unknown') "
         "ON CONFLICT (id) DO NOTHING", (_YOGA,)),
        ("INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
         "VALUES (%s,%s,'yoga classes',true) ON CONFLICT DO NOTHING", (_YOGA, _USER)),
        ("INSERT INTO staged_facts (user_id, subject_id, object_id, rel_type, "
         "fact_class, provenance, confidence) "
         "VALUES (%s,%s,%s,'educated_at','B','user_stated',0.8) ON CONFLICT DO NOTHING",
         (_USER, _USER, _YOGA)),
        ("INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text) "
         "VALUES (%s,%s,'educated_at',%s) "
         "ON CONFLICT (user_id, entity_id, attribute) DO UPDATE SET value_text = EXCLUDED.value_text",
         (_USER, _YOGA, _RATE)),
    ]
    for sql, params in stmts:
        try:
            with conn.cursor() as c:
                c.execute(f"SET search_path TO {_SCHEMA}")
                c.execute(sql, params)
        except Exception:
            pass  # row likely already present; verified below
    # Verify the load-bearing rows exist; else the tenant lacks the fixture -> skip.
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {_SCHEMA}")
        c.execute(
            "SELECT 1 FROM entity_attributes WHERE entity_id=%s AND lower(value_text)=%s",
            (_YOGA, _RATE),
        )
        if not c.fetchone():
            pytest.skip("yoga rate-scalar fixture not present and not seedable")


def _rate_surfaced(facts):
    return any(
        isinstance(f.get("object"), str) and _RATE in f.get("object", "").lower()
        for f in facts
    )


def test_named_subentity_scalar_is_reachable():
    """The rate scalar on the NAMED activity node surfaces from the user anchor."""
    conn = _connect_or_skip()
    try:
        _ensure_shape(conn)
        from src.api.main import resolve_anchor, determine_path, fetch_facts_from_anchor

        q = "How often do I attend yoga classes?"
        anchor = resolve_anchor(q, [], _USER, conn)
        assert anchor == _USER, "first-person 'I' must anchor on the user"

        path = determine_path(q, conn, user_id=_USER, anchor_resolved_uuid=anchor)
        # The bounded reach: connectivity-scope pulled the anchor<->'yoga classes'
        # linking rel into scope (the phrase Phase-0 match enabled it).
        assert "educated_at" in path.relationship_rels, (
            "multi-word entity 'yoga classes' must feed the connecting rel into scope; "
            f"got relationship_rels={path.relationship_rels}"
        )

        facts = fetch_facts_from_anchor(anchor, _USER, path, query_text=q)
        assert _rate_surfaced(facts), (
            "the rate scalar on the named sub-entity must surface from the user anchor"
        )
    finally:
        conn.close()


def test_no_overprojection_when_nothing_named():
    """OVER-PROJECTION GUARD: a query that names NO entity must NOT pull the
    sub-entity's scalar (no whole-profile dump)."""
    conn = _connect_or_skip()
    try:
        _ensure_shape(conn)
        from src.api.main import resolve_anchor, determine_path, fetch_facts_from_anchor

        q = "Tell me about myself."
        anchor = resolve_anchor(q, [], _USER, conn)
        path = determine_path(q, conn, user_id=_USER, anchor_resolved_uuid=anchor)
        # No entity named -> no connectivity rel -> the activity node is never seeded.
        assert "educated_at" not in path.relationship_rels, (
            "a control query naming no entity must not scope the sub-entity's linking rel; "
            f"got relationship_rels={path.relationship_rels}"
        )

        facts = fetch_facts_from_anchor(anchor, _USER, path, query_text=q)
        assert not _rate_surfaced(facts), (
            "OVER-PROJECTION: the sub-entity rate scalar leaked into an unscoped self-query"
        )
    finally:
        conn.close()

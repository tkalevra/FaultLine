"""`/extract/rewrite` answered its per-tenant questions out of the SEED TEMPLATE.

The handler opens four bare ``psycopg2.connect`` connections and, until this change, bound the
tenant schema on exactly ONE of them (the extraction-prompt menu, "RC-locative-drop FIX 2").
Every other per-tenant read ran on the default ``"$user", public`` search_path.

Measured on the live rig (2026-08-26, DSN 172.20.0.3):

  * ``SELECT DISTINCT entity_type FROM entities WHERE ... AND user_id = %s`` — the ``user_id``
    predicate is the LEGACY ``public`` shape. ``public.entities`` holds 15 rows, ALL of them
    ``user_id='anonymous'``, so for every real tenant the read returned **zero** rows and the
    branch it gates (Attempt 1, constrained inference) has NEVER executed. The running
    container's log agrees: 4 × ``gliner2_discovery_result`` (Attempt 2), 0 ×
    ``gliner2_constrained_inference`` (Attempt 1), 0 query failures.
  * Binding the schema WITHOUT dropping that predicate is strictly WORSE than the bug: the
    per-tenant ``entities`` table has no ``user_id`` column, so it raises ``UndefinedColumn``
    and ABORTS the connection's transaction — taking the ``entity_taxonomies`` read on the SAME
    connection down with it. Bind and drop the predicate TOGETHER or not at all.
  * ``rel_types`` was read from public's 66 rows while the seven live tenants carry 86-126.

Deliberately NOT bound: ``_track_correction_signal_candidate``'s connection, which is
cross-tenant BY DESIGN (``user_id='global_pattern_candidates'``).
"""
import inspect
import os
import re

import pytest

import src.api.main as main


# ── source contract ────────────────────────────────────────────────────────────

def _handler_source() -> str:
    return inspect.getsource(main.extract_rewrite)


def _connect_lines(src: str):
    return [(i, ln) for i, ln in enumerate(src.splitlines())
            if "psycopg2.connect(" in ln]


# The one connection whose reads/writes are cross-tenant by design. Keyed on the helper it
# feeds, not on a line number.
_GLOBAL_LEDGER_MARKER = "_track_correction_signal_candidate"


def test_every_per_tenant_connection_in_the_handler_is_bound():
    """Each bare connection either binds the tenant schema or is the documented global ledger.

    ABLATION: delete any ``_bind_extract_tenant_schema(...)`` call from the handler and this
    goes RED naming the offending line.
    """
    src = _handler_source()
    lines = src.splitlines()
    conns = _connect_lines(src)
    assert len(conns) >= 4, f"handler shape changed: {len(conns)} psycopg2.connect sites"

    unbound = []
    for idx, ln in conns:
        window = "\n".join(lines[idx:idx + 14])
        if "_bind_extract_tenant_schema(" in window:
            continue
        if _GLOBAL_LEDGER_MARKER in window:
            continue
        # The prompt-menu connection binds inline via the pre-existing pattern.
        if "SET search_path TO" in window:
            continue
        unbound.append((idx + 1, ln.strip()))

    assert not unbound, (
        "per-tenant read on an UNBOUND connection inside /extract/rewrite "
        f"(reads the public seed template, not the tenant): {unbound}"
    )


def test_the_entities_read_carries_no_user_id_predicate():
    """The per-tenant ``entities`` table has no ``user_id`` column; the bound search_path IS
    the scope.

    ABLATION: re-add ``AND user_id = %s`` to that SELECT and this goes RED.
    """
    src = _handler_source()
    m = re.search(r"SELECT DISTINCT entity_type FROM entities(.*?)\"\"\"", src, re.S)
    assert m, "the entities-typing SELECT is gone or was reshaped — re-verify this pin"
    assert "user_id" not in m.group(1), (
        "the entities read filters on user_id: against a BOUND per-tenant schema that is an "
        "UndefinedColumn which aborts the transaction and takes the entity_taxonomies read on "
        "the same connection with it"
    )


def test_the_constrained_attempt_is_gated_on_an_actually_widened_label_set():
    """Binding the read must not silently switch on a duplicate CPU inference.

    ABLATION: drop ``_label_set_widened`` from the Attempt-1 condition and this goes RED.
    """
    src = _handler_source()
    assert "_label_set_widened" in src, "the redundant-pass guard is gone"
    gate = re.search(r"if db_types and _label_set_widened:\s*\n\s*gliner_labels = ", src)
    assert gate, (
        "Attempt 1 (constrained inference) is no longer gated on the label set having actually "
        "widened — with the tenant bind live this fires a second GLiNER2 pass (median 263 ms "
        "measured on this CPU rig) with labels byte-identical to Attempt 2's"
    )


def test_tenant_types_cannot_widen_the_canonical_label_floor():
    """The invariant the guard rests on, on the REAL functions.

    ``_filter_to_canonical_types(..., allow_wildcards=False)`` admits only names already in
    ``_CANONICAL_ENTITY_TYPES``, and the label set is SEEDED with that whole tuple — so a
    tenant's grown types are structurally incapable of adding a label here. This is why
    binding the read changes zero types: the real growth (Event, Degree, PROPERTY, observed on
    live tenants) is non-canonical and is rejected by Pitfall-11 purity, correctly.
    """
    grown = ["Animal", "Concept", "Event", "LOCATION", "Location",
             "Object", "Organization", "PERSON", "Person", "Degree", "PROPERTY"]
    canon = main._filter_to_canonical_types(grown, allow_wildcards=False) or []
    assert set(canon) <= set(main._CANONICAL_ENTITY_TYPES)

    label_set = list(main._CANONICAL_ENTITY_TYPES)
    for t in canon:
        if t not in label_set:
            label_set.append(t)
    assert label_set == list(main._CANONICAL_ENTITY_TYPES), (
        "a tenant type widened the canonical floor — the redundant-pass guard's premise no "
        "longer holds and Attempt 1 must be allowed to fire"
    )


# ── live DB contract ───────────────────────────────────────────────────────────

_DSN = os.environ.get("POSTGRES_DSN")


def _a_live_tenant():
    import psycopg2
    with psycopg2.connect(_DSN) as db, db.cursor() as cur:
        cur.execute(
            "SELECT n.nspname FROM pg_namespace n "
            "WHERE n.nspname LIKE 'faultline!_%' ESCAPE '!' "
            "  AND EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = n.oid "
            "              AND c.relname = 'entities') "
            "ORDER BY 1"
        )
        for (schema,) in cur.fetchall():
            with db.cursor() as c2:
                c2.execute(f"SET search_path TO {schema}")
                c2.execute("SELECT count(*) FROM entities WHERE entity_type <> 'unknown'")
                if c2.fetchone()[0] > 0:
                    return schema
    return None


@pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set")
def test_the_unbound_read_answers_out_of_the_seed_and_the_bound_read_does_not():
    """The defect, and the fix, as SQL — the two arms on one live tenant."""
    import psycopg2
    schema = _a_live_tenant()
    if not schema:
        pytest.skip("no live tenant with typed entities on this DSN")

    unbound_q = ("SELECT DISTINCT entity_type FROM entities "
                 "WHERE entity_type != 'unknown' AND user_id = %s LIMIT 100")
    bound_q = ("SELECT DISTINCT entity_type FROM entities "
               "WHERE entity_type != 'unknown' LIMIT 100")
    uid = schema[len("faultline_"):].replace("_", "-")

    db = psycopg2.connect(_DSN)
    try:
        with db.cursor() as cur:
            cur.execute("SELECT current_schemas(true)")
            assert "public" in cur.fetchone()[0], "default path is not public — rig changed"
            cur.execute(unbound_q, (uid,))
            assert cur.fetchall() == [], (
                "public.entities answered for a real tenant — the seed template used to hold "
                "only user_id='anonymous' rows; re-verify the whole finding"
            )
    finally:
        db.close()

    db = psycopg2.connect(_DSN)
    try:
        with db.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SELECT current_schemas(true)")
            assert "public" not in cur.fetchone()[0], "public leaked onto the bound path"
            with pytest.raises(psycopg2.errors.UndefinedColumn):
                cur.execute(unbound_q, (uid,))
        db.rollback()
        with db.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(bound_q)
            assert len(cur.fetchall()) > 0, "bound read returned nothing"
    finally:
        db.close()


@pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set")
def test_bind_helper_binds_without_public_and_is_ablatable():
    """ABLATION: ``EXTRACT_REWRITE_TENANT_BIND=false`` must restore the unbound legacy read."""
    import psycopg2
    schema = _a_live_tenant()
    if not schema:
        pytest.skip("no live tenant on this DSN")
    uid = schema[len("faultline_"):].replace("_", "-")

    db = psycopg2.connect(_DSN)
    try:
        assert main._bind_extract_tenant_schema(db, uid, "pin") == schema
        with db.cursor() as cur:
            cur.execute("SELECT current_schemas(true)")
            assert "public" not in cur.fetchone()[0]
    finally:
        db.close()

    db = psycopg2.connect(_DSN)
    try:
        assert main._bind_extract_tenant_schema(db, "anonymous", "pin") is None
        with db.cursor() as cur:
            cur.execute("SELECT current_schemas(true)")
            assert "public" in cur.fetchone()[0], "anonymous must keep the legacy default path"
    finally:
        db.close()

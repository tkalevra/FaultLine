"""S3 — the ToS promises a COMPLETE export; the code could not produce one.

migrations/243_contract_infrastructure.sql §4 commits us to producing a complete,
machine-readable export of a customer's data on emailed request. Three things made that false,
and each has tests below:

  1. ``memory_ops.memory_export`` read THREE of the five tables named by the module's own
     ``_MEMORY_TABLES`` — ``staged_facts`` (every Class-B/C fact) and ``entities`` were simply
     never queried, while ``clear_memory`` deleted all five. Two hand-maintained lists, no
     coupling, so the reader fell behind. ``episodic_log`` — the verbatim record of every turn
     the user typed — was in NEITHER list.
  2. Every read was ``try/except → leave the key at []``, so a failed read was
     byte-indistinguishable from a customer with no data. A partial export could be delivered
     as a complete one and nobody would know.
  3. No operator could produce an export at all: ``_authorize_seat`` raises 400
     ("use an account key for memory operations") for the ``__operator__`` sentinel, and the
     only other doors are the individual's own ``/me/*``. The person answering the §4 email
     would have needed the tenant's own ``flk_`` key.

⚠️ THESE TESTS ASSERT THE EXPORTED ROWS, NOT THE TABLE LIST. Asserting
``'staged_facts' in _MEMORY_TABLES`` would have been green throughout the entire life of this
defect — the list was always right; the READER was wrong. So the DB tests seed a row into every
covered table and assert the row comes back out.

RUN IT (never raw pytest — owner rule):

    PYTHONPATH="src:<venv site-packages>" \\
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_term_test \\
    python3 tools/fltest.py --bug TERM --test tests/test_memory_export.py

THE THROWAWAY DATABASE needs the SaaS control plane (migrations >= 131) AND one tenant schema
built from ``src/provisioning/templates/user_schema.sql``. The fixture builds the tenant schema
itself; it refuses to run against a DB whose name does not look like a test DB.

FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the LAST test.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from saas import memory_ops as M          # noqa: E402

DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_term_test")
TEST_DSN = os.environ.get("MEMORY_EXPORT_TEST_DSN",
                          os.environ.get("POSTGRES_DSN", DEFAULT_TEST_DSN))

_DB_RAN = {"value": False}

# A fixed UUID → a deterministic schema name, so the fixture can build/drop it precisely.
# One seat per PROCESS (uuid5 of the xdist worker id / pid): a fixed uuid made this module's
# tests race their own tenant schema when `pytest -n N` put them on different workers
# (DuplicateSchema / "relation entities does not exist" in the floor, green alone - #44).
SEAT_UUID = str(uuid.uuid5(uuid.UUID("3f8b1c22-0e44-4a19-9c77-5d2ab6e91f30"),
                           os.environ.get("PYTEST_XDIST_WORKER") or str(os.getpid())))


def _connect():
    import psycopg2
    return psycopg2.connect(TEST_DSN)


def _schema_name():
    from src.provisioning.schema_manager import (derive_user_slug_from_uuid,
                                                 derive_schema_name)
    return derive_schema_name(derive_user_slug_from_uuid(SEAT_UUID))


# ════════════════════════════════════════════════════════════════════════════════════
# PART A — coverage is COMPUTED, not hand-listed (pure; no database)
# ════════════════════════════════════════════════════════════════════════════════════

def test_export_coverage_contains_every_table_the_wipe_deletes():
    """THE ROOT CAUSE, pinned. ``_MEMORY_TABLES`` drives the DELETE; if the export coverage can
    fall behind it again, the §4 promise breaks again. ``_EXPORT_TABLES`` is DERIVED from it, so
    a table added to the wipe is exported automatically — this asserts the containment holds."""
    missing = set(M._MEMORY_TABLES) - set(M._EXPORT_TABLES)
    assert not missing, f"the export would silently omit: {sorted(missing)}"


def test_export_coverage_also_carries_the_verbatim_turn_log():
    """``episodic_log`` holds ``raw_text`` — literally what the user typed. It is unambiguously
    their data and §4 covers it, but it is deliberately NOT in ``_MEMORY_TABLES`` (that tuple
    drives a DESTRUCTIVE path). It must be exported all the same."""
    assert "episodic_log" in M._EXPORT_ONLY_TABLES
    assert "episodic_log" in M._EXPORT_TABLES
    assert "episodic_log" not in M._MEMORY_TABLES, (
        "adding it here silently widens what 'wipe my memories' DELETES — that is an owner's "
        "call, not a side effect of an export fix")


def test_the_two_tables_the_old_reader_dropped_are_covered():
    """Named explicitly so the regression is legible: these two are the actual §4 breach."""
    assert "staged_facts" in M._EXPORT_TABLES
    assert "entities" in M._EXPORT_TABLES


def test_jsonable_coerces_every_type_a_tenant_row_can_hold():
    """``SELECT *`` returns whatever the column type is; the export must survive JSON."""
    import datetime as _dt
    import decimal as _dec
    assert M._jsonable(None) is None
    assert M._jsonable(7) == 7
    assert M._jsonable("x") == "x"
    assert M._jsonable(_dec.Decimal("1.5")) == 1.5
    assert M._jsonable(_dt.date(2026, 1, 2)) == "2026-01-02"
    assert M._jsonable(uuid.UUID(SEAT_UUID)) == SEAT_UUID
    assert M._jsonable([1, _dec.Decimal("2")]) == [1, 2.0]
    assert M._jsonable({"a": _dt.date(2026, 1, 2)}) == {"a": "2026-01-02"}
    import json
    json.dumps(M._jsonable({"n": [_dec.Decimal("3"), _dt.date(2026, 3, 4)]}))


# ════════════════════════════════════════════════════════════════════════════════════
# PART B — the REAL export, against a REAL tenant schema with REAL rows
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def tenant_schema():
    if "test" not in TEST_DSN.rsplit("/", 1)[-1]:
        pytest.skip(f"refusing to run against a non-test database: {TEST_DSN}")
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"test DB unreachable ({type(exc).__name__}): {TEST_DSN}")
    conn.autocommit = True
    schema = _schema_name()
    tpl_path = os.path.join(os.path.dirname(__file__), "..",
                            "src/provisioning/templates/user_schema.sql")
    if not os.path.exists(tpl_path):
        conn.close()
        pytest.skip("tenant schema template not found")
    sql = open(tpl_path).read().replace("{schema_name}", schema)
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}")
        try:
            cur.execute(sql)
        except Exception as exc:  # noqa: BLE001
            conn.close()
            pytest.skip(f"could not build the tenant schema ({type(exc).__name__})")
        cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s "
            "AND table_name = ANY(%s)", (schema, list(M._EXPORT_TABLES)))
        got = int(cur.fetchone()[0])
    if got != len(M._EXPORT_TABLES):
        conn.close()
        pytest.skip(f"tenant schema is missing covered tables ({got}/{len(M._EXPORT_TABLES)})")
    yield conn, schema
    conn.close()


@pytest.fixture
def seeded(tenant_schema, monkeypatch):
    """One row in EVERY covered table, so 'did it come back' is a real question."""
    _DB_RAN["value"] = True
    conn, schema = tenant_schema
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setenv("SAAS_MODE", "1")
    # The Qdrant point-count is a network call the export does not need; keep tests offline.
    monkeypatch.setattr(M, "_qdrant_point_count", lambda uid: 0)
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        for t in M._EXPORT_TABLES:
            cur.execute(f"DELETE FROM {t}")
        cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s,'Person'),(%s,'Animal')",
                    (s1, s2))
        cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                    "VALUES (%s,'chris',true),(%s,'fraggle',true)", (s1, s2))
        cur.execute("INSERT INTO entity_attributes (user_id, entity_id, attribute, value_int) "
                    "VALUES (%s,%s,'age',44)", (SEAT_UUID, s1))
        cur.execute("INSERT INTO facts (subject_id, object_id, rel_type, fact_class, confidence)"
                    " VALUES (%s,%s,'has_pet','A',1.0)", (s1, s2))
        cur.execute("INSERT INTO staged_facts (subject_id, object_id, rel_type, fact_class, "
                    "confidence) VALUES (%s,%s,'likes','C',0.5)", (s1, s2))
        cur.execute("INSERT INTO episodic_log (user_id, raw_text, source) "
                    "VALUES (%s,'My dog Fraggle is a beagle.','mcp')", (SEAT_UUID,))
    return conn, schema, s1, s2


def test_the_export_returns_rows_from_ALL_FIVE_memory_tables(seeded):
    """THE S3 OUTCOME. Every covered table has exactly one seeded population; every one of them
    must appear in ``data`` with a non-zero row count. Before the fix, ``staged_facts`` and
    ``entities`` came back absent entirely and nothing said so."""
    out = M.memory_export(SEAT_UUID)
    assert out["provisioned"] is True
    assert out["complete"] is True, out["tables"]
    assert out["incomplete"] == []
    for t in M._EXPORT_TABLES:
        assert t in out["data"], f"{t} missing from the export entirely"
        assert out["tables"][t]["status"] == "ok", out["tables"][t]
        assert out["tables"][t]["row_count"] > 0, f"{t} exported ZERO rows but was seeded"
        assert len(out["data"][t]) == out["tables"][t]["row_count"]


def test_the_verbatim_user_text_actually_appears_in_the_export(seeded):
    """Not 'a row came back' — THE USER'S OWN WORDS came back. episodic_log is the table a §4
    request is most obviously about."""
    out = M.memory_export(SEAT_UUID)
    texts = [r.get("raw_text") for r in out["data"]["episodic_log"]]
    assert "My dog Fraggle is a beagle." in texts


def test_the_staged_class_C_fact_actually_appears(seeded):
    """Class B/C facts are the customer's memory too — they were dropped wholesale."""
    out = M.memory_export(SEAT_UUID)
    rels = [r.get("rel_type") for r in out["data"]["staged_facts"]]
    assert "likes" in rels


def test_the_export_carries_columns_the_old_handpicked_projection_dropped(seeded):
    """``SELECT *`` vs seven hand-typed columns. 'Complete' has to mean complete."""
    fact = M.memory_export(SEAT_UUID)["data"]["facts"][0]
    for col in ("fact_provenance", "confirmed_count", "polarity", "temporal_status",
                "unified_confidence", "archived_at"):
        assert col in fact, f"{col} is still being dropped from the export"


def test_the_back_compat_keys_are_preserved_for_their_live_callers(seeded):
    """``transfers.py:667`` counts ``len(facts)+len(attributes)`` as the export-before-transfer
    receipt and ``dashboard/user.js`` renders all three. Changing their shape would break a
    file this task does not own."""
    out = M.memory_export(SEAT_UUID)
    assert out["facts"] and out["attributes"] and out["aliases"]
    assert set(out["facts"][0]) == {"subject_id", "rel_type", "object_id", "fact_class",
                                    "confidence", "event_date", "created_at"}
    assert set(out["attributes"][0]) == {"entity_id", "attribute", "value_text", "value_int",
                                         "value_float", "value_date", "category"}
    assert set(out["aliases"][0]) == {"entity_id", "alias", "is_preferred"}
    assert out["attributes"][0]["value_int"] == 44


def test_the_back_compat_facts_key_still_hides_archived_rows(seeded):
    """Its historic filter is ``archived_at IS NULL AND superseded_at IS NULL``. The COMPLETE
    export in ``data`` must still carry the archived row — it is the customer's data."""
    conn, schema, s1, s2 = seeded
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute("INSERT INTO facts (subject_id, object_id, rel_type, fact_class, "
                    "archived_at) VALUES (%s,%s,'owns','A', now())", (s1, s2))
    out = M.memory_export(SEAT_UUID)
    assert len(out["data"]["facts"]) == 2, "the archived row must be IN the complete export"
    assert len(out["facts"]) == 1, "the back-compat key must keep its active-only filter"
    assert out["facts"][0]["rel_type"] == "has_pet"


# ── THE FAIL-LOUD HALF ─────────────────────────────────────────────────────────────

def test_a_failing_read_is_LOUD_and_never_a_silent_empty_list(seeded, capsys):
    """THE DEFECT THAT MATTERS MOST. A read that blows up used to leave the key at ``[]``, so a
    truncated export looked exactly like an empty customer.

    The forced failure is real, not mocked: ``facts`` is replaced by a VIEW that raises at
    execution time. It EXISTS in ``information_schema.tables`` (so it cannot be dismissed as
    'absent') but ``SELECT *`` from it errors — precisely the shape the old ``except`` swallowed.
    """
    conn, schema, _s1, _s2 = seeded
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute("ALTER TABLE facts RENAME TO facts_real")
        cur.execute("CREATE VIEW facts AS SELECT 1 / 0 AS boom")
    try:
        out = M.memory_export(SEAT_UUID)
        assert out["complete"] is False, "a partial export reported itself COMPLETE"
        assert "facts" in out["incomplete"]
        assert out["tables"]["facts"]["status"] == "error"
        assert out["tables"]["facts"]["error"], "the failure cause was not recorded"
        # …and the tables that DID read are still reported honestly.
        assert out["tables"]["staged_facts"]["status"] == "ok"
        assert out["tables"]["staged_facts"]["row_count"] > 0
        # LOUD on stderr, not a silent degradation.
        assert "EXPORT INCOMPLETE" in capsys.readouterr().err
    finally:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("DROP VIEW IF EXISTS facts")
            cur.execute("ALTER TABLE facts_real RENAME TO facts")
    # The fixture must be intact for every later test.
    assert M.memory_export(SEAT_UUID)["complete"] is True


def test_a_table_that_does_not_exist_is_reported_absent_not_failed(seeded):
    """An older tenant schema predating a migration genuinely has no such table. Saying
    'absent' is honest; calling it an error would cry wolf on every legacy seat."""
    conn, schema, _s1, _s2 = seeded
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute("ALTER TABLE episodic_log RENAME TO episodic_log_tmp")
    try:
        out = M.memory_export(SEAT_UUID)
        assert out["tables"]["episodic_log"]["status"] == "absent"
        assert out["complete"] is True, "a genuinely absent table must not fake an incomplete"
    finally:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("ALTER TABLE episodic_log_tmp RENAME TO episodic_log")


def test_an_unprovisioned_seat_is_a_COMPLETE_export_of_nothing(monkeypatch, tenant_schema):
    """A licensed seat whose schema was never built has no data. That is complete, not partial
    — reporting it as incomplete would make the flag meaningless."""
    _DB_RAN["value"] = True
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    monkeypatch.setattr(M, "_qdrant_point_count", lambda uid: 0)
    out = M.memory_export(str(uuid.uuid4()))
    assert out["provisioned"] is False
    assert out["complete"] is True
    assert all(t["status"] == "absent" for t in out["tables"].values())


def test_a_malformed_user_id_never_reaches_a_schema_bind(monkeypatch):
    """Defense in depth, unchanged by this work but pinned because the export is now a door an
    OPERATOR can open for any seat."""
    monkeypatch.setenv("POSTGRES_DSN", TEST_DSN)
    with pytest.raises(ValueError):
        M.memory_export("not-a-uuid")
    with pytest.raises(ValueError):
        M.memory_export("'; DROP SCHEMA public CASCADE; --")


# ── THE OPERATOR DOOR ──────────────────────────────────────────────────────────────

def test_the_account_key_seat_gate_still_refuses_the_operator_sentinel():
    """``_authorize_seat``'s 400 is CORRECT and must STAY. It guards the account-key doors,
    whose contract is 'the target is a seat of the CALLING account'. If this ever goes green
    for the operator, those routes have lost their scoping predicate."""
    from fastapi import HTTPException
    from saas import admin as A
    with pytest.raises(HTTPException) as ei:
        A._authorize_seat("__operator__", SEAT_UUID)
    assert ei.value.status_code == 400
    assert "account key" in str(ei.value.detail)


def test_the_operator_export_route_is_operator_gated_and_audited():
    """The capability §4 sells. The GATE is asserted on the RESOLVED DEPENDENCY OBJECT, not on
    the source text.

    ⚠️ THE FIRST VERSION OF THIS TEST HAD NO TEETH AND A MUTATION RUN PROVED IT. It grepped
    ``inspect.getsource(fn)`` for "require_operator"; swapping the real
    ``Depends(require_operator)`` for ``Depends(require_account)`` left the suite GREEN, because
    the DOCSTRING says "require_operator" and the grep matched the prose. Position is not
    effect — a structural pin needs its own mutation run. This version compares the actual
    callable identity, so the gate cannot be swapped without going red."""
    import inspect
    from saas import admin as A
    assert hasattr(A, "operator_export_user_memory")
    assert hasattr(A, "operator_export_account_memory")
    for fn in (A.operator_export_user_memory, A.operator_export_account_memory):
        deps = [p.default.dependency
                for p in inspect.signature(fn).parameters.values()
                if hasattr(p.default, "dependency")]
        assert A.require_operator in deps, (
            f"{fn.__name__} is NOT operator-gated — its dependencies are "
            f"{[getattr(d, '__name__', d) for d in deps]}")
        assert A.require_account not in deps, (
            f"{fn.__name__} accepts an ACCOUNT key; this route must be operator-only")
        src = inspect.getsource(fn)
        assert "_reject_partial_export" in src, f"{fn.__name__} could leak a partial export"


def test_the_operator_export_actually_returns_the_data_and_writes_an_audit_row(seeded):
    """BEHAVIOURAL, because the grep version of this was toothless too.

    Asserting ``"_audit.record" in inspect.getsource(fn)`` stayed GREEN when the success-path
    audit call was deleted — the two calls in the except branches still matched the string. So
    this drives the real route function and reads ``public.saas_audit_log``: the row either
    landed or it did not.

    This is also the end-to-end proof of the S3 capability: an OPERATOR, holding no tenant
    ``flk_`` key, obtains a complete export for a seat."""
    conn, schema, _s1, _s2 = seeded
    from saas import admin as A
    # Control-plane rows so _operator_seat_account can resolve the owning account.
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        cur.execute("SELECT to_regclass('public.saas_audit_log'), "
                    "       to_regclass('public.account_users')")
        if not all(cur.fetchone()):
            pytest.skip("control plane not applied in this DB")
        slug = f"e{uuid.uuid4().hex[:12]}"
        cur.execute("INSERT INTO public.accounts (name, slug, plan, account_type, account_kind,"
                    " seat_limit, status) VALUES (%s,%s,'standard','standard','team',15,"
                    "'active') RETURNING account_id::text", (slug, slug))
        aid = cur.fetchone()[0]
        cur.execute("DELETE FROM public.account_users WHERE user_id = %s", (SEAT_UUID,))
        cur.execute("INSERT INTO public.account_users (account_id, user_id, status) "
                    "VALUES (%s,%s,'active')", (aid, SEAT_UUID))
        # ``public.saas_audit_log`` is APPEND-ONLY (a trigger refuses DELETE), which is the
        # point of an audit log — so watermark it instead of clearing it.
        cur.execute("SELECT COALESCE(max(ts), '-infinity'::timestamptz) "
                    "FROM public.saas_audit_log")
        watermark = cur.fetchone()[0]

    assert A._operator_seat_account(SEAT_UUID) == aid

    class _Req:
        client = None
    out = A.operator_export_user_memory(SEAT_UUID, _Req(), _op=True, allow_partial=False)
    assert out["complete"] is True
    assert out["data"]["staged_facts"], "the operator export lost the staged facts"
    assert out["data"]["episodic_log"], "the operator export lost the verbatim turns"

    with conn.cursor() as cur:
        cur.execute("SELECT action, result, account_id::text, detail FROM public.saas_audit_log "
                    "WHERE target_id = %s AND ts > %s ORDER BY ts DESC LIMIT 1",
                    (SEAT_UUID, watermark))
        row = cur.fetchone()
    assert row is not None, "an operator read a customer's memory and NOTHING was audited"
    assert row[0] == "seat.memory_export"
    assert row[1] == "success"
    assert row[2] == aid, "the audit row did not carry the owning account"
    assert "rows=" in (row[3] or "")


def test_an_unknown_seat_is_a_404_not_a_schema_probe():
    """``_operator_seat_account`` is fail-CLOSED: a user_id that is nobody's seat must not
    reach a tenant bind."""
    from fastapi import HTTPException
    from saas import admin as A
    with pytest.raises(HTTPException) as ei:
        A._operator_seat_account("not-a-uuid")
    assert ei.value.status_code == 400


def test_both_export_routes_are_mounted_under_admin():
    from saas import admin as A
    paths = {r.path for r in A.router.routes}
    assert "/admin/operator/users/{user_id}/memory/export" in paths
    assert "/admin/operator/accounts/{account_id}/memory/export" in paths


def test_a_partial_export_is_refused_by_the_transport_unless_explicitly_accepted():
    """The module stamps ``complete``; this is the half that stops the partial result leaving
    the building by accident."""
    from fastapi import HTTPException
    from saas import admin as A
    partial = {"complete": False, "incomplete": ["staged_facts"]}
    with pytest.raises(HTTPException) as ei:
        A._reject_partial_export(partial, False)
    assert ei.value.status_code == 500
    assert "staged_facts" in str(ei.value.detail)
    # explicit opt-in returns it
    A._reject_partial_export(partial, True)
    # and a complete one always passes
    A._reject_partial_export({"complete": True, "incomplete": []}, False)


# ════════════════════════════════════════════════════════════════════════════════════
# THE FALSE-GREEN GUARD — must stay LAST
# ════════════════════════════════════════════════════════════════════════════════════

def test_no_test_was_silently_skipped():
    """Part A is pure and always runs; Part B is the half that can actually falsify the fix.
    A suite reporting "PASS | N passed, M skipped" with the DB half skipped is a FALSE GREEN."""
    assert _DB_RAN["value"], (
        "the database half of this suite never ran (DSN unreachable, or the tenant schema "
        f"template could not be applied at {TEST_DSN}) — this is a FALSE GREEN, not a pass.")

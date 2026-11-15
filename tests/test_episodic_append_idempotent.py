"""first-touch-cold-path round 4 — SERVER-SIDE idempotency of the retained-turn floor.

Pins migration 274 + `main.py::episodic_append`: two calls with the SAME `turn_key` land ONE row,
and the second answers `deduplicated: true` (the second attempt's answer IS the read-back). Drive
the REAL endpoint function against a THROWAWAY migrated database — never `faultline` (the local
dev tenants) and never a real tenant DB: the fixture refuses a DSN whose database name does not
look like a test database. Removing the `ON CONFLICT (turn_key) … DO NOTHING` (the mutant the
critic named) makes the second INSERT raise a unique violation → `soft_error` → this goes red.

Build the throwaway like the entrypoint does (empty DB + every migration, ON_ERROR_STOP=0), then:
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_ft_episodic_test \\
    python3 tools/fltest.py --bug FT3 --test tests/test_episodic_append_idempotent.py
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DSN = os.environ.get("POSTGRES_DSN", "")


def _dbname(dsn: str) -> str:
    return dsn.rsplit("/", 1)[-1].split("?")[0] if dsn else ""


@pytest.fixture(scope="module")
def tenant():
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set (throwaway migrated DB required)")
    name = _dbname(_DSN)
    if "test" not in name or name == "faultline":
        pytest.skip(f"refusing to run against a non-test database: {name!r}")
    import psycopg2
    from src.provisioning.schema_manager import create_user_schema, derive_user_slug_from_uuid, derive_schema_name
    uid = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(uid)
    schema, status = create_user_schema(uid, slug)
    assert status == "ready", f"throwaway mint failed: {status}"
    assert schema == derive_schema_name(slug)
    yield uid, schema
    with psycopg2.connect(_DSN) as conn, conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (uid,))


def _rows(schema, key):
    import psycopg2
    with psycopg2.connect(_DSN) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT id FROM "{schema}".episodic_log WHERE turn_key = %s ORDER BY id', (key,))
        return [r[0] for r in cur.fetchall()]


def test_same_turn_key_twice_lands_one_row_and_the_second_answer_is_deduplicated(tenant):
    uid, schema = tenant
    import src.api.main as main
    from src.api.models import EpisodicAppendRequest
    key = uuid.uuid4().hex
    text = "My aunt Mireille keeps bees near Kemptville."
    r1 = main.episodic_append(EpisodicAppendRequest(user_id=uid, raw_text=text, source="mcp",
                                                     intent="STATEMENT", turn_key=key))
    r2 = main.episodic_append(EpisodicAppendRequest(user_id=uid, raw_text=text, source="mcp",
                                                     intent="STATEMENT", turn_key=key))
    assert r1["status"] == "stored" and r1["id"] is not None and not r1.get("deduplicated"), r1
    assert r2["status"] == "stored" and r2["id"] == r1["id"] and r2.get("deduplicated") is True, r2
    assert _rows(schema, key) == [r1["id"]], "exactly one row for one turn_key"


def test_a_different_key_for_the_same_words_is_a_new_turn(tenant):
    """The key is per TURN, not per content: a genuine re-statement still appends."""
    uid, schema = tenant
    import src.api.main as main
    from src.api.models import EpisodicAppendRequest
    text = "My aunt Mireille keeps bees near Kemptville."
    k1, k2 = uuid.uuid4().hex, uuid.uuid4().hex
    a = main.episodic_append(EpisodicAppendRequest(user_id=uid, raw_text=text, turn_key=k1))
    b = main.episodic_append(EpisodicAppendRequest(user_id=uid, raw_text=text, turn_key=k2))
    assert a["id"] != b["id"] and not b.get("deduplicated")
    assert _rows(schema, k1) == [a["id"]] and _rows(schema, k2) == [b["id"]]


def test_probe_reads_back_by_key_exactly(tenant):
    uid, schema = tenant
    import src.api.main as main
    from src.api.models import EpisodicAppendRequest
    key = uuid.uuid4().hex
    absent = main.episodic_probe(EpisodicAppendRequest(user_id=uid, raw_text="x", turn_key=key))
    assert absent == {"retained": False, "id": None, "status": "ok"}
    r = main.episodic_append(EpisodicAppendRequest(user_id=uid, raw_text="Probe me.", turn_key=key))
    present = main.episodic_probe(EpisodicAppendRequest(user_id=uid, raw_text="Probe me.", turn_key=key))
    assert present == {"retained": True, "id": r["id"], "status": "ok"}

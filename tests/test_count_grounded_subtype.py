"""COUNT-COMPLETE — aggregation-count completion: count DISTINCT USER-GROUNDED nodes under a
resolved hypernym for "how many (different) <TYPE>".

THE GAP THIS PINS: a BARE ROLE-NOUN mention ("I saw a dermatologist", "I visited a physician")
lands a TYPE node + its engine-grown ``subclass_of`` ladder (WordNet) but NO countable
``instance_of``. The gold ("How many DIFFERENT doctors did I visit?" → 3) counts the distinct
GROUNDED SUBTYPES the user actually named (dermatologist, physician, cardiologist), NOT the
engine's interior ladder rungs (specialist, medical_practitioner) and NOT the seed hypernym.

Distinguishing rule (structural, deterministic, subject-agnostic — no domain word-list):
  • real ``instance_of`` instances are counted as before (no regression: "how many cats"), AND
  • a descendant node counts as a GROUNDED SUBTYPE under exactly two signals — it touches a
    NON-classification (memory) edge ((user, see, dermatologist)), OR it is the SUBJECT of a
    classification edge whose provenance is NOT engine-interior (a Hearst hyponym lifted from the
    user's text: "citrus fruits such as ORANGE, LEMON, LIME").
  • an interior WordNet/engine rung (only engine-provenance ``subclass_of`` edges) and a plain
    TYPE node that merely has instances (``dog`` because ``Rex instance_of dog``) are EXCLUDED —
    so "how many pets" (2 instances) never inflates to 4 by counting the ``dog``/``cat`` types.

REAL-DB integration test (mirrors the deployed WordNet + Hearst + spine row shapes exactly).
Self-skips when no Postgres is reachable. NO mock — reads the real SQL against real Postgres,
the seam the query executes (verify the READ, not a fake).
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")


def _candidate_dsns():
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        yield dsn
    # Local dev container fallbacks (host TCP + docker-network IP).
    yield "postgresql://faultline:faultline@127.0.0.1:5432/faultline"
    yield "postgresql://faultline:faultline@172.20.0.5:5432/faultline"


def _connect():
    for dsn in _candidate_dsns():
        try:
            conn = psycopg2.connect(dsn, connect_timeout=4)
            conn.autocommit = True
            return conn
        except Exception:
            continue
    return None


def _U(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


@pytest.fixture()
def scenario_db():
    """Ephemeral schema with the minimal tables the count functions read, seeded with the
    doctors / citrus / pets / cats scenarios in their real (deployed) row shapes."""
    conn = _connect()
    if conn is None:
        pytest.skip("no Postgres reachable for real-DB count test")
    schema = "count_grounded_" + uuid.uuid4().hex[:8]
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA {schema}")
    cur.execute(f"SET search_path TO {schema}")
    cur.execute("CREATE TABLE rel_types(rel_type text primary key, wikidata_pid text, "
                "is_hierarchy_rel bool default false)")
    cur.execute("CREATE TABLE entity_aliases(entity_id text, alias text, "
                "is_preferred bool default false)")
    cur.execute("CREATE TABLE entity_taxonomies(taxonomy_name text, member_entity_types text[])")
    cur.execute("CREATE TABLE facts(subject_id text, object_id text, rel_type text, "
                "provenance text, superseded_at timestamptz, archived_at timestamptz, "
                "deleted_at timestamptz)")
    cur.execute("CREATE TABLE staged_facts(subject_id text, object_id text, rel_type text, "
                "provenance text, promoted_at timestamptz, deleted_at timestamptz, "
                "expires_at timestamptz)")
    cur.executemany("INSERT INTO rel_types VALUES(%s,%s,%s)", [
        ("subclass_of", "P279", True), ("instance_of", "P31", True), ("is_a", "P31", True),
        ("see", None, False), ("use", None, False), ("has_pet", None, False)])

    names = ["user", "doctor", "specialist", "medical_practitioner", "physician",
             "cardiologist", "dermatologist", "citrus_fruit", "orange", "lemon", "lime",
             "pet", "dog", "rex", "cat", "fluffy", "whiskers", "tom"]
    ids = {n: _U(schema + n) for n in names}
    for n, i in ids.items():
        cur.execute("INSERT INTO entity_aliases VALUES(%s,%s,true)", (i, n.replace("_", " ")))

    def fact(s, o, r, prov):
        cur.execute("INSERT INTO facts(subject_id,object_id,rel_type,provenance) "
                    "VALUES(%s,%s,%s,%s)", (ids[s], ids[o], r, prov))

    def stg(s, o, r, prov):
        cur.execute("INSERT INTO staged_facts(subject_id,object_id,rel_type,provenance) "
                    "VALUES(%s,%s,%s,%s)", (ids[s], ids[o], r, prov))

    # DOCTORS — bare role-noun mentions: user relational edge (grounded) + WordNet ladder
    # (interior rungs specialist / medical_practitioner). No instance_of exists.
    fact("user", "dermatologist", "see", "mcp")
    fact("user", "physician", "see", "mcp")
    fact("user", "cardiologist", "see", "mcp")
    stg("dermatologist", "specialist", "subclass_of", "ingest_wordnet_hypernym")
    stg("specialist", "doctor", "subclass_of", "ingest_wordnet_hypernym")
    stg("doctor", "medical_practitioner", "subclass_of", "ingest_wordnet_hypernym")
    stg("physician", "doctor", "subclass_of", "ingest_wordnet_hypernym")
    stg("cardiologist", "specialist", "subclass_of", "ingest_wordnet_hypernym")

    # CITRUS — Hearst hyponyms (user text) + SVO to the hypernym. No instance_of.
    fact("user", "citrus_fruit", "use", "mcp")
    stg("orange", "citrus_fruit", "subclass_of", "ingest_hearst_hyponym")
    stg("lemon", "citrus_fruit", "subclass_of", "ingest_hearst_hyponym")
    stg("lime", "citrus_fruit", "subclass_of", "ingest_hearst_hyponym")

    # PETS — a real instance (Rex) + a type node (dog) that must NOT over-count.
    fact("rex", "dog", "instance_of", "user_stated")
    fact("user", "rex", "has_pet", "mcp")
    stg("dog", "pet", "subclass_of", "ingest_wordnet_hypernym")

    # CATS — three real instances (plain instance-count no-regression baseline).
    for pet in ("fluffy", "whiskers", "tom"):
        fact(pet, "cat", "instance_of", "user_stated")
        fact("user", pet, "has_pet", "mcp")

    yield conn, ids
    try:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    finally:
        conn.close()


def _grounded(conn, phrase):
    from src.api import main as m
    tset = m._resolve_count_type_uuids(conn, phrase)
    seed = m._resolve_count_seed_uuids(conn, phrase)
    crels = m._get_classification_rels(conn)
    res = m._l4_direct_grounded_type_count(conn, tset, seed, crels, crels)
    return res


def test_grounded_subtypes_three_doctors(scenario_db):
    """"how many (different) doctors" → 3 grounded subtypes, interior rung 'specialist' excluded."""
    conn, ids = scenario_db
    res = _grounded(conn, "different doctors")
    got = set(res["instance_ids"])
    assert res["count"] == 3, f"expected 3 grounded doctors, got {res['count']}: {got}"
    assert got == {ids["dermatologist"], ids["physician"], ids["cardiologist"]}
    assert ids["specialist"] not in got            # engine interior rung excluded
    assert ids["medical_practitioner"] not in got  # engine interior rung (and an ancestor)
    assert ids["doctor"] not in got                # the seed hypernym itself excluded


def test_grounded_subtypes_three_citrus_via_hearst(scenario_db):
    """"how many different types of citrus fruits" → 3 Hearst hyponyms (orange/lemon/lime)."""
    conn, ids = scenario_db
    res = _grounded(conn, "different types of citrus fruits")
    got = set(res["instance_ids"])
    assert res["count"] == 3, f"expected 3 citrus subtypes, got {res['count']}: {got}"
    assert got == {ids["orange"], ids["lemon"], ids["lime"]}
    assert ids["citrus_fruit"] not in got          # seed hypernym excluded


def test_type_node_with_instances_not_overcounted(scenario_db):
    """"how many pets" → 1 (the instance Rex); the 'dog' TYPE node is NOT counted as a subtype."""
    conn, ids = scenario_db
    res = _grounded(conn, "pets")
    got = set(res["instance_ids"])
    assert res["count"] == 1, f"expected 1 pet (Rex), got {res['count']}: {got}"
    assert got == {ids["rex"]}
    assert ids["dog"] not in got                   # a type-with-instances is not a grounded subtype


def test_real_instance_count_intact(scenario_db):
    """No regression: "how many cats" (3 real instance_of edges) still counts 3 instances."""
    conn, ids = scenario_db
    res = _grounded(conn, "cats")
    assert res["count"] == 3, f"expected 3 cats, got {res['count']}"
    assert set(res["instance_ids"]) == {ids["fluffy"], ids["whiskers"], ids["tom"]}


def test_deterministic_repeat(scenario_db):
    """Deterministic: identical result across repeated calls (no cosine/LLM, pure SQL)."""
    conn, _ = scenario_db
    a = _grounded(conn, "different doctors")
    b = _grounded(conn, "different doctors")
    assert a["count"] == b["count"] == 3
    assert set(a["instance_ids"]) == set(b["instance_ids"])


def test_grounded_count_failsafe_on_db_error():
    """Fail-safe: a broken cursor → miss (never a fabricated count)."""
    from src.api import main as m

    class Boom:
        def cursor(self):
            raise RuntimeError("db down")

    out = m._l4_direct_grounded_type_count(
        Boom(), {"t"}, set(), {"instance_of", "is_a"}, {"subclass_of", "instance_of", "is_a"})
    assert out["miss"] is True and out["count"] == 0


def test_grounded_count_empty_type_is_miss():
    """An unresolvable type (empty uuid set) → miss, never a fabricated 0-count assertion."""
    from src.api import main as m
    out = m._l4_direct_grounded_type_count(
        object(), set(), set(), {"instance_of"}, {"subclass_of", "instance_of"})
    assert out["miss"] is True and out["count"] == 0

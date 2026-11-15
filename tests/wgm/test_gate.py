import pytest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from wgm.gate import WGMValidationGate, SEED_ONTOLOGY


def test_project_structure():
    assert os.path.exists("src/wgm/__init__.py")
    assert os.path.exists("src/wgm/gate.py")


# ── REAL-SEAM FIXTURES (gauntlet wgm-test-gate-dead-mocks) ──────────────────
# The nine dead-mock reds at HEAD drove validate_edge() through a bare
# MagicMock connection. Their fixtures predated three LATER gate seams that
# read REAL cursor shapes and fail loud (by design — never weaken them):
#   • _find_inverse_rel_type — a truthy mock fetchone() row became the
#     "canonical" rel_type (a MagicMock), so the ontology fail-loud
#     (RuntimeError "not found in rel_types ontology", f913f37c 2026-05-22)
#     fired on a garbage key.
#   • _concrete_role_violation — the type-refusal guard (8670ac99, 2026-09-16)
#     resolved a MagicMock entity type and REFUSED the edge (type_refused).
#   • _infer_entity_type / _validate_category_constraints — compared / iterated
#     mock rowcount and mock taxonomy rows ('>' with int / 'int' object is not
#     iterable).
# The fixtures below re-feed the SAME scenarios through the seam the gate
# actually opens: a psycopg2-shaped cursor whose DEFAULT response is an honest
# empty database (fetchone() -> None, fetchall() -> [], rowcount 0), with
# scenario rows supplied per-statement, plus an in-memory rel_types stub
# sourced from the REAL SEED_ONTOLOGY rows and installed through the same
# get_current_ontology patch point the staged-hierarchy tests below use.

def _rows(sql_substr, fetchone=None, fetchall=None, rowcount=None, params=None):
    """Scenario rule: respond to statements whose whitespace-normalized SQL
    contains `sql_substr` — and whose params contain `params` as an
    order-preserving subsequence when given — with the supplied row shapes."""
    return {"sql": sql_substr, "params": params, "fetchone": fetchone,
            "fetchall": fetchall, "rowcount": rowcount}


def _subsequence(needle, haystack):
    it = iter(haystack)
    return all(any(n == h for h in it) for n in needle)


class _ScriptedCursor:
    """psycopg2-shaped cursor routed by the scenario. Unmatched statements get
    the honest empty-DB shapes — never a truthy mock row."""

    def __init__(self, conn):
        self._conn = conn

    def _rule(self):
        if not self._conn.executed:
            return None
        sql, params = self._conn.executed[-1]
        for rule in self._conn.scenario:
            if rule["sql"] in sql and (
                rule["params"] is None or _subsequence(rule["params"], params)
            ):
                return rule
        return None

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(str(sql).split()), tuple(params or ())))

    def fetchone(self):
        rule = self._rule()
        return rule["fetchone"] if rule and rule["fetchone"] is not None else None

    def fetchall(self):
        rule = self._rule()
        return rule["fetchall"] if rule and rule["fetchall"] is not None else []

    @property
    def rowcount(self):
        rule = self._rule()
        return rule["rowcount"] if rule and rule["rowcount"] is not None else 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ScriptedConn:
    """Stands in for the gate's tenant connection at the cursor seam
    validate_edge() opens. Records every executed statement for assertion."""

    def __init__(self, scenario=None):
        self.scenario = scenario or []
        self.executed = []  # (whitespace-normalized SQL, params tuple)
        self.commits = 0

    def cursor(self):
        return _ScriptedCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def sql_log(self):
        return [sql for sql, _ in self.executed]


def _stub_gate(rel_names, scenario=None):
    """A gate wired through the real cursor seam, whose ontology is an
    in-memory stub of the REAL SEED_ONTOLOGY rows for `rel_names`. registry /
    validator are unset: the unified-confidence block fails safe to the edge
    confidence, which is the deterministic no-LLM unit behaviour."""
    gate = WGMValidationGate.__new__(WGMValidationGate)
    gate.db_conn = _ScriptedConn(scenario)
    gate._schema_name = None
    gate.registry = None
    gate.validator = None
    gate.get_current_ontology = lambda: {
        name: SEED_ONTOLOGY[name] for name in rel_names
    }
    return gate


def test_validate_novel_type():
    """Novel rel_type not in ontology → 'novel_unapproved' for Class C staging
    (dprompt-130), with the type queued into pending_types (we don't forget —
    the re_embedder evaluates it async). Re-fed through the real cursor seam:
    the inverse lookup reads fetchone() -> None (no inverse row exists); the
    old bare MagicMock turned that None into a truthy mock "canonical"
    rel_type, which tripped the ontology fail-loud (RuntimeError, f913f37c)."""
    gate = _stub_gate([])  # empty in-memory ontology: the rel is genuinely novel

    result = gate.validate_edge(1, 2, "unknown_relationship")

    assert result["status"] == "novel_unapproved"
    assert any("INSERT INTO pending_types" in sql for sql in gate.db_conn.sql_log())


def test_validate_conflict():
    """Known rel_type but a different rel already exists for same pair → conflict."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = [(42, 1.0)]  # Existing fact with id=42, confidence=1.0
    cursor_mock.fetchone.return_value = (99,)  # New fact id from RETURNING

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge(1, 2, "is_a", provenance="doc")

    assert result["status"] == "conflict"


def test_validate_valid_edge():
    """Known rel_type, no existing edges for this pair → valid."""
    mock_conn = MagicMock()
    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.return_value = []

    gate = WGMValidationGate(mock_conn)
    result = gate.validate_edge(1, 2, "IS_A")

    assert result["status"] == "valid"
    assert "unified_confidence" in result


def test_validate_same_rel_existing():
    """Re-asserting the SAME triple is valid — idempotent BY CONSTRUCTION now:
    the conflict probe excludes same-object rows (`object_id != %s`, 8c2ec971),
    so a same-triple rewrite never reaches conflict handling (the storage-layer
    `ON CONFLICT (subject_id, object_id, rel_type)` upsert absorbs it). The old
    fixture fed a MagicMock rel_type row into every fetch and expected the dead
    exact shape {"status": "valid"}; the valid return carries unified_confidence
    since dBug-046 Phase 2a (aeeea3ca)."""
    gate = _stub_gate(["is_a"])

    result = gate.validate_edge(1, 2, "IS_A")  # case-normalized by the gate

    assert result["status"] == "valid"
    assert "unified_confidence" in result
    conflict_probes = [sql for sql in gate.db_conn.sql_log()
                       if "SELECT id, confidence FROM facts" in sql]
    assert conflict_probes, "the conflict probe must run"
    assert all("object_id !=" in sql for sql in conflict_probes)
    assert not any("INSERT INTO" in sql for sql in gate.db_conn.sql_log())


# Conflict scenario fed through the real seam: the probe returns a real
# (id, confidence) row for the OLD fact; INSERT ... RETURNING id returns the
# NEW fact's id — the exact shapes psycopg2 delivers on this path.
_CONFLICT_SCENARIO = [
    _rows("SELECT id, confidence FROM facts", fetchall=[(42, 1.0)]),
    _rows("RETURNING id", fetchone=(99,)),
]


def test_conflict_inserts_new_fact():
    """Conflicting edge (same subject+rel, different object) gets INSERTed, not
    dropped. Re-fed through the real seam: the probe reads real (id, confidence)
    rows; the old MagicMock shipped a mock into the category-constraint lane,
    which iterated it and raised."""
    gate = _stub_gate(["also_known_as"], scenario=_CONFLICT_SCENARIO)

    result = gate.validate_edge("arthur", "christophe", "also_known_as",
                                user_id="user1", provenance="doc")

    assert result["status"] == "conflict"
    assert any("INSERT INTO facts" in sql for sql in gate.db_conn.sql_log())


def test_conflict_penalizes_old_fact():
    """After the conflict INSERT, the old fact's confidence drops by the 0.5
    penalty (GREATEST-floored at 0.0, via FactStoreManager.mark_contradicted)
    and the result links old → new fact ids."""
    gate = _stub_gate(["also_known_as"], scenario=_CONFLICT_SCENARIO)

    result = gate.validate_edge("arthur", "christophe", "also_known_as",
                                user_id="user1", provenance="doc")

    assert result["superseded_fact_id"] == 42
    assert result["new_fact_id"] == 99
    assert result["old_confidence_after_penalty"] == pytest.approx(0.5)
    assert any("GREATEST" in sql for sql in gate.db_conn.sql_log())


def test_conflict_marks_old_superseded():
    """After a conflict INSERT, the old fact's contradicted_by is updated to the
    new fact id — the supersession link recall reads."""
    gate = _stub_gate(["works_for"], scenario=_CONFLICT_SCENARIO)

    gate.validate_edge("alice", "corp_b", "works_for",
                       user_id="user1", provenance="doc")

    assert any("contradicted_by" in sql for sql in gate.db_conn.sql_log())


def test_valid_path_unaffected():
    """Valid edge with no prior conflict returns status=valid (carrying the
    dBug-046 unified_confidence, aeeea3ca) and writes NO supersession: no
    contradicted_by update, no INSERT. The old fixture died in the type-refusal
    guard (8670ac99) because its bare MagicMock resolved a mock entity type for
    'alice'; the honest empty-DB shapes resolve nothing → no refusal."""
    gate = _stub_gate(["works_for"])

    result = gate.validate_edge("alice", "corp", "works_for")

    assert result["status"] == "valid"
    assert "unified_confidence" in result
    sql_log = gate.db_conn.sql_log()
    assert not any("contradicted_by" in sql for sql in sql_log)
    assert not any("INSERT INTO facts" in sql for sql in sql_log)


def test_owns_in_ontology():
    """New rel_type 'owns' is in the ontology."""
    from wgm.gate import SEED_ONTOLOGY
    assert "owns" in SEED_ONTOLOGY


def test_educated_at_in_ontology():
    """New rel_type 'educated_at' is in the ontology."""
    from wgm.gate import SEED_ONTOLOGY
    assert "educated_at" in SEED_ONTOLOGY


def test_occupation_in_ontology():
    """New rel_type 'occupation' is in the ontology."""
    from wgm.gate import SEED_ONTOLOGY
    assert "occupation" in SEED_ONTOLOGY


def test_rel_type_registry_loads_from_db():
    """RelTypeRegistry can load types from mock DB."""
    from wgm.gate import RelTypeRegistry
    from unittest.mock import MagicMock, patch

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_cursor.fetchall.return_value = [("is_a",), ("works_for",), ("spouse",)]
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None
    mock_conn.cursor.return_value = mock_cursor

    with patch("wgm.gate.psycopg2.connect", return_value=mock_conn):
        registry = RelTypeRegistry("fake_dsn")
        types = registry.get_valid_types()

    assert "is_a" in types
    assert "works_for" in types
    assert "spouse" in types


def test_rel_type_registry_is_valid():
    """RelTypeRegistry.is_valid checks membership correctly."""
    from wgm.gate import RelTypeRegistry
    from unittest.mock import MagicMock, patch

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_cursor.fetchall.return_value = [("is_a",), ("works_for",)]
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None
    mock_conn.cursor.return_value = mock_cursor

    with patch("wgm.gate.psycopg2.connect", return_value=mock_conn):
        registry = RelTypeRegistry("fake_dsn")
        assert registry.is_valid("is_a") is True
        assert registry.is_valid("unknown_type") is False


def test_novel_type_approved_by_qwen(monkeypatch):
    """When the LLM approves a novel type with confidence >= 0.7, full ontology
    metadata is INSERTed into rel_types and the type is usable immediately.
    Re-fed through the REAL seam — _try_approve_novel_type calls the
    centralized call_llm_with_retry_sync (23134f90), not raw httpx.post; the
    old patch on wgm.gate.httpx.post was a dead mock and this test passed only
    via the auto-approve fallback, asserting nothing about approval. The model
    env is set because LLMModels.get (argument evaluation) is fail-loud on an
    unconfigured box — the same config read the old test stubbed via os.getenv."""
    from unittest.mock import patch

    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    approved = {
        "valid": True, "label": "influenced by", "wikidata_pid": "P1891",
        "is_symmetric": False, "inverse_rel_type": None,
        "head_types": ["Person"], "tail_types": ["Concept"],
        "is_hierarchy_rel": False, "category": "other", "confidence": 0.85,
    }
    gate = _stub_gate([])

    with patch("wgm.gate.call_llm_with_retry_sync", return_value=approved):
        result = gate._try_approve_novel_type("influenced_by")

    assert result is True
    # The FULL-metadata approval INSERT (not the fallback's bare 0.7 row).
    assert any("INSERT INTO rel_types" in sql and "wikidata_pid" in sql
               for sql in gate.db_conn.sql_log())


def test_novel_type_rejected_by_qwen(monkeypatch):
    """When the LLM rejects a novel type (confidence < 0.7), it is queued into
    pending_types (we don't forget — async re-evaluation) and the approval
    returns False. Re-fed through the real call_llm_with_retry_sync seam
    (23134f90); the old httpx.post patch never fired, the real call failed,
    and the auto-approve fallback returned the opposite verdict."""
    from unittest.mock import patch

    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    rejected = {
        "valid": False, "label": "unknown", "wikidata_pid": None,
        "confidence": 0.2, "reason": "Not a standard relationship",
    }
    gate = _stub_gate([])

    with patch("wgm.gate.call_llm_with_retry_sync", return_value=rejected):
        result = gate._try_approve_novel_type("nonsense_type")

    assert result is False
    assert any("INSERT INTO pending_types" in sql for sql in gate.db_conn.sql_log())


def test_novel_type_qwen_timeout(monkeypatch):
    """When the LLM call fails/times out, the novel type is AUTO-APPROVED with
    fallback metadata so facts are not silently dropped. Re-fed through the
    real seam: the old test raised httpx.TimeoutException from
    wgm.gate.httpx.post — a seam this function stopped using (23134f90) — so
    the timeout never fired and the green was vacuous."""
    from unittest.mock import patch

    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    gate = _stub_gate([])

    with patch("wgm.gate.call_llm_with_retry_sync",
               side_effect=Exception("LLM backend unreachable")):
        result = gate._try_approve_novel_type("some_type")

    # Auto-approve on LLM failure — facts should not be silently dropped
    assert result is True
    # The bare fallback row (0.7 engine-generated Class B), not a full-metadata INSERT.
    assert any("INSERT INTO rel_types" in sql and "0.7, 'engine', 'B'" in sql
               for sql in gate.db_conn.sql_log())


def test_symmetric_duplicate_suppressed():
    """For symmetric rel_types, storing A→B when NEITHER A→B nor B→A exists is
    a plain valid — the gate itself writes nothing (the duplicate probes find no
    rows through the real cursor seam). The old bare MagicMock died in the
    category-constraint lane on mock taxonomy rows; the honest empty-DB shapes
    take the documented defer-to-re_embedder path instead. The old
    exact-equality `== {"status": "valid"}` pinned a dead shape: the return
    carries unified_confidence since dBug-046 Phase 2a (aeeea3ca)."""
    gate = _stub_gate(["spouse"])

    result = gate.validate_edge("alice", "bob", "spouse", provenance="doc")

    assert result["status"] == "valid"
    assert "unified_confidence" in result
    sql_log = gate.db_conn.sql_log()
    assert not any("INSERT INTO" in sql for sql in sql_log)
    assert not any("UPDATE facts" in sql for sql in sql_log)


def test_symmetric_duplicate_detected():
    """When B→A already exists and A→B is attempted for a symmetric type, the
    gate returns valid + note=symmetric_duplicate (the fact is implicitly stored
    in both directions). Re-fed through the real seam: the two duplicate probes
    are TEXTUALLY IDENTICAL SQL distinguished only by param ORDER — (A,B) then
    (B,A) — so the scenario rule matches the params subsequence, the exact
    shapes psycopg2 delivers."""
    gate = _stub_gate(
        ["spouse"],
        scenario=[
            # Second duplicate probe (object_id, subject_id) = ("bob", "alice"): found.
            _rows("SELECT id FROM facts", params=("bob", "alice"), fetchone=(42,)),
        ],
    )

    result = gate.validate_edge("alice", "bob", "spouse", provenance="doc")

    assert result["status"] == "valid"
    assert result["note"] == "symmetric_duplicate"


def test_instance_of_valid():
    """instance_of (P31) is a valid rel_type."""
    from wgm.gate import SEED_ONTOLOGY
    assert "instance_of" in SEED_ONTOLOGY


def test_subclass_of_valid():
    """subclass_of (P279) is a valid rel_type."""
    from wgm.gate import SEED_ONTOLOGY
    assert "subclass_of" in SEED_ONTOLOGY


def test_pref_name_valid():
    """pref_name (skos:prefLabel) is a valid rel_type."""
    from wgm.gate import SEED_ONTOLOGY
    assert "pref_name" in SEED_ONTOLOGY


def test_same_as_valid():
    """same_as (owl:sameAs) is a valid rel_type."""
    from wgm.gate import SEED_ONTOLOGY
    assert "same_as" in SEED_ONTOLOGY


def test_is_a_deprecated_but_valid():
    """is_a is deprecated but remains valid for backward compatibility."""
    from wgm.gate import SEED_ONTOLOGY
    assert "is_a" in SEED_ONTOLOGY


# ── Tests for _delete_conflicting_staged_hierarchy_facts ─────────────────────

def _make_gate_with_hierarchy_ontology():
    """Helper: build a WGMValidationGate whose get_current_ontology() returns
    instance_of as is_hierarchy_rel=True and works_for as is_hierarchy_rel=False."""
    mock_conn = MagicMock()
    gate = WGMValidationGate(mock_conn)
    # Patch get_current_ontology to return a minimal ontology.
    from unittest.mock import patch
    ontology = {
        "instance_of": {"is_hierarchy_rel": True, "is_symmetric": False,
                        "correction_behavior": "supersede", "inverse_rel_type": None,
                        "head_types": ["ANY"], "tail_types": ["ANY"]},
        "works_for":   {"is_hierarchy_rel": False, "is_symmetric": False,
                        "correction_behavior": "supersede", "inverse_rel_type": None,
                        "head_types": ["Person"], "tail_types": ["Organization"]},
    }
    gate.get_current_ontology = lambda: ontology
    return gate, mock_conn


def test_staged_hierarchy_conflict_skips_non_hierarchy():
    """_delete_conflicting_staged_hierarchy_facts is a no-op for non-hierarchy rel_types."""
    gate, mock_conn = _make_gate_with_hierarchy_ontology()

    deleted = gate._delete_conflicting_staged_hierarchy_facts(
        subject_id="entity-abc",
        rel_type="works_for",
        object_id="org-123",
    )

    assert deleted == 0
    # DB should NOT have been touched for a non-hierarchy rel_type.
    mock_conn.cursor.assert_not_called()


def test_staged_hierarchy_conflict_deletes_conflicting_rows():
    """When a new instance_of arrives, existing staged_facts rows with a different
    object_id for the same subject are hard-deleted."""
    gate, mock_conn = _make_gate_with_hierarchy_ontology()

    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    # Pass 1 query (conflicting rows) returns two stale staged_facts IDs.
    # Pass 2 query (inverted rows) returns nothing — subject is not a system token.
    cursor_mock.fetchall.side_effect = [
        [(101,), (102,)],  # conflicting rows
        [],                 # inverted rows (subject "entity-abc" is not a system name)
    ]
    cursor_mock.rowcount = 2

    deleted = gate._delete_conflicting_staged_hierarchy_facts(
        subject_id="entity-abc",
        rel_type="instance_of",
        object_id="org-uuid-new",
    )

    assert deleted == 2
    mock_conn.commit.assert_called()
    # Verify a DELETE statement was issued.
    all_sql = " ".join(str(c) for c in cursor_mock.execute.call_args_list)
    assert "DELETE" in all_sql


def test_staged_hierarchy_conflict_no_rows_no_delete():
    """When staged_facts has no conflicting rows, nothing is deleted and no commit fires."""
    gate, mock_conn = _make_gate_with_hierarchy_ontology()

    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    cursor_mock.fetchall.side_effect = [[], []]  # no conflicts, no inverted

    deleted = gate._delete_conflicting_staged_hierarchy_facts(
        subject_id="entity-abc",
        rel_type="instance_of",
        object_id="org-uuid-new",
    )

    assert deleted == 0
    mock_conn.commit.assert_not_called()


def test_staged_hierarchy_conflict_purges_inverted_system_token_subject():
    """When subject_id is a _SYSTEM_ENTITY_NAMES token (e.g. 'person'), all staged
    hierarchy rows for that subject are deleted (they are inverted mis-extractions)."""
    gate, mock_conn = _make_gate_with_hierarchy_ontology()

    cursor_mock = mock_conn.cursor.return_value.__enter__.return_value
    # Pass 1 (conflicting rows, different object): 0 results —
    #   subject_id "person" == object_id "person"? No, we test distinct objects.
    # Pass 2 (inverted — subject IS a system token): returns one row.
    cursor_mock.fetchall.side_effect = [
        [],        # conflicting rows with different object_id
        [(201,)],  # inverted row: "person instance_of alicia" ← wrong direction
    ]
    cursor_mock.rowcount = 1

    deleted = gate._delete_conflicting_staged_hierarchy_facts(
        subject_id="person",   # system token — inversion guard fires
        rel_type="instance_of",
        object_id="alicia-uuid",
    )

    assert deleted == 1
    mock_conn.commit.assert_called()
    all_sql = " ".join(str(c) for c in cursor_mock.execute.call_args_list)
    assert "DELETE" in all_sql


def test_staged_hierarchy_conflict_db_error_logs_loud_returns_zero():
    """A DB error in _delete_conflicting_staged_hierarchy_facts must log.error and
    return 0 — never silently skip (CLAUDE.md: fail loud)."""
    gate, mock_conn = _make_gate_with_hierarchy_ontology()

    mock_conn.cursor.side_effect = Exception("DB connection lost")

    deleted = gate._delete_conflicting_staged_hierarchy_facts(
        subject_id="entity-abc",
        rel_type="instance_of",
        object_id="org-uuid-new",
    )

    assert deleted == 0  # Must return 0, not raise

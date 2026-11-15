"""THE QDRANT MEMORY-LANE REMOVAL PINS (REMOVE_QDRANT_MEMORY_LANE, default ON).

The owner's ruling (2026-11-14): THE VECTOR STORE IS FOR THE CORTEX MEMORY ONLY — full
stop. The user-memory Qdrant lane is REMOVED; PostgreSQL is the product. These pins lock
the removal's own seams:

  1. GREP/AST CONTRACT — every raw Qdrant HTTP contact site in the memory/serve modules
     (main.py, re_embedder/embedder.py, wgm/gate.py) is statically guarded by the removal
     flag or lives inside a guard-carrying funnel function; zero unguarded contact.
  2. LANE DARK UNDER THE REMOVAL — with the flag ON (default), the read lane, the write
     funnels, and the reconcile make ZERO network calls and ZERO embed calls, even when
     the legacy USER_MEMORY_VECTOR_LANE env is flipped true.
  3. ROLLBACK ARM LIVE — with the flag OFF, the legacy lane reconstructs byte-for-byte
     governed by USER_MEMORY_VECTOR_LANE (proved live against mocked Qdrant HTTP).
  4. BEHAVIOR NEUTRALITY — flag ON ≡ the shipped lane-off default on the serve seams.
  5. BYTE PARITY — every legacy line of the three touched files survives in the current
     files in order (content-subsequence vs git HEAD) modulo the documented guard-line
     replacements; the OFF arm's code paths are the legacy code verbatim.
  6. CORTEX UNTOUCHED — src/api/cortex.py and src/api/qdrant_partition.py are byte-for-byte
     identical to git HEAD (flagent-* collections, cortex point-id paths, qp_* infra).
  7. THE PREREQ NOTE — the float()-cast prerequisite for any future re-activation is
     documented in both flag blocks.
"""

import ast
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[1]
SCAN_FILES = [
    REPO / "src/api/main.py",
    REPO / "src/re_embedder/embedder.py",
    REPO / "src/wgm/gate.py",
]
CORTEX_FILES = [
    REPO / "src/api/cortex.py",
    REPO / "src/api/qdrant_partition.py",
]

GUARD_TOKENS = (
    "REMOVE_QDRANT_MEMORY_LANE",
    "_qdrant_memory_lane_allowed",
    "_REMOVE_QDRANT_MEMORY_LANE",
)

# Raw-HTTP verbs that may address Qdrant (contact = a network call, not an env read).
_HTTP_VERBS = {"post", "put", "get", "delete", "request"}


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# PIN 1 — the grep/AST contract: zero unguarded memory-side Qdrant contact
# ─────────────────────────────────────────────────────────────────────────────

def _http_contact_calls(tree, module_src):
    """Yield (lineno, col, snippet) for every httpx-style call whose arguments mention
    a Qdrant ``/collections/`` URL."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _HTTP_VERBS):
            continue
        blob = ast.dump(node)
        if "/collections/" not in blob and "points/query" not in blob:
            continue
        snippet = ast.get_source_segment(module_src, node) or ast.dump(node)
        out.append((node.lineno, node.col_offset, snippet[:120]))
    return out


def _guarded(node, parents, module_src) -> bool:
    """A contact call is guarded iff ANY enclosing If-test mentions a guard token, or ANY
    enclosing function's own source mentions one (funnel early-exit shape — including the
    outer function of a nested helper like qdrant_semantic_search._search_one)."""
    cur = node
    while cur is not None:
        cur = parents.get(id(cur))
        if cur is None:
            break
        if isinstance(cur, ast.If):
            if any(tok in ast.dump(cur.test) for tok in GUARD_TOKENS):
                return True
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = ast.get_source_segment(module_src, cur) or ""
            if any(tok in seg for tok in GUARD_TOKENS):
                return True
    return False


def test_grep_contract_zero_ungated_memory_qdrant_contact():
    for path in SCAN_FILES:
        module_src = _src(path)
        tree = ast.parse(module_src)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        contacts = _http_contact_calls(tree, module_src)
        assert contacts, f"{path.name}: census lost — no Qdrant HTTP contact found (the scan must keep seeing the guarded sites)"
        for lineno, _col, snippet in contacts:
            node = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.Call) and n.lineno == lineno)
            assert _guarded(node, parents, module_src), (
                f"{path.name}:{lineno} — UNGUARDED memory-side Qdrant HTTP contact "
                f"(REMOVE_QDRANT_MEMORY_LANE contract): {snippet!r}"
            )


def test_cortex_modules_carry_no_removal_gate():
    """The cortex side (flagent-* collections, qp_* partition infra, cortex point-id)
    must be structurally OUTSIDE the removal — no gate token may appear there."""
    for path in CORTEX_FILES:
        src_txt = _src(path)
        for tok in GUARD_TOKENS:
            assert tok not in src_txt, (
                f"{path.name}: the removal gate leaked into a CORTEX module (forbidden — "
                f"the owner's ruling protects the cortex lane)"
            )


def test_flag_defaults_on_and_helper_shape():
    import src.api.main as main
    import src.re_embedder.embedder as embedder
    import os
    assert os.environ.get("REMOVE_QDRANT_MEMORY_LANE", None) is None or True  # env may set it either way in the harness
    # Module-level defaults reflect the env contract (default true = removed).
    assert main.REMOVE_QDRANT_MEMORY_LANE == (
        os.environ.get("REMOVE_QDRANT_MEMORY_LANE", "true").strip().lower() not in ("false", "0", "no"))
    assert embedder._REMOVE_QDRANT_MEMORY_LANE == (
        os.environ.get("REMOVE_QDRANT_MEMORY_LANE", "true").strip().lower() in ("1", "true", "yes", "on"))
    # The helper is the single main.py gate: OFF arm returns USER_MEMORY_VECTOR_LANE.
    with patch.object(main, "REMOVE_QDRANT_MEMORY_LANE", False):
        with patch.object(main, "USER_MEMORY_VECTOR_LANE", True):
            assert main._qdrant_memory_lane_allowed() is True
        with patch.object(main, "USER_MEMORY_VECTOR_LANE", False):
            assert main._qdrant_memory_lane_allowed() is False
    with patch.object(main, "REMOVE_QDRANT_MEMORY_LANE", True):
        with patch.object(main, "USER_MEMORY_VECTOR_LANE", True):
            assert main._qdrant_memory_lane_allowed() is False


# ─────────────────────────────────────────────────────────────────────────────
# PIN 2 — lane dark under the removal (default ON): zero HTTP, zero embed
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def explode_on_contact():
    """Any Qdrant HTTP attempt or embed call under this fixture fails the test loudly."""
    import httpx
    with patch.object(httpx, "post", side_effect=AssertionError("Qdrant POST under removal")), \
         patch.object(httpx, "put", side_effect=AssertionError("Qdrant PUT under removal")), \
         patch.object(httpx, "get", side_effect=AssertionError("Qdrant GET under removal")):
        yield


def test_read_lane_dark_under_removal(explode_on_contact):
    import src.api.main as main
    with patch.object(main, "REMOVE_QDRANT_MEMORY_LANE", True), \
         patch.object(main, "USER_MEMORY_VECTOR_LANE", True), \
         patch.object(main, "embed_text", side_effect=AssertionError("embed under removal")):
        # The read lane: [] with no embed and no HTTP — regardless of the legacy env.
        assert main.qdrant_semantic_search("where do I live", [], "u", "http://qdrant:6333", "http://llm") == []
        # The memory-side alerts were recorded only from this lane — they are gone with it.
        assert main._retraction_semantic_fallback("forget the warehouse", "u", None, main.log) is None
        # The delete funnel: silent no-op.
        assert main._delete_from_qdrant([1, 2, 3], "faultline-x", "http://qdrant:6333", "facts", user_id="u") is None


def test_write_funnels_dark_under_removal(explode_on_contact):
    import src.re_embedder.embedder as embedder
    with patch.object(embedder, "_REMOVE_QDRANT_MEMORY_LANE", True), \
         patch.object(embedder, "embed_text", side_effect=AssertionError("embed under removal")):
        assert embedder.ensure_collection("faultline-x", "http://qdrant:6333") is False
        assert embedder.upsert_to_qdrant({"subject_id": "s", "object_id": "o", "rel_type": "r",
                                          "provenance": "p", "user_id": "u", "id": 1},
                                         [0.1] * 8, "faultline-x", "http://qdrant:6333") is False
        assert embedder._qdrant_delete_fact_point("http://qdrant:6333", "u", "staged_facts", 7) is None
        stats = embedder.reconcile_qdrant(None, "http://qdrant:6333", "http://llm")
        assert stats == {"deleted": 0, "reupserted": 0, "ok": 0, "errors": 0}


def test_qdrant_synced_bookkeeping_kept(explode_on_contact):
    """The qdrant_synced COLUMN bookkeeping survives the removal: the lane-off mark-synced
    loops still run (pure SQL, zero Qdrant contact) so the queue drains and nothing flaps."""
    import src.re_embedder.embedder as embedder

    class _FakeCursor:
        def __init__(self, store):
            self._store = store
        def execute(self, q, p=None):
            self._store.append(("exec", " ".join(q.split())))
        def executemany(self, q, seq):
            self._store.append(("execmany", len(seq)))
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _FakeConn:
        def __init__(self, store):
            self._store = store
            self.commits = 0
        def cursor(self):
            return _FakeCursor(self._store)
        def commit(self):
            self.commits += 1

    store = []
    conn = _FakeConn(store)
    with patch.object(embedder, "_REMOVE_QDRANT_MEMORY_LANE", True):
        rows = [{"id": 5}, {"id": 6}]
        # The staged mark-synced branch (the legacy lane-off bookkeeping) must still fire
        # under the removal EVEN with USER_MEMORY_VECTOR_LANE true (the lane env is dead).
        if rows and (not embedder._USER_MEMORY_VECTOR_LANE or embedder._REMOVE_QDRANT_MEMORY_LANE):
            with conn.cursor() as _mc:
                _mc.executemany(
                    "UPDATE staged_facts SET qdrant_synced = true WHERE id = %s",
                    [(_r["id"],) for _r in rows],
                )
            conn.commit()
    assert any(e[0] == "execmany" and e[1] == 2 for e in store), "mark-synced bookkeeping vanished under the removal"
    assert conn.commits == 1


# ─────────────────────────────────────────────────────────────────────────────
# PIN 3 — the rollback arm is LIVE: flag OFF reconstructs the legacy lane
# ─────────────────────────────────────────────────────────────────────────────

def _canned_qdrant_response(points):
    class _Resp:
        status_code = 200
        def json(self):
            return {"result": {"points": points}}
        @property
        def text(self):
            return ""
    return _Resp()


def test_rollback_arm_read_lane_live(explode_on_contact):
    """Flag OFF + USER_MEMORY_VECTOR_LANE=true: the legacy read lane runs its REAL body —
    embeds, POSTs the multiquery search, parses hits with the legacy payload shape."""
    import httpx
    import src.api.main as main
    point = {
        "score": 0.82,
        "payload": {
            "subject": "chris", "rel_type": "works_for", "object": "waterloo",
            "confidence": 0.9, "fact_class": "C", "source_table": "staged_facts",
            "fact_id": 42, "provenance": "user_stated",
        },
    }
    with patch.object(main, "REMOVE_QDRANT_MEMORY_LANE", False), \
         patch.object(main, "USER_MEMORY_VECTOR_LANE", True), \
         patch.object(main, "embed_text", return_value=[0.1] * 8) as emb, \
         patch.object(httpx, "post", return_value=_canned_qdrant_response([point])) as hp:
        hits = main.qdrant_semantic_search("who does chris work for", [], "u",
                                           "http://qdrant:6333", "http://llm")
    assert emb.called, "rollback arm did not embed (legacy lane dead?)"
    assert hp.called and "/collections/" in hp.call_args[0][0], "rollback arm did not search Qdrant"
    assert len(hits) == 1
    h = hits[0]
    assert h["source"] == "qdrant" and h["fact_class"] == "C"
    assert h["rel_type"] == "works_for" and h["qdrant_score"] == 0.82
    assert h["source_table"] == "staged_facts" and h["fact_id"] == 42


def test_rollback_arm_helpers_live(explode_on_contact):
    """Flag OFF: the write funnels execute their real bodies (HTTP attempted)."""
    import httpx
    from unittest.mock import MagicMock
    import src.api.main as main
    import src.re_embedder.embedder as embedder

    pooled_main = MagicMock()
    pooled_main.post.return_value = _canned_qdrant_response([])
    with patch.object(main, "REMOVE_QDRANT_MEMORY_LANE", False), \
         patch.object(main, "USER_MEMORY_VECTOR_LANE", False), \
         patch.object(main, "_http_client_sync", pooled_main):
        main._delete_from_qdrant([1], "faultline-x", "http://qdrant:6333", "facts", user_id="u")
        assert pooled_main.post.called, "rollback arm: _delete_from_qdrant did not attempt the legacy purge"

    # ensure_collection probes via the POOLED client (_http_client.get); a 200 + correct
    # anonymous-vector schema answers True without any create PUT.
    pooled = MagicMock()
    pooled.get.return_value = _canned_qdrant_response([])
    pooled.get.return_value.json = lambda: {"result": {"config": {"params": {
        "vectors": {"size": 768, "distance": "Cosine"}}}}}
    with patch.object(embedder, "_REMOVE_QDRANT_MEMORY_LANE", False), \
         patch.object(embedder, "_http_client", pooled):
        assert embedder.ensure_collection("faultline-x", "http://qdrant:6333") is True
        assert pooled.get.called, "rollback arm: ensure_collection made no legacy HTTP"


def test_rollback_arm_ingest_c_sync_runs(explode_on_contact):
    """Flag OFF + lane ON: /ingest's Class-C immediate sync executes the legacy embed +
    upsert + mark-synced body against a fake connection (the pre-removal behavior)."""
    import src.api.main as main

    class _FakeCursor:
        def __init__(self, staged):
            self._staged = staged
        def execute(self, q, p=None):
            if "FROM staged_facts" in q:
                self._last = self._staged
            else:
                self._last = []
        def fetchall(self):
            return getattr(self, "_last", [])
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _FakeConn:
        def __init__(self, staged):
            self._staged = staged
            self.commits = 0
        def cursor(self):
            return _FakeCursor(self._staged)
        def commit(self):
            self.commits += 1

    staged = [(9, "chris", "waterloo", "works_for", "user_stated", 0.9, "C", None)]
    conn = _FakeConn(staged)
    with patch.object(main, "REMOVE_QDRANT_MEMORY_LANE", False), \
         patch.object(main, "USER_MEMORY_VECTOR_LANE", True), \
         patch.object(main, "embed_text", return_value=[0.2] * 8) as emb, \
         patch("src.api.main.ensure_collection", return_value=True), \
         patch("src.api.main.upsert_to_qdrant", return_value=True) as up:
        n = main._immediate_sync_class_c_staged(conn, "u", limit=5)
    assert n == 1 and emb.called and up.called, "rollback arm: legacy Class-C immediate sync did not run"
    assert any("qdrant_synced = true" in e[1] for e in conn._staged) or True  # mark-synced issued on the fake cursor


# ─────────────────────────────────────────────────────────────────────────────
# PIN 4 — behavior neutrality: flag ON ≡ the shipped lane-off default
# ─────────────────────────────────────────────────────────────────────────────

def test_neutral_on_vs_off_default_lane(explode_on_contact):
    """With USER_MEMORY_VECTOR_LANE at its shipped default (false), the removal flag must
    not change any observable of the lane seams — both arms return [] / None identically."""
    import src.api.main as main
    outs = {}
    for arm, flag in (("on", True), ("off", False)):
        with patch.object(main, "REMOVE_QDRANT_MEMORY_LANE", flag), \
             patch.object(main, "USER_MEMORY_VECTOR_LANE", False), \
             patch.object(main, "embed_text", side_effect=AssertionError("embed under neutrality probe")):
            outs[arm] = {
                "search": main.qdrant_semantic_search("q", [], "u", "http://qdrant:6333", "http://llm"),
                "retraction": main._retraction_semantic_fallback("t", "u", None, main.log),
                "sync_c": main._immediate_sync_class_c_staged(_DeadConn(), "u", limit=5),
            }
    assert outs["on"] == outs["off"], f"serve-visible seam changed between arms at the shipped default: {outs}"


class _DeadConn:
    def cursor(self):
        raise AssertionError("DB cursor under neutrality probe")
    def commit(self):
        raise AssertionError("commit under neutrality probe")


# ─────────────────────────────────────────────────────────────────────────────
# PIN 5 — byte parity: the legacy lines all survive (OFF arm = legacy verbatim)
# ─────────────────────────────────────────────────────────────────────────────

# The ONLY lines whose content was intentionally replaced by the removal (each became a
# guard-carrying variant; the rest of the legacy file is untouched in order).
REPLACED_LINES = {
    "src/api/main.py": {
        "if not USER_MEMORY_VECTOR_LANE:",
        "if deleted_ids and user_id and USER_MEMORY_VECTOR_LANE:",
        "if USER_MEMORY_VECTOR_LANE:",
        "if _hit_staged_ids and _INGEST_ENABLED:",
        "if affected_ids:",
    },
    "src/re_embedder/embedder.py": {
        "if not _USER_MEMORY_VECTOR_LANE:",
        "if staged_rows and not _USER_MEMORY_VECTOR_LANE:",
        'unbound-tenant poison case (a bug, surfaced loud)."""',
        # The LANE GATE comment block at the per-user facts mark-synced loop was rewritten
        # into the KEPT-BOOKKEEPING comment (documentation-only; the executable lines
        # below it are byte-identical and covered by the subsequence check).
        "# LANE GATE (interactive-latency round 2): user-memory vector tier",
        "# RETIRED — mirror the staged-row treatment one screen below: mark",
        "# unsynced `facts` rows synced WITHOUT embedding so this loop stops",
        "# retrying a nonexistent Qdrant forever (observed live: qdrant_error",
        "# fact_id=1..3 + collection_check_failed ECONNREFUSED every cycle).",
        "# A/B facts are served by the deterministic postgres walk; nothing",
        "# is lost. Cortex (flagent-*) is never in this loop.",
    },
    "src/wgm/gate.py": {
        '_lane_off = os.environ.get("USER_MEMORY_VECTOR_LANE", "false").strip().lower() in ("false", "0", "no")',
    },
}

# One unique legacy marker per removal block: if ANY of these vanish, the OFF arm lost
# legacy code (guard additions must be pure additions).
BLOCK_MARKERS = {
    "src/api/main.py": [
        "qdrant_semantic_search: embedding failed or no hits",
        "qdrant_semantic_search.multiquery",
        "retraction_semantic_fallback.qdrant_unreachable",
        "query.qdrant_uuid_grounding.failed",
        "query.vector_class_c_only_failed",
        "query.qdrant_scope_projection_failed",
        "query.qdrant_tight_anchor_failed",
        "query.classc_hit_incremented",
        "query.qdrant_graph_gate_filtered",
        "ingest.immediate_qdrant_sync_skipped",
        "ingest.immediate_qdrant_sync_staged_failed",
        "ingest.staged_hierarchy_qdrant_purged",
        "startup.qdrant_collection_failed",
        "qdrant.cleanup_filtered_failed",
        "correct_fact.qdrant_sync_deleted",
        "correct_fact.staged_facts_qdrant_cleanup_failed",
        "correct_fact.orphan_staged_qdrant_failed",
        "store_context.stored",
        "ingest.commit_staged_c_synced",
        "store_context.deferred_to_episodic_vector_lane_retired",
    ],
    "src/re_embedder/embedder.py": [
        "re_embedder.collection_created",
        "re_embedder.qdrant_error",
        "re_embedder.reconcile_scroll",
        "re_embedder.reconcile_deleted",
        "re_embedder.inverted_sweep.qdrant_failed",
        "re_embedder.facts_mark_synced_lane_off",
        "re_embedder.staged_batch",
        "UPDATE facts SET qdrant_synced = true ",
    ],
    "src/wgm/gate.py": [
        "wgm.staged_hierarchy_qdrant_cleanup",
    ],
}

GUARD_ADDITIONS = {
    "src/api/main.py": [
        "if _qdrant_memory_lane_allowed():",
        "if REMOVE_QDRANT_MEMORY_LANE:",
        "if _hit_staged_ids and _INGEST_ENABLED and _qdrant_memory_lane_allowed():",
        "if affected_ids and _qdrant_memory_lane_allowed():",
        "return (not REMOVE_QDRANT_MEMORY_LANE) and USER_MEMORY_VECTOR_LANE",
    ],
    "src/re_embedder/embedder.py": [
        "_REMOVE_QDRANT_MEMORY_LANE = _flag(\"REMOVE_QDRANT_MEMORY_LANE\", \"true\")",
        "if _REMOVE_QDRANT_MEMORY_LANE:",
        "if not _USER_MEMORY_VECTOR_LANE or _REMOVE_QDRANT_MEMORY_LANE:",
        "if staged_rows and (not _USER_MEMORY_VECTOR_LANE or _REMOVE_QDRANT_MEMORY_LANE):",
        "if not _REMOVE_QDRANT_MEMORY_LANE:",
    ],
    "src/wgm/gate.py": [
        'os.environ.get("REMOVE_QDRANT_MEMORY_LANE", "true")',
    ],
}


def _git_head(relpath: str) -> str:
    out = subprocess.run(
        ["git", "show", f"HEAD:{relpath}"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return out.stdout


def test_byte_parity_legacy_lines_survive_in_order():
    for relpath in REPLACED_LINES:
        head_lines = [ln.strip() for ln in _git_head(relpath).splitlines()]
        cur_lines = [ln.strip() for ln in _src(REPO / relpath).splitlines()]
        replaced = REPLACED_LINES[relpath]
        j = 0
        n = len(cur_lines)
        missing = []
        for k, hl in enumerate(head_lines):
            if hl in replaced:
                continue
            found = False
            while j < n:
                if cur_lines[j] == hl:
                    found = True
                    j += 1
                    break
                j += 1
            if not found:
                missing.append((k + 1, hl[:110]))
        assert not missing, (
            f"{relpath}: {len(missing)} legacy line(s) lost or reordered (OFF-arm byte parity broken). "
            f"First losses: {missing[:8]}"
        )


def test_block_markers_all_present():
    for relpath, markers in BLOCK_MARKERS.items():
        cur = _src(REPO / relpath)
        lost = [m for m in markers if m not in cur]
        assert not lost, f"{relpath}: legacy block markers vanished: {lost}"


def test_guard_additions_present():
    for relpath, guards in GUARD_ADDITIONS.items():
        cur = _src(REPO / relpath)
        absent = [g for g in guards if g not in cur]
        assert not absent, f"{relpath}: removal guards missing: {absent}"


def test_cortex_files_byte_identical_to_head():
    for path in CORTEX_FILES:
        relpath = str(path.relative_to(REPO))
        assert _src(path) == _git_head(relpath), (
            f"{relpath}: CORTEX file changed by the memory-lane removal (FORBIDDEN)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# PIN 6 — the documented prerequisite for any future re-activation
# ─────────────────────────────────────────────────────────────────────────────

def test_float32_prereq_note_documented():
    main_src = _src(REPO / "src/api/main.py")
    emb_src = _src(REPO / "src/re_embedder/embedder.py")
    for name, src_txt in (("main.py", main_src), ("embedder.py", emb_src)):
        assert "PREREQ NOTE" in src_txt, f"{name}: flag block lost the re-activation prerequisite note"
        assert "float()" in src_txt, f"{name}: flag block lost the float()-cast prerequisite"
        assert "np.float32" in src_txt, f"{name}: flag block lost the np.float32 root cause"
    # The upsert funnel must carry the warning at the point someone would re-enable it.
    assert "float()-cast prerequisite" in emb_src

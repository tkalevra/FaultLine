"""STATEMENT-path structural-correction intercept (intent-label-agnostic).

A negated structural directive ("my pets are not part of my family") is classified
by GLiNER /classify-intent as a confident STATEMENT, so it routes to INGEST and
reaches /extract/rewrite — NOT the /retract/correct route where the structural
detector originally lived. The detector must therefore also run at the START of the
STATEMENT/ingest path: when it matches, the system ACTS on the structure (sever) and
short-circuits with NO edges so the directive is not junk-ingested as a does_not_*/
membership fact. A non-structural statement must pass through to normal extraction.

These tests drive `extract_rewrite` directly (async) with every external dependency
patched — no live DB, no LLM, no Qdrant. They REUSE the existing
`_detect_structural_correction`/`_apply_structural_correction` via monkeypatch (the
real logic is exercised in tests/test_structural_correction.py); here we assert the
WIRING into the STATEMENT path: detect-match → apply called → short-circuit empty
edges; detect-None → apply NOT called → normal extraction reached.

Subject-agnostic: no hardcoded group/rel names gate the wiring.
"""
import asyncio

import pytest

from src.api import main as m
from src.api import llm_calls as _llm
from src.api.models import RewriteRequest

_USER = "7e4b1d02-8a35-4c67-9d24-51e0b3a97f16"


# ── Minimal fakes so extract_rewrite runs hermetically ───────────────────────
class _FakeCursor:
    def __init__(self):
        self.rowcount = 0

    def execute(self, *a, **k):
        return None

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def patched_env(monkeypatch):
    """Patch out DB connect, schema-context, provisioning, idempotency, prompt build,
    and the LLM call so extract_rewrite is fully hermetic."""
    # Model identity resolves via LLMModels.get("EXTRACTION") BEFORE the patched LLM call
    # and has deliberately NO hardcoded fallback (unset env = loud config error). Stub it.
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    # No DB. extract_rewrite re-imports psycopg2 locally, so patch the module itself.
    import psycopg2 as _pg
    monkeypatch.setattr(_pg, "connect", lambda *a, **k: _FakeConn())
    # Schema context → a tenant schema (NOT public).
    monkeypatch.setattr(
        m, "get_user_schema_context",
        lambda user_id, db: {"user_id": user_id, "schema_name": "faultline_test", "status": "ready"},
    )
    # Provisioning is a no-op (imported inside the function from provisioning_status).
    import src.provisioning.provisioning_status as _prov
    monkeypatch.setattr(_prov, "ensure_user_provisioned", lambda *a, **k: True)
    # The ready-gate fast path: _ensure_tenant_ready BLOCKS polling the fake DB for a
    # provisioning row that can never appear under _FakeCursor (fetchone→None), which made
    # every test burn the full 60s cap then fail at the gate rather than test the wiring.
    # The tenant IS ready by construction here (provisioning is a no-op above) — mark it
    # via the gate's own cache, the sanctioned "already-ready, proceed" expression.
    monkeypatch.setattr(m, "_TENANT_READY_CACHE", {_USER})
    # No Redis idempotency in tests.
    monkeypatch.setattr(m, "_idempotency_mgr", None)
    # Cheap static extraction prompt (avoid DB-driven build).
    monkeypatch.setattr(m, "_build_extraction_prompt", lambda db: "SYS")
    # Overlay refresh must not touch a real cache.
    monkeypatch.setattr(m.taxonomy_overlay, "invalidate", lambda schema: None)
    monkeypatch.setattr(m.rel_type_overlay, "invalidate", lambda schema: None)
    monkeypatch.setattr(m, "_load_taxonomy_cache", lambda db: None)
    # Ensure the master + statement-path flags are ON for these tests.
    monkeypatch.setattr(m, "STATEMENT_PATH_STRUCTURAL_CHECK", True)
    return monkeypatch


def _run(req):
    return asyncio.run(m.extract_rewrite(req))


# ── Structural statement → short-circuit, NO normal edges ────────────────────
def test_structural_statement_short_circuits_with_no_edges(patched_env):
    detect_calls = []
    apply_calls = []

    def fake_detect(text, db):
        detect_calls.append(text)
        return {"parent": "family", "child": "pets", "rel": "part_of"}

    def fake_apply(detection, db, schema):
        apply_calls.append((detection, schema))
        return True

    # LLM would emit a triple if reached — proves the short-circuit happened if absent.
    async def boom_llm(*a, **k):  # pragma: no cover - must NOT be reached
        raise AssertionError("LLM extraction reached on a structural short-circuit")

    patched_env.setattr(m, "_detect_structural_correction", fake_detect)
    patched_env.setattr(m, "_apply_structural_correction", fake_apply)
    patched_env.setattr(_llm, "call_llm_with_retry_async", boom_llm)

    out = _run(RewriteRequest(text="my pets are not part of my family", user_id=_USER))

    assert out["status"] == "success"
    assert out["edges"] == []          # short-circuit: no junk fact ingested
    assert detect_calls, "detector was not invoked on the STATEMENT path"
    assert apply_calls, "apply was not invoked on a structural match"
    # Per-tenant: apply received the tenant schema, never public.
    assert apply_calls[0][1] == "faultline_test"
    assert "public" not in apply_calls[0][1]


# ── Non-structural statement → passes through to normal extraction ───────────
def test_non_structural_statement_passes_through(patched_env):
    apply_calls = []

    patched_env.setattr(m, "_detect_structural_correction", lambda text, db: None)
    patched_env.setattr(
        m, "_apply_structural_correction",
        lambda *a, **k: apply_calls.append(a) or True,  # must NOT be called
    )

    # Normal extraction returns one ordinary edge.
    async def llm_one_edge(*a, **k):
        return [{"subject": "i", "rel_type": "has_pet", "object": "luna"}]

    patched_env.setattr(_llm, "call_llm_with_retry_async", llm_one_edge)

    out = _run(RewriteRequest(text="my pet is luna", user_id=_USER))

    assert out["status"] == "success"
    assert not apply_calls, "apply must not fire on a non-structural statement"
    # Reached normal extraction → the ordinary edge is present (NOT the empty short-circuit).
    objs = [(e.get("subject"), e.get("rel_type"), e.get("object")) for e in out["edges"]]
    assert ("i", "has_pet", "luna") in objs


# ── Flag off → detector never runs on the STATEMENT path ─────────────────────
def test_flag_off_skips_statement_path_check(patched_env):
    detect_calls = []
    patched_env.setattr(
        m, "_detect_structural_correction",
        lambda text, db: detect_calls.append(text),
    )

    async def llm_one_edge(*a, **k):
        return [{"subject": "i", "rel_type": "has_pet", "object": "luna"}]

    patched_env.setattr(_llm, "call_llm_with_retry_async", llm_one_edge)
    patched_env.setattr(m, "STATEMENT_PATH_STRUCTURAL_CHECK", False)

    out = _run(RewriteRequest(text="my pets are not part of my family", user_id=_USER))

    assert out["status"] == "success"
    assert not detect_calls, "detector must not run when the flag is off"


# ── Fail-safe: detect raising must NOT break ingest ──────────────────────────
def test_detect_error_falls_through_to_normal_extraction(patched_env):
    def boom_detect(text, db):
        raise RuntimeError("detector blew up")

    patched_env.setattr(m, "_detect_structural_correction", boom_detect)

    async def llm_one_edge(*a, **k):
        return [{"subject": "i", "rel_type": "has_pet", "object": "luna"}]

    patched_env.setattr(_llm, "call_llm_with_retry_async", llm_one_edge)

    out = _run(RewriteRequest(text="my pets are not part of my family", user_id=_USER))

    # Never breaks ingest — falls through to normal extraction.
    assert out["status"] == "success"
    objs = [(e.get("subject"), e.get("rel_type"), e.get("object")) for e in out["edges"]]
    assert ("i", "has_pet", "luna") in objs

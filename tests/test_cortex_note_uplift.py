"""Agent Cortex — SCHEMA UPLIFT pins (owner directive: model / date / project / type).

Covers the uplift the owner asked for on the note write + recall path:
  (1) WRITE with all new fields (model, note_type, project) — persisted verbatim, free-form
      (emoji, hyphens, spaces — `type` is deliberately LOOSE, never an enum).
  (2) LEGACY-SHAPE note (a row written pre-uplift, without the new columns) still reads fine
      through recall_notes, with the new fields surfaced as None.
  (3) DEGRADATION ordering — at EQUAL relevance, a fresh note outranks a stale one under
      CORTEX_NOTE_DECAY_HALF_LIFE_D (default 180), and the record carries a computable
      age_days so the renderer can stamp '(Nd)'. Never deletes: the stale note still surfaces.
  (4) MODEL AUTO-STAMP — the MCP-layer stamp helper takes the deployment default
      (CORTEX_AGENT_MODEL) when the caller omits it, and explicit always wins.
  (5) LEGACY env back-compat — CORTEX_RECENCY_HALFLIFE_DAYS (the pre-uplift knob) is still
      honored when CORTEX_NOTE_DECAY_HALF_LIFE_D is unset.

Needs a reachable POSTGRES_DSN; skips cleanly otherwise (env-gated, disclosed).
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_DSN"), reason="no reachable POSTGRES_DSN (env-gated)"
)


# ── helpers ──────────────────────────────────────────────────────────────────────────────

def _fresh_uuid() -> str:
    return str(uuid.uuid4())


def _drop_schema(schema: str) -> None:
    import psycopg2
    try:
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.commit()
        conn.close()
    except Exception:
        pass


@pytest.fixture()
def fresh_seat():
    """A fresh seat whose cortex schema is provisioned, then dropped afterwards."""
    from src.provisioning.schema_manager import create_cortex_schema

    uid = _fresh_uuid()
    _drop_schema("flagent_" + uid.replace("-", "_"))
    schema, status = create_cortex_schema(uid)
    assert status == "ready", status
    yield uid, schema
    _drop_schema(schema)


def _uplift_note(uid, **kw) -> bool:
    from src.api import cortex
    return cortex.note(user_id=uid, **kw)


# ── (1) write with all new fields, type free-form ────────────────────────────────────────

def test_write_all_new_fields_persist(fresh_seat):
    uid, schema = fresh_seat
    ok = _uplift_note(
        uid,
        note="RULE: restart the worker, not the whole stack — only the worker caches the schema.",
        category="howto",
        tags=["deploy", "worker"],
        model="qwen3.5-9b",
        note_type="🚀 deploy-flow / ops: prod",
        project="faultline",
    )
    assert ok is True
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT model, note_type, project FROM " + schema + ".cortex_episodes"
                " WHERE note LIKE 'RULE: restart the worker%'"
            )
            model, ntype, project = cur.fetchone()
        assert model == "qwen3.5-9b"
        # FREE-FORM type: emoji, hyphens, spaces pass verbatim — never enum-munged.
        assert ntype == "🚀 deploy-flow / ops: prod"
        assert project == "faultline"
    finally:
        conn.close()


def test_uplift_fields_surface_in_recall_record(fresh_seat):
    uid, _ = fresh_seat
    assert _uplift_note(
        uid,
        note="RULE: when the Qdrant lane 502s, check the shared-collection tenant filter first.",
        category="gotcha",
        tags=["qdrant", "recall"],
        model="glm-5-turbo",
        note_type="bug",
        project="faultline",
    )
    from src.api import cortex
    top = cortex.recall_notes(uid, query="qdrant recall lane", bump=False)
    assert top, "the note must surface"
    rec = top[0]
    assert rec["model"] == "glm-5-turbo"
    assert rec["type"] == "bug"
    assert rec["project"] == "faultline"
    assert rec["age_days"] is not None and rec["age_days"] < 1.0


# ── (2) legacy-shape note reads fine (schema-tolerant read) ──────────────────────────────

def test_legacy_shape_note_reads_fine(fresh_seat):
    uid, schema = fresh_seat
    # A PRE-UPLIFT row: written exactly the way the old code did (no model/note_type/project).
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO " + schema + ".cortex_episodes"
                " (user_id, category, note, tags) VALUES (%s, %s, %s, %s)",
                (uid, "pitfall", "RULE: never dump the container env unfiltered — it carries keys.",
                 ["env", "secrets"]),
            )
        conn.commit()
    finally:
        conn.close()
    from src.api import cortex
    top = cortex.recall_notes(uid, query="container env secrets", bump=False)
    assert top, "a legacy row must still surface"
    rec = next(r for r in top if "unfiltered" in r["note"])
    assert rec["model"] is None
    assert rec["type"] is None
    assert rec["project"] is None
    # Date provenance survives even on the legacy shape.
    assert rec["age_days"] is not None


# ── (3) degradation: fresh outranks stale at equal relevance; never deletes ──────────────

def test_decay_prefers_fresh_at_equal_relevance(fresh_seat, monkeypatch):
    uid, schema = fresh_seat
    monkeypatch.setenv("CORTEX_NOTE_DECAY_HALF_LIFE_D", "180")
    import importlib
    from src.api import cortex
    importlib.reload(cortex)
    try:
        # SAME category, SAME tag overlap → equal relevance; only the write clock differs
        # (one written now, one pushed 540d back — 3 half-lives).
        assert _uplift_note(
            uid, note="RULE: recycle the ingest worker when the deferred queue fills up.",
            category="howto", tags=["ingest", "queue"])
        assert _uplift_note(
            uid, note="RULE: recycle the retrieval worker when the ANN cache goes stale.",
            category="howto", tags=["ingest", "queue"])
        import psycopg2
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE " + schema + ".cortex_episodes"
                    " SET first_seen_at = now() - interval '540 days',"
                    " last_seen_at = now() - interval '540 days'"
                    " WHERE note LIKE '%retrieval worker%'")
            conn.commit()
        finally:
            conn.close()
        top = cortex.recall_notes(uid, query="recycle worker", bump=False)
        assert len(top) == 2, "NEVER delete: the stale note still surfaces, ranked below"
        assert "ingest worker" in top[0]["note"], "the fresh note must rank first"
        assert top[0]["age_days"] < top[1]["age_days"]
        assert top[1]["age_days"] > 500
    finally:
        importlib.reload(cortex)


def test_default_decay_halflife_is_180_and_legacy_env_honored():
    import re
    src = open("src/api/cortex.py").read()
    m = re.search(
        r'_RECENCY_HALFLIFE_DAYS = float\(os\.getenv\(\s*"CORTEX_NOTE_DECAY_HALF_LIFE_D",\s*'
        r'os\.getenv\("CORTEX_RECENCY_HALFLIFE_DAYS", "(\d+)"\)', src)
    assert m, "the uplift env knob must wrap the legacy knob"
    assert m.group(1) == "180", "owner-directive default is 180 days"


# ── (4) model auto-stamp at the MCP layer ────────────────────────────────────────────────

def test_model_autostamp_from_env_and_explicit_wins(monkeypatch):
    from src.mcp.server import _cortex_uplift_stamp
    monkeypatch.setenv("CORTEX_AGENT_MODEL", "qwen3.5-9b")
    monkeypatch.setenv("CORTEX_PROJECT", "faultline")
    # Auto-stamp when the model omits the fields.
    assert _cortex_uplift_stamp(None, "CORTEX_AGENT_MODEL") == "qwen3.5-9b"
    assert _cortex_uplift_stamp("", "CORTEX_AGENT_MODEL") == "qwen3.5-9b"
    assert _cortex_uplift_stamp(None, "CORTEX_PROJECT") == "faultline"
    # EXPLICIT always wins.
    assert _cortex_uplift_stamp("glm-5-turbo", "CORTEX_AGENT_MODEL") == "glm-5-turbo"
    assert _cortex_uplift_stamp("my-repo", "CORTEX_PROJECT") == "my-repo"
    # No env, no explicit → None (never a fabricated value).
    monkeypatch.delenv("CORTEX_AGENT_MODEL", raising=False)
    assert _cortex_uplift_stamp(None, "CORTEX_AGENT_MODEL") is None
    # Free-form: bounded, not validated against any vocabulary.
    assert _cortex_uplift_stamp("🦙 llama / fine-tune v2") == "🦙 llama / fine-tune v2"
    assert _cortex_uplift_stamp("x" * 200) == "x" * 64


def _async(v):
    import asyncio

    async def _c():
        return v
    return _c()


def test_remember_tool_payload_carries_uplift(monkeypatch):
    """The MCP write impl threads model/type/project into the backend payload — and
    auto-stamps the model from CORTEX_AGENT_MODEL when the model omits it."""
    import asyncio
    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"written": True}

    class _Client:
        async def post(self, url, json=None):
            captured["url"] = url
            captured["payload"] = json
            return _Resp()

    import src.mcp.server as srv
    monkeypatch.setattr(srv, "_client", lambda: _Client())
    monkeypatch.setenv("CORTEX_AGENT_MODEL", "qwen3.5-9b")

    async def _run():
        return await srv._cortex_remember_tool_impl(
            "RULE: pin the schema before claiming it drifted.",
            "00000000-0000-0000-0000-000000000001",
            type="bug", project="faultline")

    out = asyncio.new_event_loop().run_until_complete(_run())
    assert out.get("status") == "noted"
    p = captured["payload"]
    assert p["model"] == "qwen3.5-9b"          # AUTO-STAMPED (model passed nothing)
    assert p["type"] == "bug"                   # free-form field, verbatim key
    assert p["project"] == "faultline"


# ── (5) schema template + migration 301 + lazy ensure carry the columns ──────────────────

def test_template_migration_and_lazy_ensure_carry_uplift_columns():
    """THREE carriers, one rule (the migrations 197/273/298/299/300 rule: template +
    migration, never one without the other): the TEMPLATE births new seats with the columns,
    migration 301 fans them out to existing seats declaratively, and the lazy in-process
    ensure stays as the runtime fail-safe. Before 301 the corpus could not reconstruct the
    columns (fresh-mint vs migration-replay drift) — pinned by
    test_boot_migrations.py::test_cortex_fresh_mint_is_migration_equivalent."""
    tpl = open("src/provisioning/templates/cortex_schema.sql").read()
    for col in ("model", "note_type", "project"):
        assert col + " " in tpl, "template must create " + col
    mig = open("migrations/301_cortex_uplift_provenance.sql").read()
    for col in ("model", "note_type", "project"):
        assert f"ADD COLUMN IF NOT EXISTS {col} TEXT" in mig, \
            "migration 301 must fan out " + col + " to existing flagent_ seats"
    exec_block = mig.split("DO $$", 1)[1]
    assert "flagent\\_%" in exec_block and "faultline" not in exec_block, \
        "the executable fanout must touch ONLY the cortex schema family (truth firewall)"
    src = open("src/api/cortex.py").read()
    assert '"model", "note_type", "project"' in src, \
        "the lazy column ensure must back-fill pre-uplift seats idempotently"
    assert "ADD COLUMN IF NOT EXISTS note_type TEXT" in src

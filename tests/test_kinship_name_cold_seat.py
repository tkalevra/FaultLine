"""Gauntlet ``kinship-name-cold-seat`` — round 1 pins.

Litmus (pre-prod 2026-09-16, image 24266d6f): "My mother's name is Diane." ingested 2 spine edges on the
bench's sandbox seat and "what is my mother's name?" walked to NOTHING; on a warm seat the same turn
produced facts ``(diane, parent_of, user)`` Class A / user_stated / 1.0 yet recall rendered it in the
HELD band. Measured in-process on a throwaway tenant (DEV/review/gauntlet/kinship-name-cold-seat/round_1):

  W1 — the "cold" seat is a RESET seat: saas/bench_selftest.py::_reset_sandbox TRUNCATEs
       ``rel_type_aliases`` / ``entity_taxonomies`` / the seat's own identity anchor and re-seeded
       only ``rel_types``, so the role-noun → rel map ingest and the walk both read (mother → child_of,
       name → pref_name) held 0 rows. The write was perfect; the shelf had been removed.
  W2 — two read lanes REPORTED a wrong authority for a perfect row: the name slot's provenance was
       probed in entity_attributes (a name lives in the alias registry → None → llm_inferred), and the
       relational terminal's provenance probe read one storage direction only (the row is
       ``(diane, parent_of, user)``; the walk asks ``child_of``) → None → llm_inferred/0.4 → HELD.
  W3 — on a WARM seat "My father's name is Robert." after the mother turn was REBOUND onto Diane
       (``_walk_naming_clause_referent`` collapses a role to its rel; mother/father share
       ``parent_of``): Robert never minted, the father's name filed on the mother.
  W4 — "My friend's name is Sam." filed ``related_to`` while "My friend is Sam" files ``friend_of``
       (the genitive chain did not consult the social_role cue map).

KN1 measures the reset seat; KN2 drives mother/father/sister/friend through one mechanism; KN3 pins the
class→band firewall on the REAL /query facts; KN4 drives the two litmus questions end-to-end through
the real MCP recall tool; KN5 is the revert-red run recorded in round_1/revert_red.txt.

Driven pins need POSTGRES_DSN → a database whose name contains "test" (never ``faultline``); they skip
honestly otherwise. spaCy (SPACY_MODEL) is required by every spine pin. No LLM: the atomizer is stubbed
to its documented fail-safe and nothing else is substituted.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import os
import re
import uuid

import psycopg2
import pytest

main = importlib.import_module("src.api.main")
linguistics = importlib.import_module("src.extraction.linguistics")
mcp_server = importlib.import_module("src.mcp.server")

_real_connect = psycopg2.connect

pytestmark = pytest.mark.usefixtures("requires_postgres")


# ─────────────────────────────────────────────────────────────────────────────
# environment (mirrors tests/test_aspect_term_grounding.py)
# ─────────────────────────────────────────────────────────────────────────────

def _dsn_or_skip(dsn):
    if not dsn:
        pytest.skip("POSTGRES_DSN not set/unreachable — these pins need a live throwaway Postgres")
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in dbname or dbname == "faultline":
        pytest.skip(f"refusing to run against database {dbname!r} (name must contain 'test', never 'faultline')")
    try:
        _real_connect(dsn, connect_timeout=3).close()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"POSTGRES_DSN unreachable: {type(e).__name__}: {str(e)[:120]}")
    return dsn


def _spacy_or_skip():
    if not linguistics.linguistics_available():
        pytest.skip("spaCy model unavailable (SPACY_MODEL) — the spine cannot run")


def _provision(dsn):
    from src.provisioning.schema_manager import (create_user_schema, derive_user_slug_from_uuid,
                                                 derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    prior = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn
    db = _real_connect(dsn)
    try:
        schema, status = create_user_schema(user_id, slug, db)
    finally:
        db.close()
        if prior is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = prior
    assert status == "ready", status
    assert schema == derive_schema_name(slug)
    c = _real_connect(dsn)
    with c.cursor() as cur:
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING", (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                    "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING", (user_id, schema))
    c.commit()
    c.close()
    return {"user_id": user_id, "schema": schema, "dsn": dsn}


def _drop(t):
    c = _real_connect(t["dsn"])
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{t["schema"]}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{t["user_id"].replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (t["user_id"],))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (t["user_id"],))
    finally:
        c.close()


@pytest.fixture(scope="module")
def tenant(postgres_dsn):
    """A REAL provisioned tenant for the warm-seat pins (KN2/KN3/KN4)."""
    t = _provision(_dsn_or_skip(postgres_dsn))
    yield t
    _drop(t)


@pytest.fixture(scope="module")
def reset_tenant(postgres_dsn):
    """A second REAL tenant that KN1 puts through the bench's own reset."""
    t = _provision(_dsn_or_skip(postgres_dsn))
    yield t
    _drop(t)


class _DictRedis:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def setex(self, k, t, v):
        self.d[k] = v
        return True

    def ping(self):
        return True


def _bind_app(monkeypatch, t):
    """The real app, brain-free: ONLY the LLM atomizer is stubbed (to its documented fail-safe)."""
    _spacy_or_skip()
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("POSTGRES_DSN", t["dsn"])
    monkeypatch.setenv("SPACY_MODEL", os.environ.get("SPACY_MODEL", "en_core_web_sm"))
    # every deploy compose sets the naming chain on (docker-compose*.yml / .env); the code default is off
    monkeypatch.setattr(linguistics, "SPINE_NAMING_CHAIN", True, raising=False)
    monkeypatch.setattr(main, "SPINE_NAMING_CHAIN", True, raising=False)
    monkeypatch.setattr(main, "SENTENCE_PIPELINE", True, raising=False)
    monkeypatch.setattr(main, "_INGEST_ENABLED", True, raising=False)
    main._TENANT_READY_CACHE.add(t["user_id"])
    idem = importlib.import_module("src.api.idempotency")
    mgr = idem.IdempotencyManager.__new__(idem.IdempotencyManager)
    mgr.redis_url = "dict://twin"
    mgr.ttl = 3600
    mgr.client = _DictRedis()
    monkeypatch.setattr(main, "_idempotency_mgr", mgr, raising=False)
    monkeypatch.setattr(main, "paused_for_background", lambda: False, raising=False)
    reframe = importlib.import_module("src.extraction.reframe")

    async def _no_atoms(*a, **k):
        return None
    monkeypatch.setattr(reframe, "reframe_to_atomic", _no_atoms)
    monkeypatch.setattr(main, "_resolve_atomic_subjects_via_llm", lambda *a, **k: [], raising=False)
    return t


@pytest.fixture
def app_env(monkeypatch, tenant):
    return _bind_app(monkeypatch, tenant)


@pytest.fixture
def reset_env(monkeypatch, reset_tenant):
    return _bind_app(monkeypatch, reset_tenant)


def _rows(dsn, schema, sql, params=()):
    c = _real_connect(dsn)
    try:
        with c.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def _client():
    from starlette.testclient import TestClient
    return TestClient(main.app)


def _remember(client, user_id, text):
    """The MCP statement lane, in-process: /harvest-spans (spine) → /ingest (source=mcp)."""
    h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
    assert h.status_code == 200, h.text[:300]
    hd = h.json()
    assert not hd.get("extraction_degraded"), hd
    edges = hd.get("edges") or []
    assert edges, f"spine produced no edge for {text!r}: {hd}"
    r = client.post("/ingest", json={"text": text, "user_id": user_id, "edges": edges, "source": "mcp"})
    assert r.status_code == 200, r.text[:300]
    return edges, r.json()


def _query(client, user_id, q):
    r = client.post("/query", json={"text": q, "user_id": user_id})
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert not d.get("error"), d.get("error")
    return d.get("facts") or []


def _band(fact) -> str:
    """The REAL band decision (src/mcp/server.py::_fact_is_asserted) for a /query fact dict."""
    ok = mcp_server._fact_is_asserted(fact.get("fact_provenance") or fact.get("provenance"),
                                      fact.get("confidence"), fact.get("fact_class"))
    return "ASSERT" if ok else "HELD"


# ─────────────────────────────────────────────────────────────────────────────
# KN2 (mechanism, spaCy only) — one chain for kin AND social roles
# ─────────────────────────────────────────────────────────────────────────────

def test_genitive_name_chain_files_a_social_role_through_the_person_role_resolver():
    """"My friend's name is Sam." must file the SAME tie "My friend is Sam" files (friend_of via the
    social_role cue map) — not a bare related_to the role-noun walk cannot read back."""
    _spacy_or_skip()
    from datetime import datetime, timezone
    ref = datetime.now(timezone.utc)
    facts = linguistics.derive_sentence_facts("My friend's name is Sam.", ref)
    triples = {(f.subject, f.rel_type, f.object) for f in facts}
    expected = linguistics._person_role_relation("friend")
    assert expected, "the social_role cue map (floor or tenant rows) must carry 'friend'"
    assert ("sam", expected, "user") in triples, triples
    assert not any(t[1] == "related_to" and t[0] == "sam" for t in triples), triples
    # the kin arm is untouched: mother still resolves through the kinship map
    kin = {(f.subject, f.rel_type, f.object)
           for f in linguistics.derive_sentence_facts("My mother's name is Diane.", ref)}
    assert ("diane", linguistics._inherent_relation_for_noun("mother"), "user") in kin, kin


# ─────────────────────────────────────────────────────────────────────────────
# KN1 — the cold seat: the bench reset must return a FRESH seat, not a STRIPPED one
# ─────────────────────────────────────────────────────────────────────────────

def test_bench_reset_reseeds_the_structure_it_truncates_and_the_litmus_walks(reset_env, monkeypatch):
    t = reset_env
    bench = importlib.import_module("saas.bench_selftest")
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:1")   # the Qdrant clear is best-effort; fail fast
    seeded_aliases = _rows(t["dsn"], t["schema"], "SELECT count(*) FROM rel_type_aliases")[0][0]
    seeded_tax = _rows(t["dsn"], t["schema"], "SELECT count(*) FROM entity_taxonomies")[0][0]
    assert seeded_aliases > 0 and seeded_tax > 0, "provisioning seeds these; the litmus needs them"
    assert bench._reset_sandbox(t["user_id"], attempts=1) is True
    after_aliases = _rows(t["dsn"], t["schema"], "SELECT count(*) FROM rel_type_aliases")[0][0]
    after_tax = _rows(t["dsn"], t["schema"], "SELECT count(*) FROM entity_taxonomies")[0][0]
    assert after_aliases == seeded_aliases, (after_aliases, seeded_aliases)
    assert after_tax == seeded_tax, (after_tax, seeded_tax)
    anchor = _rows(t["dsn"], t["schema"],
                   "SELECT alias, is_preferred, preference_source FROM entity_aliases WHERE entity_id = %s",
                   (t["user_id"],))
    assert ("user", True, "provisioned") in {(a, p, s) for a, p, s in anchor}, anchor
    assert _rows(t["dsn"], t["schema"], "SELECT count(*) FROM entities WHERE id = %s", (t["user_id"],))[0][0] == 1
    # the litmus, on the reset seat: the statement walks back through the re-seeded shelf
    with _client() as client:
        edges, res = _remember(client, t["user_id"], "My mother's name is Diane.")
        assert res.get("committed", 0) >= 1, res
        got = {(f.get("rel_type"), str(f.get("object") or "").lower()) for f in _query(client, t["user_id"], "what is my mother's name?")}
        assert any(o == "diane" for _, o in got), got
        got2 = {(f.get("rel_type"), str(f.get("object") or "").lower()) for f in _query(client, t["user_id"], "who is my mother?")}
        assert any(o == "diane" for _, o in got2), got2


# ─────────────────────────────────────────────────────────────────────────────
# KN2 / KN3 — warm seat: father, sister, friend through the same mechanism; the band firewall
# ─────────────────────────────────────────────────────────────────────────────

STATEMENTS = [
    "My son's name is Max.",          # a CHILD first: the mother clause must not walk to him (direction)
    "My mother's name is Diane.",
    "My father's name is Robert.",    # shares parent_of with mother: must MINT, never rebind onto Diane
    "My sister's name is Alice.",
    "My friend's name is Sam.",
    "My name is Christopher.",
]
# (question, the name the answer must carry)
QUESTIONS = [
    ("what is my mother's name?", "diane"),
    ("who is my mother?", "diane"),
    ("what is my father's name?", "robert"),
    ("who is my father?", "robert"),
    ("what is my sister's name?", "alice"),
    ("who is my sister?", "alice"),
    ("what is my friend's name?", "sam"),
    ("who is my friend?", "sam"),
    ("what is my son's name?", "max"),
    ("what is my name?", "christopher"),
]


@pytest.fixture(scope="module")
def warm_seat(tenant):
    """Ingest the six statements ONCE on the warm tenant (module-scoped; the pins below read it)."""
    return tenant


def _ingest_all(monkeypatch, t):
    if t.get("_ingested"):
        return
    _bind_app(monkeypatch, t)
    with _client() as client:
        for s in STATEMENTS:
            _remember(client, t["user_id"], s)
    t["_ingested"] = True


def test_each_role_names_its_own_person_and_a_shared_rel_never_rebinds(monkeypatch, warm_seat):
    t = warm_seat
    _ingest_all(monkeypatch, t)
    aliases = _rows(t["dsn"], t["schema"],
                    "SELECT entity_id, alias FROM entity_aliases WHERE is_preferred ORDER BY alias")
    pref = {a: e for e, a in aliases}
    for name in ("max", "diane", "robert", "alice", "sam", "christopher"):
        assert name in pref, (name, sorted(pref))
    # six DISTINCT people: Robert is not Diane, Max is not Diane
    assert len({pref[n] for n in ("max", "diane", "robert", "alice", "sam", "christopher")}) == 6, pref
    parents = _rows(t["dsn"], t["schema"],
                    "SELECT subject_id FROM facts WHERE rel_type = %s AND object_id = %s"
                    "   AND superseded_at IS NULL AND deleted_at IS NULL",
                    (linguistics._inherent_relation_for_noun("mother"), t["user_id"]))
    assert {r[0] for r in parents} == {pref["diane"], pref["robert"]}, parents
    # the role the user used rides as a slot alias on the person it named — the finer key
    slots = _rows(t["dsn"], t["schema"], "SELECT entity_id, alias FROM entity_aliases WHERE NOT is_preferred")
    slot = {(e, a) for e, a in slots}
    assert (pref["diane"], "mother") in slot and (pref["robert"], "father") in slot, slot
    assert (pref["diane"], "father") not in slot, "the father's role must never land on the mother"


@pytest.mark.parametrize("question,name", QUESTIONS)
def test_every_answer_is_the_stored_row_class_a_user_stated_and_asserted(monkeypatch, warm_seat, question, name):
    t = warm_seat
    _ingest_all(monkeypatch, t)
    with _client() as client:
        facts = _query(client, t["user_id"], question)
    hits = [f for f in facts if name in (str(f.get("object") or "") + " " + str(f.get("subject") or "")).lower()]
    assert hits, (question, facts)
    for f in hits:
        assert f.get("fact_class") == "A", f
        assert (f.get("fact_provenance") or f.get("provenance")) == "user_stated", f
        assert float(f.get("confidence") or 0) >= 0.99, f
        assert _band(f) == "ASSERT", f


def test_name_authority_reads_the_alias_registry_then_the_rows_that_carry_the_entity(monkeypatch, warm_seat):
    """Rung 1: 'My name is Christopher' stamped the alias user_stated. Rung 2: Diane's alias is the
    resolve()-minted 'unspecified', so the authority is the highest among the rows that carry her
    (the user_stated parent_of). An entity carried by nothing → None (the caller's held direction)."""
    t = warm_seat
    _ingest_all(monkeypatch, t)
    pref = {a: e for e, a in _rows(t["dsn"], t["schema"],
                                   "SELECT entity_id, alias FROM entity_aliases WHERE is_preferred")}
    src = dict(_rows(t["dsn"], t["schema"],
                     "SELECT alias, preference_source FROM entity_aliases WHERE is_preferred"))
    assert src["christopher"] == "user_stated" and src["diane"] != "user_stated", src
    c = _real_connect(t["dsn"])
    try:
        with c.cursor() as cur:
            cur.execute(f'SET search_path TO "{t["schema"]}"')
        fn = getattr(main, "_alias_name_provenance", None)
        assert fn is not None, "main.py::_alias_name_provenance is missing — a name answer has no honest authority read"
        assert fn(c, pref["christopher"]) == "user_stated"
        assert fn(c, pref["diane"]) == "user_stated"
        assert fn(c, str(uuid.uuid4())) is None
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────────────
# KN4 — end-to-end through the REAL MCP recall tool (in-process ASGI transport)
# ─────────────────────────────────────────────────────────────────────────────

def test_mcp_recall_renders_both_litmus_questions_in_the_asserted_band(monkeypatch, warm_seat):
    t = warm_seat
    _ingest_all(monkeypatch, t)
    import httpx
    monkeypatch.setenv("MEMORY_TEACHES", "false")
    monkeypatch.setattr(mcp_server, "FAULTLINE_API_URL", "http://backend.test", raising=False)
    mcp_server._provisioned_users.add(t["user_id"])

    async def _drive():
        out = {}
        async with main.app.router.lifespan_context(main.app):
            inner = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                      base_url="http://backend.test", timeout=60.0)
            prior = mcp_server._http_client
            mcp_server._http_client = mcp_server.TurnBoundedClient(inner)
            try:
                for q in ("what is my mother's name?", "who is my mother?"):
                    r = await mcp_server.recall_memory_tool(q, t["user_id"])
                    out[q] = r.get("memory") if isinstance(r, dict) else r
            finally:
                mcp_server._http_client = prior
                await inner.aclose()
        return out
    out = asyncio.run(_drive())
    for q, mem in out.items():
        text = str(mem)
        assert "diane" in text.lower(), (q, text)
        assert "less certain" not in text.lower(), (q, text)   # the held-band lead-in


# ─────────────────────────────────────────────────────────────────────────────
# structural — no role noun / rel name is spelled in the mechanism (subject-agnostic)
# ─────────────────────────────────────────────────────────────────────────────

def _string_constants(func) -> list[str]:
    import ast
    src = inspect.getsource(func)
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    # code only — the docstring is where the measured example is recorded on purpose
    # (clean=False: the raw constant, not the dedented rendering, is what ast.walk yields)
    doc = ast.get_docstring(tree.body[0], clean=False) or ""
    return [s for s in out if s != doc]


_ROLE_WORDS = {"mother", "father", "sister", "brother", "son", "daughter", "friend", "wife", "husband"}
_REL_NAMES = {"parent_of", "child_of", "sibling_of", "friend_of", "spouse", "related_to"}


@pytest.mark.parametrize("name", ["_walk_naming_clause_referent", "_alias_name_provenance",
                                  "_resolve_possessive_rel_target"])
def test_no_role_or_rel_literal_in_the_mechanism(name):
    func = getattr(main, name, None)
    assert func is not None, f"main.py::{name} is missing — the mechanism this lane shipped is not on disk"
    consts = " ".join(_string_constants(func)).lower()
    words = set(re.findall(r"[a-z_]+", consts))
    assert not (words & _ROLE_WORDS), sorted(words & _ROLE_WORDS)
    assert not (words & _REL_NAMES), sorted(words & _REL_NAMES)


# ═════════════════════════════════════════════════════════════════════════════
# ROUND 2 (critic: KN2 LOSS) — the reset is derived from the provisioner, pinned as FULL parity;
# re-plant C (chain OFF) and re-plant E (the rendered recall) pinned.
# ═════════════════════════════════════════════════════════════════════════════

def _seeded_tables_by_ast() -> set[str]:
    """Census the PROVISIONER's seed statements by AST, not a hand list: every ``INSERT INTO <t>``
    string constant reachable from ``schema_manager.seed_tenant_metadata`` (itself + the module-level
    functions it calls). A table the provisioner seeds tomorrow lands in this set automatically."""
    sm = importlib.import_module("src.provisioning.schema_manager")
    tree = ast.parse(inspect.getsource(sm))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "seed_tenant_metadata" in fns, "schema_manager.seed_tenant_metadata is missing — no single seed source"
    todo, seen = ["seed_tenant_metadata"], set()
    tables: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen or name not in fns:
            continue
        seen.add(name)
        for node in ast.walk(fns[name]):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                tables.update(t.lower() for t in re.findall(r"INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", node.value, re.I))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                todo.append(node.func.id)
    return tables


def _snapshot(dsn: str, schema: str, user_id: str) -> dict[str, set]:
    """Every base table's row-set, normalised: timestamp/date-typed columns dropped (they record WHEN a
    row was written), serial ids dropped (RESTART IDENTITY), and the seat's own uuid / slug replaced by
    a placeholder (a fresh tenant and a reset tenant are different seats by construction)."""
    from src.provisioning.schema_manager import derive_user_slug_from_uuid
    slug = derive_user_slug_from_uuid(user_id)
    out: dict[str, set] = {}
    c = _real_connect(dsn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY 1", (schema,))
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                cur.execute("SELECT column_name, data_type FROM information_schema.columns"
                            " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (schema, t))
                cols = [cn for cn, dt in cur.fetchall()
                        if not dt.startswith("timestamp") and dt != "date" and cn != "id"]
                if not cols:
                    cur.execute(f'SELECT count(*) FROM "{schema}"."{t}"')
                    out[t] = {("COUNT", cur.fetchone()[0])}
                    continue
                cur.execute(f'SELECT {", ".join(chr(34) + x + chr(34) for x in cols)} FROM "{schema}"."{t}"')
                rows = set()
                for r in cur.fetchall():
                    rows.add(tuple(str(v).replace(user_id, "<seat>").replace(slug, "<slug>") for v in r))
                out[t] = rows
    finally:
        c.close()
    return out


def test_reset_seat_carries_every_row_a_fresh_tenant_carries(postgres_dsn, monkeypatch):
    """KN1, the full bar: after the bench reset a seat is row-set IDENTICAL to a freshly provisioned
    tenant on EVERY base table (∪ _DATA_TABLES ∪ every table the provisioner seeds, censused by AST) —
    not just the four tables round 1 happened to touch (the critic measured correction_patterns 4→0 and
    correction_signals 12→0 on every reset seat). The reseed is the provisioner's own function, so this
    holds by construction; the pin is what keeps it true when the seed grows."""
    dsn = _dsn_or_skip(postgres_dsn)
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:1")
    bench = importlib.import_module("saas.bench_selftest")
    seeded = _seeded_tables_by_ast()
    # the census must not be vacuous: the tables the two rounds measured stripped are in it
    for t in ("rel_type_aliases", "entity_taxonomies", "correction_patterns", "correction_signals",
              "entities", "entity_aliases"):
        assert t in seeded, (t, sorted(seeded))
    # no second hand list: the bench's reseed carries no INSERT of its own, it calls the provisioner
    src = inspect.getsource(bench._reseed_after_truncate)
    assert "seed_tenant_metadata" in src and not re.search(r"INSERT\s+INTO", src, re.I), src
    fresh = _provision(dsn)
    reset = _provision(dsn)
    try:
        assert bench._reset_sandbox(reset["user_id"], attempts=1) is True
        a = _snapshot(dsn, fresh["schema"], fresh["user_id"])
        b = _snapshot(dsn, reset["schema"], reset["user_id"])
        assert set(a) == set(b), (sorted(set(a) ^ set(b)))
        must = (set(bench._DATA_TABLES) | seeded) & set(a)
        assert must, "nothing to compare"
        diffs = {t: (len(a[t] - b[t]), len(b[t] - a[t])) for t in sorted(a) if a[t] != b[t]}
        assert not diffs, f"reset seat ≠ fresh tenant on {diffs} (missing_after_reset, extra_after_reset)"
        # and every seeded table is non-empty on the reset seat (a seed that copied nothing is a seed
        # whose source is empty on this database — say so rather than pass on 0 == 0)
        empty = sorted(t for t in seeded & set(b) if not b[t] or b[t] == {("COUNT", 0)})
        assert not empty, f"seeded tables empty after reset: {empty}"
    finally:
        _drop(fresh)
        _drop(reset)


def test_chain_off_shared_rel_class_declines_and_three_people_mint(postgres_dsn, monkeypatch):
    """Re-plant C. With SPINE_NAMING_CHAIN OFF (the code default — no role-alias leg is written) the
    referent walk has only the EDGE to go on, and mother/father share one rel: test (a) alone must
    never pick "the one parent it finds". Pinned: son > mother > father in that order mints THREE
    people (direction keeps the son out of "my mother"; the shared-rel-class rule declines "my father"
    instead of rebinding onto the mother).

    DOCUMENTED LIMITATION, asserted rather than hidden: with no slot alias the graph cannot say WHICH
    parent is the mother, so the possessive walk DEFERS on the two-parent ambiguity (it must not pick).
    The answer lane that then serves "who is my mother?" is the legacy scope lane, out of this lane's
    footprint (reported); every deploy compose sets the chain ON, where the slot alias answers it."""
    dsn = _dsn_or_skip(postgres_dsn)
    t = _provision(dsn)
    try:
        _bind_app(monkeypatch, t)
        monkeypatch.setattr(linguistics, "SPINE_NAMING_CHAIN", False, raising=False)
        monkeypatch.setattr(main, "SPINE_NAMING_CHAIN", False, raising=False)
        with _client() as client:
            for s_ in ("My son's name is Max.", "My mother's name is Diane.", "My father's name is Robert."):
                edges, _ = _remember(client, t["user_id"], s_)
                assert not any(e.get("rel_type") == "also_known_as" for e in edges), edges  # the chain is OFF
        pref = {a: e for e, a in _rows(dsn, t["schema"],
                                      "SELECT entity_id, alias FROM entity_aliases WHERE is_preferred")}
        assert {"max", "diane", "robert"} <= set(pref), sorted(pref)
        assert len({pref["max"], pref["diane"], pref["robert"]}) == 3, pref
        kin = linguistics._inherent_relation_for_noun("mother")
        parents = {r[0] for r in _rows(dsn, t["schema"],
                                        "SELECT subject_id FROM facts WHERE rel_type = %s AND object_id = %s"
                                        "   AND superseded_at IS NULL AND deleted_at IS NULL", (kin, t["user_id"]))}
        assert parents == {pref["diane"], pref["robert"]}, parents
        # the limitation: no slot → the possessive walk must DEFER on two parents, never pick one
        c = _real_connect(dsn)
        try:
            with c.cursor() as cur:
                cur.execute(f'SET search_path TO "{t["schema"]}"')
            assert main._resolve_possessive_rel_target(c, t["user_id"], "mother") is None
            assert main._resolve_possessive_rel_target(c, t["user_id"], "son") == pref["max"]
        finally:
            c.close()
    finally:
        _drop(t)


ROLE_WORDS_IN_BATTERY = ("son", "mother", "father", "sister", "friend")


def test_rendered_recall_is_the_typed_walk_and_never_renders_a_role_alias_as_a_name(monkeypatch, warm_seat):
    """Re-plant E. With the slot-alias narrowing in ``_resolve_possessive_rel_target`` disabled the
    typed walk defers on two parents and the ladder lane rendered "Mother is the parent of you" —
    the role slot leaking as a NAME — while a fact-dict pin stayed green. So pin the RENDERED recall
    through the real MCP tool AND that the typed walk (not the ladder) produced the answer: wrap
    ``resolve_typed_walk`` (never replace it) and require status "answer" for every question."""
    t = warm_seat
    _ingest_all(monkeypatch, t)
    import httpx
    monkeypatch.setenv("MEMORY_TEACHES", "false")
    monkeypatch.setattr(mcp_server, "FAULTLINE_API_URL", "http://backend.test", raising=False)
    mcp_server._provisioned_users.add(t["user_id"])
    seen: dict[str, str] = {}
    _orig = main.resolve_typed_walk

    def _wrapped(query_text, user_id, db, *a, **k):
        r = _orig(query_text, user_id, db, *a, **k)
        seen[query_text] = (r or {}).get("status") if isinstance(r, dict) else repr(r)
        return r
    monkeypatch.setattr(main, "resolve_typed_walk", _wrapped)

    async def _drive():
        out = {}
        async with main.app.router.lifespan_context(main.app):
            inner = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                      base_url="http://backend.test", timeout=60.0)
            prior = mcp_server._http_client
            mcp_server._http_client = mcp_server.TurnBoundedClient(inner)
            try:
                for q, _ in QUESTIONS:
                    r = await mcp_server.recall_memory_tool(q, t["user_id"])
                    out[q] = str(r.get("memory") if isinstance(r, dict) else r)
            finally:
                mcp_server._http_client = prior
                await inner.aclose()
        return out
    out = asyncio.run(_drive())
    for q, name in QUESTIONS:
        text = out[q].lower()
        assert seen.get(q) == "answer", (q, seen.get(q), out[q][:200])
        assert name in text, (q, out[q][:200])
        assert "less certain" not in text, (q, out[q][:200])
        # a role slot is never a name: no line may predicate a bare role word as its subject
        for role in ROLE_WORDS_IN_BATTERY:
            assert not re.search(rf"(^|\n)\s*{role} (is|was|has|goes)\b", text), (q, role, out[q][:200])
        assert "the parent of you" not in text, (q, out[q][:200])


def test_direction_alone_is_load_bearing_for_a_grown_unshared_directional_role(postgres_dsn, monkeypatch):
    """Re-plant C, the half the seed cannot show. In the SEEDED cue maps every directional rel is
    shared by several roles (mother/father/parent/mom/dad → parent_of), so the shared-rel-class rule
    already declines and the direction test is masked. It is load-bearing for a per-tenant GROWN role
    that is the ONLY role on a directional rel: grow ``guardian → guardian_of`` / ``ward → ward_of``
    (inverse pair, one role each, no slot alias — chain OFF) and file "My ward's name is Tim." then
    "My guardian's name is Pat.": Tim sits at the WARD end of the pair, so the guardian clause must
    decline (a) and mint Pat. With (a) undirected (rel ∪ inverse across both directions — HEAD's
    shape, the critic's re-plant C) Tim satisfies "my guardian", no other role shares guardian_of to
    trigger the shared-class decline, and Pat is REBOUND onto Tim."""
    dsn = _dsn_or_skip(postgres_dsn)
    t = _provision(dsn)
    try:
        c = _real_connect(dsn)
        try:
            with c.cursor() as cur:
                cur.execute(f'SET search_path TO "{t["schema"]}"')
                # grow the pair from the seeded parent_of row's shape (per-tenant, source='user')
                for rel, inv, label in (("guardian_of", "ward_of", "guardian of"), ("ward_of", "guardian_of", "ward of")):
                    cur.execute(
                        "INSERT INTO rel_types (rel_type, label, category, head_types, tail_types, is_symmetric,"
                        " inverse_rel_type, is_hierarchy_rel, fact_class, storage_target, source, temporal_class)"
                        " SELECT %s, %s, category, head_types, tail_types, false, %s, false, fact_class,"
                        "        storage_target, 'user', temporal_class FROM rel_types WHERE rel_type = %s"
                        " ON CONFLICT (rel_type) DO NOTHING", (rel, label, inv, linguistics._inherent_relation_for_noun("mother")))
                for cue, rel in (("guardian", "guardian_of"), ("ward", "ward_of")):
                    cur.execute(
                        "INSERT INTO linguistic_cues (cue, category, description, source, is_active)"
                        " VALUES (%s, 'kinship_noun', %s, 'user', true) ON CONFLICT (cue, category) DO NOTHING",
                        (cue, rel))
            c.commit()
        finally:
            c.close()
        # the overlays cache per schema with a 5s TTL — a fresh tenant has no entry yet, but be explicit
        for mod in ("src.api.linguistic_cue_overlay", "src.api.rel_type_overlay"):
            m = importlib.import_module(mod)
            for attr in ("_overlay_cache", "_keyed_map_cache", "_cache", "_CACHE"):
                d = getattr(m, attr, None)
                if isinstance(d, dict):
                    d.clear()
        _bind_app(monkeypatch, t)
        monkeypatch.setattr(linguistics, "SPINE_NAMING_CHAIN", False, raising=False)
        monkeypatch.setattr(main, "SPINE_NAMING_CHAIN", False, raising=False)
        with _client() as client:
            e1, _ = _remember(client, t["user_id"], "My ward's name is Tim.")
            assert any(e.get("rel_type") == "ward_of" and e.get("subject") == "tim" for e in e1), e1
            e2, _ = _remember(client, t["user_id"], "My guardian's name is Pat.")
            assert any(e.get("rel_type") == "guardian_of" and e.get("subject") == "pat" for e in e2), e2
        pref = {a: e for e, a in _rows(dsn, t["schema"],
                                      "SELECT entity_id, alias FROM entity_aliases WHERE is_preferred")}
        assert {"tim", "pat"} <= set(pref), sorted(pref)
        assert pref["tim"] != pref["pat"], "the guardian's name was rebound onto the ward — direction test (a) is not load-bearing"
    finally:
        _drop(t)

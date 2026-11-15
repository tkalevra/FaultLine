"""Aspect-term L4 grounding — the slot NAME the user states becomes a per-tenant PLACE at ingest.

LITMUS (pre-prod 2026-09-15, image 24266d6f): "My email address is jordan.smith@example.com." lands as
`entity_attributes has_email` (user_stated, network) and "what is my email?" recalls it, but the user's
OWN phrasing "what is my email address?" returned nothing. Traced in-process on a throwaway tenant
(DEV/review/gauntlet/aspect-term-l4-grounding/round_1/repro.txt): nothing in the tenant's ontology
recorded that "email address" is where has_email is filed, so the query's compound-rel resolver
missed, determine_path admitted the RESIDENCE rels through the seeded "X lives at Y (address)"
template, and the lexical topic gate resolved the topic to `address` (SemCor never annotated `email`)
and projected the has_email row out.

THE FIX IS AT INGEST (`main.py::_ground_aspect_term_place`, called from the spine's possessive-
attribute connect; `linguistics.py::possessive_attribute_term` supplies the term + head):
  1. TERM → REL: a per-tenant `rel_type_aliases` row `normalize_rel(term) → rel` — the table the
     query's DOOR 1 (`_resolve_compound_rel_terminal`) already canonicalises a possessed NP through.
  2. TERM IS-A HEAD: `(term, subclass_of, head)` staged Class C (Right-hand Head Rule, Williams 1981;
     Wikidata Q1273217 P279 Q64826646 etc. — see round_1/GROUNDING.md).
The query CONSUMES that row and nothing else: DOOR 1 of the typed walk (pre-existing reader),
`determine_path`'s possessed-NP binding (`_query_possessed_noun_phrases` → `path.aspect_bound_rels`,
words consumed so the head noun cannot widen into the residence rels) and the topic gate's exemption
for the bound rel. These pins drive the REAL app over the REAL spine against a
REAL provisioned tenant (POSTGRES_DSN → a database whose name contains "test"; skipped otherwise), with
ONLY the LLM atomizer stubbed to its documented fail-safe (segment_clauses) — no brain.

CRITERIA PINNED: AT1 (repro mechanism), AT2 (rows written at ingest, seed as ceiling, query unchanged),
AT3 (four terms through ONE mechanism + an AST census that no term/rel literal exists in the mechanism),
AT4 (residence sense untouched; narrow ⊇ broad), AT5 (per-tenant only, visible on the very next recall).
"""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import re
import uuid

import psycopg2
import pytest

main = importlib.import_module("src.api.main")
linguistics = importlib.import_module("src.extraction.linguistics")
canonical = importlib.import_module("src.ontology.canonical")

_real_connect = psycopg2.connect

pytestmark = pytest.mark.usefixtures("requires_postgres")

# The four litmus terms. They live HERE, in the test, and nowhere in src/ — see test_no_term_literal_in_mechanism.
STATEMENTS = [
    ("My email address is jordan.smith@example.com.", "jordan.smith@example.com", "email address", "address"),
    ("My phone number is 519-555-0123.", "519-555-0123", "phone number", "number"),
    ("My IP address is 192.168.7.42.", "192.168.7.42", "ip address", "address"),
    ("My MAC address is 3c:22:fb:9a:11:20.", "3c:22:fb:9a:11:20", "mac address", "address"),
]
RESIDENCE = "My address is 123 Main Street, Kitchener."


# ─────────────────────────────────────────────────────────────────────────────
# environment
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


@pytest.fixture(scope="module")
def tenant(postgres_dsn):
    """A REAL provisioned tenant (template + seeds + control-plane rows) in the throwaway database."""
    dsn = _dsn_or_skip(postgres_dsn)
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
    with c.cursor() as cur:  # the provisioning JOB writes these; /query refuses an unregistered seat
        cur.execute("INSERT INTO public.users (user_id, email, display_name, slug) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING", (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute("INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                    "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING", (user_id, schema))
    c.commit()
    c.close()
    yield {"user_id": user_id, "schema": schema, "dsn": dsn}
    c = _real_connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        c.close()


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


@pytest.fixture
def app_env(monkeypatch, tenant):
    """The real app, brain-free: ONLY the LLM atomizer is stubbed (to its documented fail-safe)."""
    _spacy_or_skip()
    # The CORE engine path (SAAS off = byte-for-byte the open core; conftest pins SAAS_MODE=1 for the
    # transport suites, under which a seat with no bound brain is refused at the harvest door).
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("POSTGRES_DSN", tenant["dsn"])
    monkeypatch.setenv("SPACY_MODEL", os.environ.get("SPACY_MODEL", "en_core_web_sm"))
    monkeypatch.setattr(main, "SENTENCE_PIPELINE", True, raising=False)
    monkeypatch.setattr(main, "_INGEST_ENABLED", True, raising=False)
    main._TENANT_READY_CACHE.add(tenant["user_id"])
    idem = importlib.import_module("src.api.idempotency")
    mgr = idem.IdempotencyManager.__new__(idem.IdempotencyManager)
    mgr.redis_url = "dict://twin"
    mgr.ttl = 3600
    mgr.client = _DictRedis()
    monkeypatch.setattr(main, "_idempotency_mgr", mgr, raising=False)
    monkeypatch.setattr(main, "paused_for_background", lambda: False, raising=False)
    # The atomizer's documented fail-safe: no atoms → segment_clauses. No LLM call is made, so the
    # harvest is not "degraded" and nothing is withheld. Every other lane runs for real.
    reframe = importlib.import_module("src.extraction.reframe")

    async def _no_atoms(*a, **k):
        return None
    monkeypatch.setattr(reframe, "reframe_to_atomic", _no_atoms)
    # A residue the possessive connect did not bind would go to the LLM subject binder; refuse it
    # loudly rather than let a stray call make the pin depend on a brain.
    monkeypatch.setattr(main, "_resolve_atomic_subjects_via_llm", lambda *a, **k: [], raising=False)
    return tenant


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


def _recall(client, user_id, q):
    r = client.post("/query", json={"text": q, "user_id": user_id})
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert not d.get("error"), d.get("error")
    return {(f.get("rel_type"), str(f.get("object") or "").lower()) for f in (d.get("facts") or [])}


# ─────────────────────────────────────────────────────────────────────────────
# AT1 — the mechanism: the term + its head are read off the clause, grammar only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sentence,value,term,head", STATEMENTS)
def test_possessive_attribute_term_reads_the_stated_slot_name(sentence, value, term, head):
    _spacy_or_skip()
    assert linguistics.possessive_attribute_term(sentence, value) == (term, head)


def test_possessive_attribute_term_refuses_non_possessive_and_negated():
    _spacy_or_skip()
    # value on the SUBJECT side → not a possessive-attribute copula about this value
    assert linguistics.possessive_attribute_term("jordan@x.com is my email address.", "jordan@x.com") is None
    # negated copula → absence, never a binding
    assert linguistics.possessive_attribute_term("My email address is not jordan@x.com.", "jordan@x.com") is None
    # a genitive owner still yields the term (subject-agnostic) — the possessor is someone else's job
    assert linguistics.possessive_attribute_term(
        "The router's management IP address is 10.0.0.1.", "10.0.0.1") == ("management ip address", "address")


# ─────────────────────────────────────────────────────────────────────────────
# AT2 / AT3 / AT4 / AT5 — driven end to end on a real tenant
# ─────────────────────────────────────────────────────────────────────────────

def test_stated_terms_are_grounded_and_recall_by_their_own_words(app_env):
    user_id, schema, dsn = app_env["user_id"], app_env["schema"], app_env["dsn"]
    # Warm the alias cache BEFORE any grounding so AT5 proves invalidation, not a cold read.
    canonical.reset_caches(schema)
    assert canonical.resolve_canonical("email address", dsn=dsn, schema=schema)["matched"] == "none"
    with _client() as client:
        for sentence, value, term, head in STATEMENTS:
            edges, ingest = _remember(client, user_id, sentence)
            # the VALUE landed on the possessor as the detector-typed scalar (the memory)
            assert ingest.get("scalar_committed", 0) >= 1, ingest
            atomic = [e for e in edges if str(e.get("object") or "").lower() == value.lower()]
            assert atomic and atomic[0]["subject"] == "user", edges
            rel = atomic[0]["rel_type"]
            # AT2 (1) TERM → REL attach, per tenant, source engine
            alias_norm = canonical.normalize_rel(term)
            rows = _rows(dsn, schema, "SELECT canonical_rel_type, source FROM rel_type_aliases WHERE alias = %s",
                         (alias_norm,))
            assert rows == [(rel, "engine")], (term, alias_norm, rows)
            # AT2 (2) TERM IS-A HEAD rung, staged Class C on TYPE nodes (a place, never the value)
            rungs = _rows(dsn, schema,
                          "SELECT a.alias, b.alias, s.fact_class, s.fact_provenance, s.is_hierarchy_rel "
                          "FROM staged_facts s "
                          "JOIN entity_aliases a ON a.entity_id = s.subject_id AND a.is_preferred "
                          "JOIN entity_aliases b ON b.entity_id = s.object_id AND b.is_preferred "
                          "WHERE s.rel_type = 'subclass_of' AND s.provenance = 'ingest_aspect_term' AND a.alias = %s",
                          (term,))
            assert rungs == [(term, head, "C", "llm_inferred", True)], (term, rungs)
            # AT5 the grown place is visible NOW — the per-schema alias cache was WARM before the
            # first grounding (see the top of this test) and must have been invalidated on the write:
            # the RUNG-3 reader's own cache resolves the term to the rel on this very call.
            hit = canonical._load_aliases(dsn, schema).get(alias_norm)
            assert hit and hit[0] == rel, (term, alias_norm, hit)
            # AT3 recall by the user's OWN term, the very next call, through the unchanged query path
            got = _recall(client, user_id, f"what is my {term}?")
            assert (rel, value.lower()) in got, (term, got)
        # the VALUE rows are untouched memories on the user (THE HARD LINE)
        vals = _rows(dsn, schema, "SELECT attribute, value_text, provenance FROM entity_attributes "
                                  "WHERE entity_id = %s ORDER BY attribute", (user_id,))
        assert {v for _, v, _ in vals} >= {v.lower() for _, v, _, _ in STATEMENTS}, vals
        assert all(p == "user_stated" for _, _, p in vals), vals
        # AT4 — the RESIDENCE sense is untouched, stated AFTER the 'address' type node exists
        _remember(client, user_id, RESIDENCE)
        res_facts = _recall(client, user_id, "what is my address?")
        assert any("123 main street" in obj for _, obj in res_facts), res_facts
        network_vals = {v.lower() for _, v, _, _ in STATEMENTS}
        assert not any(obj in network_vals for _, obj in res_facts), (
            "a broad 'what is my address?' must not descend into the network scalars", res_facts)
        # AT4 — narrow ⊇ broad: the stated term never returns LESS than the bare aspect word
        broad = _recall(client, user_id, "what is my email?")
        narrow = _recall(client, user_id, "what is my email address?")
        assert broad and narrow >= broad, (broad, narrow)
        # and the narrow ask is PRECISE: the residence does not ride along with the email
        assert not any("123 main street" in obj for _, obj in narrow), narrow
        # a phrasing the typed walk does NOT decompose still consumes the grown place through
        # determine_path + the gate exemption — and stays precise (no residence in the answer)
        for sentence, value, term, head in STATEMENTS:
            got = _recall(client, user_id, f"do you know my {term}?")
            rels = {r for r, _ in got}
            assert any(v == value.lower() for _, v in got), (term, got)
            assert not any("123 main street" in obj for _, obj in got), (term, got)
        # a CHAINED possessor ("my mother's …") is NOT bound here — whose slot is asked is the
        # multi-hop walk's job; the anchor's own value must never answer for someone else
        assert main._query_possessed_noun_phrases("what is my mother's email address?") == []
        assert main._query_possessed_noun_phrases("what is Sarah's phone number?") == ["phone number"]
        assert main._query_possessed_noun_phrases("do you know my email address?") == ["email address"]


def _seeded_alias_rows(conn) -> list[tuple[str, str]]:
    """EVERY seeded alias row on the tenant (source <> 'engine') — never an unordered LIMIT 1.

    Round 1 pinned the seed ceiling on ONE unordered row, on a throwaway whose first boot had lost the
    93-row migration-030 seed (a first-boot FK rejection, fixed at 75a55f7e), so the pin was vacuous and
    the wound below shipped green. The pin now REQUIRES the seed to be present (a seat missing it is an
    environment failure, not a pass) and drives every row."""
    with conn.cursor() as cur:
        cur.execute("SELECT alias, canonical_rel_type FROM rel_type_aliases WHERE source <> 'engine' ORDER BY alias")
        rows = [(a, c) for a, c in cur.fetchall()]
    assert len(rows) >= 90, (f"the seeded alias set is missing on this seat ({len(rows)} rows; migration 030 "
                            f"carries 93) — re-migrate the throwaway from the current corpus (first-boot fix "
                            f"75a55f7e); a pin driven without the seed is vacuous")
    return rows


def test_grounding_never_writes_public_and_holds_the_seed_ceiling(app_env):
    user_id, schema, dsn = app_env["user_id"], app_env["schema"], app_env["dsn"]
    # public.* is the template — never written at runtime
    c = _real_connect(dsn)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.rel_type_aliases WHERE source = 'engine'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM public.staged_facts WHERE provenance = 'ingest_aspect_term'")
            assert cur.fetchone()[0] == 0
    finally:
        c.close()
    # SEED AS CEILING, driven through the real function on the tenant connection:
    #  (a) a single-word term whose name IS an existing rel is refused (it would hijack that rel's scope);
    #  (b) EVERY seeded alias, fed as the user's natural surface form, is held IN THE READERS' IDENTITY
    #      SPACE (normalize_rel on both sides) — no grown twin may land beside a seeded row, whether the
    #      term is bound to the seed's own rel (already present) or to another rel (held);
    #  (c) a compound term is idempotent (second grounding is a no-op, no duplicate rung).
    conn = _real_connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        from src.entity_registry.registry import EntityRegistry
        registry = EntityRegistry(conn, auto_commit=False, schema_name=schema)
        with conn.cursor() as cur:
            # a SINGLE-WORD rel the tenant holds (e.g. a bare scalar attribute name) + a different scalar rel
            cur.execute("SELECT rel_type FROM rel_types WHERE position('_' in rel_type) = 0 "
                        "AND 'SCALAR' = ANY(tail_types) ORDER BY rel_type LIMIT 1")
            single_word_rel = cur.fetchone()[0]
            cur.execute("SELECT rel_type FROM rel_types WHERE 'SCALAR' = ANY(tail_types) AND rel_type <> %s "
                        "ORDER BY rel_type LIMIT 1", (single_word_rel,))
            other_rel = cur.fetchone()[0]
            cur.execute("SELECT rel_type FROM rel_types WHERE 'SCALAR' = ANY(tail_types) ORDER BY rel_type")
            scalar_rels = [r[0] for r in cur.fetchall()]
        assert single_word_rel and other_rel
        # (a) a single-word term whose name IS a rel the tenant holds is refused for ANY other target rel
        rep = main._ground_aspect_term_place(conn, schema, registry, user_id, single_word_rel, single_word_rel,
                                             other_rel)
        assert rep["skipped"] == "single_word_term_is_existing_rel_type" and not rep["alias_inserted"], rep
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM rel_type_aliases WHERE alias = %s", (single_word_rel,))
            assert cur.fetchone()[0] == 0
        # (b) EVERY seeded alias is HELD — growth never overrides a higher authority, and the comparison is
        #     made in the space the readers resolve in. DOOR 1 (_resolve_compound_rel_terminal) keys its
        #     lookup on normalize_rel(<possessed NP>) and RUNG 3 (canonical._load_aliases) indexes every row
        #     under normalize_rel(alias): 36 of the 93 seeded rows are spelled differently from their
        #     normalised form (preferred_name / prefer_name, married_to / marry_to, years_old / year_old …),
        #     and a raw-column compare reads each of those as UNBOUND and writes an engine twin that then
        #     outranks the seed's own normalised index (raw row wins in _load_aliases). Twin count must be 0.
        seeded = _seeded_alias_rows(conn)
        seeded_norm: dict[str, list[tuple[str, str]]] = {}
        for a, cn in seeded:
            seeded_norm.setdefault(canonical.normalize_rel(a), []).append((a, cn))
        spelled_differently = [a for a, _cn in seeded if canonical.normalize_rel(a) != a]
        assert spelled_differently, "no seeded alias differs from its normalised form — the pin cannot bite"
        hold_reasons = {"alias_present", "alias_held_by_other_rel", "term_is_rel",
                        "single_word_term_is_existing_rel_type"}
        reports = []
        for a, cn in seeded:
            surface = a.replace("_", " ")          # the user's natural phrasing of the seeded alias
            other = next((r for r in scalar_rels if r != cn), None)
            for rel in (cn, other):               # bound to its own rel, and to a DIFFERENT rel
                rep = main._ground_aspect_term_place(conn, schema, registry, user_id, surface, surface, rel)
                conn.commit()
                reports.append((a, surface, rel, rep))
                assert not rep["alias_inserted"] and rep["skipped"] in hold_reasons, (a, surface, rel, rep)
        assert len(reports) == 2 * len(seeded)
        # a seeded alias bound to a DIFFERENT rel is HELD by that seed, never re-bound
        held = [r for r in reports if r[3]["skipped"] == "alias_held_by_other_rel"]
        assert held, reports[:4]
        with conn.cursor() as cur:
            cur.execute("SELECT alias, canonical_rel_type FROM rel_type_aliases WHERE source = 'engine' ORDER BY alias")
            engine_rows = cur.fetchall()
        twins = [(a, cn, seeded_norm[canonical.normalize_rel(a)]) for a, cn in engine_rows
                 if canonical.normalize_rel(a) in seeded_norm]
        assert twins == [], f"{len(twins)} engine alias row(s) shadow a seeded row by normalised form: {twins[:6]}"
        # the seed rows are byte-identical afterwards (held = untouched, not re-pointed)
        with conn.cursor() as cur:
            cur.execute("SELECT alias, canonical_rel_type FROM rel_type_aliases WHERE source <> 'engine' ORDER BY alias")
            assert cur.fetchall() == seeded
        # (c) idempotent re-grounding of an already-grounded compound
        sentence, value, term, head = STATEMENTS[0]
        rel = _rows(dsn, schema, "SELECT canonical_rel_type FROM rel_type_aliases WHERE alias = %s",
                    (canonical.normalize_rel(term),))[0][0]
        before = _rows(dsn, schema, "SELECT count(*) FROM staged_facts WHERE provenance = 'ingest_aspect_term'")[0][0]
        rep = main._ground_aspect_term_place(conn, schema, registry, user_id, term, head, rel)
        conn.commit()
        assert rep["skipped"] == "alias_present" and not rep["alias_inserted"], rep
        after = _rows(dsn, schema, "SELECT count(*) FROM staged_facts WHERE provenance = 'ingest_aspect_term'")[0][0]
        assert after == before, (before, after)
    finally:
        conn.close()


def test_held_check_compares_in_the_readers_normalized_space():
    """STRUCTURAL twin of (b): the held-check's alias read is the FULL column compared through
    ``normalize_rel`` — not an ``alias = %s`` point lookup of the raw column. A point lookup is exactly the
    round-1 shape that let ``prefer_name`` twin ``preferred_name``. Pinned on the source so the regression
    is named at collection time, with the DB-driven pin above proving the behaviour."""
    src = inspect.getsource(main._ground_aspect_term_place)
    assert "SELECT canonical_rel_type FROM rel_type_aliases WHERE alias = %s" not in src, (
        "held-check regressed to a raw-column point lookup — compare normalize_rel(alias) on BOTH sides")
    assert "SELECT alias, canonical_rel_type FROM rel_type_aliases" in src
    assert "_canon.normalize_rel(_a_l) == alias_norm" in src


# ─────────────────────────────────────────────────────────────────────────────
# AT3 — AST census: the mechanism carries NO term / rel-name literal
# ─────────────────────────────────────────────────────────────────────────────

def _string_constants(func) -> list[str]:
    """Every str constant in the function's AST, docstring excluded — the census asks the SYNTAX TREE,
    never the text, so a term hidden in a comment or docstring cannot fake a hit and a literal cannot
    hide behind formatting."""
    src = inspect.getsource(func)
    tree = ast.parse(src)
    fn = tree.body[0]
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    out: list[str] = []
    for node in body:
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.append(n.value)
    return out


def _seeded_scalar_rel_names() -> set[str]:
    """The seed's OWN scalar vocabulary, read from the migrations that seed it (no DB needed)."""
    names: set[str] = set()
    mig = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
    for fn in sorted(os.listdir(mig)):
        if not fn.endswith(".sql"):
            continue
        with open(os.path.join(mig, fn), encoding="utf-8", errors="ignore") as fh:
            for m in re.finditer(r"'(has_[a-z_]+)'", fh.read()):
                names.add(m.group(1))
    assert names, "no seeded has_* rel names found in migrations/"
    return names


def test_no_term_literal_in_mechanism():
    """The four litmus terms, their heads, their words, and every seeded has_* rel name must appear in
    NO string constant of the mechanism (ingest grounding + the term extractor + the query resolver it
    feeds). A word-level check: a constant is split on non-letters and each piece is tested, so
    'email_address' or 'has email' cannot slip past as one token."""
    # WORD vocabulary: every word of the four terms and their heads — a constant is split into words
    # and each word is tested, so 'email_address', 'email address' or 'Email' cannot slip past.
    words_vocab: set[str] = set()
    for _s, _v, term, head in STATEMENTS:
        words_vocab.update(re.findall(r"[a-z]+", term))
        words_vocab.add(head)
    # NAME vocabulary: every seeded has_* rel name as a WHOLE token (a rel-name literal check).
    names_vocab = _seeded_scalar_rel_names() | {t.replace(" ", "_") for _s, _v, t, _h in STATEMENTS}
    offenders = {}
    for func in (main._ground_aspect_term_place, linguistics.possessive_attribute_term,
                 main._resolve_compound_rel_terminal, main._query_possessed_noun_phrases):
        for const in _string_constants(func):
            tokens = set(re.findall(r"[a-z_]+", const.lower()))
            words = set()
            for t in tokens:
                words.update(re.findall(r"[a-z]+", t))
            hit = (tokens & names_vocab) | (words & words_vocab)
            if hit:
                offenders.setdefault(func.__name__, []).append((const[:60], sorted(hit)))
    assert not offenders, f"term/rel literals in the mechanism: {offenders}"


def test_spine_hook_routes_every_bound_term_through_the_one_function():
    """The ONLY way a term reaches the tenant is `_ground_aspect_term_place`, called from the spine's
    backbone block on the tenant-bound connection — pinned on the AST of the spine helper (a Call node
    to that name exists) and on there being no OTHER writer of rel_type_aliases in main.py."""
    src = inspect.getsource(main._harvest_via_sentence_pipeline)
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_ground_aspect_term_place"]
    assert len(calls) == 1, "the spine must ground the term at exactly ONE site"
    terms = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_term_of"]
    assert len(terms) == 1, "the term is read off the clause at exactly ONE site"
    whole = inspect.getsource(main)
    writers = re.findall(r"INSERT\s+INTO\s+rel_type_aliases", whole, flags=re.IGNORECASE)
    assert not writers, "main.py must not write rel_type_aliases directly — go through canonical.record_alias"
    # and the query side reads the grown place at exactly ONE site inside determine_path
    dp = ast.parse(inspect.getsource(main.determine_path))
    reads = [n for n in ast.walk(dp) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_query_possessed_noun_phrases"]
    assert len(reads) == 1, "determine_path must consume the grown place at exactly ONE site"

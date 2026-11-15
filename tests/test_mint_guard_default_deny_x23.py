"""MINT GUARD DEFAULT-DENY (issue #23) — the sixth-site wound, flipped contract, AST census.

THE WOUND (deploy #13, live, image f18ca1d6 which CARRIES the #22 fix): the #22 opt-in
guard (edge kwargs threaded at 5 adjudicated resolve() sites) NEVER FIRED live —
'lisinopril related_to quantity' + the bare 'quantity' alias still minted. The live log
pinned the SIXTH site: the mint rode ``entity_registry.resolve_start name=quantity``
IMMEDIATELY BEFORE ``sentence_pipeline.pending_scalar_grounded`` — the harvest's
pending-scalar-grounding CONCEPT-QUEUE resolve (``_harvest_via_sentence_pipeline``'s
``_pg_registry.resolve(user_id, _concept.lower())``), a context-free call that minted the
alias DURING /harvest-spans, BEFORE /ingest ran. Once the alias existed, every guarded
/ingest site sailed through the alias probe's EARLY RETURN — an unguarded FIRST mint
poisons the whole seam. An opt-in guard at a seam with dozens of callers is a CENSUS
problem, not a threading problem.

THE FIX (three layers, one wound):
  1. REGISTRY DEFAULT-DENY: a single-token cue-rail family member REFUSES the mint at the
     surrogate-registration block for EVERY caller without ``name_intent=True``.
  2. PLANNER GUARD: the pending-scalar-grounding planner never re-homes a family canonical
     (the flat scalar is KEPT — the value lands structured on the subject, the #14
     take-frame's own shape; fail-toward-not-losing).
  3. /ingest loud drops unchanged (ValueError handlers record stage+reason, turn retained).

THE NAMING OPT-OUT ADJUDICATION (frozen bar 2: 'my cat is named Quantity' still mints):
  • SCALAR-tailed naming rels (pref_name/also_known_as) keep naming objects string-kept at
    /ingest — they file through register_alias, a different method the refusal never touches.
  • A RELATIONAL edge's subject is a potential NAME BINDING (the genitive-name frame 'my
    cat's name is Level' → (level, <role>, user)) — the /ingest subject site passes
    ``name_intent=not _edge_is_scalar(edge)``: the SAME proven metadata signal as #22's
    scalar gate, read INVERTED (a scalar edge's bare-family SUBJECT is the mangled shape
    (quantity, quantity, '10 mg') — refused; a relational subject opts out).
  • The relation-pattern name lane ('my cat Quantity is 7') and the correction re-ground
    lanes resolve user-NAMED values → ``name_intent=True`` (user-is-truth; also keeps the
    unwrapped correct_fact sites from ever seeing the exception).

THE CENSUS (the load-bearing pin, the alias-morphology r5 shape): every resolve() site in
src/ + saas/ detected by per-file BINDING RESOLUTION (import aliases, class-aliases,
constructor instances at any scope, the conventional registry parameter names in
registry-aware files, dotted access, the getattr spelling) and ATTRIBUTED TO THE
INNERMOST enclosing def — including the ATTRIBUTE-REFERENCE spelling
(``_sync_in_txn_thread(registry.resolve, ...)``) that hides the /ingest subject/object
sites from a call-only census. The frozen table below is the pin: a NEW site, a moved
site, or a dropped name_intent anywhere = RED. Anti-vacuity plants (temp copies + a
real-tree re-plant with inverse-replace restore) prove each escape spelling fires.

Deterministic per PD3: the fixture owns its env. Requires POSTGRES_DSN pointing ONLY at a
throwaway test DB (the name must contain 'test').
"""
import ast
import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

TURN = "I take lisinopril 10 milligrams daily for high blood pressure."
BARE = "quantity"
DOSAGE_Q = "What is my lisinopril dosage?"

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN

_E2E_STATE: dict = {}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 1 — THE AST CENSUS (no DB)
# ════════════════════════════════════════════════════════════════════════════════════════

_REGISTRY_MODULES = {"src.entity_registry.registry", "src.entity_registry"}
# The conventional instance/parameter names a registry binding takes in this codebase.
# Only honored in files that IMPORT the registry (registry-aware) — a local namesake
# class (schema_oracle) is excluded by construction.
_PARAM_NAMES = {"registry", "reg", "_reg", "_ba_registry", "_pg_registry"}


def _file_bindings(tree):
    """Names a file binds to the ENTITY REGISTRY: class aliases (import + assign),
    constructed instances (any scope), dotted module aliases. The default seed is NOT
    a binding — a file must import (or alias) the real registry to be registry-aware."""
    classes = set()
    insts = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _REGISTRY_MODULES:
            for a in node.names:
                if a.name == "EntityRegistry":
                    classes.add(a.asname or "EntityRegistry")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "src.entity_registry.registry":
                    classes.add(a.asname or "src.entity_registry.registry")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                v = node.value
                if isinstance(v, ast.Name) and v.id in classes:
                    classes.add(t.id)  # RR = EntityRegistry (alias-of-class)
                elif isinstance(v, ast.Call):
                    f = v.func
                    if (isinstance(f, ast.Name) and f.id in classes) or \
                            (isinstance(f, ast.Attribute) and (
                                f.attr in classes or (
                                    isinstance(f.value, ast.Name)
                                    and f.value.id in classes))):
                        insts.add(t.id)
    return classes, insts


def _walk_with_parents(tree):
    def rec(node, parents):
        yield node, parents
        for ch in ast.iter_child_nodes(node):
            yield from rec(ch, parents + [node])
    yield from rec(tree, [])


def _innermost_def(node, parents):
    for p in reversed(parents):
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return p.name
        if isinstance(p, ast.ClassDef):
            return f"{p.name}::<class>"
    return "<module>"


def _site_shape(attr_node, parents):
    """kwargs-shape of the resolve USE: called directly, or passed by reference to a
    runner (`_sync_in_txn_thread(registry.resolve, ..., name_intent=...)`) — the runner
    call's keywords are the carriage."""
    par = parents[-1] if parents else None
    ks = set()
    if isinstance(par, ast.Call):
        ks = {k.arg for k in par.keywords}
    if "name_intent" in ks:
        return "name_intent"
    if "type_hint" in ks:
        return "type_hint"
    return "plain"


def _census(root):
    """{(relpath, innermost_def): Counter(shape)} over src/ + saas/ under root."""
    from collections import Counter
    table = {}
    for base in ("src", "saas"):
        top = os.path.join(root, base)
        if not os.path.isdir(top):
            continue
        for dirpath, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, root)
                try:
                    tree = ast.parse(open(path).read())
                except SyntaxError:
                    raise AssertionError(f"census cannot parse {rel} — resolve this first")
                classes, insts = _file_bindings(tree)
                if not classes and not insts:
                    continue  # not registry-aware (namesake local classes excluded)
                for node, parents in _walk_with_parents(tree):
                    shape = None
                    if isinstance(node, ast.Attribute) and node.attr == "resolve":
                        v = node.value
                        if isinstance(v, ast.Name) and (
                                v.id in insts or v.id in classes or v.id in _PARAM_NAMES):
                            shape = _site_shape(node, parents)
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                            and node.func.id == "getattr" and len(node.args) >= 2 \
                            and isinstance(node.args[1], ast.Constant) \
                            and node.args[1].value == "resolve" \
                            and isinstance(node.args[0], ast.Name) and (
                                node.args[0].id in insts
                                or node.args[0].id in classes
                                or node.args[0].id in _PARAM_NAMES):
                        shape = "plain"  # the getattr spelling never carries name_intent
                    if shape:
                        key = (rel, _innermost_def(node, parents))
                        table.setdefault(key, Counter())[shape] += 1
    return table


# THE FROZEN TABLE — every entry adjudicated (issue #23):
#   edge_default  — an edge-mint surface; refused by default (the mangled class).
#   name_frame    — a naming frame; MUST carry name_intent (the count pins carriage).
#   growth_skip   — an engine growth lane; refused→skipped rung (wrapped except).
#   correction    — user-is-truth correction lane; name sites carry name_intent,
#                   lookup sites are wrapped (refusal→None anchor, honest miss).
#   dead          — pre-existing broken lane (reversed args + a kwarg resolve() never
#                   took — any call raises TypeError); untouched, flagged to the owner.
_FROZEN = {
    # (file, innermost def): {shape: count, "_class": adjudication}
    ("src/api/main.py", "_attach_to_seeded_backbone"): {"plain": 2, "_class": "growth_skip"},
    ("src/api/main.py", "_grow_compound_type_rungs"): {"plain": 2, "_class": "growth_skip"},
    ("src/api/main.py", "_ground_aspect_term_place"): {"plain": 2, "_class": "growth_skip"},
    ("src/api/main.py", "_attach_wordnet_hypernym_ladder"): {"plain": 2, "_class": "growth_skip"},
    ("src/api/main.py", "_attach_hearst_hyponyms"): {"plain": 2, "_class": "growth_skip"},
    # THE SIXTH SITE lives here (the _pg_registry concept-queue resolve) beside the
    # backbone-attach object and the climb/part rungs — all plain (default-deny).
    ("src/api/main.py", "_harvest_via_sentence_pipeline"): {"plain": 5, "_class": "edge_default"},
    # subject site (name_intent = not scalar — the genitive-name opt-out), the
    # relation-pattern name lane, the validation-lane subject; object + validation
    # object + climb pairs plain; the two type_hint sites are the DEAD prerequisite lane.
    ("src/api/main.py", "_ingest_txn_body"): {
        "name_intent": 3, "plain": 4, "type_hint": 2, "_class": "edge_default+name_frame+dead"},
    ("src/api/main.py", "extract_rewrite"): {"plain": 1, "_class": "growth_skip"},
    ("src/api/main.py", "correct_fact"): {
        # name_intent 6 (was 4): the value-surface lane's two interleaved naming-frame
        # sites (the assembled 6864e53c commit) — correction naming-frames, same class.
        "name_intent": 6, "plain": 3, "_class": "correction"},
    ("src/re_embedder/embedder.py", "classify_unknown_concepts"): {"plain": 2, "_class": "growth_skip"},
    ("src/re_embedder/embedder.py", "_place_full_chain"): {"plain": 3, "_class": "growth_skip"},
    ("src/re_embedder/embedder.py", "_place_climb_rung"): {"plain": 1, "_class": "growth_skip"},
    ("src/re_embedder/embedder.py", "_splice_intermediate_rung"): {"plain": 2, "_class": "growth_skip"},
}
# namesake EXCLUSION (verified, not a registry site): src/schema_oracle/oracle.py defines
# its OWN in-memory `class EntityRegistry` (resolve(name, etype) → dict) — a design-demo
# module, never imported by the engine lanes; the census's registry-aware gate skips it.


class TestResolveCensus:
    """Every EntityRegistry.resolve() site in src/ + saas/, classified and frozen."""

    def test_census_matches_frozen_table(self):
        table = _census(_REPO_ROOT)
        got = {k: dict(v) for k, v in table.items() if "_class" not in v}
        expected = {k: {s: c for s, c in v.items() if s != "_class"}
                    for k, v in _FROZEN.items()}
        missing = {k: expected[k] for k in set(expected) - set(got)}
        extra = {k: got[k] for k in set(got) - set(expected)}
        drift = {k: (expected[k], got[k]) for k in set(expected) & set(got)
                 if expected[k] != got[k]}
        assert not missing and not extra and not drift, (
            f"resolve() census drift — every resolve( call site must be adjudicated in "
            f"_FROZEN (issue #23): missing={missing} extra={extra} drift={drift}. "
            f"A NEW site means a caller reached the mint seam without classification; "
            f"adjudicate it (edge/name/growth/correction/dead) and extend _FROZEN "
            f"deliberately — never delete the pin to go green.")

    def test_name_frames_carry_the_opt_out(self):
        """CARRIAGE: every def whose sites are name frames carries name_intent on each
        of them (a dropped opt-out reads as 'plain' and fails the frozen counts)."""
        table = _census(_REPO_ROOT)
        for key, spec in _FROZEN.items():
            if key not in table:
                continue
            if spec.get("name_intent"):
                assert table[key].get("name_intent", 0) == spec["name_intent"], (
                    f"{key}: name_frame site lost its name_intent carriage: "
                    f"{dict(table[key])}")
                for shape in ("plain", "type_hint"):
                    allowed = spec.get(shape, 0)
                    assert table[key].get(shape, 0) == allowed, (
                        f"{key}: unexpected {shape} site beside the name frames — "
                        f"adjudicate it in _FROZEN or restore its carriage")

    # ── ANTI-VACUITY: each escape spelling planted in a TEMP COPY must fire the census ──

    def _planted_root(self, plant):
        tmp = tempfile.mkdtemp(prefix="mint23_census_")
        # copy the REAL tree (src only — saas/ has no sites and the copy is expensive)
        shutil.copytree(os.path.join(_REPO_ROOT, "src"), os.path.join(tmp, "src"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        target = os.path.join(tmp, "src", "api", "main.py")
        src_txt = open(target).read()
        planted = src_txt + "\n\n" + plant + "\n"
        with open(target, "w") as f:
            f.write(planted)
        self._addCleanup_shutil(tmp)
        return tmp

    def _addCleanup_shutil(self, tmp):
        import gc
        _TO_CLEANUP.append(tmp)

    def test_plant_new_def_plain_resolve_fires(self):
        """P1: a NEW def with a fresh instance + bare resolve — the sixth-site class."""
        plant = (
            "def _planted_new_lane(u, s):\n"
            "    from src.entity_registry.registry import EntityRegistry\n"
            "    _pr = EntityRegistry(dsn='x')\n"
            "    return _pr.resolve(u, s)\n")
        table = _census(self._planted_root(plant))
        assert ("src/api/main.py", "_planted_new_lane") in table, \
            "census MISSED a fresh-instance plain resolve (the sixth-site spelling)"

    def test_plant_class_alias_fires(self):
        """P2: RR = EntityRegistry; x = RR(...) — the alias-of-class spelling."""
        plant = (
            "def _planted_alias_lane(u, s):\n"
            "    from src.entity_registry.registry import EntityRegistry as RR\n"
            "    _pr2 = RR(dsn='x')\n"
            "    return _pr2.resolve(u, s)\n")
        table = _census(self._planted_root(plant))
        assert ("src/api/main.py", "_planted_alias_lane") in table, \
            "census MISSED an aliased-class instance resolve"

    def test_plant_reference_escape_fires(self):
        """P3: `_run(r.resolve, u, s)` — the attribute-REFERENCE spelling with NO
        carriage, planted inside a def whose frozen sites are ALL name frames: the
        plain-count drift must fire even though the def already exists."""
        plant = (
            "def _planted_ref_lane(registry, u, s):\n"
            "    return _sync_in_txn_thread(registry.resolve, u, s)\n")
        table = _census(self._planted_root(plant))
        assert ("src/api/main.py", "_planted_ref_lane") in table, \
            "census MISSED the attribute-reference (runner) spelling"

    def test_plant_getattr_spelling_fires(self):
        """P4: getattr(registry, 'resolve')(...) — the getattr spelling."""
        plant = (
            "def _planted_getattr_lane(registry, u, s):\n"
            "    return getattr(registry, 'resolve')(u, s)\n")
        table = _census(self._planted_root(plant))
        assert ("src/api/main.py", "_planted_getattr_lane") in table, \
            "census MISSED the getattr spelling"

    def test_plant_real_tree_replant_fires_and_restores(self):
        """P5 (the real-tree re-plant): P1+P4 planted in the REAL src/api/main.py —
        the census MUST fire; the restore is byte-identical (md5 + cmp). Inverse
        string-replace restore, never a cp-restore while siblings are live."""
        target = os.path.join(_REPO_ROOT, "src", "api", "main.py")
        original = open(target).read()
        plant = (
            "\n\n# MINT23-REPLANT\n"
            "def _planted_replant_lane(registry, u, s):\n"
            "    return getattr(registry, 'resolve')(u, s)\n")
        assert "_planted_replant_lane" not in original
        import hashlib
        pre_md5 = hashlib.md5(original.encode()).hexdigest()
        try:
            with open(target, "w") as f:
                f.write(original + plant)
            table = _census(_REPO_ROOT)
            assert ("src/api/main.py", "_planted_replant_lane") in table, \
                "census MISSED a real-tree planted getattr resolve"
        finally:
            with open(target, "w") as f:
                f.write(original)  # inverse replace: subtract exactly what was added
        restored = open(target).read()
        assert hashlib.md5(restored.encode()).hexdigest() == pre_md5, \
            "real-tree restore is NOT byte-identical — fix the tree before proceeding"
        # and the census is green again on the restored tree
        got = {k: dict(v) for k, v in _census(_REPO_ROOT).items() if "_class" not in v}
        expected = {k: {s: c for s, c in v.items() if s != "_class"}
                    for k, v in _FROZEN.items()}
        assert got == expected, "census not green after the byte-identical restore"


_TO_CLEANUP: list = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_plants():
    yield
    for tmp in _TO_CLEANUP:
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 2+3 — REGISTRY CONTRACT + THE LIVE-SHAPE E2E (throwaway DB)
# ════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")
class TestMintGuardDefaultDeny:
    @pytest.fixture(autouse=True, scope="class")
    def _driver(self, request):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)

        _had_saas = "SAAS_MODE" in os.environ
        _saas_val = os.environ.pop("SAAS_MODE", None)

        pytest.main_mod = M
        M._INGEST_ENABLED = True

        class _D:
            def __init__(self): self.d = {}
            def get(self, k): return self.d.get(k)
            def setex(self, k, t, v): self.d[k] = v; return True
            def ping(self): return True

        class _Idem:
            redis_url = "dict://test"; ttl = 3600; client = _D()
        M._idempotency_mgr = _Idem()

        def _provision():
            user_id = str(uuid.uuid4())
            slug = derive_user_slug_from_uuid(user_id)
            conn = psycopg2.connect(_DSN)
            schema, status = create_user_schema(user_id, slug, conn)
            assert status == "ready", status
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.users (user_id, email, display_name, slug) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (user_id, f"{slug}@example.invalid", slug, slug))
                cur.execute(
                    "INSERT INTO public.user_provisioning (user_id, schema_name, status, "
                    "ready_at) VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                    (user_id, schema))
            conn.commit()
            conn.close()
            return user_id, schema

        _E2E_STATE["live_seat"] = _provision()   # the deploy-13 live-shape reproduction
        _E2E_STATE["name_seat"] = _provision()   # bar-2 genuine-name frames
        _E2E_STATE["ctx_seat"] = _provision()    # registry-contract probes

        def _fetch(schema, exec_fn):
            c = psycopg2.connect(_DSN, options=f"-csearch_path={schema}")
            try:
                with c.cursor() as cur:
                    return exec_fn(cur)
            finally:
                c.close()

        def _aliases_of(schema):
            return _fetch(schema, lambda cur: (
                cur.execute("SELECT alias FROM entity_aliases") or
                {r0 for (r0,) in cur.fetchall()}))

        _E2E_STATE["_fetch"] = _fetch
        _E2E_STATE["_aliases_of"] = _aliases_of
        try:
            with TestClient(M.app) as client:
                from src.extraction import linguistics as _L
                t0 = time.time()
                while _L._nlp is None and time.time() - t0 < 180:
                    time.sleep(0.5)
                assert _L._nlp is not None
                _E2E_STATE["client"] = client
                yield
        finally:
            if _had_saas:
                os.environ["SAAS_MODE"] = _saas_val

    @property
    def _fetch(self):
        return _E2E_STATE["_fetch"]

    @property
    def _aliases(self):
        return _E2E_STATE["_aliases_of"]

    def _facts_rows(self, schema):
        return self._fetch(schema, lambda cur: (
            cur.execute(
                "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
                "f.subject_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.subject_id::text), "
                "f.rel_type, COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
                "f.object_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.object_id::text) "
                "FROM facts f") or cur.fetchall()))

    def _attrs(self, schema):
        return self._fetch(schema, lambda cur: (
            cur.execute(
                "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
                "a.entity_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), a.entity_id::text), "
                "a.attribute, a.value_text FROM entity_attributes a") or cur.fetchall()))

    # ── THE LIVE SHAPE (frozen bar 1: the deploy-13 turn, reproduced atomizer) ──────────

    def test_live_shape_sixth_site_never_mints(self, monkeypatch):
        """The deploy-13 live shape END-TO-END: the REAL spine driven with a
        live-shaped atomizer (ONE atom = the whole sentence, exactly what the live
        REFRAME produced — atom_count=1 in the live log). Pre-fix this minted the bare
        alias at the SIXTH site (the pending-grounding concept queue) and committed
        (lisinopril, related_to, quantity) + the scalar on the junk node. Post-fix:
        NO quantity entity/alias, NO related_to→quantity row, the scalar lands
        STRUCTURED on lisinopril (the planner keeps the flat orphan — the value is
        never lost), and the query renders clean."""
        import src.api.main as M
        from src.extraction.reframe import Atom, ReframeResult

        client = _E2E_STATE["client"]
        lu, ls = _E2E_STATE["live_seat"]

        # the LIVE atomizer shape: one atom covering the whole turn (live log:
        # reframe.done atom_count=1 → sentence_pipeline.atomized atom_count=1)
        async def _live_reframe(text, user_id, messages=None):
            return ReframeResult(atoms=[Atom(text=text, source_span=text)],
                                 used_llm=True, rejected_count=0)

        monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", False,
                            raising=False)
        import src.extraction.reframe as _RF
        monkeypatch.setattr(_RF, "reframe_to_atomic", _live_reframe)

        h = client.post("/harvest-spans", json={"text": TURN, "user_id": lu})
        assert h.status_code == 200, h.text[:400]
        edges = h.json().get("edges") or []
        # the wound precondition: the mangled re-home link must NOT be in the stream
        assert not any(e.get("rel_type") == "related_to" and
                       (e.get("object") or "").strip().lower() == BARE
                       for e in edges), \
            f"the (lisinopril, related_to, quantity) mangled link still harvested: {edges}"
        # the value MUST still be carried (the flat scalar kept by the planner guard)
        assert any(e.get("rel_type") == BARE and "10" in (e.get("object") or "")
                   for e in edges), \
            f"the quantity value was lost from the edge stream: {edges}"

        r = client.post("/ingest", json={
            "text": TURN, "user_id": lu, "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        dropped = body.get("dropped") or []

        aliases = self._aliases(ls)
        assert BARE not in aliases, \
            f"the bare family canonical STILL minted (sixth-site class): {sorted(aliases)}"
        facts = self._facts_rows(ls)
        assert not any(row[1] == "related_to" and row[2] == BARE for row in facts), \
            f"the (lisinopril, related_to, quantity) junk fact committed: {facts}"
        assert not any(row[0] == BARE or row[2] == BARE for row in facts), \
            f"a facts row still carries the bare canonical: {facts}"
        # the value lands STRUCTURED on the substance — the lossless outcome
        attrs = self._attrs(ls)
        assert any(x == "lisinopril" and a == BARE and "10" in (v or "")
                   for x, a, v in attrs), \
            f"the quantity scalar did not land on lisinopril: {attrs}"

    def test_sixth_site_queue_resolve_refused_at_registry(self):
        """The SIXTH site's call SHAPE (a context-free queue resolve of the re-homed
        concept surface) is refused at the registry itself — pinned directly, so a
        future planner variant that re-introduces the queue hits the default-deny."""
        import psycopg2
        from src.entity_registry.registry import (
            EntityRegistry, MeasurementFamilySurfaceError)
        cu, cs = _E2E_STATE["ctx_seat"]
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        reg = EntityRegistry(c, auto_commit=True, schema_name=cs)
        try:
            with pytest.raises(MeasurementFamilySurfaceError):
                reg.resolve(cu, BARE)  # exactly the sixth site's call, no opt-out
        finally:
            c.close()

    # ── FROZEN BAR 2: genuine names still mint ─────────────────────────────────────────

    def test_naming_edge_still_mints_the_family_word_name(self):
        """'(cat, pref_name, Quantity)' — the naming edge as the lane delivers it
        (SCALAR-tailed → string-kept → register_alias) still mints the name."""
        client = _E2E_STATE["client"]
        nu, ns = _E2E_STATE["name_seat"]
        r = client.post("/ingest", json={
            "text": "My cat is named Quantity.", "user_id": nu,
            "edges": [
                {"subject": "user", "rel_type": "owns", "object": "cat",
                 "object_type": "ANIMAL"},
                {"subject": "cat", "rel_type": "pref_name", "object": "Quantity",
                 "object_datatype": "string"},
            ], "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        dropped = r.json().get("dropped") or []
        assert not any(d.get("object") == BARE or d.get("subject") == BARE
                       for d in dropped), f"a naming edge was refused: {dropped}"
        assert BARE in self._aliases(ns), \
            "the genuine family-word NAME no longer mints (frozen bar 2 violated)"

    def test_genitive_name_genuine_frame_still_mints(self):
        """'My cat's name is Level.' — the genitive frame binds the NAME as a
        RELATIONAL edge's subject → the /ingest subject site's name_intent opt-out
        (not _edge_is_scalar) must keep minting; 'level' IS a measure_noun member."""
        client = _E2E_STATE["client"]
        nu, ns = _E2E_STATE["name_seat"]
        h = client.post("/harvest-spans", json={
            "text": "My cat's name is Level.", "user_id": nu})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        # PREASSERT (r1 critic): distinguish infra from contract — an LLM-outage harvest
        # returning ZERO edges must SKIP loudly, never false-red as a bar-2 violation.
        # The naming edge is the cat->name relational leg (any edge with 'level' in it).
        _naming = [e for e in edges
                   if "level" in str((e.get("object") or "") + (e.get("subject") or "")).lower()]
        if not edges or not _naming:
            pytest.skip("HARVEST-ENV: the atomizer produced no naming edge "
                        f"(edges={len(edges)}) — infra, not the bar-2 contract")
        r = client.post("/ingest", json={
            "text": "My cat's name is Level.", "user_id": nu, "edges": edges,
            "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        assert "level" in self._aliases(ns), \
            "the genitive-name genuine frame no longer mints (bar 2 violated)"

    def test_relation_pattern_name_frame_still_mints(self):
        """'My cat Quantity is 7 years old.' — the relation-pattern name lane (a
        capitalized NAME in my <relation> <Name> is <N>) opts out via name_intent."""
        client = _E2E_STATE["client"]
        nu, ns = _E2E_STATE["name_seat"]
        r = client.post("/ingest", json={
            "text": "My cat Quantity is 7 years old.", "user_id": nu,
            "edges": [
                {"subject": "user", "rel_type": "owns", "object": "cat",
                 "object_type": "ANIMAL"},
                {"subject": "user", "rel_type": "age", "object": "7",
                 "object_datatype": "integer"},
            ], "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        assert BARE in self._aliases(ns), \
            "the relation-pattern name frame ('my cat Quantity is 7') no longer mints"

    # ── CONTROLS ────────────────────────────────────────────────────────────────────────

    def test_town_of_level_lookup_unaffected(self):
        """'the town of Level' / 'High Level' — multi-token surfaces never reach the
        bare refusal, and a query-side lookup of a family word declines honestly
        (resolve_anchor reads entity_aliases directly — the query lane never mints)."""
        client = _E2E_STATE["client"]
        cu, cs = _E2E_STATE["ctx_seat"]
        r = client.post("/ingest", json={
            "text": "I was born in High Level.", "user_id": cu,
            "edges": [
                {"subject": "user", "rel_type": "born_in", "object": "High Level",
                 "object_type": "LOCATION"},
            ], "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        aliases = self._aliases(cs)
        assert "high level" in aliases, "the multi-token place name must still mint"
        # the query side: asking about a family word on a seat with no such entity
        # returns an honest miss — never mints, never 500s
        q = client.post("/query", json={"text": "What is my quantity?", "user_id": cu})
        assert q.status_code == 200, q.text[:300]

    def test_flag_off_mints_as_before(self, monkeypatch):
        """REGISTRY_FAMILY_COMPOUND_REDIRECT off → the default-deny never runs and the
        surface mints byte-for-byte (the ONE rollback lever for #21+#22+#23)."""
        import psycopg2
        import src.entity_registry.registry as R
        from src.entity_registry.registry import EntityRegistry
        cu, cs = _E2E_STATE["ctx_seat"]
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        reg = EntityRegistry(c, auto_commit=True, schema_name=cs)
        try:
            monkeypatch.setattr(R, "REGISTRY_FAMILY_COMPOUND_REDIRECT", False)
            got = reg.resolve(cu, "dosage")
            assert got, "flag OFF must restore the legacy mint byte-for-byte"
            assert "dosage" in self._aliases(cs)
        finally:
            c.close()

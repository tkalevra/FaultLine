"""CROSS-RESTATEMENT CONVERGENCE AT MINT (issue #226) — one referent, restated, stays one.

THE WOUND (measured, prod 2026-10-07, fresh seat): the owner said the same referent three
ways in one session — "My opnsense box sits at…" / "It resides on VLAN 30" / "My opnsense box
resides on…" — and ingest minted THREE entities ('opnsense box', 'firewall', 'opnsense box
info'), each accruing its own facts. Restatement is the norm in dialogue; per-phrasing
minting shards one memory into rivals.

THE FIX (this lane): when a NEW surface reaching ``EntityRegistry.resolve`` (the one mint
chokepoint — the #23 lesson: convergence lives INSIDE resolve, never threaded at call
sites) is DETERMINISTICALLY compatible with a RECENT entity's alias set, bind to that
entity and register the surface as a NON-preferred 'inferred' alias through the EXISTING
register_alias machinery — no second registry, no embeddings, no substring matching:

    exact             determiner-stripped token-set equality (number preserved)
    modifier_added    same head ∧ alias ⊂ new ∧ len(alias) ≥ 2
    modifier_dropped  same head ∧ new ⊂ alias ∧ len(new) ≥ 2
    aspect strip      strip trailing aspect_head_noun cue-rail heads, re-apply

Guards (each can veto): user/seat entity excluded; class-side (place) nodes excluded;
EXACTLY ONE compatible survivor required — genuine ambiguity keeps today's separate-mint
behavior (an honest no-merge beats a wrong merge — THE HARD LINE).

FIXTURES ARE SUBJECT-AGNOSTIC AND NOVEL by constraint: a pottery kiln ('skutt kiln'), a
musical instrument ('hurdy gurdy'), a bicycle — never the bench's family/networking
subjects.

Deterministic per PD3: the fixture owns its env. Requires POSTGRES_DSN pointing ONLY at a
throwaway test DB (the name must contain 'test'). DB-gated parts skip (loudly) without it.
"""
import ast
import os
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN

_E2E_STATE: dict = {}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fresh novel subjects (NOT the bench's family/networking domains).
KILN = "skutt kiln"
KILN_MODIFIED = "old skutt kiln"
KILN_ASPECT = "skutt kiln info"
WHEEL = "shimpo whisper wheel"          # genuinely different referent, different head
ASPECT = frozenset({"info", "information", "detail", "details", "status",
                    "summary", "overview", "description"})


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 1 — THE COMPATIBILITY RULE (pure, no DB)
# ════════════════════════════════════════════════════════════════════════════════════════

from src.entity_registry.registry import EntityRegistry  # noqa: E402

_nt = EntityRegistry._convergence_norm_tokens
_compat = EntityRegistry._convergence_compat


class TestCompatibilityRule:
    """The merge-vs-keep-separate line, exactly. Every case cites the wound or the guard."""

    def test_issue_example_aspect_head_merges(self):
        # 'opnsense box info' ⊇ 'opnsense box' — the issue's OWN candidate: merges via strip.
        assert _compat(_nt("opnsense box info"), _nt("opnsense box"), ASPECT) \
            == "aspect_head_strip+exact"

    def test_issue_example_firewall_never_merges(self):
        # 'firewall' vs 'opnsense box' — the issue's OWN negative: NOT deterministically
        # compatible (no shared tokens, different heads) → must NOT merge.
        assert _compat(_nt("firewall"), _nt("opnsense box"), ASPECT) is None

    def test_modifier_added_merges(self):
        assert _compat(_nt("old skutt kiln"), _nt(KILN), ASPECT) == "modifier_added"
        assert _compat(_nt("white opnsense box"), _nt("opnsense box"), ASPECT) == "modifier_added"

    def test_modifier_dropped_merges(self):
        assert _compat(_nt(KILN), _nt("old skutt kiln"), ASPECT) == "modifier_dropped"

    def test_exact_with_determiner_merges(self):
        assert _compat(_nt("the skutt kiln"), _nt(KILN), ASPECT) == "exact"
        assert _compat(_nt("my opnsense box"), _nt("opnsense box"), ASPECT) == "exact"

    def test_bare_head_noun_never_swallowed_either_direction(self):
        # len≥2 guards BOTH ways: a single-token alias is a bare common noun whose compounds
        # can denote a DIFFERENT referent ('cardboard box'), and a single-token new surface
        # is the harvest backbone's own bare-head TYPE RUNG (welding it onto the instance
        # would be THE HARD LINE inverted).
        assert _compat(_nt("opnsense box"), _nt("box"), ASPECT) is None
        assert _compat(_nt("box"), _nt("opnsense box"), ASPECT) is None
        assert _compat(_nt("skutt kiln"), _nt("kiln"), ASPECT) is None
        assert _compat(_nt("kiln"), _nt("skutt kiln"), ASPECT) is None

    def test_different_head_never_merges_even_with_shared_tokens(self):
        # part-vs-whole: 'paragon kiln controller' shares 'paragon kiln' but the controller
        # is a DIFFERENT referent (its head) — kept separate, honestly.
        assert _compat(_nt("paragon kiln controller"), _nt("paragon kiln"), ASPECT) is None
        assert _compat(_nt("opnsense firewall"), _nt("opnsense box"), ASPECT) is None

    def test_number_never_crossed(self):
        # 'kilns' names a SET; merging it onto the member referent corrupts user truth.
        assert _compat(_nt("skutt kilns"), _nt(KILN), ASPECT) is None
        assert _compat(_nt(KILN), _nt("skutt kilns"), ASPECT) is None

    def test_aspect_strip_strips_only_trailing_aspect_heads(self):
        assert _compat(_nt("skutt kiln details"), _nt(KILN), ASPECT) == "aspect_head_strip+exact"
        assert _compat(_nt("skutt kiln status"), _nt(KILN), ASPECT) == "aspect_head_strip+exact"
        # a NON-aspect trailing noun is a referent head of its own — no strip, no merge
        assert _compat(_nt("skutt kiln controller"), _nt(KILN), ASPECT) is None

    def test_aspect_strip_then_modifier_drop(self):
        # strip resolves to a subset of the alias set: still a compatible restatement.
        assert _compat(_nt("old skutt kiln info"), _nt("old skutt kiln"), ASPECT) \
            == "aspect_head_strip+exact"
        assert _compat(_nt("skutt kiln info"), _nt("old skutt kiln"), ASPECT) \
            == "aspect_head_strip+modifier_dropped"

    def test_empty_and_junk_surfaces(self):
        assert _compat([], _nt(KILN), ASPECT) is None
        assert _compat(_nt(""), _nt(KILN), ASPECT) is None
        assert _compat(_nt("the"), _nt(KILN), ASPECT) is None  # determiners fully stripped


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 2 — MINT-PATH CENSUS (pin 4): every INSERT INTO entities / entity_aliases in src/
# ════════════════════════════════════════════════════════════════════════════════════════
# The #23 lesson: a guard on N-1 of N mint sites leaves the bug live. Convergence lives
# INSIDE EntityRegistry.resolve, so EVERY resolve() caller is covered BY CONSTRUCTION (the
# resolve-site authority is tests/test_mint_guard_default_deny_x23.py's frozen census — a
# floor run beside this file). This census pins the OTHER half: every site that writes
# entities/entity_aliases DIRECTLY, each with a frozen disposition, so a NEW unguarded mint
# site (bypassing resolve) fails the suite.

_MINT_TARGETS = ("insert into entities", "insert into entity_aliases")

# disposition key → meaning:
#   resolve_mint      — the surrogate-registration block: CONVERGENCE APPLIES immediately
#                       before it (the probe runs first, inside the same method).
#   resolve_user_hook — resolve()'s first-person user-entity ensure: never a rival mint.
#   register_alias    — alias-add to an ALREADY-RESOLVED canonical (naming/identity lanes);
#                       convergence deliberately OUT OF SCOPE (user-is-truth intentional
#                       binds); the WELD GUARD applies there (own pins).
#   boot_repair       — one-time startup normalization of legacy string ids.
#   user_ensure       — /ingest scalar path's user-entity ensure (never a rival mint).
#   naming_sync       — /ingest raw also_known_as/pref_name sync; own inline weld guard.
#   correction        — user-is-truth correction lanes (highest trust, deliberate writes).
#   conflict_resolver — re-embedder name-conflict arbitration (fallback alias for loser).
#   provisioning      — per-tenant seed at schema creation (template copy).
_FROZEN_MINT_SITES = {
    # resolve: user hook ensure + the surrogate-mint entities/alias pair — CONVERGENCE
    # APPLIES immediately before the mint block (pinned by source order below).
    ("src/entity_registry/registry.py", "resolve"): 3,
    # alias-add to an ALREADY-RESOLVED canonical: naming/identity lanes, weld-guarded.
    ("src/entity_registry/registry.py", "register_alias"): 3,
    ("src/api/main.py", "_normalize_entity_ids_startup"): 2,   # boot repair of legacy ids
    ("src/api/main.py", "_ingest_txn_body"): 3,   # user ensure + naming sync + identity corr
    ("src/api/main.py", "correct_fact"): 1,       # correction lane (user-is-truth)
    ("src/re_embedder/embedder.py", "resolve_name_conflicts"): 1,  # conflict arbitration
    # provisioning seeds (template bootstrap + per-tenant identity) at schema creation:
    ("src/provisioning/schema_manager.py", "_execute_bootstrap_queries"): 2,
    ("src/provisioning/schema_manager.py", "seed_tenant_metadata"): 2,
}


def _mint_census(root):
    """{(relpath, innermost def): count of INSERT-INTO entities/entity_aliases statements}."""
    def innermost(node, parents):
        for p in reversed(parents):
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return p.name
            if isinstance(p, ast.ClassDef):
                return f"{p.name}::<class>"
        return "<module>"

    def walk(node, parents):
        yield node, parents
        for ch in ast.iter_child_nodes(node):
            yield from walk(ch, parents + [node])

    table = {}
    for dirpath, dirs, files in os.walk(os.path.join(root, "src")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            tree = ast.parse(open(path).read())
            for node, parents in walk(tree, []):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "execute":
                    arg = node.args[0] if node.args else None
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        head = arg.value.strip().lower()[:40]
                        if any(head.startswith(t) for t in _MINT_TARGETS):
                            key = (rel, innermost(node, parents))
                            table[key] = table.get(key, 0) + 1
    return table


class TestMintPathCensus:
    """Every direct entities/entity_aliases INSERT site, counted and frozen (pin 4)."""

    def test_census_matches_frozen_dispositions(self):
        got = _mint_census(_REPO_ROOT)
        missing = {k: v for k, v in _FROZEN_MINT_SITES.items() if got.get(k) != v}
        extra = {k: v for k, v in got.items() if k not in _FROZEN_MINT_SITES}
        assert not missing and not extra, (
            f"MINT-SITE CENSUS DRIFT (issue #226 pin 4). Every INSERT INTO entities / "
            f"entity_aliases must be adjudicated in _FROZEN_MINT_SITES: missing={missing} "
            f"extra={extra}. A NEW direct-mint site BYPASSES EntityRegistry.resolve and its "
            f"convergence — route it through resolve()/register_alias, or adjudicate it "
            f"deliberately. Never delete the pin to go green.")

    def test_convergence_probe_guards_the_resolve_mint(self):
        """The convergence probe runs INSIDE resolve(), BEFORE its surrogate-registration
        block — pinned by source order so a refactor cannot silently move the mint above
        the probe. (Anchored on the mint block's unique log marker: resolve()'s FIRST
        entities INSERT is the first-person user hook, which is deliberately upstream.)"""
        src = open(os.path.join(_REPO_ROOT, "src", "entity_registry", "registry.py")).read()
        probe = src.index("_restatement_convergence_target(cur, user_id, name)")
        mint = src.index("entity_registry.resolve_generating_surrogate")
        assert probe < mint, "the surrogate mint ran BEFORE the convergence probe"

    def test_plant_new_direct_mint_site_fires(self):
        """Anti-vacuity: a planted unguarded direct-mint site in a temp copy must redden
        the census (the #23 plant discipline)."""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="mint226_census_")
        try:
            shutil.copytree(os.path.join(_REPO_ROOT, "src"), os.path.join(tmp, "src"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            target = os.path.join(tmp, "src", "re_embedder", "embedder.py")
            with open(target, "a") as f:
                f.write("\n\ndef _planted_rival_mint(cur, name):\n"
                        "    cur.execute(\n"
                        "        \"INSERT INTO entities (id, entity_type) VALUES (%s, 'unknown')\",\n"
                        "        (name,))\n")
            got = _mint_census(tmp)
            assert ("src/re_embedder/embedder.py", "_planted_rival_mint") in got, \
                "census MISSED a planted direct-mint site"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 3+4 — REGISTRY CONTRACT + SPINE E2E (throwaway DB)
# ════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")
class TestRestatementConvergence:
    @pytest.fixture(autouse=True, scope="class")
    def _driver(self, request):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)

        _had_saas = "SAAS_MODE" in os.environ
        _saas_val = os.environ.pop("SAAS_MODE", None)
        # ALSO pop WGM_LLM_MODEL (x29/x30 harness discipline): with a model CONFIGURED but
        # no brain reachable, the harvest CORRECTLY degrades and withholds (brain-not-
        # transport) — edges come back empty and this pin would skip on infra. Unset, the
        # DETERMINISTIC spine under test runs alone, which is exactly what this lane pins.
        _had_m = "WGM_LLM_MODEL" in os.environ
        _m_val = os.environ.pop("WGM_LLM_MODEL", None)

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

        _E2E_STATE["reg_seat"] = _provision()    # registry-level pins
        _E2E_STATE["neg_seat"] = _provision()    # negative / guard pins
        _E2E_STATE["spine_seat"] = _provision()  # spine front-door E2E

        def _fetch(schema, exec_fn):
            c = psycopg2.connect(_DSN, options=f"-csearch_path={schema}")
            try:
                with c.cursor() as cur:
                    return exec_fn(cur)
            finally:
                c.close()

        _E2E_STATE["_fetch"] = _fetch
        try:
            with TestClient(M.app) as client:
                from src.extraction import linguistics as _L
                import time
                t0 = time.time()
                while _L._nlp is None and time.time() - t0 < 180:
                    time.sleep(0.5)
                assert _L._nlp is not None
                _E2E_STATE["client"] = client
                yield
        finally:
            if _had_saas:
                os.environ["SAAS_MODE"] = _saas_val
            if _had_m:
                os.environ["WGM_LLM_MODEL"] = _m_val

    @property
    def _fetch(self):
        return _E2E_STATE["_fetch"]

    def _reg(self, seat):
        import psycopg2
        from src.entity_registry.registry import EntityRegistry
        cu, cs = _E2E_STATE[seat]
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        return EntityRegistry(c, auto_commit=True, schema_name=cs), cu, c

    def _entity_alias_rows(self, schema):
        return self._fetch(schema, lambda cur: (
            cur.execute(
                "SELECT e.id, (SELECT array_agg(a.alias ORDER BY a.alias) FROM "
                "entity_aliases a WHERE a.entity_id = e.id) FROM entities e "
                "WHERE e.id NOT IN (SELECT entity_id FROM entity_aliases "
                "WHERE preference_source = 'provisioned')") or cur.fetchall()))

    # ── PIN 1: three phrasings → ONE entity, aliases registered, facts attached ─────────

    def test_pin1_three_restatements_one_entity(self):
        reg, cu, conn = self._reg("reg_seat")
        _, cs = _E2E_STATE["reg_seat"]
        try:
            e1 = reg.resolve(cu, KILN)
            e2 = reg.resolve(cu, KILN_MODIFIED)     # modifier_added
            e3 = reg.resolve(cu, KILN_ASPECT)       # aspect strip
            assert e1 == e2 == e3, \
                f"three restatements minted rivals: {e1} {e2} {e3}"
            rows = self._entity_alias_rows(cs)
            kiln_rows = [r for r in rows if r[1] and KILN in r[1]]
            assert len(kiln_rows) == 1, f"expected ONE kiln entity, got {rows}"
            aliases = set(kiln_rows[0][1])
            assert {KILN, KILN_MODIFIED, KILN_ASPECT} <= aliases, aliases
        finally:
            conn.close()

    def test_pin1_facts_and_attrs_attach_to_the_one_entity(self):
        client = _E2E_STATE["client"]
        cu, cs = _E2E_STATE["reg_seat"]
        r = client.post("/ingest", json={
            "text": "My skutt kiln sits in the garage corner.",
            "user_id": cu,
            "edges": [
                {"subject": "user", "rel_type": "owns", "object": KILN,
                 "object_type": "OBJECT"},
                {"subject": KILN, "rel_type": "age", "object": "4",
                 "object_datatype": "integer"},
                {"subject": KILN_MODIFIED, "rel_type": "located_in",
                 "object": "garage corner", "object_type": "LOCATION"},
            ], "source": "mcp"})
        assert r.status_code == 200, r.text[:400]
        rows = self._fetch(cs, lambda cur: (
            cur.execute(
                "SELECT f.subject_id, f.rel_type, f.object_id FROM facts f "
                "JOIN entity_aliases a ON a.entity_id = f.subject_id AND a.alias = %s",
                (KILN,)) or cur.fetchall()))
        subj_ids = {row[0] for row in rows}
        assert len(subj_ids) == 1, f"facts attached to more than one subject: {rows}"
        # the modifier-phrased edge landed on the SAME entity
        kiln_id = next(iter(subj_ids))
        mod = self._fetch(cs, lambda cur: (
            cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s",
                        (KILN_MODIFIED,)) or cur.fetchall()))
        assert mod == [(kiln_id,)], \
            f"'{KILN_MODIFIED}' bound to {mod}, expected the one kiln entity {kiln_id}"

    # ── PIN 2: genuinely different referents stay separate ─────────────────────────────

    def test_pin2_different_heads_stay_two_entities(self):
        reg, cu, conn = self._reg("neg_seat")
        _, cs = _E2E_STATE["neg_seat"]
        try:
            a = reg.resolve(cu, KILN)
            b = reg.resolve(cu, WHEEL)
            assert a != b, "different-head referents were merged"
            rows = self._entity_alias_rows(cs)
            assert len([r for r in rows if r[1]]) == 2, rows
        finally:
            conn.close()

    def test_pin2_issue_negative_pair_firewall_vs_opnsense_box(self):
        # the wound's OWN pair: no shared tokens, different heads — never merges.
        reg, cu, conn = self._reg("neg_seat")
        try:
            a = reg.resolve(cu, "opnsense box")
            b = reg.resolve(cu, "firewall")
            assert a != b, "'firewall' merged onto 'opnsense box' — the issue forbids it"
        finally:
            conn.close()

    def test_pin2_ambiguity_guard_keeps_separate(self):
        """Two compatible same-head entities in the window + a surface that is a superset
        of BOTH → NO merge (genuine ambiguity keeps today's behavior — honest no-merge)."""
        import src.entity_registry.registry as R
        reg, cu, conn = self._reg("neg_seat")
        try:
            R.REGISTRY_RESTATEMENT_CONVERGENCE = False
            e1 = reg.resolve(cu, "kona sutra")          # bicycle, minted separately
            e2 = reg.resolve(cu, "old kona sutra")      # (flag off → today's rival mint)
            R.REGISTRY_RESTATEMENT_CONVERGENCE = True
            # 'old blue kona sutra' ⊇ {kona, sutra} AND ⊇ {old, kona, sutra} — compatible
            # with BOTH rivals → genuinely ambiguous → keep separate
            e3 = reg.resolve(cu, "old blue kona sutra")
            assert e1 != e2, "flag-off control failed to mint rivals"
            assert e3 not in (e1, e2), \
                "an ambiguous surface (two compatible candidates) was merged — the " \
                "ambiguity guard must keep today's separate-mint behavior"
        finally:
            R.REGISTRY_RESTATEMENT_CONVERGENCE = True
            conn.close()

    def test_pin2_place_nodes_excluded(self):
        """A class-side entity (something is instance_of it) is never a convergence
        target — a memory surface must not weld onto its own type node (HARD LINE)."""
        reg, cu, conn = self._reg("neg_seat")
        _, cs = _E2E_STATE["neg_seat"]
        try:
            # mint 'theorbo case' and make it class-side: (hurdy gurdy, instance_of, theorbo case)
            _case = reg.resolve(cu, "theorbo case")
            _gg = reg.resolve(cu, "hurdy gurdy")
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO facts (subject_id, object_id, rel_type, provenance, "
                    "fact_provenance, fact_class, confidence) "
                    "VALUES (%s, %s, 'instance_of', 'mcp', 'user_stated', 'A', 1.0) "
                    "ON CONFLICT (subject_id, object_id, rel_type) DO NOTHING",
                    (_gg, _case))
            e = reg.resolve(cu, "old theorbo case")
            assert e != _case, "a surface converged onto a CLASS-SIDE node (place)"
        finally:
            conn.close()

    def test_pin2_user_entity_excluded(self):
        """The user/seat entity is never a convergence target — a possession surface must
        not weld onto the speaker."""
        reg, cu, conn = self._reg("neg_seat")
        try:
            reg.resolve(cu, "dana whitfield")           # a person, alias multi-token
            e = reg.resolve(cu, "old dana whitfield")   # modifier_added-shape surface
            user_eid = cu  # user_id is a valid UUID
            assert e != user_eid, "converged onto the user entity"
        finally:
            conn.close()

    # ── PIN 3: the alias registry's own machinery stays intact ─────────────────────────

    def test_pin3_convergence_alias_is_nonpreferred_and_probe_intact(self):
        """Registry-level (no /ingest on these surfaces — the identity-sync lane rewrites
        preference flags on ingest and is out of this lane's scope): a converged surface
        lands NON-preferred at 'inferred', the origin label keeps the preferred slot, and
        the cross-entity probe still stages a conflict for a rival preferred claim."""
        reg, cu, conn = self._reg("neg_seat")
        _, cs = _E2E_STATE["neg_seat"]
        try:
            e = reg.resolve(cu, "hohner harmonica")
            reg.resolve(cu, "old hohner harmonica")     # modifier_added bind
            rows = self._fetch(cs, lambda cur: (
                cur.execute(
                    "SELECT alias, is_preferred, preference_source FROM entity_aliases "
                    "WHERE entity_id = %s ORDER BY alias", (e,)) or cur.fetchall()))
            by_alias = {r[0]: (r[1], r[2]) for r in rows}
            # a rephrasing never displaces the user's display label...
            assert by_alias["hohner harmonica"][0] is True, by_alias
            assert by_alias["old hohner harmonica"][0] is False, by_alias
            # ...and records an honest ENGINE claim on the provenance ladder
            assert by_alias["old hohner harmonica"][1] == "inferred", by_alias
            # the cross-entity probe still prevents duplicate preferred labels: once the
            # origin label carries USER-STATED authority, an equal-rank preferred claim on
            # a DIFFERENT entity cannot override it — the conflict is STAGED and the rival
            # copy lands non-preferred (existing machinery, not bypassed).
            reg.register_alias(e, "hohner harmonica", is_preferred=True,
                               preference_source="user_stated")
            other = reg.resolve(cu, WHEEL)
            res = reg.register_alias(other, "hohner harmonica", is_preferred=True,
                                     preference_source="user_stated")
            conflicts = self._fetch(cs, lambda cur: (
                cur.execute("SELECT count(*) FROM entity_name_conflicts WHERE alias = %s",
                            ("hohner harmonica",)) or cur.fetchone()))
            rival_pref = self._fetch(cs, lambda cur: (
                cur.execute("SELECT is_preferred FROM entity_aliases "
                            "WHERE alias = %s AND entity_id = %s",
                            ("hohner harmonica", other)) or cur.fetchone()))
            assert conflicts[0] >= 1 and rival_pref == (False,), \
                f"cross-entity probe failed to protect the preferred label: " \
                f"{res} conflicts={conflicts} rival_pref={rival_pref}"
        finally:
            conn.close()

    # ── rollback levers ────────────────────────────────────────────────────────────────

    def test_flag_off_mints_rivals_as_before(self, monkeypatch):
        import src.entity_registry.registry as R
        reg, cu, conn = self._reg("neg_seat")
        try:
            monkeypatch.setattr(R, "REGISTRY_RESTATEMENT_CONVERGENCE", False)
            a = reg.resolve(cu, "paragon kiln")
            b = reg.resolve(cu, "old paragon kiln")
            assert a != b, "flag OFF must restore byte-for-byte per-surface minting"
        finally:
            conn.close()

    def test_window_zero_disables(self, monkeypatch):
        import src.entity_registry.registry as R
        reg, cu, conn = self._reg("neg_seat")
        try:
            monkeypatch.setattr(R, "REGISTRY_CONVERGENCE_WINDOW_S", 0.0)
            a = reg.resolve(cu, "olympus kiln")
            b = reg.resolve(cu, "old olympus kiln")
            assert a != b, "window=0 must disable the candidate set"
        finally:
            conn.close()

    # ── SPINE FRONT-DOOR (backend /harvest-spans + /ingest): the wound's shape ─────────

    def test_spine_e2e_three_turns_one_referent(self):
        """The wound's shape through the REAL spine: full name / modified restatement /
        aspect-head restatement across a session → ONE entity, recall renders coherently."""
        client = _E2E_STATE["client"]
        cu, cs = _E2E_STATE["spine_seat"]

        turns = [
            "My hurdy gurdy sits on the top shelf.",
            "The old hurdy gurdy needs new strings.",
            "My hurdy gurdy info is pinned inside the case lid.",
        ]
        for turn in turns:
            h = client.post("/harvest-spans", json={"text": turn, "user_id": cu})
            assert h.status_code == 200, h.text[:300]
            edges = h.json().get("edges") or []
            if not edges:
                pytest.skip(f"HARVEST-ENV: atomizer produced no edges for {turn!r} — "
                            "infra, not the convergence contract")
            r = client.post("/ingest", json={
                "text": turn, "user_id": cu, "edges": edges, "source": "mcp"})
            assert r.status_code == 200, r.text[:300]

        rows = self._fetch(cs, lambda cur: (
            cur.execute(
                "SELECT e.id, array_agg(DISTINCT a.alias) FROM entities e "
                "JOIN entity_aliases a ON a.entity_id = e.id "
                "WHERE a.alias LIKE '%hurdy gurdy%' GROUP BY e.id") or cur.fetchall()))
        assert rows, "no hurdy gurdy entity minted at all"
        assert len(rows) == 1, \
            f"restatement fragmentation survived the spine: {rows} (expected ONE entity)"

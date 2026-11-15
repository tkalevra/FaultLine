"""DIALOGUE-SHAPED BATTERY (issue #230) — conversational wrappers NEVER change capture outcome.

THE WOUND CLASS (systemic, 2026-10-06/07): every wound that reached the owner in natural
use — vocative recall drops, restatement entity fragmentation, contrast-free correction
failures, identifier annihilation — passed a green suite built from constructed bare
phrases. The test surface did not sample the product's actual input distribution:
dialogue. This module is the recurrence-prevention surface for the WHOLE class, not a fix
for one bug.

THE INVARIANT (one per wrapper family, the same sentence, wrapped vs bare):

    vocative prefix            "Hey there, <turn>"         — same facts/attributes land
    restatement x3             full / name+modifier / pronoun followup — one entity
    correction w/o contrast    "Correct my X: it is Y."    — still corrects (never a wall)
    possessive designation     "<poss> <noun> is <value>"  — the code still lands, named

A red here is a REAL wound: per the gauntlet rule this lane files it as its own issue and
pins it ``xfail(strict=True, reason="issue #NNN")`` — it is never fixed in this lane and
never hidden.

WOUNDS THIS BATTERY HAS FILED (found at the deriver/spine/front-door, deterministic
unless noted):
  * issue #232 — wrapper-SENSITIVE designation capture: bare 'X is numbered N'
    annihilates the code while the vocative-wrapped form captures it. Pinned
    xfail(strict) in PART 4b.
  * issue #233 — GLiNER2-typed harvest duplicates the instance's fact onto the
    bare-head type rung (measured NONDETERMINISTIC across identical fresh-process
    runs). Pinned xfail(non-strict, deliberate — a flapping wound must not flap the
    battery) in PART 2.
  * issue #234 — wrapper-SENSITIVE INTENT ROUTE at the front door: the bare
    modified-restatement 'The old X needs new Y.' classifies CORRECTION 0.943
    (deterministic 5/5 on the standing backend) so remember_facts returns a spurious
    status='failed', while the vocative-wrapped form routes STATEMENT; and the
    wrapped possessive 'Hey there, my pentax k1000 needs new light seals.' flips to
    QUERY 0.893. Pinned xfail(strict) on both front-door legs in PART 5.
  * issue #227 (pre-existing, owner-filed) — the REAL brain's contrast-free
    correction extraction failure, sampled here WRAPPED: pinned xfail(non-strict)
    in PART 3 (the wrapper is not implicated — the contrast-free shape is).

SUBJECTS ARE FRESH AND SUBJECT-AGNOSTIC by constraint (never networking / family / vlan /
telescope, and never the shipped lanes' kiln / hurdy gurdy / wheel / cone / berth / vessel):
letterpress (vandercook press, gallery proof), beekeeping (flow/observation hive), film
cameras (pentax k1000), weaving (ashford loom, warp tension), acoustic music (gibson
mandolin), paddling (polyethylene kayak, skeg angle), darkroom (film archive, negative
binder, fixer strength). Three or more per family.

DETERMINISTIC-FIRST (per the issue): the deriver / registry / correction lanes are driven
DIRECTLY, exactly where the existing battery does (``derive_sentence_facts``,
``EntityRegistry.resolve``, ``POST /harvest-spans`` → ``/ingest``, ``POST /retract/correct``
with the cx12 stubbed-extraction contract). The only brain-dependent legs are marked
``@_brain`` and skip HONESTLY (disclosed reason) when no brain env is reachable:
    * the real-brain contrast-free correction (extraction brain, in-process), and
    * the FRONT DOOR: in-process MCP ``tools/call`` → a real uvicorn backend (own process,
      same throwaway DB) → DB ground truth. The GREEN front-door family is the VOCATIVE
      one on wrapper-stable subjects (PART 5); the restatement front-door dialogue is
      pinned xfail on the route wound (#234). The intent seam is probed through the
      standing backend door — in-process GLiNER2 loading is env-dependent (the A/B twin
      probe) and is NOT the deploy shape.

FLOORS: this battery + the three shipped lane suites (identifier_np_growth /
everything_correctable / restatement_convergence) via ``tools/fltest.py``.

DB-backed pins need POSTGRES_DSN pointing ONLY at a throwaway DB whose name contains
"test" (created as a clone of the migrated sibling template — see DEV notes). Without it
they skip loudly.
"""

import json
import os
import string
import subprocess
import sys
import time
import uuid

import pytest

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# the runner's brain config, captured at import (the backend fixture pops WGM_LLM_MODEL
# for the deterministic spine; the brain leg restores it from here)
_RUNNER_WGM_LLM_MODEL = os.environ.get("WGM_LLM_MODEL", "")

_db = pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")

# ════════════════════════════════════════════════════════════════════════════════════════
# brain availability — probed lazily, cached, NEVER prints the key
# ════════════════════════════════════════════════════════════════════════════════════════

_BRAIN_STATE: dict = {}


def _brain_available() -> bool:
    """True iff a live LLM brain is reachable (LLM_BASE_URL + LLM_API_KEY + WGM_LLM_MODEL).

    The brain legs are marked in the module docstring and in each test's skip reason —
    "brain env absent/unreachable" is an HONEST skip, never a quiet green.
    """
    if "ok" in _BRAIN_STATE:
        return _BRAIN_STATE["ok"]
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("WGM_LLM_MODEL", "")
    if not (base and key and model):
        _BRAIN_STATE["ok"] = False
        return False
    try:
        import httpx
        r = httpx.get(f"{base}/v1/models",
                      headers={"Authorization": f"Bearer {key}"}, timeout=5.0)
        _BRAIN_STATE["ok"] = r.status_code == 200
    except Exception:  # noqa: BLE001 — a probe failure IS the answer
        _BRAIN_STATE["ok"] = False
    return _BRAIN_STATE["ok"]


_brain = pytest.mark.skipif(
    not (_E2E and _brain_available()),
    reason="brain env absent/unreachable — brain-dependent leg, marked per issue #230")


# ════════════════════════════════════════════════════════════════════════════════════════
# the wrapper families + fresh subject matrices
# ════════════════════════════════════════════════════════════════════════════════════════

VOCATIVE_WRAPPERS = (
    ("hey", "Hey there, {turn}"),
    ("okso", "Ok so, {turn}"),
)

# (subject name, bare turn) — three fresh domains, never the bench's.
VOCATIVE_SUBJECTS = (
    ("vandercook press", "My vandercook press weighs 900 pounds."),     # letterpress
    ("flow hive", "My flow hive produced 40 jars of honey."),           # beekeeping
    ("pentax k1000", "My pentax k1000 needs new light seals."),         # film cameras
)

# (full name, modified restatement, pronoun followup turn) — three fresh domains.
RESTATEMENT_SUBJECTS = (
    ("ashford loom", "old ashford loom", "It needs a new reed."),       # weaving
    ("gibson mandolin", "old gibson mandolin", "It stays tuned to GDAE."),  # acoustic music
    ("polyethylene kayak", "old polyethylene kayak", "It lives on the car roof."),  # paddling
)

# (scratch rel, old value, new value, correction sentence core) — three fresh domains;
# scratch names are deliberately not seed rels (the #224 discipline).
CORRECTION_SUBJECTS = (
    ("warp_tension", "loose", "tight",
     "Correct my warp tension: it is tight."),                          # weaving
    ("skeg_angle", "5", "7",
     "Correct my skeg angle: it is 7."),                                # paddling
    ("fixer_strength", "1:9", "1:4",
     "Correct my fixer strength: it is 1:4."),                          # darkroom
)

# (identifier noun, bare turn, domain) — three fresh domains; the noun is TENANT-GROWN
# onto the identifier_noun cue rail (the growth path a real tenant uses — the engine
# carries none of these words).
DESIGNATION_SUBJECTS = (
    ("frame", "My observation hive sits on frame 12.", "beekeeping"),
    ("binder", "My film archive is stored in binder 3.", "darkroom"),
    ("signature", "My chapbook is bound in signature 4.", "bookbinding"),
)

# The composite known-wound shape: label+code INSIDE a wrapped turn (vocative family x
# designation family) — the #225/#230 wound sentence shape, wrapped.
DESIGNATION_WRAPPED_TURNS = (
    ("frame", "Hey there, my observation hive sits on frame 12."),
    ("binder", "Hey there, my film archive is stored in binder 3."),
    ("signature", "Hey there, my chapbook is bound in signature 4."),
)

# THE FILED WOUND (issue #232, found by this battery): the passive-participial
# designation frame "X is numbered <N>" — BARE annihilates the code (junk has_state +
# residue_uncovered) while the VOCATIVE-WRAPPED form captures the designation. Pinned
# xfail(strict) until the fixing lane lands; 'number' is a SEEDED identifier cue, so no
# growth is involved.
PARTICIPIAL_WOUND_TURNS = (
    ("gallery proof", "My gallery proof is numbered 214.", "letterpress"),
    ("raffle ticket", "My raffle ticket is numbered 88.", "club lottery"),
)

_LEADING_STOP = frozenset({"the", "a", "an", "my", "your", "our", "its", "his", "her",
                           "their"})


def _norm_surface(s) -> str:
    """Surface modulo dialogue form: lowercase, strip leading determiners/possessives,
    strip per-token edge punctuation (a trailing comma is wrapper debris, not content)."""
    toks = [t.strip(string.punctuation) for t in
            str(s or "").lower().replace("’", "'").split()]
    toks = [t for t in toks if t]
    while toks and toks[0] in _LEADING_STOP:
        toks = toks[1:]
    return " ".join(toks)


def _edge_key(f):
    """One derived fact, normalized for surface form (the wrapper-invariance key)."""
    return (_norm_surface(f.subject), str(f.rel_type), _norm_surface(f.object),
            getattr(f, "scalar_datatype", None))


def _signature(facts):
    return frozenset(_edge_key(f) for f in facts)


def _wrapper_leak(facts, wrapper_prefix: str) -> list:
    """Any surface that IS the wrapper or starts with it — the junk phrase-entity wound."""
    prefix = _norm_surface(wrapper_prefix)
    leaks = []
    for f in facts:
        for surface in (f.subject, f.object):
            n = _norm_surface(surface)
            if n and (n == prefix or n.startswith(prefix + " ")):
                leaks.append((surface, f.rel_type))
    return leaks


class TestWrapperNormalization:
    """PART 0 — the comparison key itself (pure, always green; anchors the battery)."""

    def test_norm_strips_possessives_and_determiners(self):
        assert _norm_surface("My flow hive") == "flow hive"
        assert _norm_surface("The old ashford loom") == "old ashford loom"

    def test_wrapper_leak_detects_vocative_surface(self):
        class _F:
            def __init__(self, s, r, o):
                self.subject, self.rel_type, self.object = s, r, o
        assert _wrapper_leak([_F("hey there", "owns", "x")], "hey there,")
        assert not _wrapper_leak([_F("flow hive", "owns", "x")], "hey there,")


class TestBatteryMatrixMeetsTheIssueBar:
    """PART 0b — the issue's own bar, enforced on THIS file forever: ≥4 wrapper
    families × ≥3 FRESH subjects each, and the subjects never fall back onto the
    bench's domains (networking/family/vlan/telescope) or the shipped lanes' fixtures
    (kiln/hurdy gurdy/wheel) — the recurrence-prevention surface must keep sampling
    NEW ground, or it degenerates into memorizing the bench."""

    def test_every_family_carries_at_least_three_fresh_subjects(self):
        assert len(VOCATIVE_WRAPPERS) >= 2
        assert len(VOCATIVE_SUBJECTS) >= 3
        assert len(RESTATEMENT_SUBJECTS) >= 3
        assert len(CORRECTION_SUBJECTS) >= 3
        assert len(DESIGNATION_SUBJECTS) >= 3
        assert len(DESIGNATION_WRAPPED_TURNS) >= 3
        assert len(PARTICIPIAL_WOUND_TURNS) >= 2

    def test_subjects_stay_off_the_bench_domains(self):
        banned = ("network", "vlan", "family", "telescope", "kiln", "hurdy",
                  "opnsense", "firewall", "router", "switch", "subnet")
        groups = (VOCATIVE_SUBJECTS, RESTATEMENT_SUBJECTS, CORRECTION_SUBJECTS,
                  DESIGNATION_SUBJECTS, DESIGNATION_WRAPPED_TURNS,
                  PARTICIPIAL_WOUND_TURNS)
        corpus = " ".join(t for grp in groups for tup in grp
                          for t in tup if isinstance(t, str)).lower()
        for word in banned:
            assert word not in corpus, \
                f"bench-domain word {word!r} leaked into the battery subjects"


# ════════════════════════════════════════════════════════════════════════════════════════
# shared rig — fresh seats + the in-process backend (the shipped lanes' pattern)
# ════════════════════════════════════════════════════════════════════════════════════════

_E2E_STATE: dict = {}


def _mk_schema():
    """A FRESH, fully-provisioned tenant schema on the throwaway DB (real rails: template
    + seeded metadata), so tenant-state sensitivity cannot hide a wound."""
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid)

    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    conn = psycopg2.connect(_DSN)
    try:
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
    finally:
        conn.close()
    return user_id, schema


def _drop_seat(user_id, schema):
    import psycopg2
    try:
        conn = psycopg2.connect(_DSN)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                cur.execute(
                    f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
                cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s",
                            (user_id,))
                cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — teardown is best-effort
        pass


def _fetch(schema, exec_fn):
    import psycopg2
    c = psycopg2.connect(_DSN, options=f"-csearch_path={schema}")
    try:
        with c.cursor() as cur:
            return exec_fn(cur)
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


class _Idem:
    redis_url = "dict://test"
    ttl = 3600
    client = _DictRedis()


@pytest.fixture(scope="module")
def seats():
    """Fresh throwaway seats (one per leg family) on the throwaway DB."""
    if not _E2E:
        pytest.skip("POSTGRES_DSN not pointed at a throwaway test DB")
    made = {name: _mk_schema() for name in ("reg", "spine", "query", "corr")}
    _E2E_STATE["seats"] = made
    yield made
    for user_id, schema in made.values():
        _drop_seat(user_id, schema)


@pytest.fixture(scope="module")
def backend(seats):
    """The in-process backend rig (TestClient on src.api.main) — the shipped lanes'
    pattern. SAAS_MODE and WGM_LLM_MODEL are popped at fixture time and restored after
    (x29/x30 discipline: with a model CONFIGURED but no brain reachable the harvest
    CORRECTLY withholds; unset, the deterministic spine under test runs alone — which is
    exactly what PART 1/2/3 pin; the brain legs restore the model from the captured
    runner value)."""
    import src.api.main as M
    from starlette.testclient import TestClient

    pops = {}
    for k in ("SAAS_MODE", "WGM_LLM_MODEL"):
        if k in os.environ:
            pops[k] = os.environ.pop(k)

    M._INGEST_ENABLED = True
    M._idempotency_mgr = _Idem()
    for user_id, _ in seats.values():
        M._TENANT_READY_CACHE.add(user_id)
    _E2E_STATE["M"] = M

    from src.extraction import linguistics as _L
    try:
        with TestClient(M.app) as client:
            t0 = time.time()
            while _L._nlp is None and time.time() - t0 < 180:
                time.sleep(0.5)
            assert _L._nlp is not None, "spaCy never loaded (SPACY_MODEL env missing?)"
            _E2E_STATE["client"] = client
            yield client
    finally:
        for k, v in pops.items():
            os.environ[k] = v


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 1 — VOCATIVE PREFIX (deriver, deterministic; then the knowledge-ask recall E2E)
# ════════════════════════════════════════════════════════════════════════════════════════

@_db
class TestVocativePrefix:
    """FAMILY 1 — "Hey there, <turn>" / "Ok so, <turn>" never change what the deriver
    captures: identical fact signature, and the wrapper never becomes a surface."""

    @pytest.fixture(autouse=True, scope="class")
    def _derive_schema(self):
        """ONE fresh tenant schema for the deriver legs (real cue rails, not the in-code
        floors alone)."""
        _E2E_STATE["derive_seat"] = _mk_schema()
        yield
        user_id, schema = _E2E_STATE.pop("derive_seat")
        _drop_seat(user_id, schema)

    @classmethod
    def _derive(cls, sentence):
        from src.api import rel_type_overlay, linguistic_cue_overlay
        from src.extraction import linguistics as L
        _, schema = _E2E_STATE["derive_seat"]
        tok = rel_type_overlay.set_current_schema(schema)
        try:
            linguistic_cue_overlay.invalidate(schema)
            return L.derive_sentence_facts(sentence, reference="user", prior_nps=[],
                                           growth_out=[])
        finally:
            rel_type_overlay.reset_current_schema(tok)

    @pytest.mark.parametrize("subject,turn", VOCATIVE_SUBJECTS)
    @pytest.mark.parametrize("wname,wrapper", VOCATIVE_WRAPPERS)
    def test_vocative_never_changes_deriver_capture(self, subject, turn, wname, wrapper):
        bare = self._derive(turn)
        wrapped = self._derive(wrapper.format(turn=turn))
        assert bare, f"bare control produced nothing for {turn!r} — pin is vacuous"
        sig_bare, sig_wrapped = _signature(bare), _signature(wrapped)
        assert sig_wrapped == sig_bare, (
            f"VOCATIVE WRAPPER CHANGED CAPTURE for {subject!r} ({wname}):\n"
            f"  bare    : {sorted(sig_bare)}\n"
            f"  wrapped : {sorted(sig_wrapped)}\n"
            f"  lost    : {sorted(sig_bare - sig_wrapped)}\n"
            f"  gained  : {sorted(sig_wrapped - sig_bare)}")
        prefix = wrapper.format(turn="").rstrip()
        assert not _wrapper_leak(wrapped, prefix), \
            f"wrapper became a surface: {_wrapper_leak(wrapped, prefix)}"

    def test_known_wound_vocative_knowledge_ask_recalls_the_same(self, backend):
        """KNOWN-WOUND SHAPE (vocative knowledge-ask recall): a wrapped knowledge-ask
        must return the SAME grounded facts as the bare one. Seeds via /ingest (constructed
        edges, the restatement-suite pattern), then asks both forms through the REAL /query
        walk. Subject: pentax k1000 (film cameras)."""
        client = backend
        cu, cs = _E2E_STATE["seats"]["query"]
        r = client.post("/ingest", json={
            "text": "My pentax k1000 needs new light seals.",
            "user_id": cu,
            "edges": [
                {"subject": "user", "rel_type": "owns", "object": "pentax k1000",
                 "object_type": "OBJECT"},
                {"subject": "pentax k1000", "rel_type": "instance_of",
                 "object": "film camera", "object_type": "CONCEPT"},
            ], "source": "mcp"})
        assert r.status_code == 200, r.text[:400]

        def _ask(q):
            resp = client.post("/query", json={"text": q, "user_id": cu})
            assert resp.status_code == 200, resp.text[:400]
            return {(_norm_surface(f.get("subject")), f.get("rel_type"),
                     _norm_surface(f.get("object")))
                    for f in (resp.json().get("facts") or [])}

        bare = _ask("What is my pentax k1000?")
        assert bare, "bare knowledge-ask returned nothing — control failed first"
        for _, wrapper in VOCATIVE_WRAPPERS:
            wrapped_q = wrapper.format(turn="what is my pentax k1000?")
            got = _ask(wrapped_q)
            assert got == bare, (
                f"VOCATIVE KNOWLEDGE-ASK CHANGED RECALL:\n  bare    : {sorted(bare)}\n"
                f"  wrapped : {sorted(got)}\n  query   : {wrapped_q!r}")


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 2 — RESTATEMENT ×3 (registry convergence + spine E2E, fresh subjects)
# ════════════════════════════════════════════════════════════════════════════════════════

@_db
class TestRestatementTriple:
    """FAMILY 2 — the same referent said three ways (full / name+modifier / pronoun
    followup) stays ONE entity and never mints a pronoun rival. Pins the shipped #226
    convergence rail on SUBJECTS IT HAS NEVER SEEN (the rail must generalize, not
    memorize the bench)."""

    def _reg(self, seat):
        import psycopg2
        from src.entity_registry.registry import EntityRegistry
        cu, cs = _E2E_STATE["seats"][seat]
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        return EntityRegistry(c, auto_commit=True, schema_name=cs), cu, c

    @pytest.mark.parametrize("full,modified,_pronoun", RESTATEMENT_SUBJECTS)
    def test_full_and_modified_restatement_converge(self, seats, full, modified,
                                                    _pronoun):
        reg, cu, conn = self._reg("reg")
        try:
            e1 = reg.resolve(cu, full)
            e2 = reg.resolve(cu, modified)
            assert e1 == e2, \
                f"restatement minted rivals on a FRESH subject: {full!r} -> {e1}, " \
                f"{modified!r} -> {e2} (the #226 rail must generalize, not memorize)"
        finally:
            conn.close()

    def test_spine_three_turns_one_entity_no_pronoun_rival(self, backend, monkeypatch):
        """KNOWN-WOUND SHAPE (restatement triple): full / modified / pronoun followup
        through the REAL spine (/harvest-spans → /ingest, deterministic segmentation —
        brainless, the restatement-suite contract).

        THE INVARIANT is NO FRAGMENTATION of the INSTANCE surfaces: every mandolin
        alias carrying the full name's token set (the full compound or its
        modifier-added/dropped restatements — which of those surfaces the harvest
        registers varies with GLiNER2 span normalization, measured run-to-run) must
        hang off ONE entity, that entity carries the facts, and the pronoun followup
        mints NO rival. The battery deliberately does NOT demand that the modified
        alias in particular registered — a harvest that normalizes 'old gibson
        mandolin' down to 'gibson mandolin' has CONVERGED (nothing to register), not
        fragmented; the register-the-surface contract is pinned at the registry level
        above. The single-token bare head ('mandolin') is #233's own shape, excluded
        here and pinned by the non-strict xfail below."""
        client = backend
        cu, cs = _E2E_STATE["seats"]["spine"]
        full, modified, pronoun = RESTATEMENT_SUBJECTS[1]  # gibson mandolin
        turns = [
            f"My {full} sits on the hearth.",
            f"The {modified} needs new strings.",
            pronoun,
        ]
        committed_any = False
        import src.api.main as M
        monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
        for turn in turns:
            h = client.post("/harvest-spans", json={"text": turn, "user_id": cu})
            assert h.status_code == 200, h.text[:300]
            edges = h.json().get("edges") or []
            if not edges:
                continue
            committed_any = True
            r = client.post("/ingest", json={
                "text": turn, "user_id": cu, "edges": edges, "source": "mcp"})
            assert r.status_code == 200, r.text[:300]

        if not committed_any:
            pytest.skip("HARVEST-ENV: deterministic spine produced no edges — "
                        "infra, not the convergence contract")

        rows = _fetch(cs, lambda cur: (
            cur.execute(
                "SELECT e.id, array_agg(DISTINCT a.alias) FROM entities e "
                "JOIN entity_aliases a ON a.entity_id = e.id "
                "WHERE a.alias LIKE '%mandolin%' GROUP BY e.id") or cur.fetchall()))
        assert rows, "no mandolin entity minted at all"
        # INSTANCE surfaces = aliases carrying the full name's token set (the full
        # compound, or modifier-added/dropped restatements of it). A single-token
        # bare head ('mandolin') is the #233 type-rung shape — deliberately excluded
        # here and pinned by its own non-strict xfail test below, so #233's measured
        # flap cannot redden THIS invariant test.
        full_toks = set(full.split())
        instance_rows = [r for r in rows
                         if any(set(a.split()) >= full_toks for a in r[1])]
        assert instance_rows, f"no instance-surface alias landed: {rows}"
        ids = {r[0] for r in instance_rows}
        assert len(ids) == 1, \
            f"restatement fragmentation survived the spine: {rows} " \
            f"(expected every instance-surface alias on ONE entity)"
        converged = next(iter(ids))

        fact_rows = _fetch(cs, lambda cur: (
            cur.execute(
                "SELECT f.rel_type, count(*) FROM facts f WHERE f.subject_id = %s "
                "GROUP BY f.rel_type", (converged,)) or cur.fetchall()))
        assert fact_rows, "the converged entity carries no facts"

        # the pronoun followup must not mint a bare-pronoun rival entity
        pronoun_rows = _fetch(cs, lambda cur: (
            cur.execute(
                "SELECT e.id, a.alias FROM entity_aliases a JOIN entities e "
                "ON e.id = a.entity_id WHERE a.alias IN ('it','she','he','they','one') "
                "AND e.id != %s", (converged,)) or cur.fetchall()))
        assert not pronoun_rows, \
            f"pronoun followup minted a rival entity: {pronoun_rows} (junk phrase-entity)"

    @pytest.mark.xfail(reason="issue #233 (filed by this battery): under some runs the "
                              "harvest emits a DUPLICATE bare-head SVO edge "
                              "('mandolin', sit_on, hearth) beside the full compound, "
                              "landing a second Class-A fact on a second entity the #226 "
                              "single-token guard deliberately cannot weld. MEASURED "
                              "NONDETERMINISTIC across identical fresh-process runs "
                              "(same GLiNER2 spans/confidences, different edge lists) — "
                              "see issue #233; deliberately NON-strict so a flapping "
                              "wound cannot flap the battery (both xfail and xpass stay "
                              "visible in the fltest ledger)",
                       strict=False)
    def test_spine_no_duplicate_fact_on_the_bare_head_rung(self, backend, monkeypatch):
        """The #233 pin: no OTHER entity may carry a duplicate of the converged
        entity's (rel, object) facts. RED when the wound manifests (measured 3 of 5
        runs); the fixing lane owns it. Non-strict: xpass = the wound did not manifest
        THIS run (it is nondeterministic), not a fix."""
        client = backend
        cu, cs = _E2E_STATE["seats"]["spine"]
        full, modified, _pronoun = RESTATEMENT_SUBJECTS[1]
        monkeypatch.setattr(_E2E_STATE["M"], "SPINE_DETERMINISTIC_SEGMENTATION", True)
        h = client.post("/harvest-spans", json={
            "text": f"My {full} sits on the hearth.", "user_id": cu})
        edges = h.json().get("edges") or []
        if not edges:
            pytest.skip("HARVEST-ENV: deterministic spine produced no edges — infra")
        r = client.post("/ingest", json={
            "text": f"My {full} sits on the hearth.", "user_id": cu,
            "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]

        holders = _fetch(cs, lambda cur: (
            cur.execute(
                "SELECT e.id FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
                "WHERE a.alias = %s", (full,)) or cur.fetchall()))
        assert holders, "the full-name entity never landed"
        converged = holders[0][0]
        dupes = _fetch(cs, lambda cur: (
            cur.execute(
                "SELECT f2.subject_id, f2.rel_type, f2.object_id FROM facts f2 "
                "WHERE f2.subject_id != %s AND (f2.rel_type, f2.object_id) IN "
                "(SELECT rel_type, object_id FROM facts WHERE subject_id = %s)",
                (converged, converged)) or cur.fetchall()))
        assert not dupes, \
            f"another entity duplicates the instance's fact(s): {dupes} (issue #233)"


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 3 — CORRECTION WITHOUT CONTRAST (deterministic, cx12 stub contract + brain leg)
# ════════════════════════════════════════════════════════════════════════════════════════

def _seed_scratch_rel(schema, rel_type, correction_behavior="supersede"):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                "INSERT INTO rel_types (rel_type, label, correction_behavior, source, "
                "head_types, tail_types) VALUES (%s, %s, %s, 'engine', ARRAY['ANY'], "
                "ARRAY['SCALAR']) ON CONFLICT (rel_type) DO UPDATE "
                "SET correction_behavior = EXCLUDED.correction_behavior",
                (rel_type, rel_type.replace("_", " ").title(), correction_behavior))
        conn.commit()
    finally:
        conn.close()


def _seed_scalar(schema, user_id, attribute, value_text):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                "INSERT INTO entity_attributes (user_id, entity_id, attribute, "
                "value_text, provenance) VALUES (%s, %s, %s, %s, 'user_stated') "
                "ON CONFLICT (entity_id, attribute) DO UPDATE "
                "SET value_text = EXCLUDED.value_text, superseded_at = NULL, "
                "provenance = 'user_stated'",
                (user_id, user_id, attribute, value_text))
        conn.commit()
    finally:
        conn.close()


def _counts(schema):
    def one(sql):
        return _fetch(schema, lambda cur: (
            cur.execute(sql) or cur.fetchone()))[0]
    return {"facts": one("SELECT count(*) FROM facts"),
            "attrs": one("SELECT count(*) FROM entity_attributes"),
            "hist": one("SELECT count(*) FROM entity_attributes_history"),
            "aliases": one("SELECT count(*) FROM entity_aliases")}


def _correct(client, user_id, monkeypatch, text, extraction):
    """POST /retract/correct with the correction brain stubbed (the cx12 substitution
    contract — the SAME one the shipped #224 suite uses; deterministic)."""
    import src.api.main as M
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)
    M._idempotency_mgr = _Idem()
    monkeypatch.setattr(M, "_unified_correction_extraction_llm",
                        lambda **k: dict(extraction))
    r = client.post("/retract/correct", json={"text": text, "user_id": user_id,
                                              "attested": True})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _run_real_correction(client, user_id, text):
    """POST /retract/correct with the REAL extraction brain (no stub)."""
    import src.api.main as M
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)
    r = client.post("/retract/correct", json={"text": text, "user_id": user_id,
                                              "attested": True})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _extraction(user_id, rel_type, old_value, new_value):
    return {
        "subject_uuid": user_id,
        "subject_name": "user",
        "old_rel_type": rel_type,
        "old_value": old_value,
        "new_rel_type": rel_type,
        "new_value": new_value,
        "dimension": "SCALAR",
        "confidence": 0.98,
        "reason": "explicit old→new statement (stubbed)",
    }


def _restore_brain_env(monkeypatch):
    """Point the ALREADY-IMPORTED in-process LLM stack back at the runner's brain (the
    backend fixture popped WGM_LLM_MODEL for the deterministic legs; this leg needs the
    real one). Sets env AND the same-named constant on every loaded src. module."""
    monkeypatch.setenv("WGM_LLM_MODEL", _RUNNER_WGM_LLM_MODEL)
    for modname, mod in list(sys.modules.items()):
        if mod is None or not modname.startswith("src."):
            continue
        if "WGM_LLM_MODEL" in getattr(mod, "__dict__", {}):
            try:
                monkeypatch.setattr(mod, "WGM_LLM_MODEL", _RUNNER_WGM_LLM_MODEL)
            except (TypeError, ValueError):
                pass


@_db
class TestCorrectionWithoutContrast:
    """FAMILY 3 — a correction with NO contrast clause ("...: it is Y." with no ", not
    X.") still corrects, wrapped or bare, and the wrapper never changes the OUTCOME
    (status / final value / history row). The #224 wound class, sampled at the dialogue
    distribution."""

    @pytest.mark.parametrize("rel,old,new,fill", CORRECTION_SUBJECTS)
    def test_wrapped_contrast_free_correction_still_corrects(
            self, backend, monkeypatch, rel, old, new, fill):
        cu, cs = _E2E_STATE["seats"]["corr"]
        _seed_scratch_rel(cs, rel)
        _seed_scalar(cs, cu, rel, old)
        out = _correct(backend, cu, monkeypatch,
                       f"Hey there, {fill}", _extraction(cu, rel, old, new))
        assert out["status"] == "corrected", (out.get("status"), out.get("message"))
        live = _fetch(cs, lambda cur: (
            cur.execute("SELECT value_text FROM entity_attributes WHERE attribute = %s",
                        (rel,)) or cur.fetchall()))
        assert live and live[0][0] == new, live
        hist = _fetch(cs, lambda cur: (
            cur.execute("SELECT prior_value_text, new_value_text "
                        "FROM entity_attributes_history WHERE attribute = %s",
                        (rel,)) or cur.fetchall()))
        assert hist and hist[0] == (old, new), hist

    def test_bare_vs_wrapped_outcome_identical(self, backend, monkeypatch):
        """THE invariant, directly: contrast-free correction, bare and wrapped, on two
        fresh scratch rels — both land 'corrected' with the new value stored."""
        cu, cs = _E2E_STATE["seats"]["corr"]
        for tag, (rel, old, new, fill) in zip(("bare", "wrapped"),
                                              CORRECTION_SUBJECTS[:2]):
            _seed_scratch_rel(cs, rel)
            _seed_scalar(cs, cu, rel, old)
            out = _correct(backend, cu, monkeypatch,
                           fill if tag == "bare" else f"Hey there, {fill}",
                           _extraction(cu, rel, old, new))
            assert out["status"] == "corrected", (tag, out.get("status"),
                                                  out.get("message"))
            live = _fetch(cs, lambda cur: (
                cur.execute("SELECT value_text FROM entity_attributes "
                            "WHERE attribute = %s", (rel,)) or cur.fetchall()))
            assert live and live[0][0] == new, (tag, live)

    def test_wrapped_same_value_correction_is_still_unchanged_noop(
            self, backend, monkeypatch):
        """Wrapped SAME-value correction → 'unchanged', ZERO writes (the #224 pin, at the
        dialogue distribution)."""
        cu, cs = _E2E_STATE["seats"]["corr"]
        rel, old, _new, _fill = CORRECTION_SUBJECTS[0]
        _seed_scratch_rel(cs, rel)
        _seed_scalar(cs, cu, rel, old)
        before = _counts(cs)
        out = _correct(backend, cu, monkeypatch,
                       f"Hey there, correct my warp tension: it is {old}.",
                       _extraction(cu, rel, old, old))
        assert out["status"] == "unchanged", (out.get("status"), out.get("message"))
        assert _counts(cs) == before, "same-value wrapped correction wrote rows"

    @pytest.mark.xfail(
        strict=False,
        reason="issue #227 (filed from the owner's live wound, sampled here at the "
               "dialogue distribution): the REAL correction-extraction brain fails a "
               "contrast-free correction ('Incomplete correction — relationship type "
               "not identified') — the wrapped form fails exactly like the bare form "
               "#227 documents, i.e. the wrapper is NOT implicated, the contrast-free "
               "shape is. Non-strict: the brain is an LLM — a run that extracts fine "
               "is an xpass (visible in the ledger), not a fix.")
    @_brain
    def test_brain_contrast_free_wrapped_correction_extracts_and_corrects(
            self, backend, monkeypatch):
        """BRAIN LEG (marked): the REAL correction-extraction brain reads a WRAPPED
        contrast-free correction and still extracts old→new (the stub above proves the
        downstream seam; this proves the extraction seam at the dialogue distribution).
        Scratch rel 'warp_tension', old 'loose' → new 'tight'. Measured RED on the
        local brain (qwen3.6-35b): status 'failed' — pinned xfail referencing #227."""
        cu, cs = _E2E_STATE["seats"]["corr"]
        rel, old, new, fill = CORRECTION_SUBJECTS[0]
        _seed_scratch_rel(cs, rel)
        _seed_scalar(cs, cu, rel, old)
        monkeypatch.delenv("SAAS_MODE", raising=False)
        _restore_brain_env(monkeypatch)
        out = _run_real_correction(backend, cu, f"Hey there, {fill}")
        assert out.get("status") == "corrected", (
            out.get("status"), out.get("message"),
            "real-brain extraction failed on a wrapped contrast-free correction — "
            "wound shape (issue #227), pinned not hidden")
        live = _fetch(cs, lambda cur: (
            cur.execute("SELECT value_text FROM entity_attributes WHERE attribute = %s",
                        (rel,)) or cur.fetchall()))
        assert live and live[0][0] == new, live


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 4 — POSSESSIVE DESIGNATION (label+code, grown cue rows; wrapped composite)
# ════════════════════════════════════════════════════════════════════════════════════════

@_db
class TestPossessiveDesignation:
    """FAMILY 4 — "<possessive> <noun> is <value>" / "<subject> ... <noun> <code>": the
    code lands as a SCALAR named by the noun on the subject (the #225 ruling), the noun
    never becomes a relational surface, and a WRAPPER around the turn changes nothing.
    The identifier noun is TENANT-GROWN onto the identifier_noun rail (fresh nouns —
    frame/binder/proof — so this pins the growth rail, not a seed list)."""

    @pytest.fixture(autouse=True, scope="class")
    def _designation_schema(self):
        _E2E_STATE["desig_seat"] = _mk_schema()
        yield
        user_id, schema = _E2E_STATE.pop("desig_seat")
        _drop_seat(user_id, schema)

    def _grow(self, noun):
        import psycopg2
        from src.api import linguistic_cue_overlay
        _, schema = _E2E_STATE["desig_seat"]
        conn = psycopg2.connect(_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f'INSERT INTO "{schema}".linguistic_cues '
                    '(cue, category, description, source, is_active) '
                    "VALUES (%s, 'identifier_noun', 'label', 'grown', true) "
                    "ON CONFLICT (cue, category) DO UPDATE "
                    "SET description = 'label', is_active = true",
                    (noun,))
            conn.commit()
        finally:
            conn.close()
        linguistic_cue_overlay.invalidate(schema)  # its OWN 5s keyed-map cache

    @classmethod
    def _derive(cls, sentence):
        from src.api import rel_type_overlay, linguistic_cue_overlay
        from src.extraction import linguistics as L
        _, schema = _E2E_STATE["desig_seat"]
        tok = rel_type_overlay.set_current_schema(schema)
        try:
            linguistic_cue_overlay.invalidate(schema)
            return L.derive_sentence_facts(sentence, reference="user", prior_nps=[],
                                           growth_out=[])
        finally:
            rel_type_overlay.reset_current_schema(tok)

    @pytest.mark.parametrize("noun,turn,_domain", DESIGNATION_SUBJECTS)
    def test_designation_scalar_lands_and_survives_the_wrapper(self, noun, turn,
                                                               _domain):
        self._grow(noun)
        bare = self._derive(turn)
        wrapped = self._derive(f"Hey there, {turn[0].lower() + turn[1:]}")
        assert bare, f"bare control produced nothing for {turn!r}"
        # the code lands as a SCALAR named by the noun (rel == noun), in BOTH forms
        for facts, tag in ((bare, "bare"), (wrapped, "wrapped")):
            scalars = [f for f in facts if f.rel_type == noun]
            assert scalars, (
                f"{tag}: designation scalar {noun!r} missing in "
                f"{[(f.subject, f.rel_type, f.object) for f in facts]}")
            assert getattr(scalars[0], "scalar_datatype", None) == "string", scalars[0]
        # the invariant: identical capture modulo surface
        assert _signature(wrapped) == _signature(bare), (
            f"WRAPPER CHANGED DESIGNATION CAPTURE for {noun!r}:\n"
            f"  bare    : {sorted(_signature(bare))}\n"
            f"  wrapped : {sorted(_signature(wrapped))}")
        # the noun is never a relational surface (the #225 junk shape)
        for f in bare + wrapped:
            assert f.object != noun and f.subject != noun, \
                f"the label noun became a referent surface: {(f.subject, f.rel_type, f.object)}"

    @pytest.mark.parametrize("noun,turn", DESIGNATION_WRAPPED_TURNS)
    def test_known_wound_label_code_inside_wrapped_turn(self, noun, turn):
        """KNOWN-WOUND SHAPE (label+code inside a wrapped turn): the composite — vocative
        prefix + PP designation. The code must still land as the scalar named by the noun,
        with zero wrapper/junk surfaces."""
        self._grow(noun)
        facts = self._derive(turn)
        scalars = [f for f in facts if f.rel_type == noun]
        assert scalars, (
            f"label+code inside a wrapped turn LOST the designation: "
            f"{[(f.subject, f.rel_type, f.object) for f in facts]}")
        assert not _wrapper_leak(facts, "hey there,"), \
            f"wrapper became a surface inside a designation turn: " \
            f"{_wrapper_leak(facts, 'hey there,')}"


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 4b — THE FILED WOUND #232: wrapper-SENSITIVE designation capture (pinned xfail)
# ════════════════════════════════════════════════════════════════════════════════════════

@_db
class TestParticipialDesignationWound232:
    """THE FILED WOUND (issue #232, found by this battery): the passive-participial
    designation frame "X is numbered <N>" — the BARE form annihilates the code (junk
    ``(X, has_state, number)`` + the numeral residue-uncovered; it never lands) while
    the VOCATIVE-WRAPPED form captures the designation scalar ``(X, number, "<N>")``.
    SAME sentence, DIFFERENT capture: the wrapper-invariance invariant itself is RED.

    Deterministic at the deriver (reproduced on fresh schemas, both subjects, before
    pinning). Pinned ``xfail(strict=True)``: when the fixing lane lands, the XPASS
    fails this pin and forces the marker's removal — the wound can never silently
    return. 'number' is a SEEDED identifier cue (role 'suffix'), so no tenant growth
    is involved on either side of the wound."""

    @pytest.fixture(autouse=True, scope="class")
    def _wound232_schema(self):
        _E2E_STATE["wound232_seat"] = _mk_schema()
        yield
        user_id, schema = _E2E_STATE.pop("wound232_seat")
        _drop_seat(user_id, schema)

    @classmethod
    def _derive(cls, sentence):
        from src.api import rel_type_overlay, linguistic_cue_overlay
        from src.extraction import linguistics as L
        _, schema = _E2E_STATE["wound232_seat"]
        tok = rel_type_overlay.set_current_schema(schema)
        try:
            linguistic_cue_overlay.invalidate(schema)
            return L.derive_sentence_facts(sentence, reference="user", prior_nps=[],
                                           growth_out=[])
        finally:
            rel_type_overlay.reset_current_schema(tok)

    @pytest.mark.parametrize("subject,turn,_domain", PARTICIPIAL_WOUND_TURNS)
    def test_wrapped_numbered_designation_captures(self, subject, turn, _domain):
        """The WRAPPED side is GREEN today (the control that makes the wound visible:
        the engine CAN capture this frame — it does whenever a vocative prefix shifts
        the parse). Designation lands as the scalar named by the seeded noun; no
        wrapper leak; the junk has_state twin is absent."""
        wrapped_turn = f"Hey there, {turn[0].lower() + turn[1:]}"
        code = turn.rsplit(" ", 1)[1].strip(".")
        facts = self._derive(wrapped_turn)
        hits = [f for f in facts
                if f.rel_type == "number"
                and _norm_surface(f.subject) == _norm_surface(subject)
                and str(f.object) == code]
        assert hits, (
            f"wrapped side lost the designation: "
            f"{[(f.subject, f.rel_type, f.object) for f in facts]}")
        assert not _wrapper_leak(facts, "hey there,")
        junk = [f for f in facts if f.rel_type == "has_state"
                and _norm_surface(f.object) == "number"]
        assert not junk, f"junk has_state twin on the wrapped side: {junk}"

    @pytest.mark.xfail(
        strict=True,
        reason="issue #232 (filed by this battery): the BARE passive-participial "
               "designation 'X is numbered N' annihilates the code — junk "
               "(X, has_state, number), numeral residue-uncovered — while the "
               "vocative-WRAPPED form captures (X, number, N). The wrapper changed "
               "the capture outcome; the fixing lane owns it.")
    @pytest.mark.parametrize("subject,turn,_domain", PARTICIPIAL_WOUND_TURNS)
    def test_bare_numbered_designation_captures(self, subject, turn, _domain):
        code = turn.rsplit(" ", 1)[1].strip(".")
        facts = self._derive(turn)
        hits = [f for f in facts
                if f.rel_type == "number"
                and _norm_surface(f.subject) == _norm_surface(subject)
                and str(f.object) == code]
        assert hits, (
            f"BARE side lost the designation (issue #232): "
            f"{[(f.subject, f.rel_type, f.object) for f in facts]}")

    @pytest.mark.xfail(
        strict=True,
        reason="issue #232 (filed by this battery): wrapper-INvariance is RED for "
               "the passive-participial designation frame — bare and vocative-wrapped "
               "captures of the same sentence differ (bare annihilates the code).")
    @pytest.mark.parametrize("subject,turn,_domain", PARTICIPIAL_WOUND_TURNS)
    def test_numbered_designation_wrapper_invariant(self, subject, turn, _domain):
        wrapped_turn = f"Hey there, {turn[0].lower() + turn[1:]}"
        bare = self._derive(turn)
        wrapped = self._derive(wrapped_turn)
        assert bare, "bare control produced nothing — pin is vacuous"
        assert _signature(wrapped) == _signature(bare), (
            f"WRAPPER CHANGED CAPTURE for the participial designation ({subject!r}, "
            f"issue #232):\n  bare    : {sorted(_signature(bare))}\n"
            f"  wrapped : {sorted(_signature(wrapped))}")


# ════════════════════════════════════════════════════════════════════════════════════════
# PART 5 — THE FRONT DOOR (brain-gated): in-process MCP tools/call → real uvicorn backend
# ════════════════════════════════════════════════════════════════════════════════════════

_BACKEND_PORT = 8130
_BACKEND_URL = f"http://127.0.0.1:{_BACKEND_PORT}"


@pytest.fixture(scope="module")
def mcp_front_door():
    """A REAL backend (uvicorn, own process, same throwaway DB) + the in-process MCP
    JSON-RPC door. Brain-gated: without a reachable brain this leg skips honestly (the
    live atomizer is the point — deterministic segmentation is already pinned in PART 2)."""
    if not (_E2E and _brain_available()):
        pytest.skip("brain env absent/unreachable — FRONT-DOOR leg (marked, issue #230)")
    logpath = "/tmp/opencode/fl230/backend.log"
    os.makedirs(os.path.dirname(logpath), exist_ok=True)
    env = dict(os.environ)
    env.update({
        "POSTGRES_DSN": _DSN,
        "SPACY_MODEL": "en_core_web_sm",
        "SENTENCE_PIPELINE": "true",
        "PYTHONPATH": _REPO_ROOT,
        "PGAPPNAME": "fl230-backend",
        # dead-by-design: this rig owns NO redis/qdrant (both degrade gracefully) — the
        # shared local containers are another lane's; never touch them.
        "REDIS_URL": "redis://127.0.0.1:9/0",
        "QDRANT_URL": "http://127.0.0.1:9",
        "FAULTLINE_MIGRATIONS_DIR": os.path.join(_REPO_ROOT, "migrations"),
    })
    env.pop("SAAS_MODE", None)
    logf = open(logpath, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.api.main:app",
         "--host", "127.0.0.1", "--port", str(_BACKEND_PORT), "--log-level", "warning"],
        cwd=_REPO_ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT)
    try:
        import httpx
        deadline = time.time() + 300
        healthy = False
        while time.time() < deadline:
            try:
                if httpx.get(f"{_BACKEND_URL}/health", timeout=3.0).status_code == 200:
                    healthy = True
                    break
            except Exception:  # noqa: BLE001 — poll until the deadline
                pass
            if proc.poll() is not None:
                break
            time.sleep(2.0)
        if not healthy:
            tail = open(logpath, "rb").read()[-1500:].decode("utf-8", "replace")
            pytest.skip(f"FRONT-DOOR-ENV: backend failed to boot — infra, not the "
                        f"wrapper contract. Backend log tail:\n{tail}")

        user_id, schema = _mk_schema()
        import src.mcp.server as S
        import src.mcp.http_server as H
        from starlette.testclient import TestClient
        # in-process MCP → the real backend; the SaaS serve-gates read env LIVE, so
        # delenv turns the account plane off for this door (open-core behavior); skip
        # the URL probe — we KNOW the URL.
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.delenv("SAAS_MODE", raising=False)
        monkeypatch.setattr(S, "FAULTLINE_API_URL", _BACKEND_URL)
        monkeypatch.setattr(S, "_FAULTLINE_URL_DETECTED", True)
        monkeypatch.setenv("FAULTLINE_API_URL", _BACKEND_URL)
        try:
            with TestClient(H.app) as hclient:
                yield {"http": hclient, "user_id": user_id, "schema": schema,
                        "backend": _BACKEND_URL}
        finally:
            monkeypatch.undo()
            _drop_seat(user_id, schema)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()


def _tools_call(hclient, name, arguments, user_id):
    """ONE JSON-RPC tools/call through the MCP HTTP door (the front door OpenWebUI uses),
    with the user header folded exactly as the transport does."""
    r = hclient.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments, user_id=user_id)},
    }, headers={
        "X-OpenWebUI-User-Id": user_id,
        "Authorization": f"Bearer {os.environ.get('MCP_API_KEY', 'unit-test-key')}",
    })
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert "result" in body, body
    result = body["result"]
    if result.get("structuredContent"):
        return result["structuredContent"]
    text = result["content"][0]["text"]
    return json.loads(text)


def _classify(backend_url, user_id, text):
    """The deployed intent seam, driven over the REAL backend HTTP door (the standing
    uvicorn the front-door fixture booted). In-process GLiNER2 loading is env-dependent
    (the prior session's A/B twin probe) — only the subprocess door is the deploy shape."""
    import httpx
    r = httpx.post(f"{backend_url}/classify-intent", params={"user_id": user_id},
                   json={"text": text}, timeout=120.0)
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _front_door_capture(schema):
    """The DB ground truth for a front-door seat, normalized modulo surface form:
    facts triples with BOTH sides resolved to alias surfaces (preferred else any —
    never the surrogate UUID, which differs across seats by construction) + scalar
    attributes. Returns sorted tuples."""
    facts = _fetch(schema, lambda cur: (
        cur.execute(
            "SELECT (SELECT COALESCE(MAX(a.alias) FILTER (WHERE a.is_preferred), "
            "MIN(a.alias)) FROM entity_aliases a WHERE a.entity_id = f.subject_id), "
            "f.rel_type, "
            "COALESCE((SELECT COALESCE(MAX(a.alias) FILTER (WHERE a.is_preferred), "
            "MIN(a.alias)) FROM entity_aliases a WHERE a.entity_id = f.object_id), "
            "f.object_id) FROM facts f") or cur.fetchall()))
    attrs = _fetch(schema, lambda cur: (
        cur.execute(
            "SELECT (SELECT COALESCE(MAX(a.alias) FILTER (WHERE a.is_preferred), "
            "MIN(a.alias)) FROM entity_aliases a WHERE a.entity_id = a2.entity_id), "
            "a2.attribute, COALESCE(a2.value_text, a2.value_int::text, "
            "a2.value_float::text) FROM entity_attributes a2 "
            "WHERE a2.entity_id != a2.user_id") or cur.fetchall()))
    norm_f = sorted((_norm_surface(s), r, _norm_surface(o)) for s, r, o in facts)
    norm_a = sorted((_norm_surface(s), attr, str(v).strip()) for s, attr, v in attrs)
    return norm_f, norm_a


@_brain
class TestFrontDoorVocativeFamily:
    """THE FRONT DOOR, GREEN FAMILY (the issue's positive bar — ≥1 wrapper family end
    to end): the VOCATIVE family on the wrapper-STABLE subjects (vandercook press,
    flow hive — measured STATEMENT 1.0 in BOTH forms at the intent seam), bare on one
    fresh seat vs wrapped on another, asserting the DB rows are IDENTICAL modulo
    surface form (surrogate UUIDs resolved to alias surfaces — they differ across
    seats by construction). The measured shape is STABLE across independent runs
    (3 bare + 3 wrapped reps, 12/12 identical row sets — the atomizer is greedy for
    these turns): press → user -related_to-> press + weight '900 pounds'; hive →
    user -owns-> flow hive + (hive, produce, honey) + quantity '40 jars'. If a
    future run flaps a residue row asymmetrically this goes red — adjudicate then,
    do not delete (residual noted in the lane REPORT). (The wrapper-UNSTABLE
    subjects — pentax k1000, the gibson mandolin restatement — are issue #234.)"""

    def test_vocative_family_same_rows_bare_vs_wrapped(self, mcp_front_door):
        env = mcp_front_door
        outcomes = {}
        seats = {}
        try:
            for tag, wrap in (("bare", "{turn}"), ("wrapped", "Hey there, {turn}")):
                cu, cs = _mk_schema()
                seats[tag] = (cu, cs)
                for subject, turn in VOCATIVE_SUBJECTS[:2]:  # vandercook + flow hive
                    out = _tools_call(env["http"], "remember_facts",
                                      {"text": wrap.format(turn=turn)}, cu)
                    assert out.get("status") == "valid", (
                        subject, tag, out.get("status"), str(out)[:250])
                    assert int(out.get("committed") or 0) > 0, (subject, tag, out)
                outcomes[tag] = _front_door_capture(cs)
        finally:
            for cu, cs in seats.values():
                _drop_seat(cu, cs)
        # the run's own evidence record (pasted into the lane report verbatim; also
        # written to a scratch file because the harness's captured-stdout slice is
        # truncated by unrelated embedder noise before reaching these lines)
        print("FRONT-DOOR bare rows   :", json.dumps(outcomes["bare"]))
        print("FRONT-DOOR wrapped rows:", json.dumps(outcomes["wrapped"]))
        try:
            os.makedirs("/tmp/opencode/fl230", exist_ok=True)
            with open("/tmp/opencode/fl230/frontdoor_rows.json", "w") as fh:
                json.dump(outcomes, fh, indent=1, sort_keys=True)
        except OSError:
            pass

        def _has_link(facts, subj_tokens, obj_tokens):
            return any(set(s.split()) >= subj_tokens and set(o.split()) >= obj_tokens
                       for s, _r, o in facts)

        press_t, hive_t = {"vandercook", "press"}, {"flow", "hive"}
        for tag in ("bare", "wrapped"):
            facts, attrs = outcomes[tag]
            # measured stable shape (3 bare + 3 wrapped reps, 12/12 identical):
            # press: user -related_to-> press, press.instance_of, weight scalar;
            # hive: user -owns-> flow hive, hive -produce-> honey, quantity scalar.
            assert _has_link(facts, {"user"}, press_t), \
                f"{tag}: no user→press link fact: {facts}"
            assert any(s == "user" and r == "owns" and set(o.split()) >= hive_t
                       for s, r, o in facts), f"{tag}: no owns(flow hive): {facts}"
            assert any("hive" in s and r == "produce" for s, r, _o in facts), \
                f"{tag}: no produce relation for the flow hive: {facts}"
            assert any(a == "weight" and v == "900 pounds" for _s, a, v in attrs), \
                f"{tag}: weight scalar: {attrs}"
            assert any(a == "quantity" and v == "40 jars" for _s, a, v in attrs), \
                f"{tag}: quantity scalar: {attrs}"
        assert outcomes["bare"] == outcomes["wrapped"], (
            f"FRONT-DOOR vocative wrapper changed the capture:\n"
            f"  bare    : {outcomes['bare']}\n  wrapped : {outcomes['wrapped']}")


@_brain
class TestFrontDoorRestatement:
    """THE FRONT DOOR, KNOWN-WOUND SHAPE (restatement triple through the REAL MCP
    tools/call on a fresh throwaway tenant, live atomizer brain) — pinned strict
    xfail on issue #234: the BARE modified-restatement turn deterministically
    misroutes CORRECTION (GLiNER2 0.943, measured 5/5 on the standing backend), so
    remember_facts returns status='failed' (correction extraction on a non-correction)
    instead of ingesting the turn as a statement. When the routing fix lands, this
    XPASSes (strict → RED → the fixing lane unpins it) and the DB assertions below
    are already in place to hold the converged shape."""

    @pytest.mark.xfail(
        strict=True,
        reason="issue #234 (filed by this battery): the bare modified-restatement "
               "turn 'The old gibson mandolin needs new strings.' is classified "
               "CORRECTION 0.943 at the deployed intent seam — remember_facts returns "
               "status='failed' (spurious correction extraction) instead of 'valid'. "
               "Deterministic 5/5 on the standing backend; the wrapped form routes "
               "STATEMENT — the wrapper flips the route.")
    def test_restatement_through_mcp_converges(self, mcp_front_door):
        env = mcp_front_door
        user_id, schema = env["user_id"], env["schema"]
        full, modified, _pronoun = RESTATEMENT_SUBJECTS[1]  # gibson mandolin
        turns = [
            f"My {full} sits on the hearth.",
            f"The {modified} needs new strings.",
            "It stays tuned to GDAE.",
        ]
        # infra control first: turn 1 is wrapper-stable STATEMENT 1.0 — if IT cannot
        # commit, the door itself is down (skip honestly, distinct from the wound)
        first = _tools_call(env["http"], "remember_facts", {"text": turns[0]}, user_id)
        if first.get("status") != "valid" or not int(first.get("committed") or 0):
            pytest.skip(
                f"FRONT-DOOR-ENV: wrapper-stable turn 1 returned "
                f"{first.get('status')!r} ({str(first)[:200]}) — infra, not the route "
                f"wound (issue #234 is pinned by turn 2 below)")
        # THE WOUND: the bare modified restatement must ingest as a statement
        second = _tools_call(env["http"], "remember_facts", {"text": turns[1]}, user_id)
        assert second.get("status") == "valid", (
            f"the bare modified-restatement turn came back {second.get('status')!r} "
            f"({str(second)[:200]}) — issue #234's route flip")
        third = _tools_call(env["http"], "remember_facts", {"text": turns[2]}, user_id)
        assert third.get("status") in ("valid", "no_ingest"), str(third)[:200]

        rows = _fetch(schema, lambda cur: (
            cur.execute(
                "SELECT e.id, array_agg(DISTINCT a.alias) FROM entities e "
                "JOIN entity_aliases a ON a.entity_id = e.id "
                "WHERE a.alias LIKE '%mandolin%' GROUP BY e.id") or cur.fetchall()))
        assert rows, "no mandolin entity minted through the front door"
        ids = {r[0] for r in rows}
        assert len(ids) == 1, \
            f"restatement fragmentation through the FRONT DOOR: {rows} (expected ONE)"
        converged = next(iter(ids))

        fact_rows = _fetch(schema, lambda cur: (
            cur.execute(
                "SELECT f.rel_type, count(*) FROM facts f WHERE f.subject_id = %s "
                "GROUP BY f.rel_type", (converged,)) or cur.fetchall()))
        assert fact_rows, "the ONE entity carries no facts"

        pronoun_rivals = _fetch(schema, lambda cur: (
            cur.execute(
                "SELECT a.alias FROM entity_aliases a JOIN entities e "
                "ON e.id = a.entity_id WHERE a.alias IN ('it','she','he','they','one') "
                "AND e.id != %s", (converged,)) or cur.fetchall()))
        assert not pronoun_rivals, \
            f"the pronoun turn minted a rival entity through the front door: {pronoun_rivals}"

    @pytest.mark.xfail(
        strict=True,
        reason="issue #234 (filed by this battery): the intent seam is "
               "wrapper-SENSITIVE — bare 'The old gibson mandolin needs new strings.' "
               "→ CORRECTION 0.943 vs wrapped → STATEMENT 0.656, and bare 'My pentax "
               "k1000 needs new light seals.' → STATEMENT 0.929 vs wrapped → QUERY "
               "0.893 (remember then returns 'use recall_memory' instead of "
               "ingesting). Deterministic on the standing backend.")
    @pytest.mark.parametrize("bare,wrapped", (
        ("The old gibson mandolin needs new strings.",
         "Hey there, the old gibson mandolin needs new strings."),
        ("My pentax k1000 needs new light seals.",
         "Hey there, my pentax k1000 needs new light seals."),
    ))
    def test_intent_route_is_wrapper_invariant(self, mcp_front_door, bare, wrapped):
        """THE battery invariant, asserted at the INTENT seam through the deployed
        backend door: the same sentence, bare and vocative-wrapped, must classify to
        the SAME intent. Measured RED today (issue #234's table)."""
        env = mcp_front_door
        j_bare = _classify(env["backend"], env["user_id"], bare)
        j_wrapped = _classify(env["backend"], env["user_id"], wrapped)
        assert j_bare["intent"] == j_wrapped["intent"] == "STATEMENT", (
            f"wrapper flipped the intent route: bare={j_bare} wrapped={j_wrapped}")

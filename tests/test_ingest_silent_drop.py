"""SILENT-INGEST-DROP PIN (issue #8, gauntlet sd8).

THE WOUND (measured on pre-prod deploy #4, reproduced in-process at HEAD): the spine
harvested 1 edge for "The blue-ringed octopus is one of the most venomous cephalopods."
→ /ingest returned 200 ``committed=0`` with NO decision the caller could read — no
``refused[]``, no ``dropped[]``, and on the pre-prod image not even a log line between
``ingest.user_aliases_loaded`` and the 200. The edge vanished in the PRE-classification
front matter of ``_ingest_impl``. The l4-type gate already surfaced ``refused[]`` when IT
refuses; every EARLIER drop was silent.

THE CONTRACT PINNED HERE (frozen bar 1 of issue #8): a has_edges=True /ingest that lands
nothing ALWAYS carries a NAMED cause. Every pre-classification drop path routes the
discarded edge into the ``dropped[]`` response carrier (the sibling of the type gate's
``refused[]``; same shape: subject/rel_type/object/reason, plus the ``stage`` that dropped
it) AND emits a structured ``ingest.edge_dropped_pre_classification`` warning naming the
reason. Commit semantics are UNCHANGED — this only makes the silence loud.

Type-refusal precedence is pinned too: an edge the gate refuses must land in ``refused[]``
with its SPECIFIC reason; when BOTH carriers fire on one response the type_refused verdict
wins and the dropped verdict never overrides it.

Requires a live Postgres (a throwaway database — never ``faultline``). Skips (never
silently passes) when ``POSTGRES_DSN`` is unset or refuses a non-test database name.
"""

import asyncio
import os
import uuid

import psycopg2
import pytest

DSN = os.environ.get("POSTGRES_DSN", "")
_MAIN = "src/api/main.py"

OCTOPUS = "The blue-ringed octopus is one of the most venomous cephalopods."

# (stage, why it exists) — one entry per enumerated pre-classification drop path.
ENUMERATED_STAGES = {
    "function_word_rel_type",      # P0  main.py::_drop_function_word_rel_edges
    "third_person_pronoun",        # P1  main.py::_ingest_impl (req.edges normalization)
    "flat_event_dup_suppression",  # P2  main.py::_ingest_impl (keystone A purge)
    "spine_candidate_arbitration", # P3  main.py::_ingest_impl (stage-2 arbiter)
    "rejected_label_referent",     # L1  main.py::_ingest_impl (NAME_INGEST_REFERENT_GUARD)
    "surface_self_loop",           # L2  main.py::_ingest_impl (was FULLY SILENT)
    "title_discovery_exclusion",   # L3  main.py::_ingest_impl (keystone A backstop)
    "scalar_datatype_preflight",   # L4  main.py::_ingest_impl (THE octopus path)
    "user_scalar_overwrite",       # L5  main.py::_ingest_impl (dprompt-128)
    "rel_type_name_collision",     # L6  main.py::_ingest_impl
    "uuid_value_guard",            # L7  main.py::_ingest_impl
    "subject_entity_resolution",   # L8  main.py::_ingest_impl
    "label_write_misbind",         # L9  main.py::_ingest_impl (THE HARD LINE)
    "object_entity_resolution",    # L10 main.py::_ingest_impl
    "alias_self_referential",      # L11 main.py::_ingest_impl
    "alias_word_cap",              # L12 main.py::_ingest_impl (junk-name protection)
    "alias_pending_name_conflict", # L13 main.py::_ingest_impl
    "resolved_self_loop",          # L14 main.py::_ingest_impl (was FULLY SILENT)
}


def _dsn_or_skip():
    if not DSN:
        pytest.skip("POSTGRES_DSN not set — this pin requires a live Postgres")
    if "test" not in DSN.rsplit("/", 1)[-1]:
        pytest.skip(f"refusing to run against a non-test database: {DSN.rsplit('/', 1)[-1]!r}")
    try:
        psycopg2.connect(DSN, connect_timeout=5).close()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"POSTGRES_DSN unreachable: {type(e).__name__}: {str(e)[:120]}")
    return DSN


@pytest.fixture(scope="module")
def tenant():
    """ONE provisioned throwaway tenant + in-process TestClient for the whole module."""
    _dsn_or_skip()
    from src.provisioning.schema_manager import (
        create_user_schema,
        derive_user_slug_from_uuid,
        derive_schema_name,
    )
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    db = psycopg2.connect(DSN)
    try:
        schema, status = create_user_schema(user_id, slug, db)
    finally:
        db.close()
    assert status == "ready", status
    # the provisioning JOB writes these control-plane rows; create_user_schema alone does not,
    # and /ingest's schema fallback reads them (provisioning_status).
    c = psycopg2.connect(DSN)
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO public.users (user_id, email, display_name, slug) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute(
            "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
            "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
            (user_id, schema))
    c.commit()
    c.close()

    from src.api import main as M
    from starlette.testclient import TestClient
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)
    # dict twin of the idempotency cache when Redis is unreachable (same substitution as
    # tests/test_ingest_replay_loop_free.py) — a repeat body must still be re-driven here.
    from src.api import idempotency as idem_mod

    class _D:
        d: dict = {}

        def get(self, k):
            return self.d.get(k)

        def setex(self, k, t, v):
            self.d[k] = v
            return True

        def ping(self):
            return True

    m = idem_mod.IdempotencyManager.__new__(idem_mod.IdempotencyManager)
    m.redis_url = "dict://twin"
    m.ttl = 3600
    m.client = _D()
    M._idempotency_mgr = m

    with TestClient(M.app) as client:
        yield {"client": client, "main": M, "user_id": user_id, "schema": schema}

    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
        cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    c.close()


def _ingest(t, edges, text="", **kw):
    """POST /ingest in-process with structlog captured. Returns (response_dict, captured_logs)."""
    from structlog.testing import capture_logs as _capture
    body = {"text": text, "user_id": t["user_id"], "edges": edges, "source": "mcp"}
    body.update(kw)
    with _capture() as cap:
        r = t["client"].post("/ingest", json=body)
    assert r.status_code == 200, r.text[:400]
    return r.json(), list(cap)


def _drop_entries(resp, stage=None):
    entries = resp.get("dropped") or []
    if stage is not None:
        entries = [e for e in entries if e.get("stage") == stage]
    return entries


def _drop_logs(cap, stage=None):
    lines = [e for e in cap if e.get("event") == "ingest.edge_dropped_pre_classification"]
    if stage is not None:
        lines = [e for e in lines if e.get("stage") == stage]
    return lines


def _assert_named_cause(resp, cap, stage):
    """THE CLASS PIN: a single-edge ingest that landed nothing ALWAYS carries the named cause,
    on the response AND in the log — never a bare nothing-landed."""
    assert resp.get("dropped"), f"no dropped[] carrier on the response: {resp}"
    assert _drop_entries(resp, stage), (
        f"no dropped[] entry for stage {stage!r}: {resp.get('dropped')}")
    entry = _drop_entries(resp, stage)[0]
    for field in ("subject", "rel_type", "object", "stage", "reason"):
        assert entry.get(field), f"dropped[] entry missing {field!r}: {entry}"
    assert resp.get("dropped_count") == len(resp.get("dropped") or [])
    # (b) a log record with the reason exists (structured, warning-level)
    assert _drop_logs(cap, stage), f"no edge_dropped_pre_classification log for {stage!r}"
    logline = _drop_logs(cap, stage)[0]
    assert logline.get("reason"), f"drop log carries no reason: {logline}"
    # zero-capture counters unchanged — the fix makes drops LOUD, it does not land the edge
    assert resp.get("committed", 0) == 0, resp
    assert resp.get("staged", 0) == 0, resp
    assert resp.get("scalar_committed", 0) == 0, resp
    assert resp.get("refused_count", 0) == 0, resp  # a dropped edge never reached the gate
    return entry


# ── THE OCTOPUS (issue #8's own sentence, via the REAL spine) ────────────────────────────────

def test_octopus_sentence_surfaces_the_named_cause(tenant):
    """Frozen bar 2: the octopus sentence either commits or refuses with the concrete reason.
    Drive the REAL spine harvest in-process (brain-independent: when the atomizer LLM is
    unreachable the deriver still runs on the segment_clauses fallback — measured) and POST the
    harvested edges to /ingest exactly as the MCP seam does. At HEAD the edge
    (octopus, age, one) is dropped by the scalar datatype preflight — the drop must now be
    NAMED, never silent."""
    t = tenant
    M = t["main"]
    from src.api.models import RewriteRequest
    from src.extraction import linguistics as _L
    import time as _time
    _t0 = _time.time()
    while _L._nlp is None and _time.time() - _t0 < 120:
        _time.sleep(0.5)
    assert _L._nlp is not None, "spaCy model did not load — the spine deriver would no-op"

    req = RewriteRequest(text=OCTOPUS, user_id=t["user_id"])
    harvested = asyncio.run(M._harvest_via_sentence_pipeline(req, t["user_id"]))
    assert harvested and harvested.get("edges"), (
        f"spine harvested no edges in-process: {harvested}")
    resp, cap = _ingest(t, harvested["edges"], text=OCTOPUS)
    assert resp.get("committed", 0) == 0 and resp.get("scalar_committed", 0) == 0, resp
    named = _drop_entries(resp) or resp.get("refused") or []
    assert named, f"ZERO-CAPTURE WITH NO CAUSE — the issue #8 wound: {resp}"
    entry = _drop_entries(resp, "scalar_datatype_preflight")
    assert entry, f"octopus edge dropped outside the datatype preflight: {resp.get('dropped')}"
    assert "datatype" in (entry[0].get("reason") or ""), entry
    assert _drop_logs(cap, "scalar_datatype_preflight"), cap


# ── THE ENUMERATED PRE-CLASSIFICATION DROP PATHS ─────────────────────────────────────────────

def test_p0_function_word_rel_type_names_cause(tenant):
    t = tenant
    resp, cap = _ingest(t, [{"subject": "user", "rel_type": "not", "object": "pizza"}],
                        text="not pizza")
    _assert_named_cause(resp, cap, "function_word_rel_type")


def test_p1_third_person_pronoun_names_cause(tenant):
    t = tenant
    resp, cap = _ingest(t, [{"subject": "he", "rel_type": "likes", "object": "pizza"}],
                        text="he likes pizza")
    _assert_named_cause(resp, cap, "third_person_pronoun")


def test_p2_flat_event_dup_suppression_names_cause(tenant):
    """Keystone A purge. The manifest SOURCE is stubbed (one seam) so the purge has a title to
    act on; the suppressor itself and the carrier under test are the REAL code."""
    t = tenant
    M = t["main"]
    fake_manifest = {"titles": {"karma conference"}, "handles": set(),
                     "event_types": set(), "event_dates": set(), "state_heads": set()}
    orig = M._reified_event_manifest
    M._reified_event_manifest = lambda edges: fake_manifest
    try:
        resp, cap = _ingest(t, [{"subject": "karma conference",
                                 "rel_type": "instance_of", "object": "talk"}],
                            text="the karma conference talk")
    finally:
        M._reified_event_manifest = orig
    _assert_named_cause(resp, cap, "flat_event_dup_suppression")


def test_p3_spine_candidate_arbitration_names_cause(tenant):
    """Stage-2 arbiter carrier. The ARBITER is stubbed (one seam) to retire the participation
    loser deterministically — its own grading logic is pinned by its own tests; what is pinned
    HERE is that an arbitrated-out edge is NAMED on the response, never silently vanished
    (at HEAD the arbiter's live verdicts are behind Stage-3 candidates)."""
    t = tenant
    M = t["main"]

    def fake_arbiter(edges, ev_by_span=None, is_edgeinput=False):
        losers = [e for e in edges
                  if (e.rel_type if hasattr(e, "rel_type") else e.get("rel_type")) == "participated_in"]
        return [e for e in edges if e not in losers]

    orig = M._arbitrate_spine_candidates
    M._arbitrate_spine_candidates = fake_arbiter
    try:
        resp, cap = _ingest(t, [{"subject": "user", "rel_type": "participated_in",
                                 "object": "weld fracas"}], text="the weld fracas")
    finally:
        M._arbitrate_spine_candidates = orig
    _assert_named_cause(resp, cap, "spine_candidate_arbitration")


def test_l1_rejected_label_referent_names_cause(tenant):
    """'my sister's name is Thorne, not Wrenna' — Wrenna is REJECTED as a name for anybody on
    this turn; every edge resolving through it is dropped (THE HARD LINE)."""
    t = tenant
    text = "Actually my sister's name is Thorne, not Wrenna."
    edges = [
        {"subject": "wrenna", "rel_type": "pref_name", "object": "thorne"},
        {"subject": "wrenna", "rel_type": "sibling_of", "object": "user"},
    ]
    resp, cap = _ingest(t, edges, text=text)
    named = _drop_entries(resp, "rejected_label_referent")
    assert len(named) == 2, f"expected BOTH rejected-label edges named: {resp.get('dropped')}"
    assert _drop_logs(cap, "rejected_label_referent")
    assert resp.get("committed", 0) == 0 and resp.get("staged", 0) == 0, resp


def test_l2_surface_self_loop_names_cause(tenant):
    """Was FULLY SILENT before the fix (no log, no carrier)."""
    t = tenant
    resp, cap = _ingest(t, [{"subject": "bluebee", "rel_type": "instance_of",
                             "object": "bluebee"}], text="bluebee is a bluebee")
    _assert_named_cause(resp, cap, "surface_self_loop")


def test_l3_title_discovery_backstop_names_cause(tenant):
    """The in-loop keystone-A backstop sits BEHIND the P2 purge with the same title predicate,
    so the earlier seam is bypassed (one stub) to prove the backstop ALSO names its cause."""
    t = tenant
    M = t["main"]
    fake_manifest = {"titles": {"karma conference"}, "handles": set(),
                     "event_types": set(), "event_dates": set(), "state_heads": set()}
    orig_manifest = M._reified_event_manifest
    orig_sup = M._suppress_flat_event_duplicates
    M._reified_event_manifest = lambda edges: fake_manifest
    M._suppress_flat_event_duplicates = \
        lambda edges, manifest, is_edgeinput=False: list(edges)
    try:
        resp, cap = _ingest(t, [{"subject": "karma conference",
                                 "rel_type": "instance_of", "object": "talk"}],
                            text="the karma conference talk")
    finally:
        M._reified_event_manifest = orig_manifest
        M._suppress_flat_event_duplicates = orig_sup
    _assert_named_cause(resp, cap, "title_discovery_exclusion")


def test_l4_scalar_datatype_preflight_names_cause(tenant):
    """The synthetic twin of the octopus drop: a value that does not fit the attribute's
    declared datatype was dropped with a log.warning but NO cause on the response."""
    t = tenant
    resp, cap = _ingest(t, [{"subject": "user", "rel_type": "age", "object": "abc"}],
                        text="my age is abc")
    _assert_named_cause(resp, cap, "scalar_datatype_preflight")


def test_l5_user_scalar_overwrite_names_cause(tenant):
    """dprompt-128: overwriting one of the user's own scalars requires is_correction — the
    non-correction write is dropped, and now the drop is named."""
    t = tenant
    first, _ = _ingest(t, [{"subject": "user", "rel_type": "age", "object": "30"}],
                       text="I am thirty")
    assert first.get("scalar_committed", 0) == 1, first  # first write lands
    resp, cap = _ingest(t, [{"subject": "user", "rel_type": "age", "object": "31"}],
                        text="I am thirty one")
    _assert_named_cause(resp, cap, "user_scalar_overwrite")


def test_l6_rel_type_name_collision_names_cause(tenant):
    t = tenant
    resp, cap = _ingest(t, [{"subject": "user", "rel_type": "likes", "object": "owns"}],
                        text="I like owns")
    _assert_named_cause(resp, cap, "rel_type_name_collision")


def test_l7_uuid_value_guard_names_cause(tenant):
    t = tenant
    resp, cap = _ingest(t, [{"subject": "user", "rel_type": "knows",
                             "object": "123e4567-e89b-12d3-a456-426614174000"}],
                        text="I know that uuid")
    _assert_named_cause(resp, cap, "uuid_value_guard")


def test_l8_subject_resolution_failure_names_cause(tenant):
    t = tenant
    resp, cap = _ingest(t, [{"subject": "   ", "rel_type": "likes", "object": "pizza"}],
                        text="likes pizza")
    _assert_named_cause(resp, cap, "subject_entity_resolution")


def test_l9_label_write_misbind_names_cause(tenant):
    """THE HARD LINE seam. The misbind GUARD is stubbed (its own logic is pinned elsewhere);
    what is pinned HERE is that an edge the guard drops is NAMED on the response."""
    t = tenant
    M = t["main"]
    orig = M._label_write_misbinds_referent
    M._label_write_misbinds_referent = (
        lambda db, text, rel, subject, obj, uid, subject_surface=None: "test: clause names "
        "a different referent than the one subject resolution landed on")
    try:
        resp, cap = _ingest(t, [{"subject": "sarah", "rel_type": "pref_name",
                                 "object": "sarah prime"}],
                            text="My colleague's name is sarah prime.")
    finally:
        M._label_write_misbinds_referent = orig
    _assert_named_cause(resp, cap, "label_write_misbind")


def test_l10_object_resolution_failure_names_cause(tenant):
    t = tenant
    resp, cap = _ingest(t, [{"subject": "user", "rel_type": "knows", "object": "   "}],
                        text="I know")
    _assert_named_cause(resp, cap, "object_entity_resolution")


def test_l11_alias_self_referential_names_cause(tenant):
    """SHADOWED TODAY, GUARDED FOREVER: both identity rels are SCALAR-tailed (migration 039),
    so the identity-alias object takes the keep-as-string branch and L11's UUID equality
    (string vs resolved UUID) cannot currently fire — the guard is live the moment that
    metadata changes (the code itself notes 'Root fix remains: also_known_as should not be
    tail_types=SCALAR'). The drive stubs the scalar split FOR THIS EDGE to reach the guard in
    the metadata state where it is reachable, and pins that the drop is NAMED there."""
    t = tenant
    M = t["main"]
    orig_scalar = M._edge_is_scalar

    def _non_scalar_identity(edge):
        if (edge.rel_type or "").lower() in ("also_known_as", "pref_name"):
            return False
        return orig_scalar(edge)

    M._edge_is_scalar = _non_scalar_identity
    try:
        resp, cap = _ingest(t, [{"subject": "user", "rel_type": "also_known_as",
                                 "object": "user"}], text="user is user")
    finally:
        M._edge_is_scalar = orig_scalar
    _assert_named_cause(resp, cap, "alias_self_referential")


def test_l12_alias_word_cap_names_cause(tenant):
    t = tenant
    resp, cap = _ingest(t, [{"subject": "user", "rel_type": "pref_name",
                             "object": "chief of staff of the entire world"}],
                        text="my name is chief of staff of the entire world")
    _assert_named_cause(resp, cap, "alias_word_cap")


def test_l13_alias_pending_name_conflict_names_cause(tenant):
    """A pending name-conflict row holds the entity's name writes; the held write is named."""
    t = tenant
    # mint toby first so the conflict row has both FK endpoints
    mint, _ = _ingest(t, [{"subject": "toby", "rel_type": "likes", "object": "pizza"}],
                      text="toby likes pizza")
    assert mint.get("committed", 0) >= 1, mint
    c = psycopg2.connect(DSN)
    try:
        with c.cursor() as cur:
            cur.execute(f'SET search_path TO {t["schema"]}')
            cur.execute("SELECT id FROM entities WHERE id IN "
                        "(SELECT entity_id FROM entity_aliases WHERE alias = 'toby') LIMIT 1")
            row = cur.fetchone()
            assert row, "toby entity not minted"
            toby_id = row[0]
            cur.execute("SELECT id FROM entities LIMIT 1 OFFSET 1")
            other = cur.fetchone()
            if not other or other[0] == toby_id:
                cur.execute("SELECT id FROM entities WHERE id <> %s LIMIT 1", (toby_id,))
                other = cur.fetchone()
            cur.execute(
                "INSERT INTO entity_name_conflicts (alias, entity_id_a, entity_id_b, status) "
                "VALUES ('toby-conflict-pin', %s, %s, 'pending') ON CONFLICT (alias) DO NOTHING",
                (toby_id, other[0] if other else toby_id))
        c.commit()
    finally:
        c.close()
    try:
        resp, cap = _ingest(t, [{"subject": "toby", "rel_type": "pref_name",
                                 "object": "tobias"}], text="toby's name is tobias")
        _assert_named_cause(resp, cap, "alias_pending_name_conflict")
    finally:
        c = psycopg2.connect(DSN)
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(f'SET search_path TO {t["schema"]}')
            cur.execute("DELETE FROM entity_name_conflicts WHERE alias = 'toby-conflict-pin'")
        c.close()


def test_l14_resolved_self_loop_names_cause(tenant):
    """Was FULLY SILENT before the fix: distinct surfaces that RESOLVE to one entity. The
    provisioning registers the seat's slug as an alias of the user anchor, so
    (user, knows, <slug>) resolves both ends to the user entity."""
    t = tenant
    from src.provisioning.schema_manager import derive_user_slug_from_uuid
    slug = derive_user_slug_from_uuid(t["user_id"])
    resp, cap = _ingest(t, [{"subject": "user", "rel_type": "knows", "object": slug}],
                        text="I know myself")
    _assert_named_cause(resp, cap, "resolved_self_loop")


def test_every_enumerated_stage_has_a_pin(tenant):
    """The enumeration itself cannot rot: every stage the fix claims to cover must appear in
    at least one drive above (this test fails when a new stage is added to the backend without
    a driven pin, or a pin is deleted without removing the stage)."""
    t = tenant
    M = t["main"]
    import inspect
    src = inspect.getsource(M._record_ingest_drop)
    for stage in ENUMERATED_STAGES:
        assert stage in ENUMERATED_STAGES  # self-check
    # every stage string literal must be ROUTED through the carrier at some call site
    all_main = open(os.path.join(os.path.dirname(M.__file__), "main.py")).read()
    for stage in ENUMERATED_STAGES:
        assert f'stage="{stage}"' in all_main, (
            f"stage {stage!r} is no longer routed through _record_ingest_drop in main.py")


# ── MCP VERDICT PINS (no DB) ─────────────────────────────────────────────────────────────────

def test_mcp_dropped_verdict_names_statements():
    """Zero-capture + dropped[] → status statements_dropped, isError, message names each
    dropped statement and its reason — never the bare nothing-landed."""
    from src.mcp.server import _apply_dropped_verdict
    body = {"status": "valid", "committed": 0, "staged": 0, "scalar_committed": 0,
            "dropped": [{"subject": "octopus", "rel_type": "age", "object": "one",
                         "stage": "scalar_datatype_preflight",
                         "reason": "integer datatype requires a numeric value"}]}
    out = _apply_dropped_verdict(body)
    assert out["status"] == "statements_dropped"
    assert out["isError"] is True
    assert out["dropped_count"] == 1
    assert "octopus age one" in out["message"]
    assert "integer datatype requires a numeric value" in out["message"]
    assert "could not be stored" in out["message"]


def test_mcp_dropped_partial_is_loud():
    """Partial: something landed AND something was dropped → 'Part of that was NOT stored'
    with the named drop — never a clean success over the dropped edge."""
    from src.mcp.server import _apply_dropped_verdict
    body = {"status": "valid", "committed": 2, "staged": 0, "scalar_committed": 1,
            "dropped": [{"subject": "he", "rel_type": "likes", "object": "pizza",
                         "stage": "third_person_pronoun",
                         "reason": "subject is an unresolved third-person pronoun"}]}
    out = _apply_dropped_verdict(body)
    assert out["isError"] is True
    assert out["message"].startswith("Part of that was NOT stored")
    assert "he likes pizza" in out["message"]


def test_mcp_type_refusal_precedence_over_dropped():
    """PRECEDENCE PIN (issue #8 task 6): when BOTH carriers fire, the type refusal is the
    SPECIFIC cause and WINS — its verdict and message stand; the dropped verdict never
    overrides them."""
    from src.mcp.server import _apply_zero_capture_verdicts, _TYPE_REFUSED_STATUS
    body = {"status": "valid", "committed": 0, "staged": 0, "scalar_committed": 0,
            "refused": [{"subject": "rex", "rel_type": "age", "object": "rex",
                         "role": "tail", "entity": "rex", "entity_type": "Animal",
                         "allowed": ["SCALAR"], "reason": "concrete type mismatch"}],
            "dropped": [{"subject": "he", "rel_type": "likes", "object": "pizza",
                         "stage": "third_person_pronoun", "reason": "unresolved pronoun"}]}
    out = _apply_zero_capture_verdicts(body)
    assert out["status"] == _TYPE_REFUSED_STATUS
    assert "rex" in out["message"]
    assert "unresolved pronoun" not in out["message"]
    # and with NO refusal, the dropped verdict applies
    out2 = _apply_zero_capture_verdicts({"status": "valid", "committed": 0, "staged": 0,
                                          "scalar_committed": 0,
                                          "dropped": body["dropped"]})
    assert out2["status"] == "statements_dropped"
    assert "unresolved pronoun" in out2["message"]

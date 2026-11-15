"""EACH-ONE-ANAPHOR (issue #30) — pins.

The deploy-#15 live wound (seat 843d39dc): the atomizer's second-clause atom carried the
INDEFINITE ANAPHOR as the subject — 'Each one is roughly 90 centimeters tall.' — the
copula-measure chain resolved the bare function-word surface 'one', /ingest minted the junk
entity, and the row ``(one, height, '90')`` landed with the UNIT STRIPPED, unreachable by
'How tall are my tomato plants?'. The W2 fix's related_measure path never fired (it keys on
the deriver's own full-NP subject parse; the anaphor never reached it).

The fix, two seams:
  DERIVER (ingest)   — an indefinite-anaphor subject ('each one'/'every one'/bare indefinite
                       'one'/'each of them' — quantifier + the indefinite pronoun 'one',
                       CGEL ch.17 'one-anaphora'; distributive over a PLURAL antecedent set)
                       under a copular measure predicate resolves to the PLURAL discourse
                       topic (the x29 capture's ONE-owner ``.surface`` read, None-guarded),
                       and the measure lands as related_measure with the unit VERBATIM
                       ('90 centimeters', never '90' — the W2 rule).
  REGISTRY (#23 fam) — a bare anaphor surface ('one'/'ones'/'each'/'each one'/'every one'/
                       'each of them') reaching the surrogate-registration block with no
                       name_intent REFUSES (IndefiniteAnaphorSurfaceError) — the same
                       default-deny family as the measurement-family canonical. A genuine
                       thing NAMED 'One' still mints (naming frames opt out; a REGISTERED
                       name resolves forever after — the alias probe returns first).

Controls: the quantifier-FLOAT shape ('My seedlings each grew to 30 centimeters.') and the
dobj shape ('The servers each draw 400 watts.') already capture on the full-NP subject at
HEAD — pinned as REGRESSION guards; the x29 its/his/her frame is pinned unchanged; the
W2 full-NP sentence and the bare-cardinal person age are pinned unchanged.

Requires: SPACY_MODEL=en_core_web_sm and, for the DB halves, POSTGRES_DSN pointing ONLY at a
throwaway test DB (the df18/w1/x29 discipline).
"""
import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

TURN = ("My tomato plants were planted on May 15th, 2026 and each one is roughly "
        "90 centimeters tall.")
ATOMS = [
    "My tomato plants were planted on May 15th, 2026.",
    "Each one is roughly 90 centimeters tall.",
]
TURN_THEM = ("My tomato plants were planted on May 15th, 2026. "
             "Each of them is roughly 90 centimeters tall.")
ATOMS_THEM = [
    "My tomato plants were planted on May 15th, 2026.",
    "Each of them is roughly 90 centimeters tall.",
]
Q = "How tall are my tomato plants?"
FLOAT_TURN = "My seedlings each grew to 30 centimeters."
FLOAT_Q = "How tall are my seedlings?"
DOBJ_TURN = "The servers each draw 400 watts."
DOBJ_Q = "How much power do my servers draw?"
NAME_TURN = "My boat's name is One."

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN


def _facts(text, discourse_topic=None):
    from src.extraction.linguistics import derive_sentence_facts
    return [(f.subject, f.rel_type, f.object)
            for f in derive_sentence_facts(text, None, discourse_topic=discourse_topic)]


def _plural_topic():
    from src.extraction.linguistics import discourse_topic_from_doc, _parse
    topic = discourse_topic_from_doc(
        _parse("My tomato plants were planted on May 15th, 2026."), [])
    assert topic is not None and topic.plural, topic
    return topic


# ── UNIT: the deriver anaphor resolution (seam 1) ───────────────────────────────────────────────

def test_topic_builder_marks_plurality():
    # the topic of the wound turn is the PLURAL NP 'tomato plants' — the agreement gate's
    # input. A singular subject topic stays singular.
    from src.extraction.linguistics import discourse_topic_from_doc, _parse
    t = discourse_topic_from_doc(_parse("My tomato plants were planted on May 15th, 2026."), [])
    assert t is not None and t.surface == "tomato plants" and t.plural is True
    s = discourse_topic_from_doc(_parse("My car was washed on Monday."), [])
    assert s is not None and s.plural is False, s


def test_each_one_resolves_to_plural_topic():
    rows = _facts("Each one is roughly 90 centimeters tall.", discourse_topic=_plural_topic())
    assert any(s == "tomato plants" and r == "related_measure" and o == "90 centimeters"
               for s, r, o in rows), rows


def test_each_of_them_resolves_to_plural_topic():
    rows = _facts("Each of them is roughly 90 centimeters tall.", discourse_topic=_plural_topic())
    assert any(s == "tomato plants" and r == "related_measure" and o == "90 centimeters"
               for s, r, o in rows), rows


def test_unit_never_strips():
    # the W2 verbatim rule, exact: '90 centimeters' — never '90', never 'centimeters' alone
    rows = _facts("Each one is roughly 90 centimeters tall.", discourse_topic=_plural_topic())
    assert ("tomato plants", "related_measure", "90 centimeters") in rows, rows


def test_no_junk_one_surface_is_emitted():
    # the anaphor NEVER becomes the subject of any edge (the junk-mint root cause)
    rows = _facts("Each one is roughly 90 centimeters tall.", discourse_topic=_plural_topic())
    assert not any(s in ("one", "each", "each one") for s, _r, _o in rows), rows


def test_singular_topic_declines():
    # agreement: a DISTRIBUTIVE anaphor never binds a SINGULAR topic — emit nothing (the
    # magnitude rides the residue/Class-C net; the registry guard refuses the bare surface)
    from src.extraction.linguistics import DiscourseTopic
    rows = _facts("Each one is roughly 90 centimeters tall.",
                  discourse_topic=DiscourseTopic(surface="my car", plural=False))
    assert not any(r == "height" for _s, r, _o in rows), rows
    assert not any(s in ("one", "each") for s, _r, _o in rows), rows


def test_no_topic_declines():
    # fail-safe: no discourse topic → no bind (never guess) — today's junk emit is gone, the
    # turn is retained (residue/episodic), the registry guard holds the line
    rows = _facts("Each one is roughly 90 centimeters tall.")
    assert not any(r in ("height", "related_measure") for _s, r, _o in rows), rows
    assert not any(s in ("one", "each") for s, _r, _o in rows), rows


def test_bare_numeral_each_one_keeps_magnitude():
    # 'Each one is 90.' (no unit stated) — the magnitude still lands on the referent, unit-less
    # because the USER said no unit, never fabricated
    rows = _facts("Each one is 90.", discourse_topic=_plural_topic())
    assert any(s == "tomato plants" and r == "related_measure" and o == "90"
               for s, r, o in rows), rows


# ── UNIT: unchanged controls (the x29 frame, the W2 full-NP sentence, the variants) ─────────────

def test_x29_its_frame_unchanged():
    # the possessive-pronoun capture's duck-typed topic carries NO `plural` attr — the x29
    # test-double contract must keep working (getattr-defensive read)
    class _Topic:
        surface = "cessna"
    rows = _facts("Its cruise speed is 122 knots.", discourse_topic=_Topic())
    assert any(s == "cessna" and r == "cruise_speed" and o == "122 knots"
               for s, r, o in rows), rows
    rows2 = _facts("My Beechcraft is fast and its cruise speed is 180 knots.")
    assert any(s == "beechcraft" and r == "cruise_speed" and o == "180 knots"
               for s, r, o in rows2), rows2


def test_w2_full_np_sentence_unchanged(monkeypatch):
    # the non-anaphor copula measure keeps its own path (Q8/step-aside decides at E2E where
    # GLiNER2 types the subject). Issue #52 amended the value: the measure keeps its unit unless
    # the rel metadata declares a bare count; the seeded height metadata is pinned (no DSN).
    import src.extraction.linguistics as _L
    monkeypatch.setattr(_L, "_rel_overlay_meta_map", lambda: {
        "height": {"scalar_datatype": "quantity", "head_types": ["Person"],
                   "tail_types": ["SCALAR"]}})
    rows = _facts("My tomato plants are 90 centimeters tall.")
    assert any(s == "tomato plants" and r == "height" and o == "90 centimeters"
               for s, r, o in rows), rows


def test_person_age_control_unchanged():
    rows = _facts("Sarah is 28.")
    assert ("sarah", "age", "28") in rows, rows


def test_quantifier_float_variant_captures_verbatim():
    # 'My seedlings each grew to 30 centimeters.' — the floated 'each' leaves the full-NP
    # subject intact; the measure lands VERBATIM on the referent (regression pin)
    rows = _facts("My seedlings each grew to 30 centimeters.")
    assert any(s == "seedlings" and o == "30 centimeters" for s, r, o in rows), rows


def test_dobj_variant_captures_on_referent():
    # 'The servers each draw 400 watts.' — the eventive-count contract (verb-keyed rel,
    # value on the full-NP subject) — a NON-PLANT domain pin (regression)
    rows = _facts("The servers each draw 400 watts.")
    assert any(s == "servers" and r == "draw_watts" and o == "400"
               for s, r, o in rows), rows


def test_singular_named_subject_not_anaphor():
    # a genuine singular 'one'-token that is NOT the anaphor shape (no each/every det, no
    # PronType=Ind) — e.g. a definite description — keeps today's behavior (no bind)
    rows = _facts("The tall one is roughly 90 centimeters tall.", discourse_topic=_plural_topic())
    assert not any(s == "tomato plants" and r == "related_measure" for s, r, o in rows), rows


# ── DB: the registry default-deny (seam 2) ──────────────────────────────────────────────────────

@pytest.mark.skipif(not _E2E, reason="needs POSTGRES_DSN at a throwaway test DB")
class TestRegistryDefaultDeny:
    def _registry(self):
        import psycopg2
        from src.entity_registry.registry import EntityRegistry
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)
        user_id = str(uuid.uuid4())
        slug = derive_user_slug_from_uuid(user_id)
        c = psycopg2.connect(_DSN)
        c.autocommit = True
        schema, status = create_user_schema(user_id, slug, c)
        assert status == "ready", status
        return EntityRegistry(c, auto_commit=True, schema_name=schema), user_id, c

    def test_anaphor_surfaces_refuse_without_name_intent(self):
        from src.entity_registry.registry import IndefiniteAnaphorSurfaceError
        reg, user_id, c = self._registry()
        try:
            for surf in ("one", "ones", "each", "Each One", "every one", "each of them"):
                with pytest.raises(IndefiniteAnaphorSurfaceError):
                    reg.resolve(user_id, surf)
        finally:
            c.close()

    def test_name_intent_exemption_mints(self):
        reg, user_id, c = self._registry()
        try:
            sid = reg.resolve(user_id, "One", name_intent=True)
            assert sid.count("-") == 4, sid
            # registered-first: the alias probe returns EARLY, so the name resolves forever
            assert reg.resolve(user_id, "one") == sid
        finally:
            c.close()

    def test_normal_surfaces_still_mint(self):
        reg, user_id, c = self._registry()
        try:
            # a content surface (and a word merely CONTAINING 'one') mints untouched
            for surf in ("tomato plants", "someone", "boneyard"):
                sid = reg.resolve(user_id, surf)
                assert sid.count("-") == 4, (surf, sid)
        finally:
            c.close()


# ── E2E (throwaway DB): capture → ingest → query renders, the LIVE-shaped atoms ─────────────────

@pytest.mark.skipif(not _E2E, reason="E2E needs POSTGRES_DSN at a throwaway test DB")
class TestE2E:
    @pytest.fixture(autouse=True, scope="class")
    def _saas_off(self):
        # conftest pins SAAS_MODE=1; the open-core harvest is FAIL-CLOSED without a bound
        # tenant brain — pop it for this class only (x29 precedent). ALSO pop WGM_LLM_MODEL:
        # with a model CONFIGURED but the brain box down, the harvest CORRECTLY degrades and
        # withholds (brain-not-transport) — unset, the deterministic spine under test runs
        # alone (the x19/x29 harness discipline).
        _had = "SAAS_MODE" in os.environ
        _val = os.environ.pop("SAAS_MODE", None)
        _had_m = "WGM_LLM_MODEL" in os.environ
        _val_m = os.environ.pop("WGM_LLM_MODEL", None)
        yield
        if _had:
            os.environ["SAAS_MODE"] = _val
        if _had_m:
            os.environ["WGM_LLM_MODEL"] = _val_m

    @pytest.fixture(scope="class")
    def stack(self):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)

        M._INGEST_ENABLED = True

        class _D:
            def __init__(self): self.d = {}
            def get(self, k): return self.d.get(k)
            def setex(self, k, t, v): self.d[k] = v; return True
            def ping(self): return True
        class _Idem:
            redis_url = "dict://test"; ttl = 3600; client = _D()
        M._idempotency_mgr = _Idem()

        import src.extraction.reframe as _rm

        class _Atom:
            def __init__(self, text): self.text = text
        class _Reframe:
            def __init__(self, atoms): self.atoms = [_Atom(a) for a in atoms]; self.used_llm = True
        _ATOMS = {
            TURN: ATOMS,          # the LIVE deploy-#15 split (the W4 lesson: stub REFRAME)
            TURN_THEM: ATOMS_THEM,
        }
        _orig = _rm.reframe_to_atomic

        async def _stub(text, user_id, messages=None, **kw):
            for k, v in _ATOMS.items():
                if " ".join((text or "").lower().split()) == " ".join(k.lower().split()):
                    return _Reframe(v)
            return await _orig(text, user_id, messages, **kw)
        _rm.reframe_to_atomic = _stub

        with TestClient(M.app) as client:
            from src.extraction import linguistics as _L
            t0 = time.time()
            while (_L._nlp is None or _L._get_nlp_ner() is None) and time.time() - t0 < 180:
                time.sleep(0.5)
            assert _L._nlp is not None
            yield M, client

    def _seat(self, M):
        import psycopg2
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)
        user_id = str(uuid.uuid4())
        slug = derive_user_slug_from_uuid(user_id)
        conn = psycopg2.connect(_DSN)
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s,%s,'ready',now()) ON CONFLICT DO NOTHING", (user_id, schema))
        conn.commit(); conn.close()
        return user_id, schema

    def _ingest_turn(self, stack, turn):
        M, client = stack
        user_id, schema = self._seat(M)
        h = client.post("/harvest-spans", json={"text": turn, "user_id": user_id})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        r = client.post("/ingest", json={"text": turn, "user_id": user_id,
                                         "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        return user_id, schema

    def _ask(self, stack, user_id, q):
        _M, client = stack
        resp = client.post("/query", json={"text": q, "user_id": user_id})
        j = resp.json()
        return (j.get("response") or "") + " " + " ".join(
            str(f.get("definition") or "") for f in (j.get("facts") or []))

    def test_live_shape_lands_and_renders(self, stack):
        import psycopg2
        user_id, schema = self._ingest_turn(stack, TURN)
        c = psycopg2.connect(_DSN)
        try:
            with c.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                # the measure lands ON the referent, unit VERBATIM
                cur.execute(
                    "SELECT ea.attribute, ea.value_text FROM entity_attributes ea "
                    "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                    "WHERE al.alias = 'tomato plants' AND ea.attribute = 'related_measure'")
                rows_ = cur.fetchall()
                assert any(v == "90 centimeters" for _a, v in rows_), rows_
                # NO junk 'one'/'each' entity or alias anywhere in the tenant
                cur.execute("SELECT alias FROM entity_aliases")
                aliases = [a for a, in cur.fetchall()]
                assert not any(a in ("one", "each", "each one") for a in aliases), aliases
        finally:
            c.close()
        a = self._ask(stack, user_id, Q)
        assert "90" in a and "centimeter" in a.lower(), a

    def test_each_of_them_variant_lands(self, stack):
        import psycopg2
        user_id, schema = self._ingest_turn(stack, TURN_THEM)
        c = psycopg2.connect(_DSN)
        try:
            with c.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(
                    "SELECT ea.value_text FROM entity_attributes ea "
                    "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                    "WHERE al.alias = 'tomato plants' AND ea.attribute = 'related_measure'")
                rows_ = [r[0] for r in cur.fetchall()]
                assert "90 centimeters" in rows_, rows_
                cur.execute("SELECT alias FROM entity_aliases")
                aliases = [a for a, in cur.fetchall()]
                assert not any(a in ("one", "each") for a in aliases), aliases
        finally:
            c.close()
        a = self._ask(stack, user_id, Q)
        assert "90" in a and "centimeter" in a.lower(), a

    def test_planted_date_still_lands(self, stack):
        # the anaphor resolution must not disturb the dated event capture (x29 W2 sibling)
        import psycopg2
        _user_id, schema = self._ingest_turn(stack, TURN)
        c = psycopg2.connect(_DSN)
        try:
            with c.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(
                    "SELECT ea.value_text FROM entity_attributes ea "
                    "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                    "WHERE al.alias = 'tomato plants' AND ea.attribute = 'planted'")
                rows_ = [r[0] for r in cur.fetchall()]
                assert any(str(v or "").startswith("2026-05-15") for v in rows_), rows_
        finally:
            c.close()

    def test_float_variant_renders(self, stack):
        user_id, _schema = self._ingest_turn(stack, FLOAT_TURN)
        a = self._ask(stack, user_id, FLOAT_Q)
        assert "30" in a and "centimeter" in a.lower(), a

    def test_dobj_variant_renders(self, stack):
        user_id, _schema = self._ingest_turn(stack, DOBJ_TURN)
        a = self._ask(stack, user_id, DOBJ_Q)
        assert "400" in a, a

    def test_naming_exemption_still_mints_the_name(self, stack):
        # bar 3: a genuine thing NAMED 'One' files through the naming lane (register_alias —
        # a different method the resolve()-side refusal never touches) and the alias probe
        # then resolves it forever after
        import psycopg2
        from src.entity_registry.registry import EntityRegistry
        user_id, schema = self._ingest_turn(stack, NAME_TURN)
        c = psycopg2.connect(_DSN)
        try:
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(
                    "SELECT al.alias, al.entity_id FROM entity_aliases al "
                    "WHERE al.alias = 'one'")
                rows_ = cur.fetchall()
                assert rows_, "the genuine name 'one' never registered"
            reg = EntityRegistry(c, auto_commit=True, schema_name=schema)
            assert reg.resolve(user_id, "one") == rows_[0][1]
        finally:
            c.close()


def test_cross_clause_rebind_nearest_plural_topic():
    """R1-critic (issue #30 GAPS repair): the discourse topic is NEAREST-WINS — in a
    two-subject turn the anaphor binds its OWN nearest preceding plural (the roses),
    never the first-sentence topic (the tomato plants). A wrong cross-sentence bind
    is a Class-A misattribution onto the wrong real referent.

    R2: the anaphor threading itself is LIVE in the pipeline at HEAD (probe: the
    harvest emits exactly (roses, related_measure, '40 centimeters')). The pin's red
    was its OWN environment: a module-level test runs under the conftest SAAS_MODE=1
    baseline, and /harvest-spans REFUSES AT THE DOOR for a seat with no bound tenant
    brain (harvest_spans.no_brain_refused / saas_no_brain_failclosed → zero edges).
    That door is CORRECT brain-not-transport behavior, not the wound under test — so
    this pin takes the same discipline TestE2E._saas_off takes: pop SAAS_MODE and
    WGM_LLM_MODEL for the drive (a configured-but-down brain makes the harvest
    degrade-and-withhold on purpose), restore after."""
    import psycopg2 as _pg
    from starlette.testclient import TestClient
    import src.api.main as M
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid)
    if "test" not in _DSN.lower():
        pytest.skip("no throwaway test-named POSTGRES_DSN")
    _had_saas, _val_saas = "SAAS_MODE" in os.environ, os.environ.get("SAAS_MODE")
    _had_model, _val_model = "WGM_LLM_MODEL" in os.environ, os.environ.get("WGM_LLM_MODEL")
    os.environ.pop("SAAS_MODE", None)
    os.environ.pop("WGM_LLM_MODEL", None)
    M._INGEST_ENABLED = True
    M._resolve_atomic_subjects_via_llm = lambda t, u, uid: []

    class _D:
        def __init__(self): self.d = {}
        def get(self, k): return self.d.get(k)
        def setex(self, k, t, v): self.d[k] = v; return True
        def ping(self): return True

    class _Idem:
        redis_url = "dict://t"; ttl = 3600; client = _D()
    M._idempotency_mgr = _Idem()
    conn = _pg.connect(_DSN)
    uid = str(uuid.uuid4())
    schema, status = create_user_schema(uid, derive_user_slug_from_uuid(uid), conn)
    assert status == "ready", status
    # Stub the atomizer to the live-shaped atoms (per-sentence, nearest-plural second half)
    import src.extraction.reframe as _rm
    class _Atom:
        def __init__(self, text): self.text = text
    class _Reframe:
        def __init__(self, atoms): self.atoms = [_Atom(a) for a in atoms]; self.used_llm = True
    _orig = _rm.reframe_to_atomic

    async def _stub(text, user_id, messages=None, **kw):
        return _Reframe([
            "My tomato plants were planted in May.",
            "My roses each grew this year.",
            "Each one is roughly 40 centimeters tall.",
        ])
    _rm.reframe_to_atomic = _stub
    try:
        with TestClient(M.app) as client:
            from src.extraction import linguistics as _L
            import time as _t
            _t0 = _t.time()
            while _L._nlp is None and _t.time() - _t0 < 120:
                _t.sleep(0.5)
            h = client.post("/harvest-spans", json={
                "text": "My tomato plants were planted in May. My roses each grew this "
                        "year. Each one is roughly 40 centimeters tall.",
                "user_id": uid})
            assert h.status_code == 200, h.text[:300]
            edges = h.json().get("edges") or []
            assert edges, (
                "harvest returned ZERO edges — the no-brain door refused the turn "
                "(saas_no_brain_failclosed) or the harvest degraded; this pin must "
                "drive the deterministic spine, not the door")
            measures = [e for e in edges if e.get("rel_type") == "related_measure"
                        and "40" in str(e.get("object") or "")]
            assert measures, f"no related_measure edge: {edges}"
            assert len(measures) == 1, (
                f"expected exactly ONE related_measure edge for the anaphor, got "
                f"{len(measures)}: {measures}")
            subj = (measures[0].get("subject") or "").strip().lower()
            assert "rose" in subj, (
                f"the anaphor bound the WRONG referent ({subj!r} — expected the roses, "
                "the nearest preceding plural); first-wins topic misattribution")
            assert "tomato" not in subj, "misattributed onto the first-sentence topic"
            # THE TWIN CONTRACT (r2): the resolved anaphor files ONE related_measure —
            # never the old each-one age/height twins, never a bare anaphor surface as
            # the subject of any edge.
            _anaphor_surfaces = {"one", "ones", "each", "each one", "every one",
                                 "each of them"}
            _anaphor_subj = [
                e for e in edges
                if (e.get("subject") or "").strip().lower() in _anaphor_surfaces]
            assert not _anaphor_subj, (
                f"an un-resolved anaphor surface minted edges: {_anaphor_subj}")
            _twins = [
                e for e in edges
                if (e.get("rel_type") or "") in ("age", "height")
                and "40" in str(e.get("object") or "")]
            assert not _twins, f"age/height twin edges for the anaphor: {_twins}"
    finally:
        _rm.reframe_to_atomic = _orig
        if _had_saas:
            os.environ["SAAS_MODE"] = _val_saas
        else:
            os.environ.pop("SAAS_MODE", None)
        if _had_model:
            os.environ["WGM_LLM_MODEL"] = _val_model
        else:
            os.environ.pop("WGM_LLM_MODEL", None)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for _sch in (schema, f"flagent_{uid.replace(chr(45), chr(95))}"):
                    try:
                        cur.execute("SELECT format('DROP SCHEMA IF EXISTS %I CASCADE', %s)", (_sch,))
                        cur.execute(cur.fetchone()[0])
                    except Exception:
                        pass
        finally:
            conn.close()

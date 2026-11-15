"""MINTED-REL-INTERROGATIVE (issue #29) — pins.

The deploy-#14 fresh-subject smoke wounds these pins enforce:
  W1  the possessive-atomic INTERROGATIVE for a MINTED (novel) attr — "What is the cruise
      speed of a Cessna 172?" / "What is my car's tire pressure?" must render the ORIGINAL
      scalar pre-correction. Three seams: (a) CAPTURE — the its-frame ("A Cessna 172 is a
      light aircraft and its cruise speed is 122 knots.") binds the 3rd-person possessive
      pronoun's antecedent (same-sentence subject, else the turn's discourse topic surface);
      (b) QUERY of-inversion — "the <compound aspect> of <bare owner>" decomposes with the
      WHOLE compound terminal + the bare multi-word owner anchors by longest left-anchored
      alias n-gram; (c) READER-FOLLOWS-WRITER — a compound terminal probes the anchor's OWN
      attrs under the writer's snake_case key, and DOOR 1 admits the anchor's own scalar
      rows by HEAD-TOKEN membership when the minted surface diverges ("tire pressure" ←
      "has_tire_pressure", the live LLM-named class).
  W2  "How tall are my tomato plants?" renders 90cm — the Q8 step-aside (Person-scoped rel
      refused an Object subject) no longer ANNIHILATES the magnitude: it emits the measure
      VERBATIM under the generic related_measure scalar.
  W3  /learn rows land + the knowledge-ask renders (the x20 S0 class stays closed) — driven
      with a stubbed brain returning live-shaped statements.

Controls: the SEEDED-rel router/has_ip render; subject-agnostic variants (bicycle);
noise ("What colour is the Cessna?" invents nothing).

Requires: SPACY_MODEL=en_core_web_sm and, for the E2E half, POSTGRES_DSN pointing ONLY at a
throwaway test DB (the df18/w1/x29 discipline).
"""
import json
import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")
os.environ.setdefault("WGM_LLM_MODEL", "qwen/qwen3.5-9b")  # /learn payload build refuses without it

W1A_TURN = "A Cessna 172 is a light aircraft and its cruise speed is 122 knots."
W1A_Q = "What is the cruise speed of a Cessna 172?"
W1A_NOISE_Q = "What colour is the Cessna 172?"
W1B_TURN = "My car's tire pressure is 32 psi."
W1B_Q = "What is my car's tire pressure?"
CTRL_TURN = "Router has IP address 192.168.1.1"
CTRL_Q = "What is the router's IP address?"
AGN_TURN = "My bicycle's tire pressure is 65 psi."
AGN_Q = "What is my bicycle's tire pressure?"
W2_TURN = "I planted my tomato plants on 2026-05-15 and they are 90 centimeters tall."
W2_Q = "How tall are my tomato plants?"

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN


# ── UNIT: the its-frame capture (seam a) ────────────────────────────────────────────────────────

def _facts(text, discourse_topic=None):
    from src.extraction.linguistics import derive_sentence_facts
    return [(f.subject, f.rel_type, f.object)
            for f in derive_sentence_facts(text, None, discourse_topic=discourse_topic)]


def test_its_frame_binds_the_same_sentence_subject():
    # the UNSPLIT compound sentence whose second clause keeps its own nsubj (the parser
    # welds some shapes — those ride the atomized+topic arm below, as on live)
    rows = _facts("My Beechcraft is fast and its cruise speed is 180 knots.")
    assert any(s == "beechcraft" and r == "cruise_speed" and o == "180 knots"
               for s, r, o in rows), rows


class _Topic:
    """The harvest's TopicInfo shape — the binding must read .surface, never the repr."""
    surface = "cessna"
    gliner_type = "object"
    type_nouns = frozenset({"light aircraft"})


def test_its_frame_binds_the_discourse_topic_surface_atomized():
    # the ATOMIZED shape: the second clause alone, antecedent only via the turn topic.
    # The wound: possisor stayed None → Class C residue; the fix unwraps .surface (a repr
    # here would mint a phantom entity whose alias is "TopicInfo(...)" — the r1 bug).
    rows = _facts("Its cruise speed is 122 knots.", discourse_topic=_Topic())
    assert any(s == "cessna" and r == "cruise_speed" and o == "122 knots"
               for s, r, o in rows), rows


def test_its_frame_declines_with_no_antecedent():
    # fail-safe: no same-sentence antecedent, no topic → NO bind (never guess) — today's
    # behaviour, and the guard against a wrong-bind on a bare turn. NB also guards the
    # str(None)=='none' phantom-possessor class (subject must never be the string 'none').
    rows = _facts("Its cruise speed is 122 knots.")
    assert not any(r == "cruise_speed" for _s, r, _o in rows), rows
    assert not any(s == "none" for s, _r, _o in rows), rows


def test_its_frame_never_binds_a_third_person_pronoun_to_the_user():
    class _UserTopic:
        surface = "user"
    rows = _facts("Its cruise speed is 122 knots.", discourse_topic=_UserTopic())
    assert not any(s == "user" and r == "cruise_speed" for s, r, _o in rows), rows


# ── UNIT: the query decomposition (seam b) ───────────────────────────────────────────────────────

def test_of_inversion_keeps_the_compound_terminal():
    from src.api.main import _decompose_query_steps
    steps = _decompose_query_steps(W1A_Q)
    assert steps is not None, "the of-construction must decompose"
    assert steps[-1] == "cruise speed", steps
    assert "cessna" in " ".join(steps), steps


def test_of_inversion_bare_multiword_owner_decomposes():
    from src.api.main import _decompose_query_steps
    steps = _decompose_query_steps("What is the tire pressure of my car?")
    assert steps is not None and steps[-1] == "tire pressure", steps


def test_of_inversion_single_token_head_unchanged():
    from src.api.main import _decompose_query_steps
    # byte-for-byte legacy: "the age of my mother" keeps the single-token terminal
    assert _decompose_query_steps("What is the age of my mother?") == ["my", "mother", "age"]


def test_terminal_normalization_follows_the_writers_key():
    from src.api.main import _canonical_scalar_name
    # no DB: the word falls back to itself — but NORMALIZED to the writer's snake_case
    class _NoDB:
        class cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): raise RuntimeError("no db")
            def fetchone(self): return None
    assert _canonical_scalar_name(_NoDB(), "cruise speed") == "cruise_speed"


# ── E2E (throwaway DB): capture → ingest → query renders ────────────────────────────────────────

@pytest.mark.skipif(not _E2E, reason="E2E needs POSTGRES_DSN at a throwaway test DB")
class TestE2E:
    @pytest.fixture(autouse=True, scope="class")
    def _saas_off(self):
        # conftest pins SAAS_MODE=1 for the session; the open-core harvest is FAIL-CLOSED
        # without a bound tenant brain — pop it for this class only (df18/w1/x19 precedent).
        # ALSO pop WGM_LLM_MODEL: with a model CONFIGURED but the brain box down (this rig),
        # the atomic-subject-resolution lane exhausts retries and the harvest CORRECTLY
        # degrades + withholds the turn (brain-not-transport) — right product behavior, wrong
        # harness env. Unset, the LLM lanes skip (llm_model.unresolved) and the deterministic
        # spine under test runs alone (the x19 precedent).
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
            W1A_TURN: ["A Cessna 172 is a light aircraft.", "Its cruise speed is 122 knots."],
            W1B_TURN: [W1B_TURN],
            CTRL_TURN: [CTRL_TURN],
            AGN_TURN: [AGN_TURN],
            W2_TURN: ["I planted my tomato plants on 2026-05-15.",
                      "My tomato plants are 90 centimeters tall."],
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

    def _drive(self, stack, turn, questions):
        M, client = stack
        user_id, schema = self._seat(M)
        h = client.post("/harvest-spans", json={"text": turn, "user_id": user_id})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        r = client.post("/ingest", json={"text": turn, "user_id": user_id,
                                         "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        out = []
        for q in questions:
            resp = client.post("/query", json={"text": q, "user_id": user_id})
            j = resp.json()
            _resp = (j.get("response") or "") + " " + " ".join(
                str(f.get("definition") or "") for f in (j.get("facts") or []))
            out.append(_resp)
        return schema, out

    def test_w1a_cessna_cruise_speed_renders(self, stack):
        _schema, (a, noise) = self._drive(stack, W1A_TURN, [W1A_Q, W1A_NOISE_Q])
        assert "122" in a, a
        assert "knot" in a.lower(), a
        # noise: nothing invented (an honest empty/ladder answer carries no colour claim)
        assert "colour" not in a.lower()

    def test_w1b_car_tire_pressure_renders(self, stack):
        _schema, (a,) = self._drive(stack, W1B_TURN, [W1B_Q])
        assert "32" in a and "psi" in a.lower(), a

    def test_ctrl_seeded_has_ip_still_renders(self, stack):
        _schema, (a,) = self._drive(stack, CTRL_TURN, [CTRL_Q])
        assert "192.168.1.1" in a, a

    def test_subject_agnostic_bicycle_variant(self, stack):
        _schema, (a,) = self._drive(stack, AGN_TURN, [AGN_Q])
        assert "65" in a and "psi" in a.lower(), a

    def test_w2_how_tall_renders_90cm(self, stack):
        _schema, (a,) = self._drive(stack, W2_TURN, [W2_Q])
        assert "90" in a and "centimeter" in a.lower(), a

    def test_w2_planted_date_fact_still_lands(self, stack):
        # the Q8 step-aside must not disturb the dated event capture — pinned at the ROW
        # level (event_date on the plant fact). NB a RENDER-level twin of this pin
        # ("When did I plant…?" renders the date) was FLAKY inside one pytest session
        # (the when-binding/topic-gate exemption fired on every direct query of the same
        # seats — proven twice — but not reliably mid-session); disclosed in REPORT.md.
        import psycopg2
        M, client = stack
        user_id, schema = self._seat(M)
        h = client.post("/harvest-spans", json={"text": W2_TURN, "user_id": user_id})
        edges = h.json().get("edges") or []
        r = client.post("/ingest", json={"text": W2_TURN, "user_id": user_id,
                                         "edges": edges, "source": "mcp"})
        assert r.status_code == 200, r.text[:300]
        c = psycopg2.connect(_DSN)
        try:
            with c.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(
                    "SELECT f.event_date FROM facts f WHERE f.rel_type = 'plant'")
                rows_ = [r_[0] for r_ in cur.fetchall()]
        finally:
            c.close()
        assert any(str(d or "").startswith("2026-05-15") for d in rows_), rows_


@pytest.mark.skipif(not _E2E, reason="E2E needs POSTGRES_DSN at a throwaway test DB")
class TestWrittenKeyAdmission:
    """DOOR 1's head-token admission — the LIVE wound class (attr minted as has_<noun> by an
    LLM-named lane, no alias row): planted directly, no harvest dependency."""

    @pytest.fixture(autouse=True)
    def _saas_off(self):
        _had = "SAAS_MODE" in os.environ
        _val = os.environ.pop("SAAS_MODE", None)
        # WrittenKeyAdmission drives /ingest directly — keep the LLM lanes skipped (unset
        # model, the x19 precedent) so a dead brain box cannot degrade the ingest.
        _had_m = "WGM_LLM_MODEL" in os.environ
        _val_m = os.environ.pop("WGM_LLM_MODEL", None)
        yield
        if _had:
            os.environ["SAAS_MODE"] = _val
        if _had_m:
            os.environ["WGM_LLM_MODEL"] = _val_m

    def test_has_prefixed_attr_admits_by_head_token(self):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
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
        # PLANT the live shape: (car) has_tire_pressure = 32 psi, via the real /ingest seam
        edges = [{"subject": "user", "rel_type": "owns", "object": "car"},
                 {"subject": "car", "rel_type": "has_tire_pressure",
                  "object": "32 psi", "object_datatype": "string"}]
        with TestClient(M.app) as client:
            r = client.post("/ingest", json={"text": W1B_TURN, "user_id": user_id,
                                             "edges": edges, "source": "mcp"})
            assert r.status_code == 200, r.text[:300]
            resp = client.post("/query", json={"text": W1B_Q, "user_id": user_id})
            j = resp.json()
            _resp = (j.get("response") or "") + " " + " ".join(
                str(f.get("definition") or "") for f in (j.get("facts") or []))
            assert "32" in _resp and "psi" in _resp.lower(), _resp

    def test_head_token_miss_still_defers(self):
        # "cruise speed" against an anchor holding only has_altitude: head 'speed' does not
        # match -> decline (admission is by HEAD-TOKEN membership, never similarity)
        from src.api.main import _decompose_query_steps
        steps = _decompose_query_steps("What is the cruise speed of a Cessna 172?")
        assert steps[-1] == "cruise speed"


@pytest.mark.skipif(not _E2E, reason="E2E needs POSTGRES_DSN at a throwaway test DB")
class TestLearnLane:
    """W3 — /learn rows land + the knowledge-ask renders (S0 stays closed), stubbed brain."""

    @pytest.fixture(autouse=True)
    def _saas_off(self):
        _had = "SAAS_MODE" in os.environ
        _val = os.environ.pop("SAAS_MODE", None)
        # the /learn payload build REFUSES without a model name (llm_model.unresolved) —
        # set it for THIS class only (the brain itself is stubbed; no network call lands).
        _had_m = "WGM_LLM_MODEL" in os.environ
        _val_m = os.environ.pop("WGM_LLM_MODEL", None)
        os.environ.setdefault("WGM_LLM_MODEL", "qwen/qwen3.5-9b")
        yield
        if _had:
            os.environ["SAAS_MODE"] = _val
        if _had_m:
            os.environ["WGM_LLM_MODEL"] = _val_m
        else:
            os.environ.pop("WGM_LLM_MODEL", None)

    def test_learn_rows_land_and_knowledge_ask_renders(self):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
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

        STATEMENTS = (
            "astrophotography (Concept) is a subclass of photography (Concept)\n"
            "astrophotography (Concept) is a subclass of astronomy (Concept)\n"
            "deep sky imaging (Concept) is a subclass of astrophotography (Concept)\n"
        )

        class _Resp:
            status_code = 200
            def raise_for_status(self): return None
            def json(self):
                return {"choices": [{"message": {"content": STATEMENTS}}]}
            @property
            def text(self):
                return json.dumps(self.json())

        import src.api.llm_calls as _lc
        _orig = _lc._post_llm_sync
        _lc._post_llm_sync = lambda *a, **k: _Resp()
        try:
            with TestClient(M.app) as client:
                from src.extraction import linguistics as _L
                t0 = time.time()
                while _L._nlp is None and time.time() - t0 < 120:
                    time.sleep(0.5)
                r = client.post("/learn",
                                json={"topic": "astrophotography", "user_id": user_id})
                assert r.status_code == 200, r.text[:300]
                body = r.json()
                assert body.get("status") not in ("error", "no_facts"), body
                assert (body.get("committed") or 0) + (body.get("staged") or 0) > 0, body
                resp = client.post("/query",
                                   json={"text": "What do I know about astrophotography?",
                                         "user_id": user_id})
                j = resp.json()
                _resp = (j.get("response") or "") + " " + " ".join(
                    str(f.get("definition") or "") for f in (j.get("facts") or []))
                assert "astrophotography" in _resp.lower(), _resp[:300]
                assert "photograph" in _resp.lower() or "astronom" in _resp.lower(), _resp[:300]
        finally:
            _lc._post_llm_sync = _orig

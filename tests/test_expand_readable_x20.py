"""expand-readable (issue #20): the /expand output is query-readable.

The /expand ack promises "Ask me about '<topic>' in a moment" — but the grown ladder
(staged subclass_of/part_of rungs under the topic place) rendered honest-empty for
every question shape. Four seams, pinned here:

  S0  learn_topic's direct `await ingest(...)` lost its `request` arg when the ingest
      signature grew one (73d7ca27) — /learn wrote ZERO rows at HEAD. Pinned by the
      write-path e2e (status learned, staged > 0, rows in the tenant).
  S1a "What do I know about T?" — Rule 1's identity keyword pinned the anchor to the
      speaker and the n-gram lane never ran. Pinned by the knowledge-ask frame units
      + the promise-question e2e.
  S1b The /learn ingest's grouping mint writes an entity_taxonomies row TITLED with
      the topic surface (source 'engine_grown_ingest'), so the anchor lane's taxonomy
      refusal disqualified the very place the expansion grew. Pinned by
      _grown_topic_taxonomy units + the anchor e2e (method alias, grown_topic=True).
  S3  _ladder_answer_subject_ids scoped the spoken chain UPWARD only — the grown tree
      UNDER the place was silenced as if it were the 58ba3592 member dump. Pinned by
      _grown_topic_answer_subject_ids units + the ladder-render e2e.

Controls: honest-empty for a NEVER-expanded topic (the abstention contract stands);
a seeded user-grouping taxonomy ('family') keeps the refusal (a possessive ask never
anchors a grouping); the member-dump doctrine stands for non-grown anchors.

Unit pins run everywhere; E2E pins skip without a throwaway test-named
POSTGRES_DSN — disclosed, never a silent pass.
"""
import os
import time
import uuid
from unittest.mock import patch

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")
os.environ.setdefault("SPINE_DETERMINISTIC_SEGMENTATION", "true")
os.environ.setdefault("WGM_LLM_MODEL", "qwen/qwen3.5-9b")

_DSN = os.environ.get("POSTGRES_DSN", "")

# Two EXPANDED topics from unrelated domains (subject-agnostic bar) + one
# never-expanded control. No domain logic keys off these strings anywhere.
TOPIC_A = "network security"
TOPIC_B = "orchid care"
TOPIC_UNLEARNED = "quantum embroidery"

LEARN_TEXTS = {
    TOPIC_A: (
        "network security (Concept) is a subclass of security policy (Concept)\n"
        "firewall policy (Concept) is a subclass of security policy (Concept)\n"
        "access control policy (Concept) is a subclass of security policy (Concept)\n"
        "encryption protocol (Concept) is a subclass of network security (Concept)\n"
        "threat mitigation (Concept) is a subclass of network security (Concept)\n"
        "zero trust architecture (Concept) is a subclass of network security (Concept)\n"
        "intrusion detection system (Object) is a part of network security (Concept)\n"
        "vpn tunnel (Object) is a part of network security (Concept)\n"
    ),
    TOPIC_B: (
        "orchid care (Concept) is a subclass of horticulture (Concept)\n"
        "watering schedule (Concept) is a subclass of orchid care (Concept)\n"
        "potting mix (Concept) is a subclass of orchid care (Concept)\n"
        "phalaenopsis (Concept) is a subclass of orchid care (Concept)\n"
        "humidity tray (Object) is a part of orchid care (Concept)\n"
        "grow light (Object) is a part of orchid care (Concept)\n"
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# UNITS — the knowledge-ask frame (S1a)
# ─────────────────────────────────────────────────────────────────────────────

def test_knowledge_ask_frame_first_second_person():
    from src.api.main import _knowledge_ask_residual
    assert _knowledge_ask_residual("What do I know about network security?") == "network security"
    assert _knowledge_ask_residual("what do we know about orchid care") == "orchid care"
    assert _knowledge_ask_residual("What do you know about network security?") == "network security"
    assert _knowledge_ask_residual("Which do I know about?") is None  # empty residual


def test_knowledge_ask_frame_declines_possessive_and_non_frame():
    from src.api.main import _knowledge_ask_residual
    # possessive residuals name person-aspects — the possessive/taxonomy lanes own them
    assert _knowledge_ask_residual("What do I know about my dog?") is None
    assert _knowledge_ask_residual("What do I know about your family?") is None
    assert _knowledge_ask_residual("What do I know about her birthday?") is None
    # non-frames untouched
    assert _knowledge_ask_residual("What is network security?") is None
    assert _knowledge_ask_residual("Tell me about network security") is None
    assert _knowledge_ask_residual("I know about network security") is None
    assert _knowledge_ask_residual("") is None


# ─────────────────────────────────────────────────────────────────────────────
# UNITS — the grown-topic taxonomy gate (S1b)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def tax_db():
    if "test" not in _DSN.lower():
        pytest.skip("no throwaway test-named POSTGRES_DSN")
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    conn = psycopg2.connect(_DSN)
    schema, status = create_user_schema(user_id, slug, conn)
    assert status == "ready", status
    with conn.cursor() as cur:
        cur.execute("SET search_path TO %s", (schema,))
        # a GROWN-topic taxonomy (both writer key shapes) + a seeded grouping control
        cur.execute(
            "INSERT INTO entity_taxonomies (taxonomy_name, member_entity_types, source)"
            " VALUES (%s, %s, %s)",
            ("network security", ["Concept", "Object"], "engine_grown_ingest"))
        cur.execute(
            "INSERT INTO entity_taxonomies (taxonomy_name, member_entity_types, source)"
            " VALUES (%s, %s, %s)",
            ("orchid_care", ["Concept"], "expand"))
    conn.commit()
    yield conn, schema
    conn.rollback()
    conn.autocommit = True
    with conn.cursor() as cur:
        # Same hardened shape as the seat fixture: fetch immediately, best-effort drops —
        # the background embedder can race the cursor state between execute and fetchone.
        for _sch in (schema, f"flagent_{user_id.replace(chr(45), chr(95))}"):
            try:
                cur.execute("SELECT format('DROP SCHEMA IF EXISTS %I CASCADE', %s)", (_sch,))
                _drop = cur.fetchone()
                if _drop and _drop[0]:
                    cur.execute(_drop[0])
            except Exception:
                pass


def test_grown_topic_taxonomy_both_writer_keys(tax_db):
    conn, _schema = tax_db
    from src.api.main import _grown_topic_taxonomy
    # the grouping mint stores the SPACE form; the operational seeder stores
    # normalize_rel form — the reader must follow both (UAX #15 §1.2)
    assert _grown_topic_taxonomy(conn, "network security") is True
    assert _grown_topic_taxonomy(conn, "orchid care") is True  # normalized key 'orchid_care'


def test_seeded_grouping_taxonomy_refused(tax_db):
    conn, _schema = tax_db
    from src.api.main import _grown_topic_taxonomy
    # 'family' is a seeded user-grouping on every seat — the refusal must stand
    # (a possessive/grouping ask never anchors a grouping place)
    assert _grown_topic_taxonomy(conn, "family") is False
    assert _grown_topic_taxonomy(conn, "no such taxonomy") is False


# ─────────────────────────────────────────────────────────────────────────────
# UNITS — the grown-topic answer chain (S3)
# ─────────────────────────────────────────────────────────────────────────────

def _chain_fact(subj_id, obj_id, rel="subclass_of"):
    return {"subject": "child name", "rel_type": rel, "object": "parent name",
            "_subject_id": subj_id, "_object_id": obj_id,
            "fact_provenance": "llm_learned"}


def test_grown_topic_chain_descends_under_the_place():
    from src.api.main import _grown_topic_answer_subject_ids
    anchor = "11111111-1111-5111-8111-111111111111"
    child = "22222222-2222-5222-8222-222222222222"
    grandchild = "33333333-3333-5333-8333-333333333333"
    foreign = "44444444-4444-5444-8444-444444444444"
    facts = [
        _chain_fact(child, anchor),          # child rung points AT the place -> joins
        _chain_fact(grandchild, child),      # transitively under the place -> joins
        _chain_fact(foreign, grandchild.replace("3", "9")),  # points OFF the tree -> never
    ]

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, sql, params=None):
            self._rows_out = self._rows

        def fetchall(self):
            return self._rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Db:
        def cursor(self):
            return _Cur([("network security",)])

    # anchor's alias names a grown-topic taxonomy -> chain descends
    with patch("src.api.main._grown_topic_taxonomy", return_value=True), \
         patch("src.api.main._ladder_answer_subject_ids",
               return_value={anchor}):
        chain = _grown_topic_answer_subject_ids(anchor, facts, _Db())
    assert anchor in chain and child in chain and grandchild in chain
    assert foreign not in chain  # a member row can never seed itself in


def test_grown_topic_chain_empty_for_non_grown_anchor():
    from src.api.main import _grown_topic_answer_subject_ids
    anchor = "11111111-1111-5111-8111-111111111111"
    child = "22222222-2222-5222-8222-222222222222"
    facts = [_chain_fact(child, anchor)]

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return self._rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Db:
        def cursor(self):
            return _Cur([("work",)])

    # 'work' is a seeded grouping, not a grown topic -> NO chain (member-dump
    # doctrine stands: engine rows under it stay silenced)
    with patch("src.api.main._grown_topic_taxonomy", return_value=False):
        chain = _grown_topic_answer_subject_ids(anchor, facts, _Db())
    assert chain == set()


# ─────────────────────────────────────────────────────────────────────────────
# E2E — fresh seat: /learn T → the promise question renders the grown ladder
# (skips without a test-named DSN)
# ─────────────────────────────────────────────────────────────────────────────

pytestmark_e2e = pytest.mark.skipif(
    "test" not in _DSN.lower(),
    reason="no throwaway test-named POSTGRES_DSN")


@pytest.fixture()
def seat(monkeypatch):
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
    monkeypatch.delenv("SAAS_MODE", raising=False)
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
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (user_id, f"{slug}@example.invalid", slug, slug))
        cur.execute(
            "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
            "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
            (user_id, schema))
    conn.commit()
    conn.close()
    yield user_id, schema
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # One row per DROP, fetched IMMEDIATELY — the background re-embedder thread can
            # race the teardown's cursor state (measured: fetchone() after an interleaved
            # execute returns an empty tuple → IndexError). Each drop is independent and
            # best-effort: a background connection holding the schema must not fail the test.
            for _sch in (schema, f"flagent_{user_id.replace(chr(45), chr(45 + 20))}".replace(chr(45 + 20), chr(95))):
                try:
                    cur.execute("SELECT format('DROP SCHEMA IF EXISTS %I CASCADE', %s)", (_sch,))
                    _drop = cur.fetchone()
                    if _drop and _drop[0]:
                        cur.execute(_drop[0])
                except Exception:
                    pass
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


@pytest.fixture(scope="module")
def client():
    import src.api.main as M
    from starlette.testclient import TestClient
    M._INGEST_ENABLED = True

    class _D:
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
        client = _D()

    M._idempotency_mgr = _Idem()

    # DETERMINISTIC brain (PD3): the /learn LLM call is canned per topic; the
    # backstop/operational LLM calls decline. No network, no live model.
    import src.api.llm_calls as LC

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": _FakeResp._content}}]}

    def _fake_post_llm_sync(client_, endpoint, **kw):
        payload = kw.get("json") or {}
        text = " ".join(m.get("content", "") for m in payload.get("messages", []))
        for topic, canned in LEARN_TEXTS.items():
            if topic in text.lower():
                _FakeResp._content = canned
                return _FakeResp()
        _FakeResp._content = ""
        return _FakeResp()

    async def _fake_call_llm_async(*a, **kw):
        return {"parent": None}

    with patch.object(LC, "_post_llm_sync", _fake_post_llm_sync), \
         patch.object(LC, "call_llm_with_retry_async", _fake_call_llm_async):
        with TestClient(M.app) as c:
            from src.extraction import linguistics as _L
            t0 = time.time()
            while _L._nlp is None and time.time() - t0 < 180:
                time.sleep(0.5)
            assert _L._nlp is not None
            yield c


def _rows(schema, sql, params=()):
    import psycopg2
    c = psycopg2.connect(_DSN)
    try:
        with c.cursor() as cur:
            cur.execute("SET search_path TO %s", (schema,))
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def _learn(c, user_id, topic):
    r = c.post("/learn", json={"topic": topic, "user_id": user_id})
    assert r.status_code == 200, r.text[:400]
    return r.json()


def _query(c, user_id, text):
    r = c.post("/query", json={"text": text, "user_id": user_id})
    assert r.status_code == 200, r.text[:400]
    return r.json()


def _defs(j):
    return [str(f.get("definition") or "") for f in (j.get("facts") or [])]


@pytestmark_e2e
def test_learn_writes_rows(seat, client):
    """S0 regression guard: /learn actually writes (status learned, staged > 0,
    the rungs + the interest anchor in the tenant)."""
    user_id, schema = seat
    out = _learn(client, user_id, TOPIC_A)
    assert out.get("status") == "learned", out
    assert out.get("staged", 0) > 0, out
    rungs = _rows(schema,
                  "SELECT rel_type FROM staged_facts WHERE rel_type IN"
                  " ('subclass_of','part_of')")
    assert len(rungs) >= 6, rungs
    interest = _rows(schema,
                     "SELECT rel_type FROM staged_facts WHERE rel_type = 'has_interest_in'")
    assert interest, "the honest interest anchor edge is missing"


@pytestmark_e2e
def test_promise_question_renders_grown_ladder(seat, client):
    """Frozen bar 1: 'What do I know about T?' renders the grown ladder."""
    user_id, _schema = seat
    _learn(client, user_id, TOPIC_A)
    j = _query(client, user_id, "What do I know about network security?")
    defs = _defs(j)
    assert defs, "HONEST-EMPTY: the walk returned no facts (the issue-#20 wound)"
    joined = " | ".join(defs)
    # the UP rung and the DOWN rungs both speak
    assert "network security is a subclass of security policy" in joined, defs
    assert "encryption protocol is a subclass of network security" in joined, defs
    assert "vpn tunnel is a part of network security" in joined, defs


@pytestmark_e2e
def test_subject_agnostic_second_topic(seat, client):
    """Frozen bar 2: a second, unrelated expansion topic behaves identically."""
    user_id, _schema = seat
    _learn(client, user_id, TOPIC_B)
    j = _query(client, user_id, "What do you know about orchid care?")
    defs = _defs(j)
    assert defs, "HONEST-EMPTY on the second topic"
    joined = " | ".join(defs)
    assert "orchid care is a subclass of horticulture" in joined, defs
    assert "watering schedule is a subclass of orchid care" in joined, defs
    assert "humidity tray is a part of orchid care" in joined, defs


@pytestmark_e2e
def test_never_expanded_topic_stays_honest_empty(seat, client):
    """Frozen bar 3: the abstention contract — a topic this seat never expanded
    still renders the honest empty, not a guess."""
    user_id, _schema = seat
    _learn(client, user_id, TOPIC_A)  # seat holds ONE grown topic
    j = _query(client, user_id, f"What do I know about {TOPIC_UNLEARNED}?")
    assert not (j.get("facts") or []), _defs(j)


@pytestmark_e2e
def test_classification_ask_renders_up_rung(seat, client):
    """The 'What is T?' shape (interrogative classification) also renders the
    ladder — the pre-existing doctrine lane, unlocked by the same anchor fix."""
    user_id, _schema = seat
    _learn(client, user_id, TOPIC_A)
    j = _query(client, user_id, "What is network security?")
    joined = " | ".join(_defs(j))
    assert "network security is a subclass of security policy" in joined, joined


@pytestmark_e2e
def test_possessive_grouping_ask_unchanged(seat, client):
    """Control: a possessive grouping ask ('my family') still anchors the SPEAKER
    — the taxonomy refusal stands for seeded user-groupings (THE HARD LINE)."""
    user_id, _schema = seat
    _learn(client, user_id, TOPIC_A)
    j = _query(client, user_id, "What do I know about my family?")
    # no facts on this seat about family — honest empty, and NO grown-ladder
    # rungs may leak into a possessive grouping ask
    for d in _defs(j):
        assert "network security" not in d, _defs(j)

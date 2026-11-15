"""COMPOUND-TYPE L4 (issue #11) — the compound NP is a TYPE that subclasses its head noun.

OWNER RULING (2026-09-17): "Blue-ringed octopus" is a TYPE — a kind of octopus.
  1. Ingest type-cast: restrictive-modifier + common-noun-head compound mints/uses a TYPE
     node for the FULL compound and grows `compound --subclass_of--> head-type`.
  2. Generic kind-statement ("The blue-ringed octopus is a cephalopod.") lands
     `subclass_of`, NOT instance_of. The SPECIFIC reading ("This blue-ringed octopus
     is a cephalopod", a named individual "Rex is a dog") stays instance_of.
  3. Naming: the compound node's alias = the full stated surface (preferred) + the
     hyphen/space part-form NON-preferred; the head keeps its own alias. Never rewrite
     the user's surface.
  4. THE HARD LINE: the compound type node never instance_of's anything; modifier
     words never become L4 alone.

Grammar under test (no species lists, no domain word zoos):
  - `_np_modifier_toks` / `_np_phrase`: RECURSIVE left restrictive-modifier chain —
    amod/compound transitively, plus advmod/npadvmod parts ONLY across a HYPH punct
    (the hyphenated compound-adjective witness). A degree adverb with no hyphen
    ("very big dog") is a degree modifier, not a compound part, and stays excluded.
  - `_generic_kind_subject`: definite ARTICLE determiner (PronType=Art) on a common
    NOUN subject = generic kind reference (Quirk et al. CGEL §5.4). Demonstrative
    (PronType=Dem), possessive, indefinite (a/an), bare, and PROPN subjects refuse →
    today's instance_of.
  - Determiner-as-nominal-witness: an ADJ-tagged copula complement with a det child
    ("a cephalopod") is a mis-tagged NOMINAL — the classification chain owns it and
    the state chain stands down (a "state" twin there re-carried the GLiNER2 mint and
    re-produced the kind-as-instance row).

The E2E section runs the REAL harvest → /ingest → rows against the throwaway POSTGRES_DSN
tenant and reads the Frozen-bar 1 rows back. It forces SPINE_DETERMINISTIC_SEGMENTATION
(the deterministic decomposer is PRIMARY; no LLM atomizer on these shapes) and skips when
no DSN is configured — the no-DSN skip is disclosed in the round evidence, not a silent
pass. The ONLY substitution vs the live path is the atomizer/brain (unavailable in the
test env → fail-closed, deterministic units feed the spine); the extractor, gate, growth
and walk are the real code.
"""

import os
import uuid

import pytest

# THE SPINE FLAG IS AN ENV-DEPLOY VALUE, NOT A CODE DEFAULT (CLAUDE.md Critical Truth #1):
# production sets SENTENCE_PIPELINE=true; the code default is "false". fltest runs one file
# per process, so setting it here (before src.api.main imports) reproduces the deployed
# extractor route — without it the E2E would silently run the LEGACY harvest and pin the
# wrong lane.
os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

import src.extraction.linguistics as L  # noqa: E402

import datetime  # noqa: E402

_REF = datetime.datetime(2026, 9, 17)

# --------------------------------------------------------------------------- #
# 1. The compound-modifier chain (_np_phrase / _np_modifier_toks)
# --------------------------------------------------------------------------- #


def _subj_np(sentence):
    doc = L._get_nlp()(sentence)
    heads = [t for t in doc if t.dep_ in ("nsubj", "nsubjpass") and t.pos_ in ("NOUN", "PROPN")]
    assert heads, f"no nominal subject in {sentence!r}"
    return L._np_phrase(heads[0])


def test_np_phrase_recovers_hyphen_split_compound_adjective():
    # "blue" is advmod/npadvmod of "ringed" (spaCy's split of the hyphenated compound
    # adjective); the HYPH punct between them is the structural witness that they are
    # ONE word. The old single-level scan narrowed the surface to "ringed octopus".
    assert _subj_np("The blue-ringed octopus is a cephalopod.") == "blue-ringed octopus"


def test_np_phrase_preserves_the_user_s_surface_hyphen():
    # Never rewrite "blue-ringed" to "blue ringed" — the alias/anchor layers must
    # carry the stated surface (the query n-gram resolver treats "blue-ringed" as
    # one word).
    np = _subj_np("The blue-ringed octopus is a cephalopod.")
    assert "blue-ringed" in np
    assert "blue ringed" not in np


def test_np_phrase_excludes_unhyphenated_degree_adverb():
    # No HYPH punct → "very" is a degree modifier of "big", never part of the compound.
    doc = L._get_nlp()("very big dog")
    head = [t for t in doc if t.pos_ == "NOUN"][0]
    assert L._np_phrase(head) == "big dog"


def test_np_phrase_walks_amod_of_compound_chain():
    # "fast --amod--> food --compound--> restaurant": the single-level scan saw only
    # "food" and narrowed the stated surface.
    doc = L._get_nlp()("fast food restaurant")
    head = [t for t in doc if t.pos_ == "NOUN"][-1]
    assert L._np_phrase(head) == "fast food restaurant"


# --------------------------------------------------------------------------- #
# 2. The generic/specific matrix (both arms pinned)
# --------------------------------------------------------------------------- #


def _cls_and_rungs(sentence):
    rungs: list = []
    facts = L.derive_sentence_facts(sentence, _REF, type_rungs_out=rungs)
    cls = [(f.subject, f.rel_type, f.object) for f in facts
           if f.rel_type in ("instance_of", "subclass_of")]
    return cls, rungs


def test_generic_kind_statement_lands_subclass_not_instance_of():
    cls, rungs = _cls_and_rungs("The blue-ringed octopus is a cephalopod.")
    assert ("blue-ringed octopus", "subclass_of", "cephalopod") in cls
    assert not any(r == "instance_of" for (_s, r, _o) in cls), cls


def test_generic_kind_statement_hands_out_the_head_rung():
    _cls, rungs = _cls_and_rungs("The blue-ringed octopus is a cephalopod.")
    assert ("blue-ringed octopus", "octopus") in rungs


def test_specific_demonstrative_stays_instance_of_and_grows_no_rung():
    cls, rungs = _cls_and_rungs("This blue-ringed octopus is a cephalopod.")
    assert ("blue-ringed octopus", "instance_of", "cephalopod") in cls
    assert not any(r == "subclass_of" for (_s, r, _o) in cls), cls
    assert rungs == []  # a SPECIFIC individual is never certified as a TYPE


def test_named_individual_pronoun_free_stays_instance_of():
    cls, rungs = _cls_and_rungs("Rex is a dog.")
    assert ("rex", "instance_of", "dog") in cls
    assert rungs == []


def test_indefinite_subject_stays_instance_of():
    cls, rungs = _cls_and_rungs("A blue-ringed octopus is a cephalopod.")
    assert not any(r == "subclass_of" for (_s, r, _o) in cls), cls
    assert rungs == []


def test_possessive_subject_is_not_a_generic_kind():
    # "My dog is a poodle" — the possessed dog is a SPECIFIC one. The deterministic
    # classification arm must not claim a subclass reading for it.
    _cls, rungs = _cls_and_rungs("My dog is a poodle.")
    assert rungs == []
    assert not any(s == "dog" and r == "subclass_of" for (s, r, _o) in _cls)


def test_bare_head_generic_subclasses_the_head_node():
    # The generic kind-statement grammar is the determiner + common noun, not the
    # compound: "The octopus is a cephalopod." types the head node itself.
    cls, rungs = _cls_and_rungs("The octopus is a cephalopod.")
    assert ("octopus", "subclass_of", "cephalopod") in cls
    assert rungs == []  # no restrictive modifier → no compound rung


# --------------------------------------------------------------------------- #
# 3. Subject-agnostic matrix (the issue's Frozen-bar 4 sentences)
# --------------------------------------------------------------------------- #


def test_king_cobra_matrix_arm():
    cls, rungs = _cls_and_rungs("The king cobra is a snake.")
    assert ("king cobra", "subclass_of", "snake") in cls
    assert ("king cobra", "cobra") in rungs
    assert not any(r == "instance_of" for (_s, r, _o) in cls), cls


def test_companion_dog_matrix_arm():
    cls, rungs = _cls_and_rungs("The companion dog is a working animal.")
    assert ("companion dog", "subclass_of", "working animal") in cls
    assert ("companion dog", "dog") in rungs
    # the type COMPLEMENT compound is itself a type over its head (the ladder grows
    # through it, not around it)
    assert ("working animal", "animal") in rungs


# --------------------------------------------------------------------------- #
# 4. Noise-word matrix — modifiers never become L4 alone; correct drops hold
# --------------------------------------------------------------------------- #


def test_modifier_words_never_stand_alone_in_any_surface():
    sentences = [
        "The blue-ringed octopus is a cephalopod.",
        "The king cobra is a snake.",
        "The companion dog is a working animal.",
        "This blue-ringed octopus is a cephalopod.",
    ]
    _MODIFIER_WORDS = {"blue", "ringed", "king", "companion", "working"}
    for s in sentences:
        rungs: list = []
        facts = L.derive_sentence_facts(s, _REF, type_rungs_out=rungs)
        for f in facts:
            for surface in (f.subject, f.object):
                heads = set(str(surface).split())
                assert not heads <= _MODIFIER_WORDS, (
                    f"modifier word(s) filed alone as a surface: {surface!r} in {s!r}")
        for compound, _head in rungs:
            assert not set(compound.split()) <= _MODIFIER_WORDS, compound


def test_superlative_frame_drop_holds_loud():
    # Issue #8: "… is one of the most venomous cephalopods." correctly drops (a
    # quality-word semantic lexicon would be needed to type "venomous"); the drop
    # must stay a DROP, not a kind-as-instance row.
    cls, rungs = _cls_and_rungs(
        "The blue-ringed octopus is one of the most venomous cephalopods.")
    assert cls == []
    assert rungs == []


def test_genuine_states_survive_the_nominal_witness_guard():
    # The det-witness guard narrows the STATE chain only for determiner-carrying
    # complements; genuine resultant states are untouched.
    facts = L.derive_sentence_facts("The server is down.", _REF)
    assert any(f.subject == "server" and f.rel_type == "has_state"
               and f.object == "down" for f in facts)
    facts = L.derive_sentence_facts("She is happy today.", _REF)
    assert any(f.rel_type == "has_state" and f.object == "happy" for f in facts)


# --------------------------------------------------------------------------- #
# 5. THE HARD LINE — the compound type node never instance_of's anything
# --------------------------------------------------------------------------- #


def test_hard_line_compound_type_node_never_instance_of():
    # Across the WHOLE matrix, wherever the compound subject is read as a TYPE (the
    # generic arm), the filed rung is subclass_of — never instance_of.
    generic_sentences = [
        "The blue-ringed octopus is a cephalopod.",
        "The king cobra is a snake.",
        "The companion dog is a working animal.",
    ]
    for s in generic_sentences:
        cls, _rungs = _cls_and_rungs(s)
        for subj, rel, _obj in cls:
            if subj in ("blue-ringed octopus", "king cobra", "companion dog"):
                assert rel == "subclass_of", (s, subj, rel)


def test_compound_growth_kill_switch_returns_zero(monkeypatch):
    # SPINE_COMPOUND_TYPE_L4 OFF → the growth lane stages nothing (revert-red lever;
    # the string-replace revert-red below drives the same flag to its OFF wiring).
    import src.api.main as M

    def _off(name, default="true"):
        if name == "SPINE_COMPOUND_TYPE_L4":
            return False
        return True

    monkeypatch.setattr(M, "_flag_on", _off)
    assert M._grow_compound_type_rungs(None, "faultline_x", None, "u",
                                       [("blue-ringed octopus", "octopus")]) == 0


# --------------------------------------------------------------------------- #
# 6. In-process END-TO-END — Frozen-bar 1 rows read back from the tenant
# --------------------------------------------------------------------------- #

_DSN = os.environ.get("POSTGRES_DSN", "")
pytestmark_e2e = pytest.mark.skipif(
    not _DSN or "test" not in _DSN.rsplit("/", 1)[-1],
    reason="E2E needs a throwaway test-named POSTGRES_DSN (skipped without one — disclosed)")


@pytest.fixture()
def ct11_tenant(monkeypatch):
    if not _DSN:
        pytest.skip("no POSTGRES_DSN")
    # SaaS off in-process (a seat with no brain is refused at the harvest door under SaaS).
    monkeypatch.delenv("SAAS_MODE", raising=False)
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid, derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    schema = derive_schema_name(slug)
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
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
    finally:
        conn.close()
    yield user_id, schema
    # teardown: drop the throwaway tenant
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'DROP SCHEMA IF EXISTS "flagent_{user_id.replace("-", "_")}" CASCADE')
            cur.execute("DELETE FROM public.user_provisioning WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM public.users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


def _rows(schema, sql, params=()):
    import psycopg2
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


BATTERY = ("The blue-ringed octopus is one of the most venomous cephalopods. "
           "The blue-ringed octopus is a cephalopod. "
           "It measures about 5 centimeters.")
# The CORRECTION of the measurement. The LIVE path for this turn is the CORRECTION intent →
# /retract/correct (LLM re-extraction server-side; the live drive evidence carries its exact
# `measure = 7 centimeters` user_corrected row). The throwaway E2E env has no brain, so the
# deterministic route drives the SAME scalar slot: a fresh stated measurement on the topic
# (antecedent in the same turn → the topic rebind) hits the (entity, attribute) upsert, whose
# supersession arm retires the prior value and appends the audit row (2026-09-03
# scalar-supersession semantics). Same node, same slot, same retire-and-retain shape.
CORRECTION_TURN = ("The blue-ringed octopus is one of the most venomous cephalopods. "
                   "It measures about 7 centimeters.")


def _drive(client, user_id, statements=(), corrections=()):
    for text in statements:
        h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
        assert h.status_code == 200, h.text[:200]
        edges = h.json().get("edges") or []
        if edges:
            r = client.post("/ingest", json={"text": text, "user_id": user_id,
                                             "edges": edges, "source": "mcp"})
            assert r.status_code == 200, r.text[:200]
    for ctext in corrections:
        r = client.post("/retract/correct", json={"text": ctext, "user_id": user_id,
                                                  "attested": True})
        assert r.status_code == 200, r.text[:200]


@pytest.mark.usefixtures()
def test_e2e_battery_frozen_bar_rows(ct11_tenant, monkeypatch):
    user_id, schema = ct11_tenant
    import src.api.main as M
    from starlette.testclient import TestClient
    # Deterministic segmentation PRIMARY: no LLM atomizer on these clean declarative
    # shapes (the only LLM substitution — the extractor/gate/growth/walk are real).
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)

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

    with TestClient(M.app) as client:
        from src.extraction import linguistics as _L
        deadline = 120
        import time
        t0 = time.time()
        while _L._nlp is None and time.time() - t0 < deadline:
            time.sleep(0.5)
        assert _L._nlp is not None, "spaCy layer never loaded — the spine would no-op"

        _drive(client, user_id, statements=[BATTERY])

        # ── FROZEN BAR 1: the rows read back EXACTLY ────────────────────────────
        facts = _rows(schema, "SELECT subject_id, rel_type, object_id FROM facts "
                              "WHERE deleted_at IS NULL")
        staged = _rows(schema, "SELECT subject_id, rel_type, object_id FROM staged_facts "
                               "WHERE deleted_at IS NULL AND promoted_at IS NULL")
        aliases = _rows(schema, "SELECT entity_id, alias, is_preferred FROM entity_aliases")

        def alias_id(surface):
            hits = [eid for eid, a, _p in aliases if a == surface]
            assert len(hits) == 1, (surface, hits)
            return hits[0]

        compound = alias_id("blue-ringed octopus")
        head = alias_id("octopus")
        cephalopod = alias_id("cephalopod")

        # (a) the GROWN rung: blue-ringed octopus --subclass_of--> octopus
        assert (compound, "subclass_of", head) in staged or \
               (compound, "subclass_of", head) in facts
        # (b) the STATED kind: blue-ringed octopus --subclass_of--> cephalopod
        assert (compound, "subclass_of", cephalopod) in facts
        # (c) THE HARD LINE: the compound node never instance_of's ANYTHING
        assert not any(s == compound and r == "instance_of" for (s, r, _o) in facts + staged)
        # (d) alias layer: the full stated surface is the PREFERRED name; the
        #     hyphen/space part-form of the SAME name is non-preferred; the head
        #     node keeps its own alias (never swallowed by the compound).
        pref = [(eid, a) for eid, a, p in aliases if p]
        assert (compound, "blue-ringed octopus") in pref
        variants = [(eid, a, p) for eid, a, p in aliases
                    if a == "blue ringed octopus"]
        assert variants and variants[0][0] == compound and not variants[0][2]
        assert (head, "octopus") in pref
        # (e) modifier words never became L4 nodes alone
        lone = {a for _e, a, _p in aliases} & {"blue", "ringed", "king",
                                               "companion", "working"}
        assert not lone, lone

        # ── the corrected measurement lands ON the type node ───────────────────
        # (the LLM correction lane's exact `7 centimeters` user_corrected row is proven
        # in the live drive evidence — DEV/review/gauntlet/compound-type-l4/round_1/; here
        # the deterministic supersession route drives the same slot brain-free).
        _drive(client, user_id, statements=[CORRECTION_TURN])
        attrs = _rows(schema, "SELECT entity_id, attribute, value_text FROM entity_attributes "
                              "WHERE superseded_at IS NULL")
        measure_vals = [v for (e, a, v) in attrs if e == compound and a == "measure"]
        assert measure_vals and "7" in measure_vals[0], attrs
        history = _rows(schema, "SELECT prior_value_text, new_value_text FROM "
                                "entity_attributes_history WHERE entity_id = %s AND "
                                "attribute = 'measure'", (compound,))
        assert any("5" in (r[0] or "") for r in history), history

        # ── FROZEN BAR 3: both recall questions walk to the facts ──────────────
        q1 = client.post("/query", json={
            "text": "What do you know about the blue-ringed octopus?",
            "user_id": user_id}).json()
        got1 = {(f.get("subject"), f.get("rel_type"), f.get("object"))
                for f in (q1.get("facts") or [])}
        assert str(q1.get("anchor")) == compound
        assert (compound, "subclass_of", cephalopod) in _resolved(got1, aliases) or \
               any(r == "subclass_of" and o in ("cephalopod", "octopus")
                   for (_s, r, o) in got1), got1
        assert any(r == "measure" for (_s, r, _o) in got1), got1

        q2 = client.post("/query", json={
            "text": "How big does the blue-ringed octopus get?",
            "user_id": user_id}).json()
        got2 = {(f.get("subject"), f.get("rel_type"), f.get("object"))
                for f in (q2.get("facts") or [])}
        assert any(r == "measure" for (_s, r, _o) in got2), got2


def _resolved(got, aliases):
    by_id = {eid: a for eid, a, _p in aliases}
    return {(by_id.get(s, s), r, by_id.get(o, o)) for (s, r, o) in got}


def test_e2e_matrix_specific_arm_reads_instance_of(ct11_tenant, monkeypatch):
    user_id, schema = ct11_tenant
    import src.api.main as M
    from starlette.testclient import TestClient
    monkeypatch.setattr(M, "SPINE_DETERMINISTIC_SEGMENTATION", True)
    M._INGEST_ENABLED = True
    M._TENANT_READY_CACHE.add(user_id)

    class _D:
        def get(self, k):
            return None

        def setex(self, k, t, v):
            return True

        def ping(self):
            return True

    class _Idem:
        redis_url = "dict://test"
        ttl = 3600
        client = _D()

    M._idempotency_mgr = _Idem()
    with TestClient(M.app) as client:
        from src.extraction import linguistics as _L
        import time
        t0 = time.time()
        while _L._nlp is None and time.time() - t0 < 120:
            time.sleep(0.5)
        _drive(client, user_id,
               statements=["This blue-ringed octopus is a cephalopod."])
        facts = _rows(schema, "SELECT subject_id, rel_type, object_id FROM facts "
                              "WHERE deleted_at IS NULL")
        aliases = _rows(schema, "SELECT entity_id, alias FROM entity_aliases")
        by_alias = {a: eid for eid, a in aliases}
        compound = by_alias.get("blue-ringed octopus")
        cephalopod = by_alias.get("cephalopod")
        # FROZEN BAR 2 (specific arm): instance_of — the specific reading keeps the
        # first-mention instance rule.
        assert (compound, "instance_of", cephalopod) in facts, facts
        assert not any(s == compound and r == "subclass_of" for (s, r, _o) in facts)

"""Spanish uplift pins (es branch): kinship-anchor residence walk, employment capture,
negation-scope gating. Capture pins need SPACY_MODEL=es_core_news_md; walk pins also need a
POSTGRES_DSN migrated from this tree (the role-noun cue classes are DB seeds)."""
import datetime
import os
import uuid

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available() or (getattr(m._get_nlp(), "lang", "") != "es"),
    reason="Spanish spaCy model not configured (SPACY_MODEL=es_core_news_md)",
)

_REF = datetime.datetime(2023, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)


def _facts(s):
    return [(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(s, _REF, None)]


# ── Target 1: kin naming never mints a role-noun ghost ─────────────────────────

def test_kin_naming_emits_no_role_noun_alias_edge():
    """'Mi hermana se llama Lucía' → (lucía, sibling_of, user) ALONE. The old extra
    (hermana, also_known_as, lucía) minted a second entity keyed on the role noun, and
    '¿Dónde vive mi hermana?' anchored on that ghost."""
    f = _facts("Mi hermana se llama Lucía.")
    assert ("lucía", "sibling_of", "user") in f, f
    assert not any(r == "also_known_as" for _s, r, _o in f), f


def test_kin_naming_fresh_domain_control():
    f = _facts("Mi hija se llama Elena.")
    assert ("elena", "child_of", "user") in f, f
    assert not any(s == "hija" for s, _r, _o in f), f


def test_non_kin_pronominal_naming_keeps_alias_edge():
    """A named THING (not a person role) keeps its naming edge."""
    f = _facts("Mi perro se llama Rex.")
    assert ("perro", "also_known_as", "rex") in f, f


# ── Target 2: finite verb read back from subject agreement ─────────────────────

def test_employment_org_then_role_captures_both():
    """'Yo trabajo en Google como ingeniero': es_core_news_md tags 'trabajo' NOUN; the nominative
    1sg subject with no copula makes it the finite verb trabajar → works_for + occupation."""
    f = _facts("Yo trabajo en Google como ingeniero.")
    assert ("user", "works_for", "google") in f, f
    assert ("user", "occupation", "ingeniero") in f, f


def test_agreement_repair_fresh_domain_control():
    doc = m._parse("Yo estudio en Salamanca.")
    root = [t for t in doc if t.dep_ == "ROOT"][0]
    assert root.pos_ == "VERB" and root.lemma_ == "estudiar", (root.pos_, root.lemma_)
    doc = m._parse("Yo corro en el parque.")
    root = [t for t in doc if t.dep_ == "ROOT"][0]
    assert root.lemma_ == "correr" and root.morph.get("Person") == ["1"], (root.lemma_, root.morph)


def test_agreement_repair_leaves_agreeing_and_copular_parses_alone():
    doc = m._parse("Tú trabajas en Madrid.")
    root = [t for t in doc if t.dep_ == "ROOT"][0]
    assert root.lemma_ == "trabajar" and root.morph.get("Person") == ["2"]
    # a nominal predicate WITH a copula is a real noun predicate — never re-read as a verb
    doc = m._parse("Yo soy ingeniero.")
    assert all(not (t.text == "ingeniero" and t.pos_ == "VERB") for t in doc)
    # a third-person noun subject is outside the repair (no nominative pronoun)
    doc = m._parse("El trabajo en Google es duro.")
    assert [t for t in doc if t.text == "trabajo"][0].pos_ == "NOUN"


# ── Target 1 (walk): the query anchor reads the role-noun class ingest used ────

def _dsn():
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return None
    try:
        import psycopg2
        psycopg2.connect(dsn, connect_timeout=3).close()
        return dsn
    except Exception:
        return None


@pytest.fixture
def tenant():
    dsn = _dsn()
    if dsn is None:
        pytest.skip("no reachable POSTGRES_DSN")
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid, derive_schema_name)
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    create_user_schema(user_id, slug)
    schema = derive_schema_name(slug)
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO " + schema)
    conn.commit()
    from src.entity_registry.registry import EntityRegistry
    reg = EntityRegistry(conn, auto_commit=True, schema_name=schema)
    yield conn, reg, user_id
    conn.close()


def _fact(conn, s, r, o):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO facts (subject_id, object_id, rel_type, fact_provenance, confidence,"
            " fact_class, polarity) VALUES (%s,%s,%s,'user_stated',1.0,'A','affirmed')"
            " ON CONFLICT (subject_id, object_id, rel_type) DO NOTHING", (s, o, r))
    conn.commit()


def _anchor(conn, user_id, q):
    from src.api.main import resolve_anchor
    return resolve_anchor(q, [], user_id, conn, {})


@pytest.fixture
def seat(monkeypatch):
    """A fresh provisioned seat driven through the REAL harvest → ingest endpoints (in-process),
    with the production naming-chain flag on (every deploy compose sets SPINE_NAMING_CHAIN)."""
    dsn = _dsn()
    if dsn is None:
        pytest.skip("no reachable POSTGRES_DSN")
    import psycopg2
    import src.api.main as M
    from fastapi.testclient import TestClient
    from src.provisioning.schema_manager import (
        create_user_schema, derive_user_slug_from_uuid, derive_schema_name)
    monkeypatch.setattr(m, "SPINE_NAMING_CHAIN", True)
    monkeypatch.setattr(M, "SPINE_NAMING_CHAIN", True)
    monkeypatch.setattr(M, "SENTENCE_PIPELINE", True, raising=False)
    user_id = str(uuid.uuid4())
    client = TestClient(M.app)
    client.post("/query", json={"text": "hola", "user_id": user_id})  # provisioning record
    slug = derive_user_slug_from_uuid(user_id)
    create_user_schema(user_id, slug)
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO " + derive_schema_name(slug))
    conn.commit()
    from src.entity_registry.registry import EntityRegistry
    reg = EntityRegistry(conn, auto_commit=True, schema_name=derive_schema_name(slug))

    def ingest(text):
        h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
        assert h.status_code == 200, h.text[:300]
        edges = h.json().get("edges") or []
        assert edges, f"no edges harvested from {text!r}"
        r = client.post("/ingest", json={"text": text, "user_id": user_id, "source": "mcp",
                                         "edges": edges})
        assert r.status_code == 200, r.text[:300]
        return edges

    yield conn, reg, user_id, ingest
    conn.close()


def _eid(conn, alias):
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s LIMIT 1", (alias,))
        row = cur.fetchone()
    return row[0] if row else None


def test_kinship_role_noun_anchors_the_named_sibling(seat):
    conn, reg, user_id, ingest = seat
    ingest("Mi hermana se llama Lucía.")
    ingest("Lucía vive en Sevilla.")
    lucia = _eid(conn, "lucía")
    assert lucia and _anchor(conn, user_id, "¿Dónde vive mi hermana?") == lucia


def test_social_role_noun_anchors_the_friend_fresh_domain(seat):
    """Real ingest of 'Mi amiga se llama Carmen' (no hand-inserted friend_of)."""
    conn, reg, user_id, ingest = seat
    ingest("Mi amiga se llama Carmen.")
    carmen = _eid(conn, "carmen")
    assert carmen and _anchor(conn, user_id, "¿Dónde vive mi amiga?") == carmen


def test_directional_kin_role_anchors_the_parent_not_the_child(seat):
    """'Mi madre se llama Rosa' (madre tagged PROPN) + a son: 'mi madre' is Rosa, never Teo."""
    conn, reg, user_id, ingest = seat
    ingest("Mi madre se llama Rosa.")
    ingest("Mi hijo se llama Teo.")
    rosa = _eid(conn, "rosa")
    assert rosa and _anchor(conn, user_id, "¿Dónde vive mi madre?") == rosa


def test_catch_all_role_cue_never_answers_uncorroborated_english(tenant):
    """ISSUE #59 BLOCKER (English install): with (user, knows, bob) and (rose, related_to, user)
    on the seat, 'my aunt' / 'my cousin' / 'my colleague' / 'my neighbour' named nobody — the
    role-cue fallback answered with whichever knows/related_to filler existed."""
    conn, reg, user_id = tenant
    user = reg.resolve(user_id, "user")
    bob = reg.resolve(user_id, "bob")
    rose = reg.resolve(user_id, "rose")
    _fact(conn, user, "knows", bob)
    _fact(conn, rose, "related_to", user)
    for q in ("Where does my aunt live?", "Where does my cousin live?",
              "Where does my colleague live?", "Where does my neighbour live?"):
        got = _anchor(conn, user_id, q)
        assert got not in (bob, rose), (q, got)


def test_catch_all_role_cue_never_answers_uncorroborated_spanish(tenant):
    conn, reg, user_id = tenant
    user = reg.resolve(user_id, "user")
    pedro = reg.resolve(user_id, "pedro")
    rosa = reg.resolve(user_id, "rosa")
    _fact(conn, user, "knows", pedro)
    _fact(conn, rosa, "related_to", user)
    for q in ("¿Dónde vive mi tía?", "¿Dónde vive mi primo?", "¿Dónde vive mi vecino?",
              "¿Dónde vive mi colega?"):
        got = _anchor(conn, user_id, q)
        assert got not in (pedro, rosa), (q, got)


def test_role_cue_answers_when_the_slot_corroborates(tenant):
    """Control: the same catch-all rel DOES answer when the filler carries the asked role slot."""
    conn, reg, user_id = tenant
    user = reg.resolve(user_id, "user")
    rose = reg.resolve(user_id, "rose")
    _fact(conn, rose, "related_to", user)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred) VALUES (%s,'aunt',false)"
                    " ON CONFLICT DO NOTHING", (rose,))
    conn.commit()
    assert _anchor(conn, user_id, "Where does my aunt live?") == rose


def test_spanish_occupation_question_scopes_occupation(tenant):
    """Migration 283: '¿Cuál es mi profesión?' scopes the occupation rel ingest files."""
    conn, reg, user_id = tenant
    from src.api.main import determine_path
    path = determine_path("¿Cuál es mi profesión?", conn, user_id=user_id,
                          anchor_resolved_uuid=reg.resolve(user_id, "user"))
    assert "occupation" in set(path.allowed_rels), path.allowed_rels


def test_spanish_occupation_question_fresh_domain_control(tenant):
    conn, reg, user_id = tenant
    from src.api.main import determine_path
    path = determine_path("¿Cuál es el oficio de mi padre?", conn, user_id=user_id,
                          anchor_resolved_uuid=reg.resolve(user_id, "user"))
    assert "occupation" in set(path.allowed_rels), path.allowed_rels


# ── Target 3 (live-LLM findings): coordinated measure, accented terms, /learn language ──

def test_coordinated_tener_measure_binds_the_shared_subject():
    """Live atomizer kept the raw turn as a coverage atom: 'Mi perro se llama Toby y tiene 5
    años' — the conjoined 'tiene' has no subject child, so the measure chain skipped it and the
    SVO lane filed (perro, tener, años) + (años, instance_of, año)."""
    f = _facts("Mi perro se llama Toby y tiene 5 años.")
    assert ("perro", "age", "5") in f, f
    assert not any(r == "tener" for _s, r, _o in f), f
    assert not any(s == "años" for s, _r, _o in f), f


def test_coordinated_measure_fresh_domain_control():
    f = _facts("Mi gata se llama Luna y pesa 4 kilos.")
    assert ("gata", "weight", "4") in f, f
    f = _facts("Me llamo Ana y tengo 30 años.")
    assert ("user", "age", "30") in f, f


def test_term_shape_admits_accented_letters():
    """The orthographic rule was ASCII-only: 'técnica' / 'carpintería' were refused an is-a
    ladder as non_lexical_orthography (measured on a Spanish /learn)."""
    for t in ("técnica", "carpintería", "año", "cigüeña"):
        ok, why = m.type_term_shape(t)
        assert ok, (t, why)


def test_term_shape_still_refuses_values():
    for t in ("notes.md", "~/.config/x", "alpha+beta", "a@b"):
        ok, why = m.type_term_shape(t)
        assert not ok and why == "non_lexical_orthography", (t, why)


def test_learn_prompt_names_the_install_language(tenant, monkeypatch):
    """A Spanish /learn came back as English nodes (beekeeping, hive) that a Spanish query can
    never ground; with only 'the topic's language' the LLM picked Portuguese for 'apicultura'."""
    conn, reg, user_id = tenant
    import asyncio
    import src.api.main as M
    from src.api.models import LearnTopicRequest
    seen = {}

    def _capture(*a, **k):
        seen["messages"] = k.get("messages")
        raise RuntimeError("stop after prompt capture")

    monkeypatch.setattr(M, "build_llm_payload", _capture)
    monkeypatch.setenv("WGM_LLM_MODEL", "stub-model")
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "es")
    try:
        asyncio.run(M.learn_topic(LearnTopicRequest(topic="apicultura", user_id=user_id)))
    except Exception:
        pass
    prompt = (seen.get("messages") or [{}])[0].get("content", "")
    assert "ISO 639-1 code 'es'" in prompt, prompt[:400]
    assert "is a subclass of" in prompt  # the parser's scaffold stays verbatim


def test_learn_prompt_unchanged_on_english_install(tenant, monkeypatch):
    conn, reg, user_id = tenant
    import asyncio
    import src.api.main as M
    from src.api.models import LearnTopicRequest
    seen = {}

    def _capture(*a, **k):
        seen["messages"] = k.get("messages")
        raise RuntimeError("stop")

    monkeypatch.setattr(M, "build_llm_payload", _capture)
    monkeypatch.setenv("WGM_LLM_MODEL", "stub-model")
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "en")
    try:
        asyncio.run(M.learn_topic(LearnTopicRequest(topic="beekeeping", user_id=user_id)))
    except Exception:
        pass
    prompt = (seen.get("messages") or [{}])[0].get("content", "")
    assert prompt and "LANGUAGE:" not in prompt, prompt[:300]


# ── Round 2 (issue #59) capture pins ───────────────────────────────────────────

def test_propn_tagged_kin_role_is_not_a_name(monkeypatch):
    """es_core_news_md tags 'madre' PROPN in 'Mi madre se llama Rosa.'; the alias-predicate chain
    then filed (madre, also_known_as, rosa). A possessive determiner marks the common-noun role."""
    for flag in (False, True):
        monkeypatch.setattr(m, "SPINE_NAMING_CHAIN", flag)
        f = _facts("Mi madre se llama Rosa.")
        assert ("rosa", "parent_of", "user") in f, (flag, f)
        assert not any(s == "madre" for s, _r, _o in f), (flag, f)
        if flag:
            assert ("rosa", "also_known_as", "madre") in f, f  # the role slot on the person


def test_social_role_pronominal_naming_binds_the_person(monkeypatch):
    monkeypatch.setattr(m, "SPINE_NAMING_CHAIN", True)
    f = _facts("Mi amiga se llama Carmen.")
    assert ("carmen", "friend_of", "user") in f, f
    assert ("carmen", "also_known_as", "amiga") in f, f
    assert not any(s == "amiga" for s, _r, _o in f), f
    f = _facts("Mi vecino se llama Paco.")  # fresh-domain control (knows)
    assert ("paco", "knows", "user") in f, f
    assert not any(s == "vecino" for s, _r, _o in f), f


def test_naming_conjunct_rekeys_onto_the_named_person():
    """'Mi hermana se llama Lucía y vive en Sevilla' filed the residence on a bare 'hermana'."""
    f = _facts("Mi hermana se llama Lucía y vive en Sevilla.")
    assert ("lucía", "vivir_en", "sevilla") in f, f
    assert not any(s == "hermana" for s, _r, _o in f), f
    f = _facts("Mi hijo se llama Teo y trabaja en Madrid.")  # fresh-domain control
    assert ("teo", "works_for", "madrid") in f or ("teo", "trabajar_en", "madrid") in f, f
    assert not any(s == "hijo" for s, _r, _o in f), f


def test_measure_after_a_later_coordinator_is_not_the_first_subjects():
    f = _facts("Mi hermana vive en Lima y mido 1,80 metros.")
    assert not any(s == "hermana" and r == "height" for s, r, _o in f), f
    assert ("user", "height", "1,80") in f, f
    assert ("hermana", "vivir_en", "lima") in f, f
    assert not any(o in ("metros", "mido") for _s, _r, o in f), f


def test_measure_conjunct_with_own_subject_control():
    f = _facts("Mi hermana vive en Lima y el edificio mide 30 metros.")
    assert ("edificio", "height", "30") in f, f
    assert not any(s == "hermana" and r == "height" for s, r, _o in f), f


# crafted parses for the agreement-repair guards (the model's own tagging is not needed here)
def _crafted(words, pos, deps, heads, morphs, lemmas):
    from spacy.tokens import Doc
    nlp = m._get_nlp()
    return Doc(nlp.vocab, words=words, pos=pos, deps=deps, heads=heads, morphs=morphs,
               lemmas=lemmas), nlp.get_pipe("lemmatizer")


_NOM1 = "Case=Nom|Number=Sing|Person=1|PronType=Prs"


def test_repair_positive_crafted():
    doc, lem = _crafted(["Yo", "trabajo"], ["PRON", "NOUN"], ["nsubj", "ROOT"], [1, 1],
                        [_NOM1, "Gender=Masc|Number=Sing"], ["yo", "trabajo"])
    m._repair_subject_agreement(doc, lem)
    assert doc[1].pos_ == "VERB" and doc[1].lemma_ == "trabajar"


def test_repair_requires_an_attested_verb():
    """Verb-index acceptance: a surface whose verb re-reading is NOT in the lemmatizer's verb index
    ('xyzzo' → 'xyzzar') is left a NOUN."""
    doc, lem = _crafted(["Yo", "xyzzo"], ["PRON", "NOUN"], ["nsubj", "ROOT"], [1, 1],
                        [_NOM1, "Gender=Masc|Number=Sing"], ["yo", "xyzzo"])
    m._repair_subject_agreement(doc, lem)
    assert doc[1].pos_ == "NOUN" and doc[1].lemma_ == "xyzzo"


def test_repair_skips_a_copular_nominal_predicate():
    """cop/aux guard: 'Yo soy trabajo' (nominal predicate WITH a copula) is never a verb, even
    though 'trabajar' is attested."""
    doc, lem = _crafted(["Yo", "soy", "trabajo"], ["PRON", "AUX", "NOUN"],
                        ["nsubj", "cop", "ROOT"], [2, 2, 2],
                        [_NOM1, "Mood=Ind|Number=Sing|Person=1|Tense=Pres|VerbForm=Fin",
                         "Gender=Masc|Number=Sing"], ["yo", "ser", "trabajo"])
    m._repair_subject_agreement(doc, lem)
    assert doc[2].pos_ == "NOUN"


def test_repair_requires_a_nominative_first_or_second_person_subject():
    """Nom-case / person guard: an accusative or a 3rd-person pronoun subject does not trigger."""
    for morph in ("Case=Acc|Number=Sing|Person=1|PronType=Prs",
                  "Case=Nom|Number=Sing|Person=3|PronType=Prs"):
        doc, lem = _crafted(["X", "trabajo"], ["PRON", "NOUN"], ["nsubj", "ROOT"], [1, 1],
                            [morph, "Gender=Masc|Number=Sing"], ["x", "trabajo"])
        m._repair_subject_agreement(doc, lem)
        assert doc[1].pos_ == "NOUN", morph


def test_learn_prompt_language_directive_on_source_text_branch(tenant, monkeypatch):
    conn, reg, user_id = tenant
    import asyncio
    import src.api.main as M
    from src.api.models import LearnTopicRequest
    seen = {}

    def _capture(*a, **k):
        seen["messages"] = k.get("messages")
        raise RuntimeError("stop after prompt capture")

    monkeypatch.setattr(M, "build_llm_payload", _capture)
    monkeypatch.setenv("WGM_LLM_MODEL", "stub-model")
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "es")
    try:
        asyncio.run(M.learn_topic(LearnTopicRequest(
            topic="apicultura", user_id=user_id, source_text="La apicultura es la cría de abejas.")))
    except Exception:
        pass
    prompt = (seen.get("messages") or [{}])[0].get("content", "")
    assert "reference material" in prompt, prompt[:300]
    assert "ISO 639-1 code 'es'" in prompt, prompt[:400]

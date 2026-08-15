"""Spanish capture + query-back regression gate (es branch)."""
import datetime
import pytest
from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) - spine deriver no-ops",
)

_REF_DATE = datetime.date(2023, 6, 1)
_REF = datetime.datetime(2023, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)


def _facts(sentence):
    return m.derive_sentence_facts(sentence, _REF, None)


def _find(facts, rel):
    for f in facts:
        if (f.rel_type or "") == rel:
            return f
    return None


# ── LAYER 1: SCHEME PORTABILITY ────────────────────────────────────────────────

def test_spanish_possessive_det_reads_like_english_poss():
    """'mi madre' (DET+Poss, UD) must bind the kin rel exactly like 'my mother' (PRON/poss)."""
    facts = _facts("Mi madre tiene 60 años.")
    kin = _find(facts, "parent_of")
    assert kin is not None, f"mi madre -> parent_of missing: {facts}"
    assert kin.subject == "madre" and kin.object == "user"
    age = _find(facts, "age")
    assert age is not None and age.object == "60", f"age missing: {facts}"


def test_spanish_prodrop_first_person_binds_to_user():
    """'Tengo un perro' (pro-drop, Person=1 on the verb — NGLE 33.4a-b) -> subject is the user."""
    facts = _facts("Tengo un perro.")
    own = _find(facts, "tener")
    assert own is not None and own.subject == "user" and own.object == "perro", f"{facts}"


def test_spanish_copular_predicate_is_the_ud_head():
    """UD es makes the PREDICATE the clause head with the copula as AUX/cop (UD cop page)."""
    facts = _facts("Rex es un labrador.")
    io = _find(facts, "instance_of")
    assert io is not None and io.subject == "rex" and io.object == "labrador", f"{facts}"


def test_spanish_pronominal_naming_binds_the_name():
    """'se llama' — UD expl:pv (inherently reflexive verb) -> also_known_as edge."""
    facts = _facts("Mi perro se llama Rex.")
    aka = _find(facts, "also_known_as")
    assert aka is not None and aka.subject == "perro" and aka.object == "rex", f"{facts}"


def test_spanish_dative_experiencer_is_the_speaker():
    """'Me gusta la pizza' — dative experiencer (NGLE 15.11o): the speaker likes pizza."""
    facts = _facts("Me gusta la pizza.")
    like = _find(facts, "gustar")
    assert like is not None and like.subject == "user" and like.object == "pizza", f"{facts}"


def test_spanish_de_genitive_binds_the_name_to_the_role():
    """'El nombre de mi madre es Diane' (de-PP genitive) -> (diane, parent_of, user)."""
    facts = _facts("El nombre de mi madre es Diane.")
    kin = _find(facts, "parent_of")
    assert kin is not None and kin.subject == "diane" and kin.object == "user", f"{facts}"


def test_spanish_tener_measure_is_a_scalar():
    """'Tengo 34 años' -> (user, age, 34); the SVO twin must be suppressed."""
    facts = _facts("Tengo 34 años.")
    age = _find(facts, "age")
    assert age is not None and age.subject == "user" and age.object == "34", f"{facts}"
    assert _find(facts, "tener") is None, f"tener twin leaked: {facts}"


def test_spanish_measure_obl_unit_captured():
    """'Corrí 5 kilómetros' — the distance unit attaches as obl (not obj) on the motion verb."""
    facts = _facts("Corrí 5 kilómetros esta mañana.")
    dist = _find(facts, "distance")
    assert dist is not None and dist.subject == "user" and dist.object == "5", f"{facts}"


def test_spanish_feelings_via_prodrop_copula():
    """'Estoy cansado' (null-subject copula, Person=1) -> the affect seam reads it as feels."""
    comps = m.analyze_copula_affect_complements("Estoy cansado.")
    assert "cansado" in comps, f"affect complements: {comps}"


def test_spanish_preference_via_possessive_predication():
    """'Mi color favorito es el azul' -> the preference seam reads the possessed value."""
    pp = m.analyze_possessive_predication("Mi color favorito es el azul.")
    assert pp is not None and pp.possessed == "color" and "azul" in pp.value, f"{pp}"


def test_spanish_worded_dates_resolve(monkeypatch):
    """The date layer resolves Spanish worded dates (migration 218 month cues + dateparser es).

    The dateparser language pin is read at import from FAULTLINE_LANGUAGE (the es branch ships
    it in .env.example); the test pins it explicitly so the date path is exercised on any env.
    """
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "es")
    import importlib
    importlib.reload(m)
    try:
        iso, gran = m.extract_event_date("Nací el 15 de marzo de 1990.", _REF)
        assert iso is not None and iso.startswith("1990-03-15"), f"{iso}"
    finally:
        importlib.reload(m)


def test_spanish_relative_dates_resolve(monkeypatch):
    """Relative Spanish dates (ayer/mañana/hace N) resolve via the DB temporal cues."""
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "es")
    import importlib
    importlib.reload(m)
    try:
        iso, gran = m.extract_event_date("La boda fue hace dos semanas.", _REF)
        assert iso is not None, "hace dos semanas did not resolve"
    finally:
        importlib.reload(m)


def test_spanish_residence_city_not_lost():
    """'Mis hermanos viven en México' must keep the city AND emit the folded residence predicate
    (critic round-2: the object was kept but the rel stayed bare 'vivir' — unseeded, invisible to
    the walk). The SVO lane now folds the UD case particle ('vivir_en', the English 'live_in'
    mirror), and the seeded alias folds it to lives_in at ingest so '¿dónde vives?' walks it."""
    facts = _facts("Mis hermanos viven en México.")
    assert any(f.object == "méxico" for f in facts), f"city lost: {facts}"
    preds = {f.rel_type for f in facts}
    assert "vivir_en" in preds, f"residence predicate not folded to vivir_en: {preds}"


def test_spanish_residence_predicate_canonicalizes_to_lives_in():
    """The folded 'vivir_en' predicate must canonicalize to lives_in (the walk's rel) exactly like
    English 'live_in' — via the seeded alias, NOT a vivir_en rel_type (an exact PK would shadow
    the alias at RUNG 2; measured). This is the /ingest convergence path, pinned here."""
    import os
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        pytest.skip("no POSTGRES_DSN — alias canonicalization needs the seeded DB")
    from src.ontology.canonical import resolve_canonical
    res = resolve_canonical("vivir_en", dsn, "faultline_9d5a4f62_8b6c_4b3e_9d4f_1a2b3c4d5e6f")
    assert (res or {}).get("canonical") == "lives_in", f"vivir_en -> {res}"


def test_spanish_bare_occupation_classified():
    """'Mi madre es enfermera' (article-less occupation under ser) -> instance_of (critic finding 2)."""
    facts = _facts("Mi madre es enfermera.")
    io = _find(facts, "instance_of")
    assert io is not None and io.subject == "madre" and io.object == "enfermera", f"{facts}"


def test_spanish_color_not_minted_as_a_type():
    """'Mi coche es rojo' must NOT mint (coche, instance_of, rojo) (critic round-2 fabrication).

    es_core_news_md tags the ADJECTIVE 'rojo' as NOUN in copular position (its inflected form
    'roja' tags ADJ — same lemma, gender-dependent POS), so the bare-NOUN occupation arm would
    file a color as a TYPE. The occupation reading is grammatical only for a PERSON subject
    (kinship-class head or a proper name); a thing subject ('mi coche') with a NOUN-tagged
    complement is a mis-tagged property -> honest-empty (the pre-fix state), never a fabricated
    instance_of."""
    facts = _facts("Mi coche es rojo.")
    assert _find(facts, "instance_of") is None, f"color minted as type: {facts}"


def test_spanish_self_name_not_minted_as_owns():
    """'Mi nombre es Carlos' must NOT mint (user, owns, nombre) (critic finding 3 — fabrication)."""
    facts = _facts("Mi nombre es Carlos.")
    assert _find(facts, "owns") is None, f"owns fabrication: {facts}"


def test_spanish_third_party_kinship_name_binds_the_owner():
    """'La madre de Juan se llama Ana' must bind (ana, parent_of, JUAN), never the user (a
    fabrication of the round-1 ghost class: Juan's mother is not the speaker's mother)."""
    facts = _facts("La madre de Juan se llama Ana.")
    kin = [f for f in facts if f.rel_type == "parent_of"]
    assert len(kin) == 1 and kin[0].subject == "ana" and kin[0].object == "juan", f"{facts}"


def test_spanish_named_possessor_genitive_name_captured():
    """'El nombre de Juan es Pedro' must capture (pedro, related_to, juan) — parity with English
    "Juan's name is Pedro" -> (pedro, related_to, juan) (pre-fix this returned [])."""
    facts = _facts("El nombre de Juan es Pedro.")
    rt = [f for f in facts if f.rel_type == "related_to"]
    assert len(rt) == 1 and rt[0].subject == "pedro" and rt[0].object == "juan", f"{facts}"


def test_spanish_de_genitive_role_collapsed():
    """'El nombre de mi madre es Diane' must NOT leave a ghost (madre, parent_of, user) alongside
    the named person (critic finding 4)."""
    facts = _facts("El nombre de mi madre es Diane.")
    subjects = [f.subject for f in facts if f.rel_type == "parent_of"]
    assert "diane" in subjects, f"{facts}"
    assert "madre" not in subjects, f"ghost madre entity: {facts}"


def test_spanish_negated_dative_is_absence():
    """'No me gusta el café' must NOT capture the affirmed preference (my own finding, same class
    as the branch's LEEME-es.md negation-corruption warning)."""
    facts = _facts("No me gusta el café.")
    assert _find(facts, "gustar") is None, f"negated preference affirmed: {facts}"


def test_spanish_weekday_complement_is_not_a_type():
    """'La reunión es el lunes' must NOT mint (reunión, instance_of, lunes) — a calendar complement
    is not a type (my own finding)."""
    facts = _facts("La reunión es el lunes.")
    assert _find(facts, "instance_of") is None, f"weekday minted as type: {facts}"


def test_spanish_det_arm_color_not_minted_as_type():
    """'Mi coche es el azul' (article + nominalized color) must NOT mint (coche, instance_of,
    azul) (critic round-2 blocker: the person gate covered only the article-less bare arm; the
    DET arm filed a color as a TYPE). 'Rex es un labrador' / 'París es la capital' stay."""
    for s in ("Mi coche es el azul.", "El libro es el rojo.", "El perro es el bravo."):
        facts = _facts(s)
        assert _find(facts, "instance_of") is None, f"{s!r} minted a type: {facts}"
    assert _find(_facts("Rex es un labrador."), "instance_of") is not None
    assert _find(_facts("París es la capital."), "instance_of") is not None


def test_spanish_first_person_naming_binds_the_name():
    """'Me llamo Carlos' must emit (user, also_known_as, carlos) — the 1sg clitic parses as
    iobj/Case=Dat (not expl:pv) and the model puts the NAME in the nsubj slot; the naming chain
    now accepts both (critic round-2 should-fix). And it must NOT twin a verb-lemma rel
    (user, llamar, carlos) via the dative-experiencer lane."""
    facts = _facts("Me llamo Carlos.")
    aka = _find(facts, "also_known_as")
    assert aka is not None and aka.subject == "user" and aka.object == "carlos", f"{facts}"
    assert _find(facts, "llamar") is None, f"naming/dative twin leaked: {facts}"


def test_spanish_negated_naming_is_absence():
    """'No me llamo Carlos' must NOT capture the affirmed name — a negated naming clause denies
    the NAME (same negation-as-absence corruption the LEEME warns about for preferences)."""
    facts = _facts("No me llamo Carlos.")
    assert _find(facts, "also_known_as") is None, f"negated name affirmed: {facts}"


def test_spanish_kin_naming_has_no_role_noun_ghost():
    """'Mi hermana se llama Ana' must NOT mint a standalone (hermana, sibling_of, user) twin
    alongside the name-bound (ana, sibling_of, user) — the role noun is a slot on the named
    person (round-1 critic ghost class, re-found by the round-1 re-review)."""
    facts = _facts("Mi hermana se llama Ana.")
    kin = [f for f in facts if f.rel_type == "sibling_of"]
    assert len(kin) == 1 and kin[0].subject == "ana" and kin[0].object == "user", f"{facts}"


def test_spanish_teen_cardinal_dates_resolve(monkeypatch):
    """'hace dieciséis semanas' resolves (the cardinal map covers 16-19, matching the worded-day
    regex) — the round-1 re-review's dieciséis gap."""
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "es")
    import importlib
    importlib.reload(m)
    try:
        iso, gran = m.extract_event_date("La boda fue hace dieciséis semanas.", _REF)
        assert iso is not None, "dieciséis semanas did not resolve"
    finally:
        importlib.reload(m)


def test_spanish_con_particle_folds():
    """'Quedo con Ana' must fold quedar_con (the es model parses Ana as obj+case, not obl+case —
    critic round-2 should-fix; the fold arm now reads obj/dobj+case like obl+case)."""
    facts = _facts("Quedo con Ana.")
    assert any(f.rel_type == "quedar_con" and f.object == "ana" for f in facts), f"{facts}"


def test_spanish_possessive_subject_state_not_the_det():
    """'Mi perro es marrón' must emit (perro, has_state, marrón), NEVER (mi, has_state, marrón) —
    the es model mis-attaches the possessive DET as nsubj (Mi/DET/nsubj, perro/flat); the
    copula-state chain now resolves the possessed noun (critic round-3 blocker)."""
    facts = _facts("Mi perro es marrón.")
    hs = [f for f in facts if f.rel_type == "has_state"]
    assert len(hs) == 1 and hs[0].subject == "perro" and hs[0].object == "marrón", f"{facts}"


def test_spanish_employment_como_role_no_corruption():
    """'Yo trabajo como ingeniero.' must emit occupation(user, ingeniero), NEVER the corruption
    (user, has_state, trabajar) — the critic's A: 'trabajar' was missing from the es
    employment_verb cue class, so the intransitive chain read the 1sg nmod role ('ingeniero'
    parses nmod, not obl) as objectless and minted a FALSE STATE about the user. The 'como'
    role marker (UD case/mark on an obl/nmod head) is the Spanish twin of Penn 'as'."""
    facts = _facts("Yo trabajo como ingeniero.")
    occ = _find(facts, "occupation")
    assert occ is not None and occ.subject == "user" and occ.object == "ingeniero", f"{facts}"
    assert _find(facts, "has_state") is None, f"corruption has_state minted: {facts}"


def test_spanish_employment_role_and_org_both_captured():
    """'Yo trabajo como ingeniero en Google.' must capture BOTH the role and the employer —
    the 'en'/'para' org markers (UD case on an obl/nmod) are the Spanish twins of Penn
    at/for, read off the verb OR the role head (English parity: 'I work as an engineer at
    Google' -> occupation + works_for)."""
    facts = _facts("Yo trabajo como ingeniero en Google.")
    occ = _find(facts, "occupation")
    wf = _find(facts, "works_for")
    assert occ is not None and occ.subject == "user" and occ.object == "ingeniero", f"{facts}"
    assert wf is not None and wf.subject == "user" and wf.object == "google", f"{facts}"


def test_spanish_employment_org_folds_to_works_for():
    """'Yo trabajo en Google.' / 'Ella trabaja para IBM.' emit works_for DIRECTLY at capture
    (canonical rel, no alias dependency) — the employment chain's es 'en'/'para' org arm."""
    for s, org in (("Yo trabajo en Google.", "google"), ("Ella trabaja para IBM.", "ibm")):
        facts = _facts(s)
        wf = _find(facts, "works_for")
        assert wf is not None and wf.object == org, f"{s!r}: {facts}"


def test_spanish_relative_pronoun_binds_the_antecedent():
    """'Tengo un perro que se llama Rex.' must bind the name to the ANTECEDENT (perro, aka,
    rex), never mint the relative pronoun as an entity (que, ...) — the es 'que' is PRON with
    PronType=Int,Rel (no Penn WP tag), so the chokepoint relative-pronoun guard was blind to
    it (critic's D); the es arm of _is_relative_pronoun (morphology, NO word list) now
    resolves it like EN 'that'."""
    facts = _facts("Tengo un perro que se llama Rex.")
    aka = [f for f in facts if f.rel_type == "also_known_as"]
    assert len(aka) == 1 and aka[0].subject == "perro" and aka[0].object == "rex", f"{facts}"
    assert not any(f.subject == "que" or f.object == "que" for f in facts), f"que minted: {facts}"


def test_spanish_relative_pronoun_never_an_entity():
    """A relative pronoun is NEVER bound as an entity in any chain: 'El lunes que viene
    vuelvo.' must not mint (que, ...) — the subject resolves to the antecedent (lunes),
    mirroring the EN engine's (monday, has_state, come)."""
    facts = _facts("El lunes que viene vuelvo.")
    assert not any(f.subject == "que" or f.object == "que" for f in facts), f"que minted: {facts}"


def test_spanish_next_week_resolves_with_article(monkeypatch):
    """'Vuelvo la próxima semana.' resolves against the reference — the relative cue must match
    the ARTICLE form 'la próxima semana' (dateparser's es locale resolves the article form,
    NOT the bare 'próxima semana' — measured on the pinned es locale). The es locale grounds
    the future-week phrase deterministically (exceeds the EN engine, which deliberately skips
    'next week' — documented divergence)."""
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "es")
    import importlib
    importlib.reload(m)
    try:
        iso, gran = m.extract_event_date("Vuelvo la próxima semana.", _REF)
        assert iso is not None and iso.startswith("2023-06-08"), f"próxima semana: {iso}"
    finally:
        importlib.reload(m)


def test_spanish_weekday_grounded(monkeypatch):
    """'Vuelvo el lunes.' resolves to a concrete weekday (prefer-past, like the EN engine's
    'next Monday' — the weekday translate gate) — the es weekday seeds are live, not dead."""
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "es")
    import importlib
    importlib.reload(m)
    try:
        iso, gran = m.extract_event_date("Vuelvo el lunes.", _REF)
        assert iso is not None and iso.startswith("2023-05-29"), f"lunes: {iso}"
    finally:
        importlib.reload(m)


# ── LAYER 2: CAPTURE COMPARED AGAINST THE ENGLISH ENGINE (parity) ───────────────
# The English engine runs on en_core_web_sm; the Spanish engine on es_core_news_md. The
# deriver reads SPACY_MODEL once at import, so BOTH engines cannot live in one process.
# Each parity test runs the ENGLISH sentence through a SUBPROCESS with the en model and
# compares the captured rel set against the Spanish capture — the brief's per-construction
# bar: Spanish must capture the same grounded facts the English engine captures.

import json as _json
import subprocess as _subprocess
import sys as _sys
import os as _os

_EN_HELPER = _os.path.join(_os.path.dirname(__file__), "_es_en_capture_helper.py")


def _english_capture(sentence):
    """Run the deriver on an ENGLISH sentence in a subprocess pinned to en_core_web_sm."""
    env = dict(_os.environ, SPACY_MODEL="en_core_web_sm")
    r = _subprocess.run(
        [_sys.executable, _EN_HELPER, sentence],
        capture_output=True, text=True, env=env, timeout=60,
    )
    if r.returncode != 0:
        raise AssertionError(f"english helper failed: {r.stderr[-500:]}")
    # the helper prints the RESULT FILE path (stdout carries structlog noise)
    _out_lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    if not _out_lines:
        raise AssertionError(f"english helper produced no result path: {r.stdout!r}")
    _rf = _out_lines[-1]
    with open(_rf) as _fh:
        return _json.load(_fh)


def _spanish_rel_set(facts):
    return {(f.subject or ""), (f.rel_type or ""), (f.object or "")}


def test_spanish_possession_matches_english_capture():
    es = _facts("Tengo un perro.")
    en = _english_capture("I have a dog.")
    assert any(f.subject == "user" and f.object == "perro" for f in es), f"{es}"
    assert any(f["subject"] == "user" and f["object"] == "dog" for f in en), f"{en}"


def test_spanish_kinship_age_matches_english_capture():
    es = _facts("Mi madre tiene 60 años.")
    en = _english_capture("My mother is 60 years old.")
    for facts in (es, en):
        rels = {(f["rel_type"] if isinstance(f, dict) else f.rel_type) for f in facts}
        assert "parent_of" in rels, f"{facts}"
        assert "age" in rels, f"{facts}"
    es_age = _find(es, "age"); en_age = next((f for f in en if f["rel_type"] == "age"), None)
    assert es_age is not None and en_age is not None and es_age.object == en_age["object"] == "60"


def test_spanish_measurement_matches_english_capture():
    es_age = _find(_facts("Tengo 34 años."), "age")
    en = _english_capture("I am 34 years old.")
    en_age = next((f for f in en if f["rel_type"] == "age"), None)
    assert es_age is not None and en_age is not None
    assert es_age.object == en_age["object"] == "34"


def test_spanish_classification_matches_english_capture():
    es = _find(_facts("Rex es un labrador."), "instance_of")
    en = _english_capture("Rex is a labrador.")
    en_io = next((f for f in en if f["rel_type"] == "instance_of"), None)
    assert es is not None and en_io is not None
    assert es.subject == en_io["subject"] and es.object == en_io["object"] == "labrador"


def test_spanish_naming_matches_english_capture():
    es = _facts("Mi hermana se llama Ana.")
    en = _english_capture("My sister is called Ana.")
    es_rels = {f.rel_type for f in es}
    en_rels = {f["rel_type"] for f in en}
    assert "sibling_of" in es_rels, f"{es}"
    assert "sibling_of" in en_rels, f"{en}"
    assert "also_known_as" in es_rels, f"{es}"  # Spanish ALSO binds the name


# ── LAYER 3: QUERY-BACK WALK (DSN-gated) ───────────────────────────────────────

def _walk(conn, user_id, query_text):
    from src.api.main import resolve_anchor, determine_path, fetch_facts_from_anchor
    resolution = {}
    anchor = resolve_anchor(query_text, [], user_id, conn, resolution)
    path = determine_path(query_text, conn, user_id=user_id, anchor_resolved_uuid=anchor)
    return (resolution, path, fetch_facts_from_anchor(anchor, user_id, path, query_text=query_text))


def _scratch_dsn():
    """The test tenant DSN, or None when unreachable (honest skip)."""
    import os, psycopg2
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.close()
        return dsn
    except Exception:
        return None


def test_spanish_query_walk_returns_the_captured_age():
    """END-TO-END: capture (user, age, 34) then '¿cuántos años tengo?' walks back the age."""
    import psycopg2
    dsn = _scratch_dsn()
    if dsn is None:
        pytest.skip("no reachable POSTGRES_DSN — the walk needs a provisioned tenant")
    user_id = "9d5a4f62-8b6c-4b3e-9d4f-1a2b3c4d5e6f"
    from src.provisioning.schema_manager import derive_user_slug_from_uuid, derive_schema_name
    schema = derive_schema_name(derive_user_slug_from_uuid(user_id))
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO " + schema)
    conn.commit()
    from src.entity_registry.registry import EntityRegistry
    reg = EntityRegistry(conn, auto_commit=True, schema_name=schema)
    user_uuid = reg.resolve(user_id, "user")
    cur.execute(
        "INSERT INTO entity_attributes (entity_id, attribute, value_text, user_id, provenance, datatype) "
        "VALUES (%s, %s, %s, %s, 'user_stated', 'string') "
        "ON CONFLICT (entity_id, attribute) DO UPDATE SET value_text = EXCLUDED.value_text",
        (user_uuid, "age", "34", user_id),
    )
    conn.commit()
    _res, path, facts = _walk(conn, user_id, "¿cuántos años tengo?")
    assert "age" in path.scalar_rels, f"age not scoped: {path.scalar_rels}"
    # the walk returns attribute rows normalized to the fact shape: rel_type=age, object=34
    assert any(
        (f.get("rel_type") or "") == "age" and str(f.get("object")) == "34"
        for f in (facts or [])
    ), f"walk did not return the age: {facts}"
    cur.close(); conn.close()


def test_spanish_query_walk_returns_the_residence():
    """END-TO-END: a stored (user, lives_in, <city>) edge is walked back by the Spanish query
    '¿dónde vivo?' — the critic's round-2 blocker: the residence rel was emitted bare 'vivir'
    (unseeded, invisible to the walk) AND the alias never seeded (the ON CONFLICT target didn't
    match the table's unique constraint). Now the fold emits vivir_en, the alias folds it to
    lives_in at ingest, and the query verb-form aliases (vivo/vives/vive -> lives_in) scope the
    walk exactly like English 'live'/'lives'."""
    import psycopg2
    dsn = _scratch_dsn()
    if dsn is None:
        pytest.skip("no reachable POSTGRES_DSN — the walk needs a provisioned tenant")
    user_id = "9d5a4f62-8b6c-4b3e-9d4f-1a2b3c4d5e6f"
    from src.provisioning.schema_manager import derive_user_slug_from_uuid, derive_schema_name
    schema = derive_schema_name(derive_user_slug_from_uuid(user_id))
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO " + schema)
    conn.commit()
    from src.entity_registry.registry import EntityRegistry
    reg = EntityRegistry(conn, auto_commit=True, schema_name=schema)
    user_uuid = reg.resolve(user_id, "user")
    # a Location entity for the city (resolve creates/grounds it)
    madrid_uuid = reg.resolve("madrid", "Location")
    cur.execute(
        "INSERT INTO facts (subject_id, object_id, rel_type, fact_provenance, confidence, fact_class, polarity) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (subject_id, object_id, rel_type) DO UPDATE SET fact_provenance = EXCLUDED.fact_provenance",
        (user_uuid, madrid_uuid, "lives_in", "user_stated", 1.0, "A", "affirmed"),
    )
    conn.commit()
    _res, path, facts = _walk(conn, user_id, "¿dónde vivo?")
    assert "lives_in" in path.relationship_rels, f"lives_in not scoped: {path.relationship_rels}"
    assert any(
        (f.get("rel_type") or "") == "lives_in" for f in (facts or [])
    ), f"walk did not return the residence: {facts}"
    cur.close(); conn.close()
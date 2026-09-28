"""Dated passive-event capture — bind the event_date + predicate to the NAMED entity, and never
let the passive participle become a relationship object.

These lock the fully ENTITY-AGNOSTIC + PREDICATE-AGNOSTIC grammatical rule in
``derive_sentence_facts`` (the ``_chain_passive_event`` pre-pass + chain) and the passive-participle
guard in ``analyze_possessive_predication``. NO domain/lemma/role literals are asserted on the
CODE side — the rule is driven purely by passive-participle morphology (VBN + a 'be' auxpass) and
the existing naming/possessive/coref subject resolution. Validated across a person, an org, a
server, a product, and a pet, each with a DIFFERENT participle.
"""

import datetime
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


_HAS_MODEL = _reload(LINGUISTIC_LAYER="true").linguistics_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed")

_REF = datetime.datetime(2026, 6, 30)


def _facts(sentence):
    m = _reload(LINGUISTIC_LAYER="true", SPINE_NAMING_CHAIN="true")
    return [(f.subject, f.rel_type, f.object, f.event_date, f.scalar_datatype)
            for f in m.derive_sentence_facts(sentence, _REF)]


def _find(facts, subject, rel):
    return [f for f in facts if f[0] == subject and f[1] == rel]


# ───────────────────────── date binds to the NAMED entity ─────────────────────────

@requires_model
def test_birth_date_binds_to_named_person_not_role():
    f = _facts("My wife Nora was born on July 8, 1985.")
    # the DATE is filed on the NAME (nora), never on the role noun "wife"
    born = _find(f, "nora", "born")
    assert born and born[0][2] == "1985-07-08"
    assert born[0][3] and born[0][3].startswith("1985-07-08")
    # the role stays the kinship relation, untouched
    assert _find(f, "nora", "spouse")
    # NO junk: the participle is never a state OBJECT, and "wife" never carries the date
    assert not [x for x in f if x[2] in ("born", "bear")]
    assert not _find(f, "wife", "has_state")


@requires_model
def test_three_people_birthdays_on_their_names():
    for sent, name, val in (
        ("My son Leo was born on March 14, 2012.", "leo", "2012-03-14"),
        ("My mother Carol was born in 1958.", "carol", "1958"),
    ):
        f = _facts(sent)
        born = _find(f, name, "born")
        assert born and born[0][2] == val, (sent, f)


@requires_model
def test_first_person_birth_binds_to_user():
    f = _facts("I was born on April 2, 1980.")
    born = _find(f, "user", "born")
    assert born and born[0][2] == "1980-04-02"


# ───────────────────────── subject- & predicate-agnostic (no literals) ─────────────────────────

@requires_model
def test_org_founded_date_on_name():
    f = _facts("My company Acme was founded in 1998.")
    assert _find(f, "acme", "founded") and _find(f, "acme", "founded")[0][2] == "1998"


@requires_model
def test_server_provisioned_date_on_name():
    f = _facts("My server Apollo was provisioned on March 1, 2023.")
    prov = _find(f, "apollo", "provisioned")
    assert prov and prov[0][2] == "2023-03-01"


@requires_model
def test_product_released_common_noun_subject():
    # no appositive name — the definite common-noun subject IS the entity (no person gate)
    f = _facts("The product was released in 2019.")
    rel = _find(f, "product", "released")
    assert rel and rel[0][2] == "2019"


@requires_model
def test_pet_born_date_on_name():
    f = _facts("My dog Fraggle was born in 2020.")
    assert _find(f, "fraggle", "born") and _find(f, "fraggle", "born")[0][2] == "2020"


@requires_model
def test_name_preferred_over_possessed_common_noun_two_nsubjpass():
    # spaCy attaches "my cat Whiskers" as TWO nsubjpass siblings (cat + Whiskers); the date must bind
    # to the trailing PROPER NAME (whiskers), NEVER the possessed common noun (cat). Non-kinship,
    # non-person — the SAME grammatical name-preference rule as "my wife Nora".
    f = _facts("My cat Whiskers was born on May 3, 2019.")
    assert _find(f, "whiskers", "born") and _find(f, "whiskers", "born")[0][2] == "2019-05-03"
    assert not _find(f, "cat", "born")   # the common noun never carries the date


# ───────────────────────── the participle is NEVER an object ─────────────────────────

@requires_model
def test_passive_participle_never_a_relationship_object():
    for sent in (
        "My wife Nora was born on July 8, 1985.",
        "My company Acme was founded in 1998.",
        "My server Apollo was provisioned on March 1, 2023.",
    ):
        f = _facts(sent)
        # no edge anywhere carries the participle SURFACE as its OBJECT
        for subj, rel, obj, _ed, _dt in f:
            assert obj not in ("born", "founded", "provisioned", "bear"), (sent, subj, rel, obj)


@requires_model
def test_possessive_predication_rejects_passive_participle_complement():
    # "my <X> was <PARTICIPLE>" must NOT be read as a preference/attribute value → no (user, X, participle)
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_possessive_predication("My wife was born on July 8, 1985.") is None
    assert m.analyze_possessive_predication("My server was provisioned in 2023.") is None
    # a genuine possessive attribute value (NOUN/ADJ complement) still fires
    pp = m.analyze_possessive_predication("My favorite color is teal.")
    assert pp is not None and pp.value == "teal"


# ───────────── construction-agnostic: same date, possessive/copula/have phrasings ─────────────

@requires_model
def test_genitive_copula_date_attribute_on_owner():
    # "<owner>'s <noun> is <date>" → a dated scalar named by the noun, on the owner (not the noun/month)
    f = _facts("Fraggle's birthday is March 3, 2020.")
    bd = _find(f, "fraggle", "birthday")
    assert bd and bd[0][2] == "2020-03-03" and bd[0][4] == "date"
    # NO month/day junk: "March" is never an entity, "3" is never an age
    assert not [x for x in f if x[0] == "march" or x[2] == "march"]
    assert not _find(f, "march", "age")


@requires_model
def test_have_noun_of_date_on_subject():
    f = _facts("Fraggle has a birthday of March 3, 2020.")
    assert any(s == "fraggle" and r == "birthday" and o == "2020-03-03"
               for s, r, o, _e, _d in f)


@requires_model
def test_nested_genitive_provision_date_on_named_server():
    # "My server Apollo's provision date is 2023-03-01" → the compound attribute on the GENITIVE name
    f = _facts("My server Apollo's provision date is 2023-03-01.")
    pd = _find(f, "apollo", "provision date")
    assert pd and pd[0][2] == "2023-03-01"
    assert not _find(f, "date", "instance_of")   # "date" is the attribute, never a standalone entity


@requires_model
def test_first_person_date_attribute_binds_to_user():
    f = _facts("My birthday is July 8, 1985.")
    bd = _find(f, "user", "birthday")
    assert bd and bd[0][2] == "1985-07-08"


@requires_model
def test_dateless_attribute_reference_invents_nothing():
    # "X's birthday is a happy day" carries NO date → the date-value chain fires NOTHING (no fabrication)
    f = _facts("Fraggle's birthday is a happy day.")
    assert not any(d == "date" for _s, _r, _o, _e, d in f)  # no dated scalar emitted


# ───────────────────────── no regressions ─────────────────────────

@requires_model
def test_dateless_passive_state_unchanged():
    # a passive with NO date is NOT our lane — the existing intransitive state capture is untouched
    f = _facts("The server was decommissioned.")
    assert _find(f, "server", "has_state")
    assert not _find(f, "server", "decommissioned")


@requires_model
def test_by_agent_passive_left_to_relational_chain():
    # "founded BY Ada" is captured relationally (found_by) with its date — our lane must NOT fire a twin
    f = _facts("The company was founded in 1998 by Ada.")
    assert not _find(f, "company", "founded")   # no date-only scalar twin
    assert any(rel == "found_by" for _s, rel, _o, _e, _d in f)


# ───────────────────── coordinated reduced participle → date on the named entity ─────────────────
# Ingest-hardening: "a dog named Rex, born in 2020" — the coordinated reduced participle ("born",
# an acl of the appositive NAME with NO auxpass) must bind its date to the named instance (rex),
# NOT leak as (rex, has_state, bear). Reuses the passive-event pre-pass (reduced-participle branch).

@requires_model
def test_coordinated_reduced_participle_binds_date_to_name():
    f = _facts("I have a dog named Rex, born in 2020.")
    born = _find(f, "rex", "born")
    assert born and born[0][2] == "2020", f
    # the participle is NEVER a state OBJECT — no (rex, has_state, bear) junk
    assert not [x for x in f if x[2] in ("bear", "born") and x[1] == "has_state"]
    assert not _find(f, "rex", "has_state")


@requires_model
def test_reduced_participle_is_predicate_agnostic():
    # subject- & predicate-agnostic: any "<thing named Name>, <participle> in <year>"
    f = _facts("I own a server called Apollo, provisioned in 2021.")
    prov = _find(f, "apollo", "provisioned")
    assert prov and prov[0][2] == "2021", f


# ───────────────────── possessed-typed device + structured-atomic (Bug 1 deriver half) ───────────
# "My router is a UniFi at 192.168.1.1" — read as a CLASSIFICATION so the noun grounds as an ENTITY
# (owns + instance_of), freeing the /ingest atomic detector to host (router, has_ip, <IP>). The
# possessed noun must NEVER become a scalar rel_type (which would collide with the router entity).

@requires_model
def test_possessed_typed_atomic_frees_entity():
    f = _facts("My router is a UniFi at 192.168.1.1.")
    assert _find(f, "user", "owns") and _find(f, "user", "owns")[0][2] == "router", f
    assert _find(f, "router", "instance_of") and _find(f, "router", "instance_of")[0][2] == "unifi", f
    # the possessed noun must NOT be minted as a scalar rel_type (the collision that dropped has_ip)
    assert not any(rel == "router" for _s, rel, _o, _e, _d in f), f


@requires_model
def test_plain_typed_classification_without_atomic_unchanged():
    # NO structured-atomic → the pre-pass must NOT fire; "my router is a UniFi" stays a scalar/pref
    f = _facts("My router is a UniFi.")
    assert not _find(f, "router", "instance_of"), f

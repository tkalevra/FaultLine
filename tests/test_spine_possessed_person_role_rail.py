"""POSSESSED PERSON-ROLE RAIL — "my colleague" must be grown the same way as "my sister".

THE DEFECT (measured on the REAL deriver against a GLiNER2-typed Doc, the DEPLOYED flag state
``SENTENCE_PIPELINE=SPINE_NAMING_CHAIN=true`` every shipped compose sets):

    "My colleague works late."           -> (user, owns, colleague)     a PERSON filed as an OWNED OBJECT
    "My friend Devon works on X."        -> (friend, also_known_as, devon)   a NAME filed as an ALIAS
                                                                             OF A ROLE NOUN
    "My sister works late."              -> (sister, sibling_of, user)  CORRECT

The second is THE HARD LINE inverted: a name is a MEMORY filed ON an entity; a role noun is a TYPE,
a PLACE, and a PLACE must never hold a MEMORY as its alias.

THE ROOT CAUSE IS SLICE-vs-RAIL, NOT A MISSING FEATURE. Three sites in ``linguistics.py`` each ask
"is this possessed head a PERSON ROLE, and which relation binds that person to the speaker?" and each
answered with a DIFFERENT, narrower slice of the SAME per-tenant ``linguistic_cues`` rail:

  * ``_chain_possessive``      -> ``_kinship_nouns()`` alone            -> ``owns`` for every non-kin role
  * the alias-only naming chain-> kinship u social_role u role_noun     -> membership only, no PERSON
                                                                          signal, so an UN-GROWN role
                                                                          noun leaked the alias edge
  * ``_chain_named_instance``  -> the COMPLETE ladder                   -> already correct

``_possessed_person_role_rel`` factors the complete ladder out of the site that had it right and the
first two now call it. UNIFORM MECHANISM, NOT UNIFORM REL_TYPE: the CLASS picks the relation
(kinship -> its mapped kin rel; social_role/role_noun -> their mapped rel; PERSON-typed but un-grown
-> ``related_to`` + a growth signal), and a SORTAL head ("my computer") resolves nothing and keeps the
ownership reading. No noun list, no rel literal.

GROUNDING: Loebner's sortal/relational noun split -- a RELATIONAL noun carries an implicit argument
slot the possessor fills, so "my sister"/"my colleague" express the noun's OWN inherent relation,
while a SORTAL noun has no such slot and its possessive is the free ownership reading.

NO DSN IS SET BY THESE TESTS ON PURPOSE: the cue maps then resolve to their in-code BOOTSTRAP floors,
so every expectation below is deterministic and independent of any tenant's grown rows.
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


def _model_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2023, 5, 30)


def _mod(rail=None):
    """Reload with the deployed flag state. ``rail=None`` leaves SPINE_POSSESSED_ROLE_RAIL UNSET so the
    CODE DEFAULT decides -- that is what ``test_rail_is_on_by_default`` pins, and why the ablation
    tests below must set it EXPLICITLY to "false" rather than relying on an unset var."""
    return _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm",
                   SPINE_NAMING_CHAIN="true", POSTGRES_DSN=None,
                   SPINE_POSSESSED_ROLE_RAIL=rail)


def _triples(facts):
    return sorted((f.subject, f.rel_type, f.object) for f in facts)


def _facts(text, rail=None):
    m = _mod(rail)
    return m, {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(text, _REF)}


# ── DEFAULT ─────────────────────────────────────────────────────────────────────────────────────
# Pinned SEPARATELY from every ablation below: a test that monkeypatches/sets the flag proves nothing
# about the value the code ships with, and a lane silently defaulting OFF is the "built and dark"
# failure this repo has shipped before.

def test_rail_is_on_by_default():
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm",
                SPINE_POSSESSED_ROLE_RAIL=None)
    assert m.SPINE_POSSESSED_ROLE_RAIL is True, \
        "SPINE_POSSESSED_ROLE_RAIL must default ON -- an unset var is not an OFF var, and a dark " \
        "lane is indistinguishable in the data from a lane that never ran"


# ── THE RESOLVER: one ladder, per-class rel, sortal returns None ─────────────────────────────────

@pytest.mark.parametrize("role,expected", [
    ("sister", "sibling_of"),     # kinship_noun cue class -> its mapped kin rel
    ("mother", "parent_of"),      # kinship_noun, a different mapping -> not one hardcoded rel
    ("friend", "friend_of"),      # social_role cue class -> the row's mapped rel VERBATIM
    ("manager", "manager_of"),    # role_noun cue class -> the role-DERIVED <role>_of relation
    ("boss", "boss_of"),          # role_noun, a different lemma -> derived, not a literal
])
def test_resolver_reads_the_relation_from_the_role_s_cue_class(role, expected):
    m = _mod()
    assert m._possessed_person_role_rel(role) == expected


def test_resolver_declines_a_sortal_head_so_ownership_is_untouched():
    # "my computer" may legitimately differ from "my colleague": a device IS possessed. The rail must
    # decline it, so the ownership leg is reached unchanged.
    m = _mod()
    for sortal in ("computer", "laptop", "hometown", "commute", "appointment"):
        assert m._possessed_person_role_rel(sortal) is None, sortal


def test_resolver_reads_only_the_cue_rail_and_never_an_entity_type():
    """A PERSON-tag degrade was built, measured over 1,200 sentences, and DELETED: it produced 14
    correct flips and 7 false ones because the entity typer assigns MORE THAN ONE label to the same
    surface ({'Person': ['IP'], 'Organization': ['IP']}) and the span-collapse keeps whichever arrives
    first. Same finding the SPINE_INSTANCE_PLACEMENT type gate was deleted over (0 true vetoes,
    9 false). This pins the resolver to the RAIL ALONE so the rung cannot be re-added silently.

    If this goes RED, an entity-type signal was re-introduced -- RE-MEASURE the false-positive rate on
    a corpus before keeping it; do not just widen the pin.
    """
    m = _mod()
    import inspect
    params = list(inspect.signature(m._possessed_person_role_rel).parameters)
    assert params == ["role_lemma"], \
        f"resolver must take the lemma alone; got {params}"
    # and the un-grown role declines rather than degrading on a guessed type.
    # ⚠️ EXHIBIT CHANGED 2026-08-27 (migration 272): this used to use "teammate", which is now a
    # SEEDED social_role — WordNet-derived, closed-class. A test whose exhibit is a word someone may
    # later seed pins the wrong thing; `mentor`/`landlord` are person roles that are deliberately in
    # NO cue class (open-class, grown per-tenant), which is the property under test.
    assert m._possessed_person_role_rel("mentor") is None
    assert m._possessed_person_role_rel("landlord") is None
    assert m._possessed_person_role_rel("dermatologist") is None


def test_declining_an_ungrown_role_still_lets_the_class_grow():
    # Declining is not dropping: the NAMED construction's own chain observes the role and queues it,
    # so the class still grows on the freq gate and then joins at step 2 here with no code edit.
    m = _mod()
    import inspect as _i
    body = _i.getsource(m.derive_sentence_facts)
    assert '_record_cue_candidate(type_head, "social_role")' in body, \
        "the named-instance chain must still queue an un-grown person role for growth"


# ── SITE A: _chain_possessive -- a possessed PERSON ROLE is never ``owns`` ───────────────────────

@pytest.mark.parametrize("text,role,rel", [
    ("My friend works late.", "friend", "friend_of"),      # social_role (bootstrap floor)
    ("My manager works late.", "manager", "manager_of"),   # role_noun
    ("My boss approved the budget.", "boss", "boss_of"),   # role_noun
])
def test_possessed_person_role_binds_its_class_relation_not_owns(text, role, rel):
    m = _mod()
    got = {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(text, _REF)}
    assert ("user", "owns", role) not in got, \
        f"a PERSON must never be filed as an owned object; got {sorted(got)}"
    assert (role, rel, "user") in got, sorted(got)


def test_possessed_person_role_uses_the_same_mechanism_as_kinship():
    # The owner's ruling: "'my colleague' should be grown the same way as any other subject". Same
    # direction (FILLER -> user), same shape -- only the rel_type differs, and it comes from the rail.
    m = _mod()
    kin = {(f.subject, f.rel_type, f.object)
           for f in m.derive_sentence_facts("My sister works late.", _REF)}
    soc = {(f.subject, f.rel_type, f.object)
           for f in m.derive_sentence_facts("My friend works late.", _REF)}
    assert ("sister", "sibling_of", "user") in kin, sorted(kin)
    assert ("friend", "friend_of", "user") in soc, sorted(soc)
    assert not any(r == "owns" for _, r, _ in soc), sorted(soc)


def test_site_a_ablation_kinship_only_gate_refiles_the_person_as_owned():
    # ABLATION: remove the rail at site A and the PERSON goes back to being an owned object.
    m = _mod("false")
    got = {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts("My friend works late.", _REF)}
    assert ("user", "owns", "friend") in got, \
        f"rail OFF must restore the pre-existing ownership reading byte-for-byte; got {sorted(got)}"


def test_sortal_possession_is_unchanged_by_the_rail():
    # The rail must not widen into the ownership lane. A device is possessed; keep it that way in
    # BOTH arms, so the change is provably scoped to person roles.
    for rail in ("true", "false"):
        _, got = _facts("My computer crashed.", rail)
        assert ("user", "owns", "computer") in got, (rail, sorted(got))


def test_negated_possessed_role_keeps_its_polarity():
    # The branch BODY is reused unchanged, so presupposition/negation handling must carry over --
    # a denied role tie must not silently re-affirm the relation.
    m = _mod()
    facts = m.derive_sentence_facts("My sister is not my friend.", _REF)
    denied = [f for f in facts if f.subject == "friend"]
    assert denied and all(f.negated for f in denied), _triples(facts)


# ── SITE C: analyze_possessive_predication -- a PERSON is not a PREFERENCE AXIS ─────────────────
# This seam takes a STRING and reparses internally, so no entity-typer label ever reaches it: only the
# cue-class steps (1 and 2) of the ladder can fire here, never the PERSON degrade. Pinned at the
# FUNCTION because its downstream consumer lives in main.py, which this change does not touch.

@pytest.mark.parametrize("text,possessed", [
    ("My friend is 34.", "friend"),     # social_role -> would become (user, friend, "34")
    ("My manager is 45.", "manager"),   # role_noun
    ("My boss is Amara.", "boss"),      # role_noun -> a NAME as the VALUE of a role-derived rel
])
def test_possessed_person_role_is_not_read_as_a_preference_axis(text, possessed):
    # A possessed PERSON has no preference AXIS to fill. Read as one, the ROLE NOUN canonicalizes into
    # a rel_type and somebody ELSE's scalar lands in the user's own slot. The guard already declined
    # for kinship ("My mother is 62." -> None); it must decline for the whole rail.
    m = _mod()
    assert m.analyze_possessive_predication(text) is None, \
        f"{text!r} must not yield a preference; got {m.analyze_possessive_predication(text)!r}"


def test_site_c_ablation_kinship_only_gate_mints_the_role_as_a_preference_axis():
    m = _mod("false")
    got = m.analyze_possessive_predication("My manager is 45.")
    assert got is not None and got.possessed == "manager" and got.value == "45", got


def test_site_c_residual_an_ungrown_role_still_leaks_here():
    """HONEST RESIDUAL, pinned so it is visible rather than assumed closed.

    This seam takes a STRING and reparses internally, so the entity typer NEVER reaches it and the
    ladder's PERSON degrade (step 3) is unreachable BY CONSTRUCTION. A person role that is in no cue
    class therefore still mints the preference reading here -- unlike sites A and B, which read
    ``token.ent_type_`` off the typed Doc and do degrade. Closing this needs the seam to accept a
    typed Doc -- its caller is in main.py and is out of scope here.

    ⚠️ EXHIBIT CHANGED 2026-08-27 (migration 272). This used "colleague", chosen because it was
    absent from the BOOTSTRAP social_role floor. `social_role` is now SEEDED (WordNet-derived, 45
    members incl. colleague/coworker/teammate/roommate), so colleague resolves and this seam now
    DECLINES for it -- a genuine improvement, and it narrows the residual to roles that really are
    un-grown. `mentor` is such a role: a person role in no cue class, grown per tenant.

    If this goes GREEN, the seam gained a type signal: delete this test and widen the pin above.
    """
    m = _mod()
    # ⚠️ RE-POINTED 2026-08-27, and the correction is the interesting part. I originally pinned
    # "a SEEDED social role no longer leaks" using `colleague` — which only held because the 45
    # role words had been duplicated INTO CODE (`_BOOTSTRAP_SOCIAL_ROLE_MAP`). That duplication
    # was a domain word zoo in the engine, forbidden by the subject-agnostic rule and already
    # pinned against by test_carved_class_bootstraps_are_empty. Removing it is correct, and it
    # makes this assertion false WHEN UNBOUND: the rail lives in the DB (migration 272, 48 rows
    # live on pre-prod and prod), so with no tenant bound `colleague` resolves to nothing and
    # leaks exactly like any un-grown role.
    # So the honest pin is the RESIDUAL below. The "closed half" belongs in a DSN-bound test
    # against the real rail, not here — this module runs unbound by design.
    # THE RESIDUAL, still open: a role in no cue class has no signal at this string-only seam.
    got = m.analyze_possessive_predication("My mentor is 34.")
    assert got is not None and got.possessed == "mentor", \
        "unexpected -- the un-grown-role leak appears closed; see the docstring"


@pytest.mark.parametrize("text,possessed", [
    ("My favorite colour is blue.", "favorite colour"),
    ("My blood type is O negative.", "blood type"),
    ("My hometown is Waterloo.", "hometown"),
])
def test_site_c_leaves_a_genuine_preference_axis_alone(text, possessed):
    # FIREWALL in BOTH arms: the widening must not eat the preference lane this seam exists for.
    for rail in ("true", "false"):
        m = _mod(rail)
        got = m.analyze_possessive_predication(text)
        assert got is not None and got.possessed == possessed, (rail, got)


# ── SITE B (NOT CHANGED — recorded because it was tried and measured) ───────────────────────────
# The alias-only naming chain already skips PERSON constructions on kinship u social_role u role_noun,
# which is EXACTLY what the resolver resolves, so routing that skip through the resolver is a no-op.
# The HARD LINE leak measured on a live tenant -- "My friend Devon works on X." ->
# (friend, also_known_as, devon), a NAME filed as an alias of a ROLE NOUN -- was a RAIL defect, not a
# deriver one: `linguistic_cue_overlay._resolve_keyed_map` REPLACED the bootstrap floor with the
# tenant map whenever the tenant held any row, so a tenant that grew ONE social_role row DISCARDED
# the seeded `friend -> friend_of` floor entirely.
# ✅ FIXED 2026-08-27 in the overlay: resolution is now floor UNION tenant rows, tenant winning on
# key collision, minus any cue the tenant has DEACTIVATED. Pinned in
# tests/test_linguistic_cue_overlay_union.py. These two tests pin the behaviour that IS in this
# file's control.

def test_person_role_in_a_grown_class_keeps_its_name_out_of_the_alias_layer():
    # THE HARD LINE: a role noun is a TYPE (a PLACE); it must not hold a NAME as its alias.
    m = _mod()
    got = {(f.subject, f.rel_type, f.object)
           for f in m.derive_sentence_facts("My friend Devon works on the payments team.", _REF)}
    assert not any(r == "also_known_as" and s == "friend" for s, r, o in got), sorted(got)
    assert ("devon", "instance_of", "friend") in got, sorted(got)


def test_a_non_person_named_instance_keeps_its_alias_edge():
    # FIREWALL: the site-B skip is scoped to PERSON roles. A device/animal type is NOT a person role,
    # so the alias-only chain keeps its reading -- the change must not silently delete capture.
    m = _mod()
    got = {(f.subject, f.rel_type, f.object)
           for f in m.derive_sentence_facts("My router Gateway1 crashed this morning.", _REF)}
    assert ("router", "also_known_as", "gateway1") in got, sorted(got)

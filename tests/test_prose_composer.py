"""Unit tests for the structure-driven object-clause composer + post-passes.

Covers the 5 object-shapes of `_compose_object_clause`, the `_extract_template_verb`
generic verb parse, the titlecase post-pass, the inverse-pair dedup post-pass, and
perspective ("you") preservation. Pure-function tests — no DB. Subject-agnostic: the
assertions never hinge on a domain kind; the same path renders pets, servers, people.
"""
import pytest

from src.api.main import (
    _compose_object_clause,
    _compose_inverse_anchor_clause,
    _extract_template_verb,
    _indefinite_article,
    _titlecase_display_slots,
    _dedup_inverse_pairs,
)

try:
    import inflect as _inflect  # noqa: F401
    _HAVE_INFLECT = True
except Exception:  # pragma: no cover
    _HAVE_INFLECT = False


# ── _indefinite_article (sound-aware a/an, networking acronym-soup) ────────────
# The INITIALISM rule is the closed 26-letter table — deterministic with OR without
# inflect, so these always hold. FaultLine's domain is acronym-soup: "an IP", not "a IP".
@pytest.mark.parametrize("word,art", [
    ("IP", "an"), ("SSD", "an"), ("FQDN", "an"), ("MRI", "an"), ("FBI", "an"),
    ("HTTP", "an"), ("SQL", "an"), ("SLA", "an"), ("LDAP", "an"), ("RJ45", "an"),
    ("A-record", "an"),                       # leading A-letter in an initialism → "an"
    ("URL", "a"), ("UID", "a"), ("UUID", "a"), ("UTF", "a"),  # U = "you" → consonant
    ("5G", "a"), ("802.1Q", "a"),             # digit-leading → "a"
    ("dog", "a"), ("son", "a"), ("server", "a"), ("daughter", "a"),  # plain consonant
    ("engineer", "an"), ("apple", "an"),      # plain vowel
])
def test_indefinite_article_initialism_and_plain(word, art):
    assert _indefinite_article(word) == art


@pytest.mark.skipif(not _HAVE_INFLECT, reason="inflect not installed — lexical exceptions need it")
@pytest.mark.parametrize("word,art", [
    ("hour", "an"), ("honest", "an"),         # silent-h
    ("university", "a"), ("unicorn", "a"), ("one", "a"),  # /juː/, /w/
    ("X-ray", "an"),                          # mixed-case word, /ɛks/
])
def test_indefinite_article_lexical_exceptions_via_inflect(word, art):
    assert _indefinite_article(word) == art


def test_indefinite_article_empty_safe():
    assert _indefinite_article(None) == "a"
    assert _indefinite_article("") == "a"
    assert _indefinite_article("   ") == "a"


# ── _extract_template_verb (generic verb parse, no per-rel hardcoding) ─────────
def test_verb_parse_has_pet_3p():
    assert _extract_template_verb("X has a pet that is Y", "X") == "has"


def test_verb_parse_has_pet_2p():
    assert _extract_template_verb("You have a pet that is Y", "You") == "have"


def test_verb_parse_owns_2p():
    assert _extract_template_verb("You own Y", "You") == "own"


def test_verb_parse_none_on_empty():
    assert _extract_template_verb(None, "X") is None
    assert _extract_template_verb("", "X") is None


# ── Shape 1: NAMED-INSTANCE ───────────────────────────────────────────────────
def test_named_instance_pet_you():
    out = _compose_object_clause(
        rel_type="has_pet",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"], "category": ""},
        subject_name="you", object_name="fraggle",
        object_id="11111111-1111-1111-1111-111111111111", object_is_uuid=True,
        named_type_name="dog",
        template="X has a pet that is Y", template_2p="You have a pet that is Y",
        subject_is_you=True,
    )
    assert out == "you have a dog named fraggle"


def test_named_instance_server_you():
    out = _compose_object_clause(
        rel_type="owns",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"], "category": ""},
        subject_name="you", object_name="apollo",
        object_id="22222222-2222-2222-2222-222222222222", object_is_uuid=True,
        named_type_name="server",
        template="X owns Y", template_2p="You own Y",
        subject_is_you=True,
    )
    assert out == "you own a server named apollo"


def test_named_instance_skips_type_equals_name():
    # Guard: no "a dog named dog".
    out = _compose_object_clause(
        rel_type="has_pet",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"], "category": ""},
        subject_name="you", object_name="dog",
        object_id="33333333-3333-3333-3333-333333333333", object_is_uuid=True,
        named_type_name="dog",
        template="X has a pet that is Y", template_2p="You have a pet that is Y",
        subject_is_you=True,
    )
    # type==name → no shape-1 compose → None (fall through to template path)
    assert out is None


# ── Shape 2: BARE-TYPE (instance_of / subclass_of) ────────────────────────────
def test_bare_type_instance_of_drops_parenthetical():
    out = _compose_object_clause(
        rel_type="instance_of",
        rel_meta={"is_hierarchy_rel": True, "tail_types": ["ANY"], "category": ""},
        subject_name="fraggle", object_name="dog",
        object_id="44444444-4444-4444-4444-444444444444", object_is_uuid=True,
        named_type_name=None,
        template="X is an instance of Y (type)", template_2p=None,
        subject_is_you=False,
    )
    assert out == "fraggle is a dog"


def test_bare_type_instance_of_article_an():
    out = _compose_object_clause(
        rel_type="instance_of",
        rel_meta={"is_hierarchy_rel": True, "tail_types": ["ANY"], "category": ""},
        subject_name="apollo", object_name="appliance",
        object_id="55555555-5555-5555-5555-555555555555", object_is_uuid=True,
        named_type_name=None,
        template="X is an instance of Y (type)", template_2p=None,
        subject_is_you=False,
    )
    assert out == "apollo is an appliance"


def test_bare_type_subclass_keeps_connector():
    out = _compose_object_clause(
        rel_type="subclass_of",
        rel_meta={"is_hierarchy_rel": True, "tail_types": ["ANY"], "category": ""},
        subject_name="dog", object_name="animal",
        object_id="66666666-6666-6666-6666-666666666666", object_is_uuid=True,
        named_type_name=None,
        template="X is a subclass of Y", template_2p=None,
        subject_is_you=False,
    )
    assert out == "dog is a subclass of animal"


# ── Shape 3: STATE (category == 'state') ──────────────────────────────────────
def test_state_third_person():
    out = _compose_object_clause(
        rel_type="has_state",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"], "category": "state"},
        subject_name="apollo", object_name="down",
        object_id="77777777-7777-7777-7777-777777777777", object_is_uuid=True,
        named_type_name=None,
        template="X is in state Y", template_2p="You are in state Y",
        subject_is_you=False,
    )
    assert out == "apollo is down"


def test_state_second_person():
    out = _compose_object_clause(
        rel_type="has_state",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"], "category": "state"},
        subject_name="you", object_name="tired",
        object_id="88888888-8888-8888-8888-888888888888", object_is_uuid=True,
        named_type_name=None,
        template="X is in state Y", template_2p="You are in state Y",
        subject_is_you=True,
    )
    assert out == "you are tired"


# ── Shape 4: SCALAR (tail_types == {SCALAR}) ──────────────────────────────────
def test_scalar_ip_third_person():
    out = _compose_object_clause(
        rel_type="has_ip",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["SCALAR"],
                  "category": "", "label": "IP"},
        subject_name="apollo", object_name="10.0.0.5",
        object_id="10.0.0.5", object_is_uuid=False,
        named_type_name=None,
        # has_ip.natural_language IS NULL in the real seed — that NULL is exactly what Shape 4
        # exists to rescue. The old fictional template accidentally pinned Shape 4 OVERRIDING a
        # curated one (the bug that shipped "Diane's born on is …"). Assertion unchanged.
        template=None, template_2p=None,
        subject_is_you=False,
    )
    assert out == "apollo's ip is 10.0.0.5"


def test_scalar_age_second_person():
    out = _compose_object_clause(
        rel_type="age",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["SCALAR"],
                  "category": "", "label": "Age"},
        subject_name="you", object_name="28",
        object_id="28", object_is_uuid=False,
        named_type_name=None,
        template="X is Y years old", template_2p=None,
        subject_is_you=True,
    )
    assert out == "your age is 28"


def test_scalar_strips_baked_has_predicate():
    """Shape 4 RESCUES a scalar rel with NO curated template, stripping the baked-in predicate.

    label "Has IP Address" must read "X's ip address is Y", not "X's has ip address is Y".

    ⚠️ RE-GATED (2026-07-13): this used to pass `template="X's ip is Y"` — a template that DOES
    NOT EXIST. The real seed has `has_ip.natural_language IS NULL`, which is precisely the case
    this shape exists to rescue. By supplying a fictional template it accidentally pinned Shape 4
    OVERRIDING a curated one — the very bug that shipped "Diane's born on is 1980-04-02" and
    "Diane's also known as is Di". A curated template now WINS in either person; Shape 4 only
    fires when there is nothing to fall back to. Passing template=None makes this test assert
    the real contract instead of a fictional one.
    """
    out = _compose_object_clause(
        rel_type="has_ip",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["SCALAR"],
                  "category": "", "label": "Has IP Address"},
        subject_name="apollo", object_name="10.0.0.5",
        object_id="10.0.0.5", object_is_uuid=False,
        named_type_name=None,
        template=None, template_2p=None,      # has_ip.natural_language IS NULL in the real seed
        subject_is_you=False,
    )
    assert out == "apollo's ip address is 10.0.0.5"


def test_curated_3p_template_is_never_clobbered():
    """REGRESSION GUARD (the 3p twin of the 2p clobber bug).

    A curated `natural_language` must survive for a THIRD-PARTY subject. Shape 4 used to
    overwrite it, shipping ungrammatical prose for every fact about anyone but the user:
        "Diane was born on 1980-04-02" -> "Diane's born on is 1980-04-02"
    CLAUDE.md's own worked example ("my mother's birthday?") lands on exactly this path.
    """
    out = _compose_object_clause(
        rel_type="born_on",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["SCALAR"],
                  "category": "", "label": "was born on"},
        subject_name="diane", object_name="1980-04-02",
        object_id="1980-04-02", object_is_uuid=False,
        named_type_name=None,
        template="X was born on Y", template_2p=None,
        subject_is_you=False,
    )
    assert out is None, \
        "a curated 3p template must WIN — Shape 4 has nothing to rescue and must fall through"


def test_scalar_value_verbatim_not_titlecased_in_compose():
    # Value preserved verbatim by the composer (titlecasing happens only on name slots).
    out = _compose_object_clause(
        rel_type="has_fqdn",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["SCALAR"],
                  "category": "", "label": "FQDN"},
        subject_name="nexus", object_name="host.example.com",
        object_id="host.example.com", object_is_uuid=False,
        named_type_name=None,
        template=None, template_2p=None,   # has_fqdn.natural_language IS NULL in the real seed
        subject_is_you=False,
    )
    assert out == "nexus's fqdn is host.example.com"


# ── Shape 5: ENTITY/DEFAULT → None (fall through to template) ──────────────────
def test_entity_default_falls_through():
    out = _compose_object_clause(
        rel_type="spouse",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"], "category": ""},
        subject_name="you", object_name="marla",
        object_id="99999999-9999-9999-9999-999999999999", object_is_uuid=True,
        named_type_name=None,  # no instance_of type → not a named-instance shape
        template="X and Y are spouses/partners", template_2p=None,
        subject_is_you=True,
    )
    assert out is None


def test_participated_in_not_hijacked():
    # participated_in carries its OWN occurrence/type append path; the composer must
    # not produce a named-instance clause for it (no named_type_name passed there in
    # the loop, and even with one the occurrence path owns the render). Here, default.
    out = _compose_object_clause(
        rel_type="participated_in",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"], "category": ""},
        subject_name="you", object_name="advanced python",
        object_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", object_is_uuid=True,
        named_type_name=None,
        template="X participated in Y", template_2p="You participated in Y",
        subject_is_you=True,
    )
    assert out is None


# ── SUBJECT-SIDE NAMED-INSTANCE flip (inverse-anchor composer) ────────────────
# THE BUG: (cyrus, child_of, you) was rendered "you are a son named Cyrus" (makes
# the USER the son). Fix: flip along inverse_rel_type → "you have a son named Cyrus".
# Subject-agnostic, ZERO kin literals — Type comes from the entity's instance_of.
def test_inverse_anchor_child_of_you():
    out = _compose_inverse_anchor_clause(
        rel_type="child_of",
        rel_meta={"inverse_rel_type": "parent_of", "is_symmetric": False},
        anchor_name="you", anchor_is_you=True,
        instance_name="cyrus", instance_type_name="son",
    )
    assert out == "you have a son named cyrus"


def test_inverse_anchor_daughter_article_unchanged():
    out = _compose_inverse_anchor_clause(
        rel_type="child_of",
        rel_meta={"inverse_rel_type": "parent_of", "is_symmetric": False},
        anchor_name="you", anchor_is_you=True,
        instance_name="gabriella", instance_type_name="daughter",
    )
    assert out == "you have a daughter named gabriella"


def test_inverse_anchor_non_kin_metadata_driven():
    # SUBJECT-AGNOSTIC: a non-kin inverse rel flips the SAME way — proof the flip is
    # metadata-driven (inverse_rel_type), not kin-coded. (X, created_by, you) →
    # you-created-X possessive named-instance reading.
    out = _compose_inverse_anchor_clause(
        rel_type="created_by",
        rel_meta={"inverse_rel_type": "creator_of", "is_symmetric": False},
        anchor_name="you", anchor_is_you=True,
        instance_name="atlas", instance_type_name="project",
    )
    assert out == "you have a project named atlas"


def test_inverse_anchor_no_inverse_metadata_returns_none():
    # No inverse in metadata → not flippable → None (leave render untouched).
    out = _compose_inverse_anchor_clause(
        rel_type="located_in",
        rel_meta={"inverse_rel_type": None, "is_symmetric": False},
        anchor_name="you", anchor_is_you=True,
        instance_name="toronto", instance_type_name="city",
    )
    assert out is None


def test_inverse_anchor_no_type_returns_none():
    # No instance_of TYPE → never fabricate "you have a named cyrus" → None.
    out = _compose_inverse_anchor_clause(
        rel_type="child_of",
        rel_meta={"inverse_rel_type": "parent_of", "is_symmetric": False},
        anchor_name="you", anchor_is_you=True,
        instance_name="cyrus", instance_type_name=None,
    )
    assert out is None


def test_inverse_anchor_symmetric_renders_relationship():
    # SYMMETRIC rel (spouse): no possessive reading → "<Name> is your <role>".
    out = _compose_inverse_anchor_clause(
        rel_type="spouse",
        rel_meta={"inverse_rel_type": "spouse", "is_symmetric": True, "label": "spouse"},
        anchor_name="you", anchor_is_you=True,
        instance_name="marla", instance_type_name=None,
    )
    assert out == "marla is your spouse"


def test_inverse_anchor_third_person_uses_has():
    # Non-"you" anchor uses "has" (subject-agnostic person agreement).
    out = _compose_inverse_anchor_clause(
        rel_type="child_of",
        rel_meta={"inverse_rel_type": "parent_of", "is_symmetric": False},
        anchor_name="diane", anchor_is_you=False,
        instance_name="cyrus", instance_type_name="son",
    )
    assert out == "diane has a son named cyrus"


# ── Shape 1 symmetric guard (object-side spouse softening) ────────────────────
def test_object_side_symmetric_typed_renders_relationship():
    # (user, spouse, marla) with marla typed "wife": NOT "you and a wife named
    # Marla" — render "Marla is your wife" (role = object's own type word).
    out = _compose_object_clause(
        rel_type="spouse",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"],
                  "category": "", "is_symmetric": True},
        subject_name="you", object_name="marla",
        object_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", object_is_uuid=True,
        named_type_name="wife",
        template="X and Y are spouses", template_2p="You and Y are spouses",
        subject_is_you=True,
    )
    assert out == "marla is your wife"


def test_object_side_copula_verb_becomes_possessive():
    # (you, parent_of, cyrus) — the walk presents the user-subject direction of the
    # stored (cyrus, child_of, you). parent_of's verb is the copula "are"; the
    # named-instance shape is POSSESSIVE → "you have a son named Cyrus", NOT
    # "you are a son named Cyrus" (which makes the USER the son). Metadata-driven,
    # ZERO kin literals — son comes from cyrus's instance_of.
    out = _compose_object_clause(
        rel_type="parent_of",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"],
                  "category": "", "is_symmetric": False, "inverse_rel_type": "child_of"},
        subject_name="you", object_name="cyrus",
        object_id="dddddddd-dddd-dddd-dddd-dddddddddddd", object_is_uuid=True,
        named_type_name="son",
        template="X is the parent of Y", template_2p="You are the parent of Y",
        subject_is_you=True,
    )
    assert out == "you have a son named cyrus"


def test_object_side_copula_third_person_uses_has():
    out = _compose_object_clause(
        rel_type="parent_of",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"],
                  "category": "", "is_symmetric": False, "inverse_rel_type": "child_of"},
        subject_name="diane", object_name="cyrus",
        object_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", object_is_uuid=True,
        named_type_name="son",
        template="X is the parent of Y", template_2p="You are the parent of Y",
        subject_is_you=False,
    )
    assert out == "diane has a son named cyrus"


def test_object_side_copula_vowel_article_an():
    # NON-KIN subject-agnostic: (you, manages, dana) with type "engineer" → vowel
    # article "an engineer", possessive verb from manages (non-copula kept verbatim).
    out = _compose_object_clause(
        rel_type="manages",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"],
                  "category": "", "is_symmetric": False, "inverse_rel_type": "managed_by"},
        subject_name="you", object_name="dana",
        object_id="ffffffff-ffff-ffff-ffff-ffffffffffff", object_is_uuid=True,
        named_type_name="engineer",
        template="X manages Y", template_2p="You manage Y",
        subject_is_you=True,
    )
    assert out == "you manage an engineer named dana"


def test_object_side_asymmetric_named_still_possessive():
    # NO REGRESSION: an ASYMMETRIC named-instance object stays possessive ("named").
    out = _compose_object_clause(
        rel_type="has_pet",
        rel_meta={"is_hierarchy_rel": False, "tail_types": ["ANY"],
                  "category": "", "is_symmetric": False},
        subject_name="you", object_name="fraggle",
        object_id="cccccccc-cccc-cccc-cccc-cccccccccccc", object_is_uuid=True,
        named_type_name="dog",
        template="X has a pet that is Y", template_2p="You have a pet that is Y",
        subject_is_you=True,
    )
    assert out == "you have a dog named fraggle"


# ── Post-pass (a): titlecase display slots ────────────────────────────────────
def test_titlecase_names_only():
    out = _titlecase_display_slots(
        "you have a dog named fraggle", ["fraggle"])
    assert out == "you have a dog named Fraggle"


def test_titlecase_leaves_you_and_value():
    # "you" never in name_slots; scalar value not in name_slots → untouched.
    out = _titlecase_display_slots("you have a cat named goose", ["goose"])
    assert out == "you have a cat named Goose"
    # multiple slots, longest-first (no partial corruption)
    out2 = _titlecase_display_slots(
        "sarah knows sarahjane", ["sarah", "sarahjane"])
    assert out2 == "Sarah knows Sarahjane"


def test_titlecase_skips_you_slot_if_passed():
    out = _titlecase_display_slots("you are tired", ["you"])
    assert out == "you are tired"


# ── Post-pass (b): inverse-pair dedup ─────────────────────────────────────────
def test_inverse_dedup_prefers_user_subject():
    overlay = {
        "child_of": {"inverse_rel_type": "parent_of"},
        "parent_of": {"inverse_rel_type": "child_of"},
    }
    rendered = [
        {"prose": "mother is the parent of you", "subj_id": "M", "obj_id": "U",
         "rel_type": "parent_of", "subject_is_you": False},
        {"prose": "you are the child of mother", "subj_id": "U", "obj_id": "M",
         "rel_type": "child_of", "subject_is_you": True},
    ]
    out = _dedup_inverse_pairs(rendered, overlay)
    assert out == ["you are the child of mother"]


def test_inverse_dedup_keeps_non_inverse():
    overlay = {"spouse": {"inverse_rel_type": None}}
    rendered = [
        {"prose": "you are married to marla", "subj_id": "U", "obj_id": "X",
         "rel_type": "spouse", "subject_is_you": True},
        {"prose": "you have a dog named fraggle", "subj_id": "U", "obj_id": "D",
         "rel_type": "has_pet", "subject_is_you": True},
    ]
    out = _dedup_inverse_pairs(rendered, overlay)
    assert out == ["you are married to marla", "you have a dog named fraggle"]


def test_inverse_dedup_distinct_pairs_not_collapsed():
    overlay = {
        "parent_of": {"inverse_rel_type": "child_of"},
        "child_of": {"inverse_rel_type": "parent_of"},
    }
    rendered = [
        {"prose": "you are the parent of sarah", "subj_id": "U", "obj_id": "S",
         "rel_type": "parent_of", "subject_is_you": True},
        {"prose": "you are the parent of tom", "subj_id": "U", "obj_id": "T",
         "rel_type": "parent_of", "subject_is_you": True},
    ]
    out = _dedup_inverse_pairs(rendered, overlay)
    assert len(out) == 2

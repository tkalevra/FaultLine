"""Regression gate for five capture defects the SPINE deriver showed on a live roster turn.

All five were reproduced against ``derive_sentence_facts`` (src/extraction/linguistics.py) with the
production spine flags ON (SENTENCE_PIPELINE / SPINE_NAMING_CHAIN), and each stored REAL NOISE in a
live memory. All inputs here are SUBJECT-AGNOSTIC (kin/pet roles drawn from the DB cue classes, names
are NON-personal reference data) — never a code literal.

── DEFECT 1 — an UNSPACED parenthetical GLUED the alias run onto the name ──────────────────────
"a son named Rowan(goes by Ro)" tokenized as the single PROPN ``Rowan(goes``, because a bracket is a
spaCy PREFIX/SUFFIX rule and fires only at a token EDGE — never in the middle of an alphanumeric run.
The malformed surface became THE NAME and collected kin + gender edges. FIX: ``_install_bracket_infix``
adds the missing INFIX rule, so the spaced and unspaced forms now tokenize (and parse) identically.

── DEFECT 2 — a PARENTHETICAL alias run was dropped AND minted a junk scalar ────────────────────
spaCy gives "(goes by Ro)" no subject of its own and re-attaches the bare alias VERB as the nsubj of
the following copula, so "My son Rowan (goes by Ro) is 12" derived ("goes", age, "12") — a fact about
a verb — while the alias itself was DROPPED (``_binding_nickname`` only read the relative-clause form
"who goes by Ro"). FIX: the nickname reader also accepts the LINEARLY-anchored parenthetical run
(alias vocabulary from the growable ``alias_predicate`` cue map), the measure chain rejects a
non-nominal subject, and it RECOVERS the real subject to the left of the bracket so the age lands.

── DEFECT 3 — the INVERSE copula minted a PHANTOM ROLE ENTITY ───────────────────────────────────
``_bound_name_for_type`` read only "my <role> is <Name>", never the everyday reverse "<Name> is my
<role>". So "Ines is my wife" bound NOTHING and the possessive chain minted (wife, spouse, user) — a
phantom "Wife" PERSON that went on to collect an instance_of, an owns, and a retraction's negative
fact, all conflicting with the real spouse. FIX: binding branch (4), gated on 1st-person possession.

── DEFECT 4 — an ATTRIBUTE-VALUE apposition was read as a TYPE↔NAME binding ─────────────────────
"Quinn, age 10, F." hangs the trailing code on "age" as an ``appos``, which read as 'a "type" (age)
named F' → an entity F filed as an instance of age. FIX: a type noun already SATURATED by its own
cardinal ``nummod`` cannot host a name; a single-character "name" is an initial, not a name; and the
appositive chain now stores the pair as the SCALAR it is instead of a phantom ``has_role``.

── DEFECT 5 — a COPULA TYPE-PREDICATE lost the type and minted a NOUN-AS-RELATION ───────────────
"My dog Bracken is a morkie" bound the breed to nothing, and the attribute-scalar chain read the
clause as a possessed-attribute literal → (user, "dog", "morkie"): the breed filed as the value of a
"dog" attribute ON THE USER, the named instance never typed. FIX: binding branch (5) binds the
determiner-introduced predicate nominal to the subject NP's name, and the attribute-scalar chain
steps aside for exactly that shape — NARROWLY, so a real possessed scalar ("my address is 123 …",
covered below) is still captured.
"""
import datetime

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2023, 6, 1)


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _surfaces(triples):
    return [s for t in triples for s in (t[0], t[2])]


# ── DEFECT 1 — the unspaced parenthetical must not glue onto the name ───────────────────────

@pytest.mark.parametrize("text", [
    "I have a son named Rowan(goes by Ro).",   # UNSPACED — the live repro shape
    "I have a son named Rowan (goes by Ro).",  # spaced — must derive the same thing
])
def test_parenthetical_never_glues_onto_the_name(text):
    triples = _triples(m.derive_sentence_facts(text, _REF))
    # the clean name is bound, and NO surface anywhere carries the bracket or the alias verb
    assert ("rowan", "child_of", "user") in triples, triples
    for surface in _surfaces(triples):
        assert "(" not in surface and ")" not in surface, triples
        assert "goes" not in surface.split(), triples


def test_unspaced_and_spaced_parenthetical_agree():
    """The bracket is orthography, not content — both spellings must derive the SAME fact set."""
    unspaced = sorted(_triples(m.derive_sentence_facts(
        "I have a son named Rowan(goes by Ro).", _REF)))
    spaced = sorted(_triples(m.derive_sentence_facts(
        "I have a son named Rowan (goes by Ro).", _REF)))
    assert unspaced == spaced, (unspaced, spaced)


# ── DEFECT 2 — the parenthetical alias is captured; no fact is ever ABOUT the alias verb ────

def test_parenthetical_alias_is_captured_as_an_alias():
    triples = _triples(m.derive_sentence_facts(
        "I have a son named Rowan (goes by Ro).", _REF))
    assert ("rowan", "also_known_as", "Ro") in triples or \
           ("rowan", "also_known_as", "ro") in triples, triples


def test_a_nickname_is_not_a_pronoun_antecedent():
    """An alias is a second name, not a new referent — recency must not hand it the pronoun.

    Only observable once the bracket infix splits the run into real tokens: the nickname then
    becomes the NEAREST preceding PROPN, and the age landed on "ro" instead of "rowan".
    """
    triples = _triples(m.derive_sentence_facts(
        "I have a son named Rowan(goes by Ro), he is 12.", _REF))
    assert ("rowan", "age", "12") in triples, triples
    assert not [t for t in triples if t[0] == "ro" and t[1] == "age"], triples


def test_parenthetical_run_never_becomes_the_scalar_subject():
    """("goes", age, "12") — a fact ABOUT A VERB — was the live noise; the age must land on the son."""
    triples = _triples(m.derive_sentence_facts("My son Rowan (goes by Ro) is 12.", _REF))
    assert ("rowan", "age", "12") in triples, triples
    assert not [t for t in triples if t[0] == "goes"], triples


# ── DEFECT 3 — "<Name> is my <role>" binds the NAME, never a phantom role entity ────────────

@pytest.mark.parametrize("text,name,kin", [
    ("Ines is my wife.", "ines", "spouse"),
    ("Dana is my sister.", "dana", "sibling_of"),
    ("Priya is my mother.", "priya", "parent_of"),
])
def test_inverse_copula_binds_the_name_not_the_role(text, name, kin):
    triples = _triples(m.derive_sentence_facts(text, _REF))
    assert (name, kin, "user") in triples, triples
    # THE PHANTOM: the bare role noun must never be an entity in its own right
    role = text.rsplit(" my ", 1)[1].rstrip(".").strip()
    assert not [t for t in triples if t[0] == role], triples


def test_inverse_copula_still_ignores_an_unpossessed_predicate_nominal():
    """Only the 1st-person POSSESSED role frame binds — an unpossessed role is owned elsewhere."""
    triples = _triples(m.derive_sentence_facts("Ines is a doctor.", _REF))
    assert not [t for t in triples if t[1] == "spouse"], triples


# ── DEFECT 4 — an attribute-value apposition is a SCALAR, not a type or a role ──────────────

def test_attribute_value_apposition_stores_a_scalar():
    triples = _triples(m.derive_sentence_facts("Quinn, age 10, F.", _REF))
    assert ("quinn", "age", "10") in triples, triples
    # no phantom: no entity named for the single-letter code, and "age" is never a type or a role
    assert not [t for t in triples if t[1] == "instance_of" and t[2] == "age"], triples
    assert not [t for t in triples if t[1] == "has_role" and t[2] == "age"], triples
    for surface in _surfaces(triples):
        assert len(surface.strip().rstrip(".")) > 1, triples


def test_a_real_role_apposition_still_reads_as_a_role():
    """The cardinal is the discriminator — a role appositive with no value keeps its has_role."""
    triples = _triples(m.derive_sentence_facts("Quinn, a real estate agent, called.", _REF))
    assert [t for t in triples if t[1] == "has_role"], triples


# ── DEFECT 5 — a copula type-predicate types the NAMED INSTANCE, not the user ───────────────

def test_copula_type_predicate_binds_the_breed_to_the_named_instance():
    triples = _triples(m.derive_sentence_facts("My dog Bracken is a morkie.", _REF))
    assert ("bracken", "instance_of", "morkie") in triples, triples
    # the noun-as-relation junk: the role noun must never become a RELATION on the user
    assert not [t for t in triples if t[0] == "user" and t[1] == "dog"], triples


def test_possessed_attribute_scalar_is_still_captured():
    """The narrow guard's sentinel: suppressing on the binding ALONE dropped this real scalar."""
    triples = _triples(m.derive_sentence_facts(
        "My address is 123 Main Street, Riverton, Ontario.", _REF))
    assert [t for t in triples
            if t[0] == "user" and t[1] == "address" and "main street" in t[2].lower()], triples


# ── The whole roster turn, end to end — every member typed, no noise ────────────────────────

def test_named_roster_binds_each_member_to_its_own_type():
    """The list shape that mis-filed one member's name under another member's type."""
    triples = _triples(m.derive_sentence_facts(
        "We have a morkie dog named Bracken, a cat named Pepper, "
        "and a corn snake named Juniper.", _REF))
    assert ("bracken", "instance_of", "morkie dog") in triples, triples
    assert ("pepper", "instance_of", "cat") in triples, triples
    assert ("juniper", "instance_of", "corn snake") in triples, triples
    # no cross-binding: no member is ever filed at another member's type
    assert not [t for t in triples if t[0] == "bracken" and t[2] in ("cat", "corn snake")], triples

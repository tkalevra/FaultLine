"""Pins for the THIRD-PARTY NAME CORRECTION defect — a name landing on the SPEAKER.

MEASURED DEFECT (fresh tenant, MCP door, at the parent commit):
    1. "My sister Mara lives in Toronto."          -> (user, sibling_of, mara); Mara is her own
       entity, preferred alias "mara", holding lives_in / instance_of / has_gender.
    2. "Actually my sister's name is Renata, not Mara."  -> status:"corrected", and the write
       landed on the WRONG PERSON. Two observed variants, both reported success:
         * `name = Marlow` written as a SCALAR on the USER'S OWN entity (a name the speaker never
           claimed — THE HARD LINE: a memory filed at the wrong person), or
         * old_rel_type pinned to `sibling_of`, so the kin edge was SUPERSEDED and a brand-new
           EMPTY "renata" entity was minted as the sibling — the sister keeps her old name, loses
           her kinship, and her city/gender/type facts are orphaned.
       Recall answered "Mara" either way.

ROOT CAUSE, in two layers, and BOTH are pinned here:

  (1) THE DERIVER NEVER SAW THE CONSTRUCTION. The ingest spine already owns this exact repair
      (``_chain_genitive_name`` + ``SPINE_NAME_REPAIR``): it anchors on the REJECTED alternative,
      re-lands the kin edge on that SAME entity, and rides the label change on ``pref_name``. It
      declined silently because ``en_core_web_sm`` mis-parses the OOV name in TWO slots — the
      asserted complement ("… name is Renata" -> acomp/ADJ instead of attr/PROPN) and the rejected
      alternative ("… Renata, not Marlow" -> amod/ADJ instead of appos). The mis-tag tracks the NAME
      TOKEN, not the frame: "my {sister,mother,dog,colleague}'s name is Renata" is acomp in all four
      while Diana/Renata/Rex/Kate/Bob/Sarah are attr in all four.

  (2) THE CORRECTION ENDPOINT NEVER ASKED THE DERIVER. It ran the LLM extraction, which flattens the
      1st-person possessive to subject_name="user", and its value-entity guard then REJECTED the one
      correct anchor it had found ("mara") on the premise that "a correction's subject can never be
      its own old/new value". That premise holds for a VALUE correction and INVERTS for a NAME
      correction, where the old value IS the referent's current label.

NEGATIVE CONTROLS ARE THE POINT OF THE TITLE-CASE GATE: "my sister's name is short/unusual" must
NOT be read as a name. If those go red, the orthographic gate was widened — RE-MEASURE, do not
relax the assertion.

PURE tests — no DB, no network, no GLiNER2, no LLM (a fake cursor stands in for the tenant).
Run: python3 tools/fltest.py --bug NAMEFIX --test tests/test_name_repair_correction.py
     (tests/ is gitignored -> git add -f)
"""
import datetime
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402
import src.api.main as m  # noqa: E402


_REF = datetime.date(2026, 8, 25)


def _repair_edge(sentence):
    """The single LABEL-CHANGE edge the deriver flagged, or None."""
    facts = derive_sentence_facts(sentence, _REF) or []
    return next((f for f in facts if getattr(f, "preferred_label", False)), None)


# ── (1) THE DERIVER: the repair must fire, and must anchor on the EXISTING entity ────────────────

@pytest.mark.parametrize("sentence,old_name,new_name", [
    # acomp/ADJ asserted complement (the measured sister failure — derived ZERO edges before).
    ("Actually my sister's name is Renata, not Mara.", "mara", "renata"),
    # amod/ADJ rejected alternative (the measured colleague failure — anchored on the NEW name,
    # minting a rival entity, before).
    ("My colleague's name is actually Renata, not Marlow.", "marlow", "renata"),
    # attr/appos — the shape that already worked; it must keep working.
    ("Actually my mother's name is Diana, not Diane.", "diane", "diana"),
])
def test_name_repair_anchors_on_the_rejected_label_not_the_new_one(sentence, old_name, new_name):
    edge = _repair_edge(sentence)
    assert edge is not None, (
        f"no label-change edge derived for {sentence!r} — the naming chain declined, which is "
        f"exactly the silent failure this pin exists for")
    # THE HARD LINE: the SUBJECT is the person being renamed (already stored under the old label),
    # never the speaker and never the newly asserted name.
    assert edge.subject == old_name, (
        f"repair anchored on {edge.subject!r}; anchoring anywhere but the rejected label either "
        f"renames the SPEAKER or mints a rival referent")
    assert edge.subject != "user"
    assert edge.object == new_name


def test_the_repair_relands_the_relation_on_the_same_entity_it_renames():
    """The kin/relational edge must re-land on the ANCHOR — otherwise the correction replaces the
    person instead of relabelling them (the measured 'sister loses her kinship' variant)."""
    facts = derive_sentence_facts(
        "Actually my sister's name is Renata, not Mara.", _REF) or []
    subjects = {f.subject for f in facts}
    assert subjects == {"mara"}, (
        f"every edge must hang off the one anchor; got {sorted(subjects)} — a second subject means "
        f"a second person was minted")
    rels = {f.rel_type for f in facts}
    assert "pref_name" in rels and "sibling_of" in rels


# ── NEGATIVE CONTROLS — a genuine adjectival predicate is NOT a name ─────────────────────────────

@pytest.mark.parametrize("sentence", [
    "My sister's name is short.",
    "My sister's name is unusual.",
    "My sister's name is hard to spell.",
])
def test_a_lowercase_predicate_adjective_is_never_read_as_a_name(sentence):
    """The acomp admission is gated on the orthographic proper-name marker (title case, not
    sentence-initial). If this goes red the gate was widened — RE-MEASURE before relaxing it."""
    assert _repair_edge(sentence) is None
    facts = derive_sentence_facts(sentence, _REF) or []
    assert not any(f.rel_type in ("pref_name", "also_known_as") for f in facts), (
        "an adjectival predicate was bound as a label")


def test_plain_predicate_negation_is_not_a_repair():
    """"her name is not Diane" rejects without offering an alternative — there is nothing to rename."""
    assert _repair_edge("My sister's name is not Mara.") is None


# ── (2) THE CORRECTION-ENDPOINT INTERCEPT ───────────────────────────────────────────────────────

class _FakeCur:
    """Scriptable cursor for the detector's lookup-only probes.

    ``row`` is the anchor the alias lookup returns (``None`` = the rejected label names nothing).
    ``rivals`` supplies MORE than one entity holding that label, and ``role_holders`` says which of
    them the graph actually records the deriver's role edge for — the discriminator the detector
    uses instead of an arbitrary ``LIMIT 1`` pick over a non-unique column.
    """

    def __init__(self, row, rivals=None, role_holders=()):
        self._row = row
        self._rivals = rivals
        self._role_holders = set(role_holders)
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = (sql, params)

    def fetchone(self):
        sql, params = self._last or ("", None)
        if "FROM facts" in sql:                       # the role-edge existence probe
            cand = params[1] if params else None
            return (1,) if cand in self._role_holders else None
        return self._row

    def fetchall(self):
        sql, params = self._last or ("", None)
        if "entity_aliases" not in sql:
            return []
        if self._rivals is not None and params and params[0] == "mara":
            return [(e,) for e in self._rivals]
        if self._rivals is not None and params and params[0] != "mara":
            return [("00000000-0000-0000-0000-0000000000ff",)]   # the possessor surface
        return [self._row] if self._row else []


def test_intercept_resolves_the_anchor_and_reads_the_rel_off_the_edge():
    cur = _FakeCur(("11111111-2222-3333-4444-555555555555",))
    got = m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", cur)
    assert got is not None
    assert got["entity_id"] == "11111111-2222-3333-4444-555555555555"
    assert got["old_name"] == "mara" and got["new_name"] == "renata"
    # The rel_type is read off the deriver's own edge — the intercept names no rel_type itself.
    assert got["rel_type"] == "pref_name"
    # It must have looked the anchor up by the REJECTED label, not by the new one.
    assert any(p == ("mara",) for _sql, p in cur.executed), cur.executed


def test_intercept_declines_rather_than_minting_a_referent():
    """LOOKUP-ONLY: no stored entity under the rejected label -> decline to the LLM path. A
    correction must never mint the person it is correcting."""
    assert m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", _FakeCur(None)) is None


@pytest.mark.parametrize("text", [
    "My favorite color is red, not blue.",     # a VALUE correction — the guard's original premise
    "I moved to Riverton, not Toronto.",
    "",
])
def test_intercept_declines_on_anything_that_is_not_a_name_repair(text):
    assert m._detect_name_repair_correction(text, _FakeCur(("deadbeef",))) is None


def test_intercept_is_fail_safe_when_the_lookup_raises():
    class _Boom(_FakeCur):
        def execute(self, sql, params=None):
            raise RuntimeError("db gone")

    assert m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", _Boom(None)) is None


# ── SUBJECT-AGNOSTICISM, DEMONSTRATED (not merely asserted) ──────────────────────────────────────
# A suite that only exercises the defect's own lexicon (sister/Mara) passes a fix that hardcodes
# that lexicon. These drive the SAME construction through roles from DIFFERENT cue classes
# (kinship_noun, relational/role nouns, and a non-person possessor) with DIFFERENT names, so a
# lexical patch cannot make them green. The added code carries no role, name, or rel_type literal —
# the rel is read off the deriver's edge and the roles come from the DB-grown cue classes.

@pytest.mark.parametrize("role", ["brother", "father", "aunt", "husband", "dog", "manager"])
def test_the_repair_is_keyed_on_grammar_not_on_the_role_word(role):
    edge = _repair_edge(f"Actually my {role}'s name is Renata, not Kestrel.")
    assert edge is not None, f"role {role!r} lost the construction"
    assert edge.subject == "kestrel" and edge.object == "renata"


@pytest.mark.parametrize("old_name,new_name", [
    ("Diane", "Diana"), ("Zebediah", "Zeb"), ("Fluff", "Fluffy"), ("Rob", "Bob"),
])
def test_the_repair_is_keyed_on_grammar_not_on_the_name(old_name, new_name):
    edge = _repair_edge(f"Actually my sister's name is {new_name}, not {old_name}.")
    assert edge is not None
    assert edge.subject == old_name.lower() and edge.object == new_name.lower()


# ── NEGATIVE CONTROLS THAT CAN ACTUALLY FIRE (round-18 critic) ────────────────────────────────
# ⚠️ THE ORIGINAL CONTROLS FOR THE ORTHOGRAPHIC GATE WERE VACUOUS, AND THAT IS WHY A REGRESSION
# SHIPPED. They were `short` / `unusual` / `hard` — all LOWER-CASE, i.e. members of the rejected
# class that LACK the discriminating feature. A negative control for a surface-feature guard must
# contain members of the rejected class that CARRY the feature, or it cannot go red no matter how
# wide the guard becomes.
#
# The feature is title case, and English PROPER ADJECTIVES are an open, productive, obligatorily
# title-cased class sitting in exactly this slot. Gated on orthography alone, "My sister's name is
# Irish." minted A SIBLING NAMED IRISH — 25/25 of these minted a person, against 4/25 before the
# arm existed. The arm is now additionally gated on the CORRECTIVE frame ("…, not X"), which is
# grammatical evidence that the slot holds a name, and which a plain assertion does not carry.

_PROPER_ADJECTIVES = ["Irish", "French", "German", "Biblical", "Victorian", "Shakespearean",
                      "Nordic", "Hebrew", "Greek", "Roman", "Celtic", "Arabic", "Spanish",
                      "Italian", "Japanese", "Portuguese", "Slavic", "Georgian", "Scottish"]


@pytest.mark.parametrize("adjective", _PROPER_ADJECTIVES)
def test_a_proper_adjective_is_never_minted_as_a_person(adjective):
    """A title-cased PROPER ADJECTIVE in the name slot must not become a relative.

    These carry the guard's discriminating feature (title case) while belonging to the class it
    must reject — the property the original lower-case controls could not test. If this goes red,
    the orthographic arm has been widened past the corrective frame again."""
    facts = derive_sentence_facts(f"My sister's name is {adjective}.", _REF) or []
    rows = [(f.subject, f.rel_type, f.object) for f in facts]
    kin = [r for r in rows if r[1] in ("sibling_of", "child_of", "parent_of")]
    assert not kin, (f"proper adjective {adjective!r} minted a person", rows)


def test_the_corrective_frame_is_what_licenses_the_mistag_recovery():
    """The paired positive: the SAME orthographic shape IS admitted inside a correction, because a
    rejected alternative is evidence the slot holds a name. Without this the test above is
    satisfiable by disabling the arm entirely."""
    edge = _repair_edge("Actually my sister's name is Renata, not Mara.")
    assert edge is not None and edge.object == "renata", edge


# ── THE ANCHOR IS NOT A COIN FLIP (defect: the correction landed on the wrong human) ─────────────
# ``entity_aliases`` is UNIQUE on ``(entity_id, alias)``, NOT on ``alias`` — two people may hold the
# same label — so the old ``ORDER BY is_preferred DESC LIMIT 1`` anchor lookup was an arbitrary pick
# dressed as a lookup, AND the pipeline manufactures the tie it then resolves arbitrarily: the same
# turn's spine ingest flips the new label preferred on the right person first, demoting the rejected
# label there and leaving every rival row ``is_preferred = false``. Measured 2/2 on fresh tenants: a
# sister and an accountant both aliased ``wrenna`` → the correction wrote ``thorne`` onto the
# ACCOUNTANT and left the sister with no preferred alias at all.

_SISTER = "aaaaaaaa-0000-0000-0000-00000000aaaa"
_RIVAL = "bbbbbbbb-0000-0000-0000-00000000bbbb"


def test_rival_referents_are_separated_by_the_role_edge_the_deriver_already_emitted():
    """Two entities hold the rejected label; only one holds the kin relation the SAME turn derived.
    The detector must land on that one — the discriminator is already in the fact stream and must
    not be thrown away for a row-order guess."""
    cur = _FakeCur(None, rivals=[_RIVAL, _SISTER], role_holders={_SISTER})
    got = m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", cur,
        "00000000-0000-0000-0000-0000000000ff")
    assert got is not None and got["entity_id"] == _SISTER, (got, cur.executed)


def test_rival_referents_the_role_edge_cannot_separate_are_REFUSED_not_guessed():
    """When the role probe leaves 0 or >1 survivors the detector declines. An arbitrary pick writes
    one real person's name onto another — losing the repair is the recoverable failure."""
    for holders in ((), {_RIVAL, _SISTER}):
        cur = _FakeCur(None, rivals=[_RIVAL, _SISTER], role_holders=holders)
        assert m._detect_name_repair_correction(
            "Actually my sister's name is Renata, not Mara.", cur,
            "00000000-0000-0000-0000-0000000000ff") is None, holders


def test_a_single_holder_of_the_label_still_resolves_without_any_probe():
    """The unambiguous case is unchanged — no tie, no discriminator needed."""
    cur = _FakeCur(None, rivals=[_SISTER], role_holders=())
    got = m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", cur, "u")
    assert got is not None and got["entity_id"] == _SISTER


# ── THE SPEAKER IS NEVER RENAMED BY A THIRD-PARTY NAME CORRECTION ────────────────────────────────
# "actually my sister's name is thorne, not wrenna." (lower case) defeats the complement scan, the
# deriver and the intercept both decline, and the LLM path then wrote ``thorne`` as the PREFERRED
# alias of the USER'S OWN entity. The FRAME survives the mis-tag even when the name does not, so
# the frame alone is enough to refuse the speaker-rename — no widening of the name admission, which
# was measured (above) to cost fabricated relatives.
#
# ⚠️ A GUARD KEYED ON ONE SURFACE REALIZATION GUARDS ONE PHRASING. The first cut required a copular
# `be` whose nsubj lemma was literally "name" and whose `poss` child was NOUN/PROPN — and returned NO
# OPINION for every other ordinary phrasing of the same construction. Each one was then measured
# CORRUPTING REAL ROWS on a live stack (fresh tenants, DB-read, repeated — the corruption is
# nondeterministic because it rides the correction LLM's subject guess):
#     (a) "Actually her name is Thorne, not Wrenna."             2/10 runs wrote `thorne` as the
#                                                                PREFERRED alias of the USER'S entity
#     (b) "Actually the name of my sister is Thorne, not Wrenna." instance_of|USER|thorne minted
#     (c) "Actually my sister goes by Thorne, not Wrenna."        sibling_of|user|wrenna SUPERSEDED,
#                                                                replaced by an edge to a new EMPTY
#                                                                entity — the sister lost her kinship
#     (d) "My sister's nickname is Thorne, not Wrenna."           naming noun was hardcoded to "name"
# The profile below is asked about ALL FIVE realizations, and the refusal is asked about each of the
# outcomes. The guard's REFUSAL, not the profile's opinion, is what protects a row — so the
# refusal-level tests drive the very predicate main.correct_fact evaluates.


SPEAKER = "aaaaaaaa-0000-0000-0000-000000000001"
SISTER = "bbbbbbbb-0000-0000-0000-000000000002"
ACCOUNTANT = "cccccccc-0000-0000-0000-000000000003"
PET_DOG = "dddddddd-0000-0000-0000-000000000004"
PET_CAT = "eeeeeeee-0000-0000-0000-000000000005"
TYPE_DOG = "ffffffff-0000-0000-0000-000000000006"


class _AliasCur:
    """The tenant's ``entity_aliases`` as a dict ``{alias: [entity_id, ...]}``.

    Lookup-only, exactly as the guard uses it. ``None`` for the registry means "raises", which pins
    the fail-safe."""

    def __init__(self, registry):
        self._registry = registry
        self._rows = []

    def execute(self, sql, params=None):
        if self._registry is None:
            raise RuntimeError("registry unreadable")
        assert "entity_aliases" in sql, sql
        wanted = list(params[0]) if params else []
        self._rows = [(a, e) for a in wanted for e in self._registry.get(a, ())]

    def fetchall(self):
        return self._rows


def _refuses(text, value, target=SPEAKER, registry=None, user_id=SPEAKER,
             db=None, referent_probed=False):
    """THE REAL PREDICATE ``main.correct_fact`` applies at the referent-misbind refusal.

    This calls ``main._name_correction_misbinds_referent`` itself rather than re-deriving it, so a
    pin cannot drift away from the branch it claims to guard (a re-implemented helper is exactly how
    a suite ends up green over a changed seam).

    DEFAULTS ARE THE ORIGINAL DEFECT: the write is about to land on the SPEAKER and the tenant's
    alias registry is empty, so nothing identifies the speaker as the clause's bearer.
    """
    return m._name_correction_misbinds_referent(
        _AliasCur({} if registry is None else registry), text, target, value, user_id,
        db=db, referent_probed=referent_probed) is not None


@pytest.mark.parametrize("text,value", [
    # (1) GENITIVE possessor — the one realization the first cut covered
    ("actually my sister's name is thorne, not wrenna.", "thorne"),   # measured defect, lower case
    ("Actually my sister's name is Thorne, not Wrenna.", "thorne"),   # same frame, title case
    ("John's name is actually Jonathan.", "jonathan"),                # third party by PROPER NAME
    # (2) APOSTROPHE-STRIPPED genitive — what the LLM atomizer emits routinely
    ("my sisters name is thorne, not wrenna.", "thorne"),
    # (3) PRONOMINAL possessor — Person=3 grammatically EXCLUDES the speaker
    ("Actually her name is Thorne, not Wrenna.", "thorne"),
    ("Actually his name is Thorne, not Wrenna.", "thorne"),
    ("Actually their name is Thorne, not Wrenna.", "thorne"),
    # (4) OF-GENITIVE — the bearer is the pobj, NOT the `my` inside it
    ("Actually the name of my sister is Thorne, not Wrenna.", "thorne"),
    # (5) NAMING PREDICATE — phrasal alias class and predicative naming class
    ("Actually my sister goes by Thorne, not Wrenna.", "thorne"),
    ("She is known as Thorne, not Wrenna.", "thorne"),
    ("My sister is called Thorne, not Wrenna.", "thorne"),
    ("My sister prefers to be called Thorne.", "thorne"),
    # NAMING NOUN beyond the literal "name" — the DB-grown naming_noun cue class
    ("My sister's nickname is Thorne, not Wrenna.", "thorne"),
    ("Her nickname is Thorne, not Wrenna.", "thorne"),
    ("My sister's alias is Thorne, not Wrenna.", "thorne"),
    ("My sister's surname is Thorne, not Wrenna.", "thorne"),
])
def test_a_third_party_name_is_refused_on_the_speaker(text, value):
    """Every ordinary realization of the naming frame must reach the refusal. A realization that
    does not is a live data-corruption path — see the measured outcomes in the block above."""
    assert _refuses(text, value), f"NOT refused — this write lands on the speaker: {text!r}"


# ── NEGATIVE CONTROLS THAT ACTUALLY REACH THE GUARDED BRANCH ─────────────────────────────────────
# A control that cannot satisfy the guard's admission condition pins nothing (that vacuity shipped
# twice on this lane). Every control below CARRIES a naming frame and a value drawn from it — the
# entire admission condition — and differs from the refused population only in WHOSE name it is.

@pytest.mark.parametrize("text,value", [
    ("Actually my name is Thorne, not Wrenna.", "thorne"),   # THE self-rename: must NOT be refused
    ("my name is Thorne, not Wrenna.", "thorne"),
    ("Actually my name is thorne, not wrenna.", "thorne"),   # lower case, same frame
    ("I go by Chris, not Christopher.", "chris"),            # naming predicate, 1st-person subject
    ("Actually our name is Thorne, not Wrenna.", "thorne"),  # Person=1 plural
])
def test_the_speaker_renaming_themselves_is_never_refused(text, value):
    """A first-person naming frame is the speaker's own truth. Refusing it would lose real user
    data on the single most ordinary phrasing there is."""
    assert not _refuses(text, value), f"OVER-REFUSED a legitimate self-rename: {text!r}"


@pytest.mark.parametrize("text,value", [
    ("Actually my sister's name is Thorne, and my age is 40.", "40"),
    ("Actually my sister's name is Thorne, and I live in Toronto.", "toronto"),
])
def test_an_unrelated_correction_riding_the_same_turn_is_untouched(text, value):
    """The refusal is scoped to the VALUE, not to the turn: a third-party naming clause elsewhere in
    the message must not block a correction about something else."""
    assert not _refuses(text, value)


# ── PER-CLAUSE, NOT PER-TURN ─────────────────────────────────────────────────────────────────────
# The first cut returned a turn-global ``speaker_names_self`` BOOLEAN, so ONE self-naming clause
# anywhere disabled the guard for the WHOLE turn — including the third-party clause sitting next to
# it. Both orders are pinned because they exercise different halves of the fix: with the self clause
# as the sentence ROOT, the third-party clause is a ``conj`` INSIDE the root's subtree, so without
# clause-local scoping the self set swallows the third party's name and the `not in own` test lets
# the write through.

@pytest.mark.parametrize("text,value,refused", [
    ("Actually my sister's name is Thorne, and my name is Chris.", "thorne", True),
    ("Actually my sister's name is Thorne, and my name is Chris.", "chris", False),
    ("Actually my name is Chris, and my sister's name is Thorne.", "thorne", True),
    ("Actually my name is Chris, and my sister's name is Thorne.", "chris", False),
    ("My name is Chris. My sister's name is Thorne.", "thorne", True),
    ("My name is Chris. My sister's name is Thorne.", "chris", False),
])
def test_a_self_naming_clause_does_not_disarm_the_third_party_clause(text, value, refused):
    assert _refuses(text, value) is refused, (text, value)


# ── THE GUARD KEEPS ITS HANDS OFF EVERYTHING ELSE ────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Actually my mother's age is 63, not 62.",   # not a naming frame at all
    "My favorite color is red, not blue.",
    "My sister goes to work.",                   # `go` WITHOUT its licensing `by` — motion, not alias
    "",
])
def test_a_non_naming_turn_yields_no_opinion(text):
    from src.extraction.linguistics import naming_frame_third_party_profile as nftp
    third, own = nftp(text)
    assert not third and not own, (third, own)


def test_a_noun_noun_compound_is_not_a_personal_naming_frame():
    """"My user name is bob." possesses the compound nominal "user name", not a person's name. The
    first cut fell through to the first-person test here and reported the SPEAKER NAMING THEMSELVES
    — a wrong answer where no opinion is the right one. Ablating the compound branch (letting it
    fall through to the ``poss`` test) turns ``own`` non-empty and reddens this."""
    from src.extraction.linguistics import naming_frame_third_party_profile as nftp
    third, own = nftp("My user name is bob.")
    assert not third and not own, (third, own)


@pytest.mark.parametrize("text,value,refused", [
    ("Actually my sister's name is Thorne Vale, not Wrenna Vale.", "Thorne Vale", True),
    ("Actually my name is Thorne Vale, not Wrenna Vale.", "Thorne Vale", False),
])
def test_a_multi_token_name_is_matched_token_by_token(text, value, refused):
    """``new_value`` is routinely a multi-token name, which no single-token membership test can
    match — the refusal would silently never fire on a full name. Ablating the split (comparing the
    whole string) reddens the third-party half of this pair."""
    assert _refuses(text, value) is refused, (text, value)


def test_a_stripped_genitive_with_an_uncued_role_still_reads_as_third_party():
    """The apostrophe-stripped genitive that the cue classes have not grown yet. The discriminator
    is MORPHOLOGICAL, not lexical: the stripped possessor is PLURAL-SHAPED with a SINGULAR lemma
    ("flatmates"->flatmate, tag NNS), which an ordinary attributive modifier is not ("user name" —
    NN; English attributive modifiers are canonically singular, Quirk et al. CGEL §17.104).
    Ablating that arm reddens this while leaving the cued roles green."""
    assert _refuses("my flatmates name is thorne, not wrenna.", "thorne")


# ── THE REMOVED acomp ARM, PINNED AS A KNOWN LIMITATION ───────────────────────────────────────
# `Marlow` is the token the (now removed) acomp mis-tag arm was fitted to: spaCy tags it ADJ in the
# naming slot, so it only ever repaired through a special-case arm whose measured net gain across a
# 225-name x 5-role sweep was that ONE name, while it lost Juniper/Rosalind/Ugo and risked minting
# relatives out of proper adjectives ("My sister's name is Irish." -> a sibling named Irish).
# Five discriminators were measured and all failed — title case, the corrective frame, complement
# POS, a neutral-frame re-parse, and the rejected alternative's POS — so the arm was removed rather
# than repeatedly re-gated. This xfail records the resulting hole HONESTLY instead of deleting the
# case and pretending the surface is covered. If it ever passes, a real discriminator was found:
# re-run the full name sweep AND the proper-adjective sweeps before believing it.

@pytest.mark.xfail(strict=True, reason=(
    "spaCy tags `Marlow` ADJ in the naming slot; the acomp recovery arm was removed after five "
    "measured discriminators all failed and its net gain proved to be this one token"))
def test_known_hole_a_mistagged_name_is_not_repaired():
    edge = _repair_edge("Actually my sister's name is Marlow, not Mara.")
    assert edge is not None and edge.object == "marlow"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# THE GUARD IS SUBJECT-AGNOSTIC: THE SAME CORRUPTION ON THREE KINDS OF REFERENT
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The refusal above used to admit on ``str(subject_uuid) == str(req.user_id)`` — it protected the
# SPEAKER and nobody else. MEMORY IS TIED TO THE SUBJECT, NOT TO THE USER: the speaker is one
# subject among many, so a sister's name landing on the ACCOUNTANT is the same falsehood as the
# same name landing on the user, and a correction about one PET must not land on the other. The
# predicate is now "the write must land on the entity the clause names, whoever or whatever that
# is", and these drive it through all three referent kinds with IDENTICAL expectations.

@pytest.mark.parametrize("kind,text,value,target,registry", [
    # (1) THE SPEAKER — the sub-case the old gate caught, must not regress.
    ("speaker", "Actually my sister's name is Thorne, not Wrenna.", "thorne",
     SPEAKER, {"wrenna": [SISTER]}),
    # (2) A NAMED THIRD PARTY — the accountant who shares the label. Identical corruption; the old
    #     gate let this one through because the wrong entity was not the person asking.
    ("third_party", "Actually my sister's name is Thorne, not Wrenna.", "thorne",
     ACCOUNTANT, {"wrenna": [SISTER, ACCOUNTANT]}),
    # (3) A NON-PERSON SUBJECT — two pets. "ducks" is as much a subject as a person.
    ("non_person", "Actually my dog's name is Rex, not Bandit.", "rex",
     PET_CAT, {"bandit": [PET_DOG, PET_CAT]}),
])
def test_the_same_corruption_is_refused_on_every_kind_of_referent(kind, text, value, target, registry):
    """One predicate, three referent kinds, one answer. If any of these goes green-to-red the guard
    has been re-keyed on a particular subject."""
    assert _refuses(text, value, target=target, registry=registry), kind


@pytest.mark.parametrize("kind,text,value,target,registry", [
    ("speaker", "Actually my name is Thorne, not Wrenna.", "thorne", SPEAKER, {"wrenna": [SPEAKER]}),
    ("third_party", "Actually my sister's name is Thorne, not Wrenna.", "thorne",
     SISTER, {"wrenna": [SISTER]}),
    ("non_person", "Actually my dog's name is Rex, not Bandit.", "rex",
     PET_DOG, {"bandit": [PET_DOG]}),
])
def test_the_write_still_lands_when_the_clause_names_that_referent(kind, text, value, target, registry):
    """THE OVER-REFUSAL CONTROL, and it reaches the whole admission condition: a naming clause IS
    present, the value IS one of its labels, and the only thing separating these rows from the
    refused ones above is that the target really is the referent the clause names. A guard that
    refuses everything would pass the trio above and fail here."""
    assert not _refuses(text, value, target=target, registry=registry), kind


def test_the_speakers_own_naming_clause_is_protected_the_same_way():
    """SYMMETRY, and it is the proof the first-person arm is a BEARER RESOLUTION and not a subject
    privilege: a FIRST-PERSON naming clause whose write would land on somebody else is refused by
    the same predicate that refuses a third party's name landing on the speaker."""
    assert _refuses("Actually my name is Thorne, not Wrenna.", "thorne",
                    target=SISTER, registry={"wrenna": [SISTER]})
    assert not _refuses("Actually my name is Thorne, not Wrenna.", "thorne", target=SPEAKER)


def test_rival_holders_of_the_label_are_refused_rather_than_guessed_between():
    """Two entities hold the rejected label, so no label identifies the referent — REFUSED for BOTH,
    including the one that happens to be right. That is deliberate: this guard sees only the
    outcome, and the rival case is exactly what ``_disambiguate_name_repair_anchor`` (which sees the
    deriver's role edge) is for. When THAT resolves, ``correct_fact`` skips this guard entirely.
    Ablating the ``len(_held) == 1`` uniqueness test greens the wrong half of this pair."""
    reg = {"wrenna": [SISTER, ACCOUNTANT]}
    assert _refuses("Actually my sister's name is Thorne, not Wrenna.", "thorne",
                    target=ACCOUNTANT, registry=reg)
    assert _refuses("Actually my sister's name is Thorne, not Wrenna.", "thorne",
                    target=SISTER, registry=reg)


def test_a_role_noun_is_not_a_label_for_the_referent():
    """MEASURED, not stylistic. On a live tenant, "I have a dog named Bandit." + "I have a cat named
    Bandit." leaves THREE entities aliased ``bandit`` — the pet and the two type nodes it is
    ``instance_of`` — while ``dog`` is held UNIQUELY by the type node. Admitting on the role noun
    would therefore admit the TYPE NODE as if it were the pet: an admission in the UNSAFE direction
    (a wrong admission WRITES; a wrong refusal only asks). Ablating the role-noun exclusion in
    ``_naming_frame_label_surfaces`` reddens this."""
    assert _refuses("Actually my dog's name is Rex, not Bandit.", "rex",
                    target=TYPE_DOG, registry={"bandit": [PET_DOG, TYPE_DOG], "dog": [TYPE_DOG]})


@pytest.mark.parametrize("text", [
    "Actually my sister's name is Thorne, not Wrenna. My accountant's name is Kai, not Mira.",
    "My accountant's name is Kai, not Mira. Actually my sister's name is Thorne, not Wrenna.",
])
def test_labels_are_clause_local_and_do_not_admit_across_clauses(text):
    """Two naming clauses, two referents. The accountant's label must not admit a write that
    belongs to the sister's clause. Ablating the clause-local label collection (unioning every
    third-party clause's labels into one set) greens this — ``mira`` would then admit the
    accountant for a value that is the SISTER'S name. Both orders, because clause-local scoping has
    been defeated by subtree containment before on this lane.

    ⚠️ KNOWN PARSER LIMIT, stated rather than papered over: as ONE sentence joined by "and"
    ('…not Wrenna, and my accountant\'s name is Kai, not Mira.') en_core_web_sm attaches BOTH names
    to the second clause's subtree and leaves the first clause with no complement at all, so no
    downstream logic can separate them. Two sentences (the ordinary way this is written) parse
    cleanly. Verified in the parser, not in the annotation scheme."""
    assert _refuses(text, "thorne", target=ACCOUNTANT,
                    registry={"mira": [ACCOUNTANT], "wrenna": [SISTER]})


def test_a_proper_name_possessor_identifies_its_own_bearer():
    """"John's name is actually Jonathan." carries no rejected alternative — the bearer's own
    current label IS the possessor, and it is a PROPER NAME (unlike a role noun, which may alias a
    type node). Ablating the PROPN-possessor arm reddens the second half."""
    assert _refuses("John's name is actually Jonathan.", "jonathan",
                    target=SPEAKER, registry={"john": [SISTER]})
    assert not _refuses("John's name is actually Jonathan.", "jonathan",
                        target=SISTER, registry={"john": [SISTER]})


def test_the_guard_is_fail_safe_when_the_alias_registry_raises():
    """A guard that cannot read the registry has NO OPINION on the LABEL question — it never refuses
    on an infrastructure error, because that would turn a DB blip into lost user corrections.

    ⚠️ THE TARGET HERE IS **NOT** THE SPEAKER, AND THAT CHANGE IS THE POINT. This pin used to drive
    the speaker case, which no longer reaches the registry at all: the BEARER test settles it from
    grammar alone (see the companion pin below). A fail-safe control that short-circuits before the
    thing it claims to make safe pins nothing — the same structural vacuity this lane has shipped
    twice. The cursor is built directly (``_AliasCur(None)`` raises) rather than through
    ``_refuses``, whose ``None`` means "empty registry"."""
    assert m._name_correction_misbinds_referent(
        _AliasCur(None), "Actually my sister's name is Thorne, not Wrenna.",
        SISTER, "thorne", SPEAKER) is None


def test_the_bearer_refusal_needs_no_registry_at_all():
    """The companion to the fail-safe pin, and the reason the guard survives a dead registry with
    its teeth in: a naming clause whose bearer is grammatically NOT the speaker can never land on
    the speaker, and settling that requires no lookup. A DB blip therefore downgrades the LABEL
    evidence to "no opinion" without disarming the protection that stops the measured corruption."""
    assert m._name_correction_misbinds_referent(
        _AliasCur(None), "Actually my sister's name is Thorne, not Wrenna.",
        SPEAKER, "thorne", SPEAKER) is not None


@pytest.mark.parametrize("text,value", [
    ("Actually my mother's age is 63, not 62.", "63"),      # no naming frame at all
    ("My favorite color is red, not blue.", "red"),
    ("Actually my sister's name is Thorne, and my age is 40.", "40"),  # value outside the clause
    ("", "thorne"),
    ("Actually my sister's name is Thorne, not Wrenna.", ""),          # nothing to write
])
def test_the_guard_keeps_its_hands_off_everything_else(text, value):
    assert not _refuses(text, value, target=ACCOUNTANT, registry={"wrenna": [SISTER, ACCOUNTANT]})


# ── SUBJECT-AGNOSTICISM DEMONSTRATED, NOT ASSERTED ──────────────────────────────────────────────
# Same construction, different cue-class roles, different names, both genders' pronouns, and a
# non-person bearer. A fix that hardcoded this defect's own lexicon (sister/Wrenna/Thorne) passes
# the rows above and fails here.

@pytest.mark.parametrize("role", ["sister", "mother", "brother", "father", "colleague",
                                  "accountant", "dog", "cat", "neighbour"])
@pytest.mark.parametrize("old,new", [("Wrenna", "Thorne"), ("Mira", "Kai"), ("Susan", "Diana")])
def test_the_refusal_is_keyed_on_grammar_not_on_the_role_or_the_name(role, old, new):
    text = f"Actually my {role}'s name is {new}, not {old}."
    assert _refuses(text, new, target=ACCOUNTANT, registry={old.lower(): [SISTER, ACCOUNTANT]})
    assert not _refuses(text, new, target=SISTER, registry={old.lower(): [SISTER]})


@pytest.mark.parametrize("pron", ["her", "his", "their"])
def test_the_pronominal_bearer_is_grammatical_not_a_pronoun_list(pron):
    text = f"Actually {pron} name is Thorne, not Wrenna."
    assert _refuses(text, "thorne", target=SPEAKER, registry={"wrenna": [SISTER]})
    assert not _refuses(text, "thorne", target=SISTER, registry={"wrenna": [SISTER]})


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# THE SOLE-HOLDER HOLE — THE REGRESSION THIS SECTION EXISTS TO KEEP CLOSED
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# MEASURED, 4/4 on fresh tenants, DB-verified (not the response):
#     turn 1  "My name is Wrenna."
#     turn 2  "Actually my sister's name is Thorne, not Wrenna."
#     RESULT  the SPEAKER's own entity preferred-named ``thorne``, their ``user`` label demoted,
#             and the sister received nothing.
#
# ROOT CAUSE: the guard ENDED on ``if len(holders) == 1 and target in holders: allow``. SOLE
# HOLDERSHIP OF A LABEL IS NOT BEARERHOOD — turn 1 leaves the SPEAKER the only entity aliased
# ``wrenna``, so the admission fired for the speaker and the sister's new name was written onto
# them. Every refusal the old predicate produced came from the UNIQUENESS test (two holders), never
# from identifying the bearer.
#
# ⚠️ WHY THE OLD SUITE COULD NOT SEE IT — read this before adding a control here. Every existing
# refusal pin was built on a registry with TWO holders, so it passed on the uniqueness test alone
# and was STRUCTURALLY BLIND to the one-holder case. A negative control must satisfy the guard's
# ENTIRE admission condition and differ from the refused population only in THE FEATURE THE GUARD
# NAMES — here, whether the target is the clause's bearer — not in an incidental precondition.

_DEVICE = "99999999-0000-0000-0000-000000000009"


@pytest.mark.parametrize("kind,target", [
    ("speaker", SPEAKER), ("third_party", ACCOUNTANT), ("pet", PET_DOG), ("device", _DEVICE),
])
def test_sole_holdership_of_the_label_does_not_make_the_target_the_bearer(kind, target):
    """The SAME registry shape the old admission green-lit — exactly one stored entity holds the
    rejected label, and it IS the write's target — driven through four referent kinds. The answer
    must track BEARERHOOD, not uniqueness: a first-person-excluding naming clause can never land on
    the SPEAKER, whoever else happens to hold the label.

    Ablating the bearer test (deleting the ``_is_speaker`` refusal for an ``other`` clause) greens
    the speaker row and reddens nothing else — which is precisely the shape the regression had."""
    refused = _refuses("Actually my sister's name is Thorne, not Wrenna.", "thorne",
                       target=target, registry={"wrenna": [target]})
    assert refused is (kind == "speaker"), (kind, target)


def test_the_two_turn_repro_is_refused():
    """The literal repro, as a unit. Turn 1 ("My name is Wrenna.") is represented by its only
    consequence that matters here: the SPEAKER is the sole holder of ``wrenna``."""
    assert _refuses("Actually my sister's name is Thorne, not Wrenna.", "thorne",
                    target=SPEAKER, registry={"wrenna": [SPEAKER]})


# ── THE OPPOSITE DIRECTION: ABSENT EVIDENCE IS NOT GROUNDS TO REFUSE ────────────────────────────
# ``labels = (labels - own) - set(parts)`` empties whenever the only label IS the new value, which
# is the ordinary shape of a correction with no rejected alternative. The old predicate then
# returned a refusal, blocking an entire ordinary construction even when correctly targeted.

@pytest.mark.parametrize("text", [
    "Actually my sister's name is Thorne.",
    "My sister is called Thorne.",
    "My sister goes by Thorne.",
    "Actually her name is Thorne.",
    "My sister's nickname is Thorne.",
])
def test_a_naming_correction_with_no_rejected_alternative_still_lands(text):
    """Correctly targeted, no "not X" to offer a second label — the write must proceed. Refusal is
    the safe default only where the write would be MISBOUND, never where evidence is merely absent.
    Ablating the "no label evidence -> allow" branch (returning a refusal instead) reddens all of
    these while leaving every refusal pin green."""
    assert not _refuses(text, "thorne", target=SISTER, registry={"wrenna": [SISTER]}), text


@pytest.mark.parametrize("text", [
    "Actually my sister's name is Thorne.",
    "My sister is called Thorne.",
    "My sister goes by Thorne.",
    "Actually her name is Thorne.",
])
def test_the_same_construction_is_still_refused_when_it_would_land_on_the_speaker(text):
    """The paired half: the construction above is ALLOWED because the target is the bearer, not
    because the guard stopped looking. Same text, same registry, speaker target -> refused."""
    assert _refuses(text, "thorne", target=SPEAKER, registry={"wrenna": [SISTER]}), text


# ── A NAME PART IS NOT AN IDENTIFICATION KEY ────────────────────────────────────────────────────

def test_a_single_token_of_a_multi_token_rejected_name_cannot_admit_an_unrelated_entity():
    """"…is Thorne, not Wrenna Vale." used to admit any entity uniquely aliased ``vale``, because
    every token of the rejected name was an independent label. ``Wrenna Vale`` is ONE label (spaCy
    attaches ``Wrenna`` as ``compound`` of ``Vale``; UD ``flat`` covers the same nominal sequence),
    and its parts may REFUTE but never ADMIT. Ablating the ``_NAMING_NAME_PART_DEPS`` grouping in
    ``linguistics._naming_frame_label_surfaces`` greens this."""
    assert _refuses("Actually my sister's name is Thorne, not Wrenna Vale.", "thorne",
                    target=_DEVICE, registry={"vale": [_DEVICE], "wrenna": [SISTER]})


def test_the_whole_multi_token_label_still_admits_its_real_bearer():
    """The other direction of the same change: when the store holds the full name, the full name
    identifies its bearer."""
    assert not _refuses("Actually my sister's name is Thorne, not Wrenna Vale.", "thorne",
                        target=SISTER, registry={"wrenna vale": [SISTER]})


def test_the_label_set_carries_the_multi_token_name_as_one_element():
    from src.extraction.linguistics import naming_frames
    labels = set()
    for _kind, _surfaces, _lab, _pred in naming_frames(
            "Actually my sister's name is Thorne, not Wrenna Vale."):
        labels.update(_lab)
    assert "wrenna vale" in labels and "vale" not in labels, sorted(labels)


# ── TYPE NODES ARE AN INGEST ARTEFACT, NOT A RIVAL REFERENT ─────────────────────────────────────

def test_a_type_node_holding_the_name_does_not_manufacture_a_rival(monkeypatch):
    """Ingest files an instance's name onto the ROLE'S TYPE NODE as well as onto the instance, so on
    a real tenant a name label almost never has exactly one holder. Counting the type node as a
    rival referent refuses the ordinary, correctly targeted correction. Holders are filtered through
    ``_entity_is_l4_place`` (THE HARD LINE, metadata-driven from ``rel_types.wikidata_pid``).

    Ablating the filter (dropping the ``db is not None`` block) reddens this."""
    monkeypatch.setattr(m, "_entity_is_l4_place", lambda db, eid: eid == TYPE_DOG)
    reg = {"bandit": [PET_DOG, TYPE_DOG]}
    assert not _refuses("Actually my dog's name is Rex, not Bandit.", "rex",
                        target=PET_DOG, registry=reg, db=object())
    # …and the filter must not become a blanket admission: a genuine second MEMORY still refuses.
    assert _refuses("Actually my dog's name is Rex, not Bandit.", "rex",
                    target=PET_DOG, registry={"bandit": [PET_DOG, PET_CAT, TYPE_DOG]},
                    db=object())


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# THE BYPASS IS PINNED — IT SHIPPED COMPLETELY UNTESTED
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The regressing commit added ``and not _name_repair_bound_referent`` to the guard's condition,
# justified as "that binding IS a referent resolution". That is FALSE on the single-candidate path:
# ``_detect_name_repair_correction`` takes ``candidates[0]`` and only calls
# ``_disambiguate_name_repair_anchor`` when ``len(candidates) > 1``. So the one path with NO probe
# was the one path with NO guard — and ablating the bypass term reddened NOTHING, because nothing
# pinned it. These pin both halves.

def test_the_single_candidate_path_reports_that_no_referent_probe_ran():
    """Ablating this (setting ``referent_probed = True`` unconditionally) reddens here and re-opens
    the exact hole the repro drove through."""
    cur = _FakeCur(None, rivals=[_SISTER], role_holders=())
    got = m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", cur, "u")
    assert got is not None and got["referent_probed"] is False, got


def test_the_role_edge_probe_reports_that_it_resolved_the_referent():
    cur = _FakeCur(None, rivals=[_RIVAL, _SISTER], role_holders={_SISTER})
    got = m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", cur,
        "00000000-0000-0000-0000-0000000000ff")
    assert got is not None and got["referent_probed"] is True, got


def test_a_probed_referent_suppresses_the_label_step_only():
    """What the probe legitimately buys: it already separated the rival holders by the deriver's own
    ROLE EDGE, so the label step must not re-litigate them.

    Ablating the ``referent_probed`` short-circuit reddens the first row."""
    reg = {"wrenna": [SISTER, ACCOUNTANT]}
    assert not _refuses("Actually my sister's name is Thorne, not Wrenna.", "thorne",
                        target=SISTER, registry=reg, referent_probed=True)
    assert _refuses("Actually my sister's name is Thorne, not Wrenna.", "thorne",
                    target=SISTER, registry=reg, referent_probed=False)


def test_a_probed_referent_does_NOT_suppress_the_bearer_test():
    """AND WHAT IT MUST NEVER BUY. A graph probe over rival holders says nothing about whether the
    clause is first person, so the BEARER test runs on every path. Moving the ``referent_probed``
    short-circuit above the bearer test — i.e. restoring the old wholesale bypass — greens this and
    re-ships the regression."""
    assert _refuses("Actually my sister's name is Thorne, not Wrenna.", "thorne",
                    target=SPEAKER, registry={"wrenna": [SPEAKER]}, referent_probed=True)
    assert _refuses("Actually my name is Thorne, not Wrenna.", "thorne",
                    target=SISTER, registry={"wrenna": [SISTER]}, referent_probed=True)


def test_a_rejected_label_that_names_nothing_stored_is_not_grounds_to_refuse():
    """The other absent-evidence shape: the clause DOES offer a label, but no stored entity holds
    it (the old name was never captured on this tenant). Grammar has already cleared the bearer, so
    the write proceeds — refusing here would block a correction for want of a row that a correction
    is under no obligation to have. Ablating the final "no evidence -> allow" reddens this."""
    assert not _refuses("Actually my sister's name is Thorne, not Wrenna.", "thorne",
                        target=SISTER, registry={})


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# THE SAME BEARER TEST AT THE OTHER DOOR — /ingest
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# MEASURED on the live stack, and it is why the correction guard alone did not close the repro:
# the MCP's INTENT-INDEPENDENT HARVEST fires ``_harvest_turn_facts`` on a CORRECTION turn BEFORE
# honouring the retract route, so the deriver's ``(wrenna, pref_name, thorne)`` edge reaches
# ``/ingest`` FIRST. There the alias short-circuit collapses the subject ``wrenna`` onto the
# SPEAKER (correct in general — after "My name is Wrenna." it really is their alias) and the label
# lands on them. One tenant, in order:
#     ingest.subject_resolved_as_user_identity_via_alias input=wrenna output=<user_uuid>
#     entity_registry.alias_registered alias=thorne canonical=<user_uuid> preferred=True
#     correct_fact.name_referent_misbind_refused   ← the guard refusing a write already committed
#                                                    at the other door

class _FakeDb:
    def __init__(self, registry):
        self._registry = registry

    def cursor(self):
        cur = _AliasCur(self._registry)

        class _Ctx:
            def __enter__(_s):
                return cur

            def __exit__(_s, *_a):
                return False
        return _Ctx()


def _ing(monkeypatch, text, rel_type, value, target, tail=("SCALAR",), registry=None,
         subject=None):
    """``subject`` is the RAW SUBJECT SURFACE the deriver put on the edge — the string ingest
    resolved the target FROM. It is a real argument of the live call site
    (``subject_surface=edge.subject``), and leaving it out of a pin silently tests a different
    question than the one /ingest asks."""
    monkeypatch.setattr(m, "_rel_meta", lambda rt=None: {"tail_types": list(tail)})
    return m._label_write_misbinds_referent(
        _FakeDb({} if registry is None else registry), text, rel_type, target, value, SPEAKER,
        subject_surface=subject)


def test_a_label_write_onto_the_speaker_from_a_third_party_clause_is_dropped(monkeypatch):
    """THE REPRO'S INGEST HALF. Ablating the ``_label_write_misbinds_referent`` call in ``/ingest``
    (or the guard's bearer test) re-opens it."""
    assert _ing(monkeypatch, "Actually my sister's name is Thorne, not Wrenna.",
                "pref_name", "thorne", SPEAKER) is not None


def test_the_speakers_own_naming_turn_still_ingests(monkeypatch):
    assert _ing(monkeypatch, "My name is Wrenna.", "also_known_as", "wrenna", SPEAKER) is None
    assert _ing(monkeypatch, "Actually my name is Thorne, not Wrenna.",
                "pref_name", "thorne", SPEAKER) is None


def test_a_non_label_scalar_on_the_same_turn_is_untouched(monkeypatch):
    """``(user, age, 62)`` rides a turn that also carries a naming clause: the object is a scalar
    but it is not a LABEL of anybody, so precondition 2 declines."""
    assert _ing(monkeypatch, "Actually my sister's name is Thorne, and my age is 62.",
                "age", "62", SPEAKER) is None


def test_a_scalar_whose_value_is_the_ROLE_NOUN_is_not_dropped(monkeypatch):
    """THE LABEL-MEMBERSHIP PRECONDITION, PINNED — and it pins the guard's NARROWNESS, which is a
    deliberate choice rather than an oversight.

    The same turn really does produce ``(user, also_known_as, sister)`` on the live stack (the
    deriver files the role noun beside the name). ``sister`` is a token of the third-party clause,
    so the shared predicate WOULD refuse it — but it is not a LABEL of the clause's bearer
    (``_naming_frame_label_surfaces`` excludes the role noun, measured: a role noun is routinely an
    alias of a TYPE NODE), and this guard exists to stop a NAME landing on the wrong human, not to
    tidy the deriver's role-alias artefact. Ablating the membership test (guarding every
    SCALAR-tailed edge whose value appears anywhere in the clause) reddens this and quietly widens
    an ingest-side DROP well past the defect it was built for.

    ⚠️ The role-alias artefact itself is REAL and is reported as an open residual, not fixed here."""
    assert _ing(monkeypatch, "Actually my sister's name is Thorne, not Wrenna.",
                "also_known_as", "sister", SPEAKER) is None


def test_an_entity_tailed_edge_is_untouched(monkeypatch):
    """``(user, has_pet, rex)`` from "my dog's name is Rex, not Bandit." — ``rex`` IS a label of the
    clause, but the rel's tail is an ENTITY, so this edge files a relation, not a label. Ablating
    the SCALAR ``tail_types`` precondition reddens this and drops real relational content."""
    assert _ing(monkeypatch, "Actually my dog's name is Rex, not Bandit.",
                "has_pet", "rex", SPEAKER, tail=("Animal",)) is None


def test_the_ingest_guard_is_fail_safe(monkeypatch):
    """No metadata for the rel → no opinion. A guard that cannot read its own preconditions must
    let the edge through, never drop user data on an infrastructure gap."""
    monkeypatch.setattr(m, "_rel_meta", lambda rt=None: {})
    assert m._label_write_misbinds_referent(
        _FakeDb({}), "Actually my sister's name is Thorne, not Wrenna.",
        "pref_name", SPEAKER, "thorne", SPEAKER) is None


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# THE VALUE THE PRODUCER EMITS — AND THE TARGETS THAT ARE NOT THE SPEAKER
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# ⚠️⚠️ WHY THIS BLOCK EXISTS, AND WHY 162 GREEN PINS DID NOT CATCH WHAT IT CATCHES.
#
#   * EVERY ingest-door pin above passes a SINGLE token ("thorne", "wrenna", "sister", "rex").
#   * The one multi-token pin passes the guard ``"Thorne Vale"`` — a value THE PIPELINE NEVER
#     PRODUCES — and it exercises only the CORRECTION door.
#   * Every end-to-end case used ``new="thorne"`` or ``new="rex"``.
#
# So the whole lane sampled a ONE-TOKEN value space while varying constructions, and the hole was
# in the value space. ``_naming_frame_label_surfaces`` JOINS a multi-token name into ONE label
# ("mary anne"); the DERIVER emits the LAST TOKEN ('anne'). The ingest door gated on membership in
# the joined set, so it was SHUT for every multi-token name: swept over 7 names, 0/7 satisfied the
# precondition, and end-to-end the only difference between a clean run and a 5/5 corrupt one was
# one token versus two.
#
# THE RULE THESE PINS ENFORCE: a pin must feed the guard the value THE PIPELINE ACTUALLY EMITS.
# ``_producer_label_edge`` below runs the real deriver and reads the value off its edge, so a pin
# CANNOT drift back to a hand-typed surface — if the deriver's output shape changes, these pins
# change with it instead of quietly pinning a fiction.

def _producer_label_edge(text):
    """The (subject, rel_type, object) the REAL deriver emits for the label change in ``text``."""
    from src.extraction.linguistics import derive_sentence_facts
    for f in derive_sentence_facts(text, None, None) or []:
        if getattr(f, "preferred_label", False):
            return f.subject, f.rel_type, f.object
    return None


def test_the_deriver_emits_a_name_PART_not_the_joined_label():
    """THE PRECONDITION PROBE, STATED AS A FACT ABOUT THE PRODUCER. If this ever goes red the two
    consumers have re-converged and the ingest door's part-matching may be revisited — until then
    it is the reason that door needs it."""
    edge = _producer_label_edge("Actually my sister's name is Mary Anne, not Wrenna.")
    assert edge is not None and edge[2] == "anne", edge
    labels = set()
    from src.extraction.linguistics import naming_frames
    for _k, _s, _lab, _p in naming_frames("Actually my sister's name is Mary Anne, not Wrenna."):
        labels.update(_lab)
    assert "mary anne" in labels and edge[2] not in labels, (edge, sorted(labels))


@pytest.mark.parametrize("text,target", [
    # the SPEAKER (the original defect) and a THIRD PARTY — the guard must not tell them apart.
    ("Actually my sister's name is Mary Anne, not Wrenna.", SPEAKER),
    ("Actually my sister's name is Mary Anne, not Wrenna.", ACCOUNTANT),
    ("Actually my sister's name is Mira Nair, not Wrenna.", SPEAKER),
    ("Actually my sister's name is Mira Nair, not Wrenna.", ACCOUNTANT),
    ("Actually my brother's name is Jean Luc, not Wrenna.", ACCOUNTANT),
])
def test_a_multi_token_name_reaches_the_ingest_door(monkeypatch, text, target):
    """Ablating the name-PART matching in ``_label_write_misbinds_referent`` (going back to
    ``new_value not in _labels``) reddens every row here — that is the exact disarm that shipped."""
    edge = _producer_label_edge(text)
    assert edge is not None, text
    assert _ing(monkeypatch, text, edge[1], edge[2], target, subject=edge[0],
                registry={"wrenna": [target]}) is not None, edge


@pytest.mark.parametrize("target", [SPEAKER, ACCOUNTANT, PET_CAT])
def test_the_ingest_door_runs_for_every_target_not_only_the_speaker(monkeypatch, target):
    """DEFECT 2, PINNED AT THE PREDICATE. The call site used to invoke this only when the write
    would land on the SPEAKER — a literal identity in a guard condition, which is the shape the
    owner retired. The predicate itself must answer identically for whoever the write would land
    on; the CALL SITE's own ablation is end-to-end (see the module note below)."""
    edge = _producer_label_edge("Actually my sister's name is Thorne, not Wrenna.")
    assert _ing(monkeypatch, "Actually my sister's name is Thorne, not Wrenna.",
                edge[1], edge[2], target, subject=edge[0],
                registry={"wrenna": [target]}) is not None


def test_resolution_through_a_PREDICATED_label_is_refused_whoever_holds_it(monkeypatch):
    """THE NON-SPEAKER HALF, AND THE ARM THAT CLOSES IT.

    In "X's name is Y" the referring expression is ``X``; ``Y`` is PREDICATED of it. In "…my
    sister's name is Thorne, not Wrenna." BOTH names sit in the predicate and the only referring
    expression is the ROLE — which is not an identification key. So resolving the write's subject
    through ``wrenna`` answers "whoever holds that string", not "the bearer". Measured: with a
    COLLEAGUE aliased ``wrenna``, ingest resolved to HER and renamed a real person 3/3.

    The refusal is stated over the RESOLUTION ROUTE, so it is identical for the speaker, a third
    party, and even the correctly-intended referent — no literal identity anywhere in it. Ablating
    the ``_pred_keys`` block reddens this."""
    for target in (SPEAKER, ACCOUNTANT, SISTER, PET_DOG):
        assert _ing(monkeypatch, "Actually my sister's name is Thorne, not Wrenna.",
                    "pref_name", "thorne", target, subject="wrenna",
                    registry={"wrenna": [target]}) is not None, target


def test_resolution_through_a_REFERRING_label_still_ingests(monkeypatch):
    """The other side of the split, and the reason it is a split rather than a blanket refusal.
    "John's name is actually Jonathan." resolves the subject through ``john`` — John's own way of
    being referred to, a REFERRING label, not a predicated one. Ablating the referring/predicated
    distinction (treating every label as predicated) reddens this and blocks an ordinary
    construction."""
    assert _ing(monkeypatch, "John's name is actually Jonathan.",
                "pref_name", "jonathan", SISTER, subject="john",
                registry={"john": [SISTER]}) is None


@pytest.mark.parametrize("text,subject,value", [
    # a FIRST-MENTION naming statement: the only label IS the value being assigned, the subject is
    # the ROLE noun, and nothing is being replaced. These must keep flowing.
    ("My daughter's name is Mira Nair.", "daughter", "mira nair"),
    ("The restaurant's name is Miss Bee Providore.", "restaurant", "miss bee providore"),
])
def test_a_first_mention_naming_statement_is_untouched(monkeypatch, text, subject, value):
    assert _ing(monkeypatch, text, "name", value, SISTER, subject=subject, registry={}) is None


# ── THE CORRECTION DOOR'S HALF OF THE NON-SPEAKER DEFECT ────────────────────────────────────────

def test_a_role_refuted_target_is_refused_at_the_correction_door():
    """SOLE HOLDERSHIP IS NOT BEARERHOOD, AND THE PROBE NOW SAYS SO ON THE SINGLE-CANDIDATE PATH.

    The colleague holds ``wrenna`` uniquely, so the intercept found ONE candidate and took it with
    no probe at all — the path with no evidence was the path with no guard. With the probe running
    on every path, real role evidence that the target fails is a REFUTATION, and it must dominate
    the label lookup for the same reason the bearer test does: the label lookup would only
    re-confirm what is not in dispute (the target holds the rejected label).

    Ablating ``referent_refuted`` in ``_name_correction_misbinds_referent`` reddens this."""
    assert m._name_correction_misbinds_referent(
        _AliasCur({"wrenna": [ACCOUNTANT]}), "Actually my sister's name is Thorne, not Wrenna.",
        ACCOUNTANT, "thorne", SPEAKER, referent_refuted=True) is not None
    # …and a probe that RESOLVED the target still allows: the two flags are opposites, not degrees.
    assert m._name_correction_misbinds_referent(
        _AliasCur({"wrenna": [ACCOUNTANT]}), "Actually my sister's name is Thorne, not Wrenna.",
        ACCOUNTANT, "thorne", SPEAKER, referent_probed=True) is None


class _RoleCur(_AliasCur):
    """A cursor that also answers the role-evidence existence probe."""

    def execute(self, sql, params=None):
        if self._registry is None:
            raise RuntimeError("registry unreadable")
        if "LIMIT 1" in sql:
            self._rows = [(1,)] if self._registry.get((params or [""])[0]) else []
            return
        super().execute(sql, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_role_evidence_is_distinguished_from_its_absence():
    """ABSENCE OF EVIDENCE IS NOT REFUTATION — the corollary that cost this lane a whole round when
    it was got backwards. A clause that describes its bearer relationally ("my sister's …") and
    resolves its possessor gives evidence a candidate can FAIL. A clause that gives no role edge at
    all ("John's name is actually Jonathan.") gives nothing to fail, and refusing on that silence
    blocks an ordinary construction."""
    from types import SimpleNamespace as NS
    role_stream = [NS(subject="wrenna", rel_type="sibling_of", object="user",
                      preferred_label=False),
                   NS(subject="wrenna", rel_type="pref_name", object="thorne",
                      preferred_label=True)]
    bare_stream = [NS(subject="john", rel_type="also_known_as", object="jonathan",
                      preferred_label=True)]
    assert m._role_evidence_available(_RoleCur({}), role_stream, "wrenna", "thorne", SPEAKER)
    assert not m._role_evidence_available(_RoleCur({}), bare_stream, "john", "jonathan", SPEAKER)
    # A FIRST-PERSON possessor needs no registry at all — the engine's one language hook binds it,
    # which is why the row above holds even with an empty registry.
    named_stream = [NS(subject="wrenna", rel_type="works_for", object="acme",
                       preferred_label=False)]
    assert m._role_evidence_available(_RoleCur({"acme": [ACCOUNTANT]}), named_stream,
                                      "wrenna", "thorne", SPEAKER)
    assert not m._role_evidence_available(_RoleCur({}), named_stream, "wrenna", "thorne", SPEAKER)
    # fail-safe: an unreadable cursor yields NO evidence, hence NO refutation — a DB blip must
    # never manufacture a refusal (nor, on the first-person arm, need the DB at all).
    assert not m._role_evidence_available(_RoleCur(None), named_stream, "wrenna", "thorne", SPEAKER)


# ⚠️ THE /ingest CALL SITE ITSELF — the removal of ``canonical_subject == user_entity_id`` — is
# endpoint wiring no unit pin can reach. It is pinned END-TO-END instead:
#   DEV/review/gauntlet/perfect-flow/e2e/referent_rename_repeat.py
#     N_non_speaker_target / N2_non_speaker_target_mt   CORRUPT 5/5 before, 0/5 after
#     M2_multitoken_sole_holder                         CORRUPT 5/5 before, 0/5 after
# Restoring the identity comparison re-opens the colleague rename; restoring the joined-set
# membership test re-opens every multi-token name.


# ── THE PROBE MUST RUN ON THE SINGLE-CANDIDATE PATH TOO ─────────────────────────────────────────
# ⚠️ THE ARM THAT WAS UNPINNED BY CONSTRUCTION. Restoring ``if len(candidates) > 1:`` around the
# probe reddened NOTHING in the ablation matrix — the existing single-candidate pin above uses a
# fixture with NO role holders, so it answers ``referent_probed is False`` whether the probe ran and
# declined or never ran at all. A pin that cannot tell "asked and got no" from "never asked" pins
# nothing, and "never asked" is precisely where the colleague rename came from: ONE holder of
# ``wrenna``, taken with no evidence and no guard.
#
# These two rows differ from that pin in the ONE feature the change names — whether role evidence
# exists for the sole candidate — so each answers a different question and both go red under the
# ablation.

def test_a_sole_candidate_that_SATISFIES_the_role_edge_is_probed():
    """The correctly-targeted case: the only holder of the rejected label really is the speaker's
    sister, the probe confirms it, and that is a genuine bearer resolution."""
    cur = _FakeCur(None, rivals=[_SISTER], role_holders={_SISTER})
    got = m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", cur,
        "00000000-0000-0000-0000-0000000000ff")
    assert got is not None and got["referent_probed"] is True, got
    assert got["referent_refuted"] is False, got


def test_a_sole_candidate_that_FAILS_the_role_edge_is_refuted():
    """THE COLLEAGUE, IN A UNIT PIN. She holds ``mara`` uniquely and is NOT the speaker's sister,
    so the probe has real evidence and she fails it — a REFUTATION, not an absence. Restoring
    ``len(candidates) > 1`` around the probe greens this and re-opens the measured 5/5 rename."""
    cur = _FakeCur(None, rivals=[_RIVAL], role_holders=set())
    got = m._detect_name_repair_correction(
        "Actually my sister's name is Renata, not Mara.", cur,
        "00000000-0000-0000-0000-0000000000ff")
    assert got is not None and got["referent_probed"] is False, got
    assert got["referent_refuted"] is True, got


# ── THE REJECTED NAME IS NOT A REFERENT KEY FOR *ANY* EDGE ON THE TURN ──────────────────────────
# ⚠️ GUARDING THE LABEL WRITE ALONE LEFT THE COLLEAGUE CASE CORRUPT 5/5 — measured, after the
# multi-token precondition and the call-site gate were both already fixed. The deriver emits TWO
# edges from one clause and only one of them is a label write; the other resolved the SAME unsound
# way, made a real colleague the speaker's sibling, and then FORGED the evidence the repair lane
# reads, so the correction door renamed her with the guard's blessing.

class _E:
    def __init__(self, subject, rel_type, object):
        self.subject, self.rel_type, self.object = subject, rel_type, object


_CORRECTIVE = [_E("wrenna", "sibling_of", "user"), _E("wrenna", "pref_name", "thorne")]


def test_the_rejected_name_is_collected_the_asserted_one_is_not():
    """Ablating ``_turn_rejected_label_surfaces`` (or its use at the /ingest loop head) re-opens the
    colleague rename AND the probe-poisoning behind it."""
    got = m._turn_rejected_label_surfaces(
        "Actually my sister's name is Thorne, not Wrenna.", _CORRECTIVE)
    assert got == {"wrenna"}, got


def test_a_multi_token_rejected_name_is_collected_from_the_part_the_deriver_emits():
    edges = [_E("wrenna", "sibling_of", "user"), _E("wrenna", "pref_name", "anne")]
    assert m._turn_rejected_label_surfaces(
        "Actually my sister's name is Mary Anne, not Wrenna.", edges) == {"wrenna"}, edges


@pytest.mark.parametrize("text,edges", [
    # a FIRST-MENTION naming statement rejects nothing
    ("My daughter's name is Mira Nair.", [_E("daughter", "name", "mira nair")]),
    # a REFERRING possessor is not a predicated label, so it is never "rejected"
    ("John's name is actually Jonathan.", [_E("john", "also_known_as", "jonathan")]),
    # the speaker's OWN naming clause is not a third-party clause
    ("Actually my name is Thorne, not Wrenna.", [_E("user", "also_known_as", "thorne")]),
    # no naming clause at all
    ("Actually my mother's age is 63, not 62.", [_E("mother", "age", "63")]),
    ("", _CORRECTIVE),
    ("Actually my sister's name is Thorne, not Wrenna.", []),
])
def test_nothing_is_rejected_where_nothing_was_rejected(text, edges):
    assert m._turn_rejected_label_surfaces(text, edges) == set(), (text, edges)


def test_the_asserted_name_stays_usable_as_a_referent_key():
    """"…is Thorne, not Wrenna. She lives in Toronto." — ``thorne`` is the name being ASSIGNED, so
    it identifies the bearer from here on and must never be collected. Widening the scan to every
    predicated label reddens this and would silently drop ordinary content on a correction turn."""
    edges = _CORRECTIVE + [_E("thorne", "live_in", "toronto")]
    got = m._turn_rejected_label_surfaces(
        "Actually my sister's name is Thorne, not Wrenna. She lives in Toronto.", edges)
    assert "thorne" not in got and got == {"wrenna"}, got


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE WALK — a correction resolves its referent out of the graph, exactly as recall does
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# OWNER'S RULING: "shouldn't that corpus be an L4 walk to user->family->sister->entity then update?
# ... the entire concept of strong ingest, L4 growth, and the rules surrounding it are intended to
# allow recall and correction alike to be BRAIN-DEAD STUPID."
#
# Everything above this line resolves the referent by LABEL — who holds the rejected string — and
# then guards the answer. These pin the replacement: the clause's own referent description (a ROLE
# plus a POSSESSOR) is a PATH, and walking it lands on the referent with no label lookup at all.
#
# ⚠️ THE FAKE GRAPH BELOW IS BUILT FROM MEASURED SUBSTRATE, not from what would be convenient. Live
# on fresh tenants at this HEAD, "My sister Wrenna lives in Toronto." commits
# ``(user, sibling_of, wrenna)`` AND ``(wrenna, instance_of, <sister node>)``; "I have a dog named
# Bandit." commits ``(user, has_pet, bandit)`` AND ``(bandit, instance_of, <dog node>)``. The dict
# shape handed to the walk is the one ``fetch_facts_from_anchor`` really returns — raw UUIDs in
# ``subject``/``object`` and a ``source`` of ``db``/``staged``/``attributes`` — because a pin that
# feeds a guard a value the pipeline never produces proves nothing about the pipeline.

USER = "00000000-0000-0000-0000-00000000user"
SISTER = "00000000-0000-0000-0000-000000sister"
SISTER_NODE = "00000000-0000-0000-0000-00000snode"
COLLEAGUE = "00000000-0000-0000-0000-0000colleag"
DOG = "00000000-0000-0000-0000-000000000dog"
CAT = "00000000-0000-0000-0000-000000000cat"
DOG_NODE = "00000000-0000-0000-0000-0000dognode"
CAT_NODE = "00000000-0000-0000-0000-0000catnode"


class _WalkDbCur:
    """Answers only the two alias lookups the walk makes (possessor surface, role surface)."""

    def __init__(self, aliases, boom=False):
        self._aliases = aliases
        self._boom = boom
        self._last = None

    def execute(self, sql, params=None):
        if self._boom:
            raise RuntimeError("registry gone")
        self._last = (params or (None,))[0]

    def fetchall(self):
        return [(e,) for e in self._aliases.get(self._last, ())]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _WalkDb:
    def __init__(self, aliases, boom=False):
        self._aliases, self._boom = aliases, boom

    def cursor(self):
        return _WalkDbCur(self._aliases, self._boom)


def _install_graph(monkeypatch, edges, aliases, ladders, places=(), kin_map=None, fetch=None):
    """Wire the walk's four collaborators to a small explicit graph."""
    def _fetch(anchor, uid, path, **kw):
        if fetch is not None:
            return fetch(anchor, uid, path)
        return [{"subject": s, "object": o, "rel_type": r, "source": "db"}
                for s, r, o in edges]
    monkeypatch.setattr(m, "fetch_facts_from_anchor", _fetch)
    monkeypatch.setattr(m, "_entity_is_l4_place", lambda db, e: e in set(places))
    _ladder = lambda db, uid, e, direction="up", max_depth=3: {e} | set(ladders.get(e, ()))  # noqa: E731
    monkeypatch.setattr(m, "_hierarchy_expand", _ladder)
    # WALK_QUERY_BATCHING: the walk's L4-ladder site batches through this twin —
    # mirror the patched per-seed ladder so the pin still defines the graph.
    monkeypatch.setattr(m, "_hierarchy_expand_batch",
                        lambda db, uid, ids, direction="up", max_depth=3:
                        {e: _ladder(db, uid, e, direction, max_depth) for e in ids})
    monkeypatch.setattr(m.linguistic_cue_overlay, "resolve_kinship_rel_map",
                        lambda dsn: dict(kin_map or {}))
    monkeypatch.setattr(m, "_rel_meta", lambda rel: {"inverse_rel_type": ""})
    return _WalkDb(aliases)


_SISTER_GRAPH = dict(
    edges=[(USER, "sibling_of", SISTER), (USER, "works_with", COLLEAGUE)],
    aliases={"sister": [SISTER_NODE], "wrenna": [SISTER, COLLEAGUE]},
    ladders={SISTER: [SISTER_NODE]},
    places=[SISTER_NODE],
    kin_map={"sister": "sibling_of"},
)


def test_the_walk_lands_on_the_sister_the_clause_names(monkeypatch):
    """THE RULING, in one assertion: anchor(speaker) -> role `sister` -> the edge ingest built ->
    the entity. The colleague shares the rejected label and is never consulted, because the walk
    never asks who holds a label."""
    db = _install_graph(monkeypatch, **_SISTER_GRAPH)
    got = m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, not Wrenna.")
    assert got and got["entity_id"] == SISTER and got["method"] == "walk", got


@pytest.mark.parametrize("text", [
    # ⚠️ EVERY REALIZATION, BOTH LETTER CASES. The lower-case arms are the live hole the label lane
    # could not reach at all: the parser mis-tags the OOV name so the deriver emits no label-change
    # edge, the intercept declines, and the LLM's subject guess reaches the write. The walk reads
    # the ROLE and the POSSESSOR, which parse identically in both cases.
    "Actually my sister's name is Thorne, not Wrenna.",
    "actually my sister's name is thorne, not wrenna.",
    "Actually my sister's name is Thorne.",
    "actually my sisters name is thorne, not wrenna.",
    "The name of my sister is Thorne.",
    "My sister is called Thorne.",
    "My sister goes by Thorne.",
    "Actually my sister's name is Mary Anne, not Wrenna.",
])
def test_the_walk_is_indifferent_to_the_phrasing_and_the_letter_case(monkeypatch, text):
    db = _install_graph(monkeypatch, **_SISTER_GRAPH)
    got = m._walk_naming_clause_referent(db, USER, text)
    assert got and got["entity_id"] == SISTER, (text, got)


def test_a_first_person_naming_clause_needs_no_walk_at_all(monkeypatch):
    """``Person=1`` on a personal possessive denotes the speaker BY DEFINITION OF THE CATEGORY. No
    registry, no graph — the engine's single language hook answers it."""
    db = _install_graph(monkeypatch, **_SISTER_GRAPH)
    got = m._walk_naming_clause_referent(db, USER, "Actually my name is Thorne, not Wrenna.")
    assert got and got["entity_id"] == USER and got["method"] == "first_person", got


def test_the_walk_resolves_a_role_the_cue_map_says_nothing_about(monkeypatch):
    """SUBJECT-AGNOSTIC, AND THIS IS WHERE IT IS PROVEN. ``dog`` is in no kinship map and there is
    no noun list anywhere — the L4 ladder ingest built for every named instance carries it. Delete
    the ladder test and this reddens while the kin cases stay green."""
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "has_pet", DOG)],
        aliases={"dog": [DOG_NODE]},
        ladders={DOG: [DOG_NODE]},
        places=[DOG_NODE],
        kin_map={"sister": "sibling_of"},
    )
    got = m._walk_naming_clause_referent(db, USER, "Actually my dog's name is Rex, not Bandit.")
    assert got and got["entity_id"] == DOG, got


def test_two_pets_sharing_a_name_are_separated_by_their_l4_type(monkeypatch):
    """THE OWNER'S OWN DISAMBIGUATION CASE: ``has_pet`` reaches two animals and ``instance_of dog``
    vs ``instance_of cat`` separates them. The label lane REFUSED here (both hold ``bandit``); the
    walk answers, because it never asked about the label."""
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "has_pet", DOG), (USER, "has_pet", CAT)],
        aliases={"dog": [DOG_NODE], "cat": [CAT_NODE], "bandit": [DOG, CAT]},
        ladders={DOG: [DOG_NODE], CAT: [CAT_NODE]},
        places=[DOG_NODE, CAT_NODE],
    )
    assert m._walk_naming_clause_referent(
        db, USER, "Actually my dog's name is Rex, not Bandit.")["entity_id"] == DOG
    assert m._walk_naming_clause_referent(
        db, USER, "Actually my cat's name is Rex, not Bandit.")["entity_id"] == CAT


def test_the_edge_alone_resolves_a_kin_referent_with_no_l4_ladder(monkeypatch):
    """The mirror of the pet case, and the ablation target for the EDGE test: a kin entity that
    carries no ``instance_of`` rung is resolved by the role's rel_type off the cue map. Delete the
    edge test and this reddens while the pet case stays green."""
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "sibling_of", SISTER)],
        aliases={},
        ladders={},
        kin_map={"sister": "sibling_of"},
    )
    got = m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, not Wrenna.")
    assert got and got["entity_id"] == SISTER, got


def test_where_both_role_tests_speak_they_are_INTERSECTED_not_unioned(monkeypatch):
    """Two entities classify as ``sister`` but only one holds the kin EDGE to the speaker. The
    conjunction resolves; a union would see two and decline. Ablating the intersection to a union
    reddens exactly this."""
    OTHER = "00000000-0000-0000-0000-00000000othr"
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "sibling_of", SISTER), (USER, "knows", OTHER)],
        aliases={"sister": [SISTER_NODE]},
        ladders={SISTER: [SISTER_NODE], OTHER: [SISTER_NODE]},
        places=[SISTER_NODE],
        kin_map={"sister": "sibling_of"},
    )
    got = m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, not Wrenna.")
    assert got and got["entity_id"] == SISTER, got


def test_the_walk_declines_rather_than_picking_when_the_role_reaches_two(monkeypatch):
    """FAIL LOUD, NEVER ARBITRARY. Two neighbours answer to the role and nothing separates them —
    an arbitrary pick files one referent's name onto another. Declining leaves the existing path
    and its guards exactly as they were."""
    OTHER = "00000000-0000-0000-0000-00000000othr"
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "sibling_of", SISTER), (USER, "sibling_of", OTHER)],
        aliases={},
        ladders={},
        kin_map={"sister": "sibling_of"},
    )
    assert m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, not Wrenna.") is None


def test_the_walk_declines_when_the_role_reaches_nobody(monkeypatch):
    """"My colleague Wrenna lives in Toronto." + "…my sister's name is Thorne, not Wrenna." — the
    speaker has no sister in the graph. The LABEL lane resolved this to the COLLEAGUE (sole holder
    of ``wrenna``) and renamed a real person; the walk has nothing to land on and says so."""
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "works_with", COLLEAGUE)],
        aliases={"wrenna": [COLLEAGUE]},
        ladders={COLLEAGUE: []},
        kin_map={"sister": "sibling_of"},
    )
    assert m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, not Wrenna.") is None


def test_a_type_node_reached_from_the_anchor_is_never_the_referent(monkeypatch):
    """THE HARD LINE. Ingest files the role as an alias of a TYPE node and that node can also pick
    up the instance's name, so a place sitting one hop from the anchor is an artefact, not a memory.

    ⚠️ THIS FIXTURE IS DELIBERATELY BUILT SO THE PLACE FILTER IS THE **ONLY** THING SEPARATING THEM,
    and the first version was not — with a kin rel in the cue map the INTERSECTION already excluded
    the type node, so ablating the place filter left the suite green and the term shipped unpinned
    (measured). Here the role is one the cue map says nothing about, so ``by_rel`` is empty and the
    ladder test alone decides: without the filter both the pet AND its type node classify as ``dog``
    and the walk declines; with it, exactly one survives."""
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "related_to", DOG_NODE), (USER, "has_pet", DOG)],
        aliases={"dog": [DOG_NODE]},
        ladders={DOG: [DOG_NODE], DOG_NODE: [DOG_NODE]},
        places=[DOG_NODE],
    )
    got = m._walk_naming_clause_referent(
        db, USER, "Actually my dog's name is Rex, not Bandit.")
    assert got and got["entity_id"] == DOG, got


def test_only_the_anchors_own_neighbours_are_candidates(monkeypatch):
    """``fetch_facts_from_anchor`` also returns the hierarchy expansion of entities it reaches. A
    role describes the ANCHOR'S OWN neighbour, so an edge that does not touch the anchor is not a
    candidate for it. Ablating the 1-hop restriction admits the far entity and reddens this."""
    FAR = "00000000-0000-0000-0000-0000000000far"
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "sibling_of", SISTER), (COLLEAGUE, "sibling_of", FAR)],
        aliases={},
        ladders={},
        kin_map={"sister": "sibling_of"},
    )
    got = m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, not Wrenna.")
    assert got and got["entity_id"] == SISTER, got


def test_each_clause_of_a_two_correction_turn_resolves_to_its_OWN_referent(monkeypatch):
    """"…my sister's name is Thorne, not Wrenna. …my brother's name is Wrenna, not Kai." names TWO
    referents and repairs BOTH. A LABEL-keyed mechanism cannot tell them apart — ``wrenna`` is the
    rejected name of one clause and the ASSERTED name of the other, so a turn-scoped rule keyed on
    it eats the brother's legitimate rename. Under a walk the question does not arise: each clause
    carries its own role, and the values being written say which clause is being written."""
    BROTHER = "00000000-0000-0000-0000-000000brothr"
    BROTHER_NODE = "00000000-0000-0000-0000-00000bnode"
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "sibling_of", SISTER), (USER, "sibling_of", BROTHER)],
        aliases={"sister": [SISTER_NODE], "brother": [BROTHER_NODE]},
        ladders={SISTER: [SISTER_NODE], BROTHER: [BROTHER_NODE]},
        places=[SISTER_NODE, BROTHER_NODE],
        kin_map={"sister": "sibling_of", "brother": "sibling_of"},
    )
    turn = ("Actually my sister's name is Thorne, not Wrenna. "
            "Actually my brother's name is Wrenna, not Kai.")
    assert m._walk_naming_clause_referent(
        db, USER, turn, values=("thorne", "wrenna"))["entity_id"] == SISTER
    assert m._walk_naming_clause_referent(
        db, USER, turn, values=("wrenna", "kai"))["entity_id"] == BROTHER


def test_a_multi_token_value_selects_its_clause_by_the_part_the_deriver_emits(monkeypatch):
    """⚠️ THE VALUE THE PRODUCER EMITS, NOT THE ONE THE SENTENCE CONTAINS. The deriver hands the
    caller ``anne`` for "Mary Anne" while the clause's label set joins the parts into ``mary anne``,
    so a clause selector testing the joined surface alone answers False for every two-token name —
    the exact hole that shipped twice in this lane, one slot over each time.

    ⚠️ THE TURN CARRIES TWO CLAUSES ON PURPOSE. With only one clause the selector's failure is
    invisible: no clause matches, the code falls back to "the turn holds exactly one naming clause"
    and resolves anyway. Ablating the part-expansion left the suite green on the single-clause
    version, so that term shipped unpinned (measured). Two clauses remove the fallback, and the
    selector is then the only thing that can answer.
    """
    BROTHER = "00000000-0000-0000-0000-000000brothr"
    BROTHER_NODE = "00000000-0000-0000-0000-00000bnode"
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "sibling_of", SISTER), (USER, "sibling_of", BROTHER)],
        aliases={"sister": [SISTER_NODE], "brother": [BROTHER_NODE]},
        ladders={SISTER: [SISTER_NODE], BROTHER: [BROTHER_NODE]},
        places=[SISTER_NODE, BROTHER_NODE],
        kin_map={"sister": "sibling_of", "brother": "sibling_of"},
    )
    got = m._walk_naming_clause_referent(
        db, USER,
        "Actually my sister's name is Mary Anne, not Wrenna. "
        "Actually my brother's name is Kai, not Devon.",
        values=("anne", "wrenna"))
    assert got and got["entity_id"] == SISTER, got


def test_two_naming_clauses_in_one_turn_are_not_resolved_to_one_referent(monkeypatch):
    """"…my sister's name is Thorne, and my name is Chris." names TWO referents. The walk resolves
    a clause, not a turn, and a predicate that cannot say WHICH clause the write is for must not
    answer. Clause-locality is the same discipline the guards already keep."""
    db = _install_graph(monkeypatch, **_SISTER_GRAPH)
    assert m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, and my name is Chris.") is None


@pytest.mark.parametrize("text", [
    "My favorite color is red, not blue.",     # a VALUE correction — no naming clause
    "Actually my mother's age is 63, not 62.",
    "My user name is bob.",                    # a noun-noun compound, not a personal naming frame
    "Actually her name is Thorne.",            # a bare pronominal possessor names nobody
    "",
])
def test_the_walk_has_no_opinion_outside_a_naming_clause_with_a_role(monkeypatch, text):
    db = _install_graph(monkeypatch, **_SISTER_GRAPH)
    assert m._walk_naming_clause_referent(db, USER, text) is None, text


def test_the_walk_is_off_byte_for_byte_behind_its_flag(monkeypatch):
    db = _install_graph(monkeypatch, **_SISTER_GRAPH)
    monkeypatch.setattr(m, "NAME_REFERENT_L4_WALK", False)
    assert m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, not Wrenna.") is None


def test_the_walk_is_fail_safe_when_the_graph_is_unreadable(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("graph gone")
    db = _install_graph(monkeypatch, **_SISTER_GRAPH)
    monkeypatch.setattr(m, "fetch_facts_from_anchor", _boom)
    assert m._walk_naming_clause_referent(
        db, USER, "Actually my sister's name is Thorne, not Wrenna.") is None


def test_a_proper_name_possessor_anchors_the_walk_on_that_person(monkeypatch):
    """SUBJECT-AGNOSTIC: the speaker gets no privilege. "Diane's sister's name is …" anchors on
    DIANE and walks HER sibling edge."""
    DIANE = "00000000-0000-0000-0000-0000000diane"
    db = _install_graph(
        monkeypatch,
        edges=[(DIANE, "sibling_of", SISTER), (USER, "sibling_of", COLLEAGUE)],
        aliases={"diane": [DIANE]},
        ladders={},
        kin_map={"sister": "sibling_of"},
        fetch=lambda anchor, uid, path: [
            {"subject": s, "object": o, "rel_type": r, "source": "db"}
            for s, r, o in [(DIANE, "sibling_of", SISTER), (USER, "sibling_of", COLLEAGUE)]
            if anchor in (s, o)],
    )
    got = m._walk_naming_clause_referent(
        db, USER, "Actually Diane's sister's name is Thorne, not Wrenna.")
    assert got and got["entity_id"] == SISTER, got


def test_a_referring_proper_name_possessor_IS_the_bearer_zero_hops(monkeypatch):
    """"John's name is actually Jonathan." names NO role. Its pre-copular expression is a proper
    name, and that is the ONE label that legitimately identifies a bearer — it is how the speaker
    was ALREADY referring to them, not a name being assigned or rejected. So the path is zero hops
    and the referent is the possessor itself.

    ⚠️ NOT A LABEL GUESS, and the difference is the uniqueness + place filter above it: the label
    must resolve to exactly one stored MEMORY. A role-less third-party clause has no other way of
    naming its bearer, which is precisely why the post-copular slot must never be used for it."""
    JOHN = "00000000-0000-0000-0000-0000000ojohn"
    db = _install_graph(
        monkeypatch,
        edges=[(USER, "works_with", JOHN)],
        aliases={"john": [JOHN]},
        ladders={},
    )
    got = m._walk_naming_clause_referent(db, USER, "John's name is actually Jonathan.")
    assert got and got["entity_id"] == JOHN and got["method"] == "referring_possessor", got


def test_a_roleless_third_party_clause_that_resolves_to_the_speaker_says_nothing(monkeypatch):
    """The bearer contradiction the guards exist for: a clause that is grammatically NOT about the
    speaker cannot land on the speaker. The walk declines instead of asserting it, so the bearer
    test answers rather than being pre-empted by a resolution that agrees with the defect."""
    db = _install_graph(
        monkeypatch, edges=[], aliases={"john": [USER]}, ladders={})
    assert m._walk_naming_clause_referent(
        db, USER, "John's name is actually Jonathan.") is None


def test_a_possessor_label_held_by_two_entities_identifies_neither(monkeypatch):
    """The anchor must be a MEMORY the possessor label identifies UNIQUELY. Two entities aliased
    ``diane`` means the label names neither of them, and walking from an arbitrary one lands the
    write on a stranger's relative. Ablating the uniqueness test takes ``_holders[0]`` — a LIMIT-1
    pick over a non-unique column, which is the exact substitution this whole rewrite removes."""
    DIANE = "00000000-0000-0000-0000-0000000diane"
    OTHER_DIANE = "00000000-0000-0000-0000-000000diane2"
    db = _install_graph(
        monkeypatch,
        edges=[(OTHER_DIANE, "sibling_of", COLLEAGUE), (DIANE, "sibling_of", SISTER)],
        aliases={"diane": [OTHER_DIANE, DIANE]},
        ladders={},
        kin_map={"sister": "sibling_of"},
        fetch=lambda anchor, uid, path: [
            {"subject": s_, "object": o_, "rel_type": r_, "source": "db"}
            for s_, r_, o_ in [(OTHER_DIANE, "sibling_of", COLLEAGUE),
                               (DIANE, "sibling_of", SISTER)]
            if anchor in (s_, o_)],
    )
    assert m._walk_naming_clause_referent(
        db, USER, "Actually Diane's sister's name is Thorne, not Wrenna.") is None


# ── the clause's referent description, recovered from the parse ──────────────────────────────────

@pytest.mark.parametrize("text,kind,role,poss", [
    ("Actually my sister's name is Thorne, not Wrenna.", "other", "sister", "SPEAKER"),
    ("actually my sister's name is thorne, not wrenna.", "other", "sister", "SPEAKER"),
    ("actually my sisters name is thorne, not wrenna.", "other", "sister", "SPEAKER"),
    ("The name of my sister is Thorne.", "other", "sister", "SPEAKER"),
    ("My sister goes by Thorne.", "other", "sister", "SPEAKER"),
    ("My sister is called Thorne.", "other", "sister", "SPEAKER"),
    ("Actually my dog's name is Rex, not Bandit.", "other", "dog", "SPEAKER"),
    ("Actually my name is Thorne, not Wrenna.", "self", None, "SPEAKER"),
    ("Actually her name is Thorne.", "other", None, None),
    ("John's name is actually Jonathan.", "other", None, "john"),
])
def test_the_referent_description_is_recovered_from_every_realization(text, kind, role, poss):
    from src.extraction.linguistics import naming_clause_referents
    recs = naming_clause_referents(text)
    assert len(recs) == 1, (text, recs)
    assert recs[0]["kind"] == kind, (text, recs)
    assert recs[0]["role"] == role, (text, recs)
    assert recs[0]["possessor"] == poss, (text, recs)


# ── THE WALK DECIDES **WHO**, NEVER **WHAT** ────────────────────────────────────────────────────
# MEASURED REGRESSION (fresh tenants, 5/5): "actually my dog's name is rex, not bandit." — the
# correction LLM answered ``dimension=RELATIONAL, old_rel_type=has_pet, new='has_pet=rex'``. With
# the referent correctly bound to the dog, the endpoint SUPERSEDED the real ``(user, has_pet, dog)``
# edge and MINTED a new entity aliased ``rex`` as the user's pet: the real dog kept its old name and
# lost its owner. Before the walk existed that turn was REFUSED, so binding the right subject turned
# a refusal into a wrong write — strictly worse. A naming clause licenses a LABEL write and nothing
# else, so the fallback door opens only when the extraction is already in the label/scalar lane
# (``tail_types`` SCALAR, from ``rel_types`` metadata — no rel-name literal).

def test_a_relational_extraction_is_not_a_label_write(monkeypatch):
    """The metadata test the fallback door gates on, exercised directly: a rel whose tail is an
    ENTITY is not a label write, and a naming clause never licenses superseding an edge."""
    monkeypatch.setattr(m, "_rel_meta", lambda r: {
        "pref_name": {"tail_types": ["SCALAR"]},
        "also_known_as": {"tail_types": ["SCALAR"]},
        "has_pet": {"tail_types": ["Animal"]},
        "sibling_of": {"tail_types": ["Person"]},
    }.get((r or "").lower(), {}))

    def _is_label_lane(*rels):
        return any(any(str(x).upper() == "SCALAR"
                       for x in ((m._rel_meta((r or "").lower().strip()) or {}).get("tail_types") or []))
                   for r in rels)

    assert _is_label_lane("pref_name", "pref_name")
    assert _is_label_lane("also_known_as", "")
    assert not _is_label_lane("has_pet", "has_pet")
    assert not _is_label_lane("sibling_of", "sibling_of")
    assert not _is_label_lane("", "")


def test_the_published_naming_frames_contract_is_unchanged():
    """``naming_frames`` still yields exactly four elements per clause — every existing caller
    unpacks that shape, and the referent description rides a SEPARATE projection."""
    from src.extraction.linguistics import naming_frames, naming_clause_referents
    text = "Actually my sister's name is Thorne, not Wrenna."
    frames = naming_frames(text)
    assert frames and all(len(f) == 4 for f in frames), frames
    kind, surfaces, labels, predicated = frames[0]
    rec = naming_clause_referents(text)[0]
    assert (kind, surfaces, labels, predicated) == (
        rec["kind"], rec["surfaces"], rec["labels"], rec["predicated"])


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE INGEST DOOR: RESOLVE THE REFERENT INSTEAD OF DELETING THE EDGE
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# The drop this replaces was right about the diagnosis and wrong about the remedy, and it cost three
# MEASURED defects (5/5 each, on fresh tenants, in BOTH arms of a before/after sweep):
#   1. `REJECTED_LABEL_MINTED_vale` + `FABRICATED_ROLE_HOLDER` — a MULTI-TOKEN rejected name was
#      never detected, so "…not Wrenna Vale." minted a fabricated sibling under the denied name.
#   2. an ordinary "John's name is actually Jonathan." could flag its own ASSERTED name, because the
#      rejected set was built from ALL of a clause's labels including the REFERRING one.
#   3. `ASSERTED_CONTENT_LOST_toronto` — the drop is TURN-scoped, so the clarification the refusal
#      invites the user to type was deleted along with the repair.
# All three are the same shape: a rule keyed on WHICH LABEL rather than on WHICH REFERENT.

def _rebind(monkeypatch, text, **graph):
    pref = graph.pop("_pref", {})
    db = _install_graph(monkeypatch, **graph)
    monkeypatch.setattr(m, "EntityRegistry", lambda _db: type(
        "R", (), {"get_preferred_name": staticmethod(lambda e: pref.get(e, ""))})())
    r, u, _files = m._naming_clause_referent_rebinds(db, USER, text)
    return r, u


_SISTER_REBIND = dict(
    edges=[(USER, "sibling_of", SISTER)],
    aliases={"sister": [SISTER_NODE], "wrenna": [SISTER]},
    ladders={SISTER: [SISTER_NODE]},
    places=[SISTER_NODE],
    kin_map={"sister": "sibling_of"},
    _pref={SISTER: "wrenna"},
)


def test_both_names_of_a_naming_clause_bind_to_the_one_referent(monkeypatch):
    """A naming clause is ABOUT ONE ENTITY, so the asserted name and the rejected one are two
    labels of the same sister — binding either to her is correct. Not having to decide WHICH label
    was rejected is exactly what removes this lane's dependence on a slot the parser INVERTS across
    letter case, which is where defects 1 and 2 came from."""
    rebinds, _ = _rebind(monkeypatch, "Actually my sister's name is Thorne, not Wrenna.",
                         **_SISTER_REBIND)
    assert rebinds.get("thorne") == "wrenna", rebinds
    assert rebinds.get("wrenna") == "wrenna", rebinds


def test_every_part_of_a_multi_token_name_is_rebound(monkeypatch):
    """DEFECT 1, closed at the root. The old scan compared the deriver's edge SUBJECT — the LAST
    TOKEN of a name — against the JOINED label, so a two-token rejected name matched nothing and a
    fabricated relative was minted under it. A resolution needs no such comparison: every part of
    the clause's names denotes the clause's referent."""
    rebinds, _ = _rebind(monkeypatch, "Actually my sister's name is Thorne, not Wrenna Vale.",
                         **_SISTER_REBIND)
    for part in ("vale", "wrenna", "wrenna vale", "thorne"):
        assert rebinds.get(part) == "wrenna", (part, rebinds)


def test_an_ordinary_naming_statement_binds_to_its_own_bearer(monkeypatch):
    """DEFECT 2. "John's name is actually Jonathan." refers to its bearer by his OWN label, so the
    zero-hop referring-possessor path resolves him and BOTH labels bind to him — nothing is flagged,
    nothing is deleted."""
    JOHN = "00000000-0000-0000-0000-0000000ojohn"
    rebinds, _ = _rebind(
        monkeypatch, "John's name is actually Jonathan.",
        edges=[(USER, "works_with", JOHN)], aliases={"john": [JOHN]}, ladders={},
        _pref={JOHN: "john"})
    assert rebinds.get("john") == "john" and rebinds.get("jonathan") == "john", rebinds


def test_a_surface_two_clauses_both_claim_is_never_rebound(monkeypatch):
    """"…my sister's name is Thorne, not Wrenna. …my brother's name is Wrenna, not Kai." gives
    ``wrenna`` to BOTH clauses. A predicate that cannot say which referent a surface names must not
    answer for either — it falls through to the existing drop, which is loud, lossy and safe."""
    BROTHER = "00000000-0000-0000-0000-000000brothr"
    BROTHER_NODE = "00000000-0000-0000-0000-00000bnode"
    rebinds, undecided = _rebind(
        monkeypatch,
        "Actually my sister's name is Thorne, not Wrenna. "
        "Actually my brother's name is Wrenna, not Kai.",
        edges=[(USER, "sibling_of", SISTER), (USER, "sibling_of", BROTHER)],
        aliases={"sister": [SISTER_NODE], "brother": [BROTHER_NODE]},
        ladders={SISTER: [SISTER_NODE], BROTHER: [BROTHER_NODE]},
        places=[SISTER_NODE, BROTHER_NODE],
        kin_map={"sister": "sibling_of", "brother": "sibling_of"},
        _pref={SISTER: "wrenna", BROTHER: "kai"})
    assert "wrenna" not in rebinds and "wrenna" in undecided, (rebinds, undecided)
    assert rebinds.get("thorne") == "wrenna" and rebinds.get("kai") == "kai", rebinds


def test_a_clause_the_walk_cannot_resolve_contributes_no_rebind(monkeypatch):
    """FAIL LOUD, NEVER GUESS. With no sister in the graph the walk declines, so the clause offers
    no binding at all and its edges meet the UNCHANGED drop. A resolution lane that fabricated a
    referent here would be strictly worse than the deletion it replaces."""
    rebinds, undecided = _rebind(
        monkeypatch, "Actually my sister's name is Thorne, not Wrenna.",
        edges=[(USER, "works_with", COLLEAGUE)], aliases={"wrenna": [COLLEAGUE]},
        ladders={}, kin_map={"sister": "sibling_of"}, _pref={COLLEAGUE: "wrenna"})
    assert rebinds == {} and "thorne" in undecided, (rebinds, undecided)


def test_a_referent_with_no_usable_preferred_label_is_not_rebound(monkeypatch):
    """An edge endpoint is a SURFACE, so a referent the registry cannot name has nothing to be
    rebound TO. Inventing one would file the turn's facts under a label nobody holds."""
    rebinds, undecided = _rebind(monkeypatch, "Actually my sister's name is Thorne, not Wrenna.",
                                 **{**_SISTER_REBIND, "_pref": {SISTER: ""}})
    assert rebinds == {} and undecided, (rebinds, undecided)


def test_the_ingest_resolve_is_off_byte_for_byte_behind_its_flag(monkeypatch):
    monkeypatch.setattr(m, "NAME_INGEST_REFERENT_RESOLVE", False)
    assert _rebind(monkeypatch, "Actually my sister's name is Thorne, not Wrenna.",
                   **_SISTER_REBIND) == ({}, set())


@pytest.mark.parametrize("text", [
    "My favorite color is red, not blue.",
    "Actually my mother's age is 63, not 62.",
    "My user name is bob.",
    "",
])
def test_a_turn_with_no_naming_clause_yields_no_rebinds(monkeypatch, text):
    assert _rebind(monkeypatch, text, **_SISTER_REBIND) == ({}, set()), text


# ── A NAME NEVER BECOMES A PLACE, AND CASING CANNOT DECIDE A REFERENT ────────────────────────────
# MEASURED, 5/5 each: "Actually my dog's name is Rex, not Bandit." landed on the dog, while its
# byte-identical LOWER-CASE twin minted a fresh entity for `rex` and refused. The seeded graph was
# the same and the walk's inputs were the same — role='dog', possessor=SPEAKER, and the same clause
# token surfaces INCLUDING `rex`. Only ``labels`` differed ({bandit, rex} vs {bandit}), because the
# parser inverts the complement slots in lower case. Keying the rebind on ``labels`` therefore made
# the OUTCOME depend on CAPITALISATION. No step of `possessor → has_pet → {candidates} →
# instance_of dog` reads a name string, so no step of it may depend on letter case.

class _RebindDb(_WalkDb):
    """A registry that also answers "is this surface a stored alias?" — the case-independent
    question that replaces the label-set membership test."""

    def __init__(self, aliases, known):
        super().__init__(aliases)
        self._known = {k.lower() for k in known}

    def cursor(self):
        outer = self

        class _C(_WalkDbCur):
            def __init__(self):
                super().__init__(outer._aliases)
                self._sql = ""

            def execute(self, sql, params=None):
                self._sql = sql
                super().execute(sql, params)
                self._p = (params or (None,))[0]

            def fetchone(self):
                if "SELECT 1 FROM entity_aliases" in self._sql:
                    return (1,) if str(self._p).lower() in outer._known else None
                return None

            def fetchall(self):
                if "wikidata_pid" in self._sql:
                    return [("also_known_as",)]
                return super().fetchall()
        return _C()


def _pet_graph(monkeypatch, known):
    # the anti-mint arm is DEFAULTED OFF (it regresses a multi-sentence turn — see the flag's
    # comment); these pins exercise it explicitly so the built behaviour stays measured.
    monkeypatch.setattr(m, "NAME_INGEST_ANTIMINT", True)
    monkeypatch.setattr(m, "fetch_facts_from_anchor", lambda a, u, p, **k: [
        {"subject": USER, "object": DOG, "rel_type": "has_pet", "source": "db"},
        {"subject": USER, "object": CAT, "rel_type": "has_pet", "source": "db"}])
    monkeypatch.setattr(m, "_entity_is_l4_place", lambda db, e: e in (DOG_NODE, CAT_NODE))
    _ladder = lambda db, u, e, direction="up", max_depth=3: (  # noqa: E731
        {e} | {DOG: {DOG_NODE}, CAT: {CAT_NODE}}.get(e, set()))
    monkeypatch.setattr(m, "_hierarchy_expand", _ladder)
    # WALK_QUERY_BATCHING twin (see _install_graph).
    monkeypatch.setattr(m, "_hierarchy_expand_batch",
                        lambda db, u, ids, direction="up", max_depth=3:
                        {e: _ladder(db, u, e, direction, max_depth) for e in ids})
    monkeypatch.setattr(m.linguistic_cue_overlay, "resolve_kinship_rel_map", lambda dsn: {})
    monkeypatch.setattr(m, "_rel_meta", lambda r: {"inverse_rel_type": ""})
    monkeypatch.setattr(m, "EntityRegistry", lambda _db: type(
        "R", (), {"get_preferred_name": staticmethod(
            lambda e: {DOG: "bandit", CAT: "bandit"}.get(e, ""))})())
    return _RebindDb({"dog": [DOG_NODE], "cat": [CAT_NODE]}, known)


class _E2:
    def __init__(self, s, r, o):
        self.subject, self.rel_type, self.object = s, r, o


@pytest.mark.parametrize("text", [
    "Actually my dog's name is Rex, not Bandit.",
    "actually my dog's name is rex, not bandit.",
])
def test_the_new_name_is_never_minted_and_lands_on_the_walked_pet(monkeypatch, text):
    """BOTH CASINGS, ONE OUTCOME. ``rex`` is not a stored alias, so ingesting it as an edge endpoint
    would MINT a node for a NAME — the founding violation. It is bound to the pet the walk reached
    and FILED there instead. The lower-case row is the one that used to mint."""
    db = _pet_graph(monkeypatch, known={"dog", "cat", "bandit", "user"})
    edges = [_E2(USER, "related_to", "rex"), _E2("bandit", "related_to", USER)]
    rebinds, _und, files = m._naming_clause_referent_rebinds(db, USER, text, edges=edges)
    assert rebinds.get("rex") == "bandit", (text, rebinds)
    assert files == [("bandit", "also_known_as", "rex")], (text, files)


def test_a_stored_type_node_surface_is_never_rebound(monkeypatch):
    """THE HARD LINE, the other way round. ``dog`` is a clause token AND an edge endpoint, but the
    registry knows it — it is the L4 type node, not a name being assigned. Rebinding it onto the pet
    would file a PLACE as the pet's label. The registry lookup is the whole discriminator."""
    db = _pet_graph(monkeypatch, known={"dog", "cat", "bandit", "user"})
    edges = [_E2("bandit", "instance_of", "dog"), _E2(USER, "related_to", "rex")]
    rebinds, _und, _f = m._naming_clause_referent_rebinds(
        db, USER, "actually my dog's name is rex, not bandit.", edges=edges)
    assert "dog" not in rebinds and rebinds.get("rex") == "bandit", rebinds


def test_no_referent_means_no_rebind_and_nothing_filed(monkeypatch):
    """The temptation is highest exactly here. With no pet reachable the walk declines, so the turn
    gets NO binding and NO name file — it refuses, and it still does not mint."""
    monkeypatch.setattr(m, "fetch_facts_from_anchor", lambda a, u, p, **k: [])
    monkeypatch.setattr(m, "_entity_is_l4_place", lambda db, e: False)
    _ladder = lambda db, u, e, direction="up", max_depth=3: {e}  # noqa: E731
    monkeypatch.setattr(m, "_hierarchy_expand", _ladder)
    monkeypatch.setattr(m, "_hierarchy_expand_batch",
                        lambda db, u, ids, direction="up", max_depth=3:
                        {e: _ladder(db, u, e, direction, max_depth) for e in ids})
    monkeypatch.setattr(m.linguistic_cue_overlay, "resolve_kinship_rel_map", lambda dsn: {})
    monkeypatch.setattr(m, "_rel_meta", lambda r: {"inverse_rel_type": ""})
    monkeypatch.setattr(m, "EntityRegistry", lambda _db: type(
        "R", (), {"get_preferred_name": staticmethod(lambda e: "")})())
    db = _RebindDb({}, known={"user"})
    rebinds, _und, files = m._naming_clause_referent_rebinds(
        db, USER, "actually my dog's name is rex, not bandit.",
        edges=[_E2(USER, "related_to", "rex")])
    assert rebinds == {} and files == [], (rebinds, files)


def test_two_unresolvable_new_surfaces_file_nothing(monkeypatch):
    """Which surface is the NEW name is settled by the GRAPH — the referent already holds its old
    label, so the one it does NOT hold is the assignment. That reasoning needs EXACTLY ONE unknown
    surface; with two, the turn says nothing rather than guessing which is the name."""
    db = _pet_graph(monkeypatch, known={"dog", "cat", "user"})   # `bandit` unknown too
    edges = [_E2(USER, "related_to", "rex"), _E2("bandit", "related_to", USER)]
    _r, _u, files = m._naming_clause_referent_rebinds(
        db, USER, "actually my dog's name is rex, not bandit.", edges=edges)
    assert files == [], files

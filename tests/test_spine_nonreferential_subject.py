"""NON-REFERENTIAL SUBJECT GATE — refuse to mint a pronoun that has no referent.

PURE tests — no DB, no network, no GLiNER2 model call, no LLM. They exercise
``derive_sentence_facts`` (the real spine deriver) over junk + control constructions and assert on
the emitted (subject, rel_type, object) triples.

WHY THIS FILE EXISTS (the bug it pins). ``_chain_copula_state`` excluded only FIRST-PERSON pronoun
subjects, so a discourse-deictic demonstrative (``that``/``this``) and an anticipatory/extraposed
``it`` — nominals that by definition have NO referent in the atom — sailed through as ``nsubj`` and
got minted as graph nodes, dragging the predicate adjective in as a state off nothing::

    'That is great to hear!' -> [('that','has_state','great')]     JUNK
    'The server is down'     -> [('server','has_state','down')]    CORRECT — SAME CHAIN

The discriminator is the SUBJECT's referentiality, not the object. The fix
(``SPINE_NONREFERENTIAL_SUBJECT_GATE``, applied at the ``_emit`` chokepoint so it covers EVERY chain
— copula-state, intransitive, locative, …) refuses the two non-referential classes; it does NOT touch
nominal subjects, self subjects, anaphoric he/she/they, or bare anaphoric ``it`` (which need
coreference resolution, a separate piece of work). ``you`` (deictic addressee) is a THIRD class this
gate deliberately does NOT reach — it needs its own product decision and is asserted here as a
SCOPE BOUNDARY, not silently folded in. See the internal design record §6.3.

Run:  SPACY_MODEL=en_core_web_sm python3 -m pytest tests/test_spine_nonreferential_subject.py -q
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as L  # noqa: E402

_REF = "2026-07-16"


def _triples(sentence):
    return [(f.subject, f.rel_type, f.object) for f in L.derive_sentence_facts(sentence, _REF)]


def _has(triples, subj, rel, obj=None):
    return any(s == subj and r == rel and (obj is None or o == obj)
               for s, r, o in triples)


# ── CLASS 1: discourse-deictic demonstrative subject — REFUSED ─────────────────────────────────


@pytest.mark.parametrize("sentence", [
    "That's great to hear!",
    "That was really helpful.",
    "This is interesting.",
    "This is fascinating to see.",
])
def test_demonstrative_subject_is_not_minted(sentence):
    """A bare demonstrative subject's referent is a preceding PROPOSITION outside the atom — never a
    node. No edge may carry a demonstrative surface as its subject."""
    trip = _triples(sentence)
    assert not _has(trip, "that", "has_state") , f"junk demonstrative minted: {trip}"
    assert not _has(trip, "this", "has_state"), f"junk demonstrative minted: {trip}"
    # and the demonstrative must not become the subject of ANY edge
    assert not any(s in ("that", "this") for s, _, _ in trip), \
        f"a demonstrative pronoun became a graph node: {trip}"


# ── CLASS 2: anticipatory / extraposed `it` subject — REFUSED ──────────────────────────────────


@pytest.mark.parametrize("sentence", [
    "It is clear that we should decline.",          # ccomp off the copula directly
    "It is nice to have a garden.",                 # xcomp off the adjectival complement
    "It's important to verify the numbers.",        # xcomp off acomp
    "It's inspiring to see the progress.",          # xcomp off acomp
])
def test_extraposition_it_subject_is_not_minted(sentence):
    """Anticipatory `it` (the real subject is the trailing clause) is non-referential — never a node.
    Grounded in UD `expl`; detected structurally because spaCy tags these `nsubj`, not `expl`."""
    trip = _triples(sentence)
    assert not any(s == "it" for s, _, _ in trip), \
        f"anticipatory `it` became a graph node: {trip}"


def test_extraposition_it_subject_refused_on_the_locative_sibling_chain():
    """The ARM-2 'enumerate siblings' lesson, live: the locative ``located_in`` chain ALSO takes a
    copular pronoun subject. Before the chokepoint guard it minted ``('it','located_in','aerospace
    industry')`` from the extraposed frame — the gate must refuse it at ``_emit`` regardless of which
    chain emitted."""
    trip = _triples(
        "It's inspiring to see how much potential there is for innovation and "
        "sustainability in the aerospace industry.")
    assert not _has(trip, "it", "located_in"), \
        f"anticipatory `it` minted via the locative sibling: {trip}"


# ── CONTROLS: legitimate captures MUST survive (zero false rejects) ────────────────────────────


def test_nominal_subject_state_survives():
    """A NOMINAL subject is never matched by the gate (requires ``PRON``); the canonical copular state
    is byte-for-byte unchanged. This is the load-bearing control — 'the server is down' is the SAME
    chain the junk came from."""
    assert _has(_triples("The server is down."), "server", "has_state", "down")
    assert _has(_triples("The food was great."), "food", "has_state", "great")


def test_first_person_still_routes_to_feelings_not_state():
    """Self subjects remain excluded UPSTREAM (the existing first-person check) → the feeling seam,
    not a copular STATE. The gate does not regress that."""
    # "I am worried" is owned by analyze_copula → feels; it must NOT emit (user, has_state, worried)
    trip = _triples("I am worried.")
    assert not _has(trip, "user", "has_state", "worried"), \
        f"first-person state regressed onto has_state: {trip}"


def test_bare_anaphoric_it_survives():
    """Bare anaphoric `it` with NO extraposed clause is a referring expression (needs coreference
    resolution) — explicitly OUT OF SCOPE per the diagnosis. The gate must NOT refuse it."""
    trip = _triples("I have a mixer. It is broken.")
    # the `it` may or may not coref-resolve to `mixer` (a separate gap), but it must NOT be refused
    assert _has(trip, "it", "has_state", "break") or _has(trip, "mixer", "has_state", "break"), \
        f"bare anaphoric `it` was wrongly refused: {trip}"


def test_third_person_personal_pronouns_survive():
    """he/she/they are NON-NEUTER 3rd-person personal pronouns — the extraposition disjunct requires
    Gender=Neut, so they are UNREACHED and remain anaphoric (owned by coref), never refused."""
    assert _has(_triples("Sarah is happy."), "sarah", "has_state", "happy")
    # `he` with an xcomp complement ("he is eager to leave") must NOT trip the extraposition disjunct
    trip = _triples("He is eager to leave.")
    assert _has(trip, "he", "has_state", "eager"), \
        f"`he` (non-neuter) was wrongly refused by the extraposition disjunct: {trip}"


def test_negated_nominal_state_survives():
    """A negated genuine state is captured negated (assertion polarity) — the gate must not interact."""
    assert _has(_triples("The server is not down."), "server", "has_state", "down")


def test_intransitive_state_survives():
    """The intransitive-state chain (a content verb + subject, no object) with a NOMINAL subject is
    unchanged — the gate only inspects PRON subjects."""
    trip = _triples("My car broke last week.")
    assert _has(trip, "car", "has_state", "break"), f"nominal intransitive state lost: {trip}"


# ── `you` (2nd-person deictic addressee) → SEAT — FLAG-GATED, DEFAULT **OFF** ────────────────────
#
# ⚠️ CORRECTED 2026-08-12 (adversarial review). This section header and the first docstring below
# both read "OWNER DECISION 2026-08-11". **THERE WAS NO SUCH OWNER DECISION** — the same fabricated
# attribution that was corrected in `src/extraction/linguistics.py` survived HERE because `tests/`
# is gitignored, so it is invisible to `git diff` and to any review that reads only the tracked
# diff. It is stripped rather than reworded: the record of the fabrication lives in the flag's own
# comment in `linguistics.py`.
#
# The two tests below asserted the flag's ON behaviour WITHOUT setting the flag, so when the default
# was flipped to `false` on 2026-08-12 they both went RED. They now set it EXPLICITLY: they pin the
# MECHANISM (which is sound and is kept), not the default. `test_the_seat_binding_default_is_off…`
# pins the default itself, so a future silent re-flip is caught by a test rather than in production.


def test_you_deictic_addressee_resolves_to_seat(monkeypatch):
    """WITH THE FLAG ON, a bare 2nd-person SUBJECT resolves to the SAME "user" target as first-person
    ``I``/``me`` and is never minted as a standalone ``you`` node (on prod ``you`` is typed Person
    with 293 edge rows, live on the owner's seat). The tenant/account is NEVER a binding target.
    This pins the MECHANISM only — whether the mechanism SHOULD be on is an open owner question, and
    the default is asserted separately below."""
    monkeypatch.setattr(L, "SPINE_SECOND_PERSON_SEAT_BINDING", True)
    trip = _triples("You are really helpful.")
    assert _has(trip, "user", "has_state", "helpful"), \
        f"`you` did not resolve to the seat: {trip}"
    assert not _has(trip, "you", "has_state"), f"`you` was minted as a standalone node: {trip}"


def test_you_as_transitive_subject_also_resolves_to_seat(monkeypatch):
    """With the flag ON, seat resolution holds regardless of the predicate (transitive too)."""
    monkeypatch.setattr(L, "SPINE_SECOND_PERSON_SEAT_BINDING", True)
    trip = _triples("You always recommend the best tools.")
    assert _has(trip, "user", "recommend"), f"`you` (transitive subject) did not resolve to seat: {trip}"
    assert not any(s == "you" for s, _, _ in trip), f"`you` minted as a node: {trip}"


def test_the_seat_binding_default_is_off_pending_an_owner_ruling():
    """THE DEFAULT IS OFF, AND THIS TEST EXISTS SO A SILENT RE-FLIP IS CAUGHT HERE, NOT IN PRODUCTION.

    On the primary ingest path the USER's own message is what is captured (`remember_facts` passes
    the user's text verbatim), so in a user-authored turn the addressee of ``you`` is the ASSISTANT,
    not the seat. Binding it to the seat writes a FALSE FACT ABOUT THE USER. The disqualifying case
    is not the flattering one — ``You are really helpful.`` looks harmless either way — it is the
    CLASSIFICATION, which files the human into L4 as an instance of a machine. Production already
    holds a separate ``you`` entity carrying ``you -> helpful assistant`` / ``-> teacher``; with the
    binding on, those weld onto the seat user.

    Pick the adversarial sentence, not the friendly one."""
    assert L.SPINE_SECOND_PERSON_SEAT_BINDING is False, \
        "SPINE_SECOND_PERSON_SEAT_BINDING must default OFF until the owner rules on the binding target"
    trip = _triples("You are a language model.")
    assert not any(s == "user" for s, _, _ in trip), \
        f"a statement ABOUT THE ASSISTANT was filed as a classification OF THE USER: {trip}"


def test_possessive_your_is_not_the_addressee():
    """Possessive "your" (Poss=Yes) carries a different referent and is owned by the possessive chain
    ("your dog" → the dog). The seat resolution must NOT swallow it — "your dog is cute" stays about
    the dog, not the user."""
    trip = _triples("Your dog is cute.")
    assert _has(trip, "dog", "has_state", "cute"), \
        f"possessive `your` was wrongly folded into the seat: {trip}"


def test_you_seat_resolution_off_is_legacy(monkeypatch):
    """``SPINE_SECOND_PERSON_SEAT_BINDING=false`` reproduces today's minting (``you`` becomes a node).
    The rollback lever, and the proof the flag gates the whole effect."""
    monkeypatch.setattr(L, "SPINE_SECOND_PERSON_SEAT_BINDING", False)
    trip = _triples("You are really helpful.")
    assert _has(trip, "you", "has_state", "helpful"), \
        f"flag OFF did not restore legacy `you` minting: {trip}"


# ── the flag-OFF contract: byte-for-byte legacy ────────────────────────────────────────────────


def test_gate_off_is_byte_for_byte_legacy(monkeypatch):
    """``SPINE_NONREFERENTIAL_SUBJECT_GATE=false`` reproduces today's minting exactly (the junk
    returns). This is the rollback lever and the proof the flag gates the whole effect."""
    monkeypatch.setattr(L, "SPINE_NONREFERENTIAL_SUBJECT_GATE", False)
    trip = _triples("That's great to hear!")
    assert _has(trip, "that", "has_state", "great"), \
        f"flag OFF did not restore legacy junk minting: {trip}"
    trip2 = _triples("It is clear that we should decline.")
    assert _has(trip2, "it", "has_state", "clear"), \
        f"flag OFF did not restore legacy junk minting: {trip2}"

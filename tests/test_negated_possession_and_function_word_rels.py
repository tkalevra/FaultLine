"""Regression tests for two MEASURED defects, both reproduced end-to-end before the fix.

BUG 1 — a user's negation of a possession was silently DISCARDED, and worse, counted as a
RE-CONFIRMATION. Measured on a clean local tenant:

    "Aurora is my dog."      -> facts: has_pet user -> aurora   polarity=affirmed
    "Aurora is not my dog."  -> facts: has_pet user -> aurora   polarity=affirmed  (committed:1)

Nothing superseded, nothing archived, and recall still asserted the pet in the CONFIDENT band.
Root cause: ``_chain_possessive`` reads the ``my X`` NP in isolation and never consults the clause
it sits in, so it emitted an identical ``negated=False`` fact for the affirmative and the denial.
The negated copula/state chains already handled negation correctly, and the SVO chain correctly
emits nothing under negation — only the PRESUPPOSED possessive/kinship leg was blind to it.

The fix is scoped by GRAMMAR, not a word list: the possession is marked negated ONLY when the
possessed noun is the DIRECT PREDICATIVE COMPLEMENT of a predicate carrying a ``neg`` dependency.
Everywhere else the possession is a PRESUPPOSITION that PROJECTS THROUGH the negation and stays
asserted (Karttunen 1973, Linguistic Inquiry 4(2)) — "My dog is not sick" still means I have a dog.

BUG 2 — a negation particle was being MINTED as a rel_type. Measured: POSTing an edge with
``rel_type="not"`` created a ``rel_types`` row (source='engine', engine_generated=true) and
committed the Class-A fact ``(aurora, not, dog)`` stored with ``polarity='affirmed'``. Growth now
refuses a candidate whose tokens are ALL closed-class (function) words, per the UD open/closed POS
split — and only ever for a rel_type the system does not already know.

PURE tests — no DB, no network, no GLiNER2, no LLM.

Run: python3 tools/fltest.py --bug FOSSNEG --test tests/test_negated_possession_and_function_word_rels.py
     (tests/ is gitignored -> git add -f)
"""
import os
from datetime import datetime

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import (  # noqa: E402
    derive_sentence_facts,
    linguistics_available,
)

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy model unavailable — the deriver no-ops without it (env failure, not a product failure)",
)

REF = datetime(2026, 8, 3)


def _facts(sentence):
    return derive_sentence_facts(sentence, REF, None)


def _find(facts, rel_substr):
    """The emitted fact whose rel_type contains ``rel_substr``, or None."""
    for f in facts:
        if rel_substr in (f.rel_type or ""):
            return f
    return None


# ── BUG 1: the denial must be captured as a NEGATED edge ────────────────────────────────────────

def test_negated_predicative_possession_is_marked_negated():
    """THE REPRO. "Aurora is not my dog" must not emit the affirmative edge.

    Before the fix this returned negated=False — byte-identical to the affirmative sentence — so
    ingest re-committed the affirmed row and the user's correction had zero effect."""
    f = _find(_facts("Aurora is not my dog."), "owns")
    assert f is not None, "the possession edge must still be CAPTURED, only its polarity changes"
    assert f.negated is True


def test_affirmative_possession_stays_affirmed():
    """The control arm: the affirmative must be untouched by the fix."""
    f = _find(_facts("Aurora is my dog."), "owns")
    assert f is not None
    assert f.negated is False


def test_negated_predicative_kinship_is_marked_negated():
    """The kin leg of the same chain: "Sarah is not my sister" denies the tie."""
    f = _find(_facts("Sarah is not my sister."), "sibling_of")
    assert f is not None
    assert f.negated is True


def test_affirmative_kinship_stays_affirmed():
    f = _find(_facts("Sarah is my sister."), "sibling_of")
    assert f is not None
    assert f.negated is False


# ── BUG 1, the other half: presupposition PROJECTION must not be over-negated ───────────────────
# These are the cases a blunt "clause contains a neg -> negate everything" fix would corrupt.

def test_presupposition_projects_when_possessum_is_the_subject():
    """"My dog is not sick" — I STILL HAVE A DOG. Only the state is denied.

    The possessum is the ``nsubj``, outside the scope of the predicate negation."""
    facts = _facts("My dog is not sick.")
    owns = _find(facts, "owns")
    assert owns is not None
    assert owns.negated is False, "possession presupposition must PROJECT through the negation"
    state = _find(facts, "has_state")
    assert state is not None and state.negated is True, "the STATE is what is denied"


def test_presupposition_projects_from_a_nested_complement():
    """"my pets are not part of my family" — I still have pets AND a family.

    ``family`` is a ``pobj`` nested under the "part of" complement, not the complement head, so
    the possession is not what the clause denies."""
    facts = _facts("my pets are not part of my family")
    for f in facts:
        if "owns" in (f.rel_type or ""):
            assert f.negated is False, f"possession {f.object!r} must stay affirmed"


def test_kinship_presupposition_projects_when_role_is_the_subject():
    """"My mother is not happy" — she is still my mother."""
    facts = _facts("My mother is not happy.")
    kin = _find(facts, "parent_of")
    assert kin is not None
    assert kin.negated is False


def test_unnegated_clause_never_marks_negated():
    """Belt-and-suspenders: no ``neg`` anywhere -> nothing is ever flipped."""
    for sentence in ("I have my laptop.", "That is my car.", "My sister is happy."):
        for f in _facts(sentence):
            assert f.negated is False, f"{sentence!r} carries no negation"


# ── BUG 2: growth must refuse a closed-class relation name ──────────────────────────────────────

def test_negation_particle_is_refused_as_a_rel_type():
    """THE REPRO. ``not`` named no predicate yet became a permanent ontology entry."""
    import src.api.main as m
    assert m._rel_type_is_function_word_only("not") is True


def test_other_function_words_are_refused():
    """Any all-closed-class candidate — not a negation word list."""
    import src.api.main as m
    for candidate in ("is", "of", "and", "that", "the", "n't", "neither"):
        assert m._rel_type_is_function_word_only(candidate) is True, candidate


def test_content_bearing_rel_types_are_admitted():
    """A rel_type carrying ANY open-class token is content-bearing and must pass.

    ``has_pet``/``instance_of``/``part_of`` all contain closed-class tokens; the rule is
    ALL-function-words, never any-function-word."""
    import src.api.main as m
    for candidate in ("has_pet", "instance_of", "subclass_of", "part_of", "same_as",
                      "also_known_as", "related_to", "lives_in", "works_for", "born_on",
                      "favorite_colour", "buy_movies", "has_ip"):
        assert m._rel_type_is_function_word_only(candidate) is False, candidate


def test_an_already_known_rel_type_is_never_dropped():
    """SAFETY. The guard constrains GROWTH only — a rel the overlay resolves is never examined.

    ``is_a`` is all-closed-class ('is' AUX + 'a' PRON) AND is source='builtin' in the public seed.
    It must survive, because the filter skips any rel_type ``_rel_meta`` can resolve. If this goes
    red, the known-rel exemption was removed and seeded ontology is now droppable."""
    import src.api.main as m

    class _E:
        def __init__(self, rt):
            self.rel_type, self.subject, self.object = rt, "x", "y"

    known = {"is_a": {"rel_type": "is_a"}}
    original = m._rel_meta
    try:
        m._rel_meta = lambda rt=None: (known.get(rt) if rt is not None else known)
        kept = m._drop_function_word_rel_edges([_E("is_a"), _E("not")])
    finally:
        m._rel_meta = original
    kept_rels = [e.rel_type for e in kept]
    assert "is_a" in kept_rels, "a KNOWN all-function-word rel must be exempt"
    assert "not" not in kept_rels, "an UNKNOWN all-function-word rel must be refused"


def test_guard_is_disableable_and_fails_open():
    """Flag OFF -> byte-identical legacy admission."""
    import src.api.main as m
    original = m.REL_TYPE_FUNCTION_WORD_GUARD
    try:
        m.REL_TYPE_FUNCTION_WORD_GUARD = False
        m._rel_type_is_function_word_only.cache_clear()
        assert m._rel_type_is_function_word_only("not") is False
    finally:
        m.REL_TYPE_FUNCTION_WORD_GUARD = original
        m._rel_type_is_function_word_only.cache_clear()


# ── RENDER: a negated possession must read as English ───────────────────────────────────────────

def test_lexical_verb_negation_uses_do_support():
    """The fix above made this path reachable and it rendered "You HAVE NOT a pet".

    English negates a LEXICAL verb with do-support (Huddleston & Pullum, CGEL 2002 ch.3 §1.3)."""
    import src.api.main as m
    assert m._negate_prose("You have a pet that is Dog") == "You do not have a pet that is Dog"
    assert m._negate_prose("Diane lives in Toronto") == "Diane does not live in Toronto"
    assert m._negate_prose("You had a pet") == "You did not have a pet"


def test_auxiliary_negation_is_unchanged():
    """A genuine auxiliary/copula still takes "not" directly — the legacy path, byte-identical."""
    import src.api.main as m
    assert m._negate_prose("The gps system is functioning") == "The gps system is not functioning"
    assert m._negate_prose("You have eaten lunch") == "You have not eaten lunch"
    assert m._negate_prose("You will attend the meeting") == "You will not attend the meeting"


def test_already_negated_prose_is_not_double_negated():
    import src.api.main as m
    assert m._negate_prose("You are not sick") == "You are not sick"


def test_past_tense_lexical_verb_takes_the_bare_form_not_the_past_form():
    """MEASURED USER-VISIBLE DEFECT (dev line): recall rendered "Diane did not LIVED in Toronto".

    The past branch correctly selected ``did``, then took the bare form from a de-conjugator
    for PRESENT-tense finite verbs that strips a trailing ``-s``. Handed a past form it
    returns it unchanged ("lived"→"lived", "went"→"went"), so the clause carried tense TWICE.
    English do-support puts the tense on the ``do`` operator and leaves the lexical verb in
    its BARE form (Huddleston & Pullum, CGEL 2002 ch.3 §1.3; Quirk et al. §3.21ff) — "did not
    live", never "did not lived".

    This line was written with the lemma from the start (the de-conjugator never existed
    here), so this test PINS that, it does not change it. The bare form is the finite token's
    own lemma off the same parse, so it is correct for regular AND irregular pasts, which a
    spelling rule can never be."""
    import src.api.main as m
    assert m._negate_prose("Diane lived in Toronto") == "Diane did not live in Toronto"
    assert m._negate_prose("You carried the box") == "You did not carry the box"
    # Irregular past — no suffix rule could ever recover the bare form from the surface.
    assert m._negate_prose("Diane went to Toronto") == "Diane did not go to Toronto"


def test_present_tense_do_support_is_unchanged_by_the_lemma_switch():
    """The lemma must agree with a surface de-conjugator on every PRESENT form it handled."""
    import src.api.main as m
    assert m._negate_prose("Diane lives in Toronto") == "Diane does not live in Toronto"
    assert m._negate_prose("Diane goes to school") == "Diane does not go to school"
    assert m._negate_prose("You have a pet that is Dog") == "You do not have a pet that is Dog"


def test_lowercase_clause_still_gets_do_support():
    """A LOWERCASE clause must negate as well as its capitalised twin.

    Composed prose renders entity names from ``entity_aliases.alias``, which is LOWERCASED at
    registration (entity_registry/registry.py) and never re-capitalised for display — so the
    clause the parser actually sees is sentence-initial-lowercase. That is orthographically
    ill-formed English and therefore out-of-distribution for a model trained on
    conventionally-cased text: ``en_core_web_sm`` loses the finite verb entirely and tags
    "diane lives in toronto" as one compound NOUN chain, so do-support declined and recall read
    "It is not the case that diane lives in toronto" for a fact whose capitalised twin rendered
    correctly.

    Case restoration as a preprocessing step for degraded input is standard practice — Lita et
    al., "tRuEcasIng", ACL 2003. The retry runs ONLY when the raw parse found no finite verb,
    so every already-working clause is byte-identical by construction.

    NOTE the asserted output keeps the ORIGINAL lowercase: the casing is applied to the PARSE
    INPUT only and the returned string is spliced from the caller's own text. If this goes red
    with a capitalised subject, the normalisation has leaked into the render."""
    import src.api.main as m
    assert m._negate_prose("diane lives in toronto") == "diane does not live in toronto"
    assert m._negate_prose("marcus works for acme") == "marcus does not work for acme"
    assert m._negate_prose("sarah dislikes cilantro") == "sarah does not dislike cilantro"
    # The capitalised twin is unchanged.
    assert m._negate_prose("Diane lives in Toronto") == "Diane does not live in Toronto"


def test_verbless_clause_still_gets_the_honest_wrapper():
    """THE FAIL-SAFE MUST STAY REACHABLE. The label-fallback lane renders a rel with no learned
    template as the HONEST NEUTRAL "X {label} Y", which for a NOUN-PHRASE label is a genuinely
    VERBLESS clause — there is no verb to apply do-support to, and the clause-level wrapper is
    the CORRECT render.

    This is the guard on the sentence-case retry: a PRO-FORM SUBSTITUTION probe was measured
    and REJECTED for this exact reason (substituting a pronoun subject COERCES a verb reading,
    giving 8/8 false positives on these clauses — "apollo does not ip address 10.0.0.4").
    Sentence-casing fabricates nothing, and this test is what proves it. A wrong polarity is a
    TRUTH error; clunky prose is only a style error — never trade the second for the first."""
    import src.api.main as m
    assert m._negate_prose("apollo ip address 10.0.0.4") == (
        "It is not the case that apollo ip address 10.0.0.4"
    )
    assert m._negate_prose("you favorite color teal") == (
        "It is not the case that you favorite color teal"
    )
    # Negation is never LOST, whatever branch runs.
    for clause in ("diane lives in toronto", "apollo ip address 10.0.0.4"):
        assert " not " in m._negate_prose(clause)


def test_do_support_flag_off_restores_the_legacy_insertion():
    """FAIL-SAFE lever: flag OFF → the blunt regex path, byte-identical to pre-feature."""
    import src.api.main as m
    original = m.RENDER_NEGATION_DO_SUPPORT
    try:
        m.RENDER_NEGATION_DO_SUPPORT = False
        assert m._negate_prose("The gps system is functioning") == (
            "The gps system is not functioning"
        )
        # No _NEGATE_AUX match in a bare lexical past clause → clause-level wrapper.
        assert m._negate_prose("Diane lived in Toronto") == (
            "It is not the case that diane lived in Toronto"
        )
    finally:
        m.RENDER_NEGATION_DO_SUPPORT = original


def test_log_crit_call_sites_have_the_logger_argument():
    """FAIL-LOUD PATHS MUST NOT CRASH. ``log_crit(logger, msg, **args)`` — a call site in
    main.py omitted the logger, so the reporter raised at the exact moment it was meant to
    report a failure (TypeError: missing required positional argument 'msg').

    Pinned by AST across the whole log_* family so a future site cannot regress silently."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ("log_crit", "log_warn", "log_info", "log_debug"):
                continue
            # First positional arg is the LOGGER; a bare string literal there is the bug.
            if len(node.args) < 2 or isinstance(node.args[0], ast.Constant):
                offenders.append(f"{path}:{node.lineno} {node.func.id}")
    assert not offenders, "log_* called without a logger argument: " + ", ".join(offenders)

"""Unit tests for src/extraction/linguistics.py — the deterministic spaCy linguistic layer.

The layer is purely ADDITIVE and fail-safe: it gives a grammatical first-cut for extraction /
grounding, and degrades to a no-op (today's behavior) when the kill-switch is OFF or spaCy / the
model is unavailable. These tests cover:

  - copula complement-POS first-cut (ADJ→feels, NOUN→occupation, PROPN→also_known_as)
  - deterministic negation (neg dependency, not a regex window)
  - ambiguous complement (VERB participle) → no relation (caller routes the residue)
  - possessive head (poss dep → head noun) — "my car's GPS system" → "gps system"
  - function-word predicate POS test (in-context multi-word)
  - clause/sentence segmentation
  - FAIL-SAFE when spaCy import fails / model load fails (mocked) → benign no-op
  - KILL-SWITCH off (LINGUISTIC_LAYER=0) → no-op (caller's bespoke fallback path)

The model-backed tests skip cleanly if en_core_web_sm is not installed in the test env, so a
sandbox without the baked model still runs the fail-safe / kill-switch tests.

Run: python3 -m pytest tests/test_linguistics.py -q   (tests/ is gitignored → git add -f)
"""
import importlib
import os
import sys

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    """Reload the module under a fresh env so the kill-switch constant + singleton re-read."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _model_available() -> bool:
    """True iff a fresh, flag-ON reload can load the spaCy model in this env."""
    m = _reload(LINGUISTIC_LAYER="true")
    return m.linguistics_available()


# Skip the model-backed tests if en_core_web_sm is not installed (sandbox without the bake).
_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")


# ─────────────────────────── COPULA FIRST-CUT (complement POS) ───────────────────────────

@requires_model
def test_copula_adjective_complement_is_feeling():
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("I'm worried")
    assert c is not None
    assert c.subject_is_self is True
    assert c.complement == "worried"
    assert c.complement_pos == "ADJ"
    assert c.relation == "feels"     # ADJ → feeling
    assert c.negated is False


@requires_model
def test_copula_noun_complement_is_role():
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("I am a teacher")
    assert c is not None
    assert c.complement == "teacher"
    assert c.complement_pos == "NOUN"
    assert c.relation == "occupation"   # NOUN → role/occupation


@requires_model
def test_copula_proper_noun_complement_is_name():
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("I am Chris")
    assert c is not None
    assert c.complement == "chris"
    assert c.complement_pos == "PROPN"
    assert c.relation == "also_known_as"   # PROPN → name


@requires_model
def test_copula_my_name_is_x_is_not_self_possessive():
    # RC1 (2026-06-19): a possessive subject ("my name") is a predication ABOUT the noun,
    # NOT a 1st-person self-predication. analyze_copula must NOT promote a possessive subject
    # to self — that possessive-promotion was the feeling-layer over-fire root cause
    # ("my favorite color is blue" -> feels(blue)). Self is now grammatical: a 1st-person
    # personal pronoun (Person=1, PronType=Prs, Poss absent). Name capture for "my name is X"
    # is preserved by the identity regex (_IDENTITY_PATTERNS) + analyze_possessive_predication.
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("my name is chris")
    assert c is not None
    assert c.subject_is_self is False      # possessive subject is NOT self (RC1 fix)
    assert c.relation == "also_known_as"   # complement is PROPN -> naming relation


@requires_model
def test_copula_determiner_role_beats_propn_casing_artifact():
    """A title-cased multi-word job title ("I am a Tier 3 Systems Analyst") gets POS-tagged PROPN
    by the sm model due to capitalization — but the article ("a", a det child) marks it a
    common-noun ROLE, so the determiner override routes it to occupation, NOT also_known_as.
    This is the regression guard for the casing artifact found in testing."""
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("I am a Tier 3 Systems Analyst")
    assert c is not None
    assert c.relation == "occupation"      # determiner override beats the PROPN casing artifact
    # Sanity: a bare proper name (NO determiner) still routes to also_known_as.
    n = m.analyze_copula("I am Christopher")
    assert n is not None and n.relation == "also_known_as"


@requires_model
def test_copula_negation_is_detected_deterministically():
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("I am not worried")
    assert c is not None
    assert c.negated is True               # neg dependency, not a regex window
    assert c.complement == "worried"


@requires_model
def test_copula_verb_participle_is_ambiguous_no_relation():
    """'I am exhausted' → participle (VERB) — first-cut is ambiguous → no relation; caller
    routes the LLM grounding residue rather than guess."""
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("I am exhausted")
    assert c is not None
    assert c.complement_pos == "VERB"
    assert c.relation is None              # ambiguous → caller's residue path


@requires_model
def test_non_copula_returns_none():
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_copula("the cat sat on the mat") is None
    assert m.analyze_copula("") is None


# ────────────────── NAMED-INSTANCE copula+appositive ("My dog Fraggle is a poodle") ──────────
# The named-instance construction: possessed HEAD NOUN (kind) + appositive PROPER NAME (instance)
# + determiner-introduced common-noun COMPLEMENT (the specific type). THE HARD LINE: the NAME
# never enters L4 — it is the naming layer + instance_of; the type/kind are the L4 rung.
@requires_model
def test_named_instance_dog_fraggle_poodle():
    m = _reload(LINGUISTIC_LAYER="true")
    ni = m.analyze_named_instance("My dog Fraggle is a poodle.")
    assert ni is not None
    assert ni.kind == "dog"               # the broad KIND (possessed head noun)
    assert ni.name == "Fraggle"           # the appositive NAME (naming layer — never L4)
    assert ni.instance_type == "poodle"   # the determiner-introduced specific TYPE
    assert ni.possessor_is_self is True   # 1st-person possessive → ownership binds to user
    assert ni.negated is False


@requires_model
def test_named_instance_is_subject_agnostic():
    """Same construction shape on ANY subject — cat/tabby, car/Subaru — not a dog/breed word-list."""
    m = _reload(LINGUISTIC_LAYER="true")
    cat = m.analyze_named_instance("My cat Whiskers is a tabby.")
    assert cat is not None and cat.kind == "cat" and cat.name == "Whiskers"
    assert cat.instance_type == "tabby"
    car = m.analyze_named_instance("My car Betsy is a Subaru.")
    assert car is not None and car.kind == "car" and car.name == "Betsy"
    assert car.instance_type == "subaru"  # PROPN complement, but det-introduced → still a TYPE


@requires_model
def test_named_instance_no_appositive_returns_none():
    """A bare type-predication with NO named instance ("My dog is a poodle") is NOT this
    construction — it has no appositive name to mint, so the analyzer returns None (the ordinary
    owns + instance_of placement handles the nameless case; we never over-capture it here)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_named_instance("My dog is a poodle.") is None


@requires_model
def test_named_instance_adjective_state_returns_none():
    """'Fraggle is happy.' is a STATE (ADJ acomp, no determiner) — NOT a type predication.
    The analyzer must return None so no bogus type ('happy') is minted (the state seam owns it)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_named_instance("Fraggle is happy.") is None


@requires_model
def test_named_instance_bare_proper_complement_returns_none():
    """'My dog Fraggle is Max.' — a bare PROPN complement with NO determiner is a SECOND NAME,
    not a type. THE HARD LINE: a name is never classified into L4, so this returns None."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_named_instance("My dog Fraggle is Max.") is None


@requires_model
def test_named_instance_self_copula_and_preference_return_none():
    """Constructions owned by other seams must not be claimed here: a self copula
    ('I am a teacher') and a possessive preference ('My favorite color is blue') both lack the
    appositive-name + kind shape → None (no cross-capture)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_named_instance("I am a teacher.") is None
    assert m.analyze_named_instance("My favorite color is blue.") is None


@requires_model
def test_named_instance_unavailable_layer_returns_none(monkeypatch):
    """Flag OFF → benign no-op (fail-safe), never a crash."""
    m = _reload(LINGUISTIC_LAYER="false")
    assert m.analyze_named_instance("My dog Fraggle is a poodle.") is None


# ───────────── NAMING-VERB construction — single + MULTI ("a dog named Fraggle, …") ──────────
# analyze_naming returns the FIRST (head-noun, proper-name) pair (byte-identical legacy behavior);
# analyze_naming_all returns EVERY naming construction in the clause so a comma-and enumeration
# binds all conjuncts, not just the first. Subject-agnostic: the KIND is whatever common noun the
# naming verb modifies — no kind/breed/pet word-list anywhere in the analyzer.
@requires_model
def test_analyze_naming_single_construction():
    m = _reload(LINGUISTIC_LAYER="true")
    one = m.analyze_naming("I have a cat named Goose.")
    assert one is not None
    assert one.named == "cat"
    assert one.proper_name == "Goose"
    assert one.negated is False


@requires_model
def test_analyze_naming_all_parity_on_single():
    """On a single-construction clause analyze_naming == analyze_naming_all()[0] — the multi
    path is a strict superset; single-construction behavior is unchanged."""
    m = _reload(LINGUISTIC_LAYER="true")
    one = m.analyze_naming("a server called Apollo")
    allr = m.analyze_naming_all("a server called Apollo")
    assert len(allr) == 1
    assert (allr[0].named, allr[0].proper_name) == (one.named, one.proper_name) == ("server", "Apollo")


@requires_model
def test_analyze_naming_all_multi_same_kind():
    """THE ORIGINAL CASE — three same-domain naming constructions all recovered (not just Fraggle)."""
    m = _reload(LINGUISTIC_LAYER="true")
    allr = m.analyze_naming_all(
        "I have a dog named Fraggle, a snake named Sophia, and a cat named Goose.")
    pairs = [(r.named, r.proper_name) for r in allr]
    assert ("dog", "Fraggle") in pairs
    assert ("snake", "Sophia") in pairs
    assert ("cat", "Goose") in pairs
    assert len(pairs) == 3


@requires_model
def test_analyze_naming_all_mixed_kinds_subject_agnostic():
    """THE AGNOSTIC PROOF — mixed kinds (animal/object/person) all bind through the ONE chain.
    The analyzer makes NO kind decision; it is pure (head-noun, proper-name) grammar."""
    m = _reload(LINGUISTIC_LAYER="true")
    allr = m.analyze_naming_all(
        "I have a cat named Goose, a snake named Sophia, a server named Apollo, "
        "and a friend named Diane.")
    pairs = [(r.named, r.proper_name) for r in allr]
    assert ("cat", "Goose") in pairs
    assert ("snake", "Sophia") in pairs
    assert ("server", "Apollo") in pairs
    assert ("friend", "Diane") in pairs
    assert len(pairs) == 4


@requires_model
def test_analyze_naming_all_no_construction_returns_empty():
    """No naming verb → empty list (fail-safe; the genitive-name chain owns 'My wife's name is X')."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_naming_all("My wife's name is Dana.") == []
    assert m.analyze_naming("My wife's name is Dana.") is None


# ─────────────────────────── POSSESSIVE HEAD (poss dep → head noun) ───────────────────────

@requires_model
def test_possessive_head_lifts_head_noun_phrase():
    m = _reload(LINGUISTIC_LAYER="true")
    head = m.possessive_head("I had an issue with my car's GPS system", "car")
    assert head == "gps system"            # poss "car" → head "system" + compound "gps"


@requires_model
def test_possessive_head_nested_chain_takes_rightmost():
    """Nested possessive ("joel's bike's tire") → the RIGHTMOST head ("tire"), not the immediate
    one — chasing the poss chain. Matches the bespoke walk's rightmost-head contract."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.possessive_head("joel's bike's tire is flat", "joel") == "tire"


@requires_model
def test_possessive_head_non_possessor_returns_none():
    m = _reload(LINGUISTIC_LAYER="true")
    # "server" is not a possessive in this text → no rewrite (object left as-is by the caller).
    assert m.possessive_head("I had an issue with the server", "server") is None
    assert m.possessive_head("anything", "") is None


# ─────────────────────────── FUNCTION-WORD PREDICATE TEST (POS) ───────────────────────────

@requires_model
def test_function_word_predicate_rejects_pure_grammar():
    m = _reload(LINGUISTIC_LAYER="true")
    # Bare conjunction / subordinator / preposition / copula → all-function-word → True (reject).
    assert m.is_function_word_predicate("but") is True
    assert m.is_function_word_predicate("when") is True
    assert m.is_function_word_predicate("near") is True
    assert m.is_function_word_predicate("be") is True
    # Empty predicate carries no content.
    assert m.is_function_word_predicate("") is True


@requires_model
def test_function_word_predicate_keeps_content_predicates():
    m = _reload(LINGUISTIC_LAYER="true")
    # Content-bearing (verb / verb+prep / light-verb+noun) → False (keep).
    assert m.is_function_word_predicate("fix") is False
    assert m.is_function_word_predicate("lives_in") is False     # underscore → "lives in"
    assert m.is_function_word_predicate("has_issue") is False
    assert m.is_function_word_predicate("owns") is False


# ─────────────────────────── CLAUSE / SENTENCE SEGMENTATION ───────────────────────────────

@requires_model
def test_segment_clauses_splits_sentences():
    m = _reload(LINGUISTIC_LAYER="true")
    out = m.segment_clauses("I am worried. My car broke down. I called a mechanic.")
    assert len(out) == 3
    assert out[0].startswith("I am worried")


# ─────────────────────── INTERROGATIVE-VS-DECLARATIVE (query-path pollution gate) ─────────
# These back the harvest fix: recall_memory("what did I attend after my graduation") must NOT
# write facts. The harvest skips any clause this flags True; a declarative clause survives.

@requires_model
def test_interrogative_wh_question_no_qmark():
    """The exact bug case (no trailing '?'): a wh question is interrogative → skipped by harvest."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.is_interrogative_clause("what did I attend after my graduation") is True


@requires_model
def test_interrogative_trailing_qmark():
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.is_interrogative_clause("when's my meeting?") is True


@requires_model
def test_interrogative_yes_no_aux_inversion():
    """Subject-aux inversion (no wh-word, no '?') is still a question."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.is_interrogative_clause("did I attend the conference") is True


@requires_model
def test_declarative_statement_is_not_interrogative():
    """A genuine buried statement must be harvested → must NOT be flagged interrogative."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.is_interrogative_clause("I adopted a cat named Mittens") is False
    assert m.is_interrogative_clause("oh and I adopted a cat named Mittens") is False


@requires_model
def test_interrogative_embedded_clause_question_forms():
    """The live recall-pollution repro: pure questions whose object surface ("car I own",
    "what kind of X") would otherwise be minted as a fact MUST be flagged interrogative so the
    recall-path harvest drops them. Covers wh-after-aux ("do you know what kind of car I own"),
    bare wh-copula ("what is my favorite color"), and yes/no aux-inversion ("do I have any pets").
    With AND without the trailing '?' (the recall path may strip punctuation)."""
    m = _reload(LINGUISTIC_LAYER="true")
    for q in (
        "do you know what kind of car I own?",
        "do you know what kind of car I own",
        "what is my favorite color?",
        "what is my favorite color",
        "do I have any pets?",
        "do I have any pets",
    ):
        assert m.is_interrogative_clause(q) is True, q


@requires_model
def test_declarative_with_relative_wh_is_not_interrogative():
    """A wh-word inside a RELATIVE clause is a declarative modifier, not a question."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.is_interrogative_clause("I visited the school where I graduated") is False


@requires_model
def test_empty_text_is_not_interrogative():
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.is_interrogative_clause("") is False
    assert m.is_interrogative_clause("   ") is False


def test_interrogative_returns_none_when_layer_unavailable(monkeypatch):
    """Layer entirely unavailable → None so the harvest keeps its today's harvest-all fallback
    (rather than silently dropping every clause / every fact)."""
    monkeypatch.setitem(sys.modules, "spacy", None)
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.linguistics_available() is False
    assert m.is_interrogative_clause("what did I attend after my graduation") is None


# ─────────────────────────── FAIL-SAFE: spaCy import / model load fails ───────────────────

def test_fail_safe_when_spacy_import_missing(monkeypatch):
    """If `import spacy` raises (spaCy not baked), every public fn is a benign no-op — no crash."""
    # Force the deferred `import spacy` inside _get_nlp to fail.
    monkeypatch.setitem(sys.modules, "spacy", None)  # importing a None module raises ImportError
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.linguistics_available() is False
    # All public functions degrade to the "no analysis" result, never raise.
    assert m.analyze_copula("I am worried") is None
    assert m.possessive_head("my car's GPS system", "car") is None
    assert m.is_function_word_predicate("but") is None   # None → caller uses bespoke set
    assert m.segment_clauses("a. b.") == []
    assert m.is_interrogative_clause("did I go?") is None  # None → harvest keeps its fallback


def test_fail_safe_when_model_load_fails(monkeypatch):
    """If spaCy imports but the model isn't installed, _get_nlp returns None (graceful no-op)."""
    m = _reload(LINGUISTIC_LAYER="true")

    import types
    fake_spacy = types.SimpleNamespace()

    def _boom(*a, **k):
        raise OSError("[E050] Can't find model 'en_core_web_sm'")

    fake_spacy.load = _boom
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    # Reset the cached singleton so the next call re-attempts the (now-failing) load.
    m._nlp = None
    m._load_attempted = False

    assert m.linguistics_available() is False
    assert m.analyze_copula("I am worried") is None


def test_failed_load_is_not_retried(monkeypatch):
    """A failed load caches the failure (no per-call retry / import storm)."""
    m = _reload(LINGUISTIC_LAYER="true")
    import types

    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise OSError("no model")

    fake_spacy = types.SimpleNamespace(load=_boom)
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    m._nlp = None
    m._load_attempted = False

    assert m.linguistics_available() is False
    assert m.linguistics_available() is False   # second call must NOT re-attempt the load
    assert calls["n"] == 1


# ─────────────────────────── WIRED INTEGRATION CONTRACT (main.py route) ───────────────────
# Mirrors the spaCy branch of src/api/main.py::_ground_self_predication so the routing contract
# is regression-guarded without spinning the async LLM/DB stack. Three outcomes:
#   DETERMINISTIC  → return the edge, NO LLM call (the retired regex + INTENT_PRECLASSIFY)
#   DROP           → negated self-copula (LLM 'none'-on-negation contract; Phase-1 no negatives)
#   LLM_RESIDUE    → ambiguous / non-self / non-copula → fall through to today's regex+LLM path
_SELF_PREDICATION_RELS = ("feels", "also_known_as", "occupation")


def _route(m, text):
    if not m.linguistics_available():
        return "LLM_RESIDUE", None
    cop = m.analyze_copula(text)
    if cop is not None and cop.subject_is_self:
        if cop.negated:
            return "DROP", None
        if cop.relation in _SELF_PREDICATION_RELS and cop.complement and len(cop.complement) >= 2:
            return "DETERMINISTIC", {"subject": "user", "rel_type": cop.relation, "object": cop.complement}
    return "LLM_RESIDUE", None


@requires_model
def test_wired_route_deterministic_short_circuits_llm():
    m = _reload(LINGUISTIC_LAYER="true")
    # 2026-07-06 full-NP complement fix: the occupation carries the WHOLE predicate-nominal NP
    # (compound/amod premodifiers), not just the head token ("analyst").
    assert _route(m, "I am a senior systems analyst") == (
        "DETERMINISTIC",
        {"subject": "user", "rel_type": "occupation", "object": "senior systems analyst"})
    assert _route(m, "I'm Chris")[0] == "DETERMINISTIC"
    assert _route(m, "I am worried about the migration") == (
        "DETERMINISTIC", {"subject": "user", "rel_type": "feels", "object": "worried"})


@requires_model
def test_wired_route_negation_drops():
    m = _reload(LINGUISTIC_LAYER="true")
    assert _route(m, "I am not happy")[0] == "DROP"


@requires_model
def test_wired_route_ambiguous_and_non_self_fall_to_residue():
    m = _reload(LINGUISTIC_LAYER="true")
    assert _route(m, "I am exhausted")[0] == "LLM_RESIDUE"        # VERB participle → ambiguous
    assert _route(m, "I love hiking on weekends")[0] == "LLM_RESIDUE"  # not a copula
    assert _route(m, "she is a nurse")[0] == "LLM_RESIDUE"        # not self-predication


def test_wired_route_falls_to_residue_when_layer_off():
    """Flag OFF → the wired branch yields LLM_RESIDUE → main.py runs its bespoke regex+LLM path."""
    m = _reload(LINGUISTIC_LAYER="0")
    try:
        assert _route(m, "I am worried")[0] == "LLM_RESIDUE"
    finally:
        _reload(LINGUISTIC_LAYER="true")


# ─────────────────────────── KILL-SWITCH OFF (bespoke fallback path) ───────────────────────

def test_kill_switch_off_is_total_no_op():
    """LINGUISTIC_LAYER=0 → the layer is unavailable and every fn no-ops, regardless of whether
    spaCy/the model are installed. The caller then runs its bespoke fallback (today's behavior)."""
    m = _reload(LINGUISTIC_LAYER="0")
    try:
        assert m.linguistics_available() is False
        assert m.analyze_copula("I am worried") is None
        assert m.possessive_head("my car's GPS system", "car") is None
        assert m.is_function_word_predicate("but") is None
        assert m.segment_clauses("a. b.") == []
    finally:
        _reload(LINGUISTIC_LAYER="true")   # restore default for any later module users


# ─────────────────────────── gap-2 — match-driven recursion (derive_sentence_facts) ─────────
# These exercise the str-fallback path (no GLiNER2/GPU needed — the deriver re-parses the str and
# fills object types from the thin slot tag). They assert the gap-2 contract behaviors:
#   (1) intransitive state/event capture binds an event_date (§10.4),
#   (2) overlap reconcile — one span matched by two chains yields ONE fact, not two (§2.1/§10.7),
#   (3) a content-bearing span is accounted for in the covered-set, never silently dropped (§10.3).

from datetime import datetime as _dt


@requires_model
def test_gap2_intransitive_emits_subject_state_as_typed_relational_node():
    """§10.4 + DESIGN-state-typing (owner decision, scalar→RELATIONAL correction) — a content verb
    with a subject and NO object ("the server crashed yesterday" / "GPS broke last week") emits the
    STATE as a TYPED, REUSABLE relational hierarchy NODE on the THING (the structural twin of
    ``feels``), NOT a scalar string. The THING is the subject (grounded normally); the STATE rides
    the canonical RELATIONAL ``_STATE_REL`` (``has_state``, tail_types={Concept}) so it IS a
    typeable span (carries a thin/object type), resolves to a UUID node, and self-builds an is-a
    ladder. NODE IDENTITY is the spaCy LEMMA ("broke"→"break", "crashed"→"crash") — deterministic,
    byte-identical so a GPS / a server / a leg all converge to ONE node (no hardcoded map). The
    event_date binds to the governing verb. Detected STRUCTURALLY (verb has nsubj, no object)."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 6, 21)
    for sent, subj, state_lemma in (
        ("the server crashed yesterday", "server", "crash"),
        ("the GPS broke last week", "gps", "break"),
    ):
        facts = m.derive_sentence_facts(sent, ref)
        # the subject's state fact exists on the canonical RELATIONAL state predicate …
        state = [f for f in facts
                 if f.subject == subj and f.rel_type == m._STATE_REL]
        assert state, f"no intransitive state fact for {sent!r}: got {facts!r}"
        # … its object is the spaCy LEMMA of the state verb (the reusable node identity), NOT the
        #     inflected surface — this is what makes the node converge across occurrences …
        assert any(f.object == state_lemma for f in state), \
            f"state lemma {state_lemma!r} not the node identity for {sent!r}: {state!r}"
        # … the relative date bound to the governing verb (NOT wall-clock, NOT None) …
        assert any(f.event_date for f in state), \
            f"intransitive fact carried no event_date for {sent!r}: {state!r}"
        # NOTE on typing: ``obj_tok`` is now RESTORED so GLiNER2 would TYPE the span at ingest and
        #     it resolves to a groundable UUID node (the relational contract). In this no-GPU
        #     str-fallback path GLiNER2 does not run and the verb lemma is not in the minimal thin
        #     map, so ``thin_type`` is None here — that is expected and not load-bearing for the
        #     deriver; the load-bearing change is that the object is RELATIONAL (resolved/grounded
        #     at ingest), asserted via the lemma-node identity + reuse test, not a scalar string.
        # … and the state node is NEVER emitted under any OTHER (non-state) rel — only ``_STATE_REL``
        #     carries it (so no verb-lemma relation re-grounds it as a different edge).
        bad = [f for f in facts
               if f.object == state_lemma and f.rel_type != m._STATE_REL]
        assert not bad, \
            f"state node emitted under a non-state rel for {sent!r}: {bad!r}"


@requires_model
def test_gap2_intransitive_state_node_is_reused_across_subjects():
    """DESIGN-state-typing §1.2 — convergence-by-identity: two DIFFERENT things in the SAME state
    resolve to the SAME node string (the spaCy lemma), so the second occurrence reuses the one
    ``break`` node (zero LLM, deterministic). We assert the EMIT identity here (the node string);
    the UUID-level reuse + already-laddered skip are exercised at ingest via EntityRegistry."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 6, 21)
    objs = []
    for sent in ("the GPS broke last week", "my leg broke yesterday"):
        facts = m.derive_sentence_facts(sent, ref)
        st = [f.object for f in facts if f.rel_type == m._STATE_REL]
        assert st, f"no state fact for {sent!r}: {facts!r}"
        objs.append(st[0])
    assert objs[0] == objs[1] == "break", \
        f"state node did not converge to one identity across subjects: {objs!r}"


def test_gap2_intransitive_date_does_not_attach_to_possessive():
    """§10.4 date-scope fix — in "my car's GPS broke last week" the date governs ONLY the
    intransitive STATE fact (the verb 'broke'); the co-occurring verb-LESS possessive/genitive
    facts ((user, owns, car), (gps, part_of/related_to, car)) must NOT inherit the date. A date
    binds only to the fact whose predicate IS the date span's governing verb."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 6, 21)
    facts = m.derive_sentence_facts("my car's GPS broke last week", ref)
    # the state fact carries the date …
    state = [f for f in facts if f.rel_type == m._STATE_REL]
    assert state and any(f.event_date for f in state), \
        f"state fact missing/undated: {facts!r}"
    # … but the verb-less possessive/genitive facts (owns / part_of / related_to) do NOT.
    verbless = [f for f in facts
                if f.rel_type in ("owns", "part_of", "related_to")]
    assert verbless, f"expected possessive/genitive facts for the date-scope check: {facts!r}"
    assert all(f.event_date is None for f in verbless), \
        f"date wrongly attached to a verb-less possessive/genitive fact: {verbless!r}"


# ── WHOLE-UTTERANCE DEIXIS (LME elapsed-duration-calc cluster) ────────────────────────────────────
# A bare adverbial deictic ("today"/"just") scopes the ENTIRE clause. spaCy attaches it to ONE verb
# of a verb-complex (the aspectual matrix "started" in "started playing"; a coordinated conj verb),
# so a content edge built on a SIBLING/EMBEDDED verb was left UNDATED and became unreachable by the
# between/duration calc — the shared root cause of LME gpt4_fa19884c / gpt4_1d4ab0c9 ("I couldn't
# find a dated record for the events being compared"). Every content edge of a single-deictic clause
# must now carry the session date; a PREPOSITIONAL date PP must stay verb-scoped (never spread).

@requires_model
@pytest.mark.parametrize("sent,want_obj", [
    # aspectual matrix + xcomp: "today" attaches to "started", content edge is on "playing" (LME fa19884c)
    ("I started playing along to my favorite songs on my old keyboard today", "favorite songs"),
    # coordinated verb: "today" buried on "started enjoying", anchor content verb is "discovered" (LME fa19884c)
    ("I recently discovered a bluegrass band and started enjoying their music today", "bluegrass band"),
    # aspectual matrix + xcomp: "today" on "started", content edge on "watering" (LME 1d4ab0c9)
    ("I started watering my herb garden every morning today", "herb garden"),
])
def test_whole_utterance_deixis_dates_all_content_edges(sent, want_obj):
    """A bare adverbial deictic dates EVERY content edge of the clause (aspectual xcomp / coordinated
    verb), so the between/duration calc can reach each operand's date. This is the elapsed-duration
    cluster fix — parse-wobble in the deictic's verb attachment must not leave a content edge undated."""
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    ref = _dt(2023, 3, 22)
    facts = m.derive_sentence_facts(sent, ref)
    hit = [f for f in facts if not f.presupposed and want_obj in f.object]
    assert hit, f"no content edge for {want_obj!r} in {sent!r}: {facts!r}"
    assert any(f.event_date for f in hit), \
        f"content edge for {want_obj!r} left UNDATED (unreachable by calc) in {sent!r}: {hit!r}"


@requires_model
def test_prepositional_date_does_not_spread_to_sibling_verb():
    """The whole-utterance spread is gated to BARE ADVERBIAL deictics — a PREPOSITIONAL date PP
    ("on February 20th", pobj under an ADP) stays verb-scoped and must NOT spread to a co-occurring
    sibling verb. Guards the over-stamp regression the adverbial-vs-prepositional split prevents."""
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    facts = m.derive_sentence_facts(
        "I bought a phone on February 20th and my brother lives in Toronto", _dt(2023, 5, 10))
    bought = [f for f in facts if f.rel_type == "buy" and "phone" in f.object]
    assert bought and any(f.event_date for f in bought), f"buy edge missing its date: {facts!r}"
    lives = [f for f in facts if "toronto" in f.object.lower()]
    assert lives, f"expected a residence edge for the sibling clause: {facts!r}"
    assert all(f.event_date is None for f in lives), \
        f"prepositional date wrongly spread to the sibling residence edge: {lives!r}"


# ── OVER-CAPTURE FIREWALL — structural state discrimination (NO verb list) ────────────────────────
# The bare ``_svo_object_head is None`` object-test was too weak: it minted ``has_state`` for
# control/mental verbs ("think"/"want"), light/transitive-with-pronoun verbs ("help"), and present-
# tense activities ("work"). These are discriminated STRUCTURALLY (dep_/tag_/morph), not by a list.

@requires_model
@pytest.mark.parametrize("sent", [
    "I think I will wait",   # mental verb + ccomp; embedded "wait" is itself a ccomp
    "I want to go",          # control verb + xcomp / infinitival "to"
    "I have a car",          # transitive (dobj NOUN) — SVO owns it
    "I work at home",        # present-tense activity, no object
    "she helped me",         # transitive used with a PRONOUN object ("me")
    "I told them",           # transitive used with a PRONOUN object ("them")
    "I am a teacher",        # nominal copula — a role/identity, not a state
])
def test_intransitive_state_does_not_overfire_on_non_states(sent):
    """A control/mental verb (ccomp/xcomp), a transitive verb (object of ANY pos), and a nominal
    copula must NEVER mint a DURABLE ``has_state`` fact — purely from spaCy dep_/tag_/morph cues."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    durable_states = [f for f in facts
                      if f.rel_type == m._STATE_REL and not f.tentative]
    assert not durable_states, \
        f"{sent!r} wrongly minted a DURABLE state: {durable_states!r}"


# ── ASSERTION POLARITY (Q1) — negated genuine STATE capture (NOT correction/retraction) ──────────
# A negated genuine state ("the GPS is not functioning") must be CAPTURED as a has_state fact with
# negated=True and tentative=False (durable) so it reads back NEGATED, never as its positive opposite.
# Read deterministically from the spaCy ``neg`` dep already at the state lanes — no word list, no LLM.

@requires_model
@pytest.mark.parametrize("sent", [
    "the GPS is not functioning",     # present-tense activity verb, negated → durable non-functional
    "the printer is not working",     # ditto
    "the server is not down",         # copula + ADV particle complement, negated
    "the device is not idle",         # copula + ADJ complement, negated
])
def test_negated_state_is_captured_negated_and_durable(sent):
    """A NEGATED genuine state is captured has_state(negated=True, tentative=False) — durable, so it
    routes to a Class-B edge (not the tentative C lane where the positive would survive on decay)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    states = [f for f in facts if f.rel_type == m._STATE_REL]
    assert states, f"{sent!r} produced NO has_state fact: {facts!r}"
    assert all(f.negated for f in states), \
        f"{sent!r} state(s) not marked negated: {states!r}"
    assert all(not f.tentative for f in states), \
        f"{sent!r} negated state must be DURABLE (tentative False): {states!r}"


@requires_model
def test_affirmed_state_stays_affirmed():
    """A plain (affirmed) state is unchanged — negated=False (no regression to the polarity default)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("the server crashed", _dt(2026, 6, 21))
    states = [f for f in facts if f.rel_type == m._STATE_REL]
    assert states, f"affirmed state missing: {facts!r}"
    assert all(not f.negated for f in states), \
        f"affirmed state wrongly marked negated: {states!r}"


# ── ASPECTUAL-ADVERB MIS-CAST (junk-occurrence) — "Luna is still in potty-training" ──────────────
# An ASPECTUAL adverb (still/already/now) that spaCy attaches as ``advmod`` on a copula must NEVER be
# minted as a resultant ``has_state`` value: it is not the copula's predicate, and a junk
# ``(x, has_state, "still")`` edge date-stamps and pollutes ordered-event / temporal recall. The real
# state — the locative PP ("in potty-training") or the ADJ predicate ("busy") or the nominal identity
# ("a puppy") — is captured by its own lane. Purely spaCy dep_/pos_ + the closed containment-prep
# primitive; NO adverb/domain word list.

@requires_model
@pytest.mark.parametrize("sent,adv", [
    ("Luna is still in potty-training.", "still"),   # aspectual adv + copula-locative PP predicate
    ("She's still in potty-training.", "still"),
    ("She is still in school.", "still"),
    ("The device is now inside the case.", "now"),   # 'now' + containment 'inside' PP
    ("My dog is still a puppy.", "still"),            # aspectual adv + NOMINAL identity complement
])
def test_aspectual_adverb_is_not_a_copula_state(sent, adv):
    """The aspectual advmod adverb never becomes a has_state value; the real locative/identity is
    captured by its own lane (so this is a re-route, not a silent drop)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    bad = [f for f in facts if f.rel_type == m._STATE_REL and f.object == adv]
    assert not bad, f"{sent!r} mis-cast aspectual {adv!r} as has_state: {bad!r}"
    # the real state survives (strong ingest — re-route, do not lose): a locative case must still land
    # its located_in; the whole sentence must not go empty.
    assert facts, f"{sent!r} produced NO facts at all (over-suppression): {facts!r}"


@requires_model
def test_aspectual_shadowing_adverb_yields_to_adjective_state():
    """A leading aspectual adverb must NOT shadow the genuine ADJ state complement: "She is still busy"
    → has_state(busy), never has_state(still)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("She is still busy.", _dt(2026, 6, 21))
    states = [f.object for f in facts if f.rel_type == m._STATE_REL]
    assert "busy" in states, f"real ADJ state 'busy' lost: {facts!r}"
    assert "still" not in states, f"aspectual 'still' wrongly kept as state: {facts!r}"


@requires_model
@pytest.mark.parametrize("sent,state", [
    ("The server is down.", "down"),                        # sole advmod ADV predicate → keep
    ("The printer is idle.", "idle"),                       # acomp ADJ predicate → keep
    ("The cat is outside.", "outside"),                     # sole advmod ADV predicate → keep
    ("The server is down in the datacenter.", "down"),      # PP attaches to the ADV, not the copula
    ("The server is down since yesterday.", "down"),        # temporal PP adjunct (non-containment prep)
    ("The AC is off at night.", "off"),                     # temporal PP adjunct ('at', not containment)
])
def test_genuine_resultant_state_adverb_is_kept(sent, state):
    """A genuine resultant-state adverb ("is down"/"is outside") — including with a PP that hangs off
    the ADV or a temporal/non-containment adjunct — must SURVIVE (no over-suppression)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    states = [f.object for f in facts if f.rel_type == m._STATE_REL]
    assert state in states, f"{sent!r} lost genuine state {state!r}: {facts!r}"


@requires_model
@pytest.mark.parametrize("sent", [
    "I did not break it",     # TRANSITIVE negated clause (dobj pronoun) — NOT this lane
    "that is not my dog",     # NOMINAL copula complement — identity, the correction gate owns it
])
def test_negated_action_or_identity_is_not_a_state(sent):
    """The negated-state lane is STATE-only: a transitive negated clause and a nominal-copula identity
    negation must NOT produce a has_state edge (these are absence/identity, owned elsewhere — the
    correction gate ran BEFORE the deriver). No has_state edge of any polarity."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    states = [f for f in facts if f.rel_type == m._STATE_REL]
    assert not states, f"{sent!r} wrongly minted a has_state edge: {states!r}"


@requires_model
@pytest.mark.parametrize("sent,node", [
    ("the server crashed yesterday", "crash"),   # VBD / Tense=Past eventive change-of-state
    ("my GPS broke", "break"),                    # VBD eventive change-of-state
    ("the system is broken", "break"),            # VBN / Aspect=Perf past participle
    ("the printer is idle", "idle"),              # copula + acomp ADJ resultant state
    ("the server is down", "down"),               # copula + advmod ADV stative particle
])
def test_intransitive_confirmed_state_is_durable(sent, node):
    """A structurally-CONFIRMED change-of-state (past/perfective/participle eventive, or a copula
    predicative ADJ/ADV state) MUST emit a DURABLE ``has_state`` on the canonical state predicate,
    with the spaCy-lemma node identity. Detected via tag_/morph/dep_ — no verb list."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    states = [f for f in facts if f.rel_type == m._STATE_REL and not f.tentative]
    assert states, f"{sent!r} produced no durable state: {facts!r}"
    assert any(f.object == node for f in states), \
        f"{sent!r} state node identity != {node!r}: {states!r}"


@requires_model
@pytest.mark.parametrize("sent", [
    "I run every day",   # ongoing present-tense activity, objectless
    "I sleep",           # objectless present-tense activity
])
def test_intransitive_uncertain_state_is_tentative_not_durable(sent):
    """An UNCERTAIN intransitive (objectless present-tense activity that passes the weak object-test
    but lacks the change-of-state cues) is CAPTURED-NOT-DROPPED as a TENTATIVE fact (the consumer
    routes it to short-term Class C), NEVER a durable Class-B state — preventing the live
    ``has_state work/run`` junk while still not forgetting the content."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    states = [f for f in facts if f.rel_type == m._STATE_REL]
    # captured (not dropped) …
    assert states, f"{sent!r} dropped the intransitive content entirely: {facts!r}"
    # … but TENTATIVE (short-term C), never durable B.
    assert all(f.tentative for f in states), \
        f"{sent!r} minted a DURABLE state for an uncertain activity: {states!r}"


# ─────────────────── G1: COPULA PREDICATE-NOMINATIVE NAMED VALUE ───────────────────
# An identity copula whose complement is a PROPER NAME must LAND the name as the subject NP's
# VALUE/alias — never drop it, never classify the NAME into L4 (THE HARD LINE). Two routings:
#   • preference subject ("my favorite/go-to X is <Name>") → favorite_X value on the user;
#   • definite sortal subject ("the play/movie/book I … is <Name>") → the name filed as an
#     also_known_as alias of the sortal (via the connector-agnostic name↔type binder).
# The relative-clause verb ("the play I ATTENDED") is an OBJECT-relative gap (the antecedent IS
# the object) → it must NOT mint a junk (user, has_state, attend). Determiner-led PROPN titles
# ("The Glass Menagerie", "the Bright Angel Trail") are TITLES, not types, and must bind.


@requires_model
def test_g1_preference_copula_named_value_lands_on_user():
    """"My favorite restaurant is Nobu." → the PROPER NAME lands as the user's favorite_restaurant
    VALUE (a preference value filed at the subject NP), NOT dropped, NOT a type of anything."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("My favorite restaurant is Nobu.", _dt(2026, 6, 21))
    fav = [f for f in facts if f.rel_type.startswith("favorite") and f.object == "nobu"]
    assert fav, f"favorite value 'nobu' not captured on the user: {facts!r}"
    assert all(f.subject == "user" for f in fav), f"favorite not filed on the user: {fav!r}"


@requires_model
@pytest.mark.parametrize("sent,sortal,name", [
    ("The play I attended was The Glass Menagerie.", "play", "glass menagerie"),   # det-led title
    ("The movie I watched was Inception.", "movie", "inception"),                  # bare PROPN title
    ("The book I am reading is Dune.", "book", "dune"),
])
def test_g1_definite_sortal_binds_title_as_alias(sent, sortal, name):
    """A definite-sortal copula ("the <sortal> I … is <Name>") binds the proper NAME as an
    ``also_known_as`` ALIAS of the sortal — the naming layer, NEVER an instance_of/subclass_of of
    the name. The determiner-led title ("The Glass Menagerie") binds exactly like a bare PROPN."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    alias = [f for f in facts
             if f.rel_type == "also_known_as" and f.subject == sortal and f.object == name]
    assert alias, f"{sent!r} did not bind {name!r} as an alias of {sortal!r}: {facts!r}"


@requires_model
@pytest.mark.parametrize("sent,name", [
    ("The play I attended was The Glass Menagerie.", "glass menagerie"),
    ("The movie I watched was Inception.", "inception"),
    ("My favorite restaurant is Nobu.", "nobu"),
])
def test_g1_named_value_is_never_classified_into_l4(sent, name):
    """THE HARD LINE (load-bearing): the PROPER NAME must NEVER be the SUBJECT of an ``instance_of``
    or ``subclass_of`` edge (a name never becomes a place). It lands as a VALUE/alias only."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    violations = [f for f in facts
                  if f.subject == name and f.rel_type in ("instance_of", "subclass_of")]
    assert not violations, \
        f"{sent!r} classified the NAME {name!r} into L4 (HARD LINE): {violations!r}"


@requires_model
@pytest.mark.parametrize("sent", [
    "The play I attended was The Glass Menagerie.",
    "The movie I watched was Inception.",
    "The restaurant we went to was Nobu.",
])
def test_g1_object_relative_clause_mints_no_junk_state(sent):
    """The relative-clause verb of a definite-sortal copula ("the play I ATTENDED …") is an
    OBJECT-relative gap (the antecedent fills the verb's object slot), so it is TRANSITIVE and must
    NOT emit a junk ``(user, has_state, <verb>)``. A SUBJECT-relative ("the man WHO died") keeps
    its state — covered by test_g1_subject_relative_keeps_its_state."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    junk = [f for f in facts if f.rel_type == m._STATE_REL]
    assert not junk, f"{sent!r} minted a junk state for an object-relative verb: {junk!r}"


@requires_model
def test_g1_subject_relative_keeps_its_state():
    """The object-relative guard must NOT over-suppress a SUBJECT-relative clause: "the man who died"
    has the antecedent AS the subject (a wh nsubj) → a genuine intransitive state is kept."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("the man who died was my neighbor", _dt(2026, 6, 21))
    states = [f for f in facts if f.rel_type == m._STATE_REL and f.object == "die"]
    assert states, f"subject-relative state 'die' was wrongly suppressed: {facts!r}"


@requires_model
def test_gap2_overlap_reconciles_to_single_fact_not_two():
    """§2.1/§10.7 — convergence-on-overlap: a span that is matched by more than one chain (here the
    head noun of a 1st-person possessive, which the possessive chain claims) reconciles to ONE fact
    per (subject, rel, object) — never two competing facts for the same span."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 6, 21)
    facts = m.derive_sentence_facts("I have a dog", ref)
    keys = [(f.subject, f.rel_type, f.object) for f in facts]
    # dedup invariant: no (subj, rel, obj) triple appears twice (convergence consumed the overlap).
    assert len(keys) == len(set(keys)), f"duplicate facts for one span: {keys!r}"
    # and the possessive-style ownership / SVO does not double-write the same object.
    dog_facts = [k for k in keys if k[2] == "dog"]
    assert len(dog_facts) == len(set(dog_facts))


@requires_model
def test_gap2_content_span_is_accounted_not_silently_dropped():
    """§10.3 — a content-bearing span must be ACCOUNTED FOR (claimed by a chain → emitted, or
    surfaced as residue), never silently vanish. Here a normal SVO sentence's content nouns are all
    folded into emitted facts; the deriver returns those facts (the covered-set proves nothing
    content-bearing was dropped without a trace). Fail-safe: returns a list, never raises."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 6, 21)
    facts = m.derive_sentence_facts("I bought a phone", ref)
    assert isinstance(facts, list)
    # the content object "phone" is captured into a fact (claimed, not dropped).
    assert any(f.object == "phone" for f in facts), f"content span 'phone' dropped: {facts!r}"


# ─────────────── gap-1 PHASE 2 — rel_type convergence (Doc._.rel; GLiNER2 WINS) ───────────────
# A GLiNER2-minted rel carried on ``doc._.rel`` (keyed by the subject/object TOKEN indices) is
# AUTHORITATIVE: the deriver's _emit uses it over the SVO verb-lemma predicate. With no minted rel
# for a pair, the SVO verb-lemma stands (the gap-fill). Mirrors the gap-1 type convergence.


class _FakeUnderscore:
    """Minimal stand-in for spaCy's ``Doc._`` accessor (``get``/``set``) — model-free."""

    def __init__(self):
        self._store = {}

    def get(self, name):
        return self._store.get(name)

    def set(self, name, value):
        self._store[name] = value


class _FakeDoc:
    """Model-free fake Doc exposing only what the minted-rel helpers touch:
    ``has_extension`` and the ``_`` accessor. Lets us exercise the convergence helper
    (set_minted_rel / _minted_rel_for_pair) WITHOUT spaCy / a GPU."""

    def __init__(self):
        self._ = _FakeUnderscore()

    @staticmethod
    def has_extension(name):
        # The real registration goes on spaCy's Doc class; for the fake we report True so the
        # lookup path is exercised (the helper still guards on the store being present).
        return True


class _FakeTok:
    def __init__(self, i):
        self.i = i


def test_phase2_minted_rel_roundtrip_helper_model_free():
    """set_minted_rel stores by exact (subj.i, obj.i) index pair; _minted_rel_for_pair reads it
    back. A different pair → None (so the deriver's SVO predicate fills the gap). Deterministic,
    exact integer-pair lookup, no fuzzy. Runs without the spaCy model."""
    m = _reload(LINGUISTIC_LAYER="true")
    doc = _FakeDoc()
    # set_minted_rel needs the extension registered on the REAL Doc class; if spaCy is absent it
    # returns False and we manually seed the fake store to still exercise the lookup direction.
    ok = m.set_minted_rel(doc, 0, 3, "spouse")
    if not ok:
        doc._.set("rel", {(0, 3): "spouse"})
    # GLiNER2 minted rel present for (0,3) → returned authoritatively.
    assert m._minted_rel_for_pair(doc, _FakeTok(0), _FakeTok(3)) == "spouse"
    # a pair with NO minted rel → None (the SVO verb-lemma fills the gap downstream).
    assert m._minted_rel_for_pair(doc, _FakeTok(1), _FakeTok(2)) is None
    # fail-safe: None tokens → None, never raises.
    assert m._minted_rel_for_pair(doc, None, _FakeTok(3)) is None
    assert m._minted_rel_for_pair(None, _FakeTok(0), _FakeTok(3)) is None


@requires_model
def test_phase2_minted_rel_wins_over_svo_else_svo_fills_gap():
    """The convergence in _emit: when ``doc._.rel`` carries a GLiNER2-minted rel for the (subject
    token, object token) pair, that rel WINS over the SVO verb lemma; with NO minted rel the SVO
    verb lemma stands (the gap-fill). Builds a REAL typed Doc and seeds ``_.rel`` manually (no GPU /
    GLiNER2 needed for the convergence itself).

    This pins a LAYER property, so the overlay must resolve the BOOTSTRAP maps: POSTGRES_DSN is
    cleared (the file's established pattern, see _derive below) — with a tenant/seed alias map
    present, _predicate_is_novel_to_ontology('marry') legitimately flips the convergence branch
    (authority order: user > seed > growth), which is a DIFFERENT property owned by the
    authority-order tests."""
    os.environ.pop("POSTGRES_DSN", None)
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 6, 21)
    sent = "I married dana"
    doc = m._parse(sent)
    assert doc is not None
    # locate the subject (nsubj of the content verb) and object (dobj) token indices.
    verb = next((t for t in doc if t.pos_ == "VERB"), None)
    assert verb is not None
    subj = next((c for c in verb.children if c.dep_ in ("nsubj", "nsubjpass")), None)
    obj = next((c for c in verb.children if c.dep_ in ("dobj", "obj")), None)
    assert subj is not None and obj is not None

    # BASELINE (no minted rel) → SVO verb-lemma predicate ("marry") stands as the gap-fill.
    facts_svo = m.derive_sentence_facts(doc, ref)
    svo_rels = {f.rel_type for f in facts_svo if f.object == "dana"}
    assert any(r.startswith("marry") for r in svo_rels), \
        f"SVO gap-fill predicate missing: {[(f.subject, f.rel_type, f.object) for f in facts_svo]!r}"

    # CONVERGENCE: seed the AUTHORITATIVE GLiNER2 rel for THIS (subj.i, obj.i) pair → it WINS.
    doc2 = m._parse(sent)
    verb2 = next((t for t in doc2 if t.pos_ == "VERB"), None)
    subj2 = next((c for c in verb2.children if c.dep_ in ("nsubj", "nsubjpass")), None)
    obj2 = next((c for c in verb2.children if c.dep_ in ("dobj", "obj")), None)
    assert m.set_minted_rel(doc2, subj2.i, obj2.i, "spouse")
    facts_minted = m.derive_sentence_facts(doc2, ref)
    minted_rels = {f.rel_type for f in facts_minted if f.object == "dana"}
    assert "spouse" in minted_rels, \
        f"GLiNER2 minted rel did not win: {[(f.subject, f.rel_type, f.object) for f in facts_minted]!r}"
    # and the SVO verb lemma did NOT also emit a competing fact for the same pair.
    assert not any(r.startswith("marry") for r in minted_rels), \
        f"SVO predicate competed with the minted rel: {minted_rels!r}"


# ─────────────────── EVENT CAPTURE — analyze_event pobj selection (T3) ───────────────────
# The garbled-event-capture root-cause fix (event-capture spine authoritative). T3:
# "got back from the 'Rack Fest' in nearby city" — analyze_event must reify the NAMED
# eventive head governed by the source preposition (the quoted PROPN "Rack Fest") rather
# than the adjunct LOCATIVE pobj ("nearby city"). The fix is grammatical (dep_/pos_ + a
# quote primitive) and fail-safe: an unquoted location PROPN ("Paris") is NOT poached.

@requires_model
def test_analyze_event_named_source_pobj_beats_locative_adjunct():
    """T3: 'got back from the "Rack Fest" in nearby city' reifies the quoted source-governed
    eventive head ('rack fest'), NOT the locative adjunct pobj ('nearby city')."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_event("I got back from the 'Rack Fest' in nearby city")
    assert ev is not None, "named occurrence under a phrasal source PP was not detected"
    assert ev.event == "rack fest", f"expected 'rack fest', got {ev.event!r}"
    assert ev.negated is False


@requires_model
def test_analyze_event_bare_location_propn_not_poached():
    """Fail-safe: an UNQUOTED location PROPN under a motion verb ('went to Paris') is NOT an
    eventive occurrence — it is left to the locative/naming seams (current behavior)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_event("I went to Paris") is None


@requires_model
def test_analyze_event_propn_dobj_possession_not_poached():
    """Fail-safe: a PROPN direct object of a possession support verb ('I have Fido') is
    POSSESSION of a named entity, NOT an occurrence — the dobj path stays NOUN-only."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_event("I have Fido") is None


@requires_model
def test_analyze_event_common_noun_occurrences_unchanged():
    """Regression: ordinary common-noun LVC occurrences reify exactly as before the T3 change."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_event("I had a dentist visit").event == "dentist visit"
    assert m.analyze_event("I went to a concert").event == "concert"
    assert m.analyze_event("I attended a workshop").event == "workshop"


# ───────────── SIMPLEST-FORM EVENT NAMING (participle-object descent) — EVENT-SIMPLEST-FORM ─────────────
# The light-verb-with-gerund idiom "I had a great TIME celebrating my BIRTHDAY" bleaches the eventive
# content onto the reduced participle ("celebrating"); the matrix dobj "time" is an experiential/
# duration CONTAINER, not the event. Before the fix the occurrence surfaced under the verb-phrase
# paraphrase ("great time celebrating"), so a "birthday party" anchor could never reach it. The
# participle-object descent files the occurrence under the simplest event head noun ("birthday") and
# suppresses the bleached container twin. Deterministic (dep_/pos_/tag_/lemma), first-person grammatical.

@requires_model
def test_event_simplest_form_container_host_yields_participle_object():
    """'had a great time celebrating my birthday' → occurrence 'birthday' (not 'great time ...'); the
    bleached container host 'time' is suppressed as the paraphrase twin."""
    m = _reload(LINGUISTIC_LAYER="true")
    evs = m.analyze_events("I had a great time celebrating my birthday.")
    surfaces = {e.event for e in evs}
    assert "birthday" in surfaces, f"expected simplest-form 'birthday', got {surfaces!r}"
    assert not any("time" in s for s in surfaces), \
        f"bleached container paraphrase twin not suppressed: {surfaces!r}"


@requires_model
def test_event_simplest_form_advcl_off_verb():
    """'went out celebrating my birthday' — the participle is an advcl of the verb (no container dobj);
    the real event 'birthday' is recovered from the participle's object."""
    m = _reload(LINGUISTIC_LAYER="true")
    evs = m.analyze_events("I went out celebrating my birthday.")
    assert "birthday" in {e.event for e in evs}, f"advcl participle-object descent failed: {evs!r}"


@requires_model
def test_event_simplest_form_genuine_event_host_kept_additive():
    """A GENUINE event host ('workshop') is NOT in the container class, so it is KEPT while the
    participle's object ('anniversary') is ADDED — additive, never a regression (workshop survives)."""
    m = _reload(LINGUISTIC_LAYER="true")
    surfaces = {e.event for e in m.analyze_events("I attended the workshop celebrating our anniversary.")}
    assert any("workshop" in s for s in surfaces), f"genuine event host suppressed (regression): {surfaces!r}"
    assert "anniversary" in surfaces, f"participle object not added: {surfaces!r}"


@requires_model
def test_event_simplest_form_no_event_np_leaves_container_untouched():
    """No clean event NP under the participle (intransitive gerund) → no descent, no suppression: the
    container reading is left exactly as today (no fabrication)."""
    m = _reload(LINGUISTIC_LAYER="true")
    # bare container, no participle at all → unchanged
    assert m.analyze_event("I had a great time").event == "great time"
    # participle with NO nominal object ("dancing") → container host NOT suppressed (leave as-is)
    surfaces = {e.event for e in m.analyze_events("I had a great time dancing.")}
    assert any("time" in s for s in surfaces), \
        f"container wrongly suppressed with no event NP under participle: {surfaces!r}"


# ─────────────── QUOTED-TITLED-EVENT lane (LME q a3838d2b — event-participation) ───────────────
# A NAMED charity/participation event stated as a QUOTED title apposed to a generic eventive head
# ("'Run for the Cure' event") is routinely mis-parsed by spaCy (the opening title word mis-tags,
# the internal "for"-PP fragments the span, the head noun is buried) so analyze_events returns []
# and the dated participation is shredded. analyze_quoted_titled_event_occurrences recovers the
# occurrence from the quote + following common-noun head under a 1st-person predicate.

@requires_model
def test_quoted_titled_event_ran_in_pivot():
    """The pivot: 'I ran … in the "Run for the Cure" event' — the LVC verb ("ran") is not a support
    verb AND spaCy mis-tags 'Run' as a VERB, so analyze_events misses it; the quoted-title lane
    recovers title='Run for the Cure' filed at the head noun 'event'."""
    m = _reload(LINGUISTIC_LAYER="true")
    evs = m.analyze_quoted_titled_event_occurrences(
        "I just ran 5 kilometers in the 'Run for the Cure' event and raised $500.")
    assert len(evs) == 1, f"expected 1 quoted event, got {[e.event for e in evs]}"
    assert evs[0].event == "event"
    assert evs[0].title == "Run for the Cure"
    assert evs[0].negated is False


@requires_model
def test_quoted_titled_event_volunteered_at():
    """'I volunteered at the "Food for Thought" event.' → title bound to the head noun 'event'."""
    m = _reload(LINGUISTIC_LAYER="true")
    evs = m.analyze_quoted_titled_event_occurrences("I volunteered at the 'Food for Thought' event.")
    assert len(evs) == 1
    assert evs[0].event == "event"
    assert evs[0].title == "Food for Thought"


@requires_model
def test_quoted_titled_event_compound_head():
    """A compound head after the quote is kept whole: 'the "Bike-a-Thon" charity event' → head
    phrase 'charity event', title 'Bike-a-Thon'."""
    m = _reload(LINGUISTIC_LAYER="true")
    evs = m.analyze_quoted_titled_event_occurrences(
        "I recently participated in the 'Bike-a-Thon' charity event.")
    assert len(evs) == 1
    assert evs[0].event == "charity event"
    assert evs[0].title == "Bike-a-Thon"


@requires_model
def test_quoted_titled_event_no_following_head_not_fired():
    """A quoted WORK with NO following common-noun head ('I read "Dune"') is left to the titled-work
    lane — the quoted-event lane must NOT poach it (no event-type classifier after the quote)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_quoted_titled_event_occurrences("I read 'Dune'.") == []
    assert m.analyze_quoted_titled_event_occurrences("I finished reading 'The Hate U Give'.") == []


@requires_model
def test_quoted_titled_event_subject_position_not_fired():
    """A subject-position quoted event ('The "Food for Thought" event was great') carries no
    1st-person-governing predicate → not admitted (we only capture events the user took part in)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_quoted_titled_event_occurrences("The 'Food for Thought' event was great.") == []


def test_lvc_support_includes_volunteer():
    """'volunteer' is in the LVC support-verb code-fallback seed (mirrors mig 191's public seed) so a
    DB-down / unwarmed-overlay turn detects 'I volunteered at the <event>' via analyze_events. Asserts
    the code-fallback frozenset membership (DB-independent); the live overlay resolves seed ∪ tenant.
    The end-to-end quoted-title binding is covered by the quoted-titled-event lane tests above (which
    do not depend on the light-verb class at all)."""
    from src.extraction import linguistics as m
    assert "volunteer" in m._LVC_SUPPORT_VERB_LEMMAS


# ─────────────────────── INCHOATIVE / INGRESSIVE START-VERB (q7 gap) ───────────────────────
# "I started <item> on DATE" — the event meaning is on the ingressive VERB, not an eventive noun,
# so analyze_event misses it. analyze_inchoative recognizes the ingressive-verb + concrete-direct-
# object shape so the dated start binds to the item. Subject-agnostic, grammatical, NO word-list
# (the ingressive verb class is the DB-grown linguistic_cues overlay; here the code-fallback seed).

@requires_model
def test_analyze_inchoative_started_item():
    """The marigold real turn (q7): 'I just started some marigold seeds …' → item 'marigold seeds'."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative("I just started some marigold seeds, and they are already germinating.")
    assert ev is not None
    assert ev.event == "marigold seeds"   # head + left compound
    assert ev.negated is False


@requires_model
def test_analyze_inchoative_progressive_xcomp_noun():
    """The tomato real turn (q7): 'I have been starting seeds indoors …' → item 'seeds' (xcomp NOUN)."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative(
        "I have been starting seeds indoors under grow lights in my basement.")
    assert ev is not None
    assert ev.event == "seeds"
    assert ev.negated is False


@requires_model
def test_analyze_inchoative_subject_agnostic_begin():
    """Subject-agnostic: a DIFFERENT ingressive verb + item ('began piano lessons') fires too."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative("I began piano lessons.")
    assert ev is not None
    assert ev.event == "piano lessons"


@requires_model
def test_analyze_inchoative_verb_complement_rejected():
    """Over-capture guard: a VERB complement ('started crying' / 'started to think') is NOT a dated
    activity-on-a-thing — the object-is-NOUN guard rejects it (no junk occurrence)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_inchoative("I started crying.") is None
    assert m.analyze_inchoative("I started to think about it.") is None


@requires_model
def test_analyze_inchoative_negated_flagged_not_dropped_at_gate():
    """A negated ingressive ('I did not start the seeds') returns negated=True so the CALLER drops it
    — the negation/correction gate is never consulted here (parity with analyze_event)."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative("I did not start the seeds.")
    assert ev is not None
    assert ev.negated is True


@requires_model
def test_analyze_inchoative_non_self_subject_no_user_occurrence():
    """1st-person subject required for a user occurrence: 'The project began in March' has a
    non-self subject and no direct object, so it yields no (user, …) inchoative occurrence."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_inchoative("The project began in March.") is None


# ── Q8 CROP-ANCHOR REACHABILITY ─────────────────────────────────────────────────────────────────
# An inchoative start whose dated occurrence binds only to a GENERIC head ("seeds") is unreachable
# from the SPECIFIC operand a comparison asks about ("tomatoes"/"marigolds"). analyze_inchoative now
# surfaces the crop ANCHORS (compound modifier + coordination/apposition list members) as
# (canonical, plural) morphological pairs so the occurrence is reachable from either operand form.
# Subject-agnostic, dependency-driven, NO crop word-list.

@requires_model
def test_inchoative_compound_crop_anchor_marigold():
    """'I started marigold seeds …' surfaces the crop 'marigold' (compound modifier) with its plural
    variant 'marigolds' so the operand 'marigolds' reaches the dated start (Q8)."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative("I started marigold seeds that arrived on March 3rd.")
    assert ev is not None and ev.negated is False
    assert ("marigold", "marigolds") in ev.anchors


@requires_model
def test_inchoative_compound_crop_anchor_tomato_progressive():
    """'I have been starting tomato seeds indoors …' surfaces 'tomato'/'tomatoes' even though spaCy
    chains the crop two compounds deep (tomato→seeds→indoors); the generic 'seeds' is NOT anchored."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative("I have been starting tomato seeds indoors since February 20th.")
    assert ev is not None
    assert ("tomato", "tomatoes") in ev.anchors
    _canons = {c for c, _ in ev.anchors}
    assert "seed" not in _canons and "seeds" not in _canons  # generic head not anchored


@requires_model
def test_inchoative_conjunct_apposition_crop_list_per_crop():
    """A coordination/apposition crop list off the generic head emits ONE anchor per named crop."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative("I've been starting seeds indoors - tomatoes, peppers, cucumbers.")
    assert ev is not None
    _canons = {c for c, _ in ev.anchors}
    assert {"tomato", "pepper", "cucumber"} <= _canons


@requires_model
def test_inchoative_bare_item_has_no_false_crop_anchor():
    """A start with NO specific crop ('a new garden') yields no anchors — the caller keeps the bare
    item; we never confabulate a crop (HARD LINE: only a real specific type is anchored)."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative("I started a new garden.")
    assert ev is not None
    assert ev.anchors == ()


@requires_model
def test_inchoative_crop_anchor_excludes_date_fragment():
    """A PROPN/NOUN date fragment in the clause is NEVER surfaced as a crop anchor (date spans are
    excluded via the same detector the temporal lane uses)."""
    m = _reload(LINGUISTIC_LAYER="true")
    ev = m.analyze_inchoative("I started marigold seeds that arrived on March 3rd.")
    assert ev is not None
    _canons = {c for c, _ in ev.anchors}
    assert "march" not in _canons


def test_morph_variants_round_trip_regular_nouns():
    """The deterministic singular/plural morphology round-trips for regular nouns (NO word-list)."""
    import src.extraction.linguistics as _ling
    assert _ling._morph_variants("marigold") == ("marigold", "marigolds")
    assert _ling._morph_variants("marigolds") == ("marigold", "marigolds")
    assert _ling._morph_variants("tomato") == ("tomato", "tomatoes")
    assert _ling._morph_variants("tomatoes") == ("tomato", "tomatoes")
    assert _ling._morph_variants("cherry") == ("cherry", "cherries")
    assert _ling._pluralize_en("box") == "boxes"


# ─────────────── ASPECTUAL SUBJECT-CONTROL DESCENT (split SVO across an aspectual aux) ───────────
# "I started working with Rachel" splits the subject ("I", raised to the matrix "started") from the
# object ("Rachel", on the xcomp "working"). The flat SVO lanes require subject AND object on the same
# verb, so the engine derived ``work_with`` then discarded it for lack of a co-located subject. The fix
# DESCENDS into a REALIZED-activity aspectual ``-ing`` xcomp and runs the SAME SVO recovery there, using
# the matrix subject — minting (user, work_with, rachel) exactly as "I work with Rachel" does. The
# INTENT firewall (catenative/mental-state matrix, or an infinitival ``to`` complement) blocks
# unrealized intent ("I want to buy a car", "I considered hiring Sarah") from ever minting.


def _svo_tuples(m, text):
    """(subject-or-'user', predicate, object) for each non-negated SVORelation from analyze_svo."""
    out = []
    for r in m.analyze_svo_relations(text):
        if r.negated:
            continue
        subj = "user" if r.subject_is_self else r.subject_text
        out.append((subj, r.predicate, r.object_text))
    return out


@requires_model
@pytest.mark.parametrize("sent,expected", [
    ("I started working with Rachel on 2/15.", ("user", "work_with", "rachel")),
    ("I began managing the Anderson account.", ("user", "manage", "anderson account")),
    ("I kept emailing Tom.",                    ("user", "email", "tom")),
    ("I continued reviewing the report.",       ("user", "review", "report")),
    ("I resumed training the model.",           ("user", "train", "model")),
    ("I finished writing the report.",          ("user", "write", "report")),
    ("I stopped using Slack.",                  ("user", "use", "slack")),
])
def test_svo_aspectual_xcomp_descent_mints_activity(sent, expected):
    """An aspectual/phase matrix (start/begin/keep/continue/resume/finish/stop) + a progressive -ing
    activity xcomp descends and mints (user, <activity-verb>, <object>) via the SAME machinery as the
    flat SVO — the subject is recovered from the matrix, the predicate+object from the xcomp."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert expected in _svo_tuples(m, sent), \
        f"{sent!r}: expected {expected} not in {_svo_tuples(m, sent)!r}"


@requires_model
def test_svo_flat_work_with_no_regression():
    """The base flat-SVO case ("I work with Rachel") is unchanged by the descent — same edge."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert ("user", "work_with", "rachel") in _svo_tuples(m, "I work with Rachel.")


@requires_model
@pytest.mark.parametrize("sent,forbidden", [
    ("I want to buy a car.",          ("buy", "car")),
    ("I plan to visit Paris.",        ("visit", "paris")),
    ("I tried to call Tom.",          ("call", "tom")),
    ("I am thinking of getting a cow.", ("get", "cow")),
    ("I considered hiring Sarah.",    ("hire", "sarah")),    # mental-state matrix (-ing complement)
    ("I like working with Rachel.",   ("work_with", "rachel")),  # catenative preference (-ing comp.)
    ("I enjoy working with Rachel.",  ("work_with", "rachel")),  # catenative preference (-ing comp.)
    ("I avoided talking to Tom.",     ("talk_to", "tom")),    # non-aspectual matrix → no admit
])
def test_svo_aspectual_descent_does_not_mint_unrealized_intent(sent, forbidden):
    """The INTENT firewall: an infinitival-to complement OR a catenative/mental-state matrix must NOT
    descend — an unrealized desire/opinion/avoidance is NOT a thing the user did (user is truth)."""
    m = _reload(LINGUISTIC_LAYER="true")
    pred, obj = forbidden
    got = _svo_tuples(m, sent)
    assert not any(p == pred and o == obj for (_s, p, o) in got), \
        f"{sent!r}: minted forbidden intent edge {forbidden} in {got!r}"


@requires_model
@pytest.mark.parametrize("sent", ["I started crying.", "I started to think about it."])
def test_svo_aspectual_descent_verb_complement_no_edge(sent):
    """A bare verb complement ("started crying") / infinitival ("started to think") has no
    activity-on-a-thing object → no edge, no junk."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert _svo_tuples(m, sent) == [], f"{sent!r}: minted a bogus edge {_svo_tuples(m, sent)!r}"


@requires_model
def test_svo_aspectual_descent_via_deriver_binds_date_to_activity():
    """The deriver path (_chain_svo) also descends, and the event_date on "on 2/15" binds to the
    DESCENDED activity verb ("working"), not the aspectual matrix — (user, work_with, rachel) dated."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I started working with Rachel on 2/15.", _dt(2026, 2, 20))
    hit = [f for f in facts if f.subject == "user" and f.rel_type == "work_with"
           and f.object == "rachel"]
    assert hit, f"deriver did not mint the descended SVO edge: {facts!r}"
    assert any(f.event_date and f.event_date.startswith("2026-02-15") for f in hit), \
        f"date did not bind to the activity verb: {hit!r}"


# ─── DASH/COLON-INTRODUCED SPECIFYING LIST (gap-class: loose apposition enumerating a generic ────
# event object). A dash/colon followed by a coordinated NP list SPECIFIES the generic object of the
# preceding clause's event ("starting seeds … - tomatoes, peppers, cucumbers"); spaCy mis-parses the
# dash boundary so the seed-starting EVENT loses its real objects. _chain_dash_specifier reads the
# surface specification structure and DISTRIBUTES the event (with its date) over each named member.
# Subject-agnostic — the non-tomato cases (tools, languages, a single non-tomato crop) assert the
# class generalizes; "tomatoes" is just the LongMemEval Q8 reproducer.

@requires_model
@pytest.mark.parametrize("sent,predicate,members,want_date", [
    # NON-tomato / non-produce — the SAME shape with hardware (no crop vocabulary at all). NON-
    # inchoative ("buy"): the members stay BARE (no generic-head combine, no reification).
    ("I bought some tools - a hammer, a saw, and a drill.",
     "buy", {"hammer", "saw", "drill"}, None),
    # NON-tomato — a colon separator and proper-noun list members. NON-inchoative ("learn"): bare.
    ("I am learning languages: Python and Rust.",
     "learn", {"python", "rust"}, None),
])
def test_dash_specifier_distributes_event_over_named_list(sent, predicate, members, want_date):
    """A NON-inchoative dash/colon-introduced specifying list distributes the preceding event over each
    named member, carrying the event's date, with the member kept BARE (no generic-head combine).
    Subject-agnostic (hardware, languages); structural, no word list. The INCHOATIVE case (start) is
    covered separately by test_dash_specifier_inchoative_combines_generic_for_reification."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 21))
    got = {f.object for f in facts if f.subject == "user" and f.rel_type == predicate}
    assert members <= got, (
        f"dash-specifier did not distribute {predicate!r} over {members!r}: got {got!r}")
    if want_date is not None:
        dated = [f for f in facts if f.subject == "user" and f.rel_type == predicate
                 and f.object in members]
        assert dated and all(f.event_date and f.event_date.startswith(want_date) for f in dated), (
            f"date {want_date!r} did not follow the event onto the members: {dated!r}")


@requires_model
def test_dash_specifier_inchoative_combines_generic_for_reification():
    """Q8 fix (operand-tier parity): an INCHOATIVE dash list ("starting seeds … - tomatoes, peppers,
    cucumbers") combines each crop with the generic event head ("seeds") into a multi-token NAMED
    object ("tomato seed") so the harvest seam can REIFY it as a Class-A participated_in occurrence —
    matching the sibling "I started marigold seeds" turn so both comparison operands sit on the SAME
    tier. The crop is singular-folded; the generic head rides along; the date follows the event.
    Subject-agnostic: gated on the DB-grown inchoative cue class, not crop vocabulary."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I have been starting seeds indoors in my basement since February 20th - "
        "tomatoes, peppers, and cucumbers are all doing well.", _dt(2026, 6, 21))
    objs = {f.object for f in facts if f.subject == "user" and f.rel_type == "start"}
    # the crop is combined with the generic head and singular-folded (tomatoes → "tomato seed")
    assert {"tomato seed", "pepper seed", "cucumber seed"} <= objs, (
        f"inchoative dash did not combine crop+generic for reification: {objs!r}")
    # the date follows the event onto each combined member
    dated = [f for f in facts if f.subject == "user" and f.rel_type == "start"
             and f.object in {"tomato seed", "pepper seed", "cucumber seed"}]
    assert dated and all(f.event_date and f.event_date.startswith("2026-02-20") for f in dated), (
        f"date did not follow the inchoative event onto the combined members: {dated!r}")


@requires_model
def test_dash_specifier_ignores_intra_word_hyphen():
    """A glued hyphen in a compound ("mid-February") is NOT a clause-boundary separator — the chain
    must NOT treat the token after it as a specifier list. "I planted tomatoes, peppers, and cucumbers
    in mid-February" keeps its three planted objects and never mints (user, plant, february)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I planted tomatoes, peppers, and cucumbers in mid-February.", _dt(2026, 6, 21))
    objs = {f.object for f in facts if f.subject == "user" and f.rel_type == "plant"}
    assert {"tomatoes", "peppers", "cucumbers"} <= objs, f"lost a planted object: {objs!r}"
    assert "february" not in objs, f"intra-word hyphen wrongly distributed onto a date: {objs!r}"


@requires_model
def test_dash_specifier_strips_stray_date_token_from_member():
    """The date "February 20th" sits immediately before the dash, and spaCy attaches "20th" as a
    compound of the first list member ("tomatoes"). The specifier NP builder must DROP that stray
    date/ordinal token — the member is "tomatoes", never "20th tomatoes"."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I have been starting seeds in my basement since February 20th - "
        "tomatoes, peppers, and cucumbers are all doing well.", _dt(2026, 6, 21))
    starts = {f.object for f in facts if f.subject == "user" and f.rel_type == "start"}
    # the inchoative "start" combines the crop with the generic head ("tomato seed"); the stray date
    # token "20th" must never glue onto the member regardless of the combine.
    assert "tomato seed" in starts, f"tomato member not recovered cleanly: {starts!r}"
    assert not any("20th" in (o or "") for o in starts), \
        f"stray date token glued onto a specifier member: {starts!r}"


@requires_model
def test_dash_specifier_past_simple_recovers_members():
    """FIX #3 — PAST-SIMPLE POS MIS-TAG robustness. spaCy mis-tags the generic object of a past-simple
    event verb as a VERB ``xcomp`` ("I started seeds … - tomatoes …" → ``seeds`` pos=VERB, dep=xcomp of
    ``started``), so the NOUN/PROPN generic-object fallback rejects it and the whole dash list is lost
    (generic_obj=None → continue → ZERO members). The resolver must accept the immediate post-event-verb
    ``xcomp`` head even when mis-tagged VERB, so the past-simple form recovers the SAME combined,
    reifiable members the progressive form does. Structural, no word list."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I started seeds in my basement since February 20th - "
        "tomatoes, peppers, and cucumbers are all doing well.", _dt(2026, 6, 21))
    objs = {f.object for f in facts if f.subject == "user" and f.rel_type == "start"}
    # at minimum the first combined crop must be recovered (past-simple no longer drops to zero)
    assert "tomato seed" in objs, (
        f"past-simple inchoative dash dropped its members (VERB-mistag not recovered): {objs!r}")
    # the stray date token never glues onto the member regardless of the combine
    assert not any("20th" in (o or "") for o in objs), \
        f"stray date token glued onto a past-simple member: {objs!r}"


@requires_model
def test_dash_specifier_progressive_path_byte_identical():
    """FIX #3 guard — the progressive form ("I have been starting seeds …") tags the generic object
    NOUN/dobj and is resolved by the EXISTING NOUN/PROPN fallback, so it must NEVER reach the new
    VERB-xcomp branch. Assert the progressive members are byte-identical to the pre-fix behavior."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I have been starting seeds in my basement since February 20th - "
        "tomatoes, peppers, and cucumbers are all doing well.", _dt(2026, 6, 21))
    objs = {f.object for f in facts if f.subject == "user" and f.rel_type == "start"}
    assert {"tomato seed", "pepper seed", "cucumber seed"} <= objs, (
        f"progressive path regressed after the past-simple fix: {objs!r}")


@requires_model
def test_dash_specifier_non_inchoative_past_simple_unchanged():
    """FIX #3 subject-agnostic / non-inchoative guard. A non-inchoative past-simple dash event
    ("I fixed appliances - the washer, the dryer, and the fridge") distributes the bare members (no
    generic combine, no reification) exactly like the present-tense non-inchoative case."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I fixed appliances - the washer, the dryer, and the fridge.", _dt(2026, 6, 21))
    objs = {f.object for f in facts if f.subject == "user" and f.rel_type == "fix"}
    # the members recover and are kept BARE (no "<member> appliance" inchoative combine)
    assert "washer" in objs, f"non-inchoative past-simple dropped its members: {objs!r}"
    assert not any(o and "appliance" in o.split() and len(o.split()) > 1 for o in objs), \
        f"non-inchoative dash wrongly combined the generic head onto a member: {objs!r}"


@requires_model
def test_keep_plus_noun_object_does_not_become_inchoative_occurrence():
    """Cross-seam guard: extending the ASPECTUAL set with keep/continue must NOT broaden
    ``analyze_inchoative`` (which keys on its OWN inchoative set). "I kept the receipt" is a flat
    keep+NOUN SVO, NEVER a (user, participated_in, receipt) ingressive occurrence."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_inchoative("I kept the receipt.") is None
    assert ("user", "keep", "receipt") in _svo_tuples(m, "I kept the receipt.")


# ─── PREPOSITION-AWARE PREDICATE CONVERGENCE (Q7 prep-blind works_for regression) ─────────────
# The GLiNER2 relation scorer is PREP-BLIND: "work WITH X" (collaboration) and "work FOR X"
# (employment) both score onto the SAME seeded rel. The SVO predicate already folds the load-
# bearing prep ("work_with" vs "work_for") and THAT prep disambiguates the relation. The
# convergence in _emit must let a minted rel override the SVO predicate ONLY when it does not
# clash with the verb's governing prep — a prep-blind minted rel that omits the disambiguating
# prep loses to the prep-bearing SVO predicate. (Deterministic grammar; subject-agnostic.)

def _derive_with_minted(m, text, obj_prefix, minted_rel, ref):
    """Inject a prep-blind GLiNER2-style minted rel on the (first-person subject, object) pair —
    exactly as the live sentence-pipeline does via set_minted_rel — then run the deriver and
    return the rel set for the object. Simulates the GLiNER2 relation pass without the GPU model."""
    doc = m._parse(text)
    subj_i = next((t.i for t in doc if m._is_first_person_personal_pronoun(t)), None)
    obj_i = next((t.i for t in doc if (t.text or "").lower().startswith(obj_prefix)), None)
    if subj_i is not None and obj_i is not None:
        m.set_minted_rel(doc, subj_i, obj_i, minted_rel)
    facts = m.derive_sentence_facts(doc, ref)
    return sorted({f.rel_type for f in facts if f.object and obj_prefix in f.object})


@requires_model
@pytest.mark.parametrize("sent,obj_prefix", [
    ("I started working with Rachel", "rach"),
    ("I work with Rachel",            "rach"),
])
def test_prep_blind_minted_works_for_loses_to_work_with(sent, obj_prefix):
    """REGRESSION (Q7 prep-blind works_for): a prep-blind minted ``works_for`` must NOT override the
    prep-correct ``work_with`` — "work WITH X" is collaboration, not employment. Only ``work_with``
    survives; the spurious ``works_for`` is dropped for this pair."""
    m = _reload(LINGUISTIC_LAYER="true")
    rels = _derive_with_minted(m, sent, obj_prefix, "works_for", _dt(2026, 2, 20))
    assert rels == ["work_with"], f"{sent!r}: prep-blind works_for leaked: {rels!r}"


@requires_model
@pytest.mark.parametrize("sent,obj_prefix", [
    ("I work for Acme Corp",   "acme"),
    ("I started working for Acme", "acme"),
])
def test_prep_correct_minted_works_for_still_wins_for_employment(sent, obj_prefix):
    """The guard does NOT over-suppress: "work FOR X" IS employment, so a minted ``works_for`` HONORS
    the "for" prep and is allowed to win (it is the seeded employment rel). Asserts the employment
    rel survives (``works_for`` or the SVO ``work_for`` that canonicalizes to it downstream)."""
    m = _reload(LINGUISTIC_LAYER="true")
    rels = _derive_with_minted(m, sent, obj_prefix, "works_for", _dt(2026, 2, 20))
    assert any(r in ("works_for", "work_for") for r in rels), \
        f"{sent!r}: employment rel was wrongly suppressed: {rels!r}"
    assert "work_with" not in rels, f"{sent!r}: minted a collaboration rel for a 'for' clause: {rels!r}"


@requires_model
def test_non_prep_verb_minted_rel_still_wins_convergence_intact():
    """The carve-out is NOVELTY-scoped: "meet" is a KNOWN ontology rel (it folds to the seeded
    symmetric ``met``), so a minted ``knows`` for "I met Rachel" is a legitimate ontology-level choice
    and STILL WINS (today's convergence). Only a genuinely-novel verb loses to its content predicate.
    In this no-DB unit env the fail-safe (ontology not loaded → mint wins) yields the same verdict."""
    m = _reload(LINGUISTIC_LAYER="true")
    rels = _derive_with_minted(m, "I met Rachel", "rach", "knows", _dt(2026, 2, 20))
    assert rels == ["knows"], f"minted rel convergence broke for a known-rel verb: {rels!r}"


# ─────────────────────────── Q4 ACQUISITION / TRANSFER-OF-POSSESSION ───────────────────────
# An acquisition is a CROSS-SECTION (who/what/where/when): the user→device ownership linkage is
# INFERRED and EXPOSED, the locative is kept as a co-bound LOCATION (not promoted to the event), and
# the device is a dated, comparison-resolvable object. These tests assert the deriver + the seam.

@requires_model
def test_acquisition_infers_device_and_location_clean_alias():
    """"I got a Samsung Galaxy S22 at the mall on Feb 20" → device='samsung galaxy s22' (clean — no
    'a'/'new' cruft, full brand+model), location='mall' kept (NOT promoted), not negated."""
    m = _reload(LINGUISTIC_LAYER="true")
    a = m.analyze_acquisition("I got a Samsung Galaxy S22 at the mall on February 20th.")
    assert a is not None
    assert a.device == "samsung galaxy s22"
    assert a.location == "mall"
    assert a.negated is False


@requires_model
def test_acquisition_strips_leading_evaluative_amod():
    """A leading evaluative ADJ ('new') is determiner-cruft a comparison operand must not carry."""
    m = _reload(LINGUISTIC_LAYER="true")
    a = m.analyze_acquisition("I got a new Samsung Galaxy S22 at the store.")
    assert a is not None and a.device == "samsung galaxy s22"


@requires_model
def test_acquisition_recovers_right_side_model_number():
    """"a Dell XPS 13" — the model number is a right-side nummod; it must be kept in the device NP."""
    m = _reload(LINGUISTIC_LAYER="true")
    a = m.analyze_acquisition("I bought a Dell XPS 13 last month.")
    assert a is not None and a.device == "dell xps 13"


@requires_model
def test_acquisition_is_subject_agnostic_buy_purchase():
    """The transfer-of-possession class is not get-only — buy/purchase fire the same lane."""
    m = _reload(LINGUISTIC_LAYER="true")
    for verb in ("bought", "purchased", "acquired"):
        a = m.analyze_acquisition(f"I {verb} a Lenovo ThinkPad at the shop.")
        assert a is not None and "lenovo thinkpad" in a.device, f"{verb}: {a!r}"


@requires_model
def test_acquisition_verb_complement_xcomp_is_rejected():
    """"I got to leave early" — a VERB xcomp complement is NOT a possession → None (no bogus owns)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_acquisition("I got to leave early.") is None


@requires_model
def test_acquisition_negation_is_detected():
    """A negated acquisition ('I didn't get a phone') is flagged so the caller skips it."""
    m = _reload(LINGUISTIC_LAYER="true")
    a = m.analyze_acquisition("I did not get a phone.")
    assert a is not None and a.negated is True


@requires_model
def test_bare_have_possession_is_not_acquisition():
    """POSSESSION-POS rule still holds for a bare 'I have a Samsung' — 'have' is NOT an acquisition
    verb (no transfer-of-possession event), so this lane does not fire (the possessive seam owns it)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_acquisition("I have a Samsung phone.") is None


@requires_model
def test_acquisition_relative_clause_that_form():
    """RELATIVE-CLAUSE acquisition parity: '<X> that I got <when>' — the acquisition verb is stranded
    inside a relcl whose antecedent IS the acquired object. The device is the antecedent, and the
    relative-time modifier inside the relcl is NOT leaked into the device name (the dominant idx-28
    'the 50mm lens that I got a month ago' fix)."""
    m = _reload(LINGUISTIC_LAYER="true")
    a = m.analyze_acquisition("the 50mm lens that I got a month ago")
    assert a is not None and a.device == "50 mm lens" and a.negated is False


@requires_model
def test_acquisition_reduced_relative_clause_form():
    """REDUCED relative ('the camera I bought last week' — no 'that') fires the same antecedent rule."""
    m = _reload(LINGUISTIC_LAYER="true")
    a = m.analyze_acquisition("I love the camera I bought last week.")
    assert a is not None and a.device == "camera"


@requires_model
def test_acquisition_relcl_strips_date_tokens_from_device():
    """The relative-time modifier's NUM/PROPN tokens ('3 weeks ago', 'Feb 20') must NOT leak into the
    device name — they live inside the relcl verb's subtree and are pruned (canonical-parity)."""
    m = _reload(LINGUISTIC_LAYER="true")
    a = m.analyze_acquisition("the Dell XPS 13 that I bought 3 weeks ago")
    assert a is not None and a.device == "dell xps 13"
    b = m.analyze_acquisition("the lens that I got on Feb 20")
    assert b is not None and b.device == "lens"


@requires_model
def test_acquisition_canonical_form_unchanged():
    """The canonical 'I got <X> <when>' path is byte-identical after the relcl branch (no regression)."""
    m = _reload(LINGUISTIC_LAYER="true")
    a = m.analyze_acquisition("I got a Canon prime lens three weeks ago.")
    assert a is not None and a.device == "canon lens" and a.location is None


@requires_model
def test_event_seam_exposes_owns_edge_dated_with_location():
    """The full _detect_event_states seam EXPOSES (user, owns, device) Class-B-bound (llm_inferred),
    dated, with the locative kept as a co-bound located_in — and SUPPRESSES the locative-as-event."""
    from src.api import main
    edges = main._detect_event_states(
        "I got a Samsung Galaxy S22 at the mall on February 20th.", reference=_dt(2026, 3, 1))
    owns = [e for e in edges if e["rel_type"] == "owns"]
    assert len(owns) == 1, f"expected one inferred owns edge, got {edges!r}"
    o = owns[0]
    assert o["subject"] == "user" and o["object"] == "samsung galaxy s22"
    assert o["fact_provenance"] == "llm_inferred"          # INFERRED → gate computes Class B
    assert o["event_date"].startswith("2026-02-20")        # dated, comparison-orderable
    # mall kept as a co-bound LOCATION, never promoted to a participated_in event
    locs = [e for e in edges if e["rel_type"] == "located_in" and e["object"] == "mall"]
    assert len(locs) == 1, f"mall not bound as a location: {edges!r}"
    assert not any(e["rel_type"] == "participated_in" for e in edges), \
        f"locative was wrongly promoted to an event: {edges!r}"


@requires_model
def test_event_seam_genuine_occurrence_unchanged_by_acquisition_lane():
    """No-regression: a genuine light-verb occurrence ('I went to a concert') is untouched — it still
    yields a participated_in edge and NO owns edge."""
    from src.api import main
    edges = main._detect_event_states("I went to a concert.", reference=_dt(2026, 3, 1))
    assert any(e["rel_type"] == "participated_in" and e["object"] == "concert" for e in edges)
    assert not any(e["rel_type"] == "owns" for e in edges)


# ─────────────────── FAMILY / KINSHIP CAPTURE (derive_sentence_facts chains) ───────────────────
# Fix 1: kinship-aware first-person possessive ("my mother" → kin rel, NOT owns).
# Fix 2: genitive name-binding ("my mother's name is Diane" → (diane,parent_of,user); role collapsed).
# Fix 3: copula measurement/scalar ("she is 62 years old" → (subject,age,62); "Sarah is 28" → age).
# Subject-agnostic, metadata-driven (kinship_noun/unit_scalar cue maps via the DB-down bootstrap),
# grammatical first-person. POSTGRES_DSN cleared so the overlay resolves to the bootstrap maps.

from datetime import datetime as _dt2


def _derive(text):
    os.environ.pop("POSTGRES_DSN", None)
    m = _reload(LINGUISTIC_LAYER="true")
    return m.derive_sentence_facts(text, _dt2(2026, 6, 23))


def _triples(facts):
    return {(f.subject, f.rel_type, f.object) for f in facts}


@requires_model
def test_kinship_possessive_is_not_ownership():
    """Fix 1: 'My mother is 62 years old' → kinship parent_of (mother is the parent of me),
    NOT (user, owns, mother). Also yields the age scalar."""
    t = _triples(_derive("My mother is 62 years old."))
    assert ("mother", "parent_of", "user") in t, t
    assert not any(rel == "owns" for (_s, rel, _o) in t), t
    assert ("mother", "age", "62") in t, t


@requires_model
def test_genitive_name_binding_first_person():
    """Fix 2: 'My mother's name is Diane' → (diane, parent_of, user); the role-noun 'mother' is
    COLLAPSED into the named person, never left as a standalone entity, and 'name' never leaks."""
    t = _triples(_derive("My mother's name is Diane."))
    assert ("diane", "parent_of", "user") in t, t
    assert not any(s == "name" or o == "name" for (s, _r, o) in t), t
    # 'mother' is NEVER a standalone ENTITY (subject of its own kin edge) — the role is collapsed
    # into the named person. Under SPINE_NAMING_CHAIN (Fix B Part 2) the role IS additionally
    # registered as an also_known_as ALIAS of the named person — (diane, also_known_as, mother) —
    # so a later split atom ("My mother is 62") resolves mother→diane. THE HARD LINE: the role is a
    # slot/alias ON the named instance, never a parallel entity. So 'mother' must never be a SUBJECT;
    # it may appear only as the also_known_as alias OBJECT when the flag is on.
    from src.extraction.linguistics import SPINE_NAMING_CHAIN as _SNC
    assert not any(s == "mother" for (s, _r, o) in t), t
    if _SNC:
        assert ("diane", "also_known_as", "mother") in t, t
    else:
        assert not any(o == "mother" for (_s, _r, o) in t), t


@requires_model
def test_genitive_name_binding_is_subject_agnostic():
    """Fix 2 subject-agnostic: 'John's mother's name is Susan' → (susan, parent_of, john)."""
    t = _triples(_derive("John's mother's name is Susan."))
    assert ("susan", "parent_of", "john") in t, t


@requires_model
def test_kinship_specific_rels_metadata_driven():
    """Fix 1/2: the specific kin rel comes from the kinship_noun cue map (son→child_of,
    wife→spouse), not an in-code literal."""
    assert ("desmond", "child_of", "user") in _triples(_derive("My son's name is Desmond.")), "son→child_of"
    assert ("dana", "spouse", "user") in _triples(_derive("My wife's name is Dana.")), "wife→spouse"


@requires_model
def test_copula_bare_number_is_age():
    """Fix 3: a bare NUM copula complement → age scalar. 'Sarah is 28' → (sarah, age, 28)."""
    assert ("sarah", "age", "28") in _triples(_derive("Sarah is 28.")), "bare-num age"


@requires_model
def test_copula_measurement_with_unit_and_coref():
    """Fix 3: 'My mother's name is Diane and she is 62 years old' → the age binds to Diane (the
    'she' antecedent), not to 'name' or the bare pronoun."""
    t = _triples(_derive("My mother's name is Diane and she is 62 years old."))
    assert ("diane", "age", "62") in t, t
    assert not any(s == "name" for (s, _r, _o) in t), t


@requires_model
def test_copula_height_unit_maps_to_height():
    """Fix 3: 'he is 6 feet tall' → (he, height, '6 feet') — unit 'feet'→height via the
    unit_scalar map; no junk (he, has_state, tall) state edge. Issue #52 amended the value: a
    degree-adjective measure keeps its unit unless the rel metadata declares a bare count
    (integer). The seeded metadata is pinned explicitly so the pin does not depend on a DSN."""
    os.environ.pop("POSTGRES_DSN", None)
    m = _reload(LINGUISTIC_LAYER="true")
    m._rel_overlay_meta_map = lambda: {
        "height": {"scalar_datatype": "quantity", "head_types": ["Person"],
                   "tail_types": ["SCALAR"]},
        "age": {"scalar_datatype": "integer", "head_types": ["ANY"], "tail_types": ["SCALAR"]}}
    try:
        t = _triples(m.derive_sentence_facts("he is 6 feet tall", _dt2(2026, 6, 23)))
    finally:
        _reload(LINGUISTIC_LAYER="true")
    assert ("he", "height", "6 feet") in t, t
    assert not any(rel == "has_state" for (_s, rel, _o) in t), t


@requires_model
def test_kinship_no_regression_on_component_possessive():
    """No-regression: a NON-kinship genitive ('my car's GPS') still splits to the mereology
    part_of reading, NOT a kin rel."""
    t = _triples(_derive("My car's GPS broke last week."))
    assert ("gps", "part_of", "car") in t, t
    assert ("user", "owns", "car") in t, t
    assert not any(rel in ("parent_of", "child_of", "spouse", "sibling_of") for (_s, rel, _o) in t), t


# ─── PART 1: DATE-PEEL ROBUSTNESS TO ATOMIZER WOBBLE (the Q7 "14 days" determinism residual) ───────
# The LLM atomizer reworded "I … saw a house … on 3/1" two ways across runs; the YEAR-LESS month-day
# variant ("on march 1st") let the SVO grammar fold "on" into a ``see_on`` particle and lift the DATE
# PHRASE as the relationship OBJECT instead of "house". The deriver now PEELS the date span's tokens
# out of the SVO predicate-fold + object-selection (deterministic, structural, subject-agnostic), so
# BOTH atomizations converge on (user, see, house)@event_date and the spurious ``see_on``/date-object
# edge can never form. The deriver is itself deterministic (the wobble was upstream); these prove the
# date-introducing PP can never corrupt the predicate/object regardless of phrasing.

@requires_model
@pytest.mark.parametrize("sent,want_date", [
    ("I saw a house on 3/1.", "2026-03-01"),          # numeric date (the HIT-style atomization)
    ("I saw a house on march 1st.", "2026-03-01"),    # YEAR-LESS month-day (the MISS-style trigger)
    ("I see house on march 1st.", "2026-03-01"),      # determiner-less reword — still (user, see, house)
])
def test_part1_date_peel_keeps_dobj_strips_bare_month_day(sent, want_date):
    """Whatever the atomizer's reword, the TRUE direct object ("house") is kept and the date is peeled
    to event_date — never a ``see_on`` predicate, never "march 1st"/"march" as the object."""
    facts = _derive(sent)
    hit = [f for f in facts if f.subject == "user" and f.rel_type == "see" and f.object == "house"]
    assert hit, f"date-peel lost the true object (expected (user, see, house)): {_triples(facts)!r}"
    assert any(f.event_date and f.event_date.startswith(want_date) for f in hit), \
        f"date not peeled to event_date: {hit!r}"
    # the wobble artefacts must NEVER appear: no see_on particle, no date-phrase object
    triples = _triples(facts)
    assert not any(rel == "see_on" for (_s, rel, _o) in triples), triples
    assert not any(obj in ("march 1st", "march", "1st") for (_s, _r, obj) in triples), triples


@requires_model
def test_part1_no_object_atomization_yields_no_date_object_edge():
    """The atomizer's worst reword DELETES the object ("I saw on march 1st" — no dobj at all). The
    OLD path folded "on" into ``see_on`` and emitted (user, see_on, "march 1st") — a date masquerading
    as a graph object. The peel makes that impossible: a date-only clause emits NO spurious edge (the
    date routes to the temporal lane; there is simply no true object to host it). Never a wrong fact."""
    facts = _derive("I saw on march 1st.")
    triples = _triples(facts)
    assert not any(rel == "see_on" for (_s, rel, _o) in triples), triples
    assert not any(obj in ("march 1st", "march", "1st") for (_s, _r, obj) in triples), triples


@requires_model
def test_part1_is_subject_agnostic_named_object():
    """Subject-agnostic: a year-less-date turn with a PROPER-NOUN object ("I met Sarah on march 5th")
    keeps the named object and peels the date — date never becomes the object, no literals."""
    facts = _derive("I met Sarah on march 5th.")
    hit = [f for f in facts if f.subject == "user" and f.rel_type == "meet" and f.object == "sarah"]
    assert hit, f"named object lost / date became object: {_triples(facts)!r}"
    assert any(f.event_date and f.event_date.startswith("2026-03-05") for f in hit), hit
    assert not any(obj in ("march 5th", "march", "5th") for (_s, _r, obj) in _triples(facts))


@requires_model
def test_part1_determinism_edge_stable_across_runs():
    """The deriver is deterministic: the same turn yields the SAME (subj, rel, obj, event_date) tuple
    on every run (the Q7 flicker lived in the LLM atomizer, which the peel now makes irrelevant — both
    rewords converge). Repeat the dense trigger N times → exactly ONE distinct outcome each."""
    for sent in ("I saw a house on march 1st.", "I see house on march 1st.", "I met Sarah on march 5th."):
        outcomes = set()
        for _ in range(8):
            facts = _derive(sent)
            outcomes.add(tuple(sorted(
                (f.subject, f.rel_type, f.object, f.event_date) for f in facts)))
        assert len(outcomes) == 1, f"non-deterministic deriver outcome for {sent!r}: {outcomes!r}"


@requires_model
def test_part1_genuine_phrasal_and_year_date_no_regression():
    """No-regression: a genuine load-bearing phrasal ("went to a concert" — pobj IS the object, no
    date) still folds to ``go_to``/concert; an explicit YEAR date ("on march 1st 2022") still parses
    and peels. The peel touches ONLY resolved date spans."""
    f1 = _derive("I went to a concert.")
    assert ("user", "go_to", "concert") in _triples(f1), _triples(f1)
    f2 = _derive("I saw a house on march 1, 2022.")
    hit = [f for f in f2 if f.subject == "user" and f.rel_type == "see" and f.object == "house"]
    assert hit and any(f.event_date and f.event_date.startswith("2022-03-01") for f in hit), \
        f"explicit-year date regressed: {_triples(f2)!r}"


# ─── PART 2: _object_is_temporal CATCHES YEAR-LESS MONTH-DAY (deterministic guard backstop) ─────────
# The deterministic guard that strips a temporal scalar leaked as a relationship object previously
# MISSED a year-less month-day ("march 1st"): _parse_relative_date excludes bare month-day and the
# scalar_atomic date regex requires a 4-digit year. The guard now also runs the SAME extract_event_date
# machinery (spaCy DATE NER + dateparser) the temporal lane uses, so a leaked bare month-day is caught
# and re-routed to event_date — while a non-date object is kept (no over-drop).

@requires_model
@pytest.mark.parametrize("obj,expected", [
    ("march 1st", True),        # YEAR-LESS month-day — the PART 2 gap (caught now)
    ("february 20th", True),    # subject-agnostic month-day
    ("march 5th", True),
    ("2021-03-01", True),       # explicit ISO date — already caught (no regression)
    ("two weeks ago", True),    # relative-time — already caught (no regression)
    ("yesterday", True),
    ("house", False),           # non-date noun — MUST be kept (no over-drop)
    ("dana", False),
    ("sarah", False),
    ("gps system", False),
])
def test_part2_object_is_temporal_catches_year_less_month_day(obj, expected):
    _reload(LINGUISTIC_LAYER="true")  # ensure the linguistics layer is loadable for the date machinery
    os.environ.pop("POSTGRES_DSN", None)
    from src.api import main as _main
    assert _main._object_is_temporal(obj) is expected, obj


# ───────────────────────── DEFECT 1: POSSESSIVE-ATTRIBUTE SCALAR ─────────────────────────
# "my address is 123 Main Street, Kitchener, Ontario" / "the laptop's serial is XR7-9920" must
# route the ENTIRE post-copula span VERBATIM as a SCALAR attribute value, keyed by the attribute
# head-noun, on the subject — never decomposed into a relationship to a mangled sub-entity, never
# read as an age. Subject-agnostic: detected by grammar + value-shape, NO attribute word-list.

@requires_model
def test_defect1_first_person_address_is_verbatim_scalar():
    # Pin SPINE_NAMING_CHAIN=false: this asserts the deterministic attr-scalar path, and the flag can
    # LEAK true from a prior test file's _reload (no env teardown) → the naming chain then mis-binds the
    # capitalized "Main Street" as a named instance. Pinning makes the test order-independent. (A known
    # SEPARATE pre-existing SPINE-on issue: the address copula also emits named-instance junk — out of
    # scope here; the scalar itself is always captured correctly.)
    m = _reload(LINGUISTIC_LAYER="true", SPINE_NAMING_CHAIN="false")
    facts = m.derive_sentence_facts(
        "my address is 123 Main Street, Kitchener, Ontario", _dt(2026, 6, 21))
    triples = {(f.subject, f.rel_type): f for f in facts}
    assert ("user", "address") in triples, \
        f"address scalar not captured: {[(f.subject, f.rel_type, f.object) for f in facts]!r}"
    sc = triples[("user", "address")]
    # FULL verbatim span (lowercased), not the head-only mangle ("street").
    assert sc.object == "123 main street, kitchener, ontario", repr(sc.object)
    assert sc.scalar_datatype == "string"
    # NO ownership of the bare attribute noun, NO age mis-read of "123".
    assert ("user", "owns") not in triples
    assert not any(f.rel_type == "age" for f in facts), \
        f"123 mis-read as age: {[(f.subject, f.rel_type, f.object) for f in facts]!r}"


@requires_model
def test_defect1_genitive_serial_is_verbatim_scalar_on_possessor():
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("the laptop's serial is XR7-9920", _dt(2026, 6, 21))
    triples = {(f.subject, f.rel_type): f for f in facts}
    assert ("laptop", "serial") in triples, \
        f"serial scalar not on possessor: {[(f.subject, f.rel_type, f.object) for f in facts]!r}"
    sc = triples[("laptop", "serial")]
    assert sc.object == "xr7-9920"
    assert sc.scalar_datatype == "string"
    # No mangled (serial, related_to, laptop) twin from the possessive chain.
    assert not any(f.rel_type == "related_to" for f in facts)


@requires_model
def test_defect1_numeric_attribute_is_scalar_not_age(monkeypatch):
    # No database: this pins the in-code cue FLOOR (the seeded-table divergence is #75;
    # it used to run DSN-less only because another module popped the DSN at import (#63).
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("my employee id is 4471", _dt(2026, 6, 21))
    # the value 4471 is captured as a scalar attribute, never as age.
    assert any(f.scalar_datatype == "string" and f.object == "4471" for f in facts), \
        f"employee id not captured as scalar: {[(f.subject, f.rel_type, f.object) for f in facts]!r}"
    assert not any(f.rel_type == "age" for f in facts)


@requires_model
def test_defect1_single_word_preference_is_NOT_scalar_attribute():
    # "my favorite color is blue" — a single-word value stays with the preference seam (NOT scalar).
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("my favorite color is blue", _dt(2026, 6, 21))
    assert not any(f.scalar_datatype for f in facts), \
        f"single-word preference wrongly captured as attr-scalar: {[(f.subject, f.rel_type, f.object) for f in facts]!r}"


@requires_model
def test_defect1_kinship_age_unaffected_by_attr_scalar():
    # "my daughter is 28" — a kinship subject is an AGE, never an attribute scalar.
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("my daughter is 28", _dt(2026, 6, 21))
    assert any(f.rel_type == "age" and f.object == "28" for f in facts), \
        f"kinship age lost: {[(f.subject, f.rel_type, f.object) for f in facts]!r}"
    assert not any(f.scalar_datatype for f in facts)


# ───────────────────────── DEFECT 2: CLASSIFICATION + CONTAINMENT ─────────────────────────
# "X is a <type> in <Location>" → instance_of(X, type) + located_in(X, Location); a geographic
# comma-list yields the containment edge located_in(child, parent). Subject-agnostic, no geo zoo.

@requires_model
def test_defect2_classification_and_containment():
    m = _reload(LINGUISTIC_LAYER="true")
    facts = {(f.subject, f.rel_type, f.object) for f in
             m.derive_sentence_facts("Kitchener is a city in Ontario", _dt(2026, 6, 21))}
    assert ("kitchener", "instance_of", "city") in facts, facts
    assert ("kitchener", "located_in", "ontario") in facts, facts


@requires_model
def test_defect2_public_reference_classification_containment():
    m = _reload(LINGUISTIC_LAYER="true")
    facts = {(f.subject, f.rel_type, f.object) for f in
             m.derive_sentence_facts("Paris is a city in France", _dt(2026, 6, 21))}
    assert ("paris", "instance_of", "city") in facts, facts
    assert ("paris", "located_in", "france") in facts, facts


@requires_model
def test_defect2_subject_agnostic_containment_non_geo():
    # "the server is in rack 4" — same containment chain, no instance_of (no type complement).
    m = _reload(LINGUISTIC_LAYER="true")
    facts = {(f.subject, f.rel_type, f.object) for f in
             m.derive_sentence_facts("the server is in rack 4", _dt(2026, 6, 21))}
    assert ("server", "located_in", "rack 4") in facts, facts


@requires_model
def test_defect2_geo_comma_list_fallback_locative_prep():
    # No GLiNER2 types (raw str): the PROPN appos/conj chain under a locative-prep pobj decomposes.
    m = _reload(LINGUISTIC_LAYER="true")
    facts = {(f.subject, f.rel_type, f.object) for f in
             m.derive_sentence_facts("I live in Kitchener, Ontario", _dt(2026, 6, 21))}
    assert ("kitchener", "located_in", "ontario") in facts, facts
    # Ontario is no longer dropped; the lives_in capture still stands.
    assert any(s == "user" and o == "kitchener" for (s, r, o) in facts), facts


@requires_model
def test_defect2_geo_comma_list_gliner2_locations():
    # GLiNER2 Location typing path: adjacent Location ents separated by a comma → located_in.
    import spacy  # noqa: F401
    from spacy.tokens import Span
    m = _reload(LINGUISTIC_LAYER="true")
    doc = m._parse("my address is 123 Main Street, Kitchener, Ontario")
    # tokens: my0 address1 is2 123_3 Main4 Street5 ,6 Kitchener7 ,8 Ontario9
    doc.set_ents([Span(doc, 7, 8, label="LOCATION"), Span(doc, 9, 10, label="LOCATION")])
    facts = {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(doc, _dt(2026, 6, 21))}
    # dual reading: the FULL address is the verbatim scalar AND the geo entities hierarchize.
    assert ("user", "address", "123 main street, kitchener, ontario") in facts, facts
    assert ("kitchener", "located_in", "ontario") in facts, facts


# ── NAME↔TYPE BINDER vs ATTR-SCALAR PRECEDENCE (pure helper — no model needed) ─────────────────────
# These exercise suppress_name_type_binder_vs_attr_scalar directly with a SYNTHETIC edge list mirroring
# the live /harvest-spans output for "My address is 123 Main Street, Kitchener, Ontario". The leak only
# manifests with LIVE GLiNER2 (it types "Street" as a named instance of the "address" type), so we feed
# the post-GLiNER2 edge cluster the binder mints and assert the union-level suppression drops the junk
# while preserving the verbatim attr-scalar edge AND the geo-containment `located_in` chain.

def test_binder_suppression_drops_junk_keeps_scalar_and_geo():
    edges = [
        {"subject": "user", "rel_type": "address",
         "object": "123 main street, kitchener, ontario", "object_datatype": "string"},
        {"subject": "123 main street", "rel_type": "located_in", "object": "kitchener"},
        {"subject": "kitchener", "rel_type": "located_in", "object": "ontario"},
        {"subject": "street", "rel_type": "also_known_as", "object": "address"},
        {"subject": "street", "rel_type": "instance_of", "object": "address"},
        {"subject": "street", "rel_type": "has_role", "object": "address"},
        {"subject": "user", "rel_type": "owns", "object": "street"},
        {"subject": "street", "rel_type": "age", "object": "123"},
    ]
    kept = ling.suppress_name_type_binder_vs_attr_scalar(edges)
    kept_keys = {(e["subject"], e["rel_type"], e["object"]) for e in kept}
    # the 3 KEEP edges survive
    assert ("user", "address", "123 main street, kitchener, ontario") in kept_keys, kept_keys
    assert ("123 main street", "located_in", "kitchener") in kept_keys, kept_keys
    assert ("kitchener", "located_in", "ontario") in kept_keys, kept_keys
    # the 5 binder-twin junk edges are gone
    assert ("street", "also_known_as", "address") not in kept_keys, kept_keys
    assert ("street", "instance_of", "address") not in kept_keys, kept_keys
    assert ("street", "has_role", "address") not in kept_keys, kept_keys
    assert ("user", "owns", "street") not in kept_keys, kept_keys
    assert ("street", "age", "123") not in kept_keys, kept_keys
    assert len(kept) == 3, kept_keys


def test_binder_suppression_subject_agnostic_license_plate():
    # public, non-personal example — "my license plate is ABC 123, registered in Ontario"
    edges = [
        {"subject": "user", "rel_type": "license_plate",
         "object": "abc 123, ontario", "object_datatype": "string"},
        {"subject": "abc 123", "rel_type": "located_in", "object": "ontario"},
        {"subject": "plate", "rel_type": "instance_of", "object": "license"},
        {"subject": "user", "rel_type": "owns", "object": "abc"},
        {"subject": "abc", "rel_type": "age", "object": "123"},
    ]
    kept = ling.suppress_name_type_binder_vs_attr_scalar(edges)
    kept_keys = {(e["subject"], e["rel_type"], e["object"]) for e in kept}
    assert ("user", "license_plate", "abc 123, ontario") in kept_keys, kept_keys
    assert ("abc 123", "located_in", "ontario") in kept_keys, kept_keys  # geo chain preserved
    assert ("user", "owns", "abc") not in kept_keys, kept_keys
    assert ("abc", "age", "123") not in kept_keys, kept_keys
    # "plate" shares no whole-word token with the value → NOT dropped (deterministic, no over-reach)
    assert ("plate", "instance_of", "license") in kept_keys, kept_keys


def test_binder_suppression_no_scalar_claim_is_noop():
    # No edge carries object_datatype → nothing is a scalar claim → list returned unchanged (fail-safe).
    edges = [
        {"subject": "street", "rel_type": "instance_of", "object": "address"},
        {"subject": "user", "rel_type": "owns", "object": "street"},
        {"subject": "kitchener", "rel_type": "located_in", "object": "ontario"},
    ]
    kept = ling.suppress_name_type_binder_vs_attr_scalar(edges)
    assert kept == edges, kept


def test_binder_suppression_never_drops_the_scalar_edge_itself():
    # A scalar edge whose own rel happens to be in the twin set is protected by the object_datatype guard.
    edges = [
        {"subject": "user", "rel_type": "age", "object": "42", "object_datatype": "string"},
    ]
    kept = ling.suppress_name_type_binder_vs_attr_scalar(edges)
    assert kept == edges, kept


# ─────────── Coordinated-list / collective / relative-pronoun (Task 1) ───────────

@requires_model
def test_coordinated_propn_list_appos_tail_not_dropped():
    # spaCy chains a PROPN list via appos (Apollo -appos-> Vault -conj-> Echo); the conj-only
    # walk dropped the tail. Subject-agnostic: every named server must be captured.
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("We run three servers: Apollo, Vault, and Echo.", None)
    objs = {(f.object or "").lower() for f in facts if f.rel_type == "run"}
    assert {"apollo", "vault", "echo"} <= objs, objs
    # no merged "apollo vault" style entity
    assert not any(" " in o and "apollo" in o for o in objs), objs


@requires_model
def test_kinship_collective_routes_to_kin_rel_not_owns():
    # "kids"/"children" → child_of (member is SUBJECT), NEVER (user, owns/have, kids).
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("We have three kids: Bianca, Dez, and Cato.", None)
    triples = {(f.subject, f.rel_type, (f.object or "").lower()) for f in facts}
    assert ("bianca", "child_of", "user") in triples, triples
    assert ("dez", "child_of", "user") in triples, triples
    assert ("cato", "child_of", "user") in triples, triples
    # the collective head never becomes an owned/have object
    assert not any(r in ("owns", "have") and o in ("kids", "kid", "children")
                   for (_s, r, o) in triples), triples


@requires_model
def test_kinship_collective_gendered_head_emits_gender():
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I have two sons: Alex and Ben.", None)
    triples = {(f.subject, f.rel_type, (f.object or "").lower()) for f in facts}
    assert ("alex", "child_of", "user") in triples, triples
    assert ("ben", "child_of", "user") in triples, triples
    assert ("alex", "has_gender", "male") in triples, triples


@requires_model
def test_non_kin_collective_members_each_captured():
    # "My team has Sarah, Tom, and Priya" → three people; no member dropped, no merge.
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("My team has Sarah, Tom, and Priya.", None)
    objs = {(f.object or "").lower() for f in facts}
    assert {"sarah", "tom", "priya"} <= objs, objs


@requires_model
def test_relative_pronoun_never_bound_as_entity():
    m = _reload(LINGUISTIC_LAYER="true")
    for sent in ("I have a brother who lives in Boston.",
                 "I met a man who knows Sarah.",
                 "I like the car that runs fast."):
        facts = m.derive_sentence_facts(sent, None)
        subjs = {(f.subject or "").lower() for f in facts}
        assert "who" not in subjs, (sent, subjs)
        assert "that" not in subjs, (sent, subjs)
        assert "which" not in subjs, (sent, subjs)


@requires_model
def test_relative_pronoun_resolves_to_antecedent():
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I have a brother who lives in Boston.", None)
    triples = {(f.subject, f.rel_type, (f.object or "").lower()) for f in facts}
    assert ("brother", "live_in", "boston") in triples, triples


# ─────────── Coordinated affective complements (Task 2) ───────────

@requires_model
def test_affect_complements_enumerates_full_coordinated_list():
    m = _reload(LINGUISTIC_LAYER="true")
    comps = m.analyze_copula_affect_complements(
        "I was extremely busy, overwhelmed, underappreciated, and exhausted last week.")
    assert comps == ["busy", "overwhelmed", "underappreciated", "exhausted"], comps


@requires_model
def test_affect_complements_two_adjectives():
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_copula_affect_complements("I'm excited and nervous.") == ["excited", "nervous"]


@requires_model
def test_affect_complements_skips_negated_conjunct():
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_copula_affect_complements("I was busy but not exhausted.") == ["busy"]


@requires_model
def test_affect_complements_empty_for_non_self_or_nominal():
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_copula_affect_complements("My favorite color is blue.") == []
    assert m.analyze_copula_affect_complements("I am a teacher.") == []
    # feel-verb construction is owned by _detect_feeling_states, not this copula enumerator
    assert m.analyze_copula_affect_complements("I feel overwhelmed.") == []


# ─────────────────────── COLLECTIVE MEMBER-LIST reconciliation (Task 1) ───────────────────────
# A "<subj> <verb> [<count>] <HEAD>: M1, M2, …" named enumeration was mangled by the generic chains:
# the collective HEAD became a type each member was ``instance_of`` / the user ``owns``, only the FIRST
# member was typed, and members lost their proper membership/kinship edge. The post-chain reconciliation
# routes by the HEAD's resolved cue/type, distributes to EVERY member, and drops the junk. These run on
# the str-fallback path (no GLiNER2); the kinship cue maps resolve via the bootstrap (no DB in test).

@requires_model
def test_collective_kinship_list_routes_every_member_to_kin_rel():
    """"We have three kids: Bianca, Dez, and Cato." → each member child_of the user; NO member is
    instance_of the collective "kids", and the user never ``owns``/``has`` a member."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "We have three kids: Bianca, Dez, and Cato.", _dt(2026, 6, 21))
    kids = {"bianca", "dez", "cato"}
    childof = {f.subject for f in facts if f.rel_type == "child_of" and f.object == "user"}
    assert kids <= childof, f"not every kid is child_of user: {facts!r}"
    # NO collective-noun type, NO possession junk for any member.
    assert not [f for f in facts
                if f.rel_type == "instance_of" and f.object in ("kids", "kid")], \
        f"a member was classified into the collective noun: {facts!r}"
    assert not [f for f in facts
                if f.rel_type in ("owns", "have", "has") and f.object in kids], \
        f"a member leaked as a possession of the subject: {facts!r}"


@requires_model
def test_collective_kinship_list_carries_intrinsic_gender():
    """A GENDERED kinship head distributes the intrinsic gender to each member (sons → male)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "We have three sons: Bianca, Dez, and Cato.", _dt(2026, 6, 21))
    for who in ("bianca", "dez", "cato"):
        assert any(f.subject == who and f.rel_type == "child_of" and f.object == "user"
                   for f in facts), f"{who} not child_of user: {facts!r}"
        assert any(f.subject == who and f.rel_type == "has_gender" and f.object == "male"
                   for f in facts), f"{who} missing intrinsic gender: {facts!r}"


@requires_model
def test_collective_group_list_member_of_and_singular_type():
    """A non-kin GROUP head with a non-self governor → each member instance_of the SINGULAR head +
    member_of the governing group; the plural collective is never a member's type."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "My team has three engineers: Sarah, Tom, and Priya.", _dt(2026, 6, 21))
    for who in ("sarah", "tom", "priya"):
        assert any(f.subject == who and f.rel_type == "instance_of" and f.object == "engineer"
                   for f in facts), f"{who} not instance_of singular 'engineer': {facts!r}"
        assert any(f.subject == who and f.rel_type == "member_of" and f.object == "team"
                   for f in facts), f"{who} not member_of team: {facts!r}"
    assert not [f for f in facts
                if f.rel_type == "instance_of" and f.object == "engineers"], \
        f"plural collective leaked as a type: {facts!r}"


@requires_model
def test_collective_group_list_self_activity_links_user():
    """A non-kin GROUP head with a SELF governor + an ACTIVITY verb → each member keeps the user's OWN
    verb ("we run servers" → (user, run, <server>)) and is instance_of the singular head."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "We run three servers: Apollo, Vault, and Echo.", _dt(2026, 6, 21))
    for who in ("apollo", "vault", "echo"):
        assert any(f.subject == "user" and f.rel_type == "run" and f.object == who
                   for f in facts), f"user not linked to {who} via 'run': {facts!r}"
        assert any(f.subject == who and f.rel_type == "instance_of" and f.object == "server"
                   for f in facts), f"{who} not instance_of singular 'server': {facts!r}"


@requires_model
def test_collective_drops_member_to_member_spurious_edge():
    """The members of an enumeration are distinct PEERS — any edge BETWEEN two of them (e.g. the geo
    chain reading "three cities: Paris, Tokyo, Cairo" as Paris located_in Tokyo) is dropped."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I have visited three cities: Paris, Tokyo, and Cairo.", _dt(2026, 6, 21))
    cities = {"paris", "tokyo", "cairo"}
    assert not [f for f in facts if f.subject in cities and f.object in cities], \
        f"spurious member-to-member edge survived: {facts!r}"
    for who in cities:
        assert any(f.subject == who and f.rel_type == "instance_of" and f.object == "city"
                   for f in facts), f"{who} not instance_of city: {facts!r}"


@requires_model
def test_collective_single_appositive_rename_does_not_fire():
    """A SINGLE proper-noun apposition rename ("I met my friend John") is NOT a collective list — the
    reconciliation must not fire (friend is singular, one member)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I met my friend John.", _dt(2026, 6, 21))
    # no collective junk; John is handled by the named-instance/social chains, not the collective pass.
    assert not [f for f in facts if f.rel_type == "member_of"], \
        f"collective pass wrongly fired on a single rename: {facts!r}"


@requires_model
def test_propn_age_survives_noisy_gliner_type():
    """FIX A (live-only bug): the copula-measure additive guard must NOT let a NOISY GLiNER2 type veto a
    PROPER-NOUN subject's bare-cardinal age. We force "Bianca" to the (wrong) ANIMAL type on the Doc
    — exactly what live GLiNER2 does for a one-name micro-sentence — and assert the age still lands."""
    m = _reload(LINGUISTIC_LAYER="true")
    doc = m._parse("Bianca is 10.")
    assert doc is not None
    span = doc.char_span(0, len("Bianca"), label="ANIMAL", alignment_mode="contract")
    assert span is not None
    doc.set_ents([span])
    # sanity: the subject really carries the noisy non-Person type the guard would otherwise veto on.
    assert any(e.label_ == "ANIMAL" for e in doc.ents)
    facts = m.derive_sentence_facts(doc, _dt(2026, 6, 21))
    assert any(f.subject == "bianca" and f.rel_type == "age" and f.object == "10"
               for f in facts), f"PROPN age vetoed by noisy NER type: {facts!r}"
    # NOTE: the complementary "COMMON-noun subject with an incompatible type STILL steps aside" case
    # depends on the rel_types ``head_types`` metadata resolved from the DB; offline that metadata is
    # unresolved so ``age`` admits any subject. It is validated on the LIVE stack. The PROPN override
    # above is the load-bearing fix and short-circuits BEFORE the admit check, so it is testable here.


# ──────────────────────────────────────────────────────────────────────────────
# split_enumeration — the DETERMINISTIC colon-enumeration pre-split (atomizer net)
#
# The LLM atomizer's split of a colon list varies run-to-run (folds "a designer named Priya" into
# the membership line → downstream drops Priya's member_of; drops a per-member attribute; or leaves
# the dense sentence un-split on a REFRAME timeout). split_enumeration normalizes the structurally
# obvious colon list deterministically so the spine sees the SAME atom set every time. These tests
# are pure-function (spaCy only, no LLM/DB) and assert determinism + the fabrication guard.
# ──────────────────────────────────────────────────────────────────────────────

@requires_model
def test_split_enumeration_relative_and_naming_mixed():
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")
    out = m.split_enumeration(
        "My team has three engineers: Sarah who is 35, Tom who is a backend dev, "
        "and a designer named Priya.")
    assert out == [
        "My team has three engineers: Sarah, Tom, and Priya.",
        "Sarah is 35.",
        "Tom is a backend dev.",
        "Priya is a designer.",
    ]


@requires_model
def test_split_enumeration_all_naming_items():
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")
    out = m.split_enumeration(
        "We run three servers: a web server named Apollo, a database named Vault, "
        "and a cache named Echo.")
    assert out == [
        "We run three servers: Apollo, Vault, and Echo.",
        "Apollo is a web server.",
        "Vault is a database.",
        "Echo is a cache.",
    ]


@requires_model
def test_split_enumeration_relative_clauses():
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")
    out = m.split_enumeration(
        "We have three kids: Bianca who is 10, Dez who is 12, and Cato who is 19.")
    assert out == [
        "We have three kids: Bianca, Dez, and Cato.",
        "Bianca is 10.",
        "Dez is 12.",
        "Cato is 19.",
    ]


@requires_model
def test_split_enumeration_is_deterministic_across_repeats():
    # determinism is the deliverable — the SAME input must give the SAME atom set every call.
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")
    txt = ("My team has three engineers: Sarah who is 35, Tom who is a backend dev, "
           "and a designer named Priya.")
    first = m.split_enumeration(txt)
    for _ in range(8):
        assert m.split_enumeration(txt) == first


@requires_model
def test_split_enumeration_affect_list_not_an_enumeration():
    # a coordinated ADJECTIVE list (no colon, no named entities) is NOT an entity enumeration → None,
    # so the affect seam keeps it. The pre-split must never hijack the multi-feeling construction.
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")
    assert m.split_enumeration(
        "Last week I was extremely busy, overwhelmed, underappreciated, and exhausted.") is None


@requires_model
def test_split_enumeration_no_colon_returns_none():
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")
    assert m.split_enumeration("My wife is Dana.") is None


@requires_model
def test_split_enumeration_item_without_proper_name_bails():
    # bare COMMON-noun items ("a Tesla and a truck") have no proper NAME for "truck" → bail (None),
    # the un-split sentence falls through to the LLM/spine (never a lossy guess). Fail-safe.
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")
    assert m.split_enumeration("I have two cars: a Tesla and a truck.") is None


@requires_model
def test_split_enumeration_fabrication_guard_rejects_invented_token(monkeypatch):
    # if a transform ever introduced a token absent from the source, the guard aborts the whole
    # split (USER IS TRUTH). Simulate by forcing a parse that would inject a foreign name; here we
    # assert the guard path by checking a clean split has every token grounded in the source.
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")
    src = "We have three kids: Bianca who is 10, Dez who is 12, and Cato who is 19."
    out = m.split_enumeration(src)
    src_tokens = set(__import__("re").findall(r"[a-z0-9]+", src.lower())) | {"is"}
    for atom in out:
        atom_tokens = set(__import__("re").findall(r"[a-z0-9]+", atom.lower()))
        assert atom_tokens <= src_tokens, f"atom introduced a foreign token: {atom!r}"


@requires_model
def test_split_enumeration_flag_off_returns_none():
    m = _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="false")
    assert m.split_enumeration(
        "We have three kids: Bianca who is 10, Dez who is 12, and Cato who is 19.") is None
    _reload(LINGUISTIC_LAYER="true", ENUM_PRESPLIT="true")  # restore for other tests


# ──────────────── DECLARATIVE-ASSERTION COUNTER (intent-misroute pre-route) ────────────────
@requires_model
def test_count_declarative_assertions_routes_multi_fact_statement():
    # A first-person declarative opening with a courtesy "tell you about" clause but ASSERTING
    # facts must score >= 2 so the caller routes it to STATEMENT (it was mis-scored QUERY → dropped).
    m = _reload(LINGUISTIC_LAYER="true")
    txt = ("I wanted to tell you about my family. My wife's name is Dana. "
           "We have three kids: Bianca, Dez, and Cato.")
    assert m.count_declarative_assertions(txt) >= 2


@requires_model
def test_count_declarative_assertions_recall_imperative_is_zero():
    # "tell me about my family" is an IMPERATIVE recall (no overt subject) → 0, so the pre-route
    # never hijacks a genuine recall.
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.count_declarative_assertions("tell me about my family") == 0


@requires_model
def test_count_declarative_assertions_retrieval_desire_is_zero():
    # explicit subject but the predicate is a desire verb (xcomp/prep), not a copula-attr/dobj → 0.
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.count_declarative_assertions("I want to know about my family") == 0


@requires_model
def test_count_declarative_assertions_questions_are_zero():
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.count_declarative_assertions("how have I been feeling") == 0
    assert m.count_declarative_assertions("where do I live") == 0


# ─────────────────── GAP-2 — comma-role collective membership (member_of) ────────────────────
# "<group> has a <role> named <Name>, a <role> named <Name>, and a <role> named <Name>" has NO
# colon "members:" head, so split_enumeration leaves it whole. _reconcile_named_role_collective
# distributes (member, member_of, <group>) for a non-self COMMON-NOUN group governing a >=2
# named-role roster. Subject-agnostic, grammar/dep + naming-verb cue driven, ZERO role/group/domain
# literals. (The live harvest atomizes first, so it runs this via the named_role_only recovery pass;
# the whole-sentence path is exercised here directly.)

@requires_model
def test_comma_role_collective_emits_member_of():
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "My band has a guitarist named Jax, a drummer named Reef, and a singer named Coda.", None)
    members = {(f.subject, f.object) for f in facts if f.rel_type == "member_of"}
    assert ("jax", "band") in members, facts
    assert ("reef", "band") in members, facts
    assert ("coda", "band") in members, facts


@requires_model
def test_comma_role_collective_subject_agnostic():
    # A different, never-seeded domain + group/role words → same membership shape (no literals).
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "My pottery guild has a potter named Wynn, a glazer named Sol, and a kiln master named Bex.",
        None)
    members = {(f.subject, f.object) for f in facts if f.rel_type == "member_of"}
    assert ("wynn", "guild") in members, facts
    assert ("sol", "guild") in members, facts
    assert ("bex", "guild") in members, facts


@requires_model
def test_comma_role_named_role_only_recovery_pass():
    # The harvest's raw-turn recovery mode runs ONLY this reconcile (no chains) — it must still emit
    # the membership edges and NOTHING else (no SVO/owns/instance_of smear).
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "My band has a guitarist named Jax, a drummer named Reef, and a singer named Coda.",
        None, named_role_only=True)
    rels = {f.rel_type for f in facts}
    assert rels == {"member_of"}, facts
    assert len([f for f in facts if f.rel_type == "member_of"]) == 3, facts


@requires_model
def test_comma_role_self_subject_emits_no_member_of():
    # SELF subject ("I have a son named Alex …") is kinship/possession, NOT a collective that HAS
    # members → no member_of (the kinship/has_pet forms must not regress).
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I have a son named Alex and a daughter named Robin.", None)
    assert not [f for f in facts if f.rel_type == "member_of"], facts


@requires_model
def test_single_named_instance_emits_no_member_of():
    # A single "<noun> has a <type> named <Name>" is not a >=2 roster → no member_of (bounds the
    # possession/pet false-positive: "My friend has a dog named Rex").
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("My friend has a dog named Rex.", None)
    assert not [f for f in facts if f.rel_type == "member_of"], facts


# ─────────────────── CARVE-OUT: domain cue classes grow per-tenant (lean-seed) ───────────────────
# The DOMAIN-flavored cue vocabulary stays CARVED out of the seed — thin_type is fully empty, and
# social_role carries ONLY the universal social primitive (friend) while its DOMAIN roles (colleague/
# boss/roommate/…) grow per-tenant; a cold tenant degrades + queues a freq-gated growth candidate.
# problem_noun was ALSO carved empty originally, but that broke the LVC device-issue gate on a
# fresh/oracle tenant (the gate is keyed on problem-noun MEMBERSHIP, so an empty class → never fires →
# "I had an issue with my GPS" FRAGMENTS). It now carries a small CLOSED abnormal-state-noun FLOOR
# (the never-empty fail-safe every other cue class has), still extended per-tenant by growth. social_role
# got the SAME treatment for the SAME reason: an empty class dropped the social-role copula tie ("my
# friend is Sam" → owns(user, sam), a PERSON owned) — so the ONE universal tie friend→friend_of is a
# CLOSED floor (parity with kinship being a seeded universal class), domain roles still grown.

def test_carved_class_bootstraps_are_empty():
    """thin_type keeps an EMPTY in-code fallback (no enumerated domain vocabulary); social_role and
    problem_noun carry a small CLOSED floor (the universal friend tie / the abnormal-state heads),
    both still extended per-tenant by growth — never the domain-flavored word zoo."""
    from src.api import linguistic_cue_overlay as lco
    assert lco._BOOTSTRAP_THIN_TYPE_MAP == {}, "thin_type must not be enumerated in code"
    # social_role floor = ONLY the universal social primitive (friend); domain roles stay grown.
    assert lco._BOOTSTRAP_SOCIAL_ROLE_MAP == {"friend": "friend_of"}, \
        "social_role floor is the universal friend tie only (no domain-role word zoo)"
    assert "colleague" not in lco._BOOTSTRAP_SOCIAL_ROLE_MAP, "domain roles must NOT be seeded"
    # problem_noun now has a non-empty CLOSED floor (the device-issue gate fail-safe).
    assert "issue" in lco._BOOTSTRAP_PROBLEM_NOUNS, "problem_noun floor must include the generic heads"
    assert "problem" in lco._BOOTSTRAP_PROBLEM_NOUNS
    # KEPT classes (kinship + grammar/unit) stay seeded — sanity-check a couple are still populated.
    assert lco._BOOTSTRAP_KINSHIP_REL_MAP, "kinship_noun (KEPT) must remain seeded"
    assert lco._BOOTSTRAP_NAMING_VERBS, "naming_verb (KEPT grammar primitive) must remain seeded"
    assert lco._BOOTSTRAP_UNIT_SCALAR_MAP, "unit_scalar (KEPT unit primitive) must remain seeded"


def test_carved_consumers_resolve_empty_when_unbound(monkeypatch):
    """With no tenant bound (no DSN): thin_type resolves to the EMPTY carved fallback; social_role and
    problem_noun resolve to their non-empty closed floors (never-empty fail-safe)."""
    # No database: this pins the in-code cue FLOOR (the seeded-table divergence is #75;
    # it used to run DSN-less only because another module popped the DSN at import (#63).
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    m = _reload(LINGUISTIC_LAYER="true")
    assert "issue" in m._problem_nouns(), "problem_noun resolves to its floor when unbound"
    assert m._social_role_map() == {"friend": "friend_of"}, \
        "social_role resolves to its universal-friend floor when unbound (domain roles still grow)"
    assert m._thin_type_map() == {}


def test_cue_candidate_accumulator_record_drain_dedup():
    """The request-scoped growth accumulator records, de-dups, and drains (clearing on drain)."""
    from src.api import linguistic_cue_overlay as lco
    lco.drain_cue_candidates()  # clear any prior state
    lco.record_cue_candidate("colleague", "social_role")
    lco.record_cue_candidate("colleague", "social_role")  # dup → collapsed
    lco.record_cue_candidate("issue", "problem_noun")
    lco.record_cue_candidate("", "social_role")           # empty → ignored
    drained = lco.drain_cue_candidates()
    assert ("colleague", "social_role") in drained
    assert ("issue", "problem_noun") in drained
    assert len(drained) == 2, drained
    # drained → cleared
    assert lco.drain_cue_candidates() == []


@requires_model
def test_problem_noun_device_issue_fires_on_possessed_thing():
    """The LVC device-issue gate: 'had an <problem-noun> with <my THING>' → problem_head (dobj is a
    problem noun via the cold-tenant floor) AND problem_affected_thing (non-social + 1st-person
    possessed). This is the GPS case generalized to a router."""
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    evs = m.analyze_events("I had an issue with my router.")
    assert evs, "expected an event analysis for the LVC construction"
    ev = evs[0]
    assert ev.problem_head is True, "issue ∈ problem_noun floor"
    assert ev.problem_affected_thing is True, "my router = non-social, 1st-person possessed"
    assert ev.concerns and "router" in ev.concerns, ev


@requires_model
def test_problem_noun_nested_possessive_thing_fires():
    """A NESTED 1st-person possessive ('my car's GPS system' — 'my' possesses 'car' possesses
    'system') still counts as speaker-possessed → device-issue fires (the literal Q1 case)."""
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    evs = m.analyze_events("I had an issue with my car's GPS system.")
    assert evs, "expected an event analysis"
    ev = evs[0]
    assert ev.problem_head is True
    assert ev.problem_affected_thing is True, ev


@requires_model
def test_problem_noun_person_pobj_does_not_fire():
    """'had a problem with my coworker Sam' is INTERPERSONAL (PERSON pobj) → must NOT bind a device
    has_state (problem_affected_thing False even though problem_head is True)."""
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    evs = m.analyze_events("I had a problem with my coworker Sam.")
    assert evs, "expected an event analysis"
    ev = evs[0]
    assert ev.problem_affected_thing is False, "PERSON pobj is social → no device bind"


@requires_model
def test_problem_noun_abstract_pobj_does_not_fire():
    """'had a problem with the traffic' is ABSTRACT (not 1st-person possessed) → no device has_state
    is forced (problem_affected_thing False)."""
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    evs = m.analyze_events("I had a problem with the traffic.")
    assert evs, "expected an event analysis"
    ev = evs[0]
    assert ev.problem_affected_thing is False, "abstract/unpossessed pobj → no device bind"


@requires_model
def test_problem_noun_non_problem_head_does_not_fire():
    """A neutral non-problem LVC ('had a meeting with Sarah') is NOT a device-issue: meeting ∉
    problem_noun and Sarah is a PERSON → both problem_head and problem_affected_thing False."""
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    evs = m.analyze_events("I had a meeting with Sarah.")
    assert evs, "expected an event analysis for the LVC construction"
    ev = evs[0]
    assert ev.problem_head is False, "meeting ∉ problem_noun"
    assert ev.problem_affected_thing is False
    assert ev.problem_candidate is None, ev


# ─────────────── CORRECTION-PATH SUPPORT: 3rd-person pronoun + contrastive negation ───────────────
# These back the correction-execution fix (deterministic pronoun coref + cue routing). The pronoun
# detector grounds a correction subject like "his"/"its" to its antecedent; the contrastive-negation
# generalization lets "X, not Y" (incl. an adjective complement: "red, not blue") route to CORRECTION
# while plain predicate negation ("I am not worried") stays a STATEMENT.

@requires_model
def test_is_third_person_pronoun_grammatical():
    m = _reload(LINGUISTIC_LAYER="true")
    # 3rd-person personal/possessive pronouns → True (Person=3 ∧ PronType=Prs)
    for w in ("his", "its", "their", "her", "him", "them", "he", "she", "they"):
        assert m.is_third_person_pronoun(w) is True, w
    # NOT 3rd-person pronouns → False (1st/2nd person, possessive determiners, common/proper nouns)
    for w in ("my", "our", "your", "I", "we", "name", "car", "rex", ""):
        assert m.is_third_person_pronoun(w) is False, w
    # Multi-token input is not a bare pronoun → False (fail-safe)
    assert m.is_third_person_pronoun("his name") is False


@requires_model
def test_contrastive_negation_nominal_and_complement():
    m = _reload(LINGUISTIC_LAYER="true")
    # Nominal contrast ("…, not <noun/propn/num>") — already supported.
    assert m.analyze_directive("she is 14, not 12").has_contrastive_negation is True
    assert m.analyze_directive("my car is a Toyota, not a Honda").has_contrastive_negation is True
    # Complement contrast: a negated complement opposite an ASSERTED sibling complement.
    assert m.analyze_directive("no, my favorite color is red, not blue").has_contrastive_negation is True
    assert m.analyze_directive("it is red not blue").has_contrastive_negation is True


@requires_model
def test_predicate_negation_is_not_contrastive():
    """Plain predicate negation must NOT read as a contrast — a single negated complement with no
    asserted sibling stays a STATEMENT (no destructive correction route)."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m.analyze_directive("I am not worried").has_contrastive_negation is False
    assert m.analyze_directive("this is not a problem").has_contrastive_negation is False
    assert m.analyze_directive("I am a teacher").has_contrastive_negation is False


# ── SCALAR VALUE-SPAN CAPTURE — the full grounded value, no leading-modifier truncation ──────────
# THE HARD LINE: when the object IS the memory (a value string), capture the FULL contiguous value,
# not just the head phrase. A NAMED multi-token value keeps its leading number/determiner-stripped
# form; a bare COUNT is NOT absorbed (relational path unchanged). Subject-agnostic, grammar-driven.

@requires_model
def test_scalar_value_keeps_leading_number_for_named_value():
    """"I live at 156 Cedar St. S" → the locative value keeps the house number "156" (a NAMED
    multi-token value), not the truncated "cedar st. s"."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I live at 156 Cedar St. S, Kitchener, Ontario.", _dt(2026, 7, 1))
    res = [f for f in facts if f.subject == "user" and f.rel_type == "live_at"]
    assert res, f"no live_at fact: {facts!r}"
    assert res[0].object == "156 cedar st. s", res[0].object


@requires_model
def test_scalar_value_count_is_not_absorbed_relational():
    """"I have 3 cats" → the count "3" is NEVER folded into a relational object ("3 cats").

    UPDATED for ``_chain_count_scalar``: the bare possessed count now OWNS the construction — it is a
    SCALAR ``(user, cats, "3")`` and the SVO relational twin ``(user, have, cats)`` is deliberately
    suppressed (so recall no longer emits the contradictory "you have a pet that is Cats"). The numeral
    is a proper scalar VALUE, never glued into a relational object. Assert: the count scalar exists, and
    no "3 cats" glued object anywhere."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I have 3 cats.", _dt(2026, 7, 1))
    objs = [f.object for f in facts if f.subject == "user"]
    assert any(f.subject == "user" and f.rel_type == "cats" and f.object == "3"
               and f.scalar_datatype is not None for f in facts), facts
    assert "3 cats" not in objs, objs


@requires_model
def test_scalar_attr_value_strips_leading_determiner():
    """"My car is a Tesla Model 3" → value "tesla model 3" (full model kept, leading article "a"
    dropped — a function word is never part of the value)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("My car is a Tesla Model 3.", _dt(2026, 7, 1))
    scalars = [f for f in facts if f.subject == "user" and f.scalar_datatype == "string"]
    assert scalars, f"no scalar attr fact: {facts!r}"
    assert scalars[0].object == "tesla model 3", scalars[0].object


@requires_model
def test_scalar_quoted_value_captured_verbatim():
    """"My favorite quote is 'the only constant is change'" → the whole quoted span is a verbatim
    scalar value (previously dropped entirely because spaCy parses the quote as an inner clause)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "My favorite quote is 'the only constant is change'.", _dt(2026, 7, 1))
    scalars = [f for f in facts if f.subject == "user" and f.scalar_datatype == "string"]
    assert any(f.object == "the only constant is change" for f in scalars), \
        f"quoted value not captured verbatim: {facts!r}"


# ─────────────────────── employment construction (_chain_employment) ─────────────────────────
# "<subject> <employment verb> as <role> [at|for <org>]" → occupation(subject, role) +
# works_for(subject, org). Deterministic, grammar + a DB-grown `employment_verb` cue class (the
# safety gate). Subject-agnostic (1st-person → user; a named 3rd-person subject → that name),
# type-agnostic objects. The employment VERB decides the relation, so a university/school org is
# NEVER flipped to educated_at. Guardrails: study/graduate are NOT employment verbs (→ educated_at
# untouched); a non-employment "as" verb ("dressed as a pirate") is NOT an occupation.


@requires_model
def test_employment_work_as_role_at_org_full_title():
    """The headline case: "I work as a Systems Analyst III at the University of Springfield's
    Computing Services" → occupation(user, "systems analyst iii") (FULL multi-level-compound title,
    not "analyst iii") + works_for(user, <org>). NO educated_at, NO genitive related_to junk."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I work as a Systems Analyst III at the University of Springfield's Computing Services.",
        _dt(2026, 7, 1))
    occ = [f for f in facts if f.subject == "user" and f.rel_type == "occupation"]
    assert occ and occ[0].object == "systems analyst iii", f"occupation wrong: {facts!r}"
    assert any(f.subject == "user" and f.rel_type == "works_for" for f in facts), \
        f"no works_for: {facts!r}"
    # the employment verb owns the construction — no education mislabel, no genitive junk
    assert not any(f.rel_type == "educated_at" for f in facts), f"educated_at leaked: {facts!r}"
    assert not any(f.rel_type == "related_to" for f in facts), f"related_to junk leaked: {facts!r}"
    assert not any(f.rel_type == m._STATE_REL and f.object == "work" for f in facts), \
        f"has_state work leaked: {facts!r}"


@requires_model
def test_employment_third_person_named_subject():
    """Subject-agnostic: "Sarah works as a nurse at the clinic" → occupation(sarah, nurse) +
    works_for(sarah, clinic). Not first-person-only; the named 3rd-person subject is captured."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("Sarah works as a nurse at the clinic.", _dt(2026, 7, 1))
    assert any(f.subject == "sarah" and f.rel_type == "occupation" and f.object == "nurse"
               for f in facts), f"occupation(sarah, nurse) missing: {facts!r}"
    assert any(f.subject == "sarah" and f.rel_type == "works_for" and f.object == "clinic"
               for f in facts), f"works_for(sarah, clinic) missing: {facts!r}"


@requires_model
def test_employment_synonym_verb_via_cue_class():
    """Broad via the cue class, not the literal "work": "I am employed as an engineer at Globex" →
    occupation(user, engineer) + works_for(user, globex) ("employ" is in the employment cue class)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I am employed as an engineer at Globex.", _dt(2026, 7, 1))
    assert any(f.rel_type == "occupation" and f.object == "engineer" for f in facts), \
        f"occupation(engineer) missing: {facts!r}"
    assert any(f.rel_type == "works_for" and f.object == "globex" for f in facts), \
        f"works_for(globex) missing: {facts!r}"


@requires_model
def test_employment_work_at_org_still_works_for():
    """No regression: "I work at Acme Corp" / "I work for Acme Corp." → works_for (no "as"
    role, org affiliation only). Both parses currently carry the sentence-final period into the
    org NP surface ("acme corp.") — the "at" expectation is corrected to that ACTUAL behavior
    (was a stale, self-inconsistent "acme corp"; the "for" case already asserted the period).
    Stripping trailing sentence punctuation from an org name is a separate cosmetic cleanup."""
    m = _reload(LINGUISTIC_LAYER="true")
    for sent, org in (("I work at Acme Corp.", "acme corp."),
                      ("I work for Acme Corp.", "acme corp.")):
        facts = m.derive_sentence_facts(sent, _dt(2026, 7, 1))
        assert any(f.subject == "user" and f.rel_type == "works_for" and f.object == org
                   for f in facts), f"works_for({org}) missing for {sent!r}: {facts!r}"


@requires_model
def test_employment_cue_gate_rejects_non_employment_as_verb():
    """The verb cue class is the safety gate: "I dressed as a pirate" — "dress" is NOT an employment
    verb → NEVER an occupation (no occupation edge minted)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I dressed as a pirate.", _dt(2026, 7, 1))
    assert not any(f.rel_type == "occupation" for f in facts), \
        f"'dressed as a pirate' wrongly minted an occupation: {facts!r}"


@requires_model
def test_employment_does_not_touch_education_verbs():
    """Guardrail: study/graduate are NOT employment verbs — the employment chain emits NOTHING for
    "I studied at the University of Springfield" (→ the education path is left untouched; no
    works_for/occupation from the employment chain)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I studied at the University of Springfield.", _dt(2026, 7, 1))
    assert not any(f.rel_type in ("works_for", "occupation") for f in facts), \
        f"education verb wrongly captured as employment: {facts!r}"


@requires_model
def test_employment_work_with_is_collaboration_not_employment():
    """"I work with Rachel" is collaboration, not employment — the employment frame needs an
    as/at/for PP, so "with" leaves the clause to the SVO chain (work_with), never works_for."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I work with Rachel.", _dt(2026, 7, 1))
    assert not any(f.rel_type == "works_for" for f in facts), \
        f"'work with' wrongly became works_for: {facts!r}"


# ─── possessed POSITION-NOUN construction ("my ROLE as <occupation> [at <org>]") ──────────────
# The noun-headed twin of _chain_employment: an occupation stated via a POSSESSED position noun
# ("role"/"position"/"job", cue class `position_noun`) whose apposed "as <NP>" carries the role,
# even when the sentence's MAIN verb is unrelated ("I've USED Trello in my role as …"). Reuses the
# employment occupation + works_for emission. Subject-agnostic (1st-person possessor → user);
# `position_noun` is the safety gate ("my HOUSE as collateral" is NOT an occupation).


@requires_model
def test_possessed_position_noun_as_role_captures_occupation():
    """The headline case: "I've used Trello in my previous role as a marketing specialist at a small
    startup" → occupation(user, "marketing specialist") + works_for(user, "small startup"), while the
    main clause verb still lands (user, use, trello). Previously NOTHING occupation-shaped was
    captured (no employment verb) → "What was my previous occupation?" recalled empty."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I have used Trello in my previous role as a marketing specialist at a small startup.",
        _dt(2026, 7, 1))
    assert any(f.subject == "user" and f.rel_type == "occupation"
               and f.object == "marketing specialist" for f in facts), \
        f"occupation(user, marketing specialist) missing: {facts!r}"
    assert any(f.subject == "user" and f.rel_type == "works_for"
               and "startup" in (f.object or "") for f in facts), \
        f"works_for(user, small startup) missing: {facts!r}"
    # the possessed role noun must NOT fall to a junk ownership edge
    assert not any(f.rel_type == "owns" and "role" in (f.object or "") for f in facts), \
        f"junk owns(role) leaked: {facts!r}"


@requires_model
def test_possessed_position_noun_third_person_named_possessor():
    """Subject-agnostic: a NAMED genitive possessor binds the occupation to that person, not the
    speaker — "Sarah's role as a nurse at the clinic is demanding" → occupation(sarah, "nurse")."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "Sarah's role as a nurse at the clinic is demanding.", _dt(2026, 7, 1))
    assert any(f.subject == "sarah" and f.rel_type == "occupation" and f.object == "nurse"
               for f in facts), f"occupation(sarah, nurse) missing: {facts!r}"


@requires_model
def test_possessed_position_noun_parse_robust_adjacency():
    """PARSE-ROBUSTNESS: the sm parser mis-attaches the apposed "as" to the matrix verb — "…
    automating workflows in my new role as a senior marketing analyst" — but "as" is linearly
    adjacent to the role noun, so the occupation is still recovered."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "I am thinking of automating workflows in my new role as a senior marketing analyst.",
        _dt(2026, 7, 1))
    assert any(f.subject == "user" and f.rel_type == "occupation"
               and f.object == "senior marketing analyst" for f in facts), \
        f"occupation(user, senior marketing analyst) missing: {facts!r}"


@requires_model
def test_possessed_position_noun_gate_rejects_non_position_noun():
    """The position_noun cue class is the safety gate: "I used my house as collateral for the loan"
    — "house" is NOT a position noun → the "as <NP>" apposition NEVER mints an occupation."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I used my house as collateral for the loan.", _dt(2026, 7, 1))
    assert not any(f.rel_type == "occupation" for f in facts), \
        f"'my house as collateral' wrongly minted an occupation: {facts!r}"


# ─── CONTENT-VERB-BEATS-MINT convergence carve-out (fuzzy GLiNER2 rel must not clobber a real verb) ─
# The GLiNER2 relation scorer picks the best-fitting rel from the SEEDED menu even when the sentence's
# verb expresses a relation NOT in that menu — so "CVE affects Exchange" mints a fuzzy ``manages`` that
# would OVERRIDE the user's own content verb ``affect``. The convergence carve-out keeps a genuine,
# non-light content verb whose lemma is a DIFFERENT relation from the mint; a light-verb mint
# ("have"→has_pet) and a same-verb canonicalization ("live"→lives_in) still let the mint win.
# Subject-agnostic, grammar/identity-driven — asserted across pharma / legal / security domains.

def _derive_with_minted_pair(m, text, subj_prefix, obj_prefix, minted_rel, ref):
    """Inject a fuzzy GLiNER2-style minted rel on an arbitrary (subject, object) token pair (matched by
    surface prefix — subject need NOT be first-person), exactly as the live sentence-pipeline does via
    set_minted_rel, then run the deriver and return the rel set for the object."""
    doc = m._parse(text)
    subj_i = next((t.i for t in doc if (t.text or "").lower().startswith(subj_prefix)), None)
    obj_i = next((t.i for t in doc if (t.text or "").lower().startswith(obj_prefix)), None)
    if subj_i is not None and obj_i is not None:
        m.set_minted_rel(doc, subj_i, obj_i, minted_rel)
    facts = m.derive_sentence_facts(doc, ref)
    return sorted({f.rel_type for f in facts if f.object and obj_prefix in f.object})


@requires_model
@pytest.mark.parametrize("sent,subj_prefix,obj_prefix,minted,keep", [
    # security: the reported bug — "affects" must not be laundered into "manages".
    ("Aspirin inhibits COX-1",            "aspirin", "cox",    "manages", "inhibit"),
    # pharma / legal: a genuine content verb keeps its own relation over a fuzzy seeded pick.
    ("The court overruled the precedent", "court",   "preced", "manages", "overrule"),
])
def test_content_verb_beats_fuzzy_mint_subject_agnostic(monkeypatch, sent, subj_prefix, obj_prefix,
                                                        minted, keep):
    """A fuzzy minted rel (a forced seeded pick for a verb ABSENT from the ontology) is DROPPED — the
    user's deterministic content verb wins — across domains. The ontology-novelty gate is stubbed True
    (simulating a loaded ontology that lacks the verb); the LIVE rig validates the real resolver marks
    ``affect``/``inhibit``/``overrule`` novel. NEVER stores the fuzzy ``manages``."""
    m = _reload(LINGUISTIC_LAYER="true")
    monkeypatch.setattr(m, "_predicate_is_novel_to_ontology", lambda _p: True)
    rels = _derive_with_minted_pair(m, sent, subj_prefix, obj_prefix, minted, _dt(2026, 2, 20))
    assert minted not in rels, f"{sent!r}: fuzzy mint {minted!r} clobbered the content verb: {rels!r}"
    assert any(keep in r for r in rels), f"{sent!r}: the content verb {keep!r} was lost: {rels!r}"


@requires_model
def test_light_verb_mint_still_wins_has_pet_no_regression(monkeypatch):
    """NO REGRESSION: "I have a dog" — "have" is a LIGHT verb with no relational content, so the seeded
    ``has_pet`` mint MUST still win even if the ontology-novelty gate would say novel. The light-verb
    rule (b) short-circuits BEFORE the novelty gate, proving light verbs are never protected."""
    m = _reload(LINGUISTIC_LAYER="true")
    monkeypatch.setattr(m, "_predicate_is_novel_to_ontology", lambda _p: True)  # gate can't override (b)
    rels = _derive_with_minted(m, "I have a dog", "dog", "has_pet", _dt(2026, 2, 20))
    assert "has_pet" in rels, f"light-verb mint has_pet was wrongly suppressed: {rels!r}"


@requires_model
def test_known_predicate_mint_still_wins_lives_in_no_regression(monkeypatch):
    """NO REGRESSION: "I live in Kitchener" — the SVO predicate ``live_in`` is a KNOWN rel (it folds to
    the seeded ``lives_in``), so the ontology-novelty gate returns False and the mint WINS. Stubbing the
    gate False (known) proves the known-predicate branch keeps the mint."""
    m = _reload(LINGUISTIC_LAYER="true")
    monkeypatch.setattr(m, "_predicate_is_novel_to_ontology", lambda _p: False)  # known predicate
    rels = _derive_with_minted(m, "I live in Kitchener", "kitch", "lives_in", _dt(2026, 2, 20))
    assert "lives_in" in rels, f"known-predicate mint (lives_in) was wrongly suppressed: {rels!r}"


@requires_model
def test_content_verb_beats_mint_helper_unit(monkeypatch):
    """Unit: _content_verb_beats_mint fires ONLY for a non-light content verb whose SVO predicate is
    NOVEL to the ontology; light verbs and known predicates return False (mint wins)."""
    m = _reload(LINGUISTIC_LAYER="true")
    doc = m._parse("Aspirin inhibits COX-1")
    vtok = next(t for t in doc if t.lemma_.lower() == "inhibit")
    monkeypatch.setattr(m, "_predicate_is_novel_to_ontology", lambda _p: True)   # novel verb
    assert m._content_verb_beats_mint(vtok, "inhibit", "manages") is True         # → content verb wins
    monkeypatch.setattr(m, "_predicate_is_novel_to_ontology", lambda _p: False)  # known predicate
    assert m._content_verb_beats_mint(vtok, "inhibit", "manages") is False        # → mint wins
    # light verb short-circuits BEFORE the (patched-True) novelty gate → mint always wins.
    monkeypatch.setattr(m, "_predicate_is_novel_to_ontology", lambda _p: True)
    doc2 = m._parse("I have a dog")
    have = next(t for t in doc2 if t.lemma_.lower() == "have")
    assert m._content_verb_beats_mint(have, "have", "has_pet") is False
    # a rel that is NOT this verb's own SVO predicate (deliberate canonical rel) is never touched.
    assert m._content_verb_beats_mint(vtok, "has_state", "manages") is False


def test_predicate_novelty_fail_safe_when_ontology_unloaded(monkeypatch):
    """FAIL-SAFE: with no loaded ontology (empty rel_types — DB down / unbound), novelty CANNOT be
    confirmed, so _predicate_is_novel_to_ontology returns False → the mint keeps winning (today's
    behavior). Never treats an unavailable ontology as 'everything is novel'."""
    m = _reload(LINGUISTIC_LAYER="true")
    from src.ontology import canonical as _canon
    monkeypatch.setattr(_canon, "_load_reltypes", lambda *a, **k: frozenset())
    assert m._predicate_is_novel_to_ontology("affect") is False


# ─────────────── DENSE MULTI-PREDICATE DECOMPOSITION (subordinate/coordinated clauses) ──────────
# A dense sentence packs several predicates about ONE subject into participial / appositive /
# coordinated / scalar-PP adjuncts that spaCy leaves subject-LESS. The deriver must decompose EACH
# into its own fact (subject carried by coreference), route scalars/dates to the right lanes, and
# NOT over-split a simple sentence. Grammar-driven + subject-agnostic — proven across THREE domains.

@requires_model
def test_dense_secops_decomposes_all_subordinate_predicates():
    """SECOPS dense CVE sentence → a fact for EACH subordinate predicate: passive participle
    (attributed_to), present participle (target), the copula type (instance_of) + containment
    (located_in), a scalar PP (cvss_score), and a dated participle state (patch @ event_date)."""
    m = _reload(LINGUISTIC_LAYER="true")
    sent = ("CVE-2026-35273 is an SSRF-to-RCE vulnerability in PeopleSoft, attributed to "
            "ShinyHunters, targeting higher education institutions, with a CVSS score of 9.8, "
            "patched on June 15 2026.")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 30))
    triples = {(f.subject, f.rel_type, f.object) for f in facts}
    assert ("cve-2026-35273", "instance_of", "vulnerability") in triples, triples
    assert ("cve-2026-35273", "located_in", "peoplesoft") in triples, triples
    assert ("cve-2026-35273", "attribute_to", "shinyhunters") in triples, triples
    assert ("cve-2026-35273", "target", "higher education institutions") in triples, triples
    # scalar PP → SCALAR attribute, never a relationship object
    cvss = [f for f in facts if f.subject == "cve-2026-35273" and f.rel_type == "cvss_score"]
    assert cvss and cvss[0].object == "9.8" and cvss[0].scalar_datatype == "string", facts
    # dated participle → objectless state carrying the patch date
    patch = [f for f in facts if f.subject == "cve-2026-35273" and f.object == "patch"]
    assert patch and patch[0].event_date and patch[0].event_date.startswith("2026-06-15"), facts


@requires_model
def test_dense_clinical_decomposes_subject_agnostic():
    """CLINICAL dense sentence (subject-agnostic proof) → attributive age scalar (62), the
    appositive role, the participle/coordinated predicates each a fact, the date stamped."""
    m = _reload(LINGUISTIC_LAYER="true")
    sent = ("The patient, a 62-year-old smoker, presented with chest pain, was diagnosed with "
            "pneumonia, and was prescribed amoxicillin on March 3.")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 30))
    triples = {(f.subject, f.rel_type, f.object) for f in facts}
    age = [f for f in facts if f.subject == "patient" and f.rel_type == "age"]
    assert age and age[0].object == "62" and age[0].scalar_datatype == "string", facts
    assert ("patient", "present_with", "chest pain") in triples, triples
    assert ("patient", "diagnose_with", "pneumonia") in triples, triples
    presc = [f for f in facts if f.subject == "patient" and f.object == "amoxicillin"]
    assert presc and presc[0].event_date and presc[0].event_date.startswith("2026-03-03"), facts


@requires_model
def test_dense_legal_decomposes_coordinated_and_passive_agent():
    """LEGAL dense sentence → coordinated/advcl predicates each a fact (overrule, establish), a
    passive by-agent predicate (cite_by), and the dated decision (event_date)."""
    m = _reload(LINGUISTIC_LAYER="true")
    sent = ("Smith v. Jones, decided in 2019, overruled Baker, established the negligence standard, "
            "and was cited by three later cases.")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 30))
    triples = {(f.subject, f.rel_type, f.object) for f in facts}
    assert ("smith", "overrule", "baker") in triples, triples
    assert ("smith", "establish", "negligence standard") in triples, triples
    assert ("smith", "cite_by", "later cases") in triples, triples
    decided = [f for f in facts if f.subject == "smith" and f.object == "decide"]
    assert decided and decided[0].event_date and decided[0].event_date.startswith("2019"), facts


@requires_model
@pytest.mark.parametrize("sent,expect_max", [
    ("I have a dog.", 1),          # a single possession — no extra sub-facts
    ("My mother is Diane.", 3),    # kin + name + gender only
    ("I feel worried.", 1),        # a single feeling
])
def test_dense_decomposition_does_not_over_split_simple_sentences(sent, expect_max):
    """A sentence with NO subordinate predicate yields exactly today's facts — the carried-subject /
    scalar-PP / attributive-measure additions must never manufacture extra edges on a simple clause."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 30))
    assert len(facts) <= expect_max, f"{sent!r} over-split into {facts!r}"


@requires_model
@pytest.mark.parametrize("sent", [
    "I have 3 cats.",              # a bare COUNT is not a scalar measurement
    "I bought a 3-pack of sodas.", # a non-unit '-pack' surface must not mint a scalar
])
def test_measure_chains_do_not_misfire_on_non_measurements(sent):
    """The scalar-measure PP and attributive-measure chains are UNIT-map gated: a plain count or a
    non-unit hyphenated token mints NO MEASUREMENT scalar (a numeral+unit value).

    UPDATED for ``_chain_count_scalar`` (knowledge-update recency-scalar cluster): a possessed bare
    count IS now captured as a COUNT scalar whose object is a BARE INTEGER ("3", rel="cats") — that is
    NOT a fabricated measurement (which would glue a unit onto the numeral, "3 cats"/"3 packs"). Forbid
    any scalar whose object is NOT a bare integer; a bare-integer count scalar is permitted."""
    import re as _re
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(sent, _dt(2026, 6, 30))
    measurement_scalars = [f for f in facts if f.scalar_datatype == "string"
                           and not _re.fullmatch(r"\d[\d,]*", (f.object or "").strip())]
    assert not measurement_scalars, f"{sent!r} fabricated a measurement scalar: {measurement_scalars!r}"


# ─── CROSS-SENTENCE DISCOURSE-TOPIC COREFERENCE (subject-agnostic, deterministic) ───────────────
# A dense multi-sentence description must CONSOLIDATE onto the salient primary entity established from
# sentence 1: a later sentence's subject pronoun / definite type-NP with no closer antecedent resolves
# back to the topic. The anti-over-eager guards (one unambiguous topic, no-closer-antecedent, type-
# compatibility) prevent a wrong bind. Validated across three domains (secops / clinical / legal) + the
# guardrail cases. spaCy tags an OOV identifier (a CVE id) inconsistently, so the topic anchor is keyed
# on the sentence-1 grammatical subject regardless of coarse POS (excluding only genuine pronouns).

def _turn(m, sents, ref):
    """Mimic the harvest loop: derive per sentence, establish the discourse topic from the FIRST
    sentence, thread it into the later sentences, and accumulate object NPs as prior-turn coref
    antecedents. Returns the flat list of (subject, rel_type, object, is_scalar) tuples per sentence."""
    topic = None
    prior: list[str] = []
    out = []
    for s in sents:
        facts = m.derive_sentence_facts(s, ref, prior_nps=list(prior), discourse_topic=topic)
        if topic is None:
            topic = m.discourse_topic_from_doc(s, facts)
        out.append([(f.subject, f.rel_type, f.object, bool(f.scalar_datatype)) for f in facts])
        for f in facts:
            if f.object and f.object not in ("it", "they", "them"):
                prior.append(f.object)
    return topic, out


@requires_model
def test_cross_sentence_topic_pronoun_and_has_scalar_secops():
    """SECOPS: 'It has a CVSS base score of 9.8' → the pronoun binds to the CVE topic AND the
    has-a-measure construction emits a SCALAR (not a relationship object). Subject-agnostic."""
    m = _reload(LINGUISTIC_LAYER="true")
    topic, out = _turn(m, [
        "CVE-2026-1234 is a request-forgery vulnerability in PeopleSoft.",
        "It has a CVSS base score of 9.8.",
    ], _dt(2026, 6, 30))
    assert topic is not None and topic.surface.startswith("cve-2026-1234")
    scalars = [t for sent in out for t in sent if t[3]]
    assert any(s[0].startswith("cve-2026-1234") and "9.8" in s[2] for s in scalars), out


@requires_model
def test_cross_sentence_definite_typenoun_and_by_agent_secops():
    """SECOPS: 'The vulnerability was patched by Oracle' → the definite type-NP ('vulnerability' is the
    CVE's own type noun) binds to the CVE, and the passive by-agent yields the agent as the object."""
    m = _reload(LINGUISTIC_LAYER="true")
    _topic, out = _turn(m, [
        "CVE-2026-1234 is a request-forgery vulnerability in PeopleSoft.",
        "The vulnerability was patched by Oracle.",
    ], _dt(2026, 6, 30))
    later = out[1]
    assert any(s.startswith("cve-2026-1234") and "oracle" in o for (s, _r, o, _sc) in later), later


@requires_model
def test_cross_sentence_topic_pronoun_binds_clinical():
    """CLINICAL: 'He presented with chest pain' → the animate pronoun binds to the PERSON patient."""
    m = _reload(LINGUISTIC_LAYER="true")
    _topic, out = _turn(m, [
        "The patient is a 62-year-old smoker.",
        "He presented with chest pain.",
    ], _dt(2026, 6, 30))
    later = out[1]
    assert later and all(s == "patient" for (s, _r, _o, _sc) in later), later


@requires_model
def test_cross_sentence_type_incompatible_definite_np_not_bound_clinical():
    """GUARDRAIL: 'The lesion was biopsied' must NOT bind to the PERSON patient — 'lesion' is neither
    the patient's type noun nor its coarse type. A wrong bind is worse than none, so it stays local."""
    m = _reload(LINGUISTIC_LAYER="true")
    _topic, out = _turn(m, [
        "The patient is a 62-year-old smoker.",
        "The lesion was biopsied on March 3.",
    ], _dt(2026, 6, 30))
    later = out[1]
    assert later and not any(s == "patient" for (s, _r, _o, _sc) in later), later


@requires_model
def test_cross_sentence_topic_pronoun_binds_legal():
    """LEGAL: 'It overruled Baker' → the pronoun binds to the case topic (the ruling ties to the case)."""
    m = _reload(LINGUISTIC_LAYER="true")
    topic, out = _turn(m, [
        "Smith v. Jones was decided in 2019.",
        "It overruled Baker.",
    ], _dt(2026, 6, 30))
    assert topic is not None
    later = out[1]
    assert any(s == topic.surface and "baker" in o for (s, _r, o, _sc) in later), (topic, later)


@requires_model
def test_cross_sentence_pronoun_local_antecedent_wins_over_topic():
    """GUARDRAIL: 'I have a dog. It is brown.' → 'it' (inanimate) ⇎ the PERSON topic ('user'), so it
    resolves to the LOCAL antecedent (the dog), never the user."""
    m = _reload(LINGUISTIC_LAYER="true")
    _topic, out = _turn(m, ["I have a dog.", "It is brown."], _dt(2026, 6, 30))
    later = out[1]
    assert any(s == "dog" for (s, _r, _o, _sc) in later), later
    assert not any(s == "user" for (s, _r, _o, _sc) in later), later


@requires_model
def test_cross_sentence_ambiguous_topic_not_bound():
    """GUARDRAIL: a sentence-1 with COORDINATED competing subjects → no single topic → a later 'it'
    is NOT bound to either (topic is None). No wrong bind on ambiguity."""
    m = _reload(LINGUISTIC_LAYER="true")
    topic, out = _turn(m, [
        "The server crashed and the database failed.",
        "It was restarted.",
    ], _dt(2026, 6, 30))
    assert topic is None
    later = out[1]
    assert not any(s in ("server", "database") for (s, _r, _o, _sc) in later), later


# ── SHELL-NOUN ANAPHORA → discourse topic (gap-1) + SECONDARY-SENTENCE DATES (gap-2) ─────────────
# A later-sentence DEFINITE subject whose head is a GENERIC SHELL noun ("the flaw"/"the ruling"/"the
# condition") co-refers with the turn's topic even though GLiNER2 does NOT coarse-match it to the
# topic's exact type noun (flaw ≉ vulnerability). The shell-noun class is DB-grown (linguistic_cues
# category='shell_noun'); these tests exercise the in-code bootstrap floor (DB-down fail-safe). The
# same tests confirm the event_date is stamped on a SECONDARY / coref-rebound sentence's fact.

@requires_model
def test_shell_noun_binds_to_topic_secops():
    """SECOPS: 'The flaw has been exploited by ShinyHunters' → 'flaw' (a generic shell noun, NOT the
    CVE's exact type noun 'vulnerability') binds to the CVE topic, so the attribution consolidates."""
    m = _reload(LINGUISTIC_LAYER="true")
    topic, out = _turn(m, [
        "CVE-2026-1234 is a critical SQL injection vulnerability in Oracle E-Business Suite.",
        "The flaw has been actively exploited by ShinyHunters.",
    ], _dt(2026, 6, 30))
    assert topic is not None and topic.surface.startswith("cve-2026-1234")
    later = out[1]
    assert later and all(s.startswith("cve-2026-1234") for (s, _r, _o, _sc) in later), later
    assert any("shinyhunters" in o for (_s, _r, o, _sc) in later), later


@requires_model
def test_shell_noun_binds_to_topic_clinical_with_secondary_date():
    """CLINICAL: 'The condition worsened on March 3' → 'condition' (shell noun) binds to the PERSON
    patient topic (shells are type-agnostic re-reference), AND the secondary-sentence date is stamped."""
    m = _reload(LINGUISTIC_LAYER="true")
    _topic, out = _turn(m, [
        "The patient is a 54-year-old male.",
        "The condition worsened on March 3.",
    ], _dt(2026, 6, 30))
    later = out[1]
    assert later and all(s == "patient" for (s, _r, _o, _sc) in later), later
    # secondary-sentence date binding (gap-2): re-derive to read the event_date off the fact.
    facts = m.derive_sentence_facts(
        "The condition worsened on March 3.", _dt(2026, 6, 30),
        discourse_topic=m.discourse_topic_from_doc(
            "The patient is a 54-year-old male.",
            m.derive_sentence_facts("The patient is a 54-year-old male.", _dt(2026, 6, 30))))
    assert any(f.subject == "patient" and (f.event_date or "").startswith("2026-03-03")
               for f in facts), [(f.subject, f.rel_type, f.event_date) for f in facts]


@requires_model
def test_shell_noun_binds_to_topic_legal():
    """LEGAL: 'The ruling overruled Baker' → 'ruling' (outcome shell) binds to the case topic."""
    m = _reload(LINGUISTIC_LAYER="true")
    topic, out = _turn(m, [
        "Smith v. Jones was decided in 2019.",
        "The ruling overruled Baker.",
    ], _dt(2026, 6, 30))
    assert topic is not None
    later = out[1]
    assert any(s == topic.surface and "baker" in o for (s, _r, o, _sc) in later), (topic, later)


@requires_model
def test_indefinite_shell_noun_not_bound():
    """GUARDRAIL: 'A flaw was found in the parser' (INDEFINITE) introduces a NEW entity — it must NOT
    bind to the topic. Only DEFINITE shell nouns are co-referents."""
    m = _reload(LINGUISTIC_LAYER="true")
    topic, out = _turn(m, [
        "CVE-2026-1234 is a critical vulnerability.",
        "A flaw was found in the parser.",
    ], _dt(2026, 6, 30))
    assert topic is not None and topic.surface.startswith("cve-2026-1234")
    later = out[1]
    assert not any(s.startswith("cve-2026-1234") for (s, _r, _o, _sc) in later), later
    assert any(s == "flaw" for (s, _r, _o, _sc) in later), later


@requires_model
def test_two_topic_passage_no_shell_bind():
    """GUARDRAIL: a coordinated (two-topic) sentence-1 yields NO single unambiguous topic
    (discourse_topic_from_doc → None), so a later 'the issue' shell noun is NOT bound to either
    coordinated member — the wrong-bind-on-ambiguity case. (The topic later settles on sentence 2's
    own subject, which is fine; what matters is neither 'server' nor 'database' is bound.)"""
    m = _reload(LINGUISTIC_LAYER="true")
    # sentence-1 alone yields no topic (coordinated competing subjects)
    s1 = "The server and the database were upgraded."
    assert m.discourse_topic_from_doc(s1, m.derive_sentence_facts(s1, _dt(2026, 6, 30))) is None
    _topic, out = _turn(m, [s1, "The issue was resolved."], _dt(2026, 6, 30))
    later = out[1]
    assert not any(s in ("server", "database") for (s, _r, _o, _sc) in later), later
    assert any(s == "issue" for (s, _r, _o, _sc) in later), later


@requires_model
def test_secondary_sentence_date_stamped_active_voice():
    """gap-2: 'Oracle released a patch on June 15, 2026' (a NON-first sentence) carries the event_date;
    a date is NEVER fabricated and NEVER a relationship object."""
    m = _reload(LINGUISTIC_LAYER="true")
    topic = m.discourse_topic_from_doc(
        "CVE-2026-1234 is a critical vulnerability.",
        m.derive_sentence_facts("CVE-2026-1234 is a critical vulnerability.", _dt(2026, 6, 30)))
    facts = m.derive_sentence_facts(
        "Oracle released a patch on June 15, 2026.", _dt(2026, 6, 30), discourse_topic=topic)
    assert any(f.subject == "oracle" and "patch" in (f.object or "")
               and (f.event_date or "").startswith("2026-06-15") for f in facts), \
        [(f.subject, f.rel_type, f.object, f.event_date) for f in facts]
    # the date is never a relationship object
    assert not any("june" in (f.object or "").lower() for f in facts), facts


@requires_model
def test_has_a_measure_scalar_standalone_and_digit_gate():
    """The has-a-measure construction emits a SCALAR only when the of-value is DIGIT-bearing:
    'has a temperature of 39' → scalar; 'has a dog' → no scalar (narrow, no word list)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("The patient has a temperature of 39.", _dt(2026, 6, 30))
    scalars = [f for f in facts if f.scalar_datatype == "string"]
    assert any("39" in f.object and "temperature" in f.rel_type for f in scalars), facts
    facts2 = m.derive_sentence_facts("The patient has a dog.", _dt(2026, 6, 30))
    assert not [f for f in facts2 if f.scalar_datatype == "string"], facts2


# ─────────────── quantity-of-substance SCALAR capture (num + unit + substance) ───────────────
# "<num> <unit> of <substance>" / "<substance> <num> <unit>" / "<subj> have <num> <unit> of <noun>"
# must NEVER bind the UNIT as a relationship object (the live-DB bug: take(user, milligrams) /
# owns(humans, pairs), dropping both the number and the substance). The number+unit is a SCALAR
# value; the substance is grounded (content mode) or the of-noun names the attribute (possession).
# Subject-agnostic (grammar + the DB unit_scalar map), DIGIT-gated (no numeral → never fires).

@requires_model
def test_quantity_content_verb_dose_scalar_on_substance():
    """"I take 500 milligrams of metformin" → the substance is the real object (take(user, metformin))
    and "500 milligrams" is a SCALAR on metformin — NEVER take(user, milligrams)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I take 500 milligrams of metformin twice daily.", _dt(2026, 6, 30))
    # the substance is grounded as the real relationship object
    assert any(f.subject == "user" and f.object == "metformin" and f.scalar_datatype is None
               for f in facts), facts
    # the dose is a SCALAR on the substance carrying the full "500 milligrams" value
    scal = [f for f in facts if f.subject == "metformin" and f.scalar_datatype == "string"]
    assert scal and any("500" in f.object and "milligram" in f.object for f in scal), facts
    # the UNIT is NEVER a relationship object, and the number is never dropped
    assert not any(f.object in ("milligrams", "milligram") and f.scalar_datatype is None
                   for f in facts), facts


@requires_model
def test_quantity_appositive_substance_num_unit_scalar():
    """"lisinopril 10 milligrams" → SCALAR on lisinopril, NEVER has_role(lisinopril, milligrams)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I take lisinopril 10 milligrams once a day.", _dt(2026, 6, 30))
    assert any(f.subject == "user" and f.object == "lisinopril" for f in facts), facts
    scal = [f for f in facts if f.subject == "lisinopril" and f.scalar_datatype == "string"]
    assert scal and any("10" in f.object and "milligram" in f.object for f in scal), facts
    assert not any(f.rel_type == "has_role" and "milligram" in f.object for f in facts), facts


@requires_model
def test_quantity_possession_of_noun_names_attribute():
    """"my NAS has 40 terabytes of storage" → storage="40 terabytes" SCALAR on the NAS;
    "humans have 23 pairs of chromosomes" → chromosomes="23 pairs" — NEVER owns(subj, unit)."""
    m = _reload(LINGUISTIC_LAYER="true")
    f1 = m.derive_sentence_facts("My NAS has 40 terabytes of storage.", _dt(2026, 6, 30))
    assert any(f.subject == "nas" and "storage" in f.rel_type and "40" in f.object
               and f.scalar_datatype == "string" for f in f1), f1
    assert not any(f.object in ("terabytes", "terabyte") and f.scalar_datatype is None
                   for f in f1), f1
    f2 = m.derive_sentence_facts("Humans have 23 pairs of chromosomes.", _dt(2026, 6, 30))
    assert any(f.subject == "humans" and "chromosome" in f.rel_type and "23" in f.object
               and f.scalar_datatype == "string" for f in f2), f2
    assert not any(f.object == "pairs" and f.scalar_datatype is None for f in f2), f2


@requires_model
def test_quantity_digit_gate_no_fire_without_numeral():
    """DIGIT-gate: "I take a tablet of metformin" (no numeral) must NOT emit a quantity scalar —
    the substance still grounds via the normal path, but no fabricated "<unit>" scalar appears."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I take a tablet of metformin.", _dt(2026, 6, 30))
    # no digit-bearing quantity scalar was fabricated
    assert not any(f.scalar_datatype == "string" and any(ch.isdigit() for ch in f.object)
                   for f in facts), facts


@requires_model
def test_quantity_no_regression_heart_rate_and_cvss_scalars():
    """The pre-existing measure chains still land: heart-rate "62 beats per minute" and the
    "CVSS score of 9.8" PP measure are unaffected by the quantity-of chain."""
    m = _reload(LINGUISTIC_LAYER="true")
    hr = m.derive_sentence_facts("My resting heart rate is 62 beats per minute.", _dt(2026, 6, 30))
    assert any(f.scalar_datatype == "string" and "62" in f.object for f in hr), hr
    cv = m.derive_sentence_facts("It has a CVSS score of 9.8.", _dt(2026, 6, 30))
    assert any(f.scalar_datatype == "string" and "9.8" in f.object for f in cv), cv


# ─────────────────── COMPOSITE-ADDRESS RESIDENCE→CITY BRIDGE (DESIGN-address-composite.md) ───────
# The bridge reads GLiNER2 Location typing off ``doc.ents``; the deriver's own ``_parse`` disables NER
# (doc.ents empty), so — exactly as the live harvest hands a GLiNER2-typed Doc — these tests parse with
# a full NER pipeline and pass the Doc to ``derive_sentence_facts`` so Kitchener/Ontario type as GPE.

def _ner_doc(text):
    """A full-pipeline (NER-enabled) spaCy Doc — stands in for the harvest's GLiNER2-typed Doc so the
    Location ents are present. Skips cleanly if the model is absent."""
    import spacy
    nlp = spacy.load("en_core_web_sm")  # default pipeline keeps NER (ents populated)
    return nlp(text)


@requires_model
def test_address_bridge_live_at_street_in_city_province():
    """'I live at <street> in <city>, <province>' → verbatim residence SCALAR (the memory) is kept AND
    a relational lives_in(user, city) BRIDGE is added, adopting the located_in(city, province) chain so
    the city is no longer orphaned. THE HARD LINE: the street stays the scalar; only the bridge is new."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        _ner_doc("I live at 156 Cedar Street South in Kitchener, Ontario."), _dt(2026, 6, 30))
    # the verbatim residence scalar (memory) survives — the street, not decomposed
    assert any(f.subject == "user" and "156 cedar street south" in f.object for f in facts), facts
    # the city/province place chain exists
    assert any(f.subject == "kitchener" and f.rel_type == "located_in" and f.object == "ontario"
               for f in facts), facts
    # the NEW relational bridge links the user to the city (the whole place is now reachable)
    assert any(f.subject == "user" and f.rel_type == "lives_in" and f.object == "kitchener"
               for f in facts), facts
    # the province is reached via located_in, NOT a second lives_in(user, ontario)
    assert not any(f.subject == "user" and f.rel_type == "lives_in" and f.object == "ontario"
                   for f in facts), facts


@requires_model
def test_address_bridge_my_address_is_variant():
    """'My address is <street>, <city>, <province>' → the opaque verbatim address SCALAR is kept AND the
    same lives_in(user, city) bridge is added (no junk-twin cluster)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        _ner_doc("My address is 156 Cedar Street South, Kitchener, Ontario."), _dt(2026, 6, 30))
    assert any(f.subject == "user" and f.scalar_datatype == "string"
               and "kitchener" in f.object for f in facts), facts  # verbatim composite scalar
    assert any(f.subject == "kitchener" and f.rel_type == "located_in" and f.object == "ontario"
               for f in facts), facts
    assert any(f.subject == "user" and f.rel_type == "lives_in" and f.object == "kitchener"
               for f in facts), facts


@requires_model
def test_address_bridge_subject_agnostic_named_person():
    """Subject-agnostic: 'Sarah lives at <street> in <city>, <province>' bridges lives_in(sarah, city)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        _ner_doc("Sarah lives at 5 Elm Road in Waterloo, Ontario."), _dt(2026, 6, 30))
    assert any(f.subject == "sarah" and f.rel_type == "lives_in" and f.object == "waterloo"
               for f in facts), facts
    assert any(f.subject == "waterloo" and f.rel_type == "located_in" and f.object == "ontario"
               for f in facts), facts


@requires_model
def test_address_bridge_single_city_no_double():
    """No-regression: 'I live in Toronto' (single city, no street) still yields ONE residence edge to
    toronto (owned by the SVO chain) — the bridge does NOT add a duplicate lives_in."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(_ner_doc("I live in Toronto."), _dt(2026, 6, 30))
    res = [f for f in facts if f.subject == "user" and f.object == "toronto"
           and f.rel_type in ("live_in", "lives_in")]
    assert len(res) == 1, facts


@requires_model
def test_address_bridge_employment_not_mis_bridged():
    """Guardrail: 'I work at Google in Mountain View' is EMPLOYMENT — the folded predicate is not a
    residence rel, so NO lives_in bridge is emitted for the city."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(_ner_doc("I work at Google in Mountain View."), _dt(2026, 6, 30))
    assert not any(f.rel_type == "lives_in" for f in facts), facts


@requires_model
def test_address_bridge_birth_not_mis_bridged():
    """Guardrail: 'I was born at <street> in <city>, <province>' — born_in is location-category but
    IMMUTABLE (a birthplace never moves), so it is NOT a residence and emits NO lives_in bridge."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        _ner_doc("I was born at 12 Oak Street in Hamilton, Ontario."), _dt(2026, 6, 30))
    assert not any(f.rel_type == "lives_in" for f in facts), facts


# ─────────────────────────── 2026-07-06 spine fixes: atomic-scalar shape gate, ──────────────────
# ─────────────────────────── predicate-nominal role chain, full-NP copula complement ────────────
# Grounding (constraint 11): UD tags STRUCTURED numeric formats ("11:00", "11/11/1918" — and a
# dotted-quad IP, live-parsed NUM/CD dep=attr) as NUM exactly like a bare cardinal
# (universaldependencies.org/u/pos/NUM.html), so POS alone cannot separate an age from an IP —
# hence the value-shape gate. "X is my employer" is the textbook copular clause whose NONVERBAL
# PREDICATE is the possessed role noun (universaldependencies.org/u/dep/cop.html; spaCy 'attr' =
# the copular predicate "attribute" — github.com/explosion/spacy/blob/master/spacy/glossary.py).


@requires_model
def test_copula_measure_rejects_structured_numeric_literal():
    """IP-as-age fix: 'The edge firewall's IP is 172.16.5.9' must NOT mint an age scalar — a
    dotted quad parses as a single NUM attr token (verified live), but any internal structure
    ('.', ':', '-', '/') marks a formatted literal owned by the atomic-scalar detector."""
    t = _triples(_derive("The edge firewall's IP is 172.16.5.9."))
    assert not any(r == "age" for (_s, r, _o) in t), t
    assert not any(r == "age" and o == "172.16.5.9" for (_s, r, o) in t), t


@requires_model
def test_copula_measure_bare_plain_cardinal_still_lands():
    """Regression guard for the shape gate: a PLAIN integer cardinal is still an age."""
    assert ("sarah", "age", "28") in _triples(_derive("Sarah is 28.")), "plain cardinal age"


@requires_model
def test_copula_role_predicate_binds_subject_np():
    """Predicate-nominal role: 'Globex Industries is my employer' binds the SUBJECT NP as the
    filler entity via the role_noun cue map (employer→works_for, USER→FILLER direction) —
    (user, works_for, globex industries) — and the (user, owns, employer) twin is suppressed so
    the lone role noun never becomes a standalone (GLiNER2-mistypable) entity."""
    t = _triples(_derive("Globex Industries is my employer."))
    assert ("user", "works_for", "globex industries") in t, t
    assert not any(r == "owns" and o == "employer" for (_s, r, o) in t), t
    assert not any(s == "employer" for (s, _r, _o) in t), t


@requires_model
def test_copula_role_predicate_person_filler():
    """Role map is metadata-driven, not an 'employer' literal: 'Jane is my boss' → boss→works_for
    (works_for tail_types admit Person)."""
    t = _triples(_derive("Jane is my boss."))
    assert ("user", "works_for", "jane") in t, t


@requires_model
def test_copula_role_predicate_unmapped_role_keeps_ownership():
    """Honest no-op on a map miss: an UNMAPPED possessed complement keeps today's ownership
    reading — the interplay guard steps aside ONLY on a role_noun map hit."""
    t = _triples(_derive("That painting is my treasure."))
    assert not any(r == "works_for" for (_s, r, _o) in t), t
    # the possessive reading survives (user owns/related-to the possessed noun) — no capture lost
    assert any(o == "treasure" or s == "treasure" for (s, _r, o) in t), t


@requires_model
def test_analyze_copula_nominal_complement_full_np():
    """Full-NP complement: 'I am a principal network architect' keeps the premodifiers
    (amod 'principal' + compound 'network'), not just the head token."""
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("I am a principal network architect")
    assert c is not None and c.subject_is_self is True
    assert c.relation == "occupation"
    assert c.complement == "principal network architect", c


@requires_model
def test_has_attr_value_captures_hex_address_scalar_colon_and_hyphen():
    """HAS-ATTR-VALUE colon/hyphen-hex scalar (MAC-shape) — value-FORMAT recognition, subject-agnostic.

    A colon-hex MAC (with digits) was already kept whole; the fix keeps a DIGITLESS all-hex-letter
    hyphen MAC ("aa-bb-cc-dd-ee-ff") whole too (tokenizer hook), and strips a GLUED trailing period
    ("00:1A:2B:3C:4D:5E."). Each lands as a string scalar edge routed to entity_attributes downstream.
    Networking is NOT hardcoded — the attribute name comes from the sentence, the VALUE from its shape."""
    m = _reload(LINGUISTIC_LAYER="true")
    cases = [
        ("core-1 has mac address 00:1a:2b:3c:4d:5e.", "core-1", "00:1a:2b:3c:4d:5e"),
        ("my laptop has mac address aa-bb-cc-dd-ee-ff.", "laptop", "aa-bb-cc-dd-ee-ff"),
        ("the printer has mac 00:1A:2B:3C:4D:5E.", "printer", "00:1a:2b:3c:4d:5e"),
    ]
    for sent, subj, value in cases:
        facts = m.derive_sentence_facts(sent, None)
        scalars = [f for f in facts
                   if f.subject == subj and getattr(f, "scalar_datatype", None) == "string"
                   and f.object == value]
        assert scalars, f"no hex-address scalar for {sent!r}: got {[(x.subject, x.rel_type, x.object) for x in facts]!r}"
        # the attribute is derived from the sentence noun ("mac"/"mac_address"), never a hardcoded rel.
        assert any("mac" in f.rel_type for f in scalars), scalars


@requires_model
def test_has_attr_value_captures_combined_ip_and_mac():
    """Combined 'X has ip <IP> and mac <MAC>' pairs each attr→value into its own scalar edge."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("core-1 has ip 10.0.0.1 and mac 00:1a:2b:3c:4d:5e.", None)
    vals = {(f.rel_type, f.object) for f in facts if getattr(f, "scalar_datatype", None) == "string"}
    assert ("ip", "10.0.0.1") in vals, vals
    assert ("mac", "00:1a:2b:3c:4d:5e") in vals, vals


@requires_model
def test_analyze_copula_adj_complement_stays_bare():
    """Scope guard: the NP expansion is NOMINAL-only — the feeling path ('I am excited') still
    returns the bare ADJ complement."""
    m = _reload(LINGUISTIC_LAYER="true")
    c = m.analyze_copula("I am excited")
    assert c is not None and c.complement == "excited" and c.relation == "feels", c


@requires_model
def test_copula_identifying_of_pp_captures_named_referent():
    """Specificational copula with an identifying "of"-PP — "The play I attended was a production
    of The Glass Menagerie" — must CAPTURE the named work (previously dropped as CRIT-uncovered
    residue). Two edges: the subject is related_to the named referent, AND the first-person
    relative clause collapses the placeholder head → (user, <relcl-verb>, <named work>). The named
    work must NOT be lost. Subject-agnostic (no domain/verb literals) — a PROPN of-PP pobj only."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2023, 5, 27)
    facts = m.derive_sentence_facts(
        "The play I attended was a production of The Glass Menagerie.", ref)
    pairs = {(f.subject, f.rel_type, f.object) for f in facts}
    # the named work is captured on the subject entity (loose identifying link) …
    assert ("play", "related_to", "glass menagerie") in pairs, pairs
    # … and the first-person relcl collapses the placeholder → the user's action on the named work
    assert ("user", "attend", "glass menagerie") in pairs, pairs


@requires_model
def test_copula_of_pp_common_noun_not_captured():
    """Firewall: a COMMON-noun "of"-PP pobj ("a bowl of water") is NOT a named referent — no
    related_to edge is minted for it (only a PROPER-NOUN identifying work qualifies)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("The soup I made was a bowl of water.", _dt(2023, 5, 27))
    assert not any(f.rel_type == "related_to" and f.object == "water" for f in facts), facts


# ─────────── NESTED LEFT-COMPOUND CHAIN — object-phrase truncation (LME dense-haystack) ───────────
# spaCy builds a left-branching noun compound as a CHAIN, so only the topmost modifier is a DIRECT
# child of the head and the deeper name tokens were dropped ("sports store downtown" → "store
# downtown"; "10% discount" → "% discount"), losing the answer-bearing token and defeating the
# multi-token containment the recall scorer needs. _object_value_phrase now descends the nested
# chain. These cover 2+ LongMemEval "dense-haystack-capture" exemplars at the capture level.
def _last_noun_head(m, text):
    doc = m._parse(text)
    cand = [t for t in doc if t.pos_ in ("NOUN", "PROPN")]
    return cand[-1]


@requires_model
def test_object_phrase_nested_compound_chain_preserved():
    """A left-nested compound chain keeps ALL name tokens (tennis-racket exemplar f8c5f88b:
    "the sports store downtown")."""
    m = _reload(LINGUISTIC_LAYER="true")
    out = m._object_value_phrase(_last_noun_head(m, "a sports store downtown"))
    for tok in ("sports", "store", "downtown"):
        assert tok in out, (out, tok)


@requires_model
def test_object_phrase_nested_nummod_under_compound_preserved():
    """A number nested under an admitted compound is part of the value, not a count (discount
    exemplar 726462e0: gold "10%" — the "10" must survive)."""
    m = _reload(LINGUISTIC_LAYER="true")
    out = m._object_value_phrase(_last_noun_head(m, "a 10% discount"))
    assert "10" in out, out
    assert "discount" in out, out


@requires_model
def test_object_phrase_count_firewall_unchanged():
    """The head's OWN direct nummod is still a COUNT, not folded into the value ("3 cats" → "cats",
    "3 file servers" → "file servers"). The nested descent must not regress this."""
    m = _reload(LINGUISTIC_LAYER="true")
    assert m._object_value_phrase(_last_noun_head(m, "3 cats")) == "cats"
    assert "3" not in m._object_value_phrase(_last_noun_head(m, "3 file servers"))


@requires_model
def test_dense_haystack_object_capture_contains_gold_tokens():
    """End-to-end deriver: the acquired-object edge for two dense-haystack exemplars now CONTAINS
    the gold's salient tokens (was truncated to a blob that dropped them)."""
    m = _reload(LINGUISTIC_LAYER="true")
    # tennis-racket (f8c5f88b): gold "the sports store downtown"
    facts = m.derive_sentence_facts(
        "I got my new tennis racket from a sports store downtown.", None)
    objs = " ".join(f.object for f in facts)
    for tok in ("sports", "store", "downtown"):
        assert tok in objs, (tok, objs)
    # discount (726462e0): gold "10%" → salient token "10"
    facts2 = m.derive_sentence_facts(
        "I got a 10% discount on my first purchase from that new clothing brand.", None)
    objs2 = " ".join(f.object for f in facts2)
    assert "10" in objs2, objs2


# ─────────── LME cluster (76d63226 / gpt4_2c50253f): attributive VALUE/MEASURE scalar capture ────────
# CLUSTER: a user-stated VALUE/MEASUREMENT that spaCy tokenizes as a MERGED premodifier ("55-inch",
# "3-bedroom") was DROPPED as a hard-line residue — no scalar chain saw it (the ADJ/NUM attributive
# chain never admits a NOUN-tagged merged token), and a dense premodified object NP whose merge split
# it into TWO sibling dobj heads ("… Samsung 55-inch 4K smart TV") returned the BRAND ("Samsung") and
# orphaned the true head noun "TV" + its measurement. Fix: (1) _svo_object_head head-final recovery for
# a contiguous multi-dobj fragment; (2) _chain_attributive_merged_measure scalarizes the merged measure
# onto the modified head's NP (reachable == the SVO object surface). Subject-agnostic, deterministic.

@requires_model
def test_attributive_merged_measure_scalarized_on_reachable_head():
    """EX2 (76d63226) — 'I set up my new Samsung 55-inch 4K smart TV'. FAIL-ON-OLD: the object was
    'new samsung' (brand), the head 'TV' + '55-inch' were dropped as residue, and no scalar carried the
    size. PASS-ON-FIX: the SVO object is the head-final 'TV' NP AND a scalar carries '55-inch' on the
    SAME entity surface (so it is reachable from the user via the descendant walk)."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I set up my new Samsung 55-inch 4K smart TV", None)
    # the head-final noun is recovered as the acquired object (not the brand 'samsung' alone) …
    acquired = [f for f in facts if f.subject == "user" and "tv" in f.object]
    assert acquired, f"head-final TV object not recovered: {facts!r}"
    # … and the measurement is a walkable SCALAR whose subject == the object entity surface (reachable).
    scal = [f for f in facts
            if f.scalar_datatype == "string" and "55-inch" in f.object]
    assert scal, f"'55-inch' not captured as a scalar: {facts!r}"
    obj_surfaces = {f.object for f in acquired}
    assert any(s.subject in obj_surfaces for s in scal), \
        f"measure scalar not anchored on the reachable object entity: scal={scal!r} objs={obj_surfaces!r}"


@requires_model
def test_attributive_merged_measure_generalizes_subject_agnostic():
    """CLUSTER generalization (NOT a per-question hack) — a merged '<num>-<unit>' premodifier on ANY
    thing is scalarized onto that thing's NP, whether or not the unit is in the seed map (a novel unit
    falls back to the unit token itself as the scalar attribute → still walkable, flows to growth). The
    scalar subject MATCHES the relational object surface, so it is reachable."""
    m = _reload(LINGUISTIC_LAYER="true")
    for sent, val, must_match_obj in (
        ("I bought a 3-bedroom apartment", "3-bedroom", "apartment"),
        ("I installed a 500-gb drive", "500-gb", "drive"),
        ("I built a 6-foot fence", "6-foot", "fence"),
    ):
        facts = m.derive_sentence_facts(sent, None)
        scal = [f for f in facts if f.scalar_datatype == "string" and f.object == val]
        assert scal, f"merged measure {val!r} not scalarized for {sent!r}: {facts!r}"
        rel_objs = {f.object for f in facts if f.scalar_datatype is None and must_match_obj in f.object}
        assert any(s.subject in rel_objs for s in scal), \
            f"scalar {val!r} not anchored on the reachable object for {sent!r}: {facts!r}"


@requires_model
def test_merged_measure_does_not_misfire_on_bare_count_or_age():
    """Non-regression: a BARE count ('3 cats') is NEVER scalarized as a MEASURE (a numeral+noun value
    like '3 cats'), and the ADJ person-age path ('12 years old') is untouched.

    UPDATED for ``_chain_count_scalar``: a possessed bare count IS now a COUNT scalar (object = the bare
    numeral '3', rel = the noun 'cats') — distinct from the merged-measure misfire this guards (object
    would glue the noun, '3 cats'). Assert the misfire is absent AND the count scalar is present."""
    m = _reload(LINGUISTIC_LAYER="true")
    f1 = m.derive_sentence_facts("I have 3 cats", None)
    assert not [f for f in f1 if f.scalar_datatype == "string" and "cat" in (f.object or "")], \
        f"bare count mis-scalarized as a measure: {f1!r}"
    assert any(f.scalar_datatype == "string" and f.rel_type == "cats" and f.object == "3" for f in f1), \
        f"bare count not captured as a count scalar: {f1!r}"
    f2 = m.derive_sentence_facts("my son David is 12 years old", None)
    assert any(f.rel_type == "age" and f.object == "12" for f in f2), \
        f"person-age path regressed: {f2!r}"


@requires_model
def test_ex1_clock_time_already_captured_at_ingest():
    """EX1 (gpt4_2c50253f) — DISTINCT ROOT, reported honestly: the clock-time VALUE is ALREADY captured
    as a scalar at ingest ('I wake up at 6:45 AM …' → (user, wake, '6:45 am')). EX1 is therefore NOT an
    ingest CAPTURE defect; its benchmark miss is downstream (a competing distractor surfaced at recall),
    which cannot be fixed under the no-re-ingest constraint. This test pins the already-clean capture so
    the cluster fix is not mistaken for EX1's owner."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("I wake up at 6:45 AM on Tuesdays and Thursdays", None)
    assert any(f.scalar_datatype == "string" and "6:45" in f.object for f in facts), \
        f"EX1 clock-time value not captured at ingest: {facts!r}"


@requires_model
def test_comma_appositive_role_name_binds_in_agent_pp():
    """COMMA-ADJACENCY DANGLING-APPOSITIVE RECOVERY (LME gpt4_f2262a51 — cross-session countable-instance
    undercapture, "how many different doctors did I visit"). English apposes a proper name to a common-
    noun role with a bare comma: "my dermatologist, Dr. Lee" / "an ENT specialist, Dr. Patel". When the
    role sits in a clean object slot spaCy attaches the name as ``appos`` and the binding is found. But
    when the role is buried in an AGENT PP ("diagnosed ... by an ENT specialist, Dr. Patel") the parser
    DANGLES the name onto the ROOT verb as ``npadvmod`` — no ``appos`` child — and the named instance is
    SILENTLY DROPPED. The recovery reads the parse-independent surface signal (singular common-noun role
    + ", " + PROPN name run) so ALL THREE doctors bind uniformly and each lands ``instance_of`` its role.
    Subject-agnostic (NO role/domain word list); structural (comma adjacency + PROPN)."""
    m = _reload(LINGUISTIC_LAYER="true")
    # the three doctor mentions across sessions — each must bind name -> role.
    for sent, name, role_head in (
        ("I just got back from a follow-up appointment with my dermatologist, Dr. Lee.",
         "dr. lee", "dermatologist"),
        ("I recently had a UTI and was prescribed antibiotics by my primary care physician, Dr. Smith.",
         "dr. smith", "physician"),
        ("I just got diagnosed with chronic sinusitis by an ENT specialist, Dr. Patel.",
         "dr. patel", "specialist"),
    ):
        bindings = m.analyze_name_type_bindings(sent)
        assert any((b.name or "").lower() == name and role_head in (b.type_noun or "")
                   for b in bindings), \
            f"appositive role<-name not bound for {sent!r}: {[(b.name, b.type_noun) for b in bindings]}"


@requires_model
def test_comma_appositive_recovery_does_not_overfire():
    """Non-regression for the comma-adjacency recovery: it must NOT bind when the post-comma proper noun
    is a CLAUSE SUBJECT ("..., Dr. Lee said hello"), a wh-relative ("..., which was closed"), or a
    coordinating conjunction follows ("..., and we ..."). A genuine apposition is a RENAME, never a new
    clause / relative / coordinated continuation."""
    m = _reload(LINGUISTIC_LAYER="true")
    for sent in (
        "I went to the store, Dr. Lee said hello.",     # Dr. Lee is nsubj of 'said'
        "I visited the clinic, which was closed.",       # wh-relative
        "I met my friend, and we had coffee.",           # comma + CCONJ
    ):
        bindings = m.analyze_name_type_bindings(sent)
        assert not any(b.connector == "appos" and (b.name or "").lower().startswith("dr")
                       for b in bindings), \
            f"comma-appositive recovery over-fired on {sent!r}: {[(b.name, b.type_noun, b.connector) for b in bindings]}"


@requires_model
def test_g2_bare_cardinal_verb_frame_scalar_capture():
    """G2 — a NUM(+no unit) that IS the answer, standing as the verb's OWN direct object, is captured
    as a SCALAR under the verb lemma even when a rival PP wins the relation slot. "I scored 95 on the
    exam" drops the 95 on the SVO backbone (a ``(user, score_on, exam)`` PP relation captures the object
    slot and the bare cardinal vanishes). The verb-frame measure lane now ALSO emits the bare-cardinal
    RESULT scalar ``(user, score, 95)`` — WITHOUT suppressing the coexisting rival relation. Deterministic
    (NUM dobj grammar + value-shape), subject-agnostic, no verb/unit word list."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 6, 21)
    facts = m.derive_sentence_facts("I scored 95 on the exam.", ref)
    # the bare-cardinal RESULT lands as a SCALAR on the user under the verb lemma
    scalar = [f for f in facts
              if f.subject == "user" and f.rel_type == "score"
              and f.object == "95" and f.scalar_datatype]
    assert scalar, f"bare-cardinal score scalar (user, score, 95) not captured: {facts!r}"
    # the rival PP relation COEXISTS (both survive — the scalar does not suppress it)
    assert any(f.subject == "user" and f.object == "exam" for f in facts), \
        f"rival PP relation (user, score_on, exam) was suppressed: {facts!r}"
    # other bare-NUM-doj result frames land too (weigh / turn), the number is never dropped.
    # ROUND-4 vehicle swap (SPINE_TURNED_AGE): "I turned 30." now files the result under
    # the AGE convention — (user, age, 30), the same storage the copula-measure "I am 30"
    # uses — instead of the legacy verb-lemma (user, turn, 30) that the age-ask could
    # never serve. That age storage carries scalar_datatype None (byte-parity with the
    # "X is 9" copula-measure age row), so the datatype truthiness applies to the weigh
    # scalar only. The mechanism assertion is unchanged: the bare cardinal is never dropped.
    for sent, rel, val, need_sdt in (("I weigh 180.", "weigh", "180", True),
                                     ("I turned 30.", "age", "30", False)):
        f2 = m.derive_sentence_facts(sent, ref)
        assert any(x.subject == "user" and x.rel_type == rel and x.object == val
                   and (x.scalar_datatype if need_sdt else True)
                   for x in f2), f"bare-cardinal result scalar dropped for {sent!r}: {f2!r}"


@requires_model
def test_g2_bare_cardinal_does_not_overfire_on_counts():
    """G2 non-regression — a NUM that is a nummod UNDER a noun ("have 3 cats") or a bare cardinal in a
    PP ("applied for 3 jobs") is a COUNT, NOT a verb result: the bare-cardinal scalar lane must NOT mint
    a ``(user, have, 3)`` / ``(user, apply, 3)`` measure. The count / SVO lanes own those. Also asserts
    the copula-measure chain ("She is 62 years old" → age 62) is untouched by the new branch."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 6, 21)
    # "have 3 cats" — dobj is the NOUN "cats" (3 is a nummod); the verb-frame scalar must NOT fire.
    f_cats = m.derive_sentence_facts("I have 3 cats.", ref)
    assert not any(f.rel_type == "have" and f.object == "3" for f in f_cats), \
        f"bare-cardinal lane over-fired on a count 'have 3 cats': {f_cats!r}"
    # "applied for 3 jobs" — 3 is a nummod under a PP pobj; the verb-frame scalar must NOT fire.
    f_jobs = m.derive_sentence_facts("I applied for 3 jobs.", ref)
    assert not any(f.rel_type in ("apply", "apply_for") and f.object == "3" for f in f_jobs), \
        f"bare-cardinal lane over-fired on a PP count 'applied for 3 jobs': {f_jobs!r}"
    # copula-measure age capture is untouched by the new branch.
    f_age = m.derive_sentence_facts("She is 62 years old.", ref)
    assert any(f.rel_type == "age" and f.object == "62" for f in f_age), \
        f"copula-measure age regressed: {f_age!r}"


# ─────────────────── VERB-MEASURE SCALAR lane (VERB_MEASURE_SCALAR, issue #9) ───────────────────
# THE FRAME: a MEASURE VERB + [approximator] NUM unit [postmodifier] — "An adult blue-ringed octopus
# measures about 5 centimeters across." fell through EVERY scalar lane (measured: spine 0 edges,
# DETECT-ONLY rewrite 0 → no_ingest). Two admission shapes, both flag-gated:
#   V1 — the measure NP (npadvmod NOUN + nummod NUM) hangs under an ADJ/ADV ``advmod`` child of a
#        properly tagged VERB ("The sailfish measures roughly 3 meters long").
#   V2 — spaCy MIS-TAGS "measures" NOUN/NNS and ROOTs the clause (the sentence has NO verb), so a
#        ROOT NOUN whose lemma resolves in the DB-grown ``measure_verb`` cue class recovers the
#        measurement READING; the frame structure (compound subject + nummod'd unit NP + the shared
#        NER QUANTITY discriminator) still gates. Grammar alone was measured under-firing here.
# Subject-agnostic (animals AND structures), no domain word lists; the rel comes from the unit_scalar
# map when the metadata admits the subject, else the verb lemma (a grown/novel rel).

@requires_model
def test_vm9_battery_sentence_commits_scalar():
    """The issue-#9 battery sentence — mis-tagged 'measures' (NOUN/NNS ROOT) — captures the FULL
    measure span (approximator kept as prefix, the classic pre-pass's measured behaviour) as ONE
    scalar on the measured entity.

    ⚠️ subject UPDATED BY #16 (compound-subject-narrowing, lane 19): the measured entity is the
    COMPOUND ("blue-ringed octopus" — the #11 type node the V2 root-recovery reconstructs from
    the mis-tagged ROOT's own modifier children), no longer the bare narrowed head."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts(
        "An adult blue-ringed octopus measures about 5 centimeters across.", _dt(2026, 9, 17))
    scalars = [f for f in facts if f.scalar_datatype]
    assert len(scalars) == 1, f"expected exactly ONE scalar, got: {facts!r}"
    f = scalars[0]
    assert f.subject == "blue-ringed octopus" and "5 centimeters" in (f.object or ""), \
        f"measure not captured on the measured entity: {facts!r}"
    assert f.rel_type in ("height", "measure"), \
        f"rel must be the unit_scalar mapping or the grown verb lemma, got {f.rel_type!r}"


@requires_model
def test_vm9_adverbial_measure_phrase_v1_sailfish():
    """V1 — a properly tagged VERB whose measure NP rides an ADJ ``advmod`` child: the scalar lands
    on the clause subject and the pre-existing junk has_state relational coexists (non-suppressing
    adjunct contract), but there is exactly ONE scalar."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("The sailfish measures roughly 3 meters long.", _dt(2026, 9, 17))
    scalars = [f for f in facts if f.scalar_datatype]
    assert len(scalars) == 1, f"expected exactly ONE scalar, got: {facts!r}"
    assert scalars[0].subject == "sailfish" and "3 meters" in (scalars[0].object or ""), \
        f"V1 measure not captured on the subject: {facts!r}"


@requires_model
def test_vm9_structures_commit_subject_agnostically():
    """Subject-agnostic: a STRUCTURE subject (bridge) commits exactly like an animal one — the lane
    never consults a domain/entity-type list. 'span' is a measure verb on the same grammar."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("The bridge spans nearly 2 kilometers.", _dt(2026, 9, 17))
    scalars = [f for f in facts if f.scalar_datatype]
    assert len(scalars) == 1 and scalars[0].subject == "bridge" \
        and "2 kilometers" in (scalars[0].object or ""), f"structure capture regressed: {facts!r}"


@requires_model
def test_vm9_approximators_kept_as_prefix():
    """The approximator is kept as a VALUE PREFIX (matching the classic pre-pass's measured NER-span
    behaviour: 'about 30 grams'), never dropped and never promoted into the rel."""
    m = _reload(LINGUISTIC_LAYER="true")
    facts = m.derive_sentence_facts("The hatchling weighs about 30 grams.", _dt(2026, 9, 17))
    scalars = [f for f in facts if f.scalar_datatype]
    assert len(scalars) == 1 and scalars[0].object == "about 30 grams", \
        f"approximator handling changed: {facts!r}"


@requires_model
def test_vm9_does_not_fire_without_a_measure_verb():
    """Lane discipline — the copula frame stays the COPULA lane's ('The octopus is 5 centimeters
    across' → height from _chain_copula_measure), and the issue-#8 superlative mis-read gains NO new
    verb-measure scalar (n=0 scalar gain there; the pre-existing junk age='one' is a DIFFERENT seam)."""
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 9, 17)
    cop = m.derive_sentence_facts("The octopus is 5 centimeters across.", ref)
    # exactly the copula lane's capture — no second scalar from this lane
    assert [f for f in cop if f.scalar_datatype] == [] or \
        all(f.rel_type != "measure" for f in cop if f.scalar_datatype), \
        f"this lane fired on a be-clause: {cop!r}"
    assert any(f.rel_type == "height" for f in cop), f"copula lane capture regressed: {cop!r}"
    sup = m.derive_sentence_facts(
        "The blue-ringed octopus is one of the most venomous cephalopods.", ref)
    assert not any(f.scalar_datatype for f in sup), \
        f"superlative frame wrongly produced a verb-measure scalar: {sup!r}"


@requires_model
def test_vm9_anti_twin_matrix_one_scalar_per_sentence(monkeypatch):
    """Anti-twin: each existing-lane regression sentence gains NO new scalar and keeps its legacy
    count; each new-frame sentence carries exactly ONE scalar. Guards the compose-don't-re-derive
    contract against the copula / adjunct / duration lanes and the classic pre-pass shapes."""
    # No database: this pins the in-code cue FLOOR (the seeded-table divergence is #75;
    # it used to run DSN-less only because another module popped the DSN at import (#63).
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    m = _reload(LINGUISTIC_LAYER="true")
    ref = _dt(2026, 9, 17)
    # (sentence, exact legacy scalar count — measured at HEAD before this lane)
    cases = [
        ("She is 62 years old.", 0),                                  # copula lane: age emits sdt=None
        ("I ran 5 kilometers this morning.", 1),                      # adjunct npadvmod lane
        ("I have been learning Spanish for 3.5 weeks.", 1),           # duration lane
        ("My commute takes 45 minutes each way.", 2),                 # classic + possessed-measure
        ("The hatchling weighs about 30 grams.", 1),                  # classic dobj shape
        ("The bridge spans nearly 2 kilometers.", 1),                 # classic dobj shape
        ("He weighs 180 pounds.", 1),                                 # classic dobj shape
        ("An adult blue-ringed octopus measures about 5 centimeters across.", 1),  # NEW V2
        ("The sailfish measures roughly 3 meters long.", 1),          # NEW V1
    ]
    for sent, want in cases:
        facts = m.derive_sentence_facts(sent, ref)
        got = sum(1 for f in facts if f.scalar_datatype)
        assert got == want, f"scalar count {got} != legacy+pinned {want} for {sent!r}: {facts!r}"


@requires_model
def test_vm9_flag_off_is_byte_identical_legacy():
    """REVERT-RED: VERB_MEASURE_SCALAR=false → neither frame is admitted, no rel override is set —
    the battery sentence goes back to 0 edges (today's silent drop) and the sibling lanes are
    untouched. This red is the revert lever's pin: the flag must actually disable the lane."""
    m = _reload(LINGUISTIC_LAYER="true", VERB_MEASURE_SCALAR="false")
    ref = _dt(2026, 9, 17)
    assert m.derive_sentence_facts(
        "An adult blue-ringed octopus measures about 5 centimeters across.", ref) == [], \
        "flag OFF must restore the legacy behaviour (battery sentence yields 0 edges)"
    facts = m.derive_sentence_facts("The sailfish measures roughly 3 meters long.", ref)
    assert not any(f.scalar_datatype for f in facts), \
        f"flag OFF must not capture the V1 frame: {facts!r}"
    # legacy capture intact with the flag OFF — including the CLASSIC dobj shape of the very same
    # verb family ("spans nearly 2 kilometers" is the pre-pass's dobj frame, NOT this lane's)
    assert m.derive_sentence_facts("The hatchling weighs about 30 grams.", ref), \
        "flag OFF regressed the classic measure-verb capture"
    b = m.derive_sentence_facts("The bridge spans nearly 2 kilometers.", ref)
    assert any(f.scalar_datatype for f in b), \
        f"flag OFF regressed the classic dobj capture: {b!r}"
    # restore the default (pop → subsequent reloads read the code default ON)
    _reload(LINGUISTIC_LAYER="true", VERB_MEASURE_SCALAR=None)

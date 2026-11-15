"""#40 — spaCy Token identity census + the per-site verdicts that needed a behaviour pin.

THE MECHANISM. spaCy builds a NEW ``Token`` wrapper on every access (``doc[1] is doc[1]`` is
False; ``==`` compares doc + index). So ``x.head is tok`` is always False and ``x.head is not tok``
always True, whatever the parse says. On saas 452b177c / af14483b the AST scan found 67 Is/IsNot
compares between Token-valued expressions in src/ (43 linguistics.py, 22 main.py, 2 clause_pa.py);
the issue's grep found 39 because it only matched ``.head is``.

THE CENSUS (structural, regenerated from source on every run — not a frozen name list):
  1. Across ALL of src/, no Is/IsNot compare may have a Token-shaped side: an attribute that only a
     spaCy Token/Span carries (``.head``, ``.root``, ``.left_edge`` …), or a call/subscript on one.
  2. In every module that reads the dependency parse (an AST ``.dep_`` attribute), EVERY
     non-constant Is/IsNot compare must be on the allowlist below, keyed by (module, enclosing
     function, unparsed compare). Each entry is a dict/sentinel/dataclass identity check that is
     correct as ``is``. A renamed Token variable (``foo is bar``) therefore fails the census even
     though no heuristic recognises it as a Token.
Non-vacuity is proved here: synthetic sources carrying each defect shape must be flagged, and the
census must have actually walked linguistics.py / main.py / clause_pa.py.

VERDICTS (every site adjudicated; per-site before/after deriver runs on a 2,926-sentence battery
harvested from the linguistics/spine tests + 70 fresh-domain sentences + a 57-sentence crafted
battery; see the #40 PR for the table):
  (a) 61 sites -> ``==`` / ``!=``. Every reached site produced byte-identical deriver / analyzer /
      query-helper output with the compare flipped, because each is a ROOT test inside a walk that
      otherwise spins to its hop cap on the same root, or is redundant with a ``dep_ == "ROOT"``
      / ``_CLAUSE_HEAD_DEPS`` test beside it, or compares objects drawn from one list / one
      ``.subtree`` (where identity already held). One reached site changes a HELPER's output and is
      pinned below: ``_trailing_naming_appositive`` now honours its documented gate.
  (b) 3 sites -> guard deleted, today's behaviour kept: _inchoative_crop_anchors' list-head guard
      (turning it on drops the first list member — pinned below), _locative_subject_tok's
      ``_gh is not _h`` (the copula-root case is right as it is), clause_pa's unsatisfiable
      ``aux``-and-self-headed disjunct.
  (c) 3 sites -> today's behaviour made explicit and filed: the phrasal-particle ADV fold in
      _svo_predicate_token (two compares; turning it on folds degree adverbs — "move_far_from" —
      and fragments relations — pinned below) and the SPINE_FUNCTIONAL_SLOT=off legacy role
      filter in _chain_genitive_name.
Plus #40 part 2: ``_chain_named_role``'s bare-first-person-possessor block (pasted from
``_chain_genitive_name``) read undefined ``head`` / ``_inverted`` — pinned below with a Doc that
reaches it.
"""

import ast
import os
import pathlib
from datetime import datetime

import pytest

pytestmark = pytest.mark.usefixtures("production_flags", "no_tenant_db")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Attributes that only a spaCy Token (or Span) carries — a compare touching one is Token-valued.
_TOKEN_ATTRS = frozenset({
    "head", "root", "left_edge", "right_edge", "lefts", "rights", "children", "subtree",
    "ancestors", "conjuncts", "sent", "nbor",
})

# Non-constant identity compares that are CORRECT as ``is`` in parse-reading modules. Keyed by
# (module, innermost enclosing function, unparsed compare) — no line numbers, so it does not rot.
ALLOWLIST = {
    ("src/api/main.py", "_suppress_measurement_family_mints", "e is E"):
        "edge dicts: skip the scalar edge itself while scanning the same list",
    ("src/api/main.py", "_ingest_inflight_guard", "_INGEST_INFLIGHT_LOCKS.get(idempotency_key) is entry"):
        "lock-table entry identity (release only our own entry)",
    ("src/api/main.py", "resolve_typed_walk", "scalar_res is _WALK_KNOWN_GAP"):
        "module sentinel",
    ("src/api/main.py", "_apply_get_first_last", "facts is not _pre"):
        "list identity: did a lane replace the facts list",
    ("src/api/main.py", "_sv_pick_current", "cand is o"):
        "fact dicts from one group list",
    ("src/api/main.py", "_retire_superseded_single_valued_states", "m is cur_m"):
        "fact dicts from one members list",
    ("src/api/main.py", "_recency_collapse_single_valued_state", "c is _cur"):
        "fact dicts from one candidates list",
    ("src/extraction/clause_pa.py", "_build_clause", "a is not agent"):
        "Argument dataclass objects from one args list",
}


def _innermost_def(tree):
    """Map id(Compare node) -> innermost enclosing function name ('<module>' when none)."""
    owner = {}

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.Compare):
                owner[id(child)] = name
            visit(child, name)

    visit(tree, "<module>")
    return owner


def _token_shaped(expr) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Attribute) and n.attr in _TOKEN_ATTRS:
            return True
    return False


def _reads_parse(tree) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "dep_" for n in ast.walk(tree))


def census(sources):
    """``sources``: iterable of (relpath, text). Returns (checked, violations, parse_modules)."""
    checked, violations, parse_modules = 0, [], []
    for rel, text in sources:
        tree = ast.parse(text)
        owner = _innermost_def(tree)
        reads = _reads_parse(tree)
        if reads:
            parse_modules.append(rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)) and not (
                        isinstance(left, ast.Constant) or isinstance(right, ast.Constant)):
                    checked += 1
                    fn = owner.get(id(node), "<module>")
                    pair = f"{ast.unparse(left)} {'is not' if isinstance(op, ast.IsNot) else 'is'} " \
                           f"{ast.unparse(right)}"
                    where = f"{rel}:{node.lineno} ({fn}): {pair}"
                    if _token_shaped(left) or _token_shaped(right):
                        violations.append("TOKEN-SHAPED " + where)
                    elif reads and (rel, fn, pair) not in ALLOWLIST:
                        violations.append("UNLISTED-IN-PARSE-MODULE " + where)
                left = right
    return checked, violations, parse_modules


def _src_sources():
    for p in sorted(SRC.rglob("*.py")):
        yield str(p.relative_to(ROOT)), p.read_text()


# ── the census ────────────────────────────────────────────────────────────────────────────────


def test_no_identity_compare_between_tokens_in_src():
    checked, violations, parse_modules = census(_src_sources())
    assert not violations, (
        "spaCy builds a fresh Token per access, so `is`/`is not` between Tokens is constant "
        "(#40). Compare with == / != (or .i). Offenders:\n  " + "\n  ".join(violations))
    # non-vacuity: the census walked the parse-reading modules and saw the allowlisted compares
    for mod in ("src/extraction/linguistics.py", "src/extraction/clause_pa.py", "src/api/main.py"):
        assert mod in parse_modules, f"census did not treat {mod} as parse-reading"
    assert checked >= len(ALLOWLIST)


def test_allowlist_has_no_stale_entries():
    """Every allowlisted compare still exists — a stale entry would silently widen the census."""
    seen = set()
    for rel, text in _src_sources():
        tree = ast.parse(text)
        owner = _innermost_def(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                    and isinstance(node.ops[0], (ast.Is, ast.IsNot)):
                op = "is not" if isinstance(node.ops[0], ast.IsNot) else "is"
                seen.add((rel, owner.get(id(node)), f"{ast.unparse(node.left)} {op} "
                          f"{ast.unparse(node.comparators[0])}"))
    stale = sorted(k for k in ALLOWLIST if k not in seen)
    assert not stale, f"allowlist entries no longer in src: {stale}"


@pytest.mark.parametrize("snippet", [
    "def f(cur):\n    if cur.head is cur:\n        return 1\n    _ = cur.dep_\n",
    "def f(a, b):\n    return a.head is not b\n",
    "def f(t, r):\n    return t.root is r.root\n",
    "def f(t):\n    _ = t.dep_\n    return [d for d in t.subtree if d is t]\n",
    # a renamed Token compare in a parse-reading module, which no attribute heuristic sees
    "def f(foo, bar):\n    if foo.dep_ == 'nsubj' and foo is not bar:\n        return 1\n",
])
def test_census_flags_planted_defects(snippet):
    _, violations, _ = census([("src/extraction/planted.py", snippet)])
    assert violations, f"census missed a planted Token identity compare:\n{snippet}"


@pytest.mark.parametrize("snippet", [
    "def f(cur):\n    if cur.head is None or cur.head == cur:\n        return 1\n    _ = cur.dep_\n",
    "def f(x):\n    return x is _SENTINEL\n",          # no .dep_ in module -> not a parse module
])
def test_census_passes_correct_shapes(snippet):
    _, violations, _ = census([("src/extraction/clean.py", snippet)])
    assert not violations, violations


def test_spacy_token_identity_mechanism():
    """The premise of the census: a fresh wrapper per access; == is index + doc equality."""
    spacy = pytest.importorskip("spacy")
    nlp = spacy.blank("en")
    doc = nlp("my sister lives in Oslo")
    other = nlp("my sister lives in Oslo")
    assert doc[1] is not doc[1]
    assert doc[1] == doc[1]
    assert doc[1] != other[1]          # same index, different doc
    assert (doc[1] == None) is False   # noqa: E711 — None-safe, used after `is None` guards


# ── behaviour pins for the sites whose verdict changed (or deliberately froze) behaviour ──────

@pytest.fixture(scope="module")
def ling():
    import importlib
    import src.extraction.linguistics as m
    m = importlib.reload(m)
    if m._get_nlp() is None:
        pytest.skip("spaCy model unavailable")
    return m


REF = datetime(2026, 6, 21)


def _triples(m, sentence):
    return {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(sentence, REF)}


def _anchor_canon(m, sentence):
    ev = m.analyze_inchoative(sentence)
    assert ev is not None, f"no inchoative analysis for {sentence!r}"
    return {a[0] for a in ev.anchors}


@pytest.mark.parametrize("sentence,first,rest", [
    ("I started growing tomatoes, peppers, and cucumbers.", "tomato", {"pepper", "cucumber"}),
    # fresh domain
    ("I began planting basil, mint, and thyme in March.", "basil", {"mint", "thyme"}),
])
def test_8774_list_head_is_kept_when_it_is_the_item(ling, sentence, first, rest):
    """(b) the deleted ``h is not item_tok`` guard: flipping it to ``!=`` drops the first member."""
    anchors = _anchor_canon(ling, sentence)
    assert first in anchors, f"first list member dropped (half a list): {anchors}"
    assert rest <= anchors, anchors


@pytest.mark.parametrize("sentence,today,never", [
    ("I came back from vacation.", ("user", "come_from", "vacation"), "come_back_from"),
    ("I moved far from home.", ("user", "move_from", "home"), "move_far_from"),
    # fresh domain
    ("The kids ran away from the dog.", ("kids", "run_from", "dog"), "run_away_from"),
])
def test_6884_adverb_is_not_folded_into_predicate(ling, sentence, today, never):
    """(c) the dead ``_adv.head is verb_tok`` fold: today's relation stays, explicitly."""
    triples = _triples(ling, sentence)
    assert today in triples, triples
    assert not any(r == never for (_s, r, _o) in triples), triples


def test_5943_trailing_appositive_leaves_prenominal_naming_to_named_instance(ling):
    """(a) ``nv.head is obj_tok`` was always False, so the helper double-bound the pre-nominal
    "a puppy named Biscuit" that its docstring hands to _chain_named_instance."""
    m = ling
    for sentence in ("I adopted a puppy named Biscuit.", "I bought a boat called Seawolf."):
        doc = m._parse(sentence)
        verb = next(t for t in doc if t.dep_ == "ROOT")
        obj = next(t for t in doc if t.dep_ == "dobj")
        assert m._trailing_naming_appositive(verb, obj, m._naming_verbs()) == ("", []), sentence
    # control: the DETACHED appositive this helper exists for is still captured
    doc = m._parse("I created this playlist on Spotify that I created, called Summer Vibes.")
    verb = next(t for t in doc if t.dep_ == "ROOT")
    obj = next(t for t in doc if t.dep_ == "dobj")
    name, _toks = m._trailing_naming_appositive(verb, obj, m._naming_verbs())
    assert name == "Summer Vibes"


# ── #40 part 2: the undefined head/_inverted in the bare-possessor naming block ────────────────

def _elided_possessor_doc(m, words, lemmas, poss_morph):
    """A naming clause whose possessed head noun is elided, so the 1st-person possessive hangs
    off the naming verb itself: "My [name] is called Chris". en_core_web_sm never produces this
    attachment from text (2,926 + 65 sentences scanned: zero), so the Doc is built directly —
    derive_sentence_facts accepts a parsed Doc."""
    from spacy.tokens import Doc
    return Doc(
        m._get_nlp().vocab, words=words, spaces=[True, True, True, False, False],
        heads=[2, 2, 2, 2, 2], deps=["poss", "auxpass", "ROOT", "oprd", "punct"],
        pos=["PRON", "AUX", "VERB", "PROPN", "PUNCT"], tags=["PRP$", "VBZ", "VBN", "NNP", "."],
        lemmas=lemmas,
        morphs=[poss_morph, "Mood=Ind|Number=Sing|Person=3|Tense=Pres|VerbForm=Fin",
                "Aspect=Perf|Tense=Past|VerbForm=Part", "Number=Sing", "PunctType=Peri"])


@pytest.mark.parametrize("words,lemmas,poss_morph,name", [
    (["My", "is", "called", "Chris", "."], ["my", "be", "call", "Chris", "."],
     "Number=Sing|Person=1|Poss=Yes|PronType=Prs", "chris"),
    # fresh domain: plural possessor, the other naming verb
    (["Our", "is", "named", "Seawolf", "."], ["our", "be", "name", "Seawolf", "."],
     "Number=Plur|Person=1|Poss=Yes|PronType=Prs", "seawolf"),
])
def test_named_role_bare_possessor_block_reaches_its_emit(ling, words, lemmas, poss_morph, name):
    m = ling
    doc = _elided_possessor_doc(m, words, lemmas, poss_morph)
    triples = {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(doc, REF)}
    assert ("user", "also_known_as", name) in triples, triples

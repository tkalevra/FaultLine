"""G3/G4 verb-argument capture — LongMemEval capture-gap backlog.

Pins the two ingest-side capture fixes landed on the spine deriver:

  • G3  — DITRANSITIVE / DATIVE THEME. "I gave my sister a necklace" — spaCy tags the recipient
          ``dative`` and (a parser wobble) the theme ``npadvmod`` instead of ``dobj``, so the
          transfer's theme ("necklace" — the answer to "what did I give") was dropped. The
          ditransitive frame (a ``dative`` sibling) now admits the bare-NP theme as the object of
          the transfer relation. UD: recipient ``iobj`` / theme ``obj``.

  • G4  — TRAILING "called / named" APPOSITIVE. "…this playlist on Spotify that I created, called
          Summer Vibes." — a DETACHED naming appositive whose participle spaCy mis-attaches to the
          nearer PROPN/verb, so the created thing's NAME was dropped. The name now binds as an
          ``also_known_as`` alias of the clause's own object surface.

THE HARD LINE (asserted): a NAME is filed via the alias registry (``also_known_as``), NEVER
classified into ``instance_of``/``subclass_of``.

Deterministic / subject-agnostic: spaCy dependency + POS + the naming-verb cue class only — no
name/domain word list, no cosine/LLM. First-person is grammatical (``Person=1``).
"""
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
    m = _reload(LINGUISTIC_LAYER="true")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")


def _edges(m, sentence):
    """(subject, rel_type, object) tuples the deriver emits for ``sentence`` (str path, no DB)."""
    return [(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(sentence, None)]


# ─────────────────────────── G3 — ditransitive / dative theme ───────────────────────────

@requires_model
def test_g3_dative_theme_becomes_transfer_object():
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I gave my sister a necklace for her birthday.")
    # the THEME "necklace" now rides the transfer relation, subject = the user.
    assert ("user", "give", "necklace") in edges, edges
    # the recipient still rides its own kinship chain (unchanged).
    assert ("sister", "sibling_of", "user") in edges, edges


@requires_model
def test_g3_dative_theme_coref_recipient_still_captures_theme():
    # "gave HER a necklace" — the recipient is an unresolved pronoun (NOT folded), but the theme
    # must still land. Documented safe boundary: the bare pronoun recipient is not attached.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I gave her a necklace.")
    assert ("user", "give", "necklace") in edges, edges


@requires_model
def test_g3_transitive_dobj_frame_is_unchanged():
    # "gave Sarah a book" — a genuine ``dobj`` theme; the direct-object scan owns it, the dative
    # branch is never reached. Byte-identical to before the fix.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I gave Sarah a book.")
    assert ("user", "give", "book") in edges, edges


@requires_model
def test_g3_gate_no_dative_no_theme_overcapture():
    # A plain adverbial-noun ``npadvmod`` ("each way") must NOT be grabbed as a theme when there is
    # NO dative recipient — the fix is gated on the ditransitive signal.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "My commute takes 45 minutes each way.")
    # never a spurious (user, <verb>, each way) transfer object.
    assert all(obj != "each way" for (_s, _r, obj) in edges), edges


# ─────────────────────────── G4 — trailing "called/named" appositive ───────────────────────────

@requires_model
def test_g4_trailing_called_binds_name_to_object():
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I created this playlist on Spotify, called Summer Vibes.")
    # the created thing keeps its relation …
    assert ("user", "create", "playlist on spotify") in edges, edges
    # … and the trailing name is an alias of THAT SAME object surface.
    assert ("playlist on spotify", "also_known_as", "summer vibes") in edges, edges


@requires_model
def test_g4_hard_line_name_never_classified_into_l4():
    # THE HARD LINE: "summer vibes" is a NAME — it must appear ONLY on also_known_as, never as the
    # subject/object of instance_of or subclass_of.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I created this playlist on Spotify, called Summer Vibes.")
    for (subj, rel, obj) in edges:
        if rel in ("instance_of", "subclass_of"):
            assert "summer vibes" not in (subj, obj), (subj, rel, obj)
        if "summer vibes" in (subj, obj):
            assert rel == "also_known_as", (subj, rel, obj)


@requires_model
def test_g4_attached_named_instance_not_double_bound():
    # The pre-nominal "a puppy named Rex" (participle DIRECTLY on the object) stays OWNED by the
    # named-instance chain — exactly ONE also_known_as, no duplicate from the trailing detector.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I adopted a puppy named Rex.")
    aka = [(s, r, o) for (s, r, o) in edges if r == "also_known_as" and o == "rex"]
    assert aka == [("puppy", "also_known_as", "rex")], edges


@requires_model
def test_g4_no_naming_appositive_is_unchanged():
    # A plain create clause with no trailing name emits NO stray alias edge.
    m = _reload(LINGUISTIC_LAYER="true")
    edges = _edges(m, "I created a playlist on Spotify.")
    assert all(r != "also_known_as" for (_s, r, _o) in edges), edges

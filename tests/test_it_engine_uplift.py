"""[it branch] Italian engine localisation: naming frame, first-person possessive, query hook.

The Italian parses run in a child process under it_core_news_sm (tests/_it_capture_helper.py) so
the suite's English pipeline is never swapped. The Italian closed-class members live in the DB
(migration 282), so these tests need a POSTGRES_DSN migrated from this tree; without the model or
the DSN they skip honestly.
"""
import json
import os
import subprocess
import sys

import pytest

_HELPER = os.path.join(os.path.dirname(__file__), "_it_capture_helper.py")


def _it_model_installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("it_core_news_sm") is not None
    except Exception:  # noqa: BLE001
        return False


def _dsn_ok() -> bool:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg2
        c = psycopg2.connect(dsn, connect_timeout=3)
        cur = c.cursor()
        cur.execute("SELECT 1 FROM public.linguistic_cues WHERE category = 'first_person_possessive' LIMIT 1")
        ok = cur.fetchone() is not None
        c.close()
        return ok
    except Exception:  # noqa: BLE001
        return False


needs_it = pytest.mark.skipif(
    not (_it_model_installed() and _dsn_ok()),
    reason="it_core_news_sm not installed or no POSTGRES_DSN migrated with the Italian seeds (282)")


def _run(mode, *items):
    env = dict(os.environ)
    p = subprocess.run([sys.executable, _HELPER, mode, *items], capture_output=True, text=True,
                       env=env, timeout=600)
    path = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else ""
    assert path and os.path.exists(path), f"helper failed: rc={p.returncode} err={p.stderr[-2000:]}"
    with open(path) as fh:
        data = json.load(fh)
    os.unlink(path)
    return data


# Every sentence the facts pins read, derived in ONE child process (the model + DB load dominate).
_SENTENCES = (
    "mia figlia si chiama Anna",
    "il mio cane si chiama Fido",
    "La mia barca si chiama Stella",
    "Mi chiamo Marco",
    "Io mi chiamo Marco Rossi",
    "Ho una figlia che si chiama Anna",
    "Mia madre chiama Anna ogni giorno",
    "Mio fratello non si chiama Luca",
    "Il cane di Luca si chiama Fido",
    "cane|Luca::Il cane di Luca si chiama Fido",
    "sorella|cane::Il cane si chiama Rex",
    "Mi chiama Luca",
    "Ci chiama Luca",
    "mia sorella vive a Roma",
    "il mio amico è simpatico",
    "la sua amica è simpatica",
    "La corda è lunga 4 metri",
    "Mia figlia è alta 120 centimetri",
    "Il film dura 2 ore",
    "Ho 34 anni",
    "mia sorella ha 30 anni",
    "Mio figlio non ha 12 anni",
    "Il ponte è lungo 300 metri",
    "Ho 10 anni di esperienza",
    "Ho 2 chili di farina",
    "Ho 3 ore di tempo",
    "Il corso dura 3 anni",
    "Il film è lungo 2 ore",
    "Quando avevo 20 anni vivevo a Roma",
)
_FACTS_CACHE: dict = {}


def _facts(*sentences):
    if not _FACTS_CACHE:
        _FACTS_CACHE.update(zip(_SENTENCES, _run("facts", *_SENTENCES)))
    return [_FACTS_CACHE[s] if s in _FACTS_CACHE else _run("facts", s)[0] for s in sentences]


def _has(facts, s, r, o):
    return any(f["subject"] == s and f["rel_type"] == r and f["object"] == o for f in facts)


# ── Target 1: the Italian naming frame (reflexive chiamarsi + PROPN complement) ──────────────

@needs_it
def test_kin_naming_binds_the_name_to_the_role():
    (f,) = _facts("mia figlia si chiama Anna")
    assert _has(f, "anna", "child_of", "user"), f
    assert not any(x["rel_type"] == "chiamare" for x in f), f"junk verb-lemma rel filed: {f}"
    # the role noun never becomes a parallel entity carrying the kin edge
    assert not _has(f, "figlia", "child_of", "user"), f


@needs_it
def test_thing_naming_files_the_preferred_label():
    (f,) = _facts("il mio cane si chiama Fido")
    pn = [x for x in f if x["rel_type"] == "pref_name"]
    assert pn and pn[0]["subject"] == "cane" and pn[0]["object"] == "fido", f
    assert pn[0]["preferred_label"] is True, f
    assert not any(x["rel_type"] == "chiamare" for x in f), f


@needs_it
def test_fresh_domain_control_thing_naming():
    """A domain nothing in the code or seeds mentions: the frame is grammar, not vocabulary."""
    (f,) = _facts("La mia barca si chiama Stella")
    assert _has(f, "barca", "pref_name", "stella"), f


@needs_it
def test_first_person_naming_binds_the_user():
    f1, f2 = _facts("Mi chiamo Marco", "Io mi chiamo Marco Rossi")
    assert _has(f1, "user", "also_known_as", "marco"), f1
    assert not any(x["rel_type"] in ("has_state", "chire", "chare") for x in f1), f1
    assert _has(f2, "user", "also_known_as", "marco rossi"), f2
    assert not any(x["rel_type"] in ("chire", "chare") for x in f2), f2


@needs_it
def test_relative_clause_naming_binds_the_antecedent():
    (f,) = _facts("Ho una figlia che si chiama Anna")
    assert _has(f, "figlia", "also_known_as", "anna"), f
    assert not any(x["subject"] == "che" or x["rel_type"] == "chiamare" for x in f), f


@needs_it
def test_active_calling_is_not_naming():
    """No reflexive clitic → plain 'call' (it_core_news_sm still parses Anna as xcomp)."""
    (f,) = _facts("Mia madre chiama Anna ogni giorno")
    assert not any("anna" in (x["subject"], x["object"]) and x["rel_type"] in
                   ("pref_name", "also_known_as", "parent_of") for x in f), f


@needs_it
def test_negated_naming_is_absence():
    (f,) = _facts("Mio fratello non si chiama Luca")
    assert not any("luca" in (x["subject"], x["object"]) for x in f), f


@needs_it
def test_third_party_owner_is_never_rebound_onto_the_bare_noun():
    # with AND without a prior-turn antecedent in the pool (the _prior path is exercised)
    f1, f2 = _facts("Il cane di Luca si chiama Fido", "cane|Luca::Il cane di Luca si chiama Fido")
    assert not any(x["rel_type"] == "pref_name" for x in f1), f1
    assert not any(x["rel_type"] == "pref_name" for x in f2), f2


@needs_it
def test_bare_definite_never_names_the_bare_key_even_with_a_prior():
    """'Mia sorella ha un cane.' + 'Il cane si chiama Rex.' — whose dog is not decidable without a
    referent model; keyed on the bare 'cane' it would later be rebound by 'il mio cane…'."""
    (f,) = _facts("sorella|cane::Il cane si chiama Rex")
    assert not any(x["rel_type"] == "pref_name" or x["object"] == "rex" for x in f), f


@needs_it
def test_clitic_object_of_a_third_person_verb_is_not_self_naming():
    """'Mi chiama Luca' = Luca calls me; 'Ci chiama Luca' = Luca calls us."""
    f1, f2 = _facts("Mi chiama Luca", "Ci chiama Luca")
    for f in (f1, f2):
        assert not any(x["rel_type"] == "also_known_as" and x["subject"] == "user" for x in f), f


# ── Target 2: the first-person possessive (Person=1 restored from the cue class) ──────────────

@needs_it
def test_bridge_restores_person_on_first_person_possessives_only():
    (toks,) = _run("morph", "mia figlia e la sua amica")
    by = {t["text"]: t for t in toks}
    assert by["mia"]["person"] == ["1"] and by["mia"]["poss"] == ["Yes"], toks
    assert by["sua"]["person"] != ["1"], toks  # third person untouched


@needs_it
def test_possessed_kin_and_social_roles_anchor_on_the_user():
    f1, f2, f3 = _facts("mia sorella vive a Roma", "il mio amico è simpatico", "la sua amica è simpatica")
    assert _has(f1, "sorella", "sibling_of", "user"), f1
    assert _has(f2, "amico", "friend_of", "user"), f2
    assert not _has(f2, "user", "owns", "amico"), f"a person filed as an owned object: {f2}"
    assert not any(x["object"] == "user" for x in f3), f"third-person possessive bound to the user: {f3}"


@needs_it
def test_query_first_person_possessive_hook_reads_italian():
    assert _run("fp_poss", "Chi è mia figlia?", "Chi è sua figlia?") == [True, False]


@needs_it
def test_query_scopes_the_kin_rel_for_an_italian_possessive():
    (w,) = _run("walk", "Chi è mia figlia?")
    assert w["fp_poss"] is True and "parent_of" in w["rels"], w


# ── English is byte-identical: the bridge and the UD arms never touch a Penn parse ────────────

class _Morph:
    def __init__(self, feats):
        self._f = feats

    def get(self, k):
        v = self._f.get(k)
        return [v] if v else []


class _Tok:
    def __init__(self, dep, morph=None):
        self.dep_ = dep
        self.morph = _Morph(morph or {})


def test_possessive_marker_reads_both_label_schemes():
    from src.extraction import linguistics as L
    assert L._is_possessive_marker(_Tok("poss"))
    assert L._is_possessive_marker(_Tok("det:poss", {"Poss": "Yes"}))
    assert not L._is_possessive_marker(_Tok("det", {"Poss": "Yes"}))
    # an argument-slot possessive pronoun ("the book is mine") is not a marker
    assert not L._is_possessive_marker(_Tok("attr", {"Poss": "Yes"}))
    assert L._first_person_possessive_marker(_Tok("det:poss", {"Poss": "Yes", "Person": "1"}))
    assert not L._first_person_possessive_marker(_Tok("det:poss", {"Poss": "Yes"}))


def test_bridge_is_inert_on_an_english_pipeline():
    from src.extraction import linguistics as L

    class _Nlp:
        lang = "en"
        pipe_names = []

        def add_pipe(self, *_a, **_k):
            raise AssertionError("bridge must never be added to an English pipeline")

    L._install_ud_morph_bridge(_Nlp())

    class _Doc(list):
        lang_ = "en"
    d = _Doc()
    assert L._ud_first_person_possessive_repair(d) is d


# ── Target 3: the HARD-LINE ladder guard no longer fails closed on every Italian node ─────────

@needs_it
def test_hardline_guard_admits_grown_types_and_refuses_names():
    rows = {r["surface"]: r for r in _run(
        "guard", "cane>animale", "barca>veicolo", "Krellin", "Halifax",
        "marco", "xqzt", "blorf", "iphone", "luca@Person>persona")}
    # a node the grown ontology already holds as a TYPE gets its rung (fresh-domain control: barca)
    assert rows["cane>animale"]["refuse"] is False, rows
    assert rows["barca>veicolo"]["refuse"] is False, rows
    # names — capitalised or typed in lowercase — never ladder on the language-neutral path
    for name in ("Krellin", "Halifax", "marco", "xqzt", "blorf", "iphone"):
        assert rows[name]["refuse"] is True, (name, rows)
    # an ingest-typed named referent is refused even if something grew a rung on it
    assert rows["luca@Person>persona"]["refuse"] is True, rows


def test_hardline_fallback_is_inert_on_an_english_install(monkeypatch):
    """English with the lexicon missing keeps the documented fail-closed refusal."""
    from src.api import hardline_guard as hg
    from src.api import wordnet_ladder as wl
    monkeypatch.delenv("FAULTLINE_LANGUAGE", raising=False)
    monkeypatch.setattr(wl, "has_common_noun_sense", lambda _s: None)
    called = []
    monkeypatch.setattr(hg, "_language_neutral_common_type",
                        lambda *a, **k: called.append(a) or True)
    assert hg._surface_is_common_type("dog") is False
    assert not called


def test_hardline_fallback_consulted_only_when_lexicon_has_no_answer(monkeypatch):
    from src.api import hardline_guard as hg
    from src.api import wordnet_ladder as wl
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "it")
    monkeypatch.setattr(hg, "_language_neutral_common_type", lambda *a, **k: True)
    monkeypatch.setattr(wl, "has_common_noun_sense", lambda _s: False)
    assert hg._surface_is_common_type("x") is False      # a lexical verdict stands
    monkeypatch.setattr(wl, "has_common_noun_sense", lambda _s: None)
    assert hg._surface_is_common_type("x") is True       # no verdict -> fallback decides


# ── Target 5: measures on the UD parse (copular dimension adjective, avere + age, measure verb) ─

@needs_it
def test_dimension_adjective_names_the_scalar():
    (f,) = _facts("La corda è lunga 4 metri")
    assert _has(f, "corda", "length", "4 metri"), f


@needs_it
def test_fresh_domain_control_dimension():
    (f,) = _facts("Il ponte è lungo 300 metri")
    assert _has(f, "ponte", "length", "300 metri"), f


@needs_it
def test_height_on_a_possessed_kin_role():
    (f,) = _facts("Mia figlia è alta 120 centimetri")
    assert _has(f, "figlia", "height", "120 centimetri") and _has(f, "figlia", "child_of", "user"), f


@needs_it
def test_measure_verb_duration():
    (f,) = _facts("Il film dura 2 ore")
    assert _has(f, "film", "duration", "2 ore"), f


@needs_it
def test_avere_age_is_a_scalar_not_a_possession():
    f1, f2 = _facts("Ho 34 anni", "mia sorella ha 30 anni")
    assert _has(f1, "user", "age", "34"), f1
    assert _has(f2, "sorella", "age", "30"), f2
    assert not any(x["rel_type"] in ("avere", "avere_anni", "instance_of") for x in f2), f2


@needs_it
def test_negated_age_files_nothing():
    (f,) = _facts("Mio figlio non ha 12 anni")
    assert not any(x["rel_type"] in ("age", "avere", "avere_anni") for x in f), f


# ── The naming-frame licence, pinned directly (the parse always routes a clitic-less name to
#    xcomp, which the complement reader also gates — so the licence needs its own pin) ──────────

class _UDDoc:
    lang_ = "it"


class _UTok:
    def __init__(self, dep, pos="X", morph=None, children=()):
        self.dep_, self.pos_ = dep, pos
        self.morph = _Morph(morph or {})
        self.children = list(children)
        self.doc = _UDDoc()
        self.tag_ = ""


def test_naming_frame_needs_a_person_agreeing_reflexive_clitic():
    from src.extraction import linguistics as L
    name = _UTok("obj", "PROPN")
    # "chiama Anna" — no clitic: plain calling, never a naming frame
    assert not L._naming_frame_licensed(_UTok("ROOT", "VERB", {"Person": "3"}, [name]))
    # "si chiama Anna" — 3rd-person expl clitic on a 3rd-person verb: naming
    si = _UTok("expl", "PRON", {"Clitic": "Yes", "Person": "3"})
    assert L._naming_frame_licensed(_UTok("ROOT", "VERB", {"Person": "3"}, [si, name]))
    # "Mi chiama Luca" — 1st-person clitic on a 3rd-person verb: Luca calls me
    mi = _UTok("expl", "PRON", {"Clitic": "Yes", "Person": "1"})
    assert not L._naming_frame_licensed(_UTok("ROOT", "VERB", {"Person": "3"}, [mi, name]))
    # "Mi chiamo Marco" — agreeing 1st person: naming
    assert L._naming_frame_licensed(_UTok("ROOT", "VERB", {"Person": "1"}, [mi, name]))



@needs_it
def test_unit_with_a_di_complement_is_not_a_property_of_the_owner():
    f1, f2, f3 = _facts("Ho 10 anni di esperienza", "Ho 2 chili di farina", "Ho 3 ore di tempo")
    assert not any(x["rel_type"] == "age" for x in f1), f1
    assert not any(x["rel_type"] == "weight" for x in f2), f2
    assert not any(x["rel_type"] == "duration" for x in f3), f3


@needs_it
def test_measure_verb_names_the_dimension():
    (f,) = _facts("Il corso dura 3 anni")
    assert _has(f, "corso", "duration", "3 anni"), f
    assert not any(x["rel_type"] == "age" for x in f), f


@needs_it
def test_dimension_row_applies_only_to_a_fitting_unit():
    (f,) = _facts("Il film è lungo 2 ore")
    assert not any(x["rel_type"] == "length" for x in f), f


@needs_it
def test_past_subordinate_measure_is_not_the_current_age():
    (f,) = _facts("Quando avevo 20 anni vivevo a Roma")
    assert not any(x["rel_type"] == "age" for x in f), f

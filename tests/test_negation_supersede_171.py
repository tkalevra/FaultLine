"""#171 (and #139): a negated edge retires only what it DENIES.

"No, I don't have a parrot." used to retire every has_pet row and "I don't own a boat." every owns
row: `_decide_supersede_or_coexist`'s cessation branch returned supersede_existing for any live row
on the slot, and `_soft_supersede_conflicting_fact` then archived every OTHER object. A denial names
no replacement value, so it may only retire the same object (the upsert flips that row's polarity)
or an instance of the denied type the turn identifies (stored instance_of + rel cardinality).

Decider pins are pure. E2E pins drive the real /harvest-spans → /ingest on a throwaway ``*_test``
POSTGRES_DSN seat with production flags (module-scoped, never module-level os.environ — #63).
"""
from __future__ import annotations

import pytest

import src.api.main as M


# ── decider (pure) ──────────────────────────────────────────────────────────────────────────
_NEG_USER = {"fact_provenance": "user_stated", "is_correction": False, "polarity": "negated"}


@pytest.mark.parametrize("tc", ["state", "event", "immutable"])
def test_negation_never_retires_an_undenied_other_value(tc):
    """Another object on the slot is not in conflict with a denial — on any temporal class
    (immutable included: its value-replacement rule must never be read off a denial)."""
    out = M._decide_supersede_or_coexist(_NEG_USER, {"fact_provenance": "user_stated"},
                                         {"temporal_class": tc})
    assert out == "coexist", out


def test_negation_retires_the_row_it_denies():
    out = M._decide_supersede_or_coexist(
        _NEG_USER, {"fact_provenance": "user_stated", "denied_by_incoming": True},
        {"temporal_class": "state"})
    assert out == "supersede_existing"


def test_engine_negation_never_retires_and_the_firewall_still_drops():
    eng = dict(_NEG_USER, fact_provenance="llm_inferred")
    assert M._decide_supersede_or_coexist(
        eng, {"fact_provenance": "user_stated", "denied_by_incoming": True},
        {"temporal_class": "state"}) == "drop_incoming"
    assert M._decide_supersede_or_coexist(
        eng, {"fact_provenance": "llm_inferred", "denied_by_incoming": True},
        {"temporal_class": "state"}) == "coexist"


# ── denied-NP reading (pure grammar, #183/#184) ─────────────────────────────────────────────
@pytest.mark.parametrize("sent,surf,want", [
    ("I don't have a cat anymore.", "cat", "all"),
    ("I don't have any cats anymore.", "cat", "all"),
    ("I don't have cats anymore.", "cat", "all"),
    ("I don't have the cats anymore.", "cat", "all"),
    ("I don't have the cat anymore.", "cat", "one"),
    ("I don't have my cat anymore.", "cat", "one"),
    ("I don't have that cat anymore.", "cat", "one"),
    ("I don't have Rex anymore.", "rex", "named"),
    ("I don't work at a bank anymore.", "bank", "all"),          # fresh domain
    ("I don't work at the bank anymore.", "bank", "one"),
    ("I don't have a pet that sheds.", "pet", "restricted"),            # #190
    ("I don't work at a bank that charges fees.", "bank", "restricted"),
    ("I don't own a red car.", "car", "restricted"),
])
def test_denied_np_reading(sent, surf, want):
    pytest.importorskip("spacy")
    reading, surface = M._denied_np_reading(sent, [surf])
    assert (reading, surface) == (want, surf), (sent, reading, surface)


@pytest.mark.parametrize("sent,surfaces,want", [
    ("I don't have cats anymore.", ["cats"], ("all", "cat")),          # #192: lemma, not surface
    ("I don't work at banks anymore.", ["banks"], ("all", "bank")),
])
def test_denied_np_reading_resolves_the_lemma(sent, surfaces, want):
    pytest.importorskip("spacy")
    assert M._denied_np_reading(sent, surfaces) == want


# ── E2E (fresh *_test seat per pin) ─────────────────────────────────────────────────────────
pytest.importorskip("spacy")
from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_pets = e2e_fixture()
e2e_owns = e2e_fixture()
e2e_same = e2e_fixture()
e2e_lima = e2e_fixture()
e2e_fresh = e2e_fixture()
e2e_toronto = e2e_fixture()
e2e_named = e2e_fixture()
e2e_twodogs = e2e_fixture()


def _slot(env, rel=None):
    """Every row on the user's slots: (rel, object alias set, polarity, live)."""
    rs = rows(env["schema"], """
        SELECT f.rel_type, (SELECT string_agg(lower(alias), '|') FROM entity_aliases
                             WHERE entity_id = f.object_id),
               f.polarity, f.superseded_at IS NULL
          FROM facts f WHERE f.subject_id = %s
        UNION ALL
        SELECT s.rel_type, (SELECT string_agg(lower(alias), '|') FROM entity_aliases
                             WHERE entity_id = s.object_id),
               s.polarity, (s.deleted_at IS NULL AND s.expires_at > now())
          FROM staged_facts s WHERE s.subject_id = %s""", (env["user_id"], env["user_id"]))
    return [(r, set((o or "").split("|")), p, live) for r, o, p, live in rs
            if rel is None or r == rel]


def _live_affirmed(env, name):
    return [r for r in _slot(env) if name in r[1] and r[2] == "affirmed" and r[3]
            and r[0] != "instance_of"]


def test_denying_a_parrot_keeps_both_named_pets(e2e_pets):
    state(e2e_pets, "My dog Quillon is 4 years old.")
    state(e2e_pets, "My cat Mango is 2 years old.")
    assert _live_affirmed(e2e_pets, "quillon") and _live_affirmed(e2e_pets, "mango"), _slot(e2e_pets)
    state(e2e_pets, "No, I don't have a parrot.")
    assert _live_affirmed(e2e_pets, "quillon"), _slot(e2e_pets)
    assert _live_affirmed(e2e_pets, "mango"), _slot(e2e_pets)


def test_denying_a_boat_keeps_the_car_and_the_bike(e2e_owns):
    state(e2e_owns, "I own a Honda.")
    state(e2e_owns, "I own a bicycle.")
    assert _live_affirmed(e2e_owns, "honda") and _live_affirmed(e2e_owns, "bicycle"), _slot(e2e_owns)
    state(e2e_owns, "I don't own a boat.")
    assert _live_affirmed(e2e_owns, "honda"), _slot(e2e_owns)
    assert _live_affirmed(e2e_owns, "bicycle"), _slot(e2e_owns)


def test_same_object_denial_retires_the_dog(e2e_same):
    state(e2e_same, "I have a dog.")
    assert _live_affirmed(e2e_same, "dog"), _slot(e2e_same)
    state(e2e_same, "I don't have a dog anymore.")
    assert not _live_affirmed(e2e_same, "dog"), _slot(e2e_same)


def test_single_valued_cessation_ends_lima(e2e_lima):
    state(e2e_lima, "I live in Lima.")
    assert _live_affirmed(e2e_lima, "lima"), _slot(e2e_lima)
    state(e2e_lima, "I don't live in Lima anymore.")
    assert not _live_affirmed(e2e_lima, "lima"), _slot(e2e_lima)


def test_fresh_domain_denial_keeps_the_collections(e2e_fresh):
    """Fresh-domain control: no pets, no vehicles, another rel — two collections, deny a third."""
    state(e2e_fresh, "I collect stamps.")
    state(e2e_fresh, "I collect coins.")
    assert _live_affirmed(e2e_fresh, "stamps") and _live_affirmed(e2e_fresh, "coins"), _slot(e2e_fresh)
    state(e2e_fresh, "I don't collect shells.")
    assert _live_affirmed(e2e_fresh, "stamps"), _slot(e2e_fresh)
    assert _live_affirmed(e2e_fresh, "coins"), _slot(e2e_fresh)


def test_single_valued_denial_of_another_city_keeps_the_current_one(e2e_toronto):
    """Single-valued cessation ends only the city it names: Toronto is not Lima."""
    state(e2e_toronto, "I live in Toronto.")
    state(e2e_toronto, "I don't live in Lima anymore.")
    assert _live_affirmed(e2e_toronto, "toronto"), _slot(e2e_toronto)
    r = e2e_toronto["client"].post("/query", json={"text": "Where do I live?",
                                                   "user_id": e2e_toronto["user_id"]}).json()
    prose = " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()
    assert "toronto" in prose and "used to live in toronto" not in prose, prose


def test_type_denial_retires_the_one_identified_pet(e2e_named):
    """Identity through the stored instance_of row: the only dog on file IS the dog denied."""
    state(e2e_named, "My dog Quillon is 4 years old.")
    assert _live_affirmed(e2e_named, "quillon"), _slot(e2e_named)
    state(e2e_named, "I don't have a dog anymore.")
    assert not [r for r in _live_affirmed(e2e_named, "quillon") if r[1] == {"quillon"}], \
        _slot(e2e_named)


def test_ambiguous_type_denial_retires_neither_dog(e2e_twodogs):
    """Two dogs on a multi-valued slot and a DEFINITE singular denial ("the dog"): the turn does
    not identify which — nothing is retired. (An indefinite "a dog" denies both — #183.)"""
    state(e2e_twodogs, "My dog Quillon is 4 years old.")
    state(e2e_twodogs, "My dog Biscuit is 6 years old.")
    state(e2e_twodogs, "I don't have the dog anymore.")
    assert [r for r in _live_affirmed(e2e_twodogs, "quillon") if r[1] == {"quillon"}], \
        _slot(e2e_twodogs)
    assert [r for r in _live_affirmed(e2e_twodogs, "biscuit") if r[1] == {"biscuit"}], \
        _slot(e2e_twodogs)

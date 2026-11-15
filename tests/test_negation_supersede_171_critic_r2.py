"""Critic round-2 pins for fix/negation-supersede-171 (e2677306).

1. RESTRICTIVE MODIFIER IGNORED (#171 shape again). "I don't have a pet that sheds." denies only
   shedding pets, but the slot-head-noun rule (`_find_denied_same_slot_facts`: denied surface ∈ the
   rel label's nouns → `found = list(slot)`) retires EVERY has_pet row. The type path has the same
   blindness: the denied NP's relative clause / PP restriction ("a bank that charges fees") is
   dropped and every instance of the head type is denied.
2. BARE PLURAL NOT RESOLVED TO ITS TYPE. "I don't have cats anymore." lands on a separate 'cats'
   entity; no instance is `instance_of cats`, so nothing is retired (base 30235608 retired both).
3. NAMED SUFFIX MATCH: "I don't know Smith anymore." retires both John Smith and Mary Smith.
4. LADDER AUTHORITY: a document-sourced subclass_of row licenses retiring a user-stated pet.

E2E on fresh *_test seats, production flags (laneb_e2e_support; module-scoped, never module-level
os.environ — #63). Each wound has a fresh-domain control.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")
from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_sheds = e2e_fixture()
e2e_feebank = e2e_fixture()
e2e_cats = e2e_fixture()
e2e_banks = e2e_fixture()
e2e_smith = e2e_fixture()
e2e_lopez = e2e_fixture()
e2e_ladder = e2e_fixture()


def _slot(env):
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
    return [(r, set((o or "").split("|")), p, live) for r, o, p, live in rs]


def _live_instance(env, name):
    return [r for r in _slot(env) if r[1] == {name} and r[2] == "affirmed" and r[3]
            and r[0] != "instance_of"]


# ── 1. a restricted denial must not wipe the slot ─────────────────────────────────────────────
def test_restricted_head_noun_denial_keeps_the_pets(e2e_sheds):
    state(e2e_sheds, "My cat Mango is 2 years old.")
    state(e2e_sheds, "My dog Fido is 3 years old.")
    assert _live_instance(e2e_sheds, "mango") and _live_instance(e2e_sheds, "fido"), _slot(e2e_sheds)
    state(e2e_sheds, "I don't have a pet that sheds.")
    assert _live_instance(e2e_sheds, "mango"), _slot(e2e_sheds)
    assert _live_instance(e2e_sheds, "fido"), _slot(e2e_sheds)


def test_fresh_domain_restricted_type_denial_keeps_the_employer(e2e_feebank):
    """Fresh-domain control (employment, type path): a restriction on the denied type."""
    state(e2e_feebank, "I work at Acme.")
    state(e2e_feebank, "Acme is a bank.")
    assert _live_instance(e2e_feebank, "acme"), _slot(e2e_feebank)
    state(e2e_feebank, "I don't work at a bank that charges fees.")
    assert _live_instance(e2e_feebank, "acme"), _slot(e2e_feebank)


# ── 2. a bare plural denial reaches its type ──────────────────────────────────────────────────
def test_bare_plural_denial_retires_both_cats(e2e_cats):
    state(e2e_cats, "My cat Mango is 2 years old.")
    state(e2e_cats, "My cat Luna is 4 years old.")
    assert _live_instance(e2e_cats, "mango") and _live_instance(e2e_cats, "luna"), _slot(e2e_cats)
    state(e2e_cats, "I don't have cats anymore.")
    assert not _live_instance(e2e_cats, "mango"), _slot(e2e_cats)
    assert not _live_instance(e2e_cats, "luna"), _slot(e2e_cats)


def test_surname_denial_does_not_retire_two_people(e2e_smith):
    """3. NAMED SUFFIX MATCH: the named path admits any slot object whose alias ENDS WITH the
    denied name (meant for the split 'dog rex' capture), so a bare surname retires every person
    who carries it — the #171 shape on the named path."""
    state(e2e_smith, "I know John Smith.")
    state(e2e_smith, "I know Mary Smith.")
    assert _live_instance(e2e_smith, "john smith") and _live_instance(e2e_smith, "mary smith"), \
        _slot(e2e_smith)
    state(e2e_smith, "I don't know Smith anymore.")
    assert _live_instance(e2e_smith, "john smith") or _live_instance(e2e_smith, "mary smith"), \
        _slot(e2e_smith)


def test_fresh_domain_surname_denial_does_not_retire_two_people(e2e_lopez):
    state(e2e_lopez, "I know Ana Lopez.")
    state(e2e_lopez, "I know Maria Lopez.")
    assert _live_instance(e2e_lopez, "ana lopez") and _live_instance(e2e_lopez, "maria lopez"), \
        _slot(e2e_lopez)
    state(e2e_lopez, "I don't know Lopez anymore.")
    assert _live_instance(e2e_lopez, "ana lopez") or _live_instance(e2e_lopez, "maria lopez"), \
        _slot(e2e_lopez)


def test_document_ladder_does_not_license_retiring_a_user_pet(e2e_ladder):
    """4. #185 via the LADDER: the authority gate ranks the instance_of row but walks subclass_of
    rows of any provenance, so a document-sourced (cat, subclass_of, parrot) still lets 'No, I
    don't have a parrot.' retire the user's cat."""
    from tests.laneb_e2e_support import ingest_edges
    state(e2e_ladder, "My cat Mango is 2 years old.")
    ingest_edges(e2e_ladder, "A cat is a parrot.",
                 [{"subject": "cat", "object": "parrot", "rel_type": "subclass_of"}],
                 source="document")
    assert _live_instance(e2e_ladder, "mango"), _slot(e2e_ladder)
    state(e2e_ladder, "No, I don't have a parrot.")
    assert _live_instance(e2e_ladder, "mango"), _slot(e2e_ladder)


def test_fresh_domain_bare_plural_denial_retires_both_banks(e2e_banks):
    state(e2e_banks, "I work at Acme.")
    state(e2e_banks, "Acme is a bank.")
    state(e2e_banks, "I work at Zenith.")
    state(e2e_banks, "Zenith is a bank.")
    assert _live_instance(e2e_banks, "acme") and _live_instance(e2e_banks, "zenith"), _slot(e2e_banks)
    state(e2e_banks, "I don't work at banks anymore.")
    assert not _live_instance(e2e_banks, "acme"), _slot(e2e_banks)
    assert not _live_instance(e2e_banks, "zenith"), _slot(e2e_banks)

"""Critic pins for fix/negation-supersede-171 (09810e0f).

The fix stops a denial retiring the slot's unrelated values (#171), but it derives "what the denial
names" too narrowly and from evidence of any authority:

  1. INDEFINITE denial = no instance at all. "I don't have a cat anymore." asserts NOT-EXISTS x
     (cat(x) and have(user, x)); with two cats on file both are denied. The fix retires an instance
     only when it is the UNIQUE match on a multi-valued slot, so both cats stay current and /query
     answers "you have a Cat named Mango / Luna" next to "You do not have a pet that is Cat".
     Base 30235608 retired both (correct outcome). Ambiguity is a property of a DEFINITE singular
     ("the cat"), readable from the parse (determiner / Definite morphology), not of the count.
  2. Denial of the slot's own head noun ("I don't have a pet anymore." with a cat on has_pet) and
     of a named instance captured under a different surface ("I don't have Rex anymore." after
     "My dog Rex ..." captured as 'dog rex') now retire nothing; base retired them.
  3. Identity evidence ignores authority: an llm_inferred (document / unattested) instance_of row
     ("Mango is a parrot.") licenses a user's "No, I don't have a parrot." to retire the
     user-stated cat Mango, whose user-stated type is cat. Pre-existing on base (base retired
     everything); the fix keeps it.

E2E on fresh *_test seats, production flags (module-scoped via laneb_e2e_support, never module-level
os.environ — #63). Each wound pin has a fresh-domain control.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")
from tests.laneb_e2e_support import e2e_fixture, rows, state  # noqa: E402

e2e_twocats = e2e_fixture()
e2e_twobanks = e2e_fixture()
e2e_petnoun = e2e_fixture()
e2e_rex = e2e_fixture()
e2e_docparrot = e2e_fixture()
e2e_userbank = e2e_fixture()


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
    """Live affirmed user row whose object IS the named instance (exact alias set {name}), so the
    welded type entity (#177: 'cat' aliases mango/luna) never counts as the instance."""
    return [r for r in _slot(env) if r[1] == {name} and r[2] == "affirmed" and r[3]
            and r[0] != "instance_of"]


def _live_containing(env, token):
    return [r for r in _slot(env) if any(token in a for a in r[1]) and r[2] == "affirmed"
            and r[3] and r[0] != "instance_of"]


def _prose(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()


# ── 1. indefinite denial retires every instance ────────────────────────────────────────────────
def test_indefinite_denial_retires_both_cats(e2e_twocats):
    state(e2e_twocats, "My cat Mango is 2 years old.")
    state(e2e_twocats, "My cat Luna is 4 years old.")
    assert _live_instance(e2e_twocats, "mango") and _live_instance(e2e_twocats, "luna"), \
        _slot(e2e_twocats)
    state(e2e_twocats, "I don't have a cat anymore.")
    assert not _live_instance(e2e_twocats, "mango"), _slot(e2e_twocats)
    assert not _live_instance(e2e_twocats, "luna"), _slot(e2e_twocats)
    prose = _prose(e2e_twocats, "What pets do I have?")
    assert "named mango" not in prose and "named luna" not in prose, prose


def test_fresh_domain_indefinite_denial_retires_both_banks(e2e_twobanks):
    """Fresh-domain control (employment, no pets): two employers that are both banks, then
    'I don't work at a bank anymore.' denies both."""
    state(e2e_twobanks, "I work at Acme.")
    state(e2e_twobanks, "Acme is a bank.")
    state(e2e_twobanks, "I work at Zenith.")
    state(e2e_twobanks, "Zenith is a bank.")
    assert _live_instance(e2e_twobanks, "acme") and _live_instance(e2e_twobanks, "zenith"), \
        _slot(e2e_twobanks)
    state(e2e_twobanks, "I don't work at a bank anymore.")
    assert not _live_instance(e2e_twobanks, "acme"), _slot(e2e_twobanks)
    assert not _live_instance(e2e_twobanks, "zenith"), _slot(e2e_twobanks)


# ── 2. head-noun and named denials ─────────────────────────────────────────────────────────────
def test_denying_the_slot_head_noun_retires_the_pet(e2e_petnoun):
    state(e2e_petnoun, "My cat Mango is 2 years old.")
    assert _live_instance(e2e_petnoun, "mango"), _slot(e2e_petnoun)
    state(e2e_petnoun, "I don't have a pet anymore.")
    assert not _live_instance(e2e_petnoun, "mango"), _slot(e2e_petnoun)
    assert "named mango" not in _prose(e2e_petnoun, "What pets do I have?")


def test_denying_a_named_pet_retires_it(e2e_rex):
    state(e2e_rex, "My dog Rex is 5 years old.")
    assert _live_containing(e2e_rex, "rex"), _slot(e2e_rex)
    state(e2e_rex, "I don't have Rex anymore.")
    assert not _live_containing(e2e_rex, "rex"), _slot(e2e_rex)


# ── 3. lower-authority identity must not license retirement ───────────────────────────────────
def test_document_classification_does_not_license_retiring_a_user_pet(e2e_docparrot):
    state(e2e_docparrot, "My cat Mango is 2 years old.")
    state(e2e_docparrot, "Mango is a parrot.", source="document")
    assert _live_instance(e2e_docparrot, "mango"), _slot(e2e_docparrot)
    state(e2e_docparrot, "No, I don't have a parrot.")
    assert _live_instance(e2e_docparrot, "mango"), _slot(e2e_docparrot)


def test_user_classification_still_licenses_retirement(e2e_userbank):
    """Control: the same shape with a USER-stated classification retires (identity is the user's)."""
    state(e2e_userbank, "I work at Acme.")
    state(e2e_userbank, "Acme is a bank.")
    assert _live_instance(e2e_userbank, "acme"), _slot(e2e_userbank)
    state(e2e_userbank, "I don't work at a bank anymore.")
    assert not _live_instance(e2e_userbank, "acme"), _slot(e2e_userbank)

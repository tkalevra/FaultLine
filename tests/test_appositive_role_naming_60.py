"""#60 — appositive naming: "My brother Pablo lives in Bilbao" filed no role alias; clause
predicates keyed on the role noun ("My sister Kate works at Google" → (sister, works_for, google)).

Fix (linguistics, UD appos): a PROPN in ``appos`` of a speaker-possessed kin/relational role noun is
that role's name —
  • _chain_named_instance's kin arm now files the same role-alias leg the naming frame files
    ((pablo, also_known_as, brother); SPINE_NAMING_CHAIN parity, speaker-possessed only);
  • the _emit chokepoint rebinds a subject keyed on such a role TOKEN to the appositive name, and
    the employment chain threads its subject token so works_for reaches it.

Rebind-regression probe (the kinship naming chain's history, cortex): a second, differently-named
role in the same seat does not steal or leak the first role's alias.
Fresh-domain control: "My sister Kate works at Google" / "My cousin Lena lives in Graz".
"""
from __future__ import annotations

import datetime

import pytest

pytest.importorskip("spacy")

from src.extraction import linguistics as L  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    ask, e2e_fixture, fact_rows, module_flags_fixture, rows, state,
)

_flags = module_flags_fixture()
e2e = e2e_fixture()
_REF = datetime.datetime(2026, 9, 29)


def _facts(s):
    return {(f.subject, f.rel_type, (f.object or "").lower()) for f in L.derive_sentence_facts(s, _REF)}


@pytest.mark.parametrize("text,name,role", [
    ("My brother Pablo lives in Bilbao.", "pablo", "brother"),
    ("My sister Kate works at Google.", "kate", "sister"),
])
def test_appositive_role_rides_instance_of_not_an_alias(text, name, role):
    # #96: the role is carried by instance_of + the kin edge — never a display-capable alias
    got = _facts(text)
    assert (name, "instance_of", role) in got, got
    assert (name, "also_known_as", role) not in got, got


@pytest.mark.parametrize("text,name,role,rel,obj", [
    ("My sister Kate works at Google.", "kate", "sister", "works_for", "google"),
    ("My brother Pablo lives in Bilbao.", "pablo", "brother", "live_in", "bilbao"),
])
def test_clause_predicate_keys_on_the_name(text, name, role, rel, obj):
    got = _facts(text)
    assert (role, rel, obj) not in got, f"#60: predicate keyed on the role noun: {got}"
    assert (name, rel, obj) in got, got


def test_unnamed_role_and_plain_subjects_unchanged():
    assert ("sister", "works_for", "google") in _facts("My sister works at Google.")
    assert ("user", "works_for", "google") in _facts("I work at Google.")


def _aliases_of(env, name):
    return {r[0] for r in rows(env["schema"], """
        SELECT a2.alias FROM entity_aliases a1 JOIN entity_aliases a2 ON a2.entity_id = a1.entity_id
         WHERE a1.alias = %s""", (name,))}


def test_e2e_role_alias_and_no_rebind(e2e):
    state(e2e, "My brother Pablo lives in Bilbao.")
    state(e2e, "My sister is named Kate.")
    state(e2e, "My cousin Lena lives in Graz.")              # fresh domain
    pablo, kate = _aliases_of(e2e, "pablo"), _aliases_of(e2e, "kate")
    assert "brother" not in pablo, f"#96: role alias on the appositive filler: {pablo}"
    assert "sister" not in pablo and "brother" not in kate, (pablo, kate)
    facts = fact_rows(e2e)
    assert ("pablo", "lives_in", "bilbao") in facts, sorted(facts)
    assert not any(s in ("brother", "cousin") for (s, _r, _o) in facts), sorted(facts)
    got = ask(e2e, "Where does my brother live?")
    assert any("bilbao" in str(o).lower() for (_s, _r, o) in got), got
    assert not any("graz" in str(o).lower() for (_s, _r, o) in got), got


# ═════════════════════════════════════════════════════════════════════════════
# #96 — the NAME is the display; a role is never the rendered name (pin the RENDERED answer)
# ═════════════════════════════════════════════════════════════════════════════
e2e96a = e2e_fixture()
e2e96b = e2e_fixture()


def _rendered(env, q):
    """The prose the query renders: every fact's ``definition`` plus subject/object as rendered."""
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    lines = []
    for f in (r.get("facts") or []):
        lines.append(" | ".join(str(f.get(k) or "") for k in ("subject", "rel_type", "object",
                                                             "definition")).lower())
    return lines


def _says_lives(lines, who, where):
    return any(who in ln.split(" | ")[0] and where in ln for ln in lines)


def test_e2e_96_two_brothers_render_their_names(e2e96a):
    state(e2e96a, "My brother Pablo lives in Bilbao.")
    state(e2e96a, "My brother Juan lives in Madrid.")
    lines = _rendered(e2e96a, "Where does Pablo live?")
    assert _says_lives(lines, "pablo", "bilbao"), f"#96: Pablo not rendered by name: {lines}"
    assert not any(ln.split(" | ")[0].strip() == "brother" for ln in lines), lines
    lines = _rendered(e2e96a, "Where does my brother live?")
    assert _says_lives(lines, "pablo", "bilbao") and _says_lives(lines, "juan", "madrid"), lines
    assert not any(ln.split(" | ")[0].strip() == "brother" for ln in lines), lines


def test_e2e_96_brother_and_sister_render_their_names(e2e96b):
    state(e2e96b, "My brother Pablo and my sister Ana live in Bilbao.")
    for who in ("pablo", "ana"):
        lines = _rendered(e2e96b, f"Where does {who.title()} live?")
        assert not any(ln.split(" | ")[0].strip() in ("brother", "sister") for ln in lines), \
            f"#96: a role rendered as {who}'s name: {lines}"

"""#67 — engine growth aliased a MODIFIER adjective to a relation ('favorite' → want, source=engine).

"What is my favorite color?" on a seat whose only relation row was a want-row: every lane missed,
the aspect-synonym lane (main.py::determine_path) offered each query keyword to the brain with the
anchor's held rels as candidates, and 'favorite' — an amod of the head noun 'color' — was grown
into ``rel_type_aliases (favorite → want)``. From then on every "my favorite X?" scoped to want.

Fix: only a word that HEADS something in the question (predicate / relational noun / predicative
ADJ) is offered for growth (``linguistics.aspect_growth_eligible``); a word seen only as an
amod/advmod dependent, or a closed-class token, is never grown. Plus a read-only census
(``junk_repair.census_modifier_grown_aliases``) listing grown aliases whose retained evidence shows
the surface only ever as a modifier.

Revert-red: base has neither helper and grows the modifier alias (E2E).
Fresh-domain control: "how HEAVY is my anvil" / "where have I TRAVELED" stay growable; "my
PREFERRED wrench", "which languages do I speak FLUENTLY" do not.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from src.extraction import linguistics as L  # noqa: E402
from tests.laneb_e2e_support import (  # noqa: E402
    dsn, e2e_fixture, ingest_edges, module_flags_fixture, rows,
)

_flags = module_flags_fixture()
e2e = e2e_fixture()


def _elig():
    fn = getattr(L, "aspect_growth_eligible", None)
    assert fn is not None, "#67: linguistics.aspect_growth_eligible missing (base behaviour)"
    return fn


@pytest.mark.parametrize("q,w,ok", [
    ("What is my favorite color?", "favorite", False),     # the #67 wound (amod)
    ("What is my favourite fruit?", "favourite", False),
    ("How tall am I?", "tall", True),                      # predicative ADJ — a head
    ("Where have I traveled?", "traveled", True),          # predicate
    ("What is my color?", "color", True),                  # the head noun itself
    # fresh domain
    ("How heavy is my anvil?", "heavy", True),
    ("What is my preferred wrench?", "preferred", False),
    ("Which languages do I speak fluently?", "fluently", False),
    ("What does every gauntlet need?", "every", False),    # closed class
])
def test_only_heads_are_growable(q, w, ok):
    assert _elig()(q, w) is ok, (q, w)


def test_modifier_evidence_counts():
    fn = getattr(L, "modifier_only_evidence", None)
    assert fn is not None, "#67: linguistics.modifier_only_evidence missing (base)"
    assert fn(["What is my favorite color?", "My favorite fruit is mango."], "favorite") == (2, 0)
    mod, head = fn(["How tall am I?"], "tall")
    assert head >= 1 and mod == 0


# ═════════════════════════════════════════════════════════════════════════════
# E2E — the real /query aspect lane on a throwaway tenant, brain stubbed to the #67 answer
# ═════════════════════════════════════════════════════════════════════════════

def _grown(env, alias):
    return rows(env["schema"], "SELECT canonical_rel_type, source FROM rel_type_aliases "
                               "WHERE alias = %s", (alias,))


def test_e2e_modifier_is_never_grown_into_a_rel_alias(e2e):
    M = e2e["M"]
    ingest_edges(e2e, "I want a first-person shooter.",
                 [{"subject": "user", "object": "first-person shooter", "rel_type": "want"}])
    asked = []

    def _brain(word, candidates):
        asked.append(word)
        names = [c[0] for c in candidates]
        return ("want", 0.95, True) if "want" in names else (None, 0.0, True)

    mp = pytest.MonkeyPatch()
    mp.setattr(M, "_aspect_map_via_llm", _brain)
    try:
        e2e["client"].post("/query", json={"text": "What is my favorite color?",
                                           "user_id": e2e["user_id"]})
    finally:
        mp.undo()
    assert "favorite" not in asked, f"#67: modifier offered for growth: {asked}"
    assert not _grown(e2e, "favorite"), "#67: favorite grown into a rel alias"


def test_e2e_head_word_still_grows(e2e):
    """Control: a predicate word the anchor's rel does not name is still offered and grown."""
    M = e2e["M"]
    ingest_edges(e2e, "I visited Lisbon.",
                 [{"subject": "user", "object": "lisbon", "rel_type": "visited"}])
    asked = []

    def _brain(word, candidates):
        asked.append(word)
        names = [c[0] for c in candidates]
        return ("visited", 0.95, True) if word == "roamed" and "visited" in names \
            else (None, 0.0, True)

    mp = pytest.MonkeyPatch()
    mp.setattr(M, "_aspect_map_via_llm", _brain)
    try:
        e2e["client"].post("/query", json={"text": "Where have I roamed?",
                                           "user_id": e2e["user_id"]})
    finally:
        mp.undo()
    assert "roamed" in asked, asked


def test_e2e_census_lists_the_modifier_alias(e2e):
    import psycopg2
    from src.api import junk_repair
    fn = getattr(junk_repair, "census_modifier_grown_aliases", None)
    assert fn is not None, "#67: junk_repair.census_modifier_grown_aliases missing (base)"
    schema, uid = e2e["schema"], e2e["user_id"]
    rows(schema, "INSERT INTO rel_type_aliases (alias, canonical_rel_type, source) VALUES "
                 "('fave', 'want', 'engine'), ('tall', 'height', 'engine') "
                 "ON CONFLICT (alias) DO NOTHING")
    rows(schema, "INSERT INTO episodic_log (user_id, raw_text, source) VALUES "
                 "(%s, 'What is my fave color?', 'mcp'), (%s, 'How tall am I?', 'mcp')",
         (uid, uid))
    conn = psycopg2.connect(dsn())
    try:
        got = {r["alias"]: r["verdict"] for r in fn(conn, schema)}
    finally:
        conn.close()
    assert got.get("fave") == "suspect", got
    assert "tall" not in got, got

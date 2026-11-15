"""Issue #77 (critic of fix/anchor-precedence-61-50-49 @ d49a447c): one pin per resolve_anchor
layer, each designed so that removing THAT layer alone turns it red while every sibling layer is
still present (the E2E pins cover the layers together, so single mutations survived there).

Layers: M4 question-punctuation strip (Rule 2), M5 rel_type_direct alias probe (Rule 2),
M7 Rule 2.4 whole-NP entity, M8 Rule 2.4 user-held rel, M10 Rule 2.5 entity guard,
M11 Rule 3 head-of-possessed-NP refusal. (M13 mirror decline and M14 measure exclusion are pinned
on a real seat in tests/test_anchor_precedence_61_50_49.py — they live in determine_path.)

The DB is a tiny fake that answers the two raw reads resolve_anchor makes itself (alias probe,
user attributes); every helper whose own SQL is not under test is monkeypatched to a dict lookup,
and the spaCy-backed readers are patched to the parse each case asserts. No domain literal is in
the code under test — the words here are fixtures.
"""
import pytest

import src.api.main as M

USER = "u-0000"


class _Cur:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()
        p = params or ()
        if "from entity_attributes where entity_id = %s" in s and "attribute = any" not in s:
            self.rows = [(a,) for a in self.db.attrs]
        elif "from entity_aliases where alias = %s" in s:
            hit = self.db.aliases.get(str(p[0]).lower()) if p else None
            self.rows = [(hit,)] if hit else []
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _DB:
    def __init__(self, aliases=None, attrs=(), owned=(), held_rels=None, grounded=True):
        self.aliases = dict(aliases or {})
        self.attrs = list(attrs)
        self.owned = set(owned)
        self.held_rels = dict(held_rels or {})
        self.grounded = grounded

    def cursor(self):
        return _Cur(self)


@pytest.fixture
def wire(monkeypatch):
    def _wire(db, nps, degree=None, semantic=None):
        semantic = semantic or {}
        monkeypatch.setattr(M, "_first_person_possessed_nps", lambda q: list(nps))
        monkeypatch.setattr(M, "_measure_interrogative_degree", lambda q: degree)
        monkeypatch.setattr(M, "_query_has_first_person_possessive", lambda q: True)
        monkeypatch.setattr(M, "_entity_is_l4_place", lambda d, e: False)
        monkeypatch.setattr(M, "resolve_entity_synonym", lambda *a, **k: None)
        monkeypatch.setattr(M, "_resolve_possessive_rel_target", lambda *a, **k: None)
        monkeypatch.setattr(M, "_query_has_hierarchy_intent", lambda q: False)
        monkeypatch.setattr(M, "_get_semantic_word_mappings",
                            lambda d, w: semantic.get(str(w).lower(), {"category": "unknown"}))
        monkeypatch.setattr(
            M, "_entity_named_exactly",
            lambda d, w, exclude=None: (lambda e: e if e and e != exclude else None)(
                db.aliases.get(str(w).strip().lower())))
        monkeypatch.setattr(M, "_entity_owned_by", lambda d, e, u: e in db.owned)
        monkeypatch.setattr(M, "_user_holds_rel_named",
                            lambda d, u, np: db.held_rels.get(np))
        monkeypatch.setattr(M, "_possessed_np_aspect_grounded",
                            lambda d, u, np: db.grounded)
        return db
    return _wire


def _run(q, db):
    out = {}
    anchor = M.resolve_anchor(q, [], USER, db, resolution_out=out)
    return anchor, out


def test_m4_rule2_strips_question_punctuation(wire):
    """Rule 2 (possessive entity) itself answers "my kayak?" — Rule 3 would also find it, but only
    Rule 2's result carries no matched_term. Without the strip Rule 2 probes 'kayak?' and misses."""
    db = wire(_DB(aliases={"kayak": "e-kayak"}), nps=["kayak"], degree="long")
    anchor, out = _run("How long is my kayak?", db)
    assert anchor == "e-kayak", out
    assert out.get("method") == "alias" and "matched_term" not in out, out


def test_m5_rule2_probes_a_word_that_is_also_a_rel_type(wire):
    """A pre-#36 junk rel named 'kayak' makes the word category rel_type_direct; Rule 2 must still
    probe the alias (no matched_term = Rule 2, not Rule 3)."""
    db = wire(_DB(aliases={"kayak": "e-kayak"}), nps=["kayak"], degree="long",
              semantic={"kayak": {"category": "rel_type_direct", "rel_type": "kayak"}})
    anchor, out = _run("How long is my kayak", db)
    assert anchor == "e-kayak", out
    assert out.get("method") == "alias" and "matched_term" not in out, out


def test_m7_rule24_whole_np_entity_beats_a_junk_head_scalar(wire):
    """'my pottery kiln' names a multi-word entity; a junk user scalar 'kiln' (its head, unowned
    entity-less) would pull Rule 2.5 onto the user. Only Rule 2.4 anchors the kiln first."""
    db = wire(_DB(aliases={"pottery kiln": "e-kiln"}, attrs=["kiln"]),
              nps=["pottery kiln"], degree="wide")
    anchor, out = _run("How wide is my pottery kiln?", db)
    assert anchor == "e-kiln", out
    assert out.get("matched_term") == "pottery kiln", out


def test_m8_rule24_user_held_rel_anchors_the_user(wire):
    """The user holds favorite_color; without Rule 2.4 the question falls to the default lane
    (method 'default'), not the self-rel reading."""
    db = wire(_DB(aliases={"color": "e-color"},
                  held_rels={"favorite color": ("favorite_color", "rel")}),
              nps=["favorite color"])
    anchor, out = _run("What is my favorite color?", db)
    assert anchor == USER, out
    assert out.get("method") == "self_rel", out


def test_m10_rule25_owned_entity_outranks_junk_user_scalar(wire):
    """'my red kayak': Rule 2 probes 'red' (miss), no NP entity/rel, the junk user scalar 'kayak'
    would anchor the user — the guard drops it because the user OWNS the entity 'kayak', and
    Rule 3 then binds the kayak."""
    db = wire(_DB(aliases={"kayak": "e-kayak"}, attrs=["kayak"], owned={"e-kayak"}),
              nps=["red kayak"], degree="long")
    anchor, out = _run("How long is my red kayak?", db)
    assert anchor == "e-kayak", out


def test_m10_control_genuine_user_scalar_still_anchors_the_user(wire):
    db = wire(_DB(attrs=["daily_commute"]), nps=["daily commute"])
    anchor, out = _run("how long is my daily commute", db)
    assert anchor == USER and out.get("method") == "self_scalar", out


def test_m11_rule3_refuses_an_unowned_head_of_the_possessed_np(wire):
    """No favorite_color held; the junk node 'color' shares only the NP head. It must not anchor."""
    db = wire(_DB(aliases={"color": "e-color"}, grounded=False), nps=["favorite color"])
    anchor, out = _run("What is my favorite color?", db)
    assert anchor == USER, out
    assert out.get("absent_np") == "favorite color", out


def test_m11_control_owned_head_entity_still_anchors(wire):
    db = wire(_DB(aliases={"kayak": "e-kayak"}, owned={"e-kayak"}), nps=["red kayak"])
    anchor, out = _run("Tell me about my red kayak", db)
    assert anchor == "e-kayak", out


def test_compound_question_abstains_only_if_every_np_is_ungrounded(wire):
    """#77 item 2: one grounded NP in a compound question clears the absent-aspect marker."""
    db = wire(_DB(held_rels={"favorite fruit": ("favorite_fruit", "scalar")}, grounded=False),
              nps=["favorite sport", "favorite fruit"])
    _anchor, out = _run("What is my favorite sport and my favorite fruit?", db)
    assert "absent_np" not in out, out

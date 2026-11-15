"""Behavioral tests for possessive-RELATIONSHIP anchor resolution (PIECE A) and the
walk-up-on-miss seam (PIECE B helpers), plus the apostrophe-suffix stripper.

CONTRACT UNDER TEST
-------------------
PIECE A — "what is my mother's age" resolves the anchor "my mother" to the entity
that FILLS that relationship from the owner:
  • role-noun → canonical rel_type(s) via the PER-TENANT rel_type_aliases table
    (read UNQUALIFIED — NO `public.` qualification anywhere in the issued SQL),
  • the relationship PAIR is completed from rel_types.inverse_rel_type (metadata,
    NOT hardcoded — parent_of↔child_of),
  • walk the edge BOTH storage directions → the single filler UUID,
  • unknown word / no filler / >1 filler → None (DEFER, never guess).

SUBJECT-AGNOSTIC: opaque tokens + synthetic UUIDs only, no hardcoded relation list.
These are fake-DB behavioral tests modelling exactly the query shapes the helpers
touch; they assert NO query is public-qualified (the per-tenant hard rule).
"""

import pytest

import src.api.main as main


# --------------------------------------------------------------------------- #
# Fake DB — recognizes the helper query set by SQL fragment.
# --------------------------------------------------------------------------- #

USER = "00000000-0000-0000-0000-000000000001"
HELEN = "00000000-0000-0000-0000-0000000000he"  # mother
JOEL = "00000000-0000-0000-0000-00000000j0el"
BOSS = "00000000-0000-0000-0000-0000000b0555"


class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        # HARD per-tenant assertion: the resolution path must NEVER qualify to public.
        assert "public." not in s, f"runtime read is public-qualified: {s!r}"
        self._db.queries.append((s, params))

        if "FROM rel_type_aliases a JOIN rel_types r" in s:
            alias = params[0]
            self._result = list(self._db.alias_rows.get(alias, []))
        elif "SELECT DISTINCT object_id FROM facts" in s:
            owner, rels = params[0], set(params[1])
            self._result = [(o,) for (subj, rt, o) in self._db.facts
                            if subj == owner and rt in rels]
        elif "SELECT DISTINCT subject_id FROM facts" in s:
            owner, rels = params[0], set(params[1])
            self._result = [(subj,) for (subj, rt, o) in self._db.facts
                            if o == owner and rt in rels]
        elif "FROM staged_facts" in s:
            self._result = []  # no staged facts in these fixtures
        elif "FROM entity_aliases WHERE alias" in s:
            alias = params[0]
            eid = self._db.aliases.get(alias)
            self._result = [(eid,)] if eid else []
        else:
            raise AssertionError(f"unexpected SQL hit fake cursor: {s!r}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeDB:
    def __init__(self, alias_rows=None, facts=None, aliases=None):
        # alias_rows: {alias: [(canonical_rel_type, requires_inversion, inverse_rel_type)]}
        self.alias_rows = alias_rows or {}
        # facts: list of (subject_id, rel_type, object_id)
        self.facts = facts or []
        # aliases: {alias_text: entity_uuid}
        self.aliases = aliases or {}
        self.queries = []

    def cursor(self):
        return _FakeCursor(self)


# --------------------------------------------------------------------------- #
# _strip_possessive_suffix — strip 's only, never over-strip ("boss" stays)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("token,expected", [
    ("Joel's", "Joel"),
    ("Joel’s", "Joel"),     # curly apostrophe
    ("boss", "boss"),            # MUST NOT become "bos"
    ("mother", "mother"),
    ("mothers", "mothers"),      # plural, no apostrophe → untouched
    ("s", "s"),                  # too short to strip
])
def test_strip_possessive_suffix(token, expected):
    assert main._strip_possessive_suffix(token) == expected


# --------------------------------------------------------------------------- #
# _relword_to_rel_candidates — tenant-only word→rel mapping
# --------------------------------------------------------------------------- #

def test_relword_maps_to_canonical_and_carries_inverse():
    db = _FakeDB(alias_rows={
        "mother": [("child_of", True, "parent_of")],
    })
    out = main._relword_to_rel_candidates(db, "Mother")  # case-insensitive
    assert out == [{"rel_type": "child_of", "requires_inversion": True,
                    "inverse_rel_type": "parent_of"}]


def test_relword_unknown_returns_empty():
    db = _FakeDB(alias_rows={})
    assert main._relword_to_rel_candidates(db, "spaceship") == []


def test_relword_no_db_or_blank_returns_empty():
    assert main._relword_to_rel_candidates(None, "mother") == []
    assert main._relword_to_rel_candidates(_FakeDB(), "  ") == []


# --------------------------------------------------------------------------- #
# _resolve_possessive_rel_target — owner → filler over the metadata rel-pair
# --------------------------------------------------------------------------- #

def test_resolve_target_forward_storage_direction():
    # (user, child_of, helen) stored; "my mother" → helen.
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN)],
    )
    assert main._resolve_possessive_rel_target(db, USER, "mother") == HELEN


def test_resolve_target_inverse_storage_direction():
    # Stored as the inverse (helen, parent_of, user); both-direction walk + the
    # metadata inverse pair still finds helen for "my mother".
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(HELEN, "parent_of", USER)],
    )
    assert main._resolve_possessive_rel_target(db, USER, "mother") == HELEN


def test_resolve_target_no_filler_returns_none():
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[],  # user has no parent on file
    )
    assert main._resolve_possessive_rel_target(db, USER, "mother") is None


def test_resolve_target_ambiguous_returns_none():
    # Two distinct fillers over the same rel-pair → DEFER (never guess).
    other = "00000000-0000-0000-0000-0000000000ff"
    db = _FakeDB(
        alias_rows={"parent": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN), (USER, "child_of", other)],
    )
    assert main._resolve_possessive_rel_target(db, USER, "parent") is None


def test_resolve_target_unknown_word_returns_none():
    db = _FakeDB(alias_rows={}, facts=[(USER, "child_of", HELEN)])
    assert main._resolve_possessive_rel_target(db, USER, "spaceship") is None


# --------------------------------------------------------------------------- #
# resolve_anchor — Rule 3.5 fall-through (flag-gated)
# --------------------------------------------------------------------------- #

def _anchor(db, q, flag, monkeypatch):
    monkeypatch.setattr(main, "QUERY_POSSESSIVE_REL_ANCHOR", flag)
    meta = {}
    a = main.resolve_anchor(q, [], USER, db, resolution_out=meta)
    return a, meta


def test_resolve_anchor_my_mother_resolves_filler(monkeypatch):
    # "my" is a possessive keyword; "mother" is NOT a taxonomy word and NOT an
    # entity alias → falls through to Rule 3.5 → helen.
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN)],
        aliases={},  # "mother" is not a registered alias
    )
    # _get_semantic_word_mappings must report "mother" as non-taxonomy/unknown.
    monkeypatch.setattr(main, "_get_semantic_word_mappings",
                        lambda db, w: {"category": "unknown", "taxonomy": None})
    monkeypatch.setattr(main, "resolve_entity_synonym", lambda *a, **k: None)
    a, meta = _anchor(db, "what is my mother's age", True, monkeypatch)
    assert a == HELEN
    assert meta["method"] == "possessive_rel"  # NOT "synonym"/"alias" → no scope-deference


def test_resolve_anchor_apostrophe_owner_form(monkeypatch):
    # "Joel's boss" → owner Joel resolves by alias, then works_for edge → BOSS.
    db = _FakeDB(
        alias_rows={"boss": [("works_for", False, None)]},
        facts=[(JOEL, "works_for", BOSS)],
        aliases={"joel": JOEL},
    )
    monkeypatch.setattr(main, "_get_semantic_word_mappings",
                        lambda db, w: {"category": "unknown", "taxonomy": None})
    monkeypatch.setattr(main, "resolve_entity_synonym", lambda *a, **k: None)
    a, meta = _anchor(db, "who is Joel's boss", True, monkeypatch)
    assert a == BOSS
    assert meta["method"] == "possessive_rel"


def test_resolve_anchor_flag_off_is_today(monkeypatch):
    # Flag OFF → Rule 3.5 never runs → defaults to user (today's behavior).
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN)],
        aliases={},
    )
    monkeypatch.setattr(main, "_get_semantic_word_mappings",
                        lambda db, w: {"category": "unknown", "taxonomy": None})
    monkeypatch.setattr(main, "resolve_entity_synonym", lambda *a, **k: None)
    a, meta = _anchor(db, "what is my mother's age", False, monkeypatch)
    assert a == USER
    assert meta["method"] == "default"


def test_resolve_anchor_unknown_relword_defers_to_default(monkeypatch):
    # "my spaceship" — not taxonomy, not alias, not synonym, not a known role-noun.
    db = _FakeDB(alias_rows={}, facts=[], aliases={})
    monkeypatch.setattr(main, "_get_semantic_word_mappings",
                        lambda db, w: {"category": "unknown", "taxonomy": None})
    monkeypatch.setattr(main, "resolve_entity_synonym", lambda *a, **k: None)
    a, meta = _anchor(db, "what is my spaceship", True, monkeypatch)
    assert a == USER
    assert meta["method"] == "default"

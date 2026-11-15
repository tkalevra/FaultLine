"""Behavioral tests for the TYPED MULTI-HOP DETERMINISTIC WALK query resolver
(`resolve_typed_walk` + decomposition/terminal helpers in src/api/main.py).

CONTRACT UNDER TEST (DEV/DESIGN-query-scope-resolution.md,
DEV/dprompt-query-subject-anchored-hierarchy.md)
--------------------------------------------------------------------------------
A question resolves as an ORDERED CHAIN of TYPED STEPS, walked deterministically,
STOPPING the instant the asked terminal resolves:

  "what's my mother's age"
    my      → [self-reference]  → anchor = user (UUID)
    mother  → [relational hop]  → walk facts edge → helen (UUID)
    age     → [scalar terminal] → read helen's stored age → 67 → ANSWER → STOP

Hard rules asserted here:
  • Typed steps: entity / relational / scalar — typing from registry + rel_type
    METADATA (rel_type name, tail_types={SCALAR}, rel_type_aliases), no word-list.
  • Decompose off SYNTAX (possessive 's, "of", "my"); arbitrary chain length.
  • Stop on answer-found; read ONLY the asked terminal (never dump all attributes).
  • Climb the is-a hierarchy ONLY on a miss, bounded + visited-set.
  • Three outcomes (Rule 6): value / known-gap-defer / no-slot.
  • Branch ambiguity → DEFER, never guess (Rule 7).
  • Every hop lands on a UUID (Rule 8). No public-qualified read (per-tenant).

These are FAKE-DB behavioral tests (no live DB): the fake cursor recognizes the
walk's SQL by fragment and asserts NO query is public-qualified.
"""

import pytest

import src.api.main as main


USER = "00000000-0000-0000-0000-000000000001"
HELEN = "00000000-0000-0000-0000-00000000helen"  # user's mother
GRAN = "00000000-0000-0000-0000-000000000gran"   # helen's mother
HUMAN = "00000000-0000-0000-0000-00000000human"  # class node
JOEL = "00000000-0000-0000-0000-00000000j0elx"


# --------------------------------------------------------------------------- #
# Fake DB — recognizes the walk query set by SQL fragment.
# --------------------------------------------------------------------------- #

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
        # HARD per-tenant assertion: the walk must NEVER qualify to public.
        assert "public." not in s, f"runtime read is public-qualified: {s!r}"
        self._db.queries.append((s, params))

        if "SELECT canonical_rel_type FROM rel_type_aliases WHERE alias = %s" in s:
            # DOOR 1 compound-rel alias expansion (plain alias→canonical, no SCALAR join).
            alias = params[0]
            canon = self._db.compound_aliases.get(alias)
            self._result = [(canon,)] if canon else []
        elif "SELECT DISTINCT rel_type, object_id FROM facts" in s:
            # DOOR 1 relational compound-rel lookup on the anchor.
            owner, rels = params[0], set(params[1])
            self._result = [(rt, o) for (subj, rt, o) in self._db.facts
                            if subj == owner and rt in rels]
        elif "SELECT DISTINCT rel_type, object_id FROM staged_facts" in s:
            # DOOR 1 staged compound-rel lookup.
            owner, rels = params[0], set(params[1])
            self._result = [(rt, o) for (subj, rt, o) in self._db.staged
                            if subj == owner and rt in rels]
        elif ("SELECT attribute, value_text, value_int, value_float, value_date"
              in s and "FROM entity_attributes" in s):
            # DOOR 1 scalar compound-rel lookup (5-col, attribute first).
            owner, attrs = params[0], set(params[1])
            out = []
            for (o, a), val in self._db.entity_attrs.items():
                if o == owner and a in attrs:
                    out.append((a, val, None, None, None))
            self._result = out
        elif "FROM entity_aliases WHERE alias = %s" in s:
            alias = params[0]
            eid = self._db.aliases.get(alias)
            self._result = [(eid,)] if eid else []
        elif "FROM rel_type_aliases a JOIN rel_types r" in s and "'SCALAR' = ANY(r.tail_types)" in s:
            alias = params[0]
            canon = self._db.scalar_aliases.get(alias)
            self._result = [(canon,)] if canon else []
        elif "FROM entity_attributes" in s and "attribute = ANY(%s)" in s and "value_text" in s:
            owner, attrs = params[0], set(params[1])
            for (o, a), val in self._db.entity_attrs.items():
                if o == owner and a in attrs:
                    # value_text, value_int, value_float, value_date
                    self._result = [(val, None, None, None)]
                    return
            self._result = []
        elif "FROM rel_types WHERE rel_type = ANY(%s)" in s and "'SCALAR' = ANY(tail_types)" in s:
            attrs = set(params[0])
            self._result = [(1,)] if (attrs & self._db.scalar_rel_types) else []
        elif "WITH RECURSIVE chain" in s and "FROM entity_attributes" in s:
            # _class_schema_has_attribute: ancestor classes' attribute schema.
            owner, class_rels = params[0], set(params[1])
            attr_names = set(params[-1])
            ancestors = set()
            frontier = [owner]
            for _ in range(8):
                nxt = []
                for sub in frontier:
                    for (s2, rt, o2) in self._db.facts:
                        if s2 == sub and rt in class_rels:
                            if o2 not in ancestors:
                                ancestors.add(o2)
                                nxt.append(o2)
                frontier = nxt
            hit = any((anc, a) in self._db.entity_attrs
                      for anc in ancestors for a in attr_names)
            self._result = [(1,)] if hit else []
        elif "SELECT DISTINCT object_id FROM facts" in s:
            owner, rels = params[0], set(params[1])
            self._result = [(o,) for (subj, rt, o) in self._db.facts
                            if subj == owner and rt in rels]
        elif "SELECT DISTINCT subject_id FROM facts" in s:
            owner, rels = params[0], set(params[1])
            self._result = [(subj,) for (subj, rt, o) in self._db.facts
                            if o == owner and rt in rels]
        elif "FROM staged_facts" in s:
            self._result = []
        elif "FROM rel_type_aliases a JOIN rel_types r" in s:
            # _relword_to_rel_candidates
            alias = params[0]
            self._result = list(self._db.alias_rows.get(alias, []))
        elif "is_hierarchy_rel = true" in s and "wikidata_pid = ANY(%s)" in s:
            # _get_classification_rels(db_conn) — return the tenant's is-a rels.
            self._result = [(r,) for r in self._db.classification_rels]
        else:
            raise AssertionError(f"unexpected SQL hit fake cursor: {s!r}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeDB:
    def __init__(self, aliases=None, alias_rows=None, scalar_aliases=None,
                 entity_attrs=None, scalar_rel_types=None, facts=None,
                 classification_rels=None, compound_aliases=None, staged=None):
        self.aliases = aliases or {}            # alias → uuid
        self.alias_rows = alias_rows or {}      # relword → [(canon, req_inv, inverse)]
        self.scalar_aliases = scalar_aliases or {}  # alias → canonical scalar rel
        self.compound_aliases = compound_aliases or {}  # compound alias → canonical rel
        self.entity_attrs = entity_attrs or {}  # (uuid, attr) → value
        self.scalar_rel_types = set(scalar_rel_types or [])
        self.facts = facts or []                # [(subj, rel, obj)]
        self.staged = staged or []              # [(subj, rel, obj)] staged_facts
        self.classification_rels = classification_rels or ["instance_of", "subclass_of", "is_a"]
        self.queries = []

    def cursor(self):
        return _FakeCursor(self)


# --------------------------------------------------------------------------- #
# Decomposition — off SYNTAX, arbitrary chain length, defers non-chains.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("q,expected", [
    ("what's my mother's age", ["my", "mother", "age"]),
    ("what is helen's age", ["helen", "age"]),
    ("my mother's mother's age", ["my", "mother", "mother", "age"]),
    ("the age of my mother", ["my", "mother", "age"]),     # "of" inversion
    ("how old is helen", ["helen", "age"]),                # interrogative-attr
    ("how old is my mother", ["my", "mother", "age"]),
])
def test_decompose_chain_forms(q, expected):
    assert main._decompose_query_steps(q) == expected


@pytest.mark.parametrize("q", [
    "what is fraggle",              # bare classification → legacy
    "hello",                        # chitchat
    "where do I work",              # not a possessive + attr chain
    "",
])
def test_decompose_non_chain_defers(q):
    # Not even syntactically a possessive chain → None → caller runs legacy.
    assert main._decompose_query_steps(q) is None


def test_taxonomy_chain_resolver_defers_to_legacy():
    # "my family" IS a syntactic possessive chain, but "family" is a taxonomy
    # concept — neither a scalar attribute nor a role-noun. The decomposition is
    # purely syntactic (may yield ["my","family"]); the RESOLVER must return None
    # (no slot of any kind) so the legacy taxonomy/membership path handles it.
    db = _FakeDB()  # no aliases, no scalar rels, no rel_type_aliases for "family"
    res = main.resolve_typed_walk("tell me about my family", USER, db)
    assert res is None


# --------------------------------------------------------------------------- #
# Single-hop scalar — "what's my IP"
# --------------------------------------------------------------------------- #

def test_single_hop_scalar_user_ip():
    db = _FakeDB(
        scalar_aliases={"ip": "has_ip"},
        entity_attrs={(USER, "has_ip"): "10.0.0.5"},
        scalar_rel_types={"has_ip"},
    )
    res = main.resolve_typed_walk("what's my ip", USER, db)
    assert res["status"] == "answer"
    assert res["anchor"] == USER
    f = res["facts"][0]
    assert f["object"] == "10.0.0.5"
    assert f["rel_type"] == "has_ip"          # canonicalized via alias
    assert f["source"] == "attributes"
    # every hop landed on a UUID (subject is the user UUID)
    assert f["subject"] == USER


# --------------------------------------------------------------------------- #
# The canonical case — "what's my mother's age" (1 relational hop + scalar)
# --------------------------------------------------------------------------- #

def test_my_mothers_age():
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN)],     # (user, child_of, helen) → mother = helen
        entity_attrs={(HELEN, "age"): "67"},
        scalar_rel_types={"age"},
    )
    res = main.resolve_typed_walk("what's my mother's age", USER, db)
    assert res["status"] == "answer"
    assert res["anchor"] == HELEN              # every hop landed on a UUID
    f = res["facts"][0]
    assert f["subject"] == HELEN
    assert f["object"] == "67"
    assert f["rel_type"] == "age"


def test_how_old_is_helen():
    db = _FakeDB(
        aliases={"helen": HELEN},
        entity_attrs={(HELEN, "age"): "67"},
        scalar_rel_types={"age"},
    )
    res = main.resolve_typed_walk("how old is helen", USER, db)
    assert res["status"] == "answer"
    assert res["anchor"] == HELEN
    assert res["facts"][0]["object"] == "67"


# --------------------------------------------------------------------------- #
# 2 relational hops — "my mother's mother's age"
# --------------------------------------------------------------------------- #

def test_my_mothers_mothers_age():
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN), (HELEN, "child_of", GRAN)],
        entity_attrs={(GRAN, "age"): "92"},
        scalar_rel_types={"age"},
    )
    res = main.resolve_typed_walk("my mother's mother's age", USER, db)
    assert res["status"] == "answer"
    assert res["anchor"] == GRAN               # two relational hops, then scalar
    assert res["facts"][0]["object"] == "92"


# --------------------------------------------------------------------------- #
# Climb-on-miss — slot lives on the CLASS attribute schema (entity grounding)
# --------------------------------------------------------------------------- #

def test_climb_on_miss_class_schema_known_gap():
    # Helen has NO stored age; "age" is not a scalar rel_type by name in this
    # tenant; but Helen is_a human, and the HUMAN class node carries an "age"
    # attribute (its schema). Slot is VALID → KNOWN GAP (defer), not no-slot.
    db = _FakeDB(
        aliases={"helen": HELEN},
        facts=[(HELEN, "instance_of", HUMAN)],
        entity_attrs={(HUMAN, "age"): "<schema>"},   # the class HAS an age slot
        scalar_rel_types=set(),                       # "age" not a scalar rel by name
        classification_rels=["instance_of", "subclass_of", "is_a"],
    )
    res = main.resolve_typed_walk("what is helen's age", USER, db)
    assert res["status"] == "defer"
    assert "known gap" in res["reason"]
    assert res["anchor"] == HELEN


# --------------------------------------------------------------------------- #
# Three outcomes of Rule 6
# --------------------------------------------------------------------------- #

def test_rule6_value_present():
    db = _FakeDB(aliases={"helen": HELEN},
                 entity_attrs={(HELEN, "age"): "67"}, scalar_rel_types={"age"})
    res = main.resolve_typed_walk("what is helen's age", USER, db)
    assert res["status"] == "answer" and res["facts"][0]["object"] == "67"


def test_rule6_known_gap_defers():
    # Slot is valid (age is a scalar rel_type) but Helen has NO stored value.
    db = _FakeDB(aliases={"helen": HELEN},
                 entity_attrs={}, scalar_rel_types={"age"})
    res = main.resolve_typed_walk("what is helen's age", USER, db)
    assert res["status"] == "defer"
    assert "known gap" in res["reason"]


def test_rule6_no_slot_returns_none():
    # "favoritecolor" is not a scalar rel, not stored, not on any class schema →
    # no slot → walk returns None → caller falls through to the legacy path.
    db = _FakeDB(aliases={"helen": HELEN},
                 entity_attrs={}, scalar_rel_types={"age"},
                 facts=[])
    res = main.resolve_typed_walk("what is helen's favoritecolor", USER, db)
    assert res is None


# --------------------------------------------------------------------------- #
# Branch ambiguity → DEFER, never guess (Rule 7)
# --------------------------------------------------------------------------- #

def test_branch_ambiguity_defers():
    other = "00000000-0000-0000-0000-00000000oth3r"
    # user has TWO parents on file → "my mother" is ambiguous → DEFER.
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN), (USER, "child_of", other)],
        entity_attrs={(HELEN, "age"): "67", (other, "age"): "70"},
        scalar_rel_types={"age"},
    )
    res = main.resolve_typed_walk("what's my mother's age", USER, db)
    assert res["status"] == "defer"
    assert "unresolved relation" in res["reason"]


# --------------------------------------------------------------------------- #
# Relational chain miss → DEFER (no filler)
# --------------------------------------------------------------------------- #

def test_relational_chain_miss_defers():
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[],                              # user has no mother on file
        scalar_rel_types={"age"},
    )
    res = main.resolve_typed_walk("what's my mother's age", USER, db)
    assert res["status"] == "defer"


# --------------------------------------------------------------------------- #
# Termination / visited-set — climb is bounded, no infinite loop
# --------------------------------------------------------------------------- #

def test_climb_is_bounded_no_infinite_loop():
    # A pathological cyclic is-a chain (a is_a b, b is_a a). The class-schema
    # climb is depth-bounded by the CTE; the fake cursor also caps iterations.
    A = "00000000-0000-0000-0000-0000000000000a"
    B = "00000000-0000-0000-0000-0000000000000b"
    db = _FakeDB(
        aliases={"thinga": A},
        facts=[(A, "instance_of", B), (B, "instance_of", A)],
        entity_attrs={},                       # no age anywhere → no slot
        scalar_rel_types=set(),
    )
    # Must terminate and return None (no slot found), not hang.
    res = main.resolve_typed_walk("what is thinga's age", USER, db)
    assert res is None


# --------------------------------------------------------------------------- #
# Unknown owner entity → defer to legacy (None)
# --------------------------------------------------------------------------- #

def test_unknown_owner_entity_defers_to_legacy():
    db = _FakeDB(aliases={})  # "nobody" is not a registered alias
    res = main.resolve_typed_walk("what is nobody's age", USER, db)
    assert res is None


# --------------------------------------------------------------------------- #
# Fail-safe — bad inputs never raise
# --------------------------------------------------------------------------- #

def test_failsafe_inputs():
    assert main.resolve_typed_walk("", USER, _FakeDB()) is None
    assert main.resolve_typed_walk("what is helen's age", None, _FakeDB()) is None
    assert main.resolve_typed_walk("what is helen's age", USER, None) is None


# --------------------------------------------------------------------------- #
# Every issued SQL is per-tenant (no public-qualified read)
# --------------------------------------------------------------------------- #

def test_no_public_qualified_reads():
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN)],
        entity_attrs={(HELEN, "age"): "67"},
        scalar_rel_types={"age"},
    )
    main.resolve_typed_walk("what's my mother's age", USER, db)
    assert db.queries  # the fake cursor asserts no "public." per execute()
    for s, _ in db.queries:
        assert "public." not in s


# --------------------------------------------------------------------------- #
# DOOR 1 — COMPOUND-REL single-lookup (the return-to-sender fix)
# --------------------------------------------------------------------------- #

BLUE = "00000000-0000-0000-0000-0000000bluex"
SUSHI = "00000000-0000-0000-0000-000000sushix"
COFFEE = "00000000-0000-0000-0000-00000coffeex"


def test_door1_favorite_color_relational_edge():
    # "my favorite color" → favorite_color (a grown rel, object→UUID in facts).
    # The compound rel resolves as ONE (user, favorite_color, blue) lookup — it
    # must NOT shatter into hop "favorite" / terminal "color" (the bug).
    db = _FakeDB(facts=[(USER, "favorite_color", BLUE)])
    res = main.resolve_typed_walk("what is my favorite color", USER, db)
    assert res is not None and res["status"] == "answer"
    assert res["anchor"] == USER
    f = res["facts"][0]
    assert f["rel_type"] == "favorite_color"
    assert f["object"] == BLUE
    assert f["subject"] == USER


def test_door1_favourite_food_subject_agnostic():
    # Different compound, no colour patch — SUBJECT-AGNOSTIC (any <adj>_<noun> rel).
    db = _FakeDB(facts=[(USER, "favorite_food", SUSHI)])
    res = main.resolve_typed_walk("what's my favourite food", USER, db)
    # NB "favourite food" → favourite_food; the stored rel is the US-spelling
    # favorite_food, so Door 1 finds no live favourite_food rel → no false hit. The
    # walk then shatters and DEFERS (correct: Door 2 catches a genuine spelling miss
    # at the dispatch level, never a wrong answer here).
    assert res is None or res["status"] == "defer"

    # With the rel stored under the asked spelling it resolves.
    db2 = _FakeDB(facts=[(USER, "favourite_food", SUSHI)])
    res2 = main.resolve_typed_walk("what's my favourite food", USER, db2)
    assert res2 is not None and res2["status"] == "answer"
    assert res2["facts"][0]["rel_type"] == "favourite_food"
    assert res2["facts"][0]["object"] == SUSHI


def test_door1_favorite_drink_scalar_attribute():
    # Same construction but the value lives as a SCALAR attribute, not a facts edge.
    db = _FakeDB(entity_attrs={(USER, "favorite_drink"): "coffee"})
    res = main.resolve_typed_walk("what's my favorite drink", USER, db)
    assert res is not None and res["status"] == "answer"
    f = res["facts"][0]
    assert f["rel_type"] == "favorite_drink"
    assert f["object"] == "coffee"
    assert f["source"] == "attributes"


def test_door1_via_alias_canonicalization():
    # The asked compound aliases to a canonical rel (metadata-driven, no literal).
    db = _FakeDB(
        compound_aliases={"favorite_color": "preferred_colour"},
        facts=[(USER, "preferred_colour", BLUE)],
    )
    res = main.resolve_typed_walk("what is my favorite color", USER, db)
    assert res is not None and res["status"] == "answer"
    assert res["facts"][0]["rel_type"] == "preferred_colour"


def test_door1_staged_compound_rel():
    db = _FakeDB(staged=[(USER, "favorite_color", BLUE)])
    res = main.resolve_typed_walk("what is my favorite color", USER, db)
    assert res is not None and res["status"] == "answer"
    assert res["facts"][0]["object"] == BLUE


def test_door1_does_not_break_genuine_hop_chain():
    # "my mother's age" decomposes to ["my","mother","age"]; the compound tokens are
    # ["mother","age"] → mother_age, which is NOT a live rel → Door 1 returns None and
    # the genuine hop-chain walk resolves it normally.
    db = _FakeDB(
        alias_rows={"mother": [("child_of", True, "parent_of")]},
        facts=[(USER, "child_of", HELEN)],
        entity_attrs={(HELEN, "age"): "67"},
        scalar_rel_types={"age"},
    )
    res = main.resolve_typed_walk("what's my mother's age", USER, db)
    assert res is not None and res["status"] == "answer"
    assert res["facts"][0]["object"] == "67"
    assert res["facts"][0]["subject"] == HELEN   # walked to mother, not user


def test_door1_ambiguous_compound_defers():
    # Two distinct values for the same compound rel → ambiguity → Door 1 returns None
    # (never guesses); the walk proceeds (and will itself defer).
    db = _FakeDB(facts=[(USER, "favorite_color", BLUE), (USER, "favorite_color", SUSHI)])
    res = main.resolve_typed_walk("what is my favorite color", USER, db)
    # Door 1 declines; the hop path then shatters favorite/color and defers.
    assert res is None or res["status"] == "defer"


def test_door1_single_token_not_compound():
    # A bare single-token terminal ("my ip") is NOT a compound rel — Door 1 must not
    # fire; the ordinary scalar terminal path handles it.
    db = _FakeDB(
        scalar_aliases={"ip": "has_ip"},
        entity_attrs={(USER, "has_ip"): "10.0.0.5"},
        scalar_rel_types={"has_ip"},
    )
    res = main.resolve_typed_walk("what's my ip", USER, db)
    assert res is not None and res["status"] == "answer"
    assert res["facts"][0]["rel_type"] == "has_ip"  # via the normal scalar terminal

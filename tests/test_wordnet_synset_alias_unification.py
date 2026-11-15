"""Cross-synonym query-surface unification (bug SYNSET-UNIFY).

THE GAP: the WordNet ladder emits a hypernym node under its synset's PRIMARY lemma. Members
ladder UNDER that surface ("lemon subclass_of citrus"), but a user querying "how many CITRUS
FRUITS" grounds the surface "citrus fruit" — a DISTINCT entity that shares the WordNet SYNSET
with "citrus" but NOT a graph node. `_resolve_count_type_uuids` exact-alias-grounds "citrus
fruit", finds a different/absent node, and the count misses the members.

THE FIX (ingest-side, deterministic + offline): when the ladder emits a hypernym node,
`_attach_wordnet_hypernym_ladder` ALSO registers the node's WordNet co-synset lemmas
("citrus fruit"/"citrous fruit") as NON-preferred `also_known_as` aliases of the SAME node
UUID. WordNet synonymy = shared synset membership (Princeton WordNet). So both surfaces resolve
to one node and the existing count walk (unchanged) reaches the members. HARD LINE safe (TYPE-node
synonym, never a NAME); additive (never clobbers the node's display name).

Two levels are pinned here:
  A) INGEST WIRING — `_attach_wordnet_hypernym_ladder` calls `register_alias` with the node's
     real WordNet synonyms (uses the real `synset_synonyms`, fakes only the DB/registry plumbing).
  B) QUERY EFFECT — given the alias the fix registers, `_resolve_count_type_uuids("citrus fruits")`
     now grounds the SAME node and descends the members; WITHOUT it (pre-fix) the count misses.

Pure/offline: real WordNet corpus + fake cursor/registry. No network, no live services.
"""
import pytest

from src.api import main as m
from src.api import wordnet_ladder as W


def _has_corpus() -> bool:
    return W._wn() is not None


pytestmark = pytest.mark.skipif(not _has_corpus(), reason="WordNet corpus not available offline")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# A) INGEST WIRING — the ladder-stager registers the node's WordNet synonyms as aliases
# ══════════════════════════════════════════════════════════════════════════════════════════════

class _RecordingRegistry:
    """Deterministic UUID-by-surface resolver + records register_alias calls."""
    def __init__(self):
        self.alias_calls = []  # (canonical_uuid, alias, is_preferred)

    def resolve(self, user_id, name):
        return f"uuid::{(name or '').strip().lower()}"

    def register_alias(self, canonical, alias, is_preferred=False, **kw):
        self.alias_calls.append((canonical, alias, is_preferred))


class _NoNameCursor:
    """Cursor that answers the naming-edge HARD-LINE probe as 'not a name' (fetchone -> None)
    and no-ops everything else (SAVEPOINT/RELEASE/SET search_path)."""
    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _NoNameCursor()

    def rollback(self):
        return None


def test_ingest_registers_citrus_fruit_synonym_alias(monkeypatch):
    """Laddering a member ('grapefruit') whose chain passes through the 'citrus' node registers
    the node's WordNet synonym 'citrus fruit' as an alias of the SAME node UUID."""
    monkeypatch.setenv("L4_WORDNET_LADDER", "1")
    staged_rows = []
    # Stub the Class-C commit so the test needs no DB; capture the rungs for a sanity check.
    monkeypatch.setattr(m, "_commit_staged",
                        lambda conn, rows, tier, confidence=None: staged_rows.extend(rows))

    reg = _RecordingRegistry()
    n = m._attach_wordnet_hypernym_ladder(
        _FakeConn(), "faultline_test",
        concept_object_id="uuid::grapefruit", concept_display="grapefruit",
        object_type="object", registry=reg, user_id="u",
    )
    assert n > 0, "grapefruit must ladder (rungs staged)"

    # The 'citrus' node UUID (as the registry resolves it) gained the synonym alias 'citrus fruit'.
    citrus_uuid = reg.resolve("u", "citrus")
    aliased = {(c, a) for (c, a, _pref) in reg.alias_calls}
    assert (citrus_uuid, "citrus fruit") in aliased, (
        f"expected 'citrus fruit' registered as an alias of the citrus node; got {reg.alias_calls}")
    # Synonym aliases are NON-preferred (never clobber the node's display name).
    assert all(pref is False for (_c, _a, pref) in reg.alias_calls)
    # HARD LINE: the member/leaf surface 'grapefruit' is NEVER aliased as a synonym of a type node.
    assert not any(a == "grapefruit" for (_c, a, _p) in reg.alias_calls)


def test_ingest_no_synonym_alias_when_flag_off(monkeypatch):
    monkeypatch.setenv("L4_WORDNET_LADDER", "0")
    reg = _RecordingRegistry()
    n = m._attach_wordnet_hypernym_ladder(
        _FakeConn(), "faultline_test",
        concept_object_id="uuid::grapefruit", concept_display="grapefruit",
        object_type="object", registry=reg, user_id="u",
    )
    assert n == 0 and reg.alias_calls == [], "flag OFF → byte-for-byte no-op"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# B) QUERY EFFECT — with the fix's alias present, "citrus fruits" grounds the members; without it, miss
# ══════════════════════════════════════════════════════════════════════════════════════════════

CITRUS = "citrus-type-uuid"
LEMON = "lemon-type-uuid"
ORANGE = "orange-type-uuid"

# subclass_of ladder (child -> parent), the Class-C place-index the members hang off of "citrus".
_SUBCLASS = {LEMON: CITRUS, ORANGE: CITRUS}


class _ResolverCursor:
    """Fake cursor for `_resolve_count_type_uuids`, parameterised by the alias map so we can model
    BEFORE (no synonym alias) and AFTER (synonym alias registered by the ingest fix)."""
    def __init__(self, alias_map):
        self._alias_map = alias_map
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = list(params) if params else []
        if "FROM rel_types" in s and "wikidata_pid = ANY" in s:
            self._result = [("instance_of",), ("subclass_of",), ("is_a",)]
            return
        if "FROM entity_aliases" in s and "alias = ANY" in s and "entity_id" in s:
            cands = [str(c).lower() for c in (p[0] or [])]
            self._result = [(self._alias_map[c],) for c in cands if c in self._alias_map]
            return
        if "FROM entity_taxonomies" in s:
            self._result = []
            return
        _rels = set()
        if "object_id = ANY" in s and "subject_id" in s and len(p) >= 2:
            _rels = {str(r).lower() for r in (p[1] or [])}
        if _rels and "subclass_of" in _rels and "instance_of" not in _rels:
            frontier = set(p[0] or [])
            children = [c for c, parent in _SUBCLASS.items() if parent in frontier]
            self._result = [(c,) for c in children]
            return
        self._result = []

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ResolverDB:
    def __init__(self, alias_map):
        self._alias_map = alias_map

    def cursor(self):
        return _ResolverCursor(self._alias_map)


def test_before_fix_citrus_fruit_query_misses_members():
    # PRE-FIX: only "citrus" is an alias of the node. "citrus fruits" grounds nothing → no descent.
    before = {"citrus": CITRUS}
    out = m._resolve_count_type_uuids(_ResolverDB(before), "citrus fruits")
    assert CITRUS not in out and not ({LEMON, ORANGE} & out), (
        f"pre-fix, the synonym query surface must miss the members; got {out}")


def test_after_fix_citrus_fruit_query_reaches_members():
    # AFTER: ingest registered the WordNet synonym(s) of the node as aliases. Build the alias map
    # from the REAL `synset_synonyms` output (the exact surfaces the fix registers).
    syns = W.synset_synonyms("citrus")
    assert "citrus fruit" in syns
    after = {"citrus": CITRUS}
    for sy in syns:
        after[sy] = CITRUS  # each co-synset lemma now grounds to the SAME node
    out = m._resolve_count_type_uuids(_ResolverDB(after), "citrus fruits")
    assert CITRUS in out, "the synonym query surface now grounds the citrus node"
    assert {LEMON, ORANGE} <= out, (
        f"the count walk must reach the laddered members via the unified node; got {out}")


def test_after_fix_same_node_uuid_for_both_surfaces():
    # "citrus" and "citrus fruit" resolve to the SAME node UUID (the unification the fix guarantees).
    syns = W.synset_synonyms("citrus")
    after = {"citrus": CITRUS}
    for sy in syns:
        after[sy] = CITRUS
    a = m._resolve_count_type_uuids(_ResolverDB(after), "citrus")
    b = m._resolve_count_type_uuids(_ResolverDB(after), "citrus fruits")
    assert CITRUS in a and CITRUS in b and a == b


def test_no_overunify_unrelated_surface_still_misses():
    # A NON-synonym query surface ("vegetables") is not in the synset → still grounds nothing here,
    # so the fix never over-unifies unrelated types.
    syns = W.synset_synonyms("citrus")
    after = {"citrus": CITRUS}
    for sy in syns:
        after[sy] = CITRUS
    out = m._resolve_count_type_uuids(_ResolverDB(after), "vegetables")
    assert CITRUS not in out and not ({LEMON, ORANGE} & out)

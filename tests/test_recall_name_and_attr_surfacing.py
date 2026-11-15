"""Focused gate for the two recall-path enhancements (both flag-gated, fail-safe):

PIECE 2 — NAME-INTENT SURFACING (_synthesize_user_name_facts): the schema owner's names
live MULTI-VALUED in entity_aliases (registry) with NO also_known_as/pref_name FACT, so
"what is my name" returns nothing. The synthesizer reads the surfaceable registry aliases
and returns them as fact dicts to render. Detection is the EXISTING _query_has_name_intent
gate (subject-agnostic, not a per-name check) — proven here against junk/state words.

PIECE 1 — TRAVERSED-ENTITY ATTRIBUTES (_fetch_traversed_entity_attributes): reads the typed
scalar store for 1-hop neighbours the walk already reached, sensitivity-aware (a foreign
entity's sensitive attribute is never surfaced), anchor excluded.
"""
import pytest

from src.api.main import (
    _synthesize_user_name_facts,
    _fetch_traversed_entity_attributes,
    _query_has_name_intent,
    _is_surfaceable_user_name,
)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


# ── name-intent DETECTION is the existing gate, not a per-name zoo ────────────
@pytest.mark.parametrize("q", [
    "what is my name", "what's my real name", "what do you call me",
    "what is my full name", "what am I also known as", "what's her legal name",
])
def test_name_intent_detected(q):
    assert _query_has_name_intent(q) is True


@pytest.mark.parametrize("q", [
    "what is my favorite color", "where do I live", "tell me about my dog",
    "what is the capital of france", "",
])
def test_non_name_query_not_detected(q):
    assert _query_has_name_intent(q) is False


# ── PIECE 2: registry → name facts ───────────────────────────────────────────
def test_synthesize_emits_pref_and_aka():
    rows = [("chris", True), ("ace", False), ("finn", False)]
    out = _synthesize_user_name_facts(_FakeConn(rows), "u-1", preferred_user_name="chris")
    rels = {f["object"]: f["rel_type"] for f in out}
    assert rels["chris"] == "pref_name"
    assert rels["ace"] == "also_known_as"
    assert rels["finn"] == "also_known_as"
    # subject is the user UUID (identity slot), object is the alias STRING (no UUID leak)
    assert all(f["subject_id"] == "u-1" for f in out)
    assert all(isinstance(f["object"], str) for f in out)


def test_synthesize_filters_junk_aliases():
    # "worried"/"thinking" are loose-ingest junk — never surfaced as a name.
    rows = [("chris", True), ("worried", False), ("thinking", False)]
    out = _synthesize_user_name_facts(_FakeConn(rows), "u-1", preferred_user_name="chris")
    names = {f["object"] for f in out}
    assert names == {"chris"}
    assert not _is_surfaceable_user_name("worried")


def test_synthesize_dedups_case_insensitive():
    rows = [("Chris", True), ("chris", False)]
    out = _synthesize_user_name_facts(_FakeConn(rows), "u-1", preferred_user_name="chris")
    assert len(out) == 1


def test_synthesize_anonymous_is_empty():
    assert _synthesize_user_name_facts(_FakeConn([]), "anonymous") == []
    assert _synthesize_user_name_facts(_FakeConn([]), "") == []


def test_synthesize_failsafe_on_db_error():
    class _BoomConn:
        def cursor(self):
            raise RuntimeError("db down")
    assert _synthesize_user_name_facts(_BoomConn(), "u-1") == []


# ── PIECE 1: traversed-entity attributes ──────────────────────────────────────
def _attr_rows():
    # (entity_id, attribute, value_text, value_int, value_float, value_date, sensitivity,
    #  provenance)
    # ⚠️ The 8th column was added by the scalar-retract-recall-leak gauntlet (bar RL4): the
    # lane now reports the tier the row's stored provenance earns instead of a flat
    # Class A / 1.0. That campaign widened the sibling-lane fixture for the same reason and
    # MISSED this one, so this test went red at `30ded2b3` with
    # `query.traversed_attributes.failed error='not enough values to unpack'` — the lane
    # silently degraded to [] and its own fail-safe swallowed it. "user_stated" keeps the
    # surfacing behaviour asserted below byte-identical.
    return [
        ("mother-uuid", "born_on", "1960-05-01", None, None, None, "normal", "user_stated"),
        ("mother-uuid", "home_address", "123 Main St", None, None, None, "sensitive",
         "user_stated"),
        ("server-uuid", "has_ip", "10.0.0.9", None, None, None, "normal", "user_stated"),
    ]


def test_traversed_attrs_reads_normal_skips_sensitive():
    out = _fetch_traversed_entity_attributes(
        _FakeConn(_attr_rows()), {"mother-uuid", "server-uuid"}, anchor_uuid="user-uuid"
    )
    surfaced = {(f["subject"], f["rel_type"], f["object"]) for f in out}
    assert ("mother-uuid", "born_on", "1960-05-01") in surfaced
    assert ("server-uuid", "has_ip", "10.0.0.9") in surfaced
    # sensitive foreign attribute is NOT surfaced
    assert not any(f["rel_type"] == "home_address" for f in out)


def test_traversed_attrs_excludes_anchor():
    out = _fetch_traversed_entity_attributes(
        _FakeConn(_attr_rows()), {"anchor-uuid"}, anchor_uuid="anchor-uuid"
    )
    assert out == []


def test_traversed_attrs_failsafe_on_db_error():
    class _BoomConn:
        def cursor(self):
            raise RuntimeError("db down")
    assert _fetch_traversed_entity_attributes(_BoomConn(), {"x-uuid"}, anchor_uuid="a") == []

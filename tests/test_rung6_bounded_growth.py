"""RUNG-6 bounded-growth unit tests (deterministic, no DB / no LLM).

Covers the pure, deterministic surface of the RUNG-6 re-architecture:
  - the bridge-placement VALIDATOR (rung-4 rule gate) — the part that is fully implemented
    and gates any LLM bridging proposal;
  - the feature-flag defaults (cosine-map demoted OFF, convergence ON, bridging stubbed OFF);
  - the bridging STUB no-ops while the flag is off (design-target contract).

The DB-backed convergence sweep (converge_hierarchy_by_identity) needs a live per-tenant
schema; it is exercised by the live re_embedder integration tests, not here. These tests
guard the deterministic logic that can regress silently.
"""
import importlib

import pytest

emb = importlib.import_module("src.re_embedder.embedder")


# ── Flag defaults — the live-behaviour contract ────────────────────────────────────────────────
def test_flag_defaults():
    # Convergence is the new deterministic primary → ON by default.
    assert emb._RUNG6_CONVERGENCE is True
    # Cosine-map is RETIRED as primary → demoted OFF by default.
    assert emb._ONTOLOGY_COSINE_MAP is False
    # Bridging is a design-target stub → OFF by default.
    assert emb._RUNG6_BRIDGING is False


# ── Bridge placement validator (rung-4 hierarchy rules) ─────────────────────────────────────────
def test_bridge_valid_simple():
    ok, reason = emb._validate_bridge_placement("dog", "wolf", "canid")
    assert ok is True
    assert reason == "ok"


def test_bridge_rejects_upper_root():
    # connect-to-known, never extend to a universal root.
    for root in ("thing", "entity", "object", "concept"):
        ok, reason = emb._validate_bridge_placement("dog", "wolf", root)
        assert ok is False
        assert reason == "lca_is_upper_root"


def test_bridge_rejects_scalar_lca():
    ok, reason = emb._validate_bridge_placement("host", "router", "192.168.1.1")
    assert ok is False
    assert reason == "lca_looks_scalar"


def test_bridge_rejects_loose_phrase():
    ok, reason = emb._validate_bridge_placement("dog", "wolf", "a kind of wild animal thing")
    assert ok is False
    assert reason == "lca_not_a_category_token"


def test_bridge_rejects_self_bridge():
    ok, reason = emb._validate_bridge_placement("dog", "wolf", "dog")
    assert ok is False
    assert reason == "lca_equals_child"


def test_bridge_rejects_type_mismatch():
    ok, reason = emb._validate_bridge_placement(
        "dog", "riverton", "canid", child_a_type="Animal", child_b_type="Location"
    )
    assert ok is False
    assert reason.startswith("type_mismatch")


def test_bridge_allows_unknown_types():
    # unknown/blank types must NOT block a structurally-valid bridge.
    ok, _ = emb._validate_bridge_placement(
        "dog", "wolf", "canid", child_a_type="Animal", child_b_type="unknown"
    )
    assert ok is True
    ok2, _ = emb._validate_bridge_placement("dog", "wolf", "canid", child_a_type="", child_b_type="")
    assert ok2 is True


# ── Bridging stub — no-op while flagged off ─────────────────────────────────────────────────────
def test_bridge_stub_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(emb, "_RUNG6_BRIDGING", False)
    assert emb._propose_lca_bridge("dog", "wolf", "http://unused") is None


# ── Convergence is a no-op when the flag is off (cannot touch the DB) ───────────────────────────
def test_convergence_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(emb, "_RUNG6_CONVERGENCE", False)

    class _BoomConn:
        def cursor(self, *a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("convergence must not touch the DB when disabled")

    stats = emb.converge_hierarchy_by_identity(_BoomConn(), schema_name="t")
    assert stats == {"merged_nodes": 0, "edges_repointed": 0}


# ── LEVER 1: coherent grown-root category gate (pure) ───────────────────────────────────────────
_ROOTS = {"animal", "location", "person", "organization", "object", "concept"}


def test_coherent_grown_root_accepts_real_category():
    # program / software / video_game are genuine categories the climb may TERMINATE at
    # gracefully (grown, not seeded) — INTENT over ISLANDS.
    for tok in ("program", "software", "video_game", "emotion", "network_device"):
        assert emb._is_coherent_category_token(tok, _ROOTS) is True, tok


def test_coherent_grown_root_rejects_seeded_root():
    # a SEEDED root is the PRIMARY termination case, NOT the grown-root backstop → gate says False.
    for tok in _ROOTS:
        assert emb._is_coherent_category_token(tok, _ROOTS) is False, tok


def test_coherent_grown_root_rejects_no_info_and_scalar_and_phrase():
    # no-info upper placeholders / scalars / loose phrases are NOT coherent grown roots.
    assert emb._is_coherent_category_token("abstract_entity", _ROOTS) is False
    assert emb._is_coherent_category_token("psychological_phenomenon", _ROOTS) is False
    assert emb._is_coherent_category_token("thing", _ROOTS) is False
    assert emb._is_coherent_category_token("192.168.1.1", _ROOTS) is False
    assert emb._is_coherent_category_token("a kind of wild animal thing", _ROOTS) is False
    assert emb._is_coherent_category_token("", _ROOTS) is False


# ── LEVER 2: deterministic survivor pick for a 2-cycle merge (pure) ─────────────────────────────
def test_survivor_pick_prefers_higher_evidence():
    # higher hierarchy degree survives regardless of UUID order.
    surv, loser = emb._pick_survivor_by_evidence("bbbb", 9, "aaaa", 2)
    assert surv == "bbbb" and loser == "aaaa"
    surv, loser = emb._pick_survivor_by_evidence("aaaa", 2, "bbbb", 9)
    assert surv == "bbbb" and loser == "aaaa"


def test_survivor_pick_tie_breaks_on_lowest_uuid():
    surv, loser = emb._pick_survivor_by_evidence("bbbb", 5, "aaaa", 5)
    assert surv == "aaaa" and loser == "bbbb"


def test_2cycle_collapse_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(emb, "_RUNG6_CONVERGENCE", False)

    class _BoomConn:
        def cursor(self, *a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("2-cycle collapse must not touch the DB when disabled")

    stats = emb.collapse_hierarchy_2cycles(_BoomConn(), schema_name="t")
    assert stats == {"cycles_collapsed": 0, "edges_repointed": 0}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

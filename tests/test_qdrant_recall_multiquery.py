"""Unit tests for the Class-C / Qdrant multi-query expansion + RRF rerank lane.

Covers src/api/qdrant_recall.py — the deterministic-by-default short-term recall
strengthening. No DB / Qdrant / LLM needed (all pure functions). Asserts:
  • deterministic query variants strip recall/question framing without inventing facts;
  • multi-query angle building is bounded and original-first;
  • RRF fusion ranks a fact that appears across MULTIPLE sub-queries above a fact that
    scored higher in only one (the core multi-query payoff).
"""

from src.api import qdrant_recall as qr


def test_deterministic_variants_strip_framing_original_first():
    v = qr.deterministic_query_variants("What is my favorite hobby?")
    assert v[0] == "What is my favorite hobby?"          # original always first
    assert "my favorite hobby" in v                       # lead-framing stripped
    # No fabricated tokens — every variant is a substring-ish reshuffle of the input.
    for s in v:
        assert "hobby" in s.lower() or "favorite" in s.lower()


def test_deterministic_variants_empty_input():
    assert qr.deterministic_query_variants("") == []
    assert qr.deterministic_query_variants("   ") == []


def test_build_query_angles_bounded_and_original_first():
    angles = qr.build_query_angles("tell me about my dog")
    assert angles[0] == "tell me about my dog"
    assert len(angles) <= qr.QDRANT_MULTIQUERY_MAX
    assert "my dog" in angles  # framing-stripped angle present


def test_rrf_prefers_multi_angle_agreement():
    # fact 2 appears in BOTH sub-queries; fact 1 has the single highest cosine but
    # only appears once. RRF must float fact 2 to the top.
    h1 = [
        {"fact_id": 1, "source_table": "staged_facts", "qdrant_score": 0.9,
         "subject": "a", "rel_type": "r", "object": "b"},
        {"fact_id": 2, "source_table": "staged_facts", "qdrant_score": 0.5,
         "subject": "c", "rel_type": "r", "object": "d"},
    ]
    h2 = [
        {"fact_id": 2, "source_table": "staged_facts", "qdrant_score": 0.6,
         "subject": "c", "rel_type": "r", "object": "d"},
        {"fact_id": 3, "source_table": "staged_facts", "qdrant_score": 0.4,
         "subject": "e", "rel_type": "r", "object": "f"},
    ]
    fused = qr.fuse_multiquery_hits([h1, h2])
    assert [f["fact_id"] for f in fused][0] == 2          # cross-angle winner on top
    # best cosine is preserved (used by downstream confidence/hit gates).
    top = next(f for f in fused if f["fact_id"] == 2)
    assert top["qdrant_score"] == 0.6
    assert top["_rrf_score"] > 0


def test_rrf_dedups_context_rows_by_triple():
    # store_context rows have no fact_id → keyed by (subject, rel_type, object).
    h1 = [{"fact_id": None, "rel_type": "context", "subject": "user",
           "object": "warm room", "qdrant_score": 0.7}]
    h2 = [{"fact_id": None, "rel_type": "context", "subject": "user",
           "object": "warm room", "qdrant_score": 0.8}]
    fused = qr.fuse_multiquery_hits([h1, h2])
    assert len(fused) == 1                                 # same blob unioned, not duped
    assert fused[0]["qdrant_score"] == 0.8                 # best cosine kept


def test_fuse_empty_is_empty():
    assert qr.fuse_multiquery_hits([[], []]) == []

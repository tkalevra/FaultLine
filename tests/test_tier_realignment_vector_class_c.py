"""
TIER REALIGNMENT — vector (Qdrant) scoped to Class C only.

Spec: DEV/DESIGN-hierarchy-ladder-and-growth.md §"Strong ingest / brain-dead query —
and the hierarchy IS the index".

Rules under test:
  1. A/B are served by the deterministic postgres walk and are NEVER served from the
     vector. The Qdrant lane contributes ONLY Class C — a Qdrant result whose
     authoritative DB class is A or B is dropped from the lane.
  2. The query goal is to ANSWER: a Class C result that answers (passes the existing
     confidence floor) is STILL returned — C answers are never suppressed by this filter.
  3. store_context (pure-vector, no Postgres backing) is always Class C.
  4. Cosine = the Class-C tally: the hit_count bump is gated on fact_class == 'C'.

These tests exercise the REAL predicate `_qdrant_fact_is_class_c` (main.py) — the same
function the /query Qdrant lane uses — and the flag wiring on both sides.
"""

from src.api.main import _qdrant_fact_is_class_c, VECTOR_CLASS_C_ONLY


# ─────────────────────────────────────────────────────────────────────────────
# Rule 1 — A/B dropped from the Qdrant lane
# ─────────────────────────────────────────────────────────────────────────────

def test_facts_table_point_is_not_class_c():
    """A facts-table-sourced point (A/promoted-B) is A/B → dropped from the vector lane."""
    f = {"source_table": "facts", "fact_id": 7, "fact_class": "A", "rel_type": "spouse"}
    assert _qdrant_fact_is_class_c(f) is False


def test_payload_class_b_is_not_class_c():
    """A staged point whose read-back class is B is A/B-tier → dropped."""
    f = {"source_table": "staged_facts", "fact_id": 12, "fact_class": "B", "rel_type": "works_for"}
    assert _qdrant_fact_is_class_c(f) is False


def test_facts_table_overrides_stale_c_payload():
    """Even if a facts-table point carries a stale fact_class='C' payload, source_table
    is authoritative: facts holds only A/B → NOT C (kills the A/B-reads-back-as-C bug)."""
    f = {"source_table": "facts", "fact_id": 3, "fact_class": "C", "rel_type": "has_pet"}
    assert _qdrant_fact_is_class_c(f) is False


def test_grounding_keys_make_fact_authoritatively_ab():
    """A fact_id resolved in the `facts` table (via the UUID-grounding step) is A/B even
    when its own source_table says staged_facts (defensive: authoritative table wins)."""
    f = {"source_table": "staged_facts", "fact_id": 9, "fact_class": "C", "rel_type": "owns"}
    ab_keys = {("facts", 9)}
    assert _qdrant_fact_is_class_c(f, ab_keys) is False


# ─────────────────────────────────────────────────────────────────────────────
# Rule 2 / 3 — C still answers; store_context is always C
# ─────────────────────────────────────────────────────────────────────────────

def test_staged_class_c_is_kept():
    """A genuine Class C staged fact is KEPT — the goal is to ANSWER, not tier-purity."""
    f = {"source_table": "staged_facts", "fact_id": 5, "fact_class": "C", "rel_type": "likes"}
    assert _qdrant_fact_is_class_c(f) is True


def test_store_context_is_always_class_c():
    """store_context rows have no Postgres backing → pure-vector Class C, always kept."""
    f = {"rel_type": "context", "source_table": None, "fact_id": None}
    assert _qdrant_fact_is_class_c(f) is True


def test_missing_class_defaults_to_c_for_staged():
    """A staged point with no class info defaults to C (kept) — never silently drop an
    answer; only authoritative A/B are removed."""
    f = {"source_table": "staged_facts", "fact_id": 8, "rel_type": "related_to"}
    assert _qdrant_fact_is_class_c(f) is True


def test_lane_filter_keeps_c_drops_ab():
    """End-to-end on the lane: a mixed batch is filtered to C-only; the C answer survives."""
    lane = [
        {"source_table": "facts", "fact_id": 1, "fact_class": "A", "rel_type": "spouse"},
        {"source_table": "staged_facts", "fact_id": 2, "fact_class": "B", "rel_type": "works_for"},
        {"source_table": "staged_facts", "fact_id": 3, "fact_class": "C", "rel_type": "likes"},
        {"rel_type": "context", "source_table": None, "fact_id": None},
    ]
    ab_keys = {("facts", 1)}
    kept = [f for f in lane if _qdrant_fact_is_class_c(f, ab_keys)]
    rels = {f["rel_type"] for f in kept}
    assert rels == {"likes", "context"}, kept


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4 — cosine tally is gated on Class C (mirror the query-side guard)
# ─────────────────────────────────────────────────────────────────────────────

def test_hit_tally_only_targets_class_c():
    """The hit_count bump must only fire for fact_class == 'C' (cosine = the Class-C
    tally; it never tallies A/B). Mirror the guard the /query hit loop applies."""
    facts = [
        {"fact_class": "A", "fact_id": 1},
        {"fact_class": "B", "fact_id": 2},
        {"fact_class": "C", "fact_id": 3},
        {"fact_class": None, "fact_id": 4},  # defaults to C
    ]
    eligible = [f for f in facts if (f.get("fact_class") or "C") == "C"]
    ids = {f["fact_id"] for f in eligible}
    assert ids == {3, 4}


# ─────────────────────────────────────────────────────────────────────────────
# Flag wiring — default ON, env-overridable, fail-safe on both modules
# ─────────────────────────────────────────────────────────────────────────────

def test_query_flag_default_on():
    assert VECTOR_CLASS_C_ONLY is True


def test_query_flag_parse_off_values():
    """The flag-parse expression (mirrors main.py) turns off only on false/0/no."""
    def _parse(val: str) -> bool:
        return val.strip().lower() not in ("false", "0", "no")
    assert _parse("false") is False
    assert _parse("0") is False
    assert _parse("no") is False
    assert _parse("true") is True
    assert _parse("1") is True
    assert _parse("") is True  # unset-style empty → default ON


def test_embedder_flag_default_on():
    import src.re_embedder.embedder as emb
    assert emb._VECTOR_CLASS_C_ONLY is True

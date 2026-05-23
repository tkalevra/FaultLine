"""Test suite for calculate_relevance_score() in faultline_tool.py"""

import sys
from pathlib import Path

# Add the openwebui directory to the path so we can import the filter
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "openwebui"))

from faultline_tool import Filter


def test_greeting_scores_zero_for_pii_facts():
    """
    fact: rel_type="lives_at", confidence=0.8, category="location"
    query: "hey how are you"
    expected: score == 0.0 (sensitivity penalty drives to zero)
    """
    filter_instance = Filter()
    fact = {
        "rel_type": "lives_at",
        "confidence": 0.8,
        "category": "location",
        "subject": "user",
        "object": "my home",
    }
    query = "hey how are you"
    score = filter_instance.calculate_relevance_score(fact, query)
    assert score == 0.0, f"Expected 0.0, got {score}"


def test_explicit_address_query_scores_high():
    """
    fact: rel_type="lives_at", confidence=0.8, category="location"
    query: "where do I live"
    expected: score >= 0.0 (confidence-only; "live" in _SENSITIVE_TERMS prevents penalty)
    """
    filter_instance = Filter()
    fact = {
        "rel_type": "lives_at",
        "confidence": 0.8,
        "category": "location",
        "subject": "user",
        "object": "123 Main Street",
    }
    query = "where do I live"
    score = filter_instance.calculate_relevance_score(fact, query)
    assert score == 0.24, f"Expected 0.24 (0.8*0.3), got {score}"


def test_work_query_scores_work_fact():
    """
    fact: rel_type="works_for", confidence=0.9, category="work"
    query: "where do I work"
    expected: score == 0.27 (0.9*0.3, works_for not sensitive → no penalty)
    """
    filter_instance = Filter()
    fact = {
        "rel_type": "works_for",
        "confidence": 0.9,
        "category": "work",
        "subject": "user",
        "object": "TechCorp",
    }
    query = "where do I work"
    score = filter_instance.calculate_relevance_score(fact, query)
    assert score == 0.27, f"Expected 0.27 (0.9*0.3), got {score}"


def test_unrelated_query_scores_low_for_normal_fact():
    """
    fact: rel_type="works_for", confidence=0.6, category="work"
    query: "what is the capital of France"
    expected: score < 0.4
    """
    filter_instance = Filter()
    fact = {
        "rel_type": "works_for",
        "confidence": 0.6,
        "category": "work",
        "subject": "user",
        "object": "TechCorp",
    }
    query = "what is the capital of France"
    score = filter_instance.calculate_relevance_score(fact, query)
    assert score < 0.4, f"Expected < 0.4, got {score}"


def test_high_confidence_boosts_score():
    """
    fact: rel_type="works_for", confidence=1.0, category="work"
    query: "tell me about myself"
    expected: score > low_confidence equivalent
    """
    filter_instance = Filter()
    fact_high_conf = {
        "rel_type": "works_for",
        "confidence": 1.0,
        "category": "work",
        "subject": "user",
        "object": "TechCorp",
    }
    fact_low_conf = {
        "rel_type": "works_for",
        "confidence": 0.4,
        "category": "work",
        "subject": "user",
        "object": "TechCorp",
    }
    query = "tell me about myself"

    score_high = filter_instance.calculate_relevance_score(fact_high_conf, query)
    score_low = filter_instance.calculate_relevance_score(fact_low_conf, query)

    assert score_high > score_low, f"Expected high ({score_high}) > low ({score_low})"


def test_sensitive_rel_without_explicit_ask_penalised():
    """
    fact: rel_type="born_on", confidence=1.0, category="temporal"
    query: "what day is it today"
    expected: score == 0.0
    """
    filter_instance = Filter()
    fact = {
        "rel_type": "born_on",
        "confidence": 1.0,
        "category": "temporal",
        "subject": "user",
        "object": "march 15",
    }
    query = "what day is it today"
    score = filter_instance.calculate_relevance_score(fact, query)
    assert score == 0.0, f"Expected 0.0, got {score}"


def test_sensitive_rel_with_explicit_ask_not_penalised():
    """
    fact: rel_type="born_on", confidence=0.8, category="temporal"
    query: "when was I born"
    expected: score == 0.24 (0.8*0.3, "born" in _SENSITIVE_TERMS prevents penalty)
    """
    filter_instance = Filter()
    fact = {
        "rel_type": "born_on",
        "confidence": 0.8,
        "category": "temporal",
        "subject": "user",
        "object": "march 15",
    }
    query = "when was I born"
    score = filter_instance.calculate_relevance_score(fact, query)
    assert score == 0.24, f"Expected 0.24 (0.8*0.3), got {score}"


def test_graph_proximity_pass_through_keeps_backend_facts():
    """
    _filter_relevant_facts() Tier 3: graph-proximity pass-through with 0.0 threshold.
    When backend returns only non-identity facts (e.g., taxonomy-filtered pet facts),
    Tier 2 finds no identity rels and falls through to Tier 3, where all
    confidence-gated facts pass.

    "our pets" works without keyword "my pets" — the backend's graph proximity
    from /query is the relevance signal. The Filter just gates by confidence.
    """
    filter_instance = Filter()
    # Backend already filtered by taxonomy (dprompt-47): "pets" query → household
    # taxonomy → only Animal-type facts. No identity/spouse facts mixed in.
    facts = [
        {
            "rel_type": "has_pet",
            "confidence": 0.7,
            "subject": "user",
            "object": "fraggle",
        },
        {
            "rel_type": "has_pet",
            "confidence": 0.6,
            "subject": "user",
            "object": "morkie",
        },
    ]
    query = "tell me about our pets"
    identity = "user"
    preferred_names = {"fraggle": "fraggle", "morkie": "morkie"}

    filtered = filter_instance._filter_relevant_facts(
        facts, identity, preferred_names=preferred_names, query=query
    )

    # Tier 1: no entity name in query tokens → skipped
    # Tier 2: has_pet not in _TIER2_IDENTITY_RELS → empty → falls through
    # Tier 3: both facts: confidence*0.3 >= 0.0 → pass. No sensitivity penalty.
    # MIN_INJECT_CONFIDENCE=0.5 gates: 0.7 >= 0.5, 0.6 >= 0.5 → both pass.
    assert len(filtered) == 2, f"Expected 2 facts (both has_pet), got {len(filtered)}"
    assert all(f["rel_type"] == "has_pet" for f in filtered)


def test_graph_proximity_all_facts_pass_confidence_only():
    """
    Tier 3: graph-proximity pass-through. All non-identity, non-garbage
    facts from the backend pass the 0.0 threshold. The backend already
    determined these facts are connected via graph traversal.
    """
    filter_instance = Filter()
    facts = [
        {
            "rel_type": "has_pet",
            "confidence": 0.7,
            "subject": "user",
            "object": "fraggle",
        },
        {
            "rel_type": "works_for",
            "confidence": 0.9,
            "subject": "user",
            "object": "TechCorp",
        },
    ]
    query = "tell me about our pets"
    identity = "user"
    # No preferred_names → Tier 1 skipped.
    # Tier 2: has_pet not in _TIER2_IDENTITY_RELS, works_for not either → empty.
    # Tier 3: both pass 0.0 threshold → both returned.

    filtered = filter_instance._filter_relevant_facts(
        facts, identity, query=query
    )

    # Both facts pass 0.0 threshold (confidence-only, no sensitivity penalty
    # for these rel_types). "our pets" returns has_pet without keyword "my pets".
    assert len(filtered) == 2, f"Expected 2 facts, got {len(filtered)}"


def test_sensitive_facts_dropped_when_high_conf_alternatives_exist():
    """
    When high-confidence facts exist, the confidence gate prefers them.
    Low-confidence sensitive facts without explicit ask are excluded
    because higher-confidence alternatives exist.
    """
    filter_instance = Filter()
    facts = [
        {
            "rel_type": "lives_at",
            "confidence": 0.9,
            "subject": "user",
            "object": "123 Main Street",
        },
        {
            "rel_type": "born_on",
            "confidence": 0.3,  # Low confidence — will be gated
            "subject": "user",
            "object": "march 15",
        },
    ]
    query = "how are you"
    identity = "user"

    filtered = filter_instance._filter_relevant_facts(
        facts, identity, query=query
    )

    # lives_at: confidence 0.9 → score 0.27, sensitivity -0.5 → -0.23 → 0.0 → passes 0.0
    # born_on: confidence 0.3 → score 0.09, sensitivity -0.5 → -0.41 → 0.0 → passes 0.0
    # Both pass 0.0 threshold. Then _apply_confidence_gate:
    #   lives_at (0.9) >= 0.5 → high_conf
    #   born_on (0.3) < 0.5 → excluded
    # Result: only lives_at returned
    assert len(filtered) == 1, f"Expected 1 fact (high-conf only), got {len(filtered)}"
    assert filtered[0]["rel_type"] == "lives_at"


def test_entity_attribute_height_scores_on_tall_query():
    """
    Synthetic fact: rel_type="height", category="physical", confidence=1.0
    Query: "how tall am I?"
    Expected: score == 0.3 (1.0*0.3, "tall" in _SENSITIVE_TERMS prevents penalty).
    """
    filter_instance = Filter()
    synthetic_fact = {
        "rel_type": "height",
        "category": "physical",
        "confidence": 1.0,
    }
    query = "how tall am I?"
    score = filter_instance.calculate_relevance_score(synthetic_fact, query)
    assert score == 0.3, f"Expected 0.3 (1.0*0.3), got {score} for height on 'how tall am I?'"


def test_entity_attribute_height_suppressed_on_unrelated_query():
    """
    Synthetic fact: rel_type="height", category="physical", confidence=1.0
    Query: "what is the weather today"
    Expected: score == 0.0 (sensitivity penalty -0.5 drives to zero).
    """
    filter_instance = Filter()
    synthetic_fact = {
        "rel_type": "height",
        "category": "physical",
        "confidence": 1.0,
    }
    query = "what is the weather today"
    score = filter_instance.calculate_relevance_score(synthetic_fact, query)
    assert score == 0.0, f"Expected 0.0, got {score} for height on weather query"

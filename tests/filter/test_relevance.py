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
    expected: score >= 0.4
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
    assert score >= 0.4, f"Expected >= 0.4, got {score}"


def test_work_query_scores_work_fact():
    """
    fact: rel_type="works_for", confidence=0.9, category="work"
    query: "where do I work"
    expected: score >= 0.4
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
    assert score >= 0.4, f"Expected >= 0.4, got {score}"


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
    expected: score >= 0.4
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
    assert score >= 0.4, f"Expected >= 0.4, got {score}"


def test_empty_scored_returns_empty_not_cleaned():
    """
    _filter_relevant_facts() with all facts scoring below threshold.
    Verify: returns empty list, NOT the full cleaned list.
    Bug 1 test: fallback path now returns scored (empty) not cleaned (all facts).
    """
    filter_instance = Filter()
    # Three facts that all score below 0.4 for this query
    facts = [
        {
            "rel_type": "lives_at",
            "confidence": 0.8,
            "category": "location",
            "subject": "user",
            "object": "123 Main Street",
        },
        {
            "rel_type": "works_for",
            "confidence": 0.8,
            "category": "work",
            "subject": "user",
            "object": "TechCorp",
        },
        {
            "rel_type": "born_on",
            "confidence": 0.8,
            "category": "temporal",
            "subject": "user",
            "object": "march 15",
        },
    ]
    # Query with no matching signals and no sensitive term matches
    query = "what is the weather like"
    categories = set()  # No categories matched
    identity = "user"

    filtered = filter_instance._filter_relevant_facts(
        facts, categories, identity, query=query, is_realtime=False
    )

    # All facts should be filtered out (no matches, no sensitive term hits)
    assert len(filtered) == 0, f"Expected 0 facts, got {len(filtered)}"


def test_entity_attribute_height_scores_on_tall_query():
    """
    Synthetic fact: rel_type="height", category="physical", confidence=1.0
    Query: "how tall am I?"
    Expected: score >= 0.4 (tall matches physical signal, "tall" in _SENSITIVE_TERMS prevents penalty).
    Bug 2 & 3 test: entity attributes should score >= 0.4 on physical queries.
    """
    filter_instance = Filter()
    synthetic_fact = {
        "rel_type": "height",
        "category": "physical",
        "confidence": 1.0,
    }
    query = "how tall am I?"
    score = filter_instance.calculate_relevance_score(synthetic_fact, query)
    assert score >= 0.4, f"Expected >= 0.4, got {score} for height on 'how tall am I?'"


def test_entity_attribute_height_suppressed_on_unrelated_query():
    """
    Synthetic fact: rel_type="height", category="physical", confidence=1.0
    Query: "what is the weather today"
    Expected: score < 0.4 (no physical signals in query, sensitivity penalty applies).
    Bug 2 test: entity attributes should not inject on unrelated queries.
    """
    filter_instance = Filter()
    synthetic_fact = {
        "rel_type": "height",
        "category": "physical",
        "confidence": 1.0,
    }
    query = "what is the weather today"
    score = filter_instance.calculate_relevance_score(synthetic_fact, query)
    assert score < 0.4, f"Expected < 0.4, got {score} for height on weather query"

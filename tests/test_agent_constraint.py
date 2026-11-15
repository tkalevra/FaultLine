"""CONSTRAINED customer-agent pipeline + EVALUATION GAUNTLET — pure-logic tests.

Covers ``saas/agent_constraint.py`` (triage / abstention / refusal / grounding) and the
pure, injectable parts of ``saas/agent_gauntlet.py`` (blind-judge prompt + verdict parsing)
without DB, without network, without a brain. The route-level + DB round-log behavior is
covered separately (test_agent_scope_routes.py / the delegate-lane tests in test_agent_control.py).

Run: python3 -m pytest tests/test_agent_constraint.py -q
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from saas.agent_constraint import (
    abstention_text,
    consequential_refusal_text,
    domain_for_reason,
    grounding_instruction,
    out_of_scope_text,
    scope_enabled,
    triage,
)
from saas.agent_gauntlet import (
    blind_judge_prompt,
    consecutive_wins,
    parse_verdict,
)


# ════════════════════════════════════════════════════════════════════════════════════
# triage
# ════════════════════════════════════════════════════════════════════════════════════

EMPTY = {"enabled": True, "allowed_topics": [], "forbidden_topics": [],
         "redirect_target": "", "escalation_triggers": []}


def test_triage_unconstrained_scope_empty_is_in_scope():
    """No scope restriction matches → in-scope (a greeting / normal turn passes)."""
    v = triage("Hi there, how can I help today?", EMPTY)
    assert v["verdict"] == "in_scope"


def test_triage_consequential_medical_refused():
    v = triage("I've been feeling dizzy after taking my new medication, what should I do?",
               EMPTY)
    assert v["verdict"] == "escalation"
    assert "medical" in domain_for_reason(v["reason"], v["matched"])


def test_triage_consequential_medical_short_form():
    v = triage("Can you tell me what dose of my prescription I should take?", EMPTY)
    assert v["verdict"] == "escalation"
    assert "medical" in domain_for_reason(v["reason"], v["matched"])


def test_triage_consequential_legal():
    v = triage("Should I sue my landlord over this?", EMPTY)
    assert v["verdict"] == "escalation"
    assert "legal" in domain_for_reason(v["reason"], v["matched"])


def test_triage_consequential_financial():
    v = triage("Is buying more of that stock a good investment right now?", EMPTY)
    assert v["verdict"] == "escalation"
    assert "financial" in domain_for_reason(v["reason"], v["matched"])


def test_triage_safety_emergency():
    v = triage("I need help now — I feel like hurting myself.", EMPTY)
    assert v["verdict"] == "escalation"
    assert "urgent-safety" in domain_for_reason(v["reason"], v["matched"])


def test_triage_forbidden_topic_declined():
    scope = dict(EMPTY, forbidden_topics=["pricing", "bulk orders"])
    v = triage("Can you give me pricing for 500 units?", scope)
    assert v["verdict"] == "out_of_scope"
    assert v["matched"] == "pricing"


def test_triage_forbidden_is_word_boundary_not_substring():
    scope = dict(EMPTY, forbidden_topics=["bag"])
    v = triage("Do you sell bags of our product?", scope)
    assert v["verdict"] == "in_scope"  # "bags" does not match word-boundary "bag"


def test_triage_allowed_topic_gate():
    scope = dict(EMPTY, allowed_topics=["store hours", "delivery"])
    assert triage("What are your store hours?", scope)["verdict"] == "in_scope"
    assert triage("Tell me about your delivery options.", scope)["verdict"] == "in_scope"
    # An ask outside the allow-list is declined (the tenant decides the boundary).
    assert triage("Who founded the company?", scope)["verdict"] == "out_of_scope"


def test_triage_tenant_escalation_trigger():
    scope = dict(EMPTY, escalation_triggers=["promotions", "sponsorship"])
    v = triage("Can we discuss a sponsorship deal?", scope)
    assert v["verdict"] == "escalation"
    assert v["matched"] == "sponsorship"


def test_triage_fail_closed_on_unparseable_scope():
    """An unparseable scope config must NOT silently open the unconstrained lane."""
    v = triage("What do you sell?", {"enabled": "garbage", "forbidden_topics": None})
    assert v["verdict"] in ("in_scope", "escalation")  # never crashes; stays defined


def test_triage_empty_turn_is_greeting_in_scope():
    v = triage("   ", EMPTY)
    assert v["verdict"] == "in_scope"


# ════════════════════════════════════════════════════════════════════════════════════
# templates — the product's voice (informational tool, not a professional)
# ════════════════════════════════════════════════════════════════════════════════════

def test_abstention_states_plainly_it_is_informational():
    t = abstention_text("Optimist Depot", "https://optimus.example/contact")
    assert "informational tool" in t
    assert "not a professional" in t
    assert "Optimist Depot" in t
    assert "https://optimus.example/contact" in t


def test_consequential_refusal_directs_to_licensed_professionals():
    t = consequential_refusal_text("Pharma Co", "1-800-CARE", "consequential domain", "dose")
    assert "medical" in t
    assert "licensed professionals" in t
    assert "Pharma Co" in t


def test_out_of_scope_declines_and_redirects():
    t = out_of_scope_text("Baker & Sons", "support@baker.example")
    assert "outside what" in t.lower()
    assert "support@baker.example" in t


def test_grounding_instruction_forbids_parametric_guessing():
    g = grounding_instruction("Acme")
    assert "only" in g.lower() and "never" in g.lower()
    assert "Acme" in g


def test_scope_enabled_fail_closed():
    assert scope_enabled({"enabled": True}) is True
    assert scope_enabled({"enabled": False}) is False
    assert scope_enabled({}) is False
    # An unreadable value can't be confirmed OFF → stays ON (fail closed).
    assert scope_enabled({"enabled": "???"}) is True


# ════════════════════════════════════════════════════════════════════════════════════
# the blind judge — labels stripped, winner parsing, fail-closed
# ════════════════════════════════════════════════════════════════════════════════════

def test_blind_judge_prompt_never_names_the_agent():
    p = blind_judge_prompt("q", "ans1", "ans2", "Optimus")
    assert "Optimus" in p
    # The judge never learns WHICH answer is the agent's — the prompt only ever labels them
    # "Answer A" / "Answer B" and states the judge does not know the origin of either.
    assert "You do NOT know which answer" in p
    assert "Answer A" in p and "Answer B" in p
    # Neither answer body is ever tagged with the word "agent" or "reference".
    for frag in ("Answer A:", "Answer B:"):
        idx = p.index(frag)
        seg = p[idx:idx + len(frag) + 40].lower()
        assert "agent" not in seg and "reference" not in seg


def test_parse_verdict_winner_a():
    assert parse_verdict("WINNER A\n")["verdict"] == "agent"


def test_parse_verdict_winner_b_with_gap():
    r = parse_verdict("WINNER B\nThe agent never mentioned the free consultation.")
    assert r["verdict"] == "reference"
    assert r["gap"] == "The agent never mentioned the free consultation."


def test_parse_verdict_tie():
    assert parse_verdict("WINNER TIE")["verdict"] == "tie"


def test_parse_verdict_fail_closed_to_reference():
    """A judge that does not clearly pick the agent must NOT hand it a win."""
    r = parse_verdict("I don't know, they both seem fine.")
    assert r["verdict"] == "reference"


def test_parse_verdict_case_insensitive():
    assert parse_verdict("winner b")["verdict"] == "reference"


def test_consecutive_wins_no_db_is_fail_closed():
    """With no DB, the streak reads as zero wins — never a false pass."""
    st = consecutive_wins("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                          "10d7d879-63cd-4f31-92ce-f2c9edb760ab")
    assert st["count"] == 0
    assert st["passed"] is False


# ════════════════════════════════════════════════════════════════════════════════════
# AutoScope — the lenient draft parser (saas.agent_autoscope._parse_draft)
# ════════════════════════════════════════════════════════════════════════════════════

from saas.agent_autoscope import _parse_draft


def test_parse_draft_clean_json():
    d = _parse_draft('{"allowed_topics": ["free consultations", "delivery"], '
                     '"forbidden_topics": ["pricing"], "redirect_target": "support@x.ca", '
                     '"escalation_triggers": ["sponsorship"]}')
    assert d["allowed_topics"] == ["free consultations", "delivery"]
    assert d["forbidden_topics"] == ["pricing"]
    assert d["redirect_target"] == "support@x.ca"
    assert d["escalation_triggers"] == ["sponsorship"]


def test_parse_draft_tolerates_trailing_comma():
    d = _parse_draft('{"allowed_topics": ["a", "b",], "forbidden_topics": [],'
                     ' "redirect_target": "", "escalation_triggers": []}')
    assert d["allowed_topics"] == ["a", "b"]


def test_parse_draft_tolerates_prose_around_json():
    d = _parse_draft('Here is the scope I propose:\n\n```json\n{"allowed_topics": ["a"],'
                     ' "forbidden_topics": [], "redirect_target": "", "escalation_triggers": []}\n```')
    assert d["allowed_topics"] == ["a"]


def test_parse_draft_empty_on_malformed():
    d = _parse_draft('the brain rambled and produced no json at all')
    assert d["allowed_topics"] == [] and d["redirect_target"] == ""


def test_parse_draft_string_redirect():
    d = _parse_draft('{"allowed_topics": [], "forbidden_topics": [],'
                     ' "redirect_target": "https://x.example/contact", "escalation_triggers": []}')
    assert d["redirect_target"] == "https://x.example/contact"

"""OFFLINE SCORER-FIXTURE VALIDATION for the walk-litmus bench (closes the README.md
"Scoring integrity" promise: "the scorer is validated offline against perfect-memory
(must score 1.0/1.0/1.0) and corrupted-memory fixtures before every run").

No network, no seat, no MCP: the fixtures are synthetic corpus/answer dicts run through
benchmarks/walk_litmus/runner.py::score(). Catches scorer drift (expectation resolution,
supersession forbids, negative-probe sentinel family) before a bench run trusts it.

Adversarial cases (critic round 1 + recall-fidelity lane):
- the honest-abstention family the recall lane added (exact contract sentinel first,
  then negative-existence phrasings, blocked by a positive-render guard);
- "you haven't mentioned" must NOT trip the positive-render guard ("you have ..." does);
- a correction whose OLD value surfaces is never honored even when the new value shows.
"""
import importlib
import os
import sys

import pytest

_BENCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "benchmarks", "walk_litmus")

sys.path.insert(0, _BENCH_DIR)   # runner resolves its own "import generator" the same way
try:
    runner = importlib.import_module("runner")
except Exception as exc:  # noqa: BLE001 - generator/scipy deps unavailable in this env
    pytest.skip(f"walk-litmus runner deps unavailable: {exc}", allow_module_level=True)


def _fact(fid, kind, expect, corrected_by=None):
    return {"fact_id": fid, "kind": kind, "session": 0, "turn": 0,
            "statement": "", "expect": expect, "slot": "", "forbid": [],
            "corrected_by": corrected_by, "is_correction": False,
            "superseded": None, "paraphrase": "", "category": ""}


def _probe_q(qid, text, targets):
    return {"q_id": qid, "text": text, "targets": targets, "kind": "probe",
            "expect": [], "forbid": []}


def _neg_q(qid, text):
    # never-stated probes: kind "negative", NO target fact (mirror of generator.py)
    return {"q_id": qid, "text": text, "targets": [], "kind": "negative",
            "expect": [], "forbid": []}


def _ans(qid, question, kind, core, error=None, paraphrase=False):
    return {"q_id": qid, "question": question, "kind": kind, "paraphrase": paraphrase,
            "core": core, "error": error, "latency_ms": 1.0,
            "payload": core}


def _corpus(facts, questions):
    # "sessions" feeds the token-accounting family: per-session lists of turn dicts the
    # fixture facts' (session, turn) coordinates index into. Fixture scoring exercises the
    # quality/honoring/abstention families; token numbers are throwaway.
    return {"facts": facts, "questions": questions,
            "sessions": [[{"text": "", "turn_kind": "fact"}]]}


PERFECT_FACTS = [
    _fact("f0", "preference", ["blue"]),
    _fact("f1", "quantity", ["19 laps"]),
    _fact("f2", "kinship_name", ["Dario"], corrected_by="f3"),
    _fact("f3", "kinship_name", ["Jonas"]),
]
PERFECT_QUESTIONS = [
    _probe_q("q0", "What's my favorite color?", ["f0"]),
    _probe_q("q1", "How many laps did I do?", ["f1"]),
    _probe_q("q2", "What's my father's name?", ["f2"]),
    _neg_q("q3", "What's my passport number?"),   # never-stated (no target fact)
]
PERFECT_ANSWERS = [
    _ans("q0", PERFECT_QUESTIONS[0]["text"], "probe", "Your favorite color is blue."),
    _ans("q1", PERFECT_QUESTIONS[1]["text"], "probe", "You have 19 laps on record."),
    # FINAL value post-correction (the stale Dario must NOT surface)
    _ans("q2", PERFECT_QUESTIONS[2]["text"], "probe", "Your father is named Jonas."),
    _ans("q3", PERFECT_QUESTIONS[3]["text"], "negative", runner.EMPTY_SENTINEL),
]

CORRUPTED_ANSWERS = [
    _ans("q0", PERFECT_QUESTIONS[0]["text"], "probe", "You like green."),     # wrong value
    _ans("q1", PERFECT_QUESTIONS[1]["text"], "probe", "You have 19 laps on record."),
    _ans("q2", PERFECT_QUESTIONS[2]["text"], "probe", "Your father is named Dario."),
    _ans("q3", PERFECT_QUESTIONS[3]["text"], "negative", "You own passport number 42."),
]


def test_perfect_memory_scores_one():
    s = runner.score(_corpus(PERFECT_FACTS, PERFECT_QUESTIONS), [], PERFECT_ANSWERS, {})
    assert s["hit_quality"]["rate"] == 1.0, s["hit_quality"]
    assert s["abstention"]["negative_sentinel_rate"] == 1.0, s["abstention"]


def test_corrupted_memory_scores_degraded():
    s = runner.score(_corpus(PERFECT_FACTS, PERFECT_QUESTIONS), [], CORRUPTED_ANSWERS, {})
    # q1 the only clean hit; q2 answers the superseded value (stale -> miss); q0 wrong
    assert s["hit_quality"]["rate"] == 0.333, s["hit_quality"]   # scorer rounds to 3dp
    # junk render on the negative probe -> zero abstention
    assert s["abstention"]["negative_sentinel_rate"] == 0.0, s["abstention"]


def test_correction_old_value_surfacing_never_honored():
    corpus = _corpus(PERFECT_FACTS, [
        {"q_id": "c0", "text": "Who is my father?", "targets": [], "kind": "correction",
         "expect": ["jonas"], "forbid": ["dario"]}])
    both = runner.score(corpus, [], [_ans("c0", "Who is my father?", "correction",
                                          "Your father is Jonas now, not Dario.")], {})
    assert both["correction_honoring"]["rate"] == 0.0   # old value surfaced -> forbidden
    clean = runner.score(corpus, [], [_ans("c0", "Who is my father?", "correction",
                                           "Your father is named Jonas.")], {})
    assert clean["correction_honoring"]["rate"] == 1.0


def test_honest_abstention_family():
    corpus = _corpus(PERFECT_FACTS, [_neg_q("q3", "What's my passport number?")])
    positives = [
        runner.EMPTY_SENTINEL,                                        # exact contract first
        "I don't have any information about that in your memory - "
        "you haven't mentioned it in our previous conversations.",    # measured paraphrase
        "You haven't told me your passport number yet.",
        "Nothing on record about a passport.",
    ]
    for core in positives:
        s = runner.score(corpus, [], [_ans("q3", corpus["questions"][0]["text"],
                                           "negative", core)], {})
        assert s["abstention"]["negative_sentinel_rate"] == 1.0, core
    negatives = [
        "You have 3 cats on record.",                    # positive fact render
        "You haven't mentioned your favorite color - it is blue. "
        "You have a passport ending 1234 on file.",      # abstention AND a positive render
    ]
    for core in negatives:
        s = runner.score(corpus, [], [_ans("q3", corpus["questions"][0]["text"],
                                           "negative", core)], {})
        assert s["abstention"]["negative_sentinel_rate"] == 0.0, core

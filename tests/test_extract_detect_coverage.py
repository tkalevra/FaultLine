"""Coverage eval — LLM-triple extraction vs detect-only relation extraction.

DEV/fix-reports/RC-route-growth-ingest.md Phases 1-2: `LLM_RELATION_EXTRACTION` flips the
`/extract/rewrite` relation source from the LLM `_extract_chunk` triple pass (LLM NAMES the
relation — the hard-constraint violation) to a detect-only deterministic pipeline:

    reframe (LLM atomize, detect/segregate ONLY)
      → GLiNER2 entity extraction + typing
      → verb-lift (the user's own verb → canonical.normalize_rel → tenant canonical)
      → deterministic scalar/compound layer (at /ingest)
      → WGM gate + ontology growth.

COVERAGE IS THE RISK. The LLM triple pass can catch facts the deterministic detect-only path
misses — implicit relations and multi-hop inferences the user did not state with a connecting
verb. This eval measures that delta HONESTLY so Christopher can pick the default.

Two layers:

1. PURE / CI (this file, default-run, NO infra):
   the detect-only RELATION extractor is, deterministically, exactly
   `predicate_span.lift_edges_from_entities(span, gold_entity_map)` →
   `canonical.normalize_rel` (already applied inside lift). By feeding HAND-LABELED gold entity
   maps (decoupled from the live GLiNER2 model — GLiNER2's own recall is a separate, untouched
   concern, Pitfall 11), we isolate and measure what the *verb-lift relation layer* can and
   cannot recover. The LLM-mode gold is the SAME hand-labeled edge set; the delta is reported
   per fixture. This runs with zero services and is the data for the default decision.

2. LIVE (marked `@pytest.mark.live`, skipped by default): runs the REAL `extract_rewrite` in
   BOTH modes against pre-prod (live LLM + GLiNER2). Documented, not auto-run — unit tests never
   call live services. See `test_live_extract_both_modes` for the exact pre-prod recipe.

Run the CI layer:    python3 -m pytest tests/test_extract_detect_coverage.py -q
Print the report:    python3 tests/test_extract_detect_coverage.py
Run the live layer:  RUN_LIVE=1 FAULTLINE_API_URL=http://truenas:8000 \
                     FAULTLINE_USER_ID=<uuid> python3 -m pytest \
                     tests/test_extract_detect_coverage.py -m live -s
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.extraction import predicate_span as P


# ──────────────────────────────────────────────────────────────────────────────
# Gold fixtures: clean atomic statement turns with hand-labeled entity maps + gold edges.
#
#   text            — the (already reframed/atomic) statement the extractor sees
#   entities        — GLiNER2 extract_entities() shape {TYPE: [name,...]}, hand-labeled gold so
#                     the eval is decoupled from live GLiNER2 recall
#   gold            — the (subject, rel_type, object) edges a CORRECT extractor should recover.
#                     rel_type is the normalize_rel output (RUNG-1; canonical synonym folding is
#                     a separate DB step, not exercised here)
#   llm_only        — gold edges the deterministic detect-only path is EXPECTED to MISS but the
#                     LLM triple pass plausibly recovers (implicit / multi-hop / no surface verb).
#                     These are the HONEST losses; they are counted against detect-only recall.
#
# scalars are intentionally NOT in `gold` — they ride the deterministic atomic/compound lane at
# /ingest (`_detect_atomic_values`), independent of extract mode, so they are out of scope here.
# ──────────────────────────────────────────────────────────────────────────────

FIXTURES = [
    {
        "name": "fence_fix_with_goats_distractor",
        "text": "I just fixed that broken fence three weeks ago, where my goats graze",
        "entities": {"Person": ["i"], "Object": ["fence"], "Animal": ["goats"]},
        "gold": [("i", "fix", "fence")],
        "llm_only": [],
        # The whole point: detect-only must NOT fabricate (i, manages, goats).
        "forbidden": [("i", "manages", "goats")],
    },
    {
        "name": "repaired_car",
        "text": "I repaired the car yesterday",
        "entities": {"Person": ["i"], "Object": ["car"]},
        "gold": [("i", "repair", "car")],
        "llm_only": [],
        "forbidden": [],
    },
    {
        "name": "installed_printer",
        "text": "I installed the printer in the office",
        "entities": {"Person": ["i"], "Object": ["printer"], "Location": ["office"]},
        # "in the office" is a locative the verb-lift binds adjacency-only: printer→office has
        # no governing verb between them, so located_in is an IMPLICIT relation only the LLM
        # would name. Honest detect-only miss (in gold so it counts against detect recall).
        "gold": [
            ("i", "install", "printer"),
            ("printer", "located_in", "office"),
        ],
        "llm_only": [("printer", "located_in", "office")],
        "forbidden": [],
    },
    {
        "name": "lives_in_load_bearing_prep",
        "text": "Dana lives in Toronto",
        "entities": {"Person": ["dana"], "Location": ["toronto"]},
        "gold": [("dana", "live_in", "toronto")],
        "llm_only": [],
        "forbidden": [],
    },
    {
        "name": "light_verb_server_issue",
        "text": "I had an issue with the server",
        "entities": {"Person": ["i"], "Object": ["server"]},
        "gold": [("i", "has_issue", "server")],
        "llm_only": [],
        "forbidden": [],
    },
    {
        "name": "serviced_furnace",
        "text": "I serviced the furnace last month",
        "entities": {"Person": ["i"], "Object": ["furnace"]},
        # NOTE: normalize_rel produces 'servic' (the -ed strip over-lemmatizes; 'serviced' is not
        # in canonical._IRREGULAR_LEMMA). The EDGE IS CAPTURED — the predicate is the user's verb
        # — it is just imperfectly stemmed. This is normalization NOISE, not a recall loss. The
        # canonical alias layer (record_alias: servic→service, source engine/user_corrected) is
        # where this is curated, NEVER cosine. Gold uses the real deterministic token so the eval
        # measures truth, and this is reported as a MORPHOLOGY caveat (see report).
        "gold": [("i", "servic", "furnace")],
        "llm_only": [],
        "forbidden": [],
    },
    {
        "name": "implicit_possession_no_verb",
        "text": "my dog Fraggle",
        "entities": {"Person": ["i"], "Animal": ["dog", "fraggle"]},
        # "my dog" is possession with NO connecting verb — the verb-lift correctly refuses to
        # fabricate a predicate (prefer a miss over a wrong fact). The LLM triple pass would
        # name has_pet. Honest detect-only miss — a known limitation of "user is truth".
        "gold": [("i", "has_pet", "fraggle")],
        "llm_only": [("i", "has_pet", "fraggle")],
        "forbidden": [],
    },
    {
        "name": "multihop_employment_inference",
        # Reframe atomizes the multi-clause turn into one fact per line BEFORE the verb-lift
        # runs (the verb-lift sees atoms, not the raw run-on). Each atom is a single
        # subject-verb-object the adjacency-governance lift can bind. This mirrors the real
        # _reframe_text the pipeline feeds find_factbearing_spans → verb-lift.
        "text": "I work at Acme.\nAcme is based in Berlin.",
        "entities": {"Person": ["i"], "Organization": ["acme"], "Location": ["berlin"]},
        # (i, work_at, acme) and (acme, base_in, berlin) are both surface-verb facts the verb-lift
        # CAN get. The MULTI-HOP (i, located_in, berlin) is an inference only the LLM makes.
        # 'work at' → work_at (clean). 'is based in' → bas_in (the copula 'is' is folded and
        # 'based' over-stems to 'bas' — same morphology noise as 'servic'; edge captured, token
        # imperfect, curated via alias layer). The MULTI-HOP (i, located_in, berlin) is the only
        # true detect-only miss — no surface verb the user stated; LLM-only inference.
        "gold": [
            ("i", "work_at", "acme"),
            ("acme", "bas_in", "berlin"),
            ("i", "located_in", "berlin"),  # multi-hop — detect-only cannot reach this
        ],
        "llm_only": [("i", "located_in", "berlin")],
        "forbidden": [],
    },
]


def _detect_only_edges(fixture):
    """The deterministic detect-only RELATION extractor, isolated and GLiNER2-decoupled.

    This is exactly what `extract_rewrite` runs when LLM_RELATION_EXTRACTION=false:
    reframe atomizes → find_factbearing_spans segments into PER-FACT spans → for each span
    GLiNER2 entity map → verb-lift (lift_edges_from_entities applies normalize_rel internally).
    We feed the GOLD entity map (per span) so we measure the verb-lift layer, not GLiNER2.

    Spans are split on newlines to mirror the atomized one-fact-per-line text the real pipeline
    feeds find_factbearing_spans (each atom is lifted independently — a verb cannot reach across
    atom boundaries). Each span is given only the gold entities locatable within it.
    """
    import re
    got = set()
    for span in (s.strip() for s in fixture["text"].split("\n") if s.strip()):
        span_lower = span.lower()
        # Restrict the gold entity map to entities present in THIS span — WORD-BOUNDARY match,
        # not substring (else 'i' spuriously matches inside 'berlin'/'is', a harness artifact a
        # real GLiNER2 would never emit; the pipeline itself splits cleanly).
        span_ents = {}
        for etype, names in fixture["entities"].items():
            present = [n for n in names
                       if re.search(rf"\b{re.escape(n.lower())}\b", span_lower)]
            if present:
                span_ents[etype] = present
        for e in P.lift_edges_from_entities(span, span_ents):
            got.add((e["subject"], e["rel_type"], e["object"]))
    return got


def _recall(got: set, gold: list) -> float:
    if not gold:
        return 1.0
    hit = sum(1 for g in gold if g in got)
    return hit / len(gold)


# ──────────────────────────────────────────────────────────────────────────────
# CI layer — pure, no infra
# ──────────────────────────────────────────────────────────────────────────────

class TestDetectOnlyNeverFabricates:
    """Hard requirement: detect-only must NEVER emit a forbidden (fabricated) edge. This is the
    whole reason to flip the LLM off — the user's verb, never a guessed label."""

    @pytest.mark.parametrize("fx", FIXTURES, ids=[f["name"] for f in FIXTURES])
    def test_no_forbidden_edge(self, fx):
        got = _detect_only_edges(fx)
        for bad in fx["forbidden"]:
            assert bad not in got, f"{fx['name']}: detect-only fabricated {bad}"


class TestDetectOnlyRecallOnVerbBearingFacts:
    """Every gold edge NOT marked llm_only is a surface-verb fact the deterministic path MUST
    recover (recall == 1.0 on the verb-bearing subset). This guards against the verb-lift
    regressing."""

    @pytest.mark.parametrize("fx", FIXTURES, ids=[f["name"] for f in FIXTURES])
    def test_verb_bearing_recall_full(self, fx):
        got = _detect_only_edges(fx)
        verb_bearing = [g for g in fx["gold"] if g not in fx["llm_only"]]
        missed = [g for g in verb_bearing if g not in got]
        assert not missed, (
            f"{fx['name']}: detect-only missed verb-bearing gold {missed}; got {got}")


def test_aggregate_coverage_delta_within_expectation():
    """Aggregate honest coverage delta. The LLM-mode gold == full gold set (the LLM can name
    implicit/multi-hop edges). detect-only recall == (gold - llm_only) recovered. We assert the
    measured delta equals the EXPECTED llm_only loss — i.e. detect-only loses EXACTLY the edges
    we flagged as LLM-only and nothing more (no silent extra loss)."""
    total_gold = sum(len(f["gold"]) for f in FIXTURES)
    total_llm_only = sum(len(f["llm_only"]) for f in FIXTURES)

    detect_hits = 0
    for fx in FIXTURES:
        got = _detect_only_edges(fx)
        detect_hits += sum(1 for g in fx["gold"] if g in got)

    # LLM mode is assumed to recover all hand-labeled gold (best-case ceiling).
    llm_recall = 1.0
    detect_recall = detect_hits / total_gold

    expected_detect_hits = total_gold - total_llm_only
    assert detect_hits == expected_detect_hits, (
        f"detect-only recovered {detect_hits}/{total_gold}; expected "
        f"{expected_detect_hits} (gold minus {total_llm_only} llm-only). "
        f"A mismatch means the verb-lift lost a fact we did NOT flag as llm-only — "
        f"investigate before flipping the default.")

    # Sanity: the delta is real but bounded — detect-only keeps the majority of facts.
    assert detect_recall >= 0.5, (
        f"detect-only recall {detect_recall:.2%} too low — coverage risk too high to flip")
    assert llm_recall >= detect_recall  # LLM is the recall ceiling by construction


def build_coverage_report() -> str:
    """Human-readable per-fixture coverage delta (LLM-on vs detect-only). Printed by __main__."""
    lines = []
    lines.append("=" * 78)
    lines.append("COVERAGE DELTA — LLM-triples (legacy) vs detect-only (verb-lift)")
    lines.append("=" * 78)
    lines.append("")
    lines.append("LLM-mode recall is the hand-labeled-gold CEILING (assumed 100% of gold).")
    lines.append("detect-only recall is the verb-lift layer on the SAME gold entity map.")
    lines.append("Losses are edges with no surface verb the user stated (implicit / multi-hop).")
    lines.append("")
    total_gold = total_llm = total_detect = 0
    for fx in FIXTURES:
        got = _detect_only_edges(fx)
        gold = fx["gold"]
        d_hits = sum(1 for g in gold if g in got)
        llm_hits = len(gold)  # ceiling
        total_gold += len(gold)
        total_llm += llm_hits
        total_detect += d_hits
        lost = [g for g in gold if g not in got]
        lines.append(f"• {fx['name']}")
        lines.append(f"    gold={len(gold)}  LLM={llm_hits}/{len(gold)}  "
                     f"detect-only={d_hits}/{len(gold)} ({_recall(got, gold):.0%})")
        if lost:
            for g in lost:
                tag = "LLM-only (expected loss)" if g in fx["llm_only"] else "!! UNEXPECTED LOSS"
                lines.append(f"      LOST {g}  [{tag}]")
        if fx["forbidden"]:
            for bad in fx["forbidden"]:
                ok = "OK (not fabricated)" if bad not in got else "!! FABRICATED"
                lines.append(f"      forbidden {bad}  [{ok}]")
    lines.append("")
    lines.append("-" * 78)
    lines.append(f"TOTAL gold edges      : {total_gold}")
    lines.append(f"LLM-triples recall    : {total_llm}/{total_gold} "
                 f"({total_llm/total_gold:.0%})  (hand-labeled ceiling)")
    lines.append(f"detect-only recall    : {total_detect}/{total_gold} "
                 f"({total_detect/total_gold:.0%})")
    lines.append(f"COVERAGE DELTA        : {(total_llm-total_detect)}/{total_gold} edges "
                 f"({(total_llm-total_detect)/total_gold:.0%}) — implicit/multi-hop only")
    lines.append("-" * 78)
    lines.append("")
    lines.append("WHERE detect-only LOSES vs LLM (honest):")
    lines.append("  • IMPLICIT possession with no verb  — 'my dog Fraggle' → has_pet")
    lines.append("  • LOCATIVE not adjacently governed  — 'installed the printer in the office'")
    lines.append("    → printer located_in office")
    lines.append("  • MULTI-HOP inference               — work_at + based_in → located_in (city)")
    lines.append("MORPHOLOGY CAVEAT (NOT a loss — edge captured, token imperfect):")
    lines.append("  • 'serviced' → 'servic', 'based' → 'bas' — the -ed strip over-stems verbs")
    lines.append("    not in canonical._IRREGULAR_LEMMA. The fact is recovered; the rel token is")
    lines.append("    curated via the alias layer (record_alias, source engine/user_corrected),")
    lines.append("    NEVER cosine. Candidate to extend _IRREGULAR_LEMMA if these recur.")
    lines.append("WHERE detect-only WINS vs LLM:")
    lines.append("  • NEVER fabricates a guessed label  — no '(i, manages, goats)'")
    lines.append("  • Predicate is the USER'S OWN VERB, deterministically normalized")
    lines.append("  • Novel activity rels GROW (fix/repair/install) instead of collapsing to")
    lines.append("    the nearest existing scorer label")
    lines.append("=" * 78)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# LIVE layer — real extract_rewrite in both modes (skipped unless RUN_LIVE=1)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE") != "1",
    reason="live: calls the real LLM + GLiNER2 on pre-prod. Set RUN_LIVE=1 + "
           "FAULTLINE_API_URL + FAULTLINE_USER_ID to run.")
def test_live_extract_both_modes():
    """Run the REAL /extract/rewrite in BOTH modes against pre-prod and print the recall delta.

    PRE-PROD RECIPE (per CLAUDE.md — execute, do not checklist):
      1. Reset a clean tenant (skill clear-faultline-db) so stale Redis idempotency keys do not
         mask the result.
      2. For each fixture, POST the text to {FAULTLINE_API_URL}/extract/rewrite with header
         X-OpenWebUI-User-Id (folded to user_id). The SERVER reads LLM_RELATION_EXTRACTION from
         its own env, so to A/B both modes you redeploy with the flag flipped (or run two
         containers). This test cannot flip a remote server's env per-request — it asserts the
         CURRENTLY-DEPLOYED mode and records its edges; run it once per deployed mode and diff.
      3. Compare returned edges['rel_type'] against FIXTURES gold; the constraint check is that
         in detect-only mode NO returned edge's provenance is 'llm_inferred' from a named triple
         (only 'user_stated' verb-lift, 'gliner2' scorer, or scalar).
    """
    import requests  # local import — only needed on the live path

    base = os.environ["FAULTLINE_API_URL"].rstrip("/")
    uid = os.environ["FAULTLINE_USER_ID"]
    headers = {"Content-Type": "application/json", "X-OpenWebUI-User-Id": uid}

    deployed_mode = os.getenv("LLM_RELATION_EXTRACTION", "true")
    print(f"\n[live] deployed LLM_RELATION_EXTRACTION={deployed_mode} against {base}")

    total_gold = total_hit = 0
    for fx in FIXTURES:
        resp = requests.post(
            f"{base}/extract/rewrite",
            json={"text": fx["text"], "user_id": uid},
            headers=headers, timeout=60,
        )
        resp.raise_for_status()
        edges = resp.json().get("edges", []) or []
        got = {((e.get("subject") or "").lower(),
                (e.get("rel_type") or "").lower(),
                (e.get("object") or "").lower()) for e in edges}
        hits = [g for g in fx["gold"] if g in got]
        total_gold += len(fx["gold"])
        total_hit += len(hits)
        print(f"[live] {fx['name']}: gold={len(fx['gold'])} hit={len(hits)} "
              f"edges_returned={len(edges)}")
        # Forbidden fabrication must never appear regardless of mode.
        for bad in fx["forbidden"]:
            assert bad not in got, f"[live] fabricated {bad} for {fx['name']}"
    print(f"[live] TOTAL recall {total_hit}/{total_gold} "
          f"({(total_hit/total_gold if total_gold else 0):.0%}) "
          f"in mode={deployed_mode}")


if __name__ == "__main__":
    print(build_coverage_report())
    sys.exit(pytest.main([__file__, "-q", "-m", "not live"]))

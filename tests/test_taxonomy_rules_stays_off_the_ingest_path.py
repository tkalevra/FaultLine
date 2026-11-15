"""`_apply_taxonomy_rules` has no in-flow caller, and that is the DECISION — pin it.

`520a74bf` deleted the single call `rows = _apply_taxonomy_rules(rows, req.user_id, db)` from
`/ingest`. That commit's own `DeepSeek/Slowness-Fix/latency_investigation.md` records the reason
WITH its measurement: the function's synchronous `_llm_discover_taxonomy_from_facts()` cost
"10s per unique rel_type in batch", and the turn "alice manages, bob supervises, charlie mentors"
went 22.9s → 11.9s when the sync LLM calls left the hot path.

Re-measured 2026-08-26 on the live rig, driving the real function against a real tenant:

  * COST — still synchronous, still per-rel_type: 0.7–1.5 s each; 4.2 s for that same 3-rel
    turn, 6.3 s over 7. The SaaS target calls a REMOTE tenant BYO brain on a 10 s timeout.
  * YIELD — 1 placement in 7, and it was ``manages -> work``: the same ``work`` attractor
    ``c2f4f1b9`` gated ``TAXONOMY_TYPE_MATCH_PLACEMENT`` OFF for. Six declined.
  * IT HAS NEVER WRITTEN A ROW — ``entity_taxonomies.source='engine_learned_ingest'`` is 0 in
    public and 0 across all 7 live tenants.

And the half worth keeping was SUBSTITUTED, not lost: the row annotation is done live by
``FactStoreManager.commit()`` on the bound per-tenant cursor, no LLM.

So this suite pins the ABSENCE plus the two facts that absence rests on. If a future session
restores the caller, or the substitute disappears, or the sync LLM call becomes async (which
would genuinely change the calculus), exactly one of these goes red and names what changed.
"""
import inspect
import re
from pathlib import Path

import src.api.main as main

REPO = Path(__file__).resolve().parent.parent
_FN = "_apply_taxonomy_rules"


def _call_sites():
    """Every `_apply_taxonomy_rules(` occurrence in src/ that is not its own definition."""
    hits = []
    for path in sorted((REPO / "src").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if f"{_FN}(" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith(f"def {_FN}("):
                continue
            if stripped.startswith("#"):        # the header block / prose references
                continue
            hits.append((str(path.relative_to(REPO)), i, stripped))
    return hits


def test_the_ingest_path_has_no_caller():
    """ABLATION: re-add `rows = _apply_taxonomy_rules(rows, req.user_id, db)` → RED."""
    hits = _call_sites()
    assert not hits, (
        "_apply_taxonomy_rules was re-wired. It makes a SYNCHRONOUS per-rel_type LLM call "
        "(0.7-1.5 s each, measured 2026-08-26), overwrites the PROCESS-GLOBAL _TAXONOMY_CACHE "
        "with one tenant's rows, and its LLM placement predicate files `manages` under `work` "
        "— the attractor c2f4f1b9 gated off. Read the header block above the def. "
        f"Call sites found: {hits}"
    )


def test_the_latency_reason_is_still_the_live_reason():
    """The deletion's justification is only valid while the call is still sync + in-flow.

    ABLATION: make the discovery call async/deferred inside the function and this goes RED —
    correctly, because the cost half of the argument would no longer hold and the decision
    would deserve re-litigating on the placement half alone.
    """
    src = inspect.getsource(main._apply_taxonomy_rules)
    assert "_llm_discover_taxonomy_from_facts(" in src, (
        "the synchronous LLM discovery call is gone from _apply_taxonomy_rules — the measured "
        "latency half of the no-restore decision no longer applies; re-evaluate it"
    )
    assert "await " not in src and "asyncio" not in src, (
        "the discovery call became async — re-read the header block and re-decide"
    )


def test_the_global_taxonomy_cache_it_would_clobber_is_still_process_global():
    """Reason 3: `_load_taxonomy_cache` writes a cache with NO tenant dimension."""
    assert isinstance(main._TAXONOMY_CACHE, dict)
    src = inspect.getsource(main._load_taxonomy_cache)
    assert "global _TAXONOMY_CACHE" in src
    assert "search_path" not in src and "schema" not in src, (
        "_load_taxonomy_cache grew a tenant dimension — reason 3 of the no-restore decision "
        "changed; re-read the header block"
    )


def test_the_row_annotation_half_is_owned_by_the_fact_store():
    """Reason 2: the substitute is live, per-tenant and LLM-free.

    ABLATION: delete the entity_taxonomies lookup from FactStoreManager.commit() → RED, which
    is the correct signal: the annotation would then genuinely be missing and the in-flow
    question reopens (on the annotation half only, never on LLM discovery).
    """
    store = (REPO / "src" / "fact_store" / "store.py").read_text(encoding="utf-8")
    m = re.search(
        r"if not taxonomies:.*?"
        r"SELECT array_agg\(DISTINCT taxonomy_name\) FROM entity_taxonomies\s*\"?\s*"
        r"\"?\s*WHERE rel_types_defining_group @> ARRAY",
        store, re.S)
    assert m, (
        "FactStoreManager.commit() no longer annotates rows from entity_taxonomies — the "
        "substitute that made _apply_taxonomy_rules's step 5 redundant is gone"
    )
    # …and it must stay LLM-free: this is the whole reason it is allowed to run in-flow.
    window = store[m.start():m.start() + 1200]
    assert "llm" not in window.lower(), "an LLM call appeared in the in-flow annotation lane"

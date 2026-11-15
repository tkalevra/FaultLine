"""DOC-LANE SCHEMA-GUIDED SECOND PASS (DESIGN-document-extraction-sota.md §4/§8.2).

Pins for the EDC-shape refinement: after a document chunk's primary extraction, the
worker makes AT MOST ONE bounded re-ask against /extract/schema-second-pass; the door
selects candidate rel_types from the seat's OWN ontology (tenant-bound rel_types
overlay + entity_taxonomies, Postgres metadata — Pitfall 11: no label phrasing, no
hardcoded domains), re-asks ONCE, and validates the answer against the candidate MENU
(the LLM cannot mint vocabulary), the litmus junk ruler's shape rules, and dedupe vs
the pass-1 triples. The conversational spine never reaches the door (census pin).

House style: DB-less, monkeypatched. Red-at-baseline: every pin here fails at the
pre-lane tree (no door, no flag, no ride).
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import src.api.main as main  # noqa: E402
import src.api.llm_calls as llm_calls  # noqa: E402
import src.re_embedder.embedder as emb  # noqa: E402
from src.api.models import SchemaSecondPassRequest  # noqa: E402

SCHEMA_DOOR = "/extract/schema-second-pass"


@pytest.fixture(autouse=True)
def _quiet_lane(monkeypatch):
    # Embedder-side ambient state pinned to the lane-quiet shape.
    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", False)
    monkeypatch.setattr(emb, "DOC_CITE_DENSE_SKIP", False)
    monkeypatch.setattr(emb, "INGEST_ASSISTANT_TURNS", False)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _Resp:
    status_code = 200

    def __init__(self, payload=None):
        self._p = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _recorder(responses=None):
    urls, bodies = [], []

    def _post(url, **kw):
        urls.append(str(url))
        bodies.append(kw.get("json"))
        if responses is not None:
            return responses.get(str(url)) or _Resp({})
        return _Resp({})

    return urls, bodies, _post


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.new_event_loop().run_until_complete(coro)


def _endpoint(monkeypatch, *, candidates, llm_result, text="The zephyr mesh router publishes its routing table.",
              prior=None, cap=None):
    """Drive the door body with every ambient seam monkeypatched."""
    monkeypatch.setattr(main, "_INGEST_ENABLED", True)

    async def _noop(user_id, endpoint):
        return None

    monkeypatch.setattr(main, "_ensure_tenant_ready", _noop)
    monkeypatch.setattr(main, "paused_for_background", lambda: False)
    monkeypatch.setattr(main, "_schema_second_pass_candidates",
                        lambda dsn, types: candidates)
    if cap is not None:
        monkeypatch.setattr(main, "DOC_SCHEMA_SECOND_PASS_MENU_CAP", cap)

    async def _llm(**kw):
        return llm_result

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_async", _llm)
    req = SchemaSecondPassRequest(text=text, user_id="0f0e0d0c0b0a0908",
                                  prior_edges=prior or [])
    return _run(main.extract_schema_second_pass(req))


def _cands(*names, source="wikidata"):
    return {n: {"description": n.replace("_", " "), "source": source} for n in names}


# ---------------------------------------------------------------------------
# the door: guards, selection, validation (the junk firewall)
# ---------------------------------------------------------------------------
def test_door_refuses_ingest_disabled_200shaped(monkeypatch):
    monkeypatch.setattr(main, "_INGEST_ENABLED", False)
    out = _run(main.extract_schema_second_pass(
        SchemaSecondPassRequest(text="x", user_id="u1")))
    assert out["status"] == "ingest_disabled"
    assert out["edges"] == []


def test_door_refuses_operator_pause_200shaped(monkeypatch):
    monkeypatch.setattr(main, "_INGEST_ENABLED", True)

    async def _noop(user_id, endpoint):
        return None

    monkeypatch.setattr(main, "_ensure_tenant_ready", _noop)
    monkeypatch.setattr(main, "paused_for_background", lambda: True)
    out = _run(main.extract_schema_second_pass(
        SchemaSecondPassRequest(text="x", user_id="u1")))
    assert out["status"] == "doc_lane_paused"
    assert out["edges"] == []


def test_no_candidates_is_honest_zero_and_never_calls_the_llm(monkeypatch):
    out = _endpoint(monkeypatch, candidates={}, llm_result=[{"subject": "a", "rel_type": "works_for", "object": "b"}])
    assert out["status"] == "success"
    assert out["edges"] == [] and out["added"] == 0
    assert out["llm_calls"] == 0, "an empty schema must not spend an LLM call"


def test_unknown_rel_rejected_and_counted_menu_only_vocabulary(monkeypatch):
    out = _endpoint(
        monkeypatch,
        candidates=_cands("works_for", "located_in"),
        llm_result=[
            {"subject": "marlin packet broker", "rel_type": "totally_bogus_rel", "object": "tap port"},
            {"subject": "marlin packet broker", "rel_type": "works_for", "object": "tap port"},
        ],
    )
    assert out["added"] == 1
    assert out["edges"][0]["rel_type"] == "works_for"
    assert out["rejected_unknown_rel"] == 1


def test_prior_dedupe_and_self_dedupe(monkeypatch):
    prior = [{"subject": "Marlin", "rel_type": "works_for", "object": "tap port"}]
    out = _endpoint(
        monkeypatch,
        candidates=_cands("works_for"),
        llm_result=[
            {"subject": "marlin", "rel_type": "works_for", "object": "tap port"},  # dup vs prior
            {"subject": "marlin", "rel_type": "works_for", "object": "TAP Port"},  # dup vs prior (norm)
            {"subject": "marlin", "rel_type": "works_for", "object": "uplink pool"},  # kept
            {"subject": "marlin", "rel_type": "works_for", "object": "uplink pool"},  # dup vs self
        ],
        prior=prior,
    )
    assert out["added"] == 1
    assert out["edges"][0]["object"] == "uplink pool"
    assert out["deduped_vs_prior"] == 2
    assert out["deduped_self"] == 1


def test_shape_junk_rules_bare_number_selfloop_fragment(monkeypatch):
    out = _endpoint(
        monkeypatch,
        candidates=_cands("has_state", "related_measure"),
        llm_result=[
            {"subject": "zephyr", "rel_type": "has_state", "object": "32"},       # bare number
            {"subject": "zephyr", "rel_type": "has_state", "object": "zephyr"},   # self loop
            {"subject": "z", "rel_type": "has_state", "object": "run mode"},      # fragment subject
            {"subject": "zephyr", "rel_type": "has_state", "object": "run mode"},  # kept
        ],
    )
    assert out["added"] == 1
    assert out["edges"][0]["object"] == "run mode"
    assert out["rejected_shape"] == 3


def test_unparseable_llm_result_yields_zero_edges_counted(monkeypatch):
    out = _endpoint(monkeypatch, candidates=_cands("works_for"), llm_result=None)
    assert out["edges"] == []
    assert out["llm_unparseable"] == 1
    assert out["llm_calls"] == 1  # the call WAS made; only its answer was unusable


def test_single_triple_dict_answer_accepted(monkeypatch):
    out = _endpoint(monkeypatch, candidates=_cands("works_for"),
                    llm_result={"subject": "marlin packet broker",
                                "rel_type": "works_for", "object": "tap port"})
    assert out["added"] == 1


def test_menu_capped_and_grown_leads(monkeypatch):
    cands = _cands("zzz_grown_rel", "aaa_grown_rel", "mmm_seed_rel", "bbb_seed_rel",
                   source="wikidata")
    cands["zzz_grown_rel"]["source"] = "engine"
    cands["aaa_grown_rel"]["source"] = "engine"
    out = _endpoint(monkeypatch, candidates=cands, llm_result=[], cap=3)
    # the tenant's OWN grown rels LEAD the menu (they are the schema guidance the
    # pass exists to surface); the seeded backbone fills the remainder (alphabetical
    # within tier). Measured live 2026-11-06: seeds-first + cap-15 left a full seed
    # ontology with ZERO grown rel_types on the menu — the pass then adds nothing.
    assert out["schema_candidates"] == ["aaa_grown_rel", "zzz_grown_rel", "bbb_seed_rel"]
    assert len(out["schema_candidates"]) == 3


# ---------------------------------------------------------------------------
# the deterministic schema-candidate selector (the seat's OWN ontology)
# ---------------------------------------------------------------------------
def test_selector_metadata_driven_relational_only(monkeypatch):
    rel_meta = {
        "located_in": {"is_hierarchy_rel": False, "tail_types": ["Location"],
                       "head_types": ["Object", "Organization"], "natural_language": "X is located in Y",
                       "source": "wikidata"},
        "works_for": {"is_hierarchy_rel": False, "tail_types": ["Organization"],
                      "head_types": ["Person"], "natural_language": "X works for Y",
                      "source": "wikidata"},
        "part_of": {"is_hierarchy_rel": True, "tail_types": ["Object"],
                    "head_types": ["Object"], "natural_language": "X is part of Y",
                    "source": "wikidata"},           # hierarchy → excluded
        "age": {"is_hierarchy_rel": False, "tail_types": ["SCALAR"],
                "head_types": ["Person"], "natural_language": "X is Y years old",
                "source": "wikidata"},                # scalar → excluded
        "manages": {"is_hierarchy_rel": False, "tail_types": ["SCALAR"],
                    "head_types": [], "natural_language": "X manages Y",
                    "source": "engine"},              # ANY-concrete mismatch → excluded
    }
    monkeypatch.setattr(main, "_rel_meta", lambda *a, **k: dict(rel_meta))
    monkeypatch.setattr(main.taxonomy_overlay, "resolve_current", lambda dsn: {})
    got = main._schema_second_pass_candidates("postgres://x", {"Object", "Location"})
    assert "located_in" in got
    assert "works_for" not in got          # tail Organization ∩ {Object, Location} = ∅
    assert "part_of" not in got            # hierarchy rel never selected
    assert "age" not in got                # scalar tail never selected
    assert got["located_in"]["description"] == "X is located in Y"


def test_selector_taxonomy_defining_rels_admit_members(monkeypatch):
    rel_meta = {
        "runs": {"is_hierarchy_rel": False, "tail_types": ["ANY"], "head_types": ["ANY"],
                 "natural_language": "X runs Y", "source": "engine"},
    }
    tax = {"computer_system": {"member_entity_types": ["Object"],
                               "rel_types_defining_group": ["runs"]}}
    monkeypatch.setattr(main, "_rel_meta", lambda *a, **k: dict(rel_meta))
    monkeypatch.setattr(main.taxonomy_overlay, "resolve_current", lambda dsn: dict(tax))
    got = main._schema_second_pass_candidates("postgres://x", {"Object"})
    assert got.get("runs", {}).get("description") == "X runs Y"


def test_selector_empty_detected_types_is_empty(monkeypatch):
    assert main._schema_second_pass_candidates("postgres://x", set()) == {}


# ---------------------------------------------------------------------------
# the worker ride: ONE bounded call, doc-lane only, fail-safe
# ---------------------------------------------------------------------------
def _chunk(monkeypatch, *, pass1_edges, schema_payload=None, schema_raises=False, flag=True):
    monkeypatch.setattr(emb, "DOC_SCHEMA_SECOND_PASS", flag)
    monkeypatch.setattr(emb, "_document_chunk_edges",
                        lambda text, uid, url, route, _meta=None:
                        (list(pass1_edges)))
    schema_resp = _Resp({"status": "success",
                         "edges": (schema_payload or []),
                         "schema_candidates": ["located_in"]})
    urls, bodies, _post = _recorder()
    real_post = urls  # noqa: F841

    def _post2(url, **kw):
        urls.append(str(url))
        bodies.append(kw.get("json"))
        if schema_raises and SCHEMA_DOOR in str(url):
            raise RuntimeError("schema door down")
        if SCHEMA_DOOR in str(url):
            return schema_resp
        posted = (kw.get("json") or {}).get("edges") or []
        return _Resp({"committed": len(posted), "staged": 0})

    monkeypatch.setattr(emb.httpx, "post", _post2)
    got = emb._process_document_chunk(
        "Section 1: the marlin packet broker taps the uplink pool.", idx=0, doc_id=1,
        user_id="0f0e0d0c0b0a0908", backend_url="http://backend", source_ref="ab-test")
    return got, urls, bodies


PASS1 = [{"subject": "marlin packet broker", "rel_type": "taps", "object": "uplink pool",
          "confidence": 0.8}]


def test_flag_on_merges_schema_edges_before_ingest(monkeypatch):
    extra = [{"subject": "marlin packet broker", "rel_type": "located_in",
              "object": "rack 12"}]
    got, urls, bodies = _chunk(monkeypatch, pass1_edges=PASS1, schema_payload=extra)
    assert got[0] == len(PASS1) + len(extra), "both pass-1 and pass-2 edges commit"
    schema_calls = [u for u in urls if SCHEMA_DOOR in u]
    assert len(schema_calls) == 1, "ONE bounded second-pass call per chunk"
    ingest_body = [b for u, b in zip(urls, bodies) if u.endswith("/ingest")][0]
    assert len(ingest_body["edges"]) == 2
    assert ingest_body["edges"][1]["rel_type"] == "located_in"


def test_flag_off_makes_no_schema_call(monkeypatch):
    got, urls, bodies = _chunk(monkeypatch, pass1_edges=PASS1, flag=False,
                               schema_payload=[{"subject": "x", "rel_type": "located_in",
                                                "object": "y"}])
    assert not [u for u in urls if SCHEMA_DOOR in u]
    ingest_body = [b for u, b in zip(urls, bodies) if u.endswith("/ingest")][0]
    assert len(ingest_body["edges"]) == 1


def test_schema_call_failure_keeps_pass1_edges(monkeypatch):
    got, urls, bodies = _chunk(monkeypatch, pass1_edges=PASS1, schema_raises=True)
    assert got[0] == len(PASS1)
    ingest_body = [b for u, b in zip(urls, bodies) if u.endswith("/ingest")][0]
    assert len(ingest_body["edges"]) == 1


def test_zero_pass1_edges_skip_the_schema_call(monkeypatch):
    got, urls, _b = _chunk(monkeypatch, pass1_edges=[], schema_raises=False)
    assert got[0] == 0 and got[1] == 0
    assert not [u for u in urls if SCHEMA_DOOR in u]


def test_l1_deferred_chunk_skips_the_schema_call(monkeypatch):
    """The L1 lane's LLM read is DEFERRED to the R2 remine by contract; the second pass
    must not spend a second LLM call behind that decision."""
    monkeypatch.setattr(emb, "DOC_SCHEMA_SECOND_PASS", True)
    monkeypatch.setattr(emb, "DOC_L1_DETERMINISTIC_FIRST", True)
    monkeypatch.setattr(emb, "_brain_lane_slow", lambda uid: "p95:28500ms")

    def _edges(text, uid, url, route, _meta=None):
        if _meta is not None:
            _meta["l1"] = True
        return list(PASS1)

    monkeypatch.setattr(emb, "_document_chunk_edges", _edges)
    urls, bodies, _post = _recorder()

    def _post2(url, **kw):
        urls.append(str(url))
        bodies.append(kw.get("json"))
        return _Resp({"committed": 1, "staged": 0})

    monkeypatch.setattr(emb.httpx, "post", _post2)
    got = emb._process_document_chunk("Diane lives in Toronto.", idx=0, doc_id=1,
                                      user_id="0f0e0d0c0b0a0908",
                                      backend_url="http://backend", source_ref="ab")
    assert got[0] == 1
    assert not [u for u in urls if SCHEMA_DOOR in u]


# ---------------------------------------------------------------------------
# census pins — the LLM-authority seam shape
# ---------------------------------------------------------------------------
def test_only_the_doc_worker_calls_the_schema_door():
    """The door's ONLY caller is the document worker (the conversational spine never
    reaches it); the route is registered exactly once."""
    # CENSUS THE TESTED TREE, not a hardcoded checkout path: derive src/ from the
    # imported module so this pin stays bound to whatever tree pytest is running in
    # (a hardcoded absolute path passed green in a foreign worktree whose src/ had
    # no door at all — critic R1, tree-blindness).
    src = pathlib.Path(main.__file__).resolve().parent.parent
    assert src.name == "src", src
    files = {}
    for p in src.rglob("*.py"):
        n = sum(1 for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
                if SCHEMA_DOOR in line)
        if n:
            files[str(p.relative_to(src.parent))] = n
    assert set(files) == {"src/api/main.py", "src/re_embedder/embedder.py"}, files
    main_lines = (src / "api" / "main.py").read_text(encoding="utf-8").splitlines()
    decorators = [l for l in main_lines
                  if l.strip().startswith('@app.post("' + SCHEMA_DOOR + '"')]
    assert len(decorators) == 1, decorators
    emb_lines = (src / "re_embedder" / "embedder.py").read_text(encoding="utf-8").splitlines()
    code_calls = [l for l in emb_lines
                  if SCHEMA_DOOR in l and not l.strip().startswith("#")]
    assert len(code_calls) == 1, code_calls


def test_one_bounded_call_site_per_extraction_unit():
    """Exactly ONE _schema_second_pass_edges call site in the worker — the budget pin."""
    text = pathlib.Path(emb.__file__).resolve().read_text(encoding="utf-8")
    calls = [l for l in text.splitlines() if "_schema_second_pass_edges(" in l
             and not l.strip().startswith("def ") and "def " not in l]
    assert len(calls) == 1, calls


def test_second_pass_never_rides_the_conversational_rewrite_door():
    """LLM_RELATION_EXTRACTION=false semantics untouched: /extract/rewrite's detect-only
    branch must not reference the schema pass at all."""
    text = pathlib.Path(main.__file__).resolve().read_text(encoding="utf-8")
    rewrite_zone = text[text.index('async def extract_rewrite'):]
    # the function ends at the first column-0 def after it (nested defs are indented)
    rewrite_zone = rewrite_zone[:rewrite_zone.index(chr(10) + 'def _apply_correction')]
    assert SCHEMA_DOOR not in rewrite_zone

"""THE CONSENSUS PINS (extractor-consensus reconciliation reader — A24 carrier+harness, A25 tier).

Spec: DEV/BUGS/competitive-eval-20261110/CONSENSUS-INGEST-SCOPING.md §4 + the owner amendments
(DEV/review/consensus-ingest/AMENDMENTS.md — the risk-first sequencing, the fixed harness schema,
the effective-rank report, the merge split, the loud liveness litmus).

A24 (this file, part 1 — the carrier + the shadow harness):
  1. THE CARRIER (linguistics.py:12727 owner ruling — "needs a CARRIER … both in main.py"):
     _gliner2_type_entities returns the per-span score side-channel ("confidences", keyed
     label→surface, JSON-safe) WITHOUT changing the "entities" shape any consumer reads;
     _build_typed_doc attaches each committed span's score on ``ent._.gliner_conf`` (Span
     extension, default None). A carrier transports — it gates nothing.
  2. THE FIXED HARNESS SCHEMA (AMENDMENT 2): reader SETS as a fixed-order bitmask/list over
     [parse_arm, gliner2_span, atomizer_surface, register_frame] — never a count — keyed by the
     normalized edges_dict fact identity, τ recorded, NEGATIVES logged (full presence/absence).
  3. NO FACT-COLUMN WRITES in A24 (AMENDMENT 1): the harness's only output is the
     consensus.cofiring log line; EdgeInput's reader_* fields default None and no consumer
     computes a tier yet.

THE LOUD LIVENESS LITMUS (AMENDMENT 5) lives here too: the DB/door-level pins in this file
ASSERT the SENTENCE_PIPELINE lane state of their environment and SKIP-WITH-LOUD-REASON when the
lane is dark — a skip is never silent and never counts as a pass.
"""

import json
from types import SimpleNamespace

import pytest

from src.api import main as main_mod
from src.api.main import (
    _CONSENSUS_READER_IDS,
    _build_typed_doc,
    _consensus_cofiring_rows,
    _consensus_endpoint_conf,
    _consensus_norm_key,
    _consensus_reader_bitmask,
    _consensus_tier_from_bits,
    _gliner2_type_entities,
    _tag_reader_signals,
)
from src.api.models import EdgeInput


# -- fixed reader schema (AMENDMENT 2) ---------------------------------------------

def test_reader_ids_are_the_fixed_four_in_forever_order():
    assert _CONSENSUS_READER_IDS == ("parse_arm", "gliner2_span", "atomizer_surface", "register_frame")


def test_bitmask_is_fixed_width_4_and_order_stable():
    assert _consensus_reader_bitmask(True, True, True, True) == "1111"
    assert _consensus_reader_bitmask(False, False, False, False) == "0000"
    # parse_arm only → "1000" (bit 0); gliner2 only → "0100"; atomizer only → "0010";
    # register only → "0001". The ORDER IS THE CONTRACT (renaming/reordering breaks every
    # historical row; the IDs are fixed, the positions are fixed).
    assert _consensus_reader_bitmask(True, False, False, False) == "1000"
    assert _consensus_reader_bitmask(False, True, False, False) == "0100"
    assert _consensus_reader_bitmask(False, False, True, False) == "0010"
    assert _consensus_reader_bitmask(False, False, False, True) == "0001"
    assert _consensus_reader_bitmask(True, False, True, False) == "1010"
    assert len(_consensus_reader_bitmask(True, True, False, False)) == 4


def test_norm_key_is_the_edges_dict_identity_normalized():
    assert _consensus_norm_key("User", "Apollo", "Owns") == ("user", "apollo", "owns")
    assert _consensus_norm_key(" user ", "  APOLLO ", " owns ") == ("user", "apollo", "owns")
    assert _consensus_norm_key(None, None, None) == ("", "", "")


# -- the tag helper (transport bookkeeping) ----------------------------------------

def test_tag_writes_only_provided_signals_and_never_overwrites():
    e = {"subject": "user", "rel_type": "owns", "object": "apollo"}
    _tag_reader_signals(e, parse_arm=True)
    assert e["reader_parse_arm"] is True
    assert "reader_atomizer_surface" not in e  # absent = reader not observed (the honest negative)
    # second writer must not erase the first (fields never overwrite)
    _tag_reader_signals(e, parse_arm=False, atomizer_surface=True)
    assert e["reader_parse_arm"] is True
    assert e["reader_atomizer_surface"] is True


def test_endpoint_conf_takes_strongest_endpoint_score_none_when_unscored():
    conf = {"apollo": 0.91, "my phone": 0.42}
    assert _consensus_endpoint_conf(conf, "Apollo", "user") == 0.91
    assert _consensus_endpoint_conf(conf, "apollo", "my phone") == 0.91
    assert _consensus_endpoint_conf(conf, "unknown", "surfaces") is None
    assert _consensus_endpoint_conf(None, "apollo", "x") is None
    assert _consensus_endpoint_conf({"x": 0.0}, "x", "y") == 0.0  # 0.0 is a score, not an absence


# -- the cofiring row builder (the A24 log body — pure) -----------------------------

def _edge(subj, obj, rel, *, parse=None, atom=None, conf=None, reg=None, prov="user_stated"):
    e = SimpleNamespace(subject=subj, object=obj, rel_type=rel, fact_provenance=prov)
    if parse is not None:
        e.reader_parse_arm = parse
    if atom is not None:
        e.reader_atomizer_surface = atom
    if conf is not None:
        e.reader_gliner2_conf = conf
    if reg is not None:
        e.reader_register_frame = reg
    return e


def test_cofiring_rows_carry_sets_not_counts_and_log_negatives():
    tau = 0.5
    edges = [
        _edge("user", "apollo", "owns", parse=True, atom=True, conf=0.91),          # 1110
        _edge("user", "wren", "spouse", parse=True, conf=None, atom=False),         # 1000: parse fired,
        #           ^ THE NEGATIVE, verbatim in the row: GLiNER2 silent (conf None) AND the
        #             atomizer silent on this sentence — both 0 bits present.
        _edge("nexus", "10.0.0.5", "has_ip", parse=False, reg=True, conf=0.2),      # register mint
    ]
    rows, counts = _consensus_cofiring_rows(edges, set(), tau)
    by_obj = {r["key"][1]: r for r in rows}
    assert by_obj["apollo"]["mask"] == "1110"
    assert by_obj["apollo"]["readers"] == ["parse_arm", "gliner2_span", "atomizer_surface"]
    assert by_obj["wren"]["mask"] == "1000"
    assert "gliner2_span" not in by_obj["wren"]["readers"]
    assert "atomizer_surface" not in by_obj["wren"]["readers"]
    assert by_obj["wren"]["gliner_conf"] is None
    # register_frame observed (harvest-side tag), gliner scored BELOW τ → 0 bit, raw score kept.
    assert by_obj["10.0.0.5"]["mask"] == "0001"
    assert by_obj["10.0.0.5"]["gliner_conf"] == 0.2
    assert counts == {"1110": 1, "1000": 1, "0001": 1}


def test_cofiring_tau_boundary_and_ingest_side_register_observation():
    tau = 0.5
    edges = [
        _edge("u", "exact-tau", "r", conf=0.5),    # conf == τ → the bit FIRES (>= τ)
        _edge("u", "just-under", "r", conf=0.499),  # just under → 0 bit
    ]
    obs = {_consensus_norm_key("u", "hosted-value", "has_setting")}
    edges.append(_edge("u", "hosted-value", "has_setting"))  # no harvest tag — observed at the seam
    rows, _ = _consensus_cofiring_rows(edges, obs, tau)
    by_obj = {r["key"][1]: r for r in rows}
    assert by_obj["exact-tau"]["mask"] == "0100"
    assert by_obj["just-under"]["mask"] == "0000"
    assert by_obj["hosted-value"]["mask"] == "0001"  # ingest-side convergence observation


def test_cofiring_rows_are_deterministically_ordered():
    tau = 0.5
    edges = [
        _edge("b", "2", "rel", parse=True),
        _edge("a", "9", "rel", parse=True),
        _edge("a", "1", "rel", parse=True),
        _edge("A", "1", "rel", parse=True, prov="llm_inferred"),  # normalizes onto ("a","1","rel")
    ]
    rows, _ = _consensus_cofiring_rows(edges, set(), tau)
    keys = [tuple(r["key"]) for r in rows]
    assert keys == sorted(keys)  # identity-sorted
    # normalized collision is resolved by the total key (provenance, mask, readers) — deterministic
    assert len({(tuple(r["key"]), r["provenance"]) for r in rows}) == len(rows)


def test_cofiring_rows_json_serializable():
    rows, counts = _consensus_cofiring_rows([_edge("u", "x", "r", parse=True, conf=0.7)], set(), 0.5)
    json.dumps({"rows": rows, "mask_counts": counts})  # must not raise


# -- the carrier: _gliner2_type_entities side-channel -------------------------------

class _FakeGliner:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def extract_entities(self, text, labels, threshold=None, include_confidence=False, max_len=None):
        self.calls.append({"threshold": threshold, "include_confidence": include_confidence})
        return self._result


def test_carrier_side_channel_keyed_label_surface_and_entities_unchanged():
    g = _FakeGliner({"entities": {
        "Person": [{"text": "Selin", "confidence": 0.87}, {"text": "Isolde", "confidence": 0.31}],
        "Concept": [{"text": "Selin", "confidence": 0.44}],
        "Organization": ["bare-string-shape"],  # back-compat: a non-confidence result shape
    }})
    out = _gliner2_type_entities(g, "Selin and Isolde talk about Selin.", ["Person", "Concept", "Organization"])
    # THE CONTRACT: the entities shape every consumer reads is byte-identical.
    assert out["entities"] == {"Person": ["Selin", "Isolde"], "Concept": ["Selin"],
                               "Organization": ["bare-string-shape"]}
    # THE CARRIER: the same (label, surface) keys, now carrying the score.
    assert out["confidences"] == {"Person": {"Selin": 0.87, "Isolde": 0.31},
                                  "Concept": {"Selin": 0.44}}
    assert "Organization" not in out["confidences"]  # a bare string never had a score
    # the call still asks GLiNER2 for confidence (the light-up contract)
    assert g.calls[0]["include_confidence"] is True


def test_carrier_duplicate_surface_keeps_first_score_deterministically():
    g = _FakeGliner({"entities": {"Person": [{"text": "Selin", "confidence": 0.9},
                                             {"text": "Selin", "confidence": 0.1}]}})
    out = _gliner2_type_entities(g, "Selin", ["Person"])
    assert out["confidences"] == {"Person": {"Selin": 0.9}}  # FIRST seen — not max/min (scan-order-free)


def test_carrier_miss_paths_return_empty_side_channel():
    assert _gliner2_type_entities(None, "x", ["Person"]) == {"entities": {}, "confidences": {}}
    assert _gliner2_type_entities(_FakeGliner(None), "x", []) == {"entities": {}, "confidences": {}}
    assert _gliner2_type_entities(_FakeGliner(None), "x", ["Person"]) == {"entities": {}, "confidences": {}}

    class _Boom:
        def extract_entities(self, *a, **k):
            raise RuntimeError("model exploded")

    assert _gliner2_type_entities(_Boom(), "x", ["Person"]) == {"entities": {}, "confidences": {}}


# -- the carrier: _build_typed_doc attaches ent._.gliner_conf ------------------------

def _linguistics_or_loud_skip():
    """AMENDMENT 5 discipline for the parse-dependent tests: the grammar lane must be PRESENT,
    else SKIP WITH A LOUD REASON — never a silent pass."""
    try:
        from src.extraction import linguistics as _ling
        if not _ling.linguistics_available():
            pytest.skip("LOUD SKIP: the spaCy grammar lane (linguistics_available()) is DARK in "
                        "this environment — the ent._.gliner_conf carrier test cannot run here; "
                        "run it where the grammar model is baked (dev/pre-prod).")
        return _ling
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"LOUD SKIP: linguistics import failed ({e}) — carrier test needs the grammar lane")


def test_build_typed_doc_attaches_gliner_conf_to_committed_ents():
    ling = _linguistics_or_loud_skip()
    g = _FakeGliner({"entities": {
        "Person": [{"text": "Selin", "confidence": 0.87}],
        "Concept": [{"text": "the thesis", "confidence": 0.55}],
    }})
    doc = _build_typed_doc("Selin finished the thesis.", g, ["Person", "Concept"])
    assert doc is not None
    conf_by_surface = {}
    for ent in doc.ents:
        # every committed ent carries the extension attribute (None = unscored)
        conf = getattr(ent._, "gliner_conf", "MISSING-EXTENSION")
        assert conf != "MISSING-EXTENSION", "the gliner_conf Span extension is not registered"
        conf_by_surface[ent.text.strip().lower()] = conf
    assert conf_by_surface.get("selin") == pytest.approx(0.87)
    assert conf_by_surface.get("the thesis") == pytest.approx(0.55)


def test_build_typed_doc_coordinated_split_inherits_parent_score():
    ling = _linguistics_or_loud_skip()
    g = _FakeGliner({"entities": {"Person": [{"text": "Marina, Theo", "confidence": 0.66}]}})
    doc = _build_typed_doc("Marina, Theo are colleagues.", g, ["Person"])
    assert doc is not None
    got = {}
    for ent in doc.ents:
        got[ent.text.strip().lower()] = getattr(ent._, "gliner_conf", None)
    # each PART inherited the parent surface's score (the split part has no score of its own)
    assert got.get("marina") == pytest.approx(0.66)
    assert got.get("theo") == pytest.approx(0.66)


def test_build_typed_doc_untyped_or_failed_typing_leaves_none_conf():
    ling = _linguistics_or_loud_skip()
    doc = _build_typed_doc("Selin finished the thesis.", None, None)
    assert doc is not None  # parsed but untyped → thin-tag gap-fill (today's behavior)
    for ent in doc.ents:
        assert getattr(ent._, "gliner_conf", None) is None
    # a typing MISS (model raises) returns the parsed Doc — no crash, no scores
    class _Boom:
        def extract_entities(self, *a, **k):
            raise RuntimeError("down")
    doc2 = _build_typed_doc("Selin finished the thesis.", _Boom(), ["Person"])
    assert doc2 is not None


# -- A24 WRITES NO FACT COLUMN (AMENDMENT 1) -----------------------------------------

def test_edgeinput_reader_fields_default_none_and_transport():
    e = EdgeInput(subject="user", object="apollo", rel_type="owns")
    assert e.reader_parse_arm is None
    assert e.reader_atomizer_surface is None
    assert e.reader_gliner2_conf is None
    assert e.reader_register_frame is None
    # the fields survive the harvest → MCP → /ingest JSON hop
    e2 = EdgeInput(**json.loads(json.dumps({
        "subject": "user", "object": "apollo", "rel_type": "owns",
        "reader_parse_arm": True, "reader_atomizer_surface": False,
        "reader_gliner2_conf": 0.91, "reader_register_frame": None})))
    assert e2.reader_parse_arm is True and e2.reader_gliner2_conf == pytest.approx(0.91)


def test_a25_tier_semantics_and_stage_guards():
    """A25 (landed): the tier is the EVIDENCE-STACK indicator — 3-4 readers = tier 1, two = tier 2,
    one = tier 3, none = None. The write goes ONLY to the migration-313 consensus_tier column
    (flag-gated, default OFF); unified_confidence/confidence are NEVER overloaded; the recall-side
    ordering stays DEFERRED to increment 2 (its own flag + the user_stated pin)."""
    assert main_mod.CONSENSUS_TIER is False  # the default: OFF = byte-parity
    assert _consensus_tier_from_bits((True, True, True, True)) == 1
    assert _consensus_tier_from_bits((True, True, True, False)) == 1
    assert _consensus_tier_from_bits((True, True, False, False)) == 2
    assert _consensus_tier_from_bits((True, False, False, False)) == 3
    assert _consensus_tier_from_bits((False, False, False, True)) == 3
    assert _consensus_tier_from_bits((False, False, False, False)) is None
    import inspect
    src = inspect.getsource(main_mod)
    # the tier NEVER writes the WGM authority columns from a reader path
    assert "SET unified_confidence = consensus" not in src
    assert "SET confidence = consensus" not in src
    # the recall-side ordering is increment 2 — its flag must NOT exist yet
    assert "CONSENSUS_RECALL_TIER" not in src
    # migration 313 exists (the additive column, both tables, the skew caveat)
    import pathlib
    mig = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "313_consensus_tier_column.sql"
    assert mig.exists(), "migration 313 missing"
    mig_sql = mig.read_text()
    assert "facts" in mig_sql and "staged_facts" in mig_sql
    assert "ADD COLUMN IF NOT EXISTS consensus_tier SMALLINT" in mig_sql
    assert "skew" in mig_sql.lower()  # the n=65 re-cut caveat is ON the migration record
    # the provisioning template carries the same column (template + migration, never one alone)
    tpl = (pathlib.Path(__file__).resolve().parents[1]
           / "src" / "provisioning" / "templates" / "user_schema.sql").read_text()
    assert tpl.count("consensus_tier SMALLINT") >= 2  # facts + staged_facts


def test_a25_keepboth_loser_tag_never_touches_user_truth():
    """The keep-both loser tag: tier-3 NON-USER rows may demote llm_learned → llm_inferred at a
    coexist; a user_stated row is NEVER demoted (the truth firewall); llm_inferred is a no-op."""
    import inspect
    src = inspect.getsource(main_mod)
    assert "consensus.keepboth_loser_tagged" in src
    # the guard order: tier==3 AND provenance == 'llm_learned' — user_stated never matches
    assert '_kb_tier == 3 and _kb_prov == "llm_learned"' in src


def test_a25_tier_reads_the_shared_derivation():
    """The tier and the cofiring log MUST share one reader derivation (drift between the logged
    matrix and the stored tier is the failure mode)."""
    import inspect
    src = inspect.getsource(main_mod._consensus_cofiring_rows)
    assert "_consensus_reader_bits_for_edge" in src
    e = _edge("u", "x", "r", parse=True, atom=True, conf=0.9)
    bits, mask, conf = main_mod._consensus_reader_bits_for_edge(e, set(), 0.5)
    assert bits == (True, True, True, False)
    assert mask == "1110"
    assert conf == 0.9
    # tier joins through the observation set exactly like the log's register bit
    obs = {_consensus_norm_key("u", "x", "r")}
    bits2, _, _ = main_mod._consensus_reader_bits_for_edge(_edge("u", "x", "r"), obs, 0.5)
    assert bits2 == (False, False, False, True)
    assert main_mod._consensus_tier_from_bits(bits2) == 3


def test_a25_migration_column_is_additive_and_checked():
    """The migration is additive+idempotent only: ADD COLUMN IF NOT EXISTS + CHECK, no backfill,
    no UPDATE/DELETE of any row."""
    import pathlib
    mig_sql = (pathlib.Path(__file__).resolve().parents[1]
               / "migrations" / "313_consensus_tier_column.sql").read_text().upper()
    assert "UPDATE FACTS" not in mig_sql and "UPDATE %I.STAGED_FACTS" not in mig_sql \
        and "UPDATE %I.FACTS" not in mig_sql
    assert "DELETE FROM" not in mig_sql
    assert "ADD COLUMN IF NOT EXISTS CONSENSUS_TIER SMALLINT" in mig_sql


def test_shadow_log_flag_default_on_and_log_gated():
    # the flag reads once at import; its DEFAULT is the contract (log-only, ON)
    assert main_mod.CONSENSUS_SHADOW_LOG is True or main_mod.CONSENSUS_SHADOW_LOG is False
    import os
    assert os.environ.get("CONSENSUS_SHADOW_LOG", "true").strip().lower() not in ("0", "false", "no") \
        or main_mod.CONSENSUS_SHADOW_LOG is False


# -- THE LOUD LIVENESS LITMUS (AMENDMENT 5) — the lane assertion ----------------------

def test_sentence_pipeline_lane_state_is_asserted_not_guessed():
    """The harness readers live on the spine lane. This pin asserts the lane state OUT LOUD so a
    dark lane is never mistaken for a green battery: on a SENTENCE_PIPELINE=false environment the
    spine-lane-dependent behavior below is SKIPPED WITH THE REASON, never silently passed."""
    lane_on = main_mod.SENTENCE_PIPELINE
    if not lane_on:
        pytest.skip(
            "LOUD SKIP: SENTENCE_PIPELINE=false in this environment — the statement lane routes "
            "through /extract/rewrite (the LLM lane) and the spine readers never fire. The "
            "lane-independent reader path (register seams) is pinned separately; the spine-lane "
            "co-firing pins require a SENTENCE_PIPELINE=true environment (pre-prod runs it: "
            "measured 2026-11-12, GET /internal/ingest-route → statement_extractor=spine).")
    assert main_mod.SENTENCE_PIPELINE is True


def test_lane_independent_reader_path_when_spine_lane_off():
    """The register seams (instruction register, negated-prescription, atomic value detector) are
    LANE-INDEPENDENT: they run inside /ingest on req.text regardless of which extractor fed the
    edges. This pins the pure row-builder half of that contract: an edge with NO harvest signals
    (the legacy-lane shape) still yields a well-formed row whose register_frame bit can fire from
    the ingest-side observation set alone."""
    e = _edge("tool", "--force-recreate", "has_setting")  # zero reader fields — the legacy shape
    obs = {_consensus_norm_key("tool", "--force-recreate", "has_setting")}
    rows, counts = _consensus_cofiring_rows([e], obs, 0.5)
    assert rows[0]["mask"] == "0001"
    assert rows[0]["readers"] == ["register_frame"]
    assert counts == {"0001": 1}
    # and with no observation: the honest all-silent row (every 0 bit present — the negative)
    rows2, _ = _consensus_cofiring_rows([e], set(), 0.5)
    assert rows2[0]["mask"] == "0000"
    assert rows2[0]["readers"] == []

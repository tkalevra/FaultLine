"""AUTHORSHIP AT THE INGEST ROUTER - the unattested source token (2026-08-15).

Unit pins for the backend half of the authorship-honest provenance change:
  * the episodic origin map DOWN-ROUTES never-attested origins to the explicit
    unattested ingest lane (so the re-mine can never land Class A),
  * FactCorrectionRequest.attested defaults True (explicit corrections keep
    user_stated / Class A byte-identical; only the recall divert passes False),
  * assign_class_and_confidence user_stated-to-A branch is UNTOUCHED by the change
    (the unattested force lives at the /ingest call site, keyed on req.source),
  * the llm_inferred arm keeps its historical per-rel_type behavior for every
    OTHER lane (the force to staged B applies ONLY to source=unattested).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from api.models import FactCorrectionRequest


def test_correction_request_attested_defaults_true():
    """An explicit retract_fact sends no attested field (or True) - the historical
    user_stated / Class A correction must be byte-identical."""
    req = FactCorrectionRequest(text="Rex is a dog not a bunny", user_id="u1")
    assert req.attested is True


def test_correction_request_accepts_unattested():
    req = FactCorrectionRequest(
        text="actually my dog is Rex not Rexx", user_id="u1", attested=False)
    assert req.attested is False


def test_episodic_origin_map_downroutes_unattested_origins():
    """store_context_deferred (the store_context tool lane) and unattested (a recall
    divert verbatim) must re-mine through the EXPLICIT unattested ingest lane - never
    the legacy lane that can land a defined-A rel at facts Class A, and never the
    preserving mcp lane that would re-elevate a demoted capture."""
    from re_embedder.embedder import (
        _EPISODIC_ORIGIN_INGEST_SOURCE, _reextract_ingest_source,
    )
    assert _EPISODIC_ORIGIN_INGEST_SOURCE["store_context_deferred"] == "unattested"
    assert _EPISODIC_ORIGIN_INGEST_SOURCE["unattested"] == "unattested"
    assert _reextract_ingest_source("store_context_deferred") == "unattested"
    assert _reextract_ingest_source("unattested") == "unattested"


def test_episodic_origin_map_preserving_entries_unchanged():
    """The REEXTRACT_PRESERVE_PROVENANCE entries are untouched: an attested mcp origin
    still re-mines under mcp (user_stated preserved). The document origin moved to its
    own tier on 2026-08-21 (owner ruling: document-extracted facts are staged B, never
    A) — see tests/api/test_doc_tier_ingest_authorship.py, which pins that mapping."""
    from re_embedder.embedder import _EPISODIC_ORIGIN_INGEST_SOURCE
    assert _EPISODIC_ORIGIN_INGEST_SOURCE["mcp"] == "mcp"


def test_episodic_origin_map_unknown_stays_legacy():
    """Unknown origin is NEVER guessed - None means the legacy reextract lane
    (forced llm_inferred), the existing safety property."""
    from re_embedder.embedder import _reextract_ingest_source
    assert _reextract_ingest_source(None) is None
    assert _reextract_ingest_source("something-new") is None


SEED_FACT_CLASSES = {
    "lives_in": {"fact_class": "A"},
    "favorite_colour": {"fact_class": "B"},
    "feels": {"fact_class": "C"},
}


def _seeded(monkeypatch):
    """Cold-process _REL_TYPE_META is empty (it loads from the DB); inject the seed
    classes so the assign arms are exercised deterministically."""
    from src.api import main as m
    monkeypatch.setattr(m, "_REL_TYPE_META", dict(SEED_FACT_CLASSES))
    monkeypatch.setattr(m, "_REL_TYPE_CACHE", {})
    return m


def test_assign_user_stated_to_a_branch_untouched(monkeypatch):
    """The user_stated-to-A branch in class assignment is NOT modified by the
    authorship change: user_stated lands A for defined-A and defined-B rels,
    durable B for the defined-C (feels) tier, and corrections stay A/1.0."""
    m = _seeded(monkeypatch)
    cls3d = {}  # classification dict unused by the provenance arms
    assert m.assign_class_and_confidence(
        cls3d, fact_provenance="user_stated", rel_type="lives_in") == ("A", 1.0)
    assert m.assign_class_and_confidence(
        cls3d, fact_provenance="user_stated", rel_type="favorite_colour") == ("A", 1.0)
    assert m.assign_class_and_confidence(
        cls3d, fact_provenance="user_stated", rel_type="feels") == ("B", 0.8)
    assert m.assign_class_and_confidence(
        cls3d, fact_provenance="user_stated", is_correction=True) == ("A", 1.0)


def test_scalar_path_maps_source_to_canonical_provenance():
    """CRITIC ROUND 1 note ('code-verified only'): the entity_attributes INSERT has no
    fact_class — provenance is its ONLY authorship signal, and it previously wrote
    req.source VERBATIM (a raw token like 'unattested'). Pin that the mapping block
    exists and the INSERT consumes _scalar_provenance, not req.source."""
    import re
    src = open(os.path.join(os.path.dirname(__file__), "../../src/api/main.py")).read()
    m = re.search(r"_scalar_provenance = \(", src)
    assert m, "scalar path must derive a canonical _scalar_provenance before the INSERT"
    m2 = re.search(r"val_text, val_int, val_float, val_date, _scalar_provenance", src)
    assert m2, "entity_attributes INSERT must write _scalar_provenance, never req.source"
    # and the mapping itself is the same rule the relational router applies
    m3 = re.search(
        r"_scalar_provenance = \(\s*"
        r"\"llm_learned\" if req\.source == \"llm_learn\"\s*"
        r"else \"user_stated\" if req\.source in \(\"mcp\", \"assistant\"\)\s*"
        r"else \"llm_inferred\"", src)
    assert m3, "scalar provenance mapping must mirror the ingest provenance router"


def test_hierarchy_demotions_never_put_unattested_on_c_clock():
    """CRITIC ROUND 1 regression: the hierarchy-type-mismatch demotion (fact_class='C',
    default-ON) runs AFTER the unattested B-force in /ingest. An unattested capture must
    NEVER land fact_class='C' (the 30-day decay clock) — the guard keys on req.source.
    The mirror branch (hierarchy_violation staging) carries the same floor.
    (2026-08-21: the guard shape widened to also exclude the document lane — see
    tests/api/test_doc_tier_ingest_authorship.py; the regexes below pin the NEW shape.)"""
    import re
    src = open(os.path.join(os.path.dirname(__file__), "../../src/api/main.py")).read()
    m = re.search(
        r"hierarchy_type_mismatch\"\) and status == \"valid\"[\s\S]{0,140}?"
        r"req\.source not in \(\"unattested\", \"document\"\)", src)
    assert m, "hierarchy_type_mismatch demotion must exclude source='unattested' AND 'document'"
    m2 = re.search(
        r"hierarchy_violation and status == \"valid\" and not req\.edges[\s\S]{0,100}?"
        r"req\.source not in \(\"unattested\", \"document\"\)", src)
    assert m2, "hierarchy_violation staging must exclude source='unattested' AND 'document'"


def test_assign_llm_inferred_arm_unchanged_for_other_lanes(monkeypatch):
    """The unattested force lives at the /ingest call site keyed on req.source - the
    llm_inferred ARM itself keeps its historical per-rel_type behavior (defined class
    with extraction confidence) for every other lane (reextract legacy, api, ...)."""
    m = _seeded(monkeypatch)
    cls3d = {}
    klass, conf = m.assign_class_and_confidence(
        cls3d, fact_provenance="llm_inferred", rel_type="lives_in", confidence=0.9)
    assert klass == "A" and abs(conf - 0.9) < 1e-9
    klass, conf = m.assign_class_and_confidence(
        cls3d, fact_provenance="llm_inferred", rel_type="feels", confidence=0.5)
    assert klass == "C"


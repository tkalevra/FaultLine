"""CITE-DENSE SKIP (litmus fix L4, ticket DEV/BUGS/prod-document-drain-20261103/06).

A reference section / affiliation front-matter chunk has NO assertions in it; the
extraction ladder over it burned the full spine+rewrite+GLiNER2 ladder to mint
citation junk or a timed-out nothing (measured on prod: 179/254 chunks FAILED, 70%
of one document's wall time; zero-yield 37% on arXiv docs). The pure gate in
src/ingest/document_structure.py was measured on the REAL corpus BEFORE shipping
(175/534 arXiv chunks flagged = 33%, inside the ticket's 12-37% band; 0/137 control
chunks - the CLAUDE.md 60%-false-veto precedent demands a measured gate). The wire
seam in _process_document_chunk runs AFTER verbatim retention and BEFORE any
extractor call, and returns an HONEST zero-yield class (cite_dense_reference_block)
carried into documents.chunk_state by BOTH flush lanes - neither a failure nor a
silent read-and-empty.

House style: DB-less, monkeypatched. The corpus CSV is optional: the parity test
runs against it when present (it lives in benchmarks/) and pins the measured
numbers; the seam tests fabricate text.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.ingest.document_structure import citation_density_verdict  # noqa: E402
import src.re_embedder.embedder as emb  # noqa: E402


# R10 hermeticity — same class the gauntlet paid for twice in the sibling file:
# citation_density_verdict reads DOC_CITE_MIN_MARKS / DOC_CITE_DENSITY_RATIO live
# (document_structure.py) and the chunk path reads DOC_CITE_AFFIL_FRONT_MATTER_MAX
# (embedder seam) and DOC_CHUNK_SPINE_FIRST (embedder chunk path) — all call-time,
# so an ambient runner env spuriously reddens pins here (all four critic-measured).
@pytest.fixture(autouse=True)
def _pin_cite_env_defaults(monkeypatch):
    monkeypatch.delenv("DOC_CITE_MIN_MARKS", raising=False)
    monkeypatch.delenv("DOC_CITE_DENSITY_RATIO", raising=False)
    monkeypatch.delenv("DOC_CITE_AFFIL_FRONT_MATTER_MAX", raising=False)
    monkeypatch.delenv("DOC_CHUNK_SPINE_FIRST", raising=False)


# ---------------------------------------------------------------------------
# The pure gate (same regexes the corpus measurement validated)
# ---------------------------------------------------------------------------
_REFERENCE_BLOCK = (
    "[1] Smith J, Jones K. Deep learning for graphs. In Proceedings of the "
    "International Conference on Learning Representations, 2021, pp. 1-12.\n"
    "[2] Lee M. Attention mechanisms revisited. Journal of Machine Learning "
    "Research, 2020, vol. 21, pp. 33-49.\n"
    "[3] Brown A. arXiv:2006.11244. Association for Computational Linguistics, "
    "2019.\n"
    "[4] Davis R, et al. In Proceedings of the Annual Meeting of the Association "
    "for Computational Linguistics, 2018, pp. 100-110.\n"
    "[5] Wilson T. IEEE Transactions on Pattern Analysis, 2017, pp. 5-15.\n"
    "[6] Clark K. Advances in Neural Information Processing Systems, 2016.\n"
    "[7] White P. ACM Symposium on Theory of Computing, 2015, pp. 9-19.\n"
    "[8] Green H. Journal of Artificial Intelligence Research, 2014, pp. 2-8."
)

_AFFIL_BLOCK = (
    "Jane Doe is with the Department of Computer Science, University of "
    "Somewhere, Somewhere, USA (jdoe@somewhere.edu).\n"
    "John Roe is with the Key Laboratory of Intelligent Processing, Institute "
    "of Computing, Academy of Sciences (john@lab.ac.cn).\n"
    "Mary Poe is with the School of Artificial Intelligence, University of "
    "Elsewhere (mary@elsewhere.edu). Corresponding author: Mary Poe.\n"
    "Richard Blue is with the Department of Mathematics, Institute of "
    "Technology (rb@tech.edu)."
)

_BODY_PROSE = (
    "The router forwards packets between networks. My sister Diane lives in "
    "Toronto with her cat Fraggle. The meeting is scheduled for 3 pm tomorrow. "
    "Photosynthesis converts carbon dioxide and water into glucose and oxygen. "
    "I bought four movies at the festival last weekend."
)


def test_reference_block_is_flagged():
    v = citation_density_verdict(_REFERENCE_BLOCK)
    assert v["cite_dense"] is True
    assert v["reason"] == "ref_run"
    assert v["ref_line_ratio"] >= 0.5
    assert v["marks"] >= 6


def test_affiliation_block_is_flagged():
    v = citation_density_verdict(_AFFIL_BLOCK)
    assert v["cite_dense"] is True
    assert v["reason"] == "affil_block"


def test_body_prose_is_never_flagged():
    v = citation_density_verdict(_BODY_PROSE)
    assert v["cite_dense"] is False
    assert v["reason"] == "body_prose"


def test_gate_is_deterministic():
    assert citation_density_verdict(_REFERENCE_BLOCK) == citation_density_verdict(_REFERENCE_BLOCK)


def test_empty_text_is_never_flagged():
    assert citation_density_verdict("")["cite_dense"] is False
    assert citation_density_verdict(None)["cite_dense"] is False


def test_env_knobs_retune_the_gate(monkeypatch):
    monkeypatch.setenv("DOC_CITE_DENSITY_RATIO", "0.99")
    monkeypatch.setenv("DOC_CITE_MIN_MARKS", "999")
    v = citation_density_verdict(_REFERENCE_BLOCK)
    assert v["cite_dense"] is False, "ratio 0.99 + marks 999 must refuse the ref run"
    assert v["reason"] == "body_prose"


# ---------------------------------------------------------------------------
# Corpus parity (runs only when the measured CSV is present)
# ---------------------------------------------------------------------------
_CSV = Path(__file__).resolve().parents[1] / "benchmarks" /     "doc-ingest-litmus-corpus-chunks-prod-20261103.csv"


@pytest.mark.skipif(not _CSV.exists(), reason="corpus CSV not present")
def test_gate_matches_measured_corpus_decision():
    import collections
    import csv as _csv
    rows = list(_csv.reader(open(_CSV)))
    by_doc = collections.defaultdict(list)
    for r in rows:
        by_doc[int(r[0])].append(r[2])
    arx = [10, 12, 13, 14]
    ctrl = [d for d in by_doc if d not in arx]
    flagged_arx = sum(citation_density_verdict(t)["cite_dense"]
                      for d in arx for t in by_doc[d])
    total_arx = sum(len(by_doc[d]) for d in arx)
    flagged_ctrl = sum(citation_density_verdict(t)["cite_dense"]
                       for d in ctrl for t in by_doc[d])
    assert flagged_arx == 175, f"measured decision was 175 arXiv chunks, got {flagged_arx}"
    assert flagged_ctrl == 0, f"measured decision was ZERO control hits, got {flagged_ctrl}"
    # the ticket's zero-yield band: the flagged share must stay inside 12-37%
    share = flagged_arx / total_arx
    assert 0.12 <= share <= 0.37, f"flagged share {share:.2%} left the measured band"


# ---------------------------------------------------------------------------
# The seam: skip AFTER retention, BEFORE the ladder, classified honestly
# ---------------------------------------------------------------------------
class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"committed": 0, "staged": 0}


def _patch_no_network(monkeypatch, calls):
    monkeypatch.setattr(emb.httpx, "post",
                        lambda *a, **k: calls.append(a[0]) or _Resp())


def test_cite_dense_chunk_skips_the_ladder_and_classifies(monkeypatch):
    monkeypatch.setattr(emb, "DOC_CITE_DENSE_SKIP", True)
    calls = []
    _patch_no_network(monkeypatch, calls)
    ladder_urls = []
    real_post = emb.httpx.post

    def recording_post(url, *a, **k):
        ladder_urls.append(str(url))
        return _Resp()

    monkeypatch.setattr(emb.httpx, "post", recording_post)
    got = emb._process_document_chunk(
        _REFERENCE_BLOCK, idx=7, doc_id=10, user_id="u" * 8,
        backend_url="http://backend", source_ref="doc")
    # 5-tuple: zero committed/staged/failed, NO failure reason, the honest class
    assert got[:4] == (0, 0, 0, None)
    assert got[4] == "cite_dense_reference_block"
    # only the episodic retention fired - NO extractor call (no /harvest-spans, no /extract)
    assert not any("/harvest-spans" in u or "/extract" in u for u in ladder_urls), ladder_urls
    assert any("/episodic/append" in u for u in ladder_urls), (
        "verbatim retention must happen BEFORE the skip")


def test_cite_dense_skip_off_restores_legacy_ladder(monkeypatch):
    monkeypatch.setattr(emb, "DOC_CITE_DENSE_SKIP", False)
    ladder_urls = []

    def recording_post(url, *a, **k):
        ladder_urls.append(str(url))

        class _R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "success", "edges": []}

        return _R()

    monkeypatch.setattr(emb.httpx, "post", recording_post)
    got = emb._process_document_chunk(
        _REFERENCE_BLOCK, idx=7, doc_id=10, user_id="u" * 8,
        backend_url="http://backend", source_ref="doc")
    assert got[4] is None  # no classification: the chunk went through the ladder
    assert any("/harvest-spans" in u for u in ladder_urls), (
        "rollback lever OFF: the spine must be called as before")


def test_body_prose_chunk_still_runs_the_ladder(monkeypatch):
    monkeypatch.setattr(emb, "DOC_CITE_DENSE_SKIP", True)
    ladder_urls = []

    def recording_post(url, *a, **k):
        ladder_urls.append(str(url))

        class _R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "success", "edges": []}

        return _R()

    monkeypatch.setattr(emb.httpx, "post", recording_post)
    got = emb._process_document_chunk(
        _BODY_PROSE, idx=1, doc_id=2, user_id="u" * 8,
        backend_url="http://backend", source_ref="doc")
    assert got[4] is None
    assert any("/harvest-spans" in u for u in ladder_urls), (
        "ordinary prose must NEVER take the skip lane")


def test_gate_error_never_fails_a_chunk(monkeypatch):
    monkeypatch.setattr(emb, "DOC_CITE_DENSE_SKIP", True)

    def boom(text):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(emb, "_citation_density_verdict", boom)
    ladder_urls = []

    def recording_post(url, *a, **k):
        ladder_urls.append(str(url))

        class _R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "success", "edges": []}

        return _R()

    monkeypatch.setattr(emb.httpx, "post", recording_post)
    got = emb._process_document_chunk(
        _REFERENCE_BLOCK, idx=7, doc_id=10, user_id="u" * 8,
        backend_url="http://backend", source_ref="doc")
    assert got[4] is None
    assert any("/harvest-spans" in u for u in ladder_urls), (
        "a broken gate must fall through to the ladder, never skip it silently")

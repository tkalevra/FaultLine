"""BenchIE scorer contract tests.

THE LOAD-BEARING TEST is `test_reproduces_published_system_scores`: our scorer is an INDEPENDENT
reimplementation of BenchIE's metric (the upstream repo's licence claims ownership of derivatives,
so none of its code is used here). The only proof that it IS BenchIE's metric — and therefore that
FaultLine's number is comparable to the published ClausIE F1 0.34 — is that it reproduces the
paper's own Table 5 figures from the paper's own system-extraction files.

A green suite is not evidence unless it executes the path. These tests execute the real gold file
and the real published extraction files; they SKIP LOUDLY (never silently pass) when the
licence-gated BenchIE cache is absent.
"""
import os

import pytest

from benchmarks.harness.benchie import fetch, gold as G, scorer as S

DATA = fetch.data_dir()
HAVE = fetch.have_data(DATA)
skip_no_data = pytest.mark.skipif(
    not HAVE,
    reason=("BenchIE cache absent — it is NOT vendored (NEC noncommercial licence). "
            f"Set {fetch.ACK_ENV}=1 and run benchmarks/harness/benchie/run_benchie.py to fetch."))


@pytest.fixture(scope="module")
def gold():
    g, malformed = G.parse_gold(os.path.join(DATA, "benchie_gold_annotations_en.txt"))
    assert not malformed, f"{len(malformed)} malformed gold lines — the parser dropped real gold"
    return g


@skip_no_data
def test_gold_shape(gold):
    """300 English sentences, ~1.35k fact synsets — the corpus the published numbers are on."""
    assert len(gold) == 300
    n_syn = sum(len(v.synsets) for v in gold.values())
    assert 1300 <= n_syn <= 1400, n_syn
    assert all(s.facts for v in gold.values() for s in v.synsets), "a synset with no surface forms"


@skip_no_data
@pytest.mark.parametrize("system,fname", [
    ("clausie", "clausie_explicit.txt"),
    ("minie", "minie_explicit.txt"),
    ("openie6", "openie6_explicit.txt"),
])
def test_reproduces_published_system_scores(gold, system, fname):
    """Our metric must reproduce BenchIE ACL-2022 Table 5 (English) to 2 decimal places."""
    ext, bad = S.load_system_extractions(os.path.join(DATA, fname))
    assert not bad, f"{len(bad)} malformed extraction lines in {fname}"
    sc = S.score(gold, ext, keep_detail=False)
    pub = S.PUBLISHED[system]
    assert round(sc.precision, 2) == pub["p"], f"P {sc.precision:.4f} vs published {pub['p']}"
    assert round(sc.recall, 2) == pub["r"], f"R {sc.recall:.4f} vs published {pub['r']}"
    assert round(sc.f1, 2) == pub["f1"], f"F1 {sc.f1:.4f} vs published {pub['f1']}"


def test_optional_tokens_are_optional():
    """`[ ]` groups are all-or-nothing and may be omitted — the core gold semantics."""
    p = G.SlotPattern.parse("[the] [first] Prime Minister [of Australia]")
    assert p.matches(G.normalize_tokens("Prime Minister"))
    assert p.matches(G.normalize_tokens("the first Prime Minister of Australia"))
    assert p.matches(G.normalize_tokens("the Prime Minister"))
    assert not p.matches(G.normalize_tokens("Prime Minister of"))      # partial optional group
    assert not p.matches(G.normalize_tokens("first the Prime Minister"))  # order matters
    assert not p.matches(G.normalize_tokens("Prime Minister of Canada"))


def test_matching_is_exact_not_lenient():
    """No token-overlap credit. Leniency is what makes CaRB overstate by ~26 F1."""
    p = G.SlotPattern.parse("became [a] founding justice")
    assert p.matches(G.normalize_tokens("became founding justice"))
    assert not p.matches(G.normalize_tokens("became justice"))          # required token missing
    assert not p.matches(G.normalize_tokens("became a founding justice of"))  # extra token


def test_sentence_with_no_extractions_is_still_scored():
    """A NO-RESULT sentence contributes full FN. It is never dropped from the denominator —
    the LongMemEval harness did exactly that and flattered itself."""
    g = {1: G.GoldSentence(sent_id=1, text="x", synsets=[
        G.FactSynset(sent_id=1, cluster_id=1, facts=[G.GoldFact(
            subject=G.SlotPattern.parse("a"), relation=G.SlotPattern.parse("b"),
            object=G.SlotPattern.parse("c"), raw="a --> b --> c")])])}
    sc = S.score(g, {})
    assert sc.n_sentences_scored == 1 and sc.tp == 0 and sc.fn == 1
    assert sc.recall == 0.0


def test_duplicate_extractions_of_one_fact_count_once():
    """Two extractions of the SAME fact are one TP, not two — BenchIE counts facts, not tokens."""
    g = {1: G.GoldSentence(sent_id=1, text="x", synsets=[
        G.FactSynset(sent_id=1, cluster_id=1, facts=[
            G.GoldFact(subject=G.SlotPattern.parse("he"),
                       relation=G.SlotPattern.parse("became"),
                       object=G.SlotPattern.parse("[a] justice"), raw=""),
        ])])}
    ex = {1: [S.Extraction(1, "he", "became", "a justice"),
              S.Extraction(1, "he", "became", "justice")]}
    sc = S.score(g, ex)
    assert (sc.tp, sc.fp, sc.fn) == (1, 0, 0)


def test_unknown_sent_id_is_excluded_and_visible():
    """An extraction for a sentence not in the gold is EXCLUDED and RECORDED, never silent."""
    g = {1: G.GoldSentence(sent_id=1, text="x", synsets=[])}
    sc = S.score(g, {99: [S.Extraction(99, "a", "b", "c")]})
    assert len(sc.excluded) == 1
    assert sc.excluded[0]["reason"] == "extraction_sent_id_not_in_gold"
    assert sc.excluded[0]["sent_id"] == 99


def test_runner_clears_its_own_stale_output(tmp_path):
    """Aggregating a previous run's artifacts as the current run's is the most repeated
    measurement bug in this repo. The runner must delete its own output at startup."""
    from benchmarks.harness.benchie.run_benchie import ARTIFACTS, clear_stale_output
    for name in ARTIFACTS:
        (tmp_path / name).write_text("STALE")
    removed = clear_stale_output(str(tmp_path), quiet=True)
    assert sorted(removed) == sorted(ARTIFACTS)
    assert not any((tmp_path / n).exists() for n in ARTIFACTS)

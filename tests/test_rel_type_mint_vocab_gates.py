"""MEASURED regression tests for the rel_type mint vocabulary gates (bug 05 + bug 03).

Every fixture below is a REAL prod measurement from the tenant that produced the junk
(faultline_10d7d879, dump-window 2026-11-01..03; corpus pull /tmp/veto_corpus_all.txt):
  * 225 rel_types minted in 24h, ~25% surname-shaped single tokens (citation lift);
  * the negation family (not_at, cannot_handle, never_watches, ...) minted from negated
    clauses that folded their polarity into the predicate string (20/20 vetoed by the
    spine's own UD parse here; 0/42 healthy types false-vetoed);
  * reference-list surnames (askell, chan, ...) minted from PDF reference sections -
    vetoed ONLY when every occurrence sits on a citation-genre line (the proportional_to
    control - prose mentioning 'proportional to' with an et-al citation ELSEWHERE in the
    same snippet - must survive; that control killed the whole-snippet veto design).
Thresholds are calibrated so 0/60 healthy controls false-veto on the stored corpus.

PURE module tests + scripted-fake gate seam tests. No DB, no network.
Run: python3 -m pytest tests/test_rel_type_mint_vocab_gates.py -q
     (tests/ is gitignored -> git add -f)
"""
import importlib
import os

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import pytest

rel_vocab = importlib.import_module("src.extraction.rel_vocab")
gate = importlib.import_module("src.wgm.gate")
main_api = importlib.import_module("src.api.main")
wgm_gate = importlib.import_module("src.wgm.gate")
_create_rel_type_in_flow = main_api._create_rel_type_in_flow


# R11 hermeticity (gauntlet, same class the sibling files paid for in R9/R10): the
# veto levers are read LIVE by rel_vocab/gate, and an ambient SPACY_MODEL beats
# the setdefault above — any of the four hostile env values spuriously reddens
# these parse-dependent pins (all critic-measured). Forced to the defaults here.
@pytest.fixture(autouse=True)
def _pin_rel_vocab_env_defaults(monkeypatch):
    monkeypatch.delenv("REL_TYPE_NEGATION_VETO", raising=False)
    monkeypatch.delenv("REL_TYPE_CITATION_VETO", raising=False)
    monkeypatch.delenv("REL_TYPE_VOCAB_NORMALIZER", raising=False)
    monkeypatch.setenv("SPACY_MODEL", "en_core_web_sm")


# ---------------------------------------------------------------- measured fixtures

ASKELL_SNIPPET = "\n".join([
    "Ryder, M. Subbiah, J. D. Kaplan, P . Dhari-",
    "wal, A. Neelakantan, P . Shyam, G. Sastry, A. Askell et al. , ",
    "\u201cLanguage models are few-shot learners,\u201d Advances in neural",
    "information processing systems, vol. 33, pp. 1877\u20131901, 2020.",
])
CHAN_SNIPPET = "\n".join([
    "Qian C, Cong X, Liu W, Yang C, Chen W, Su Y, Dang Y, Li J, Xu J, ",
    "Li S, Liu Z, Sun M. Communicative agents for software development. 2023, arXiv preprint arXiv: 2307.07924",
    "18. Chen W, Su Y, Zuo J, Yang C, Yuan C, Chan C, Yu H, Lu Y, Hung Y, ",
    "Qian C, Qin Y, Cong X, Xie R, Liu Z, Sun M, Zhou, J. Agentverse: ",
    "Facilitating multi-agent collaboration and exploring emergent behaviors in agents. arXiv preprint arXiv:2308.10848 .",
])
ISSN_SNIPPET = "\n".join([
    "Food Sci. Technol, Campinas, 43, e26423, 2023 1",
    "Original Article",
    "Food Science and Technology",
    "DOI: https://doi.org/10.5327/fst.26423",
    "ISSN 0101-2061 (Print)",
    "ISSN 1678-457X (Online)",
])
# THE CONTROL that killed the whole-snippet veto: 'proportional to' occurs on a prose
# line; the et-al citations sit on a DIFFERENT line of the same snippet.
PROPORTIONAL_SNIPPET = "\n".join([
    "Esters were detected at 0, 1, 3, and 7 days of storage, and their",
    "contents were proportional to longer storage time, with 3.08%",
    "at the 7th day. During storage, oxidation of unsaturated fatty",
    "acids and microbial action can lead to changes in the odor of",
    "crayfish meat. Hydrocarbons with high threshold values have",
    "little effect on food flavor, but some aromatic hydrocarbons",
    "have significant effects on the overall flavor of meat (Min et al.,",
    "1979; Zhu et al., 2019). The finding about the incr",
])


# ---------------------------------------------------------------- 1. normal form

@pytest.mark.parametrize("rt", [
    "cannot handle", "enhances performance", "retrieve entities_from",
    "takes into account", "y .", "USA", "cnn_2023", "",
    "austin, tx, usa",
])
def test_normalizer_refuses_measured_junk(rt):
    ok, why = rel_vocab.vocab_normal_form(rt)
    assert not ok, why


@pytest.mark.parametrize("rt", [
    "created_by", "instance_of", "coauthor_of", "friend_of", "works_for",
    "part_of", "member_of", "located_in", "has_state", "publish_in",
    "negatively_correlated_with",
    "max_percentage_during_storage",       # 4 tokens - measured healthy tail
    "stores_long_term_memory_in",          # 4 tokens - measured healthy tail
])
def test_normalizer_admits_measured_healthy(rt):
    ok, why = rel_vocab.vocab_normal_form(rt)
    assert ok, why


def test_normalizer_rejects_sentence_fragment_by_token_cap():
    # 7 snake tokens is a sentence riding an underscore; no measured healthy type
    # exceeds 4 tokens. (The modal fragment is ALSO caught by the negation gate -
    # this pin keeps the cap honest independently of the parser.)
    ok, _ = rel_vocab.vocab_normal_form("should_not_be_given_broad_autonomy_when_here")
    assert not ok


def test_normalizer_env_rollback(monkeypatch):
    monkeypatch.setenv("REL_TYPE_VOCAB_NORMALIZER", "0")
    assert rel_vocab.vocab_normalizer_enabled() is False
    ok, _ = rel_vocab.vocab_normal_form("cnn_2023")
    assert ok  # lever off -> pass-through
    monkeypatch.setenv("REL_TYPE_VOCAB_NORMALIZER", "1")
    assert rel_vocab.vocab_normalizer_enabled() is True


# ---------------------------------------------------------------- 2. negation via the spine parse

@pytest.mark.parametrize("rt", [
    "not_at", "cannot_handle", "never_watches", "might_not_understand",
    "does_not_like", "doesnt_trust", "is_not_valid", "wont_sync",
    "hasnt_run", "should_not_be_given_broad_autonomy_when",
    "not_inspects", "cannot_see", "never_fails_to_pay",
])
def test_negation_gate_refuses_measured_family(rt):
    negated, why = rel_vocab.phrase_is_negated(rt)
    assert negated, why


@pytest.mark.parametrize("rt", [
    "no_show",          # bare determiner 'no' on a NOUN - the parse (not a
                        # word list) is what keeps this healthy type alive
    "created_by", "works_for", "friend_of", "coauthor_of", "instance_of",
    "negatively_correlated_with",   # semantic negation in a healthy rel
    "part_of", "member_of", "located_in", "has_state",
])
def test_negation_gate_admits_healthy(rt):
    negated, why = rel_vocab.phrase_is_negated(rt)
    assert not negated, why


def test_negation_gate_env_rollback(monkeypatch):
    monkeypatch.setenv("REL_TYPE_NEGATION_VETO", "0")
    assert rel_vocab.negation_veto_enabled() is False
    negated, _ = rel_vocab.phrase_is_negated("not_at")
    assert not negated
    monkeypatch.setenv("REL_TYPE_NEGATION_VETO", "1")
    assert rel_vocab.negation_veto_enabled() is True


def test_negation_gate_fails_open_without_parser(monkeypatch):
    # fail-open contract: no parser available -> admit, never block ingest on the gate.
    # phrase_is_negated lazy-imports the spine's parser per call, so patch the SOURCE
    # module's availability predicate.
    monkeypatch.setattr("src.extraction.linguistics.linguistics_available", lambda: False)
    negated, why = rel_vocab.phrase_is_negated("not_at")
    assert not negated and "parser" in why


# ---------------------------------------------------------------- 3. citation frame lift

@pytest.mark.parametrize("rt,snippet", [
    ("askell", ASKELL_SNIPPET),
    ("chan", CHAN_SNIPPET),
    ("issn", ISSN_SNIPPET),
    ("hung", CHAN_SNIPPET),
])
def test_citation_gate_refuses_measured_reference_entries(rt, snippet):
    lifted, why = rel_vocab.citation_frame_lift(rt, snippet)
    assert lifted, why


@pytest.mark.parametrize("rt,snippet", [
    ("proportional_to", PROPORTIONAL_SNIPPET),  # THE measured control
    ("created_by", "The framework (Smith, 2020) maps each role created by the admin."),
    ("member_of", "each user is a member_of exactly one team"),
    ("works_for", "Maya works_for the harbour authority, 2023 audit division."),
])
def test_citation_gate_admits_prose_and_single_marker_lines(rt, snippet):
    lifted, why = rel_vocab.citation_frame_lift(rt, snippet)
    assert not lifted, why


def test_citation_gate_fail_open_on_empty_text():
    lifted, _ = rel_vocab.citation_frame_lift("askell", "")
    assert not lifted  # no text -> cannot judge -> admit (full text arrives at mint time)


def test_citation_gate_env_rollback(monkeypatch):
    monkeypatch.setenv("REL_TYPE_CITATION_VETO", "0")
    assert rel_vocab.citation_veto_enabled() is False
    lifted, _ = rel_vocab.citation_frame_lift("askell", ASKELL_SNIPPET)
    assert not lifted
    monkeypatch.setenv("REL_TYPE_CITATION_VETO", "1")
    assert rel_vocab.citation_veto_enabled() is True


# ---------------------------------------------------------------- 4. the WGM gate seams

def test_gate_refusal_helper_verdicts():
    assert gate._rel_type_growth_gate_refusal("cannot handle")  # normal form
    assert gate._rel_type_growth_gate_refusal("not_at")         # negated phrase
    assert gate._rel_type_growth_gate_refusal("not_at".replace("_", " "))  # space form
    assert gate._rel_type_growth_gate_refusal("created_by") is None
    assert gate._rel_type_growth_gate_refusal("no_show") is None
    assert gate._rel_type_growth_gate_refusal("stores_long_term_memory_in") is None


class _FakeGateCursor:
    def __init__(self, record):
        self._record = record

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        # lowercased: the assertions match SQL case-insensitively, like the real seams
        self._record.append((" ".join(sql.split()).lower(), params))

    def fetchone(self):
        return None


class _FakeGateDB:
    def __init__(self):
        self.executed = []
        self.committed = 0

    def cursor(self):
        return _FakeGateCursor(self.executed)

    def commit(self):
        self.committed += 1


def _gate_instance():
    g = gate.WGMValidationGate.__new__(gate.WGMValidationGate)
    g.db_conn = _FakeGateDB()
    g.registry = None
    g._invalidate_overlay = lambda: None
    return g


def test_auto_approve_refuses_junk_without_insert():
    g = _gate_instance()
    assert g._auto_approve_novel_type("cannot handle") is False
    assert g._auto_approve_novel_type("not_at") is False
    # NO write reached rel_types: the refusal happens before any INSERT
    assert not any("insert into rel_types" in s for s, _p in g.db_conn.executed)


def test_auto_approve_still_inserts_healthy_type():
    g = _gate_instance()
    assert g._auto_approve_novel_type("coauthor_of") is True
    assert any("insert into rel_types" in s for s, _p in g.db_conn.executed)


def test_try_approve_refuses_before_spending_llm_validation(monkeypatch):
    g = _gate_instance()
    called = {"llm": False}

    def _boom(**kw):
        called["llm"] = True
        raise AssertionError("LLM must not be called for a structurally refused string")

    monkeypatch.setattr(gate, "call_llm_with_retry_sync", _boom)
    assert g._try_approve_novel_type("never_watches") is False
    assert called["llm"] is False
    # and NOTHING was written - not rel_types, not pending_types
    assert g.db_conn.executed == []


def test_try_approve_defers_healthy_novel_type_path_intact(monkeypatch):
    # a HEALTHY novel type must still take the enrichment -> pending_types lane
    # (simulate LLM unavailable -> auto-approve fallback inserts): pinned so the
    # new gates did not amputate the growth valve itself.
    g = _gate_instance()
    monkeypatch.setattr(gate, "call_llm_with_retry_sync",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("llm down")))
    assert g._try_approve_novel_type("coauthor_of") is True  # fallback approved the healthy type
    assert any("insert into rel_types" in s for s, _p in g.db_conn.executed)

# ---------------------------------------------------------------- 5. mint seam wiring
# (bug 05 fix 1 / bug 03 remainder - the seam-scoped refusals: the polarity and the
#  citation frame are decidable ONLY at _create_rel_type_in_flow, where the request
#  text and the edge polarity ride the call. db=None proves refusal happens BEFORE
#  any DB use - the admitted-control pin uses a scripted fake so the INSERT is seen.)


class TestMintSeamWiring:
    def test_negated_polarity_refused_before_db(self, capsys):
        # FIX 1: a negated edge never mints vocabulary - the reading routes to the
        # fact's polarity column (the caller keeps the fact row, polarity='negated').
        assert _create_rel_type_in_flow(
            None, "collects", {}, 0.8,
            edge_polarity="negated") is False
        captured = capsys.readouterr()
        assert "negated_polarity_rel_type_mint_refused" in captured.out + captured.err

    def test_citation_frame_refused_before_db(self, capsys):
        # BUG-03 pin at the mint: the measured reference-entry snippet vetoes the
        # surname BEFORE any DB use; the candidate is refused, not quarantined.
        assert _create_rel_type_in_flow(
            None, "askell", {}, 0.8,
            source_text=ASKELL_SNIPPET) is False
        captured = capsys.readouterr()
        assert "citation_frame_rel_type_mint_refused" in captured.out + captured.err

    def test_no_source_text_fails_open(self, capsys):
        # /expand and the scalar-growth lanes pass no text: the citation gate cannot
        # judge and must not veto. The pin is the LOG ABSENCE of the citation refusal
        # (a False return alone is reachable via the structural guards and proves
        # nothing about this gate).
        assert _create_rel_type_in_flow(None, "askell", {}, 0.8) is False
        captured = capsys.readouterr()
        assert "citation_frame_rel_type_mint_refused" not in captured.out + captured.err

    def test_healthy_candidate_survives_citation_dense_text(self):
        # THE false-veto pin at the mint: a healthy predicate riding an ingest whose
        # text is FULL of citation markers still mints (0/60 controls measured).
        dense_text = PROPORTIONAL_SNIPPET + "\n" + ASKELL_SNIPPET
        g = _MintFakeDB()
        ok = _create_rel_type_in_flow(
            g, "proportional_to", {}, 0.8,
            source_text=dense_text, edge_polarity="affirmed")
        assert ok is True
        assert any("insert into rel_types" in s for s, _p in g.executed)


class _MintFakeCursor:
    def __init__(self, record):
        self._record = record

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._record.append((" ".join(sql.split()).lower(), params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _MintFakeDB:
    def __init__(self):
        self.executed = []
        self.committed = 0

    def cursor(self):
        return _MintFakeCursor(self.executed)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass


class TestCriticRound1Repro:
    """GAUNTLET CRITIC ROUND 1 (2026-11-03) - executed false-veto repros that the
    original 60-control corpus missed. The veto needs a STRONG genre marker
    (weight >= 1.5) on the containing line: weak markers (bare year, initials,
    venue-prep 'in', list numbers) STACK on ordinary prose and must NOT veto."""

    def test_author_prose_with_year_does_not_veto(self):
        lifted, why = rel_vocab.citation_frame_lift(
            "wrote_in", "J. K. Rowling wrote in 1997 and still lives in Edinburgh")
        assert not lifted, why

    def test_list_number_prose_does_not_veto(self):
        lifted, why = rel_vocab.citation_frame_lift(
            "moved_to", "1. In 2019 the team moved to Berlin")
        assert not lifted, why

    def test_weak_stack_never_reaches_veto_without_strong_marker(self):
        # initials + year + serial-ish digits but NO strong marker: survive
        lifted, why = rel_vocab.citation_frame_lift(
            "located_in", "T. Zhang, S. G. Patil works in building 2100-2 since 2019.")
        assert not lifted, why

    def test_strong_marker_still_vetoes(self):
        # the rule keeps the measured veto class intact
        lifted, why = rel_vocab.citation_frame_lift(
            "khandelwal", "Khandelwal et al. (2020a) build on the dense retriever")
        assert lifted, why


class TestRound3PreconjCorrelative:
    """GAUNTLET CRITIC ROUND 3 - executed MAJOR finding: spaCy labels the negator
    preconj (not neg) on the correlative 'not only X but also Y', so the UD-neg
    detector never saw not_only_stores_but_also_shares and it minted at both WGM
    seams. ROUND 4 tightened it: the head-constraint (VERB/AUX/ROOT) was itself a
    blind subclass (spaCy roots the second conjunct and tags the first NOUN/nsubj in
    not_only_comments_but_shares_posts), so the veto now fires on the preconj
    dependency + closed-class negator lemma ALONE - the preconj negator IS the folded
    polarity marker, whatever token spaCy rooted. Healthy correlatives admit."""

    def test_not_only_correlative_vetoes(self):
        negated, why = rel_vocab.phrase_is_negated("not_only_stores_but_also_shares")
        assert negated, why
        assert "neg_preconj_correlative" in why

    def test_not_only_predicate_head_variants_veto(self):
        for phrase in ("not_only_reads_but_also_writes", "not_just_stores_but_shares",
                       "stores_not_only_data"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert negated, (phrase, why)

    def test_healthy_correlatives_admit(self):
        for phrase in ("both_stores_and_shares", "either_sends_or_receives",
                       "created_by", "works_without_pay"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert not negated, (phrase, why)

    def test_negator_lemma_not_content_list(self):
        # the lemma set is CLOSED-CLASS function words only - a content word that is
        # not a negator never widens the veto
        assert "store" not in rel_vocab._CLOSED_NEGATOR_LEMMAS
        assert "without" not in rel_vocab._CLOSED_NEGATOR_LEMMAS


class TestRound4NsubjNounHeadSubclass:
    """GAUNTLET CRITIC ROUND 4 - executed MAJOR finding: the round-3 head-constraint
    (VERB/AUX/ROOT) missed the subclass where spaCy roots the SECOND conjunct and
    tags the first as NOUN/nsubj - the negator's head is a plain noun and these two
    exact strings minted end-to-end at both seams. The veto must not depend on which
    token spaCy rooted: preconj + negator lemma alone."""

    def test_nsubj_noun_head_variants_veto(self):
        for phrase in ("not_only_comments_but_shares_posts",
                       "not_only_comments_but_posts_updates"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert negated, (phrase, why)
            assert "neg_preconj_correlative" in why

    def test_gate_refusal_covers_noun_head_subclass(self):
        refusal = wgm_gate._rel_type_growth_gate_refusal(
            "not_only_comments_but_shares_posts")
        assert refusal is not None
        assert "neg_preconj_correlative" in refusal


class TestRound5FragmentShapesAndCharCap:
    """GAUNTLET CRITIC ROUND 5 - executed MINORs. (1) The 64-char cap existed only at
    the ontology/WGM seam: extraction-gated seams (rewrite filter, quarantine, edge
    filter, the rel_types INSERT) minted 65+-char strings the WGM seams refuse; the
    cap is now ONE shared value imported from the ontology module. (2) Bare-fragment
    negators (spaCy never labels them preconj): 'nor' is always cc, bare 'not' is
    ROOT/PART - both are folded negative polarity and now veto on their own
    dep/POS + closed-class lemma, no head constraint."""

    def test_char_cap_shared_across_seams(self):
        erv = importlib.import_module("src.extraction.rel_vocab")
        assert not erv.rel_type_vocabulary_ok(
            "internationalization_personalization_categorization_prioritization")
        assert erv.growth_veto_reason(
            "internationalization_personalization_categorization_prioritization") is not None
        # measured healthy tail unaffected
        assert erv.rel_type_vocabulary_ok("max_percentage_during_storage")
        # parity at the boundary: both modules agree 63 ok / 65 refuse
        assert erv.rel_type_vocabulary_ok("a" * 63)
        assert not erv.rel_type_vocabulary_ok("a" * 65)
        assert rel_vocab.vocab_normal_form("a" * 63)[0]
        assert not rel_vocab.vocab_normal_form("a" * 65)[0]

    def test_fragment_negators_veto(self):
        for phrase in ("not_only", "nor_rain_nor_snow", "not_ever", "neither_here_nor_there"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert negated, (phrase, why)

    def test_healthy_still_admits_after_fragment_rule(self):
        for phrase in ("also_known_as", "created_by", "instance_of", "works_without_pay",
                       "both_stores_and_shares"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert not negated, (phrase, why)

    def test_measured_negation_family_still_vetoes(self):
        for phrase in ("do_not_alter", "might_not_understand", "not_optimized_towards"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert negated, (phrase, why)


class TestRound6ScopeAdverbEscape:
    """GAUNTLET CRITIC ROUND 6 - executed MAJOR: spaCy attaches the negator to a SCOPE
    ADVERB (yet/necessarily/always/here) which attaches to the predicate, so loop 1's
    predicate check, the continuative grandchild hop, and the cc/ROOT branch ALL miss:
    not_yet_done minted at every seam. The blanket rule - a UD 'neg' label ANYWHERE in
    the folded name is the family (spaCy labels 'neg' only on true negators) - closes
    the class. Bare 'never' (ROOT/ADV, one token) vetoes via the widened ROOT branch.
    Measured before shipping: no_show/works_without_pay + the 0/60 control corpus carry
    no neg-labeled token; healthy admissions unchanged."""

    def test_scope_adverb_escape_class_vetoes(self):
        for phrase in ("not_yet_done", "not_yet_rated", "not_necessarily_true",
                       "not_always_right", "never_yet_failed", "not_here_working",
                       "not_yet_represent", "not_always_optimal"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert negated, (phrase, why)

    def test_bare_never_vetoes(self):
        negated, why = rel_vocab.phrase_is_negated("never")
        assert negated, why

    def test_blanket_neg_rule_no_false_positives(self):
        for phrase in ("no_show", "works_without_pay", "created_by", "also_known_as",
                       "instance_of", "max_percentage_during_storage",
                       "both_stores_and_shares"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert not negated, (phrase, why)


class TestRound7ContractionPartSplit:
    """GAUNTLET CRITIC ROUND 7 - executed MINOR: spaCy splits contractions capriciously.
    In cant/couldnt/dont the PART negator lands dep=neg (loop 1 catches), but in
    cannot/wouldnt it lands dep=punct - no branch fired and the bare strings minted at
    extraction + WGM (defended only by the chokepoint's function-word gate). A PART
    carrying a closed-negator lemma is never content: veto regardless of dep_."""

    def test_contraction_part_punct_vetoes(self):
        for phrase in ("cannot", "wouldnt"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert negated, (phrase, why)
            assert "neg_function_word_shape" in why

    def test_full_contraction_family_vetoes(self):
        for phrase in ("cant", "couldnt", "dont", "wont", "isnt", "arent", "wasnt",
                       "werent", "never", "not", "nor", "neither"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert negated, (phrase, why)

    def test_part_rule_no_content_false_positive(self):
        # content words are never PART - the rule cannot widen onto vocabulary
        for phrase in ("created_by", "works_without_pay", "no_show", "also_known_as",
                       "max_percentage_during_storage"):
            negated, why = rel_vocab.phrase_is_negated(phrase)
            assert not negated, (phrase, why)


class TestAdoptedNameIdentityLift:
    """ORCHESTRATOR ADOPTION (2026-11-03): the intruder lane's name-identity lift veto,
    ported into the consolidated module and MEASURED before shipping. A candidate whose
    content tokens are a non-empty subset of its OWN edge head/tail surface tokens is an
    entity mention promoted into the predicate slot (THE HARD LINE: a name is never a
    place). Measured: beltagy/usa lift family vetoes; healthy anchors with neutral
    surfaces admit; the measured-healthy 4-token tail admits on realistic surfaces.
    DISCLOSED BOUNDARY (measured, fail-closed): an endpoint surface that ECHOES the
    predicate noun (member_of <- 'the member of the family') false-vetoes - same
    disclosure class as the cant/wont homographs; re-measure before any fix."""

    def test_lift_family_vetoes(self):
        nl = rel_vocab.rel_type_is_name_lift
        gvr = rel_vocab.growth_veto_reason
        assert nl("beltagy", head_surface="Beltagy et al. propose a new retriever")
        assert nl("usa", tail_surface="austin, tx, usa")
        assert gvr("beltagy", "Beltagy et al. propose", "the retriever") == "name_identity_lift"

    def test_healthy_anchors_admit_neutral_surfaces(self):
        gvr = rel_vocab.growth_veto_reason
        for phrase in ("created_by", "instance_of", "coauthor_of", "friend_of", "member_of",
                       "works_at", "max_percentage_during_storage"):
            assert gvr(phrase, "Alice", "Bob") is None, phrase

    def test_measured_healthy_tail_admits_realistic_surfaces(self):
        gvr = rel_vocab.growth_veto_reason
        assert gvr("stores_long_term_memory_in", "the device", "long term memory") is None

    def test_no_surfaces_fails_open(self):
        nl = rel_vocab.rel_type_is_name_lift
        gvr = rel_vocab.growth_veto_reason
        # the gate cannot judge without surfaces: admit (structural guards still hold)
        # (round-10 critic: a disjunction here would swallow the very regression this
        # pin guards - the exact no-surfaces contract is pinned directly)
        assert gvr("beltagy", None, None) is None
        assert nl("beltagy", None, None) is False

    def test_echo_boundary_disclosed(self):
        gvr = rel_vocab.growth_veto_reason
        # the measured false-veto boundary: predicate noun echoed by the tail surface
        assert gvr("member_of", "Alice", "the member of the family") == "name_identity_lift"
        assert gvr("works_at", "Alice", "the works at the plant") == "name_identity_lift"


class TestRound12LeverMatrix:
    """GAUNTLET CRITIC ROUND 12 - executed MINOR: the REL_TYPE_VOCAB_NORMALIZER lever was
    honored only at the two WGM seams; growth_veto_reason -> rel_type_vocabulary_ok was
    lever-free, so the documented live-read rollback was narrower than written at the
    chokepoint-structural / triple filter / edge filter / quarantine / heuristic-insert
    families. The lever now reads inside the shared predicate - flipping it relaxes the
    normal-form/caps veto at EVERY consuming seam family (the negation + citation levers
    were already honored everywhere their detectors fire)."""

    def test_lever_off_relaxes_normal_form_everywhere(self, monkeypatch):
        gvr = rel_vocab.growth_veto_reason
        monkeypatch.setenv("REL_TYPE_VOCAB_NORMALIZER", "0")
        try:
            assert rel_vocab.rel_type_vocabulary_ok("cnn 2023") is True
            assert gvr("cnn_2023") is None          # shape-invalid, lever off
            assert gvr("a_b_c_d_e_f_g") is None     # 7-token, lever off
        finally:
            monkeypatch.delenv("REL_TYPE_VOCAB_NORMALIZER")

    def test_lever_on_default_vetoes(self, monkeypatch):
        gvr = rel_vocab.growth_veto_reason
        monkeypatch.setenv("REL_TYPE_VOCAB_NORMALIZER", "1")
        try:
            assert rel_vocab.rel_type_vocabulary_ok("cnn 2023") is False
            assert gvr("cnn_2023") == "vocabulary_normal_form"
            assert gvr("a_b_c_d_e_f_g") == "vocabulary_normal_form"
        finally:
            monkeypatch.delenv("REL_TYPE_VOCAB_NORMALIZER")

    def test_negation_lever_independent(self, monkeypatch):
        # the normalizer lever must NOT relax the negation veto (separate lever)
        monkeypatch.setenv("REL_TYPE_VOCAB_NORMALIZER", "0")
        try:
            assert rel_vocab.growth_veto_reason("not_inspects") == "negation_token"
        finally:
            monkeypatch.delenv("REL_TYPE_VOCAB_NORMALIZER")

class TestCapHistorySeedFloorVsGrownTail:
    """Re-adjudication (brain-picker lane reconciliation): BOTH cap measurements pinned
    so the next agent never re-litigates the token cap from one side alone.

    SEED FLOOR: migration 005's seed vocabulary (26 rel_types) maxes at 3 tokens
    (also_known_as) - the original brain-picker cap of 3 measured THIS floor, and it
    was correct FOR THE SEED.

    GROWN TAIL: the prod-grown vocabulary reaches 5 tokens on live healthy rows
    (stores_long_term_memory_in - 5 tokens/26 chars; max_percentage_during_storage -
    4 tokens/29 chars; bug-05 corpus, measured on the tenant rel_types table) - a
    3-token cap false-vetoes every one of them, and even a 4-cap would clip the 5-token
    row. The shipped cap is 6 = 2x the seed floor: admits the whole measured grown
    tail, refuses the measured 7-token clause fragments, and still bounds junk."""

    def test_seed_floor_is_three_tokens(self):
        # migration 005 is the seed-vocabulary source of truth; parse it, don't trust
        # this docstring.
        import re
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / "migrations" / "005_rel_types_table.sql"
        names = re.findall(r"\('([a-z_]+)',\s*'", p.read_text())
        assert len(names) >= 20  # the seed block is intact
        toks = {n: n.count("_") + 1 for n in names}
        assert max(toks.values()) == 3, "seed floor moved - re-measure the cap before changing it"
        assert "also_known_as" in toks and toks["also_known_as"] == 3
        # the seed holds NO row over 3: the 3-cap was seed-exact
        assert all(t <= 3 for t in toks.values())

    def test_cap_is_six_and_admits_the_grown_tail(self):
        # the shipped bound (2x seed floor); MAX_REL_TYPE_TOKENS is the public alias.
        assert rel_vocab.MAX_REL_TYPE_TOKENS == 6
        for grown, ntok in (("max_percentage_during_storage", 4), ("stores_long_term_memory_in", 5)):
            assert grown.count("_") + 1 == ntok
            assert rel_vocab.rel_type_vocabulary_ok(grown), grown  # a 3-cap (or even a 4-cap) would false-veto measured rows
            ok, why = rel_vocab.vocab_normal_form(grown)
            assert ok, (grown, why)

    def test_three_token_cap_false_veto_is_documented_by_construction(self):
        # the healthy grown tail sits at 4 tokens: prove the 3-cap WOULD have refused
        # it (this is the measurement that killed the 3-cap; keep it executable).
        grown = "max_percentage_during_storage"  # 4 tokens; the tail's 5-token row is pinned above
        assert grown.count("_") + 1 > 3
        # ...while the cap the code ships keeps refusing fragments:
        assert not rel_vocab.rel_type_vocabulary_ok("did_not_run_the_model_on_gpu")  # 7 tokens - past the 6-cap

class TestMigration307CorpusContract:
    """Re-adjudication addendum: the closed lane's migration 307 is the ONE retirement
    migration (a new one from this lane would be 308+; none needed - the mint veto
    refuses new junk at birth). Its predicates are verified HERE against the measured
    corpus contract by parsing the migration itself, so any drift from the code guard
    fails this test.

    Measured contract (canonical corpus parse: junk n=62, controls n=60):
    - the negator arm retires EXACTLY ONE control-named row, do_not_alter - the
      DISCLOSED true positive (bug-05:92, the sole neg-labeled junk verb); zero
      undisclosed false retires;
    - the token cap retires NO measured-healthy grown row
      (max_percentage_during_storage 4 tok, stores_long_term_memory_in 5 tok);
    - the seed vocabulary (migration 005) is untouched structurally and by the
      engine_generated IS TRUE authority filter (user > seed > growth)."""

    def _migration_src(self):
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / "migrations" / "307_retire_junk_vocabulary_hollow_groupings.sql"
        return p.read_text()

    def test_strict_form_and_negator_set_match_the_code_guard(self):
        import re
        import src.extraction.rel_vocab as rel_vocab
        src_ = self._migration_src()
        m = re.search(r"rel_type !~ '([^']+)'", src_)
        assert m and m.group(1) == '^[a-z]+(_[a-z]+)*$'
        m2 = re.search(r"rel_type ~ '\(\^\|_\)\(([^)]+)\)\(_\|\$\)'", src_)
        assert m2, 'negator regex shape'
        toks = m2.group(1).split('|')
        assert set(toks) == set(rel_vocab._CLOSED_NEGATOR_LEMMAS), '307 negator set drifted from the code guard'
        assert 'without' not in toks and 'no' not in toks  # their R4 edit + the bare-no carve

    def test_cap_arm_is_the_shipped_six_and_spares_the_grown_tail(self):
        import re
        src_ = self._migration_src()
        ms = re.findall(r"array_length\(string_to_array\((?:rel_type|candidate_rel_type), '_'\), 1\) > (\d+)", src_)
        assert ms == ['6', '6'], ms  # BOTH sites (rel_types + quarantine drain)
        for g in ('max_percentage_during_storage', 'stores_long_term_memory_in'):
            assert g.count('_') + 1 <= 6
            assert re.match('^[a-z]+(_[a-z]+)*$', g)  # strict form - survives 307

    def test_negator_arm_retires_only_the_disclosed_true_positive(self):
        import re
        m = re.search(r"rel_type ~ '([^']+)'", self._migration_src())
        neg = re.compile(m.group(1))
        assert neg.search('do_not_alter')  # the disclosed sole neg-labeled junk verb
        for c in ('proportional_to', 'uses_backbone', 'member_of', 'works_at', 'max_percentage_during_storage'):
            assert not neg.search(c), c  # no measured-healthy control name is touched

    def test_no_second_retirement_migration_shipped(self):
        # the addendum's numbering rule: 307 is theirs; a NEW retirement migration from
        # this lane would double-ship. The tree must hold exactly one 307 and no 308.
        from pathlib import Path
        mig = Path(__file__).resolve().parents[1] / "migrations"
        names = sorted(p.name for p in mig.glob('*.sql'))
        assert sum(1 for n in names if n.startswith('307_')) == 1
        assert not any(n.startswith('308_') for n in names)

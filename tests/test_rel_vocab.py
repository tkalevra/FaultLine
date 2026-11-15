# tests/test_rel_vocab.py - BUG-05 + BUG-03-remainder growth-guard pins.
#
# Two layers:
#   1. PURE predicates (src/extraction/rel_vocab.py) - the ticket-evidence strings MUST
#      veto and the house vocabulary MUST admit, incl. the measured false-veto traps
#      (no_show, noted_by, works_at).
#   2. FUNNEL pins - the vetoes are wired at the growth seams in src/api/main.py (both
#      mints, the quarantine, the edge finalisation filter, the rewrite triple filter);
#      each pin proves the seam honours the veto with NO database present (the guards
#      fire before any DB use - that ordering IS part of the contract). Log assertions
#      read stdout via capsys because the house structlog is a PrintLogger (vetoes never
#      become logging records).
from types import SimpleNamespace

import pytest

from src.extraction.rel_vocab import (
    MAX_REL_TYPE_TOKENS,
    growth_veto_reason,
    rel_type_has_negator,
    rel_type_is_name_lift,
    rel_type_vocabulary_ok,
)


# R12 hermeticity (gauntlet): the veto levers are read LIVE by the shared gate
# predicates — REL_TYPE_NEGATION_VETO=false alone critic-measured to redden 12
# pins here. Session baseline (conftest) already delenvs them; this keeps the
# file's own contract explicit when run standalone.
@pytest.fixture(autouse=True)
def _pin_rel_vocab_env_defaults(monkeypatch):
    monkeypatch.delenv("REL_TYPE_NEGATION_VETO", raising=False)
    monkeypatch.delenv("REL_TYPE_CITATION_VETO", raising=False)
    monkeypatch.delenv("REL_TYPE_VOCAB_NORMALIZER", raising=False)

# ---------- 1. pure predicates ----------


class TestVocabularyNormalForm:
    @pytest.mark.parametrize("rt", ["created_by", "works_for", "favorite_color",
                                     "instance_of", "coauthor_of", "publish_in"])
    def test_house_vocabulary_admits(self, rt):
        assert rel_type_vocabulary_ok(rt) is True

    def test_strict_snake_case(self):
        # single underscores joining letter groups only - no trailing/double underscores;
        # also_known_as is the longest seeded rel (3 tokens) and must admit.
        assert rel_type_vocabulary_ok("also_known_as") is True
        assert rel_type_vocabulary_ok("trailing_") is False
        assert rel_type_vocabulary_ok("a__b") is False

    def test_case_folds_to_normal_form(self):
        # the mint lowercases BEFORE the guard (house normal form), so the predicate
        # validates the NORMALIZED string - 'Cannot_Handle' folds to 'cannot_handle'.
        assert rel_type_vocabulary_ok("Cannot_Handle") is True
        assert rel_type_vocabulary_ok("cannot handle") is False  # the space survives

    @pytest.mark.parametrize("rt", ["cannot handle", "", None,
                                     "has digit2", "-leading", "_leading",
                                     "trailing_", "a__b", "_a_"])
    def test_non_normal_form_refused(self, rt):
        assert rel_type_vocabulary_ok(rt) is False

    def test_sentence_length_phrase_refused(self):
        # BUG-05 evidence: should_not_be_given_broad_autonomy_when (6 tokens)
        assert len("should_not_be_given_broad_autonomy_when".split("_")) > MAX_REL_TYPE_TOKENS
        assert rel_type_vocabulary_ok("should_not_be_given_broad_autonomy_when") is False


class TestNegatorTokens:
    @pytest.mark.parametrize("rt", ["not_inspects", "not_running_on", "never_watches",
                                     "cannot_incorporate_semantics_of",
                                     "might_not_understand", "do_not_alter",
                                     "should_not_be_given_broad_autonomy_when"])
    def test_bug05_negation_family_vetoes(self, rt):
        assert rel_type_has_negator(rt) is True

    @pytest.mark.parametrize("rt", ["no_show", "noted_by", "notion_of",
                                     "works_at", "created_by", "works_without_pay"])
    def test_bare_no_and_content_words_admit(self, rt):
        # bare "no" is deliberately NOT a negator (no_show is a genuine predicate);
        # noted/notion merely CONTAIN the letters n-o-t.
        assert rel_type_has_negator(rt) is False


class TestNameLift:
    def test_author_name_lift(self):
        # BUG-03: rel 'beltagy' minted from a sentence whose head is the author mention.
        assert rel_type_is_name_lift("beltagy", "Beltagy et al.", "the paper") is True

    def test_place_token_lift_from_tail(self):
        assert rel_type_is_name_lift("usa", None, "austin, tx, usa") is True

    def test_house_predicates_never_lift(self):
        assert rel_type_is_name_lift("works_at", "john", "acme corp") is False
        assert rel_type_is_name_lift("instance_of", "luna", "dog") is False
        assert rel_type_is_name_lift("created_by", "paper x", "someone") is False

    def test_no_surfaces_decides_false(self):
        assert rel_type_is_name_lift("beltagy", None, None) is False
        assert rel_type_is_name_lift("", "john", "acme") is False

    def test_function_tokens_never_collide(self):
        # works_at's content token is "works" - "at" is closed-class and dropped on
        # BOTH sides, so an endpoint phrase containing only "at" cannot flip the verdict.
        assert rel_type_is_name_lift("works_at", "john smith", "at microsoft") is False


class TestVetoReasonOrdering:
    def test_precedence_normal_form_first(self):
        assert growth_veto_reason("cannot handle") == "vocabulary_normal_form"
        assert growth_veto_reason("not_inspects") == "negation_token"
        assert growth_veto_reason("beltagy", "Beltagy et al.", "paper") == "name_identity_lift"

    def test_clean_candidate_has_no_reason(self):
        assert growth_veto_reason("created_by", "x", "y") is None

    def test_never_raises_on_garbage(self):
        for bad in (None, 123, [], {}, object()):
            assert growth_veto_reason(bad, bad, bad) in (None, "vocabulary_normal_form")


# ---------- 2. funnel pins (no DB present - the vetoes fire BEFORE any DB use) ----------


pytest.importorskip("src.api.main", reason="api module tree unavailable")
from src.api.main import (  # noqa: E402
    REL_TYPE_VOCAB_GUARD,
    _create_rel_type_in_flow,
    _drop_function_word_rel_edges,
    _quarantine_pending_rel,
    _vocab_guard_filter_triples,
)


class TestMintVeto:
    def test_name_lift_mint_refused_with_surfaces(self, capsys):
        # BUG-03 pin: the doc-lane carries the triple surfaces through the metadata
        # (_head_surface/_tail_surface stash keys) so the mint CAN decide the
        # name-identity class - a candidate that is its own edge's endpoint token is
        # refused here, exactly as at the rewrite triple filter.
        assert _create_rel_type_in_flow(
            None, "beltagy",
            {"_head_surface": "Beltagy et al.", "_tail_surface": "the library"},
            0.8) is False
        captured = capsys.readouterr()
        assert "name_identity_lift" in captured.out + captured.err

    @pytest.mark.parametrize("rt", ["cannot handle", "not_inspects",
                                     "should_not_be_given_broad_autonomy_when"])
    def test_mint_refused_without_db(self, rt, capsys):
        # db=None: an admitted candidate would explode on the first cursor use -
        # returning False WITHOUT touching db proves the veto fired first. The refusal
        # log line is the fail-loud surface (PrintLogger -> stdout).
        assert _create_rel_type_in_flow(None, rt, {}, 0.8) is False
        captured = capsys.readouterr()
        assert "junk_rel_type_mint_refused" in captured.out + captured.err


class TestQuarantineVeto:
    @pytest.mark.parametrize("rt", ["never_watches", "cannot handle"])
    def test_never_enters_queue(self, rt, capsys):
        _quarantine_pending_rel(None, rt, ["ANY"], ["ANY"], 0.8)
        captured = capsys.readouterr()
        assert "pending_rel_quarantine_refused" in captured.out + captured.err

    def test_name_lift_without_surfaces_is_not_vetoed_here(self, capsys):
        # HONEST BOUNDARY (BUG-03): the quarantine seam has no sentence surfaces, so a
        # name-only candidate is undecidable THERE - the name-identity veto lives at the
        # rewrite/document triple filter, which holds the surfaces (the measured
        # producers: arXiv survey dumps ride the document lane). Pinned so a future
        # "move the veto to the quarantine" patch must confront this boundary instead of
        # silently assuming coverage.
        _quarantine_pending_rel(None, "beltagy", ["ANY"], ["ANY"], 0.8)
        captured = capsys.readouterr()
        assert "pending_rel_quarantine_refused" not in captured.out + captured.err


class TestEdgeFilterDrops:
    def _edge(self, rt, subj="s", obj="o"):
        return SimpleNamespace(rel_type=rt, subject=subj, object=obj)

    def test_vocabulary_and_negation_dropped_with_sink(self):
        e1 = self._edge("cannot handle")
        e2 = self._edge("not_inspects")
        sink = []
        kept = _drop_function_word_rel_edges([e1, e2], dropped_sink=sink)
        assert kept == []
        assert [d["stage"] for d in sink] == ["vocab_guard_rel_type", "vocab_guard_rel_type"]

    def test_name_lift_edge_kept(self):
        # The FACT is user truth - a name-lift edge is not dropped here; its mint and
        # quarantine are refused downstream.
        e = self._edge("beltagy", "Beltagy et al.", "the paper")
        assert _drop_function_word_rel_edges([e]) == [e]

    def test_clean_edge_kept(self):
        e = self._edge("created_by", "paper", "person")
        assert _drop_function_word_rel_edges([e]) == [e]


class TestTripleFilter:
    def _t(self, rel, subj, obj):
        return {"rel_type": rel, "subject": subj, "object": obj}

    def test_bug03_citation_triples_dropped(self):
        triples = [
            self._t("beltagy", "Beltagy et al.", "the library"),
            self._t("usa", "the venue", "austin, tx, usa"),
            self._t("created_by", "the paper", "Beltagy"),
        ]
        kept = _vocab_guard_filter_triples(triples)
        assert len(kept) == 1 and kept[0]["rel_type"] == "created_by"

    def test_negation_and_normal_form_dropped(self):
        triples = [self._t("not_inspects", "auditor", "books"),
                   self._t("cannot handle", "system", "input")]
        assert _vocab_guard_filter_triples(triples) == []

    def test_flag_off_passthrough(self, monkeypatch):
        import src.api.main as m
        monkeypatch.setattr(m, "REL_TYPE_VOCAB_GUARD", False)
        triples = [self._t("not_inspects", "a", "b")]
        assert _vocab_guard_filter_triples(triples) == triples

    def test_flag_default_on_pin_grade(self):
        assert REL_TYPE_VOCAB_GUARD is True
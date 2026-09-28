"""Unit tests for src/extraction/reframe.py — the deterministic token-subset GUARDRAIL.

RC-reframe-atomize §7.1. These are PURE-FUNCTION tests: NO live DB, NO GLiNER2, NO LLM.
They verify the "USER IS TRUTH" guardrail that makes the LLM reframe safe to reintroduce:

  - Tier 1 (content tokens, message-scoped): a noun the user never typed → REJECT; a pronoun
    resolved to a noun present elsewhere in the message → ALLOW.
  - Tier 2 (numeric literals, span-scoped & strict): any digit-bearing token must be byte-intact
    in the SOURCE SPAN — a dropped IP octet or a reformatted date → REJECT.
  - Fabricated source (source not a substring of the message) → that atom dropped.
  - Filler-only turn → reframe returns [] (no LLM invoked in these tests).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.extraction import reframe as R


# ──────────────────────────────────────────────────────────────────────────────
# Tier 2 — numeric literal integrity (span-scoped, strict, byte-intact)
# ──────────────────────────────────────────────────────────────────────────────

class TestLiteralIntegrity:
    def test_ip_byte_intact_ok(self):
        msg = "set my server ip to 10.0.0.21 please"
        src = "my server ip to 10.0.0.21"
        atom = "my server ip is 10.0.0.21"
        assert R._guardrail_ok(atom, src, msg) is True

    def test_ip_dropped_octet_reject(self):
        msg = "set my server ip to 10.0.0.21 please"
        src = "my server ip to 10.0.0.21"
        atom = "my server ip is 10.0.0.2"   # dropped last octet
        assert R._guardrail_ok(atom, src, msg) is False

    def test_date_reformat_reject(self):
        # source "on 3/22", atom "on March 22" → 3 and 22 not verbatim in span → REJECT
        msg = "i fixed it on 3/22 by the way"
        src = "on 3/22"
        atom = "i fixed it on March 22"
        assert R._guardrail_ok(atom, src, msg) is False

    def test_number_relocated_reject(self):
        # atom introduces a digit literal absent from the source span
        msg = "i have 3 goats and 2 dogs"
        src = "i have 3 goats"
        atom = "i have 2 goats"
        assert R._guardrail_ok(atom, src, msg) is False

    def test_spelled_number_rides_tier1_ok(self):
        # "three weeks ago" is word-tokens → Tier 1; present in source → OK, byte-intact survives
        msg = "i just fixed the fence three weeks ago"
        src = "i just fixed the fence three weeks ago"
        atom = "i fixed the fence three weeks ago"
        assert R._guardrail_ok(atom, src, msg) is True


# ──────────────────────────────────────────────────────────────────────────────
# Tier 1 — content tokens (message-scoped pronoun allowance)
# ──────────────────────────────────────────────────────────────────────────────

class TestPronounAllowance:
    def test_pronoun_resolved_to_present_noun_ok(self):
        # "it" -> "the fence"; "fence" present elsewhere in the message → ALLOW
        msg = "i fixed the fence yesterday because it was broken"
        src = "it was broken"
        atom = "the fence was broken"
        assert R._guardrail_ok(atom, src, msg) is True

    def test_pronoun_resolved_to_absent_noun_reject(self):
        # same atom but "fence" never appears in the message → REJECT (invented noun)
        msg = "i fixed it yesterday because it was broken"
        src = "it was broken"
        atom = "the fence was broken"
        assert R._guardrail_ok(atom, src, msg) is False

    def test_determiner_swap_ok(self):
        # "that fence" → "the fence": function-word swap, no content introduced → ALLOW
        msg = "i fixed that fence on the east side"
        src = "i fixed that fence on the east side"
        atom = "i fixed the fence on the east side"
        assert R._guardrail_ok(atom, src, msg) is True


# ──────────────────────────────────────────────────────────────────────────────
# Invention — content token in neither span nor message
# ──────────────────────────────────────────────────────────────────────────────

class TestInvention:
    def test_invented_noun_reject(self):
        # "dealership" absent from the whole message → REJECT
        msg = "i bought a truck last week"
        src = "i bought a truck"
        atom = "i bought a truck from the dealership"
        assert R._guardrail_ok(atom, src, msg) is False

    def test_invented_value_reject(self):
        msg = "my dog is named fraggle"
        src = "my dog is named fraggle"
        atom = "my cat is named fraggle"   # "cat" never typed
        assert R._guardrail_ok(atom, src, msg) is False


# ──────────────────────────────────────────────────────────────────────────────
# Tier 3 — SUBJECT PRESERVATION (grammatical, spaCy-only, no word lists)
#
# Bug: the LLM atomizer turned "I have been attending the workshop ... last Saturday"
# into the subject-STRIPPED fragment "attending the workshop ... last Saturday". That
# is a strict token-subset of the source, so Tiers 1+2 passed it — but with no nsubj
# the deterministic spine can't ground (user, participated_in, workshop) and the fact
# fell to Class C (lost). Tier 3 rejects a subject-dropping atom → verbatim fallback.
# ──────────────────────────────────────────────────────────────────────────────

def _spacy_or_skip():
    from src.extraction.linguistics import _get_nlp
    if _get_nlp() is None:
        pytest.skip("spaCy model not available; Tier 3 fail-safes to PASS")


class TestSubjectPreservation:
    def test_first_person_subject_dropped_reject(self):
        # THE BUG CASE: "I" dropped → bare participial fragment, no nsubj → REJECT
        _spacy_or_skip()
        src = "I have been attending the workshop on Effective Time Management last Saturday"
        atom = "attending the workshop on Effective Time Management last Saturday"
        assert R._guardrail_ok(atom, src, src) is False

    def test_noun_subject_dropped_reject(self):
        # subject-agnostic: "son" dropped from "My son Theodore broke his leg" → REJECT
        _spacy_or_skip()
        src = "My son Theodore broke his leg"
        atom = "broke his leg"
        assert R._guardrail_ok(atom, src, src) is False

    def test_first_person_subject_kept_ok(self):
        _spacy_or_skip()
        src = "I attended the workshop"
        atom = "I attended the workshop"
        assert R._guardrail_ok(atom, src, src) is True

    def test_noun_subject_kept_ok(self):
        _spacy_or_skip()
        src = "My son Theodore broke his leg"
        atom = "My son Theodore broke his leg"
        assert R._guardrail_ok(atom, src, src) is True

    def test_compound_source_per_clause_split_not_false_rejected(self):
        # A legitimate per-clause split of a compound source must NOT be falsely rejected:
        # the atom covering the non-first-person clause keeps ITS subject ("son").
        _spacy_or_skip()
        src = "My son Theodore broke his leg and I drove him to the hospital"
        assert R._guardrail_ok("My son Theodore broke his leg", src, src) is True
        assert R._guardrail_ok("I drove him to the hospital", src, src) is True

    def test_single_first_person_reattribution_reject(self):
        # single first-person-subject source; a passivized reword loses the first-person
        # subject even though SOME nsubj ("car") survives → REJECT.
        _spacy_or_skip()
        src = "I drove the car"
        atom = "the car was driven"
        assert R._guardrail_ok(atom, src, src) is False

    def test_no_subject_source_passes(self):
        # source is itself a subjectless fragment → nothing to preserve → PASS (fail-safe)
        _spacy_or_skip()
        src = "workshop on time management"
        atom = "workshop on time management"
        assert R._guardrail_ok(atom, src, src) is True

    def test_apply_guardrail_subject_drop_falls_back_to_verbatim(self):
        # End-to-end through _apply_guardrail: the subject-stripped rewrite is discarded and
        # the TRUE ORIGINAL sentence (which retains "I") is substituted so capture is not lost.
        # Here source == the whole single-sentence message, so the true sentence IS that message.
        _spacy_or_skip()
        msg = "I have been attending the workshop on Effective Time Management last Saturday"
        raw_atoms = [{
            "statement": "attending the workshop on Effective Time Management last Saturday",
            "source": msg,
        }]
        out = R._apply_guardrail(raw_atoms, msg)
        assert len(out) == 1
        assert out[0].text == msg            # true sentence restored, subject "I" intact
        assert out[0].text.lower().startswith("i ")

    def test_subject_preserved_fail_safe_on_no_spacy(self, monkeypatch):
        # If spaCy is unavailable, Tier 3 must PASS (never crash, never a new false reject).
        monkeypatch.setattr("src.extraction.linguistics._get_nlp", lambda: None)
        assert R._subject_preserved("attending the workshop", "I attended the workshop") is True


# ──────────────────────────────────────────────────────────────────────────────
# _apply_guardrail — fabricated source + fallback-to-verbatim behavior
# ──────────────────────────────────────────────────────────────────────────────

class TestApplyGuardrail:
    def test_fabricated_source_dropped(self):
        # source not a substring of the message → atom dropped entirely (no verbatim anchor)
        msg = "i fixed the fence"
        raw_atoms = [{"statement": "i sold the car", "source": "i sold the car"}]
        out = R._apply_guardrail(raw_atoms, msg)
        assert out == []

    def test_invention_falls_back_to_verbatim_span(self):
        # valid source (substring) but the rewrite invents → keep the verbatim source as text
        msg = "i bought a truck last week"
        raw_atoms = [{"statement": "i bought a truck from the dealership",
                      "source": "i bought a truck"}]
        out = R._apply_guardrail(raw_atoms, msg)
        assert len(out) == 1
        assert out[0].text == "i bought a truck"          # rewrite discarded
        assert out[0].source_span == "i bought a truck"

    def test_clean_rewrite_kept(self):
        msg = "i just fixed that broken fence three weeks ago"
        raw_atoms = [{"statement": "i fixed the broken fence three weeks ago",
                      "source": "i just fixed that broken fence three weeks ago"}]
        out = R._apply_guardrail(raw_atoms, msg)
        assert len(out) == 1
        assert out[0].text == "i fixed the broken fence three weeks ago"   # clean rewrite kept

    def test_empty_atoms_in_yields_empty_out(self):
        assert R._apply_guardrail([], "anything") == []

    def test_non_dict_items_skipped(self):
        out = R._apply_guardrail(["not a dict", 42, None], "msg")
        assert out == []


# ──────────────────────────────────────────────────────────────────────────────
# Tier 3 — SUBJECT-DROP FALLBACK targets the TRUE ORIGINAL SENTENCE (the bug-fix half)
#
# The Tier-3 guardrail correctly REJECTS a subject-dropped atom, but the verbatim fallback
# used to substitute the LLM's own `source` field — which the model also stripped the subject
# from ("attending the workshop ... last Saturday"). Downstream then ran on a subject-less
# fragment, found no nsubj, and lost the fact to Class C. The fix: on a subject-drop rejection,
# fall back to the TRUE ORIGINAL sentence (subject intact) — the whole message for a single
# sentence, or the containing sentence within a multi-fact turn.
# ──────────────────────────────────────────────────────────────────────────────

class TestSubjectDropFallbackTarget:
    def test_source_also_subject_dropped_falls_back_to_true_sentence(self):
        # THE LIVE TRACE: the LLM set BOTH statement AND source to the subject-less fragment
        # (a byte-substring of the original, so the substring invariant passed). The old fallback
        # anchored to that subject-less `source` and lost capture. The fix anchors to the TRUE
        # sentence (with "I").
        _spacy_or_skip()
        full = "I have been attending the workshop on Effective Time Management last Saturday."
        dropped = "attending the workshop on Effective Time Management last Saturday"
        out = R._apply_guardrail([{"statement": dropped, "source": dropped}], full)
        assert len(out) == 1
        assert out[0].text == full                 # true sentence, subject "I" intact
        assert out[0].source_span == full
        assert out[0].text.lower().startswith("i ")

    def test_resulting_atom_text_now_captures_the_event(self):
        # Prove the fix RESTORES capture: analyze_event on the fallback text finds the workshop.
        _spacy_or_skip()
        from src.extraction import linguistics as L
        full = "I have been attending the workshop on Effective Time Management last Saturday."
        dropped = "attending the workshop on Effective Time Management last Saturday"
        out = R._apply_guardrail([{"statement": dropped, "source": dropped}], full)
        ev = L.analyze_event(out[0].text)
        assert ev is not None and ev.event == "workshop"
        # the OLD (mangled) fallback target did NOT capture — confirm the contrast
        assert L.analyze_event(dropped) is None

    def test_multi_fact_falls_back_to_containing_sentence_not_whole_turn(self):
        # A multi-sentence turn must NOT collapse to one giant atom: the subject-dropped 2nd atom
        # falls back to ITS containing sentence ("I attended the gala."), not the entire message.
        _spacy_or_skip()
        full = "My son broke his leg. I attended the gala."
        raw_atoms = [
            {"statement": "My son broke his leg.", "source": "My son broke his leg"},
            {"statement": "attended the gala", "source": "attended the gala"},
        ]
        out = R._apply_guardrail(raw_atoms, full)
        assert len(out) == 2
        assert out[0].text == "My son broke his leg."     # clean atom unaffected (kept rewrite)
        assert out[1].text == "I attended the gala."        # containing sentence, NOT whole turn
        assert out[1].text != full

    def test_clean_subject_keeping_atom_unaffected(self):
        # No false change: an atom that keeps its subject passes Tier 3 and keeps the LLM rewrite.
        _spacy_or_skip()
        full = "I attended the gala."
        out = R._apply_guardrail([{"statement": "I attended the gala.",
                                   "source": "I attended the gala"}], full)
        assert len(out) == 1
        assert out[0].text == "I attended the gala."        # rewrite kept, no fallback fired


# ──────────────────────────────────────────────────────────────────────────────
# Tier-3 PRECEDENCE over Tier-1/Tier-2 (the final Q2 bug).
#
# THE LIVE TRACE: for "I have been attending the workshop ... last Saturday." the LLM returned
#   statement="I attended the workshop ... last Saturday."   (kept "I", but tweaked attending→attended)
#   source   ="attending the workshop ... last Saturday"     (subject "I" DROPPED)
# The content tweak ("attended" ∉ message) makes Tier 1 return _GR_CONTENT, so the content `else`
# branch runs. The KEY INVARIANT (load-bearing): the spine must end up parsing the TRUE
# subject-intact sentence, NOT the subject-LESS source fragment (which it can't ground → Class C →
# no_ingest).
#
# ENUMERATION-FIX RESTRUCTURE: Tier 3 now triggers ONLY on the ATOM TEXT dropping the subject (the
# thing that flows downstream), NOT on the source span — because a correctly-split enumeration item
# carries the subject in its atom text but cites a bare subjectless NP as its source ("a dog named
# Fraggle"), and the old source-span check falsely rejected those GOOD atoms (the live merge bug).
# The source-subjectlessness now matters ONLY where the source is actually USED as downstream text
# (the content/numeric verbatim fallback), so `_apply_guardrail` anchors a subjectless source to its
# true containing sentence there. Net downstream behavior for this case is UNCHANGED (true sentence).
# ──────────────────────────────────────────────────────────────────────────────

class TestTier3Precedence:
    def test_subject_intact_atom_with_content_change_routes_content(self):
        # statement keeps "I" (subject intact) but ALSO changes a token (attending→attended); source
        # dropped "I". Tier 3 now passes (atom text has the subject); Tier 1 fires on the content
        # change → _GR_CONTENT. The subjectless-source SAFETY moves to the apply-level fallback
        # (next test), which still yields the true subject-intact sentence downstream.
        _spacy_or_skip()
        full = "I have been attending the workshop on Effective Time Management last Saturday."
        statement = "I attended the workshop on Effective Time Management last Saturday."
        source = "attending the workshop on Effective Time Management last Saturday"
        assert R._guardrail_check(statement, source, full) == R._GR_CONTENT

    def test_apply_guardrail_falls_back_to_true_sentence_and_captures_event(self):
        # End-to-end: the subject-drop precedence yields the TRUE subject-intact sentence (NOT the
        # content `source`-span fallback), and analyze_event then captures the workshop.
        _spacy_or_skip()
        from src.extraction import linguistics as L
        full = "I have been attending the workshop on Effective Time Management last Saturday."
        statement = "I attended the workshop on Effective Time Management last Saturday."
        source = "attending the workshop on Effective Time Management last Saturday"
        out = R._apply_guardrail([{"statement": statement, "source": source}], full)
        assert len(out) == 1
        assert out[0].text == full                  # true sentence, subject "I" intact
        assert out[0].source_span == full
        ev = L.analyze_event(out[0].text)
        assert ev is not None and ev.event == "workshop"
        # contrast: the OLD content fallback target (the subject-less source) does NOT capture
        assert L.analyze_event(source) is None

    def test_content_violation_with_subject_intact_still_returns_content(self):
        # NO-REGRESSION: Tier 3 only wins when the SUBJECT was dropped. A genuine content invention
        # whose subject is intact still returns _GR_CONTENT → verbatim source-span fallback.
        _spacy_or_skip()
        full = "I bought a car yesterday."
        statement = "I bought a Toyota car yesterday."   # "toyota" invented, subject "I" kept
        source = "I bought a car yesterday"
        assert R._guardrail_check(statement, source, full) == R._GR_CONTENT
        out = R._apply_guardrail([{"statement": statement, "source": source}], full)
        assert out[0].text == source                  # verbatim source span (subject intact)


class TestContainingSentence:
    def test_single_sentence_returns_whole_message(self):
        _spacy_or_skip()
        full = "I attended the gala on Friday."
        assert R._containing_sentence(full, "attended the gala") == full

    def test_multi_sentence_returns_the_one_containing_span(self):
        _spacy_or_skip()
        full = "My son broke his leg. I attended the gala."
        assert R._containing_sentence(full, "attended the gala") == "I attended the gala."

    def test_ambiguous_span_falls_back_to_whole_message(self):
        # span present in more than one sentence → fail-safe to the whole turn (never a drop)
        _spacy_or_skip()
        full = "I attended the gala. I attended the workshop."
        assert R._containing_sentence(full, "attended") == full

    def test_empty_span_returns_whole_message(self):
        assert R._containing_sentence("hello world.", "") == "hello world."

    def test_no_spacy_falls_back_to_whole_message(self, monkeypatch):
        # spaCy unavailable → single-sentence assumption → whole message (subject intact)
        monkeypatch.setattr("src.extraction.linguistics.segment_clauses",
                            lambda _m: (_ for _ in ()).throw(RuntimeError("no spacy")))
        full = "My son broke his leg. I attended the gala."
        assert R._containing_sentence(full, "attended the gala") == full


# ──────────────────────────────────────────────────────────────────────────────
# reframe_to_atomic — fail-safe / flag behavior (no LLM call exercised)
# ──────────────────────────────────────────────────────────────────────────────

class TestReframeFailSafe:
    def test_empty_text_returns_empty(self):
        import asyncio
        res = asyncio.run(R.reframe_to_atomic("   ", "u"))
        assert res.atoms == []
        assert res.used_llm is False

    def test_flag_off_returns_empty_no_llm(self, monkeypatch):
        import asyncio
        monkeypatch.setenv("REFRAME_ENABLED", "false")
        res = asyncio.run(R.reframe_to_atomic("i fixed the fence", "u"))
        assert res.atoms == []
        assert res.used_llm is False  # short-circuit before any LLM call


# ──────────────────────────────────────────────────────────────────────────────
# tokenizer sanity — dotted/dated/IP literals stay one token
# ──────────────────────────────────────────────────────────────────────────────

class TestTokenizer:
    def test_ip_is_single_token(self):
        assert "10.0.0.21" in R._content_tokens("ip 10.0.0.21")

    def test_email_is_single_token(self):
        assert "foo@bar.com" in R._content_tokens("email foo@bar.com")

    def test_date_slash_is_single_token(self):
        assert "3/22" in R._content_tokens("on 3/22")

    def test_function_words_excluded(self):
        ct = R._content_tokens("the fence is on the east side")
        assert "the" not in ct and "is" not in ct and "on" not in ct
        assert "fence" in ct and "east" in ct and "side" in ct

    def test_literals_only_digit_tokens(self):
        lits = R._literals("i fixed the fence on 3/22 with 10.0.0.21")
        assert lits == {"3/22", "10.0.0.21"}


# ──────────────────────────────────────────────────────────────────────────────
# ENUMERATION FIX — a correctly-split list item carries the shared subject in its ATOM TEXT
# but cites a bare subjectless NP as its `source` ("a dog named Fraggle" out of "We have a cat …,
# a dog named Fraggle, …"). The Tier-3 source-span check used to REJECT these GOOD atoms and
# substitute the whole run-on sentence → the live Fraggle/Slinky entity-merge bug. The guardrail
# must now ACCEPT them (atom text has the subject), while still blocking genuine fabrication.
# ──────────────────────────────────────────────────────────────────────────────

_PET_MSG = ("We have a cat named Goose, a dog named Fraggle, he is a morkie, "
            "and a pet corn snake named Slinky")


class TestEnumerationSubjectlessSource:
    def test_enumeration_item_with_subjectless_source_is_accepted(self):
        _spacy_or_skip()
        # atom text keeps the shared subject "We"; source is the bare NP fragment.
        assert R._guardrail_check("We have a dog named Fraggle.",
                                  "a dog named Fraggle", _PET_MSG) == R._GR_OK

    def test_enumeration_item_keeps_clean_statement_not_run_on(self):
        _spacy_or_skip()
        out = R._apply_guardrail(
            [{"statement": "We have a dog named Fraggle.", "source": "a dog named Fraggle"}],
            _PET_MSG)
        assert len(out) == 1
        assert out[0].text == "We have a dog named Fraggle."   # the clean atom, NOT the whole turn
        assert out[0].rejected is False

    def test_coref_resolved_atom_is_accepted(self):
        _spacy_or_skip()
        # "he is a morkie" → "Fraggle is a morkie": "Fraggle" appears earlier in the SAME message,
        # so Tier 1 (message-scoped) admits it, and the source has its own subject ("he").
        assert R._guardrail_check("Fraggle is a morkie.", "he is a morkie", _PET_MSG) == R._GR_OK

    def test_fabricated_noun_still_blocked(self):
        _spacy_or_skip()
        # "labrador" is nowhere in the message → genuine invention → still rejected.
        assert R._guardrail_check("We have a dog named Fraggle, a labrador.",
                                  "a dog named Fraggle", _PET_MSG) == R._GR_CONTENT

    def test_rejected_flag_false_when_source_equals_statement(self):
        # METRIC FIX: an OK atom whose model `source` equals its `statement` (the common single-fact
        # case) must NOT be counted as rejected (the old text==source_span heuristic false-counted it).
        out = R._apply_guardrail(
            [{"statement": "My wife is Nora.", "source": "My wife is Nora."}],
            "My wife is Nora.")
        assert len(out) == 1
        assert out[0].rejected is False


# ──────────────────────────────────────────────────────────────────────────────
# Tier 2.5 — VERB INTEGRITY (grammatical, lemma-level, CONTENT-verb scope)
#
# The LLM reframe/atomizer is DETECT/SEGMENT/PASS-ALONG-ONLY — it may split and de-noise the
# user's prose but must NEVER swap the CONTENT verb of a fact. Live bug: "CVE-2026-5555 affects
# Microsoft Exchange" atomized with the verb "manages" (absent from the source) → the spine
# faithfully produced the fabricated relation manages(cve, exchange). Message-scoped Tier 1
# waves a verb borrowed from a sibling clause/sentence straight through; Tier 2.5 pins the atom's
# content-verb lemmas to the fact's OWN containing sentence. CONTENT verbs only — AUX/copula is
# exempt so the copula split + affect/preference seams are untouched. Subject-agnostic, no lists.
# ──────────────────────────────────────────────────────────────────────────────

class TestVerbIntegrity:
    def test_reworded_verb_rejected_single_sentence(self):
        # THE BUG: "affects" → "manages" in a single-sentence enumeration → REJECT (content).
        _spacy_or_skip()
        msg = "CVE-2026-5555 affects Microsoft Exchange, Apache Tomcat, and nginx."
        assert R._guardrail_check("CVE-2026-5555 manages Microsoft Exchange.",
                                  "CVE-2026-5555 affects Microsoft Exchange", msg) == R._GR_CONTENT

    def test_reworded_verb_rejected_bare_np_source(self):
        # Same swap when the model cites a bare-NP source — the containing sentence still pins it.
        _spacy_or_skip()
        msg = "CVE-2026-5555 affects Microsoft Exchange, Apache Tomcat, and nginx."
        assert R._guardrail_check("CVE-2026-5555 manages Microsoft Exchange.",
                                  "Microsoft Exchange", msg) == R._GR_CONTENT

    def test_verb_borrowed_from_sibling_sentence_rejected(self):
        # "manages" appears in a DIFFERENT sentence of the turn → message-scoped Tier 1 alone would
        # pass it; Tier 2.5 (containing-sentence scope) rejects the cross-sentence borrow.
        _spacy_or_skip()
        msg = "CVE-2026-5555 affects Microsoft Exchange. Our team manages the patch rollout."
        assert R._guardrail_check("CVE-2026-5555 manages Microsoft Exchange.",
                                  "CVE-2026-5555 affects Microsoft Exchange", msg) == R._GR_CONTENT

    def test_reworded_verb_fallback_carries_true_verb(self):
        # End-to-end: the rejected atom falls back to the verbatim source, which carries "affects".
        _spacy_or_skip()
        msg = "CVE-2026-5555 affects Microsoft Exchange, Apache Tomcat, and nginx."
        out = R._apply_guardrail(
            [{"statement": "CVE-2026-5555 manages Microsoft Exchange.",
              "source": "CVE-2026-5555 affects Microsoft Exchange"}], msg)
        assert len(out) == 1
        assert "affects" in out[0].text.lower()
        assert "manages" not in out[0].text.lower()
        assert out[0].rejected is True

    def test_domain_agnostic_med_verb_swap_rejected(self):
        # Subject-agnostic: medical domain, "inhibits" → "blocks".
        _spacy_or_skip()
        assert R._guardrail_check("Aspirin blocks COX-1.",
                                  "Aspirin inhibits COX-1", "Aspirin inhibits COX-1.") == R._GR_CONTENT

    def test_domain_agnostic_law_verb_swap_rejected(self):
        # Subject-agnostic: legal domain, "binds" → "controls".
        _spacy_or_skip()
        assert R._guardrail_check("The treaty controls all signatories.",
                                  "The treaty binds all signatories",
                                  "The treaty binds all signatories.") == R._GR_CONTENT

    def test_same_verb_inflection_passes_verb_gate(self):
        # Same lemma, different inflection ("affect" → "affects") must PASS the verb gate (lemma-
        # level). (Tier 1 may still reject on the raw token — that is separate, pre-existing, and
        # its verbatim-source fallback preserves the verb anyway.)
        _spacy_or_skip()
        from src.extraction.reframe import _verb_integrity_ok
        assert _verb_integrity_ok("CVE-2026-5555 affects Microsoft Exchange.",
                                  "CVE-2026-5555 affect Microsoft Exchange",
                                  "CVE-2026-5555 affect Microsoft Exchange") is True

    def test_coordinated_split_same_verb_accepted(self):
        # Legit atomization: per-member split reusing the SAME source verb must PASS the verb gate,
        # even when the model cites a bare-NP source (head verb "have" lives in the containing sent).
        _spacy_or_skip()
        from src.extraction.reframe import _verb_integrity_ok
        msg = "I have a dog named Rex and a cat named Milo."
        assert _verb_integrity_ok("I have a dog named Rex.", "a dog named Rex", msg) is True
        assert _verb_integrity_ok("I have a cat named Milo.", "a cat named Milo", msg) is True

    def test_copula_split_not_touched(self):
        # AUX/copula atoms have no content verb → verb gate is a no-op (PASS); the parent/feeling
        # copula seams stay intact.
        _spacy_or_skip()
        from src.extraction.reframe import _verb_integrity_ok
        assert _verb_integrity_ok("My mother is Carol.", "My mother is Carol",
                                  "My mother is Carol and my father is Paul.") is True
        assert _verb_integrity_ok("I am excited.", "I am excited", "I am excited.") is True

    def test_feeling_content_verb_matches_source(self):
        # "feel" is a content VERB but it matches the source → PASS (the affect seam is untouched).
        _spacy_or_skip()
        from src.extraction.reframe import _verb_integrity_ok
        assert _verb_integrity_ok("I feel worried.", "I feel worried", "I feel worried.") is True

    def test_spacy_unavailable_fail_safe_pass(self, monkeypatch):
        # Fail-safe: spaCy unavailable → verb gate PASSes (Tiers 1+2 decide, no worse than today).
        from src.extraction import reframe as RR
        monkeypatch.setattr("src.extraction.linguistics._get_nlp", lambda: None)
        assert RR._verb_integrity_ok("CVE-2026-5555 manages Microsoft Exchange.",
                                     "CVE-2026-5555 affects Microsoft Exchange",
                                     "CVE-2026-5555 affects Microsoft Exchange.") is True

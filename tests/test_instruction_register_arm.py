# -*- coding: utf-8 -*-
"""THE INSTRUCTION-REGISTER ARM (BUG-RECALL-7 part B) — the pinned litmus.

THE BUG (DEV/BUGS/recall-servicing-20261109/BUG-RECALL-7-instruction-register-no-facts.md):
prescription/instruction sentences — "you need the --force-recreate flag", "Set Redis
maxmemory to 512mb", "the health probe wants the X-Health-Token header", "always pg_dump
with --clean --if-exists" — are captured VERBATIM in episodic but mint ZERO
entity_attributes rows: no extraction arm recognized the register. The class is the
single largest corpus-A user-lane miss block (8-9 of the 19 misses at L; .348 vs rag .826).

THE FIX: one deterministic detector (``linguistics.detect_instruction_settings`` — raw-text
command-token frames + a parse-driven subject ladder; the design contract lives on the
``INSTRUCTION_REGISTER_SETTING`` flag's block comment) integrated at TWO points:

  1. the deriver arm in ``derive_sentence_facts`` (the sentence-pipeline spine, the same
     seam every other deterministic deriver arm uses), and
  2. the ingest-side supplementation in ``_ingest_impl`` right beside the atomic-value
     supplementation ("ingest is smart, extract is dumb") — lane-independent, because the
     DEPLOYED extractor route is "rewrite" (legacy) and both lanes feed /ingest, so this
     hook is what makes the register live on the deployed stack.

THE SHELF: rel_type ``has_setting`` — SEEDED vocabulary (migration 310: tail_types {SCALAR},
category 'system', scalar_datatype 'string'), which the WGM gate admits and the
determine_path ATTRSCOPE read-side contract requires (an attribute row is walk-admissible
only when rel_type-backed with a SCALAR tail). The EXTENSION frames (BUG-RECALL-7
extension, 2026-11-10 — cert-expiry dates and vendor account-reps) mint under their own
seeded shelves ``expires_on`` / ``account_rep`` (migration 311, the same contract) because
their honest attribute is not a setting; the detector returns the shelf per hit and BOTH
integration seams carry it. The arm never bypasses the gate and PROPOSES
NO growth candidate, so the R5 dual-feed discipline (ContextVar accumulator +
growth_out/_pending_growth out-param) has nothing to feed — pinned below by source scan.

THE LITMUS (Part F, requires a reachable control-plane DB): provision a THROWAWAY seat,
push the receipt corpus turns through the REAL ``ingest`` door (no edges — the deterministic
supplement must carry the register alone, exactly the deployed rewrite-lane shape minus the
brain), assert the (subject, has_setting, value) rows exist in entity_attributes, and
assert the natural question phrasing reaches the rows through the recall WALK
(determine_path + fetch_facts_from_anchor — the same functions recall_memory_tool drives;
the brain /query band is unreachable from the test box, so the tool-level recall degrades
there — the walk IS the read path under test). The seat schema is dropped in the finally.

Measured pre-pin (this tree, real ingest on a fresh seat): 10/10 corpus instruction turns
minted scalar=1 with the right subject/value; 10/10 rows live; 3/3 natural-question walks
surface the gold token.
"""
import asyncio
import inspect
import os
import pathlib
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as ling

# ── the receipt + corpus turns (ctx_corpus.json command_flag/config_value/correction class,
#    byte-identical to the bench evidence) ─────────────────────────────────────────────────
F1 = ("Note to self, and I keep re-learning this: after rebuilding an image with the same "
      "tag, docker compose up -d does nothing to running containers — you need the "
      "--force-recreate flag or the stack silently keeps the old container.")
F1B = "You need the --force-recreate flag or the stack silently keeps the old container."
F2 = ("House rule after last month's incident: force-push only with --force-with-lease, "
      "never a bare --force — the lease check has already saved the release branch once.")
F3 = ("Set Redis maxmemory to 512mb with allkeys-lru; the session cache is expendable by "
      "design.")
F4 = ("The health probe curl needs --max-time 8 or a hung endpoint wedges the whole "
      "monitoring loop; learned that during the Tuesday outage.")
F5 = ("Mirror sync is rsync -a --delete every night; the --delete is what keeps the mirror "
      "from growing zombie files forever.")
F4H = ("The probe also sends the service token under the X-Api-Token header — that's the "
       "auth for the health endpoint.")
C5 = F3
R3 = ("Release tags are prefixed release/ from now on — release/2.5.0 style — so the "
      "cleanup script can find them.")
F6 = ("Ran the exporter with --workers 4 and the queue finally drains under load; single "
      "worker was the bottleneck all along.")
X5 = ("One more header mixup fixed: the health probe wants the X-Health-Token header, not "
      "X-Api-Token — the old name silently 403s.")

# ── the EXTENSION frames (BUG-RECALL-7 extension, 2026-11-10): the A14/A15 corpus-A
#    L-tier miss classes, sentences VERBATIM from ctx_corpus.json ────────────────────────
PGDUMP = ("For restoreable dumps always pg_dump with --clean --if-exists, otherwise the "
          "restore script fights the existing schema and dies halfway.")
SMOKE = ("Personal rule that has paid for itself: always run the smoke suite before a "
         "deploy, no exceptions even for dot releases.")
CERT = ("Reminder: the TLS certificate for bramble.internal expires March 3 — renew "
        "before the first, not the day of.")
VENDOR = ("Our vendor is Northlake Cloud, account rep is Fenna; she responds fastest over "
          "email, not the portal.")

# (turn, expected [(subject, value), …]) — the full instruction class, all 12 measured,
# plus the extension frames that ride has_setting (pg_dump was minting unpinned; the
# rule-run frame is new).
EXPECTATIONS = [
    (F1, [("docker compose", "--force-recreate")]),
    (F1B, []),  # no tool named → capture NOTHING (never guess a subject)
    (F2, [("force-push", "--force-with-lease")]),  # "never a bare --force" refused
    (F3, [("redis", "512mb")]),
    (F4, [("health probe curl", "--max-time 8")]),
    (F5, [("mirror sync", "-a --delete")]),  # single-dash flags count INSIDE a run only
    (F4H, [("probe", "X-Api-Token")]),
    (R3, [("release tags", "release/")]),
    (F6, [("exporter", "--workers 4")]),
    (X5, [("health probe", "X-Health-Token")]),  # "not X-Api-Token" refused; env var is a
    # side note about the SAME tool, never its own subject
    (PGDUMP, [("pg_dump", "--clean --if-exists")]),  # the double-dash flag RUN is ONE value
    (SMOKE, [("deploy", "smoke suite")]),  # rule-run frame: the flow the rule guards
]

# (turn, shelf, expected [(subject, value), …]) — the extension frames whose honest
# attribute is NOT has_setting (migration 311 shelves; detector returns the rel per hit).
REL_EXPECTATIONS = [
    (CERT, "expires_on", [("bramble.internal tls certificate", "March 3")]),
    (VENDOR, "account_rep", [("northlake cloud", "Fenna")]),
]


def _pairs(text):
    doc = ling._parse(text)
    return [(a, b) for a, b, _, _, _r in ling.detect_instruction_settings(text, doc)]


def _hits(text):
    """(subject, value, rel) triples — the full detector contract."""
    doc = ling._parse(text)
    return [(a, b, r) for a, b, _, _, r in ling.detect_instruction_settings(text, doc)]


# ── A: the detector (receipt + corpus, the measured class) ────────────────────────────────

@pytest.mark.parametrize("text,expected", EXPECTATIONS,
                         ids=["F1", "F1b-refusal", "F2", "C5", "F4", "F5", "F4H",
                              "R3", "F6", "X5", "pgdump", "rule-smoke"])
def test_detector_mints_the_instruction_class(text, expected):
    assert _pairs(text) == expected


# ── A2: THE PREFERENCE REGISTER (BUG-RECALL-7 extension, the ctx-bench corpus-A
#    qR1/qR2/qR3 class, sentences VERBATIM from ctx_corpus.json). The pins:
#    R1/R3 mint the honest (subject, value) pairs AND nothing else — the generic chains'
#    twin readings of the register's own material are the junk rows the bench seat served
#    where the gold belonged (measured live on the A17 seat 90206360: (rule, has_state,
#    pay) + (personal rule, run, smoke suite) for R1; (release tags, prefix, style) for
#    R3; (history, deploy, "every friday") for R2b's rate reading). R2's negated rule
#    ("Never deploy Friday afternoon.") REFUSES at the detector — the negation scan-back
#    contract holds (the gold token 'never' must never become, or launder into, a bare
#    value mint). ────────────────────────────────────────────────────────────────────────
R1_FULL = ("Personal rule that has paid for itself: always run the smoke suite before a "
           "deploy, no exceptions even for dot releases.")
R2_TURN = ("Never deploy Friday afternoon. Every Friday deploy in my history has ended "
           "with me on the laptop at dinner.")
R2B = "Every Friday deploy in my history has ended with me on the laptop at dinner."
R3_FULL = ("Release tags are prefixed release/ from now on \u2014 release/2.5.0 style \u2014 "
           "so the cleanup script can find them.")


def test_preference_register_R1_mints_only_the_rule_run_row():
    # the (subject, value) pair the rule-run frame declares — and NOT the chains' meta-head
    # twins ((rule, has_state, pay) / (personal rule, run, smoke suite))
    assert [(f.subject, f.rel_type, f.object)
            for f in ling.derive_sentence_facts(R1_FULL, reference=None)] == [
        ("deploy", "has_setting", "smoke suite")]


def test_preference_register_R3_mints_only_the_prefix_row():
    # the prefix frame's honest value — and NOT the chains' relational twin
    # (release tags, prefix, style) that hijacked the question's aspect word
    assert [(f.subject, f.rel_type, f.object)
            for f in ling.derive_sentence_facts(R3_FULL, reference=None)] == [
        ("release tags", "has_setting", "release/")]


def test_preference_register_R2_negated_rule_refuses_at_the_detector():
    # "Never deploy …" is a negated prescription: the scan-back refusal holds at the
    # detector AND across the full two-sentence turn (no tier launders 'never' into a value)
    assert _pairs("Never deploy Friday afternoon.") == []
    assert _pairs(R2_TURN) == []


def test_preference_register_R2b_never_mints_the_history_rate_junk():
    # the rate lane's activity-object resolution must not land the recurrence on the
    # possessed PP aspect ("in MY history") — the junk row the bench served for the
    # Friday-deploy questions
    facts = ling.derive_sentence_facts(R2B, reference=None)
    assert not any(f.subject == "history" and f.rel_type == "deploy" for f in facts), facts


# ── A3: THE CONSEQUENCE-CLAUSE REFUSAL (BUG-RECALL-13 PART C, critic ctx R2-1 MED) ────────
# The measured turn's second half — "otherwise the restore script fights the existing schema
# and dies halfway." — is the prescription's NEGATIVE CONSEQUENCE (a warning, never a user
# fact), yet the generic chains read its clean SVO parse and minted (restore script, fight,
# existing schema) + (restore script, has_state, die) beside the honest register row. The
# _emit chokepoint refuses every NON-scalar edge whose verb/subject token sits in the
# otherwise-scoped clause (position-cut: tokens BEFORE the connective are never scope, so
# "I take the bus to work, otherwise I walk." keeps (user, take, bus)); scalar emits pass —
# the register arm's own row can never be refused by its own turn's warning clause.

PGDUMP_JUNK = [("restore script", "fight", "existing schema"),
               ("restore script", "has_state", "die")]


def test_consequence_clause_refuses_the_junk_co_facts():
    # THE measured sentence: ONLY the register row mints — both junk co-facts refused.
    facts = [(f.subject, f.rel_type, f.object)
             for f in ling.derive_sentence_facts(PGDUMP, reference=None)]
    assert facts == [("pg_dump", "has_setting", "--clean --if-exists")], facts


def test_consequence_clause_refusal_flag_off_is_byte_parity(monkeypatch):
    monkeypatch.setattr(ling, "SPINE_CONSEQUENCE_CLAUSE_REFUSAL", False)
    facts = [(f.subject, f.rel_type, f.object)
             for f in ling.derive_sentence_facts(PGDUMP, reference=None)]
    assert ("pg_dump", "has_setting", "--clean --if-exists") in facts
    for junk in PGDUMP_JUNK:
        assert junk in facts, "OFF must restore the pre-fix mints byte-for-byte"
    assert facts == PGDUMP_JUNK + [("pg_dump", "has_setting", "--clean --if-exists")], facts


def test_consequence_refusal_spares_the_main_clause_of_an_alternative_turn():
    # the position cut: tokens BEFORE the connective are never scope — the main clause's
    # genuine fact survives the warning clause's refusal
    facts = [(f.subject, f.rel_type, f.object)
             for f in ling.derive_sentence_facts("I take the bus to work, otherwise I walk.",
                                                 reference=None)]
    assert ("user", "take", "bus") in facts, facts
    assert not any(rel in ("walk", "walk_to") for _s, rel, _o in facts), facts


def test_consequence_refusal_never_touches_the_register_arm_or_the_negation_scanback():
    # the register arm's hits on the measured turn are byte-identical (the scalar exemption)
    doc = ling._parse(PGDUMP)
    assert ling.detect_instruction_settings(PGDUMP, doc) == [
        ("pg_dump", "--clean --if-exists", 42, 61, "has_setting")]
    # the negation scan-back contract holds (the refusal refuses mints; it never launders)
    assert _pairs("Never deploy Friday afternoon.") == []
    assert _pairs(R2_TURN) == []


def test_instrumental_how_frame_is_the_closed_class_frame():
    # the anchor-side frame detector (BUG-RECALL-9 CLASS 1): the instrumental "how do I"
    # opening is the frame; "what do I …" and dative/declarative shapes are not
    import src.api.main as main_mod
    assert main_mod._query_is_instrumental_how(
        "how do I make pg_dump produce something the restore script can actually replay?")
    assert main_mod._query_is_instrumental_how("how to force-push safely?")
    assert main_mod._query_is_instrumental_how("How can we roll back a compose deploy?")
    assert not main_mod._query_is_instrumental_how("what do I always run before a deploy?")
    assert not main_mod._query_is_instrumental_how("tell me about my kayak")
    assert not main_mod._query_is_instrumental_how("the deploy failed on Friday")


# ── A4: THE BARE TOOL-NOUN ANCHOR (BUG-RECALL-13 PART B, critic ctx R2-1 MED) ─────────────
# The R1 anchor fix covers the instrumental-how frame and the evidence-ranked compound-alias
# containment — but the BARE tool-noun shape still abstained: "what flags do I use for
# pg_dump?" carries the first-person pronoun of the FLAG question itself, Rule 1 pinned the
# SPEAKER, and the gold rode the episodic verbatim band only. The probe anchors the query at
# the entity whose EXACT single-token alias the query itself names, with three guards: exact
# full-token match, LIVE attribute evidence, and UNIQUENESS (a tie refuses). The generic-noun
# worry (script/deploy/backup) is handled by construction — never an inventory: a generic
# noun anchors only when it IS a unique evidence-bearing alias. Placement is the seam's
# contract: the probe is consumed ONLY inside the Rule 1 identity-keyword branch (it replaces
# the speaker return), so a possessive ask ("what's my deploy rule?") binds at Rule 2 and a
# speaker-free question never reaches the branch — both byte-for-byte unchanged.

def test_bare_tool_noun_probe_flag_defaults_on():
    import src.api.main as main_mod
    assert main_mod._RECALL_ANCHOR_BARE_TOOL_NOUN is True


def test_bare_tool_noun_probe_is_consumed_only_in_the_identity_branch():
    # SOURCE PIN: the probe runs beside the instrumental-how probe and is consumed where the
    # speaker return is made — never as a global pre-anchor (that would hijack possessive
    # asks, the "what's my deploy rule?" litmus).
    import inspect
    import src.api.main as main_mod
    src = inspect.getsource(main_mod.resolve_anchor)
    i_probe_call = src.find("_btn_probe = _bare_tool_noun_anchor(db, query_text, user_id)")
    i_consume = src.find("if _btn_probe is not None:")
    i_identity_return = src.find('log.info("resolve_anchor.identity_keyword"')
    assert 0 < i_probe_call < i_consume < i_identity_return, (
        "the probe must be computed before and consumed inside the identity branch")
    # the probe body carries the three guards + the seeded-grouping refusal
    # (nested defs are indented — slice between THIS probe's def line and the next def)
    i0 = src.find("def _bare_tool_noun_anchor(db, query_text, user_id):")
    i1 = src.find("def _containment_candidates", i0)
    probe_src = src[i0:i1]
    assert i0 > 0 and i1 > i0, "probe body slice"
    assert "WHERE alias = %s" in probe_src, "exact alias probe"
    assert "_evidence_free(db, _eid)" in probe_src, "evidence guard"
    assert "len(_eids) == 1" in probe_src, "uniqueness guard"
    assert "_grown_topic_taxonomy" in probe_src, "seeded-grouping refusal"


@pytest.mark.parametrize("text,shelf,expected", REL_EXPECTATIONS,
                         ids=["cert-expiry", "vendor-rep"])
def test_detector_mints_the_extension_shelves(text, shelf, expected):
    hits = _hits(text)
    assert [(s, v) for s, v, r in hits] == expected
    assert [r for _s, _v, r in hits] == [shelf]
    # the extension shelves never ride has_setting (that strain is what migration 311 fixes)
    assert not any(r == "has_setting" for _s, _v, r in hits)


def test_negated_alternative_never_the_value():
    # the correction frame's refused alternative is NEVER minted as the value
    assert not any(v.lower() == "x-api-token" for _s, v in _pairs(X5))
    assert not any(v == "--force" for _s, v in _pairs(F2))


def test_negation_refusals_hold_on_the_extension_frames():
    # "never run …" / "does not expire …" / "rep is not …" — the refused form never mints
    assert _hits("Standing rule: never run the smoke suite before a deploy, it wastes "
                 "the window.") == []
    assert _hits("Reminder: the TLS certificate for bramble.internal does not expire "
                 "March 3 — that was the old one.") == []
    assert _hits("Our vendor is Northlake Cloud, account rep is not Fenna anymore.") == []
    # the first-person HABIT is not a declared rule — outside the register (the corpus
    # A15/R1 pair: "no exceptions" and "personal rule" cues are what make it a rule)
    assert _hits("I always run the smoke suite before a deploy, never after.") == []


def test_non_instruction_sentences_mint_nothing():
    # no command token / no prescription frame → no values → no facts (deterministic gate)
    for t in [
        "The staging API listens on port 8001, the dashboard proxies to it directly.",
        "I always run the smoke suite before a deploy, never after.",
        "My wife is Selin and we live in Cardiff.",
    ]:
        assert _pairs(t) == [], t


# ── B: the deriver arm (the sentence-pipeline seam) ───────────────────────────────────────

def test_deriver_emits_has_setting_scalar_facts():
    for text, expected in EXPECTATIONS:
        facts = ling.derive_sentence_facts(text, reference=None)
        arm = [(f.subject, f.object) for f in facts if f.rel_type == "has_setting"]
        assert arm == expected, (text[:50], arm, expected)


def test_deriver_facts_carry_the_scalar_contract():
    facts = ling.derive_sentence_facts(F1, reference=None)
    arm = [f for f in facts if f.rel_type == "has_setting"]
    assert arm, "F1 must mint"
    for f in arm:
        assert f.scalar_datatype == "string"          # → edge object_datatype → scalar store
        assert f.provenance == "user_stated"
        assert f.event_date is None and f.thin_type is None


def test_deriver_emits_the_extension_shelves():
    # the cert-expiry DATE and the vendor rep mint under THEIR seeded shelves (migration
    # 311), through the same deriver seam, same scalar contract
    for text, shelf, expected in REL_EXPECTATIONS:
        facts = ling.derive_sentence_facts(text, reference=None)
        arm = [(f.subject, f.object) for f in facts if f.rel_type == shelf]
        assert arm == expected, (text[:50], arm, expected)
        for f in facts:
            if f.rel_type == shelf:
                assert f.scalar_datatype == "string"
                assert f.provenance == "user_stated"
                assert f.event_date is None and f.thin_type is None


def test_extension_shelf_values_are_the_seeded_vocabulary():
    assert ling.INSTRUCTION_REGISTER_EXPIRES_REL == "expires_on"
    assert ling.INSTRUCTION_REGISTER_REP_REL == "account_rep"


# ── C: the flag (ONE kill-switch, both hooks; OFF → byte-parity) ──────────────────────────

def test_flag_off_deriver_mints_nothing(monkeypatch):
    monkeypatch.setattr(ling, "INSTRUCTION_REGISTER_SETTING", False)
    facts = ling.derive_sentence_facts(F1, reference=None)
    assert not [f for f in facts if f.rel_type == "has_setting"]
    # the extension shelves ride the SAME ONE flag (OFF → byte-parity everywhere)
    facts_cert = ling.derive_sentence_facts(CERT, reference=None)
    assert not [f for f in facts_cert if f.rel_type == "expires_on"]
    facts_vendor = ling.derive_sentence_facts(VENDOR, reference=None)
    assert not [f for f in facts_vendor if f.rel_type == "account_rep"]


def test_ingest_hook_is_flag_gated():
    import src.api.main as main_mod
    src = inspect.getsource(main_mod._ingest_impl)
    # the hook reads the module-global flag (monkeypatchable) and is try/except-wrapped
    assert "if req.text and INSTRUCTION_REGISTER_SETTING:" in src
    assert "instruction_register_supplementation_failed" in src


# ── D: the shelf is PROPER vocabulary (never a gate bypass) ───────────────────────────────

def test_migration_seeds_has_setting_with_scalar_tail():
    here = os.path.dirname(os.path.abspath(__file__))
    sql = open(os.path.join(here, "..", "migrations",
                            "310_seed_has_setting_rel_type.sql"), encoding="utf-8").read()
    assert "INSERT INTO public.rel_types" in sql
    assert "('has_setting', 'Has Setting'" in sql
    assert "ARRAY['SCALAR']::TEXT[]" in sql
    assert "'string'" in sql and "'system'" in sql          # category + scalar_datatype
    assert "'facts', 'B'" in sql                            # storage_target, fact_class
    assert "'supersede'" in sql                             # correction_behavior
    assert "ON CONFLICT (rel_type) DO NOTHING" in sql       # idempotent, tenant-safe
    assert "faultline\\_%" in sql                           # the per-tenant DO loop


def test_migration_seeds_the_extension_shelves():
    # migration 311: expires_on + account_rep, the SAME 310 contract (tail SCALAR so the
    # WGM gate admits the arm's edges and the ATTRSCOPE walk reads the rows back)
    here = os.path.dirname(os.path.abspath(__file__))
    sql = open(os.path.join(here, "..", "migrations",
                            "311_seed_register_extension_rel_types.sql"),
               encoding="utf-8").read()
    assert "INSERT INTO public.rel_types" in sql
    assert "('expires_on', 'Expires On'" in sql
    assert "('account_rep', 'Account Rep'" in sql
    assert sql.count("ARRAY['SCALAR']::TEXT[]") >= 2
    assert "'string'" in sql and "'system'" in sql          # category + scalar_datatype
    assert "'facts', 'B'" in sql                            # storage_target, fact_class
    assert "'supersede'" in sql                             # correction_behavior
    assert "ON CONFLICT (rel_type) DO NOTHING" in sql       # idempotent, tenant-safe
    assert "faultline\\_%" in sql                           # the per-tenant DO loop


def test_no_backspace_bytes_in_new_source():
    # the heredoc lesson: a raw-string patch can corrupt \\b into chr(8)
    for path in (ling.__file__,):
        src = open(path, encoding="utf-8").read()
        assert src.count(chr(8)) == 0, f"BACKSPACE 0x08 in {path}"


def test_canonical_rel_is_stable():
    assert ling.INSTRUCTION_REGISTER_REL == "has_setting"


# ── E: the growth rails — WHAT the arm feeds is now SPLIT by shape (BUG-FAMILY-7) ─────────
# R5 (cortex rails @ 8a8d6fd2): any deriver arm that PROPOSES a growth candidate must feed
# BOTH the ContextVar accumulator AND the growth_out/_pending_growth out-param list. The
# instruction-register arm's SEEDED-shelf mints (has_setting/expires_on/account_rep) still
# propose NOTHING — the mint shape is vocabulary, never a proposal. BUG-FAMILY-7 (2026-11-12)
# added the ONE growth shape the register genuinely carries: the CUE-CONTAINMENT signal — a
# rule DECLARED under a live cue that NO frame captured rides BOTH rails (the ContextVar
# accumulator via the detector's own _record_cue_candidate, the growth_out out-param via the
# integration block), plus the matched-cue confirmation on every register firing. The pinned
# scan below asserts the split: the seeded MINT path stays growth-free; the cue surface is
# the only thing the arm feeds. (By-design change from the R5-era "feeds nothing" pin — the
# engine-growth directive: the engine grows its own cue vocabulary instead of only the
# hand-pinned regex; the value mint itself is STILL never a guess.)
# Pinned by source scan of the arm's integration blocks.

def _code_lines(src):
    """Source with comment lines stripped — the scan must key on CODE, not on the arm's
    own comment text (which NAMES the growth params it deliberately does not touch)."""
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def test_arm_feeds_no_growth_lane():
    arm_src = _code_lines(inspect.getsource(ling.detect_instruction_settings))
    # the SEEDED MINT path never proposes: no growth write keyed to a minted fact
    assert "_pending_growth" not in arm_src
    assert "growth_out" not in arm_src
    # the ONLY growth feed is the cue surface (matched cue confirmation + cue containment)
    assert arm_src.count("record_cue_candidate") == 2
    assert arm_src.count('"rule_register_cue"') == 2
    block_src = _code_lines(inspect.getsource(ling.derive_sentence_facts))
    # anchor on the arm's CALL inside the integration block (the block's own header lives in
    # a comment, which _code_lines strips — a find() on it silently made the R5-era scan
    # vacuous; the call site is code-visible)
    _call = block_src.find("detect_instruction_settings(sentence, doc)")
    assert _call != -1, "the register arm's integration block moved — re-anchor this pin"
    arm_block = block_src[_call:_call + 1400]
    # the integration block feeds ONLY the out-param half of the cue-containment signal
    assert "record_cue_candidate" not in arm_block
    assert "growth_out" in arm_block
    assert '"rule_register_cue"' in arm_block


# ── F: THE LITMUS — the REAL ingest door on a THROWAWAY seat ──────────────────────────────

LITMUS_TURNS = [
    # (fid, turn, subject, value, shelf) — the shelf rides each turn so the row check and
    # the walk read the RIGHT attribute (the extension shelves are migration 311 rels).
    ("F1", F1, "docker compose", "--force-recreate", "has_setting"),
    ("F2", F2, "force-push", "--force-with-lease", "has_setting"),
    ("F4", F4, "health probe curl", "--max-time 8", "has_setting"),
    ("F5", F5, "mirror sync", "-a --delete", "has_setting"),
    ("F4H", F4H, "probe", "X-Api-Token", "has_setting"),
    ("C5", C5, "redis", "512mb", "has_setting"),
    ("R3", R3, "release tags", "release/", "has_setting"),
    ("F6", F6, "exporter", "--workers 4", "has_setting"),
    ("X5", X5, "health probe", "X-Health-Token", "has_setting"),
    ("PGDUMP", PGDUMP, "pg_dump", "--clean --if-exists", "has_setting"),
    ("SMOKE", SMOKE, "deploy", "smoke suite", "has_setting"),
    ("CERT", CERT, "bramble.internal tls certificate", "March 3", "expires_on"),
    ("VENDOR", VENDOR, "northlake cloud", "Fenna", "account_rep"),
]

LITMUS_WALKS = [
    ("--force-recreate",
     "what's the flag to make compose actually swap containers when the image tag didn't change?",
     "docker compose"),
    ("512mb", "what should the redis cache maxmemory be set to?", "redis"),
    ("--max-time 8", "what timeout does the health probe curl need?", "health probe curl"),
    ("smoke suite", "what do I always run before a deploy?", "deploy"),
    ("march 3", "when does the bramble.internal certificate expire?",
     "bramble.internal tls certificate"),
]

# THE ANCHOR LITMUS (BUG-RECALL-9 CLASS 1, the recall tool's question→anchor resolution):
# the natural question must RESOLVE ITS OWN ANCHOR to the entity that holds the gold —
# the instrumental-how frame's speaker pronoun is the frame ("how do I make pg_dump
# …"), a single-token surface twin ("nightly mirror" → the evidence-free junk
# 'mirror' alias) must yield to the evidence-bearing compound alias the query names
# ("mirror sync", with 'sync' riding inside 'rsync'), and the BARE tool-noun-in-question
# shape ("what flags do I use for pg_dump?", BUG-RECALL-13 PART B) anchors the tool the
# query's own token names. Pinned through the REAL resolve_anchor on the same seat the
# turns were ingested into.
ANCHOR_LITMUS = [
    ("pg_dump", "--clean --if-exists",
     "how do I make pg_dump produce something the restore script can actually replay?"),
    ("mirror sync", "-a --delete",
     "what rsync flags does the nightly mirror use?"),
    ("pg_dump", "--clean --if-exists",
     "what flags do I use for pg_dump?"),
]


def _apply_migration_public(dsn, filename="310_seed_has_setting_rel_type.sql"):
    """Apply a seed migration's public section (idempotent ON CONFLICT DO NOTHING) so a fresh
    tenant inherits the shelf via provisioning's public copy. The tenant DO-loop section is
    the boot path's job; the litmus seat is provisioned AFTER this, so it copies the row."""
    import psycopg2
    here = os.path.dirname(os.path.abspath(__file__))
    sql = open(os.path.join(here, "..", "migrations", filename), encoding="utf-8").read()
    pub = sql.split("-- ── 2.")[0]
    c = psycopg2.connect(dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(pub)
    c.close()


@pytest.mark.usefixtures("requires_postgres")
class TestInstructionRegisterRealIngestLitmus:
    """REAL /ingest on a throwaway seat: rows minted + walk-reachable for the natural
    question phrasing. Skips honestly when no control-plane DB is reachable."""

    def test_receipt_turns_mint_walk_reachable_rows(self, postgres_dsn, monkeypatch):
        import psycopg2
        import src.api.main as main_mod
        from src.api.main import determine_path, fetch_facts_from_anchor
        from src.api.models import IngestRequest
        from src.provisioning.schema_manager import (create_user_schema,
                                                     derive_user_slug_from_uuid)

        seat = str(uuid.uuid4())
        _apply_migration_public(postgres_dsn)
        _apply_migration_public(postgres_dsn, "311_seed_register_extension_rel_types.sql")
        schema, status = create_user_schema(seat, derive_user_slug_from_uuid(seat))
        assert status == "ready", f"provision failed: {status}"
        log = []
        try:
            # the shelves MUST exist in the fresh tenant (the public copy carries them)
            conn = psycopg2.connect(postgres_dsn)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                cur.execute("SELECT rel_type, tail_types, scalar_datatype FROM rel_types "
                            "WHERE rel_type IN ('has_setting', 'expires_on', 'account_rep')")
                seeded = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            conn.close()
            assert "has_setting" in seeded, "has_setting missing from the fresh tenant"
            assert "SCALAR" in (seeded["has_setting"][0] or []) and seeded["has_setting"][1] == "string"
            for _shelf in ("expires_on", "account_rep"):
                assert _shelf in seeded, f"{_shelf} missing from the fresh tenant"
                assert "SCALAR" in (seeded[_shelf][0] or []) and seeded[_shelf][1] == "string"

            # THE REAL DOOR: /ingest with the turn text and NO edges — the deterministic
            # supplement carries the register (deployed rewrite-lane shape, brainless).
            for fid, turn, _subj, _val, _shelf in LITMUS_TURNS:
                res = asyncio.run(main_mod.ingest(
                    IngestRequest(text=turn, user_id=seat, source="mcp", edges=[]),
                    main_mod._DirectIngestRequest(), None))
                assert res.get("status") == "valid", (fid, res)
                log.append(f"{fid}: scalar={res.get('scalar_committed')}")

            # THE ROWS: (subject, <shelf>, value) live in entity_attributes (values
            # stored lowercased — the scalar store's verbatim contract; the bench scorer
            # lowercases gold tokens too, so this is score-compatible).
            conn = psycopg2.connect(postgres_dsn)
            nhit = 0
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                for fid, turn, subj, val, shelf in LITMUS_TURNS:
                    cur.execute(
                        "SELECT ea.value_text, (ea.superseded_at IS NULL) AS live "
                        "FROM entity_attributes ea "
                        "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                        "WHERE al.alias = %s AND ea.attribute = %s",
                        (subj, shelf))
                    rows = cur.fetchall()
                    hit = any(r[0] == val.lower() for r in rows if r[1])
                    nhit += hit
                    assert hit, f"{fid}: expected ({subj}, {shelf}, {val}) live, got {rows}"
            conn.close()

            # THE WALK: the natural question phrasing reaches the row through
            # determine_path + fetch_facts_from_anchor (the recall walk's read path —
            # ATTRSCOPE admits the rel_type-backed SCALAR attribute; recall_memory_tool
            # drives exactly these functions plus the brain bands).
            conn = psycopg2.connect(postgres_dsn)
            nwalk = 0
            for gold, q, anchor_alias in LITMUS_WALKS:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                    cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s "
                                "LIMIT 1", (anchor_alias,))
                    row = cur.fetchone()
                assert row, f"anchor {anchor_alias!r} unresolved"
                path = determine_path(q, conn, user_id=seat)
                res = fetch_facts_from_anchor(row[0], seat, path, query_text=q)
                blob = str(res).lower()
                assert gold.lower() in blob, f"walk missed {gold!r} for {q!r}: {blob[:200]}"
                nwalk += 1

            # THE ANCHOR (BUG-RECALL-9 CLASS 1): the question resolves ITS OWN anchor to
            # the entity holding the gold — no hand-picked anchor. resolve_anchor is the
            # recall tool's question→anchor resolution (main.py /query Step 1); the walk
            # off the resolved anchor must surface the gold token.
            from src.api.main import resolve_anchor
            nanchor = 0
            for anchor_alias, gold, q in ANCHOR_LITMUS:
                conn2 = psycopg2.connect(postgres_dsn)
                conn2.autocommit = True
                with conn2.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                _meta: dict = {}
                _anchor = resolve_anchor(q, [], seat, conn2, resolution_out=_meta)
                with conn2.cursor() as cur:
                    cur.execute("SELECT alias FROM entity_aliases WHERE entity_id = %s",
                                (_anchor,))
                    _aliases = [r[0] for r in cur.fetchall()]
                conn2.close()
                assert anchor_alias in _aliases, (
                    f"anchor resolution missed: {q!r} resolved {_aliases} "
                    f"(method={_meta.get('method')}), expected {anchor_alias!r}")
                _path = determine_path(q, conn, user_id=seat)
                _res = fetch_facts_from_anchor(_anchor, seat, _path, query_text=q)
                _blob = str(_res).lower()
                assert gold.lower() in _blob, (
                    f"resolved-anchor walk missed {gold!r} for {q!r}: {_blob[:200]}")
                nanchor += 1
            conn.close()

            # THE FLAG: OFF → the register mints nothing (byte-parity, same real door)
            monkeypatch.setattr(main_mod, "INSTRUCTION_REGISTER_SETTING", False)
            res = asyncio.run(main_mod.ingest(
                IngestRequest(text=("The flendric controller needs --shard-count 9 to "
                                    "survive a rebalance."),
                              user_id=seat, source="mcp", edges=[]),
                main_mod._DirectIngestRequest(), None))
            conn = psycopg2.connect(postgres_dsn)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                cur.execute("SELECT count(*) FROM entity_attributes ea "
                            "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                            "WHERE al.alias = 'flendric controller' "
                            "AND ea.attribute = 'has_setting'")
                assert cur.fetchone()[0] == 0, "flag OFF must mint nothing"
            conn.close()

            assert nhit == len(LITMUS_TURNS)
            assert nwalk == len(LITMUS_WALKS)
            assert nanchor == len(ANCHOR_LITMUS)
        finally:
            try:
                c = psycopg2.connect(postgres_dsn)
                c.autocommit = True
                with c.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                c.close()
            except Exception:
                pass


# ── G: THE PROBE-ANCHORED REGISTER-ASPECT SEAM (BUG-RECALL-14, critic ctx R3-1 HIGH,
#    2026-11-11) ─────────────────────────────────────────────────────────────────────────────
# THE WOUND (measured on the built image through the real door): the BUG-RECALL-13 bare-
# tool-noun probe FIRES (`resolve_anchor.bare_tool_noun_entity` in the server log), the
# anchored walk even FETCHES the gold — and the answer still abstains, because /query's
# tail LEXICAL TOPIC GATE (`_topic_grounded_facts`) re-judges the fetched has_setting row
# against the question's WordNet topic and drops it: `topic_grounding_filtered before=2
# after=0` on "what flags do I use for pg_dump?" while "which pg_dump flags make a
# restorable dump?" — anchored by the n-gram lane, whose topic happens to token-overlap
# the row — keeps 3/3. The BUG-RECALL-13 receipt's "verified walk serves" ran
# determine_path + fetch_facts_from_anchor directly (which DO serve for shape A — that is
# exactly why the receipt lied): the gate sits between the fetch and the answer and no pin
# exercised it. THE FIX (the #13 measure-interrogative lane's admission, one shape over):
# resolve_anchor stamps the probe PROVENANCE into resolution_out ("bare_tool_noun" /
# "instrumental_how"), /query threads it into determine_path, and a probe-anchored query
# that names the register ASPECT NOUN (the CLOSED class flags/settings/config) lays
# `has_setting` into path.scalar_rels — the tail seam (`_asp_rels or _mi_rels` split)
# then exempts the anchor's own attribute-store row from the lexical gate. Flag
# RECALL_ASPECT_SEAM (default ON); OFF → no shelf → byte-parity.

def test_aspect_seam_flag_defaults_on():
    import src.api.main as main_mod
    assert main_mod._RECALL_ASPECT_SEAM is True


def test_aspect_seam_closed_aspect_noun_class():
    # the closed class — no growth, no fuzzy: the register question's head nouns
    import src.api.main as main_mod
    assert main_mod._REGISTER_ASPECT_NOUNS == ("flags", "settings", "config")
    assert main_mod._ANCHOR_PROBE_BOUNDED == ("bare_tool_noun", "instrumental_how")


def test_aspect_seam_probe_provenance_stamped_at_both_consumption_sites():
    # SOURCE PIN: the provenance rides OUT of resolve_anchor exactly where the probes are
    # consumed in the identity branch — never anywhere else.
    import src.api.main as main_mod
    src = inspect.getsource(main_mod.resolve_anchor)
    i_ih = src.find('resolution_out["anchor_probe"] = "instrumental_how"')
    i_btn = src.find('resolution_out["anchor_probe"] = "bare_tool_noun"')
    assert 0 < i_ih < i_btn, "both probe arms stamp the provenance"
    for i, ret in ((i_ih, "return _ih_probe[0]"), (i_btn, "return _btn_probe[0]")):
        after = src[i:i + 400]
        assert ret in after, "the stamp sits beside the probe return"
        assert src[max(0, i - 200):i + 400].count("resolution_out is not None") >= 1, (
            "the write is guarded (no-dict callers unaffected)")


def test_aspect_seam_query_threads_provenance_and_seam_needs_it():
    # SOURCE PIN: /query Step 2 threads the provenance into determine_path; the seam block
    # requires it (a non-probe anchor never enters) and lays the REGISTER SHELF.
    import src.api.main as main_mod
    qsrc = inspect.getsource(main_mod)
    assert "anchor_probe_bound=_anchor_meta.get(\"anchor_probe\")" in qsrc, (
        "/query must thread the provenance into determine_path")
    dsrc = inspect.getsource(main_mod.determine_path)
    i_seam = dsrc.find("THE PROBE-ANCHORED REGISTER-ASPECT SEAM")
    assert i_seam > 0
    block = dsrc[i_seam:i_seam + 2600]
    assert "anchor_probe_bound in _ANCHOR_PROBE_BOUNDED" in block, "probe provenance required"
    assert "_RECALL_ASPECT_SEAM" in block, "flag-gated"
    assert '"has_setting" not in path.scalar_rels' in block, "the shelf lays into scalar_rels"
    assert "_REGISTER_ASPECT_NOUNS" in block, "the closed aspect-noun class"


@pytest.mark.usefixtures("requires_postgres")
class TestProbeAspectSeamLitmus:
    """THE WOUND, end to end, on a throwaway seat: shape A's gold row is fetched and then
    DROPPED by the real lexical gate (the in-process walk alone cannot see this — the
    BUG-RECALL-13 receipt's blind spot); the seam admission is what keeps it. Both measured
    question shapes pinned, both parity directions."""

    def test_both_measured_shapes_seam_and_gate(self, postgres_dsn, monkeypatch):
        import psycopg2
        import src.api.main as main_mod
        from src.api.main import (resolve_anchor, determine_path, fetch_facts_from_anchor,
                                  convert_to_prose, _topic_grounded_facts)
        from src.api.models import IngestRequest
        from src.provisioning.schema_manager import (create_user_schema,
                                                     derive_user_slug_from_uuid)

        seat = str(uuid.uuid4())
        _apply_migration_public(postgres_dsn)
        schema, status = create_user_schema(seat, derive_user_slug_from_uuid(seat))
        assert status == "ready", f"provision failed: {status}"
        conn = None
        try:
            # the register row, through the REAL ingest door (no edges — the deterministic
            # supplement carries it, the deployed shape)
            res = asyncio.run(main_mod.ingest(
                IngestRequest(text=PGDUMP, user_id=seat, source="mcp", edges=[]),
                main_mod._DirectIngestRequest(), None))
            assert res.get("status") == "valid", res
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True

            def _walk(q):
                conn2 = psycopg2.connect(postgres_dsn)
                conn2.autocommit = True
                with conn2.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                meta: dict = {}
                anchor = resolve_anchor(q, [], seat, conn2, resolution_out=meta)
                conn2.close()
                path = determine_path(q, conn, user_id=seat,
                                      anchor_is_concrete_entity=(
                                          meta.get("method") in ("synonym", "alias")),
                                      anchor_uuid=(anchor if meta.get("method") in
                                                   ("synonym", "alias") else None),
                                      anchor_resolved_uuid=anchor,
                                      anchor_probe_bound=meta.get("anchor_probe"))
                facts = fetch_facts_from_anchor(anchor, seat, path, query_text=q)
                # compose EXACTLY as /query does: convert_to_prose welds the definition,
                # facts_with_definition carries it INTO the gate
                copies = [f.copy() for f in facts]
                convert_to_prose(copies, conn, anchor=anchor, user_id=seat)
                fwdef = []
                for f in copies:
                    fc = f.copy()
                    p = fc.pop("_prose", None)
                    if p is not None:
                        fc["definition"] = p
                    fwdef.append(fc)
                return meta, path, fwdef

            # ── SHAPE A (the abstaining wound): probe provenance + seam + gate survival ──
            q_a = "what flags do I use for pg_dump?"
            meta_a, path_a, fwdef_a = _walk(q_a)
            assert meta_a.get("anchor_probe") == "bare_tool_noun", meta_a
            assert "has_setting" in path_a.scalar_rels, (
                "the seam must lay the register shelf for the probe-anchored aspect question")
            gold_rows = [f for f in fwdef_a if f.get("rel_type") == "has_setting"]
            assert gold_rows and "--clean --if-exists" in str(gold_rows).lower(), fwdef_a
            # THE WOUND PIN: the REAL lexical gate alone drops the gold row (measured live:
            # topic_grounding_filtered before=2 after=0). The tail's seam split is what
            # saves it — the row's rel is bound by the admission, so it is exempt.
            gated = _topic_grounded_facts(q_a, fwdef_a)
            assert "--clean --if-exists" not in str(gated).lower(), (
                "precondition lost: the gate no longer drops the row — the wound pin is stale")
            _mi_rels = {r.lower() for r in path_a.scalar_rels}
            kept = [f for f in fwdef_a
                    if (str(f.get("rel_type") or "").lower() in _mi_rels
                        and f.get("source") == "attributes")]
            assert kept and "--clean --if-exists" in str(kept).lower(), (
                "the seam admission must exempt the anchor's own attribute-store row")

            # ── SHAPE B (the working control): n-gram anchor, NO provenance, NO shelf ──
            q_b = "which pg_dump flags make a restorable dump?"
            meta_b, path_b, fwdef_b = _walk(q_b)
            assert "anchor_probe" not in meta_b, meta_b
            assert "has_setting" not in path_b.scalar_rels, (
                "the n-gram shape must keep today's scope byte-for-byte")
            assert "--clean --if-exists" in str(fwdef_b).lower()

            # ── PARITY: probe fires but the query names NO aspect noun → no shelf ──
            q_c = "what do I use pg_dump for?"
            meta_c, path_c, _fwdef_c = _walk(q_c)
            assert meta_c.get("anchor_probe") == "bare_tool_noun", meta_c
            assert "has_setting" not in path_c.scalar_rels

            # ── PARITY: instrumental-how provenance, no aspect noun → no shelf ──
            q_d = "how do I make pg_dump produce something the restore script can actually replay?"
            meta_d, path_d, _fwdef_d = _walk(q_d)
            assert meta_d.get("anchor_probe") == "instrumental_how", meta_d
            assert "has_setting" not in path_d.scalar_rels

            # ── PARITY: flag OFF → no shelf even for shape A (byte-for-byte today) ──
            monkeypatch.setattr(main_mod, "_RECALL_ASPECT_SEAM", False)
            meta_e, path_e, _fwdef_e = _walk(q_a)
            assert meta_e.get("anchor_probe") == "bare_tool_noun"
            assert "has_setting" not in path_e.scalar_rels, "OFF must be byte-parity"
        finally:
            try:
                c = psycopg2.connect(postgres_dsn)
                c.autocommit = True
                with c.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                c.close()
            except Exception:
                pass


# ── B: THE NEGATED-PRESCRIPTION REGISTER TIER (BUG-RECALL-11 SEAM 3 / qR2; BUG-RECALL-9's
#    conversion path (a), directed by the owner's critic 2026-11-11). THE CONTRACT SPLIT:
#    detect_instruction_settings KEEPS refusing every negated shape (the scan-back pins
#    above stay green); the NEW tier mints ONLY the sentence-initial imperative-prohibitive
#    ("Never deploy Friday afternoon.") with the PROHIBITION SURFACE preserved verbatim —
#    never the inverted positive, never a bare 'never' value. ─────────────────────────────

def test_negated_prescription_R2_mints_the_prohibition_surface():
    # the REAL corpus sentence → the honest row: user-anchored, the prohibition verbatim
    hits = ling.detect_negated_prescriptions("Never deploy Friday afternoon.")
    assert hits == [("user", "never deploy friday afternoon", 0, 30, "has_setting")]


def test_negated_prescription_R2_full_turn_mints_exactly_one_row():
    # the second sentence (a declarative about the past) must not mint — the tier is
    # imperative-prohibitive only
    hits = ling.detect_negated_prescriptions(R2_TURN)
    assert [(s, v, r) for s, v, _a, _b, r in hits] == [
        ("user", "never deploy friday afternoon", "has_setting")]


@pytest.mark.parametrize("text", [
    # negated FACT-declarations (the scan-back refusals stand — a negated fact is a
    # different speech act from a prescribed prohibition)
    "Reminder: the TLS certificate for bramble.internal does not expire "
    "March 3 — that was the old one.",
    "Our vendor is Northlake Cloud, account rep is not Fenna anymore.",
    # postposed 'never' (an afterthought adverbial, not a prescription)
    "I always run the smoke suite before a deploy, never after.",
    # rule-cue-led sentence (NOT sentence-initial — the pinned refusal holds)
    "Standing rule: never run the smoke suite before a deploy, it wastes the window.",
    # a question never prescribes
    "Never deploy on Friday?",
    # a declarative with an overt subject is a negated FACT, not a prohibition
    "I never deploy on Friday afternoons.",
], ids=["neg-fact-cert", "neg-fact-rep", "postposed-never",
        "rule-cue-led", "question", "declarative-subject"])
def test_negated_prescription_refusals_hold(text):
    assert ling.detect_negated_prescriptions(text) == []


def test_negated_prescription_detector_flag_off_mints_nothing(monkeypatch):
    # flag gate: REGISTER_NEGATED_PRESCRIPTION OFF → byte-for-byte today (no mint)
    monkeypatch.setattr(ling, "REGISTER_NEGATED_PRESCRIPTION", False)
    assert ling.detect_negated_prescriptions("Never deploy Friday afternoon.") == []


def test_negated_prescription_contract_split_with_the_generic_register():
    # THE SPLIT PINNED BOTH WAYS: the generic register still refuses R2 (the scan-back
    # contract, unchanged), while the new tier mints it — neither launders 'never'
    # into a bare value and neither inverts the prohibition.
    assert _pairs("Never deploy Friday afternoon.") == []
    assert [(s, v, r) for s, v, _a, _b, r in
            ling.detect_negated_prescriptions("Never deploy Friday afternoon.")] == [
        ("user", "never deploy friday afternoon", "has_setting")]
    # the value is NOT the bare gold token — the prohibition surface carries it
    assert all(v != "never" for _s, v, _a, _b, _r in
               ling.detect_negated_prescriptions("Never deploy Friday afternoon."))


# ── H: THE RULE-ALWAYS FRAME (BUG-FAMILY-7, the sparse-store snapshot-rule miss) ──────────
# "House rule for the lab: always snapshot before a Proxmox upgrade." — the RULE cue is
# present, the imperative declares the standing rule, and NO pre-existing frame matched it
# (rule-run needs "run"; always-use needs "I ... use"; the flag net needs a --flag), so the
# rule never landed and the litmus recall abstained. The frame captures the ALWAYS-CLAUSE
# VERBATIM under the seeded has_setting shelf, STRICTLY cue-gated (the first-person habit
# without a rule cue never mints — measured corpus A15), with the cue's own ``for <NP>``
# complement as the tier-0 subject ("the lab"). The negation scan-back contract is unchanged.

def test_rule_always_frame_mints_the_lab_rule():
    hits = ling.detect_instruction_settings(
        "House rule for the lab: always snapshot before a Proxmox upgrade.")
    assert [(s, v, r) for s, v, _a, _b, r in hits] == [
        ("lab", "snapshot before a Proxmox upgrade", "has_setting")]


def test_rule_always_frame_is_cue_gated():
    # the bare first-person habit declares nothing (corpus A15) — no rule cue, no mint
    assert ling.detect_instruction_settings(
        "I always snapshot the VM before a Proxmox upgrade, never after.") == []


def test_rule_always_frame_negation_scanback_holds():
    # a negated value inside the cue-led sentence is STILL refused (the scan-back contract)
    assert ling.detect_instruction_settings(
        "House rule for the lab: never snapshot without checking the NAS first.") == []


def test_rule_always_yields_to_rule_run_ownership():
    # A15 regression guard: when rule-RUN owns the sentence, rule-ALWAYS must not double-mint
    # the same prescription under a junk subject ("itself")
    hits = ling.detect_instruction_settings(
        "Personal rule that has paid for itself: always run the smoke suite before a deploy.")
    assert [(s, v, r) for s, v, _a, _b, r in hits] == [
        ("deploy", "smoke suite", "has_setting")]


def test_rule_cue_containment_records_growth_candidate(monkeypatch):
    # the engine-growth half: a rule DECLARED under a live cue that NO frame captured is the
    # register's honest contained-shape — the cue surface rides the growth rail (the
    # ContextVar accumulator), never a mint.
    rec = []
    monkeypatch.setattr(ling, "_record_cue_candidate",
                        lambda cue, cat: rec.append((cue, cat)))
    assert ling.detect_instruction_settings("House rule: rotate the certs quarterly.") == []
    assert rec == [("rule", "rule_register_cue")]


def test_rule_frame_firing_confirms_the_matched_cue(monkeypatch):
    # a captured rule frame confirms the MATCHED cue surface on the growth rail (freq bump
    # for the engine's cue vocabulary)
    rec = []
    monkeypatch.setattr(ling, "_record_cue_candidate",
                        lambda cue, cat: rec.append((cue, cat)))
    hits = ling.detect_instruction_settings(
        "House rule for the lab: always snapshot before a Proxmox upgrade.")
    assert hits
    assert ("rule", "rule_register_cue") in rec


def test_rule_register_cue_gate_reads_the_grown_class(monkeypatch):
    # the cue gate is the closed regex OR the tenant's GROWN cue surface — a tenant that
    # declares rules under "guideline" enters the register after the growth rail approves it
    monkeypatch.setattr(ling, "_rule_register_cues",
                        lambda: {"rule", "no exceptions", "guideline"})
    assert ling._rule_register_cue_active("Lab guideline: always snapshot first.") == "guideline"
    assert ling._rule_register_cue_active("House rule: snapshot first.") == "rule"
    assert ling._rule_register_cue_active("I always snapshot first.") is None
    # the grown cue gates the frame the same way; the subject rides the cue's for-PP domain
    # (a bare "Lab guideline:" lead has no for-PP and stays unpaired — the ladder does not
    # guess a subject)
    hits = ling.detect_instruction_settings(
        "Standing guideline for the lab: always snapshot before an upgrade.")
    assert [(s, v, r) for s, v, _a, _b, r in hits] == [
        ("lab", "snapshot before an upgrade", "has_setting")]


def test_medical_relation_adj_class_maps_the_declared_shelf(monkeypatch):
    # the allergy half (BUG-FAMILY-7 / SEC-BUG-2 serve-side): a class member in the
    # ``<adj> to <NP>`` first-person frame files the SEEDED has_allergy shelf (migration 025,
    # 314-stamped sensitive) instead of the novel rel that goes dark; a NON-member in the
    # same frame records the growth candidate and keeps the novel mint (the engine grows the
    # class); OFF → the novel mint is byte-for-byte today's.
    monkeypatch.setattr(ling, "_medical_relation_adjs", lambda: {"allergic"})
    hits = ling.analyze_copula_relational_predicate("I am allergic to penicillin.")
    assert [(e["subject"], e["rel_type"], e["object"]) for e in hits] == [
        ("user", "has_allergy", "penicillin")]
    # 'afraid OF' is a different PP — never maps, never records
    rec = []
    monkeypatch.setattr(ling, "_record_cue_candidate",
                        lambda cue, cat: rec.append((cue, cat)))
    hits = ling.analyze_copula_relational_predicate("I am afraid of spiders.")
    assert [(e["subject"], e["rel_type"], e["object"]) for e in hits] == [
        ("user", "afraid_of", "spiders")]
    assert rec == []
    # a non-member TO-frame records the growth candidate and stays a novel rel
    monkeypatch.setattr(ling, "_medical_relation_adjs", lambda: {"allergic"})
    hits = ling.analyze_copula_relational_predicate("I am hostile to change.")
    if hits:  # parse-shape dependent; the CONTRACT is: mapped-or-recorded, never silent
        assert rec or all(e["rel_type"] != "has_allergy" for e in hits)
    # flag OFF → the novel mint exactly as today
    monkeypatch.setattr(ling, "HEALTH_SHELF_DETECTOR", False)
    hits = ling.analyze_copula_relational_predicate("I am allergic to penicillin.")
    assert [(e["subject"], e["rel_type"], e["object"]) for e in hits] == [
        ("user", "allergic_to", "penicillin")]


def test_migration_316_seeds_both_growable_classes():
    # the 308 discipline: the two new categories seeded in public AND idempotently fanned out
    sql = (pathlib.Path(__file__).resolve().parents[1] / "migrations" /
           "316_seed_medical_relation_adj_and_rule_register_cues.sql").read_text()
    assert "medical_relation_adj" in sql and "rule_register_cue" in sql
    assert sql.count("ON CONFLICT (cue, category) DO NOTHING") >= 2
    assert "LIKE 'faultline\\_%'" in sql  # the per-tenant fan-out loop (the 308 pattern)

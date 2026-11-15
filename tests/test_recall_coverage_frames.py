# -*- coding: utf-8 -*-
"""THE RECALL-COVERAGE FRAMES (BUG-RECALL-11) — the pinned litmus for the three frames
built against the remaining-miss taxonomy
(DEV/BUGS/recall-servicing-20261110/BUG-RECALL-11-remaining-miss-taxonomy.md):

  FRAME 1 (qH1) — THE ARCHIVE-QUERY RECALL. The corpus design requires the SUPERSEDED
  value to stay answerable: "what port did the staging API listen on before the
  segmentation change?" asks for the OLD value (8001) while the walk honestly serves the
  CURRENT one (8010). The archive already exists — the supersede path snapshots the prior
  value into the tenant's entity_attributes_history (SS1; migration 273 shape) — so the
  fix is a RECALL-PATH frame: the MCP recall tool's archive-query band
  (_archive_query_band in src/mcp/server.py). Cue-gated ("before"/"previously"/"used to"/
  "the old"), read-only (writes nothing — no WGM surface exists on the read path),
  flag-gated (RECALL_ARCHIVE_QUERY_BAND, default on), fail-safe "".

  FRAME 2 (qT3) — THE RECURRING-TEMPORAL FRAME. "We agreed on a migration freeze the
  first Monday of every month" states a MONTHLY PATTERN, not a one-shot date; the natural
  question ("when is the monthly migration freeze?") asks for the pattern itself. The
  register's recurring frame (linguistics._SETTING_FRAMES "recurring") mints the schedule
  surface as the VALUE on the SEEDED has_setting shelf (migration 310 — walk-compatible
  attribute-style, never a new rel, never a gate bypass), and the walk-side
  schedule-value admission (fetch_facts_from_anchor, the Q1 clock-time twin) lays the
  attribute into scalar_rels when the query is a schedule interrogative and the anchored
  entity holds a recurring-shaped value. Flag RECALL_FRAME_RECURRING_TEMPORAL, default on.

  FRAME 3 (qT2) — THE CERT-EXPIRY DEPLOY. "Reminder: the TLS certificate for
  bramble.internal expires March 3" mints under the seeded expires_on shelf (migration
  311); the frame + its local pins ALREADY EXIST in tests/test_instruction_register_arm.py
  (CERT in LITMUS_TURNS; the natural-question walk "when does the bramble.internal
  certificate expire?" → "march 3" in LITMUS_WALKS) — Part C below pins that trio STAYS
  pinned there. The deployed-image door-level litmus (remember_facts_tool → expires_on
  row → recall HIT) ran on the pre-prod stack against a throwaway seat; the test box has
  no reachable MCP door, so the door check is operational (see the BUG file), not a pin.

Deterministic only (regex + one bounded SQL read each; no LLM). Every flag OFF → the
pre-frame behaviour byte-for-byte.
"""
import asyncio
import inspect
import os
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction import linguistics as ling

# ── the corpus turns (ctx_corpus.json, byte-identical to the bench evidence) ────────────
C1 = "The staging API listens on port 8001, the dashboard proxies to it directly."
X1 = ("Actually the staging API port changed: it's 8010 now, not 8001 — we moved it "
      "during the network segmentation work.")
T3 = ("We agreed on a migration freeze the first Monday of every month, so provisioning "
      "jobs don't fight the release train.")
T3_BARE = "the migration freeze is the first Monday of every month"
T3_VARIANT_EVERY = "The exporter restarts every Friday."
T3_VARIANT_ORDINAL = "The deployment freeze is the second Tuesday of each month."
T3_VARIANT_CADENCE = "The cleanup jobs run every Friday."
R2 = "Never deploy Friday afternoon."
R2B = "Every Friday deploy in my history has ended with me on the laptop at dinner."

QH1 = "what port did the staging API listen on before the segmentation change?"
QT3 = "when is the monthly migration freeze?"
QT2 = "when does the bramble.internal certificate expire?"


# ══ FRAME 2: the recurring-temporal frame ═══════════════════════════════════════════════

def _hits(text):
    doc = ling._parse(text)
    return [(a, b, r) for a, b, _s, _e, r in ling.detect_instruction_settings(text, doc)]


def test_recurring_frame_mints_the_corpus_schedule():
    # qT3: the schedule surface is the VALUE, the whole subject NP (with its leftward
    # modifier fold) is the SUBJECT — the question names "migration freeze", not "freeze"
    assert _hits(T3) == [("migration freeze", "first Monday of every month",
                          "has_setting")]


def test_recurring_frame_mints_the_task_phrasing():
    # the task's own bare phrasing mints identically (schedule noun + recurring pattern)
    assert _hits(T3_BARE) == [("migration freeze", "first Monday of every month",
                               "has_setting")]


@pytest.mark.parametrize("text,expected", [
    (T3_VARIANT_EVERY, [("exporter", "every Friday", "has_setting")]),
    (T3_VARIANT_ORDINAL, [("deployment freeze", "second Tuesday of each month",
                           "has_setting")]),
    (T3_VARIANT_CADENCE, [("cleanup jobs", "every Friday", "has_setting")]),
], ids=["every-friday", "ordinal-of-each", "cadence-noun"])
def test_recurring_frame_both_value_shapes(text, expected):
    assert _hits(text) == expected


@pytest.mark.parametrize("text", [
    R2,                      # the corpus R2 refusal: noun AFTER the value, no frame value
    R2B,                     # "Every Friday deploy" — value BEFORE the noun (leftward-only)
    "I was born on the first Monday of March.",            # no schedule noun
    "The report is due the first Monday of every month.",  # "report" is no schedule noun
    C1,                      # the corpus config turn never mints a schedule
    X1,                      # the corpus correction never mints a schedule
    "We shipped v2.3 on June 14, and v2.4 followed exactly two weeks later on June 28.",
], ids=["R2-never-deploy", "R2-every-friday-first", "no-noun", "wrong-noun",
        "C1", "X1", "T1-oneshot"])
def test_recurring_frame_refusals(text):
    assert _hits(text) == [], text


def test_recurring_frame_rides_the_seeded_vocabulary():
    # walk-compatibility: the schedule mints on the SEEDED has_setting shelf (migration
    # 310) — never a new rel, never a gate bypass (the WGM gate admits has_setting and
    # the ATTRSCOPE walk reads it back)
    hits = _hits(T3)
    assert hits and all(r == ling.INSTRUCTION_REGISTER_REL for _s, _v, r in hits)
    assert ling.INSTRUCTION_REGISTER_REL == "has_setting"


def test_recurring_frame_deriver_contract():
    facts = ling.derive_sentence_facts(T3, reference=None)
    arm = [f for f in facts if f.rel_type == "has_setting"
           and f.subject == "migration freeze"]
    assert arm, "T3 must mint through the deriver seam"
    for f in arm:
        assert f.object == "first Monday of every month"
        assert f.scalar_datatype == "string"      # → scalar store
        assert f.provenance == "user_stated"
        assert f.event_date is None and f.thin_type is None


def test_recurring_frame_flag_off_mints_nothing(monkeypatch):
    monkeypatch.setattr(ling, "INSTRUCTION_REGISTER_SETTING", False)
    assert not [f for f in ling.derive_sentence_facts(T3, reference=None)
                if f.rel_type == "has_setting"]


def test_walk_schedule_admission_exists_and_is_bound_param():
    # the walk-side serving twin (the Q1 clock-time admission's schedule twin) — present
    # in fetch_facts_from_anchor, flag-gated, and the PG pattern rides as a BOUND
    # PARAMETER (the Q1 REPAIR-3 lesson: a concatenated regex literal aborts the
    # shared connection's transaction)
    import src.api.main as main_mod
    src = inspect.getsource(main_mod.fetch_facts_from_anchor)
    assert "RECALL_FRAME_RECURRING_TEMPORAL" in src
    assert "_RECURRING_VALUE_PG_PATTERN" in src
    assert "schedule_value_admitted" in src
    assert "value_text ~ %s" in src  # bound parameter, never a concatenated literal
    assert hasattr(main_mod, "_RECURRING_VALUE_PG_PATTERN")


def test_walk_schedule_admission_pattern_matches_the_minted_values():
    import re
    import src.api.main as main_mod
    pat = main_mod._RECURRING_VALUE_PG_PATTERN
    for stored in ("first monday of every month",   # the scalar store lowercases
                   "second tuesday of each month",
                   "every friday", "every friday afternoon",
                   "the first monday of every month"):
        assert re.match(pat, stored), stored
    for not_sched in ("8001", "8010", "02:30", "march 3", "512mb",
                      "smoke suite", "--force-recreate", "release/"):
        assert not re.match(pat, not_sched), not_sched


# ══ FRAME 1: the archive-query recall band ══════════════════════════════════════════════

def _server_src():
    import src.mcp.server as server_mod
    return inspect.getsource(server_mod)


def test_archive_band_exists_in_the_recall_tool():
    src = _server_src()
    assert "_archive_query_band" in src
    assert "RECALL_ARCHIVE_QUERY_BAND" in src
    assert "FROM MEMORY'S ARCHIVE" in src          # the clearly-labeled band
    assert "entity_attributes_history" in src       # the superseded rows' archive
    assert "'superseded', 'retired'" in src         # the archived actions only


def test_archive_band_cue_vocabulary():
    import src.mcp.server as server_mod
    for q in (QH1, "who used to own the ingest rewrite?",
              "what was the old port?", "what did you have previously?"):
        assert server_mod._ARCHIVE_QUERY_CUE_RE.search(q), q
    for q in (QT3, QT2, "what port does the staging API listen on?",
              "what do I always run before a deploy? no archive ask — wait"):
        # NOTE: qR1's phrasing carries "before" too — the cue is intentionally broad
        # (it gates a bounded read, not a write); the band only SERVES when history
        # rows exist. QT3/QT2/the current-value ask carry no cue.
        if q.startswith("what do I"):
            assert server_mod._ARCHIVE_QUERY_CUE_RE.search(q)  # broad cue, honest band
        else:
            assert not server_mod._ARCHIVE_QUERY_CUE_RE.search(q), q


def test_archive_band_flag_off_returns_empty(monkeypatch):
    import src.mcp.server as server_mod
    monkeypatch.setenv("RECALL_ARCHIVE_QUERY_BAND", "0")
    assert server_mod._archive_query_band("00000000-0000-0000-0000-000000000000",
                                          QH1) == ""


def test_archive_band_no_cue_skips_the_db(monkeypatch):
    import src.mcp.server as server_mod
    called = {"db": False}

    def _boom(*a, **k):  # the DB read must never be reached without a cue
        called["db"] = True
        raise AssertionError("DB read without a cue")

    monkeypatch.setattr(server_mod, "_archive_query_band_once", _boom)
    assert server_mod._archive_query_band(
        "00000000-0000-0000-0000-000000000000", QT3) == ""
    assert not called["db"]


def test_archive_band_humanizes_attributes():
    import src.mcp.server as server_mod
    assert server_mod._archive_query_human_attr("has_port") == "port"
    assert server_mod._archive_query_human_attr("has_setting") == "setting"
    assert server_mod._archive_query_human_attr("expires_on") == "expires on"


# ── FRAME 1 litmus: the band against a REAL tenant schema on a throwaway seat ──────────

def _apply_migration_public(dsn, filename):
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
class TestArchiveQueryBandRealSchemaLitmus:
    """The band's read path against a real tenant schema: the supersede archive shape
    _record_scalar_supersession writes (migration 273 columns) is what the band must
    surface. The seat schema is dropped in the finally."""

    def test_band_serves_the_archived_value(self, postgres_dsn, monkeypatch):
        import psycopg2
        from src.provisioning.schema_manager import (create_user_schema,
                                                     derive_user_slug_from_uuid)
        import src.mcp.server as server_mod

        seat = str(uuid.uuid4())
        schema, status = create_user_schema(seat, derive_user_slug_from_uuid(seat))
        assert status == "ready", f"provision failed: {status}"
        try:
            eid = str(uuid.uuid4())
            conn = psycopg2.connect(postgres_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                # the anchor entity + its surfaces (the preferred alias is what renders)
                cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'tool')",
                            (eid,))
                cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                            "VALUES (%s, %s, true), (%s, %s, false)",
                            (eid, "staging api", eid, "api"))
                # the CURRENT value row (what the walk serves — qH1's 8010)
                cur.execute("INSERT INTO entity_attributes (user_id, entity_id, "
                            "attribute, value_text, provenance) "
                            "VALUES (%s, %s, 'has_port', '8010', 'user_stated')",
                            (seat, eid))
                # the ARCHIVE row — the exact columns _record_scalar_supersession writes
                # (SS1, migration 273 shape): the prior 8001 snapshot + the new 8010
                cur.execute(
                    "INSERT INTO entity_attributes_history (user_id, entity_id, "
                    "attribute, prior_value_text, prior_provenance, new_value_text, "
                    "new_provenance, action, cause) "
                    "VALUES (%s, %s, 'has_port', '8001', 'user_stated', '8010', "
                    "'user_stated', 'superseded', 'correction')",
                    (seat, eid))
                # a noise entity whose alias contains "port" as a SUBSTRING ("support")
                # — the ranking must prefer the 2-word anchor match
                nid = str(uuid.uuid4())
                cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'tool')",
                            (nid,))
                cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                            "VALUES (%s, %s, true)", (nid, "support portal"))
            conn.close()

            band = server_mod._archive_query_band(seat, QH1)
            assert band, "the archive band must serve on the qH1 question"
            assert "8001" in band, band                       # THE archived value
            assert "8010" in band, band                       # the current value rides too
            assert "staging api" in band, band                # the preferred surface
            assert band.startswith("=== FROM MEMORY'S ARCHIVE"), band
            assert "port was 8001" in band, band              # the humanized attribute

            # no cue → no band (the current-value ask never reads the archive)
            assert server_mod._archive_query_band(
                seat, "what port does the staging API listen on?") == ""

            # flag OFF → no band even on the cue (byte-parity)
            monkeypatch.setenv("RECALL_ARCHIVE_QUERY_BAND", "0")
            assert server_mod._archive_query_band(seat, QH1) == ""
            monkeypatch.setenv("RECALL_ARCHIVE_QUERY_BAND", "1")
            assert "8001" in server_mod._archive_query_band(seat, QH1)
        finally:
            try:
                c = psycopg2.connect(postgres_dsn)
                c.autocommit = True
                with c.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                c.close()
            except Exception:
                pass


# ── FRAME 2 litmus: the REAL /ingest door mints the schedule row and the WALK serves it ─

@pytest.mark.usefixtures("requires_postgres")
class TestRecurringFrameRealIngestLitmus:
    """REAL /ingest (brainless — the deterministic supplement carries the register, the
    deployed rewrite-lane shape) on a throwaway seat: the schedule row lands on the
    seeded has_setting shelf and the natural question reaches it through the walk
    (determine_path + fetch_facts_from_anchor — the recall walk's read path, including
    the schedule-value admission). The seat schema is dropped in the finally."""

    def test_schedule_mints_and_the_walk_serves_it(self, postgres_dsn, monkeypatch):
        import psycopg2
        import src.api.main as main_mod
        from src.api.main import determine_path, fetch_facts_from_anchor
        from src.api.models import IngestRequest
        from src.provisioning.schema_manager import (create_user_schema,
                                                     derive_user_slug_from_uuid)

        seat = str(uuid.uuid4())
        _apply_migration_public(postgres_dsn, "310_seed_has_setting_rel_type.sql")
        schema, status = create_user_schema(seat, derive_user_slug_from_uuid(seat))
        assert status == "ready", f"provision failed: {status}"
        try:
            # THE REAL DOOR: the corpus T3 turn, no edges — the supplement mints
            res = asyncio.run(main_mod.ingest(
                IngestRequest(text=T3, user_id=seat, source="mcp", edges=[]),
                main_mod._DirectIngestRequest(), None))
            assert res.get("status") == "valid", res

            # THE ROW: (migration freeze, has_setting, first monday of every month) live
            conn = psycopg2.connect(postgres_dsn)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                cur.execute(
                    "SELECT ea.value_text, (ea.superseded_at IS NULL) AS live "
                    "FROM entity_attributes ea "
                    "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                    "WHERE al.alias = %s AND ea.attribute = %s",
                    ("migration freeze", "has_setting"))
                rows = cur.fetchall()
            conn.close()
            assert any(r[0] == "first monday of every month" and r[1]
                       for r in rows), rows

            # THE WALK: the natural question reaches the schedule through the REAL read
            # path (the walk admission + the anchored scalar lane)
            conn = psycopg2.connect(postgres_dsn)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s "
                            "LIMIT 1", ("migration freeze",))
                row = cur.fetchone()
            assert row, "anchor 'migration freeze' unresolved"
            path = determine_path(QT3, conn, user_id=seat)
            res = fetch_facts_from_anchor(row[0], seat, path, query_text=QT3)
            blob = str(res).lower()
            assert "first monday" in blob, f"walk missed the schedule: {blob[:400]}"
            conn.close()
        finally:
            try:
                c = psycopg2.connect(postgres_dsn)
                c.autocommit = True
                with c.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                c.close()
            except Exception:
                pass


# ══ FRAME 3: the cert-expiry frame stays pinned where it was built ══════════════════════

def test_cert_expiry_frame_pins_survive_in_the_arm_file():
    # the frame-coverage agent's build (migration 311 + the time-expire frame) is pinned
    # in tests/test_instruction_register_arm.py: CERT in LITMUS_TURNS under the seeded
    # expires_on shelf + the natural-question walk → "march 3". This pin is the guard
    # that the trio STAYS there (the door-level deploy litmus ran on the pre-prod stack;
    # see the BUG file for the operational record).
    import tests.test_instruction_register_arm as arm
    cert_rows = [t for t in arm.LITMUS_TURNS if t[0] == "CERT"]
    assert cert_rows and cert_rows[0][4] == "expires_on"
    assert "march 3" in cert_rows[0][3].lower()          # t[3] = the VALUE (gold)
    assert "bramble.internal tls certificate" == cert_rows[0][2]  # t[2] = the SUBJECT
    walk_rows = [w for w in arm.LITMUS_WALKS if w[0] == "march 3"]
    assert walk_rows and "certificate expire" in walk_rows[0][1]
    assert arm.CERT.startswith("Reminder: the TLS certificate for bramble.internal")


# ══════════════════════════════════════════════════════════════════════════════════
# BUG-RECALL-11 SEAM 1 (qT2) — the ordered-subsequence compound-alias anchor lane.
# The alias as minted is "bramble.internal tls certificate" (verified in entity_aliases
# on the live seats); the question says "bramble.internal certificate" — the alias
# carries an EXTRA token ('tls') the question never says, so the exact lanes miss every
# n-gram and the containment lane (every ALIAS token must be in the query) cannot fire.
# The lane anchors the unique evidence-bearing compound alias whose tokens carry the
# query's phrase IN ORDER. Door-proven 2026-11-11 (in-process /harvest-spans+/ingest
# litmus on a throwaway seat): T2 → expires_on row → QT2 → anchor method=alias →
# 'march 3' served.

def test_seam1_subsequence_lane_source_pins():
    import inspect
    import src.api.main as main_mod
    src = inspect.getsource(main_mod.resolve_anchor)
    assert "_subsequence_alias_unique" in src
    assert "subsequence_alias_override" in src      # shape A: the evidence-free yield
    assert "subsequence_fallback" in src            # shape B: the no-hit fall-through
    # the probe escapes LIKE wildcards so a literal %/_ token cannot widen the pattern
    assert "_like_escape" in src


def test_seam1_subsequence_candidates_gates():
    # the candidate scan rides the SAME discipline as the containment lane: compound
    # aliases only, evidence-bearing only, place-guarded, bounded probes
    import inspect
    import src.api.main as main_mod
    src = inspect.getsource(main_mod)
    helper = inspect.getsource(main_mod.resolve_anchor)
    assert "subsequence" in helper
    # the evidence gate text is shared with the containment lane (one discipline)
    assert "an evidence-free compound never outranks anything" in helper
    assert "RECALL_ANCHOR_SUBSEQUENCE_ALIAS" in inspect.getsource(main_mod)


def test_seam1_subsequence_flag_defaults_on():
    import src.api.main as main_mod
    assert main_mod._RECALL_ANCHOR_SUBSEQUENCE_ALIAS is True


# ══════════════════════════════════════════════════════════════════════════════════
# BUG-RECALL-11 SEAM 2 (qP1) — the possession-transfer frame + the single-owner
# reconcile + the walk recency ranking. Measured on the A17 seat 90206360: the
# correction minted only the weld (kira, hand, "ingest rewrite to mateo") and the seed
# row (kira, owns, ingest rewrite) served as the owner forever. Door-proven 2026-11-11:
# X4 → (mateo, owns, ingest rewrite) minted, the kira edge RETIRED by the reconcile,
# the walk serves Mateo only.

P1_SEED = ("Kira owns the ingest rewrite — she's been driving it since the spring "
           "planning round.")
X4_CORRECTION = ("Small org news: Kira handed the ingest rewrite to Mateo last sprint, "
                 "she's on the billing migration now.")


def test_seam2_transfer_frame_mints_the_recipient_owns_edge():
    import src.extraction.linguistics as ling
    facts = ling.derive_sentence_facts(X4_CORRECTION, reference=None)
    triples = [(f.subject, f.rel_type, f.object) for f in facts]
    # THE FIX: the honest second edge — the recipient as the thing's new owner
    assert ("mateo", "owns", "ingest rewrite") in triples, triples
    # today's welded event capture is unchanged (the frame is additive)
    assert ("kira", "hand", "ingest rewrite to mateo") in triples, triples


def test_seam2_seed_sentence_deriver_output_unchanged():
    import src.extraction.linguistics as ling
    facts = ling.derive_sentence_facts(P1_SEED, reference=None)
    triples = [(f.subject, f.rel_type, f.object) for f in facts]
    assert ("kira", "own", "ingest rewrite") in triples, triples
    assert not any(s == "mateo" for s, _r, _o in triples), triples


def test_seam2_negated_transfer_never_mints():
    # a negated transfer owns nothing — the frame is affirmative-clauses-only
    import src.extraction.linguistics as ling
    facts = ling.derive_sentence_facts(
        "Kira didn't hand the ingest rewrite to Mateo.", reference=None)
    assert not any(f.subject == "mateo" and f.rel_type == "owns" for f in facts), \
        [(f.subject, f.rel_type, f.object) for f in facts]


def test_seam2_non_give_verb_never_mints_the_transfer():
    # the Levin GIVE-class gate: a motion verb with a locative GOAL is NOT a possession
    # transfer — "moved the couch to the garage" never reads as the garage OWNING it
    import src.extraction.linguistics as ling
    facts = ling.derive_sentence_facts(
        "Kira moved the couch to the garage.", reference=None)
    triples = [(f.subject, f.rel_type, f.object) for f in facts]
    assert ("garage", "owns", "couch") not in triples, triples


def test_seam2_store_reconciliation_source_pins():
    import inspect
    import src.api.main as main_mod
    assert main_mod._OWNS_TRANSFER_SUPERSEDE is True
    assert main_mod._OWNS_FAMILY_RELS == ("own", "owns")
    rec_src = inspect.getsource(main_mod._reconcile_single_owner_edges)
    assert "created_at DESC, id DESC" in rec_src      # THE RECENCY RULE (newest wins)
    assert "superseded_at = now()" in rec_src         # the archive-at-write mechanism
    assert "make_interval" in rec_src                 # the same-instant window guard
    assert "count(DISTINCT subject_id) > 1" in rec_src  # single-owner slot detection
    # the post-commit wiring rides the residence-guard pattern (inside _ingest_impl;
    # the endpoint is a thin wrapper)
    impl_src = inspect.getsource(main_mod._ingest_impl)
    assert "_reconcile_single_owner_edges" in impl_src
    assert "_turn_touched_owns" in impl_src


def test_seam2_walk_recency_ranking_source_pins():
    import inspect
    import src.api.main as main_mod
    src = inspect.getsource(main_mod.fetch_facts_from_anchor)
    assert "RECALL_INVERSE_RECENCY_RANK" in src
    assert "ORDER BY created_at DESC, id DESC" in src
    assert main_mod._RECALL_INVERSE_RECENCY_RANK is True


def test_seam2_transfer_frame_flag_defaults_on():
    import src.extraction.linguistics as ling
    assert ling.OWNS_TRANSFER_FRAME is True
    # the verb class is the Levin GIVE class — closed, lexical, no morphology guesses
    assert "hand" in ling._GIVE_CLASS_TRANSFER_VERBS
    assert "give" in ling._GIVE_CLASS_TRANSFER_VERBS
    assert "move" not in ling._GIVE_CLASS_TRANSFER_VERBS


# ══════════════════════════════════════════════════════════════════════════════════
# BUG-RECALL-12 (F-HIGH-1) — the correction lane's deterministic SS1 fallback + the
# walk's current-value recency shadow.

def test_br12_ss1_fallback_source_pins():
    import inspect
    import src.api.main as main_mod
    src = inspect.getsource(main_mod.correct_fact)
    assert "CORRECTION_SS1_FALLBACK" in src
    assert "correction_ss1_fallback" in src            # the history-row cause stamp
    assert "_record_scalar_supersession" in src        # the SS1 contract (migration 273)
    # R5 honesty: a landed retirement is reported as the EFFECT, never "both may show up"
    assert "_ss1_fallback_retired" in src


def test_br12_scalar_recency_shadow_source_pins():
    import inspect
    import src.api.main as main_mod
    src = inspect.getsource(main_mod.fetch_facts_from_anchor)
    assert "RECALL_SCALAR_RECENCY_SHADOW" in src
    assert main_mod._RECALL_SCALAR_RECENCY_SHADOW is True
    # the shadow predicate: a newer live fact on the SAME (subject, attribute-as-rel)
    assert "f.created_at > entity_attributes.updated_at" in src
    assert "f.rel_type = entity_attributes.attribute" in src


# ══════════════════════════════════════════════════════════════════════════════════
# THE REAL-SCHEMA LITMUS (live per-tenant Postgres; skipped without one)

@pytest.mark.usefixtures("requires_postgres")
class TestSeam2OwnsTransferRealSchemaLitmus:
    """The store-side half of SEAM 2 against a REAL tenant schema: the seed edge lands
    LIVE, the transfer turn fires the single-owner reconcile (the prior owner's row is
    archived), the walk serves Mateo only, and the flag OFF keeps both rows live with
    the walk's recency ranking still ordering the newer edge first. The seat schema is
    dropped in the finally."""

    QP1 = "who owns the ingest rewrite these days?"

    def _ingest_edge(self, main_mod, seat, subj, rel, obj):
        from src.api.models import EdgeInput, IngestRequest
        return asyncio.run(main_mod.ingest(
            IngestRequest(text="", user_id=seat, source="mcp",
                          edges=[EdgeInput(subject=subj, object=obj, rel_type=rel,
                                           fact_provenance="user_stated")]),
            main_mod._DirectIngestRequest(), None))

    def _own_rows(self, dsn, schema):
        import psycopg2
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                cur.execute(
                    "SELECT al.alias, (f.superseded_at IS NULL) FROM facts f "
                    "LEFT JOIN entity_aliases al ON al.entity_id = f.subject_id "
                    "WHERE f.rel_type IN ('own','owns') "
                    "AND f.archived_at IS NULL AND f.deleted_at IS NULL ORDER BY f.id")
                return cur.fetchall()
        finally:
            conn.close()

    def test_transfer_retires_the_prior_owner_and_the_walk_serves_mateo(
            self, postgres_dsn, monkeypatch):
        import psycopg2
        from src.provisioning.schema_manager import (create_user_schema,
                                                     derive_user_slug_from_uuid)
        import src.api.main as main_mod
        from src.api.main import resolve_anchor, determine_path, fetch_facts_from_anchor

        seat = str(uuid.uuid4())
        schema, status = create_user_schema(seat, derive_user_slug_from_uuid(seat))
        assert status == "ready", f"provision failed: {status}"
        try:
            # the same-instant window guard protects against THIS TURN's own flap
            # output; a genuine transfer is a LATER statement. The two ingests below
            # run back-to-back (<1s apart), so the window is zeroed for the run —
            # deterministic, and the flag-off sibling keeps the guard semantics pinned.
            monkeypatch.setenv("INGEST_SELF_SUPERSEDE_MIN_AGE_SEC", "0")
            # the seed: (kira, own, ingest rewrite) — the corpus P1 turn's edge
            res = self._ingest_edge(main_mod, seat, "kira", "own", "ingest rewrite")
            assert res.get("status") == "valid", res
            rows = self._own_rows(postgres_dsn, schema)
            assert any(a == "kira" and live for a, live in rows), rows

            # the correction: (mateo, owns, ingest rewrite) — the transfer edge the
            # possession-transfer frame mints (the deriver pins above prove the mint;
            # this litmus feeds it explicitly so the STORE side is brain-independent)
            res = self._ingest_edge(main_mod, seat, "mateo", "owns", "ingest rewrite")
            assert res.get("status") == "valid", res
            rows = self._own_rows(postgres_dsn, schema)
            assert any(a == "mateo" and live for a, live in rows), rows
            # THE SUPERSESSION: the prior owner's edge is retired, not coexisting
            assert not any(a == "kira" and live for a, live in rows), rows

            # THE WALK: the natural question serves Mateo, never the stale owner
            conn = psycopg2.connect(postgres_dsn)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s "
                            "LIMIT 1", ("ingest rewrite",))
                ir_row = cur.fetchone()
            assert ir_row, "anchor 'ingest rewrite' unresolved"
            conn.close()
            conn = psycopg2.connect(postgres_dsn)
            res_out = {}
            anchor = resolve_anchor(self.QP1, [], seat, conn, resolution_out=res_out)
            conn.commit()
            path = determine_path(self.QP1, conn, user_id=seat)
            conn.close()
            facts = fetch_facts_from_anchor(ir_row[0], seat, path, query_text=self.QP1)
            served = " | ".join(f"{f.get('subject')} {f.get('rel_type')}"
                                for f in facts or [])
            assert "owns" in served, served
            assert "kira" not in served.lower(), served   # the stale owner never serves
        finally:
            try:
                c = psycopg2.connect(postgres_dsn)
                c.autocommit = True
                with c.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                c.close()
            except Exception:
                pass

    def test_flag_off_keeps_both_rows_and_the_walk_ranks_recency_first(
            self, postgres_dsn, monkeypatch):
        import psycopg2
        from src.provisioning.schema_manager import (create_user_schema,
                                                     derive_user_slug_from_uuid)
        import src.api.main as main_mod
        from src.api.main import resolve_anchor, determine_path, fetch_facts_from_anchor

        seat = str(uuid.uuid4())
        schema, status = create_user_schema(seat, derive_user_slug_from_uuid(seat))
        assert status == "ready", f"provision failed: {status}"
        try:
            monkeypatch.setattr(main_mod, "_OWNS_TRANSFER_SUPERSEDE", False)
            res = self._ingest_edge(main_mod, seat, "kira", "own", "ingest rewrite")
            assert res.get("status") == "valid", res
            # sleep past the same-instant window so the second edge is a genuine
            # later statement (the reconcile would retire the first if it ran)
            import time as _time
            _time.sleep(2.5)
            res = self._ingest_edge(main_mod, seat, "mateo", "owns", "ingest rewrite")
            assert res.get("status") == "valid", res
            rows = self._own_rows(postgres_dsn, schema)
            # flag OFF → both stay live (byte-parity with today's coexistence)
            assert any(a == "kira" and live for a, live in rows), rows
            assert any(a == "mateo" and live for a, live in rows), rows

            # THE RECENCY RANKING: the walk's inverse fetch orders the NEWER edge
            # first, so the fresh answer leads even in the both-live state
            conn = psycopg2.connect(postgres_dsn)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                cur.execute("SELECT entity_id FROM entity_aliases WHERE alias = %s "
                            "LIMIT 1", ("ingest rewrite",))
                ir_row = cur.fetchone()
                cur.execute(
                    "SELECT al.alias FROM facts f "
                    "LEFT JOIN entity_aliases al ON al.entity_id = f.subject_id "
                    "WHERE f.object_id = %s AND f.rel_type IN ('own','owns') "
                    "AND f.superseded_at IS NULL "
                    "ORDER BY f.created_at DESC, f.id DESC LIMIT 1", (ir_row[0],))
                newest = cur.fetchone()[0]
            conn.close()
            assert newest == "mateo", newest   # the walk's ORDER BY picks the newer
        finally:
            try:
                c = psycopg2.connect(postgres_dsn)
                c.autocommit = True
                with c.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                c.close()
            except Exception:
                pass


@pytest.mark.usefixtures("requires_postgres")
class TestSeam3NegatedPrescriptionRealIngestLitmus:
    """The SEAM 3 serving path against a REAL tenant schema: the R2 turn (the verbatim
    corpus sentence) brainless-ingests → the negated-prescription tier mints
    (user, has_setting, 'never deploy friday afternoon') → the natural question's walk
    serves the rule row. Flag OFF → no mint (byte-parity). Schema dropped in finally."""

    QR2 = "what's my rule about Friday deploys?"
    R2 = ("Never deploy Friday afternoon. Every Friday deploy in my history has ended "
          "with me on the laptop at dinner.")

    def test_rule_row_mints_and_the_walk_serves_it(self, postgres_dsn, monkeypatch):
        import psycopg2
        from src.provisioning.schema_manager import (create_user_schema,
                                                     derive_user_slug_from_uuid)
        import src.api.main as main_mod
        from src.api.main import resolve_anchor, determine_path, fetch_facts_from_anchor
        from src.api.models import IngestRequest

        seat = str(uuid.uuid4())
        schema, status = create_user_schema(seat, derive_user_slug_from_uuid(seat))
        assert status == "ready", f"provision failed: {status}"
        try:
            res = asyncio.run(main_mod.ingest(
                IngestRequest(text=self.R2, user_id=seat, source="mcp", edges=[]),
                main_mod._DirectIngestRequest(), None))
            assert res.get("status") == "valid", res
            assert int(res.get("scalar_committed") or 0) >= 1, res

            conn = psycopg2.connect(postgres_dsn)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
                cur.execute(
                    "SELECT ea.value_text FROM entity_attributes ea "
                    "JOIN entity_aliases al ON al.entity_id = ea.entity_id "
                    "WHERE al.alias = 'user' AND ea.attribute = 'has_setting' "
                    "AND ea.superseded_at IS NULL")
                vals = [r[0] for r in cur.fetchall()]
            conn.close()
            assert any("never deploy friday afternoon" in (v or "").lower()
                       for v in vals), vals

            conn = psycopg2.connect(postgres_dsn)
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public")
            res_out = {}
            anchor = resolve_anchor(self.QR2, [], seat, conn, resolution_out=res_out)
            conn.commit()
            path = determine_path(self.QR2, conn, user_id=seat)
            conn.close()
            facts = fetch_facts_from_anchor(anchor, seat, path, query_text=self.QR2)
            blob = str(facts).lower()
            assert "never deploy friday afternoon" in blob, blob
        finally:
            try:
                c = psycopg2.connect(postgres_dsn)
                c.autocommit = True
                with c.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                c.close()
            except Exception:
                pass

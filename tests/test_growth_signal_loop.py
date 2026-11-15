"""THE ERROR/RECALL ENHANCEMENT LOOP (migration 324, src/growth_signals.py).

The owner's principle under pin: every recall miss and every ingest error is a
GROWTH SIGNAL — "any subject missed is grown into by the spine/engine and not
dropped". These pins hold the three lanes of the loop:

  CAPTURE — one durable growth_signals row per distinct miss fingerprint
  (kind | miss_class | normalized tokens), occurrence-counted, TTL/cap bounded,
  flags OFF = byte-parity (no row, no feed, no log-worthy side effect).

  FEED — the missed shape rides the EXISTING growth seam ONLY: the feed row
  lands in ontology_evaluations in the EXACT shape the LIVE reader
  (re_embedder.classify_unknown_concepts) selects (extraction_method=
  'ingest_miss_pushback', re_embedder_decision NULL, sample_object non-empty),
  and every surface passes the SAME type_term_shape admissibility hard line the
  ingest concept writers run (a path/value never becomes a place).

  RECORD — the wrapper classifier (src.api.main._capture_recall_outcome) pins
  the miss taxonomy: honest_empty / served_vs_asked_gap (the BUG-RECALL-15
  class) / escalated / walk_shed — served→NO capture; walk_shed→capture-only
  (an infra shed says nothing about what the store lacks); and the ingest
  funnel classifier (capture_ingest_error_signal) pins type_refused /
  edge_dropped / nothing_captured / extraction_degraded / chunk_failures.

Migration pins hold the fanout (324 fans the table to every faultline_% schema,
idempotently) and the template↔migration parity (the 197/273/298/299/300/301
rule: template + migration, never one without the other).
"""

import uuid
from pathlib import Path

import psycopg2
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.usefixtures("requires_postgres")

_TEMPLATE = _REPO_ROOT / "src" / "provisioning" / "templates" / "user_schema.sql"
_MIGRATION_324 = _REPO_ROOT / "migrations" / "324_growth_signals.sql"


# ── DDL slices (template shape) ───────────────────────────────────────────────

def _signals_ddl(schema_name: str) -> str:
    """The growth_signals block of the provisioning template, {schema_name}
    substituted — the block runs to end-of-file (it is the template's last)."""
    text = _TEMPLATE.read_text()
    start = text.index("CREATE TABLE IF NOT EXISTS {schema_name}.growth_signals")
    return text[start:].replace("{schema_name}", schema_name)


def _oe_ddl(schema_name: str) -> str:
    """The ontology_evaluations block (the feed's target table — REAL shape)."""
    text = _TEMPLATE.read_text()
    start = text.index("CREATE TABLE IF NOT EXISTS ontology_evaluations")
    end = text.index("WHERE re_embedder_decision IS NULL;", start)
    return text[start:end + len("WHERE re_embedder_decision IS NULL;")].replace(
        "{schema_name}", schema_name)


@pytest.fixture()
def tenant(requires_postgres, monkeypatch):
    """A tenant uid + schema carrying growth_signals + ontology_evaluations
    (both at their REAL template shapes), with the capture throttle reset."""
    import src.api.main as M
    import src.growth_signals as GS
    uid = str(uuid.uuid4())
    schema = M._derive_tenant_schema(uid)
    assert schema, "uuid user must derive a tenant schema"
    GS._last_capture.clear()
    conn = psycopg2.connect(requires_postgres)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(_oe_ddl(schema))
            cur.execute(_signals_ddl(schema))
        conn.commit()
        yield {"uid": uid, "schema": schema, "dsn": requires_postgres}
    finally:
        GS._last_capture.clear()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()


def _signals_rows(dsn, schema):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "SELECT signal_kind, miss_class, query_tokens, served, expected,"
                " growth_state, growth_target, occurrence_count, context"
                " FROM growth_signals ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def _fed_candidates(dsn, schema):
    """The rows the LIVE classify_unknown_concepts reader selects — the EXACT
    WHERE clause of embedder.classify_unknown_concepts (embedder.py)."""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "SELECT sample_object, first_text_snippet FROM ontology_evaluations"
                " WHERE extraction_method = 'ingest_miss_pushback'"
                "   AND re_embedder_decision IS NULL"
                "   AND sample_object IS NOT NULL AND sample_object <> ''"
                " ORDER BY last_seen_at DESC")
            return cur.fetchall()
    finally:
        conn.close()


# ── pure: the missed-shape tokens / fingerprint ───────────────────────────────

def test_normalize_tokens_prunes_scaffold_and_stopwords():
    from src.growth_signals import normalize_tokens
    toks = normalize_tokens("What are my medical conditions?")
    assert toks == ["medical", "conditions"], toks
    assert normalize_tokens("what is the name of my hamster") == ["hamster"]
    assert normalize_tokens("") == []
    assert len(normalize_tokens(" ".join(f"w{i}" for i in range(40)))) == 12


def test_fingerprint_is_shape_keyed_and_stable():
    from src.growth_signals import fingerprint, normalize_tokens
    a = fingerprint("recall_miss", "honest_empty", normalize_tokens("what are my medical conditions?"))
    b = fingerprint("recall_miss", "honest_empty", normalize_tokens("What ARE my medical conditions?"))
    c = fingerprint("recall_miss", "escalated", normalize_tokens("what are my medical conditions?"))
    assert a == b, "paraphrase-equivalent shapes dedup onto ONE row"
    assert a != c, "a different miss class is a different signal"


def test_noun_candidates_fail_safe_and_gated():
    from src.growth_signals import noun_candidates
    assert noun_candidates("") == []
    assert noun_candidates("   ") == []
    # garbage in, empty out — never a raise
    assert noun_candidates("   !!! ???") == []


def test_admissible_uses_the_hard_line():
    """The feed proposes, type_term_shape disposes: a path-shaped surface is a
    VALUE and never becomes a place (the production-measured junk class)."""
    from src.growth_signals import _admissible
    assert _admissible("daughter") is True
    assert _admissible("medical condition") is True
    assert _admissible("~/.config/opencode.json") is False


# ── CAPTURE: the durable record ───────────────────────────────────────────────

def test_capture_writes_the_record_and_returns_state(tenant):
    from src.growth_signals import capture_signal
    out = capture_signal(
        tenant["schema"], kind="recall_miss", miss_class="honest_empty",
        query_text="what are my medical conditions?",
        served="empty answer (abstention)",
        expected="medical facts matching the asked subject",
        context={"anchor": "default"},
        feed_concepts=["medical condition"])
    assert out["fingerprint"], out
    assert out["growth_state"] == "fed"
    assert out["fed"] == ["medical condition"]
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert len(rows) == 1
    kind, miss_class, toks, served, expected, state, target, occ, ctx = rows[0]
    assert (kind, miss_class) == ("recall_miss", "honest_empty")
    assert toks == "medical conditions"
    assert "abstention" in served and "medical" in expected
    assert state == "fed" and "ontology_evaluations" in (target or "")
    assert occ == 1


def test_capture_bumps_occurrence_on_reoccurrence(tenant):
    from src.growth_signals import capture_signal
    import src.growth_signals as GS
    capture_signal(tenant["schema"], kind="recall_miss",
                   miss_class="honest_empty",
                   query_text="what are my medical conditions?",
                   served="empty answer (abstention)",
                   expected="facts", feed_concepts=None)
    GS._last_capture.clear()  # past the throttle: a re-occurrence inside the window
    capture_signal(tenant["schema"], kind="recall_miss", miss_class="honest_empty",
                   query_text="What ARE my medical conditions?",
                   served="empty answer (abstention)", expected="facts",
                   feed_concepts=None)
    GS._last_capture.clear()
    capture_signal(tenant["schema"], kind="recall_miss", miss_class="honest_empty",
                   query_text="what are my medical conditions??",
                   served="empty answer (abstention)", expected="facts",
                   feed_concepts=None)
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert len(rows) == 1, "paraphrase-equivalent re-asks bump ONE row"
    assert rows[0][7] == 3, rows[0]


def test_capture_feed_lands_in_the_live_reader_shape(tenant):
    """THE GROWTH-FEED CONTRACT: the missed subject must be selectable by the
    REAL classify_unknown_concepts WHERE clause — a candidate row no reader can
    see is a dark write (the pending_types lesson)."""
    from src.growth_signals import capture_signal
    capture_signal(tenant["schema"], kind="recall_miss", miss_class="honest_empty",
                   query_text="who is my daughter?",
                   served="empty answer (abstention)", expected="child rows",
                   feed_concepts=["daughter"])
    fed = _fed_candidates(tenant["dsn"], tenant["schema"])
    assert ("daughter", "who is my daughter?") in [
        (obj, snip) for (obj, snip) in fed], fed


def test_capture_junk_surface_refuses_the_feed_but_records(tenant):
    from src.growth_signals import capture_signal
    out = capture_signal(tenant["schema"], kind="ingest_error",
                         miss_class="edge_dropped",
                         query_text="config file at ~/.config/opencode/opencode.json",
                         served="edge dropped", expected="classified",
                         feed_concepts=["~/.config/opencode/opencode.json"])
    assert out["growth_state"] == "capture_only", out
    assert _fed_candidates(tenant["dsn"], tenant["schema"]) == [], \
        "a path-shaped surface never becomes a place"


def test_capture_feed_flag_off_is_capture_only(tenant, monkeypatch):
    from src.growth_signals import capture_signal
    monkeypatch.setenv("GROWTH_SIGNAL_FEED", "false")
    out = capture_signal(tenant["schema"], kind="recall_miss",
                         miss_class="honest_empty",
                         query_text="what are my medical conditions?",
                         served="empty answer (abstention)", expected="facts",
                         feed_concepts=["medical condition"])
    assert out["growth_state"] == "capture_only" and out["fed"] == []
    assert _fed_candidates(tenant["dsn"], tenant["schema"]) == []
    assert len(_signals_rows(tenant["dsn"], tenant["schema"])) == 1, \
        "the FEED flag gates only the feed — the record still lands"


def test_capture_flag_off_is_byte_parity(tenant, monkeypatch):
    """OFF = today's behavior: not one row, not one feed row, empty verdict."""
    from src.growth_signals import capture_signal
    monkeypatch.setenv("GROWTH_SIGNAL_CAPTURE", "false")
    out = capture_signal(tenant["schema"], kind="recall_miss",
                         miss_class="honest_empty",
                         query_text="what are my medical conditions?",
                         served="empty answer (abstention)", expected="facts",
                         feed_concepts=["medical condition"])
    assert out == {}
    assert _signals_rows(tenant["dsn"], tenant["schema"]) == []
    assert _fed_candidates(tenant["dsn"], tenant["schema"]) == []


def test_capture_never_raises_and_never_falls_through_to_public(tenant, monkeypatch):
    from src.growth_signals import capture_signal
    assert capture_signal(None, kind="recall_miss", miss_class="honest_empty",
                          query_text="x", served="", expected="",
                          feed_concepts=["daughter"]) == {}
    assert capture_signal("public", kind="recall_miss", miss_class="honest_empty",
                          query_text="x", served="", expected="",
                          feed_concepts=["daughter"]) == {}
    monkeypatch.setenv("POSTGRES_DSN", "")
    assert capture_signal(tenant["schema"], kind="recall_miss",
                          miss_class="honest_empty", query_text="x",
                          served="", expected="",
                          feed_concepts=["daughter"]) == {}
    assert _signals_rows(tenant["dsn"], tenant["schema"]) == []


def test_capture_ttls_and_caps(tenant, monkeypatch):
    from src.growth_signals import capture_signal  # noqa: F401 — env read live
    monkeypatch.setenv("GROWTH_SIGNAL_TTL_DAYS", "30")
    monkeypatch.setenv("GROWTH_SIGNALS_MAX", "3")
    conn = psycopg2.connect(tenant["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {tenant['schema']}")
            for i in range(4):
                cur.execute(
                    "INSERT INTO growth_signals (signal_kind, miss_class,"
                    " fingerprint, last_seen_at) VALUES ('recall_miss', 'old', %s,"
                    " now() - interval '40 days')", (f"old{i}",))
        conn.commit()
    finally:
        conn.close()
    GS = __import__("src.growth_signals", fromlist=["capture_signal"])
    GS._last_capture.clear()
    capture_signal(tenant["schema"], kind="recall_miss", miss_class="honest_empty",
                   query_text="fresh question about hamsters", served="empty",
                   expected="facts", feed_concepts=None)
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    classes = {r[1] for r in rows}
    assert "old" not in classes, "TTL pruned the stale rows"
    assert len(rows) <= 3, "the row cap holds"


# ── RECORD: the wrapper classifier (the /query door's miss taxonomy) ──────────

class _StubResp:
    """The QueryResponse surface the classifier reads (duck-typed — the real
    QueryResponse field set is pinned by the /query door tests)."""

    def __init__(self, facts=None, walk_facts_found=None, alerts=None,
                 error=None, anchor="", staged_facts_count=0):
        self.facts = facts or []
        self.walk_facts_found = walk_facts_found
        self.alerts = alerts or []
        self.error = error
        self.anchor = anchor
        self.staged_facts_count = staged_facts_count


def test_wrapper_honest_empty_captures_and_feeds(tenant):
    import src.api.main as M
    M._capture_recall_outcome(tenant["uid"], "who is my daughter?",
                              _StubResp(), [], None)
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert rows and rows[0][1] == "honest_empty" and rows[0][5] == "fed"
    assert ("daughter", "who is my daughter?") in [
        (obj, snip) for (obj, snip) in _fed_candidates(tenant["dsn"], tenant["schema"])]


def test_wrapper_served_captures_nothing(tenant):
    import src.api.main as M
    M._capture_recall_outcome(tenant["uid"], "who is my daughter?",
                              _StubResp(facts=[{"rel": "has_child"}]), [], None)
    assert _signals_rows(tenant["dsn"], tenant["schema"]) == []


def test_wrapper_served_vs_asked_gap_pins_bug_recall_15(tenant):
    """The BUG-RECALL-15 class: the walk RESOLVED 13 facts, the presentation
    gates rendered none — the served-vs-asked mismatch, recorded."""
    import src.api.main as M
    M._capture_recall_outcome(tenant["uid"], "what flags do I use for pg_dump?",
                              _StubResp(walk_facts_found=13), [], None)
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert rows and rows[0][1] == "served_vs_asked_gap"
    assert "13" in rows[0][3], rows[0]  # served names the dropped count


def test_wrapper_escalated_captures_the_graph_miss(tenant):
    import src.api.main as M
    M._capture_recall_outcome(tenant["uid"], "what is my warranty period?",
                              _StubResp(),
                              [{"document_id": 7, "chunk_index": 3}], None)
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert rows and rows[0][1] == "escalated"


def test_wrapper_walk_shed_captures_without_feeding(tenant):
    """An infra shed says nothing about what the store lacks — record-only."""
    import src.api.main as M
    M._capture_recall_outcome(tenant["uid"], "who is my daughter?",
                              _StubResp(), [], "walk_timeout")
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert rows and rows[0][1] == "walk_shed"
    assert rows[0][5] == "capture_only"
    assert _fed_candidates(tenant["dsn"], tenant["schema"]) == []


# ── RECORD: the ingest funnel classifier ──────────────────────────────────────

def test_ingest_type_refused_feeds_the_refused_type(tenant):
    """THE FAMILY-WOUND PIN: 'daughter' refused at store → the refused TYPE noun
    becomes a growth candidate (the missing admission, not a dropped memory)."""
    import src.api.main as M
    M.capture_ingest_error_signal(
        tenant["schema"],
        "My daughter's name is Wren and she is seven years old.",
        "type_refused",
        {"subject": "user", "rel_type": "has_child", "object": "wren",
         "entity": "daughter", "entity_type": "daughter",
         "allowed": ["Person", "Animal"], "reason": "type not admitted"})
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert rows and rows[0][1] == "type_refused" and rows[0][5] == "fed"
    fed = [obj for (obj, _snip) in _fed_candidates(tenant["dsn"], tenant["schema"])]
    assert "daughter" in fed, fed


def test_ingest_nothing_captured_is_record_only(tenant):
    import src.api.main as M
    M.capture_ingest_error_signal(tenant["schema"], "the server room is at 4C",
                                  "nothing_captured", {})
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert rows and rows[0][1] == "nothing_captured" and rows[0][5] == "capture_only"
    assert _fed_candidates(tenant["dsn"], tenant["schema"]) == []


def test_ingest_degraded_records_without_feeding(tenant):
    import src.api.main as M
    M.capture_ingest_error_signal(
        tenant["schema"], "remember that my hypervisor runs proxmox",
        "extraction_degraded",
        {"edges_withheld": 4, "llm_calls_unanswered": 2,
         "first_reason": "REFRAME:HTTPStatusError"})
    rows = _signals_rows(tenant["dsn"], tenant["schema"])
    assert rows and rows[0][1] == "extraction_degraded"
    assert "withheld" in rows[0][3]
    assert _fed_candidates(tenant["dsn"], tenant["schema"]) == []


# ── the live reader contract (source pin) ─────────────────────────────────────

def test_feed_target_reader_still_selects_the_seam():
    """THE DARK-WRITE TRIPWIRE: the feed's reader (classify_unknown_concepts)
    must still select extraction_method='ingest_miss_pushback'. If this goes
    red the reader moved — re-point the feed, never leave it dark."""
    src = (Path(_REPO_ROOT) / "src" / "re_embedder" / "embedder.py").read_text()
    assert "extraction_method = 'ingest_miss_pushback'" in src
    m = src.index("def classify_unknown_concepts")
    seg = src[m:src.index("ORDER BY last_seen_at DESC", m)]
    assert "re_embedder_decision IS NULL" in seg
    assert "sample_object IS NOT NULL" in seg


# ── the migration: fanout + template parity ───────────────────────────────────

def test_migration_324_fans_and_is_idempotent(tenant):
    """324 fans growth_signals into every faultline_% schema (the tenant fixture
    carries NO growth_signals DDL pre-applied here — drop it first), twice."""
    import src.api.main as M
    schema = tenant["schema"]
    conn = psycopg2.connect(tenant["dsn"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {schema}.growth_signals CASCADE")
            cur.execute(f"DROP TABLE IF EXISTS {schema}.ontology_evaluations CASCADE")
        conn.commit()
        sql = _MIGRATION_324.read_text()
        for _ in range(2):  # idempotent: safe to re-run
            with conn.cursor() as cur:
                cur.execute(sql)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = 'growth_signals'"
                " ORDER BY ordinal_position", (schema,))
            cols = [r[0] for r in cur.fetchall()]
        assert set(cols) >= {
            "id", "signal_kind", "miss_class", "fingerprint", "query_text",
            "query_tokens", "expected", "served", "context", "growth_state",
            "growth_target", "occurrence_count", "first_seen_at", "last_seen_at"}
    finally:
        # restore the fixture's own DDL for the teardown (it drops by CASCADE)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(_oe_ddl(schema))
            cur.execute(_signals_ddl(schema))
        conn.commit()
        conn.close()
    # and the fanned schema captures end-to-end (the boot lane works)
    import src.api.main as M2
    M2._capture_recall_outcome(tenant["uid"], "who is my daughter?",
                               _StubResp(), [], None)
    assert _signals_rows(tenant["dsn"], tenant["schema"])


def test_template_and_migration_shapes_match(tenant):
    """The 197/273/298/299/300/301 rule: the template block and the migration
    fan produce the SAME columns (a fresh mint == a fanned legacy schema)."""
    import re
    tmpl = _signals_ddl("faultline_x")
    mig = _MIGRATION_324.read_text()
    tcols = set(re.findall(r"^\s+(\w+)\s+(?:TEXT|BIGSERIAL|JSONB|INTEGER|TIMESTAMPTZ)",
                           tmpl, re.M))
    mcols = set(re.findall(r"'    (\w+)\s+(?:TEXT|BIGSERIAL|JSONB|INTEGER|TIMESTAMPTZ)", mig))
    assert tcols == mcols, (tcols ^ mcols)


# ── RECORD: the operator's dashboard aggregation ─────────────────────────────

def test_report_aggregates_and_soft_fails(tenant):
    import src.api.main as M
    from src.growth_signals import report
    M._capture_recall_outcome(tenant["uid"], "who is my daughter?",
                              _StubResp(), [], None)
    M.capture_ingest_error_signal(tenant["schema"], "the server room is at 4C",
                                  "nothing_captured", {})
    rep = report(tenant["schema"], limit=10)
    assert rep["counts_by_kind"].get("recall_miss", {}).get("signals", 0) >= 1
    assert rep["counts_by_kind"].get("ingest_error", {}).get("signals", 0) >= 1
    assert "honest_empty" in rep["counts_by_miss_class"]
    assert "capture_only" in rep["counts_by_growth_state"]
    assert len(rep["recent"]) >= 2
    assert rep["recent"][0]["miss_class"]
    # the endpoint contract: per-tenant derived schema, soft-fail empty
    assert report(None) == {"counts_by_kind": {}, "counts_by_miss_class": {},
                            "counts_by_growth_state": {}, "recent": []}
    assert report("public")["recent"] == []

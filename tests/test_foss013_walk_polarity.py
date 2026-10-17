"""FOSS-013 pin: the NARROW walk lane (inverse facts) serves polarity, so recall
renders a negated state NEGATED.

The round-3 D-es critic measured the wound live (seat 8888): the store held exactly ONE
Class-A row (usar, polarity="negated", user_stated) yet recall of "Uso el puerto 9004?"
rendered "You use Puerto". Capture is correct end to end (the deriver emits negated=True;
ingest threads _f.negated onto edge["polarity"] and the row carries it); the READ side
was the gap: the row (user, usar, puerto) is only reachable at anchor=puerto through the
INVERSE fetch of fetch_facts_from_anchor, and a spine-grown rel carries no inverse rel
metadata — so the row came back through the fallback inverse dict whose SELECT named no
polarity column and whose served fact dict had NO polarity field. The prose renderer
(_negate_prose, keyed on fact.get("polarity")) then treated every inverse row as
affirmative. The legacy direct lane (temporal_status alongside polarity) rendered the
same row negated.

Fix under pin: the inverse SELECTs now select temporal_status + polarity and all three
inverse dicts (full-invert / symmetric / fallback) project them — the same field names
the legacy lane serves. The pins drive the REAL walk lane through a dispatching fake
cursor (no DB in this env; the critic live-verifies the stack) and the REAL renderer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.api.main as main
from src.api.models import QueryPath
from src.extraction import linguistics as ling_mod
from src.extraction.linguistics import (
    analyze_negation_scopes,
    derive_sentence_facts,
    linguistics_available,
)

_PORT = "11111111-1111-1111-1111-111111111111"
_USER = "22222222-2222-2222-2222-222222222222"


# ── 1. THE WOUND AT THE EXACT SEAM: the inverse walk serves polarity ────────────


class _DispatchCursor:
    """Answers fetch_facts_from_anchor's queries: the INVERSE SELECT returns the wound
    row (user, usar, puerto, polarity="negated"); every other read is an empty relation.
    The row shape follows the SELECT the query actually asked for (the pre-fix SELECT
    names 10 columns, the fixed one 12) — so the red-check fails on the MISSING FIELD,
    not on a tuple-shape error."""

    def __init__(self, log):
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(" ".join(sql.split()))

    @staticmethod
    def _row_for(sql):
        # The inverse fetch: object-side walk over facts.
        if ("FROM facts" in sql and "WHERE object_id = %s" in sql
                and "superseded_at IS NOT NULL" not in sql):
            base = [_USER, _PORT, "usar", 0.9, "A", None, None,
                    "user_stated", None, None]
            if "polarity" in sql:
                base += ["now", "negated"]
            return [tuple(base)]
        return []

    def fetchall(self):
        sql = self._log[-1] if self._log else ""
        return self._row_for(sql)

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _DispatchConn:
    def __init__(self):
        self.queries = []

    def cursor(self):
        return _DispatchCursor(self.queries)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def _walk_env(monkeypatch):
    """Transport stubs only: schema-context lookup short-circuits, connect returns the
dispatching fake. The WALK LANE CODE runs for real."""
    # The lane returns early (no_dsn) when the env var is absent — point it at the
    # monkeypatched connect (the DSN is never dialed for real).
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://fake@localhost/fake")
    monkeypatch.setattr(main, "get_user_schema_context",
                        lambda user_id, conn: {"schema_name": "faultline_test"})
    conn = _DispatchConn()
    monkeypatch.setattr(main.psycopg2, "connect", lambda dsn=None, **kw: conn)
    return conn


def _query_path():
    return QueryPath(fetch_all_details=True, relationship_rels=["usar"],
                     scalar_rels=[], scope_active=False)


def test_inverse_walk_lane_serves_polarity(_walk_env):
    """THE FOSS-013 seam pin: recalling the port walks the stored (user, usar, puerto,
    negated) row through the INVERSE fetch — the served fact dict must carry
    polarity="negated" (the legacy lane's field name) plus the row's temporal layer.
    Red pre-fix: the dict had NO polarity key and the renderer read it affirmative."""
    facts = main.fetch_facts_from_anchor(
        _PORT, _USER, _query_path(), temporal_intent=None,
        query_text="Uso el puerto 9004?", comparison_entity_ids=None, reach_uuids=None)
    wound = [f for f in facts if f.get("rel_type") == "usar"]
    assert wound, "the inverse lane never served the row: " + repr(facts)
    f = wound[0]
    assert f["polarity"] == "negated", (
        "the narrow walk lane dropped polarity — recall renders affirmative from a "
        "negated row: " + repr(f))
    assert f["temporal_status"] == "now", "temporal layer missing: " + repr(f)


def test_inverse_walk_lane_negated_row_renders_negated(_walk_env):
    """END-TO-END at the served shape (the es battery shape, seat 8888): the row the
    inverse lane serves must render NEGATED — never the affirmative assertion."""
    facts = main.fetch_facts_from_anchor(
        _PORT, _USER, _query_path(), temporal_intent=None,
        query_text="Uso el puerto 9004?", comparison_entity_ids=None, reach_uuids=None)
    prose = main.convert_to_prose(facts, None, anchor=_PORT, user_id=_USER)
    joined = " ".join(prose).lower()
    assert "not" in joined, "the negated row rendered with no negation: " + repr(prose)
    assert "it is not the case that you usar" in joined, "prose: " + repr(prose)


# ── 2. RENDERER PINS: narrow vs legacy shapes, negated + affirmative control ────


def _served_fact(polarity=None):
    f = {"subject": _USER, "object": "Puerto 9004", "rel_type": "usar",
         "confidence": 0.9, "fact_class": "A", "source": "db",
         "fact_provenance": "user_stated", "event_date": None,
         "event_date_granularity": None,
         "category": main._get_rel_type_category("usar")}
    if polarity is not None:
        f["polarity"] = polarity
    return f


def test_narrow_shape_negated_renders_negated():
    """(i) the es battery shape, render half: the served narrow dict (post-fix field set)
    renders the negated state — the response must NOT assert affirmative use."""
    prose = main.convert_to_prose(
        [_served_fact("negated")], None, anchor="Puerto 9004", user_id=_USER)
    joined = " ".join(prose).lower()
    assert "not" in joined, "affirmative render from a negated row: " + repr(prose)
    assert "it is not the case that you usar puerto 9004" in joined, "prose: " + repr(prose)


def test_legacy_shape_with_temporal_status_still_renders_negated():
    """(iii) legacy-shape regression: the direct-lane dict (temporal_status alongside
    polarity) keeps its negated render byte-for-byte."""
    legacy = _served_fact("negated")
    legacy["temporal_status"] = "now"
    prose = main.convert_to_prose(
        [legacy], None, anchor="Puerto 9004", user_id=_USER)
    assert "it is not the case that you usar puerto 9004" in " ".join(prose).lower(), (
        "legacy render changed: " + repr(prose))


def test_affirmative_rows_still_render_affirmative():
    """(iv) control: an affirmed row (and a polarity-less dict — the documented
    fail-safe) render affirmative; the negation transform never over-fires."""
    for f in (_served_fact("affirmed"), _served_fact(None)):
        prose = main.convert_to_prose(
            [f], None, anchor="Puerto 9004", user_id=_USER)
        joined = " ".join(prose).lower()
        assert "not" not in joined, "negation leaked into an affirmative row: " + repr(prose)
        assert "you usar puerto 9004" in joined, "prose: " + repr(prose)


# ── 3. EN REAL-PARSE TWIN (item ii): the adverbial-never delta, measured ────────

_spaCy = pytest.mark.skipif(
    not linguistics_available(),
    reason="SPACY_MODEL unset — the capture-side pins need a real parse",
)


@_spaCy
def test_en_never_sentence_captures_negated_and_scopes_sentential():
    """(ii) en real-parse twin, MEASURED (en_core_web_sm): "I never use port 9004 on any
    server." derives (user, use, port) with negated=True (user_stated) — the capture
    polarity the ingest row carries — and analyze_negation_scopes yields ONE SENTENTIAL
    scope with cue "never" (the en adverbial-never delta the round-3 critic disclosed:
    the UD negator arm fires on en NEVER). MEASUREMENT VERDICT: the un-gated lemma set
    behaves CORRECTLY here — "never" is a genuine sentential negator, the scope is
    verbal (no nominal complement, no two poles), so a destructive GLiNER2 lean on this
    sentence is vetoed to STATEMENT by the same grammar the es sentence binds, and the
    capture is negated. Pinned as the contract; if this pin goes red the lemma set grew
    a real misbehavior and must be re-measured, not silently absorbed."""
    facts = derive_sentence_facts(
        "I never use port 9004 on any server.", None, None) or []
    use = [f for f in facts if f.rel_type == "use"]
    assert use, "capture lost the sentence: " + repr(facts)
    f = use[0]
    assert f.subject == "user" and f.object == "port", repr(f)
    assert f.negated is True, "the en-never capture lost polarity: " + repr(f)
    assert f.provenance == "user_stated", repr(f)
    scopes = analyze_negation_scopes("I never use port 9004 on any server.")
    assert scopes, "the en adverbial-never yields no scope"
    s = scopes[0]
    assert (s.cue_text or "").lower() == "never", repr(s)
    assert s.predicates_nominal is False and s.two_referential_poles is False, repr(s)


@_spaCy
def test_en_never_negated_capture_renders_negated_at_recall():
    """(ii) render half: the row the en-never capture commits (polarity="negated" via the
    ingest _f.negated threading) renders NEGATED at recall — the served narrow dict from
    the inverse lane carries it and the prose negates."""
    facts = derive_sentence_facts(
        "I never use port 9004 on any server.", None, None) or []
    use = [f for f in facts if f.rel_type == "use"][0]
    served = _served_fact("negated" if use.negated else "affirmed")
    served["rel_type"] = "use"
    served["object"] = "port"
    prose = main.convert_to_prose(
        [served], None, anchor="port", user_id=_USER)
    joined = " ".join(prose).lower()
    if use.negated:
        assert "not" in joined, "negated capture rendered affirmative: " + repr(prose)
    else:
        assert "not" not in joined, "affirmed capture rendered negated: " + repr(prose)


# ── 4. SOURCE CONTRACT: the inverse SELECTs name the columns; the dicts project ─


def test_inverse_selects_name_temporal_and_polarity_columns():
    """Static contract (always runs): both inverse SELECTs select temporal_status +
    polarity, and all three inverse served dicts project them — the legacy lane's field
    names. A revert of the projection fails this pin even without a DB."""
    src = (_ROOT / "src" / "api" / "main.py").read_text(encoding="utf-8")
    fn = src[src.index("def fetch_facts_from_anchor"):]
    fn = fn[:fn.index("\ndef ", 10)]
    inv = fn[fn.index("inverse_query = "):]
    cut = inv.find("REACH COLLECTOR")
    if cut != -1:
        inv = inv[:cut]
    assert inv.count("event_date_granularity, temporal_status, polarity") == 2, (
        "the inverse SELECTs no longer name the temporal/polarity columns")
    assert inv.count('"polarity": _inv_pol or') == 3, (
        "the three inverse served dicts must project polarity")
    assert inv.count('"temporal_status": _inv_ts or') == 3, (
        "the three inverse served dicts must project the temporal layer")

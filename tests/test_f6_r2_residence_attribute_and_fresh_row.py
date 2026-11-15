"""FOG-FAMILY R2 — the two critic-R1 residence residuals (Lane C territory).

F4 (attribute lane): a plain address plant was refused by the user-scalar overwrite guard
("overwriting it requires stating the turn as a correction") while the SAME turn rel-lane
rows superseded via the F6 decider branch — the family walk voiced the stale address
attribute forever. THE FIX routes the residence ATTRIBUTE lane through the ONE decider
(single-authority discipline): the F6 safe subset (mutable-residence attribute, affirmed,
incoming now) supersedes the prior slot (soft, with history); everything else keeps the
shipped refusal.

F8 (fresh live row): a genuine re-statement landing on a RETIRED facts row used to resurrect
it in place (falsifying its validity span) — or, on the byte-identical path, was swallowed by
the idempotency cache echo (retirement preserved, NO new city row). THE FIX: migration 309
replaces the full natural-key unique constraint with a PARTIAL unique index on LIVE rows, the
store files a FRESH LIVE row for a genuine residence re-statement (never resurrecting the
archived one), replay-onto-existing-row is skipped (freeze contract intact), and the cache
echo busts when its residence row has since been retired.

PURE pins + fake-cursor store pins (no live DB). Run: python3 -m pytest
tests/test_f6_r2_residence_attribute_and_fresh_row.py -q
"""
from datetime import datetime

import pytest

import src.api.main as M
import src.fact_store.store as FS


def _new(rel="lives_at", prov="user_stated", status="now", pol="affirmed", correction=False):
    return {"fact_provenance": prov, "is_correction": correction, "polarity": pol,
            "rel_type": rel, "temporal_status": status}


def _old(prov="user_stated"):
    return {"fact_provenance": prov}


# ── F4: the residence ATTRIBUTE lane routes through the ONE decider ──────────────────
def test_f4_attribute_lane_probe_supersedes():
    """The exact probe shape the guard builds (rel_meta=None -> state coercion): a
    user-stated current lives_at value supersedes the stored one."""
    assert M._decide_supersede_or_coexist(_new("lives_at"), _old(), None) == "supersede_existing"


def test_f4_rel_meta_none_coercion_reaches_residence_branch():
    """Unknown/missing temporal_class coerces to state — the guard passes rel_meta=None
    and the residence branch must still fire (the bridge files no class metadata)."""
    assert M._decide_supersede_or_coexist(
        _new("lives_in", status=None), _old(), None) == "supersede_existing"


def test_f4_past_stamped_attribute_keeps_refusal():
    """A past-stamped address never writes the live slot (#135 discipline): the residence
    branch requires incoming now; a past value falls to blanket-coexist, so the
    guard refusal stands."""
    verdict = M._decide_supersede_or_coexist(_new("lives_at", status="past"), _old(), None)
    assert verdict == "coexist", verdict


def test_f4_negated_attribute_coexists():
    """A negation never archives via the residence branch (the cessation branch owns
    negations; without denied_by_incoming it coexists) — the guard refusal stands."""
    assert M._decide_supersede_or_coexist(_new("lives_at", pol="negated"), _old(), None) == "coexist"


def test_f4_lower_authority_incoming_dropped_by_firewall():
    """The trust firewall precedes the residence branch: an llm_learned address NEVER
    archives the user stored one."""
    assert M._decide_supersede_or_coexist(_new("lives_at", prov="llm_learned"), _old(), None) == "drop_incoming"


def test_f4_non_residence_attribute_keeps_refusal():
    """The overwrite guard dprompt-128 refusal covers every NON-residence scalar exactly
    as shipped (age, ip, height, occupation — single-decision discipline)."""
    assert M._decide_supersede_or_coexist(_new("occupation"), _old(), None) == "coexist"


def test_f4_lives_at_is_a_residence_identity():
    """The attribute the composite-address bridge files is in the ONE authority set."""
    assert M._rel_is_mutable_current_residence("lives_at") is True


# ── F8: the store natural-key routing ────────────────────────────────────────────────
def test_f8_store_residence_resolver_matches_main_authority():
    """Same authority, same folds: the store resolver and the main decider helper agree."""
    for rel in ("lives_at", "lives_in", "live_in", "reside_at"):
        assert FS._rel_is_mutable_residence_rel(rel) == M._rel_is_mutable_current_residence(rel), rel
    assert FS._rel_is_mutable_residence_rel("works_at") is False
    assert FS._rel_is_mutable_residence_rel("") is False
    assert FS._rel_is_mutable_residence_rel(None) is False


class _FakeCur:
    """Scripted cursor: records executed SQL, pops scripted fetchone rows in order."""

    def __init__(self, responses=None):
        self.executed = []
        self.responses = list(responses or [])

    def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self.responses.pop(0) if self.responses else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


_ROW_16 = ("user", "sub-uuid", "obj-uuid", "lives_at", "user_stated")


def test_f8_probe_states():
    retired_dt = datetime(2026, 11, 7)
    assert FS._facts_natural_key_state(_FakeCur([None]), "s", "o", "lives_at") is None
    assert FS._facts_natural_key_state(_FakeCur([(None,)]), "s", "o", "lives_at") == "live"
    assert FS._facts_natural_key_state(_FakeCur([(retired_dt,)]), "s", "o", "lives_at") == "retired"

    class _Boom:
        def execute(self, *a):
            raise RuntimeError("db gone")

        def fetchone(self):
            return None

    assert FS._facts_natural_key_state(_Boom(), "s", "o", "lives_at") is None


def test_f8_replay_onto_existing_row_is_frozen():
    """The deferred-replay freeze contract: a replay landing on ANY existing row (live or
    retired) is skipped entirely — retirement and count preserved, no write issued."""
    # first fetchone is consumed by the dprompt-130 taxonomies lookup
    conn = _FakeConn(_FakeCur([(None,), (datetime(2026, 11, 7),)]))
    n = FS.FactStoreManager(conn).commit([_ROW_16], fact_provenance="user_stated", replay=True)
    assert n == 0
    inserts = [sql for sql, _ in conn._cur.executed if "INSERT INTO" in sql]
    assert inserts == []


def test_f8_residence_restatement_on_retired_row_files_a_fresh_live_row():
    """THE F8 PIN: a genuine (non-replay) lives_at re-statement landing on a RETIRED row
    inserts a NEW live row via the partial-index arbiter — never an id-targeted revive."""
    conn = _FakeConn(_FakeCur([(None,), (datetime(2026, 11, 7),), (42,)]))
    n = FS.FactStoreManager(conn).commit([_ROW_16], fact_provenance="user_stated", replay=False)
    assert n == 1
    inserts = [(sql, params) for sql, params in conn._cur.executed if "INSERT INTO" in sql]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "ON CONFLICT (subject_id, object_id, rel_type) WHERE superseded_at IS NULL" in sql
    assert "ON CONFLICT (id)" not in sql
    assert params[0] == "sub-uuid"  # 16 value params -> no id column -> a NEW row id


def test_f8_non_residence_restatement_keeps_in_place_revive():
    """Outside the residence lane the shipped contract is byte-identical: a genuine
    re-statement on a retired AFFIRMED row revives THAT row (id-targeted pkey upsert).
    R7: the retired-id probe now returns (id, polarity) 2-wide — the twin here carries
    polarity='affirmed' (superseded-by-correction), the shape the revive CASE is for;
    a polarity='negated' denial-retired twin is refused by the R7 guard and is pinned
    in tests/test_correction_intent_rails.py."""
    conn = _FakeConn(_FakeCur([(None,), (datetime(2026, 11, 7),), (42, "affirmed")]))
    row = ("user", "sub-uuid", "obj-uuid", "owns", "user_stated")
    n = FS.FactStoreManager(conn).commit([row], fact_provenance="user_stated", replay=False)
    assert n == 1
    sql, params = [(s, p) for s, p in conn._cur.executed if "INSERT INTO" in s][0]
    assert "ON CONFLICT (id)" in sql
    assert params[0] == 42


def test_f8_live_row_and_no_row_take_the_predicate_upsert():
    """A live natural key (or an empty one) takes the normal partial-arbiter upsert."""
    conn = _FakeConn(_FakeCur([(None,), (None,)]))
    n = FS.FactStoreManager(conn).commit([_ROW_16], fact_provenance="user_stated", replay=False)
    assert n == 1
    sql = [s for s, _ in conn._cur.executed if "INSERT INTO" in s][0]
    assert "WHERE superseded_at IS NULL" in sql and "ON CONFLICT (id)" not in sql
    conn2 = _FakeConn(_FakeCur([(None,), None]))
    n2 = FS.FactStoreManager(conn2).commit([_ROW_16], fact_provenance="user_stated", replay=True)
    assert n2 == 1  # replay onto a NEVER-WRITTEN row is a first write


# ── F8 seam: the idempotency cache busts when its residence row is retired ───────────
def _patch_psycopg_connect(monkeypatch, cur):
    class _FakePSConn:
        def cursor(self):
            return cur

        def close(self):
            pass

    monkeypatch.setattr(M.psycopg2, "connect", lambda *a, **k: _FakePSConn())


def test_f8_cache_busts_when_residence_row_retired(monkeypatch):
    cur = _FakeCur([(1,)])  # a retired residence row exists
    _patch_psycopg_connect(monkeypatch, cur)
    resp = {"facts": [{"subject": "76e214a0-0ae9-4523-914c-f1709568bfa6",
                       "object": "30e65f04-56bb-5bdb-8c4f-c979e8608fc2",
                       "rel_type": "lives_in"}]}
    assert M._idempotency_cached_residence_rows_retired(
        "faultline_76e214a0_0ae9_4523_914c_f1709568bfa6", resp) is True
    cur2 = _FakeCur([None])  # a real SELECT 1 miss returns None, not a (None,) row
    _patch_psycopg_connect(monkeypatch, cur2)
    assert M._idempotency_cached_residence_rows_retired(
        "faultline_76e214a0_0ae9_4523_914c_f1709568bfa6", resp) is False


def test_f8_cache_bust_is_residence_only(monkeypatch):
    """A corrected NON-residence row never busts the echo (the correction lane shipped
    never-resurrect cache contract is untouched)."""
    seen = []
    cur = _FakeCur([])
    orig_execute = cur.execute

    def _recording_execute(sql, params=None):
        seen.append(" ".join(str(sql).split()))
        return orig_execute(sql, params)

    cur.execute = _recording_execute
    _patch_psycopg_connect(monkeypatch, cur)
    resp = {"facts": [{"subject": "76e214a0-0ae9-4523-914c-f1709568bfa6",
                       "object": "30e65f04-56bb-5bdb-8c4f-c979e8608fc2",
                       "rel_type": "spouse"}]}
    assert M._idempotency_cached_residence_rows_retired(
        "faultline_76e214a0_0ae9_4523_914c_f1709568bfa6", resp) is False
    assert not any("superseded_at IS NOT NULL" in s for s in seen)


def test_f8_cache_bust_fail_safes(monkeypatch):
    _patch_psycopg_connect(monkeypatch, _FakeCur([]))
    assert M._idempotency_cached_residence_rows_retired(None, {"facts": []}) is False
    assert M._idempotency_cached_residence_rows_retired("faultline_x", {}) is False
    resp = {"facts": [{"subject": "user", "object": "30e65f04-56bb-5bdb-8c4f-c979e8608fc2",
                       "rel_type": "lives_in"}]}
    assert M._idempotency_cached_residence_rows_retired("faultline_x", resp) is False


def test_f8_gate_chain_orders_the_bust_before_the_echo():
    """Source pin: in /ingest the residence-retired bust sits in the elif chain BEFORE the
    cached-response return — a bust falls through to the full re-ingest."""
    import inspect
    src = inspect.getsource(M)
    bust = src.find("ingest.idempotency_cache_busted_residence_retired")
    echo = src.find("ingest.idempotency_cache_hit")
    assert 0 < bust < echo, "the residence-retired bust must precede the cache echo return"


def test_f8_migration_309_pins_the_partial_index():
    """Migration 309 replaces the full constraint with the live-only partial unique index,
    fanned out over every schema holding a facts table (the 308 pattern)."""
    from pathlib import Path
    sql = Path(__file__).resolve().parents[1] / "migrations" / "309_facts_partial_live_unique.sql"
    body = sql.read_text(encoding="utf-8")
    assert "DROP CONSTRAINT facts_subject_id_object_id_rel_type_key" in body
    assert "CREATE UNIQUE INDEX IF NOT EXISTS facts_subject_object_rel_live_key" in body
    assert "WHERE superseded_at IS NULL" in body
    assert "pg_namespace" in body  # the per-schema fan-out


def test_f8_store_sql_carries_the_partial_arbiter():
    """The store live-route SQL targets migration 309 partial index — a retired twin
    cannot conflict, which is what makes the fresh live row possible."""
    assert "ON CONFLICT (subject_id, object_id, rel_type) WHERE superseded_at IS NULL" in FS._FACTS_INSERT_LIVE_SQL
    assert "ON CONFLICT (id)" in FS._FACTS_INSERT_REVIVE_SQL

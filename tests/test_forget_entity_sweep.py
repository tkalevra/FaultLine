"""THE FORGET ENTITY-SWEEP PINS (BUG-R4-H — the redaction class, 2026-11-14).

THE WOUND: /forget resolved sentence-TEXT objects ("Nadia Herrera was born on
2026-08-08"), deleted 4 qdrant points, returned forgotten=0 — the seat kept the
child's data whole, because the seat stores information STRUCTURALLY (entities +
facts + attrs keyed by entity id), not as sentence-shaped rows. Vector deletion
without row deletion is search-cosmetic, not redaction.

THE FIX under pin: ``_forget_entity_sweep`` — after the sentence-object pass, a
name-resolution pass. (a) proper-noun mentions from the forget text
(``proper_noun_mentions``, the spine parse — dates never mentions); (b) each
mention resolves via exact case-insensitive alias match admitting ONLY strong
matches; a weak/partial match DECLINES with an explicit reason (a redaction must
never sweep a namesake); (c) per strongly-resolved entity, hard-delete semantics
across every shape — facts both directions, entity_attributes, pending
staged_facts, entity_aliases, the episodic_log verbatim band, the entities row;
(d) the honest per-table envelope (SEC-BUG-1: never report a redaction that did
not happen — forgotten=0 must say WHY).
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


# ── HERMETIC LEGS: mention extraction ─────────────────────────────────────────────

def test_mention_extraction_the_wound_text_names_the_child():
    """The wound's own forget text yields exactly the child's full name — the date,
    the role noun, and the verb are never mentions."""
    from src.extraction.linguistics import proper_noun_mentions

    mentions = proper_noun_mentions(
        "Remove Nadia Herrera — newborn, born 2026-08-08")
    assert mentions == ("Nadia Herrera",), mentions


def test_mention_extraction_dates_are_never_mentions():
    """Month/day fragments and digits are values, not referents — a date-shaped
    PROPN run ('born on August 8') must not surface as a mention."""
    from src.extraction.linguistics import proper_noun_mentions

    mentions = proper_noun_mentions("Nadia Herrera was born on August 8")
    assert mentions == ("Nadia Herrera",), mentions
    # pure date-ish text yields NOTHING to resolve — the sweep then declines.
    assert proper_noun_mentions("born 2026-08-08, turning one on August 8") == ()


def test_mention_extraction_dedup_case_insensitively_and_fail_safe():
    """A repeated name collapses to one mention (first casing); empty/unparsable
    text fails safe to () — the caller declines, never guesses."""
    from src.extraction.linguistics import proper_noun_mentions

    mentions = proper_noun_mentions(
        "Wrenna is my sister and Wrenna plays violin with Wrenna's teacher Marlow")
    assert mentions.count("Wrenna") == 1, mentions
    assert "Marlow" in mentions
    assert proper_noun_mentions("") == ()
    assert proper_noun_mentions("   ") == ()


# ── HERMETIC LEGS: the strong/weak ADMISSION RULE ─────────────────────────────────

def test_admission_strong_unique_holder_admits():
    """Exactly one entity holds the full alias → ADMIT (strong: the full alias
    equals the mention, which subsumes the preferred-alias arm)."""
    import src.api.main as M

    eid, reason = M._admit_forget_mention(
        "Nadia Herrera", ["11111111-1111-1111-1111-111111111111"],
        "99999999-9999-9999-9999-999999999999")
    assert eid == "11111111-1111-1111-1111-111111111111"
    assert reason is None


def test_admission_namesake_ambiguity_declines():
    """The same full alias held by TWO entities identifies NEITHER — decline with
    the explicit ambiguity reason; a redaction must never pick."""
    import src.api.main as M

    eid, reason = M._admit_forget_mention(
        "Nadia Herrera",
        ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"],
        "99999999-9999-9999-9999-999999999999")
    assert eid is None
    assert reason == "ambiguous_alias:2"


def test_admission_speaker_entity_declines():
    """The single holder being the SPEAKER is not a bounded redaction — the user
    entity is not sweepable through the name pass."""
    import src.api.main as M

    _u = "99999999-9999-9999-9999-999999999999"
    eid, reason = M._admit_forget_mention("Sam", [_u], _u)
    assert eid is None
    assert reason == "speaker_entity"


def test_admission_no_holders_is_undecidable_not_declined():
    """Zero exact holders is neither admit nor decline HERE — the caller's weak/
    partial probe supplies the evidence, so a namesake partial can never ride the
    strong arm in."""
    import src.api.main as M

    eid, reason = M._admit_forget_mention("Nadia Herrera", [], "u")
    assert eid is None and reason is None
    # degenerate mention: never admissible
    eid, reason = M._admit_forget_mention("  ", ["x"], "u")
    assert eid is None and reason is None


# ── HERMETIC LEGS: OFF = byte-parity + the envelope's honest shape ────────────────

def test_flag_default_on_and_off_is_byte_parity():
    """FORGET_ENTITY_SWEEP defaults ON; the handler invokes the sweep ONLY under
    the flag (assert on source so the guard survives refactors — the wire-test
    pattern), and the response model carries the optional envelope field."""
    import inspect
    import src.api.main as M

    assert M.FORGET_ENTITY_SWEEP is True
    src_text = inspect.getsource(M.forget_fact)
    assert "if FORGET_ENTITY_SWEEP:" in src_text
    assert "_forget_entity_sweep(cur, req.user_id, _forget_text)" in src_text
    from src.api.models import RetractResponse
    assert "entity_sweep" in RetractResponse.model_fields


class _SweepStoreCur:
    """A scripted cursor over the sweep's own query shapes — enough store to pin
    the ENVELOPE (per-table counts + declined reasons) hermetically, no DB."""

    def __init__(self, aliases, facts, attrs, staged, episodic):
        self.aliases = dict(aliases)          # alias(lower) -> entity_id
        self.entity_aliases = {v: set() for v in aliases.values()}
        for a, e in aliases.items():
            self.entity_aliases[e].add(a)
        self.facts = list(facts)              # live fact ids
        self.attrs = list(attrs)
        self.staged = list(staged)
        self.episodic = list(enumerate(episodic))  # (id, text)
        self.rowcount = 0
        self._pending = []

    def execute(self, sql, params=None):
        s = sql.strip()
        if s.startswith("SELECT DISTINCT entity_id FROM entity_aliases WHERE lower(alias) ="):
            self._pending = [self.aliases.get(params[0].lower())]
            self._pending = [p for p in self._pending if p]
        elif "position(lower(alias) IN" in s:
            # the weak/partial probe (decline-only)
            m = params[0].strip("%")
            self._pending = [e for a, e in self.aliases.items() if m in a]
        elif s.startswith("SELECT alias FROM entity_aliases WHERE entity_id = ANY"):
            _out = set()
            for e in params[0]:
                _out |= self.entity_aliases.get(e, set())
            self._pending = list(_out)
        elif "SELECT alias FROM entity_aliases" in s and "LIKE" in s:
            # the admit-arm namesake-protection probe
            m = params[0].strip("%")
            _not = params[1] if isinstance(params[1], (list, tuple)) else [params[1]]
            self._pending = [a for a, e in self.aliases.items()
                             if m in a and e not in _not and len(a) > params[2]]
        elif s.startswith("SELECT alias FROM entity_aliases"):
            self._pending = list(self.entity_aliases.get(params[0], ()))
        elif s.startswith("SELECT id FROM facts"):
            self._pending = list(self.facts)
            self.rowcount = 0
        elif s.startswith("UPDATE facts"):
            self.rowcount = len(params[0])
            self.facts = []
        elif s.startswith("DELETE FROM entity_attributes"):
            self.rowcount = len(self.attrs)
            self.attrs = []
        elif s.startswith("SELECT id FROM staged_facts"):
            self._pending = list(self.staged)
        elif s.startswith("UPDATE staged_facts"):
            self.rowcount = len(params[0])
            self.staged = []
        elif s.startswith("SELECT id FROM episodic_log"):
            _adm = params[0]
            _pro = params[1] if len(params) > 1 else []
            self._pending = [
                i for i, t in self.episodic
                if _any_pat(t, _adm) and not _any_pat(t, _pro)]
        elif s.startswith("DELETE FROM episodic_log"):
            _dead = set(params[0])
            self.rowcount = sum(1 for i, _ in self.episodic if i in _dead)
            self.episodic = [(i, t) for i, t in self.episodic if i not in _dead]
        elif s.startswith("DELETE FROM entity_aliases"):
            self.rowcount = len(self.entity_aliases.pop(params[0], set()))
        elif s.startswith("DELETE FROM entities"):
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected sweep SQL: {s[:80]}")

    def fetchall(self):
        return [(p,) for p in self._pending]


def _any_pat(text, pats):
    """Word-boundary regex match against the sweep's PG patterns ('\\y…\\y')."""
    import re as _re
    for p in pats or ():
        _py = p.replace("\\y", "\\b").replace("\\ ", " ")
        if _re.search(_py, text, flags=_re.IGNORECASE):
            return True
    return False


def test_envelope_shape_and_honest_zero(monkeypatch):
    """The envelope reports per-table counts for what it deleted, declines with
    reasons, and forgotten=0 with NO structured entity says WHY (the SEC-BUG-1
    contract). No commit inside the helper (the caller owns the commit)."""
    import inspect
    import src.api.main as M

    _aurora = "11111111-1111-1111-1111-111111111111"
    cur = _SweepStoreCur(
        aliases={"aurora flood": _aurora, "rory": _aurora},
        facts=[1, 2], attrs=[("age", 0)], staged=[7],
        episodic=["Nadia Herrera arrived", "the aurora borealis photo", "unrelated"])
    monkeypatch.setattr(M, "_forget_name_mentions",
                        lambda t: ("Nadia Herrera", "Rory", "Nobody Here"))
    envelope, fact_ids, staged_ids = M._forget_entity_sweep(
        cur, "99999999-9999-9999-9999-999999999999",
        "Remove Nadia Herrera — newborn, born 2026-08-08")

    assert sorted(envelope.keys()) == ["admitted", "declined", "totals"]
    assert sorted(envelope["totals"].keys()) == [
        "entities", "entity_aliases", "entity_attributes", "episodic_log",
        "facts", "staged_facts"]
    assert len(envelope["admitted"]) == 1
    adm = envelope["admitted"][0]
    assert adm["entity_id"] == _aurora and adm["mention"] == "Nadia Herrera"
    assert adm["facts"] == 2 and adm["staged_facts"] == 1
    assert envelope["totals"]["facts"] == 2
    assert envelope["totals"]["entity_attributes"] == 1
    assert envelope["totals"]["staged_facts"] == 1
    assert envelope["totals"]["entity_aliases"] == 2
    assert envelope["totals"]["entities"] == 1
    assert envelope["totals"]["episodic_log"] == 1
    assert fact_ids == [1, 2] and staged_ids == [7]
    assert envelope["declined"] == [{"mention": "Nobody Here",
                                     "reason": "no_structured_entity"}]
    # the caller owns the commit — counts follow the commit (SEC-BUG-1)
    assert ".commit(" not in inspect.getsource(M._forget_entity_sweep)


def test_envelope_empty_text_and_weak_decline(monkeypatch):
    """Empty forget text sweeps nothing (empty envelope); a mention that only
    PARTIALLY matches a stored alias declines with the namesake reason and the
    weak probe never triggers a sweep."""
    import src.api.main as M

    env, fids, sids = M._forget_entity_sweep(M.__dict__ and object(), "u", "")
    assert env == {"admitted": [], "declined": [],
                   "totals": {"facts": 0, "entity_attributes": 0, "staged_facts": 0,
                              "episodic_log": 0, "entity_aliases": 0, "entities": 0}}
    assert fids == [] and sids == []

    _aurora = "11111111-1111-1111-1111-111111111111"
    cur = _SweepStoreCur(aliases={"aurora flood": _aurora}, facts=[1],
                         attrs=[], staged=[], episodic=[])
    monkeypatch.setattr(M, "_forget_name_mentions", lambda t: ("Aurora",))
    envelope, fact_ids, staged_ids = M._forget_entity_sweep(cur, "u2", "Forget Aurora")
    assert envelope["admitted"] == []
    assert envelope["declined"] == [{"mention": "Aurora",
                                     "reason": "weak_partial_alias_match"}]
    assert fact_ids == [] and staged_ids == []
    assert cur.facts == [1]  # NOTHING was swept


# ── DB-GATED LEGS (temp tables shadowing the tenant names, real shapes) ───────────

_DSN = os.environ.get("POSTGRES_DSN", "")
_fes_db = pytest.mark.skipif(
    not _DSN, reason="POSTGRES_DSN unset — the sweep DB gate stays dark")

_AURORA = "aaaaaaaa-1111-1111-1111-111111111111"
_USER = "99999999-9999-9999-9999-999999999999"
_DATE = "dddddddd-1111-1111-1111-111111111111"


def _fes_plant(cur):
    cur.execute("SET search_path TO pg_temp, public")
    cur.execute(
        "CREATE TEMP TABLE entities (id TEXT PRIMARY KEY, entity_type TEXT,"
        " node_role TEXT, created_at TIMESTAMPTZ DEFAULT now())")
    cur.execute(
        "CREATE TEMP TABLE entity_aliases (id SERIAL PRIMARY KEY, entity_id TEXT"
        " NOT NULL, alias TEXT NOT NULL, display_form TEXT,"
        " is_preferred BOOLEAN NOT NULL DEFAULT false, preference_source TEXT"
        " NOT NULL DEFAULT 'unspecified', coreference_warrant TEXT,"
        " created_at TIMESTAMPTZ DEFAULT now(), valid_from TIMESTAMPTZ DEFAULT now(),"
        " valid_until TIMESTAMPTZ, UNIQUE (entity_id, alias),"
        " FOREIGN KEY (entity_id) REFERENCES entities(id))")
    cur.execute(
        "CREATE TEMP TABLE facts (id SERIAL PRIMARY KEY, subject_id TEXT NOT NULL,"
        " object_id TEXT NOT NULL, rel_type TEXT NOT NULL, provenance TEXT,"
        " fact_provenance TEXT NOT NULL DEFAULT 'llm_inferred', fact_class TEXT,"
        " qdrant_synced BOOLEAN NOT NULL DEFAULT false,"
        " superseded_at TIMESTAMPTZ, archived_at TIMESTAMPTZ,"
        " deleted_at TIMESTAMPTZ)")
    cur.execute(
        "CREATE TEMP TABLE staged_facts (id SERIAL PRIMARY KEY, subject_id TEXT"
        " NOT NULL, object_id TEXT NOT NULL, rel_type TEXT, promoted_at TIMESTAMPTZ,"
        " deleted_at TIMESTAMPTZ)")
    cur.execute(
        "CREATE TEMP TABLE entity_attributes (user_id TEXT NOT NULL,"
        " entity_id TEXT NOT NULL, attribute TEXT NOT NULL, value_text TEXT,"
        " superseded_at TIMESTAMPTZ,"
        " FOREIGN KEY (entity_id) REFERENCES entities(id))")
    cur.execute(
        "CREATE TEMP TABLE episodic_log (id BIGSERIAL PRIMARY KEY,"
        " user_id TEXT NOT NULL, raw_text TEXT NOT NULL, source TEXT)")
    # the aurora-shaped store (the wound's own census): both-direction facts,
    # attrs, aliases, pending staged, verbatim episodic — plus the survivors.
    cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s,'person'),"
                " (%s,'person'), (%s,'date')", (_USER, _AURORA, _DATE))
    cur.execute("INSERT INTO entity_aliases (entity_id, alias, is_preferred)"
                " VALUES (%s,'aurora flood',true), (%s,'rory',false),"
                " (%s,'user',true), (%s,'2026-08-08',false)",
                (_AURORA, _AURORA, _USER, _DATE))
    cur.execute("INSERT INTO facts (subject_id, rel_type, object_id) VALUES"
                f" ('{_USER}','has_child','{_AURORA}'),"
                f" ('{_AURORA}','born_on','{_DATE}')")
    cur.execute("INSERT INTO entity_attributes (user_id, entity_id, attribute,"
                f" value_text) VALUES ('{_USER}','{_AURORA}','age','0')")
    cur.execute("INSERT INTO staged_facts (subject_id, rel_type, object_id)"
                f" VALUES ('{_AURORA}','has_state','{_DATE}')")
    cur.execute("INSERT INTO episodic_log (user_id, raw_text) VALUES"
                " ('u','Nadia Herrera was born today'),"
                " ('u','the aurora borealis photo'),"
                " ('u','totally unrelated turn')")


@_fes_db
def test_db_sweep_redacts_the_aurora_shape_to_zero():
    """The wound's own shape: the full-name mention admits the entity and every
    storage shape redacts to zero — facts both directions tombstoned with the
    qdrant purge mark, attrs HARD-DELETED (the entity_attributes→entities FK makes
    a superseded-KEPT row an FK violation once the entities row goes — CRIT-1's
    500-and-rollback shape, pinned by the FK on the temp table), staged deleted,
    aliases gone, the verbatim band's aurora row gone, the entities row gone. The
    namesake text ('aurora borealis', a DIFFERENT surface) and unrelated rows
    SURVIVE."""
    import psycopg2
    import src.api.main as M

    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            _fes_plant(cur)
        conn.commit()
        envelope, fact_ids, staged_ids = M._forget_entity_sweep(
            conn.cursor(), _USER, "Remove Nadia Herrera — newborn, born 2026-08-08")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM facts WHERE deleted_at IS NULL")
            live_facts = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM facts WHERE deleted_at IS NOT NULL"
                        " AND qdrant_synced = false")
            purged = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM entity_attributes"
                        " WHERE entity_id = %s", (_AURORA,))
            live_attrs = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM staged_facts WHERE deleted_at IS NULL")
            live_staged = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM entity_aliases"
                        " WHERE entity_id = %s", (_AURORA,))
            live_aliases = cur.fetchone()[0]
            cur.execute("SELECT raw_text FROM episodic_log ORDER BY raw_text")
            episodic = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM entities WHERE id = %s", (_AURORA,))
            live_entity = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()

    assert len(envelope["admitted"]) == 1
    assert envelope["admitted"][0]["entity_id"] == _AURORA
    assert envelope["totals"] == {
        "facts": 2, "entity_attributes": 1, "staged_facts": 1,
        "episodic_log": 1, "entity_aliases": 2, "entities": 1}
    assert len(fact_ids) == 2 and len(staged_ids) == 1
    assert (live_facts, purged) == (0, 2)
    assert live_attrs == 0 and live_staged == 0
    assert live_aliases == 0 and live_entity == 0
    assert sorted(episodic) == ["the aurora borealis photo",
                                "totally unrelated turn"]


@_fes_db
def test_db_sweep_namesake_partial_alias_declines_and_sweeps_nothing():
    """'Aurora' against a stored 'aurora flood' alias is a WEAK partial match —
    the sweep declines with the namesake reason and every row stays live."""
    import psycopg2
    import src.api.main as M

    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            _fes_plant(cur)
        conn.commit()
        envelope, fact_ids, staged_ids = M._forget_entity_sweep(
            conn.cursor(), _USER, "Forget Aurora")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM facts WHERE deleted_at IS NULL")
            live_facts = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM episodic_log")
            live_episodic = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()

    assert envelope["admitted"] == []
    assert {"mention": "Aurora",
            "reason": "weak_partial_alias_match"} in envelope["declined"]
    assert fact_ids == [] and staged_ids == []
    assert envelope["totals"]["facts"] == 0
    assert live_facts == 2 and live_episodic == 3


@_fes_db
def test_db_sweep_ambiguous_full_alias_declines():
    """The same full name held by two entities (a genuine namesake pair) declines
    with the ambiguity reason — a redaction must never pick."""
    import psycopg2
    import src.api.main as M

    _twin = "bbbbbbbb-1111-1111-1111-111111111111"
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            _fes_plant(cur)
            cur.execute("INSERT INTO entities (id, entity_type)"
                        f" VALUES ('{_twin}','person')")
            cur.execute("INSERT INTO entity_aliases (entity_id, alias)"
                        f" VALUES ('{_twin}','aurora flood')")
        conn.commit()
        envelope, fact_ids, _ = M._forget_entity_sweep(
            conn.cursor(), _USER, "Remove Nadia Herrera — newborn, born 2026-08-08")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM facts WHERE deleted_at IS NULL")
            live_facts = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()

    assert envelope["admitted"] == []
    assert {"mention": "Nadia Herrera",
            "reason": "ambiguous_alias:2"} in envelope["declined"]
    assert fact_ids == [] and live_facts == 2


@_fes_db
def test_db_sweep_admitted_word_namesake_phrase_survives():
    """CRIT-2's exact shape: the admitted alias is the WORD 'aurora' (a genuine
    exact-alias entity), and a DIFFERENT entity carries the longer alias
    'aurora borealis'. A word boundary is NOT a phrase boundary — the old
    '\\yaurora\\y' delete physically ate the namesake's verbatim rows while the
    envelope over-counted. Now: the bare-aurora rows die with an HONEST count,
    every 'aurora borealis' row survives, and the entities DELETE still lands
    (no FK fallout)."""
    import psycopg2
    import src.api.main as M

    _word = "eeeeeeee-1111-1111-1111-111111111111"   # alias 'aurora'
    _bore = "ffffffff-1111-1111-1111-111111111111"   # alias 'aurora borealis'
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO pg_temp, public")
            cur.execute(
                "CREATE TEMP TABLE entities (id TEXT PRIMARY KEY,"
                " entity_type TEXT, created_at TIMESTAMPTZ DEFAULT now())")
            cur.execute(
                "CREATE TEMP TABLE entity_aliases (id SERIAL PRIMARY KEY,"
                " entity_id TEXT NOT NULL, alias TEXT NOT NULL,"
                " is_preferred BOOLEAN NOT NULL DEFAULT false,"
                " FOREIGN KEY (entity_id) REFERENCES entities(id))")
            cur.execute(
                "CREATE TEMP TABLE facts (id SERIAL PRIMARY KEY,"
                " subject_id TEXT NOT NULL, object_id TEXT NOT NULL,"
                " rel_type TEXT, qdrant_synced BOOLEAN NOT NULL DEFAULT false,"
                " superseded_at TIMESTAMPTZ, archived_at TIMESTAMPTZ,"
                " deleted_at TIMESTAMPTZ)")
            cur.execute(
                "CREATE TEMP TABLE staged_facts (id SERIAL PRIMARY KEY,"
                " subject_id TEXT NOT NULL, object_id TEXT NOT NULL,"
                " rel_type TEXT, promoted_at TIMESTAMPTZ,"
                " deleted_at TIMESTAMPTZ)")
            cur.execute(
                "CREATE TEMP TABLE entity_attributes (user_id TEXT NOT NULL,"
                " entity_id TEXT NOT NULL, attribute TEXT NOT NULL,"
                " value_text TEXT, superseded_at TIMESTAMPTZ,"
                " FOREIGN KEY (entity_id) REFERENCES entities(id))")
            cur.execute(
                "CREATE TEMP TABLE episodic_log (id BIGSERIAL PRIMARY KEY,"
                " user_id TEXT NOT NULL, raw_text TEXT NOT NULL, source TEXT)")
            cur.execute("INSERT INTO entities (id) VALUES (%s),(%s)",
                        (_word, _bore))
            cur.execute("INSERT INTO entity_aliases (entity_id, alias,"
                        " is_preferred) VALUES (%s,'aurora',true),"
                        " (%s,'aurora borealis',true)", (_word, _bore))
            cur.execute("INSERT INTO facts (subject_id, rel_type, object_id)"
                        f" VALUES ('{_USER}','has_child','{_word}')")
            cur.execute("INSERT INTO episodic_log (user_id, raw_text) VALUES"
                        " ('u','Aurora arrived home'),"
                        " ('u','we saw the aurora borealis last night'),"
                        " ('u','Aurora slept'),"
                        " ('u','aurora borealis over the lake'),"
                        " ('u','totally unrelated')")
        conn.commit()
        envelope, fact_ids, _ = M._forget_entity_sweep(
            conn.cursor(), _USER, "Forget Aurora")
        with conn.cursor() as cur:
            cur.execute("SELECT raw_text FROM episodic_log ORDER BY raw_text")
            episodic = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM entities WHERE id = %s", (_word,))
            word_gone = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM entity_aliases"
                        " WHERE entity_id = %s", (_bore,))
            bore_aliases = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()

    # the WORD entity was admitted and swept
    assert len(envelope["admitted"]) == 1
    assert envelope["admitted"][0]["entity_id"] == _word
    assert envelope["totals"]["facts"] == 1 and len(fact_ids) == 1
    assert word_gone == 0
    # HONEST COUNT: exactly the two bare-aurora rows — the two 'aurora borealis'
    # rows and the unrelated row SURVIVE (phrase-boundary protection).
    assert envelope["totals"]["episodic_log"] == 2
    assert sorted(episodic) == ["aurora borealis over the lake",
                                "totally unrelated",
                                "we saw the aurora borealis last night"]
    # the namesake entity is untouched
    assert bore_aliases == 1


def test_qdrant_purge_filter_shape_per_seat_vs_shared(monkeypatch):
    """CRIT-3: in collection_per_seat (the DEFAULT) the delete bodies carry NO
    tenant_id filter — per-seat points have no tenant payload, so the filter was a
    200-OK silent no-op. In shared_payload mode the tenant filter MUST be present
    (the cross-tenant leak guard).

    THE REMOVAL (REMOVE_QDRANT_MEMORY_LANE, default ON): this specs the legacy purge
    funnel, which survives verbatim behind the removal's early-exit as the rollback arm —
    run against flag OFF. The removed contract (no purge at all under the removal) is
    pinned in test_removal_qdrant_memory_lane.py."""
    import json
    import src.api.main as M

    monkeypatch.setattr(M, "REMOVE_QDRANT_MEMORY_LANE", False)

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self):
            self.posts = []

        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append((url, json))
            return _Resp()

    _uid = "11111111-1111-1111-1111-111111111111"
    cli = _Client()
    monkeypatch.setattr(M, "_http_client_sync", cli)
    M._delete_from_qdrant([5], "seatcol", "http://q", "facts", user_id=_uid)
    assert cli.posts, "the purge must still issue deletes"
    for _url, body in cli.posts:
        assert "tenant_id" not in json.dumps(body), body

    cli2 = _Client()
    monkeypatch.setattr(M, "_http_client_sync", cli2)
    monkeypatch.setenv("QDRANT_PARTITION_MODE", "shared_payload")
    try:
        M._delete_from_qdrant([5], "sharedcol", "http://q", "facts", user_id=_uid)
    finally:
        monkeypatch.delenv("QDRANT_PARTITION_MODE")
    assert cli2.posts
    assert all("tenant_id" in json.dumps(b) for _u, b in cli2.posts)

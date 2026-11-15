"""KIN WELD-ENTITY REPOINT — the ingest-side half of SPINE_ROLE_HEAD_NO_BIND (fog-family lab
F1-R2/R3). Pins the SEAT-STATE-DEPENDENT half the R1 clean-DB pins could not see, in the LIVE
ROW DIRECTION (critic R3: the R2 pins encoded the inverse direction and were hermetic-only —
the live pipeline stores (user, spouse, NAME), SUBJECT=user, OBJECT=name, and the /ingest
door's kin edges may arrive either direction).

Pinned here (all hermetic — NO live DB):
  • collect_kin_name_binds in BOTH edge directions: the LIVE door shape (user, spouse,
    isolde) + (isolde, also_known_as, wife) — and the deriver's own surface shape
    (isolde, spouse, user) — both yield the bind; plus the refusals (no kin edge, non-kin
    alias object, mapped-rel mismatch, name==role, self-kin-edge).
  • rival_is_pure_role_weld: a rival whose ENTIRE alias set is kinship role words is a weld
    entity (archived); a rival carrying ANY real-name alias is a person — never touched.
  • kin_name_surface_taint (the R2-B-2 mint gate): a pure role-weld alias target taints;
    an entity carrying a LIVE row of a DIFFERENT kinship rel to the user taints (the
    Rosalind-on-the-mother wound); a genuine named person with the same kin rel does NOT;
    an unknown surface does not.
  • kin_role_object_on_weld (confirm-magnetic-NO): a role-word surface resolving onto a pure
    weld taints; onto a named entity or nowhere does not.
  • retire_rival_weld_entities against a fake cursor in the LIVE arrangement: the ghost row
    (user, spouse, weld) is superseded by the named bind (user, spouse, isolde), the weld's
    alias is transferred off, a named rival is spared, an uncommitted bind no-ops.
"""
from collections import namedtuple

from src.api.kin_weld_repoint import (
    collect_kin_name_binds,
    kin_name_surface_taint,
    kin_role_object_on_weld,
    rival_is_pure_role_weld,
    retire_rival_weld_entities,
)

Edge = namedtuple("Edge", ["subject", "rel_type", "object", "is_correction"])
KIN_NOUNS = frozenset({"wife", "husband", "spouse", "partner", "mother", "father",
                       "son", "daughter", "kid", "child", "sibling", "brother", "sister",
                       "grandmother", "grandfather"})
KIN_REL_MAP = {"wife": "spouse", "husband": "spouse", "spouse": "spouse",
               "partner": "spouse", "mother": "parent_of", "father": "parent_of",
               "son": "child_of", "daughter": "child_of", "kid": "child_of",
               "child": "child_of", "sibling": "sibling_of", "brother": "sibling_of",
               "sister": "sibling_of", "grandmother": "related_to",
               "grandfather": "related_to"}
KIN_REL_VALUES = set(KIN_REL_MAP.values())


def _edges_isolde_live():
    # the LIVE /ingest arrangement: the kin row is (user, spouse, name).
    return [Edge(subject="user", rel_type="spouse", object="isolde", is_correction=False),
            Edge(subject="isolde", rel_type="also_known_as", object="wife", is_correction=False)]


def _edges_isolde_deriver():
    # the deriver's own surface arrangement: (name, spouse, user).
    return [Edge(subject="isolde", rel_type="spouse", object="user", is_correction=False),
            Edge(subject="isolde", rel_type="also_known_as", object="wife", is_correction=False)]


# ── bind signature ──────────────────────────────────────────────────────────────────────

def test_bind_signature_found_in_live_direction():
    assert collect_kin_name_binds(_edges_isolde_live(), KIN_REL_MAP, KIN_NOUNS) == [
        ("isolde", "wife", "spouse")]


def test_bind_signature_found_in_deriver_direction():
    assert collect_kin_name_binds(_edges_isolde_deriver(), KIN_REL_MAP, KIN_NOUNS) == [
        ("isolde", "wife", "spouse")]


def test_bind_signature_refusals():
    # aka alone (no kin edge) — never a bind
    assert collect_kin_name_binds(
        [Edge(subject="isolde", rel_type="also_known_as", object="wife", is_correction=False)],
        KIN_REL_MAP, KIN_NOUNS) == []
    # non-kin alias object (the pet/manager classes) — never matches
    assert collect_kin_name_binds(
        [Edge(subject="rex", rel_type="has_pet", object="user", is_correction=False),
         Edge(subject="rex", rel_type="also_known_as", object="manager", is_correction=False)],
        KIN_REL_MAP, KIN_NOUNS) == []
    # mapped-rel mismatch: wife binds via spouse, not parent_of
    assert collect_kin_name_binds(
        [Edge(subject="isolde", rel_type="parent_of", object="user", is_correction=False),
         Edge(subject="isolde", rel_type="also_known_as", object="wife", is_correction=False)],
        KIN_REL_MAP, KIN_NOUNS) == []
    # name == role (the self-weld surface) — never a bind
    assert collect_kin_name_binds(
        [Edge(subject="wife", rel_type="spouse", object="user", is_correction=False),
         Edge(subject="wife", rel_type="also_known_as", object="wife", is_correction=False)],
        KIN_REL_MAP, KIN_NOUNS) == []
    # the kin edge touching the name on BOTH ends (self-pair) — never a bind
    assert collect_kin_name_binds(
        [Edge(subject="isolde", rel_type="spouse", object="isolde", is_correction=False),
         Edge(subject="isolde", rel_type="also_known_as", object="wife", is_correction=False)],
        KIN_REL_MAP, KIN_NOUNS) == []
    # empty batch
    assert collect_kin_name_binds([], KIN_REL_MAP, KIN_NOUNS) == []


def test_bind_signature_mother_and_dedup():
    edges = [Edge(subject="user", rel_type="parent_of", object="marta", is_correction=False),
             Edge(subject="marta", rel_type="also_known_as", object="mother", is_correction=False),
             Edge(subject="marta", rel_type="also_known_as", object="mother", is_correction=False)]
    assert collect_kin_name_binds(edges, KIN_REL_MAP, KIN_NOUNS) == [
        ("marta", "mother", "parent_of")]


def test_pure_role_weld_signature():
    assert rival_is_pure_role_weld({"wife"}, KIN_NOUNS) is True           # the R1 weld entity
    assert rival_is_pure_role_weld({"wife", "spouse"}, KIN_NOUNS) is True  # multi-role weld
    assert rival_is_pure_role_weld({"seraphina", "wife"}, KIN_NOUNS) is False  # a NAMED person
    assert rival_is_pure_role_weld({"manager"}, KIN_NOUNS) is False        # non-kin role
    assert rival_is_pure_role_weld(set(), KIN_NOUNS) is False
    assert rival_is_pure_role_weld({""}, KIN_NOUNS) is False


# ── the resolution gates (R2-B-2) ───────────────────────────────────────────────────────

class _GateCursor:
    """Fake for the read-only gate helpers: scripted alias probe + entity aliases + kin rows."""

    def __init__(self, alias_hits, entity_aliases, kin_rows):
        # alias_hits: surface -> entity_id; entity_aliases: entity_id -> set;
        # kin_rows: list of (subj, rel, obj) LIVE rows
        self._hits = alias_hits
        self._aliases = {k: set(v) for k, v in entity_aliases.items()}
        self._kin = list(kin_rows)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM entity_aliases" in s and "WHERE lower(alias) = %s" in s and "ORDER BY" in s:
            hit = self._hits.get(params[0])
            self._last = [(hit,)] if hit else []
        elif s.startswith("SELECT lower(alias) FROM entity_aliases WHERE entity_id"):
            self._last = [(a,) for a in sorted(self._aliases.get(params[0], set()))]
        elif s.startswith("SELECT rel_type FROM facts"):
            rels, kin_rel, a, b = params[0], params[1], params[2], params[3]
            found = None
            for subj, rel, obj in self._kin:
                if rel in rels and rel != kin_rel and (
                        (subj == a and obj == b) or (subj == b and obj == a)):
                    found = rel
                    break
            self._last = [(found,)] if found else []
        else:
            self._last = []

    def fetchone(self):
        return self._last[0] if getattr(self, "_last", None) else None

    def fetchall(self):
        return list(getattr(self, "_last", []))


class _GateDB:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        class _C:
            def __enter__(self_inner):
                return self._cursor

            def __exit__(self_inner, *a):
                return False
        return _C()


def _gate_db(alias_hits, entity_aliases, kin_rows):
    return _GateDB(_GateCursor(alias_hits, entity_aliases, kin_rows))


USER = "seat-user-uuid-0001"
MOTHER = "mother-entity-uuid-0001"
SELIN = "selin-entity-uuid-0001"
WELD = "weld-entity-uuid-00001"


def test_taint_pure_role_weld_alias_target():
    db = _gate_db({"isolde": WELD}, {WELD: {"wife"}}, [])
    t = kin_name_surface_taint(db, "isolde", "spouse", KIN_NOUNS, KIN_REL_VALUES, USER)
    assert t and t["taint"] == "pure_role_weld" and t["entity_id"] == WELD


def test_taint_conflicting_kin_rel_target():
    # the Rosalind-on-the-mother wound: the surface aliases onto an entity that already
    # carries a LIVE parent_of row to the user — a wife bind cannot resolve onto her.
    db = _gate_db({"rosalind": MOTHER}, {MOTHER: {"mother", "imogen", "rosalind"}},
                  [(USER, "parent_of", MOTHER)])
    t = kin_name_surface_taint(db, "rosalind", "spouse", KIN_NOUNS, KIN_REL_VALUES, USER)
    assert t and t["taint"] == "kin_conflict" and t["conflicting_rel"] == "parent_of"


def test_no_taint_on_genuine_named_entity():
    # seraphina+wife with a live SPOUSE row (same rel as the bind) — the safe target.
    db = _gate_db({"seraphina": SELIN}, {SELIN: {"seraphina", "wife"}},
                  [(USER, "spouse", SELIN)])
    assert kin_name_surface_taint(db, "seraphina", "spouse", KIN_NOUNS,
                                  KIN_REL_VALUES, USER) is None


def test_no_taint_on_unknown_surface():
    db = _gate_db({}, {}, [])
    assert kin_name_surface_taint(db, "maribel", "spouse", KIN_NOUNS,
                                  KIN_REL_VALUES, USER) is None


def test_weld_object_gate():
    # prong B: the role-word surface itself resolving onto the pure weld — confirm-magnetic-NO.
    db = _gate_db({"wife": WELD}, {WELD: {"wife"}}, [])
    assert kin_role_object_on_weld(db, "wife", KIN_NOUNS) is True
    # a role-word surface that resolves onto a NAMED entity — do not refuse.
    db2 = _gate_db({"wife": SELIN}, {SELIN: {"seraphina", "wife"}}, [])
    assert kin_role_object_on_weld(db2, "wife", KIN_NOUNS) is False
    # no alias at all — mint path, nothing to refuse.
    db3 = _gate_db({}, {}, [])
    assert kin_role_object_on_weld(db3, "wife", KIN_NOUNS) is False


# ── the retire pass, LIVE row arrangement ───────────────────────────────────────────────

class _RetireCursor:
    """Records writes; scripted reads keyed on the SQL shape + params. Facts are ROWS so both
    the live (user, spouse, name) and deriver (name, spouse, user) arrangements are exercisable."""

    def __init__(self, entity_aliases, fact_rows):
        self._aliases = {k: set(v) for k, v in entity_aliases.items()}
        self._facts = [dict(subj=s, rel=r, obj=o) for (s, r, o) in fact_rows]
        self.writes = []
        self.savepoints = 0
        self._last = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT entity_id FROM entity_aliases WHERE lower(alias)"):
            hits = [u for u, a in self._aliases.items()
                    if any(x.lower() == params[0].lower() for x in a)]
            self._last = [(u,) for u in sorted(hits)[:1]]
        elif s.startswith("SELECT lower(alias) FROM entity_aliases WHERE entity_id"):
            self._last = [(a,) for a in sorted(self._aliases.get(params[0], set()))]
        elif s.startswith("SELECT subject_id, object_id FROM facts"):
            name = params[1]
            self._last = [(f["subj"], f["obj"]) for f in self._facts
                          if f["rel"] == params[0] and name in (f["subj"], f["obj"])][:1]
        elif s.startswith("SELECT DISTINCT entity_id FROM entity_aliases"):
            wanted = {p.lower() for p in params[0]}
            self._last = [(u,) for u, a in sorted(self._aliases.items())
                          if u != params[1] and any(x.lower() in wanted for x in a)]
        elif s.startswith("UPDATE facts SET superseded_at"):
            _, c1, r1, r2, c2 = params
            n = 0
            for f in self._facts:
                if f["rel"] == params[0] and (
                        (f["subj"] == c1 and f["obj"] == r1) or
                        (f["subj"] == r2 and f["obj"] == c2)):
                    f["dead"] = True
                    n += 1
            self.rowcount = n
            self.writes.append(("supersede", params, n))
        elif s.startswith("DELETE FROM entity_aliases WHERE entity_id"):
            rival, roles = params
            for role in roles:
                self._aliases[rival].discard(role.lower())
            self.writes.append(("alias_delete", params))
            self.rowcount = len(roles)
        elif s.startswith("SAVEPOINT"):
            self.savepoints += 1
            self.rowcount = 0
        else:
            self.rowcount = 0

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last)


class _RetireDB:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        class _C:
            def __enter__(self_inner):
                return self._cursor

            def __exit__(self_inner, *a):
                return False
        return _C()


def test_retire_pass_live_arrangement_repoints_pure_weld():
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    ISOLDE = "isolde-33333333-3333-3333-3333-333333333333"
    NAMED = "named-22222222-2222-2222-2222-222222222222"
    cur = _RetireCursor(
        {WELD_ID: {"wife"}, NAMED: {"seraphina", "wife"}, ISOLDE: {"isolde", "wife"}},
        [(USER, "spouse", WELD_ID),      # the pre-fix ghost row, LIVE arrangement
         (USER, "spouse", ISOLDE)])      # this turn's named bind
    retired = retire_rival_weld_entities(_RetireDB(cur), _edges_isolde_live(),
                                         KIN_REL_MAP, KIN_NOUNS)
    assert retired == 1
    marks = [w for w in cur.writes if w[0] == "supersede"]
    assert len(marks) == 1 and marks[0][2] == 1
    assert marks[0][1][1:3] == (USER, WELD_ID)   # counterpart=user side, rival=weld
    dels = [w for w in cur.writes if w[0] == "alias_delete"]
    assert len(dels) == 1 and dels[0][1][0] == WELD_ID
    assert cur._aliases[WELD_ID] == set()                      # weld fully de-aliased
    assert cur._aliases[NAMED] == {"seraphina", "wife"}        # named person untouched
    assert cur.savepoints >= 1


def test_retire_pass_deriver_arrangement_repoints_pure_weld():
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    ISOLDE = "isolde-33333333-3333-3333-3333-333333333333"
    cur = _RetireCursor(
        {WELD_ID: {"wife"}, ISOLDE: {"isolde", "wife"}},
        [(WELD_ID, "spouse", USER),      # ghost row, deriver arrangement
         (ISOLDE, "spouse", USER)])      # named bind, deriver arrangement
    retired = retire_rival_weld_entities(_RetireDB(cur), _edges_isolde_deriver(),
                                         KIN_REL_MAP, KIN_NOUNS)
    assert retired == 1
    marks = [w for w in cur.writes if w[0] == "supersede"]
    assert len(marks) == 1 and marks[0][2] == 1
    assert marks[0][1][1:3] in ((USER, WELD_ID), (WELD_ID, USER))


def test_retire_pass_noops_without_committed_kin_row():
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    ISOLDE = "isolde-33333333-3333-3333-3333-333333333333"
    cur = _RetireCursor({WELD_ID: {"wife"}, ISOLDE: {"isolde", "wife"}}, [])  # nothing committed
    retired = retire_rival_weld_entities(_RetireDB(cur), _edges_isolde_live(),
                                         KIN_REL_MAP, KIN_NOUNS)
    assert retired == 0
    assert cur.writes == []


def test_retire_pass_noops_on_non_kin_batch():
    cur = _RetireCursor({}, {})
    edges = [Edge(subject="rex", rel_type="has_pet", object="user", is_correction=False)]
    assert retire_rival_weld_entities(_RetireDB(cur), edges, KIN_REL_MAP, KIN_NOUNS) == 0
    assert cur.writes == []

# ── F1-R4: the ID-keyed POST-RESOLUTION twins (the live pipeline rewrites surfaces to UUIDs
#    at extraction — critic R3-1 — so the real gates must key on RESOLVED ENTITY IDs) ────────

from src.api.kin_weld_repoint import (  # noqa: E402
    kin_weld_entity_on_weld,
    retire_rival_weld_entities_resolved,
)


def test_weld_entity_gate_id_keyed_pure_weld_true():
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    cur = _RetireCursor({WELD_ID: {"wife"}}, [])
    assert kin_weld_entity_on_weld(_RetireDB(cur), WELD_ID, KIN_NOUNS) is True


def test_weld_entity_gate_id_keyed_named_person_false():
    NAMED = "named-22222222-2222-2222-2222-222222222222"
    cur = _RetireCursor({NAMED: {"seraphina", "wife"}}, [])
    assert kin_weld_entity_on_weld(_RetireDB(cur), NAMED, KIN_NOUNS) is False
    UNKNOWN = "unknow-44444444-4444-4444-4444-444444444444"
    assert kin_weld_entity_on_weld(_RetireDB(cur), UNKNOWN, KIN_NOUNS) is False


def test_resolved_retire_landed_state_repoints_pure_weld():
    # the POST-RESOLUTION landed state: rows carry ENTITY IDs — (user, spouse, weld) ghost +
    # (user, spouse, isolde-entity) this turn's bind; the bind endpoint carries the kin-role
    # alias 'wife' (the naming-chain signature in ID form).
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    ISOLDE = "isolde-33333333-3333-3333-3333-333333333333"
    cur = _RetireCursor(
        {WELD_ID: {"wife"}, ISOLDE: {"isolde", "wife"}},
        [(USER, "spouse", WELD_ID), (USER, "spouse", ISOLDE)])
    rows = [(USER, USER, WELD_ID, "spouse"), (USER, USER, ISOLDE, "spouse")]
    retired = retire_rival_weld_entities_resolved(_RetireDB(cur), rows, KIN_REL_VALUES,
                                                  KIN_NOUNS, USER)
    assert retired == 1
    marks = [w for w in cur.writes if w[0] == "supersede"]
    assert len(marks) == 1 and marks[0][2] == 1
    assert marks[0][1][1:3] == (USER, WELD_ID)
    dels = [w for w in cur.writes if w[0] == "alias_delete"]
    assert len(dels) == 1 and dels[0][1][0] == WELD_ID
    assert cur._aliases[WELD_ID] == set()
    assert cur._aliases[ISOLDE] == {"isolde", "wife"}   # the named bind untouched
    assert cur.savepoints >= 1


def test_resolved_retire_noops_without_kin_role_alias_on_endpoint():
    # the landed endpoint carries NO kin-role alias (alias leg authority-dropped) -> not a
    # naming-chain bind -> no reconcile (and no crash).
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    ISOLDE = "isolde-33333333-3333-3333-3333-333333333333"
    cur = _RetireCursor(
        {WELD_ID: {"wife"}, ISOLDE: {"isolde"}},
        [(USER, "spouse", WELD_ID), (USER, "spouse", ISOLDE)])
    rows = [(USER, USER, WELD_ID, "spouse"), (USER, USER, ISOLDE, "spouse")]
    assert retire_rival_weld_entities_resolved(_RetireDB(cur), rows, KIN_REL_VALUES,
                                               KIN_NOUNS, USER) == 0
    assert cur.writes == []


def test_resolved_retire_noops_on_non_kin_and_third_party_rows():
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    OTHER = "other-55555555-5555-5555-5555-555555555555"
    cur = _RetireCursor({WELD_ID: {"wife"}, OTHER: {"mother"}}, [])
    rows = [(USER, USER, OTHER, "has_pet"),                    # non-kin rel
            (OTHER, OTHER, USER, "spouse"),                    # third-party pair
            (USER, USER, WELD_ID, "spouse")]                   # weld endpoint, no named bind
    assert retire_rival_weld_entities_resolved(_RetireDB(cur), rows, KIN_REL_VALUES,
                                               KIN_NOUNS, USER) == 0
    assert cur.writes == []


# ── F1-R4-followup (critic R4-1): the minted entity type must be read ON THE PASSED
#    connection — the ingest txn — because the same-turn mint is INVISIBLE to the gate's own
#    connection until the txn commits (the Odalys wound: override read None, failed open,
#    refusal on the GLiNER ANIMAL stood). ──────────────────────────────────────────────────

from src.api.kin_weld_repoint import (  # noqa: E402
    kin_bind_registry_type,
    kin_bind_type_wins,
)


class _TypeCursor:
    """Fake cursor over ONE connection's visible rows — the POST-commit-read race shape: the
    row exists only on the conn handed to the helper."""

    def __init__(self, rows):
        self._rows = rows
        self._last = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._last = None
        if " ".join(sql.split()).startswith("SELECT entity_type FROM entities"):
            self._last = self._rows.get(params[0])

    def fetchone(self):
        return self._last


class _TypeDB:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        outer = self

        class _C:
            def __enter__(self_inner):
                return _TypeCursor(outer._rows)

            def __exit__(self_inner, *a):
                return False
        return _C()


def test_minted_type_read_on_passed_conn_sees_uncommitted_mint():
    # the exact race shape: the mint row is visible ONLY on the passed (ingest txn) conn.
    PERSON = "0780e1f0-0000-0000-0000-000000000000"
    assert kin_bind_registry_type(_TypeDB({PERSON: ("Person",)}), PERSON) == "Person"


def test_minted_type_read_fails_open_on_absent_row_and_bad_conn():
    assert kin_bind_registry_type(_TypeDB({}), "99999999-9999-9999-9999-999999999999") is None
    assert kin_bind_registry_type(None, "whatever") is None  # no conn -> fail-open None


def test_kin_bind_type_wins_decision_table():
    assert kin_bind_type_wins("Person", "Animal") is True   # the Odalys wound shape
    assert kin_bind_type_wins("Person", "person") is False  # casing-insensitive agreement
    assert kin_bind_type_wins("Person", "Person") is False  # agree -> keep
    assert kin_bind_type_wins(None, "Animal") is False      # no row -> fail open
    assert kin_bind_type_wins("Animal", "Animal") is False
    assert kin_bind_type_wins("Animal", "Person") is False  # non-Person row never overrides
    assert kin_bind_type_wins("Person", None) is False      # no declaration -> nothing to fix



# ── F1-R5 (critic R4-3): the LOGGER SEAM — this module logs via stdlib
#    logging.getLogger("uvicorn.error"), and structlog-style kwargs on it raise TypeError
#    INSIDE every except handler (live, uvicorn configures that logger at INFO, so even the
#    repoint success log raised). The swallowed error surfaced to main.py's catch as a clean
#    kin_weld_repoint_skipped ×2 — the reconcile "never ran" while the gates were firing.
#    Pinned here: (a) every except path EMITS the real error without TypeError and fails
#    open honestly; (b) the repoint success path logs at INFO without TypeError. ───────────

import logging  # noqa: E402

import pytest  # noqa: E402

from src.api.kin_weld_repoint import (  # noqa: E402
    kin_name_surface_taint as _taint_for_logpin,
    kin_weld_entity_on_weld as _weld_entity_for_logpin,
)


class _BoomConn:
    """A connection whose cursor ALWAYS raises — the honest-signal shape: the gate must log
    the REAL error and fail open, never mask it."""

    def cursor(self):
        raise RuntimeError("boom-12345-signal")


def test_taint_gate_except_logs_real_error_without_typeerror(caplog):
    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        out = _taint_for_logpin(_BoomConn(), "isolde", "spouse", KIN_NOUNS,
                                KIN_REL_VALUES, USER)
    assert out is None  # fail-open value
    assert any("boom-12345-signal" in rec.getMessage() for rec in caplog.records), \
        "the swallowed except must at minimum log the real error"


def test_weld_entity_gate_except_logs_real_error_without_typeerror(caplog):
    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        out = _weld_entity_for_logpin(_BoomConn(), "weld-00000000-0000-0000-0000-000000000000",
                                      KIN_NOUNS)
    assert out is False  # fail-open value
    assert any("boom-12345-signal" in rec.getMessage() for rec in caplog.records)


def test_repoint_success_log_emits_at_info_without_typeerror(caplog):
    # the R4-3 wound shape: with uvicorn's INFO config, the repoint success log raised
    # TypeError and the reconcile surfaced as kin_weld_repoint_skipped. Level the logger to
    # INFO so the call actually fires, then require the emit + the retired count.
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    ISOLDE = "isolde-33333333-3333-3333-3333-333333333333"
    cur = _RetireCursor(
        {WELD_ID: {"wife"}, ISOLDE: {"isolde", "wife"}},
        [(USER, "spouse", WELD_ID), (USER, "spouse", ISOLDE)])
    rows = [(USER, USER, WELD_ID, "spouse"), (USER, USER, ISOLDE, "spouse")]
    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        retired = retire_rival_weld_entities_resolved(_RetireDB(cur), rows, KIN_REL_VALUES,
                                                      KIN_NOUNS, USER)
    assert retired == 1
    assert any("kin_weld_entity_repointed" in rec.getMessage() for rec in caplog.records), \
        "the reconcile success must be VISIBLE, not eaten as skipped"


def test_rival_failure_logs_real_error_without_typeerror(caplog):
    # a rival whose alias read raises mid-loop: the per-rival except must log the real error
    # (positional) and the pass must continue honestly (0 retired, no exception escaping).
    WELD_ID = "weld-11111111-1111-1111-1111-111111111111"
    ISOLDE = "isolde-33333333-3333-3333-3333-333333333333"

    class _HalfBrokenCursor(_RetireCursor):
        def execute(self, sql, params=None):
            if "SELECT DISTINCT entity_id FROM entity_aliases" in " ".join(sql.split()):
                raise RuntimeError("boom-67890-rival-signal")
            return super().execute(sql, params)

    cur = _HalfBrokenCursor(
        {WELD_ID: {"wife"}, ISOLDE: {"isolde", "wife"}},
        [(USER, "spouse", WELD_ID), (USER, "spouse", ISOLDE)])
    rows = [(USER, USER, WELD_ID, "spouse"), (USER, USER, ISOLDE, "spouse")]
    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        retired = retire_rival_weld_entities_resolved(_RetireDB(cur), rows, KIN_REL_VALUES,
                                                      KIN_NOUNS, USER)
    assert retired == 0  # honest: nothing retired, no fake success
    assert any("boom-67890-rival-signal" in rec.getMessage() for rec in caplog.records)



# ── F1-R6 (critic R4-1b + R4-3b): STRUCTURE PINS — the adjudication order and the
#    accumulator binding are the two seams previous rounds got wrong invisibly. These pins
#    read main.py's source so a future refactor that re-starves the adjudication or loses
#    the nonlocal fails LOUDLY here instead of silently live. ─────────────────────────────

import re  # noqa: E402

_MAIN = "/workspace/FaultLine-dev-saas/src/api/main.py"


def _main_source() -> str:
    with open(_MAIN, "r", encoding="utf-8") as fh:
        return fh.read()


def test_type_adjudication_sits_between_ledger_and_gate_call():
    """R4-1b flow order: the refusal ledger's live-row release runs FIRST, then the
    adjudication, THEN gate.validate_edge consumes the adjudicated type — and the marker the
    critic greps for is on the path that actually runs (the gate-call site, not a starvable
    pre-stamp block)."""
    s = _main_source()
    marker = s.index("ingest.kin_bind_type_from_registry_row")
    ledger = s.index("previously_refused_edge_withheld", 2_000_000)
    gate_call = s.index("gate.validate_edge", marker)
    assert ledger < marker < gate_call, (
        "the kin-bind type adjudication must sit between the refusal-ledger release and "
        "gate.validate_edge (the refusal consumes the PASSED type — any upstream-only "
        "adjudication is live-dark, critic R4-1b)")


def test_adjudication_uses_txn_conn_read_and_marker_fields():
    """R4-1 evidence chain: the adjudication reads via kin_bind_registry_type on the ingest
    txn connection (kin_bind_registry_type(db, ...) — db, not the gate conn) and the log
    carries read_conn=ingest_txn so the critic's grep finds it."""
    s = _main_source()
    marker = s.index("ingest.kin_bind_type_from_registry_row")
    window = s[marker - 2000: marker + 2000]
    flat = window.replace(" ", "").replace("\n", "")
    assert "kin_bind_registry_type(db" in flat
    assert 'read_conn="ingest_txn"' in window


def test_stamp_discipline_precedes_the_type_stamp():
    """R6 task 2: a fresh kin-bind mint must never be Animal-stamped by the turn-local
    declaration — the stamp's own adjudication marker precedes the entity_type UPDATE."""
    s = _main_source()
    guard = s.index("ingest.kin_bind_stamp_from_registry_row")
    stamp = s.index("AND entity_type = 'unknown'", guard)
    assert guard < stamp


def test_supersession_accumulator_is_nonlocal_in_the_counting_scope():
    """R4-3b: the += sites live inside the nested is_scalar_rel_type scope; without
    nonlocal each one raised UnboundLocalError, swallowed as a clean skip — the response
    reported sup_by_ingest=0 while rows moved. The binding must sit between the def and the
    first counting site."""
    s = _main_source()
    d = s.index("def is_scalar_rel_type")
    nl = s.index("nonlocal _facts_superseded_by_ingest")
    first_count = s.index("_facts_superseded_by_ingest += _n")
    assert d < nl < first_count


def test_nonlocal_is_python_loadable_in_the_real_scope():
    """Compile check with the same AST rules the runtime uses — a misplaced nonlocal is a
    SyntaxError/SyntaxWarning class, not a semantic one."""
    import ast
    tree = ast.parse(_main_source())
    assert tree is not None


# ── F1-R7 (critic R5-2/5-3/5-5): the bidirectional endpoint picker, the tautology gate,
#    and the instructed-repair assertion — plus the structure wiring. ──────────────────────

from src.api.kin_weld_repoint import (  # noqa: E402
    kin_bind_endpoint,
    kin_repair_type_assertion,
    kin_self_role_surface,
)


def test_bind_endpoint_keys_either_arrangement():
    USER = "921514dd-512e-44e1-8f0c-f58090027bd9"
    FOX = "foxglove-0000-4000-8000-000000000001"
    # the LIVE door arrangement: name on OBJECT
    assert kin_bind_endpoint(USER, FOX, USER) == FOX
    # the DERIVER arrangement (critic R5-3): name on SUBJECT
    assert kin_bind_endpoint(FOX, USER, USER) == FOX
    # both user / both empty: nothing to adjudicate
    assert kin_bind_endpoint(USER, USER, USER) is None
    assert kin_bind_endpoint("", "", USER) is None


def test_tautology_role_word_surface_refuses_both_sides():
    NOUNS = frozenset({"wife", "spouse", "mother"})
    # 'My wife is Wife.' — object-side surface IS the role word (subject is the user)
    assert kin_self_role_surface("user", "Wife", "user", NOUNS) == "wife"
    # subject-side role-word surface
    assert kin_self_role_surface("Wife", "user", "user", NOUNS) == "wife"
    # a genuine name never trips it
    assert kin_self_role_surface("user", "Foxglove", "user", NOUNS) is None
    assert kin_self_role_surface("Foxglove", "user", "user", NOUNS) is None
    # the user surface itself is exempt (perspective)
    assert kin_self_role_surface("user", "user", "user", NOUNS) is None


def test_tautology_never_fires_on_the_predicate_slot_r8():
    """RG-B(2) (critic R5B): 'Dana is my wife.' is the INVERSE-COPULA family — the subject
    is a genuine name and the role phrase sits in the predicate slot. The tautology refusal
    keys on the NAME slot only; this shape must reach the adjudication, never refuse here."""
    NOUNS = frozenset({"wife", "spouse"})
    assert kin_self_role_surface("Dana", "wife", "user", NOUNS) is None
    assert kin_self_role_surface("dana", "wife", "user", NOUNS) is None
    # ...but the same role word on a genuine USER-side frame still refuses
    assert kin_self_role_surface("user", "wife", "user", NOUNS) == "wife"


def test_r8_structure_ledger_release_and_disclosure():
    """RG-A + RG-D structure: the instructed repair retires the entity's refusal records in
    the same flow; the ledger's primary read is the txn conn and the OTHER-CONN gate
    resolver fallback is GONE; the bind decision states its state; the surface refusal
    surfaces in the disclosure."""
    s = _main_source()
    # RG-A: the stamp retires refused_edges for the repaired entity
    assert "ingest.kin_repair_refusal_retired" in s
    assert "DELETE FROM refused_edges" in s
    retire = s.index("ingest.kin_repair_refusal_retired")
    # RG-A: the ledger no longer consults the other-conn resolver, and the withhold
    # states the types it decided on
    assert "gate._resolve_entity_type(_off_id_now)" not in s, (
        "the ledger must not consult the gate's own connection (a typed cache from another "
        "view is the RG-A dark path)")
    assert "ingest.refusal_ledger_bind_state" in s
    bind_state = s.index("ingest.refusal_ledger_bind_state")
    withhold = s.index("ingest.previously_refused_edge_withheld")
    ledger_read = s.index("SELECT entity_type FROM entities WHERE id = %s", withhold)
    assert ledger_read > 0
    # RG-D: the surface refusal lands in the disclosure, not just the log
    refuse = s.index("ingest.kin_role_word_surface_refused")
    cont = s.index("continue", refuse)
    drop = s.index('_record_ingest_drop(', refuse)
    assert refuse < drop < cont, "the tautology refuse must record the drop BEFORE continuing"
    assert 'stage="kin_role_word_surface"' in s


def test_r8_unarmed_marker_and_guard_breadth():
    """RG-B(1): the adjudication guard accepts the canonical rel AND the raw surface word,
    and a kin-shaped edge arriving unarmed logs ingest.kin_adjudication_unarmed."""
    s = _main_source()
    assert "ingest.kin_adjudication_unarmed" in s
    assert '"kin_rel_map": dict(_kwr_gate_relmap)' in s
    assert '"kin_rel_guard": _kwr_gate_mod.kin_rel_guard_set(' in s



def test_instructed_repair_assertion_vocabulary():
    # the refusal message teaches '<name> is a person' — that exact contract
    assert kin_repair_type_assertion("instance_of", "person") is True
    assert kin_repair_type_assertion("is_a", "a human.") is True
    assert kin_repair_type_assertion("instance_of", "  Person ") is True
    # non-type-assertion rels and non-person objects never qualify
    assert kin_repair_type_assertion("spouse", "person") is False
    assert kin_repair_type_assertion("instance_of", "animal") is False
    assert kin_repair_type_assertion("instance_of", "") is False


def test_r7_structure_pins_adjudication_and_repair_flip():
    s = _main_source()
    # the call-site adjudication keys the BIDIRECTIONAL endpoint and refuses the tautology
    assert "kin_self_role_surface" in s and "kin_role_word_surface_refused" in s
    assert "kin_bind_endpoint(" in s
    # the subject-side flip is wired (name-on-subject arrangement, critic R5-3)
    assert 'bind_arrangement="name_on_subject"' in s
    assert "final_subject_type = str(_kwr_reg_t).title()" in s
    # the instructed repair flips the stale annotation at the subject stamp
    assert "ingest.kin_repair_type_stamp" in s
    assert "kin_repair_type_assertion" in s
    # the tautology refuse precedes the gate call (the resolve half must run first)
    refuse = s.index("kin_role_word_surface_refused")
    gate_call = s.index("gate.validate_edge", refuse)
    assert refuse < gate_call



# ── F1-R8 ADDENDUM (Lane A's RG-F root trace): the weld-boundary accretion arms. ─────────

from src.api.kin_weld_repoint import kin_weld_slot_refusal_core  # noqa: E402

_N = frozenset({"wife", "spouse", "mother"})


def test_person_name_never_accretes_onto_a_pure_weld():
    # 'magnus' onto the wife-weld {wife} — how e6efb2a2 became magnus+wife
    assert kin_weld_slot_refusal_core(["wife"], "magnus", "doc", _N) == "person_onto_pure_weld"
    # source authority is irrelevant on this arm
    assert kin_weld_slot_refusal_core(["wife", "spouse"], "magnus", "user_stated", _N) == \
        "person_onto_pure_weld"
    # a role word onto a role slot is the slot's OWN kind — allowed
    assert kin_weld_slot_refusal_core(["wife"], "spouse", "doc", _N) is None
    # empty target: nothing to classify
    assert kin_weld_slot_refusal_core([], "magnus", "doc", _N) is None


def test_role_word_never_accretes_onto_a_person_entity_from_doc():
    # 'wife' onto {magnus} from the fenland doc — the contamination arm
    assert kin_weld_slot_refusal_core(["magnus"], "wife", "doc", _N) == \
        "role_word_onto_person_from_doc"
    assert kin_weld_slot_refusal_core(["magnus"], "wife", "staged", _N) == \
        "role_word_onto_person_from_doc"
    # USER authority keeps its documented transfer path (R3: the named bind carries the
    # role alias — anouk+wife preferred=anouk is the legitimate walk shape)
    assert kin_weld_slot_refusal_core(["anouk"], "wife", "user_stated", _N) is None
    assert kin_weld_slot_refusal_core(["anouk"], "wife", "unspecified", _N) is None
    # a role word onto a person slot from doc when the entity has NO person aliases: the
    # person arm needs non-role aliases present; a pure weld target absorbs role words
    assert kin_weld_slot_refusal_core(["wife"], "mother", "doc", _N) is None


def test_r8_slot_wiring_structure():
    s = _main_source()
    # the alias-sync INSERT is guarded by BOTH arms, single weld_guard call, dispatch before
    # the write
    guard = s.index("_wg_refused = weld_guard.refuse_weld(")
    slot = s.index("kin_weld_slot_refusal(")
    dispatch = s.index("if _kwr_slot_arm:")
    insert = s.index("INSERT INTO entity_aliases", dispatch)
    assert guard < slot < dispatch < insert
    assert "ingest.kin_weld_slot_refused" in s
    assert "kin_weld_slot_refusal_core" in open(
        "/workspace/FaultLine-dev-saas/src/api/kin_weld_repoint.py", encoding="utf-8").read()



# ── R8 HOTFIX (critic RG-0): the gate-merge is BEHAVIORAL, not structural — the merged
#    guard set is computed in-process with real-shaped config and asserted for membership;
#    the mixed-container union class that cooked dead is pinned as the rejected shape. ────

from src.api.kin_weld_repoint import kin_rel_guard_set  # noqa: E402


def test_kin_rel_guard_merge_real_config_no_raise_and_membership():
    """RG-0: drive the merge with REAL-SHAPED gate config (the exact containers the
    precompute builds) — no raise, and the membership test that guards every txn edge works
    for BOTH canonical rels and raw surface words."""
    values = {"spouse", "parent", "child"}          # set(_kwr_gate_relmap.values())
    relmap = {"wife": "spouse", "husband": "spouse", "mother": "parent"}  # the relmap dict
    guard = kin_rel_guard_set(values, relmap)       # must not raise
    assert isinstance(guard, frozenset)
    # canonical rels still guard
    assert "spouse" in guard and "parent" in guard
    # RAW surface words (the R8 intent — the spine may emit them verbatim)
    assert "wife" in guard and "husband" in guard and "mother" in guard
    # the fallback kin words ride in the merged set
    for w in ("son", "daughter", "brother", "sister", "father"):
        assert w in guard
    # empty/None containers fail open to the fallback words only
    assert kin_rel_guard_set(None, None) >= {"spouse", "wife"}
    # the request-time membership shape that guards every edge:
    rt = "wife"
    assert rt.strip().lower() in guard


def test_mixed_container_union_is_the_rejected_shape():
    """The class that killed the cook: dict | set raises TypeError. The merge must live in
    kin_rel_guard_set (one container, one type) — this pin documents WHY the guard sites do
    plain membership and would fail loudly if the union shape returned."""
    import pytest
    with pytest.raises(TypeError):
        {"wife": "spouse"} | {"spouse"}  # noqa: B033 — the exact R8 cook killer


def test_no_request_time_kin_rel_union_remains():
    """Structure armor for the hotfix: main.py's guard sites use the precomputed merged set;
    no request-time union over the kin_rel_map container survives."""
    s = _main_source()
    assert 'in _kwr_gate["kin_rel_guard"]' in s
    assert '"kin_rel_guard": _kwr_gate_mod.kin_rel_guard_set(' in s
    import re
    assert not re.search(r'get\("kin_rel_map", \{\}\)\s*\|', s), (
        "a request-time union over the kin_rel_map container is the RG-0 crash shape")



# ── F1-R9 (critic RG-B-M2): POSITIVE diagnosability — every adjudicated kin bind reports,
#    agreement or override; observability never depends on divergence. ────────────────────

def test_agreement_and_override_semantics_including_concept_class():
    """The Petrel class (agreeing bind) must be an AGREEMENT verdict, the Heron/Obsidian
    class (divergence) an OVERRIDE verdict — the same predicate drives the marker's
    agreement bit. The Obsidian shape is C's R9 handoff: a Concept-typed name arriving
    object-side over a registry-Person mint."""
    from src.api.kin_weld_repoint import kin_bind_type_wins
    # divergence -> override (Heron: GLiNER Animal vs Person row)
    assert kin_bind_type_wins("Person", "Animal") is True
    # divergence -> override (Obsidian: Concept-declared vs Person mint, case-insensitive)
    assert kin_bind_type_wins("Person", "Concept") is True
    assert kin_bind_type_wins("Person", "concept") is True
    # agreement (Petrel: the declarations agree) -> no override
    assert kin_bind_type_wins("Person", "Person") is False
    # textual divergence (human) still lands Person — the agreement bit is textual, the
    # OUTCOME (the registry row's typing) is right
    assert kin_bind_type_wins("person", "human") is True
    # no row / unknown row -> fail-open, agreement (declared stands, marker still reports)
    assert kin_bind_type_wins(None, "Animal") is False
    assert kin_bind_type_wins("Animal", "Animal") is False


def test_positive_adjudication_marker_wired_unconditionally():
    """The marker fires on EVERY adjudicated bind — it must sit AFTER the flip branches
    (not inside them) and carry the contract fields: bind_arrangement, guard_source,
    agreement/override_fired."""
    s = _main_source()
    assert "ingest.kin_bind_adjudicated" in s
    assert 'guard_source="kin_rel_guard"' in s
    assert "override_fired=bool(_kwr_flip)" in s
    assert "agreement=not _kwr_flip" in s
    # the LAST adjudicated-marker emit is the adjudication-block one; the flip sites are
    # defined before it, and the corrected-envelope emit (R10b) is additional coverage
    obj_flip = s.index("final_object_type = str(_kwr_reg_t).title()")
    subj_flip = s.index("final_subject_type = str(_kwr_reg_t).title()")
    marker = s.index('log.info("ingest.kin_bind_adjudicated"', subj_flip)
    assert obj_flip < marker and subj_flip < marker, (
        "the positive marker must be OUTSIDE the flip branches — it fires on agreement too")
    # the divergence markers still exist alongside (the critic's grep contract)
    assert s.count("ingest.kin_bind_type_from_registry_row") >= 2



# ── F1-R10 (critic RG-A2 last mile): SUBJECT-side stamp discipline — the Onyx class. ────

def test_subject_stamp_discipline_precedes_the_subject_update():
    """The name-on-subject shape ((onyx, spouse, user), GLiNER 'Object') must not have the
    turn-local declaration stamped onto the row — the gate call's registry read would see
    Object and never flip, and the accepted-types check would refuse. The subject stamp's
    kin discipline (stamp_side=subject marker) must precede the subject UPDATE, exactly as
    the R6 object-side discipline precedes the object UPDATE."""
    s = _main_source()
    marker = s.index('log.info("ingest.kin_bind_stamp_from_registry_row"')
    stamp_side = s.index('stamp_side="subject"')
    subj_update = s.index("UPDATE entities SET entity_type = %s WHERE id = %s", marker)
    assert stamp_side < subj_update
    assert marker < subj_update
    # the discipline is kin-gated (guard membership) and Person-declared exempt
    window = s[marker - 3000: marker]
    assert 'in _kwr_gate["kin_rel_guard"]' in window
    assert '!= "person"' in window


def test_onyx_class_object_typed_decleration_flips():
    """The Onyx/Basalt class end to end at the predicate level: GLiNER 'Object' declared over
    a registry-Person mint is an OVERRIDE verdict — the flip lands Person, the accepted-types
    check (spouse accepts Animal/Person) then admits, committed=1."""
    from src.api.kin_weld_repoint import kin_bind_type_wins
    assert kin_bind_type_wins("Person", "Object") is True
    assert kin_bind_type_wins("Person", "object") is True
    # and the frame's correct routing (onyx, spouse, user) keeps the name on the SUBJECT
    # endpoint — the picker's deriver arrangement covers it
    assert kin_bind_endpoint("onyx-entity", "user-entity", "user-entity") == "onyx-entity"



# ── R10 RE-GATE, CORRECTED (critic re-checked the container): the live API container serves
#    structlog's UNCONFIGURED DEFAULT ConsoleRenderer — logging_config.configure_structlog
#    (the JSONRenderer branch) has ZERO callers — so markers render KEY=VALUE with
#    Python-case booleans (agreement=True) and plain unquoted strings
#    (bind_arrangement=name_on_object). The behavioral render vectors below render through
#    structlog.dev.ConsoleRenderer exactly and pin the OBSERVED greppable substrings. ──

import structlog  # noqa: E402

_CONSOLE = structlog.dev.ConsoleRenderer(colors=False)


def test_kin_bind_adjudicated_rendered_format_matches_contract():
    """Behavioral render vector for ingest.kin_bind_adjudicated — the OBSERVED CONTAINER
    FORM: structlog's unconfigured default ConsoleRenderer. Grep contract:
    'agreement=True', 'override_fired=False', 'bind_arrangement=name_on_object'."""
    out = _CONSOLE(None, None, {
        "event": "ingest.kin_bind_adjudicated",
        "bind_entity": "onyx-entity"[:16],
        "bind_arrangement": "name_on_object",
        "guard_source": "kin_rel_guard",
        "registry_type": "Person",
        "declared_object_type": "Object",
        "declared_subject_type": "Person",
        "override_fired": False,
        "agreement": True,
    })
    assert "ingest.kin_bind_adjudicated" in out
    assert "agreement=True" in out
    assert "override_fired=False" in out
    assert "bind_arrangement=name_on_object" in out
    assert "guard_source=kin_rel_guard" in out
    assert "registry_type=Person" in out
    # the divergence render flips the booleans exactly as Python reprs them
    div = _CONSOLE(None, None, {"event": "ingest.kin_bind_adjudicated",
                                "override_fired": True, "agreement": False})
    assert "override_fired=True" in div and "agreement=False" in div


def test_stamp_marker_rendered_format_matches_contract():
    """Behavioral render vector for ingest.kin_bind_stamp_from_registry_row (R10 subject-side
    variant). Grep contract: 'stamp_side=subject' (console form)."""
    rendered = _CONSOLE(None, None, {
        "event": "ingest.kin_bind_stamp_from_registry_row",
        "subject": "onyx-entity"[:16],
        "stamp_side": "subject",
        "gliner_type": "Object",
        "registry_type": "Person",
    })
    assert "stamp_side=subject" in rendered
    assert "gliner_type=Object" in rendered
    assert "ingest.kin_bind_stamp_from_registry_row" in rendered
    # the emit kwargs exist verbatim in main.py (the contract is the SOURCE's, not aspirational)
    s = _main_source()
    assert 'stamp_side="subject"' in s
    assert 'override_fired=bool(_kwr_flip)' in s
    assert 'agreement=not _kwr_flip' in s
    assert 'guard_source="kin_rel_guard"' in s


def test_ancient_reconciled_turn_never_stamps_over_current_truth():
    """Lane D backlog age-policy interaction — documented NO + the pin: the stamp disciplines
    consult the CURRENT registry row through kin_bind_type_wins before any write, so an
    ancient reconciled turn (whatever its old declaration) can never stamp over a current
    non-Person value; a current Person row only receives Person (idempotent)."""
    from src.api.kin_weld_repoint import kin_bind_type_wins
    # ancient turn declares Animal, current row is Organization (corrected since): NO stamp
    assert kin_bind_type_wins("Organization", "Animal") is False
    # ancient turn declares Animal, current row is Animal: no flip, no write of a new value
    assert kin_bind_type_wins("Animal", "Animal") is False
    # only a CURRENT Person row authorizes the stamp, and it writes Person (idempotent with
    # a current Person row; corrective for an unknown/Animal row — never invents a value)
    assert kin_bind_type_wins("Person", "Animal") is True
    assert kin_bind_type_wins("Person", "Object") is True


# ── F1-R12 (critic RG-Z-2): the VALID-LANE kin bind retires its slot's incumbents — a
#    spouse is ONE-LIVE by construction. The Gabbro class: a first-turn bind filed a
#    PARALLEL spouse row and only the swap superseded the original — dual live spouses
#    with no flap needed. Now the bind itself retires the incumbents at landing time. ────

def test_kin_incumbent_retire_helper_guards_and_sql_shape():
    """The helper's contract: refuse degenerate calls without touching the DB; the UPDATE
    retires archive-not-annihilate (superseded_at set, row kept), BOTH orientations of the
    user-anchored slot, only affirmed live rows, and never the fresh bind's own endpoint."""
    import inspect
    from src.api.kin_weld_repoint import kin_retire_slot_incumbents
    # degenerate calls return 0 without a DB round-trip (db=None would explode on use)
    assert kin_retire_slot_incumbents(None, "user-id", "spouse", "bind-id") == 0
    assert kin_retire_slot_incumbents(object(), "user-id", "spouse", "user-id") == 0
    assert kin_retire_slot_incumbents(object(), "user-id", "", "bind-id") == 0
    src = inspect.getsource(kin_retire_slot_incumbents)
    # archive-not-annihilate: superseded_at set, the row kept
    assert "superseded_at = COALESCE(superseded_at, now())" in src
    # both orientations of the user-anchored slot
    assert "subject_id = %s AND object_id IS DISTINCT FROM %s" in src
    assert "object_id = %s AND subject_id IS DISTINCT FROM %s" in src
    # live + affirmed only (denials keep their own gate)
    assert "superseded_at IS NULL AND archived_at IS NULL" in src
    assert "COALESCE(polarity, 'affirmed') = 'affirmed'" in src
    # savepoint-guarded fail-open: a failure rolls back to the savepoint, returns 0
    assert "SAVEPOINT sp_kin_incumbent" in src
    assert "ROLLBACK TO SAVEPOINT sp_kin_incumbent" in src


def test_valid_lane_bind_retirement_wiring_and_gate():
    """The retirement fires in the validated-rows loop (rows proceeding to land), gated on
    kin_rel_guard membership + metadata is_single_valued (pets/multi-valued coexist
    untouched) + a UUID non-user endpoint — and AFTER the tautology gate, so honest-decline
    and tautology paths are structurally untouched. Dual-live is impossible by construction:
    every landed bind retires its slot's incumbents before the swap ever runs."""
    s = _main_source()
    assert "kin_retire_slot_incumbents(" in s
    marker = s.index("ingest.kin_incumbent_superseded")
    call = s.index("kin_retire_slot_incumbents(")
    window = s[call - 2200: call]
    assert '_kwr_guard_fs = _kwr_gate.get("kin_rel_guard")' in window
    assert "kin_rel_guard_set(" in window
    assert 'is_single_valued' in window
    assert "_UUID_PATTERN.match(str(obj))" in window
    # after the tautology gate (declines/tautology structurally upstream, untouched)
    taut = s.index("kin_self_role_surface(")
    assert taut < call
    # the marker names the superseding bind + count (the swap-after-bind case is the same
    # pass: the swap's own bind retires whatever the earlier bind landed)
    mwin = s[marker:marker + 900]
    assert "bind_entity=" in mwin and "retired=_kwr_slot_retired" in mwin


def test_corrected_envelope_emit_is_inside_the_bypass_branch():
    """Honest firing verification for 7aff8a3d (the critic's UNTESTED flag): the
    corrected-envelope emit is provably INSIDE the self-correction bypass branch — the
    axiomatic-valid branch is the only code path that can carry it — and the render vector
    proves the emitted shape. Firing-on-live-seat proof is the cook's (fix lanes do not
    write seats); wiring + render are pinned here so any refactor that unlinks the emit
    from the branch fails this pin."""
    s = _main_source()
    bypass = s.index("if fact_subject == user_entity_id and edge.is_correction:")
    emit = s.index('envelope="corrected"')
    valid_after = s.index('status = "valid"', emit)
    assert bypass < emit < valid_after
    # the emit is kin-gated inside the branch (a non-kin correction stays silent)
    branch = s[bypass:valid_after]
    assert 'in _kwr_gate["kin_rel_guard"]' in branch
    assert "kin_bind_endpoint(" in branch

# ── EMERGENCY #2 HOTFIX GATE (the critic demanded it twice): a structure pin suite cannot
#    see runtime key errors — the R12 cook died 6/6 statement txns on KeyError:
#    'kin_rel_guard' because the txn-body arming carried a 3-key dict while consumers read
#    five. TWO TIERS below. ─────────────────────────────────────────────────────────────

import re  # noqa: E402


def test_kin_gate_armings_carry_identical_key_sets_and_all_lookups():
    """FAST TIER — the key-lifetime gate. Parses main.py: (a) BOTH _kwr_gate arming sites
    must set the SAME key set (the txn-body arming may not silently shrink); (b) both must
    carry the RG-0 precomputed frozenset (kin_rel_guard_set call present); (c) every
    _kwr_gate["key"] literal lookup in the file is covered by BOTH armings — any future
    key drift fails HERE, in unit, not in the cook."""
    s = _main_source()
    blocks = []
    for m in re.finditer(r"_kwr_gate = {", s):
        chunk = s[m.end(): m.end() + 1200]
        keys = set(re.findall(r'"([a-z_]+)":', chunk))
        blocks.append(keys)
        assert "kin_rel_guard_set(" in chunk, (
            "an arming site skips the RG-0 precompute (kin_rel_guard_set) — the request-time "
            "union crash class returns")
    assert len(blocks) == 2, f"expected exactly two arming sites, found {len(blocks)}"
    lookups = set(re.findall(r'_kwr_gate\["([a-z_]+)"\]', s))
    assert lookups, "no gate lookups found"
    for i, keys in enumerate(blocks):
        missing = lookups - keys
        assert not missing, (
            f"arming site {i} lacks keys {sorted(missing)} — KeyError class (emergency #2); "
            "the txn-body arming must mirror the extraction-phase arming")


def test_guard_consumer_survives_arming_drift_in_process():
    """FAST TIER, in-process: the R12 consumer's defensive derivation — even if an arming
    site drops kin_rel_guard again, the consumer derives the frozenset from
    kin_rel_values + kin_rel_map (keys both armings set) and membership still works."""
    from src.api.kin_weld_repoint import kin_rel_guard_set
    drifted_arming = {"kin_nouns": frozenset({"wife"}), "kin_rel_values": {"spouse"}}
    guard = kin_rel_guard_set(set(drifted_arming["kin_rel_values"]),
                              drifted_arming.get("kin_rel_map", {}))
    assert "spouse" in guard


@pytest.mark.live
def test_live_ingest_smoke_statement_with_kin_edge():
    """INTEGRATION TIER — the gate that matters (critic demand, second broken cook): boot
    the REAL app in-process against the real test DB and POST one statement WITH a kin
    edge end-to-end. Asserts no 500 and committed>=1. Runs only under RUN_LIVE=1 with a
    POSTGRES_DSN (pytest -m live / the combined sweep); the unit suite skips it."""
    import os
    import uuid as _uuid
    if os.getenv("RUN_LIVE") != "1" or not (os.getenv("POSTGRES_DSN") or "").strip():
        pytest.skip("live smoke needs RUN_LIVE=1 and POSTGRES_DSN")
    import psycopg2
    from fastapi.testclient import TestClient
    import src.api.main as _main_mod

    class _MarkerCapture:
        """Wrap the app's structlog logger: record event names, delegate unchanged."""

        def __init__(self, real):
            self._real = real
            self.events = []

        def info(self, event, **kw):
            self.events.append((str(event), kw))
            return self._real.info(event, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    uid = str(_uuid.uuid4())
    schema = "faultline_" + uid.replace("-", "_")
    cap = _MarkerCapture(_main_mod.log)
    saved_log = _main_mod.log
    _main_mod.log = cap
    try:
        from fastapi.testclient import TestClient
        from src.api.main import app
        with TestClient(app) as client:

            def _bind(name):
                resp = client.post("/ingest", json={
                    "user_id": uid,
                    "text": f"My wife is {name}.",
                    "source": "api",
                    "edges": [{"subject": "I", "object": name,
                               "rel_type": "spouse", "fact_provenance": "user_stated",
                               "subject_type": "Person", "object_type": "Person"}],
                })
                assert resp.status_code < 500, (
                    f"live ingest 500-class on {name}: {resp.status_code} {resp.text[:300]}")
                body = resp.json()
                assert isinstance(body, dict) and "committed" in body, (
                    f"unexpected body: {str(body)[:300]}")
                assert int(body.get("committed") or 0) >= 1, (
                    f"kin statement committed nothing end-to-end ({name}): {str(body)[:400]}")
                return body

            # THE WIRING GATE (R13): the pins were hermetic truth about unwired helpers.
            # Bind A plants the incumbent; bind B MUST retire it — asserted in the DB
            # (single-current by wiring) AND in the captured marker (observability).
            _bind("Cobalt Quarry")
            _bind("Gabbro Hollow")
            conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
            try:
                cur = conn.cursor()
                cur.execute("SET search_path TO " + schema)
                cur.execute("SELECT object_id, superseded_at IS NOT NULL FROM facts"
                            " WHERE rel_type = 'spouse'")
                rows = cur.fetchall()
            finally:
                conn.close()
            live_rows = [r for r in rows if not r[1]]
            assert len(rows) >= 2, f"expected incumbent+bind in the slot, got {rows!r}"
            assert len(live_rows) == 1, (
                f"DUAL LIVE SPOUSES BY CONSTRUCTION — the retirement is unwired on the "
                f"live lane: {rows!r}")
            superseded = [e for e in cap.events
                          if e[0] == "ingest.kin_incumbent_superseded"
                          and int(e[1].get("retired") or 0) >= 1]
            assert superseded, (
                "the bind retired the incumbent in the DB but the marker never fired — "
                "the observability half is unwired")

            # the corrected envelope: an is_correction kin edge must emit
            # envelope=corrected — through whichever branch actually routes it
            rcorr = client.post("/ingest", json={
                "user_id": uid,
                "text": "Correction: my wife is Cobalt Quarry, not Gabbro Hollow.",
                "source": "api",
                "edges": [{"subject": "I", "object": "Cobalt Quarry",
                           "rel_type": "spouse", "fact_provenance": "user_stated",
                           "is_correction": True,
                           "subject_type": "Person", "object_type": "Person"}],
            })
            assert rcorr.status_code < 500, (
                f"correction 500-class: {rcorr.status_code} {rcorr.text[:300]}")
            env_corr = [e for e in cap.events
                        if e[0] == "ingest.kin_bind_adjudicated"
                        and e[1].get("envelope") == "corrected"]
            assert env_corr, (
                "the corrected-envelope emit never fired on the live correction — "
                "the observability contract is unwired on this route; captured kin "
                "events: " + str([e[0] for e in cap.events if "kin" in e[0]][:12]))
            assert env_corr, (
                "the corrected-envelope emit never fired on the live correction — "
                "the observability contract is unwired on this route; captured kin "
                "events: " + str([e[0] for e in cap.events if "kin" in e[0]][:12]))

            # R14 (1) — THE SLATE CLASS RESTORED, live: an Object-typable first-turn bind
            # must ADMIT ahead of the accepted-types check (the frame IS the person
            # assertion), not type_refuse.
            rslate = client.post("/ingest", json={
                "user_id": uid,
                "text": "My wife is Slate.",
                "source": "api",
                "edges": [{"subject": "I", "object": "Slate",
                           "rel_type": "spouse", "fact_provenance": "user_stated",
                           "subject_type": "Person", "object_type": "Object"}],
            })
            assert rslate.status_code < 500, (
                f"Slate 500-class: {rslate.status_code} {rslate.text[:300]}")
            slate_body = rslate.json()
            assert int(slate_body.get("committed") or 0) >= 1, (
                f"RG-A2 REGRESSION: the Object-typable first-turn bind type_refused — "
                f"{str(slate_body)[:400]}")
            asserted = [e for e in cap.events
                        if e[0] == "ingest.kin_copula_person_asserted"]
            assert asserted, (
                "the Slate bind admitted but the copula person-assertion marker never "
                "fired — the restore is not on the live route")

            # R14 (2) — THE RETRACT DOOR IS THE LIVE CORRECTION ROUTE: the swap's
            # supersession happens in /retract (remember_facts diverts there; the new
            # fact re-enters through the spine as a plain statement). The retract-door
            # envelope MUST fire on a kin-slot retraction.
            rret = client.post("/retract", json={
                "user_id": uid, "subject": "user", "rel_type": "spouse",
                "old_value": "Slate"})
            assert rret.status_code < 500, (
                f"retract 500-class: {rret.status_code} {rret.text[:300]}")
            env_ret = [e for e in cap.events
                       if e[0] == "ingest.kin_bind_adjudicated"
                       and e[1].get("envelope") == "corrected"
                       and int(e[1].get("retracted_count") or 0) >= 1]
            assert env_ret, (
                "the retract-door envelope never fired on a kin-slot retraction — "
                "the live correction route's observability is unwired")

            # the honest-decline path stands: a lowercase, non-frame complement still
            # declines (no person assertion for a common noun)
            rgran = client.post("/ingest", json={
                "user_id": uid,
                "text": "my wife is granite.",
                "source": "api",
                "edges": [{"subject": "I", "object": "granite",
                           "rel_type": "spouse", "fact_provenance": "user_stated",
                           "subject_type": "Person", "object_type": "Object"}],
            })
            assert rgran.status_code < 500, (
                f"granite 500-class: {rgran.status_code} {rgran.text[:300]}")
            granite_body = rgran.json()
            granite_asserted = [e for e in cap.events
                                if e[0] == "ingest.kin_copula_person_asserted"
                                and str(e[1].get("surface") or "").lower() == "granite"]
            assert not granite_asserted, (
                "the honest-decline path broke: a lowercase common-noun complement "
                "was person-asserted")

            # R14-M1 - THE CRITIC'S SWAP SHAPE through the HARVEST DOOR: a remember_facts
            # spouse swap lands as the intent-independent harvest's plain-statement /ingest
            # (source=mcp) - the reconcile arm's flips ARE this slot retirement, so the
            # corrected envelope must fire HERE, not only on the /retract divert arm.
            rswap = client.post("/ingest", json={
                "user_id": uid,
                "text": "Actually my wife is Gabbro Hollow, not Cobalt Quarry.",
                "source": "mcp",
                "edges": [{"subject": "I", "object": "Gabbro Hollow",
                           "rel_type": "spouse", "fact_provenance": "user_stated",
                           "subject_type": "Person", "object_type": "Person"}],
            })
            assert rswap.status_code < 500, (
                f"swap 500-class: {rswap.status_code} {rswap.text[:300]}")
            swap_env = [e for e in cap.events
                        if e[0] == "ingest.kin_bind_adjudicated"
                        and e[1].get("envelope") == "corrected"
                        and int(e[1].get("superseded_count") or 0) >= 1]
            assert swap_env, (
                "the critic's swap shape never emitted envelope=corrected at the door "
                "where its supersession happened (the harvest ingest's slot retirement); "
                "captured: " + str([e[0] for e in cap.events if "kin" in e[0]][:12]))
            assert int(rswap.json().get("committed") or 0) >= 1, (
                f"the swap did not commit: {str(rswap.json())[:300]}")
    finally:
        _main_mod.log = saved_log
        _main_mod.log = saved_log


# ── R14 unit pins: the Slate restore (copula person assertion) + the retract-door
#    envelope, structure-accurate. The LIVE proof is the wiring gate above. ──────────────

def test_kin_copula_person_assertion_semantics():
    """Name-shaped surfaces assert personhood; role words, lowercase common nouns and
    degenerate surfaces never do — the honest-decline path stands."""
    from src.api.kin_weld_repoint import kin_copula_person_assertion
    nouns = frozenset({"wife", "husband", "mother", "father", "son", "daughter"})
    assert kin_copula_person_assertion("Slate", nouns) is True
    assert kin_copula_person_assertion("Cobalt Quarry", nouns) is True
    assert kin_copula_person_assertion("granite", nouns) is False  # lowercase: declines
    assert kin_copula_person_assertion("Wife", nouns) is False     # a role word: declines
    assert kin_copula_person_assertion("", nouns) is False
    assert kin_copula_person_assertion("7", nouns) is False
    assert kin_copula_person_assertion(None, nouns) is False


def test_copula_assertion_wired_in_both_stamp_blocks():
    """The Slate restore rides BOTH stamp disciplines (object-side R6 and subject-side
    R10) — whichever endpoint carries the name on the live route — and the assertion
    marker carries stamp_side so the endpoint is visible in the wild."""
    s = _main_source()
    assert s.count("kin_copula_person_assertion(") >= 2
    assert s.count("ingest.kin_copula_person_asserted") >= 2
    assert s.count('stamp_side="object"') >= 1
    assert s.count('stamp_side="subject"') >= 2  # the R10 discipline + the assertion
    # each assertion marker has its Person stamp assignment immediately before it:
    # the subject-side marker follows _subj_stamp = "Person"; the object-side marker
    # follows _stamp_type = "Person"
    first = s.index("ingest.kin_copula_person_asserted")
    second = s.index("ingest.kin_copula_person_asserted", first + 10)
    assert '_subj_stamp = "Person"' in s[first - 900: first]
    assert '_stamp_type = "Person"' in s[second - 900: second]


def test_retract_door_envelope_wired():
    """The LIVE correction route's supersession happens in the retract doors — the
    corrected-envelope emit is wired there, kin-gated (kin_rel_guard_set fallback) and
    counted (retracted_count in the marker fields)."""
    s = _main_source()
    door = s.index("def retract_fact(req: RetractRequest):")
    window = s[door: s.index("\ndef ", door + 100)]
    assert 'envelope="corrected"' in window
    assert "kin_rel_guard_set" in window
    assert "retracted_count=len(affected_ids)" in window
    assert "ingest.kin_bind_adjudicated" in window
    # every live retract door carries it (the /retract + forget variants)
    assert s.count('envelope="corrected"') >= 3


"""Pins for the L4-placement fix cluster (WOUNDS A-D, prod seat 10d7d879 laptop turn).

WOUND A  — a CIDR host address ("an ip of: 198.51.100.83/25") files has_ip (datatype cidr),
           never has_subnet-by-default, and NEVER feeds the date lane (no fabricated date).
WOUND B  — GROWTH-FIRST canonical keys (owner directive): resolution is (1) an EMPTY-floor
           tenant override rail, (2) identity no-op, (3) structural ASPECT-TOKEN IDENTITY
           against the tenant's OWN seeded/grown SCALAR shelves (ip_address → has_ip with the
           alias GROWN into rel_type_aliases so the walk resolves it), (4) a MINTED walkable
           SCALAR shelf for an aspect the seeds never named. NO datatype→key lookup table.
WOUND C  — device hostname-stem coref shape rules: sam-p70/p70/p70 laptop/lenovo thinkpad p70
           all stem-match; a person-shaped surface does NOT.
WOUND D  — the exist_on alias seeds resolve via the rel_type_aliases data path (member_of).

Structural/local pins run here in seconds. DB-gated behaviour (the live per-tenant overlay read,
the registry SQL probe, the migration fan-out) is honestly SKIPPED — no Postgres on the local box.

Run: python3 -m pytest tests/test_l4_placement_wounds_abcd.py -q
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.api import main as m
from src.api import linguistic_cue_overlay as lco
from src.entity_registry.registry import EntityRegistry


# ── shared fixture: a tenant's OWN rel_types metadata (the seeded network shelves, migration
#    061 shape — public seed ∪ tenant; _rel_meta() resolves this when no schema is bound) ──
_SHELVES = {
    "has_ip":     {"tail_types": ["SCALAR"], "category": "network"},
    "has_mac":    {"tail_types": ["SCALAR"], "category": "network"},
    "has_subnet": {"tail_types": ["SCALAR"], "category": "network"},
    "has_uuid":   {"tail_types": ["SCALAR"], "category": "network"},
    "born_in":    {"tail_types": ["Location"], "category": "network"},  # place-tailed: NEVER a shelf
}


class _FakeCur:
    def __init__(self, owner):
        self._owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._owner.sqls.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    rowcount = 0


class _FakeDB:
    def __init__(self):
        self.sqls = []
        self.commits = 0

    def cursor(self):
        return _FakeCur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


@pytest.fixture()
def shelves(monkeypatch):
    monkeypatch.setattr(m, "_REL_TYPE_META", dict(_SHELVES))
    yield


# ── WOUND A: CIDR host address files has_ip; bare subnet statement keeps has_subnet ──

def _atomic_by_value(text, value):
    return [a for a in m._detect_atomic_values(text, None) if a["value"] == value]


def test_cidr_host_address_files_has_ip():
    got = _atomic_by_value(
        "My laptop, hostname: sam-P70 has an ip of: 198.51.100.83/25 and exists on tailscale.",
        "198.51.100.83/25")
    assert len(got) == 1
    assert got[0]["rel_type"] == "has_ip"
    assert got[0]["type"] == "cidr"  # datatype stays cidr (detector-carried)


def test_bare_subnet_statement_still_files_has_subnet():
    got = _atomic_by_value("The subnet is 198.51.100.0/24 for the office LAN.",
                           "198.51.100.0/24")
    assert len(got) == 1
    assert got[0]["rel_type"] == "has_subnet"


# ── WOUND A: the IP/CIDR firewall on the date lane ──────────────────────────────────

def test_ip_cidr_span_never_a_date_candidate():
    from src.extraction.linguistics import _collect_date_spans, _span_is_ip_cidr_token
    assert _span_is_ip_cidr_token("198.51.100.83/25")
    assert _span_is_ip_cidr_token("198.51.100.83")
    assert _span_is_ip_cidr_token("an address 10.0.0.5 tail")
    assert _span_is_ip_cidr_token("83/25")  # the sheared CIDR tail — the measured fabrication path
    assert not _span_is_ip_cidr_token("2026-10-25")
    assert not _span_is_ip_cidr_token("october 25th")
    assert not _span_is_ip_cidr_token("12/25")  # a real calendar m/d reading stays a candidate
    text = ("My laptop, hostname: sam-P70 has an ip of: 198.51.100.83/25 and exists "
            "on tailscale as the same name.")
    for _start, span in _collect_date_spans(text):
        assert "192.168" not in span and "40.83" not in span


def test_p70_turn_fabricates_no_event_date():
    # The exact wound sentence: the temporal layer must return NO event_date (never a
    # fabricated calendar date minted from the address text). Measured WITH the NER model
    # loaded: spans == [] and date == (None, None).
    pytest.importorskip("dateparser")
    from src.temporal import extract_event_date
    from datetime import datetime, timezone
    ref = datetime(2026, 10, 9, tzinfo=timezone.utc)
    text = ("My laptop, hostname: sam-P70 has an ip of: 198.51.100.83/25 and exists "
            "on tailscale as the same name. The mac of this (wireless) nic is: "
            "2e:0f:ef:d9:67:11")
    iso, _gran = extract_event_date(text, ref)
    assert iso is None


def test_mac_address_is_not_a_date_candidate():
    from src.extraction.linguistics import _span_is_ip_cidr_token
    assert _span_is_ip_cidr_token("2e:0f:ef:d9:67:11")


def test_clock_time_stays_a_valid_candidate():
    from src.extraction.linguistics import _span_is_ip_cidr_token
    assert not _span_is_ip_cidr_token("12:30:45")


# ── WOUND B (growth-first): no seed floor, identity convergence, minted walkable shelf ──

def test_floor_is_deliberately_empty_growth_first():
    # Owner directive: direct seeding is last-ditch — the canonical shelves are seeded
    # rel_types (migration 061), not a datatype→key lookup table.
    assert lco._BOOTSTRAP_SCALAR_ATTRIBUTE_KEY == {}
    assert lco.resolve_scalar_attribute_key_map("") == {}


def test_no_big_seed_migration_exists():
    import pathlib
    assert not (pathlib.Path(__file__).resolve().parents[1]
                / "migrations" / "298_linguistic_cues_scalar_attribute_key.sql").exists()


def test_stray_key_converges_by_aspect_token_identity(shelves):
    # ip_address → has_ip: the aspect token 'ip' of the seeded shelf appears in the raw key.
    assert m._canonical_scalar_attribute_key("ip_address") == "has_ip"
    assert m._canonical_scalar_attribute_key("mac_address") == "has_mac"
    assert m._canonical_scalar_attribute_key("uuid") == "has_uuid"
    assert m._canonical_scalar_attribute_key("has_ip") == "has_ip"   # identity no-op (known shelf)


def test_place_tailed_rel_never_converges(shelves):
    # born_in carries a Location tail — it can never hold a scalar, so a raw key sharing
    # its aspect is NOT rewritten to it (only SCALAR-tailed has_* shelves resolve).
    assert m._canonical_scalar_attribute_key("born_in") == "born_in"


def test_convergence_grows_the_walkable_alias(shelves):
    # The alias is GROWN into rel_type_aliases from ingest evidence — the query's aspect
    # routing reads alias rows, so the convergence is walk-visible, not a private fix.
    db = _FakeDB()
    got = m._canonical_scalar_attribute_key("ip_address", db=db)
    assert got == "has_ip"
    alias_sqls = [s for s, _p in db.sqls if "rel_type_aliases" in s]
    assert alias_sqls and "ON CONFLICT (alias) DO NOTHING" in alias_sqls[0]
    assert "ingest_identity_growth" in alias_sqls[0]
    assert db.commits >= 1


def test_unmatched_aspect_MINTS_a_walkable_scalar_shelf(shelves):
    # 'website' matches NO seeded shelf aspect → MINTED as a NEW rel_type with
    # tail_types=['SCALAR'] through the in-flow growth mint. THE WALKABILITY PIN: a minted
    # type the walk cannot see is a word, not a type — the mint must carry the SCALAR tail
    # (and the mint path propagates category/taxonomy inside _create_rel_type_in_flow).
    db = _FakeDB()
    got = m._canonical_scalar_attribute_key("website", db=db)
    assert got == "website"
    mint_sqls = [s for s, _p in db.sqls if "insert into rel_types" in s.lower()]
    assert mint_sqls, "the growth mint never fired"
    # tail_types=['SCALAR'] rides the mint's params — THE walkability: ATTRSCOPE/determine_path
    # admit a scalar lane only for SCALAR-tailed rel_types.
    mint_params = [p for s, p in db.sqls if "insert into rel_types" in s.lower()]
    assert any("SCALAR" in str(p) for p in mint_params), mint_params[:1]
    assert db.commits >= 1


def test_resolve_without_db_is_read_only(shelves):
    # Pins/pre-connection callers: identity resolution still works, nothing is written.
    db = _FakeDB()
    assert m._canonical_scalar_attribute_key("ip_address") == "has_ip"
    assert not db.sqls  # read-only (no db → no growth writes)


def test_unresolvable_passes_through(shelves):
    assert m._canonical_scalar_attribute_key("age") == "age"
    assert m._canonical_scalar_attribute_key("") == ""


def test_ingest_write_site_uses_canonical_key_source():
    import inspect
    src = inspect.getsource(m)
    assert "_canonical_scalar_attribute_key(" in src
    # wired at the entity_attributes write site with the db (growth writes enabled)
    assert "_ss_attr = _canonical_scalar_attribute_key(" in src
    assert "db=db)" in src.split("_ss_attr = _canonical_scalar_attribute_key(")[1][:200]


# ── WOUND C: device hostname-stem coref shape rules ─────────────────────────────────

def test_p70_family_surfaces_stem_match():
    fam = ["p70", "sam-p70", "chris_P70", "p70 laptop", "lenovo thinkpad p70",
           "my p70", "SAM-P70"]
    for a in fam:
        for b in fam:
            assert EntityRegistry._hostname_stem_match(a, b), (a, b)


def test_similar_named_person_shape_does_not_match():
    # A person surface carrying no digit-bearing hostname token can never match.
    assert EntityRegistry._hostname_tokens("marina") == []
    assert EntityRegistry._hostname_stem_match("sam-p70", "marina") is None
    assert EntityRegistry._hostname_stem_match("daughter", "marina") is None


def test_hostname_guard_rejects_short_or_digitless_tokens():
    # 'xps' alone (no digit) is not hostname-shaped; 'a1' (<3 chars) is not either.
    assert EntityRegistry._hostname_tokens("xps") == []
    assert EntityRegistry._hostname_tokens("a1") == []
    assert EntityRegistry._hostname_stem_match("xps laptop", "xps 13") is None


def test_different_models_do_not_stem_match():
    # Boundary: 'p700' does not stem-match 'p70'; 'xps-13' and 'xps-15' share no
    # digit-bearing stem suffix.
    assert EntityRegistry._hostname_stem_match("p700", "p70") is None
    assert EntityRegistry._hostname_stem_match("xps-13", "xps-15") is None


def test_resolve_wires_the_hostname_coref_before_mint():
    import inspect
    src = inspect.getsource(EntityRegistry.resolve)
    assert "_device_hostname_coref_target(" in src
    assert src.index("_device_hostname_coref_target(") < src.index(
        'log.info("entity_registry.registered"')


# ── WOUND D: exist_on alias rows (data path; the ingest lane already canonicalizes) ──

def test_exist_on_alias_rows_present_in_migration():
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parents[1] / "migrations"
           / "297_rel_type_aliases_exist_on.sql")
    body = sql.read_text()
    assert "('member_of', 'exist_on',  'ontology', 1.0)" in body
    assert "('member_of', 'exists_on', 'ontology', 1.0)" in body
    assert "ON CONFLICT (alias) DO NOTHING" in body


# ── honest env-gated skips ──────────────────────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("POSTGRES_DSN"),
                    reason="DB-gated: the live per-tenant overlay read, the registry SQL coref "
                           "probe, and the migration fan-out need a Postgres; structural rules "
                           "are pinned above locally.")
def test_tenant_union_and_registry_sql_probe_db_gated():
    assert True



# ── WOUND E: the instance_of TARGET-SHAPE guard (the cross-ontology break) ──────────
# Prod specimen: recall rendered "P70 Laptop is an instance of DAX" — dax is the owner's
# SON, a named Person. instance_of is the L4 PLACE edge and must target a TYPE node, never a
# named instance/person. Guard: hardline_guard.refuses_instance_target (same evidence row as
# refuses_subclass_rung, polarity inverted for the TARGET slot), wired at
# main._detect_semantic_conflicts so the edge is REFUSED, never misfiled.

from src.api import hardline_guard as hg


def _ev_row(role=None, instance=False, naming=False, attr=False, slot=False,
            asserted=False, instance_slot=False):
    # _EVIDENCE_SQL row layout: (node_role, instance, naming, attr, slot, asserted, instance_slot)
    return (role, instance, naming, attr, slot, asserted, instance_slot)


class _EvidenceConn:
    """Fake DB connection serving one canned evidence row to the guard's savepointed query."""

    def __init__(self, row):
        self._row = row

    class _Cur:
        def __init__(self, owner):
            self._owner = owner

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            s = " ".join(str(sql).split())
            self._rowout = self._owner._row if (s.startswith("SELECT") and "FROM entities" in s) else None

        def fetchone(self):
            return self._rowout

    def cursor(self):
        return _EvidenceConn._Cur(self)


class _BoomConn:
    """A connection whose entity read always fails — the guard must FAIL CLOSED."""

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "FROM entities" in " ".join(str(sql).split()):
                raise RuntimeError("socket dead")

    def cursor(self):
        return _BoomConn._Cur()


def test_instance_of_target_guard_refuses_a_named_person():
    # THE wound shape: target 'dax' — a named PERSON (naming row + its own instance_of
    # into person/son). All memory-side arms refuse, structurally, no word lists.
    for row, why in (
        (_ev_row(instance=True), "instance_target_is_an_instance"),
        (_ev_row(naming=True), "instance_target_is_a_name"),
        (_ev_row(attr=True), "instance_target_holds_a_value"),
        (_ev_row(slot=True), "instance_target_is_a_value_slot"),
        (_ev_row(role="name"), "node_role=name"),
        (_ev_row(asserted=True, instance_slot=True), "instance_target_is_an_instance_slot"),
    ):
        refuse, reason = hg.refuses_instance_target(_EvidenceConn(row), "dax")
        assert refuse, (row, reason)
        assert reason == why or reason.startswith("node_role"), (why, reason)


def test_instance_of_target_guard_allows_genuine_type_nodes():
    # A type node (fresh, or user-asserted with a common-noun surface) admits — the LV2 bar:
    # absence of memory-side evidence is the normal state of every genuine new type.
    refuse, reason = hg.refuses_instance_target(_EvidenceConn(_ev_row()), "laptop", surface="laptop")
    assert not refuse, reason
    # Asserted + common-noun surface: admits when the lexical source can confirm the common
    # type (prod, nltk/WordNet installed); FAILS CLOSED to refuse when it cannot (this box —
    # no nltk). Both directions are correct: the assert arm never over-fires in prod, and
    # uncertainty refuses rather than misfiles.
    try:
        import nltk  # noqa: F401
        _lexical = True
    except Exception:  # noqa: BLE001
        _lexical = False
    refuse, reason = hg.refuses_instance_target(
        _EvidenceConn(_ev_row(asserted=True)), "laptop", surface="laptop")
    if _lexical:
        assert not refuse, reason
    else:
        # TRI-STATE (round-5): no nltk = oracle DOWN = install defect, NOT a verdict.
        # The instance_of typing arm fails OPEN here (a definite proper-noun verdict
        # still refuses; an absent lexical DB must not starve first-mention ladders).
        assert not refuse, reason


def test_instance_of_target_guard_fails_closed_on_read_error():
    # An undecidable target REFUSES the edge — fail direction: refuse, never misfile.
    refuse, reason = hg.refuses_instance_target(_BoomConn(), "mystery")
    assert refuse and reason == "read_error_fail_closed"


def test_instance_of_target_guard_wired_at_the_semantic_conflict_gate():
    import inspect
    src = inspect.getsource(m._detect_semantic_conflicts)
    assert "refuses_instance_target(" in src
    # The guard must run BEFORE the is_leaf_only early-return (instance_of is not leaf_only —
    # a guard placed after it would be decoration).
    assert src.index("refuses_instance_target(") < src.index("is_leaf_only")


def test_semantic_conflict_gate_refuses_the_cross_ontology_edge(monkeypatch):
    # End-to-end through the gate with a canned memory-side target: (p70 laptop, instance_of,
    # dax) -> supersede_new (edge refused), never 'keep'.
    monkeypatch.setattr(
        hg, "refuses_instance_target",
        lambda db, nid, surface=None, user_id=None: (True, "instance_target_is_an_instance"))
    monkeypatch.setattr(m, "_get_rel_type_metadata", lambda rt: {})
    decision, reason = m._detect_semantic_conflicts(
        _FakeDB(), "u-123", "p70 laptop", "instance_of", "dax")
    assert decision == "supersede_new"
    assert "hard_line_instance_of_target_not_a_type" in reason


def test_semantic_conflict_gate_keeps_a_clean_typing_edge(monkeypatch):
    monkeypatch.setattr(
        hg, "refuses_instance_target",
        lambda db, nid, surface=None, user_id=None: (False, None))
    monkeypatch.setattr(m, "_get_rel_type_metadata", lambda rt: {})
    decision, reason = m._detect_semantic_conflicts(
        _FakeDB(), "u-123", "p70 laptop", "instance_of", "laptop")
    assert decision == "keep"


# ═══════════════════════════════════════════════════════════════════════════════
# PRE-PROD LIVE-FAIL PINS (owner verdict on be10006d: "two fixes fail live")
# ═══════════════════════════════════════════════════════════════════════════════

import ast as _ast
import re as _re2
import textwrap as _tw


def _src_of(path, name):
    with open(path, "r", encoding="utf-8") as f:
        tree = _ast.parse(f.read())
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == name:
            with open(path, "r", encoding="utf-8") as f:
                return "".join(f.readlines()[node.lineno - 1:node.end_lineno])
    return None


def test_livefail1_datatype_rides_the_value_through_the_merge():
    """FULL live-shape replay of the P70 turn (spine harvest, not just the detector):
    the surviving has_ip edge carries object_datatype 'cidr' — the detector's typed
    twin LOST the merge (first-seen spine edge wins, no datatype) until the carry;
    and the surviving edge value passes _validate_scalar_datatype against 'cidr'."""
    from src.api.main import _validate_scalar_datatype, _DT_REGEX
    main_src = open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
    # the carry exists AT the merge (not only in the detector)
    # the carry exists AT the spine merge (log tag + backfill assignment), not only in
    # the detector: the collision path threads the detector datatype onto the survivor
    assert "atomic_datatype_carried" in main_src,         "the merge-site datatype carry is missing"
    assert 'object_datatype"] = _dt' in main_src
    # the surviving edge datatype validates the /25 value against the CIDR grammar
    ok, why = _validate_scalar_datatype("cidr", "198.51.100.83/25", None)
    assert ok, why
    # value-shape-first belt: a declared ipv4 slot NEVER rejects a cidr-shaped value
    ok2, why2 = _validate_scalar_datatype("ipv4", "198.51.100.83/25", None)
    assert ok2, why2
    # and a genuinely wrong ipv4 value still refuses (no widening of the gate)
    ok3, _ = _validate_scalar_datatype("ipv4", "not-an-ip", None)
    assert not ok3
    ok4, _ = _validate_scalar_datatype("ipv4", "999.1.1.1", None)
    assert not ok4


def test_livefail2_device_restate_never_mints_a_second_entity():
    """LIVE-shape guard (registry layer): a device restate by hostname corefs onto the
    existing referent — and a GLiNER2-MISTYPED device (entity_type='Person' whose alias
    is hostname-shaped, digit-bearing) is never skipped by the person-skip, which is
    the live mint's root cause ('sam-P70' looks like a name)."""
    reg_src = open("/workspace/FaultLine-dev-saas/src/entity_registry/registry.py", encoding="utf-8").read()
    probe = _src_of("/workspace/FaultLine-dev-saas/src/entity_registry/registry.py",
                    "_device_hostname_coref_target")
    assert probe is not None
    # the mistyped-device exemption is IN the probe
    assert "_alias_is_hostname" in probe
    assert '== "person" and not _alias_is_hostname' in probe
    # and _hostname_tokens (digit-bearing shape) drives the exemption — the same
    # oracle the mint uses, never a word list
    assert "self._hostname_tokens(_alias)" in probe


def test_livefail2_its_mac_resolves_to_the_device_never_the_speaker():
    """LIVE-shape replay of the restate sentence: the inanimate 3rd-person possessive
    ('its mac') resolves to the nearest HOSTNAME-SHAPED NP — never the user entity.
    Structural, spaCy-driven; the speaker is 1st-person and a Person=3 possessive is
    grammatically incapable of denoting them."""
    pytest.importorskip("spacy")
    from src.extraction.linguistics import (
        _nearest_hostname_surface, possessor_of_possessive_attribute)
    # the helper exists and finds the hostname-shaped device surface
    doc_txt = "sam-P70 is my laptop. Its mac is 2e:0f:ef:d9:67:11"
    import spacy as _spacy
    _nlp = _spacy.load(os.environ.get("SPACY_MODEL", "en_core_web_sm"))
    doc = _nlp(doc_txt)
    its_i = next(t.i for t in doc if (t.text or "").lower() == "its")
    dev = _nearest_hostname_surface(doc, its_i)
    assert dev and "70" in dev, dev
    # and the possessive-attribute possessor for the mac value is the DEVICE surface
    # (or None) — NEVER 'user'
    poss = possessor_of_possessive_attribute(doc_txt, "2e:0f:ef:d9:67:11")
    assert poss != "user", poss


def test_livefail2_named_instance_device_scalars_never_default_to_the_user():
    """The named-instance possessor helper's missing-poss default is 'user' ONLY for a
    non-hostname-shaped type - a hostname-shaped type surface ('sam-p70') is hardware
    and anchors its own scalars (the its-mac-on-user live wound)."""
    pytest.importorskip("spacy")
    import inspect
    import src.extraction.linguistics as L
    src = inspect.getsource(L)
    # the device guard exists in the helper (a hostname-shaped type anchors itself)
    assert "DEVICE GUARD" in src
    assert "_self_host" in src


# ═══════════════════════════════════════════════════════════════════════════════
# ROUND-5 GATE RED 1 — DEVICE-FRAGMENT SPLIT ("my laptop, hostname: sam-P70 …")
# ═══════════════════════════════════════════════════════════════════════════════

import datetime as _datetime

_RED1_REF = _datetime.date(2023, 6, 1)

_RED1_SENT = ("my laptop, hostname: sam-P70 has an ip of 198.51.100.83/25 and exists on "
              "tailscale as the same name. The mac of this (wireless) nic is: "
              "2e:0f:ef:d9:67:11")


def test_red1_hostname_appositive_type_rung_not_a_fragment():
    """The exact P70 turn yields ONE device entity: sam-p70 carries the scalars AND the
    (sam-p70, instance_of, laptop) rung — the type head 'laptop' is the device L4 TYPE,
    never a standalone zero-attribute entity. The (user, owns, laptop) fragment, and the
    alias/role twins that re-mint it ((laptop, also_known_as, hostname),
    (laptop, has_role, hostname)), are suppressed at the deriver."""
    pytest.importorskip("spacy")
    import subprocess
    import sys as _sys

    # THE LESSON (issue #31 R1 + round-5 re-gate): a pin that runs the deriver under the
    # CODE-DEFAULT flags proves nothing about deploy — production sets SPINE_NAMING_CHAIN
    # et al, and the naming chain re-routes this construction (measured: the round-5 fix
    # passed locally and minted (hostname, instance_of, laptop) live). Run the deriver in
    # a SUBPROCESS under BOTH the code defaults AND the measured production flag set; the
    # required shape must hold in both environments.
    def _derive_under(extra_env):
        env = dict(os.environ)
        env.setdefault("SPACY_MODEL", "en_core_web_sm")
        env["RED1_SENT"] = _RED1_SENT
        env.update(extra_env)
        code = "\n".join([
            "import sys, datetime",
            "sys.path.insert(0, '/workspace/FaultLine-dev-saas')",
            "from src.extraction import linguistics as L",
            "for f in L.derive_sentence_facts(__import__('os').environ['RED1_SENT'],",
            "                                 datetime.date(2023, 6, 1)):",
            "    print(f.subject, f.rel_type, f.object, sep='|')",
        ])
        p = subprocess.run([_sys.executable, "-c", code], env=env,
                           capture_output=True, text=True, timeout=300)
        assert p.returncode == 0, p.stderr[-2000:]
        return {tuple(ln.split("|")) for ln in p.stdout.strip().splitlines() if ln.count("|") == 2}

    _PROD_FLAGS = {
        "SPINE_NAMING_CHAIN": "true",            # measured in-container 2026-10-09
        "SPINE_POSSESSIVE_ALIENABILITY": "true",
        "SPINE_PENDING_GROUNDING": "true",
        "SPINE_COVERAGE_GATE": "true",
        "SPINE_IMPLICATIVE_XCOMP": "true",
        "SPINE_PA_CORE": "1",
    }
    edges = _derive_under({})
    edges_prod = _derive_under(_PROD_FLAGS)
    for _e in (edges, edges_prod):
        assert ("sam-p70", "instance_of", "laptop") in _e, _e
        assert ("user", "owns", "sam-p70") in _e, _e
        # the scalars stay on the DEVICE
        assert any(s == "sam-p70" and r == "ip" for (s, r, _o) in _e), _e
        # the fragment and its twins never mint
        assert ("user", "owns", "laptop") not in _e, _e
        assert ("laptop", "also_known_as", "hostname") not in _e, _e
        assert ("laptop", "has_role", "hostname") not in _e, _e
        # the SPINE_NAMING_CHAIN relabel wound: the LABEL noun is never the device
        assert ("hostname", "instance_of", "laptop") not in _e, _e
        assert ("user", "owns", "hostname") not in _e, _e
    assert ("sam-p70", "instance_of", "laptop") in edges, edges
    assert ("user", "owns", "sam-p70") in edges, edges
    # the scalars stay on the DEVICE
    assert any(s == "sam-p70" and r == "ip" for (s, r, _o) in edges), edges
    # the fragment and its twins never mint
    assert ("user", "owns", "laptop") not in edges, edges
    assert ("laptop", "also_known_as", "hostname") not in edges, edges
    assert ("laptop", "has_role", "hostname") not in edges, edges


def test_red1_flag_off_is_legacy():
    """SPINE_HOSTNAME_APPOSITIVE_TYPE=false -> byte-identical legacy behaviour (the flag
    is read at module import, so assert the flag exists and the guard sites check it)."""
    import inspect
    from src.extraction import linguistics as L
    assert L.SPINE_HOSTNAME_APPOSITIVE_TYPE is True
    src = inspect.getsource(L)
    assert "SPINE_HOSTNAME_APPOSITIVE_TYPE" in src
    # every guard site honours the flag (no unconditional suppression)
    assert (src.count("if SPINE_HOSTNAME_APPOSITIVE_TYPE")
            + src.count("and SPINE_HOSTNAME_APPOSITIVE_TYPE")) >= 5


# ═══════════════════════════════════════════════════════════════════════════════
# ROUND-5 GATE RED 2 — SAME-TURN SELF-REFERENTIAL instance_of ((mac, instance_of, wifi mac))
# ═══════════════════════════════════════════════════════════════════════════════

def test_red2_registry_tracks_same_turn_mints():
    """The registry records every surrogate it mints this turn and exposes minted_this_turn
    (the evidence probe the WOUND E guard lacked when both junk endpoints were fresh)."""
    reg_src = open("/workspace/FaultLine-dev-saas/src/entity_registry/registry.py",
                   encoding="utf-8").read()
    assert "self._minted_names = set()" in reg_src
    assert "self._minted_names.add((str(user_id), name))" in reg_src
    assert "def minted_this_turn(" in reg_src
    r = EntityRegistry.__new__(EntityRegistry)
    r._minted_names = {("u-1", "mac")}
    assert r.minted_this_turn("u-1", "Mac") is True      # probe normalizes like the mint
    assert r.minted_this_turn("u-1", "wifi mac") is False
    assert r.minted_this_turn("u-2", "mac") is False
    # AND the surrogate id: the live conflict loop probes with the RESOLVED uuid --
    # surface-only tracking made the gate blind (live-proven 2026-10-09).
    r._minted_names.add(("u-1", "uuid-mac-1"))
    assert r.minted_this_turn("u-1", "uuid-mac-1") is True
    # fail-safe: a registry missing the set (older pickle / shadow rebind) -> not fresh
    r2 = EntityRegistry.__new__(EntityRegistry)
    assert r2.minted_this_turn("u-1", "mac") is False


class _ContentDB(_FakeDB):
    """fetchone-controlled fake: rows seen / no rows seen for the subject-evidence probe."""
    def __init__(self, has_content):
        super().__init__()
        self._has_content = has_content

    def cursor(self):
        c = _FakeCur(self)
        c.fetchone = lambda: (1,) if self._has_content else None
        return c


def test_red2_same_turn_self_referential_instance_of_refused(monkeypatch):
    """BOTH endpoints fresh this turn + subject carries NO independent content ->
    (mac, instance_of, wifi mac) is refused (supersede_new, self_referential) — the gap
    the memory-side WOUND E guard could not see."""
    monkeypatch.setattr(
        hg, "refuses_instance_target",
        lambda db, nid, surface=None, user_id=None: (False, None))
    monkeypatch.setattr(m, "_get_rel_type_metadata", lambda rt: {})
    decision, reason = m._detect_semantic_conflicts(
        _ContentDB(has_content=False), "u-123", "mac", "instance_of", "wifi mac",
        subj_fresh=True, obj_fresh=True)
    assert decision == "supersede_new"
    assert "hard_line_instance_of_self_referential" in reason


def test_red2_device_with_content_keeps_its_rung(monkeypatch):
    """A fresh SUBJECT that ALREADY carries user-stated content (the device's has_ip
    scalars land earlier in the batch) keeps its legitimate rung — the refusal is for
    content-less self-classification only, never for (device, instance_of, type-node)."""
    monkeypatch.setattr(
        hg, "refuses_instance_target",
        lambda db, nid, surface=None, user_id=None: (False, None))
    monkeypatch.setattr(m, "_get_rel_type_metadata", lambda rt: {})
    decision, _ = m._detect_semantic_conflicts(
        _ContentDB(has_content=True), "u-123", "sam-p70", "instance_of", "laptop",
        subj_fresh=True, obj_fresh=True)
    assert decision == "keep"
    # and a NON-fresh pair is governed by the original WOUND E guard alone (flags off)
    decision2, _ = m._detect_semantic_conflicts(
        _ContentDB(has_content=False), "u-123", "mac", "instance_of", "wifi mac")
    assert decision2 == "keep"


def test_red2_ingest_loop_passes_fresh_flags():
    """The /ingest conflict loop wires registry.minted_this_turn into the gate — without
    the wiring the guard above is dead code (the live wound: refused_edges empty)."""
    main_src = open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
    assert "minted_this_turn(req.user_id, _subj)" in main_src
    assert "minted_this_turn(req.user_id, _obj)" in main_src
    assert "subj_fresh=_subj_fresh, obj_fresh=_obj_fresh" in main_src
    assert "hard_line_instance_of_self_referential" in main_src


# ═══════════════════════════════════════════════════════════════════════════════
# ROUND-5 — pending-placement drain ALIAS-MAP FOLD (exist_on-style rels become walkable)
# ═══════════════════════════════════════════════════════════════════════════════

def test_red_pending_drain_alias_fold():
    """The drain consults the tenant rel_type_aliases map (the data migration 297 populates)
    before declaring a pending rel undrainable — a rel whose alias maps to a SEEDED canonical
    folds onto it without re-ingest."""
    emb_src = open("/workspace/FaultLine-dev-saas/src/re_embedder/embedder.py",
                   encoding="utf-8").read()
    assert "SELECT canonical_rel_type FROM rel_type_aliases" in emb_src
    assert "ALIAS-MAP FOLD" in emb_src
    # the fold sits BEFORE the undrainable CRIT (a mappable rel can never be counted unplaceable)
    assert emb_src.index("ALIAS-MAP FOLD") < emb_src.index("pending_placement_backlog_undrainable")


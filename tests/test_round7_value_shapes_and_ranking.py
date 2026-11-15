# ROUND-7 nvidia-lane re-gate pins (seat 93e3050b, live-proven wounds):
#   RED 3(c)  recall "what is the IP and MAC of my laptop?" anchored the JUNK 'mac'
#             Person (f37ce038, occupation=wireless) -- the wifi/mac junk PAIR still
#             minted live at the name-type binding seam, and the copula-state lane
#             minted the 'mac' Person from "the mac of this (wireless) nic is: <MAC>".
#   RED 3(c') the first-match-wins n-gram anchor lane grounds the same query on a
#             zero-scope same-surface junk node before an owned, evidence-bearing
#             candidate (the queried type's own instance) is ever considered.
# Pins are behavioral at the deriver (subprocess under the MEASURED production flag
# set -- the round-5/6 lesson: code-default pin env proves nothing about deploy),
# at the ranking helper (deterministic stub-db, no cosine), and source-level at the
# mint/typing veto (a machine value is never a Person).
import os
import subprocess
import sys

import pytest

import src.api.main as _main_mod
WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(_main_mod.__file__), "..", ".."))

# =============================================================================
# value_shapes: the machine-value SHAPE oracle (the in-code floor of the tenant's
# scalar_atomic value classes -- MAC / IPv4 / email / URL; full-span only; bare
# numbers deliberately NOT a value shape: "my son Alex, 19" keeps binding).
# =============================================================================

from src.extraction.value_shapes import is_value_shape, span_adjoins_value_shape


def test_value_shape_mac():
    assert is_value_shape("aa:bb:cc:11:22:33") is True
    assert is_value_shape("2E:0F:EF:D9:67:11") is True


def test_value_shape_ipv4_cidr():
    assert is_value_shape("192.0.2.83/25") is True
    assert is_value_shape("10.0.0.1") is True


def test_value_shape_email_url():
    assert is_value_shape("chris@example.com") is True
    assert is_value_shape("https://wiki.internal/x") is True


def test_bare_number_is_deliberately_not_a_value_shape():
    # "my son Alex, 19" must keep binding -- a bare number is a scalar, not a
    # machine value; the veto must never fire on it.
    assert is_value_shape("19") is False


def test_word_surfaces_are_not_value_shapes():
    for w in ("mac", "wifi", "chris-p70", "laptop", "wireless nic"):
        assert is_value_shape(w) is False, w


def test_field_label_adjacency():
    # "has wifi mac aa:bb:cc:11:22:33" -- the label token 'mac' is immediately
    # followed (after punctuation/space strip) by a value shape.
    text = "my p70 laptop has wifi mac aa:bb:cc:11:22:33"
    i = text.index(" mac ") + 1
    assert span_adjoins_value_shape(text, i, i + 3) is True
    # a label NOT followed by a value stays a legitimate name candidate
    text2 = "my colleague mac joined today"
    j = text2.index("mac")
    assert span_adjoins_value_shape(text2, j, j + 3) is False


# =============================================================================
# RED 3(c) deriver pin: the junk PAIR never mints; the good edges survive.
# =============================================================================

_RESTATE_SENT = "my P70 laptop has wifi mac aa:bb:cc:11:22:33"
_NIC_SENT = "the mac of this (wireless) nic is: aa:bb:cc:11:22:33"

_PROD_FLAGS = {
    "SPINE_NAMING_CHAIN": "true",             # measured in-container 2026-10-09
    "SPINE_POSSESSIVE_ALIENABILITY": "true",
    "SPINE_PENDING_GROUNDING": "true",
    "SPINE_COVERAGE_GATE": "true",
    "SPINE_IMPLICATIVE_XCOMP": "true",
    "SPINE_PA_CORE": "1",
}


def _derive_under(sentence, extra_env):
    env = dict(os.environ)
    env.setdefault("SPACY_MODEL", "en_core_web_sm")
    env["RED7_SENT"] = sentence
    env.update(extra_env)
    code = "\n".join([
        "import sys, datetime",
        "sys.path.insert(0, %r)" % WORKSPACE,
        "from src.extraction import linguistics as L",
        "for f in L.derive_sentence_facts(__import__('os').environ['RED7_SENT'],",
        "                                 datetime.date(2023, 6, 1)):",
        "    print(f.subject, f.rel_type, f.object, sep='|')",
    ])
    p = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-2000:]
    return {tuple(ln.split("|")) for ln in p.stdout.strip().splitlines()
            if ln.count("|") == 2}


@pytest.mark.parametrize("extra_env", [{}, _PROD_FLAGS],
                         ids=["code-defaults", "prod-flags"])
def test_restate_mints_no_value_word_person_pair(extra_env):
    """LIVE-shape guard: the wifi-mac restate never emits the
    (wifi, also_known_as, mac) + (mac, instance_of, wifi) junk pair (two value-word
    Person entities, live on the gate tenant). The device edges stay."""
    edges = _derive_under(_RESTATE_SENT, extra_env)
    assert ("wifi", "also_known_as", "mac") not in edges, edges
    assert ("mac", "instance_of", "wifi") not in edges, edges
    assert ("wifi", "instance_of", "mac") not in edges, edges
    assert ("laptop", "wifi_mac", "aa:bb:cc:11:22:33") in edges, edges
    assert ("user", "owns", "p70 laptop") in edges, edges


@pytest.mark.parametrize("extra_env", [{}, _PROD_FLAGS],
                         ids=["code-defaults", "prod-flags"])
def test_copula_state_value_never_files_on_the_field_label(extra_env):
    """LIVE-shape guard: the mac-of-nic sentence never mints the 'mac' Person (the
    live f37ce038 row, occupation=wireless). A machine VALUE is never the state of
    a field LABEL -- the state rides the of-possessor NP."""
    edges = _derive_under(_NIC_SENT, extra_env)
    state_edges = [e for e in edges if e[2] == "aa:bb:cc:11:22:33"]
    assert state_edges, edges
    for (subj, rel, _obj) in state_edges:
        assert is_value_shape(subj) is False, (subj, rel)
        assert subj not in ("mac", "user"), (subj, rel)


# =============================================================================
# Mint/typing veto: a machine value is never typed Person (registry seam) and
# never lands as an entity span (typed-doc seam).
# =============================================================================

def test_registry_refuses_person_typing_for_value_shape_aliases():
    reg_src = open(os.path.join(WORKSPACE, "src/entity_registry/registry.py"),
                   encoding="utf-8").read()
    assert "from src.extraction.value_shapes import is_value_shape" in reg_src
    assert "entity_type == 'Person' and is_value_shape(alias)" in reg_src
    assert "value_shape_person_typing_refused" in reg_src


def test_typed_doc_drops_value_shape_entity_spans():
    main_src = open(os.path.join(WORKSPACE, "src/api/main.py"),
                    encoding="utf-8").read()
    assert "from src.extraction.value_shapes import is_value_shape" in main_src
    assert "value_shape_span_dropped" in main_src


# =============================================================================
# RED 3(c') anchor ranking: deterministic, evidence-ranked, type-descending.
# =============================================================================

def _bucket(sql):
    s = sql.lower()
    if "entity_aliases" in s:
        return "aliases"
    if "entity_attributes" in s:
        return "attrs"
    if "from facts" in s and ("instance_of" in s or "rel_type" in s):
        return "instances"
    if "from facts" in s:
        return "facts"
    return "other"


class _FakeCursor:
    """Answers the ranking SQL deterministically."""

    def __init__(self, store):
        self._store = store
        self._last = ("", None)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        sql, params = a[0], (a[1] if len(a) > 1 else None)
        self._last = (sql, params)
        if params is None and len(sql) and not isinstance(sql, str):
            sql, params = sql, None
        self._last = (sql, params)

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def fetchall(self):
        sql, params = self._last
        rows = self._store.get(_bucket(sql), [])
        if not params or "entity_aliases" in sql.lower():
            return list(rows)
        vals = {p for p in params if isinstance(p, str)}
        rows = [r for r in rows if any(c in vals for c in r)]
        # facts.superseded_at tombstone: the helper filters it in SQL; the stub
        # models it as a trailing column (None = live, timestamp = retracted)
        if "superseded_at" in sql.lower():
            rows = [r for r in rows if len(r) < 3 or r[2] is None]
        return rows


class _FakeDB:
    def __init__(self, store):
        self._store = store
        self.cursor = lambda: _FakeCursor(store)


def test_ranked_alias_descends_type_to_its_owned_instance(monkeypatch):
    """The 'laptop' surface names a bare TYPE node whose instance (chris-p70) is
    OWNED and carries the attributes. The ranking descends the rung and returns
    the instance at rank class 0."""
    import src.api.main as M
    store = {
        "aliases": [("type-node",)],
        "attrs": [("junk-person",), ("chris-p70",)],
        "instances": [("chris-p70", "type-node")],
        "facts": [],
    }
    db = _FakeDB(store)
    monkeypatch.setattr(M, "_entity_owned_by",
                        lambda d, eid, uid: eid == "chris-p70")
    hit = M._ranked_alias_anchor(db, "laptop", "u-1")
    assert hit is not None
    assert hit[0] == "chris-p70" and hit[1] == 0, hit


def test_ranked_alias_unowned_junk_is_never_a_class0_override(monkeypatch):
    """The 'mac' surface sits on the junk Person (attrs, unowned). No owned
    candidate exists, so the helper must NOT hand back a class-0 override -- it
    returns (junk, 2) and the pre-pass falls through to the legacy lane."""
    import src.api.main as M
    store = {
        "aliases": [("junk-person",)],
        "attrs": [("junk-person",)],
        "instances": [],
        "facts": [("junk-person",)],
    }
    db = _FakeDB(store)
    monkeypatch.setattr(M, "_entity_owned_by", lambda d, eid, uid: False)
    hit = M._ranked_alias_anchor(db, "mac", "u-1")
    assert hit == ("junk-person", 2), hit


def test_ranked_alias_no_candidates_is_none(monkeypatch):
    import src.api.main as M
    db = _FakeDB({"aliases": [], "attrs": [], "instances": [], "facts": []})
    monkeypatch.setattr(M, "_entity_owned_by", lambda d, eid, uid: False)
    assert M._ranked_alias_anchor(db, "nothing", "u-1") is None


def test_ranked_alias_bare_node_without_instances_never_overrides(monkeypatch):
    """A bare alias with no evidence and no instances returns None -- the existing
    longest-match lane decides, byte-for-byte unchanged."""
    import src.api.main as M
    db = _FakeDB({"aliases": [("x",)], "attrs": [], "instances": [], "facts": []})
    monkeypatch.setattr(M, "_entity_owned_by", lambda d, eid, uid: True)
    # an owned-but-bare candidate ranks class 1 -- below the pre-pass override
    # threshold (class 0), so the legacy lane still decides
    assert M._ranked_alias_anchor(db, "x", "u-1") == ("x", 1)


def test_tombstoned_instance_of_rung_never_ranks(monkeypatch):
    """ROUND-8 (prod-proven): a RETRACTED (cato, instance_of, laptop) rung still sat in
    facts and competed at rank 0; the (rank, uuid) tie broke to the lexically-smaller
    junk uuid and recall anchored 'laptop' to Cato instead of the device. The helper's
    facts probes (has_fact + type-descend instances) must filter superseded_at IS NULL,
    so the tombstoned rung never ranks and the anchor lands on the device."""
    import src.api.main as M
    # the prod tie: BOTH instances are owned and evidence-bearing, so the rung tie
    # broke to the lexically-smaller uuid (aaa-cato) while the tombstoned rung sat
    # in facts. uuids chosen so the UNFIXED sort deterministically picks cato.
    store = {
        "aliases": [("type-node",)],
        "attrs": [("aaa-cato",), ("zzz-device",)],
        # (cato, instance_of, laptop) is RETRACTED -> superseded_at set
        "instances": [("aaa-cato", "type-node", "2026-10-10"),
                      ("zzz-device", "type-node", None)],
        "facts": [("aaa-cato", "aaa-cato", "2026-10-10")],
    }
    db = _FakeDB(store)
    monkeypatch.setattr(M, "_entity_owned_by", lambda d, eid, uid: True)
    hit = M._ranked_alias_anchor(db, "laptop", "u-1")
    assert hit is not None
    assert hit[0] == "zzz-device", hit


def test_ranked_alias_sweep_no_other_tombstone_blind_probe():
    """Sweep: every facts probe in the helper filters the facts tombstone
    (superseded_at); entity_attributes already did. entity_aliases and entities carry
    NO tombstone column (migrations 001/008: hard rows, FK CASCADE) so there is no
    deleted_at filter to add anywhere else."""
    import inspect
    import src.api.main as M
    helper_src = inspect.getsource(M._ranked_alias_anchor)
    n_facts_probes = helper_src.count("FROM facts")
    assert n_facts_probes == 2, helper_src
    assert helper_src.count("superseded_at IS NULL") == 3  # attrs + 2 facts probes
    import pathlib
    mig = ""
    for m in pathlib.Path(WORKSPACE, "migrations").glob("*.sql"):
        mig += m.read_text(encoding="utf-8", errors="replace")
    assert "deleted_at" not in "".join(
        line for line in mig.splitlines() if "entity_aliases" in line)
    assert "deleted_at" not in "".join(
        line for line in mig.splitlines() if line.strip().startswith("ALTER TABLE entities"))


def test_prepass_wired_before_the_ngram_loop():
    """The evidence-ranked pre-pass sits INSIDE the n-gram lane, BEFORE the
    first-match-wins loop, and only overrides on rank class 0."""
    import inspect
    import src.api.main as M
    src = inspect.getsource(M.resolve_anchor)
    i_pre = src.index("ranked_alias_prefpass_failed")
    i_loop = src.index("for n in range(_max_n, 0, -1):")
    assert i_pre < i_loop
    assert "_rank_best[1] == 0" in src
    assert "_ranked_alias_anchor(db, _rphrase, user_id)" in src
    helper_src = inspect.getsource(M._ranked_alias_anchor)
    assert "instance_of" in helper_src
    assert "_entity_owned_by" in helper_src

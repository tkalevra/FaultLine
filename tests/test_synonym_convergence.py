"""Engine synonym-convergence unit tests (deterministic gates, mocked DB + LLM).

Covers the DETERMINISTIC-GATED convergence pass that collapses a LIFTED novel rel onto a SEEDED rel
when it is a SEMANTIC synonym the morphology-fold could NOT see (marry→spouse), behind three
fail-closed gates:

  GATE 1 — type-constraint match (no LLM): an observed-type mismatch (affect→located_in) is killed
           WITHOUT ever calling the LLM; the ANY/ANY catch-all (related_to) is excluded.
  GATE 2 — LLM proposes STRICT equivalence only; fail-safe on no answer / LLM unavailable.
  GATE 3 — structural sanity (symmetry / hierarchy parity) before a reversible alias write.

The DB is mocked with a substring-dispatch fake cursor (the pass uses Postgres-only SQL); the
canonical helpers and the LLM proposal are monkeypatched at their module boundaries so the
deterministic decision logic is exercised in isolation. Bias: FALSE-NEGATIVE over FALSE-POSITIVE.
"""
import importlib

import pytest

emb = importlib.import_module("src.re_embedder.embedder")
canon = importlib.import_module("src.ontology.canonical")


# ── Pure deterministic gate helpers (Gate 1 math) ───────────────────────────────────────────────

def test_type_set_normalizes_and_drops_empty():
    assert emb._type_set(["Person", "Organization"]) == {"person", "organization"}
    assert emb._type_set(None) == set()
    assert emb._type_set([" ", "", "Location"]) == {"location"}


def test_side_compatible_any_autopass():
    # An ANY side auto-passes (does not validate) — even with no observed types.
    assert emb._side_compatible(set(), {"any"}) is True
    assert emb._side_compatible({"person"}, {"any"}) is True


def test_side_compatible_constrained_requires_subset_and_nonempty():
    # Constrained side: observed must be non-empty AND a subset of allowed → FAIL-CLOSED otherwise.
    assert emb._side_compatible({"person"}, {"person"}) is True
    assert emb._side_compatible({"object"}, {"location"}) is False   # affect→located_in tail mismatch
    assert emb._side_compatible(set(), {"person"}) is False          # unresolved types → fail-closed
    assert emb._side_compatible({"person", "object"}, {"person"}) is False


def test_seed_is_discriminating_excludes_any_any_catchall():
    # related_to (ANY/ANY) is NOT a valid target — no type rail to validate against.
    assert emb._seed_is_discriminating({"any"}, {"any"}) is False
    assert emb._seed_is_discriminating(set(), set()) is False
    # spouse (Person/Person) and located_in (ANY/Location) DO discriminate.
    assert emb._seed_is_discriminating({"person"}, {"person"}) is True
    assert emb._seed_is_discriminating({"any"}, {"location"}) is True


# ── Fake DB (substring-dispatch) ────────────────────────────────────────────────────────────────

# Seeded rel universe returned by the "source IN seeded" query. Column order matches the SELECT:
# (rel_type, head_types, tail_types, is_symmetric, is_hierarchy_rel, inverse_rel_type,
#  natural_language, category)
_SEEDED_ROWS = [
    ("spouse", ["Person"], ["Person"], True, False, "spouse", "X and Y are married", "family"),
    ("knows", ["Person"], ["Person", "Organization"], True, False, "knows", "X knows Y", "social"),
    ("has_pet", ["Person"], ["Animal"], False, False, None, "X has pet Y", "family"),
    ("located_in", ["ANY"], ["Location"], False, False, None, "X located in Y", "location"),
    ("related_to", ["ANY"], ["ANY"], False, False, None, "X related to Y", "association"),
]


class _FakeCursor:
    def __init__(self, state):
        self.state = state
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._rows = []
        if "FROM rel_types WHERE lower(COALESCE(source, '')) NOT IN" in s:
            self._rows = list(self.state["novel_rows"])
        elif "FROM rel_types WHERE lower(COALESCE(source, '')) IN" in s:
            self._rows = list(_SEEDED_ROWS)
        elif "FROM rel_type_aliases WHERE alias" in s:
            self._rows = [(1,)] if params and params[0] in self.state["existing_aliases"] else []
        elif "FROM ontology_evaluations" in s and "left_novel" in s:
            # FAITHFUL to the real probe, which carries `AND re_embedder_decision = 'left_novel'`
            # — so the DATABASE filters on the decision value, and a memo written with any other
            # value does NOT skip. Modelling this as decision-blind (the previous shape) made the
            # rig unable to see the difference between a door that is shut and one left open.
            self._rows = ([(1,)]
                          if params and self.state["memo_decisions"].get(params[0]) == "left_novel"
                          else [])
        elif s.startswith("INSERT INTO ontology_evaluations"):
            self.state["memos"].add(params[0])
            # Record WHICH decision was written. The skip-probe above matches the literal
            # 'left_novel' only, so a memo carrying any other value leaves the door open.
            self.state["memo_decisions"][params[0]] = (
                "left_novel" if "'left_novel'" in s else params[4])
        # UPDATE rel_types / UPDATE entity_taxonomies / SAVEPOINT / RELEASE / SET search_path → no-op

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, state):
        self.state = state
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


@pytest.fixture
def rig(monkeypatch):
    """Wire the pass onto a fake DB + fake canonical/LLM boundaries. Returns a state dict the test
    inspects (recorded aliases, which rels reached the LLM, memos)."""
    state = {
        "novel_rows": [],
        "existing_aliases": set(),
        "memos": set(),
        "memo_decisions": {},
        "observed": {},          # rel -> (subj_set, obj_set)
        "llm": {},               # rel -> proposal dict (or KeyError → treated as no-answer {})
        "recorded_aliases": [],  # (alias, canonical)
        "llm_seen": [],          # rels the LLM was actually asked about
    }

    monkeypatch.setattr(emb, "_ENGINE_SYNONYM_CONVERGENCE", True)

    # Morphology-fold MISSED everything (this pass owns semantic, not morphological, synonyms).
    monkeypatch.setattr(canon, "resolve_seeded_by_morphology", lambda rel, dsn, sch: None)
    # Identity resolver over the candidate set — resolve_canonical returns the token as canonical.
    monkeypatch.setattr(
        canon, "resolve_canonical",
        lambda rel, dsn=None, sch=None: {"canonical": (rel or "").strip().lower()},
    )
    monkeypatch.setattr(canon, "normalize_rel", lambda s: (s or "").strip().lower())
    monkeypatch.setattr(canon, "reset_caches", lambda sch=None: None)

    def _rec(alias, canonical, inv, source, dsn, sch):
        state["recorded_aliases"].append((alias, canonical))
        state["existing_aliases"].add(alias)
        return True

    monkeypatch.setattr(canon, "record_alias", _rec)

    # Observed types come from the state map (bypasses the facts/entities join SQL).
    def _observed(db, rel, which):
        subj, obj = state["observed"].get(rel, (set(), set()))
        return set(subj) if which == "subject" else set(obj)

    monkeypatch.setattr(emb, "_observed_entity_types", _observed)

    # LLM proposal: record the rel and return the canned proposal ({} = unavailable / no-answer).
    #
    # CONTRACT UPDATE (non-answer memo fix): the helper now returns (proposal, answered).
    # The stub derives `answered` with PRODUCTION's own predicate rather than hard-coding
    # True, so this rig keeps telling the truth about a canned {} — which this stub's own
    # comment above has always called "unavailable / no-answer", and which the lane
    # nevertheless used to memoize as a permanent 'left_novel' verdict.
    def _llm(rel, nl, subj, obj, candidates, url):
        state["llm_seen"].append(rel)
        state["_last_candidates"] = set(candidates)
        proposal = state["llm"].get(rel, {})
        return proposal, emb._brain_answered(proposal)

    monkeypatch.setattr(emb, "_llm_propose_equivalence", _llm)

    return state


def _run(state):
    conn = _FakeConn(state)
    return emb.converge_lifted_synonyms(conn, "dsn", "faultline_t", "http://llm")


# ── GATE 1 — type mismatch is rejected WITHOUT the LLM (the critical regression) ─────────────────

def test_gate1_type_mismatch_rejected_without_llm(rig):
    # affect(cve=Object, product=Object) — no seeded rel's type rail admits it (located_in tail is
    # Location, spouse/knows/has_pet head is Person, related_to is ANY/ANY excluded).
    rig["novel_rows"] = [("affect", False, False, "X affect Y", "pending_placement")]
    rig["observed"] = {"affect": ({"object"}, {"object"})}
    stats = _run(rig)
    assert stats["converged"] == 0
    assert rig["recorded_aliases"] == []      # nothing aliased
    assert "affect" not in rig["llm_seen"]    # Gate 1 killed it BEFORE any LLM call


def test_gate1_repeat_5x_verb_lift_preserved(rig):
    # Determinism: the type reject holds across repeats — no stochastic path to converge affect.
    for _ in range(5):
        rig["recorded_aliases"].clear()
        rig["llm_seen"].clear()
        rig["novel_rows"] = [("affect", False, False, "X affect Y", "pending_placement")]
        rig["observed"] = {"affect": ({"object"}, {"object"})}
        stats = _run(rig)
        assert stats["converged"] == 0
        assert rig["recorded_aliases"] == []
        assert rig["llm_seen"] == []


# ── GATE 2 — a true, type-compatible synonym converges and records the alias ─────────────────────

def test_gate2_equivalence_accept_records_alias(rig):
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["llm"] = {"marry": {"equivalent_to": "spouse", "confidence": 0.95, "reason": "same"}}
    stats = _run(rig)
    assert stats["converged"] == 1
    assert ("marry", "spouse") in rig["recorded_aliases"]
    # Gate 1 left a SMALL type-compatible set (Person/Person seeds), not the whole seed table.
    assert rig["_last_candidates"] == {"spouse", "knows"}


def test_gate2_related_but_distinct_left_novel_and_memoized(rig):
    # LLM says null (related-but-distinct) → leave novel + memoize so it is not re-LLM'd next cycle.
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["llm"] = {"marry": {"equivalent_to": None, "confidence": 0.0, "reason": "distinct"}}
    stats = _run(rig)
    assert stats["converged"] == 0
    assert stats["left_novel"] == 1
    assert rig["recorded_aliases"] == []
    assert "marry" in rig["memos"]            # memo parked → skipped on the next cycle


def test_gate2_low_confidence_left_novel(rig):
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["llm"] = {"marry": {"equivalent_to": "spouse", "confidence": 0.40}}  # below 0.75 floor
    stats = _run(rig)
    assert stats["converged"] == 0
    assert rig["recorded_aliases"] == []


def test_llm_unavailable_fails_closed(rig):
    # LLM returns {} (unavailable / unparseable) → leave novel, never converge.
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["llm"] = {}  # no proposal
    stats = _run(rig)
    assert stats["converged"] == 0
    assert rig["recorded_aliases"] == []


# ── Fail-closed: unresolved observed types never reach the LLM ───────────────────────────────────

def test_unresolved_types_fail_closed(rig):
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": (set(), set())}   # nothing resolved
    stats = _run(rig)
    assert stats["converged"] == 0
    assert rig["llm_seen"] == []                  # no candidate → no LLM
    assert rig["recorded_aliases"] == []


# ── GATE 3 — symmetric novel usage never collapses onto an asymmetric seed ──────────────────────

def test_gate3_symmetric_novel_onto_asymmetric_seed_rejected(rig):
    # A symmetric novel rel typed Person/Animal would be type-compatible ONLY with has_pet
    # (asymmetric) — Gate 3 must refuse to drop the reverse edge.
    rig["novel_rows"] = [("cohabits_pet", True, False, "X cohabits pet Y", "pending_placement")]
    rig["observed"] = {"cohabits_pet": ({"person"}, {"animal"})}
    rig["llm"] = {"cohabits_pet": {"equivalent_to": "has_pet", "confidence": 0.9}}
    stats = _run(rig)
    assert stats["converged"] == 0
    assert stats["left_novel"] == 1
    assert rig["recorded_aliases"] == []


# ── Skip already-aliased / already-memoized rels (no churn) ──────────────────────────────────────

def test_already_aliased_is_skipped(rig):
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["existing_aliases"] = {"marry"}
    rig["llm"] = {"marry": {"equivalent_to": "spouse", "confidence": 0.95}}
    stats = _run(rig)
    assert stats["converged"] == 0
    assert rig["llm_seen"] == []                  # skipped before Gate 1 / LLM


def test_flag_off_is_noop(rig, monkeypatch):
    monkeypatch.setattr(emb, "_ENGINE_SYNONYM_CONVERGENCE", False)
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["llm"] = {"marry": {"equivalent_to": "spouse", "confidence": 0.95}}
    stats = _run(rig)
    assert stats == {"converged": 0, "scanned": 0, "left_novel": 0,
                     "brain_unavailable": 0, "errors": 0}
    assert rig["recorded_aliases"] == []


# ── A NON-ANSWER IS NOT A VERDICT (the permanent-memo defect) ────────────────────────────────

def test_a_brain_nonanswer_does_not_park_a_left_novel_memo(rig):
    """MEASURED DEFECT. ``{}`` is what the LLM stack returns when the brain was never reached
    (saas_no_brain_failclosed) or answered nothing (empty / unparseable content) — this rig's own
    stub has always labelled it "unavailable / no-answer". The lane nevertheless memoized it as
    'left_novel', and its skip-probe honours that literal with NO TTL and NO retry, excluding the
    rel from convergence forever.

    On production 2026-08-27: 512 of 707 'left_novel' memos, across 9 of 22 tenants, carry
    ``ans='' conf=0.00`` — while ``llm_call.context_overrun_preflight operation=ENRICHMENT
    window=448`` fired 198 times in 72h, refusing before the wire.

    RFC 9520 §1 separates "a response indicating the requested data does not exist" from "a
    non-response due to a resolution failure"; caching the second as the first is the defect.
    """
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["llm"] = {}                      # → the stub returns {} → a NON-answer
    stats = _run(rig)
    assert rig["llm_seen"] == ["marry"], "the lane must still have tried to ask"
    assert stats["converged"] == 0
    assert stats["left_novel"] == 0, "a non-answer is not a 'left novel' verdict"
    assert stats["brain_unavailable"] == 1
    assert rig["recorded_aliases"] == []
    assert rig["memo_decisions"].get("marry") != "left_novel", (
        "the skip-probe matches 'left_novel' exactly — writing it here shuts the door forever")


def test_a_considered_null_is_still_memoized(rig):
    """The other half, and the one a careless fix breaks: the brain ANSWERING "not equivalent"
    is a real verdict. It must still be memoized, or every sweep re-asks the same question."""
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["llm"] = {"marry": {"equivalent_to": None, "confidence": 0.9, "reason": "distinct"}}
    stats = _run(rig)
    assert stats["left_novel"] == 1
    assert stats["brain_unavailable"] == 0
    assert rig["memo_decisions"].get("marry") == "left_novel"


def test_the_unavailable_memo_is_re_offered_on_the_next_cycle(rig):
    """The point of not writing 'left_novel': the NEXT sweep must ask again. Cycle 1 hits an
    unreachable brain; cycle 2 (brain back) must reach the LLM gate a second time and converge."""
    rig["novel_rows"] = [("marry", False, False, "X marry Y", "pending_placement")]
    rig["observed"] = {"marry": ({"person"}, {"person"})}
    rig["llm"] = {}
    _run(rig)
    assert rig["llm_seen"] == ["marry"]
    rig["llm"] = {"marry": {"equivalent_to": "spouse", "confidence": 0.95, "reason": "same"}}
    stats = _run(rig)
    assert rig["llm_seen"] == ["marry", "marry"], (
        "the memo written for a non-answer must NOT be honoured by the skip-probe")
    assert stats["converged"] == 1
    assert [a[0] for a in rig["recorded_aliases"]] == ["marry"]

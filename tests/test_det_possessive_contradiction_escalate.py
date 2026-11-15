# -*- coding: utf-8 -*-
"""THE STORED-CONTRADICTION ESCALATION + THE BELONG_TO SHAPE GUARD (BUG-R4-2).

TWO INDEPENDENT CONVICTIONS (the owner's live seat + the round-4 corpus control
tools/regression_results/results-20261114T2103Z-*.json / r4j_autopsy.md) convicted the same
arm: corrections phrased as PLAIN POSSESSIVES ("Fraggle is Mars's morkie.", "Goose is Cyrus's
cat.", "Sophia is Desmonde's corn snake.") walked in as NEW statements —

  * ROUTER HALF: the deterministic-first possessive arm (tier-2) short-circuits to STATEMENT
    and SKIPS the GLiNER2 / below-gate INTENT_ADJUDICATION layers. Fix:
    DET_POSSESSIVE_CONTRADICTION_ESCALATE (default ON; OFF → byte-parity) — ONE bounded seat
    probe before the shortcut; a LIVE possession/kin row with a DIFFERENT owner stands the
    shortcut down (the turn falls through to the ladder a cued correction rides).
  * EXTRACTION HALF: the sortal genitive leg minted the CLASS-SUBJECT junk twin
    (class, related_to/belongs_to, owner) and left the NAME as uncovered residue. Fix:
    BELONG_TO_SHAPE_GUARD (default ON; OFF → byte-identical) — the rows file per the
    pet-registry discipline: (name, instance_of, class) + (owner, has_pet, name); the
    class-subject row is NEVER minted.

Same harness as tests/test_intent_deterministic_first.py: POSTGRES_DSN cleared (every
DB-touching layer fail-opens by design); the probe is monkeypatched for the routing legs and
unit-tested pure for the adjudication legs.
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("WGM_LLM_MODEL", "qwen/qwen3.5-9b")

import src.api.main as M  # noqa: E402
import src.api.llm_calls as LC  # noqa: E402
import src.extraction.linguistics as L  # noqa: E402

os.environ.pop("POSTGRES_DSN", None)

_UID = "5d1c2b3a-2222-4000-8000-abcdefabcdef"

# THE OWNER'S LIVE SEAT SHAPES (the conviction fixtures).
OWNER_MORKIE = "Fraggle is Mars's morkie."
OWNER_CAT = "Goose is Cyrus's cat."
OWNER_SNAKE = "Sophia is Desmonde's corn snake."
OWNER_SHAPES = [OWNER_MORKIE, OWNER_CAT, OWNER_SNAKE]
# The same wound phrased speaker-anchored — the shape tier-2 actually short-circuits on.
SELF_POSSESSIVE = "Fraggle is my morkie."
HAPPY_POSSESSIVE = "My wife's name is Seraphine."
# The corpus control's miss-set source turns (qF1/qF3 of the BUG-R4-2 control artifact).
CORPUS_QF1 = ("Note to self, and I keep re-learning this: after rebuilding an image with "
              "the same tag, docker compose up -d does nothing to running containers — you "
              "need the --force-recreate flag or the stack silently keeps the old container.")
CORPUS_QF3 = ("House rule after last month's incident: force-push only with "
              "--force-with-lease, never a bare --force — the lease check has already saved "
              "the release branch once.")


@pytest.fixture(autouse=True)
def _db_free(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)
    monkeypatch.setattr(M, "CORRECTION_MARKER_GATE", True)
    monkeypatch.setattr(M, "INTENT_DETERMINISTIC_FIRST", True)
    monkeypatch.setattr(M, "DET_POSSESSIVE_CONTRADICTION_ESCALATE", True)


async def _ready(user_id, endpoint):
    return None


class _GlinerStub:
    def __init__(self, label, confidence):
        self.desc = M._build_intent_descriptions_for_gliner2()[label]
        self.confidence = confidence

    def classify_text(self, text, labels, include_confidence=True):
        return {"intent": {"label": self.desc, "confidence": self.confidence}}


class _GlinerBoom:
    def classify_text(self, text, labels, include_confidence=True):
        raise AssertionError("GLiNER2 consulted — the deterministic tier did not own the turn")


@pytest.fixture()
def llm_boom(monkeypatch):
    async def _boom(**kwargs):
        raise AssertionError(f"LLM called with operation={kwargs.get('operation')!r}")

    monkeypatch.setattr(LC, "call_llm_with_retry_async", _boom)
    return None


# ── A: ROUTER — the happy path keeps the tier-2 shortcut (zero extra LLM) ────────────────


async def test_happy_path_shortcut_stands(llm_boom, monkeypatch):
    """No contradicting live row (probe fail-opens with no DSN) → the shortcut stands,
    STATEMENT @0.85, zero LLM, zero GLiNER2."""
    resp = await M.classify_intent({"text": HAPPY_POSSESSIVE}, user_id=_UID,
                                   model=_GlinerBoom())
    assert resp["intent"] == "STATEMENT"
    assert resp["confidence"] == pytest.approx(0.85)


async def test_flag_off_is_byte_parity_zero_queries(llm_boom, monkeypatch):
    """OFF leg: the probe NEVER runs (zero queries) and tier-2 owns the turn byte-for-byte."""
    def _boom_probe(uid, text):
        raise AssertionError("probe ran with the flag OFF — byte-parity broken")

    monkeypatch.setattr(M, "DET_POSSESSIVE_CONTRADICTION_ESCALATE", False)
    monkeypatch.setattr(M, "_det_possessive_contradiction_probe", _boom_probe)
    resp = await M.classify_intent({"text": SELF_POSSESSIVE}, user_id=_UID,
                                   model=_GlinerBoom())
    assert resp["intent"] == "STATEMENT"
    assert resp["confidence"] == pytest.approx(0.85)


async def test_probe_runs_once_on_the_shortcut_band(llm_boom, monkeypatch):
    """ON leg, metadata-coupling: the probe runs exactly ONCE, only when tier-2 was about to
    short-circuit — and a NO-contradiction verdict keeps STATEMENT."""
    calls = []

    def _probe(uid, text):
        calls.append(text)
        return None

    monkeypatch.setattr(M, "_det_possessive_contradiction_probe", _probe)
    resp = await M.classify_intent({"text": SELF_POSSESSIVE}, user_id=_UID,
                                   model=_GlinerBoom())
    assert calls == [SELF_POSSESSIVE]
    assert resp["intent"] == "STATEMENT"


async def test_probe_failopen_keeps_the_shortcut(llm_boom, monkeypatch):
    """Fail-safe: a probe ERROR never blocks — the shortcut stands (never a drop)."""
    def _probe(uid, text):
        raise RuntimeError("seat unreachable")

    monkeypatch.setattr(M, "_det_possessive_contradiction_probe", _probe)
    resp = await M.classify_intent({"text": SELF_POSSESSIVE}, user_id=_UID,
                                   model=_GlinerBoom())
    assert resp["intent"] == "STATEMENT"


# ── B: ROUTER — the stored-contradiction escalation ─────────────────────────────────────


async def test_contradiction_stands_the_shortcut_down(monkeypatch):
    """THE FIX: a live (mars, has_pet, fraggle) row contradicting "Fraggle is my morkie."
    stands tier-2 down — GLiNER2 + the ladder own the turn (here: CORRECTION@0.95)."""
    def _probe(uid, text):
        return {"rel_type": "has_pet", "owner": "mars", "anchor": "fraggle"}

    monkeypatch.setattr(M, "_det_possessive_contradiction_probe", _probe)
    resp = await M.classify_intent({"text": SELF_POSSESSIVE}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.95))
    assert resp["intent"] == "CORRECTION"


async def test_escalation_flag_off_short_circuits_anyway(monkeypatch):
    """The same contradicting seat with the flag OFF → byte-parity: tier-2 still swallows the
    turn (the documented OFF-leg behavior this flag exists to repair)."""
    def _boom_probe(uid, text):
        raise AssertionError("probe ran with the flag OFF")

    monkeypatch.setattr(M, "DET_POSSESSIVE_CONTRADICTION_ESCALATE", False)
    monkeypatch.setattr(M, "_det_possessive_contradiction_probe", _boom_probe)
    resp = await M.classify_intent({"text": SELF_POSSESSIVE}, user_id=_UID,
                                   model=_GlinerBoom())
    assert resp["intent"] == "STATEMENT"


@pytest.mark.parametrize("text,name,owner", [
    (OWNER_MORKIE, "fraggle", "mars"),
    (OWNER_CAT, "goose", "cyrus"),
    (OWNER_SNAKE, "sophia", "desmonde"),
])
async def test_owner_shapes_keep_the_llm_ladder(text, name, owner, monkeypatch):
    """The owner's three live-seat shapes are NOT speaker-anchored (tier-2's gate declines
    them by morphology) — they keep GLiNER2 + the adjudication ladder. On a fixture seat
    holding the contradicting original, the third-person leg of the escalation probe admits
    the CORRECTION label the marker floor would otherwise refuse: they route CORRECTION."""
    def _probe(uid, txt):
        return {"rel_type": "has_pet", "owner": "desmonde", "anchor": name}

    monkeypatch.setattr(M, "_det_possessive_contradiction_probe", _probe)
    resp = await M.classify_intent({"text": text}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.95))
    assert resp["intent"] == "CORRECTION"


@pytest.mark.parametrize("text", OWNER_SHAPES)
async def test_owner_shapes_without_contradiction_stay_statement(text):
    """The same shapes on a seat with NO contradicting live row (probe fail-open) keep
    today's verdict — GLiNER2's marker-gated STATEMENT. Byte-parity outside the wound."""
    resp = await M.classify_intent({"text": text}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.95))
    assert resp["intent"] == "STATEMENT"


# ── C: PROBE — the pure adjudication + fail-open legs ───────────────────────────────────


def _shape(name="fraggle", owner="my", self_owner=True, kind="morkie"):
    return L.PossessiveCopulaShape(name=name, owner=owner,
                                   owner_is_self=self_owner, kind=kind)


def test_rows_contradict_different_owner():
    rows = [{"rel_type": "has_pet", "subject_id": "s1", "object_id": "o1",
             "sub_alias": "mars", "obj_alias": "fraggle", "self_ids": []}]
    v = M._possessive_rows_contradict(_shape(), rows)
    assert v and v["owner"] == "mars" and v["rel_type"] == "has_pet"


def test_rows_contradict_junk_twin_row():
    """A prior class-subject junk twin (morkie, related_to, mars) also escalates — the wound's
    own artifact must trip the probe."""
    rows = [{"rel_type": "related_to", "subject_id": "s1", "object_id": "o1",
             "sub_alias": "morkie", "obj_alias": "mars", "self_ids": []}]
    v = M._possessive_rows_contradict(_shape(), rows)
    assert v and v["owner"] == "mars"


def test_rows_no_contradiction_same_owner():
    rows = [{"rel_type": "has_pet", "subject_id": "s1", "object_id": "o1",
             "sub_alias": "mars", "obj_alias": "fraggle", "self_ids": []}]
    assert M._possessive_rows_contradict(
        _shape(name="fraggle", owner="mars", self_owner=False, kind="morkie"), rows) is None


def test_rows_no_contradiction_self_row_on_self_statement():
    rows = [{"rel_type": "has_pet", "subject_id": "selfid", "object_id": "o1",
             "sub_alias": "chris", "obj_alias": "fraggle", "self_ids": ["selfid"]}]
    assert M._possessive_rows_contradict(_shape(), rows) is None


def test_rows_no_contradiction_empty():
    assert M._possessive_rows_contradict(_shape(), []) is None


def test_probe_no_dsn_failopen():
    assert M._det_possessive_contradiction_probe(_UID, OWNER_MORKIE) is None


def test_probe_no_shape_never_touches_db(monkeypatch):
    """A non-possessive-copula turn (the naming construction) declines BEFORE any connection."""
    import psycopg2 as _pg2

    def _boom(*a, **k):
        raise AssertionError("DB touched for a shape-less turn")

    monkeypatch.setenv("POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(_pg2, "connect", _boom)
    assert M._det_possessive_contradiction_probe(_UID, HAPPY_POSSESSIVE) is None


# ── D: EXTRACTION — the belong_to shape guard (the junk-twin refusal) ───────────────────


def _facts(text):
    return [(f.subject, f.rel_type, f.object)
            for f in L.derive_sentence_facts(text, datetime.now())]


@pytest.mark.parametrize("text,name,kind,owner", [
    (OWNER_MORKIE, "fraggle", "morkie", "mars"),
    (OWNER_CAT, "goose", "cat", "cyrus"),
    (OWNER_SNAKE, "sophia", "corn snake", "desmonde"),
])
def test_guard_files_pet_registry_rows(text, name, kind, owner):
    """THE FIX (extraction half): the pet-registry rows land and the class-subject
    (class, related_to/belongs_to, owner) junk twin is NEVER minted."""
    fs = _facts(text)
    assert (name, "instance_of", kind) in fs
    assert (owner, "has_pet", name) in fs
    assert (kind, "related_to", owner) not in fs
    assert not any(s == kind and o == owner for s, _, o in fs)


@pytest.mark.parametrize("text,kind,owner", [
    (OWNER_MORKIE, "morkie", "mars"),
    (OWNER_CAT, "cat", "cyrus"),
    (OWNER_SNAKE, "corn snake", "desmonde"),
])
def test_guard_off_is_byte_identical(monkeypatch, text, kind, owner):
    """OFF leg: the legacy class-subject loose link returns byte-for-byte."""
    monkeypatch.setattr(L, "BELONG_TO_SHAPE_GUARD", False)
    fs = _facts(text)
    assert (kind, "related_to", owner) in fs


def test_guard_leaves_first_person_possessive_untouched():
    """The first-person leg (already correct) is untouched: (user, owns, morkie) + the name
    alias — no pet-registry duplication."""
    fs = _facts("Fraggle is my morkie.")
    assert ("user", "owns", "morkie") in fs
    assert ("morkie", "also_known_as", "fraggle") in fs
    assert ("my", "has_pet", "fraggle") not in fs


def test_guard_leaves_relnoun_genitive_untouched():
    """'X's gps' (a relational head) keeps the relnoun leg's (gps, part_of, X)."""
    fs = _facts("The tracker is the truck's gps.")
    assert any(r == "part_of" for _, r, _ in fs)


def test_guard_leaves_naming_construction_untouched():
    assert _facts("My dog's name is Biscuit.") == [("biscuit", "related_to", "user")]


def test_guard_negated_possessive_copula_declines():
    """A denied tie never triggers the fresh pet-registry mint (the negation machinery owns it)."""
    fs = _facts("Fraggle is not Mars's morkie.")
    assert ("mars", "has_pet", "fraggle") not in fs


# ── E: the shared grammar (router probe ↔ deriver guard cannot drift) ───────────────────


@pytest.mark.parametrize("text,name,owner,self_owner,kind", [
    (OWNER_MORKIE, "fraggle", "mars", False, "morkie"),
    (OWNER_CAT, "goose", "cyrus", False, "cat"),
    (OWNER_SNAKE, "sophia", "desmonde", False, "corn snake"),
    (SELF_POSSESSIVE, "fraggle", "my", True, "morkie"),
])
def test_shape_parses(text, name, owner, self_owner, kind):
    s = L.analyze_possessive_copula_shape(text)
    assert s is not None
    assert (s.name, s.owner, s.owner_is_self, s.kind) == (name, owner, self_owner, kind)


@pytest.mark.parametrize("text", [HAPPY_POSSESSIVE, "I live at 12 fog lane.",
                                  "Aurora is not my dog."])
def test_shape_declines_non_possessive_copula(text):
    assert L.analyze_possessive_copula_shape(text) is None


# ── F: corpus-side routing verdicts (the BUG-R4-2 control's miss set, qF1/qF3) ──────────


@pytest.mark.parametrize("text,expected", [
    (CORPUS_QF1, "STATEMENT"),   # speaker-anchored ("I keep re-learning") → tier-2 owns it
    (CORPUS_QF3, "STATEMENT"),   # not speaker-anchored → GLiNER2's own verdict
])
async def test_corpus_shapes_routing_verdicts_unchanged(text, expected, monkeypatch):
    """qF1/qF3's source turns (the BUG-R4-2 control's miss set) route exactly as before the
    fix: neither is a possessive copula, so the escalation probe declines on grammar alone
    (zero DB) and the verdicts are unchanged — qF1 by the tier-2 shortcut, qF3 by GLiNER2."""
    probe_calls = []

    def _probe(uid, txt):
        probe_calls.append(txt)
        return None

    monkeypatch.setattr(M, "_det_possessive_contradiction_probe", _probe)
    resp = await M.classify_intent({"text": text}, user_id=_UID,
                                   model=_GlinerStub("STATEMENT", 0.99))
    assert resp["intent"] == expected
    if probe_calls:
        # qF1 only (the tier-2 band consults the probe once, metadata-coupled);
        # neither turn parses as a possessive copula → no contradiction is possible.
        assert len(probe_calls) == 1

"""THE HARD LINE — ladder-on-value guard (LV1/LV2/LV3 lane tests).

Pins the ingest-time guard `src/api/hardline_guard.py::refuses_subclass_rung` and its wiring at
the REAL mint seams (`main.py::_attach_wordnet_hypernym_ladder`, the largest census lane):

  LV1 — no subclass_of rung lands on a user VALUE or a NAMED INSTANCE:
        each evidence arm (instance subject, naming subject, attribute holder,
        user_stated value-slot object, value-side stamp, user-asserted surface with
        no common-noun sense) refuses the rung.
  LV2 — genuine TYPE ladders still grow: a clean type node (no memory-side evidence,
        common-noun surface) is ALLOWED; a user_stated CONSTRAINED-tail object whose
        surface IS a common noun (`owns→dog`) is ALLOWED. A fix that blinds laddering fails.
  LV3 — the guard fails CLOSED on unstamped nodes: `node_role IS NULL` never admits by
        itself (the decision comes from structural evidence), and any read error /
        unknown node REFUSES the rung (the OPPOSITE polarity of node_role, which fails
        open to "not protected").

NON-VACUOUS BY CONSTRUCTION: `test_seam_wordnet_ladder_red_when_guard_reverted` reverts the
guard via its single rollback lever (`HARDLINE_LADDER_GUARD=0`) and asserts the SAME input
then STAGES rungs — i.e. the LV1 assertions above are red exactly when the guard is reverted.

PURE tests — no DB, no network, no GLiNER2, no LLM, no nltk (the lexical oracle is stubbed).

Run: python3 tools/fltest.py --bug ladder-on-value --test tests/test_hardline_ladder_guard.py
     (tests/ is gitignored → git add -f)
"""
import os

import pytest

import src.api.main as m
from src.api import hardline_guard as hg
from src.api import wordnet_ladder


# ── fakes ────────────────────────────────────────────────────────────────────────────────────

_NO_ROW = object()  # sentinel: rel absent from rel_types (tail lookups)


class _FakeCursor:
    """Answers the guard's ONE evidence query (detected by its `e.node_role` marker) with a
    fixed row; answers rel_types tail lookups with the conn's `tail`; every other SELECT
    fetches None; an optional `boom` flag breaks the evidence query to prove fail-closed."""

    def __init__(self, row, boom=False, tail=None, tails=None, aliases=None):
        self._row = row          # tuple(role, instance, naming, attr, slot, asserted) | None
        self._boom = boom
        self._tail = tail        # list | None | _NO_ROW for tail_types lookups
        self._tails = tails or {}      # {rel: [tails]} for fetchall-style rel_types queries
        self._aliases = aliases or {}  # {alias: canonical} for rel_type_aliases queries
        self.last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql

    def fetchone(self):
        if self._boom and "e.node_role" in self.last_sql:
            raise RuntimeError("simulated evidence read failure")
        if "e.node_role" in self.last_sql:
            return self._row
        _norm = " ".join(self.last_sql.split()).upper()
        if "TAIL_TYPES" in _norm and "FROM REL_TYPES" in _norm:
            if self._tail is _NO_ROW:
                return None
            return (self._tail,)
        return None

    def fetchall(self):
        _norm = " ".join(self.last_sql.split()).upper()
        if "FROM REL_TYPE_ALIASES" in _norm:
            return [(a, c) for a, c in (self._aliases or {}).items()]
        if "TAIL_TYPES" in _norm and "FROM REL_TYPES" in _norm:
            return [(r, t) for r, t in (self._tails or {}).items()]
        return []


class _FakeConn:
    def __init__(self, row, boom=False, tail=None, tails=None, aliases=None):
        self._row, self._boom, self._tail = row, boom, tail
        self._tails, self._aliases = tails or {}, aliases or {}
        self.cursors = []

    def cursor(self):
        c = _FakeCursor(self._row, self._boom, self._tail, self._tails, self._aliases)
        self.cursors.append(c)
        return c

    def rollback(self):
        pass


def _row(role=None, inst=False, naming=False, attr=False, slot=False, asserted=False,
        islot=False):
    return (role, inst, naming, attr, slot, asserted, islot)


# ── LV1 — every memory-side evidence arm refuses the rung ─────────────────────────────────────

@pytest.mark.parametrize("row,reason", [
    (_row(inst=True),                    "instance_subject"),   # rex instance_of poodle
    (_row(naming=True),                  "naming_surface"),     # subject of also_known_as/pref_name
    (_row(attr=True),                    "attribute_holder"),   # holds a captured value
    (_row(slot=True),                    "value_slot_object"),  # favorite_colour=blue (user_stated, {ANY} tail)
    (_row(role="value"),                 "node_role=value"),    # stamp corroboration
    (_row(role="name"),                  "node_role=name"),
    (_row(role="both"),                  "node_role=both"),
])
def test_lv1_memory_side_evidence_refuses(row, reason, monkeypatch):
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, why = hg.refuses_subclass_rung(_FakeConn(row), "node-x", "blue")
    assert refuse is True
    assert why == reason


def test_lv1_user_asserted_surface_without_common_sense_refuses(monkeypatch):
    # krellin / zzcanaryglyph: object of a user_stated non-hierarchy edge (owns), no common-noun
    # sense. The lexical arm (R6) — the ONLY arm that catches an unstamped, untyped possession
    # NAME at mint time (the census flagships carry no structural rows at all).
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: False)
    refuse, why = hg.refuses_subclass_rung(
        _FakeConn(_row(role="place", asserted=True)), "node-x", "krellin")
    assert (refuse, why) == (True, "lexical_not_common_type")


def test_lv1_place_stamp_is_not_protective_but_place_name_still_refused_via_structure(monkeypatch):
    # halifax: stamped PLACE (a constrained-tail object) — the stamp alone is NOT protective…
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    refuse, _ = hg.refuses_subclass_rung(_FakeConn(_row(role="place")), "node-x", "halifax")
    assert refuse is False
    # …but the user-asserted + proper-lemma evidence (WordNet stores proper nouns CAPITALIZED)
    # refuses it — this is exactly the wound's `halifax subclass_of provincial capital`.
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: False)
    refuse, why = hg.refuses_subclass_rung(
        _FakeConn(_row(role="place", asserted=True)), "node-x", "halifax")
    assert (refuse, why) == (True, "lexical_not_common_type")


# ── LV2 — genuine TYPE ladders still grow (regression bar) ────────────────────────────────────

def test_lv2_clean_fresh_type_node_is_allowed(monkeypatch):
    # A fresh common-noun type: no evidence rows at all, no stamp — MUST ladder exactly as today.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, why = hg.refuses_subclass_rung(_FakeConn(_row()), "node-x", "poodle")
    assert refuse is False and why is None


def test_lv2_place_stamped_type_is_allowed(monkeypatch):
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, _ = hg.refuses_subclass_rung(_FakeConn(_row(role="place")), "node-x", "dog")
    assert refuse is False


def test_lv2_user_stated_constrained_common_noun_is_allowed(monkeypatch):
    # owns→dog (user_stated, constrained tail): a genuine typed KIND — still ladders.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    refuse, _ = hg.refuses_subclass_rung(
        _FakeConn(_row(asserted=True)), "node-x", "dog")
    assert refuse is False


def test_lv2_engine_grown_node_is_allowed_even_with_odd_surface(monkeypatch):
    # Engine growth (Hearst member / WordNet rung / LLM-climb parent): fact_provenance is
    # llm_inferred, so the user-asserted arm never fires — the ±6 semblance buildout is not blinded.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: False)
    refuse, _ = hg.refuses_subclass_rung(_FakeConn(_row()), "node-x", "zzunknowntype")
    assert refuse is False


# ── Critic round (2026-09-04): the INSTANCE-DIRECTION arm (rel_types.tail_types specific) ─────

def test_critic_rex_shape_refused_via_instance_slot(monkeypatch):
    # THE CRITIC'S LIVE BREACH: "My dog Rex ..." minted (user, has_pet, rex) user_stated; the
    # spine minted NO instance_of, so R1 had no edge; 'rex' IS a lowercase common-noun WordNet
    # lemma (male monarch), so the morphology arm passed it. The rel's OWN metadata
    # (has_pet.tail_types={Animal}) says the object is a NAMED INSTANCE of that type -> refuse.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)  # the homonym resolves
    refuse, why = hg.refuses_subclass_rung(_FakeConn(_row(islot=True)), "node-x", "rex")
    assert (refuse, why) == (True, "instance_slot_object")


def test_critic_halifax_shape_refused_despite_place_stamp(monkeypatch):
    # lives_in.tail_types={Location} (measured live): a place NAME at a specific-tail slot is a
    # named instance of Location — refused EVEN THOUGH the stamp says 'place' (which alone is
    # not protective) and even though a capitalized-lemma check is not needed here.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    refuse, why = hg.refuses_subclass_rung(
        _FakeConn(_row(role="place", islot=True)), "node-x", "halifax")
    assert (refuse, why) == (True, "instance_slot_object")


def test_critic_control_bare_common_noun_without_slot_still_ladders(monkeypatch):
    # LV2 control: {ANY}-tailed or no user_stated object edge (adopt/play measured {ANY} live)
    # -> the instance-direction arm does NOT fire -> a bare common noun still ladders.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    refuse, _ = hg.refuses_subclass_rung(
        _FakeConn(_row(asserted=True)), "node-x", "beagle")
    assert refuse is False


def test_critic_seam_wordnet_ladder_refuses_rex(monkeypatch):
    # Through the REAL wordnet seam: same input as the live breach (user_stated constrained
    # tail, no stamp, homonym common surface) must stage ZERO rungs.
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    rungs, staged = _run_wordnet_ladder(monkeypatch, surface="rex",
                                        rungs=[("rex", "sovereign")], islot=True)
    assert rungs == 0 and staged == 0


def test_critic_seam_red_when_guard_reverted(monkeypatch):
    # Non-vacuity for the new arm: flag-off reverts to the breach behaviour (rungs staged).
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    rungs, staged = _run_wordnet_ladder(monkeypatch, surface="rex",
                                        rungs=[("rex", "sovereign")], guard_flag="0",
                                        islot=True)
    assert rungs >= 1 and staged >= 1


def test_critic_hardening_cursor_argument_raises_loudly(monkeypatch):
    # Critic addendum: a non-connection argument (the round-1 adjudicator passed a CURSOR)
    # must RAISE TypeError, never fold into cursor_error_fail_closed — a correctly-polarized
    # refusal for the wrong reason lets a caller bug ship silently.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    with pytest.raises(TypeError):
        hg.refuses_subclass_rung(_FakeCursor(_row()), "node-x", "dog")


# ── LV3 — fail CLOSED on unstamped / uncertain ────────────────────────────────────────────────

def test_lv3_null_stamp_with_structural_evidence_refuses(monkeypatch):
    # node_role IS NULL means UNSTAMPED, not safe: the decision comes from the structural
    # evidence (here: the node is the subject of an instance_of edge).
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, why = hg.refuses_subclass_rung(_FakeConn(_row(inst=True)), "node-x", "rex")
    assert (refuse, why) == (True, "instance_subject")


def test_lv3_null_stamp_no_evidence_fresh_type_still_ladders(monkeypatch):
    # The complement: NULL stamp + no memory-side evidence + not user-asserted = a fresh type.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, _ = hg.refuses_subclass_rung(_FakeConn(_row()), "node-x", "citrus")
    assert refuse is False


def test_lv3_read_error_fails_closed(monkeypatch):
    # A read error REFUSES (node_role fails open to "not protected" — the guard inverts that).
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, why = hg.refuses_subclass_rung(_FakeConn(_row(), boom=True), "node-x", "dog")
    assert (refuse, why) == (True, "read_error_fail_closed")


def test_lv3_unknown_node_keeps_pre_guard_behaviour(monkeypatch):
    # A node with NO entities row is not memory-side EVIDENCE — every wired seam resolves the
    # subject via the registry in-transaction, so a missing row is ordering, not a memory signal.
    # Refusing here would over-fire (it breaks 4 pinned seam tests) — absence of evidence must
    # not suppress laddering (LV2); only READ ERRORS fail closed.
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, why = hg.refuses_subclass_rung(_FakeConn(None), "node-x", "dog")
    assert (refuse, why) == (False, None)


def test_lv3_user_anchor_refused(monkeypatch):
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, why = hg.refuses_subclass_rung(_FakeConn(_row()), "user", "user", user_id="user")
    assert (refuse, why) == (True, "user_anchor")


# ── the lexical oracle itself (stubbed WordNet — no nltk needed) ──────────────────────────────

class _Lemma:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Synset:
    def __init__(self, lemmas):
        self._lemmas = [_Lemma(n) for n in lemmas]

    def lemmas(self):
        return self._lemmas


class _StubWN:
    def __init__(self, mapping, morphy_map=None):
        self._m = mapping            # key -> [Synset]
        self._mo = morphy_map or {}

    def synsets(self, key, pos="n"):
        return self._m.get(key, [])

    def morphy(self, key, pos="n"):
        return self._mo.get(key)


def test_lexical_oracle_common_vs_proper_vs_unknown(monkeypatch):
    # common: 'dog' → lowercase lemma. proper: 'halifax' → ONLY the capitalized 'Halifax' lemma
    # (WordNet's proper-noun convention). unknown: 'krellin' → no synset at all.
    monkeypatch.setattr(wordnet_ladder, "_WN", _StubWN({
        "dog": [_Synset(["dog"])],
        "halifax": [_Synset(["Halifax"])],
        "dogs": [_Synset(["dog"])],
    }, {"dogs": "dog"}))
    monkeypatch.setattr(wordnet_ladder, "_WN_TRIED", True)
    assert wordnet_ladder.has_common_noun_sense("dog") is True
    assert wordnet_ladder.has_common_noun_sense("dogs") is True       # morphological variant
    assert wordnet_ladder.has_common_noun_sense("halifax") is False   # proper lemma only
    assert wordnet_ladder.has_common_noun_sense("krellin") is False   # no synset


def test_lexical_oracle_unavailable_is_none_and_guard_fails_closed(monkeypatch):
    monkeypatch.setattr(wordnet_ladder, "_WN", None)
    monkeypatch.setattr(wordnet_ladder, "_WN_TRIED", True)
    assert wordnet_ladder.has_common_noun_sense("dog") is None
    # …and the GUARD treats None as NOT-a-common-type → refuse (fail closed).
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    refuse, why = hg.refuses_subclass_rung(
        _FakeConn(_row(asserted=True)), "node-x", "dog")
    assert (refuse, why) == (True, "lexical_not_common_type")


# ── the SEAM: main.py::_attach_wordnet_hypernym_ladder (largest census lane) ─────────────────

class _SeamRegistry:
    def __init__(self):
        self.n = 0

    def resolve(self, user_id, surface):
        self.n += 1
        return f"uuid-{surface.replace(' ', '_')}"


def _run_wordnet_ladder(monkeypatch, guard_flag=None, surface="krellin",
                        rungs=None, entity="node-krellin", otype="Object", islot=False):
    """Drive the REAL seam with the census's flagship miss: an UNSTAMPED-UNTIL-LATER possession
    name (`krellin`, user_stated CONSTRAINED tail `owns`) that every pre-existing guard
    passes (tail constrained; node_role flag off; no naming edge). Returns staged-rung count."""
    monkeypatch.setenv("L4_WORDNET_LADDER", "true")
    monkeypatch.delenv("L4_LADDER_PROTECT_USER_VALUE", raising=False)
    monkeypatch.delenv("VALUE_PLACE_FIRST_CLASS", raising=False)
    if guard_flag is not None:
        monkeypatch.setenv("HARDLINE_LADDER_GUARD", guard_flag)
    else:
        monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    # Old guards neutralized exactly as in their own seam reality: constrained tail (owns) →
    # the metadata distinguisher returns False; node_role flag is OFF on this path.
    monkeypatch.setattr(m, "_rel_tail_is_unconstrained_value", lambda _c, _r: False)
    # The in-flight arm is under test SEPARATELY (r3 lanes); this helper pins the DB arms.
    monkeypatch.setattr(m, "_inflight_rel_binds_instance", lambda _c, _r: False)
    monkeypatch.setattr(m.wordnet_ladder, "hypernym_rungs",
                        lambda *a, **k: (rungs if rungs is not None else [("krellin", "glyph")]))
    monkeypatch.setattr(m.wordnet_ladder, "synset_synonyms", lambda *a, **k: [])
    staged = []
    monkeypatch.setattr(m, "_commit_staged", lambda conn, batch, cls, confidence=None:
                        staged.extend(batch))
    row = _row(role=None, asserted=True, islot=islot)  # no stamp, no structural rows: ONLY
    conn = _FakeConn(row)                               # the hard-line guard can catch it
    n_rungs = m._attach_wordnet_hypernym_ladder(
        conn, "faultline_test_schema",
        entity, surface, otype, _SeamRegistry(), "user-uuid",
        source_rel_type="owns", source_provenance="user_stated",
    )
    return n_rungs, len(staged)


def test_seam_wordnet_ladder_refuses_value_or_name(monkeypatch):
    # The surface 'krellin' has no common-noun sense → the guard refuses → ZERO rungs staged.
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: s == "dog")
    rungs, staged = _run_wordnet_ladder(monkeypatch)
    assert rungs == 0 and staged == 0, "a user-asserted non-common surface must NOT ladder"


def test_seam_wordnet_ladder_still_ladders_types(monkeypatch):
    # LV2 through the same seam: a common-noun surface (`dog`, user_stated constrained owns)
    # proceeds and stages its rungs — the regression bar, at the seam itself.
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: s == "dog")
    rungs, staged = _run_wordnet_ladder(monkeypatch, surface="dog",
                                        rungs=[("dog", "canine")])
    assert rungs >= 1 and staged >= 1, "a genuine TYPE must still ladder at this seam"


def test_seam_wordnet_ladder_red_when_guard_reverted(monkeypatch):
    """NON-VACUOUS PROOF: revert the guard via its single rollback lever and the SAME input
    that LV1 refuses then STAGES a rung — i.e. these tests are red exactly when the guard is
    reverted (this is the red state the LV1 assertions must not silently tolerate)."""
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: s == "dog")
    rungs, staged = _run_wordnet_ladder(monkeypatch, guard_flag="0")
    assert rungs >= 1 and staged >= 1, (
        "with HARDLINE_LADDER_GUARD=0 the pre-guard behaviour must return — "
        "if this fails the test is vacuous (nothing was being blocked)")


def test_rollback_lever_disabled_is_byte_identical_predicate(monkeypatch):
    monkeypatch.setenv("HARDLINE_LADDER_GUARD", "0")
    refuse, why = hg.refuses_subclass_rung(_FakeConn(_row(role="value")), "node-x", "blue")
    assert (refuse, why) == (False, None)


# ── Critic round 2: the IN-FLIGHT instance-direction complement (first-mention blindness) ─────

_NO_ROW_TAIL = _NO_ROW

@pytest.mark.parametrize("tail,expected", [
    (["Animal"],                       True),   # has_pet — a SPECIFIC kind set: instance binding
    (["Animal", "Object", "Organization"], True),  # owns
    (["Location"],                     True),   # lives_in — halifax, in-flight
    (["ANY"],                          False),  # adopt/play — unconstrained: value direction's shape
    ([],                               False),  # empty
    (["SCALAR"],                       False),  # scalar capacity only
    (["SCALAR", "ANY"],                False),
    (_NO_ROW_TAIL,                     False),  # unknown rel — fail-open (DB arms own it later)
])
def test_r2_specific_kind_classifier(tail, expected, monkeypatch):
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    conn = _FakeConn(None, tail=tail)
    assert m._rel_tail_is_specific_kind(conn, "some_rel") is expected


def test_r2_spaniel_shape_wordnet_seam_refuses_first_mention(monkeypatch):
    # THE CRITIC'S LIVE GAP: "I own a spaniel. Spaniels are merry little dogs." — the spine
    # runs the wordnet ladder MID-HARVEST, before the derived has_pet fact commits, so every
    # DB arm is blind. The IN-FLIGHT binding (source_rel_type=has_pet, user_stated, specific
    # tail) must refuse BEFORE the DB guard — type-word-free first mention included.
    monkeypatch.setenv("L4_WORDNET_LADDER", "true")
    monkeypatch.delenv("L4_LADDER_PROTECT_USER_VALUE", raising=False)
    monkeypatch.delenv("VALUE_PLACE_FIRST_CLASS", raising=False)
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(m, "_rel_tail_is_unconstrained_value", lambda _c, _r: False)
    monkeypatch.setattr(m, "_rel_tail_is_specific_kind", lambda _c, _r: True)  # has_pet shape
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    monkeypatch.setattr(m.wordnet_ladder, "hypernym_rungs", lambda *a, **k: [("spaniel", "gun dog")])
    monkeypatch.setattr(m.wordnet_ladder, "synset_synonyms", lambda *a, **k: [])
    staged = []
    monkeypatch.setattr(m, "_commit_staged", lambda c, b, cl, confidence=None: staged.extend(b))
    conn = _FakeConn(None)   # DB arms see NOTHING (fact not yet committed) — only in-flight works
    rungs = m._attach_wordnet_hypernym_ladder(
        conn, "faultline_test_schema", "node-spaniel", "spaniel", "Animal",
        _SeamRegistry(), "user-uuid",
        source_rel_type="has_pet", source_provenance="user_stated")
    assert rungs == 0 and not staged, "a first-mention specific-slot instance must NOT ladder"


def test_r2_control_unconstrained_or_engine_still_ladders(monkeypatch):
    # LV2: the SAME seam with an {ANY}-tailed user verb (adopt->poodle shape) or engine
    # provenance proceeds past the in-flight instance check.
    monkeypatch.setenv("L4_WORDNET_LADDER", "true")
    monkeypatch.delenv("L4_LADDER_PROTECT_USER_VALUE", raising=False)
    monkeypatch.delenv("VALUE_PLACE_FIRST_CLASS", raising=False)
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(m, "_rel_tail_is_unconstrained_value", lambda _c, _r: False)
    monkeypatch.setattr(m, "_inflight_rel_binds_instance", lambda _c, _r: False)  # {ANY} tail
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    monkeypatch.setattr(m.wordnet_ladder, "hypernym_rungs", lambda *a, **k: [("poodle", "dog")])
    monkeypatch.setattr(m.wordnet_ladder, "synset_synonyms", lambda *a, **k: [])
    staged = []
    monkeypatch.setattr(m, "_commit_staged", lambda c, b, cl, confidence=None: staged.extend(b))
    rungs = m._attach_wordnet_hypernym_ladder(
        _FakeConn(None), "faultline_test_schema", "node-poodle", "poodle", "Animal",
        _SeamRegistry(), "user-uuid",
        source_rel_type="adopt", source_provenance="user_stated")
    assert rungs >= 1 and staged, "an unconstrained-slot common noun must still ladder"


# ── Critic round 3: RAW-REL canonicalization + fail-closed-unknown at the in-flight seam ──────

@pytest.mark.parametrize("raw,tails,aliases,expected", [
    ("has_pet",  {"has_pet": ["Animal"]}, {},                       True),   # direct specific
    ("own",      {"owns": ["Animal", "Object", "Organization"]}, {"own": "owns"}, True),  # alias
    ("has",      {"owns": ["Animal", "Object", "Organization"]}, {"has": "owns"}, True),
    ("live_in",  {}, {},                                         True),   # UNKNOWN -> fail CLOSED
    ("have",     {}, {},                                         True),   # the schnauzer shape
    ("love",     {"love": ["ANY"]}, {},                           False),  # {ANY} -> still ladders
    ("keep",     {"keep": ["ANY"]}, {},                           False),
    ("adopt",    {"adopt": ["ANY"]}, {},                           False),
    ("",         {}, {},                                         False),  # no binding (engine pass)
])
def test_r3_inflight_binds_instance(raw, tails, aliases, expected, monkeypatch):
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    conn = _FakeConn(None, tails=tails, aliases=aliases)
    assert m._inflight_rel_binds_instance(conn, raw) is expected


def test_r3_bath_shape_wordnet_seam_refuses_unknown_raw_rel(monkeypatch):
    # THE CRITIC'S LIVE GAP: the spine passes the RAW 'live_in' (uncatalogued) while canonical
    # 'lives_in' exists only by the LATER /ingest pass — the rung was already staged. The
    # in-flight test must fail CLOSED on a fully-unknown rel at a user_stated seam.
    monkeypatch.setenv("L4_WORDNET_LADDER", "true")
    monkeypatch.delenv("L4_LADDER_PROTECT_USER_VALUE", raising=False)
    monkeypatch.delenv("VALUE_PLACE_FIRST_CLASS", raising=False)
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(m, "_rel_tail_is_unconstrained_value", lambda _c, _r: False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    monkeypatch.setattr(m.wordnet_ladder, "hypernym_rungs", lambda *a, **k: [("bath", "town")])
    monkeypatch.setattr(m.wordnet_ladder, "synset_synonyms", lambda *a, **k: [])
    staged = []
    monkeypatch.setattr(m, "_commit_staged", lambda c, b, cl, confidence=None: staged.extend(b))
    conn = _FakeConn(None, tails={}, aliases={})  # NOTHING catalogued — raw-rel blindness
    rungs = m._attach_wordnet_hypernym_ladder(
        conn, "faultline_test_schema", "node-bath", "bath", "Location",
        _SeamRegistry(), "user-uuid",
        source_rel_type="live_in", source_provenance="user_stated")
    assert rungs == 0 and not staged, "an unknown raw rel at a user_stated seam must fail CLOSED"


def test_r3_control_love_typed_mention_still_ladders(monkeypatch):
    # LV2 through the same seam: 'love' is catalogued {ANY} — a bare type mention at an
    # unconstrained verb ladders exactly as today (the terriers growth shape).
    monkeypatch.setenv("L4_WORDNET_LADDER", "true")
    monkeypatch.delenv("L4_LADDER_PROTECT_USER_VALUE", raising=False)
    monkeypatch.delenv("VALUE_PLACE_FIRST_CLASS", raising=False)
    monkeypatch.delenv("HARDLINE_LADDER_GUARD", raising=False)
    monkeypatch.setattr(m, "_rel_tail_is_unconstrained_value", lambda _c, _r: False)
    monkeypatch.setattr(hg, "_surface_is_common_type", lambda s: True)
    monkeypatch.setattr(m.wordnet_ladder, "hypernym_rungs", lambda *a, **k: [("terriers", "terrier")])
    monkeypatch.setattr(m.wordnet_ladder, "synset_synonyms", lambda *a, **k: [])
    staged = []
    monkeypatch.setattr(m, "_commit_staged", lambda c, b, cl, confidence=None: staged.extend(b))
    conn = _FakeConn(None, tails={"love": ["ANY"]}, aliases={})
    rungs = m._attach_wordnet_hypernym_ladder(
        conn, "faultline_test_schema", "node-terriers", "terriers", "Animal",
        _SeamRegistry(), "user-uuid",
        source_rel_type="love", source_provenance="user_stated")
    assert rungs >= 1 and staged, "a catalogued {ANY}-verb type mention must still ladder"

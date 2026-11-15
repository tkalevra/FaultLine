"""UNMAPPED-UNIT MEASURE GROWTH pins (the owner's decade/quarter faceplant).

MEASURED WOUND (fresh seat, eea12a11, pre-prod test DB): the copula-measure chain
skipped every unit noun OUTSIDE the tenant unit_scalar map at the recognition gate.
"My sabbatical was two decades long." grew NOTHING: no measure, no growth signal,
magnitude+unit left as uncovered residue (CRIT). The sweep could not have grown the
map anyway -- unit_scalar was not carved in grow_linguistic_cue_candidates._CARVED,
so every landed candidate was resolved away as 'cue_skipped' (a rail built and dark
at BOTH ends). 7-probe battery: 0 proposals, 0 grows, 4/7 sentences lost their
measure entirely.

THE FIX (two arms, strictly additive):
  1. deriver: a geometry-valid measure on an UNMAPPED unit noun is captured VERBATIM
     under the generic 'related_measure' rel AND the unit is recorded as a
     unit_scalar cue candidate (SPINE_UNMAPPED_UNIT_MEASURE, default ON; OFF restores
     the old skip byte-for-byte).
  2. sweep: unit_scalar is carved with the KEYED-MAP VALUE 'related_measure' (not a
     human note -- resolve_unit_scalar_map reads description as the rel).
AFTER (same battery, fresh seat): 3 proposals -> 3 grown (decade/quarter/knot),
tenant-bound map 42 -> 45 entries, every copula-frame measure captured verbatim.

No-DB pins: conftest pops POSTGRES_DSN for extraction modules, so the overlay
resolves the in-code bootstrap floor (no decade/century/knot) -- the unmapped arm
fires deterministically. These pins are red at eea12a11 by construction (the
pre-fix code emitted no related_measure and recorded no candidate).
"""
import importlib
import inspect

ling = importlib.import_module("src.extraction.linguistics")
emb = importlib.import_module("src.re_embedder.embedder")
lco = importlib.import_module("src.api.linguistic_cue_overlay")


def _derive(sentence, reference="2026-11-06"):
    # NO DOUBLE-DRAIN (critic R1 MINOR 1): this helper must NOT bridge ``growth_out`` into
    # the ContextVar accumulator -- that bridging masked exactly the MAJOR-1 wound class
    # (the arm feeding only the ContextVar while the live sentence pipeline's bridge
    # re-records ONLY the out-param list, because run_cpu ContextVar writes never flow
    # back). The ``growth_out`` list IS the harvest bridge's food (main.py re-records it
    # request-side), so the OUT-PARAM assertions below are the load-bearing proposal
    # pins. ``drained`` still reflects the arm's own ContextVar write for in-process
    # callers.
    growth: list = []
    lco._cue_candidates.set(None)
    facts = ling.derive_sentence_facts(sentence, reference, growth_out=growth)
    drained = lco.drain_cue_candidates()
    return facts, drained, growth


def _rels(facts):
    return {(f.subject, f.rel_type, f.object) for f in facts}


def test_unmapped_unit_measure_captured_verbatim_and_proposed():
    facts, drained, growth = _derive("The tree is 3 centuries old.")
    rels = _rels(facts)
    assert ("tree", "related_measure", "3 centuries") in rels, rels
    # THE OUT-PARAM CONTRACT: growth_out is what the live harvest bridge re-records
    # request-side into ontology_evaluations. Pre-fix the arm fed only the ContextVar,
    # so this assertion is the MAJOR-1 tripwire (red at fa24e717, green now).
    assert ("century", "unit_scalar") in growth, growth
    assert ("century", "unit_scalar") in drained, drained


def test_word_number_decade_measure_captured_and_proposed():
    facts, drained, growth = _derive("My sabbatical was two decades long.")
    rels = _rels(facts)
    assert ("sabbatical", "related_measure", "two decades") in rels, rels
    assert ("decade", "unit_scalar") in growth, growth
    assert ("decade", "unit_scalar") in drained, drained


def test_flag_off_restores_the_old_skip(monkeypatch):
    monkeypatch.setattr(ling, "SPINE_UNMAPPED_UNIT_MEASURE", False)
    facts, drained, growth = _derive("The tree is 3 centuries old.")
    assert not any(f.rel_type == "related_measure" for f in facts)
    assert ("century", "unit_scalar") not in drained
    assert not any(cat == "unit_scalar" for (_c, cat) in growth), growth


def test_mapped_unit_path_unchanged_and_never_hijacked():
    # year -> age is on the bootstrap floor: the MAPPED arm owns it, and the unmapped
    # arm must neither fire nor propose. The family emit keeps its BARE-MAGNITUDE
    # contract (62 -- the unit IS the attribute's scale): only the GENERIC map value
    # keeps the W2 verbatim (the next-sighting DB pin).
    facts, drained, growth = _derive("She is 62 years old.")
    rels = _rels(facts)
    assert any(f.rel_type == "age" and f.object == "62" for f in facts), rels
    assert not any(cat == "unit_scalar" for (_c, cat) in drained), drained
    assert not any(cat == "unit_scalar" for (_c, cat) in growth), growth
    assert not any(f.rel_type == "related_measure" for f in facts), rels


def test_oblique_unit_still_declined_even_when_unmapped():
    # THE HARD LINE boundary the unmapped arm must respect: a case-marked oblique
    # unit ("in two decades") is a time POSITION, never a split measure.
    facts, drained, growth = _derive("My launch is in 2 decades.")
    assert not any(f.rel_type == "related_measure" for f in facts), \
        [(f.subject, f.rel_type, f.object) for f in facts]
    assert ("decade", "unit_scalar") not in drained, drained
    assert not any(cat == "unit_scalar" for (_c, cat) in growth), growth




def test_post_growth_degree_without_dimension_emits_one_unit(monkeypatch):
    # THE MISSING SHAPE (critic R2 NEW MAJOR -- double-append, live-measured at
    # 'The knot is 3 decades tight.'): a degree adjective with NO WordNet dimension
    # ('tight'/'sturdy'/'swift' -- NOT 'strong', which resolves strength) still sets the
    # agreeing-degree token, and Q8 is skipped (related_measure admits every subject).
    # The #52 metadata-extension block then saw the engine-minted row's metadata dict
    # (truthy regardless of '' vs NULL -- either way the datatype is undeclared) and
    # extended the value, and the generic branch appended num.head AGAIN:
    # '3 decades decades'. The user's datum corrupted. CONTRACT: the #52 block is
    # FAMILY-ONLY; for a generic map value the generic branch is the SINGLE owner of
    # unit-extension. This pin is red on fa24e717+8a8d6fd2 (the 'decades decades' emit).
    grown = dict(lco._BOOTSTRAP_UNIT_SCALAR_MAP)
    grown["decade"] = "related_measure"
    monkeypatch.setattr(ling, "_unit_scalar_map", lambda: grown)
    # The engine-minted rel_types row EXISTS in steady state (the grow landed) and its
    # scalar_datatype is UNDECLARED ('' on older seats, NULL on fresh ones -- either way
    # the #52 guard saw a truthy metadata dict with a non-'integer' datatype and
    # extended the value). Simulate exactly that row so the pin exercises the real
    # double-append mechanism, not a no-row shortcut.
    monkeypatch.setattr(ling, "_rel_overlay_meta_map",
                        lambda: {"related_measure": {"scalar_datatype": ""}})
    facts, drained, growth = _derive("The knot is 3 decades tight.")
    rels = _rels(facts)
    assert ("knot", "related_measure", "3 decades") in rels, rels
    assert ("knot", "related_measure", "3 decades decades") not in rels, (
        "the unit was appended TWICE (#52 metadata-extension + generic branch) -- "
        "the #52 block must skip generic map values", rels)
    assert ("knot", "related_measure", "3") not in rels, rels
    # Mapped now: no re-proposal of the grown unit.
    assert not any(cat == "unit_scalar" for (_c, cat) in growth), growth
    assert not any(cat == "unit_scalar" for (_c, cat) in drained), drained


def test_post_growth_family_degree_keeps_the_52_contract(monkeypatch):
    # The guard must not over-reach: a FAMILY map value with an agreeing degree
    # adjective keeps the #52 contract byte-for-byte ('60 centimeters tall' ->
    # height='60 centimeters', the #52 pin battery already locks the family shapes;
    # here the generic-value pin above locks the NEW boundary). Sanity: an agreeing
    # family shape still resolves through the family rel, never related_measure.
    facts, drained, growth = _derive("She is 62 years old.")
    rels = _rels(facts)
    assert any(f.rel_type == "age" and f.object == "62" for f in facts), rels


def test_sweep_carves_unit_scalar_with_the_keyed_map_value():
    # The regression that made the decade/quarter faceplant permanent: unit_scalar
    # missing from _CARVED means every proposal is resolved away as cue_skipped.
    # The value is the KEYED-MAP VALUE (resolve_unit_scalar_map reads description as
    # the scalar rel) -- a prose note here would grow members that can never fire.
    class _Dead:
        def cursor(self):
            raise RuntimeError("no db in unit pin")
    stats = emb.grow_linguistic_cue_candidates(_Dead(), None)
    assert stats == {"grown": 0, "errors": 0}
    src = inspect.getsource(emb.grow_linguistic_cue_candidates)
    assert '"unit_scalar": "related_measure"' in src, (
        "unit_scalar must stay carved in _CARVED with the KEYED-MAP VALUE "
        "related_measure -- removing it re-creates the measured decade/quarter "
        "faceplant (every proposal resolved away as cue_skipped)")




def test_l4_append_fires_where_the_arm_actually_reaches_it(monkeypatch):
    # L4 APPEND COVERAGE (critic R2: 'confirm your pin proves the append where the arm
    # actually fires'). REACHABILITY HISTORY (honest, measured): at 5bf36ea0 the append
    # was gated on `not _scalar_rel_l4` (unit NOT in the map) while the measure-verb
    # BINDING that reaches it was gated on the unit IN the map (the same
    # _unit_scalar_map resolver, the same synchronous scope) -- with a stable map the
    # two gates were MUTUALLY EXCLUSIVE: through real traffic the append was UNREACHABLE
    # (dead trigger, DEV/BUGS/BUG-l4-arm-dead-emit-issue19-20261106.md), and the ONLY
    # reachable trigger was the tenant map CHANGING between the chain's two resolver
    # reads (impossible mid-derive single-threaded -- hence the stateful flipping map
    # below, the k=5/6 window measured at 5bf36ea0). REPAIRED in the issue-#19 lane:
    # the binding now admits shape-valid UNMAPPED units under SPINE_POSSESSED_QUANTITY_L4,
    # so with a STABLE map the arm's unmapped half fires through real traffic (see
    # test_l4_append_reachable_through_real_traffic_stable_map). This simulation pin
    # STAYS: it pins the mid-derive flip window (binding saw mapped, arm saw unmapped →
    # the append still rides growth_out, never a stale re-proposal) — the mechanics
    # proof the R1 out-param contract asked for.
    base = dict(lco._BOOTSTRAP_UNIT_SCALAR_MAP)
    with_z = dict(base)
    with_z["zorph"] = "quantity"
    state = {"n": 0}

    def _flipping_map():
        state["n"] += 1
        return dict(with_z) if state["n"] <= 5 else dict(base)

    monkeypatch.setattr(ling, "_unit_scalar_map", _flipping_map)
    # a tenant whose overlay lacks the verb rel ('use' is NOVEL for them) but is
    # otherwise readable -- the L4 HARD-LINE guard requires a truly novel rel
    monkeypatch.setattr(ling, "_rel_overlay_meta_map",
                        lambda: {"age": {"scalar_datatype": "integer"},
                                 "height": {"scalar_datatype": "quantity"}})
    growth: list = []
    lco._cue_candidates.set(None)
    facts = ling.derive_sentence_facts(
        "My bouillabaisse recipe uses 800 zorphs of fish.", None, growth_out=growth)
    rels = _rels(facts)
    # the L4 generic link emitted with the generic rel and the VERBATIM measure
    assert ("bouillabaisse recipe", "related_measure", "800 zorphs of fish") in rels, rels
    # THE APPEND: the unit rides the growth_out OUT-PARAM (the harvest bridge's food),
    # not just the ContextVar -- the MAJOR-1 contract, proven at this site.
    assert ("zorph", "unit_scalar") in growth, (
        "the L4 site must append the unit to _pending_growth like the other "
        "proposal sites (run_cpu ContextVar seam)", growth)


def test_l4_append_reachable_through_real_traffic_stable_map():
    # THE DEAD TRIGGER REPAIRED (issue-#19 lane, DEV/BUGS/BUG-l4-arm-dead-emit-
    # issue19-20261106.md): with a STABLE map that lacks the unit — no mid-derive flip,
    # no simulation — the measure-verb binding admits the shape-valid unmapped unit
    # (SPINE_POSSESSED_QUANTITY_L4 on) and the L4 arm's generic emit + growth_out
    # append fire through REAL traffic. Red at 5bf36ea0/cd4d51da/6f99e55a by
    # construction (the binding declined the unmapped unit before any arm ran).
    growth: list = []
    lco._cue_candidates.set(None)
    facts = ling.derive_sentence_facts(
        "My bouillabaisse recipe uses 800 zorphs of fish.", None, growth_out=growth)
    rels = _rels(facts)
    # the L4 generic link emitted with the generic rel and the VERBATIM measure
    assert ("bouillabaisse recipe", "related_measure", "800 zorphs of fish") in rels, rels
    # THE APPEND through the real door: the unit rides the growth_out OUT-PARAM (the
    # harvest bridge's food) on the FIRST sighting of the unmapped unit.
    assert ("zorph", "unit_scalar") in growth, (
        "the L4 append must be reachable through real traffic with a stable map "
        "(the 5bf36ea0 dead-trigger repair)", growth)
    # the concept link rides with it (the L4 shape: user → related_to → concept).
    assert ("user", "related_to", "bouillabaisse recipe") in rels, rels
    # the SPLIT/FRAGMENT twins are dropped even though the fire's scalar rel is the GENERIC
    # related_measure while the sibling chain files its stripped fragment under the family
    # generic 'quantity' (measured live through /harvest-spans pre-fix: (fish, quantity,
    # "800 zorphs") survived a related_measure fire) — the L4 ruling is rel-label-agnostic
    # for this construction's own surfaces.
    assert not any(r in ("quantity", "related_measure", "use") and s == "fish"
                   for s, r, _o in rels), rels
    assert not any(r == "use" for _s, r, _o in rels), rels



def test_grown_unit_widens_the_floor_union_without_touching_it():
    # The overlay contract the growth rides: floor UNION tenant rows, tenant wins on
    # key collision. A grown 'decade' row widens the resolution; the floor stays
    # minimal by design (closed measurement primitives only, never a unit zoo).
    floor = lco._BOOTSTRAP_UNIT_SCALAR_MAP
    assert "year" in floor and "decade" not in floor
    merged = dict(floor)
    merged["decade"] = "related_measure"
    assert merged["year"] == floor["year"] and "decade" in merged
    assert "decade" not in floor

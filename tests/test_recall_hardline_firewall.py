"""
THE HARD LINE — recall-side provenance firewall + out-of-band status framing.

Two defects let the PLACE (engine-grown L4 type ladder) and a drain STATUS banner render
as the user's MEMORY:

  FIX 2 — the recall WALK admitted engine-grown TYPE-ladder rows (subclass_of type→type,
          provenance llm_learn / engine_whatis_classify / engine_classify_climb /
          hierarchy_reconciliation) into the returned user-memory fact set. `_is_grown_ontology_row`
          holds those out of what convert_to_prose renders, while the walk still TRAVERSES the
          ladder so a grounded LEAF reached THROUGH it (family → pets → Fraggle) still surfaces.

  FIX 1 — the "still processing" document banner was prepended INTO the asserted-memory prose.
          It now renders as an explicit out-of-band "STATUS (not memory)" section.

Deterministic, subject-agnostic (keyed on provenance + subclass_of shape, no domain literals).
"""

from src.api.main import _is_grown_ontology_row, _ENGINE_GROWN_PROVENANCES
from src.mcp.response_types import render


# ── FIX 2: the grown-ontology firewall predicate ────────────────────────────

def test_engine_grown_provenances_are_held_out():
    """Every engine-minted provenance is grown-ontology (the PLACE) → held out of memory."""
    for prov in _ENGINE_GROWN_PROVENANCES:
        # even on an instance_of row (hierarchy_reconciliation mints instance_of), the
        # engine provenance alone marks it grown.
        assert _is_grown_ontology_row("instance_of", prov) is True, prov
    # any engine_* provenance (open-ended prefix), not just the enumerated ones.
    assert _is_grown_ontology_row("instance_of", "engine_backfill_something") is True


def test_bare_subclass_of_held_out_only_without_recorded_authorship():
    """A type→type subclass_of edge is grown ladder STRUCTURE when nobody recorded
    authorship — but the AUTHORSHIP CLAUSE (BUG-RECALL-5: the doctrine that answered
    "round 1 deleted the person's own words") keeps a user_stated rung in the reply.
    The old pin asserted held-out REGARDLESS of provenance; that doctrine evolved."""
    assert _is_grown_ontology_row("subclass_of", None) is True
    assert _is_grown_ontology_row("subclass_of", "user_stated") is False
    assert _is_grown_ontology_row("subclass_of", "llm_learned") is True
    assert _is_grown_ontology_row("SubClass_Of", "") is True   # case-insensitive


def test_user_grounded_leaf_rows_are_returned():
    """User-grounded leaf memory is NOT grown-ontology — it is returned as memory."""
    # the leaf edges that reach Fraggle through the ladder, and the instance's own is-a:
    assert _is_grown_ontology_row("has_pet", "user_stated") is False
    assert _is_grown_ontology_row("part_of", "user_stated") is False
    assert _is_grown_ontology_row("owns", "user_stated") is False
    assert _is_grown_ontology_row("instance_of", "user_stated") is False
    # a user-grounded instance_of with NO provenance recorded still passes (shape is not
    # subclass_of, provenance is not engine) — leaf memory is never accidentally dropped.
    assert _is_grown_ontology_row("instance_of", None) is False


def test_class_anchored_walk_returns_no_grown_taxonomy_rows():
    """(a) A class-anchored recall whose children are only subclass_of / llm_learn rows
    returns NO grown-taxonomy rows once the firewall runs over the walked set."""
    # Model the rows a down/ladder walk collected for a grown 3d-printing type node.
    walked = [
        {"rel_type": "subclass_of", "provenance": "engine_classify_climb",
         "object": "filament"},                              # pla → filament
        {"rel_type": "subclass_of", "provenance": "engine_whatis_classify",
         "object": "material"},                              # filament → material
        {"rel_type": "instance_of", "provenance": "hierarchy_reconciliation",
         "object": "polymer"},                               # engine is-a reconciliation
        {"rel_type": "subclass_of", "provenance": "llm_learned",
         "object": "thermoplastic"},                         # /expand-grown rung
    ]
    kept = [r for r in walked
            if not _is_grown_ontology_row(r["rel_type"], r["provenance"])]
    assert kept == []   # the whole grown ladder is held out of returned memory


def test_leaf_through_ladder_still_surfaces():
    """(b) A user-grounded leaf reachable THROUGH the grown ladder still surfaces — only the
    grown TYPE nodes are held out; the Fraggle-style descent is intact."""
    # family → pets grouping → the pet instance, mixed with grown type rungs the walk crossed.
    walked = [
        {"rel_type": "has_pet", "provenance": "user_stated", "object": "fraggle"},   # LEAF
        {"rel_type": "instance_of", "provenance": "user_stated", "object": "dog"},   # leaf is-a
        {"rel_type": "subclass_of", "provenance": "engine_classify_climb",
         "object": "canine"},                                                        # grown
        {"rel_type": "subclass_of", "provenance": "engine_classify_climb",
         "object": "mammal"},                                                        # grown
    ]
    kept = [r for r in walked
            if not _is_grown_ontology_row(r["rel_type"], r["provenance"])]
    kept_objs = {r["object"] for r in kept}
    assert "fraggle" in kept_objs           # the grounded leaf memory survives
    assert "dog" in kept_objs               # its user-stated type survives
    assert "canine" not in kept_objs        # grown type nodes held out
    assert "mammal" not in kept_objs


# ── FIX 1: out-of-band status framing in render() ───────────────────────────

def test_status_note_is_fenced_outside_the_memory_body():
    """(c) The pending-docs notice is NOT in the asserted-memory band — it is a separate,
    explicitly-labeled non-memory section AFTER the facts."""
    out = render(
        None,
        event_lines=[],
        assert_lines=["Your dog is Rex."],
        hold_lines=[],
        status_note="(background: still importing 1 document you submitted — their facts "
                    "aren't in memory yet; they should be searchable in about 30 seconds.)",
    )
    assert out.startswith("Your dog is Rex.")           # fact leads, status never prepends
    assert "=== STATUS (not memory) ===" in out
    assert out.index("Your dog is Rex.") < out.index("=== STATUS (not memory) ===")
    assert "background: still importing 1 document" in out


def test_status_note_alone_when_no_facts():
    """Empty bands + a pending notice → the notice renders ALONE under the status label,
    never as a bare asserted-memory string."""
    out = render(None, event_lines=[], assert_lines=[], hold_lines=[],
                 status_note="(background: still importing 2 documents you submitted.)")
    assert out.startswith("=== STATUS (not memory) ===")
    assert "still importing 2 documents" in out


def test_success_path_unchanged_without_status():
    """(d) With no status note (and no ops), render is byte-for-byte the pre-feature output —
    the plain asserted-memory prose."""
    out = render(None, event_lines=[], assert_lines=["Your dog is Rex."], hold_lines=[])
    assert out == "Your dog is Rex."

"""THE HARD LINE — a common-noun INSTANCE must never render as a proper NAME.

LIVE BUG (deployed spine path, verified via harvest-spans + e2e remember/recall on a
non-benchmark seat): "I built a birdhouse. I built a third birdhouse." captured CLEANLY as
    (user, create, birdhouse)
    (user, create, third birdhouse)
    (third birdhouse, instance_of, birdhouse)      # NO name/also_known_as edge
yet recall rendered "you currently create a Birdhouse named third Birdhouse" — the
structure-driven NAMED-INSTANCE composer (main.py ``_compose_object_clause`` Shape 1 /
``_compose_inverse_anchor_clause`` mirror) treated the common-noun instance's display form
("third birdhouse") as a PROPER NAME because its only guard was string-inequality
(type != name). "third birdhouse" != "birdhouse" so it composed "…named…" — a HARD-LINE
violation (a common noun is a TYPE, never a name) that pollutes recall and confuses the
cardinality read.

THE FIX (render guard, subject-agnostic, deterministic — NO domain list, NO cosine): a
"…named <Name>" composition is a PROPER-NAME construction. Suppress it when the object's
display form is a common-noun instance DESCRIPTOR — the instance_of type HEAD word appears
as a whole-word token inside the display form ("third birdhouse" ∋ "birdhouse", "navy blue
blazer" ∋ "blazer", "revell f-15 eagle kit" ∋ "kit"). A genuine proper name never contains
its own type word ("rex" ∌ "dog"), so real named instances are untouched.

This test FAILS on the old code (composes "…named…" for every premodified common noun) and
PASSES on the fix. Covers MULTIPLE exemplars of the structural cluster + the proper-name
control (must still render "named").
"""
import uuid

import pytest

from src.api.main import _compose_object_clause, _compose_inverse_anchor_clause


_OID = str(uuid.uuid4())


def _compose(object_name, named_type_name, *, template_2p="You created Y", rel_meta=None):
    return _compose_object_clause(
        rel_type="create",
        rel_meta=rel_meta if rel_meta is not None else {},
        subject_name="you",
        object_name=object_name,
        object_id=_OID,
        object_is_uuid=True,
        named_type_name=named_type_name,
        template="X created Y",
        template_2p=template_2p,
        subject_is_you=True,
    )


# ── CLUSTER: premodified common-noun instances must NOT render as a NAME ───────────────
@pytest.mark.parametrize("object_name,type_head", [
    ("third birdhouse", "birdhouse"),      # ordinal-premodified (the live repro)
    ("second birdhouse", "birdhouse"),     # ordinal
    ("navy blue blazer", "blazer"),        # adjective-premodified
    ("revell f-15 eagle kit", "kit"),      # compound/brand-premodified (the count exemplar)
    ("big red model kit", "kit"),          # multi-adjective
])
def test_common_noun_instance_renders_plain_not_named(object_name, type_head):
    out = _compose(object_name, type_head)
    assert out is not None, "composer should still render the clause"
    assert "named" not in out.lower(), (
        f"HARD-LINE violation: common-noun instance {object_name!r} rendered as a NAME "
        f"(a common noun is a TYPE, never a name): {out!r}")
    # the full descriptive NP survives verbatim (nothing is dropped)
    assert object_name in out.lower(), out


# ── CONTROL: a genuine PROPER name must still render "…named <Name>" ───────────────────
@pytest.mark.parametrize("object_name,type_head", [
    ("rex", "dog"),
    ("apollo", "server"),
    ("goose", "cat"),
])
def test_proper_name_still_renders_named(object_name, type_head):
    out = _compose(object_name, type_head, template_2p="You have Y")
    assert out is not None
    assert ("named %s" % object_name) in out.lower(), (
        f"a real proper name must keep the naming render: {out!r}")


# ── MIRROR path (inverse-anchor composer) carries the same guard ──────────────────────
def test_inverse_anchor_common_noun_not_named():
    out = _compose_inverse_anchor_clause(
        rel_type="made_by", rel_meta={"inverse_rel_type": "created"},
        anchor_name="you", anchor_is_you=True,
        instance_name="third birdhouse", instance_type_name="birdhouse")
    assert out is not None
    assert "named" not in out.lower(), out
    assert "third birdhouse" in out.lower(), out


def test_inverse_anchor_proper_name_still_named():
    out = _compose_inverse_anchor_clause(
        rel_type="child_of", rel_meta={"inverse_rel_type": "parent_of"},
        anchor_name="you", anchor_is_you=True,
        instance_name="leo", instance_type_name="son")
    assert out is not None
    assert "named leo" in out.lower(), out

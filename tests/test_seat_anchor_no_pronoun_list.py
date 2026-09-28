"""The first-person anchor is detected STRUCTURALLY — never by a pronoun/name list.

CONTEXT
───────
`src/api/main.py::_normalize_entity_ids_startup` decides `is_preferred` for the legacy
entity-id normalisation with:

    is_pref = string_id.lower() in ("user", "me", "myself")

That is the exact anti-pattern this repo deleted in `f7d6995` (`_SELF_SUBJECT_LEMMAS =
{"i","we","my","our"}`). It is also wrong on its own terms: it MINTS the placeholder token
"user" as an entity's preferred display label, and `entity_aliases`' partial unique index
(one preferred label per entity) means doing so where a real name already exists raises —
aborting the whole normalisation before `conn.commit()`.

`weld_guard` already solved this structurally, measured across 12 production tenants. This
file pins that detector (`is_seat_anchor`, APPLIED) and carries a strict-xfail tripwire on the
main.py literal (UNAPPLIED — main.py is owned by another agent). When the hunk lands, the
tripwire XPASSes, which pytest reports as a FAILURE under `strict=True`: that is the signal to
delete the marker, not a red test to excuse.
"""

import re

import pytest

from src.entity_registry import weld_guard as WG


# ── a fake cursor: enough to serve _existing_labels + the savepoint calls ──────

class _FakeCur:
    """Answers only what `_existing_labels` asks. `explode=True` simulates a probe failure."""

    def __init__(self, labels_by_entity, explode=False):
        self._labels = labels_by_entity
        self._explode = explode
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        s = sql.strip().upper()
        if s.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK")):
            return
        if self._explode:
            raise RuntimeError("simulated probe failure")
        self._rows = list(self._labels.get(str(params[0]), []))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SEAT_UUID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SEAT_SCHEMA = "faultline_0a1b2c3d_4e5f_4a6b_8c7d_9e0f1a2b3c4d"
OTHER_UUID = "3f1c9d22-1111-4222-8333-444455556666"


# ── marker 1: the bound schema name carries the seat UUID ─────────────────────

def test_marker1_the_bound_schema_name_identifies_the_seat_owner():
    cur = _FakeCur({})
    assert WG.is_seat_anchor(cur, SEAT_UUID, schema_name=SEAT_SCHEMA) is True
    assert cur.executed == [], "marker 1 must not need a DB read"


def test_marker1_is_prefix_agnostic_any_schema_family():
    assert WG.is_seat_anchor(_FakeCur({}), SEAT_UUID,
                             schema_name="otherfamily_0a1b2c3d_4e5f_4a6b_8c7d_9e0f1a2b3c4d") is True


def test_marker1_does_not_promote_a_different_entity_in_the_same_schema():
    assert WG.is_seat_anchor(_FakeCur({}), OTHER_UUID, schema_name=SEAT_SCHEMA) is False


def test_marker1_is_case_insensitive_on_the_entity_id():
    assert WG.is_seat_anchor(_FakeCur({}), SEAT_UUID.upper(), schema_name=SEAT_SCHEMA) is True


# ── marker 2: an alias written at the provisioning tier ───────────────────────

def test_marker2_a_provisioning_tier_alias_identifies_the_seat_owner_with_no_schema():
    """Measured on prod: 11/12 tenants carry exactly one such alias, never on a non-seat
    entity. It is what covers a caller that has no bound schema name."""
    cur = _FakeCur({OTHER_UUID: [("alex", "provisioned")]})
    assert WG.is_seat_anchor(cur, OTHER_UUID) is True


def test_a_user_stated_label_alone_is_NOT_a_seat_marker():
    """Every entity accumulates user-stated labels; only provisioning writes the anchor tier.
    Treating user_stated as the marker would make every named entity the speaker."""
    cur = _FakeCur({OTHER_UUID: [("rex", "user_stated")]})
    assert WG.is_seat_anchor(cur, OTHER_UUID) is False


def test_growth_tier_labels_are_not_a_seat_marker():
    cur = _FakeCur({OTHER_UUID: [("dog", "inferred"), ("canine", "rel_default")]})
    assert WG.is_seat_anchor(cur, OTHER_UUID) is False


def test_an_entity_with_no_labels_is_not_the_anchor():
    assert WG.is_seat_anchor(_FakeCur({}), OTHER_UUID) is False


# ── fail direction ────────────────────────────────────────────────────────────

def test_a_failed_probe_answers_NOT_the_anchor():
    """Opposite of `weld_verdict`'s fail-OPEN, deliberately: answering "yes, this is the
    speaker" on an error would mint an identity claim out of a database failure."""
    assert WG.is_seat_anchor(_FakeCur({}, explode=True), OTHER_UUID) is False


def test_a_failed_probe_still_honours_marker_1():
    """Marker 1 is decided before any DB access, so a dead probe cannot lose the seat."""
    assert WG.is_seat_anchor(_FakeCur({}, explode=True), SEAT_UUID,
                             schema_name=SEAT_SCHEMA) is True


def test_empty_and_none_entity_ids_are_never_the_anchor():
    for bad in (None, "", "   "):
        assert WG.is_seat_anchor(_FakeCur({}), bad) is False


def test_a_non_tenant_schema_name_contributes_nothing():
    """`public` carries no seat UUID; the detector must fall through to marker 2, not guess."""
    cur = _FakeCur({OTHER_UUID: [("alex", "unspecified")]})
    assert WG.is_seat_anchor(cur, OTHER_UUID, schema_name="public") is False


# ── the anti-pattern guard itself ─────────────────────────────────────────────

def test_the_detector_contains_no_pronoun_or_placeholder_literals():
    """Constraint 15. The whole point is that no token list decides who the speaker is."""
    import inspect
    src = inspect.getsource(WG.is_seat_anchor) + inspect.getsource(WG._is_seat_anchor) \
        + inspect.getsource(WG.seat_entity_id)
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    body = re.sub(r'""".*?"""', "", body, flags=re.S).lower()
    for banned in ("'user'", '"user"', "'me'", '"me"', "'myself'", '"myself"',
                   "'i'", '"i"', "'my'", '"my"', "'self'", '"self"'):
        assert banned not in body, f"pronoun/placeholder literal {banned} in the seat detector"


@pytest.mark.xfail(
    strict=True,
    reason="UNAPPLIED: the replacement hunk needs src/api/main.py's owner (another agent "
           "holds that file). When it lands this XPASSes — delete this marker, do not "
           "loosen the assertion.",
)
def test_main_py_no_longer_decides_the_anchor_from_a_pronoun_list():
    """THE TRIPWIRE. `_normalize_entity_ids_startup` must not carry a self-reference token
    list; the seat anchor is `weld_guard.is_seat_anchor`'s job."""
    src = open("src/api/main.py", encoding="utf-8").read()
    assert 'in ("user", "me", "myself")' not in src, (
        "hardcoded self-reference token list still present in main.py"
    )

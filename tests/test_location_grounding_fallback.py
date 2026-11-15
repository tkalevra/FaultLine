"""No-answer grounding fallback — _fetch_location_grounding (src/api/main.py).

The deterministic L4 walk resolving NOTHING for an unanswerable query (empty facts + no
concrete anchor) should surface the user's grounded LOCATION, HELD (Class C voice), instead
of a bare "No relevant facts found." — the deictic anchor a downstream model most often
lacks ("weather tomorrow?" → WHERE are you). These tests pin the metadata-driven rel
resolution (category=='location'), residence-over-birthplace preference (immutable ranks
last), the held Class-C stance, and the NEVER-fabricate contract — all subject-agnostic and
deterministic (no cosine, no weather/keyword list).
"""
import pytest

import src.api.main as main


class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        # HARD per-tenant assertion: runtime reads never qualify to public.
        assert "public." not in s, f"runtime read is public-qualified: {s!r}"
        self._db.queries.append((s, params))

        if "FROM rel_types WHERE category = 'location'" in s:
            # (rel_type, correction_behavior) rows for the location category.
            self._result = list(self._db.location_rels)
        elif "FROM facts" in s and "rel_type = ANY(%s)" in s and "superseded_at IS NULL" in s:
            owner, rels = params[0], set(params[1])
            # 5 cols now: the real fact_class AND fact_provenance are CARRIED, not discarded.
            # Fixture row: (subj, rel, obj, conf[, fact_class[, fact_provenance]]).
            self._result = [
                (f[1], f[2], f[3],
                 (f[4] if len(f) > 4 else "A"),
                 (f[5] if len(f) > 5 else "user_stated"))
                for f in self._db.facts
                if f[0] == owner and f[1] in rels
            ]
        elif "FROM entity_attributes" in s and "attribute = ANY(%s)" in s:
            owner, attrs = params[0], set(params[1])
            # 3 cols now: provenance decides the class via assign_class_and_confidence.
            # A fixture value may be a bare string (legacy) or (value, provenance).
            self._result = [
                (a, (val[0] if isinstance(val, tuple) else val),
                 (val[1] if isinstance(val, tuple) else "user_stated"))
                for (o, a), val in self._db.entity_attrs.items()
                if o == owner and a in attrs
            ]
        else:
            raise AssertionError(f"unexpected SQL hit fake cursor: {s!r}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeDB:
    def __init__(self, location_rels=None, facts=None, entity_attrs=None):
        # location_rels: [(rel_type, correction_behavior)]
        self.location_rels = location_rels or []
        # facts: [(subject_id, rel_type, object_id, confidence)]
        self.facts = facts or []
        # entity_attrs: {(entity_id, attribute): value_text}
        self.entity_attrs = entity_attrs or {}
        self.queries = []

    def cursor(self):
        return _FakeCursor(self)


_USER = "user-uuid"
_LOC_RELS = [
    ("lives_in", "supersede"),
    ("lives_at", "supersede"),
    ("located_in", "supersede"),
    ("born_in", "immutable"),
]


@pytest.fixture(autouse=True)
def _stub_prose(monkeypatch):
    # Render deterministically without the per-tenant overlay: echo the rel+object so the
    # residence-vs-birthplace choice is observable in the definition.
    def _fake_prose(facts, db, anchor=None, user_id=None, **kw):
        out = []
        for f in facts:
            out.append(f"you {f.get('rel_type')} {f.get('object')}")
        return out

    monkeypatch.setattr(main, "convert_to_prose", _fake_prose)
    monkeypatch.setattr(main.rel_type_overlay, "set_current_schema", lambda s: None)
    monkeypatch.setattr(main.rel_type_overlay, "reset_current_schema", lambda t: None)


def test_relational_residence_carries_its_real_class_never_forced_to_C():
    """THE FIX. This test used to assert `fact_class == "C"  # HELD, never asserted` — it was
    PINNING THE BUG.

    The class was HARDCODED to 'C', which routes the fact into the HELD band, whose contract
    tells the model "you are less certain about the following — mention tentatively". A live
    model then hedged a user's own address back at them: "I see that you might be here ... if
    that's wrong". It hedged because WE TOLD IT TO.

    The conflation: the RELEVANCE inference is uncertain (we are guessing they wanted their
    location surfaced); the FACT is not (we are not guessing where they live). Hardcoding 'C'
    hedged the wrong one. Anything in `facts` is A/B by construction — Class C lives in
    staged_facts — so the true class was RIGHT THERE and was being thrown away.

    The design doc's real intent ("never as the literal answer to the asked question") survives:
    it belongs in the FRAMING, not in downgrading a true fact to a maybe. Live evidence agrees —
    the model used the location as grounding and produced the correct forecast; it never mistook
    it for the answer. The hedge was the only defect.
    """
    db = _FakeDB(
        location_rels=_LOC_RELS,
        facts=[(_USER, "lives_in", "toronto-uuid", 0.9, "A")],
    )
    out = main._fetch_location_grounding(db, _USER, "faultline_x")
    assert out is not None
    assert out["fact_class"] == "A", "a user-stated address must NOT be downgraded to a maybe"
    assert out["fact_class"] != "C"
    assert out["rel_type"] == "lives_in"
    assert out["source"] == "location_grounding"
    assert "toronto-uuid" in out["definition"]
    # internal UUID keys must not leak
    assert "_subject_id" not in out and "_object_id" not in out


def test_the_CONSUMER_actually_asserts_it__not_just_the_field():
    """VERIFY THE READ, NOT THE WRITE.

    I first "fixed" this by carrying `fact_class` — and it changed NOTHING live, because the
    CONSUMER does not read fact_class. `_fact_is_asserted` (src/mcp/server.py) keys the
    assert/hold voice on PROVENANCE: "user_stated → ASSERT always; user is truth, never hedge,
    regardless of class or confidence." With `provenance` absent from the dict it fell back to the
    capture tier PLUS a low-confidence floor — and the real rows carry confidence 0 — so a Class-A
    user_stated address STILL rendered as "you might be here".

    So this test drives the REAL consumer with the REAL dict. Asserting `out["fact_class"] == "A"`
    would pass while the user still gets hedged at. That is the whole lesson.
    """
    from src.mcp.server import _fact_is_asserted

    db = _FakeDB(
        location_rels=_LOC_RELS,
        # confidence 0.0 ON PURPOSE — this is what the production rows actually carry, and it is
        # what the low-confidence floor was demoting. user_stated must win over the floor.
        facts=[(_USER, "lives_in", "toronto-uuid", 0.0, "A", "user_stated")],
    )
    out = main._fetch_location_grounding(db, _USER, "faultline_x")
    assert out is not None
    assert _fact_is_asserted(
        out.get("provenance"), out.get("confidence"), out.get("fact_class")
    ) is True, "the user's own stated address must reach the ASSERT band, not be hedged back at them"


def test_inferred_location_still_reads_held():
    """The band still tracks TRUTH: a low-confidence/inferred location is genuinely uncertain and
    must still be held. We removed a LIE about the class, not the ability to hedge."""
    from src.mcp.server import _fact_is_asserted

    db = _FakeDB(
        location_rels=_LOC_RELS,
        facts=[(_USER, "lives_in", "toronto-uuid", 0.4, "C", "llm_inferred")],
    )
    out = main._fetch_location_grounding(db, _USER, "faultline_x")
    assert out is not None
    assert out["fact_class"] == "C", "an inferred location SHOULD still be held"
    # And again, checked at the CONSUMER: a genuine GUESS must still be hedged. We removed a lie
    # about a user-stated fact — not the ability to hedge an actual inference.
    assert _fact_is_asserted(
        out.get("provenance"), out.get("confidence"), out.get("fact_class")
    ) is False, "an llm_inferred location must still be held — the guard against fabrication stays"


def test_residence_preferred_over_birthplace():
    # BOTH a residence and a birthplace exist — residence (non-immutable) must win.
    db = _FakeDB(
        location_rels=_LOC_RELS,
        facts=[
            (_USER, "born_in", "london-uuid", 1.0),
            (_USER, "lives_in", "toronto-uuid", 0.8),
        ],
    )
    out = main._fetch_location_grounding(db, _USER, "faultline_x")
    assert out is not None
    assert out["rel_type"] == "lives_in"       # NOT born_in
    assert "toronto-uuid" in out["definition"]


def test_birthplace_used_when_no_residence():
    db = _FakeDB(
        location_rels=_LOC_RELS,
        facts=[(_USER, "born_in", "london-uuid", 1.0)],
    )
    out = main._fetch_location_grounding(db, _USER, "faultline_x")
    assert out is not None
    assert out["rel_type"] == "born_in"
    # Same fix as the residence case: a user's stated BIRTHPLACE is not a maybe either. This
    # asserted "C" — pinning the same hardcoded downgrade. The residence-over-birthplace
    # PREFERENCE (the real subject of this test) is unchanged and still asserted above.
    assert out["fact_class"] == "A", "a stated birthplace must not be downgraded to a maybe"


def test_scalar_location_attribute_fallback():
    db = _FakeDB(
        location_rels=_LOC_RELS,
        entity_attrs={(_USER, "lives_at"): "123 Main St"},
    )
    out = main._fetch_location_grounding(db, _USER, "faultline_x")
    assert out is not None
    assert out["rel_type"] == "lives_at"
    assert "123 Main St" in out["definition"]


def test_no_location_returns_none_never_fabricates():
    db = _FakeDB(location_rels=_LOC_RELS, facts=[], entity_attrs={})
    assert main._fetch_location_grounding(db, _USER, "faultline_x") is None


def test_no_location_rels_in_ontology_returns_none():
    db = _FakeDB(location_rels=[], facts=[(_USER, "lives_in", "toronto-uuid", 0.9)])
    assert main._fetch_location_grounding(db, _USER, "faultline_x") is None


def test_null_db_returns_none():
    assert main._fetch_location_grounding(None, _USER, "faultline_x") is None

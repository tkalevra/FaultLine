"""STATIC (non-fluent) properties must never take tensed framing at render.

THE BUG (censused from 854 real bench recall lines — 21 hits):

    "effective time management used to be an instance of workshops"
    "coffee maker used to be an instance of maker"
    "occurrence: analysis data python using webinar used to be an instance of webinar"

A past-tense narrative ("I attended a workshop on effective time management") stamps the
turn's coarse ingest-time ``temporal_status='past'`` on EVERY edge it derives — including
the type-cast. ``convert_to_prose`` then read that stamp as a FLUENT phase and reframed the
clause as past-habitual, asserting a class-membership LAPSE the user never claimed.

ESTABLISHED TERM — STATIC vs DYNAMIC (FLUENT) property. Batsakis, Petrakis, Tachmazidis,
Antoniou & Gangemi, "Temporal representation and reasoning in OWL 2", Semantic Web
8(6):981-1000, 2017, DOI 10.3233/SW-160248, §3.1 (4D-fluents, after Welty & Fikes):
"CompanyName is a static property (its value does not change in time), while properties
produces, productName, locatedAt and Price are dynamic (fluent) properties. Fluent
properties are the properties whose values may change in time."

WHY THE CLASSIFICATION RELS ARE STATIC — they are SET-THEORETIC. W3C RDF Schema 1.1
(https://www.w3.org/TR/rdf-schema/) §3.3: "R rdf:type C states that C is an instance of
rdfs:Class and R is an instance of C"; §2/§3.4: "If a class C is a subclass of a class C',
then all instances of C will also be instances of C'." Set membership has no temporal
extent. FaultLine seeds instance_of→rdf:type (P31), subclass_of→rdfs:subClassOf (P279).

THE PREDICATE UNDER TEST is ``correction_behavior == 'immutable'`` — the EXISTING declared
"this fact is unchangeable" metadata (src/wgm/gate.py), which is verbatim the 4D-fluents
definition of STATIC — NOT ``is_hierarchy_rel``. This suite pins BOTH failure directions of
the is_hierarchy_rel alternative, so a future session cannot "simplify" to it:

  • OVER-BROAD  — ``member_of`` IS a hierarchy rel but is declared 'supersede' (a
    membership genuinely lapses). It MUST keep its tense; suppressing it would render a
    real former state as CURRENT.
  • UNDER-BROAD — ``parent_of`` is NOT a hierarchy rel but IS declared 'immutable'
    (biological kinship never lapses). It MUST lose its tense.

FIXTURE GROUNDING: every ``natural_language`` / ``natural_language_2p`` / metadata value in
_OVERLAY below is copied VERBATIM from the live seed (``SELECT rel_type,
correction_behavior, temporal_class, is_hierarchy_rel, natural_language,
natural_language_2p FROM public.rel_types``), not invented — CLAUDE.md "A GREEN SUITE IS
NOT EVIDENCE" §3 (tests pinning a FICTIONAL contract).

NOT IN SCOPE (deliberately): the ``historical`` supersession lane. That is the
BELIEF/transaction clock (an explicitly superseded row surfaced by the "what did I USED
TO…" ask), orthogonal to this VALID-time stamp — pinned by
``test_supersession_lane_untouched_for_static_rel``.
"""
import os
import uuid

import pytest

os.environ.setdefault("POSTGRES_DSN", "postgresql://x")
import src.api.main as m


# ── Fixture: rel metadata copied VERBATIM from the live public.rel_types seed ──────────
# rel_type          correction_behavior  temporal_class  is_hierarchy_rel
# instance_of       immutable            state           t     ← STATIC + hierarchy
# subclass_of       immutable            state           t     ← STATIC + hierarchy
# parent_of         immutable            state           f     ← STATIC, NOT hierarchy
# same_as           immutable            state           f     ← STATIC, NOT hierarchy
# member_of         supersede            state           t     ← FLUENT, IS hierarchy
# part_of           supersede            state           t     ← FLUENT, IS hierarchy
# lives_in          supersede            state           f     ← FLUENT (shipped feature)
_OVERLAY = {
    "instance_of": {"correction_behavior": "immutable", "temporal_class": "state",
                    "is_hierarchy_rel": True, "tail_types": ["ANY"],
                    "label": "instance of",
                    "natural_language": "X is an instance of Y (type)",
                    "natural_language_2p": "You are an instance of Y"},
    "subclass_of": {"correction_behavior": "immutable", "temporal_class": "state",
                    "is_hierarchy_rel": True, "tail_types": ["ANY"],
                    "label": "subclass of",
                    "natural_language": "X is a subclass of Y",
                    "natural_language_2p": "You are a subclass of Y"},
    "parent_of": {"correction_behavior": "immutable", "temporal_class": "state",
                  "is_hierarchy_rel": False, "tail_types": ["Person"],
                  "label": "parent of",
                  "natural_language": "X is the parent of Y",
                  "natural_language_2p": "You are the parent of Y"},
    "same_as": {"correction_behavior": "immutable", "temporal_class": "state",
                "is_hierarchy_rel": False, "tail_types": ["ANY"],
                "label": "same as",
                "natural_language": "X and Y are the same entity",
                "natural_language_2p": "You and Y are the same entity"},
    "member_of": {"correction_behavior": "supersede", "temporal_class": "state",
                  "is_hierarchy_rel": True, "tail_types": ["ANY"],
                  "label": "member of",
                  "natural_language": "X is a member of Y",
                  "natural_language_2p": "You are a member of Y"},
    "part_of": {"correction_behavior": "supersede", "temporal_class": "state",
                "is_hierarchy_rel": True, "tail_types": ["ANY"],
                "label": "part of",
                "natural_language": "X is a part of Y",
                "natural_language_2p": "You are a part of Y"},
    "lives_in": {"correction_behavior": "supersede", "temporal_class": "state",
                 "is_hierarchy_rel": False, "tail_types": ["Location"],
                 "label": "lives in",
                 "natural_language": "X lives in Y (residence)",
                 "natural_language_2p": "You live in Y"},
}

# Every tense marker the render layer can emit for a FORMER/UPCOMING phase
# (main.py convert_to_prose: _reframe_former_state → "<subj> used to <BASE>", the
# "Previously, …" fail-safe, and the 'future' "In the future, …" marker).
_TENSE_MARKERS = ("used to", "previously,", "in the future,")


def _tensed(line: str) -> bool:
    low = line.lower()
    return any(mark in low for mark in _TENSE_MARKERS)


@pytest.fixture
def _stub_overlay(monkeypatch):
    """Bind the per-tenant rel_type overlay to the verbatim seed fixture. `_build_identity_set`
    returns only "user", so a UUID subject renders 3rd-person via `natural_language` —
    exactly the shape the bench evidence lines have."""
    monkeypatch.setattr(m.rel_type_overlay, "resolve_current", lambda *a, **k: _OVERLAY)
    monkeypatch.setattr(m, "_build_identity_set", lambda *a, **k: {"user"})


def _pf(rel, subj_uuid, obj_uuid, status="past", **kw):
    """An UNDATED fact carrying the coarse ingest-time grammatical stamp."""
    f = {"rel_type": rel, "_subject_id": subj_uuid, "_object_id": obj_uuid,
         "temporal_status": status, "event_date": None, "confidence": 1.0,
         "fact_class": "A"}
    f.update(kw)
    return f


def _render(rel, subj_name, obj_name, status="past", **kw):
    subj, obj = str(uuid.uuid4()), str(uuid.uuid4())
    out = m.convert_to_prose(
        [_pf(rel, subj, obj, status, **kw)], db=None, anchor="user", user_id="user",
        preferred_alias_map={subj: subj_name, obj: obj_name})
    assert out, "render produced no prose — the fact must never be dropped"
    return out[0]


# ── THE BUG: static properties must render tenseless ──────────────────────────────────

@pytest.mark.parametrize("rel,subj,obj", [
    # The exact bench evidence shapes:
    ("instance_of", "effective time management", "workshops"),
    ("instance_of", "coffee maker", "maker"),
    ("subclass_of", "canine", "mammal"),
])
def test_static_classification_never_takes_tense(_stub_overlay, rel, subj, obj):
    """A set-theoretic classification edge stamped 'past' by a past-tense narrative renders
    in the TIMELESS PRESENT — never "X used to be an instance of Y"."""
    line = _render(rel, subj, obj, "past")
    assert not _tensed(line), line
    # FAIL LOUD, never silent: the content survives, only the framing changes.
    assert obj.split()[-1] in line.lower(), line


@pytest.mark.parametrize("rel,subj,obj", [
    ("parent_of", "diane", "chris"),
    ("same_as", "bob", "robert"),
])
def test_static_NON_hierarchy_rel_also_tenseless(_stub_overlay, rel, subj, obj):
    """UNDER-BROAD proof: parent_of / same_as are NOT hierarchy rels but ARE declared
    immutable — biological kinship and owl:sameAs identity never lapse. An
    `is_hierarchy_rel` predicate would MISS these; `correction_behavior` catches them."""
    line = _render(rel, subj, obj, "past")
    assert not _tensed(line), line


def test_static_future_stamp_also_suppressed(_stub_overlay):
    """The 'future' phase marker is equally meaningless on a static property — a class
    membership does not become true later."""
    line = _render("instance_of", "coffee maker", "maker", "future")
    assert not _tensed(line), line


# ── The CONTRAST seam (found only by live replay, not by the unit test above) ─────────

def test_static_rel_does_not_take_the_currently_contrast_marker(_stub_overlay):
    """REGRESSION PIN — gating only the FORMER-framing seam was NOT enough.

    convert_to_prose keeps a separate FORMER-STATE KEY INDEX (`_former_state_keys`) that
    sharpens a surviving current value with "currently" when a former member coexists for
    the same (subject, rel). It applied the SAME stored-'past' test, so the de-tensed static
    line marked its own key as having a former member and came back out as
    "X currently is an instance of Y" — a fluent reading smuggled in through the contrast
    seam. Both seams must read the same static predicate.

    Caught on the live seat1 row (instance_of / temporal_status='past' / event_date NULL),
    NOT by the framing assertion — which is why this pin exists. CLAUDE.md: "Verify a fix by
    rendering the REAL output at the seam it claims to fix."
    """
    line = _render("instance_of", "data analysis using python", "data analysis using", "past")
    assert not _tensed(line), line
    assert "currently" not in line.lower(), line
    # The timeless present, plainly:
    assert "is an instance of" in line.lower(), line


def test_fluent_rel_keeps_the_currently_contrast(_stub_overlay):
    """The contrast marker itself is NOT disabled — a FLUENT rel with a coexisting former
    value still sharpens the current one ("You currently live in …")."""
    subj, cur, former = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    out = m.convert_to_prose(
        [_pf("lives_in", subj, cur, "now"), _pf("lives_in", subj, former, "past")],
        db=None, anchor="user", user_id="user",
        preferred_alias_map={subj: "chris", cur: "vancouver", former: "toronto"})
    cur_line = next(p for p in out if "vancouver" in p.lower())
    assert "currently" in cur_line.lower(), out


# ── The other direction: FLUENT rels must KEEP their tense (no over-suppression) ───────

@pytest.mark.parametrize("rel,subj,obj", [
    ("member_of", "chris", "chess club"),   # hierarchy rel, but a membership LAPSES
    ("part_of", "wheel", "car"),            # hierarchy rel, but a part is DETACHED
])
def test_fluent_hierarchy_rel_keeps_tense(_stub_overlay, rel, subj, obj):
    """OVER-BROAD proof: member_of / part_of ARE hierarchy rels but are declared
    'supersede' — genuinely fluent. Suppressing their tense would render a real FORMER
    state as CURRENT (a false assertion). They must still read as former."""
    line = _render(rel, subj, obj, "past")
    assert _tensed(line), line


def test_fluent_state_rel_unchanged_no_regression(_stub_overlay):
    """The shipped undated-state FORMER framing (lives_in) is untouched."""
    line = _render("lives_in", "chris", "london", "past")
    assert _tensed(line), line


# ── Flag gate + fail-safe ─────────────────────────────────────────────────────────────

def test_flag_off_is_byte_identical_to_pre_fix(_stub_overlay, monkeypatch):
    """RENDER_STATIC_PROPERTY_TENSELESS=off → the pre-fix output returns verbatim, so the
    fix is a clean one-flag rollback."""
    subj, obj = str(uuid.uuid4()), str(uuid.uuid4())
    pam = {subj: "coffee maker", obj: "maker"}

    def _one():
        return m.convert_to_prose([_pf("instance_of", subj, obj, "past")], db=None,
                                  anchor="user", user_id="user", preferred_alias_map=pam)

    monkeypatch.setattr(m, "_RENDER_STATIC_PROPERTY_TENSELESS", False)
    off = _one()
    monkeypatch.setattr(m, "_RENDER_STATIC_PROPERTY_TENSELESS", True)
    on = _one()
    assert _tensed(off[0]), off          # pre-fix behavior = the bug, reproduced
    assert not _tensed(on[0]), on        # fixed
    assert off != on


@pytest.mark.parametrize("meta", [
    None, {}, {"correction_behavior": None}, {"correction_behavior": ""},
    {"correction_behavior": "supersede"}, {"correction_behavior": "hard_delete"},
    {"correction_behavior": "some_grown_value"},
])
def test_is_static_property_fail_safe(meta):
    """Metadata missing / NULL / any value other than the declared 'immutable' → NOT
    static → today's behavior. Never guesses."""
    assert m._is_static_property(meta) is False


def test_is_static_property_declared(_stub_overlay):
    """Positive: only the declared 'immutable' reads as static (case/whitespace tolerant)."""
    assert m._is_static_property({"correction_behavior": "immutable"}) is True
    assert m._is_static_property({"correction_behavior": "  IMMUTABLE "}) is True


def test_unknown_rel_meta_absent_keeps_current_behavior(_stub_overlay):
    """A rel with NO overlay row at all (un-seeded / mid-provisioning) must fall through to
    the existing path, not silently lose its framing."""
    subj, obj = str(uuid.uuid4()), str(uuid.uuid4())
    out = m.convert_to_prose([_pf("grown_novel_rel", subj, obj, "past")], db=None,
                             anchor="user", user_id="user",
                             preferred_alias_map={subj: "a", obj: "b"})
    assert out, "an un-seeded rel must still render (never dropped)"


# ── Scope boundary: the supersession (BELIEF-clock) lane is deliberately untouched ─────

def test_supersession_lane_untouched_for_static_rel(_stub_overlay):
    """DUAL-CLOCK ORTHOGONALITY (CLAUDE.md §Temporal Determination): `historical` marks a
    row the user EXPLICITLY superseded, surfaced only by the "what did I USED TO…" ask.
    That is the BELIEF/transaction clock, not this VALID-time stamp — suppressing it would
    render a superseded fact as CURRENT. It must still read as former, even for a static
    rel. If this ever needs to change it is a SEPARATE, evidenced decision."""
    line = _render("instance_of", "the old wing", "classroom", "past",
                   historical=True, superseded_at="2020-01-01T00:00:00+00:00")
    assert _tensed(line), line

"""Every sentence recall returns must describe the row it is attached to.

`convert_to_prose` emits FEWER lines than it is handed — three deliberate scaffold
suppressions, two render-error drops, and an inverse-pair dedup — and the holes fall at
arbitrary positions. Both callers used to re-associate POSITIONALLY, so from the first
hole onward fact *i* was handed the sentence for fact *i+1*.

MEASURED on the bench before the fix, seat with stated family facts, "what is my family?":
13 rows in, 12 sentences out, and the reply read

    (mother,  parent_of, user)     ->  "Diane's gender is female"
    (diane,   has_gender, female)  ->  "Diane is an instance of mother"
    (mother,  lives_in, Toronto)   ->  "You own iphone ebay"

that last sentence describing (user, owns, iphone ebay) — a row the answer did not contain.

These drive the REAL `_query_impl`. A previous fix in this area shipped twelve green tests
that never called the entrypoint and a critic broke it by driving it, so the unit-level
check below is deliberately paired with an end-to-end one.

DB-gated on LADDER_TEST_DSN + LADDER_TEST_SEAT so a database-less CI skips rather than lies.
"""
import os
import re

import pytest

DSN = os.environ.get("LADDER_TEST_DSN")
SEAT = os.environ.get("LADDER_TEST_SEAT")

_gate = pytest.mark.skipif(
    not (DSN and SEAT),
    reason="set LADDER_TEST_DSN and LADDER_TEST_SEAT to a bench seat to run these",
)

# The phrasings that actually produce a hole differ per seat and the bench is not
# deterministic, so the end-to-end assertions sweep several rather than pinning one.
_QUESTIONS = (
    "what is my family?",
    "tell me about my family",
    "who is in my family?",
    "what do you know about me?",
    "tell me about my pets",
)


def _drive(uid, text):
    """Run the real query and return (response_facts, bond).

    `bond` maps (subject, rel_type, object) -> the sentence convert_to_prose actually
    rendered FROM that row, captured by reading the render bond off the input facts. It
    is the ground truth the response is checked against.
    """
    # FORCE, never setdefault: an already-set POSTGRES_DSN would silently win and the
    # test would validate against a different database than LADDER_TEST_DSN names.
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
    from src.api.models import QueryRequest
    from src.api import main as M

    bond = {}
    _orig = M.convert_to_prose

    def _spy(facts, db, *a, **kw):
        out = _orig(facts, db, *a, **kw)
        for f in facts:
            key = (str(f.get("subject")), str(f.get("rel_type")), str(f.get("object")))
            if f.get("_prose") is not None:
                bond[key] = f["_prose"]
            elif f.get("_prose_suppressed"):
                bond.setdefault(key, _SUPPRESSED)
        return out

    M.convert_to_prose = _spy
    try:
        resp = M._query_impl(QueryRequest(text=text, user_id=uid, source="test"))
    finally:
        M.convert_to_prose = _orig
    return (resp.facts or []), bond


_SUPPRESSED = object()
# Rows recall SYNTHESISES (a count, a computed duration, a named comparison winner) carry
# their own definition and never pass through convert_to_prose, so they have no bond.
_SYNTHETIC_SOURCES = {
    "named_comparison_winner", "temporal_calc", "count_over_walk", "sum_over_walk",
}


def _misattached(facts, bond):
    """Return the rows whose sentence did not come from that row."""
    bad = []
    for i, f in enumerate(facts):
        if f.get("source") in _SYNTHETIC_SOURCES:
            continue
        key = (str(f.get("subject")), str(f.get("rel_type")), str(f.get("object")))
        expected = bond.get(key, None)
        got = f.get("definition")
        if expected is None:
            bad.append((i, key, got, "this row was never rendered by convert_to_prose"))
        elif expected is _SUPPRESSED:
            # A row convert_to_prose declined to speak comes back WITHOUT a sentence, not
            # dropped — never lose the person's row to a rendering preference. Carrying a
            # sentence here would mean it inherited a neighbour's line.
            if got is not None:
                bad.append((i, key, got,
                            "this row was declined yet came back with a sentence"))
        elif got != expected:
            bad.append((i, key, got, f"this row rendered {expected!r}"))
    return bad


@_gate
def test_every_sentence_describes_its_own_row():
    """THE BAR. Checked one by one against the row it claims to render."""
    answered = 0
    for q in _QUESTIONS:
        facts, bond = _drive(SEAT, q)
        if not facts:
            continue
        answered += 1
        bad = _misattached(facts, bond)
        assert not bad, f"{q!r}: sentence attached to the wrong row: {bad}"
    assert answered, (
        "every question came back empty — the seat must hold stated facts for this "
        "test to mean anything, and an empty answer is a fail"
    )


@_gate
def test_the_family_question_is_answered_at_all():
    """Precision direction: welding the sentence to its row must not empty the answer."""
    facts, bond = _drive(SEAT, "tell me about my family")
    assert facts, "a seat with stated family facts must answer its family question"
    spoken = [f for f in facts if f.get("definition")]
    assert spoken, f"the answer carried no sentence at all: {facts}"


@_gate
def test_no_sentence_survives_for_a_row_the_answer_does_not_contain():
    """The tail leak: the last sentence used to describe a row that was filtered out."""
    for q in _QUESTIONS:
        facts, bond = _drive(SEAT, q)
        returned = {(str(f.get("subject")), str(f.get("rel_type")), str(f.get("object")))
                    for f in facts}
        for f in facts:
            if f.get("source") in _SYNTHETIC_SOURCES:
                continue
            sentence = f.get("definition")
            if sentence is None:
                continue
            owners = [k for k, v in bond.items() if v == sentence]
            assert any(o in returned for o in owners), (
                f"{q!r}: the answer speaks {sentence!r}, which belongs to a row it does "
                f"not contain (owners={owners})"
            )


def test_convert_to_prose_binds_its_output_to_the_input_facts():
    """Unit level: the bond exists at all, and a declined row is FLAGGED rather than
    silently leaving a hole for a caller to mis-fill.

    This is the property that makes positional drift unrepresentable; it needs no DB.
    """
    from src.api import main as M
    import inspect
    src = inspect.getsource(M.convert_to_prose)
    assert '"_prose"' in src or "'_prose'" in src, \
        "convert_to_prose no longer welds its sentence to the fact that produced it"
    assert "_prose_suppressed" in src, \
        "convert_to_prose no longer flags the rows it deliberately declines"
    # And the callers must not have gone back to walking an index. Match the CODE
    # form, not the word — the comment above the fix names `prose_index` on purpose so
    # nobody re-derives it, and a test that greps for a spelling constrains the spelling.
    caller = inspect.getsource(M._query_impl)
    assert "prose_facts[prose_index]" not in caller, \
        "a positional prose_index walk was reintroduced in _query_impl"
    assert "_wprose[_i]" not in caller, \
        "a positional prose walk was reintroduced in the multi-hop walk lane"


# ─────────────────────────────────────────────────────────────────────────────────────
# GROUND TRUTH THAT DOES NOT MOVE WITH THE BUG.
#
# The checks above read their expectation off `_prose` — the very mark the implementation
# writes. A fresh-context critic broke that: welding by index INSIDE `convert_to_prose`
# (`_src = facts[_i2]` instead of `_row.get("fact")`) reproduces the original defect
# verbatim and leaves those tests GREEN, because the recorded bond moves with the bug.
#
# So this half derives the expectation from the ROW ITSELF, never from the render: a
# sentence that describes a triple must NAME that triple's own slots. Measured, this
# catches every misattachment at the parent commit (14 violations on two seats) and
# reports zero here.
# ─────────────────────────────────────────────────────────────────────────────────────

def _norm(x):
    return re.sub(r"[^a-z0-9]+", " ", str(x or "").lower()).strip()


def _slot_violations(resp, seat):
    """Rows whose sentence fails to name their own subject/object. Independent of `_prose`.

    Three renderings are legitimate and are NOT violations:
      * the querying USER's slot is spoken as "you"/"your" (perspective resolution);
      * the ANCHOR's SUBJECT slot may be rendered with the term the QUESTION used
        ("what is paseo?" renders the `walks` entity as "Paseo") — a deliberate
        question-framed override in convert_to_prose;
      * synthesised rows (a count, a computed duration, an abstention) never pass through
        convert_to_prose at all.
    """
    pref = resp.preferred_names or {}
    anchor = str(resp.anchor or "")
    # IDENTITY is the querying USER, never "whatever the question is about". Conflating
    # the two makes every object literally named `person` look like the user's own slot
    # on "what is a person?" — 16 false positives when this was first measured.
    ident_raw = {str(seat), "user", "you"}
    ident_names = {_norm(pref.get(str(seat))), "user", "you"} - {""}
    if anchor == str(seat):
        ident_raw.add(anchor)
        ident_names.add(_norm(pref.get(anchor)))

    bad = []
    for fact in (resp.facts or []):
        sentence = fact.get("definition")
        if not sentence or fact.get("source") in _SYNTHETIC_SOURCES:
            continue
        n = _norm(sentence)
        words = set(n.split())
        for slot in ("object", "subject"):
            raw = fact.get(slot)
            if raw is None:
                continue
            disp = str(pref.get(str(raw), str(raw)))
            tok = _norm(disp)
            is_ident = str(raw) in ident_raw or tok in ident_names
            is_anchor = str(raw) == anchor or (tok and tok == _norm(pref.get(anchor)))
            if is_ident:
                if not ({"you", "your"} & words):
                    bad.append((slot, fact.get("subject"), fact.get("rel_type"),
                                fact.get("object"), sentence,
                                "the querying user's slot is not spoken as 'you'"))
            elif slot == "subject" and is_anchor:
                continue
            elif not (tok and (tok in n or all(w in n for w in tok.split()))):
                bad.append((slot, fact.get("subject"), fact.get("rel_type"),
                            fact.get("object"), sentence,
                            f"{slot} {disp!r} is absent from its own sentence"))
    return bad


def _ask_resp(uid, text):
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
    from src.api.models import QueryRequest
    from src.api import main as M
    return M._query_impl(QueryRequest(text=text, user_id=uid, source="test"))


@_gate
@pytest.mark.parametrize("question", _QUESTIONS)
def test_every_sentence_names_the_row_it_describes(question):
    """THE BAR, checked WITHOUT trusting the implementation's own bookkeeping."""
    resp = _ask_resp(SEAT, question)
    assert resp.error is None, f"the query raised: {resp.error}"
    bad = _slot_violations(resp, SEAT)
    assert not bad, f"{question!r}: sentence does not describe its own row: {bad}"


@_gate
def test_the_independent_check_actually_inspects_something():
    """A checker that matches nothing passes vacuously. Pin that it sees real rows."""
    resp = _ask_resp(SEAT, "tell me about my family")
    spoken = [f for f in (resp.facts or [])
              if f.get("definition") and f.get("source") not in _SYNTHETIC_SOURCES]
    assert len(spoken) >= 3, (
        f"only {len(spoken)} sentences to check; the seat must answer its family "
        f"question for this suite to mean anything"
    )


@_gate
def test_a_row_the_render_declines_is_kept_without_a_sentence():
    """A declined row is KEPT with no `definition` — never dropped, never given a
    neighbour's line.

    Dropping it read better and was the wrong trade: on a real family seat the row the
    role-scaffold suppression declines is the person's own user_stated
    `mother parent_of user`, and a fact the person stated must not vanish from the
    response because the renderer preferred its twin's phrasing. The MCP render already
    skips a definition-less fact, so the reader is unaffected either way.
    """
    from src.api.models import QueryRequest
    from src.api import main as M
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

    declined = []
    _orig = M.convert_to_prose

    def _spy(facts, db, *a, **kw):
        out = _orig(facts, db, *a, **kw)
        for f in facts:
            if f.get("_prose_suppressed"):
                declined.append((str(f.get("subject")), str(f.get("rel_type")),
                                 str(f.get("object"))))
        return out

    seen_any = False
    M.convert_to_prose = _spy
    try:
        for q in _QUESTIONS:
            declined.clear()
            resp = M._query_impl(QueryRequest(text=q, user_id=SEAT, source="test"))
            if not declined:
                continue
            seen_any = True
            returned = {(str(f.get("subject")), str(f.get("rel_type")),
                         str(f.get("object"))): f for f in (resp.facts or [])}
            for key in declined:
                assert key in returned, (
                    f"{q!r}: the render declined {key} and the response DROPPED it — a row "
                    f"the person may have stated must survive a rendering preference"
                )
                assert returned[key].get("definition") is None, (
                    f"{q!r}: {key} was declined yet came back carrying a sentence "
                    f"{returned[key].get('definition')!r} — it inherited a neighbour's line"
                )
    finally:
        M.convert_to_prose = _orig
    assert seen_any, (
        "no question on this seat exercised a render decline, so this test proved "
        "nothing; point LADDER_TEST_SEAT at a seat whose family answer collapses an "
        "inverse pair or a role scaffold"
    )


@_gate
def test_a_second_render_pass_cannot_inherit_the_first_ones_sentence():
    """`convert_to_prose` clears the bond on entry. Without that, a fact dict rendered
    twice keeps the earlier pass's sentence on any pass that DECLINES it — the caller
    then reads a stale line as if it described the row."""
    from src.api.models import QueryRequest
    from src.api import main as M
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

    captured = {}
    _orig = M.convert_to_prose

    def _spy(facts, db, *a, **kw):
        out = _orig(facts, db, *a, **kw)
        # Capture a pass that actually DECLINES a row: the stale bond can only survive on
        # a fact the next pass does not re-render, so a pass with no decline proves nothing.
        if "facts" not in captured and any(f.get("_prose_suppressed") for f in facts):
            captured["facts"] = facts
            captured["db"] = db
            captured["kw"] = kw
            captured["args"] = a
        return out

    M.convert_to_prose = _spy
    try:
        for q in _QUESTIONS:
            if "facts" in captured:
                break
            M._query_impl(QueryRequest(text=q, user_id=SEAT, source="test"))
    finally:
        M.convert_to_prose = _orig

    facts = captured.get("facts")
    assert facts, (
        "no render pass on this seat declined a row, so a stale bond could not be "
        "observed; point LADDER_TEST_SEAT at a seat whose answer collapses an inverse "
        "pair or a role scaffold"
    )
    SENTINEL = "STALE-SENTINEL-FROM-A-PREVIOUS-PASS"
    for f in facts:
        f["_prose"] = SENTINEL
    M.convert_to_prose(facts, captured["db"], *captured["args"], **captured["kw"])
    stale = [(f.get("subject"), f.get("rel_type"), f.get("object"))
             for f in facts if f.get("_prose") == SENTINEL]
    assert not stale, f"a stale sentence survived a second render pass on {stale}"

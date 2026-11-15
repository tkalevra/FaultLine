"""Lane I — residence capture: #85 and #102 items 3-5.

#85      "When I was 20 I lived in Rome." filed the residence as current.
#102.3   "I lived in Paris from 2010 to 2015." filed as current.
#102.4   "I live in Kitchener and work in Waterloo." dropped the conjoined second predicate (spaCy
         tags the verb conjunct "work" NOUN).
#102.5   "My sister moved to Cusco." yielded no residence edge (the relocation chain admitted only a
         1st-person or PROPN mover).

Deriver pins + rendered-answer pins on fresh seats; each with a fresh-domain control.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, fact_rows, module_flags_fixture, rows, state,
)

_flags = module_flags_fixture()
e2e_past = e2e_fixture()
e2e_conj = e2e_fixture()
e2e_move = e2e_fixture()
e2e_two = e2e_fixture()


def _derive(s):
    import datetime
    import src.extraction.linguistics as L
    return {(f.subject, f.rel_type, f.object, f.temporal_status)
            for f in (L.derive_sentence_facts(s, reference=datetime.datetime(2026, 10, 1)) or [])}


def _prose(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()


# ── deriver ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("sent,place", [
    ("When I was 20 I lived in Rome.", "rome"),
    ("When I was a student I lived in Leeds.", "leeds"),         # fresh domain
    ("I lived in Paris from 2010 to 2015.", "paris"),
    ("I lived in Quito from 1998 to 2003.", "quito"),            # fresh domain
])
def test_past_residence_is_past(sent, place):
    fs = _derive(sent)
    rows = [f for f in fs if f[0] == "user" and f[2] == place]
    assert rows and all(f[3] == "past" for f in rows), fs


@pytest.mark.parametrize("sent,place", [
    ("I live in Kitchener and work in Waterloo.", "waterloo"),
    ("I live in Halifax and study in Montreal.", "montreal"),    # fresh domain
])
def test_102_4_conjoined_predicate_kept(sent, place):
    fs = _derive(sent)
    assert any(s == "user" and o == place for s, r, o, t in fs), fs


@pytest.mark.parametrize("sent,role,place", [
    ("My sister moved to Cusco.", "sister", "cusco"),
    ("My colleague moved to Bergen.", "colleague", "bergen"),    # fresh domain
])
def test_102_5_possessed_role_relocation(sent, role, place):
    assert (role, "lives_in", place, "now") in _derive(sent)


def test_102_5_non_person_mover_untouched():
    assert not any(r == "lives_in" for s, r, o, t in _derive("My laptop moved to Oslo."))


def _anchors(s):
    import datetime
    import src.extraction.linguistics as L
    return {(f.subject, f.rel_type, f.object): (f.subject_role_anchor, f.object_role_anchor)
            for f in (L.derive_sentence_facts(s, reference=datetime.datetime(2026, 10, 1)) or [])}


@pytest.mark.parametrize("sent,edge,role", [
    ("My sister moved to Cusco.", ("sister", "lives_in", "cusco"), "sister"),
    ("My colleague moved to Bergen.", ("colleague", "lives_in", "bergen"), "colleague"),  # fresh
])
def test_160_bare_speaker_role_subject_is_anchored(sent, edge, role):
    """#160: the role-holder bind keys on the TOKEN, not the surface — the deriver proves the edge's
    subject IS the speaker's bare role mention by its dependency path to the verb."""
    assert _anchors(sent).get(edge, (None, None))[0] == role, _anchors(sent)


@pytest.mark.parametrize("sent,edge", [
    ("Tom's sister moved to Oslo and my sister stayed.", ("sister", "move_to", "oslo")),
    ("Maria's brother moved to Faro and my brother stayed.", ("brother", "move_to", "faro")),
    ("I visited my brother and my sister lives in Rome.", ("brother", "live_in", "rome")),  # #161
    ("I phoned my mother and my father lives in Bern.", ("mother", "live_in", "bern")),     # fresh
])
def test_160_161_third_party_or_misattached_role_is_not_anchored(sent, edge):
    """#160/#161: a third party's role ("Tom's sister") and a subject the parser attached through a
    coordination carry no anchor, so /ingest never re-keys them onto the speaker's holder."""
    a = _anchors(sent)
    assert a.get(edge, (None, None))[0] is None, a


# ── rendered answers ──────────────────────────────────────────────────────────
def test_e2e_past_residences_do_not_read_current(e2e_past):
    for t in ("I live in Toronto.", "When I was 20 I lived in Rome.",
              "I lived in Paris from 2010 to 2015."):
        state(e2e_past, t)
    p = _prose(e2e_past, "Where do I live?")
    assert "toronto" in p, p
    assert "you live in rome" not in p and "you live in paris" not in p, p


def test_e2e_conjoined_work_place(e2e_conj):
    state(e2e_conj, "I live in Kitchener and work in Waterloo.")
    p = _prose(e2e_conj, "Where do I work?")
    assert "waterloo" in p, p
    state(e2e_conj, "I live in Halifax and study in Montreal.")   # fresh domain
    p = _prose(e2e_conj, "Where do I study?")
    assert "montreal" in p, p


def _live_rows(env):
    return fact_rows(env)


def _residence_status(env, subj, place):
    """temporal_status of the live (subj, lives_in, place) row(s) — #99 ends a superseded
    residence by stamping it 'past' (kept as history), never by deleting it."""
    return {r[0] for r in rows(env["schema"], """
        SELECT f.temporal_status FROM facts f
          JOIN entity_aliases sa ON sa.entity_id = f.subject_id AND sa.alias = %s
          JOIN entity_aliases oa ON oa.entity_id = f.object_id AND oa.alias = %s
         WHERE f.rel_type = 'lives_in' AND f.superseded_at IS NULL
        UNION ALL
        SELECT s.temporal_status FROM staged_facts s
          JOIN entity_aliases sa ON sa.entity_id = s.subject_id AND sa.alias = %s
          JOIN entity_aliases oa ON oa.entity_id = s.object_id AND oa.alias = %s
         WHERE s.rel_type = 'lives_in' AND s.deleted_at IS NULL""",
        (subj, place, subj, place))}


def test_e2e_sister_moved(e2e_move):
    """#134: the move lands on the sister already on file (Ana), her Lima residence ends, and no
    second entity named after the role is minted."""
    state(e2e_move, "My sister Ana lives in Lima.")
    state(e2e_move, "My sister moved to Cusco.")
    rs = _live_rows(e2e_move)
    assert ("ana", "lives_in", "cusco") in rs, rs
    assert _residence_status(e2e_move, "ana", "lima") == {"past"}, rs
    assert not any("sister" in (s, o) and r == "lives_in" for s, r, o in rs), rs
    p = _prose(e2e_move, "Where does my sister live?")
    assert "cusco" in p and "sister lives" not in p, p
    assert "ana lives in lima" not in p and "ana currently lives in lima" not in p, p
    # fresh domain: a different role and places
    state(e2e_move, "My brother Leo lives in Porto.")
    state(e2e_move, "My brother moved to Faro.")
    rs = _live_rows(e2e_move)
    assert ("leo", "lives_in", "faro") in rs, rs
    assert _residence_status(e2e_move, "leo", "porto") == {"past"}, rs


def test_e2e_two_holders_no_guess(e2e_two):
    """Two sisters on file: "My sister moved to …" cannot say which — no holder is guessed;
    both keep their residences."""
    state(e2e_two, "My sister Ana lives in Lima.")
    state(e2e_two, "My sister Eva lives in Quito.")
    state(e2e_two, "My sister moved to Cusco.")
    rs = _live_rows(e2e_two)
    assert ("ana", "lives_in", "lima") in rs and ("eva", "lives_in", "quito") in rs, rs
    assert ("ana", "lives_in", "cusco") not in rs and ("eva", "lives_in", "cusco") not in rs, rs

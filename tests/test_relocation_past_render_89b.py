"""#89b — pre-prod smoke on saas 907d1677: "I moved from Halifax to Regina last year." then "Where do I
live?" rendered

    You currently live in Regina (on 2025-10-01)
    You used to live in Hamilton
    You currently live in Halifax (on 2025-10-01)      <- the ended residence read as current

The row that landed: (user, lives_in, halifax) temporal_status='past' BUT event_date = the move date.
For a state, event_date is its VALID-FROM; the reader (convert_to_prose) rightly recomputes a dated
state from its date and treats it as current, so the 'past' stamp never reached the render. The move
date is when Halifax ENDED, not when it began. Fix at ingest (linguistics._chain_relocation): the prior
residence of a move is filed as an UNDATED 'past' state — the same shape "I used to live in X" files.

E2E on a throwaway *_test tenant, production flags: real harvest -> /ingest -> /query, atoms stubbed to
the sentence itself. The RENDERED answer is pinned.
"""
import pytest

from tests.test_smoke_ingest_35_36_37 import (  # noqa: F401 — e2e + the production-flag fixture
    L, _spine, e2e, _state, _module_flags,
)
from tests.test_residence_supersession_99 import seat  # noqa: F401 — a fresh seat per test


def _render(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return [(f.get("definition") or "") for f in (r.get("facts") or [])
            if (f.get("rel_type") or "") == "lives_in"]


def _say(env, s):
    _state(env, s, [s])


def _line(lines, place):
    hits = [x for x in lines if place.lower() in x.lower()]
    assert len(hits) == 1, (place, lines)
    return hits[0].lower()


@_spine
def test_moved_with_prior_known_residence(e2e):
    _say(e2e, "I used to live in Hamilton.")
    _say(e2e, "I moved from Halifax to Regina last year.")
    lines = _render(e2e, "Where do I live?")
    assert "used to live" in _line(lines, "Hamilton"), lines
    assert "used to live" in _line(lines, "Halifax"), lines
    assert "used to" not in _line(lines, "Regina"), lines
    assert sum("currently" in x.lower() for x in lines) <= 1, lines   # never a second "currently"
    assert not any("currently" in x.lower() and "halifax" in x.lower() for x in lines), lines


@_spine
def test_moved_fresh_domain(seat):
    # its own fresh seat (#101 R4): independent of what other tests (or xdist workers) wrote
    _say(seat, "I moved from Leeds to York in 2024.")   # dated, like the smoke turn
    lines = _render(seat, "Where do I live?")
    assert "used to live" in _line(lines, "Leeds"), lines
    assert not any("currently" in x.lower() and "leeds" in x.lower() for x in lines), lines
    assert "used to" not in _line(lines, "York"), lines
    assert sum("currently" in x.lower() for x in lines) <= 1, lines


def test_prior_residence_is_filed_undated_past():
    # deriver contract, with a reference date: the move date binds to the NEW residence only
    import datetime
    facts = L.derive_sentence_facts("I moved from Halifax to Regina last year.",
                                    datetime.date(2026, 10, 1))
    by_place = {(f.object or "").lower(): f for f in facts if f.rel_type == "lives_in"}
    assert by_place["halifax"].temporal_status == "past", by_place
    assert by_place["halifax"].event_date is None, by_place
    assert by_place["regina"].temporal_status == "now", by_place

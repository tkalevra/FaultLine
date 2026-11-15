"""Unit tests for the FLAT-EVENT-DUPLICATE SUPPRESSOR — the event-capture spine-authoritative fix.

PURE tests — no DB, no network, no GLiNER2, no LLM. They exercise the deterministic seams that
make the reified event spine the ONE canonical occurrence capture on the live /ingest path:

  * _detect_event_states_reified  — per-clause reified occurrence (peels the date, analyze_event).
  * _reified_event_manifest       — derives the (titles, event_types, handles) suppression spans.
  * _suppress_flat_event_duplicates — purges the LLM/GLiNER2 flat duplicates of a reified event:
        (1) title-leak, (2) flat event-type, (3) STEP-4 flat-participation supersede (date-suffixed
        LLM dupe whose surface disagrees with the spine's), with the DATE-SUFFIX STRIP that reuses
        the EXISTING deterministic date detector (extract_event_date_and_residue).

The model-backed reify tests skip cleanly if en_core_web_sm is not installed; the date-suffix-strip
and Step-4 unit tests build a manifest by hand so they run without spaCy.

Run: python3 -m pytest tests/test_event_suppression.py -q   (tests/ is gitignored → git add -f)
"""
import os

import pytest

# No live DSN — the overlays fall back to seed/code defaults; the suppressor is pure-deterministic.
os.environ.pop("POSTGRES_DSN", None)
os.environ.setdefault("LINGUISTIC_LAYER", "true")
os.environ.setdefault("OCCURRENCE_REIFY", "true")
os.environ.setdefault("TEMPORAL_DATE_LAYER", "true")

import src.api.main as m  # noqa: E402


def _model_available() -> bool:
    try:
        from src.extraction.linguistics import linguistics_available
        return linguistics_available()
    except Exception:  # noqa: BLE001
        return False


requires_model = pytest.mark.skipif(
    not _model_available(), reason="en_core_web_sm not installed in test env"
)


# ─────────────────── STEP 1 — date-suffix strip in the suppressor (unit) ───────────────────
# The reified manifest title is BARE because the deterministic seam peeled the clause date out
# upstream. The LLM flat lane (no date peel on /extract/rewrite) bakes the trailing date into the
# object surface. The suppressor's _norm_title must strip that date suffix with the EXISTING date
# detector so the date-suffixed LLM dupe matches the bare manifest title.

@requires_model
def test_date_suffix_strip_matches_bare_manifest_title():
    """A flat LLM object carrying a trailing date ('foo bar on 2023-06-14') normalizes to the bare
    title ('foo bar') and is suppressed against a manifest title='foo bar' — even though the spine
    title is bare (the spine peeled the date)."""
    manifest = {"handles": {"h1"}, "titles": {"foo bar"}, "event_types": set()}
    edges = [
        {"subject": "user", "rel_type": "participated_in", "object": "foo bar on 2023-06-14"},
        {"subject": "user", "rel_type": "has_pet", "object": "fraggle"},  # non-event control
    ]
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    rels = {(e["rel_type"], e["object"]) for e in kept}
    assert ("participated_in", "foo bar on 2023-06-14") not in rels, \
        "date-suffixed title dupe was NOT suppressed"
    assert ("has_pet", "fraggle") in rels, "non-event control was wrongly dropped"


@requires_model
def test_date_suffix_strip_does_not_false_strip_non_date():
    """A title surface with NO date ('data analysis using python') is left intact by the date peel —
    no false strip — and still matches a bare manifest title of the same value."""
    manifest = {"handles": {"h1"}, "titles": {"data analysis using python"}, "event_types": set()}
    edges = [{"subject": "user", "rel_type": "participated_in",
              "object": '"data analysis using python"'}]  # surrounding quotes only, no date
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    assert kept == [], "quoted title leak (no date) should still be suppressed via punct-normalize"


# ─────────────── STEP 4 — flat-participation supersede when the spine reified (unit) ───────────────

def test_step4_flat_participation_superseded_when_spine_reified():
    """When the spine reified an occurrence (handles non-empty), a competing flat participated_in
    edge whose surface DISAGREES with the spine is dropped — keyed PURELY on the occurrence rel +
    handle-ness (no domain word list)."""
    manifest = {
        "handles": {"racking event on 2023-06-14"},
        "titles": set(),
        "event_types": {"racking event"},
    }
    edges = [
        # garbled flat LLM participation (different surface than the spine) → must be superseded
        {"subject": "user", "rel_type": "participated_in", "object": "turbocharged tuesdays"},
        # non-event edges → must survive
        {"subject": "user", "rel_type": "has_pet", "object": "fraggle"},
        {"subject": "user", "rel_type": "spouse", "object": "dez"},
        # the spine's OWN canonical edges (subject/object ARE handles) → must survive
        {"subject": "user", "rel_type": "participated_in", "object": "racking event on 2023-06-14"},
        {"subject": "racking event on 2023-06-14", "rel_type": "instance_of", "object": "racking event"},
    ]
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    rels = {(e["subject"], e["rel_type"], e["object"]) for e in kept}
    assert ("user", "participated_in", "turbocharged tuesdays") not in rels, \
        "garbled flat participation was NOT superseded by the reified spine"
    assert ("user", "has_pet", "fraggle") in rels
    assert ("user", "spouse", "dez") in rels
    assert ("user", "participated_in", "racking event on 2023-06-14") in rels, \
        "the spine's OWN reified participated_in (object is a handle) was wrongly dropped"


def test_step4_no_op_when_spine_did_not_reify():
    """Fail-safe: no reified handle → clause (3) is a no-op — a flat participated_in survives so
    nothing is lost when the spine did not fire (manifest event_types present from a legacy path,
    but no handle)."""
    manifest = {"handles": set(), "titles": set(), "event_types": {"workshop"}}
    edges = [{"subject": "user", "rel_type": "participated_in", "object": "some conference"}]
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    assert {e["object"] for e in kept} == {"some conference"}, \
        "flat participation was dropped though the spine produced no handle"


def test_step4_does_not_touch_non_event_rels():
    """BLAST-RADIUS guard: clause (3) ONLY drops the occurrence rel (participated_in). A non-event
    edge is never touched even when the spine reified."""
    manifest = {"handles": {"h1"}, "titles": set(), "event_types": {"meeting"}}
    edges = [
        {"subject": "user", "rel_type": "lives_in", "object": "guelph"},
        {"subject": "user", "rel_type": "works_for", "object": "university"},
        {"subject": "user", "rel_type": "age", "object": "40"},
    ]
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    assert len(kept) == 3, "a non-event edge was dropped by the participation-supersede clause"


# ─────────────────── D1 — DATE-KEYED event-noise suppression (spine-primary) ─────────────────────
# The spine reifies the canonical dated occurrence for a clause, but ALSO emits competing FLAT
# (user, participated_in, <bare-noun>) edges for the SAME clause (discourse markers "way", day-words
# "sunday", generic "issues", session-date leakage). Each carries the SAME event_date as the reified
# occurrence. Drop them by DATE KEY (structural, no noun list); a genuinely-distinct 2nd dated event
# has a DIFFERENT date → kept.

def test_manifest_collects_reified_event_dates():
    """The manifest now carries the reified occurrence's own event_date (hinge 1)."""
    reified = [
        {"subject": "user", "rel_type": "participated_in", "object": "racking event on 2026-06-14",
         "event_date": "2026-06-14", "event_date_granularity": "day"},
        {"subject": "racking event on 2026-06-14", "rel_type": "instance_of", "object": "racking event"},
    ]
    man = m._reified_event_manifest(reified)
    assert man["event_dates"] == {"2026-06-14"}, man
    assert "racking event" in man["event_types"]


def test_flat_fallback_manifest_has_date_but_no_title_or_type():
    """THE GATE GAP (root cause): the reified seam's FLAT-FALLBACK shape (a bare common-noun event)
    emits ONLY a flat participated_in — no instance_of/also_known_as — so titles AND event_types are
    EMPTY. Before D1, the call-site gate `if titles or event_types` then SKIPPED the suppressor and
    ALL same-clause flat noise survived. The manifest now carries event_dates from the flat-fallback,
    so the gate (and the suppressor's own early-exit) fire on the date alone."""
    flat_fallback = [
        {"subject": "user", "rel_type": "participated_in", "object": "mass",
         "event_date": "2026-06-14", "event_date_granularity": "day"},
    ]
    man = m._reified_event_manifest(flat_fallback)
    assert not man["titles"] and not man["event_types"], \
        "flat-fallback unexpectedly produced a title/type"
    assert man["event_dates"] == {"2026-06-14"}, man
    assert "mass" in man["handles"], "flat-fallback occurrence object must seed handles"
    # The suppressor must NOT early-exit now that event_dates is non-empty: a same-date noise edge is
    # dropped even though titles/event_types are empty (the pre-D1 skip is gone).
    edges = [
        {"subject": "user", "rel_type": "participated_in", "object": "mass", "event_date": "2026-06-14"},
        {"subject": "user", "rel_type": "participated_in", "object": "issues", "event_date": "2026-06-14"},
    ]
    kept = m._suppress_flat_event_duplicates(edges, man, is_edgeinput=False)
    objs = {e["object"] for e in kept}
    assert "mass" in objs and "issues" not in objs, objs


def test_date_keyed_same_date_flat_noise_dropped():
    """A flat (user, participated_in, <bare-noun>) sharing the reified occurrence's date is DROPPED —
    keyed PURELY on the date, not the noun ('way'/'sunday'/'issues' are not enumerated anywhere)."""
    manifest = {
        "handles": {"racking event on 2026-06-14"},
        "titles": set(),
        "event_types": {"racking event"},
        "event_dates": {"2026-06-14"},
    }
    edges = [
        # same-date flat spine NOISE (any bare noun) → dropped by date key
        {"subject": "user", "rel_type": "participated_in", "object": "way", "event_date": "2026-06-14"},
        {"subject": "user", "rel_type": "participated_in", "object": "sunday", "event_date": "2026-06-14"},
        {"subject": "user", "rel_type": "participated_in", "object": "issues", "event_date": "2026-06-14"},
        # the spine's OWN canonical reified edges (object IS a handle) → survive
        {"subject": "user", "rel_type": "participated_in", "object": "racking event on 2026-06-14",
         "event_date": "2026-06-14"},
        {"subject": "racking event on 2026-06-14", "rel_type": "instance_of", "object": "racking event"},
        # a non-event edge with the same date → never touched (different rel)
        {"subject": "user", "rel_type": "has_pet", "object": "fraggle", "event_date": "2026-06-14"},
    ]
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    objs = {(e["rel_type"], e["object"]) for e in kept}
    assert ("participated_in", "way") not in objs
    assert ("participated_in", "sunday") not in objs
    assert ("participated_in", "issues") not in objs
    assert ("participated_in", "racking event on 2026-06-14") in objs, \
        "the spine's OWN reified participation (object is a handle) was wrongly dropped"
    assert ("has_pet", "fraggle") in objs, "a non-event edge sharing the date was wrongly dropped"


def test_date_keyed_distinct_dated_events_both_reified_survive():
    """idx-9 two-event case: turbo @06-14 and rack fest @06-18 are BOTH reified (their objects are
    handles), so both survive; the stray same-date flat @06-14 noise ('way') is dropped. Mirrors how
    a genuinely-distinct 2nd event is captured — as its OWN reified occurrence (a handle), NOT a flat
    bare-noun edge."""
    manifest = {
        # both real events reified → both objects are handles, both dates in event_dates
        "handles": {"turbo on 2026-06-14", "rack fest on 2026-06-18"},
        "titles": set(),
        "event_types": {"racing event", "fest"},
        "event_dates": {"2026-06-14", "2026-06-18"},
    }
    edges = [
        # the two canonical reified participations (objects ARE handles) → BOTH survive
        {"subject": "user", "rel_type": "participated_in", "object": "turbo on 2026-06-14",
         "event_date": "2026-06-14"},
        {"subject": "user", "rel_type": "participated_in", "object": "rack fest on 2026-06-18",
         "event_date": "2026-06-18"},
        # stray same-date flat noise sharing the turbo date → dropped (object not a handle)
        {"subject": "user", "rel_type": "participated_in", "object": "way", "event_date": "2026-06-14"},
    ]
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    objs = {e["object"] for e in kept if e["rel_type"] == "participated_in"}
    assert "turbo on 2026-06-14" in objs, "the turbo @06-14 reified occurrence was wrongly dropped"
    assert "rack fest on 2026-06-18" in objs, "the rack-fest @06-18 reified occurrence was wrongly dropped"
    assert "way" not in objs, "same-date flat noise survived"


def test_date_keyed_no_op_when_no_reified_date():
    """Fail-safe: no reified date AND no handle for the turn → date-keyed clause is a no-op; a dated
    flat participated_in survives so a genuine event is never over-suppressed when the spine produced
    no dated occurrence (manifest fully empty → legacy behavior)."""
    manifest = {"handles": set(), "titles": set(), "event_types": set(), "event_dates": set()}
    edges = [
        {"subject": "user", "rel_type": "participated_in", "object": "some event", "event_date": "2026-06-14"},
    ]
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    assert {e["object"] for e in kept} == {"some event"}, \
        "a dated flat participation was dropped though the manifest was fully empty"


def test_date_keyed_flat_fallback_occurrence_object_survives():
    """The reified seam's FLAT-FALLBACK canonical occurrence (a bare common-noun event, e.g. 'mass')
    has its participated_in OBJECT added to handles, so it is exempt from the date-keyed drop while a
    same-date bare-noun NOISE edge ('sunday') sharing that date is purged."""
    manifest = {
        # flat-fallback canonical occurrence: object 'mass' lands in handles (manifest line ~1378)
        "handles": {"mass"},
        "titles": set(),
        "event_types": set(),
        "event_dates": {"2026-06-14"},
    }
    edges = [
        # canonical flat-fallback occurrence (object IS a handle) → survives
        {"subject": "user", "rel_type": "participated_in", "object": "mass", "event_date": "2026-06-14"},
        # same-date discourse/day-word noise (object not a handle) → dropped
        {"subject": "user", "rel_type": "participated_in", "object": "sunday", "event_date": "2026-06-14"},
    ]
    kept = m._suppress_flat_event_duplicates(edges, manifest, is_edgeinput=False)
    objs = {e["object"] for e in kept if e["rel_type"] == "participated_in"}
    assert "mass" in objs, "the canonical flat-fallback occurrence was wrongly dropped"
    assert "sunday" not in objs, "same-date day-word noise survived"


# ─────────────────── T1 — end-to-end reify → manifest → suppress (one clean event) ───────────────────

@requires_model
def test_t1_date_suffixed_llm_dupe_suppressed_one_clean_event():
    """T1: 'I participated in "Turbocharged Tuesdays" racking event on June 14th' — the spine
    reifies the occurrence CLEAN; the date-suffixed LLM dupe (turbocharged tuesdays on 2023-06-14)
    is purged, leaving ONE canonical reified participation (the spine's, object = handle)."""
    text = ("[Date: 2023/06/20] I participated in 'Turbocharged Tuesdays' "
            "racking event on June 14th")
    ref, body = m._compute_ingest_reference_and_text(text)
    reified = m._detect_event_states_reified(body, ref)
    assert reified, "the spine produced no reified occurrence for the T1 turn"
    manifest = m._reified_event_manifest(reified)
    assert manifest["handles"], "manifest carries no reified handle"

    # the spine's own edges as dicts + the garbled date-suffixed LLM flat dupe
    spine_dicts = [{"subject": e["subject"], "rel_type": e["rel_type"], "object": e["object"]}
                   for e in reified]
    llm_dupe = {"subject": "user", "rel_type": "participated_in",
                "object": "turbocharged tuesdays on 2023-06-14"}
    kept = m._suppress_flat_event_duplicates([llm_dupe] + spine_dicts, manifest, is_edgeinput=False)

    # exactly ONE participated_in survives, and it is the spine's (its object is a reified handle).
    parts = [e for e in kept if e["rel_type"] == "participated_in"]
    assert len(parts) == 1, f"expected ONE clean participation, got {[p['object'] for p in parts]!r}"
    assert parts[0]["object"] in manifest["handles"], \
        "the surviving participation is not the spine's reified handle"


# ── DATE-SKIP in analyze_event's governed-prep pobj loop (date-PP-as-event bug) ───────────────────
# A governed-prep pobj that is itself a DATE must NEVER be selected as the eventive head: "a date is
# NEVER a relationship object." Before the fix, "got back from the 'Rack Fest' event ON JUNE 18TH"
# could select "18th" (a NOUN pobj of the trailing `on` PP) as the eventive noun, fragmenting the
# rack handle. The fix skips a DATE pobj (via the existing _collect_date_spans detector — dep_/NER,
# no word-list) before bucketing, so the named occurrence head ('Rack Fest') wins and the handle
# converges across mentions whether or not a trailing date is present.

@requires_model
def test_analyze_event_skips_date_pobj_picks_named_event():
    """The trailing 'on June 18th' date PP must not divert the eventive head — 'Rack Fest' (the
    named occurrence) is selected, never '18th'/'June'."""
    from src.extraction.linguistics import analyze_event
    ev = analyze_event("I got back from the 'Rack Fest' event on June 18th.")
    assert ev is not None, "analyze_event found no occurrence for the dated Rack Fest turn"
    surface = f"{(ev.event or '').lower()} {(ev.title or '').lower()}"
    assert "rack" in surface or "fest" in surface, \
        f"named event head not selected: event={ev.event!r} title={ev.title!r}"
    assert "18" not in surface and "june" not in surface, \
        f"a DATE leaked into the eventive head: event={ev.event!r} title={ev.title!r}"


@requires_model
def test_analyze_event_rack_handle_converges_with_and_without_trailing_date():
    """The date-skip fix makes the rack handle DATE-INVARIANT at the parse level: a mention with a
    trailing 'on June 18th' and one without fold to the SAME occurrence handle (one node)."""
    from src.extraction.linguistics import analyze_event
    ev_dated = analyze_event("I got back from the 'Rack Fest' event on June 18th.")
    ev_bare = analyze_event("I attended the 'Rack Fest' event.")
    assert ev_dated is not None and ev_bare is not None
    h_dated = m._occurrence_handle(ev_dated.event, ev_dated.title, None)
    h_bare = m._occurrence_handle(ev_bare.event, ev_bare.title, None)
    assert h_dated is not None and h_dated == h_bare, (h_dated, h_bare)


# ── CONVERGENCE-BY-IDENTITY: same occurrence, two parses → ONE node ──────────────────────────────
# Regression guard for the reified-occurrence FRAGMENTATION blocker (litmus Q10): the SAME real
# occurrence was minting 2-4 surrogate nodes because the handle baked the date / an "on <date>"
# prefix into its identity, so the date scalar landed on one node and the name on another. The handle
# is now DATE/PREFIX-INDEPENDENT — it derives only from the folded (title ∪ type) content tokens — so
# every parse variant of the same occurrence resolves to ONE uuid5 surrogate.

import uuid as _uuid_conv  # noqa: E402


def _surrogate(user_id: str, name: str) -> str:
    # Mirror EntityRegistry._make_surrogate: uuid5(user_namespace, name.lower().strip()).
    try:
        ns = _uuid_conv.UUID(user_id)
    except (ValueError, AttributeError):
        ns = _uuid_conv.uuid5(_uuid_conv.NAMESPACE_DNS, user_id)
    return str(_uuid_conv.uuid5(ns, name.lower().strip())).lower()


def test_occurrence_handle_is_date_and_prefix_independent():
    """The handle identity must NOT change with the date or any 'on <date>' surface — that was the
    fragmentation. Same type(+title), different dates → identical handle."""
    h_undated = m._occurrence_handle("turbocharged tuesdays", None, None)
    h_jun14 = m._occurrence_handle("turbocharged tuesdays", None, "2026-06-14")
    h_jun18 = m._occurrence_handle("turbocharged tuesdays", None, "2026-06-18")
    assert h_undated is not None
    assert h_undated == h_jun14 == h_jun18, (h_undated, h_jun14, h_jun18)
    assert "2026" not in h_undated and " on " not in h_undated, h_undated


def test_inconsistent_parses_of_one_event_converge_to_one_surrogate():
    """Turbocharged Tuesdays parses as event='turbocharged tuesdays',title=None in one turn and
    event='tuesdays',title='Turbocharged' in another (proper-noun splits off when a date trails).
    BOTH must fold to the SAME handle → SAME uuid5 surrogate (convergence-by-identity)."""
    user = "throwaway-conv-tenant"
    h_a = m._occurrence_handle("turbocharged tuesdays", None, "2026-06-14")   # untitled parse, dated
    h_b = m._occurrence_handle("tuesdays", "Turbocharged", "2026-06-14")      # title-split parse
    h_c = m._occurrence_handle("turbocharged tuesdays", None, None)           # untitled, undated
    assert h_a == h_b == h_c, (h_a, h_b, h_c)
    assert _surrogate(user, h_a) == _surrogate(user, h_b) == _surrogate(user, h_c)


def test_two_mentions_one_with_title_one_with_date_converge_with_both():
    """The core acceptance: two mentions of ONE event — one carrying a TITLE, one carrying a DATE —
    converge to ONE entity that carries BOTH the name (also_known_as) and the date (event_date)."""
    # Mention 1: titled, no date.
    e1 = m._reify_occurrence_edges("tuesdays", "Turbocharged", None, None)
    # Mention 2: untitled parse, dated.
    e2 = m._reify_occurrence_edges("turbocharged tuesdays", None, "2026-06-14", "day")
    assert e1 and e2

    handle1 = next(x["object"] for x in e1 if x["rel_type"] == "participated_in")
    handle2 = next(x["object"] for x in e2 if x["rel_type"] == "participated_in")
    # ONE node: both mentions resolve to the SAME surrogate handle.
    assert handle1 == handle2, (handle1, handle2)

    # The DATE coheres on that node (from mention 2's participated_in edge).
    dated = [x for x in e2 if x["rel_type"] == "participated_in" and x.get("event_date")]
    assert dated and dated[0]["event_date"] == "2026-06-14"

    # The NAME coheres on that node (also_known_as rides the SAME handle, never the bare type node).
    names1 = {x["object"].lower() for x in e1 if x["rel_type"] == "also_known_as"}
    names2 = {x["object"].lower() for x in e2 if x["rel_type"] == "also_known_as"}
    assert all(
        x["subject"] == handle1 for x in (e1 + e2) if x["rel_type"] == "also_known_as"
    ), "a name leaked off the converged occurrence node"
    assert names1 and names2  # both mentions name the SAME node


def test_generic_bare_type_without_title_stays_flat_no_overreify():
    """A single bare common type ('webinar') with no title is NOT a distinguishable per-occurrence
    identity → handle None → flat shape preserved (no date-baked over-reification)."""
    assert m._occurrence_handle("webinar", None, "2026-06-18") is None
    assert m._occurrence_handle("meeting", None, None) is None


# ── INCHOATIVE dated-start capture (LongMemEval q7) ──────────────────────────────────────────────
# "I started <item> on DATE" was dropped: analyze_event only fires on the light-verb class. The new
# analyze_inchoative lane (wired into _detect_event_states / _detect_event_states_reified) captures
# the ingressive-verb + concrete-direct-object shape with the clause date, so "which seeds were
# started first?" resolves. Real turns from longmemeval_oracle q-index 7.

@requires_model
def test_inchoative_marigold_real_turn_dated_start():
    """marigold real turn → a DATED participation on the CROP ANCHOR ('marigold'), + its plural alias.

    This used to assert the object was the full NP 'marigold seeds'. That expectation predates the
    crop-anchor lane and is the wrong target: what recall has to resolve is the CROP, because the
    question asks "which SEEDS were started first, the tomatoes or the MARIGOLDS?" — the operand is
    the crop surface, not the NP. The lane files the crop token + its morphological variant as an
    alias, which is exactly what the comparison walk grounds against.

    VERIFIED on pre-prod (LongMemEval q8, `gpt4_385a5000`): recall HIT 3/3, matched_on=['tomato'] —
    i.e. the crop token IS the resolution key. Asserting the NP here would pin a surface the query
    never asks for. Bug ref: RED1-MARIGOLD-NP.
    """
    text = "I just started some marigold seeds that arrived on March 3rd, and they are already germinating."
    ref, body = m._compute_ingest_reference_and_text("[Date: 2026/03/15] " + text)
    reified = m._detect_event_states_reified(body, ref)
    parts = [e for e in reified if e.get("rel_type") == "participated_in"]
    assert parts, "no participation captured for the marigold inchoative start"
    p = parts[0]
    # The crop anchor is the participation object, and it carries the clause date.
    assert (p.get("object") or "") == "marigold", p
    assert (p.get("event_date") or "").startswith("2026-03-03"), p
    # …and the plural surface the question actually uses ("marigolds") resolves to it.
    assert any(e.get("rel_type") == "also_known_as" and e.get("subject") == "marigold"
               and e.get("object") == "marigolds" for e in reified), \
        "the plural crop surface ('marigolds') — the operand the query uses — was not aliased"


@requires_model
def test_inchoative_tomato_real_turn_dated_start():
    """tomato real turn → a dated 'seeds' start (2026-02-20); tomatoes are among these seeds, so the
    02-20 batch precedes the marigolds' 03-03 start → 'which started first' resolves to tomatoes."""
    text = ("I have been starting seeds indoors under grow lights in my basement since February 20th "
            "- tomatoes, peppers, and cucumbers are all doing well, about 2-3 inches tall now.")
    ref, body = m._compute_ingest_reference_and_text("[Date: 2026/03/15] " + text)
    reified = m._detect_event_states_reified(body, ref)
    parts = [e for e in reified if e.get("rel_type") == "participated_in" and e.get("event_date")]
    assert parts, "no DATED participation captured for the tomato inchoative start"
    assert any((e.get("event_date") or "").startswith("2026-02-20") for e in parts), \
        [(e.get("object"), e.get("event_date")) for e in parts]


@requires_model
def test_inchoative_does_not_over_capture_verb_complement():
    """Over-capture firewall at the seam: 'I started thinking on Monday' has a VERB complement →
    no inchoative occurrence (the item-is-NOUN guard holds end-to-end)."""
    ref, body = m._compute_ingest_reference_and_text("[Date: 2026/03/15] I started thinking on Monday.")
    reified = m._detect_event_states_reified(body, ref)
    # No (user, participated_in, <noun item>) occurrence from a bare verb-complement start.
    assert not [e for e in reified if e.get("rel_type") == "participated_in"], reified


# ── Q4 ACQUISITION LANE on the LIVE reified path (the bug this mirrors fixes) ─────────────────────
# The acquisition lane (user→owns→device + located_in, NO participated_in locative-event) was only
# wired into the legacy fast-path _detect_event_states; under OCCURRENCE_REIFY (default ON) the LIVE
# harvest path is _detect_event_states_reified, so the real acquisition turn minted
# (user, participated_in, mall)/(…, buy store) instead of the inferred ownership. The mirror runs the
# SAME shared _acquisition_lane_edges per-clause on the reified path. Exact real turn (q-idx q2/q4).

@requires_model
def test_acquisition_real_turn_reified_emits_owns_not_participated():
    """The REAL Samsung turn on the LIVE reified path → (user, owns, samsung galaxy s22) dated, the
    locative kept as located_in, and NO participated_in mall / buy-store occurrence."""
    text = ("I recently got a new Samsung Galaxy S22 from the Best Buy store at the mall on "
            "February 20th, and I am loving it so far.")
    ref, body = m._compute_ingest_reference_and_text("[Date: 2023/02/25] " + text)
    reified = m._detect_event_states_reified(body, ref)
    owns = [e for e in reified if e.get("rel_type") == "owns"]
    assert owns, f"no inferred ownership emitted on the reified path: {reified}"
    o = owns[0]
    assert o.get("subject") == "user"
    assert "samsung galaxy s22" in (o.get("object") or "")
    assert (o.get("event_date") or "").startswith("2023-02-20"), o
    assert o.get("fact_provenance") == "llm_inferred", o  # EXPOSED Class-B inferred ownership
    # the locative is kept as located_in (bound to the device), never promoted to a participated_in
    assert any(e.get("rel_type") == "located_in" for e in reified), reified
    # HARD: the bogus locative-as-event occurrences are SUPPRESSED on the live path
    parts = [(e.get("object") or "") for e in reified if e.get("rel_type") == "participated_in"]
    assert not parts, f"acquisition clause must mint NO participated_in occurrence, got: {parts}"


@requires_model
def test_acquisition_reified_dell_dated_owns():
    """A cue-rail acquisition verb on the reified path → owns <device> dated (q2-idx-3 shape)."""
    text = "I bought a Dell XPS 13 on February 25th."
    ref, body = m._compute_ingest_reference_and_text("[Date: 2023/03/01] " + text)
    reified = m._detect_event_states_reified(body, ref)
    owns = [e for e in reified if e.get("rel_type") == "owns"]
    assert owns, f"no owns emitted for the Dell acquisition: {reified}"
    assert "dell xps 13" in (owns[0].get("object") or "")
    assert (owns[0].get("event_date") or "").startswith("2023-02-25"), owns[0]
    assert not [e for e in reified if e.get("rel_type") == "participated_in"], reified


@requires_model
def test_genuine_occurrence_unchanged_by_acquisition_mirror():
    """A real occurrence ('I participated in a webinar on Data Analysis using Python') is NOT an
    acquisition → the acquisition lane does NOT fire and the reified occurrence is still minted."""
    text = "I participated in a webinar on 'Data Analysis using Python'."
    ref, body = m._compute_ingest_reference_and_text("[Date: 2023/02/25] " + text)
    reified = m._detect_event_states_reified(body, ref)
    assert not [e for e in reified if e.get("rel_type") == "owns"], reified
    parts = [e for e in reified if e.get("rel_type") == "participated_in"]
    assert parts, f"genuine occurrence must still reify: {reified}"
    assert any("webinar" in (e.get("object") or "") for e in reified), reified


# ── Q2 MULTI-OCCURRENCE: analyze_events enumerates EVERY eventive head ────────────────────────────
# The Q2 last-first miss: analyze_event returned only the FIRST support-verb object, so an appositive
# TITLED occurrence that is not the clause's first head ("…various workshops, like the workshop on
# 'Effective Time Management'") was dropped — never reified, no title alias, no instance_of. The fix:
# analyze_events enumerates dobj ∪ governed-prep pobj ∪ conj siblings ∪ nested appositive exemplars
# and runs the existing title binder against EACH head's own subtree. analyze_event is the back-compat
# first-element shim (single-event behavior unchanged). All structural (dep_/pos_/det), no word-list.

def _events_index(text):
    from src.extraction.linguistics import analyze_events
    return {(e.event or "").strip().lower(): (e.title or "").strip().lower()
            for e in analyze_events(text)}


@requires_model
def test_analyze_events_appositive_titled_occurrence_not_dropped():
    """The appositive titled occurrence ('the workshop on Effective Time Management') is captured
    alongside the generic lead — the Q2 last-first miss. Title binds to the SPECIFIC workshop."""
    idx = _events_index(
        "I've been attending various workshops, like the workshop on 'Effective Time Management'.")
    assert "workshop" in idx, idx
    assert idx["workshop"] == "effective time management", idx
    # the generic lead must NOT steal the appositive head's title.
    assert idx.get("various workshops") in (None, ""), idx


@requires_model
def test_analyze_events_two_conjoined_titled_occurrences():
    """Two conjoined titled events both surface, each with its OWN title."""
    idx = _events_index(
        "I attended a workshop on 'Intro to Rust', and a webinar on 'Data Pipelines'.")
    assert idx.get("workshop") == "intro to rust", idx
    assert idx.get("webinar") == "data pipelines", idx


@requires_model
def test_analyze_events_such_as_title_binds_to_specific_head():
    """'such as the conference on Cloud Security' → the title binds to CONFERENCE, never the generic
    lead ('several events'). The generic lead carries no title."""
    idx = _events_index("I went to several events, such as the conference on 'Cloud Security'.")
    assert idx.get("conference") == "cloud security", idx
    assert idx.get("several events") in (None, ""), idx


@requires_model
def test_analyze_events_single_event_unchanged():
    """SINGLE-EVENT no-regression: exactly ONE occurrence, title intact — analyze_event (the shim)
    and analyze_events agree on the single head."""
    from src.extraction.linguistics import analyze_event, analyze_events
    txt = "I participated in a webinar on 'Data Analysis using Python'."
    evs = analyze_events(txt)
    assert len(evs) == 1, [(e.event, e.title) for e in evs]
    assert evs[0].event == "webinar" and evs[0].title == "Data Analysis using Python", \
        (evs[0].event, evs[0].title)
    shim = analyze_event(txt)
    assert shim is not None and shim.event == evs[0].event and shim.title == evs[0].title


@requires_model
def test_analyze_events_named_propn_occurrence_suppresses_locative_adjunct():
    """A bare locative adjunct ('in nearby city') is NOT minted as a second occurrence when a named
    (quoted-PROPN) occurrence head exists — preserves the single-head PROPN preference."""
    idx = _events_index("I got back from the 'Rack Fest' in nearby city")
    assert any("rack" in k or "fest" in k for k in idx), idx
    assert "nearby city" not in idx and "city" not in idx, idx


@requires_model
def test_analyze_events_untitled_single_unchanged_no_false_multi():
    """An untitled single occurrence stays exactly one occurrence (no spurious multi-capture)."""
    from src.extraction.linguistics import analyze_events
    assert [(e.event, e.title) for e in analyze_events("I went to a webinar")] == [("webinar", None)]
    # a non-event ingressive verb-complement is not an LVC occurrence.
    assert analyze_events("I started crying.") == []


@requires_model
def test_analyze_events_apostrophe_contraction_does_not_open_a_title():
    """The contraction apostrophe in 'I've' must not open a spurious quoted-title run (T2 regex
    boundary fix); the real quoted title is recovered intact."""
    idx = _events_index(
        "I've been attending various workshops, like the workshop on 'Effective Time Management'.")
    # no title may equal a fragment opened at the I've apostrophe.
    for _t in idx.values():
        assert "ve been" not in _t and "i've" not in _t, idx


def test_match_db_correction_removed_dead_code():
    """The legacy _match_db_correction DB-matching helper was deleted (dead code — uncalled +
    schema-incompatible). The live routing is the spaCy∧LLM dual-gate (_spacy_cue_route)."""
    assert not hasattr(m, "_match_db_correction")
    assert callable(getattr(m, "_spacy_cue_route", None))


# ─────────────── FIX 1 — untitled bare-type occurrence files its subject-matter ───────────────
# ROOT CAUSE: an untitled single-token bare-type occurrence ("an issue with my car's GPS system")
# has no stable per-occurrence identity → _occurrence_handle returns None → _reify_occurrence_edges
# bails → the caller emits the FLAT (user, participated_in, issue) fallback, which DROPPED the
# `concerns` subject-matter ("gps system"). The reified path's hinge-4 (issue→gps via _subject_matter_rel)
# never fired for the untitled case → recall walking "issue" could not reach "GPS system" (L4 starved).
# FIX mirrors hinge-4 onto the flat-fallback: presence-of-concerns gated, metadata-resolved rel, date
# rides participation (the subject-matter link itself carries no date, exactly like the reified hinge-4).

@requires_model
def test_flat_fallback_untitled_occurrence_promotes_argument():
    """FIX B — ARGUMENT PROMOTION over a bland result-noun head, AS SUPERSEDED BY STAGE-3 (12b6e80).

    The PROMOTION half is unchanged and still asserted: a bland head ('issue') carrying an
    OBLIGATORY with-PP argument ('gps system') is ABOUT its argument, so the argument is promoted
    out of the head and the bland head is DEMOTED. Discriminator is argument STRUCTURE (presence of
    `concerns`), not a word-list.

    What CHANGED is the CAST the promoted argument lands in. This test used to demand
    `(user, participated_in, "gps system")`. That edge was itself the bug `12b6e80` fixed: it files
    a device (an Object) as an EVENT — a `participated_in` tail_types violation — which POISONS L4
    (gps lands in the event graph, unwalkable from the car it is part of). Stage-3 now casts the
    promoted pair as the type-clean device STATE `(<affected>, has_state, <problem>)` carrying the
    date, and Stage-2 arbitration retires the type-vetoed participation reading
    (`test_spine_has_state_lane.py` pins that contract; the two are MUTUALLY EXCLUSIVE — forcing the
    old participated_in edge to survive breaks the has_state lane).

    The walk still reaches it deterministically, which was always the point:
        user -owns-> car -part_of^-1-> gps system -has_state-> issue @ 2026-05-03
    """
    ref, body = m._compute_ingest_reference_and_text(
        "I had an issue with my car's GPS system on May 3rd")
    edges = m._detect_event_states_reified(body, ref)
    # The promoted ARGUMENT is the walkable, DATED anchor — as the affected-entity SUBJECT.
    assert any(e.get("rel_type") == "has_state" and e.get("subject") == "gps system"
               and e.get("object") == "issue" and e.get("event_date") for e in edges), \
        "the with-PP argument ('gps system') was not promoted to the dated affected-entity state"
    # The bland head is DEMOTED, and the promoted argument is NEVER filed as an Event object.
    part = [e for e in edges if e.get("rel_type") == "participated_in"]
    assert not any(e.get("object") in ("issue", "gps system") for e in part), \
        "a bland head / promoted argument was wrongly filed as a participated_in Event object"


@requires_model
def test_flat_fallback_no_concerns_unchanged():
    """A bare-type occurrence WITHOUT a `concerns` with-PP ('I had a meeting on May 3rd') emits ONLY
    the flat participation edge — no spurious subject-matter link (fail-safe: presence-gated)."""
    ref, body = m._compute_ingest_reference_and_text("I had a meeting on May 3rd")
    edges = m._detect_event_states_reified(body, ref)
    sm_rel = m._subject_matter_rel()
    assert not [e for e in edges if e.get("rel_type") == sm_rel], \
        "a no-concerns occurrence wrongly emitted a subject-matter edge"
    assert any(e.get("rel_type") == "participated_in" and e.get("object") == "meeting"
               for e in edges), "the bare 'meeting' occurrence was lost"


@requires_model
def test_reified_titled_occurrence_unchanged_by_fix1():
    """A TITLED occurrence still reifies (handle present) — it never enters the flat-fallback, so the
    hinge-4 concerns edge is emitted by _reify_occurrence_edges as before; FIX 1 does not double-fire."""
    ref, body = m._compute_ingest_reference_and_text(
        "I attended the 'Advanced Python' webinar on May 3rd")
    edges = m._detect_event_states_reified(body, ref)
    # The occurrence reified (instance_of webinar present) → flat fallback was not taken.
    assert any(e.get("rel_type") == "instance_of" and e.get("object") == "webinar" for e in edges), \
        "the titled occurrence failed to reify (regression)"


@requires_model
def test_fix_a_fronted_adjunct_prep_pobj_rejected():
    """FIX A — a FRONTED discourse/adjunct PP whose prep precedes the verb's subject ('By the way, I
    had …') must NOT yield its pobj ('way') as an eventive occurrence. Structural: prep index < the
    verb's nsubj index, NO word-list ('by'/'way' never named)."""
    from src.extraction.linguistics import analyze_events
    eas = analyze_events("By the way, I had an issue with my car's GPS system.")
    surfaces = {(ea.event or "").strip().lower() for ea in eas}
    assert "way" not in surfaces, "FIX A: the fronted-adjunct pobj 'way' was wrongly harvested"
    # The genuine occurrence head ('issue', about its gps argument) still surfaces.
    assert "issue" in surfaces, "FIX A over-suppressed the real eventive head 'issue'"


@requires_model
def test_fix_a_governed_complement_prep_kept():
    """FIX A must NOT reject a GENUINE governed-complement prep that FOLLOWS the subject —
    'participate IN a webinar', 'go TO a concert'. The pobj is a real occurrence head."""
    from src.extraction.linguistics import analyze_events
    eas = analyze_events("I participated in a webinar on 'Data Analysis using Python'.")
    surfaces = {(ea.event or "").strip().lower() for ea in eas}
    assert "webinar" in surfaces, "FIX A wrongly dropped the governed-complement 'in a webinar' pobj"
    eas2 = analyze_events("I went to a concert.")
    assert "concert" in {(ea.event or "").strip().lower() for ea in eas2}, \
        "FIX A wrongly dropped the motion-complement 'to a concert' pobj"


@requires_model
def test_fix_b_bland_head_no_argument_unchanged():
    """A bland-ish head WITHOUT any with-PP argument keeps the flat head shape (no promotion, no
    over-suppression). 'I did some research on quantum computing' → flat (user, participated_in,
    research); we deliberately do NOT over-suppress a no-argument head (the deferred ambiguous case)."""
    ref, body = m._compute_ingest_reference_and_text("I did some research on quantum computing")
    edges = m._detect_event_states_reified(body, ref)
    part = [e for e in edges if e.get("rel_type") == "participated_in"]
    assert any(e.get("object") == "research" for e in part), \
        "the no-argument bland head 'research' was over-suppressed"


@requires_model
def test_fix_b_corolla_no_spurious_event():
    """'I got detailing done on my Corolla' must NOT mint a participated_in event for the locative
    adjunct ('corolla') or the result-noun ('detailing') — no LVC support verb governs an occurrence
    here ('detailing' is the clause ROOT)."""
    ref, body = m._compute_ingest_reference_and_text("I got detailing done on my Corolla")
    edges = m._detect_event_states_reified(body, ref)
    objs = {(e.get("object") or "").strip().lower() for e in edges
            if e.get("rel_type") == "participated_in"}
    assert "corolla" not in objs, "the locative adjunct 'corolla' was filed as an event"
    assert "detailing" not in objs, "the result-noun 'detailing' was filed as an event"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# STAGE 2 — SAME-SPAN CANDIDATE ARBITRATION (synthetic competing candidates; no model/DB needed).
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# These exercise the arbitration MACHINERY with SYNTHETIC candidates carrying explicit `_spine_grade`
# values. The REAL Stage-3 competitor shape: the affected entity sits in the participated_in OBJECT
# (`user participated_in <affected>`) AND in the has_state SUBJECT (`<affected> has_state <problem>`),
# so the arbitration groups on the AFFECTED-ENTITY span regardless of role (see _affected_span). They
# prove: a CLEAR grounded winner suppresses the loser; a TIE / CLOSE grade keeps BOTH; an
# UNGROUNDABLE candidate keeps BOTH; a single candidate is untouched; the flag OFF disables it.

def _cand(rel, obj, grade=None, subject="user"):
    e = {"subject": subject, "rel_type": rel, "object": obj, "fact_provenance": "user_stated"}
    if grade is not None:
        e["_spine_grade"] = grade
    return e


def test_arb_clear_winner_suppresses_loser():
    """strong has_state vs crater participated_in for the SAME span → has_state committed,
    participated_in (the crater) suppressed (clear 4-rung gap)."""
    edges = [
        _cand("participated_in", "gps system", grade="crater"),
        _cand("has_state", "issue", grade="strong", subject="gps system"),
        _cand("has_pet", "fraggle"),  # non-competing control — must survive untouched
    ]
    kept = m._arbitrate_spine_candidates(edges, is_edgeinput=False)
    rels = {(e["rel_type"], e["object"]) for e in kept}
    assert ("has_state", "issue") in rels, "the strong winner was dropped"
    assert ("participated_in", "gps system") not in rels, "the crater loser was NOT suppressed"
    assert ("has_pet", "fraggle") in rels, "a non-competing edge was touched"


def test_arb_same_grade_keeps_both():
    """Two SAME-grade candidates for one span → AMBIGUOUS → KEEP BOTH (fail-safe, no drop)."""
    edges = [
        _cand("participated_in", "gps system", grade="moderate"),
        _cand("has_state", "issue", grade="moderate", subject="gps system"),
    ]
    kept = m._arbitrate_spine_candidates(edges, is_edgeinput=False)
    assert len(kept) == 2, "a same-grade tie silently dropped a candidate"


def test_arb_adjacent_grade_keeps_both():
    """A ONE-rung (adjacent) gap (strong vs neutral) is within the ambiguity band → KEEP BOTH."""
    edges = [
        _cand("participated_in", "thing", grade="neutral"),
        _cand("has_state", "issue", grade="strong", subject="thing"),
    ]
    kept = m._arbitrate_spine_candidates(edges, is_edgeinput=False)
    assert len(kept) == 2, "an adjacent-grade gap was wrongly treated as a clear winner"


def test_arb_clear_gap_two_rungs_suppresses():
    """A 2-rung gap (strong vs weak) IS decisive → the weak loser is suppressed."""
    edges = [
        _cand("participated_in", "x", grade="weak"),
        _cand("has_state", "issue", grade="strong", subject="x"),
    ]
    kept = m._arbitrate_spine_candidates(edges, is_edgeinput=False)
    rels = {e["rel_type"] for e in kept}
    assert rels == {"has_state"}, "a decisive 2-rung gap did not suppress the loser"


def test_arb_ungroundable_keeps_both():
    """ANY candidate with NO groundable grade (missing/unknown) → KEEP BOTH (never drop the
    competitor of something we can't score). A `has_state` candidate w/o a Stage-3 scorer is this."""
    edges = [
        _cand("participated_in", "y", grade="strong"),
        # no _spine_grade, scorer returns None for has_state → ungroundable (affected span = subject)
        _cand("has_state", "issue", subject="y"),
    ]
    kept = m._arbitrate_spine_candidates(edges, is_edgeinput=False)
    assert len(kept) == 2, "an ungroundable competitor caused a silent drop"


def test_arb_single_candidate_unchanged():
    """A span with a SINGLE candidate (today's live shape) → no competitor → untouched."""
    edges = [
        _cand("participated_in", "webinar", grade="strong"),
        _cand("has_pet", "fraggle"),
    ]
    kept = m._arbitrate_spine_candidates(edges, is_edgeinput=False)
    assert kept == edges, "a single-candidate span was altered (must be inert)"


def test_arb_flag_off_no_arbitration(monkeypatch):
    """Flag OFF → legacy: even a clear winner/loser pair passes through unchanged."""
    monkeypatch.setenv("SPINE_CANDIDATE_ARBITRATION", "false")
    edges = [
        _cand("participated_in", "z", grade="crater"),
        _cand("has_state", "issue", grade="strong", subject="z"),
    ]
    kept = m._arbitrate_spine_candidates(edges, is_edgeinput=False)
    assert len(kept) == 2, "arbitration fired while the flag was OFF"


def test_arb_distinct_spans_not_arbitrated():
    """Two candidates on DIFFERENT spans never compete (each span has one candidate) → both kept."""
    edges = [
        _cand("participated_in", "span_a", grade="crater"),
        _cand("has_state", "issue", grade="strong", subject="span_b"),
    ]
    kept = m._arbitrate_spine_candidates(edges, is_edgeinput=False)
    assert len(kept) == 2, "candidates on different spans were wrongly arbitrated together"


def test_arb_grade_rank_and_helpers():
    """The grade-rank helper is ordinal on the published ladder; unknown → None (keep-both signal)."""
    assert m._spine_grade_rank("crater") == 0
    assert m._spine_grade_rank("strong") == 4
    assert m._spine_grade_rank("bogus") is None
    assert m._spine_grade_rank(None) is None
    # attached-grade precedence
    assert m._spine_candidate_grade({"rel_type": "has_state", "object": "x",
                                     "_spine_grade": "moderate"}) == "moderate"
    # has_state with no attached grade and no Stage-3 scorer → ungroundable (None)
    assert m._spine_candidate_grade({"rel_type": "has_state", "object": "x"}) is None


# ─────────────────────────────────────────────────────────────────────────────
# SPINE-AUTHORITATIVE REIFIED-OCCURRENCE DROP (the participated_in clobber fix).
# When the deterministic spine (req.edges) already bound an entity surface with a
# CONTENTFUL rel, the reified-event lane must NOT re-cast that same surface as a
# participated_in occurrence (which would purge the spine's child_of/friend_of via
# the title-leak suppressor). Pure tests — no DB, no spaCy, no LLM.
# ─────────────────────────────────────────────────────────────────────────────

class _E:
    """Minimal EdgeInput-like stand-in (attribute access) for _spine_bound_surfaces."""
    def __init__(self, subject, rel_type, object):
        self.subject, self.rel_type, self.object = subject, rel_type, object


def test_spine_bound_surfaces_collects_contentful_surfaces():
    edges = [
        _E("alvin", "child_of", "user"),
        _E("alvin", "instance_of", "son"),
        _E("sam", "friend_of", "user"),
        _E("user", "has_pet", "rex"),
    ]
    surf = m._spine_bound_surfaces(edges, is_edgeinput=True)
    assert "alvin" in surf and "sam" in surf and "rex" in surf and "son" in surf
    # "user" is the universal speaking subject — never a gating surface.
    assert "user" not in surf


def test_spine_bound_surfaces_excludes_participated_in_only_surface():
    """A surface bound ONLY by the occurrence rel must NOT self-exempt the cast we suppress."""
    edges = [_E("user", "participated_in", "occurrence: python webinar")]
    surf = m._spine_bound_surfaces(edges, is_edgeinput=True)
    assert surf == set()


def test_drop_reified_occurrence_named_for_bound_entity():
    """A reified occurrence whose also_known_as title is a spine-bound surface is dropped WHOLE."""
    reified = [
        {"subject": "user", "rel_type": "participated_in", "object": "occurrence: alvin son",
         "event_date": None},
        {"subject": "occurrence: alvin son", "rel_type": "instance_of", "object": "son"},
        {"subject": "occurrence: alvin son", "rel_type": "also_known_as", "object": "alvin"},
    ]
    kept = m._drop_reified_occurrences_for_bound_surfaces(reified, {"alvin", "sam"})
    assert kept == [], "the colliding occurrence's edges were not all dropped"


def test_drop_reified_keeps_legitimate_event():
    """An occurrence the spine did NOT bind (no colliding surface) is fully preserved."""
    reified = [
        {"subject": "user", "rel_type": "participated_in", "object": "occurrence: python webinar",
         "event_date": "2026-05-03"},
        {"subject": "occurrence: python webinar", "rel_type": "instance_of", "object": "webinar"},
        {"subject": "occurrence: python webinar", "rel_type": "also_known_as", "object": "python"},
    ]
    # spine bound "alvin" (a person), never the webinar/"python" event surface.
    kept = m._drop_reified_occurrences_for_bound_surfaces(reified, {"alvin"})
    assert len(kept) == 3, "a legitimate event was wrongly dropped"


def test_drop_reified_mixed_drops_only_collider():
    """Mixed set: drop ONLY the spine-bound occurrence, keep the legitimate event occurrence."""
    reified = [
        {"subject": "user", "rel_type": "participated_in", "object": "occurrence: alvin son"},
        {"subject": "occurrence: alvin son", "rel_type": "also_known_as", "object": "alvin"},
        {"subject": "user", "rel_type": "participated_in", "object": "occurrence: python webinar",
         "event_date": "2026-05-03"},
        {"subject": "occurrence: python webinar", "rel_type": "instance_of", "object": "webinar"},
    ]
    kept = m._drop_reified_occurrences_for_bound_surfaces(reified, {"alvin"})
    objs = {(e["subject"], e["object"]) for e in kept}
    assert ("user", "occurrence: python webinar") in objs
    assert ("occurrence: python webinar", "webinar") in objs
    assert all("alvin" not in (e.get("object") or "") and "alvin son" not in (e.get("subject") or "")
               for e in kept), "the alvin collider survived"


def test_drop_reified_flat_shape_object_is_bound_name():
    """Legacy FLAT shape (user, participated_in, <bound-name>) with no handle is dropped directly."""
    reified = [{"subject": "user", "rel_type": "participated_in", "object": "sam"}]
    kept = m._drop_reified_occurrences_for_bound_surfaces(reified, {"sam"})
    assert kept == []


def test_drop_reified_failsafe_empty_inputs():
    assert m._drop_reified_occurrences_for_bound_surfaces([], {"alvin"}) == []
    untouched = [{"subject": "user", "rel_type": "participated_in", "object": "occurrence: x"}]
    assert m._drop_reified_occurrences_for_bound_surfaces(untouched, set()) is untouched


# ───────── EVENT-SIMPLEST-FORM — participle-object descent + user-surface-preservation POISON ─────────
# The light-verb-with-gerund idiom "I had a great TIME celebrating my SAUSAGE PARTY" bleaches the
# eventive content onto the reduced participle ("celebrating"); the matrix dobj "time" is an
# experiential/duration CONTAINER, not the event. The engine derives the SIMPLEST-FORM event surface
# from the participle's object head noun so an anchor ("party" / "birthday party") can REACH the
# occurrence. CRITICAL INVARIANT (DEV/DESIGN-canonicalization.md §2.1, user > seed > growth): the
# derived surface is an ENGINE canonicalization for FINDABILITY only — it must NEVER clobber the
# user's own wording. This suite pins that the reduction (a) recovers the event, (b) PRESERVES the
# user's full surface (modifiers kept — "sausage party", never a bare "party"), and (c) mints NO
# is_preferred=True alias from an engine surface that differs from the user's.

@requires_model
def test_event_simplest_form_reachable_and_dated():
    """'had a great time celebrating my friend's birthday last Saturday' → the occurrence surfaces
    under the simplest event noun ('birthday'), dated, subject=user — findable by a 'birthday' anchor;
    the bleached container paraphrase ('great time …') is suppressed (not filed as an occurrence)."""
    ref, body = m._compute_ingest_reference_and_text(
        "[Date: 2026/06/10] I had a great time celebrating my friend's birthday last Saturday.")
    edges = m._detect_event_states_reified(body, ref)
    part = [e for e in edges if e.get("rel_type") == "participated_in"]
    assert any(e.get("subject") == "user" and e.get("object") == "birthday" and e.get("event_date")
               for e in part), f"simplest-form 'birthday' occurrence not reachable/dated: {part!r}"
    # the bleached container paraphrase must NOT surface as its own occurrence.
    assert not any("time" in (e.get("object") or "") for e in part), \
        f"bleached container paraphrase twin was filed as an occurrence: {part!r}"


@requires_model
def test_event_simplest_form_preserves_user_surface_never_bare_head():
    """POISON (truth invariant): the user's OWN modifier must survive — 'celebrating my sausage party'
    files the occurrence under 'sausage party' (the user's word), NEVER a bare 'party'. Recall renders
    the user's surface; a head-only reduction that dropped 'sausage' would make recall say 'your party'
    instead of 'your sausage party' (a user > engine authority violation)."""
    ref, body = m._compute_ingest_reference_and_text(
        "[Date: 2026/06/10] I had a great time celebrating my sausage party last Saturday.")
    edges = m._detect_event_states_reified(body, ref)
    # The occurrence's human-facing NAME (also_known_as) is the user's FULL surface — modifiers kept.
    akas = {(e.get("object") or "").strip().lower()
            for e in edges if e.get("rel_type") == "also_known_as"}
    types = {(e.get("object") or "").strip().lower()
             for e in edges if e.get("rel_type") == "instance_of"}
    assert "sausage party" in akas, f"user surface 'sausage party' not preserved as the name: {akas!r}"
    # FINDABILITY coexists: the head-noun anchor 'party' is a TOKEN of the preserved surface, so a
    # 'party' anchor reaches the occurrence WITHOUT a lossy bare-'party' surface ever being minted.
    assert all("sausage" in s for s in akas | types if "party" in s), \
        f"a bare 'party' surface (dropping the user's 'sausage') was minted: names={akas!r} types={types!r}"


@requires_model
def test_event_simplest_form_mints_no_preferred_engine_alias():
    """POISON: the engine's simplest-form derivation must never emit an is_preferred=True alias whose
    surface diverges from the user's wording (it clobbers the user's language). The reified edges the
    descent produces carry no is_preferred flag (preference is decided downstream, non-preferred for
    engine growth per the cross-synonym template main.py:7031-7034)."""
    ref, body = m._compute_ingest_reference_and_text(
        "[Date: 2026/06/10] I had a great time celebrating my sausage party last Saturday.")
    edges = m._detect_event_states_reified(body, ref)
    assert not any(e.get("is_preferred") is True for e in edges), \
        f"an engine-derived edge was marked is_preferred=True (may clobber the user surface): {edges!r}"

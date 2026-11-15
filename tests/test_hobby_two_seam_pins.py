"""THE s1 WIRE-DRIVE HOBBY TWO-SEAM FIX (filed honestly in the s1 verdict, 2026-11-13;
DEV/BUGS/wire-drive-s1-20261113/S1-WIRE-FIX-VERDICT.md hobby row).

"On weekends I like to ride my Vespa around town." — the restore row's class landed but
TWO seams blocked the serve:

  (a) CAPTURE — no deterministic front-adverbial activity frame existed (the temporal
      frames are cue-gated: sched-cadence needs a clock time, visit-cadence needs an
      "is with" copula), and the catenative "like TO RIDE" swallowed the activity: only
      (user, like_on, weekends) + (user, owns, vespa) landed. THE FIX: linguistics'
      FRONT_ADVERBIAL_ACTIVITY_FRAME — the front week-cadence PP + the activity verb
      chain (the preference-matrix's infinitival xcomp, else the front-PP's own verb) +
      the direct object → (subject, <activity-lemma>, <object>), with the temporal
      surface preserved on the activity's own verb_prep cadence row
      (subject, <activity-lemma>_on, <cadence>).
  (b) SERVE — the gold token 'vespa' lives on the activity fact the phase-5 lexical
      topic gate drops (the s1 drive measured fetch-all 27 rows → after=0): the
      canonical ask "what do I like to do on weekends?" never surfaces the row's own
      verb, so main.py's SPEAKER-anchored hobby arm (RECALL_HOBBY_ACTIVITY_ADMISSION,
      the aspect-rel-affinity admission's sibling) admits the shelf's activity CLASS
      (migration 319/321's aspect_rel_affinity VALUES) into aspect_bound_rels when the
      first-person activity-ask frame fires AND the store holds LIVE rows on them —
      the same metadata-coupled admission the declared shelves carry.

Both deterministic, flag-gated (OFF = byte-parity). Hermetic pins run everywhere; the
store-coupled arm's door verdict rides the deploy litmus (the migration 321 seed and
the live-row coupling need a real schema).
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("spacy")

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

import src.extraction.linguistics as L  # noqa: E402

# ── THE MEASURED SENTENCES (verbatim from the s1 verdict / the brief) ───────────────
VESPA1 = "On weekends I like to ride my Vespa around town."
RESTORE1 = "On weekends I restore vintage scooters in my garage."
BIKE1 = "On weekends I ride my bike along the river."

_REF = __import__("datetime").datetime(2026, 11, 11)


def _triples(text, ref=_REF):
    return [(f.subject, f.rel_type, str(f.object))
            for f in L.derive_sentence_facts(L._parse(text), ref) or []]


# ══════════════════════════════════════════════════════════════════════════════════
# SEAM (a) — the capture frame

def test_hobby_vespa_activity_row_lands():
    """The measured Vespa sentence: the catenative activity lands as its own row
    (user, ride, vespa) — the hobby ask's gold-token carrier — beside the pre-existing
    like_on/owns rows, and the temporal surface is preserved on the activity's own
    verb_prep cadence row (user, ride_on, weekends)."""
    triples = _triples(VESPA1)
    assert ("user", "ride", "vespa") in triples, triples
    assert ("user", "ride_on", "weekends") in triples, triples
    assert ("user", "like_on", "weekends") in triples, triples   # pre-existing, preserved
    assert ("user", "owns", "vespa") in triples, triples          # pre-existing, preserved


def test_hobby_direct_verb_shapes_no_double_emit():
    """The direct-verb siblings the s1 verdict measured: the SVO activity row already
    landed, so the frame adds ONLY the cadence row — never a duplicate activity row."""
    restore = _triples(RESTORE1)
    assert ("user", "restore", "vintage scooters") in restore, restore
    assert ("user", "restore_on", "weekends") in restore, restore
    assert [t for t in restore if t[1] == "restore"].count(
        ("user", "restore", "vintage scooters")) == 1, restore
    bike = _triples(BIKE1)
    assert ("user", "ride", "bike") in bike, bike
    assert ("user", "ride_on", "weekends") in bike, bike
    assert bike.count(("user", "ride", "bike")) == 1, bike


def test_hobby_frame_flag_off_byte_parity(monkeypatch):
    """FRONT_ADVERBIAL_ACTIVITY_FRAME=false → today's pre-fix behavior byte-for-byte:
    the Vespa sentence carries only (like_on, owns), the direct shapes carry no cadence
    row — the frame never fires."""
    monkeypatch.setattr(L, "FRONT_ADVERBIAL_ACTIVITY_FRAME", False)
    assert ("user", "ride", "vespa") not in _triples(VESPA1)
    assert ("user", "ride_on", "weekends") not in _triples(VESPA1)
    assert ("user", "restore_on", "weekends") not in _triples(RESTORE1)
    assert ("user", "ride_on", "weekends") not in _triples(BIKE1)
    # the pre-fix rows themselves are untouched
    assert ("user", "like_on", "weekends") in _triples(VESPA1)
    assert ("user", "owns", "vespa") in _triples(VESPA1)


def test_hobby_frame_scope_guards():
    """The frame's deterministic scope: a BARE single day ("On Monday I met Anna.") is
    the date layer's territory — its own verb_prep lane already files (user, met→meet_on,
    monday) pre-existing, and the frame's cadence gate never fires (NEVER a second row);
    a TRAILING PP ("I ride my bike on weekends.") stays the verb_prep lanes' territory
    (the frame is FRONT-adverbial only); an interrogative never asserts."""
    monday = _triples("On Monday I met Anna.")
    assert monday.count(("user", "meet_on", "monday")) <= 1, monday
    trailing = _triples("I ride my bike on weekends.")
    assert trailing.count(("user", "ride_on", "weekends")) <= 1, trailing
    q = _triples("What do I like to do on weekends?")
    assert not [t for t in q if t[1] in ("ride", "ride_on")], q


# ══════════════════════════════════════════════════════════════════════════════════
# SEAM (b) — the serve co-admission

def test_hobby_ask_frame_shapes():
    """The first-person activity-ask frame: fires on the canonical phrasings (the ask
    matrix like/love/enjoy/prefer/do, and shelf aspect words like 'ride'), refuses
    non-hobby first-person asks and non-first-person shapes."""
    import src.api.main as M
    assert M._query_hobby_activity_ask("What do I like to do on weekends?") == "like"
    assert M._query_hobby_activity_ask("what do I do on weekends?") == "do"
    assert M._query_hobby_activity_ask("What do I ride?") == "ride"      # the shelf's own word
    assert M._query_hobby_activity_ask("what do we play?") == "play"
    assert M._query_hobby_activity_ask("What do I use for deploys?") is None
    assert M._query_hobby_activity_ask("What did I do last weekend?") is None
    assert M._query_hobby_activity_ask("tell me about my cat") is None
    assert M._query_hobby_activity_ask("where does my wife like to hike?") is None


def test_hobby_activity_class_rels_carry_the_frame_rels():
    """The admission's rel CLASS is the shelf's VALUES — the capture frame's rels
    (ride/play/restore/do) and the prod-drive's run_on are members; the class is
    deterministic (sorted) and non-empty (the floor stands when the DB is down)."""
    import src.api.main as M
    rels = M._aspect_rel_affinity_class_rels()
    assert rels, "the floor must never resolve empty"
    for _r in ("ride", "play", "restore", "do", "run_on"):
        assert _r in rels, (rels,)
    assert list(rels) == sorted(rels)


def test_hobby_aspects_seeded_in_migration_321():
    """The seed file: the hobby members join the EXISTING aspect_rel_affinity class
    (cue → canonical rel in description), public + the tenant fan-out, idempotent —
    the 319 discipline."""
    from pathlib import Path
    sql = Path(REPO, "migrations", "321_seed_hobby_activity_aspects.sql").read_text()
    assert "aspect_rel_affinity" in sql
    for cue, rel in (("ride", "'ride'"), ("play", "'play'"),
                     ("restore", "'restore'"), ("do", "'do'")):
        assert f"('{cue}'," in sql and rel in sql
    assert "ON CONFLICT (cue, category) DO NOTHING" in sql
    assert "faultline\\_%" in sql          # the per-tenant fan-out loop
    assert "'does'" not in sql and "'doing'" not in sql  # the auxiliary never seeds


def test_hobby_serve_arm_source_pins():
    """The serve arm is flag-gated and metadata-coupled in the source: the sibling arm
    reads RECALL_HOBBY_ACTIVITY_ADMISSION, fires only on the speaker anchor, and its
    rows ride aspect_bound_rels (the topic gate's by-construction exemption)."""
    import inspect
    import src.api.main as M
    src = inspect.getsource(M)
    assert "RECALL_HOBBY_ACTIVITY_ADMISSION" in src
    assert "_aspect_rel_affinity_class_rels" in src
    assert "hobby_activity_admitted" in src

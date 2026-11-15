"""THIN-OBJECT QUARANTINE — the bare-generic-head discriminator (TASK A) and the second
alienability enforcement point on the named-instance possession chain (TASK B).

Census source: the 76-question LongMemEval bench archive
``/home/chris/lme-preprod/archive_oracle_combined.preabstention-20260728-001641.json``
(29 rendered ``interested in`` recall lines / 24 distinct objects, re-derived 2026-07-28).

Mechanism (established terms, cited in the helper docstrings):
  * bare singular count noun / only-proper-names-may-be-bare — Longobardi 1994, Linguistic Inquiry
    25(4) "Reference and Proper Names".
  * kind/mass bare nominals as the licensed exception — Carlson 1977; Chierchia 1998, NALS 6.
  * general nouns / shell nouns — Halliday & Hasan 1976; Schmid 2000.
  * alienable vs inalienable possession — Nichols 1988; Chappell & McGregor 1996.

These tests exercise the DISCRIMINATOR in isolation (no DB, no LLM, no network). The spaCy
singleton is required; ``tools/fltest.py`` sets SPACY_MODEL=en_core_web_sm.
"""
import pytest

from src.api.main import (
    _object_is_thin_generic_reference as thin,
    _rel_range_is_unconstrained,
    _surface_is_bare_common_type_noun,
)

# ── The REAL census. (surface, source sentence in the casing a user would have written.) ─────────
# JUNK — contentless bare generic heads rendered as "You are interested in <X>".
CENSUS_JUNK = [
    ("options", "I was looking at some options."),
    ("tip", "Do you have a tip for me?"),
    ("film", "I want to watch the film."),
    ("gift", "I need to buy a gift."),
    ("earring", "I lost an earring."),
    ("motif", "There is a motif running through it."),
    ("place", "I need a place to stay."),
]
# JUNK the discriminator does NOT catch — pinned as KNOWN RESIDUE, not as correct behaviour.
CENSUS_JUNK_RESIDUE = [
    ("games", "I have been playing games."),
    ("which", "I cannot decide which to pick."),
]
# LEGIT — must ALL survive. Zero legitimate loss is the bar.
CENSUS_LEGIT = [
    ("minimalist chic earrings", "I am interested in minimalist chic earrings."),
    ("cinematography workshop", "I signed up for a cinematography workshop."),
    ("recommendations on gifts", "I would like recommendations on gifts."),
    ("recommendations for independent films", "I want recommendations for independent films."),
    ("Florida Project", "I loved Florida Project."),
    ("themes of social justice", "The film explores themes of social justice."),
    ("gaming news", "I follow gaming news."),
    ("games with strong narratives", "I enjoy games with strong narratives."),
    ("more indie games", "I want more indie games."),
    ("Moonlight", "I watched Moonlight last night."),
    ("Gris", "I played Gris on my laptop."),
    ("Ori", "I played Ori last weekend."),
    ("Cuphead", "I finally beat Cuphead."),
    ("Spelunky", "I keep replaying Spelunky."),
    ("Farewell", "I watched Farewell yesterday."),
]


@pytest.mark.parametrize("surface,text", CENSUS_JUNK)
def test_census_junk_is_demoted(surface, text):
    assert thin(surface, text) is True, f"junk {surface!r} should be quarantined"


@pytest.mark.parametrize("surface,text", CENSUS_LEGIT)
def test_census_legit_is_never_demoted(surface, text):
    """THE BAR: zero legitimate loss. A false demotion here is a real capture regression."""
    assert thin(surface, text) is False, f"LEGIT {surface!r} was wrongly demoted"


@pytest.mark.parametrize("surface,text", CENSUS_JUNK_RESIDUE)
def test_known_residue_is_kept_fail_safe(surface, text):
    """Documented residue: unknown → KEEP. Pinned so a future change that catches these is a
    deliberate, measured improvement rather than an accident."""
    assert thin(surface, text) is False


def test_confusion_matrix_meets_the_bar():
    """The headline number, asserted as a whole so a regression cannot hide in one parametrization."""
    legit_lost = [s for s, t in CENSUS_LEGIT if thin(s, t)]
    junk_caught = [s for s, t in CENSUS_JUNK if thin(s, t)]
    assert legit_lost == [], f"legitimate objects wrongly demoted: {legit_lost}"
    assert len(junk_caught) == len(CENSUS_JUNK)


# ── The three signals, each pinned independently ─────────────────────────────────────────────────

def test_modifier_content_keeps_a_modified_nominal():
    """Signal (2): a modified nominal carries real descriptive content. This is the
    negative-decision counterpart of 'no head-reduction for a negative decision' — we never reduce
    'minimalist chic earrings' to 'earrings' in order to condemn it."""
    assert _surface_is_bare_common_type_noun("minimalist chic earrings") is True  # signal (1) alone
    assert thin("minimalist chic earrings", "I am interested in minimalist chic earrings.") is False


def test_orthographic_proper_name_signal_requires_source_text():
    """Signal (3) — the reason this test lives at CAPTURE and not at render.

    On the LOWERCASED stored surface alone, spaCy tags real titles as common nouns, so the first
    two signals would condemn them. The non-sentence-initial capital in the SOURCE TEXT is the
    evidence that survives only at ingest."""
    for title in ("Ori", "Cuphead", "Farewell"):
        # signals (1)+(2) alone would demote it …
        assert _surface_is_bare_common_type_noun(title) is True
        # … and the in-context orthographic signal rescues it.
        assert thin(title, f"I played {title} last weekend.") is False


def test_sentence_initial_capital_is_not_proper_name_evidence():
    """A capital in sentence-initial position is orthography, not proper-name evidence — otherwise
    every junk head that happened to start a sentence would escape."""
    assert thin("options", "Options were limited that day.") is True


def test_digit_bearing_and_styled_surfaces_are_kept():
    """Inherited from the reused hard-line predicate: a value/identifier and a CamelCase product
    name are never bare common type nouns."""
    assert thin("iphone 15", "I bought an iphone 15.") is False
    assert thin("TrueNAS", "I run TrueNAS at home.") is False


# ── Fail-safe contract: unknown → KEEP, always ───────────────────────────────────────────────────

@pytest.mark.parametrize("surface,text", [
    ("", "I am interested in options."),      # no surface
    ("   ", "I am interested in options."),   # blank surface
    ("options", ""),                          # no source text
    ("options", "   "),                       # blank source text
])
def test_fail_safe_inputs_never_demote(surface, text):
    assert thin(surface, text) is False


def test_probe_exception_never_demotes(monkeypatch):
    """ANY error in the probe keeps today's classification. A capture is never lost to uncertainty."""
    import src.api.main as m

    def _boom(_s):
        raise RuntimeError("spaCy exploded")

    monkeypatch.setattr(m, "_surface_is_bare_common_type_noun", _boom)
    assert m._object_is_thin_generic_reference("options", "I saw some options.") is False


# ── The rel-range gate: stand aside wherever the existing constraint machinery can act ───────────

def test_unconstrained_range_detection(monkeypatch):
    """Only a rel that declares NO usable range is eligible. A rel with a concrete declared range is
    owned by WGM_SEED_TYPE_CONSTRAINT_ENFORCE and must never be double-gated here.

    ``has_interest_in`` is seeded ``tail_types={ANY,Concept}`` on pre-prod — the ANY wildcard means
    the SHACL value-type check has nothing to enforce, which is exactly the gap being closed."""
    import src.api.main as m
    table = {
        "has_interest_in": {"tail_types": ["ANY", "Concept"]},   # seeded, but wildcard → eligible
        "interest": {"tail_types": ["ANY"]},                      # engine-grown wildcard → eligible
        "owns": {"tail_types": ["Animal", "Object", "Organization"]},  # concrete → stand aside
        "lives_in": {"tail_types": ["Location"]},                 # concrete → stand aside
        "novel_grown_rel": {},                                    # no metadata at all → eligible
    }
    monkeypatch.setattr(m, "_rel_meta", lambda r=None: table.get(r, {}) if r else table)
    assert _rel_range_is_unconstrained("has_interest_in") is True
    assert _rel_range_is_unconstrained("interest") is True
    assert _rel_range_is_unconstrained("novel_grown_rel") is True
    assert _rel_range_is_unconstrained("owns") is False
    assert _rel_range_is_unconstrained("lives_in") is False
    assert _rel_range_is_unconstrained("") is False
    assert _rel_range_is_unconstrained(None) is False


def test_rel_range_probe_failure_stands_aside(monkeypatch):
    """Fail-safe direction: an unreadable overlay must NOT make everything eligible."""
    import src.api.main as m

    def _boom(_r=None):
        raise RuntimeError("overlay down")

    monkeypatch.setattr(m, "_rel_meta", _boom)
    assert _rel_range_is_unconstrained("has_interest_in") is False


def test_flag_defaults_off():
    """SAAS/prod safety: the lane ships OFF, so classification is byte-identical until switched on."""
    import src.api.main as m
    assert m.INGEST_THIN_OBJECT_QUARANTINE is False


# ── TASK B: the second alienability enforcement point (named-instance possession) ────────────────

def test_named_instance_possession_gate_demotes_an_inalienable_place():
    """'My hometown is Guelph' must not read as OWNERSHIP.

    The named-instance chain re-homes the possession onto the NAME and its ``_ni_suppress`` entry
    makes ``_chain_possessive`` step aside, so the alienability gate that chain applies was never
    reached for this construction. The possessum tested is the TYPE noun ('hometown'), because
    alienability is a property of the KIND of thing possessed — never of the name."""
    from src.extraction import linguistics as L
    tails = {"owns": ["Animal", "Object", "Organization"], "related_to": ["ANY"]}
    orig = L._rel_tail_types
    try:
        L._rel_tail_types = lambda rel: list(tails.get((rel or "").strip().lower(), []))
        # associative PLACE → outside the declared ownership range → demote
        assert L._possessum_admitted_by_rel_tail("hometown", "", "owns") is False
        assert L._possessum_admitted_by_rel_tail("birthplace", "", "owns") is False
        # real possessions are untouched
        assert L._possessum_admitted_by_rel_tail("car", "", "owns") is True
        assert L._possessum_admitted_by_rel_tail("laptop", "", "owns") is True
    finally:
        L._rel_tail_types = orig

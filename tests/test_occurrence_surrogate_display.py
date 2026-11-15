"""OCCUR — the internal occurrence SURROGATE must never be rendered as a display label.

THE BUG (read-side, not capture): ``_occurrence_handle`` deliberately mints
``"occurrence: <sorted tokens>"`` as an INTERNAL surrogate so two inconsistent parses of the same
real event converge onto ONE uuid5 node. The human name rides ``also_known_as`` (skos:altLabel) on
that node. But ``EntityRegistry.resolve()`` (src/entity_registry/registry.py:214-218) registers the
FIRST-SEEN surface with ``is_preferred = true`` — and for a reified occurrence that first surface IS
the handle. The title then lands ``is_preferred = false`` (main.py ~24752:
``is_pref = edge.is_preferred_label or object in preferred_objects``). Every display resolver orders
``is_preferred DESC`` and therefore renders the MACHINE IDENTIFIER.

Verified on the wire (pre-prod seat1, 2026-07-27), entity 0d7e2c20-…:
    "occurrence: analysis data python using" | is_preferred = t | unspecified
    "data analysis using python"             | is_preferred = f | rel_default
  → deployed recall rendered:
    'You attended occurrence: analysis data python using data analysis using (on 2023-03-28)'

W3C SKOS Reference §5 (integrity conditions S13/S14) + §1.2 draw exactly this line: the URI/
identifier is for machines, the label (prefLabel/altLabel) is for humans. CLAUDE.md already maps
also_known_as → skos:altLabel and pref_name → skos:prefLabel, so rendering the surrogate violates
the model the repo declares.

THE FIX: ``_resolve_surrogate_display`` (src/api/main.py) — display-layer only, flag-gated by
``SURROGATE_ALTLABEL_RENDER``. The stored handle is untouched (convergence-by-identity preserved);
registration is untouched (the registry stays dumb).

Run: python3 tools/fltest.py --bug OCCUR --test tests/test_occurrence_surrogate_display.py
     (tests/ is gitignored → git add -f if committing)
"""
import os

import pytest

os.environ.setdefault("SURROGATE_ALTLABEL_RENDER", "1")

main = pytest.importorskip("src.api.main")


# ── The REAL rows read from pre-prod seat1 after ingesting
#    "I attended the 'Data Analysis using Python' webinar on March 28th 2023."
#    (ORDER BY is_preferred DESC, alias — exactly what the query lane fetches.)
_LIVE_OCCURRENCE_ID = "0d7e2c20-74e3-5194-ab3b-353c81a198f9"
_LIVE_ALIASES_PREF_FIRST = [
    "occurrence: analysis data python using",   # is_preferred = TRUE  (the machine handle)
    "data analysis using python",               # is_preferred = FALSE (the skos:altLabel title)
]


class _FakeCursor:
    """Minimal cursor stub: returns ``rows`` for any query. Context-manager compatible."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, *_a, **_k):
        return None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


# ── structural surrogate detection ────────────────────────────────────────────────────────────
def test_handle_is_detected_as_internal_surrogate():
    assert main._is_internal_surrogate_surface("occurrence: analysis data python using")
    assert main._is_internal_surrogate_surface("OCCURRENCE: rack fest")  # normalized


def test_ordinary_labels_are_not_surrogates():
    for _label in ("data analysis using python", "webinar", "diane", "waterloo", ""):
        assert not main._is_internal_surrogate_surface(_label)
    assert not main._is_internal_surrogate_surface(None)


def test_mint_and_read_share_one_marker():
    """The mint site and the read-side detector must not drift (one constant)."""
    _handle = main._occurrence_handle("webinar", "Data Analysis using Python", None)
    assert _handle is not None
    assert main._is_internal_surrogate_surface(_handle)


# ── the fix: altLabel wins over the surrogate ─────────────────────────────────────────────────
def test_altlabel_title_is_rendered_not_the_surrogate():
    _out = main._resolve_surrogate_display(
        _LIVE_OCCURRENCE_ID, _LIVE_ALIASES_PREF_FIRST[0], _LIVE_ALIASES_PREF_FIRST, None)
    assert _out == "data analysis using python"
    assert "occurrence:" not in _out


def test_non_surrogate_display_is_untouched():
    """Every entity that is NOT surrogate-surfaced renders exactly as before."""
    assert main._resolve_surrogate_display("x", "webinar", ["webinar", "seminar"], None) == "webinar"


def _seed_hierarchy_meta(monkeypatch):
    """The type fallback is METADATA-DRIVEN: it walks whatever rels declare is_hierarchy_rel.

    In a DSN-less test process ``_REL_TYPE_META`` is empty, so the fallback correctly NO-OPs
    (fail-safe). Seeding a hierarchy rel here proves the gate reads metadata, not a rel-name
    literal — the rel is named arbitrarily on purpose."""
    monkeypatch.setattr(main, "_REL_TYPE_META", {
        "some_grown_hierarchy_rel": {"is_hierarchy_rel": True},
        "a_flat_rel": {"is_hierarchy_rel": False},
    }, raising=False)


def test_type_fallback_noops_without_hierarchy_metadata(monkeypatch):
    """No metadata → no walk → degrade to the marker-stripped tokens (never raise, never guess)."""
    # "No metadata" means no database to read hierarchy rels from: with POSTGRES_DSN set the
    # hierarchy-rel reader opens its own connection, finds the seeded rels and walks to 'webinar'.
    # This pin used to hold whole-suite only because another module popped the DSN at import (#63).
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    _out = main._resolve_surrogate_display(
        "e1", "occurrence: rack fest", ["occurrence: rack fest"], _FakeDB([("webinar",)]))
    assert _out == "rack fest"


def test_fallback_to_l4_type_when_no_altlabel(monkeypatch):
    """No human label at all → render the L4 TYPE the occurrence is filed at, never the marker."""
    _seed_hierarchy_meta(monkeypatch)
    _db = _FakeDB([("webinar",)])
    _out = main._resolve_surrogate_display(
        "e1", "occurrence: rack fest", ["occurrence: rack fest"], _db)
    assert _out == "webinar"


def test_last_resort_strips_the_marker_and_never_fabricates():
    """No altLabel, no type → content tokens, marker gone, nothing invented."""
    _db = _FakeDB([])
    _out = main._resolve_surrogate_display(
        "e1", "occurrence: rack fest", ["occurrence: rack fest"], _db)
    assert _out == "rack fest"
    assert "occurrence:" not in _out


def test_type_fallback_never_falls_onto_another_surrogate(monkeypatch):
    _seed_hierarchy_meta(monkeypatch)
    _db = _FakeDB([("occurrence: other thing",)])
    _out = main._resolve_surrogate_display(
        "e1", "occurrence: rack fest", ["occurrence: rack fest"], _db)
    assert not main._is_internal_surrogate_surface(_out)


# ── flag discipline ───────────────────────────────────────────────────────────────────────────
def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(main, "SURROGATE_ALTLABEL_RENDER", False)
    _out = main._resolve_surrogate_display(
        _LIVE_OCCURRENCE_ID, _LIVE_ALIASES_PREF_FIRST[0], _LIVE_ALIASES_PREF_FIRST, None)
    assert _out == "occurrence: analysis data python using"  # today's (buggy) behavior, unchanged


# ── the MCP scrubber net (transport belt-and-suspenders) ──────────────────────────────────────
def test_mcp_scrubber_strips_the_marker():
    srv = pytest.importorskip("src.mcp.server")
    _cleaned = srv._clean_for_mcp("You attended occurrence: analysis data python using")
    assert "occurrence:" not in _cleaned
    assert "analysis data python using" in _cleaned

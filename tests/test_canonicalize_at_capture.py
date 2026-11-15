"""canonicalize-at-capture STEP 1 (bug CANONICALIZE-STEP1).

The consolidation's first rollout step (DEV/DESIGN-canonicalization.md §3.4): introduce ONE helper
(`src/api/canonicalize.py::canonicalize_alias`) that HARDWIRES the invariant "engine/canonical
forms are NON-preferred aliases only" and route the SAFEST already-conformant site — the
cross-synonym unification (site B, `_attach_wordnet_hypernym_ladder`) — through it behind the
`CANONICALIZE_AT_CAPTURE` flag (default OFF).

The bar this pins:
  * flag OFF  → byte-identical to today (original inline template runs);
  * flag ON   → the SAME aliases registered via the helper (same UUIDs, same is_preferred=False);
  * INVARIANT → an engine form can NEVER be registered preferred through the helper, and can never
                demote a user_stated surface; a user correction still supersedes (rank ladder).

Pure/offline: real WordNet corpus (for the live-site parity test) + fake cursor/registry, and the
REAL `registry.preference_rank` ladder for the demotion poison test. No network, no live services.
"""
import pytest

from src.api import canonicalize as C
from src.api import main as m
from src.api import wordnet_ladder as W
from src.entity_registry.registry import preference_rank


def _has_corpus() -> bool:
    return W._wn() is not None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Shared fakes (mirror tests/test_wordnet_synset_alias_unification.py)
# ══════════════════════════════════════════════════════════════════════════════════════════════

class _RecordingRegistry:
    """Deterministic UUID-by-surface resolver; records every register_alias call verbatim."""
    def __init__(self):
        self.alias_calls = []  # (canonical_uuid, alias, is_preferred, preference_source)

    def resolve(self, user_id, name):
        return f"uuid::{(name or '').strip().lower()}"

    def register_alias(self, canonical, alias, is_preferred=False,
                       preference_source="unspecified", **kw):
        self.alias_calls.append((canonical, alias, is_preferred, preference_source))


class _NoNameCursor:
    """Answers the naming-edge HARD-LINE probe as 'not a name' (fetchone -> None); no-ops the rest."""
    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _NoNameCursor()

    def rollback(self):
        return None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 1) The helper — the invariant is HARDWIRED (is_preferred can never be True)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def test_helper_registers_each_surface_non_preferred():
    reg = _RecordingRegistry()
    n = C.canonicalize_alias(reg, "node-uuid", ["citrus fruit", "citrous fruit"])
    assert n == 2
    # DEFAULT PROVENANCE MOVED "inferred" -> "lexical" (deliberate contract change).
    # It buys this lane NO extra trust — "lexical" ranks EQUAL to "inferred" in
    # registry._PREFERENCE_RANK — it RECORDS THE WARRANT for the co-reference a second
    # surface asserts (shared WordNet synset membership). The weld guard's ARM 3 reads that
    # warrant; without it, this lane and an extraction-invented weld are byte-identical at the
    # write and the arm would close this lane. See DEV/DESIGN-alias-weld-guard.md §7.
    assert reg.alias_calls == [
        ("node-uuid", "citrus fruit", False, "lexical"),
        ("node-uuid", "citrous fruit", False, "lexical"),
    ]
    # THE INVARIANT: every alias the helper emits is NON-preferred.
    assert all(is_pref is False for (_c, _a, is_pref, _s) in reg.alias_calls)


def test_helper_forces_non_preferred_provenance_never_user_stated():
    reg = _RecordingRegistry()
    # Even asking for a different provenance never makes it preferred, and it is never
    # user_stated. Passing "inferred" explicitly here is deliberate: it proves the caller-supplied
    # value is honoured while the is_preferred=False invariant stays HARDWIRED.
    C.canonicalize_alias(reg, "n", ["doctor"], preference_source="inferred")
    assert reg.alias_calls[0][2] is False              # is_preferred hardwired False
    assert reg.alias_calls[0][3] != "user_stated"       # engine growth, never the sacred rank


def test_helper_skips_empty_and_noop_inputs():
    reg = _RecordingRegistry()
    assert C.canonicalize_alias(reg, "", ["x"]) == 0        # no node
    assert C.canonicalize_alias(reg, "n", []) == 0          # no surfaces
    assert C.canonicalize_alias(reg, "n", ["", None, "ok"]) == 1  # skip falsy surfaces
    assert reg.alias_calls == [("n", "ok", False, "lexical")]


def test_helper_propagates_register_alias_failure_to_caller():
    """The helper does NOT swallow — a register_alias failure PROPAGATES so the CALLER's existing
    rollback/search_path re-bind runs (byte-identical to the inline template)."""
    class _Boom:
        def register_alias(self, *a, **k):
            raise RuntimeError("db down")
    with pytest.raises(RuntimeError):
        C.canonicalize_alias(_Boom(), "n", ["citrus fruit"])


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 2) Flag parity at the LIVE site — flag ON registers the SAME aliases as flag OFF (byte-identical)
# ══════════════════════════════════════════════════════════════════════════════════════════════

pytestmark_corpus = pytest.mark.skipif(not _has_corpus(),
                                       reason="WordNet corpus not available offline")


def _run_site(monkeypatch, canon_flag: str):
    """Run the real _attach_wordnet_hypernym_ladder for 'grapefruit' with the given
    CANONICALIZE_AT_CAPTURE value; return the recorded alias_calls (sorted for comparison)."""
    monkeypatch.setenv("L4_WORDNET_LADDER", "1")
    monkeypatch.setenv("CANONICALIZE_AT_CAPTURE", canon_flag)
    monkeypatch.setattr(m, "_commit_staged",
                        lambda conn, rows, tier, confidence=None: None)
    reg = _RecordingRegistry()
    n = m._attach_wordnet_hypernym_ladder(
        _FakeConn(), "faultline_test",
        concept_object_id="uuid::grapefruit", concept_display="grapefruit",
        object_type="object", registry=reg, user_id="u",
    )
    return n, sorted(reg.alias_calls)


@pytestmark_corpus
def test_flag_on_off_register_identical_aliases(monkeypatch):
    n_off, calls_off = _run_site(monkeypatch, "0")
    n_on, calls_on = _run_site(monkeypatch, "1")
    assert n_off > 0 and n_on == n_off, "same rungs staged either way"
    assert calls_off == calls_on, (
        f"flag ON must register byte-identical aliases to flag OFF;\n OFF={calls_off}\n ON ={calls_on}")
    # And every registered alias is non-preferred under BOTH flag states.
    assert all(is_pref is False for (_c, _a, is_pref, _s) in calls_on)
    # The site B model: 'citrus fruit' is registered as an alias of the citrus node.
    citrus = f"uuid::citrus"
    assert any(c == citrus and a == "citrus fruit" for (c, a, _p, _s) in calls_on)


@pytestmark_corpus
def test_flag_on_never_registers_engine_form_preferred(monkeypatch):
    """Poison: no path through the helper-routed site can register an engine form is_preferred=True."""
    _n, calls_on = _run_site(monkeypatch, "1")
    assert calls_on, "the site must register at least one synonym alias for grapefruit's chain"
    assert not any(is_pref for (_c, _a, is_pref, _s) in calls_on)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 3) INVARIANT poison test — user surface NOT demoted; a correction still supersedes
#    (models register_alias's REAL demotion guard via the REAL preference_rank ladder)
# ══════════════════════════════════════════════════════════════════════════════════════════════

class _LadderRegistry:
    """Faithful in-memory model of EntityRegistry.register_alias's preference-demotion guard
    (registry.py:447/482): a newly-preferred alias only DEMOTES the incumbent preferred alias when
    preference_rank(new) >= preference_rank(incumbent). Uses the REAL `preference_rank`."""
    def __init__(self):
        # node_uuid -> {alias: {"is_preferred": bool, "source": str}}
        self.store = {}

    def register_alias(self, canonical, alias, is_preferred=False,
                       preference_source="unspecified", **kw):
        node = self.store.setdefault(str(canonical), {})
        alias = alias.lower().strip()
        if is_preferred:
            incumbent = next((a for a, r in node.items()
                              if r["is_preferred"] and a != alias), None)
            if incumbent is not None:
                if preference_rank(preference_source) >= preference_rank(node[incumbent]["source"]):
                    node[incumbent]["is_preferred"] = False   # demote
                else:
                    is_preferred = False                       # guard: incoming too weak
        node[alias] = {"is_preferred": is_preferred, "source": preference_source}

    def preferred(self, node_uuid):
        node = self.store.get(str(node_uuid), {})
        return next((a for a, r in node.items() if r["is_preferred"]), None)


def test_helper_never_demotes_user_preferred_surface():
    reg = _LadderRegistry()
    # The user named their event — the sacred preferred surface (rank 5).
    reg.register_alias("evt", "sausage party", is_preferred=True, preference_source="user_stated")
    # Engine growth registers the head/simplest form via the helper (rank 3 — "lexical" ranks
    # EQUAL to "inferred", so the demotion arithmetic below is unchanged by the warrant).
    C.canonicalize_alias(reg, "evt", ["party"])
    assert reg.preferred("evt") == "sausage party", "engine form must NOT demote the user's word"
    assert reg.store["evt"]["party"]["is_preferred"] is False


def test_user_correction_still_supersedes_after_helper_ran():
    reg = _LadderRegistry()
    reg.register_alias("evt", "sausage party", is_preferred=True, preference_source="user_stated")
    C.canonicalize_alias(reg, "evt", ["party"])
    # User corrects: "actually it's my get-together" — user_stated (rank 5) supersedes.
    reg.register_alias("evt", "get-together", is_preferred=True, preference_source="user_stated")
    assert reg.preferred("evt") == "get-together", "a user correction must win via the rank ladder"
    # Growth never fought it: the engine 'party' alias is still present and still non-preferred.
    assert reg.store["evt"]["party"]["is_preferred"] is False

"""FIX A — PROVENANCE PROMOTES, NEVER DEGRADES (USER IS TRUTH; authority user > seed > growth).

Owner principle (2026-07): "If it's USER STATED, the components therein should be PROMOTED,
not degrade the fact." A user-STATED structured fact must rise to Class A (authoritative,
`facts` table) REGARDLESS of the rel_type's SEEDED default class. The seeded `fact_class`
FLOORS *inference* (llm_inferred / llm_learned) only — it must never demote user truth to
the staged B/C tier.

Proven bug this pins the fix for: `favorite_colour`/`likes`/`prefers`/`dislikes`/`has_pet`/
`located_in` are seeded `fact_class=B`, so a user_stated statement on them landed
`staged_facts` Class-B — non-authoritative, never asserting cleanly in recall AND invisible
to the facts-table correction lookup. Now they land `facts` Class-A like lives_in(A)/age(A).

PRESERVE (hard-won, do NOT regress): a C-defined rel (the `feels` affective family, is_a,
member_of, system markers) is the transient/inference tier. A user_stated fact on it stays
durable Class **B** — byte-identical to the pre-fix behaviour. This is NOT an
`if defined_class=='C': return C` (that buries stated feelings); it is the same `("B", 0.8)`
result, now reached only by the C tier. The split is METADATA-driven (seeded class marker),
never a rel/emotion literal.

Metadata-driven throughout — no rel_type name is hardcoded in the DECISION; the tests seed
the in-process rel-meta cache exactly like test_assistant_turn_provenance.py.
"""
import pytest

from src.api import main


@pytest.fixture
def _seed_rel_meta(monkeypatch):
    """Seed the in-process rel-meta cache so assign_class_and_confidence resolves a defined
    fact_class without a DB (the caches are DB-populated at runtime)."""
    def _set(rel, fact_class):
        monkeypatch.setitem(main._REL_TYPE_META, rel, {"fact_class": fact_class})
        if rel in getattr(main, "_REL_TYPE_CACHE", {}):
            monkeypatch.delitem(main._REL_TYPE_CACHE, rel, raising=False)
    return _set


def _cls(rel, provenance, **kw):
    return main.assign_class_and_confidence(
        {"storage": "relational"}, fact_provenance=provenance, rel_type=rel, **kw)


# ─────────────── FIX A: user_stated STRUCTURED (A or B) → authoritative Class A ───────────────

def test_user_stated_b_rel_promotes_to_a(_seed_rel_meta):
    """The core bug: a B-defined preference/relational rel, when USER-STATED, is Class A."""
    _seed_rel_meta("favorite_colour", "B")
    cls, conf = _cls("favorite_colour", "user_stated")
    assert cls == "A"
    assert conf == pytest.approx(1.0)


@pytest.mark.parametrize("rel", ["likes", "prefers", "dislikes", "has_pet", "located_in"])
def test_user_stated_all_b_preference_rels_promote(_seed_rel_meta, rel):
    """Generalizes to EVERY B-defined structured rel — no per-rel special-casing."""
    _seed_rel_meta(rel, "B")
    cls, conf = _cls(rel, "user_stated")
    assert cls == "A", f"user_stated {rel} (defined B) must promote to Class A"
    assert conf == pytest.approx(1.0)


def test_user_stated_a_rel_stays_a(_seed_rel_meta):
    """An A-defined rel (lives_in/age) is unchanged — Class A, conf 1.0."""
    _seed_rel_meta("lives_in", "A")
    assert _cls("lives_in", "user_stated") == ("A", pytest.approx(1.0))


# ─────────────── PRESERVE: user_stated FEELING (C-defined) stays durable B ───────────────

def test_user_stated_feeling_stays_durable_b(_seed_rel_meta):
    """The `feels` family is seeded fact_class=C (transient tier). A STATED feeling parks at
    durable Class B — NOT promoted to A, NOT buried at C. Byte-identical to pre-fix."""
    _seed_rel_meta("feels", "C")
    cls, conf = _cls("feels", "user_stated")
    assert cls == "B"
    assert conf == pytest.approx(0.8)


@pytest.mark.parametrize("rel", ["feels", "is_a", "member_of"])
def test_user_stated_c_defined_rels_stay_b(_seed_rel_meta, rel):
    """No C-defined rel promotes on the user_stated path — the C tier is left durable-B
    exactly as before (this is what protects feelings)."""
    _seed_rel_meta(rel, "C")
    assert _cls(rel, "user_stated") == ("B", pytest.approx(0.8))


# ─────────────── INFERENCE STILL FLOORED (must NOT be promoted) ───────────────

def test_llm_inferred_b_rel_stays_floored_at_b(_seed_rel_meta):
    """An llm_INFERRED fact on a B-defined rel follows the SEEDED class (B) — it is NOT
    promoted to A. Only USER truth promotes; inference is floored by the rel's default."""
    _seed_rel_meta("favorite_colour", "B")
    cls, _ = _cls("favorite_colour", "llm_inferred", confidence=0.8)
    assert cls == "B", "llm_inferred B-rel must stay Class B (inference is floored, not promoted)"


def test_llm_inferred_a_rel_follows_defined(_seed_rel_meta):
    """llm_inferred on an A-defined rel follows the defined class (A) with extraction conf."""
    _seed_rel_meta("lives_in", "A")
    cls, conf = _cls("lives_in", "llm_inferred", confidence=0.75)
    assert cls == "A"
    assert conf == pytest.approx(0.75)


def test_llm_learned_never_class_a(_seed_rel_meta):
    """UNCHANGED firewall: engine /expand growth (llm_learned) never reaches Class A."""
    for fc in ("A", "B", "C"):
        _seed_rel_meta("some_rel", fc)
        cls, _ = _cls("some_rel", "llm_learned")
        assert cls != "A", f"llm_learned defined_class={fc} must never be Class A"


def test_is_correction_always_class_a(_seed_rel_meta):
    """UNCHANGED: a correction overrides everything to Class A / conf 1.0."""
    _seed_rel_meta("feels", "C")
    assert _cls("feels", "user_stated", is_correction=True) == ("A", pytest.approx(1.0))


# ─────────────── FLAG SAFETY: default ON = fixed; OFF = legacy byte-identical ───────────────

def test_flag_off_restores_legacy_degradation(_seed_rel_meta, monkeypatch):
    """USER_STATED_PROMOTES_TO_A=0 restores the exact pre-fix behaviour: user_stated B-rel
    DEGRADES to staged Class B (the bug), user_stated A-rel stays A, feeling stays B."""
    monkeypatch.setenv("USER_STATED_PROMOTES_TO_A", "0")
    _seed_rel_meta("favorite_colour", "B")
    assert _cls("favorite_colour", "user_stated") == ("B", pytest.approx(0.8))
    _seed_rel_meta("lives_in", "A")
    assert _cls("lives_in", "user_stated") == ("A", pytest.approx(1.0))
    _seed_rel_meta("feels", "C")
    assert _cls("feels", "user_stated") == ("B", pytest.approx(0.8))


def test_flag_on_is_default(_seed_rel_meta, monkeypatch):
    """With the flag UNSET (production default), the fix is active: B-rel → A."""
    monkeypatch.delenv("USER_STATED_PROMOTES_TO_A", raising=False)
    _seed_rel_meta("favorite_colour", "B")
    assert _cls("favorite_colour", "user_stated") == ("A", pytest.approx(1.0))


# ─────────────── VERIFY-THE-READ: the REAL production seam (tenant-ONLY overlay) ───────────────
# The tests above seed the GLOBAL cache — but the SHIPPED bug was that a user-GROWN rel
# (favorite_colour, British spelling minted per-tenant) lives ONLY in the tenant schema and
# is ABSENT from public/the process-global caches. assign_class_and_confidence read the
# global cache directly, so defined_class defaulted to "C" and the promote branch buried the
# preference at durable-B. The fix routes class resolution through _rel_meta() (the per-tenant
# overlay honoring the ContextVar-bound schema). These tests reproduce that exact reality:
# rel ABSENT from the global caches, PRESENT in a ContextVar-bound tenant overlay.

@pytest.fixture
def _tenant_overlay(monkeypatch):
    """Bind a fake tenant schema and warm the per-tenant rel_type overlay cache (no DB),
    reproducing a user-GROWN rel that exists ONLY in the tenant schema, not public."""
    from src.api import rel_type_overlay as _ov

    schema = "faultline_test_grown_tenant"
    # A non-empty DSN so resolve_meta() does not early-return {} (the warm cache below means
    # NO DB is ever touched — cache hit within TTL).
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://unused/warm-cache-only")

    import time as _time
    state = {"tok": None}

    def _bind(rel, fact_class):
        # Ensure the rel is ABSENT from the process-global caches (the production reality).
        monkeypatch.delitem(main._REL_TYPE_META, rel, raising=False)
        if rel in getattr(main, "_REL_TYPE_CACHE", {}):
            monkeypatch.delitem(main._REL_TYPE_CACHE, rel, raising=False)
        # Warm the per-tenant overlay cache so _rel_meta() resolves the tenant row DB-free.
        with _ov._lock:
            entry = _ov._overlay_cache.setdefault(schema, {"meta": {}, "loaded_at": _time.time()})
            entry["meta"][rel] = {"fact_class": fact_class}
            entry["loaded_at"] = _time.time()
        if state["tok"] is None:
            state["tok"] = _ov.set_current_schema(schema)

    yield _bind

    if state["tok"] is not None:
        _ov.reset_current_schema(state["tok"])
    with _ov._lock:
        _ov._overlay_cache.pop(schema, None)


def test_tenant_only_grown_pref_promotes_to_a(_tenant_overlay):
    """The SHIPPED-bug repro: favorite_colour is a per-tenant GROWN B-rel, ABSENT from the
    global cache. With the tenant overlay bound (as at /ingest), it MUST promote to Class A —
    NOT fall through to the C default and land durable-B."""
    _tenant_overlay("favorite_colour", "B")
    # Sanity: _rel_meta() resolves the tenant-only rel that the global cache cannot see.
    assert main._rel_meta("favorite_colour").get("fact_class") == "B"
    assert _cls("favorite_colour", "user_stated") == ("A", pytest.approx(1.0))


def test_tenant_only_grown_feeling_stays_durable_b(_tenant_overlay):
    """A per-tenant C-defined rel (feels family) still stays durable-B even when resolved via
    the tenant overlay — the FEELINGS firewall is metadata-driven, not global-cache-coupled."""
    _tenant_overlay("feels", "C")
    assert _cls("feels", "user_stated") == ("B", pytest.approx(0.8))

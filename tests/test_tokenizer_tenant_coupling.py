"""Regression gate: the PROCESS-GLOBAL spaCy tokenizer must not be baked from ONE tenant's cues.

WHAT WAS WRONG (reproduced by execution on `saas` @ 95956067, both directions, before any change).
`_reconcile_cue_tokenizer_exceptions` removes a spaCy tokenizer special case whose ORTH casefolds to
a `linguistic_cues` surface — a real and necessary fix, because a cue the tokenizer SHATTERS can
never match any token and the row is silently DEAD (the seeded `id` → ['i','d']). But it read the
surfaces through `resolve_all_cue_surfaces`, which resolves the **ContextVar-bound tenant schema**,
and it runs exactly ONCE, inside `_get_nlp()`, on a module-level singleton. So the tokenizer every
tenant shares was baked from whichever tenant happened to trigger the first model load:

    tenant A first  ->  A's private cue removed the rule for tenant B as well (B, who never
                        declared it, silently got a different tokenizer)
    tenant B first  ->  tenant A's OWN seeded `id` cue stayed DEAD for the entire process lifetime

WHY IT MATTERS — the author's two safety premises were both measured FALSE:

  (1) "removal is monotonic; it can only make a token MORE whole, never worse."
      FALSE, and the failure mode is INVERTED TRUTH rather than a miss. Measured on the real
      deriver with the `cannot` rule removed:
          "I cannot eat peanuts"  ->  (user, eat, peanuts)  negated=False
      (with the rule present: nothing is captured). The engine records the OPPOSITE of what the
      user said. `dep_ == "neg"` is read in dozens of places in this module; deleting the `not`
      shard deletes the negation.

  (2) "the growth engine cannot mint a colliding surface, because it records candidates from
      tokens spaCy produced and a shattered surface is by definition a token spaCy never produces."
      FALSE by CASING: the exception table is keyed on exact ORTH (`cannot`/`Cannot`, but not
      `CANNOT`) while both this comparison and `record_cue_candidate` lowercase. Measured: ALL 151
      shattering alpha orths are produced as a SINGLE token in all-caps.

THE FIX (two coordinated guards, no restructuring — see `_reconcile_cue_tokenizer_exceptions`):
  (a) SEED SCOPE — the surface set is read with the schema ContextVar deliberately UNBOUND (the
      `public` template). Identical for every tenant, independent of load order, and a per-tenant
      GROWN cue can no longer reach the shared tokenizer at all — which makes premise (2)'s
      conclusion TRUE BY CONSTRUCTION rather than assumed.
  (b) POLARITY GUARD — a special case whose expansion carries UD `Polarity=Neg` is never removed,
      however the surface reached the seed. A closed morphological FEATURE, not a word list.

The live behaviour is UNCHANGED: `id` is in the `public` seed, carries no `Polarity=Neg`, and is
still removed — `tests/test_dead_cue_and_atomic_overclaim.py` and
`tests/test_identifier_context_shattered_value.py` stay green.
"""
import time
import warnings

import pytest

warnings.filterwarnings("ignore")  # spaCy/torch CUDA warnings are noise in the test env

from src.api import linguistic_cue_overlay as lco  # noqa: E402
from src.api import rel_type_overlay as rto  # noqa: E402
from src.extraction.linguistics import (  # noqa: E402
    _expansion_is_negating,
    _reconcile_cue_tokenizer_exceptions,
)

TENANT = "faultline_coupling_probe_tenant"
_DSN = "postgresql://unused:unused@127.0.0.1:1/unused"  # never dialled — every read is a cache hit


def _fresh_nlp():
    """A private pipeline, so a test can never poison the module singleton other tests share."""
    spacy = pytest.importorskip("spacy")
    try:
        return spacy.load("en_core_web_sm", disable=["ner"])
    except Exception:  # noqa: BLE001 — model not installed in this env
        pytest.skip("en_core_web_sm not installed")


def _prime(seed_surfaces, tenant_surfaces):
    """Seed the overlay's OWN cache for both scopes, so the resolver answers without a DB.

    This is the seam the DB read populates, so priming it exercises the real resolution path
    (including `get_current_schema()`), not a mock of it.
    """
    now = time.time()
    with lco._lock:
        lco._overlay_cache[f"public::{lco._ALL_SURFACES_KEY}"] = {
            "cues": frozenset(seed_surfaces), "loaded_at": now}
        lco._overlay_cache[f"{TENANT}::{lco._ALL_SURFACES_KEY}"] = {
            "cues": frozenset(tenant_surfaces), "loaded_at": now}


@pytest.fixture
def primed(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", _DSN)
    yield _prime
    with lco._lock:
        for key in (f"public::{lco._ALL_SURFACES_KEY}", f"{TENANT}::{lco._ALL_SURFACES_KEY}"):
            lco._overlay_cache.pop(key, None)


# ── INVARIANT 1: a tenant's cue inventory NEVER mutates the shared tokenizer ─────────

def test_a_tenant_only_cue_never_reaches_the_process_global_tokenizer(primed):
    """THE COUPLING. `hes` is held ONLY by the bound tenant; the seed does not have it.

    Before the fix this removed the `hes`/`Hes` special cases process-wide, so every OTHER tenant
    silently inherited a tokenizer shaped by one tenant's private cue row.
    """
    primed(seed_surfaces={"id"}, tenant_surfaces={"id", "hes"})
    nlp = _fresh_nlp()
    token = rto.set_current_schema(TENANT)
    try:
        _reconcile_cue_tokenizer_exceptions(nlp)
    finally:
        rto.reset_current_schema(token)
    assert "hes" in nlp.tokenizer.rules, (
        "a cue held ONLY by the bound tenant mutated the PROCESS-GLOBAL tokenizer — the seam is "
        "reading the ContextVar-bound schema again; it must read the SEED (unbound → public)")


def test_the_result_is_identical_whichever_tenant_triggers_the_first_load(primed):
    """LOAD-ORDER INDEPENDENCE — the other direction of the same defect.

    Before the fix, a tenant whose schema held no colliding cue would load the model with NO
    reconciliation, leaving the seeded `id` dead for the whole process. The outcome must not
    depend on who got there first.
    """
    primed(seed_surfaces={"id"}, tenant_surfaces=set())
    outcomes = []
    for bound in (TENANT, None):
        nlp = _fresh_nlp()
        token = rto.set_current_schema(bound)
        try:
            _reconcile_cue_tokenizer_exceptions(nlp)
        finally:
            rto.reset_current_schema(token)
        outcomes.append(frozenset(nlp.tokenizer.rules))
    assert outcomes[0] == outcomes[1], (
        "the tokenizer bake depends on which tenant triggered the first model load")


def test_a_seeded_surface_is_still_reconciled_under_a_bound_tenant(primed):
    """The fix must not disable the feature: the SEED's `id` is still removed (live behaviour)."""
    primed(seed_surfaces={"id"}, tenant_surfaces=set())
    nlp = _fresh_nlp()
    token = rto.set_current_schema(TENANT)
    try:
        removed = _reconcile_cue_tokenizer_exceptions(nlp)
    finally:
        rto.reset_current_schema(token)
    assert removed >= 1 and "id" not in nlp.tokenizer.rules
    assert [t.text for t in nlp("my order id is AB123")] == \
        ["my", "order", "id", "is", "AB123"]


# ── INVARIANT 2: removal must never strip a NEGATION ─────────────────────────────────

def test_a_negating_special_case_is_never_removed_even_when_seeded(primed):
    """The POLARITY GUARD. If `cannot` ever lands in the seed, the rule must SURVIVE.

    Removing it would delete the `neg` dependency and capture negated statements as affirmed.
    """
    primed(seed_surfaces={"id", "cannot"}, tenant_surfaces=set())
    nlp = _fresh_nlp()
    token = rto.set_current_schema(None)
    try:
        _reconcile_cue_tokenizer_exceptions(nlp)
    finally:
        rto.reset_current_schema(token)
    assert "cannot" in nlp.tokenizer.rules, (
        "a NEGATING tokenizer special case was removed — negated statements will be captured as "
        "AFFIRMED (measured: 'I cannot eat peanuts' -> (user, eat, peanuts) negated=False)")
    assert "id" not in nlp.tokenizer.rules, "the guard must be surgical, not a blanket refusal"
    assert [c.dep_ for c in nlp("I cannot eat peanuts")].count("neg") == 1


def test_polarity_probe_separates_negating_from_benign_expansions():
    """The discriminator is UD `Polarity=Neg` morphology, not a word list — and it must split the
    two populations cleanly, or the guard is either inert or a blanket veto."""
    nlp = _fresh_nlp()
    for negating in ("cannot", "wont", "dont", "isnt"):
        assert _expansion_is_negating(nlp, negating), f"{negating!r} must be protected"
    for benign in ("id", "im", "hes", "gonna", "thats", "ive"):
        assert not _expansion_is_negating(nlp, benign), f"{benign!r} must stay reconcilable"


def test_probe_fails_safe_toward_keeping_the_rule():
    """An un-probeable pipeline answers 'negating' ⇒ keep the rule ⇒ today's tokenizer.

    A dead cue is a MISS; a stripped negation is a CORRUPTION. Fail toward the miss.
    """
    class _Exploding:
        def __call__(self, _text):
            raise RuntimeError("boom")

    assert _expansion_is_negating(_Exploding(), "anything") is True


def test_the_counterexample_that_justifies_the_polarity_guard():
    """PINNED EVIDENCE, not a behaviour of ours: removing a negating special case DELETES the
    `neg` dependency. If this ever goes green-by-changing (spaCy stops splitting, or starts
    tagging differently), re-measure before relaxing the guard — do not just delete this test."""
    nlp = _fresh_nlp()
    assert [t.dep_ for t in nlp("I cannot eat peanuts")].count("neg") == 1
    rules = dict(nlp.tokenizer.rules)
    for orth in ("cannot", "Cannot"):
        rules.pop(orth, None)
    nlp.tokenizer.rules = rules
    assert [t.dep_ for t in nlp("I cannot eat peanuts")].count("neg") == 0, (
        "the premise behind the polarity guard no longer holds — RE-MEASURE the deriver before "
        "changing the guard")


def test_all_caps_defeats_the_growth_engine_cannot_mint_one_argument():
    """PINNED EVIDENCE for why SEED SCOPE (not the growth argument) is what makes this safe.

    The exception table is keyed on exact ORTH, so an all-caps occurrence is produced as a SINGLE
    token — and `record_cue_candidate` lowercases. A GROWN cue therefore CAN collide, which is
    precisely why the reconciler must never read tenant-grown rows.
    """
    nlp = _fresh_nlp()
    shattering = {o.lower() for o, e in nlp.tokenizer.rules.items() if len(e) > 1 and o.isalpha()}
    assert shattering, "no alpha multi-token special cases — model changed, re-measure"
    reachable = {
        w for w in shattering
        if any(t.text.lower() == w for t in nlp("The " + w.upper() + " thing"))
    }
    assert reachable == shattering, (
        "expected EVERY shattering alpha surface to be reachable as a single all-caps token")
